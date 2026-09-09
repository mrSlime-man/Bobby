"""Durable, privacy-safe application tracking for Bobby's operator GUI.

The dashboard event stream remains useful runtime telemetry, but this store is
the canonical long-lived application view.  Runtime events update it at the
point they are emitted; the GUI queries this structured store instead of
replaying logs on every refresh.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APPLICATION_STORE_PATH = (
    REPOSITORY_ROOT / "data" / "output" / "applications" / "applications.json"
)
STORE_VERSION = 1
NO_RESPONSE = "NO_RESPONSE"
EMPLOYER_RESPONDED = "EMPLOYER_RESPONDED"
INTERVIEW_REQUESTED = "INTERVIEW_REQUESTED"
REJECTED = "REJECTED"
OTHER_RESPONSE = "OTHER_RESPONSE"

DISCOVERY_STATUSES = {"DISCOVERED", "SKIPPED", "ADMITTED"}
APPLICATION_STATUSES = {
    "DISCOVERED",
    "SKIPPED",
    "ADMITTED",
    "IN_PROGRESS",
    "SUBMITTED",
    "UNVERIFIED_AFTER_SUBMIT",
    "TECHNICAL_FAILURE",
    "NEEDS_HUMAN",
    "NOT_ELIGIBLE",
    "CANCELLED",
    "DEFERRED_EASY_APPLY_LIMIT",
}
IRREVERSIBLE_STATUSES = {"SUBMITTED", "UNVERIFIED_AFTER_SUBMIT"}

EMAIL_VERIFICATION_STATES = {
    "NOT_REQUIRED",
    "WAITING_FOR_EMAIL",
    "EMAIL_FOUND",
    "VERIFICATION_LINK_OPENED",
    "CODE_ENTERED",
    "VERIFIED",
    "FAILED",
    "EXPIRED",
}

_SENSITIVE_QUERY_KEYS = re.compile(
    r"(?:token|auth|code|key|secret|signature|sig|nonce|session|state|redirect|oauth)",
    re.I,
)
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(?::\d+)?$")


def canonical_safe_url(value: Any) -> str:
    """Return a navigable HTTP(S) URL without credentials, query tokens, or fragments."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ""
        if parsed.scheme.casefold() not in {"http", "https"} or not host:
            return ""
        if parsed.username or parsed.password or not _HOST_RE.match(parsed.netloc):
            return ""
        port = ""
        try:
            if parsed.port is not None:
                port = f":{parsed.port}"
        except ValueError:
            return ""
        # Query strings are not needed to reopen a vacancy and often carry
        # redirect/session tokens.  Strip them all rather than guessing an
        # allowlist that could become stale as ATS providers change.
        return urlunsplit(
            (parsed.scheme.casefold(), host.casefold() + port, parsed.path or "/", "", "")
        )
    except (TypeError, ValueError):
        return ""


def _safe_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score != score or score in {float("inf"), float("-inf")}:
        return None
    return max(0.0, min(100.0, score))


def quality_tier(score: Any) -> str:
    """Map the 0–100 suitability score to the GUI's contractual color bands."""

    normalized = _safe_score(score)
    if normalized is None:
        return "NEUTRAL"
    if normalized < 50:
        return "NEUTRAL"
    if normalized < 60:
        return "RED"
    if normalized < 80:
        return "YELLOW"
    if normalized < 90:
        return "GREEN"
    return "CYAN"


