import base64
import asyncio
import fcntl
import hashlib
import json
import os
import re
import secrets
import string
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse, urlsplit

from src.utils.redaction import redact_text
from src.utils.log_privacy import load_sensitive_log_values
from src.utils.run_context import get_run_id
from src.llm.ats_engine import (
    ATSStage,
    ControlDescriptor,
    FieldConstraint,
    SiteFamily,
    detect_site_family,
    discover_controls,
    infer_stage,
    navigation_intent,
    page_fingerprint,
    rank_application_entry_candidates,
    validation_repair_plan,
)

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = ROOT / "data/output/external_evidence"
ANALYTICS_DIR = ROOT / "data/output/external_analytics"
MEMORY_DIR = ROOT / "data/output/external_memory"
ACCOUNTS_DIR = Path.home() / ".config/job-bot"
# The historical filename is retained so existing deployments migrate in
# place.  Its contents are now a Fernet envelope, never a JSON password map.
ACCOUNTS_FILE = ACCOUNTS_DIR / "ats_accounts.json"


def _accounts_key_file() -> Path:
    return ACCOUNTS_FILE.with_name(f"{ACCOUNTS_FILE.stem}.key")


def _secure_directory(directory: Path) -> None:
    """Create and retain private artifact directories without deleting history."""

    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass


def _secure_existing_artifacts(directory: Path) -> None:
    """Tighten existing local artifact modes while preserving every record."""

    _secure_directory(directory)
    try:
        artifacts = tuple(directory.rglob("*"))
    except OSError:
        return
    for artifact in artifacts:
        try:
            artifact.chmod(0o700 if artifact.is_dir() else 0o600)
        except OSError:
            continue


for _artifact_directory in (EVIDENCE_DIR, ANALYTICS_DIR, MEMORY_DIR):
    _secure_existing_artifacts(_artifact_directory)
_secure_directory(ACCOUNTS_DIR)


