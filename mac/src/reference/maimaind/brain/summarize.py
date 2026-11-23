"""Session summaries: what the model extracts from one conversation.

The model sees the paragraphs numbered P1…Pn (never the long ids) and returns
JSON constrained by a schema (llama-server turns it into a grammar, so the
JSON is always well formed). Then the code checks every item:
- refs must be paragraphs of the session; an item left without refs is dropped;
- media paragraphs may only back "heard", and "heard" only media paragraphs;
- who made a commitment is who spoke in the paragraph cited, not the model's guess;
- people are matched to the session's named people; anyone else stays a label
  ("Persona 1") and never becomes a person.
Long sessions are cut into windows (map), then one call writes the title and
summary from the windows' points (reduce).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from brain import dates
from brain.llm import LLM
from brain.tools import fold

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
PROMPT_VERSION = 1
# Characters of paragraphs per model call (~7,000 tokens): long sessions are cut.
WINDOW_CHARS = 24000
# Below this much text, a session is not worth a model call ("Sì." "Ok, ciao."):
# a promise fits in very few words ("prenoto io il volo"), so keep it low.
TRIVIAL_CHARS = 60
MAX_TOKENS = 2048

REF = {"type": "array", "items": {"type": "string", "pattern": "^P[0-9]+$"}, "maxItems": 8}


def _items(props: dict, required: list[str], most: int) -> dict:
    return {"type": "array", "maxItems": most, "items": {
        "type": "object", "properties": {**props, "refs": REF}, "required": [*required, "refs"]}}


NULLABLE = {"type": ["string", "null"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 80},
        "quality": {"type": "string", "enum": ["good", "partial", "noise"]},
        "summary": _items({"text": {"type": "string"}}, ["text"], 6),
        "topics": _items({"name": {"type": "string", "maxLength": 40}}, ["name"], 5),
        "commitments": _items({"who": {"type": "string"}, "to": NULLABLE, "what": {"type": "string"},
                               "due": NULLABLE, "due_text": NULLABLE}, ["who", "to", "what", "due", "due_text"], 8),
        "facts": _items({"about": {"type": "string"}, "fact": {"type": "string"}}, ["about", "fact"], 8),
        "heard": _items({"summary": {"type": "string"}}, ["summary"], 4),
    },
    "required": ["title", "quality", "summary", "topics", "commitments", "facts", "heard"],
}
REDUCE_SCHEMA = {
    "type": "object",
    "properties": {"title": SCHEMA["properties"]["title"], "quality": SCHEMA["properties"]["quality"],
                   "summary": SCHEMA["properties"]["summary"]},
    "required": ["title", "quality", "summary"],
}


class SummarizeError(Exception):
    pass


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(text)).strip("-")[:60] or "senza-nome"


def session_rows(conn: sqlite3.Connection, sid: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM paragraphs WHERE session = ? ORDER BY ms", (sid,)).fetchall()


def my_name(conn: sqlite3.Connection) -> str:
    me = conn.execute("SELECT name FROM people WHERE id = 'me'").fetchone()
    return me[0] if me else "Io"


def line(n: int, r: sqlite3.Row) -> str:
    who = r["speaker"] or "?"
    if r["channel"] == "heard":
        who = f"[MEDIA {r['via_app'] or 'Video'}]"
    elif r["via_kind"] == "call":
        who += " (in chiamata)"
    return f"[P{n}] {r['start'][11:16]} {who}: {r['text']}"


def windows(lines: list[str]) -> list[list[str]]:
    out, cur, size = [], [], 0
    for ln in lines:
        if cur and size + len(ln) > WINDOW_CHARS:
            out.append(cur)
            cur, size = [], 0
        cur.append(ln)
        size += len(ln)
    return out + ([cur] if cur else [])


def prompt(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> str:
    day = date.fromisoformat(rows[0]["day"])
    names = sorted({r["speaker"] for r in rows if r["person"] and r["person"] != "me" and r["speaker"]})
    return (PROMPTS / "session_extract.md").read_text().format(
        my_name=my_name(conn), weekday=dates.WEEKDAYS[day.weekday()], day=day.isoformat(),
        people=", ".join(names) or "nessuna oltre all'utente")


def call(llm: LLM, system: str, user: str, schema: dict) -> tuple[dict, dict]:
    r = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                 max_tokens=MAX_TOKENS, temperature=0.1,
                 response_format={"type": "json_schema", "json_schema": {"name": "estrazione", "schema": schema}})
    text = (r["message"].get("content") or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        raise SummarizeError(f"invalid JSON from the model: {text[:200]!r}")
    if not isinstance(data, dict):
        raise SummarizeError("the model did not return an object")
    return data, r["timings"]


# --- checks ----------------------------------------------------------------------------

class Checker:
    """Turns the model's JSON into items backed by paragraph ids, or drops them."""

    def __init__(self, conn: sqlite3.Connection, rows: list[sqlite3.Row]):
        self.rows = rows
        self.me = my_name(conn)
        # Names the session's named people go by, folded -> person id.
        self.people: dict[str, str] = {fold(self.me): "me", "io": "me", "me": "me"}
        for r in rows:
            if r["person"] and r["person"] != "me" and r["speaker"]:
                self.people[fold(re.sub(r"\s*\(.*\)$", "", r["speaker"]))] = r["person"]
        for p in conn.execute("SELECT id, name FROM people WHERE id != 'me'"):
            if any(r["person"] == p["id"] for r in rows):
                self.people[fold(p["name"])] = p["id"]
        self.dropped = 0

    def refs(self, item: dict, heard: bool) -> list[sqlite3.Row]:
        out = []
        for ref in item.get("refs") or []:
            m = re.fullmatch(r"P([0-9]+)", str(ref).strip())
            if not m or not 1 <= int(m.group(1)) <= len(self.rows):
                continue
            row = self.rows[int(m.group(1)) - 1]
            if (row["channel"] == "heard") == heard and row not in out:
                out.append(row)
        return out

    def person(self, name) -> str | None:
        if not isinstance(name, str):
            return None
        key = fold(re.sub(r"\s*\(.*\)$", "", name.strip()))
        if key in self.people:
            return self.people[key]
        matches = {pid for n, pid in self.people.items() if key and (n.startswith(key) or key.startswith(n))}
        return matches.pop() if len(matches) == 1 else None

    def keep(self, items, heard: bool = False):
        for item in items or []:
            if not isinstance(item, dict):
                self.dropped += 1
                continue
            rows = self.refs(item, heard)
            if not rows:
                self.dropped += 1
                continue
            yield item, rows


