"""Production backend: GitHub through the `gh` CLI (GH_TOKEN in CI)."""

from __future__ import annotations

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from workflow import ghcache, log
from workflow.claims import BOT, CLAIMED, COMMANDS, REACTION, is_bot
from workflow.model import Board, Comment, Field, Issue, Item, LinkedPR

# Few parallel calls: GitHub's secondary (burst) limit trips on concurrency
# long before the hourly points run out, and writes trip it soonest.
READ_WORKERS = 4
WRITE_WORKERS = 3
RED_DAYS = 7


def ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def run(args: list[str]) -> str:
    """Run gh, retrying with backoff: secondary rate limits and 502s are
    common on bursts of calls."""
    for attempt in range(4):
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout
        print(f"gh failed (try {attempt + 1}): {proc.stderr.strip()}")
        # A rate limit clears in about a minute; other failures retry sooner.
        limited = "rate limit" in proc.stderr.lower()
        time.sleep((60 if limited else 5) * 2**attempt)
    raise RuntimeError(f"gh kept failing: {' '.join(args[:4])}")


def rest(path: str, paginate: bool = True) -> Any:
    args = ["gh", "api", "-H", "Accept: application/vnd.github+json", path]
    if paginate:
        args[2:2] = ["--paginate", "--slurp"]
    out = json.loads(run(args))
    return [x for page in out for x in page] if paginate else out


def graphql(query: str, **variables: Any) -> dict[str, Any]:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, int) else "-f"
        args += [flag, f"{key}={value}"]
    return json.loads(run(args))["data"]


LEASE_REPO = "obs-rust/.github"


