"""Central redaction helpers for logs, mirrors, events, and evidence."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_URL_BASE_PATTERN = re.compile(r"(?i)\bhttps?://[^\s<>\"'?]+")

_SENSITIVE_KEYS = {
    "address", "api_key", "authorization", "birth_date", "birthday", "cookie",
    "date_of_birth", "dob", "email", "gmail_oauth", "magic_link", "oauth",
    "otp", "password", "phone", "refresh_token", "resume", "secret", "ssn",
    "storage_state", "token", "verification_code",
}

_URL_KEYS = {
    "apply_url",
    "ats_url",
    "external_url",
    "final_url",
    "job_url",
    "linkedin_url",
    "url",
}

_PATTERNS = (
    (re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), REDACTED),
    (re.compile(r"(?<!\d)(?:\+?1[-. (]*)?(?:\d{3}[-. )]*){2}\d{4}(?!\d)"), REDACTED),
    (re.compile(r"(?i)\b(?:date of birth|birth date|dob)\s*[:=]\s*[^\n,;]+"), "date_of_birth=[REDACTED]"),
    (re.compile(r"(?i)\b(password|api[_ -]?key|oauth[_ -]?token|access[_ -]?token|refresh[_ -]?token|otp|verification code|one[- ]?time code)\b\s*[:=]?\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)(https?://\S*[?&](?:token|code|key|secret|signature)=)[^&\s]+"), r"\1[REDACTED]"),
)


def redact_text(value: Any, *, limit: int | None = None) -> str:
    text = str(value or "")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:limit] if limit is not None else text


def redact_urls(value: Any, *, limit: int | None = None) -> str:
    """Remove complete URLs from text written to durable mirrors."""
    # Keep harmless query labels used in human-readable diagnostics while
    # removing the host/path and allowing redact_text to scrub secret values.
    text = _URL_BASE_PATTERN.sub(REDACTED, str(value or ""))
    return text[:limit] if limit is not None else text


def sanitize_value(value: Any, key: str = "") -> Any:
    normalized = key.lower().replace("-", "_")
    if normalized in _URL_KEYS:
        return REDACTED
    if normalized in _SENSITIVE_KEYS or any(
        normalized.endswith(f"_{suffix}")
        for suffix in ("password", "token", "otp", "secret", "api_key", "cookie")
    ):
        return REDACTED
    if isinstance(value, dict):
        return {k: sanitize_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_value(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value
