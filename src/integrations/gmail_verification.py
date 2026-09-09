import asyncio
import base64
import html
import re
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

VERIFICATION_WORDS = (
    "verification",
    "verify",
    "confirmation",
    "confirm",
    "security code",
    "verification code",
    "one-time code",
    "one time code",
    "otp",
)

SECURITY_WORDS = (
    "otp",
    "one-time code",
    "one time code",
    "verification code",
    "security code",
    "mfa",
    "2fa",
    "two-factor",
    "two factor",
    "suspicious login",
    "password reset",
    "magic link",
    "device verification",
    "captcha",
)


class GmailVerificationAmbiguous(RuntimeError):
    """More than one equally plausible activation email was found."""


APPLICATION_RECEIPT_PHRASES = (
    "thank you for applying",
    "thanks for applying",
    "thank you for your application",
    "application received",
    "we received your application",
    "we have received your application",
    "application submitted",
    "your application has been submitted",
    "application successfully submitted",
    "thank you for submitting",
    "thank you for your submission",
)

REJECTION_PHRASES = (
    r"\bwe will not be moving forward\b",
    r"\bnot selected\b",
    r"\bapplication (?:was|has been) unsuccessful\b",
    r"\bposition has been filled\b",
    r"\bno longer under consideration\b",
    r"\bdecided to pursue other candidates\b",
    r"\bwe have decided to move forward with other candidates\b",
    r"\bwill not be advancing your application\b",
)
INTERVIEW_PHRASES = (
    r"\brequest(?:ed)? (?:an )?interview\b",
    r"\binvite you to (?:an )?interview\b",
    r"\bschedule (?:a|your) interview\b",
    r"\bwant(?:s)? to speak with you\b",
    r"\brecruiter (?:would like|wants) to (?:speak|connect)\b",
    r"\bscreening (?:call|interview|questions?)\b",
    r"\bnext steps in the interview process\b",
)
_NEGATED_REJECTION = re.compile(
    r"\b(?:not|never|hasn't|have not|has not)\s+(?:been\s+)?(?:rejected|declined|unsuccessful)\b",
    re.I,
)


@dataclass
class RelevantEmail:
    message_id: str
    sender: str
    subject: str
    received_ms: int
    body: str
    codes: list[str]
    verification_links: list[str]
    looks_like_receipt: bool
    message_kind: str
    company_match: bool
    job_title_match: bool
    score: int
    employer_response: str = ""
    employer_confidence: float = 0.0
    sender_domain: str = ""


@dataclass(frozen=True)
class EmployerEmailMatch:
    """Privacy-safe result of matching one employer email to one job."""

    message_id: str
    job_key: str | None
    ambiguous: bool
    response: str
    confidence: float
    sender_domain: str
    received_ms: int


def _sender_domain(sender: str) -> str:
    match = re.search(r"@([A-Za-z0-9.-]+)", str(sender or ""))
    return (match.group(1) if match else "").casefold().rstrip(".")


def classify_employer_email(subject: str, body: str) -> tuple[str, float]:
    """Classify only strong employer-response semantics, with negation guards."""

    normalized = " ".join(str(subject or "").casefold().split()) + " " + " ".join(
        str(body or "").casefold().split()
    )
    if _NEGATED_REJECTION.search(normalized):
        return "", 0.0
    for pattern in INTERVIEW_PHRASES:
        if re.search(pattern, normalized, re.I):
            return "INTERVIEW_REQUESTED", 0.95
    for pattern in REJECTION_PHRASES:
        if re.search(pattern, normalized, re.I):
            return "REJECTED", 0.94
    if any(phrase in normalized for phrase in APPLICATION_RECEIPT_PHRASES):
        return "APPLICATION_RECEIVED", 0.88
    return "", 0.0


def _tokens(value: Any, minimum: int = 4) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").casefold())
        if len(token) >= minimum
    }


