"""Persistent, privacy-safe LinkedIn Easy Apply quota state.

The state is deliberately separate from the encountered-job cache.  A quota
is a retryable routing condition, not a job outcome, and it must not consume a
vacancy or weaken duplicate-submit protection.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Awaitable, Callable

from config.logger_config import logger


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EASY_APPLY_QUOTA_STATE_PATH = (
    REPOSITORY_ROOT / "data" / "output" / "linkedin" / "easy_apply_quota.json"
)
STATE_VERSION = 1
AVAILABLE = "available"
BLOCKED = "blocked"
UNKNOWN = "unknown"
SOURCE = "linkedin_ui_runtime"

# This is only a bounded recheck throttle.  It is not an assumed LinkedIn
# daily reset and never turns BLOCKED into AVAILABLE without fresh UI evidence.
RECHECK_INTERVAL_SECONDS = 15 * 60

DEFERRED_REASON = (
    "DEFERRED_EASY_APPLY_LIMIT: LinkedIn Easy Apply quota is blocked; "
    "external ATS remains eligible"
)

_LIMIT_ACTIONS = (
    "reached",
    "exceeded",
    "cannot",
    "can't",
    "unable",
    "not able",
    "try again",
    "temporarily",
    "unavailable",
    "tomorrow",
    "later",
)
_VALIDATION_MARKERS = (
    "validation error",
    "required field",
    "required question",
    "invalid answer",
    "missing answer",
    "please enter",
)


@dataclass(frozen=True)
class QuotaObservation:
    status: str
    reason: str = ""
    retry_after_epoch: float | None = None
    button_available: bool = False


def _now_epoch() -> float:
    return time.time()


def _iso_from_epoch(value: float | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _epoch_from_iso(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_ui_text(value: Any) -> str:
    """Normalize transient UI text without persisting or logging it."""

    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _duration_seconds(text: str) -> float | None:
    """Parse only explicit retry durations; never infer midnight or a day reset."""

    match = re.search(
        r"\b(?:try again|apply again|retry)\s+(?:in|after)\s+"
        r"(\d{1,3})\s*(minute|minutes|min|hour|hours|hr|hrs)\b",
        text,
    )
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    multiplier = 60 if unit.startswith("min") else 3600
    return float(amount * multiplier)


def detect_quota_message(value: Any) -> tuple[bool, float | None]:
    """Return strong quota evidence and an optional explicit retry duration.

    Generic validation, network, provider, CAPTCHA, and stale-modal text do
    not satisfy these semantic patterns.  The generic application patterns
    are intentionally accepted only because this function is called from the
    Easy Apply UI context, never from arbitrary ATS or provider errors.
    """

    text = normalize_ui_text(value)
    if not text:
        return False, None

    has_easy_context = bool(re.search(r"\beasy\s*apply\b", text))
    has_limit_word = bool(re.search(r"\b(?:limit|quota|volume)\b", text))
    has_action = any(marker in text for marker in _LIMIT_ACTIONS)

    easy_apply_pattern = (
        has_easy_context
        and has_limit_word
        and has_action
        and bool(
            re.search(
                r"\b(?:application|submission|apply|easy\s*apply|limit|quota|volume)\b",
                text,
            )
        )
    )
    generic_daily_pattern = bool(
        re.search(
            r"\b(?:reached|exceeded|cannot|can't|unable)\b.{0,100}"
            r"\b(?:today|daily|applications?|submissions?)\b.{0,80}"
            r"\b(?:limit|quota|again later|tomorrow)\b",
            text,
        )
    )
    explicit_retry_pattern = bool(
        re.search(
            r"\b(?:try again|apply again)\b.{0,60}\b(?:later|tomorrow|in|after)\b",
            text,
        )
        and (has_limit_word or has_easy_context or "application" in text)
    )

    if not (easy_apply_pattern or generic_daily_pattern or explicit_retry_pattern):
        return False, None

    # A validation message may contain both "application" and "limit" as
    # ordinary form language.  It is not quota evidence unless the stronger
    # action/limit relationship also appears.
    if any(marker in text for marker in _VALIDATION_MARKERS) and not (
        easy_apply_pattern and has_action and has_limit_word
    ):
        return False, None

    duration = _duration_seconds(text)
    return True, duration


def classify_easy_apply_ui(
    text: Any,
    *,
    button_available: bool = False,
    now_epoch: float | None = None,
) -> QuotaObservation:
    """Classify transient page evidence without retaining raw UI text."""

    detected, retry_seconds = detect_quota_message(text)
    if detected:
        now = _now_epoch() if now_epoch is None else float(now_epoch)
        return QuotaObservation(
            status=BLOCKED,
            reason="limit_message",
            retry_after_epoch=now + retry_seconds if retry_seconds else None,
            button_available=bool(button_available),
        )
    if button_available:
        return QuotaObservation(status=AVAILABLE, reason="enabled_easy_apply_control", button_available=True)
    return QuotaObservation(status=UNKNOWN, reason="no_decisive_evidence")


async def collect_easy_apply_ui(
    page: Any,
    *,
    find_elements: Callable[[Any, str, str], Awaitable[list[Any]]] | None = None,
) -> QuotaObservation:
    """Read bounded Easy Apply evidence from a live LinkedIn page.

    The caller can inject Bobby's already-imported finder in tests and in the
    Playwright adapter.  Only a boolean/allowlisted reason leaves this helper;
    raw page text is never logged or persisted.
    """

    if find_elements is None:
        from src.utils.browser_utils import find_elements_safely

        find_elements = find_elements_safely

    text_parts: list[str] = []
    selectors = (
        ("xpath", "//div[contains(@class, 'artdeco-inline-feedback--error')]") ,
        ("xpath", "//*[@role='dialog']"),
    )
    for by, selector in selectors:
        try:
            elements = await find_elements(page, selector, by)
        except Exception:
            continue
        for element in list(elements or [])[:20]:
            try:
                is_visible = getattr(element, "is_visible", None)
                if is_visible is not None and not await is_visible():
                    continue
                text_content = getattr(element, "text_content", None)
                if text_content is not None:
                    value = await text_content()
                else:
                    inner_text = getattr(element, "inner_text", None)
                    value = await inner_text() if inner_text is not None else ""
                if value:
                    text_parts.append(str(value)[:6000])
            except Exception:
                continue

    # `body.text_content()` includes display:none content from stale dialogs.
    # Rendered innerText is the safer whole-page fallback for LinkedIn's
    # transient limit notice when it is not inside a semantic error/dialog node.
    try:
        rendered_body = await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
        if rendered_body:
            text_parts.append(str(rendered_body)[:12000])
    except Exception:
        pass

    button_available = False
    button_selectors = (
        ("xpath", "//button[contains(translate(@aria-label, 'EASYAPPLY', 'easyapply'), 'easy apply') or contains(translate(., 'EASYAPPLY', 'easyapply'), 'easy apply')]") ,
        ("xpath", "//a[contains(translate(@aria-label, 'EASYAPPLY', 'easyapply'), 'easy apply') or contains(translate(., 'EASYAPPLY', 'easyapply'), 'easy apply')]") ,
    )
    for by, selector in button_selectors:
        try:
            elements = await find_elements(page, selector, by)
        except Exception:
            continue
        for element in list(elements or [])[:10]:
            try:
                visible = await element.is_visible()
                enabled = await element.is_enabled()
                if visible and enabled:
                    button_available = True
                    break
            except Exception:
                continue
        if button_available:
            break

    return classify_easy_apply_ui("\n".join(text_parts), button_available=button_available)


class EasyApplyQuotaState:
    """Atomic owner-only JSON state shared by profiles and process restarts."""

    def __init__(self, path: Path = EASY_APPLY_QUOTA_STATE_PATH) -> None:
        self.path = Path(path)
        self._lock = RLock()

    @staticmethod
    def _default() -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "status": UNKNOWN,
            "detected_at": None,
            "last_confirmed_at": None,
            "reason": None,
            "retry_after": None,
            "source": SOURCE,
        }

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._default()
        if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
            return self._default()
        state = self._default()
        state.update(value)
        if state.get("status") not in {AVAILABLE, BLOCKED, UNKNOWN}:
            return self._default()
        state["source"] = SOURCE
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            temporary = Path(name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._load())

    def status(self) -> str:
        return str(self.snapshot().get("status") or UNKNOWN)

    def is_blocked(self) -> bool:
        return self.status() == BLOCKED

    def should_recheck(self, now_epoch: float | None = None) -> bool:
        with self._lock:
            state = self._load()
            if state.get("status") != BLOCKED:
                return False
            now = _now_epoch() if now_epoch is None else float(now_epoch)
            retry_after = _epoch_from_iso(state.get("retry_after"))
            if retry_after is not None and now >= retry_after:
                return True
            baseline = _epoch_from_iso(state.get("last_confirmed_at")) or _epoch_from_iso(
                state.get("detected_at")
            )
            return baseline is None or now - baseline >= RECHECK_INTERVAL_SECONDS

    def _publish(self, event_name: str, *, status: str, reason: str) -> None:
        logger.info(
            f"{event_name} | status={status} | reason={reason} | source={SOURCE}"
        )
        try:
            from src.dashboard.runtime import emit_event

            emit_event(
                event_name,
                event_name,
                status=status,
                reason=reason,
                source=SOURCE,
            )
        except Exception:
            # Quota state remains authoritative even when optional telemetry is unavailable.
            pass

    def mark_blocked(
        self,
        *,
        reason: str = "limit_message",
        retry_after_epoch: float | None = None,
        now_epoch: float | None = None,
    ) -> dict[str, Any]:
        now = _now_epoch() if now_epoch is None else float(now_epoch)
        with self._lock:
            state = self._load()
            was_blocked = state.get("status") == BLOCKED
            state["status"] = BLOCKED
            state["detected_at"] = state.get("detected_at") or _iso_from_epoch(now)
            state["last_confirmed_at"] = _iso_from_epoch(now)
            state["reason"] = reason if reason in {"limit_message", "limit_dialog"} else "limit_message"
            state["retry_after"] = _iso_from_epoch(retry_after_epoch)
            state["source"] = SOURCE
            self._save(state)
        if not was_blocked:
            self._publish("EASY_APPLY_QUOTA_DETECTED", status=BLOCKED, reason=state["reason"])
        else:
            self._publish("EASY_APPLY_QUOTA_BLOCK_ACTIVE", status=BLOCKED, reason=state["reason"])
        return state

    def mark_available(
        self, *, reason: str = "enabled_easy_apply_control", now_epoch: float | None = None
    ) -> dict[str, Any]:
        now = _now_epoch() if now_epoch is None else float(now_epoch)
        with self._lock:
            state = self._load()
            was_blocked = state.get("status") == BLOCKED
            state["status"] = AVAILABLE
            state["last_confirmed_at"] = _iso_from_epoch(now)
            state["reason"] = reason if reason == "enabled_easy_apply_control" else "ui_available"
            state["retry_after"] = None
            state["source"] = SOURCE
            self._save(state)
        if was_blocked:
            self._publish("EASY_APPLY_QUOTA_RESTORED", status=AVAILABLE, reason=state["reason"])
        return state

    def mark_recheck(self) -> None:
        if self.is_blocked():
            self._publish("EASY_APPLY_QUOTA_RECHECK", status=BLOCKED, reason="recheck_due")

    def apply_observation(self, observation: QuotaObservation) -> str:
        if observation.status == BLOCKED:
            return self.mark_blocked(
                reason=observation.reason or "limit_message",
                retry_after_epoch=observation.retry_after_epoch,
            ).get("status", BLOCKED)
        if observation.status == AVAILABLE:
            return self.mark_available().get("status", AVAILABLE)
        return self.status()

    @staticmethod
    def deferred_reason() -> str:
        return DEFERRED_REASON


_DEFAULT_STATE = EasyApplyQuotaState()


def easy_apply_quota_state() -> EasyApplyQuotaState:
    """Return the process-local facade over the durable shared state."""

    return _DEFAULT_STATE
