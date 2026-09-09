#!/usr/bin/env fish

# =============================================================================
# LinkedIn Job Bot — Remote + Tampa automatic runner
# Detailed per-cycle logging
# =============================================================================

set -g ROOT (path resolve (path dirname (status filename)))
set -g LOG_DIR "$HOME/Logs"
set -g LOCK_FILE "/tmp/bobby-production.lock"
set -g BOT_PID_FILE "/tmp/bobby-production-bot.pid"

# For bounded production/smoke runs, keep `timeout` *inside*
# `systemd-inhibit`. Bobby is started in its own session below so the timeout
# signal reaches the launch supervisors, which forward it only to Bobby:
#   systemd-inhibit ... fish ./run_big_pool.fish --timeout-seconds 300
if not set -q BOBBY_TIMEOUT_ACTIVE
    if test (count $argv) -ge 1; and test "$argv[1]" = "--timeout-seconds"
        if test (count $argv) -lt 2; or not string match -qr '^[1-9][0-9]*$' -- "$argv[2]"
            echo "ERROR: --timeout-seconds requires a positive integer"
            exit 2
        end
        set -l timeout_seconds "$argv[2]"
        set -l remaining_args $argv[3..-1]
        exec env BOBBY_TIMEOUT_ACTIVE=1 timeout \
            --preserve-status \
            --signal=INT \
            --kill-after=180s \
            "$timeout_seconds" \
            fish (status filename) $remaining_args
    end
end

# The timed wrapper signals the launcher process group. Bobby and Playwright
# are in a separate session; these handlers and the Bash wait supervisor
# forward the request only to Bobby and preserve its cleanup window.
function _bobby_launcher_signal_notice --on-signal INT
    set -g BOBBY_LAUNCHER_SHUTDOWN 1
    if set -q BOBBY_SHUTDOWN_FILE
        touch "$BOBBY_SHUTDOWN_FILE"
    end
    echo "Bobby launcher received SIGINT; waiting for graceful drain..."
    if test -f "$BOT_PID_FILE"
        set -l bot_pid (string trim < "$BOT_PID_FILE")
        if string match -qr '^[1-9][0-9]*$' -- "$bot_pid"
            command kill -INT "$bot_pid" 2>/dev/null
        end
    end
end

function _bobby_launcher_term_notice --on-signal TERM
    set -g BOBBY_LAUNCHER_SHUTDOWN 1
    if set -q BOBBY_SHUTDOWN_FILE
        touch "$BOBBY_SHUTDOWN_FILE"
    end
    echo "Bobby launcher received SIGTERM; waiting for graceful drain..."
    if test -f "$BOT_PID_FILE"
        set -l bot_pid (string trim < "$BOT_PID_FILE")
        if string match -qr '^[1-9][0-9]*$' -- "$bot_pid"
            command kill -TERM "$bot_pid" 2>/dev/null
        end
    end
end

function _bobby_return_status
    return "$argv[1]"
end

function exit_if_launcher_shutdown
    if set -q BOBBY_LAUNCHER_SHUTDOWN
        echo "Bobby graceful drain complete; launcher exiting."
        exit 0
    end
end

# `flock` owns the descriptor while this launcher and all descendants run.
# A stale lock-file is harmless because ownership is tied to the live fd.
if not set -q BOBBY_LOCK_HELD
    flock -n -E 73 --no-fork "$LOCK_FILE" env BOBBY_LOCK_HELD=1 fish (status filename) $argv
    set -l lock_status $status
    if test $lock_status -eq 73
        echo "BOBBY_ALREADY_RUNNING"
    end
    exit $lock_status
end

if not set -q BOBBY_RUN_ID
    set -gx BOBBY_RUN_ID "run-"(python3 -c 'import uuid; print(uuid.uuid4())')
end
set -gx BOBBY_SHUTDOWN_FILE "/tmp/bobby-shutdown-$BOBBY_RUN_ID"
set -gx BOBBY_RUN_STATE_FILE "$LOG_DIR/.bobby-$BOBBY_RUN_ID-stats.json"
command rm -f "$BOBBY_SHUTDOWN_FILE"

# New run logs and derived runtime files are owner-only by default.
umask 077

mkdir -p "$LOG_DIR"

cd "$ROOT"
or begin
    echo "ERROR: Could not enter $ROOT"
    exit 1
end


