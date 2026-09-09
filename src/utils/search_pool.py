"""Explicit, recoverable reset of Bobby's discovery/search pool."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application_tracker import ApplicationTracker


def reset_search_pool(
    encountered_path: Path,
    *,
    tracker: ApplicationTracker | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Reset only discovery suppression, retaining an auditable backup/ledger."""

    if not confirmed:
        raise PermissionError("Reset Search Pool requires explicit operator confirmation")
    path = Path(encountered_path)
    backup = None
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.stem}.before-reset-{stamp}{path.suffix}")
        shutil.copy2(path, backup)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text("[]\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    result = {"encountered_reset": True, "backup": str(backup) if backup else None}
    if tracker is not None:
        result.update(tracker.reset_discovery())
    return result

