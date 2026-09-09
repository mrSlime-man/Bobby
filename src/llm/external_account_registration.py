"""Bounded, profile-backed handling for ordinary ATS account registration."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from config.logger_config import logger
from src.utils.runtime_control import runtime_controller

from src.llm.external_apply_support import (
    ats_account_registration_state,
    claim_ats_account_registration,
    find_validation_errors,
    mark_ats_registration_created,
    mark_ats_registration_submit_started,
    needs_human,
    normalize_text,
    release_ats_registration_claim,
)


_REGISTRATION_SUBMIT_CONTROL_PATTERN = (
    r"^(?:create (?:an? )?account|register(?: now)?|sign[ -]?up(?: now)?|"
    r"get started for free)(?:\s+to\s+apply)?$"
)
_REGISTRATION_SUBMIT_CONTROL_RE = re.compile(_REGISTRATION_SUBMIT_CONTROL_PATTERN, re.IGNORECASE)
_REGISTRATION_TRANSITION_OBSERVATIONS = 8
_REGISTRATION_TRANSITION_POLL_SECONDS = 1.0


def is_registration_submit_control_text(value: object) -> bool:
    """Recognize a registration dispatch label, never a landing-page CTA alone."""

    return bool(_REGISTRATION_SUBMIT_CONTROL_RE.fullmatch(str(value or "").strip()))


@dataclass(frozen=True)
class RegistrationFacts:
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    phone: str = ""
    country: str = ""


@dataclass(frozen=True)
class RegistrationAction:
    ordinal: int
    kind: str
    value: str = ""


@dataclass(frozen=True)
class RegistrationResult:
    status: str
    actions: int = 0
    detail: str = ""


def registration_facts_from_profile(profile: Mapping[str, Any]) -> RegistrationFacts:
    """Select only the profile facts needed for a normal account form."""

    def known(value: object) -> str:
        text = str(value or "").strip()
        return "" if normalize_text(text) == "unknown" else text

    candidate = profile.get("candidate") or {}
    address = profile.get("address") or {}
    legal_name = known(candidate.get("legal_name")) or known(candidate.get("full_name"))
    parts = legal_name.split()
    first_name = (
        known(candidate.get("first_name"))
        if "first_name" in candidate
        else (parts[0] if parts else "")
    )
    last_name = (
        known(candidate.get("last_name"))
        if "last_name" in candidate
        else (parts[-1] if len(parts) > 1 else "")
    )
    return RegistrationFacts(
        email=known(candidate.get("email")),
        first_name=first_name,
        last_name=last_name,
        phone=known(candidate.get("phone")),
        country=known(address.get("country")),
    )


def _semantic(control: Mapping[str, Any]) -> str:
    return normalize_text(
        " ".join(
            str(control.get(key) or "")
            for key in (
                "label",
                "name",
                "id",
                "autocomplete",
                "aria_label",
                "placeholder",
                "nearby_text",
                "type",
            )
        )
    )


def _option_choice(options: object, country: str) -> str:
    """Choose a visible native-select value using only profile country data."""

    country_text = normalize_text(country)
    if not country_text or not isinstance(options, list):
        return ""
    aliases = {country_text}
    if country_text in {"united states", "united states of america", "usa", "us"}:
        aliases.update({"united states", "united states of america", "usa", "us", "us +1", "+1"})

    best_score = -1
    best_value = ""
    for option in options:
        if not isinstance(option, Mapping):
            continue
        value = str(option.get("value") or "").strip()
        label = normalize_text(option.get("label") or "")
        normalized_value = normalize_text(value)
        score = 0
        if label in aliases or normalized_value in aliases:
            score = 100
        elif any(alias and alias in label for alias in aliases if len(alias) >= 2):
            score = 90
        elif country_text in label or country_text == normalized_value:
            score = 80
        if score > best_score and value:
            best_score = score
            best_value = value
    return best_value


def registration_plan(
    controls: list[Mapping[str, Any]],
    facts: RegistrationFacts,
) -> tuple[RegistrationAction, ...]:
    """Make a deterministic, least-disclosure registration plan.

    It deliberately covers only standard account fields.  Unsupported required
    fields are left for the caller to classify, never guessed.
    """

    actions: list[RegistrationAction] = []
    for control in controls:
        if not bool(control.get("enabled", True)):
            continue
        try:
            ordinal = int(control["ordinal"])
        except (KeyError, TypeError, ValueError):
            continue
        kind = normalize_text(control.get("kind") or "")
        semantic = _semantic(control)
        required = bool(control.get("required"))
        has_value = bool(control.get("has_value"))
        checked = bool(control.get("checked"))

        if kind == "password":
            actions.append(RegistrationAction(ordinal, "password"))
        elif kind in {"text", "email", "tel"} and required and not has_value:
            if kind == "email" or "email" in semantic:
                if facts.email:
                    actions.append(RegistrationAction(ordinal, "fill", facts.email))
            elif (
                ("first" in semantic and "name" in semantic) or "given-name" in semantic
            ) and facts.first_name:
                actions.append(RegistrationAction(ordinal, "fill", facts.first_name))
            elif (
                ("last" in semantic and "name" in semantic) or "family-name" in semantic
            ) and facts.last_name:
                actions.append(RegistrationAction(ordinal, "fill", facts.last_name))
            elif (kind == "tel" or "phone" in semantic or "mobile" in semantic) and facts.phone:
                actions.append(RegistrationAction(ordinal, "fill", facts.phone))
        elif kind == "select" and required and not has_value:
            selected = _option_choice(control.get("options"), facts.country)
            if selected:
                actions.append(RegistrationAction(ordinal, "select", selected))
        elif kind == "radio" and not checked:
            # A per-job-only profile is the safe privacy-minimizing option.
            if "this job opportunity only" in semantic or "this job only" in semantic:
                actions.append(RegistrationAction(ordinal, "check"))
        elif kind == "checkbox" and required and not checked:
            if "terms" in semantic or "privacy" in semantic or "i agree" in semantic:
                actions.append(RegistrationAction(ordinal, "check"))
    return tuple(actions)


def unresolved_required_registration_controls(
    controls: list[Mapping[str, Any]],
    actions: tuple[RegistrationAction, ...],
) -> tuple[int, ...]:
    """Return required controls not covered by a safe deterministic action."""

    resolved = {action.ordinal for action in actions}
    by_ordinal = {
        int(control["ordinal"]): control
        for control in controls
        if str(control.get("ordinal", "")).isdigit()
    }
    resolved_radio_groups = {
        normalize_text(by_ordinal[action.ordinal].get("name") or "")
        for action in actions
        if action.kind == "check"
        and normalize_text(by_ordinal.get(action.ordinal, {}).get("kind") or "") == "radio"
        and normalize_text(by_ordinal.get(action.ordinal, {}).get("name") or "")
    }
    unresolved: list[int] = []
    for control in controls:
        if not bool(control.get("required")) or not bool(control.get("enabled", True)):
            continue
        kind = normalize_text(control.get("kind") or "")
        if kind == "radio" and normalize_text(control.get("name") or "") in resolved_radio_groups:
            continue
        if kind in {"radio", "checkbox"}:
            complete = bool(control.get("checked"))
        else:
            complete = bool(control.get("has_value"))
        if complete:
            continue
        try:
            ordinal = int(control["ordinal"])
        except (KeyError, TypeError, ValueError):
            continue
        if ordinal not in resolved:
            unresolved.append(ordinal)
    return tuple(unresolved)


_CONTROL_DISCOVERY = """() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const labelFor = element => {
        const labels = element.labels ? [...element.labels].map(label => label.innerText || label.textContent).join(' ') : '';
        const parent = element.closest('label, .field, .form-group, [data-field], li, td, div');
        return clean(labels || element.getAttribute('aria-label') || element.getAttribute('placeholder') || parent?.innerText || '');
    };
    return [...document.querySelectorAll('input, select, textarea')].map((element, ordinal) => ({
        ordinal,
        kind: element.tagName === 'SELECT' ? 'select' : (element.type || element.tagName).toLowerCase(),
        label: labelFor(element).slice(0, 400),
        name: clean(element.name),
        id: clean(element.id),
        autocomplete: clean(element.autocomplete),
        aria_label: clean(element.getAttribute('aria-label')),
        placeholder: clean(element.getAttribute('placeholder')),
        nearby_text: clean(element.closest('label, .field, .form-group, [data-field], li, td, div')?.innerText || '').slice(0, 400),
        required: Boolean(element.required || element.getAttribute('aria-required') === 'true'),
        enabled: !element.disabled,
        has_value: Boolean(element.value),
        checked: Boolean(element.checked),
        min_length: Number(element.minLength) > 0 ? Number(element.minLength) : null,
        max_length: Number(element.maxLength) > 0 ? Number(element.maxLength) : null,
        options: element.tagName === 'SELECT' ? [...element.options].map(option => ({ value: option.value, label: clean(option.textContent) })) : []
    }));
}"""


async def _control_element(page: Any, ordinal: int) -> Any | None:
    marked = await page.evaluate(
        """(ordinal) => {
            const element = [...document.querySelectorAll('input, select, textarea')][ordinal];
            if (!element) return false;
            element.setAttribute('data-bobby-registration-control', String(ordinal));
            return true;
        }""",
        ordinal,
    )
    if str(marked).casefold() != "true":
        return None
    elements = await page.get_elements_by_css_selector(
        f'[data-bobby-registration-control="{ordinal}"]'
    )
    return elements[0] if elements else None


async def _select_native_option(page: Any, ordinal: int, value: str) -> bool:
    selected = await page.evaluate(
        """(ordinal, value) => {
            const select = [...document.querySelectorAll('input, select, textarea')][ordinal];
            if (!select || select.tagName !== 'SELECT') return false;
            const option = [...select.options].find(item => item.value === value);
            if (!option) return false;
            select.value = option.value;
            select.dispatchEvent(new Event('input', {bubbles: true}));
            select.dispatchEvent(new Event('change', {bubbles: true}));
            return select.value === option.value;
        }""",
        ordinal,
        value,
    )
    return str(selected).casefold() == "true"


async def _click_create_account(
    page: Any,
    before_click: Callable[[], bool] | None = None,
) -> bool:
    pattern = json.dumps(_REGISTRATION_SUBMIT_CONTROL_PATTERN)
    marked = await page.evaluate(r"""() => {
            const controls = [...document.querySelectorAll('button, input[type="submit"], input[type="button"]')];
            const text = control => [control.innerText, control.value, control.getAttribute('aria-label'), control.getAttribute('title')]
                .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
            const target = controls.find(control => new RegExp(""" + pattern + r""", 'i').test(text(control)));
            if (!target || target.disabled) return false;
            target.setAttribute('data-bobby-registration-submit', 'true');
            return true;
        }""")
    if str(marked).casefold() != "true":
        return False
    buttons = await page.get_elements_by_css_selector('[data-bobby-registration-submit="true"]')
    if not buttons:
        return False
    if before_click is not None and not before_click():
        return False
    await buttons[0].click()
    return True


async def _route_to_existing_account(page: Any) -> bool:
    """Choose one real sign-in entry when account creation is already claimed.

    This is intentionally limited to a visible auth control on the current
    registration page.  It never submits Create Account, guesses credentials,
    or follows a security flow.
    """

    marked = await page.evaluate(r"""() => {
            const visible = element => {
                const style = window.getComputedStyle(element);
                return style.display !== 'none' && style.visibility !== 'hidden' && !element.disabled;
            };
            const text = control => [control.innerText, control.value, control.getAttribute('aria-label'), control.getAttribute('title')]
                .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
            const controls = [...document.querySelectorAll('a, button, input[type="button"], input[type="submit"]')];
            const target = controls.find(control => visible(control) && /\b(?:sign in|log in|login|existing (?:candidate|account)|returning candidate)\b/i.test(text(control)));
            if (!target) return false;
            target.setAttribute('data-bobby-existing-account-auth', 'true');
            return true;
        }""")
    if str(marked).casefold() != "true":
        return False
    controls = await page.get_elements_by_css_selector('[data-bobby-existing-account-auth="true"]')
    if not controls:
        return False
    await controls[0].click()
    return True


async def registration_page_present(browser_session: Any) -> bool:
    """Return true only for a page whose actual submit control creates an account.

    A sign-in page can link to registration, so visible prose alone is not a
    sufficient reason to run an irreversible account-creation action.
    """

    try:
        page = await browser_session.must_get_current_page()
        pattern = json.dumps(_REGISTRATION_SUBMIT_CONTROL_PATTERN)
        found = await page.evaluate(r"""() => {
                const visible = element => {
                    const style = window.getComputedStyle(element);
                    return style.display !== 'none' && style.visibility !== 'hidden' && !element.disabled;
                };
                const text = control => [control.innerText, control.value, control.getAttribute('aria-label'), control.getAttribute('title')]
                    .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
                const submit = [...document.querySelectorAll('button, input[type="submit"], input[type="button"]')]
                    .some(control => visible(control) && new RegExp(""" + pattern + r""", 'i').test(text(control)));
                const passwords = [...document.querySelectorAll('input[type="password"]')].filter(visible).length;
                const profileField = [...document.querySelectorAll('input, select, textarea')].some(control => {
                    const labels = control.labels ? [...control.labels].map(label => label.innerText || label.textContent).join(' ') : '';
                    const parent = control.closest('label, .field, .form-group, [data-field], li, td, div');
                    const semantic = [control.name, control.id, control.autocomplete, control.getAttribute('aria-label'), control.getAttribute('placeholder'), labels, parent?.innerText]
                        .join(' ').toLowerCase();
                    return /email|first.?name|last.?name|phone|country/.test(semantic);
                });
                return submit && passwords > 0 && profileField;
            }""")
        return str(found).casefold() == "true"
    except Exception:
        return False


async def _registration_submit_still_present(page: Any) -> bool | None:
    """Read the one-way registration controls without relying on field labels.

    Post-dispatch verification already knows this was a real registration form.
    Requiring an email/name label again can turn a still-visible custom form
    into a false "created" transition after a client-side re-render. A read
    failure is intentionally indeterminate so callers fail closed.
    """

    try:
        pattern = json.dumps(_REGISTRATION_SUBMIT_CONTROL_PATTERN)
        present = await page.evaluate(r"""() => {
                const visible = element => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden'
                        && !element.disabled && rect.width > 0 && rect.height > 0;
                };
                const text = control => [control.innerText, control.value, control.getAttribute('aria-label'), control.getAttribute('title')]
                    .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
                const submit = [...document.querySelectorAll('button, input[type="submit"], input[type="button"]')]
                    .some(control => visible(control) && new RegExp(""" + pattern + r""", 'i').test(text(control)));
                const password = [...document.querySelectorAll('input[type="password"]')].some(visible);
                return submit && password;
            }""")
        return str(present).casefold() == "true"
    except Exception:
        return None


async def _wait_for_registration_transition(
    browser_session: Any,
    page: Any,
    *,
    before_url: str,
    before_body: str,
    action_count: int,
) -> RegistrationResult | None:
    """Read-only bounded verification after one account-create dispatch.

    Some ATSs accept a normal registration submit asynchronously. A two-second
    single check can classify that legitimate transition as a failure before
    the client has updated. This function never clicks again or enters another
    value; it only observes the current page until the real registration form
    disappears, validation/security evidence appears, or the bounded window
    expires.
    """

    before_body_key = normalize_text(before_body)
    for observation in range(1, _REGISTRATION_TRANSITION_OBSERVATIONS + 1):
        after_url = str(await page.get_url() or "")
        after_body = str(
            await page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        )
        human_required, human_reason = needs_human(after_body)
        if human_required:
            return RegistrationResult("NEEDS_HUMAN", action_count, human_reason)
        if find_validation_errors(after_body):
            return RegistrationResult("TECHNICAL_FAILURE", action_count, "registration_validation")
        registration_submit_present = await _registration_submit_still_present(page)
        if registration_submit_present is None:
            return RegistrationResult(
                "TECHNICAL_FAILURE", action_count, "registration_transition_unavailable"
            )
        url_changed = after_url != before_url
        body_changed = normalize_text(after_body) != before_body_key
        logger.info(
            "ATS_ACCOUNT_REGISTRATION_RECHECK | "
            f"observation={observation}/{_REGISTRATION_TRANSITION_OBSERVATIONS} "
            f"url_changed={str(url_changed).lower()} "
            f"body_changed={str(body_changed).lower()} "
            f"registration_submit_present={str(registration_submit_present).lower()}"
        )
        # A lost label/profile-field match alone is not success evidence. The
        # original account form must actually disappear *and* the page must
        # expose a separate visible/navigation transition before the durable
        # account state can become created.
        if not registration_submit_present and (url_changed or body_changed):
            return None
        if observation < _REGISTRATION_TRANSITION_OBSERVATIONS:
            await asyncio.sleep(_REGISTRATION_TRANSITION_POLL_SECONDS)
    return RegistrationResult("TECHNICAL_FAILURE", action_count, "registration_no_transition")


async def complete_account_registration(
    browser_session: Any,
    *,
    facts: RegistrationFacts,
) -> RegistrationResult:
    """Complete one ordinary account form without handling security challenges."""

    if runtime_controller.is_shutdown_requested():
        return RegistrationResult("CANCELLED_BY_SHUTDOWN")

    claim = None
    submit_started = False
    password = None
    try:
        page = await browser_session.must_get_current_page()
        body = str(await page.evaluate("() => document.body ? document.body.innerText : ''") or "")
        human_required, human_reason = needs_human(body)
        if human_required:
            return RegistrationResult("NEEDS_HUMAN", detail=human_reason)

        raw_controls = await page.evaluate(_CONTROL_DISCOVERY)
        controls = json.loads(raw_controls) if isinstance(raw_controls, str) else raw_controls
        if not isinstance(controls, list):
            return RegistrationResult(
                "TECHNICAL_FAILURE", detail="registration_controls_unavailable"
            )
        password_controls = [
            control for control in controls if normalize_text(control.get("kind")) == "password"
        ]
        limits = [
            int(control["max_length"])
            for control in password_controls
            if isinstance(control.get("max_length"), int) and int(control["max_length"]) > 0
        ]
        minimums = [
            int(control["min_length"])
            for control in password_controls
            if isinstance(control.get("min_length"), int) and int(control["min_length"]) > 0
        ]
        claim = claim_ats_account_registration(
            await page.get_url(),
            facts.email,
            minimum_length=max(minimums, default=8),
            maximum_length=min(limits) if limits else None,
        )
        if claim is None:
            registration_state = ats_account_registration_state(await page.get_url(), facts.email)
            if registration_state == "claimed":
                return RegistrationResult("TECHNICAL_FAILURE", detail="registration_in_progress")
            if registration_state in {
                "submit_started",
                "created",
                "legacy",
            } and await _route_to_existing_account(page):
                return RegistrationResult("AUTH_ROUTED_EXISTING_ACCOUNT")
            return RegistrationResult("TECHNICAL_FAILURE", detail="registration_already_claimed")

        if not password_controls:
            return RegistrationResult("TECHNICAL_FAILURE", detail="registration_password_missing")
        plan = registration_plan(controls, facts)
        unresolved = unresolved_required_registration_controls(controls, plan)
        if unresolved:
            return RegistrationResult(
                "TECHNICAL_FAILURE", detail="unsupported_required_registration_control"
            )
        password = claim.password

        action_count = 0
        for action in plan:
            element = await _control_element(page, action.ordinal)
            if element is None:
                return RegistrationResult(
                    "TECHNICAL_FAILURE", action_count, "registration_control_stale"
                )
            if action.kind == "password":
                await element.fill(password)
            elif action.kind == "fill":
                await element.fill(action.value)
            elif action.kind == "select":
                if not await _select_native_option(page, action.ordinal, action.value):
                    return RegistrationResult(
                        "TECHNICAL_FAILURE", action_count, "registration_select_not_applied"
                    )
            elif action.kind == "check":
                await element.check()
            action_count += 1

        def begin_submit() -> bool:
            nonlocal submit_started
            if not runtime_controller.try_start_irreversible_dispatch("account_registration"):
                return False
            submit_started = mark_ats_registration_submit_started(claim)
            if submit_started:
                logger.info("EXTERNAL_IRREVERSIBLE_DISPATCH | action=account_registration")
            return submit_started

        before_url = str(await page.get_url() or "")
        if not await _click_create_account(page, before_click=begin_submit):
            if runtime_controller.is_shutdown_requested():
                return RegistrationResult("CANCELLED_BY_SHUTDOWN", action_count)
            return RegistrationResult(
                "TECHNICAL_FAILURE", action_count, "registration_submit_unavailable"
            )
        transition_result = await _wait_for_registration_transition(
            browser_session,
            page,
            before_url=before_url,
            before_body=body,
            action_count=action_count,
        )
        if transition_result is not None:
            return transition_result
        if not mark_ats_registration_created(claim):
            return RegistrationResult(
                "TECHNICAL_FAILURE", action_count, "registration_state_unavailable"
            )
        return RegistrationResult("ACCOUNT_CREATED", action_count)
    except ValueError:
        return RegistrationResult("TECHNICAL_FAILURE", detail="registration_password_policy")
    except Exception as exc:
        return RegistrationResult("TECHNICAL_FAILURE", detail=type(exc).__name__)
    finally:
        # Claims are released only before the durable Create Account boundary.
        # Once that marker is written, replaying the form would risk a duplicate
        # account even when the browser dies immediately after the click.
        password = None
        if claim is not None and not submit_started:
            release_ats_registration_claim(claim)


class AccountRegistrationGuard:
    """Allow one physical account-creation attempt per worker/session."""

    def __init__(self, facts: RegistrationFacts) -> None:
        self.facts = facts
        self._lock = asyncio.Lock()
        self._attempted = False
        self._result: RegistrationResult | None = None

    @property
    def attempted(self) -> bool:
        return self._attempted

    @property
    def result(self) -> RegistrationResult | None:
        """Return the first guarded result without re-running registration."""

        return self._result

    async def complete(self, browser_session: Any) -> RegistrationResult:
        async with self._lock:
            if self._attempted:
                return self._result or RegistrationResult(
                    "TECHNICAL_FAILURE", detail="registration_interrupted"
                )
            self._attempted = True
            self._result = await complete_account_registration(
                browser_session,
                facts=self.facts,
            )
            return self._result
