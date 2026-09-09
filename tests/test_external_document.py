"""External startup must reach the admitted document before any model action."""

import asyncio
import json
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from browser_use import Agent, Tools
from browser_use.actor.page import Page

from src.llm.external_document import (
    ExternalDocumentGate,
    ExternalEmptyDocument,
    ExternalInitialNavigationFailed,
)


JOB_URL = "https://careers.example.invalid/jobs/100"
LINKEDIN_URL = "https://www.linkedin.com/jobs/view/100/"


def _snapshot(**changes):
    observation = {
        "url": JOB_URL,
        "title": "Example careers",
        "ready_state": "complete",
        "body_exists": True,
        "body_length": 64,
        "dom_element_count": 8,
        "raw_control_count": 1,
        "visible_control_count": 1,
        "frame_count": 0,
        "visible_frame_count": 0,
    }
    observation.update(changes)
    return observation


def _empty_snapshot(**changes):
    return _snapshot(
        **{
            "body_length": 0,
            "raw_control_count": 0,
            "visible_control_count": 0,
            **changes,
        }
    )


class _SnapshotPage:
    """Match actor Page.evaluate's string result and repeat the final state."""

    def __init__(self, *observations, target_id="application-tab"):
        self._observations = deque(observations)
        self._target_id = target_id
        self.evaluate_count = 0

    async def evaluate(self, _script):
        self.evaluate_count += 1
        observation = self._observations[0]
        if len(self._observations) > 1:
            self._observations.popleft()
        if isinstance(observation, Exception):
            raise observation
        return observation if isinstance(observation, str) else json.dumps(observation)


class _Browser:
    def __init__(self, *pages):
        self._pages = deque(pages)
        self.navigate_to = AsyncMock()
        self.must_get_current_page = AsyncMock(side_effect=self._current_page)
        self.get_tabs = AsyncMock(return_value=[])
        # None of these actions is needed to observe startup readiness.
        self.new_page = AsyncMock(side_effect=AssertionError("unexpected new tab"))
        self.reload = AsyncMock(side_effect=AssertionError("unexpected reload"))
        self.switch_to_tab = AsyncMock(side_effect=AssertionError("unexpected tab adoption"))

    async def _current_page(self):
        page = self._pages[0]
        if len(self._pages) > 1:
            self._pages.popleft()
        return page


def _gate(timeout=0.5):
    return ExternalDocumentGate(
        JOB_URL,
        timeout_seconds=timeout,
        poll_interval_seconds=0.001,
    )


async def _prepare(gate, browser, *, shutdown=False, irreversible=False):
    return await asyncio.wait_for(
        gate.prepare(
            browser,
            shutdown_requested=shutdown if callable(shutdown) else lambda: shutdown,
            irreversible_activity=(
                irreversible if callable(irreversible) else lambda: irreversible
            ),
        ),
        timeout=2.0,
    )


def test_installed_browser_use_cannot_infer_the_employer_url_from_job_context():
    """0.12.6 URL inference silently skips Bobby's employer + LinkedIn task."""

    probe = SimpleNamespace(logger=Mock())
    task = f"Apply to the job at: {JOB_URL}\nLinkedIn URL: {LINKEDIN_URL}"
    assert Agent._extract_start_url(probe, task) is None
    assert Agent._extract_start_url(probe, f"Apply to the job at: {JOB_URL}") == JOB_URL


