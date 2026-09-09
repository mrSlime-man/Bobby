"""Tests for src/job_manager/linkedin/easy_applier_linkedin.py"""

import inspect
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.job_manager.easy_applier import NoInfoException
from src.job_manager.application_question_resolver import field_spec_from_snapshot
from src.job_manager.linkedin.easy_applier_linkedin import (
    LinkedInEasyApplier,
    UnverifiedAfterSubmitError,
)
from src.pydantic_models.job_models import Job, Question
from src.utils.runtime_control import runtime_controller
from src.utils.easy_apply_quota import EasyApplyQuotaState

LINKEDIN_DEFAULT_JOB_URL = "https://www.linkedin.com/jobs/view/4410066193"


def build_linkedin_job_url(job_url_or_id: str | None = None) -> str:
    if not job_url_or_id:
        return LINKEDIN_DEFAULT_JOB_URL
    job_url_or_id = job_url_or_id.strip()
    if not job_url_or_id:
        return LINKEDIN_DEFAULT_JOB_URL
    if job_url_or_id.isdigit():
        return f"https://www.linkedin.com/jobs/view/{job_url_or_id}"
    return job_url_or_id


def normalize_test_job_from_parsed_page(parsed_job: Job | None, job_url: str) -> Job:
    job = parsed_job or Job()
    if not job.url:
        job.url = job_url
    if not job.job_title:
        job.job_title = "LinkedIn job"
    if not job.company_name:
        job.company_name = "Unknown company"
    if not job.job_description:
        job.job_description = "LinkedIn vacancy information was not parsed from the page."
    if not job.apply_method:
        job.apply_method = "Easy Apply"
    return job


def collect_apply_result_metadata(easy_applier: object, submitted_resume_path: object) -> dict:
    metadata = {}
    if isinstance(submitted_resume_path, str) and submitted_resume_path:
        metadata["submitted_resume_path"] = submitted_resume_path
    applied_at = getattr(easy_applier, "already_applied_at", None)
    applied_at_text = getattr(easy_applier, "already_applied_at_text", None)
    if isinstance(applied_at, str) and applied_at:
        metadata["applied_at"] = applied_at
    if isinstance(applied_at_text, str) and applied_at_text:
        metadata["applied_at_text"] = applied_at_text
    return metadata


LINKEDIN_JOB_URL = "https://www.linkedin.com/jobs/view/123456"


@pytest.fixture
def mock_page():
    page = AsyncMock()
    page.url = LINKEDIN_JOB_URL
    return page


@pytest.fixture(autouse=True)
def isolate_easy_apply_quota(tmp_path, monkeypatch):
    state = EasyApplyQuotaState(tmp_path / "easy_apply_quota.json")
    monkeypatch.setattr(
        "src.job_manager.linkedin.easy_applier_linkedin.easy_apply_quota_state",
        lambda: state,
    )


@pytest.fixture
def mock_gpt_answerer():
    gpt = MagicMock()
    gpt.write_cover_letter.return_value = "Dear Hiring Manager, ..."
    gpt.answer_question_textual_wide_range.return_value = "My answer"
    gpt.answer_question_numeric.return_value = "5"
    gpt.select_one_answer_from_options.return_value = "yes"
    gpt.select_many_answers_from_options.return_value = ["yes"]
    return gpt


@pytest.fixture
def mock_resume_anonymizer():
    anon = MagicMock()
    anon.deanonymize_text = lambda text: text
    return anon


@pytest.fixture
def applier(mock_page, mock_gpt_answerer, mock_resume_anonymizer, tmp_path):
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    cover_dir = tmp_path / "cover_letters"
    cover_dir.mkdir()
    answers_file = tmp_path / "answers.yaml"

    with patch(
        "src.job_manager.linkedin.easy_applier_linkedin.get_ready_made_resume", return_value=None
    ):
        with patch(
            "src.job_manager.linkedin.easy_applier_linkedin.get_ready_made_photo", return_value=None
        ):
            with patch(
                "src.job_manager.linkedin.easy_applier_linkedin.load_yaml_file", return_value=None
            ):
                inst = LinkedInEasyApplier(
                    page=mock_page,
                    gpt_answerer=mock_gpt_answerer,
                    resume_anonymizer=mock_resume_anonymizer,
                    resume_generator_manager=AsyncMock(),
                    pause_checker=None,
                    answers_file=answers_file,
                    resume_dir=resume_dir,
                    cover_letter_dir=cover_dir,
                    test_mode=False,
                )
    inst.current_job = Job(job_title="Engineer", company_name="TestCorp", url=LINKEDIN_JOB_URL)
    return inst


@pytest.fixture
def test_job():
    return Job(job_title="Engineer", company_name="TestCorp", url=LINKEDIN_JOB_URL)


def navigation_candidate(
    action: str,
    *,
    source: str = "text",
    tag: str = "button",
    role: str = "",
    attached: bool = True,
    visible: bool = True,
    enabled: bool = True,
    aria_disabled: bool = False,
) -> dict:
    return {
        "action": action,
        "source": source,
        "tag": tag,
        "role": role,
        "attached": attached,
        "visible": visible,
        "enabled": enabled,
        "aria_disabled": aria_disabled,
    }


def configure_navigation_dialog(applier, candidates, button=None, page_progress="3/5"):
    """Install a Playwright-shaped active dialog without positional locators."""
    button = button or MagicMock()
    button.count = AsyncMock(return_value=1)
    button.evaluate = AsyncMock(return_value=True)
    button.is_visible = AsyncMock(return_value=True)
    button.is_enabled = AsyncMock(return_value=True)
    button.get_attribute = AsyncMock(return_value=None)
    button.click = AsyncMock()

    candidate_scan = MagicMock()
    candidate_scan.evaluate_all = AsyncMock(return_value=candidates)
    progress_scan = MagicMock()
    progress_scan.evaluate_all = AsyncMock(return_value=page_progress)

    base_query = MagicMock()
    base_query.filter.return_value.first = button
    aria_query = MagicMock()
    aria_query.first = button

    scope = MagicMock()
    scope.count = AsyncMock(return_value=1)
    scope.is_visible = AsyncMock(return_value=True)

    def scope_locator(selector):
        if selector == "button, [role='button']":
            return candidate_scan
        if selector.startswith('[role="progressbar"]'):
            return progress_scan
        if "aria-label=" in selector:
            return aria_query
        return base_query

    scope.locator = MagicMock(side_effect=scope_locator)
    dialog_query = MagicMock()
    dialog_query.first = scope
    applier.page.locator = MagicMock(return_value=dialog_query)
    applier.page.is_closed = MagicMock(return_value=False)
    return button, scope


class TestInit:
    def test_sets_attributes(self, applier, mock_page):
        assert applier.page is mock_page
        assert applier.test_mode is False
        assert applier.all_questions == []
        assert applier.previous_question_texts == []
        assert applier.submitted_resume_path is None

    def test_generated_dirs_are_subdirs(self, applier):
        assert applier.generated_resume_dir.name == "generated_resumes"
        assert applier.generated_cover_letter_dir.name == "generated_cover_letters"


class TestDirectScriptHelpers:
    def test_builds_job_url_from_id(self):
        assert build_linkedin_job_url("4410514476") == (
            "https://www.linkedin.com/jobs/view/4410514476"
        )

    def test_preserves_full_job_url(self):
        url = "https://www.linkedin.com/jobs/view/4410514476"
        assert build_linkedin_job_url(url) == url

    def test_normalizes_parsed_job_without_overwriting_real_fields(self):
        parsed_job = Job(
            job_title="Head of AI",
            company_name="Involved Solutions",
            url="https://www.linkedin.com/jobs/view/4410514476",
            job_description="Parsed description",
        )

        job = normalize_test_job_from_parsed_page(
            parsed_job, "https://www.linkedin.com/jobs/view/4410514476"
        )

        assert job.job_title == "Head of AI"
        assert job.company_name == "Involved Solutions"
        assert job.job_description == "Parsed description"
        assert job.apply_method == "Easy Apply"

    def test_normalizes_missing_parsed_fields_with_generic_fallbacks(self):
        job = normalize_test_job_from_parsed_page(
            Job(), "https://www.linkedin.com/jobs/view/4410514476"
        )

        assert job.url == "https://www.linkedin.com/jobs/view/4410514476"
        assert job.job_title == "LinkedIn job"
        assert job.company_name == "Unknown company"
        assert job.job_description
        assert job.apply_method == "Easy Apply"

    def test_collect_apply_result_metadata(self):
        applier = MagicMock()
        applier.already_applied_at = "2026-05-07T10:00:00"
        applier.already_applied_at_text = "5 hours ago"

        assert collect_apply_result_metadata(applier, "/tmp/resume.pdf") == {
            "submitted_resume_path": "/tmp/resume.pdf",
            "applied_at": "2026-05-07T10:00:00",
            "applied_at_text": "5 hours ago",
        }

    def test_collect_apply_result_metadata_omits_empty_values(self):
        applier = MagicMock()
        applier.already_applied_at = None
        applier.already_applied_at_text = None

        assert collect_apply_result_metadata(applier, None) == {}


