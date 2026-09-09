#!/usr/bin/python3
"""Start Bobby's GTK control center with the compatible system/runtime split.

Fedora provides PyGObject to the system Python, while Bobby's pinned Python
environment contains the application's own dependencies.  Keep that boundary
explicit for the desktop entry instead of relying on whichever interpreter the
desktop happens to choose.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping


REPOSITORY = Path(__file__).resolve().parent
SYSTEM_PYTHON = Path("/usr/bin/python3")
GUI_PROGRAM = REPOSITORY / "bobby_gui.py"


class GuiRuntimeError(RuntimeError):
    """Raised when the installed Bobby runtime cannot support the GUI."""


def venv_site_packages(repository: Path = REPOSITORY) -> Path:
    """Return Bobby's sole virtual-environment site-packages directory."""
    candidates = sorted((repository / ".venv" / "lib").glob("python*/site-packages"))
    if len(candidates) != 1:
        raise GuiRuntimeError(
            "Bobby GUI needs its prepared .venv site-packages directory; "
            "run `uv sync` from the Bobby repository first."
        )
    return candidates[0]


def gui_environment(
    repository: Path = REPOSITORY, environment: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Prepend Bobby dependencies while preserving the desktop environment."""
    result = dict(os.environ if environment is None else environment)
    existing = result.get("PYTHONPATH")
    paths = [str(venv_site_packages(repository))]
    if existing:
        paths.append(existing)
    result["PYTHONPATH"] = os.pathsep.join(paths)
    return result


def main() -> int:
    if not SYSTEM_PYTHON.is_file():
        raise GuiRuntimeError(f"GTK-capable system Python not found: {SYSTEM_PYTHON}")
    if not GUI_PROGRAM.is_file():
        raise GuiRuntimeError(f"Bobby GUI program not found: {GUI_PROGRAM}")
    os.execve(
        str(SYSTEM_PYTHON),
        [str(SYSTEM_PYTHON), str(GUI_PROGRAM), *sys.argv[1:]],
        gui_environment(),
    )
    return 1  # pragma: no cover - execve replaces this process on success.


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GuiRuntimeError as exc:
        print(f"Bobby GUI could not start: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
