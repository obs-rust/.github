"""The three claim-bot bugs of obs-rust/.github#31."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_claims import applied, h, human, issue
from workflow import claim_bot, gh, pr_claims
from workflow import claims as cb


def comment_event(c, labels=()):
    return {
        "action": "created",
        "issue": {"number": 1, "labels": [{"name": n} for n in labels]},
        "comment": {
            "user": {"login": c.author},
            "author_association": c.association,
            "body": c.body,
            "created_at": c.created,
            "id": c.id,
        },
    }


class NoOpActions(unittest.TestCase):
    """Item 1: an action the issue already reflects never reaches GitHub."""

    def test_decided_without_the_label_sends_no_label_remove_and_reacts(self):
        # obs-rust/obs-studio#3810: `Decided:` on an issue that never had
        # `decision-needed` made `label_remove` 404 and failed the run.
        i = issue(labels=[cb.READY])
        c = human("maintainer", "Decided: option 1", h(1), assoc="MEMBER")
        backend = Mock()
        backend.issue.return_value = i
        claim_bot.on_comment(backend, "obs-rust/obs-studio", comment_event(c), h(1))
        sent = [call.args[2] for call in backend.apply_issue.call_args_list]
        self.assertNotIn(("label_remove", cb.DECISION), sent)
        self.assertIn(("react", c.id), sent)

    def test_sweep_sends_no_action_the_issue_already_reflects(self):
        i = issue(labels=[cb.READY])
        backend = Mock()
        backend.claimed_issues.return_value = [1]
        backend.issue.return_value = i
        backend.issues_commented_since.return_value = []
        with patch.object(
            claim_bot.claims,
            "decide_sweep",
            return_value=[("label_remove", cb.BLOCKED), ("unassign", "nobody")],
        ):
            claim_bot.sweep(backend, "obs-rust/obs-studio", h(1))
        backend.apply_issue.assert_not_called()


class NotFoundIsDone(unittest.TestCase):
    """Item 2: removing a label or an assignee that is not there is done."""

    def run_with_404(self, action):
        failed = SimpleNamespace(
            returncode=1, stdout="", stderr="gh: Label does not exist (HTTP 404)"
        )
        with patch.object(gh.subprocess, "run", return_value=failed) as run:
            gh.GhBackend.apply_issue(Mock(), "obs-rust/obs-studio", 1, action)
        return run

    def test_label_remove_404_is_done_after_one_call(self):
        self.assertEqual(self.run_with_404(("label_remove", cb.DECISION)).call_count, 1)

    def test_unassign_404_is_done_after_one_call(self):
        self.assertEqual(self.run_with_404(("unassign", "alice")).call_count, 1)

    def test_other_404_still_fails(self):
        with self.assertRaises(gh.GhError):
            self.run_with_404(("label_add", cb.READY))


class AssigneeClaim(unittest.TestCase):
    """Item 3: a `Claiming:` by the assignee, who has no granted claim yet."""

    def test_claim_by_the_hand_assigned_author_is_granted(self):
        # obs-rust/obs-studio#3872: the author commented `Claiming:` and then
        # assigned themselves before the bot's run started.
        i = issue(assignees=["alice"])
        c = human("alice", "Claiming: the getters", h(1))
        acts = cb.decide_comment(i, c, h(1))
        self.assertIn(("react", c.id), acts)

    def test_pr_by_that_assignee_has_no_conflict(self):
        i = issue(assignees=["alice"])
        c = human("alice", "Claiming: the getters", h(1))
        i = applied(i, c, cb.decide_comment(i, c, h(1)))
        backend = Mock()
        backend.issue.return_value = i
        backend.pr_files.return_value = ["libobs/obs-source.c"]
        pr = {"number": 2, "body": "Fixes #1", "user": {"login": "alice"}}
        self.assertEqual(pr_claims.conflicts(backend, "obs-rust/obs-studio", pr), [])

    def test_repeated_claim_by_a_granted_holder_stays_silent(self):
        i = issue([human("alice", "Claiming: x", h(0), granted=True)], assignees=["alice"])
        self.assertEqual(cb.decide_comment(i, human("alice", "Claiming: x", h(1)), h(1)), [])


if __name__ == "__main__":
    unittest.main()
