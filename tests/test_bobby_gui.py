import signal
from pathlib import Path
from unittest.mock import MagicMock

import bobby_gui
import bobby_gui_launcher
import pytest


SAMPLE_CONFIG = """# keep this comment
EASY_APPLY_ONLY_MODE = False  # owned inline comment
MONKEY_MODE = False
HEADLESS_MODE = False
TEST_MODE = False
COLLECT_INFO_MODE = False
LINKEDIN_RECOMMENDED_JOBS_MODE = False
LINKEDIN_TOP_APPLICANT_JOBS_MODE = False
RESTART_EVERY_DAY = False
FREE_TIER = True
MAX_APPLIES_NUM = 20
JOB_IS_INTERESTING_THRESH = 50
MINIMUM_WAIT_TIME_SEC = 10
FREE_TIER_RPM_LIMIT = 15
MINIMUM_LOG_LEVEL = "INFO"
UNRELATED_SETTING = {"preserve": "exactly"}
"""


def test_launch_commands_delegate_to_existing_runner():
    normal = bobby_gui.build_launch_command()
    smoke = bobby_gui.build_launch_command(smoke=True)

    assert normal[:4] == [
        "/usr/bin/systemd-inhibit",
        "--what=sleep:idle",
        "--why=LinkedIn Job Bot running",
        "/usr/bin/fish",
    ]
    assert normal[4] == str(bobby_gui.RUNNER)
    assert smoke == normal + ["--timeout-seconds", "300"]


def test_desktop_gui_launcher_keeps_system_gtk_and_bobby_dependencies_separate(tmp_path):
    site_packages = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)

    environment = bobby_gui_launcher.gui_environment(
        tmp_path, {"PYTHONPATH": "/existing/python/path", "DISPLAY": ":0"}
    )

    assert environment["PYTHONPATH"] == (
        f"{site_packages}{bobby_gui_launcher.os.pathsep}/existing/python/path"
    )
    assert environment["DISPLAY"] == ":0"


def test_owned_process_group_receives_sigint(monkeypatch):
    process = MagicMock()
    process.pid = 4321
    process.poll.return_value = None
    getpgid = MagicMock(return_value=9876)
    killpg = MagicMock()
    monkeypatch.setattr(bobby_gui.os, "getpgid", getpgid)
    monkeypatch.setattr(bobby_gui.os, "killpg", killpg)

    assert bobby_gui.request_graceful_stop(process) is True
    killpg.assert_called_once_with(9876, signal.SIGINT)


def test_mode_reader_reports_actual_boolean(tmp_path):
    config = tmp_path / "app_config.py"
    config.write_text("EASY_APPLY_ONLY_MODE = True\n", encoding="utf-8")

    assert bobby_gui.read_easy_apply_only_mode(config) == "True"


def test_existing_config_values_load_into_adapter(tmp_path):
    config = tmp_path / "app_config.py"
    config.write_text(SAMPLE_CONFIG, encoding="utf-8")

    values = bobby_gui.PythonConfigAdapter(config).load()

    assert values["EASY_APPLY_ONLY_MODE"] is False
    assert values["MONKEY_MODE"] is False
    assert values["MAX_APPLIES_NUM"] == 20


def test_easy_apply_toggle_updates_only_intended_setting(tmp_path):
    config = tmp_path / "app_config.py"
    config.write_text(SAMPLE_CONFIG, encoding="utf-8")

    values = bobby_gui.PythonConfigAdapter(config).save(
        {"EASY_APPLY_ONLY_MODE": True}
    )

    assert values["EASY_APPLY_ONLY_MODE"] is True
    assert values["MONKEY_MODE"] is False
    assert "EASY_APPLY_ONLY_MODE = True  # owned inline comment" in config.read_text(
        encoding="utf-8"
    )


def test_unrelated_config_content_is_preserved(tmp_path):
    config = tmp_path / "app_config.py"
    config.write_text(SAMPLE_CONFIG, encoding="utf-8")

    bobby_gui.PythonConfigAdapter(config).save({"MAX_APPLIES_NUM": 25})
    updated = config.read_text(encoding="utf-8")

    assert "# keep this comment" in updated
    assert 'UNRELATED_SETTING = {"preserve": "exactly"}' in updated