def _text(value, limit: int = 400) -> str:
    return " ".join(str(value or "").split())[:limit]


# What the model writes when there is nobody: "null" as a string, or the prompt's own placeholder.
NOBODY = {"", "null", "none", "nessuno", "n/a", "?", "persona n", "sconosciuto n"}


def label(value, limit: int = 60) -> str | None:
    text = _text(value, limit)
    return None if text.casefold() in NOBODY else text


def check(conn: sqlite3.Connection, rows: list[sqlite3.Row], raw: dict) -> dict:
    c = Checker(conn, rows)
    ids = lambda rs: [r["id"] for r in rs]  # noqa: E731
    out: dict = {
        "title": _text(raw.get("title"), 80) or "Conversazione",
        "quality": raw.get("quality") if raw.get("quality") in ("good", "partial", "noise") else "partial",
        "summary": [{"text": _text(i["text"]), "refs": ids(rs)} for i, rs in c.keep(raw.get("summary")) if _text(i.get("text"))],
        "topics": [], "commitments": [], "facts": [],
        "heard": [{"summary": _text(i.get("summary")), "app": rs[0]["via_app"], "refs": ids(rs)}
                  for i, rs in c.keep(raw.get("heard"), heard=True) if _text(i.get("summary"))],
    }
    seen = set()
    for i, rs in c.keep(raw.get("topics")):
        name = _text(i.get("name"), 40)
        if name and slug(name) not in seen:
            seen.add(slug(name))
            out["topics"].append({"name": name, "slug": slug(name), "refs": ids(rs)})
    for i, rs in c.keep(raw.get("commitments")):
        what = _text(i.get("what"), 200)
        if not what:
            continue
        # Who promised is who said it: the speaker of a cited paragraph.
        speakers = [r for r in rs if r["speaker"]]
        claimed = c.person(i.get("who"))
        said_by = next((r for r in speakers if r["person"] and r["person"] == claimed), speakers[0] if speakers else rs[0])
        to = c.person(i.get("to"))
        due = i.get("due") if isinstance(i.get("due"), str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", i["due"]) else None
        out["commitments"].append({
            "id": hashlib.sha256(f"{rows[0]['session']}/{said_by['id']}/{fold(what)}".encode()).hexdigest()[:10],
            "owner": said_by["person"], "owner_label": said_by["speaker"],
            "counterpart": to if to != said_by["person"] else None,
            "counterpart_label": label(i.get("to")),
            "what": what, "due": due, "due_text": _text(i.get("due_text"), 60) or None, "refs": ids(rs),
        })
    for i, rs in c.keep(raw.get("facts")):
        fact = _text(i.get("fact"), 300)
        if fact:
            out["facts"].append({"person": c.person(i.get("about")), "about_label": label(i.get("about")),
                                 "fact": fact, "refs": ids(rs)})
    out["dropped"] = c.dropped
    return out


# --- running -----------------------------------------------------------------------------

def trivial(rows: list[sqlite3.Row]) -> dict | None:
    """A session too short for the model: a summary without it."""
    if sum(len(r["text"]) for r in rows) >= TRIVIAL_CHARS:
        return None
    first = next((r for r in rows if r["channel"] != "heard"), rows[0])
    return {"title": _text(first["text"], 50) or "Breve", "quality": "noise", "summary": [], "topics": [],
            "commitments": [], "facts": [], "heard": [], "dropped": 0}


def extract(conn: sqlite3.Connection, llm: LLM, sid: str) -> tuple[dict, dict]:
    rows = session_rows(conn, sid)
    if not rows:
        raise SummarizeError(f"session {sid} has no paragraphs")
    stats = {"calls": 0, "prompt_tokens": 0, "gen_tokens": 0, "windows": 0}
    if (data := trivial(rows)) is not None:
        return data, stats
    system = prompt(conn, rows)
    lines = [line(n, r) for n, r in enumerate(rows, 1)]
    parts = windows(lines)
    stats["windows"] = len(parts)
    merged: dict = {k: [] for k in ("summary", "topics", "commitments", "facts", "heard")}
    title, quality = None, None
    for part in parts:
        raw, t = call(llm, system, "\n".join(part), SCHEMA)
        stats["calls"] += 1
        stats["prompt_tokens"] += t.get("prompt_n", 0)
        stats["gen_tokens"] += t.get("predicted_n", 0)
        for k in merged:
            merged[k] += raw.get(k) or []
        title, quality = title or raw.get("title"), quality or raw.get("quality")
    if len(parts) > 1:
        # Reduce: one title and summary from the windows' points (refs kept as they are).
        points = "\n".join(f"- {i.get('text')} {json.dumps(i.get('refs'))}" for i in merged["summary"]
                           if isinstance(i, dict))
        raw, t = call(llm, system, "Punti estratti dalle parti della conversazione, con i loro refs. Scrivi il "
                                   "titolo e un riassunto di massimo 6 punti, mantenendo i refs:\n" + points,
                      REDUCE_SCHEMA)
        stats["calls"] += 1
        stats["prompt_tokens"] += t.get("prompt_n", 0)
        stats["gen_tokens"] += t.get("predicted_n", 0)
        title, quality, merged["summary"] = raw.get("title"), raw.get("quality"), raw.get("summary") or merged["summary"]
    return check(conn, rows, {"title": title, "quality": quality, **merged}), stats


def store(conn: sqlite3.Connection, sid: str, data: dict, model: str | None, content_hash: str | None = None) -> None:
    """Replace what is known about the session with data (one transaction).
    content_hash: the session's when extraction started; if it changed since (a
    sync in between), the session stays stale and is summarized again."""
    s = conn.execute("SELECT day, content_hash FROM sessions WHERE id = ?", (sid,)).fetchone()
    if s is None:
        raise SummarizeError(f"session {sid} not in the index")
    content_hash = content_hash or s["content_hash"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        for table in ("summaries", "summaries_fts", "commitments", "facts", "topics"):
            conn.execute(f"DELETE FROM {table} WHERE session = ?", (sid,))
        conn.execute("INSERT INTO summaries(session, title, quality, data, model, prompt_v, content_hash, created) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (sid, data["title"], data["quality"], json.dumps(data, ensure_ascii=False), model,
                      PROMPT_VERSION, content_hash, now))
        body = " ".join([data["title"]] + [i["text"] for i in data["summary"]] + [t["name"] for t in data["topics"]]
                        + [h["summary"] for h in data["heard"]])
        conn.execute("INSERT INTO summaries_fts(session, text) VALUES (?, ?)", (sid, body))
        for c in data["commitments"]:
            conn.execute("INSERT OR REPLACE INTO commitments(id, session, day, owner, owner_label, counterpart, "
                         "counterpart_label, what, due, due_text, refs) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (c["id"], sid, s["day"], c["owner"], c["owner_label"], c["counterpart"],
                          c["counterpart_label"], c["what"], c["due"], c["due_text"], json.dumps(c["refs"])))
        for f in data["facts"]:
            conn.execute("INSERT INTO facts(session, day, person, about_label, fact, refs) VALUES (?, ?, ?, ?, ?, ?)",
                         (sid, s["day"], f["person"], f["about_label"], f["fact"], json.dumps(f["refs"])))
        for t in data["topics"]:
            conn.execute("INSERT OR REPLACE INTO topics(session, slug, name, refs) VALUES (?, ?, ?, ?)",
                         (sid, t["slug"], t["name"], json.dumps(t["refs"])))
        conn.execute("UPDATE sessions SET summarized_hash = ?, summary_status = CASE WHEN content_hash = ? "
                     "THEN 'done' ELSE 'stale' END WHERE id = ?", (content_hash, content_hash, sid))


