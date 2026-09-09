"""The admitted destination is acquired before any model-controlled action."""

import json
import asyncio
import runpy
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.llm.apply_agent as apply_module
from src.llm.apply_agent import ApplyAgent
from src.llm.external_document import ExternalDocumentGate
from src.llm.external_worker_state import read_worker_state, write_worker_state


JOB_URL = "https://example.wd1.myworkdayjobs.com/en-US/Careers/job/Support_R123"
LINKEDIN_URL = "https://www.linkedin.com/jobs/view/1234567890/"


class _ModelReached(RuntimeError):
    """End a test at the first simulated model call, without application actions."""


class _Page:
    _target_id = "synthetic-application-target"

    def __init__(self):
        self.url = "about:blank"
        self.body = ""
        self.title = "Starting agent synthetic..."
        self.controls = []

    async def evaluate(self, script):
        if "ready_state:" in script:
            return json.dumps(
                {
                    "url": self.url,
                    "title": self.title,
                    "ready_state": "complete",
                    "body_exists": True,
                    "body_length": len(self.body),
                    "dom_element_count": 12 if self.body else 3,
                    "raw_control_count": 1 if self.body else 0,
                    "visible_control_count": 1 if self.body else 0,
                    "frame_count": 0,
                }
            )
        if "document.querySelectorAll" in script:
            return json.dumps({"controls": self.controls, "frame_count": 0})
        return self.body

    async def get_url(self):
        return self.url

    async def get_title(self):
        return self.title


@pytest.fixture
def bootstrap(tmp_path, monkeypatch):
    page = _Page()
    harness = SimpleNamespace(page=page, launches=[], model_pages=[], stay_blank=False)

    async def navigate(url, *, new_tab):
        assert new_tab is False
        if not harness.stay_blank:
            page.url = url
            page.body = "Service Desk Technician. Apply now."
            page.title = "Example Careers"

    async def tabs():
        return [SimpleNamespace(target_id=page._target_id, url=page.url)]

    session = SimpleNamespace(
        navigate_to=AsyncMock(side_effect=navigate),
        must_get_current_page=AsyncMock(return_value=page),
        get_tabs=AsyncMock(side_effect=tabs),
        get_pages=AsyncMock(return_value=[page]),
    )
    harness.session = session
    worker_browser = SimpleNamespace(kill=AsyncMock())
    agent = object.__new__(ApplyAgent)
    agent.api_key = "synthetic-test-key"
    agent.primary_api_key = "synthetic-test-key"
    agent.browser_storage_state = str(tmp_path / "state.json")
    agent.worker_state_path = str(tmp_path / "worker.json")
    agent.provider_order = ("gemini",)
    agent.model_type = "gemini"
    agent.model = "gemini-test"
    agent.llm_api_url = None
    agent.resume_readable = "Synthetic resume."
    agent.run_id = "run-synthetic-external-bootstrap"
    agent.calls_log = str(tmp_path / "calls.yaml")
    agent._create_browser = MagicMock(return_value=worker_browser)
    agent.select_model_type = MagicMock(return_value=MagicMock())
    agent._log_token_usage = MagicMock(return_value=0)
    harness.agent = agent
    harness.browser = worker_browser
    harness.save_diagnostic = MagicMock()

    class _Agent:
        def __init__(self, **kwargs):
            harness.launches.append(kwargs)
            self.browser_session = session

        async def run(self, **hooks):
            return await harness.run(self, hooks)

    def short_document_gate(job_url, **_kwargs):
        # Preserve the real gate; accelerate only its test deadline.
        return ExternalDocumentGate(
            job_url, timeout_seconds=0.03 if harness.stay_blank else 0.5,
            poll_interval_seconds=0.001,
        )

    monkeypatch.setattr(apply_module, "Agent", _Agent)
    monkeypatch.setattr(apply_module, "ExternalDocumentGate", short_document_gate)
    monkeypatch.setattr(apply_module, "GMAIL_APPLICATION_INTEGRATION", False)
    monkeypatch.setattr(apply_module, "APPLY_AGENT_FALLBACK_MODEL", "")
    monkeypatch.setattr(apply_module, "circuit_is_open", lambda *_args: False)
    monkeypatch.setattr(apply_module, "open_circuit", MagicMock())
    monkeypatch.setattr(apply_module, "load_candidate_profile", lambda *_args: {})
    monkeypatch.setattr(apply_module, "get_ready_made_resume", lambda: Path("/tmp/synthetic-resume.docx"))
    monkeypatch.setattr(apply_module, "save_landing_diagnostic", harness.save_diagnostic)
    monkeypatch.setattr(apply_module, "emit_event", MagicMock())
    monkeypatch.setattr(apply_module.runtime_controller, "is_shutdown_requested", lambda: False)
    return harness