def test_installed_browser_use_initial_navigation_requires_the_excluded_action():
    """Removing contextual URLs alone cannot repair guarded-tools startup."""

    tools = Tools(exclude_actions=["navigate"])
    probe = SimpleNamespace(ActionModel=tools.registry.create_action_model(), tools=tools)
    assert "navigate" not in tools.registry.registry.actions
    with pytest.raises(KeyError, match="navigate"):
        Agent._convert_initial_actions(
            probe,
            [{"navigate": {"url": JOB_URL, "new_tab": False}}],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_json", [False, True])
async def test_installed_actor_page_evaluate_returns_a_single_decodable_snapshot(explicit_json):
    """Actor Page returns strings unchanged and serializes object results."""

    observation = _snapshot()
    value = json.dumps(observation) if explicit_json else observation
    evaluate = AsyncMock(return_value={"result": {"value": value}})
    browser = SimpleNamespace(
        cdp_client=SimpleNamespace(
            send=SimpleNamespace(Runtime=SimpleNamespace(evaluate=evaluate))
        )
    )
    page = Page(browser, "fixture-target", session_id="fixture-session")

    payload = await page.evaluate("() => ({url: location.href})")

    assert isinstance(payload, str)
    assert json.loads(payload) == observation
    assert evaluate.call_args.kwargs["session_id"] == "fixture-session"


@pytest.mark.asyncio
async def test_empty_spa_can_render_before_model_work_without_reloading():
    page = _SnapshotPage(
        _empty_snapshot(ready_state="loading"),
        _empty_snapshot(ready_state="interactive"),
        _snapshot(),
    )
    browser = _Browser(page)

    observed = await _prepare(_gate(), browser)

    assert observed["meaningful"] is True
    assert page.evaluate_count == 3
    browser.navigate_to.assert_awaited_once_with(JOB_URL, new_tab=False)
    browser.reload.assert_not_awaited()
    browser.new_page.assert_not_awaited()
    browser.switch_to_tab.assert_not_awaited()


@pytest.mark.asyncio
async def test_readiness_reacquires_the_current_target_after_focus_changes():
    initial_page = _SnapshotPage(_empty_snapshot(), target_id="initial-tab")
    replacement_page = _SnapshotPage(
        _snapshot(url="https://ats.example.invalid/application/100"),
        target_id="application-tab",
    )
    browser = _Browser(initial_page, replacement_page)

    observed = await _prepare(_gate(), browser)

    assert observed["meaningful"] is True
    assert observed["url"] == "https://ats.example.invalid/application/100"
    assert initial_page.evaluate_count == 1
    assert replacement_page.evaluate_count == 1
    assert browser.must_get_current_page.await_count == 2
    browser.navigate_to.assert_awaited_once_with(JOB_URL, new_tab=False)
    browser.switch_to_tab.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("transient", [RuntimeError("execution context replaced"), "not json"])
async def test_transient_extraction_failure_can_recover_on_the_current_document(transient):
    page = _SnapshotPage(transient, _snapshot())
    browser = _Browser(page)

    observed = await _prepare(_gate(), browser)

    assert observed["meaningful"] is True
    assert page.evaluate_count == 2
    browser.navigate_to.assert_awaited_once_with(JOB_URL, new_tab=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "observation",
    [
        _empty_snapshot(),
        _empty_snapshot(raw_control_count=3),
        _empty_snapshot(frame_count=1, visible_frame_count=0),
        _snapshot(ready_state="loading"),
        _snapshot(body_exists=False),
        "not json",
        "[]",
        RuntimeError("execution context unavailable"),
    ],
)
async def test_permanently_empty_or_unreadable_document_stops_within_the_budget(observation):
    browser = _Browser(_SnapshotPage(observation))

    with pytest.raises(ExternalEmptyDocument, match="EXTERNAL_EMPTY_DOCUMENT"):
        await _prepare(_gate(timeout=0.015), browser)

    browser.navigate_to.assert_awaited_once_with(JOB_URL, new_tab=False)
    browser.reload.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["about:blank", "chrome-error://chromewebdata/", "data:text/html,ready"])
