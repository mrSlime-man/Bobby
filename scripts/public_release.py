#!/usr/bin/env python3
"""Build and check Bobby's sanitized public release tree."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

SOURCE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = SOURCE_ROOT.parent / "Bobby-public"
MARKER = ".bobby-public-export.json"
VERSION = "0.1.0-alpha.1"

PRIVATE_PARTS = {
    ".git", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__",
    "htmlcov", ".claude", "audit", "backups", "browser_session", "logs",
}
PRIVATE_FILES = {
    ".env", ".coverage", "candidate_profile.yaml", "requirements.txt", "Dockerfile",
    "README.md", "architecture.excalidraw", "external_stats.py", "gmail_auth.py",
    "job_bot_v3_patch.py", "docs/current_task.md", "docs/readiness_audit.md",
    "examples/.env_example", "examples/data/resumes/Alex_Nettleton_CV.pdf",
    "src/telegram/error_cache.yaml",
}

SAFE_ENV = """# Copy to .env and replace placeholders locally. Never commit .env.
linkedin_email=your-linkedin-email@example.invalid
linkedin_password=replace-me
indeed_email=your-indeed-email@example.invalid
indeed_password=replace-me
llm_api_key=replace-me
openai_api_key=
anthropic_api_key=
GEMINI_API_KEY=
llm_proxy=
llm_api_url=
tg_token=
TELEGRAM_CHAT_ID=
TELEGRAM_TOPIC_ID=
"""

SAFE_SEARCH = """positions:
  - Software Engineer
  - Automation Engineer
  - QA Engineer
remote: true
hybrid: true
onsite: false
experience_level:
  internship: false
  entry: true
  associate: true
  mid_senior_level: true
  director: false
  executive: false
job_types:
  full_time: true
  contract: false
  part_time: false
  temporary: false
  volunteer: false
  internship: false
  other: false
date:
  all_time: false
  month: false
  week: false
  24_hours: true
locations:
  - United States
apply_once_at_company: true
company_blacklist: []
title_blacklist: []
location_blacklist: []
"""

SAFE_PROFILE = """# Synthetic example only. Copy to candidate_profile.yaml and replace every fact.
candidate:
  legal_name: Taylor Example
  preferred_name: Taylor
  email: taylor@example.invalid
  phone: "+1 555 0100"
  age_over_18: true
address:
  street: 100 Example Street
  city: Testville
  state: EX
  postal_code: "00000"
  country: United States
communication:
  can_receive_email: true
  can_receive_phone_calls: true
  can_receive_text_messages: false
  preferred_contact_method: email
work_authorization:
  authorized_to_work_in_us: true
  requires_sponsorship_now: false
  requires_sponsorship_future: false
location_preferences:
  current_location: Testville, EX
  relocation_locations: []
  willing_to_relocate: false
  willing_to_work_for_out_of_state_employer_remotely: true
work_arrangement:
  remote: true
  hybrid: true
  onsite: false
employment_type:
  full_time: true
  contract: false
  part_time: false
availability:
  available_to_start_immediately: false
  currently_employed: true
  notice_period_days: 14
languages: [English]
technical_profile:
  general: [Python, Git, REST APIs]
professional_experience:
  has_customer_service_experience: false
  known_experience_areas: [software delivery, testing, automation]
common_answers:
  are_you_18_or_older: true
  legally_authorized_to_work_in_us: true
  require_sponsorship_now: false
  require_sponsorship_future: false
  willing_to_work_full_time: true
  willing_to_work_remote: true
  willing_to_work_hybrid: true
  willing_to_work_onsite: false
  willing_to_use_electronic_signature: true
"""

SAFE_RESUME = """personal_information:
  first_name: Taylor
  last_name: Example
  gender: male
  country: United States
  city: Testville
  state_area_region: EX
  address: 100 Example Street
  zip_code: "00000"
  phone: "5550100"
  phone_code: "+1"
  email: taylor@example.invalid
  github: https://www.github.com/taylor-example
  linkedin: https://www.linkedin.com/in/taylor-example
education_details:
  - education_level: Bachelor of Science
    institution: Example State University
    field_of_study: Computer Science
    year_of_completion: 2020
