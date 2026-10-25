"""Board rules (workflow/board.py): Status, Priority, Size, dates, the plan."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from workflow import board
from workflow.model import Board, Field, Item

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def item(kind="issue", state="OPEN", labels=(), updated_h=1, **kw) -> Item:
    return Item(
        "I1",
        "obs-studio",
        1,
        kind,
        state,
        NOW - timedelta(days=3),
        NOW - timedelta(hours=updated_h),
        labels=set(labels),
        **kw,
    )


def st(**kw) -> str:
    return board.status(item(**kw), NOW)


class IssueStatusTest(unittest.TestCase):
    def test_triage_by_default(self):
        self.assertEqual(st(), "Triage")

    def test_ready(self):
        self.assertEqual(st(labels={"ready"}), "Ready")
        self.assertEqual(st(labels={"P0"}), "Ready")
        self.assertEqual(st(labels={"critical"}), "Ready")

    def test_investigating_even_when_assigned(self):
        self.assertEqual(st(labels={"needs-investigation"}), "Investigating")
        self.assertEqual(st(labels={"needs-investigation"}, assignees=["a"]), "Investigating")

    def test_tracker_with_sub_issues(self):
        self.assertEqual(st(sub_issues=3), "Tracker")
        self.assertEqual(st(sub_issues=3, assignees=["a"]), "In progress")

    def test_assigned_is_in_progress(self):
        self.assertEqual(st(labels={"ready"}, assignees=["a"]), "In progress")

    def test_open_pr_puts_it_in_review_and_queue(self):
        self.assertEqual(st(assignees=["a"], linked_prs_in_queue=[False]), "In review")
        self.assertEqual(st(assignees=["a"], linked_prs_in_queue=[False, True]), "In merge queue")

    def test_blocked_by_dependency_or_claimant(self):
        self.assertEqual(st(labels={"ready"}, open_blockers=1), "Blocked")
        self.assertEqual(st(labels={"claim-blocked"}, assignees=["a"]), "Blocked")

    def test_decision_wins(self):
        for label in board.DECISION_LABELS:
            self.assertEqual(st(labels={label, "ready"}, open_blockers=1), "Needs decision")

    def test_closed_is_done(self):
        self.assertEqual(st(state="CLOSED", labels={"decision-needed"}), "Done")


class PrStatusTest(unittest.TestCase):
    def pr(self, **kw):
        return st(kind="pr", **kw)

    def test_draft_is_in_progress(self):
        self.assertEqual(self.pr(draft=True), "In progress")

    def test_review_ready_queue(self):
        self.assertEqual(self.pr(merge_state="BLOCKED", checks="PENDING"), "In review")
        self.assertEqual(self.pr(merge_state="CLEAN", checks="SUCCESS"), "Ready to merge")
        self.assertEqual(self.pr(in_queue=True, checks="FAILURE"), "In merge queue")

    def test_needs_fix(self):
        self.assertEqual(self.pr(checks="FAILURE"), "Needs fix")
        self.assertEqual(self.pr(checks="ERROR"), "Needs fix")
        self.assertEqual(self.pr(merge_state="DIRTY", checks="SUCCESS"), "Needs fix")

    def test_stalled_after_72h(self):
        self.assertEqual(self.pr(updated_h=71), "In review")
        self.assertEqual(self.pr(updated_h=72), "Stalled")
        self.assertEqual(self.pr(updated_h=100, draft=True), "Stalled")
        self.assertEqual(self.pr(updated_h=100, in_queue=True), "In merge queue")

    def test_merged_and_closed_are_done(self):
        self.assertEqual(self.pr(state="MERGED"), "Done")
        self.assertEqual(self.pr(state="CLOSED"), "Done")


class FieldsTest(unittest.TestCase):
    def test_priority_and_size_from_labels(self):
        self.assertEqual(board.priority(item(labels={"P2"})), "P2")
        self.assertEqual(board.priority(item(labels={"critical", "P3"})), "P0")
        self.assertIsNone(board.priority(item()))
        self.assertEqual(board.size(item(labels={"size:XL"})), "XL")
        self.assertIsNone(board.size(item(labels={"XL"})))

    def test_every_status_has_help_text(self):
        self.assertEqual(set(board.STATUSES), set(board.STATUS_HELP))


class PlanTest(unittest.TestCase):
    def empty(self, values=None, ids=None) -> Board:
        return Board("P", {}, ids or {}, values or {})

    def test_new_item_is_added_with_every_field(self):
        ops = board.plan([item(labels={"P1"})], self.empty(), NOW)
        self.assertEqual(ops[0], ("add", "I1"))
        self.assertIn(("set", "I1", "Status", "Triage"), ops)
        self.assertIn(("set", "I1", "Priority", "P1"), ops)
        self.assertNotIn("Size", [o[2] for o in ops[1:]])

    def test_in_sync_item_needs_nothing(self):
        it = item()
        values = {k: v for k, v in board.desired(it, NOW).items() if v}
        self.assertEqual(board.plan([it], self.empty({"I1": values}, {"I1": "x"}), NOW), [])

    def test_removed_label_clears_the_field(self):
        it = item()
        values = {k: v for k, v in board.desired(it, NOW).items() if v} | {"Priority": "P1"}
        self.assertEqual(
            board.plan([it], self.empty({"I1": values}, {"I1": "x"}), NOW),
            [("clear", "I1", "Priority")],
        )

    def test_missing_fields_and_options(self):
        status = Field("F", "Status", "SINGLE_SELECT", {"Triage": "o1", "Done": "o2"})
        ops = board.missing_fields(Board("P", {"Status": status}, {}, {}))
        add = [o for o in ops if o[0] == "add_options"][0]
        self.assertEqual(add[1], "Status")
        self.assertNotIn("Triage", add[2])
        self.assertIn("Investigating", add[2])
        self.assertIn(("create_field", "Size", "SINGLE_SELECT", board.SIZES), ops)


if __name__ == "__main__":
    unittest.main()
