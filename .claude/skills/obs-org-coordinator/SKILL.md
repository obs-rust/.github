---
name: obs-org-coordinator
description: Coordinator playbook for the whole obs-rust GitHub organization. Use when you run on a timer (/loop) as coordinator, concierge or steward. Each tick you run the work-list command, clear every task it lists across all repos (queue, rebase, fix CI, review, decide, triage, unstick), take a lease before each task so parallel coordinators never collide, and report. You never claim or port an issue; that is the worker role.
---

# obs-org-coordinator: clear the work list, every tick

You keep work moving in every obs-rust repo while the maintainer is away.
Workers stop when their PR enters the merge queue. Everything after that
(ejections, rebases, CI repairs), plus triage, reviews and decisions, is
yours. If you skip something, it piles up unseen, so the list below is
computed, not remembered.

The [constitution](https://github.com/obs-rust/constitution/blob/main/CONSTITUTION.md)
overrides this file; a repo's `AGENTS.md` adds its own rules (obs-studio's
`obs-coordinator` skill has its merge-queue and security details).
How work moves: [WORKFLOW.md](../../../WORKFLOW.md).

Agents without skills: `gh api repos/obs-rust/.github/contents/.claude/skills/obs-org-coordinator/SKILL.md -H 'Accept: application/vnd.github.raw'`.

## Setup (once per session)

