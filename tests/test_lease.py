"""Two (or twenty) coordinators at once: leases let exactly one act."""

from __future__ import annotations

import threading
import unittest
from datetime import timedelta

from workflow import coordinator, lease
from workflow.sim import SimBackend, World

R = "obs-studio"


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.gh = SimBackend()
        self.addCleanup(self.gh.db.close)
        self.now = self.gh.now
        self.k = lease.key("review", R, 7)

    def test_key_drops_the_org(self):
        self.assertEqual(lease.key("review", "obs-rust/obs-studio", 7), "obs-studio/7/review")

    def test_first_taker_wins_second_is_told_who(self):
        self.assertEqual(lease.take(self.gh, self.k, "a", self.now), (True, "a"))
        self.assertEqual(lease.take(self.gh, self.k, "b", self.now), (False, "a"))

    def test_taking_your_own_lease_again_is_harmless(self):
        lease.take(self.gh, self.k, "a", self.now)
        self.assertEqual(lease.take(self.gh, self.k, "a", self.now), (True, "a"))

    def test_release_frees_it_and_only_the_holder_can(self):
        lease.take(self.gh, self.k, "a", self.now)
        self.assertFalse(lease.release(self.gh, self.k, "b"))
        self.assertTrue(lease.release(self.gh, self.k, "a"))
        self.assertEqual(lease.take(self.gh, self.k, "b", self.now), (True, "b"))

    def test_lapsed_lease_is_taken_over(self):
        lease.take(self.gh, self.k, "a", self.now)
        later = self.now + lease.TTL
        self.assertEqual(
            lease.take(self.gh, self.k, "b", later - timedelta(seconds=1)), (False, "a")
        )
        self.assertEqual(lease.take(self.gh, self.k, "b", later), (True, "b"))

    def test_twenty_coordinators_race_and_one_wins(self):
        results, start = [], threading.Barrier(20)

        def coord(i):
            start.wait()
            results.append(lease.take(self.gh, self.k, f"c{i}", self.now)[0])

        threads = [threading.Thread(target=coord, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 1)

    def test_racing_takeovers_of_a_lapsed_lease_let_one_through(self):
        lease.take(self.gh, self.k, "dead", self.now)
        later = self.now + lease.TTL
        results, start = [], threading.Barrier(10)

        def coord(i):
            start.wait()
            results.append(lease.take(self.gh, self.k, f"c{i}", later)[0])

        threads = [threading.Thread(target=coord, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertGreaterEqual(results.count(True), 1)
        self.assertEqual(len({o for o, _ in self.gh.leases().values()}), 1)


class TwoCoordinatorsTest(unittest.TestCase):
    """The work list each coordinator sees, with leases in play."""

    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.shutdown)

    def todo(self, owner):
        held = lease.held_by_others(self.w.gh, owner, self.w.now)
        return [
            (t.kind, t.number)
            for t in coordinator.tasks(self.w.gh.items("obs-rust"), [], self.w.now, held)
        ]

    def test_a_task_one_holds_disappears_from_the_others_list(self):
        a = self.w.open_pr(R, "x")
        b = self.w.open_pr(R, "y")
        self.assertEqual(self.todo("A"), [("review", a), ("review", b)])
        self.assertTrue(lease.take(self.w.gh, lease.key("review", R, a), "A", self.w.now)[0])
        self.assertEqual(self.todo("A"), [("review", a), ("review", b)])
        self.assertEqual(self.todo("B"), [("review", b)])

    def test_finished_task_leaves_both_lists(self):
        a = self.w.open_pr(R, "x")
        k = lease.key("review", R, a)
        lease.take(self.w.gh, k, "A", self.w.now)
        self.w.review(R, a)
        lease.release(self.w.gh, k, "A")
        self.assertEqual(self.todo("A"), [])
        self.assertEqual(self.todo("B"), [])

    def test_a_dead_coordinators_task_comes_back_after_the_ttl(self):
        a = self.w.open_pr(R, "x")
        lease.take(self.w.gh, lease.key("review", R, a), "A", self.w.now)
        self.assertEqual(self.todo("B"), [])
        self.w.advance(hours=2)
        self.assertEqual(self.todo("B"), [("review", a)])


if __name__ == "__main__":
    unittest.main()
