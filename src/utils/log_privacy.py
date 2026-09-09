"""Apply privacy protection before a record reaches any production log sink."""

from __future__ import annotations

import re
from pathlib import Path

import dotenv
import yaml

from src.utils.redaction import REDACTED, redact_text


def profile_sensitive_values(profile: object) -> tuple[str, ...]:
    """Return scalar profile values for sink-side redaction only.

    The application profile is authoritative for facts that may not appear in
    the resume, including the application email.  Treat every configured
    scalar as private here so a newly added profile field cannot accidentally
    reach a log sink before this protection is updated.
    """

    values: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, (list, tuple, set)):
            for nested in item:
                visit(nested)
        elif isinstance(item, (str, int, float)) and not isinstance(item, bool):
            normalized = str(item).strip()
            if len(normalized) >= 3 and normalized.casefold() != "unknown":
                values.add(normalized)

    visit(profile)
    return tuple(sorted(values, key=len, reverse=True))


def load_sensitive_log_values(root: Path) -> tuple[str, ...]:
    """Read only local protection values; never log these inputs or parse errors."""
    values: set[str] = set()
    try:
        for key, value in dotenv.dotenv_values(root / ".env").items():
            if value and any(
                word in key.lower() for word in ("key", "token", "password", "email", "secret")
            ):
                values.add(str(value))
    except Exception:
        pass
    try:
        source = yaml.safe_load((root / "data/resumes/structured_resume.yaml").read_text()) or {}
        personal = source.get("personal_information") or {}
        for key, value in personal.items():
            if any(
                word in key.lower()
                for word in (
                    "name",
                    "phone",
                    "email",
                    "address",
                    "birth",
                    "zip",
                    "linkedin",
                    "github",
                )
            ):
                if isinstance(value, (str, int)):
                    values.add(str(value))
    except Exception:
        pass
    try:
        profile = yaml.safe_load((root / "candidate_profile.yaml").read_text()) or {}
        values.update(profile_sensitive_values(profile))
    except Exception:
        pass
    return tuple(
        sorted((value for value in values if len(value.strip()) >= 3), key=len, reverse=True)
    )


def protect_log_record(record: dict, sensitive_values: tuple[str, ...] = ()) -> None:
    message = str(record["message"])
    for value in sensitive_values:
        message = re.sub(re.escape(value), REDACTED, message, flags=re.IGNORECASE)
    # Job and ATS URLs can carry tracking identifiers or session-bearing query
    # parameters. URLs are useful in persisted application records, but should
    # not be copied into the production log stream.
    message = re.sub(r"(?i)\bhttps?://[^\s<>\"']+", REDACTED, message)
    record["message"] = redact_text(message)
    # Exception formatting occurs after the patcher. Retain its class, never
    # its unfiltered message, traceback locals, or form/provider payloads.
    exception = record.get("exception")
    if exception:
        record["message"] += f" | exception_type={exception.type.__name__}"
        record["exception"] = None