class TestCheckForPremiumRedirect:
    @pytest.mark.asyncio
    async def test_no_redirect(self, applier, test_job):
        applier.page.url = LINKEDIN_JOB_URL
        with patch("src.job_manager.linkedin.easy_applier_linkedin.async_pause"):
            result = await applier.check_for_premium_redirect(test_job)
        assert result is False

    @pytest.mark.asyncio
    async def test_redirect_resolved_after_one_attempt(self, applier, test_job):
        applier.page.url = "https://www.linkedin.com/premium/something"

        async def set_url(*args, **kwargs):
            applier.page.url = LINKEDIN_JOB_URL

        applier.page.goto = AsyncMock(side_effect=set_url)
        with patch("src.job_manager.linkedin.easy_applier_linkedin.async_pause"):
            result = await applier.check_for_premium_redirect(test_job)
        assert result is True

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts(self, applier, test_job):
        applier.page.url = "https://www.linkedin.com/premium/something"
        applier.page.goto = AsyncMock()
        with patch("src.job_manager.linkedin.easy_applier_linkedin.async_pause"):
            with pytest.raises(Exception, match="Redirected to linkedIn Premium page"):
                await applier.check_for_premium_redirect(test_job, max_attempts=2)


class TestCheckEasyApplyLimit:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_limit_error(self, applier):
        with patch(
            "src.job_manager.linkedin.easy_applier_linkedin.find_elements_safely",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await applier._check_easy_apply_limit()
        assert not result

    @pytest.mark.asyncio
    async def test_returns_true_when_limit_text_found(self, applier):
        mock_element = AsyncMock()
        mock_element.text_content = AsyncMock(
            return_value="You've reached today's Easy Apply limit"
        )
        with patch(
            "src.job_manager.linkedin.easy_applier_linkedin.find_elements_safely",
            new_callable=AsyncMock,
            return_value=[mock_element],
        ):
            result = await applier._check_easy_apply_limit()
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self, applier):
        with patch(
            "src.job_manager.linkedin.easy_applier_linkedin.find_elements_safely",
            new_callable=AsyncMock,
            side_effect=Exception("network error"),
        ):
            with patch("src.job_manager.linkedin.easy_applier_linkedin.debug_capture"):
                result = await applier._check_easy_apply_limit()
        assert not result


class TestApplyToJob:
    @pytest.mark.asyncio
    async def test_skips_when_limit_reached(self, applier, test_job):
        applier._check_easy_apply_limit = AsyncMock(return_value=True)
        result = await applier.apply_to_job(test_job)
        assert result[0][0] == "Deferred"
        assert "DEFERRED_EASY_APPLY_LIMIT" in result[0][1]

    @pytest.mark.asyncio
    async def test_delegates_to_job_easy_apply(self, applier, test_job):
        applier._check_easy_apply_limit = AsyncMock(return_value=False)
        applier.submitted_resume_path = None
        applier.job_easy_apply = AsyncMock(return_value=("Success", ""))
        with patch("src.job_manager.linkedin.easy_applier_linkedin.emit_event"):
            result = await applier.apply_to_job(test_job)
        assert result == (("Success", ""), None)

    @pytest.mark.asyncio
    async def test_reraises_exception(self, applier, test_job):
        applier._check_easy_apply_limit = AsyncMock(return_value=False)
        applier.job_easy_apply = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("src.job_manager.linkedin.easy_applier_linkedin.emit_event"):
            with patch("src.job_manager.linkedin.easy_applier_linkedin.debug_capture"):
                with pytest.raises(RuntimeError, match="boom"):
                    await applier.apply_to_job(test_job)


class TestJobEasyApply:
    @pytest.mark.asyncio
    async def test_returns_skip_when_no_easy_apply_button(self, applier, test_job):
        applier._is_already_applied = AsyncMock(return_value=False)
        applier._find_easy_apply_button = AsyncMock(return_value=False)
        with patch("src.job_manager.linkedin.easy_applier_linkedin.async_pause"):
            result = await applier.job_easy_apply(test_job)
        assert result[0] == "Skip"

    @pytest.mark.asyncio
    async def test_returns_skip_when_already_applied_status_is_present(self, applier, test_job):
        applier._is_already_applied = AsyncMock(return_value=True)

        result = await applier.job_easy_apply(test_job)

        assert result == ("Skip", "Already applied to this job")

    @pytest.mark.asyncio
    async def test_returns_success_on_complete_application(self, applier, test_job):
        applier._is_already_applied = AsyncMock(return_value=False)
        applier._find_easy_apply_button = AsyncMock(return_value=True)
        applier._click_continue_applying_button = AsyncMock()
        applier.check_for_premium_redirect = AsyncMock(return_value=False)
        applier._fill_application_form = AsyncMock()
        with patch("src.job_manager.linkedin.easy_applier_linkedin.async_pause"):
            with patch("src.job_manager.linkedin.easy_applier_linkedin.capture_page_screenshot"):
                with patch(
                    "src.job_manager.linkedin.easy_applier_linkedin.emit_event"
                ) as emit:
                    result = await applier.job_easy_apply(test_job)
        assert result[0] == "Success"
        emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_unconfirmed_submit_returns_only_unverified_without_completion_event(
        self, applier, test_job
    ):
        applier._is_already_applied = AsyncMock(return_value=False)
        applier._find_easy_apply_button = AsyncMock(return_value=True)
        applier._click_continue_applying_button = AsyncMock()
        applier.check_for_premium_redirect = AsyncMock(return_value=False)
        applier._fill_application_form = AsyncMock(
            side_effect=UnverifiedAfterSubmitError(
                "UNVERIFIED_AFTER_SUBMIT: no independent receipt"
            )
        )

        with (
            patch("src.job_manager.linkedin.easy_applier_linkedin.async_pause"),
            patch("src.job_manager.linkedin.easy_applier_linkedin.capture_page_screenshot"),
            patch("src.job_manager.linkedin.easy_applier_linkedin.emit_event") as emit,
        ):
            result = await applier.job_easy_apply(test_job)

        assert result == (
            "Error",
            "UNVERIFIED_AFTER_SUBMIT: no independent receipt",
        )
        emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_test_mode_discard_never_returns_submitted(self, applier, test_job):
        applier.test_mode = True
        applier._is_already_applied = AsyncMock(return_value=False)
        applier._find_easy_apply_button = AsyncMock(return_value=True)
        applier._click_continue_applying_button = AsyncMock()
        applier.check_for_premium_redirect = AsyncMock(return_value=False)
        applier._fill_application_form = AsyncMock()

        with (
            patch("src.job_manager.linkedin.easy_applier_linkedin.async_pause"),
            patch("src.job_manager.linkedin.easy_applier_linkedin.capture_page_screenshot"),
        ):
            result = await applier.job_easy_apply(test_job)

        assert result == ("Skip", "Test mode: final submission was not sent")

    @pytest.mark.asyncio
    async def test_returns_error_on_unexpected_exception(self, applier, test_job):
        applier._is_already_applied = AsyncMock(return_value=False)
        applier._find_easy_apply_button = AsyncMock(return_value=True)
        applier._click_continue_applying_button = AsyncMock()
        applier.check_for_premium_redirect = AsyncMock(return_value=False)
        applier._fill_application_form = AsyncMock(side_effect=RuntimeError("form error"))
        applier._save_job_application_process = AsyncMock()
        with patch("src.job_manager.linkedin.easy_applier_linkedin.async_pause"):
            with patch("src.job_manager.linkedin.easy_applier_linkedin.capture_page_screenshot"):
                with patch("src.job_manager.linkedin.easy_applier_linkedin.debug_capture"):
                    result = await applier.job_easy_apply(test_job)
        assert result[0] == "Error"
        assert "form error" in result[1]

    @pytest.mark.asyncio
    async def test_returns_skip_when_easy_apply_dialog_does_not_open(self, applier, test_job):
        applier._is_already_applied = AsyncMock(return_value=False)
        applier._find_easy_apply_button = AsyncMock(return_value=True)
        applier._click_continue_applying_button = AsyncMock()
        applier.check_for_premium_redirect = AsyncMock(return_value=False)
        applier._fill_application_form = AsyncMock(
            side_effect=NoInfoException("Easy Apply dialog did not open")
        )
        with patch("src.job_manager.linkedin.easy_applier_linkedin.async_pause"):
            with patch("src.job_manager.linkedin.easy_applier_linkedin.capture_page_screenshot"):
                with patch("src.job_manager.linkedin.easy_applier_linkedin.debug_capture"):
                    result = await applier.job_easy_apply(test_job)

        assert result[0] == "Skip"
        assert "Easy Apply dialog did not open" in result[1]


