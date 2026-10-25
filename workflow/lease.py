"""Leases: one coordinator per task, even when several run at once.

A lease is a git ref, ``refs/leases/<repo>/<number>/<kind>``, in
obs-rust/.github. Creating a ref is atomic on GitHub: when it already exists
the call fails. So two coordinators that pick the same task race on one API
call, exactly one wins, and the other is told who holds it and moves on.

The ref points at a commit whose message records the holder and the time.
A lease lapses after TTL (a coordinator that died mid-task); the next taker
deletes it and creates its own, and if two take over at once, the create
still lets only one through.

Most coordinator actions are safe to repeat anyway (`gh pr merge --auto`,
labels, `--force-with-lease` pushes); leases cover the ones that are not
(reviews, `Decided:` answers, triage, CI fixes).
"""

from __future__ import annotations

from datetime import datetime, timedelta

TTL = timedelta(hours=2)


def key(kind: str, repo: str, number: int) -> str:
    return f"{repo.split('/')[-1]}/{number}/{kind}"


def take(gh, k: str, owner: str, now: datetime) -> tuple[bool, str]:
    """Try to take the lease. Returns (taken, holder)."""
    if gh.create_lease(k, owner, now):
        return True, owner
    held = gh.leases().get(k)
    if held is None:  # released between our try and our look: try once more
        return (True, owner) if gh.create_lease(k, owner, now) else (False, "someone")
    holder, since = held
    if holder == owner:
        return True, owner  # ours already: taking twice is harmless
    if now - since < TTL:
        return False, holder
    gh.delete_lease(k)  # lapsed: take it over, still through the atomic create
    if gh.create_lease(k, owner, now):
        return True, owner
    return False, gh.leases().get(k, ("someone", now))[0]


def release(gh, k: str, owner: str) -> bool:
    held = gh.leases().get(k)
    if held is None or held[0] != owner:
        return False
    gh.delete_lease(k)
    return True


def held_by_others(gh, owner: str, now: datetime) -> dict[str, str]:
    """Live leases someone else holds: key -> holder."""
    return {k: who for k, (who, since) in gh.leases().items() if who != owner and now - since < TTL}
