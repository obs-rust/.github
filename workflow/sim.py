"""Simulation backend: a SQLite stand-in for GitHub and its Actions.

``SimBackend`` implements the same methods as ``gh.GhBackend``, so the claim
bot and the board sync run unchanged against it. ``World`` drives it the way
GitHub would: a comment runs the claim bot's issue_comment workflow at once,
and ``advance()`` moves a fake clock, firing the hourly claim sweep and the
board sync on their cron schedules.

    world = World()                      # in memory; World("sim.db") persists
    n = world.open_issue("obs-studio", labels={"ready"})
    world.comment("obs-studio", n, "alice", "Claiming: it")
    world.advance(hours=25)              # the sweep expires the claim
    world.status("obs-studio", n)        # what the board shows

Also a CLI, so the bots run against a saved world in simulation mode:
    uv run -m workflow.claim_bot sweep --sim sim.db
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from workflow.claims import BOT, COMMANDS, REACTION, is_bot
from workflow.model import Board, Comment, Field, Issue, Item, LinkedPR

ORG = "obs-rust"
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

SCHEMA = """
CREATE TABLE IF NOT EXISTS clock (now TEXT);
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY, repo TEXT, number INTEGER, kind TEXT, state TEXT,
  author TEXT, created TEXT, updated TEXT, closed TEXT,
  draft INTEGER DEFAULT 0, in_queue INTEGER DEFAULT 0,
  merge_state TEXT DEFAULT 'BLOCKED', checks TEXT DEFAULT 'PENDING',
  reviews INTEGER DEFAULT 0, title TEXT DEFAULT '', parent TEXT);
CREATE TABLE IF NOT EXISTS labels (item TEXT, name TEXT, UNIQUE(item, name));
CREATE TABLE IF NOT EXISTS assignees (item TEXT, login TEXT, at TEXT, UNIQUE(item, login));
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT, author TEXT, assoc TEXT,
  body TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS reactions (comment INTEGER, user TEXT, content TEXT);