experience_details:
  - position: Automation Engineer
    company: Example Systems
    employment_period: 2022-present
    location: Remote
    key_responsibilities: [Built reliable automation and improved test coverage.]
    skills_acquired: [Python, CI, REST APIs]
projects:
  - name: Example Workflow Toolkit
    description: A small public demonstration of reliable automation patterns.
    link: https://github.com/taylor-example/example-workflow-toolkit
achievements: [Improved release confidence for a representative service.]
certifications: []
languages: [English]
interests: [automation, developer tools, reliable systems]
availability:
  notice_period: 14 days
salary_expectations:
  salary_range_usd: 70000-90000
self_identification:
  disability: decline_to_answer
  ethnicity: decline_to_answer
  pronouns: they/them
  veteran: decline_to_answer
legal_authorization:
  legally_allowed_to_work_in_us: true
  requires_us_sponsorship: false
work_preferences:
  remote_work: true
  in_person_work: false
  open_to_relocation: false
"""

SAFE_RESUME_TEXT = """Taylor Example
Software and automation engineer | taylor@example.invalid

Builds testable automation, developer tooling, and reliable workflows.

Automation Engineer — Example Systems (2022-present)
- Built maintainable test and workflow automation.
- Improved observability and release confidence.

B.S. Computer Science — Example State University (2020)
Skills: Python, Git, CI, REST APIs, Linux, testing, documentation
"""

README = """# Bobby — Open-Source Alpha

Bobby is an experimental, local-first, human-supervised job-application
assistant. It can search supported job sites, score opportunities, prepare
application material, and assist with Easy Apply workflows while keeping
submission decisions visible and auditable.

This is an active-development alpha. It is not a hosted service, a guarantee
of successful applications, or a replacement for reviewing every answer and
submission. Use a test account and synthetic data until you complete the
checks below.

## What works today

- LinkedIn and Indeed workflow foundations with configurable search criteria.
- LLM-assisted suitability and application-question handling.
- Resume and cover-letter generation paths.
- Durable application tracking, run IDs, redaction, and a local dashboard.
- A GTK control panel plus bounded provider fallback and submit-state checks.
- Experimental external ATS support behind explicit opt-in configuration.

The alpha has broad ATS detection and workflow code, but independent live
confirmation is limited. Treat Workday, Greenhouse, iCIMS, and ADP as
experimental observations, not compatibility guarantees. Other ATS paths are
subject to per-site verification.

## Proven in production

The private validation baseline behind this public alpha includes:

- A confirmed real external ATS submission in a controlled, human-supervised run.
- 1,300+ offline regression tests covering safety and workflow behavior.
- Duplicate-submit protection and durable submit-state checks.
- Provider fallback with bounded health and quota handling.
- An application tracker with run IDs, evidence fields, and redaction.

These results describe the current private validation baseline, not a promise
of universal ATS compatibility or unattended operation.

## Screenshots

The GTK control panel keeps the main workflow visible: run state, metrics,
search profiles, and safety controls are available from one local interface.

![Bobby dashboard](assets/screenshots/dashboard.png)

![Bobby search profiles](assets/screenshots/search-profiles.png)

## Safety boundaries

The public example configuration enables `TEST_MODE` and
`EASY_APPLY_ONLY_MODE`; external ATS automation, automatic account creation,
Gmail verification, and resume uploads are disabled. Review generated
answers, documents, and the final submit state yourself.

Never commit `.env`, `candidate_profile.yaml`, browser state, resumes, logs,
application history, screenshots, or Gmail credentials. See
[SECURITY.md](SECURITY.md) before using real candidate data.

## Quick start (Linux)

The canonical development environment is Python 3.12.7 with `uv`.

```bash
git clone https://github.com/mrSlime-man/Bobby.git
cd Bobby
uv sync --locked
cp .env.example .env
cp candidate_profile.example.yaml candidate_profile.yaml
cp examples/data/resumes/structured_resume.yaml data/resumes/structured_resume.yaml
uv run python scripts/doctor.py
uv run python main.py
```

Replace every synthetic profile value and configure one supported LLM
provider before attempting a run. Keep the public example configuration in
test mode while learning the workflow. The dashboard is `uv run python
dashboard.py`; the GTK panel is `uv run python bobby_gui_launcher.py` on
systems with GTK 3 and PyGObject.

