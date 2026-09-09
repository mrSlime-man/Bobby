"""Atomic state shared between an external ATS worker and its parent."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

PRE_SUBMIT = {"starting", "running", "pre_submit", "cancelled_before_submit"}
SUBMIT_CRITICAL = {"submit_click_started", "submit_attempted", "verifying"}
TERMINAL = {"submitted", "unverified_after_submit", "technical_failure", "needs_human", "not_eligible"}


def read_worker_state(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def write_worker_state(path: str | Path, phase: str, **updates: Any) -> dict[str, Any]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = read_worker_state(target)
    payload.update(updates)
    payload.update({"phase": phase, "updated_at": datetime.now().isoformat(timespec="seconds")})
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        target.chmod(0o600)
    finally:
        try:
            Path(temp_name).unlink()
        except FileNotFoundError:
            pass
    return payload


def submit_may_have_occurred(state: dict[str, Any]) -> bool:
    return state.get("phase") in SUBMIT_CRITICAL | {"submitted", "unverified_after_submit"} or bool(
        state.get("submit_attempted")
    )