def _write_private_bytes(path: Path, data: bytes) -> None:
    """Atomically write a 0600 artifact, including when the umask is permissive."""

    _secure_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as handle:
            temporary.chmod(0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


SUBMITTED = "SUBMITTED"
UNVERIFIED = "UNVERIFIED"
NEEDS_HUMAN = "NEEDS_HUMAN"
FAILED = "FAILED"
NOT_ELIGIBLE = "NOT_ELIGIBLE"

CONFIRMATION_PHRASES = (
    "thank you for applying",
    "thanks for applying",
    "thank you for your application",
    "thank you for submitting",
    "thanks for submitting",
    "thank you for your submission",
    "application submitted",
    "application successfully submitted",
    "application has been submitted",
    "your application has been submitted",
    "application was submitted",
    "your application was submitted",
    "application received",
    "we received your application",
    "we have received your application",
    "your submission has been received",
    "application successfully received",
    "successfully applied",
    "application complete",
    "your application is complete",
    "you've applied",
    "you have applied",
)

ERROR_PHRASES = (
    "please complete",
    "required field",
    "this field is required",
    "please enter",
    "please select",
    "invalid value",
    "invalid email",
    "invalid phone",
    "please correct",
    "missing required",
)

HUMAN_PHRASES = (
    "captcha",
    "cloudflare",
    "verify you are human",
    "security verification",
    "security challenge",
    # Exact external WAF block semantics, not ordinary application prose.
    "unauthorized activity detected",
    "unauthorized request blocked",
    "device verification",
    "email verification",
    "verify your email",
    "confirmation code",
    "verification code",
    "one-time code",
    "one time code",
    "magic link",
    "multi-factor authentication",
    "multifactor authentication",
    "two-factor authentication",
    "two factor authentication",
    "mfa",
    "otp",
    "2fa",
)

ATS_PATTERNS = {
    "workday": ("myworkdayjobs.com", "workday.com", "workdayjobs.com"),
    "greenhouse": ("greenhouse.io", "boards.greenhouse.io"),
    "lever": ("lever.co", "jobs.lever.co"),
    "ashby": ("ashbyhq.com", "jobs.ashbyhq.com"),
    "icims": ("icims.com",),
    "smartrecruiters": ("smartrecruiters.com",),
    "taleo": ("taleo.net", "oraclecloud.com"),
    "bamboohr": ("bamboohr.com",),
    "jobvite": ("jobvite.com",),
}

ATS_HINTS = {
    "workday": """
WORKDAY RULES:
- Workday usually has multiple application pages.
- Re-scan every page after Next.
- Select a real autocomplete suggestion for location/address fields.
- Do not assume resume parsing filled fields correctly.
- Check every required field before continuing.
- Do not treat account creation as submission.
""",
    "greenhouse": """
GREENHOUSE RULES:
- Check required text fields and custom questions carefully.
- Resume upload may not populate every field.
- Check EEO/demographic questions separately.
- Wait for explicit post-submit confirmation.
""",
    "lever": """
LEVER RULES:
- Fill contact fields and attachments carefully.
- Check custom screening questions near the bottom.
- Wait for explicit post-submit confirmation.
""",
    "ashby": """
ASHBY RULES:
- Forms can reveal new fields dynamically.
- Re-scan after each selection.
- Resolve validation errors before retrying Submit.
""",
    "icims": """
ICIMS RULES:
- Account/login flows are common.
- Account creation is NOT an application submission.
- Continue until explicit submission confirmation.
""",
    "smartrecruiters": """
SMARTRECRUITERS RULES:
- Verify resume upload and parsed fields.
- Some sections reveal after previous sections are complete.
- Wait for explicit post-submit confirmation.
""",
}


def normalize_text(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def detect_ats(url: str) -> str:
    family = detect_site_family(url)
    if family is not SiteFamily.GENERIC:
        return family.value
    host = urlparse(str(url)).netloc.lower()
    for ats, patterns in ATS_PATTERNS.items():
        if any(pattern in host for pattern in patterns):
            return ats
    return "generic"


def ats_prompt(url: str) -> str:
    return ATS_HINTS.get(detect_ats(url), "")


def detect_confirmation(body_text: str, evidence_text: str = ""):
    body = normalize_text(body_text)
    evidence = normalize_text(evidence_text)
    for phrase in CONFIRMATION_PHRASES:
        if phrase in body:
            return True, phrase
    if evidence:
        looks_valid = (
            (
                "application" in evidence
                and any(
                    word in evidence
                    for word in (
                        "submitted",
                        "received",
                        "complete",
                        "successful",
                        "successfully",
                        "confirmed",
                    )
                )
            )
            or "thank you for applying" in evidence
            or "thank you for submitting" in evidence
            or "thanks for applying" in evidence
            or "thanks for submitting" in evidence
            or "you've applied" in evidence
            or "you have applied" in evidence
        )
        if looks_valid and evidence in body:
            return True, evidence_text.strip()
    return False, ""


def find_validation_errors(body_text: str):
    body = normalize_text(body_text)
    return [phrase for phrase in ERROR_PHRASES if phrase in body]


def needs_human(body_text: str):
    body = normalize_text(body_text)
    for phrase in HUMAN_PHRASES:
        # Short acronyms must be whole words: treating arbitrary substrings as
        # OTP/2FA would turn ordinary content into a false security challenge.
        found = (
            re.search(rf"\b{re.escape(phrase)}\b", body) is not None
            if len(phrase) <= 3
            else phrase in body
        )
        if found:
            return True, phrase
    return False, ""


def needs_human_from_visible_controls(controls: object):
    """Detect a visible security boundary from safe control semantics only.

    Some ATS CAPTCHA widgets expose their warning solely through a button's
    title or accessible label, rather than body text.  Inspecting those labels
    lets Bobby stop for a human without reading field values or interacting
    with the challenge.
    """

    labels: list[str] = []
    for control in controls if isinstance(controls, (list, tuple)) else ():
        if not isinstance(control, dict) or control.get("visible") is False:
            continue
        labels.extend(
            str(control.get(key) or "")
            for key in ("label", "aria_label", "title", "name", "id", "type")
        )
    return needs_human(" ".join(labels))


@dataclass(frozen=True)
class SubmissionEvidence:
    """Private, in-memory result of the bounded post-submit verifier.

    ``body_text`` and ``url`` are deliberately never written by this helper.
    Callers may use them for the existing redacted evidence path only.
    """

    verified: bool = False
    confirmation: str = ""
    source: str = ""
    body_text: str = ""
    url: str = ""
    human_reason: str = ""
    validation_errors: tuple[str, ...] = ()
    error_class: str = ""


async def collect_post_submit_evidence(
    browser_session: Any,
    *,
    evidence_text: str = "",
    attempts: int = 3,
    poll_interval_seconds: float = 4.0,
    receipt_checker: Callable[[], Awaitable[str | None]] | None = None,
) -> SubmissionEvidence:
    """Read independent post-submit evidence without taking another action.

    This path is intentionally usable when an LLM call has failed after the
    durable final-submit marker.  It never clicks, uploads, changes providers,
    or navigates.  Gmail, when supplied, is receipt-only and runs after the
    bounded live-page checks.
    """

    body_text = ""
    url = ""
    try:
        for index in range(max(1, int(attempts))):
            if index:
                await asyncio.sleep(max(0.0, float(poll_interval_seconds)))
            page = await browser_session.must_get_current_page()
            body_text = str(
                await page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            )
            url = str(await page.get_url() or "")
            human_required, human_reason = needs_human(body_text)
            if human_required:
                return SubmissionEvidence(
                    body_text=body_text,
                    url=url,
                    human_reason=human_reason,
                )
            verified, confirmation = detect_confirmation(body_text, evidence_text)
            if verified:
                return SubmissionEvidence(
                    verified=True,
                    confirmation=confirmation,
                    source="page_dom",
                    body_text=body_text,
                    url=url,
                )

        validation_errors = tuple(find_validation_errors(body_text))
        if validation_errors:
            return SubmissionEvidence(
                body_text=body_text,
                url=url,
                validation_errors=validation_errors,
            )

        if receipt_checker is not None:
            receipt = await receipt_checker()
            if receipt:
                return SubmissionEvidence(
                    verified=True,
                    confirmation=str(receipt),
                    source="email_receipt",
                    body_text=body_text,
                    url=url,
                )

        return SubmissionEvidence(body_text=body_text, url=url)
    except Exception as exc:
        # Preserve the error class only.  Browser/page exception text can carry
        # form values or session-bearing URLs.
        return SubmissionEvidence(
            body_text=body_text,
            url=url,
            error_class=type(exc).__name__,
        )


def company_tokens(company: str):
    stop = {
        "inc",
        "llc",
        "ltd",
        "corp",
        "corporation",
        "company",
        "co",
        "the",
        "group",
        "solutions",
        "technologies",
        "technology",
        "jobs",
        "careers",
        "career",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", normalize_text(company))
        if token not in stop and len(token) >= 3
    }


def company_match(linkedin_company: str, external_company_hint: str):
    if not linkedin_company or not external_company_hint:
        return None
    a = company_tokens(linkedin_company)
    b = company_tokens(external_company_hint)
    if not a or not b:
        return None
    return bool(a & b)


def safe_job_id(url: str):
    raw_url = str(url or "")
    try:
        parts = urlsplit(raw_url)
        # Evidence paths are identifiers, not URLs.  Query/fragment values
        # can contain provider state or tokens and must never reach disk.
        identifier_source = (
            f"{parts.scheme}://{parts.netloc}{parts.path}"
            if parts.scheme or parts.netloc
            else parts.path
        )
    except Exception:
        identifier_source = raw_url.split("?", 1)[0].split("#", 1)[0]
    match = re.search(r"/jobs/view/(\d+)", identifier_source)
    if match:
        return match.group(1)
    return re.sub(r"[^a-zA-Z0-9]+", "_", identifier_source)[-80:]


def evidence_path(job_id: str):
    _secure_directory(EVIDENCE_DIR)
    folder = EVIDENCE_DIR / str(job_id)
    _secure_directory(folder)
    return folder


def _safe_landing_url(url: str) -> dict[str, str]:
    """Keep a replayable origin/path while dropping every query value."""
    try:
        parts = urlsplit(str(url or ""))
        host = str(parts.hostname or "").casefold()
        scheme = str(parts.scheme or "").casefold()
        origin = f"{scheme}://{host}" if scheme in {"http", "https"} and host else ""
        return {
            "origin": origin,
            "path": _safe_landing_text(parts.path or "/", limit=240),
        }
    except Exception:
        return {"origin": "", "path": ""}


def _safe_landing_text(value: object, *, limit: int) -> str:
    """Redact configured candidate values before a diagnostic reaches disk."""
    text = redact_text(value, limit=limit)
    for sensitive_value in load_sensitive_log_values(ROOT):
        text = re.sub(re.escape(sensitive_value), "[REDACTED]", text, flags=re.IGNORECASE)
    return text[:limit]


def _safe_landing_controls(controls: object) -> list[dict[str, object]]:
    """Persist only semantic entry-control metadata, never populated values."""
    safe_controls: list[dict[str, object]] = []
    if not isinstance(controls, (list, tuple)):
        return safe_controls
    for raw in controls[:160]:
        if not isinstance(raw, dict):
            continue
        safe_controls.append(
            {
                "label": _safe_landing_text(raw.get("label", ""), limit=180),
                "aria_label": _safe_landing_text(raw.get("aria_label", ""), limit=180),
                "title": _safe_landing_text(raw.get("title", ""), limit=180),
                "kind": redact_text(raw.get("kind", ""), limit=40),
                "role": redact_text(raw.get("role", ""), limit=40),
                "href": _safe_landing_url(str(raw.get("href", "") or "")),
                "visible": bool(raw.get("visible")),
            }
        )
    return safe_controls


def _safe_landing_form_fields(fields: object) -> list[dict[str, object]]:
    """Persist only form semantics and state booleans, never candidate values."""

    safe_fields: list[dict[str, object]] = []
    if not isinstance(fields, (list, tuple)):
        return safe_fields
    for raw in fields[:160]:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind", "") or "").upper()
        input_type = str(raw.get("input_type", "") or "").casefold()
        if kind not in {"INPUT", "SELECT", "TEXTAREA", "CHOICE_GROUP"}:
            continue
        if not re.fullmatch(r"[a-z0-9_-]{0,40}", input_type):
            input_type = ""
        try:
            option_count = min(500, max(0, int(raw.get("option_count", 0))))
        except (TypeError, ValueError):
            option_count = 0
        safe_fields.append(
            {
                "label": _safe_landing_text(raw.get("label", ""), limit=180),
                "aria_label": _safe_landing_text(raw.get("aria_label", ""), limit=180),
                "placeholder": _safe_landing_text(raw.get("placeholder", ""), limit=120),
                "kind": kind,
                "input_type": input_type,
                "required": bool(raw.get("required")),
                "enabled": bool(raw.get("enabled")),
                "read_only": bool(raw.get("read_only")),
                "has_value": bool(raw.get("has_value")),
                "invalid": bool(raw.get("invalid")),
                "option_count": option_count,
            }
        )
    return safe_fields


def save_landing_diagnostic(
    *,
    job_id: str,
    worker_id: str,
    reason: str,
    url: str,
    title: str,
    body: str,
    ats: str,
    stage: ATSStage,
    controls: object,
    form_fields: object = (),
    tabs: object = (),
    frame_count: object = 0,
    editable_field_count: object = 0,
    readiness: object = None,
) -> Path:
    """Write a private, sanitized pre-application artifact for offline replay."""
    normalized_reason = re.sub(r"[^a-z0-9_.-]+", "_", str(reason).casefold())[:80]
    normalized_worker = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(worker_id))[:80] or "worker"
    run_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", get_run_id())[:100] or "run"
    safe_tabs = []
    if isinstance(tabs, (list, tuple)):
        for tab in tabs[:20]:
            if isinstance(tab, dict):
                safe_tabs.append(
                    {
                        "selected": bool(tab.get("selected")),
                        "url": _safe_landing_url(str(tab.get("url", "") or "")),
                    }
                )
    raw_controls = controls if isinstance(controls, (list, tuple)) else ()
    try:
        safe_frame_count = max(0, int(frame_count))
    except (TypeError, ValueError):
        safe_frame_count = 0
    try:
        safe_editable_field_count = max(0, int(editable_field_count))
    except (TypeError, ValueError):
        safe_editable_field_count = 0
    payload = {
        "version": 1,
        "run_id": run_id,
        "worker_id": normalized_worker,
        "reason": normalized_reason,
        "url": _safe_landing_url(url),
        "page_title": _safe_landing_text(title, limit=240),
        "ats": str(ats or SiteFamily.GENERIC.value),
        "stage": ATSStage(stage).value,
        "page_fingerprint": page_fingerprint(url, body),
        "body_length": len(str(body or "")),
        "raw_control_count": len(raw_controls),
        "visible_control_count": sum(
            1
            for control in raw_controls
            if isinstance(control, dict) and bool(control.get("visible"))
        ),
        "frame_count": safe_frame_count,
        "editable_field_count": safe_editable_field_count,
        "controls": _safe_landing_controls(controls),
        "form_fields": _safe_landing_form_fields(form_fields),
        "tab_count": len(safe_tabs),
        "tabs": safe_tabs,
        "timestamp": datetime.now().isoformat(),
    }
    if isinstance(readiness, dict) and readiness:
        # Strict allowlist: the live probe must never become a raw payload
        # dump. URL/title use the same candidate-aware sanitizer as the rest
        # of this artifact; only typed counts and flags survive otherwise.
        safe_readiness = {}
        for key in (
            "body_length", "dom_element_count", "raw_control_count",
            "visible_control_count", "frame_count", "visible_frame_count", "page_count",
            "active_page_index", "observation_count", "elapsed_ms",
        ):
            if type(readiness.get(key)) is int:
                safe_readiness[key] = readiness[key]
        for key in ("body_exists", "page_closed", "target_changed", "url_changed", "meaningful"):
            if type(readiness.get(key)) is bool:
                safe_readiness[key] = readiness[key]
        if readiness.get("ready_state") in {"loading", "interactive", "complete"}:
            safe_readiness["ready_state"] = readiness["ready_state"]
        identity = str(readiness.get("page_identity") or "")
        if re.fullmatch(r"[a-f0-9]{16}", identity):
            safe_readiness["page_identity"] = identity
        if "url" in readiness:
            safe_readiness["url"] = _safe_landing_url(str(readiness["url"]))
        if "title" in readiness:
            safe_readiness["title"] = _safe_landing_text(readiness["title"], limit=240)
        payload["readiness"] = safe_readiness
    path = evidence_path(job_id) / f"landing-{run_id}-{normalized_worker}-{normalized_reason}.json"
    _write_private_bytes(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
    )
    return path


def load_landing_diagnostic(path: Path) -> dict[str, object]:
    """Load a saved sanitized landing artifact for deterministic test replay."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Unsupported landing diagnostic artifact")
    return payload


def replay_application_entry_diagnostic(artifact: dict[str, object]) -> dict[str, object]:
    """Run the deterministic resolver against a sanitized landing artifact."""
    try:
        stage = ATSStage(str(artifact.get("stage") or ATSStage.LANDING.value))
    except ValueError:
        stage = ATSStage.LANDING
    controls = artifact.get("controls")
    controls = controls if isinstance(controls, list) else []
    resolver_controls = [
        {
            "label": control.get("label", ""),
            "aria_label": control.get("aria_label", ""),
            "title": control.get("title", ""),
            "kind": control.get("kind", "button"),
            "href": "",
        }
        for control in controls
        if isinstance(control, dict) and control.get("visible")
    ]
    candidates = rank_application_entry_candidates(resolver_controls, stage=stage)
    accepted = {(candidate.label, candidate.href) for candidate in candidates}
    rejected: list[dict[str, object]] = []
    for index, control in enumerate(resolver_controls):
        label = normalize_text(
            " ".join(
                str(control.get(key, "") or "")
                for key in ("label", "aria_label", "title")
            )
        )
        key = (normalize_text(control.get("label", "")) or label[:120], "")
        if key in accepted:
            continue
        if any(term in label for term in ("search", "filter", "share", "talent community", "subscribe")):
            reason = "non_application_control"
        elif stage is ATSStage.APPLY_ENTRY and any(
            word in label for word in ("submit", "finish", "complete")
        ):
            reason = "late_stage_submit_protected"
        else:
            reason = "not_a_semantic_application_entry"
        rejected.append({"index": index, "reason": reason})
    return {
        "stage": stage.value,
        "candidate_count": len(candidates),
        "candidates": [
            {
                "label": candidate.label,
                "kind": candidate.kind.value,
                "confidence": candidate.confidence,
                "reason": candidate.reason,
            }
            for candidate in candidates
        ],
        "selected": candidates[0].label if candidates else "",
        "rejected": rejected,
    }


def save_screenshot_b64(job_id: str, screenshot_b64: str | None):
    if not screenshot_b64:
        return None
    value = str(screenshot_b64)
    if "," in value and value.lower().startswith("data:"):
        value = value.split(",", 1)[1]
    try:
        data = base64.b64decode(value)
    except Exception:
        return None
    path = evidence_path(job_id) / "confirmation.png"
    _write_private_bytes(path, data)
    return path


def save_evidence(
    *,
    job_id: str,
    result: str,
    external_url: str,
    final_url: str,
    ats: str,
    confirmation: str = "",
    linkedin_company: str = "",
    external_company: str = "",
    reason: str = "",
    body_excerpt: str = "",
    verification_source: str = "",
):
    folder = evidence_path(job_id)
    payload = {
        "run_id": get_run_id(),
        "job_id": job_id,
        "result": result,
        "external_url": redact_text(external_url),
        "final_url": redact_text(final_url),
        "ats": ats,
        "confirmation": redact_text(confirmation, limit=500),
        "verification_source": verification_source,
        "linkedin_company": linkedin_company,
        "external_company_hint": external_company,
        "company_match": company_match(linkedin_company, external_company),
        "reason": redact_text(reason, limit=1000),
        "timestamp": datetime.now().isoformat(),
    }
    _write_private_bytes(
        folder / "result.json",
        json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
    )
    # Never persist raw ATS page bodies: they commonly contain the populated
    # application form and therefore candidate PII.
    _secure_directory(ANALYTICS_DIR)
    path = ANALYTICS_DIR / "external_results.jsonl"
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    with os.fdopen(descriptor, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        f.flush()
    return payload


def _answers_file():
    return MEMORY_DIR / "answers.json"


def load_answer_memory():
    """Disable replay of learned answers from unrelated employers.

    Older artifacts remain on disk for audit preservation but are neither read
    nor injected into a later provider prompt.
    """

    return {}


def answer_memory_prompt():
    return "Persistent learned application answers are disabled for privacy."


def save_answer(question: str, answer: str):
    """Never persist candidate form answers for external applications."""

    # Keep the compatibility function as a no-op for in-flight callers.  Do
    # not delete older memory files: audit history is preserved, simply not
    # replayed or expanded.
    del question, answer


def _load_accounts():
    if not ACCOUNTS_FILE.exists():
        return {}
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError as exc:
        raise RuntimeError("encrypted ATS credential storage is unavailable") from exc
    try:
        raw = ACCOUNTS_FILE.read_bytes()
        # One-time in-place migration for the old 0600 plaintext JSON format.
        # The plaintext is parsed only in memory, then replaced by ciphertext.
        if raw.lstrip().startswith(b"{"):
            legacy = json.loads(raw.decode("utf-8"))
            if not isinstance(legacy, dict):
                raise RuntimeError("legacy ATS credential store is not a map")
            _write_accounts(legacy)
            return legacy
        key_file = _accounts_key_file()
        if not key_file.exists():
            raise RuntimeError("ATS credential encryption key is missing")
        key = key_file.read_bytes()
        return json.loads(Fernet(key).decrypt(raw).decode("utf-8"))
    except InvalidToken as exc:
        raise RuntimeError("ATS credential store could not be decrypted") from exc
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("ATS credential store is unreadable") from exc


@contextmanager
def _locked_accounts_store():
    """Serialize credential creation across isolated external workers.

    A worker can only create an account after it holds this lock.  That keeps
    two naturally admitted jobs on the same ATS from each inventing a new
    password/account before either record reaches disk.
    """

    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        ACCOUNTS_FILE.parent.chmod(0o700)
    except OSError:
        pass
    lock_path = ACCOUNTS_FILE.with_name(f".{ACCOUNTS_FILE.name}.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "a+", encoding="utf-8") as lock_file:
            descriptor = -1
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_accounts(accounts: dict[str, Any]) -> None:
    """Atomically persist an encrypted credential map while holding the lock."""

    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError("encrypted ATS credential storage is unavailable") from exc
    key_file = _accounts_key_file()
    if key_file.exists():
        key = key_file.read_bytes()
    else:
        key = Fernet.generate_key()
        _write_private_bytes(key_file, key)
    token = Fernet(key).encrypt(
        json.dumps(accounts, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    _write_private_bytes(ACCOUNTS_FILE, token)


def credential_reference(url: str, email: str = "") -> str:
    """Return a stable non-secret identifier suitable for application records."""

    _, account_key = _account_key(url, email)
    return "atsacct-" + hashlib.sha256(account_key.encode("utf-8")).hexdigest()[:20]


_PASSWORD_SPECIALS = "!@#$%"


@dataclass(frozen=True)
class AccountRegistrationClaim:
    """A durable, single-worker reservation for an irreversible registration.

    The password is deliberately omitted from ``repr`` so accidental diagnostic
    rendering cannot disclose it.  The claim itself is persisted under the
    existing 0600 credential store before the Create Account click.
    """

    account_key: str
    claim_id: str
    password: str = field(repr=False)


def _account_key(url: str, email: str) -> tuple[str, str]:
    host = urlparse(str(url)).netloc.lower() or "unknown"
    return host, f"{host}|{email}"


def _process_is_alive(pid: object) -> bool:
    """Return whether a claimant process is still alive without logging it."""

    try:
        candidate = int(pid)
    except (TypeError, ValueError):
        return False
    if candidate <= 0:
        return False
    try:
        os.kill(candidate, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def password_meets_policy(
    password: str,
    *,
    minimum_length: int = 8,
    maximum_length: int | None = None,
) -> bool:
    """Check the conservative policy shared by ordinary ATS registrations."""

    value = str(password or "")
    if len(value) < max(8, int(minimum_length)):
        return False
    if maximum_length is not None and len(value) > int(maximum_length):
        return False
    return (
        any(character.islower() for character in value)
        and any(character.isupper() for character in value)
        and any(character.isdigit() for character in value)
        and any(character in _PASSWORD_SPECIALS for character in value)
        and value.isascii()
        and not any(character.isspace() for character in value)
    )


def generate_ats_password(
    *,
    minimum_length: int = 8,
    maximum_length: int | None = None,
) -> str:
    """Generate a bounded ASCII password accepted by common ATS policies."""

    floor = max(8, int(minimum_length))
    ceiling = int(maximum_length) if maximum_length is not None else 16
    if ceiling < floor:
        raise ValueError("ATS password maximum is shorter than its required minimum")
    length = min(16, ceiling)
    alphabet = string.ascii_letters + string.digits + _PASSWORD_SPECIALS
    required = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice(_PASSWORD_SPECIALS),
    ]
    required.extend(secrets.choice(alphabet) for _ in range(length - len(required)))
    # SystemRandom is cryptographically secure and avoids predictable required
    # character placement without retaining a plaintext copy outside the store.
    secrets.SystemRandom().shuffle(required)
    password = "".join(required)
    if not password_meets_policy(
        password,
        minimum_length=floor,
        maximum_length=ceiling,
    ):
        raise RuntimeError("Could not generate a compatible ATS password")
    return password


def get_or_create_ats_password(
    url: str,
    email: str = "",
    *,
    minimum_length: int = 8,
    maximum_length: int | None = None,
):
    """Return a reused account password or make one that fits visible limits.

    Existing credentials are never silently rotated: doing so could orphan an
    account or induce a duplicate registration.  A visible incompatible policy
    therefore fails safely for operator review.
    """

    host, key = _account_key(url, email)
    with _locked_accounts_store():
        accounts = _load_accounts()
        existing = accounts.get(key)
        if isinstance(existing, dict) and existing.get("password"):
            password = str(existing["password"])
            if not password_meets_policy(
                password,
                minimum_length=minimum_length,
                maximum_length=maximum_length,
            ):
                raise ValueError(
                    "Existing ATS credential is incompatible with the visible password policy"
                )
            return password

        accounts[key] = {
            "email": email,
            "host": host,
            "password": generate_ats_password(
                minimum_length=minimum_length,
                maximum_length=maximum_length,
            ),
            "created_at": datetime.now().isoformat(),
            # This is only a credential reservation.  A later registration
            # claim records the irreversible Create Account boundary.
            "registration": {"state": "credential_created"},
        }
        _write_accounts(accounts)
        return str(accounts[key]["password"])


def claim_ats_account_registration(
    url: str,
    email: str,
    *,
    minimum_length: int = 8,
    maximum_length: int | None = None,
) -> AccountRegistrationClaim | None:
    """Atomically reserve one ordinary Create Account submission.

    The durable claim closes the gap between credential generation and the
    physical click: isolated workers sharing an ATS host and candidate email
    cannot both submit the registration form.  A dead process may relinquish
    only a *pre-submit* claim.  Once the click boundary is recorded, the claim
    is never automatically recycled because a second click could duplicate an
    account or trigger account-security controls.
    """

    host, key = _account_key(url, email)
    if not str(email).strip():
        return None
    with _locked_accounts_store():
        accounts = _load_accounts()
        entry = accounts.get(key)
        if not isinstance(entry, dict):
            entry = {
                "email": email,
                "host": host,
                "password": generate_ats_password(
                    minimum_length=minimum_length,
                    maximum_length=maximum_length,
                ),
                "created_at": datetime.now().isoformat(),
                "registration": {"state": "credential_created"},
            }
            accounts[key] = entry

        password = str(entry.get("password") or "")
        if not password_meets_policy(
            password,
            minimum_length=minimum_length,
            maximum_length=maximum_length,
        ):
            raise ValueError(
                "Existing ATS credential is incompatible with the visible password policy"
            )

        registration = entry.get("registration")
        # Older opaque credential records cannot establish whether an account
        # was already created.  Fail closed instead of clicking Create Account
        # again; the normal sign-in flow can still use the stored password.
        if not isinstance(registration, dict):
            return None
        state = str(registration.get("state") or "")
        if state == "credential_created":
            pass
        elif state == "claimed" and not _process_is_alive(registration.get("pid")):
            # A dead worker had not crossed the persisted click boundary, so
            # this strictly pre-submit reservation can be reclaimed.
            pass
        else:
            # Future/corrupt/ambiguous states may be written by a worker that
            # crossed the physical Create Account boundary before it could
            # durably update the record.  Never infer that replay is safe.
            return None

        claim_id = secrets.token_urlsafe(24)
        entry["registration"] = {
            "state": "claimed",
            "claim_id": claim_id,
            "pid": os.getpid(),
            "claimed_at": datetime.now().isoformat(),
        }
        _write_accounts(accounts)
        return AccountRegistrationClaim(
            account_key=key,
            claim_id=claim_id,
            password=password,
        )


def ats_account_registration_state(url: str, email: str) -> str:
    """Read the non-secret account lifecycle state under the store lock."""

    _, key = _account_key(url, email)
    with _locked_accounts_store():
        accounts = _load_accounts()
        entry = accounts.get(key)
        if not isinstance(entry, dict):
            return "missing"
        registration = entry.get("registration")
        if not isinstance(registration, dict):
            return "legacy"
        return str(registration.get("state") or "unknown")


def mark_ats_registration_submit_started(claim: AccountRegistrationClaim) -> bool:
    """Durably cross the no-duplicate boundary immediately before clicking."""

    with _locked_accounts_store():
        accounts = _load_accounts()
        entry = accounts.get(claim.account_key)
        registration = entry.get("registration") if isinstance(entry, dict) else None
        if (
            not isinstance(registration, dict)
            or registration.get("state") != "claimed"
            or registration.get("claim_id") != claim.claim_id
        ):
            return False
        entry["registration"] = {
            "state": "submit_started",
            "claim_id": claim.claim_id,
            "submit_started_at": datetime.now().isoformat(),
        }
        _write_accounts(accounts)
        return True


def mark_ats_registration_created(claim: AccountRegistrationClaim) -> bool:
    """Record a deterministic post-click transition without exposing credentials."""

    with _locked_accounts_store():
        accounts = _load_accounts()
        entry = accounts.get(claim.account_key)
        registration = entry.get("registration") if isinstance(entry, dict) else None
        if (
            not isinstance(registration, dict)
            or registration.get("state") != "submit_started"
            or registration.get("claim_id") != claim.claim_id
        ):
            return False
        entry["registration"] = {
            "state": "created",
            "created_at": datetime.now().isoformat(),
        }
        _write_accounts(accounts)
        return True


def release_ats_registration_claim(claim: AccountRegistrationClaim) -> None:
    """Release a claim only when no Create Account click was attempted."""

    with _locked_accounts_store():
        accounts = _load_accounts()
        entry = accounts.get(claim.account_key)
        registration = entry.get("registration") if isinstance(entry, dict) else None
        if (
            isinstance(registration, dict)
            and registration.get("state") == "claimed"
            and registration.get("claim_id") == claim.claim_id
        ):
            entry["registration"] = {"state": "credential_created"}
            _write_accounts(accounts)


def analytics_summary():
    path = ANALYTICS_DIR / "external_results.jsonl"
    if not path.exists():
        return {}
    stats = {
        SUBMITTED: 0,
        UNVERIFIED: 0,
        NEEDS_HUMAN: 0,
        FAILED: 0,
        NOT_ELIGIBLE: 0,
        "TOTAL": 0,
        "ATS": {},
    }
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        result = row.get("result", "UNKNOWN")
        ats = row.get("ats", "generic")
        stats["TOTAL"] += 1
        stats[result] = stats.get(result, 0) + 1
        ats_stats = stats["ATS"].setdefault(ats, {"total": 0, "submitted": 0})
        ats_stats["total"] += 1
        if result == SUBMITTED:
            ats_stats["submitted"] += 1
    return stats
