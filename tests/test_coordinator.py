"""The coordinator's work list (workflow/coordinator.py), in simulation.

Each case builds a situation in the SQLite world, checks that the right task
appears, does what the task says, and checks that the task is gone. If the
coordinator's list is wrong, work piles up unseen, so every rule is here.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta

from workflow import coordinator
from workflow.sim import World

R = "obs-studio"


class Case(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.shutdown)

    def work(self):
        return coordinator.tasks(
            self.w.gh.items("obs-rust"), self.w.gh.red_branches("obs-rust"), self.w.now
        )

    def kinds(self, number=None):
        return [t.kind for t in self.work() if number is None or t.number == number]

    def green_pr(self, closes=(), author="alice"):
        n = self.w.open_pr(R, author, closes=closes)
        self.w.review(R, n)
        self.w.ci(R, n, passed=True)
        return n


class PullRequestTasks(Case):
    def test_green_pr_is_enqueued_then_leaves_the_list(self):
        n = self.green_pr()
        self.assertEqual(self.kinds(n), ["enqueue"])
        self.w.enqueue(R, n)
        self.assertEqual(self.kinds(n), [])

    def test_ejected_pr_is_requeued(self):
        n = self.green_pr()
        self.w.enqueue(R, n)
        self.w.eject(R, n, conflict=True)
        self.assertEqual(self.kinds(n), ["requeue"])
        self.w.push(R, n)  # the rebase
        self.w.ci(R, n, passed=True)
        self.assertEqual(self.kinds(n), ["enqueue"])

    def test_failing_ci_is_fix_ci(self):
        n = self.w.open_pr(R, "alice")
        self.w.ci(R, n, passed=False)
        self.assertEqual(self.kinds(n), ["fix-ci"])
        self.w.push(R, n)
        self.w.ci(R, n, passed=True)
        self.assertNotIn("fix-ci", self.kinds(n))

    def test_conflict_before_ci_passed_is_a_rebase(self):
        n = self.w.open_pr(R, "alice")
        self.w.gh.x("UPDATE items SET merge_state='DIRTY' WHERE id=?", self.w.gh.node(R, n))
        self.assertEqual(self.kinds(n), ["rebase"])

    def test_behind_is_a_rebase(self):
        n = self.w.open_pr(R, "alice")
        self.w.gh.x(
            "UPDATE items SET merge_state='BEHIND', checks='SUCCESS' WHERE id=?",
            self.w.gh.node(R, n),
        )
        self.assertEqual(self.kinds(n), ["rebase"])

    def test_unreviewed_pr_needs_review(self):
        n = self.w.open_pr(R, "alice")
        self.assertEqual(self.kinds(n), ["review"])
        self.w.review(R, n)
        self.assertEqual(self.kinds(n), [])

    def test_green_pr_is_enqueued_even_without_a_review(self):
        """A green PR has passed the merge gate; queuing beats waiting for review."""
        n = self.w.open_pr(R, "alice")
        self.w.ci(R, n, passed=True)
        self.assertEqual(self.kinds(n), ["enqueue"])

    def test_draft_is_left_to_its_author_until_it_stalls(self):
        n = self.w.open_pr(R, "alice", draft=True)
        self.assertEqual(self.kinds(n), [])
        self.w.advance(hours=72)
        self.assertEqual(self.kinds(n), ["stalled"])

    def test_reviewed_pr_with_pending_ci_stalls_after_72h(self):
        n = self.w.open_pr(R, "alice")
        self.w.review(R, n)
        self.w.advance(hours=71)
        self.assertEqual(self.kinds(n), [])
        self.w.advance(hours=1)
        self.assertEqual(self.kinds(n), ["stalled"])
        self.w.push(R, n)
        self.assertEqual(self.kinds(n), [])

    def test_queued_pr_needs_nothing(self):
        n = self.green_pr()
        self.w.enqueue(R, n)
        self.w.advance(hours=100)
        self.assertEqual(self.kinds(n), [])

    def test_pr_with_a_decision_is_left_to_the_decision(self):
        n = self.green_pr()
        self.w.gh.x("INSERT INTO labels VALUES (?, 'decision-needed')", self.w.gh.node(R, n))
        self.assertEqual(self.kinds(n), [])

    def test_merged_pr_leaves_the_list(self):
        n = self.green_pr()
        self.w.merge(R, n)
        self.assertEqual(self.kinds(n), [])


class NoMergeQueueRepos(Case):
    """obs-deps and friends have no merge queue: `gh pr merge --auto` fails there."""

    def test_green_reviewed_pr_is_merged_not_enqueued(self):
        self.w.no_merge_queue(R)
        n = self.green_pr()
        self.assertEqual(self.kinds(n), ["merge"])

    def test_green_unreviewed_pr_is_reviewed_first(self):
        self.w.no_merge_queue(R)
        n = self.w.open_pr(R, "alice")
        self.w.ci(R, n, passed=True)
        self.assertEqual(self.kinds(n), ["review"])


class FlowTestFindings(Case):
    """Gaps a coordinator hit on its first live tick."""

    def test_legacy_claimed_label_is_not_triaged(self):
        # A repo without the claim bot never migrates its `claimed` labels.
        n = self.w.open_issue(R, labels={"claimed"})
        item = next(i for i in self.w.gh.items("obs-rust") if i.number == n)
        item.created -= timedelta(hours=2)
        self.assertIsNone(coordinator.issue_task(item, self.w.now))
        item.labels.add("needs-investigation")
        self.assertIsNone(coordinator.issue_task(item, self.w.now))

    def test_tracker_label_without_sub_issues_leaves_triage(self):
        n = self.w.open_issue(R, labels={"tracker"})
        self.w.advance(hours=2)
        self.assertEqual(self.kinds(n), [])
        self.w.sync()
        self.assertEqual(self.w.status(R, n), "Tracker")

    def test_tracker_titles_leave_triage(self):
        for title in ("meta: Phase 6 core", "Phase 3 remaining ports: utf8", "Tracker: CI"):
            n = self.w.open_issue(R)
            self.w.gh.x("UPDATE items SET title=? WHERE id=?", title, self.w.gh.node(R, n))
            self.w.advance(hours=2)
            self.assertEqual(self.kinds(n), [], title)
        n = self.w.open_issue(R)
        self.w.gh.x(
            "UPDATE items SET title='metadata loss in mp4' WHERE id=?", self.w.gh.node(R, n)
        )
        self.w.advance(hours=2)
        self.assertEqual(self.kinds(n), ["triage"])

    def test_report_shows_labels(self):
        n = self.w.open_issue(R, labels={"docs"})
        self.w.advance(hours=2)
        self.assertIn(f"#{n} (2 h) ", coordinator.report(self.work()))
        self.assertIn("[docs]", coordinator.report(self.work()))

    def test_json_output_is_only_json(self):
        import contextlib
        import io
        import json
        import sys
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            db = f"{d}/w.db"
            w = World(db)
            w.open_issue(R)
            w.advance(hours=2)
            w.shutdown()
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["c", "--json", "--sim", db]):
                with contextlib.redirect_stdout(out):
                    coordinator.main()
            self.assertEqual(json.loads(out.getvalue())[0]["kind"], "triage")


class IssueTasks(Case):
    def test_new_issue_is_triaged_after_an_hour(self):
        n = self.w.open_issue(R)
        self.assertEqual(self.kinds(n), [])
        self.w.advance(hours=1)
        self.assertEqual(self.kinds(n), ["triage"])
        self.w.comment(R, n, "zackees", "/ready", "OWNER")
        self.assertEqual(self.kinds(n), [])

    def test_triage_can_send_it_to_investigation(self):
        n = self.w.open_issue(R)
        self.w.advance(hours=2)
        self.w.comment(R, n, "zackees", "/investigate repro missing", "OWNER")
        self.assertEqual(self.kinds(n), ["investigating"])
        self.w.comment(R, n, "bob", "Claiming: investigation")
        self.assertEqual(self.kinds(n), [])

    def test_expired_investigation_returns_to_the_list(self):
        n = self.w.open_issue(R)
        self.w.advance(hours=2)
        self.w.comment(R, n, "bob", "Claiming: investigation")
        self.assertEqual(self.kinds(n), [])
        self.w.advance(hours=25)
        self.assertEqual(self.w.assignees(R, n), [])
        self.assertEqual(self.kinds(n), ["investigating"])

    def test_decision_is_decided_then_leaves(self):
        n = self.w.open_issue(R, labels={"ready"})
        self.w.comment(R, n, "bob", "Decision needed: A or B?")
        self.assertEqual(self.kinds(n), ["decide"])
        self.w.comment(R, n, "zackees", "Decided: A.", "OWNER")
        self.assertEqual(self.kinds(n), [])

    def test_escalated_decision_is_reported_until_answered(self):
        n = self.w.open_issue(R, labels={"ready", "human-review-needed"})
        self.assertEqual(self.kinds(n), ["escalated"])
        self.w.comment(R, n, "zackees", "Decision: B.", "OWNER")
        self.assertEqual(self.kinds(n), [])

    def test_ready_claimed_blocked_and_reviewed_issues_need_nothing(self):
        ready = self.w.open_issue(R, labels={"ready"})
        claimed = self.w.open_issue(R, labels={"ready"})
        self.w.comment(R, claimed, "alice", "Claiming: it")
        blocker = self.w.open_issue(R, labels={"ready"})
        blocked = self.w.open_issue(R, labels={"ready"})
        self.w.block(R, blocked, by=blocker)
        p0 = self.w.open_issue(R, labels={"P0"})
        self.w.advance(hours=3)
        for n in (ready, claimed, blocked, p0):
            self.assertEqual(self.kinds(n), [], n)

    def test_tracker_is_not_triaged(self):
        parent = self.w.open_issue(R)
        child = self.w.open_issue(R, labels={"ready"})
        self.w.sub_issue(R, parent, child)
        self.w.advance(hours=2)
        self.assertEqual(self.kinds(parent), [])

    def test_report_caps_each_kind(self):
        for _ in range(20):
            self.w.open_issue(R)
        self.w.advance(hours=2)
        text = coordinator.report(self.work(), per_kind=15)
        self.assertIn("## triage (20)", text)
        self.assertIn("5 more", text)

    def test_closed_issue_leaves_the_list(self):
        n = self.w.open_issue(R)
        self.w.advance(hours=2)
        self.w.close(R, n)
        self.assertEqual(self.kinds(n), [])


class OrderAndReport(Case):
    def test_red_master_comes_first(self):
        self.green_pr()
        self.w.master(R, red=True)
        self.assertEqual(self.kinds()[0], "fix-master")
        self.w.master(R, red=False)
        self.assertNotIn("fix-master", self.kinds())

    def test_full_order(self):
        self.w.master(R, red=True)
        self.w.open_issue(R, labels={"human-review-needed"})
        inv = self.w.open_issue(R, labels={"needs-investigation"})
        stalled = self.w.open_pr(R, "dave", draft=True)
        self.w.advance(hours=73)
        tri = self.w.open_issue(R)
        dec = self.w.open_issue(R, labels={"decision-needed"})
        rev = self.w.open_pr(R, "erin")
        reb = self.w.open_pr(R, "frank")
        self.w.gh.x("UPDATE items SET merge_state='BEHIND' WHERE id=?", self.w.gh.node(R, reb))
        enq = self.w.open_pr(R, "gina")
        self.w.ci(R, enq, passed=True)
        fix = self.w.open_pr(R, "hal")
        self.w.ci(R, fix, passed=False)
        req = self.green_pr(author="ivy")
        self.w.enqueue(R, req)
        self.w.eject(R, req)
        self.w.no_merge_queue("obs-rust/obs-deps")
        mrg = self.w.open_pr("obs-rust/obs-deps", "jo")
        self.w.review("obs-rust/obs-deps", mrg)
        self.w.ci("obs-rust/obs-deps", mrg, passed=True)
        self.w.advance(hours=1)
        self.assertEqual(self.kinds(), coordinator.ORDER)
        del inv, stalled, tri, dec, rev

    def test_oldest_first_within_a_kind(self):
        a = self.w.open_issue(R)
        self.w.advance(hours=5)
        b = self.w.open_issue(R)
        self.w.advance(hours=2)
        self.assertEqual([t.number for t in self.work()], [a, b])

    def test_report_names_each_kind_with_its_instructions(self):
        self.green_pr()
        text = coordinator.report(self.work())
        self.assertIn("## enqueue (1)", text)
        self.assertIn("gh pr merge", text)
        self.assertEqual(coordinator.report([]), "quiet: nothing to do")

    def test_every_kind_has_instructions(self):
        self.assertEqual(set(coordinator.ORDER), set(coordinator.HOW))


if __name__ == "__main__":
    unittest.main()
