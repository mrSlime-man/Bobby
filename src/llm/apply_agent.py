import asyncio
import hashlib
import inspect
import json
import time
import os
import re
import signal
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from browser_use import Agent, Browser, ChatAnthropic, ChatGoogle, ChatOllama, ChatOpenAI, Tools
from browser_use.agent.views import ActionResult

from config.app_config import APPLY_AGENT_MODEL, HEADLESS_MODE, LLM_MODEL_TYPE
from config.app_config import (
    APPLY_AGENT_FALLBACK_MODEL,
    LLM_FALLBACK_ENABLED,
    LLM_PROVIDER_COOLDOWN_SEC,
    LLM_PROVIDER_MAX_RETRIES,
    LLM_PROVIDER_ORDER,
    GMAIL_APPLICATION_INTEGRATION,
    GMAIL_CREDENTIALS_PATH,
    GMAIL_RECEIPT_TIMEOUT_SEC,
    GMAIL_TOKEN_PATH,
    GMAIL_VERIFICATION_POLL_SEC,
    EXTERNAL_ATS_ENABLED,
    EXTERNAL_ATS_MAX_RECOVERY_ATTEMPTS,
    EXTERNAL_ATS_PAGE_TIMEOUT_SEC,
    EXTERNAL_ATS_NAVIGATION_TIMEOUT_SEC,
    EXTERNAL_ATS_AUTO_ACCOUNT_CREATION,
    EXTERNAL_ATS_RESUME_UPLOAD_ENABLED,
    EXTERNAL_ATS_SUBMISSION_VERIFICATION_ENABLED,
)
from config.constants import CUSTOM_COST_PER_TOKEN, LOG_DIR, RESUME_DIR, cost_per_token
from config.logger_config import logger
from src.dashboard.runtime import emit_event
from src.integrations.gmail_verification import (
    SECURITY_WORDS,
    GmailVerificationAmbiguous,
    GmailVerificationClient,
)
from src.llm.external_worker_state import (
    read_worker_state,
    submit_may_have_occurred,
    write_worker_state,
)
from src.llm.external_resume_upload import register_resume_upload_action
from src.llm.external_document import (
    ExternalDocumentGate,
    ExternalEmptyDocument,
    ExternalInitialNavigationFailed,
)
from src.llm.external_account_registration import (
    AccountRegistrationGuard,
    registration_facts_from_profile,
    registration_page_present,
)
from src.llm.provider_health import (
    ProviderFallbackState,
    circuit_is_open,
    classify_provider_error,
    open_circuit,
)
from src.llm.provider_config import (
    PROVIDER_SPECS,
    configured_provider_candidates,
    credential_for,
    endpoint_for,
    endpoint_is_valid,
)
from src.llm.ats_engine import (
    ATSRunState,
    ATSStage,
    SiteFamily,
    classify_application_entry_progress,
    detect_site_family,
    infer_stage,
    normalize_text,
    rank_application_entry_candidates,
    stage_evidence_from_visible_controls,
)
from src.pydantic_models.log_models import LLMCall
from src.utils.redaction import redact_text
from src.utils.candidate_profile import (
    CANDIDATE_PROFILE_PATH,
    load_candidate_profile,
    structured_application_facts,
)
from src.utils.run_context import get_run_id
from src.utils.runtime_control import runtime_controller
from src.utils.utils import append_yaml_file, get_ready_made_resume
from src.llm.external_apply_support import (
    NEEDS_HUMAN,
    SUBMITTED,
    UNVERIFIED,
    ats_prompt,
    collect_post_submit_evidence,
    credential_reference,
    detect_ats,
    detect_confirmation,
    find_validation_errors,
    ats_account_registration_state,
    get_or_create_ats_password,
    needs_human,
    needs_human_from_visible_controls,
    safe_job_id,
    save_landing_diagnostic,
    save_evidence,
    save_screenshot_b64,
)

# An admitted external worker gets a short natural drain on shutdown. The
# parent then requests graceful cancellation and finally enforces a hard bound.
EXTERNAL_SHUTDOWN_NATURAL_DRAIN_SECONDS = 45.0
EXTERNAL_SHUTDOWN_TERMINATE_SECONDS = 15.0


def kill_external_process_group(process) -> None:
    """Force-stop an isolated worker and any browser processes it owns."""
    if os.name == "posix" and isinstance(process.pid, int):
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError as error:
            logger.warning(
                "Could not end the isolated external worker process group; "
                f"falling back to the worker process: {error}"
            )
    process.kill()


def validate_custom_action_schemas(tools: Tools) -> None:
    """Fail before Browser Use starts if a custom action has an empty LLM schema.

    Browser Use 0.12.6's Gemini adapter inserts an ``_placeholder`` field into
    empty object schemas, while the original Pydantic action model rejects that
    field. Requiring a meaningful argument avoids that incompatible schema.
    """
    actions = tools.registry.registry.actions
    empty = [
        name
        for name, action in actions.items()
        if not action.param_model.model_json_schema().get("properties")
    ]
    if empty:
        raise RuntimeError(
            "Browser Use custom actions have empty schemas: " + ", ".join(sorted(empty))
        )


def unresolved_submission_error(final_submit_attempted: bool) -> str:
    """Keep pre-submit failures out of the unverified-after-submit bucket."""
    if final_submit_attempted:
        return (
            "APPLICATION_NOT_VERIFIED: A final submit attempt was reported, "
            "but independent confirmation was unavailable."
        )
    return (
        "EXTERNAL_AGENT_FAILED: The external flow ended before a credible " "final submit attempt."
    )


def completed_semantic_form_action_keys(agent_instance) -> tuple[str, ...]:
    """Return non-sensitive identities for newly completed ordinary form actions.

    Browser Use records the action history after each model step. URL and
    visible page text intentionally exclude populated input values, so a real
    field entry can otherwise look indistinguishable from a no-op to the
    same-page guard. Keep only the action type and DOM index: candidate text,
    selected values, and action results never leave the browser history here.
    """

    history = getattr(agent_instance, "history", None)
    entries = getattr(history, "history", None)
    if not isinstance(entries, list) or not entries:
        return ()
    latest = entries[-1]
    model_output = getattr(latest, "model_output", None)
    actions = getattr(model_output, "action", None) or ()
    results = getattr(latest, "result", None) or ()
    semantic_actions = {"input", "select_dropdown", "click"}
    keys: list[str] = []

    for position, action in enumerate(actions):
        result = results[position] if position < len(results) else None
        if getattr(result, "error", None):
            continue
        try:
            payload = action.model_dump(exclude_none=True, mode="json")
        except Exception:
            continue
        if not isinstance(payload, dict) or len(payload) != 1:
            continue
        action_name, action_arguments = next(iter(payload.items()))
        if action_name not in semantic_actions or not isinstance(action_arguments, dict):
            continue
        index = action_arguments.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            continue
        key = f"{action_name}:{index}"
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def classify_worker_result(return_code: int, state: dict) -> tuple[str, str]:
    """Classify a worker without losing a durable final-submit state."""
    if return_code == 0 and state.get("phase") == "submitted":
        return ("Success", "")
    if return_code == 10 or submit_may_have_occurred(state):
        return (
            "Error",
            "UNVERIFIED_AFTER_SUBMIT: final submit was reached but confirmation was not established",
        )
    if return_code == 11:
        return ("Error", "NEEDS_HUMAN: external application requires human action")
    if return_code == 12:
        return ("Skip", "NOT_ELIGIBLE: ATS reported an evidence-backed mismatch")
    if return_code == 130 or state.get("phase") == "cancelled_before_submit":
        return (
            "Cancelled",
            "CANCELLED_BY_SHUTDOWN: external application stopped before final submit",
        )
    # Browser Use can raise a later wrapper error (for example, the guarded
    # resume-upload stop hook) after a provider outage was already observed.
    # Prefer that durable provider observation over the wrapper's terminal
    # error class so a pre-submit external outage is not misreported as a
    # Bobby workflow defect.
    provider_error_class = str(state.get("provider_error_class") or "").strip()
    error_class = (
        "EXTERNAL_PROVIDER_UNAVAILABLE"
        if provider_error_class
        else str(state.get("error_class") or "").strip().upper()
    )
    phase = str(state.get("phase") or "unknown").strip()
    ats = str(state.get("ats") or "generic").strip()
    submit_attempted = bool(state.get("submit_attempted"))
    if error_class == "EXTERNAL_PROVIDER_UNAVAILABLE":
        return (
            "Error",
            "NEEDS_HUMAN: EXTERNAL_BLOCKER: external application provider was unavailable "
            "before final submit | internal_code=EXTERNAL_PROVIDER_UNAVAILABLE "
            f"phase={phase} ats={ats} submit_attempted={str(submit_attempted).lower()} "
            "retryable=true",
        )
    return (
        "Error",
        "TECHNICAL_FAILURE: external apply worker exited before final submit "
        f"| internal_code={error_class or 'WORKER_EXIT'} code={return_code} "
        f"phase={phase} ats={ats} submit_attempted={str(submit_attempted).lower()} "
        "retryable=true",
    )


def should_poll_for_receipt(*, submit_attempted: bool, post_submit_signal: bool = False) -> bool:
    """Receipt correlation is useful only when an application may have been sent."""
    return bool(submit_attempted or post_submit_signal)


class ExternalATSProgressStalled(RuntimeError):
    """The external worker exhausted its safe same-page progress budget."""


_STRONG_FINAL_SUBMIT_CONTROL = re.compile(
    r"\b(?:submit|send|finish|complete)\s+(?:(?:your|my|the)\s+)?(?:job\s+)?application\b"
    r"(?!\s+(?:feedback|status|link|email|via|by)\b)"
)


def is_strong_final_submit_control(semantic: str) -> bool:
    """Recognize unambiguous final-application labels at every form stage.

    Some employer-hosted forms expose their final dispatch button beside a
    resume section instead of a separately detected Review stage.  Those
    labels must still cross Bobby's one-way submit guard, never the generic
    click action.
    """

    return bool(_STRONG_FINAL_SUBMIT_CONTROL.search(normalize_text(semantic)))


def classify_external_provider_failure(agent_error: object, agent: object | None = None) -> str | None:
    """Classify provider failure even when Browser Use surfaces a hook error.

    Browser Use catches model/provider exceptions inside ``Agent.step()``,
    records them in its history, and then invokes Bobby's ``on_step_end`` hook
    outside that step's exception handler.  A deterministic ATS progress guard
    raised by that hook can therefore be the exception seen by Bobby even
    though the history contains the real provider failure.  Inspect only the
    privacy-safe error class; never log or return raw provider text.
    """

    direct = classify_provider_error(agent_error)
    if direct:
        return direct
    if agent is None:
        return None
    try:
        history = getattr(agent, "history", None)
        errors = history.errors() if history is not None else ()
        history_text = " ".join(str(item) for item in errors if item)
    except Exception:
        return None
    return classify_provider_error(history_text)


