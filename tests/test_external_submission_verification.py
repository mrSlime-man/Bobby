from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.llm.external_apply_support import (
    collect_post_submit_evidence,
    needs_human,
    needs_human_from_visible_controls,
)


class _Page:
    def __init__(self, bodies):
        self._bodies = iter(bodies)

    async def evaluate(self, _script):
        try:
            return next(self._bodies)
        except StopIteration:
            return ""

    async def get_url(self):
        return "https://careers.example.invalid/application/status"


@pytest.mark.asyncio
async def test_post_submit_verifier_detects_delayed_confirmation_without_another_action():
    page = _Page(["Submitting application…", "Thank you for applying to this role."])
    session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))

    result = await collect_post_submit_evidence(
        session,
        attempts=2,
        poll_interval_seconds=0,
    )

    assert result.verified is True
    assert result.source == "page_dom"
    assert result.confirmation == "thank you for applying"
    assert session.must_get_current_page.await_count == 2


@pytest.mark.asyncio
async def test_post_submit_verifier_marks_unverified_when_no_page_or_receipt_evidence():
    page = _Page(["Application review", "Application review"])
    session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))
    receipt_checker = AsyncMock(return_value=None)

    result = await collect_post_submit_evidence(
        session,
        attempts=2,
        poll_interval_seconds=0,
        receipt_checker=receipt_checker,
    )

    assert result.verified is False
    assert result.human_reason == ""
    receipt_checker.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_submit_verifier_stops_for_security_challenge_without_receipt_polling():
    page = _Page(["Enter your MFA verification code"])
    session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))
    receipt_checker = AsyncMock(return_value="Application receipt email received")

    result = await collect_post_submit_evidence(
        session,
        attempts=1,
        receipt_checker=receipt_checker,
    )

    assert result.verified is False
    assert result.human_reason == "verification code"
    receipt_checker.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_submit_security_stops_before_a_later_confirmation_or_receipt():
    page = _Page(["Enter OTP", "Thank you for applying."])
    session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))
    receipt_checker = AsyncMock(return_value="Application receipt email received")

    result = await collect_post_submit_evidence(
        session,
        attempts=2,
        poll_interval_seconds=0,
        receipt_checker=receipt_checker,
    )

    assert result.verified is False
    assert result.human_reason == "otp"
    assert session.must_get_current_page.await_count == 1
    receipt_checker.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_submit_verifier_accepts_receipt_only_after_page_checks():
    page = _Page(["Application review"])
    session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))
    receipt_checker = AsyncMock(return_value="Application receipt email received")

    result = await collect_post_submit_evidence(
        session,
        attempts=1,
        receipt_checker=receipt_checker,
    )

    assert result.verified is True
    assert result.source == "email_receipt"
    receipt_checker.assert_awaited_once()


@pytest.mark.parametrize("body", ["Enter OTP to continue", "2FA required before continuing"])
def test_short_security_markers_are_not_mistaken_for_ordinary_form_text(body):
    required, reason = needs_human(body)

    assert required is True
    assert reason in {"otp", "2fa"}


def test_explicit_unauthorized_activity_waf_page_requires_human():
    required, reason = needs_human(
        "Unauthorized Request Blocked. Unauthorized Activity Detected. "
        "Contact the website security team if this is an error."
    )

    assert required is True
    assert reason == "unauthorized activity detected"


def test_visible_captcha_control_requires_human_when_body_text_omits_it():
    required, reason = needs_human_from_visible_controls(
        [
            {
                "visible": True,
                "label": "",
                "aria_label": "",
                "title": "Change the CAPTCHA code",
            }
        ]
    )

    assert required is True
    assert reason == "captcha"


def test_hidden_captcha_control_does_not_create_a_false_human_boundary():
    required, reason = needs_human_from_visible_controls(
        [{"visible": False, "title": "Change the CAPTCHA code"}]
    )

    assert required is False
    assert reason == ""
