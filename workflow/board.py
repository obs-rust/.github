"""Board rules: the fields each issue and PR should have on the org project.

Pure functions. ``board_sync.py`` loads items and the project from a backend,
calls ``plan`` and applies the changes it returns.

Status, for an open issue (first match wins):
  Needs decision  label decision-needed or human-review-needed
  Blocked         an open "blocked by" dependency, or label claim-blocked
  In merge queue  a PR that will close it is in the merge queue
  In review       an open PR will close it
  Investigating   label needs-investigation
  In progress     assigned (claimed)
  Tracker         has sub-issues (the work is in them)
  Ready           label ready, or P0/critical
  Triage          anything else

for an open PR:
  Needs decision  label decision-needed or human-review-needed
  In merge queue  waiting in the merge queue
  Needs fix       checks fail, or it conflicts (DIRTY)
  Stalled         no activity for STALLED_HOURS
  In progress     draft
  Ready to merge  mergeStateStatus CLEAN
  In review       anything else

and Done for a closed issue or a merged or closed PR.

Priority comes from labels P0..P3 (`critical` counts as P0), Size from
size:S..size:XL. Dates for progress charts are GitHub's built-in Created,
Updated and Closed fields, so the sync writes none.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from workflow.model import Board, Item

STATUSES = [
    "Triage",
    "Tracker",
    "Investigating",
    "Ready",
    "Needs decision",
    "Blocked",
    "In progress",
    "Needs fix",
    "Stalled",
    "In review",
    "Ready to merge",
    "In merge queue",
    "Done",
]
STATUS_HELP = {
    "Triage": "New; not yet checked against the definition of Ready",
    "Tracker": "Tracks sub-issues; the work is in them",
    "Investigating": "Underdefined; research is needed before it can be Ready",
    "Ready": "Meets the definition of Ready; claimable",
    "Needs decision": "decision-needed or human-review-needed",
    "Blocked": "Waits on another issue, or the claimant marked it blocked",
    "In progress": "Claimed issue or draft PR",
    "Needs fix": "PR with failing checks or conflicts",
    "Stalled": "PR with no activity for 72 hours",
    "In review": "Open PR waiting on review or checks",
    "Ready to merge": "Green, mergeable PR",
    "In merge queue": "PR waiting in the merge queue",
    "Done": "Closed issue, merged or closed PR",
}
PRIORITIES = ["P0", "P1", "P2", "P3"]
SIZES = ["S", "M", "L", "XL"]
DECISION_LABELS = {"decision-needed", "human-review-needed"}
STALLED_HOURS = 72

# Field name -> (type, options).
FIELDS: dict[str, tuple[str, list[str]]] = {
    "Status": ("SINGLE_SELECT", STATUSES),
    "Priority": ("SINGLE_SELECT", PRIORITIES),
    "Size": ("SINGLE_SELECT", SIZES),
}


def status(item: Item, now: datetime) -> str:
    if item.state != "OPEN":
        return "Done"
    labels = item.labels
    if labels & DECISION_LABELS:
        return "Needs decision"
    if item.kind == "issue":
        if item.open_blockers or "claim-blocked" in labels:
            return "Blocked"
        if any(item.linked_prs_in_queue):
            return "In merge queue"
        if item.linked_prs_in_queue:
            return "In review"
        if "needs-investigation" in labels:
            return "Investigating"
        if item.assignees:
            return "In progress"
        if item.sub_issues:
            return "Tracker"
        if "ready" in labels or {"P0", "critical"} & labels:
            return "Ready"
        return "Triage"
    if item.in_queue:
        return "In merge queue"
    if item.checks in ("FAILURE", "ERROR") or item.merge_state == "DIRTY":
        return "Needs fix"
    if now - item.updated >= timedelta(hours=STALLED_HOURS):
        return "Stalled"
    if item.draft:
        return "In progress"
    if item.merge_state == "CLEAN":
        return "Ready to merge"
    return "In review"


def priority(item: Item) -> str | None:
    if "critical" in item.labels:
        return "P0"
    return next((p for p in PRIORITIES if p in item.labels), None)


def size(item: Item) -> str | None:
    return next((s for s in SIZES if f"size:{s}" in item.labels), None)


def desired(item: Item, now: datetime) -> dict[str, str | None]:
    """Each field's value for this item; None means empty."""
    return {
        "Status": status(item, now),
        "Priority": priority(item),
        "Size": size(item),
    }


def plan(items: list[Item], board: Board, now: datetime) -> list[tuple]:
    """Changes that bring the board in line with the items:

    ("add", content_id)                  add the item to the project
    ("set", content_id, field, value)    set a field (option name or date)
    ("clear", content_id, field)         empty a field
    """
    ops: list[tuple] = []
    for item in items:
        current = board.values.get(item.id, {})
        if item.id not in board.item_ids:
            ops.append(("add", item.id))
        for name, value in desired(item, now).items():
            if current.get(name) == value:
                continue
            if value is None:
                ops.append(("clear", item.id, name))
            else:
                ops.append(("set", item.id, name, value))
    return ops


def missing_fields(board: Board) -> list[tuple]:
    """Fields and options the project lacks:
    ("create_field", name, type, options) or ("add_options", name, [options])."""
    ops: list[tuple] = []
    for name, (kind, options) in FIELDS.items():
        field = board.fields.get(name)
        if field is None:
            ops.append(("create_field", name, kind, options))
        elif lacking := [o for o in options if o not in field.options]:
            ops.append(("add_options", name, lacking))
    return ops
