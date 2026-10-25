"""Claim rules (workflow/claims.py), ported from obs-studio's
ci/test_claim_bot.py (#909, #1808, #2057, #2212) to claims by assignment.

    uv run -m unittest discover -s tests
"""

from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta, timezone

from workflow import claims as cb
from workflow.model import Comment, Issue, LinkedPR

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
_ids = iter(range(1000, 10**6))


def h(n: float) -> datetime:
    return T0 + timedelta(hours=n)


def human(author, body, at, assoc="CONTRIBUTOR", granted=False):
    return Comment(author, assoc, body, at, id=next(_ids), granted=granted)


def bot(body, at):
    return Comment(cb.BOT, "NONE", f"{cb.BOT_PREFIX} {body}", at)


def issue(comments=(), labels=(cb.READY,), prs=(), assignees=(), blockers=0):
    i = Issue(1, set(labels), list(assignees), list(comments), list(prs))
    i.assigned_at = {a: T0 for a in assignees}
    i.open_blockers = blockers
    return i


def kinds(actions):
    return [a[0] for a in actions]


def applied(i, c, acts):
    """The issue after the bot's actions, as GitHub would record them."""
    for kind, arg in acts:
        if kind == "label_add":
            i.labels.add(arg)
        elif kind == "label_remove":
            i.labels.discard(arg)
        elif kind == "assign":
            i.assignees.append(arg)
            i.assigned_at[arg] = c.created
        elif kind == "unassign":
            i.assignees.remove(arg)
            i.assigned_at.pop(arg, None)
        elif kind == "react":
            assert arg == c.id
            c.granted = True
    i.comments.append(c)
    return i


def claimed_by_alice(labels=(cb.READY,), prs=()):
    return issue([human("alice", "Claiming: x", h(0), granted=True)], labels, prs, ["alice"])


def accepted(acts, c, who=None):
    who = who or c.author
    return ("assign", who) in acts and acts[-1] == ("react", c.id) and "comment" not in kinds(acts)


def rejected(acts, who):
    return kinds(acts) == ["comment"] and f"@{who}," in acts[0][1]


class ClaimTest(unittest.TestCase):
    def test_claim_on_a_ready_issue_assigns_and_reacts(self):
        c = human("vpacher", "Claiming: audio-io\n1. plan", h(0))
        i = issue()
        acts = cb.decide_comment(i, c, h(0))
        self.assertEqual(acts, [("assign", "vpacher"), ("react", c.id)])
        self.assertEqual(cb.current_claim(applied(i, c, acts)).user, "vpacher")

    def test_one_account_may_hold_any_number_of_claims(self):
        c = human("zackees", "Claiming: x", h(0), "MEMBER")
        self.assertTrue(accepted(cb.decide_comment(issue(), c, h(0)), c))

    def test_claim_is_case_insensitive_and_first_line_only(self):
        self.assertTrue(cb.decide_comment(issue(), human("a", "claiming: x", h(0)), h(0)))
        self.assertEqual(
            cb.decide_comment(issue(), human("a", "plan\nClaiming: x", h(0)), h(0)), []
        )

    def test_fresh_claim_by_someone_else_is_rejected_with_a_mention(self):
        acts = cb.decide_comment(claimed_by_alice(), human("bob", "Claiming: x", h(1)), h(1))
        self.assertTrue(rejected(acts, "bob"), acts)
        self.assertIn("already claimed by `alice`", acts[0][1])

    def test_rejected_claim_comment_does_not_become_the_claim(self):
        i = claimed_by_alice()
        c = human("bob", "Claiming: x", h(1))
        i = applied(i, c, cb.decide_comment(i, c, h(1)))
        self.assertEqual(cb.current_claim(i).user, "alice")

    def test_reclaim_by_holder_is_a_no_op(self):
        self.assertEqual(
            cb.decide_comment(claimed_by_alice(), human("alice", "Claiming: x", h(1)), h(1)), []
        )

    def test_claim_on_a_stale_claim_moves_it(self):
        i = claimed_by_alice()
        c = human("bob", "Claiming: x", h(cb.STALE_HOURS + 1))
        acts = cb.decide_comment(i, c, c.created)
        self.assertEqual(acts, [("unassign", "alice"), ("assign", "bob"), ("react", c.id)])
        self.assertEqual(cb.current_claim(applied(i, c, acts)).user, "bob")

    def test_locked_issue_is_ignored(self):
        self.assertEqual(
            cb.decide_comment(
                issue(labels=[cb.LOCKED, cb.READY]), human("a", "Claiming: x", h(0)), h(0)
            ),
            [],
        )

    def test_bot_comments_are_ignored(self):
        self.assertEqual(
            cb.decide_comment(issue(), Comment("x[bot]", "NONE", "Claiming: x", h(0)), h(0)), []
        )

    def test_a_manual_assignment_is_a_claim(self):
        """A maintainer assigning someone in the UI claims it for them."""
        i = issue(assignees=["carol"])
        self.assertEqual(cb.current_claim(i).user, "carol")
        self.assertTrue(
            rejected(cb.decide_comment(i, human("bob", "Claiming: x", h(1)), h(1)), "bob")
        )


