"""This module is used to run the LinkedIn/Indeed bot"""

import asyncio
import os
import traceback
from pathlib import Path
from threading import Lock

import dotenv
from pypdf import PdfReader

# TODO: Create a tutorial video for the bot


# Try to import pynput for keyboard control (optional, not available in Docker)
try:
    from pynput import keyboard as pynput_kb

    PYNPUT_AVAILABLE = True
except (ImportError, Exception):
    PYNPUT_AVAILABLE = False
    pynput_kb = None

from config.app_config import JOB_SITE, RESTART_EVERY_DAY
from config.constants import (
    BROWSER_STORAGE_STATE,
    RESUME_DIR,
    RESUME_TEXT_TEMPLATE_FILE,
    SEARCH_CONFIG_FILE,
)
from config.logger_config import logger
from src.dashboard.runtime import StopRequested, emit_event, get_control_state, update_control_state

if JOB_SITE == "indeed":
    from src.job_manager.indeed.authenticator_indeed import IndeedAuthenticator as Authenticator
    from src.job_manager.indeed.job_manager_indeed import IndeedJobManager as LinkedInJobManager
    from src.job_manager.indeed.search_customizer_indeed import (
        IndeedSearchCustomizer as SearchCustomizer,
    )
else:
    from src.job_manager.linkedin.authenticator_linkedin import (
        LinkedInAuthenticator as Authenticator,
    )
    from src.job_manager.linkedin.job_manager_linkedin import LinkedInJobManager
    from src.job_manager.linkedin.search_customizer_linkedin import SearchCustomizer

from src.job_manager.bot_facade import BotFacade
from src.job_manager.resume_anonymizer import ResumeAnonymizer
from src.llm.apply_agent import ApplyAgent
from src.llm.llm_manager import GPTAnswerer
from src.pydantic_models.config_models import SearchConfig, Secrets
from src.pydantic_models.prompt_models import ResumeStructure
from src.resume_builder.resume_generator import ResumeGenerator
from src.resume_builder.resume_manager import ResumeManager
from src.resume_builder.style_manager import StyleManager
from src.utils.browser_utils import create_playwright_browser, save_browser_session, stop_tracing
from src.utils.candidate_integrity import validate_candidate_identity
from src.utils.candidate_profile import CANDIDATE_PROFILE_PATH
from src.utils.candidate_preferences import normalize_relocation_prompt
from src.utils.runtime_control import (
    ShutdownState,
    register_shutdown_handlers,
    runtime_controller,
    sleep_with_shutdown,
)
from src.utils.run_context import get_run_id
from src.utils.utils import (
    get_ready_made_resume,
    load_yaml_file,
    save_yaml_file,
    validate_and_prompt_resume_completion,
)

# Create necessary directories if they don't exist
os.makedirs(RESUME_DIR, exist_ok=True)

# Resume file paths
RESUME_STRUCTURED_FILE = Path(RESUME_DIR) / "structured_resume.yaml"
RESUME_TEXT_FILE = Path(RESUME_DIR) / "resume_text.txt"

READY_MADE_RESUME = get_ready_made_resume()

# Global pause state for keyboard control
paused = False
pause_lock = Lock()
ctrl_pressed = False
last_dashboard_pause_state = False


class ConfigError(Exception):
    pass


def browser_recovery_allowed(restart_due_to_driver: bool, should_exit: bool) -> bool:
    """Return whether a dead driver may open another browser session."""
    return (
        restart_due_to_driver
        and not should_exit
        and not runtime_controller.is_shutdown_requested()
    )


