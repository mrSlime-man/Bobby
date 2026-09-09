from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.job_manager.linkedin.messages_manager_linkedin import LinkedInMessagesManager


@pytest.fixture
def page():
    return AsyncMock()


@pytest.fixture
def llm_answerer():
    answerer = MagicMock()
    answerer.classify_linkedin_message.return_value = {
        "category": "job_offer_to_me",
        "confidence": 92,
        "reasoning": "Recruiter-style outreach for a role.",
        "proposed_action": "draft_reply",
    }
    answerer.draft_linkedin_message_reply.return_value = "Thanks for reaching out. I'd be open to a short call to learn more."
    return answerer


@pytest.fixture
def manager(page, llm_answerer, tmp_path):
    resume_structured = {
        "personal_information": {
            "first_name": "Jordan",
            "last_name": "Example",
        }
    }
    return LinkedInMessagesManager(
        page=page,
        llm_answerer=llm_answerer,
        resume_structured=resume_structured,
        output_file=tmp_path / "messages_dry_run.yaml",
    )


class TestHelpers:
    def test_conversation_summary_target_uses_small_buffer(self):
        assert LinkedInMessagesManager._conversation_summary_target(1) == 3
        assert LinkedInMessagesManager._conversation_summary_target(10) == 13
        assert LinkedInMessagesManager._conversation_summary_target(25) == 30

    def test_normalize_classification_skips_personal_message_drafts(self, manager):
        conversation = {"participant_name": "Joseph Gasper"}
        classification = {
            "category": "personal_message",
            "confidence": 100,
            "reasoning": "Birthday message.",
            "proposed_action": "draft_reply",
        }

        normalized = manager._normalize_classification(conversation, classification)

        assert normalized["proposed_action"] == "skip"
        assert "No reply drafted automatically" in normalized["reasoning"]

    def test_normalize_classification_respects_personal_message_config(self, manager):
        manager.message_preferences["skip_drafting_for_personal_messages"] = False
        conversation = {"participant_name": "Joseph Gasper"}
        classification = {
            "category": "personal_message",
            "confidence": 100,
            "reasoning": "Birthday message.",
            "proposed_action": "draft_reply",
        }

        normalized = manager._normalize_classification(conversation, classification)

        assert normalized["proposed_action"] == "draft_reply"

    def test_sanitize_result_removes_dom_index(self):
        result = {
            "dom_index": 38,
            "participant_name": "Mahmoud Hammoud",
            "timestamp": "Jun 22, 2025",
            "snippet": "You: hello",
        }

        sanitized = LinkedInMessagesManager._sanitize_result(result)

        assert "dom_index" not in sanitized
        assert sanitized["participant_name"] == "Mahmoud Hammoud"

    def test_preview_detects_current_user_reply(self):
        assert LinkedInMessagesManager._is_preview_from_current_user("You: Thanks for the note")
        assert LinkedInMessagesManager._is_preview_from_current_user("Jobs You: Thanks for the note")
        assert not LinkedInMessagesManager._is_preview_from_current_user("Karen: Hello there")

    def test_last_message_detects_current_user_by_you(self, manager):
        messages = [{"sender": "Karen", "body": "Hello"}, {"sender": "You", "body": "Hi"}]
        assert manager._is_last_message_from_current_user(messages)

    def test_last_message_detects_current_user_by_name(self, manager):
        messages = [
            {"sender": "Karen", "body": "Hello"},
            {"sender": "Jordan Example", "body": "Hi"},
        ]
        assert manager._is_last_message_from_current_user(messages)

    def test_last_message_detects_current_user_by_name_with_suffix(self, manager):
        messages = [
            {"sender": "Sean Ellis", "body": "Hello"},
            {"sender": "Jordan Example , Eng.", "body": "Hi"},
        ]
        assert manager._is_last_message_from_current_user(messages)

    @pytest.mark.asyncio
    async def test_get_locator_text_returns_empty_when_missing(self, manager):
        locator = AsyncMock()
        locator.count.return_value = 0

        assert await manager._get_locator_text(locator) == ""


