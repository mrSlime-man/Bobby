import time

from src.llm.provider_health import (
    ProviderFallbackState,
    open_circuit,
    provider_candidates,
    provider_health,
)
from src.llm.provider_config import (
    configured_provider_candidates,
    provider_is_configured,
)


def test_provider_candidates_are_bounded_and_deduplicated():
    assert provider_candidates("gemini", ("gemini", "openai", "claude"), max_attempts=2) == (
        "gemini",
        "openai",
    )


def test_unconfigured_provider_is_visible_without_exposing_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("BOBBY_RUN_ID", "provider-health-test")
    monkeypatch.setattr("src.llm.provider_health.tempfile.gettempdir", lambda: str(tmp_path))
    state = provider_health("openai", configured=False)
    assert state.state == "UNCONFIGURED"
    assert state.configured is False
    assert state.reason == ""


def test_provider_health_reports_bounded_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("BOBBY_RUN_ID", "provider-cooldown-health-test")
    monkeypatch.setattr("src.llm.provider_health.tempfile.gettempdir", lambda: str(tmp_path))
    open_circuit("gemini", "model", "rate_limit", permanent=False, cooldown_seconds=1)
    assert provider_health("gemini", configured=True).state == "COOLDOWN"
    time.sleep(1.1)
    assert provider_health("gemini", configured=True).state == "AVAILABLE"


def test_secondary_provider_requires_its_own_credential(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("openai_api_key", raising=False)
    # Isolate this credential-discovery test from a developer's real .env.
    monkeypatch.setattr("src.llm.provider_config._dotenv_values", lambda: {})
    assert provider_is_configured("openai", primary_api_key="gemini-secret") is False
    assert configured_provider_candidates(
        "gemini",
        ("gemini", "openai"),
        primary_api_key="gemini-secret",
        max_attempts=2,
    ) == ("gemini",)


def test_real_openai_secondary_is_selected_when_separately_configured(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    assert provider_is_configured("openai", primary_api_key="gemini-secret") is True
    assert configured_provider_candidates(
        "gemini",
        ("gemini", "openai"),
        primary_api_key="gemini-secret",
        max_attempts=2,
    ) == ("gemini", "openai")


def test_provider_fallback_state_allows_verified_upload_but_stops_at_submit_or_failed_upload():
    state = ProviderFallbackState(("gemini", "openai"))
    assert state.next_provider() == "gemini"
    state.record_failure("gemini")
    assert state.can_switch(upload_failed=False, submit_attempted=False) is True
    assert state.next_provider() == "openai"
    state.record_failure("openai")
    assert state.exhausted is True
    assert state.can_switch(upload_failed=False, submit_attempted=False) is False

    state = ProviderFallbackState(("gemini", "openai"))
    assert state.next_provider() == "gemini"
    # A verified upload stays attached to the one retained browser session.
    assert state.can_switch(upload_failed=False, submit_attempted=False) is True
    assert state.can_switch(upload_failed=True, submit_attempted=False) is False
    assert state.can_switch(upload_failed=False, submit_attempted=True) is False


def test_transient_primary_failure_continues_same_bounded_application_state():
    state = ProviderFallbackState(("gemini", "openai"))
    current_step = "questions"
    provider = state.next_provider()
    assert provider == "gemini"
    # Simulated 503: the browser/application step is preserved while only the
    # model provider changes.
    state.record_failure(provider)
    assert state.can_switch(upload_failed=False, submit_attempted=False)
    provider = state.next_provider()
    assert provider == "openai"
    assert current_step == "questions"


def test_quota_failure_has_no_provider_loop_or_unsafe_replay():
    state = ProviderFallbackState(("gemini", "openai"))
    assert state.next_provider() == "gemini"
    state.record_failure("gemini")
    assert state.next_provider() == "openai"
    state.record_failure("openai")
    assert state.next_provider() is None
    assert state.can_switch(upload_failed=False, submit_attempted=False) is False


def test_browser_use_openai_secondary_client_initializes_without_network(monkeypatch):
    from src.llm.apply_agent import ApplyAgent

    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    agent = object.__new__(ApplyAgent)
    agent.primary_api_key = "test-gemini-key"
    agent.api_key = "test-gemini-key"
    agent.model = "gpt-4o-mini"
    llm = agent.select_model_type("openai", None, model="gpt-4o-mini")
    assert llm.__class__.__name__ == "ChatOpenAI"
