"""The job queue of the worker: ARCHIVE/cache/jobs.sqlite.

One table of jobs (transcribe, diarize, render, organize, index) and one of
"demand" (when somebody last asked the chat model something, see omy.models).
Any process can enqueue (the CLI, the receiver); only the worker runs jobs.
A connection per call: the file may be deleted and recreated under us.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass

from omy.config import ARCHIVE

DB = ARCHIVE / "cache" / "jobs.sqlite"
WAKE_FILE = ARCHIVE / "cache" / "worker.wake"

PRIORITY = {"transcribe": 10, "diarize": 20, "render": 30, "organize": 40, "index": 50}
KINDS = tuple(PRIORITY)
MAX_ATTEMPTS = 3
BACKOFF_S = 60.0  # doubled at each attempt

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    not_before REAL NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    started REAL,
    finished REAL,
    error TEXT,
    force INTEGER NOT NULL DEFAULT 0,
    ignore_window INTEGER NOT NULL DEFAULT 0,
    rerun INTEGER NOT NULL DEFAULT 0,
    input_hash TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_queued ON jobs(kind, key) WHERE state = 'queued';
CREATE UNIQUE INDEX IF NOT EXISTS jobs_running ON jobs(kind, key) WHERE state = 'running';
CREATE INDEX IF NOT EXISTS jobs_pick ON jobs(state, priority, id);
CREATE TABLE IF NOT EXISTS demand (role TEXT PRIMARY KEY, last_at REAL NOT NULL);
"""


@dataclass
class Job:
    id: int
    kind: str
    key: str
    priority: int
    attempts: int
    force: bool
    started: float | None = None
    ignore_window: bool = False
    input_hash: str | None = None


def _connect() -> sqlite3.Connection:
    DB.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(SCHEMA)
    cols = {r["name"] for r in db.execute("PRAGMA table_info(jobs)")}
    for col, ddl in (("ignore_window", "INTEGER NOT NULL DEFAULT 0"), ("rerun", "INTEGER NOT NULL DEFAULT 0"),
                     ("input_hash", "TEXT")):
        if col not in cols:
            try:
                db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:  # another process added it first
                pass
    return db


def _job(row: sqlite3.Row) -> Job:
    return Job(row["id"], row["kind"], row["key"], row["priority"], row["attempts"], bool(row["force"]),
               row["started"], bool(row["ignore_window"]), row["input_hash"])


def get(job_id: int) -> Job | None:
    with closing(_connect()) as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _job(row) if row else None


def state_of(job_id: int) -> str | None:
    with closing(_connect()) as db:
        row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
    return row["state"] if row else None


