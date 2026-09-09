"""Tests for src/job_manager/easy_applier.py"""

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.job_manager.easy_applier import BaseEasyApplier
from src.pydantic_models.job_models import Job, Question


class ConcreteEasyApplier(BaseEasyApplier):
    """Minimal concrete implementation for testing BaseEasyApplier methods."""

    def __init__(self):
        super().__init__()
        self.page = MagicMock()
        self.generated_resume_dir = "/mock/resumes"
        self.resume_generator_manager = AsyncMock()
        self.answers_file = Path("/mock/answers.yaml")
        self.all_questions = []
        self.current_job = Job(job_title="Engineer", company_name="TestCorp")

    async def apply_to_job(self, job):
        pass

    async def job_easy_apply(self, job):
        return ("Success", "")

    async def _handle_terms_of_service(self, section):
        return False

    async def _find_and_handle_radio_question(self, section):
        return False

    async def _find_and_handle_checkbox_question(self, section):
        return False

    async def _find_and_handle_textbox_question(self, section):
        return False

    async def _find_and_handle_dropdown_question(self, section):
        return False


@pytest.fixture
def applier():
    return ConcreteEasyApplier()


class TestProcessFormSection:
    @pytest.mark.asyncio
    async def test_stops_at_terms_of_service(self, applier):
        applier._handle_terms_of_service = AsyncMock(return_value=True)
        applier._find_and_handle_radio_question = AsyncMock(return_value=False)

        await applier._process_form_section(MagicMock())

        applier._handle_terms_of_service.assert_called_once()
        applier._find_and_handle_radio_question.assert_not_called()

    @pytest.mark.asyncio
    async def test_stops_at_radio_question(self, applier):
        applier._handle_terms_of_service = AsyncMock(return_value=False)
        applier._find_and_handle_radio_question = AsyncMock(return_value=True)
        applier._find_and_handle_checkbox_question = AsyncMock(return_value=False)

        await applier._process_form_section(MagicMock())

        applier._find_and_handle_radio_question.assert_called_once()
        applier._find_and_handle_checkbox_question.assert_not_called()

    @pytest.mark.asyncio
    async def test_stops_at_checkbox_question(self, applier):
        applier._handle_terms_of_service = AsyncMock(return_value=False)
        applier._find_and_handle_radio_question = AsyncMock(return_value=False)
        applier._find_and_handle_checkbox_question = AsyncMock(return_value=True)
        applier._find_and_handle_dropdown_question = AsyncMock(return_value=False)

        await applier._process_form_section(MagicMock())

        applier._find_and_handle_checkbox_question.assert_called_once()
        applier._find_and_handle_dropdown_question.assert_not_called()

    @pytest.mark.asyncio
    async def test_stops_at_dropdown_question(self, applier):
        applier._handle_terms_of_service = AsyncMock(return_value=False)
        applier._find_and_handle_radio_question = AsyncMock(return_value=False)
        applier._find_and_handle_checkbox_question = AsyncMock(return_value=False)
        applier._find_and_handle_dropdown_question = AsyncMock(return_value=True)
        applier._find_and_handle_textbox_question = AsyncMock(return_value=False)

        await applier._process_form_section(MagicMock())

        applier._find_and_handle_dropdown_question.assert_called_once()
        applier._find_and_handle_textbox_question.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_through_to_textbox(self, applier):
        applier._handle_terms_of_service = AsyncMock(return_value=False)
        applier._find_and_handle_radio_question = AsyncMock(return_value=False)
        applier._find_and_handle_checkbox_question = AsyncMock(return_value=False)
        applier._find_and_handle_dropdown_question = AsyncMock(return_value=False)
        applier._find_and_handle_textbox_question = AsyncMock(return_value=True)

        await applier._process_form_section(MagicMock())

        applier._find_and_handle_textbox_question.assert_called_once()

    @pytest.mark.asyncio
    async def test_date_question_handler_called_before_textbox(self, applier):
        applier._handle_terms_of_service = AsyncMock(return_value=False)
        applier._find_and_handle_radio_question = AsyncMock(return_value=False)
        applier._find_and_handle_checkbox_question = AsyncMock(return_value=False)
        applier._find_and_handle_dropdown_question = AsyncMock(return_value=False)
        applier._find_and_handle_date_question = AsyncMock(return_value=True)
        applier._find_and_handle_textbox_question = AsyncMock(return_value=False)

        await applier._process_form_section(MagicMock())

        applier._find_and_handle_date_question.assert_called_once()
        applier._find_and_handle_textbox_question.assert_not_called()


class TestAnswerContainsCompanyName:
    def test_returns_true_when_company_name_in_answer(self, applier):
        applier.current_job = Job(job_title="Engineer", company_name="TestCorp")
        assert applier._answer_contains_company_name("I applied at TestCorp last year") is True

    def test_returns_false_when_company_name_not_in_answer(self, applier):
        applier.current_job = Job(job_title="Engineer", company_name="TestCorp")
        assert applier._answer_contains_company_name("I have 5 years of experience") is False

    def test_returns_false_when_answer_not_a_string(self, applier):
        applier.current_job = Job(job_title="Engineer", company_name="TestCorp")
        assert applier._answer_contains_company_name(["TestCorp", "other"]) is False

    def test_returns_false_for_non_string_answer(self, applier):
        applier.current_job = Job(job_title="Engineer", company_name="TestCorp")
        assert applier._answer_contains_company_name(42) is False


