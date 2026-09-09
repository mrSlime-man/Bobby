import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from playwright.sync_api import Page

from config.constants import OUTPUT_DIR_LINKEDIN
from config.logger_config import logger
from src.dashboard.runtime import emit_event
from src.utils.utils import async_pause, load_yaml_file, save_yaml_file


class LinkedInMessagesManager:
    MESSAGING_URL = "https://www.linkedin.com/messaging/"
    THREAD_ACTION_TRIGGER = ".msg-thread .msg-thread-actions__control"
    THREAD_ACTIONS_DROPDOWN_SELECTOR = ".msg-thread-actions__dropdown-options, .msg-thread-actions__dropdown-options--inbox-shortcuts"
    OPEN_THREAD_ACTIONS_DROPDOWN_SELECTOR = (
        ":is(.msg-thread-actions__dropdown-options, .msg-thread-actions__dropdown-options--inbox-shortcuts)"
        ".artdeco-dropdown__content--is-open[aria-hidden='false']"
    )
    COMPOSER_SELECTOR = ".msg-form__contenteditable[contenteditable='true']"
    THREAD_STAR_BUTTON_SELECTOR = ".msg-thread__star-icon"
    UNREAD_FILTER_SELECTOR = "[data-test-messaging-inbox-filters__filter-pill='UNREAD']"
    CONVERSATION_ITEM_SELECTOR = (
        "li.msg-conversations-container__convo-item:not(.msg-conversation-card--occluded)"
    )

    def __init__(
        self,
        page: Page,
        llm_answerer: Any,
        resume_structured: Dict[str, Any] | None = None,
        output_file: Path | None = None,
        message_preferences: Dict[str, Any] | None = None,
    ):
        self.page = page
        self.llm_answerer = llm_answerer
        self.resume_structured = resume_structured or {}
        self.output_file = output_file or Path(OUTPUT_DIR_LINKEDIN) / "messages_dry_run.yaml"
        self.message_preferences = message_preferences or {}
        self.pause_checker = None
        personal_info = self.resume_structured.get("personal_information", {})
        first_name = self._normalize_text(personal_info.get("first_name", ""))
        last_name = self._normalize_text(personal_info.get("last_name", ""))
        self.current_user_name = self._normalize_text(" ".join([first_name, last_name]).strip())

    def set_pause_checker(self, pause_checker) -> None:
        self.pause_checker = pause_checker

    async def run(
        self,
        limit: int = 25,
        dry_run: bool = True,
        execute_archives: bool = False,
        execute_replies: bool = False,
        unread_only: bool = False,
    ) -> List[Dict[str, Any]]:
        logger.info("Starting LinkedIn messages workflow")
        await self._open_inbox()
        if unread_only:
            await self._apply_unread_filter()
        summaries = await self._load_conversation_summaries(self._conversation_summary_target(limit))
        results = []
        processed = 0

        for summary in summaries:
            if self.pause_checker:
                await self.pause_checker()

            if self._is_preview_from_current_user(summary.get("snippet", "")):
                result = self._build_skipped_result(
                    summary,
                    "Preview shows the current user already replied.",
                )
                self._record_result(result, results, dry_run, execute_archives, execute_replies)
                continue

            if not await self._open_conversation(summary.get("participant_name", "")):
                result = self._build_skipped_result(summary, "Failed to open conversation.")
                self._record_result(result, results, dry_run, execute_archives, execute_replies)
                continue

            conversation = await self._extract_active_conversation(summary)
            if self._is_last_message_from_current_user(conversation.get("messages", [])):
                result = self._build_skipped_result(
                    conversation,
                    "Latest message in the thread is from the current user.",
                )
                self._record_result(result, results, dry_run, execute_archives, execute_replies)
                continue

            classification = self.llm_answerer.classify_linkedin_message(conversation)
            classification = self._normalize_classification(conversation, classification)
            draft_reply = ""
            if classification.get("proposed_action") == "draft_reply":
                draft_reply = self.llm_answerer.draft_linkedin_message_reply(
                    conversation,
                    classification,
                )

            is_recruiter = classification.get("category") == "job_offer_to_me"
            star_execution = await self._ensure_starred(
                should_star=is_recruiter and self.message_preferences.get("auto_star_job_offers", True),
                dry_run=dry_run,
            )
            label_as_jobs_execution = await self._ensure_label_as_jobs(
                should_label=is_recruiter and self.message_preferences.get("auto_label_job_offers", True),
                dry_run=dry_run,
            )

            execution = await self._execute_action(
                classification.get("proposed_action", "skip"),
                draft_reply,
                dry_run=dry_run,
                execute_archives=execute_archives,
                execute_replies=execute_replies,
            )

            result = self._build_classified_result(
                conversation,
                classification,
                draft_reply,
                execution,
                star_execution,
                label_as_jobs_execution,
            )
            self._record_result(result, results, dry_run, execute_archives, execute_replies)
            emit_event(
                "linkedin_message_processed",
                f"Processed message from {conversation.get('participant_name') or 'unknown sender'}",
                category=classification.get("category"),
                proposed_action=classification.get("proposed_action"),
            )

            processed += 1
            if processed >= limit:
                break

        if processed < limit:
            logger.info(
                f"Reached end of conversation list with {processed} conversations processed "
                f"(target was {limit})."
            )

        report = self._build_final_report(results, dry_run, execute_archives, execute_replies)
        self._log_final_report(report)
        logger.info("LinkedIn messages workflow completed")
        return results

    async def run_dry_run(self, limit: int = 25) -> List[Dict[str, Any]]:
        return await self.run(limit=limit, dry_run=True)

    async def _open_inbox(self) -> None:
        await self.page.goto(self.MESSAGING_URL, wait_until="domcontentloaded")
        await self.page.wait_for_selector(
            ".msg-conversations-container__conversations-list",
            timeout=15000,
        )
        await async_pause(2, 3)

    async def _apply_unread_filter(self) -> None:
        unread_button = self.page.locator(self.UNREAD_FILTER_SELECTOR).first
        if await unread_button.count() == 0:
            logger.warning("Unread filter button not found, continuing without filter.")
            return
        pressed = (await unread_button.get_attribute("aria-pressed") or "").strip().lower()
        if pressed == "true":
            logger.info("Unread filter already active.")
            return
        await unread_button.click(timeout=5000)
        await async_pause(1, 1.5)
        await self.page.wait_for_selector(
            ".msg-conversations-container__conversations-list",
            timeout=10000,
        )
        logger.info("Unread filter applied.")

    async def _load_conversation_summaries(self, limit: int) -> List[Dict[str, Any]]:
        summaries = []
        seen_signatures = set()
        stagnant_attempts = 0

        while len(summaries) < limit and stagnant_attempts < 3:
            current = await self._extract_visible_conversation_summaries()
            previous_count = len(summaries)
            for summary in current:
                signature = self._conversation_signature(summary)
                if signature in seen_signatures:
                    continue
                seen_signatures.add(signature)
                summaries.append(summary)
                if len(summaries) >= limit:
                    break

            if len(summaries) == previous_count:
                stagnant_attempts += 1
            else:
                stagnant_attempts = 0

            if len(summaries) >= limit:
                break

            if await self._click_load_more_conversations():
                continue

            await self._scroll_conversation_list()

        return summaries[:limit]

    async def _extract_visible_conversation_summaries(self) -> List[Dict[str, Any]]:
        items = self.page.locator(self.CONVERSATION_ITEM_SELECTOR)
        count = await items.count()
        summaries = []

        for index in range(count):
            item = items.nth(index)
            name = await self._get_locator_text(
                item.locator(".msg-conversation-listitem__participant-names").first
            )
            if not name:
                continue

            snippet = await self._get_locator_text(
                item.locator(".msg-conversation-card__message-snippet").first
            )
            timestamp = await self._get_locator_text(
                item.locator(".msg-conversation-listitem__time-stamp").first
            )
            summaries.append(
                {
                    "dom_index": index,
                    "participant_name": name,
                    "timestamp": timestamp,
                    "snippet": snippet,
                }
            )

        return summaries

    async def _click_load_more_conversations(self) -> bool:
        button = self.page.get_by_role("button", name="Load more conversations")
        if await button.count() == 0:
            return False

        try:
            await button.first.click(timeout=5000)
            await async_pause(2, 3)
            return True
        except Exception as exc:
            logger.debug(f"Failed to click 'Load more conversations': {exc}")
            return False

    async def _scroll_conversation_list(self) -> None:
        container = self.page.locator(".msg-conversations-container--inbox-shortcuts").first
        if await container.count() == 0:
            return
        try:
            await container.evaluate(
                """
                (element) => {
                    const increment = Math.max(Math.floor(element.clientHeight * 0.75), 200);
                    element.scrollTop = Math.min(element.scrollTop + increment, element.scrollHeight);
                }
                """
            )
        except Exception as exc:
            logger.debug(f"Failed to scroll conversations list: {exc}")
        await async_pause(1, 2)

    async def _open_conversation(self, participant_name: str) -> bool:
        normalized_target = self._normalize_text(participant_name)
        all_items = self.page.locator("li.msg-conversations-container__convo-item")

        for index in range(await all_items.count()):
            item = all_items.nth(index)
            name_element = item.locator(".msg-conversation-listitem__participant-names").first
            item_name = self._normalize_text(await self._get_locator_text(name_element))
            if not item_name or item_name != normalized_target:
                continue

            link = item.locator(".msg-conversation-listitem__link").first
            try:
                await link.scroll_into_view_if_needed(timeout=3000)
                await link.wait_for(state="visible", timeout=5000)
                try:
                    await link.click(timeout=5000)
                except Exception:
                    await link.click(timeout=5000, force=True)
                await self.page.wait_for_selector(".msg-thread", timeout=10000)
                await async_pause(1, 2)
                return True
            except Exception as exc:
                logger.warning(f"Failed to open conversation with {participant_name}: {exc}")
                return False

        logger.warning(f"Conversation item not found for '{participant_name}' in list.")
        return False

    async def _extract_active_conversation(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        participant_name = self._normalize_text(
            await self.page.locator(".msg-thread .msg-entity-lockup__entity-title").first.text_content()
            or summary.get("participant_name", "")
        )
        participant_headline = self._normalize_text(
            await self.page.locator(".msg-thread .msg-entity-lockup__entity-info").first.text_content()
            or ""
        )
        messages = await self._extract_thread_messages(participant_name)
        last_sender = messages[-1]["sender"] if messages else ""

        return {
            "participant_name": participant_name,
            "participant_headline": participant_headline,
            "timestamp": summary.get("timestamp", ""),
            "snippet": summary.get("snippet", ""),
            "last_sender": last_sender,
            "messages": messages,
        }

    async def _extract_thread_messages(self, participant_name: str) -> List[Dict[str, Any]]:
        event_items = self.page.locator(".msg-thread .msg-s-event-listitem[data-event-urn]")
        count = await event_items.count()
        messages = []

        for index in range(count):
            item = event_items.nth(index)

            try:
                raw_body = await item.locator(".msg-s-event-listitem__body").first.text_content(timeout=3000)
            except Exception:
                continue
            body = self._normalize_text(raw_body or "")
            if not body:
                continue

            classes = await item.get_attribute("class") or ""
            sender = self._sender_from_classes(classes, participant_name)

            sender_name = ""
            try:
                sender_name = self._normalize_text(
                    await item.locator(".msg-s-message-group__name").first.text_content(timeout=2000) or ""
                )
            except Exception:
                pass

            if sender == "other" and sender_name:
                sender = sender_name
            elif sender == "self" and self.current_user_name:
                sender = self.current_user_name
            elif sender == "self":
                sender = "You"
            elif sender == "other":
                sender = participant_name

            timestamp = ""
            try:
                timestamp = self._normalize_text(
                    await item.locator(".msg-s-message-group__timestamp").first.text_content(timeout=2000) or ""
                )
            except Exception:
                pass

            messages.append(
                {
                    "sender": sender,
                    "timestamp": timestamp,
                    "body": body,
                }
            )

        return messages

    def _persist_results(
        self,
        results: List[Dict[str, Any]],
        limit: int,
        dry_run: bool,
        execute_archives: bool,
        execute_replies: bool,
        report: Dict[str, Any],
    ) -> None:
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        existing_entries = self._load_existing_results()
        results_by_key = {
            self._conversation_signature(result): self._with_persistence_metadata(
                result,
                dry_run,
                execute_archives,
                execute_replies,
            )
            for result in results
        }

        merged_entries = []
        seen_keys = set()

        for entry in existing_entries:
            key = self._conversation_signature(entry)
            if key in results_by_key:
                merged_entries.append(results_by_key[key])
                seen_keys.add(key)
            else:
                merged_entries.append(entry)
                seen_keys.add(key)

        for key, result in results_by_key.items():
            if key not in seen_keys:
                merged_entries.append(result)

        save_yaml_file(self.output_file, merged_entries, sort_keys=False)

    def _record_result(
        self,
        result: Dict[str, Any],
        results: List[Dict[str, Any]],
        dry_run: bool,
        execute_archives: bool,
        execute_replies: bool,
    ) -> None:
        sanitized_result = self._sanitize_result(result)
        results.append(sanitized_result)
        self._persist_results(
            [sanitized_result],
            limit=1,
            dry_run=dry_run,
            execute_archives=execute_archives,
            execute_replies=execute_replies,
            report={},
        )

    @staticmethod
    def _sanitize_result(result: Dict[str, Any]) -> Dict[str, Any]:
        sanitized = dict(result)
        sanitized.pop("dom_index", None)
        return sanitized

    def _build_final_report(
        self,
        results: List[Dict[str, Any]],
        dry_run: bool,
        execute_archives: bool,
        execute_replies: bool,
    ) -> Dict[str, Any]:
        category_counts: Dict[str, int] = {}
        action_counts: Dict[str, int] = {}
        execution_counts: Dict[str, int] = {}
        important_conversations = []

        for result in results:
            category = result.get("category") or "uncategorized"
            proposed_action = result.get("proposed_action") or "none"
            execution_status = (result.get("action_execution") or {}).get("status") or "none"

            category_counts[category] = category_counts.get(category, 0) + 1
            action_counts[proposed_action] = action_counts.get(proposed_action, 0) + 1
            execution_counts[execution_status] = execution_counts.get(execution_status, 0) + 1

            if proposed_action == "draft_reply":
                important_conversations.append(
                    {
                "participant_name": result.get("participant_name", ""),
                "category": category,
                "proposed_action": proposed_action,
                "star_status": (result.get("star_execution") or {}).get("status"),
            }
        )

        return {
            "mode": "dry_run" if dry_run else "execute",
            "execution_flags": {
                "execute_archives": execute_archives,
                "execute_replies": execute_replies,
            },
            "total_conversations": len(results),
            "category_counts": category_counts,
            "action_counts": action_counts,
            "execution_counts": execution_counts,
            "important_conversations": important_conversations,
        }

    def _load_existing_results(self) -> List[Dict[str, Any]]:
        if not self.output_file.exists():
            return []

        try:
            existing = load_yaml_file(self.output_file)
        except Exception as exc:
            logger.warning(f"Failed to load existing messages ledger: {exc}")
            return []

        if isinstance(existing, list):
            return [entry for entry in existing if isinstance(entry, dict)]

        if isinstance(existing, dict) and isinstance(existing.get("conversations"), list):
            return [entry for entry in existing["conversations"] if isinstance(entry, dict)]

        return []

    @staticmethod
    def _with_persistence_metadata(
        result: Dict[str, Any],
        dry_run: bool,
        execute_archives: bool,
        execute_replies: bool,
    ) -> Dict[str, Any]:
        enriched = dict(result)
        enriched["updated_at"] = datetime.now().isoformat(timespec="seconds")
        enriched["last_run_mode"] = "dry_run" if dry_run else "execute"
        enriched["execute_archives"] = execute_archives
        enriched["execute_replies"] = execute_replies
        return enriched

    def _log_final_report(self, report: Dict[str, Any]) -> None:
        logger.info("LinkedIn messages final report")
        logger.info(f"Mode: {report.get('mode')}")
        logger.info(f"Total conversations: {report.get('total_conversations')}")
        logger.info(f"Category counts: {report.get('category_counts')}")
        logger.info(f"Action counts: {report.get('action_counts')}")
        logger.info(f"Execution counts: {report.get('execution_counts')}")

        important = report.get("important_conversations") or []
        if important:
            logger.info(
                "Important conversations requiring attention: %s",
                ", ".join(item.get("participant_name", "") for item in important),
            )

    def _build_skipped_result(self, summary: Dict[str, Any], reason: str) -> Dict[str, Any]:
        result = dict(summary)
        result.update(
            {
                "processing_status": "skipped",
                "skip_reason": reason,
                "proposed_action": "skip",
                "draft_reply": "",
            }
        )
        return result

    def _build_classified_result(
        self,
        conversation: Dict[str, Any],
        classification: Dict[str, Any],
        draft_reply: str,
        execution: Dict[str, Any],
        star_execution: Dict[str, Any],
        label_as_jobs_execution: Dict[str, Any],
    ) -> Dict[str, Any]:
        result = dict(conversation)
        result.update(
            {
                "processing_status": "classified",
                "category": classification.get("category"),
                "confidence": classification.get("confidence"),
                "reasoning": classification.get("reasoning"),
                "proposed_action": classification.get("proposed_action"),
                "draft_reply": draft_reply,
                "action_execution": execution,
                "star_execution": star_execution,
                "label_as_jobs_execution": label_as_jobs_execution,
            }
        )
        return result

    def _normalize_classification(
        self,
        conversation: Dict[str, Any],
        classification: Dict[str, Any],
    ) -> Dict[str, Any]:
        normalized = dict(classification)
        if (
            self.message_preferences.get("skip_drafting_for_personal_messages", True)
            and normalized.get("category") == "personal_message"
            and normalized.get("proposed_action") == "draft_reply"
        ):
            participant_name = conversation.get("participant_name") or "the sender"
            normalized["proposed_action"] = "skip"
            normalized["reasoning"] = (
                f"{normalized.get('reasoning', '').strip()} "
                f"No reply drafted automatically because this is a personal message from {participant_name}."
            ).strip()
        return normalized

    async def _ensure_starred(self, should_star: bool, dry_run: bool) -> Dict[str, Any]:
        if not should_star:
            return {"status": "not_applicable", "message": "Conversation does not require starring."}

        star_button = self.page.locator(self.THREAD_STAR_BUTTON_SELECTOR).first
        if await star_button.count() == 0:
            return {"status": "failed", "message": "Star button not found."}

        if await self._is_conversation_starred(star_button):
            return {"status": "already_starred", "message": "Conversation already starred."}

        if dry_run:
            return {"status": "dry_run", "message": "Conversation would be starred."}

        try:
            await star_button.click(timeout=5000)
            await async_pause(0.5, 1)
            return {"status": "executed", "message": "Conversation starred."}
        except Exception as exc:
            logger.warning(f"Failed to star conversation: {exc}")
            return {"status": "failed", "message": "Failed to star conversation."}

    async def _is_conversation_starred(self, star_button: Any) -> bool:
        for attribute in ["aria-label", "title", "aria-pressed", "data-test-state"]:
            value = (await star_button.get_attribute(attribute) or "").strip().lower()
            if not value:
                continue
            if "unstar" in value or "remove star" in value:
                return True
            if attribute == "aria-pressed" and value == "true":
                return True
            if attribute == "data-test-state" and value == "starred":
                return True
        class_value = (await star_button.get_attribute("class") or "").lower()
        if "star-icon--starred" in class_value:
            return True
        return False

    async def _ensure_label_as_jobs(self, should_label: bool, dry_run: bool) -> Dict[str, Any]:
        if not should_label:
            return {"status": "not_applicable", "message": "Conversation does not require label as jobs."}

        if not await self._open_thread_actions_menu():
            return {"status": "failed", "message": "Failed to open thread actions menu."}

        remove_label_option = self.page.locator(
            f"{self.OPEN_THREAD_ACTIONS_DROPDOWN_SELECTOR} [role='button']",
            has=self.page.locator("text=Remove Jobs label"),
        ).first
        if await remove_label_option.count() > 0:
            return {"status": "already_labeled", "message": "Conversation already labeled as jobs."}

        if dry_run:
            return {"status": "dry_run", "message": "Conversation would be labeled as jobs."}

        label_clicked = await self._click_menu_option(["Label as Jobs"])
        if not label_clicked:
            return {"status": "failed", "message": "Label as Jobs option not found or click failed."}

        await async_pause(0.5, 1)
        return {"status": "executed", "message": "Conversation labeled as jobs."}

    async def _execute_action(
        self,
        proposed_action: str,
        draft_reply: str,
        *,
        dry_run: bool,
        execute_archives: bool,
        execute_replies: bool,
    ) -> Dict[str, Any]:
        if dry_run:
            return {"status": "dry_run", "message": "Action not executed."}

        if proposed_action == "draft_reply":
            if not execute_replies:
                return {"status": "skipped", "message": "Reply execution disabled."}
            success = await self._send_reply(draft_reply)
            return {
                "status": "executed" if success else "failed",
                "message": "Reply sent." if success else "Failed to send reply.",
            }

        if proposed_action in {"archive", "flag_spam_and_archive"}:
            if not execute_archives:
                return {"status": "skipped", "message": "Archive execution disabled."}
            success = await self._archive_or_flag_spam(proposed_action == "flag_spam_and_archive")
            return {
                "status": "executed" if success else "failed",
                "message": (
                    "Conversation archived." if proposed_action == "archive" else "Conversation flagged and archived."
                )
                if success
                else "Failed to execute conversation action.",
            }

        return {"status": "skipped", "message": "No executable action for this classification."}

    async def _archive_or_flag_spam(self, flag_spam: bool) -> bool:
        if not await self._open_thread_actions_menu():
            return False

        archive_clicked = await self._click_menu_option(["Archive"])
        if archive_clicked:
            await async_pause(1, 2)
        return archive_clicked

    async def _send_reply(self, draft_reply: str) -> bool:
        if not draft_reply.strip():
            return False

        composer = self.page.locator(self.COMPOSER_SELECTOR).first
        if await composer.count() == 0:
            return False

        try:
            await composer.click(timeout=5000)
            await composer.fill("")
        except Exception:
            try:
                await composer.click(timeout=5000)
                await composer.press("Control+A")
                await composer.press("Backspace")
            except Exception as exc:
                logger.warning(f"Failed to reset reply composer: {exc}")
                return False

        try:
            await composer.type(draft_reply, delay=10)
            await async_pause(1, 1.5)
            send_button = self.page.locator("button.msg-form__send-button").first
            await send_button.wait_for(state="visible", timeout=5000)
            await send_button.click(timeout=5000)
            await async_pause(1, 2)
            return True
        except Exception as exc:
            logger.warning(f"Failed to send reply: {exc}")
            return False

    async def _open_thread_actions_menu(self) -> bool:
        trigger = self.page.locator(self.THREAD_ACTION_TRIGGER).first
        if await trigger.count() == 0:
            return False
        try:
            await trigger.scroll_into_view_if_needed(timeout=3000)
            await trigger.wait_for(state="visible", timeout=5000)
            try:
                await trigger.click(timeout=5000)
            except Exception:
                await trigger.click(timeout=5000, force=True)
            if not await self._wait_for_actions_dropdown(trigger):
                return False
            await async_pause(1, 1.5)
            return True
        except Exception as exc:
            logger.warning(f"Failed to open thread actions menu: {exc}")
            return False

    async def _wait_for_actions_dropdown(self, trigger: Any, timeout_ms: int = 6000) -> bool:
        deadline = datetime.now().timestamp() + (timeout_ms / 1000)
        while datetime.now().timestamp() < deadline:
            try:
                expanded = (await trigger.get_attribute("aria-expanded") or "").strip().lower()
                if expanded == "true":
                    return True
            except Exception:
                pass

            try:
                dropdown = self.page.locator(self.OPEN_THREAD_ACTIONS_DROPDOWN_SELECTOR)
                if await dropdown.count() > 0:
                    return True
            except Exception:
                pass

            await async_pause(0.2, 0.3)

        logger.warning("Timed out waiting for thread actions dropdown to open")
        return False

    async def _click_menu_option(self, labels: List[str]) -> bool:
        for label in labels:
            if label == "Archive":
                archive_option = self.page.locator(
                    f"{self.OPEN_THREAD_ACTIONS_DROPDOWN_SELECTOR} [data-view-name='message-toolbar-dropdown-toggle-archive']"
                ).first
                if await archive_option.count() > 0:
                    try:
                        await archive_option.scroll_into_view_if_needed(timeout=3000)
                        await archive_option.click(timeout=5000)
                        return True
                    except Exception as exc:
                        logger.debug(f"Failed clicking archive menu option via data-view-name: {exc}")

            option = self.page.get_by_role("button", name=label, exact=True)
            if await option.count() > 0:
                try:
                    await option.first.click(timeout=5000)
                    return True
                except Exception as exc:
                    logger.debug(f"Failed clicking menu button '{label}': {exc}")

            dropdown_options = self.page.locator(f"{self.OPEN_THREAD_ACTIONS_DROPDOWN_SELECTOR} [role='button']")
            option_count = await dropdown_options.count()
            for index in range(option_count):
                candidate = dropdown_options.nth(index)
                candidate_text = await self._get_locator_text(candidate)
                if candidate_text != label:
                    continue
                try:
                    await candidate.scroll_into_view_if_needed(timeout=3000)
                    await candidate.click(timeout=5000)
                    return True
                except Exception as exc:
                    logger.debug(f"Failed clicking exact dropdown text '{label}': {exc}")

        return False

    @staticmethod
    def _sender_from_classes(classes: str, participant_name: str) -> str:
        normalized_classes = classes or ""
        if "msg-s-event-listitem--self" in normalized_classes:
            return "self"
        if "msg-s-event-listitem--other" in normalized_classes:
            return participant_name or "other"
        return "other"

    @staticmethod
    def _normalize_text(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

    async def _get_locator_text(self, locator: Any) -> str:
        try:
            if await locator.count() == 0:
                return ""
            return self._normalize_text(await locator.text_content(timeout=1500) or "")
        except Exception as exc:
            logger.debug(f"Failed to extract locator text: {exc}")
            return ""

    @staticmethod
    def _conversation_signature(summary: Dict[str, Any]) -> str:
        return LinkedInMessagesManager._normalize_text(summary.get("participant_name", ""))

    @staticmethod
    def _conversation_summary_target(limit: int) -> int:
        safe_limit = max(int(limit), 1)
        buffer = min(max(safe_limit // 3, 2), 5)
        return safe_limit + buffer

    @staticmethod
    def _is_preview_from_current_user(snippet: str) -> bool:
        normalized = LinkedInMessagesManager._normalize_text(snippet)
        return normalized.startswith("You:") or " You:" in normalized

    def _is_last_message_from_current_user(self, messages: List[Dict[str, Any]]) -> bool:
        if not messages:
            return False

        last_sender = self._normalize_text(messages[-1].get("sender", ""))
        if last_sender in {"You", self.current_user_name}:
            return True

        if self.current_user_name and self.current_user_name in last_sender:
            return True

        return False
