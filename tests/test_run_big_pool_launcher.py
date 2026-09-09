from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "run_big_pool.fish"


def test_timed_mode_places_timeout_inside_systemd_inhibit_launch():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--timeout-seconds" in text
    assert "exec env BOBBY_TIMEOUT_ACTIVE=1 timeout" in text
    assert "--foreground" not in text
    assert "--signal=INT" in text
    assert "--kill-after=180s" in text
    assert "--on-signal INT" in text
    assert "set -g BOBBY_LAUNCHER_SHUTDOWN 1" in text
    assert 'set -g BOT_PID_FILE "/tmp/bobby-production-bot.pid"' in text
    assert 'command kill -INT "$bot_pid"' in text
    assert 'command kill -TERM "$bot_pid"' in text
    assert 'set -gx BOBBY_SHUTDOWN_FILE "/tmp/bobby-shutdown-$BOBBY_RUN_ID"' in text
    assert text.count('touch "$BOBBY_SHUTDOWN_FILE"') == 2
    assert 'setsid .venv/bin/python main.py &' in text
    assert 'trap "forward_signal INT" INT' in text
    assert 'kill -KILL -- "-$bot_pid"' in text
    assert ') </dev/null >/dev/null 2>&1 &' in text
    assert 'pkill -TERM -P "$watchdog_pid"' in text
    assert 'wait "$bot_pid"' in text
    assert 'bot_status=$?' in text
    assert "bash -c '" in text
    assert text.count("exit_if_launcher_shutdown") >= 5
    assert "Bobby graceful drain complete; launcher exiting." in text
    assert "flock -n -E 73 --no-fork" in text
    assert 'end 2>&1 | tee -i "$logfile"' in text
    assert 'end 2>&1 | tee -i -a "$logfile"' in text
    assert "function read_easy_apply_mode" in text
    assert "EASY_APPLY_MODE_START" in text
    assert "EASY_APPLY_MODE_CHANGED" in text
    assert text.count("set -l tampa_easy_apply_mode") == 1


def test_latest_log_alias_is_live_symlink_created_before_bot_output():
    text = SCRIPT.read_text(encoding="utf-8")
    touch_at = text.index('touch "$logfile"')
    link_at = text.index('ln -sfn "$logfile" "$LOG_DIR/latest-$profile.log"')
    bot_at = text.index("BOT OUTPUT STARTS HERE")
    assert touch_at < link_at < bot_at
    assert 'cp "$logfile" "$LOG_DIR/latest-$profile.log"' not in text
