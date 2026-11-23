"""Question answering over the archive (INTERFACES.md §9).

answer(question, history, now) is an async generator of the events of the
/v1/ask stream: status, citations, delta, done, error.

Choices:
- MIN_SCORE: index.search() fuses BM25 and cosine with RRF k=60, so a score is
  1/(60+rank) summed over the two lists: 0.033 is rank 1 in both, 0.0164 rank 1
  in one. A hit below 0.010 (rank > 40 in a single list) is noise: when the best
  hit is under it, or there is none, the answer is "not found" and the model is
  never loaded.
- Persons: a name of people.json (or my_name / my_aliases, fuzzy like
  organize.Checker) present in the question filters the search to that person.
  "me" is not a filter (every conversation has the user). With < 2 hits the
  search is retried without the filter and the hits that involve the person are
  put first (boost).
- The retrieval query is the question, plus the previous user question when the
  new one has < 6 words ("e a Marco?").
- ttft_ms counts from the start of answer(), so it includes the wait for the
  model and the retrieval: it is what the user waits for before the first word.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

from omy import jobs, models
from omy.brain import dates
from omy.config import ARCHIVE

PROMPT = Path(__file__).with_name("prompts") / "ask.md"
MIN_SCORE = 0.010
# The vector list always returns its nearest chunks, so RRF alone never says "nothing": a hit is
# relevant only if the full-text query matched it or its cosine is at least COS_MIN.
COS_MIN = 0.50
K = 8
CONTEXT_CHARS = int(850 * 3.2)  # ~850 tokens of sources: prefill dominates the latency (~500 tok/s)
MAX_SOURCE_CHARS = 700
SNIPPET = 200
MAX_HISTORY = 8  # messages = 4 turns
MAX_HISTORY_CHARS = 600
SHORT_QUESTION_WORDS = 6
NOT_FOUND = "Non ho trovato nulla nell'archivio su questo."
LOG = ARCHIVE / "cache" / "ask-log.jsonl"
LLM_TIMEOUT = 300
_DONE = object()


def _index():
    from omy.brain import index  # written in parallel: imported lazily
    return index


# --- people -----------------------------------------------------------------------

_FUZZY_STOP = {"marzo", "anno", "anni", "luce", "caro", "cara", "sara", "saro", "sarai", "sarebbe", "ecc", "come",
               "cosa", "cose", "detto", "ieri", "oggi", "domani", "quando", "dove", "mamma", "papa", "casa",
               "pasta", "mare", "carta", "forse", "tutto", "tutti", "ancora", "sempre", "mese", "mesi", "giorno",
               "giorni", "sera", "notte", "mattina", "settimana", "scorso", "scorsa"} | \
    {dates.fold(w) for w in (*dates.MONTHS, *dates.WEEKDAYS)}


def _strict(text: str) -> str:
    """Casefolded, accents kept ("sarà" is not "sara")."""
    return unicodedata.normalize("NFC", text.casefold())


def person_groups(question: str) -> tuple[list[str], list[str]]:
    """(sure, ambiguous) person ids ("me" included) named in the question.

    Exact: a full name or first name, on word boundaries and with the accents. Fuzzy (organize.FUZZY): only
    tokens capitalized in the question, not at a sentence start, and not a month/weekday/common word.
    A first name shared by two people is ambiguous (both listed there, none in sure)."""
    from omy import diarize
    from omy.brain import organize
    from omy.brain.names import my_aliases
    names: dict[str, set[str]] = {}

    def add(n: str, pid: str) -> None:
        for key in {_strict(n), organize._fold(n)}:  # a name with accents is also found without them
            if key:
                names.setdefault(key, set()).add(pid)

    add(diarize.my_name(), "me")
    for a in my_aliases():
        add(a, "me")
    for pid, p in diarize.load_people().items():
        if p.get("name"):
            full = organize.APP_SUFFIX.sub("", p["name"]).strip()
            add(full, pid)
            if full.split():
                add(full.split()[0], pid)
    toks = [(m.group(), m.start()) for m in re.finditer(r"\w+", question)]
    text = " ".join(_strict(w) for w, _ in toks)
    sure: list[str] = []
    maybe: list[str] = []
    shared: list[set[str]] = []
    for n, pids in names.items():
        if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text):
            if len(pids) == 1:
                (pid,) = pids
                if pid not in sure:
                    sure.append(pid)
            else:
                shared.append(pids)
    for pids in shared:
        if not pids & set(sure):  # "Marco Rossi" settles the "marco" shared with Marco Bianchi
            maybe += [p for p in sorted(pids) if p not in maybe]
    fuzzy_names = {organize._fold(n): pids for n, pids in names.items() if len(organize._fold(n)) >= 4}
    for w, pos in toks:
        wf = organize._fold(w)
        if len(wf) < 4 or not w[0].isupper() or wf in _FUZZY_STOP:
            continue
        if pos == 0 or re.search(r"[.!?:]\s*$|\n\s*$", question[:pos]):
            continue  # a sentence start is always capitalized: no evidence
        best = [(organize._lev_ratio(wf, n), pid) for n, pids in fuzzy_names.items() for pid in pids
                if n[0] == wf[0] and n != wf]
        best = [b for b in best if b[0] >= organize.FUZZY]
        if best:
            top = max(r for r, _ in best)
            winners = {pid for r, pid in best if r == top}
            if len(winners) == 1 and (pid := winners.pop()) not in sure:
                sure.append(pid)
    return sure, [p for p in maybe if p not in sure]


def detect_persons(question: str) -> list[str]:
    """Person ids ("me" included) named in the question, ambiguous first names included."""
    sure, maybe = person_groups(question)
    return sure + maybe


# --- context ---------------------------------------------------------------------

def _when(hit: dict) -> tuple[str, str]:
    """("sabato 21 novembre", "18:22") of a hit."""
    start = hit.get("start") or ""
    try:
        d = datetime.fromisoformat(hit["day"])
        label = f"{dates.WEEKDAYS[d.weekday()]} {d.day} {dates.MONTHS[d.month - 1]}"
    except (KeyError, ValueError, TypeError):
        label = str(hit.get("day") or "")
    return label, start[11:16] if len(start) >= 16 else ""


def relevant(hits: list[dict]) -> bool:
    if not hits or max(h.get("score") or 0 for h in hits) < MIN_SCORE:
        return False
    if not any("lexical" in h or "cos" in h for h in hits):   # an index without these fields
        return True
    return any(h.get("lexical") or (h.get("cos") or 0) >= COS_MIN for h in hits)


def build_context(hits: list[dict], session_days: dict[str, str] | None = None) -> tuple[str, list[dict]]:
    """Numbered sources within the budget and the citations of those that fit."""
    lines, cites, used = [], [], 0
    for hit in hits:
        label, hhmm = _when(hit)
        text = " ".join(str(hit.get("text") or "").split())
        title = str(hit.get("title") or "").strip()
        n = len(cites) + 1
        line = f"[{n}] {label} {hhmm} — {title} ({hit.get('kind')}): {text[:MAX_SOURCE_CHARS]}"
        if cites and used + len(line) > CONTEXT_CHARS:
            break
        used += len(line)
        lines.append(line)
        cites.append({"n": n, "kind": hit.get("kind"), "session": hit.get("session"), "day": hit.get("day"),
                      "session_day": (session_days or {}).get(hit.get("session")) or hit.get("day"),
                      "time": hhmm, "t": hit.get("t"), "row_id": hit.get("row_id"), "title": title,
                      "snippet": text[:SNIPPET]})
    return "\n".join(lines), cites


def build_messages(question: str, history: list[dict], sources: str, now: datetime) -> list[dict]:
    d = now.date()
    today = f"{dates.WEEKDAYS[d.weekday()]} {d.day} {dates.MONTHS[d.month - 1]} {d.year}"
    system = PROMPT.read_text(encoding="utf-8").replace("{oggi}", today).replace("{fonti}", sources)
    msgs = [{"role": "system", "content": system}]
    for h in history[-MAX_HISTORY:]:
        if h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str) and h["content"].strip():
            msgs.append({"role": h["role"], "content": h["content"].strip()[:MAX_HISTORY_CHARS]})
    msgs.append({"role": "user", "content": question})
    return msgs


# --- streaming from llama-server ------------------------------------------------------

class ThinkFilter:
    """Drops <think>…</think> from a stream of text pieces, even when a tag is split between pieces."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf = ""
        self.inside = False

    def feed(self, piece: str) -> str:
        self.buf += piece
        out = ""
        while True:
            tag = self.CLOSE if self.inside else self.OPEN
            i = self.buf.find(tag)
            if i >= 0:
                out += "" if self.inside else self.buf[:i]
                self.buf = self.buf[i + len(tag):]
                self.inside = not self.inside
                continue
            keep = 0  # a suffix that may be the start of the tag stays in the buffer
            for k in range(min(len(tag) - 1, len(self.buf)), 0, -1):
                if tag.startswith(self.buf[-k:]):
                    keep = k
                    break
            if not self.inside:
                out += self.buf[:len(self.buf) - keep]
            self.buf = self.buf[len(self.buf) - keep:]
            return out

    def flush(self) -> str:
        out = "" if self.inside else self.buf
        self.buf = ""
        return out


