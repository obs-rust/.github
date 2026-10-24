---
name: obs-org-worker
description: Org-wide worker playbook for the obs-rust GitHub organization. Use when you act as a worker who picks the next most important issue anywhere in the obs-rust org (obs-studio, obs-deps, obs-browser, obs-websocket, obs-studio-libs, …), claims it, fixes it and lands the PR, following each repo's own rules.
---

# obs-org-worker: picking up work anywhere in obs-rust

The `obs-worker` and `obs-worker-loop` skills in obs-rust/obs-studio cover
that repo. This skill picks work across the whole org and says how to do
it in a repo that has no worker skill of its own.

The [constitution](https://github.com/obs-rust/constitution/blob/main/CONSTITUTION.md)
overrides this file. A repo's own `AGENTS.md`, `CONTRIBUTING.md` or worker
skill overrides the defaults below for that repo.

Codex and other agents without skills: read this file by hand
(`gh api repos/obs-rust/.github/contents/.claude/skills/obs-org-worker/SKILL.md -H 'Accept: application/vnd.github.raw'`).

## 1. Move your own PRs first

```sh
gh search prs --owner obs-rust --author @me --state open \
  --json repository,number,title,isDraft
```

Fix failures, answer reviews and rebase conflicts on every one before you
start something new.

## 2. Pick the most important issue in the org

```sh
gh search issues --owner obs-rust --state open --no-assignee \
  --json repository,number,title,labels,createdAt,commentsCount --limit 100
```

Rank by: master or release broken in any repo > a bug that blocks
obs-studio (a dependency build in obs-deps or obs-studio-libs, a plugin
crash in obs-browser) > user-facing bugs > the rest. Skip issues
with a fresh claim (`claimed` label or a `Claiming:` comment under 24 h
old), `decision-needed` or `human-review-needed` issues, and anything in
`constitution`. Before you start, search for open PRs in that repo that
already fix it.

An obs-studio issue: switch to the obs-studio `obs-worker` skill and
follow it exactly. It has the testing tiers and CI rules.

## 3. Claim, then work in the right checkout

Comment `Claiming: <one-line plan>` on the issue (and add the `claimed`
label where the repo has it) before you write code.

Work in the existing local checkout of that repo. Look under
`~/dev/obs-rust/` and `~/dev/obs-rust/obs-studio2-extern/`, fetch, and branch
from the default branch. Clone into `~/dev/obs-rust/obs-studio2-extern/<repo>`
only when there is no local copy. Never make a second clone or worktree of a
repo you already have; build caches are keyed to the path.

## 4. Do the work

- Read the repo's `AGENTS.md`/`CONTRIBUTING.md`/README build section and
  follow its build, lint and test commands.
- **RED then GREEN** everywhere: a commit with a failing test or
  reproduction, then the fix. If the repo has no test harness, put the exact
  reproduction and its before/after output in the PR body.
- Keep the repo's style and commit-subject format (look at `git log`).
- In obs-websocket, fix the plugin's own bugs only. Never change it to
  suit Rust OBS (obs-studio AGENTS.md).
- A change that obs-studio must pick up (a new obs-deps or
  obs-studio-libs build, a cef or obs-browser fix): link the follow-up
  obs-studio issue or PR in both places.
- High-risk security bugs go to a private advisory, never a public
  issue, PR or branch (obs-studio SECURITY.md, "Two tiers").

## 5. Open the PR and land it

Open the PR against the default branch as soon as RED exists (a draft is
fine), with `Fixes #<N>` and the RED/GREEN evidence. Keep it moving until
it merges: repos with a merge queue use `gh pr merge --auto`. In repos
without one, ask for review in a comment once CI is green; the coordinator
(`obs-org-coordinator`) merges green, reviewed PRs.

## 6. Report

When the PR is merged or handed off, say what you did, and pick the next
issue (§2) if you run on a timer.
