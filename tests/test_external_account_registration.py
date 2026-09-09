import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.llm.external_account_registration import (
    AccountRegistrationGuard,
    RegistrationFacts,
    RegistrationResult,
    complete_account_registration,
    is_registration_submit_control_text,
    registration_facts_from_profile,
    registration_page_present,
    registration_plan,
    unresolved_required_registration_controls,
)
from src.llm.external_apply_support import (
    claim_ats_account_registration,
    get_or_create_ats_password,
    mark_ats_registration_submit_started,
    password_meets_policy,
    release_ats_registration_claim,
)


def successfactors_controls():
    return [
        {
            "ordinal": 0,
            "kind": "password",
            "required": True,
            "enabled": True,
            "has_value": False,
            "checked": False,
            "max_length": 18,
        },
        {
            "ordinal": 1,
            "kind": "password",
            "required": True,
            "enabled": True,
            "has_value": False,
            "checked": False,
            "max_length": 18,
        },
        {
            "ordinal": 2,
            "kind": "text",
            "label": "First Name",
            "required": True,
            "enabled": True,
            "has_value": True,
            "checked": False,
        },
        {
            "ordinal": 3,
            "kind": "text",
            "label": "Last Name",
            "required": True,
            "enabled": True,
            "has_value": True,
            "checked": False,
        },
        {
            "ordinal": 4,
            "kind": "select",
            "label": "Country/Region Code",
            "required": True,
            "enabled": True,
            "has_value": False,
            "checked": False,
            "options": [
                {"value": "", "label": "Select"},
                {"value": "US1", "label": "United States (+1)"},
            ],
        },
        {
            "ordinal": 5,
            "kind": "radio",
            "name": "profile_visibility",
            "label": "Any job opportunity available",
            "required": True,
            "enabled": True,
            "has_value": True,
            "checked": False,
        },
        {
            "ordinal": 6,
            "kind": "radio",
            "name": "profile_visibility",
            "label": "This job opportunity only",
            "required": True,
            "enabled": True,
            "has_value": True,
            "checked": False,
        },
        {
            "ordinal": 7,
            "kind": "checkbox",
            "label": "Terms of Use and data privacy statement",
            "required": True,
            "enabled": True,
            "has_value": True,
            "checked": False,
        },
    ]


def test_registration_facts_use_candidate_profile_not_login_configuration():
    facts = registration_facts_from_profile(
        {
            "candidate": {
                "legal_name": "Example Candidate",
                "email": "candidate@example.invalid",
                "phone": "+15555550123",
            },
            "address": {"country": "United States"},
        }
    )

    assert facts.first_name == "Example"
    assert facts.last_name == "Candidate"
    assert facts.email == "candidate@example.invalid"
    assert facts.country == "United States"


def test_unknown_profile_registration_facts_are_not_filled_or_guessed():
    facts = registration_facts_from_profile(
        {
            "candidate": {
                "legal_name": "Known Candidate",
                "first_name": "UNKNOWN",
                "phone": "UNKNOWN",
                "email": "candidate@example.invalid",
            },
            "address": {"country": "UNKNOWN"},
        }
    )
    controls = [
        {
            "ordinal": 0,
            "kind": "tel",
            "label": "Phone number",
            "required": True,
            "enabled": True,
            "has_value": False,
            "checked": False,
        },
        {
            "ordinal": 1,
            "kind": "select",
            "label": "Country",
            "required": True,
            "enabled": True,
            "has_value": False,
            "checked": False,
            "options": [{"value": "US", "label": "United States"}],
        },
    ]

    plan = registration_plan(controls, facts)

    assert facts.first_name == ""
    assert facts.phone == ""
    assert facts.country == ""
    assert plan == ()
    assert unresolved_required_registration_controls(controls, plan) == (0, 1)


def test_successfactors_registration_plan_handles_policy_country_visibility_and_terms():
    facts = RegistrationFacts(
        email="candidate@example.invalid",
        first_name="Example",
        last_name="Candidate",
        phone="+15555550123",
        country="United States",
    )

    plan = registration_plan(successfactors_controls(), facts)

    assert [(item.ordinal, item.kind, item.value) for item in plan] == [
        (0, "password", ""),
        (1, "password", ""),
        (4, "select", "US1"),
        (6, "check", ""),
        (7, "check", ""),
    ]
    assert unresolved_required_registration_controls(successfactors_controls(), plan) == ()


