"""Organizer: one transcribed session -> a structured note (JSON sidecar, INTERFACES.md §1).

Flow: rows of the session (transcripts/DAY.jsonl) + speakers document -> numbered
paragraph lines [P1]… -> parts of ~2,000-4,000 tokens, one model call each (schema
extract_schema.json, checkpointed in WORK/<session>/part-<k>.json) -> merge in code
-> one short reduce call when there are several parts -> post-checks in code
(refs, media, who speaks, names -> ids, dates) -> confidence from logprobs ->
atomic write of the sidecar.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from omy.brain import NOTES, SCHEMA_VERSION, WORK, confidence, dates, llm
from omy.config import ARCHIVE

PROMPT_VERSION = 1
MODEL = "Qwen3.8-27B-GSQ-RCO-IQ2_XS-mtp"
HERE = Path(__file__).resolve().parent
CHARS_PER_TOKEN = 3.2
PART_MIN_TOKENS, PART_MAX_TOKENS = 2000, 4000
# Below this much speech (not counting media), a session is noise: no model call.
TRIVIAL_CHARS = 60
SIMILAR = 0.9  # normalized-text similarity above which two items are the same
MAX_TOKENS_PART, MAX_TOKENS_RETRY, MAX_TOKENS_REDUCE = 2500, 4000, 800

REDUCE_SCHEMA = {
    "type": "object",
    "properties": {"titolo": {"type": "string", "maxLength": 80},
                   "qualita": {"type": "string", "enum": ["buona", "parziale", "rumore"]},
                   "riassunto": {"type": "string"}},
    "required": ["titolo", "qualita", "riassunto"],
}
REF_RE = re.compile(r"^P([0-9]+)$")
NO_ONE = {"", "null", "none", "nessuno", "n/a", "?"}
ANON_RE = re.compile(r"^(persona|sconosciuto)\b(\s+\d+)?$", re.I)
APP_SUFFIX = re.compile(r"\s*\([^)]*\)$")


class OrganizeError(Exception):
    pass


def _transcripts() -> Path:
    return ARCHIVE / "transcripts"


def _check_preempt() -> None:
    """Between two parts: let the worker's preemption (a chat question) in."""
    try:
        from omy import models
    except ImportError:
        return
    models.check_preempt()


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFD", str(text).casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


