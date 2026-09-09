"""Normalize application preferences separately from resume biography."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from config.logger_config import logger


def _collect_key(value: Any, key: str) -> list[bool]:
    found: list[bool] = []
    if isinstance(value, dict):
        for item_key, item in value.items():
            if item_key == key and isinstance(item, bool):
                found.append(item)
            found.extend(_collect_key(item, key))
    elif isinstance(value, list):
        for item in value:
            found.extend(_collect_key(item, key))
    return found


def authoritative_relocation_preference(
    profile_path: Path, resume_structured: dict[str, Any] | None = None,
) -> bool | None:
    try:
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        profile = {}
    profile_values = _collect_key(profile, "willing_to_relocate")
    if len(set(profile_values)) > 1:
        raise ValueError("Contradictory willing_to_relocate values in application profile")
    preference = profile_values[0] if profile_values else None
    resume_values = _collect_key(resume_structured or {}, "open_to_relocation")
    if preference is not None and any(value != preference for value in resume_values):
        logger.warning(
            "Relocation preference differs from resume biography; "
            "candidate_profile.yaml is authoritative"
        )
    return preference


def normalize_relocation_prompt(
    resume_text: str, profile_path: Path, resume_structured: dict[str, Any] | None = None,
) -> str:
    preference = authoritative_relocation_preference(profile_path, resume_structured)
    if preference is None:
        return resume_text
    cleaned = re.sub(
        r"(?im)^\s*(?:open_to_relocation|willing_to_relocate|open to relocation)\s*:\s*.*$",
        "",
        resume_text,
    ).strip()
    value = "Yes" if preference else "No"
    return (
        f"AUTHORITATIVE APPLICATION PREFERENCE\n"
        f"Willing to relocate: {value}\n"
        "This application-profile preference overrides relocation fields in resume-derived data.\n\n"
        f"{cleaned}"
    )
