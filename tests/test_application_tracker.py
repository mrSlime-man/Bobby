from datetime import datetime, timezone, timedelta

from src.application_tracker import (
    ApplicationTracker,
    canonical_safe_url,
    is_grey_lifecycle,
    lifecycle_state,
    quality_label,
    quality_tier,
    safe_tracker_event,
)


def _tracker(tmp_path):
    return ApplicationTracker(tmp_path / "applications.json")


def _job_event(kind, *, timestamp="2026-09-01T12:00:00+00:00", **payload):
    return {"type": kind, "timestamp": timestamp, "payload": payload}


def test_quality_tiers_and_contractual_boundaries():
    assert quality_tier(None) == "NEUTRAL"
    assert quality_tier(49.99) == "NEUTRAL"
    assert quality_tier(50) == "RED"
    assert quality_tier(59.99) == "RED"
    assert quality_tier(60) == "YELLOW"
    assert quality_tier(79.99) == "YELLOW"
    assert quality_tier(80) == "GREEN"
    assert quality_tier(89.99) == "GREEN"
    assert quality_tier(90) == "CYAN"
    assert quality_tier(100) == "CYAN"
    assert quality_label(91).startswith("● 90–100")


def test_urls_strip_query_tokens_and_keep_linkedin_external_separate():
    assert canonical_safe_url("https://jobs.example.test/app?token=secret#review") == "https://jobs.example.test/app"
    assert canonical_safe_url("https://user:pass@jobs.example.test/app") == ""
    tracker = _tracker(__import__("pathlib").Path("/tmp") / "bobby-tracker-test")
    record = tracker.apply_event(
        _job_event(
            "job_discovered",
            job_title="Support Technician",
            company_name="Acme",
            url="https://www.linkedin.com/jobs/view/123?trk=secret",
            external_url="https://jobs.acme.test/apply/123?token=secret",
        )
    )
    assert record["linkedin_url"] == "https://www.linkedin.com/jobs/view/123"
    assert record["external_url"] == "https://jobs.acme.test/apply/123"


def test_runtime_only_opaque_events_do_not_create_unknown_application_rows(tmp_path):
    tracker = _tracker(tmp_path)

    assert tracker.apply_event(
        _job_event(
            "job_disposition",
            job_key="job:opaque-runtime-digest",
            disposition="JOB_DISCOVERED_NEW",
        )
    ) == {}
    assert tracker.apply_event(
        _job_event("cycle_job_stats", found=1, encountered=1, new=1)
    ) == {}

    assert tracker.records() == []


def test_submitted_stale_and_rejected_lifecycle_states(tmp_path):
    tracker = _tracker(tmp_path)
    tracker.apply_event(
        _job_event(
            "job_result",
            timestamp="2026-09-01T12:00:00+00:00",
            job_id="job-1",
            job_title="Engineer",
            company_name="Acme",
            score=91,
            classification="SUBMITTED",
            url="https://jobs.acme.test/1",
        )
    )
    row = tracker.records(now="2026-09-07T12:00:00+00:00")[0]
    assert row["quality_tier"] == "CYAN"
    assert row["lifecycle_state"] == "STALE_NO_RESPONSE"
    assert is_grey_lifecycle(row, now="2026-09-06T12:00:00+00:00") is False
    tracker.apply_event(
        _job_event(
            "gmail_status_update",
            timestamp="2026-09-08T12:00:00+00:00",
            job_key=row["job_key"],
            employer_response="INTERVIEW_REQUESTED",
            message_id="message-1",
            confidence=0.95,
        )
    )
    assert tracker.records(now="2026-09-10T12:00:00+00:00")[0]["lifecycle_state"] == "ACTIVE"
    tracker.apply_event(
        _job_event(
            "gmail_status_update",
            timestamp="2026-09-11T12:00:00+00:00",
            job_key=row["job_key"],
            employer_response="REJECTED",
            message_id="message-2",
            confidence=0.94,
        )
    )
    rejected = tracker.records(now="2026-09-11T12:00:00+00:00")[0]
    assert rejected["lifecycle_state"] == "REJECTED_GREY"
    assert rejected["quality_tier"] == "CYAN"


def test_skipped_and_technical_failure_never_start_no_response_timer(tmp_path):
    tracker = _tracker(tmp_path)
    for job_id, classification in (("skip", "SKIPPED"), ("fail", "TECHNICAL_FAILURE")):
        tracker.apply_event(
            _job_event(
                "job_result",
                timestamp="2026-08-01T12:00:00+00:00",
                job_id=job_id,
                job_title="Role",
                company_name="Acme",
                classification=classification,
                url=f"https://jobs.acme.test/{job_id}",
            )
        )
    assert all(row["lifecycle_state"] == "ACTIVE" for row in tracker.records(now="2026-09-01T12:00:00+00:00"))


def test_cancellation_is_not_labeled_as_failure(tmp_path):
    tracker = _tracker(tmp_path)
    row = tracker.apply_event(
        _job_event(
            "job_result",
            job_id="cancelled",
            job_title="Role",
            company_name="Acme",
            classification="CANCELLED",
            reason="Cancelled by graceful shutdown",
            url="https://jobs.acme.test/cancelled",
        )
    )

    assert row["application_status"] == "CANCELLED"
    assert row["failure_category"] == ""


