#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Keep the obs-rust Multiview project in step with every repo in the org.

Each run:

  1. Lists every non-archived repo in the org (new repos join on their own;
     private ``*-ghsa-*`` advisory forks are skipped).
  2. Pages through every issue and PR of each repo, open or closed, and
     adds what the project lacks, so the board holds the org's whole history
     for progress charts (views filter it: is:open, is:merged, is:issue...).
  3. Sets Status on each item from its live state, so the board is the
     coordinator's master list:

       Needs decision  label decision-needed or human-review-needed
       In merge queue  PR waiting in the merge queue
       Ready to merge  PR, not a draft, mergeStateStatus CLEAN
       Needs fix       PR whose checks fail, or that conflicts (DIRTY)
       In review       any other open, non-draft PR
       In progress     draft PR, or an issue labeled claimed
       Triage          any other open issue
       Done            closed issue, merged or closed PR

Every mutation is idempotent; a run changes only what drifted.

Env: GH_TOKEN (GitHub App token: Projects read/write, Issues, Pull requests
and Metadata read), PROJECT_OWNER, PROJECT_NUMBER, DRY_RUN=1 to print only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

OWNER = os.environ.get("PROJECT_OWNER", "obs-rust")
NUMBER = int(os.environ.get("PROJECT_NUMBER", "0"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
STATUSES = [
    "Triage",
    "Needs decision",
    "In progress",
    "Needs fix",
    "In review",
    "Ready to merge",
    "In merge queue",
    "Done",
]
# Reads run wide; writes stay narrow, since bursts of project mutations
# trip GitHub's secondary rate limit.
READ_WORKERS = 12
WRITE_WORKERS = 6
DECISION_LABELS = {"decision-needed", "human-review-needed"}


def graphql(query: str, **variables: Any) -> dict[str, Any]:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, int) else "-f"
        args += [flag, f"{key}={value}"]
    for attempt in range(4):
        run = subprocess.run(args, capture_output=True, text=True)
        if run.returncode == 0:
            return json.loads(run.stdout)["data"]
        # Secondary rate limits and 502s are common on bursts of mutations.
        print(f"gh api graphql failed (try {attempt + 1}): {run.stderr.strip()}")
        time.sleep(5 * 2**attempt)
    raise RuntimeError("gh api graphql kept failing")


def org_repos() -> list[str]:
    repos: list[str] = []
    cursor = None
    while True:
        d = graphql(
            """query($o: String!, $c: String) { organization(login: $o) {
              repositories(first: 100, after: $c, isArchived: false) {
                pageInfo { hasNextPage endCursor } nodes { name } } } }""",
            o=OWNER,
            **({"c": cursor} if cursor else {}),
        )["organization"]["repositories"]
        repos += [n["name"] for n in d["nodes"] if "-ghsa-" not in n["name"]]
        if not d["pageInfo"]["hasNextPage"]:
            return repos
        cursor = d["pageInfo"]["endCursor"]


ISSUE_FIELDS = "id number state labels(first: 20) { nodes { name } }"
PR_FIELDS = (
    "id number state isDraft isInMergeQueue mergeStateStatus labels(first: 20) { nodes { name } } "
    "commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }"
)


def paged(repo: str, connection: str, states: str, fields: str) -> list[dict[str, Any]]:
    """Every node of one repo connection, 100 per page."""
    nodes: list[dict[str, Any]] = []
    cursor = None
    while True:
        d = graphql(
            f"""query($o: String!, $r: String!, $c: String) {{
              repository(owner: $o, name: $r) {{
                {connection}(states: [{states}], first: 100, after: $c) {{
                  pageInfo {{ hasNextPage endCursor }} nodes {{ {fields} }} }} }} }}""",
            o=OWNER,
            r=repo,
            **({"c": cursor} if cursor else {}),
        )["repository"][connection]
        nodes += d["nodes"]
        if not d["pageInfo"]["hasNextPage"]:
            return nodes
        cursor = d["pageInfo"]["endCursor"]


def repo_items(repo: str) -> list[dict[str, Any]]:
    """Every issue and PR of one repo, open or closed, with its Status."""
    issues = paged(repo, "issues", "OPEN, CLOSED", ISSUE_FIELDS)
    prs = paged(repo, "pullRequests", "OPEN, MERGED, CLOSED", PR_FIELDS)
    for n in issues:
        n["status"] = status_of(n, is_pr=False)
    for n in prs:
        n["status"] = status_of(n, is_pr=True)
    return issues + prs


def status_of(n: dict[str, Any], *, is_pr: bool) -> str:
    if n["state"] != "OPEN":
        return "Done"
    labels = {lbl["name"] for lbl in n["labels"]["nodes"]}
    if labels & DECISION_LABELS:
        return "Needs decision"
    if not is_pr:
        return "In progress" if "claimed" in labels else "Triage"
    if n["isInMergeQueue"]:
        return "In merge queue"
    if n["isDraft"]:
        return "In progress"
    commits = n["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    if (rollup or {}).get("state") in ("FAILURE", "ERROR") or n[
        "mergeStateStatus"
    ] == "DIRTY":
        return "Needs fix"
    if n["mergeStateStatus"] == "CLEAN":
        return "Ready to merge"
    return "In review"


def project() -> tuple[str, str, dict[str, str], dict[str, dict[str, Any]]]:
    """Project id, Status field id, option ids, and items keyed by content id."""
    items: dict[str, dict[str, Any]] = {}
    cursor = None
    while True:
        d = graphql(
            """query($o: String!, $n: Int!, $c: String) {
              organization(login: $o) { projectV2(number: $n) { id
                field(name: "Status") { ... on ProjectV2SingleSelectField {
                  id options { id name } } }
                items(first: 100, after: $c) {
                  pageInfo { hasNextPage endCursor }
                  nodes { id
                    status: fieldValueByName(name: "Status") {
                      ... on ProjectV2ItemFieldSingleSelectValue { name } }
                    content { ... on Issue { id state }
                              ... on PullRequest { id state } } } } } } }""",
            o=OWNER,
            n=NUMBER,
            **({"c": cursor} if cursor else {}),
        )["organization"]["projectV2"]
        for n in d["items"]["nodes"]:
            if n["content"] and n["content"].get("id"):
                items[n["content"]["id"]] = n
        if not d["items"]["pageInfo"]["hasNextPage"]:
            break
        cursor = d["items"]["pageInfo"]["endCursor"]
    field = d["field"]
    options = {o["name"]: o["id"] for o in field["options"]}
    missing = [s for s in STATUSES if s not in options]
    if missing:
        sys.exit(f"Status field lacks options: {missing}")
    return d["id"], field["id"], options, items


def set_status(pid: str, fid: str, item: str, option: str) -> None:
    graphql(
        """mutation($p: ID!, $i: ID!, $f: ID!, $o: String!) {
          updateProjectV2ItemFieldValue(input: {projectId: $p, itemId: $i,
            fieldId: $f, value: {singleSelectOptionId: $o}}) {
            projectV2Item { id } } }""",
        p=pid,
        i=item,
        f=fid,
        o=option,
    )


def place(
    pid: str, fid: str, item: dict[str, Any] | None, n: dict[str, Any], option: str
) -> str:
    """Add one item if the project lacks it, then set its Status."""
    if item is None:
        item_id = graphql(
            """mutation($p: ID!, $c: ID!) { addProjectV2ItemById(
              input: {projectId: $p, contentId: $c}) { item { id } } }""",
            p=pid,
            c=n["id"],
        )["addProjectV2ItemById"]["item"]["id"]
    else:
        item_id = item["id"]
    set_status(pid, fid, item_id, option)
    return "added" if item is None else "moved"


def main() -> int:
    if not NUMBER:
        sys.exit("PROJECT_NUMBER is required")
    pid, fid, options, existing = project()
    with ThreadPoolExecutor(READ_WORKERS) as pool:
        per_repo = list(zip(repos := org_repos(), pool.map(repo_items, repos)))

    jobs: list[tuple[dict[str, Any] | None, dict[str, Any], str]] = []
    for repo, items in per_repo:
        for n in items:
            item = existing.get(n["id"])
            current = ((item or {}).get("status") or {}).get("name")
            if current != n["status"]:
                print(f"{repo}#{n['number']}: {current or 'new'} -> {n['status']}")
                jobs.append((item, n, n["status"]))
    if DRY_RUN:
        print(f"dry run: {len(jobs)} changes")
        return 0

    counts = {"added": 0, "moved": 0, "failed": 0}
    with ThreadPoolExecutor(WRITE_WORKERS) as pool:
        futures = [
            pool.submit(place, pid, fid, item, n, options[s]) for item, n, s in jobs
        ]
        for future in as_completed(futures):
            try:
                counts[future.result()] += 1
            except RuntimeError:
                counts["failed"] += 1
    print(
        f"added {counts['added']}, status changed on {counts['moved']}, "
        f"failed {counts['failed']}"
    )
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
