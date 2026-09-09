"""Load the canonical candidate application profile safely.

The repository-level ``candidate_profile.yaml`` is the source of truth for
administrative application facts.  The older resume-directory profile is kept
as a compatibility fallback for installations that have not migrated yet, but
it must never override the canonical file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_PROFILE_PATH = REPOSITORY_ROOT / "candidate_profile.yaml"
LEGACY_APPLICATION_PROFILE_PATH = REPOSITORY_ROOT / "data" / "resumes" / "application_profile.yaml"


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Candidate profile could not be loaded: {path}") from exc
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"Candidate profile must contain a mapping: {path}")
    return dict(value)


def load_candidate_profile(path: Path | None = None) -> dict[str, Any]:
    """Return the canonical profile, with a legacy fallback for old installs."""
    selected = path or CANDIDATE_PROFILE_PATH
    if selected.is_file():
        return _read_mapping(selected)
    if selected == CANDIDATE_PROFILE_PATH and LEGACY_APPLICATION_PROFILE_PATH.is_file():
        return _read_mapping(LEGACY_APPLICATION_PROFILE_PATH)
    return {}


def profile_name(profile: Mapping[str, Any]) -> str:
    """Return the configured candidate name without exposing other profile data."""
    candidate = profile.get("candidate") or {}
    if not isinstance(candidate, Mapping):
        return ""
    return str(
        candidate.get("full_name")
        or candidate.get("legal_name")
        or " ".join(
            part
            for part in (candidate.get("first_name"), candidate.get("last_name"))
            if part
        )
    ).strip()


def structured_application_facts(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Select administrative facts for an application agent.

    This intentionally omits professional history and technical claims.  The
    resume remains authoritative for those fields; the selected sections are
    passed as structured data only when an application agent needs them.
    """
    allowed_sections = (
        "candidate",
        "address",
        "communication",
        "work_authorization",
        "location_preferences",
        "work_arrangement",
        "employment_type",
        "availability",
        "compensation",
        "transportation",
        "remote_work_setup",
        "business_travel",
        "screening",
        "references",
        "common_answers",
        "voluntary_self_identification",
        "accommodation",
        "legal",
        "answer_guidance",
    )
    return {
        section: profile[section]
        for section in allowed_sections
        if section in profile
    }