def test_unknown_required_registration_control_fails_without_guessing():
    controls = successfactors_controls() + [
        {
            "ordinal": 8,
            "kind": "text",
            "label": "Employee identification number",
            "required": True,
            "enabled": True,
            "has_value": False,
            "checked": False,
        }
    ]
    plan = registration_plan(controls, RegistrationFacts(country="United States"))

    assert unresolved_required_registration_controls(controls, plan) == (8,)


def test_registration_plan_uses_accessible_candidate_backed_standard_fields():
    controls = [
        {
            "ordinal": 0,
            "kind": "password",
            "required": True,
            "enabled": True,
            "has_value": False,
            "checked": False,
        },
        {
            "ordinal": 1,
            "kind": "email",
            "aria_label": "Email address",
            "required": True,
            "enabled": True,
            "has_value": False,
            "checked": False,
        },
        {
            "ordinal": 2,
            "kind": "text",
            "autocomplete": "given-name",
            "required": True,
            "enabled": True,
            "has_value": False,
            "checked": False,
        },
        {
            "ordinal": 3,
            "kind": "text",
            "autocomplete": "family-name",
            "required": True,
            "enabled": True,
            "has_value": False,
            "checked": False,
        },
        {
            "ordinal": 4,
            "kind": "tel",
            "placeholder": "Mobile phone",
            "required": True,
            "enabled": True,
            "has_value": False,
            "checked": False,
        },
        {
            "ordinal": 5,
            "kind": "checkbox",
            "label": "I agree to the Privacy Policy",
            "required": True,
            "enabled": True,
            "has_value": True,
            "checked": False,
        },
    ]
    facts = RegistrationFacts(
        email="candidate@example.invalid",
        first_name="Example",
        last_name="Candidate",
        phone="+15555550123",
    )

    plan = registration_plan(controls, facts)

    assert [(item.ordinal, item.kind, item.value) for item in plan] == [
        (0, "password", ""),
        (1, "fill", "candidate@example.invalid"),
        (2, "fill", "Example"),
        (3, "fill", "Candidate"),
        (4, "fill", "+15555550123"),
        (5, "check", ""),
    ]
    assert unresolved_required_registration_controls(controls, plan) == ()