def test_unverified_submission_uses_credible_timestamp(tmp_path):
    tracker = _tracker(tmp_path)
    tracker.apply_event(
        _job_event(
            "job_result",
            timestamp="2026-09-01T12:00:00+00:00",
            job_id="job-1",
            job_title="Role",
            company_name="Acme",
            classification="UNVERIFIED_AFTER_SUBMIT",
            url="https://jobs.acme.test/1",
        )
    )
    row = tracker.records(now="2026-09-07T12:00:00+00:00")[0]
    assert row["lifecycle_state"] == "STALE_NO_RESPONSE"
    assert row["duplicate_submit_protected"] is True


def test_reset_preserves_submission_ledger_and_allows_pre_submit_reconsideration(tmp_path):
    tracker = _tracker(tmp_path)
    for job_id, classification in (("skip", "SKIPPED"), ("fail", "TECHNICAL_FAILURE"), ("sent", "SUBMITTED"), ("uncertain", "UNVERIFIED_AFTER_SUBMIT")):
        tracker.apply_event(
            _job_event(
                "job_result",
                job_id=job_id,
                job_title=job_id,
                company_name="Acme",
                classification=classification,
                url=f"https://jobs.acme.test/{job_id}",
            )
        )
    result = tracker.reset_discovery()
    assert result == {"resettable": 2, "protected": 2}
    rows = {row["job_id"]: row for row in tracker.records()}
    assert rows["skip"]["application_status"] == "DISCOVERED"
    assert rows["fail"]["application_status"] == "DISCOVERED"
    assert rows["sent"]["duplicate_submit_protected"] is True
    assert rows["uncertain"]["duplicate_submit_protected"] is True
    assert tracker.path.read_text(encoding="utf-8").count("submission_ledger") == 1


def test_easy_apply_limit_is_explicitly_retryable_and_resettable(tmp_path):
    tracker = _tracker(tmp_path)
    record = tracker.apply_event(
        _job_event(
            "job_result",
            job_id="quota-job",
            job_title="Role",
            company_name="Acme",
            classification="DEFERRED_EASY_APPLY_LIMIT",
            result="deferred",
            reason="Easy Apply quota is blocked",
            url="https://www.linkedin.com/jobs/view/123",
        )
    )
    assert record["application_status"] == "DEFERRED_EASY_APPLY_LIMIT"
    assert record["duplicate_submit_protected"] is False
    assert tracker.reset_discovery() == {"resettable": 1, "protected": 0}
    assert tracker.records()[0]["application_status"] == "DISCOVERED"


def test_external_application_record_keeps_account_workflow_and_verification_metadata(tmp_path):
    tracker = _tracker(tmp_path)
    started = tracker.apply_event(
        _job_event(
            "agent_apply_started",
            run_id="run-application-metadata",
            application_id="app-123",
            job_id="123",
            job_title="Support Technician",
            company_name="Acme",
            linkedin_url="https://www.linkedin.com/jobs/view/123?trk=private",
            external_url="https://jobs.acme.test/apply/123?token=private",
            ats="workday",
            application_type="EXTERNAL_ATS",
            search_profile="remote",
            remote_state="REMOTE",
            account_required=True,
            account_created=True,
            account_email="candidate@example.invalid",
            credential_ref="atsacct-test",
            last_workflow_step="account_creation",
        )
    )
    assert started["application_id"] == "app-123"
    assert started["application_type"] == "EXTERNAL_ATS"
    assert started["account_created"] is True
    assert started["external_url"] == "https://jobs.acme.test/apply/123"
    tracker.apply_event(
        _job_event(
            "email_verification_confirmed",
            application_id="app-123",
            email_verification_state="VERIFIED",
            verification_event_id="gmail-event-1",
            email_verification_required=True,
        )
    )
    finished = tracker.apply_event(
        _job_event(
            "job_result",
            application_id="app-123",
            job_id="123",
            classification="SUBMITTED",
            result="success",
            external_url="https://jobs.acme.test/apply/123",
            ats="workday",
            application_type="EXTERNAL_ATS",
            last_workflow_step="confirmation",
        )
    )
    assert finished["application_status"] == "SUBMITTED"
    assert finished["email_verification_state"] == "VERIFIED"
    assert finished["credential_ref"] == "atsacct-test"
    assert finished["duplicate_submit_protected"] is True
    assert len(tracker.records()) == 1


def test_linkedin_and_external_alias_events_reconcile_to_one_row(tmp_path):
    tracker = _tracker(tmp_path)
    tracker.apply_event(
        _job_event(
            "agent_apply_started",
            job_id="456",
            external_url="https://jobs.acme.test/apply/456",
            job_title="Technician",
            company_name="Acme",
        )
    )
    tracker.apply_event(
        _job_event(
            "job_result",
            job_id="456",
            linkedin_url="https://www.linkedin.com/jobs/view/456",
            external_url="https://jobs.acme.test/apply/456",
            classification="TECHNICAL_FAILURE",
            reason="upload verification failed",
        )
    )
    rows = tracker.records()
    assert len(rows) == 1
    assert rows[0]["linkedin_url"].endswith("/456")
    assert rows[0]["external_url"].endswith("/456")