class GhBackend:
    # ---- claims -------------------------------------------------------

    def issue(self, repo: str, number: int) -> Issue:
        raw = rest(f"repos/{repo}/issues/{number}", paginate=False)
        comments = []
        for c in rest(f"repos/{repo}/issues/{number}/comments?per_page=100"):
            com = Comment(
                c["user"]["login"],
                c["author_association"],
                c["body"] or "",
                ts(c["created_at"]),
                c["id"],
            )
            command = any(r.match(com.first_line()) for r in COMMANDS)
            if not is_bot(com.author) and command and c.get("reactions", {}).get(REACTION):
                reactions = rest(
                    f"repos/{repo}/issues/comments/{c['id']}/reactions"
                    f"?content={REACTION}&per_page=100"
                )
                com.granted = any(r["user"]["login"] == BOT for r in reactions)
            comments.append(com)
        prs, assigned_at = [], {}
        for ev in rest(f"repos/{repo}/issues/{number}/timeline?per_page=100"):
            if ev.get("event") == "assigned":
                assigned_at[ev["assignee"]["login"]] = ts(ev["created_at"])
            src = (ev.get("source") or {}).get("issue") or {}
            if (
                ev.get("event") == "cross-referenced"
                and src.get("pull_request")
                and src.get("state") == "open"
                and src.get("repository", {}).get("full_name", repo) == repo
            ):
                prs.append(self._pr(repo, src["number"]))
        assignees = [a["login"] for a in raw["assignees"]]
        deps = raw.get("issue_dependencies_summary") or {}
        return Issue(
            number,
            {lbl["name"] for lbl in raw["labels"]},
            assignees,
            comments,
            prs,
            {a: t for a, t in assigned_at.items() if a in assignees},
            deps.get("blocked_by", 0),
        )

    def _pr(self, repo: str, number: int) -> LinkedPR:
        pr = rest(f"repos/{repo}/pulls/{number}", paginate=False)
        activity = [ts(pr["created_at"])]
        author = pr["user"]["login"].lower()
        for ev in rest(f"repos/{repo}/issues/{number}/timeline?per_page=100"):
            if ev.get("event") == "committed":
                activity.append(ts(ev["committer"]["date"]))
            elif ev.get("event") in (
                "commented",
                "reviewed",
                "ready_for_review",
                "head_ref_force_pushed",
            ):
                stamp = ev.get("created_at") or ev.get("submitted_at")
                actor = (ev.get("user") or ev.get("actor") or {}).get("login", "")
                if stamp and actor.lower() == author:
                    activity.append(ts(stamp))
        return LinkedPR(number, pr["user"]["login"], activity)

    def claimed_issues(self, repo: str) -> list[int]:
        found = set()
        for query in ("assignee=*", f"labels={CLAIMED}"):
            for i in rest(f"repos/{repo}/issues?state=open&{query}&per_page=100"):
                if "pull_request" not in i:
                    found.add(i["number"])
        return sorted(found)

    def apply_issue(self, repo: str, number: int, action: tuple) -> None:
        kind, arg = action
        base = ["gh", "issue", "edit", str(number), "--repo", repo]
        cmd = {
            "label_add": base + ["--add-label", arg],
            "label_remove": base + ["--remove-label", arg],
            "assign": base + ["--add-assignee", arg],
            "unassign": base + ["--remove-assignee", arg],
            "react": [
                "gh",
                "api",
                "-X",
                "POST",
                f"repos/{repo}/issues/comments/{arg}/reactions",
                "-f",
                f"content={REACTION}",
            ],
            "comment": ["gh", "issue", "comment", str(number), "--repo", repo, "--body", arg],
        }[kind]
        run(cmd)

    # ---- leases -------------------------------------------------------

    def create_lease(self, key: str, owner: str, now: datetime) -> bool:
        """Create refs/leases/<key>. GitHub refuses an existing ref (422),
        which is what makes a lease atomic."""
        head = rest(f"repos/{LEASE_REPO}/commits/HEAD", paginate=False)
        commit = json.loads(
            run(
                [
                    "gh",
                    "api",
                    "-X",
                    "POST",
                    f"repos/{LEASE_REPO}/git/commits",
                    "-f",
                    f"message={json.dumps({'owner': owner, 'at': now.isoformat()})}",
                    "-f",
                    f"tree={head['commit']['tree']['sha']}",
                ]
            )
        )
        proc = subprocess.run(
            [
                "gh",
                "api",
                "-X",
                "POST",
                f"repos/{LEASE_REPO}/git/refs",
                "-f",
                f"ref=refs/leases/{key}",
                "-f",
                f"sha={commit['sha']}",
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return True
        if "already exists" in proc.stdout + proc.stderr:
            return False
        raise RuntimeError(f"create lease {key}: {proc.stderr.strip()}")

    def leases(self) -> dict[str, tuple[str, datetime]]:
        proc = subprocess.run(
            ["gh", "api", f"repos/{LEASE_REPO}/git/matching-refs/leases/"],
            capture_output=True,
            text=True,
        )
        refs = json.loads(proc.stdout) if proc.returncode == 0 else []
        out = {}
        for ref in refs:
            commit = rest(f"repos/{LEASE_REPO}/git/commits/{ref['object']['sha']}", paginate=False)
            try:
                meta = json.loads(commit["message"])
                out[ref["ref"].removeprefix("refs/leases/")] = (meta["owner"], ts(meta["at"]))
            except (ValueError, KeyError):
                continue
        return out

    def delete_lease(self, key: str) -> None:
        subprocess.run(
            ["gh", "api", "-X", "DELETE", f"repos/{LEASE_REPO}/git/refs/leases/{key}"],
            capture_output=True,
            text=True,
        )

    # ---- board --------------------------------------------------------

    def items(self, org: str) -> list[Item]:
        repos = self._repos(org)
        with ThreadPoolExecutor(READ_WORKERS) as pool:
            return [
                i for chunk in pool.map(lambda r: self._repo_items(org, r), repos) for i in chunk
            ]

    def _repos(self, org: str) -> list[str]:
        repos, cursor = [], None
        while True:
            d = graphql(
                """query($o: String!, $c: String) { organization(login: $o) {
                  repositories(first: 100, after: $c, isArchived: false) {
                    pageInfo { hasNextPage endCursor } nodes { name } } } }""",
                o=org,
                **({"c": cursor} if cursor else {}),
            )["organization"]["repositories"]
            # Private advisory forks never reach the public board.
            repos += [n["name"] for n in d["nodes"] if "-ghsa-" not in n["name"]]
            if not d["pageInfo"]["hasNextPage"]:
                return repos
            cursor = d["pageInfo"]["endCursor"]

    def _paged(
        self, org: str, repo: str, connection: str, fields: str, volatile: str = ""
    ) -> list[dict]:
        def fetch(args: str, cursor: str | None) -> dict:
            return graphql(
                f"""query($o: String!, $r: String!, $c: String) {{
                  repository(owner: $o, name: $r) {{
                    {connection}(first: 50, after: $c, {args}) {{
                      pageInfo {{ hasNextPage endCursor }} nodes {{ {fields} }} }} }} }}""",
                o=org,
                r=repo,
                **({"c": cursor} if cursor else {}),
            )["repository"][connection]

        nodes, fetched = ghcache.read(org, repo, connection, fields, fetch, volatile)
        log(f"{repo} {connection}: {fetched} fetched, {len(nodes)} cached")
        return nodes

    def _repo_items(self, org: str, repo: str) -> list[Item]:
        common = (
            "id number title state createdAt updatedAt closedAt author { login } "
            "labels(first: 30) { nodes { name } } assignees(first: 5) { nodes { login } }"
        )
        issues = self._paged(
            org,
            repo,
            "issues",
            common
            + """
            issueDependenciesSummary { blockedBy } subIssuesSummary { total }
            closedByPullRequestsReferences(first: 10, includeClosedPrs: false) {
              nodes { isInMergeQueue } }""",
            volatile="states: OPEN",
        )
        prs = self._paged(
            org,
            repo,
            "pullRequests",
            common
            + """
            isDraft isInMergeQueue mergeStateStatus reviews(first: 1) { totalCount }
            commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }""",
            volatile="states: OPEN",
        )
        out = []
        for n in issues:
            out.append(
                self._item(
                    repo,
                    n,
                    "issue",
                    open_blockers=(n["issueDependenciesSummary"] or {}).get("blockedBy", 0),
                    sub_issues=(n["subIssuesSummary"] or {}).get("total", 0),
                    linked_prs_in_queue=[
                        p["isInMergeQueue"] for p in n["closedByPullRequestsReferences"]["nodes"]
                    ],
                )
            )
        for n in prs:
            commits = n["commits"]["nodes"]
            rollup = (commits[0]["commit"]["statusCheckRollup"] or {}) if commits else {}
            out.append(
                self._item(
                    repo,
                    n,
                    "pr",
                    draft=n["isDraft"],
                    in_queue=n["isInMergeQueue"],
                    merge_state=n["mergeStateStatus"],
                    checks=rollup.get("state", ""),
                    reviews=n["reviews"]["totalCount"],
                )
            )
        return out

    @staticmethod
    def _item(repo: str, n: dict, kind: str, **extra: Any) -> Item:
        return Item(
            n["id"],
            repo,
            n["number"],
            kind,
            n["state"],
            ts(n["createdAt"]),
            ts(n["updatedAt"]),
            ts(n["closedAt"]),
            {lbl["name"] for lbl in n["labels"]["nodes"]},
            [a["login"] for a in n["assignees"]["nodes"]],
            author=(n.get("author") or {}).get("login", ""),
            title=n["title"],
            **extra,
        )

    def red_branches(self, org: str) -> list[str]:
        """Repos whose latest finished push run on the default branch, in the
        last RED_DAYS, failed. Older failures are dormant copies of upstream
        repos, not a master anyone is merging into."""

        def red(repo: str) -> bool:
            meta = rest(f"repos/{org}/{repo}", paginate=False)
            runs = rest(
                f"repos/{org}/{repo}/actions/runs?branch={meta['default_branch']}"
                "&event=push&status=completed&per_page=1",
                paginate=False,
            )
            latest = runs.get("workflow_runs") or []
            if not latest or latest[0]["conclusion"] != "failure":
                return False
            age = datetime.now(timezone.utc) - ts(latest[0]["created_at"])
            return age < timedelta(days=RED_DAYS)

        repos = self._repos(org)
        with ThreadPoolExecutor(READ_WORKERS) as pool:
            return [r for r, bad in zip(repos, pool.map(red, repos)) if bad]

    def board(self, org: str, number: int) -> Board:
        item_ids: dict[str, str] = {}
        values: dict[str, dict[str, str]] = {}
        cursor = None
        while True:
            d = graphql(
                """query($o: String!, $n: Int!, $c: String) {
                  organization(login: $o) { projectV2(number: $n) { id
                    fields(first: 50) { nodes {
                      ... on ProjectV2SingleSelectField { id name dataType options { id name } }
                      ... on ProjectV2Field { id name dataType } } }
                    items(first: 100, after: $c) {
                      pageInfo { hasNextPage endCursor }
                      nodes { id
                        content { ... on Issue { id } ... on PullRequest { id } }
                        fieldValues(first: 20) { nodes {
                          ... on ProjectV2ItemFieldSingleSelectValue {
                            name field { ... on ProjectV2FieldCommon { name } } }
                          ... on ProjectV2ItemFieldDateValue {
                            date field { ... on ProjectV2FieldCommon { name } } } } } } } } } }""",
                o=org,
                n=number,
                **({"c": cursor} if cursor else {}),
            )["organization"]["projectV2"]
            for node in d["items"]["nodes"]:
                cid = (node.get("content") or {}).get("id")
                if not cid:
                    continue
                item_ids[cid] = node["id"]
                vals = values.setdefault(cid, {})
                for fv in node["fieldValues"]["nodes"]:
                    name = (fv.get("field") or {}).get("name")
                    value = fv.get("name") or fv.get("date")
                    if name and value:
                        vals[name] = value
            if not d["items"]["pageInfo"]["hasNextPage"]:
                break
            cursor = d["items"]["pageInfo"]["endCursor"]
        fields = {}
        for f in d["fields"]["nodes"]:
            if f.get("name"):
                fields[f["name"]] = Field(
                    f["id"],
                    f["name"],
                    f["dataType"],
                    {o["name"]: o["id"] for o in f.get("options", [])},
                )
        return Board(d["id"], fields, item_ids, values)

    def ensure_fields(self, board: Board, ops: list[tuple]) -> None:
        from workflow.board import STATUS_HELP

        def option(name: str, oid: str | None = None) -> str:
            desc = STATUS_HELP.get(name, "")
            head = f'id: "{oid}", ' if oid else ""
            return f'{{{head}name: "{name}", color: GRAY, description: "{desc}"}}'

        for op in ops:
            if op[0] == "create_field":
                _, name, kind, options = op
                opts = (
                    f", singleSelectOptions: [{', '.join(option(o) for o in options)}]"
                    if options
                    else ""
                )
                graphql(
                    f"""mutation($p: ID!) {{ createProjectV2Field(input: {{
                    projectId: $p, dataType: {kind}, name: "{name}"{opts} }}) {{
                    projectV2Field {{ ... on ProjectV2FieldCommon {{ id }} }} }} }}""",
                    p=board.id,
                )
            else:
                _, name, lacking = op
                field = board.fields[name]
                # Existing options keep their ids, so no item loses its value.
                kept = [option(n, oid) for n, oid in field.options.items()]
                graphql(
                    f"""mutation($f: ID!) {{ updateProjectV2Field(input: {{
                    fieldId: $f, singleSelectOptions: [{", ".join(kept + [option(o) for o in lacking])}]
                    }}) {{ projectV2Field {{ ... on ProjectV2FieldCommon {{ id }} }} }} }}""",
                    f=field.id,
                )

    def apply_board(self, board: Board, ops: list[tuple]) -> dict[str, int]:
        per_item: dict[str, list[tuple]] = {}
        for op in ops:
            per_item.setdefault(op[1], []).append(op)
        counts = {"added": 0, "changed": 0, "failed": 0}

        def one(cid: str, item_ops: list[tuple]) -> tuple[int, int]:
            added = changed = 0
            item_id = board.item_ids.get(cid)
            for op in item_ops:
                if op[0] == "add":
                    item_id = graphql(
                        """mutation($p: ID!, $c: ID!) { addProjectV2ItemById(
                          input: {projectId: $p, contentId: $c}) { item { id } } }""",
                        p=board.id,
                        c=cid,
                    )["addProjectV2ItemById"]["item"]["id"]
                    added = 1
                    continue
                field = board.fields[op[2]]
                if op[0] == "clear":
                    graphql(
                        """mutation($p: ID!, $i: ID!, $f: ID!) {
                      clearProjectV2ItemFieldValue(input: {projectId: $p, itemId: $i,
                        fieldId: $f}) { projectV2Item { id } } }""",
                        p=board.id,
                        i=item_id,
                        f=field.id,
                    )
                elif field.type == "DATE":
                    graphql(
                        """mutation($p: ID!, $i: ID!, $f: ID!, $d: Date!) {
                      updateProjectV2ItemFieldValue(input: {projectId: $p, itemId: $i,
                        fieldId: $f, value: {date: $d}}) { projectV2Item { id } } }""",
                        p=board.id,
                        i=item_id,
                        f=field.id,
                        d=op[3],
                    )
                else:
                    graphql(
                        """mutation($p: ID!, $i: ID!, $f: ID!, $o: String!) {
                      updateProjectV2ItemFieldValue(input: {projectId: $p, itemId: $i,
                        fieldId: $f, value: {singleSelectOptionId: $o}}) {
                        projectV2Item { id } } }""",
                        p=board.id,
                        i=item_id,
                        f=field.id,
                        o=field.options[op[3]],
                    )
                changed = 1
            return added, changed

        with ThreadPoolExecutor(WRITE_WORKERS) as pool:
            futures = [pool.submit(one, cid, item_ops) for cid, item_ops in per_item.items()]
            for future in as_completed(futures):
                try:
                    added, changed = future.result()
                    counts["added"] += added
                    counts["changed"] += changed and not added
                except RuntimeError:
                    counts["failed"] += 1
        return counts
