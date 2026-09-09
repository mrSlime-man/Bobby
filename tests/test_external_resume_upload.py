import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from browser_use import Tools
from browser_use.agent.service import Agent
from browser_use.agent.views import ActionResult

from src.llm.external_resume_upload import (
    choose_resume_file_input,
    register_resume_upload_action,
    score_resume_file_input,
    upload_resume_file,
)
from src.utils.runtime_control import runtime_controller


def file_input(**overrides):
    value = {
        "ordinal": 0,
        "tag": "input",
        "type": "file",
        "name": "resume",
        "id": "resume-upload",
        "aria_label": "Upload resume",
        "data_testid": "",
        "class_name": "",
        "accept": ".pdf",
        "nearby_text": "Resume / CV",
        "proxy_text": "Choose resume",
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    "candidate",
    [
        file_input(),
        file_input(class_name="visually-hidden", nearby_text="Resume"),
        file_input(aria_label="", nearby_text="", proxy_text="Attach CV"),
        file_input(id="greenhouse-resume"),
        file_input(id="lever-resume-upload"),
        file_input(id="ashby_resume_upload"),
        file_input(id="workday-resume"),
        file_input(id="icims-resume"),
        file_input(id="smartrecruiters-resume"),
        file_input(id="bamboohr-resume"),
        file_input(id="jobvite-resume"),
    ],
)
def test_resume_file_inputs_cover_visible_hidden_proxy_and_common_ats(candidate):
    assert score_resume_file_input(candidate) > 0
    assert choose_resume_file_input([candidate]) == candidate


def test_multiple_upload_fields_select_resume_not_cover_letter():
    resume = file_input(ordinal=1)
    cover = file_input(
        ordinal=0,
        name="cover_letter",
        id="cover-letter",
        aria_label="Cover letter",
        nearby_text="Cover letter",
        proxy_text="Attach cover letter",
    )
    assert choose_resume_file_input([cover, resume]) == resume


def test_wrong_non_file_element_is_never_selected():
    wrong = file_input(tag="button", type="button")
    assert score_resume_file_input(wrong) is None
    assert choose_resume_file_input([wrong]) is None


def test_missing_and_ambiguous_file_inputs_fail_closed():
    assert choose_resume_file_input([]) is None
    first = file_input(
        name="attachment", id="attachment", aria_label="", nearby_text="", proxy_text=""
    )
    second = dict(first, ordinal=1)
    assert choose_resume_file_input([first, second]) is None


def fake_browser_session(candidates, verification=None, file_info_path=None):
    runtime = SimpleNamespace(
        evaluate=AsyncMock(
            side_effect=[
                {"result": {"value": candidates}},
                {"result": {"value": True}},
                {"result": {"value": verification or {"fileCount": 1, "sectionChanged": True}}},
            ]
        )
    )
    dom = SimpleNamespace(
        enable=AsyncMock(),
        getDocument=AsyncMock(return_value={"root": {"nodeId": 1}}),
        querySelector=AsyncMock(return_value={"nodeId": 2}),
        describeNode=AsyncMock(return_value={"node": {"backendNodeId": 3}}),
        setFileInputFiles=AsyncMock(),
    )
    if file_info_path is not None:
        runtime.callFunctionOn = AsyncMock(
            return_value={"result": {"objectId": "resume-file-object"}}
        )
        dom.resolveNode = AsyncMock(
            return_value={"object": {"objectId": "resume-input-object"}}
        )
        dom.getFileInfo = AsyncMock(return_value={"path": file_info_path})
    cdp = SimpleNamespace(send=SimpleNamespace(Runtime=runtime, DOM=dom))
    cdp_session = SimpleNamespace(cdp_client=cdp, session_id="session-test")
    browser = SimpleNamespace(
        cdp_client=cdp,
        get_or_create_cdp_session=AsyncMock(return_value=cdp_session),
    )
    return browser, dom


@pytest.mark.asyncio
async def test_shutdown_before_upload_never_touches_browser(tmp_path, monkeypatch):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 synthetic")
    browser, dom = fake_browser_session([file_input()])
    monkeypatch.setattr(runtime_controller, "is_shutdown_requested", lambda: True)
    guard = register_resume_upload_action(Tools(), str(resume))

    result = await guard.upload("resume", browser)

    assert result.error.startswith("CANCELLED_BY_SHUTDOWN")
    browser.get_or_create_cdp_session.assert_not_awaited()
    dom.setFileInputFiles.assert_not_awaited()
    with pytest.raises(RuntimeError, match="CANCELLED_BY_SHUTDOWN"):
        await guard.stop_after_failure(None)