class ReadyTest(unittest.TestCase):
    """Only Ready issues can be claimed for implementation."""

    def test_triage_issue_refuses_an_implementation_claim(self):
        acts = cb.decide_comment(issue(labels=()), human("bob", "Claiming: x", h(0)), h(0))
        self.assertTrue(rejected(acts, "bob"), acts)
        self.assertIn("not `ready`", acts[0][1])
        self.assertIn("Claiming: investigation", acts[0][1])

    def test_needs_investigation_refuses_an_implementation_claim(self):
        acts = cb.decide_comment(
            issue(labels=(cb.INVESTIGATE,)), human("bob", "Claiming: x", h(0)), h(0)
        )
        self.assertTrue(rejected(acts, "bob"), acts)
        self.assertIn("needs investigation", acts[0][1])

    def test_investigation_claim_on_a_triage_issue_labels_and_assigns(self):
        c = human("bob", "Claiming: investigation of the crash", h(0))
        acts = cb.decide_comment(issue(labels=()), c, h(0))
        self.assertEqual(acts, [("label_add", cb.INVESTIGATE), ("assign", "bob"), ("react", c.id)])

    def test_investigation_claim_variants(self):
        for line in (
            "Claiming investigation",
            "claiming: the investigation",
            "Claiming: investigating",
        ):
            self.assertTrue(cb.INVESTIGATION_CLAIM_RE.match(line), line)
        self.assertFalse(cb.INVESTIGATION_CLAIM_RE.match("Claiming: the fix"))

    def test_p0_and_critical_never_wait_for_ready(self):
        for label in (cb.P0, cb.CRITICAL):
            c = human("bob", "Claiming: fix master", h(0))
            self.assertTrue(accepted(cb.decide_comment(issue(labels=(label,)), c, h(0)), c), label)

    def test_investigator_marks_it_ready_and_keeps_the_claim(self):
        i = issue(labels=(cb.INVESTIGATE,), assignees=["bob"])
        c = human("bob", "/ready\nRepro: ... Acceptance: ...", h(3))
        acts = cb.decide_comment(i, c, h(3))
        self.assertEqual(
            acts, [("label_add", cb.READY), ("label_remove", cb.INVESTIGATE), ("react", c.id)]
        )
        self.assertEqual(cb.current_claim(applied(i, c, acts)).user, "bob")

    def test_maintainer_marks_an_unclaimed_issue_ready(self):
        c = human("zackees", "/ready", h(1), "OWNER")
        self.assertEqual(
            cb.decide_comment(issue(labels=()), c, h(1)), [("label_add", cb.READY), ("react", c.id)]
        )

    def test_others_cannot_mark_ready(self):
        acts = cb.decide_comment(issue(labels=()), human("bob", "/ready", h(1)), h(1))
        self.assertTrue(rejected(acts, "bob"), acts)

    def test_investigate_sends_it_back(self):
        c = human("zackees", "/investigate the repro is missing", h(1), "MEMBER")
        acts = cb.decide_comment(issue(), c, h(1))
        self.assertEqual(
            acts, [("label_add", cb.INVESTIGATE), ("label_remove", cb.READY), ("react", c.id)]
        )

    def test_open_decision_refuses_a_claim(self):
        acts = cb.decide_comment(
            issue(labels=(cb.READY, cb.DECISION)), human("bob", "Claiming: x", h(0)), h(0)
        )
        self.assertTrue(rejected(acts, "bob"), acts)
        self.assertIn("decision", acts[0][1])

    def test_open_blocker_refuses_a_claim(self):
        acts = cb.decide_comment(issue(blockers=2), human("bob", "Claiming: x", h(0)), h(0))
        self.assertTrue(rejected(acts, "bob"), acts)
        self.assertIn("blocked by 2", acts[0][1])

    def test_blocker_closing_makes_it_claimable(self):
        c = human("bob", "Claiming: x", h(0))
        self.assertTrue(accepted(cb.decide_comment(issue(blockers=0), c, h(0)), c))

    def test_ready_and_investigate_are_commands(self):
        for line in ("/ready", "/investigate", "Claiming: investigation"):
            self.assertTrue(any(r.match(line) for r in cb.COMMANDS), line)