class TestRunDryRun:
    @pytest.mark.asyncio
    async def test_run_uses_bounded_summary_target(self, manager):
        with (
            patch.object(manager, "_open_inbox", new_callable=AsyncMock),
            patch.object(
                manager,
                "_load_conversation_summaries",
                new_callable=AsyncMock,
                return_value=[],
            ) as load_summaries,
        ):
            await manager.run_dry_run(limit=10)

        load_summaries.assert_awaited_once_with(13)

    @pytest.mark.asyncio
    async def test_skips_preview_when_user_already_replied(self, manager, llm_answerer):
        with (
            patch.object(manager, "_open_inbox", new_callable=AsyncMock),
            patch.object(
                manager,
                "_load_conversation_summaries",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "dom_index": 0,
                        "participant_name": "Maged George",
                        "timestamp": "Apr 25",
                        "snippet": "You: Thanks for connecting",
                    }
                ],
            ),
            patch.object(manager, "_persist_results") as persist_results,
        ):
            results = await manager.run_dry_run(limit=1)

        assert results[0]["processing_status"] == "skipped"
        assert results[0]["proposed_action"] == "skip"
        assert "dom_index" not in results[0]
        llm_answerer.classify_linkedin_message.assert_not_called()
        persist_results.assert_called_once()

    @pytest.mark.asyncio
    async def test_classifies_and_drafts_reply(self, manager, llm_answerer):
        conversation = {
            "participant_name": "Karen",
            "participant_headline": "Recruiter",
            "timestamp": "Apr 27",
            "snippet": "Karen: We have an interesting role for you",
            "last_sender": "Karen",
            "messages": [{"sender": "Karen", "timestamp": "9:00 AM", "body": "Interested in a CTO role?"}],
        }

        with (
            patch.object(manager, "_open_inbox", new_callable=AsyncMock),
            patch.object(
                manager,
                "_load_conversation_summaries",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "dom_index": 0,
                        "participant_name": "Karen",
                        "timestamp": "Apr 27",
                        "snippet": "Karen: We have an interesting role for you",
                    }
                ],
            ),
            patch.object(manager, "_open_conversation", new_callable=AsyncMock, return_value=True),
            patch.object(manager, "_extract_active_conversation", new_callable=AsyncMock, return_value=conversation),
            patch.object(
                manager,
                "_ensure_starred",
                new_callable=AsyncMock,
                return_value={"status": "dry_run", "message": "Conversation would be starred."},
            ),
            patch.object(
                manager,
                "_ensure_label_as_jobs",
                new_callable=AsyncMock,
                return_value={"status": "dry_run", "message": "Conversation would be labeled as jobs."},
            ),
            patch.object(
                manager,
                "_execute_action",
                new_callable=AsyncMock,
                return_value={"status": "dry_run", "message": "Action not executed."},
            ),
            patch.object(manager, "_persist_results"),
        ):
            results = await manager.run_dry_run(limit=1)

        assert results[0]["processing_status"] == "classified"
        assert results[0]["category"] == "job_offer_to_me"
        assert results[0]["proposed_action"] == "draft_reply"
        assert results[0]["draft_reply"]
        assert results[0]["action_execution"]["status"] == "dry_run"
        assert results[0]["star_execution"]["status"] == "dry_run"
        assert results[0]["label_as_jobs_execution"]["status"] == "dry_run"
        llm_answerer.classify_linkedin_message.assert_called_once_with(conversation)
        llm_answerer.draft_linkedin_message_reply.assert_called_once()

    @pytest.mark.asyncio
    async def test_personal_message_draft_reply_is_forced_to_skip(self, manager, llm_answerer):
        conversation = {
            "participant_name": "Joseph Gasper",
            "participant_headline": "Managing Director",
            "timestamp": "Aug 9, 2025",
            "snippet": "Joseph: Wishing you a very happy birthday!",
            "last_sender": "Joseph Gasper",
            "messages": [
                {
                    "sender": "Joseph Gasper",
                    "timestamp": "1:23 AM",
                    "body": "Wishing you a very happy birthday!",
                }
            ],
        }
        llm_answerer.classify_linkedin_message.return_value = {
            "category": "personal_message",
            "confidence": 100,
            "reasoning": "Birthday greeting.",
            "proposed_action": "draft_reply",
        }

        with (
            patch.object(manager, "_open_inbox", new_callable=AsyncMock),
            patch.object(
                manager,
                "_load_conversation_summaries",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "dom_index": 0,
                        "participant_name": "Joseph Gasper",
                        "timestamp": "Aug 9, 2025",
                        "snippet": "Joseph: Wishing you a very happy birthday!",
                    }
                ],
            ),
            patch.object(manager, "_open_conversation", new_callable=AsyncMock, return_value=True),
            patch.object(manager, "_extract_active_conversation", new_callable=AsyncMock, return_value=conversation),
            patch.object(
                manager,
                "_ensure_starred",
                new_callable=AsyncMock,
                return_value={"status": "not_applicable", "message": "Conversation does not require starring."},
            ),
            patch.object(
                manager,
                "_ensure_label_as_jobs",
                new_callable=AsyncMock,
                return_value={"status": "not_applicable", "message": "Conversation does not require label as jobs."},
            ),
            patch.object(
                manager,
                "_execute_action",
                new_callable=AsyncMock,
                return_value={"status": "skipped", "message": "No executable action for this classification."},
            ) as execute_action,
            patch.object(manager, "_persist_results"),
        ):
            results = await manager.run(dry_run=False, execute_archives=True, execute_replies=True, limit=1)

        assert results[0]["category"] == "personal_message"
        assert results[0]["proposed_action"] == "skip"
        llm_answerer.draft_linkedin_message_reply.assert_not_called()
        execute_action.assert_awaited_once_with(
            "skip",
            "",
            dry_run=False,
            execute_archives=True,
            execute_replies=True,
        )

    @pytest.mark.asyncio
    async def test_job_offer_respects_disabled_star_and_label_settings(self, manager, llm_answerer):
        manager.message_preferences.update(
            {
                "auto_star_job_offers": False,
                "auto_label_job_offers": False,
            }
        )
        conversation = {
            "participant_name": "Karen",
            "participant_headline": "Recruiter",
            "timestamp": "Apr 27",
            "snippet": "Karen: We have an interesting role for you",
            "last_sender": "Karen",
            "messages": [{"sender": "Karen", "timestamp": "9:00 AM", "body": "Interested in a CTO role?"}],
        }

        with (
            patch.object(manager, "_open_inbox", new_callable=AsyncMock),
            patch.object(
                manager,
                "_load_conversation_summaries",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "dom_index": 0,
                        "participant_name": "Karen",
                        "timestamp": "Apr 27",
                        "snippet": "Karen: We have an interesting role for you",
                    }
                ],
            ),
            patch.object(manager, "_open_conversation", new_callable=AsyncMock, return_value=True),
            patch.object(manager, "_extract_active_conversation", new_callable=AsyncMock, return_value=conversation),
            patch.object(
                manager,
                "_ensure_starred",
                new_callable=AsyncMock,
                return_value={"status": "not_applicable", "message": "Conversation does not require starring."},
            ) as ensure_starred,
            patch.object(
                manager,
                "_ensure_label_as_jobs",
                new_callable=AsyncMock,
                return_value={"status": "not_applicable", "message": "Conversation does not require label as jobs."},
            ) as ensure_label,
            patch.object(
                manager,
                "_execute_action",
                new_callable=AsyncMock,
                return_value={"status": "dry_run", "message": "Action not executed."},
            ),
            patch.object(manager, "_persist_results"),
        ):
            await manager.run_dry_run(limit=1)

        ensure_starred.assert_awaited_once_with(should_star=False, dry_run=True)
        ensure_label.assert_awaited_once_with(should_label=False, dry_run=True)

    @pytest.mark.asyncio
    async def test_persist_results_writes_yaml(self, manager):
        results = [
            {
                "participant_name": "Karen",
                "processing_status": "classified",
                "category": "job_offer_to_me",
                "proposed_action": "draft_reply",
                "action_execution": {"status": "dry_run", "message": "Action not executed."},
                "star_execution": {"status": "dry_run", "message": "Conversation would be starred."},
            }
        ]
        report = manager._build_final_report(
            results,
            dry_run=True,
            execute_archives=False,
            execute_replies=False,
        )

        manager._persist_results(
            results,
            limit=5,
            dry_run=True,
            execute_archives=False,
            execute_replies=False,
            report=report,
        )

        assert Path(manager.output_file).exists()
        saved = Path(manager.output_file).read_text(encoding="utf-8")
        assert "generated_at:" not in saved
        assert "report:" not in saved
        assert "participant_name: Karen" in saved

    @pytest.mark.asyncio
    async def test_persist_results_updates_existing_entry_without_overwriting_others(self, manager):
        existing_entries = [
            {
                "participant_name": "Karen",
                "timestamp": "Apr 27",
                "snippet": "Hi there",
                "processing_status": "classified",
                "category": "marketing_spam",
            },
            {
                "participant_name": "Sean",
                "timestamp": "Apr 26",
                "snippet": "Role for you",
                "processing_status": "classified",
                "category": "job_offer_to_me",
            },
        ]
        manager.output_file.write_text(
            "- participant_name: Karen\n"
            "  timestamp: Apr 27\n"
            "  snippet: Hi there\n"
            "  processing_status: classified\n"
            "  category: marketing_spam\n"
            "- participant_name: Sean\n"
            "  timestamp: Apr 26\n"
            "  snippet: Role for you\n"
            "  processing_status: classified\n"
            "  category: job_offer_to_me\n",
            encoding="utf-8",
        )

        results = [
            {
                "participant_name": "Karen",
                "timestamp": "Apr 27",
                "snippet": "Hi there",
                "processing_status": "classified",
                "category": "job_offer_to_me",
                "proposed_action": "draft_reply",
                "action_execution": {"status": "dry_run", "message": "Action not executed."},
                "star_execution": {"status": "dry_run", "message": "Conversation would be starred."},
            }
        ]
        report = manager._build_final_report(
            results,
            dry_run=True,
            execute_archives=False,
            execute_replies=False,
        )

        manager._persist_results(
            results,
            limit=1,
            dry_run=True,
            execute_archives=False,
            execute_replies=False,
            report=report,
        )

        saved = Path(manager.output_file).read_text(encoding="utf-8")
        assert "participant_name: Sean" in saved
        assert "participant_name: Karen" in saved
        assert "proposed_action: draft_reply" in saved


