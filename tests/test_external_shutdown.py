"""Shutdown closes external dispatch boundaries even when inspection is in flight."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import src.llm.apply_agent as apply_module
import src.llm.external_account_registration as registration_module
from src.llm.ats_engine import ATSStage
from src.llm.external_account_registration import RegistrationFacts, RegistrationResult
from src.llm.external_worker_state import read_worker_state, write_worker_state

# Reuse the existing synthetic browser/application fixtures; no real browser,
# candidate source, account store, provider, or employer is involved.
from test_external_account_registration import _Page, successfactors_controls
from test_external_bootstrap import JOB_URL, LINKEDIN_URL, _ModelReached, bootstrap  # noqa: F401


@pytest.fixture
def registration_shutdown(monkeypatch):
    state = SimpleNamespace(shutdown=False)
    monkeypatch.setattr(
        apply_module.runtime_controller, "is_shutdown_requested", lambda: state.shutdown
    )
    state.page = _Page(successfactors_controls())
    state.session = SimpleNamespace(
        must_get_current_page=AsyncMock(return_value=state.page)
    )
    state.claim = SimpleNamespace(password="synthetic-password")
    state.claim_account = Mock(return_value=state.claim)
    state.mark_submit = Mock(return_value=True)
    state.release_claim = Mock()
    monkeypatch.setattr(registration_module, "claim_ats_account_registration", state.claim_account)
    monkeypatch.setattr(registration_module, "mark_ats_registration_submit_started", state.mark_submit)
    monkeypatch.setattr(registration_module, "release_ats_registration_claim", state.release_claim)
    monkeypatch.setattr(registration_module, "mark_ats_registration_created", Mock(return_value=True))
    monkeypatch.setattr(registration_module.asyncio, "sleep", AsyncMock())
    return state


async def _complete_registration(state):
    return await registration_module.complete_account_registration(
        state.session,
        facts=RegistrationFacts(email="candidate@example.invalid", country="United States"),
    )


@pytest.mark.asyncio
async def test_registration_shutdown_at_entry_cannot_claim_or_touch_the_form(registration_shutdown):
    state = registration_shutdown
    state.shutdown = True

    result = await _complete_registration(state)

    assert result.status == "CANCELLED_BY_SHUTDOWN"
    state.session.must_get_current_page.assert_not_awaited()
    state.claim_account.assert_not_called()
    state.mark_submit.assert_not_called()
    state.release_claim.assert_not_called()
    state.page.element.click.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown_during", ["field_fill", "submit_button_acquisition"])
async def test_registration_shutdown_during_inspection_releases_claim_without_submit(
    registration_shutdown, shutdown_during
):
    state = registration_shutdown

    async def fill(_value):
        if shutdown_during == "field_fill":
            state.shutdown = True

    async def elements(selector):
        if shutdown_during == "submit_button_acquisition" and "registration-submit" in selector:
            state.shutdown = True
        return [state.page.element]

    state.page.element.fill.side_effect = fill
    state.page.get_elements_by_css_selector.side_effect = elements

    result = await _complete_registration(state)

    assert state.shutdown is True
    assert result.status == "CANCELLED_BY_SHUTDOWN"
    state.claim_account.assert_called_once()
    state.mark_submit.assert_not_called()
    state.release_claim.assert_called_once_with(state.claim)
    state.page.element.click.assert_not_awaited()


async def _invoke_action(bootstrap, name, **values):
    action = bootstrap.launches[-1]["tools"].registry.registry.actions[name]
    return await action.function(
        params=action.param_model(**values), browser_session=bootstrap.session
    )


@pytest.mark.asyncio
async def test_registration_cancellation_propagates_out_of_the_application_action(
    bootstrap, monkeypatch
):
    monkeypatch.setattr(apply_module, "EXTERNAL_ATS_AUTO_ACCOUNT_CREATION", True)
    monkeypatch.setattr(apply_module, "registration_page_present", AsyncMock(return_value=True))
    complete = AsyncMock(return_value=RegistrationResult("CANCELLED_BY_SHUTDOWN"))
    monkeypatch.setattr(apply_module.AccountRegistrationGuard, "complete", complete)

    async def run(_instance, _hooks):
        await _invoke_action(bootstrap, "complete_account_registration", reason="ordinary form")
        raise AssertionError("registration shutdown was converted into an ordinary form failure")

    bootstrap.run = run
    with pytest.raises(RuntimeError, match="CANCELLED_BY_SHUTDOWN"):
        await bootstrap.agent.apply(JOB_URL, linkedin_url=LINKEDIN_URL)

    complete.assert_awaited_once()
    assert read_worker_state(bootstrap.agent.worker_state_path)["submit_attempted"] is False
    bootstrap.browser.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_final_submit_shutdown_during_node_acquisition_has_no_durable_marker_or_click(
    bootstrap, monkeypatch
):
    state = SimpleNamespace(shutdown=False)
    monkeypatch.setattr(
        apply_module.runtime_controller, "is_shutdown_requested", lambda: state.shutdown
    )
    bootstrap.page.body = "Review your application"

    async def acquire_node(_index):
        # Shutdown arrives after page safety inspection but before dispatch.
        state.shutdown = True
        return SimpleNamespace()

    bootstrap.session.get_dom_element_by_index = AsyncMock(side_effect=acquire_node)
    bootstrap.session.event_bus = SimpleNamespace(dispatch=Mock())
    writes = Mock(wraps=apply_module.write_worker_state)
    monkeypatch.setattr(apply_module, "write_worker_state", writes)

    async def run(_instance, _hooks):
        await _invoke_action(bootstrap, "submit_final_application", index=4)
        raise AssertionError("final submit continued after shutdown")

    bootstrap.run = run
    with pytest.raises(RuntimeError, match="CANCELLED_BY_SHUTDOWN"):
        await bootstrap.agent.apply(JOB_URL, linkedin_url=LINKEDIN_URL)

    bootstrap.session.get_dom_element_by_index.assert_awaited_once_with(4)
    bootstrap.session.event_bus.dispatch.assert_not_called()
    phases = [call.args[1] for call in writes.call_args_list]
    assert "submit_click_started" not in phases
    assert "submit_attempted" not in phases
    assert read_worker_state(bootstrap.agent.worker_state_path)["phase"] == "pre_submit"
    bootstrap.browser.kill.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [stage for stage in ATSStage if stage is not ATSStage.CONFIRMATION])
async def test_shutdown_blocks_next_model_step_in_every_pre_submit_stage(
    bootstrap, monkeypatch, stage
):
    make_state = apply_module.ATSRunState
    states = []

    def capture_state(**kwargs):
        state = make_state(**kwargs)
        states.append(state)
        return state

    monkeypatch.setattr(apply_module, "ATSRunState", capture_state)

    async def run(instance, hooks):
        states[0].stage = stage
        monkeypatch.setattr(apply_module.runtime_controller, "is_shutdown_requested", lambda: True)
        await hooks["on_step_start"](instance)
        raise AssertionError(f"model work continued after shutdown in {stage.value}")

    bootstrap.run = run
    with pytest.raises(RuntimeError, match="CANCELLED_BY_SHUTDOWN"):
        await bootstrap.agent.apply(JOB_URL, linkedin_url=LINKEDIN_URL)

    bootstrap.session.navigate_to.assert_not_awaited()
    assert read_worker_state(bootstrap.agent.worker_state_path)["submit_attempted"] is False


@pytest.mark.asyncio
async def test_shutdown_does_not_block_existing_post_submit_confirmation(bootstrap, monkeypatch):
    async def run(instance, hooks):
        write_worker_state(
            bootstrap.agent.worker_state_path, "submit_attempted", submit_attempted=True
        )
        monkeypatch.setattr(apply_module.runtime_controller, "is_shutdown_requested", lambda: True)
        await hooks["on_step_start"](instance)
        raise _ModelReached("existing confirmation remains available during drain")

    bootstrap.run = run
    with pytest.raises(_ModelReached):
        await bootstrap.agent.apply(JOB_URL, linkedin_url=LINKEDIN_URL)

    bootstrap.session.navigate_to.assert_not_awaited()
    assert read_worker_state(bootstrap.agent.worker_state_path)["submit_attempted"] is True


@pytest.mark.asyncio
async def test_in_flight_model_step_stops_before_post_step_browser_processing(
    bootstrap, monkeypatch
):
    async def run(instance, hooks):
        monkeypatch.setattr(apply_module.runtime_controller, "is_shutdown_requested", lambda: True)
        await hooks["on_step_end"](instance)
        raise AssertionError("post-step processing continued after pre-submit shutdown")

    bootstrap.run = run
    with pytest.raises(RuntimeError, match="CANCELLED_BY_SHUTDOWN"):
        await bootstrap.agent.apply(JOB_URL, linkedin_url=LINKEDIN_URL)

    bootstrap.session.must_get_current_page.assert_not_awaited()
    bootstrap.session.navigate_to.assert_not_awaited()
    assert read_worker_state(bootstrap.agent.worker_state_path)["submit_attempted"] is False
