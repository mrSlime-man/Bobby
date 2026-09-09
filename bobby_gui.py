#!/usr/bin/python3
"""GTK control panel for Bobby's existing production runner."""

from __future__ import annotations

import ast
import concurrent.futures
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application_tracker import (
    APPLICATION_STORE_PATH,
    APPLICATION_STATUSES,
    ApplicationTracker,
    NO_RESPONSE,
    REJECTED,
    quality_label,
)
from src.utils.search_pool import reset_search_pool
from src.utils.easy_apply_quota import (
    AVAILABLE as EASY_APPLY_QUOTA_AVAILABLE,
    BLOCKED as EASY_APPLY_QUOTA_BLOCKED,
    UNKNOWN as EASY_APPLY_QUOTA_UNKNOWN,
    easy_apply_quota_state,
)


REPOSITORY = Path(__file__).resolve().parent
RUNNER = REPOSITORY / "run_big_pool.fish"
APP_CONFIG = REPOSITORY / "config" / "app_config.py"
_LOG_ROOT = Path(os.getenv("BOBBY_LOG_DIR", str(Path.home() / "Logs")))
LATEST_LOG = Path(os.getenv("BOBBY_LATEST_LOG", str(_LOG_ROOT / "latest-remote.log")))
_RUNTIME_ROOT = Path(os.getenv("BOBBY_RUNTIME_DIR", tempfile.gettempdir()))
LOCK_FILE = Path(os.getenv("BOBBY_LOCK_FILE", str(_RUNTIME_ROOT / "bobby-production.lock")))
BOT_PID_FILE = Path(os.getenv("BOBBY_PID_FILE", str(_RUNTIME_ROOT / "bobby-production-bot.pid")))
DASHBOARD_STATE = REPOSITORY / "data" / "output" / "dashboard"
SNAPSHOT_FILE = DASHBOARD_STATE / "snapshot.json"
EVENTS_FILE = DASHBOARD_STATE / "events.jsonl"
ENCOUNTERED_JOBS_FILE = REPOSITORY / "data" / "output" / "linkedin" / "encountered_jobs.json"
SEARCH_PROFILES = (
    ("Remote USA", REPOSITORY / "config" / "search_config_remote.yaml"),
    ("Tampa Bay", REPOSITORY / "config" / "search_config_tampa.yaml"),
)

# GTK 3 keeps Bobby portable across the existing Linux desktop stack.  The
# stylesheet only uses GTK 3 properties so launching the control center does
# not depend on a custom theme or an additional UI runtime.
BOBBY_CSS = """
window.bobby-window { background: @theme_bg_color; }
.bobby-shell { background: @theme_bg_color; }
.bobby-header { padding: 14px 20px; border-bottom: 1px solid alpha(@theme_fg_color, 0.12); }
.bobby-brand { font-size: 20px; font-weight: 700; }
.bobby-subtitle { color: alpha(@theme_fg_color, 0.62); font-size: 11px; }
.bobby-sidebar { background: alpha(@theme_base_color, 0.72); border-right: 1px solid alpha(@theme_fg_color, 0.10); padding: 14px 10px; }
.bobby-sidebar-title { color: alpha(@theme_fg_color, 0.52); font-size: 10px; font-weight: 700; padding: 8px 10px 5px; }
.bobby-nav row { border-radius: 9px; margin: 2px 0; padding: 2px; }
.bobby-nav row:hover { background: alpha(@theme_selected_bg_color, 0.36); }
.bobby-nav row:selected { background: @theme_selected_bg_color; color: @theme_selected_fg_color; }
.bobby-nav row:selected image { color: @theme_selected_fg_color; }
.bobby-content { padding: 22px 26px 26px; }
.bobby-page-title { font-size: 26px; font-weight: 700; }
.bobby-page-subtitle { color: alpha(@theme_fg_color, 0.64); font-size: 12px; }
.bobby-card { background: @theme_base_color; border: 1px solid alpha(@theme_fg_color, 0.10); border-radius: 12px; padding: 16px; }
.bobby-card-muted { background: alpha(@theme_base_color, 0.55); border: 1px solid alpha(@theme_fg_color, 0.08); border-radius: 12px; padding: 14px; }
.bobby-card-title { font-weight: 700; font-size: 13px; }
.bobby-card-caption { color: alpha(@theme_fg_color, 0.58); font-size: 11px; }
.bobby-metric { background: @theme_base_color; border: 1px solid alpha(@theme_fg_color, 0.10); border-radius: 10px; padding: 12px; }
.bobby-metric-label { color: alpha(@theme_fg_color, 0.60); font-size: 10px; }
.bobby-metric-value { font-size: 22px; font-weight: 700; }
.bobby-status { border-radius: 10px; padding: 5px 11px; font-weight: 700; font-size: 11px; }
.bobby-status-running { background: #d9f5e5; color: #176b3a; }
.bobby-status-stopped { background: alpha(@theme_fg_color, 0.10); color: alpha(@theme_fg_color, 0.70); }
.bobby-status-draining { background: #fff0c2; color: #855b00; }
.bobby-status-error { background: #ffe1df; color: #9d2b24; }
.bobby-primary { padding: 8px 18px; border-radius: 9px; font-weight: 700; }
.bobby-secondary { padding: 8px 14px; border-radius: 9px; }
.bobby-section { margin-top: 6px; }
.bobby-code { font-family: monospace; font-size: 11px; }
.bobby-quality-neutral { background: #eceff1; color: #4f5b62; border-radius: 8px; padding: 3px 7px; font-weight: 700; }
.bobby-quality-cyan, .bobby-quality-diamond { background: #d6fbf5; color: #087f78; border-radius: 8px; padding: 3px 7px; font-weight: 700; }
.bobby-quality-green { background: #dff4e7; color: #176b3a; border-radius: 8px; padding: 3px 7px; font-weight: 700; }
.bobby-quality-yellow { background: #fff2c7; color: #855b00; border-radius: 8px; padding: 3px 7px; font-weight: 700; }
.bobby-quality-red { background: #ffe1df; color: #9d2b24; border-radius: 8px; padding: 3px 7px; font-weight: 700; }
.bobby-quality-grey { background: alpha(@theme_fg_color, 0.16); color: alpha(@theme_fg_color, 0.66); }
.bobby-application-row { padding: 8px; border-radius: 9px; }
.bobby-application-row:hover { background: alpha(@theme_selected_bg_color, 0.28); }
.bobby-application-row:selected { background: @theme_selected_bg_color; color: @theme_selected_fg_color; }
"""

PROFILE_SCALAR_PATHS = {
    "remote": ("remote",),
    "hybrid": ("hybrid",),
    "onsite": ("onsite",),
    "apply_once_at_company": ("apply_once_at_company",),
    **{
        f"experience_level.{key}": ("experience_level", key)
        for key in (
            "internship",
            "entry",
            "associate",
            "mid_senior_level",
            "director",
            "executive",
        )
    },
    **{
        f"job_types.{key}": ("job_types", key)
        for key in (
            "full_time",
            "contract",
            "part_time",
            "temporary",
            "volunteer",
            "internship",
            "other",
        )
    },
    **{
        f"date.{key}": ("date", key)
        for key in ("all_time", "month", "week", "24_hours")
    },
}
PROFILE_LIST_KEYS = {"positions", "locations"}
SUPPORTED_PROVIDER_TYPES = {
    "gemini",
    "openai",
    "claude",
    "ollama",
    "openrouter",
    "nvidia_nim",
    "groq",
    "cerebras",
    "openai_compatible",
}


@dataclass(frozen=True)
class SettingSpec:
    key: str
    label: str
    kind: str
    description: str
    minimum: int | float | None = None
    maximum: int | float | None = None
    choices: tuple[str, ...] = ()


APPLICATION_SETTINGS = (
    SettingSpec(
        "UPLOAD_RESUME",
        "Indeed Resume Upload",
        "bool",
        "Use Bobby's existing resume upload path for the Indeed adapter when that site is selected.",
    ),
    SettingSpec(
        "EASY_APPLY_ONLY_MODE",
        "Easy Apply Only Mode",
        "bool",
        "ON processes only LinkedIn Easy Apply jobs. OFF permits configured external paths.",
    ),
    SettingSpec(
        "MONKEY_MODE",
        "Monkey Mode",
        "bool",
        "Treat every discovered vacancy as interesting instead of applying the suitability gate.",
    ),
    SettingSpec(
        "TEST_MODE",
        "Test Mode",
        "bool",
        "Prepare forms and artifacts without submitting applications.",
    ),
    SettingSpec(
        "COLLECT_INFO_MODE",
        "Collect Information Only",
        "bool",
        "Collect vacancy information and statistics without applying.",
    ),
)

SEARCH_SETTINGS = (
    SettingSpec(
        "LINKEDIN_RECOMMENDED_JOBS_MODE",
        "LinkedIn Recommended Jobs",
        "bool",
        "Use LinkedIn's recommended collection instead of configured search terms.",
    ),
    SettingSpec(
        "LINKEDIN_TOP_APPLICANT_JOBS_MODE",
        "Top Applicant Picks",
        "bool",
        "Use LinkedIn's Top applicant picks collection instead of configured search terms.",
    ),
    SettingSpec(
        "MAX_APPLIES_NUM",
        "Maximum Applications",
        "int",
        "Run-wide application limit.",
        1,
        1000,
    ),
    SettingSpec(
        "JOB_IS_INTERESTING_THRESH",
        "Minimum Suitability Score",
        "int",
        "Minimum LLM interest score required before applying.",
        0,
        100,
    ),
    SettingSpec(
        "MINIMUM_WAIT_TIME_SEC",
        "Minimum Seconds Per Job",
        "int",
        "Minimum time Bobby spends processing one job.",
        0,
        3600,
    ),
)

SAFETY_SETTINGS = (
    SettingSpec(
        "DEBUG_MODE",
        "Diagnostic Browser Artifacts",
        "bool",
        "Capture Bobby's existing debug screenshots and traces for troubleshooting; off by default.",
    ),
    SettingSpec(
        "HEADLESS_MODE",
        "Headless Browser",
        "bool",
        "Run the browser without a visible window after Bobby restarts.",
    ),
    SettingSpec(
        "RESTART_EVERY_DAY",
        "Daily Search Guard",
        "bool",
        "Use Bobby's existing last-search-time guard for daily automation.",
    ),
    SettingSpec(
        "FREE_TIER",
        "Provider Rate Limiting",
        "bool",
        "Use Bobby's existing free-tier request pacing.",
    ),
    SettingSpec(
        "FREE_TIER_RPM_LIMIT",
        "Free-Tier Requests per Minute",
        "int",
        "Maximum requests in the existing rolling one-minute provider window.",
        1,
        600,
    ),
    SettingSpec(
        "DASHBOARD_OUTPUT_APP_LOGS",
        "Dashboard Console Logs",
        "bool",
        "Show existing application log output in the runtime dashboard console.",
    ),
    SettingSpec(
        "MINIMUM_LOG_LEVEL",
        "Minimum Log Level",
        "choice",
        "Lowest severity written by Bobby's configured logger.",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    ),
    SettingSpec(
        "GMAIL_APPLICATION_INTEGRATION",
        "Gmail Readonly Verification",
        "bool",
        "Use the existing Gmail connection for supported verification and submission receipts.",
    ),
    SettingSpec(
        "GMAIL_VERIFICATION_TIMEOUT_SEC",
        "Gmail Verification Wait Seconds",
        "int",
        "Maximum wait for the existing supported Gmail verification flow.",
        1,
        600,
    ),
    SettingSpec(
        "GMAIL_VERIFICATION_POLL_SEC",
        "Gmail Poll Interval Seconds",
        "int",
        "Interval between readonly Gmail checks; must fit within both wait limits.",
        1,
        60,
    ),
    SettingSpec(
        "GMAIL_RECEIPT_TIMEOUT_SEC",
        "Submission Receipt Wait Seconds",
        "int",
        "Maximum receipt wait after credible submission activity.",
        1,
        600,
    ),
)

MODEL_SETTINGS = (
    SettingSpec(
        "LLM_MODEL_TYPE",
        "Model Provider",
        "choice",
        "Provider supported by both application paths; its credentials must already be configured.",
        choices=("gemini", "openai", "claude", "ollama", "openrouter", "nvidia_nim", "groq", "cerebras", "openai_compatible"),
    ),
    SettingSpec(
        "EASY_APPLY_MODEL",
        "Easy Apply Model",
        "model",
        "Existing provider model identifier for LinkedIn applications and suitability.",
    ),
    SettingSpec(
        "APPLY_AGENT_MODEL",
        "External Application Model",
        "model",
        "Existing provider model identifier for supported external application flows.",
    ),
    SettingSpec(
        "APPLY_AGENT_FALLBACK_MODEL",
        "External Fallback Model",
        "optional_model",
        "Existing Gemini fallback model identifier; leave blank to disable the fallback.",
    ),
    SettingSpec(
        "TEMPERATURE",
        "Model Temperature",
        "float",
        "Existing model sampling setting; lower values produce more consistent answers.",
        0,
        1,
    ),
    SettingSpec(
        "RESUME_STYLE",
        "Generated Resume Style",
        "choice",
        "Style used only when Bobby generates a resume instead of using the configured source file.",
        choices=("FAANGPath", "Cloyola Grey", "Modern Blue", "Modern Grey", "Default", "Clean Blue"),
    ),
)