class TestNextOrSubmit:
    @pytest.mark.asyncio
    async def test_returns_false_on_next_button(self, applier):
        mock_button = AsyncMock()
        applier._repair_invalid_fields_before_navigation = AsyncMock()
        applier._find_next_or_submit_button = AsyncMock(return_value=(mock_button, "next"))
        applier._check_and_fix_errors = AsyncMock(return_value=True)
        result = await applier._next_or_submit()
        assert result is False

    @pytest.mark.asyncio
    async def test_discards_in_test_mode(self, applier):
        applier.test_mode = True
        applier._repair_invalid_fields_before_navigation = AsyncMock()
        mock_button = AsyncMock()
        applier._find_next_or_submit_button = AsyncMock(
            return_value=(mock_button, "submit application")
        )
        applier._unfollow_company = AsyncMock()
        applier._discard_application = AsyncMock()
        with patch("src.job_manager.linkedin.easy_applier_linkedin.async_pause"):
            result = await applier._next_or_submit()
        assert result is True
        applier._discard_application.assert_called_once()

    @pytest.mark.asyncio
    async def test_raises_when_no_button_found(self, applier):
        applier._repair_invalid_fields_before_navigation = AsyncMock()
        applier._find_next_or_submit_button = AsyncMock(return_value=(None, None))
        applier._find_all_form_errors = AsyncMock(return_value=[])
        applier._wait_for_navigation_dom_stability = AsyncMock()
        with pytest.raises(RuntimeError, match="TECHNICAL_FAILURE"):
            await applier._next_or_submit()
        assert applier._find_all_form_errors.await_count >= 1

    @pytest.mark.asyncio
    async def test_closed_page_raises_lifecycle_error_not_missing_button(self, applier):
        applier.page.is_closed = MagicMock(return_value=True)
        with pytest.raises(RuntimeError, match="Target page, context or browser"):
            await applier._find_next_or_submit_button()

    @pytest.mark.asyncio
    async def test_submit_requires_independent_confirmation(self, applier):
        button = AsyncMock()
        applier._repair_invalid_fields_before_navigation = AsyncMock()
        applier._find_next_or_submit_button = AsyncMock(
            return_value=(button, "submit application")
        )
        applier._unfollow_company = AsyncMock()
        applier._check_and_fix_errors = AsyncMock(return_value=True)
        applier._verify_linkedin_submission = AsyncMock(return_value=True)
        with patch("src.job_manager.linkedin.easy_applier_linkedin.async_pause"):
            assert await applier._next_or_submit() is True
        applier._verify_linkedin_submission.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unconfirmed_submit_is_unverified_not_success(self, applier):
        button = AsyncMock()
        applier._repair_invalid_fields_before_navigation = AsyncMock()
        applier._find_next_or_submit_button = AsyncMock(
            return_value=(button, "submit application")
        )
        applier._unfollow_company = AsyncMock()
        applier._check_and_fix_errors = AsyncMock(return_value=True)
        applier._verify_linkedin_submission = AsyncMock(return_value=False)
        with patch("src.job_manager.linkedin.easy_applier_linkedin.async_pause"):
            with pytest.raises(UnverifiedAfterSubmitError, match="UNVERIFIED_AFTER_SUBMIT"):
                await applier._next_or_submit()


class TestCheckAndFixErrors:
    @pytest.mark.asyncio
    async def test_returns_true_when_no_errors(self, applier):
        mock_button = AsyncMock()
        applier._require_navigation_button = AsyncMock(
            return_value=(mock_button, "next")
        )
        applier._find_all_form_errors = AsyncMock(return_value=[])
        applier._wait_for_navigation_dom_stability = AsyncMock()
        result = await applier._check_and_fix_errors("next")
        assert result is True
        mock_button.click.assert_awaited_once_with(timeout=5000)


class TestRobustEasyApplyNavigation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "action",
        ["next", "continue", "review", "submit application"],
    )
    async def test_finds_each_supported_semantic_button(self, applier, action):
        button, _ = configure_navigation_dialog(
            applier,
            [navigation_candidate(action)],
        )

        found, found_action = await applier._find_next_or_submit_button()

        assert found is button
        assert found_action == action

    @pytest.mark.asyncio
    async def test_button_order_change_still_finds_native_next(self, applier):
        button, _ = configure_navigation_dialog(
            applier,
            [
                navigation_candidate("continue", tag="div", role="button"),
                navigation_candidate("next", tag="button"),
            ],
        )

        found, action = await applier._find_next_or_submit_button()

        assert found is button
        assert action == "next"

    @pytest.mark.asyncio
    async def test_nested_artdeco_text_resolves_actual_button(self, applier):
        actual_button, scope = configure_navigation_dialog(
            applier,
            [navigation_candidate("next", source="text", tag="button")],
        )

        found, action = await applier._find_next_or_submit_button()

        assert found is actual_button
        assert action == "next"
        queried_selectors = [call.args[0] for call in scope.locator.call_args_list]
        assert ".artdeco-button__text" not in queried_selectors

    @pytest.mark.asyncio
    async def test_aria_disabled_button_is_ignored(self, applier):
        configure_navigation_dialog(
            applier,
            [
                navigation_candidate(
                    "next",
                    source="aria-label",
                    enabled=False,
                    aria_disabled=True,
                )
            ],
        )

        assert await applier._find_next_or_submit_button() == (None, None)

    @pytest.mark.asyncio
    async def test_aria_label_can_resolve_native_continue_button(self, applier):
        button, _ = configure_navigation_dialog(
            applier,
            [navigation_candidate("continue", source="aria-label")],
        )

        found, action = await applier._find_next_or_submit_button()

        assert found is button
        assert action == "continue"

    @pytest.mark.asyncio
    async def test_role_button_is_bounded_fallback(self, applier):
        button, _ = configure_navigation_dialog(
            applier,
            [navigation_candidate("review", tag="div", role="button")],
        )

        found, action = await applier._find_next_or_submit_button()

        assert found is button
        assert action == "review"

    @pytest.mark.asyncio
    async def test_initially_disabled_button_is_requeried_when_enabled(self, applier):
        button = AsyncMock()
        applier._repair_invalid_fields_before_navigation = AsyncMock()
        applier._wait_for_navigation_dom_stability = AsyncMock()
        applier._find_all_form_errors = AsyncMock(return_value=[])
        applier._find_next_or_submit_button = AsyncMock(
            side_effect=[(None, None), (button, "next")]
        )

        found, action = await applier._require_navigation_button()

        assert found is button
        assert action == "next"
        assert applier._find_next_or_submit_button.await_count == 2

    @pytest.mark.asyncio
    async def test_detached_locator_is_requeried_once_then_clicks_fresh_button(self, applier):
        stale_button = AsyncMock()
        stale_button.click.side_effect = RuntimeError("Element is not attached to the DOM")
        fresh_button = AsyncMock()
        applier._require_navigation_button = AsyncMock(
            side_effect=[(stale_button, "next"), (fresh_button, "next")]
        )
        applier._wait_for_navigation_dom_stability = AsyncMock()
        applier._find_all_form_errors = AsyncMock(return_value=[])

        assert await applier._check_and_fix_errors("next") is True
        stale_button.click.assert_awaited_once_with(timeout=5000)
        fresh_button.click.assert_awaited_once_with(timeout=5000)
        assert applier._require_navigation_button.await_count == 2

    @pytest.mark.asyncio
    async def test_playwright_timeout_gets_one_bounded_requery(self, applier):
        stale_button = AsyncMock()
        stale_button.click.side_effect = PlaywrightTimeoutError("re-render timeout")
        fresh_button = AsyncMock()
        applier._require_navigation_button = AsyncMock(
            side_effect=[(stale_button, "review"), (fresh_button, "review")]
        )
        applier._wait_for_navigation_dom_stability = AsyncMock()
        applier._find_all_form_errors = AsyncMock(return_value=[])

        assert await applier._check_and_fix_errors("review") is True
        assert applier._require_navigation_button.await_count == 2
        assert stale_button.click.await_count == 1
        assert fresh_button.click.await_count == 1

    @pytest.mark.asyncio
    async def test_submit_timeout_never_retries_submit(self, applier):
        submit_button = AsyncMock()
        submit_button.click.side_effect = PlaywrightTimeoutError("ambiguous submit timeout")
        applier._require_navigation_button = AsyncMock(
            return_value=(submit_button, "submit application")
        )

        with pytest.raises(UnverifiedAfterSubmitError, match="UNVERIFIED_AFTER_SUBMIT"):
            await applier._check_and_fix_errors(
                "submit application",
                final_submit=True,
            )

        submit_button.click.assert_awaited_once_with(timeout=5000)
        applier._require_navigation_button.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_validation_is_repaired_before_navigation_query(self, applier):
        order = []
        button = AsyncMock()
        applier._repair_invalid_fields_before_navigation = AsyncMock(
            side_effect=lambda: order.append("repair")
        )
        applier._wait_for_navigation_dom_stability = AsyncMock(
            side_effect=lambda: order.append("stabilize")
        )
        applier._find_next_or_submit_button = AsyncMock(
            side_effect=lambda: order.append("query") or (button, "continue")
        )

        assert await applier._require_navigation_button() == (button, "continue")
        assert order == ["repair", "stabilize", "query"]

    def test_navigation_code_has_no_positional_or_span_click_dependency(self):
        source = "\n".join(
            [
                inspect.getsource(LinkedInEasyApplier._find_next_or_submit_button),
                inspect.getsource(LinkedInEasyApplier._check_and_fix_errors),
            ]
        )
        assert ".nth(" not in source
        assert ".artdeco-button__text" not in source


