import argparse
import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Union

import dotenv
from telethon import TelegramClient

from config.logger_config import logger
from telegram import Bot
from telegram.error import TelegramError
from src.utils.redaction import redact_text, redact_urls
from src.utils.run_context import get_run_id
from src.utils.suitability_reporting import safe_public_reason


TELEGRAM_MIRROR = Path(os.getenv("BOBBY_LOG_DIR", str(Path.home() / "Logs"))) / "telegram-outgoing.log"
TELEGRAM_INCOMING_MIRROR = (
    Path(os.getenv("BOBBY_LOG_DIR", str(Path.home() / "Logs"))) / "telegram-incoming.log"
)


def redact_sensitive_text(value: str) -> str:
    """Remove common credentials and one-time secrets from mirrored messages."""
    return redact_urls(redact_text(value))


def mirror_telegram_message(
    *, message_type: str, text: str, topic: str | None, success: bool,
    error: str = "", incoming: bool = False,
) -> None:
    mirror_path = TELEGRAM_INCOMING_MIRROR if incoming else TELEGRAM_MIRROR
    mirror_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "run_id": get_run_id(),
        "message_type": message_type,
        "topic": topic,
        "text": redact_sensitive_text(text),
        "success": success,
        "error_class": error,
    }
    try:
        with mirror_path.open("a", encoding="utf-8") as mirror:
            mirror.write(json.dumps(entry, ensure_ascii=True) + "\n")
    except OSError as exc:
        logger.warning(f"Could not write Telegram mirror: {type(exc).__name__}")


def normalize_telegram_topic_id(value: str | None) -> str | None:
    """Return a Bot API message_thread_id from a numeric value or t.me/c topic URL."""
    if not value:
        return None
    value = value.strip().strip('"').strip("'")
    if not value:
        return None
    if value.isdigit():
        return value
    topic_url_match = re.search(r"t\.me/(?:c/\d+|[^/]+)/(\d+)", value)
    if topic_url_match:
        return topic_url_match.group(1)
    return value


async def send_captcha(bot_token, chat_id, topic_id, img_path, message):
    """Send captcha message with PTB"""
    bot = Bot(token=bot_token)
    await bot.send_photo(
        chat_id=chat_id, message_thread_id=topic_id, photo=open(img_path, "rb"), caption=message
    )


async def receive_messages(api_id: str, api_hash: str, chat_id: str, topic_id: str, message: str):
    """Receive messages with Telethon"""
    client = TelegramClient("my-client", api_id, api_hash)
    async with client:
        messages_ = await client.get_messages(chat_id, limit=10, reply_to=topic_id)
        for message_ in messages_:
            if message_.reply_to_msg_id:
                reply_msg_id = message_.reply_to_msg_id
                reply_message = await client.get_messages(
                    chat_id, ids=reply_msg_id, reply_to=topic_id
                )
                if reply_message and reply_message.text == message:
                    mirror_telegram_message(
                        message_type="reply",
                        text=message_.text or "",
                        topic=topic_id,
                        success=True,
                        incoming=True,
                    )
                    return message_.text


async def process_captcha(
    tg_token, tg_api_id, tg_api_hash, chat_id, topic_id, img_path, message, listen=False
) -> Union[str, None]:
    """Search for a captcha and if found, send it to the chat for solving"""
    if not listen:
        # Send a message using PTB
        await send_captcha(tg_token, chat_id, topic_id, img_path, message)
    else:
        # Receive messages using Telethon
        return await receive_messages(tg_api_id, tg_api_hash, chat_id, topic_id, message)


