"""The repo-keyed GraphQL cache fetches only what changed."""

from __future__ import annotations

import os
import tempfile
import unittest

from workflow import ghcache


class FakeRepo:
    """A connection served the way GitHub does: ordered, paged, filtered."""

    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.calls: list[str] = []

    def put(self, id_, updated, state="OPEN", **extra):
        self.nodes[id_] = {"id": id_, "updatedAt": updated, "state": state, **extra}

    def fetch(self, args, cursor):
        self.calls.append(args)
        nodes = list(self.nodes.values())
        if "states: OPEN" in args:
            nodes = [n for n in nodes if n["state"] == "OPEN"]
        if "UPDATED_AT" in args:
            nodes.sort(key=lambda n: n["updatedAt"], reverse=True)
        start = int(cursor or 0)
        page = nodes[start : start + 2]
        more = start + 2 < len(nodes)
        return {
            "nodes": [dict(n) for n in page],
            "pageInfo": {"hasNextPage": more, "endCursor": str(start + 2)},
        }


class CacheTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        old = os.environ.get("GH_REPO_CACHE")
        os.environ["GH_REPO_CACHE"] = d.name
        self.addCleanup(
            lambda: os.environ.pop("GH_REPO_CACHE")
            if old is None
            else os.environ.__setitem__("GH_REPO_CACHE", old)
        )
        self.gh = FakeRepo()
        for i in range(10):
            self.gh.put(f"n{i}", f"2026-01-{i + 10:02d}", state="CLOSED" if i < 8 else "OPEN")

    def read(self, fields="id updatedAt state", repo="obs-studio", volatile="states: OPEN"):
        nodes, fetched = ghcache.read(
            "obs-rust", repo, "pullRequests", fields, self.gh.fetch, volatile
        )
        return {n["id"]: n for n in nodes}, fetched

    def test_first_read_fetches_everything(self):
        nodes, fetched = self.read()
        self.assertEqual(len(nodes), 10)
        self.assertEqual(fetched, 10)

    def test_unchanged_repo_refetches_only_volatile_nodes(self):
        self.read()
        nodes, fetched = self.read()
        self.assertEqual(len(nodes), 10)
        # One node at the mark, plus the two open ones.
        self.assertEqual(fetched, 3)

    def test_changed_node_is_picked_up(self):
        self.read()
        self.gh.put("n2", "2026-02-01", state="CLOSED", title="edited")
        nodes, _ = self.read()
        self.assertEqual(nodes["n2"]["title"], "edited")

    def test_volatile_change_without_updated_at_is_picked_up(self):
        self.read()
        self.gh.nodes["n9"]["checks"] = "FAILURE"
        nodes, _ = self.read()
        self.assertEqual(nodes["n9"]["checks"], "FAILURE")

    def test_new_node_is_added(self):
        self.read()
        self.gh.put("n10", "2026-03-01")
        nodes, _ = self.read()
        self.assertIn("n10", nodes)

    def test_key_is_the_repo_url_and_the_field_selection(self):
        a = ghcache.path("obs-rust", "obs-studio", "issues", "id updatedAt")
        self.assertEqual(a, ghcache.path("OBS-Rust", "OBS-Studio", "issues", "id\n  updatedAt"))
        self.assertIn(os.path.join("github.com", "obs-rust", "obs-studio"), str(a))
        self.assertNotEqual(a, ghcache.path("obs-rust", "obs-deps", "issues", "id updatedAt"))
        self.assertNotEqual(
            a, ghcache.path("obs-rust", "obs-studio", "issues", "id updatedAt title")
        )

    def test_two_tools_reading_the_same_repo_share_the_cache(self):
        self.read()
        other = FakeRepo()
        other.nodes = self.gh.nodes
        _, fetched = ghcache.read(
            "obs-rust",
            "obs-studio",
            "pullRequests",
            "id   updatedAt state",
            other.fetch,
            "states: OPEN",
        )
        self.assertEqual(fetched, 3)

    def test_corrupt_cache_falls_back_to_a_full_read(self):
        self.read()
        ghcache.path("obs-rust", "obs-studio", "pullRequests", "id updatedAt state").write_text("{")
        _, fetched = self.read()
        self.assertEqual(fetched, 10)


if __name__ == "__main__":
    unittest.main()
