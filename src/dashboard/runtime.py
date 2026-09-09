import json
import os
import re
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from shutil import copyfile
from typing import Any, Dict, List, Tuple

from src.utils.redaction import sanitize_value
from src.utils.run_context import get_run_id
from src.application_tracker import ApplicationTracker

try:
    from config.app_config import DASHBOARD_OUTPUT_APP_LOGS
except ImportError:
    DASHBOARD_OUTPUT_APP_LOGS = False
from config.constants import LOG_DIR

ROOT_DIR = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = ROOT_DIR / "data" / "output" / "dashboard"
SCREENSHOT_DIR = DASHBOARD_DIR / "screenshots"
EVENTS_FILE = DASHBOARD_DIR / "events.jsonl"
BOBBY_EVENTS_FILE = Path(os.getenv("BOBBY_LOG_DIR", str(Path.home() / "Logs"))) / "bobby-events.jsonl"
SNAPSHOT_FILE = DASHBOARD_DIR / "snapshot.json"
CONTROL_FILE = DASHBOARD_DIR / "control.json"
PROCESS_FILE = DASHBOARD_DIR / "process.json"
SCREENSHOT_INDEX_FILE = DASHBOARD_DIR / "screenshots.json"
LATEST_SCREENSHOT_FILE = SCREENSHOT_DIR / "latest.png"
BOT_STDOUT_FILE = ROOT_DIR / LOG_DIR / "dashboard_bot_stdout.log"
# BOT_STDOUT_FILE = ROOT_DIR / LOG_DIR / "app.log"

_FILE_LOCK = threading.Lock()
_APPLICATION_TRACKER = ApplicationTracker()

def _sanitize_event_value(value: Any, key: str = "") -> Any:
    return sanitize_value(value, key)


class StopRequested(Exception):
    """Raised when the dashboard asked the bot to stop gracefully."""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _default_control_state() -> Dict[str, bool]:
    return {"pause_requested": False, "stop_requested": False}


def _default_snapshot() -> Dict[str, Any]:
    return {
        "run_id": None,
        "run_status": "idle",
        "paused": False,
        "stop_requested": False,
        "started_at": None,
        "finished_at": None,
        "last_event_at": None,
        "page_num": 0,
        "current_job": None,
        "latest_screenshot_at": None,
        "latest_screenshot_path": None,
        "counters": {
            "discovered": 0,
            "evaluated": 0,
            "interesting": 0,
            "applied": 0,
            "skipped": 0,
            "failed": 0,
        },
    }


def ensure_dashboard_artifacts() -> None:
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    BOT_STDOUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not SNAPSHOT_FILE.exists():
        _write_json(SNAPSHOT_FILE, _default_snapshot())
    if not CONTROL_FILE.exists():
        _write_json(CONTROL_FILE, _default_control_state())
    if not PROCESS_FILE.exists():
        _write_json(PROCESS_FILE, {})
    if not SCREENSHOT_INDEX_FILE.exists():
        _write_json(SCREENSHOT_INDEX_FILE, [])
    if not EVENTS_FILE.exists():
        EVENTS_FILE.write_text("", encoding="utf-8")


def _read_json(path: Path, default: Dict[str, Any] | List[Any] | None = None) -> Any:
    if not path.exists():
        return {} if default is None else default
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        return {} if default is None else default


def _write_json(path: Path, data: Dict[str, Any] | List[Any]) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def get_screenshot_history(run_id: str | None = None) -> List[Dict[str, Any]]:
    ensure_dashboard_artifacts()
    entries = _read_json(SCREENSHOT_INDEX_FILE, [])
    if not isinstance(entries, list):
        return []
    if run_id is None:
        return entries
    return [entry for entry in entries if entry.get("run_id") == run_id]


def _append_screenshot_history(entry: Dict[str, Any]) -> None:
    with _FILE_LOCK:
        entries = get_screenshot_history()
        entries.append(entry)
        _write_json(SCREENSHOT_INDEX_FILE, entries)


def get_snapshot() -> Dict[str, Any]:
    ensure_dashboard_artifacts()
    snapshot = _read_json(SNAPSHOT_FILE, _default_snapshot())
    default = _default_snapshot()
    default.update(snapshot)
    default["counters"].update(snapshot.get("counters", {}))
    return default