class TestSubmissionVerification:
    @pytest.mark.asyncio
    async def test_visible_linkedin_receipt_confirms_submission(self, applier):
        applier.page.is_closed = MagicMock(return_value=False)
        applier._mark_already_applied_status = AsyncMock(return_value=False)
        locator = MagicMock()
        locator.evaluate_all = AsyncMock(
            return_value=["Application sent. Your application was sent to Example."]
        )
        applier.page.locator = MagicMock(return_value=locator)
        with patch("src.job_manager.linkedin.easy_applier_linkedin.async_pause"):
            assert await applier._verify_linkedin_submission(attempts=1) is True

    @pytest.mark.asyncio
    async def test_absence_of_receipt_does_not_confirm_submission(self, applier):
        applier.page.is_closed = MagicMock(return_value=False)
        applier._mark_already_applied_status = AsyncMock(return_value=False)
        locator = MagicMock()
        locator.evaluate_all = AsyncMock(return_value=["Review your application"])
        applier.page.locator = MagicMock(return_value=locator)
        with patch("src.job_manager.linkedin.easy_applier_linkedin.async_pause"):
            assert await applier._verify_linkedin_submission(attempts=1) is False

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts_with_errors(self, applier):
        applier._find_all_form_errors = AsyncMock(return_value=["Field is required"])
        applier._fill_textbox_question_errors = AsyncMock()
        with pytest.raises(RuntimeError, match="TECHNICAL_FAILURE"):
            await applier._check_and_fix_errors("next")

    @pytest.mark.asyncio
    async def test_fixes_errors_then_succeeds(self, applier):
        mock_button = AsyncMock()
        applier._require_navigation_button = AsyncMock(
            return_value=(mock_button, "next")
        )
        applier._find_all_form_errors = AsyncMock(return_value=["error"])
        applier._repair_invalid_fields_before_navigation = AsyncMock()
        applier._wait_for_navigation_dom_stability = AsyncMock()

        result = await applier._check_and_fix_errors("next")

        assert result is None
        applier._repair_invalid_fields_before_navigation.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_new_step_required_text_is_filled_by_main_loop_before_repair(self, applier):
        """Resume Next must not mistake the next blank page for rejected navigation."""
        button = AsyncMock()
        applier._require_navigation_button = AsyncMock(return_value=(button, "next"))
        applier._wait_for_navigation_dom_stability = AsyncMock()
        applier._capture_easy_apply_form_state = AsyncMock(
            side_effect=[
                {"structure_fingerprint": "resume-radio", "step": "2/4"},
                {
                    "structure_fingerprint": "required-contact-text", "step": "3/4",
                    "control_count": 1, "has_invalid": True, "categories": ("SHORT_TEXT",),
                },
            ]
        )
        applier._find_all_form_errors = AsyncMock(
            return_value=["required-unanswered:text", "browser-invalid:text"]
        )
        applier._repair_invalid_fields_before_navigation = AsyncMock()

        assert await applier._check_and_fix_errors("next") is True
        button.click.assert_awaited_once_with(timeout=5000)
        applier._find_all_form_errors.assert_not_awaited()
        applier._repair_invalid_fields_before_navigation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_same_structure_with_new_invalid_state_still_repairs(self, applier):
        button = AsyncMock()
        applier._require_navigation_button = AsyncMock(return_value=(button, "next"))
        applier._wait_for_navigation_dom_stability = AsyncMock()
        applier._capture_easy_apply_form_state = AsyncMock(
            side_effect=[
                {"structure_fingerprint": "same-fields", "fingerprint": "valid"},
                {"structure_fingerprint": "same-fields", "fingerprint": "invalid"},
            ]
        )
        applier._find_all_form_errors = AsyncMock(return_value=["browser-invalid:text"])
        applier._repair_invalid_fields_before_navigation = AsyncMock()

        assert await applier._check_and_fix_errors("next") is None
        applier._repair_invalid_fields_before_navigation.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_submit_validation_never_uses_step_transition_shortcut(self, applier):
        button = AsyncMock()
        applier._require_navigation_button = AsyncMock(
            return_value=(button, "submit application")
        )
        applier._wait_for_navigation_dom_stability = AsyncMock()
        applier._capture_easy_apply_form_state = AsyncMock()
        applier._find_all_form_errors = AsyncMock(return_value=["browser-invalid:text"])
        applier._repair_invalid_fields_before_navigation = AsyncMock()

        assert await applier._check_and_fix_errors("submit application", final_submit=True) is None
        assert applier.final_submit_attempted is False
        applier._capture_easy_apply_form_state.assert_not_awaited()
        applier._repair_invalid_fields_before_navigation.assert_awaited_once()


class TestIsUploadField:
    @pytest.mark.asyncio
    async def test_detects_file_input(self, applier):
        element = MagicMock()
        file_inputs_loc = AsyncMock()
        file_inputs_loc.all = AsyncMock(return_value=[MagicMock()])
        upload_containers_loc = AsyncMock()
        upload_containers_loc.all = AsyncMock(return_value=[])
        upload_buttons_loc = AsyncMock()
        upload_buttons_loc.all = AsyncMock(return_value=[])
        element.locator = MagicMock(
            side_effect=lambda sel: {
                "xpath=.//input[@type='file']": file_inputs_loc,
                ".js-jobs-document-upload__container": upload_containers_loc,
                ".jobs-document-upload__upload-button": upload_buttons_loc,
            }[sel]
        )
        result = await applier._is_upload_field(element)
        assert result is True

    @pytest.mark.asyncio
    async def test_not_upload_when_no_indicators(self, applier):
        element = MagicMock()
        empty_loc = AsyncMock()
        empty_loc.all = AsyncMock(return_value=[])
        element.locator = MagicMock(return_value=empty_loc)
        result = await applier._is_upload_field(element)
        assert result is False