@pytest.mark.asyncio
async def test_explicit_admitted_url_is_ready_before_model_with_multiple_prompt_urls(bootstrap):
    async def run(instance, hooks):
        assert LINKEDIN_URL in bootstrap.launches[0]["task"]
        assert JOB_URL in bootstrap.launches[0]["task"]
        for _ in range(2):
            await hooks["on_step_start"](instance)
            assert bootstrap.page.url == JOB_URL
            assert bootstrap.page.body == "Service Desk Technician. Apply now."
        bootstrap.model_pages.append(bootstrap.page.url)
        raise _ModelReached("synthetic model reached live destination")

    bootstrap.run = run
    with pytest.raises(_ModelReached):
        await bootstrap.agent.apply(JOB_URL, linkedin_url=LINKEDIN_URL)

    assert bootstrap.model_pages == [JOB_URL]
    bootstrap.session.navigate_to.assert_awaited_once_with(JOB_URL, new_tab=False)
    assert bootstrap.launches[0]["directly_open_url"] is False
    assert "navigate" not in bootstrap.launches[0]["tools"].registry.registry.actions
    bootstrap.save_diagnostic.assert_not_called()
    bootstrap.browser.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_visible_captcha_control_stops_before_the_model_can_interact(bootstrap):
    bootstrap.page.controls = [
        {
            "visible": True,
            "label": "",
            "aria_label": "",
            "title": "Change the CAPTCHA code",
        }
    ]

    async def run(instance, hooks):
        await hooks["on_step_start"](instance)
        raise AssertionError("model received a visible CAPTCHA page")

    bootstrap.run = run
    with pytest.raises(RuntimeError, match="APPLICATION_NEEDS_HUMAN"):
        await bootstrap.agent.apply(JOB_URL, linkedin_url=LINKEDIN_URL)

    assert bootstrap.model_pages == []
    bootstrap.session.navigate_to.assert_awaited_once_with(JOB_URL, new_tab=False)
    bootstrap.browser.kill.assert_awaited_once()


@pytest.mark.parametrize("attempted,expected_code,expected_phase", [
    (False, 130, "cancelled_before_submit"),
    (True, 10, "unverified_after_submit"),
])
def test_worker_readiness_shutdown_preserves_submit_aware_terminal_semantics(
    tmp_path, monkeypatch, attempted, expected_code, expected_phase
):
    state_path = tmp_path / "worker.json"
    write_worker_state(state_path, "pre_submit", submit_attempted=attempted)

    def stopped_main(coroutine):
        # Exercise only the worker's real terminal exception routing.
        coroutine.close()
        raise RuntimeError("CANCELLED_BY_SHUTDOWN: document readiness stopped")

    monkeypatch.setattr(asyncio, "run", stopped_main)
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    monkeypatch.setattr(sys, "argv", ["worker", JOB_URL, "", "", LINKEDIN_URL, str(state_path)])
    monkeypatch.setenv("BOBBY_EXTERNAL_STATE_FILE", str(state_path))
    monkeypatch.setenv("BROWSER_USE_LOGGING_LEVEL", "result")
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    with pytest.raises(SystemExit) as error:
        runpy.run_module("src.llm.external_apply_worker", run_name="__main__")

    assert error.value.code == expected_code
    state = read_worker_state(state_path)
    assert state["phase"] == expected_phase
    assert state["submit_attempted"] is attempted


