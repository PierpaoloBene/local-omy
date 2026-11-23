"""The vault: Markdown for Obsidian, generated from the notes' JSON sidecars.

  brain/note/AAAA/MM/GG/<HHMM>-<slug>-<id8>.md   the note, next to its sidecar
  brain/persone|luoghi|organizzazioni|progetti/<Nome>.md   what a name appears in
  brain/giorni/AAAA-MM-GG.md, impegni.md, da-rivedere.md   the indexes

What is generated sits between markers:

    <!-- omy:begin NAME h=3fa2c91b -->
    ...
    <!-- omy:end NAME -->

h is the hash of what was written. Everything outside the markers is yours and
is kept. If you edit inside a block the hash no longer matches: the block is
yours, it is not rewritten and is listed in cache/vault-conflicts.json. Ticks
(`- [x]`) are not edits: the ticked ^ids of a commitment are read back before
rendering and saved as stato "fatto" in the sidecar (the only field written
besides `collegamenti`).

The sidecar is the truth: output is deterministic (sorted), a render with
nothing changed rewrites no file. Names are the current ones (omy.brain.names).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from omy.brain import BRAIN, NOTES
from omy.brain.names import Names, current_name
from omy.config import ARCHIVE
from omy.diarize import load_people, my_name

CACHE = ARCHIVE / "cache"
INDEX = CACHE / "vault-index.json"
CONFLICTS = CACHE / "vault-conflicts.json"
LOW = 0.40            # items below this confidence get a warning mark
MAX_LINKS = 10
CATS = ("persone", "luoghi", "organizzazioni", "progetti")
TIPO = {"persone": "persona", "luoghi": "luogo", "organizzazioni": "organizzazione", "progetti": "progetto"}
WARN = "⚠️ "

BLOCK = re.compile(r"<!-- omy:begin (?P<n>[\w-]+) h=(?P<h>[0-9a-f]{8}) -->\n(?P<c>.*?)<!-- omy:end (?P=n) -->", re.S)
TICKED = re.compile(r"^\s*- \[[xX]\] .*\^(c[0-9a-f]{8}(?:-\d+)?)\s*$", re.M)
UNTICKED = re.compile(r"^\s*- \[ \] .*\^(c[0-9a-f]{8}(?:-\d+)?)\s*$", re.M)
CHECKBOX = re.compile(r"^(\s*- )\[[xX]\]", re.M)
KEY = re.compile(r"[A-Za-z_][\w-]*:")


# --- files ------------------------------------------------------------------------------------

def _mkdirs(path: Path) -> None:
    missing = []
    p = path
    while not p.exists() and p != p.parent:
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        d.mkdir(mode=0o700, exist_ok=True)
        d.chmod(0o700)


def _write_atomic(path: Path, text: str) -> bool:
    """Write text (mode 600) unless the file already holds it; True if written."""
    try:
        if path.read_text() == text:
            return False
    except (FileNotFoundError, UnicodeDecodeError):
        pass
    _mkdirs(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except (FileNotFoundError, UnicodeDecodeError):
        return None


def _json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _safe(name: str) -> str:
    """A name as a file name: no path or Obsidian-link characters."""
    s = re.sub(r'[\\/:*?"<>|#^\[\]\x00-\x1f]', " ", name)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s or "senza nome")[:80].strip(" .") or "senza nome"


def _display(names) -> str:
    """One spelling for the names that differ only in case (not the shouted one)."""
    return min(names, key=lambda n: (n == n.upper() and n != n.lower(), n))


def _link_text(s: str) -> str:
    return re.sub(r"[\[\]|]", " ", s)


# --- managed blocks ----------------------------------------------------------------------------

def _hash(content: str) -> str:
    # Ticking a box is not an edit.
    return hashlib.sha256(CHECKBOX.sub(r"\1[ ]", content.strip()).encode()).hexdigest()[:8]


def _block(name: str, content: str) -> str:
    content = content.strip()
    return f"<!-- omy:begin {name} h={_hash(content)} -->\n{content}\n<!-- omy:end {name} -->"


def _split(text: str) -> tuple[str, str]:
    """(frontmatter without the dashes, body)."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        if end >= 0:
            return text[4:end], text[end + 5:].lstrip("\n")
    return "", text.lstrip("\n")