function run_cycle
    set -l profile "$argv[1]"
    set -l config_file "$argv[2]"

    set -l timestamp (date "+%Y-%m-%d_%H-%M-%S")
    set -l logfile "$LOG_DIR/$timestamp-$profile.log"

    # Create the target and atomically replace the alias before any run output.
    # A symlink exposes live output instead of a stale copy from the prior run.
    touch "$logfile"
    or begin
        echo "ERROR: Could not create log file: $logfile"
        return 1
    end
    ln -sfn "$logfile" "$LOG_DIR/latest-$profile.log"
    or begin
        echo "ERROR: Could not update latest-$profile.log"
        return 1
    end

    echo
    echo "============================================================"
    echo "PREPARING $profile RUN"
    echo "Log: $logfile"
    echo "============================================================"

    if not test -f "$config_file"
        echo "ERROR: config file does not exist: $config_file" | tee -i "$logfile"
        return 1
    end

    cp "$config_file" config/search_config.yaml

    if test $status -ne 0
        echo "ERROR: Failed to activate $config_file" | tee -i "$logfile"
        return 1
    end


    # -------------------------------------------------------------------------
    # Everything inside this block is written both to terminal and log file.
    # -------------------------------------------------------------------------

    begin

        echo "================================================================"
        echo "LINKEDIN JOB BOT — CYCLE START"
        echo "================================================================"
        echo

        echo "Profile: $profile"
        echo "Started: "(date "+%Y-%m-%d %H:%M:%S %Z")
        echo "Working directory: $ROOT"
        echo "Log file: $logfile"
        echo "Run ID: $BOBBY_RUN_ID"

        echo
        echo "----------------------------------------------------------------"
        echo "SYSTEM"
        echo "----------------------------------------------------------------"

        uname -a

        echo
        echo "Python:"
        uv run python --version

        echo
        echo "uv:"
        uv --version

        echo
        echo "Memory before run:"
        free -h

        echo
        echo "Disk:"
        df -h "$ROOT"

        echo
        echo "----------------------------------------------------------------"
        echo "GIT"
        echo "----------------------------------------------------------------"

        echo -n "Commit: "
        git rev-parse --short HEAD 2>/dev/null
        or echo "unknown"

        echo
        echo "Modified files:"
        git status --short 2>/dev/null
        or true

        echo
        echo "Diff summary:"
        git diff --stat 2>/dev/null
        or true


        echo
        echo "----------------------------------------------------------------"
        echo "IMPORTANT APP CONFIG"
        echo "----------------------------------------------------------------"

        grep -nE \
        "^(JOB_SITE|HEADLESS_MODE|MONKEY_MODE|TEST_MODE|COLLECT_INFO_MODE|EASY_APPLY_ONLY_MODE|RESTART_EVERY_DAY|LLM_MODEL_TYPE|EASY_APPLY_MODEL|APPLY_AGENT_MODEL|JOB_IS_INTERESTING_THRESH|MAX_APPLIES_NUM)[[:space:]]*=" \
        config/app_config.py
        or true


        echo
        echo "----------------------------------------------------------------"
        echo "ACTIVE SEARCH CONFIG"
        echo "----------------------------------------------------------------"

        cat config/search_config.yaml


        echo
        echo "----------------------------------------------------------------"
        echo "APPLICATION PROFILE STATUS"
        echo "----------------------------------------------------------------"

        if test -f candidate_profile.yaml
            echo "candidate_profile.yaml present (contents intentionally omitted from logs)"
        else
            echo "WARNING: candidate_profile.yaml NOT FOUND"
        end


        echo
        echo "----------------------------------------------------------------"
        echo "EXTERNAL APPLY PATCH STATUS"
        echo "----------------------------------------------------------------"

        grep -n \
        "Starting isolated external application worker" \
        src/llm/apply_agent.py
        or echo "WARNING: isolated external worker marker not found"

        grep -n \
        "EXTERNAL APPLICATION CONFIRMED" \
        src/llm/apply_agent.py
        or echo "WARNING: strict submission verification marker not found"

        grep -n \
        "verify_submission" \
        src/llm/apply_agent.py | head -10
        or true


        echo
        echo "================================================================"
        echo "BOT OUTPUT STARTS HERE"
        echo "================================================================"
        echo

        # PYTHONUNBUFFERED makes sure logs are written immediately rather
        # than being held in Python output buffers.
        # Fish wait reports whether waiting succeeded, not the child's exit
        # code. Bash wait preserves that status so a failed profile cannot be
        # reported as a successful, empty search round. Keep the actual Python
        # PID available to the existing graceful-shutdown signal handlers.
        env PYTHONUNBUFFERED=1 BOBBY_RUN_ID="$BOBBY_RUN_ID" bash -c '
            bot_pid=""
            watchdog_pid=""

            forward_signal() {
                signal_name="$1"
                if [[ "$bot_pid" =~ ^[1-9][0-9]*$ ]]; then
                    kill -"$signal_name" "$bot_pid" 2>/dev/null || true
                fi
                if [[ -z "$watchdog_pid" ]]; then
                    (
                        sleep 150
                        if kill -0 "$bot_pid" 2>/dev/null; then
                            while read -r worker_pid; do
                                [[ "$worker_pid" =~ ^[1-9][0-9]*$ ]] && \
                                    kill -KILL -- "-$worker_pid" 2>/dev/null || true
                            done < <(pgrep -P "$bot_pid" -f external_apply_worker.py || true)
                            kill -KILL -- "-$bot_pid" 2>/dev/null || true
                        fi
                    ) </dev/null >/dev/null 2>&1 &
                    watchdog_pid=$!
                fi
            }

            trap "forward_signal INT" INT
            trap "forward_signal TERM" TERM
            setsid .venv/bin/python main.py &
            bot_pid=$!
            echo "$bot_pid" > "$1"
            while true; do
                wait "$bot_pid"
                bot_status=$?
                if ! kill -0 "$bot_pid" 2>/dev/null; then
                    break
                fi
            done
            rm -f "$1"
            if [[ "$watchdog_pid" =~ ^[1-9][0-9]*$ ]]; then
                pkill -TERM -P "$watchdog_pid" 2>/dev/null || true
                kill "$watchdog_pid" 2>/dev/null || true
                wait "$watchdog_pid" 2>/dev/null || true
            fi
            exit "$bot_status"
        ' bobby-wait "$BOT_PID_FILE"

    # Keep the log consumer alive while Python handles the forwarded SIGINT,
    # closes its browser, and emits its final report.
    end 2>&1 | tee -i "$logfile"

    # IMPORTANT: capture the status of the first side of the pipe,
    # not tee itself.
    set -l pipeline_status $pipestatus
    set -l bot_status $pipeline_status[1]


    # -------------------------------------------------------------------------
    # Post-run diagnostic information
    # -------------------------------------------------------------------------

    begin

        echo
        echo "================================================================"
        echo "CYCLE FINISHED"
        echo "================================================================"

        echo "Profile: $profile"
        echo "Finished: "(date "+%Y-%m-%d %H:%M:%S %Z")
        echo "Process exit status: $bot_status"

        echo
        echo "Memory after run:"
        free -h

        echo
        echo "----------------------------------------------------------------"
        echo "RECENT KERNEL OOM / PROCESS KILL EVENTS"
        echo "----------------------------------------------------------------"

        journalctl -k -b --no-pager 2>/dev/null | \
            grep -Ei \
            "oom|out of memory|killed process|chromium|chrome|playwright|node" | \
            tail -50
        or true

        echo
        echo "----------------------------------------------------------------"
        echo "LATEST APPLICATION OUTPUT COUNTS"
        echo "----------------------------------------------------------------"

        if test -f data/output/linkedin/success.yaml
            echo "success.yaml:"
            wc -l data/output/linkedin/success.yaml
        end

        if test -f data/output/linkedin/skipped.yaml
            echo "skipped.yaml:"
            wc -l data/output/linkedin/skipped.yaml
        end

        if test -f data/output/linkedin/failed.yaml
            echo "failed.yaml:"
            wc -l data/output/linkedin/failed.yaml
        end

        echo
        echo "================================================================"
        echo "END OF LOG"
        echo "================================================================"

    end 2>&1 | tee -i -a "$logfile"


    echo
    echo "Saved detailed log:"
    echo "$logfile"
    echo

    return $bot_status
