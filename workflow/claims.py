"""Claim rules: who may work on an issue, and when a claim expires.

Pure functions. ``claim_bot.py`` feeds them issues from a backend and applies
the actions they return. Actions are tuples:

  ("assign", login)  ("unassign", login)  ("label_add", name)
  ("label_remove", name)  ("react", comment_id)  ("comment", body)

A claim is the issue's assignee. Commands, read from the first line of a
comment:

  Claiming: <item>                  claim a Ready issue (assigns you)
  Claiming: investigation           claim the research on an issue that is not
                                    Ready yet (assigns you, labels it
                                    `needs-investigation`)
  /ready                            the investigator or a maintainer: the issue
                                    meets the definition of Ready
  /investigate                      the claimant or a maintainer: the issue is
                                    underdefined and needs research first
  Releasing <item>                  the claimant gives it up (unassigns)
  Releasing <item> — handing it to @x, Handing <item> over to @x,
  It's yours, @x, /handoff [item] [to] @x
                                    the claimant hands it to @x
  Taking over stale claim from @x   take a stale claim
  /reassign @y                      maintainers move any claim
  Blocked: <reason>, /blocked <reason>
                                    the claimant marks the claim `claim-blocked`
  Decided: <answer>, Decision: <answer>
                                    maintainers clear `decision-needed`

Read from any line of a comment:
  Decision needed: <question>       anyone adds `decision-needed`; from the
                                    claimant it also counts as `Blocked:`.

Only an issue labeled `ready` (or `P0`/`critical`, which never wait) can be
claimed for implementation. It cannot be claimed while it has an open blocker
(GitHub issue dependencies) or an open decision.

A claim goes stale after STALE_HOURS (P0_STALE_HOURS on P0/critical issues)
with no comment from the claimant on the issue and no commit, comment or
review on an open PR they opened that references it. A `claim-blocked` claim
lasts BLOCKED_STALE_HOURS from the claimant's last `Blocked:` comment. The
hourly sweep then unassigns, silently. `claim-locked` issues are left alone.

Before assignment, a claim was a `claimed` label. The sweep converts each
such claim into an assignment once and removes the label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from workflow.model import Comment, Issue, LinkedPR

__all__ = ["Comment", "Issue", "LinkedPR"]

BOT = "github-actions[bot]"
BOT_PREFIX = "**Claim bot:**"
CLAIMED = "claimed"  # the pre-assignment claim record, migrated by the sweep
CRITICAL = "critical"
P0 = "P0"
LOCKED = "claim-locked"
BLOCKED = "claim-blocked"
DECISION = "decision-needed"
HUMAN_REVIEW = "human-review-needed"
READY = "ready"
INVESTIGATE = "needs-investigation"
STALE_HOURS = 24
P0_STALE_HOURS = 8
BLOCKED_STALE_HOURS = 7 * 24
MAINTAINERS = {"OWNER", "MEMBER", "COLLABORATOR"}
REACTION = "rocket"

CLAIM_RE = re.compile(r"^\s*claiming\b", re.I)
INVESTIGATION_CLAIM_RE = re.compile(r"^\s*claiming\s*:?\s*(?:the\s+)?investigat", re.I)
READY_RE = re.compile(r"^\s*/ready\b", re.I)
INVESTIGATE_RE = re.compile(r"^\s*/investigate\b", re.I)
RELEASE_RE = re.compile(r"^\s*releasing\b", re.I)
TAKEOVER_RE = re.compile(r"^\s*taking over stale claim from @([A-Za-z0-9-]+)", re.I)
REASSIGN_RE = re.compile(r"^\s*/reassign\s+@([A-Za-z0-9-]+)", re.I)
BLOCKED_RE = re.compile(r"^\s*(?:blocked\s*:|/blocked\b)", re.I)
DECISION_RE = re.compile(
    r"^\s*(?:#{1,6}\s+)?[*_]{0,3}\s*decision\s+needed\s*[*_]{0,3}\s*:", re.I | re.M
)
DECIDED_RE = re.compile(r"^\s*[*_]{0,3}\s*(?:decided|decision)\s*[*_]{0,3}\s*:", re.I)
HANDOFF_RE = re.compile(
    r"^\s*(?:/?handoff\b|hand(?:ed|ing)\b|pass(?:ed|ing)\b|giv(?:e|ing)\b"
    r"|transfer(?:red|ring)\b|mov(?:e|ing)\b|it'?s\s+yours\b|\byours\b)",
    re.I,
)
HANDOFF_TO_RE = re.compile(
    r"\b(?:hand(?:ed|ing)|pass(?:ed|ing)|giv(?:e|ing)|transfer(?:red|ring)|mov(?:e|ing))"
    r"(?:\s+\S+){0,3}\s+to\s*@([A-Za-z0-9-]+)",
    re.I,
)
HANDOFF_CMD_RE = re.compile(r"^\s*/?handoff\b(?:\s+\S+){0,3}?\s*(?:to\s+)?@([A-Za-z0-9-]+)", re.I)
YOURS_RE = re.compile(r"\bit'?s\s+yours[,;:! ]+@([A-Za-z0-9-]+)", re.I)
COMMANDS = (
    CLAIM_RE,
    READY_RE,
    INVESTIGATE_RE,
    RELEASE_RE,
    TAKEOVER_RE,
    REASSIGN_RE,
    HANDOFF_RE,
    BLOCKED_RE,
)
BOT_CLAIM_RE = re.compile(
    re.escape(BOT_PREFIX) + r" (?:claimed by|claim moved to) [@`]([A-Za-z0-9-]+)"
)
BOT_CLEAR_RE = re.compile(re.escape(BOT_PREFIX) + r" claim (?:released|expired)")


@dataclass
class Claim:
    user: str
    since: datetime


def is_bot(login: str) -> bool:
    return login.endswith("[bot]")


def same(a: str, b: str) -> bool:
    return a.lower() == b.lower()


def handoff_target(line: str) -> str | None:
    """The `@user` a handoff line names, or None when it names nobody."""
    if m := HANDOFF_CMD_RE.match(line):
        return m.group(1)
    if not (RELEASE_RE.match(line) or HANDOFF_RE.match(line)):
        return None
    for rx in (HANDOFF_TO_RE, YOURS_RE):
        if m := rx.search(line):
            return m.group(1)
    return None


def legacy_claim(issue: Issue) -> Claim | None:
    """The claimant recorded by the old `claimed` label: the latest granted
    command decides; before the bot, the latest `Claiming:` comment."""
    claim: Claim | None = None
    seen_record = False
    for c in sorted(issue.comments, key=lambda c: c.created):
        line = c.first_line()
        if c.granted and not is_bot(c.author):
            seen_record = True
            if BLOCKED_RE.match(line) or READY_RE.match(line) or INVESTIGATE_RE.match(line):
                continue
            if m := REASSIGN_RE.match(line):
                claim = Claim(m.group(1), c.created)
            elif RELEASE_RE.match(line) or HANDOFF_RE.match(line):
                target = handoff_target(line)
                if target and (claim is None or not same(target, claim.user)):
                    claim = Claim(target, c.created)
                elif RELEASE_RE.match(line):
                    claim = None
            else:
                claim = Claim(c.author, c.created)
        elif c.author == BOT and (m := BOT_CLAIM_RE.match(c.body)):
            claim, seen_record = Claim(m.group(1), c.created), True
        elif c.author == BOT and BOT_CLEAR_RE.match(c.body):
            claim, seen_record = None, True
    if seen_record:
        return claim
    human = [c for c in issue.comments if not is_bot(c.author) and CLAIM_RE.match(c.first_line())]
    if not human:
        return None
    last = max(human, key=lambda c: c.created)
    return Claim(last.author, last.created)


def current_claim(issue: Issue) -> Claim | None:
    """The assignee, since they were assigned. A `claimed` label without an
    assignee is a claim from before assignment and is read the old way."""
    if issue.assignees:
        user = issue.assignees[0]
        since = issue.assigned_at.get(user)
        if since is None:
            granted = [c.created for c in issue.comments if c.granted and same(c.author, user)]
            since = (
                max(granted)
                if granted
                else min((c.created for c in issue.comments), default=datetime.min)
            )
        return Claim(user, since)
    if CLAIMED in issue.labels:
        return legacy_claim(issue)
    return None


def is_p0(issue: Issue) -> bool:
    return bool({P0, CRITICAL} & issue.labels)


def stale_hours(issue: Issue) -> int:
    return P0_STALE_HOURS if is_p0(issue) else STALE_HOURS


def last_activity(issue: Issue, claim: Claim) -> datetime:
    times = [claim.since]
    times += [c.created for c in issue.comments if same(c.author, claim.user)]
    for pr in issue.prs:
        if same(pr.author, claim.user):
            times += pr.activity
    return max(times)


def asks_decision(body: str) -> bool:
    return bool(DECISION_RE.search(body))


def blocks(c: Comment) -> bool:
    return bool(BLOCKED_RE.match(c.first_line())) or asks_decision(c.body)


def blocked_since(issue: Issue, claim: Claim) -> datetime | None:
    if BLOCKED not in issue.labels:
        return None
    mine = [c for c in issue.comments if same(c.author, claim.user) and c.created >= claim.since]
    blocked = [c.created for c in mine if blocks(c)]
    if not blocked:
        return None
    since = max(blocked)
    progress = [c.created for c in mine if not blocks(c)]
    for pr in issue.prs:
        if same(pr.author, claim.user):
            progress += pr.activity
    return None if any(t > since for t in progress) else since


def handles(body: str, labels: set[str]) -> bool:
    """Whether a new comment can lead to an action (the event gate)."""
    line = body.strip().splitlines()[0] if body.strip() else ""
    return (
        any(r.match(line) for r in COMMANDS)
        or bool(DECIDED_RE.match(line))
        or asks_decision(body)
        or BLOCKED in labels
    )


def stale_window(issue: Issue, claim: Claim) -> tuple[datetime, timedelta]:
    if since := blocked_since(issue, claim):
        return since, timedelta(hours=BLOCKED_STALE_HOURS)
    return last_activity(issue, claim), timedelta(hours=stale_hours(issue))


def is_stale(issue: Issue, claim: Claim, now: datetime) -> bool:
    start, span = stale_window(issue, claim)
    return now - start >= span


def u(login: str) -> str:
    """A login as code: GitHub sends no notification for it, unlike @login."""
    return f"`{login}`"


def reject(who: str, why: str) -> list[tuple]:
    """The one action that comments: it mentions the person refused."""
    return [("comment", f"{BOT_PREFIX} @{who}, {why}")]


def move(issue: Issue, claim: Claim | None, to: str, comment: Comment) -> list[tuple]:
    """Give the claim to `to`: assign them, unassign everyone else, react."""
    acts: list[tuple] = [("unassign", a) for a in issue.assignees if not same(a, to)]
    if not any(same(a, to) for a in issue.assignees):
        acts.append(("assign", to))
    if CLAIMED in issue.labels:
        acts.append(("label_remove", CLAIMED))
    return unblock(issue, acts + [("react", comment.id)])


def drop(issue: Issue, comment_id: int | None = None) -> list[tuple]:
    """End the claim: unassign everyone."""
    acts: list[tuple] = [("unassign", a) for a in issue.assignees]
    if CLAIMED in issue.labels:
        acts.append(("label_remove", CLAIMED))
    if comment_id is not None:
        acts.append(("react", comment_id))
    return unblock(issue, acts)


def unblock(issue: Issue, actions: list[tuple]) -> list[tuple]:
    """A claim that changes hands or ends is no longer blocked."""
    return actions + [("label_remove", BLOCKED)] if BLOCKED in issue.labels else actions


def not_claimable(issue: Issue, investigation: bool, trusted: bool = False) -> str | None:
    """Why the issue cannot be claimed now, or None. A maintainer's claim
    (`trusted`) is itself the triage: maintainers' agents file an issue with
    its plan and claim it in the same minute, so only a decision, a blocker
    or an explicit `needs-investigation` stops them."""
    if DECISION in issue.labels or HUMAN_REVIEW in issue.labels:
        return (
            "it waits on an open decision (`decision-needed`); "
            "it can be claimed once a maintainer answers with `Decided:`."
        )
    if issue.open_blockers:
        return (
            f"it is blocked by {issue.open_blockers} open issue(s) "
            '(see "Blocked by" on the issue); claim it once they close.'
        )
    if investigation or is_p0(issue) or READY in issue.labels:
        return None
    if trusted and INVESTIGATE not in issue.labels:
        return None
    if INVESTIGATE in issue.labels:
        return (
            "it needs investigation before anyone can implement it. "
            "Comment `Claiming: investigation` to take the research."
        )
    return (
        "it is not `ready` yet. Comment `Claiming: investigation` to research it, "
        "then `/ready` once it meets the definition of Ready."
    )


def decide_comment(issue: Issue, comment: Comment, now: datetime) -> list[tuple]:
    """Actions for one new comment."""
    if is_bot(comment.author) or LOCKED in issue.labels:
        return []
    line = comment.first_line()
    who = comment.author
    if DECIDED_RE.match(line):
        if comment.association not in MAINTAINERS:
            return reject(who, "only maintainers can mark a question `Decided:`.")
        return decided(issue, comment)
    acts = decide_command(issue, comment, now)
    if not asks_decision(comment.body) or any(a[0] == "comment" for a in acts):
        return acts
    claim = current_claim(issue)
    mine = claim is not None and same(claim.user, who)
    extra = [("label_add", DECISION)] + ([("label_add", BLOCKED)] if mine else [])
    acts = [a for a in acts if not (mine and a == ("label_remove", BLOCKED)) and a[0] != "react"]
    return extra + [a for a in acts if a not in extra] + [("react", comment.id)]


def decided(issue: Issue, comment: Comment) -> list[tuple]:
    acts = [("label_remove", DECISION)]
    acts += [("label_remove", HUMAN_REVIEW)] if HUMAN_REVIEW in issue.labels else []
    claim = current_claim(issue)
    if claim and BLOCKED in issue.labels:
        quote = "\n".join(f"> {line}" for line in comment.body.strip().splitlines())
        acts += [
            ("label_remove", BLOCKED),
            (
                "comment",
                f"{BOT_PREFIX} @{claim.user}, the decision is in; "
                f"your claim is no longer blocked.\n\n{quote}",
            ),
        ]
    return acts + [("react", comment.id)]


def decide_command(issue: Issue, comment: Comment, now: datetime) -> list[tuple]:
    line = comment.first_line()
    claim = current_claim(issue)
    who = comment.author
    maintainer = comment.association in MAINTAINERS
    mine = claim is not None and same(claim.user, who)

    if READY_RE.match(line):
        if not (mine or maintainer):
            return reject(who, "only the claimant or a maintainer can mark an issue `/ready`.")
        acts = [("label_add", READY)]
        acts += [("label_remove", INVESTIGATE)] if INVESTIGATE in issue.labels else []
        return acts + [("react", comment.id)]

    if INVESTIGATE_RE.match(line):
        if not (mine or maintainer):
            return reject(
                who, "only the claimant or a maintainer can send an issue back to `/investigate`."
            )
        acts = [("label_add", INVESTIGATE)]
        acts += [("label_remove", READY)] if READY in issue.labels else []
        return acts + [("react", comment.id)]

    if BLOCKED_RE.match(line):
        if claim is None:
            return reject(who, "nothing is claimed here, so there is no claim to mark blocked.")
        if not mine:
            return reject(who, f"only the claimant {u(claim.user)} can mark this claim blocked.")
        return [("label_add", BLOCKED), ("react", comment.id)]

    if m := REASSIGN_RE.match(line):
        if not maintainer:
            return reject(who, "only maintainers can `/reassign`.")
        return move(issue, claim, m.group(1), comment)

    if m := TAKEOVER_RE.match(line):
        if claim is None:
            why = not_claimable(issue, investigation=False)
            return (
                reject(who, f"this can't be claimed: {why}")
                if why
                else move(issue, claim, who, comment)
            )
        if not same(claim.user, m.group(1)):
            return reject(
                who, f"the current claim is held by {u(claim.user)}, not {u(m.group(1))}."
            )
        if not is_stale(issue, claim, now):
            start, span = stale_window(issue, claim)
            left = span - (now - start)
            if blocked_since(issue, claim):
                return reject(
                    who,
                    f"the claim by {u(claim.user)} is blocked on something "
                    f"outside their control; it expires in {fmt_hours(left)} without "
                    "a new `Blocked:` comment; ask the claimant or a maintainer.",
                )
            return reject(
                who,
                f"the claim by {u(claim.user)} is still active; "
                f"it goes stale in {fmt_hours(left)} without progress.",
            )
        return move(issue, claim, who, comment)

    if CLAIM_RE.match(line):
        investigation = bool(INVESTIGATION_CLAIM_RE.match(line))
        if mine:
            if investigation and INVESTIGATE not in issue.labels and READY not in issue.labels:
                return [("label_add", INVESTIGATE), ("react", comment.id)]
            return []
        if claim and not is_stale(issue, claim, now):
            return reject(
                who,
                f"this is already claimed by {u(claim.user)}. Pick something "
                "else or offer help here; once the claim goes stale, comment "
                f"`Taking over stale claim from @{claim.user}`.",
            )
        if why := not_claimable(issue, investigation, trusted=maintainer):
            return reject(who, f"this can't be claimed yet: {why}")
        acts = move(issue, claim, who, comment)
        if not investigation and READY not in issue.labels and not is_p0(issue):
            acts.insert(0, ("label_add", READY))  # a maintainer's claim triages it
        if investigation and READY not in issue.labels and INVESTIGATE not in issue.labels:
            acts.insert(0, ("label_add", INVESTIGATE))
        return acts

    if (RELEASE_RE.match(line) or HANDOFF_RE.match(line)) and mine:
        target = handoff_target(line)
        if target and not same(target, claim.user):
            return move(issue, claim, target, comment)
        if RELEASE_RE.match(line):
            return drop(issue, comment.id)
        return []
    if BLOCKED in issue.labels and mine and not any(r.match(line) for r in COMMANDS):
        return [("label_remove", BLOCKED)]
    return []


CATCH_UP = timedelta(hours=3)  # the sweep is hourly; cover two missed runs


def missed(issue: Issue, now: datetime) -> list[Comment]:
    """Recent commands the bot never answered: no reaction and no bot reply
    naming the author after them. A comment event can be lost (the workflow
    was broken, or GitHub dropped a pending run from the concurrency group);
    the sweep replays these."""
    out = []
    for i, c in enumerate(issue.comments):
        if is_bot(c.author) or c.granted or now - c.created > CATCH_UP:
            continue
        if not handles(c.body, issue.labels):
            continue
        later = issue.comments[i + 1 :]
        if any(is_bot(x.author) and f"@{c.author}" in x.body for x in later):
            continue
        out.append(c)
    return out


def effective(issue: Issue, actions: list[tuple]) -> list[tuple]:
    """Leave out actions the issue already reflects, so a replay is idempotent."""
    out = []
    for kind, arg in actions:
        if kind == "label_add" and arg in issue.labels:
            continue
        if kind == "label_remove" and arg not in issue.labels:
            continue
        if kind == "assign" and arg in issue.assignees:
            continue
        if kind == "unassign" and arg not in issue.assignees:
            continue
        out.append((kind, arg))
    return out


def decide_sweep(issue: Issue, now: datetime) -> list[tuple]:
    """Expire a stale claim (unassign, silently); convert a `claimed` label
    into an assignment."""
    if LOCKED in issue.labels:
        return []
    claim = current_claim(issue)
    if claim is None:
        acts = [("label_remove", BLOCKED)] if BLOCKED in issue.labels else []
        return acts + ([("label_remove", CLAIMED)] if CLAIMED in issue.labels else [])
    stale = is_stale(issue, claim, now)
    if stale:
        return drop(issue)
    acts: list[tuple] = []
    if BLOCKED in issue.labels and not blocked_since(issue, claim):
        acts.append(("label_remove", BLOCKED))
    if not issue.assignees:  # a `claimed` label from before assignment
        acts += [("assign", claim.user), ("label_remove", CLAIMED)]
        if READY not in issue.labels and INVESTIGATE not in issue.labels:
            acts.append(("label_add", READY))  # work already under way was ready
    return acts


def fmt_hours(d: timedelta) -> str:
    hours = max(0.0, d.total_seconds() / 3600)
    return f"{hours:.0f} h" if hours >= 1 else f"{max(1, round(hours * 60))} min"