def test_invalid_value_leaves_original_config_intact(tmp_path):
    config = tmp_path / "app_config.py"
    config.write_text(SAMPLE_CONFIG, encoding="utf-8")
    original = config.read_bytes()

    with pytest.raises(bobby_gui.ConfigError, match="at most 100"):
        bobby_gui.PythonConfigAdapter(config).save(
            {"JOB_IS_INTERESTING_THRESH": 101}
        )

    assert config.read_bytes() == original


def test_running_bobby_is_not_duplicated(monkeypatch):
    popen = MagicMock()
    monkeypatch.setattr(bobby_gui, "bobby_is_running", lambda _process=None: True)
    monkeypatch.setattr(bobby_gui.subprocess, "Popen", popen)

    with pytest.raises(bobby_gui.BobbyAlreadyRunning):
        bobby_gui.launch_bobby()

    popen.assert_not_called()


def test_gui_config_save_never_edits_encountered_history(tmp_path):
    config = tmp_path / "app_config.py"
    config.write_text(SAMPLE_CONFIG, encoding="utf-8")
    encountered = tmp_path / "encountered_jobs.json"
    encountered.write_text('{"jobs": ["keep"]}\n', encoding="utf-8")
    original = encountered.read_bytes()

    bobby_gui.PythonConfigAdapter(config).save({"MONKEY_MODE": True})

    assert encountered.read_bytes() == original


def test_setting_change_during_run_requires_restart_message():
    assert "restart Bobby" in bobby_gui.settings_saved_message(running=True)
    assert "next Bobby start" in bobby_gui.settings_saved_message(running=False)


def test_monkey_mode_is_backed_by_real_setting():
    assert bobby_gui.SETTING_BY_KEY["MONKEY_MODE"].label == "Monkey Mode"


def test_external_ats_operator_settings_are_gui_backed():
    assert "EXTERNAL_ATS_ENABLED" in bobby_gui.SETTING_BY_KEY
    assert "EXTERNAL_ATS_MAX_RECOVERY_ATTEMPTS" in bobby_gui.SETTING_BY_KEY
    assert "EXTERNAL_ATS_RESUME_UPLOAD_ENABLED" in bobby_gui.SETTING_BY_KEY
    assert bobby_gui.SETTING_BY_KEY["EXTERNAL_ATS_ENABLED"].kind == "bool"


def test_structured_runtime_events_build_safe_counters():
    snapshot = {"run_id": "run-1", "counters": {"discovered": 4}}
    events = [
        {"run_id": "run-1", "type": "easy_apply_started", "payload": {}},
        {
            "run_id": "run-1",
            "type": "job_result",
            "payload": {"classification": "NEEDS_HUMAN"},
        },
    ]

    counters = bobby_gui.current_run_counters(snapshot, events)

    assert counters["found"] == 4
    assert counters["attempted"] == 1
    assert counters["easy_apply"] == 1
    assert counters["needs_human"] == 1


def test_quota_deferred_status_is_filterable_and_counted():
    rows = [
        {"job_key": "quota", "application_status": "DEFERRED_EASY_APPLY_LIMIT", "discovered_at": "2"},
        {"job_key": "skip", "application_status": "SKIPPED", "discovered_at": "1"},
    ]
    filtered = bobby_gui.filter_application_rows(
        rows, status="Easy Apply limit deferred", quality="All", sort_by="newest"
    )
    assert [row["job_key"] for row in filtered] == ["quota"]
    counters = bobby_gui.current_run_counters(
        {"run_id": "run-1", "counters": {}},
        [{"run_id": "run-1", "type": "job_result", "payload": {"classification": "DEFERRED_EASY_APPLY_LIMIT"}}],
    )
    assert counters["easy_apply_deferred"] == 1


def test_launcher_never_uses_shell_or_force_kill():
    source = Path(bobby_gui.__file__).read_text(encoding="utf-8")

    assert "shell=True" not in source
    assert "SIGKILL" not in source
    assert "kill -9" not in source


