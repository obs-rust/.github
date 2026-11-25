"""Write failures must not replace the PR claim gate with a traceback."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from workflow import claim_bot, gh


class ClaimWriteErrors(unittest.TestCase):
    def pr_main(self, kind, reasons):
        number = 3909
        path = f"repos/obs-rust/obs-studio/issues/{number}/labels"
        method = "DELETE" if kind == "label_remove" else "POST"
        backend = Mock()
        backend.issue.return_value = SimpleNamespace(
            labels={"claim-conflict"} if kind == "label_remove" else set(),
            comments=[],
            closed=False,
        )

        def write(repo, number, action):
            if action[0] == kind:
                raise gh.GhError(method, path, 403, "Resource not accessible by integration")

        backend.apply_issue.side_effect = write
        event = {"action": "opened", "pull_request": {"number": number}}
        output = io.StringIO()
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as event_file:
            json.dump(event, event_file)
            event_file.flush()
            with (
                patch.dict(os.environ, GITHUB_EVENT_PATH=event_file.name),
                patch("sys.argv", ["claim_bot", "pr", "--repo", "obs-rust/obs-studio"]),
                patch.object(claim_bot, "backend", return_value=backend),
                patch.object(claim_bot.pr_claims, "conflicts", return_value=reasons),
                contextlib.redirect_stdout(output),
                patch.object(claim_bot, "log", side_effect=print),
            ):
                result = claim_bot.main()
        self.assertIn("::warning::", output.getvalue())
        self.assertIn(f"{method} {path} -> HTTP 403", output.getvalue())
        return result, backend

    def test_label_add_403_continues_and_conflict_exits_one(self):
        result, backend = self.pr_main("label_add", ["real conflict"])
        self.assertEqual(result, 1)
        self.assertEqual(backend.apply_issue.call_count, 2)
        self.assertEqual(backend.apply_issue.call_args.args[2][0], "comment")

    def test_label_remove_403_continues_and_clean_exits_zero(self):
        result, backend = self.pr_main("label_remove", [])
        self.assertEqual(result, 0)
        self.assertEqual(backend.apply_issue.call_count, 1)

    def test_4xx_attempted_once_with_method_path_status(self):
        path = "repos/obs-rust/obs-studio/issues/3909/labels"
        for status in (400, 401, 403, 404, 422):
            with (
                self.subTest(status=status),
                patch.object(
                    gh.subprocess,
                    "run",
                    return_value=SimpleNamespace(
                        returncode=1,
                        stderr=f"gh: Resource not accessible by integration (HTTP {status})",
                    ),
                ) as run,
                patch.object(gh.time, "sleep") as sleep,
            ):
                with self.assertRaises(gh.GhError) as caught:
                    gh.run(["gh", "api", "-X", "POST", path, "-f", "labels[]=claim-conflict"])
                self.assertEqual(run.call_count, 1)
                sleep.assert_not_called()
                error = caught.exception
                self.assertEqual((error.method, error.path, error.status), ("POST", path, status))
                self.assertIn(f"POST {path} -> HTTP {status}", str(error))

    def test_assign_403_in_comment_mode_still_raises(self):
        backend = Mock()
        backend.issue.return_value = SimpleNamespace(comments=[], labels=set(), assignees=[])
        backend.apply_issue.side_effect = gh.GhError("CLI", "issue edit", 403, "denied")
        event = {
            "issue": {"number": 1},
            "comment": {
                "user": {"login": "alice"},
                "author_association": "CONTRIBUTOR",
                "body": "Claiming: fix",
                "created_at": "2026-11-07T00:00:00Z",
                "id": 1,
            },
        }
        with (
            patch.object(claim_bot.claims, "handles", return_value=True),
            patch.object(claim_bot.claims, "decide_comment", return_value=[("assign", "alice")]),
            self.assertRaises(gh.GhError),
        ):
            claim_bot.on_comment(backend, "obs-rust/obs-studio", event, None)
        self.assertEqual(backend.apply_issue.call_count, 1)


class GhRetries(unittest.TestCase):
    def test_transient_statuses_retry_four_times(self):
        for status in (429, 502):
            with (
                self.subTest(status=status),
                patch.object(
                    gh.subprocess,
                    "run",
                    return_value=SimpleNamespace(
                        returncode=1, stderr=f"gh: unavailable (HTTP {status})"
                    ),
                ) as run,
                patch.object(gh.time, "sleep") as sleep,
            ):
                with self.assertRaises(gh.GhError):
                    gh.run(
                        [
                            "gh",
                            "api",
                            "--paginate",
                            "--slurp",
                            "-H",
                            "Accept: application/json",
                            "repos/o/r/issues",
                        ]
                    )
                self.assertEqual(run.call_count, 4)
                self.assertEqual(sleep.call_count, 3)

    def test_read_endpoint_survives_header_and_pagination_flags(self):
        with patch.object(
            gh.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stderr="gh: denied (HTTP 403)"),
        ) as run:
            with self.assertRaises(gh.GhError) as caught:
                gh.rest("repos/o/r/issues")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(caught.exception.method, "GET")
        self.assertEqual(caught.exception.path, "repos/o/r/issues")


class ForkPrExplanation(unittest.TestCase):
    """#34: a fork PR's token cannot label or comment, so the failed check
    itself must say why (annotation and step summary)."""

    def run_check(self, reasons):
        backend = Mock()
        backend.issue.return_value = SimpleNamespace(labels=set(), comments=[], closed=False)
        backend.apply_issue.side_effect = gh.GhError(
            "POST",
            "repos/obs-rust/obs-studio/issues/5547/comments",
            403,
            "Resource not accessible by integration",
        )
        event = {"action": "synchronize", "pull_request": {"number": 5547}}
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            summary = os.path.join(tmp, "summary.md")
            event_path = os.path.join(tmp, "event.json")
            with open(event_path, "w", encoding="utf-8") as f:
                json.dump(event, f)
            with (
                patch.dict(os.environ, GITHUB_EVENT_PATH=event_path, GITHUB_STEP_SUMMARY=summary),
                patch("sys.argv", ["claim_bot", "pr", "--repo", "obs-rust/obs-studio"]),
                patch.object(claim_bot, "backend", return_value=backend),
                patch.object(claim_bot.pr_claims, "conflicts", return_value=reasons),
                contextlib.redirect_stdout(output),
                patch.object(claim_bot, "log"),
            ):
                result = claim_bot.main()
            text = ""
            if os.path.exists(summary):
                with open(summary, encoding="utf-8") as f:
                    text = f.read()
        return result, output.getvalue(), text

    def test_a_conflict_is_explained_on_the_failed_check_when_writes_fail(self):
        reason = "obs-rust/obs-studio#5134 has no active claim"
        result, stdout, summary = self.run_check([reason])
        self.assertEqual(result, 1)
        errors = [line for line in stdout.splitlines() if line.startswith("::error")]
        self.assertEqual(len(errors), 1, stdout)
        self.assertIn(reason, errors[0])
        self.assertIn("Claiming:", errors[0])
        self.assertIn(reason, summary)
        self.assertIn("Claiming:", summary)

    def test_an_annotation_keeps_a_multi_line_explanation_on_one_line(self):
        reasons = ["#1 is not ready", "#2 has no active claim"]
        _, stdout, _ = self.run_check(reasons)
        error = next(line for line in stdout.splitlines() if line.startswith("::error"))
        self.assertIn("#1 is not ready%0A", error)
        self.assertIn("#2 has no active claim", error)

    def test_a_clean_pr_writes_no_error_or_summary(self):
        result, stdout, summary = self.run_check([])
        self.assertEqual(result, 0)
        self.assertNotIn("::error", stdout)
        self.assertEqual(summary, "")