class TelegramReportSender:
    """
    Class for sending error messages through Telegram.
    If there is an error in the sending process, we wait and send again.
    """

    def __init__(self):
        secrets = dotenv.dotenv_values(".env")
        telegram_bot_token = secrets["tg_token"]
        self.bot = Bot(token=telegram_bot_token)
        self.chat_id = secrets["tg_chat_id"]
        self.report_topic_id = normalize_telegram_topic_id(secrets.get("tg_report_topic_id"))
        self.err_topic_id = normalize_telegram_topic_id(secrets.get("tg_err_topic_id"))
        self.message = ""
        self.run_id = get_run_id()

    async def send_telegram_report(
        self,
        login: str,
        resume: Dict[str, Any],
        success_applies_num: str,
        jobs_no_info: str,
        skill_stat: str,
        resume_recommendations: str,
        resume_component: Any,
    ) -> None:
        """
        Async version of send_telegram_report for proper async/await usage
        """
        # add client contacts
        email = resume["personal_information"].get("email", "")

        if login and "@" in login:
            header = f"Client email: {login}"
        else:
            email = resume_component.deanonymize_text(email)
            header = f"Client email: {email}"

        first_name = resume["personal_information"].get("first_name", "")
        first_name = resume_component.deanonymize_text(first_name)
        last_name = resume["personal_information"].get("last_name", "")
        last_name = resume_component.deanonymize_text(last_name)
        header += f"\nClient name: {first_name} {last_name}\n"

        message = header
        message += (
            f"Total number of vacancies to which the application responded: {success_applies_num}\n"
        )

        # add list of vacancies that couldn't be responded to
        if jobs_no_info:
            message += "Below we attach a list of vacancies to which the application could not respond for whatever reason:\n\n"
            jobs_no_info = self._format_jobs_no_info(jobs_no_info)
            message += jobs_no_info

        # add statistics on most in-demand vacancies
        if skill_stat:
            message += "\nBelow we attach statistics on the most in-demand skills in the vacancies you are interested in:\n\n"
            skill_stat = sorted(
                [(k, v) for k, v in skill_stat.items()], key=lambda x: x[1], reverse=True
            )[:20]
            for skill, stat in skill_stat:
                message += f"  {skill}: {stat}\n"

        # add resume improvement recommendations
        if resume_recommendations:
            message += "\nAlso we attach recommendations for improving your resume:\n\n"
            message += resume_recommendations

        self.message = message

        # Use proper async/await instead of asyncio.run()
        await self._send_chunked_messages(self.message, header)

    async def send_compact_run_report(
        self,
        applied: int,
        skipped: int,
        failed: int,
        viewed: int,
        duration_seconds: int,
        applied_jobs: list[dict] | None = None,
        cycle_stats: dict[str, int] | None = None,
    ) -> None:
        """Send a compact summary for the current search cycle."""

        duration_seconds = max(0, int(duration_seconds))
        minutes, seconds = divmod(duration_seconds, 60)
        hours, minutes = divmod(minutes, 60)

        if hours:
            duration_text = f"{hours}h {minutes}m"
        elif minutes:
            duration_text = f"{minutes}m {seconds}s"
        else:
            duration_text = f"{seconds}s"

        if cycle_stats:
            report_title = "⚠️ Cycle stopped (partial)" if cycle_stats.get("partial") else "✅ Cycle complete"
            message = (
                f"{report_title}\nRun: {self.run_id}\n\n"
                f"Found: {cycle_stats.get('found', 0)}\n"
                f"Encountered: {cycle_stats.get('encountered', 0)}\n"
                f"New: {cycle_stats.get('new', 0)}\n"
                f"Attempted: {cycle_stats.get('attempted', 0)} "
                f"(Easy Apply {cycle_stats.get('easy_apply_attempted', 0)}, "
                f"external {cycle_stats.get('external_attempted', 0)})\n"
                f"Easy Apply deferred by quota: {cycle_stats.get('easy_apply_deferred', 0)}\n"
                f"Submitted: {cycle_stats.get('submitted', 0)}\n"
                f"Unverified: {cycle_stats.get('unverified', 0)}\n"
                f"Technical failures: {cycle_stats.get('technical_failure', 0)}\n"
                f"Cancelled by shutdown: {cycle_stats.get('cancelled', 0)}\n"
                f"Needs human: {cycle_stats.get('needs_human', 0)}\n"
                f"Not eligible: {cycle_stats.get('not_eligible', 0)}\n"
                f"In progress: {cycle_stats.get('in_progress', 0)}\n"
                f"Skipped: {cycle_stats.get('skipped_total', skipped + cycle_stats.get('encountered', 0))}\n"
                f"Duration: {duration_text}"
            )
        else:
            message = (
                "✅ Cycle complete\n\n"
                f"New applications: {applied}\n"
                f"Skipped: {skipped}\n"
                f"Failed: {failed}\n"
                f"Viewed: {viewed}\n"
                f"Duration: {duration_text}"
            )

        applied_jobs = applied_jobs or []

        if applied_jobs:
            message += "\n\nApplied:"

            # Keep final report short because every application
            # already has its own live Telegram message.
            for item in applied_jobs[:15]:
                title = item.get("job_title") or "Unknown job"
                company = item.get("company_name") or "Unknown company"
                message += f"\n• {title} — {company}"

            if len(applied_jobs) > 15:
                remaining = len(applied_jobs) - 15
                message += f"\n• +{remaining} more"

        await self._send_chunked_messages(
            message,
            "✅ Cycle complete\n",
        )

    async def _send_chunked_messages(self, message, header):
        """
        Since Telegram has a limit on the length of a message of 4096 characters,
        we send the report in parts of 4096 characters
        """
        i = 0
        while i < len(message):
            if i == 0:
                part_message = message[:4096]
                i += 4096
            else:
                part_message = header + message[i : i + 4096 - len(header)]
                i += 4096 - len(header)
            try:
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    message_thread_id=self.report_topic_id,
                    text=part_message,
                )
                mirror_telegram_message(
                    message_type="report",
                    text=part_message,
                    topic=self.report_topic_id,
                    success=True,
                )
            except TelegramError as e:
                logger.error(f"Failed to send Telegram report:\n{e}")
                mirror_telegram_message(
                    message_type="report",
                    text=part_message,
                    topic=self.report_topic_id,
                    success=False,
                    error=type(e).__name__,
                )
            await asyncio.sleep(3)  # Use async sleep

    async def send_start_message(
        self,
        mode: str = "",
        locations: str = "",
    ) -> None:
        """Send a short notification when a search cycle starts."""
        message = "🚀 Job bot started"
        message += f"\nRun: {self.run_id}"

        details = []
        if mode:
            details.append(mode)
        if locations:
            details.append(locations)

        if details:
            message += "\n" + " | ".join(details)

        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                message_thread_id=self.report_topic_id,
                text=message,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            mirror_telegram_message(
                message_type="start", text=message, topic=self.report_topic_id,
                success=False, error=type(exc).__name__,
            )
            raise
        mirror_telegram_message(
            message_type="start", text=message, topic=self.report_topic_id, success=True
        )

    async def send_job_event(
        self,
        job_title: str,
        company_name: str,
        url: str,
        result: str,
        reason: str = "",
        final_status: str | None = None,
        suitability_score: Any = None,
    ) -> None:
        """Send one compact Telegram message for every processed job."""

        normalized_reason = str(reason or "").upper()
        canonical_status = final_status
        if canonical_status is None:
            if "UNVERIFIED_AFTER_SUBMIT" in normalized_reason:
                canonical_status = "UNVERIFIED_AFTER_SUBMIT"
            elif result == "Success":
                canonical_status = "SUBMITTED"
            elif result == "Cancelled":
                canonical_status = "CANCELLED"
            elif result == "Skip":
                canonical_status = "SKIPPED"
            else:
                canonical_status = "TECHNICAL_FAILURE"

        status = {
            "SUBMITTED": "✅ SUBMITTED",
            "UNVERIFIED_AFTER_SUBMIT": "⚠️ UNVERIFIED AFTER SUBMIT",
            "NEEDS_HUMAN": "👤 NEEDS HUMAN",
            "NOT_ELIGIBLE": "⏭ NOT ELIGIBLE",
            "SKIPPED": "⏭ SKIPPED",
            "TECHNICAL_FAILURE": "❌ TECHNICAL FAILURE",
            "CANCELLED": "🛑 CANCELLED",
            "LIMIT": "⏹ LIMIT",
        }.get(canonical_status, f"ℹ️ {str(canonical_status).upper()}")

        title = job_title or "Unknown job"
        company = company_name or "Unknown company"

        clean_reason = safe_public_reason(
            reason, canonical_status, suitability_score=suitability_score
        )

        # Keep Telegram messages short.
        if len(clean_reason) > 180:
            clean_reason = clean_reason[:177] + "..."

        message = f"{status} — {title} @ {company}\nRun: {self.run_id}"

        if clean_reason:
            message += f"\nWhy: {clean_reason}"

        if url:
            message += f"\n{url}"

        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                message_thread_id=self.report_topic_id,
                text=message,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            mirror_telegram_message(
                message_type="job_event", text=message, topic=self.report_topic_id,
                success=False, error=type(exc).__name__,
            )
            raise
        mirror_telegram_message(
            message_type="job_event", text=message, topic=self.report_topic_id, success=True
        )

    async def send_test_message(self, text: str | None = None) -> None:
        """Send a small diagnostic message to the configured report chat/topic."""
        message = text or "Telegram report test from LinkedIn AI Job Applier."
        await self.bot.send_message(
            chat_id=self.chat_id,
            message_thread_id=self.report_topic_id,
            text=message,
        )
        destination = f"chat {self.chat_id}"
        if self.report_topic_id:
            destination += f", topic {self.report_topic_id}"
        logger.info(f"Telegram test message sent to {destination}")

    async def send_test_error_message(self, text: str | None = None) -> None:
        """Send a small diagnostic message to the configured error chat/topic."""
        message = text or "Telegram error-reporting test from LinkedIn AI Job Applier."
        await self.bot.send_message(
            chat_id=self.chat_id,
            message_thread_id=self.err_topic_id,
            text=f"Error:\n```{message}```",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        destination = f"chat {self.chat_id}"
        if self.err_topic_id:
            destination += f", topic {self.err_topic_id}"
        logger.info(f"Telegram error test message sent to {destination}")

    def _format_jobs_no_info(self, jobs_no_info: list) -> str:
        """Format the information about the vacancies to which the application could not respond for whatever reason"""
        res = ""
        for job_info in jobs_no_info:
            res += f"**Vacancy name:** {job_info['job_title']}\n"
            res += f"**Vacancy link:** {job_info['link']}\n"
            res += f"**Reason:** {job_info['reason']}\n\n"
        return res


if __name__ == "__main__":

    def parse_args():
        parser = argparse.ArgumentParser(description="Telegram diagnostics")
        parser.add_argument(
            "--test-report",
            action="store_true",
            help="Send a test message to tg_chat_id and optional tg_report_topic_id from .env",
        )
        parser.add_argument(
            "--test-error",
            action="store_true",
            help="Send a test error message to tg_chat_id and optional tg_err_topic_id from .env",
        )
        parser.add_argument(
            "--message",
            default=None,
            help="Custom text for --test-report",
        )
        return parser.parse_args()

    async def main():
        args = parse_args()
        if args.test_report:
            sender = TelegramReportSender()
            await sender.send_test_message(args.message)
            return
        if args.test_error:
            sender = TelegramReportSender()
            await sender.send_test_error_message(args.message)
            return

        logger.info("No action requested. Use --test-report or --test-error")

    asyncio.run(main())
