"""Deterministic initial navigation and bounded pre-application document readiness."""

import asyncio
import hashlib
import json
import time
from urllib.parse import urlsplit

from config.logger_config import logger


class ExternalEmptyDocument(RuntimeError):
    """No usable destination document was observed before the readiness deadline."""


class ExternalInitialNavigationFailed(RuntimeError):
    """The once-only initial navigation failed, without any model action."""


DOCUMENT_PROBE = """() => JSON.stringify({
    url: location.href,
    title: document.title,
    ready_state: document.readyState,
    body_exists: !!document.body,
    body_length: (document.body?.innerText || '').trim().length,
    dom_element_count: document.querySelectorAll('*').length,
    raw_control_count: document.querySelectorAll(
        'a, button, input, select, textarea, [role="button"], [role="link"]'
    ).length,
    visible_control_count: [...document.querySelectorAll(
        'a, button, input, select, textarea, [role="button"], [role="link"]'
    )].filter(el => {
        const style = getComputedStyle(el), rect = el.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden'
            && rect.width > 0 && rect.height > 0;
    }).length,
    frame_count: document.querySelectorAll('iframe, frame').length,
    visible_frame_count: [...document.querySelectorAll('iframe, frame')].filter(el => {
        const style = getComputedStyle(el), rect = el.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden'
            && rect.width > 0 && rect.height > 0;
    }).length
})"""


class ExternalDocumentGate:
    """Own startup across provider attempts; recovery only observes current state.

    Browser Use 0.12.6 suppresses automatic navigation for multi-URL tasks, and
    its inferred action requires the generic navigate tool Bobby excludes.
    Adapt upstream's explicit navigation pattern through BrowserSession's
    event API, outside model control. Never reopen/reload an application during
    fallback or choose an unrelated tab to manufacture apparent progress.
    """

    def __init__(self, job_url, timeout_seconds=30.0, poll_interval_seconds=0.25):
        self.job_url = job_url
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.navigation_started = False
        self.observation = {}

    async def prepare(self, browser_session, *, shutdown_requested, irreversible_activity):
        if shutdown_requested():
            raise RuntimeError("CANCELLED_BY_SHUTDOWN: document readiness stopped")
        if irreversible_activity():
            return None

        started = time.monotonic()
        # on_step_start is outside Browser Use's step_timeout. Bound the entire
        # hook, including native navigation, inventory, and every probe.
        async with asyncio.timeout(self.timeout_seconds) as deadline:
            try:
                if not self.navigation_started:
                    parts = urlsplit(self.job_url)
                    if parts.scheme not in {"http", "https"} or not parts.hostname:
                        raise ExternalInitialNavigationFailed(
                            "EXTERNAL_INITIAL_NAVIGATION_FAILED: invalid destination"
                        )
                    self.navigation_started = True  # consumed even if dispatch fails
                    logger.info("EXTERNAL_INITIAL_NAVIGATION | status=started")
                    try:
                        await browser_session.navigate_to(self.job_url, new_tab=False)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        raise ExternalInitialNavigationFailed(
                            "EXTERNAL_INITIAL_NAVIGATION_FAILED: browser navigation failed"
                        ) from None
                    logger.info("EXTERNAL_INITIAL_NAVIGATION | status=completed")

                first_identity = None
                first_url = None
                observations = 0
                while True:
                    if shutdown_requested():
                        raise RuntimeError("CANCELLED_BY_SHUTDOWN: document readiness stopped")
                    if irreversible_activity():
                        return None
                    observations += 1
                    try:
                        # Reacquire from Browser Use's current focus every time;
                        # an actor Page is neither a Playwright Page nor durable
                        # evidence that its old target is still selected.
                        page = await browser_session.must_get_current_page()
                        payload = await page.evaluate(DOCUMENT_PROBE)
                        payload = json.loads(payload) if isinstance(payload, str) else payload
                        if not isinstance(payload, dict):
                            raise ValueError("invalid document probe")
                        target_id = str(getattr(page, "_target_id", "") or "")
                        identity = hashlib.sha256(target_id.encode()).hexdigest()[:16]
                        url = str(payload.get("url") or "")
                        first_identity = identity if first_identity is None else first_identity
                        first_url = url if first_url is None else first_url
                        parts = urlsplit(url)
                        meaningful = (
                            parts.scheme in {"http", "https"}
                            and bool(parts.hostname)
                            and payload.get("ready_state") in {"interactive", "complete"}
                            and payload.get("body_exists") is True
                            and any(
                                int(payload.get(key, 0)) > 0
                                for key in ("body_length", "visible_control_count", "visible_frame_count")
                            )
                        )
                        self.observation = {
                            **payload,
                            "page_identity": identity,
                            "target_changed": identity != first_identity,
                            "url_changed": url != first_url,
                            "observation_count": observations,
                            "elapsed_ms": int((time.monotonic() - started) * 1000),
                            "meaningful": meaningful,
                        }
                        try:
                            tabs = await browser_session.get_tabs()
                            self.observation.update(
                                page_count=len(tabs),
                                active_page_index=next(
                                    (i for i, tab in enumerate(tabs)
                                     if getattr(tab, "target_id", None) == target_id), -1
                                ),
                            )
                            # Successful live evaluation proves this page is
                            # open; a lagging inventory alone cannot prove it
                            # closed or justify replacing it.
                            self.observation["page_closed"] = False
                        except Exception:
                            pass  # optional inventory must not invent zero pages
                        if meaningful:
                            logger.info(
                                "EXTERNAL_DOCUMENT_READY | "
                                f"observations={observations} "
                                f"body_length={self.observation.get('body_length', 0)} "
                                f"controls={self.observation.get('visible_control_count', 0)} "
                                f"target_changed={str(identity != first_identity).lower()}"
                            )
                            return self.observation
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        self.observation = {
                            "observation_count": observations,
                            "elapsed_ms": int((time.monotonic() - started) * 1000),
                            "error_class": type(error).__name__,
                            "meaningful": False,
                        }
                    if observations == 1:
                        logger.info("EXTERNAL_EMPTY_DOCUMENT | status=observing")
                    await asyncio.sleep(self.poll_interval_seconds)
            except asyncio.CancelledError:
                if not deadline.expired():
                    raise
                # Keep provider fallback from classifying a browser timeout as
                # provider unavailability and repeating an empty model session.
                logger.warning("EXTERNAL_EMPTY_DOCUMENT | status=bounded_failure")
                raise ExternalEmptyDocument(
                    "EXTERNAL_EMPTY_DOCUMENT: no meaningful destination document"
                ) from None
