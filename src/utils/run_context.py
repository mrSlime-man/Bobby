"""Stable production run identity shared by Bobby and child workers."""

from __future__ import annotations

import os
import uuid

RUN_ID_ENV = "BOBBY_RUN_ID"


def ensure_run_id() -> str:
    run_id = str(
        os.environ.get(RUN_ID_ENV, "") or os.environ.get("DASHBOARD_RUN_ID", "")
    ).strip()
    if not run_id:
        run_id = f"run-{uuid.uuid4()}"
    os.environ[RUN_ID_ENV] = run_id
    return run_id


def get_run_id() -> str:
    return ensure_run_id()
