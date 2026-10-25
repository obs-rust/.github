"""Claim bot entry point: apply ``claims`` decisions through a backend.

    uv run -m workflow.claim_bot auto               # from GITHUB_EVENT_NAME (Actions)
    uv run -m workflow.claim_bot comment            # an issue_comment event
    uv run -m workflow.claim_bot sweep              # the hourly expiry sweep
    uv run -m workflow.claim_bot sweep --sim w.db   # against a simulated GitHub

The repo comes from --repo or GITHUB_REPOSITORY, the comment event from
GITHUB_EVENT_PATH. The comment body is only ever read from the event file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from workflow import claims, log
from workflow.gh import ts
from workflow.model import Comment


def apply(gh, repo: str, number: int, actions: list[tuple], dry_run: bool = False) -> None:
    for action in actions:
        kind, arg = action
        log(f"{repo}#{number}: {kind} {arg if kind != 'comment' else arg[:80]!r}")
        if not dry_run:
            gh.apply_issue(repo, number, action)


def on_comment(gh, repo: str, event: dict, now: datetime, dry_run: bool = False) -> None:
    if "pull_request" in event["issue"]:
        return
    c = event["comment"]
    comment = Comment(
        c["user"]["login"],
        c["author_association"],
        c["body"] or "",
        ts(c["created_at"]) if isinstance(c["created_at"], str) else c["created_at"],
        c["id"],
    )
    labels = {lbl["name"] for lbl in event["issue"].get("labels", [])}
    if claims.is_bot(comment.author) or not claims.handles(comment.body, labels):
        return
    number = event["issue"]["number"]
    issue = gh.issue(repo, number)
    # The new comment is already in the history; decide as of just before it.
    issue.comments = [x for x in issue.comments if x.id != comment.id]
    apply(gh, repo, number, claims.decide_comment(issue, comment, now), dry_run)


def sweep(gh, repo: str, now: datetime, dry_run: bool = False) -> None:
    for number in gh.claimed_issues(repo):
        apply(gh, repo, number, claims.decide_sweep(gh.issue(repo, number), now), dry_run)
    catch_up(gh, repo, now, dry_run)


def catch_up(gh, repo: str, now: datetime, dry_run: bool = False) -> None:
    """Replay commands whose comment event never reached the bot."""
    for number in gh.issues_commented_since(repo, now - claims.CATCH_UP):
        issue = gh.issue(repo, number)
        for c in claims.missed(issue, now):
            issue = gh.issue(repo, number)  # the state the previous replay left
            before = [x for x in issue.comments if x.created < c.created or x.id < c.id]
            issue.comments = [x for x in before if x.id != c.id]
            acts = claims.effective(issue, claims.decide_comment(issue, c, now))
            log(f"{repo}#{number}: replaying comment {c.id} by {c.author}")
            apply(gh, repo, number, acts, dry_run)


def backend(sim: str | None):
    if sim:
        from workflow.sim import SimBackend

        return SimBackend(sim)
    from workflow.gh import GhBackend

    return GhBackend()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["auto", "comment", "sweep"])
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--sim", help="SQLite world to run against instead of GitHub")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.mode == "auto":
        args.mode = "comment" if os.environ.get("GITHUB_EVENT_NAME") == "issue_comment" else "sweep"
    gh = backend(args.sim)
    now = gh.now if args.sim else datetime.now(timezone.utc)
    if args.mode == "comment":
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
            on_comment(gh, args.repo, json.load(f), now, args.dry_run)
    else:
        sweep(gh, args.repo, now, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
