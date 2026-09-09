# Contributing

Create a focused branch, describe the user-visible behavior, and run
`uv sync --locked`, compile checks, and `uv run pytest -q`. Add tests for
safety-critical behavior. CI must stay offline: do not access real job sites,
accounts, or submit applications.

For release work, run `uv run python scripts/public_release.py build` and
`uv run python scripts/public_release.py check` from the private checkout.
Never copy private runtime files into the public tree manually.
