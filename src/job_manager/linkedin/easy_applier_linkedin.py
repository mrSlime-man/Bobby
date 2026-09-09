import asyncio
import base64
import hashlib
import inspect
import os
import re
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import Page
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from config.constants import PHOTO_DIR, RESUME_DIR
from config.logger_config import logger
from src.dashboard.runtime import StopRequested, capture_page_screenshot, emit_event
from src.job_manager.easy_applier import BaseEasyApplier, NoInfoException
from src.job_manager.application_question_resolver import (
    AnswerType,
    FieldSpec,
    field_spec_from_snapshot,
    canonical_category,
    match_available_option,
    normalize_answer_for_field,
    normalize_question,
    normalize_option,
    parse_radio_snapshot,
    resolve_canonical_answer,
    resolve_canonical_text_answer,
    resolve_structured_answer,
    validate_answer_for_field,
)
from src.job_manager.resume_anonymizer import ResumeAnonymizer
from src.llm.llm_manager import GPTAnswerer
from src.pydantic_models.job_models import Job, Question
from src.utils.browser_utils import (
    debug_capture,
    find_element_safely,
    find_elements_safely,
    get_clean_text,
)
from src.utils.candidate_profile import load_candidate_profile
from src.utils.easy_apply_quota import (
    BLOCKED as EASY_APPLY_QUOTA_BLOCKED,
    collect_easy_apply_ui,
    easy_apply_quota_state,
)
from src.utils.redaction import redact_text
from src.utils.runtime_control import runtime_controller
from src.utils.utils import (
    async_pause,
    get_ready_made_photo,
    get_ready_made_resume,
    load_yaml_file,
    sanitize_text,
)


class UnverifiedAfterSubmitError(RuntimeError):
    """Final submit was clicked, but no independent confirmation was found."""