CREATE TABLE IF NOT EXISTS closes (pr TEXT, issue TEXT);
CREATE TABLE IF NOT EXISTS activity (pr TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS deps (blocker TEXT, blocked TEXT);
CREATE TABLE IF NOT EXISTS fields (name TEXT PRIMARY KEY, type TEXT, options TEXT);
CREATE TABLE IF NOT EXISTS board (content TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS board_values (content TEXT, field TEXT, value TEXT,
  UNIQUE(content, field));
CREATE TABLE IF NOT EXISTS log (at TEXT, actor TEXT, what TEXT);
CREATE TABLE IF NOT EXISTS red (repo TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS noqueue (repo TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS qstate (item TEXT PRIMARY KEY, state TEXT);
CREATE TABLE IF NOT EXISTS leases (key TEXT PRIMARY KEY, owner TEXT, at TEXT);
"""


def iso(t: datetime) -> str:
    return t.isoformat()


def parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class SimBackend:
    """GitHub, as far as the bots can see it, in one SQLite database."""

    def __init__(self, path: str = ":memory:") -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        # GitHub serializes writes server-side; one lock does the same here,
        # so racing coordinator threads see GitHub's semantics.
        self.lock = threading.RLock()
        self.db.executescript(SCHEMA)
        if not self.db.execute("SELECT 1 FROM clock").fetchone():
            self.db.execute("INSERT INTO clock VALUES (?)", (iso(T0),))
        self.db.commit()

    # ---- helpers ------------------------------------------------------

    @property
    def now(self) -> datetime:
        return parse(self.db.execute("SELECT now FROM clock").fetchone()[0])

    def q(self, sql: str, *args) -> list[tuple]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql: str, *args) -> None:
        with self.lock:
            try:
                self.db.execute(sql, args)
            except sqlite3.DatabaseError:
                self.db.rollback()
                raise
            self.db.commit()

    def node(self, repo: str, number: int) -> str:
        return self.q("SELECT id FROM items WHERE repo=? AND number=?", repo, number)[0][0]

    def log(self, actor: str, what: str) -> None:
        self.x("INSERT INTO log VALUES (?,?,?)", iso(self.now), actor, what)

    def touch(self, item: str) -> None:
        self.x("UPDATE items SET updated=? WHERE id=?", iso(self.now), item)

    # ---- claims (same interface as GhBackend) -------------------------

    def issue(self, repo: str, number: int) -> Issue:
        item = self.node(repo, number)
        comments = []
        for cid, author, assoc, body, created in self.q(
            "SELECT id, author, assoc, body, created FROM comments WHERE item=? ORDER BY id", item
        ):
            c = Comment(author, assoc, body, parse(created), cid)
            if not is_bot(author) and any(r.match(c.first_line()) for r in COMMANDS):
                c.granted = bool(
                    self.q(
                        "SELECT 1 FROM reactions WHERE comment=? AND user=? AND content=?",
                        cid,
                        BOT,
                        REACTION,
                    )
                )
            comments.append(c)
        prs = []
        for (pr,) in self.q(
            "SELECT pr FROM closes JOIN items ON items.id=closes.pr WHERE issue=? AND state='OPEN'",
            item,
        ):
            author, number_, created = self.q(
                "SELECT author, number, created FROM items WHERE id=?", pr
            )[0]
            times = [parse(created)] + [
                parse(a) for (a,) in self.q("SELECT at FROM activity WHERE pr=?", pr)
            ]
            prs.append(LinkedPR(number_, author, times))
        assigned = self.q("SELECT login, at FROM assignees WHERE item=? ORDER BY at", item)
        return Issue(
            number,
            self._labels(item),
            [a for a, _ in assigned],
            comments,
            prs,
            {a: parse(t) for a, t in assigned},
            self._open_blockers(item),
            self.q("SELECT state FROM items WHERE id=?", item)[0][0] != "OPEN",
        )

    def _labels(self, item: str) -> set[str]:
        return {n for (n,) in self.q("SELECT name FROM labels WHERE item=?", item)}

    def _open_blockers(self, item: str) -> int:
        return self.q(
            "SELECT count(*) FROM deps JOIN items ON items.id=deps.blocker "
            "WHERE blocked=? AND state='OPEN'",
            item,
        )[0][0]

    def issues_commented_since(self, repo: str, since: datetime) -> list[int]:
        return [
            n
            for (n,) in self.q(
                "SELECT DISTINCT number FROM items JOIN comments ON comments.item=items.id "
                "WHERE repo=? AND kind='issue' AND state='OPEN' AND comments.created>=? "
                "ORDER BY number",
                repo,
                iso(since),
            )
        ]

    def claimed_issues(self, repo: str) -> list[int]:
        return [
            n
            for (n,) in self.q(
                "SELECT number FROM items WHERE repo=? AND kind='issue' AND state='OPEN' AND ("
                "id IN (SELECT item FROM assignees) OR "
                "id IN (SELECT item FROM labels WHERE name='claimed')) ORDER BY number",
                repo,
            )
        ]

    def apply_issue(self, repo: str, number: int, action: tuple) -> None:
        kind, arg = action
        item = self.node(repo, number)
        if kind == "label_add":
            self.x("INSERT OR IGNORE INTO labels VALUES (?,?)", item, arg)
        elif kind == "label_remove":
            self.x("DELETE FROM labels WHERE item=? AND name=?", item, arg)
        elif kind == "assign":
            self.x("INSERT OR IGNORE INTO assignees VALUES (?,?,?)", item, arg, iso(self.now))
        elif kind == "unassign":
            self.x("DELETE FROM assignees WHERE item=? AND login=?", item, arg)
        elif kind == "react":
            self.x("INSERT INTO reactions VALUES (?,?,?)", arg, BOT, REACTION)
        elif kind == "comment":
            self.x(
                "INSERT INTO comments (item, author, assoc, body, created) VALUES (?,?,?,?,?)",
                item,
                BOT,
                "NONE",
                arg,
                iso(self.now),
            )
        self.log(BOT, f"{repo}#{number} {kind} {str(arg)[:60]}")

    def repos(self, org: str) -> list[str]:
        return [r for (r,) in self.q("SELECT DISTINCT repo FROM items ORDER BY repo")]

    # ---- board (same interface as GhBackend) --------------------------

    def items(self, org: str) -> list[Item]:
        out = []
        for row in self.q(
            "SELECT id, repo, number, kind, state, created, updated, closed, "
            "draft, in_queue, merge_state, checks, author, reviews, title "
            "FROM items ORDER BY repo, number"
        ):
            (
                iid,
                repo,
                number,
                kind,
                state,
                created,
                updated,
                closed,
                draft,
                in_queue,
                merge_state,
                checks,
                author,
                reviews,
                title,
            ) = row
            linked = (
                [
                    bool(q)
                    for (q,) in self.q(
                        "SELECT in_queue FROM closes JOIN items ON items.id=closes.pr "
                        "WHERE issue=? AND state='OPEN'",
                        iid,
                    )
                ]
                if kind == "issue"
                else []
            )
            out.append(
                Item(
                    iid,
                    repo,
                    number,
                    kind,
                    state,
                    parse(created),
                    parse(updated),
                    parse(closed),
                    self._labels(iid),
                    [a for (a,) in self.q("SELECT login FROM assignees WHERE item=?", iid)],
                    bool(draft),
                    bool(in_queue),
                    merge_state,
                    checks,
                    self._open_blockers(iid) if kind == "issue" else 0,
                    linked,
                    author,
                    reviews,
                    title,
                    self.q("SELECT count(*) FROM items WHERE parent=?", iid)[0][0],
                    not self.q("SELECT 1 FROM noqueue WHERE repo=?", repo),
                    next(iter(self.q("SELECT state FROM qstate WHERE item=?", iid)), ("",))[0]
                    if in_queue
                    else "",
                )
            )
        return out

    # ---- leases (same interface as GhBackend) -------------------------

    def create_lease(self, key: str, owner: str, now: datetime) -> bool:
        """Atomic like GitHub's create-ref: fails when the key exists."""
        try:
            self.x("INSERT INTO leases VALUES (?,?,?)", key, owner, iso(now))
            return True
        except sqlite3.IntegrityError:
            return False

    def leases(self) -> dict[str, tuple[str, datetime]]:
        return {k: (o, parse(a)) for k, o, a in self.q("SELECT key, owner, at FROM leases")}

    def delete_lease(self, key: str) -> None:
        self.x("DELETE FROM leases WHERE key=?", key)

    def check_counts(self, repo: str, number: int) -> tuple[int, int, int]:
        (checks,) = self.q("SELECT checks FROM items WHERE id=?", self.node(repo, number))[0]
        return {"SUCCESS": (1, 0, 0), "FAILURE": (0, 1, 0), "": (0, 0, 0)}.get(checks, (0, 0, 1))

    def red_branches(self, org: str) -> list[str]:
        return [r for (r,) in self.q("SELECT repo FROM red")]

    def board(self, org: str, number: int) -> Board:
        fields = {}
        for name, kind, options in self.q("SELECT name, type, options FROM fields"):
            opts = [o for o in options.split("|") if o]
            fields[name] = Field(f"F_{name}", name, kind, {o: f"O_{o}" for o in opts})
        values: dict[str, dict[str, str]] = {}
        for content, field, value in self.q("SELECT content, field, value FROM board_values"):
            values.setdefault(content, {})[field] = value
        item_ids = {c: f"PVTI_{c}" for (c,) in self.q("SELECT content FROM board")}
        return Board("PVT_sim", fields, item_ids, values)

    def ensure_fields(self, board: Board, ops: list[tuple]) -> None:
        for op in ops:
            if op[0] == "create_field":
                _, name, kind, options = op
                self.x("INSERT INTO fields VALUES (?,?,?)", name, kind, "|".join(options))
            else:
                _, name, lacking = op
                (current,) = self.q("SELECT options FROM fields WHERE name=?", name)[0]
                self.x(
                    "UPDATE fields SET options=? WHERE name=?",
                    "|".join([o for o in current.split("|") if o] + lacking),
                    name,
                )

    def apply_board(self, board: Board, ops: list[tuple]) -> dict[str, int]:
        counts = {"added": 0, "changed": 0, "failed": 0}
        for op in ops:
            if op[0] == "add":
                self.x("INSERT OR IGNORE INTO board VALUES (?)", op[1])
                counts["added"] += 1
            elif op[0] == "clear":
                self.x("DELETE FROM board_values WHERE content=? AND field=?", op[1], op[2])
                counts["changed"] += 1
            else:
                _, content, field, value = op
                if board.fields[field].type != "DATE" and value not in board.fields[field].options:
                    raise ValueError(f"{field} has no option {value!r}")
                self.x("INSERT OR REPLACE INTO board_values VALUES (?,?,?)", content, field, value)
                counts["changed"] += 1
        return counts


class World:
    """GitHub plus its Actions schedule, driving SimBackend through time."""

    SWEEP_EVERY = timedelta(hours=1)  # claim-bot cron
    SYNC_EVERY = timedelta(minutes=30)  # board-sync cron

    def __init__(self, path: str = ":memory:", org: str = ORG, verbose: bool = False) -> None:
        import workflow

        workflow.VERBOSE = verbose
        self.gh = SimBackend(path)
        self.org = org
        self._last_sweep = self._last_sync = self.gh.now

    @property
    def now(self) -> datetime:
        return self.gh.now

    def shutdown(self) -> None:
        self.gh.db.close()

    # ---- what people do -----------------------------------------------

    def _new(self, repo: str, kind: str, author: str, labels=()) -> int:
        number = self.gh.q("SELECT coalesce(max(number), 0) + 1 FROM items WHERE repo=?", repo)[0][
            0
        ]
        iid = f"{'I' if kind == 'issue' else 'PR'}_{repo}_{number}"
        now = iso(self.now)
        self.gh.x(
            "INSERT INTO items (id, repo, number, kind, state, author, created, updated) "
            "VALUES (?,?,?,?,?,?,?,?)",
            iid,
            repo,
            number,
            kind,
            "OPEN",
            author,
            now,
            now,
        )
        for name in labels:
            self.gh.x("INSERT INTO labels VALUES (?,?)", iid, name)
        self.gh.log(author, f"opened {kind} {repo}#{number}")
        return number

    def open_issue(self, repo: str, author: str = "reporter", labels=()) -> int:
        return self._new(repo, "issue", author, labels)

    def label(self, repo: str, number: int, *names: str, remove: bool = False) -> None:
        for name in names:
            self.gh.apply_issue(repo, number, ("label_remove" if remove else "label_add", name))

    def comment(
        self,
        repo: str,
        number: int,
        author: str,
        body: str,
        assoc: str = "CONTRIBUTOR",
        deliver: bool = True,
    ) -> int:
        """Post a comment and run the claim bot's issue_comment workflow
        (unless `deliver` is false: GitHub lost the event)."""
        from workflow import claim_bot

        item = self.gh.node(repo, number)
        cur = self.gh.db.execute(
            "INSERT INTO comments (item, author, assoc, body, created) VALUES (?,?,?,?,?)",
            (item, author, assoc, body, iso(self.now)),
        )
        self.gh.db.commit()
        self.gh.touch(item)
        self.gh.log(author, f"commented on {repo}#{number}: {body.splitlines()[0][:60]}")
        kind = self.gh.q("SELECT kind FROM items WHERE id=?", item)[0][0]
        event = {
            "issue": {"number": number, "labels": [{"name": n} for n in self.gh._labels(item)]},
            "comment": {
                "id": cur.lastrowid,
                "user": {"login": author},
                "author_association": assoc,
                "body": body,
                "created_at": iso(self.now),
            },
        }
        if kind == "pr":
            event["issue"]["pull_request"] = {}
        if deliver:
            claim_bot.on_comment(self.gh, repo, event, self.now)
        return cur.lastrowid

    def open_pr(self, repo: str, author: str, closes=(), draft: bool = False) -> int:
        number = self._new(repo, "pr", author)
        pr = self.gh.node(repo, number)
        self.gh.x("UPDATE items SET draft=? WHERE id=?", int(draft), pr)
        for issue in closes:
            self.gh.x("INSERT INTO closes VALUES (?,?)", pr, self.gh.node(repo, issue))
        return number

    def push(self, repo: str, pr: int) -> None:
        item = self.gh.node(repo, pr)
        self.gh.x("INSERT INTO activity VALUES (?,?)", item, iso(self.now))
        self.gh.x("UPDATE items SET checks='PENDING', merge_state='BLOCKED' WHERE id=?", item)
        self.gh.touch(item)

    def ci(self, repo: str, pr: int, passed: bool, mergeable: bool = True) -> None:
        item = self.gh.node(repo, pr)
        self.gh.x(
            "UPDATE items SET checks=?, merge_state=? WHERE id=?",
            "SUCCESS" if passed else "FAILURE",
            ("CLEAN" if mergeable else "DIRTY") if passed else "BLOCKED",
            item,
        )

    def no_ci(self, repo: str, pr: int) -> None:
        """A repo with no PR workflows: no rollup ever reports."""
        self.gh.x(
            "UPDATE items SET checks='', merge_state='CLEAN' WHERE id=?", self.gh.node(repo, pr)
        )

    def review(self, repo: str, pr: int) -> None:
        self.gh.x("UPDATE items SET reviews=reviews+1 WHERE id=?", self.gh.node(repo, pr))

    def no_merge_queue(self, repo: str) -> None:
        self.gh.x("INSERT OR IGNORE INTO noqueue VALUES (?)", repo)

    def master(self, repo: str, red: bool) -> None:
        self.gh.x("DELETE FROM red WHERE repo=?", repo)
        if red:
            self.gh.x("INSERT INTO red VALUES (?)", repo)

    def ready_for_review(self, repo: str, pr: int) -> None:
        self.gh.x("UPDATE items SET draft=0 WHERE id=?", self.gh.node(repo, pr))

    def enqueue(self, repo: str, pr: int) -> None:
        self.gh.x("UPDATE items SET in_queue=1 WHERE id=?", self.gh.node(repo, pr))

    def queue_fails(self, repo: str, pr: int) -> None:
        """Its merge group failed: GitHub marks the entry UNMERGEABLE."""
        self.gh.x("INSERT OR REPLACE INTO qstate VALUES (?, 'UNMERGEABLE')", self.gh.node(repo, pr))

    def eject(self, repo: str, pr: int, conflict: bool = True) -> None:
        self.gh.x(
            "UPDATE items SET in_queue=0, merge_state=? WHERE id=?",
            "DIRTY" if conflict else "BLOCKED",
            self.gh.node(repo, pr),
        )

    def merge(self, repo: str, pr: int) -> None:
        """Merge a PR; the issues it closes close with it, like `Fixes #N`."""
        item = self.gh.node(repo, pr)
        now = iso(self.now)
        self.gh.x(
            "UPDATE items SET state='MERGED', in_queue=0, closed=?, updated=? WHERE id=?",
            now,
            now,
            item,
        )
        for (issue,) in self.gh.q("SELECT issue FROM closes WHERE pr=?", item):
            self.gh.x(
                "UPDATE items SET state='CLOSED', closed=?, updated=? WHERE id=?", now, now, issue
            )
        self.gh.log("merge-queue", f"merged {repo}#{pr}")

    def close(self, repo: str, number: int) -> None:
        now = iso(self.now)
        self.gh.x(
            "UPDATE items SET state='CLOSED', closed=?, updated=? WHERE id=?",
            now,
            now,
            self.gh.node(repo, number),
        )

    def sub_issue(self, repo: str, parent: int, child: int) -> None:
        self.gh.x(
            "UPDATE items SET parent=? WHERE id=?",
            self.gh.node(repo, parent),
            self.gh.node(repo, child),
        )

    def block(self, repo: str, blocked: int, by: int) -> None:
        self.gh.x(
            "INSERT INTO deps VALUES (?,?)", self.gh.node(repo, by), self.gh.node(repo, blocked)
        )

    # ---- what Actions does --------------------------------------------

    def advance(self, hours: float = 0, minutes: float = 0) -> None:
        """Move the clock, running each cron job whenever it comes due."""
        from workflow import board_sync, claim_bot

        end = self.now + timedelta(hours=hours, minutes=minutes)
        while True:
            due_sweep = self._last_sweep + self.SWEEP_EVERY
            due_sync = self._last_sync + self.SYNC_EVERY
            nxt = min(due_sweep, due_sync)
            if nxt > end:
                break
            self.gh.x("UPDATE clock SET now=?", iso(nxt))
            if nxt == due_sweep:
                for repo in self._repos():
                    claim_bot.sweep(self.gh, repo, self.now)
                self._last_sweep = nxt
            if nxt == due_sync:
                board_sync.sync(self.gh, self.org, 1, self.now)
                self._last_sync = nxt
        self.gh.x("UPDATE clock SET now=?", iso(end))

    def sync(self) -> None:
        """Run the board sync now (a workflow_dispatch)."""
        from workflow import board_sync

        board_sync.sync(self.gh, self.org, 1, self.now)

    def _repos(self) -> list[str]:
        return [r for (r,) in self.gh.q("SELECT DISTINCT repo FROM items")]

    # ---- what people see ----------------------------------------------

    def status(self, repo: str, number: int, field: str = "Status") -> str | None:
        rows = self.gh.q(
            "SELECT value FROM board_values WHERE content=? AND field=?",
            self.gh.node(repo, number),
            field,
        )
        return rows[0][0] if rows else None

    def assignees(self, repo: str, number: int) -> list[str]:
        return self.gh.issue(repo, number).assignees

    def labels(self, repo: str, number: int) -> set[str]:
        return self.gh._labels(self.gh.node(repo, number))

    def bot_comments(self, repo: str, number: int) -> list[str]:
        return [
            b
            for (b,) in self.gh.q(
                "SELECT body FROM comments WHERE item=? AND author=?",
                self.gh.node(repo, number),
                BOT,
            )
        ]
