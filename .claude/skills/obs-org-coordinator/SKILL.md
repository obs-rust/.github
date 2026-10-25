---
name: obs-org-coordinator
description: Org-wide coordinator playbook for the obs-rust GitHub organization. Use when you run on a timer (/loop) as coordinator, concierge or steward, so that every obs-rust repository, not only obs-studio, has its ready PRs merged, its questions answered, its decisions made and its stalled follow-ups closed. You never claim or port an issue; that is the worker role.
---

# obs-org-coordinator: keeping every obs-rust repo moving

The `obs-coordinator` skill in obs-rust/obs-studio covers that one repo in
depth. The rest of the org (obs-deps, obs-browser, obs-websocket,
obs-studio-libs, cef, …) used to have no coordinator: on 2026-10-24 three
green obs-deps PRs had waited eight days and new issues in obs-browser and
obs-studio-libs had no answer. This skill is the sweep that catches them.

The [constitution](https://github.com/obs-rust/constitution/blob/main/CONSTITUTION.md)
overrides this file. A repo's own `AGENTS.md`, `CONTRIBUTING.md` or
`.claude/skills/*coordinator*` overrides the defaults below for that repo.

Codex and other agents without skills: read this file by hand
(`gh api repos/obs-rust/.github/contents/.claude/skills/obs-org-coordinator/SKILL.md -H 'Accept: application/vnd.github.raw'`).

## 0. Each tick: find the repos with work

Start from the **obs-rust Multiview** org project (https://github.com/orgs/obs-rust/projects/1), the master list of every
open issue and PR in the org. `org-tracker-sync` in this repo refreshes it
every 30 minutes and sets Status from live state: `In merge queue`, `Ready to merge`,
`Needs fix`, `Needs decision`, `In review`, `In progress`, `Triage`, `Done`.
Work the columns in that order:

```sh
gh project item-list 1 --owner obs-rust --limit 500 --format json \
  -q '.items[]|select(.status!="Done")|"\(.status)\t\(.content.repository)#\(.content.number)\t\(.title)"' | sort
```

(Your token needs the `project` scope.) The board
old, so check an item's live state before you act on it. If the board is
missing or the sync is failing, use the direct sweep below, and report the
broken sync.

Don't hard-code the repo list; repos get added. Every tick:

```sh
gh repo list obs-rust --limit 200 --no-archived --json name -q '.[].name' |
while read r; do
  p=$(gh pr list -R obs-rust/$r --json number -q length 2>/dev/null)
  i=$(gh issue list -R obs-rust/$r --json number -q length 2>/dev/null)
  [ "${p:-0}${i:-0}" != 00 ] && echo "$r prs=${p:-0} issues=${i:-0}"
done
gh search issues --owner obs-rust --state open --sort updated --limit 50 \
  --json repository,number,title,updatedAt,isPullRequest   # recent activity
```

Skip the `obs-studio-ghsa-*` repos: they are private advisory forks and go
through §5. Then work through each repo with work, oldest item first.

**obs-rust/obs-studio:** run the full `obs-coordinator` skill from that
repo (`.claude/skills/obs-coordinator/SKILL.md`). Everything below is for
the other repos.

## 1. Learn the repo's rules first

Before you act in a repo, read its `AGENTS.md`/`CLAUDE.md`,
`CONTRIBUTING.md` and branch rules:

```sh
gh api repos/obs-rust/<r>/rulesets
gh api graphql -f query='{repository(owner:"obs-rust",name:"<r>"){defaultBranchRef{name} mergeQueue{url}}}'
```

Remember the cross-repo directive in obs-studio's AGENTS.md:
obs-rust/obs-websocket and `plugins/obs-websocket` are kept as a
compatibility check. Don't change them to suit Rust OBS. Fix the Rust side.
Fixes to the plugin's own bugs (security, crashes) are fine.

## 2. Merge ready PRs

Ready means open, not a draft, every check green, `mergeStateStatus` `CLEAN`,
and no unresolved change request.

- **Repo has a merge queue:** `gh pr merge <N> -R obs-rust/<r> --auto` and
  confirm it entered the queue, as in obs-coordinator §1.
- **No merge queue, but CI ran on the PR and passed:** review it first (§3).
  If the review finds nothing blocking, merge it with the repo's usual
  strategy (look at recent merges: `gh pr list -R obs-rust/<r> --state merged
  --limit 5 --json mergeCommit,title`), for example
  `gh pr merge <N> -R obs-rust/<r> --squash`.
- **No CI ran on the PR** (none configured, or none triggers on PRs): don't
  merge. Review it and list it in the report for the maintainer.
- **Upstream syncs:** some repos copy obsproject repos. A PR that brings
  in an upstream change gets the same review, and one that conflicts with
  an obs-rust change waits for the maintainer.

Never merge with failing checks or with `--admin`.

## 3. Review and unstick

Treat each PR as obs-coordinator §2 and §2a do: read failing logs before
re-running (`gh run rerun <run> --failed` only for flakes), rebase `DIRTY`
or stale PRs with `gh pr update-branch <N> -R obs-rust/<r> --rebase` when the
conflict is mechanical, push only mechanical `fixup:` commits, and comment
the cause for the author when the failure is in their logic. Review every
unreviewed contributor PR and post the findings as a PR review. A
draft with no activity for 7 days gets a comment asking if it is still
being worked on.

## 4. Answer questions, decide, close

For each open issue and recent comment by someone other than the maintainer
or a bot:

- **Questions:** answer when the code or docs settle it.
- **Decisions:** follow obs-coordinator §3b for every repo. Decide with a
  `Decided:` comment when a rule, prior decision or safe reversible default
  settles it; otherwise label `human-review-needed` (create the label if
  the repo lacks it: `gh label create human-review-needed -R obs-rust/<r>`)
  and give the options and a recommendation.
- **New issues with no reply:** triage them in the tick you find them.
  Confirm or ask for a reproduction, label them, and say where the fix goes.
  Many of these repos have no workers. Say so when the issue needs
  one, and list it in the report.
- **Stalled follow-ups:** follow obs-coordinator §4. Close what was
  concluded, and do the agreed next step that nobody took.
- **Cross-repo work:** an issue that really belongs in another obs-rust
  repo gets a comment linking the right one. Transfer it
  (`gh issue transfer`) only when both repos are ours and nobody has
  replied yet.

## 5. Security advisories

List advisories in triage for every repo, not only obs-studio:

```sh
gh repo list obs-rust --limit 200 --no-archived --json name -q '.[].name' |
while read r; do
  gh api "repos/obs-rust/$r/security-advisories?state=triage" \
    -q ".[]|\"$r \(.ghsa_id) \(.summary)\"" 2>/dev/null
done
```

Triage each one as obs-coordinator §3a says, with obs-studio's
[SECURITY.md](https://github.com/obs-rust/obs-studio/blob/master/SECURITY.md)
tiers unless the repo has its own SECURITY.md. Never discuss an advisory
in a public issue or PR.

## 6. Report

End each tick with one section per repo that had work: what you merged,
rebased, re-ran, reviewed, answered, decided and closed, and what needs the
maintainer. Also list every open `human-review-needed` item in the org with
its age:

```sh
gh search issues --owner obs-rust --state open --label human-review-needed \
  --json repository,number,title,createdAt
```

Say "quiet" only when the sweep in §0 found nothing to do. A PR or issue
in a side repo that shows up in report after report with nobody acting on
it is a coordinator failure, the same as in obs-studio.

## Never

- Claim, port or take over an issue (that is `obs-org-worker`).
- Merge outside a merge queue that exists, with failing checks, with no CI,
  or with `--admin`.
- Modify `constitution` (locked to the maintainer) or the obs-websocket
  plugin to suit Rust OBS.
- Open new issues for problems you notice; report them instead.
