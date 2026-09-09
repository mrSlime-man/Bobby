import json
from pathlib import Path

import pytest
from unittest.mock import AsyncMock

from src.job_manager.linkedin.easy_applier_linkedin import LinkedInEasyApplier
from src.llm.apply_agent import classify_worker_result, should_poll_for_receipt
from src.llm.llm_manager import GPTAnswerer
from src.llm.external_worker_state import (
    read_worker_state,
    submit_may_have_occurred,
    write_worker_state,
)
from src.llm.provider_health import classify_provider_error, circuit_is_open, open_circuit
from src.utils.candidate_preferences import (
    authoritative_relocation_preference,
    normalize_relocation_prompt,
)
from src.utils.encountered_jobs import migrate_legacy_encountered_jobs
from src.utils.redaction import REDACTED, redact_text, redact_urls, sanitize_value
from src.utils.browser_use_logging import BrowserUseRedactionFilter
from src.utils.run_context import ensure_run_id


@pytest.mark.parametrize(
    ("return_code", "phase", "submitted", "expected"),
    [
        (1, "technical_failure", False, "TECHNICAL_FAILURE"),
        (130, "cancelled_before_submit", False, "Cancelled"),
        (10, "unverified_after_submit", True, "UNVERIFIED_AFTER_SUBMIT"),
        (-9, "submit_attempted", True, "UNVERIFIED_AFTER_SUBMIT"),
        (0, "submitted", True, "Success"),
    ],
)
def test_durable_submit_state_controls_worker_classification(
    return_code, phase, submitted, expected
):
    result, reason = classify_worker_result(
        return_code, {"phase": phase, "submit_attempted": submitted}
    )
    assert expected in (result + reason)


def test_worker_state_is_atomic_and_submit_critical(tmp_path):
    path = tmp_path / "state.json"
    write_worker_state(path, "pre_submit", submit_attempted=False)
    assert not submit_may_have_occurred(read_worker_state(path))
    write_worker_state(path, "submit_click_started", submit_attempted=False)
    assert submit_may_have_occurred(read_worker_state(path))
    assert path.stat().st_mode & 0o777 == 0o600


def test_provider_errors_are_classified_without_retrying_daily_quota():
    assert classify_provider_error("429 quota GenerateRequestsPerDay") == "permanent_quota"
    assert classify_provider_error("429 RESOURCE_EXHAUSTED retry in 30s") == "rate_limit"
    assert classify_provider_error("503 UNAVAILABLE high demand") == "transient_unavailable"
    assert classify_provider_error("LLM call timed out") == "timeout"


def test_transient_provider_circuit_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setenv("BOBBY_RUN_ID", "provider-cooldown-test")
    monkeypatch.setattr("src.llm.provider_health.tempfile.gettempdir", lambda: str(tmp_path))
    open_circuit("gemini", "model", "rate_limit", permanent=False, cooldown_seconds=1)
    assert circuit_is_open("gemini")
    import time

    time.sleep(1.1)
    assert not circuit_is_open("gemini")


def test_provider_diagnostic_has_correlation_and_never_formats_raw_exception(monkeypatch):
    import io
    import logging
    from src.utils.browser_use_logging import install_browser_use_log_redaction

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    parent = logging.getLogger("browser_use")
    monkeypatch.setattr(parent, "handlers", [handler])
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    monkeypatch.setenv("BOBBY_RUN_ID", "run-private-log-test")
    install_browser_use_log_redaction(["private-candidate"])
    record = logging.LogRecord(
        "browser_use",
        logging.ERROR,
        __file__,
        1,
        "503 high demand for private-candidate",
        (),
        (ValueError, ValueError("raw-exception-secret"), None),
    )
    handler.handle(record)
    output = stream.getvalue()
    assert "run_id=run-private-log-test" in output
    assert "worker_pid=" in output
    assert "category=unavailable" in output
    assert "payload=redacted" in output
    assert "exception_type=ValueError" in output
    assert "private-candidate" not in output
    assert "raw-exception-secret" not in output
    assert output[:4].isdigit() and output[4] == "-"


def test_gmail_receipt_polling_requires_submit_evidence():
    assert not should_poll_for_receipt(submit_attempted=False)
    assert should_poll_for_receipt(submit_attempted=True)
    assert should_poll_for_receipt(submit_attempted=False, post_submit_signal=True)


def test_application_profile_is_authoritative_for_relocation(tmp_path, caplog):
    profile = tmp_path / "profile.yaml"
    profile.write_text("preferences:\n  willing_to_relocate: true\n", encoding="utf-8")
    structured = {"work_preferences": {"open_to_relocation": False}}
    assert authoritative_relocation_preference(profile, structured) is True
    prompt = normalize_relocation_prompt(
        "Name: Candidate\nopen_to_relocation: No\n", profile, structured
    )
    assert "Willing to relocate: Yes" in prompt
    assert "open_to_relocation: No" not in prompt


def test_conflicting_profile_preferences_fail_validation(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "a:\n  willing_to_relocate: true\nb:\n  willing_to_relocate: false\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Contradictory"):
        authoritative_relocation_preference(profile, {})


