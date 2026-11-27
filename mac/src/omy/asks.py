"""The questions of "Chiedi" that the hub keeps for the Mac (docs/hub-migration.md, decision 2): with the Mac off
a question is QUEUED, not refused. The iPhone shows it as waiting for the Mac, and the answer is stored here when
the Mac comes back and generates it (the compute worker takes asks with GET /compute/v1/ask/next).

One table in the hub's jobs database (backed up with it). Questions older than EXPIRE_S are dropped: nobody wants
the answer to yesterday's question when the Mac wakes up for the night.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import closing

from omy import jobs

LEASE_S = 180.0          # an ask the Mac took and did not finish for this long is given to the next poll
EXPIRE_S = 24 * 3600.0
KEEP_S = 3 * 24 * 3600.0  # answered or failed asks stay listed for the iPhone this long
STATES = ("pending", "leased", "done", "failed", "expired", "cancelled")


def _row(r) -> dict:
    d = dict(r)
    for k in ("history", "citations", "used"):
        d[k] = json.loads(d[k] or "[]")
    d["not_found"] = bool(d["not_found"])
    return d


def create(question: str, history: list, asked_at: str | None = None) -> str:
    ask_id = uuid.uuid4().hex[:16]
    with closing(jobs._connect()) as db:
        db.execute("INSERT INTO asks(id, question, history, asked_at, created) VALUES (?,?,?,?,?)",
                   (ask_id, question, json.dumps(history, ensure_ascii=False), asked_at, time.time()))
    return ask_id


def get(ask_id: str) -> dict | None:
    with closing(jobs._connect()) as db:
        r = db.execute("SELECT * FROM asks WHERE id=?", (ask_id,)).fetchone()
    return _row(r) if r else None


def recent(limit: int = 50) -> list[dict]:
    """The asks of the last KEEP_S, newest first: what the iPhone lists (waiting ones and the answers it has not seen)."""
    with closing(jobs._connect()) as db:
        rows = db.execute("SELECT * FROM asks WHERE created>? ORDER BY created DESC LIMIT ?",
                          (time.time() - KEEP_S, limit)).fetchall()
    return [_row(r) for r in rows]


def next_for(worker: str, now: float | None = None) -> dict | None:
    """The oldest ask nobody is answering (pending, or leased and not renewed), leased to this worker."""
    now = time.time() if now is None else now
    with closing(jobs._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            r = db.execute("SELECT * FROM asks WHERE (state='pending' OR (state='leased' AND lease_until<?)) "
                           "AND created>? ORDER BY created LIMIT 1", (now, now - EXPIRE_S)).fetchone()
            if r is None:
                db.execute("COMMIT")
                return None
            lease_id = uuid.uuid4().hex
            # a retried ask starts from scratch: the partial text of the dead attempt is dropped
            db.execute("UPDATE asks SET state='leased', lease_id=?, lease_until=?, worker=?, answer='' WHERE id=?",
                       (lease_id, now + LEASE_S, worker, r["id"]))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return get(r["id"])


def holds(ask_id: str, lease_id: str) -> bool:
    with closing(jobs._connect()) as db:
        return db.execute("SELECT 1 FROM asks WHERE id=? AND lease_id=? AND state='leased'",
                          (ask_id, lease_id)).fetchone() is not None


def renew(ask_id: str, lease_id: str, now: float | None = None) -> bool:
    with closing(jobs._connect()) as db:
        cur = db.execute("UPDATE asks SET lease_until=? WHERE id=? AND lease_id=? AND state='leased'",
                         ((time.time() if now is None else now) + LEASE_S, ask_id, lease_id))
    return cur.rowcount == 1


def append(ask_id: str, lease_id: str, text: str) -> bool:
    with closing(jobs._connect()) as db:
        cur = db.execute("UPDATE asks SET answer=answer||?, lease_until=? WHERE id=? AND lease_id=? AND state='leased'",
                         (text, time.time() + LEASE_S, ask_id, lease_id))
    return cur.rowcount == 1


def finish(ask_id: str, lease_id: str, *, citations: list | None = None, used: list | None = None,
           not_found: bool = False, answer: str | None = None) -> bool:
    with closing(jobs._connect()) as db:
        cur = db.execute(
            "UPDATE asks SET state='done', finished=?, citations=COALESCE(?, citations), used=?, not_found=?, "
            "answer=COALESCE(?, answer), lease_until=NULL WHERE id=? AND lease_id=? AND state='leased'",
            (time.time(), json.dumps(citations, ensure_ascii=False) if citations is not None else None,
             json.dumps(used or []), int(not_found), answer, ask_id, lease_id))
    return cur.rowcount == 1


def set_citations(ask_id: str, lease_id: str, citations: list) -> None:
    with closing(jobs._connect()) as db:
        db.execute("UPDATE asks SET citations=? WHERE id=? AND lease_id=? AND state='leased'",
                   (json.dumps(citations, ensure_ascii=False), ask_id, lease_id))


def fail(ask_id: str, lease_id: str, error: str) -> bool:
    with closing(jobs._connect()) as db:
        cur = db.execute("UPDATE asks SET state='failed', finished=?, error=?, lease_until=NULL "
                         "WHERE id=? AND lease_id=? AND state='leased'", (time.time(), error[:500], ask_id, lease_id))
    return cur.rowcount == 1


def cancel(ask_id: str) -> bool:
    with closing(jobs._connect()) as db:
        cur = db.execute("UPDATE asks SET state='cancelled', finished=?, lease_until=NULL "
                         "WHERE id=? AND state IN ('pending','leased')", (time.time(), ask_id))
    return cur.rowcount == 1


def expire(now: float | None = None) -> int:
    """The questions nobody answered in EXPIRE_S are dropped; the old finished ones are deleted."""
    now = time.time() if now is None else now
    with closing(jobs._connect()) as db:
        cur = db.execute("UPDATE asks SET state='expired', finished=?, lease_until=NULL "
                         "WHERE state IN ('pending','leased') AND created<?", (now, now - EXPIRE_S))
        db.execute("DELETE FROM asks WHERE finished IS NOT NULL AND finished<?", (now - KEEP_S,))
    return cur.rowcount


def summary() -> dict:
    """For the widget: how many are waiting for the Mac, and the oldest (the questions are not shown whole)."""
    with closing(jobs._connect()) as db:
        rows = db.execute("SELECT id, question, state, created FROM asks WHERE state IN ('pending','leased') "
                          "ORDER BY created LIMIT 20").fetchall()
    now = time.time()
    return {"waiting": sum(r["state"] == "pending" for r in rows), "answering": sum(r["state"] == "leased" for r in rows),
            "items": [{"id": r["id"], "question": (r["question"][:80] + "…") if len(r["question"]) > 80 else r["question"],
                       "state": r["state"], "age_min": round((now - r["created"]) / 60)} for r in rows]}