class ConfigValidator:
    """Class for validating configuration settings"""

    def validate_search_config(self, config_yaml_path: Path) -> dict:
        """Validate LinkedIn search configuration settings"""
        try:
            parameters = load_yaml_file(config_yaml_path)
            parameters = SearchConfig(**parameters)
            logger.debug("LinkedIn search config loaded successfully.")
            return parameters.model_dump()
        except Exception as e:
            raise ConfigError(f"LinkedIn configuration validation error: {str(e)}")

    @staticmethod
    def validate_secrets() -> dict:
        """Check for required secret keys based on active JOB_SITE"""
        secrets = {**dotenv.dotenv_values(".env")}
        try:
            if JOB_SITE == "indeed":
                required_keys = ["indeed_email"]
            else:
                required_keys = ["linkedin_email", "linkedin_password"]

            missing_keys = [key for key in required_keys if not secrets.get(key)]
            if missing_keys:
                raise ValueError(f"Missing required keys: {', '.join(missing_keys)}")

            secrets_config = Secrets(**secrets)
            logger.debug(f"{JOB_SITE} secrets validated successfully.")
            return secrets_config.model_dump()
        except Exception as e:
            raise ConfigError(f"Secrets validation error: {str(e)}")

    @staticmethod
    def validate_resume_text(resume_file: Path) -> str:
        """Check for resume file"""
        try:
            with open(resume_file, "r", encoding="utf-8") as f:
                resume_text = f.read()
                if not resume_text:
                    raise ConfigError("Resume not found")
                return resume_text
        except FileNotFoundError:
            return ""
        except Exception as e:
            raise ConfigError(f"Resume validation error: {str(e)}")

    @staticmethod
    def validate_resume_structured(resume_structured_file: Path) -> dict:
        """Check for structured resume file"""
        try:
            resume_structured = load_yaml_file(resume_structured_file)
            resume_structured = ResumeStructure(**resume_structured)
            return resume_structured.model_dump()
        except Exception as e:
            if str(e).startswith("File not found"):
                logger.warning("Resume template not found, creating new one")
                return {}
            raise ConfigError(f"Structured resume validation error: {str(e)}")


def generate_resume_text_from_pdf(secrets: dict) -> str:
    """Find a PDF in RESUME_DIR, parse it, and write a formatted resume_text.txt using the LLM."""
    pdf_files = list(Path(RESUME_DIR).glob("*.pdf"))
    if not pdf_files:
        return ""

    pdf_path = pdf_files[0]
    logger.info("Generating resume_text.txt from configured PDF source")

    reader = PdfReader(pdf_path)
    raw_text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    if not raw_text:
        logger.warning("Could not extract text from configured PDF source")
        return ""

    template = Path(RESUME_TEXT_TEMPLATE_FILE).read_text(encoding="utf-8")

    llm_answerer = GPTAnswerer(
        secrets.get("llm_api_key"), secrets.get("llm_proxy"), secrets.get("llm_api_url")
    )
    resume_text = llm_answerer.generate_resume_text(raw_text, template)

    RESUME_TEXT_FILE.write_text(resume_text, encoding="utf-8")
    logger.info(f"resume_text.txt generated and saved to {RESUME_TEXT_FILE}")
    return resume_text


def on_press(key):
    """Handle key press events"""
    if not PYNPUT_AVAILABLE:
        return

    global paused, ctrl_pressed
    try:
        # Track Ctrl key state
        if key in (pynput_kb.Key.ctrl_l, pynput_kb.Key.ctrl_r):
            ctrl_pressed = True
        # Check for 'x' key when Ctrl is pressed
        elif hasattr(key, "char") and key.char == "x" and ctrl_pressed:
            with pause_lock:
                paused = not paused
                if paused:
                    logger.warning("⏸️  PAUSED - Press Ctrl+X to continue")
                    emit_event("pause_state_changed", "Keyboard pause requested", paused=True)
                else:
                    logger.info("▶️  RESUMED")
                    emit_event("pause_state_changed", "Keyboard resume requested", paused=False)
    except AttributeError:
        pass


def on_release(key):
    """Handle key release events"""
    if not PYNPUT_AVAILABLE:
        return

    global ctrl_pressed
    # Reset Ctrl key state
    if key in (pynput_kb.Key.ctrl_l, pynput_kb.Key.ctrl_r):
        ctrl_pressed = False


def start_keyboard_listener():
    """Start keyboard listener in background thread"""
    if not PYNPUT_AVAILABLE:
        logger.info("Keyboard control disabled (pynput not available - Docker/headless mode)")
        return

    try:
        listener = pynput_kb.Listener(on_press=on_press, on_release=on_release)
        listener.daemon = True
        listener.start()
        logger.info("Keyboard listener started - Press Ctrl+X to pause/resume")
    except Exception as e:
        logger.warning(f"Could not start keyboard listener: {e}")