end



# =============================================================================
# MAIN LOOP
# =============================================================================

function progress_notice
    echo (date "+%Y-%m-%d %H:%M:%S %Z") "| $argv" | tee -i -a "$LOG_DIR/$BOBBY_RUN_ID-launcher.log"
end

function read_easy_apply_mode
    set -l mode_line (grep -E '^EASY_APPLY_ONLY_MODE[[:space:]]*=[[:space:]]*(True|False)([[:space:]]*#.*)?$' config/app_config.py)
    if test (count $mode_line) -ne 1
        return 1
    end
    string replace -r '^.*=[[:space:]]*(True|False).*$' '$1' -- $mode_line
end

set -g BOBBY_EASY_APPLY_MODE (read_easy_apply_mode)
if test $status -ne 0
    progress_notice "EASY_APPLY_MODE_UNAVAILABLE | state=STOPPED"
    exit 1
end
progress_notice "EASY_APPLY_MODE_START | value=$BOBBY_EASY_APPLY_MODE"

while true
    set -l current_easy_apply_mode (read_easy_apply_mode)
    if test $status -ne 0; or test "$current_easy_apply_mode" != "$BOBBY_EASY_APPLY_MODE"
        progress_notice "EASY_APPLY_MODE_CHANGED | state=STOPPED | expected=$BOBBY_EASY_APPLY_MODE | actual="(string join ',' $current_easy_apply_mode)
        exit 1
    end

    set -l round_start_new (.venv/bin/python -m src.utils.search_progress "$BOBBY_RUN_STATE_FILE" --allow-missing)
    if test $status -ne 0
        progress_notice "SEARCH_PROGRESS_UNAVAILABLE | state=STOPPED"
        exit 1
    end
    progress_notice "SEARCH_ROUND_START | new_jobs_total=$round_start_new"

    echo
    echo "############################################################"
    echo "REMOTE USA CYCLE"
    echo "############################################################"

    run_cycle \
        "remote" \
        "config/search_config_remote.yaml"

    set -l remote_status $status
    exit_if_launcher_shutdown
    if test $remote_status -ne 0
        progress_notice "SEARCH_PROFILE_FAILED | profile=remote | state=STOPPED"
        exit $remote_status
    end


    echo
    echo "Waiting 15 seconds before Tampa cycle..."
    echo

    progress_notice "PROFILE_TRANSITION_PAUSE | seconds=15 | next=tampa"
    sleep 15

    exit_if_launcher_shutdown

    set -l tampa_easy_apply_mode (read_easy_apply_mode)
    if test $status -ne 0; or test "$tampa_easy_apply_mode" != "$BOBBY_EASY_APPLY_MODE"
        progress_notice "EASY_APPLY_MODE_CHANGED | state=STOPPED | expected=$BOBBY_EASY_APPLY_MODE | actual="(string join ',' $tampa_easy_apply_mode)
        exit 1
    end


    echo
    echo "############################################################"
    echo "TAMPA BAY CYCLE"
    echo "############################################################"

    run_cycle \
        "tampa" \
        "config/search_config_tampa.yaml"

    set -l tampa_status $status
    exit_if_launcher_shutdown
    if test $tampa_status -ne 0
        progress_notice "SEARCH_PROFILE_FAILED | profile=tampa | state=STOPPED"
        exit $tampa_status
    end

    set -l round_end_new (.venv/bin/python -m src.utils.search_progress "$BOBBY_RUN_STATE_FILE")
    if test $status -ne 0; or test "$round_end_new" -lt "$round_start_new"
        progress_notice "SEARCH_PROGRESS_UNAVAILABLE | state=STOPPED"
        exit 1
    end
    set -l round_new (math "$round_end_new - $round_start_new")
    progress_notice "SEARCH_ROUND_COMPLETE | new_jobs=$round_new | new_jobs_total=$round_end_new"
    if test "$round_new" -eq 0
        set -l round_wait_seconds 1800
        progress_notice "SEARCH_BACKOFF_WAIT | seconds=$round_wait_seconds | reason=no_new_jobs | state=WAITING | next=remote"
        sleep "$round_wait_seconds"
    else
        set -l round_wait_seconds 300
        progress_notice "SEARCH_PRODUCTIVE_WAIT | seconds=$round_wait_seconds | reason=new_jobs_found | state=WAITING | next=remote"
        sleep "$round_wait_seconds"
    end

    exit_if_launcher_shutdown

    set -l next_round_easy_apply_mode (read_easy_apply_mode)
    if test $status -ne 0; or test "$next_round_easy_apply_mode" != "$BOBBY_EASY_APPLY_MODE"
        progress_notice "EASY_APPLY_MODE_CHANGED | state=STOPPED | expected=$BOBBY_EASY_APPLY_MODE | actual="(string join ',' $next_round_easy_apply_mode)
        exit 1
    end

end
