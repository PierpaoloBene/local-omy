"""The vault: ~/SecondBrain, Markdown readable by hand and by Obsidian.

What brain writes sits between markers:

    <!-- brain:begin summary h=3fa2c91b0d4e -->
    …
    <!-- brain:end summary -->

h is the hash of what brain wrote. Everything outside the markers is yours and
is never touched. If you edit inside a block, the hash no longer matches: the
block becomes yours, brain stops updating it and lists it in `brain vault
conflicts`. Deleting a whole block (markers included) means you don't want it:
it is not written again. To give an edited block back to brain, set h=0 in its
begin marker. In the frontmatter brain rewrites only its own keys.

Each note also ends with a `<!-- brain:data {…} -->` comment (invisible in
Obsidian's reading view): what brain needs to rebuild the index from the
vault (brain reindex) without running the model again.

The ticks in commitments.md ([x] done, [-] dropped) are yours too: brain reads
them back before rewriting the file, and a tick alone does not count as an edit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from brain import dates
from brain.config import private_dir

BEGIN = re.compile(r"<!-- brain:begin (?P<name>[\w-]+) h=(?P<h>[0-9a-f]+) -->\n?")
DATA = re.compile(r"<!-- brain:data (?P<json>.*?) -->", re.S)
CHECKBOX = re.compile(r"^(\s*- )\[[ xX-]\]", re.M)
COMMITMENT_LINE = re.compile(r"^\s*- \[(?P<box>[ xX-])\].*\^c(?P<id>[0-9a-f]+)\s*$", re.M)
MONTHS = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre",
          "ottobre", "novembre", "dicembre"]


# --- low level ---------------------------------------------------------------------------

def block_hash(name: str, content: str) -> str:
    if name.startswith("commitments"):
        # Ticking a box is not an edit: the ticks are read back as statuses.
        content = CHECKBOX.sub(r"\1[ ]", content)
    return hashlib.sha256(content.strip().encode()).hexdigest()[:12]


def block(name: str, content: str) -> str:
    content = content.strip()
    return (f"<!-- brain:begin {name} h={block_hash(name, content)} -->\n"
            + (content + "\n" if content else "") + f"<!-- brain:end {name} -->")


def find_block(text: str, name: str) -> tuple[re.Match, int, str] | None:
    for m in BEGIN.finditer(text):
        if m.group("name") == name:
            end = text.find(f"<!-- brain:end {name} -->", m.end())
            if end < 0:
                return None
            return m, end + len(f"<!-- brain:end {name} -->"), text[m.end():end]
    return None


def split_frontmatter(text: str) -> tuple[list[str], str]:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            return text[4:end].split("\n"), text[end + 5:]
    return [], text


def frontmatter(keys: dict, user_lines: list[str] | None = None) -> str:
    """brain's keys as one line each (JSON values are valid YAML); the user's
    other keys kept as they were."""
    lines, skip = [], False
    for ln in user_lines or []:
        key = ln.split(":", 1)[0].strip() if ln and not ln[0].isspace() else None
        if key is not None:
            skip = key in keys
        if not skip:
            lines.append(ln)
    ours = [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in keys.items() if v is not None]
    return "---\n" + "\n".join(ours + [ln for ln in lines if ln.strip()]) + "\n---\n"


def data_comment(data: dict) -> str:
    # "--" cannot end an HTML comment early: in JSON it can only appear inside strings.
    return "<!-- brain:data " + json.dumps(data, ensure_ascii=False).replace("--", "-\\u002d") + " -->"


def read_data(text: str) -> dict | None:
    m = DATA.search(text)
    try:
        return json.loads(m.group("json")) if m else None
    except ValueError:
        return None


def write_private(path: Path, text: str) -> bool:
    """Atomic, mode 600, folders 700; False if the file already says exactly this."""
    try:
        if path.read_text() == text:
            return False
    except FileNotFoundError:
        pass
    private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


@dataclass
class Result:
    written: list[str] = field(default_factory=list)
    conflicts: list[tuple[str, str, str]] = field(default_factory=list)  # (path, block, edited|removed)


def render_note(path: Path, keys: dict, blocks: list[tuple[str, str]], data: dict, result: Result) -> None:
    """Write a note, or update brain's parts of an existing one."""
    try:
        old = path.read_text()
    except FileNotFoundError:
        old = None
    names = [n for n, _ in blocks]
    if old is None:
        body = "\n\n".join(block(n, c) for n, c in blocks)
        text = frontmatter(keys) + "\n" + body + "\n\n" + data_comment({**data, "blocks": names}) + "\n"
    else:
        fm, body = split_frontmatter(old)
        previous = (read_data(old) or {}).get("blocks", [])
        body = DATA.sub("", body).rstrip()
        for name, content in blocks:
            found = find_block(body, name)
            if found is None:
                if name in previous:
                    result.conflicts.append((str(path), name, "removed"))
                else:
                    body += "\n\n" + block(name, content)  # a block new in this version of brain
                continue
            m, end, current = found
            if m.group("h") != "0" and block_hash(name, current) != m.group("h"):
                result.conflicts.append((str(path), name, "edited"))
                continue
            body = body[:m.start()] + block(name, content) + body[end:]
        text = frontmatter(keys, fm) + body.rstrip() + "\n\n" + data_comment({**data, "blocks": names}) + "\n"
    if write_private(path, text):
        result.written.append(str(path))