def _groups(fm: str) -> list[tuple[str, str]]:
    out: list[list[str]] = []
    for line in fm.split("\n") if fm else []:
        if KEY.match(line):
            out.append([line.split(":", 1)[0], line])
        elif out:
            out[-1][1] += "\n" + line
    return [(k, t) for k, t in out]


def _fm_value(fm: str, key: str):
    for k, t in _groups(fm):
        if k == key:
            try:
                return json.loads(t.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _merge(body: str, blocks: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """The body with its blocks brought to `blocks`, the text around them
    kept. Returns (body, names of the blocks the user edited and were left)."""
    new = {n: c.strip() for n, c in blocks if c and c.strip()}
    order = [n for n, c in blocks if n in new]
    items: list[tuple[str | None, str]] = []
    conflicts, pos = [], 0
    for m in BLOCK.finditer(body):
        if m.start() > pos:
            items.append((None, body[pos:m.start()]))
        pos, name = m.end(), m.group("n")
        if _hash(m.group("c")) != m.group("h") and not (name in new and _hash(m.group("c")) == _hash(new[name])):
            conflicts.append(name)
            items.append((name, m.group(0)))
        elif name in new:
            items.append((name, _block(name, new[name])))
        # else: no longer generated, nobody touched it: gone
    if pos < len(body):
        items.append((None, body[pos:]))
    present = {n for n, _ in items if n}
    for name in order:
        if name in present:
            continue
        rendered = _block(name, new[name])
        at = None
        for prev in reversed(order[:order.index(name)]):
            at = next((i for i, (n, _) in enumerate(items) if n == prev), None)
            if at is not None:
                break
        if at is not None:
            items[at + 1:at + 1] = [(None, "\n\n"), (name, rendered)]
        elif items:
            items[0:0] = [(name, rendered), (None, "\n\n")]
        else:
            items.append((name, rendered))
        present.add(name)
    text = "".join(t for _, t in items).strip("\n")
    return (text + "\n" if text else ""), conflicts


def _user_text(text: str) -> str:
    """What the user wrote in a file: everything but frontmatter and blocks."""
    return BLOCK.sub("", _split(text)[1]).strip()


# --- sidecar helpers ---------------------------------------------------------------------------

def impegno_ids(sc: dict) -> list[str]:
    """Stable ids (^c...) of the commitments of a sidecar, in their order."""
    seen: Counter = Counter()
    out = []
    for it in sc.get("impegni_e_todo") or []:
        uid = it.get("uid")
        if isinstance(uid, str) and re.fullmatch(r"c?[0-9a-f]{8}", uid):  # stable, set by the organizer
            base = uid if uid.startswith("c") else "c" + uid
        else:  # sidecars from before uid
            base = "c" + hashlib.sha1(f"{sc.get('id')}|{(it.get('cosa') or '').casefold().strip()}".encode()).hexdigest()[:8]
        seen[base] += 1
        out.append(base if seen[base] == 1 else f"{base}-{seen[base]}")
    return out


def _hhmm(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def _secs(sc: dict, ref: str) -> int | None:
    p = (sc.get("paragrafi") or {}).get(ref)
    try:
        t0, t1 = datetime.fromisoformat(sc["ora_inizio"]), datetime.fromisoformat(p["inizio"])
        return max(0, int((t1 - t0).total_seconds()))
    except (TypeError, ValueError, KeyError):
        return None


def _ts(sc: dict, ref: str) -> str:
    t = _secs(sc, ref)
    if t is None:
        return ""
    return f"[{_hhmm(sc['paragrafi'][ref]['inizio'])}](omy://session/{sc['id']}?t={t}&p={ref})"


def _refs(sc: dict, refs) -> str:
    return " ".join(x for x in (_ts(sc, r) for r in refs or []) if x)


def _low(item: dict) -> bool:
    m = (item.get("conf") or {}).get("minima")
    return m is not None and m < LOW


def _due(it: dict) -> str | None:
    return it.get("scadenza") or it.get("scadenza_testo")


def _order(sc: dict) -> tuple[str, str]:
    return (sc.get("ora_inizio") or "", sc.get("id") or "")


class _Vault:
    """One operation's view of the vault: the sidecars read, the index and the
    conflicts, saved by flush()."""

    def __init__(self):
        self.people = load_people()
        self.me = my_name()
        self.docs: dict[str, tuple[Path, dict]] | None = None
        self.names: dict[str, Names] = {}
        self.ents: dict[str, dict] = {}
        self.conflicts = _json(CONFLICTS, {})
        self.conflicts0 = json.dumps(self.conflicts, sort_keys=True)
        self.index_loaded = INDEX.is_file()
        self.index = _json(INDEX, None)
        if not isinstance(self.index, dict) or not {"notes", "entities", "pages"} <= set(self.index):
            self.index = {"notes": {}, "entities": {}, "pages": {}}
            self.index_loaded = False
        self.index0 = _dump(self.index)
        self._person_files: dict[str, set[Path]] | None = None

    # -- sidecars

    def all(self) -> dict[str, tuple[Path, dict]]:
        if self.docs is None:
            self.docs = {}
            for path in sorted(NOTES.glob("*/*/*/*.json")):
                sc = _json(path, None)
                if isinstance(sc, dict) and sc.get("id"):
                    self.docs[sc["id"]] = (path, sc)
        return self.docs

    def doc(self, sid: str) -> tuple[Path, dict] | None:
        return self.all().get(sid)

    def names_of(self, sid: str) -> Names:
        if sid not in self.names:
            self.names[sid] = Names(self.all()[sid][1], self.people, self.me)
        return self.names[sid]

    def save_sidecar(self, sid: str, sc: dict) -> None:
        path = self.all()[sid][0]
        self.docs[sid] = (path, sc)
        self.names.pop(sid, None)
        self.ents.pop(sid, None)
        _write_atomic(path, json.dumps(sc, ensure_ascii=False, indent=2) + "\n")

    def sorted_sids(self, sids) -> list[str]:
        return sorted(sids, key=lambda s: _order(self.all()[s][1]))

    def link(self, sid: str) -> str:
        path, sc = self.all()[sid]
        rel = path.relative_to(BRAIN).with_suffix("").as_posix()
        return f"[[{rel}|{_link_text(self.names_of(sid).rewrite(sc.get('titolo')) or sid)}]]"

    # -- files with blocks

    def write(self, path: Path, fm: list[tuple[str, str]], blocks: list[tuple[str, str]], extra: str = "") -> bool:
        """Write path: our frontmatter keys, our blocks, the user's text kept."""
        old = _read(path)
        old_fm, body = _split(old) if old is not None else ("", "")
        body, conflicts = _merge(body, blocks)
        if extra and extra not in body:
            body = (body.rstrip("\n") + "\n\n" if body else "") + extra + "\n"
        mine = {k for k, _ in fm}
        lines = [f"{k}: {v}" for k, v in fm] + [t for k, t in _groups(old_fm) if k not in mine]
        text = ("---\n" + "\n".join(lines) + "\n---\n\n" if lines else "") + body
        rel = path.relative_to(BRAIN).as_posix()
        if conflicts:
            self.conflicts[rel] = sorted(conflicts)
        else:
            self.conflicts.pop(rel, None)
        return _write_atomic(path, text)

    def retire(self, path: Path, own: set[str]) -> None:
        """What was generated in a page that has no more reason to exist is
        removed; the file too unless the user wrote in it."""
        old = _read(path)
        if old is None:
            return
        fm, body = _split(old)
        body, conflicts = _merge(body, [])
        kept = [t for k, t in _groups(fm) if k not in own]
        rel = path.relative_to(BRAIN).as_posix()
        if not body.strip() and not kept:
            path.unlink()
            self.conflicts.pop(rel, None)
            return
        if conflicts:
            self.conflicts[rel] = sorted(conflicts)
        else:
            self.conflicts.pop(rel, None)
        _write_atomic(path, ("---\n" + "\n".join(kept) + "\n---\n\n" if kept else "") + body)

    def flush(self) -> None:
        if json.dumps(self.conflicts, sort_keys=True) != self.conflicts0:
            _write_atomic(CONFLICTS, _dump(self.conflicts))
        if _dump(self.index) != self.index0:
            _write_atomic(INDEX, _dump(self.index))

    # -- ticks

    def sync_ticks(self, sids=None) -> None:
        """The ticks of impegni.md and of the notes' files become stato in the sidecars: ticked ->
        "fatto", unticked -> "aperto". Per id the most recently modified file that shows it wins
        (impegni.md on a tie), so a tick or an untick made in either place sticks."""
        sids = list(self.all()) if sids is None else [s for s in sids if s in self.all()]
        files = [BRAIN / "impegni.md"] + [self.all()[sid][0].with_suffix(".md") for sid in sids]
        state: dict[str, tuple[int, bool]] = {}  # id -> (mtime_ns, ticked)
        for f in files:
            text = _read(f)
            if not text:
                continue
            m = f.stat().st_mtime_ns
            for iid in UNTICKED.findall(text):
                if iid not in state or m > state[iid][0]:
                    state[iid] = (m, False)
            for iid in TICKED.findall(text):
                if iid not in state or m > state[iid][0] or (m == state[iid][0]):
                    state[iid] = (m, True)
        if not state:
            return
        for sid in sids:
            sc = self.all()[sid][1]
            changed = False
            for it, iid in zip(sc.get("impegni_e_todo") or [], impegno_ids(sc)):
                if iid not in state:
                    continue
                want = "fatto" if state[iid][1] else "aperto"
                if (it.get("stato") == "fatto") != (want == "fatto"):
                    it["stato"] = want
                    changed = True
            if changed:
                self.save_sidecar(sid, sc)

    # -- lines

    def impegno_line(self, sid: str, it: dict, iid: str, with_note: bool = False) -> str:
        sc, names = self.all()[sid][1], self.names_of(sid)
        who = names.chi(it) or "?"
        verso = names.verso_chi(it)
        if verso:
            who += f" → {verso}"
        due = _due(it)
        done = it.get("stato") == "fatto"
        parts = [f"- [{'x' if done else ' '}]", (WARN if _low(it) else "") + f"{who}: {names.rewrite(it.get('cosa')) or ''}"]
        line = " ".join(parts[:1]) + " " + parts[1]
        if due:
            line += f" (entro {due})"
        if _refs(sc, it.get("refs")):
            line += " " + _refs(sc, it.get("refs"))
        if with_note:
            line += " " + self.link(sid)
        return f"{line} ^{iid}"

    # -- note

    def note_blocks(self, sid: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        path, sc = self.all()[sid]
        names = self.names_of(sid)
        rw = names.rewrite
        ents = self.entities_of(sid)

        def pages(cat: str) -> list[str]:
            return sorted({f"- [[{cat}/{_safe(_display(e['names']))}]]" for k, e in ents.items() if e["cat"] == cat})

        def items(rows) -> str:
            return "\n".join(rows)

        rows = []
        for p in sc.get("punti_chiave") or []:
            rows.append(f"- {WARN if _low(p) else ''}{rw(p.get('testo')) or ''} {_refs(sc, p.get('refs'))}".rstrip())
        impegni = [self.impegno_line(sid, it, iid) for it, iid in zip(sc.get("impegni_e_todo") or [], impegno_ids(sc))]
        dates = []
        for d in sc.get("date_ed_eventi_citati") or []:
            when = d.get("testo_data") or ""
            if d.get("data"):
                when += f" → {d['data']}" if when else d["data"]
            dates.append(f"- {WARN if _low(d) else ''}{when}: {rw(d.get('evento')) or ''} {_refs(sc, d.get('refs'))}".rstrip())
        heard = []
        for a in sc.get("ascoltato") or []:
            heard.append(f"- {(a['app'] + ': ') if a.get('app') else ''}{rw(a.get('riassunto')) or ''} "
                         f"{_refs(sc, a.get('refs'))}".rstrip())
        parts = []
        parti = sc.get("parti") or []
        if len(parti) > 1:
            for p in parti:
                parts.append(f"- **{p.get('da')}–{p.get('a')}** {_refs(sc, [p.get('da')])} {rw(p.get('riassunto')) or ''}"
                             .replace("  ", " "))
        links = []
        for c in sc.get("collegamenti") or []:
            if c.get("nota") in self.all():
                links.append(f"- {self.link(c['nota'])} — {self.motivo(c.get('motivo'))}")
        title = (rw(sc.get("titolo")) or "Senza titolo")
        sub = f"*{sc.get('data', '')} {_hhmm(sc.get('ora_inizio'))}–{_hhmm(sc.get('ora_fine'))}*"
        if sc.get("qualita"):
            sub += f" · qualità: {sc['qualita']}"
        blocks = [
            ("titolo", f"# {title}\n\n{sub}"),
            ("riassunto", "## Riassunto\n\n" + (rw(sc.get("riassunto")) or "") if sc.get("riassunto") else ""),
            ("punti-chiave", "## Punti chiave\n\n" + items(rows) if rows else ""),
            ("impegni", "## Impegni\n\n" + items(impegni) if impegni else ""),
            ("date", "## Date ed eventi\n\n" + items(dates) if dates else ""),
        ]
        for cat, label in (("persone", "Persone"), ("luoghi", "Luoghi"), ("organizzazioni", "Organizzazioni"),
                           ("progetti", "Progetti")):
            blocks.append((cat, f"## {label}\n\n" + items(pages(cat)) if pages(cat) else ""))
        blocks += [
            ("ascoltato", "## Ascoltato\n\n" + items(heard) if heard else ""),
            ("parti", "## Parti\n\n" + items(parts) if parts else ""),
            ("collegamenti", "## Collegamenti\n\n" + items(links) if links else ""),
        ]
        people = sorted({f'"[[persone/{_safe(p["new"])}]]"' for p in names.participants if p["new"]})
        conf = (sc.get("confidenza") or {}).get("media")
        fm = [("id", json.dumps(sc["id"])), ("data", sc.get("data", "")),
              ("ora_inizio", sc.get("ora_inizio") or ""), ("ora_fine", sc.get("ora_fine") or ""),
              ("partecipanti", "[" + ", ".join(people) + "]")]
        if sc.get("luogo") and sc["luogo"].get("name"):
            fm.append(("luogo", json.dumps(f"[[luoghi/{_safe(rw(sc['luogo']['name']))}]]", ensure_ascii=False)))
        fm += [("tag", json.dumps(sc.get("tag") or [], ensure_ascii=False)),
               ("qualita", json.dumps(sc.get("qualita"), ensure_ascii=False))]
        if conf is not None:
            fm.append(("confidenza", str(conf)))
        fm.append(("da_rivedere", "true" if sc.get("da_rivedere") else "false"))
        return fm, blocks

    def motivo(self, motivo: str | None) -> str:
        kind, _, val = (motivo or "").partition(":")
        if kind == "persona":
            return "persona " + (current_name(val, val, self.people, self.me) or val)
        return {"progetto": "progetto ", "entita": ""}.get(kind, "") + (val or motivo or "")

    def render_note(self, sid: str) -> Path:
        if sid not in self.all():
            raise FileNotFoundError(f"no note for session {sid}")
        self.sync_ticks([sid])
        fm, blocks = self.note_blocks(sid)
        md = self.all()[sid][0].with_suffix(".md")
        self.write(md, fm, blocks)
        return md

    # -- entities

    def entities_of(self, sid: str) -> dict[str, dict]:
        """key ("persone/anna") -> {"cat", "names", "raw", "ids", "mentions"} of the note."""
        if sid in self.ents:
            return self.ents[sid]
        sc, names = self.all()[sid][1], self.names_of(sid)
        rw = names.rewrite
        ents: dict[str, dict] = {}
        related = []  # (refs, text, low) of the items an entity can be a mention in
        for p in sc.get("punti_chiave") or []:
            related.append((p.get("refs") or [], rw(p.get("testo")) or "", _low(p)))
        for it in sc.get("impegni_e_todo") or []:
            related.append((it.get("refs") or [], rw(it.get("cosa")) or "", _low(it)))
        for d in sc.get("date_ed_eventi_citati") or []:
            related.append((d.get("refs") or [], rw(d.get("evento")) or "", _low(d)))

        def add(cat, name, raw, pid, mentions):
            if not name:
                return
            e = ents.setdefault(f"{cat}/{name.casefold()}",
                                {"cat": cat, "names": set(), "raw": set(), "ids": set(), "mentions": []})
            e["names"].add(name)
            if raw and raw != name:
                e["raw"].add(raw)
            if pid:
                e["ids"].add(pid)
            e["mentions"] += mentions

        for p in names.participants:
            add("persone", p["new"], p["old"], p["person"], [{"refs": [], "text": "partecipante", "low": False}])
        for cat, label in (("persone", "persone"), ("luoghi", "luoghi"), ("organizzazioni", "organizzazioni"),
                           ("progetti", "progetti")):
            for e in (sc.get("entita") or {}).get(label) or []:
                refs = e.get("refs") or []
                ms = [{"refs": r, "text": t, "low": lw} for r, t, lw in related if set(r) & set(refs)]
                if not ms:
                    ms = [{"refs": refs, "text": "", "low": _low(e)}]
                if cat == "persone":
                    add(cat, names.name(e.get("person_id"), e.get("nome")), e.get("nome"), e.get("person_id"), ms)
                else:
                    add(cat, rw(e.get("nome")), e.get("nome"), None, ms)
        if (sc.get("luogo") or {}).get("name"):
            name = rw(sc["luogo"]["name"])
            if f"luoghi/{name.casefold()}" not in ents:
                add("luoghi", name, sc["luogo"]["name"], None, [{"refs": [], "text": "luogo della nota", "low": False}])
        self.ents[sid] = ents
        return ents

    def person_files(self) -> dict[str, set[Path]]:
        if self._person_files is None:
            self._person_files = {}
            for p in sorted((BRAIN / "persone").glob("*.md")):
                text = _read(p)
                if text is None:
                    continue
                v = _fm_value(_split(text)[0], "person_id")
                for pid in [v] if isinstance(v, str) else v or []:
                    self._person_files.setdefault(pid, set()).add(p)
        return self._person_files

    def build_index(self) -> None:
        self.index = {"notes": {}, "entities": {}, "pages": {}}
        for sid in self.all():
            keys = sorted(self.entities_of(sid))
            self.index["notes"][sid] = keys
            for k in keys:
                self.index["entities"].setdefault(k, []).append(sid)
        self.index["entities"] = {k: self.sorted_sids(v) for k, v in self.index["entities"].items()}

    def render_entities(self, sids: list[str]) -> list[Path]:
        idx = self.index
        if not self.index_loaded:  # no index: a full rebuild
            self.build_index()
            idx = self.index
            affected = set(idx["entities"])
        else:
            affected = set()
            for sid in sids:
                old = idx["notes"].pop(sid, [])
                for k in old:
                    if sid in idx["entities"].get(k, []):
                        idx["entities"][k].remove(sid)
                new = sorted(self.entities_of(sid)) if sid in self.all() else []
                for k in new:
                    idx["entities"].setdefault(k, []).append(sid)
                if new:
                    idx["notes"][sid] = new
                affected |= set(old) | set(new)
            for k in affected:
                idx["entities"][k] = self.sorted_sids(set(idx["entities"].get(k, [])))
        self.index_loaded = True
        written = []
        for key in sorted(affected, key=lambda k: (not idx["entities"].get(k), k)):  # pages with notes first
            p = self.page(key, idx["entities"].get(key) or [])
            if p:
                written.append(p)
            else:
                idx["entities"].pop(key, None)
        return sorted(set(written))

    def page(self, key: str, sids: list[str]) -> Path | None:
        cat = key.split("/", 1)[0]
        idx_path = self.index["pages"].get(key)
        cands = [BRAIN / idx_path] if idx_path else []
        own = {"tipo", "person_id", "alias"}
        if not sids:
            for p in cands:
                self.retire(p, own)
            self.index["pages"].pop(key, None)
            return None
        docs = [(sid, self.entities_of(sid)[key]) for sid in sids]
        display = _display({n for _, e in docs for n in e["names"]})
        target = BRAIN / cat / f"{_safe(display)}.md"
        ids = sorted({i for _, e in docs for i in e["ids"]})
        if cat == "persone":
            for i in ids:
                cands += sorted(self.person_files().get(i, ()))
        aliases = {n for _, e in docs for n in e["raw"]}
        extra = []
        for old in dict.fromkeys(c for c in cands if c != target and c.exists()):
            if not target.exists():
                _mkdirs(target.parent)
                os.replace(old, target)
            else:
                text = _read(old) or ""
                extra.append(_user_text(text))
                aliases |= set(_fm_value(_split(text)[0], "alias") or [])
                old.unlink()
                self.conflicts.pop(old.relative_to(BRAIN).as_posix(), None)
            aliases.add(old.stem)
            for fs in self.person_files().values():
                if old in fs:
                    fs.discard(old)
                    fs.add(target)
        aliases |= set(_fm_value(_split(_read(target) or "")[0], "alias") or [])
        aliases = sorted(a for a in aliases if a.casefold() != display.casefold()) if cat == "persone" else []
        fm = [("tipo", TIPO[cat])]
        if ids:
            fm.append(("person_id", json.dumps(ids[0] if len(ids) == 1 else ids, ensure_ascii=False)))
        if aliases:
            fm.append(("alias", json.dumps(aliases, ensure_ascii=False)))
        mentions, notes, commitments = [], [], []
        for sid, e in docs:
            path, sc = self.all()[sid]
            link = self.link(sid)
            notes.append(f"- {sc.get('data', '')} {_hhmm(sc.get('ora_inizio'))} {link}")
            for m in e["mentions"]:
                first = (_secs(sc, m["refs"][0]) or 0) if m["refs"] else 0
                line = f"- {sc.get('data', '')}"
                if _refs(sc, m["refs"]):
                    line += " " + _refs(sc, m["refs"])
                line += f" — {WARN if m['low'] else ''}{m['text']}" if m["text"] else ""
                mentions.append((_order(sc), first, m["text"], f"{line} · {link}"))
            if cat == "persone":
                names = self.names_of(sid)
                for it, iid in zip(sc.get("impegni_e_todo") or [], impegno_ids(sc)):
                    chi, verso = names.chi(it) or "", names.verso_chi(it) or ""
                    if key.split("/", 1)[1] in (chi.casefold(), verso.casefold()):
                        line = self.impegno_line(sid, it, iid, with_note=True)
                        line = re.sub(r"^- \[[ x]\]", "- ✅" if it.get("stato") == "fatto" else "-", line)
                        commitments.append(re.sub(r" \^c[0-9a-f]{8}(-\d+)?$", "", line))
        blocks = [("titolo", f"# {display}"),
                  ("note", "## Note\n\n" + "\n".join(notes)),
                  ("impegni", "## Impegni\n\n" + "\n".join(commitments) if commitments else ""),
                  ("menzioni", "## Menzioni\n\n" + "\n".join(dict.fromkeys(m[3] for m in sorted(mentions))))]
        self.write(target, fm, blocks, extra="\n\n".join(x for x in extra if x))
        self.index["pages"][key] = target.relative_to(BRAIN).as_posix()
        return target

    # -- links

    def link_keys(self, sid: str) -> dict[str, str]:
        """reason -> priority-ordered motive of the note: persona:<id> / progetto:<name> / entita:<name>."""
        sc, names = self.all()[sid][1], self.names_of(sid)
        keys = {}
        for pid in sorted(names.person_ids() - {"me"}):
            keys[f"persona:{pid}"] = f"persona:{pid}"
        for label, kind in (("progetti", "progetto"), ("organizzazioni", "entita")):
            for e in (sc.get("entita") or {}).get(label) or []:
                if e.get("nome"):
                    keys[f"{kind}:{names.rewrite(e['nome']).casefold()}"] = f"{kind}:{names.rewrite(e['nome'])}"
        return keys

    def update_links(self, sids: list[str]) -> set[str]:
        """Compute and save `collegamenti`; the sids of the notes changed (for the notes to be rendered)."""
        keys = {sid: self.link_keys(sid) for sid in self.all()}
        by_key: dict[str, set[str]] = {}
        for sid, ks in keys.items():
            for k in ks:
                by_key.setdefault(k, set()).add(sid)
        wanted = {s for s in sids if s in self.all()}
        for sid in list(wanted):  # the notes that share something gain a link to these ones too
            for k in keys[sid]:
                wanted |= by_key[k]
        changed = set()
        for sid in sorted(wanted):
            peers: dict[str, str] = {}
            for k, motive in sorted(keys[sid].items(), key=lambda kv: (not kv[0].startswith("persona:"),
                                                                      not kv[0].startswith("progetto:"), kv[0])):
                for other in by_key[k] - {sid}:
                    peers.setdefault(other, motive)
            recent = sorted(peers, key=lambda s: _order(self.all()[s][1]), reverse=True)[:MAX_LINKS]
            links = [{"nota": s, "motivo": peers[s]} for s in recent]
            sc = self.all()[sid][1]
            if (sc.get("collegamenti") or []) != links:
                sc["collegamenti"] = links
                self.save_sidecar(sid, sc)
                changed.add(sid)
        return changed

    # -- indexes

    def render_indexes(self) -> None:
        self.sync_ticks()
        docs = self.all()
        sids = self.sorted_sids(docs)
        # giorni/
        days: dict[str, list[str]] = {}
        for sid in sids:
            days.setdefault(docs[sid][1].get("data") or "", []).append(sid)
        for day, ds in sorted(days.items()):
            if not day:
                continue
            rows = []
            for sid in ds:
                sc = docs[sid][1]
                place = self.names_of(sid).rewrite((sc.get("luogo") or {}).get("name"))
                rows.append(f"- {_hhmm(sc.get('ora_inizio'))}–{_hhmm(sc.get('ora_fine'))} {self.link(sid)}"
                            + (f" ({place})" if place else ""))
            self.write(BRAIN / "giorni" / f"{day}.md", [("data", day)],
                       [("titolo", f"# {day}"), ("note", "## Note\n\n" + "\n".join(rows))])
        for p in sorted((BRAIN / "giorni").glob("*.md")):
            if p.stem not in days:
                self.retire(p, {"data"})
        # impegni.md
        groups: dict[str, list[str]] = {}
        for sid in sids:
            sc = docs[sid][1]
            for it, iid in zip(sc.get("impegni_e_todo") or [], impegno_ids(sc)):
                if it.get("stato") == "fatto":
                    g = "fatti"
                elif it.get("scadenza"):
                    g = f"scadenza-{it['scadenza']}"
                else:
                    g = "senza-scadenza"
                groups.setdefault(g, []).append(self.impegno_line(sid, it, iid, with_note=True))
        heads = {"senza-scadenza": "Senza scadenza", "fatti": "Fatti"}
        order = sorted(g for g in groups if g.startswith("scadenza-")) + \
            [g for g in ("senza-scadenza", "fatti") if g in groups]
        blocks = [("titolo", "# Impegni")]
        for g in order:
            head = heads.get(g) or "Entro " + g.removeprefix("scadenza-")
            blocks.append((g, f"## {head}\n\n" + "\n".join(groups[g])))
        self.write(BRAIN / "impegni.md", [], blocks)
        # da-rivedere.md
        rows = []
        for sid in sids:
            sc = docs[sid][1]
            names = self.names_of(sid)
            for r in sc.get("da_rivedere") or []:
                campo = r.get("campo") or ""
                item = _field_item(sc, campo)
                text = ""
                refs = ""
                if item:
                    text = names.rewrite(item.get("testo") or item.get("cosa") or item.get("riassunto")
                                         or item.get("evento") or item.get("nome") or "") or ""
                    refs = _refs(sc, item.get("refs"))
                rows.append(f"- {sc.get('data', '')} {self.link(sid)} — {campo}: {r.get('motivo') or ''}"
                            + (f" — {text}" if text else "") + (f" {refs}" if refs else ""))
        self.write(BRAIN / "da-rivedere.md", [],
                   [("titolo", "# Da rivedere"), ("elenco", "\n".join(rows))])


def _field_item(sc: dict, campo: str) -> dict | None:
    m = re.fullmatch(r"(\w+)(?:\.(\w+))?\[(\d+)\]", campo)
    if not m:
        return None
    seq = sc.get(m.group(1))
    if m.group(2) and isinstance(seq, dict):
        seq = seq.get(m.group(2))
    try:
        item = seq[int(m.group(3))]
    except (TypeError, IndexError, KeyError):
        return None
    return item if isinstance(item, dict) else None


# --- public API ---------------------------------------------------------------------------------

def render_note(session_id: str) -> Path:
    """The Markdown of the note, from its sidecar, with the names of today."""
    v = _Vault()
    path = v.render_note(session_id)
    v.flush()
    return path


def render_entities(session_ids: list[str]) -> list[Path]:
    """The pages of the people, places, organizations and projects the notes touch
    (those they used to touch too): same as a full rebuild."""
    v = _Vault()
    out = v.render_entities(list(session_ids))
    v.flush()
    return out


def render_indexes() -> None:
    v = _Vault()
    v.render_indexes()
    v.flush()


def update_links(session_ids: list[str]) -> None:
    """`collegamenti` of the notes (and of the ones that share a person, project
    or organization with them): saved in the sidecar and rendered."""
    v = _Vault()
    changed = v.update_links(list(session_ids))
    for sid in sorted(changed | {s for s in session_ids if s in v.all()}):
        v.render_note(sid)
    v.flush()


def rerender_people(person_ids: list[str] | None) -> list[str]:
    """After a rename, merge or delete: every note involving those people (all if
    None), their pages (the old ones moved or merged) and the indexes. Returns the session ids."""
    v = _Vault()
    want = None if person_ids is None else set(person_ids)
    sids = sorted(sid for sid in v.all() if want is None or v.names_of(sid).person_ids() & want)
    changed = v.update_links(sids)
    sids = sorted(set(sids) | changed)
    for sid in sids:
        v.render_note(sid)
    v.render_entities(sids)
    v.render_indexes()
    v.flush()
    return sids
