import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.llm.apply_agent as apply_module
from src.llm.apply_agent import (
    ApplyAgent,
    ExternalATSProgressStalled,
    completed_semantic_form_action_keys,
    is_strong_final_submit_control,
)
from src.llm.external_account_registration import RegistrationResult
from src.llm.external_worker_state import read_worker_state, write_worker_state


@pytest.fixture(autouse=True)
def isolate_production_event_sink(monkeypatch):
    """Keep synthetic external-worker tests out of the live campaign stream."""

    sink = MagicMock()
    monkeypatch.setattr(apply_module, "emit_event", sink)
    return sink


class _History:
    def errors(self):
        return ["429 RESOURCE_EXHAUSTED after final submit"]

    def final_result(self):
        return ""

    def is_successful(self):
        return False

    def is_validated(self):
        return False

    def judgement(self):
        return {}


class _Page:
    async def evaluate(self, _script):
        return "Thank you for applying."

    async def get_url(self):
        return "https://careers.example.invalid/application/confirmation"

    async def get_title(self):
        return "Application confirmation"

    async def screenshot(self):
        return None


@pytest.mark.parametrize(
    "label",
    ("Submit application", "Send application", "Send my application", "Complete the job application"),
)
def test_strong_final_submit_semantics_cover_stage_independent_application_labels(label):
    assert is_strong_final_submit_control(label) is True


def test_strong_final_submit_semantics_do_not_match_ordinary_send_controls():
    assert is_strong_final_submit_control("Send application feedback") is False


class _HiddenLandingPage:
    """Synthetic landing page whose only control is deliberately invisible."""

    _target_id = "landing-tab"
    url = "https://careers.example.invalid/jobs/opaque?token=query-token-sentinel"

    async def evaluate(self, script, *_args):
        if "document.querySelectorAll" in script:
            return json.dumps(
                {
                    "controls": [
                        {
                            "index": 0,
                            "label": "Apply for candidate-private@example.invalid",
                            "href": self.url,
                            "kind": "BUTTON",
                            "role": "button",
                            "aria_label": "Apply now",
                            "title": "Apply",
                            "value": "candidate-answer-sentinel",
                            "name": "candidate-answer",
                            "id": "candidate-private-input",
                            "visible": False,
                        }
                    ],
                    "frame_count": 1,
                }
            )
        return "Careers at Example"

    async def get_url(self):
        return self.url

    async def get_title(self):
        return "Example careers"

    async def screenshot(self):
        return None


class _EntryProsePage:
    """Landing fixture whose job description resembles an application form.

    This models the FetchJobs regression: skills/education prose appears beside
    one real Apply CTA. The deterministic worker hook must use the CTA and
    structural field count rather than treating the prose as an already-open
    form stage.
    """

    _target_id = "entry-prose-tab"
    url = "https://careers.example.invalid/jobs/support-role"

    def __init__(self):
        self.form_open = False
        self.entry_clicks = 0

    async def evaluate(self, script, *_args):
        if "(index) => {" in script and "el.click()" in script:
            self.entry_clicks += 1
            self.form_open = True
            return True
        if "JSON.stringify({" in script and "editable_field_count" in script:
            return json.dumps(
                {
                    "controls": [
                        {
                            "index": 0,
                            "label": "Apply",
                            "href": "",
                            "kind": "BUTTON",
                            "role": "button",
                            "aria_label": "Apply",
                            "title": "",
                            "value": "",
                            "name": "",
                            "id": "apply",
                            "visible": True,
                        }
                    ],
                    "frame_count": 0,
                    "editable_field_count": 2 if self.form_open else 0,
                }
            )
        if "document.body" in script:
            if self.form_open:
                return "Submit application. Personal information. Legal name. Email address."
            return "Technical skills and education requirements. Apply"
        # Registration detection sees neither an actual account form nor a
        # registration submit control, even though the landing CTA is visible.
        return False

    async def get_url(self):
        return self.url

    async def get_title(self):
        return "Example careers"

    async def screenshot(self):
        return None


class _NoSubmitHistory:
    def errors(self):
        return []

    def final_result(self):
        return "Application remains incomplete."

    def is_successful(self):
        return False

    def is_validated(self):
        return False

    def judgement(self):
        return {}


class _ActionRecord:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, **_kwargs):
        return self.payload


def test_completed_semantic_form_action_keys_keep_values_private_and_ignore_errors():
    """Only successful ordinary actions provide a safe same-page progress key."""

    history = SimpleNamespace(
        history=[
            SimpleNamespace(
                model_output=SimpleNamespace(
                    action=[
                        _ActionRecord({"input": {"index": 12, "text": "private value"}}),
                        _ActionRecord({"select_dropdown": {"index": 8, "text": "private option"}}),
                        _ActionRecord({"input": {"index": 3, "text": "failed private value"}}),
                        _ActionRecord({"wait": {"seconds": 1}}),
                    ]
                ),
                result=[
                    SimpleNamespace(error=None),
                    SimpleNamespace(error=None),
                    SimpleNamespace(error="input failed"),
                    SimpleNamespace(error=None),
                ],
            )
        ]
    )

    assert completed_semantic_form_action_keys(SimpleNamespace(history=history)) == (
        "input:12",
        "select_dropdown:8",
    )


class _AuthStallPage:
    """Synthetic account page used to prove terminal evidence is stage-agnostic."""

    _target_id = "auth-tab"
    url = "https://jobs.lever.co/example/apply"

    async def evaluate(self, script, *_args):
        if "document.querySelectorAll" in script:
            return json.dumps(
                {
                    "controls": [
                        {
                            "index": 0,
                            "label": "Continue",
                            "href": "",
                            "kind": "BUTTON",
                            "role": "button",
                            "aria_label": "Continue",
                            "title": "",
                            "value": "",
                            "name": "",
                            "id": "continue-application",
                            "visible": True,
                        }
                    ],
                    "frame_count": 0,
                }
            )
        return "Sign in to continue. Password required."

    async def get_url(self):
        return self.url

    async def get_title(self):
        return "Candidate sign in"

    async def screenshot(self):
        return None


