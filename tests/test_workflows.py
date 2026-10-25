"""The workflow files stay thin: they schedule scripts and hold no logic."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


class ThinWorkflowTest(unittest.TestCase):
    def test_there_are_workflows(self):
        self.assertEqual({w.name for w in WORKFLOWS}, {"board-sync.yml", "claim-bot.yml"})

    def test_every_run_step_is_one_uv_command(self):
        for wf in WORKFLOWS:
            for line in re.findall(r"^\s*(?:- )?run:\s*(.*)$", wf.read_text(), re.M):
                self.assertRegex(line, r"^uv run -m [\w.]+( [\w. -]+)?$", f"{wf.name}: {line}")

    def test_no_multiline_scripts(self):
        for wf in WORKFLOWS:
            self.assertNotRegex(wf.read_text(), r"run:\s*\|", wf.name)

    def test_comment_body_never_reaches_a_shell(self):
        for wf in WORKFLOWS:
            self.assertNotRegex(wf.read_text(), r"\$\{\{\s*github\.event\.comment", wf.name)
            self.assertNotIn("pull_request_target", wf.read_text(), wf.name)

    def test_claim_bot_can_write_issues_only(self):
        text = (ROOT / ".github" / "workflows" / "claim-bot.yml").read_text()
        self.assertIn("issues: write", text)
        self.assertNotIn("contents: write", text)
        self.assertIn("workflow_call", text)

    def test_board_sync_runs_tests_before_syncing(self):
        text = (ROOT / ".github" / "workflows" / "board-sync.yml").read_text()
        self.assertIn("needs: test", text)
        self.assertRegex(text, r'cron: "\*/30 \* \* \* \*"')


class LabelsTest(unittest.TestCase):
    def test_every_label_the_code_reads_is_created(self):
        from workflow import board, claims, labels

        read = {
            claims.READY,
            claims.INVESTIGATE,
            claims.BLOCKED,
            claims.LOCKED,
            claims.DECISION,
            claims.HUMAN_REVIEW,
            claims.P0,
        }
        read |= set(board.PRIORITIES) | {f"size:{s}" for s in board.SIZES}
        self.assertLessEqual(read, set(labels.LABELS))


if __name__ == "__main__":
    unittest.main()