def _stream_llama(base_url: str, messages: list[dict], put, stop: threading.Event, holder: dict) -> None:
    """Runs in a thread: put(piece) for each content delta, put(_DONE) or put(exception) at the end."""
    body = {"messages": messages, "stream": True, "temperature": 0.2, "max_tokens": 300, "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False}}
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/v1/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        resp = holder["resp"] = urllib.request.urlopen(req, timeout=LLM_TIMEOUT)
        with resp:
            for raw in resp:
                if stop.is_set():
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0].get("delta", {}).get("content")
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
                if delta:
                    put(delta)
        put(_DONE)
    except Exception as e:  # noqa: BLE001 - reported to the caller as an error event
        put(e)


def _log(question: str, n_hits: int, not_found: bool, timings: dict) -> None:
    try:
        LOG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                                "question_chars": len(question), "hits": n_hits, "not_found": not_found,
                                "timings": timings}) + "\n")
    except OSError:
        pass


# --- the pipeline -----------------------------------------------------------------------

def _prev_question(question: str, history: list[dict]) -> str | None:
    """The previous user question, when the new one is a short follow-up ("e a Marco?")."""
    if len(question.split()) >= SHORT_QUESTION_WORDS:
        return None
    prev = [h["content"] for h in history if h.get("role") == "user" and isinstance(h.get("content"), str)]
    return prev[-1].strip() if prev else None