@pytest.mark.asyncio
async def test_repeated_external_page_state_stops_the_pre_submit_worker(tmp_path):
    """A loop guard must terminate the worker rather than just emit telemetry."""

    page = _Page()
    browser_session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))

    class _Agent:
        def __init__(self, **_kwargs):
            self.browser_session = browser_session
            self.history = SimpleNamespace(history=[])

        async def run(self, **kwargs):
            for _ in range(3):
                self.history.history.append(
                    SimpleNamespace(
                        model_output=SimpleNamespace(
                            action=[_ActionRecord({"input": {"index": 7, "text": "private"}})]
                        ),
                        result=[SimpleNamespace(error=None)],
                    )
                )
                await kwargs["on_step_end"](self)
            raise AssertionError("the unchanged page loop was not stopped")

    worker_browser = SimpleNamespace(kill=AsyncMock())
    agent = object.__new__(ApplyAgent)
    agent.api_key = "test-key"
    agent.primary_api_key = "test-key"
    agent.browser_storage_state = str(tmp_path / "state.json")
    agent.worker_state_path = str(tmp_path / "worker.json")
    agent.provider_order = ("gemini",)
    agent.model_type = "gemini"
    agent.model = "gemini-test"
    agent.llm_api_url = None
    agent.resume_readable = "resume text"
    agent.run_id = "run-external-stall"
    agent.calls_log = str(tmp_path / "calls.yaml")
    agent._create_browser = MagicMock(return_value=worker_browser)
    agent.select_model_type = MagicMock(return_value=MagicMock())
    agent._log_token_usage = MagicMock(return_value=0)

    with (
        patch("src.llm.apply_agent.GMAIL_APPLICATION_INTEGRATION", False),
        patch("src.llm.apply_agent.APPLY_AGENT_FALLBACK_MODEL", ""),
        patch("src.llm.apply_agent.Agent", _Agent),
        patch("src.llm.apply_agent.circuit_is_open", return_value=False),
        patch("src.llm.apply_agent.get_ready_made_resume", return_value=Path("/tmp/resume.docx")),
    ):
        with pytest.raises(RuntimeError, match="EXTERNAL_ATS_STALLED"):
            await agent.apply("https://careers.example.invalid/apply")

    worker_browser.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_successful_form_actions_do_not_false_stall_on_static_form_text(tmp_path):
    """Greenhouse-style value-only entry is progress; repeated actions are not."""

    page = _AuthStallPage()
    browser_session = SimpleNamespace(
        must_get_current_page=AsyncMock(return_value=page),
        get_tabs=AsyncMock(return_value=[SimpleNamespace(target_id="auth-tab", url=page.url)]),
    )
    result_history = _NoSubmitHistory()

    class _Agent:
        def __init__(self, **_kwargs):
            self.browser_session = browser_session
            self.history = SimpleNamespace(history=[])

        async def run(self, **kwargs):
            for index in (4, 5, 6):
                self.history.history.append(
                    SimpleNamespace(
                        model_output=SimpleNamespace(
                            action=[_ActionRecord({"input": {"index": index, "text": "private"}})]
                        ),
                        result=[SimpleNamespace(error=None)],
                    )
                )
                await kwargs["on_step_end"](self)
            return result_history

    worker_browser = SimpleNamespace(kill=AsyncMock())
    agent = object.__new__(ApplyAgent)
    agent.api_key = "test-key"
    agent.primary_api_key = "test-key"
    agent.browser_storage_state = str(tmp_path / "state.json")
    agent.worker_state_path = str(tmp_path / "worker.json")
    agent.provider_order = ("gemini",)
    agent.model_type = "gemini"
    agent.model = "gemini-test"
    agent.llm_api_url = None
    agent.resume_readable = "resume text"
    agent.run_id = "run-static-form-action-progress"
    agent.calls_log = str(tmp_path / "calls.yaml")
    agent._create_browser = MagicMock(return_value=worker_browser)
    agent.select_model_type = MagicMock(return_value=MagicMock())
    agent._log_token_usage = MagicMock(return_value=0)

    with (
        patch("src.llm.apply_agent.GMAIL_APPLICATION_INTEGRATION", False),
        patch("src.llm.apply_agent.APPLY_AGENT_FALLBACK_MODEL", ""),
        patch("src.llm.apply_agent.Agent", _Agent),
        patch("src.llm.apply_agent.circuit_is_open", return_value=False),
        patch("src.llm.apply_agent.get_ready_made_resume", return_value=Path("/tmp/resume.docx")),
        patch("src.llm.apply_agent.load_candidate_profile", return_value={}),
        patch("src.llm.apply_agent.save_evidence"),
        patch("src.llm.apply_agent.save_screenshot_b64"),
    ):
        with pytest.raises(RuntimeError, match="EXTERNAL_AGENT_FAILED"):
            await agent.apply(
                page.url,
                linkedin_url="https://www.linkedin.com/jobs/view/1234567895/",
            )

    worker_browser.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_structural_entry_evidence_reaches_worker_hook_before_job_prose(tmp_path):
    """A visible Apply CTA recovers a prose-heavy landing page exactly once.

    This is deliberately an ApplyAgent-level regression, not just an
    ``infer_stage`` fixture. It proves the live observer receives the DOM
    structure, holds the page in APPLY_ENTRY, and uses the bounded resolver
    before the unchanged-page guard can classify skills/education prose as a
    form stage.
    """

    page = _EntryProsePage()
    browser_session = SimpleNamespace(
        must_get_current_page=AsyncMock(return_value=page),
        get_tabs=AsyncMock(
            return_value=[SimpleNamespace(target_id="entry-prose-tab", url=page.url)]
        ),
        get_pages=AsyncMock(return_value=[page]),
    )
    history = _NoSubmitHistory()

    class _Agent:
        def __init__(self, **_kwargs):
            self.browser_session = browser_session

        async def run(self, **kwargs):
            # The first observation establishes APPLY_ENTRY. The second gets
            # one deterministic recovery opportunity and opens the form.
            await kwargs["on_step_end"](self)
            await kwargs["on_step_end"](self)
            return history

    worker_browser = SimpleNamespace(kill=AsyncMock())
    agent = object.__new__(ApplyAgent)
    agent.api_key = "test-key"
    agent.primary_api_key = "test-key"
    agent.browser_storage_state = str(tmp_path / "state.json")
    agent.worker_state_path = str(tmp_path / "worker.json")
    agent.provider_order = ("gemini",)
    agent.model_type = "gemini"
    agent.model = "gemini-test"
    agent.llm_api_url = None
    agent.resume_readable = "resume text"
    agent.run_id = "run-entry-prose-structural-evidence"
    agent.calls_log = str(tmp_path / "calls.yaml")
    agent._create_browser = MagicMock(return_value=worker_browser)
    agent.select_model_type = MagicMock(return_value=MagicMock())
    agent._log_token_usage = MagicMock(return_value=0)

    with (
        patch("src.llm.apply_agent.GMAIL_APPLICATION_INTEGRATION", False),
        patch("src.llm.apply_agent.APPLY_AGENT_FALLBACK_MODEL", ""),
        patch("src.llm.apply_agent.Agent", _Agent),
        patch("src.llm.apply_agent.circuit_is_open", return_value=False),
        patch("src.llm.apply_agent.get_ready_made_resume", return_value=Path("/tmp/resume.docx")),
        patch("src.llm.apply_agent.load_candidate_profile", return_value={}),
        patch("src.llm.apply_agent.save_evidence"),
        patch("src.llm.apply_agent.save_screenshot_b64"),
        patch("src.llm.apply_agent.logger.info") as info,
        patch(
            "src.llm.apply_agent.AccountRegistrationGuard.complete",
            new_callable=AsyncMock,
        ) as complete_registration,
    ):
        with pytest.raises(RuntimeError, match="EXTERNAL_AGENT_FAILED"):
            await agent.apply(
                page.url,
                job_title="Support role",
                company_name="Example",
                linkedin_url="https://www.linkedin.com/jobs/view/1234567894/",
            )

    assert page.entry_clicks == 1
    assert page.form_open is True
    complete_registration.assert_not_awaited()
    worker_browser.kill.assert_awaited_once()
    logged = "\n".join(str(call.args[0]) for call in info.call_args_list if call.args)
    assert "APPLICATION_ENTRY_PROGRESS | stage=contact_information" in logged
    assert "APPLICATION_ENTRY_PROGRESS | stage=submit" not in logged
    assert "ATS_STAGE | site=generic stage=submit status=observed" not in logged


