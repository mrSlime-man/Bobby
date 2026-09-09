import asyncio
import signal

import pytest
from browser_use import Tools
from unittest.mock import AsyncMock, MagicMock, patch

from src.llm.apply_agent import (
    ApplyAgent,
    classify_worker_result,
    kill_external_process_group,
    unresolved_submission_error,
    validate_custom_action_schemas,
)
from src.utils.runtime_control import runtime_controller
def test_custom_action_with_meaningful_argument_has_non_empty_schema():
    tools = Tools()

    @tools.action(description="Test action")
    async def custom_action(reason: str, browser_session):  # noqa: ARG001
        return reason

    validate_custom_action_schemas(tools)


def test_empty_custom_action_schema_is_rejected():
    tools = MagicMock()
    action = MagicMock()
    action.param_model.model_json_schema.return_value = {
        "type": "object",
        "properties": {},
    }
    tools.registry.registry.actions = {"empty_action": action}

    with pytest.raises(RuntimeError, match="empty_action"):
        validate_custom_action_schemas(tools)


def test_unverified_code_requires_a_credible_final_submit_attempt():
    assert unresolved_submission_error(True).startswith("APPLICATION_NOT_VERIFIED")
    assert unresolved_submission_error(False).startswith("EXTERNAL_AGENT_FAILED")


def test_force_stop_ends_isolated_posix_process_group():
    process = MagicMock(pid=12345)

    with (
        patch("src.llm.apply_agent.os.name", "posix"),
        patch("src.llm.apply_agent.os.killpg") as killpg,
    ):
        kill_external_process_group(process)

    killpg.assert_called_once_with(12345, signal.SIGKILL)
    process.kill.assert_not_called()