def _norm_text(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", _fold(text)).split())


def _clean(value, limit: int = 400) -> str:
    return " ".join(str(value or "").split())[:limit]


def _label(value, limit: int = 80) -> str | None:
    text = _clean(value, limit)
    return None if text.casefold() in NO_ONE else text


# --- reading the session -----------------------------------------------------------------

def _candidate_days(session_id: str) -> list[str]:
    m = re.match(r"(\d{4})(\d{2})(\d{2})T", session_id)
    if not m:
        return []
    from datetime import date, timedelta
    d = date(int(m[1]), int(m[2]), int(m[3]))
    return [(d + timedelta(days=k)).isoformat() for k in (0, 1, -1)]


def _find_day(session_id: str) -> str | None:
    for day in _candidate_days(session_id):
        f = _transcripts() / f"{day}.jsonl"
        if f.is_file() and f'"{session_id}"' in f.read_text():
            return day
    return None


def _rows(session_id: str, day: str) -> list[dict]:
    out = []
    for ln in (_transcripts() / f"{day}.jsonl").read_text().splitlines():
        if ln.strip():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("session") == session_id:
                out.append(r)
    return sorted(out, key=lambda r: r["start"])


def _speakers_doc(session_id: str, day: str) -> dict:
    try:
        return json.loads((_transcripts() / day / "speakers" / f"{session_id}.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _is_media(row: dict) -> bool:
    return (row.get("via") or {}).get("kind") == "media"


def _is_call(row: dict) -> bool:
    return (row.get("via") or {}).get("kind") == "call"


def _line(n: int, r: dict) -> str:
    if _is_media(r):
        who = f"[MEDIA {(r.get('via') or {}).get('app') or 'Video'}]"
    else:
        who = r.get("speaker") or "?"
        if _is_call(r):
            who += " (in chiamata)"
    return f"[P{n}] {r['start'][11:16]} {who}: {r['text']}"


class Session:
    """Everything read from the archive for one session."""

    def __init__(self, session_id: str):
        self.id = session_id
        self.day = _find_day(session_id)
        if self.day is None:
            raise OrganizeError(f"session {session_id} not found in the transcripts")
        self.rows = _rows(session_id, self.day)
        if not self.rows:
            raise OrganizeError(f"session {session_id} has no paragraphs")
        self.speakers = _speakers_doc(session_id, self.day)
        self.clusters: dict[str, dict] = self.speakers.get("clusters") or {}
        self.lines = [_line(n, r) for n, r in enumerate(self.rows, 1)]
        by_name: dict[str, str] = {}
        for label, c in self.clusters.items():
            for name in (c.get("name"), APP_SUFFIX.sub("", c.get("name") or "")):
                if name:
                    by_name.setdefault(name, label)
        self.labels = [None if _is_media(r) else by_name.get(r.get("speaker") or "") for r in self.rows]

    def paragraphs(self) -> dict[str, dict]:
        return {f"P{n}": {"riga": r["id"], "inizio": r["start"], "fine": r["end"], "label": self.labels[n - 1],
                          "media": _is_media(r)} for n, r in enumerate(self.rows, 1)}

    def speech_chars(self) -> int:
        return sum(len(r["text"]) for r in self.rows if not _is_media(r))

    def participants(self) -> list[dict]:
        from omy import diarize
        people = diarize.load_people()
        out = []
        for label, c in self.clusters.items():
            ctx = c.get("context") or {}
            if "media" in c or c.get("source") == "media" or ctx.get("kind") == "media":
                continue
            person = c.get("person")
            name = c.get("name") or label
            out.append({"label": label, "person": person, "nome_al_momento": APP_SUFFIX.sub("", name),
                        "secondi": float(c.get("seconds") or 0), "_current": (
                            diarize.my_name() if person == "me" else (people.get(person) or {}).get("name"))})
        if not out:  # no speakers document: the names the rows carry
            secs: Counter = Counter()
            who: dict[str, str | None] = {}
            for r in self.rows:
                if _is_media(r) or not r.get("speaker"):
                    continue
                secs[r["speaker"]] += _seconds(r)
                who.setdefault(r["speaker"], r.get("person"))
            out = [{"label": None, "person": who[n], "nome_al_momento": n, "secondi": round(s, 1), "_current": None}
                   for n, s in secs.items()]
        return out

    def luogo(self) -> dict | None:
        names = Counter((r.get("place") or {}).get("name") for r in self.rows if (r.get("place") or {}).get("name"))
        return {"name": names.most_common(1)[0][0]} if names else None


def _seconds(r: dict) -> float:
    try:
        return (datetime.fromisoformat(r["end"]) - datetime.fromisoformat(r["start"])).total_seconds()
    except (KeyError, ValueError):
        return 0.0


def input_hash(session_id: str) -> str:
    s = Session(session_id)
    body = json.dumps([[ln, lb] for ln, lb in zip(s.lines, s.labels)], ensure_ascii=False)
    return hashlib.sha256(f"{body}\n{PROMPT_VERSION}\n{SCHEMA_VERSION}\n{MODEL}".encode()).hexdigest()


# --- where the note lives ----------------------------------------------------------------

def _id8(session_id: str) -> str:
    return session_id[-8:]


def note_path(session_id: str) -> Path | None:
    id8 = _id8(session_id)
    for day in _candidate_days(session_id):
        folder = NOTES / day[:4] / day[5:7] / day[8:10]
        found = sorted(folder.glob(f"*-{id8}.json")) if folder.is_dir() else []
        if found:
            return found[0]
    found = sorted(NOTES.glob(f"*/*/*/*-{id8}.json")) if NOTES.is_dir() else []
    return found[0] if found else None


def needs_organize(session_id: str) -> bool:
    try:
        h = input_hash(session_id)
    except OrganizeError:
        return False
    path = note_path(session_id)
    if path is None:
        return True
    try:
        return json.loads(path.read_text()).get("elaborazione", {}).get("input_hash") != h
    except (OSError, ValueError):
        return True


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _fold(title)).strip("-")[:40].strip("-") or "nota"


def _mkdirs(path: Path) -> None:
    """path and every missing directory above it (up to the archive), mode 700."""
    todo = []
    p = path
    while not p.exists() and p != ARCHIVE:
        todo.append(p)
        p = p.parent
    for d in reversed(todo):
        d.mkdir(mode=0o700, exist_ok=True)
        d.chmod(0o700)


def _write_atomic(path: Path, text: str) -> None:
    _mkdirs(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:  # mkstemp: mode 600
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- parts -------------------------------------------------------------------------------

def _tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def split_parts(lines: list[str]) -> list[tuple[int, int]]:
    """(first, last) line indexes of each part: as few parts as keep each under
    PART_MAX_TOKENS, balanced so none is far below PART_MIN_TOKENS, cut at paragraph boundaries."""
    sizes = [_tokens(ln) + 1 for ln in lines]
    total = sum(sizes)
    n = max(1, math.ceil(total / PART_MAX_TOKENS))
    target = total / n
    parts, start, acc = [], 0, 0
    for i, size in enumerate(sizes):
        if acc and acc + size / 2 > target and len(parts) < n - 1:  # this line would sit mostly past the target
            parts.append((start, i - 1))
            start, acc = i, 0
        acc += size
    parts.append((start, len(lines) - 1))
    return parts


def _system_prompt(s: Session) -> str:
    from omy import diarize
    day = datetime.fromisoformat(s.rows[0]["start"]).date()
    named = sorted({p["nome_al_momento"] for p in s.participants() if p["person"] not in (None, "me")})
    # Static instructions first, the variable context at the very end: identical prefix across calls.
    return (HERE / "prompts" / "extract.md").read_text() \
        .replace("{my_name}", diarize.my_name()).replace("{weekday}", dates.WEEKDAYS[day.weekday()]) \
        .replace("{day}", day.isoformat()).replace("{people}", ", ".join(named) or "nessuna oltre a te")


def _schema() -> dict:
    return json.loads((HERE / "extract_schema.json").read_text())


def _hash(*parts) -> str:
    return hashlib.sha256("\n".join(str(p) for p in parts).encode()).hexdigest()


def _call(base_url: str, system: str, user: str, schema: dict, max_tokens: int) -> llm.LLMResult:
    r = llm.chat(base_url, system, user, schema, max_tokens=max_tokens)
    if r.data is None and r.finish_reason == "length":
        r = llm.chat(base_url, system, user, schema, max_tokens=MAX_TOKENS_RETRY)
    if r.data is None:
        raise OrganizeError(f"invalid JSON from the model: {r.text[:200]!r}")
    return r


def _extract_part(s: Session, base_url: str, system: str, schema: dict, k: int, first: int, last: int,
                  work: Path) -> dict:
    user = "\n".join(s.lines[first:last + 1])
    key = _hash(system, user, PROMPT_VERSION, SCHEMA_VERSION, MODEL)
    ck = work / f"part-{k}.json"
    try:
        got = json.loads(ck.read_text())
        if got.get("key") == key:
            return got
    except (FileNotFoundError, ValueError):
        pass
    r = _call(base_url, system, user, schema, MAX_TOKENS_PART)
    got = {"key": key, "da": first + 1, "a": last + 1, "result": r.data, "text": r.text,
           "confs": confidence.item_confs(r.text, r.tokens), "conf": confidence.note_conf(r.text, r.tokens),
           "tokens": {"prompt": r.timings.get("prompt_n", 0), "gen": r.timings.get("predicted_n", len(r.tokens))}}
    _write_atomic(ck, json.dumps(got, ensure_ascii=False))
    return got


def _items(part: dict, field: str, sub: str | None = None) -> list[dict]:
    """The items of a list field of a part's result, each with its confidence attached."""
    node = part["result"]
    for k in field.split("."):
        node = node.get(k) if isinstance(node, dict) else None
    out = []
    for i, it in enumerate(node if isinstance(node, list) else []):
        if isinstance(it, dict):
            out.append({**it, "conf": part["confs"].get(f"{field}[{i}]")})
    return out


# --- merging and checks ------------------------------------------------------------------

def _refs_sorted(refs) -> list[str]:
    return sorted({r for r in refs}, key=lambda r: int(r[1:]))


def _merge(items: list[dict], same) -> list[dict]:
    out: list[dict] = []
    for it in items:
        for o in out:
            if same(o, it):
                o["refs"] = _refs_sorted([*o["refs"], *it["refs"]])
                o["conf"] = confidence.merge_conf(o.get("conf"), it.get("conf"))
                break
        else:
            out.append(it)
    return out


def _similar(a: str, b: str) -> bool:
    a, b = _norm_text(a), _norm_text(b)
    return bool(a) and (a == b or difflib.SequenceMatcher(None, a, b).ratio() >= SIMILAR)


class Checker:
    def __init__(self, s: Session):
        from omy import diarize
        self.s = s
        self.my = diarize.my_name()
        self.names: dict[str, str] = {_fold(self.my): "me", "io": "me"}
        for p in s.participants():
            if p["person"]:
                self.names[_fold(p["nome_al_momento"])] = p["person"]
        for pid, p in diarize.load_people().items():
            if p.get("name"):
                self.names[_fold(APP_SUFFIX.sub("", p["name"]))] = pid
        self.paras = s.paragraphs()
        self.dropped = 0

    def refs(self, item: dict, media: bool) -> list[str]:
        out = []
        for ref in item.get("refs") or []:
            ref = str(ref).strip()
            if REF_RE.match(ref) and ref in self.paras and self.paras[ref]["media"] == media and ref not in out:
                out.append(ref)
        return _refs_sorted(out)

    def person_id(self, name) -> str | None:
        if not isinstance(name, str) or not name.strip() or ANON_RE.match(name.strip()):
            return None
        key = _fold(APP_SUFFIX.sub("", name.strip()))
        if key in self.names:
            return self.names[key]
        first = key.split()[0] if key.split() else ""
        hits = {pid for n, pid in self.names.items() if n and (n.split()[0] == first or n == first)} if first else set()
        return hits.pop() if len(hits) == 1 else None

    def row(self, ref: str) -> dict:
        return self.s.rows[int(ref[1:]) - 1]


def _check(s: Session, merged: dict) -> dict:
    c = Checker(s)
    out: dict = {}

    def keep(items, media=False):
        for it in items:
            refs = c.refs(it, media)
            if refs:
                yield it, refs
            else:
                c.dropped += 1

    out["punti_chiave"] = _merge(
        [{"testo": _clean(i.get("testo")), "refs": refs, "conf": i.get("conf")}
         for i, refs in keep(merged["punti_chiave"]) if _clean(i.get("testo"))],
        lambda a, b: _similar(a["testo"], b["testo"]))

    ent: dict[str, list] = {}
    for kind in ("persone", "luoghi", "organizzazioni", "progetti"):
        rows = []
        for i, refs in keep(merged[f"entita.{kind}"]):
            name = _clean(i.get("nome"), 80)
            if not name:
                continue
            pid = c.person_id(name) if kind == "persone" else None
            if kind == "persone" and (ANON_RE.match(name) or pid == "me"):
                continue
            rows.append({"nome": name, **({"person_id": pid} if kind == "persone" else {}), "refs": refs,
                         "conf": i.get("conf")})
        ent[kind] = _merge(rows, lambda a, b: _fold(a["nome"]) == _fold(b["nome"]))
    out["entita"] = ent

    todo = []
    for i, refs in keep(merged["impegni_e_todo"]):
        cosa = _clean(i.get("cosa"), 240)
        if not cosa:
            continue
        chi, chi_id = _label(i.get("chi"), 60), c.person_id(i.get("chi"))
        # Who promised is who spoke: the named (or your own) speaker of a cited paragraph.
        named = [c.row(r) for r in refs if c.row(r).get("person")]
        if named and not any(r["person"] == chi_id for r in named):
            chi, chi_id = APP_SUFFIX.sub("", named[0]["speaker"] or ""), named[0]["person"]
        verso = _label(i.get("verso_chi"), 60)
        ref_dt = datetime.fromisoformat(c.paras[refs[0]]["inizio"])
        sc = _label(i.get("scadenza_testo"), 60)
        d = dates.resolve(sc, ref_dt, prefer="future") if sc else None
        todo.append({"chi": chi, "chi_id": chi_id, "verso_chi": verso, "verso_chi_id": c.person_id(verso),
                     "cosa": cosa, "scadenza_testo": sc, "scadenza": d.isoformat() if d else None,
                     "stato": "aperto", "refs": refs, "conf": i.get("conf")})
    out["impegni_e_todo"] = _merge(todo, lambda a, b: _similar(a["cosa"], b["cosa"]) and
                                   a["chi_id"] == b["chi_id"] and (a["chi_id"] is not None or
                                                                   _fold(a["chi"] or "") == _fold(b["chi"] or "")))

    evs = []
    for i, refs in keep(merged["date_ed_eventi_citati"]):
        txt, ev = _label(i.get("testo_data"), 60), _clean(i.get("evento"), 200)
        if not txt:
            continue
        d = dates.resolve(txt, datetime.fromisoformat(c.paras[refs[0]]["inizio"]))
        evs.append({"testo_data": txt, "data": d.isoformat() if d else None, "evento": ev, "refs": refs,
                    "conf": i.get("conf")})
    out["date_ed_eventi_citati"] = _merge(
        evs, lambda a, b: _fold(a["testo_data"]) == _fold(b["testo_data"]) and _similar(a["evento"], b["evento"]))

    heard = []
    for i, refs in keep(merged["ascoltato"], media=True):
        if _clean(i.get("riassunto")):
            heard.append({"riassunto": _clean(i["riassunto"]), "app": (c.row(refs[0]).get("via") or {}).get("app"),
                          "refs": refs})
    out["ascoltato"] = heard
    seen, tags = set(), []
    for t in merged["tag"]:
        t = _clean(t, 40)
        if t and _fold(t) not in seen:
            seen.add(_fold(t))
            tags.append(t)
    out["tag"] = tags
    return out


def _with_conf(note: dict) -> None:
    """Fill missing conf with nulls (no logprobs) so every item has the same shape."""
    def fill(items):
        for it in items:
            it["conf"] = it.get("conf") or {"media": None, "minima": None}
    fill(note["punti_chiave"])
    fill(note["impegni_e_todo"])
    fill(note["date_ed_eventi_citati"])
    for lst in note["entita"].values():
        fill(lst)


# --- the note ----------------------------------------------------------------------------

def _base(s: Session) -> dict:
    first, last = s.rows[0], s.rows[-1]
    parts = [{k: v for k, v in p.items() if k != "_current"} for p in s.participants()]
    tdoc = _transcripts() / s.day / f"{s.id}.json"
    try:
        segments = json.loads(tdoc.read_text()).get("segments") or []
    except (FileNotFoundError, ValueError):
        segments = []
    return {
        "schema_version": SCHEMA_VERSION, "id": s.id, "data": s.day,
        "ora_inizio": first["start"], "ora_fine": last["end"], "luogo": s.luogo(),
        "file_sorgente": {"sessione": f"transcripts/{s.day}/{s.id}.json", "giorno": f"transcripts/{s.day}.jsonl",
                          "segmenti": segments},
        "partecipanti": parts, "paragrafi": s.paragraphs(),
    }


def _trivial(s: Session) -> dict:
    first = next((r for r in s.rows if not _is_media(r)), s.rows[0])
    return {"titolo": _clean(first["text"], 50) or "Rumore", "qualita": "rumore", "riassunto": "", "parti": [],
            "punti_chiave": [], "entita": {"persone": [], "luoghi": [], "organizzazioni": [], "progetti": []},
            "impegni_e_todo": [], "date_ed_eventi_citati": [], "ascoltato": [], "tag": []}


def _reduce(base_url: str, parts: list[dict], work: Path) -> dict:
    blocks = []
    for k, p in enumerate(parts, 1):
        r = p["result"]
        pts = "\n".join(f"- {_clean(i.get('testo'))}" for i in r.get("punti_chiave") or [] if isinstance(i, dict))
        blocks.append(f"Parte {k} (P{p['da']}-P{p['a']}): {_clean(r.get('riassunto_parte'), 1500)}\n{pts}".rstrip())
    user = "\n\n".join(blocks)
    system = (HERE / "prompts" / "reduce.md").read_text()
    key = _hash(system, user, PROMPT_VERSION, SCHEMA_VERSION, MODEL)
    ck = work / "reduce.json"
    try:
        got = json.loads(ck.read_text())
        if got.get("key") == key:
            return got["result"]
    except (FileNotFoundError, ValueError):
        pass
    r = _call(base_url, system, user, REDUCE_SCHEMA, MAX_TOKENS_REDUCE)
    _write_atomic(ck, json.dumps({"key": key, "result": r.data}, ensure_ascii=False))
    return r.data


def organize_session(session_id: str, base_url: str) -> Path:
    t0 = time.monotonic()
    s = Session(session_id)
    note = _base(s)
    if s.speech_chars() < TRIVIAL_CHARS:
        body = _trivial(s)
        confs: list = []
        n_parts = 0
    else:
        system, schema = _system_prompt(s), _schema()
        work = WORK / session_id
        ranges = split_parts(s.lines)
        parts = []
        for k, (first, last) in enumerate(ranges, 1):
            if k > 1:
                _check_preempt()
            parts.append(_extract_part(s, base_url, system, schema, k, first, last, work))
        merged: dict = {"punti_chiave": [], "impegni_e_todo": [], "date_ed_eventi_citati": [], "ascoltato": [],
                        "tag": [], **{f"entita.{k}": [] for k in ("persone", "luoghi", "organizzazioni", "progetti")}}
        for p in parts:
            for field in ("punti_chiave", "impegni_e_todo", "date_ed_eventi_citati", "ascoltato",
                          *(f"entita.{k}" for k in ("persone", "luoghi", "organizzazioni", "progetti"))):
                merged[field] += _items(p, field)
            merged["tag"] += [t for t in p["result"].get("tag") or [] if isinstance(t, str)]
        body = _check(s, merged)
        first_r = parts[0]["result"]
        if len(parts) > 1:
            _check_preempt()
            red = _reduce(base_url, parts, work)
            titolo, qualita, riassunto = red.get("titolo"), red.get("qualita"), red.get("riassunto")
        else:
            titolo, qualita, riassunto = first_r.get("titolo"), first_r.get("qualita"), first_r.get("riassunto_parte")
        body.update({
            "titolo": _clean(titolo, 80) or "Conversazione",
            "qualita": qualita if qualita in ("buona", "parziale", "rumore") else "parziale",
            "riassunto": _clean(riassunto, 1500),
            "parti": [{"da": f"P{p['da']}", "a": f"P{p['a']}", "riassunto": _clean(p["result"].get("riassunto_parte"), 1500)}
                      for p in parts]})
        confs = [p["conf"] for p in parts]
        n_parts = len(parts)
    note.update({k: body[k] for k in ("titolo", "qualita", "riassunto", "parti", "punti_chiave", "entita",
                                      "impegni_e_todo", "date_ed_eventi_citati", "ascoltato", "tag")})
    _with_conf(note)
    note["collegamenti"] = []
    note["confidenza"] = confidence.combine(confs) or {"media": None, "minima": None}
    note["da_rivedere"] = confidence.da_rivedere(note)
    note["elaborazione"] = {"modello": MODEL, "prompt_version": PROMPT_VERSION, "schema_version": SCHEMA_VERSION,
                            "input_hash": input_hash(session_id),
                            "creato": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                            "durata_s": round(time.monotonic() - t0, 1), "n_parti": n_parts}
    path = note_path(session_id)
    if path is None:
        start = datetime.fromisoformat(s.rows[0]["start"])
        path = NOTES / s.day[:4] / s.day[5:7] / s.day[8:10] / f"{start:%H%M}-{_slug(note['titolo'])}-{_id8(session_id)}.json"
    _write_atomic(path, json.dumps(note, ensure_ascii=False, indent=1) + "\n")
    shutil.rmtree(WORK / session_id, ignore_errors=True)
    return path