def update_snapshot(**updates: Any) -> Dict[str, Any]:
    ensure_dashboard_artifacts()
    with _FILE_LOCK:
        snapshot = get_snapshot()
        for key, value in updates.items():
            if key == "counters" and isinstance(value, dict):
                snapshot["counters"].update(value)
            else:
                snapshot[key] = value
        _write_json(SNAPSHOT_FILE, snapshot)
    return snapshot


def get_control_state() -> Dict[str, bool]:
    ensure_dashboard_artifacts()
    control = _read_json(CONTROL_FILE, _default_control_state())
    default = _default_control_state()
    default.update(control)
    return default


def update_control_state(**updates: bool) -> Dict[str, bool]:
    ensure_dashboard_artifacts()
    with _FILE_LOCK:
        control = get_control_state()
        control.update(updates)
        _write_json(CONTROL_FILE, control)
    return control


def get_process_info() -> Dict[str, Any]:
    ensure_dashboard_artifacts()
    return _read_json(PROCESS_FILE, {})


def set_process_info(data: Dict[str, Any]) -> None:
    ensure_dashboard_artifacts()
    with _FILE_LOCK:
        _write_json(PROCESS_FILE, data)


def clear_process_info() -> None:
    set_process_info({})


def reset_runtime_state(run_id: str | None = None) -> Dict[str, Any]:
    ensure_dashboard_artifacts()
    snapshot = _default_snapshot()
    snapshot["run_id"] = run_id
    snapshot["run_status"] = "starting" if run_id else "idle"
    snapshot["started_at"] = _now_iso() if run_id else None
    with _FILE_LOCK:
        _write_json(SNAPSHOT_FILE, snapshot)
        _write_json(CONTROL_FILE, _default_control_state())
    return snapshot


def emit_event(event_type: str, message: str | None = None, **payload: Any) -> Dict[str, Any]:
    ensure_dashboard_artifacts()
    snapshot = get_snapshot()
    event = {
        "timestamp": _now_iso(),
        "run_id": payload.pop("run_id", None)
        or os.getenv("DASHBOARD_RUN_ID")
        or os.getenv("BOBBY_RUN_ID")
        or snapshot.get("run_id")
        or get_run_id(),
        "type": event_type,
        "message": message or event_type.replace("_", " ").title(),
        "payload": payload,
    }
    with _FILE_LOCK:
        with EVENTS_FILE.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=True) + "\n")
        try:
            BOBBY_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with BOBBY_EVENTS_FILE.open("a", encoding="utf-8") as mirror:
                mirror.write(
                    json.dumps(_sanitize_event_value(event), ensure_ascii=True) + "\n"
                )
        except OSError as exc:
            # The dashboard-local event stream remains authoritative; a mirror
            # permission problem must never stop Bobby or distort counters.
            pass
    # Keep a structured application repository in sync at event emission time.
    # A tracker failure is deliberately non-fatal: dashboard telemetry and the
    # application worker must remain usable even if the optional JSON store is
    # temporarily unavailable.
    try:
        _APPLICATION_TRACKER.apply_event(event)
    except Exception:
        pass
    try:
        from src.utils.failure_ledger import record_failure_event

        record_failure_event(event)
    except Exception:
        # Failure bookkeeping must never turn a terminal application result
        # into a second application error.  The event itself remains durable.
        pass
    _update_snapshot_from_event(event)
    return event


