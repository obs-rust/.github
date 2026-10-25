"""Data shared by the claim bot, the board sync and their backends.

Both bots are pure functions over these records. A backend (``gh.py`` for
production, ``sim.py`` for the SQLite simulation) loads them and applies the
actions the bots return, so the same decisions run in both modes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Comment:
    author: str
    association: str
    body: str
    created: datetime
    id: int = 0
    granted: bool = False  # a command the claim bot reacted to

    def first_line(self) -> str:
        return self.body.strip().splitlines()[0] if self.body.strip() else ""


@dataclass
class LinkedPR:
    """An open PR that references an issue, with its author's activity."""

    number: int
    author: str
    activity: list[datetime] = field(default_factory=list)


@dataclass
class Issue:
    """What the claim bot needs to know about one issue."""

    number: int
    labels: set[str]
    assignees: list[str]
    comments: list[Comment]
    prs: list[LinkedPR] = field(default_factory=list)
    # When each current assignee was assigned.
    assigned_at: dict[str, datetime] = field(default_factory=dict)
    open_blockers: int = 0


@dataclass
class Item:
    """An issue or PR as the board sync sees it."""

    id: str  # GraphQL node id
    repo: str
    number: int
    kind: str  # "issue" or "pr"
    state: str  # OPEN, CLOSED or MERGED
    created: datetime
    updated: datetime
    closed: datetime | None = None
    labels: set[str] = field(default_factory=set)
    assignees: list[str] = field(default_factory=list)
    draft: bool = False
    in_queue: bool = False
    merge_state: str = ""  # PR mergeStateStatus: CLEAN, DIRTY, BLOCKED, ...
    checks: str = ""  # PR check rollup: SUCCESS, FAILURE, ERROR, PENDING
    open_blockers: int = 0
    # Open PRs that will close this issue: is each in the merge queue?
    linked_prs_in_queue: list[bool] = field(default_factory=list)
    author: str = ""
    reviews: int = 0  # PR reviews submitted
    title: str = ""
    sub_issues: int = 0  # a tracker issue has sub-issues


@dataclass
class Field:
    id: str
    name: str
    type: str  # SINGLE_SELECT or DATE
    options: dict[str, str] = field(default_factory=dict)  # name -> option id


@dataclass
class Board:
    """The project: its fields and, per content id, the item and its values."""

    id: str
    fields: dict[str, Field]
    item_ids: dict[str, str]  # content id -> project item id
    values: dict[str, dict[str, str]]  # content id -> field name -> value
