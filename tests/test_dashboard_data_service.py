from pathlib import Path

import yaml

from src.dashboard import data_service


def _write_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        yaml.safe_dump(data, file, sort_keys=False)


def test_get_summary_aggregates_dashboard_outputs(monkeypatch, tmp_path):
    output_dir = tmp_path / "data" / "output"
    logs_dir = tmp_path / "logs"

    _write_yaml(
        output_dir / "success.yaml",
        {"Acme": [{"job_title": "CTO", "url": "https://linkedin.com/jobs/view/1"}]},
    )
    _write_yaml(
        output_dir / "skipped.yaml",
        {
            "Beta": [
                {
                    "job_title": "VP Engineering",
                    "url": "https://linkedin.com/jobs/view/2",
                    "skip_reason": "Not interesting",
                    "llm_time_seconds": 3.2,
                    "executed_at": "2026-04-15T10:03:00",
                }
            ]
        },
    )
    _write_yaml(
        output_dir / "failed.yaml",
        {
            "Gamma": [
                {
                    "job_title": "Head of Engineering",
                    "url": "https://linkedin.com/jobs/view/3",
                    "skip_reason": "Playwright error",
                }
            ]
        },
    )
    _write_yaml(
        output_dir / "interesting_jobs.yaml",
        [
            {
                "job_title": "Chief Architect",
                "company_name": "Delta",
                "url": "https://linkedin.com/jobs/view/4",
                "interest_score": 90,
                "llm_time_seconds": 1.5,
                "executed_at": "2026-04-15T10:04:00",
            }
        ],
    )
    _write_yaml(
        output_dir / "last_run.yaml",
        {
            "last_run": "2026-04-15T10:00:00",
            "last_apply": "2026-04-15T10:30:00",
            "success_applies_num": 2,
            "total_applies_num": 7,
        },
    )

    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "llm_api_calls.yaml").write_text(
        "---\n"
        "model_name: model-a\n"
        "response_time_seconds: 1.25\n"
        "total_tokens: 100\n"
        "total_cost: 0.1\n\n"
        "---\n"
        "model_name: model-b\n"
        "response_time_seconds: 2.75\n"
        "total_tokens: 250\n"
        "total_cost: 0.25\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(data_service, "SUCCESS_FILE", output_dir / "success.yaml")
    monkeypatch.setattr(data_service, "SKIPPED_FILE", output_dir / "skipped.yaml")
    monkeypatch.setattr(data_service, "FAILED_FILE", output_dir / "failed.yaml")
    monkeypatch.setattr(data_service, "INTERESTING_FILE", output_dir / "interesting_jobs.yaml")
    monkeypatch.setattr(data_service, "LLM_CALLS_FILE", logs_dir / "llm_api_calls.yaml")
    monkeypatch.setattr(data_service, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(
        data_service,
        "get_snapshot",
        lambda: {
            "run_status": "running",
            "run_id": "run-1",
            "paused": False,
            "started_at": "2026-04-15T09:55:00",
            "finished_at": None,
            "last_event_at": "2026-04-15T10:05:00",
            "page_num": 2,
            "current_job": {
                "title": "Platform Director",
                "company": "Epsilon",
                "url": "https://linkedin.com/jobs/view/5",
                "stage": "applying",
                "llm_time_seconds": 4.0,
                "executed_at": "2026-04-15T10:05:00",
            },
            "counters": {
                "discovered": 15,
                "evaluated": 6,
                "interesting": 3,
                "applied": 1,
                "skipped": 2,
                "failed": 1,
            },
        },
    )
    monkeypatch.setattr(data_service, "get_process_info", lambda: {"pid": 1234})
    monkeypatch.setattr(data_service, "sync_process_state", lambda: {})
    monkeypatch.setattr(
        data_service,
        "get_control_state",
        lambda: {"pause_requested": False, "stop_requested": False},
    )

    summary = data_service.get_summary()

    assert summary["run"]["status"] == "running"
    assert summary["totals"]["applied"] == 1
    assert summary["totals"]["skipped"] == 1
    assert summary["totals"]["failed"] == 1
    assert summary["totals"]["interesting"] == 1
    assert summary["totals"]["discovered_live"] == 15
    assert summary["totals"]["llm_calls"] == 2
    assert summary["totals"]["llm_total_tokens"] == 350
    assert summary["totals"]["llm_total_cost"] == 0.35
    assert summary["totals"]["llm_total_time_seconds"] == 4.0


def test_update_search_config_normalizes_24_hours(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(data_service, "ROOT_DIR", tmp_path)

    result = data_service.update_search_config(
        {
            "positions": ["CTO"],
            "remote": True,
            "hybrid": False,
            "onsite": False,
            "experience_level": {"executive": True},
            "job_types": {"full_time": True},
            "date": {"24_hours": True},
            "locations": ["Dubai"],
            "apply_once_at_company": True,
            "company_blacklist": [],
            "title_blacklist": [],
            "location_blacklist": [],
        }
    )

    file_content = (config_dir / "search_config.yaml").read_text(encoding="utf-8")
    assert result["date"]["24_hours"] is True
    assert "24_hours: true" in file_content
    assert "day_24_hours" not in file_content


def test_get_run_history_builds_runs_from_events(monkeypatch):
    monkeypatch.setattr(
        data_service,
        "read_events",
        lambda limit=5000: [
            {
                "run_id": "run-1",
                "timestamp": "2026-04-15T10:00:00",
                "type": "run_started",
                "message": "Started",
                "payload": {},
            },
            {
                "run_id": "run-1",
                "timestamp": "2026-04-15T10:01:00",
                "type": "jobs_discovered",
                "message": "Discovered jobs",
                "payload": {"count": 8, "total_discovered": 8},
            },
            {
                "run_id": "run-1",
                "timestamp": "2026-04-15T10:02:00",
                "type": "job_evaluated",
                "message": "Interesting job",
                "payload": {"interesting": True},
            },
            {
                "run_id": "run-1",
                "timestamp": "2026-04-15T10:03:00",
                "type": "job_result",
                "message": "Applied",
                "payload": {"result": "Success"},
            },
            {
                "run_id": "run-1",
                "timestamp": "2026-04-15T10:05:00",
                "type": "run_completed",
                "message": "Completed",
                "payload": {},
            },
        ],
    )
    monkeypatch.setattr(data_service, "sync_process_state", lambda: {})
    monkeypatch.setattr(
        data_service,
        "get_snapshot",
        lambda: {"run_id": None, "run_status": "idle", "counters": {}},
    )

    history = data_service.get_run_history()

    assert len(history) == 1
    assert history[0]["run_id"] == "run-1"
    assert history[0]["status"] == "completed"
    assert history[0]["jobs"]["discovered"] == 8
    assert history[0]["jobs"]["interesting"] == 1
    assert history[0]["jobs"]["applied"] == 1


def test_get_run_events_filters_by_run(monkeypatch):
    monkeypatch.setattr(
        data_service,
        "read_events_for_run",
        lambda run_id, limit=200: [
            {"run_id": run_id, "type": "run_started"},
            {"run_id": run_id, "type": "run_completed"},
        ][:limit],
    )

    events = data_service.get_run_events("run-77", limit=10)

    assert len(events) == 2
    assert all(event["run_id"] == "run-77" for event in events)


def test_get_run_jobs_ignores_runtime_only_events(monkeypatch):
    monkeypatch.setattr(
        data_service,
        "read_events_for_run",
        lambda run_id, limit=5000: [
            {"run_id": run_id, "type": "run_started", "payload": {}},
            {
                "run_id": run_id,
                "type": "jobs_discovered",
                "payload": {"total_discovered": 13},
            },
            {
                "run_id": run_id,
                "type": "job_disposition",
                "payload": {"job_key": "job:opaque-runtime-digest"},
            },
            {
                "run_id": run_id,
                "type": "cycle_job_stats",
                "payload": {"found": 13, "new": 0},
            },
            {"run_id": run_id, "type": "run_completed", "payload": {}},
        ][:limit],
    )

    assert data_service.get_run_jobs("run-runtime-only") == []


def test_get_run_jobs_builds_status_from_events(monkeypatch):
    monkeypatch.setattr(
        data_service,
        "read_events_for_run",
        lambda run_id, limit=5000: [
            {
                "run_id": run_id,
                "type": "job_loaded",
                "message": "Loaded job CTO",
                "timestamp": "2026-04-15T10:00:00",
                "payload": {
                    "job_title": "CTO",
                    "company_name": "Acme",
                    "url": "https://linkedin.com/jobs/view/1",
                    "stage": "loaded",
                },
            },
            {
                "run_id": run_id,
                "type": "llm_call_completed",
                "message": "LLM call completed for CTO",
                "timestamp": "2026-04-15T10:00:30",
                "payload": {
                    "job_title": "CTO",
                    "company_name": "Acme",
                    "url": "https://linkedin.com/jobs/view/1",
                    "response_time_seconds": 1.5,
                    "total_job_llm_time_seconds": 1.5,
                },
            },
            {
                "run_id": run_id,
                "type": "job_evaluated",
                "message": "Evaluated CTO",
                "timestamp": "2026-04-15T10:01:00",
                "payload": {
                    "job_title": "CTO",
                    "company_name": "Acme",
                    "url": "https://linkedin.com/jobs/view/1",
                    "interesting": True,
                    "score": 92,
                    "reasoning": "Strong match",
                    "llm_time_seconds": 1.5,
                },
            },
            {
                "run_id": run_id,
                "type": "job_result",
                "message": "Job finished with status Success",
                "timestamp": "2026-04-15T10:02:00",
                "payload": {
                    "job_title": "CTO",
                    "company_name": "Acme",
                    "url": "https://linkedin.com/jobs/view/1",
                    "result": "Success",
                    "reason": "",
                    "llm_time_seconds": 1.5,
                    "submitted_resume_path": "/tmp/resumes/cto.pdf",
                },
            },
            {
                "run_id": run_id,
                "type": "job_loaded",
                "message": "Loaded job VP Engineering",
                "timestamp": "2026-04-15T10:03:00",
                "payload": {
                    "job_title": "VP Engineering",
                    "company_name": "Beta",
                    "url": "https://linkedin.com/jobs/view/2",
                    "stage": "loaded",
                },
            },
            {
                "run_id": run_id,
                "type": "job_result",
                "message": "Job finished with status Skip",
                "timestamp": "2026-04-15T10:04:00",
                "payload": {
                    "job_title": "VP Engineering",
                    "company_name": "Beta",
                    "url": "https://linkedin.com/jobs/view/2",
                    "result": "Skip",
                    "reason": "Vacancy is not interesting",
                },
            },
        ][:limit],
    )

    jobs = data_service.get_run_jobs("run-55")

    assert len(jobs) == 2
    assert any(job["status"] == "applied" and job["interest_score"] == 92 for job in jobs)
    assert any(
        job["status"] == "applied"
        and job["submitted_resume_path"] == "/tmp/resumes/cto.pdf"
        for job in jobs
    )
    assert any(
        job["status"] == "skipped" and job["skip_reason"] == "Vacancy is not interesting"
        for job in jobs
    )


def test_get_run_jobs_enriches_missing_fields_from_saved_outputs(monkeypatch, tmp_path):
    output_dir = tmp_path / "data" / "output"
    _write_yaml(
        output_dir / "success.yaml",
        {
            "1inch": [
                {
                    "company_name": "1inch",
                    "job_title": "Chief Product Officer",
                    "url": "https://www.linkedin.com/jobs/view/4401460753/",
                    "interest_score": 75,
                    "interest_reason": "Strong fintech and Web3 alignment",
                    "skills": ["product strategy", "web3"],
                    "llm_time_seconds": 28.037,
                }
            ]
        },
    )
    _write_yaml(output_dir / "skipped.yaml", {})
    _write_yaml(output_dir / "failed.yaml", {})
    _write_yaml(output_dir / "interesting_jobs.yaml", [])

    monkeypatch.setattr(data_service, "SUCCESS_FILE", output_dir / "success.yaml")
    monkeypatch.setattr(data_service, "SKIPPED_FILE", output_dir / "skipped.yaml")
    monkeypatch.setattr(data_service, "FAILED_FILE", output_dir / "failed.yaml")
    monkeypatch.setattr(data_service, "INTERESTING_FILE", output_dir / "interesting_jobs.yaml")
    monkeypatch.setattr(
        data_service,
        "read_events_for_run",
        lambda run_id, limit=5000: [
            {
                "run_id": run_id,
                "type": "job_result",
                "message": "Job finished with status Success",
                "timestamp": "2026-04-28T11:23:12",
                "payload": {
                    "job_title": "Chief Product Officer",
                    "company_name": "1inch",
                    "url": "https://www.linkedin.com/jobs/view/4401460753",
                    "result": "Success",
                },
            }
        ][:limit],
    )

    jobs = data_service.get_run_jobs("run-1")

    assert jobs[0]["interest_score"] == 75
    assert jobs[0]["interest_reason"] == "Strong fintech and Web3 alignment"
    assert jobs[0]["skills"] == ["product strategy", "web3"]
    assert jobs[0]["llm_time_seconds"] == 28.037


def test_get_run_jobs_matches_saved_linkedin_url_with_tracking_params(monkeypatch, tmp_path):
    output_dir = tmp_path / "data" / "output"
    _write_yaml(output_dir / "success.yaml", {})
    _write_yaml(
        output_dir / "skipped.yaml",
        {
            "Do Well Do Good": [
                {
                    "company_name": "Do Well Do Good",
                    "job_title": "Director",
                    "url": "https://www.linkedin.com/jobs/view/4403407599/?trackingId=abc",
                    "interest_score": 15,
                    "interest_reason": "Not enough consulting experience",
                    "skip_reason": "Not enough consulting experience",
                    "llm_time_seconds": 107.073,
                }
            ]
        },
    )
    _write_yaml(output_dir / "failed.yaml", {})
    _write_yaml(output_dir / "interesting_jobs.yaml", [])

    monkeypatch.setattr(data_service, "SUCCESS_FILE", output_dir / "success.yaml")
    monkeypatch.setattr(data_service, "SKIPPED_FILE", output_dir / "skipped.yaml")
    monkeypatch.setattr(data_service, "FAILED_FILE", output_dir / "failed.yaml")
    monkeypatch.setattr(data_service, "INTERESTING_FILE", output_dir / "interesting_jobs.yaml")
    monkeypatch.setattr(
        data_service,
        "read_events_for_run",
        lambda run_id, limit=5000: [
            {
                "run_id": run_id,
                "type": "job_result",
                "message": "Job result: Skip",
                "timestamp": "2026-05-08T15:42:48",
                "payload": {
                    "result": "skip",
                    "job_title": "Director",
                    "company_name": "Do Well Do Good",
                    "url": "https://www.linkedin.com/jobs/view/4403407599/",
                },
            }
        ][:limit],
    )

    jobs = data_service.get_run_jobs("run-20260508-154206")

    assert jobs[0]["interest_score"] == 15
    assert jobs[0]["interest_reason"] == "Not enough consulting experience"
    assert jobs[0]["skip_reason"] == "Not enough consulting experience"
    assert jobs[0]["llm_time_seconds"] == 107.073


def test_get_run_jobs_enriches_interesting_jobs_from_saved_outputs(monkeypatch, tmp_path):
    output_dir = tmp_path / "data" / "output"
    _write_yaml(output_dir / "success.yaml", {})
    _write_yaml(output_dir / "skipped.yaml", {})
    _write_yaml(output_dir / "failed.yaml", {})
    _write_yaml(
        output_dir / "interesting_jobs.yaml",
        [
            {
                "company_name": "Al Ghurair",
                "job_title": "Vice President Information Technology",
                "url": "https://www.linkedin.com/jobs/view/4359175355/",
                "interest_score": 85,
                "interest_reason": "Could not apply. Reason: Easy Apply dialog did not open",
                "llm_time_seconds": 7.069,
                "skills": ["api management", "governance"],
            }
        ],
    )

    monkeypatch.setattr(data_service, "SUCCESS_FILE", output_dir / "success.yaml")
    monkeypatch.setattr(data_service, "SKIPPED_FILE", output_dir / "skipped.yaml")
    monkeypatch.setattr(data_service, "FAILED_FILE", output_dir / "failed.yaml")
    monkeypatch.setattr(data_service, "INTERESTING_FILE", output_dir / "interesting_jobs.yaml")
    monkeypatch.setattr(
        data_service,
        "read_events_for_run",
        lambda run_id, limit=5000: [
            {
                "run_id": run_id,
                "type": "job_result",
                "message": "Job result: Skip",
                "timestamp": "2026-05-08T15:43:19",
                "payload": {
                    "result": "skip",
                    "job_title": "Vice President Information Technology",
                    "company_name": "Al Ghurair",
                    "url": "https://www.linkedin.com/jobs/view/4359175355/",
                },
            }
        ][:limit],
    )

    jobs = data_service.get_run_jobs("run-20260508-154206")

    assert jobs[0]["status"] == "interesting"
    assert jobs[0]["interest_score"] == 85
    assert jobs[0]["interest_reason"] == "Could not apply. Reason: Easy Apply dialog did not open"
    assert jobs[0]["skills"] == ["api management", "governance"]


def test_get_jobs_includes_executed_at_from_saved_outputs(monkeypatch, tmp_path):
    output_dir = tmp_path / "data" / "output"

    _write_yaml(
        output_dir / "success.yaml",
        {
            "Acme": [
                {
                    "job_title": "CTO",
                    "url": "https://linkedin.com/jobs/view/1",
                    "executed_at": "2026-04-15T10:02:00",
                    "submitted_resume_path": "/tmp/resumes/cto.pdf",
                }
            ]
        },
    )
    _write_yaml(output_dir / "skipped.yaml", {})
    _write_yaml(output_dir / "failed.yaml", {})
    _write_yaml(output_dir / "interesting_jobs.yaml", [])

    monkeypatch.setattr(data_service, "SUCCESS_FILE", output_dir / "success.yaml")
    monkeypatch.setattr(data_service, "SKIPPED_FILE", output_dir / "skipped.yaml")
    monkeypatch.setattr(data_service, "FAILED_FILE", output_dir / "failed.yaml")
    monkeypatch.setattr(data_service, "INTERESTING_FILE", output_dir / "interesting_jobs.yaml")
    monkeypatch.setattr(data_service, "get_snapshot", lambda: {"current_job": None})

    jobs = data_service.get_jobs()

    assert jobs[0]["executed_at"] == "2026-04-15T10:02:00"
    assert jobs[0]["submitted_resume_path"] == "/tmp/resumes/cto.pdf"


def test_get_jobs_payload_includes_filtered_and_total_counts(monkeypatch):
    monkeypatch.setattr(
        data_service,
        "_load_jobs_board",
        lambda: [
            {"status": "applied", "job_title": "CTO", "company_name": "Acme", "url": "1"},
            {"status": "skipped", "job_title": "CPO", "company_name": "Beta", "url": "2"},
        ],
    )

    payload = data_service.get_jobs_payload(status="applied")

    assert payload["filtered_count"] == 1
    assert payload["total_count"] == 2
    assert payload["jobs"][0]["job_title"] == "CTO"


def test_get_run_jobs_payload_includes_filtered_and_total_counts(monkeypatch):
    monkeypatch.setattr(
        data_service,
        "_build_run_jobs",
        lambda run_id: [
            {"status": "applied", "job_title": "CTO", "company_name": "Acme", "url": "1"},
            {"status": "skipped", "job_title": "CPO", "company_name": "Beta", "url": "2"},
        ],
    )

    payload = data_service.get_run_jobs_payload("run-1", search="cto")

    assert payload["run_id"] == "run-1"
    assert payload["filtered_count"] == 1
    assert payload["total_count"] == 2
    assert payload["jobs"][0]["job_title"] == "CTO"


def test_get_run_detail_combines_run_jobs_and_events(monkeypatch):
    monkeypatch.setattr(
        data_service,
        "get_run_history",
        lambda: [
            {
                "run_id": "run-1",
                "status": "completed",
                "started_at": "2026-04-15T10:00:00",
                "finished_at": "2026-04-15T10:10:00",
                "last_event_at": "2026-04-15T10:10:00",
                "last_message": "Completed",
                "jobs": {
                    "applied": 1,
                    "skipped": 1,
                    "failed": 0,
                    "interesting": 1,
                    "discovered": 2,
                },
            }
        ],
    )
    monkeypatch.setattr(
        data_service,
        "get_run_jobs",
        lambda run_id: [{"url": "https://linkedin.com/jobs/view/1"}],
    )
    monkeypatch.setattr(
        data_service,
        "get_run_events",
        lambda run_id, limit=120: [{"type": "run_started"}],
    )

    detail = data_service.get_run_detail("run-1")

    assert detail["run"]["run_id"] == "run-1"
    assert len(detail["jobs"]) == 1
    assert len(detail["events"]) == 1


def test_get_run_detail_includes_screenshots(monkeypatch):
    monkeypatch.setattr(data_service, "get_run_history", lambda: [])
    monkeypatch.setattr(data_service, "get_run_jobs", lambda run_id: [])
    monkeypatch.setattr(data_service, "get_run_events", lambda run_id, limit=120: [])
    monkeypatch.setattr(
        data_service,
        "get_run_screenshots",
        lambda run_id: [{"path": "data/output/dashboard/screenshots/run-1/example.png"}],
    )

    detail = data_service.get_run_detail("run-1")

    assert len(detail["screenshots"]) == 1
    assert detail["screenshots"][0]["path"].endswith("example.png")


def test_update_app_config_updates_values(monkeypatch, tmp_path):
    app_config_file = tmp_path / "app_config.py"
    app_config_file.write_text(
        'HEADLESS_MODE = True\nMAX_APPLIES_NUM = 50\nRESUME_STYLE = "FAANGPath"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(data_service, "APP_CONFIG_FILE", app_config_file)

    updated = data_service.update_app_config(
        {
            "HEADLESS_MODE": False,
            "MAX_APPLIES_NUM": 25,
            "RESUME_STYLE": "Modern Blue",
        }
    )

    assert updated["HEADLESS_MODE"] is False
    assert updated["MAX_APPLIES_NUM"] == 25
    assert updated["RESUME_STYLE"] == "Modern Blue"

    file_content = app_config_file.read_text(encoding="utf-8")
    assert "HEADLESS_MODE = False" in file_content
    assert "MAX_APPLIES_NUM = 25" in file_content
    assert 'RESUME_STYLE = "Modern Blue"' in file_content