def test_policy_compatible_password_is_bounded_and_reused(tmp_path, monkeypatch):
    import src.llm.external_apply_support as support

    monkeypatch.setattr(support, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    password = get_or_create_ats_password(
        "https://careers.example.invalid/register",
        "candidate@example.invalid",
        maximum_length=18,
    )
    repeated = get_or_create_ats_password(
        "https://careers.example.invalid/register",
        "candidate@example.invalid",
        maximum_length=18,
    )

    assert password == repeated
    assert len(password) <= 18
    assert password_meets_policy(password, maximum_length=18)
    assert any(character in "!@#$%" for character in password)
    assert (tmp_path / "accounts.json").stat().st_mode & 0o777 == 0o600


def test_ats_credentials_are_encrypted_at_rest_and_reference_is_non_secret(tmp_path, monkeypatch):
    import src.llm.external_apply_support as support

    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(support, "ACCOUNTS_FILE", accounts_file)
    password = get_or_create_ats_password(
        "https://careers.example.invalid/register",
        "candidate@example.invalid",
    )

    raw = accounts_file.read_bytes()
    assert password.encode() not in raw
    assert accounts_file.read_text(encoding="utf-8", errors="ignore").lstrip()[:1] != "{"
    assert support._accounts_key_file().stat().st_mode & 0o777 == 0o600
    assert support._load_accounts()[next(iter(support._load_accounts()))]["password"] == password
    assert password not in support.credential_reference(
        "https://careers.example.invalid/register", "candidate@example.invalid"
    )


def test_concurrent_credential_requests_create_one_reusable_account(tmp_path, monkeypatch):
    import src.llm.external_apply_support as support

    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(support, "ACCOUNTS_FILE", accounts_file)

    with ThreadPoolExecutor(max_workers=8) as pool:
        passwords = list(
            pool.map(
                lambda _index: get_or_create_ats_password(
                    "https://careers.example.invalid/register",
                    "candidate@example.invalid",
                    maximum_length=18,
                ),
                range(24),
            )
        )

    assert len(set(passwords)) == 1
    assert len(support._load_accounts()) == 1
    lock_file = accounts_file.with_name(f".{accounts_file.name}.lock")
    assert lock_file.stat().st_mode & 0o777 == 0o600


def test_durable_registration_claim_allows_one_create_account_boundary(tmp_path, monkeypatch):
    import src.llm.external_apply_support as support

    monkeypatch.setattr(support, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    first = claim_ats_account_registration(
        "https://careers.example.invalid/register",
        "candidate@example.invalid",
        maximum_length=18,
    )
    assert first is not None
    # A concurrent worker cannot make a second physical Create Account click.
    assert (
        claim_ats_account_registration(
            "https://careers.example.invalid/register",
            "candidate@example.invalid",
            maximum_length=18,
        )
        is None
    )
    assert mark_ats_registration_submit_started(first) is True
    # The post-click marker is deliberately not reclaimed automatically.
    assert (
        claim_ats_account_registration(
            "https://careers.example.invalid/register",
            "candidate@example.invalid",
            maximum_length=18,
        )
        is None
    )


def test_unknown_registration_lifecycle_state_fails_closed(tmp_path, monkeypatch):
    """A corrupt/future state must never permit a second Create Account click."""

    import src.llm.external_apply_support as support

    accounts_file = tmp_path / "accounts.json"
    monkeypatch.setattr(support, "ACCOUNTS_FILE", accounts_file)
    password = get_or_create_ats_password(
        "https://careers.example.invalid/register",
        "candidate@example.invalid",
        maximum_length=18,
    )
    accounts = support._load_accounts()
    account = next(iter(accounts.values()))
    assert account["password"] == password
    account["registration"] = {"state": "future_or_corrupt_state"}
    with support._locked_accounts_store():
        support._write_accounts(accounts)

    assert (
        claim_ats_account_registration(
            "https://careers.example.invalid/register",
            "candidate@example.invalid",
            maximum_length=18,
        )
        is None
    )


def test_pre_submit_registration_claim_is_released_after_safe_failure(tmp_path, monkeypatch):
    import src.llm.external_apply_support as support

    monkeypatch.setattr(support, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    first = claim_ats_account_registration(
        "https://careers.example.invalid/register",
        "candidate@example.invalid",
    )
    assert first is not None
    release_ats_registration_claim(first)
    second = claim_ats_account_registration(
        "https://careers.example.invalid/register",
        "candidate@example.invalid",
    )
    assert second is not None


class _Element:
    def __init__(self, on_click=None):
        self.fill = AsyncMock()
        self.check = AsyncMock()
        self.click = AsyncMock(side_effect=on_click)


class _Page:
    def __init__(self, controls):
        self.controls = controls
        self.registration_visible = True
        self.auth_available = False
        self.after_body = ""
        self.element = _Element(self._click)
        self.get_elements_by_css_selector = AsyncMock(return_value=[self.element])

    async def _click(self):
        self.registration_visible = False
        self.after_body = "Account created. Continue application."

    async def get_url(self):
        return "https://career.example.invalid/register"

    async def evaluate(self, script, *_args):
        if "document.body" in script:
            return self.after_body if not self.registration_visible else ""
        if "const submit =" in script:
            return str(self.registration_visible).lower()
        if "data-bobby-existing-account-auth" in script:
            return str(self.auth_available).lower()
        if "querySelectorAll('input, select, textarea')" in script and "map" in script:
            return json.dumps(self.controls)
        return "true"


@pytest.mark.asyncio
async def test_registration_action_completes_once_with_only_standard_controls(
    tmp_path, monkeypatch
):
    import src.llm.external_apply_support as support

    monkeypatch.setattr(support, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    page = _Page(successfactors_controls())
    session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))
    facts = RegistrationFacts(
        email="candidate@example.invalid",
        country="United States",
    )

    with (patch("src.llm.external_account_registration.asyncio.sleep", new_callable=AsyncMock),):
        result = await complete_account_registration(session, facts=facts)

    assert result.status == "ACCOUNT_CREATED"
    assert result.actions == 5
    assert page.element.fill.await_count == 2
    assert page.element.check.await_count == 2
    page.element.click.assert_awaited_once()
    persisted = support._load_accounts()
    assert next(iter(persisted.values()))["registration"]["state"] == "created"


@pytest.mark.asyncio
async def test_registration_waits_for_a_bounded_late_transition(tmp_path, monkeypatch):
    """A slow ordinary registration transition gets no second create click."""

    import src.llm.external_apply_support as support

    monkeypatch.setattr(support, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    page = _Page(successfactors_controls())
    page.element = _Element()  # Keep the form visible through the first recheck.
    page.get_elements_by_css_selector.return_value = [page.element]
    session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))
    sleep_calls = 0

    async def delayed_transition(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        page.registration_visible = False
        page.after_body = "Account created. Continue application."

    with (
        patch(
            "src.llm.external_account_registration.asyncio.sleep",
            side_effect=delayed_transition,
        ),
    ):
        result = await complete_account_registration(
            session,
            facts=RegistrationFacts(email="candidate@example.invalid", country="United States"),
        )

    assert result.status == "ACCOUNT_CREATED"
    assert sleep_calls == 1
    page.element.click.assert_awaited_once()
    persisted = support._load_accounts()
    assert next(iter(persisted.values()))["registration"]["state"] == "created"


@pytest.mark.asyncio
async def test_unchanged_custom_registration_form_is_never_recorded_as_created(
    tmp_path, monkeypatch
):
    """A detector mismatch after Create Account is not positive success evidence."""

    import src.llm.external_apply_support as support

    monkeypatch.setattr(support, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    page = _Page(successfactors_controls())
    async def hide_submit_without_transition():
        page.registration_visible = False

    page.element = _Element(hide_submit_without_transition)
    page.get_elements_by_css_selector.return_value = [page.element]
    page.after_body = ""  # Keep URL and visible text unchanged after the click.
    session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))

    with patch("src.llm.external_account_registration.asyncio.sleep", new_callable=AsyncMock):
        result = await complete_account_registration(
            session,
            facts=RegistrationFacts(email="candidate@example.invalid", country="United States"),
        )

    assert result.status == "TECHNICAL_FAILURE"
    assert result.detail == "registration_no_transition"
    page.element.click.assert_awaited_once()
    persisted = support._load_accounts()
    assert next(iter(persisted.values()))["registration"]["state"] == "submit_started"


@pytest.mark.asyncio
async def test_registration_action_stops_for_security_challenge():
    page = _Page(successfactors_controls())

    async def evaluate(script, *_args):
        if "document.body" in script:
            return "MFA verification code required"
        raise AssertionError("security challenge must stop before form discovery")

    page.evaluate = evaluate
    session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))

    result = await complete_account_registration(
        session,
        facts=RegistrationFacts(email="candidate@example.invalid"),
    )

    assert result.status == "NEEDS_HUMAN"
    page.element.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_registration_validation_after_click_is_not_recorded_as_created(
    tmp_path, monkeypatch
):
    import src.llm.external_apply_support as support

    monkeypatch.setattr(support, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    page = _Page(successfactors_controls())
    page.after_body = "Please complete the required field."
    async def expose_validation():
        page.registration_visible = False

    page.element = _Element(expose_validation)
    page.get_elements_by_css_selector.return_value = [page.element]
    session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))

    with patch("src.llm.external_account_registration.asyncio.sleep", new_callable=AsyncMock):
        result = await complete_account_registration(
            session,
            facts=RegistrationFacts(email="candidate@example.invalid", country="United States"),
        )

    assert result.status == "TECHNICAL_FAILURE"
    assert result.detail == "registration_validation"
    page.element.click.assert_awaited_once()
    persisted = support._load_accounts()
    assert next(iter(persisted.values()))["registration"]["state"] == "submit_started"


