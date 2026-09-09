"""Read durable, cumulative new-job counts for the production round guard."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def read_new_job_count(path: Path, *, allow_missing: bool = False) -> int:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if allow_missing:
            return 0
        raise ValueError("Search progress state is missing") from None
    except (OSError, ValueError):
        raise ValueError("Search progress state is unreadable") from None
    aggregate = state.get("aggregate") if isinstance(state, dict) else None
    count = aggregate.get("new") if isinstance(aggregate, dict) else None
    if type(count) is not int or count < 0:
        raise ValueError("Search progress count is invalid")
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--allow-missing", action="store_true")
    args = parser.parse_args()
    try:
        print(read_new_job_count(args.path, allow_missing=args.allow_missing))
    except ValueError:
        # Never print the state payload: only the launcher needs the count.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
