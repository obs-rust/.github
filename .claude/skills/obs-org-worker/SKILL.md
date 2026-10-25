---
name: obs-org-worker
description: Worker playbook for the whole obs-rust GitHub organization. Use when you pick up work anywhere in the org (obs-studio, obs-deps, obs-browser, obs-websocket, obs-studio-libs, …): choose a Ready issue, claim it (which assigns you), land a PR in the merge queue, then move on to the next issue.
---

# obs-org-worker: Ready issue to merge queue, then the next one

How work moves: [WORKFLOW.md](../../../WORKFLOW.md). In obs-rust/obs-studio,
also follow that repo's `obs-worker` skill for its test tiers and CI.

Agents without skills: `gh api repos/obs-rust/.github/contents/.claude/skills/obs-org-worker/SKILL.md -H 'Accept: application/vnd.github.raw'`.

## 1. Your open PRs first

```sh
gh search prs --owner obs-rust --author @me --state open --json repository,number,title,isDraft
```

Answer reviews and fix failures on PRs that are **not** in the merge queue
yet. A PR in the queue is the coordinator's now; leave it.

## 2. Pick a Ready issue

The [board](https://github.com/orgs/obs-rust/projects/1) lists them
(Status **Ready**), or:

```sh
gh search issues --owner obs-rust --state open --label ready --no-assignee \
  --json repository,number,title,labels --limit 100
```

Highest priority first (`P0` > `P1` > `P2` > `P3`), then oldest. Prefer
work that unblocks others. A `P0` (red master, broken release) needs no
`ready` label.

Nothing Ready that you can do? Take an investigation instead: an issue in
**Triage** or **Investigating** with nobody on it.

## 3. Claim it

Comment on the issue, first line:

- `Claiming: <one-line plan>`, then a short numbered plan and when the first
  PR opens. The bot assigns you. In a repo with no claim bot (no
  `.github/workflows/claim-bot.yaml`), also assign yourself:
  `gh issue edit <N> -R <repo> --add-assignee @me`. A refusal comes as a reply that says why
  (not Ready, blocked, decision open, someone else holds it).
- `Claiming: investigation`: research an underdefined issue. When it meets the
  [definition of Ready](../../../WORKFLOW.md#definition-of-ready), comment
  `/ready` with the reproduction and acceptance criteria. You keep the claim
  and can implement it, or `Releasing` it for someone else.

The claim lasts **24 hours** from your last progress (a comment, a commit or
a review on your PR that references the issue). Post progress, or it lapses
and someone else may take it. Waiting on something outside your control:
`Blocked: <reason>` (7 days). A question with no safe default:
`Decision needed: <question>`.

## 4. Work in the right checkout

Use the existing local checkout of that repo (`~/dev/obs-rust/`,
`~/dev/obs-rust/obs-studio2-extern/`); clone there only if none exists.
Never a second clone or worktree of a repo you already have. Branch from the
default branch. Follow the repo's AGENTS.md for build, lint and test.

RED then GREEN: a failing test (or, with no harness, an exact reproduction)
first, then the fix. Open the PR as a draft as soon as RED exists, with
`Fixes #<N>` so the board links it.

## 5. Land it, then move on

Get it green and mergeable, then `gh pr merge <N> --auto` (repos with a
merge queue). Once it is **in the queue, you are done**: comment
`Handed off to the coordinator` on the PR and go to §2 for the next issue.
Ejections, rebases and CI repairs after that are the coordinator's.

In a repo without a merge queue, ask for review once CI is green; the
coordinator merges it.

## Rules that hold everywhere

- High-risk security bugs (remote reach, credential leaks) go to a private
  advisory, never a public issue, PR or branch.
- obs-websocket: fix the plugin's own bugs only; never change it to suit
  Rust OBS.
- A change obs-studio must pick up (deps, libs, cef, browser): link the
  follow-up obs-studio issue in both places.