PROVIDER_SETTINGS = (
    SettingSpec(
        "LLM_PROVIDER_ORDER",
        "Provider Priority",
        "provider_order",
        "Comma-separated providers tried in order after a bounded provider failure. Only providers with existing credentials/configuration are valid at runtime.",
    ),
    SettingSpec(
        "LLM_SECONDARY_PROVIDER",
        "Secondary Provider",
        "choice",
        "Provider used for the existing bounded fallback slot; credentials are still required at runtime.",
        choices=tuple(sorted(SUPPORTED_PROVIDER_TYPES)),
    ),
    SettingSpec(
        "LLM_SECONDARY_MODEL",
        "Secondary Provider Model",
        "model",
        "Model identifier passed to the configured secondary provider.",
    ),
    SettingSpec(
        "LLM_FALLBACK_ENABLED",
        "Provider Fallback",
        "bool",
        "Allow one bounded switch to the next configured provider before an external application is submitted or uploaded.",
    ),
    SettingSpec(
        "LLM_PROVIDER_COOLDOWN_SEC",
        "Provider Cooldown Seconds",
        "int",
        "Cooldown duration for transient provider failures.",
        1,
        3600,
    ),
    SettingSpec(
        "LLM_PROVIDER_MAX_RETRIES",
        "Provider Attempts",
        "int",
        "Maximum distinct provider candidates for one external worker.",
        1,
        4,
    ),
)

EXTERNAL_ATS_SETTINGS = (
    SettingSpec(
        "EXTERNAL_ATS_ENABLED",
        "External ATS Applications",
        "bool",
        "Allow the external application worker for non-LinkedIn application sites.",
    ),
    SettingSpec(
        "EXTERNAL_ATS_MAX_RECOVERY_ATTEMPTS",
        "Maximum Recovery Attempts",
        "int",
        "Bounded validation/control repair attempts within one external application.",
        0,
        5,
    ),
    SettingSpec(
        "APPLY_AGENT_MAX_RETRIES",
        "External Agent Retries",
        "int",
        "Maximum retries used by Bobby's existing external agent wrapper.",
        0,
        5,
    ),
    SettingSpec(
        "APPLY_AGENT_RETRY_DELAY_SEC",
        "External Retry Delay Seconds",
        "int",
        "Delay between bounded external agent retries.",
        0,
        600,
    ),
    SettingSpec(
        "EXTERNAL_ATS_PAGE_TIMEOUT_SEC",
        "ATS Page Step Timeout Seconds",
        "int",
        "Maximum Browser Use step duration for a dynamic ATS page.",
        10,
        600,
    ),
    SettingSpec(
        "EXTERNAL_ATS_NAVIGATION_TIMEOUT_SEC",
        "ATS Navigation Wait Seconds",
        "int",
        "Bounded wait used by the external ATS navigation contract.",
        5,
        300,
    ),
    SettingSpec(
        "EXTERNAL_ATS_AUTO_ACCOUNT_CREATION",
        "Automatic ATS Account Creation",
        "bool",
        "Use the existing secure account-password helper when a site requires ordinary registration.",
    ),
    SettingSpec(
        "EXTERNAL_ATS_RESUME_UPLOAD_ENABLED",
        "External Resume Upload",
        "bool",
        "Permit the one-attempt canonical resume upload action.",
    ),
)

RESUME_SETTINGS = (
    SettingSpec(
        "READY_MADE_RESUME_PATH",
        "Canonical Resume Path",
        "path",
        "Single source used by LinkedIn Easy Apply and external ATS uploads. The file is validated without displaying its contents.",
    ),
)

ALL_SETTINGS = (
    APPLICATION_SETTINGS
    + SEARCH_SETTINGS
    + SAFETY_SETTINGS
    + MODEL_SETTINGS
    + PROVIDER_SETTINGS
    + EXTERNAL_ATS_SETTINGS
    + RESUME_SETTINGS
)
SETTING_BY_KEY = {spec.key: spec for spec in ALL_SETTINGS}


class ConfigError(ValueError):
    """Raised when a supported GUI setting cannot be safely persisted."""


class PythonConfigAdapter:
    """Read and atomically replace only explicitly owned top-level assignments."""

    def __init__(self, path: Path = APP_CONFIG) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        try:
            source = self.path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(self.path))
        except (OSError, SyntaxError) as exc:
            raise ConfigError(f"Could not read configuration: {exc}") from exc

        values: dict[str, Any] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or target.id not in SETTING_BY_KEY:
                continue
            try:
                value = ast.literal_eval(node.value)
            except (TypeError, ValueError):
                continue
            self._validate(target.id, value)
            values[target.id] = value
        return values

    @staticmethod
    def _validate(key: str, value: Any) -> None:
        spec = SETTING_BY_KEY.get(key)
        if spec is None:
            raise ConfigError(f"Unsupported setting: {key}")
        if spec.kind == "bool":
            if type(value) is not bool:
                raise ConfigError(f"{key} must be True or False")
            return
        if spec.kind in {"int", "float"}:
            if spec.kind == "int" and type(value) is not int:
                raise ConfigError(f"{key} must be an integer")
            if spec.kind == "float" and (
                type(value) not in {int, float} or not math.isfinite(value)
            ):
                raise ConfigError(f"{key} must be a finite number")
            if spec.minimum is not None and value < spec.minimum:
                raise ConfigError(f"{key} must be at least {spec.minimum}")
            if spec.maximum is not None and value > spec.maximum:
                raise ConfigError(f"{key} must be at most {spec.maximum}")
            return
        if spec.kind in {"model", "optional_model"}:
            if value == "" and spec.kind == "optional_model":
                return
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,199}", value):
                raise ConfigError(f"{key} must be a provider model identifier")
            return
        if spec.kind == "provider_order":
            if not isinstance(value, (tuple, list)) or not value:
                raise ConfigError(f"{key} must contain at least one provider")
            for provider in value:
                if not isinstance(provider, str) or not re.fullmatch(
                    r"[a-z][a-z0-9_]{1,39}", provider
                ):
                    raise ConfigError(f"{key} contains an invalid provider")
                if provider not in SUPPORTED_PROVIDER_TYPES:
                    raise ConfigError(f"{key} contains unsupported provider: {provider}")
            return
        if spec.kind == "path":
            if not isinstance(value, str) or not value or not value.startswith("/"):
                raise ConfigError(f"{key} must be an absolute path")
            if len(value) > 500:
                raise ConfigError(f"{key} is too long")
            return
        if spec.kind == "choice":
            if not isinstance(value, str) or value not in spec.choices:
                raise ConfigError(f"{key} must be one of: {', '.join(spec.choices)}")
            return
        raise ConfigError(f"Unsupported setting type for {key}")

    @staticmethod
    def _validate_combined(values: dict[str, Any]) -> None:
        if values.get("TEST_MODE") and values.get("COLLECT_INFO_MODE"):
            raise ConfigError("Choose either Test Mode or Collect Information Only")
        if values.get("LINKEDIN_RECOMMENDED_JOBS_MODE") and values.get("LINKEDIN_TOP_APPLICANT_JOBS_MODE"):
            raise ConfigError("Choose either Recommended Jobs or Top Applicant Picks")
        if values.get("GMAIL_APPLICATION_INTEGRATION"):
            interval = values.get("GMAIL_VERIFICATION_POLL_SEC", 1)
            for key in ("GMAIL_VERIFICATION_TIMEOUT_SEC", "GMAIL_RECEIPT_TIMEOUT_SEC"):
                if key in values and interval > values[key]:
                    raise ConfigError("Gmail poll interval must not exceed either Gmail wait limit")

    def save(self, changes: dict[str, Any]) -> dict[str, Any]:
        if not changes:
            return self.load()
        for key, value in changes.items():
            self._validate(key, value)
        self._validate_combined({**self.load(), **changes})

        try:
            original = self.path.read_text(encoding="utf-8")
            original_mode = self.path.stat().st_mode & 0o777
        except OSError as exc:
            raise ConfigError(f"Could not read configuration: {exc}") from exc

        lines = original.splitlines()
        for key, value in changes.items():
            assignment = re.compile(
                rf"^(\s*{re.escape(key)}\s*=\s*)([^#]*?)(\s*(?:#.*)?)$"
            )
            for index, line in enumerate(lines):
                match = assignment.match(line)
                if match:
                    lines[index] = f"{match.group(1)}{value!r}{match.group(3)}"
                    break
            else:
                raise ConfigError(f"Could not locate setting: {key}")

        candidate = "\n".join(lines) + ("\n" if original.endswith("\n") else "")
        try:
            compile(candidate, str(self.path), "exec")
        except SyntaxError as exc:
            raise ConfigError(f"Configuration would be invalid Python: {exc}") from exc

        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(candidate)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary_path, original_mode)
            os.replace(temporary_path, self.path)
        except OSError as exc:
            raise ConfigError(f"Could not save configuration: {exc}") from exc
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        return self.load()


class SearchProfileAdapter:
    """Safely edit only allowlisted values in one existing YAML profile."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        try:
            import yaml
        except ImportError as exc:
            raise ConfigError("Could not read search profile: PyYAML is unavailable") from exc
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"Could not read search profile: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError("Search profile must contain a YAML mapping")
        values: dict[str, Any] = {
            key: list(data.get(key) or []) for key in PROFILE_LIST_KEYS
        }
        for key, path in PROFILE_SCALAR_PATHS.items():
            current: Any = data
            for part in path:
                current = current.get(part) if isinstance(current, dict) else None
            values[key] = current
        self._validate_all(values)
        return values

    @staticmethod
    def _validate_text_list(key: str, value: Any) -> None:
        if not isinstance(value, list) or not value:
            raise ConfigError(f"{key} must contain at least one item")
        for item in value:
            if not isinstance(item, str) or not item.strip() or "\n" in item or "\r" in item:
                raise ConfigError(f"{key} contains an invalid item")

    @classmethod
    def _validate_one(cls, key: str, value: Any) -> None:
        if key in PROFILE_LIST_KEYS:
            cls._validate_text_list(key, value)
            return
        if key not in PROFILE_SCALAR_PATHS:
            raise ConfigError(f"Unsupported search profile setting: {key}")
        if type(value) is not bool:
            raise ConfigError(f"{key} must be true or false")

    @classmethod
    def _validate_all(cls, values: dict[str, Any]) -> None:
        for key, value in values.items():
            cls._validate_one(key, value)
        if sum(bool(values.get(f"date.{key}")) for key in ("all_time", "month", "week", "24_hours")) != 1:
            raise ConfigError("Exactly one date filter must be selected")
        if not any(bool(values.get(key)) for key in ("remote", "hybrid", "onsite")):
            raise ConfigError("At least one work type must be enabled")

    @staticmethod
    def _replace_list_block(lines: list[str], key: str, values: list[str]) -> list[str]:
        header_index = next(
            (index for index, line in enumerate(lines) if re.match(rf"^{re.escape(key)}\s*:", line)),
            None,
        )
        if header_index is None:
            raise ConfigError(f"Could not locate search profile setting: {key}")
        end_index = header_index + 1
        while end_index < len(lines):
            line = lines[end_index]
            if line and not line[0].isspace() and not line.startswith("- "):
                break
            end_index += 1
        header = lines[header_index]
        if not re.match(rf"^{re.escape(key)}\s*:\s*(?:#.*)?$", header):
            header = f"{key}:"
        replacement = [f"  - {json.dumps(item.strip(), ensure_ascii=False)}" for item in values]
        return lines[:header_index] + [header] + replacement + lines[end_index:]

    @staticmethod
    def _replace_scalar(lines: list[str], path: tuple[str, ...], value: bool) -> list[str]:
        start = 0
        end = len(lines)
        indent = ""
        if len(path) == 2:
            section = path[0]
            section_index = next(
                (index for index, line in enumerate(lines) if re.match(rf"^{re.escape(section)}\s*:", line)),
                None,
            )
            if section_index is None:
                raise ConfigError(f"Could not locate search profile section: {section}")
            start = section_index + 1
            end = start
            while end < len(lines) and (not lines[end] or lines[end][0].isspace()):
                end += 1
            indent = "  "
        key = path[-1]
        pattern = re.compile(
            rf"^({re.escape(indent)}{re.escape(key)}\s*:\s*)([^#]*?)(\s*(?:#.*)?)$"
        )
        for index in range(start, end):
            match = pattern.match(lines[index])
            if match:
                rendered = "true" if value else "false"
                lines[index] = f"{match.group(1)}{rendered}{match.group(3)}"
                return lines
        raise ConfigError(f"Could not locate search profile setting: {'.'.join(path)}")

    def _prepare_changes(self, changes: dict[str, Any]) -> tuple[str, int]:
        """Validate the entire proposed file before a profile can be written."""
        current = self.load()
        proposed = {**current, **changes}
        self._validate_all(proposed)
        try:
            original = self.path.read_text(encoding="utf-8")
            original_mode = self.path.stat().st_mode & 0o777
        except OSError as exc:
            raise ConfigError(f"Could not read search profile: {exc}") from exc

        lines = original.splitlines()
        for key, value in changes.items():
            self._validate_one(key, value)
            if key in PROFILE_LIST_KEYS:
                lines = self._replace_list_block(lines, key, value)
            else:
                lines = self._replace_scalar(lines, PROFILE_SCALAR_PATHS[key], value)
        candidate = "\n".join(lines) + ("\n" if original.endswith("\n") else "")
        try:
            import yaml
        except ImportError as exc:
            raise ConfigError("Search profile validation requires PyYAML") from exc
        try:
            parsed = yaml.safe_load(candidate)
            if not isinstance(parsed, dict):
                raise ConfigError("Search profile would not be a YAML mapping")
            expected = yaml.safe_load(original)
            for key, value in changes.items():
                if key in PROFILE_LIST_KEYS:
                    expected[key] = [item.strip() for item in value]
                else:
                    path = PROFILE_SCALAR_PATHS[key]
                    parent = expected
                    for part in path[:-1]:
                        parent = parent[part]
                    parent[path[-1]] = value
            if parsed != expected:
                raise ConfigError("Search profile update would alter unrelated settings")
        except yaml.YAMLError as exc:
            raise ConfigError(f"Search profile would be invalid YAML: {exc}") from exc
        return candidate, original_mode

    def validate_changes(self, changes: dict[str, Any]) -> None:
        self._prepare_changes(changes)

    def save(self, changes: dict[str, Any]) -> dict[str, Any]:
        if not changes:
            return self.load()
        candidate, original_mode = self._prepare_changes(changes)

        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(candidate)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary_path, original_mode)
            os.replace(temporary_path, self.path)
        except OSError as exc:
            raise ConfigError(f"Could not save search profile: {exc}") from exc
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        return self.load()


def parse_profile_lines(text: str) -> list[str]:
    """Keep each location or search term intact, including commas in city names."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def save_search_profile_changes(
    adapters: dict[str, SearchProfileAdapter], changes: dict[str, dict[str, Any]],
) -> None:
    """Preflight every profile before saving any of the separate profile files."""
    if set(adapters) != set(changes):
        raise ConfigError("All search profiles must be present before saving")
    for name, adapter in adapters.items():
        adapter.validate_changes(changes[name])
    for name, adapter in adapters.items():
        adapter.save(changes[name])