class LinkedInEasyApplier(BaseEasyApplier):
    _NAVIGATION_ACTIONS = ("next", "continue", "review", "submit application")
    _NAVIGATION_CLICK_TIMEOUT_MS = 5000
    _NAVIGATION_RETRY_MARKERS = (
        "timeout",
        "detached",
        "not attached",
        "not connected",
    )
    _TARGET_CLOSED_MARKERS = (
        "connection closed while reading from the driver",
        "target page, context or browser has been closed",
        "browser has been closed",
        "page has been closed",
        "context has been closed",
        "playwright connection closed",
    )

    @classmethod
    def _is_target_closed_error(cls, error: BaseException) -> bool:
        message = str(error).casefold()
        return any(marker in message for marker in cls._TARGET_CLOSED_MARKERS)

    @classmethod
    def _reraise_if_target_closed(cls, error: BaseException) -> None:
        if cls._is_target_closed_error(error):
            raise error

    async def _ensure_target_open(self) -> None:
        """Raise one canonical error before any further DOM work on a dead page."""
        try:
            closed = self.page.is_closed()
            if inspect.isawaitable(closed):
                closed = await closed
        except Exception as exc:
            self._reraise_if_target_closed(exc)
            return
        if closed is True:
            raise RuntimeError("Target page, context or browser has been closed")

    def __init__(
        self,
        page: Page,
        gpt_answerer: GPTAnswerer,
        resume_anonymizer: ResumeAnonymizer,
        resume_generator_manager,
        pause_checker,
        answers_file: Path,
        resume_dir: Path,
        cover_letter_dir: Path,
        test_mode: bool,
    ):
        logger.info("Initializing LinkedInEasyApplier")
        super().__init__()
        self.page = page
        self.gpt_answerer = gpt_answerer
        self.resume_anonymizer = resume_anonymizer
        self.resume_generator_manager = resume_generator_manager
        self.pause_checker = pause_checker
        self.answers_file = answers_file
        self.resume_dir = resume_dir
        self.generated_resume_dir = Path(resume_dir) / "generated_resumes"
        self.generated_photo_dir = Path(PHOTO_DIR)
        self.generated_cover_letter_dir = Path(cover_letter_dir) / "generated_cover_letters"
        self.ready_made_resume_path = get_ready_made_resume()
        self.ready_made_photo_path = get_ready_made_photo()
        self.all_questions = self._load_questions()
        self.current_job = None
        self.test_mode = test_mode
        self.previous_question_texts = []
        self._last_navigation_state: Dict[str, Any] = {}
        self._force_fresh_question_resolution = False
        self._last_form_fingerprint: str | None = None
        self._same_form_state_count = 0
        try:
            # Production uses the repository-level canonical profile. Test or
            # embedded callers with an isolated resume directory retain their
            # local fixture profile and never read the operator's real profile.
            if Path(resume_dir).resolve() == Path(RESUME_DIR).resolve():
                self.application_profile = load_candidate_profile()
            else:
                self.application_profile = load_yaml_file(
                    Path(resume_dir) / "application_profile.yaml"
                ) or {}
        except Exception as exc:
            logger.warning("Candidate profile unavailable to screening resolver: {}", exc)
            self.application_profile = {}
        logger.info("LinkedInEasyApplier initialized successfully")

    def set_page(self, page: Page) -> None:
        self.page = page

    async def check_for_premium_redirect(self, job: Job, max_attempts=3) -> bool:
        """Check for LinkedIn premium redirect and attempt to return to job page (async)"""
        current_url = self.page.url
        attempts = 0
        is_redirected = False
        while "linkedin.com/premium" in current_url and attempts < max_attempts:
            logger.warning("Redirected to linkedIn Premium page. Attempting to return to job page.")
            attempts += 1
            is_redirected = True
            await self.page.goto(job.url)
            await async_pause(2, 3)
            current_url = self.page.url

        if "linkedin.com/premium" in current_url:
            logger.error(
                f"Failed to return to job page after {max_attempts} attempts. Cannot apply for the job."
            )
            raise Exception(
                f"Redirected to linkedIn Premium page and failed to return after {max_attempts} attempts. Job application aborted."
            )
        return is_redirected

    async def apply_to_job(self, job: Job) -> Tuple[Tuple[str, str], Any]:
        """
        Starts the process of applying to a job (async).
        :param job: A job object with the job details.
        :return: None
        """
        if (
            runtime_controller.is_shutdown_requested()
            and not runtime_controller.has_active_worker("easy_apply")
        ):
            logger.info("Shutdown requested — Easy Apply admission is closed")
            return (
                "Cancelled",
                "CANCELLED_BY_SHUTDOWN: Easy Apply worker was not started",
            ), None

        if await self._check_easy_apply_limit():
            logger.info("Easy Apply quota is blocked; deferring this Easy Apply-only job")
            return ("Deferred", easy_apply_quota_state().deferred_reason()), None

        logger.info(f"Applying to job: {job.job_title} at {job.company_name}")
        job_match = re.search(r"/jobs/view/(\d+)", str(job.url or ""))
        application_id = f"app-{job_match.group(1) if job_match else hashlib.sha256(str(job.url).encode()).hexdigest()[:20]}"
        emit_event(
            "easy_apply_started",
            f"Easy Apply started for {job.job_title}",
            application_id=application_id,
            job_id=job_match.group(1) if job_match else "",
            job_title=job.job_title,
            company_name=job.company_name,
            linkedin_url=job.url,
            application_type="EASY_APPLY",
            url=job.url,
        )

        try:
            apply_result = await self.job_easy_apply(job)
            return apply_result, self.submitted_resume_path
        except StopRequested:
            raise
        except Exception as e:
            if self._is_target_closed_error(e):
                # The page is unusable; do not attempt screenshots, locators, or
                # save/discard operations from this wrapper.
                raise
            logger.error(f"Failed to apply to job: {job.job_title} at {job.url}, error: {str(e)}")
            await debug_capture(self.page, "apply_to_job_error")
            raise e

    async def job_easy_apply(self, job: Job) -> Tuple[str, str]:
        """Main job application logic (async)"""
        self.final_submit_attempted = False
        try:
            self.current_job = job
            try:
                await self.page.evaluate("document.activeElement && document.activeElement.blur()")
            except Exception:
                pass
            logger.debug("Focus removed from the active element")
            logger.info("Attempting to click 'Easy Apply' button")
            while True:
                if await self._is_already_applied():
                    return "Skip", "Already applied to this job"
                # Click 'Easy Apply' button
                result = await self._find_easy_apply_button(job)
                if result is None:
                    return "Deferred", easy_apply_quota_state().deferred_reason()
                if result is False:
                    if await self._is_already_applied():
                        return "Skip", "Already applied to this job"
                    return (
                        "Skip",
                        "No clickable 'Easy Apply' button found, maybe you already applied to this job",
                    )
                logger.debug("'Easy Apply' button clicked successfully")
                await async_pause()
                if await self._check_easy_apply_limit():
                    logger.warning("Easy Apply quota detected after opening the control")
                    return "Deferred", easy_apply_quota_state().deferred_reason()
                # Click 'Continue Applying' button if it appears
                await self._click_continue_applying_button()
                await async_pause()
                if await self._check_easy_apply_limit():
                    logger.warning("Easy Apply quota detected in the application dialog")
                    return "Deferred", easy_apply_quota_state().deferred_reason()
                # Check for premium redirect
                if not await self.check_for_premium_redirect(self.current_job):
                    break
                else:
                    logger.debug("Redirected to premium page, trying again")

            logger.info("Filling out application form")
            await async_pause(2, 3)
            await capture_page_screenshot(self.page, "easy-apply-opened")
            await self._fill_application_form(job)
            if self.test_mode:
                logger.info("Test mode discarded the form without submitting it")
                return "Skip", "Test mode: final submission was not sent"
            logger.info(
                "LinkedIn submission independently confirmed; "
                "awaiting authoritative result publication"
            )
            return "Success", ""

        except UnverifiedAfterSubmitError as e:
            logger.warning(str(e))
            return "Error", str(e)
        except NoInfoException as e:
            logger.warning(f"Could not apply to {job.job_title} at {job.company_name}. Reason: {e}")
            return (
                "Skip",
                f"Could not apply to {job.job_title} at {job.company_name}. Reason: {e}",
            )
        except StopRequested:
            raise
        except Exception as e:
            if self.final_submit_attempted:
                logger.warning(
                    "Final LinkedIn submit was attempted, but confirmation "
                    f"could not complete ({type(e).__name__})"
                )
                return (
                    "Error",
                    "UNVERIFIED_AFTER_SUBMIT: final LinkedIn submit was attempted, "
                    "but independent confirmation could not complete",
                )
            # A dead Playwright driver is not an application failure.
            # Propagate it so the whole browser session can be restarted.
            if self._is_target_closed_error(e):
                if runtime_controller.is_shutdown_requested():
                    logger.info("Easy Apply target closed during intentional shutdown")
                    return "Cancelled", "CANCELLED_BY_SHUTDOWN: Easy Apply page closed"
                logger.warning("Easy Apply target closed unexpectedly; stopping this application")
                raise RuntimeError(
                    "TECHNICAL_FAILURE: target page, context or browser has been closed "
                    f"unexpectedly ({type(e).__name__})"
                ) from e

            tb_str = traceback.format_exc()
            logger.error(f"Failed to apply to job: {job.job_title} at {job.url}, error: {tb_str}")
            await debug_capture(self.page, "job_easy_apply_error")
            await capture_page_screenshot(self.page, "easy-apply-error")
            try:
                await self._save_job_application_process()
            except Exception as e:
                logger.error(f"Failed to save job application process: {e}")
            return "Error", f"Failed to apply to job! Original exception:\nTraceback:\n{tb_str}"

    async def _observe_easy_apply_quota(self, *, force: bool = False) -> str:
        """Refresh quota state only when evidence is fresh or a probe is due."""

        state = easy_apply_quota_state()
        if state.is_blocked() and not (force or state.should_recheck()):
            return EASY_APPLY_QUOTA_BLOCKED
        if state.is_blocked():
            state.mark_recheck()
        try:
            observation = await collect_easy_apply_ui(
                self.page,
                find_elements=find_elements_safely,
            )
            return state.apply_observation(observation)
        except Exception as error:
            logger.warning(
                "EASY_APPLY_QUOTA_OBSERVATION_FAILED | error_type={}",
                type(error).__name__,
            )
            await debug_capture(self.page, "easy_apply_limit_check_error")
            return state.status()

    async def _check_easy_apply_limit(self) -> bool:
        """Return True only for strong LinkedIn quota evidence."""

        logger.debug("Checking Easy Apply quota evidence")
        return await self._observe_easy_apply_quota() == EASY_APPLY_QUOTA_BLOCKED

    async def _find_easy_apply_button(self, job: Job) -> Any:
        """Find Easy Apply button with retries (async)"""
        logger.debug("Searching for 'Easy Apply' button and try to click")
        attempt = 0

        while attempt < 2:
            await self.check_for_premium_redirect(job)

            # Check for Easy Apply quota before searching for a button
            if await self._check_easy_apply_limit():
                logger.warning("Easy Apply quota detected while searching for button")
                return None

            easy_apply_selectors = [
                '//button[contains(@aria-label, "Easy Apply")]',
                '//a[contains(., "Apply")]',
            ]

            easy_apply_buttons = []
            for selector in easy_apply_selectors:
                easy_apply_buttons = await find_elements_safely(self.page, selector, "xpath")
                if easy_apply_buttons:
                    break

            for button in easy_apply_buttons:
                try:
                    if not (await button.is_visible() and await button.is_enabled()):
                        logger.debug("Apply button is not visible or enabled")
                        continue
                    await button.first.click(timeout=1000)
                    return True
                except Exception as e:
                    logger.debug(f"Failed to click easy apply button: {e}")

            await self.check_for_premium_redirect(job)

            if attempt == 0:
                logger.debug("Refreshing page to retry finding 'Easy Apply' button")
                await self.page.reload()
                await async_pause(3, 5)
            attempt += 1

        page_url = self.page.url
        logger.warning(
            f"No clickable 'Easy Apply' button found after 2 attempts. page url: {page_url}"
        )
        return False

    async def _click_continue_applying_button(self) -> None:
        """Click continue applying button if present (async)"""
        logger.debug("Searching for 'Continue Applying' button")
        continue_applying_button = await find_element_safely(
            self.page,
            '//*[contains(., "Continue applying") and (self::button or self::a)]',
            "xpath",
        )
        if continue_applying_button:
            await continue_applying_button.click(timeout=1000)

    async def _fill_application_form(self, job: Job):
        """Fill out application form with loop for multi-step forms (async)"""
        logger.info(f"Filling out application form for job: {job.job_title}")
        self._last_form_fingerprint = None
        self._same_form_state_count = 0
        while True:
            state = await self._capture_easy_apply_form_state(job)
            self._apply_form_progress_guard(state, job)
            self.previous_question_texts = []
            # Fill out application form
            try:
                await self._fill_up(job)
            except NoInfoException:
                if state and self._same_form_state_count == 1:
                    self._force_fresh_question_resolution = True
                    logger.warning(
                        "Easy Apply question resolution failed; re-reading the live DOM "
                        "once without cached answers"
                    )
                    continue
                if state:
                    self._log_stalled_state(state, job, "unresolved_required_question")
                raise
            # Check if execution is paused
            if self.pause_checker:
                await self.pause_checker()
            # Click 'Next' or 'Submit' or 'Confirm' button
            if await self._next_or_submit():
                logger.debug("Application form submitted")
                break

    async def _capture_easy_apply_form_state(self, job: Job) -> dict[str, Any] | None:
        """Capture a PII-safe current-step fingerprint for bounded loop detection."""
        try:
            dialog_query = self.page.locator(
                '[data-testid="dialog-content"]:visible, '
                '.jobs-easy-apply-modal__content:visible, .artdeco-modal__content:visible'
            )
            if inspect.isawaitable(dialog_query):
                close = getattr(dialog_query, "close", None)
                if callable(close):
                    close()
                return None
            dialog = dialog_query.first
            if await dialog.count() == 0:
                return None
            snapshot = await dialog.evaluate(
                r"""root => {
                    const clean = v => (v || '').replace(/\s+/g, ' ').trim();
                    const visible = el => Boolean(el && el.isConnected && el.getClientRects().length);
                    const labelFor = el => {
                        const labelled = clean((el.getAttribute('aria-labelledby') || '')
                          .split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' '));
                        const labels = clean(Array.from(el.labels || []).map(x => x.textContent || '').join(' '));
                        const group = el.closest('fieldset, [data-test-form-element], .fb-dash-form-element');
                        const semantic = clean(group?.querySelector('legend, .fb-dash-form-element__label, p')?.textContent);
                        return labelled || labels || clean(el.getAttribute('aria-label')) || semantic;
                    };
                    const placeholder = value => /^(?:|select(?: an)? option|choose(?: an)? option|please select|empty response|--.*--)$/i.test(clean(value));
                    const controls = [...root.querySelectorAll('input, select, textarea, [role="radio"], [role="checkbox"]')]
                      .filter(visible).map(el => {
                        const type = (el.getAttribute('type') || el.getAttribute('role') || el.tagName || '').toLowerCase();
                        const required = Boolean(el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(labelFor(el)));
                        const ariaInvalid = el.getAttribute('aria-invalid') === 'true';
                        const browserInvalid = Boolean(el.validity && !el.validity.valid);
                        const selectedOption = el.tagName === 'SELECT' ? el.options?.[el.selectedIndex] || null : null;
                        const selectUnanswered = el.tagName === 'SELECT' && (
                          !selectedOption || Boolean(selectedOption.disabled) ||
                          (!clean(el.value) && placeholder(selectedOption.textContent))
                        );
                        const unanswered = required && (
                          (type === 'radio' && !root.querySelector(`input[type="radio"][name="${CSS.escape(el.name || '')}"]:checked`)) ||
                          (type === 'checkbox' && !el.checked) ||
                          selectUnanswered ||
                          (!['radio', 'checkbox'].includes(type) && el.tagName !== 'SELECT' && !clean(el.value))
                        );
                        return {question: labelFor(el), type, required, invalid: ariaInvalid || browserInvalid || unanswered};
                      });
                    let step = 'unknown';
                    for (const el of root.querySelectorAll('[role="progressbar"], [aria-label*="step" i], [aria-label*="page" i]')) {
                      const now = el.getAttribute('aria-valuenow');
                      const max = el.getAttribute('aria-valuemax');
                      const match = `${el.getAttribute('aria-label') || ''} ${el.textContent || ''}`.match(/(?:step|page)?\s*(\d+)\s*(?:of|\/)\s*(\d+)/i);
                      if (now && max) { step = `${now}/${max}`; break; }
                      if (match) { step = `${match[1]}/${match[2]}`; break; }
                    }
                    if (step === 'unknown') {
                      const match = clean(root.textContent).match(/\b(\d+)\s*\/\s*(\d+)\s*pages?\b/i);
                      if (match) step = `${match[1]}/${match[2]}`;
                    }
                    return {step, controls};
                }"""
            )
        except Exception as exc:
            self._reraise_if_target_closed(exc)
            logger.debug("Could not capture Easy Apply progress fingerprint: {}", type(exc).__name__)
            return None
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("controls"), list):
            return None

        controls = []
        categories = []
        for item in snapshot["controls"]:
            if not isinstance(item, dict):
                continue
            question = normalize_question(item.get("question"))
            category = canonical_category(question) or self._question_category_for_log(question, item.get("type"))
            categories.append(category)
            controls.append(
                (
                    question.casefold(),
                    str(item.get("type") or "unknown").casefold(),
                    bool(item.get("required")),
                    bool(item.get("invalid")),
                )
            )
        job_id = job.job_id or self._job_id_from_url(job.url)
        payload = repr((job_id, snapshot.get("step") or "unknown", sorted(controls)))
        structure = repr(
            (job_id, snapshot.get("step") or "unknown", sorted(item[:3] for item in controls))
        )
        return {
            "fingerprint": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "structure_fingerprint": hashlib.sha256(structure.encode("utf-8")).hexdigest(),
            "control_count": len(controls),
            "job_id": job_id or "unknown",
            "step": snapshot.get("step") or "unknown",
            "categories": tuple(sorted(set(categories))),
            "has_invalid": any(item[3] for item in controls),
        }

    @staticmethod
    def _job_id_from_url(url: str | None) -> str | None:
        match = re.search(r"/jobs/view/(\d+)", str(url or ""))
        return match.group(1) if match else None

    @staticmethod
    def _question_category_for_log(question: str, field_type: Any = "") -> str:
        spec = field_spec_from_snapshot(question, {"type": field_type or "text"})
        return spec.answer_type.value

    def _apply_form_progress_guard(self, state: dict[str, Any] | None, job: Job) -> None:
        """Allow one fresh-DOM recovery, then stop an unchanged Easy Apply step."""
        if not state:
            self._force_fresh_question_resolution = False
            return
        fingerprint = state["fingerprint"]
        if fingerprint == self._last_form_fingerprint:
            self._same_form_state_count += 1
        else:
            self._last_form_fingerprint = fingerprint
            self._same_form_state_count = 1
            self._force_fresh_question_resolution = False

        if self._same_form_state_count == 2:
            self._force_fresh_question_resolution = True
            logger.warning(
                "Easy Apply step made no progress; retrying once with a fresh DOM parse "
                "and without cached answers"
            )
            return
        if self._same_form_state_count >= 3:
            reason = "unresolved_required_question" if state.get("has_invalid") else "no_progress"
            category = self._log_stalled_state(state, job, reason)
            raise NoInfoException(
                f"NEEDS_HUMAN: Easy Apply stalled after bounded fresh-DOM recovery "
                f"({reason}; category={category})"
            )

    def _log_stalled_state(self, state: dict[str, Any], job: Job, reason: str) -> str:
        category = next(iter(state.get("categories") or ("UNKNOWN",)), "UNKNOWN")
        logger.error(
            "EASY_APPLY_STALLED | job_id={} | step={} | reason={} | "
            "question_category={}",
            state.get("job_id") or self._job_id_from_url(job.url) or "unknown",
            state.get("step") or "unknown",
            reason,
            category,
        )
        return category

    async def _find_next_or_submit_button(self) -> Any:
        """Return a fresh semantic navigation locator from the active dialog."""
        logger.info("Finding a usable Easy Apply navigation button")
        await self._ensure_target_open()
        dialog_selector = (
            '[data-testid="dialog-content"]:visible, '
            ".jobs-easy-apply-modal__content:visible, "
            ".artdeco-modal__content:visible"
        )
        scope = self.page.locator(dialog_selector).first
        try:
            if await scope.count() == 0 or not await scope.is_visible():
                self._last_navigation_state = {
                    "page": "unknown",
                    "candidates": [],
                }
                return None, None
        except Exception as exc:
            self._reraise_if_target_closed(exc)
            self._last_navigation_state = {"page": "unknown", "candidates": []}
            return None, None

        candidates = await scope.locator("button, [role='button']").evaluate_all(
            """
            els => {
              const supported = new Set(['next', 'continue', 'review', 'submit application']);
              const normalize = value => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              return els.map(el => {
                const textAction = normalize(el.innerText || el.textContent);
                const ariaAction = normalize(el.getAttribute('aria-label'));
                const action = supported.has(textAction)
                  ? textAction
                  : (supported.has(ariaAction) ? ariaAction : '');
                if (!action) return null;
                const style = window.getComputedStyle(el);
                const attached = el.isConnected;
                const visible = attached && el.getClientRects().length > 0
                  && style.visibility !== 'hidden' && style.display !== 'none';
                const ariaDisabled = normalize(el.getAttribute('aria-disabled')) === 'true';
                const disabled = Boolean(el.disabled || el.hasAttribute('disabled') || ariaDisabled);
                return {
                  action,
                  source: supported.has(textAction) ? 'text' : 'aria-label',
                  tag: el.tagName.toLowerCase(),
                  role: normalize(el.getAttribute('role')),
                  attached,
                  visible,
                  enabled: !disabled,
                  aria_disabled: ariaDisabled
                };
              }).filter(Boolean);
            }
            """
        )
        page_progress = await scope.locator(
            '[role="progressbar"], [aria-label*="step" i], [aria-label*="page" i]'
        ).evaluate_all(
            """
            els => {
              for (const el of els) {
                const now = el.getAttribute('aria-valuenow');
                const max = el.getAttribute('aria-valuemax');
                if (now && max) return `${now}/${max}`;
                const label = `${el.getAttribute('aria-label') || ''} ${el.textContent || ''}`;
                const match = label.match(/(?:step|page)?\\s*(\\d+)\\s*(?:of|\\/)\\s*(\\d+)/i);
                if (match) return `${match[1]}/${match[2]}`;
              }
              return 'unknown';
            }
            """
        )
        self._last_navigation_state = {
            "page": page_progress or "unknown",
            "candidates": candidates,
        }

        usable = [
            candidate
            for candidate in candidates
            if candidate.get("attached")
            and candidate.get("visible")
            and candidate.get("enabled")
            and not candidate.get("aria_disabled")
        ]
        # Prefer native buttons. Role-based controls are a bounded fallback.
        selected = next((item for item in usable if item.get("tag") == "button"), None)
        if selected is None:
            selected = next(
                (item for item in usable if item.get("role") == "button"),
                None,
            )
        if selected is None:
            return None, None

        action = selected["action"]
        label = {
            "next": "Next",
            "continue": "Continue",
            "review": "Review",
            "submit application": "Submit application",
        }[action]
        if selected.get("tag") == "button":
            base_selector = 'button:visible:not(:disabled):not([aria-disabled="true" i])'
        else:
            base_selector = (
                '[role="button"]:not(button):visible:not([aria-disabled="true" i])'
            )
        if selected.get("source") == "aria-label":
            locator = scope.locator(
                f'{base_selector}[aria-label="{label}" i]'
            ).first
        else:
            locator = scope.locator(base_selector).filter(
                has_text=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
            ).first

        if not await self._is_usable_navigation_button(locator):
            return None, None
        return locator, action

    async def _is_usable_navigation_button(self, locator: Any) -> bool:
        """Recheck attachment and actionability without exposing field data."""
        try:
            if await locator.count() == 0:
                return False
            if not await locator.evaluate("el => el.isConnected"):
                return False
            if not await locator.is_visible() or not await locator.is_enabled():
                return False
            return (await locator.get_attribute("aria-disabled") or "").casefold() != "true"
        except Exception as exc:
            self._reraise_if_target_closed(exc)
            return False

    async def _wait_for_navigation_dom_stability(self) -> None:
        """Give LinkedIn's bounded re-render a moment to replace stale controls."""
        await asyncio.sleep(0.25)

    @classmethod
    def _is_retryable_navigation_error(cls, error: BaseException) -> bool:
        if isinstance(error, (PlaywrightTimeoutError, TimeoutError)):
            return True
        message = str(error).casefold()
        return any(marker in message for marker in cls._NAVIGATION_RETRY_MARKERS)

    def _log_navigation_state(self, validation_error_count: int) -> None:
        state = self._last_navigation_state or {}
        candidates = state.get("candidates") or []
        labels = [
            f"{item.get('action')}:{item.get('tag') or item.get('role') or 'control'}"
            for item in candidates
        ]
        visible = [item.get("action") for item in candidates if item.get("visible")]
        enabled = [item.get("action") for item in candidates if item.get("enabled")]
        logger.error(
            "NAVIGATION_STATE | "
            f"page={state.get('page', 'unknown')} | "
            f"candidates={labels} | visible={visible} | enabled={enabled} | "
            f"validation_errors={validation_error_count}"
        )

    async def _require_navigation_button(self) -> Tuple[Any, str]:
        """Find a usable button after inspecting and repairing validation state."""
        for attempt in range(2):
            await self._repair_invalid_fields_before_navigation()
            await self._wait_for_navigation_dom_stability()
            try:
                button, action = await self._find_next_or_submit_button()
            except Exception as exc:
                self._reraise_if_target_closed(exc)
                if attempt == 0 and self._is_retryable_navigation_error(exc):
                    logger.warning(
                        "Easy Apply dialog re-rendered during navigation lookup; "
                        f"re-querying once ({type(exc).__name__})"
                    )
                    continue
                raise
            if button is not None and action is not None:
                return button, action
            errors = await self._find_all_form_errors()
            if errors:
                await self._repair_invalid_fields_before_navigation()
            if attempt == 0:
                await self._wait_for_navigation_dom_stability()

        remaining_errors = await self._find_all_form_errors()
        if remaining_errors:
            # The repair helper distinguishes missing candidate facts from
            # automation failures; validation alone does not require a human.
            await self._repair_invalid_fields_before_navigation()
            await self._wait_for_navigation_dom_stability()
            button, action = await self._find_next_or_submit_button()
            if button is not None and action is not None:
                return button, action
            remaining_errors = await self._find_all_form_errors()
            if remaining_errors:
                self._log_navigation_state(len(remaining_errors))
                raise RuntimeError(
                    "TECHNICAL_FAILURE: validation errors remain after bounded correction"
                )
        self._log_navigation_state(len(remaining_errors))
        raise RuntimeError(
            "TECHNICAL_FAILURE: no visible enabled Easy Apply navigation button "
            "after validation inspection and bounded re-query"
        )

    async def _next_or_submit(self) -> bool:
        """Click 'Next' or 'Submit' or 'Review' button"""
        logger.info("Clicking 'Next' or 'Submit' or 'Review' button")
        _, button_text = await self._require_navigation_button()

        if "submit application" in button_text:
            logger.debug("Submit button found, submitting application")
            await self._unfollow_company()
            await async_pause()
            if self.test_mode:
                logger.debug("Test mode is enabled, skipping application form submission")
                await self._discard_application()
                return True
            navigation_complete = await self._check_and_fix_errors(
                button_text,
                final_submit=True,
            )
            if navigation_complete is not True:
                return False
            if await self._verify_linkedin_submission():
                return True
            raise UnverifiedAfterSubmitError(
                "UNVERIFIED_AFTER_SUBMIT: final LinkedIn submit was attempted, "
                "but no credible confirmation was found"
            )
        await self._check_and_fix_errors(button_text)
        return False

    async def _verify_linkedin_submission(self, attempts: int = 8) -> bool:
        """Require LinkedIn-owned confirmation after a final submit click."""
        confirmation_selectors = (
            "[data-test-modal-id='easy-apply-modal-success']",
            "[data-testid='dialog-content']",
            ".artdeco-modal",
            ".jobs-easy-apply-content",
        )
        confirmation_pattern = re.compile(
            r"\b(?:application sent|application submitted|your application was sent|you applied)\b",
            re.I,
        )
        for _ in range(attempts):
            await self._ensure_target_open()
            if await self._mark_already_applied_status():
                logger.info("LinkedIn submission confirmed by Application submitted status")
                return True
            for selector in confirmation_selectors:
                texts = await self.page.locator(selector).evaluate_all(
                    "els => els.filter(e => e.offsetParent !== null)"
                    ".map(e => e.textContent?.replace(/\\s+/g, ' ').trim() || '')"
                )
                if any(confirmation_pattern.search(text or "") for text in texts):
                    logger.info("LinkedIn submission confirmed by visible success receipt")
                    return True
            await async_pause(1, 1.1)
        return False

    async def _unfollow_company(self) -> None:
        """Unfollow company checkbox (async)"""
        try:
            follow_checkbox = await find_element_safely(
                self.page,
                "label[for='follow-company-checkbox']",
                "css",
            )
            if follow_checkbox is None:
                # New LinkedIn SDUI markup drops the static id and renders an empty
                # <label>; the toggle is a role="checkbox" div next to "Follow <Company>
                # to stay up to date..." text, checked by default
                follow_checkbox = await find_element_safely(
                    self.page,
                    "//*[@role='checkbox'][@aria-checked='true']"
                    "[contains(translate(., 'FOLLOW', 'follow'), 'follow')]",
                    "xpath",
                )
            if follow_checkbox:
                await follow_checkbox.click(timeout=1000)

        except Exception as e:
            logger.warning(f"Failed to unfollow company: {e}")
            await debug_capture(self.page, "unfollow_company_error")

    async def _check_and_fix_errors(
        self,
        expected_action: str | None,
        final_submit: bool = False,
    ) -> bool | None:
        """Validate, re-query, and click one semantic navigation action."""
        logger.info("Validating the form before Easy Apply navigation")
        expected = (
            expected_action.casefold()
            if isinstance(expected_action, str)
            and expected_action.casefold() in self._NAVIGATION_ACTIONS
            else None
        )

        for click_attempt in range(2):
            # Never carry a locator across field repairs or DOM stabilization.
            button, current_action = await self._require_navigation_button()
            if expected is not None and current_action != expected:
                logger.info(
                    f"Easy Apply navigation changed from {expected} to "
                    f"{current_action} after re-render; returning control to the form loop"
                )
                return None

            is_submit = current_action == "submit application"
            if final_submit and not is_submit:
                return None
            before_state = (
                await self._capture_easy_apply_form_state(self.current_job)
                if not is_submit and self.current_job is not None
                else None
            )
            if is_submit:
                self.final_submit_attempted = True

            try:
                await button.click(timeout=self._NAVIGATION_CLICK_TIMEOUT_MS)
            except Exception as exc:
                if is_submit:
                    # A final click timeout is ambiguous. Never click Submit a
                    # second time; confirmation remains independently required.
                    raise UnverifiedAfterSubmitError(
                        "UNVERIFIED_AFTER_SUBMIT: final LinkedIn submit click "
                        "did not return a definitive browser result"
                    ) from exc
                self._reraise_if_target_closed(exc)
                if click_attempt == 0 and self._is_retryable_navigation_error(exc):
                    logger.warning(
                        "Easy Apply navigation control re-rendered; "
                        f"re-querying once ({type(exc).__name__})"
                    )
                    await self._wait_for_navigation_dom_stability()
                    continue
                raise RuntimeError(
                    "TECHNICAL_FAILURE: Easy Apply navigation click failed after "
                    f"bounded re-query ({type(exc).__name__})"
                ) from exc

            await self._wait_for_navigation_dom_stability()
            if before_state:
                after_state = await self._capture_easy_apply_form_state(self.current_job)
                before_structure = before_state.get("structure_fingerprint")
                after_structure = (after_state or {}).get("structure_fingerprint")
                if before_structure and after_structure and before_structure != after_structure:
                    # Blank required controls on a newly opened step have not
                    # been filled yet. Give them to the normal form loop first.
                    # A changed validity flag alone is not a step transition.
                    logger.info(
                        "EASY_APPLY_STEP_TRANSITION | from_step={} | to_step={} | "
                        "controls={} | has_invalid={} | categories={}",
                        before_state.get("step", "unknown"),
                        after_state.get("step", "unknown"),
                        after_state.get("control_count", 0),
                        bool(after_state.get("has_invalid")),
                        ",".join(after_state.get("categories") or ("UNKNOWN",)),
                    )
                    return True
            validation_errors = await self._find_all_form_errors()
            if validation_errors:
                if is_submit:
                    # Visible validation means this click did not submit.
                    self.final_submit_attempted = False
                logger.info(
                    f"Easy Apply returned {len(validation_errors)} validation error(s); "
                    "repairing before the form loop re-queries navigation"
                )
                await self._repair_invalid_fields_before_navigation()
                await self._wait_for_navigation_dom_stability()
                return None
            return True

        raise RuntimeError(
            "TECHNICAL_FAILURE: Easy Apply navigation retry budget exhausted"
        )

    async def _discard_application(self) -> None:
        """Discard application (async)"""
        logger.info("Discarding application")
        try:
            dismiss = await find_element_safely(
                self.page, "//*[contains(@class, 'artdeco-modal__dismiss')]", "xpath"
            )
            if dismiss:
                await dismiss.click(timeout=1000)
                await async_pause(2, 3)
            confirm_buttons = self.page.locator(
                "xpath=//*[contains(@class, 'artdeco-modal__confirm-dialog-btn')]"
            )
            if await confirm_buttons.count() > 0:
                await confirm_buttons.first.click(timeout=1000)
                await async_pause(2, 3)
        except Exception as e:
            logger.warning(f"Failed to discard application: {e}")
            await debug_capture(self.page, "discard_application_error")

    async def _save_job_application_process(self) -> None:
        """Save job application process (async)"""
        logger.info("Application not completed. Saving job to My Jobs, In Progess section")
        try:
            dismiss = await find_element_safely(
                self.page, "//*[contains(@class, 'artdeco-modal__dismiss')]", "xpath"
            )
            if dismiss:
                await dismiss.click(timeout=1000)
                await async_pause(2, 3)
            confirm_buttons = self.page.locator(
                "xpath=//*[contains(@class, 'artdeco-modal__confirm-dialog-btn')]"
            )
            if await confirm_buttons.count() > 1:
                await confirm_buttons.nth(1).click(timeout=1000)
                await async_pause(2, 3)
        except Exception as e:
            logger.error(f"Failed to save application process: {e}")
            await debug_capture(self.page, "save_application_error")

    async def _widen_to_question_container(self, fieldset: Any) -> Any:
        """Expand a radio/checkbox <fieldset> to include its question text.

        LinkedIn's new SDUI markup renders the question label as a sibling of the
        <fieldset>, not a descendant, so the fieldset's own text_content() only
        contains the option labels. Climb ancestors until one actually adds text
        beyond the fieldset's own content (i.e. contains the question label).
        """
        container = fieldset
        try:
            own_text = (await container.text_content() or "").strip()
            for _ in range(4):
                parent = container.locator("xpath=..").first
                if await parent.count() == 0:
                    break
                parent_text = (await parent.text_content() or "").strip()
                if parent_text != own_text:
                    return parent
                container = parent
        except Exception as e:
            logger.debug(f"Failed widening fieldset to question container: {e}")
        return container

    async def _widen_to_text_input_container(self, text_field: Any) -> Any:
        """Expand a standalone text/textarea input to include its question text.

        LinkedIn's newer SDUI markup renders some text questions with the question
        text in a sibling <p> instead of a <label>, so climb ancestors until one
        contains a <p> element (the question text), capped to avoid over-widening.
        """
        container = text_field
        try:
            for _ in range(5):
                parent = container.locator("xpath=..").first
                if await parent.count() == 0:
                    break
                container = parent
                if await container.locator("p").count() > 0:
                    break
        except Exception as e:
            logger.debug(f"Failed widening text input to question container: {e}")
        return container

    async def _extract_semantic_question(self, control: Any, section: Any) -> str:
        """Read an accessible label before considering nearby container text."""
        try:
            raw = await control.evaluate(
                r"""el => {
                    const clean = value => (value || '').replace(/\s+/g, ' ').trim();
                    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
                    const labelled = clean(ids.map(id => document.getElementById(id)?.textContent || '').join(' '));
                    const nativeLabels = clean(Array.from(el.labels || []).map(item => item.textContent || '').join(' '));
                    const fieldset = el.closest('fieldset');
                    const legend = clean(fieldset?.querySelector(':scope > legend')?.textContent);
                    const aria = clean(el.getAttribute('aria-label'));
                    const container = el.closest('.fb-dash-form-element, [data-test-form-element], [data-test-single-line-text-form-component], [data-test-multiline-text-form-component]');
                    const nearby = clean(container?.querySelector('.fb-dash-form-element__label, [data-test-form-element-title], p')?.textContent);
                    return labelled || nativeLabels || legend || aria || nearby;
                }"""
            )
            if isinstance(raw, str) and raw.strip():
                return normalize_question(raw)
        except Exception as exc:
            self._reraise_if_target_closed(exc)

        try:
            label = section.locator("label, legend, .fb-dash-form-element__label").first
            if await label.count() > 0:
                return normalize_question(await label.text_content() or "")
        except Exception as exc:
            self._reraise_if_target_closed(exc)
        return ""

    async def _fill_up(self, job: Job) -> None:
        """Fill up form sections (async)"""
        logger.info(f"Filling up form sections for job: {job.job_title}")

        try:
            # Wait for the Easy Apply modal content to be present with explicit wait
            modal_content = None
            logger.debug("Waiting for Easy Apply modal to appear...")

            # Try to wait for the modal to be visible
            try:
                # Wait up to 10 seconds for the modal to appear
                await self.page.wait_for_selector(
                    '[data-testid="dialog-content"]', state="visible", timeout=10000
                )
                logger.debug("Modal selector found via wait_for_selector")
            except Exception as e:
                self._reraise_if_target_closed(e)
                logger.warning(f"wait_for_selector failed: {e}")

            # Try multiple selectors to find the modal content
            modal_selectors = [
                '[data-testid="dialog-content"]',  # New SDUI modal container
                ".jobs-easy-apply-modal__content",  # CSS selector
                ".artdeco-modal__content",  # Fallback CSS
                "//*[contains(@class, 'jobs-easy-apply-modal__content')]",  # XPath
            ]

            for selector in modal_selectors:
                selector_type = (
                    "css" if selector.startswith(".") or selector.startswith("[") else "xpath"
                )
                modal_content = await find_element_safely(self.page, selector, selector_type)
                if modal_content is not None:
                    logger.debug(f"Easy Apply modal content found with selector: {selector}")
                    break

            if modal_content is None:
                logger.error("Easy Apply modal content not found on the page with any selector")
                if await self._is_already_applied():
                    raise NoInfoException("Already applied to this job")
                raise NoInfoException("Easy Apply dialog did not open")

            logger.debug("Easy Apply modal content found successfully")

            # Track processed file inputs to avoid duplicate processing
            processed_file_inputs = set()

            # Find all form elements using the correct selectors
            form_elements = await modal_content.locator(".fb-dash-form-element").all()
            logger.debug(f"Found {len(form_elements)} form elements")

            if not form_elements:
                # Fallback to the old selector if new one doesn't work
                form_elements = await modal_content.locator(
                    "xpath=.//*[contains(@class, 'jobs-easy-apply-form-section__group')]"
                ).all()
                logger.debug(
                    f"Fallback: Found {len(form_elements)} form elements with old selector"
                )

            if not form_elements:
                # LinkedIn's newer SDUI markup uses hashed, non-semantic CSS classes,
                # so fall back to structural detection. Radio/checkbox groups (e.g. the
                # resume picker) are wrapped in a <fieldset> and must stay one section so
                # all options are visible together; every other <label> not inside such a
                # fieldset is treated as its own single-field section via its parent element
                fieldsets = await modal_content.locator("fieldset").all()
                form_elements = [await self._widen_to_question_container(fs) for fs in fieldsets]

                labels = await modal_content.locator("label").all()
                labeled_input_ids: set = set()
                for label in labels:
                    try:
                        in_fieldset = await label.locator("xpath=ancestor::fieldset").count() > 0
                    except Exception:
                        in_fieldset = False
                    if in_fieldset:
                        continue
                    label_container = label.locator("xpath=..").first
                    form_elements.append(label_container)
                    try:
                        for inp in await label_container.locator(
                            "input[type='text'], input[type='tel'], input[type='number'], "
                            "input[type='email'], textarea"
                        ).all():
                            input_id = await inp.get_attribute("id")
                            if input_id:
                                labeled_input_ids.add(input_id)
                    except Exception:
                        pass

                # LinkedIn's newer SDUI text-question markup has no <label> at all -
                # the question text lives in a sibling <p>, associated to the <input>
                # only via aria-label/aria-describedby. Pick up any text/textarea
                # input still missed by the fieldset and label passes above.
                orphan_inputs = await modal_content.locator(
                    "input[type='text'], input[type='tel'], input[type='number'], "
                    "input[type='email'], textarea"
                ).all()
                for inp in orphan_inputs:
                    try:
                        if await inp.locator("xpath=ancestor::fieldset").count() > 0:
                            continue
                    except Exception:
                        pass
                    input_id = await inp.get_attribute("id")
                    if input_id and input_id in labeled_input_ids:
                        continue
                    form_elements.append(await self._widen_to_text_input_container(inp))

                logger.debug(
                    f"Structural fallback: Found {len(form_elements)} form elements "
                    f"({len(fieldsets)} fieldsets)"
                )

            # Process regular form elements
            for element in form_elements:
                try:
                    await self._process_form_element(element, job, processed_file_inputs)
                except NoInfoException:
                    raise

            # Also look for upload sections separately (they may not be in fb-dash-form-element)
            upload_sections = await modal_content.locator(
                ".js-jobs-document-upload__container"
            ).all()
            logger.debug(f"Found {len(upload_sections)} upload sections")

            for upload_section in upload_sections:
                logger.debug("Processing upload section")
                await self._handle_upload_fields(upload_section, job, processed_file_inputs)

            # Additional fallback: look for any file inputs that might be missed
            file_inputs = await modal_content.locator("input[type='file']").all()
            logger.debug(f"Found {len(file_inputs)} file inputs as additional check")

            for file_input in file_inputs:
                # Check if this file input was already processed
                file_input_id = await file_input.get_attribute("id") or str(id(file_input))
                if file_input_id not in processed_file_inputs:
                    logger.debug("Processing additional file input")
                    parent_container = file_input.locator("xpath=../..").first
                    await self._handle_upload_fields(parent_container, job, processed_file_inputs)
                    processed_file_inputs.add(file_input_id)
        except NoInfoException:
            raise
        except Exception as exc:
            self._reraise_if_target_closed(exc)
            tb_str = traceback.format_exc()
            logger.error(f"Failed to find form elements: {tb_str}")
            await debug_capture(self.page, "fill_up_form_error")
            # Don't re-raise the exception, just log it and continue
            logger.warning("Continuing without filling form elements due to error")

    async def _process_form_element(
        self, element: Any, job: Job, processed_file_inputs: set
    ) -> None:
        """Process form element (async)"""
        logger.debug("Processing form element")
        if await self._is_upload_field(element):
            await self._handle_upload_fields(element, job, processed_file_inputs)
        else:
            await self._process_form_section(element)

    async def _is_already_applied(self) -> bool:
        """Detect LinkedIn's application status panel for already-submitted jobs."""
        return await self._mark_already_applied_status()

    async def _mark_already_applied_status(self) -> bool:
        status = await self._get_already_applied_status()
        if not status:
            return False
        self.already_applied_at = status.get("applied_at")
        self.already_applied_at_text = status.get("applied_at_text")
        if self.already_applied_at_text:
            logger.info(
                f"Detected already-submitted LinkedIn application status: {self.already_applied_at_text}"
            )
        else:
            logger.info("Detected already-submitted LinkedIn application status")
        return True

    async def _get_already_applied_status(self) -> Optional[Dict[str, Optional[str]]]:
        """Return LinkedIn's application status metadata when this job was already submitted."""
        selectors = [
            "//*[normalize-space()='Application status']/following::*[normalize-space()='Application submitted'][1]",
            "//*[normalize-space()='Application submitted']",
        ]
        for selector in selectors:
            try:
                submitted_element = await find_element_safely(
                    self.page, selector, "xpath", timeout=1000
                )
                if submitted_element:
                    applied_at_text = await self._extract_already_applied_at_text(submitted_element)
                    return {
                        "applied_at": self._parse_already_applied_at(applied_at_text),
                        "applied_at_text": applied_at_text,
                    }
            except Exception as e:
                logger.debug(f"Failed checking already-applied selector {selector}: {e}")
        return None

    async def _extract_already_applied_at_text(self, submitted_element: Any) -> Optional[str]:
        """Extract the date/relative time text next to LinkedIn's submitted status."""
        selectors = [
            "xpath=following-sibling::*[1]",
            "xpath=following::*[normalize-space()][1]",
        ]
        for selector in selectors:
            try:
                date_element = submitted_element.locator(selector).first
                if await date_element.count() > 0:
                    text = (await date_element.inner_text()).strip()
                    if text and text.lower() != "view resume":
                        return text
            except Exception as e:
                logger.debug(f"Failed extracting already-applied date with {selector}: {e}")
        return None

    def _parse_already_applied_at(self, applied_at_text: Optional[str]) -> Optional[str]:
        """Convert common LinkedIn relative submitted times into an ISO timestamp."""
        if not applied_at_text:
            return None

        normalized = applied_at_text.strip().lower()
        now = datetime.now()
        if normalized in {"just now", "moments ago", "a moment ago"}:
            return now.isoformat(timespec="seconds")

        match = re.search(
            r"(?:about\s+)?(?:a|an|1|(?P<count>\d+))\s+"
            r"(?P<unit>minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)\s+ago",
            normalized,
        )
        if not match:
            return None

        count = int(match.group("count") or 1)
        unit = match.group("unit")
        if unit.startswith("minute"):
            delta = timedelta(minutes=count)
        elif unit.startswith("hour"):
            delta = timedelta(hours=count)
        elif unit.startswith("day"):
            delta = timedelta(days=count)
        elif unit.startswith("week"):
            delta = timedelta(weeks=count)
        elif unit.startswith("month"):
            delta = timedelta(days=30 * count)
        else:
            delta = timedelta(days=365 * count)
        return (now - delta).isoformat(timespec="seconds")

    async def _is_upload_field(self, element: Any) -> bool:
        """Check if element is upload field (async)"""
        # Check for file input elements
        file_inputs = await element.locator("xpath=.//input[@type='file']").all()

        # Also check for LinkedIn-specific upload containers
        upload_containers = await element.locator(".js-jobs-document-upload__container").all()
        upload_buttons = await element.locator(".jobs-document-upload__upload-button").all()

        is_upload = bool(file_inputs or upload_containers or upload_buttons)
        logger.debug(
            f"Element is upload field: {is_upload} (file_inputs: {len(file_inputs)}, containers: {len(upload_containers)}, buttons: {len(upload_buttons)})"
        )
        return is_upload

    async def _handle_upload_fields(
        self, element: Any, job: Job, processed_file_inputs: set
    ) -> None:
        """Handle file upload fields (async)"""
        logger.info("Handling upload fields")

        try:
            show_more_button = await find_element_safely(
                self.page,
                "//button[contains(@aria-label, 'Show more resumes')]",
                "xpath",
            )
            if show_more_button:
                await show_more_button.click(timeout=1000)
                logger.debug("Clicked 'Show more resumes' button")
        except Exception:
            logger.debug("'Show more resumes' button not found, continuing...")

        # First try to find file inputs within the specific element
        file_upload_elements = await element.locator("xpath=.//input[@type='file']").all()

        # If no file inputs found in the element, fall back to global search
        if not file_upload_elements:
            logger.debug("No file inputs found in element, searching globally")
            file_upload_elements = await self.page.locator("xpath=//input[@type='file']").all()

        logger.debug(f"Found {len(file_upload_elements)} file upload elements")

        for upload_element in file_upload_elements:
            try:
                # Check if this file input was already processed
                file_input_id = await upload_element.get_attribute("id") or str(id(upload_element))
                if file_input_id in processed_file_inputs:
                    logger.debug(f"File input {file_input_id} already processed, skipping")
                    continue

                # Get the parent container to determine what type of upload this is
                parent = upload_element.locator("xpath=..").first
                container_text = (await parent.text_content() or "").lower()

                # Also check the label text if available
                # try:
                #     input_id = await upload_element.get_attribute("id") or ""
                #     if input_id:
                #         label_text = (
                #             await self.page.locator(
                #                 f"xpath=//label[@for='{input_id}']"
                #             ).first.text_content()
                #             or ""
                #         ).lower()
                #         container_text += " " + label_text
                # except Exception:
                #     pass

                # Make the hidden input visible for uploading
                try:
                    await upload_element.evaluate("el => el.classList.remove('hidden')")
                except Exception:
                    pass

                logger.debug(f"Processing upload field with context: {container_text}")

                # Mark this file input as processed before generating files
                processed_file_inputs.add(file_input_id)

                accept_types = (await upload_element.get_attribute("accept") or "").lower()

                # output = self.gpt_answerer.resume_or_cover(container_text)
                if "image/" in accept_types or "photo" in container_text:
                    logger.info("Uploading photo")
                    await self._create_and_upload_photo(upload_element, job)
                elif "resume" in container_text:
                    logger.info("Uploading resume")
                    if (
                        self.resume_generator_manager is not None
                        and getattr(self.resume_generator_manager, "selected_style", None)
                        is not None
                    ):
                        await self._create_and_upload_resume(upload_element, job)
                    elif self.ready_made_resume_path is not None:
                        logger.info(
                            "Resume generator is not ready; falling back to ready-made resume"
                        )
                        await self._create_and_upload_resume(upload_element, job)
                    else:
                        raise NoInfoException(
                            "No resume generator style selected and no ready-made resume configured"
                        )
                elif "cover" in container_text:
                    logger.info("Uploading cover letter")
                    await self._create_and_upload_cover_letter(upload_element, job)

            except Exception as e:
                logger.warning(f"Failed to process upload element: {e}")
                await debug_capture(self.page, "upload_element_error")
                continue

        logger.debug("Finished handling upload fields")

    async def _create_and_upload_photo(self, element: Any, job: Job) -> None:
        """Upload a configured profile photo or fall back to the visible LinkedIn avatar."""
        allowed_extensions = {".jpg", ".jpeg", ".png", ".gif"}
        max_file_size = 2 * 1024 * 1024

        if self.ready_made_photo_path is not None:
            photo_path = self.ready_made_photo_path.resolve()
            if photo_path.suffix.lower() not in allowed_extensions:
                raise ValueError(
                    "Photo file format is not allowed. Only JPG, JPEG, PNG, and GIF formats are supported."
                )
            if photo_path.stat().st_size > max_file_size:
                raise ValueError("Photo file size exceeds the maximum limit of 2 MB.")

            abs_path = os.path.abspath(str(photo_path))
            await element.set_input_files(abs_path)
            await async_pause(1, 2)
            logger.info("Photo uploaded from configured source")
            return

        os.makedirs(self.generated_photo_dir, exist_ok=True)

        image_selectors = [
            ".jobs-easy-apply-modal .artdeco-entity-lockup__image--type-circle img",
            ".jobs-easy-apply-modal img[src*='profile-displayphoto']",
            ".jobs-easy-apply-modal img[title][src]",
        ]

        image_src = ""
        for selector in image_selectors:
            image = await find_element_safely(self.page, selector, "css selector")
            if not image:
                continue
            image_src = (await image.get_attribute("src") or "").strip()
            if image_src:
                break

        if not image_src:
            raise ValueError("Could not locate a profile photo source for the upload field")

        data_url = await self.page.evaluate(
            """async (src) => {
                const response = await fetch(src, { credentials: 'include' });
                if (!response.ok) {
                    throw new Error(`Photo fetch failed with status ${response.status}`);
                }
                const blob = await response.blob();
                return await new Promise((resolve, reject) => {
                    const reader = new FileReader();
                    reader.onloadend = () => resolve(reader.result);
                    reader.onerror = () => reject(reader.error || new Error('Photo read failed'));
                    reader.readAsDataURL(blob);
                });
            }""",
            image_src,
        )

        if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
            raise ValueError("Profile photo fetch did not return an image data URL")

        header, encoded = data_url.split(",", 1)
        mime_type = header.split(";", 1)[0].split(":", 1)[1].lower()
        extension_map = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
        }
        extension = extension_map.get(mime_type)
        if extension is None:
            raise ValueError(f"Unsupported photo MIME type for upload: {mime_type}")

        file_path = (
            self.generated_photo_dir / f"PHOTO_{job.company_name}_{job.job_title}{extension}"
        )
        file_bytes = base64.b64decode(encoded)
        if len(file_bytes) > max_file_size:
            raise ValueError("Profile photo exceeds LinkedIn 2 MB upload limit")

        with open(file_path, "wb") as file_handle:
            file_handle.write(file_bytes)

        await element.set_input_files(os.path.abspath(str(file_path)))
        await async_pause(1, 2)
        logger.info("Photo uploaded from generated file")

    async def _detect_already_selected_resume(self, parent: Any) -> bool:
        """Detect if the is already selected resume in Easy Apply form"""
        already_selected = False
        try:
            sel = ".jobs-document-upload-redesign-card__toggle-label"
            texts = await parent.locator(sel).evaluate_all(
                "els => els.map(e => e.textContent || '')"
            )
            if not texts:
                texts = await self.page.locator(sel).evaluate_all(
                    "els => els.map(e => e.textContent || '')"
                )
            for lbl_text in texts:
                lbl_text = lbl_text.strip()
                st = sanitize_text(lbl_text)
                if st.startswith("deselect") and st.endswith(".pdf"):
                    already_selected = True
                    logger.info(
                        f"Resume already selected via toggle label, skipping upload: {lbl_text}"
                    )
                    break
        except Exception:
            pass
        if already_selected:
            return True
        return False

    async def _create_and_upload_cover_letter(self, element: Any, job: Job) -> None:
        logger.info("Starting the process of creating and uploading cover letter.")

        cover_letter_text = self.gpt_answerer.write_cover_letter()
        cover_letter_text = self.resume_anonymizer.deanonymize_text(cover_letter_text)

        try:
            if not os.path.exists(self.generated_cover_letter_dir):
                logger.debug("Creating generated cover-letter directory")
            os.makedirs(self.generated_cover_letter_dir, exist_ok=True)
        except Exception as e:
            logger.error(
                f"Failed to create directory: {self.generated_cover_letter_dir}. Error: {e}"
            )
            raise

        while True:
            try:
                file_path_pdf = os.path.join(
                    self.generated_cover_letter_dir,
                    f"Cover_Letter_{job.company_name}_{job.job_title}.pdf",
                )
                logger.debug("Generated cover-letter file prepared")

                c = canvas.Canvas(file_path_pdf, pagesize=A4)
                page_width, page_height = A4
                text_object = c.beginText(50, page_height - 50)
                text_object.setFont("Helvetica", 12)

                max_width = page_width - 100
                bottom_margin = 50

                def split_text_by_width(text, font, font_size, max_width):
                    wrapped_lines = []
                    for line in text.splitlines():
                        if stringWidth(line, font, font_size) > max_width:
                            words = line.split()
                            new_line = ""
                            for word in words:
                                if stringWidth(new_line + word + " ", font, font_size) <= max_width:
                                    new_line += word + " "
                                else:
                                    wrapped_lines.append(new_line.strip())
                                    new_line = word + " "
                            wrapped_lines.append(new_line.strip())
                        else:
                            wrapped_lines.append(line)
                    return wrapped_lines

                lines = split_text_by_width(cover_letter_text, "Helvetica", 12, max_width)

                for line in lines:
                    text_height = text_object.getY()
                    if text_height > bottom_margin:
                        text_object.textLine(line)
                    else:
                        c.drawText(text_object)
                        c.showPage()
                        text_object = c.beginText(50, page_height - 50)
                        text_object.setFont("Helvetica", 12)
                        text_object.textLine(line)

                c.drawText(text_object)
                c.save()
                logger.info("Cover letter successfully generated and saved")

                break
            except Exception as e:
                logger.error(f"Failed to generate cover letter: {e}")
                tb_str = traceback.format_exc()
                logger.error(f"Traceback: {tb_str}")
                raise

        file_size = os.path.getsize(file_path_pdf)
        max_file_size = 2 * 1024 * 1024  # 2 MB
        logger.debug(f"Cover letter file size: {file_size} bytes")
        if file_size > max_file_size:
            logger.error(f"Cover letter file size exceeds 2 MB: {file_size} bytes")
            raise ValueError("Cover letter file size exceeds the maximum limit of 2 MB.")

        allowed_extensions = {".pdf", ".doc", ".docx"}
        file_extension = os.path.splitext(file_path_pdf)[1].lower()
        logger.debug(f"Cover letter file extension: {file_extension}")
        if file_extension not in allowed_extensions:
            logger.error(f"Invalid cover letter file format: {file_extension}")
            raise ValueError(
                "Cover letter file format is not allowed. Only PDF, DOC, and DOCX formats are supported."
            )

        try:
            logger.info("Uploading generated cover letter")
            await element.set_input_files(os.path.abspath(file_path_pdf))
            await async_pause(1, 2)
            logger.info("Cover letter created and uploaded successfully")
        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Cover letter upload failed: {tb_str}")
            await debug_capture(self.page, "cover_letter_upload_error")
            raise Exception(f"Upload failed: \nTraceback:\n{tb_str}")

    async def _handle_terms_of_service(self, element: Any) -> bool:
        """Handle terms of service checkbox (async)"""
        try:
            # Check if element is checkbox to prevent false ToS processing on non-checkbox elements
            checkboxes = await element.locator("input[type='checkbox']").all()
            if not checkboxes:
                return False
            checkbox_text = (
                await element.locator("xpath=.//label").first.text_content() or ""
            ).lower()
        except Exception:
            return False
        if checkbox_text:
            if any(
                term in checkbox_text
                for term in [
                    "terms of service",
                    "privacy policy",
                    "terms of use",
                    "confirm",
                    "agree",
                    "accept",
                ]
            ):
                label_el = element.locator("xpath=.//label").first
                await label_el.click(timeout=1000)
                logger.debug("Clicked terms of service checkbox/radio")
                return True
        return False

    async def _find_and_handle_checkbox_question(self, section: Any) -> bool:
        """Handle checkbox questions that are not terms of service (async)"""
        logger.debug("Searching for checkbox questions in the section.")

        # Look for checkboxes in the new LinkedIn form structure
        checkboxes = {}

        # Try different selectors for checkboxes
        checkbox_selectors = [
            "input[type='checkbox']",
            ".fb-form-element__checkbox",
            # "[data-test-text-selectable-option__input]",
            "[data-test-checkbox-form-component] input[type='checkbox']",
        ]

        for selector in checkbox_selectors:
            found_checkboxes = await find_elements_safely(section, selector, "css selector")
            for checkbox in found_checkboxes:
                checkbox_id = await checkbox.get_attribute("id")
                if checkbox_id and checkbox_id not in checkboxes:
                    checkboxes[checkbox_id] = checkbox

        checkboxes = list(checkboxes.values())

        if checkboxes:
            logger.debug(f"Found {len(checkboxes)} checkboxes")

            # Extract question text from the section
            question_text = ""
            try:
                # Look for question text in various places
                question_selectors = [
                    "legend",
                    ".fb-dash-form-element__label",
                    "[data-test-checkbox-form-title]",
                    ".jobs-easy-apply-form-section__group-title",
                    # New LinkedIn SDUI markup has no legend/title element; the question
                    # text is a plain <p> preceding the <fieldset> in the widened section,
                    # so it's always the first <p> in document order.
                    "p",
                ]

                for selector in question_selectors:
                    question_element = await find_element_safely(section, selector, "css selector")
                    if question_element:
                        question_text = (await question_element.text_content() or "").strip()
                        # Clean up the question text
                        question_text = self._deduplicate_question_text(question_text)
                        question_list = []
                        for question in question_text.split("\n"):
                            question_text = self._deduplicate_question_text(question.strip())
                            if question_text:
                                question_list.append(question_text)
                        question_text = "\n".join(question_list)
                        if question_text and "required" not in question_text.lower():
                            break

                # If no question found, try to get it from the section text
                if not question_text:
                    section_text = (await section.text_content() or "").strip()
                    # Extract meaningful text, removing checkbox labels
                    lines = section_text.split("\n")
                    for line in lines:
                        line = line.strip()
                        if line and not any(
                            opt in line.lower()
                            for opt in ["confirmed", "agree", "accept", "required"]
                        ):
                            question_text = line
                            break

                logger.debug(f"Extracted question text: '{question_text}'")
                self.previous_question_texts.append(question_text)

            except Exception as e:
                logger.warning(f"Failed to extract question text: {e}")
                question_text = ""

            # Extract checkbox options
            checkbox_options = []
            checkbox_data = []  # Store (checkbox, label_text) pairs

            for checkbox in checkboxes:
                try:
                    # Get the associated label
                    checkbox_id = await checkbox.get_attribute("id")
                    label_text = ""

                    if checkbox_id:
                        # Look for label with matching 'for' attribute
                        label = section.locator(f"label[for='{checkbox_id}']").first
                        if label:
                            label_text = (await label.text_content() or "").strip()

                    # If no label found, try to get text from parent elements
                    if not label_text:
                        # Look for text in the same container as the checkbox
                        parent = checkbox.locator("xpath=..").first
                        if parent:
                            parent_text = (await parent.text_content() or "").strip()
                            # Extract text that's not the question
                            if parent_text and parent_text != question_text:
                                label_text = parent_text

                    if not label_text:
                        # New LinkedIn SDUI markup renders an empty <label> as a click
                        # target; the visible option text instead lives on a sibling <p>
                        # under the ancestor role="checkbox" wrapper
                        role_ancestor = checkbox.locator(
                            "xpath=ancestor::*[@role='checkbox'][1]"
                        ).first
                        if await role_ancestor.count() > 0:
                            role_text = (await role_ancestor.text_content() or "").strip()
                            if role_text and role_text != question_text:
                                label_text = role_text

                    if label_text:
                        checkbox_options.append(label_text)
                        checkbox_data.append((checkbox, label_text))
                        logger.debug(f"Checkbox option: '{redact_text(label_text)}'")

                except Exception as e:
                    logger.warning(f"Failed to extract checkbox option: {e}")
                    continue

            if not checkbox_options:
                logger.debug("No checkbox options found, skipping")
                return False

            # Use LLM to select which checkboxes to check
            try:
                logger.info(
                    "Asking LLM to select checkboxes for question: "
                    f"{redact_text(question_text)}"
                )
                logger.info(f"Available checkbox option count: {len(checkbox_options)}")

                # Look for existing answer if it's not a cover letter field
                existing_answer = None
                current_question_sanitized = sanitize_text(question_text)
                for item in self.all_questions:
                    if (
                        item.question == current_question_sanitized
                        and item.question_type == "checkbox"
                    ):
                        existing_answer = item.answer
                        logger.debug("Found cached checkbox answer (value omitted)")
                        break

                if existing_answer:
                    selected_options = existing_answer
                else:
                    selected_options = self.gpt_answerer.select_many_answers_from_options(
                        question_text, checkbox_options, self.previous_question_texts[:-1]
                    )
                    if not any(self._is_no_info_answer(s) for s in selected_options):
                        self._save_questions(
                            Question(
                                question_type="checkbox",
                                question=question_text,
                                answer=selected_options,
                            )
                        )

                logger.info(f"LLM selected {len(selected_options)} checkbox option(s)")

                # Check the selected checkboxes
                for checkbox, label_text in checkbox_data:
                    try:
                        # Check if this option was selected by LLM
                        if any(
                            selected in label_text.lower() or label_text.lower() in selected.lower()
                            for selected in selected_options
                            if not self._is_no_info_answer(selected)
                        ):
                            if not await checkbox.is_checked():
                                logger.info(
                                    f"Checking checkbox: {redact_text(label_text)}"
                                )
                                await self._click_checkbox_safely(checkbox, section)
                                logger.debug(f"Clicked checkbox: {label_text}")
                            else:
                                logger.debug(f"Checkbox already checked: {label_text}")
                        else:
                            logger.debug(f"Checkbox not selected by LLM: {label_text}")

                    except Exception as e:
                        logger.warning(f"Failed to process checkbox '{label_text}': {e}")
                        continue

                return True

            except Exception as e:
                logger.error(f"Failed to use LLM for checkbox selection: {e}")
                # Fallback: check confirmation checkboxes only
                for checkbox, label_text in checkbox_data:
                    try:
                        if any(
                            confirm_word in label_text.lower()
                            for confirm_word in ["confirmed", "confirm", "agree", "accept"]
                        ):
                            if not await checkbox.is_checked():
                                logger.info(
                                    f"Fallback: Checking confirmation checkbox: {label_text}"
                                )
                                await self._click_checkbox_safely(checkbox, section)
                                logger.debug(f"Clicked confirmation checkbox: {label_text}")
                    except Exception as e:
                        logger.warning(f"Failed to process fallback checkbox '{label_text}': {e}")
                        continue

                return True

        return False

    async def _click_checkbox_safely(self, checkbox: Any, section: Any) -> None:
        """Safely click a checkbox by trying the label first, then the checkbox itself (async)"""
        try:
            # First try to click the associated label
            checkbox_id = await checkbox.get_attribute("id")
            if checkbox_id:
                label = section.locator(f"label[for='{checkbox_id}']").first
                if label:
                    logger.debug("Clicking checkbox via label")
                    await label.click(timeout=1000)
                    return

            # If no label found or label click failed, try clicking the checkbox directly
            logger.debug("Clicking checkbox directly")
            await checkbox.click(timeout=1000)

        except Exception as e:
            logger.warning(f"Failed to click checkbox safely: {e}")
            # New LinkedIn SDUI markup draws the visible, clickable checkbox on the
            # ancestor role="checkbox" wrapper; the native <input>/<label> are visually
            # hidden and fail Playwright's actionability check, so try that next.
            try:
                role_ancestor = checkbox.locator("xpath=ancestor::*[@role='checkbox'][1]").first
                if await role_ancestor.count() > 0:
                    logger.debug("Clicking checkbox via role='checkbox' ancestor")
                    await role_ancestor.click(timeout=1000)
                    return
            except Exception as e2:
                logger.warning(f"Failed to click checkbox via role ancestor: {e2}")

            # Final fallback: try clicking the checkbox directly
            try:
                await checkbox.click(timeout=1000)
            except Exception as e3:
                logger.error(f"All checkbox click attempts failed: {e3}")
                await debug_capture(self.page, "checkbox_click_error")

    async def _is_resume_picker_radiogroup(self, section: Any) -> bool:
        """Detect LinkedIn's "select a resume" radiogroup among generic radio questions.

        It lists every previously uploaded resume (filename + upload date) as a radio
        option with one already checked, so its option labels are just resume filenames
        rather than an answerable question. Concatenating all of them as question text
        (potentially years of history) produces garbage that can't be answered by cache
        or LLM, so this must be detected and skipped before that text is ever built.
        """
        try:
            radiogroup = section.locator("[role='radiogroup']").first
            if await radiogroup.count() == 0:
                return False
            labels = await radiogroup.locator("[role='radio'][aria-label]").evaluate_all(
                "els => els.map(e => (e.getAttribute('aria-label') || '').toLowerCase())"
            )
        except Exception:
            return False
        resume_extensions = (".pdf", ".doc", ".docx")
        return bool(labels) and all(label.endswith(resume_extensions) for label in labels)

    async def _find_and_handle_radio_question(self, section: Any) -> bool:
        """Handle radio button questions (async)"""
        # Look for radio buttons in the new LinkedIn form structure
        logger.debug("Searching for radio buttons in the section.")
        radios = {}

        # Try different selectors for radio buttons
        radio_selectors = ["input[type='radio']", "[role='radio']"]

        for selector in radio_selectors:
            loc = section.locator(selector)
            ids = await loc.evaluate_all("els => els.map(e => e.id || '')")
            found_radios = await loc.all()
            for index, (radio, radio_id) in enumerate(zip(found_radios, ids)):
                key = radio_id or f"{selector}:{index}"
                if key not in radios:
                    radios[key] = radio

        if not radios:
            for selector in (".fb-text-selectable__option", ".artdeco-button--toggle"):
                loc = section.locator(selector)
                for index, radio in enumerate(await loc.all()):
                    radios[f"{selector}:{index}"] = radio

        # Remove duplicates
        radios = list(radios.values())

        if radios:
            if await self._is_resume_picker_radiogroup(section):
                logger.info(
                    "Detected resume-selection radio group; keeping LinkedIn's default "
                    "selected resume"
                )
                return True

            # Extract the question and options from semantic DOM relationships first.
            try:
                combined_text = await section.text_content() or ""
                snapshot = await section.evaluate(
                    """root => {
                        let controls = [...root.querySelectorAll("input[type='radio'], [role='radio']")];
                        if (!controls.length) {
                            controls = [...root.querySelectorAll('.fb-text-selectable__option, .artdeco-button--toggle')];
                        }
                        const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
                        const options = controls.map(control => {
                            const id = control.id || '';
                            const explicit = id ? root.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                            const wrapping = control.closest('label');
                            const optionWrapper = control.closest('[role="radio"], .fb-text-selectable__option, .artdeco-button--toggle');
                            const roleWrapper = control.closest('[role="radio"][aria-label]');
                            const label = clean(
                                explicit?.textContent || wrapping?.textContent ||
                                roleWrapper?.getAttribute('aria-label') || optionWrapper?.textContent ||
                                control.getAttribute('aria-label') ||
                                control.getAttribute('data-test-text-selectable-option__input') || control.value
                            );
                            return {id, label, value: clean(control.value || control.getAttribute('data-value') || label)};
                        }).filter(item => item.label);
                        const fieldset = controls[0]?.closest('fieldset');
                        const group = controls[0]?.closest('[role="radiogroup"]');
                        let question = clean(fieldset?.querySelector(':scope > legend')?.textContent);
                        const labelled = group?.getAttribute('aria-labelledby') || fieldset?.getAttribute('aria-labelledby');
                        if (!question && labelled) {
                            question = clean(labelled.split(/\\s+/).map(id => document.getElementById(id)?.textContent || '').join(' '));
                        }
                        if (!question) {
                            const candidates = [...root.querySelectorAll(
                                'legend, .fb-dash-form-element__label, [data-test-form-builder-radio-button-form-component__title], p'
                            )];
                            question = clean(candidates.find(el => !el.closest('label[for]') && !el.closest('[role="radio"]'))?.textContent);
                        }
                        return {
                            question,
                            required: controls.some(c => c.required || c.getAttribute('aria-required') === 'true') || /\\*\\s*$/.test(question),
                            options
                        };
                    }"""
                )
                field = parse_radio_snapshot(snapshot, combined_text)
                question_text = normalize_question(field.question, field.options)
                options = list(field.options)
            except Exception:
                field = parse_radio_snapshot(None, await section.text_content() or "")
                question_text = field.question
                options = list(field.options)

            if not question_text or not options:
                logger.debug("No options extracted from radio buttons, skipping")
                return False

            self.previous_question_texts.append(question_text)
            resume = getattr(self.gpt_answerer, "resume_structured", {}) or {}
            answer = resolve_canonical_answer(
                question_text, options, resume, self.application_profile
            )
            if answer is not None:
                await self._select_radio(section, radios, answer)
                logger.debug("Selected source-grounded canonical radio answer")
                return True

            cached_question = None
            cached_answer = None
            if not self._force_fresh_question_resolution:
                cached_question = self._find_normalized_cached_question(question_text, "radio")
                if cached_question:
                    cached_answer = match_available_option(cached_question.answer, options)
                    if cached_answer is None:
                        logger.info("Ignoring cached radio answer absent from current DOM options")
            if cached_answer is not None:
                await self._select_radio(section, radios, cached_answer)
                logger.debug("Selected existing radio answer")
                return True

            logger.info(f"Asking question: {question_text}")
            logger.info(f"Available radio option count: {len(options)}")
            answer = self.gpt_answerer.select_one_answer_from_options(
                question_text, options, self.previous_question_texts[:-1]
            )
            if self._is_no_info_answer(answer):
                raise NoInfoException(f"No info found for question: {question_text}")
            answer = match_available_option(answer, options)
            if answer is None:
                raise NoInfoException(
                    f"NEEDS_HUMAN: resolved radio answer is not a current option: {question_text}"
                )
            question_data = Question(question_type="radio", question=question_text, answer=answer)
            self._save_questions(question_data)
            self.all_questions = self._load_questions()
            await self._select_radio(section, radios, answer)
            logger.debug("Selected new radio answer")
            return True
        return False

    async def _find_and_handle_date_question(self, section: Any) -> bool:
        """Fill native date controls only from source-backed availability data."""
        fields = await section.locator("input[type='date']").all()
        if not fields:
            return False
        field = fields[0]
        question_text = (await self._extract_semantic_question(field, section)).casefold()
        spec = await self._extract_field_spec(field, section, question_text)
        existing = await field.input_value()
        if existing and not validate_answer_for_field(existing, spec):
            return True

        answer = None
        if not self._force_fresh_question_resolution:
            cached = self._find_normalized_cached_question(question_text, "date")
            if cached:
                candidate = normalize_answer_for_field(cached.answer, spec)
                if candidate and re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
                    answer = candidate
        availability = resolve_canonical_text_answer(
            "Notice period",
            getattr(self.gpt_answerer, "resume_structured", {}) or {},
            self.application_profile,
        )
        if availability and (
            "immediate" in availability.casefold() or availability.casefold() == "none"
        ):
            answer = datetime.now().date().isoformat()
        if answer is None:
            if not spec.required:
                return True
            raise NoInfoException(
                f"NEEDS_HUMAN: no source-backed date is available for question: {question_text}"
            )
        answer = normalize_answer_for_field(answer, spec)
        if answer is None or validate_answer_for_field(answer, spec):
            raise NoInfoException(
                f"NEEDS_HUMAN: source-backed date violates field constraints: {question_text}"
            )
        await field.fill(answer)
        issues = await self._field_validation_issues(field, section, spec)
        if issues:
            raise NoInfoException(
                f"NEEDS_HUMAN: date field remained invalid: {question_text}"
            )
        self._save_questions(Question(question_type="date", question=question_text, answer=answer))
        return True

    async def _find_and_handle_textbox_question(self, section: Any) -> bool:
        """Handle textbox questions (async)"""
        logger.debug("Searching for text fields in the section.")

        # Look for text input fields in the new LinkedIn form structure
        text_field = None

        # Try different selectors for text inputs
        selectors = [
            "input[type='text']",
            "input[type='tel']",
            "input[type='number']",
            "input[type='email']",
            "textarea",
            ".artdeco-text-input--input",
        ]

        for selector in selectors:
            fields = await find_elements_safely(section, selector, "css selector")
            if fields:
                text_field = fields[0]
                break

        if text_field:
            # Try to find the label for this field
            try:
                # Look for label in various ways
                label = None
                label_selectors = [
                    "label",
                    ".fb-dash-form-element__label",
                    ".artdeco-text-input--label",
                    ".jobs-easy-apply-form-section__group-title",
                ]

                for label_selector in label_selectors:
                    label = await find_element_safely(section, label_selector, "css selector")
                    if label:
                        break

                semantic_question = await text_field.evaluate(
                    """el => {
                        const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
                        const labelled = clean((el.getAttribute('aria-labelledby') || '')
                            .split(/\\s+/).map(id => document.getElementById(id)?.textContent || '').join(' '));
                        const nativeLabels = clean(Array.from(el.labels || [])
                            .map(item => item.textContent || '').join(' '));
                        const container = el.closest('.fb-dash-form-element, [data-test-form-element], fieldset') || el.parentElement;
                        const nearby = clean(container?.querySelector('legend, p, .fb-dash-form-element__label')?.textContent);
                        return labelled || nativeLabels || nearby || clean(el.getAttribute('aria-label'));
                    }"""
                )

                if isinstance(semantic_question, str) and semantic_question.strip():
                    question_text = normalize_question(semantic_question).casefold()
                elif label:
                    question_text = normalize_question(await label.text_content() or "").casefold()
                else:
                    question_text = ""

                # Add placeholder to question text if it exists
                placeholder_text = await text_field.get_attribute(
                    "placeholder"
                ) or await text_field.get_attribute("aria-label")
                if placeholder_text and not question_text:
                    question_text = normalize_question(placeholder_text).casefold()

                self.previous_question_texts.append(question_text)
                logger.debug(
                    f"Found text field with label: {redact_text(question_text)}"
                )
            except Exception as e:
                self._reraise_if_target_closed(e)
                logger.warning(f"Could not find label for text field: {e}")
                question_text = ""

            spec = await self._extract_field_spec(text_field, section, question_text)
            logger.info(
                "Resolved field answer type {} with constraints: {}",
                spec.answer_type.value,
                spec.prompt_context(),
            )
            question_type = "numeric" if spec.numeric else "textbox"

            # Check if it's a cover letter field (case-insensitive)
            is_cover_letter = "cover letter" in question_text.lower()
            logger.info(f"question: {redact_text(question_text)}")
            # Look for existing answer if it's not a cover letter field
            answer = resolve_structured_answer(
                spec,
                getattr(self.gpt_answerer, "resume_structured", {}) or {},
                self.application_profile,
            )
            if answer is None:
                answer = resolve_canonical_text_answer(
                    question_text,
                    getattr(self.gpt_answerer, "resume_structured", {}) or {},
                    self.application_profile,
                )
            existing_answer = None
            if not is_cover_letter:
                cached_question = (
                    None
                    if self._force_fresh_question_resolution
                    else self._find_normalized_cached_question(question_text, question_type)
                )
                if cached_question:
                    cached_answer = cached_question.answer.strip()
                    if self._is_no_info_answer(cached_answer):
                        logger.info(f"Ignoring cached No info answer for question: {question_text}")
                    else:
                        normalized_cached = normalize_answer_for_field(cached_answer, spec)
                        if normalized_cached and not validate_answer_for_field(normalized_cached, spec):
                            existing_answer = normalized_cached
                            logger.debug(
                                "Found constraint-valid cached answer for '{}' via cached {} field",
                                question_text,
                                cached_question.question_type,
                            )
                        else:
                            logger.info("Ignoring cached answer that violates current DOM constraints")

            if answer is not None:
                logger.info("Using deterministic source-of-truth answer")
            elif spec.answer_type == AnswerType.YEARS_EXPERIENCE:
                # Never let a cache or model turn a skills-list mention into an
                # invented duration.
                if self._find_normalized_cached_question(question_text, question_type):
                    logger.info(
                        "Ignoring cached experience duration because current source files "
                        "do not establish years for this skill"
                    )
                answer = None
            elif existing_answer and not is_cover_letter:
                answer = existing_answer
                logger.info("Using existing cached answer (value omitted)")
            elif is_cover_letter:
                logger.info(f"Cover letter field found: {question_text}")
                cover_letter_text = self.gpt_answerer.write_cover_letter()
                answer = cover_letter_text
            else:
                constrained_question = (
                    f"{question_text}\nField constraints (must be obeyed): {spec.prompt_context()}"
                )
                if spec.numeric:
                    logger.info(f"Answering numeric question: {question_text}")
                    answer = self.gpt_answerer.answer_question_numeric(
                        constrained_question, self.previous_question_texts[:-1]
                    )
                else:
                    logger.info(f"Answering textual question: {question_text}")
                    answer = self.gpt_answerer.answer_question_textual_wide_range(
                        constrained_question, self.previous_question_texts[:-1]
                    )

            if answer is None or self._is_no_info_answer(answer):
                if not await self._is_required_text_field(section, text_field):
                    logger.info(
                        f"Skipping optional text field with no available answer: {question_text}"
                    )
                    return True
                raise NoInfoException(f"No info found for question: {question_text}")

            answer = self.resume_anonymizer.deanonymize_text(answer)
            answer = normalize_answer_for_field(answer, spec)
            issues = validate_answer_for_field(answer or "", spec)
            if answer is None or issues:
                if not spec.required:
                    logger.info("Skipping optional field because no constraint-valid answer is available")
                    return True
                raise NoInfoException(
                    f"No constraint-valid answer for question: {question_text}; "
                    f"issues={list(issues) or ['unsupported source fact']}"
                )

            answer = await self._fill_and_validate_text_field(
                text_field, section, question_text, answer, spec
            )

            # Save non-cover letter answers
            if not is_cover_letter and not existing_answer:
                question_data = Question(
                    question_type=question_type, question=question_text, answer=answer
                )
                self._save_questions(question_data)
                logger.debug("Saved non-cover letter answer.")

            return True

        logger.debug("No text fields found in the section.")
        return False

    async def _extract_field_spec(
        self, text_field: Any, section: Any, question_text: str
    ) -> FieldSpec:
        """Read the live DOM contract for a text-like control."""
        attributes = (
            "type",
            "maxlength",
            "minlength",
            "min",
            "max",
            "step",
            "pattern",
            "inputmode",
            "required",
            "placeholder",
            "aria-label",
            "aria-describedby",
            "aria-required",
            "role",
        )
        snapshot: dict[str, Any] = {}
        for attr in attributes:
            value = await text_field.get_attribute(attr)
            if value is not None:
                snapshot[attr] = value

        try:
            nearby = await section.locator(
                "[role='alert'], .artdeco-inline-feedback, .fb-form-element__error-text, "
                ".artdeco-text-input--helper, [data-test-form-element-help-text]"
            ).evaluate_all("els => els.map(e => e.textContent?.trim() || '').filter(Boolean)")
            if nearby:
                snapshot["help_text"] = " | ".join(nearby)
                if any("invalid" in text.casefold() or "error" in text.casefold() for text in nearby):
                    snapshot["error_text"] = " | ".join(nearby)
        except Exception as exc:
            self._reraise_if_target_closed(exc)

        try:
            section_text = await section.inner_text()
            counters = re.findall(r"\b\d+\s*/\s*\d+\b", section_text or "")
            if counters:
                snapshot["character_counter"] = counters[-1]
                if "maxlength" not in snapshot:
                    snapshot["maxlength"] = counters[-1].split("/")[-1].strip()
        except Exception as exc:
            self._reraise_if_target_closed(exc)

        return field_spec_from_snapshot(question_text, snapshot)

    async def _field_validation_issues(
        self, text_field: Any, section: Any, spec: FieldSpec
    ) -> tuple[str, ...]:
        value = await text_field.input_value()
        issues = list(validate_answer_for_field(value, spec))
        aria_invalid = await text_field.get_attribute("aria-invalid")
        if str(aria_invalid).casefold() == "true":
            issues.append("aria-invalid")
        try:
            validity = await text_field.evaluate(
                "el => ({valid: el.validity ? el.validity.valid : true, "
                "message: el.validationMessage || ''})"
            )
            if isinstance(validity, dict) and validity.get("valid") is False:
                issues.append(validity.get("message") or "browser-validity")
        except Exception as exc:
            self._reraise_if_target_closed(exc)
        try:
            visible_errors = await section.locator(
                ".artdeco-inline-feedback--error, [role='alert'][data-test-form-element-error-messages]"
            ).evaluate_all(
                "els => els.filter(e => e.offsetParent !== null).map(e => e.textContent?.trim() || '').filter(Boolean)"
            )
            issues.extend(visible_errors)
        except Exception as exc:
            self._reraise_if_target_closed(exc)
        return tuple(dict.fromkeys(str(issue) for issue in issues if issue))

    async def _fill_and_validate_text_field(
        self,
        text_field: Any,
        section: Any,
        question_text: str,
        answer: str,
        spec: FieldSpec,
        max_corrections: int = 2,
    ) -> str:
        """Fill, inspect browser validity, and perform bounded correction."""
        current = answer
        for correction in range(max_corrections + 1):
            await text_field.fill(current)
            await self._process_autocomplete_suggestions(text_field)
            issues = await self._field_validation_issues(text_field, section, spec)
            if not issues:
                logger.debug("Entered a constraint-valid answer into the textbox")
                return current
            if correction >= max_corrections:
                raise NoInfoException(
                    f"Field remained invalid after {max_corrections} corrections for "
                    f"question: {question_text}; issues={list(issues)}"
                )
            actual = await text_field.input_value()
            normalized = normalize_answer_for_field(actual or current, spec)
            if normalized and normalized != current and not validate_answer_for_field(normalized, spec):
                current = normalized
                continue
            if spec.answer_type in {
                AnswerType.YEARS_EXPERIENCE,
                AnswerType.DAYS_AVAILABILITY,
                AnswerType.SALARY,
            }:
                # Structured facts may only come from deterministic source data.
                supported = resolve_structured_answer(
                    spec,
                    getattr(self.gpt_answerer, "resume_structured", {}) or {},
                    self.application_profile,
                )
                if supported and supported != current:
                    current = supported
                    continue
                raise NoInfoException(
                    f"NEEDS_HUMAN: structured field cannot be corrected safely: {question_text}"
                )
            constrained_question = (
                f"{question_text}\nField constraints (must be obeyed): {spec.prompt_context()}"
            )
            candidate = self.gpt_answerer.answer_question_textual_wide_range_with_error(
                constrained_question,
                "; ".join(issues),
                actual,
                self.previous_question_texts[:-1],
            )
            current = normalize_answer_for_field(candidate, spec) or ""
            if not current or validate_answer_for_field(current, spec):
                raise NoInfoException(
                    f"NEEDS_HUMAN: no constraint-valid correction for question: {question_text}"
                )
        raise AssertionError("unreachable")

    async def _is_required_text_field(self, section: Any, text_field: Any) -> bool:
        """Best-effort detection for LinkedIn required text fields."""
        for attr in ("required", "aria-required"):
            try:
                value = await text_field.get_attribute(attr)
                if value is not None and str(value).lower() in {"", "true", "required"}:
                    return True
            except Exception:
                pass

        try:
            labels = await section.locator("label").all()
            for label in labels:
                label_text = (await label.text_content() or "").strip().lower()
                if "optional" in label_text:
                    continue
                if "*" in label_text:
                    return True
        except Exception as e:
            logger.debug(f"Failed checking required text field labels: {e}")

        return False

    async def _process_autocomplete_suggestions(self, text_field: Any) -> None:
        """Handle autocomplete suggestions if they appear (async)"""
        await async_pause(1, 2)
        try:
            # Check if autocomplete suggestions are visible
            suggestions = self.page.locator(".basic-typeahead__selectable")
            if await suggestions.count() > 0:
                logger.debug("Autocomplete suggestions detected, selecting first option")
                try:
                    await self.page.keyboard.press("ArrowDown")
                    await async_pause()
                    await self.page.keyboard.press("Enter")
                except Exception:
                    pass
                logger.debug("Selected first suggestion from autocomplete")
        except Exception as e:
            logger.debug(f"No autocomplete suggestions found or error handling suggestions: {e}")

    async def _find_and_handle_dropdown_question(self, section: Any) -> bool:
        """Handle dropdown questions (async)"""
        try:
            # Look for dropdowns in the new LinkedIn form structure
            dropdowns = {}

            # Try different selectors for dropdowns
            dropdown_selectors = [
                "select",
                "[data-test-text-entity-list-form-select]",
                ".fb-dash-form-element__select-dropdown",
                "select.fb-dash-form-element__select-dropdown",
            ]

            for selector in dropdown_selectors:
                _selector = f"css={selector}" if selector == "select" else selector
                found_dropdowns = await find_elements_safely(section, _selector, "css selector")
                for dropdown in found_dropdowns:
                    dropdown_id = await dropdown.get_attribute("id")
                    if dropdown_id and dropdown_id not in dropdowns:
                        dropdowns[dropdown_id] = dropdown

            # Remove duplicates
            dropdowns = list(dropdowns.values())

            if dropdowns:
                logger.info("Dropdowns found")
                dropdown = dropdowns[0]
                # Try to gather options text if possible
                options = []
                try:
                    # For native select elements, get options via DOM
                    options = [
                        t
                        for t in await dropdown.locator("option").evaluate_all(
                            "els => els.map(e => e.textContent?.trim() ?? '')"
                        )
                        if t
                    ]
                except Exception:
                    options = []

                # Try to find the label for this dropdown
                try:
                    question_text = (
                        await self._extract_semantic_question(dropdown, section)
                    ).casefold()
                    self.previous_question_texts.append(question_text)
                except Exception as e:
                    logger.warning(f"Could not find label for dropdown: {e}")
                    question_text = ""

                try:
                    selection_info = await dropdown.evaluate(
                        """el => {
                            const opt = el.options[el.selectedIndex];
                            if (!opt) return null;
                            return {
                                text: (opt.textContent || '').trim(),
                                index: el.selectedIndex,
                                hasSelectedAttr: opt.hasAttribute('selected'),
                            };
                        }"""
                    )
                    if selection_info and (
                        selection_info["index"] > 0 or selection_info["hasSelectedAttr"]
                    ):
                        current_selection = selection_info["text"]
                    else:
                        current_selection = ""
                except Exception:
                    current_selection = ""
                logger.debug(
                    "Current dropdown selection present: "
                    f"{bool(current_selection)}"
                )

                if self._is_meaningful_existing_answer(current_selection):
                    logger.info(
                        f"Dropdown question '{redact_text(question_text)}' already "
                        "has a selected answer (value omitted)"
                    )
                    self._save_questions(
                        Question(
                            question_type="dropdown",
                            question=question_text,
                            answer=current_selection,
                        )
                    )
                    return True

                existing_answer = None
                cached_question = (
                    None
                    if self._force_fresh_question_resolution
                    else self._find_normalized_cached_question(question_text, "dropdown")
                )
                if cached_question:
                    candidate_answer = (
                        cached_question.answer.strip()
                        if isinstance(cached_question.answer, str)
                        else cached_question.answer
                    )
                    existing_answer = match_available_option(candidate_answer, options)
                    if existing_answer is None:
                        logger.info("Ignoring cached dropdown answer absent from current DOM options")

                if existing_answer:
                    logger.debug(
                        "Found cached dropdown answer for question "
                        f"'{redact_text(question_text)}' (value omitted)"
                    )
                    if current_selection != existing_answer:
                        logger.debug("Updating dropdown selection (value omitted)")
                        await self._select_dropdown_option(dropdown, existing_answer)
                else:
                    logger.info(f"Asking question: {redact_text(question_text)}")
                    logger.info(f"Available dropdown option count: {len(options)}")

                    # If LinkedIn provides exactly one real option, select it
                    # directly instead of asking the LLM.
                    placeholder_options = {
                        "select an option",
                        "choose an option",
                        "empty response",
                        "my option",
                        "your option",
                        "no info",
                    }

                    valid_options = [
                        option
                        for option in options
                        if isinstance(option, str)
                        and option.strip()
                        and option.strip().lower() not in placeholder_options
                    ]

                    if len(valid_options) == 1:
                        answer = valid_options[0]
                        logger.info("Only one valid dropdown option; selecting it")
                    else:
                        answer = resolve_canonical_answer(
                            question_text,
                            valid_options,
                            getattr(self.gpt_answerer, "resume_structured", {}) or {},
                            self.application_profile,
                        )
                        if answer is None:
                            answer = self.gpt_answerer.select_one_answer_from_options(
                                question_text, valid_options, self.previous_question_texts[:-1]
                            )

                    if self._is_no_info_answer(answer):
                        raise NoInfoException(f"No info found for question: {question_text}")
                    answer = match_available_option(answer, valid_options)
                    if answer is None:
                        raise NoInfoException(
                            f"NEEDS_HUMAN: resolved dropdown answer is not a current option: {question_text}"
                        )
                    question_data = Question(
                        question_type="dropdown", question=question_text, answer=answer
                    )
                    self._save_questions(question_data)
                    await self._select_dropdown_option(dropdown, answer)
                    logger.debug("Selected new dropdown answer (value omitted)")

                return True

            else:
                logger.debug("No dropdown found. Logging elements for debugging.")
                try:
                    elements_count = await section.locator("xpath=.//*").count()
                    logger.debug(f"Elements found count: {elements_count}")
                except Exception:
                    pass
                return False

        except NoInfoException:
            raise
        except Exception as e:
            logger.warning(f"Failed to handle dropdown or combobox question: {e}", exc_info=True)
            await debug_capture(self.page, "dropdown_question_error")
            return False

    _NUMERIC_QUESTION_KEYWORDS = (
        "salary",
        "compensation",
        "pay",
        "wage",
        "rate",
        "earnings",
        "years of experience",
        "how many years",
        "how many months",
        "number of",
        "how many",
        "gpa",
        "grade point",
    )

    async def _is_numeric_field(self, field: Any, question_text: str = "") -> bool:
        """Check if field is numeric (async)"""
        field_type = (await field.get_attribute("type") or "").lower()
        field_id = (await field.get_attribute("id") or "").lower()
        is_numeric = (
            "numeric" in field_id
            or field_type == "number"
            or ("text" == field_type and "numeric" in field_id)
        )
        if not is_numeric:
            q = question_text.lower()
            is_numeric = any(
                re.search(r"\b" + re.escape(kw) + r"\b", q)
                for kw in self._NUMERIC_QUESTION_KEYWORDS
            )
        logger.debug(f"Field type: {field_type}, Field ID: {field_id}, Is numeric: {is_numeric}")
        return is_numeric

    def _is_meaningful_existing_answer(self, value: str | None) -> bool:
        if not value:
            return False
        normalized = sanitize_text(value)
        if not normalized:
            return False
        placeholders = {
            "select an option",
            "choose an option",
            "select",
            "choose",
            "please select",
            "empty response",
            "no info",
        }
        return normalized not in placeholders

    def _deduplicate_question_text(self, question_text: str) -> str:
        """If the question consists of two lines, and the second line is the same as the first line, use the first line"""
        if len(question_text) % 2 == 0:
            half_length = len(question_text) // 2
            if question_text[:half_length] == question_text[half_length:]:
                question_text = question_text[:half_length]
                return question_text
        question_list = question_text.split("\n")
        deduplicated_question_list = list(dict.fromkeys(question_list))
        question_text = "\n".join(deduplicated_question_list)
        return question_text

    def _find_normalized_cached_question(
        self, question_text: str, question_type: str | None = None
    ) -> Question | None:
        """Match legacy cache entries semantically after safe UI-text cleanup."""
        wanted = normalize_question(question_text).casefold()
        same_type = None
        fallback = None
        for item in self.all_questions:
            if normalize_question(item.question).casefold() != wanted:
                continue
            if item.question_type == question_type:
                same_type = item
                break
            fallback = fallback or item
        return same_type or fallback

    async def _select_radio(self, section: Any, radios: List[Any], answer: str) -> None:
        """Select radio button based on answer (async)"""
        logger.debug("Selecting radio option (value omitted)")
        for radio in radios:
            try:
                # Extract text from radio button or its associated label
                radio_text = ""

                # Look for label with matching 'for' attribute
                radio_id = await radio.get_attribute("id")
                if radio_id:
                    label = section.locator(f"label[for='{radio_id}']").first
                    radio_text = (await label.text_content() or "").strip()
                    if not radio_text:
                        # New LinkedIn SDUI markup renders an empty <label>; the visible
                        # option text lives on the nearest ancestor's aria-label instead
                        radio_text = (
                            await radio.evaluate(
                                "e => (e.closest('[aria-label]')?.getAttribute('aria-label') || '')"
                            )
                        ).strip()
                if not radio_text:
                    radio_text = (
                        await radio.evaluate(
                            """e => (
                                e.closest('label')?.textContent ||
                                e.closest('[role="radio"][aria-label]')?.getAttribute('aria-label') ||
                                e.getAttribute('aria-label') || e.value || ''
                            )"""
                        )
                    ).strip()
                radio_text = normalize_option(radio_text)

                logger.debug(f"Radio button text extracted: '{radio_text}'")

                if radio_text and normalize_option(answer).casefold() == radio_text.casefold():
                    # Try different ways to click the radio button
                    try:
                        # First try clicking the associated label (most reliable for LinkedIn)
                        if radio_id:
                            await label.click(timeout=1000)
                        else:
                            await radio.click(timeout=1000)
                        checked = None
                        try:
                            checked = await radio.is_checked()
                        except Exception:
                            aria_checked = await radio.get_attribute("aria-checked")
                            if aria_checked is not None:
                                checked = str(aria_checked).casefold() == "true"
                        if checked is False:
                            raise NoInfoException("Radio option did not become selected")
                        logger.debug("Clicked matching radio control")
                        return
                    except Exception:
                        logger.warning(f"Failed to click radio button: {radio_text}")

            except Exception as e:
                logger.warning(f"Failed to process radio button: {e}")
                continue

        raise NoInfoException("Resolved radio answer did not match any available option")

    async def _select_dropdown_option(self, element: Any, text: str) -> None:
        """Select dropdown option by visible text using robust matching (async).

        Tries exact label match, then normalized label (collapsed whitespace),
        then label without parenthetical (e.g., removes "(+1)"), and finally
        resolves to the option's 'value' when a label match is found.
        """
        logger.debug("Selecting dropdown option (value omitted)")

        def normalize_label(s: str) -> str:
            try:
                import re as _re

                return _re.sub(r"\s+", " ", s or "").strip().lower()
            except Exception:
                return (s or "").strip().lower()

        label_candidates = []
        original = (text or "").strip()
        label_candidates.append(original)

        # Collapsed whitespace
        collapsed = " ".join(original.split())
        if collapsed not in label_candidates:
            label_candidates.append(collapsed)

        # Remove parenthetical like "(+1)"
        if "(" in original and ")" in original:
            base = original.split("(")[0].strip()
            if base and base not in label_candidates:
                label_candidates.append(base)

        # Normalize candidates for matching
        normalized_candidates = [normalize_label(c) for c in label_candidates]

        async def confirm_selected() -> None:
            try:
                selected = await element.evaluate(
                    "el => el.options?.[el.selectedIndex]?.textContent?.trim() || ''"
                )
            except Exception as exc:
                self._reraise_if_target_closed(exc)
                return
            if isinstance(selected, str) and selected and match_available_option(text, [selected]) is None:
                raise NoInfoException("Dropdown selection did not match the requested DOM option")

        # First try direct label selection with primary candidate
        try:
            await element.select_option(label=label_candidates[0], timeout=3000)
            await confirm_selected()
            return
        except Exception as e:
            logger.warning(f"Failed to select dropdown option: {e}")
            pass

        # Inspect available <option> elements to resolve the correct value
        try:
            matched_value = None
            opts_data = await element.locator("option").evaluate_all(
                "els => els.map(e => ({label: e.textContent || '', value: e.value || ''}))"
            )
            norm_to_value = [
                (normalize_label(d["label"]), d["value"]) for d in opts_data if d["label"].strip()
            ]

            # Exact match on normalized labels
            for cand in normalized_candidates:
                for opt_label_norm, opt_value in norm_to_value:
                    if opt_label_norm == cand:
                        matched_value = opt_value
                        break
                if matched_value:
                    break

            # Contains match if no exact
            if not matched_value:
                for cand in normalized_candidates:
                    for opt_label_norm, opt_value in norm_to_value:
                        if cand and cand in opt_label_norm:
                            matched_value = opt_value
                            break
                    if matched_value:
                        break

            if matched_value:
                await element.select_option(value=matched_value)
                await confirm_selected()
                return
        except Exception as e:
            logger.warning(f"Failed to select dropdown option: {e}")
            pass

        # Final fallbacks: try label with collapsed whitespace, then value
        for cand in label_candidates[1:]:
            try:
                await element.select_option(label=cand)
                await confirm_selected()
                return
            except Exception:
                continue

        try:
            await element.select_option(value=collapsed)
            await confirm_selected()
            return
        except Exception as e:
            logger.warning(f"Failed to select dropdown option: {e}")
        raise NoInfoException("Resolved dropdown answer could not be selected from current options")

    async def _find_all_form_errors(self) -> List[str]:
        error_selectors = [
            ".artdeco-inline-feedback--error .artdeco-inline-feedback__message",
            ".artdeco-inline-feedback--error",
            "[role='alert'][data-test-form-element-error-messages]",
        ]
        errors_text: List[str] = []
        seen: set[str] = set()
        for selector in error_selectors:
            try:
                texts = await self.page.locator(selector).evaluate_all(
                    "els => els.filter(e => e.offsetParent !== null)"
                    ".map(e => e.textContent?.trim() || '')"
                )
                for txt in texts:
                    if txt and txt not in seen:
                        seen.add(txt)
                        errors_text.append(txt)
            except Exception as e:
                self._reraise_if_target_closed(e)
                logger.debug(f"Failed to locate error elements with '{selector}': {e}")
        try:
            invalid_states = await self.page.locator(
                '[data-testid="dialog-content"]:visible input, '
                '[data-testid="dialog-content"]:visible select, '
                '[data-testid="dialog-content"]:visible textarea, '
                '[data-testid="dialog-content"]:visible [role="radio"], '
                '[data-testid="dialog-content"]:visible [role="checkbox"]'
            ).evaluate_all(
                """els => {
                    const visible = el => el.isConnected && el.getClientRects().length > 0;
                    const clean = value => (value || '').trim();
                    const placeholder = value => /^(?:|select(?: an)? option|choose(?: an)? option|please select|empty response|--.*--)$/i.test(clean(value));
                    return els.filter(visible).map(el => {
                      const role = (el.getAttribute('role') || '').toLowerCase();
                      const type = (el.getAttribute('type') || role || el.tagName).toLowerCase();
                      const required = Boolean(el.required || el.getAttribute('aria-required') === 'true');
                      const ariaInvalid = el.getAttribute('aria-invalid') === 'true';
                      const browserInvalid = Boolean(el.validity && !el.validity.valid);
                      const selectedOption = el.tagName === 'SELECT'
                        ? el.options?.[el.selectedIndex] || null : null;
                      let unanswered = false;
                      if (required && type === 'radio') {
                        const name = el.getAttribute('name');
                        unanswered = name
                          ? !el.closest('form, [role="dialog"]')?.querySelector(`input[type="radio"][name="${CSS.escape(name)}"]:checked`)
                          : el.getAttribute('aria-checked') !== 'true' && !el.checked;
                      } else if (required && type === 'checkbox') {
                        unanswered = el.getAttribute('aria-checked') !== 'true' && !el.checked;
                      } else if (required && el.tagName !== 'SELECT') {
                        unanswered = !clean(el.value);
                      }
                      const kind = type === 'radio' || type === 'checkbox'
                        ? type : (el.tagName === 'SELECT' ? 'select' : type);
                      return {
                        kind,
                        required,
                        unanswered,
                        ariaInvalid,
                        browserInvalid,
                        selectedIndex: el.tagName === 'SELECT' ? el.selectedIndex : null,
                        selectedDisabled: Boolean(selectedOption?.disabled),
                        hasValue: el.tagName === 'SELECT' ? Boolean(clean(el.value)) : null,
                        selectedPlaceholder: el.tagName === 'SELECT'
                          ? !selectedOption || placeholder(selectedOption.textContent) : null
                      };
                    });
                }"""
            )
            for state in invalid_states:
                if not isinstance(state, dict):
                    continue
                kind = str(state.get("kind") or "control")
                unanswered = (
                    self._required_select_is_unanswered(state)
                    if kind == "select"
                    else bool(state.get("unanswered"))
                )
                issues = []
                if unanswered:
                    issues.append(f"required-unanswered:{kind}")
                if state.get("ariaInvalid"):
                    issues.append(f"aria-invalid:{kind}")
                if state.get("browserInvalid"):
                    issues.append(f"browser-invalid:{kind}")
                for issue in issues:
                    if issue not in seen:
                        seen.add(issue)
                        errors_text.append(issue)
        except Exception as exc:
            self._reraise_if_target_closed(exc)
            logger.debug("Failed to inspect required control validity: {}", type(exc).__name__)
        return errors_text

    @staticmethod
    def _required_select_is_unanswered(state: dict[str, Any]) -> bool:
        """Treat a meaningful native option as answered even when it is index 0."""
        if not state.get("required"):
            return False
        selected_index = state.get("selectedIndex")
        if not isinstance(selected_index, int) or selected_index < 0:
            return True
        if state.get("selectedDisabled"):
            return True
        return not state.get("hasValue") and bool(state.get("selectedPlaceholder"))

    async def _repair_invalid_fields_before_navigation(self) -> None:
        """Repair current-step validation errors before searching for navigation."""
        for attempt in range(2):
            errors = await self._find_all_form_errors()
            if not errors:
                return
            logger.info(
                "Current Easy Apply step has {} validation error(s); correction pass {}/2",
                len(errors),
                attempt + 1,
            )
            repaired = await self._fill_textbox_question_errors()
            if not repaired and any(
                error.startswith(("required-unanswered:radio", "required-unanswered:select"))
                for error in errors
            ):
                self._force_fresh_question_resolution = True
                if self.current_job is not None:
                    await self._fill_up(self.current_job)
                    repaired = True
            if not repaired:
                logger.error(
                    "EASY_APPLY_VALIDATION_UNMAPPED | error_count={} | correction_pass={}",
                    len(errors),
                    attempt + 1,
                )
                raise RuntimeError(
                    "TECHNICAL_FAILURE: validation controls could not be mapped for safe repair"
                )
        remaining = await self._find_all_form_errors()
        if remaining:
            raise RuntimeError(
                "TECHNICAL_FAILURE: form remains invalid after bounded correction"
            )

    async def _find_invalid_native_textboxes(self) -> List[Tuple[Any, str, str]]:
        """Find modern SDUI controls without requiring legacy wrappers or alerts."""
        scopes = (
            '[data-testid="dialog-content"]:visible',
            '.jobs-easy-apply-modal__content:visible',
            '.artdeco-modal__content:visible',
        )
        controls = "input:not([type]), input[type='text'], input[type='tel'], input[type='number'], input[type='email'], input[type='url'], textarea"
        selector = ", ".join(
            f"{scope} {control.strip()}" for scope in scopes for control in controls.split(",")
        )
        fields = self.page.locator(selector)
        states = await fields.evaluate_all(
            """els => els.map((el, index) => {
                const visible = el.isConnected && el.getClientRects().length > 0;
                const editable = !el.disabled && !el.readOnly;
                const required = el.required || el.getAttribute('aria-required') === 'true';
                const unanswered = required && !(el.value || '').trim();
                const invalid = el.getAttribute('aria-invalid') === 'true' ||
                    Boolean(el.validity && !el.validity.valid) || unanswered;
                return {index, invalid: visible && editable && invalid};
            })"""
        )
        results = []
        for state in states:
            if not isinstance(state, dict) or not state.get("invalid"):
                continue
            element = fields.nth(state["index"])
            section = await self._widen_to_text_input_container(element)
            question = await self._extract_semantic_question(element, section)
            if question:
                results.append((element, question, "native-invalid:text"))
        return results

    async def _find_textbox_question_errors(self) -> List[Tuple[Any, str, str]]:
        """Find textbox fields that have validation errors and return details (async).

        Returns:
            List of tuples for each errored textbox field:
            - WebElement: the textbox (or textarea) element that needs correction
            - str: the question/label text associated with the field
            - str: the visible error message text
        """
        logger.debug("Searching for textbox validation errors in the Easy Apply modal")
        results: List[Tuple[Any, str, str]] = []
        form_containers = []

        form_container_selectors = [
            "xpath=.//*[contains(@class, 'fb-dash-form-element')]",
            "div[data-test-form-element]",
            "[data-test-single-line-text-form-component]",
            "[data-test-multiline-text-form-component]",
            "xpath=.//*[contains(@class, 'jobs-easy-apply-form-section__group')]",
        ]

        for selector in form_container_selectors:
            try:
                form_containers = await self.page.locator(selector).all()
                if form_containers:
                    logger.debug(
                        f"Found {len(form_containers)} form containers globally using selector: {selector}"
                    )
                    break
            except Exception as exc:
                self._reraise_if_target_closed(exc)
                continue

        logger.debug(f"Found {len(form_containers)} form containers to inspect for errors")

        for section in form_containers:
            # Detect an error message within this section
            error_element: Any | None = None
            error_text: str = ""
            try:
                # Prefer the explicit message span inside the error container
                error_selectors = [
                    ".artdeco-inline-feedback--error .artdeco-inline-feedback__message",
                    ".artdeco-inline-feedback--error",
                    "[role='alert'][data-test-form-element-error-messages]",
                ]
                for selector in error_selectors:
                    loc = section.locator(selector)
                    cand_data = await loc.evaluate_all(
                        "els => els.map((e, i) => ({i, visible: e.offsetParent !== null, text: e.textContent?.trim() || ''}))"
                    )
                    match = next((d for d in cand_data if d["visible"] and d["text"]), None)
                    if match:
                        error_element = loc.nth(match["i"])
                        error_text = match["text"]
                        break
            except Exception as exc:
                self._reraise_if_target_closed(exc)
                error_element = None

            if not error_element:
                continue

            # Find the textbox/textarea to correct within this section
            target_input: Any | None = None
            all_inputs_loc = section.locator(
                "input[type='text'], input[type='tel'], input[type='number'], "
                "input[type='email'], textarea, .artdeco-text-input--input"
            )
            vis_indices = await all_inputs_loc.evaluate_all(
                "els => els.map((e, i) => e.offsetParent !== null ? i : -1).filter(i => i >= 0)"
            )
            if vis_indices:
                target_input = all_inputs_loc.nth(vis_indices[0])

            if not target_input:
                # If no visible input found, skip this section
                logger.debug("Error found but no visible textbox in section; skipping")
                continue

            # Extract question/label text
            question_text = ""
            label: Any | None = None
            label_selectors = [
                "label",
                ".fb-dash-form-element__label",
                ".artdeco-text-input--label",
                "[data-test-single-typeahead-entity-form-title='true']",
            ]
            for selector in label_selectors:
                labels = await find_elements_safely(section, selector, "css selector")
                if labels:
                    label = labels[0]
                    break

            if label:
                try:
                    question_text = (await label.text_content() or "").lower().strip()
                    question_text = self._deduplicate_question_text(question_text)
                except Exception:
                    question_text = ""

            if not question_text:
                try:
                    alt = await target_input.get_attribute(
                        "aria-label"
                    ) or await target_input.get_attribute("placeholder")
                    if alt:
                        question_text = alt.strip()
                except Exception:
                    question_text = ""

            if not question_text:
                question_text = ""

            results.append((target_input, question_text, error_text))

        if not results:
            results = await self._find_invalid_native_textboxes()
        logger.debug(f"Textbox errors found: {len(results)}")
        return results

    async def _fill_textbox_question_errors(self) -> bool:
        """Find and try to fill with correct answers textbox question with errors (async)"""
        errors = await self._find_textbox_question_errors()
        if not errors:
            return False
        for error in errors:
            element, question_text, error_text = error
            logger.info("Repairing invalid textbox from current form semantics (values omitted)")
            section = await self._widen_to_text_input_container(element)
            spec = await self._extract_field_spec(element, section, question_text)
            answer = resolve_structured_answer(
                spec,
                getattr(self.gpt_answerer, "resume_structured", {}) or {},
                self.application_profile,
            )
            if answer is None:
                answer = resolve_canonical_text_answer(
                    question_text,
                    getattr(self.gpt_answerer, "resume_structured", {}) or {},
                    self.application_profile,
                )
            if answer is None and error_text == "native-invalid:text":
                raise NoInfoException(
                    "NEEDS_HUMAN: required candidate fact is not established in source data "
                    f"(category={spec.answer_type.value})"
                )
            if answer is None and spec.answer_type == AnswerType.YEARS_EXPERIENCE:
                raise NoInfoException(
                    f"NEEDS_HUMAN: years of experience are not established for: {question_text}"
                )
            if answer is None:
                constrained_question = (
                    f"{question_text}\nField constraints (must be obeyed): {spec.prompt_context()}"
                )
                answer = self.gpt_answerer.answer_question_textual_wide_range_with_error(
                    constrained_question,
                    error_text,
                    await element.input_value(),
                    self.previous_question_texts[:-1],
                )
            if self._is_no_info_answer(answer):
                raise NoInfoException(
                    f"Can't fix error: {error_text}. No info found for question: {question_text}"
                )
            answer = self.resume_anonymizer.deanonymize_text(answer)
            answer = normalize_answer_for_field(answer, spec)
            if answer is None or validate_answer_for_field(answer, spec):
                raise NoInfoException(
                    f"NEEDS_HUMAN: correction violates field constraints: {question_text}"
                )
            answer = await self._fill_and_validate_text_field(
                element, section, question_text, answer, spec
            )
            self._save_questions(
                Question(
                    question_type="numeric" if spec.numeric else "text",
                    question=question_text,
                    answer=answer,
                )
            )
        return True


