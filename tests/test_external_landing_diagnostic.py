from src.llm import external_apply_support as support
from src.llm.ats_engine import ATSStage


def test_landing_diagnostic_is_private_and_replayable_for_hidden_controls(tmp_path, monkeypatch):
    """A terminal landing artifact retains resolver metadata without form values."""

    query_token = "query-token-sentinel"
    candidate_email = "candidate-private@example.invalid"
    candidate_answer = "candidate-answer-sentinel"
    external_url = f"https://careers.example.invalid/jobs/opaque?token={query_token}#ignored"

    monkeypatch.setattr(support, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(support, "get_run_id", lambda: "run-landing-diagnostic")
    monkeypatch.setattr(
        support,
        "load_sensitive_log_values",
        lambda _root: (query_token, candidate_email, candidate_answer),
    )

    job_id = support.safe_job_id(external_url)
    artifact_path = support.save_landing_diagnostic(
        job_id=job_id,
        worker_id="worker-1",
        reason="same_page_state",
        url=external_url,
        title=f"Application for {candidate_email}",
        body=f"Hidden field value: {candidate_answer}",
        ats="generic",
        stage=ATSStage.LANDING,
        controls=[
            {
                "label": f"Apply for {candidate_email}",
                "aria_label": "Apply now",
                "title": f"Token {query_token}",
                "kind": "button",
                "role": "button",
                "href": external_url,
                "visible": False,
                "value": candidate_answer,
                "name": "candidate_answer",
                "id": "candidate-private-input",
            }
        ],
        form_fields=[
            {
                "label": "Candidate email",
                "aria_label": "",
                "placeholder": candidate_email,
                "kind": "INPUT",
                "input_type": "email",
                "required": True,
                "enabled": True,
                "read_only": False,
                "has_value": True,
                "invalid": False,
                "option_count": 0,
                "value": candidate_email,
                "name": "candidate-private-input",
                "id": "candidate-private-input",
            },
            {
                "label": "Voluntary disclosure",
                "kind": "CHOICE_GROUP",
                "input_type": "radio",
                "required": True,
                "enabled": True,
                "read_only": False,
                "has_value": True,
                "invalid": False,
                "option_count": 3,
                "selected_label": candidate_answer,
            },
        ],
        tabs=[{"selected": True, "url": external_url}],
        frame_count=2,
        editable_field_count=3,
    )

    payload = support.load_landing_diagnostic(artifact_path)
    replay = support.replay_application_entry_diagnostic(payload)
    artifact_text = artifact_path.read_text(encoding="utf-8")

    assert query_token not in job_id
    assert query_token not in str(artifact_path)
    assert all(secret not in artifact_text for secret in (query_token, candidate_email, candidate_answer))
    assert artifact_path.stat().st_mode & 0o777 == 0o600

    assert payload["raw_control_count"] == 1
    assert payload["visible_control_count"] == 0
    assert payload["frame_count"] == 2
    assert payload["editable_field_count"] == 3
    assert payload["tab_count"] == 1
    assert payload["controls"] == [
        {
            "label": "Apply for [REDACTED]",
            "aria_label": "Apply now",
            "title": "Token [REDACTED]",
            "kind": "button",
            "role": "button",
            "href": {"origin": "https://careers.example.invalid", "path": "/jobs/opaque"},
            "visible": False,
        }
    ]
    assert payload["tabs"] == [
        {
            "selected": True,
            "url": {"origin": "https://careers.example.invalid", "path": "/jobs/opaque"},
        }
    ]
    assert payload["form_fields"] == [
        {
            "label": "Candidate email",
            "aria_label": "",
            "placeholder": "[REDACTED]",
            "kind": "INPUT",
            "input_type": "email",
            "required": True,
            "enabled": True,
            "read_only": False,
            "has_value": True,
            "invalid": False,
            "option_count": 0,
        },
        {
            "label": "Voluntary disclosure",
            "aria_label": "",
            "placeholder": "",
            "kind": "CHOICE_GROUP",
            "input_type": "radio",
            "required": True,
            "enabled": True,
            "read_only": False,
            "has_value": True,
            "invalid": False,
            "option_count": 3,
        },
    ]
    assert replay["candidate_count"] == 0
    assert replay["candidates"] == []
    assert replay["selected"] == ""


def test_readiness_diagnostic_allowlist_excludes_values_and_secret_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "EVIDENCE_DIR", tmp_path)
    monkeypatch.setattr(support, "get_run_id", lambda: "run-probe")
    monkeypatch.setattr(support, "load_sensitive_log_values", lambda _: ("private-name",))
    path = support.save_landing_diagnostic(
        job_id="synthetic", worker_id="probe", reason="empty_document",
        url="about:blank", title="Starting agent", body="", ats="workday",
        stage=ATSStage.LANDING, controls=[],
        readiness={
            "url": "https://careers.example.invalid/jobs/test?token=secret-query#secret-fragment",
            "title": "Application private-name",
            "page_identity": "1234567890abcdef", "ready_state": "loading",
            "body_exists": True, "body_length": 0, "dom_element_count": 3,
            "raw_control_count": 0, "visible_control_count": 0, "frame_count": 0,
            "page_count": 1, "active_page_index": 0, "page_closed": False,
            "target_changed": False, "url_changed": True,
            "observation_count": 3, "elapsed_ms": 30, "meaningful": False,
            "body": "private-body", "value": "private-value", "cookies": "private-cookie",
            "error_class": "unsafe-provider-secret", "unknown": "private-unknown",
        },
    )
    text = path.read_text()
    for secret in ("secret-query", "secret-fragment", "private-name", "private-body",
                   "private-value", "private-cookie", "unsafe-provider-secret", "private-unknown"):
        assert secret not in text
    readiness = support.load_landing_diagnostic(path)["readiness"]
    assert readiness["url"] == {"origin": "https://careers.example.invalid", "path": "/jobs/test"}
    assert readiness["title"] == "Application [REDACTED]"
    assert readiness["body_length"] == 0
    assert readiness["ready_state"] == "loading"
    assert readiness["page_identity"] == "1234567890abcdef"
    assert path.stat().st_mode & 0o777 == 0o600