class StateTest(unittest.TestCase):
    def test_no_assignee_and_no_label_means_no_claim(self):
        i = claimed_by_alice()
        i.assignees = []
        self.assertIsNone(cb.current_claim(i))

    def test_legacy_label_claim_is_still_read(self):
        i = issue(
            [human("alice", "Claiming: x", h(0)), bot("claimed by @alice. ...", h(0))], [cb.CLAIMED]
        )
        self.assertEqual(cb.current_claim(i).user, "alice")

    def test_pre_bot_claim_is_read_from_the_comment(self):
        i = issue([human("nathan-v", "Claiming: x", h(0))], [cb.CLAIMED])
        self.assertEqual(cb.current_claim(i).user, "nathan-v")


class MigrationTest(unittest.TestCase):
    """The sweep turns `claimed` labels into assignments once."""

    def test_fresh_label_claim_becomes_an_assignment(self):
        i = issue([human("alice", "Claiming: x", h(0), granted=True)], [cb.CLAIMED])
        self.assertEqual(
            cb.decide_sweep(i, h(2)),
            [("assign", "alice"), ("label_remove", cb.CLAIMED), ("label_add", cb.READY)],
        )

    def test_stale_label_claim_is_just_dropped(self):
        i = issue([human("alice", "Claiming: x", h(0), granted=True)], [cb.CLAIMED, cb.READY])
        self.assertEqual(cb.decide_sweep(i, h(cb.STALE_HOURS)), [("label_remove", cb.CLAIMED)])

    def test_label_without_a_claimant_is_removed(self):
        i = issue([human("alice", "Releasing x", h(0), granted=True)], [cb.CLAIMED])
        self.assertEqual(cb.decide_sweep(i, h(1)), [("label_remove", cb.CLAIMED)])

    def test_label_claim_under_investigation_keeps_its_label(self):
        i = issue([human("alice", "Claiming: x", h(0), granted=True)], [cb.CLAIMED, cb.INVESTIGATE])
        self.assertEqual(
            cb.decide_sweep(i, h(2)), [("assign", "alice"), ("label_remove", cb.CLAIMED)]
        )

    def test_commands_on_a_label_claim_finish_the_migration(self):
        i = issue([human("alice", "Claiming: x", h(0), granted=True)], [cb.CLAIMED, cb.READY])
        c = human("alice", "Releasing x", h(1))
        self.assertEqual(
            cb.decide_comment(i, c, h(1)), [("label_remove", cb.CLAIMED), ("react", c.id)]
        )


class ReleaseTest(unittest.TestCase):
    def test_holder_releases_silently(self):
        i = claimed_by_alice()
        c = human("alice", "Releasing x, branch foo", h(2))
        acts = cb.decide_comment(i, c, h(2))
        self.assertEqual(acts, [("unassign", "alice"), ("react", c.id)])
        self.assertIsNone(cb.current_claim(applied(i, c, acts)))

    def test_others_cannot_release(self):
        self.assertEqual(
            cb.decide_comment(claimed_by_alice(), human("bob", "Releasing x", h(2)), h(2)), []
        )

    def test_claim_after_release_is_granted(self):
        i = claimed_by_alice()
        r = human("alice", "Releasing x", h(2))
        i = applied(i, r, cb.decide_comment(i, r, h(2)))
        c = human("bob", "Claiming: x", h(3))
        acts = cb.decide_comment(i, c, h(3))
        self.assertTrue(accepted(acts, c), acts)
        self.assertEqual(cb.current_claim(applied(i, c, acts)).user, "bob")


