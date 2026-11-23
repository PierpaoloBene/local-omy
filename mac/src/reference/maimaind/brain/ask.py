"""Answer a question: the model picks the tools, the code keeps it honest.

- Constraints from the question are set before the model sees it ("did I ever
  say" searches only the user's own words; media only when asked about them).
- The answer may cite only references a tool returned in this conversation;
  anything else is stripped.
- An answer without a valid citation becomes "not found": nothing invented.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from brain import dates
from brain.llm import LLM
from brain.tools import Constraints, Tools

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
MAX_ROUNDS = 6
MAX_TOKENS = 2048
NOT_FOUND = "Non ho trovato niente nelle trascrizioni su questo."

# "Did I ever say …": only the user's own words count.
ME_ONLY = re.compile(
    r"\b(ho|avevo|avrei|abbia)\s+(mai\s+)?(detto|scritto|pronunciato|usato\s+l[ae]\s+(parol|espression|fras)\w*)\b"
    r"|\bdid\s+i\s+(ever\s+)?say\b|\bhave\s+i\s+(ever\s+)?said\b", re.I)
# Questions about something heard on a screen or a speaker: media only.
MEDIA_ONLY = re.compile(r"\b(podcast|video|youtube|tv|televisione|tg|radio|film|serie|documentario|spotify)\b", re.I)
# "ho sentito / ascoltato": could be a person or a video; media allowed too.
HEARD_TOO = re.compile(r"\b(ho|abbiamo)\s+(sentito|ascoltato)\b|\bi\s+(heard|listened)\b", re.I)

CITATION = re.compile(r"\[((?:[RSCD]\d+)(?:\s*[,;]\s*[RSCD]\d+)*)\]")


def constraints_for(question: str) -> Constraints:
    if ME_ONLY.search(question):
        return Constraints(("me",), "Vincolo: la domanda chiede cosa ha detto {my_name}: le ricerche "
                                    "restituiscono solo paragrafi del canale me.")
    if MEDIA_ONLY.search(question):
        return Constraints(("heard",), "Vincolo: la domanda riguarda un media (video, podcast, TV): le "
                                       "ricerche restituiscono solo paragrafi del canale heard.")
    if HEARD_TOO.search(question):
        return Constraints(("me", "said", "heard"), "")
    return Constraints()


@dataclass
class Answer:
    question: str
    text: str
    citations: list[dict] = field(default_factory=list)
    found: bool = False
    tool_calls: list[dict] = field(default_factory=list)
    invalid_refs: list[str] = field(default_factory=list)
    raw: str = ""
    rounds: int = 0
    seconds: float = 0.0
    timings: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("question", "text", "citations", "found", "tool_calls",
                                              "invalid_refs", "raw", "rounds", "seconds", "timings")}


def my_name(conn: sqlite3.Connection) -> str:
    me = conn.execute("SELECT name FROM people WHERE id = 'me'").fetchone()
    return me[0] if me else "Io"


def system_prompt(conn: sqlite3.Connection, today: date) -> str:
    """The same for every question of a day: llama-server reuses it from its cache
    (the prefill, ~160 tokens/s on an M2 Pro, is most of the waiting)."""
    people = [r[0] for r in conn.execute("SELECT name FROM people WHERE id != 'me' ORDER BY name")]
    return (PROMPTS / "ask_system.md").read_text().format(
        my_name=my_name(conn),
        people=", ".join(people) if people else "nessuna per ora",
        dates=dates.context(today),
    )


def finish(answer: Answer, raw: str, tools: Tools) -> Answer:
    """Keep only the citations of references actually returned; number them."""
    answer.raw = raw
    order: list[str] = []

    def replace(m: re.Match) -> str:
        kept = []
        for ref in re.split(r"\s*[,;]\s*", m.group(1)):
            r = tools.refs.by_ref.get(ref)
            # A paragraph outside the allowed channels cannot have been returned,
            # but check anyway: it is the promise this module makes.
            if r is None or (r.kind == "paragraph" and r.row["channel"] not in tools.constraints.channels):
                answer.invalid_refs.append(ref)
                continue
            if ref not in order:
                order.append(ref)
            kept.append(str(order.index(ref) + 1))
        return f"[{', '.join(kept)}]" if kept else ""

    text = CITATION.sub(replace, raw).strip()
    text = re.sub(r"[ \t]+([.,;:])", r"\1", re.sub(r"[ \t]{2,}", " ", text))
    if not order:
        answer.text = NOT_FOUND
        answer.found = False
        return answer
    answer.found = True
    answer.text = text
    for n, ref in enumerate(order, 1):
        r = tools.refs.by_ref[ref]
        row = r.row
        c = {"n": n, "ref": ref, "kind": r.kind, "id": r.id, "day": row.get("day"),
             "time": (row.get("start") or "")[11:16], "session": row.get("session") or r.id}
        if r.kind == "paragraph":
            c.update(speaker=row.get("speaker"), person=row.get("person"), channel=row.get("channel"),
                     via=row.get("via_app"), text=row.get("text"))
        elif r.kind == "commitment":
            c.update(session=row.get("session"), text=row.get("what"), refs=json.loads(row.get("refs") or "[]"))
        elif r.kind == "document":
            from brain.docs import place
            c.update(session=None, path=row.get("path"), place=place(row.get("path") or ""), title=row.get("title"),
                     sha256=row.get("sha256"))
        answer.citations.append(c)
    return answer


def first_query(question: str, history: list[dict] | None) -> str:
    """What the search made before the model starts looks for: the question, and
    for a short follow-up ("me lo mandi?", "e quando?") the question before it too."""
    earlier = [m["content"] for m in history or [] if m["role"] == "user"]
    if earlier and len(question) < 60:
        return f"{earlier[-1]} {question}"
    return question


def ask(question: str, conn: sqlite3.Connection, llm: LLM, today: date | None = None, embedder=None,
        setup=None, on_event=None, history: list[dict] | None = None, reranker=None) -> Answer:
    """setup(tools), if given, registers more tools (documents) before the model sees them.
    on_event(kind, data), if given, follows the work: ("tool", {"name", "arguments"}),
    ("token", text) while an answer is written, ("reset", None) when written text
    turns out to come before more searching. The final, checked answer is the return value.
    history: earlier turns of the same chat, [{"role", "content"}], already checked.

    The code makes the first search itself (search, on the question) and hands it
    to the model as if it had asked for it: a whole round saved (a tool call to
    write, the prompt read again) when that search is enough, and it often is."""
    started = time.monotonic()
    constraints = constraints_for(question)
    tools = Tools(conn, constraints, embedder=embedder, reranker=reranker)
    if setup:
        setup(tools)
    note = constraints.note.format(my_name=my_name(conn))
    messages: list[dict] = [
        {"role": "system", "content": system_prompt(conn, today or date.today())},
        *[{"role": m["role"], "content": m["content"]} for m in (history or [])[-6:]],
        {"role": "user", "content": question + (f"\n\n({note})" if note else "")},
    ]
    first = {"query": first_query(question, history)}
    if on_event:
        on_event("tool", {"name": "search", "arguments": json.dumps(first, ensure_ascii=False)})
    messages += [{"role": "assistant", "content": "", "tool_calls": [{
                     "id": "call-first", "type": "function",
                     "function": {"name": "search", "arguments": json.dumps(first, ensure_ascii=False)}}]},
                 {"role": "tool", "tool_call_id": "call-first", "content": tools.call("search", first)}]
    answer = Answer(question=question, text="")
    raw = ""
    force_answer = False
    for rnd in range(MAX_ROUNDS + 1):
        last = rnd == MAX_ROUNDS or force_answer
        # Last round: answer with what there is.
        streamed = []

        def token(text: str) -> None:
            streamed.append(text)
            on_event("token", text)

        r = llm.chat(messages, tools=None if last else tools.schemas(),
                     tool_choice=None if last else "auto", max_tokens=MAX_TOKENS,
                     **({"on_token": token} if on_event else {}))
        answer.rounds = rnd + 1
        answer.timings.append(r["timings"])
        msg = r["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            raw = (msg.get("content") or "").strip()
            if raw or last:
                break
            # An empty answer (thinking used up the tokens): ask once more, without tools.
            force_answer = True
            continue
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        if on_event and streamed:
            on_event("reset", None)
        for call in calls:
            fn = call.get("function") or {}
            if on_event:
                on_event("tool", {"name": fn.get("name"), "arguments": fn.get("arguments")})
            result = tools.call(fn.get("name", ""), fn.get("arguments") or "{}")
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
    answer.tool_calls = tools.calls
    finish(answer, raw, tools)
    answer.seconds = round(time.monotonic() - started, 2)
    return answer


def render(answer: Answer) -> str:
    """For the terminal."""
    lines = [answer.text]
    if answer.citations:
        lines.append("")
        for c in answer.citations:
            when = f"{dates.short(c['day'])} {c['time']}" if c.get("day") else ""
            if c["kind"] == "paragraph":
                who = f"Media ({c['via']})" if c["channel"] == "heard" else c.get("speaker") or "?"
                text = c["text"] if len(c["text"]) <= 160 else c["text"][:160] + "…"
                lines.append(f"[{c['n']}] {when} · {who}: «{text}»  ({c['session']})")
            elif c["kind"] == "commitment":
                lines.append(f"[{c['n']}] {when} · impegno: {c['text']}  ({c['session']})")
            elif c["kind"] == "document":
                lines.append(f"[{c['n']}] documento: {c.get('title') or ''} · {c['path']}")
            else:
                lines.append(f"[{c['n']}] {when} · conversazione {c['session']}")
    return "\n".join(lines)
