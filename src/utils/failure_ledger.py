"""Durable, privacy-safe production failure ledger for Bobby.

The runtime event stream is append-only telemetry.  This module keeps a small
operator-facing index of terminal problems so a later session can continue an
audit without guessing from log text or replaying a protected application.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
FAILURE_DIR = ROOT / "data" / "output" / "failures"
FAILURE_LEDGER_PATH = FAILURE_DIR / "failure_ledger.json"
FAILURE_LEDGER_MARKDOWN_PATH = FAILURE_DIR / "FAILURE_LEDGER.md"
_LOCK = threading.RLock()


def _safe_text(value: Any, limit: int = 500) -> str:
    text = str(value or "").replace("\x00", " ").strip()
    # The ledger is written for humans and must not accidentally render a
    # secret-like multiline value as a new field or executable Markdown.
    return re.sub(r"[\r\n\t]+", " ", text)[:limit]


def _safe_url_ref(value: Any) -> str:
    raw = _safe_text(value, 1000)
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    # Query parameters and fragments may carry session material.  The host and
    # path are enough to identify the reproduction surface.
    return f"{parsed.scheme.casefold()}://{parsed.hostname.casefold()}{parsed.path or '/'}"


def classify_failure_category(reason: Any, classification: str = "") -> str:
    text = _safe_text(reason, 1000).casefold()
    if any(term in text for term in ("captcha", "mfa", "multi-factor", "two-factor", "security verification")):
        return "captcha_or_bot_protection"
    if "upload" in text or "resume" in text:
        return "upload_failure"
    if "provider" in text or "llm" in text or "quota" in text:
        return "provider_failure"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "registration" in text or "account" in text:
        return "registration_failure"
    if "email" in text or "activation" in text or "verification" in text:
        return "email_verification_failure"
    if "validation" in text or "required" in text:
        return "validation_failure"
    if "selector" in text or "element" in text or "control" in text:
        return "selector_failure"
    if "navigation" in text or "url" in text:
        return "navigation_failure"
    if classification == "UNVERIFIED_AFTER_SUBMIT":
        return "confirmation_detection_failure"
    return "workflow_transition_failure"


def _control_classification(
    classification: str,
    reason: str,
    *,
    application_type: str = "",
    workflow: str = "",
    failure_category: str = "",
) -> str:
    text = reason.casefold()
    if (
        classification == "NEEDS_HUMAN"
        and str(application_type).casefold() == "easy_apply"
        and str(workflow).casefold() == "easy_apply"
        and str(failure_category).casefold() in {"workflow_transition_failure", "selector_failure"}
    ):
        # LinkedIn owns this UI surface and Bobby intentionally stops before
        # submit when a visible Easy Apply control cannot be resolved safely.
        # Keep that human boundary distinct from a Bobby technical defect.
        return "HUMAN_BOUNDARY"
    external_terms = (
        "captcha",
        "mfa",
        "multi-factor",
        "two-factor",
        "security verification",
        "hardware security",
        "job was removed",
        "site unavailable",
        "ats outage",
        "gmail integration unavailable",
        "gmail readonly verification is unavailable",
        "gmail oauth",
        "oauth client",
        "external blocker",
        "external provider unavailable",
        "provider was unavailable",
        "provider circuit is open",
    )
    if classification == "NEEDS_HUMAN" and any(term in text for term in external_terms):
        return "EXTERNAL_BLOCKER"
    if classification == "UNVERIFIED_AFTER_SUBMIT":
        return "UNCERTAIN_EXTERNAL_STATE"
    return "BOBBY_CONTROLLED"


def _read() -> dict[str, Any]:
    try:
        value = json.loads(FAILURE_LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    failures = value.get("failures")
    return {"version": 1, "failures": failures if isinstance(failures, list) else []}


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _write_markdown(failures: list[Mapping[str, Any]]) -> None:
    lines = [
        "# Bobby failure ledger",
        "",
        "This file is generated from `failure_ledger.json`; secrets and raw page bodies are excluded.",
        "",
    ]
    if not failures:
        lines.append("No terminal failures recorded.")
    for failure in sorted(failures, key=lambda item: str(item.get("timestamp") or ""), reverse=True):
        lines.extend(
            [
                f"## {failure.get('failure_id', 'unknown')}",
                "",
                f"- Time: {failure.get('timestamp', 'unknown')}",
                f"- Run: {failure.get('run_id', 'unknown')}",
                f"- Application: {failure.get('application_id', 'unknown')}",
                f"- Company / job: {failure.get('company', 'unknown')} / {failure.get('job_title', 'unknown')}",
                f"- Category: `{failure.get('failure_category', 'unknown')}`",
                f"- Classification: `{failure.get('classification', 'unknown')}`",
                f"- Control: `{failure.get('control_classification', 'unknown')}`",
                f"- Root cause: {failure.get('root_cause', 'not diagnosed')}",
                f"- Reason: {failure.get('reason', 'not recorded')}",
                f"- Reproduction evidence: {', '.join(failure.get('evidence_refs') or []) or 'none recorded'}",
                f"- Fix status: {failure.get('fix_status', 'open')}",
                f"- Production validation: {failure.get('production_validation_status', 'not validated')}",
                f"- Recurrence count: {failure.get('recurrence_count', 1)}",
                "",
            ]
        )
    _atomic_write(FAILURE_LEDGER_MARKDOWN_PATH, "\n".join(lines) + "\n")


def record_failure_event(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """Record a terminal failure once, returning the redacted ledger entry."""

    if str(event.get("type") or "") != "job_result":
        return None
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    classification = _safe_text(payload.get("classification"), 80).upper()
    if classification not in {"TECHNICAL_FAILURE", "NEEDS_HUMAN", "UNVERIFIED_AFTER_SUBMIT"}:
        return None
    reason = _safe_text(payload.get("reason"), 1000)
    category = _safe_text(
        payload.get("failure_category") or classify_failure_category(reason, classification), 100
    )
    application_id = _safe_text(
        payload.get("application_id") or payload.get("job_id") or payload.get("url") or "unknown",
        160,
    )
    timestamp = _safe_text(event.get("timestamp") or datetime.now(timezone.utc).isoformat(), 80)
    run_id = _safe_text(event.get("run_id") or payload.get("run_id") or "unknown", 120)
    evidence_refs = [
        _safe_text(payload.get(key), 240)
        for key in ("diagnostic_ref", "artifact_ref", "screenshot_ref", "worker_state_ref")
        if payload.get(key)
    ]
    root_cause = _safe_text(payload.get("root_cause") or "Pending first-incorrect-transition audit", 500)
    recurrence_key = hashlib.sha256(
        f"{category}|{root_cause}|{_safe_text(payload.get('ats') or payload.get('ats_family'), 80)}".encode()
    ).hexdigest()[:20]
    failure_id = "failure-" + hashlib.sha256(
        f"{run_id}|{application_id}|{timestamp}|{category}".encode()
    ).hexdigest()[:20]
    entry = {
        "failure_id": failure_id,
        "run_id": run_id,
        "application_id": application_id,
        "company": _safe_text(payload.get("company_name"), 180),
        "job_title": _safe_text(payload.get("job_title"), 240),
        "job_url": _safe_url_ref(payload.get("external_url") or payload.get("url")),
        "ats": _safe_text(payload.get("ats") or payload.get("ats_family"), 80),
        "timestamp": timestamp,
        "failure_category": category,
        "classification": classification,
        "control_classification": _control_classification(
            classification,
            reason,
            application_type=_safe_text(payload.get("application_type"), 80),
            workflow=_safe_text(payload.get("workflow"), 80),
            failure_category=category,
        ),
        "root_cause": root_cause,
        "reason": reason,
        "evidence_refs": evidence_refs,
        "code_changed": _safe_text(payload.get("code_changed"), 500),
        "test_added": _safe_text(payload.get("test_added"), 500),
        "fix_status": _safe_text(payload.get("fix_status") or "open", 80),
        "production_validation_status": _safe_text(
            payload.get("production_validation_status") or "not validated", 120
        ),
        "recurrence_key": recurrence_key,
        "recurrence_count": 1,
    }
    with _LOCK:
        data = _read()
        failures = data["failures"]
        prior = next((item for item in failures if item.get("recurrence_key") == recurrence_key), None)
        if prior is not None:
            entry["recurrence_count"] = int(prior.get("recurrence_count") or 1) + 1
        failures.append(entry)
        _atomic_write(FAILURE_LEDGER_PATH, data)
        _write_markdown(failures)
    return dict(entry)


def annotate_failure(
    failure_id: str,
    *,
    root_cause: str | None = None,
    control_classification: str | None = None,
    evidence_refs: list[str] | None = None,
    code_changed: str | None = None,
    test_added: str | None = None,
    fix_status: str | None = None,
    production_validation_status: str | None = None,
) -> dict[str, Any] | None:
    """Update one audited failure without changing its identity or recurrence."""

    fields = {
        "root_cause": root_cause,
        "control_classification": control_classification,
        "evidence_refs": evidence_refs,
        "code_changed": code_changed,
        "test_added": test_added,
        "fix_status": fix_status,
        "production_validation_status": production_validation_status,
    }
    with _LOCK:
        data = _read()
        target = next(
            (item for item in data["failures"] if item.get("failure_id") == str(failure_id)),
            None,
        )
        if target is None:
            return None
        for field, value in fields.items():
            if value is None:
                continue
            if field == "evidence_refs":
                target[field] = [_safe_text(item, 240) for item in value if item]
            else:
                target[field] = _safe_text(value, 500 if field != "fix_status" else 80)
        _atomic_write(FAILURE_LEDGER_PATH, data)
        _write_markdown(data["failures"])
        return dict(target)


def failure_ledger_summary() -> dict[str, Any]:
    with _LOCK:
        failures = _read()["failures"]
    return {
        "total": len(failures),
        "bobby_controlled": sum(
            item.get("control_classification") == "BOBBY_CONTROLLED" for item in failures
        ),
        "external_blockers": sum(
            item.get("control_classification") == "EXTERNAL_BLOCKER" for item in failures
        ),
        "human_boundaries": sum(
            item.get("control_classification") == "HUMAN_BOUNDARY" for item in failures
        ),
        "open": sum(item.get("fix_status") == "open" for item in failures),
    }