if __name__ == "__main__":
    """Simple test for LinkedInEasyApplier functionality"""
    import asyncio
    from pathlib import Path

    import dotenv

    from config.app_config import TEST_MODE
    from config.constants import (
        COVER_LETTER_DIR,
        OUTPUT_DIR_LINKEDIN,
        RESUME_DIR,
        SEARCH_CONFIG_FILE,
    )
    from src.job_manager.linkedin.job_manager_linkedin import LinkedInJobManager
    from src.job_manager.resume_anonymizer import ResumeAnonymizer
    from src.llm.llm_manager import GPTAnswerer
    from src.pydantic_models.prompt_models import ResumeStructure
    from src.resume_builder.resume_generator import ResumeGenerator
    from src.resume_builder.resume_manager import ResumeManager
    from src.resume_builder.style_manager import StyleManager

    # Wrapper removed in migration; use raw Playwright page
    from src.utils.browser_utils import create_playwright_browser, save_browser_session

    RESUME_STRUCTURED_FILE = Path(RESUME_DIR) / "structured_resume.yaml"
    RESUME_TEXT_FILE = Path(RESUME_DIR) / "resume_text.txt"
    paused = False

    def build_linkedin_job_url(job_url_or_id: str | None = None) -> str:
        """Build a LinkedIn job URL from a full URL, numeric ID, or default value."""
        default_job_url = "https://www.linkedin.com/jobs/view/4147219629"
        if not job_url_or_id:
            return default_job_url
        job_url_or_id = job_url_or_id.strip()
        if not job_url_or_id:
            return default_job_url
        if job_url_or_id.isdigit():
            return f"https://www.linkedin.com/jobs/view/{job_url_or_id}"
        return job_url_or_id

    def normalize_test_job_from_parsed_page(parsed_job: Job | None, job_url: str) -> Job:
        """Ensure direct Easy Applier tests always have a usable Job object."""
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

    def collect_apply_result_metadata(
        easy_applier: Any, submitted_resume_path: Any
    ) -> dict[str, str]:
        """Collect metadata that should be persisted with standalone Easy Apply results."""
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

    async def check_pause():
        """Check if execution is paused and wait if needed"""
        global paused
        if paused:
            while paused:
                await asyncio.sleep(0.5)

    async def test_easy_applier(job_url_or_id: str | None = None):
        """Test LinkedInEasyApplier with a real LinkedIn job posting (async)"""
        logger.info("Starting LinkedInEasyApplier test...")

        # Test job URL
        job_url = build_linkedin_job_url(job_url_or_id)
        # Initialize Playwright browser
        try:
            browser, context, page = await create_playwright_browser()
            page = page
            logger.info("Playwright browser initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Playwright browser: {e}")
            return False

        try:
            # Navigate to job page and parse the actual job context before initializing LLM prompts.
            logger.info(f"Navigating to job page: {job_url}")
            await page.goto(job_url)
            await async_pause(3, 5)
            try:
                parser = LinkedInJobManager(page, "", None, None)
                parsed_job = await parser._get_detailed_job_description()
                test_job = normalize_test_job_from_parsed_page(parsed_job, job_url)
                logger.info(f"Parsed job page: {test_job.job_title} at {test_job.company_name}")
            except Exception as e:
                logger.warning(f"Failed to parse job page; using generic fallback job context: {e}")
                test_job = normalize_test_job_from_parsed_page(None, job_url)

            # Load secrets for LLM
            secrets = dotenv.dotenv_values(".env")
            llm_api_key = secrets.get("llm_api_key", "")
            llm_proxy = secrets.get("llm_proxy", "")

            # Initialize GPT answerer
            gpt_answerer = GPTAnswerer(llm_api_key, llm_proxy)
            resume_structured = load_yaml_file(RESUME_STRUCTURED_FILE)
            resume_structured = ResumeStructure(**resume_structured).model_dump()
            with open(RESUME_TEXT_FILE, "r") as f:
                resume_text = f.read()

            # Set resume anonymizer and anonymize the resume information
            resume_anonymizer = ResumeAnonymizer(resume_structured)
            resume_anonymizer.anonymize_personal_information()
            resume_structured = resume_anonymizer.resume_anonymized
            resume_text = resume_anonymizer.anonymize_text(resume_text)

            gpt_answerer.set_resume(resume_structured, resume_text)
            gpt_answerer.set_job(test_job.model_dump(), is_test=True)

            # Initialize resume generator manager (mock for testing)
            style_manager = StyleManager()
            resume_generator = ResumeGenerator(gpt_answerer, resume_anonymizer)
            resume_generator_manager = ResumeManager(llm_api_key, style_manager, resume_generator)

            # Initialize LinkedInEasyApplier
            easy_applier = LinkedInEasyApplier(
                page,
                gpt_answerer,
                resume_anonymizer,
                resume_generator_manager,
                check_pause,
                Path(OUTPUT_DIR_LINKEDIN) / "answers.yaml",
                RESUME_DIR,
                COVER_LETTER_DIR,
                test_mode=True,  # !
            )
            if easy_applier.ready_made_resume_path is None:
                resume_generator_manager.choose_style()

            # Test the apply_to_job method
            logger.info("Testing LinkedInEasyApplier.apply_to_job method...")
            result = await easy_applier.apply_to_job(test_job)
            apply_result, submitted_resume_path = result
            status, reason = apply_result

            result_manager = LinkedInJobManager(page, "", resume_anonymizer, None)
            result_manager.set_answerer_and_agent(gpt_answerer, None)
            result_manager.set_parameters(load_yaml_file(SEARCH_CONFIG_FILE) or {})
            await result_manager._handle_apply_result(
                apply_result,
                test_job,
                evaluation=collect_apply_result_metadata(easy_applier, submitted_resume_path),
            )
            logger.info("Standalone Easy Apply result persisted to output YAML")

            if easy_applier.already_applied_at_text:
                logger.info(
                    "Already-applied status date: "
                    f"{easy_applier.already_applied_at_text} ({easy_applier.already_applied_at})"
                )
            if submitted_resume_path:
                logger.info("Submitted resume attachment was recorded")

            if status == "Success":
                logger.info("✅ LinkedInEasyApplier test completed successfully!")
                return True
            if status == "Skip" and reason == "Already applied to this job":
                logger.info(
                    "✅ LinkedInEasyApplier test detected already-applied job successfully!"
                )
                return True
            else:
                logger.error(f"❌ LinkedInEasyApplier test failed - result is {status}: {reason}")
                return False

        except Exception as e:
            logger.error(f"❌ LinkedInEasyApplier test failed with error: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
        finally:
            # Keep browser open for manual inspection
            logger.info(
                "Test completed. Browser will remain open for 5 minutes for manual inspection..."
            )
            await async_pause(300, 300)
            try:
                await save_browser_session(context)
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

    print("\nTesting full LinkedInEasyApplier functionality...")
    job_url_or_id = sys.argv[1] if len(sys.argv) > 1 else None
    success = asyncio.run(test_easy_applier(job_url_or_id))
    if success:
        print("✅ LinkedInEasyApplier test passed!")
    else:
        print("❌ LinkedInEasyApplier test failed!")