@pytest.mark.asyncio
async def test_terminal_landing_stall_captures_one_raw_resolver_snapshot(tmp_path):
    """A bounded pre-submit stall records replayable diagnostics once."""

    page = _HiddenLandingPage()
    browser_session = SimpleNamespace(
        must_get_current_page=AsyncMock(return_value=page),
        get_tabs=AsyncMock(
            return_value=[SimpleNamespace(target_id="landing-tab", url=page.url)]
        ),
        get_pages=AsyncMock(return_value=[page]),
    )

    class _Agent:
        def __init__(self, **_kwargs):
            self.browser_session = browser_session

        async def run(self, **kwargs):
            for _ in range(3):
                await kwargs["on_step_end"](self)
            raise AssertionError("the unchanged page loop was not stopped")

    worker_browser = SimpleNamespace(kill=AsyncMock())
    agent = object.__new__(ApplyAgent)
    agent.api_key = "test-key"
    agent.primary_api_key = "test-key"
    agent.browser_storage_state = str(tmp_path / "state.json")
    agent.worker_state_path = str(tmp_path / "worker.json")
    agent.provider_order = ("gemini",)
    agent.model_type = "gemini"
    agent.model = "gemini-test"
    agent.llm_api_url = None
    agent.resume_readable = "resume text"
    agent.run_id = "run-external-landing-diagnostic"
    agent.calls_log = str(tmp_path / "calls.yaml")
    agent._create_browser = MagicMock(return_value=worker_browser)
    agent.select_model_type = MagicMock(return_value=MagicMock())
    agent._log_token_usage = MagicMock(return_value=0)

    with (
        patch("src.llm.apply_agent.GMAIL_APPLICATION_INTEGRATION", False),
        patch("src.llm.apply_agent.APPLY_AGENT_FALLBACK_MODEL", ""),
        patch("src.llm.apply_agent.Agent", _Agent),
        patch("src.llm.apply_agent.circuit_is_open", return_value=False),
        patch("src.llm.apply_agent.get_ready_made_resume", return_value=Path("/tmp/resume.docx")),
        patch("src.llm.apply_agent.load_candidate_profile", return_value={}),
        patch("src.llm.apply_agent.save_landing_diagnostic") as save_diagnostic,
    ):
        with pytest.raises(RuntimeError, match="EXTERNAL_ATS_STALLED"):
            await agent.apply(
                page.url,
                linkedin_url="https://www.linkedin.com/jobs/view/1234567890/",
            )

    save_diagnostic.assert_called_once()
    captured = save_diagnostic.call_args.kwargs
    assert captured["reason"] == "same_page_state"
    assert captured["stage"].value == "landing"
    assert captured["frame_count"] == 1
    assert captured["tabs"] == [{"selected": True, "url": page.url}]
    assert captured["controls"] == [
        {
            "index": 0,
            "label": "Apply for candidate-private@example.invalid",
            "href": page.url,
            "kind": "BUTTON",
            "role": "button",
            "aria_label": "Apply now",
            "title": "Apply",
            "value": "candidate-answer-sentinel",
            "name": "candidate-answer",
            "id": "candidate-private-input",
            "visible": False,
        }
    ]
    state = read_worker_state(tmp_path / "worker.json")
    assert state["phase"] == "pre_submit"
    assert state["submit_attempted"] is False
    worker_browser.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_auth_stall_captures_sanitized_control_metadata(tmp_path):
    """Non-landing stalls retain one safe snapshot without replaying the form."""

    page = _AuthStallPage()
    browser_session = SimpleNamespace(
        must_get_current_page=AsyncMock(return_value=page),
        get_tabs=AsyncMock(return_value=[SimpleNamespace(target_id="auth-tab", url=page.url)]),
    )

    class _Agent:
        def __init__(self, **_kwargs):
            self.browser_session = browser_session

        async def run(self, **kwargs):
            for _ in range(3):
                await kwargs["on_step_end"](self)
            raise AssertionError("the unchanged auth page loop was not stopped")

    worker_browser = SimpleNamespace(kill=AsyncMock())
    agent = object.__new__(ApplyAgent)
    agent.api_key = "test-key"
    agent.primary_api_key = "test-key"
    agent.browser_storage_state = str(tmp_path / "state.json")
    agent.worker_state_path = str(tmp_path / "worker.json")
    agent.provider_order = ("gemini",)
    agent.model_type = "gemini"
    agent.model = "gemini-test"
    agent.llm_api_url = None
    agent.resume_readable = "resume text"
    agent.run_id = "run-external-auth-diagnostic"
    agent.calls_log = str(tmp_path / "calls.yaml")
    agent._create_browser = MagicMock(return_value=worker_browser)
    agent.select_model_type = MagicMock(return_value=MagicMock())
    agent._log_token_usage = MagicMock(return_value=0)

    with (
        patch("src.llm.apply_agent.GMAIL_APPLICATION_INTEGRATION", False),
        patch("src.llm.apply_agent.APPLY_AGENT_FALLBACK_MODEL", ""),
        patch("src.llm.apply_agent.Agent", _Agent),
        patch("src.llm.apply_agent.circuit_is_open", return_value=False),
        patch("src.llm.apply_agent.get_ready_made_resume", return_value=Path("/tmp/resume.docx")),
        patch("src.llm.apply_agent.load_candidate_profile", return_value={}),
        patch("src.llm.apply_agent.save_landing_diagnostic") as save_diagnostic,
    ):
        with pytest.raises(RuntimeError, match="EXTERNAL_ATS_STALLED"):
            await agent.apply(
                page.url,
                linkedin_url="https://www.linkedin.com/jobs/view/1234567892/",
            )

    save_diagnostic.assert_called_once()
    captured = save_diagnostic.call_args.kwargs
    assert captured["reason"] == "same_page_state"
    assert captured["stage"].value == "auth"
    assert captured["frame_count"] == 0
    assert captured["controls"] == [
        {
            "index": 0,
            "label": "Continue",
            "href": "",
            "kind": "BUTTON",
            "role": "button",
            "aria_label": "Continue",
            "title": "",
            "value": "",
            "name": "",
            "id": "continue-application",
            "visible": True,
        }
    ]
    worker_browser.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_failure_after_final_submit_runs_deterministic_verification(tmp_path):
    state_path = tmp_path / "worker.json"
    page = _Page()
    browser_session = SimpleNamespace(
        must_get_current_page=AsyncMock(return_value=page),
    )
    history = _History()

    provider_launches = []

    class _Agent:
        def __init__(self, **_kwargs):
            provider_launches.append(agent.model_type)
            self.browser_session = browser_session

        async def run(self, **_kwargs):
            write_worker_state(state_path, "submit_attempted", submit_attempted=True)
            return history

    worker_browser = SimpleNamespace(kill=AsyncMock())
    agent = object.__new__(ApplyAgent)
    agent.api_key = "test-key"
    agent.primary_api_key = "test-key"
    agent.browser_storage_state = str(tmp_path / "state.json")
    agent.worker_state_path = str(state_path)
    agent.provider_order = ("gemini", "openai")
    agent.model_type = "gemini"
    agent.model = "gemini-test"
    agent.llm_api_url = None
    agent.resume_readable = "resume text"
    agent.run_id = "run-post-submit-provider-failure"
    agent.calls_log = str(tmp_path / "calls.yaml")
    agent._create_browser = MagicMock(return_value=worker_browser)
    agent.select_model_type = MagicMock(return_value=MagicMock())
    agent._log_token_usage = MagicMock(return_value=0)

    with (
        patch("src.llm.apply_agent.GMAIL_APPLICATION_INTEGRATION", False),
        patch("src.llm.apply_agent.APPLY_AGENT_FALLBACK_MODEL", ""),
        patch("src.llm.apply_agent.Agent", _Agent),
        patch("src.llm.apply_agent.circuit_is_open", return_value=False),
        patch("src.llm.apply_agent.open_circuit"),
        patch("src.llm.apply_agent.get_ready_made_resume", return_value=Path("/tmp/resume.docx")),
        patch("src.llm.apply_agent.save_evidence") as save_evidence,
        patch("src.llm.apply_agent.save_screenshot_b64"),
    ):
        await agent.apply(
            "https://careers.example.invalid/apply",
            job_title="Support role",
            company_name="Example",
            linkedin_url="https://www.linkedin.com/jobs/view/1234567890/",
        )

    state = read_worker_state(state_path)
    assert state["phase"] == "submitted"
    assert state["verification_source"] == "page_dom"
    assert save_evidence.call_args.kwargs["result"] == "SUBMITTED"
    assert provider_launches == ["gemini"]
    worker_browser.kill.assert_awaited_once()