class HandoffTest(unittest.TestCase):
    def hands(self, body):
        i = claimed_by_alice()
        c = human("alice", body, h(2))
        return i, c, cb.decide_comment(i, c, h(2))

    def assert_moves_to_bob(self, body):
        i, c, acts = self.hands(body)
        self.assertEqual(acts, [("unassign", "alice"), ("assign", "bob"), ("react", c.id)], body)
        self.assertEqual(cb.current_claim(applied(i, c, acts)).user, "bob")

    def test_release_naming_a_handoff_target_moves_the_claim(self):
        self.assert_moves_to_bob("Releasing x — handing it to @bob, who has the bench driver")

    def test_bare_handoff_line_moves_the_claim(self):
        self.assert_moves_to_bob("Handing x over to @bob")

    def test_handoff_command_moves_the_claim(self):
        self.assert_moves_to_bob("/handoff @bob")

    def test_informal_yours_moves_the_claim(self):
        self.assert_moves_to_bob("It's yours, @bob 🫡")

    def test_gratitude_is_not_a_handoff(self):
        i, c, acts = self.hands("Releasing x — thanks to @bob for the review")
        self.assertEqual(acts, [("unassign", "alice"), ("react", c.id)], acts)

    def test_handoff_to_self_still_releases(self):
        i, c, acts = self.hands("Releasing x — handing it to @alice")
        self.assertEqual(acts, [("unassign", "alice"), ("react", c.id)], acts)

    def test_handoff_without_a_target_still_releases(self):
        i, c, acts = self.hands("Releasing x, handing it over to nobody in particular")
        self.assertEqual(acts, [("unassign", "alice"), ("react", c.id)], acts)

    def test_only_the_holder_can_hand_off(self):
        i = claimed_by_alice()
        self.assertEqual(
            cb.decide_comment(i, human("mallory", "Handing x over to @mallory", h(2)), h(2)), []
        )

    def test_handoff_starts_the_new_holders_clock(self):
        i, c, acts = self.hands("Handing x over to @bob")
        i = applied(i, c, acts)
        claim = cb.current_claim(i)
        self.assertFalse(cb.is_stale(i, claim, h(2 + cb.STALE_HOURS - 0.1)))
        self.assertTrue(cb.is_stale(i, claim, h(2 + cb.STALE_HOURS)))

    def test_handoff_blocks_another_claimant(self):
        i, c, acts = self.hands("Handing x over to @bob")
        i = applied(i, c, acts)
        acts = cb.decide_comment(i, human("carol", "Claiming: x", h(3)), h(3))
        self.assertTrue(rejected(acts, "carol"), acts)
        self.assertIn("already claimed by `bob`", acts[0][1])

    def test_handoff_lines_are_recognized_commands(self):
        for line in (
            "Handing x over to @bob",
            "/handoff @bob",
            "It's yours, @bob",
            "Releasing x — handing it to @bob",
        ):
            self.assertTrue(any(r.match(line) for r in cb.COMMANDS), line)


class TakeoverTest(unittest.TestCase):
    def test_takeover_of_fresh_claim_is_rejected(self):
        acts = cb.decide_comment(
            claimed_by_alice(), human("bob", "Taking over stale claim from @alice", h(10)), h(10)
        )
        self.assertTrue(rejected(acts, "bob"), acts)
        self.assertIn("still active", acts[0][1])

    def test_takeover_of_stale_claim_moves_it_silently(self):
        i = claimed_by_alice()
        c = human("bob", "Taking over stale claim from @alice", h(cb.STALE_HOURS))
        acts = cb.decide_comment(i, c, c.created)
        self.assertTrue(accepted(acts, c), acts)
        self.assertEqual(cb.current_claim(applied(i, c, acts)).user, "bob")

    def test_takeover_naming_the_wrong_holder_is_rejected(self):
        acts = cb.decide_comment(
            claimed_by_alice(), human("bob", "Taking over stale claim from @carol", h(48)), h(48)
        )
        self.assertTrue(rejected(acts, "bob"), acts)

    def test_takeover_after_expiry_is_a_claim(self):
        c = human("bob", "Taking over stale claim from @alice", h(30))
        self.assertTrue(accepted(cb.decide_comment(issue(), c, h(30)), c))

    def test_p0_claim_with_idle_draft_pr_is_stealable_after_8h(self):
        pr = LinkedPR(5, "alice", [h(1)])
        i = claimed_by_alice(labels=(cb.READY, cb.P0), prs=[pr])
        take = human("bob", "Taking over stale claim from @alice", h(8.5))
        self.assertTrue(accepted(cb.decide_comment(i, take, h(9)), take))
        self.assertTrue(rejected(cb.decide_comment(i, take, h(8.5)), "bob"))

    def test_critical_still_counts_as_p0(self):
        i = claimed_by_alice(labels=(cb.CRITICAL,))
        self.assertEqual(cb.stale_hours(i), cb.P0_STALE_HOURS)

    def test_pushes_to_the_pr_keep_a_p0_claim(self):
        pr = LinkedPR(5, "alice", [h(1), h(7)])
        i = claimed_by_alice(labels=(cb.READY, cb.P0), prs=[pr])
        take = human("bob", "Taking over stale claim from @alice", h(10))
        self.assertTrue(rejected(cb.decide_comment(i, take, h(10)), "bob"))

    def test_someone_elses_pr_does_not_count_as_progress(self):
        i = claimed_by_alice(prs=[LinkedPR(5, "mallory", [h(20)])])
        self.assertTrue(cb.is_stale(i, cb.current_claim(i), h(cb.STALE_HOURS)))


