"""PR claim validation through the same backend as issue-comment events."""

import unittest
from unittest.mock import Mock

from workflow import claim_bot, coordinator
from workflow.sim import World

R = "obs-studio"


class PullRequestClaims(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.shutdown)
        self.w.gh.pr_files = Mock(return_value=["rust/obs-util/src/lib.rs"])

    def event(self, body, author="alice", action="opened", labels=()):
        pr = self.w.open_pr(R, author)
        return {
            "action": action,
            "pull_request": {
                "number": pr,
                "body": body,
                "user": {"login": author},
                "labels": [{"name": label} for label in labels],
                "state": "open",
            },
        }

    def check(self, event):
        return claim_bot.on_pull_request(self.w.gh, R, event, self.w.now)

    def test_closed_duplicate_is_flagged_even_with_same_account_assignment(self):
        issue = self.w.open_issue(R, labels={"ready"})
        self.w.comment(R, issue, "alice", "Claiming: original")
        self.w.close(R, issue)
        event = self.event(f"Fixes #{issue}")
        self.check(event)
        pr = event["pull_request"]["number"]
        self.assertIn("claim-conflict", self.w.labels(R, pr))
        self.assertIn("closed", self.w.bot_comments(R, pr)[0])

    def test_ready_assignee_without_claim_comment_is_flagged(self):
        issue = self.w.open_issue(R, labels={"ready"})
        self.w.gh.apply_issue(R, issue, ("assign", "alice"))
        event = self.event(f"Closes #{issue}")
        self.check(event)
        self.assertIn("claim-conflict", self.w.labels(R, event["pull_request"]["number"]))

    def test_claimed_issue_without_ready_is_flagged(self):
        issue = self.w.open_issue(R, labels={"ready"})
        self.w.comment(R, issue, "alice", "Claiming: fix")
        self.w.label(R, issue, "ready", remove=True)
        event = self.event(f"Resolves #{issue}")
        self.check(event)
        self.assertIn("not ready", self.w.bot_comments(R, event["pull_request"]["number"])[0])

    def test_other_authors_claim_is_flagged(self):
        issue = self.w.open_issue(R, labels={"ready"})
        self.w.comment(R, issue, "bob", "Claiming: fix")
        event = self.event(f"Fixes #{issue}")
        self.check(event)
        self.assertIn("bob", self.w.bot_comments(R, event["pull_request"]["number"])[0])

    def test_valid_claim_passes_and_refs_do_not_require_a_claim(self):
        issue = self.w.open_issue(R, labels={"ready"})
        self.w.comment(R, issue, "alice", "Claiming: fix")
        event = self.event(f"Fixes #{issue}; Refs #999")
        self.check(event)
        self.assertNotIn("claim-conflict", self.w.labels(R, event["pull_request"]["number"]))

    def test_comment_is_idempotent_and_corrected_body_clears_label(self):
        issue = self.w.open_issue(R)
        event = self.event(f"Fixes #{issue}")
        self.check(event)
        event["action"] = "synchronize"
        self.check(event)
        pr = event["pull_request"]["number"]
        self.assertEqual(len(self.w.bot_comments(R, pr)), 1)
        event["action"] = "edited"
        event["pull_request"]["body"] = f"Refs #{issue}"
        self.check(event)
        self.assertNotIn("claim-conflict", self.w.labels(R, pr))

    def test_only_docs_and_ci_paths_are_exempt(self):
        for files in (["docs/guide.md"], ["ci/check.py", ".github/workflows/existing.yml"]):
            with self.subTest(files=files):
                self.w.gh.pr_files.return_value = files
                issue = self.w.open_issue(R)
                event = self.event(f"Fixes #{issue}")
                self.check(event)
                self.assertNotIn(
                    "claim-conflict", self.w.labels(R, event["pull_request"]["number"])
                )

    def test_coordinator_does_not_enqueue_a_flagged_green_pr(self):
        event = self.event("Fixes #1")
        pr = event["pull_request"]["number"]
        self.w.label(R, pr, "claim-conflict")
        self.w.ci(R, pr, True)
        item = next(i for i in self.w.gh.items("obs-rust") if i.number == pr)
        self.assertEqual(coordinator.pr_task(item, self.w.now), "claim-conflict")

    def test_unavailable_reference_is_a_conflict_not_an_exception(self):
        event = self.event("Fixes #999999")
        self.assertFalse(self.check(event))
        pr = event["pull_request"]["number"]
        self.assertIn("could not be read", self.w.bot_comments(R, pr)[0])

    def test_accepted_stale_takeover_is_a_claim(self):
        issue = self.w.open_issue(R, labels={"ready"})
        self.w.comment(R, issue, "bob", "Claiming: original")
        self.w.advance(hours=25)
        self.w.comment(R, issue, "alice", "Taking over stale claim from @bob")
        event = self.event(f"Fixes #{issue}")
        self.assertTrue(self.check(event))

    def test_cross_repo_reference_and_fenced_example(self):
        self.assertEqual(
            __import__("workflow.pr_claims", fromlist=["references"]).references(
                R, "Fixes obs-rust/.github#26\n```\nFixes #999\n```\nRefs #100"
            ),
            [("obs-rust/.github", 26)],
        )

    def test_rejected_comment_does_not_become_accepted_on_later_assignment(self):
        issue = self.w.open_issue(R, labels={"ready"})
        self.w.comment(R, issue, "bob", "Claiming: original")
        self.w.comment(R, issue, "alice", "Claiming: parallel")
        self.w.advance(hours=1)
        self.w.gh.apply_issue(R, issue, ("unassign", "bob"))
        self.w.gh.apply_issue(R, issue, ("assign", "alice"))
        self.assertFalse(self.check(self.event(f"Fixes #{issue}")))
