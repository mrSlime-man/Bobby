from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.job_manager.linkedin.easy_applier_linkedin import LinkedInEasyApplier
from src.pydantic_models.job_models import Job

MODULE = "src.job_manager.linkedin.easy_applier_linkedin"


@pytest.fixture
def easy_applier():
    page = MagicMock()
    page.wait_for_selector = AsyncMock()
    page.locator.return_value.all = AsyncMock(return_value=[])

    with (
        patch(f"{MODULE}.get_ready_made_resume", return_value=None),
        patch.object(LinkedInEasyApplier, "_load_questions", return_value=[]),
    ):
        return LinkedInEasyApplier(
            page=page,
            gpt_answerer=MagicMock(),
            resume_anonymizer=MagicMock(),
            resume_generator_manager=MagicMock(),
            pause_checker=None,
            answers_file=Path("answers.yaml"),
            resume_dir=Path("resume"),
            cover_letter_dir=Path("cover_letters"),
            test_mode=True,
        )


class TestEasyApplyButtonDetection:
    @pytest.mark.asyncio
    async def test_find_easy_apply_button_returns_none_when_limit_reached(self, easy_applier):
        job = Job(
            job_title="VP Engineering",
            company_name="Example",
            url="https://www.linkedin.com/jobs/view/12345",
        )

        with (
            patch.object(easy_applier, "check_for_premium_redirect", new_callable=AsyncMock),
            patch.object(
                easy_applier, "_check_easy_apply_limit", new_callable=AsyncMock
            ) as mock_limit,
        ):
            mock_limit.return_value = True

            result = await easy_applier._find_easy_apply_button(job)

            assert result is None

    @pytest.mark.asyncio
    async def test_find_easy_apply_button_clicks_and_returns_true(self, easy_applier):
        job = Job(
            job_title="VP Engineering",
            company_name="Example",
            url="https://www.linkedin.com/jobs/view/12345",
        )
        button = AsyncMock()
        button.is_visible = AsyncMock(return_value=True)
        button.is_enabled = AsyncMock(return_value=True)
        button.first = AsyncMock()

        with (
            patch.object(easy_applier, "check_for_premium_redirect", new_callable=AsyncMock),
            patch.object(
                easy_applier, "_check_easy_apply_limit", new_callable=AsyncMock
            ) as mock_limit,
            patch(f"{MODULE}.find_elements_safely", new_callable=AsyncMock) as mock_find,
        ):
            mock_limit.return_value = False
            mock_find.return_value = [button]

            result = await easy_applier._find_easy_apply_button(job)

            assert result is True
            button.first.click.assert_awaited_once()


class TestNextButtonDetection:
    @pytest.mark.asyncio
    async def test_find_next_or_submit_button_returns_matching_button(self, easy_applier):
        button = MagicMock()
        candidate_scan = MagicMock()
        candidate_scan.evaluate_all = AsyncMock(
            return_value=[
                {
                    "action": "review",
                    "source": "text",
                    "tag": "button",
                    "role": "",
                    "attached": True,
                    "visible": True,
                    "enabled": True,
                    "aria_disabled": False,
                }
            ]
        )
        progress_scan = MagicMock()
        progress_scan.evaluate_all = AsyncMock(return_value="2/3")
        button_query = MagicMock()
        button_query.filter.return_value.first = button
        scope = MagicMock()
        scope.count = AsyncMock(return_value=1)
        scope.is_visible = AsyncMock(return_value=True)
        scope.locator = MagicMock(
            side_effect=lambda selector: (
                candidate_scan
                if selector == "button, [role='button']"
                else progress_scan
                if selector.startswith('[role="progressbar"]')
                else button_query
            )
        )
        dialog = MagicMock()
        dialog.first = scope
        easy_applier.page.locator = MagicMock(return_value=dialog)
        easy_applier.page.is_closed = MagicMock(return_value=False)

        with patch.object(
            easy_applier,
            "_is_usable_navigation_button",
            new_callable=AsyncMock,
            return_value=True,
        ):
            next_button, button_text = await easy_applier._find_next_or_submit_button()

        assert next_button is button
        assert button_text == "review"
