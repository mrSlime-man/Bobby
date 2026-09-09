import yaml

from src.utils.candidate_profile import (
    load_candidate_profile,
    profile_name,
    structured_application_facts,
)


def test_canonical_profile_wins_over_legacy_fallback(tmp_path, monkeypatch):
    canonical = tmp_path / "candidate_profile.yaml"
    legacy = tmp_path / "application_profile.yaml"
    canonical.write_text(
        yaml.safe_dump({"candidate": {"legal_name": "Canonical Person"}, "common_answers": {"x": True}}),
        encoding="utf-8",
    )
    legacy.write_text(
        yaml.safe_dump({"candidate": {"full_name": "Legacy Person"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.utils.candidate_profile.LEGACY_APPLICATION_PROFILE_PATH", legacy
    )
    assert profile_name(load_candidate_profile(canonical)) == "Canonical Person"
    assert load_candidate_profile(canonical)["common_answers"]["x"] is True


def test_profile_name_supports_legal_name_and_structured_facts_omit_resume_claims(tmp_path):
    profile = {
        "candidate": {"legal_name": "Canonical Person", "email": "person@example.test"},
        "work_authorization": {"authorized_to_work_in_us": True},
        "professional_experience": {"known_experience_areas": ["support"]},
    }
    path = tmp_path / "candidate_profile.yaml"
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    loaded = load_candidate_profile(path)
    facts = structured_application_facts(loaded)
    assert profile_name(loaded) == "Canonical Person"
    assert "work_authorization" in facts
    assert "professional_experience" not in facts