class TestAlreadyAppliedDetection:
    @pytest.mark.asyncio
    async def test_detects_application_submitted_status(self, applier):
        with patch(
            "src.job_manager.linkedin.easy_applier_linkedin.find_element_safely",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        ):
            result = await applier._is_already_applied()

        assert result is True

    @pytest.mark.asyncio
    async def test_extracts_application_submitted_date(self, applier):
        date_locator = AsyncMock()
        date_locator.count = AsyncMock(return_value=1)
        date_locator.inner_text = AsyncMock(return_value="18 hours ago")
        date_locator_container = MagicMock()
        date_locator_container.first = date_locator
        submitted_element = MagicMock()
        submitted_element.locator.return_value = date_locator_container

        with patch(
            "src.job_manager.linkedin.easy_applier_linkedin.find_element_safely",
            new_callable=AsyncMock,
            return_value=submitted_element,
        ) as mock_find:
            result = await applier._is_already_applied()

        assert result is True
        assert applier.already_applied_at_text == "18 hours ago"
        assert applier.already_applied_at is not None
        assert not mock_find.call_args.args[1].startswith("xpath=")

    @pytest.mark.asyncio
    async def test_returns_false_when_application_status_missing(self, applier):
        with patch(
            "src.job_manager.linkedin.easy_applier_linkedin.find_element_safely",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await applier._is_already_applied()

        assert result is False


class TestUploadFields:
    @pytest.mark.asyncio
    async def test_generates_resume_even_when_ready_made_resume_exists(self, applier, tmp_path):
        ready_made_resume = tmp_path / "existing.pdf"
        ready_made_resume.write_bytes(b"existing")
        applier.ready_made_resume_path = ready_made_resume

        upload_element = AsyncMock()
        upload_element.get_attribute = AsyncMock(return_value="resume-upload")
        upload_element.evaluate = AsyncMock()
        upload_element.set_input_files = AsyncMock()

        parent = AsyncMock()
        parent.text_content = AsyncMock(return_value="Resume")
        upload_element.locator = MagicMock(return_value=MagicMock(first=parent))

        element = MagicMock()
        upload_locator = MagicMock()
        upload_locator.all = AsyncMock(return_value=[upload_element])
        element.locator = MagicMock(return_value=upload_locator)

        applier._create_and_upload_resume = AsyncMock()
        job = Job(job_title="Engineer", company_name="Tech")

        with patch(
            "src.job_manager.linkedin.easy_applier_linkedin.find_element_safely",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await applier._handle_upload_fields(element, job, set())

        applier._create_and_upload_resume.assert_called_once_with(upload_element, job)
        upload_element.set_input_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_records_ready_made_resume_path_when_uploaded(self, applier, tmp_path):
        ready_made_resume = tmp_path / "existing.pdf"
        ready_made_resume.write_bytes(b"existing")
        applier.ready_made_resume_path = ready_made_resume
        applier.resume_generator_manager = None

        upload_element = AsyncMock()
        upload_element.get_attribute = AsyncMock(return_value="resume-upload")
        upload_element.evaluate = AsyncMock()
        upload_element.set_input_files = AsyncMock()

        parent = AsyncMock()
        parent.text_content = AsyncMock(return_value="Resume")
        upload_element.locator = MagicMock(return_value=MagicMock(first=parent))

        element = MagicMock()
        upload_locator = MagicMock()
        upload_locator.all = AsyncMock(return_value=[upload_element])
        element.locator = MagicMock(return_value=upload_locator)

        job = Job(job_title="Engineer", company_name="Tech")

        with (
            patch(
                "src.job_manager.linkedin.easy_applier_linkedin.find_element_safely",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.job_manager.linkedin.easy_applier_linkedin.async_pause",
                new_callable=AsyncMock,
            ),
        ):
            await applier._handle_upload_fields(element, job, set())

        expected_path = os.path.abspath(str(ready_made_resume.resolve()))
        upload_element.set_input_files.assert_called_once_with(expected_path)
        assert applier.submitted_resume_path == expected_path

    @pytest.mark.asyncio
    async def test_uploads_photo_when_image_file_input_is_detected(self, applier):
        upload_element = AsyncMock()

        async def get_attribute_side_effect(name):
            if name == "id":
                return "photo-upload"
            if name == "accept":
                return "image/jpg,image/jpeg,image/gif,image/png"
            return None

        upload_element.get_attribute.side_effect = get_attribute_side_effect
        upload_element.evaluate = AsyncMock()

        parent = AsyncMock()
        parent.text_content = AsyncMock(return_value="Photo")
        upload_element.locator = MagicMock(return_value=MagicMock(first=parent))

        element = MagicMock()
        upload_locator = MagicMock()
        upload_locator.all = AsyncMock(return_value=[upload_element])
        element.locator = MagicMock(return_value=upload_locator)

        applier._create_and_upload_photo = AsyncMock()
        job = Job(job_title="Engineer", company_name="Tech")

        with patch(
            "src.job_manager.linkedin.easy_applier_linkedin.find_element_safely",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await applier._handle_upload_fields(element, job, set())

        applier._create_and_upload_photo.assert_called_once_with(upload_element, job)


class TestCreateAndUploadPhoto:
    @pytest.mark.asyncio
    async def test_uploads_ready_made_photo_when_configured(self, applier, tmp_path):
        ready_made_photo = tmp_path / "profile.jpg"
        ready_made_photo.write_bytes(b"photo-bytes")
        applier.ready_made_photo_path = ready_made_photo

        upload_element = AsyncMock()
        upload_element.set_input_files = AsyncMock()
        job = Job(job_title="Engineer", company_name="Tech")

        with patch(
            "src.job_manager.linkedin.easy_applier_linkedin.async_pause",
            new_callable=AsyncMock,
        ):
            await applier._create_and_upload_photo(upload_element, job)

        upload_element.set_input_files.assert_called_once_with(
            os.path.abspath(str(ready_made_photo.resolve()))
        )

    @pytest.mark.asyncio
    async def test_rejects_ready_made_photo_with_unsupported_extension(self, applier, tmp_path):
        ready_made_photo = tmp_path / "profile.bmp"
        ready_made_photo.write_bytes(b"photo-bytes")
        applier.ready_made_photo_path = ready_made_photo

        upload_element = AsyncMock()
        job = Job(job_title="Engineer", company_name="Tech")

        with pytest.raises(ValueError, match="Photo file format is not allowed"):
            await applier._create_and_upload_photo(upload_element, job)


class TestDeduplicateQuestionText:
    def test_deduplicates_repeated_string(self, applier):
        text = "hello worldhello world"
        result = applier._deduplicate_question_text(text)
        assert result == "hello world"

    def test_deduplicates_newline_duplicates(self, applier):
        text = "question\nquestion"
        result = applier._deduplicate_question_text(text)
        assert result == "question"

    def test_leaves_unique_text_intact(self, applier):
        text = "line one\nline two"
        result = applier._deduplicate_question_text(text)
        assert result == "line one\nline two"

    def test_empty_string(self, applier):
        result = applier._deduplicate_question_text("")
        assert result == ""


class TestRadioQuestionParsing:
    @staticmethod
    def _section(snapshot, combined_text):
        radios = [AsyncMock(), AsyncMock()]
        locator = MagicMock()
        locator.evaluate_all = AsyncMock(return_value=["yes-id", "no-id"])
        locator.all = AsyncMock(return_value=radios)
        section = MagicMock()
        section.locator.return_value = locator
        section.text_content = AsyncMock(return_value=combined_text)
        section.evaluate = AsyncMock(return_value=snapshot)
        return section

    @pytest.mark.asyncio
    async def test_handler_resolves_structured_high_school_radio_before_llm(self, applier):
        section = self._section(
            {
                "question": "Have you completed the following level of education: High School Diploma? *",
                "required": True,
                "options": [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}],
            },
            "Have you completed the following level of education: High School Diploma? * Yes No",
        )
        applier._is_resume_picker_radiogroup = AsyncMock(return_value=False)
        applier._select_radio = AsyncMock()
        applier.gpt_answerer.resume_structured = {
            "education_details": [{"education_level": "High School Diploma"}]
        }
        applier.application_profile = {}

        assert await applier._find_and_handle_radio_question(section) is True
        assert applier._select_radio.await_args.args[2] == "Yes"
        applier.gpt_answerer.select_one_answer_from_options.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_radio_uses_semantic_fallback_instead_of_exact_cache_skip(self, applier):
        section = self._section(
            {
                "question": "Do you prefer cats or dogs?",
                "options": [{"label": "Cats"}, {"label": "Dogs"}],
            },
            "Do you prefer cats or dogs? Cats Dogs",
        )
        applier._is_resume_picker_radiogroup = AsyncMock(return_value=False)
        applier._select_radio = AsyncMock()
        applier._save_questions = MagicMock()
        applier._load_questions = MagicMock(return_value=[])
        applier.gpt_answerer.resume_structured = {}
        applier.application_profile = {}
        applier.gpt_answerer.select_one_answer_from_options.return_value = "Cats"

        assert await applier._find_and_handle_radio_question(section) is True
        applier.gpt_answerer.select_one_answer_from_options.assert_called_once()
        assert applier._select_radio.await_args.args[2] == "Cats"

    @pytest.mark.asyncio
    async def test_handler_accepts_visible_role_radio_wrapper_text(self, applier):
        section = self._section(
            {
                "question": "Are you authorized to work in the United States?",
                "required": True,
                "options": [
                    {"label": "Yes, I am authorized", "value": "yes"},
                    {"label": "No, I am not authorized", "value": "no"},
                ],
            },
            "Are you authorized to work in the United States? Yes, I am authorized No, I am not authorized",
        )
        applier._is_resume_picker_radiogroup = AsyncMock(return_value=False)
        applier._select_radio = AsyncMock()
        applier.gpt_answerer.resume_structured = {}
        applier.application_profile = {}
        applier.gpt_answerer.select_one_answer_from_options.return_value = "Yes, I am authorized"

        assert await applier._find_and_handle_radio_question(section) is True
        assert applier._select_radio.await_args.args[2] == "Yes, I am authorized"


class TestIsNumericField:
    @pytest.mark.asyncio
    async def test_detects_number_type(self, applier):
        field = AsyncMock()
        field.get_attribute = AsyncMock(side_effect=lambda attr: "number" if attr == "type" else "")
        result = await applier._is_numeric_field(field)
        assert result is True

    @pytest.mark.asyncio
    async def test_detects_numeric_in_id(self, applier):
        field = AsyncMock()
        field.get_attribute = AsyncMock(
            side_effect=lambda attr: "text" if attr == "type" else "numeric-experience"
        )
        result = await applier._is_numeric_field(field)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_for_plain_text(self, applier):
        field = AsyncMock()
        field.get_attribute = AsyncMock(
            side_effect=lambda attr: "text" if attr == "type" else "first-name"
        )
        result = await applier._is_numeric_field(field)
        assert result is False

    @pytest.mark.asyncio
    async def test_detects_keyword_in_question_text(self, applier):
        field = AsyncMock()
        field.get_attribute = AsyncMock(side_effect=lambda attr: "text" if attr == "type" else "")
        result = await applier._is_numeric_field(field, "what is your expected salary?")
        assert result is True

    @pytest.mark.asyncio
    async def test_keyword_not_matched_as_substring(self, applier):
        field = AsyncMock()
        field.get_attribute = AsyncMock(side_effect=lambda attr: "text" if attr == "type" else "")
        # "rate" is a substring of "demonstrates" — must not match
        question = "include github code samples and any evaluation or benchmark work that demonstrates your technical rigor."
        result = await applier._is_numeric_field(field, question)
        assert result is False

    @pytest.mark.asyncio
    async def test_keyword_matched_as_whole_word(self, applier):
        field = AsyncMock()
        field.get_attribute = AsyncMock(side_effect=lambda attr: "text" if attr == "type" else "")
        result = await applier._is_numeric_field(field, "what is your hourly rate?")
        assert result is True


class TestSelectDropdownOption:
    @pytest.mark.asyncio
    async def test_selects_by_label_directly(self, applier):
        element = AsyncMock()
        element.select_option = AsyncMock()
        await applier._select_dropdown_option(element, "Full-time")
        element.select_option.assert_called_once_with(label="Full-time", timeout=3000)

    @pytest.mark.asyncio
    async def test_falls_back_to_value_match(self, applier):
        element = AsyncMock()
        element.select_option = AsyncMock(side_effect=[Exception("no label"), None])
        element.locator = MagicMock()
        element.locator.return_value.evaluate_all = AsyncMock(
            return_value=[{"label": "Full-time", "value": "ft"}]
        )
        await applier._select_dropdown_option(element, "Full-time")
        element.select_option.assert_called_with(value="ft")

    @pytest.mark.asyncio
    async def test_normalized_match_ignores_case(self, applier):
        element = AsyncMock()
        element.select_option = AsyncMock(side_effect=[Exception("no label"), None])
        element.locator = MagicMock()
        element.locator.return_value.evaluate_all = AsyncMock(
            return_value=[{"label": "  FULL-TIME  ", "value": "ft"}]
        )
        await applier._select_dropdown_option(element, "full-time")
        element.select_option.assert_called_with(value="ft")


class TestFindAllFormErrors:
    @pytest.mark.asyncio
    async def test_returns_unique_error_texts(self, applier):
        applier.page = MagicMock()
        loc = MagicMock()
        loc.evaluate_all = AsyncMock(
            return_value=["Field is required", "Field is required", "Invalid value"]
        )
        applier.page.locator = MagicMock(return_value=loc)
        errors = await applier._find_all_form_errors()
        assert "Field is required" in errors
        assert "Invalid value" in errors
        assert errors.count("Field is required") == 1

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_errors(self, applier):
        applier.page = MagicMock()
        loc = MagicMock()
        loc.evaluate_all = AsyncMock(return_value=[])
        applier.page.locator = MagicMock(return_value=loc)
        errors = await applier._find_all_form_errors()
        assert errors == []

    @pytest.mark.asyncio
    async def test_required_unanswered_control_is_reported_without_value(self, applier):
        visible_error_loc = MagicMock()
        visible_error_loc.evaluate_all = AsyncMock(return_value=[])
        invalid_control_loc = MagicMock()
        invalid_control_loc.evaluate_all = AsyncMock(
            return_value=[
                {
                    "kind": "radio",
                    "required": True,
                    "unanswered": True,
                    "ariaInvalid": False,
                    "browserInvalid": False,
                }
            ]
        )

        def locator(selector):
            if "dialog-content" in selector:
                return invalid_control_loc
            return visible_error_loc

        applier.page = MagicMock()
        applier.page.locator = MagicMock(side_effect=locator)

        assert await applier._find_all_form_errors() == ["required-unanswered:radio"]

    @pytest.mark.asyncio
    async def test_meaningful_required_select_at_index_zero_is_not_unanswered(self, applier):
        visible_error_loc = MagicMock()
        visible_error_loc.evaluate_all = AsyncMock(return_value=[])
        invalid_control_loc = MagicMock()
        invalid_control_loc.evaluate_all = AsyncMock(
            return_value=[
                {
                    "kind": "select",
                    "required": True,
                    "unanswered": False,
                    "ariaInvalid": False,
                    "browserInvalid": False,
                    "selectedIndex": 0,
                    "selectedDisabled": False,
                    "hasValue": True,
                    "selectedPlaceholder": False,
                }
            ]
        )

        def locator(selector):
            if "dialog-content" in selector:
                return invalid_control_loc
            return visible_error_loc

        applier.page = MagicMock()
        applier.page.locator = MagicMock(side_effect=locator)

        assert await applier._find_all_form_errors() == []

    def test_index_zero_placeholder_remains_unanswered(self, applier):
        assert applier._required_select_is_unanswered(
            {
                "required": True,
                "selectedIndex": 0,
                "selectedDisabled": False,
                "hasValue": False,
                "selectedPlaceholder": True,
            }
        )

    @pytest.mark.asyncio
    async def test_talentoma_state_continues_through_ordinary_next(self, applier):
        button = AsyncMock()
        applier._require_navigation_button = AsyncMock(
            side_effect=[(button, "next"), (button, "next")]
        )
        applier._find_all_form_errors = AsyncMock(return_value=[])
        applier._wait_for_navigation_dom_stability = AsyncMock()

        assert await applier._next_or_submit() is False
        button.click.assert_awaited_once_with(timeout=5000)

    @pytest.mark.asyncio
    async def test_required_enum_error_triggers_one_fresh_dom_fill(self, applier):
        applier._find_all_form_errors = AsyncMock(
            side_effect=[["required-unanswered:select"], []]
        )
        applier._fill_textbox_question_errors = AsyncMock(return_value=False)
        applier._fill_up = AsyncMock()

        await applier._repair_invalid_fields_before_navigation()

        applier._fill_up.assert_awaited_once_with(applier.current_job)
        assert applier._force_fresh_question_resolution is True


class TestModernTextboxValidationRepair:
    @pytest.mark.asyncio
    async def test_native_required_textbox_is_found_without_legacy_error_wrapper(self, applier):
        field = MagicMock()
        fields = MagicMock()
        fields.all = AsyncMock(return_value=[])
        fields.evaluate_all = AsyncMock(return_value=[{"index": 0, "invalid": True}])
        fields.nth.return_value = field
        applier.page.locator = MagicMock(return_value=fields)
        section = MagicMock()
        applier._widen_to_text_input_container = AsyncMock(return_value=section)
        applier._extract_semantic_question = AsyncMock(return_value="First name")

        errors = await applier._find_textbox_question_errors()

        assert errors == [(field, "First name", "native-invalid:text")]
        applier._extract_semantic_question.assert_awaited_once_with(field, section)

    @pytest.mark.asyncio
    async def test_native_control_without_identifiable_question_is_not_guessed(self, applier):
        fields = MagicMock()
        fields.evaluate_all = AsyncMock(return_value=[{"index": 0, "invalid": True}])
        applier.page.locator = MagicMock(return_value=fields)
        applier._widen_to_text_input_container = AsyncMock(return_value=MagicMock())
        applier._extract_semantic_question = AsyncMock(return_value="")

        assert await applier._find_invalid_native_textboxes() == []

    @pytest.mark.asyncio
    async def test_unmapped_native_validation_is_technical_failure(self, applier):
        applier._find_all_form_errors = AsyncMock(
            return_value=["required-unanswered:text", "browser-invalid:text"]
        )
        applier._fill_textbox_question_errors = AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="TECHNICAL_FAILURE"):
            await applier._repair_invalid_fields_before_navigation()
        applier._fill_textbox_question_errors.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_known_question_without_source_fact_still_requires_human(self, applier):
        element = AsyncMock()
        applier._find_textbox_question_errors = AsyncMock(
            return_value=[(element, "How many years of networking experience?", "native-invalid:text")]
        )
        applier._widen_to_text_input_container = AsyncMock(return_value=MagicMock())
        applier._extract_field_spec = AsyncMock(return_value=field_spec_from_snapshot(
            "How many years of networking experience?", {"type": "number", "required": True}
        ))
        applier.gpt_answerer.resume_structured = {}
        applier.application_profile = {}

        with pytest.raises(NoInfoException, match="required candidate fact is not established"):
            await applier._fill_textbox_question_errors()
        applier.gpt_answerer.answer_question_textual_wide_range_with_error.assert_not_called()
        element.fill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_structure_fingerprint_excludes_validity_but_tracks_new_questions(self, applier):
        dialog = MagicMock()
        dialog.count = AsyncMock(return_value=1)
        dialog.evaluate = AsyncMock(side_effect=[
            {"step": "2/4", "controls": [{"question": "First name", "type": "text", "required": True, "invalid": False}]},
            {"step": "2/4", "controls": [{"question": "First name", "type": "text", "required": True, "invalid": True}]},
            {"step": "3/4", "controls": [{"question": "Resume", "type": "radio", "required": True, "invalid": False}]},
        ])
        applier.page.locator = MagicMock(return_value=MagicMock(first=dialog))

        valid = await applier._capture_easy_apply_form_state(applier.current_job)
        invalid = await applier._capture_easy_apply_form_state(applier.current_job)
        next_step = await applier._capture_easy_apply_form_state(applier.current_job)

        assert valid["fingerprint"] != invalid["fingerprint"]
        assert valid["structure_fingerprint"] == invalid["structure_fingerprint"]
        assert invalid["structure_fingerprint"] != next_step["structure_fingerprint"]
        assert valid["control_count"] == 1
        assert "First name" not in repr(valid)


class TestHandleTermsOfService:
    @pytest.mark.asyncio
    async def test_clicks_terms_of_service_label(self, applier):
        label = AsyncMock()
        label.text_content = AsyncMock(return_value="I agree to the terms of service")
        label.click = AsyncMock()

        checkbox_locator = MagicMock()
        checkbox_locator.all = AsyncMock(return_value=["checkbox"])
        checkbox_locator.first = label

        section = MagicMock()
        section.locator = MagicMock(return_value=checkbox_locator)

        result = await applier._handle_terms_of_service(section)

        assert result is True
        label.click.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_for_non_tos_label(self, applier):
        label = AsyncMock()
        label.text_content = AsyncMock(return_value="Enter your first name")

        section = MagicMock()
        section.locator = MagicMock(return_value=MagicMock(first=label))

        result = await applier._handle_terms_of_service(section)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self, applier):
        section = MagicMock()
        section.locator = MagicMock(side_effect=Exception("locator error"))
        result = await applier._handle_terms_of_service(section)
        assert result is False