def match_employer_email_to_jobs(
    message: RelevantEmail, jobs: Iterable[Mapping[str, Any]]
) -> EmployerEmailMatch:
    """Resolve a status email conservatively; ties are explicitly ambiguous."""

    if not message.employer_response:
        return EmployerEmailMatch(
            message.message_id, None, False, "", 0.0, message.sender_domain, message.received_ms
        )
    sender_domain = message.sender_domain or _sender_domain(message.sender)
    haystack = f"{message.subject} {message.body}".casefold()
    ranked: list[tuple[int, str]] = []
    for job in jobs:
        company = _tokens(job.get("company_name"), minimum=4)
        title = _tokens(job.get("job_title"), minimum=5)
        requisition = _tokens(job.get("requisition_id") or job.get("job_id"), minimum=3)
        external_host = (urlparse(str(job.get("external_url") or "")).hostname or "").casefold()
        score = 0
        if company and any(token in haystack for token in company):
            score += 4
        title_hits = len([token for token in title if token in haystack])
        if title_hits:
            score += min(3, title_hits)
        if requisition and any(token in haystack for token in requisition):
            score += 5
        if external_host and (sender_domain == external_host or sender_domain.endswith("." + external_host)):
            score += 2
        submitted = job.get("submitted_at") or job.get("credible_submission_at")
        try:
            submitted_ms = int(datetime.fromisoformat(str(submitted).replace("Z", "+00:00")).timestamp() * 1000)
        except (AttributeError, TypeError, ValueError, OverflowError):
            submitted_ms = 0
        if submitted_ms and message.received_ms >= submitted_ms:
            score += 1
        if score >= 4:
            ranked.append((score, str(job.get("job_key") or "")))
    ranked.sort(reverse=True)
    if not ranked:
        return EmployerEmailMatch(
            message.message_id, None, False, message.employer_response, 0.0, sender_domain, message.received_ms
        )
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return EmployerEmailMatch(
            message.message_id, None, True, message.employer_response, 0.5, sender_domain, message.received_ms
        )
    confidence = min(0.99, 0.55 + ranked[0][0] * 0.07)
    return EmployerEmailMatch(
        message.message_id, ranked[0][1], False, message.employer_response, confidence, sender_domain, message.received_ms
    )