class ApplyAgent:
    def __init__(
        self,
        api_key: str = None,
        browser_storage_state: str = None,
        llm_api_url: str = None,
        user_email: str = None,
        worker_state_path: str = None,
    ) -> None:
        self.api_key = api_key
        self.user_email = user_email
        self.model = APPLY_AGENT_MODEL
        self.model_type = LLM_MODEL_TYPE
        self.primary_api_key = api_key
        self.llm_api_url = llm_api_url
        self.llm = self.select_model_type(self.model_type, self.llm_api_url, model=self.model)
        self.provider_order = configured_provider_candidates(
            self.model_type,
            LLM_PROVIDER_ORDER,
            primary_api_key=api_key,
            configured_url=llm_api_url,
            enabled=LLM_FALLBACK_ENABLED,
            max_attempts=LLM_PROVIDER_MAX_RETRIES,
        )
        # Keep a usable primary candidate for test doubles and callers that
        # construct an agent before loading credentials.  A real worker always
        # supplies the Gemini key and therefore uses the filtered chain above.
        if not self.provider_order and self.model_type:
            self.provider_order = (self.model_type,)
        self.calls_log = os.path.join(Path(LOG_DIR), "llm_api_calls.yaml")
        self.agent = None
        self.run_id = get_run_id()
        self.worker_state_path = worker_state_path or os.getenv("BOBBY_EXTERNAL_STATE_FILE")
        self.resume_readable = None
        self.browser_storage_state = str(Path(browser_storage_state).absolute())
        storage_state = (
            self.browser_storage_state if Path(self.browser_storage_state).exists() else None
        )
        if storage_state is None:
            logger.info("External browser is starting without persisted session state")
        self.storage_state = storage_state

    def _create_browser(self) -> Browser:
        # Agent.run() closes non-persistent sessions before returning history.
        # Keep this worker's browser until final evidence has been captured.
        return Browser(headless=HEADLESS_MODE, storage_state=self.storage_state, keep_alive=True)

    def select_model_type(
        self, model_type: str, llm_api_url: str, model: str | None = None
    ) -> None:
        """Select the model to use."""
        model_type = str(model_type or "").casefold()
        spec = PROVIDER_SPECS.get(model_type)
        model_name = model or (spec.model if spec else self.model)
        provider_key = credential_for(model_type, self.primary_api_key)
        if model_type == "gemini":
            if not provider_key:
                raise ValueError("API key is required for Gemini model")
            llm = ChatGoogle(api_key=provider_key, model=model_name)
        elif model_type == "openai":
            if not provider_key:
                raise ValueError("API key is required for OpenAI model")
            llm = ChatOpenAI(api_key=provider_key, model=model_name, reasoning_effort="minimal")
        elif model_type == "claude":
            if not provider_key:
                raise ValueError("API key is required for Claude model")
            llm = ChatAnthropic(api_key=provider_key, model=model_name)
        elif model_type == "ollama":
            endpoint = endpoint_for(model_type, llm_api_url) or None
            llm = ChatOllama(model=model_name, host=endpoint)
        elif model_type == "openrouter":
            if not provider_key:
                raise ValueError("API key is required for OpenRouter model")
            llm = ChatOpenAI(
                api_key=provider_key,
                model=model_name,
                base_url="https://openrouter.ai/api/v1",
            )
        elif model_type == "nvidia_nim":
            if not provider_key:
                raise ValueError("API key is required for NVIDIA NIM model")
            llm = ChatOpenAI(
                api_key=provider_key,
                model=model_name,
                base_url="https://integrate.api.nvidia.com/v1",
            )
        elif model_type == "groq":
            if not provider_key:
                raise ValueError("API key is required for Groq model")
            llm = ChatOpenAI(
                api_key=provider_key,
                model=model_name,
                base_url="https://api.groq.com/openai/v1",
            )
        elif model_type == "cerebras":
            if not provider_key:
                raise ValueError("API key is required for Cerebras model")
            llm = ChatOpenAI(
                api_key=provider_key,
                model=model_name,
                base_url="https://api.cerebras.ai/v1",
            )
        elif model_type == "openai_compatible":
            endpoint = endpoint_for(model_type, llm_api_url)
            if not provider_key or not endpoint_is_valid(endpoint):
                raise ValueError("llm_api_url is required for openai_compatible model type")
            llm = ChatOpenAI(
                api_key=provider_key,
                model=model_name,
                base_url=endpoint,
            )
        else:
            raise ValueError(f"Unsupported model type: {model_type}")
        return llm

    def set_resume(self, resume_readable: str) -> None:
        """Add resume for analysis."""
        self.resume_readable = resume_readable

    async def apply(
        self,
        job_url: str,
        job_title: str = "",
        company_name: str = "",
        linkedin_url: str = "",
    ) -> None:
        """Apply to the job using AI Agent"""
        if not EXTERNAL_ATS_ENABLED:
            raise RuntimeError("EXTERNAL_ATS_DISABLED: external ATS applications are disabled")
        if not EXTERNAL_ATS_SUBMISSION_VERIFICATION_ENABLED:
            raise RuntimeError(
                "EXTERNAL_ATS_CONFIGURATION_INVALID: submission verification is a safety invariant"
            )
        application_started_at = time.time()
        ats_name = detect_ats(job_url)
        site_family = detect_site_family(job_url)
        ats_state = ATSRunState(
            site_family=site_family,
            max_recovery_attempts=EXTERNAL_ATS_MAX_RECOVERY_ATTEMPTS,
        )
        human_boundary_reason: str | None = None
        application_id = f"app-{safe_job_id(linkedin_url or job_url)}"

        def emit_apply_event(event_type: str, message: str, **details) -> None:
            """Publish safe application/workflow context without form values."""

            emit_event(
                event_type,
                message,
                application_id=application_id,
                job_id=safe_job_id(linkedin_url or job_url),
                job_title=job_title,
                company_name=company_name,
                linkedin_url=linkedin_url,
                external_url=job_url,
                ats=ats_state.site_family.value,
                application_type="EXTERNAL_ATS",
                **details,
            )

        def mark_human_boundary(reason: str) -> RuntimeError:
            """Preserve a human-required action across Browser Use wrappers."""

            nonlocal human_boundary_reason
            human_boundary_reason = reason
            return RuntimeError(f"APPLICATION_NEEDS_HUMAN: {reason}")

        def log_ats_stage(stage: ATSStage, status: str) -> None:
            # Structural telemetry only: no URLs, page text, answers, or PII.
            logger.info(
                "ATS_STAGE | "
                f"site={ats_state.site_family.value} stage={stage.value} status={status}"
            )
            emit_apply_event(
                "ats_workflow_step",
                f"External ATS workflow: {stage.value}",
                last_workflow_step=stage.value,
                workflow_status=status,
            )

        def log_ats_recovery(reason: str, strategy: str, success: bool) -> None:
            logger.info(
                "ATS_RECOVERY | "
                f"site={ats_state.site_family.value} reason={reason} "
                f"strategy={strategy} success={str(bool(success)).lower()}"
            )

        def record_provider_failure_state(provider: str, error_class: str) -> None:
            """Persist provider failure context before a later wrapper error."""

            if not self.worker_state_path:
                return
            current = read_worker_state(self.worker_state_path)
            write_worker_state(
                self.worker_state_path,
                current.get("phase") or "pre_submit",
                run_id=self.run_id,
                provider=provider,
                provider_error_class=str(error_class or "").strip(),
            )

        log_ats_stage(ATSStage.LANDING, "entered")
        ats_specific_instructions = ats_prompt(job_url)
        # Replaying previous form answers can disclose candidate data to an
        # unrelated employer.  External applications use only the current
        # profile and resume; learned-answer storage is deliberately disabled.
        reusable_answers = "Persistent learned application answers are disabled for privacy."

        logger.info(
            f"External ATS detected: {ats_name}; "
            f"LinkedIn company={company_name!r}; title={job_title!r}"
        )

        gmail_client = None
        if GMAIL_APPLICATION_INTEGRATION:
            try:
                gmail_client = GmailVerificationClient(
                    credentials_path=GMAIL_CREDENTIALS_PATH,
                    token_path=GMAIL_TOKEN_PATH,
                )
                await asyncio.to_thread(
                    gmail_client.connect,
                    False,
                )
                logger.info("Gmail integration enabled")
            except Exception as gmail_error:
                # Provider messages can include mailbox-specific details.  The
                # worker needs only a safe availability signal here; receipt
                # verification remains optional and cannot affect submission.
                logger.warning(
                    "Gmail integration unavailable | " f"error_class={type(gmail_error).__name__}"
                )
                gmail_client = None
        resume_path = get_ready_made_resume()
        if not resume_path:
            raise RuntimeError(
                "READY_MADE_RESUME_PATH is missing or does not point to a supported resume file"
            )

        resume_pdf_path = str(resume_path)

        candidate_profile = load_candidate_profile(CANDIDATE_PROFILE_PATH)
        application_profile = json.dumps(
            structured_application_facts(candidate_profile),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        account_registration_guard = AccountRegistrationGuard(
            registration_facts_from_profile(candidate_profile)
        )

        emit_apply_event(
            "agent_apply_started",
            "External apply agent started",
            last_workflow_step="worker_started",
        )

        # Browser Use's generic actions can bypass irreversible boundaries:
        # generic click can submit/create an account, generic input can type a
        # generated password, and evaluate/send_keys can bypass both.  Keep a
        # small read-only surface and replace state-changing controls with
        # guarded actions below.  The resume uploader is likewise replaced by
        # the strict real-file-input action.
        tools = Tools(
            exclude_actions=[
                "upload_file",
                "click",
                "input",
                "select_dropdown",
                "evaluate",
                "send_keys",
                "navigate",
                "go_back",
                "search",
                "save_as_pdf",
                "read_file",
                "replace_file",
                "write_file",
            ]
        )
        # Custom action registration honours the exclusion list.  Re-enable
        # only the three guarded names, leaving the unsafe built-ins absent.
        for action_name in ("click", "input", "select_dropdown"):
            tools.registry.exclude_actions.remove(action_name)

        if self.worker_state_path:
            write_worker_state(
                self.worker_state_path,
                "pre_submit",
                run_id=self.run_id,
                submit_attempted=False,
                job_id=safe_job_id(linkedin_url or job_url),
                ats=ats_name,
            )

        resume_upload_guard = register_resume_upload_action(
            tools,
            resume_pdf_path,
            enabled=EXTERNAL_ATS_RESUME_UPLOAD_ENABLED,
        )

        async def run_account_registration(browser_session) -> str:
            """Run the single guarded ordinary registration action safely."""

            if not EXTERNAL_ATS_AUTO_ACCOUNT_CREATION:
                return "CONFIGURATION_DISABLED"
            result = await account_registration_guard.complete(browser_session)
            if result.status == "CANCELLED_BY_SHUTDOWN":
                raise RuntimeError("CANCELLED_BY_SHUTDOWN: account registration not dispatched")
            logger.info(
                "ATS account-registration action completed | "
                f"status={result.status} actions={result.actions} "
                f"detail={result.detail or 'none'}"
            )
            account_event = (
                "account_created"
                if result.status == "ACCOUNT_CREATED"
                else "account_reused"
                if result.status == "AUTH_ROUTED_EXISTING_ACCOUNT"
                else "account_required"
            )
            emit_apply_event(
                account_event,
                f"ATS account state: {result.status}",
                account_required=True,
                account_created=result.status == "ACCOUNT_CREATED",
                account_reused=result.status == "AUTH_ROUTED_EXISTING_ACCOUNT",
                account_identifier=account_registration_guard.facts.email,
                account_email=account_registration_guard.facts.email,
                credential_ref=credential_reference(
                    job_url, account_registration_guard.facts.email
                ),
                last_workflow_step=ATSStage.ACCOUNT_CREATION.value,
            )
            if result.status == "ACCOUNT_CREATED":
                log_ats_stage(ATSStage.ACCOUNT_CREATION, "submitted")
                # Some generic ATSs return to the job page after registration
                # and expose the same Apply Now CTA again.  The model may
                # mistake that page for an already-progressed auth state and
                # loop without re-entering the application.  Re-run the
                # bounded, semantic entry resolver once after a confirmed
                # account transition; this is safe because it cannot select
                # final submit or another registration control.
                entry_attempted.clear()
                await deterministic_application_entry(
                    browser_session, ATSStage.APPLY_ENTRY
                )
                return "ACCOUNT_CREATED"
            if result.status == "AUTH_ROUTED_EXISTING_ACCOUNT":
                log_ats_stage(ATSStage.AUTH, "existing_account")
                return "AUTH_ROUTED_EXISTING_ACCOUNT"
            return result.status

        def control_semantics(node) -> str:
            """Read control metadata only for local safety classification."""

            attributes = getattr(node, "attributes", None) or {}
            node_text = ""
            try:
                node_text = str(node.get_meaningful_text_for_llm() or "")
            except Exception:
                node_text = ""
            values = [
                node_text,
                *(
                    str(attributes.get(key) or "")
                    for key in (
                        "aria-label",
                        "title",
                        "value",
                        "name",
                        "id",
                        "placeholder",
                        "type",
                    )
                ),
            ]
            return " ".join(" ".join(values).casefold().split())

        entry_attempted: set[str] = set()
        entry_recovery_attempts = 0
        last_entry_snapshot: dict[str, object] | None = None
        terminal_diagnostic_saved = False
        document_gate = ExternalDocumentGate(
            job_url, timeout_seconds=EXTERNAL_ATS_NAVIGATION_TIMEOUT_SEC
        )

        async def read_visible_control_snapshot(
            page,
            *,
            timeout_seconds: float,
        ) -> tuple[list[dict[str, object]], list[dict[str, object]], int, int]:
            """Read only safe control and form-state semantics for diagnostics.

            The browser-side snapshot deliberately returns *state booleans*,
            not form values. A terminal artifact can show that a required
            field remained empty without persisting candidate facts,
            passwords, file names, selected disclosure answers, or page HTML.
            """

            payload = await asyncio.wait_for(
                page.evaluate(
                    """() => JSON.stringify({
                        controls: [...document.querySelectorAll(
                            'button, a, input[type="button"], input[type="submit"], [role="button"], [role="link"]'
                        )].map((el, index) => {
                            return {
                                index,
                                label: (el.innerText || el.textContent || '').trim(),
                                href: el.href || '',
                                kind: el.tagName || '',
                                role: el.getAttribute('role') || '',
                                aria_label: el.getAttribute('aria-label') || '',
                                title: el.getAttribute('title') || '',
                                value: el.value || '',
                                name: el.getAttribute('name') || '',
                                id: el.id || '',
                                visible: (() => {
                                    const style = getComputedStyle(el);
                                    const rect = el.getBoundingClientRect();
                                    return style.display !== 'none' && style.visibility !== 'hidden'
                                        && rect.width > 0 && rect.height > 0;
                                })()
                            };
                        }),
                        form_fields: (() => {
                            const isVisible = el => {
                                const style = getComputedStyle(el);
                                const rect = el.getBoundingClientRect();
                                return style.display !== 'none' && style.visibility !== 'hidden'
                                    && rect.width > 0 && rect.height > 0;
                            };
                            const fieldLabel = el => {
                                const identifier = el.id || '';
                                const linked = identifier
                                    ? [...document.querySelectorAll('label')]
                                        .find(label => label.htmlFor === identifier)
                                    : null;
                                const wrapping = el.closest('label');
                                return (el.getAttribute('aria-label') || linked?.innerText
                                    || wrapping?.innerText || el.getAttribute('placeholder') || '')
                                    .trim().slice(0, 240);
                            };
                            const fields = [...document.querySelectorAll('input, select, textarea')]
                                .filter(el => {
                                    const type = String(el.type || '').toLowerCase();
                                    return isVisible(el) && type !== 'hidden' && type !== 'password'
                                        && !['button', 'submit', 'reset', 'image', 'checkbox', 'radio'].includes(type);
                                })
                                .map((el, index) => ({
                                    index,
                                    kind: el.tagName || '',
                                    input_type: String(el.type || '').toLowerCase(),
                                    label: fieldLabel(el),
                                    aria_label: el.getAttribute('aria-label') || '',
                                    placeholder: el.getAttribute('placeholder') || '',
                                    required: Boolean(el.required || el.getAttribute('aria-required') === 'true'),
                                    enabled: !el.disabled,
                                    read_only: Boolean(el.readOnly),
                                    has_value: String(el.value || '').trim().length > 0,
                                    invalid: Boolean(el.validity?.valid === false || el.getAttribute('aria-invalid') === 'true'),
                                    option_count: el.tagName === 'SELECT' ? el.options.length : 0,
                                }));
                            const choiceGroups = new Map();
                            [...document.querySelectorAll('input[type="checkbox"], input[type="radio"]')]
                                .filter(el => isVisible(el))
                                .forEach((el, index) => {
                                    const key = `${el.type}:${el.name || fieldLabel(el) || index}`;
                                    const prior = choiceGroups.get(key) || {
                                        index, kind: 'CHOICE_GROUP', input_type: String(el.type || '').toLowerCase(),
                                        label: fieldLabel(el), aria_label: el.getAttribute('aria-label') || '',
                                        placeholder: '', required: false, enabled: false, read_only: false,
                                        has_value: false, invalid: false, option_count: 0,
                                    };
                                    prior.required = prior.required || Boolean(el.required || el.getAttribute('aria-required') === 'true');
                                    prior.enabled = prior.enabled || !el.disabled;
                                    prior.has_value = prior.has_value || Boolean(el.checked);
                                    prior.invalid = prior.invalid || el.getAttribute('aria-invalid') === 'true';
                                    prior.option_count += 1;
                                    choiceGroups.set(key, prior);
                                });
                            return [...fields, ...choiceGroups.values()];
                        })(),
                        editable_field_count: [...document.querySelectorAll('input, select, textarea')]
                            .filter(el => {
                                const type = String(el.type || '').toLowerCase();
                                const semantic = [el.name, el.id, el.getAttribute('aria-label'), el.getAttribute('placeholder')]
                                    .filter(Boolean).join(' ').toLowerCase();
                                return !el.disabled && !el.readOnly
                                    && !['hidden', 'button', 'submit', 'reset', 'image', 'checkbox', 'radio'].includes(type)
                                    && type !== 'search' && !/\\bsearch\\b/.test(semantic)
                                    && (() => {
                                        const style = getComputedStyle(el);
                                        const rect = el.getBoundingClientRect();
                                        return style.display !== 'none' && style.visibility !== 'hidden'
                                            && rect.width > 0 && rect.height > 0;
                                    })();
                            }).length,
                        frame_count: document.querySelectorAll('iframe, frame').length
                    })"""
                ),
                timeout=timeout_seconds,
            )
            decoded_payload = json.loads(payload) if isinstance(payload, str) else payload
            if isinstance(decoded_payload, dict):
                raw_controls = decoded_payload.get("controls", [])
                raw_form_fields = decoded_payload.get("form_fields", [])
                raw_frame_count = decoded_payload.get("frame_count", 0)
                raw_editable_field_count = decoded_payload.get("editable_field_count", 0)
            else:
                # Preserve compatibility with a Browser Use backend that
                # returns the former control-list payload directly.
                raw_controls = decoded_payload
                raw_form_fields = []
                raw_frame_count = 0
                raw_editable_field_count = 0
            if not isinstance(raw_controls, list):
                raw_controls = []
            if not isinstance(raw_form_fields, list):
                raw_form_fields = []
            try:
                frame_count = max(0, int(raw_frame_count))
            except (TypeError, ValueError):
                frame_count = 0
            try:
                editable_field_count = max(0, int(raw_editable_field_count))
            except (TypeError, ValueError):
                editable_field_count = 0
            return raw_controls, raw_form_fields, frame_count, editable_field_count

        async def visible_security_reason(
            page, raw_controls: list[dict[str, object]] | None = None
        ) -> str:
            """Return a visible CAPTCHA/security marker without reading form values."""

            if raw_controls is None:
                try:
                    raw_controls, _raw_form_fields, _frame_count, _editable_field_count = (
                        await read_visible_control_snapshot(
                            page,
                            timeout_seconds=min(EXTERNAL_ATS_NAVIGATION_TIMEOUT_SEC, 5.0),
                        )
                    )
                except Exception as error:
                    logger.info(
                        "ATS_SECURITY_CONTROL_SCAN_UNAVAILABLE | "
                        f"error_class={type(error).__name__}"
                    )
                    return ""
            required, reason = needs_human_from_visible_controls(raw_controls)
            if required and gmail_client is not None and any(
                phrase in str(reason).casefold()
                for phrase in ("email verification", "verify your email", "verification code", "confirmation code")
            ):
                # A correlated Gmail/OAuth flow can handle ordinary ATS email
                # verification. MFA, CAPTCHA, and device checks remain human
                # boundaries because they are not mailbox verification.
                return ""
            return reason if required else ""

        async def capture_terminal_progress_diagnostic(
            browser_session,
            *,
            stage: ATSStage,
            reason: str,
        ) -> None:
            """Persist one sanitized pre-submit terminal snapshot without changing state."""

            nonlocal terminal_diagnostic_saved
            if terminal_diagnostic_saved:
                return
            try:
                page = await browser_session.must_get_current_page()
                snapshot = (
                    last_entry_snapshot or {}
                    if stage in {ATSStage.LANDING, ATSStage.APPLY_ENTRY}
                    else {}
                )
                current_url = str(snapshot.get("url") or await page.get_url() or "")
                body = str(
                    snapshot.get("body")
                    or await page.evaluate("() => document.body ? document.body.innerText : ''")
                    or ""
                )
                # Landing recovery already holds a bounded, pre-action
                # snapshot. Later form stages previously persisted no control
                # evidence at all, which made a repeated account/disclosure/
                # submit state impossible to diagnose without replaying a real
                # vacancy. Collect only control metadata once at the terminal
                # boundary; ``save_landing_diagnostic`` strips values before
                # anything is written to disk.
                if not snapshot and reason == "same_page_state":
                    try:
                        raw_controls, raw_form_fields, frame_count, editable_field_count = await read_visible_control_snapshot(
                            page,
                            timeout_seconds=min(EXTERNAL_ATS_NAVIGATION_TIMEOUT_SEC, 5.0),
                        )
                        snapshot = {
                            "url": current_url,
                            "body": body,
                            "raw_controls": raw_controls,
                            "raw_form_fields": raw_form_fields,
                            "frame_count": frame_count,
                            "editable_field_count": editable_field_count,
                        }
                    except Exception as snapshot_error:
                        logger.info(
                            "TERMINAL_CONTROL_SNAPSHOT_UNAVAILABLE | "
                            f"error_class={type(snapshot_error).__name__}"
                        )
                try:
                    title = str(await page.get_title() or "")
                except Exception:
                    title = ""

                current_target = getattr(page, "_target_id", None)
                tab_snapshot: list[dict[str, object]] = []
                try:
                    tabs = await browser_session.get_tabs()
                except Exception:
                    tabs = ()
                for tab in tabs if isinstance(tabs, (list, tuple)) else ():
                    tab_snapshot.append(
                        {
                            "selected": getattr(tab, "target_id", None) == current_target,
                            "url": str(getattr(tab, "url", "") or ""),
                        }
                    )

                save_landing_diagnostic(
                    job_id=safe_job_id(linkedin_url or job_url),
                    worker_id=f"{self.run_id}-{os.getpid()}",
                    reason=reason,
                    url=current_url,
                    title=title,
                    body=body,
                    ats=ats_state.site_family.value,
                    stage=stage,
                    controls=snapshot.get("raw_controls", ()),
                    form_fields=snapshot.get("raw_form_fields", ()),
                    tabs=tab_snapshot,
                    frame_count=snapshot.get("frame_count", 0),
                    editable_field_count=snapshot.get("editable_field_count", 0),
                    readiness=document_gate.observation,
                )
                terminal_diagnostic_saved = True
                logger.info(
                    "TERMINAL_DIAGNOSTIC_SAVED | "
                    f"stage={stage.value} reason={reason}"
                )
            except Exception as error:
                # Diagnostics must not mask the original bounded pre-submit
                # failure or trigger an additional browser action.
                logger.warning(
                    "TERMINAL_DIAGNOSTIC_WRITE_FAILED | "
                    f"error_class={type(error).__name__}"
                )

        async def switch_to_application_tab(browser_session, current_url: str) -> bool:
            """Adopt a newly opened application tab before asking the model to continue."""

            try:
                current_page = await browser_session.must_get_current_page()
                current_target = getattr(current_page, "_target_id", None)
                tabs = await browser_session.get_tabs()
                pages = await browser_session.get_pages()
                page_by_target = {
                    getattr(page, "_target_id", None): page for page in pages
                }
                for tab in reversed(tabs):
                    target_id = getattr(tab, "target_id", None)
                    tab_url = str(getattr(tab, "url", "") or "")
                    if target_id == current_target or not tab_url.startswith(("http://", "https://")):
                        continue
                    page = page_by_target.get(target_id)
                    if page is None:
                        continue
                    body = str(
                        await page.evaluate(
                            "() => document.body ? document.body.innerText : ''"
                        )
                        or ""
                    )
                    family = detect_site_family(tab_url, body)
                    stage_hint = infer_stage(tab_url, body)
                    if tab_url == current_url and family is SiteFamily.GENERIC:
                        continue
                    if family is SiteFamily.GENERIC and stage_hint is ATSStage.LANDING:
                        continue
                    from browser_use.browser.events import SwitchTabEvent

                    event = browser_session.event_bus.dispatch(
                        SwitchTabEvent(target_id=target_id)
                    )
                    await event
                    await event.event_result(raise_if_any=True, raise_if_none=False)
                    logger.info(
                        "ATS_CONTEXT_SWITCH | reason=new_application_tab "
                        f"stage={stage_hint.value} site={family.value}"
                    )
                    return True
            except Exception as error:
                logger.warning(
                    "ATS_CONTEXT_SWITCH failed safely | " f"error_class={type(error).__name__}"
                )
            return False

        async def deterministic_application_entry(browser_session, stage: ATSStage) -> bool:
            """Click one high-confidence pre-application control after a stale model step."""

            nonlocal entry_recovery_attempts, last_entry_snapshot
            if stage not in {ATSStage.LANDING, ATSStage.APPLY_ENTRY}:
                return False
            entry_recovery_attempts += 1
            logger.info(
                "APPLICATION_ENTRY_SCAN | "
                f"stage={stage.value} attempt={entry_recovery_attempts}"
            )
            logger.info(
                "ATS_ENTRY_ATTEMPT | "
                f"stage={stage.value} attempt={entry_recovery_attempts}"
            )
            try:
                page = await browser_session.must_get_current_page()
                current_url = await page.get_url()
                body = str(
                    await page.evaluate("() => document.body ? document.body.innerText : ''")
                    or ""
                )
                human_required, human_reason = needs_human(body)
                if human_required:
                    logger.info(
                        "ATS_ENTRY_BLOCKED | reason=security_challenge "
                        f"class={human_reason}"
                    )
                    return False
                if await switch_to_application_tab(browser_session, current_url):
                    return True
                raw_controls, raw_form_fields, frame_count, editable_field_count = await read_visible_control_snapshot(
                    page,
                    timeout_seconds=EXTERNAL_ATS_NAVIGATION_TIMEOUT_SEC,
                )
                last_entry_snapshot = {
                    "url": str(current_url or ""),
                    "body": body,
                    "raw_controls": raw_controls,
                    "raw_form_fields": raw_form_fields,
                    "frame_count": frame_count,
                    "editable_field_count": editable_field_count,
                }
                controls = [
                    item
                    for item in raw_controls
                    if isinstance(item, dict) and item.get("visible")
                ]
                candidates = rank_application_entry_candidates(controls, stage=stage)
                logger.info(
                    "APPLICATION_ENTRY_CANDIDATES | "
                    f"controls={len(controls)} candidates={len(candidates)}"
                )
                logger.info(
                    "ATS_ENTRY_CANDIDATES | "
                    f"controls={len(controls)} candidates={len(candidates)}"
                )
                for candidate in candidates:
                    matching = next(
                        (
                            control
                            for control in controls
                            if candidate.label.casefold()
                            in normalize_text(
                                " ".join(
                                    str(control.get(key, "") or "")
                                    for key in (
                                        "label",
                                        "aria_label",
                                        "title",
                                        "value",
                                        "name",
                                        "id",
                                    )
                                )
                            )
                            and str(control.get("href") or "") == candidate.href
                        ),
                        None,
                    )
                    if matching is None:
                        continue
                    key = (candidate.label, candidate.href)
                    if key in entry_attempted:
                        continue
                    entry_attempted.add(key)
                    logger.info(
                        "APPLICATION_ENTRY_SELECTED | "
                        f"rank={candidates.index(candidate) + 1} "
                        f"confidence={candidate.confidence} kind={candidate.kind.value}"
                    )
                    try:
                        await asyncio.wait_for(
                            page.evaluate(
                                """(index) => {
                                    const els = [...document.querySelectorAll(
                                        'button, a, input[type="button"], input[type="submit"], [role="button"], [role="link"]'
                                    )];
                                    const el = els[index];
                                    if (!el) return false;
                                    el.click();
                                    return true;
                                }""",
                                matching.get("index"),
                            ),
                            timeout=EXTERNAL_ATS_NAVIGATION_TIMEOUT_SEC,
                        )
                        logger.info(
                            "APPLICATION_ENTRY_ACTION | result=clicked "
                            f"confidence={candidate.confidence} kind={candidate.kind.value}"
                        )
                        logger.info(
                            "ATS_ENTRY | result=clicked "
                            f"confidence={candidate.confidence} kind={candidate.kind.value}"
                        )
                        await asyncio.sleep(1.0)
                        new_tab = await switch_to_application_tab(browser_session, current_url)
                        logger.info(
                            "APPLICATION_ENTRY_NEW_PAGE | "
                            f"detected={str(bool(new_tab)).lower()}"
                        )
                        new_page = await browser_session.must_get_current_page()
                        new_url = await new_page.get_url()
                        new_body = str(
                            await new_page.evaluate(
                                "() => document.body ? document.body.innerText : ''"
                            )
                            or ""
                        )
                        new_stage_evidence = None
                        try:
                            new_raw_controls, _new_raw_form_fields, _new_frame_count, new_editable_field_count = (
                                await read_visible_control_snapshot(
                                    new_page,
                                    timeout_seconds=min(
                                        EXTERNAL_ATS_NAVIGATION_TIMEOUT_SEC, 5.0
                                    ),
                                )
                            )
                            new_stage_evidence = stage_evidence_from_visible_controls(
                                new_raw_controls,
                                editable_field_count=new_editable_field_count,
                            )
                            logger.debug(
                                "ATS_STAGE_EVIDENCE | source=entry_transition "
                                f"entry={str(new_stage_evidence.has_strong_application_entry).lower()} "
                                f"editable_fields={new_stage_evidence.editable_field_count}"
                            )
                        except Exception as evidence_error:
                            # A failed post-click structure scan must not turn
                            # a real transition into a blind retry. The existing
                            # semantic comparison and bounded loop guard retain
                            # their safe fallback behavior.
                            logger.info(
                                "ATS_STAGE_EVIDENCE_UNAVAILABLE | source=entry_transition "
                                f"error_class={type(evidence_error).__name__}"
                            )
                        transition = classify_application_entry_progress(
                            current_url,
                            body,
                            new_url,
                            new_body,
                            previous_stage=stage,
                            previous_site_family=ats_state.site_family,
                            next_evidence=new_stage_evidence,
                        )
                        logger.info(
                            "APPLICATION_ENTRY_NAVIGATION | "
                            f"changed={str(transition.progressed).lower()} "
                            f"reason={transition.reason}"
                        )
                        if transition.progressed:
                            if transition.site_family is not SiteFamily.GENERIC:
                                ats_state.site_family = transition.site_family
                            ats_state.record_progress(
                                new_url, new_body, stage=transition.stage
                            )
                            logger.info(
                                "APPLICATION_ENTRY_PROGRESS | "
                                f"stage={transition.stage.value} "
                                f"site={transition.site_family.value} "
                                f"reason={transition.reason}"
                            )
                            logger.info(
                                "ATS_ENTRY | result=progress "
                                f"stage={transition.stage.value} "
                                f"site={transition.site_family.value}"
                            )
                            return True
                        logger.info("APPLICATION_ENTRY_NO_PROGRESS | reason=unchanged")
                        logger.info("ATS_ENTRY | result=unchanged")
                    except Exception as error:
                        logger.info(
                            "APPLICATION_ENTRY_ACTION | result=failed "
                            f"error_class={type(error).__name__}"
                        )
                        logger.info(
                            "ATS_ENTRY | result=failed "
                            f"error_class={type(error).__name__}"
                        )
                return False
            except Exception as error:
                logger.warning(
                    "ATS_ENTRY resolver failed safely | " f"error_class={type(error).__name__}"
                )
                return False

        @tools.action(
            description=(
                "When the external employer landing page has not progressed, locate and click "
                "one high-confidence legitimate Apply/Application entry for this job. "
                "Do not use this for final Submit, account creation, or security controls."
            )
        )
        async def resolve_application_entry(reason: str, browser_session):  # noqa: ARG001
            page = await browser_session.must_get_current_page()
            body = str(
                await page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            )
            try:
                raw_controls, _raw_form_fields, _frame_count, editable_field_count = (
                    await read_visible_control_snapshot(
                        page,
                        timeout_seconds=min(EXTERNAL_ATS_NAVIGATION_TIMEOUT_SEC, 5.0),
                    )
                )
                evidence = stage_evidence_from_visible_controls(
                    raw_controls,
                    editable_field_count=editable_field_count,
                )
            except Exception:
                evidence = None
            stage = infer_stage(await page.get_url(), body, ats_state.stage, evidence=evidence)
            if await deterministic_application_entry(browser_session, stage):
                return "APPLICATION_ENTRY_PROGRESS: continue by rescanning the current page."
            return "TECHNICAL_FAILURE: no safe application-entry control made progress."

        def is_registration_submit_control(semantic: str) -> bool:
            return bool(
                re.search(
                    r"\b(?:create (?:an? )?account|register(?: now)?|sign[ -]?up(?: now)?)\b",
                    semantic,
                )
            )

        def is_external_identity_provider_control(semantic: str) -> bool:
            """Keep third-party identity-provider authentication outside automation."""

            return bool(
                re.search(
                    r"\b(?:continue|sign[ -]?(?:in|up)|log[ -]?in)\s+"
                    r"(?:with|using)\s+(?:google|facebook|apple|microsoft|linkedin)\b",
                    semantic,
                )
            )

        def is_final_submit_control(semantic: str, stage: ATSStage) -> bool:
            if is_strong_final_submit_control(semantic):
                return True
            return stage in {ATSStage.REVIEW, ATSStage.SUBMIT} and bool(
                re.search(r"\b(?:submit|finish|complete|apply)\b", semantic)
            )

        async def guarded_interaction_context(browser_session):
            """Fail closed before every generic page-changing control action."""

            try:
                page = await browser_session.must_get_current_page()
                body = str(
                    await page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                )
                human_required, human_reason = needs_human(body)
                if human_required:
                    return (
                        None,
                        None,
                        (
                            "NEEDS_HUMAN: a security verification is visible "
                            f"({human_reason}); do not interact with this page."
                        ),
                    )
                if await registration_page_present(browser_session):
                    return (
                        None,
                        None,
                        (
                            "ACCOUNT REGISTRATION REQUIRED: call "
                            "complete_account_registration; do not use generic form actions."
                        ),
                    )
                page_url = await page.get_url()
                return page, infer_stage(page_url, body, ats_state.stage), ""
            except Exception as error:
                # DOM inspection is part of the security boundary.  A failed
                # inspection must not permit a blind interaction.
                return (
                    None,
                    None,
                    (
                        "TECHNICAL_FAILURE: guarded interaction inspection failed "
                        f"({type(error).__name__})."
                    ),
                )

        @tools.action(
            description=(
                "Click an ordinary non-final application control by DOM index. "
                "This action is blocked on security pages, account-registration "
                "forms, Create Account/Register controls, and final Submit/Apply "
                "controls. Use complete_account_registration or "
                "submit_final_application for those protected boundaries."
            )
        )
        async def click(index: int, browser_session):
            from browser_use.browser.events import ClickElementEvent

            _page, stage, blocked = await guarded_interaction_context(browser_session)
            if blocked:
                return ActionResult(extracted_content=blocked)
            node = await browser_session.get_dom_element_by_index(index)
            if node is None:
                return ActionResult(
                    extracted_content="The requested control is no longer available; inspect the updated page."
                )
            semantic = control_semantics(node)
            if is_external_identity_provider_control(semantic):
                return ActionResult(
                    extracted_content=(
                        "NEEDS_HUMAN: third-party identity-provider authentication "
                        "is not automated."
                    )
                )
            if is_registration_submit_control(semantic):
                return ActionResult(
                    extracted_content=(
                        "ACCOUNT REGISTRATION SUBMIT BLOCKED: call "
                        "complete_account_registration exactly once."
                    )
                )
            if is_final_submit_control(semantic, stage):
                return ActionResult(
                    extracted_content=(
                        "FINAL SUBMIT BLOCKED: call submit_final_application "
                        "exactly once, then verify_submission."
                    )
                )
            try:
                event = browser_session.event_bus.dispatch(ClickElementEvent(node=node))
                await event
                await event.event_result(raise_if_any=True, raise_if_none=False)
                return ActionResult(extracted_content="Clicked an ordinary non-final control.")
            except Exception as error:
                return ActionResult(
                    error=f"TECHNICAL_FAILURE: guarded click failed ({type(error).__name__})"
                )

        @tools.action(
            description=(
                "Enter a candidate-backed value into an ordinary non-password field. "
                "The action is blocked on security and account-registration pages, "
                "and never logs or persists entered values. Use "
                "fill_secure_account_password for a password field."
            )
        )
        async def input(index: int, text: str, browser_session, clear: bool = True):
            from browser_use.browser.events import TypeTextEvent

            _page, _stage, blocked = await guarded_interaction_context(browser_session)
            if blocked:
                return ActionResult(extracted_content=blocked)
            node = await browser_session.get_dom_element_by_index(index)
            if node is None:
                return ActionResult(
                    extracted_content="The requested field is no longer available; inspect the updated page."
                )
            semantic = control_semantics(node)
            if re.search(r"\b(?:password|passcode|pass word)\b", semantic):
                return ActionResult(
                    extracted_content=(
                        "PASSWORD INPUT BLOCKED: call fill_secure_account_password; "
                        "never type or reveal an ATS password."
                    )
                )
            if re.search(r"\b(?:file|resume|curriculum vitae|cv)\b", semantic):
                return ActionResult(
                    extracted_content=(
                        "FILE INPUT BLOCKED: use upload_resume for the canonical resume only."
                    )
                )
            try:
                event = browser_session.event_bus.dispatch(
                    TypeTextEvent(
                        node=node,
                        text=text,
                        clear=clear,
                        is_sensitive=True,
                        sensitive_key_name="candidate application value",
                    )
                )
                await event
                await event.event_result(raise_if_any=True, raise_if_none=False)
                return ActionResult(extracted_content="Entered a protected application value.")
            except Exception as error:
                return ActionResult(
                    error=f"TECHNICAL_FAILURE: guarded input failed ({type(error).__name__})"
                )

        @tools.action(
            description=(
                "Select an option in an ordinary non-registration native dropdown. "
                "The action is blocked on security and account-registration pages "
                "and never logs the selected candidate value."
            )
        )
        async def select_dropdown(index: int, text: str, browser_session):
            from browser_use.browser.events import SelectDropdownOptionEvent

            _page, _stage, blocked = await guarded_interaction_context(browser_session)
            if blocked:
                return ActionResult(extracted_content=blocked)
            node = await browser_session.get_dom_element_by_index(index)
            if node is None:
                return ActionResult(
                    extracted_content="The requested dropdown is no longer available; inspect the updated page."
                )
            try:
                event = browser_session.event_bus.dispatch(
                    SelectDropdownOptionEvent(node=node, text=text)
                )
                await event
                result = await event.event_result(raise_if_any=True, raise_if_none=False)
                if isinstance(result, dict) and result.get("success") == "true":
                    return ActionResult(extracted_content="Selected an ordinary dropdown option.")
                return ActionResult(error="TECHNICAL_FAILURE: dropdown selection was not confirmed")
            except Exception as error:
                return ActionResult(
                    error=f"TECHNICAL_FAILURE: guarded dropdown failed ({type(error).__name__})"
                )

        @tools.action(
            description=(
                "Use this when an ATS registration page requires a password. "
                "This securely generates/reuses a unique password for this ATS "
                "and fills all password fields without revealing the password. "
                "Pass a short reason describing the visible password requirement."
            )
        )
        async def fill_secure_account_password(reason: str, browser_session):  # noqa: ARG001
            if not EXTERNAL_ATS_AUTO_ACCOUNT_CREATION:
                return (
                    "NEEDS_HUMAN: automatic ATS account creation is disabled by operator settings"
                )
            page = await browser_session.must_get_current_page()
            if await registration_page_present(browser_session):
                return (
                    "ACCOUNT REGISTRATION REQUIRED: call "
                    "complete_account_registration exactly once."
                )
            body = str(
                await page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            )
            human_required, human_reason = needs_human(body)
            if human_required:
                return (
                    "NEEDS_HUMAN: a security verification is visible "
                    f"({human_reason}); do not enter an ATS password."
                )
            current_url = await page.get_url()
            elements = await page.get_elements_by_css_selector('input[type="password"]')
            if not elements:
                return "NO PASSWORD FIELDS FOUND"
            registration_state = ats_account_registration_state(
                current_url,
                account_registration_guard.facts.email,
            )
            if registration_state not in {"created", "legacy"}:
                return (
                    "NEEDS_HUMAN: this password form has no verified existing ATS "
                    "account and no recognized ordinary registration boundary."
                )
            limits = []
            minimums = []
            for element in elements:
                maximum = await element.get_attribute("maxlength")
                if maximum and maximum.isdigit() and int(maximum) > 0:
                    limits.append(int(maximum))
                minimum = await element.get_attribute("minlength")
                if minimum and minimum.isdigit() and int(minimum) > 0:
                    minimums.append(int(minimum))
            try:
                password = get_or_create_ats_password(
                    current_url,
                    account_registration_guard.facts.email,
                    minimum_length=max(minimums, default=8),
                    maximum_length=min(limits) if limits else None,
                )
            except ValueError:
                return "TECHNICAL_FAILURE: stored ATS password does not match the visible policy"
            filled = 0
            try:
                for element in elements:
                    await element.fill(password)
                    filled += 1
            finally:
                password = None
            logger.info(f"Secure ATS password filled into {filled} field(s)")
            return f"SECURE PASSWORD FILLED into {filled} field(s). Continue."

        @tools.action(
            description=(
                "MANDATORY on an ordinary Create Account/Register page before using "
                "individual field actions. Pass a short reason. It fills only "
                "candidate-profile-backed standard registration fields, uses/reuses "
                "a policy-compatible secure ATS password, chooses a per-job-only "
                "visibility option, accepts a required privacy/terms checkbox, and "
                "clicks Create Account once. It never handles CAPTCHA, OTP/MFA, "
                "magic links, device checks, or unsupported factual fields."
            )
        )
        async def complete_account_registration(reason: str, browser_session):  # noqa: ARG001
            if account_registration_guard.attempted:
                prior = account_registration_guard.result
                if prior and prior.status == "ACCOUNT_CREATED":
                    return (
                        "ACCOUNT REGISTRATION ALREADY DISPATCHED. Re-scan the current "
                        "page for the next application stage; do not click Create Account again."
                    )
                if prior and prior.status == "AUTH_ROUTED_EXISTING_ACCOUNT":
                    return (
                        "EXISTING ACCOUNT SIGN-IN WAS ALREADY OPENED. Continue through the "
                        "current sign-in form; do not click Create Account again."
                    )
                return (
                    "TECHNICAL_FAILURE: account registration was already attempted and "
                    "will not be dispatched again."
                )
            if not await registration_page_present(browser_session):
                return (
                    "TECHNICAL_FAILURE: the current page is not an ordinary ATS registration form."
                )
            status = await run_account_registration(browser_session)
            if status == "ACCOUNT_CREATED":
                return "ACCOUNT REGISTRATION SUBMITTED. Wait for the next application stage."
            if status == "AUTH_ROUTED_EXISTING_ACCOUNT":
                return (
                    "EXISTING ACCOUNT SIGN-IN OPENED. Continue through the ordinary "
                    "sign-in form; do not click Create Account again."
                )
            if status == "NEEDS_HUMAN":
                return "NEEDS_HUMAN: ATS account registration has a security challenge."
            if status == "CONFIGURATION_DISABLED":
                return "TECHNICAL_FAILURE: automatic ATS account creation is disabled by operator settings."
            return "TECHNICAL_FAILURE: ordinary ATS registration could not be completed safely."

        # Success is NOT based only on what the LLM claims.
        # This flag is set only after the actual web page contains
        # post-submission confirmation evidence.
        submission_verified = False
        submission_evidence = ""
        submission_verification_source = ""
        final_submit_attempted = False
        post_submit_verification_attempted = False
        final_page_url = ""
        final_page_title = ""
        final_body = ""
        final_screenshot_b64 = None

        async def verify_post_submit(
            browser_session,
            evidence_text: str = "",
        ) -> str:
            """Verify a durable submit marker without issuing another action."""

            nonlocal submission_verified
            nonlocal submission_evidence
            nonlocal submission_verification_source
            nonlocal final_submit_attempted
            nonlocal post_submit_verification_attempted
            nonlocal final_page_url
            nonlocal final_body

            final_submit_attempted = True
            post_submit_verification_attempted = True
            if self.worker_state_path:
                write_worker_state(
                    self.worker_state_path,
                    "verifying",
                    run_id=self.run_id,
                    submit_attempted=True,
                )

            async def receipt_checker() -> str | None:
                if gmail_client is None or not should_poll_for_receipt(
                    submit_attempted=final_submit_attempted
                ):
                    return None
                logger.info("No page confirmation found; checking Gmail receipt metadata")
                receipt = await gmail_client.wait_for_receipt(
                    since_epoch=application_started_at,
                    company=company_name,
                    job_title=job_title,
                    timeout=GMAIL_RECEIPT_TIMEOUT_SEC,
                    poll_interval=GMAIL_VERIFICATION_POLL_SEC,
                )
                return "Application receipt email received" if receipt is not None else None

            evidence = await collect_post_submit_evidence(
                browser_session,
                evidence_text=evidence_text,
                receipt_checker=receipt_checker if gmail_client is not None else None,
            )
            final_body = evidence.body_text or final_body
            final_page_url = evidence.url or final_page_url

            if evidence.verified:
                submission_verified = True
                submission_evidence = evidence.confirmation
                submission_verification_source = evidence.source
                ats_state.stage = ATSStage.CONFIRMATION
                log_ats_stage(ATSStage.CONFIRMATION, "verified")
                if self.worker_state_path:
                    write_worker_state(
                        self.worker_state_path,
                        "submitted",
                        run_id=self.run_id,
                        submit_attempted=True,
                        verification_source=evidence.source,
                    )
                logger.info(
                    "External submission independently VERIFIED | " f"source={evidence.source}"
                )
                return f"VERIFIED: {evidence.confirmation}"
            if evidence.human_reason:
                return f"NEEDS_HUMAN: {evidence.human_reason}"
            if evidence.validation_errors:
                return "NOT VERIFIED: Possible validation messages: " + ", ".join(
                    evidence.validation_errors
                )
            if evidence.error_class:
                logger.warning(
                    "Post-submit evidence collection failed safely | "
                    f"error_class={evidence.error_class}"
                )
                return "NOT VERIFIED: verification tool error."
            logger.warning("Submission could not be verified after bounded checks")
            return (
                "NOT VERIFIED: no explicit page confirmation or matching "
                "application receipt was found."
            )

        def has_durable_submit_attempt() -> bool:
            if final_submit_attempted or ats_state.final_submit_attempted:
                return True
            return bool(
                self.worker_state_path
                and submit_may_have_occurred(read_worker_state(self.worker_state_path))
            )

        @tools.action(
            description=(
                "MANDATORY for the actual FINAL Submit/Apply button. Pass its current DOM "
                "index. This records the critical section, clicks that exact element, and "
                "records that submission may have occurred. Never use ordinary click for "
                "the final submission button."
            )
        )
        async def submit_final_application(index: int, browser_session):
            nonlocal final_submit_attempted
            if final_submit_attempted or ats_state.final_submit_attempted:
                log_ats_recovery("duplicate_submit", "critical_section_guard", False)
                return (
                    "FINAL SUBMIT ALREADY ATTEMPTED: do not click again. "
                    "Verify the resulting state."
                )
            from browser_use.browser.events import ClickElementEvent

            try:
                page = await browser_session.must_get_current_page()
                body = str(
                    await page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                )
            except Exception as error:
                return (
                    "FINAL SUBMIT NOT CLICKED: page safety inspection failed "
                    f"({type(error).__name__})."
                )
            human_required, human_reason = needs_human(body)
            if human_required:
                return (
                    "NEEDS_HUMAN: a security verification is visible "
                    f"({human_reason}); do not submit this application."
                )

            node = await browser_session.get_dom_element_by_index(index)
            if node is None:
                if self.worker_state_path:
                    write_worker_state(
                        self.worker_state_path,
                        "pre_submit",
                        run_id=self.run_id,
                        submit_attempted=False,
                        error_class="element_not_found",
                    )
                return "FINAL SUBMIT NOT CLICKED: element index is no longer available."
            if not runtime_controller.try_start_irreversible_dispatch("final_submit"):
                raise RuntimeError("CANCELLED_BY_SHUTDOWN: final submit not dispatched")
            if self.worker_state_path:
                write_worker_state(
                    self.worker_state_path,
                    "submit_click_started",
                    run_id=self.run_id,
                    submit_attempted=False,
                )
            logger.info("EXTERNAL_IRREVERSIBLE_DISPATCH | action=final_submit")
            event = browser_session.event_bus.dispatch(ClickElementEvent(node=node))
            await event
            await event.event_result(raise_if_any=True, raise_if_none=False)
            final_submit_attempted = True
            ats_state.final_submit_attempted = True
            ats_state.stage = ATSStage.FINAL_SUBMIT
            log_ats_stage(ATSStage.FINAL_SUBMIT, "clicked")
            if self.worker_state_path:
                write_worker_state(
                    self.worker_state_path,
                    "submit_attempted",
                    run_id=self.run_id,
                    submit_attempted=True,
                )
            return "FINAL SUBMIT CLICKED. Call verify_submission now."

        @tools.action(
            description=(
                "Call this ONLY AFTER clicking the FINAL submit/apply button. "
                "Pass the exact confirmation sentence visible on the website. "
                "This checks the real page up to 3 times and may also check "
                "a matching new application receipt email."
            )
        )
        async def verify_submission(
            browser_session,
            evidence_text: str = "",
        ):
            return await verify_post_submit(browser_session, evidence_text)

        @tools.action(
            description=(
                "Call this when the application requires email verification, an "
                "OTP/code, MFA, account activation, a magic link, device "
                "verification, CAPTCHA, or another security challenge. Pass a "
                "short reason. This safely stops for human action and never reads, "
                "enters, opens, or relays a security credential."
            )
        )
        async def wait_for_email_verification(reason: str):  # noqa: ARG001
            emit_apply_event(
                "email_verification_waiting",
                "Waiting for a correlated ATS verification message",
                email_verification_required=True,
                email_verification_state="WAITING_FOR_EMAIL",
                last_workflow_step="email_verification",
            )
            if gmail_client is None:
                logger.info("EMAIL_VERIFICATION_TIMEOUT | gmail=unavailable")
                raise mark_human_boundary("Gmail readonly verification is unavailable.")
            page = await browser_session.must_get_current_page()
            current_url = await page.get_url()
            expected_host = urlparse(current_url).hostname or ""
            if self.worker_state_path:
                write_worker_state(
                    self.worker_state_path,
                    "waiting_email_verification",
                    verification_state="WAITING_EMAIL_VERIFICATION",
                )
            try:
                message = await gmail_client.wait_for_application_verification(
                    since_epoch=application_started_at,
                    company=company_name,
                    job_title=job_title,
                    expected_host=expected_host,
                    timeout=GMAIL_VERIFICATION_TIMEOUT_SEC,
                    poll_interval=GMAIL_VERIFICATION_POLL_SEC,
                )
            except GmailVerificationAmbiguous:
                logger.info("EMAIL_VERIFICATION_AMBIGUOUS")
                emit_apply_event(
                    "email_verification_failed",
                    "Correlated ATS verification was ambiguous",
                    email_verification_required=True,
                    email_verification_state="FAILED",
                    failure_category="email_verification_failure",
                    last_workflow_step="email_verification",
                )
                raise mark_human_boundary("multiple activation emails matched.")
            if message is None:
                logger.info("EMAIL_VERIFICATION_TIMEOUT")
                emit_apply_event(
                    "email_verification_failed",
                    "Correlated ATS verification message did not arrive",
                    email_verification_required=True,
                    email_verification_state="EXPIRED",
                    failure_category="email_verification_failure",
                    last_workflow_step="email_verification",
                )
                raise mark_human_boundary("activation email did not arrive before timeout.")
            verification_event_id = "gmail-" + hashlib.sha256(
                str(message.message_id).encode("utf-8")
            ).hexdigest()[:16]
            emit_apply_event(
                "email_verification_received",
                "Correlated ATS verification message received",
                email_verification_required=True,
                email_verification_state="EMAIL_FOUND",
                verification_event_id=verification_event_id,
                last_workflow_step="email_verification",
            )
            if message.codes and not message.verification_links:
                # OTP entry is limited to an input with an explicit code
                # semantic.  Bobby never guesses a field by position and
                # never logs or returns the code value.
                code_page = await browser_session.must_get_current_page()
                code_element = None
                for selector in (
                    "input[autocomplete='one-time-code']",
                    "input[name*='code' i]",
                    "input[id*='code' i]",
                    "input[name*='otp' i]",
                    "input[id*='otp' i]",
                ):
                    try:
                        elements = await code_page.get_elements_by_css_selector(selector)
                    except Exception:
                        elements = []
                    if elements:
                        code_element = elements[0]
                        break
                if code_element is None:
                    emit_apply_event(
                        "email_verification_failed",
                        "Correlated OTP was found but no explicit code field was available",
                        email_verification_required=True,
                        email_verification_state="FAILED",
                        verification_event_id=verification_event_id,
                        failure_category="email_verification_failure",
                        last_workflow_step="email_verification",
                    )
                    raise mark_human_boundary("correlated OTP field could not be identified safely.")
                try:
                    await code_element.fill(str(message.codes[0]))
                except Exception as error:
                    emit_apply_event(
                        "email_verification_failed",
                        "Correlated OTP could not be entered into the ATS field",
                        email_verification_required=True,
                        email_verification_state="FAILED",
                        verification_event_id=verification_event_id,
                        failure_category="email_verification_failure",
                        exception_class=type(error).__name__,
                        last_workflow_step="email_verification",
                    )
                    raise mark_human_boundary("correlated OTP field rejected safe entry.")
                emit_apply_event(
                    "email_verification_code_entered",
                    "Correlated ATS verification code entered",
                    email_verification_required=True,
                    email_verification_state="CODE_ENTERED",
                    verification_event_id=verification_event_id,
                    last_workflow_step="email_verification",
                )
                # Click only a clearly labeled verification control. A final
                # application Submit/Apply control is deliberately excluded.
                clicked = False
                try:
                    buttons = await code_page.get_elements_by_css_selector(
                        "button, input[type='submit']"
                    )
                except Exception:
                    buttons = []
                for button in buttons:
                    try:
                        label = str(
                            await button.get_attribute("aria-label")
                            or await button.get_attribute("value")
                            or button.get_meaningful_text_for_llm()
                            or ""
                        ).casefold()
                    except Exception:
                        label = ""
                    if (
                        any(word in label for word in ("verify", "confirm", "activate"))
                        and not any(word in label for word in ("submit application", "apply now"))
                    ):
                        try:
                            await button.click()
                            clicked = True
                        except Exception:
                            clicked = False
                        break
                await asyncio.sleep(2)
                verified_body = str(
                    await code_page.evaluate(
                        "() => document.body ? document.body.innerText : ''"
                    )
                    or ""
                )
                if clicked and any(
                    phrase in verified_body.casefold()
                    for phrase in ("email verified", "email confirmed", "account activated", "verification successful")
                ):
                    emit_apply_event(
                        "email_verification_confirmed",
                        "ATS email verification confirmed after correlated OTP entry",
                        email_verification_required=True,
                        email_verification_state="VERIFIED",
                        verification_event_id=verification_event_id,
                        last_workflow_step="email_verification",
                    )
                    return "EMAIL_VERIFICATION_CONFIRMED: continue the existing application."
                return "EMAIL_VERIFICATION_CODE_ENTERED: re-scan the ATS page and continue the application."
            link = next(
                (
                    gmail_client.activation_url(candidate, expected_host=expected_host)
                    for candidate in message.verification_links
                ),
                None,
            )
            if not link:
                emit_apply_event(
                    "email_verification_failed",
                    "Correlated verification message had no safe ATS link",
                    email_verification_required=True,
                    email_verification_state="FAILED",
                    verification_event_id=verification_event_id,
                    failure_category="email_verification_failure",
                    last_workflow_step="email_verification",
                )
                raise mark_human_boundary(
                    "activation link destination could not be safely validated."
                )
            if self.worker_state_path:
                write_worker_state(
                    self.worker_state_path,
                    "email_verification_received",
                    verification_state="EMAIL_VERIFICATION_RECEIVED",
                )
            verification_page = await browser_session.new_page(link)
            emit_apply_event(
                "email_verification_opened",
                "Correlated ATS verification link opened",
                email_verification_required=True,
                email_verification_state="VERIFICATION_LINK_OPENED",
                verification_event_id=verification_event_id,
                last_workflow_step="email_verification",
            )
            if self.worker_state_path:
                write_worker_state(
                    self.worker_state_path,
                    "email_verification_opened",
                    verification_state="EMAIL_VERIFICATION_OPENED",
                )
            try:
                logger.info("EMAIL_VERIFICATION_LINK_OPENED")
                await asyncio.sleep(2)
                final_url = await verification_page.get_url()
                body = str(
                    await verification_page.evaluate(
                        "() => document.body ? document.body.innerText : ''"
                    )
                    or ""
                )
                if any(term in body.casefold() for term in SECURITY_WORDS):
                    raise mark_human_boundary(
                        "activation page requires security verification."
                    )
                final_host = urlparse(final_url).hostname or ""
                if not gmail_client.activation_url(final_url, expected_host=expected_host):
                    raise mark_human_boundary(
                        "activation redirect destination could not be validated."
                    )
            finally:
                await browser_session.close_page(verification_page)
            if self.worker_state_path:
                write_worker_state(
                    self.worker_state_path,
                    "email_verification_confirmed",
                    verification_state="EMAIL_VERIFICATION_CONFIRMED",
                )
            emit_apply_event(
                "email_verification_confirmed",
                "ATS email verification confirmed",
                email_verification_required=True,
                email_verification_state="VERIFIED",
                verification_event_id=verification_event_id,
                last_workflow_step="email_verification",
            )
            logger.info("EMAIL_VERIFICATION_CONFIRMED")
            return "EMAIL_VERIFICATION_CONFIRMED: return to the existing application page and continue."

        task = f"""
        - Your goal is to apply to the job at: {job_url}

        ## Job context from LinkedIn
        LinkedIn company: {company_name}
        LinkedIn title: {job_title}
        LinkedIn URL: {linkedin_url}

        ## Detected ATS
        {ats_name}

        ## Generic ATS state machine
        Progress through the visible stages as they exist on this site:
        LANDING -> APPLY_ENTRY -> AUTH or ACCOUNT_CREATION -> PERSONAL_INFORMATION
        -> CONTACT_INFORMATION -> RESUME -> EMPLOYMENT_HISTORY -> EDUCATION -> SKILLS
        -> SCREENING_QUESTIONS -> EEO -> VOLUNTARY_DISCLOSURE -> REVIEW -> SUBMIT
        -> CONFIRMATION. Re-scan the DOM after every route change or async update;
        sites may omit or reorder stages.
        Use semantic labels, roles, ARIA names, required state, and visible
        validation instead of coordinates. For custom dropdowns and autocomplete,
        open the control, choose a real option, and verify its selected state.
        A normal Next/Continue validation error gets one bounded repair using the
        visible constraint; unsupported factual answers remain NEEDS_HUMAN. Use
        state-based waits and never wait longer than {EXTERNAL_ATS_NAVIGATION_TIMEOUT_SEC}
        seconds for an ordinary route, modal, or async control update.
        If LANDING or APPLY_ENTRY remains unchanged after a model step, call
        resolve_application_entry once to select a high-confidence semantic
        Apply/Application control, then re-scan the resulting page. Do not use
        this recovery action for final Submit, account creation, or security
        challenges.

        ## ATS-specific instructions
        {ats_specific_instructions}
        - Use the information from my resume (source of truth) and any additional information already present on the page.
        - If you cannot apply, finish the task (do not try different URLs).

        - Follow these instructions carefully:
            - If anything pops up that blocks the form, close it and continue.
            - Do not skip required fields. If an optional field is present, fill it if possible using my resume/context.
            - Fill the form from top to bottom; do not skip a field to come back later.
            - Some text boxes may have dropdown suggestions: after filling a textbox, check for a dropdown and select the correct option.

        - Resume:
            - You may upload my resume when the application asks for it.
            - The resume file is available to the upload tool.
            - Use upload_resume with purpose="resume" exactly once. It discovers the real input[type="file"], uploads the resume, and verifies the attachment.
            - Never target a button/div/span or choose an arbitrary element index for upload.
            - If upload_resume returns TECHNICAL_FAILURE, stop cleanly; do not loop or try unrelated attachment fields.

        - If an ATS shows Create Account/Register, call complete_account_registration
          exactly once before trying individual account fields. It handles ordinary
          profile-backed fields, the secure reusable password, country selections,
          a per-job-only visibility option, and standard required terms.
        - Do not use ordinary click/select/fill actions to work around a failed
          registration action. A security challenge is NEEDS_HUMAN; an ordinary
          control failure is TECHNICAL_FAILURE, never a fabricated answer.
        - If account registration routes to an existing-account sign-in page,
          continue through the visible ordinary sign-in fields using the secure
          password action only when Bobby's secure ATS store records a created
          or legacy existing account. Never generate a new password merely to
          try an unknown login, and never return to or click Create Account again.
        - When a password field appears outside a full registration page, call
          fill_secure_account_password. Never expose an ATS password in the final
          result or logs.

        - If ordinary ATS email verification, an OTP/code, or account activation
          is required, call wait_for_email_verification. It uses only a strongly
          correlated Gmail/OAuth message, enters an OTP only into an explicit
          code field, or opens a same-ATS activation link, then returns to this
          application. MFA, CAPTCHA, device verification, magic links that do
          not meet the same-ATS safety check, and other security challenges must
          stop with NEEDS_HUMAN.

        - Before you start, create a step-by-step plan to complete the entire application. Delegate a step for each field/section you encounter.

        *** SUBMISSION VERIFICATION — ABSOLUTE RULE ***
            - Submission verification is mandatory and cannot be disabled.
            - Clicking an Apply, Submit, Continue, Review or Finish
              button does NOT mean the application was submitted.
            - Reaching the final review screen does NOT mean success.
            - A button disappearing does NOT mean success.
            - Your own belief that the application was submitted is
              NOT evidence.
            - Use submit_final_application for the actual FINAL submission
              button. Never use the ordinary click action for final submit.
            - After submit_final_application succeeds, inspect the
              resulting page.
            - You MUST call verify_submission with the exact visible
              confirmation sentence.
            - If verify_submission returns NOT VERIFIED, continue
              working if possible.
            - You may report success ONLY after verify_submission
              returns VERIFIED.
            - If explicit confirmation cannot be verified, finish with
              success=False.
            - Do not close the task immediately after clicking Submit.
              Wait for the resulting page/state and verify it.

        *** IMPORTANT ***
            - You are not done until you have either submitted the application OR confirmed you cannot apply.
            - At the end, structure your final_result as:
                1) a human-readable summary of all detections and actions performed
                2) a list of all questions encountered on the page (including any screening questions)
                3) a short final human-readable summary at the very end

        ## My resume text
        This is the source of truth for employment history,
        education, dates, experience, skills and certifications:

        {self.resume_readable}

        ## Structured administrative application facts
        Use these structured facts for contact, eligibility, preferences and
        standardized screening answers.  They do not establish employment
        history, education history, skills, certifications or years of
        experience; use the resume for those fields.  ``UNKNOWN`` means the
        fact is not established and must not be guessed:

        {application_profile}

        ## Reusable learned application answers
        {reusable_answers}
        - Do not save, replay, or disclose answers from another employer's application.

        IMPORTANT SOURCE RULES:
        - Resume wins for employment, education, dates, technical
          experience and certifications.
        - Application profile wins for preferences and administrative
          application answers.
        - Never invent a missing factual qualification.
        - Never invent years of experience.
        - Never invent a degree, certification or security clearance.
        - If information is explicitly UNKNOWN and the field is
          required, do not guess.
        """

        available_file_paths = [resume_pdf_path]

        validate_custom_action_schemas(tools)

        browser = self._create_browser()
        history = None
        post_submit_provider_error = ""
        post_submit_human_boundary = False
        fallback_state = ProviderFallbackState(self.provider_order)
        last_observed_stage: ATSStage | None = None

        async def prepare_external_step(agent_instance) -> None:
            """Open the admitted URL once, then gate empty entry pages before LLM work."""

            if runtime_controller.is_shutdown_requested() and not has_durable_submit_attempt():
                raise RuntimeError("CANCELLED_BY_SHUTDOWN: pre-submit model work stopped")
            irreversible = lambda: (
                has_durable_submit_attempt()
                or account_registration_guard.attempted
                or resume_upload_guard.attempted
            )
            if irreversible() or ats_state.stage not in {ATSStage.LANDING, ATSStage.APPLY_ENTRY}:
                return
            try:
                observation = await document_gate.prepare(
                    agent_instance.browser_session,
                    shutdown_requested=runtime_controller.is_shutdown_requested,
                    irreversible_activity=irreversible,
                )
            except (ExternalEmptyDocument, ExternalInitialNavigationFailed):
                # A terminal browser-readiness failure never gets another LLM
                # attempt, reload, or provider handoff. Capture is best effort
                # and separately bounded so an unresponsive target cannot hang.
                try:
                    await asyncio.wait_for(
                        capture_terminal_progress_diagnostic(
                            agent_instance.browser_session,
                            stage=ats_state.stage,
                            reason="empty_document",
                        ),
                        timeout=5.0,
                    )
                except Exception:
                    logger.warning("TERMINAL_DIAGNOSTIC_WRITE_FAILED | reason=unresponsive_page")
                raise
            if observation:
                family = detect_site_family(str(observation.get("url") or ""), "")
                if family is not SiteFamily.GENERIC and family is not ats_state.site_family:
                    ats_state.site_family = family
                    logger.info(f"ATS_RECLASSIFIED | site={family.value} source=ready_document")
                page = await agent_instance.browser_session.must_get_current_page()
                security_reason = await visible_security_reason(page)
                if security_reason:
                    logger.info(
                        "ATS_SECURITY_BOUNDARY | source=visible_controls "
                        f"class={security_reason}"
                    )
                    raise mark_human_boundary("visible security challenge requires human action.")

        async def observe_external_step(agent_instance) -> None:
            """Keep state telemetry aligned with real page transitions."""

            nonlocal last_observed_stage
            await resume_upload_guard.stop_after_failure(agent_instance)
            if runtime_controller.is_shutdown_requested() and not has_durable_submit_attempt():
                raise RuntimeError("CANCELLED_BY_SHUTDOWN: pre-submit model work stopped")
            stage = None
            try:
                page = await agent_instance.browser_session.must_get_current_page()
                current_url = await page.get_url()
                body = str(
                    await page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                )
                raw_controls: list[dict[str, object]] | None = None
                stage_evidence = None
                try:
                    raw_controls, _raw_form_fields, _frame_count, editable_field_count = (
                        await read_visible_control_snapshot(
                            page,
                            timeout_seconds=min(EXTERNAL_ATS_NAVIGATION_TIMEOUT_SEC, 5.0),
                        )
                    )
                    stage_evidence = stage_evidence_from_visible_controls(
                        raw_controls,
                        editable_field_count=editable_field_count,
                    )
                    logger.debug(
                        "ATS_STAGE_EVIDENCE | "
                        f"entry={str(stage_evidence.has_strong_application_entry).lower()} "
                        f"editable_fields={stage_evidence.editable_field_count}"
                    )
                except Exception as evidence_error:
                    logger.info(
                        "ATS_STAGE_EVIDENCE_UNAVAILABLE | "
                        f"error_class={type(evidence_error).__name__}"
                    )
                security_reason = await visible_security_reason(page, raw_controls)
                if security_reason:
                    logger.info(
                        "ATS_SECURITY_BOUNDARY | source=visible_controls "
                        f"class={security_reason}"
                    )
                    raise mark_human_boundary("visible security challenge requires human action.")
                current_family = detect_site_family(current_url, body)
                if (
                    current_family is not SiteFamily.GENERIC
                    and current_family is not ats_state.site_family
                ):
                    ats_state.site_family = current_family
                    logger.info(
                        "ATS_RECLASSIFIED | " f"site={current_family.value} source=live_page"
                    )
                stage = ats_state.observe(current_url, body, evidence=stage_evidence)
                # Browser Use may retain a provider exception in its history
                # while still invoking this hook. Stop before Bobby's guarded
                # account/upload actions can cross another boundary, then let
                # the outer provider loop perform the bounded handoff.
                try:
                    history = getattr(agent_instance, "history", None)
                    history_errors = " ".join(
                        str(item) for item in (history.errors() if history is not None else ()) if item
                    )
                except Exception:
                    history_errors = ""
                observed_provider_error = classify_provider_error(history_errors)
                if observed_provider_error and not has_durable_submit_attempt():
                    record_provider_failure_state(self.model_type, observed_provider_error)
                    logger.warning(
                        "Stopping external step after observed provider failure | "
                        f"provider={self.model_type} class={observed_provider_error}"
                    )
                    raise RuntimeError(
                        f"EXTERNAL_PROVIDER_UNAVAILABLE: {observed_provider_error}"
                    )
                # Inputs and dropdown selections can change only a control's
                # value, not the visible body text used by ``page_fingerprint``.
                # Treat each *new, successful* ordinary control action as one
                # semantic transition. Repeating an action at the same DOM
                # index does not reset the guard, preserving bounded-loop
                # protection for ineffective model behavior.
                action_progress_keys = completed_semantic_form_action_keys(agent_instance)
                new_action_progress = [
                    key
                    for key in action_progress_keys
                    if ats_state.record_action(f"semantic_progress:{key}")
                ]
                if new_action_progress:
                    ats_state.record_progress(current_url, body, stage=stage)
                    action_names = sorted({key.split(":", 1)[0] for key in new_action_progress})
                    logger.info(
                        "EXTERNAL_FORM_ACTION_PROGRESS | "
                        f"actions={','.join(action_names)} count={len(new_action_progress)}"
                    )
                if stage is not last_observed_stage:
                    log_ats_stage(stage, "observed")
                    last_observed_stage = stage
                entry_progress = False
                # A model step can return successfully while leaving an
                # employer landing page unchanged. Give the deterministic
                # resolver one bounded opportunity on the next unchanged
                # observation before the existing loop guard terminates the
                # worker. This is limited to pre-application entry states.
                if (
                    stage in {ATSStage.LANDING, ATSStage.APPLY_ENTRY}
                    and ats_state._repeat_count >= 1
                    and entry_recovery_attempts < 3
                ):
                    entry_progress = await deterministic_application_entry(
                        agent_instance.browser_session, stage
                    )
                if ats_state.loop_detected:
                    if entry_progress:
                        return
                    log_ats_recovery("same_page_state", "loop_guard", False)
                    await capture_terminal_progress_diagnostic(
                        agent_instance.browser_session,
                        stage=stage,
                        reason="same_page_state",
                    )
                    # Logging alone does not make progress.  Stop the current
                    # pre-submit worker once the bounded ATS state machine has
                    # observed the same page three times; a provider handoff
                    # would reproduce the same action/state loop.
                    raise ExternalATSProgressStalled(
                        "EXTERNAL_ATS_STALLED: repeated unchanged page state before submit"
                    )
            except (ExternalATSProgressStalled, RuntimeError) as error:
                if isinstance(error, ExternalATSProgressStalled) or "APPLICATION_NEEDS_HUMAN:" in str(error):
                    raise
                logger.warning(
                    "Could not observe external ATS step safely | "
                    f"error_class={type(error).__name__}"
                )
                return
            except Exception as error:
                logger.warning(
                    "Could not observe external ATS step safely | "
                    f"error_class={type(error).__name__}"
                )
                return

            # Browser Use occasionally chooses generic select actions for an
            # account page. Once the live page has an actual registration
            # submit control, finish the known ordinary fields deterministically
            # rather than asking the model to rediscover their semantics.
            registration_page = (
                stage in {ATSStage.APPLY_ENTRY, ATSStage.ACCOUNT_CREATION}
                and await registration_page_present(agent_instance.browser_session)
            )
            if registration_page and stage is not ATSStage.ACCOUNT_CREATION:
                # A page can use an entry CTA such as "Sign up to apply" for
                # both the pre-form landing state and its resulting account
                # form.  The latter is an account-creation stage only once a
                # real visible password/profile form confirms it structurally.
                ats_state.stage = ATSStage.ACCOUNT_CREATION
                stage = ATSStage.ACCOUNT_CREATION
                if stage is not last_observed_stage:
                    log_ats_stage(stage, "registration_form")
                    last_observed_stage = stage
            if stage is ATSStage.ACCOUNT_CREATION and not registration_page:
                # A sign-in page can advertise a create-account link. Keep the
                # telemetry truthful and leave login/auth routing to the agent.
                ats_state.stage = ATSStage.AUTH
                stage = ATSStage.AUTH
                if stage is not last_observed_stage:
                    log_ats_stage(stage, "observed")
                    last_observed_stage = stage

            if (
                registration_page
                and EXTERNAL_ATS_AUTO_ACCOUNT_CREATION
                and not account_registration_guard.attempted
            ):
                registration_status = await run_account_registration(agent_instance.browser_session)
                if registration_status == "NEEDS_HUMAN":
                    raise mark_human_boundary(
                        "ATS account registration requires security verification"
                    )
                if registration_status == "AUTH_ROUTED_EXISTING_ACCOUNT":
                    ats_state.stage = ATSStage.AUTH
                    last_observed_stage = ATSStage.AUTH
                    return
                if registration_status != "ACCOUNT_CREATED":
                    raise RuntimeError(
                        "EXTERNAL_ACCOUNT_REGISTRATION_FAILED: ordinary account registration could not be completed"
                    )

        try:
            for attempt_index, provider in enumerate(self.provider_order, start=1):
                # Advance the independent bounded state machine in lockstep
                # with the immutable provider tuple.
                fallback_state.next_provider()
                if circuit_is_open(provider):
                    logger.warning(
                        "External provider candidate skipped | "
                        f"provider={provider} reason=circuit_open"
                    )
                    continue
                try:
                    self.model_type = provider
                    provider_model = (
                        PROVIDER_SPECS.get(provider).model
                        if PROVIDER_SPECS.get(provider)
                        else self.model
                    )
                    self.model = provider_model
                    self.llm = self.select_model_type(
                        provider, self.llm_api_url, model=provider_model
                    )
                    # A later provider gets a clean attribution window.  A
                    # successful handoff must not inherit an earlier
                    # provider's outage if the current provider exposes a
                    # genuine Bobby-controlled failure.
                    if self.worker_state_path:
                        current = read_worker_state(self.worker_state_path)
                        write_worker_state(
                            self.worker_state_path,
                            current.get("phase") or "pre_submit",
                            run_id=self.run_id,
                            provider=provider,
                            provider_error_class="",
                        )
                    provider_task = task
                    if attempt_index > 1:
                        provider_task += (
                            "\n\n## Provider handoff\n"
                            "Continue the same application on the current page and browser session. "
                            "Do not reopen the original job URL, restart the application, upload the "
                            "resume again, or repeat a final submit. Inspect the current form state first."
                        )
                    agent_kwargs = {
                        "task": provider_task,
                        "browser": browser,
                        "llm": self.llm,
                        "tools": tools,
                        "use_vision": True,
                        "use_thinking": True,
                        "max_failures": 2,
                        "step_timeout": EXTERNAL_ATS_PAGE_TIMEOUT_SEC,
                        "use_judge": True,
                        "ground_truth": (
                            "PASS only if the employer/ATS visibly confirms that THIS "
                            "application was submitted or received. Clicking Submit/Apply, "
                            "reaching a review page, filling all fields, or the agent merely "
                            "claiming success is NOT sufficient evidence."
                        ),
                        "available_file_paths": available_file_paths,
                        # The task contains ATS + LinkedIn URLs. Browser Use
                        # 0.12.6 suppresses ambiguous inference, and inferred
                        # actions require the unsafe navigate tool we exclude.
                        # The shared pre-step gate owns explicit startup once.
                        "directly_open_url": False,
                    }
                    fallback_llm = None
                    if (
                        provider == "gemini"
                        and APPLY_AGENT_FALLBACK_MODEL
                        and APPLY_AGENT_FALLBACK_MODEL != self.model
                    ):
                        fallback_llm = ChatGoogle(
                            api_key=self.api_key,
                            model=APPLY_AGENT_FALLBACK_MODEL,
                        )
                    if (
                        fallback_llm is not None
                        and "fallback_llm" in inspect.signature(Agent.__init__).parameters
                    ):
                        agent_kwargs["fallback_llm"] = fallback_llm
                    self.agent = Agent(**agent_kwargs)
                    try:
                        history = await self.agent.run(
                            on_step_start=prepare_external_step,
                            on_step_end=observe_external_step,
                        )
                        if human_boundary_reason:
                            raise mark_human_boundary(human_boundary_reason)
                    except Exception as agent_error:
                        if human_boundary_reason:
                            raise mark_human_boundary(human_boundary_reason) from agent_error
                        raise
                    try:
                        history_errors = " ".join(str(item) for item in history.errors() if item)
                    except Exception:
                        history_errors = ""
                    history_provider_error = classify_provider_error(history_errors)
                    if history_provider_error:
                        record_provider_failure_state(provider, history_provider_error)
                        fallback_state.record_failure(provider)
                        open_circuit(
                            provider,
                            self.model,
                            history_provider_error,
                            permanent=history_provider_error == "permanent_quota",
                            cooldown_seconds=LLM_PROVIDER_COOLDOWN_SEC,
                        )
                        logger.error(
                            "External provider attempt returned provider error | "
                            f"provider={provider} class={history_provider_error} "
                            f"attempt={attempt_index}"
                        )
                        log_ats_recovery("provider_failure", "provider_fallback", False)
                        submit_attempted = has_durable_submit_attempt()
                        if submit_attempted:
                            # A provider outage after the durable final-submit
                            # marker must not skip independent confirmation.
                            # Preserve this browser/session and verify it below;
                            # a fallback could duplicate an irreversible action.
                            post_submit_provider_error = history_provider_error
                            logger.warning(
                                "Provider failed after final submit; running deterministic "
                                "post-submit verification without retrying"
                            )
                            break
                        if resume_upload_guard.failed or not fallback_state.can_switch(
                            upload_failed=resume_upload_guard.failed,
                            submit_attempted=False,
                        ):
                            raise RuntimeError(
                                f"EXTERNAL_PROVIDER_UNAVAILABLE: {history_provider_error}"
                            )
                        ats_state.reset_loop_memory_for_provider_handoff()
                        history = None
                        continue
                    logger.info(
                        "External provider attempt completed | "
                        f"provider={provider} attempt={attempt_index}"
                    )
                    if attempt_index > 1:
                        log_ats_recovery("provider_failure", "provider_fallback", True)
                    break
                except Exception as agent_error:
                    # A guarded human/security boundary must remain terminal.
                    # Some Browser Use wrappers preserve the original
                    # APPLICATION_NEEDS_HUMAN text while also appending
                    # "unavailable", which must not be mistaken for a
                    # provider outage and handed to another model.
                    if human_boundary_reason:
                        if has_durable_submit_attempt():
                            # A security/email handoff can be requested by the
                            # model immediately after a durable final click.
                            # Stop model control, but still perform Bobby's
                            # read-only confirmation pass; never retry the
                            # vacancy, switch provider, or take another action.
                            post_submit_human_boundary = True
                            logger.warning(
                                "Human boundary raised after final submit; running "
                                "deterministic post-submit verification without retrying"
                            )
                            break
                        raise mark_human_boundary(human_boundary_reason) from agent_error
                    if "APPLICATION_NEEDS_HUMAN:" in str(agent_error):
                        if has_durable_submit_attempt():
                            post_submit_human_boundary = True
                            logger.warning(
                                "Post-submit human boundary surfaced through Browser Use; "
                                "running deterministic verification without retrying"
                            )
                            break
                        raise
                    provider_error_class = classify_external_provider_failure(
                        agent_error, self.agent
                    )
                    if not provider_error_class:
                        raise
                    record_provider_failure_state(provider, provider_error_class)
                    if classify_provider_error(agent_error) is None:
                        logger.warning(
                            "External provider failure recovered from Browser Use history | "
                            f"provider={provider} class={provider_error_class} "
                            f"surface_error={type(agent_error).__name__}"
                        )
                    open_circuit(
                        provider,
                        self.model,
                        provider_error_class,
                        permanent=provider_error_class == "permanent_quota",
                        cooldown_seconds=LLM_PROVIDER_COOLDOWN_SEC,
                    )
                    fallback_state.record_failure(provider)
                    logger.error(
                        "External provider attempt failed | "
                        f"provider={provider} class={provider_error_class} "
                        f"attempt={attempt_index}"
                    )
                    log_ats_recovery("provider_failure", "provider_fallback", False)
                    # A provider switch is safe only before upload and before
                    # the durable final-submit critical section.
                    if has_durable_submit_attempt():
                        post_submit_provider_error = provider_error_class
                        logger.warning(
                            "Provider raised after final submit; running deterministic "
                            "post-submit verification without retrying"
                        )
                        break
                    if resume_upload_guard.failed or not fallback_state.can_switch(
                        upload_failed=resume_upload_guard.failed,
                        submit_attempted=False,
                    ):
                        raise RuntimeError(
                            f"EXTERNAL_PROVIDER_UNAVAILABLE: {provider_error_class}"
                        ) from agent_error
                    ats_state.reset_loop_memory_for_provider_handoff()
                    continue

            if history is None and not (
                post_submit_provider_error or post_submit_human_boundary
            ):
                raise RuntimeError(
                    "EXTERNAL_PROVIDER_UNAVAILABLE: no configured provider candidate was available"
                )

            if (
                has_durable_submit_attempt()
                and not submission_verified
                and (post_submit_provider_error or not post_submit_verification_attempted)
            ):
                # This deterministic evidence pass is intentionally outside
                # LLM control.  It covers provider loss immediately after the
                # final click and never changes the application state.
                await verify_post_submit(self.agent.browser_session)

            try:
                final_page = await self.agent.browser_session.must_get_current_page()
                final_page_url = await final_page.get_url()
                final_page_title = await final_page.get_title()
                final_body = str(
                    await final_page.evaluate("() => document.body ? document.body.innerText : ''")
                    or ""
                )
                final_screenshot_b64 = await final_page.screenshot()
                final_stage_evidence = None
                try:
                    final_raw_controls, _final_raw_form_fields, _final_frame_count, final_editable_field_count = (
                        await read_visible_control_snapshot(
                            final_page,
                            timeout_seconds=min(EXTERNAL_ATS_NAVIGATION_TIMEOUT_SEC, 5.0),
                        )
                    )
                    final_stage_evidence = stage_evidence_from_visible_controls(
                        final_raw_controls,
                        editable_field_count=final_editable_field_count,
                    )
                except Exception as structure_error:
                    # Final evidence remains read-only even when a backend
                    # cannot expose structural metadata. Falling back to text
                    # preserves confirmation handling without another action.
                    logger.info(
                        "ATS_STAGE_EVIDENCE_UNAVAILABLE | source=final_evidence "
                        f"error_class={type(structure_error).__name__}"
                    )
                final_stage = ats_state.observe(
                    final_page_url,
                    final_body,
                    evidence=final_stage_evidence,
                )
                log_ats_stage(final_stage, "observed")
                ats_name = ats_state.site_family.value
                if resume_upload_guard.attempted:
                    ats_state.upload_attempted = True
                    log_ats_stage(ATSStage.RESUME, "upload_attempted")
                if ats_state.loop_detected:
                    log_ats_recovery("same_page_state", "loop_guard", False)
            except Exception as evidence_error:
                logger.warning(
                    "Could not capture final external page evidence safely | "
                    f"error_class={type(evidence_error).__name__}"
                )
        finally:
            # stop() respects keep_alive; kill() releases this worker's owned
            # browser after evidence capture, including on upload/provider errors.
            await browser.kill()

        self._log_token_usage(
            job_title=job_title,
            company_name=company_name,
            linkedin_url=linkedin_url,
            ats_name=ats_name,
        )

        final_result = (history.final_result() or "") if history is not None else ""
        self_reported_success = history.is_successful() if history is not None else None

        try:
            judge_verdict = history.is_validated() if history is not None else None
            judgement = history.judgement() if history is not None else None
        except Exception:
            judge_verdict = None
            judgement = None

        logger.info(
            "External application result: "
            f"verified={submission_verified}, "
            f"agent_success={self_reported_success}, "
            f"judge={judge_verdict}, "
            f"evidence={submission_evidence!r}"
        )

        logger.info("External agent final result captured (content omitted from logs)")

        # ------------------------------------------------------------
        # EXTERNAL SUBMISSION DIAGNOSTICS
        # ------------------------------------------------------------

        try:
            agent_success_debug = history.is_successful()
        except Exception:
            agent_success_debug = None

        try:
            judge_debug = history.is_validated()
        except Exception:
            judge_debug = None

        try:
            judgement_debug = history.judgement()
        except Exception:
            judgement_debug = None

        try:
            final_result_debug = history.final_result() or ""
        except Exception:
            final_result_debug = ""

        logger.info(
            "EXTERNAL VERIFY DEBUG | "
            f"agent_success={agent_success_debug} | "
            f"judge_validated={judge_debug} | "
            f"final_url={redact_text(final_page_url, limit=500)!r} | "
            f"final_title={redact_text(final_page_title, limit=300)!r} | "
            f"submission_verified_before_fallback={submission_verified}"
        )

        logger.info(
            "EXTERNAL VERIFY DEBUG | "
            f"judge_present={bool(judgement_debug)} | "
            f"final_result_present={bool(final_result_debug)}"
        )

        if final_body:
            logger.info("EXTERNAL VERIFY DEBUG | " f"final_body_length={len(final_body)}")

        else:
            logger.warning("EXTERNAL VERIFY DEBUG | FINAL BODY IS EMPTY")

        if not submission_verified and final_body:
            verified, confirmation = detect_confirmation(final_body, "")
            if verified:
                submission_verified = True
                submission_evidence = confirmation
                submission_verification_source = "final_page_dom"
                ats_state.stage = ATSStage.CONFIRMATION
                log_ats_stage(ATSStage.CONFIRMATION, "verified")
                if self.worker_state_path:
                    write_worker_state(
                        self.worker_state_path,
                        "submitted",
                        run_id=self.run_id,
                        submit_attempted=True,
                        verification_source="final_page_dom",
                    )
                logger.info("External submission independently VERIFIED from final page")

        if (
            not submission_verified
            and gmail_client is not None
            and should_poll_for_receipt(submit_attempted=final_submit_attempted)
        ):
            try:
                receipt = await gmail_client.wait_for_receipt(
                    since_epoch=application_started_at,
                    company=company_name,
                    job_title=job_title,
                    timeout=GMAIL_RECEIPT_TIMEOUT_SEC,
                    poll_interval=GMAIL_VERIFICATION_POLL_SEC,
                )
                if receipt is not None:
                    submission_verified = True
                    submission_evidence = "Application receipt email received"
                    submission_verification_source = "email_receipt"
                    ats_state.stage = ATSStage.CONFIRMATION
                    log_ats_stage(ATSStage.CONFIRMATION, "verified")
                    if self.worker_state_path:
                        write_worker_state(
                            self.worker_state_path,
                            "submitted",
                            run_id=self.run_id,
                            submit_attempted=True,
                            verification_source="email_receipt",
                        )
                    logger.info("External submission VERIFIED by matching receipt email")
            except Exception as receipt_error:
                logger.warning(
                    "Receipt-email verification failed safely | "
                    f"error_class={type(receipt_error).__name__}"
                )

        evidence_job_id = safe_job_id(linkedin_url or job_url)

        if not submission_verified:
            human_required, human_reason = needs_human(final_body)
            result_type = NEEDS_HUMAN if human_required else UNVERIFIED
            validation_errors = find_validation_errors(final_body)
            reason = (
                human_reason
                if human_required
                else (
                    ", ".join(validation_errors)
                    if validation_errors
                    else "No explicit submission confirmation"
                )
            )

            save_evidence(
                job_id=evidence_job_id,
                result=result_type,
                external_url=job_url,
                final_url=final_page_url,
                ats=ats_name,
                linkedin_company=company_name,
                external_company=final_page_title,
                reason=reason,
                body_excerpt=redact_text(final_body, limit=4000),
            )
            save_screenshot_b64(evidence_job_id, final_screenshot_b64)

            if human_required:
                raise RuntimeError(f"APPLICATION_NEEDS_HUMAN: {human_reason}")

        # Keeping the final page alive must not hide an exhausted provider.
        # A recovered provider error must not override a completed submission.
        if (
            self_reported_success is not True
            and not submission_verified
            and not final_submit_attempted
        ):
            try:
                provider_errors = " ".join(str(item) for item in history.errors() if item)
            except Exception:
                provider_errors = ""
            provider_error_class = classify_provider_error(provider_errors)
            if provider_error_class:
                open_circuit(
                    self.model_type,
                    self.model,
                    provider_error_class,
                    permanent=provider_error_class == "permanent_quota",
                    cooldown_seconds=LLM_PROVIDER_COOLDOWN_SEC,
                )
                raise RuntimeError(f"EXTERNAL_PROVIDER_UNAVAILABLE: {provider_error_class}")

        # No result or final page means failure before applying, not submission.
        if (
            self_reported_success is None
            and not final_result
            and not final_body
            and not final_page_url
        ):
            raise RuntimeError(
                "EXTERNAL_AGENT_FAILED: browser agent produced no result. "
                "Check LLM quota/model/API errors above."
            )

        # Hard requirement #1:
        # Actual live DOM verification must have succeeded.
        if not submission_verified:
            raise RuntimeError(unresolved_submission_error(final_submit_attempted))

        post_submit_interrupted = bool(
            post_submit_provider_error or post_submit_human_boundary
        )

        # A post-submit provider outage or human/security handoff can leave
        # Browser Use unable to emit a success/judge result. The durable final
        # click plus independent DOM or receipt evidence is stronger than that
        # interrupted self-report, while the confirmation pass remains
        # read-only and never retries the vacancy.
        if self_reported_success is not True and not post_submit_interrupted:
            raise RuntimeError(
                "APPLICATION_NOT_VERIFIED: Page evidence was seen, "
                "but the agent did not finish successfully."
            )

        # If browser-use's independent judge explicitly rejects the
        # trace, do not count the application.
        if judge_verdict is False and not post_submit_interrupted:
            raise RuntimeError(
                "APPLICATION_NOT_VERIFIED: Independent trace judge rejected the submission."
            )

        if post_submit_interrupted:
            logger.info("Independent confirmation accepted after post-submit interruption")

        save_evidence(
            job_id=evidence_job_id,
            result=SUBMITTED,
            external_url=job_url,
            final_url=final_page_url,
            ats=ats_name,
            confirmation=submission_evidence,
            linkedin_company=company_name,
            external_company=final_page_title,
            body_excerpt=redact_text(final_body, limit=4000),
            verification_source=submission_verification_source,
        )
        save_screenshot_b64(evidence_job_id, final_screenshot_b64)

        logger.info("EXTERNAL APPLICATION CONFIRMED — awaiting authoritative result publication")

    def _log_token_usage(
        self,
        *,
        job_title: str = "",
        company_name: str = "",
        linkedin_url: str = "",
        ats_name: str = "",
    ) -> None:
        """Log AI Agent token usage and calculate the total cost"""
        token_usage = self.agent.token_cost_service.get_usage_tokens_for_model(self.model)
        input_tokens, output_tokens = token_usage.prompt_tokens, token_usage.completion_tokens
        total_tokens = input_tokens + output_tokens
        logger.info(
            f"Token usage - Input: {input_tokens}, Output: {output_tokens}, Total: {total_tokens}"
        )
        prompt_cost, completion_cost = cost_per_token(
            model=self.model.replace("google/", ""),
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            custom_cost_per_token=CUSTOM_COST_PER_TOKEN,
        )
        total_cost = prompt_cost + completion_cost
        logger.info(f"Total cost calculated: {total_cost}")

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            log_entry = LLMCall(
                model_name=self.model,
                timestamp=current_time,
                prompts={"prompt_1": "<application prompt omitted>"},
                parsed_reply="<Some reply from agent>",
                total_tokens=total_tokens,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                response_time_seconds=0.0,
                total_cost=total_cost,
            )
            logger.debug(
                "External LLM usage recorded | "
                f"company={redact_text(company_name, limit=120)!r} | "
                f"title={redact_text(job_title, limit=160)!r} | "
                f"job_id={safe_job_id(linkedin_url)!r} | ats={ats_name!r}"
            )
        except KeyError as e:
            logger.error(f"Error creating log entry: missing key {str(e)} in parsed_reply")
            raise

        append_yaml_file(Path(self.calls_log), log_entry.model_dump())

        return total_cost

    async def apply_to_job(
        self,
        job_url: str,
        job_title: str = "",
        company_name: str = "",
        linkedin_url: str = "",
    ) -> tuple[str, str]:
        """Run external application in an isolated Python process.

        browser-use gets its own Playwright/browser driver so shutting down
        the external browser cannot kill the main LinkedIn Playwright session.
        """
        if not EXTERNAL_ATS_ENABLED:
            return (
                "Skip",
                "EXTERNAL_ATS_DISABLED: external ATS applications are disabled in settings",
            )
        if runtime_controller.is_shutdown_requested() and not runtime_controller.has_active_worker(
            "external"
        ):
            logger.info("Shutdown requested — external worker admission is closed")
            return (
                "Cancelled",
                "CANCELLED_BY_SHUTDOWN: external worker was not started",
            )

        repo_root = Path(__file__).resolve().parents[2]
        worker_path = repo_root / "src" / "llm" / "external_apply_worker.py"
        provider_order = getattr(self, "provider_order", (self.model_type,))
        if provider_order and all(circuit_is_open(provider) for provider in provider_order):
            logger.error(
                "All configured external provider circuits are open; failing before worker launch"
            )
            return (
                "Error",
                "NEEDS_HUMAN: EXTERNAL_BLOCKER: external LLM provider circuit is open "
                "before worker launch | internal_code=EXTERNAL_PROVIDER_CIRCUIT_OPEN "
                "retryable=true",
            )

        state_dir = repo_root / "data" / "output" / "external_worker_states"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / f"{self.run_id}-{safe_job_id(linkedin_url or job_url)}.json"
        write_worker_state(
            state_path,
            "starting",
            run_id=self.run_id,
            submit_attempted=False,
            job_id=safe_job_id(linkedin_url or job_url),
            ats=detect_ats(job_url),
        )

        logger.info(
            "Starting isolated external application worker for: "
            f"{redact_text(job_url, limit=500)}"
        )
        emit_event(
            "agent_apply_started",
            "External apply worker started",
            application_id=f"app-{safe_job_id(linkedin_url or job_url)}",
            job_id=safe_job_id(linkedin_url or job_url),
            job_title=job_title,
            company_name=company_name,
            linkedin_url=linkedin_url,
            external_url=job_url,
            ats=detect_ats(job_url),
            application_type="EXTERNAL_ATS",
            last_workflow_step="worker_started",
            url=job_url,
        )

        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(worker_path),
                job_url,
                job_title,
                company_name,
                linkedin_url,
                str(state_path),
                cwd=str(repo_root),
                # The timed launcher signals its own process group. Browser
                # Use interprets SIGINT as an interactive pause, so keep this
                # worker in a separate session and let the parent own its
                # bounded drain lifecycle.
                start_new_session=(os.name == "posix"),
            )

            wait_task = asyncio.create_task(process.wait())

            async def wait_for_runtime_shutdown() -> None:
                while not runtime_controller.is_shutdown_requested():
                    await asyncio.sleep(0.2)

            shutdown_task = asyncio.create_task(wait_for_runtime_shutdown())
            try:
                done, _ = await asyncio.wait(
                    {wait_task, shutdown_task},
                    timeout=1200,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if wait_task in done:
                    return_code = wait_task.result()
                elif shutdown_task in done:
                    logger.warning(
                        "Shutdown requested with an external worker active; "
                        "closing admission and draining that worker"
                    )
                    try:
                        return_code = await asyncio.wait_for(
                            asyncio.shield(wait_task),
                            timeout=EXTERNAL_SHUTDOWN_NATURAL_DRAIN_SECONDS,
                        )
                    except TimeoutError:
                        logger.warning(
                            "External worker did not finish during the natural "
                            "shutdown drain; requesting graceful cancellation"
                        )
                        try:
                            process.terminate()
                        except ProcessLookupError:
                            pass
                        try:
                            return_code = await asyncio.wait_for(
                                asyncio.shield(wait_task),
                                timeout=EXTERNAL_SHUTDOWN_TERMINATE_SECONDS,
                            )
                        except TimeoutError:
                            state = read_worker_state(state_path)
                            submit_possible = submit_may_have_occurred(state)
                            logger.error(
                                "External worker ignored graceful cancellation; "
                                "ending its isolated process group"
                            )
                            kill_external_process_group(process)
                            return_code = await asyncio.shield(wait_task)
                        # Browser Use installs its own termination handler and
                        # may report exit code 0 while leaving Chromium alive.
                        # The parent initiated this cancellation, so it owns
                        # both descendant cleanup and submit-aware terminal
                        # classification.
                        kill_external_process_group(process)
                        state = read_worker_state(state_path)
                        if not (return_code == 0 and state.get("phase") == "submitted"):
                            submit_possible = submit_may_have_occurred(state)
                            write_worker_state(
                                state_path,
                                (
                                    "unverified_after_submit"
                                    if submit_possible
                                    else "cancelled_before_submit"
                                ),
                                run_id=self.run_id,
                                submit_attempted=submit_possible,
                                reason="shutdown drain expired",
                            )
                            return_code = 10 if submit_possible else 130
                else:
                    raise TimeoutError
            except TimeoutError:
                logger.error("External application exceeded 20 minute timeout")
                kill_external_process_group(process)
                await asyncio.shield(wait_task)

                if submit_may_have_occurred(read_worker_state(state_path)):
                    write_worker_state(
                        state_path,
                        "unverified_after_submit",
                        run_id=self.run_id,
                        submit_attempted=True,
                        reason="overall worker timeout",
                    )
                    return (
                        "Error",
                        "UNVERIFIED_AFTER_SUBMIT: external verification exceeded 20 minute timeout",
                    )
                return (
                    "Error",
                    "TECHNICAL_FAILURE: External application timed out after 20 minutes",
                )
            finally:
                if not shutdown_task.done():
                    shutdown_task.cancel()
                    try:
                        await shutdown_task
                    except asyncio.CancelledError:
                        pass

            state = read_worker_state(state_path)
            if state.get("phase") == "submitted" and return_code == 0:
                logger.info(
                    "External submission independently confirmed; "
                    "awaiting authoritative result publication"
                )
                return ("Success", "")

            result = classify_worker_result(return_code, state)
            if result[0] == "Cancelled":
                logger.info(result[1])
            else:
                logger.error(f"External application worker exited with code {return_code}")
            return result

        except Exception as e:
            logger.error(f"Could not start external application worker: {e}")
            return ("Error", f"TECHNICAL_FAILURE: {e}")


if __name__ == "__main__":
    """Test ApplyAgent functionality"""
    import traceback

    import dotenv

    from config.constants import BROWSER_STORAGE_STATE
    from src.pydantic_models.prompt_models import ResumeStructure
    from src.utils.utils import load_yaml_file

    async def test_apply_agent():
        """Test ApplyAgent with a real LinkedIn job posting"""
        logger.info("Starting ApplyAgent test...")

        try:
            # Load secrets for LLM
            secrets = dotenv.dotenv_values(".env")
            llm_api_key = secrets.get("llm_api_key", "")

            if not llm_api_key:
                logger.error("❌ LLM API key not found in .env file")
                return False

            # Initialize ApplyAgent
            apply_agent = ApplyAgent(
                llm_api_key, BROWSER_STORAGE_STATE, user_email=secrets.get("linkedin_email", "")
            )
            logger.info("ApplyAgent initialized successfully")

            # Load resume data
            RESUME_STRUCTURED_FILE = Path(RESUME_DIR) / "structured_resume.yaml"
            RESUME_TEXT_FILE = Path(RESUME_DIR) / "resume_text.txt"

            if not RESUME_STRUCTURED_FILE.exists():
                logger.error(f"❌ Resume structured file not found: {RESUME_STRUCTURED_FILE}")
                return False

            if not RESUME_TEXT_FILE.exists():
                logger.error(f"❌ Resume text file not found: {RESUME_TEXT_FILE}")
                return False

            # Load and set resume data
            resume_structured = load_yaml_file(RESUME_STRUCTURED_FILE)
            resume_structured = ResumeStructure(**resume_structured).model_dump()

            with open(RESUME_TEXT_FILE, "r") as f:
                resume_text = f.read()

            # Set resume and job for the agent
            apply_agent.set_resume(resume_text)
            logger.info("Resume and job data set successfully")

            # Test the apply_to_job method
            vacancy_url = "https://app.searchwithjack.com/jobs/4372944?utm_source=linkedin-direct-apply-4372944&comet_source=linkedin"
            logger.info(f"Testing ApplyAgent.apply_to_job method with job: {vacancy_url}")
            logger.info("This will open a browser and attempt to apply to the job...")

            # Run the application
            await apply_agent.apply_to_job(vacancy_url)

            logger.info("✅ ApplyAgent test completed successfully!")
            logger.info("Check the browser window to see the application process")
            return True

        except Exception as e:
            logger.error(f"❌ ApplyAgent test failed with error: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    # Run the test
    success = asyncio.run(test_apply_agent())
    if success:
        print("✅ Test passed!")
    else:
        print("❌ Test failed!")