def pending(conn: sqlite3.Connection, limit: int | None = None) -> list[str]:
    """Stale sessions, oldest first (summaries of the past are the base of the present)."""
    sql = "SELECT id FROM sessions WHERE summary_status IN ('stale', 'queued') ORDER BY start_ts"
    return [r[0] for r in conn.execute(sql + (f" LIMIT {int(limit)}" if limit else ""))]


def summarize(conn: sqlite3.Connection, llm: LLM, sid: str, model: str | None) -> dict:
    row = conn.execute("SELECT content_hash FROM sessions WHERE id = ?", (sid,)).fetchone()
    if row is None:
        raise SummarizeError(f"session {sid} not in the index")
    try:
        data, stats = extract(conn, llm, sid)
    except SummarizeError:
        with conn:
            conn.execute("UPDATE sessions SET summary_status = 'failed' WHERE id = ?", (sid,))
        raise
    store(conn, sid, data, model if stats["calls"] else None, row["content_hash"])
    return {"session": sid, "title": data["title"], "quality": data["quality"], "dropped": data["dropped"], **stats}


# --- people profiles ---------------------------------------------------------------------

PROFILE_MIN_FACTS = 2
# Rewrite "Chi è" only after this many new facts: the page is never rewritten
# from scratch at every conversation.
PROFILE_NEW_FACTS = 3