def test_gtk_labels_use_supported_line_wrap_api():
    source = Path(bobby_gui.__file__).read_text(encoding="utf-8")

    assert "line_wrap=" not in source
    assert "line-wrap=" not in source
    assert source.count(".set_line_wrap(True)") == 4


def test_desktop_entry_launches_repository_gui():
    desktop_path = Path.home() / ".local/share/applications/bobby.desktop"
    if not desktop_path.is_file():
        pytest.skip("desktop entry is an optional local installation artifact")
    desktop = desktop_path.read_text(encoding="utf-8")
    expected_exec = f"Exec=/usr/bin/python3 {bobby_gui.REPOSITORY / 'bobby_gui_launcher.py'}"
    if expected_exec not in desktop:
        pytest.skip("desktop entry belongs to another checkout")

    assert "Name=Bobby" in desktop
    assert expected_exec in desktop
    assert "Terminal=false" in desktop


def _application_row(key, status, quality, *, employer="NO_RESPONSE", lifecycle="ACTIVE", date="2026-09-01T00:00:00+00:00"):
    return {
        "job_key": key,
        "job_title": key,
        "company_name": key,
        "application_status": status,
        "quality_tier": quality,
        "employer_response": employer,
        "lifecycle_state": lifecycle,
        "discovered_at": date,
        "attempted_at": date if status in {"SUBMITTED", "UNVERIFIED_AFTER_SUBMIT"} else None,
        "suitability_score": {"NEUTRAL": 20, "RED": 55, "YELLOW": 60, "GREEN": 75, "CYAN": 91, "DIAMOND": 91}[quality],
    }


def test_application_filters_and_sorting_are_deterministic():
    rows = [
        _application_row("old", "SKIPPED", "RED"),
        _application_row("cyan", "SUBMITTED", "CYAN", date="2026-09-03T00:00:00+00:00"),
        _application_row("green", "TECHNICAL_FAILURE", "GREEN", date="2026-09-02T00:00:00+00:00"),
    ]
    assert [row["job_key"] for row in bobby_gui.filter_application_rows(rows, quality="Cyan")] == ["cyan"]
    assert [row["job_key"] for row in bobby_gui.filter_application_rows(rows, status="Submitted")] == ["cyan"]
    assert [row["job_key"] for row in bobby_gui.filter_application_rows(rows, sort_by="oldest")] == ["old", "green", "cyan"]
    assert [row["job_key"] for row in bobby_gui.filter_application_rows(rows, sort_by="suitability")] == ["cyan", "green", "old"]


def test_application_rows_preserve_quality_when_rejected_or_stale():
    rows = [
        _application_row("rejected", "SUBMITTED", "CYAN", employer="REJECTED", lifecycle="REJECTED_GREY"),
        _application_row("stale", "SUBMITTED", "GREEN", lifecycle="STALE_NO_RESPONSE"),
    ]
    rejected = bobby_gui.filter_application_rows(rows, status="Rejected")
    assert rejected[0]["quality_tier"] == "CYAN"
    assert [row["job_key"] for row in bobby_gui.filter_application_rows(rows, status="Awaiting response")] == []


def test_application_filters_cover_audit_fields_with_normal_values():
    rows = [
        {
            **_application_row("external", "SUBMITTED", "CYAN"),
            "application_type": "EXTERNAL_ATS",
            "ats_family": "workday",
            "account_required": True,
            "account_created": True,
            "email_verification_state": "VERIFIED",
            "search_profile": "remote",
            "last_status_at": "2026-09-09T10:00:00+00:00",
        },
        {
            **_application_row("easy", "SUBMITTED", "GREEN"),
            "application_type": "EASY_APPLY",
            "ats_family": "linkedin",
            "email_verification_state": "NOT_REQUIRED",
            "search_profile": "tampa",
            "last_status_at": "2026-08-01T10:00:00+00:00",
        },
    ]
    filtered = bobby_gui.filter_application_rows(
        rows,
        search="external",
        application_type="External ATS",
        ats="workday",
        account="Created",
        verification="Verified",
        profile="Remote USA",
        date_range="Today",
        now="2026-09-09T12:00:00+00:00",
    )
    assert [row["job_key"] for row in filtered] == ["external"]
