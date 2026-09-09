"""Unit tests for the graceful-shutdown state machine in src.utils.runtime_control."""

from src.utils.runtime_control import RuntimeController, ShutdownState
import pytest


def test_initial_state_is_running():
    controller = RuntimeController()
    assert controller.shutdown_state == ShutdownState.RUNNING
    assert not controller.is_shutdown_requested()


def test_first_request_transitions_to_draining():
    controller = RuntimeController()
    controller.request_shutdown("test")
    assert controller.shutdown_state == ShutdownState.DRAINING
    assert controller.is_shutdown_requested()


def test_second_request_keeps_draining():
    controller = RuntimeController()
    controller.request_shutdown("first")
    controller.request_shutdown("second")
    assert controller.shutdown_state == ShutdownState.DRAINING
    assert controller.is_shutdown_requested()


def test_request_during_cleanup_does_not_regress_to_draining():
    controller = RuntimeController()
    controller.set_shutdown_state(ShutdownState.CLEANUP)
    controller.request_shutdown("test")
    assert controller.shutdown_state == ShutdownState.CLEANUP
    assert controller.is_shutdown_requested()


def test_shutdown_is_sticky_across_cleanup_and_running_transition():
    controller = RuntimeController()
    controller.request_shutdown("test")
    controller.set_shutdown_state(ShutdownState.CLEANUP)
    controller.set_shutdown_state(ShutdownState.DONE)
    controller.set_shutdown_state(ShutdownState.RUNNING)
    assert controller.shutdown_state == ShutdownState.DONE
    assert controller.is_shutdown_requested()


def test_begin_run_does_not_erase_pending_shutdown_request():
    controller = RuntimeController()
    controller.request_shutdown("previous")
    assert controller.begin_run() is False
    assert controller.is_shutdown_requested()
    assert controller.shutdown_state == ShutdownState.DRAINING


def test_shutdown_closes_worker_admission_and_active_worker_drains():
    controller = RuntimeController()

    assert controller.try_start_job() is True
    assert controller.try_start_worker("easy_apply") is True
    controller.request_shutdown("test")

    assert controller.has_active_worker("easy_apply") is True
    assert controller.try_start_worker("external") is False
    assert controller.try_start_job() is False
    assert controller.wait_for_drain(timeout=0) is False

    controller.finish_worker("easy_apply")
    assert controller.wait_for_drain(timeout=0) is False
    controller.finish_job()

    assert controller.wait_for_drain(timeout=0) is True
    assert controller.active_worker_count == 0
    assert controller.active_job_count == 0


def test_launcher_sentinel_closes_all_admission(monkeypatch, tmp_path):
    shutdown_file = tmp_path / "shutdown"
    monkeypatch.setenv("BOBBY_SHUTDOWN_FILE", str(shutdown_file))
    controller = RuntimeController()
    assert controller.start_process_run("run-test") is True

    shutdown_file.touch()

    assert controller.is_shutdown_requested()
    assert not controller.try_start_job()
    assert not controller.try_start_worker("easy_apply")
    assert not controller.begin_run()


def test_direct_shutdown_publishes_sentinel_for_an_isolated_worker(monkeypatch, tmp_path):
    """A direct main-process signal must close a worker's dispatch boundary too."""

    shutdown_file = tmp_path / "shutdown"
    monkeypatch.setenv("BOBBY_SHUTDOWN_FILE", str(shutdown_file))
    parent = RuntimeController()
    assert parent.start_process_run("run-parent") is True

    parent.request_shutdown("direct SIGINT")

    assert shutdown_file.is_file()
    # A separately constructed controller models the isolated external ATS
    # process, which shares the sentinel but not the parent's memory.
    worker = RuntimeController()
    assert worker.is_shutdown_requested()
    assert worker.try_start_irreversible_dispatch("final_submit") is False