def profiles_due(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT f.person, COUNT(*) n, p.facts FROM facts f LEFT JOIN profiles p ON p.person = f.person "
        "WHERE f.person IS NOT NULL GROUP BY f.person").fetchall()
    return [(r["person"], r["n"]) for r in rows
            if r["n"] >= PROFILE_MIN_FACTS and (r["facts"] is None or r["n"] - r["facts"] >= PROFILE_NEW_FACTS)]


def write_profile(conn: sqlite3.Connection, llm: LLM, person: str) -> str:
    name = (conn.execute("SELECT name FROM people WHERE id = ?", (person,)).fetchone() or [person])[0]
    facts = conn.execute("SELECT day, fact FROM facts WHERE person = ? ORDER BY day", (person,)).fetchall()
    text = "\n".join(f"- {f['day']}: {f['fact']}" for f in facts[-60:])
    r = llm.chat([{"role": "system", "content":
                   "Scrivi in italiano 2-4 frasi su chi è questa persona, usando SOLO i fatti elencati, "
                   "senza aggiungere niente. Se i fatti si contraddicono, vale il più recente. Niente elenchi."},
                  {"role": "user", "content": f"Persona: {name}\nFatti:\n{text}"}],
                 max_tokens=300, temperature=0.2)
    profile = _text(r["message"].get("content"), 1200)
    if not profile:
        raise SummarizeError(f"empty profile for {name}")
    with conn:
        conn.execute("INSERT OR REPLACE INTO profiles(person, text, facts, created) VALUES (?, ?, ?, ?)",
                     (person, profile, len(facts), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return profile