def _search(question: str, history: list[dict], rng, persons: list[str], maybe: list[str] | None = None) -> list[dict]:
    """persons filter the search; `maybe` (an ambiguous first name) only boosts."""
    index = _index()
    prev = _prev_question(question, history)
    query = f"{prev} {question}" if prev else question
    day_from, day_to = (rng[0], rng[1]) if rng else (None, None)
    kw = {"day_from": day_from, "day_to": day_to, "k": K}
    filt = [p for p in persons if p != "me"]
    boost = filt + [p for p in (maybe or []) if p != "me" and p not in filt]
    hits = index.search(query, persons=filt or None, **kw)
    if boost and (len(hits) < 2 if filt else True):
        if filt:
            hits = index.search(query, persons=None, **kw)

        def involves(h) -> bool:
            return bool(set(boost) & set(h.get("persons") or []))
        hits = sorted(hits, key=lambda h: not involves(h))  # stable: boost
    return hits


def _session_days(sessions: list[str]) -> dict[str, str]:
    """session -> its local start day: the sidecar's `data`, else the day of its first JSONL row."""
    index = _index()
    out: dict[str, str] = {}
    for sid in dict.fromkeys(s for s in sessions if s):
        try:
            day = (index._read_sidecars([sid]).get(sid) or {}).get("data")
            if not day:
                rows = index._read_rows([sid]).get(sid) or []
                day = rows[0]["start"][:10] if rows else None
        except Exception:  # noqa: BLE001 - a missing day only costs the iPhone a lookup
            day = None
        if day:
            out[sid] = str(day)
    return out


