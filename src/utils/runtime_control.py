import asyncio
import ctypes
import hashlib
import json
import os
import signal
import tempfile
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from threading import Event, Lock
from typing import Any

from config.logger_config import logger
from src.utils.redaction import REDACTED

_shutdown_handlers_registered = False
_windows_console_handler = None


class ShutdownState(Enum):
    """Phases of a graceful shutdown."""

    RUNNING = "running"
    DRAINING = "draining"  # current job finishing, no new jobs started
    CLEANUP = "cleanup"  # browser cleanup in progress
    DONE = "done"


class BrowserClosedError(RuntimeError):
    """Raised when the browser window is closed during a run."""


class GracefulShutdownRequested(RuntimeError):
    """Raised when the application should stop after cleanup."""


@dataclass
class RunAggregate:
    """One set of counters shared by all search processes in a launcher run."""

    run_id: str
    started_at: float = field(default_factory=time.monotonic)
    discovered_job_keys: set[str] = field(default_factory=set)
    terminal_outcomes: dict[str, str] = field(default_factory=dict)
    # One privacy-safe pre-application disposition for each newly discovered
    # job. Keys are opaque digests and values are fixed event names.
    job_dispositions: dict[str, str] = field(default_factory=dict)
    found: int = 0
    encountered: int = 0
    new: int = 0
    attempted: int = 0
    easy_apply_attempted: int = 0
    external_attempted: int = 0
    submitted: int = 0
    easy_apply_deferred: int = 0
    unverified: int = 0
    technical_failure: int = 0
    cancelled: int = 0
    needs_human: int = 0
    not_eligible: int = 0
    skipped: int = 0
    # Physical irreversible actions are counted at their dispatch boundary,
    # not when a worker merely discovers a control.  This lets audits prove
    # that shutdown closed every irreversible start path.
    irreversible_dispatch_starts: dict[str, int] = field(default_factory=dict)
    blocked_irreversible_dispatches: dict[str, int] = field(default_factory=dict)
    applied_jobs: list[dict[str, str]] = field(default_factory=list)
    start_notification_sent: bool = False


