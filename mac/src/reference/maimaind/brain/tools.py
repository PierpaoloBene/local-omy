"""The retrieval tools the model calls (native function calling).

Every item a tool returns gets a short reference, R<n> for a paragraph and S<n>
for a session, valid for the whole conversation. The model cites those, never
the long ids, so it cannot make one up: an answer can only cite what a tool
actually returned (ask.py checks).

The channels the model may search are not its choice alone: Constraints, set by
the code from the question, always win (only "me" for "did I ever say…", media
only when the question is about something heard).
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field

from brain.dates import short

CHANNELS = ("me", "said", "heard")
DEFAULT_CHANNELS = ("me", "said")
MAX_LIMIT = 30
EXCERPT = 300
SESSION_PAGE = 40
SESSION_CHARS = 5000
# What the tools may return in one conversation, in characters (~4,000 tokens):
# the prefill runs at ~160 tokens/s on an M2 Pro, so every result costs seconds.
BUDGET_CHARS = 14000
# Less than this left: not worth another result.
MIN_RESULT = 500
# search: candidates from words (BM25) and from meaning (vectors) are fused by
# rank (reciprocal rank fusion, k=60), then the best RERANK_CANDIDATES are read
# again, each with the question, by the reranker (a cross-encoder).
RRF_K = 60
RERANK_CANDIDATES = 30
RERANK_CHARS = 700
# Reranker relevance (0-1) below this: not about the question.
MIN_RELEVANCE = 0.05
# At most this many excerpts of the same document in one search.
PER_DOCUMENT = 2

# Words too common to search for (Italian and English).
STOPWORDS = set("""
a ad al allo ai agli all alla alle che chi ci col con da dal dallo dai dagli dall dalla dalle de del dello dei
degli dell della delle di e ed gli ha hai ho i il in io la le lei lo loro lui ma me mi ne nei negli nel nello
nell nella nelle no non o per piu poi quale quando quanto quel quello questa queste questi questo se si sono
su sul sullo sui sugli sull sulla sulle ti tra fra tu un una uno vi mai cosa come dove detto parlato
the of and or to in on at for with did i me my have has ever what when who about
""".split())


# Reference prefixes: paragraph, conversation, commitment, document.
PREFIX = {"paragraph": "R", "session": "S", "commitment": "C", "document": "D"}


@dataclass
class Constraints:
    channels: tuple[str, ...] = DEFAULT_CHANNELS
    note: str = ""


@dataclass
class Ref:
    ref: str
    kind: str  # a key of PREFIX
    id: str
    row: dict


@dataclass
class Refs:
    """The references handed to the model in one conversation."""
    by_ref: dict[str, Ref] = field(default_factory=dict)
    by_id: dict[tuple[str, str], str] = field(default_factory=dict)

    def add(self, kind: str, id_: str, row: dict) -> str:
        key = (kind, id_)
        if key not in self.by_id:
            prefix = PREFIX[kind]
            n = sum(1 for r in self.by_ref.values() if r.kind == kind) + 1
            self.by_id[key] = f"{prefix}{n}"
            self.by_ref[self.by_id[key]] = Ref(self.by_id[key], kind, id_, row)
        return self.by_id[key]


def fold(text: str) -> str:
    """Lower case, accents removed: for matching names."""
    return "".join(c for c in unicodedata.normalize("NFD", text.casefold()) if unicodedata.category(c) != "Mn")


def words(text: str) -> list[str]:
    return re.findall(r"\w+", fold(text))


def fts_query(text: str, mode: str) -> str | None:
    ws = words(text)
    if mode == "frase":
        return '"' + " ".join(ws) + '"' if ws else None
    terms = [w for w in ws if w not in STOPWORDS] or ws
    # Prefix match on longer words: trasloco finds traslochi, traslocare.
    terms = [f'"{w[:-1]}"*' if len(w) >= 5 else f'"{w}"' for w in terms]
    return (" AND " if mode == "tutte" else " OR ").join(terms) if terms else None


class Tools:
    def __init__(self, conn: sqlite3.Connection, constraints: Constraints | None = None, refs: Refs | None = None,
                 embedder=None, reranker=None):
        self.conn = conn
        self.embedder = embedder
        self.reranker = reranker
        self.constraints = constraints or Constraints()
        self.refs = refs or Refs()
        self.calls: list[dict] = []
        self.budget = BUDGET_CHARS
        # Tools added when available (documents): name -> (schema, handler).
        self.extra: dict[str, tuple[dict, object]] = {}
        # Set by the documents (docs.setup_tools): their candidates and lines for search.
        self.doc_search = None

    def register(self, name: str, description: str, props: dict, required: list[str], handler) -> None:
        self.extra[name] = ({"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required}}}, handler)

    # --- schemas -------------------------------------------------------------------

    def schemas(self) -> list[dict]:
        filters = {
            "person": {"type": "string", "description": "nome di una persona, oppure 'me' per l'utente"},
            "date_from": {"type": "string", "description": "AAAA-MM-GG, compreso"},
            "date_to": {"type": "string", "description": "AAAA-MM-GG, compreso"},
            "channel": {"type": "array", "items": {"type": "string", "enum": list(CHANNELS)},
                        "description": "me = detto dall'utente; said = detto da altri presenti o in chiamata; "
                                       "heard = video, TV, podcast ascoltati. Default: me e said"},
            "place": {"type": "string", "description": "nome di un luogo (es. Casa)"},
        }

        def fn(name: str, description: str, props: dict, required: list[str]) -> dict:
            return {"type": "function", "function": {
                "name": name, "description": description,
                "parameters": {"type": "object", "properties": props, "required": required}}}

        return [
            fn("search", "La ricerca principale: trova per parole e per significato, insieme, nelle conversazioni "
               "(paragrafi [R…], riassunti [S…]) e nei documenti del Mac [D…], e mette prima i risultati più "
               "pertinenti. Usala con una domanda o qualche parola chiave.",
               {"query": {"type": "string", "description": "la domanda, o le parole chiave"},
                **filters, "limit": {"type": "integer", "description": "max 15, default 8"}},
               ["query"]),
            fn("search_text", "Cerca parole o una frase esatta nelle trascrizioni. Restituisce paragrafi [R…].",
               {"query": {"type": "string", "description": "parole chiave, o la frase esatta"},
                "mode": {"type": "string", "enum": ["tutte", "qualsiasi", "frase"],
                         "description": "tutte le parole (default), qualsiasi parola, frase esatta"},
                **filters, "limit": {"type": "integer", "description": f"max {MAX_LIMIT}, default 15"}},
               ["query"]),
            fn("filter", "Elenca le conversazioni (sessioni [S…]) che rispettano i filtri, senza cercare parole. "
               "Utile per 'con chi ho parlato', 'cosa ho fatto quel giorno'.",
               {**filters, "limit": {"type": "integer", "description": f"max {MAX_LIMIT}, default 15"}}, []),
            fn("get_session", "Legge i paragrafi di una conversazione [S…], a pagine.",
               {"session": {"type": "string", "description": "riferimento S… o id della sessione"},
                "offset": {"type": "integer", "description": "da quale paragrafo, default 0"}},
               ["session"]),
            fn("get_person", "Chi è una persona: quanto e quando ci hai parlato, ultime conversazioni [S…]. "
               "Se il nome non è una persona registrata, restituisce i paragrafi in cui il nome compare.",
               {"name": {"type": "string"}}, ["name"]),
            fn("list_commitments", "Impegni e promesse estratti dalle conversazioni [C…]: chi ha promesso cosa a "
               "chi, con scadenza e stato.",
               {"owner": {"type": "string", "description": "chi si è impegnato: 'me' per l'utente, 'altri', o un nome"},
                "counterpart": {"type": "string", "description": "verso chi (un nome, o 'me')"},
                "date_from": filters["date_from"], "date_to": filters["date_to"],
                "status": {"type": "string", "enum": ["aperti", "chiusi", "tutti"], "description": "default tutti"}},
               []),
        ] + [schema for schema, _ in self.extra.values()]

    # --- dispatch --------------------------------------------------------------------

    def call(self, name: str, arguments: str | dict) -> str:
        """Run a tool for the model; errors go back to the model as text."""
        record = {"name": name, "arguments": arguments, "valid": True}
        self.calls.append(record)
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
            record["arguments"] = args
            fn = {"search": self.search, "search_text": self.search_text, "filter": self.filter,
                  "get_session": self.get_session,
                  "get_person": self.get_person, "list_commitments": self.list_commitments,
                  **{k: h for k, (_, h) in self.extra.items()}}.get(name)
            if fn is None:
                raise ValueError(f"strumento sconosciuto: {name}")
            if self.budget < MIN_RESULT:
                return "Hai già letto abbastanza: rispondi ora con ciò che hai trovato."
            text = fn(**args)
            if len(text) > self.budget:
                # Whole lines only: a reference cut in half would be useless.
                text = text[:self.budget].rsplit("\n", 1)[0] + "\n(risultati troncati)"
                self.budget = 0
            else:
                self.budget -= len(text)
            return text
        except (ValueError, TypeError) as e:
            record["valid"] = False
            return f"Errore: {e}"

    # --- filters ---------------------------------------------------------------------

    def _channels(self, requested) -> tuple[str, ...]:
        allowed = self.constraints.channels
        if isinstance(requested, str):
            requested = [requested]
        if not requested:
            return allowed
        chosen = tuple(c for c in allowed if c in requested)
        return chosen or allowed

    def resolve_person(self, name: str) -> dict | None:
        key = fold(name.strip())
        if key in ("me", "io", "i", "myself"):
            return dict(self.conn.execute("SELECT * FROM people WHERE id = 'me'").fetchone() or
                        {"id": "me", "name": "Io"})
        people = [dict(r) for r in self.conn.execute("SELECT * FROM people")]
        for test in (lambda n: n == key, lambda n: n.startswith(key), lambda n: key in n,
                     lambda n: all(w in n.split() for w in key.split())):
            found = [p for p in people if test(fold(p["name"]))]
            if len(found) == 1:
                return found[0]
        by_id = [p for p in people if p["id"] == name]
        return by_id[0] if by_id else None

    def _where(self, person=None, date_from=None, date_to=None, channel=None, place=None,
               alias: str = "p") -> tuple[list[str], list, str | None]:
        where, params, note = [], [], None
        channels = self._channels(channel)
        where.append(f"{alias}.channel IN ({', '.join('?' for _ in channels)})")
        params += channels
        if person:
            p = self.resolve_person(person)
            if p is None:
                raise ValueError(f"nessuna persona registrata con il nome {person!r}: prova search_text con il nome")
            if p["id"] == "me":
                where.append(f"{alias}.person = 'me'")
            else:
                # Sessions with that person: their paragraphs and the others' around them.
                where.append(f"{alias}.session IN (SELECT session FROM paragraphs WHERE person = ?)")
                params.append(p["id"])
            note = f"persona: {p['name']}"
        for value, op in ((date_from, ">="), (date_to, "<=")):
            if value:
                if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(value)):
                    raise ValueError(f"data non valida {value!r}: usa AAAA-MM-GG")
                where.append(f"{alias}.day {op} ?")
                params.append(value)
        if place:
            where.append(f"{alias}.place_name LIKE ?")
            params.append(f"%{place}%")
        return where, params, note

    # --- formatting ------------------------------------------------------------------

    def _session_ref(self, session_id: str) -> str:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self.refs.add("session", session_id, dict(row) if row else {"id": session_id})

    def _paragraph_line(self, r: sqlite3.Row) -> str:
        ref = self.refs.add("paragraph", r["id"], dict(r))
        sref = self._session_ref(r["session"])
        who = r["speaker"] or "?"
        if r["channel"] == "heard":
            who = f"MEDIA {r['via_app'] or ''}".strip()
        elif r["via_kind"] == "call":
            who += f" (chiamata {r['via_app'] or ''})".rstrip()
        text = r["text"] if len(r["text"]) <= EXCERPT else r["text"][:EXCERPT] + "…"
        where = f" · {r['place_name']}" if r["place_name"] else ""
        return f"[{ref}] {short(r['day'])} {r['start'][11:16]} · {who} · conversazione {sref}{where}: {text}"

    def _session_line(self, s: sqlite3.Row) -> str:
        ref = self.refs.add("session", s["id"], dict(s))
        speakers = ", ".join(json.loads(s["speakers"])) or "?"
        kinds = {"room": "di persona", "call": "chiamata", "media": "media"}
        kind = ", ".join(kinds.get(k, k) for k in json.loads(s["kinds"]))
        where = f" · {s['place']}" if s["place"] else ""
        title = self.conn.execute("SELECT title FROM summaries WHERE session = ?", (s["id"],)).fetchone()
        return (f"[{ref}] {short(s['day'])} {s['start'][11:16]}–{s['end'][11:16]} · "
                + (f"«{title[0]}» · " if title else "")
                + f"{kind} · voci: {speakers}{where} · {s['n_paragraphs']} paragrafi")

    # --- tools -----------------------------------------------------------------------

    def search_text(self, query: str, mode: str = "tutte", limit: int = 12, **filters) -> str:
        if mode not in ("tutte", "qualsiasi", "frase"):
            mode = "tutte"
        limit = max(1, min(int(limit or 12), MAX_LIMIT))
        where, params, note = self._where(**filters)
        tried = []
        for m in ([mode] if mode != "tutte" else ["tutte", "qualsiasi"]):
            q = fts_query(query, m)
            if q is None:
                raise ValueError("query senza parole: per elencare le conversazioni di un periodo usa filter")
            rows = self.conn.execute(
                f"""SELECT p.* FROM paragraphs_fts f JOIN paragraphs p ON p.pk = f.rowid
                    WHERE paragraphs_fts MATCH ? AND {' AND '.join(where)}
                    ORDER BY bm25(paragraphs_fts) * p.weight LIMIT ?""",
                [q, *params, limit]).fetchall()
            if not rows and m == "frase":
                # Substring, for words cut differently by the transcription.
                sub = " ".join(words(query))
                if len(sub) >= 3:
                    rows = self.conn.execute(
                        f"""SELECT p.* FROM paragraphs_tri t JOIN paragraphs p ON p.pk = t.rowid
                            WHERE paragraphs_tri MATCH ? AND {' AND '.join(where)}
                            ORDER BY p.start_ts DESC LIMIT ?""",
                        ['"' + sub.replace('"', "") + '"', *params, limit]).fetchall()
            tried.append(m)
            if rows:
                break
        head = f"search_text {query!r} (modo: {tried[-1]}; canali: {', '.join(self._channels(filters.get('channel')))}"
        head += f"; {note})" if note else ")"
        if not rows:
            return head + ": nessun risultato." + " Prova search, che cerca anche per significato."
        rows = sorted(rows, key=lambda r: r["start_ts"])
        return head + f": {len(rows)} paragrafi.\n" + "\n".join(self._paragraph_line(r) for r in rows)

    def filter(self, limit: int = 15, **filters) -> str:
        limit = max(1, min(int(limit or 15), MAX_LIMIT))
        where, params, note = self._where(**filters)
        rows = self.conn.execute(
            f"""SELECT s.* FROM sessions s WHERE s.id IN (SELECT p.session FROM paragraphs p WHERE {' AND '.join(where)})
                ORDER BY s.start_ts DESC LIMIT ?""", [*params, limit]).fetchall()
        head = "filter" + (f" ({note})" if note else "")
        if not rows:
            return head + ": nessuna conversazione."
        return head + f": {len(rows)} conversazioni, dalla più recente.\n" + "\n".join(
            self._session_line(s) for s in rows)

    def get_session(self, session: str, offset: int = 0) -> str:
        ref = self.refs.by_ref.get(session.strip("[] "))
        sid = ref.id if ref and ref.kind == "session" else session
        s = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        if s is None:
            raise ValueError(f"conversazione {session!r} non trovata")
        offset = max(0, int(offset or 0))
        channels = self.constraints.channels
        rows = self.conn.execute(
            f"SELECT * FROM paragraphs WHERE session = ? AND channel IN ({', '.join('?' for _ in channels)}) "
            f"ORDER BY ms LIMIT -1 OFFSET ?", [sid, *channels, offset]).fetchall()
        out, size = [self._session_line(s)], 0
        summary = self.conn.execute("SELECT data FROM summaries WHERE session = ?", (sid,)).fetchone()
        if summary and offset == 0:
            data = json.loads(summary[0])
            points = [i for i in data["summary"]] + ([{"text": f"(media) {h['summary']}", "refs": h["refs"]}
                                                      for h in data["heard"]] if "heard" in channels else [])
            if points:
                out.append("Riassunto:")
                for i in points:
                    cites = " ".join(f"[{self.refs.add('paragraph', pid, dict(r))}]" for pid in i["refs"]
                                     for r in self.conn.execute("SELECT * FROM paragraphs WHERE id = ? AND channel IN "
                                                                f"({', '.join('?' for _ in channels)})",
                                                                (pid, *channels)))
                    if cites:
                        out.append(f"- {i['text']} {cites}")
                out.append("Paragrafi:")
        for i, r in enumerate(rows[:SESSION_PAGE]):
            line = self._paragraph_line(r)
            size += len(line)
            if size > SESSION_CHARS and i:
                break
            out.append(line)
        shown = len(out) - 1
        if shown < len(rows):
            out.append(f"(altri {len(rows) - shown} paragrafi: get_session con offset={offset + shown})")
        return "\n".join(out)

    def get_person(self, name: str) -> str:
        p = self.resolve_person(name)
        if p is None:
            hits = self.search_text(name, mode="frase", limit=10)
            return (f"{name!r} non è una persona registrata (le voci senza nome sono 'Persona N'). "
                    f"Paragrafi in cui il nome compare nel testo:\n{hits}")
        seen = f", {p['minutes']} minuti in {p['sessions']} conversazioni, ultima {p['last_seen']}" \
            if p.get("minutes") is not None else ""
        recent = self.filter(person=p["id"] if p["id"] != "me" else "me", limit=10)
        out = f"{p['name']} (id {p['id']}){seen}.\nUltime conversazioni:\n{recent}"
        if self.doc_search is not None and p["id"] != "me":
            found = self.doc_search.about(p["name"])
            if found:
                out += f"\nDocumenti in cui compare:\n{found}"
        return out

    def list_commitments(self, owner: str | None = None, counterpart: str | None = None, date_from: str | None = None,
                         date_to: str | None = None, status: str = "tutti") -> str:
        where, params = ["1"], []
        if owner:
            if fold(owner) in ("altri", "others", "loro"):
                where.append("(c.owner IS NULL OR c.owner != 'me')")
            else:
                p = self.resolve_person(owner)
                if p is None:
                    raise ValueError(f"nessuna persona registrata con il nome {owner!r}")
                where.append("c.owner = ?")
                params.append(p["id"])
        if counterpart:
            p = self.resolve_person(counterpart)
            if p is None:
                where.append("c.counterpart_label LIKE ?")
                params.append(f"%{counterpart}%")
            else:
                where.append("c.counterpart = ?")
                params.append(p["id"])
        for value, op in ((date_from, ">="), (date_to, "<=")):
            if value:
                if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(value)):
                    raise ValueError(f"data non valida {value!r}: usa AAAA-MM-GG")
                where.append(f"c.day {op} ?")
                params.append(value)
        state = "COALESCE(st.status, 'open')"
        if status == "aperti":
            where.append(f"{state} = 'open'")
        elif status == "chiusi":
            where.append(f"{state} != 'open'")
        rows = self.conn.execute(
            f"SELECT c.*, {state} AS status FROM commitments c LEFT JOIN commitment_status st ON st.id = c.id "
            f"WHERE {' AND '.join(where)} ORDER BY c.day DESC LIMIT {MAX_LIMIT}", params).fetchall()
        if not rows:
            return "list_commitments: nessun impegno trovato (gli impegni esistono solo per le conversazioni già " \
                   "riassunte: prova anche search_text)."
        names = dict(self.conn.execute("SELECT id, name FROM people").fetchall())
        states = {"open": "aperto", "done": "fatto", "dropped": "lasciato perdere"}
        out = [f"list_commitments: {len(rows)} impegni, dal più recente."]
        for c in rows:
            ref = self.refs.add("commitment", c["id"], dict(c))
            sref = self._session_ref(c["session"])
            who = names.get(c["owner"]) or c["owner_label"] or "?"
            to = names.get(c["counterpart"]) or c["counterpart_label"]
            due = f" · entro {short(c['due'])}" if c["due"] else (f" · {c['due_text']}" if c["due_text"] else "")
            out.append(f"[{ref}] {short(c['day'])} · {who}{' → ' + to if to else ''}: {c['what']}{due} · "
                       f"{states[c['status']]} · conversazione {sref}")
        return "\n".join(out)

    # --- hybrid search ------------------------------------------------------------------

    def _paragraph_ok(self, pid: str, where: list[str], params: list) -> sqlite3.Row | None:
        return self.conn.execute(f"SELECT p.* FROM paragraphs p WHERE p.id = ? AND {' AND '.join(where)}",
                                 [pid, *params]).fetchone()

    def candidates(self, query: str, filters: dict) -> list[tuple[str, str]]:
        """(kind, id) from every list, best first, fused by reciprocal rank."""
        from brain import embed
        where, params, _ = self._where(**filters)
        channels = self._channels(filters.get("channel"))
        lists: list[list[tuple[str, str]]] = []
        q = fts_query(query, "qualsiasi")
        if q:
            lists.append([("paragraph", r[0]) for r in self.conn.execute(
                f"""SELECT p.id FROM paragraphs_fts f JOIN paragraphs p ON p.pk = f.rowid
                    WHERE paragraphs_fts MATCH ? AND {' AND '.join(where)}
                    ORDER BY bm25(paragraphs_fts) * p.weight LIMIT 30""", [q, *params])])
            lists.append([("summary", r[0]) for r in self.conn.execute(
                f"""SELECT f.session FROM summaries_fts f WHERE summaries_fts MATCH ? AND EXISTS
                    (SELECT 1 FROM paragraphs p WHERE p.session = f.session AND {' AND '.join(where)})
                    ORDER BY bm25(summaries_fts) LIMIT 10""", [q, *params])])
        # Documents have no channel, person or date of talk: only for questions about anything.
        docs = self.doc_search is not None and "said" in channels and \
            not any(filters.get(k) for k in ("person", "place", "date_from", "date_to"))
        if docs and q:
            lists += self.doc_search.fts(q)
        if self.embedder is not None:
            kinds = ("paragraph", "summary", "chunk") if docs else ("paragraph", "summary")
            hits = embed.knn(self.conn, self.embedder.query(query), kinds, channels, filters.get("date_from"),
                             filters.get("date_to"), k=60)
            near = []
            for kind, ref, _ in hits:
                if kind == "paragraph" and self._paragraph_ok(ref, where, params):
                    near.append((kind, ref))
                elif kind == "summary" and self.conn.execute(
                        f"SELECT 1 FROM paragraphs p WHERE p.session = ? AND {' AND '.join(where)} LIMIT 1",
                        [ref, *params]).fetchone():
                    near.append((kind, ref))
                elif kind == "chunk" and docs and self.doc_search.ok(ref):
                    near.append((kind, ref))
            lists.append(near)
        fused: dict[tuple[str, str], float] = {}
        for lst in lists:
            for rank, key in enumerate(lst):
                fused[key] = fused.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
        return sorted(fused, key=fused.get, reverse=True)

    def _rerank_text(self, kind: str, id_: str) -> str:
        """What the reranker reads of a candidate: enough context to judge it."""
        if kind == "paragraph":
            r = self.conn.execute("SELECT * FROM paragraphs WHERE id = ?", (id_,)).fetchone()
            title = self.conn.execute("SELECT title FROM summaries WHERE session = ?", (r["session"],)).fetchone()
            before = self.conn.execute("SELECT speaker, text FROM paragraphs WHERE session = ? AND ms < ? "
                                       "ORDER BY ms DESC LIMIT 1", (r["session"], r["ms"])).fetchone()
            who = f"media ({r['via_app'] or 'video'})" if r["channel"] == "heard" else (r["speaker"] or "?")
            text = (f"Conversazione «{title[0]}», " if title else "Conversazione, ") + f"{short(r['day'])}.\n"
            if before:
                text += f"{before['speaker'] or '?'}: {before['text'][-200:]}\n"
            return (text + f"{who}: {r['text']}")[:RERANK_CHARS]
        if kind == "summary":
            row = self.conn.execute("SELECT title, data FROM summaries WHERE session = ?", (id_,)).fetchone()
            data = json.loads(row["data"])
            points = [i["text"] for i in data["summary"]] or [h["summary"] for h in data["heard"]]
            return (f"Conversazione «{row['title']}»: " + " ".join(points))[:RERANK_CHARS]
        return self.doc_search.text(kind, id_)[:RERANK_CHARS]

    def _hit_line(self, kind: str, id_: str, extra: str) -> str:
        if kind == "paragraph":
            return self._paragraph_line(self.conn.execute("SELECT * FROM paragraphs WHERE id = ?", (id_,)).fetchone()) \
                + extra
        if kind == "summary":
            s = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (id_,)).fetchone()
            data = json.loads(self.conn.execute("SELECT data FROM summaries WHERE session = ?", (id_,)).fetchone()[0])
            points = [i["text"] for i in data["summary"][:3]]
            return self._session_line(s) + extra + (f"\n  Riassunto: {' '.join(points)}" if points else "")
        return self.doc_search.line(kind, id_, extra)

    def search(self, query: str, limit: int = 8, **filters) -> str:
        if not str(query).strip():
            raise ValueError("query vuota")
        limit = max(1, min(int(limit or 8), 15))
        _, _, note = self._where(**filters)  # checks the filters before any work
        order = self.candidates(str(query), filters)[:RERANK_CANDIDATES]
        scores = None
        if self.reranker is not None and order:
            try:
                scores = self.reranker.scores(str(query), [self._rerank_text(k, i) for k, i in order])
            except Exception:  # the reranker is an improvement, not a requirement
                scores = None
        ranked = sorted(zip(order, scores), key=lambda x: -x[1]) if scores else [(k, None) for k in order]
        chosen, per_doc, weak = [], {}, []
        for (kind, id_), score in ranked:
            if len(chosen) >= limit:
                break
            if score is not None and score < MIN_RELEVANCE:
                weak.append(((kind, id_), score))
                continue
            if kind in ("chunk", "card"):
                doc = self.doc_search.doc_of(kind, id_)
                if per_doc.get(doc, 0) >= PER_DOCUMENT:
                    continue
                per_doc[doc] = per_doc.get(doc, 0) + 1
            chosen.append(((kind, id_), score))
        head = f"search {query!r} (canali: {', '.join(self._channels(filters.get('channel')))}" + \
            (f"; {note})" if note else ")")
        if not chosen:
            if not weak:
                return head + ": nessun risultato. Prova altre parole, search_text o filter."
            return head + ": niente di pertinente. I risultati meno lontani, probabilmente non c'entrano:\n" + \
                "\n".join(self._hit_line(k, i, f"  (pertinenza {s:.2f})") for (k, i), s in weak[:2])
        lines = [self._hit_line(k, i, f"  (pertinenza {s:.2f})" if s is not None else "") for (k, i), s in chosen]
        return head + f": {len(lines)} risultati, dal più pertinente.\n" + "\n".join(lines)
