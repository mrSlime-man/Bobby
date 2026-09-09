"""Privacy guard for third-party Browser Use logging."""

from __future__ import annotations

import logging
import os
import re
from typing import Iterable

from src.utils.redaction import REDACTED, redact_text
from src.utils.run_context import get_run_id


def _provider_error_category(message: str) -> str:
    """Return a safe, low-cardinality category for third-party errors.

    Browser Use includes the complete provider response in several exception
    messages. That response can contain model-visible form data, so its body
    must never be sent to a production sink. Bobby emits provider health and
    retry decisions itself; this category preserves the useful diagnostic
    without retaining the untrusted payload.
    """
    normalized = message.casefold()
    if "429" in normalized or "resource_exhausted" in normalized or "quota" in normalized:
        return "rate_limited"
    if "503" in normalized or "unavailable" in normalized or "high demand" in normalized:
        return "unavailable"
    if "timeout" in normalized or "timed out" in normalized:
        return "timeout"
    if (
        "parse json" in normalized
        or "parse or validate" in normalized
        or "validation" in normalized
    ):
        return "malformed_response"
    return "operation_failure"


def redact_browser_use_text(value: object, sensitive_values: Iterable[str] = ()) -> str:
    """Redact third-party action payloads plus known candidate profile values."""
    message = redact_text(value)
    message = re.sub(r"(?i)\bhttps?://[^\s<>\"']+", REDACTED, message)
    for sensitive in (str(item).strip() for item in sensitive_values):
        if len(sensitive) >= 3:
            message = re.sub(re.escape(sensitive), REDACTED, message, flags=re.I)
    return re.sub(
        r"(?i)(\b(?:text|value|answer)\s*:\s*)(.*?)(?=,\s*\w+\s*:|$)",
        rf"\1{REDACTED}",
        message,
    )


class BrowserUseRedactionFilter(logging.Filter):
    def __init__(self, sensitive_values: Iterable[str] = ()) -> None:
        super().__init__()
        self.sensitive_values = tuple(
            value for value in (str(item).strip() for item in sensitive_values) if len(value) >= 3
        )

    def filter(self, record: logging.LogRecord) -> bool:
        raw_message = record.getMessage()
        if record.name.startswith("browser_use") and record.levelno >= logging.ERROR:
            # Do not redact-and-retain a provider exception: Gemini/OpenAI SDKs
            # may serialize their full raw response (including model-visible
            # form values) into the error string. A category is sufficient for
            # the provider circuit and the structured Bobby recovery event.
            message = (
                "Browser Use provider/operation error | "
                f"category={_provider_error_category(raw_message)} | payload=redacted"
            )
        else:
            message = redact_browser_use_text(raw_message, self.sensitive_values)
        record.msg = message
        record.args = ()
        # Correlate otherwise untimestamped Browser Use/provider diagnostics
        # without putting job URLs, action payloads, or candidate data in them.
        record.bobby_run_id = get_run_id()
        record.bobby_worker_pid = os.getpid()
        if record.exc_info:
            record.msg += f" | exception_type={record.exc_info[0].__name__}"
            record.exc_info = None
            record.exc_text = None
        record.stack_info = None
        return True


def install_browser_use_log_redaction(sensitive_values: Iterable[str] = ()) -> None:
    guard = BrowserUseRedactionFilter(sensitive_values)
    names = ("", "browser_use", "bubus", "cdp_use")
    seen: set[int] = set()
    for name in names:
        current = logging.getLogger(name)
        for handler in current.handlers:
            if id(handler) not in seen:
                handler.addFilter(guard)
                handler.setFormatter(
                    logging.Formatter(
                        "%(asctime)s | %(levelname)s | %(name)s | "
                        "run_id=%(bobby_run_id)s worker_pid=%(bobby_worker_pid)s | %(message)s"
                    )
                )
                seen.add(id(handler))