class RuntimeController:
    """Coordinates shutdown and browser-close reactions across sync/async boundaries."""

    def __init__(self) -> None:
        self._state_lock = Lock()
        self.shutdown_requested = Event()
        self.cleanup_complete = Event()
        self.cleanup_complete.set()
        self.drain_complete = Event()
        self.drain_complete.set()
        self.shutdown_state = ShutdownState.RUNNING
        self._active_jobs = 0
        self._active_workers = 0
        self._active_worker_kind: str | None = None
        self._aggregate: RunAggregate | None = None
        self._external_shutdown_observed = False

    def _aggregate_path(self) -> Path | None:
        value = os.environ.get("BOBBY_RUN_STATE_FILE")
        return Path(value) if value else None

    @staticmethod
    def _opaque_key(value: object, namespace: str) -> str:
        """Persist stable opaque keys instead of job URLs or other identifiers."""
        text = str(value or "").strip()
        prefix = f"{namespace}:"
        if text.startswith(prefix) and len(text) == len(prefix) + 64:
            return text
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        return f"{prefix}{digest}"

    def opaque_job_key(self, value: object) -> str:
        """Return the stable, non-reversible identifier used in telemetry."""
        return self._opaque_key(value, "job")

    def _load_aggregate_locked(self, run_id: str) -> RunAggregate:
        path = self._aggregate_path()
        if path is None or not path.exists():
            return RunAggregate(run_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or payload["aggregate"]["run_id"] != run_id:
                raise ValueError("Run aggregate identity/version mismatch")
            values = payload["aggregate"]
            # Run-state files can outlive a Bobby upgrade.  New quota fields
            # must default safely instead of invalidating a live run ledger.
            values.setdefault("easy_apply_deferred", 0)
            values.setdefault("irreversible_dispatch_starts", {})
            values.setdefault("blocked_irreversible_dispatches", {})
            values["discovered_job_keys"] = {
                self._opaque_key(value, "job") for value in values["discovered_job_keys"]
            }
            values["terminal_outcomes"] = {
                self._opaque_key(key, "outcome"): value
                for key, value in values["terminal_outcomes"].items()
            }
            values["job_dispositions"] = {
                self._opaque_key(key, "job"): str(value)
                for key, value in values.get("job_dispositions", {}).items()
                if value
            }
            values["applied_jobs"] = [
                {**dict(item), "url": REDACTED}
                for item in values.get("applied_jobs", [])
            ]
            aggregate = RunAggregate(**values)
            counts = (aggregate.found, aggregate.encountered, aggregate.new,
                      aggregate.attempted, aggregate.easy_apply_attempted,
                      aggregate.external_attempted, aggregate.submitted,
                      aggregate.easy_apply_deferred,
                      aggregate.unverified, aggregate.technical_failure,
                      aggregate.cancelled, aggregate.needs_human,
                      aggregate.not_eligible, aggregate.skipped)
            if any(type(count) is not int or count < 0 for count in counts):
                raise ValueError("Invalid run aggregate counts")
            if aggregate.found != len(aggregate.discovered_job_keys):
                raise ValueError("Invalid discovered-job count")
            return aggregate
        except Exception as exc:
            # Do not silently reset a damaged run-wide ledger and publish
            # counters that omit applications from an earlier profile.
            raise RuntimeError("Run aggregate could not be restored safely") from exc

    def _persist_aggregate_locked(self) -> None:
        path = self._aggregate_path()
        if path is None or self._aggregate is None:
            return
        values = asdict(self._aggregate)
        values["discovered_job_keys"] = sorted(self._aggregate.discovered_job_keys)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".bobby-stats-", delete=False) as stream:
                temporary = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"version": 1, "aggregate": values}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _sync_external_shutdown_locked(self) -> None:
        """Import the launcher's sticky shutdown state into this process."""
        shutdown_file = os.environ.get("BOBBY_SHUTDOWN_FILE")
        if not shutdown_file or not os.path.isfile(shutdown_file):
            return
        if not self.shutdown_requested.is_set():
            self.shutdown_requested.set()
            if self.shutdown_state == ShutdownState.RUNNING:
                self.shutdown_state = ShutdownState.DRAINING
            if not self._external_shutdown_observed:
                logger.warning(
                    "Shutdown requested via launcher sentinel. "
                    "Draining the active worker before exit."
                )
                self._external_shutdown_observed = True
            self._refresh_drain_state_locked()

    def _publish_shutdown_sentinel_locked(self) -> None:
        """Make a direct-process shutdown visible to isolated workers.

        The normal Fish launcher creates this sentinel before forwarding a
        signal.  A direct SIGINT to ``main.py`` bypasses that launcher,
        however, while an external ATS worker is deliberately a separate
        process/session.  Publish the same sticky signal before closing this
        controller's local admission boundary so every worker's existing
        irreversible-dispatch gate can fail closed on its next check.
        """

        shutdown_file = os.environ.get("BOBBY_SHUTDOWN_FILE")
        if not shutdown_file:
            return
        try:
            descriptor = os.open(
                shutdown_file,
                os.O_WRONLY | os.O_CREAT,
                0o600,
            )
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        except OSError as error:
            # The local controller must still drain safely.  A launcher
            # sentinel is an additional cross-process boundary, so expose
            # only a fixed error class and never the environment-provided
            # path.
            logger.warning(
                "Could not publish launcher shutdown sentinel | "
                f"error_class={type(error).__name__}"
            )

    def start_process_run(self, run_id: str) -> bool:
        """Initialize state once for a new process, never for browser recovery."""
        with self._state_lock:
            self._sync_external_shutdown_locked()
            if self.shutdown_requested.is_set():
                return False
            self.cleanup_complete.clear()
            self.drain_complete.set()
            self.shutdown_state = ShutdownState.RUNNING
            self._active_jobs = 0
            self._active_workers = 0
            self._active_worker_kind = None
            self._aggregate = self._load_aggregate_locked(str(run_id))
            self._persist_aggregate_locked()
            return True

    def set_shutdown_state(self, state: ShutdownState) -> None:
        with self._state_lock:
            self._sync_external_shutdown_locked()
            if state == ShutdownState.RUNNING and self.shutdown_requested.is_set():
                return
            self.shutdown_state = state
            self._refresh_drain_state_locked()

    def request_shutdown(self, source: str) -> None:
        with self._state_lock:
            # Publish first: an isolated worker can otherwise pass its final
            # dispatch gate in the narrow interval after this process starts
            # draining but before it learns about a direct signal.
            self._publish_shutdown_sentinel_locked()
            if self.shutdown_state == ShutdownState.RUNNING:
                self.shutdown_state = ShutdownState.DRAINING
                logger.warning(
                    f"Shutdown requested via {source}. "
                    "Draining the active worker before exit."
                )
            elif not self.shutdown_requested.is_set():
                logger.warning(f"Shutdown requested via {source}. Finishing current cleanup.")
            # Setting this while holding the same lock as the admission methods
            # makes closing admission and observing shutdown one atomic boundary.
            self.shutdown_requested.set()
            self._refresh_drain_state_locked()

    def is_shutdown_requested(self) -> bool:
        with self._state_lock:
            self._sync_external_shutdown_locked()
            return self.shutdown_requested.is_set()

    def is_accepting_new_jobs(self) -> bool:
        """Return whether a new vacancy may cross the intake boundary."""
        with self._state_lock:
            self._sync_external_shutdown_locked()
            return (
                self.shutdown_state == ShutdownState.RUNNING
                and not self.shutdown_requested.is_set()
            )

    def try_start_job(self) -> bool:
        """Atomically admit one job while the runtime is still accepting work."""
        with self._state_lock:
            self._sync_external_shutdown_locked()
            if (
                self.shutdown_state != ShutdownState.RUNNING
                or self.shutdown_requested.is_set()
                or self._active_jobs > 0
            ):
                return False
            self._active_jobs += 1
            self.drain_complete.clear()
            return True

    def finish_job(self) -> None:
        """Release the current job after its result and counters are finalized."""
        with self._state_lock:
            if self._active_jobs > 0:
                self._active_jobs -= 1
            self._refresh_drain_state_locked()

    def try_start_worker(self, kind: str) -> bool:
        """Atomically admit one application worker before shutdown starts."""
        with self._state_lock:
            self._sync_external_shutdown_locked()
            if (
                self.shutdown_state != ShutdownState.RUNNING
                or self.shutdown_requested.is_set()
                or self._active_workers > 0
            ):
                return False
            self._active_workers = 1
            self._active_worker_kind = kind
            self.drain_complete.clear()
            return True

    def finish_worker(self, kind: str | None = None) -> None:
        """Release the active application worker after terminal classification."""
        with self._state_lock:
            if self._active_workers > 0 and (
                kind is None or kind == self._active_worker_kind
            ):
                self._active_workers = 0
                self._active_worker_kind = None
            self._refresh_drain_state_locked()

    def has_active_worker(self, kind: str | None = None) -> bool:
        with self._state_lock:
            if self._active_workers == 0:
                return False
            return kind is None or kind == self._active_worker_kind

    def try_start_irreversible_dispatch(self, action: str) -> bool:
        """Atomically admit one physical irreversible browser dispatch.

        Inspection and DOM-node acquisition may finish while SIGINT is being
        delivered.  Callers must use this immediately before the physical
        upload/click/registration dispatch.  A rejected call is telemetry
        only; it is never counted as a dispatch start.
        """
        action = str(action or "unknown").strip() or "unknown"
        # Keep the public hook easy to control in deterministic tests and
        # ensure an already-visible launcher sentinel is imported before the
        # critical section below.
        if self.is_shutdown_requested():
            with self._state_lock:
                aggregate = self._aggregate
                if aggregate is not None:
                    blocked = aggregate.blocked_irreversible_dispatches
                    blocked[action] = blocked.get(action, 0) + 1
                    self._persist_aggregate_locked()
            return False
        with self._state_lock:
            self._sync_external_shutdown_locked()
            if self.shutdown_requested.is_set() or self.shutdown_state != ShutdownState.RUNNING:
                aggregate = self._aggregate
                if aggregate is not None:
                    blocked = aggregate.blocked_irreversible_dispatches
                    blocked[action] = blocked.get(action, 0) + 1
                    self._persist_aggregate_locked()
                return False
            aggregate = self._aggregate
            if aggregate is not None:
                starts = aggregate.irreversible_dispatch_starts
                starts[action] = starts.get(action, 0) + 1
                self._persist_aggregate_locked()
            return True

    @property
    def active_job_count(self) -> int:
        with self._state_lock:
            return self._active_jobs

    @property
    def active_worker_count(self) -> int:
        with self._state_lock:
            return self._active_workers

    def wait_for_drain(self, timeout: float = 15.0) -> bool:
        return self.drain_complete.wait(timeout)

    def _refresh_drain_state_locked(self) -> None:
        if (
            self.shutdown_requested.is_set()
            and self._active_jobs == 0
            and self._active_workers == 0
        ):
            self.drain_complete.set()

    def begin_run(self) -> bool:
        with self._state_lock:
            self._sync_external_shutdown_locked()
            # A signal can arrive during startup, before create_and_run_bot().
            # Never erase that request while opening a new admission window.
            if self.shutdown_requested.is_set():
                self.shutdown_state = ShutdownState.DRAINING
                self._refresh_drain_state_locked()
                return False
            self.cleanup_complete.clear()
            self.drain_complete.set()
            self.shutdown_state = ShutdownState.RUNNING
            self._active_jobs = 0
            self._active_workers = 0
            self._active_worker_kind = None
            return True

    def mark_start_notification(self) -> bool:
        """Return True exactly once for the process-wide run."""
        with self._state_lock:
            if self._aggregate is None or self._aggregate.start_notification_sent:
                return self._aggregate is None
            self._aggregate.start_notification_sent = True
            self._persist_aggregate_locked()
            return True

    def record_discovered_job(self, job_key: str, *, encountered: bool) -> bool:
        """Count a stable job key once across browser/session recovery."""
        with self._state_lock:
            aggregate = self._aggregate
            if aggregate is None:
                return True
            key = self._opaque_key(job_key, "job")
            if not key or key in aggregate.discovered_job_keys:
                return False
            aggregate.discovered_job_keys.add(key)
            aggregate.found += 1
            if encountered:
                aggregate.encountered += 1
            else:
                aggregate.new += 1
                aggregate.job_dispositions[key] = "JOB_DISCOVERED_NEW"
            self._persist_aggregate_locked()
            return True

    def record_job_disposition(self, job_key: str, disposition: str) -> bool:
        """Record one safe disposition for a newly discovered job.

        The initial ``JOB_DISCOVERED_NEW`` value is replaced by a final
        disposition as the job moves through filtering, suitability, routing,
        or worker admission. A later duplicate caller cannot overwrite a
        final value, keeping recovery and shutdown paths idempotent.
        """
        with self._state_lock:
            aggregate = self._aggregate
            if aggregate is None:
                return True
            key = self._opaque_key(job_key, "job")
            if key not in aggregate.job_dispositions:
                return False
            value = str(disposition or "").strip()
            if not value:
                return False
            current = aggregate.job_dispositions.get(key)
            if current == value:
                return False
            if current and current != "JOB_DISCOVERED_NEW":
                return False
            aggregate.job_dispositions[key] = value
            self._persist_aggregate_locked()
            return True

    def record_retryable_job_disposition(self, job_key: str, disposition: str) -> bool:
        """Replace an admission marker with a narrowly allowlisted retryable outcome."""

        if disposition != "JOB_DEFERRED_EASY_APPLY_LIMIT":
            return False
        with self._state_lock:
            aggregate = self._aggregate
            if aggregate is None:
                return True
            key = self._opaque_key(job_key, "job")
            current = aggregate.job_dispositions.get(key)
            outcome_key = self._opaque_key(job_key, "outcome")
            if aggregate.terminal_outcomes.get(outcome_key) in {
                "SUBMITTED",
                "UNVERIFIED_AFTER_SUBMIT",
            }:
                return False
            if current not in {"JOB_DISCOVERED_NEW", "JOB_ADMITTED"}:
                return False
            aggregate.job_dispositions[key] = disposition
            self._persist_aggregate_locked()
            return True

    def job_disposition(self, job_key: str) -> str | None:
        """Return the current safe disposition for a discovered job."""
        with self._state_lock:
            aggregate = self._aggregate
            if aggregate is None:
                return None
            return aggregate.job_dispositions.get(self._opaque_key(job_key, "job"))

    def record_attempt(self, method: str) -> None:
        with self._state_lock:
            if self._aggregate is None:
                return
            self._aggregate.attempted += 1
            if method == "easy_apply":
                self._aggregate.easy_apply_attempted += 1
            elif method == "external":
                self._aggregate.external_attempted += 1
            self._persist_aggregate_locked()

    def record_terminal_outcome(
        self,
        job_key: str,
        classification: str,
        *,
        job_title: str = "",
        company_name: str = "",
        url: str = "",
    ) -> bool:
        """Commit one terminal classification once before public publication."""
        with self._state_lock:
            aggregate = self._aggregate
            if aggregate is None or classification == "LIMIT":
                return True
            key = self._opaque_key(
                job_key or url or f"{company_name}|{job_title}", "outcome"
            )
            if key in aggregate.terminal_outcomes:
                return False
            aggregate.terminal_outcomes[key] = classification
            if classification == "SUBMITTED":
                aggregate.submitted += 1
                aggregate.applied_jobs.append(
                    {
                        "job_title": job_title,
                        "company_name": company_name,
                        "url": REDACTED,
                    }
                )
            elif classification == "UNVERIFIED_AFTER_SUBMIT":
                aggregate.unverified += 1
            elif classification == "DEFERRED_EASY_APPLY_LIMIT":
                aggregate.easy_apply_deferred += 1
            elif classification == "TECHNICAL_FAILURE":
                aggregate.technical_failure += 1
            elif classification == "CANCELLED":
                aggregate.cancelled += 1
            elif classification == "NEEDS_HUMAN":
                aggregate.needs_human += 1
            elif classification == "NOT_ELIGIBLE":
                aggregate.not_eligible += 1
                aggregate.skipped += 1
            elif classification == "SKIPPED":
                aggregate.skipped += 1
            self._persist_aggregate_locked()
            return True

    def has_terminal_outcome(self, job_key: str) -> bool:
        with self._state_lock:
            if self._aggregate is None:
                return False
            key = self._opaque_key(job_key, "outcome")
            return key in self._aggregate.terminal_outcomes

    def aggregate_snapshot(self, *, partial: bool | None = None) -> dict[str, Any] | None:
        """Return an internally consistent copy suitable for final reporting."""
        with self._state_lock:
            aggregate = self._aggregate
            if aggregate is None:
                return None
            terminal_counts = {
                name: sum(
                    1 for value in aggregate.terminal_outcomes.values() if value == name
                )
                for name in (
                    "SUBMITTED",
                    "DEFERRED_EASY_APPLY_LIMIT",
                    "UNVERIFIED_AFTER_SUBMIT",
                    "TECHNICAL_FAILURE",
                    "CANCELLED",
                    "NEEDS_HUMAN",
                    "NOT_ELIGIBLE",
                    "SKIPPED",
                )
            }
            terminal_total = sum(terminal_counts.values())
            processed = len(aggregate.terminal_outcomes)
            disposition_counts: dict[str, int] = {}
            for value in aggregate.job_dispositions.values():
                disposition_counts[value] = disposition_counts.get(value, 0) + 1
            counters_match_buckets = (
                aggregate.submitted == terminal_counts["SUBMITTED"]
                and aggregate.easy_apply_deferred
                == terminal_counts["DEFERRED_EASY_APPLY_LIMIT"]
                and aggregate.unverified == terminal_counts["UNVERIFIED_AFTER_SUBMIT"]
                and aggregate.technical_failure == terminal_counts["TECHNICAL_FAILURE"]
                and aggregate.cancelled == terminal_counts["CANCELLED"]
                and aggregate.needs_human == terminal_counts["NEEDS_HUMAN"]
                and aggregate.not_eligible == terminal_counts["NOT_ELIGIBLE"]
                and aggregate.skipped
                == terminal_counts["SKIPPED"] + terminal_counts["NOT_ELIGIBLE"]
            )
            return {
                "run_id": aggregate.run_id,
                "duration_seconds": max(0, int(time.monotonic() - aggregate.started_at)),
                "found": aggregate.found,
                "encountered": aggregate.encountered,
                "new": aggregate.new,
                "attempted": aggregate.attempted,
                "submitted": aggregate.submitted,
                "easy_apply_attempted": aggregate.easy_apply_attempted,
                "easy_apply_deferred": aggregate.easy_apply_deferred,
                "external_attempted": aggregate.external_attempted,
                "unverified": aggregate.unverified,
                "technical_failure": aggregate.technical_failure,
                "cancelled": aggregate.cancelled,
                "needs_human": aggregate.needs_human,
                "not_eligible": aggregate.not_eligible,
                "skipped": aggregate.skipped,
                "skipped_total": aggregate.skipped + aggregate.encountered,
                "processed": processed,
                "failed": (
                    aggregate.unverified
                    + aggregate.technical_failure
                    + aggregate.needs_human
                ),
                "in_progress": self._active_jobs,
                "partial": self.shutdown_requested.is_set() if partial is None else partial,
                "applied_jobs": [dict(item) for item in aggregate.applied_jobs],
                "terminal_counts": terminal_counts,
                "disposition_counts": disposition_counts,
                "irreversible_dispatch_starts": dict(
                    aggregate.irreversible_dispatch_starts
                ),
                "blocked_irreversible_dispatches": dict(
                    aggregate.blocked_irreversible_dispatches
                ),
                "new_dispositions": len(aggregate.job_dispositions),
                "unresolved_new_dispositions": disposition_counts.get(
                    "JOB_DISCOVERED_NEW", 0
                ),
                "consistent": terminal_total == processed and counters_match_buckets,
            }

    def finish_run(self) -> None:
        self.cleanup_complete.set()

    def wait_for_cleanup(self, timeout: float = 15.0) -> bool:
        return self.cleanup_complete.wait(timeout)