def test_run_aggregate_survives_browser_cycle_and_deduplicates_retries():
    controller = RuntimeController()
    assert controller.start_process_run("run-test")
    assert controller.record_discovered_job("job-1", encountered=False)
    controller.record_attempt("easy_apply")
    assert controller.record_terminal_outcome(
        "job-1", "SUBMITTED", job_title="Role", company_name="Company"
    )

    controller.set_shutdown_state(ShutdownState.CLEANUP)
    controller.set_shutdown_state(ShutdownState.DONE)
    assert controller.begin_run()  # browser recovery, not a new process run
    assert not controller.record_discovered_job("job-1", encountered=True)
    assert not controller.record_terminal_outcome("job-1", "TECHNICAL_FAILURE")

    snapshot = controller.aggregate_snapshot()
    assert snapshot is not None
    assert snapshot["found"] == 1
    assert snapshot["new"] == 1
    assert snapshot["encountered"] == 0
    assert snapshot["attempted"] == 1
    assert snapshot["submitted"] == 1
    assert snapshot["technical_failure"] == 0
    assert snapshot["processed"] == 1
    assert snapshot["consistent"] is True


def test_new_job_disposition_is_opaque_and_final_value_is_idempotent(monkeypatch, tmp_path):
    path = tmp_path / "stats.json"
    monkeypatch.setenv("BOBBY_RUN_STATE_FILE", str(path))
    controller = RuntimeController()
    assert controller.start_process_run("run-disposition")
    raw_job = "https://linkedin.example/jobs/view/123?private=tracking"
    assert controller.record_discovered_job(raw_job, encountered=False)
    assert controller.job_disposition(raw_job) == "JOB_DISCOVERED_NEW"
    assert controller.record_job_disposition(raw_job, "JOB_DEFERRED_PROVIDER")
    assert not controller.record_job_disposition(raw_job, "JOB_SKIPPED_LOW_SUITABILITY")
    snapshot = controller.aggregate_snapshot()
    assert snapshot["disposition_counts"] == {"JOB_DEFERRED_PROVIDER": 1}
    assert snapshot["unresolved_new_dispositions"] == 0
    assert raw_job not in path.read_text(encoding="utf-8")


def test_easy_apply_quota_deferral_is_retryable_and_reconciles(monkeypatch, tmp_path):
    monkeypatch.setenv("BOBBY_RUN_STATE_FILE", str(tmp_path / "stats.json"))
    controller = RuntimeController()
    assert controller.start_process_run("run-quota")
    assert controller.record_discovered_job("quota-job", encountered=False)
    assert controller.record_retryable_job_disposition(
        "quota-job", "JOB_DEFERRED_EASY_APPLY_LIMIT"
    )
    assert controller.job_disposition("quota-job") == "JOB_DEFERRED_EASY_APPLY_LIMIT"
    assert controller.record_terminal_outcome("quota-job", "DEFERRED_EASY_APPLY_LIMIT")
    snapshot = controller.aggregate_snapshot()
    assert snapshot["easy_apply_deferred"] == 1
    assert snapshot["skipped"] == 0
    assert snapshot["consistent"] is True


def test_easy_apply_quota_deferral_cannot_replace_submission(monkeypatch, tmp_path):
    monkeypatch.setenv("BOBBY_RUN_STATE_FILE", str(tmp_path / "stats.json"))
    controller = RuntimeController()
    assert controller.start_process_run("run-quota-protected")
    assert controller.record_discovered_job("submitted-job", encountered=False)
    assert controller.record_job_disposition("submitted-job", "JOB_ADMITTED")
    assert controller.record_terminal_outcome("submitted-job", "SUBMITTED")
    assert not controller.record_retryable_job_disposition(
        "submitted-job", "JOB_DEFERRED_EASY_APPLY_LIMIT"
    )


def test_terminal_outcome_lookup_uses_same_opaque_key(monkeypatch, tmp_path):
    monkeypatch.setenv("BOBBY_RUN_STATE_FILE", str(tmp_path / "stats.json"))
    controller = RuntimeController()
    assert controller.start_process_run("run-terminal-key")
    assert controller.record_terminal_outcome("job-key", "SKIPPED")
    assert controller.has_terminal_outcome("job-key")


def test_irreversible_dispatch_gate_counts_starts_and_blocks_after_shutdown():
    controller = RuntimeController()
    assert controller.start_process_run("run-dispatch")
    assert controller.try_start_irreversible_dispatch("resume_upload") is True
    controller.request_shutdown("SIGINT")
    assert controller.try_start_irreversible_dispatch("final_submit") is False
    snapshot = controller.aggregate_snapshot()
    assert snapshot["irreversible_dispatch_starts"] == {"resume_upload": 1}
    assert snapshot["blocked_irreversible_dispatches"] == {"final_submit": 1}