class TestFillApplicationForm:
    @pytest.mark.asyncio
    async def test_loops_until_submitted(self, applier, test_job):
        call_count = 0

        async def mock_next_or_submit():
            nonlocal call_count
            call_count += 1
            return call_count >= 2

        applier._fill_up = AsyncMock()
        applier._next_or_submit = mock_next_or_submit
        await applier._fill_application_form(test_job)
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_calls_pause_checker(self, applier, test_job):
        pause_checker = AsyncMock()
        applier.pause_checker = pause_checker
        applier._fill_up = AsyncMock()
        applier._next_or_submit = AsyncMock(return_value=True)
        await applier._fill_application_form(test_job)
        pause_checker.assert_called_once()

    @pytest.mark.asyncio
    async def test_repeated_same_page_gets_one_fresh_retry_then_stops(self, applier, test_job):
        state = {
            "fingerprint": "same",
            "job_id": "123456",
            "step": "3/5",
            "categories": ("YEARS_EXPERIENCE",),
            "has_invalid": True,
        }
        applier._capture_easy_apply_form_state = AsyncMock(return_value=state)
        applier._fill_up = AsyncMock()
        applier._next_or_submit = AsyncMock(return_value=False)

        with pytest.raises(NoInfoException, match="stalled"):
            await applier._fill_application_form(test_job)

        assert applier._fill_up.await_count == 2
        assert applier._next_or_submit.await_count == 2

    @pytest.mark.asyncio
    async def test_resolution_failure_rereads_dom_once_without_cache(self, applier, test_job):
        state = {
            "fingerprint": "same",
            "job_id": "123456",
            "step": "3/5",
            "categories": ("YEARS_EXPERIENCE",),
            "has_invalid": True,
        }
        applier._capture_easy_apply_form_state = AsyncMock(return_value=state)
        seen_fresh_flags = []

        async def fill(_job):
            seen_fresh_flags.append(applier._force_fresh_question_resolution)
            if len(seen_fresh_flags) == 1:
                raise NoInfoException("initial stale parse")

        applier._fill_up = AsyncMock(side_effect=fill)
        applier._next_or_submit = AsyncMock(return_value=True)

        await applier._fill_application_form(test_job)

        assert seen_fresh_flags == [False, True]
        assert applier._capture_easy_apply_form_state.await_count == 2

    @pytest.mark.asyncio
    async def test_unsupported_fact_retries_once_and_never_loops(self, applier, test_job):
        state = {
            "fingerprint": "saas-years",
            "job_id": "123456",
            "step": "3/5",
            "categories": ("YEARS_EXPERIENCE",),
            "has_invalid": True,
        }
        applier._capture_easy_apply_form_state = AsyncMock(return_value=state)
        applier._fill_up = AsyncMock(
            side_effect=NoInfoException("No info found for question: SaaS years")
        )
        applier._next_or_submit = AsyncMock()

        with pytest.raises(NoInfoException, match="SaaS years"):
            await applier._fill_application_form(test_job)

        assert applier._fill_up.await_count == 2
        applier._next_or_submit.assert_not_awaited()

    def test_stall_state_contains_no_raw_question_or_answer(self):
        state = {
            "fingerprint": "opaque-hash",
            "job_id": "123456",
            "step": "3/5",
            "categories": ("SHORT_TEXT",),
            "has_invalid": True,
        }
        assert set(state) == {
            "fingerprint",
            "job_id",
            "step",
            "categories",
            "has_invalid",
        }
        assert "question" not in state
        assert "answer" not in state


