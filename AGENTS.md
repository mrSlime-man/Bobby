# Agent and maintainer notes

Bobby is safety-sensitive local automation. Preserve these invariants:

- Never commit credentials, candidate PII, resumes, browser state, logs, or
  application history.
- Keep submit-state classification conservative; an attempted submit without
  evidence is not a successful submission.
- Keep browser automation behind explicit configuration and human review.
- Keep tests offline and deterministic; never access real job sites in CI.
- Run locked dependency, compile, and pytest checks before a PR.
- Use `scripts/public_release.py` for public exports.
