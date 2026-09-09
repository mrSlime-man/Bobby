import asyncio
import os
import signal
import sys
from pathlib import Path

import dotenv

# Browser Use 0.12.6 honors these before its package is imported. Keep action
# payloads (which may contain form PII) out of production stdout and disable
# anonymized telemetry without changing the installed version.
os.environ["BROWSER_USE_LOGGING_LEVEL"] = "result"
os.environ["ANONYMIZED_TELEMETRY"] = "false"

from config.app_config import READY_MADE_RESUME_PATH
from config.constants import RESUME_DIR
from src.llm.apply_agent import ApplyAgent
from src.llm.external_worker_state import (
    read_worker_state,
    submit_may_have_occurred,
    write_worker_state,
)
from src.utils.candidate_integrity import validate_candidate_identity
from src.utils.candidate_profile import CANDIDATE_PROFILE_PATH, load_candidate_profile
from src.utils.browser_use_logging import install_browser_use_log_redaction, redact_browser_use_text
from src.utils.log_privacy import profile_sensitive_values
from src.utils.utils import load_yaml_file

_SENSITIVE_VALUES: tuple[str, ...] = ()


async def main(
    job_url: str,
    job_title: str = "",
    company_name: str = "",
    linkedin_url: str = "",
    state_file: str = "",
) -> None:
    global _SENSITIVE_VALUES
    secrets = dotenv.dotenv_values(".env")

    api_key = secrets.get("llm_api_key", "")
    llm_api_url = secrets.get("llm_api_url")
    candidate_profile = load_candidate_profile(CANDIDATE_PROFILE_PATH)
    candidate = candidate_profile.get("candidate") or {}
    application_email = str(candidate.get("email") or "").strip()
    if not application_email:
        raise RuntimeError("Candidate application email is missing from candidate_profile.yaml")

    if not api_key:
        raise RuntimeError("llm_api_key is missing from .env")

    resume_file = Path(RESUME_DIR) / "resume_text.txt"
    if not resume_file.is_file():
        raise RuntimeError(f"Resume text file not found: {resume_file}")

    resume_text = resume_file.read_text(encoding="utf-8")
    structured_file = Path(RESUME_DIR) / "structured_resume.yaml"
    resume_structured = load_yaml_file(structured_file)
    validate_candidate_identity(
        resume_text=resume_text,
        resume_structured=resume_structured,
        application_profile_path=CANDIDATE_PROFILE_PATH,
        resume_pdf_path=Path(READY_MADE_RESUME_PATH),
    )
    personal = resume_structured.get("personal_information") or {}
    resume_sensitive_values = (
        value
        for key, value in personal.items()
        if key in {"email", "phone", "address", "current_location"} and value
    )
    environment_sensitive_values = (
        value
        for key, value in secrets.items()
        if value
        and any(
            marker in key.casefold()
            for marker in ("key", "token", "password", "secret", "email", "url")
        )
    )
    sensitive_values = tuple(
        dict.fromkeys(
            str(value)
            for value in (
                *resume_sensitive_values,
                *profile_sensitive_values(candidate_profile),
                *environment_sensitive_values,
            )
            if str(value).strip()
        )
    )
    _SENSITIVE_VALUES = sensitive_values
    install_browser_use_log_redaction(sensitive_values)

    external_storage_state = "browser_session/external_apply_state.json"

    agent = ApplyAgent(
        api_key,
        external_storage_state,
        llm_api_url,
        application_email,
        worker_state_path=state_file,
    )
    agent.set_resume(resume_text)

    await agent.apply(
        job_url,
        job_title=job_title,
        company_name=company_name,
        linkedin_url=linkedin_url,
    )
    write_worker_state(
        state_file,
        "submitted",
        submit_attempted=True,
        run_id=os.getenv("BOBBY_RUN_ID", ""),
    )


def _install_signal_handlers(state_file: str) -> None:
    def handle_signal(signum, _frame):
        state = read_worker_state(state_file)
        write_worker_state(
            state_file,
            state.get("phase", "pre_submit"),
            submit_attempted=bool(state.get("submit_attempted")),
            shutdown_requested=True,
            shutdown_signal=signal.Signals(signum).name,
        )
        # Production workers are isolated from the launcher's SIGINT. The
        # parent sends SIGTERM only after the natural drain expires; translate
        # it into the existing submit-aware terminal classification below.
        if signum == signal.SIGTERM:
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: external_apply_worker.py "
            "<job_url> [job_title] [company_name] [linkedin_url] [state_file]",
            file=sys.stderr,
        )
        raise SystemExit(2)

    job_url = sys.argv[1]
    job_title = sys.argv[2] if len(sys.argv) > 2 else ""
    company_name = sys.argv[3] if len(sys.argv) > 3 else ""
    linkedin_url = sys.argv[4] if len(sys.argv) > 4 else ""
    state_file = sys.argv[5] if len(sys.argv) > 5 else os.getenv("BOBBY_EXTERNAL_STATE_FILE", "")
    if not state_file:
        raise SystemExit(2)
    os.environ["BOBBY_EXTERNAL_STATE_FILE"] = state_file
    _install_signal_handlers(state_file)

    try:
        asyncio.run(main(job_url, job_title, company_name, linkedin_url, state_file))
    except KeyboardInterrupt:
        state = read_worker_state(state_file)
        phase = (
            "unverified_after_submit"
            if submit_may_have_occurred(state)
            else "cancelled_before_submit"
        )
        write_worker_state(
            state_file,
            phase,
            submit_attempted=submit_may_have_occurred(state),
            run_id=os.getenv("BOBBY_RUN_ID", ""),
        )
        if submit_may_have_occurred(state):
            raise SystemExit(10)
        raise SystemExit(130)
    except Exception as exc:
        message = redact_browser_use_text(exc, _SENSITIVE_VALUES)
        print(f"EXTERNAL APPLY WORKER ERROR: {message}", file=sys.stderr)

        state = read_worker_state(state_file)

        if "CANCELLED_BY_SHUTDOWN" in message:
            attempted = submit_may_have_occurred(state)
            write_worker_state(
                state_file,
                "unverified_after_submit" if attempted else "cancelled_before_submit",
                submit_attempted=attempted,
            )
            raise SystemExit(10 if attempted else 130)
        if "APPLICATION_NEEDS_HUMAN" in message:
            write_worker_state(
                state_file, "needs_human", submit_attempted=submit_may_have_occurred(state)
            )
            raise SystemExit(11)
        if "APPLICATION_UNVERIFIED" in message or "APPLICATION_NOT_VERIFIED" in message:
            write_worker_state(state_file, "unverified_after_submit", submit_attempted=True)
            raise SystemExit(10)
        if "APPLICATION_NOT_ELIGIBLE" in message:
            write_worker_state(
                state_file, "not_eligible", submit_attempted=submit_may_have_occurred(state)
            )
            raise SystemExit(12)
        durable_provider_error = str(state.get("provider_error_class") or "").strip()
        durable_error_class = (
            "EXTERNAL_PROVIDER_UNAVAILABLE"
            if durable_provider_error
            else str(state.get("error_class") or "").strip()
            or message.split(":", 1)[0][:80]
        )
        write_worker_state(
            state_file,
            "unverified_after_submit" if submit_may_have_occurred(state) else "technical_failure",
            submit_attempted=submit_may_have_occurred(state),
            error_class=durable_error_class,
        )
        if submit_may_have_occurred(state):
            raise SystemExit(10)
        raise SystemExit(1)