## Configuration and testing

Search criteria live in `config/search_config.yaml`. Candidate facts belong in
`candidate_profile.yaml`, while professional history belongs in the resume
source. Run `uv run python scripts/doctor.py` before a run.

```bash
uv run python -m compileall -q main.py src tests scripts
uv run pytest -q
```

Tests are offline and must not use real credentials, browsers, job sites, or
application submissions. See [ROADMAP.md](ROADMAP.md) for the next milestones.

## Contributing, credits, and license

See [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md), and
[CREDITS.md](CREDITS.md). The current release is `v0.1.0-alpha.1` and the
license is in [LICENSE](LICENSE).
"""

SECURITY = """# Security and privacy

Bobby processes candidate identity, resumes, job history, browser sessions,
and sometimes email receipts. The alpha is designed for local execution and
human supervision.

- Keep `.env`, `candidate_profile.yaml`, `browser_session/`, `data/output/`,
  `data/debug/`, `logs/`, and resumes out of Git.
- Use a dedicated test account and a low application limit first.
- Keep test mode and Easy Apply-only mode enabled until you understand the
  workflow; external ATS automation is opt-in.
- Never paste secrets or unredacted candidate data into issues, pull requests,
  chat, or logs.
- Report suspected credential or privacy exposure privately to the repository
  owner before public disclosure.

Run `uv run python scripts/public_release.py check` before every public export.
"""

CONTRIBUTING = """# Contributing

Create a focused branch, describe the user-visible behavior, and run
`uv sync --locked`, compile checks, and `uv run pytest -q`. Add tests for
safety-critical behavior. CI must stay offline: do not access real job sites,
accounts, or submit applications.

For release work, run `uv run python scripts/public_release.py build` and
`uv run python scripts/public_release.py check` from the private checkout.
Never copy private runtime files into the public tree manually.
"""

CHANGELOG = """# Changelog

## v0.1.0-alpha.1 — 2026-09-09

- First sanitized public alpha release.
- Added repeatable privacy-aware export and installation doctor tooling.
- Documented human-supervised boundaries and experimental ATS compatibility.
- Added offline CI and release-readiness checks.
"""

CREDITS = """# Credits

Bobby is an independent continuation of work in the open-source LinkedIn AI
Job Applier ecosystem, including lineage associated with
`beatwad/LinkedIn-AI-Job-Applier-Ultimate` and earlier AIHawk-style workflows.

It also relies on Browser Use, Playwright, Pydantic, LangChain integrations,
FastAPI, PyYAML, python-telegram-bot, and the other libraries in
`pyproject.toml`. Their licenses and notices remain authoritative.
"""

ROADMAP = """# Roadmap

## 0.1 alpha — current

- Keep local execution, human review, and conservative submit-state tracking.
- Maintain LinkedIn and Indeed foundations with offline regression coverage.
- Keep credentials, PII, and browser state outside the public repository.
- Improve provider health, quota handling, and interrupted-run diagnostics.

## 0.2 beta target

- Add a stable CLI doctor and profile/config migration guide.
- Expand deterministic adapter contracts and synthetic ATS fixtures.
- Improve resume rendering checks and dashboard export ergonomics.
- Publish a supported provider matrix with reproducible evidence.

## Later

- Make browser/session lifecycle more portable across Linux environments.
- Add opt-in integrations only with explicit verification and rollback behavior.
- Provide stronger local retention controls and richer audit exports.

No milestone promises unattended submission or universal ATS coverage.
"""

AGENTS = """# Agent and maintainer notes

Bobby is safety-sensitive local automation. Preserve these invariants:

- Never commit credentials, candidate PII, resumes, browser state, logs, or
  application history.
- Keep submit-state classification conservative; an attempted submit without
  evidence is not a successful submission.
- Keep browser automation behind explicit configuration and human review.
- Keep tests offline and deterministic; never access real job sites in CI.
- Run locked dependency, compile, and pytest checks before a PR.
- Use `scripts/public_release.py` for public exports.
"""

LINKEDIN_DESCRIPTION = """# LinkedIn project description

Bobby is an open-source, local-first job-application assistant for
human-supervised workflows. It combines configurable search, LLM-assisted
screening and question handling, resume preparation, application tracking,
and conservative verification states in one auditable toolkit.

