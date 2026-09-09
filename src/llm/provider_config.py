"""Safe provider configuration and credential discovery.

Only the primary ``llm_api_key`` is accepted for Gemini.  Every secondary
provider requires its own explicitly named credential or a valid local
endpoint.  This prevents accidentally sending a Gemini key to another API
and keeps status reporting free of secret material.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import dotenv


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    model: str
    credential_env: tuple[str, ...] = ()
    endpoint_env: tuple[str, ...] = ()
    timeout_seconds: int = 120
    priority: int = 0


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "gemini": ProviderSpec("gemini", "gemini-3.1-flash-lite", ("llm_api_key", "GEMINI_API_KEY"), priority=0),
    # OpenAI is the real secondary integration exposed by Browser Use 0.12.6.
    "openai": ProviderSpec("openai", "gpt-4o-mini", ("openai_api_key", "OPENAI_API_KEY", "LLM_OPENAI_API_KEY"), priority=1),
    "claude": ProviderSpec("claude", "claude-3-7-sonnet-latest", ("anthropic_api_key", "ANTHROPIC_API_KEY", "LLM_ANTHROPIC_API_KEY"), priority=2),
    "ollama": ProviderSpec("ollama", "llama3.2", (), ("ollama_api_url", "OLLAMA_BASE_URL"), priority=3),
    "openai_compatible": ProviderSpec("openai_compatible", "gpt-4o-mini", ("llm_secondary_api_key", "LLM_SECONDARY_API_KEY"), ("llm_secondary_api_url", "LLM_SECONDARY_API_URL"), priority=4),
}


def _dotenv_values() -> dict[str, str]:
    path = Path(__file__).resolve().parents[2] / ".env"
    try:
        return {str(k): str(v or "") for k, v in dotenv.dotenv_values(path).items() if k}
    except OSError:
        return {}


def credential_for(provider: str, primary_api_key: str | None = None) -> str:
    provider = str(provider or "").casefold()
    if provider == "gemini" and primary_api_key:
        return primary_api_key
    spec = PROVIDER_SPECS.get(provider)
    if spec is None:
        return ""
    values = _dotenv_values()
    for key in spec.credential_env:
        value = os.getenv(key) or values.get(key, "")
        if value:
            return value
    return ""


def endpoint_for(provider: str, configured_url: str | None = None) -> str:
    provider = str(provider or "").casefold()
    if provider == "openai_compatible" and configured_url:
        return configured_url
    spec = PROVIDER_SPECS.get(provider)
    if spec is None:
        return ""
    values = _dotenv_values()
    for key in spec.endpoint_env:
        value = os.getenv(key) or values.get(key, "")
        if value:
            return value
    return ""


def endpoint_is_valid(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def provider_is_configured(
    provider: str,
    *,
    primary_api_key: str | None = None,
    configured_url: str | None = None,
) -> bool:
    provider = str(provider or "").casefold()
    if provider == "gemini":
        return bool(credential_for(provider, primary_api_key))
    if provider == "ollama":
        # Ollama is local and needs no secret; its endpoint is optional.
        return True
    if provider == "openai_compatible":
        return bool(credential_for(provider)) and endpoint_is_valid(endpoint_for(provider, configured_url))
    return bool(credential_for(provider))


def configured_provider_candidates(
    primary: str,
    configured_order: object,
    *,
    primary_api_key: str | None = None,
    configured_url: str | None = None,
    enabled: bool = True,
    max_attempts: int = 2,
) -> tuple[str, ...]:
    """Return a bounded chain containing only genuinely configured providers."""
    requested = [primary]
    if enabled:
        if isinstance(configured_order, str):
            configured_order = (configured_order,)
        requested.extend(configured_order or ())
    result: list[str] = []
    for raw in requested:
        provider = str(raw or "").strip().casefold()
        if provider in result or not provider_is_configured(
            provider, primary_api_key=primary_api_key, configured_url=configured_url
        ):
            continue
        result.append(provider)
        if len(result) >= max(1, int(max_attempts)):
            break
    return tuple(result)


def provider_statuses(
    order: object,
    *,
    primary_api_key: str | None = None,
    configured_url: str | None = None,
) -> tuple[tuple[str, str, str], ...]:
    """Return ``(provider, model, configured/unconfigured)`` only."""
    if isinstance(order, str):
        order = (order,)
    rows = []
    for raw in order or ():
        provider = str(raw or "").strip().casefold()
        spec = PROVIDER_SPECS.get(provider)
        if not spec:
            continue
        status = "Configured" if provider_is_configured(
            provider, primary_api_key=primary_api_key, configured_url=configured_url
        ) else "Not Configured"
        rows.append((provider, spec.model, status))
    return tuple(rows)