@pytest.mark.asyncio
async def test_existing_registration_routes_once_to_actual_sign_in_without_create_click(
    tmp_path, monkeypatch
):
    import src.llm.external_apply_support as support

    monkeypatch.setattr(support, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    first = claim_ats_account_registration(
        "https://career.example.invalid/register",
        "candidate@example.invalid",
    )
    assert first is not None
    assert mark_ats_registration_submit_started(first)

    # An old registration form may contain fields this worker should never
    # touch.  The persisted submit boundary routes straight to sign-in first.
    page = _Page(
        successfactors_controls()
        + [
            {
                "ordinal": 8,
                "kind": "text",
                "label": "Employee identification number",
                "required": True,
                "enabled": True,
                "has_value": False,
                "checked": False,
            }
        ]
    )
    page.auth_available = True
    session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))
    result = await complete_account_registration(
        session,
        facts=RegistrationFacts(email="candidate@example.invalid", country="United States"),
    )

    assert result.status == "AUTH_ROUTED_EXISTING_ACCOUNT"
    page.element.fill.assert_not_awaited()
    page.element.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_known_account_without_an_auth_entry_never_retries_registration(
    tmp_path, monkeypatch
):
    import src.llm.external_apply_support as support

    monkeypatch.setattr(support, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    first = claim_ats_account_registration(
        "https://career.example.invalid/register",
        "candidate@example.invalid",
    )
    assert first is not None
    assert mark_ats_registration_submit_started(first)

    page = _Page(successfactors_controls())
    session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))
    result = await complete_account_registration(
        session,
        facts=RegistrationFacts(email="candidate@example.invalid", country="United States"),
    )

    assert result.status == "TECHNICAL_FAILURE"
    assert result.detail == "registration_already_claimed"
    page.element.fill.assert_not_awaited()
    page.element.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_live_registration_does_not_route_or_click_from_second_worker(
    tmp_path, monkeypatch
):
    import src.llm.external_apply_support as support

    monkeypatch.setattr(support, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    first = claim_ats_account_registration(
        "https://career.example.invalid/register",
        "candidate@example.invalid",
    )
    assert first is not None
    # The first worker owns a live pre-submit claim. A second worker may not
    # fill, create, or try credentials before that boundary resolves.
    page = _Page(successfactors_controls())
    page.auth_available = True
    session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))
    result = await complete_account_registration(
        session,
        facts=RegistrationFacts(email="candidate@example.invalid", country="United States"),
    )

    assert result.status == "TECHNICAL_FAILURE"
    assert result.detail == "registration_in_progress"
    page.element.fill.assert_not_awaited()
    page.element.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_registration_page_presence_requires_actionable_registration_form():
    page = _Page(successfactors_controls())
    session = SimpleNamespace(must_get_current_page=AsyncMock(return_value=page))

    assert await registration_page_present(session) is True

    async def login_only(_script, *_args):
        return "false"

    page.evaluate = login_only
    assert await registration_page_present(session) is False


def test_registration_submit_label_accepts_to_apply_only_at_the_guarded_boundary():
    assert is_registration_submit_control_text("Sign up to apply") is True
    assert is_registration_submit_control_text("Create an account to apply") is True
    assert is_registration_submit_control_text("Register now") is True
    assert is_registration_submit_control_text("Get Started for Free") is True
    assert is_registration_submit_control_text("Already a member? Sign in now") is False
    assert is_registration_submit_control_text("Get Started") is False
    assert is_registration_submit_control_text("Send my profile") is False


@pytest.mark.asyncio
async def test_registration_guard_never_repeats_a_physical_registration_attempt():
    guard = AccountRegistrationGuard(RegistrationFacts(email="candidate@example.invalid"))
    session = object()
    with patch(
        "src.llm.external_account_registration.complete_account_registration",
        new_callable=AsyncMock,
        return_value=RegistrationResult("ACCOUNT_CREATED", actions=5),
    ) as complete:
        first = await guard.complete(session)
        second = await guard.complete(session)

    assert first == second
    assert guard.attempted is True
    complete.assert_awaited_once_with(session, facts=guard.facts)