def test_encountered_migration_merges_ids_once_and_preserves_existing(tmp_path):
    output = tmp_path / "linkedin"
    output.mkdir()
    encountered = output / "encountered_jobs.json"
    encountered.write_text('["111"]', encoding="utf-8")
    legacy = output / "success.yaml"
    legacy.write_text("url: https://www.linkedin.com/jobs/view/222/\n", encoding="utf-8")
    assert migrate_legacy_encountered_jobs(output, encountered) == {"111", "222"}
    legacy.write_text("url: https://www.linkedin.com/jobs/view/333/\n", encoding="utf-8")
    assert migrate_legacy_encountered_jobs(output, encountered) == {"111", "222"}
    assert json.loads(encountered.read_text(encoding="utf-8")) == ["111", "222"]


def test_redaction_removes_candidate_and_secret_patterns():
    value = "Email person@example.com phone +1 (555) 222-1111 OTP: 123456 token=abc"
    redacted = redact_text(value)
    assert "person@example.com" not in redacted
    assert "555" not in redacted
    assert "123456" not in redacted
    assert sanitize_value("secret", "api_key") == REDACTED


def test_durable_mirror_redaction_removes_job_urls():
    url = "https://jobs.example.invalid/view/123?tracking=private"
    redacted_text = redact_urls(f"job={url}")
    assert "https://" not in redacted_text
    assert REDACTED in redacted_text
    assert sanitize_value(url, "url") == REDACTED
    assert sanitize_value({"nested": {"job_url": url}})["nested"]["job_url"] == REDACTED


def test_event_mirror_redacts_job_urls(monkeypatch, tmp_path):
    import src.dashboard.runtime as runtime

    dashboard = tmp_path / "dashboard"
    monkeypatch.setattr(runtime, "DASHBOARD_DIR", dashboard)
    monkeypatch.setattr(runtime, "SCREENSHOT_DIR", dashboard / "screenshots")
    monkeypatch.setattr(runtime, "EVENTS_FILE", dashboard / "events.jsonl")
    monkeypatch.setattr(runtime, "BOBBY_EVENTS_FILE", tmp_path / "bobby-events.jsonl")
    monkeypatch.setattr(runtime, "SNAPSHOT_FILE", dashboard / "snapshot.json")
    monkeypatch.setattr(runtime, "CONTROL_FILE", dashboard / "control.json")
    monkeypatch.setattr(runtime, "PROCESS_FILE", dashboard / "process.json")
    monkeypatch.setattr(runtime, "SCREENSHOT_INDEX_FILE", dashboard / "screenshots.json")
    monkeypatch.setattr(runtime, "BOT_STDOUT_FILE", tmp_path / "dashboard.log")

    url = "https://jobs.example.invalid/view/123"
    runtime.emit_event("job_loaded", url=url, job_title="Role", company_name="Company")

    mirror = (tmp_path / "bobby-events.jsonl").read_text(encoding="utf-8")
    assert url not in mirror
    assert REDACTED in mirror


def test_browser_use_log_filter_redacts_pii_and_form_answer():
    import logging

    record = logging.LogRecord(
        "browser_use.Agent",
        logging.INFO,
        __file__,
        1,
        "input text: private-answer, index: 3; person@example.com; +1 555 222 1111; 7 Main Street",
        (),
        None,
    )
    assert BrowserUseRedactionFilter(["7 Main Street"]).filter(record)
    rendered = record.getMessage()
    assert "private-answer" not in rendered
    assert "person@example.com" not in rendered
    assert "555" not in rendered
    assert "7 Main Street" not in rendered
    assert "index: 3" in rendered


def test_browser_use_provider_payload_error_is_dropped_wholesale():
    """An SDK error must not retain arbitrary model-visible response text."""
    import logging

    private_answer = "non-pattern-private-application-answer"
    record = logging.LogRecord(
        "browser_use.llm.example",
        logging.ERROR,
        __file__,
        1,
        "Failed to parse or validate response candidates=[Candidate("
        f"content=Content(parts=[Part(text={private_answer})]))]",
        (),
        None,
    )

    assert BrowserUseRedactionFilter().filter(record)
    assert record.getMessage() == (
        "Browser Use provider/operation error | " "category=malformed_response | payload=redacted"
    )
    assert private_answer not in record.getMessage()


@pytest.mark.asyncio
async def test_application_answer_values_are_not_logged(caplog):
    dropdown = AsyncMock()
    dropdown.select_option.return_value = None
    applier = object.__new__(LinkedInEasyApplier)
    await applier._select_dropdown_option(dropdown, "person@example.com")

    answerer = object.__new__(GPTAnswerer)
    answerer.resume_structured = {
        "personal_information": {"phone": "5552221111", "phone_code": "+1"}
    }
    assert answerer.answer_question_numeric("mobile phone", []) == "15552221111"
    assert "person@example.com" not in caplog.text
    assert "5552221111" not in caplog.text


def test_run_id_is_non_null_stable_and_inherited(monkeypatch):
    monkeypatch.delenv("BOBBY_RUN_ID", raising=False)
    monkeypatch.setenv("DASHBOARD_RUN_ID", "run-dashboard-test")
    assert ensure_run_id() == "run-dashboard-test"
    assert ensure_run_id() == "run-dashboard-test"
    assert __import__("os").environ["BOBBY_RUN_ID"] == "run-dashboard-test"