class ReassignTest(unittest.TestCase):
    def test_maintainer_reassigns_a_fresh_claim_silently(self):
        i = claimed_by_alice()
        c = human("zackees", "/reassign @bob", h(1), "MEMBER")
        acts = cb.decide_comment(i, c, h(1))
        self.assertEqual(acts, [("unassign", "alice"), ("assign", "bob"), ("react", c.id)])
        self.assertEqual(cb.current_claim(applied(i, c, acts)).user, "bob")

    def test_contributor_cannot_reassign(self):
        acts = cb.decide_comment(claimed_by_alice(), human("bob", "/reassign @bob", h(1)), h(1))
        self.assertTrue(rejected(acts, "bob"), acts)


class SweepTest(unittest.TestCase):
    def test_stale_hours_is_24(self):
        self.assertEqual(cb.STALE_HOURS, 24)

    def test_fresh_claim_is_left_alone(self):
        self.assertEqual(cb.decide_sweep(claimed_by_alice(), h(23.9)), [])

    def test_expires_after_24h_silently(self):
        self.assertEqual(cb.decide_sweep(claimed_by_alice(), h(24)), [("unassign", "alice")])

    def test_claimant_comment_resets_the_clock(self):
        i = claimed_by_alice()
        i.comments.append(human("alice", "progress: RED test pushed", h(20)))
        self.assertEqual(cb.decide_sweep(i, h(30)), [])

    def test_pr_activity_resets_the_clock(self):
        i = claimed_by_alice(prs=[LinkedPR(5, "alice", [h(20)])])
        self.assertEqual(cb.decide_sweep(i, h(30)), [])

    def test_p0_expires_after_8h(self):
        self.assertEqual(cb.decide_sweep(claimed_by_alice((cb.P0,)), h(8)), [("unassign", "alice")])

    def test_locked_is_never_expired(self):
        self.assertEqual(cb.decide_sweep(claimed_by_alice((cb.READY, cb.LOCKED)), h(100)), [])

    def test_pre_bot_label_claim_expires(self):
        i = issue([human("nathan-v", "Claiming: x", h(0))], [cb.CLAIMED])
        self.assertEqual(cb.decide_sweep(i, h(48)), [("label_remove", cb.CLAIMED)])


