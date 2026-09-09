"""One-time, atomic migration of legacy LinkedIn job IDs."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

MIGRATION_VERSION = 1
JOB_ID_PATTERN = re.compile(r"(?:linkedin\.com/jobs/view/|/jobs/view/)(\d+)")


def _load_ids(path: Path) -> set[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    values = value.keys() if isinstance(value, dict) else value if isinstance(value, list) else []
    return {str(item) for item in values if str(item).isdigit()}


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


def migrate_legacy_encountered_jobs(output_root: Path, encountered_path: Path) -> set[str]:
    marker = output_root / ".encountered_jobs_migration.json"
    ids = _load_ids(encountered_path)
    try:
        marker_value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        marker_value = {}
    if marker_value.get("version") == MIGRATION_VERSION:
        return ids

    for old_file in output_root.rglob("*"):
        if not old_file.is_file() or old_file in {encountered_path, marker}:
            continue
        if old_file.suffix.lower() not in {".yaml", ".yml", ".json", ".jsonl", ".txt"}:
            continue
        try:
            text = old_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        ids.update(match.group(1) for match in JOB_ID_PATTERN.finditer(text))

    _atomic_json(encountered_path, sorted(ids))
    _atomic_json(marker, {"version": MIGRATION_VERSION, "migrated_ids": len(ids)})
    return ids