async def check_pause():
    """Check if execution is paused and wait if needed"""
    global last_dashboard_pause_state, paused

    control = get_control_state() if os.environ.get("DASHBOARD_RUN_ID") else {}
    effective_paused = paused or control.get("pause_requested", False)

    if control.get("stop_requested"):
        runtime_controller.request_shutdown("dashboard")
        return

    if effective_paused != last_dashboard_pause_state:
        emit_event(
            "pause_state_changed",
            f"Execution {'paused' if effective_paused else 'resumed'}",
            paused=effective_paused,
            source="dashboard" if control.get("pause_requested") and not paused else "keyboard",
        )
        last_dashboard_pause_state = effective_paused

    while paused or (
        os.environ.get("DASHBOARD_RUN_ID") and get_control_state().get("pause_requested", False)
    ):
        if os.environ.get("DASHBOARD_RUN_ID") and get_control_state().get("stop_requested"):
            runtime_controller.request_shutdown("dashboard")
            return
        await asyncio.sleep(0.5)

    if last_dashboard_pause_state:
        emit_event("pause_state_changed", "Execution resumed", paused=False)
        last_dashboard_pause_state = False


async def create_and_run_bot(
    search_config: dict,
    secrets: dict,
    resume_text: str,
    resume_structured: dict,
):
    """Start LinkedIn bot (async)"""
    logger.info("Initializing LinkedIn bot...")
    # Fresh shutdown state per run (matters when RESTART_EVERY_DAY loops runs)
    if not runtime_controller.begin_run():
        logger.info("Shutdown was already requested; refusing to start a new bot run")
        emit_event(
            "run_stopped",
            "LinkedIn bot run was not started because shutdown is draining",
            partial=True,
        )
        runtime_controller.finish_run()
        return False
    emit_event(
        "run_started",
        "LinkedIn bot run started",
        positions=search_config.get("positions", []),
        locations=search_config.get("locations", []),
    )

    # Initialize browser Playwright based on configuration
    try:
        if runtime_controller.is_shutdown_requested():
            logger.info("Shutdown requested — refusing browser startup")
            runtime_controller.finish_run()
            return False
        browser, context, page = await create_playwright_browser()
        # Local runtime patch: detect manual browser closure and recover cleanly.
        logger.info("Playwright browser initialized successfully")
        emit_event("browser_initialized", "Playwright browser initialized")

    except Exception as e:
        runtime_controller.finish_run()
        logger.error(f"Browser initialization error: {e}")
        emit_event("run_failed", "Browser initialization failed", error=str(e))
        raise RuntimeError(f"Failed to initialize browser: {e}")

    try:
        if runtime_controller.is_shutdown_requested():
            logger.info("Shutdown requested — refusing authentication startup")
            return False

        # Resolve credentials based on active site
        if JOB_SITE == "indeed":
            site_email = secrets["indeed_email"]
            site_password = None
        else:
            site_email = secrets["linkedin_email"]
            site_password = secrets["linkedin_password"]

        # Initialize authenticator
        authenticator = Authenticator(page)
        authenticator.set_parameters(site_email, site_password)

        # Attempt login
        login_success = await authenticator.start()
        if login_success:
            await save_browser_session(context)
            logger.info("Successfully logged into LinkedIn!")
            logger.info("LinkedIn bot ready to work")
            emit_event("login_success", "LinkedIn login succeeded")
        else:
            logger.error("Failed to log into LinkedIn")
            emit_event("run_failed", "LinkedIn login failed")
            return False


        if runtime_controller.is_shutdown_requested():
            logger.info("Shutdown requested — refusing search/filter startup")
            return False

        # Set GPT answerer
        llm_api_key = secrets.get("llm_api_key")
        llm_proxy = secrets.get("llm_proxy")
        llm_api_url = secrets.get("llm_api_url")
        llm_answerer_component = GPTAnswerer(llm_api_key, llm_proxy, llm_api_url)
        # Keep browser-use / external ATS state isolated from
        # the main LinkedIn Playwright session.
        external_browser_state = (
            "browser_session/external_apply_state.json"
        )

        llm_agent_component = ApplyAgent(
            llm_api_key,
            external_browser_state,
            llm_api_url,
            site_email,
        )

        linkedin_email = site_email  # kept for LinkedInJobManager constructor compatibility

        if not resume_structured:
            resume_structured = llm_answerer_component.parse_resume(resume_text)
            resume_structured = ResumeStructure(**resume_structured).model_dump()
            save_yaml_file(RESUME_STRUCTURED_FILE, resume_structured)

        # Set resume anonymizer and anonymize the resume information
        resume_anonymizer = ResumeAnonymizer(resume_structured)
        resume_anonymizer.anonymize_personal_information()
        resume_structured = resume_anonymizer.resume_anonymized
        resume_text_anonymized = resume_anonymizer.anonymize_text(resume_text)
        application_profile_path = CANDIDATE_PROFILE_PATH
        resume_text_anonymized = normalize_relocation_prompt(
            resume_text_anonymized,
            application_profile_path,
            resume_structured,
        )
        normalized_external_resume = normalize_relocation_prompt(
            resume_text,
            application_profile_path,
            resume_structured,
        )

        # Set GPT resume generator
        style_manager = StyleManager()
        resume_generator = ResumeGenerator(llm_answerer_component, resume_anonymizer)
        resume_generator_manager = ResumeManager(llm_api_key, style_manager, resume_generator)

        resume_ready_made = READY_MADE_RESUME is not None and READY_MADE_RESUME.resolve().is_file()
        if not resume_ready_made:
            resume_generator_manager.choose_style()

        # Set search component
        search_component = SearchCustomizer(page)

        # Set apply component
        apply_component = LinkedInJobManager(
            page, linkedin_email, resume_anonymizer, search_component
        )

        # Set bot facade
        bot = BotFacade(resume_anonymizer, search_component, apply_component, llm_agent_component)
        bot.set_parameters(search_config)
        bot.set_pause_checker(check_pause)

        # Check if the last search was less than a day ago (LinkedIn only)
        if (
            RESTART_EVERY_DAY
            and JOB_SITE == "linkedin"
            and not apply_component.check_the_last_search_time()
        ):
            logger.warning(
                "Last search was less than a day ago, finishing work. If you want to restart the search, delete the file data/output/last_run.yaml file"
            )
            emit_event(
                "run_stopped", "Run skipped because the daily restart window is still active"
            )
            return True

        # Validate structured resume and prompt user if needed (skip when launched from dashboard)
        if not os.environ.get("DASHBOARD_RUN_ID") and not validate_and_prompt_resume_completion(
            resume_structured, RESUME_STRUCTURED_FILE, RESUME_TEXT_FILE
        ):
            logger.info("User chose to exit and complete resume information")
            emit_event("run_stopped", "Run stopped because resume validation was not accepted")
            return False

        if runtime_controller.is_shutdown_requested():
            logger.info("Shutdown requested — refusing search navigation")
            return False
        await bot.set_search_parameters(search_config)
        if runtime_controller.is_shutdown_requested():
            logger.info("Shutdown requested — refusing worker startup after search setup")
            return False
        bot.set_answerer_and_agent(llm_answerer_component, llm_agent_component, search_config)
        bot.set_resume(resume_structured, normalized_external_resume, resume_text_anonymized)
        if not resume_ready_made:
            bot.set_resume_generator(resume_generator_manager)
        await bot.start_apply()
        if runtime_controller.is_shutdown_requested():
            emit_event("run_stopped", "LinkedIn bot run stopped gracefully", partial=True)
        else:
            emit_event("run_completed", "LinkedIn bot run completed successfully")

    finally:
        # Cleanup browser resources
        logger.info("Cleaning up browser resources...")
        runtime_controller.set_shutdown_state(ShutdownState.CLEANUP)
        try:
            if context is not None:
                await save_browser_session(context)
                await stop_tracing(context)
            # Close Playwright browser (browser is None when using persistent context)
            if browser is not None:
                await browser.close()
            elif context is not None:
                await context.close()
            logger.info("Playwright browser closed")
            emit_event("browser_closed", "Playwright browser closed")

        except Exception as e:
            logger.warning(f"Error during browser cleanup: {e}")
        finally:
            runtime_controller.set_shutdown_state(ShutdownState.DONE)
            # Local runtime patch: release any pending shutdown handler waits.
            runtime_controller.finish_run()