def enqueue(kind: str, key: str, priority: int | None = None, force: bool = False,
            not_before: float = 0.0, ignore_window: bool = False, input_hash: str | None = None) -> int:
    """Queue a job; one already queued (or running, except render, which may
    have missed the change that is asking) is left alone, and returned. With
    force or ignore_window (force implies it), a queued job gets the flags;
    a running one gets them too and is run again once it has finished.
    Returns the job id."""
    if kind not in PRIORITY:
        raise ValueError(f"unknown job kind {kind!r}")
    prio = PRIORITY[kind] if priority is None else priority
    ignore_window = ignore_window or force
    with closing(_connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT id FROM jobs WHERE kind=? AND key=? AND state='queued'", (kind, key)).fetchone()
            if row:
                if force or ignore_window:
                    db.execute("UPDATE jobs SET force=MAX(force,?), ignore_window=MAX(ignore_window,?) WHERE id=?",
                               (int(force), int(ignore_window), row["id"]))
                db.execute("COMMIT")
                return row["id"]
            if kind != "render":
                row = db.execute("SELECT id, force, ignore_window FROM jobs WHERE kind=? AND key=? "
                                 "AND state='running'", (kind, key)).fetchone()
                if row:
                    if (force and not row["force"]) or (ignore_window and not row["ignore_window"]):
                        db.execute("UPDATE jobs SET force=MAX(force,?), ignore_window=MAX(ignore_window,?), "
                                   "rerun=1 WHERE id=?", (int(force), int(ignore_window), row["id"]))
                    db.execute("COMMIT")
                    return row["id"]
            cur = db.execute(
                "INSERT INTO jobs(kind, key, priority, created, not_before, force, ignore_window, input_hash) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (kind, key, prio, time.time(), not_before, int(force), int(ignore_window), input_hash))
            db.execute("COMMIT")
            return cur.lastrowid
        except BaseException:
            db.execute("ROLLBACK")
            raise


def next_job(allowed_kinds: list[str] | tuple[str, ...], now: float | None = None,
             forced_kinds: list[str] | tuple[str, ...] = ()) -> Job | None:
    """Claim (atomically: state running, attempts + 1) the best queued job of
    these kinds that is due, or None. Jobs of forced_kinds count only if they
    were enqueued with ignore_window (or force)."""
    if not allowed_kinds and not forced_kinds:
        return None
    now = time.time() if now is None else now
    marks = ",".join("?" * len(allowed_kinds))
    fmarks = ",".join("?" * len(forced_kinds))
    with closing(_connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                f"SELECT * FROM jobs WHERE state='queued' AND not_before<=? "
                f"AND (kind IN ({marks}) OR (ignore_window=1 AND kind IN ({fmarks}))) "
                "ORDER BY priority, id LIMIT 1", (now, *allowed_kinds, *forced_kinds)).fetchone()
            # A render queued behind the same render still running waits.
            if row is None or db.execute("SELECT 1 FROM jobs WHERE kind=? AND key=? AND state='running'",
                                         (row["kind"], row["key"])).fetchone():
                db.execute("COMMIT")
                return None
            db.execute("UPDATE jobs SET state='running', started=?, attempts=attempts+1, error=NULL WHERE id=?",
                       (now, row["id"]))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return get(row["id"])


def finish(job_id: int) -> None:
    """Done. If a forced run was asked for while it was running, queue it."""
    with closing(_connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        db.execute("UPDATE jobs SET state='done', finished=?, error=NULL, rerun=0 WHERE id=?",
                   (time.time(), job_id))
        if row and row["rerun"]:
            try:
                db.execute("INSERT INTO jobs(kind, key, priority, created, force, ignore_window, input_hash) "
                           "VALUES (?,?,?,?,?,?,?)", (row["kind"], row["key"], row["priority"], time.time(),
                                                      row["force"], row["ignore_window"], row["input_hash"]))
            except sqlite3.IntegrityError:
                pass
        db.execute("COMMIT")


def fail(job_id: int, error: str, retry_after: float | None = None) -> bool:
    """The job failed. Back in the queue after retry_after seconds (default:
    a backoff that doubles) unless it has used MAX_ATTEMPTS; returns whether
    it will be retried."""
    now = time.time()
    with closing(_connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        attempts = row["attempts"] if row else MAX_ATTEMPTS
        retry = attempts < MAX_ATTEMPTS
        if retry:
            wait = BACKOFF_S * 2 ** (attempts - 1) if retry_after is None else retry_after
            try:
                db.execute("UPDATE jobs SET state='queued', not_before=?, error=?, started=NULL, rerun=0 WHERE id=?",
                           (now + wait, error[-1000:], job_id))
            except sqlite3.IntegrityError:
                # The same job was queued meanwhile: this one is moot.
                retry = False
        if not retry:
            db.execute("UPDATE jobs SET state='failed', finished=?, error=? WHERE id=?", (now, error[-1000:], job_id))
        db.execute("COMMIT")
    return retry


def requeue(job_id: int, wait: float = 0.0) -> None:
    """Back in the queue after a preemption (or a busy lock), after wait
    seconds: attempts as before the claim."""
    with closing(_connect()) as db:
        try:
            db.execute("UPDATE jobs SET state='queued', started=NULL, attempts=MAX(attempts-1, 0), not_before=?, "
                       "rerun=0 WHERE id=? AND state='running'", (time.time() + wait if wait else 0, job_id))
        except sqlite3.IntegrityError:
            db.execute("UPDATE jobs SET state='done', finished=? WHERE id=?", (time.time(), job_id))


def reset_running() -> int:
    """At startup nothing is running: the jobs that were, go back in the queue
    with their attempts (one that crashed the worker must run out of them), or
    fail if they have none left."""
    with closing(_connect()) as db:
        rows = db.execute("SELECT id, attempts FROM jobs WHERE state='running'").fetchall()
        for r in rows:
            try:
                if r["attempts"] >= MAX_ATTEMPTS:
                    db.execute("UPDATE jobs SET state='failed', finished=?, error='the worker died while running it' "
                               "WHERE id=?", (time.time(), r["id"]))
                else:
                    db.execute("UPDATE jobs SET state='queued', started=NULL, not_before=0, rerun=0 WHERE id=?",
                               (r["id"],))
            except sqlite3.IntegrityError:
                db.execute("UPDATE jobs SET state='done', finished=? WHERE id=?", (time.time(), r["id"]))
    return len(rows)


def gave_up(kind: str, key: str, input_hash: str | None) -> bool:
    """Whether this kind and key failed for good (MAX_ATTEMPTS) on this same
    input, with nothing done since: not worth queueing again."""
    with closing(_connect()) as db:
        row = db.execute("SELECT id, attempts, input_hash FROM jobs WHERE kind=? AND key=? AND state IN "
                         "('failed','done') ORDER BY id DESC LIMIT 1", (kind, key)).fetchone()
        if row is None or row["attempts"] < MAX_ATTEMPTS:
            return False
        failed = db.execute("SELECT 1 FROM jobs WHERE id=? AND state='failed'", (row["id"],)).fetchone()
    return failed is not None and row["input_hash"] == input_hash


def pending(kinds: list[str] | tuple[str, ...]) -> int:
    """Jobs of these kinds queued or running."""
    marks = ",".join("?" * len(kinds))
    with closing(_connect()) as db:
        return db.execute(f"SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running') AND kind IN ({marks})",
                          tuple(kinds)).fetchone()[0]


def recent(kind: str, key: str, state: str, within_s: float) -> bool:
    """Whether a job of this kind and key reached this state in the last within_s."""
    with closing(_connect()) as db:
        return db.execute("SELECT 1 FROM jobs WHERE kind=? AND key=? AND state=? AND finished>?",
                          (kind, key, state, time.time() - within_s)).fetchone() is not None


def summary() -> dict:
    with closing(_connect()) as db:
        counts: dict[str, dict[str, int]] = {}
        for r in db.execute("SELECT kind, state, COUNT(*) n FROM jobs GROUP BY kind, state"):
            counts.setdefault(r["kind"], {})[r["state"]] = r["n"]
        running = [{"id": r["id"], "kind": r["kind"], "key": r["key"], "started": r["started"]}
                   for r in db.execute("SELECT * FROM jobs WHERE state='running'")]
        failed = [{"id": r["id"], "kind": r["kind"], "key": r["key"], "error": r["error"], "finished": r["finished"]}
                  for r in db.execute("SELECT * FROM jobs WHERE state='failed' ORDER BY finished DESC LIMIT 10")]
        queued = db.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0]
    return {"queued": queued, "by_kind": counts, "running": running, "failed": failed}


# --- demand (chat questions, see omy.models) ---------------------------------

def touch_demand(role: str = "chat", at: float | None = None) -> None:
    with closing(_connect()) as db:
        db.execute("INSERT INTO demand(role, last_at) VALUES (?, ?) "
                   "ON CONFLICT(role) DO UPDATE SET last_at=excluded.last_at",
                   (role, time.time() if at is None else at))


def demand_at(role: str = "chat") -> float | None:
    with closing(_connect()) as db:
        row = db.execute("SELECT last_at FROM demand WHERE role=?", (role,)).fetchone()
    return row["last_at"] if row else None


# --- waking the worker ---------------------------------------------------------

def wake() -> None:
    """Ask the worker to scan for work now (it also does every few minutes)."""
    try:
        WAKE_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        WAKE_FILE.touch()
        now = time.time()
        os.utime(WAKE_FILE, (now, now))
    except OSError:
        pass


def wake_time() -> float:
    try:
        return WAKE_FILE.stat().st_mtime
    except OSError:
        return 0.0