class _RegistrationHistory:
    def errors(self):
        return []

    def final_result(self):
        return "Registration completed; application did not reach submit."

    def is_successful(self):
        return False

    def is_validated(self):
        return None

    def judgement(self):
        return {}


class _RegistrationPage:
    async def evaluate(self, _script):
        return "Create Account"

    async def get_url(self):
        return "https://career.example.invalid/register"

    async def get_title(self):
        return "Create account"

    async def screenshot(self):
        return None


@pytest.mark.asyncio
async def test_live_account_creation_stage_uses_guarded_registration_before_model_retries(
    tmp_path,
):
    """A real registration stage invokes the deterministic one-shot helper.

    This mirrors the SuccessFactors regression: Browser Use reaches a page
    with a Create Account form, and the per-step observer takes over before
    the model can apply an invalid generic select/radio action.
    """

    page = _RegistrationPage()
    browser_session = SimpleNamespace(
        must_get_current_page=AsyncMock(return_value=page),
    )
    history = _RegistrationHistory()
    action_results = []
    complete_calls = []

    class _Agent:
        def __init__(self, **kwargs):
            self.browser_session = browser_session
            self.tools = kwargs["tools"]

        async def run(self, **kwargs):
            await kwargs["on_step_end"](self)
            action = self.tools.registry.registry.actions["complete_account_registration"]
            action_results.append(
                await action.function(
                    params=action.param_model(reason="already completed registration"),
                    browser_session=browser_session,
                )
            )
            return history

    async def complete_once(guard, _browser_session):
        complete_calls.append(True)
        guard._attempted = True
        guard._result = RegistrationResult(
            "ACCOUNT_CREATED", actions=5, detail="registration_transition_observed"
        )
        return guard._result

    worker_browser = SimpleNamespace(kill=AsyncMock())
    agent = object.__new__(ApplyAgent)
    agent.api_key = "test-key"
    agent.primary_api_key = "test-key"
    agent.browser_storage_state = str(tmp_path / "state.json")
    agent.worker_state_path = str(tmp_path / "worker.json")
    agent.provider_order = ("gemini",)
    agent.model_type = "gemini"
    agent.model = "gemini-test"
    agent.llm_api_url = None
    agent.resume_readable = "resume text"
    agent.run_id = "run-account-registration-observer"
    agent.calls_log = str(tmp_path / "calls.yaml")
    agent._create_browser = MagicMock(return_value=worker_browser)
    agent.select_model_type = MagicMock(return_value=MagicMock())
    agent._log_token_usage = MagicMock(return_value=0)

    with (
        patch("src.llm.apply_agent.GMAIL_APPLICATION_INTEGRATION", False),
        patch("src.llm.apply_agent.APPLY_AGENT_FALLBACK_MODEL", ""),
        patch("src.llm.apply_agent.Agent", _Agent),
        patch("src.llm.apply_agent.circuit_is_open", return_value=False),
        patch("src.llm.apply_agent.get_ready_made_resume", return_value=Path("/tmp/resume.docx")),
        patch(
            "src.llm.apply_agent.load_candidate_profile",
            return_value={
                "candidate": {
                    "email": "candidate@example.invalid",
                    "phone": "+15555550123",
                }
            },
        ),
        patch(
            "src.llm.apply_agent.registration_page_present",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.llm.apply_agent.AccountRegistrationGuard.complete",
            new=complete_once,
        ),
        patch("src.llm.apply_agent.logger.info") as info,
        patch("src.llm.apply_agent.save_evidence"),
        patch("src.llm.apply_agent.save_screenshot_b64"),
    ):
        with pytest.raises(RuntimeError, match="EXTERNAL_AGENT_FAILED"):
            await agent.apply(
                "https://career.example.invalid/apply",
                job_title="Support role",
                company_name="Example",
                linkedin_url="https://www.linkedin.com/jobs/view/1234567891/",
            )

    assert complete_calls == [True]
    assert "ALREADY DISPATCHED" in action_results[0]
    worker_browser.kill.assert_awaited_once()
    logged = " ".join(str(call.args) for call in info.call_args_list)
    assert "candidate@example.invalid" not in logged
    assert "+15555550123" not in logged
    assert "detail=registration_transition_observed" in logged
    assert "APPLICATION_ENTRY_SCAN | stage=apply_entry" in logged


