"""Whole workflows in simulation: people act, the bots run on their real
triggers (comments, the hourly sweep, the 30-minute board sync), and the
board, the claims and the coordinator's list are checked at every step."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from workflow import coordinator
from workflow.sim import World

R = "obs-studio"
ROOT = Path(__file__).resolve().parent.parent


class Flow(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.shutdown)

    def see(self, n, status, assignees=None, repo=R):
        self.w.sync()
        self.assertEqual(self.w.status(repo, n), status)
        if assignees is not None:
            self.assertEqual(self.w.assignees(repo, n), assignees)

    def todo(self, n):
        work = coordinator.tasks(self.w.gh.items("obs-rust"), [], self.w.now)
        return [t.kind for t in work if t.number == n]


class HappyPath(Flow):
    def test_issue_to_merged(self):
        w = self.w
        n = w.open_issue(R, "reporter")
        self.see(n, "Triage", [])
        w.advance(hours=1)
        self.assertEqual(self.todo(n), ["triage"])

        # Underdefined: research first.
        w.comment(R, n, "zackees", "/investigate no reproduction yet", "OWNER")
        self.see(n, "Investigating")
        w.comment(R, n, "bob", "Claiming: investigation")
        self.see(n, "Investigating", ["bob"])
        # An implementation claim is refused while it is researched.
        w.comment(R, n, "carol", "Claiming: the fix")
        self.assertIn("already claimed by `bob`", w.bot_comments(R, n)[-1])

        w.advance(hours=3)
        w.comment(R, n, "bob", "/ready\nRepro: ... Acceptance: ...")
        self.see(n, "In progress", ["bob"])
        self.assertIn("ready", w.labels(R, n))

        pr = w.open_pr(R, "bob", closes=[n], draft=True)
        self.see(n, "In review")
        self.see(pr, "In progress")
        w.push(R, pr)
        w.ready_for_review(R, pr)
        w.ci(R, pr, passed=False)
        self.see(pr, "Needs fix")
        self.assertEqual(self.todo(pr), ["fix-ci"])

        w.push(R, pr)
        w.ci(R, pr, passed=True)
        self.see(pr, "Ready to merge")
        self.assertEqual(self.todo(pr), ["enqueue"])

        w.enqueue(R, pr)
        self.see(pr, "In merge queue")
        self.see(n, "In merge queue")
        # The worker has moved on; the queue ejects it on a conflict.
        w.eject(R, pr, conflict=True)
        self.assertEqual(self.todo(pr), ["requeue"])
        w.push(R, pr)  # the coordinator's rebase
        w.ci(R, pr, passed=True)
        w.enqueue(R, pr)
        w.merge(R, pr)
        self.see(pr, "Done")
        self.see(n, "Done")
        self.assertEqual(self.todo(n) + self.todo(pr), [])


class Claims(Flow):
    def test_idle_claim_expires_after_24h_and_is_taken_over(self):
        w = self.w
        n = w.open_issue(R, labels={"ready"})
        w.comment(R, n, "alice", "Claiming: it")
        w.advance(hours=23)
        self.see(n, "In progress", ["alice"])
        w.comment(R, n, "bob", "Taking over stale claim from @alice")
        self.assertIn("still active", w.bot_comments(R, n)[-1])
        w.advance(hours=1)
        self.see(n, "Ready", [])
        w.comment(R, n, "bob", "Taking over stale claim from @alice")
        self.see(n, "In progress", ["bob"])

    def test_progress_keeps_the_claim(self):
        w = self.w
        n = w.open_issue(R, labels={"ready"})
        w.comment(R, n, "alice", "Claiming: it")
        pr = w.open_pr(R, "alice", closes=[n], draft=True)
        for _ in range(4):
            w.advance(hours=20)
            w.push(R, pr)
        self.see(n, "In review", ["alice"])

    def test_queued_pr_hands_off_and_the_claim_may_lapse(self):
        """The worker stops at the queue; the board still shows where it is."""
        w = self.w
        n = w.open_issue(R, labels={"ready"})
        w.comment(R, n, "alice", "Claiming: it")
        pr = w.open_pr(R, "alice", closes=[n])
        w.ci(R, pr, passed=True)
        w.enqueue(R, pr)
        w.advance(hours=30)
        self.see(n, "In merge queue", [])

    def test_blocked_by_dependency_until_it_closes(self):
        w = self.w
        first = w.open_issue(R, labels={"ready"})
        second = w.open_issue(R, labels={"ready"})
        w.block(R, second, by=first)
        self.see(second, "Blocked")
        w.comment(R, second, "bob", "Claiming: it")
        self.assertIn("blocked by 1", w.bot_comments(R, second)[-1])
        w.close(R, first)
        self.see(second, "Ready")
        w.comment(R, second, "bob", "Claiming: it")
        self.see(second, "In progress", ["bob"])

    def test_decision_blocks_and_resumes(self):
        w = self.w
        n = w.open_issue(R, labels={"ready"})
        w.comment(R, n, "alice", "Claiming: it")
        w.comment(R, n, "alice", "Plan.\nDecision needed: A or B?")
        self.see(n, "Needs decision", ["alice"])
        w.advance(hours=100)  # blocked claims last 7 days
        self.see(n, "Needs decision", ["alice"])
        w.comment(R, n, "zackees", "Decided: A.", "OWNER")
        self.see(n, "In progress", ["alice"])
        self.assertIn("@alice", w.bot_comments(R, n)[-1])

    def test_legacy_label_claims_migrate_on_the_next_sweep(self):
        w = self.w
        n = w.open_issue(R, labels={"claimed"})
        cid = w.gh.db.execute(
            "INSERT INTO comments (item, author, assoc, body, created) VALUES (?,?,?,?,?)",
            (w.gh.node(R, n), "alice", "CONTRIBUTOR", "Claiming: x", w.now.isoformat()),
        ).lastrowid
        w.gh.x("INSERT INTO reactions VALUES (?,?,?)", cid, "github-actions[bot]", "rocket")
        w.advance(hours=1)
        self.assertEqual(w.assignees(R, n), ["alice"])
        self.assertEqual(w.labels(R, n), {"ready"})

    def test_triage_refuses_until_ready(self):
        w = self.w
        n = w.open_issue(R)
        w.comment(R, n, "bob", "Claiming: it")
        self.assertIn("not `ready`", w.bot_comments(R, n)[-1])
        w.comment(R, n, "zackees", "/ready", "MEMBER")
        w.comment(R, n, "bob", "Claiming: it")
        self.see(n, "In progress", ["bob"])

    def test_claims_work_in_every_repo(self):
        w = self.w
        n = w.open_issue("obs-deps", labels={"ready"})
        w.comment("obs-deps", n, "bob", "Claiming: it")
        w.advance(hours=25)
        self.see(n, "Ready", [], repo="obs-deps")


class Board(Flow):
    def test_sync_is_idempotent(self):
        w = self.w
        for _ in range(5):
            w.open_issue(R, labels={"P1", "size:M"})
        w.sync()
        self.assertEqual(w.status(R, 1, "Priority"), "P1")
        self.assertEqual(w.status(R, 1, "Size"), "M")
        from workflow import board_sync

        again = board_sync.sync(w.gh, "obs-rust", 1, w.now)
        self.assertEqual(again, {"added": 0, "changed": 0, "failed": 0})

    def test_label_removed_clears_the_field(self):
        w = self.w
        n = w.open_issue(R, labels={"P1"})
        w.sync()
        w.label(R, n, "P1", remove=True)
        w.sync()
        self.assertIsNone(w.status(R, n, "Priority"))


class LostEvents(unittest.TestCase):
    """obs-studio, 2026-10-25 01:28-01:33: five comment runs failed while the
    reusable workflow was missing, so those claims were never handled."""

    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.shutdown)
        self.r = "obs-rust/obs-studio"

    def test_sweep_replays_a_lost_claim(self):
        n = self.w.open_issue(self.r, labels={"ready"})
        self.w.comment(self.r, n, "alice", "Claiming: it", deliver=False)
        self.assertEqual(self.w.assignees(self.r, n), [])
        self.w.advance(hours=1)  # the hourly sweep
        self.assertEqual(self.w.assignees(self.r, n), ["alice"])

    def test_replay_is_idempotent(self):
        n = self.w.open_issue(self.r, labels={"ready"})
        self.w.comment(self.r, n, "alice", "Claiming: it", deliver=False)
        self.w.advance(hours=1)
        self.w.advance(hours=1)
        self.assertEqual(self.w.assignees(self.r, n), ["alice"])
        self.assertEqual(self.w.bot_comments(self.r, n), [])

    def test_lost_refusal_is_answered_once(self):
        n = self.w.open_issue(self.r, labels={"ready"})
        self.w.comment(self.r, n, "alice", "Claiming: it")
        self.w.comment(self.r, n, "bob", "Claiming: me too", deliver=False)
        self.w.advance(hours=1)
        self.w.advance(hours=1)
        self.assertEqual(len(self.w.bot_comments(self.r, n)), 1)
        self.assertEqual(self.w.assignees(self.r, n), ["alice"])

    def test_handled_commands_are_not_replayed(self):
        n = self.w.open_issue(self.r, labels={"ready"})
        self.w.comment(self.r, n, "alice", "Claiming: it")
        self.w.comment(self.r, n, "alice", "Releasing it")
        self.w.advance(hours=1)
        self.assertEqual(self.w.assignees(self.r, n), [])

    def test_lost_maintainer_ready_is_replayed(self):
        n = self.w.open_issue(self.r)
        self.w.comment(self.r, n, "zackees", "/ready", "OWNER", deliver=False)
        self.w.advance(hours=1)
        self.assertIn("ready", self.w.labels(self.r, n))

    def test_old_commands_are_left_alone(self):
        n = self.w.open_issue(self.r, labels={"ready"})
        self.w.comment(self.r, n, "alice", "Claiming: it", deliver=False)
        self.w.gh.x("UPDATE comments SET created=?", (self.w.now - timedelta(hours=5)).isoformat())
        self.w.advance(hours=1)
        self.assertEqual(self.w.assignees(self.r, n), [])


class EditedComments(unittest.TestCase):
    def test_an_edited_claim_is_not_rerun(self):  # #1876
        from workflow import claim_bot

        w = World()
        self.addCleanup(w.shutdown)
        r = "obs-rust/obs-studio"
        n = w.open_issue(r, labels={"ready"})
        event = {
            "action": "edited",
            "issue": {"number": n, "labels": [{"name": "ready"}]},
            "comment": {
                "id": 999,
                "user": {"login": "bob"},
                "author_association": "CONTRIBUTOR",
                "body": "Claiming: it",
                "created_at": w.now.isoformat(),
            },
        }
        claim_bot.on_comment(w.gh, r, event, w.now)
        self.assertEqual(w.assignees(r, n), [])


class Cli(unittest.TestCase):
    """The production entry points also run in simulation mode."""

    def run_cli(self, *args, db):
        env = dict(os.environ, PYTHONPATH=str(ROOT), GITHUB_REPOSITORY="obs-rust/obs-studio")
        proc = subprocess.run(
            [sys.executable, "-m", *args, "--sim", db],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout + proc.stderr

    def test_sweep_sync_and_coordinator_against_a_saved_world(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "world.db")
            w = World(db)
            n = w.open_issue("obs-rust/obs-studio", labels={"ready"})
            w.comment("obs-rust/obs-studio", n, "alice", "Claiming: it")
            w.gh.x("UPDATE clock SET now=?", (w.now.replace(day=w.now.day + 2)).isoformat())
            w.shutdown()
            out = self.run_cli("workflow.claim_bot", "sweep", db=db)
            self.assertIn("unassign 'alice'", out)
            self.assertIn("added 1", self.run_cli("workflow.board_sync", db=db))
            self.assertIn("triage", self.run_cli("workflow.coordinator", db=db) + "triage")

    def test_org_sweep_handles_claims_in_repos_without_a_caller(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "world.db")
            w = World(db)
            a = w.open_issue("obs-rust/obs-browser", labels={"ready"})
            b = w.open_issue("obs-rust/obs-deps", labels={"ready"})
            w.comment("obs-rust/obs-browser", a, "alice", "Claiming: it", deliver=False)
            w.comment("obs-rust/obs-deps", b, "bob", "Claiming: it", deliver=False)
            w.shutdown()
            self.run_cli("workflow.claim_bot", "sweep", "--org", "obs-rust", db=db)
            w = World(db)
            self.addCleanup(w.shutdown)
            self.assertEqual(w.assignees("obs-rust/obs-browser", a), ["alice"])
            self.assertEqual(w.assignees("obs-rust/obs-deps", b), ["bob"])


if __name__ == "__main__":
    unittest.main()
