"""The labels the workflow reads, created in every repo that has issues.

    uv run -m workflow.labels            # create or update them org-wide
    uv run -m workflow.labels --dry-run

Safe to rerun: an existing label is updated in place (`gh label create
--force`).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

LABELS = {
    "ready": ("0e8a16", "Meets the definition of Ready; claimable"),
    "needs-investigation": ("fbca04", "Underdefined: research before it can be Ready"),
    "claim-blocked": ("d4c5f9", "The claimant waits on something outside their control"),
    "claim-locked": ("bfd4f2", "The claim bot leaves this issue alone"),
    "decision-needed": ("5319e7", "An open question for the maintainer"),
    "human-review-needed": ("b60205", "Escalated: waits on the human maintainer"),
    "tracker": ("c2e0c6", "Tracks work in sub-issues or a checklist; not claimable itself"),
    "P0": ("b60205", "Red master or broken release: drop everything (8 h claims)"),
    "P1": ("d93f0b", "Next up"),
    "P2": ("fbca04", "Normal priority"),
    "P3": ("c5def5", "Low priority"),
    "size:S": ("ededed", "Hours"),
    "size:M": ("ededed", "About a day"),
    "size:L": ("ededed", "Several days"),
    "size:XL": ("ededed", "Too big: split into sub-issues before it is Ready"),
}


def repos(org: str) -> list[str]:
    out = subprocess.run(
        [
            "gh",
            "repo",
            "list",
            org,
            "--limit",
            "200",
            "--no-archived",
            "--json",
            "name,hasIssuesEnabled",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [
        r["name"] for r in json.loads(out) if r["hasIssuesEnabled"] and "-ghsa-" not in r["name"]
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default="obs-rust")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    failed = 0
    for repo in repos(args.org):
        for name, (color, desc) in LABELS.items():
            if args.dry_run:
                print(f"{repo}: {name}")
                continue
            proc = subprocess.run(
                [
                    "gh",
                    "label",
                    "create",
                    name,
                    "-R",
                    f"{args.org}/{repo}",
                    "--color",
                    color,
                    "--description",
                    desc,
                    "--force",
                ],
                capture_output=True,
                text=True,
            )
            if proc.returncode:
                failed += 1
                print(f"{repo}: {name}: {proc.stderr.strip()}")
        print(f"{repo}: done")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