def _update_snapshot_from_event(event: Dict[str, Any]) -> None:
    payload = event.get("payload", {})
    snapshot = get_snapshot()
    counters = dict(snapshot.get("counters", {}))

    snapshot["last_event_at"] = event["timestamp"]
    if event.get("run_id"):
        snapshot["run_id"] = event["run_id"]

    event_type = event["type"]
    if event_type == "run_started":
        snapshot["run_status"] = "running"
        snapshot["started_at"] = event["timestamp"]
        snapshot["finished_at"] = None
        snapshot["current_job"] = None
        snapshot["paused"] = False
        snapshot["stop_requested"] = False
        counters = _default_snapshot()["counters"]
    elif event_type in {"run_completed", "run_stopped", "run_failed"}:
        snapshot["run_status"] = (
            "completed"
            if event_type == "run_completed"
            else "stopped" if event_type == "run_stopped" else "failed"
        )
        snapshot["finished_at"] = event["timestamp"]
        snapshot["current_job"] = None
        snapshot["paused"] = False
    elif event_type == "pause_state_changed":
        snapshot["paused"] = bool(payload.get("paused"))
    elif event_type == "stop_requested":
        snapshot["stop_requested"] = True
    elif event_type == "page_changed":
        snapshot["page_num"] = int(payload.get("page_num", 0))
    elif event_type == "jobs_discovered":
        count = int(payload.get("count", 0))
        counters["discovered"] = int(
            payload.get("total_discovered", counters.get("discovered", 0) + count)
        )
        snapshot["page_num"] = int(payload.get("page_num", snapshot.get("page_num", 0)))
    elif event_type in {
        "job_loaded",
        "job_application_started",
        "job_evaluation_started",
        "job_progress",
        "easy_apply_started",
        "easy_apply_completed",
        "agent_apply_started",
        "agent_apply_completed",
    }:
        snapshot["current_job"] = {
            "title": payload.get("job_title"),
            "company": payload.get("company_name"),
            "url": payload.get("url"),
            "stage": payload.get("stage", event_type),
            "message": event.get("message"),
        }
    elif event_type == "llm_call_completed":
        current_job = snapshot.get("current_job") or {}
        if payload.get("url") and payload.get("url") == current_job.get("url"):
            current_job["llm_time_seconds"] = payload.get("total_job_llm_time_seconds", 0.0)
            snapshot["current_job"] = current_job
    elif event_type == "job_evaluated":
        counters["evaluated"] = counters.get("evaluated", 0) + 1
        if payload.get("interesting"):
            counters["interesting"] = counters.get("interesting", 0) + 1
        snapshot["current_job"] = {
            "title": payload.get("job_title"),
            "company": payload.get("company_name"),
            "url": payload.get("url"),
            "stage": "evaluated",
            "interesting": payload.get("interesting"),
            "score": payload.get("score"),
            "llm_time_seconds": payload.get("llm_time_seconds", 0.0),
            "message": event.get("message"),
        }
    elif event_type == "job_result":
        result = str(payload.get("result", "")).lower()
        if result == "success":
            counters["applied"] = counters.get("applied", 0) + 1
        elif result == "skip":
            counters["skipped"] = counters.get("skipped", 0) + 1
        elif result in {"error", "failed"}:
            counters["failed"] = counters.get("failed", 0) + 1
        snapshot["current_job"] = None
    elif event_type == "screenshot_updated":
        snapshot["latest_screenshot_at"] = event["timestamp"]
        snapshot["latest_screenshot_path"] = payload.get("path")

    snapshot["counters"] = counters
    with _FILE_LOCK:
        _write_json(SNAPSHOT_FILE, snapshot)