class TestActionExecution:
    @pytest.mark.asyncio
    async def test_execute_action_skips_when_reply_execution_disabled(self, manager):
        result = await manager._execute_action(
            "draft_reply",
            "Thanks for reaching out.",
            dry_run=False,
            execute_archives=False,
            execute_replies=False,
        )

        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_execute_action_sends_reply_when_enabled(self, manager):
        with patch.object(manager, "_send_reply", new_callable=AsyncMock, return_value=True):
            result = await manager._execute_action(
                "draft_reply",
                "Thanks for reaching out.",
                dry_run=False,
                execute_archives=False,
                execute_replies=True,
            )

        assert result["status"] == "executed"

    @pytest.mark.asyncio
    async def test_execute_action_runs_archive_when_enabled(self, manager):
        with patch.object(manager, "_archive_or_flag_spam", new_callable=AsyncMock, return_value=True):
            result = await manager._execute_action(
                "flag_spam_and_archive",
                "",
                dry_run=False,
                execute_archives=True,
                execute_replies=False,
            )

        assert result["status"] == "executed"

    @pytest.mark.asyncio
    async def test_send_reply_returns_false_for_empty_draft(self, manager):
        assert not await manager._send_reply("   ")

    @pytest.mark.asyncio
    async def test_ensure_starred_skips_non_recruiter_conversation(self, manager):
        result = await manager._ensure_starred(should_star=False, dry_run=True)

        assert result["status"] == "not_applicable"

    @pytest.mark.asyncio
    async def test_ensure_starred_returns_already_starred(self, manager):
        star_button = AsyncMock()
        star_button.count.return_value = 1
        star_button.get_attribute.side_effect = ["Unstar conversation", "", "", ""]
        star_button.first = star_button
        manager.page.locator = MagicMock(return_value=star_button)

        result = await manager._ensure_starred(should_star=True, dry_run=False)

        assert result["status"] == "already_starred"

    @pytest.mark.asyncio
    async def test_ensure_starred_dry_run_when_not_starred(self, manager):
        star_button = AsyncMock()
        star_button.count.return_value = 1
        star_button.get_attribute.side_effect = ["Star conversation", "", "false", "", "msg-thread__star-icon"]
        star_button.first = star_button
        manager.page.locator = MagicMock(return_value=star_button)

        result = await manager._ensure_starred(should_star=True, dry_run=True)

        assert result["status"] == "dry_run"

    @pytest.mark.asyncio
    async def test_click_menu_option_prefers_exact_archive_dropdown_item(self, manager):
        archive_locator = AsyncMock()
        archive_locator.count.return_value = 1
        archive_locator.click = AsyncMock()
        archive_locator.scroll_into_view_if_needed = AsyncMock()
        archive_locator.first = archive_locator

        empty_locator = AsyncMock()
        empty_locator.count.return_value = 0
        empty_locator.first = empty_locator

        def locator_side_effect(selector):
            if "message-toolbar-dropdown-toggle-archive" in selector:
                return archive_locator
            return empty_locator

        manager.page.locator = MagicMock(side_effect=locator_side_effect)
        manager.page.get_by_role.return_value = empty_locator

        clicked = await manager._click_menu_option(["Archive"])

        assert clicked is True
        archive_locator.click.assert_called_once()
        manager.page.locator.assert_called_with(
            f"{manager.OPEN_THREAD_ACTIONS_DROPDOWN_SELECTOR} [data-view-name='message-toolbar-dropdown-toggle-archive']"
        )

    @pytest.mark.asyncio
    async def test_archive_or_flag_spam_archives_even_when_report_flow_is_missing(self, manager):
        with (
            patch.object(manager, "_open_thread_actions_menu", new_callable=AsyncMock, return_value=True),
            patch.object(
                manager,
                "_click_menu_option",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            archived = await manager._archive_or_flag_spam(flag_spam=True)

        assert archived is True

    @pytest.mark.asyncio
    async def test_flag_spam_and_archive_only_clicks_archive(self, manager):
        with (
            patch.object(manager, "_open_thread_actions_menu", new_callable=AsyncMock, return_value=True),
            patch.object(manager, "_click_menu_option", new_callable=AsyncMock, return_value=True) as click_menu,
        ):
            archived = await manager._archive_or_flag_spam(flag_spam=True)

        assert archived is True
        click_menu.assert_called_once_with(["Archive"])

    @pytest.mark.asyncio
    async def test_ensure_label_as_jobs_skips_non_recruiter_conversation(self, manager):
        result = await manager._ensure_label_as_jobs(should_label=False, dry_run=True)

        assert result["status"] == "not_applicable"

    @pytest.mark.asyncio
    async def test_ensure_label_as_jobs_dry_run_when_enabled(self, manager):
        remove_label_option = AsyncMock()
        remove_label_option.count.return_value = 0
        remove_label_option.first = remove_label_option
        manager.page.locator = MagicMock(return_value=remove_label_option)

        with patch.object(manager, "_open_thread_actions_menu", new_callable=AsyncMock, return_value=True):
            result = await manager._ensure_label_as_jobs(should_label=True, dry_run=True)

        assert result["status"] == "dry_run"

    @pytest.mark.asyncio
    async def test_ensure_label_as_jobs_executes_label_click(self, manager):
        remove_label_option = AsyncMock()
        remove_label_option.count.return_value = 0
        remove_label_option.first = remove_label_option
        manager.page.locator = MagicMock(return_value=remove_label_option)

        with (
            patch.object(manager, "_open_thread_actions_menu", new_callable=AsyncMock, return_value=True),
            patch.object(manager, "_click_menu_option", new_callable=AsyncMock, return_value=True) as click_menu,
        ):
            result = await manager._ensure_label_as_jobs(should_label=True, dry_run=False)

        assert result["status"] == "executed"
        click_menu.assert_called_once_with(["Label as Jobs"])

    @pytest.mark.asyncio
    async def test_ensure_label_as_jobs_skips_when_already_labeled(self, manager):
        remove_label_option = AsyncMock()
        remove_label_option.count.return_value = 1
        remove_label_option.first = remove_label_option
        manager.page.locator = MagicMock(return_value=remove_label_option)

        with patch.object(manager, "_open_thread_actions_menu", new_callable=AsyncMock, return_value=True):
            result = await manager._ensure_label_as_jobs(should_label=True, dry_run=False)

        assert result["status"] == "already_labeled"

    @pytest.mark.asyncio
    async def test_open_thread_actions_menu_waits_for_dropdown(self, manager):
        trigger = AsyncMock()
        trigger.count.return_value = 1
        trigger.scroll_into_view_if_needed = AsyncMock()
        trigger.wait_for = AsyncMock()
        trigger.click = AsyncMock()
        trigger.get_attribute = AsyncMock(return_value="true")
        trigger.first = trigger
        manager.page.locator = MagicMock(return_value=trigger)

        opened = await manager._open_thread_actions_menu()

        assert opened is True

    @pytest.mark.asyncio
    async def test_wait_for_actions_dropdown_accepts_aria_expanded(self, manager):
        trigger = AsyncMock()
        trigger.get_attribute = AsyncMock(return_value="true")

        opened = await manager._wait_for_actions_dropdown(trigger, timeout_ms=200)

        assert opened is True

    @pytest.mark.asyncio
    async def test_apply_unread_filter_clicks_when_not_active(self, manager):
        unread_button = AsyncMock()
        unread_button.count.return_value = 1
        unread_button.get_attribute = AsyncMock(return_value="false")
        unread_button.click = AsyncMock()
        unread_button.first = unread_button
        manager.page.locator = MagicMock(return_value=unread_button)
        manager.page.wait_for_selector = AsyncMock()

        await manager._apply_unread_filter()

        unread_button.click.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_unread_filter_skips_when_already_active(self, manager):
        unread_button = AsyncMock()
        unread_button.count.return_value = 1
        unread_button.get_attribute = AsyncMock(return_value="true")
        unread_button.click = AsyncMock()
        unread_button.first = unread_button
        manager.page.locator = MagicMock(return_value=unread_button)

        await manager._apply_unread_filter()

        unread_button.click.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_unread_filter_skips_when_button_missing(self, manager):
        unread_button = AsyncMock()
        unread_button.count.return_value = 0
        unread_button.first = unread_button
        manager.page.locator = MagicMock(return_value=unread_button)

        await manager._apply_unread_filter()

        unread_button.click.assert_not_called()
