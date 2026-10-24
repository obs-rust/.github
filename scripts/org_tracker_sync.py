#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Keep the obs-rust Tracker project in step with every repo in the org.

Each run:

  1. Lists every non-archived repo in the org (new repos join on their own;
     private ``*-ghsa-*`` advisory forks are skipped).
  2. Adds each open issue and PR that the project lacks.
  3. Sets Status on each open item from its live state, so the board is the
     coordinator's master list:

       Needs decision  label decision-needed or human-review-needed
       Ready to merge  PR, not a draft, mergeStateStatus CLEAN
       Needs fix       PR whose checks fail, or that conflicts (DIRTY)
       In review       any other open, non-draft PR
       In progress     draft PR, or an issue labeled claimed
       Triage          any other open issue
  4. Sets Status to Done on items whose issue or PR has closed.

Every mutation is idempotent; a run changes only what drifted.

Env: GH_TOKEN (GitHub App token: Projects read/write, Issues, Pull requests
and Metadata read), PROJECT_OWNER, PROJECT_NUMBER, DRY_RUN=1 to print only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
    "Done",
]
DECISION_LABELS = {"decision-needed", "human-review-needed"}


def graphql(query: str, **variables: Any) -> dict[str, Any]:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, int) else "-f"
        args += [flag, f"{key}={value}"]
    out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    return json.loads(out)["data"]


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


def open_items(repo: str) -> list[dict[str, Any]]:
    """Open issues and PRs of one repo, with what Status needs."""
    d = graphql(
        """query($o: String!, $r: String!) { repository(owner: $o, name: $r) {
          issues(states: OPEN, first: 100) { nodes {
            id number labels(first: 20) { nodes { name } } } }
          pullRequests(states: OPEN, first: 100) { nodes {
            id number isDraft mergeStateStatus
            labels(first: 20) { nodes { name } }
            commits(last: 1) { nodes { commit {
              statusCheckRollup { state } } } } } } } }""",
        o=OWNER,
        r=repo,
    )["repository"]
    items = []
    for kind in ("issues", "pullRequests"):
        for n in d[kind]["nodes"]:
            n["status"] = status_of(n, is_pr=kind == "pullRequests")
            items.append(n)
    return items


def status_of(n: dict[str, Any], *, is_pr: bool) -> str:
    labels = {lbl["name"] for lbl in n["labels"]["nodes"]}
    if labels & DECISION_LABELS:
        return "Needs decision"
    if not is_pr:
        return "In progress" if "claimed" in labels else "Triage"
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


def main() -> int:
    if not NUMBER:
        sys.exit("PROJECT_NUMBER is required")
    pid, fid, options, existing = project()
    added = moved = 0
    live: set[str] = set()
    for repo in org_repos():
        for n in open_items(repo):
            live.add(n["id"])
            item = existing.get(n["id"])
            current = ((item or {}).get("status") or {}).get("name")
            if current == n["status"]:
                continue
            print(f"{repo}#{n['number']}: {current or 'new'} -> {n['status']}")
            if DRY_RUN:
                continue
            if item is None:
                item_id = graphql(
                    """mutation($p: ID!, $c: ID!) { addProjectV2ItemById(
                      input: {projectId: $p, contentId: $c}) { item { id } } }""",
                    p=pid,
                    c=n["id"],
                )["addProjectV2ItemById"]["item"]["id"]
                added += 1
            else:
                item_id = item["id"]
                moved += 1
            set_status(pid, fid, item_id, options[n["status"]])
    for content_id, item in existing.items():
        closed = item["content"].get("state") in ("CLOSED", "MERGED")
        if content_id not in live and closed and (item.get("status") or {}).get(
            "name"
        ) != "Done":
            print(f"{content_id}: -> Done")
            if not DRY_RUN:
                set_status(pid, fid, item["id"], options["Done"])
                moved += 1
    print(f"added {added}, status changed on {moved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