Use the local checkout of obs-rust/.github (`~/dev/obs-rust/obs-studio2-extern/dotgithub`,
or clone it there). Each tick: `git switch master && git pull --prune`
(a merged branch's remote is deleted, so a plain `git pull` on it fails). Pick an id for this session:

```sh
export COORDINATOR_ID="coord-$(hostname)-$$"
```

## 1. Get the list

```sh
uv run -m workflow.coordinator            # grouped, with instructions per kind
uv run -m workflow.coordinator --json     # every task
```

It reads every repo live (not the board), leaves out tasks another
coordinator holds, and orders them:

| Kind | What it means | What you do |
|---|---|---|
| `fix-master` | a default branch's last push run failed | find the PR that broke it; fix or revert first. Queued PRs fail until it is green. Needs an admin (token, secret, rule)? One `human-review-needed` issue with the log line |
| `requeue` | a green PR now conflicts (ejected from the queue) | rebase, keep both sides of list conflicts, `--force-with-lease`, `gh pr merge --auto` |
| `fix-ci` | a PR's checks fail | §3 |
| `enqueue` | green and mergeable, not queued | `gh pr merge <N> -R <repo> --auto`, then confirm (gh pr view has no queue field): `gh api graphql -f query='{repository(owner:"obs-rust",name:"<repo>"){pullRequest(number:<N>){isInMergeQueue}}}'` |
| `merge` | green and reviewed, in a repo with **no** merge queue | `gh pr merge <N> -R <repo> --squash` |
| `rebase` | conflicts or is behind | `gh pr update-branch <N> --rebase`, or by hand |
| `review` | no review yet | review it (§4); then queue it, or merge it in a repo with no queue |
| `decide` | `decision-needed` | §5 |
| `triage` | not yet checked against the definition of Ready | §6 |
| `stalled` | a PR silent for 72 h | ask the author; a handed-off PR or one silent 7 days: take over the mechanical part |
| `investigating` | `needs-investigation` with nobody on it | say what is missing, or research it and `/ready` it |
| `escalated` | `human-review-needed` | report only |

## 2. Work it: lease, act, release

For each task, top to bottom:

```sh
uv run -m workflow.coordinator take <kind> <repo> <number> --owner "$COORDINATOR_ID" || continue
# ... do the task ...
uv run -m workflow.coordinator done <kind> <repo> <number> --owner "$COORDINATOR_ID"
```

`take` creates a git ref atomically, so if another coordinator runs the same
tick, exactly one of you gets each task and the other skips it. A lease
lapses after 2 hours if you die mid-task. Actions safe to repeat
(`gh pr merge --auto`, labels, lease-protected pushes) need no lease, but
take one anyway: it keeps the lists in step.

Before a push to someone's branch, check their latest commit: if they pushed
after the failure, read it instead of racing them. Push to a fork only when
maintainer edits are allowed, always with
`--force-with-lease=<branch>:<old sha>`. Never merge around a merge queue,
never with failing checks, never with `--admin`.

A repo without a merge queue: merge a green, reviewed PR with the repo's
usual strategy. A PR with no CI at all: review it and report it; don't merge.

## 3. Fixing CI (`fix-ci`, `fix-master`, ejections)

You are the CI fixer; there is no separate agent. Most crashes should
already be caught by the local gate, so this is the exception path:

1. Read the failed job log (`gh run view <run> --log-failed`).
2. **Flake** (timeout, runner died, network, a timing test the diff doesn't
   touch that passed in the sibling job): `gh run rerun <run> --failed`.
3. **Mechanical** (lint, format, a test count, a features-ledger line, a
   stale reference): push a `fixup:` commit and say so in a comment.
4. **Master was red**: fix master first (`fix-master`); re-queuing only
   ejects the PR again.
5. **A real failure in the PR's logic**: comment the cause and the log line
   for the author. A large fix: run it as a subagent with the obs-worker
   skill and this PR as its task; don't block the tick on it.

Never re-queue an ejected PR without reading why it was ejected.

**Requeue conflicts.** A PR shown UNMERGEABLE in the queue conflicts with a
PR ahead of it. Wait until that one lands (rebasing earlier drags its commits
in), then rebase onto the new default branch. Never resolve by just dropping
conflict markers: two blocks that share a closing line (`endif()`, `}`) lose
one. After resolving, check `git diff <new base> <new head> --stat` lists
only the PR's own files and line counts, then push with
`--force-with-lease=<branch>:<old sha>` and `gh pr merge --auto` again.

## 4. Reviewing

Correctness, RED then GREEN, the repo's required test tiers, the features
ledger, the repo's AGENTS.md. Post the findings as a PR review. Fix small
mechanical issues yourself as a fixup commit; leave design to the author.

One review per PR per tick: check `gh pr view <N> --json reviews` first, so
a second coordinator (or your retry) doesn't post a duplicate. GitHub won't
let you approve a PR opened by the account you run as: post a `--comment`
review with your verdict; that counts as the review.

## 5. Deciding

Research the question in the rules, docs, code and earlier decisions.
Decide it when they settle it, or when one option is a safe, reversible
default: a comment whose first line starts `Decided:`, then the reasons and
sources. Escalate only policy, governance, money, legal, security
trade-offs, outside parties, irreversible steps or the maintainer's
taste: relabel `human-review-needed` with the options and a recommendation.

## 6. Triage

Check the issue against the [definition of Ready](../../../WORKFLOW.md#definition-of-ready):

- **Acceptance criteria must be testable in that repo:** before you ask for
  a RED test, check the repo has a harness that can run it; if not, ask for
  an exact reproduction or a review-checkable criterion instead.
- **Meets it:** comment `/ready` with why, then set the labels yourself in
  one call: `gh issue edit <N> -R <repo> --add-label ready,P2,size:S`. Repos
  without the claim bot never answer `/ready`, and the label is what takes
  the issue out of triage; where the bot runs, the extra add is harmless.
  Split an XL into sub-issues.
- **Underdefined:** comment what is missing and `/investigate`.
- **Not a task** (an idea, a question): move it to Discussions and close it.
- **Already fixed or a duplicate:** close it with the link.
- **A tracker:** add its children as sub-issues; it leaves triage. A tracker
  whose items aren't issues yet (a checklist): label it `tracker`.
- **Asks to change the obs-websocket plugin to suit Rust OBS:** the
  AGENTS.md directive forbids it. Retitle it as the Rust OBS fix (or close it
  pointing to one) and quote the directive.
- **Looks finished** (its PRs merged): check each acceptance criterion
  against master, then close it with the PR links, even if claimed; the
  claimant gets the notification.

A large backlog: do the oldest 15 each tick (the list shows the count and
each item's labels). Big meta and port trackers sit at the front; label them
`tracker` on sight so small issues reach the front.

## 7. Outside the list

Every tick, also check what the list can't see:
- **Security advisories** in triage, in every repo
  (`gh api "repos/obs-rust/<r>/security-advisories?state=triage"`); triage
  them as obs-studio's `obs-coordinator` §3a says. Never discuss one in public.
- **New comments with questions** from people other than the maintainer:
  answer when the code or docs settle it.

## 8. Report

End each tick with: what you did (queued, rebased, fixed, reviewed, decided,
triaged), the `escalated` items with their ages, and anything you could not
finish and why. Say "quiet" only when `uv run -m workflow.coordinator`
prints `quiet: nothing to do` and §7 is empty.

When the list shows a kind growing tick after tick, the process has a gap:
fix the cause (a rule in `workflow/`, a skill, a doc) with a test in
`tests/`, not just the instances.

## Never

- Claim, port or take over an issue (that is `obs-org-worker`).
- Act on a task whose lease you didn't get.
- Merge outside a merge queue, with failing checks, or with `--admin`.
- Edit `constitution`, or change the obs-websocket plugin to suit Rust OBS.
- Open new issues for problems you notice; report them. Filing an agreed
  follow-up is fine.