class TestTextboxCaching:
    @pytest.mark.asyncio
    async def test_reuses_cached_answer_when_field_type_changed(self, applier):
        text_field = AsyncMock()
        text_field.get_attribute = AsyncMock(
            side_effect=lambda attr: "text" if attr == "type" else None
        )
        text_field.fill = AsyncMock()

        label = AsyncMock()
        label.text_content = AsyncMock(return_value="Email Address")

        applier.all_questions = [
            Question(
                question="email address",
                question_type="dropdown",
                answer="  candidate@example.invalid  ",
            )
        ]

        with (
            patch(
                "src.job_manager.linkedin.easy_applier_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[text_field],
            ),
            patch(
                "src.job_manager.linkedin.easy_applier_linkedin.find_element_safely",
                new_callable=AsyncMock,
                side_effect=[label],
            ),
            patch.object(applier, "_process_autocomplete_suggestions", new_callable=AsyncMock),
        ):
            result = await applier._find_and_handle_textbox_question(MagicMock())

        assert result is True
        text_field.fill.assert_called_once_with("candidate@example.invalid")
        applier.gpt_answerer.answer_question_textual_wide_range.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_cached_no_info_and_uses_resume_answer(self, applier):
        text_field = AsyncMock()
        text_field.get_attribute = AsyncMock(
            side_effect=lambda attr: "text" if attr == "type" else None
        )
        text_field.fill = AsyncMock()

        label = AsyncMock()
        label.text_content = AsyncMock(return_value="Middle name")
        section = MagicMock()
        section.locator.return_value.all = AsyncMock(return_value=[label])

        applier.all_questions = [
            Question(question="middle name", question_type="textbox", answer=" No info")
        ]
        applier.gpt_answerer.answer_question_textual_wide_range.return_value = "Example"

        with (
            patch(
                "src.job_manager.linkedin.easy_applier_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[text_field],
            ),
            patch(
                "src.job_manager.linkedin.easy_applier_linkedin.find_element_safely",
                new_callable=AsyncMock,
                side_effect=[label],
            ),
            patch.object(applier, "_process_autocomplete_suggestions", new_callable=AsyncMock),
        ):
            result = await applier._find_and_handle_textbox_question(section)

        assert result is True
        text_field.fill.assert_called_once_with("Example")
        applier.gpt_answerer.answer_question_textual_wide_range.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_optional_textbox_when_no_info_available(self, applier):
        text_field = AsyncMock()
        text_field.get_attribute = AsyncMock(
            side_effect=lambda attr: "text" if attr == "type" else None
        )
        text_field.fill = AsyncMock()

        label = AsyncMock()
        label.text_content = AsyncMock(return_value="Middle name")
        section = MagicMock()
        section.locator.return_value.all = AsyncMock(return_value=[label])

        applier.gpt_answerer.answer_question_textual_wide_range.return_value = "No info"

        with (
            patch(
                "src.job_manager.linkedin.easy_applier_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[text_field],
            ),
            patch(
                "src.job_manager.linkedin.easy_applier_linkedin.find_element_safely",
                new_callable=AsyncMock,
                side_effect=[label],
            ),
        ):
            result = await applier._find_and_handle_textbox_question(section)

        assert result is True
        text_field.fill.assert_not_called()

    @pytest.mark.asyncio
    async def test_required_textbox_still_fails_when_no_info_available(self, applier):
        text_field = AsyncMock()
        text_field.get_attribute = AsyncMock(
            side_effect=lambda attr: "true" if attr == "aria-required" else None
        )
        text_field.fill = AsyncMock()

        label = AsyncMock()
        label.text_content = AsyncMock(return_value="Legal first name")
        section = MagicMock()
        section.locator.return_value.all = AsyncMock(return_value=[label])

        applier.gpt_answerer.answer_question_textual_wide_range.return_value = "No info"

        with (
            patch(
                "src.job_manager.linkedin.easy_applier_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[text_field],
            ),
            patch(
                "src.job_manager.linkedin.easy_applier_linkedin.find_element_safely",
                new_callable=AsyncMock,
                side_effect=[label],
            ),
        ):
            with pytest.raises(NoInfoException, match="legal first name"):
                await applier._find_and_handle_textbox_question(section)