class BlockedTest(unittest.TestCase):
    def blocked_by_alice(self, at=h(1), extra=()):
        i = claimed_by_alice()
        c = human("alice", "Blocked: waiting on maintainer", at)
        acts = cb.decide_comment(i, c, at)
        self.assertEqual(acts, [("label_add", cb.BLOCKED), ("react", c.id)])
        i = applied(i, c, acts)
        for x in extra:
            i.comments.append(x)
        return i

    def test_claimant_marks_the_claim_blocked_silently(self):
        self.blocked_by_alice()

    def test_slash_blocked_works_too(self):
        i = claimed_by_alice()
        c = human("alice", "/blocked waiting on #12", h(1))
        self.assertEqual(
            cb.decide_comment(i, c, h(1)), [("label_add", cb.BLOCKED), ("react", c.id)]
        )

    def test_someone_else_cannot_mark_it_blocked(self):
        acts = cb.decide_comment(claimed_by_alice(), human("bob", "Blocked: reasons", h(1)), h(1))
        self.assertTrue(rejected(acts, "bob"), acts)
        self.assertIn("only the claimant `alice`", acts[0][1])

    def test_blocked_without_a_claim_is_refused(self):
        self.assertTrue(
            rejected(cb.decide_comment(issue(), human("bob", "Blocked: x", h(1)), h(1)), "bob")
        )

    def test_blocked_claim_outlives_24h(self):
        self.assertEqual(cb.decide_sweep(self.blocked_by_alice(), h(100)), [])

    def test_blocked_claim_expires_after_the_blocked_timer(self):
        i = self.blocked_by_alice()
        acts = cb.decide_sweep(i, h(1 + cb.BLOCKED_STALE_HOURS))
        self.assertCountEqual(acts, [("unassign", "alice"), ("label_remove", cb.BLOCKED)])

    def test_a_new_blocked_comment_renews_the_timer(self):
        i = self.blocked_by_alice()
        c = human("alice", "Blocked: still waiting", h(150))
        i = applied(i, c, cb.decide_comment(i, c, h(150)))
        self.assertEqual(cb.decide_sweep(i, h(1 + cb.BLOCKED_STALE_HOURS + 10)), [])

    def test_takeover_of_a_blocked_claim_is_refused_with_time_left(self):
        acts = cb.decide_comment(
            self.blocked_by_alice(),
            human("bob", "Taking over stale claim from @alice", h(100)),
            h(100),
        )
        self.assertTrue(rejected(acts, "bob"), acts)
        self.assertIn("blocked", acts[0][1])

    def test_claimant_progress_comment_clears_blocked(self):
        acts = cb.decide_comment(
            self.blocked_by_alice(), human("alice", "Unblocked, back on it", h(10)), h(10)
        )
        self.assertEqual(acts, [("label_remove", cb.BLOCKED)])

    def test_progress_returns_to_the_normal_timer(self):
        i = self.blocked_by_alice(extra=[human("alice", "PR soon", h(10))])
        self.assertIn(("label_remove", cb.BLOCKED), cb.decide_sweep(i, h(20)))
        self.assertIn(("unassign", "alice"), cb.decide_sweep(i, h(10 + cb.STALE_HOURS)))

    def test_pr_activity_clears_blocked_in_the_sweep(self):
        i = self.blocked_by_alice()
        i.prs.append(LinkedPR(5, "alice", [h(20)]))
        self.assertEqual(cb.decide_sweep(i, h(30)), [("label_remove", cb.BLOCKED)])

    def test_release_handoff_and_reassign_remove_blocked(self):
        i = self.blocked_by_alice()
        for c in (
            human("alice", "Releasing x", h(10)),
            human("alice", "Releasing x - handing it to @bob", h(10)),
            human("zackees", "/reassign @bob", h(10), "MEMBER"),
        ):
            self.assertIn(("label_remove", cb.BLOCKED), cb.decide_comment(i, c, h(10)), c.body)

    def test_locked_wins_over_blocked(self):
        i = self.blocked_by_alice()
        i.labels.add(cb.LOCKED)
        self.assertEqual(cb.decide_sweep(i, h(1000)), [])

    def test_stray_blocked_label_without_a_claim_is_removed(self):
        self.assertEqual(
            cb.decide_sweep(issue(labels=[cb.BLOCKED]), h(1)), [("label_remove", cb.BLOCKED)]
        )