async def answer(question: str, history: list[dict], now: datetime) -> AsyncIterator[dict]:
    t0 = time.monotonic()
    ms = lambda a, b: int((b - a) * 1000)  # noqa: E731
    timings = {"wait_model_ms": 0, "retrieval_ms": 0, "ttft_ms": 0, "total_ms": 0}
    n_hits, not_found = 0, False
    stop = threading.Event()
    holder: dict = {}  # the HTTP response of the reader thread: closing it stops llama-server's generation
    try:
        try:  # the question reaches the worker before anything else: it may have to stop the organizer
            await asyncio.to_thread(jobs.touch_demand, "chat")
        except Exception:  # noqa: BLE001 - request_chat renews it anyway
            pass
        yield {"type": "status", "stage": "searching"}
        prev = _prev_question(question, history)
        rng = dates.question_range(question, now) or (dates.question_range(f"{prev} {question}", now) if prev else None)
        persons, maybe = await asyncio.to_thread(person_groups, question)
        if prev and not persons and not maybe:
            persons, maybe = await asyncio.to_thread(person_groups, f"{prev} {question}")
        hits = await asyncio.to_thread(_search, question, history, rng, persons, maybe)
        timings["retrieval_ms"] = ms(t0, time.monotonic())
        n_hits = len(hits)
        if not relevant(hits):
            not_found = True
            yield {"type": "citations", "items": []}
            yield {"type": "delta", "text": NOT_FOUND}
            timings["total_ms"] = ms(t0, time.monotonic())
            yield {"type": "done", "not_found": True, "used": [], "timings": timings}
            return
        session_days = await asyncio.to_thread(_session_days, [h.get("session") for h in hits])
        sources, cites = build_context(hits, session_days)
        yield {"type": "citations", "items": cites}
        msgs = build_messages(question, history, sources, now)

        if models.state().get("loaded") != "chat":
            yield {"type": "status", "stage": "waking"}
        w0 = time.monotonic()
        base_url = await asyncio.to_thread(models.request_chat)
        timings["wait_model_ms"] = ms(w0, time.monotonic())
        yield {"type": "status", "stage": "answering"}

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def put(x) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, x)
            except RuntimeError:  # the loop is closed: the client left
                pass
        threading.Thread(target=_stream_llama, daemon=True, args=(base_url, msgs, put, stop, holder)).start()
        think, full, first = ThinkFilter(), "", True
        while True:
            item = await queue.get()
            if item is _DONE:
                break
            if isinstance(item, Exception):
                raise item
            text = think.feed(item)
            if text:
                if first:
                    timings["ttft_ms"] = ms(t0, time.monotonic())
                    first = False
                full += text
                yield {"type": "delta", "text": text}
        if tail := think.flush():
            full += tail
            yield {"type": "delta", "text": tail}
        valid = {c["n"] for c in cites}
        used = sorted({int(n) for n in re.findall(r"\[(\d+)\]", full)} & valid)
        timings["total_ms"] = ms(t0, time.monotonic())
        yield {"type": "done", "not_found": False, "used": used, "timings": timings}
    except Exception as e:  # noqa: BLE001
        timings["total_ms"] = ms(t0, time.monotonic())
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"[:200]}
    finally:
        stop.set()  # the client left or we are done: let the reader thread go
        resp = holder.get("resp")
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        _log(question, n_hits, not_found, timings)