# --- links and paths -------------------------------------------------------------------------

def session_path(vault: Path, day: str, sid: str) -> Path:
    return vault / "sessions" / day[:4] / day[5:7] / day[8:10] / f"{sid}.md"


def plink(row: dict, same_note: bool = False, label: str | None = None) -> str:
    """A link to a paragraph (its block id ^p<ms> in the session note)."""
    label = label or row["start"][11:16]
    return f"[[{'' if same_note else row['session']}#^p{row['ms']}|{label}]]"


def long_date(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{dates.WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} {d.year}"


def short_date(day: str) -> str:
    return f"{int(day[8:10])}/{day[5:7]}"


class Context:
    """What the renderers need from the index; paragraphs are read when needed."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.people = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM people")}
        self.me = self.people.get("me", "Io")
        self.statuses = dict(conn.execute("SELECT id, status FROM commitment_status").fetchall())
        self._paragraphs: dict[str, dict | None] = {}

    def paragraph(self, pid: str) -> dict | None:
        if pid not in self._paragraphs:
            row = self.conn.execute("SELECT * FROM paragraphs WHERE id = ?", (pid,)).fetchone()
            self._paragraphs[pid] = dict(row) if row else None
        return self._paragraphs[pid]

    def session_paragraphs(self, sid: str) -> list[dict]:
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM paragraphs WHERE session = ? ORDER BY ms", (sid,))]
        self._paragraphs.update((r["id"], r) for r in rows)
        return rows

    def person_link(self, pid: str | None, label: str | None = None) -> str:
        if pid and pid in self.people:
            return f"[[{pid}|{self.people[pid]}]]"
        return label or "?"

    def refs(self, ids, same_note: bool = False, with_day: bool = False) -> str:
        out = []
        for pid in ids:
            p = self.paragraph(pid)
            if p:
                label = f"{short_date(p['day'])} {p['start'][11:16]}" if with_day else None
                out.append(plink(p, same_note, label))
        return " ".join(out)


# --- notes -----------------------------------------------------------------------------------

def commitment_text(ctx: Context, c: dict, same_note: bool = False) -> str:
    from brain.summarize import label
    who = ctx.person_link(c["owner"], c["owner_label"])
    to_label = label(c["counterpart_label"])  # summaries made before label() existed
    to = ctx.person_link(c["counterpart"], to_label) if (c["counterpart"] or to_label) else None
    due = ""
    if c.get("due"):
        due = f" · entro {dates.short(c['due'])}"
    elif c.get("due_text"):
        due = f" · {c['due_text']}"
    refs = json.loads(c["refs"]) if isinstance(c["refs"], str) else c["refs"]
    return f"**{who}{' → ' + to if to else ''}**: {c['what']}{due} {ctx.refs(refs, same_note, with_day=not same_note)}"


def render_session(ctx: Context, vault: Path, s: sqlite3.Row, result: Result) -> None:
    rows = ctx.session_paragraphs(s["id"])
    srow = ctx.conn.execute("SELECT * FROM summaries WHERE session = ?", (s["id"],)).fetchone()
    data = json.loads(srow["data"]) if srow else None
    persons = json.loads(s["persons"])
    title = data["title"] if data else f"Conversazione delle {s['start'][11:16]}"
    with_whom = ", ".join(ctx.person_link(p) for p in persons if p != "me")
    unnamed = sorted({p["speaker"] for p in rows if not p["person"] and p["speaker"] and p["channel"] != "heard"})
    head = [f"# {title}", "",
            f"_{long_date(s['day'])} · {s['start'][11:16]}–{s['end'][11:16]}"
            + (f" · 📍 {s['place']}" if s["place"] else "")
            + (f" · con {with_whom}" if with_whom else "")
            + (f" · voci senza nome: {', '.join(unnamed)}" if unnamed else "") + "_",
            f"[[{s['day']}|← {long_date(s['day'])}]]"]

    def section(heading: str, items: list[str]) -> str:
        return f"## {heading}\n\n" + "\n".join(f"- {i}" for i in items) if items else ""

    if data:
        summary = section("Riassunto", [f"{i['text']} {ctx.refs(i['refs'], True)}" for i in data["summary"]]) \
            or "## Riassunto\n\n_Niente di rilevante (qualità: " + data["quality"] + ")._"
        commitments = section("Impegni", [commitment_text(ctx, c, True) for c in data["commitments"]])
        facts = section("Fatti emersi", [f"**{ctx.person_link(f['person'], f['about_label'])}**: {f['fact']} "
                                         f"{ctx.refs(f['refs'], True)}" for f in data["facts"]])
        heard = section("Cose che ho sentito", [f"({h.get('app') or 'media'}) {h['summary']} {ctx.refs(h['refs'], True)}"
                                                 for h in data["heard"]])
        topics = " ".join(f"[[{t['slug']}|#{t['name']}]]" for t in data["topics"])
    else:
        summary, commitments, facts, heard, topics = "## Riassunto\n\n_Non ancora fatto._", "", "", "", ""

    lines = []
    for p in rows:
        who = p["speaker"] or "?"
        if p["channel"] == "heard":
            who = f"📺 {p['via_app'] or 'Media'}"
        elif p["via_kind"] == "call":
            who = f"📞 {who}"
        lines.append(f"**{p['start'][11:16]} · {who}** {p['text']} ^p{p['ms']}")
    transcript = "## Trascrizione\n\n" + "\n\n".join(lines)
    keys = {"type": "session", "date": s["day"], "start": s["start"], "end": s["end"], "place": s["place"],
            "participants": [ctx.people.get(p, p) for p in persons],
            "kinds": json.loads(s["kinds"]),
            "topics": [t["name"] for t in data["topics"]] if data else [],
            "quality": data["quality"] if data else None}
    stored = {"v": 1, "session": s["id"], "content_hash": srow["content_hash"] if srow else None,
              "model": srow["model"] if srow else None, "prompt_v": srow["prompt_v"] if srow else None,
              "summary": data}
    render_note(session_path(vault, s["day"], s["id"]), keys,
                [("title", "\n".join(head) + (f"\n\n{topics}" if topics else "")), ("summary", summary),
                 ("commitments", commitments), ("facts", facts), ("heard", heard), ("transcript", transcript)],
                stored, result)


def render_day(ctx: Context, vault: Path, day: str, sessions: list[sqlite3.Row], result: Result) -> None:
    talks, heard, promised = [], [], []
    for s in sessions:
        srow = ctx.conn.execute("SELECT title, data FROM summaries WHERE session = ?", (s["id"],)).fetchone()
        data = json.loads(srow["data"]) if srow else None
        title = srow["title"] if srow else f"Conversazione delle {s['start'][11:16]}"
        persons = [p for p in json.loads(s["persons"]) if p != "me"]
        line = (f"- {s['start'][11:16]}–{s['end'][11:16]} · [[{s['id']}|{title}]]"
                + (f" · con {', '.join(ctx.person_link(p) for p in persons)}" if persons else "")
                + (f" · 📍 {s['place']}" if s["place"] else ""))
        if data and data["summary"]:
            line += f"\n  - {data['summary'][0]['text']}"
        kinds = json.loads(s["kinds"])
        (heard if kinds == ["media"] else talks).append(line)
        if data:
            heard += [f"- {s['start'][11:16]} ({h.get('app') or 'media'}) {h['summary']} {ctx.refs(h['refs'])}"
                      for h in data["heard"] if kinds != ["media"]]
            promised += [f"- {commitment_text(ctx, c)}" for c in data["commitments"]]
    blocks = [("title", f"# {long_date(day)}")]
    blocks.append(("sessions", "## Conversazioni\n\n" + "\n".join(talks) if talks else ""))
    blocks.append(("heard", "## Cose che ho sentito\n\n" + "\n".join(heard) if heard else ""))
    blocks.append(("commitments", "## Impegni presi\n\n" + "\n".join(promised) if promised else ""))
    render_note(vault / "days" / f"{day}.md", {"type": "day", "date": day}, blocks, {"v": 1, "day": day}, result)


def render_person(ctx: Context, vault: Path, pid: str, result: Result) -> None:
    name = ctx.people[pid]
    conn = ctx.conn
    facts = conn.execute("SELECT * FROM facts WHERE person = ? ORDER BY day, id", (pid,)).fetchall()
    sessions = conn.execute(
        "SELECT s.*, (SELECT title FROM summaries WHERE session = s.id) AS title FROM sessions s "
        "WHERE s.id IN (SELECT session FROM paragraphs WHERE person = ?) ORDER BY s.start_ts DESC LIMIT 100",
        (pid,)).fetchall()
    topics = conn.execute(
        "SELECT name, slug, COUNT(*) n FROM topics WHERE session IN "
        "(SELECT session FROM paragraphs WHERE person = ?) GROUP BY slug ORDER BY n DESC LIMIT 15", (pid,)).fetchall()
    open_c = conn.execute(
        "SELECT * FROM commitments WHERE (owner = ? OR counterpart = ?) AND id NOT IN "
        "(SELECT id FROM commitment_status WHERE status != 'open') ORDER BY day", (pid, pid)).fetchall()
    profile = conn.execute("SELECT text FROM profiles WHERE person = ?", (pid,)).fetchone()
    blocks = [
        ("title", f"# {name}"),
        ("about", "## Chi è\n\n" + (profile["text"] if profile else "_Profilo non ancora scritto._")),
        ("facts", "## Fatti\n\n" + "\n".join(f"- {f['fact']} {ctx.refs(json.loads(f['refs']), with_day=True)}"
                                             for f in facts) if facts else ""),
        ("commitments", "## Impegni aperti\n\n" + "\n".join(f"- {commitment_text(ctx, dict(c))}" for c in open_c)
         if open_c else ""),
        ("topics", "## Argomenti ricorrenti\n\n" + " · ".join(f"[[{t['slug']}|{t['name']}]] ({t['n']})" for t in topics)
         if topics else ""),
        ("sessions", "## Conversazioni\n\n" + "\n".join(
            f"- {dates.short(s['day'])} {s['start'][11:16]} · [[{s['id']}|{s['title'] or 'Conversazione'}]]"
            for s in sessions) if sessions else ""),
    ]
    render_note(vault / "people" / f"{pid}.md", {"type": "person", "id": pid, "aliases": [name]}, blocks,
                {"v": 1, "person": pid, "profile": profile["text"] if profile else None}, result)


def render_topic(ctx: Context, vault: Path, slug: str, result: Result) -> None:
    rows = ctx.conn.execute(
        "SELECT t.*, s.day, s.start, (SELECT title FROM summaries WHERE session = s.id) AS title "
        "FROM topics t JOIN sessions s ON s.id = t.session WHERE t.slug = ? ORDER BY s.start_ts DESC",
        (slug,)).fetchall()
    if not rows:
        return
    names = sorted({r["name"] for r in rows})
    lines = [f"- {dates.short(r['day'])} · [[{r['session']}|{r['title'] or 'Conversazione'}]] "
             f"{ctx.refs(json.loads(r['refs'])[:3])}" for r in rows]
    render_note(vault / "topics" / f"{slug}.md", {"type": "topic", "aliases": names},
                [("title", f"# {names[0]}"), ("sessions", "## Conversazioni\n\n" + "\n".join(lines))],
                {"v": 1, "topic": slug}, result)


def read_commitment_ticks(vault: Path) -> dict[str, str]:
    try:
        text = (vault / "commitments.md").read_text()
    except FileNotFoundError:
        return {}
    return {m.group("id"): {" ": "open", "x": "done", "X": "done", "-": "dropped"}[m.group("box")]
            for m in COMMITMENT_LINE.finditer(text)}


def sync_commitment_ticks(conn: sqlite3.Connection, vault: Path) -> int:
    """Your ticks in commitments.md become statuses in the index."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ticks = read_commitment_ticks(vault)
    with conn:
        for cid, status in ticks.items():
            conn.execute("INSERT INTO commitment_status(id, status, updated) VALUES (?, ?, ?) "
                         "ON CONFLICT(id) DO UPDATE SET status = excluded.status, updated = excluded.updated "
                         "WHERE commitment_status.status != excluded.status", (cid, status, now))
    return len(ticks)


def set_tick(conn: sqlite3.Connection, vault: Path, cid: str, status: str) -> None:
    """A status changed from the app: written as the tick in commitments.md (the
    source of truth), then the index follows."""
    box = {"open": " ", "done": "x", "dropped": "-"}[status]
    path = vault / "commitments.md"
    try:
        text = path.read_text()
    except FileNotFoundError:
        text = None
    if text is not None:
        new = re.sub(rf"^(\s*- )\[[ xX-]\](.*\^c{re.escape(cid)}\s*)$", rf"\g<1>[{box}]\g<2>", text, flags=re.M)
        if new != text:
            write_private(path, new)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute("INSERT INTO commitment_status(id, status, updated) VALUES (?, ?, ?) ON CONFLICT(id) "
                     "DO UPDATE SET status = excluded.status, updated = excluded.updated", (cid, status, now))
    ctx = Context(conn)
    render_commitments(ctx, vault, Result())


def render_commitments(ctx: Context, vault: Path, result: Result) -> None:
    rows = [dict(r) for r in ctx.conn.execute("SELECT * FROM commitments ORDER BY day DESC, id")]
    by = defaultdict(list)
    for c in rows:
        by[ctx.statuses.get(c["id"], "open")].append(c)
    box = {"open": " ", "done": "x", "dropped": "-"}

    def lines(status: str, mine: bool | None = None) -> str:
        items = [c for c in by[status] if mine is None or (c["owner"] == "me") == mine]
        return "\n".join(f"- [{box[status]}] {commitment_text(ctx, c)} · {dates.short(c['day'])} ^c{c['id']}"
                         for c in items)

    blocks = [
        ("title", "# Impegni\n\n_Spunta [x] quando è fatto, [-] se non vale più: brain lo rilegge._"),
        ("commitments-mine", "## Ho promesso io\n\n" + (lines("open", True) or "_Niente di aperto._")),
        ("commitments-theirs", "## Mi hanno promesso\n\n" + (lines("open", False) or "_Niente di aperto._")),
        ("commitments-closed", "## Chiusi\n\n" + "\n".join(x for x in (lines("done"), lines("dropped")) if x)
         if by["done"] or by["dropped"] else ""),
    ]
    render_note(vault / "commitments.md", {"type": "commitments"}, blocks, {"v": 1}, result)


def render_all(conn: sqlite3.Connection, vault: Path, sessions: set[str] | None = None) -> Result:
    """Bring the vault up to date with the index: everything, or only these
    sessions and the days, people and topics they touch. Notes whose content is
    the same are not rewritten."""
    sync_commitment_ticks(conn, vault)
    ctx = Context(conn)
    result = Result()
    private_dir(vault)
    if sessions is None:
        rows = conn.execute("SELECT * FROM sessions ORDER BY start_ts").fetchall()
    else:
        rows = [r for sid in sessions for r in conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,))]
    days, people, topics = set(), set(), set()
    for s in rows:
        render_session(ctx, vault, s, result)
        days.add(s["day"])
        people.update(json.loads(s["persons"]))
        topics.update(r[0] for r in conn.execute("SELECT slug FROM topics WHERE session = ?", (s["id"],)))
    if sessions is None:
        people = set(ctx.people)
        topics = {r[0] for r in conn.execute("SELECT DISTINCT slug FROM topics")}
    for day in sorted(days):
        render_day(ctx, vault, day, conn.execute("SELECT * FROM sessions WHERE day = ? ORDER BY start_ts",
                                                 (day,)).fetchall(), result)
    for pid in sorted(people & set(ctx.people)):
        render_person(ctx, vault, pid, result)
    for slug in sorted(topics):
        render_topic(ctx, vault, slug, result)
    render_commitments(ctx, vault, result)
    if sessions is None:
        mark_orphans(conn, vault, result)
    return result