def test_force_stop_falls_back_to_worker_when_group_kill_fails():
    process = MagicMock(pid=12345)

    with (
        patch("src.llm.apply_agent.os.name", "posix"),
        patch("src.llm.apply_agent.os.killpg", side_effect=PermissionError),
    ):
        kill_external_process_group(process)

    process.kill.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_result", "expected_reason"),
    [
        (0, "Success", ""),
        (10, "Error", "UNVERIFIED_AFTER_SUBMIT"),
        (11, "Error", "NEEDS_HUMAN"),
        (12, "Skip", "NOT_ELIGIBLE"),
        (130, "Cancelled", "CANCELLED_BY_SHUTDOWN"),
        (1, "Error", "TECHNICAL_FAILURE"),
    ],
)
async def test_worker_exit_code_taxonomy(code, expected_result, expected_reason):
    process = MagicMock()
    process.wait = AsyncMock(return_value=code)
    agent = object.__new__(ApplyAgent)
    agent.run_id = "run-test"
    agent.model_type = "gemini"
    with (
        patch("src.llm.apply_agent.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=process),
        patch("src.llm.apply_agent.emit_event") as mock_emit,
        patch(
            "src.llm.apply_agent.read_worker_state",
            return_value={
                "phase": (
                    "submitted" if code == 0 else
                    "cancelled_before_submit" if code == 130 else
                    "technical_failure"
                )
            },
        ),
        patch("src.llm.apply_agent.circuit_is_open", return_value=False),
    ):
        result, reason = await agent.apply_to_job("https://example.com/job")

    assert result == expected_result
    assert expected_reason in reason
    event_types = [call.args[0] for call in mock_emit.call_args_list]
    assert "agent_apply_completed" not in event_types
    assert "agent_apply_cancelled" not in event_types
    assert "agent_apply_failed" not in event_types


@pytest.mark.asyncio
async def test_permanent_provider_circuit_fails_before_external_worker_launch():
    agent = object.__new__(ApplyAgent)
    agent.run_id = "run-circuit-test"
    agent.model_type = "gemini"

    with (
        patch("src.llm.apply_agent.circuit_is_open", return_value=True),
        patch(
            "src.llm.apply_agent.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
        ) as launch,
    ):
        result = await agent.apply_to_job("https://external.example/job")

    assert result == (
        "Error",
        "NEEDS_HUMAN: EXTERNAL_BLOCKER: external LLM provider circuit is open "
        "before worker launch | internal_code=EXTERNAL_PROVIDER_CIRCUIT_OPEN retryable=true",
    )
    launch.assert_not_awaited()


def test_provider_unavailability_is_an_external_blocker_before_final_submit():
    result, reason = classify_worker_result(
        1,
        {
            "phase": "technical_failure",
            "error_class": "EXTERNAL_PROVIDER_UNAVAILABLE",
            "ats": "generic",
            "submit_attempted": False,
        },
    )

    assert result == "Error"
    assert reason.startswith("NEEDS_HUMAN: EXTERNAL_BLOCKER:")
    assert "EXTERNAL_PROVIDER_UNAVAILABLE" in reason


def test_durable_provider_error_overrides_later_upload_wrapper_error():
    result, reason = classify_worker_result(
        1,
        {
            "phase": "technical_failure",
            "error_class": "EXTERNAL_RESUME_UPLOAD_FAILED",
            "provider_error_class": "transient_unavailable",
            "ats": "workday",
            "submit_attempted": False,
        },
    )

    assert result == "Error"
    assert reason.startswith("NEEDS_HUMAN: EXTERNAL_BLOCKER:")
    assert "internal_code=EXTERNAL_PROVIDER_UNAVAILABLE" in reason


@pytest.mark.asyncio
async def test_shutdown_drains_active_external_worker_without_terminating_it():
    completed = asyncio.Event()
    launched = asyncio.Event()
    launch_options = {}
    process = MagicMock()

    async def wait_for_completion():
        await completed.wait()
        return 0

    process.wait = AsyncMock(side_effect=wait_for_completion)
    process.terminate = MagicMock()
    process.kill = MagicMock()

    async def launch(*_args, **kwargs):
        launch_options.update(kwargs)
        launched.set()
        return process

    agent = object.__new__(ApplyAgent)
    agent.run_id = "run-drain-test"
    agent.model_type = "gemini"

    runtime_controller.shutdown_requested.clear()
    runtime_controller.begin_run()
    assert runtime_controller.try_start_worker("external") is True
    try:
        with (
            patch(
                "src.llm.apply_agent.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                side_effect=launch,
            ),
            patch("src.llm.apply_agent.write_worker_state"),
            patch(
                "src.llm.apply_agent.read_worker_state",
                return_value={"phase": "submitted", "submit_attempted": True},
            ),
            patch("src.llm.apply_agent.circuit_is_open", return_value=False),
            patch("src.llm.apply_agent.emit_event"),
        ):
            apply_task = asyncio.create_task(agent.apply_to_job("https://example.com/job"))
            await launched.wait()
            runtime_controller.request_shutdown("test")
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            assert apply_task.done() is False
            process.terminate.assert_not_called()
            process.kill.assert_not_called()

            completed.set()
            assert await apply_task == ("Success", "")
            assert launch_options["start_new_session"] is True
    finally:
        runtime_controller.finish_worker("external")
        runtime_controller.shutdown_requested.clear()
        runtime_controller.begin_run()


@pytest.mark.asyncio
async def test_shutdown_refuses_external_worker_before_subprocess_start():
    agent = object.__new__(ApplyAgent)
    agent.run_id = "run-refuse-test"
    agent.model_type = "gemini"

    runtime_controller.shutdown_requested.clear()
    runtime_controller.begin_run()
    runtime_controller.request_shutdown("test")
    try:
        with patch(
            "src.llm.apply_agent.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
        ) as launch:
            result = await agent.apply_to_job("https://example.com/job")
    finally:
        runtime_controller.shutdown_requested.clear()
        runtime_controller.begin_run()

    assert result == (
        "Cancelled",
        "CANCELLED_BY_SHUTDOWN: external worker was not started",
    )
    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_cancels_pre_submit_worker_after_bounded_natural_drain():
    completed = asyncio.Event()
    launched = asyncio.Event()
    state = {"phase": "pre_submit", "submit_attempted": False}
    process = MagicMock()
    process.pid = 12345

    async def wait_for_completion():
        await completed.wait()
        # Browser Use's signal handler can exit with zero and leave the
        # worker's durable pre-submit state untouched.
        return 0

    def terminate():
        completed.set()

    def write_state(_path, phase, **values):
        state.update(phase=phase, **values)

    process.wait = AsyncMock(side_effect=wait_for_completion)
    process.terminate = MagicMock(side_effect=terminate)
    process.kill = MagicMock()

    async def launch(*_args, **_kwargs):
        launched.set()
        return process

    agent = object.__new__(ApplyAgent)
    agent.run_id = "run-bounded-drain-test"
    agent.model_type = "gemini"

    runtime_controller.shutdown_requested.clear()
    runtime_controller.begin_run()
    assert runtime_controller.try_start_worker("external") is True
    try:
        with (
            patch(
                "src.llm.apply_agent.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                side_effect=launch,
            ),
            patch("src.llm.apply_agent.write_worker_state", side_effect=write_state),
            patch("src.llm.apply_agent.read_worker_state", side_effect=lambda _path: dict(state)),
            patch("src.llm.apply_agent.circuit_is_open", return_value=False),
            patch("src.llm.apply_agent.emit_event"),
            patch("src.llm.apply_agent.kill_external_process_group") as kill_group,
            patch(
                "src.llm.apply_agent.EXTERNAL_SHUTDOWN_NATURAL_DRAIN_SECONDS",
                0.01,
            ),
            patch(
                "src.llm.apply_agent.EXTERNAL_SHUTDOWN_TERMINATE_SECONDS",
                1,
            ),
        ):
            apply_task = asyncio.create_task(agent.apply_to_job("https://example.com/job"))
            await launched.wait()
            runtime_controller.request_shutdown("test")
            assert await apply_task == (
                "Cancelled",
                "CANCELLED_BY_SHUTDOWN: external application stopped before final submit",
            )
            process.terminate.assert_called_once()
            process.kill.assert_not_called()
            kill_group.assert_called_once_with(process)
            assert state["phase"] == "cancelled_before_submit"
    finally:
        runtime_controller.finish_worker("external")
        runtime_controller.shutdown_requested.clear()
        runtime_controller.begin_run()


def test_external_worker_sigint_records_shutdown_without_aborting_active_work(tmp_path):
    from src.llm import external_apply_worker
    from src.llm.external_worker_state import read_worker_state, write_worker_state

    state_path = tmp_path / "worker-state.json"
    write_worker_state(state_path, "pre_submit", submit_attempted=False)
    handlers = {}

    with patch.object(
        external_apply_worker.signal,
        "signal",
        side_effect=lambda sig, handler: handlers.__setitem__(sig, handler),
    ):
        external_apply_worker._install_signal_handlers(str(state_path))

    # A pre-submit graceful signal used to raise KeyboardInterrupt here.
    handlers[signal.SIGINT](signal.SIGINT, None)

    state = read_worker_state(state_path)
    assert state["phase"] == "pre_submit"
    assert state["shutdown_requested"] is True
    assert state["shutdown_signal"] == "SIGINT"


def test_external_worker_sigterm_records_state_then_cancels_safely(tmp_path):
    from src.llm import external_apply_worker
    from src.llm.external_worker_state import read_worker_state, write_worker_state

    state_path = tmp_path / "worker-state.json"
    write_worker_state(state_path, "pre_submit", submit_attempted=False)
    handlers = {}

    with patch.object(
        external_apply_worker.signal,
        "signal",
        side_effect=lambda sig, handler: handlers.__setitem__(sig, handler),
    ):
        external_apply_worker._install_signal_handlers(str(state_path))

    with pytest.raises(KeyboardInterrupt):
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    state = read_worker_state(state_path)
    assert state["phase"] == "pre_submit"
    assert state["shutdown_requested"] is True
    assert state["shutdown_signal"] == "SIGTERM"
