"""Regression tests for Easy Apply quota detection and retryable routing state."""

from datetime import datetime, timezone

import pytest

from src.utils.easy_apply_quota import (
    AVAILABLE,
    BLOCKED,
    UNKNOWN,
    EasyApplyQuotaState,
    classify_easy_apply_ui,
    collect_easy_apply_ui,
    detect_quota_message,
)


@pytest.mark.parametrize(
    "text",
    [
        "You've reached today's Easy Apply limit.",
        "Easy Apply limit reached. Try again later.",
        "You cannot submit any more applications today. Please try again tomorrow.",
        "Daily application limit exceeded; try again in 30 minutes.",
    ],
)
def test_strong_linkedin_limit_evidence_blocks(text):
    detected, retry_after = detect_quota_message(text)
    assert detected is True
    if "30 minutes" in text:
        assert retry_after == 1800


@pytest.mark.parametrize(
    "text",
    [
        "This field is required.",
        "Please enter a valid answer.",
        "Provider request timed out.",
        "CAPTCHA required.",
        "The application form is closed.",
    ],
)
def test_non_quota_errors_do_not_block(text):
    assert detect_quota_message(text) == (False, None)


def test_enabled_control_is_positive_availability_evidence():
    observation = classify_easy_apply_ui(
        "LinkedIn job details", button_available=True, now_epoch=100
    )
    assert observation.status == AVAILABLE


def test_state_persists_block_across_instances_and_rechecks_without_guessing_reset(tmp_path):
    path = tmp_path / "easy_apply_quota.json"
    state = EasyApplyQuotaState(path)
    state.mark_blocked(now_epoch=100)

    restarted = EasyApplyQuotaState(path)
    assert restarted.status() == BLOCKED
    assert restarted.should_recheck(now_epoch=100 + 60) is False
    assert restarted.should_recheck(now_epoch=100 + 15 * 60) is True

    # Passing the retry window alone does not restore availability.
    assert restarted.status() == BLOCKED
    restarted.apply_observation(classify_easy_apply_ui("Easy Apply", button_available=True))
    assert restarted.status() == AVAILABLE


def test_stale_unknown_state_remains_unknown_without_decisive_evidence(tmp_path):
    state = EasyApplyQuotaState(tmp_path / "quota.json")
    assert state.status() == UNKNOWN
    assert state.apply_observation(classify_easy_apply_ui("LinkedIn job details")) == UNKNOWN


def test_restoration_clears_retry_after_and_records_safe_reason(tmp_path):
    state = EasyApplyQuotaState(tmp_path / "quota.json")
    state.mark_blocked(retry_after_epoch=3600, now_epoch=100)
    state.mark_available(now_epoch=200)
    snapshot = state.snapshot()
    assert snapshot["status"] == AVAILABLE
    assert snapshot["retry_after"] is None
    assert snapshot["reason"] == "enabled_easy_apply_control"
    assert snapshot["source"] == "linkedin_ui_runtime"
    assert "raw" not in str(snapshot).casefold()


@pytest.mark.asyncio
async def test_hidden_stale_modal_does_not_activate_quota_state():
    class Element:
        async def is_visible(self):
            return False

        async def inner_text(self):
            return "Easy Apply limit reached. Try again later."

    class Page:
        async def evaluate(self, _script):
            return "LinkedIn job details"

    async def find_elements(_page, _selector, _by):
        return [Element()]

    observation = await collect_easy_apply_ui(Page(), find_elements=find_elements)
    assert observation.status == UNKNOWN


@pytest.mark.asyncio
async def test_visible_quota_dialog_is_detected():
    class Element:
        async def is_visible(self):
            return True

        async def inner_text(self):
            return "Easy Apply limit reached. Try again later."

    class Page:
        async def evaluate(self, _script):
            return "LinkedIn job details"

    async def find_elements(_page, _selector, _by):
        return [Element()]

    observation = await collect_easy_apply_ui(Page(), find_elements=find_elements)
    assert observation.status == BLOCKED