def mark_orphans(conn: sqlite3.Connection, vault: Path, result: Result) -> None:
    """Notes of sessions that no longer exist (re-transcribed under new ids):
    kept, with your notes in them, and marked orphan: true."""
    known = {r[0] for r in conn.execute("SELECT id FROM sessions")}
    for path in (vault / "sessions").glob("*/*/*/*.md") if (vault / "sessions").is_dir() else []:
        if path.stem not in known:
            fm, body = split_frontmatter(path.read_text())
            if not any(ln.startswith("orphan:") for ln in fm):
                write_private(path, frontmatter({"orphan": True}, fm) + body)
                result.written.append(str(path))


def conflicts(vault: Path) -> list[tuple[str, str, str]]:
    """Blocks you edited or removed: brain leaves them alone."""
    out = []
    for path in vault.rglob("*.md"):
        text = path.read_text()
        previous = (read_data(text) or {}).get("blocks", [])
        present = set()
        for m in BEGIN.finditer(text):
            name = m.group("name")
            present.add(name)
            found = find_block(text, name)
            if found and m.group("h") != "0" and block_hash(name, found[2]) != m.group("h"):
                out.append((str(path.relative_to(vault)), name, "edited"))
        out += [(str(path.relative_to(vault)), n, "removed") for n in previous if n not in present]
    return sorted(out)