class TestConstraintAwareValidation:
    @pytest.mark.asyncio
    async def test_aria_invalid_triggers_bounded_numeric_correction(self, applier):
        field = AsyncMock()
        field.fill = AsyncMock()
        field.input_value = AsyncMock(
            side_effect=["Approximately 3 years", "Approximately 3 years", "3"]
        )
        field.get_attribute = AsyncMock(side_effect=["true", "false"])
        field.evaluate = AsyncMock(return_value={"valid": True, "message": ""})
        empty_errors = MagicMock()
        empty_errors.evaluate_all = AsyncMock(return_value=[])
        section = MagicMock()
        section.locator = MagicMock(return_value=empty_errors)
        applier._process_autocomplete_suggestions = AsyncMock()
        spec = field_spec_from_snapshot(
            "How many years?", {"type": "number", "maxlength": "2"}
        )

        answer = await applier._fill_and_validate_text_field(
            field, section, "How many years?", "Approximately 3 years", spec
        )

        assert answer == "3"
        assert field.fill.await_args_list[0].args == ("Approximately 3 years",)
        assert field.fill.await_args_list[1].args == ("3",)
        assert field.fill.await_count == 2

    @pytest.mark.asyncio
    async def test_visible_validation_is_repaired_before_button_lookup(self, applier):
        applier._find_all_form_errors = AsyncMock(side_effect=[["Invalid input"], []])
        applier._fill_textbox_question_errors = AsyncMock(return_value=True)
        button = AsyncMock()
        applier._find_next_or_submit_button = AsyncMock(return_value=(button, "next"))
        applier._check_and_fix_errors = AsyncMock(return_value=True)

        assert await applier._next_or_submit() is False
        applier._fill_textbox_question_errors.assert_awaited_once()
        applier._find_next_or_submit_button.assert_awaited_once()


class TestTargetClosedLifecycle:
    @pytest.mark.asyncio
    async def test_target_closed_stops_form_without_secondary_next_lookup(self, applier, test_job):
        applier._fill_up = AsyncMock(
            side_effect=RuntimeError("Target page, context or browser has been closed")
        )
        applier._next_or_submit = AsyncMock()

        with pytest.raises(RuntimeError, match="Target page"):
            await applier._fill_application_form(test_job)

        applier._next_or_submit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unexpected_target_close_is_technical_failure_and_not_saved(self, applier, test_job):
        applier._is_already_applied = AsyncMock(return_value=False)
        applier._find_easy_apply_button = AsyncMock(return_value=True)
        applier._click_continue_applying_button = AsyncMock()
        applier.check_for_premium_redirect = AsyncMock(return_value=False)
        applier._fill_application_form = AsyncMock(
            side_effect=RuntimeError("Target page, context or browser has been closed")
        )
        applier._save_job_application_process = AsyncMock()

        with (
            patch("src.job_manager.linkedin.easy_applier_linkedin.async_pause"),
            patch("src.job_manager.linkedin.easy_applier_linkedin.capture_page_screenshot"),
        ):
            with pytest.raises(RuntimeError, match="TECHNICAL_FAILURE"):
                await applier.job_easy_apply(test_job)

        applier._save_job_application_process.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_intentional_shutdown_target_close_is_cancelled(self, applier, test_job):
        applier._is_already_applied = AsyncMock(return_value=False)
        applier._find_easy_apply_button = AsyncMock(return_value=True)
        applier._click_continue_applying_button = AsyncMock()
        applier.check_for_premium_redirect = AsyncMock(return_value=False)
        applier._fill_application_form = AsyncMock(
            side_effect=RuntimeError("Target page, context or browser has been closed")
        )
        applier._save_job_application_process = AsyncMock()
        runtime_controller.shutdown_requested.set()
        try:
            with (
                patch("src.job_manager.linkedin.easy_applier_linkedin.async_pause"),
                patch("src.job_manager.linkedin.easy_applier_linkedin.capture_page_screenshot"),
            ):
                result = await applier.job_easy_apply(test_job)
        finally:
            runtime_controller.shutdown_requested.clear()

        assert result == (
            "Cancelled",
            "CANCELLED_BY_SHUTDOWN: Easy Apply page closed",
        )
        applier._save_job_application_process.assert_not_awaited()


class TestDropdownCaching:
    @pytest.mark.asyncio
    async def test_reuses_cached_answer_from_different_field_type(self, applier):
        dropdown = AsyncMock()
        dropdown.get_attribute = AsyncMock(return_value="email-dropdown")
        option_locator = MagicMock()
        option_locator.evaluate_all = AsyncMock(
            return_value=["Select an option", "candidate@example.invalid"]
        )
        dropdown.locator = MagicMock(return_value=option_locator)

        checked_locator = MagicMock()
        checked_locator.first.text_content = AsyncMock(return_value="Select an option")

        def dropdown_locator(selector):
            if selector == "option:checked":
                return checked_locator
            return option_locator

        dropdown.locator = MagicMock(side_effect=dropdown_locator)

        label = AsyncMock()
        label.text_content = AsyncMock(return_value="Email Address")

        applier.all_questions = [
            Question(
                question="email address", question_type="textbox", answer=" candidate@example.invalid "
            )
        ]
        applier._select_dropdown_option = AsyncMock()

        async def find_elements(section, selector, by="css selector", **kwargs):
            if selector == "css=select":
                return [dropdown]
            return []

        with (
            patch(
                "src.job_manager.linkedin.easy_applier_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                side_effect=find_elements,
            ),
            patch(
                "src.job_manager.linkedin.easy_applier_linkedin.find_element_safely",
                new_callable=AsyncMock,
                return_value=label,
            ),
        ):
            result = await applier._find_and_handle_dropdown_question(MagicMock())

        assert result is True
        applier._select_dropdown_option.assert_called_once_with(dropdown, "candidate@example.invalid")
        applier.gpt_answerer.select_one_answer_from_options.assert_not_called()

    @pytest.mark.asyncio
    async def test_accepts_already_selected_dropdown_answer(self, applier):
        dropdown = AsyncMock()
        dropdown.get_attribute = AsyncMock(return_value="email-dropdown")
        option_locator = MagicMock()
        option_locator.evaluate_all = AsyncMock(
            return_value=["Select an option", "candidate@example.invalid"]
        )

        checked_locator = MagicMock()
        checked_locator.first.text_content = AsyncMock(return_value="candidate@example.invalid")

        def dropdown_locator(selector):
            if selector == "option:checked":
                return checked_locator
            return option_locator

        dropdown.locator = MagicMock(side_effect=dropdown_locator)

        label = AsyncMock()
        label.text_content = AsyncMock(return_value="Email Address")
        applier._save_questions = MagicMock()

        async def find_elements(section, selector, by="css selector", **kwargs):
            if selector == "css=select":
                return [dropdown]
            return []

        with (
            patch(
                "src.job_manager.linkedin.easy_applier_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                side_effect=find_elements,
            ),
            patch(
                "src.job_manager.linkedin.easy_applier_linkedin.find_element_safely",
                new_callable=AsyncMock,
                return_value=label,
            ),
        ):
            result = await applier._find_and_handle_dropdown_question(MagicMock())

        assert result is True
        applier.gpt_answerer.select_one_answer_from_options.assert_not_called()
