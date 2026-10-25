"""Board sync entry point: bring the org project in line with every repo.

    uv run -m workflow.board_sync                   # production (GH_TOKEN)
    uv run -m workflow.board_sync --sim w.db        # a simulated GitHub
    uv run -m workflow.board_sync --dry-run         # print the plan only

Fields and options the project lacks are created first (existing options
keep their ids, so no item loses its value). Then every issue and PR of every
repo is added if missing and given its Status, Priority, Size, Opened and
Closed values (``board.py``). A run changes only what drifted.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from workflow import board, log


def sync(gh, org: str, number: int, now: datetime, dry_run: bool = False) -> dict[str, int]:
    project = gh.board(org, number)
    if fixes := board.missing_fields(project):
        log(f"fields: {fixes}")
        if dry_run:
            return {"added": 0, "changed": 0, "failed": 0}
        gh.ensure_fields(project, fixes)
        project = gh.board(org, number)
    ops = board.plan(gh.items(org), project, now)
    if dry_run:
        for op in ops:
            log(str(op))
        return {"added": 0, "changed": 0, "failed": 0}
    counts = gh.apply_board(project, ops)
    log(f"added {counts['added']}, changed {counts['changed']}, failed {counts['failed']}")
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default=os.environ.get("PROJECT_OWNER", "obs-rust"))
    ap.add_argument("--project", type=int, default=int(os.environ.get("PROJECT_NUMBER", "1")))
    ap.add_argument("--sim", help="SQLite world to run against instead of GitHub")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.sim:
        from workflow.sim import SimBackend

        gh = SimBackend(args.sim)
        now = gh.now
    else:
        from workflow.gh import GhBackend

        gh, now = GhBackend(), datetime.now(timezone.utc)
    counts = sync(gh, args.org, args.project, now, args.dry_run)
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