runtime_controller = RuntimeController()


def _handle_shutdown_signal(signum, _frame) -> None:
    """Convert OS signals into a graceful shutdown request."""
    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        signal_name = str(signum)
    runtime_controller.request_shutdown(f"signal {signal_name}")


def register_shutdown_handlers() -> None:
    """Register SIGINT/SIGTERM and Windows console-close handlers once."""
    global _shutdown_handlers_registered, _windows_console_handler
    if _shutdown_handlers_registered:
        return

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    if os.name == "nt":
        handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
        event_names = {
            0: "CTRL_C_EVENT",
            1: "CTRL_BREAK_EVENT",
            2: "CTRL_CLOSE_EVENT",
            5: "CTRL_LOGOFF_EVENT",
            6: "CTRL_SHUTDOWN_EVENT",
        }

        def console_handler(ctrl_type: int) -> bool:
            runtime_controller.request_shutdown(
                f"console event {event_names.get(ctrl_type, ctrl_type)}"
            )
            runtime_controller.wait_for_cleanup(timeout=15.0)
            return True

        _windows_console_handler = handler_type(console_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_windows_console_handler, True)

    _shutdown_handlers_registered = True


def attach_browser_close_watchers(browser: Any, context: Any, page: Any) -> asyncio.Event:
    """Return an event that fires when the browser/page/context is closed."""
    browser_closed = asyncio.Event()

    def mark_closed(target_name: str) -> None:
        if not browser_closed.is_set():
            logger.warning(
                f"{target_name} was closed. Press Ctrl+C to stop, or the bot will reopen the browser shortly."
            )
            browser_closed.set()

    browser.on("disconnected", lambda: mark_closed("Browser"))
    context.on("close", lambda: mark_closed("Browser context"))
    page.on("close", lambda: mark_closed("Browser page"))
    return browser_closed