This is an experimental alpha: it does not promise unattended applications or
universal ATS compatibility. Keep credentials and candidate data local,
review every generated answer, and treat external ATS support as opt-in.

Current release: v0.1.0-alpha.1
Repository: https://github.com/mrSlime-man/Bobby
"""

CI = """name: CI
on:
  push:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12.7'
      - name: Install uv
        uses: astral-sh/setup-uv@v6
        with:
          version: '0.8.x'
      - run: uv sync --locked
      - run: uv run python -m compileall -q main.py src tests scripts
      - run: uv run pytest -q
      - run: uv run python scripts/public_release.py check
"""


def destination_from_args(value: str | None) -> Path:
    return Path(value or os.getenv("BOBBY_PUBLIC_DIR", str(DEFAULT_DESTINATION))).expanduser().resolve()


def relative_path(path: Path) -> str:
    return path.relative_to(SOURCE_ROOT).as_posix()


def should_skip(relative: str) -> bool:
    path = Path(relative)
    if any(part in PRIVATE_PARTS for part in path.parts):
        return True
    if relative in PRIVATE_FILES or path.name in {".env", "candidate_profile.yaml"}:
        return True
    if path.suffix in {".pyc", ".zip"}:
        return True
    if any(
        ".before_" in part or part.endswith((".backup", ".bak"))
        for part in path.parts
    ):
        return True
    if path.parts[:2] == ("data", "resumes"):
        return True
    if path.parts[:2] in {("data", "output"), ("data", "debug")}:
        return True
    if path.parts[:3] == ("examples", "data", "resumes"):
        return True
    if relative.startswith("config/search_") and path.suffix in {".yaml", ".yml"}:
        return True
    if path.parts[:1] == ("examples",) and path.name in {
        "app_config.py", "search_config.yaml", "search_config_remote.yaml", "search_config_tampa.yaml",
    }:
        return True
    return False


def copy_source_tree(destination: Path) -> None:
    for current, directories, files in os.walk(SOURCE_ROOT, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name for name in directories
            if not should_skip(relative_path(current_path / name))
            and not (current_path / name).is_symlink()
        ]
        for name in files:
            source = current_path / name
            relative = relative_path(source)
            if should_skip(relative) or source.is_symlink():
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def replace_assignment(text: str, name: str, value: str) -> str:
    pattern = re.compile(rf"(?m)^{re.escape(name)}\s*=.*$")
    replacement = f"{name} = {value}"
    return pattern.sub(replacement, text, count=1) if pattern.search(text) else text + f"\n{replacement}\n"


def public_app_config() -> str:
    text = (SOURCE_ROOT / "config" / "app_config.py").read_text(encoding="utf-8")
    if "from pathlib import Path" not in text:
        text = "from pathlib import Path\n\n" + text
    replacements = {
        "MAX_APPLIES_NUM": "5",
        "TEST_MODE": "True",
        "EASY_APPLY_ONLY_MODE": "True",
        "READY_MADE_RESUME_PATH": "str(Path.home() / '.local/share/bobby/resumes/resume.pdf')",
        "READY_MADE_PHOTO_PATH": "''",
        "DASHBOARD_OUTPUT_APP_LOGS": "False",
        "MINIMUM_LOG_LEVEL": "'INFO'",
        "LLM_MODEL_TYPE": "'openai'",
        "EASY_APPLY_MODEL": "'gpt-4o-mini'",
        "APPLY_AGENT_MODEL": "'gpt-4o-mini'",
        "EXTERNAL_ATS_ENABLED": "False",
        "EXTERNAL_ATS_AUTO_ACCOUNT_CREATION": "False",
        "EXTERNAL_ATS_RESUME_UPLOAD_ENABLED": "False",
        "GMAIL_APPLICATION_INTEGRATION": "False",
        "GMAIL_CREDENTIALS_PATH": "str(Path.home() / '.config/bobby/gmail_credentials.json')",
        "GMAIL_TOKEN_PATH": "str(Path.home() / '.config/bobby/gmail_token.json')",
    }
    for name, value in replacements.items():
        text = replace_assignment(text, name, value)
    return text.rstrip() + "\n"


def public_pyproject() -> str:
    text = (SOURCE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    text = re.sub(r'(?m)^name\s*=\s*"[^"]+"$', 'name = "bobby-job-application-agent"', text, count=1)
    text = re.sub(r'(?m)^version\s*=\s*"[^"]+"$', f'version = "{VERSION}"', text, count=1)
    text = re.sub(r'(?m)^description\s*=\s*"[^"]+"$', 'description = "Local-first, human-supervised job-application assistant"', text, count=1)
    text = re.sub(r"(?ms)^authors\s*=\s*\[.*?^\]", 'authors = [{name = "Bobby contributors"}]', text, count=1)
    return text.rstrip() + "\n"


def public_lock() -> str:
    text = (SOURCE_ROOT / "uv.lock").read_text(encoding="utf-8")
    text = text.replace('name = "linkedin-ai-jobs-applier-ultimate"', 'name = "bobby-job-application-agent"', 1)
    text = text.replace(
        'name = "bobby-job-application-agent"\nversion = "0.1.0"',
        f'name = "bobby-job-application-agent"\nversion = "{VERSION}"',
        1,
    )
    return text.rstrip() + "\n"


def write_text(destination: Path, relative: str, content: str) -> None:
    path = destination / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def generate_public_files(destination: Path) -> None:
    files = {
        MARKER: json.dumps({"format": 1, "project": "Bobby", "version": VERSION}, indent=2) + "\n",
        ".env.example": SAFE_ENV,
        "examples/.env_example": SAFE_ENV,
        "config/app_config.py": public_app_config(),
        "config/search_config.yaml": SAFE_SEARCH,
        "config/search_config_remote.yaml": SAFE_SEARCH,
        "config/search_config_tampa.yaml": SAFE_SEARCH,
        "examples/config/app_config.py": public_app_config(),
        "examples/config/search_config.yaml": SAFE_SEARCH,
        "candidate_profile.example.yaml": SAFE_PROFILE,
        "examples/data/resumes/structured_resume.yaml": SAFE_RESUME,
        "examples/data/resumes/resume_text.txt": SAFE_RESUME_TEXT,
        "README.md": README,
        "SECURITY.md": SECURITY,
        "CONTRIBUTING.md": CONTRIBUTING,
        "CHANGELOG.md": CHANGELOG,
        "CREDITS.md": CREDITS,
        "ROADMAP.md": ROADMAP,
        "AGENTS.md": AGENTS,
        "docs/linkedin_project_description.md": LINKEDIN_DESCRIPTION,
        ".github/workflows/ci.yml": CI,
        "pyproject.toml": public_pyproject(),
        "uv.lock": public_lock(),
        ".gitignore": """.env