class TestSaveQuestions:
    def test_saves_new_question(self, applier):
        applier.all_questions = []
        q = Question(question="Years of experience?", answer="5", question_type="text")

        with patch("src.job_manager.easy_applier.save_yaml_file") as mock_save:
            applier._save_questions(q)

        assert len(applier.all_questions) == 1
        mock_save.assert_called_once()

    def test_replaces_existing_question(self, applier):
        # sanitize_text lowercases, so pre-store the sanitized form
        existing = Question(question="years of experience?", answer="3", question_type="text")
        applier.all_questions = [existing]
        updated = Question(question="Years of experience?", answer="5", question_type="text")

        with patch("src.job_manager.easy_applier.save_yaml_file"):
            applier._save_questions(updated)

        assert len(applier.all_questions) == 1
        assert applier.all_questions[0].answer == "5"

    def test_skips_save_when_answer_contains_company_name(self, applier):
        applier.current_job = Job(job_title="Engineer", company_name="TestCorp")
        applier.all_questions = []
        q = Question(
            question="Why TestCorp?", answer="I love TestCorp culture", question_type="text"
        )

        with patch("src.job_manager.easy_applier.save_yaml_file") as mock_save:
            applier._save_questions(q)

        assert len(applier.all_questions) == 0
        mock_save.assert_not_called()

    def test_question_text_is_sanitized(self, applier):
        applier.all_questions = []
        q = Question(question="  Years of experience?  ", answer="5", question_type="text")

        with patch("src.job_manager.easy_applier.save_yaml_file"):
            with patch(
                "src.job_manager.easy_applier.sanitize_text",
                return_value="years of experience?",
            ) as mock_sanitize:
                applier._save_questions(q)

        mock_sanitize.assert_called_once_with("  Years of experience?  ")

    def test_debug_log_does_not_include_question_or_answer_values(self, applier):
        q = Question(
            question="Private screening question?",
            answer="candidate-private-answer",
            question_type="radio",
        )

        with patch("src.job_manager.easy_applier.save_yaml_file"):
            with patch("src.job_manager.easy_applier.logger.debug") as debug:
                applier._save_questions(q)

        rendered_calls = repr(debug.call_args_list)
        assert "Private screening question" not in rendered_calls
        assert "candidate-private-answer" not in rendered_calls


class TestLoadQuestions:
    def test_returns_empty_list_when_file_not_found(self, applier):
        from src.utils.utils import ConfigError

        with patch(
            "src.job_manager.easy_applier.load_yaml_file", side_effect=ConfigError("not found")
        ):
            result = applier._load_questions()

        assert result == []

    def test_returns_empty_list_when_data_is_none(self, applier):
        with patch("src.job_manager.easy_applier.load_yaml_file", return_value=None):
            result = applier._load_questions()

        assert result == []

    def test_returns_questions_from_yaml(self, applier):
        data = [
            {"question": "Years of experience?", "answer": "5", "question_type": "text"},
            {"question": "Preferred location?", "answer": "Remote", "question_type": "text"},
        ]
        with patch("src.job_manager.easy_applier.load_yaml_file", return_value=data):
            result = applier._load_questions()

        assert len(result) == 2
        assert all(isinstance(q, Question) for q in result)
        assert result[0].question == "Years of experience?"

    def test_raises_on_unexpected_error(self, applier):
        with patch(
            "src.job_manager.easy_applier.load_yaml_file", side_effect=ValueError("bad data")
        ):
            with patch("src.job_manager.easy_applier.logger"):
                with pytest.raises(Exception, match="Error loading questions"):
                    applier._load_questions()


class TestCreateAndUploadResume:
    @pytest.mark.asyncio
    async def test_creates_and_uploads_resume(self, applier, tmp_path):
        applier.generated_resume_dir = str(tmp_path)
        job = Job(job_title="Engineer", company_name="TestCorp")

        pdf_bytes = b"%PDF-1.4 fake content"
        applier.resume_generator_manager.pdf_base64 = AsyncMock(
            return_value=base64.b64encode(pdf_bytes).decode()
        )

        mock_element = AsyncMock()

        with patch("src.job_manager.easy_applier.async_pause"):
            await applier._create_and_upload_resume(mock_element, job)

        mock_element.set_input_files.assert_called_once()
        call_arg = mock_element.set_input_files.call_args[0][0]
        assert "CV_TestCorp_Engineer.pdf" in call_arg
        assert applier.submitted_resume_path == call_arg

    @pytest.mark.asyncio
    async def test_raises_when_file_exceeds_size_limit(self, applier, tmp_path):
        applier.generated_resume_dir = str(tmp_path)
        job = Job(job_title="Engineer", company_name="TestCorp")

        large_bytes = b"x" * (3 * 1024 * 1024)  # 3 MB
        applier.resume_generator_manager.pdf_base64 = AsyncMock(
            return_value=base64.b64encode(large_bytes).decode()
        )

        with pytest.raises(ValueError, match="exceeds the maximum limit"):
            await applier._create_and_upload_resume(AsyncMock(), job)

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit_then_succeeds(self, applier, tmp_path):
        from httpx import HTTPStatusError, Request, Response

        applier.generated_resume_dir = str(tmp_path)
        job = Job(job_title="Engineer", company_name="TestCorp")

        pdf_bytes = b"%PDF-1.4 fake"
        b64 = base64.b64encode(pdf_bytes).decode()

        request = Request("POST", "http://example.com")
        response = Response(429, request=request, headers={"retry-after": "1"})
        rate_limit_error = HTTPStatusError("Rate limited", request=request, response=response)

        applier.resume_generator_manager.pdf_base64 = AsyncMock(side_effect=[rate_limit_error, b64])

        mock_element = AsyncMock()
        with patch("src.job_manager.easy_applier.async_pause"):
            await applier._create_and_upload_resume(mock_element, job)

        assert applier.resume_generator_manager.pdf_base64.call_count == 2
        mock_element.set_input_files.assert_called_once()