async def _wait_for_shutdown_request() -> str:
    await asyncio.to_thread(runtime_controller.shutdown_requested.wait)
    return "shutdown"


async def _wait_for_browser_close(browser_closed: asyncio.Event) -> str:
    await browser_closed.wait()
    return "browser_closed"


async def _cancel_task(task: asyncio.Task | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def run_with_runtime_guards(bot: Any, browser_closed: asyncio.Event):
    """Race the bot against runtime control events."""
    apply_task = asyncio.create_task(bot.start_apply())
    browser_task = asyncio.create_task(_wait_for_browser_close(browser_closed))
    shutdown_task = asyncio.create_task(_wait_for_shutdown_request())

    try:
        done, _pending = await asyncio.wait(
            {apply_task, browser_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if apply_task in done:
            return await apply_task

        if browser_task in done:
            await _cancel_task(apply_task)
            raise BrowserClosedError("Browser window closed by user")

        if shutdown_task in done:
            await _cancel_task(apply_task)
            raise GracefulShutdownRequested("Shutdown requested")
    finally:
        await _cancel_task(browser_task)
        await _cancel_task(shutdown_task)


async def sleep_with_shutdown(seconds: int) -> bool:
    """Sleep in short chunks so shutdown requests are honored quickly."""
    for _ in range(seconds):
        if runtime_controller.is_shutdown_requested():
            return False
        await asyncio.sleep(1)
    return not runtime_controller.is_shutdown_requested()


def countdown_before_restart(seconds: int = 5) -> bool:
    """Give the user a short window to cancel restart after browser closure."""
    logger.warning("I can reopen the browser and continue. Press Ctrl+C now to stop applications.")
    for remaining in range(seconds, 0, -1):
        if runtime_controller.is_shutdown_requested():
            logger.info("Restart cancelled by shutdown request")
            return False
        logger.warning(f"Reopening browser in {remaining}...")
        time.sleep(1)
    return not runtime_controller.is_shutdown_requested()
