"""The search index of the second brain (INTERFACES.md, 8).

SQLite ARCHIVE/cache/brain-index.sqlite: `chunks` (the unit of search: the
facts of a note, or a window of paragraphs of the transcript), `chunks_fts`
(FTS5, BM25) and `vectors` (float16, for the cosine search). `update` is
incremental per (session, kind); `search` fuses BM25 and cosine with RRF.

Names in the chunks are the CURRENT ones (omy.brain.names): after a rename the
text of the affected chunks changes, so their hash changes and `update`
rebuilds them.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import threading
import unicodedata
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

import numpy as np

from omy.brain import NOTES, embed, names
from omy.config import ARCHIVE
from omy.diarize import load_people, my_name

INDEX_PATH = ARCHIVE / "cache" / "brain-index.sqlite"
EMBED_BATCH = 32
WINDOW_CHARS = 600
TOP = 50
RRF_K = 60
FTS_LIMIT = 1000

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, session TEXT NOT NULL, day TEXT, start TEXT, t REAL, row_id TEXT,
  persons TEXT NOT NULL DEFAULT '[]', title TEXT, field TEXT, text TEXT NOT NULL, source_hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_session ON chunks(session, kind);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, title, content='chunks', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS vectors(chunk_id INTEGER PRIMARY KEY, vec BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""
COLUMNS = ("id", "kind", "session", "day", "start", "t", "row_id", "persons", "title", "field", "text")

STOPWORDS = set("""
a ad al allo ai agli all alla alle che chi ci col con da dal dallo dai dagli dall dalla dalle de del dello dei
degli dell della delle di e ed gli ha hai ho i il in io la le lei lo loro lui ma me mi ne nei negli nel nello
nell nella nelle no non o per piu poi quale quando quanto quel quello questa queste questi questo se si sono
su sul sullo sui sugli sull sulla sulle ti tra fra tu un una uno vi mai cosa come dove detto parlato ho ha
abbiamo hanno era ero sei siamo siete sia sto sta stai essere stato stata anche cosi ancora
""".split())

_log_lock = threading.Lock()
_warned = False


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} index: {msg}", file=sys.stderr, flush=True)


def _warn_once(msg: str) -> None:
    global _warned
    with _log_lock:
        if not _warned:
            _warned = True
            log(msg)


def _rearm_warning() -> None:
    global _warned
    _warned = False


def _connect(create: bool = True) -> sqlite3.Connection:
    if create:
        INDEX_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(INDEX_PATH, timeout=60)
    db.row_factory = sqlite3.Row
    if create:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('version', '0')")
        db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('uid', ?)", (uuid.uuid4().hex,))
        db.commit()
    return db


def _meta(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


# --- reading the archive -------------------------------------------------------------------

def _hm(iso: str | None) -> str:
    return iso[11:16] if iso and len(iso) >= 16 else ""


def _row_t(row_id: str | None) -> float | None:
    """Seconds from the start of the session: the row id is "<session>/<ms>"."""
    try:
        return int(str(row_id).rsplit("/", 1)[1]) / 1000.0
    except (IndexError, ValueError):
        return None


def _day_files(sessions: list[str] | None) -> list[Path]:
    tr = ARCHIVE / "transcripts"
    if sessions is None:
        return sorted(tr.glob("*.jsonl")) if tr.is_dir() else []
    from omy.brain import organize
    out: dict[Path, None] = {}
    for sid in sessions:
        for day in organize._candidate_days(sid):
            f = tr / f"{day}.jsonl"
            if f.is_file():
                out[f] = None
    return list(out)


def _read_rows(sessions: list[str] | None) -> dict[str, list[dict]]:
    """session -> its rows of the day files, in time order."""
    want = None if sessions is None else set(sessions)
    out: dict[str, list[dict]] = {}
    for f in _day_files(sessions):
        for ln in f.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            sid = r.get("session")
            if sid and r.get("start") and (want is None or sid in want):
                out.setdefault(sid, []).append(r)
    for rows in out.values():
        rows.sort(key=lambda r: r["start"])
    return out


def _read_sidecars(sessions: list[str] | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if sessions is None:
        paths = sorted(NOTES.glob("*/*/*/*.json")) if NOTES.is_dir() else []
    else:
        from omy.brain import organize
        paths = [p for p in (organize.note_path(s) for s in sessions) if p]
    for p in paths:
        try:
            sc = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(sc, dict) and sc.get("id"):
            out[sc["id"]] = sc
    return out


# --- building the chunks --------------------------------------------------------------------

def _chunk(kind, field, text, *, start, t, row_id, persons, title, day) -> dict:
    return {"kind": kind, "field": field, "text": text, "start": start, "t": t, "row_id": row_id,
            "persons": sorted(p for p in persons if p), "title": title, "day": day}


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def _row_name(r: dict, people: dict, me: str) -> str:
    if (r.get("via") or {}).get("kind") == "media":
        return "[media]"
    return names.current_name(r.get("person"), r.get("speaker"), people, me) or "?"


def _session_title(rows: list[dict], people: dict, me: str) -> str:
    who: list[str] = []
    for r in rows:
        n = _row_name(r, people, me)
        if n != "[media]" and n not in who:
            who.append(n)
    first = _hm(rows[0]["start"]) if rows else ""
    return f"{first} · {', '.join(who)}" if who else first


def _note_chunks(sc: dict, rows: list[dict], people: dict, me: str) -> list[dict]:
    n = names.Names(sc, people, me)
    rw = lambda x: _clean(n.rewrite(x))  # noqa: E731
    day = sc.get("data") or (rows[0]["start"][:10] if rows else None)
    paras = sc.get("paragrafi") or {}
    session_ids = [p["person"] for p in n.participants if p.get("person")]
    title = rw(sc.get("titolo")) or (_session_title(rows, people, me) if rows else _hm(sc.get("ora_inizio")))

    def para(refs) -> dict:
        for r in sorted(refs or [], key=lambda x: int(x[1:]) if re.fullmatch(r"P\d+", str(x)) else 10 ** 9):
            if r in paras:
                return paras[r]
        first = min(paras, key=lambda x: int(x[1:]) if re.fullmatch(r"P\d+", x) else 10 ** 9) if paras else None
        return paras.get(first) or {}

    def mk(field, text, refs, persons=None):
        text = _clean(text)
        if not text:
            return None
        p = para(refs)
        row_id, start = p.get("riga"), p.get("inizio") or sc.get("ora_inizio")
        t = _row_t(row_id)
        if t is None and start and sc.get("ora_inizio"):
            try:
                t = (datetime.fromisoformat(start) - datetime.fromisoformat(sc["ora_inizio"])).total_seconds()
            except ValueError:
                t = 0.0
        pers = set(session_ids) | n.person_ids() if persons is None else persons
        return _chunk("nota", field, text, start=start, t=t, row_id=row_id, persons=pers, title=title, day=day)

    out = [mk("riassunto", rw(sc.get("riassunto")), None)]
    for pk in sc.get("punti_chiave") or []:
        out.append(mk("punto", rw(pk.get("testo")), pk.get("refs")))
    for it in sc.get("impegni_e_todo") or []:
        chi, verso = _clean(n.chi(it)), _clean(n.verso_chi(it))
        cosa = rw(it.get("cosa"))
        text = f"{chi} → {verso}: {cosa}" if verso else f"{chi}: {cosa}"
        if it.get("scadenza_testo") or it.get("scadenza"):
            text += f" (entro {it.get('scadenza_testo') or it.get('scadenza')})"
        pers = {it.get("chi_id"), it.get("verso_chi_id")}
        out.append(mk("impegno", text if cosa else "", it.get("refs"), pers if any(pers) else None))
    for d in sc.get("date_ed_eventi_citati") or []:
        ev = rw(d.get("evento"))
        text = f"{_clean(d.get('testo_data'))}: {ev}" + (f" ({d['data']})" if d.get("data") else "")
        out.append(mk("data", text if ev else "", d.get("refs")))
    ent = sc.get("entita") or {}
    parts = []
    for label, key in (("Persone", "persone"), ("Luoghi", "luoghi"), ("Organizzazioni", "organizzazioni"),
                       ("Progetti", "progetti")):
        items = []
        for e in ent.get(key) or []:
            nm = (names.current_name(e.get("person_id"), n.rewrite(e.get("nome")), people, me)
                  if key == "persone" else e.get("nome"))
            if nm and nm not in items:
                items.append(_clean(nm))
        if items:
            parts.append(f"{label}: {', '.join(items)}")
    if parts:
        out.append(mk("entita", ". ".join(parts), None))
    return [c for c in out if c]


def _text_chunks(rows: list[dict], people: dict, me: str, day: str | None = None) -> list[dict]:
    title = _session_title(rows, people, me)
    out: list[dict] = []
    cur: list[str] = []
    first: dict | None = None
    pers: set[str] = set()
    size = 0

    def flush():
        nonlocal cur, first, pers, size
        if cur and first:
            row_id = first.get("id")
            out.append(_chunk("testo", "testo", "\n".join(cur), start=first["start"], t=_row_t(row_id),
                              row_id=row_id, persons=pers, title=title, day=first["start"][:10]))
        cur, first, pers, size = [], None, set(), 0

    for r in rows:
        text = _clean(r.get("text"))
        if not text:
            continue
        line = f"{_hm(r['start'])} {_row_name(r, people, me)}: {text}" if _row_name(r, people, me) != "[media]" \
            else f"{_hm(r['start'])} [media]: {text}"
        if cur and size + len(line) + 1 > WINDOW_CHARS:
            flush()
        if first is None:
            first = r
        cur.append(line)
        size += len(line) + 1
        if r.get("person"):
            pers.add(r["person"])
    flush()
    return out


def _group_hash(chunks: list[dict]) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(json.dumps([c["field"], c["text"], c["start"], c["t"], c["row_id"], c["persons"], c["title"],
                             c["day"]], ensure_ascii=False).encode())
    return h.hexdigest()


# --- update ------------------------------------------------------------------------------------

def _delete_group(db: sqlite3.Connection, session: str, kind: str) -> int:
    rows = db.execute("SELECT id, text, title FROM chunks WHERE session=? AND kind=?", (session, kind)).fetchall()
    for r in rows:
        db.execute("INSERT INTO chunks_fts(chunks_fts, rowid, text, title) VALUES('delete', ?, ?, ?)",
                   (r["id"], r["text"], r["title"]))
        db.execute("DELETE FROM vectors WHERE chunk_id=?", (r["id"],))
    db.execute("DELETE FROM chunks WHERE session=? AND kind=?", (session, kind))
    return len(rows)


def _insert(db: sqlite3.Connection, session: str, chunks: list[dict], source_hash: str,
            reuse: dict[str, bytes]) -> int:
    for c in chunks:
        cur = db.execute(
            "INSERT INTO chunks(kind, session, day, start, t, row_id, persons, title, field, text, source_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (c["kind"], session, c["day"], c["start"], c["t"], c["row_id"], json.dumps(c["persons"]),
             c["title"], c["field"], c["text"], source_hash))
        db.execute("INSERT INTO chunks_fts(rowid, text, title) VALUES (?,?,?)", (cur.lastrowid, c["text"], c["title"]))
        vec = reuse.get(c["text"])
        if vec is not None:
            db.execute("INSERT OR REPLACE INTO vectors(chunk_id, vec) VALUES (?,?)", (cur.lastrowid, vec))
    return len(chunks)


def _embed_missing(db: sqlite3.Connection) -> tuple[int, int]:
    """Embed the chunks without a vector, EMBED_BATCH at a time. Returns
    (embedded, still missing): a failing server leaves them for the next update."""
    todo = db.execute("SELECT id, text FROM chunks WHERE id NOT IN (SELECT chunk_id FROM vectors)").fetchall()
    done = 0
    for i in range(0, len(todo), EMBED_BATCH):
        batch = todo[i:i + EMBED_BATCH]
        try:
            vecs = embed.embed([r["text"] for r in batch], query=False)
        except embed.EmbedError as e:
            log(f"embedding failed, {len(todo) - done} chunks left without a vector: {e}")
            break
        with db:
            for r, v in zip(batch, vecs):
                db.execute("INSERT OR REPLACE INTO vectors(chunk_id, vec) VALUES (?,?)",
                           (r["id"], v.astype(np.float16).tobytes()))
        done += len(batch)
    return done, len(todo) - done


def update(sessions: list[str] | None = None) -> dict:
    """Bring the index up to date with the sidecars and the JSONL rows of the
    given sessions (all if None). Returns counters."""
    people, me = load_people(), my_name()
    rows_by = _read_rows(sessions)
    notes_by = _read_sidecars(sessions)
    stats = {"sessions": 0, "rebuilt": 0, "unchanged": 0, "removed": 0, "embedded": 0, "pending": 0}
    with closing(_connect()) as db:
        known = {r["session"] for r in db.execute("SELECT DISTINCT session FROM chunks")}
        found = set(rows_by) | set(notes_by)
        targets = (found | known) if sessions is None else set(sessions)
        stats["sessions"] = len(targets & found)
        changed = False
        for sid in sorted(targets):
            wanted: dict[str, list[dict]] = {"nota": [], "testo": []}
            rows = rows_by.get(sid, [])
            if sid in notes_by:
                wanted["nota"] = _note_chunks(notes_by[sid], rows, people, me)
            if rows:
                wanted["testo"] = _text_chunks(rows, people, me)
            for kind, chunks in wanted.items():
                old = db.execute("SELECT DISTINCT source_hash FROM chunks WHERE session=? AND kind=?",
                                 (sid, kind)).fetchall()
                new_hash = _group_hash(chunks) if chunks else None
                if (not old and not chunks) or (len(old) == 1 and old[0][0] == new_hash):
                    stats["unchanged"] += bool(chunks)
                    continue
                with db:
                    reuse = {r["text"]: r["vec"] for r in db.execute(
                        "SELECT c.text, v.vec FROM chunks c JOIN vectors v ON v.chunk_id=c.id "
                        "WHERE c.session=? AND c.kind=?", (sid, kind))}
                    removed = _delete_group(db, sid, kind)
                    if chunks:
                        _insert(db, sid, chunks, new_hash, reuse)
                        stats["rebuilt"] += 1
                    else:
                        stats["removed"] += removed
                changed = True
        embedded, pending = _embed_missing(db)
        stats["embedded"], stats["pending"] = embedded, pending
        if embedded:
            changed = True
        with db:
            db.execute("UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='version'")
        stats["version"] = int(_meta(db, "version") or 0)
        stats["changed"] = changed
    return stats


# --- search ----------------------------------------------------------------------------------------

_cache: dict = {}
_cache_lock = threading.Lock()


def _load(db: sqlite3.Connection) -> dict:
    """The in-memory view of the index (all chunks, and the float32 matrix of
    the vectors), reloaded when meta.version (or the database) changes."""
    key = (str(INDEX_PATH), _meta(db, "uid"), _meta(db, "version"))
    with _cache_lock:
        if _cache.get("key") == key:
            return _cache
        rows = {}
        for r in db.execute(f"SELECT {', '.join(COLUMNS)} FROM chunks"):
            d = dict(r)
            d["persons"] = json.loads(d["persons"] or "[]")
            rows[d["id"]] = d
        ids, blobs = [], []
        for r in db.execute("SELECT chunk_id, vec FROM vectors"):
            if r["chunk_id"] in rows:
                ids.append(r["chunk_id"])
                blobs.append(np.frombuffer(r["vec"], dtype=np.float16))
        mat = (np.vstack(blobs).astype(np.float32) if blobs
               else np.zeros((0, embed.EMBED_DIM), dtype=np.float32))
        _cache.clear()
        _cache.update(key=key, rows=rows, ids=np.asarray(ids, dtype=np.int64), mat=mat)
        _loads[0] += 1
        return _cache


_loads = [0]  # how many times the view was (re)built, for the tests


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFD", text.casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


def fts_query(question: str) -> str | None:
    """The question's words, quoted and OR-ed: stopwords dropped (unless that
    leaves nothing), long words matched as prefixes (trasloco ~ traslochi)."""
    words = re.findall(r"\w+", _fold(question))
    terms = [w for w in words if w not in STOPWORDS] or words
    terms = [f'"{w}"*' if len(w) >= 5 else f'"{w}"' for w in dict.fromkeys(terms)]
    return " OR ".join(terms) if terms else None


def _person_names(persons: list[str]) -> list[str]:
    people, me = load_people(), my_name()
    out = []
    for p in persons:
        nm = names.current_name(p, None, people, me)
        if nm:
            out.append(_fold(nm))
        if p == "me":
            out += [_fold(a) for a in names.my_aliases()]
    return [n for n in out if n]


def search(query: str, *, day_from: str | None = None, day_to: str | None = None,
           persons: list[str] | None = None, k: int = 8) -> list[dict]:
    """Hybrid search (BM25 + cosine, RRF k=60). Hit = the chunk's fields plus `score`, `lexical`
    (matched by the full-text query) and `cos` (cosine similarity, None when not in the vector top)."""
    if not INDEX_PATH.is_file():
        return []
    with closing(_connect(create=False)) as db:
        view = _load(db)
        rows = view["rows"]
        allowed: set[int] | None = None
        if day_from or day_to or persons:
            pnames = _person_names(persons) if persons else []
            allowed = set()
            for cid, c in rows.items():
                if (day_from and (c["day"] or "") < day_from) or (day_to and (c["day"] or "") > day_to):
                    continue
                if persons and not (set(persons) & set(c["persons"])
                                    or any(n in _fold(c["text"]) for n in pnames)):
                    continue
                allowed.add(cid)

        lists: list[list[int]] = []
        lexical: set[int] = set()
        cos: dict[int, float] = {}
        q = fts_query(query)
        if q:
            try:
                found = [r[0] for r in db.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
                    (q, FTS_LIMIT))]
            except sqlite3.OperationalError as e:
                log(f"fts query failed: {e}")
                found = []
            lists.append([i for i in found if allowed is None or i in allowed][:TOP])
            lexical.update(lists[-1])

        if len(view["ids"]):
            try:
                qv = embed.embed([query], query=True)[0]
            except embed.EmbedError as e:
                _warn_once(f"embedding server unavailable, BM25 only: {e}")
            else:
                _rearm_warning()
                scores = view["mat"] @ qv
                ids = view["ids"]
                if allowed is not None:
                    mask = np.fromiter((int(i) in allowed for i in ids), dtype=bool, count=len(ids))
                    scores = np.where(mask, scores, -np.inf)
                order = np.argsort(-scores)[:TOP]
                lists.append([int(ids[i]) for i in order if np.isfinite(scores[i])])
                cos.update({int(ids[i]): float(scores[i]) for i in order if np.isfinite(scores[i])})

        fused: dict[int, float] = {}
        for lst in lists:
            for rank, cid in enumerate(lst):
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
        best = sorted(fused, key=lambda i: (-fused[i], rows[i]["kind"] != "nota", i))[:k]
        return [{**rows[i], "persons": list(rows[i]["persons"]), "score": fused[i],
                 "lexical": i in lexical, "cos": cos.get(i)} for i in best]
