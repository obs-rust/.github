# How work moves in obs-rust

One process for every repo in the org. The code is in [`workflow/`](workflow/),
tested in simulation (`uv run -m unittest discover -s tests -t .`). The board
is the [obs-rust Multiview](https://github.com/orgs/obs-rust/projects/1)
project.

## The lifecycle

```
Triage ──/investigate──▶ Investigating ──/ready──▶ Ready ──Claiming:──▶ In progress
   │                                                 ▲                      │
   └──────────────────────/ready─────────────────────┘                PR opened
                                                                           ▼
            Done ◀──merged── In merge queue ◀──queued── Ready to merge ◀── In review
                                   │                                       ▲
                                   └──ejected──▶ Needs fix ──fixed─────────┘
```

Side states: **Blocked** (an open "blocked by" issue, or the claimant's
`Blocked:`), **Needs decision** (`decision-needed`, `human-review-needed`),
**Stalled** (a PR with no activity for 72 hours), **Tracker** (an issue whose
work is in its sub-issues).

The board sync sets every Status from labels, assignees, dependencies and PR
state every 30 minutes; nobody edits Status by hand. Priority comes from
`P0`..`P3` labels, Size from `size:S`..`size:XL`. Created, Updated and Closed
are GitHub's built-in dates, for charts.

## Definition of Ready

An issue is Ready when it has:

1. a reproduction or a failing-test sketch (a bug), or a concrete scope (a task);
2. acceptance criteria: what must be true when it is done;
3. its dependencies linked as "blocked by", and all of them closed;
4. no open `Decision needed:`;
5. a `P0`..`P3` label and a `size:` label. An XL issue is split into
   sub-issues first.

The coordinator checks new issues against this every tick (`triage`) and
comments `/ready` or `/investigate`. `P0` issues (a red master, a broken
release) are always Ready.

## Claims

A claim is the issue's assignee. Comment on the issue (first line):

| Command | Who | Effect |
|---|---|---|
| `Claiming: <plan>` | anyone, on a Ready issue; a maintainer on any issue not under investigation | assigns you (a maintainer's claim also labels it `ready`) |
| `Claiming: investigation` | anyone, on any issue | assigns you, labels `needs-investigation` |
| `/ready` | the claimant or a maintainer | labels `ready` (the claim continues) |
| `/investigate` | the claimant or a maintainer | back to `needs-investigation` |
| `Releasing <item>` | the claimant | unassigns |
| `Handing <item> over to @x`, `/handoff @x`, `It's yours, @x` | the claimant | reassigns to x |
| `Taking over stale claim from @x` | anyone | takes a stale claim |
| `/reassign @x` | maintainers | moves any claim |
| `Blocked: <reason>` | the claimant | `claim-blocked`, 7-day timer |
| `Decision needed: <question>` (any line) | anyone | `decision-needed` |
| `Decided: <answer>` | maintainers | clears the decision, resumes a blocked claim |

A claim goes stale after **24 hours** without progress (8 hours on `P0`): no
comment from the claimant and no commit, comment or review on their open PR
that references the issue. The hourly sweep unassigns it. Refused commands
get one reply that mentions only the person refused.

A repo turns claims on with this workflow (`.github/workflows/claim-bot.yaml`):

```yaml
name: Claim bot
on:
  issue_comment:
    types: [created]
  schedule:
    - cron: "23 * * * *"
  workflow_dispatch:
permissions:
  issues: write
  pull-requests: read
jobs:
  claim:
    uses: obs-rust/.github/.github/workflows/claim-bot.yml@master
```

## Roles

**Workers** take a Ready issue, claim it, open a PR (a draft as soon as RED
exists), get it green and into the merge queue, then **stop and move on**.
Ejections, rebases and CI repairs after that are the coordinator's.

**The coordinator** runs `uv run -m workflow.coordinator` at the start of
every tick and clears the list top to bottom. Each kind comes with its
instructions:

`fix-master` → `requeue` → `fix-ci` → `enqueue` → `rebase` → `review` →
`decide` → `triage` → `stalled` → `investigating` → `escalated`

Several coordinators can run at once. Before acting on a task:

```sh
uv run -m workflow.coordinator take review obs-studio 2272 --owner "$COORDINATOR_ID"
# exit 0: yours. exit 1: another coordinator has it; skip it.
uv run -m workflow.coordinator done review obs-studio 2272 --owner "$COORDINATOR_ID"
```

A lease is a git ref in this repo; GitHub creates a ref atomically, so only
one taker wins. It lapses after 2 hours. The list hides tasks others hold.

## Simulation

Every rule runs against a SQLite stand-in for GitHub and its Actions
(`workflow/sim.py`): comments trigger the claim bot, a fake clock fires the
hourly sweep and the 30-minute board sync. The same entry points run in
production and in simulation:

```sh
uv run -m workflow.claim_bot sweep --sim world.db
uv run -m workflow.board_sync --sim world.db
uv run -m workflow.coordinator --sim world.db
```