.env.*
!.env.example
candidate_profile.yaml
browser_session/
data/output/
data/debug/
data/resumes/*
!data/resumes/.gitkeep
logs/
backups/
.venv/
.pytest_cache/
__pycache__/
*.pyc
*.before_*
""",
        "data/resumes/.gitkeep": "",
    }
    for relative, content in files.items():
        write_text(destination, relative, content)


def build(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        marker = destination / MARKER
        if not marker.is_file():
            raise RuntimeError("destination exists and is not a generated Bobby public export")
        try:
            metadata = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("destination export marker is invalid") from exc
        if metadata.get("project") != "Bobby":
            raise RuntimeError("destination export marker does not identify Bobby")

    temporary = Path(tempfile.mkdtemp(prefix=".bobby-public-", dir=destination.parent))
    preserved_git: Path | None = None
    try:
        copy_source_tree(temporary)
        generate_public_files(temporary)
        check(temporary, quiet=True)
        if destination.exists():
            destination_git = destination / ".git"
            if destination_git.exists():
                preserved_git = Path(tempfile.mkdtemp(prefix=".bobby-public-git-", dir=destination.parent)) / ".git"
                shutil.move(str(destination_git), str(preserved_git))
            shutil.rmtree(destination)
        temporary.rename(destination)
        if preserved_git is not None:
            shutil.move(str(preserved_git), str(destination / ".git"))
            preserved_git.parent.rmdir()
            preserved_git = None
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        if preserved_git is not None and preserved_git.exists():
            destination.mkdir(parents=True, exist_ok=True)
            shutil.move(str(preserved_git), str(destination / ".git"))
            preserved_git.parent.rmdir()
        raise
    print(f"PUBLIC BUILD PASS {destination}")


def tracked_files(root: Path) -> set[str]:
    if not (root / ".git").exists():
        return set()
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files"],
        check=False,
        capture_output=True,
        text=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def scan_file(path: Path) -> list[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return ["unreadable"]
    if b"\x00" in data:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []

    issues: list[str] = []
    # Construct sensitive prefixes so the checker does not flag its own code.
    patterns = (
        re.compile(r"(?i)" + "sk" + r"-[A-Za-z0-9]{20,}"),
        re.compile(r"(?i)" + "ghp" + r"_[A-Za-z0-9]{20,}"),
        re.compile(r"(?i)" + "AIza" + r"[A-Za-z0-9_-]{20,}"),
        re.compile(r"-----BEGIN " + "PRIVATE KEY" + r"-----"),
    )
    if any(pattern.search(text) for pattern in patterns):
        issues.append("credential-like value")
    if re.search(r"/(?:home|Users)/[A-Za-z0-9_.-]+(?:/|$)", text):
        issues.append("machine-specific absolute path")
    email_pattern = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    def reserved_email(email: str) -> bool:
        domain = email.rsplit("@", 1)[-1].lower()
        return domain.endswith((".example", ".invalid", ".test")) or domain in {"example.com", "example.org"}

    if any(not reserved_email(email) for email in email_pattern.findall(text)):
        issues.append("non-example email")
    credential_urls = re.findall(
        r"https?://[^\s'\"<>]+[?&](?:token|key|secret|auth|code)=",
        text,
        re.IGNORECASE,
    )
    if any(
        not urlparse(url).hostname
        or not urlparse(url).hostname.lower().endswith((".example", ".invalid", ".test"))
        for url in credential_urls
    ):
        issues.append("credential-bearing URL")
    return issues


def check(root: Path, *, quiet: bool = False) -> None:
    failures: list[tuple[str, str, str]] = []
    required = (
        "README.md", "LICENSE", "SECURITY.md", "ROADMAP.md", ".github/workflows/ci.yml",
        "config/app_config.py", ".env.example", "candidate_profile.example.yaml",
        "uv.lock", "scripts/public_release.py",
    )
    for relative in required:
        if not (root / relative).is_file():
            failures.append(("REQUIRED", relative, "MISSING"))
    if (root / "requirements.txt").exists() or (root / "Dockerfile").exists():
        failures.append(("POLICY", "requirements.txt or Dockerfile", "PRESENT"))
    if not (root / MARKER).is_file():
        failures.append(("REQUIRED", MARKER, "MISSING"))

    tracked = tracked_files(root)
    private_names = {".env", "candidate_profile.yaml", "browser_state.json", "credentials.json", "token.json"}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        parts = set(Path(relative).parts)
        if parts & PRIVATE_PARTS:
            continue
        if path.name in private_names or parts & {"browser_session", "logs", "audit", "backups"}:
            failures.append(("NAME", relative, "PRIVATE"))
        if relative.startswith(("data/output/", "data/debug/")):
            failures.append(("NAME", relative, "RUNTIME_DATA"))
        for issue in scan_file(path):
            failures.append(("CONTENT", relative, issue))

    if not quiet:
        for kind, relative, issue in failures:
            state = "TRACKED" if relative in tracked else "UNTRACKED"
            print(f"{kind} {relative} {state} UNSAFE ({issue})")
        print("PUBLIC_CHECK PASS" if not failures else f"PUBLIC_CHECK FAIL ({len(failures)} issue(s))")
    if failures:
        if quiet:
            raise RuntimeError(f"public export check found {len(failures)} issue(s)")
        raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "check", "publish"))
    parser.add_argument("--destination", help="public export directory")
    args = parser.parse_args()
    destination = Path(args.destination or os.getenv("BOBBY_PUBLIC_DIR", str(DEFAULT_DESTINATION))).expanduser().resolve()
    if args.command in {"build", "publish"}:
        build(destination)
    check(destination)
    if args.command == "publish":
        print("PUBLIC PREFLIGHT PASS; use the GitHub integration to publish the checked tree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
