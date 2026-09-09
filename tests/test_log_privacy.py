from loguru import logger

from src.utils.log_privacy import (
    load_sensitive_log_values,
    profile_sensitive_values,
    protect_log_record,
)
from src.utils.browser_use_logging import redact_browser_use_text


def test_local_candidate_and_secret_values_are_hidden_at_sink_boundary(tmp_path):
    (tmp_path / ".env").write_text(
        "llm_api_key=opaque-example-key\nlinkedin_password=opaque-example-password\n"
    )
    source = tmp_path / "data/resumes/structured_resume.yaml"
    source.parent.mkdir(parents=True)
    source.write_text(
        "personal_information:\n  first_name: Examplecandidate\n  address: 42 Example Street\n"
    )
    (tmp_path / "candidate_profile.yaml").write_text(
        "candidate:\n  email: profile-only@example.com\n  legal_name: Profile Candidate\n"
        "preferences:\n  application_answer: profile-only-answer\n",
        encoding="utf-8",
    )
    values = load_sensitive_log_values(tmp_path)
    record = {
        "message": (
            "opaque-example-key opaque-example-password Examplecandidate "
            "42 Example Street person@example.com profile-only@example.com "
            "Profile Candidate profile-only-answer"
        ),
        "exception": None,
    }
    protect_log_record(record, values)
    assert all(value not in record["message"] for value in values)
    assert "person@example.com" not in record["message"]


def test_profile_sensitive_values_cover_nested_candidate_facts_without_retaining_unknown():
    values = profile_sensitive_values(
        {
            "candidate": {"email": "candidate@example.invalid", "phone": "+15555550123"},
            "address": {"street": "42 Example Street"},
            "preferences": {"answer": "UNKNOWN", "salary": 52000},
        }
    )

    assert "candidate@example.invalid" in values
    assert "+15555550123" in values
    assert "42 Example Street" in values
    assert "52000" in values
    assert "UNKNOWN" not in values


def test_exception_payload_and_locals_never_reach_sink():
    output = []
    sink_id = logger.add(output.append, format="{message}", diagnose=True, backtrace=True)
    try:
        protected = logger.patch(
            lambda record: protect_log_record(record, ("unlisted-private-value",))
        )
        try:
            raise ValueError("unlisted-private-value")
        except ValueError:
            protected.exception("Application operation failed")
        assert "unlisted-private-value" not in str(output)
        assert "exception_type=ValueError" in str(output)
    finally:
        logger.remove(sink_id)


def test_external_answer_memory_is_disabled_and_private_artifacts_are_restricted(
    tmp_path, monkeypatch
):
    """External form answers are never persisted or replayed across employers."""

    import base64
    import src.llm.external_apply_support as support

    monkeypatch.setattr(support, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(support, "ANALYTICS_DIR", tmp_path / "analytics")
    monkeypatch.setattr(support, "MEMORY_DIR", tmp_path / "memory")

    support.save_answer("Email address", "candidate-private@example.invalid")
    assert not (tmp_path / "memory" / "answers.json").exists()
    assert "candidate-private@example.invalid" not in support.answer_memory_prompt()
    assert support.load_answer_memory() == {}

    support.save_evidence(
        job_id="123",
        result="UNVERIFIED_AFTER_SUBMIT",
        external_url="https://careers.example.invalid/apply",
        final_url="https://careers.example.invalid/confirmation",
        ats="generic",
    )
    support.save_screenshot_b64("123", base64.b64encode(b"test-png").decode("ascii"))

    result = tmp_path / "evidence" / "123" / "result.json"
    screenshot = tmp_path / "evidence" / "123" / "confirmation.png"
    analytics = tmp_path / "analytics" / "external_results.jsonl"
    assert (tmp_path / "evidence").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "evidence" / "123").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "analytics").stat().st_mode & 0o777 == 0o700
    assert result.stat().st_mode & 0o777 == 0o600
    assert screenshot.stat().st_mode & 0o777 == 0o600
    assert analytics.stat().st_mode & 0o777 == 0o600


def test_browser_worker_error_redacts_an_unlabelled_environment_secret():
    secret = "opaque-worker-environment-secret"
    message = redact_browser_use_text(ValueError(secret), (secret,))

    assert secret not in message