class GmailVerificationClient:
    def __init__(self, credentials_path: str, token_path: str):
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)
        self.service = None
        self.email_address = None

    def connect(self, interactive: bool = True):
        creds = None
        if self.token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
            except Exception:
                creds = None
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds or not creds.valid:
            if not interactive:
                raise RuntimeError(
                    "Gmail OAuth token is missing or invalid. Run: uv run python gmail_auth.py"
                )
            if not self.credentials_path.exists():
                raise FileNotFoundError(
                    f"Gmail OAuth credentials not found: {self.credentials_path}"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True)
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_path.write_text(creds.to_json(), encoding="utf-8")
            try:
                self.token_path.chmod(0o600)
            except Exception:
                pass
        self.service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        profile = self.service.users().getProfile(userId="me").execute()
        self.email_address = profile.get("emailAddress")
        return self.email_address

    def _ensure_connected(self):
        if self.service is None:
            self.connect(interactive=False)

    @staticmethod
    def _decode(data: str) -> str:
        if not data:
            return ""
        padding = "=" * (-len(data) % 4)
        try:
            raw = base64.urlsafe_b64decode(data + padding)
            return raw.decode("utf-8", errors="ignore")
        except Exception:
            return ""

    def _extract_part(self, part) -> tuple[str, str]:
        mime = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data")
        plain = ""
        html_body = ""
        if body_data:
            decoded = self._decode(body_data)
            if mime == "text/plain":
                plain += decoded
            elif mime == "text/html":
                html_body += decoded
        for child in part.get("parts", []) or []:
            p, h = self._extract_part(child)
            plain += "\n" + p
            html_body += "\n" + h
        return plain, html_body

    @staticmethod
    def _html_to_text(value: str) -> str:
        if not value:
            return ""
        value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
        value = re.sub(r"(?i)<br\s*/?>", "\n", value)
        value = re.sub(r"(?i)</p>", "\n", value)
        value = re.sub(r"<[^>]+>", " ", value)
        return html.unescape(value)

    @staticmethod
    def _extract_codes(text: str) -> list[str]:
        candidates = []
        patterns = [
            r"(?i)(?:verification|security|confirmation|one[- ]?time|otp)(?:\s+code)?[^0-9]{0,50}([0-9]{4,8})",
            r"(?i)(?:code)[^0-9]{0,30}([0-9]{4,8})",
        ]
        for pattern in patterns:
            candidates.extend(re.findall(pattern, text))
        candidates.extend(re.findall(r"(?<!\d)(\d{6})(?!\d)", text))
        result = []
        for code in candidates:
            if code not in result:
                result.append(code)
        return result[:5]

    @staticmethod
    def _extract_links(raw: str) -> list[str]:
        if not raw:
            return []
        found = re.findall(r'https?://[^\s<>"\']+', html.unescape(raw))
        cleaned = []
        for url in found:
            url = url.rstrip(".,);]}>")
            low = url.lower()
            if (
                any(
                    word in low
                    for word in (
                        "verify",
                        "verification",
                        "confirm",
                        "confirmation",
                        "activate",
                        "activation",
                        "token",
                        "applicant",
                        "candidate",
                        "account",
                    )
                )
                and url not in cleaned
            ):
                cleaned.append(url)
        return cleaned[:10]

    @staticmethod
    def _header(headers, name):
        for header in headers:
            if header.get("name", "").lower() == name.lower():
                return header.get("value", "")
        return ""

    @staticmethod
    def _normalize(value):
        return " ".join(str(value or "").lower().split())

    def _message_to_relevant(self, message, *, company: str = "", job_title: str = ""):
        payload = message.get("payload", {})
        headers = payload.get("headers", [])
        sender = self._header(headers, "From")
        subject = self._header(headers, "Subject")
        plain, html_body = self._extract_part(payload)
        body = "\n".join(x for x in (plain, self._html_to_text(html_body)) if x)
        codes = self._extract_codes(body)
        links = self._extract_links(plain + "\n" + html_body)
        normalized = self._normalize(subject + "\n" + sender + "\n" + body)
        score = 0
        for word in VERIFICATION_WORDS:
            if word in normalized:
                score += 4
        if codes:
            score += 8
        if links:
            score += 6
        looks_like_receipt = any(phrase in normalized for phrase in APPLICATION_RECEIPT_PHRASES)
        if looks_like_receipt:
            score += 10
        company_tokens = [
            token for token in re.findall(r"[a-z0-9]+", self._normalize(company)) if len(token) >= 4
        ]
        company_match = bool(
            company_tokens and any(token in normalized for token in company_tokens)
        )
        if company_match:
            score += 5
        job_tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", self._normalize(job_title))
            if len(token) >= 5
        ]
        job_title_match = bool(job_tokens and any(token in normalized for token in job_tokens))
        if job_title_match:
            score += 3

        has_verification_language = any(word in normalized for word in VERIFICATION_WORDS)
        employer_response, employer_confidence = classify_employer_email(subject, body)
        if employer_response == "REJECTED":
            message_kind = "employer_rejection"
        elif employer_response == "INTERVIEW_REQUESTED":
            message_kind = "interview_request"
        elif employer_response == "APPLICATION_RECEIVED":
            message_kind = "application_receipt"
        elif looks_like_receipt:
            message_kind = "application_receipt"
        elif codes and has_verification_language:
            message_kind = "otp_code"
        elif links and any(word in normalized for word in ("activate", "activation", "account")):
            message_kind = "account_activation"
        elif links and has_verification_language:
            message_kind = "verification_link"
        else:
            message_kind = "other"
        return RelevantEmail(
            message_id=message.get("id", ""),
            sender=sender,
            subject=subject,
            received_ms=int(message.get("internalDate", "0")),
            body=body,
            codes=codes,
            verification_links=links,
            looks_like_receipt=looks_like_receipt,
            message_kind=message_kind,
            company_match=company_match,
            job_title_match=job_title_match,
            score=score,
            employer_response=employer_response,
            employer_confidence=employer_confidence,
            sender_domain=_sender_domain(sender),
        )

    def search_relevant(
        self,
        *,
        since_epoch: float,
        company: str = "",
        job_title: str = "",
        max_results: int = 30,
    ) -> list[RelevantEmail]:
        self._ensure_connected()
        query_after = max(0, int(since_epoch) - 120)
        response = (
            self.service.users()
            .messages()
            .list(
                userId="me",
                q=f"after:{query_after}",
                maxResults=max_results,
            )
            .execute()
        )
        results = []
        for item in response.get("messages", []) or []:
            msg = (
                self.service.users()
                .messages()
                .get(userId="me", id=item["id"], format="full")
                .execute()
            )
            relevant = self._message_to_relevant(msg, company=company, job_title=job_title)
            # Gmail's after: search is intentionally widened slightly because
            # its query boundary is coarse. Enforce the exact application-start
            # timestamp locally so an older code or receipt is never reused.
            if relevant.received_ms < int(since_epoch * 1000):
                continue

            # Prefer a safe manual fallback over consuming a code, magic link,
            # or receipt belonging to a different application.
            context_matches = (
                relevant.company_match
                if company
                else relevant.job_title_match if job_title else True
            )
            if relevant.score >= 4 and context_matches:
                results.append(relevant)
        results.sort(key=lambda x: (x.score, x.received_ms), reverse=True)
        return results

    def search_status_messages(
        self, *, since_epoch: float, max_results: int = 100
    ) -> list[RelevantEmail]:
        """Fetch only recent messages with strong employer-status semantics."""

        self._ensure_connected()
        query_after = max(0, int(since_epoch) - 120)
        response = (
            self.service.users()
            .messages()
            .list(userId="me", q=f"after:{query_after}", maxResults=max_results)
            .execute()
        )
        results: list[RelevantEmail] = []
        for item in response.get("messages", []) or []:
            message = self.service.users().messages().get(
                userId="me", id=item["id"], format="full"
            ).execute()
            relevant = self._message_to_relevant(message)
            if relevant.received_ms >= int(since_epoch * 1000) and relevant.employer_response:
                results.append(relevant)
        return sorted(results, key=lambda item: item.received_ms)

    def scan_application_statuses(
        self, tracker: Any, *, now_epoch: float | None = None, max_results: int = 100
    ) -> dict[str, int]:
        """Incrementally classify and persist conservative employer responses."""

        from src.application_tracker import safe_tracker_event

        current = float(now_epoch if now_epoch is not None else time.time())
        state = tracker.gmail_state()
        last_scan = state.get("last_scan_at")
        try:
            since = datetime.fromisoformat(str(last_scan).replace("Z", "+00:00")).timestamp() - 120 if last_scan else current - 7 * 86400
        except (TypeError, ValueError, OverflowError):
            since = current - 7 * 86400
        messages = self.search_status_messages(since_epoch=max(0, since), max_results=max_results)
        jobs = tracker.records()
        processed: list[str] = []
        matched = 0
        ambiguous = 0
        from src.integrations.gmail_verification import match_employer_email_to_jobs

        for message in messages:
            if message.message_id in state.get("processed_message_ids", set()):
                continue
            match = match_employer_email_to_jobs(message, jobs)
            processed.append(message.message_id)
            if match.ambiguous:
                ambiguous += 1
                continue
            if match.job_key:
                job = next((row for row in jobs if row.get("job_key") == match.job_key), None) or {}
                tracker.apply_event(
                    safe_tracker_event(
                        "gmail_status_update",
                        job_key=match.job_key,
                        job_title=job.get("job_title"),
                        company_name=job.get("company_name"),
                        external_url=job.get("external_url"),
                        linkedin_url=job.get("linkedin_url"),
                        employer_response=match.response,
                        confidence=match.confidence,
                        message_id=match.message_id,
                        sender_domain=match.sender_domain,
                        received_at=datetime.fromtimestamp(
                            match.received_ms / 1000, tz=timezone.utc
                        ).isoformat(timespec="seconds"),
                    )
                )
                matched += 1
        tracker.mark_gmail_processed(processed, datetime.fromtimestamp(current, tz=timezone.utc))
        return {"scanned": len(messages), "matched": matched, "ambiguous": ambiguous}

    @staticmethod
    def activation_url(url: str, *, expected_host: str = "") -> str | None:
        """Return an ordinary activation URL only when its destination is trusted."""
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").casefold().rstrip(".")
        expected = (
            (urlparse(str(expected_host or "")).hostname or expected_host).casefold().rstrip(".")
        )
        if parsed.scheme not in {"http", "https"} or not host or not expected:
            return None
        if (
            host != expected
            and not host.endswith("." + expected)
            and not expected.endswith("." + host)
        ):
            return None
        return url

    def _select_activation(
        self, messages: list[RelevantEmail], *, expected_host: str
    ) -> RelevantEmail:
        candidates = []
        for message in messages:
            normalized = self._normalize(message.subject + "\n" + message.body)
            if any(term in normalized for term in SECURITY_WORDS):
                continue
            if message.message_kind not in {"account_activation", "verification_link"}:
                continue
            links = [
                self.activation_url(link, expected_host=expected_host)
                for link in message.verification_links
            ]
            if any(links):
                candidates.append(message)
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item.score, item.received_ms), reverse=True)
        if len(candidates) > 1 and candidates[0].score == candidates[1].score:
            raise GmailVerificationAmbiguous("multiple activation emails matched")
        return candidates[0]

    def _select_application_verification(
        self, messages: list[RelevantEmail], *, expected_host: str
    ) -> RelevantEmail:
        """Select one correlated activation link or OTP for the active ATS."""

        candidates = []
        for message in messages:
            normalized = self._normalize(message.subject + "\n" + message.body)
            if any(term in normalized for term in ("suspicious login", "password reset", "captcha", "device verification")):
                continue
            if message.message_kind in {"account_activation", "verification_link"}:
                if not any(
                    self.activation_url(link, expected_host=expected_host)
                    for link in message.verification_links
                ):
                    continue
            elif message.message_kind == "otp_code":
                if not message.codes:
                    continue
            else:
                continue
            candidates.append(message)
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item.score, item.received_ms), reverse=True)
        if len(candidates) > 1 and candidates[0].score == candidates[1].score:
            raise GmailVerificationAmbiguous("multiple application verification messages matched")
        return candidates[0]

    async def wait_for_application_verification(
        self,
        *,
        since_epoch: float,
        company: str = "",
        job_title: str = "",
        expected_host: str = "",
        timeout: int = 180,
        poll_interval: int = 5,
    ) -> Optional[RelevantEmail]:
        """Wait for one strongly correlated ATS activation message or OTP."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            messages = await asyncio.to_thread(
                self.search_relevant, since_epoch=since_epoch, company=company, job_title=job_title
            )
            selected = self._select_application_verification(
                messages, expected_host=expected_host
            )
            if selected is not None:
                return selected
            await asyncio.sleep(poll_interval)
        return None

    async def wait_for_account_activation(
        self,
        *,
        since_epoch: float,
        company: str = "",
        job_title: str = "",
        expected_host: str = "",
        timeout: int = 180,
        poll_interval: int = 5,
    ) -> Optional[RelevantEmail]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            messages = await asyncio.to_thread(
                self.search_relevant, since_epoch=since_epoch, company=company, job_title=job_title
            )
            selected = self._select_activation(messages, expected_host=expected_host)
            if selected is not None:
                return selected
            await asyncio.sleep(poll_interval)
        return None

    async def wait_for_verification(
        self,
        *,
        since_epoch: float,
        company: str = "",
        job_title: str = "",
        timeout: int = 180,
        poll_interval: int = 5,
    ) -> Optional[RelevantEmail]:
        deadline = time.monotonic() + timeout
        seen = set()
        while time.monotonic() < deadline:
            messages = await asyncio.to_thread(
                self.search_relevant,
                since_epoch=since_epoch,
                company=company,
                job_title=job_title,
            )
            for message in messages:
                if message.message_id in seen:
                    continue
                seen.add(message.message_id)
                if message.codes or message.verification_links:
                    return message
            await asyncio.sleep(poll_interval)
        return None

    async def wait_for_receipt(
        self,
        *,
        since_epoch: float,
        company: str = "",
        job_title: str = "",
        timeout: int = 60,
        poll_interval: int = 5,
    ) -> Optional[RelevantEmail]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            messages = await asyncio.to_thread(
                self.search_relevant,
                since_epoch=since_epoch,
                company=company,
                job_title=job_title,
            )
            for message in messages:
                if message.looks_like_receipt:
                    return message
            await asyncio.sleep(poll_interval)
        return None
