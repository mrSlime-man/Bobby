"""Tests for src/job_manager/linkedin/job_manager_linkedin.py (LinkedIn-specific methods)"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.job_manager.linkedin.job_manager_linkedin import LinkedInJobManager
from src.pydantic_models.job_models import Job
from src.utils.runtime_control import runtime_controller
from src.utils.easy_apply_quota import EasyApplyQuotaState


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_mode", ["expire", "shutdown", "permanent", "filters_changed"])
async def test_mid_page_provider_circuit_preserves_next_job_until_available(
    manager, tmp_path, monkeypatch, exit_mode
):
    from src.utils.runtime_control import RuntimeController

    module = "src.job_manager.linkedin.job_manager_linkedin"
    controller = RuntimeController()
    controller.start_process_run("intake-circuit-test")
    monkeypatch.setattr(f"{module}.runtime_controller", controller)
    monkeypatch.setattr(f"{module}.OUTPUT_DIR_LINKEDIN", str(tmp_path))
    monkeypatch.setattr(f"{module}.EASY_APPLY_ONLY_MODE", False)
    monkeypatch.setattr(f"{module}.emit_event", MagicMock())
    monkeypatch.setattr(
        f"{module}.TelegramReportSender",
        MagicMock(return_value=SimpleNamespace(send_start_message=AsyncMock())),
    )
    history = tmp_path / "encountered_jobs.json"
    history.write_text('["999"]')
    manager.cache = MagicMock(last_run=None)
    manager.resume_improvement_recommendations = MagicMock()
    manager.send_report = AsyncMock()
    manager.success_applies_num = manager.applies_num = 0
    manager.max_applies_num = 10
    manager.search_component.filters_verified = True
    manager.search_component.verify_current_search_state = AsyncMock(return_value=True)
    manager.get_vacancies_from_page = AsyncMock(
        return_value=[
            {"id": "111", "url": "https://linkedin.com/jobs/view/111"},
            {"id": "222", "url": "https://linkedin.com/jobs/view/222"},
        ]
    )
    manager._go_to_next_page = AsyncMock(return_value=False)
    circuit = {"open": False}
    monkeypatch.setattr(f"{module}.circuit_is_open", lambda _: circuit["open"])
    monkeypatch.setattr(f"{module}.circuit_status", lambda: {"permanent": exit_mode == "permanent"})

    async def apply(vacancy):
        if vacancy["id"] == "111":
            circuit["open"] = True
            return "Continue"
        return "Limit"

    manager.apply_job = AsyncMock(side_effect=apply)

    async def cooldown(_):
        # This assertion runs while the circuit is actually open, after job A.
        assert set(json.loads(history.read_text())) == {"999", "111"}
        assert controller.aggregate_snapshot()["found"] == 1
        manager.apply_job.assert_awaited_once()
        if exit_mode == "shutdown":
            controller.request_shutdown("test")
        else:
            circuit["open"] = False
            if exit_mode == "filters_changed":
                manager.search_component.verify_current_search_state.return_value = False

    monkeypatch.setattr(f"{module}.asyncio.sleep", cooldown)

    await manager.start_applying()

    expected = {"999", "111", "222"} if exit_mode == "expire" else {"999", "111"}
    assert set(json.loads(history.read_text())) == expected
    assert manager.apply_job.await_count == (2 if exit_mode == "expire" else 1)
    manager._go_to_next_page.assert_not_awaited()
    manager.send_report.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_intake_uses_healthy_fallback_when_primary_circuit_open(manager, monkeypatch):
    module = "src.job_manager.linkedin.job_manager_linkedin"
    manager.llm_agent_component = SimpleNamespace(
        model_type="gemini", provider_order=("gemini", "openai")
    )
    monkeypatch.setattr(
        f"{module}.circuit_is_open",
        lambda provider: provider == "gemini",
    )
    monkeypatch.setattr(
        f"{module}.circuit_status",
        lambda **kwargs: {"permanent": True} if kwargs.get("provider") == "gemini" else {},
    )

    assert await manager._wait_for_provider_before_intake() is True


@pytest.mark.asyncio
async def test_retryable_pre_admission_error_does_not_stop_following_jobs(
    manager, tmp_path, monkeypatch
):
    from src.utils.runtime_control import RuntimeController

    module = "src.job_manager.linkedin.job_manager_linkedin"
    controller = RuntimeController()
    controller.start_process_run("pre-admission-continue-test")
    monkeypatch.setattr(f"{module}.runtime_controller", controller)
    monkeypatch.setattr(f"{module}.OUTPUT_DIR_LINKEDIN", str(tmp_path))
    monkeypatch.setattr(f"{module}.EASY_APPLY_ONLY_MODE", False)
    monkeypatch.setattr(f"{module}.emit_event", MagicMock())
    monkeypatch.setattr(
        f"{module}.TelegramReportSender",
        MagicMock(return_value=SimpleNamespace(send_start_message=AsyncMock())),
    )

    history = tmp_path / "encountered_jobs.json"
    history.write_text("[]", encoding="utf-8")
    manager.cache = MagicMock(last_run=None)
    manager.resume_improvement_recommendations = MagicMock()
    manager.send_report = AsyncMock()
    manager.success_applies_num = manager.applies_num = 0
    manager.max_applies_num = 10
    manager.search_component.filters_verified = True
    manager.search_component.verify_current_search_state = AsyncMock(return_value=True)
    manager.get_vacancies_from_page = AsyncMock(
        side_effect=[
            [
                {"id": "retryable", "url": "https://linkedin.com/jobs/view/retryable"},
                {"id": "next", "url": "https://linkedin.com/jobs/view/next"},
            ],
            [],
        ]
    )
    manager._go_to_next_page = AsyncMock(return_value=False)

    async def apply(vacancy):
        if vacancy["id"] == "retryable":
            controller.record_job_disposition(vacancy["id"], "JOB_DEFERRED_TIMEOUT")
            return "Error"
        return "Continue"

    manager.apply_job = AsyncMock(side_effect=apply)

    await manager.start_applying()

    assert manager.apply_job.await_count == 2
    assert json.loads(history.read_text(encoding="utf-8")) == ["next"]
    assert controller.aggregate_snapshot()["unresolved_new_dispositions"] == 0


LINKEDIN_JOB_URL = "https://linkedin.com/jobs/view/123456"


@pytest.fixture
def mock_page():
    page = AsyncMock()
    page.url = LINKEDIN_JOB_URL
    page.context = AsyncMock()
    return page


@pytest.fixture
def manager(mock_page):
    return LinkedInJobManager(
        page=mock_page,
        linkedin_email="test@example.com",
        resume_anonymizer=MagicMock(),
        search_component=MagicMock(),
    )


@pytest.fixture(autouse=True)
def reset_runtime_admission():
    runtime_controller.shutdown_requested.clear()
    runtime_controller.begin_run()
    yield
    runtime_controller.shutdown_requested.clear()
    runtime_controller.begin_run()


@pytest.fixture(autouse=True)
def isolate_easy_apply_quota(tmp_path, monkeypatch):
    state = EasyApplyQuotaState(tmp_path / "easy_apply_quota.json")
    monkeypatch.setattr(
        "src.job_manager.linkedin.job_manager_linkedin.easy_apply_quota_state",
        lambda: state,
    )


@pytest.fixture
def test_job():
    return Job(
        job_title="Software Engineer",
        company_name="Tech Corp",
        location="Tampa, FL",
        url=LINKEDIN_JOB_URL,
        job_description="We need a Python developer",
    )


class TestGetVacanciesFromPage:
    @pytest.mark.asyncio
    async def test_returns_vacancies_with_urls(self, manager):
        mock_element = AsyncMock()
        with (
            patch.object(manager, "_scroll_to_load_jobs"),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[mock_element],
            ),
            patch.object(
                manager,
                "_extract_job_url",
                new_callable=AsyncMock,
                return_value=LINKEDIN_JOB_URL,
            ),
            patch("src.job_manager.linkedin.job_manager_linkedin.emit_event"),
        ):
            result = await manager.get_vacancies_from_page()

        assert len(result) == 1
        assert result[0]["url"] == LINKEDIN_JOB_URL
        assert result[0]["id"] == "123456"

    @pytest.mark.asyncio
    async def test_skips_element_with_no_url(self, manager):
        mock_element = AsyncMock()
        with (
            patch.object(manager, "_scroll_to_load_jobs"),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[mock_element],
            ),
            patch.object(manager, "_extract_job_url", new_callable=AsyncMock, return_value=None),
            patch("src.job_manager.linkedin.job_manager_linkedin.emit_event"),
        ):
            result = await manager.get_vacancies_from_page()

        assert result == []

    @pytest.mark.asyncio
    async def test_skips_already_applied_job_card(self, manager):
        mock_element = MagicMock()
        applied_locator = MagicMock()
        applied_locator.count = AsyncMock(return_value=1)
        applied_item = MagicMock()
        applied_item.inner_text = AsyncMock(return_value="Applied")
        applied_locator.nth.return_value = applied_item
        mock_element.locator.return_value = applied_locator

        with (
            patch.object(manager, "_scroll_to_load_jobs"),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[mock_element],
            ),
            patch.object(manager, "_extract_job_url", new_callable=AsyncMock) as mock_extract_url,
            patch("src.job_manager.linkedin.job_manager_linkedin.emit_event"),
        ):
            result = await manager.get_vacancies_from_page()

        assert result == []
        mock_extract_url.assert_not_called()

    @pytest.mark.asyncio
    async def test_detects_applied_job_card_from_text(self, manager):
        mock_element = MagicMock()
        mock_element.locator.side_effect = RuntimeError("locator unavailable")

        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.get_clean_text",
            new_callable=AsyncMock,
            return_value="Head of Digitalization\nMadison Pearl\nApplied",
        ):
            result = await manager._is_applied_job_card(mock_element)

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_exception(self, manager):
        with (
            patch.object(manager, "_scroll_to_load_jobs", side_effect=Exception("scroll error")),
            patch("src.job_manager.linkedin.job_manager_linkedin.debug_capture"),
        ):
            result = await manager.get_vacancies_from_page()

        assert result == []

    @pytest.mark.asyncio
    async def test_parsing_does_not_increment_cycle_unique_total(self, manager):
        mock_element = AsyncMock()
        manager.total_discovered_jobs = 0
        with (
            patch.object(manager, "_scroll_to_load_jobs"),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[mock_element, mock_element],
            ),
            patch.object(
                manager,
                "_extract_job_url",
                new_callable=AsyncMock,
                side_effect=[
                    "https://linkedin.com/jobs/view/123456",
                    "https://linkedin.com/jobs/view/123457",
                ],
            ),
            patch("src.job_manager.linkedin.job_manager_linkedin.emit_event"),
        ):
            await manager.get_vacancies_from_page()

        assert manager.total_discovered_jobs == 0


class TestCycleCounters:
    def test_job_key_prefers_stable_id(self, manager):
        assert manager._job_key({"id": "123", "url": "https://x.invalid"}) == "123"

    def test_job_key_extracts_id_from_url(self, manager):
        assert manager._job_key({"url": "https://linkedin.com/jobs/view/456?trk=x"}) == "456"

    def test_records_real_attempt_by_method(self, manager):
        manager.cycle_attempted_num = 0
        manager.cycle_easy_apply_attempted = 0
        manager.cycle_external_attempted = 0
        manager._record_application_attempt("external")
        manager._record_application_attempt("easy_apply")
        assert manager.cycle_attempted_num == 2
        assert manager.cycle_external_attempted == 1
        assert manager.cycle_easy_apply_attempted == 1


class TestExtractJobUrl:
    @pytest.mark.asyncio
    async def test_returns_url_with_https_prefix(self, manager):
        mock_element = AsyncMock()
        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.get_element_attribute_safely",
            new_callable=AsyncMock,
            return_value="/jobs/view/999",
        ):
            result = await manager._extract_job_url(mock_element)

        assert result == "https://www.linkedin.com/jobs/view/999"

    @pytest.mark.asyncio
    async def test_returns_url_already_absolute(self, manager):
        mock_element = AsyncMock()
        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.get_element_attribute_safely",
            new_callable=AsyncMock,
            return_value="https://linkedin.com/jobs/view/999",
        ):
            result = await manager._extract_job_url(mock_element)

        assert result == "https://linkedin.com/jobs/view/999"

    @pytest.mark.asyncio
    async def test_returns_canonical_url_from_recommended_current_job_id(self, manager):
        mock_element = AsyncMock()
        recommended_href = (
            "https://www.linkedin.com/jobs/collections/recommended?"
            "currentJobId=4401460753&start=0&trackingId=abc"
        )
        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.get_element_attribute_safely",
            new_callable=AsyncMock,
            return_value=recommended_href,
        ):
            result = await manager._extract_job_url(mock_element)

        assert result == "https://www.linkedin.com/jobs/view/4401460753"

    @pytest.mark.asyncio
    async def test_returns_canonical_url_from_top_applicant_current_job_id(self, manager):
        mock_element = AsyncMock()
        top_applicant_href = (
            "https://www.linkedin.com/jobs/collections/top-applicant?"
            "currentJobId=4401460753&start=0&trackingId=abc"
        )
        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.get_element_attribute_safely",
            new_callable=AsyncMock,
            return_value=top_applicant_href,
        ):
            result = await manager._extract_job_url(mock_element)

        assert result == "https://www.linkedin.com/jobs/view/4401460753"

    @pytest.mark.asyncio
    async def test_returns_canonical_url_from_data_job_id(self, manager):
        mock_element = AsyncMock()
        mock_element.get_attribute = AsyncMock(return_value="4401460753")
        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.get_element_attribute_safely",
            new_callable=AsyncMock,
            return_value="",
        ):
            result = await manager._extract_job_url(mock_element)

        assert result == "https://www.linkedin.com/jobs/view/4401460753"

    @pytest.mark.asyncio
    async def test_rejects_non_linkedin_absolute_url(self, manager):
        mock_element = AsyncMock()
        mock_element.get_attribute = AsyncMock(return_value=None)
        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.get_element_attribute_safely",
            new_callable=AsyncMock,
            return_value="https://malicious.com/jobs/view/999",
        ):
            result = await manager._extract_job_url(mock_element)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_href_found(self, manager):
        mock_element = AsyncMock()
        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.get_element_attribute_safely",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await manager._extract_job_url(mock_element)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_href_has_no_jobs_view(self, manager):
        mock_element = AsyncMock()
        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.get_element_attribute_safely",
            new_callable=AsyncMock,
            return_value="https://linkedin.com/company/techcorp",
        ):
            result = await manager._extract_job_url(mock_element)

        assert result is None


class TestGetDetailedJobDescription:
    @pytest.mark.asyncio
    async def test_extracts_job_id_from_url(self, manager, mock_page):
        mock_page.url = "https://linkedin.com/jobs/view/987654"
        manager.page = mock_page
        with (
            patch.object(manager, "_extract_job_title", new_callable=AsyncMock, return_value="Dev"),
            patch.object(
                manager, "_extract_company_name", new_callable=AsyncMock, return_value="Corp"
            ),
            patch.object(
                manager,
                "_extract_job_description",
                new_callable=AsyncMock,
                return_value="desc",
            ),
            patch.object(
                manager,
                "_extract_company_description",
                new_callable=AsyncMock,
                return_value="about",
            ),
        ):
            job = await manager._get_detailed_job_description()

        assert job.job_id == "987654"
        assert job.url == "https://linkedin.com/jobs/view/987654"

    @pytest.mark.asyncio
    async def test_returns_empty_job_on_extraction_error(self, manager, mock_page):
        mock_page.url = "https://linkedin.com/jobs/view/111"
        manager.page = mock_page
        with (
            patch.object(
                manager,
                "_extract_job_title",
                new_callable=AsyncMock,
                side_effect=Exception("network"),
            ),
            patch.object(
                manager,
                "_extract_company_name",
                new_callable=AsyncMock,
                side_effect=Exception("network"),
            ),
            patch.object(
                manager,
                "_extract_job_description",
                new_callable=AsyncMock,
                side_effect=Exception("network"),
            ),
            patch.object(
                manager,
                "_extract_company_description",
                new_callable=AsyncMock,
                side_effect=Exception("network"),
            ),
            patch("src.job_manager.linkedin.job_manager_linkedin.debug_capture"),
        ):
            job = await manager._get_detailed_job_description()

        assert isinstance(job, Job)

    @pytest.mark.asyncio
    async def test_url_not_set_when_not_jobs_view(self, manager, mock_page):
        mock_page.url = "https://linkedin.com/feed"
        manager.page = mock_page
        with (
            patch.object(manager, "_extract_job_title", new_callable=AsyncMock, return_value=""),
            patch.object(manager, "_extract_company_name", new_callable=AsyncMock, return_value=""),
            patch.object(
                manager,
                "_extract_job_description",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch.object(
                manager,
                "_extract_company_description",
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            job = await manager._get_detailed_job_description()

        assert job.url == ""


class TestExtractJobTitle:
    @pytest.mark.asyncio
    async def test_returns_title_from_element(self, manager):
        mock_element = AsyncMock()
        with (
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[mock_element],
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.get_clean_text",
                new_callable=AsyncMock,
                return_value="Senior Engineer",
            ),
        ):
            result = await manager._extract_job_title()

        assert result == "Senior Engineer"

    @pytest.mark.asyncio
    async def test_extracts_title_from_comma_separated(self, manager):
        mock_element = AsyncMock()
        with (
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[mock_element],
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.get_clean_text",
                new_callable=AsyncMock,
                return_value="Senior Engineer, New York, USA",
            ),
        ):
            result = await manager._extract_job_title()

        assert result == "Senior Engineer"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_elements(self, manager):
        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await manager._extract_job_title()

        assert result is None

    @pytest.mark.asyncio
    async def test_skips_short_title(self, manager):
        mock_element = AsyncMock()
        with (
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[mock_element],
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.get_clean_text",
                new_callable=AsyncMock,
                return_value="AI",
            ),
        ):
            result = await manager._extract_job_title()

        assert result is None


class TestExtractCompanyName:
    @pytest.mark.asyncio
    async def test_returns_company_name_from_link(self, manager):
        mock_element = AsyncMock()
        with (
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[mock_element],
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.get_clean_text",
                new_callable=AsyncMock,
                return_value="Acme Corp",
            ),
        ):
            result = await manager._extract_company_name()

        assert result == "Acme Corp"

    @pytest.mark.asyncio
    async def test_fallback_to_alert_paragraph(self, manager):
        mock_alert_element = AsyncMock()

        def find_elements_side_effect(page, selector, by):
            if "company" in selector:
                return []
            return [mock_alert_element]

        with (
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                side_effect=find_elements_side_effect,
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.get_clean_text",
                new_callable=AsyncMock,
                return_value="Software Engineer, Acme Corp, CA, USA",
            ),
        ):
            result = await manager._extract_company_name()

        assert result == "Acme Corp"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_company_found(self, manager):
        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await manager._extract_company_name()

        assert result is None


class TestExtractJobDescription:
    @pytest.mark.asyncio
    async def test_returns_description_from_element(self, manager):
        mock_element = AsyncMock()
        with (
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_element_safely",
                new_callable=AsyncMock,
                return_value=mock_element,
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.get_clean_text",
                new_callable=AsyncMock,
                return_value="We are looking for a talented developer " * 5,
            ),
            patch.object(
                manager,
                "_extract_requirements_section",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await manager._extract_job_description()

        assert result is not None
        assert len(result) > 50

    @pytest.mark.asyncio
    async def test_appends_requirements_when_present(self, manager):
        mock_element = AsyncMock()
        long_desc = "We need a developer. " * 10
        with (
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_element_safely",
                new_callable=AsyncMock,
                return_value=mock_element,
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.get_clean_text",
                new_callable=AsyncMock,
                return_value=long_desc,
            ),
            patch.object(
                manager,
                "_extract_requirements_section",
                new_callable=AsyncMock,
                return_value="• Python\n• Docker",
            ),
        ):
            result = await manager._extract_job_description()

        assert "• Python" in result
        assert long_desc.strip() in result

    @pytest.mark.asyncio
    async def test_returns_none_when_no_description(self, manager):
        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.find_element_safely",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await manager._extract_job_description()

        assert result is None


class TestExtractRequirementsSection:
    @pytest.mark.asyncio
    async def test_returns_bullet_requirements(self, manager):
        mock_el1 = AsyncMock()
        mock_el2 = AsyncMock()

        texts = iter(["• Python 3+ years", "• Docker experience"])

        async def get_text(el):
            return next(texts)

        with (
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[mock_el1, mock_el2],
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.get_clean_text",
                side_effect=get_text,
            ),
        ):
            result = await manager._extract_requirements_section()

        assert result is not None
        assert "• Python 3+ years" in result
        assert "• Docker experience" in result

    @pytest.mark.asyncio
    async def test_stops_at_non_bullet_text(self, manager):
        mock_el1 = AsyncMock()
        mock_el2 = AsyncMock()

        texts = iter(["• Python 3+ years", "Some other section content"])

        async def get_text(el):
            return next(texts)

        with (
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[mock_el1, mock_el2],
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.get_clean_text",
                side_effect=get_text,
            ),
        ):
            result = await manager._extract_requirements_section()

        assert result is not None
        assert "• Python 3+ years" in result
        assert "Some other section" not in result

    @pytest.mark.asyncio
    async def test_returns_none_when_no_elements(self, manager):
        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await manager._extract_requirements_section()

        assert result is None


class TestExtractCompanyDescription:
    @pytest.mark.asyncio
    async def test_returns_company_description(self, manager):
        mock_element = AsyncMock()
        long_text = "We are a leading technology company. " * 5
        with (
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_element_safely",
                new_callable=AsyncMock,
                return_value=mock_element,
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.get_clean_text",
                new_callable=AsyncMock,
                return_value=long_text,
            ),
        ):
            result = await manager._extract_company_description()

        assert result is not None
        assert len(result) > 20

    @pytest.mark.asyncio
    async def test_strips_more_button_text(self, manager):
        mock_element = AsyncMock()
        with (
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_element_safely",
                new_callable=AsyncMock,
                return_value=mock_element,
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.get_clean_text",
                new_callable=AsyncMock,
                return_value="We are a great company building great products… more",
            ),
        ):
            result = await manager._extract_company_description()

        assert result is not None
        assert "… more" not in result

    @pytest.mark.asyncio
    async def test_returns_none_when_no_element(self, manager):
        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.find_element_safely",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await manager._extract_company_description()

        assert result is None


class TestCheckApplyButton:
    @pytest.mark.asyncio
    async def test_returns_empty_string_for_easy_apply(self, manager):
        mock_button = AsyncMock()
        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
            new_callable=AsyncMock,
            return_value=[mock_button],
        ):
            result = await manager._check_apply_button()

        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_buttons(self, manager):
        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await manager._check_apply_button()

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_url_for_external_apply(self, manager):
        mock_button = AsyncMock()

        def find_elements_side_effect(page, selector, by):
            if "Easy Apply" in selector:
                return []
            return [mock_button]

        with (
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_elements_safely",
                new_callable=AsyncMock,
                side_effect=find_elements_side_effect,
            ),
            patch.object(
                manager,
                "_get_button_link",
                new_callable=AsyncMock,
                return_value="https://external-site.com/apply",
            ),
        ):
            result = await manager._check_apply_button()

        assert result == "https://external-site.com/apply"


class TestGoToNextPage:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("has_result_next", [False, True])
    async def test_company_photo_next_is_never_used_for_result_pagination(
        self, manager, monkeypatch, has_result_next
    ):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).exists():
                pytest.skip("Playwright Chromium is required for the pagination DOM regression")
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content("""
                    <script>window.companyClicks = 0; window.resultClicks = 0;</script>
                    <div class="artdeco-pagination">
                      <button aria-label="Company photos Next"
                              class="artdeco-pagination__button--next"
                              onclick="window.companyClicks++">Company photos</button>
                    </div>
                    <div class="jobs-search-pagination"></div>
                    """)
                if has_result_next:
                    await page.locator(".jobs-search-pagination").evaluate(
                        """element => element.innerHTML = `<button aria-label="Next"
                           class="artdeco-pagination__button--next"
                           onclick="window.resultClicks++">More results</button>`"""
                    )
                manager.page = page
                manager.page_num = 0
                monkeypatch.setattr("src.utils.browser_utils.debug_capture", AsyncMock())
                monkeypatch.setattr(
                    "src.job_manager.linkedin.job_manager_linkedin.async_pause", AsyncMock()
                )
                result = await manager._go_to_next_page()

                assert result is has_result_next
                assert manager.page_num == int(has_result_next)
                assert await page.evaluate("window.companyClicks") == 0
                assert await page.evaluate("window.resultClicks") == int(has_result_next)
            finally:
                await browser.close()

    @pytest.mark.asyncio
    async def test_stalled_pagination_is_cancelled_within_one_overall_budget(
        self, manager, monkeypatch
    ):
        module = "src.job_manager.linkedin.job_manager_linkedin"
        click_cancelled = asyncio.Event()

        async def stalled_click(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                click_cancelled.set()

        click = AsyncMock(side_effect=stalled_click)
        fallback = AsyncMock()
        monkeypatch.setattr(f"{module}.safe_click", click)
        monkeypatch.setattr(f"{module}.find_element_safely", fallback)
        monkeypatch.setattr(f"{module}.LINKEDIN_PAGINATION_TIMEOUT_SECONDS", 0.01)
        manager.page_num = 0

        result = await asyncio.wait_for(manager._go_to_next_page(), timeout=1)

        assert result is False
        assert manager.page_num == 0
        assert click_cancelled.is_set()
        click.assert_awaited_once()
        fallback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shutdown_stops_next_selector_and_fallback(self, manager, monkeypatch):
        module = "src.job_manager.linkedin.job_manager_linkedin"

        async def signal_during_click(*args, **kwargs):
            runtime_controller.request_shutdown("pagination regression")
            return False

        click = AsyncMock(side_effect=signal_during_click)
        fallback = AsyncMock()
        monkeypatch.setattr(f"{module}.safe_click", click)
        monkeypatch.setattr(f"{module}.find_element_safely", fallback)

        assert await manager._go_to_next_page() is False
        click.assert_awaited_once()
        fallback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_targets_second_visible_page_after_first_page(self, manager):
        manager.page_num = 0
        attempted_selectors = []

        async def safe_click_side_effect(page, selector, timeout=10000):
            attempted_selectors.append(selector)
            return True

        with (
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.safe_click",
                side_effect=safe_click_side_effect,
            ),
            patch("src.job_manager.linkedin.job_manager_linkedin.emit_event"),
            patch("src.job_manager.linkedin.job_manager_linkedin.async_pause"),
        ):
            result = await manager._go_to_next_page()

        assert result is True
        assert manager.page_num == 1
        assert attempted_selectors[0] == (
            "button[aria-label='Page 2']:not([disabled]):not([aria-current='page'])"
        )

    @pytest.mark.asyncio
    async def test_returns_true_and_increments_page_on_success(self, manager):
        manager.page_num = 1
        with (
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.safe_click",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("src.job_manager.linkedin.job_manager_linkedin.emit_event"),
            patch("src.job_manager.linkedin.job_manager_linkedin.async_pause"),
        ):
            result = await manager._go_to_next_page()

        assert result is True
        assert manager.page_num == 2

    @pytest.mark.asyncio
    async def test_returns_false_when_no_button_found(self, manager):
        manager.page_num = 1
        with (
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.safe_click",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_element_safely",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("src.job_manager.linkedin.job_manager_linkedin.emit_event"),
        ):
            result = await manager._go_to_next_page()

        assert result is False
        assert manager.page_num == 1

    @pytest.mark.asyncio
    async def test_falls_back_to_element_click(self, manager):
        manager.page_num = 0
        mock_element = AsyncMock()
        mock_element.click = AsyncMock()

        with (
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.safe_click",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.find_element_safely",
                new_callable=AsyncMock,
                return_value=mock_element,
            ),
            patch("src.job_manager.linkedin.job_manager_linkedin.emit_event"),
            patch("src.job_manager.linkedin.job_manager_linkedin.async_pause"),
        ):
            result = await manager._go_to_next_page()

        assert result is True
        mock_element.click.assert_called_once()


class TestEasyApply:
    @pytest.mark.asyncio
    async def test_delegates_to_linkedin_easy_applier(self, manager, test_job):
        mock_applier = AsyncMock()
        mock_applier.apply_to_job = AsyncMock(
            return_value=(("Success", ""), "/tmp/resumes/generated.pdf")
        )

        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.LinkedInEasyApplier",
            return_value=mock_applier,
        ):
            result = await manager.easy_apply(test_job)

        assert result == ("Success", "")
        mock_applier.apply_to_job.assert_called_once_with(test_job)
        assert manager.submitted_resume_path == "/tmp/resumes/generated.pdf"

    @pytest.mark.asyncio
    async def test_sets_page_on_applier(self, manager, test_job):
        mock_applier = AsyncMock()
        mock_applier.apply_to_job = AsyncMock(return_value=("Skip", ""))
        mock_applier.submitted_resume_path = None

        with patch(
            "src.job_manager.linkedin.job_manager_linkedin.LinkedInEasyApplier",
            return_value=mock_applier,
        ):
            await manager.easy_apply(test_job)

        mock_applier.set_page.assert_called_once_with(manager.page)

    @pytest.mark.asyncio
    async def test_external_provider_circuit_does_not_disable_easy_apply(self, manager, test_job):
        mock_applier = AsyncMock()
        mock_applier.apply_to_job = AsyncMock(return_value=(("Skip", ""), None))
        with (
            patch("src.llm.provider_health.circuit_is_open", return_value=True),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.LinkedInEasyApplier",
                return_value=mock_applier,
            ),
        ):
            result = await manager.easy_apply(test_job)

        assert result == ("Skip", "")
        mock_applier.apply_to_job.assert_awaited_once_with(test_job)


class TestApplyJob:
    def _setup_manager(self, manager):
        manager.success_applies_num = 0
        manager.max_applies_num = 10
        manager.applies_num = 0
        manager.error_num = 0
        manager.success_companies = {}
        manager.skipped_companies = {}
        manager.failed_companies = {}
        manager.apply_once_at_company = True
        manager.job_blacklist = []
        manager.pause_checker = None

    @pytest.mark.asyncio
    async def test_skips_blacklisted_company(self, manager, mock_page, test_job):
        self._setup_manager(manager)
        manager.job_blacklist = ["tech corp"]
        new_page = AsyncMock()
        new_page.url = LINKEDIN_JOB_URL
        mock_page.context.new_page = AsyncMock(return_value=new_page)

        with (
            patch.object(
                manager,
                "_get_detailed_job_description",
                new_callable=AsyncMock,
                return_value=test_job,
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.async_pause",
                new_callable=AsyncMock,
            ),
            patch.object(manager, "_handle_apply_result", new_callable=AsyncMock),
        ):
            result = await manager.apply_job({"url": LINKEDIN_JOB_URL})

        assert result == "Skip"

    @pytest.mark.asyncio
    async def test_skips_already_seen_job(self, manager, mock_page, test_job):
        self._setup_manager(manager)
        manager.success_companies = {
            "Tech Corp": [{"job_title": "Software Engineer", "url": LINKEDIN_JOB_URL}]
        }
        new_page = AsyncMock()
        new_page.url = LINKEDIN_JOB_URL
        mock_page.context.new_page = AsyncMock(return_value=new_page)

        with (
            patch.object(
                manager,
                "_get_detailed_job_description",
                new_callable=AsyncMock,
                return_value=test_job,
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.async_pause",
                new_callable=AsyncMock,
            ),
            patch.object(manager, "_handle_apply_result", new_callable=AsyncMock),
            patch("src.job_manager.linkedin.job_manager_linkedin.COLLECT_INFO_MODE", False),
        ):
            result = await manager.apply_job({"url": LINKEDIN_JOB_URL})

        assert result == "Skip"

    @pytest.mark.asyncio
    async def test_returns_error_for_invalid_job(self, manager, mock_page):
        self._setup_manager(manager)
        empty_job = Job()
        new_page = AsyncMock()
        new_page.url = LINKEDIN_JOB_URL
        mock_page.context.new_page = AsyncMock(return_value=new_page)

        with (
            patch.object(
                manager,
                "_get_detailed_job_description",
                new_callable=AsyncMock,
                return_value=empty_job,
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.async_pause",
                new_callable=AsyncMock,
            ),
            patch.object(manager, "_handle_apply_result", new_callable=AsyncMock),
        ):
            result = await manager.apply_job({"url": LINKEDIN_JOB_URL})

        assert result == "Error"

    @pytest.mark.asyncio
    async def test_suitability_provider_failure_is_a_technical_failure(
        self, manager, mock_page, test_job
    ):
        self._setup_manager(manager)
        new_page = AsyncMock(url=LINKEDIN_JOB_URL)
        mock_page.context.new_page = AsyncMock(return_value=new_page)
        manager.llm_answerer_component = MagicMock()
        manager.llm_answerer_component.job_is_interesting.return_value = None

        with (
            patch.object(
                manager,
                "_get_detailed_job_description",
                new_callable=AsyncMock,
                return_value=test_job,
            ),
            patch.object(manager, "_handle_apply_result", new_callable=AsyncMock) as handle,
            patch("src.job_manager.linkedin.job_manager_linkedin.COLLECT_INFO_MODE", False),
            patch("src.job_manager.linkedin.job_manager_linkedin.MONKEY_MODE", False),
        ):
            result = await manager.apply_job({"url": LINKEDIN_JOB_URL})

        assert result == "Error"
        apply_result = handle.await_args.args[0]
        assert apply_result == (
            "Error",
            "TECHNICAL_FAILURE: suitability LLM request failed",
        )

    @pytest.mark.asyncio
    async def test_pre_admission_suitability_failure_does_not_consume_history(
        self, manager, mock_page, test_job, tmp_path
    ):
        self._setup_manager(manager)
        history = tmp_path / "encountered_jobs.json"
        history.write_text('["existing"]', encoding="utf-8")
        manager._encountered_jobs = {"existing"}
        manager._encountered_jobs_path = history
        new_page = AsyncMock(url=LINKEDIN_JOB_URL)
        mock_page.context.new_page = AsyncMock(return_value=new_page)
        manager.llm_answerer_component = MagicMock()
        manager.llm_answerer_component.job_is_interesting.return_value = None

        with (
            patch.object(
                manager,
                "_get_detailed_job_description",
                new_callable=AsyncMock,
                return_value=test_job,
            ),
            patch.object(manager, "_handle_apply_result", new_callable=AsyncMock),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.async_pause", new_callable=AsyncMock
            ),
            patch("src.job_manager.linkedin.job_manager_linkedin.COLLECT_INFO_MODE", False),
            patch("src.job_manager.linkedin.job_manager_linkedin.MONKEY_MODE", False),
        ):
            assert await manager.apply_job({"url": LINKEDIN_JOB_URL}) == "Error"

        assert json.loads(history.read_text(encoding="utf-8")) == ["existing"]

    @pytest.mark.asyncio
    async def test_optional_skill_analytics_failure_still_admits_external_worker(
        self, manager, mock_page, test_job, tmp_path
    ):
        """Transient analytics are never an external-application routing gate."""
        self._setup_manager(manager)
        history = tmp_path / "encountered_jobs.json"
        history.write_text("[]", encoding="utf-8")
        manager._encountered_jobs_path = history
        new_page = AsyncMock(url=LINKEDIN_JOB_URL)
        mock_page.context.new_page = AsyncMock(return_value=new_page)
        manager.llm_answerer_component = MagicMock()
        manager.llm_answerer_component.job_is_interesting.return_value = (True, 80, "Good fit")
        manager.llm_agent_component = MagicMock()

        async def external_worker(*_args, **_kwargs):
            runtime_controller.finish_worker("external")
            return "Skip", "Test worker outcome"

        manager.llm_agent_component.apply_to_job = AsyncMock(side_effect=external_worker)

        with (
            patch.object(
                manager,
                "_get_detailed_job_description",
                new_callable=AsyncMock,
                return_value=test_job,
            ),
            patch.object(
                manager,
                "_extract_skills_from_vacancy",
                side_effect=TimeoutError("analytics provider timeout"),
            ),
            patch.object(manager, "_update_skill_stat") as update_skills,
            patch.object(
                manager,
                "_check_apply_button",
                new_callable=AsyncMock,
                return_value="https://careers.example.invalid/apply",
            ),
            patch.object(manager, "_handle_apply_result", new_callable=AsyncMock),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.async_pause",
                new_callable=AsyncMock,
            ),
            patch("src.job_manager.linkedin.job_manager_linkedin.COLLECT_INFO_MODE", False),
            patch("src.job_manager.linkedin.job_manager_linkedin.MONKEY_MODE", False),
            patch("src.job_manager.linkedin.job_manager_linkedin.EASY_APPLY_ONLY_MODE", False),
        ):
            assert await manager.apply_job({"id": "analytics", "url": LINKEDIN_JOB_URL}) == "Skip"

        manager.llm_agent_component.apply_to_job.assert_awaited_once()
        update_skills.assert_not_called()
        assert json.loads(history.read_text(encoding="utf-8")) == ["analytics"]

    @pytest.mark.asyncio
    async def test_easy_apply_quota_defers_easy_only_job_without_consuming_history(
        self, manager, mock_page, test_job, tmp_path
    ):
        import src.job_manager.linkedin.job_manager_linkedin as module

        self._setup_manager(manager)
        history = tmp_path / "encountered_jobs.json"
        history.write_text("[]", encoding="utf-8")
        manager._encountered_jobs_path = history
        new_page = AsyncMock(url=LINKEDIN_JOB_URL)
        mock_page.context.new_page = AsyncMock(return_value=new_page)
        manager.llm_answerer_component = MagicMock()
        manager.llm_answerer_component.job_is_interesting.return_value = (True, 80, "Good fit")
        state = module.easy_apply_quota_state()
        state.mark_blocked(now_epoch=100)

        with (
            patch.object(manager, "_get_detailed_job_description", new_callable=AsyncMock, return_value=test_job),
            patch.object(manager, "_extract_skills_from_vacancy", return_value="Python"),
            patch.object(manager, "_update_skill_stat"),
            patch.object(manager, "_check_apply_button", new_callable=AsyncMock, return_value=""),
            patch.object(manager, "_handle_apply_result", new_callable=AsyncMock) as handle,
            patch("src.job_manager.linkedin.job_manager_linkedin.async_pause", new_callable=AsyncMock),
            patch("src.job_manager.linkedin.job_manager_linkedin.COLLECT_INFO_MODE", False),
            patch("src.job_manager.linkedin.job_manager_linkedin.MONKEY_MODE", False),
            patch("src.job_manager.linkedin.job_manager_linkedin.EASY_APPLY_ONLY_MODE", False),
        ):
            result = await manager.apply_job({"id": "quota-only", "url": LINKEDIN_JOB_URL})

        assert result == "Deferred"
        assert json.loads(history.read_text(encoding="utf-8")) == []
        assert handle.await_args.args[0][0] == "Deferred"
        assert "DEFERRED_EASY_APPLY_LIMIT" in handle.await_args.args[0][1]

    @pytest.mark.asyncio
    async def test_easy_apply_quota_prefers_same_vacancy_external_route(
        self, manager, mock_page, test_job, tmp_path
    ):
        import src.job_manager.linkedin.job_manager_linkedin as module

        self._setup_manager(manager)
        history = tmp_path / "encountered_jobs.json"
        history.write_text("[]", encoding="utf-8")
        manager._encountered_jobs_path = history
        new_page = AsyncMock(url=LINKEDIN_JOB_URL)
        mock_page.context.new_page = AsyncMock(return_value=new_page)
        manager.llm_answerer_component = MagicMock()
        manager.llm_answerer_component.job_is_interesting.return_value = (True, 80, "Good fit")
        manager.llm_agent_component = MagicMock()

        async def external_worker(*_args, **_kwargs):
            runtime_controller.finish_worker("external")
            return "Skip", "Test worker outcome"

        manager.llm_agent_component.apply_to_job = AsyncMock(side_effect=external_worker)
        module.easy_apply_quota_state().mark_blocked(now_epoch=100)

        with (
            patch.object(manager, "_get_detailed_job_description", new_callable=AsyncMock, return_value=test_job),
            patch.object(manager, "_extract_skills_from_vacancy", return_value="Python"),
            patch.object(manager, "_update_skill_stat"),
            patch.object(manager, "_check_apply_button", new_callable=AsyncMock, return_value="https://careers.example.invalid/apply"),
            patch.object(manager, "_handle_apply_result", new_callable=AsyncMock),
            patch("src.job_manager.linkedin.job_manager_linkedin.async_pause", new_callable=AsyncMock),
            patch("src.job_manager.linkedin.job_manager_linkedin.COLLECT_INFO_MODE", False),
            patch("src.job_manager.linkedin.job_manager_linkedin.MONKEY_MODE", False),
            patch("src.job_manager.linkedin.job_manager_linkedin.EASY_APPLY_ONLY_MODE", False),
        ):
            result = await manager.apply_job({"id": "quota-external", "url": LINKEDIN_JOB_URL})

        assert result == "Skip"
        manager.llm_agent_component.apply_to_job.assert_awaited_once()
        assert json.loads(history.read_text(encoding="utf-8")) == ["quota-external"]

    @pytest.mark.asyncio
    async def test_successfactors_external_route_is_admitted(
        self, manager, mock_page, test_job, tmp_path
    ):
        """SuccessFactors uses the generic external worker, including account registration."""
        self._setup_manager(manager)
        history = tmp_path / "encountered_jobs.json"
        history.write_text("[]", encoding="utf-8")
        manager._encountered_jobs_path = history
        new_page = AsyncMock(url=LINKEDIN_JOB_URL)
        mock_page.context.new_page = AsyncMock(return_value=new_page)
        manager.llm_answerer_component = MagicMock()
        manager.llm_answerer_component.job_is_interesting.return_value = (True, 80, "Good fit")
        manager.llm_agent_component = MagicMock()

        async def external_worker(*_args, **_kwargs):
            runtime_controller.finish_worker("external")
            return "Skip", "Test worker outcome"

        manager.llm_agent_component.apply_to_job = AsyncMock(side_effect=external_worker)
        successfactors_url = "https://jobs.successfactors.com/job/example"

        with (
            patch.object(
                manager,
                "_get_detailed_job_description",
                new_callable=AsyncMock,
                return_value=test_job,
            ),
            patch.object(manager, "_extract_skills_from_vacancy", return_value="Python"),
            patch.object(manager, "_update_skill_stat"),
            patch.object(
                manager,
                "_check_apply_button",
                new_callable=AsyncMock,
                return_value=successfactors_url,
            ),
            patch.object(manager, "_handle_apply_result", new_callable=AsyncMock),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.async_pause",
                new_callable=AsyncMock,
            ),
            patch("src.job_manager.linkedin.job_manager_linkedin.COLLECT_INFO_MODE", False),
            patch("src.job_manager.linkedin.job_manager_linkedin.MONKEY_MODE", False),
            patch("src.job_manager.linkedin.job_manager_linkedin.EASY_APPLY_ONLY_MODE", False),
        ):
            assert await manager.apply_job({"id": "sf", "url": LINKEDIN_JOB_URL}) == "Skip"

        manager.llm_agent_component.apply_to_job.assert_awaited_once_with(
            successfactors_url,
            job_title=test_job.job_title,
            company_name=test_job.company_name,
            linkedin_url=test_job.url,
        )
        assert json.loads(history.read_text(encoding="utf-8")) == ["sf"]

    @pytest.mark.asyncio
    async def test_returns_limit_when_max_applies_reached(self, manager, mock_page, test_job):
        self._setup_manager(manager)
        manager.success_applies_num = 10
        manager.max_applies_num = 10
        new_page = AsyncMock()
        new_page.url = LINKEDIN_JOB_URL
        mock_page.context.new_page = AsyncMock(return_value=new_page)

        mock_llm = MagicMock()
        mock_llm.job_is_interesting.return_value = (True, 80, "Great fit")
        mock_llm.set_job = MagicMock()
        manager.llm_answerer_component = mock_llm

        with (
            patch.object(
                manager,
                "_get_detailed_job_description",
                new_callable=AsyncMock,
                return_value=test_job,
            ),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.async_pause",
                new_callable=AsyncMock,
            ),
            patch.object(
                manager, "easy_apply", new_callable=AsyncMock, return_value=("Success", "")
            ),
            patch.object(manager, "_handle_apply_result", new_callable=AsyncMock),
            patch.object(manager, "_extract_skills_from_vacancy", return_value="Python"),
            patch.object(manager, "_update_skill_stat"),
            patch("src.job_manager.linkedin.job_manager_linkedin.COLLECT_INFO_MODE", False),
            patch("src.job_manager.linkedin.job_manager_linkedin.MONKEY_MODE", False),
            patch("src.job_manager.linkedin.job_manager_linkedin.EASY_APPLY_ONLY_MODE", True),
        ):
            result = await manager.apply_job({"url": LINKEDIN_JOB_URL})

        assert result == "Limit"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("easy_apply_only", "apply_url", "worker_name"),
        [
            (True, None, "Easy Apply"),
            (False, "https://external.example/apply", "external"),
        ],
    )
    async def test_shutdown_during_dispatch_starts_no_easy_or_external_worker(
        self, manager, mock_page, test_job, easy_apply_only, apply_url, worker_name
    ):
        self._setup_manager(manager)
        new_page = AsyncMock()
        new_page.url = LINKEDIN_JOB_URL
        mock_page.context.new_page = AsyncMock(return_value=new_page)

        mock_llm = MagicMock()
        mock_llm.job_is_interesting.return_value = (True, 80, "Great fit")
        mock_llm.set_job = MagicMock(
            side_effect=lambda *_: runtime_controller.request_shutdown("dispatch-test")
        )
        manager.llm_answerer_component = mock_llm
        manager.llm_agent_component = MagicMock()
        manager.llm_agent_component.apply_to_job = AsyncMock()

        with (
            patch.object(
                manager,
                "_get_detailed_job_description",
                new_callable=AsyncMock,
                return_value=test_job,
            ),
            patch.object(
                manager,
                "_check_apply_button",
                new_callable=AsyncMock,
                return_value=apply_url,
            ),
            patch.object(manager, "easy_apply", new_callable=AsyncMock) as easy_apply,
            patch.object(manager, "_handle_apply_result", new_callable=AsyncMock) as handle,
            patch.object(manager, "_extract_skills_from_vacancy", return_value="Python"),
            patch.object(manager, "_update_skill_stat"),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.async_pause",
                new_callable=AsyncMock,
            ),
            patch("src.job_manager.linkedin.job_manager_linkedin.COLLECT_INFO_MODE", False),
            patch("src.job_manager.linkedin.job_manager_linkedin.MONKEY_MODE", False),
            patch(
                "src.job_manager.linkedin.job_manager_linkedin.EASY_APPLY_ONLY_MODE",
                easy_apply_only,
            ),
        ):
            result = await manager.apply_job({"url": LINKEDIN_JOB_URL})

        assert result == "Cancelled"
        assert manager.cycle_attempted_num == 0
        manager.llm_agent_component.apply_to_job.assert_not_awaited()
        easy_apply.assert_not_awaited()
        handle.assert_awaited_once()
        assert handle.await_args.args[0] == (
            "Cancelled",
            f"CANCELLED_BY_SHUTDOWN: {worker_name} worker was not started",
        )
        assert runtime_controller.wait_for_drain(timeout=0) is True

    @pytest.mark.asyncio
    async def test_active_worker_drains_and_finalizes_before_job_exit(self, manager):
        worker_started = asyncio.Event()
        finish_worker = asyncio.Event()
        lifecycle = []

        async def run_active_worker(_vacancy):
            assert runtime_controller.try_start_worker("easy_apply") is True
            lifecycle.append("worker_started")
            runtime_controller.request_shutdown("drain-test")
            worker_started.set()
            await finish_worker.wait()
            lifecycle.append("result_finalized")
            runtime_controller.finish_worker("easy_apply")
            return "Success"

        with patch.object(
            manager,
            "_apply_admitted_job",
            new_callable=AsyncMock,
            side_effect=run_active_worker,
        ) as admitted_job:
            active_task = asyncio.create_task(manager.apply_job({"url": LINKEDIN_JOB_URL}))
            await worker_started.wait()

            assert active_task.done() is False
            assert (
                await manager.apply_job({"url": "https://linkedin.com/jobs/view/2"}) == "Shutdown"
            )
            assert runtime_controller.wait_for_drain(timeout=0) is False

            finish_worker.set()
            assert await active_task == "Success"

        admitted_job.assert_awaited_once()
        assert lifecycle == ["worker_started", "result_finalized"]
        assert runtime_controller.wait_for_drain(timeout=0) is True