def read_events(limit: int = 200) -> List[Dict[str, Any]]:
    ensure_dashboard_artifacts()
    try:
        lines = EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: List[Dict[str, Any]] = []
    for line in lines[-limit:]:
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def read_events_for_run(run_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    return [event for event in read_events(limit=5000) if event.get("run_id") == run_id][-limit:]


def read_events_since(position: int) -> Tuple[List[Dict[str, Any]], int]:
    ensure_dashboard_artifacts()
    if not EVENTS_FILE.exists():
        return [], position

    with EVENTS_FILE.open("r", encoding="utf-8") as file:
        file.seek(position)
        lines = file.readlines()
        position = file.tell()

    events: List[Dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events, position


def read_events_since_for_run(position: int, run_id: str) -> Tuple[List[Dict[str, Any]], int]:
    events, position = read_events_since(position)
    return [event for event in events if event.get("run_id") == run_id], position


def latest_event_position() -> int:
    ensure_dashboard_artifacts()
    try:
        return EVENTS_FILE.stat().st_size
    except OSError:
        return 0


def is_process_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _tee_process_output(process: subprocess.Popen, log_path: Path) -> None:
    def _reader() -> None:
        with log_path.open("a", encoding="utf-8") as log:
            assert process.stdout is not None
            for raw_line in iter(process.stdout.readline, b""):
                line = raw_line.decode("utf-8", errors="replace")
                if DASHBOARD_OUTPUT_APP_LOGS:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                log.write(line)

    threading.Thread(target=_reader, daemon=True).start()


def start_bot_process() -> Dict[str, Any]:
    process_info = get_process_info()
    if is_process_running(process_info.get("pid")):
        return process_info

    run_id = datetime.now().strftime("run-%Y%m%d-%H%M%S")
    reset_runtime_state(run_id=run_id)
    emit_event("dashboard_start_requested", "Dashboard requested a bot run", run_id=run_id)

    env = os.environ.copy()
    env["DASHBOARD_RUN_ID"] = run_id

    process = subprocess.Popen(
        ["uv", "run", "python", "main.py"],
        cwd=ROOT_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _tee_process_output(process, BOT_STDOUT_FILE)

    process_info = {"pid": process.pid, "run_id": run_id, "started_at": _now_iso()}
    set_process_info(process_info)
    update_snapshot(run_id=run_id, run_status="starting", started_at=_now_iso())
    return process_info


def request_pause() -> Dict[str, bool]:
    state = update_control_state(pause_requested=True)
    emit_event("pause_state_changed", "Dashboard pause requested", paused=True)
    return state


def request_resume() -> Dict[str, bool]:
    state = update_control_state(pause_requested=False)
    emit_event("pause_state_changed", "Dashboard resume requested", paused=False)
    return state


def request_stop() -> Dict[str, bool]:
    state = update_control_state(stop_requested=True)
    emit_event("stop_requested", "Dashboard stop requested")
    return state


def sync_process_state() -> Dict[str, Any]:
    process_info = get_process_info()
    if process_info and not is_process_running(process_info.get("pid")):
        clear_process_info()
    return get_process_info()


async def capture_page_screenshot(page: Any, label: str) -> str | None:
    ensure_dashboard_artifacts()
    snapshot = get_snapshot()
    run_id = os.getenv("DASHBOARD_RUN_ID") or snapshot.get("run_id") or "adhoc"
    run_dir = SCREENSHOT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    history_file = run_dir / f"{timestamp}-{label}.png"

    try:
        await page.screenshot(path=str(LATEST_SCREENSHOT_FILE), full_page=False)
    except Exception:
        return None

    try:
        copyfile(LATEST_SCREENSHOT_FILE, history_file)
    except OSError:
        history_file = LATEST_SCREENSHOT_FILE

    relative_path = str(LATEST_SCREENSHOT_FILE.relative_to(ROOT_DIR))
    history_relative_path = str(history_file.relative_to(ROOT_DIR))
    current_job = snapshot.get("current_job") or {}
    _append_screenshot_history(
        {
            "run_id": run_id,
            "timestamp": _now_iso(),
            "label": label,
            "path": history_relative_path,
            "job_title": current_job.get("title"),
            "company_name": current_job.get("company"),
            "url": current_job.get("url"),
            "stage": current_job.get("stage"),
        }
    )
    emit_event(
        "screenshot_updated",
        f"Screenshot updated for {label}",
        label=label,
        path=relative_path,
        history_path=history_relative_path,
    )
    return relative_path


def check_for_stop_request() -> None:
    if get_control_state().get("stop_requested"):
        raise StopRequested("Dashboard requested stop")


def _signal_process_tree(pid: int, sig: int) -> None:
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = pid
    try:
        os.killpg(pgid, sig)
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def terminate_running_process() -> bool:
    process_info = get_process_info()
    pid = process_info.get("pid")
    if not is_process_running(pid):
        clear_process_info()
        return False
    update_control_state(stop_requested=True)
    _signal_process_tree(pid, signal.SIGTERM)
    clear_process_info()
    emit_event("run_stopped", "Bot process terminated by dashboard")
    update_snapshot(run_status="stopped", finished_at=_now_iso(), current_job=None)
    return True