def quality_label(score: Any) -> str:
    return {
        "NEUTRAL": "● Unscored",
        "RED": "● 50–59",
        "YELLOW": "● 60–79",
        "GREEN": "● 80–89",
        "CYAN": "● 90–100",
    }[quality_tier(score)]


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.fromtimestamp(float(text), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    parsed = _parse_timestamp(value)
    return parsed.isoformat(timespec="seconds") if parsed else None


def credible_application_timestamp(record: Mapping[str, Any]) -> datetime | None:
    """Prefer confirmed submission, then credible unverified-submit activity."""

    return _parse_timestamp(record.get("submitted_at")) or _parse_timestamp(
        record.get("credible_submission_at")
    )


def is_grey_lifecycle(record: Mapping[str, Any], now: Any = None) -> bool:
    """Whether a lifecycle overlay should mute a job without changing its tier."""

    employer = str(record.get("employer_response") or NO_RESPONSE).upper()
    if employer == REJECTED:
        return True
    if employer not in {NO_RESPONSE, ""}:
        return False
    if str(record.get("application_status") or "").upper() not in IRREVERSIBLE_STATUSES:
        return False
    applied_at = credible_application_timestamp(record)
    if applied_at is None:
        return False
    current = _parse_timestamp(now) or datetime.now(timezone.utc)
    # Calendar-day semantics are stable across DST changes and satisfy the
    # product rule that the sixth calendar day is the first stale day.
    return (current.date() - applied_at.date()).days > 5


def lifecycle_state(record: Mapping[str, Any], now: Any = None) -> str:
    employer = str(record.get("employer_response") or NO_RESPONSE).upper()
    if employer == REJECTED:
        return "REJECTED_GREY"
    if is_grey_lifecycle(record, now=now):
        return "STALE_NO_RESPONSE"
    return "ACTIVE"


def _record_key(payload: Mapping[str, Any]) -> str:
    explicit_key = str(payload.get("job_key") or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", explicit_key):
        return explicit_key
    explicit = str(
        payload.get("application_id")
        or payload.get("job_id")
        or payload.get("requisition_id")
        or ""
    ).strip()
    linkedin = canonical_safe_url(payload.get("linkedin_url"))
    external = canonical_safe_url(payload.get("external_url") or payload.get("url"))
    identity = explicit or linkedin or external
    if not identity:
        identity = "|".join(
            str(payload.get(key) or "").strip().casefold()
            for key in ("company_name", "job_title", "location")
        )
    return hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()


def _matching_record_key(
    jobs: Mapping[str, Any], payload: Mapping[str, Any], preferred_key: str
) -> str:
    """Resolve a later event to the existing application record when aliases differ."""

    if preferred_key in jobs:
        return preferred_key
    explicit_id = str(
        payload.get("application_id")
        or payload.get("job_id")
        or payload.get("requisition_id")
        or ""
    ).strip()
    linkedin = canonical_safe_url(payload.get("linkedin_url") or payload.get("url"))
    external = canonical_safe_url(payload.get("external_url"))
    for key, raw in jobs.items():
        if not isinstance(raw, Mapping):
            continue
        if explicit_id and explicit_id in {
            str(raw.get("application_id") or "").strip(),
            str(raw.get("job_id") or "").strip(),
            str(raw.get("requisition_id") or "").strip(),
        }:
            return key
        if linkedin and linkedin in {
            canonical_safe_url(raw.get("linkedin_url")),
            canonical_safe_url(raw.get("url")),
        }:
            return key
        if external and external in {
            canonical_safe_url(raw.get("external_url")),
            canonical_safe_url(raw.get("application_url")),
        }:
            return key
    return preferred_key


def _has_application_identity(jobs: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    """Return whether an event can be safely attached to an application row.

    Runtime-control telemetry intentionally uses opaque ``job:<digest>`` keys
    and is emitted alongside application events. Those keys are useful for
    aggregate run accounting, but they are not sufficient to create a durable
    application record. Accept an existing tracker key for alias-only updates
    (for example Gmail status events), while requiring a real id, URL, or
    recognizable company/title pair for new rows.
    """

    explicit_key = str(payload.get("job_key") or "").strip()
    if explicit_key and (explicit_key in jobs or re.fullmatch(r"[0-9a-f]{64}", explicit_key)):
        return True
    if any(
        str(payload.get(field) or "").strip()
        for field in ("application_id", "job_id", "requisition_id")
    ):
        return True
    if any(
        canonical_safe_url(payload.get(field))
        for field in ("linkedin_url", "external_url", "url")
    ):
        return True
    return bool(
        str(payload.get("company_name") or "").strip()
        and str(payload.get("job_title") or "").strip()
    )


def _failure_category(reason: Any, classification: str = "") -> str:
    """Return a stable, privacy-safe category for operator diagnostics."""

    text = str(reason or "").casefold()
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
    return "workflow_transition_failure" if classification else "unknown"


def _source_for(payload: Mapping[str, Any], external_url: str, linkedin_url: str) -> str:
    source = str(payload.get("source") or "").strip()
    if source:
        return source[:80]
    if linkedin_url and external_url:
        return "LinkedIn + External ATS"
    if linkedin_url:
        return "LinkedIn"
    if external_url:
        return "External ATS"
    return "Unknown"


def _default_record(key: str, payload: Mapping[str, Any], timestamp: Any) -> dict[str, Any]:
    raw_url = payload.get("external_url") or payload.get("url")
    candidate_url = canonical_safe_url(raw_url)
    is_linkedin = "linkedin.com" in (urlsplit(candidate_url).hostname or "").casefold()
    external_url = "" if is_linkedin else candidate_url
    linkedin_url = canonical_safe_url(payload.get("linkedin_url")) or (candidate_url if is_linkedin else "")
    score = _safe_score(payload.get("score", payload.get("interest_score")))
    discovered = _iso(payload.get("discovered_at") or timestamp)
    application_id = str(payload.get("application_id") or f"app-{key[:20]}")[:120]
    application_type = str(payload.get("application_type") or "UNKNOWN").upper()[:40]
    verification_required = payload.get("email_verification_required")
    verification_state = str(payload.get("email_verification_state") or "NOT_REQUIRED").upper()
    return {
        "job_key": key,
        "application_id": application_id,
        "job_id": str(payload.get("job_id") or "")[:120],
        "job_title": str(payload.get("job_title") or "Unknown job")[:240],
        "company_name": str(payload.get("company_name") or "Unknown company")[:180],
        "location": str(payload.get("location") or "")[:180],
        "source": _source_for(payload, external_url, linkedin_url),
        "ats_family": str(payload.get("ats") or payload.get("ats_family") or "")[:80],
        "application_type": application_type,
        "search_profile": str(payload.get("search_profile") or "")[:80],
        "remote_state": str(payload.get("remote_state") or "")[:40],
        "linkedin_url": linkedin_url,
        "external_url": external_url,
        "application_url": external_url or linkedin_url,
        "discovered_at": discovered,
        "attempted_at": _iso(payload.get("attempted_at")),
        "submitted_at": _iso(payload.get("submitted_at")),
        "credible_submission_at": _iso(payload.get("credible_submission_at")),
        "last_status_at": _iso(timestamp) or discovered,
        "suitability_score": score,
        "quality_tier": quality_tier(score),
        "suitability_reason": str(
            payload.get("reasoning") or payload.get("interest_reason") or ""
        )[:500],
        "application_status": "DISCOVERED",
        "result": "DISCOVERED",
        "last_workflow_step": str(payload.get("last_workflow_step") or "discovered")[:120],
        "failure_category": str(payload.get("failure_category") or "")[:80],
        "employer_response": NO_RESPONSE,
        "employer_response_at": None,
        "last_gmail_evidence": None,
        "failure_reason": "",
        "account_required": bool(payload.get("account_required", False)),
        "account_created": bool(payload.get("account_created", False)),
        "account_reused": bool(payload.get("account_reused", False)),
        "account_identifier": str(payload.get("account_identifier") or "")[:180],
        "account_email": str(payload.get("account_email") or payload.get("email") or "")[:180],
        "credential_ref": str(payload.get("credential_ref") or "")[:120],
        "email_verification_required": verification_required,
        "email_verification_state": verification_state if verification_state in EMAIL_VERIFICATION_STATES else "NOT_REQUIRED",
        "verification_event_id": str(payload.get("verification_event_id") or "")[:120],
        "verification_updated_at": _iso(payload.get("verification_updated_at") or timestamp),
        "run_id": str(payload.get("run_id") or "")[:120],
        "lifecycle_state": "ACTIVE",
        "duplicate_submit_protected": False,
        "discovery_reset_at": None,
    }


def _public_gmail_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "message_id": str(payload.get("message_id") or "")[:160],
        "kind": str(payload.get("kind") or payload.get("employer_response") or "OTHER_RESPONSE")[:60],
        "received_at": _iso(payload.get("received_at") or payload.get("timestamp")),
        "sender_domain": str(payload.get("sender_domain") or "")[:120],
        "confidence": round(max(0.0, min(1.0, float(payload.get("confidence", 0.0)))), 3),
    }


def _enrich_record(record: dict[str, Any], key: str) -> dict[str, Any]:
    """Backfill the current schema for records written by earlier Bobby builds."""

    defaults = {
        "application_id": f"app-{key[:20]}",
        "application_type": "UNKNOWN",
        "search_profile": "",
        "remote_state": "",
        "result": record.get("application_status") or "DISCOVERED",
        "last_workflow_step": record.get("application_status") or "discovered",
        "failure_category": "",
        "account_required": False,
        "account_created": False,
        "account_reused": False,
        "account_identifier": "",
        "account_email": "",
        "credential_ref": "",
        "email_verification_required": None,
        "email_verification_state": "NOT_REQUIRED",
        "verification_event_id": "",
        "verification_updated_at": None,
        "run_id": "",
    }
    for field, value in defaults.items():
        record.setdefault(field, value)
    return record


class ApplicationTracker:
    """Small atomic JSON repository for safe application lifecycle records."""

    def __init__(self, path: Path = APPLICATION_STORE_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {}
        if not isinstance(value, dict) or value.get("version") != STORE_VERSION:
            return {
                "version": STORE_VERSION,
                "jobs": {},
                "submission_ledger": {},
                "processed_gmail_message_ids": [],
                "last_gmail_status_scan_at": None,
            }
        value.setdefault("jobs", {})
        value.setdefault("submission_ledger", {})
        value.setdefault("processed_gmail_message_ids", [])
        value.setdefault("last_gmail_status_scan_at", None)
        return value

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            temporary = Path(name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def records(self, *, now: Any = None) -> list[dict[str, Any]]:
        with self._lock:
            data = self._load()
            rows = []
            changed = False
            for key, raw in data["jobs"].items():
                row = dict(raw)
                before = dict(row)
                _enrich_record(row, str(key))
                row["quality_tier"] = quality_tier(row.get("suitability_score"))
                row["lifecycle_state"] = lifecycle_state(row, now=now)
                if row != before:
                    data["jobs"][key] = row
                    changed = True
                rows.append(row)
            if changed:
                self._save(data)
            return rows

    def get(self, job_key: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._load()["jobs"].get(job_key)
            return _enrich_record(dict(record), job_key) if isinstance(record, dict) else None

    def apply_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        timestamp = event.get("timestamp") or payload.get("timestamp")
        with self._lock:
            data = self._load()
            if not _has_application_identity(data["jobs"], payload):
                return {}
            key = _matching_record_key(data["jobs"], payload, _record_key(payload))
            record = dict(data["jobs"].get(key) or _default_record(key, payload, timestamp))
            _enrich_record(record, key)
            for field in (
                "application_id",
                "job_id",
                "job_title",
                "company_name",
                "location",
                "source",
                "ats_family",
                "application_type",
                "search_profile",
                "remote_state",
                "last_workflow_step",
                "failure_category",
                "credential_ref",
                "account_identifier",
                "verification_event_id",
            ):
                value = payload.get(field)
                if value not in (None, ""):
                    record[field] = str(value)[:500 if field in {"last_workflow_step", "failure_category"} else 240]
            if event.get("run_id"):
                record["run_id"] = str(event.get("run_id"))[:120]
            if payload.get("account_email") or payload.get("email"):
                record["account_email"] = str(
                    payload.get("account_email") or payload.get("email")
                )[:180]
            if payload.get("account_identifier"):
                record["account_identifier"] = str(payload.get("account_identifier"))[:180]
            for field in (
                "account_required",
                "account_created",
                "account_reused",
                "email_verification_required",
            ):
                if field in payload and payload.get(field) is not None:
                    record[field] = bool(payload.get(field))
            for field, source in (("linkedin_url", "linkedin_url"), ("external_url", "external_url"), ("external_url", "url")):
                safe = canonical_safe_url(payload.get(source))
                if safe:
                    host = (urlsplit(safe).hostname or "").casefold()
                    if source == "url" and "linkedin.com" in host:
                        record["linkedin_url"] = safe
                    else:
                        record[field] = safe
            record["application_url"] = record.get("external_url") or record.get("linkedin_url")
            if payload.get("application_type"):
                record["application_type"] = str(payload["application_type"]).upper()[:40]
            elif record.get("application_type", "UNKNOWN") == "UNKNOWN":
                if payload.get("external_url") or payload.get("ats") or event.get("type") in {
                    "agent_apply_started",
                    "ats_workflow_step",
                }:
                    record["application_type"] = "EXTERNAL_ATS"
                elif event.get("type") in {"easy_apply_started", "easy_apply_completed"}:
                    record["application_type"] = "EASY_APPLY"
            if payload.get("remote_state") is None:
                states = [
                    label
                    for label, key_name in (("REMOTE", "remote"), ("HYBRID", "hybrid"), ("ON_SITE", "onsite"))
                    if payload.get(key_name) is True
                ]
                if states:
                    record["remote_state"] = "/".join(states)
            if payload.get("score") is not None or payload.get("interest_score") is not None:
                record["suitability_score"] = _safe_score(
                    payload.get("score", payload.get("interest_score"))
                )
                record["quality_tier"] = quality_tier(record["suitability_score"])
            if payload.get("reasoning") or payload.get("interest_reason"):
                record["suitability_reason"] = str(
                    payload.get("reasoning") or payload.get("interest_reason")
                )[:500]
            event_type = str(event.get("type") or "")
            if event_type in {"job_discovered", "job_loaded", "job_evaluation_started"}:
                record["application_status"] = "DISCOVERED"
                record["result"] = "DISCOVERED"
                record["discovered_at"] = record.get("discovered_at") or _iso(timestamp)
            elif event_type == "job_evaluated":
                record["application_status"] = "ADMITTED" if payload.get("interesting") else "SKIPPED"
                record["result"] = record["application_status"]
                if not payload.get("interesting"):
                    record["failure_reason"] = str(payload.get("reasoning") or "Not admitted by suitability rules")[:500]
            elif event_type in {"job_application_started", "easy_apply_started", "agent_apply_started"}:
                record["application_status"] = "IN_PROGRESS"
                record["result"] = "IN_PROGRESS"
                record["attempted_at"] = record.get("attempted_at") or _iso(timestamp)
                record["last_workflow_step"] = str(
                    payload.get("last_workflow_step") or payload.get("stage") or event_type
                )[:120]
            elif event_type in {"ats_workflow_step", "workflow_step", "application_progress"}:
                record["application_status"] = "IN_PROGRESS"
                record["result"] = "IN_PROGRESS"
                record["attempted_at"] = record.get("attempted_at") or _iso(timestamp)
                record["last_workflow_step"] = str(
                    payload.get("last_workflow_step") or payload.get("step") or payload.get("stage") or event_type
                )[:120]
            elif event_type in {"account_required", "account_created", "account_reused", "ats_account_update"}:
                record["account_required"] = True
                record["account_created"] = bool(
                    payload.get("account_created", event_type == "account_created")
                ) or record.get("account_created", False)
                record["account_reused"] = bool(
                    payload.get("account_reused", event_type == "account_reused")
                ) or record.get("account_reused", False)
                record["last_workflow_step"] = str(
                    payload.get("last_workflow_step") or event_type
                )[:120]
            elif event_type in {
                "email_verification_required",
                "email_verification_waiting",
                "email_verification_received",
                "email_verification_opened",
                "email_verification_code_entered",
                "email_verification_confirmed",
                "email_verification_failed",
            }:
                record["email_verification_required"] = True
                state = str(
                    payload.get("email_verification_state")
                    or payload.get("verification_state")
                    or event_type.removeprefix("email_verification_")
                ).replace("-", "_").replace(" ", "_").upper()
                aliases = {
                    "REQUIRED": "WAITING_FOR_EMAIL",
                    "WAITING": "WAITING_FOR_EMAIL",
                    "RECEIVED": "EMAIL_FOUND",
                    "OPENED": "VERIFICATION_LINK_OPENED",
                    "CODE_ENTERED": "CODE_ENTERED",
                    "CONFIRMED": "VERIFIED",
                }
                state = aliases.get(state, state)
                record["email_verification_state"] = (
                    state if state in EMAIL_VERIFICATION_STATES else "FAILED"
                )
                record["verification_event_id"] = str(
                    payload.get("verification_event_id") or record.get("verification_event_id") or ""
                )[:120]
                record["verification_updated_at"] = _iso(timestamp) or record.get(
                    "verification_updated_at"
                )
                record["last_workflow_step"] = str(
                    payload.get("last_workflow_step") or event_type
                )[:120]
            elif event_type in {"gmail_status_update", "employer_response"}:
                response = str(payload.get("employer_response") or payload.get("kind") or OTHER_RESPONSE).upper()
                if response in {"INTERVIEW", "INTERVIEW_REQUESTED", "RECRUITER_RESPONSE", "EMPLOYER_RESPONDED"}:
                    response = INTERVIEW_REQUESTED if "INTERVIEW" in response else EMPLOYER_RESPONDED
                elif response not in {REJECTED, OTHER_RESPONSE}:
                    response = OTHER_RESPONSE
                record["employer_response"] = response
                record["employer_response_at"] = _iso(timestamp or payload.get("received_at"))
                record["last_gmail_evidence"] = _public_gmail_evidence(payload)
            elif event_type == "job_result":
                classification = str(payload.get("classification") or "").upper()
                result = str(payload.get("result") or "").casefold()
                if not classification:
                    classification = "SUBMITTED" if result == "success" else "SKIPPED" if result == "skip" else "TECHNICAL_FAILURE"
                if classification == "CANCELLED_BY_SHUTDOWN":
                    classification = "CANCELLED"
                if classification not in APPLICATION_STATUSES:
                    classification = "TECHNICAL_FAILURE"
                # Once the final submit boundary is protected, a late or
                # duplicated pre-submit event may not downgrade the durable row.
                if not record.get("duplicate_submit_protected") or classification in IRREVERSIBLE_STATUSES:
                    record["application_status"] = classification
                record["result"] = classification
                supplied_failure_category = payload.get("failure_category")
                if supplied_failure_category:
                    record["failure_category"] = str(supplied_failure_category)[:80]
                elif classification in {
                    "TECHNICAL_FAILURE",
                    "NEEDS_HUMAN",
                    "UNVERIFIED_AFTER_SUBMIT",
                }:
                    record["failure_category"] = _failure_category(
                        payload.get("reason"), classification
                    )[:80]
                else:
                    # Cancellation, skip, and ordinary discovery outcomes are
                    # not failures and should not appear in the failure view.
                    record["failure_category"] = ""
                record["last_workflow_step"] = str(
                    payload.get("last_workflow_step") or payload.get("stage") or classification
                )[:120]
                record["last_status_at"] = _iso(timestamp) or record.get("last_status_at")
                if classification in {"IN_PROGRESS", "SUBMITTED", "UNVERIFIED_AFTER_SUBMIT"}:
                    record["attempted_at"] = record.get("attempted_at") or _iso(timestamp)
                if classification == "SUBMITTED":
                    record["submitted_at"] = _iso(timestamp) or record.get("submitted_at")
                    record["credible_submission_at"] = record.get("submitted_at")
                elif classification == "UNVERIFIED_AFTER_SUBMIT":
                    record["credible_submission_at"] = _iso(timestamp) or record.get("credible_submission_at")
                if payload.get("reason"):
                    record["failure_reason"] = str(payload.get("reason"))[:500]
                if classification in IRREVERSIBLE_STATUSES:
                    record["duplicate_submit_protected"] = True
                    data["submission_ledger"][key] = {
                        "job_key": key,
                        "first_irreversible_at": record.get("credible_submission_at"),
                        "status": classification,
                    }
            if timestamp:
                record["last_status_at"] = _iso(timestamp) or record.get("last_status_at")
            record["quality_tier"] = quality_tier(record.get("suitability_score"))
            record["lifecycle_state"] = lifecycle_state(record)
            data["jobs"][key] = record
            self._save(data)
            return dict(record)

    def mark_gmail_processed(self, message_ids: Iterable[str], scanned_at: Any) -> None:
        with self._lock:
            data = self._load()
            existing = list(data.get("processed_gmail_message_ids") or [])
            for message_id in message_ids:
                value = str(message_id or "")
                if value and value not in existing:
                    existing.append(value)
            data["processed_gmail_message_ids"] = existing[-5000:]
            data["last_gmail_status_scan_at"] = _iso(scanned_at)
            self._save(data)

    def gmail_state(self) -> dict[str, Any]:
        with self._lock:
            data = self._load()
            return {
                "processed_message_ids": set(data.get("processed_gmail_message_ids") or []),
                "last_scan_at": data.get("last_gmail_status_scan_at"),
            }

    def reset_discovery(self, *, skipped_only: bool = False, reset_at: Any = None) -> dict[str, int]:
        """Reset reconsideration state while retaining the durable app ledger."""

        with self._lock:
            data = self._load()
            resettable = 0
            protected = 0
            stamp = _iso(reset_at) or datetime.now(timezone.utc).isoformat(timespec="seconds")
            for record in data["jobs"].values():
                status = str(record.get("application_status") or "")
                if status in IRREVERSIBLE_STATUSES or record.get("duplicate_submit_protected"):
                    protected += 1
                    continue
                if skipped_only and status != "SKIPPED":
                    continue
                if status in {
                    "SKIPPED",
                    "TECHNICAL_FAILURE",
                    "NOT_ELIGIBLE",
                    "DEFERRED_EASY_APPLY_LIMIT",
                    "DISCOVERED",
                    "ADMITTED",
                }:
                    record["discovery_reset_at"] = stamp
                    record["application_status"] = "DISCOVERED"
                    record["failure_reason"] = ""
                    resettable += 1
            self._save(data)
            return {"resettable": resettable, "protected": protected}


def safe_tracker_event(event_type: str, **payload: Any) -> dict[str, Any]:
    """Convenience for tests and integrations that need a canonical event shape."""

    return {"type": event_type, "timestamp": datetime.now(timezone.utc).isoformat(), "payload": payload}
