import json

import pytest

from src.application_tracker import ApplicationTracker
from src.utils.search_pool import reset_search_pool


def test_reset_requires_confirmation(tmp_path):
    path = tmp_path / "encountered_jobs.json"
    path.write_text('["123"]\n', encoding="utf-8")
    with pytest.raises(PermissionError):
        reset_search_pool(path)


def test_reset_keeps_recoverable_backup_and_submission_protection(tmp_path):
    path = tmp_path / "encountered_jobs.json"
    path.write_text('["123"]\n', encoding="utf-8")
    tracker = ApplicationTracker(tmp_path / "applications.json")
    tracker.apply_event({
        "type": "job_result",
        "timestamp": "2026-09-01T00:00:00+00:00",
        "payload": {"job_id": "submitted", "classification": "SUBMITTED", "url": "https://jobs.example/1"},
    })
    result = reset_search_pool(path, tracker=tracker, confirmed=True)
    assert json.loads(path.read_text(encoding="utf-8")) == []
    assert result["protected"] == 1
    assert result["backup"]
    assert (tmp_path / "applications.json").exists()
