# Security and privacy

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