@pytest.mark.asyncio
async def test_shutdown_during_input_discovery_prevents_physical_upload(tmp_path, monkeypatch):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 synthetic")
    browser, dom = fake_browser_session([file_input()])
    shutdown = {"requested": False}
    monkeypatch.setattr(runtime_controller, "is_shutdown_requested", lambda: shutdown["requested"])

    async def described(**_kwargs):
        shutdown["requested"] = True
        return {"node": {"backendNodeId": 3}}

    dom.describeNode.side_effect = described
    guard = register_resume_upload_action(Tools(), str(resume))
    result = await guard.upload("resume", browser)

    assert result.error.startswith("CANCELLED_BY_SHUTDOWN")
    dom.setFileInputFiles.assert_not_awaited()
    with pytest.raises(RuntimeError, match="CANCELLED_BY_SHUTDOWN"):
        await guard.stop_after_failure(None)


@pytest.mark.asyncio
async def test_shutdown_after_upload_dispatch_allows_verification_without_duplicate(tmp_path, monkeypatch):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 synthetic")
    browser, dom = fake_browser_session([file_input()])
    shutdown = {"requested": False}
    monkeypatch.setattr(runtime_controller, "is_shutdown_requested", lambda: shutdown["requested"])

    async def dispatched(**_kwargs):
        shutdown["requested"] = True

    dom.setFileInputFiles.side_effect = dispatched
    guard = register_resume_upload_action(Tools(), str(resume))
    result = await guard.upload("resume", browser)

    assert result.error is None
    assert guard.succeeded
    assert await guard.upload("resume", browser) is result
    await guard.stop_after_failure(None)
    dom.setFileInputFiles.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_tools_registry_action_returns_supported_action_result(tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    browser, dom = fake_browser_session([file_input()])
    tools = Tools(exclude_actions=["upload_file"])
    register_resume_upload_action(tools, str(resume))

    result = await tools.registry.execute_action(
        "upload_resume", {"purpose": "resume"}, browser_session=browser
    )

    assert isinstance(result, ActionResult)
    assert result.error is None
    assert "verified" in (result.extracted_content or "").lower()
    dom.setFileInputFiles.assert_awaited_once()
    assert "upload_file" not in tools.registry.registry.actions


@pytest.mark.asyncio
async def test_missing_resume_input_returns_clean_technical_failure(tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    browser, dom = fake_browser_session([])

    result = await upload_resume_file(browser, str(resume))

    assert isinstance(result, ActionResult)
    assert result.error.startswith("TECHNICAL_FAILURE")
    dom.setFileInputFiles.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_verification_uses_cdp_file_metadata_when_ats_replaces_input(tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    browser, dom = fake_browser_session(
        [file_input()],
        verification={"fileCount": 0, "sectionChanged": False},
        file_info_path="/private/candidate/resume.pdf",
    )

    result = await upload_resume_file(browser, str(resume))

    assert result.error is None
    assert "verified" in (result.extracted_content or "").lower()
    dom.setFileInputFiles.assert_awaited_once()
    dom.resolveNode.assert_awaited_once_with(
        params={"backendNodeId": 3}, session_id="session-test"
    )
    dom.getFileInfo.assert_awaited_once_with(
        params={"objectId": "resume-file-object"}, session_id="session-test"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("has_input", [True, False])
async def test_registered_upload_runs_once_even_with_concurrent_retries(tmp_path, has_input):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    browser, dom = fake_browser_session([file_input()] if has_input else [])
    tools = Tools(exclude_actions=["upload_file"])
    guard = register_resume_upload_action(tools, str(resume))

    results = await asyncio.gather(
        *(
            tools.registry.execute_action(
                "upload_resume", {"purpose": "resume"}, browser_session=browser
            )
            for _ in range(3)
        )
    )

    browser.get_or_create_cdp_session.assert_awaited_once()
    assert all(bool(result.error) == (not has_input) for result in results)
    if has_input:
        assert guard.succeeded is True
        assert guard.failed is False
        dom.setFileInputFiles.assert_awaited_once()
        await guard.stop_after_failure(None)
    else:
        assert guard.succeeded is False
        assert guard.failed is True
        dom.setFileInputFiles.assert_not_awaited()
        with pytest.raises(RuntimeError, match="EXTERNAL_RESUME_UPLOAD_FAILED"):
            await guard.stop_after_failure(None)


@pytest.mark.asyncio
async def test_upload_failure_stops_browser_use_before_judge_or_next_step(tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    browser, _ = fake_browser_session([])
    guard = register_resume_upload_action(Tools(), str(resume))
    fake_agent = SimpleNamespace(
        _demo_mode_log=AsyncMock(),
        logger=SimpleNamespace(debug=lambda *args: None),
        settings=SimpleNamespace(step_timeout=1),
        step=AsyncMock(),
    )

    async def perform_step(*_):
        await guard.upload("resume", browser)

    fake_agent.step.side_effect = perform_step
    with pytest.raises(RuntimeError, match="EXTERNAL_RESUME_UPLOAD_FAILED"):
        await Agent._execute_step(fake_agent, 0, 10, None, None, guard.stop_after_failure)
    fake_agent.step.assert_awaited_once()
