"""Strict resume-file targeting for Browser Use 0.12.6 external ATS flows."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from browser_use.agent.views import ActionResult

from config.logger_config import logger
from src.utils.runtime_control import runtime_controller


_RESUME_TERMS = re.compile(r"\b(resume|résumé|cv|curriculum vitae)\b", re.I)
_OTHER_UPLOAD_TERMS = re.compile(
    r"\b(cover letter|portfolio|work sample|writing sample|photo|avatar|attachment|transcript)\b",
    re.I,
)
_SUPPORTED_ATS_TERMS = (
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "icims",
    "smartrecruiters",
    "bamboohr",
    "jobvite",
)


def score_resume_file_input(candidate: Mapping[str, Any]) -> int | None:
    """Score only genuine ``input[type=file]`` metadata for resume use."""
    if str(candidate.get("tag") or "").casefold() != "input":
        return None
    if str(candidate.get("type") or "").casefold() != "file":
        return None
    own = " ".join(
        str(candidate.get(key) or "")
        for key in ("name", "id", "aria_label", "data_testid", "class_name", "accept")
    )
    nearby = str(candidate.get("nearby_text") or "")
    proxy = str(candidate.get("proxy_text") or "")
    semantic = f"{own} {nearby} {proxy}"
    if _OTHER_UPLOAD_TERMS.search(own) or (
        _OTHER_UPLOAD_TERMS.search(nearby) and not _RESUME_TERMS.search(semantic)
    ):
        return -100
    score = 0
    if _RESUME_TERMS.search(own):
        score += 100
    if _RESUME_TERMS.search(nearby):
        score += 50
    if _RESUME_TERMS.search(proxy):
        score += 40
    if any(ats in semantic.casefold() for ats in _SUPPORTED_ATS_TERMS):
        score += 5
    return score


def choose_resume_file_input(candidates: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    valid = [(score_resume_file_input(item), item) for item in candidates]
    valid = [(score, item) for score, item in valid if score is not None and score > -100]
    if not valid:
        return None
    valid.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best = valid[0]
    if best_score > 0:
        return best
    # A single neutral file field is safe; with multiple ambiguous fields, fail closed.
    return best if len(valid) == 1 else None


def _runtime_value(response: Mapping[str, Any]) -> Any:
    return (response.get("result") or {}).get("value")


async def _cdp_file_info_confirms_upload(
    cdp: Any, session_id: str, backend_node_id: int
) -> bool:
    """Verify a file attached to the resolved input without exposing its path.

    Some ATS pages replace or hide the file input immediately after
    ``DOM.setFileInputFiles``.  In that case the marker-based DOM query can no
    longer see the input even though Chromium still owns the selected file.
    Resolve the already-dispatched input node, obtain its ``File`` wrapper,
    and ask CDP for metadata.  This is verification only; it never dispatches
    another upload and the returned path is deliberately not logged.
    """

    dom = getattr(getattr(cdp, "send", None), "DOM", None)
    runtime = getattr(getattr(cdp, "send", None), "Runtime", None)
    resolve_node = getattr(dom, "resolveNode", None)
    get_file_info = getattr(dom, "getFileInfo", None)
    call_function_on = getattr(runtime, "callFunctionOn", None)
    if not all(callable(method) for method in (resolve_node, get_file_info, call_function_on)):
        return False

    resolved = await resolve_node(
        params={"backendNodeId": backend_node_id},
        session_id=session_id,
    )
    input_object_id = (resolved.get("object") or {}).get("objectId")
    if not input_object_id:
        return False

    file_wrapper = await call_function_on(
        params={
            "objectId": input_object_id,
            "functionDeclaration": (
                "function () { return this.files && this.files.length ? this.files[0] : null; }"
            ),
            "returnByValue": False,
        },
        session_id=session_id,
    )
    file_object_id = ((file_wrapper.get("result") or {}).get("objectId"))
    if not file_object_id:
        return False

    file_info = await get_file_info(
        params={"objectId": file_object_id},
        session_id=session_id,
    )
    # The path is only used as a boolean signal. Never return or log it.
    return bool(str(file_info.get("path") or "").strip())


async def upload_resume_file(browser_session: Any, resume_path: str) -> ActionResult:
    """Find, upload, and verify the real resume input using Browser Use CDP APIs."""
    if runtime_controller.is_shutdown_requested():
        return ActionResult(error="CANCELLED_BY_SHUTDOWN: resume upload not started")
    path = Path(resume_path)
    if not path.is_file() or path.stat().st_size <= 0:
        return ActionResult(error="TECHNICAL_FAILURE: configured resume file is unavailable")

    marker = "data-bobby-resume-upload"
    try:
        cdp_session = await browser_session.get_or_create_cdp_session()
        cdp = cdp_session.cdp_client
        session_id = cdp_session.session_id
        discovery = await cdp.send.Runtime.evaluate(
            params={
                "expression": """(() => {
                    const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
                    return [...document.querySelectorAll('input[type="file"]')].map((input, ordinal) => {
                        const labels = input.labels ? [...input.labels].map(label => label.innerText || label.textContent).join(' ') : '';
                        const labelledBy = clean(input.getAttribute('aria-labelledby')).split(/\\s+/)
                            .filter(Boolean).map(id => document.getElementById(id)?.innerText || '').join(' ');
                        const field = input.closest(
                            '[data-field], .field, .application-field, .form-group, .jobs-easy-apply-form-section__grouping, section'
                        );
                        const proxy = input.id ? document.querySelector(`label[for="${CSS.escape(input.id)}"]`) : null;
                        return {
                            ordinal,
                            tag: input.tagName.toLowerCase(),
                            type: (input.type || '').toLowerCase(),
                            name: clean(input.name),
                            id: clean(input.id),
                            aria_label: clean(input.getAttribute('aria-label')),
                            data_testid: clean(input.getAttribute('data-testid')),
                            class_name: clean(input.className),
                            accept: clean(input.accept),
                            nearby_text: clean(labels + ' ' + labelledBy + ' ' + (field?.innerText || '')).slice(0, 500),
                            proxy_text: clean(proxy?.innerText || proxy?.textContent || '')
                        };
                    });
                })()""",
                "returnByValue": True,
            },
            session_id=session_id,
        )
        candidates = _runtime_value(discovery) or []
        selected = choose_resume_file_input(candidates)
        if selected is None:
            logger.warning("Resume upload stopped: no unambiguous resume file input found")
            return ActionResult(
                error="TECHNICAL_FAILURE: no unambiguous input[type=file] for the resume was found"
            )

        ordinal = int(selected["ordinal"])
        await cdp.send.Runtime.evaluate(
            params={
                "expression": (
                    "(() => { const inputs = [...document.querySelectorAll('input[type=\\\"file\\\"]')]; "
                    f"const input = inputs[{ordinal}]; if (!input) return false; "
                    f"input.setAttribute('{marker}', 'selected'); return true; }})()"
                ),
                "returnByValue": True,
            },
            session_id=session_id,
        )
        await cdp.send.DOM.enable(session_id=session_id)
        document = await cdp.send.DOM.getDocument(
            params={"depth": 0, "pierce": True}, session_id=session_id
        )
        root_id = (document.get("root") or {}).get("nodeId")
        matched = await cdp.send.DOM.querySelector(
            params={"nodeId": root_id, "selector": f'input[type="file"][{marker}="selected"]'},
            session_id=session_id,
        )
        node_id = matched.get("nodeId")
        if not node_id:
            return ActionResult(error="TECHNICAL_FAILURE: selected resume input became unavailable")
        described = await cdp.send.DOM.describeNode(
            params={"nodeId": node_id}, session_id=session_id
        )
        backend_node_id = (described.get("node") or {}).get("backendNodeId")
        if not backend_node_id:
            return ActionResult(error="TECHNICAL_FAILURE: resume input could not be resolved")
        # Discovery can await CDP while SIGINT starts draining. Check again at
        # the physical file boundary, then allow attachment verification to
        # finish if this dispatch was already admitted before shutdown.
        if not runtime_controller.try_start_irreversible_dispatch("resume_upload"):
            return ActionResult(error="CANCELLED_BY_SHUTDOWN: resume upload not dispatched")
        logger.info("EXTERNAL_IRREVERSIBLE_DISPATCH | action=resume_upload")
        await cdp.send.DOM.setFileInputFiles(
            params={"files": [os.fspath(path.resolve())], "backendNodeId": backend_node_id},
            session_id=session_id,
        )
        verification = await cdp.send.Runtime.evaluate(
            params={
                "expression": (
                    f"(() => {{ const input = document.querySelector('[{marker}=\\\"selected\\\"]'); "
                    "if (!input) return {fileCount: 0, sectionChanged: false}; "
                    "input.dispatchEvent(new Event('input', {bubbles: true})); "
                    "input.dispatchEvent(new Event('change', {bubbles: true})); "
                    "const field = input.closest('[data-field], .field, .application-field, .form-group, section'); "
                    "const filename = input.files?.[0]?.name || ''; "
                    "return {fileCount: input.files?.length || 0, sectionChanged: !!filename && "
                    "((field?.innerText || '').includes(filename) || !!input.value)}; })()"
                ),
                "returnByValue": True,
            },
            session_id=session_id,
        )
        state = _runtime_value(verification) or {}
        if int(state.get("fileCount") or 0) < 1:
            if not await _cdp_file_info_confirms_upload(cdp, session_id, backend_node_id):
                return ActionResult(error="TECHNICAL_FAILURE: resume upload could not be verified")
            logger.info("Resume uploaded and verified via CDP file metadata")
        else:
            logger.info("Resume uploaded to verified resume input")
        return ActionResult(
            extracted_content="Resume uploaded and verified.",
            long_term_memory="Resume is attached in the resume/CV field.",
        )
    except Exception as exc:
        logger.warning("Resume upload failed safely: %s", type(exc).__name__)
        return ActionResult(error=f"TECHNICAL_FAILURE: resume upload failed ({type(exc).__name__})")


class ResumeUploadGuard:
    """One physical upload per worker, with deterministic termination on failure."""

    def __init__(self, resume_path: str, enabled: bool = True) -> None:
        self.resume_path = resume_path
        self.enabled = bool(enabled)
        self._lock = asyncio.Lock()
        self._attempted = False
        self._result: ActionResult | None = None

    @property
    def attempted(self) -> bool:
        """Whether this worker has already made its single physical attempt."""
        return self._attempted

    @property
    def succeeded(self) -> bool:
        """Whether the one physical upload was independently verified."""
        return bool(self._attempted and self._result is not None and not self._result.error)

    @property
    def failed(self) -> bool:
        """Whether the upload boundary ended in a safe terminal failure."""
        return bool(self._attempted and (self._result is None or self._result.error))

    async def upload(self, purpose: str, browser_session: Any) -> ActionResult:
        async with self._lock:
            if self._attempted:
                return self._result or ActionResult(error="TECHNICAL_FAILURE: resume upload interrupted")
            self._attempted = True
            if runtime_controller.is_shutdown_requested():
                self._result = ActionResult(error="CANCELLED_BY_SHUTDOWN: resume upload not started")
            elif not self.enabled:
                self._result = ActionResult(error="TECHNICAL_FAILURE: resume upload is disabled by operator settings")
            elif purpose.strip().casefold() not in {"resume", "cv", "curriculum vitae"}:
                self._result = ActionResult(error="TECHNICAL_FAILURE: upload_resume accepts resume/CV only")
            else:
                self._result = await upload_resume_file(browser_session, self.resume_path)
            return self._result

    async def stop_after_failure(self, _agent: Any) -> None:
        # Browser Use invokes this outside its retryable step/action wrapper,
        # before judging completion or asking the model for another action.
        if self._attempted and (self._result is None or self._result.error):
            if self._result and str(self._result.error or "").startswith("CANCELLED_BY_SHUTDOWN"):
                raise RuntimeError(self._result.error)
            raise RuntimeError("EXTERNAL_RESUME_UPLOAD_FAILED: resume attachment could not be established")


def register_resume_upload_action(
    tools: Any, resume_path: str, *, enabled: bool = True
) -> ResumeUploadGuard:
    """Register a 0.12.6-compatible action that returns ``ActionResult``."""

    guard = ResumeUploadGuard(resume_path, enabled=enabled)

    @tools.action(
        description=(
            "Upload the configured resume to the real resume/CV input. Pass purpose='resume'. "
            "Call once; if it returns TECHNICAL_FAILURE, stop instead of trying arbitrary elements."
        )
    )
    async def upload_resume(purpose: str, browser_session) -> ActionResult:
        return await guard.upload(purpose, browser_session)

    return guard