@pytest.mark.asyncio
async def test_provider_fallback_keeps_live_form_and_never_repeats_bootstrap(bootstrap):
    form_url = "https://example.wd1.myworkdayjobs.com/en-US/Careers/application/personal"
    bootstrap.agent.provider_order = ("gemini", "openai")

    async def run(instance, hooks):
        await hooks["on_step_start"](instance)
        bootstrap.model_pages.append(bootstrap.page.url)
        if len(bootstrap.launches) == 1:
            bootstrap.page.url = form_url
            bootstrap.page.body = "Personal information. Continue."
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        assert bootstrap.page.body == "Personal information. Continue."
        raise _ModelReached("synthetic fallback reached preserved form")

    bootstrap.run = run
    with pytest.raises(_ModelReached):
        await bootstrap.agent.apply(JOB_URL, linkedin_url=LINKEDIN_URL)

    assert bootstrap.model_pages == [JOB_URL, form_url]
    assert len(bootstrap.launches) == 2
    assert bootstrap.launches[0]["browser"] is bootstrap.launches[1]["browser"]
    assert bootstrap.launches[0]["tools"] is bootstrap.launches[1]["tools"]
    assert all(launch["directly_open_url"] is False for launch in bootstrap.launches)
    assert all("navigate" not in launch["tools"].registry.registry.actions for launch in bootstrap.launches)
    bootstrap.session.navigate_to.assert_awaited_once_with(JOB_URL, new_tab=False)
    bootstrap.save_diagnostic.assert_not_called()
    bootstrap.browser.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_permanently_empty_startup_stops_before_model_and_captures_once(bootstrap):
    bootstrap.stay_blank = True

    async def run(instance, hooks):
        await hooks["on_step_start"](instance)
        bootstrap.model_pages.append(bootstrap.page.url)
        raise AssertionError("model received an empty startup document")

    bootstrap.run = run
    with pytest.raises(RuntimeError, match="EXTERNAL_EMPTY_DOCUMENT"):
        await bootstrap.agent.apply(JOB_URL, linkedin_url=LINKEDIN_URL)

    assert bootstrap.model_pages == []
    assert len(bootstrap.launches) == 1
    bootstrap.session.navigate_to.assert_awaited_once_with(JOB_URL, new_tab=False)
    bootstrap.save_diagnostic.assert_called_once()
    captured = bootstrap.save_diagnostic.call_args.kwargs
    assert captured["reason"] == "empty_document"
    assert captured["url"] == "about:blank"
    assert captured["body"] == ""
    assert read_worker_state(bootstrap.agent.worker_state_path)["submit_attempted"] is False
    bootstrap.browser.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_durable_submit_marker_forbids_initial_navigation(bootstrap):
    async def run(instance, hooks):
        write_worker_state(
            bootstrap.agent.worker_state_path, "submit_attempted", submit_attempted=True
        )
        await hooks["on_step_start"](instance)
        raise _ModelReached("synthetic existing post-submit session preserved")

    bootstrap.run = run
    with pytest.raises(_ModelReached):
        await bootstrap.agent.apply(JOB_URL, linkedin_url=LINKEDIN_URL)

    bootstrap.session.navigate_to.assert_not_awaited()
    bootstrap.save_diagnostic.assert_not_called()
    state = read_worker_state(bootstrap.agent.worker_state_path)
    assert state["submit_attempted"] is True
    assert state["phase"] == "submit_attempted"
    bootstrap.browser.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_before_first_step_forbids_navigation_and_model(bootstrap, monkeypatch):
    async def run(instance, hooks):
        monkeypatch.setattr(apply_module.runtime_controller, "is_shutdown_requested", lambda: True)
        await hooks["on_step_start"](instance)
        bootstrap.model_pages.append(bootstrap.page.url)
        raise AssertionError("model started after shutdown")

    bootstrap.run = run
    with pytest.raises(RuntimeError, match="CANCELLED_BY_SHUTDOWN"):
        await bootstrap.agent.apply(JOB_URL, linkedin_url=LINKEDIN_URL)

    assert bootstrap.model_pages == []
    bootstrap.session.navigate_to.assert_not_awaited()
    bootstrap.save_diagnostic.assert_not_called()
    assert read_worker_state(bootstrap.agent.worker_state_path)["submit_attempted"] is False
    bootstrap.browser.kill.assert_awaited_once()