def main() -> None:
    run_id = get_run_id()
    logger.info(f"Bobby run ID: {run_id}")
    # Start keyboard listener for pause/resume functionality
    # Local runtime patch: register graceful shutdown handlers once at startup.
    register_shutdown_handlers()
    if not runtime_controller.start_process_run(run_id):
        logger.info("Shutdown was already requested; process run will not start")
        return
    start_keyboard_listener()
    if not os.environ.get("DASHBOARD_RUN_ID"):
        update_control_state(stop_requested=False, pause_requested=False)

    while True:
        should_exit = False
        restart_due_to_driver = False
        try:
            # create output folder if it doesn't exist
            data = Path("data")
            output_folder = data / "output"
            output_folder.mkdir(exist_ok=True)
            linkedin_output_folder = output_folder / "linkedin"
            linkedin_output_folder.mkdir(exist_ok=True)
            indeed_output_folder = output_folder / "indeed"
            indeed_output_folder.mkdir(exist_ok=True)

            # validate config files
            config_validator = ConfigValidator()
            secrets = config_validator.validate_secrets()
            search_config = config_validator.validate_search_config(SEARCH_CONFIG_FILE)
            resume_text = config_validator.validate_resume_text(RESUME_TEXT_FILE)
            if not resume_text:
                resume_text = generate_resume_text_from_pdf(secrets)
            resume_structured = config_validator.validate_resume_structured(RESUME_STRUCTURED_FILE)

            if not resume_text and not resume_structured:
                raise FileNotFoundError(
                    f"Can't find neither resume text file {RESUME_TEXT_FILE} nor resume structured file {RESUME_STRUCTURED_FILE}"
                )

            candidate_name = validate_candidate_identity(
                resume_text=resume_text,
                resume_structured=resume_structured,
                application_profile_path=CANDIDATE_PROFILE_PATH,
                resume_pdf_path=READY_MADE_RESUME or Path(""),
            )
            logger.info("Candidate identity integrity check passed")

            logger.info(f"Starting {JOB_SITE.capitalize()} Job Applier...")
            logger.info(f"Search config loaded with {len(search_config)} parameters")

            asyncio.run(create_and_run_bot(search_config, secrets, resume_text, resume_structured))
            logger.info(f"{JOB_SITE.capitalize()} bot completed successfully")

        except StopRequested as stop_requested:
            logger.warning(str(stop_requested))
            emit_event("run_stopped", "Run stopped gracefully by dashboard")
            should_exit = True

        except ConfigError as ce:
            logger.error(f"Configuration error: {str(ce)}")
            emit_event("run_failed", "Configuration error", error=str(ce))
        except FileNotFoundError as fnf:
            tb_str = traceback.format_exc()
            logger.error(f"File not found: {str(fnf)}\n{tb_str}")
            emit_event("run_failed", "Required file was not found", error=str(fnf))
        except RuntimeError as re:
            if "PLAYWRIGHT_DRIVER_DIED" in str(re):
                if runtime_controller.is_shutdown_requested():
                    should_exit = True
                    logger.info(
                        "Playwright driver closed during shutdown; "
                        "browser recovery is permanently suppressed"
                    )
                else:
                    restart_due_to_driver = True
                    logger.warning(
                        "Playwright driver died. "
                        "The bot will create a fresh browser in 10 seconds."
                    )
                    emit_event(
                        "browser_restart_requested",
                        "Playwright driver died; browser restart requested",
                    )
            else:
                tb_str = traceback.format_exc()
                logger.error(f"Runtime error: {str(re)}\n{tb_str}")
                emit_event("run_failed", "Runtime error", error=str(re))
        except Exception as e:
            tb_str = traceback.format_exc()
            logger.error(f"Unknown error: {str(e)}\n{tb_str}")
            emit_event("run_failed", "Unhandled exception", error=str(e))
        finally:
            logger.info("Program completed")
            # A dead Playwright driver requires a fresh browser immediately.
            if browser_recovery_allowed(restart_due_to_driver, should_exit):
                logger.warning(
                    "Restarting LinkedIn browser session in 10 seconds..."
                )
                if asyncio.run(sleep_with_shutdown(10)):
                    should_exit = False
                else:
                    logger.info("Shutdown requested during browser recovery wait")
                    should_exit = True

            # Normal scheduled restart behavior.
            elif (
                RESTART_EVERY_DAY
                and not should_exit
                and not runtime_controller.is_shutdown_requested()
            ):
                logger.info("Waiting 1 hour before next run")
                # Local runtime patch: make the daily wait interruptible.
                if not asyncio.run(sleep_with_shutdown(3600)):
                    logger.info("Shutdown requested during wait interval")
                    should_exit = True
            else:
                logger.info("Exiting program")
                should_exit = True

        if should_exit:
            break


if __name__ == "__main__":
    main()
