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
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

from omy import models
from omy.brain import dates
from omy.config import ARCHIVE

PROMPT = Path(__file__).with_name("prompts") / "ask.md"
MIN_SCORE = 0.010
# The vector list always returns its nearest chunks, so RRF alone never says "nothing": a hit is
# relevant only if the full-text query matched it or its cosine is at least COS_MIN.
COS_MIN = 0.50
K = 8
CONTEXT_CHARS = int(1200 * 3.2)  # ~1,200 tokens of sources
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

def detect_persons(question: str) -> list[str]:
    """Person ids ("me" included) named in the question: exact first name / full name, else fuzzy."""
    from omy import diarize
    from omy.brain import organize
    from omy.brain.names import my_aliases
    names: dict[str, str] = {organize._fold(diarize.my_name()): "me", **{organize._fold(a): "me" for a in my_aliases()}}
    for pid, p in diarize.load_people().items():
        if p.get("name"):
            full = organize._fold(organize.APP_SUFFIX.sub("", p["name"]))
            names[full] = pid
            if full.split():
                names.setdefault(full.split()[0], pid)
    words = re.findall(r"\w+", organize._fold(question))
    text = " ".join(words)
    found: list[str] = []
    for n, pid in names.items():
        if n and re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text) and pid not in found:
            found.append(pid)
    for w in words:
        if len(w) < 4:
            continue
        best = [(organize._lev_ratio(w, n), pid) for n, pid in names.items()
                if len(n) >= 4 and n[0] == w[0] and n != w]
        best = [b for b in best if b[0] >= organize.FUZZY]
        if best:
            top = max(r for r, _ in best)
            winners = {pid for r, pid in best if r == top}
            if len(winners) == 1 and (pid := winners.pop()) not in found:
                found.append(pid)
    return found


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


def build_context(hits: list[dict]) -> tuple[str, list[dict]]:
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


def _stream_llama(base_url: str, messages: list[dict], put, stop: threading.Event) -> None:
    """Runs in a thread: put(piece) for each content delta, put(_DONE) or put(exception) at the end."""
    body = {"messages": messages, "stream": True, "temperature": 0.2, "max_tokens": 300, "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False}}
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/v1/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
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

def _search(question: str, history: list[dict], rng, persons: list[str]) -> list[dict]:
    index = _index()
    query = question
    if len(question.split()) < SHORT_QUESTION_WORDS:
        prev = [h["content"] for h in history if h.get("role") == "user" and isinstance(h.get("content"), str)]
        if prev:
            query = f"{prev[-1].strip()} {question}"
    day_from, day_to = (rng[0], rng[1]) if rng else (None, None)
    kw = {"day_from": day_from, "day_to": day_to, "k": K}
    filt = [p for p in persons if p != "me"]
    hits = index.search(query, persons=filt or None, **kw)
    if filt and len(hits) < 2:
        hits = index.search(query, persons=None, **kw)

        def involves(h) -> bool:
            try:
                return bool(set(filt) & set(json.loads(h.get("persons") or "[]")))
            except (ValueError, TypeError):
                return False
        hits = sorted(hits, key=lambda h: not involves(h))  # stable: boost
    return hits


async def answer(question: str, history: list[dict], now: datetime) -> AsyncIterator[dict]:
    t0 = time.monotonic()
    ms = lambda a, b: int((b - a) * 1000)  # noqa: E731
    timings = {"wait_model_ms": 0, "retrieval_ms": 0, "ttft_ms": 0, "total_ms": 0}
    n_hits, not_found = 0, False
    stop = threading.Event()
    try:
        yield {"type": "status", "stage": "searching"}
        rng = dates.question_range(question, now)
        persons = await asyncio.to_thread(detect_persons, question)
        hits = await asyncio.to_thread(_search, question, history, rng, persons)
        timings["retrieval_ms"] = ms(t0, time.monotonic())
        n_hits = len(hits)
        if not relevant(hits):
            not_found = True
            yield {"type": "citations", "items": []}
            yield {"type": "delta", "text": NOT_FOUND}
            timings["total_ms"] = ms(t0, time.monotonic())
            yield {"type": "done", "not_found": True, "used": [], "timings": timings}
            return
        sources, cites = build_context(hits)
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
        threading.Thread(target=_stream_llama, daemon=True, args=(
            base_url, msgs, lambda x: loop.call_soon_threadsafe(queue.put_nowait, x), stop)).start()
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
        _log(question, n_hits, not_found, timings)