async def test_non_web_startup_or_error_documents_never_count_as_live_content(url):
    browser = _Browser(_SnapshotPage(_snapshot(url=url, title="Starting agent fixture...")))

    with pytest.raises(ExternalEmptyDocument, match="EXTERNAL_EMPTY_DOCUMENT"):
        await _prepare(_gate(timeout=0.015), browser)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"body_length": 1, "visible_control_count": 0, "frame_count": 0},
        {"body_length": 0, "visible_control_count": 1, "frame_count": 0},
        {"body_length": 0, "visible_control_count": 0, "frame_count": 1, "visible_frame_count": 1},
    ],
)
async def test_live_text_control_or_frame_is_sufficient_without_speculative_navigation(changes):
    browser = _Browser(_SnapshotPage(_empty_snapshot(**changes)))

    observed = await _prepare(_gate(), browser)

    assert observed["meaningful"] is True
    browser.navigate_to.assert_awaited_once_with(JOB_URL, new_tab=False)
    browser.new_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_before_startup_blocks_navigation_and_observation():
    browser = _Browser(_SnapshotPage(_snapshot()))

    with pytest.raises(RuntimeError, match="CANCELLED_BY_SHUTDOWN"):
        await _prepare(_gate(), browser, shutdown=True)

    browser.navigate_to.assert_not_awaited()
    browser.must_get_current_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_irreversible_activity_blocks_startup_and_readiness_actions():
    browser = _Browser(_SnapshotPage(_snapshot()))

    assert await _prepare(_gate(), browser, irreversible=True) is None

    browser.navigate_to.assert_not_awaited()
    browser.must_get_current_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_during_empty_document_wait_stops_without_another_navigation():
    page = _SnapshotPage(_empty_snapshot())
    browser = _Browser(page)

    with pytest.raises(RuntimeError, match="CANCELLED_BY_SHUTDOWN"):
        await _prepare(_gate(), browser, shutdown=lambda: page.evaluate_count > 0)

    browser.navigate_to.assert_awaited_once_with(JOB_URL, new_tab=False)
    assert page.evaluate_count == 1


@pytest.mark.asyncio
async def test_irreversible_activity_during_readiness_stops_further_observation():
    page = _SnapshotPage(_empty_snapshot())
    browser = _Browser(page)

    observed = await _prepare(
        _gate(), browser, irreversible=lambda: page.evaluate_count > 0
    )

    assert observed is None
    browser.navigate_to.assert_awaited_once_with(JOB_URL, new_tab=False)
    assert page.evaluate_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["navigate_to", "must_get_current_page"])
async def test_startup_deadline_also_bounds_hanging_browser_operations(operation):
    browser = _Browser(_SnapshotPage(_snapshot()))

    async def never_finishes(*_args, **_kwargs):
        await asyncio.Event().wait()

    getattr(browser, operation).side_effect = never_finishes

    with pytest.raises(
        RuntimeError,
        match="EXTERNAL_(?:EMPTY_DOCUMENT|INITIAL_NAVIGATION_FAILED)",
    ):
        await _prepare(_gate(timeout=0.015), browser)

    browser.navigate_to.assert_awaited_once_with(JOB_URL, new_tab=False)


@pytest.mark.asyncio
async def test_provider_handoff_observes_the_existing_page_without_reopening_the_job():
    page = _SnapshotPage(_snapshot())
    browser = _Browser(page)
    gate = _gate()

    assert (await _prepare(gate, browser))["meaningful"] is True
    assert (await _prepare(gate, browser))["meaningful"] is True

    browser.navigate_to.assert_awaited_once_with(JOB_URL, new_tab=False)
    assert page.evaluate_count == 2


@pytest.mark.asyncio
async def test_initial_navigation_failure_is_technical_and_never_dispatched_twice():
    browser = _Browser(_SnapshotPage(_snapshot()))
    browser.navigate_to.side_effect = RuntimeError("navigation failed")
    gate = _gate()

    with pytest.raises(ExternalInitialNavigationFailed, match="EXTERNAL_INITIAL_NAVIGATION_FAILED"):
        await _prepare(gate, browser)

    # A second provider must never restart an uncertain navigation. Remembering
    # the terminal error or safely observing an existing page are both valid.
    try:
        await _prepare(gate, browser)
    except (ExternalInitialNavigationFailed, ExternalEmptyDocument):
        pass

    browser.navigate_to.assert_awaited_once_with(JOB_URL, new_tab=False)


@pytest.mark.asyncio
async def test_empty_document_failure_never_reopens_the_job_on_a_later_call():
    browser = _Browser(_SnapshotPage(_empty_snapshot()))
    gate = _gate(timeout=0.015)

    for _ in range(2):
        with pytest.raises(ExternalEmptyDocument, match="EXTERNAL_EMPTY_DOCUMENT"):
            await _prepare(gate, browser)

    browser.navigate_to.assert_awaited_once_with(JOB_URL, new_tab=False)
