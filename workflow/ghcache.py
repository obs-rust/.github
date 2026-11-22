"""Incremental GraphQL cache, keyed on the repo.

GraphQL has no ETags, so a full read of every issue and PR each run burns the
rate limit (2,300+ nodes per sync). Instead each repo connection is cached on
disk with a high-water mark: a run asks for nodes ordered by `updatedAt`
descending and stops at the first one older than the mark, then merges by id.

Some fields change without bumping `updatedAt` (a PR's check rollup, merge
state, queue membership), so the caller names a `volatile` filter whose nodes
are refetched in full every run (open PRs: a small set).

The key is `<host>/<owner>/<repo>/<connection>-<hash of the field selection>`
under `$GH_REPO_CACHE` (default `~/.cache/gh-repo-cache`), so every tool and
checkout that reads the same repo URL shares one cache, and two queries with
different fields never collide.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Callable

# fetch(connection_args, cursor) -> {"nodes": [...], "pageInfo": {...}}
Fetch = Callable[[str, str | None], dict]

HOST = "github.com"


def root() -> Path:
    return Path(os.environ.get("GH_REPO_CACHE", Path.home() / ".cache" / "gh-repo-cache"))


def path(owner: str, repo: str, connection: str, fields: str) -> Path:
    digest = hashlib.sha256(" ".join(fields.split()).encode()).hexdigest()[:12]
    return root() / HOST / owner.lower() / repo.lower() / f"{connection}-{digest}.json"


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {"mark": "", "nodes": {}}


def _save(p: Path, data: dict) -> None:
    # Atomic replace: parallel tools sharing the cache never read half a file.
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, p)


def _pages(fetch: Fetch, args: str, stop: Callable[[dict], bool] = lambda n: False):
    cursor = None
    while True:
        d = fetch(args, cursor)
        for n in d["nodes"]:
            if stop(n):
                return
            yield n
        if not d["pageInfo"]["hasNextPage"]:
            return
        cursor = d["pageInfo"]["endCursor"]


def read(
    owner: str, repo: str, connection: str, fields: str, fetch: Fetch, volatile: str = ""
) -> tuple[list[dict], int]:
    """All nodes of the connection, and how many were fetched this run.
    Nodes need `id` and `updatedAt` in `fields`."""
    p = path(owner, repo, connection, fields)
    data = _load(p)
    mark, nodes = data["mark"], data["nodes"]
    fetched = 0
    order = "orderBy: {field: UPDATED_AT, direction: DESC}"
    for n in _pages(fetch, order, stop=lambda n: bool(mark) and n["updatedAt"] < mark):
        nodes[n["id"]] = n
        fetched += 1
    if volatile and mark:
        for n in _pages(fetch, volatile):
            nodes[n["id"]] = n
            fetched += 1
    if nodes:
        data = {"mark": max(n["updatedAt"] for n in nodes.values()), "nodes": nodes}
        _save(p, data)
    return list(nodes.values()), fetched