def read_easy_apply_only_mode(path: Path = APP_CONFIG) -> str:
    try:
        value = PythonConfigAdapter(path).load().get("EASY_APPLY_ONLY_MODE")
    except ConfigError:
        return "Unknown"
    return "True" if value is True else "False" if value is False else "Unknown"


def settings_saved_message(running: bool) -> str:
    if running:
        return "Saved — restart Bobby to apply these startup settings."
    return "Saved — takes effect on the next Bobby start."


def build_launch_command(
    smoke: bool = False, timeout_seconds: int | None = None
) -> list[str]:
    command = [
        "/usr/bin/systemd-inhibit",
        "--what=sleep:idle",
        "--why=LinkedIn Job Bot running",
        "/usr/bin/fish",
        str(RUNNER),
    ]
    if smoke:
        command.extend(["--timeout-seconds", str(timeout_seconds or 300)])
    return command


def _read_pid() -> int | None:
    try:
        value = int(BOT_PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(value, 0)
        return value
    except (OSError, TypeError, ValueError):
        return None


def _lock_is_held() -> bool:
    descriptor = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def bobby_is_running(process: subprocess.Popen[Any] | None = None) -> bool:
    if process is not None and process.poll() is None:
        return True
    return _read_pid() is not None or _lock_is_held()


class BobbyAlreadyRunning(RuntimeError):
    pass


def launch_bobby(
    smoke: bool = False,
    process: subprocess.Popen[Any] | None = None,
    timeout_seconds: int | None = None,
) -> subprocess.Popen[Any]:
    if bobby_is_running(process):
        raise BobbyAlreadyRunning("Bobby is already running")
    return subprocess.Popen(
        build_launch_command(smoke, timeout_seconds),
        cwd=REPOSITORY,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _parent_pid(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _command_line(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return ""


def find_runner_pid(bot_pid: int) -> int | None:
    """Find the nearest run_big_pool.fish ancestor without touching other terminals."""
    current = bot_pid
    for _ in range(12):
        parent = _parent_pid(current)
        if not parent or parent <= 1:
            return None
        if "run_big_pool.fish" in _command_line(parent):
            return parent
        current = parent
    return None


def request_graceful_stop(process: subprocess.Popen[Any] | None = None) -> bool:
    """Request Bobby's existing irreversible SIGINT drain; never force-kill it."""
    if process is not None and process.poll() is None:
        os.killpg(os.getpgid(process.pid), signal.SIGINT)
        return True

    bot_pid = _read_pid()
    if bot_pid is None:
        return False
    runner_pid = find_runner_pid(bot_pid)
    os.kill(runner_pid or bot_pid, signal.SIGINT)
    return True


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_runtime_snapshot() -> dict[str, Any]:
    return _read_json(SNAPSHOT_FILE)


def load_runtime_events(limit: int = 2000) -> list[dict[str, Any]]:
    try:
        lines = EVENTS_FILE.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def load_application_rows(
    tracker: ApplicationTracker | None = None, *, now: Any = None
) -> list[dict[str, Any]]:
    """Read the structured tracker; never reparse application logs for the GUI."""

    return (tracker or ApplicationTracker()).records(now=now)


def filter_application_rows(
    rows: list[dict[str, Any]], *, status: str = "All", quality: str = "All",
    sort_by: str = "newest", search: str = "", application_type: str = "All",
    ats: str = "All", account: str = "All", verification: str = "All",
    profile: str = "All", date_range: str = "All", now: Any = None,
) -> list[dict[str, Any]]:
    status = status or "All"
    quality = quality or "All"
    application_type = application_type or "All"
    ats = ats or "All"
    account = account or "All"
    verification = verification or "All"
    profile = profile or "All"
    date_range = date_range or "All"
    rows = list(rows)
    applied = {"IN_PROGRESS", "SUBMITTED", "UNVERIFIED_AFTER_SUBMIT"}
    if status == "Applied":
        rows = [row for row in rows if row.get("application_status") in applied]
    elif status == "Submitted":
        rows = [row for row in rows if row.get("application_status") in {"SUBMITTED", "UNVERIFIED_AFTER_SUBMIT"}]
    elif status == "Awaiting response":
        rows = [
            row for row in rows
            if row.get("application_status") in {"SUBMITTED", "UNVERIFIED_AFTER_SUBMIT"}
            and row.get("employer_response", NO_RESPONSE) == NO_RESPONSE
            and row.get("lifecycle_state") != "STALE_NO_RESPONSE"
        ]
    elif status == "Rejected":
        rows = [row for row in rows if row.get("employer_response") == REJECTED]
    elif status == "Needs Human":
        rows = [row for row in rows if row.get("application_status") == "NEEDS_HUMAN"]
    elif status == "Technical Failure":
        rows = [row for row in rows if row.get("application_status") == "TECHNICAL_FAILURE"]
    elif status == "Skipped":
        rows = [row for row in rows if row.get("application_status") == "SKIPPED"]
    elif status == "Easy Apply limit deferred":
        rows = [
            row
            for row in rows
            if row.get("application_status") == "DEFERRED_EASY_APPLY_LIMIT"
        ]
    if quality != "All":
        rows = [row for row in rows if row.get("quality_tier") == quality.upper()]
    if search.strip():
        needle = search.casefold().strip()
        rows = [
            row for row in rows
            if needle in " ".join(
                str(row.get(key) or "")
                for key in (
                    "job_title", "company_name", "location", "application_status",
                    "ats_family", "application_type", "search_profile", "account_email",
                )
            ).casefold()
        ]
    if application_type != "All":
        wanted = application_type.casefold()
        rows = [
            row for row in rows
            if (
                str(row.get("application_type") or "").casefold() == wanted
                or (wanted == "external ats" and str(row.get("application_type") or "").casefold() == "external_ats")
                or (wanted == "easy apply" and str(row.get("application_type") or "").casefold() == "easy_apply")
            )
        ]
    if ats != "All":
        rows = [row for row in rows if str(row.get("ats_family") or "").casefold() == ats.casefold()]
    if account != "All":
        if account == "Created":
            rows = [row for row in rows if row.get("account_created") is True]
        elif account == "Reused":
            rows = [row for row in rows if row.get("account_reused") is True]
        elif account == "Required":
            rows = [row for row in rows if row.get("account_required") is True]
        elif account == "Not used":
            rows = [row for row in rows if not row.get("account_required")]
    if verification != "All":
        state = str(verification).replace(" ", "_").upper()
        if state == "NOT_REQUIRED":
            rows = [row for row in rows if row.get("email_verification_state") in {None, "", "NOT_REQUIRED"}]
        elif state == "WAITING":
            rows = [row for row in rows if row.get("email_verification_state") in {"WAITING_FOR_EMAIL", "EMAIL_FOUND", "VERIFICATION_LINK_OPENED"}]
        elif state == "VERIFIED":
            rows = [row for row in rows if row.get("email_verification_state") == "VERIFIED"]
        elif state == "FAILED":
            rows = [row for row in rows if row.get("email_verification_state") in {"FAILED", "EXPIRED"}]
    if profile != "All":
        aliases = {
            "Remote USA": "remote",
            "Tampa Bay": "tampa",
        }
        expected = aliases.get(profile, profile).casefold()
        rows = [row for row in rows if str(row.get("search_profile") or "").casefold() == expected]
    if date_range != "All":
        reference = now
        try:
            reference_dt = reference if isinstance(reference, datetime) else datetime.fromisoformat(
                str(reference).replace("Z", "+00:00")
            ) if reference else datetime.now(timezone.utc)
            if reference_dt.tzinfo is None:
                reference_dt = reference_dt.replace(tzinfo=timezone.utc)
            reference_dt = reference_dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            reference_dt = datetime.now(timezone.utc)
        days = {"Today": 1, "Last 7 days": 7, "Last 30 days": 30}.get(date_range)
        def row_age(row: dict[str, Any]) -> float | None:
            value = row.get("last_status_at") or row.get("discovered_at")
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return (reference_dt - parsed.astimezone(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                return None
        if days is not None:
            rows = [row for row in rows if (row_age(row) is not None and 0 <= row_age(row) < days * 86400)]
        elif date_range == "Older":
            rows = [row for row in rows if (row_age(row) is not None and row_age(row) >= 30 * 86400)]
    key_map = {
        "newest": lambda row: row.get("discovered_at") or "",
        "oldest": lambda row: row.get("discovered_at") or "",
        "suitability": lambda row: row.get("suitability_score") if row.get("suitability_score") is not None else -1,
        "application date": lambda row: row.get("attempted_at") or row.get("submitted_at") or "",
        "company": lambda row: (row.get("company_name") or "").casefold(),
        "current status": lambda row: row.get("application_status") or "",
    }
    sort_key = key_map.get(sort_by.casefold(), key_map["newest"])
    rows.sort(key=lambda row: (sort_key(row), row.get("job_key") or ""), reverse=sort_by.casefold() not in {"oldest", "company", "current status"})
    return rows


def current_run_counters(
    snapshot: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, int]:
    run_id = snapshot.get("run_id")
    run_events = [event for event in events if run_id and event.get("run_id") == run_id]
    snapshot_counters = snapshot.get("counters") or {}
    counters = {
        "found": int(snapshot_counters.get("discovered") or 0),
        "encountered": int(snapshot_counters.get("encountered") or 0),
        "new": int(snapshot_counters.get("new") or 0),
        "attempted": 0,
        "easy_apply": 0,
        "easy_apply_deferred": 0,
        "external": 0,
        "submitted": 0,
        "unverified": 0,
        "technical": 0,
        "needs_human": 0,
        "cancelled": 0,
    }
    for event in run_events:
        event_type = event.get("type")
        payload = event.get("payload") or {}
        if event_type in {"job_encountered", "job_skipped_encountered"}:
            counters["encountered"] += 1
        if event_type == "job_discovered" and payload.get("is_new") is True:
            counters["new"] += 1
        if event_type == "easy_apply_started":
            counters["attempted"] += 1
            counters["easy_apply"] += 1
        elif event_type == "agent_apply_started":
            counters["attempted"] += 1
            counters["external"] += 1
        if event_type != "job_result":
            continue
        classification = str((event.get("payload") or {}).get("classification") or "").upper()
        target = {
            "SUBMITTED": "submitted",
            "UNVERIFIED_AFTER_SUBMIT": "unverified",
            "DEFERRED_EASY_APPLY_LIMIT": "easy_apply_deferred",
            "TECHNICAL_FAILURE": "technical",
            "NEEDS_HUMAN": "needs_human",
            "CANCELLED": "cancelled",
            "CANCELLED_BY_SHUTDOWN": "cancelled",
        }.get(classification)
        if target:
            counters[target] += 1
    return counters


def _safe_reason_category(classification: str, reason: str) -> str:
    normalized = reason.casefold()
    if classification != "NEEDS_HUMAN":
        return classification.replace("_", " ").title()
    if any(term in normalized for term in ("captcha", "2fa", "security", "verification")):
        return "Security verification"
    if any(term in normalized for term in ("no info", "unsupported", "source-backed", "experience")):
        return "Unsupported candidate fact"
    if any(term in normalized for term in ("validation", "required", "ambiguous")):
        return "Form validation or decision"
    return "Manual intervention"


def recent_terminal_events(
    snapshot: dict[str, Any], events: list[dict[str, Any]], limit: int = 8
) -> list[dict[str, str]]:
    run_id = snapshot.get("run_id")
    rows = []
    for event in reversed(events):
        if run_id and event.get("run_id") != run_id:
            continue
        if event.get("type") != "job_result":
            continue
        payload = event.get("payload") or {}
        classification = str(payload.get("classification") or "UNKNOWN").upper()
        rows.append(
            {
                "status": classification.replace("_AFTER_SUBMIT", "").replace("_", " "),
                "job": str(payload.get("job_title") or "Unknown job"),
                "company": str(payload.get("company_name") or "Unknown company"),
                "category": _safe_reason_category(
                    classification, str(payload.get("reason") or "")
                ),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def format_runtime_duration(
    started_at: Any, running: bool, finished_at: Any = None
) -> str:
    if not started_at:
        return "—"
    try:
        started = datetime.fromisoformat(str(started_at))
        finished = (
            datetime.now(tz=started.tzinfo)
            if running
            else datetime.fromisoformat(str(finished_at)) if finished_at else started
        )
        seconds = max(0, int((finished - started).total_seconds()))
    except (TypeError, ValueError):
        return "—"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def load_search_profile_summaries() -> list[str]:
    try:
        import yaml
    except ImportError:
        return ["Search profile summaries unavailable (PyYAML missing)."]
    summaries = []
    for name, path in SEARCH_PROFILES:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            summaries.append(f"{name}: unavailable")
            continue
        modes = [mode.title() for mode in ("remote", "hybrid", "onsite") if data.get(mode)]
        locations = data.get("locations") or []
        summaries.append(
            f"{name}: {', '.join(modes) or 'no work mode'} · "
            f"{', '.join(map(str, locations)) or 'no location'} · "
            f"{len(data.get('positions') or [])} search terms"
        )
    return summaries


def main() -> int:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk, Pango

    class BobbyWindow(Gtk.Window):
        def __init__(self) -> None:
            super().__init__(title="Bobby")
            self.set_default_size(1280, 820)
            self.set_size_request(1050, 700)
            self.get_style_context().add_class("bobby-window")
            self.process: subprocess.Popen[Any] | None = None
            self.draining = False
            self.config_adapter = PythonConfigAdapter()
            self.profile_adapters = {
                name: SearchProfileAdapter(path) for name, path in SEARCH_PROFILES
            }
            self.profile_widgets: dict[str, dict[str, Any]] = {}
            self.setting_widgets: dict[str, Any] = {}
            self.application_tracker = ApplicationTracker()
            self.application_rows_signature: tuple[Any, ...] = ()
            self.application_selected_key: str | None = None
            self.application_records: dict[str, dict[str, Any]] = {}
            self.gmail_poll_future: Any = None
            self.gmail_poll_started_at = 0.0
            self.settings_notices: list[Any] = []
            self.dirty_settings: set[str] = set()
            self.loading_settings = False
            self.log_path: Path | None = None
            self.log_offset = 0
            self.log_paused = False
            self.last_event_signature: tuple[tuple[str, str, str, str], ...] = ()
            self.log_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            self.log_read_pending = False
            self.log_read_lock = threading.Lock()
            self.page_names: list[str] = []

            provider = Gtk.CssProvider()
            try:
                provider.load_from_data(BOBBY_CSS.encode("utf-8"))
                Gtk.StyleContext.add_provider_for_screen(
                    self.get_screen(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
            except (GLib.Error, TypeError):
                # A system theme must never prevent the control layer from opening.
                pass

            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            root.get_style_context().add_class("bobby-shell")
            self.add(root)

            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
            header.get_style_context().add_class("bobby-header")
            brand_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            brand = Gtk.Label(label="Bobby", xalign=0)
            brand.get_style_context().add_class("bobby-brand")
            brand_box.pack_start(brand, False, False, 0)
            self.header_subtitle = Gtk.Label(label="Job search control center", xalign=0)
            self.header_subtitle.get_style_context().add_class("bobby-subtitle")
            brand_box.pack_start(self.header_subtitle, False, False, 0)
            header.pack_start(brand_box, False, False, 0)
            spacer = Gtk.Box()
            header.pack_start(spacer, True, True, 0)
            self.header_status = Gtk.Label(label="STOPPED")
            self.header_status.get_style_context().add_class("bobby-status")
            self.header_status.get_style_context().add_class("bobby-status-stopped")
            header.pack_end(self.header_status, False, False, 0)
            root.pack_start(header, False, False, 0)

            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            self.navigation = Gtk.ListBox()
            self.navigation.get_style_context().add_class("bobby-nav")
            self.navigation.set_selection_mode(Gtk.SelectionMode.SINGLE)
            self.navigation.set_size_request(220, -1)
            self.navigation.set_activate_on_single_click(True)
            self.navigation.connect("row-selected", self._select_navigation)
            sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            sidebar.get_style_context().add_class("bobby-sidebar")
            sidebar.pack_start(
                Gtk.Label(label="WORKSPACE", xalign=0), False, False, 0
            )
            sidebar.get_children()[0].get_style_context().add_class("bobby-sidebar-title")
            sidebar.pack_start(self.navigation, True, True, 0)
            content.pack_start(sidebar, False, False, 0)
            self.stack = Gtk.Stack()
            self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
            self.stack.set_transition_duration(220)
            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            content_box.get_style_context().add_class("bobby-content")
            content_box.pack_start(self.stack, True, True, 0)
            content.pack_start(content_box, True, True, 0)
            root.pack_start(content, True, True, 0)

            self._build_dashboard_tab()
            self._build_applications_tab()
            self._build_settings_tab("Application Settings", APPLICATION_SETTINGS)
            self._build_search_profiles_tab()
            self._build_settings_tab("Filters", SEARCH_SETTINGS, search_summary=True)
            self._build_settings_tab("AI & Providers", MODEL_SETTINGS + PROVIDER_SETTINGS)
            self._build_settings_tab("External ATS", EXTERNAL_ATS_SETTINGS)
            self._build_resume_tab()
            self._build_settings_tab("Automation", SAFETY_SETTINGS)
            self._build_safety_tab()
            self._build_notifications_tab()
            self._build_advanced_tab()
            self._build_runtime_tab()
            self._build_logs_tab()
            self._load_settings()
            self._load_search_profiles()

            self.connect("destroy", self._on_destroy)
            GLib.timeout_add_seconds(1, self.refresh)
            self.refresh()
            if os.environ.get("BOBBY_GUI_SMOKE") == "1":
                GLib.timeout_add(120, self._run_gui_smoke)

        def _on_destroy(self, _window: Any) -> None:
            self.log_executor.shutdown(wait=False, cancel_futures=True)
            Gtk.main_quit()

        def _run_gui_smoke(self) -> bool:
            """Exercise page construction/navigation without starting Bobby."""
            self.resize(1280, 820)
            for row in self.navigation.get_children():
                self.navigation.select_row(row)
                while Gtk.events_pending():
                    Gtk.main_iteration_do(False)
            GLib.timeout_add(120, Gtk.main_quit)
            return False

        def _select_navigation(self, _listbox: Any, row: Any) -> None:
            if row is not None:
                self.stack.set_visible_child_name(row.get_name())

        def _append_tab(self, child: Any, label: str) -> None:
            name = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")
            self.stack.add_named(child, name)
            row = Gtk.ListBoxRow()
            row.set_name(name)
            icon_names = {
                "Dashboard": "view-grid-symbolic",
                "Applications": "document-send-symbolic",
                "Application Settings": "document-properties-symbolic",
                "Search Profiles": "system-search-symbolic",
                "Filters": "funnel-symbolic",
                "AI & Providers": "network-server-symbolic",
                "External ATS": "applications-internet-symbolic",
                "Resume": "x-office-document-symbolic",
                "Automation": "preferences-system-symbolic",
                "Safety": "security-high-symbolic",
                "Notifications": "preferences-desktop-notification-symbolic",
                "Runtime": "speedometer-symbolic",
                "Logs": "text-x-generic-symbolic",
                "Advanced": "preferences-other-symbolic",
            }
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            box.set_border_width(7)
            image = Gtk.Image.new_from_icon_name(
                icon_names.get(label, "application-x-executable-symbolic"), Gtk.IconSize.MENU
            )
            box.pack_start(image, False, False, 0)
            box.pack_start(Gtk.Label(label=label, xalign=0), True, True, 0)
            row.add(box)
            self.navigation.add(row)
            self.page_names.append(label)
            if self.navigation.get_selected_row() is None:
                self.navigation.select_row(row)

        def _build_dashboard_tab(self) -> None:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
            page.set_border_width(2)

            title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            title = Gtk.Label(label="Dashboard", xalign=0)
            title.get_style_context().add_class("bobby-page-title")
            title_box.pack_start(title, False, False, 0)
            subtitle = Gtk.Label(
                label="A calm overview of Bobby’s current search and application run.", xalign=0
            )
            subtitle.get_style_context().add_class("bobby-page-subtitle")
            title_box.pack_start(subtitle, False, False, 0)
            page.pack_start(title_box, False, False, 0)

            overview = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            status_card = self._make_card()
            status_grid = Gtk.Grid(column_spacing=18, row_spacing=7)
            self.status_label = Gtk.Label(xalign=0)
            self.status_label.set_markup("<b>Status</b>  STOPPED")
            self.mode_label = Gtk.Label(xalign=0)
            self.quota_status_label = Gtk.Label(xalign=0)
            self.run_id_label = Gtk.Label(xalign=0)
            self.runtime_label = Gtk.Label(xalign=0)
            self.latest_log_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
            for row, widget in enumerate(
                (
                    self.status_label,
                    self.mode_label,
                    self.quota_status_label,
                    self.run_id_label,
                    self.runtime_label,
                    self.latest_log_label,
                )
            ):
                widget.set_selectable(True)
                status_grid.attach(widget, 0, row, 1, 1)
            status_card.pack_start(self._card_heading("Current run"), False, False, 0)
            status_card.pack_start(status_grid, False, False, 10)
            overview.pack_start(status_card, True, True, 0)

            activity_card = self._make_card()
            activity_card.pack_start(self._card_heading("Current activity"), False, False, 0)
            self.activity_label = Gtk.Label(label="No active job", xalign=0, yalign=0)
            activity_card.pack_start(self.activity_label, False, False, 9)
            overview.pack_start(activity_card, True, True, 0)
            page.pack_start(overview, False, False, 0)

            controls_card = self._make_card()
            controls_card.pack_start(self._card_heading("Run controls"), False, False, 0)
            controls = Gtk.Box(spacing=9)
            self.start_buttons = []
            for label, smoke, timeout in (
                ("Start Bobby", False, None),
                ("5 Minute Smoke", True, 300),
                ("10 Minute Audit", True, 600),
            ):
                button = Gtk.Button(label=label)
                button.get_style_context().add_class(
                    "bobby-primary" if not smoke else "bobby-secondary"
                )
                button.connect(
                    "clicked",
                    lambda _button, value=smoke, seconds=timeout: self.start_bobby(value, seconds),
                )
                self.start_buttons.append(button)
                controls.pack_start(button, False, False, 0)
            self.stop_button = Gtk.Button(label="Stop Gracefully")
            self.stop_button.get_style_context().add_class("bobby-secondary")
            self.stop_button.connect("clicked", self.stop_bobby)
            controls.pack_start(self.stop_button, False, False, 0)
            open_button = Gtk.Button(label="Open Latest Log")
            open_button.get_style_context().add_class("bobby-secondary")
            open_button.connect("clicked", self.open_latest_log)
            controls.pack_start(open_button, False, False, 0)
            controls_card.pack_start(controls, False, False, 10)
            self.dashboard_notice = Gtk.Label(xalign=0)
            self.dashboard_notice.set_line_wrap(True)
            self.dashboard_notice.get_style_context().add_class("bobby-card-caption")
            controls_card.pack_start(self.dashboard_notice, False, False, 0)
            page.pack_start(controls_card, False, False, 0)

            counter_card = self._make_card()
            counter_card.pack_start(self._card_heading("Run metrics"), False, False, 0)
            counter_grid = Gtk.Grid(column_spacing=10, row_spacing=10)
            self.counter_labels = {}
            labels = (
                ("found", "Jobs found"),
                ("new", "New"),
                ("attempted", "Attempted"),
                ("easy_apply", "Easy Apply"),
                ("easy_apply_deferred", "EA quota deferred"),
                ("external", "External ATS"),
                ("submitted", "Submitted"),
                ("unverified", "Unverified"),
                ("needs_human", "Needs human"),
                ("technical", "Technical failures"),
                ("cancelled", "Cancelled"),
            )
            for index, (key, label) in enumerate(labels):
                metric = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                metric.get_style_context().add_class("bobby-metric")
                name = Gtk.Label(label=label, xalign=0)
                name.get_style_context().add_class("bobby-metric-label")
                value = Gtk.Label(label="0", xalign=0)
                value.get_style_context().add_class("bobby-metric-value")
                metric.pack_start(name, False, False, 0)
                metric.pack_start(value, False, False, 0)
                counter_grid.attach(metric, index % 5, index // 5, 1, 1)
                self.counter_labels[key] = value
            counter_card.pack_start(counter_grid, False, False, 10)
            page.pack_start(counter_card, False, False, 0)
            self._append_tab(page, "Dashboard")

        @staticmethod
        def _make_card() -> Any:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
            card.get_style_context().add_class("bobby-card")
            return card

        @staticmethod
        def _card_heading(text: str) -> Any:
            label = Gtk.Label(label=text, xalign=0)
            label.get_style_context().add_class("bobby-card-title")
            return label

        def _build_applications_tab(self) -> None:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            page.set_border_width(2)
            heading = Gtk.Label(label="Applications", xalign=0)
            heading.get_style_context().add_class("bobby-page-title")
            page.pack_start(heading, False, False, 0)
            subtitle = Gtk.Label(
                label="Track Bobby’s durable job lifecycle, employer responses, and safe application links.",
                xalign=0,
            )
            subtitle.get_style_context().add_class("bobby-page-subtitle")
            page.pack_start(subtitle, False, False, 0)

            filters = Gtk.Grid(column_spacing=8, row_spacing=6)
            filters.get_style_context().add_class("bobby-card-muted")

            def filter_box(label: str, widget: Any) -> Any:
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
                caption = Gtk.Label(label=label, xalign=0)
                caption.get_style_context().add_class("bobby-card-caption")
                box.pack_start(caption, False, False, 0)
                box.pack_start(widget, False, False, 0)
                return box

            self.application_search = Gtk.SearchEntry()
            self.application_search.set_placeholder_text("Search company, role, ATS, or status")
            self.application_search.set_width_chars(28)
            self.application_search.connect("search-changed", lambda *_args: self._refresh_applications(force=True))
            filters.attach(filter_box("Search", self.application_search), 0, 0, 2, 1)
            self.application_status_filter = Gtk.ComboBoxText()
            for value in (
                "All", "Applied", "Submitted", "Awaiting response", "Rejected",
                "Needs Human", "Technical Failure", "Skipped",
                "Easy Apply limit deferred",
            ):
                self.application_status_filter.append_text(value)
            self.application_status_filter.set_active(0)
            self.application_status_filter.connect("changed", lambda *_args: self._refresh_applications(force=True))
            filters.attach(filter_box("Status", self.application_status_filter), 2, 0, 1, 1)
            self.application_quality_filter = Gtk.ComboBoxText()
            for value in ("All", "Cyan", "Green", "Yellow", "Red", "Neutral"):
                self.application_quality_filter.append_text(value)
            self.application_quality_filter.set_active(0)
            self.application_quality_filter.connect("changed", lambda *_args: self._refresh_applications(force=True))
            filters.attach(filter_box("Match band", self.application_quality_filter), 3, 0, 1, 1)
            self.application_type_filter = Gtk.ComboBoxText()
            for value in ("All", "External ATS", "Easy Apply"):
                self.application_type_filter.append_text(value)
            self.application_type_filter.set_active(0)
            self.application_type_filter.connect("changed", lambda *_args: self._refresh_applications(force=True))
            filters.attach(filter_box("Application type", self.application_type_filter), 4, 0, 1, 1)
            self.application_sort = Gtk.ComboBoxText()
            for value in ("newest", "oldest", "suitability", "application date", "company", "current status"):
                self.application_sort.append_text(value)
            self.application_sort.set_active(0)
            self.application_sort.connect("changed", lambda *_args: self._refresh_applications(force=True))
            filters.attach(filter_box("Sort", self.application_sort), 5, 0, 1, 1)

            self.application_ats_filter = Gtk.ComboBoxText()
            for value in ("All", "Workday", "Greenhouse", "Lever", "SmartRecruiters", "Generic"):
                self.application_ats_filter.append_text(value)
            self.application_ats_filter.set_active(0)
            self.application_ats_filter.connect("changed", lambda *_args: self._refresh_applications(force=True))
            filters.attach(filter_box("ATS", self.application_ats_filter), 0, 1, 1, 1)
            self.application_account_filter = Gtk.ComboBoxText()
            for value in ("All", "Created", "Reused", "Required", "Not used"):
                self.application_account_filter.append_text(value)
            self.application_account_filter.set_active(0)
            self.application_account_filter.connect("changed", lambda *_args: self._refresh_applications(force=True))
            filters.attach(filter_box("Account", self.application_account_filter), 1, 1, 1, 1)
            self.application_verification_filter = Gtk.ComboBoxText()
            for value in ("All", "Verified", "Waiting", "Failed", "Not required"):
                self.application_verification_filter.append_text(value)
            self.application_verification_filter.set_active(0)
            self.application_verification_filter.connect("changed", lambda *_args: self._refresh_applications(force=True))
            filters.attach(filter_box("Email verification", self.application_verification_filter), 2, 1, 1, 1)
            self.application_profile_filter = Gtk.ComboBoxText()
            for value in ("All", "Remote USA", "Tampa Bay"):
                self.application_profile_filter.append_text(value)
            self.application_profile_filter.set_active(0)
            self.application_profile_filter.connect("changed", lambda *_args: self._refresh_applications(force=True))
            filters.attach(filter_box("Search profile", self.application_profile_filter), 3, 1, 1, 1)
            self.application_date_filter = Gtk.ComboBoxText()
            for value in ("All", "Today", "Last 7 days", "Last 30 days", "Older"):
                self.application_date_filter.append_text(value)
            self.application_date_filter.set_active(0)
            self.application_date_filter.connect("changed", lambda *_args: self._refresh_applications(force=True))
            filters.attach(filter_box("Date", self.application_date_filter), 4, 1, 1, 1)

            clear_filters = Gtk.Button(label="Clear filters")
            clear_filters.get_style_context().add_class("bobby-secondary")
            clear_filters.connect("clicked", self._clear_application_filters)
            filters.attach(clear_filters, 5, 1, 1, 1)
            page.pack_start(filters, False, False, 0)

            toolbar = Gtk.Box(spacing=8)
            reload_button = Gtk.Button(label="Refresh")
            reload_button.connect("clicked", lambda *_args: self._refresh_applications(force=True))
            toolbar.pack_start(reload_button, False, False, 0)
            reevaluate = Gtk.Button(label="Re-evaluate Skipped")
            reevaluate.connect("clicked", lambda *_args: self._confirm_tracker_reset(skipped_only=True))
            toolbar.pack_start(reevaluate, False, False, 0)
            reset = Gtk.Button(label="Reset Search Pool")
            reset.get_style_context().add_class("bobby-secondary")
            reset.connect("clicked", lambda *_args: self._confirm_tracker_reset(skipped_only=False))
            toolbar.pack_start(reset, False, False, 0)
            page.pack_start(toolbar, False, False, 0)

            self.gmail_status_label = Gtk.Label(label="Gmail: checking configuration…", xalign=0)
            self.gmail_status_label.get_style_context().add_class("bobby-card-caption")
            page.pack_start(self.gmail_status_label, False, False, 0)
            self.application_quota_label = Gtk.Label(xalign=0)
            self.application_quota_label.get_style_context().add_class("bobby-card-caption")
            page.pack_start(self.application_quota_label, False, False, 0)

            paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            list_scroller = Gtk.ScrolledWindow()
            list_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.application_list = Gtk.ListBox()
            self.application_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
            self.application_list.connect("row-selected", self._application_selected)
            list_scroller.add(self.application_list)
            paned.pack1(list_scroller, True, False)

            detail_scroller = Gtk.ScrolledWindow()
            detail_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.application_detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=14)
            detail_scroller.add(self.application_detail)
            paned.pack2(detail_scroller, True, False)
            page.pack_start(paned, True, True, 0)
            self.application_notice = Gtk.Label(xalign=0)
            self.application_notice.set_property("wrap", True)
            self.application_notice.get_style_context().add_class("bobby-card-caption")
            page.pack_start(self.application_notice, False, False, 0)
            self._append_tab(page, "Applications")

        def _clear_application_filters(self, _button: Any = None) -> None:
            self.application_search.set_text("")
            for widget in (
                self.application_status_filter,
                self.application_quality_filter,
                self.application_type_filter,
                self.application_sort,
                self.application_ats_filter,
                self.application_account_filter,
                self.application_verification_filter,
                self.application_profile_filter,
                self.application_date_filter,
            ):
                widget.set_active(0)
            self._refresh_applications(force=True)

        def _application_selected(self, _listbox: Any, row: Any) -> None:
            record = getattr(row, "application_record", None) if row is not None else None
            self.application_selected_key = record.get("job_key") if record else None
            self._show_application_detail(record)

        def _show_application_detail(self, record: dict[str, Any] | None) -> None:
            for child in self.application_detail.get_children():
                self.application_detail.remove(child)
            if not record:
                self.application_detail.pack_start(
                    Gtk.Label(label="Select a job to see its safe details.", xalign=0),
                    False, False, 0,
                )
                self.application_detail.show_all()
                return
            title = Gtk.Label(label=record.get("job_title") or "Unknown job", xalign=0)
            title.get_style_context().add_class("bobby-card-title")
            self.application_detail.pack_start(title, False, False, 0)
            score = record.get("suitability_score")
            status = record.get("application_status") or "DISCOVERED"
            display_status = (
                "Easy Apply limit — deferred"
                if status == "DEFERRED_EASY_APPLY_LIMIT"
                else status
            )
            employer = record.get("employer_response") or NO_RESPONSE
            grey = record.get("lifecycle_state") in {"REJECTED_GREY", "STALE_NO_RESPONSE"}
            account_state = (
                "Created" if record.get("account_created") else
                "Reused" if record.get("account_reused") else
                "Required" if record.get("account_required") else "Not used"
            )
            details = (
                f"Application ID: {record.get('application_id') or record.get('job_key') or '—'}\n"
                f"{record.get('company_name') or 'Unknown company'} · {record.get('location') or 'Remote / location unknown'}\n"
                f"Quality: {quality_label(score)}  score={score if score is not None else '—'}\n"
                f"Application: {display_status}\nEmployer response: {employer}\n"
                f"Lifecycle: {'GREY / inactive' if grey else 'Active'}\n"
                f"ATS: {record.get('ats_family') or 'Unknown'} · Type: {record.get('application_type') or 'Unknown'}\n"
                f"Source: {record.get('source') or 'Unknown'} · Profile: {record.get('search_profile') or 'Unknown'}\n"
                f"Work location: {record.get('remote_state') or 'Unknown'}\n"
                f"Account: {account_state} · {record.get('account_email') or '—'}\n"
                f"Credential reference: {record.get('credential_ref') or '—'}\n"
                f"Email verification: {str(record.get('email_verification_state') or 'NOT_REQUIRED').replace('_', ' ').title()}\n"
                f"Last workflow step: {record.get('last_workflow_step') or '—'}\n"
                f"Discovered: {record.get('discovered_at') or '—'}\n"
                f"Attempted: {record.get('attempted_at') or '—'}\n"
                f"Submitted: {record.get('submitted_at') or record.get('credible_submission_at') or '—'}\n"
                f"Last update: {record.get('last_status_at') or '—'}\n"
                f"Result: {record.get('result') or status}\n"
                f"Failure category: {record.get('failure_category') or '—'}\n"
                f"Reason: {record.get('suitability_reason') or record.get('failure_reason') or '—'}"
            )
            body = Gtk.Label(label=details, xalign=0, yalign=0)
            body.set_property("wrap", True)
            body.set_selectable(True)
            self.application_detail.pack_start(body, False, False, 0)
            links = Gtk.Box(spacing=8)
            for label, key in (("LinkedIn", "linkedin_url"), ("External Application", "external_url")):
                url = record.get(key) or ""
                if url:
                    link = Gtk.LinkButton.new_with_label(url, label)
                    links.pack_start(link, False, False, 0)
            if links.get_children():
                self.application_detail.pack_start(links, False, False, 0)
            gmail = record.get("last_gmail_evidence")
            if gmail:
                evidence = Gtk.Label(
                    label=f"Last Gmail evidence: {gmail.get('kind', 'response')} · {gmail.get('received_at') or '—'} · confidence {gmail.get('confidence', '—')}",
                    xalign=0,
                )
                evidence.set_property("wrap", True)
                self.application_detail.pack_start(evidence, False, False, 0)
            self.application_detail.show_all()

        def _confirm_tracker_reset(self, *, skipped_only: bool) -> None:
            if bobby_is_running(self.process):
                self.application_notice.set_text("Stop Bobby before resetting the search pool; no live run was changed.")
                return
            action = "re-evaluate skipped/discovery state" if skipped_only else "reset the search pool"
            dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.NONE,
                text=f"Confirm: {action}?",
            )
            dialog.format_secondary_text(
                "This preserves submitted and unverified applications and the durable duplicate-submit ledger. "
                "A recoverable backup of encountered discovery state is kept." if not skipped_only else
                "Submitted and unverified applications remain protected and are not cleared."
            )
            dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
            dialog.add_button("Confirm", Gtk.ResponseType.OK)
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.OK:
                return
            try:
                if skipped_only:
                    result = self.application_tracker.reset_discovery(skipped_only=True)
                else:
                    result = reset_search_pool(
                        ENCOUNTERED_JOBS_FILE,
                        tracker=self.application_tracker,
                        confirmed=True,
                    )
                self.application_notice.set_text(
                    f"Reset complete: {result.get('resettable', 0)} reconsiderable records; "
                    f"{result.get('protected', 0)} protected submission records preserved."
                )
                self._refresh_applications(force=True)
            except (OSError, ValueError, PermissionError) as exc:
                self.application_notice.set_text(f"Reset was not completed: {type(exc).__name__}")

        def _gmail_configuration(self) -> tuple[bool, str, str]:
            try:
                from config.app_config import GMAIL_APPLICATION_INTEGRATION, GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH
                configured = bool(
                    GMAIL_APPLICATION_INTEGRATION
                    and Path(GMAIL_CREDENTIALS_PATH).is_file()
                    and Path(GMAIL_TOKEN_PATH).is_file()
                )
                return configured, GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH
            except (ImportError, AttributeError, TypeError):
                return False, "", ""

        def _poll_gmail_worker(self, credentials_path: str, token_path: str) -> dict[str, int]:
            from src.integrations.gmail_verification import GmailVerificationClient
            client = GmailVerificationClient(credentials_path, token_path)
            client.connect(interactive=False)
            return client.scan_application_statuses(self.application_tracker)

        def _refresh_gmail_status(self) -> None:
            configured, credentials_path, token_path = self._gmail_configuration()
            if not configured:
                self.gmail_status_label.set_text("Gmail: Not configured")
                self.gmail_poll_future = None
                return
            if self.gmail_poll_future is not None and self.gmail_poll_future.done():
                try:
                    result = self.gmail_poll_future.result()
                    self.gmail_status_label.set_text(
                        f"Gmail: connected · last scan {result.get('scanned', 0)} messages · "
                        f"matched {result.get('matched', 0)} · ambiguous {result.get('ambiguous', 0)}"
                    )
                except Exception:
                    self.gmail_status_label.set_text("Gmail: configured but currently unavailable")
                self.gmail_poll_future = None
            if self.gmail_poll_future is None and time.monotonic() - self.gmail_poll_started_at >= 60:
                self.gmail_poll_started_at = time.monotonic()
                self.gmail_status_label.set_text("Gmail: scanning recent status messages…")
                self.gmail_poll_future = self.log_executor.submit(
                    self._poll_gmail_worker, credentials_path, token_path
                )

        def _refresh_applications(self, *, force: bool = False) -> None:
            if hasattr(self, "application_quota_label"):
                self.application_quota_label.set_text(self._easy_apply_quota_label())
            rows = load_application_rows(self.application_tracker, now=datetime.now(timezone.utc))
            status = self.application_status_filter.get_active_text() or "All"
            quality = self.application_quality_filter.get_active_text() or "All"
            sort_by = self.application_sort.get_active_text() or "newest"
            rows = filter_application_rows(
                rows,
                status=status,
                quality=quality,
                sort_by=sort_by,
                search=self.application_search.get_text(),
                application_type=self.application_type_filter.get_active_text() or "All",
                ats=self.application_ats_filter.get_active_text() or "All",
                account=self.application_account_filter.get_active_text() or "All",
                verification=self.application_verification_filter.get_active_text() or "All",
                profile=self.application_profile_filter.get_active_text() or "All",
                date_range=self.application_date_filter.get_active_text() or "All",
                now=datetime.now(timezone.utc),
            )
            signature = tuple(
                (row.get("job_key"), row.get("application_status"), row.get("employer_response"),
                 row.get("lifecycle_state"), row.get("suitability_score"), row.get("last_status_at"))
                for row in rows
            )
            if not force and signature == self.application_rows_signature:
                self._refresh_gmail_status()
                return
            self.application_rows_signature = signature
            self.application_records = {row["job_key"]: row for row in rows}
            selected = self.application_selected_key
            for child in self.application_list.get_children():
                self.application_list.remove(child)
            for record in rows:
                row = Gtk.ListBoxRow()
                row.application_record = record
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
                box.get_style_context().add_class("bobby-application-row")
                top = Gtk.Box(spacing=8)
                title = Gtk.Label(label=record.get("job_title") or "Unknown job", xalign=0)
                title.set_ellipsize(3)
                top.pack_start(title, True, True, 0)
                badge = Gtk.Label(label=quality_label(record.get("suitability_score")), xalign=0)
                badge.get_style_context().add_class("bobby-quality-" + str(record.get("quality_tier") or "red").casefold())
                if record.get("lifecycle_state") in {"REJECTED_GREY", "STALE_NO_RESPONSE"}:
                    badge.get_style_context().add_class("bobby-quality-grey")
                top.pack_end(badge, False, False, 0)
                box.pack_start(top, False, False, 0)
                box.pack_start(
                    Gtk.Label(
                        label=f"{record.get('company_name') or 'Unknown company'} · {record.get('location') or 'Remote / location unknown'}",
                        xalign=0,
                    ), False, False, 0,
                )
                box.pack_start(
                    Gtk.Label(
                        label=f"{'Easy Apply limit — deferred' if record.get('application_status') == 'DEFERRED_EASY_APPLY_LIMIT' else record.get('application_status', 'DISCOVERED')} · {record.get('employer_response', NO_RESPONSE)}",
                        xalign=0,
                    ), False, False, 0,
                )
                account_state = (
                    "created" if record.get("account_created") else
                    "reused" if record.get("account_reused") else
                    "required" if record.get("account_required") else "not used"
                )
                verification_state = str(record.get("email_verification_state") or "NOT_REQUIRED").replace("_", " ").title()
                box.pack_start(
                    Gtk.Label(
                        label=(
                            f"{record.get('application_type') or 'Unknown type'} · "
                            f"ATS {record.get('ats_family') or '—'} · Account {account_state} · "
                            f"Email {verification_state}"
                        ),
                        xalign=0,
                    ), False, False, 0,
                )
                row.add(box)
                self.application_list.add(row)
                if record.get("job_key") == selected:
                    self.application_list.select_row(row)
            if rows and self.application_list.get_selected_row() is None:
                self.application_list.select_row(self.application_list.get_row_at_index(0))
            elif not rows:
                self._show_application_detail(None)
            self.application_list.show_all()
            self._refresh_gmail_status()

        def _build_settings_tab(
            self, title: str, specs: tuple[SettingSpec, ...], search_summary: bool = False
        ) -> None:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            page.set_border_width(2)
            heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            heading_title = Gtk.Label(label=title, xalign=0)
            heading_title.get_style_context().add_class("bobby-page-title")
            heading.pack_start(heading_title, False, False, 0)
            descriptions = {
                "Application Settings": "Choose how Bobby discovers and processes applications.",
                "Filters": "Tune provider-safe search limits and LinkedIn collection modes.",
                "AI & Providers": "Configure the real providers Bobby may use and inspect their health.",
                "External ATS": "Control bounded external application recovery and upload behavior.",
                "Automation": "Set startup, pacing, logging, and readonly verification preferences.",
            }
            subtitle = Gtk.Label(label=descriptions.get(title, "Saved Bobby configuration"), xalign=0)
            subtitle.get_style_context().add_class("bobby-page-subtitle")
            heading.pack_start(subtitle, False, False, 0)
            page.pack_start(heading, False, False, 0)
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            grid = Gtk.Grid(column_spacing=18, row_spacing=13, margin=8)
            scroller.add(grid)

            row = 0
            if search_summary:
                summary = Gtk.Label(
                    label="Saved launcher profiles (read-only here):\n"
                        + "\n".join(load_search_profile_summaries()),
                    xalign=0,
                )
                summary.set_line_wrap(True)
                grid.attach(summary, 0, row, 3, 1)
                row += 1
                filter_card = self._make_card()
                filter_card.pack_start(self._card_heading("Filter gate"), False, False, 0)
                self.filter_gate_label = Gtk.Label(xalign=0, yalign=0)
                self.filter_gate_label.get_style_context().add_class("bobby-card-caption")
                filter_card.pack_start(self.filter_gate_label, False, False, 0)
                grid.attach(filter_card, 0, row, 3, 1)
                row += 1

            for spec in specs:
                label = Gtk.Label(xalign=0)
                label.set_markup(f"<b>{spec.label}</b>")
                description = Gtk.Label(label=spec.description, xalign=0)
                description.set_line_wrap(True)
                description.set_max_width_chars(70)
                widget = self._make_setting_widget(spec)
                grid.attach(label, 0, row, 1, 1)
                grid.attach(widget, 1, row, 1, 1)
                grid.attach(description, 0, row + 1, 3, 1)
                self.setting_widgets[spec.key] = widget
                row += 2

            page.pack_start(scroller, True, True, 0)
            if title == "AI & Providers":
                health_frame = self._make_card()
                health_frame.pack_start(self._card_heading("Provider health"), False, False, 0)
                self.provider_health_label = Gtk.Label(xalign=0, yalign=0)
                self.provider_health_label.get_style_context().add_class("bobby-card-caption")
                health_frame.pack_start(self.provider_health_label, False, False, 7)
                page.pack_start(health_frame, False, False, 0)
            if title == "External ATS":
                safety = Gtk.Label(
                    label=(
                        "Safety invariants · submission verification ON · duplicate submit prevention ON · "
                        "resume upload guard ON · CAPTCHA/OTP/2FA bypass OFF"
                    ),
                    xalign=0,
                )
                safety.get_style_context().add_class("bobby-card-caption")
                page.pack_start(safety, False, False, 0)
            actions = Gtk.Box(spacing=10)
            save = Gtk.Button(label="Save Settings")
            save.connect("clicked", self.save_settings)
            reload_button = Gtk.Button(label="Reload Saved Values")
            reload_button.connect("clicked", lambda _button: self._load_settings())
            actions.pack_start(save, False, False, 0)
            actions.pack_start(reload_button, False, False, 0)
            page.pack_start(actions, False, False, 0)
            settings_notice = Gtk.Label(xalign=0)
            self.settings_notices.append(settings_notice)
            page.pack_start(settings_notice, False, False, 0)
            self._append_tab(page, title)

        def _make_setting_widget(self, spec: SettingSpec) -> Any:
            if spec.kind == "bool":
                widget = Gtk.Switch()
                widget.set_halign(Gtk.Align.START)
                widget.connect("notify::active", lambda *_args, key=spec.key: self._mark_dirty(key))
                return widget
            if spec.kind in {"int", "float"}:
                step = 0.05 if spec.kind == "float" else 1
                widget = Gtk.SpinButton.new_with_range(
                    spec.minimum if spec.minimum is not None else 0,
                    spec.maximum if spec.maximum is not None else 1000,
                    step,
                )
                if spec.kind == "float":
                    widget.set_digits(2)
                widget.connect("value-changed", lambda *_args, key=spec.key: self._mark_dirty(key))
                return widget
            if spec.kind in {"model", "optional_model", "path", "provider_order"}:
                widget = Gtk.Entry()
                widget.set_max_length(500 if spec.kind == "path" else 300)
                widget.connect("changed", lambda *_args, key=spec.key: self._mark_dirty(key))
                return widget
            widget = Gtk.ComboBoxText()
            for choice in spec.choices:
                widget.append_text(choice)
            widget.connect("changed", lambda *_args, key=spec.key: self._mark_dirty(key))
            return widget

        def _build_search_profiles_tab(self) -> None:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            page.set_border_width(2)
            title = Gtk.Label(label="Search Profiles", xalign=0)
            title.get_style_context().add_class("bobby-page-title")
            page.pack_start(title, False, False, 0)
            page.pack_start(
                Gtk.Label(label="Remote and Tampa profiles remain independent and editable.", xalign=0),
                False,
                False,
                0,
            )
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            profiles_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=6)
            scroller.add(profiles_box)

            for name, _path in SEARCH_PROFILES:
                frame = Gtk.Frame(label=name)
                frame.get_style_context().add_class("bobby-card")
                grid = Gtk.Grid(column_spacing=14, row_spacing=8, margin=12)
                frame.add(grid)
                widgets: dict[str, Any] = {}
                row = 0

                grid.attach(Gtk.Label(label="Locations (one per line)", xalign=0), 0, row, 1, 1)
                location_scroller = Gtk.ScrolledWindow()
                location_scroller.set_min_content_height(65)
                location = Gtk.TextView()
                location.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                location_scroller.add(location)
                grid.attach(location_scroller, 1, row, 3, 1)
                widgets["locations"] = location
                row += 1

                for key, label in (
                    ("remote", "Remote"),
                    ("hybrid", "Hybrid"),
                    ("onsite", "On-site"),
                    ("apply_once_at_company", "Apply once/company"),
                ):
                    switch = Gtk.Switch()
                    switch.set_halign(Gtk.Align.START)
                    grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
                    grid.attach(switch, 1, row, 1, 1)
                    widgets[key] = switch
                    row += 1

                grid.attach(Gtk.Label(label="Experience", xalign=0), 0, row, 1, 1)
                exp_box = Gtk.Box(spacing=10)
                for key, label in (
                    ("internship", "Internship"),
                    ("entry", "Entry"),
                    ("associate", "Associate"),
                    ("mid_senior_level", "Mid-Senior"),
                    ("director", "Director"),
                    ("executive", "Executive"),
                ):
                    check = Gtk.CheckButton(label=label)
                    exp_box.pack_start(check, False, False, 0)
                    widgets[f"experience_level.{key}"] = check
                grid.attach(exp_box, 1, row, 3, 1)
                row += 1

                grid.attach(Gtk.Label(label="Job types", xalign=0), 0, row, 1, 1)
                job_box = Gtk.Box(spacing=8)
                for key, label in (
                    ("full_time", "Full-time"),
                    ("contract", "Contract"),
                    ("part_time", "Part-time"),
                    ("temporary", "Temporary"),
                    ("volunteer", "Volunteer"),
                    ("internship", "Internship"),
                    ("other", "Other"),
                ):
                    check = Gtk.CheckButton(label=label)
                    job_box.pack_start(check, False, False, 0)
                    widgets[f"job_types.{key}"] = check
                grid.attach(job_box, 1, row, 3, 1)
                row += 1

                grid.attach(Gtk.Label(label="Date posted", xalign=0), 0, row, 1, 1)
                date = Gtk.ComboBoxText()
                for key, label in (
                    ("all_time", "Any time"),
                    ("month", "Past month"),
                    ("week", "Past week"),
                    ("24_hours", "Past 24 hours"),
                ):
                    date.append(key, label)
                grid.attach(date, 1, row, 1, 1)
                widgets["date"] = date
                row += 1

                grid.attach(Gtk.Label(label="Search terms (one per line)", xalign=0), 0, row, 1, 1)
                term_scroller = Gtk.ScrolledWindow()
                term_scroller.set_min_content_height(130)
                positions = Gtk.TextView(monospace=True)
                positions.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                term_scroller.add(positions)
                grid.attach(term_scroller, 1, row, 3, 1)
                widgets["positions"] = positions
                self.profile_widgets[name] = widgets
                profiles_box.pack_start(frame, False, False, 0)

            page.pack_start(scroller, True, True, 0)
            actions = Gtk.Box(spacing=10)
            save = Gtk.Button(label="Save Search Profiles")
            save.connect("clicked", self.save_search_profiles)
            reload_button = Gtk.Button(label="Reload Search Profiles")
            reload_button.connect("clicked", lambda _button: self._load_search_profiles())
            actions.pack_start(save, False, False, 0)
            actions.pack_start(reload_button, False, False, 0)
            page.pack_start(actions, False, False, 0)
            self.profile_notice = Gtk.Label(xalign=0)
            page.pack_start(self.profile_notice, False, False, 0)
            self._append_tab(page, "Search Profiles")

        def _load_search_profiles(self) -> None:
            try:
                for name, adapter in self.profile_adapters.items():
                    values = adapter.load()
                    widgets = self.profile_widgets[name]
                    widgets["locations"].get_buffer().set_text("\n".join(values["locations"]))
                    for key, widget in widgets.items():
                        if key in {"locations", "positions", "date"}:
                            continue
                        widget.set_active(bool(values[key]))
                    date_key = next(
                        key
                        for key in ("all_time", "month", "week", "24_hours")
                        if values[f"date.{key}"]
                    )
                    widgets["date"].set_active_id(date_key)
                    buffer = widgets["positions"].get_buffer()
                    buffer.set_text("\n".join(values["positions"]))
                self.profile_notice.set_text("Saved search profiles loaded")
            except (ConfigError, StopIteration) as exc:
                self.message(str(exc), error=True)

        @staticmethod
        def _text_view_lines(view: Any) -> list[str]:
            buffer = view.get_buffer()
            text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
            return parse_profile_lines(text)

        def save_search_profiles(self, _button: object) -> None:
            try:
                all_changes: dict[str, dict[str, Any]] = {}
                for name, adapter in self.profile_adapters.items():
                    widgets = self.profile_widgets[name]
                    changes: dict[str, Any] = {
                        "locations": self._text_view_lines(widgets["locations"]),
                        "positions": self._text_view_lines(widgets["positions"]),
                    }
                    for key, widget in widgets.items():
                        if key in {"locations", "positions", "date"}:
                            continue
                        changes[key] = bool(widget.get_active())
                    selected_date = widgets["date"].get_active_id()
                    for key in ("all_time", "month", "week", "24_hours"):
                        changes[f"date.{key}"] = key == selected_date
                    all_changes[name] = changes
                save_search_profile_changes(self.profile_adapters, all_changes)
                self.profile_notice.set_text(settings_saved_message(bobby_is_running(self.process)))
            except ConfigError as exc:
                self.message(str(exc), error=True)

        def _build_resume_tab(self) -> None:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            page.set_border_width(2)
            title = Gtk.Label(xalign=0)
            title.set_markup("<b>Resume</b>")
            title.get_style_context().add_class("bobby-page-title")
            page.pack_start(title, False, False, 0)
            page.pack_start(
                Gtk.Label(
                    label="One canonical file is shared by LinkedIn Easy Apply and external ATS upload. Bobby validates identity before a production worker starts.",
                    xalign=0,
                ),
                False,
                False,
                0,
            )
            row = Gtk.Box(spacing=8)
            self.resume_path_entry = Gtk.Entry()
            self.resume_path_entry.set_hexpand(True)
            self.resume_path_entry.set_editable(False)
            row.pack_start(self.resume_path_entry, True, True, 0)
            browse = Gtk.Button(label="Browse")
            browse.connect("clicked", self.browse_resume)
            row.pack_start(browse, False, False, 0)
            validate = Gtk.Button(label="Validate")
            validate.connect("clicked", self.validate_resume)
            row.pack_start(validate, False, False, 0)
            page.pack_start(row, False, False, 0)
            self.resume_status = Gtk.Label(xalign=0)
            page.pack_start(self.resume_status, False, False, 0)
            self._append_tab(page, "Resume")

        def _resume_config_path(self) -> str:
            try:
                return str(self.config_adapter.load().get("READY_MADE_RESUME_PATH") or "")
            except ConfigError:
                return ""

        def _set_resume_status(self, text: str, error: bool = False) -> None:
            self.resume_status.set_markup(
                f"<span foreground='{'#c0392b' if error else '#237a3b'}'>{text}</span>"
            )

        def browse_resume(self, _button: object) -> None:
            dialog = Gtk.FileChooserDialog(
                title="Choose canonical resume",
                transient_for=self,
                action=Gtk.FileChooserAction.OPEN,
            )
            dialog.add_buttons(
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_OPEN,
                Gtk.ResponseType.OK,
            )
            response = dialog.run()
            selected = dialog.get_filename() if response == Gtk.ResponseType.OK else None
            dialog.destroy()
            if not selected:
                return
            path = Path(selected).resolve()
            if path.suffix.casefold() not in {".docx", ".pdf"}:
                self._set_resume_status("Choose a .docx or .pdf resume file.", error=True)
                return
            try:
                self.config_adapter.save({"READY_MADE_RESUME_PATH": str(path)})
            except ConfigError as exc:
                self._set_resume_status(str(exc), error=True)
                return
            self.resume_path_entry.set_text(str(path))
            self._set_resume_status("Canonical resume path saved; restart Bobby before production.")

        def validate_resume(self, _button: object) -> None:
            path_text = self.resume_path_entry.get_text().strip() or self._resume_config_path()
            path = Path(path_text).expanduser()
            if not path.is_file() or not os.access(path, os.R_OK):
                self._set_resume_status("Resume file is missing or unreadable.", error=True)
                return
            try:
                from config.constants import RESUME_DIR
                from src.utils.candidate_integrity import validate_candidate_identity
                from src.utils.utils import load_yaml_file

                resume_text = (Path(RESUME_DIR) / "resume_text.txt").read_text(encoding="utf-8")
                structured = load_yaml_file(Path(RESUME_DIR) / "structured_resume.yaml")
                validate_candidate_identity(
                    resume_text=resume_text,
                    resume_structured=structured,
                    application_profile_path=Path(RESUME_DIR) / "application_profile.yaml",
                    resume_pdf_path=path,
                )
            except Exception as exc:
                self._set_resume_status(f"Identity validation failed: {type(exc).__name__}", error=True)
                return
            self._set_resume_status(f"Readable and identity-validated: {path}")

        def _build_notifications_tab(self) -> None:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            page.set_border_width(2)
            title = Gtk.Label(label="Notifications", xalign=0)
            title.get_style_context().add_class("bobby-page-title")
            page.pack_start(title, False, False, 0)
            page.pack_start(
                Gtk.Label(label="Connection state is shown without exposing credentials.", xalign=0),
                False,
                False,
                0,
            )
            status = []
            env_path = REPOSITORY / ".env"
            try:
                env_keys = {
                    line.split("=", 1)[0].strip()
                    for line in env_path.read_text(encoding="utf-8").splitlines()
                    if "=" in line and line.split("=", 1)[0].strip()
                }
            except OSError:
                env_keys = set()
            try:
                gmail_enabled = bool(self.config_adapter.load().get("GMAIL_APPLICATION_INTEGRATION"))
            except ConfigError:
                gmail_enabled = False
            status.append(f"Gmail readonly verification: {'enabled' if gmail_enabled else 'disabled'}")
            status.append(f"Telegram reporting: {'configured' if 'tg_token' in env_keys else 'not configured'}")
            status.append("Credential values are intentionally hidden.")
            card = self._make_card()
            card.pack_start(self._card_heading("Integrations"), False, False, 0)
            for line in status:
                card.pack_start(Gtk.Label(label=line, xalign=0), False, False, 0)
            page.pack_start(card, False, False, 0)
            self._append_tab(page, "Notifications")

        def _build_safety_tab(self) -> None:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            page.set_border_width(2)
            title = Gtk.Label(label="Safety", xalign=0)
            title.get_style_context().add_class("bobby-page-title")
            page.pack_start(title, False, False, 0)
            page.pack_start(
                Gtk.Label(
                    label="These safeguards are read-only invariants of the application layer.", xalign=0
                ),
                False,
                False,
                0,
            )
            card = self._make_card()
            card.pack_start(self._card_heading("Protection status"), False, False, 0)
            for label in (
                "Submission verification — Enabled",
                "Duplicate submission protection — Enabled",
                "CAPTCHA bypass — Disabled",
                "2FA bypass — Disabled",
                "Candidate fact fabrication — Disabled",
                "Encountered-history protection — Enabled",
                "Graceful shutdown — Enabled",
                "PII log protection — Enabled",
            ):
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic", Gtk.IconSize.MENU)
                row.pack_start(icon, False, False, 0)
                row.pack_start(Gtk.Label(label=label, xalign=0), True, True, 0)
                card.pack_start(row, False, False, 3)
            page.pack_start(card, False, False, 0)
            self._append_tab(page, "Safety")

        def _build_advanced_tab(self) -> None:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            page.set_border_width(2)
            title = Gtk.Label(label="Advanced", xalign=0)
            title.get_style_context().add_class("bobby-page-title")
            page.pack_start(title, False, False, 0)
            page.pack_start(Gtk.Label(label="Operator context and immutable policy boundaries.", xalign=0), False, False, 0)
            policy = self._make_card()
            policy.pack_start(self._card_heading("Policy"), False, False, 0)
            for line in (
                "CAPTCHA and 2FA bypass: disabled",
                "Submission verification: required before success classification",
                "Candidate facts: resume/profile source rules enforced",
                "Encountered history: preserved; no reset control is exposed",
                "Single-instance lock: enabled",
            ):
                policy.pack_start(Gtk.Label(label="• " + line, xalign=0), False, False, 0)
            page.pack_start(policy, False, False, 0)
            self._append_tab(page, "Advanced")

        def _mark_dirty(self, key: str) -> None:
            if not self.loading_settings:
                self.dirty_settings.add(key)
                for notice in self.settings_notices:
                    notice.set_text("Unsaved configuration changes")

        def _load_settings(self) -> None:
            try:
                values = self.config_adapter.load()
            except ConfigError as exc:
                self.message(str(exc), error=True)
                return
            self.loading_settings = True
            try:
                for key, widget in self.setting_widgets.items():
                    if key not in values:
                        widget.set_sensitive(False)
                        continue
                    spec = SETTING_BY_KEY[key]
                    value = values[key]
                    if spec.kind == "bool":
                        widget.set_active(value)
                    elif spec.kind in {"int", "float"}:
                        widget.set_value(value)
                    elif spec.kind in {"model", "optional_model", "path"}:
                        widget.set_text(value)
                    elif spec.kind == "provider_order":
                        widget.set_text(", ".join(value))
                    else:
                        widget.set_active(spec.choices.index(value))
            finally:
                self.loading_settings = False
            if hasattr(self, "resume_path_entry"):
                self.resume_path_entry.set_text(str(values.get("READY_MADE_RESUME_PATH") or ""))
                self._set_resume_status("Configured path loaded; press Validate to check readability and identity.")
            self.dirty_settings.clear()
            for notice in self.settings_notices:
                notice.set_text("Saved configuration loaded")

        def _widget_value(self, key: str) -> Any:
            widget = self.setting_widgets[key]
            kind = SETTING_BY_KEY[key].kind
            if kind == "bool":
                return bool(widget.get_active())
            if kind == "int":
                return int(widget.get_value_as_int())
            if kind == "float":
                return float(widget.get_value())
            if kind in {"model", "optional_model", "path"}:
                return widget.get_text().strip()
            if kind == "provider_order":
                return tuple(
                    provider.strip().lower()
                    for provider in widget.get_text().split(",")
                    if provider.strip()
                )
            return widget.get_active_text()

        def save_settings(self, _button: object) -> None:
            changes = {key: self._widget_value(key) for key in self.dirty_settings}
            try:
                self.config_adapter.save(changes)
            except ConfigError as exc:
                self.message(str(exc), error=True)
                return
            self.dirty_settings.clear()
            message = settings_saved_message(bobby_is_running(self.process))
            for notice in self.settings_notices:
                notice.set_text(message)
            self.refresh()

        def _build_runtime_tab(self) -> None:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            page.set_border_width(2)
            title = Gtk.Label(label="Runtime", xalign=0)
            title.get_style_context().add_class("bobby-page-title")
            page.pack_start(title, False, False, 0)
            self.runtime_details = Gtk.Label(xalign=0, yalign=0, selectable=True)
            page.pack_start(self.runtime_details, False, False, 0)
            frame = Gtk.Frame(label="Recent safe terminal events")
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            self.events_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7, margin=10)
            scroller.add(self.events_box)
            frame.add(scroller)
            page.pack_start(frame, True, True, 0)
            self._append_tab(page, "Runtime")

        def _build_logs_tab(self) -> None:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            page.set_border_width(2)
            title = Gtk.Label(label="Logs", xalign=0)
            title.get_style_context().add_class("bobby-page-title")
            page.pack_start(title, False, False, 0)
            toolbar = Gtk.Box(spacing=8)
            self.log_path_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
            toolbar.pack_start(self.log_path_label, True, True, 0)
            self.log_filter_entry = Gtk.Entry(placeholder_text="Search log")
            self.log_filter_entry.set_width_chars(18)
            self.log_filter_entry.connect("changed", lambda *_args: self._reload_log_view())
            toolbar.pack_start(self.log_filter_entry, False, False, 0)
            self.log_level_filter = Gtk.ComboBoxText()
            for level in ("ALL", "DEBUG", "INFO", "WARNING", "ERROR"):
                self.log_level_filter.append_text(level)
            self.log_level_filter.set_active(0)
            self.log_level_filter.connect("changed", lambda *_args: self._reload_log_view())
            toolbar.pack_start(self.log_level_filter, False, False, 0)
            self.pause_log_button = Gtk.ToggleButton(label="Pause Follow")
            self.pause_log_button.connect("toggled", self.toggle_log_pause)
            toolbar.pack_start(self.pause_log_button, False, False, 0)
            self.auto_scroll_switch = Gtk.Switch(active=True)
            toolbar.pack_start(Gtk.Label(label="Auto-scroll"), False, False, 0)
            toolbar.pack_start(self.auto_scroll_switch, False, False, 0)
            clear = Gtk.Button(label="Clear View")
            clear.connect("clicked", self.clear_log_view)
            toolbar.pack_start(clear, False, False, 0)
            open_button = Gtk.Button(label="Open Externally")
            open_button.connect("clicked", self.open_latest_log)
            toolbar.pack_start(open_button, False, False, 0)
            page.pack_start(toolbar, False, False, 0)

            scroller = Gtk.ScrolledWindow()
            self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
            self.log_view.set_wrap_mode(Gtk.WrapMode.NONE)
            self.log_buffer = self.log_view.get_buffer()
            scroller.add(self.log_view)
            page.pack_start(scroller, True, True, 0)
            self._append_tab(page, "Logs")

        def _filter_log_text(self, text: str) -> str:
            query = self.log_filter_entry.get_text().strip().casefold()
            level = self.log_level_filter.get_active_text() or "ALL"
            lines = []
            for line in text.splitlines(keepends=True):
                if query and query not in line.casefold():
                    continue
                if level != "ALL" and level not in line.upper():
                    continue
                lines.append(line)
            return "".join(lines)

        def _reload_log_view(self) -> None:
            self.log_path = None
            self.log_offset = 0
            self.refresh_log()

        def message(self, text: str, error: bool = False) -> None:
            dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text=text,
            )
            dialog.run()
            dialog.destroy()

        def start_bobby(self, smoke: bool, timeout_seconds: int | None = None) -> None:
            try:
                self.process = launch_bobby(smoke, self.process, timeout_seconds)
            except BobbyAlreadyRunning:
                self.message("Bobby is already running.")
                return
            except OSError as exc:
                self.message(f"Could not start Bobby: {exc}", error=True)
                return
            self.draining = False
            label = "Bobby launch requested with saved startup settings."
            if timeout_seconds:
                label = f"Supervised {timeout_seconds // 60}-minute validation requested."
            self.dashboard_notice.set_text(label)
            self.refresh()

        def stop_bobby(self, _button: object) -> None:
            try:
                stopped = request_graceful_stop(self.process)
            except OSError as exc:
                self.message(f"Could not request graceful stop: {exc}", error=True)
                return
            if not stopped:
                self.message("Bobby is not running.")
                return
            self.draining = True
            self.dashboard_notice.set_text("Graceful shutdown requested; active work is draining.")
            self.refresh()

        def open_latest_log(self, _button: object) -> None:
            try:
                log_path = LATEST_LOG.resolve(strict=True)
                subprocess.Popen(
                    ["/usr/bin/xdg-open", str(log_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                self.message(f"Latest log is unavailable: {exc}", error=True)

        def toggle_log_pause(self, button: Any) -> None:
            self.log_paused = bool(button.get_active())
            button.set_label("Resume Follow" if self.log_paused else "Pause Follow")

        def clear_log_view(self, _button: object) -> None:
            self.log_buffer.set_text("")
            try:
                self.log_path = LATEST_LOG.resolve(strict=True)
                self.log_offset = self.log_path.stat().st_size
            except OSError:
                self.log_path = None
                self.log_offset = 0

        def _append_log_text(self, text: str) -> None:
            if not text:
                return
            end = self.log_buffer.get_end_iter()
            self.log_buffer.insert(end, text)
            if self.log_buffer.get_char_count() > 180000:
                start = self.log_buffer.get_start_iter()
                trim = self.log_buffer.get_iter_at_offset(30000)
                self.log_buffer.delete(start, trim)
            if self.auto_scroll_switch.get_active():
                mark = self.log_buffer.create_mark(None, self.log_buffer.get_end_iter(), False)
                self.log_view.scroll_mark_onscreen(mark)
                self.log_buffer.delete_mark(mark)

        def refresh_log(self) -> None:
            if self.log_paused:
                return
            try:
                path = LATEST_LOG.resolve(strict=True)
                size = path.stat().st_size
                reset = path != self.log_path or size < self.log_offset
                if (reset or size > self.log_offset) and not self.log_read_pending:
                    start = max(0, size - 100000) if reset else self.log_offset
                    self.log_path = path
                    self.log_read_pending = True
                    self.log_executor.submit(self._read_log_chunk, path, start, size, reset).add_done_callback(
                        lambda future: GLib.idle_add(self._apply_log_chunk, future)
                    )
                self.log_path_label.set_text(f"Following: {path}")
            except OSError:
                self.log_path_label.set_text("Latest remote log is not available")

        @staticmethod
        def _read_log_chunk(path: Path, start: int, size: int, reset: bool) -> tuple[int, bool, str]:
            try:
                with path.open("rb") as file:
                    file.seek(start)
                    data = file.read(max(0, size - start))
                text = data.decode("utf-8", errors="replace")
                if reset and start > 0 and "\n" in text:
                    text = text.split("\n", 1)[1]
                return size, reset, text
            except OSError:
                return start, reset, ""

        def _apply_log_chunk(self, future: Any) -> bool:
            self.log_read_pending = False
            try:
                size, reset, text = future.result()
            except Exception:
                return False
            if reset:
                self.log_buffer.set_text(self._filter_log_text(text))
            else:
                self._append_log_text(self._filter_log_text(text))
            self.log_offset = size
            return False

        def _refresh_recent_events(
            self, snapshot: dict[str, Any], events: list[dict[str, Any]]
        ) -> None:
            rows = recent_terminal_events(snapshot, events)
            signature = tuple(
                (row["status"], row["job"], row["company"], row["category"])
                for row in rows
            )
            if signature == self.last_event_signature:
                return
            self.last_event_signature = signature
            for child in self.events_box.get_children():
                self.events_box.remove(child)
            if not rows:
                self.events_box.pack_start(
                    Gtk.Label(label="No terminal events in the current run.", xalign=0),
                    False,
                    False,
                    0,
                )
            for row in rows:
                label = Gtk.Label(xalign=0)
                label.set_line_wrap(True)
                label.set_text(
                    f"{row['status']} — {row['job']} @ {row['company']}\n"
                    f"Category: {row['category']}"
                )
                self.events_box.pack_start(label, False, False, 0)
            self.events_box.show_all()

        def _refresh_provider_health(self) -> None:
            if not hasattr(self, "provider_health_label"):
                return
            try:
                from src.llm.provider_health import provider_health
                from src.llm.provider_config import credential_for, provider_statuses

                values = self.config_adapter.load()
                order = values.get("LLM_PROVIDER_ORDER") or (values.get("LLM_MODEL_TYPE") or "gemini",)
                rows = []
                status_rows = provider_statuses(
                    order,
                    primary_api_key=credential_for("gemini"),
                )
                for priority, (provider, model, configured_status) in enumerate(status_rows, start=1):
                    state = provider_health(
                        provider, configured=configured_status == "Configured"
                    )
                    cooldown = (
                        f" until {datetime.fromtimestamp(state.expires_at).isoformat(timespec='seconds')}"
                        if state.expires_at and state.state == "COOLDOWN"
                        else ""
                    )
                    rows.append(
                        f"{priority}. {provider}: {configured_status} | model={model} | "
                        f"health={state.state}{cooldown} | retry budget="
                        f"{values.get('LLM_PROVIDER_MAX_RETRIES', '—')}"
                    )
                self.provider_health_label.set_text("\n".join(rows) or "No providers configured")
            except Exception:
                self.provider_health_label.set_text("Provider health unavailable")

        @staticmethod
        def _easy_apply_quota_label() -> str:
            state = easy_apply_quota_state()
            snapshot = state.snapshot()
            status = snapshot.get("status") or EASY_APPLY_QUOTA_UNKNOWN
            mode = read_easy_apply_only_mode()
            if status == EASY_APPLY_QUOTA_BLOCKED:
                if state.should_recheck():
                    return "Easy Apply: Rechecking availability"
                if mode == "False":
                    return "Easy Apply: Limit reached — external ATS active"
                if mode == "True":
                    return "Easy Apply: Limit reached — Easy Apply-only mode remains active"
                return "Easy Apply: Limit reached — mode unknown"
            if status == EASY_APPLY_QUOTA_AVAILABLE:
                return "Easy Apply: Available"
            return "Easy Apply: Availability unknown — will probe safely"

        def _refresh_filter_gate(self, snapshot: dict[str, Any]) -> None:
            if not hasattr(self, "filter_gate_label"):
                return
            verification = snapshot.get("filter_verification")
            if isinstance(verification, dict):
                profile = verification.get("profile") or snapshot.get("profile") or "—"
                actual_url = verification.get("url") or "not recorded"
                state = verification.get("state") or verification.get("result") or "UNKNOWN"
                self.filter_gate_label.set_text(
                    f"Profile reached: {profile}\n"
                    f"LinkedIn filter UI: {state}\n"
                    f"Verified URL: {actual_url}"
                )
                return
            self.filter_gate_label.set_text(
                "Configured filters are shown above. Last UI verification: not recorded."
            )

        def refresh(self) -> bool:
            running = bobby_is_running(self.process)
            snapshot = load_runtime_snapshot()
            events = load_runtime_events()
            draining = running and (
                self.draining
                or bool(snapshot.get("stop_requested"))
                or str(snapshot.get("run_status") or "").casefold() in {"draining", "stopping"}
            )
            if not running:
                self.process = None
                self.draining = False
            status = "DRAINING" if draining else "RUNNING" if running else "STOPPED"
            self.status_label.set_markup(f"<b>Status:</b> {status}")
            self.header_status.set_text(status)
            status_context = self.header_status.get_style_context()
            for class_name in ("bobby-status-running", "bobby-status-stopped", "bobby-status-draining"):
                status_context.remove_class(class_name)
            status_context.add_class(
                {
                    "RUNNING": "bobby-status-running",
                    "DRAINING": "bobby-status-draining",
                    "STOPPED": "bobby-status-stopped",
                }[status]
            )
            mode = read_easy_apply_only_mode()
            self.mode_label.set_markup(
                f"<b>Easy Apply Only:</b> {'ON' if mode == 'True' else 'OFF' if mode == 'False' else 'UNKNOWN'}"
            )
            self.quota_status_label.set_text(self._easy_apply_quota_label())
            run_id = snapshot.get("run_id") or "—"
            self.run_id_label.set_markup(f"<b>Run ID:</b> {run_id}")
            self.runtime_label.set_markup(
                f"<b>Runtime:</b> {format_runtime_duration(snapshot.get('started_at'), running, snapshot.get('finished_at'))}"
            )
            try:
                log_path = str(LATEST_LOG.resolve(strict=True))
            except OSError:
                log_path = "Unavailable"
            self.latest_log_label.set_markup(f"<b>Latest log:</b> {log_path}")
            for button in self.start_buttons:
                button.set_sensitive(not running)
            self.stop_button.set_sensitive(running)

            counters = current_run_counters(snapshot, events)
            for key, label in self.counter_labels.items():
                label.set_markup(f"<b>{counters[key]}</b>")
            current_job = snapshot.get("current_job") or {}
            current_job_text = (
                f"{current_job.get('title') or '—'} @ {current_job.get('company') or '—'}"
            )
            if current_job:
                self.activity_label.set_text(
                    f"{current_job.get('title') or 'Untitled job'}\n"
                    f"Company: {current_job.get('company') or '—'}\n"
                    f"Stage: {current_job.get('stage') or snapshot.get('stage') or '—'}\n"
                    f"Application: {current_job.get('application_type') or '—'}\n"
                    f"Provider: {current_job.get('provider') or '—'}\n"
                    f"Profile: {snapshot.get('profile') or snapshot.get('profile_name') or '—'}"
                )
            else:
                self.activity_label.set_text("No active job")
            self.runtime_details.set_text(
                f"Status: {status}\n"
                f"Run ID: {run_id}\n"
                f"Started: {snapshot.get('started_at') or '—'}\n"
                f"Runtime: {format_runtime_duration(snapshot.get('started_at'), running, snapshot.get('finished_at'))}\n"
                f"Current job: {current_job_text}\n"
                f"Latest log: {log_path}"
            )
            self._refresh_recent_events(snapshot, events)
            self._refresh_filter_gate(snapshot)
            self._refresh_provider_health()
            self._refresh_applications()
            self.refresh_log()
            return True

    window = BobbyWindow()
    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
