"""Tests for src/job_manager/indeed/easy_applier_indeed.py"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.job_manager.easy_applier import NoInfoException
from src.job_manager.indeed.easy_applier_indeed import IndeedEasyApplier
from src.pydantic_models.job_models import Job, Question

INDEED_JOB_URL = "https://www.indeed.com/viewjob?jk=abc123"


@pytest.fixture
def mock_page():
    page = AsyncMock()
    page.url = INDEED_JOB_URL
    page.context = AsyncMock()
    return page


@pytest.fixture
def mock_gpt_answerer():
    gpt = MagicMock()
    gpt.write_cover_letter.return_value = "Dear Hiring Manager, ..."
    gpt.answer_question_textual_wide_range.return_value = "My answer"
    gpt.answer_question_numeric.return_value = "5"
    gpt.answer_question_date.return_value = "01/15/2024"
    gpt.select_one_answer_from_options.return_value = "yes"
    gpt.select_many_answers_from_options.return_value = ["yes"]
    gpt.resume_structured = {"personal_information": {}, "experience_details": []}
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
        "src.job_manager.indeed.easy_applier_indeed.get_ready_made_resume", return_value=None
    ):
        with patch("src.job_manager.indeed.easy_applier_indeed.load_yaml_file", return_value=None):
            inst = IndeedEasyApplier(
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
    inst.current_job = Job(job_title="Engineer", company_name="TestCorp", url=INDEED_JOB_URL)
    return inst


@pytest.fixture
def test_job():
    return Job(job_title="Engineer", company_name="TestCorp", url=INDEED_JOB_URL)


class TestInit:
    def test_sets_attributes(self, applier, mock_page):
        assert applier.page is mock_page
        assert applier.test_mode is False
        assert applier.all_questions == []
        assert applier.previous_question_texts == []

    def test_generated_resume_dir_is_subdir(self, applier):
        assert applier.generated_resume_dir.name == "generated_resumes"


class TestApplyToJob:
    @pytest.mark.asyncio
    async def test_navigates_and_delegates(self, applier, test_job):
        applier.submitted_resume_path = None
        applier.job_easy_apply = AsyncMock(return_value=("Success", ""))
        with patch("src.job_manager.indeed.easy_applier_indeed.emit_event"):
            with patch("src.job_manager.indeed.easy_applier_indeed.async_pause"):
                result = await applier.apply_to_job(test_job)
        applier.page.goto.assert_called_once_with(test_job.url)
        assert result == (("Success", ""), None)


class TestJobEasyApply:
    @pytest.mark.asyncio
    async def test_returns_skip_when_no_apply_button(self, applier, test_job):
        applier._find_apply_button = AsyncMock(return_value=None)
        result = await applier.job_easy_apply(test_job)
        assert result[0] == "Skip"

    @pytest.mark.asyncio
    async def test_test_mode_discards_and_returns_success(self, applier, test_job):
        applier.test_mode = True
        mock_btn = AsyncMock()
        applier._find_apply_button = AsyncMock(return_value=mock_btn)
        applier._fill_application_form = AsyncMock(return_value="")
        applier._discard_application = AsyncMock()

        mock_page_info = AsyncMock()
        mock_page_info.value = AsyncMock()
        applier.page.context.expect_page = MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(return_value=mock_page_info),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        with patch("src.job_manager.indeed.easy_applier_indeed.capture_page_screenshot"):
            with patch("src.job_manager.indeed.easy_applier_indeed.emit_event"):
                result = await applier.job_easy_apply(test_job)

        assert result[0] == "Success"
        applier._discard_application.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_error_on_exception(self, applier, test_job):
        applier._find_apply_button = AsyncMock(return_value=AsyncMock())
        applier._fill_application_form = AsyncMock(side_effect=RuntimeError("form error"))
        applier._discard_application = AsyncMock()

        applier.page.context.expect_page = MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(side_effect=Exception("no new page")),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        with patch("src.job_manager.indeed.easy_applier_indeed.capture_page_screenshot"):
            with patch("src.job_manager.indeed.easy_applier_indeed.debug_capture"):
                with patch("src.job_manager.indeed.easy_applier_indeed.async_pause"):
                    result = await applier.job_easy_apply(test_job)

        assert result[0] == "Error"

    @pytest.mark.asyncio
    async def test_returns_skip_on_no_info_exception(self, applier, test_job):
        applier._find_apply_button = AsyncMock(return_value=AsyncMock())
        applier._fill_application_form = AsyncMock(side_effect=NoInfoException("missing data"))
        applier._discard_application = AsyncMock()

        applier.page.context.expect_page = MagicMock(
            return_value=MagicMock(
                __aenter__=AsyncMock(side_effect=Exception("no new page")),
                __aexit__=AsyncMock(return_value=None),
            )
        )

        with patch("src.job_manager.indeed.easy_applier_indeed.capture_page_screenshot"):
            with patch("src.job_manager.indeed.easy_applier_indeed.debug_capture"):
                with patch("src.job_manager.indeed.easy_applier_indeed.async_pause"):
                    result = await applier.job_easy_apply(test_job)

        assert result[0] == "Skip"


class TestFindApplyButton:
    @pytest.mark.asyncio
    async def test_returns_button_when_found(self, applier):
        mock_btn = AsyncMock()
        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=mock_btn,
        ):
            result = await applier._find_apply_button(MagicMock())
        assert result is mock_btn

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self, applier):
        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await applier._find_apply_button(MagicMock())
        assert result is None


class TestHandleTermsOfService:
    @pytest.mark.asyncio
    async def test_always_returns_false(self, applier):
        result = await applier._handle_terms_of_service(MagicMock())
        assert result is False


class TestParseDateToMmddyyyy:
    def test_parses_iso_format(self, applier):
        assert applier._parse_date_to_mmddyyyy("2024-01-15") == "01/15/2024"

    def test_parses_mmddyyyy(self, applier):
        assert applier._parse_date_to_mmddyyyy("01/15/2024") == "01/15/2024"

    def test_parses_long_month_name(self, applier):
        assert applier._parse_date_to_mmddyyyy("January 15, 2024") == "01/15/2024"

    def test_parses_short_month_name(self, applier):
        assert applier._parse_date_to_mmddyyyy("Jan 15, 2024") == "01/15/2024"

    def test_returns_as_is_for_unknown_format(self, applier):
        assert applier._parse_date_to_mmddyyyy("not-a-date") == "not-a-date"


class TestIsNumericField:
    @pytest.mark.asyncio
    async def test_detects_number_type(self, applier):
        field = AsyncMock()
        field.get_attribute = AsyncMock(side_effect=lambda attr: "number" if attr == "type" else "")
        assert await applier._is_numeric_field(field) is True

    @pytest.mark.asyncio
    async def test_detects_inputmode_numeric(self, applier):
        field = AsyncMock()
        field.get_attribute = AsyncMock(
            side_effect=lambda attr: "numeric" if attr == "inputmode" else "text"
        )
        assert await applier._is_numeric_field(field) is True

    @pytest.mark.asyncio
    async def test_detects_number_input_id_prefix(self, applier):
        field = AsyncMock()
        field.get_attribute = AsyncMock(
            side_effect=lambda attr: "number-input-years" if attr == "id" else "text"
        )
        assert await applier._is_numeric_field(field) is True

    @pytest.mark.asyncio
    async def test_returns_false_for_plain_text(self, applier):
        field = AsyncMock()
        field.get_attribute = AsyncMock(side_effect=lambda attr: "text" if attr == "type" else "")
        assert await applier._is_numeric_field(field) is False

    @pytest.mark.asyncio
    async def test_detects_keyword_in_question_text(self, applier):
        field = AsyncMock()
        field.get_attribute = AsyncMock(return_value="text")
        assert await applier._is_numeric_field(field, "what is your expected salary?") is True

    @pytest.mark.asyncio
    async def test_keyword_not_matched_as_substring(self, applier):
        field = AsyncMock()
        field.get_attribute = AsyncMock(return_value="text")
        # "rate" is a substring of "demonstrates" — must not match
        question = "include github code samples and any evaluation or benchmark work that demonstrates your technical rigor."
        assert await applier._is_numeric_field(field, question) is False

    @pytest.mark.asyncio
    async def test_keyword_matched_as_whole_word(self, applier):
        field = AsyncMock()
        field.get_attribute = AsyncMock(return_value="text")
        assert await applier._is_numeric_field(field, "what is your hourly rate?") is True


class TestFindAndHandleDateQuestion:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_date_input(self, applier):
        section = MagicMock()
        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await applier._find_and_handle_date_question(section)
        assert result is False

    @pytest.mark.asyncio
    async def test_fills_date_from_llm(self, applier):
        date_input = AsyncMock()
        section = MagicMock()
        applier.gpt_answerer.answer_question_date.return_value = "2024-01-15"

        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=date_input,
        ):
            with patch(
                "src.job_manager.indeed.easy_applier_indeed.get_clean_text",
                new_callable=AsyncMock,
                return_value="Start date",
            ):
                with patch("src.job_manager.easy_applier.save_yaml_file"):
                    result = await applier._find_and_handle_date_question(section)

        assert result is True
        date_input.fill.assert_called_once_with("01/15/2024")

    @pytest.mark.asyncio
    async def test_uses_cached_answer(self, applier):
        date_input = AsyncMock()
        applier.all_questions = [
            Question(question="start date", question_type="date", answer="03/10/2023")
        ]

        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=date_input,
        ):
            with patch(
                "src.job_manager.indeed.easy_applier_indeed.get_clean_text",
                new_callable=AsyncMock,
                return_value="Start Date",
            ):
                result = await applier._find_and_handle_date_question(MagicMock())

        assert result is True
        date_input.fill.assert_called_once_with("03/10/2023")
        applier.gpt_answerer.answer_question_date.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_no_info_exception(self, applier):
        date_input = AsyncMock()
        applier.gpt_answerer.answer_question_date.return_value = "no info available"

        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=date_input,
        ):
            with patch(
                "src.job_manager.indeed.easy_applier_indeed.get_clean_text",
                new_callable=AsyncMock,
                return_value="Start date",
            ):
                with pytest.raises(NoInfoException):
                    await applier._find_and_handle_date_question(MagicMock())


class TestFindAndHandleTextboxQuestion:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_input(self, applier):
        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await applier._find_and_handle_textbox_question(MagicMock())
        assert result is False

    @pytest.mark.asyncio
    async def test_fills_text_from_llm(self, applier):
        text_input = AsyncMock()
        text_input.get_attribute = AsyncMock(return_value="text")
        applier.gpt_answerer.answer_question_textual_wide_range.return_value = "My answer"

        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=text_input,
        ):
            with patch(
                "src.job_manager.indeed.easy_applier_indeed.get_clean_text",
                new_callable=AsyncMock,
                return_value="What is your experience?",
            ):
                with patch("src.job_manager.easy_applier.save_yaml_file"):
                    result = await applier._find_and_handle_textbox_question(MagicMock())

        assert result is True
        text_input.fill.assert_called_once_with("My answer")

    @pytest.mark.asyncio
    async def test_uses_cached_answer(self, applier):
        text_input = AsyncMock()
        text_input.get_attribute = AsyncMock(return_value="text")
        applier.all_questions = [
            Question(
                question="what is your experience?", question_type="text", answer="Cached answer"
            )
        ]

        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=text_input,
        ):
            with patch(
                "src.job_manager.indeed.easy_applier_indeed.get_clean_text",
                new_callable=AsyncMock,
                return_value="What is your experience?",
            ):
                result = await applier._find_and_handle_textbox_question(MagicMock())

        assert result is True
        text_input.fill.assert_called_once_with("Cached answer")
        applier.gpt_answerer.answer_question_textual_wide_range.assert_not_called()

    @pytest.mark.asyncio
    async def test_fills_numeric_field(self, applier):
        text_input = AsyncMock()
        text_input.get_attribute = AsyncMock(return_value="number")
        applier.gpt_answerer.answer_question_numeric.return_value = "3"

        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=text_input,
        ):
            with patch(
                "src.job_manager.indeed.easy_applier_indeed.get_clean_text",
                new_callable=AsyncMock,
                return_value="Years of experience",
            ):
                with patch("src.job_manager.easy_applier.save_yaml_file"):
                    result = await applier._find_and_handle_textbox_question(MagicMock())

        assert result is True
        text_input.fill.assert_called_once_with("3")

    @pytest.mark.asyncio
    async def test_raises_no_info_exception(self, applier):
        text_input = AsyncMock()
        text_input.get_attribute = AsyncMock(return_value="text")
        applier.gpt_answerer.answer_question_textual_wide_range.return_value = "no info found"

        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=text_input,
        ):
            with patch(
                "src.job_manager.indeed.easy_applier_indeed.get_clean_text",
                new_callable=AsyncMock,
                return_value="Some question",
            ):
                with pytest.raises(NoInfoException):
                    await applier._find_and_handle_textbox_question(MagicMock())


class TestFindAndHandleDropdownQuestion:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_dropdown(self, applier):
        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await applier._find_and_handle_dropdown_question(MagicMock())
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_question_text(self, applier):
        dropdown = AsyncMock()
        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=dropdown,
        ):
            with patch(
                "src.job_manager.indeed.easy_applier_indeed.get_clean_text",
                new_callable=AsyncMock,
                return_value="",
            ):
                with patch(
                    "src.job_manager.indeed.easy_applier_indeed.find_elements_safely",
                    new_callable=AsyncMock,
                    return_value=[],
                ):
                    result = await applier._find_and_handle_dropdown_question(MagicMock())
        assert result is False

    @pytest.mark.asyncio
    async def test_selects_exact_match(self, applier):
        dropdown = AsyncMock()
        opt1 = AsyncMock()
        opt2 = AsyncMock()
        applier.gpt_answerer.select_one_answer_from_options.return_value = "Full-time"

        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=dropdown,
        ):
            with patch(
                "src.job_manager.indeed.easy_applier_indeed.get_clean_text",
                new_callable=AsyncMock,
                side_effect=["Employment type", "Part-time", "Full-time"],
            ):
                with patch(
                    "src.job_manager.indeed.easy_applier_indeed.find_elements_safely",
                    new_callable=AsyncMock,
                    return_value=[opt1, opt2],
                ):
                    with patch("src.job_manager.easy_applier.save_yaml_file"):
                        result = await applier._find_and_handle_dropdown_question(MagicMock())

        assert result is True
        dropdown.select_option.assert_called_once_with(label="Full-time")

    @pytest.mark.asyncio
    async def test_raises_no_info_exception(self, applier):
        dropdown = AsyncMock()
        opt = AsyncMock()
        applier.gpt_answerer.select_one_answer_from_options.return_value = "no info"

        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=dropdown,
        ):
            with patch(
                "src.job_manager.indeed.easy_applier_indeed.get_clean_text",
                new_callable=AsyncMock,
                side_effect=["Employment type", "Part-time"],
            ):
                with patch(
                    "src.job_manager.indeed.easy_applier_indeed.find_elements_safely",
                    new_callable=AsyncMock,
                    return_value=[opt],
                ):
                    with pytest.raises(NoInfoException):
                        await applier._find_and_handle_dropdown_question(MagicMock())


class TestFindAndHandleRadioQuestion:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_radio(self, applier):
        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await applier._find_and_handle_radio_question(MagicMock())
        assert result is False

    @pytest.mark.asyncio
    async def test_selects_exact_match(self, applier):
        radio_element = AsyncMock()
        radio_element.get_attribute = AsyncMock(return_value="radio-yes")
        label = AsyncMock()
        label.is_visible = AsyncMock(return_value=True)
        applier.gpt_answerer.select_one_answer_from_options.return_value = "yes"

        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            # 1st: initial radio check, 2nd: label for option text, 3rd: label in click_radio
            side_effect=[radio_element, label, label],
        ):
            with patch(
                "src.job_manager.indeed.easy_applier_indeed.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[radio_element],
            ):
                with patch(
                    "src.job_manager.indeed.easy_applier_indeed.get_clean_text",
                    new_callable=AsyncMock,
                    side_effect=["Are you authorized to work?", "yes"],
                ):
                    with patch("src.job_manager.easy_applier.save_yaml_file"):
                        result = await applier._find_and_handle_radio_question(MagicMock())

        assert result is True

    @pytest.mark.asyncio
    async def test_uses_cached_answer(self, applier):
        radio = AsyncMock()
        radio.get_attribute = AsyncMock(return_value="radio-yes")
        label = AsyncMock()
        label.is_visible = AsyncMock(return_value=True)
        applier.all_questions = [
            Question(question="are you authorized to work?", question_type="radio", answer="yes")
        ]

        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            # 1st: initial radio check, 2nd: label for option text, 3rd: label in click_radio
            side_effect=[radio, label, label],
        ):
            with patch(
                "src.job_manager.indeed.easy_applier_indeed.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[radio],
            ):
                with patch(
                    "src.job_manager.indeed.easy_applier_indeed.get_clean_text",
                    new_callable=AsyncMock,
                    side_effect=["Are you authorized to work?", "yes"],
                ):
                    result = await applier._find_and_handle_radio_question(MagicMock())

        assert result is True
        applier.gpt_answerer.select_one_answer_from_options.assert_not_called()


class TestFindAndHandleCheckboxQuestion:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_checkbox(self, applier):
        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await applier._find_and_handle_checkbox_question(MagicMock())
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_options_found(self, applier):
        checkbox = AsyncMock()
        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=checkbox,
        ):
            with patch(
                "src.job_manager.indeed.easy_applier_indeed.find_elements_safely",
                new_callable=AsyncMock,
                return_value=[],
            ):
                with patch(
                    "src.job_manager.indeed.easy_applier_indeed.get_clean_text",
                    new_callable=AsyncMock,
                    return_value="Select all that apply",
                ):
                    result = await applier._find_and_handle_checkbox_question(MagicMock())
        assert result is False


class TestSubmitApplication:
    @pytest.mark.asyncio
    async def test_returns_true_on_success(self, applier):
        submit_btn = AsyncMock()
        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            side_effect=[None, submit_btn],
        ):
            with patch("src.job_manager.indeed.easy_applier_indeed.async_pause"):
                result = await applier._submit_application()
        assert result is True
        submit_btn.click.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_no_submit_button(self, applier):
        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with patch("src.job_manager.indeed.easy_applier_indeed.debug_capture"):
                result = await applier._submit_application()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self, applier):
        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            side_effect=RuntimeError("click failed"),
        ):
            with patch("src.job_manager.indeed.easy_applier_indeed.debug_capture"):
                result = await applier._submit_application()
        assert result is False


class TestDiscardApplication:
    @pytest.mark.asyncio
    async def test_clicks_close_button(self, applier):
        close_btn = AsyncMock()
        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            side_effect=[close_btn, None],
        ):
            with patch("src.job_manager.indeed.easy_applier_indeed.async_pause"):
                await applier._discard_application()
        close_btn.click.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_raise_on_exception(self, applier):
        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            side_effect=RuntimeError("no button"),
        ):
            with patch("src.job_manager.indeed.easy_applier_indeed.debug_capture"):
                await applier._discard_application()  # should not raise


class TestFillTextboxQuestionErrors:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_invalid_inputs(self, applier):
        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_elements_safely",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await applier._fill_textbox_question_errors()
        assert result is False

    @pytest.mark.asyncio
    async def test_fills_invalid_inputs(self, applier):
        input_el = AsyncMock()
        input_el.get_attribute = AsyncMock(
            side_effect=lambda attr: "error-id" if attr == "aria-describedby" else "field-id"
        )
        input_el.input_value = AsyncMock(return_value="bad value")
        applier.gpt_answerer.answer_question_textual_wide_range_with_error = MagicMock(
            return_value="corrected value"
        )

        error_locator = AsyncMock()
        error_locator.count = AsyncMock(return_value=1)
        error_locator.text_content = AsyncMock(return_value="Please enter a valid value")
        applier.page.locator = MagicMock(return_value=error_locator)

        label_locator = AsyncMock()
        label_locator.count = AsyncMock(return_value=1)
        label_locator.text_content = AsyncMock(return_value="Years of experience")
        applier.page.locator = MagicMock(
            side_effect=lambda sel: error_locator if "error-id" in sel else label_locator
        )

        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_elements_safely",
            new_callable=AsyncMock,
            return_value=[input_el],
        ):
            with patch("src.job_manager.easy_applier.save_yaml_file"):
                result = await applier._fill_textbox_question_errors()

        assert result is True
        input_el.fill.assert_called_once_with("corrected value")


class TestFillProfileLocationPage:
    @pytest.mark.asyncio
    async def test_fills_location_fields(self, applier):
        applier.gpt_answerer.resume_structured = {
            "personal_information": {"zip_code": "90210", "city": "Beverly Hills", "address": ""}
        }
        postal_field = AsyncMock()
        city_field = AsyncMock()

        call_count = {"n": 0}

        async def mock_find(page, selector, timeout=None):
            call_count["n"] += 1
            if "postal-code" in selector:
                return postal_field
            if "locality" in selector:
                return city_field
            return None

        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            side_effect=mock_find,
        ):
            await applier._fill_profile_location_page()

        postal_field.fill.assert_called_once_with("90210")
        city_field.fill.assert_called_once_with("Beverly Hills")

    @pytest.mark.asyncio
    async def test_skips_empty_fields(self, applier):
        applier.gpt_answerer.resume_structured = {
            "personal_information": {"zip_code": "", "city": "", "address": ""}
        }
        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
        ) as mock_find:
            await applier._fill_profile_location_page()
        mock_find.assert_not_called()


class TestFillRelevantExperiencePage:
    @pytest.mark.asyncio
    async def test_fills_title_and_company_when_empty(self, applier):
        applier.gpt_answerer.resume_structured = {
            "experience_details": [{"position": "Engineer", "company": "TestCorp"}]
        }
        title_input = AsyncMock()
        title_input.input_value = AsyncMock(return_value="")
        company_input = AsyncMock()
        company_input.input_value = AsyncMock(return_value="")

        async def mock_find(page, selector, timeout=None):
            if "job-title" in selector:
                return title_input
            if "company-name" in selector:
                return company_input
            return None

        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            side_effect=mock_find,
        ):
            with patch(
                "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
                new_callable=AsyncMock,
                side_effect=[None, mock_find, title_input, company_input],
            ):
                # Simulate no radio group, then text inputs
                call_results = [None, title_input, company_input]
                idx = {"i": 0}

                async def sequential_find(page, selector, timeout=None):
                    if "RadioCardGroup" in selector:
                        return None
                    if "job-title" in selector:
                        return title_input
                    if "company-name" in selector:
                        return company_input
                    return None

                with patch(
                    "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
                    new_callable=AsyncMock,
                    side_effect=sequential_find,
                ):
                    await applier._fill_relevant_experience_page()

        title_input.fill.assert_called_once_with("Engineer")
        company_input.fill.assert_called_once_with("TestCorp")

    @pytest.mark.asyncio
    async def test_skips_prefilled_fields(self, applier):
        applier.gpt_answerer.resume_structured = {
            "experience_details": [{"position": "Engineer", "company": "TestCorp"}]
        }
        title_input = AsyncMock()
        title_input.input_value = AsyncMock(return_value="Senior Engineer")

        async def sequential_find(page, selector, timeout=None):
            if "RadioCardGroup" in selector:
                return None
            if "job-title" in selector:
                return title_input
            return None

        with patch(
            "src.job_manager.indeed.easy_applier_indeed.find_element_safely",
            new_callable=AsyncMock,
            side_effect=sequential_find,
        ):
            await applier._fill_relevant_experience_page()

        title_input.fill.assert_not_called()
