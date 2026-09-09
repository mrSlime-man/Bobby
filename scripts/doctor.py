#!/usr/bin/env python3
"""Run a non-invasive Bobby installation readiness check."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _report(label: str, state: str) -> bool:
    print(f"{state:<5} {label}")
    return state == "FAIL"


def _env_names(path: Path) -> set[str]:
    names = set(os.environ)
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                names.add(stripped.split("=", 1)[0].removeprefix("export ").strip())
    except OSError:
        pass
    return names


def _setting_is_true(path: Path, name: str) -> bool:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{name} ="):
                return line.split("=", 1)[1].strip().lower() == "true"
    except OSError:
        pass
    return False


def main() -> int:
    failures = 0
    failures += _report("Python 3.12 runtime", "PASS" if sys.version_info[:2] == (3, 12) else "FAIL")
    for relative in ("pyproject.toml", "uv.lock", "config/app_config.py", "scripts/public_release.py"):
        failures += _report(
            f"required file: {relative}",
            "PASS" if (ROOT / relative).is_file() else "FAIL",
        )

    env_file = ROOT / ".env"
    failures += _report("local .env present", "PASS" if env_file.is_file() else "WARN")
    if env_file.is_file():
        private = env_file.stat().st_mode & 0o077 == 0
        failures += _report("local .env permissions are private", "PASS" if private else "WARN")

    provider_names = {
        "llm_api_key", "GEMINI_API_KEY", "openai_api_key", "OPENAI_API_KEY",
        "anthropic_api_key", "ANTHROPIC_API_KEY",
    }
    failures += _report(
        "at least one LLM credential name configured",
        "PASS" if provider_names & _env_names(env_file) else "WARN",
    )
    failures += _report(
        "candidate profile available",
        "PASS" if (ROOT / "candidate_profile.yaml").is_file() else "WARN",
    )
    failures += _report(
        "browser session available",
        "PASS" if (ROOT / "browser_session").is_dir() else "WARN",
    )

    config = ROOT / "config" / "app_config.py"
    failures += _report(
        "Gmail verification is optional",
        "PASS" if not _setting_is_true(config, "GMAIL_APPLICATION_INTEGRATION") else "WARN",
    )
    failures += _report(
        "external ATS automation is opt-in",
        "PASS" if not _setting_is_true(config, "EXTERNAL_ATS_ENABLED") else "FAIL",
    )
    failures += _report(
        "test mode is enabled by default",
        "PASS" if _setting_is_true(config, "TEST_MODE") else "WARN",
    )
    failures += _report(
        "runtime directories available",
        "PASS" if all((ROOT / name).is_dir() for name in ("data", "logs")) else "WARN",
    )

    print("DOCTOR PASS" if failures == 0 else f"DOCTOR COMPLETE ({failures} action(s) required)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
