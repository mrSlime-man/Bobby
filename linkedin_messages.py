import argparse
import asyncio
import traceback
from pathlib import Path

import dotenv
import yaml

from config.constants import RESUME_DIR
from config.logger_config import logger
from src.job_manager.linkedin.authenticator_linkedin import LinkedInAuthenticator
from src.job_manager.linkedin.messages_manager_linkedin import LinkedInMessagesManager
from src.llm.llm_manager import GPTAnswerer
from src.pydantic_models.config_models import LinkedInMessagesConfig, Secrets
from src.pydantic_models.prompt_models import ResumeStructure
from src.utils.browser_utils import create_playwright_browser, save_browser_session, stop_tracing
from src.utils.utils import ConfigError, load_yaml_file

RESUME_STRUCTURED_FILE = Path(RESUME_DIR) / "structured_resume.yaml"
RESUME_TEXT_FILE = Path(RESUME_DIR) / "resume_text.txt"
MESSAGES_CONFIG_FILE = Path("config/linkedin_messages_manager_config.yaml")


def load_messages_config(config_file: Path = MESSAGES_CONFIG_FILE) -> dict:
    if not config_file.exists():
        logger.warning(f"Messages config file not found: {config_file}. Using defaults.")
        return LinkedInMessagesConfig().model_dump()

    with config_file.open("r", encoding="utf-8") as file:
        config_data = yaml.safe_load(file) or {}

    return LinkedInMessagesConfig(**config_data).model_dump()


def validate_secrets() -> dict:
    secrets = {**dotenv.dotenv_values(".env")}
    required_keys = ["linkedin_email", "linkedin_password"]
    missing_keys = [key for key in required_keys if not secrets.get(key)]
    if missing_keys:
        raise ConfigError(f"Missing required keys: {', '.join(missing_keys)}")
    return Secrets(**secrets).model_dump()


def validate_resume_text(resume_file: Path) -> str:
    try:
        with open(resume_file, "r", encoding="utf-8") as file:
            return file.read().strip()
    except FileNotFoundError as exc:
        raise ConfigError(f"Resume text file not found: {resume_file}") from exc


def validate_resume_structured(resume_structured_file: Path) -> dict:
    try:
        resume_structured = load_yaml_file(resume_structured_file)
    except Exception as exc:
        raise ConfigError(f"Structured resume validation error: {exc}") from exc
    return ResumeStructure(**resume_structured).model_dump()


async def run_messages(
    limit: int,
    dry_run: bool,
    execute_archives: bool,
    execute_replies: bool,
    unread_only: bool = False,
    message_preferences: dict | None = None,
) -> None:
    browser = None
    context = None
    page = None
    message_preferences = message_preferences or load_messages_config()

    secrets = validate_secrets()
    resume_text = validate_resume_text(RESUME_TEXT_FILE)
    resume_structured = validate_resume_structured(RESUME_STRUCTURED_FILE)

    try:
        browser, context, page = await create_playwright_browser()

        authenticator = LinkedInAuthenticator(page)
        authenticator.set_parameters(secrets["linkedin_email"], secrets["linkedin_password"])
        if not await authenticator.start():
            raise RuntimeError("Failed to log into LinkedIn")

        llm_answerer = GPTAnswerer(
            secrets.get("llm_api_key"),
            secrets.get("llm_proxy"),
            secrets.get("llm_api_url"),
        )
        llm_answerer.set_resume(resume_structured, resume_text)
        llm_answerer.set_linkedin_message_preferences(message_preferences)

        manager = LinkedInMessagesManager(
            page,
            llm_answerer,
            resume_structured,
            message_preferences=message_preferences,
        )
        results = await manager.run(
            limit=limit,
            dry_run=dry_run,
            execute_archives=execute_archives,
            execute_replies=execute_replies,
            unread_only=unread_only,
        )
        logger.info(f"LinkedIn messages dry run completed with {len(results)} conversations")

    finally:
        try:
            if context is not None:
                await save_browser_session(context)
                await stop_tracing(context)
            if browser is not None:
                await browser.close()
            elif context is not None:
                await context.close()
        except Exception as exc:
            logger.warning(f"Error during browser cleanup: {exc}")


def main() -> None:
    messages_config = load_messages_config()
    parser = argparse.ArgumentParser(description="Run LinkedIn messages manager in dry-run mode.")
    parser.add_argument(
        "--limit",
        type=int,
        default=messages_config["max_conversations_to_scan"],
        help="Maximum conversations to process",
    )
    parser.add_argument(
        "--execute-archives",
        action="store_true",
        help="Execute archive and spam actions instead of only recording them.",
    )
    parser.add_argument(
        "--execute-replies",
        action="store_true",
        help="Send drafted replies instead of only recording them.",
    )
    args = parser.parse_args()

    execute_archives = args.execute_archives or messages_config["execute_archives"]
    execute_replies = args.execute_replies or messages_config["execute_replies"]
    unread_only = messages_config["unread_only"]
    dry_run = messages_config["dry_run"] and not (args.execute_archives or args.execute_replies)
    if execute_archives or execute_replies:
        dry_run = False

    try:
        asyncio.run(
            run_messages(
                limit=args.limit,
                dry_run=dry_run,
                execute_archives=execute_archives,
                execute_replies=execute_replies,
                unread_only=unread_only,
                message_preferences=messages_config,
            )
        )
    except Exception as exc:
        tb_str = traceback.format_exc()
        logger.error(f"LinkedIn messages dry run failed: {exc}\n{tb_str}")
        raise


if __name__ == "__main__":
    main()
