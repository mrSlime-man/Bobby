import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from src.utils.search_progress import read_new_job_count


def test_missing_progress_is_only_allowed_before_first_round(tmp_path):
    state = tmp_path / "state.json"
    assert read_new_job_count(state, allow_missing=True) == 0
    with pytest.raises(ValueError, match="missing"):
        read_new_job_count(state)


@pytest.mark.parametrize("value", [{}, [], {"aggregate": {"new": True}},
                                   {"aggregate": {"new": -1}},
                                   {"aggregate": {"new": "3"}}])
def test_invalid_progress_never_means_no_new_jobs(tmp_path, value):
    state = tmp_path / "state.json"
    state.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="invalid"):
        read_new_job_count(state, allow_missing=True)


def test_round_progress_reads_cumulative_new_jobs(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"aggregate": {"new": 4, "found": 64}}))
    assert read_new_job_count(state) == 4


@pytest.fixture
def launcher_sandbox(tmp_path):
    if not shutil.which("fish"):
        pytest.skip("fish is required to exercise the real launcher")
    root = Path(__file__).parents[1]
    for folder in ("config", "src/utils", ".venv/bin", "bin", "logs"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    launcher = (root / "run_big_pool.fish").read_text()
    launcher = launcher.replace('set -g LOG_DIR "$HOME/Logs"',
                                f'set -g LOG_DIR "{tmp_path}/logs"')
    launcher = launcher.replace('"/tmp/bobby-production-bot.pid"',
                                f'"{tmp_path}/bot.pid"')
    launcher = launcher.replace('"/tmp/bobby-shutdown-$BOBBY_RUN_ID"',
                                '"$ROOT/shutdown"')
    (tmp_path / "run_big_pool.fish").write_text(launcher)
    shutil.copy2(root / "src/utils/search_progress.py", tmp_path / "src/utils/search_progress.py")
    (tmp_path / ".venv/bin/python").symlink_to(sys.executable)
    for profile in ("remote", "tampa"):
        (tmp_path / f"config/search_config_{profile}.yaml").write_text(profile)
    (tmp_path / "config/app_config.py").write_text("EASY_APPLY_ONLY_MODE = True\n")
    (tmp_path / "main.py").write_text('''
import json, os, signal, time
from pathlib import Path
root = Path.cwd()
if os.environ.get("TEST_HOLD_UNTIL_SIGNAL"):
    def stop(_signum, _frame):
        (root / "main-signal.txt").write_text("SIGINT")
        raise SystemExit(0)
    signal.signal(signal.SIGINT, stop)
    (root / "main-ready.txt").write_text("ready")
    while True:
        time.sleep(0.05)
trace = root / "cycles.txt"
cycles = trace.read_text().splitlines() if trace.exists() else []
profile = (root / "config/search_config.yaml").read_text()
increments = json.loads(os.environ["TEST_NEW_JOB_INCREMENTS"])
if len(cycles) >= len(increments):
    raise SystemExit(9)
cycles.append(profile)
trace.write_text("\\n".join(cycles) + "\\n")
if os.environ.get("TEST_PROFILE_FAILURE") == profile:
    raise SystemExit(7)
state = Path(os.environ["BOBBY_RUN_STATE_FILE"])
state.write_text(json.dumps({"aggregate": {"new": sum(increments[:len(cycles)])}}))
''')
    for command in ("uv", "journalctl"):
        path = tmp_path / "bin" / command
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    sleeper = tmp_path / "bin/sleep"
    sleeper.write_text(f'''#!{sys.executable}
import os, signal, sys
from pathlib import Path
with Path("sleeps.txt").open("a") as trace:
    trace.write(sys.argv[1] + "\\n")
if os.environ.get("TEST_STOP_ON_SLEEP") == sys.argv[1]:
    os.kill(os.getppid(), signal.SIGINT)
''')
    sleeper.chmod(0o755)

    def run(increments, launcher_args=None, **overrides):
        env = {k: v for k, v in os.environ.items() if not k.startswith("BOBBY_")}
        env.update(PATH=f"{tmp_path}/bin:{env['PATH']}", BOBBY_LOCK_HELD="1",
                   BOBBY_RUN_ID="run-launcher-fixture", TEST_NEW_JOB_INCREMENTS=json.dumps(increments))
        env.update(overrides)
        command = ["fish", str(tmp_path / "run_big_pool.fish")]
        command.extend(launcher_args or [])
        result = subprocess.run(command,
                                cwd=tmp_path, env=env, text=True, capture_output=True, timeout=15)
        cycles_path = tmp_path / "cycles.txt"
        cycles = cycles_path.read_text().splitlines() if cycles_path.exists() else []
        sleeps_path = tmp_path / "sleeps.txt"
        sleeps = sleeps_path.read_text().splitlines() if sleeps_path.exists() else []
        return result, cycles, sleeps
    return run


def test_zero_new_round_runs_both_profiles_then_enters_bounded_backoff(launcher_sandbox):
    result, cycles, sleeps = launcher_sandbox(
        [0, 0], TEST_STOP_ON_SLEEP="1800"
    )
    assert result.returncode == 0, result.stderr
    assert cycles == ["remote", "tampa"]
    assert sleeps == ["15", "1800"]
    assert "SEARCH_BACKOFF_WAIT | seconds=1800" in result.stdout
    assert "state=WAITING" in result.stdout
    assert "SEARCH_NO_PROGRESS_COMPLETE" not in result.stdout


def test_productive_round_uses_short_pause_then_continues(launcher_sandbox):
    result, cycles, sleeps = launcher_sandbox(
        [1, 0, 0, 0], TEST_STOP_ON_SLEEP="1800"
    )
    assert result.returncode == 0, result.stderr
    assert cycles == ["remote", "tampa", "remote", "tampa"]
    assert sleeps == ["15", "300", "15", "1800"]
    assert "SEARCH_ROUND_COMPLETE | new_jobs=1" in result.stdout
    assert "SEARCH_PRODUCTIVE_WAIT | seconds=300" in result.stdout
    assert "SEARCH_BACKOFF_WAIT | seconds=1800" in result.stdout


def test_failed_profile_stops_instead_of_repeating_ineffective_round(launcher_sandbox):
    result, cycles, sleeps = launcher_sandbox([0, 0], TEST_PROFILE_FAILURE="remote")
    assert result.returncode == 7
    assert cycles == ["remote"]
    assert sleeps == []
    assert "SEARCH_PROFILE_FAILED" in result.stdout


def test_sigint_during_profile_pause_prevents_tampa_launch(launcher_sandbox):
    result, cycles, sleeps = launcher_sandbox([0, 0], TEST_STOP_ON_SLEEP="15")
    assert result.returncode == 0, result.stderr
    assert cycles == ["remote"]
    assert sleeps == ["15"]
    assert "Bobby graceful drain complete" in result.stdout


def test_timed_launcher_forwards_sigint_to_isolated_bot(launcher_sandbox, tmp_path):
    started_at = time.monotonic()
    result, cycles, _ = launcher_sandbox(
        [0],
        launcher_args=["--timeout-seconds", "2"],
        TEST_HOLD_UNTIL_SIGNAL="1",
    )
    elapsed = time.monotonic() - started_at

    assert result.returncode == 0, result.stderr
    assert cycles == []
    assert (tmp_path / "main-signal.txt").read_text() == "SIGINT"
    assert "Bobby graceful drain complete" in result.stdout
    assert elapsed < 6
