"""The coordinator's work list: every task one tick must clear, in order.

    uv run -m workflow.coordinator                  # production (gh auth)
    uv run -m workflow.coordinator --sim w.db       # a simulated GitHub
    uv run -m workflow.coordinator --json           # for scripts
    uv run -m workflow.coordinator take review obs-studio 123 --owner coord-a
    uv run -m workflow.coordinator done review obs-studio 123 --owner coord-a

Several coordinators may run at once. Before acting on a task, take its
lease (``lease.py``): exit status 0 means it is yours, 1 means another
coordinator holds it, so skip it. The list leaves out tasks others hold.

The coordinator (the obs-org-coordinator skill) runs this at the start of
every tick and works the list top to bottom. A tick is "quiet" only when the
list is empty. Pure function ``tasks`` over the same items the board sync
reads, so every rule is tested in simulation (tests/test_coordinator.py).

Order, most urgent first:
  fix-master     the default branch is red: fix it before queuing anything
  requeue        a PR left the merge queue with a conflict: rebase, re-queue
  fix-ci         a ready PR's checks fail: rerun a flake, fix a mechanical
                 failure, or comment the cause for the author
  enqueue        a green, mergeable PR is not in the merge queue
  merge          a green, reviewed PR in a repo with no merge queue
  merge-local    a reviewed PR in a repo with no PR CI: run its tests, then merge
  rebase         a PR conflicts with or is behind its base
  review         a ready PR has no review
  decide         an open decision-needed question
  triage         an issue nobody has checked against the definition of Ready
  stalled        a PR with no activity for 72 hours: ping, or take it over
  investigating  an investigation with nobody on it
  escalated      a human-review-needed question waiting on the maintainer
                 (report only)

Workers stop once their PR is in the merge queue; everything after that
(ejections, rebases, CI fixes) is here, so nothing waits on an author who
moved on.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from workflow import board, claims, lease
from workflow.model import Item

ORDER = [
    "fix-master",
    "requeue",
    "fix-ci",
    "enqueue",
    "merge",
    "merge-local",
    "rebase",
    "review",
    "decide",
    "triage",
    "stalled",
    "investigating",
    "escalated",
]
HOW = {
    "fix-master": "Read the failing run on the default branch, find the PR that broke it "
    "and fix or revert it first. Queued PRs fail until it is green. A failure only an "
    "admin can fix (token, secret, branch rule): open or update one `human-review-needed` "
    "issue in that repo with the log line, and report it.",
    "requeue": "UNMERGEABLE in the queue: read its merge-group run; a semantic clash "
    "with master (a new lint, a renamed helper) needs a fixup: dequeue, push, `--auto`. "
    "The queue ejected it (or it conflicts): rebase on the default branch, "
    "keep both sides of list conflicts (docs/features.md), push with "
    "--force-with-lease, then `gh pr merge <N> --auto` again.",
    "fix-ci": "Run still in progress (logs locked)? Skip it this tick. Read the failed job log. Flake: `gh run rerun <run> --failed`. Lint, format, "
    "a count or a ledger line: push a `fixup:` commit. A real failure in the PR's "
    "logic: comment the cause and the log line for the author.",
    "enqueue": "`gh pr merge <N> -R obs-rust/<repo> --auto`, then confirm it is queued: "
    '`gh api graphql -f query=\'{repository(owner:"obs-rust",name:"<repo>")'
    "{pullRequest(number:<N>){isInMergeQueue}}}'`.",
    "merge": "The repo has no merge queue. First `uv run -m workflow.coordinator checks <repo> "
    "<N>` must exit 0 (every check finished, none failed); then "
    "`gh pr merge <N> -R obs-rust/<repo> --squash` "
    "once it is green and reviewed (an unreviewed one is listed under review).",
    "merge-local": "The repo runs no CI on PRs. Check out the PR head (plumbing or the "
    "local checkout), run the repo's own test and lint commands (README/AGENTS.md); all "
    "pass: `gh pr merge <N> -R obs-rust/<repo> --squash` and say in a comment what you "
    "ran. Any fail: comment the output for the author.",
    "rebase": "`gh pr update-branch <N> --rebase`, or rebase by hand when it conflicts.",
    "review": "Review it: correctness, RED then GREEN, required test tiers, the features "
    "ledger and AGENTS.md. Post a PR review; push mechanical fixups yourself. Then, if `uv run -m "
    "workflow.coordinator checks <repo> <N>` exits 0: queue it (`--auto`), or in a repo with no merge queue, merge it now.",
    "decide": "Research it in the rules, docs and earlier decisions. Settled, or a safe "
    "reversible default: comment `Decided: ...`. Otherwise relabel "
    "human-review-needed with the options and a recommendation.",
    "triage": "Check it against the definition of Ready (reproduction or failing-test "
    "sketch, acceptance criteria, dependencies linked and closed, no open "
    "decision). Meets it: comment `/ready` and add a P0-P3 and size:S-XL label. "
    "Underdefined: `/investigate`. Not a task: close it, or move it to "
    "Discussions.",
    "stalled": "Ask the author whether they are still on it. A handed-off PR, or one "
    "silent for 7 days: take over the mechanical part yourself.",
    "investigating": "Nobody is researching it: comment what is missing, or do the "
    "research and `/ready` it.",
    "escalated": "Waiting on the maintainer: list it in the report with its age.",
}
STALLED_HOURS = board.STALLED_HOURS
TRIAGE_GRACE = timedelta(hours=1)  # give the reporter time to finish the issue


@dataclass
class Task:
    kind: str
    repo: str
    number: int
    title: str
    age_hours: float
    labels: list[str] = field(default_factory=list)

    @property
    def rank(self) -> tuple[int, float]:
        return ORDER.index(self.kind), -self.age_hours


def task(kind: str, item: Item, now: datetime, since: datetime | None = None) -> Task:
    age = (now - (since or item.created)).total_seconds() / 3600
    return Task(kind, item.repo, item.number, item.title, round(age, 1), sorted(item.labels))


def pr_task(item: Item, now: datetime) -> str | None:
    if item.labels & board.DECISION_LABELS:
        return None  # its question is the task (decide / escalated)
    if item.in_queue:
        # A failing merge group fails every entry behind it until fixed.
        return "requeue" if item.queue_state == "UNMERGEABLE" else None
    if item.draft:
        return "stalled" if now - item.updated >= timedelta(hours=STALLED_HOURS) else None
    if item.checks in ("FAILURE", "ERROR"):
        return "fix-ci"
    if item.merge_state == "DIRTY":
        # A PR that passed and got a review was in (or bound for) the queue.
        return "requeue" if item.checks == "SUCCESS" else "rebase"
    if item.merge_state == "BEHIND":
        return "rebase"
    if not item.merge_queue and not item.checks and item.merge_state in ("CLEAN", "UNSTABLE"):
        return "merge-local" if item.reviews else "review"  # no CI will ever report
    if item.merge_state == "CLEAN" and item.checks == "SUCCESS":
        if item.merge_queue:
            return "enqueue"
        return "merge" if item.reviews else "review"
    if not item.reviews:
        return "review"
    if now - item.updated >= timedelta(hours=STALLED_HOURS):
        return "stalled"
    return None


def issue_task(item: Item, now: datetime) -> str | None:
    labels = item.labels
    if "human-review-needed" in labels:
        return "escalated"
    if "decision-needed" in labels:
        return "decide"
    if item.open_blockers or "claim-blocked" in labels or item.linked_prs_in_queue:
        return None
    if "needs-investigation" in labels:
        return None if item.assignees or claims.CLAIMED in labels else "investigating"
    claimed = item.assignees or claims.CLAIMED in labels  # legacy label, pre-migration
    if claimed or board.is_tracker(item) or "ready" in labels or {"P0", "critical"} & labels:
        return None
    return "triage" if now - item.created >= TRIAGE_GRACE else None


def tasks(
    items: list[Item], red: list[str], now: datetime, held: dict[str, str] | None = None
) -> list[Task]:
    """The work list, leaving out tasks another coordinator holds."""
    held = held or {}
    out = [Task("fix-master", repo, 0, "default branch is red", 0.0) for repo in red]
    for item in items:
        if item.state != "OPEN":
            continue
        kind = pr_task(item, now) if item.kind == "pr" else issue_task(item, now)
        if kind == "fix-ci" and item.repo in red:
            continue  # it fails because master does; fix-master covers it
        if kind:
            out.append(task(kind, item, now))
    out = [t for t in out if lease.key(t.kind, t.repo, t.number) not in held]
    return sorted(out, key=lambda t: t.rank)


def report(work: list[Task], per_kind: int = 15) -> str:
    """The list for a human or an agent: each kind with its instructions,
    oldest first, at most `per_kind` lines each."""
    if not work:
        return "quiet: nothing to do"
    lines: list[str] = []
    for kind in ORDER:
        mine = [t for t in work if t.kind == kind]
        if not mine:
            continue
        lines += ["", f"## {kind} ({len(mine)})", HOW[kind]]
        for t in mine[:per_kind]:
            ref = f"{t.repo}#{t.number}" if t.number else t.repo
            tags = f" [{', '.join(t.labels)}]" if t.labels else ""
            lines.append(f"- {ref} ({t.age_hours:.0f} h) {t.title}{tags}")
        if len(mine) > per_kind:
            lines.append(f"- ... {len(mine) - per_kind} more (--json lists all)")
    return "\n".join(lines).strip()


def green(passed: int, failed: int, pending: int) -> bool:
    """Merge or queue only when every check finished and none failed: a
    rollup can read SUCCESS while jobs are still running (obs-deps#6 merged
    with four FFmpeg builds in progress)."""
    return passed > 0 and failed == 0 and pending == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", nargs="?", default="list", choices=["list", "take", "done", "checks"])
    ap.add_argument(
        "task", nargs="*", help="KIND REPO NUMBER for take and done; REPO NUMBER for checks"
    )
    ap.add_argument("--owner", default=os.environ.get("COORDINATOR_ID", "coordinator"))
    ap.add_argument("--org", default=os.environ.get("PROJECT_OWNER", "obs-rust"))
    ap.add_argument("--sim", help="SQLite world to run against instead of GitHub")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.sim:
        from workflow.sim import SimBackend

        gh = SimBackend(args.sim)
        now = gh.now
    else:
        from workflow.gh import GhBackend

        gh, now = GhBackend(), datetime.now(timezone.utc)
    if args.action == "checks":
        repo, number = args.task
        passed, failed, pending = gh.check_counts(repo, int(number))
        print(f"{repo}#{number}: {passed} passed, {failed} failed, {pending} running")
        if not passed + failed + pending:
            print("no CI reports on this PR: if the repo has none, it is a merge-local task")
        return 0 if green(passed, failed, pending) else 1
    if args.action != "list":
        kind, repo, number = args.task
        k = lease.key(kind, repo, int(number))
        if args.action == "take":
            ok, holder = lease.take(gh, k, args.owner, now)
            print(f"took {k}" if ok else f"held by {holder}: skip {k}")
            return 0 if ok else 1
        print(f"released {k}" if lease.release(gh, k, args.owner) else f"not yours: {k}")
        return 0
    held = lease.held_by_others(gh, args.owner, now)
    work = tasks(gh.items(args.org), gh.red_branches(args.org), now, held)
    print(json.dumps([asdict(t) for t in work], indent=1) if args.json else report(work))
    return 0


if __name__ == "__main__":
    sys.exit(main())