class _ProviderFailureBeforeSubmitHistory:
    def errors(self):
        return ["503 service unavailable before submit"]

    def final_result(self):
        return ""

    def is_successful(self):
        return False

    def is_validated(self):
        return None

    def judgement(self):
        return {}


class _UnverifiedHistory:
    def errors(self):
        return []

    def final_result(self):
        return "Application remains in review."

    def is_successful(self):
        return False

    def is_validated(self):
        return None

    def judgement(self):
        return {}


class _ReviewPage:
    async def evaluate(self, _script):
        return "Application review"

    async def get_url(self):
        return "https://careers.example.invalid/application/review"

    async def get_title(self):
        return "Application review"

    async def screenshot(self):
        return None


class _VerifiedResumeGuard:
    attempted = True
    succeeded = True
    failed = False

    async def stop_after_failure(self, _agent):
        return None


@pytest.mark.asyncio
async def test_provider_fallback_after_verified_upload_reuses_current_browser_and_tools(tmp_path):
    """A provider handoff after resume upload never restarts or re-uploads."""

    page = _ReviewPage()
    browser_session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))
    first_history = _ProviderFailureBeforeSubmitHistory()
    second_history = _UnverifiedHistory()
    launches = []

    class _Agent:
        def __init__(self, **kwargs):
            launches.append(kwargs)
            self.browser_session = browser_session

        async def run(self, **_kwargs):
            return first_history if len(launches) == 1 else second_history

    worker_browser = SimpleNamespace(kill=AsyncMock())
    agent = object.__new__(ApplyAgent)
    agent.api_key = "test-key"
    agent.primary_api_key = "test-key"
    agent.browser_storage_state = str(tmp_path / "state.json")
    agent.worker_state_path = str(tmp_path / "worker.json")
    agent.provider_order = ("gemini", "openai")
    agent.model_type = "gemini"
    agent.model = "gemini-test"
    agent.llm_api_url = None
    agent.resume_readable = "resume text"
    agent.run_id = "run-upload-provider-handoff"
    agent.calls_log = str(tmp_path / "calls.yaml")
    agent._create_browser = MagicMock(return_value=worker_browser)
    agent.select_model_type = MagicMock(return_value=MagicMock())
    agent._log_token_usage = MagicMock(return_value=0)

    with (
        patch("src.llm.apply_agent.GMAIL_APPLICATION_INTEGRATION", False),
        patch("src.llm.apply_agent.APPLY_AGENT_FALLBACK_MODEL", ""),
        patch("src.llm.apply_agent.Agent", _Agent),
        patch("src.llm.apply_agent.circuit_is_open", return_value=False),
        patch("src.llm.apply_agent.open_circuit"),
        patch("src.llm.apply_agent.get_ready_made_resume", return_value=Path("/tmp/resume.docx")),
        patch(
            "src.llm.apply_agent.register_resume_upload_action", return_value=_VerifiedResumeGuard()
        ),
        patch("src.llm.apply_agent.save_evidence"),
        patch("src.llm.apply_agent.save_screenshot_b64"),
    ):
        with pytest.raises(RuntimeError, match="EXTERNAL_AGENT_FAILED"):
            await agent.apply(
                "https://careers.example.invalid/apply",
                job_title="Support role",
                company_name="Example",
                linkedin_url="https://www.linkedin.com/jobs/view/1234567892/",
            )

    assert len(launches) == 2
    assert launches[0]["browser"] is launches[1]["browser"] is worker_browser
    assert launches[0]["tools"] is launches[1]["tools"]
    assert launches[0]["directly_open_url"] is False
    assert launches[1]["directly_open_url"] is False
    assert "Provider handoff" in launches[1]["task"]
    worker_browser.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_fallback_recovers_provider_error_hidden_by_progress_hook(tmp_path):
    """A Browser Use hook error must not hide a provider failure from fallback."""

    page = _ReviewPage()
    browser_session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))
    first_history = _ProviderFailureBeforeSubmitHistory()
    second_history = _UnverifiedHistory()
    launches = []

    class _Agent:
        def __init__(self, **kwargs):
            launches.append(kwargs)
            self.browser_session = browser_session
            self.history = first_history if len(launches) == 1 else second_history

        async def run(self, **kwargs):
            if len(launches) == 1:
                for _ in range(3):
                    await kwargs["on_step_end"](self)
                raise AssertionError("the first provider hook did not stop the repeated page")
            await kwargs["on_step_end"](self)
            return second_history

    worker_browser = SimpleNamespace(kill=AsyncMock())
    agent = object.__new__(ApplyAgent)
    agent.api_key = "test-key"
    agent.primary_api_key = "test-key"
    agent.browser_storage_state = str(tmp_path / "state.json")
    agent.worker_state_path = str(tmp_path / "worker.json")
    agent.provider_order = ("gemini", "openai")
    agent.model_type = "gemini"
    agent.model = "gemini-test"
    agent.llm_api_url = None
    agent.resume_readable = "resume text"
    agent.run_id = "run-provider-hook-error-fallback"
    agent.calls_log = str(tmp_path / "calls.yaml")
    agent._create_browser = MagicMock(return_value=worker_browser)
    agent.select_model_type = MagicMock(return_value=MagicMock())
    agent._log_token_usage = MagicMock(return_value=0)

    with (
        patch("src.llm.apply_agent.GMAIL_APPLICATION_INTEGRATION", False),
        patch("src.llm.apply_agent.APPLY_AGENT_FALLBACK_MODEL", ""),
        patch("src.llm.apply_agent.Agent", _Agent),
        patch("src.llm.apply_agent.circuit_is_open", return_value=False),
        patch("src.llm.apply_agent.open_circuit") as open_circuit,
        patch("src.llm.apply_agent.get_ready_made_resume", return_value=Path("/tmp/resume.docx")),
        patch("src.llm.apply_agent.save_evidence"),
        patch("src.llm.apply_agent.save_screenshot_b64"),
    ):
        with pytest.raises(RuntimeError, match="EXTERNAL_AGENT_FAILED"):
            await agent.apply(
                "https://careers.example.invalid/apply",
                job_title="Support role",
                company_name="Example",
                linkedin_url="https://www.linkedin.com/jobs/view/1234567893/",
            )

    assert len(launches) == 2
    assert launches[0]["browser"] is launches[1]["browser"] is worker_browser
    assert open_circuit.call_args_list[0].args[0] == "gemini"
    assert open_circuit.call_args_list[0].args[2] == "transient_unavailable"
    worker_browser.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_guarded_browser_actions_block_registration_final_submit_and_password_bypasses(
    tmp_path,
):
    """Browser Use's generic action names cannot cross protected boundaries."""

    class _GuardPage:
        def __init__(self, body="Review your application", url="https://careers.example.invalid/application/review"):
            self.body = body
            self.url = url
            self.password_fields = []

        async def evaluate(self, _script):
            return self.body

        async def get_url(self):
            return self.url

        async def get_title(self):
            return "Application review"

        async def screenshot(self):
            return None

        async def get_elements_by_css_selector(self, selector):
            if selector == 'input[type="password"]':
                return self.password_fields
            return []

    class _Node:
        def __init__(self, label, attributes=None):
            self._label = label
            self.attributes = attributes or {"aria-label": label}

        def get_meaningful_text_for_llm(self):
            return self._label

    page = _GuardPage(
        "Upload your resume",
        "https://careers.example.invalid/application-form",
    )
    password_field = SimpleNamespace(
        get_attribute=AsyncMock(return_value=None),
        fill=AsyncMock(),
    )
    page.password_fields = [password_field]
    security_page = _GuardPage("Enter OTP verification code")
    current_page = {"value": page}

    async def active_page():
        return current_page["value"]

    event_bus = MagicMock()
    browser_session = SimpleNamespace(
        must_get_current_page=active_page,
        get_dom_element_by_index=AsyncMock(
            side_effect=[
                _Node("Create Account"),
                _Node("Send application"),
                _Node("Password", {"type": "password", "aria-label": "Password"}),
                _Node("Sign Up With Google"),
            ]
        ),
        event_bus=event_bus,
    )
    history = _UnverifiedHistory()
    action_results = []
    action_names = []

    class _Agent:
        def __init__(self, **kwargs):
            self.browser_session = browser_session
            self.tools = kwargs["tools"]
            action_names.extend(self.tools.registry.registry.actions)

        async def run(self, **_kwargs):
            async def invoke(name, **values):
                action = self.tools.registry.registry.actions[name]
                return await action.function(
                    params=action.param_model(**values), browser_session=browser_session
                )

            action_results.append(await invoke("click", index=1))
            action_results.append(await invoke("click", index=2))
            action_results.append(await invoke("input", index=3, text="not-a-password"))
            action_results.append(await invoke("click", index=4))
            action_results.append(
                await invoke("fill_secure_account_password", reason="password field visible")
            )
            current_page["value"] = security_page
            action_results.append(await invoke("click", index=5))
            action_results.append(await invoke("submit_final_application", index=5))
            action_results.append(
                await invoke("fill_secure_account_password", reason="password field visible")
            )
            return history

    worker_browser = SimpleNamespace(kill=AsyncMock())
    agent = object.__new__(ApplyAgent)
    agent.api_key = "test-key"
    agent.primary_api_key = "test-key"
    agent.browser_storage_state = str(tmp_path / "state.json")
    agent.worker_state_path = str(tmp_path / "worker.json")
    agent.provider_order = ("gemini",)
    agent.model_type = "gemini"
    agent.model = "gemini-test"
    agent.llm_api_url = None
    agent.resume_readable = "resume text"
    agent.run_id = "run-guarded-browser-actions"
    agent.calls_log = str(tmp_path / "calls.yaml")
    agent._create_browser = MagicMock(return_value=worker_browser)
    agent.select_model_type = MagicMock(return_value=MagicMock())
    agent._log_token_usage = MagicMock(return_value=0)

    with (
        patch("src.llm.apply_agent.GMAIL_APPLICATION_INTEGRATION", False),
        patch("src.llm.apply_agent.APPLY_AGENT_FALLBACK_MODEL", ""),
        patch("src.llm.apply_agent.Agent", _Agent),
        patch("src.llm.apply_agent.circuit_is_open", return_value=False),
        patch("src.llm.apply_agent.get_ready_made_resume", return_value=Path("/tmp/resume.docx")),
        patch("src.llm.apply_agent.ats_account_registration_state", return_value="missing"),
        patch("src.llm.apply_agent.get_or_create_ats_password") as get_password,
        patch("src.llm.apply_agent.save_evidence"),
        patch("src.llm.apply_agent.save_screenshot_b64"),
    ):
        with pytest.raises(RuntimeError, match="APPLICATION_NEEDS_HUMAN"):
            await agent.apply(
                "https://careers.example.invalid/apply",
                job_title="Support role",
                company_name="Example",
                linkedin_url="https://www.linkedin.com/jobs/view/1234567893/",
            )

    assert {"evaluate", "send_keys", "upload_file", "navigate", "read_file"}.isdisjoint(
        action_names
    )
    assert "click" in action_names
    assert "complete_account_registration" in action_names
    assert "submit_final_application" in action_names
    assert "ACCOUNT REGISTRATION SUBMIT BLOCKED" in action_results[0].extracted_content
    assert "FINAL SUBMIT BLOCKED" in action_results[1].extracted_content
    assert "PASSWORD INPUT BLOCKED" in action_results[2].extracted_content
    assert "NEEDS_HUMAN" in action_results[3].extracted_content
    assert "NEEDS_HUMAN" in action_results[4]
    assert "NEEDS_HUMAN" in action_results[5].extracted_content
    assert "NEEDS_HUMAN" in action_results[6]
    assert "NEEDS_HUMAN" in action_results[7]
    get_password.assert_not_called()
    password_field.fill.assert_not_awaited()
    event_bus.dispatch.assert_not_called()
    worker_browser.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_email_verification_unavailable_terminates_as_needs_human(tmp_path):
    class _Page:
        async def get_url(self):
            return "https://careers.example.invalid/application/account"

    browser_session = SimpleNamespace(
        must_get_current_page=AsyncMock(return_value=_Page()),
    )
    worker_browser = SimpleNamespace(kill=AsyncMock())

    class _Agent:
        def __init__(self, **kwargs):
            self.browser_session = browser_session
            self.tools = kwargs["tools"]

        async def run(self, **_kwargs):
            action = self.tools.registry.registry.actions["wait_for_email_verification"]
            params = action.param_model(reason="activation email is required")
            try:
                await action.function(params=params, browser_session=browser_session)
            except RuntimeError:
                # Browser Use can convert an action exception into a history
                # error and return normally. The outer worker must preserve
                # the human boundary instead of classifying "unavailable" as
                # an LLM provider failure.
                return _History()
            raise AssertionError("human-required email verification was not terminal")

    agent = object.__new__(ApplyAgent)
    agent.api_key = "test-key"
    agent.primary_api_key = "test-key"
    agent.browser_storage_state = str(tmp_path / "state.json")
    agent.worker_state_path = str(tmp_path / "worker.json")
    agent.provider_order = ("gemini",)
    agent.model_type = "gemini"
    agent.model = "gemini-test"
    agent.llm_api_url = None
    agent.resume_readable = "resume text"
    agent.run_id = "run-gmail-unavailable-needs-human"
    agent.calls_log = str(tmp_path / "calls.yaml")
    agent._create_browser = MagicMock(return_value=worker_browser)
    agent.select_model_type = MagicMock(return_value=MagicMock())
    agent._log_token_usage = MagicMock(return_value=0)

    with (
        patch("src.llm.apply_agent.GMAIL_APPLICATION_INTEGRATION", False),
        patch("src.llm.apply_agent.APPLY_AGENT_FALLBACK_MODEL", ""),
        patch("src.llm.apply_agent.Agent", _Agent),
        patch("src.llm.apply_agent.circuit_is_open", return_value=False),
        patch("src.llm.apply_agent.get_ready_made_resume", return_value=Path("/tmp/resume.docx")),
        patch("src.llm.apply_agent.save_evidence"),
        patch("src.llm.apply_agent.save_screenshot_b64"),
    ):
        with pytest.raises(
            RuntimeError, match="APPLICATION_NEEDS_HUMAN: Gmail readonly verification"
        ):
            await agent.apply(
                "https://careers.example.invalid/apply",
                job_title="Support role",
                company_name="Example",
                linkedin_url="https://www.linkedin.com/jobs/view/1234567894/",
            )

    worker_browser.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_submit_email_handoff_runs_read_only_confirmation_without_retry(
    tmp_path, isolate_production_event_sink
):
    """A late email handoff must not bypass the durable confirmation pass.

    This reproduces the Greenhouse case: final submit was already dispatched,
    then the model asked for email verification while Gmail was unavailable.
    The worker may inspect confirmation evidence, but it must not restart the
    model, switch provider, or click Submit again.
    """

    state_path = tmp_path / "worker.json"
    page = _Page()
    browser_session = SimpleNamespace(
        must_get_current_page=AsyncMock(return_value=page),
    )
    worker_browser = SimpleNamespace(kill=AsyncMock())
    provider_launches = []

    class _Agent:
        def __init__(self, **kwargs):
            provider_launches.append(agent.model_type)
            self.browser_session = browser_session
            self.tools = kwargs["tools"]

        async def run(self, **_kwargs):
            write_worker_state(state_path, "submit_attempted", submit_attempted=True)
            action = self.tools.registry.registry.actions["wait_for_email_verification"]
            params = action.param_model(reason="email verification requested after submit")
            await action.function(params=params, browser_session=browser_session)
            raise AssertionError("unavailable email verification should stop model control")

    agent = object.__new__(ApplyAgent)
    agent.api_key = "test-key"
    agent.primary_api_key = "test-key"
    agent.browser_storage_state = str(tmp_path / "state.json")
    agent.worker_state_path = str(state_path)
    agent.provider_order = ("gemini", "openai")
    agent.model_type = "gemini"
    agent.model = "gemini-test"
    agent.llm_api_url = None
    agent.resume_readable = "resume text"
    agent.run_id = "run-post-submit-email-handoff"
    agent.calls_log = str(tmp_path / "calls.yaml")
    agent._create_browser = MagicMock(return_value=worker_browser)
    agent.select_model_type = MagicMock(return_value=MagicMock())
    agent._log_token_usage = MagicMock(return_value=0)

    with (
        patch("src.llm.apply_agent.GMAIL_APPLICATION_INTEGRATION", False),
        patch("src.llm.apply_agent.APPLY_AGENT_FALLBACK_MODEL", ""),
        patch("src.llm.apply_agent.Agent", _Agent),
        patch("src.llm.apply_agent.circuit_is_open", return_value=False),
        patch("src.llm.apply_agent.get_ready_made_resume", return_value=Path("/tmp/resume.docx")),
        patch("src.llm.apply_agent.save_evidence") as save_evidence,
        patch("src.llm.apply_agent.save_screenshot_b64"),
    ):
        await agent.apply(
            "https://careers.example.invalid/apply",
            job_title="Support role",
            company_name="Example",
            linkedin_url="https://www.linkedin.com/jobs/view/1234567895/",
        )

    state = read_worker_state(state_path)
    assert state["phase"] == "submitted"
    assert state["verification_source"] == "page_dom"
    assert save_evidence.call_args.kwargs["result"] == "SUBMITTED"
    assert provider_launches == ["gemini"]
    agent_started = next(
        call
        for call in isolate_production_event_sink.call_args_list
        if call.args[:2] == ("agent_apply_started", "External apply agent started")
    )
    assert agent_started.kwargs["external_url"] == "https://careers.example.invalid/apply"
    assert agent_started.kwargs["application_type"] == "EXTERNAL_ATS"
    worker_browser.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_guarded_browser_actions_keep_ordinary_form_controls_usable(tmp_path):
    """The safety boundary still permits normal Next, text, and select controls."""

    class _Page:
        async def evaluate(self, _script):
            return "Personal information"

        async def get_url(self):
            return "https://careers.example.invalid/application/contact"

        async def get_title(self):
            return "Application contact"

        async def screenshot(self):
            return None

    class _Node:
        def __init__(self, label, attributes=None):
            self._label = label
            self.attributes = attributes or {"aria-label": label}

        def get_meaningful_text_for_llm(self):
            return self._label

    class _Event:
        def __await__(self):
            async def completed():
                return None

            return completed().__await__()

        async def event_result(self, **_kwargs):
            return {"success": "true"}

    page = _Page()
    event_bus = MagicMock()
    event_bus.dispatch.side_effect = [_Event(), _Event(), _Event()]
    typed_events = []

    def _type_event(**kwargs):
        event = SimpleNamespace(**kwargs)
        typed_events.append(event)
        return event

    browser_session = SimpleNamespace(
        must_get_current_page=AsyncMock(return_value=page),
        get_dom_element_by_index=AsyncMock(
            side_effect=[
                _Node("Next"),
                _Node("First Name", {"type": "text", "aria-label": "First Name"}),
                _Node("Country", {"aria-label": "Country", "role": "combobox"}),
            ]
        ),
        event_bus=event_bus,
    )
    history = _UnverifiedHistory()
    action_results = []

    class _Agent:
        def __init__(self, **kwargs):
            self.browser_session = browser_session
            self.tools = kwargs["tools"]

        async def run(self, **_kwargs):
            async def invoke(name, **values):
                action = self.tools.registry.registry.actions[name]
                return await action.function(
                    params=action.param_model(**values), browser_session=browser_session
                )

            action_results.append(await invoke("click", index=1))
            action_results.append(await invoke("input", index=2, text="Candidate"))
            action_results.append(await invoke("select_dropdown", index=3, text="United States"))
            return history

    worker_browser = SimpleNamespace(kill=AsyncMock())
    agent = object.__new__(ApplyAgent)
    agent.api_key = "test-key"
    agent.primary_api_key = "test-key"
    agent.browser_storage_state = str(tmp_path / "state.json")
    agent.worker_state_path = str(tmp_path / "worker.json")
    agent.provider_order = ("gemini",)
    agent.model_type = "gemini"
    agent.model = "gemini-test"
    agent.llm_api_url = None
    agent.resume_readable = "resume text"
    agent.run_id = "run-guarded-browser-ordinary-controls"
    agent.calls_log = str(tmp_path / "calls.yaml")
    agent._create_browser = MagicMock(return_value=worker_browser)
    agent.select_model_type = MagicMock(return_value=MagicMock())
    agent._log_token_usage = MagicMock(return_value=0)

    with (
        patch("src.llm.apply_agent.GMAIL_APPLICATION_INTEGRATION", False),
        patch("src.llm.apply_agent.APPLY_AGENT_FALLBACK_MODEL", ""),
        patch("src.llm.apply_agent.Agent", _Agent),
        patch("src.llm.apply_agent.circuit_is_open", return_value=False),
        patch("src.llm.apply_agent.get_ready_made_resume", return_value=Path("/tmp/resume.docx")),
        patch("src.llm.apply_agent.save_evidence"),
        patch("src.llm.apply_agent.save_screenshot_b64"),
        patch(
            "browser_use.browser.events.ClickElementEvent",
            side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
        ),
        patch("browser_use.browser.events.TypeTextEvent", side_effect=_type_event),
        patch(
            "browser_use.browser.events.SelectDropdownOptionEvent",
            side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
        ),
    ):
        with pytest.raises(RuntimeError, match="EXTERNAL_AGENT_FAILED"):
            await agent.apply(
                "https://careers.example.invalid/apply",
                job_title="Support role",
                company_name="Example",
                linkedin_url="https://www.linkedin.com/jobs/view/1234567894/",
            )

    assert all(result.error is None for result in action_results), [
        (result.error, result.extracted_content) for result in action_results
    ]
    assert [result.extracted_content for result in action_results] == [
        "Clicked an ordinary non-final control.",
        "Entered a protected application value.",
        "Selected an ordinary dropdown option.",
    ]
    assert event_bus.dispatch.call_count == 3
    assert typed_events[0].is_sensitive is True
    worker_browser.kill.assert_awaited_once()
