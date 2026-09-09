import json


def test_failure_ledger_persists_redacted_structured_failures(tmp_path, monkeypatch):
    import src.utils.failure_ledger as ledger

    monkeypatch.setattr(ledger, "FAILURE_DIR", tmp_path)
    monkeypatch.setattr(ledger, "FAILURE_LEDGER_PATH", tmp_path / "failure_ledger.json")
    monkeypatch.setattr(ledger, "FAILURE_LEDGER_MARKDOWN_PATH", tmp_path / "FAILURE_LEDGER.md")
    event = {
        "type": "job_result",
        "run_id": "run-1",
        "timestamp": "2026-09-09T12:00:00+00:00",
        "payload": {
            "classification": "TECHNICAL_FAILURE",
            "application_id": "app-123",
            "job_title": "Support Technician",
            "company_name": "Acme",
            "url": "https://jobs.acme.test/apply/123?token=do-not-store",
            "ats": "workday",
            "reason": "EXTERNAL_RESUME_UPLOAD_FAILED: verification did not complete",
            "diagnostic_ref": "data/output/external_evidence/123/result.json",
        },
    }

    first = ledger.record_failure_event(event)
    second = ledger.record_failure_event({**event, "run_id": "run-2"})

    assert first["failure_category"] == "upload_failure"
    assert first["control_classification"] == "BOBBY_CONTROLLED"
    assert first["job_url"] == "https://jobs.acme.test/apply/123"
    assert second["recurrence_count"] == 2
    content = (tmp_path / "failure_ledger.json").read_text(encoding="utf-8")
    assert "do-not-store" not in content
    assert (tmp_path / "FAILURE_LEDGER.md").exists()
    assert len(json.loads(content)["failures"]) == 2


def test_external_security_handoff_is_not_counted_as_bobby_technical_failure():
    from src.utils.failure_ledger import _control_classification

    assert _control_classification(
        "NEEDS_HUMAN", "ATS requires multi-factor authentication"
    ) == "EXTERNAL_BLOCKER"


def test_easy_apply_unresolved_control_is_a_human_boundary():
    from src.utils.failure_ledger import _control_classification

    assert _control_classification(
        "NEEDS_HUMAN",
        "Application requires human verification or intervention",
        application_type="EASY_APPLY",
        workflow="EASY_APPLY",
        failure_category="workflow_transition_failure",
    ) == "HUMAN_BOUNDARY"
    assert _control_classification(
        "NEEDS_HUMAN",
        "NEEDS_HUMAN: EXTERNAL_BLOCKER: external application provider was unavailable",
    ) == "EXTERNAL_BLOCKER"


def test_failure_ledger_annotation_preserves_identity_and_updates_audit_fields(
    tmp_path, monkeypatch
):
    import src.utils.failure_ledger as ledger

    monkeypatch.setattr(ledger, "FAILURE_DIR", tmp_path)
    monkeypatch.setattr(ledger, "FAILURE_LEDGER_PATH", tmp_path / "failure_ledger.json")
    monkeypatch.setattr(ledger, "FAILURE_LEDGER_MARKDOWN_PATH", tmp_path / "FAILURE_LEDGER.md")
    entry = ledger.record_failure_event(
        {
            "type": "job_result",
            "run_id": "run-1",
            "timestamp": "2026-09-09T12:00:00+00:00",
            "payload": {
                "classification": "TECHNICAL_FAILURE",
                "application_id": "app-123",
                "company_name": "Acme",
                "job_title": "Support Technician",
                "reason": "same page state",
            },
        }
    )

    updated = ledger.annotate_failure(
        entry["failure_id"],
        root_cause="Account-created page needed a bounded application-entry recovery.",
        code_changed="src/llm/apply_agent.py",
        test_added="tests/test_apply_agent_post_submit.py",
        fix_status="fixed",
        production_validation_status="pending next live cycle",
    )

    assert updated["failure_id"] == entry["failure_id"]
    assert updated["root_cause"].startswith("Account-created page")
    assert updated["recurrence_count"] == 1
    assert "pending next live cycle" in (tmp_path / "FAILURE_LEDGER.md").read_text()