class DecisionTest(unittest.TestCase):
    def decide(self, i, c):
        acts = cb.decide_comment(i, c, c.created)
        return acts, applied(i, c, acts)

    def test_decision_line_sets_the_label_silently(self):
        for body in (
            "Decision needed: A or B?",
            "Plan below.\n\n**Decision needed:** A or B?",
            "Notes\n### Decision needed: A or B?",
            "**Decision needed**: A or B?",
        ):
            c = human("bob", body, h(1))
            self.assertEqual(
                cb.decide_comment(issue(), c, h(1)),
                [("label_add", cb.DECISION), ("react", c.id)],
                body,
            )

    def test_running_text_does_not_set_it(self):
        for body in (
            "There is no decision needed: the default is safe.",
            "We said decision needed: later.",
        ):
            self.assertEqual(cb.decide_comment(issue(), human("bob", body, h(1)), h(1)), [], body)

    def test_maintainer_decided_clears_it_and_the_escalation(self):
        i = issue(labels=(cb.DECISION, cb.HUMAN_REVIEW))
        c = human("zed", "Decision: B.", h(2), assoc="OWNER")
        self.assertTrue(cb.handles(c.body, set()))
        acts = cb.decide_comment(i, c, h(2))
        self.assertEqual(
            acts,
            [("label_remove", cb.DECISION), ("label_remove", cb.HUMAN_REVIEW), ("react", c.id)],
        )

    def test_non_maintainer_decided_is_refused(self):
        acts = cb.decide_comment(
            issue(labels=(cb.DECISION,)), human("bob", "Decided: B.", h(2)), h(2)
        )
        self.assertTrue(rejected(acts, "bob"), acts)

    def test_decided_resumes_a_blocked_claim(self):
        _, i = self.decide(claimed_by_alice(), human("alice", "Decision needed: A or B?", h(1)))
        acts = cb.decide_comment(i, human("zed", "Decided: B.", h(3), assoc="OWNER"), h(3))
        self.assertIn(("label_remove", cb.BLOCKED), acts)
        notes = [a[1] for a in acts if a[0] == "comment"]
        self.assertEqual(len(notes), 1)
        self.assertIn("@alice", notes[0])
        self.assertIn("Decided: B.", notes[0])

    def test_claimant_decision_also_blocks_the_claim(self):
        c = human("alice", "Port plan.\nDecision needed: drop the Windows path?", h(1))
        acts, i = self.decide(claimed_by_alice(), c)
        self.assertCountEqual(
            acts, [("label_add", cb.DECISION), ("label_add", cb.BLOCKED), ("react", c.id)]
        )
        self.assertEqual(cb.decide_sweep(i, h(100)), [])

    def test_the_sweep_never_removes_it(self):
        i = claimed_by_alice(labels=(cb.READY, cb.DECISION))
        self.assertNotIn(("label_remove", cb.DECISION), cb.decide_sweep(i, h(100)))

    def test_the_event_gate(self):
        self.assertTrue(cb.handles("Long note\n**Decision needed:** A?", set()))
        self.assertTrue(cb.handles("Decided: A", {cb.DECISION}))
        self.assertFalse(cb.handles("just a comment", set()))
        self.assertTrue(cb.handles("progress", {cb.BLOCKED}))
        self.assertTrue(cb.handles("/ready", set()))


class QuietTest(unittest.TestCase):
    """Only rejections (and a decision that resumes a claim) comment."""

    def scenarios(self):
        alice = claimed_by_alice
        yield False, cb.decide_comment(issue(), human("bob", "Claiming: x", h(0)), h(0))
        yield True, cb.decide_comment(alice(), human("bob", "Claiming: x", h(1)), h(1))
        yield False, cb.decide_comment(alice(), human("bob", "Claiming: x", h(30)), h(30))
        yield False, cb.decide_comment(alice(), human("alice", "Releasing x", h(2)), h(2))
        yield (
            False,
            cb.decide_comment(
                alice(), human("alice", "Releasing x — handing it to @bob", h(2)), h(2)
            ),
        )
        yield (
            True,
            cb.decide_comment(
                alice(), human("bob", "Taking over stale claim from @alice", h(10)), h(10)
            ),
        )
        yield True, cb.decide_comment(alice(), human("bob", "/reassign @bob", h(1)), h(1))
        yield (
            False,
            cb.decide_comment(alice(), human("zackees", "/reassign @bob", h(1), "MEMBER"), h(1)),
        )
        yield True, cb.decide_comment(issue(labels=()), human("bob", "Claiming: x", h(0)), h(0))
        yield (
            False,
            cb.decide_comment(
                issue(labels=()), human("bob", "Claiming: investigation", h(0)), h(0)
            ),
        )
        yield False, cb.decide_sweep(alice(), h(24))

    def test_only_rejections_comment(self):
        for is_rejection, acts in self.scenarios():
            self.assertEqual("comment" in kinds(acts), is_rejection, acts)

    def test_a_rejection_mentions_only_the_person_refused(self):
        for _, acts in self.scenarios():
            for kind, arg in acts:
                if kind == "comment":
                    prose = re.sub(r"`[^`]*`", "", arg)
                    self.assertEqual(len(re.findall(r"(?<!\w)@[A-Za-z0-9-]", prose)), 1, arg)


if __name__ == "__main__":
    unittest.main()
