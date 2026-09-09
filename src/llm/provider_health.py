"""Per-run provider circuit breaker for external ATS automation."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from src.utils.run_context import get_run_id


def classify_provider_error(value: object) -> str | None:
    text = str(value or "").lower()
    if "429" in text and any(term in text for term in ("per day", "perday", "daily", "requests/day")):
        return "permanent_quota"
    if "resource_exhausted" in text or "429" in text:
        return "rate_limit"
    if "503" in text or "unavailable" in text or "high demand" in text:
        return "transient_unavailable"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    return None


def _path(run_id: str | None = None) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id or get_run_id())
    return Path(tempfile.gettempdir()) / f"bobby-provider-circuit-{safe}.json"


@dataclass(frozen=True)
class ProviderHealth:
    """Safe, operator-facing provider state with no credential material."""

    provider: str
    state: str
    configured: bool
    reason: str = ""
    expires_at: float | None = None


@dataclass
class ProviderFallbackState:
    """Bounded provider state for one external application worker."""

    candidates: tuple[str, ...]
    next_index: int = 0
    failed: tuple[str, ...] = ()

    def next_provider(self) -> str | None:
        if self.next_index >= len(self.candidates):
            return None
        provider = self.candidates[self.next_index]
        self.next_index += 1
        return provider

    def record_failure(self, provider: str) -> None:
        if provider not in self.failed:
            self.failed = (*self.failed, provider)

    def can_switch(self, *, upload_failed: bool = False, submit_attempted: bool) -> bool:
        """Allow one same-session handoff unless an irreversible boundary failed.

        A verified resume upload is safe to carry into a second provider's
        agent because the Browser instance and ``ResumeUploadGuard`` are
        retained.  A failed upload must stop, while a final submit can never
        be replayed and instead enters deterministic confirmation.
        """

        return not upload_failed and not submit_attempted and self.next_index < len(self.candidates)

    @property
    def exhausted(self) -> bool:
        return self.next_index >= len(self.candidates)


def provider_candidates(
    primary: str,
    configured_order: object = (),
    *,
    enabled: bool = True,
    max_attempts: int = 2,
) -> tuple[str, ...]:
    """Return a bounded, de-duplicated provider order.

    This is deliberately only an ordering helper.  It never invents provider
    credentials or enables a provider that the caller did not configure.
    """
    values = [primary]
    if enabled:
        if isinstance(configured_order, str):
            configured_order = (configured_order,)
        for value in configured_order or ():
            if isinstance(value, str) and value.strip():
                values.append(value.strip().lower())
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
        if len(result) >= max(1, int(max_attempts)):
            break
    return tuple(result)


def provider_health(
    provider: str,
    *,
    configured: bool,
    run_id: str | None = None,
) -> ProviderHealth:
    """Resolve a non-sensitive health state for a configured provider."""
    if not configured:
        return ProviderHealth(provider, "UNCONFIGURED", False)
    state = circuit_status(run_id, provider=provider)
    if state.get("provider") != provider:
        return ProviderHealth(provider, "AVAILABLE", True)
    reason = str(state.get("reason") or "")
    permanent = bool(state.get("permanent", True))
    expires_at = state.get("expires_at")
    try:
        active_cooldown = bool(expires_at and float(expires_at) > time.time())
    except (TypeError, ValueError):
        active_cooldown = False
    if permanent or active_cooldown:
        status = "QUOTA_EXHAUSTED" if reason == "permanent_quota" else "COOLDOWN"
        return ProviderHealth(provider, status, True, reason, expires_at)
    return ProviderHealth(provider, "AVAILABLE", True)


def open_circuit(
    provider: str,
    model: str,
    reason: str,
    *,
    permanent: bool = True,
    cooldown_seconds: int = 120,
) -> None:
    path = _path()
    record = {
        "provider": provider,
        "model": model,
        "reason": reason,
        "permanent": permanent,
        "opened_at": time.time(),
        "expires_at": None if permanent else time.time() + max(1, cooldown_seconds),
    }
    current = circuit_status()
    providers = current.get("providers") if isinstance(current.get("providers"), dict) else {}
    providers[str(provider)] = record
    payload = {**record, "providers": providers}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(json.dumps(payload, sort_keys=True))
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def circuit_status(run_id: str | None = None, provider: str | None = None) -> dict:
    try:
        payload = json.loads(_path(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if provider:
        providers = payload.get("providers")
        if isinstance(providers, dict) and isinstance(providers.get(provider), dict):
            return providers[provider]
        # Backward compatibility with the original single-provider file.
        return payload if payload.get("provider") == provider else {}
    return payload


def circuit_is_open(provider: str = "gemini") -> bool:
    state = circuit_status(provider=provider)
    if not state or state.get("provider") != provider:
        return False
    if state.get("permanent", True):
        return True
    try:
        return float(state.get("expires_at", 0)) > time.time()
    except (TypeError, ValueError):
        return False