def test_irreversible_dispatch_gate_survives_run_process_restart(monkeypatch, tmp_path):
    path = tmp_path / "stats.json"
    monkeypatch.setenv("BOBBY_RUN_STATE_FILE", str(path))
    first = RuntimeController()
    assert first.start_process_run("run-dispatch-persist")
    assert first.try_start_irreversible_dispatch("account_registration")
    second = RuntimeController()
    assert second.start_process_run("run-dispatch-persist")
    assert second.aggregate_snapshot()["irreversible_dispatch_starts"] == {"account_registration": 1}


def test_active_worker_can_finalize_after_shutdown_then_run_is_drained():
    controller = RuntimeController()
    controller.start_process_run("run-test")
    assert controller.try_start_job()
    assert controller.try_start_worker("external")
    controller.request_shutdown("SIGINT")

    controller.record_attempt("external")
    assert controller.record_terminal_outcome("job-1", "CANCELLED")
    controller.finish_worker("external")
    controller.finish_job()

    snapshot = controller.aggregate_snapshot(partial=True)
    assert controller.wait_for_drain(timeout=0)
    assert snapshot["cancelled"] == 1
    assert snapshot["in_progress"] == 0
    assert snapshot["partial"] is True


def test_run_counters_survive_remote_to_tampa_process_restart(monkeypatch, tmp_path):
    path = tmp_path / "stats.json"
    monkeypatch.setenv("BOBBY_RUN_STATE_FILE", str(path))
    remote = RuntimeController()
    assert remote.start_process_run("run-profile-transition")
    assert remote.mark_start_notification()
    remote.record_discovered_job("remote-job", encountered=False)
    remote.record_attempt("easy_apply")
    remote.record_terminal_outcome("remote-job", "SUBMITTED")

    tampa = RuntimeController()
    assert tampa.start_process_run("run-profile-transition")
    assert not tampa.mark_start_notification()
    assert not tampa.record_discovered_job("remote-job", encountered=True)
    assert not tampa.record_terminal_outcome("remote-job", "TECHNICAL_FAILURE")
    tampa.record_discovered_job("tampa-job", encountered=False)
    tampa.record_attempt("external")
    tampa.record_terminal_outcome("tampa-job", "UNVERIFIED_AFTER_SUBMIT")
    snapshot = tampa.aggregate_snapshot()
    assert snapshot["found"] == snapshot["attempted"] == snapshot["processed"] == 2
    assert snapshot["submitted"] == snapshot["unverified"] == 1
    assert snapshot["easy_apply_attempted"] == snapshot["external_attempted"] == 1
    assert snapshot["consistent"] is True
    assert path.stat().st_mode & 0o777 == 0o600


def test_runtime_ledger_persists_opaque_job_keys_and_redacted_applied_urls(monkeypatch, tmp_path):
    path = tmp_path / "stats.json"
    monkeypatch.setenv("BOBBY_RUN_STATE_FILE", str(path))
    url = "https://jobs.example.invalid/view/123?tracking=private"
    controller = RuntimeController()
    assert controller.start_process_run("run-private-ledger")
    assert controller.record_discovered_job(url, encountered=False)
    controller.record_attempt("external")
    assert controller.record_terminal_outcome(
        url, "SUBMITTED", job_title="Role", company_name="Company", url=url
    )

    raw = path.read_text(encoding="utf-8")
    assert url not in raw
    assert "https://" not in raw
    assert "[REDACTED]" in raw

    resumed = RuntimeController()
    assert resumed.start_process_run("run-private-ledger")
    assert not resumed.record_discovered_job(url, encountered=True)
    assert not resumed.record_terminal_outcome(url, "TECHNICAL_FAILURE")


@pytest.mark.parametrize("contents", ["{broken-json", '{"version": 1, "aggregate": {"run_id": "different"}}'])
def test_damaged_or_mismatched_ledger_cannot_silently_reset_run(monkeypatch, tmp_path, contents):
    path = tmp_path / "stats.json"
    path.write_text(contents)
    monkeypatch.setenv("BOBBY_RUN_STATE_FILE", str(path))
    with pytest.raises(RuntimeError, match="restored safely"):
        RuntimeController().start_process_run("run-current")
    assert path.read_text() == contents
