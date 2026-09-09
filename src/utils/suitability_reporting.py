"""Allowlisted, PII-safe text for suitability and terminal status publication."""

import re
from typing import Any


def suitability_reason_category(reason: Any) -> str:
    """Map private model reasoning to a small non-sensitive category."""
    text = str(reason or "").lower()
    category_markers = (
        ("authorization_mismatch", ("work authorization", "citizenship", "clearance", "sponsor")),
        ("location_mismatch", ("location", "onsite", "on-site", "remote", "relocat")),
        ("experience_mismatch", ("experience", "years", "senior", "tenure")),
        ("education_mismatch", ("education", "degree", "diploma", "university", "college")),
        ("certification_mismatch", ("certification", "certified", "comptia")),
        ("skills_mismatch", ("skill", "technology", "technical", "proficiency")),
        ("role_mismatch", ("role", "sales", "management", "position", "title")),
        ("compensation_mismatch", ("salary", "compensation", "pay", "hourly")),
    )
    for category, markers in category_markers:
        if any(marker in text for marker in markers):
            return category
    return "requirements_mismatch"


def safe_public_reason(
    reason: Any,
    classification: str,
    *,
    suitability_score: Any = None,
) -> str:
    """Return public text composed only from fixed strings and safe scalars."""
    status_messages = {
        "SUBMITTED": "Independent submission confirmation received",
        "UNVERIFIED_AFTER_SUBMIT": "Final submit was attempted but independent confirmation was not found",
        "TECHNICAL_FAILURE": "Application flow ended because of a technical failure",
        "CANCELLED": "Cancelled by graceful shutdown",
        "NEEDS_HUMAN": "Application requires human verification or intervention",
        "NOT_ELIGIBLE": "Application requirements were not met",
        "LIMIT": "Configured application limit reached",
        "DEFERRED_EASY_APPLY_LIMIT": "Easy Apply limit reached; deferred for a later quota recheck",
    }
    if classification in status_messages:
        return status_messages[classification]

    if suitability_score is not None:
        try:
            score = max(0, min(100, int(suitability_score)))
        except (TypeError, ValueError):
            score = None
        category = suitability_reason_category(reason)
        if score is not None:
            return f"Suitability score {score}; category: {category}"
        return f"Suitability category: {category}"

    already_safe = re.fullmatch(
        r"Suitability score ([0-9]{1,3}); category: "
        r"(authorization_mismatch|location_mismatch|experience_mismatch|"
        r"education_mismatch|certification_mismatch|skills_mismatch|"
        r"role_mismatch|compensation_mismatch|requirements_mismatch)",
        str(reason or ""),
    )
    if classification == "SKIPPED" and already_safe:
        score = max(0, min(100, int(already_safe.group(1))))
        return f"Suitability score {score}; category: {already_safe.group(2)}"

    # Arbitrary failure/skip explanations can contain candidate data or raw
    # model reasoning. Public channels receive a fixed, allowlisted fallback.
    if classification == "SKIPPED":
        return "Skipped before submission"
    return "Final application outcome recorded"