# --- rebuilding the index from the vault -------------------------------------------------------

def import_vault(conn: sqlite3.Connection, vault: Path) -> dict:
    """brain reindex: summaries, profiles and commitment ticks back from the notes.
    A summary counts only if its session is still the same (content_hash)."""
    from brain import summarize
    counts = {"summaries": 0, "stale": 0, "profiles": 0, "ticks": 0}
    hashes = dict(conn.execute("SELECT id, content_hash FROM sessions").fetchall())
    for path in (vault / "sessions").glob("*/*/*/*.md") if (vault / "sessions").is_dir() else []:
        data = read_data(path.read_text()) or {}
        sid, summary = data.get("session"), data.get("summary")
        if not sid or not summary or sid not in hashes:
            continue
        summarize.store(conn, sid, summary, data.get("model"), data.get("content_hash"))
        if data.get("content_hash") == hashes[sid]:
            counts["summaries"] += 1
        else:
            counts["stale"] += 1
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for path in (vault / "people").glob("*.md") if (vault / "people").is_dir() else []:
        data = read_data(path.read_text()) or {}
        if data.get("person") and data.get("profile"):
            with conn:
                conn.execute("INSERT OR REPLACE INTO profiles(person, text, facts, created) VALUES (?, ?, ?, ?)",
                             (data["person"], data["profile"], data.get("facts", 0), now))
            counts["profiles"] += 1
    counts["ticks"] = sync_commitment_ticks(conn, vault)
    return counts
