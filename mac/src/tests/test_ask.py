"""The question pipeline: date ranges, people, retrieval, streaming, endpoints. Everything is fake: the
index is a stand-in module, request_chat returns a thread serving canned SSE chunks."""

import asyncio
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from omy.brain import ask, dates

NOW = datetime.fromisoformat("2026-11-23T14:30:00+01:00")  # a Monday
SESSION = "20261121T171433Z-aafa6412"
TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# --- date ranges ----------------------------------------------------------------------

@pytest.mark.parametrize("text,want", [
    ("cosa ho fatto oggi?", ("2026-11-23", "2026-11-23")),
    ("Stamattina con Marco", ("2026-11-23", "2026-11-23")),
    ("e ieri?", ("2026-11-22", "2026-11-22")),
    ("l'altro ieri a pranzo", ("2026-11-21", "2026-11-21")),
    ("questa settimana", ("2026-11-23", "2026-11-23")),
    ("la settimana scorsa cosa ho promesso", ("2026-11-16", "2026-11-22")),
    ("questo mese", ("2026-11-01", "2026-11-23")),
    ("il mese scorso", ("2026-10-01", "2026-10-31")),
    ("lunedì", ("2026-11-23", "2026-11-23")),
    ("lunedì scorso", ("2026-11-16", "2026-11-16")),
    ("venerdì", ("2026-11-20", "2026-11-20")),
    ("domenica scorsa", ("2026-11-22", "2026-11-22")),
    ("ad ottobre", ("2026-10-01", "2026-10-31")),
    ("a dicembre", ("2026-12-01", "2026-12-31")),
    ("a novembre", ("2026-11-01", "2026-11-30")),
    ("il 3 novembre", ("2026-11-03", "2026-11-03")),
    ("il 3 dicembre", ("2026-12-03", "2026-12-03")),
    ("dal 3 al 10 novembre", ("2026-11-03", "2026-11-10")),
    ("dal 28 ottobre al 2 novembre", ("2026-10-28", "2026-11-02")),
    ("negli ultimi 3 giorni", ("2026-11-21", "2026-11-23")),
    ("negli ultimi sette giorni", ("2026-11-17", "2026-11-23")),
    ("quest'anno", ("2026-01-01", "2026-11-23")),
])
def test_question_range(text, want):
    got = dates.question_range(text, NOW)
    assert got is not None and got[:2] == want and got[2]


def test_question_range_none():
    assert dates.question_range("cosa penso di Marco?", NOW) is None


def test_question_range_matched_text():
    assert dates.question_range("Cosa dissi il 3 novembre a Marco?", NOW)[2] == "3 novembre"


# --- fakes ----------------------------------------------------------------------------

def hit(i, score=0.03, **kw):
    h = {"id": i, "kind": "nota", "session": SESSION, "day": "2026-11-21", "start": "2026-11-21T18:22:10+01:00",
         "t": 512.0, "row_id": f"{SESSION}/{i}", "persons": ["me"], "title": "Cena da Marco", "field": "punto",
         "text": f"Hai promesso a Marco di portare il vino {i}.", "score": score}
    h.update(kw)
    return h


class FakeIndex:
    def __init__(self, hits=None, by_persons=None):
        self.hits, self.by_persons, self.calls = hits or [], by_persons, []

    def search(self, query, *, day_from=None, day_to=None, persons=None, k=8):
        self.calls.append({"query": query, "day_from": day_from, "day_to": day_to, "persons": persons, "k": k})
        if persons and self.by_persons is not None:
            return self.by_persons
        return self.hits


class FakeLlama:
    """A llama-server that streams `pieces` as SSE chunks."""

    def __init__(self, pieces):
        self.pieces, self.bodies = pieces, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                outer.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for p in outer.pieces:
                    chunk = {"choices": [{"delta": {"content": p}}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def world(monkeypatch, archive):
    """index, chat requests and loaded state, all fake; `w.chat_calls` counts request_chat."""
    w = SimpleNamespace(index=FakeIndex([hit(1), hit(2, text="Marco ha chiesto una mano.", kind="testo")]),
                        llama=None, chat_calls=0, loaded=None)

    def request_chat():
        w.chat_calls += 1
        return w.llama.url

    monkeypatch.setattr(ask, "_index", lambda: w.index)
    monkeypatch.setattr(ask.models, "request_chat", request_chat)
    monkeypatch.setattr(ask.models, "state", lambda: {"loaded": w.loaded})
    return w


def run(question, history=(), now=NOW):
    async def collect():
        return [ev async for ev in ask.answer(question, list(history), now)]
    return asyncio.run(collect())


def types(events):
    return [e["type"] for e in events]


def write_people(archive, people):
    from omy import diarize
    diarize.PEOPLE.parent.mkdir(parents=True, exist_ok=True)
    diarize.PEOPLE.write_text(json.dumps({"people": people}))


# --- pipeline ---------------------------------------------------------------------------

def test_event_order_and_citations(world):
    with FakeLlama(["Hai promesso ", "il vino a Marco [1]", "."]) as llama:
        world.llama = llama
        ev = run("Cosa ho promesso a Marco ieri?")
    assert types(ev) == ["status", "citations", "status", "status", "delta", "delta", "delta", "done"]
    assert [e.get("stage") for e in ev if e["type"] == "status"] == ["searching", "waking", "answering"]
    cites = ev[1]["items"]
    assert [c["n"] for c in cites] == [1, 2]
    assert cites[0] == {"n": 1, "kind": "nota", "session": SESSION, "day": "2026-11-21", "session_day": "2026-11-21", "time": "18:22",
                        "t": 512.0,
                        "row_id": f"{SESSION}/1", "title": "Cena da Marco",
                        "snippet": "Hai promesso a Marco di portare il vino 1."}
    assert "".join(e["text"] for e in ev if e["type"] == "delta") == "Hai promesso il vino a Marco [1]."
    done = ev[-1]
    assert done["not_found"] is False and done["used"] == [1]
    assert set(done["timings"]) == {"wait_model_ms", "retrieval_ms", "ttft_ms", "total_ms"}
    body = llama.bodies[0]
    assert body["stream"] is True and body["temperature"] == 0.2 and body["max_tokens"] == 300
    assert body["chat_template_kwargs"] == {"enable_thinking": False} and body["cache_prompt"] is True
    system = body["messages"][0]["content"]
    assert "[1] sabato 21 novembre 18:22 — Cena da Marco (nota): Hai promesso" in system
    assert "lunedì 23 novembre 2026" in system
    assert body["messages"][-1] == {"role": "user", "content": "Cosa ho promesso a Marco ieri?"}
    # ieri -> the day filter
    assert world.index.calls[0]["day_from"] == world.index.calls[0]["day_to"] == "2026-11-22"
    assert world.index.calls[0]["k"] == 8


def test_no_waking_status_when_chat_is_loaded(world):
    world.loaded = "chat"
    with FakeLlama(["Ok [1]"]) as llama:
        world.llama = llama
        ev = run("Cosa ho promesso?")
    assert [e.get("stage") for e in ev if e["type"] == "status"] == ["searching", "answering"]


@pytest.mark.parametrize("hits", [[], [hit(1, score=0.002)]])
def test_not_found_never_loads_the_model(world, hits):
    world.index.hits = hits
    ev = run("Cosa ho detto a Zeno?")
    assert world.chat_calls == 0
    assert types(ev) == ["status", "citations", "delta", "done"]
    assert ev[1]["items"] == []
    assert ev[2]["text"] == "Non ho trovato nulla nell'archivio su questo."
    assert ev[3]["not_found"] is True and ev[3]["used"] == []


def test_think_is_stripped(world):
    with FakeLlama(["<thi", "nk>ragiono qui</th", "ink>Hai detto sì [2]", ". <think>x</think>Fine"]) as llama:
        world.llama = llama
        ev = run("Cosa ho detto?")
    text = "".join(e["text"] for e in ev if e["type"] == "delta")
    assert text == "Hai detto sì [2]. Fine" and "think" not in text
    assert ev[-1]["used"] == [2]


def test_used_ignores_unknown_numbers(world):
    with FakeLlama(["Boh [1] [7] [2]"]) as llama:
        world.llama = llama
        assert run("Cosa ho detto?")[-1]["used"] == [1, 2]


def test_history_in_prompt_and_elliptic_query(world):
    hist = [{"role": "user", "content": "Cosa ho promesso a Marco?"},
            {"role": "assistant", "content": "Il vino [1]."}]
    with FakeLlama(["Anche il pane [1]"]) as llama:
        world.llama = llama
        run("E a Giulia?", hist)
        msgs = llama.bodies[0]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"] == "Cosa ho promesso a Marco?" and msgs[3]["content"] == "E a Giulia?"
    assert world.index.calls[0]["query"] == "Cosa ho promesso a Marco? E a Giulia?"


def test_long_question_does_not_borrow_history(world):
    hist = [{"role": "user", "content": "Altra cosa"}]
    with FakeLlama(["x"]) as llama:
        world.llama = llama
        run("Cosa ho promesso a Marco la settimana scorsa", hist)
    assert world.index.calls[0]["query"] == "Cosa ho promesso a Marco la settimana scorsa"


def test_history_capped_at_four_turns(world):
    hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(12)]
    with FakeLlama(["x"]) as llama:
        world.llama = llama
        run("Una domanda abbastanza lunga da sola per non prendere storia", hist)
        msgs = llama.bodies[0]["messages"]
    assert [m["content"] for m in msgs[1:-1]] == [f"m{i}" for i in range(4, 12)]


def test_context_budget(world):
    world.index.hits = [hit(i, text="parola " * 120) for i in range(1, 9)]
    with FakeLlama(["x"]) as llama:
        world.llama = llama
        ev = run("Cosa ho detto?")
    n = len(ev[1]["items"])
    assert 1 <= n < 8
    assert len(llama.bodies[0]["messages"][0]["content"].split("Fonti:")[1]) < ask.CONTEXT_CHARS + 800


def test_person_filter_and_fuzzy(world, archive):
    write_people(archive, {"aabbccddeeff": {"name": "Rosalinda"}})
    world.index.by_persons = world.index.hits
    with FakeLlama(["x"]) as llama:
        world.llama = llama
        run("Cosa ho detto a Rosolinda ieri?")
    assert world.index.calls[0]["persons"] == ["aabbccddeeff"]


def test_person_filter_retry_and_boost(world, archive):
    write_people(archive, {"aabbccddeeff": {"name": "Rosalinda"}})
    a, b = hit(1), hit(2, persons=["me", "aabbccddeeff"])
    world.index.hits, world.index.by_persons = [a, b], [a]  # one hit with the filter: retry without
    with FakeLlama(["x"]) as llama:
        world.llama = llama
        ev = run("Cosa ho detto a Rosalinda?")
    assert [c["row_id"] for c in world.index.calls and ev[1]["items"]] == [f"{SESSION}/2", f"{SESSION}/1"]
    assert [c["persons"] for c in world.index.calls] == [["aabbccddeeff"], None]


def test_me_is_not_a_filter(world, archive, monkeypatch):
    monkeypatch.setattr("omy.diarize.my_name", lambda: "Pierpaolo")
    with FakeLlama(["x"]) as llama:
        world.llama = llama
        run("Cosa ha detto Pierpaolo ieri?")
    assert world.index.calls[0]["persons"] is None


def test_model_failure_is_an_error_event(world):
    world.llama = SimpleNamespace(url="http://127.0.0.1:9")  # nobody listens
    ev = run("Cosa ho detto?")
    assert ev[-1]["type"] == "error" and "message" in ev[-1]


def test_log_has_no_answer_text(world, archive):
    with FakeLlama(["Risposta segreta [1]"]) as llama:
        world.llama = llama
        run("Cosa ho detto?")
    line = json.loads(ask.LOG.read_text().splitlines()[-1])
    assert set(line) == {"ts", "question_chars", "hits", "not_found", "timings"}
    assert line["question_chars"] == len("Cosa ho detto?") and line["hits"] == 2 and line["not_found"] is False
    assert "segreta" not in ask.LOG.read_text()


# --- endpoints -----------------------------------------------------------------------------

@pytest.fixture
def client(archive):
    from omy_receiver.app import Config, create_app
    return TestClient(create_app(Config(token=TOKEN.encode(), archive=archive)))


def sse(text):
    return [json.loads(p[6:]) for p in text.split("\n\n") if p.startswith("data: ")]


def test_ask_endpoint_streams(client, world):
    with FakeLlama(["Ciao [1]"]) as llama:
        world.llama = llama
        r = client.post("/v1/ask", json={"question": "Cosa ho detto?", "now": "2026-11-23T14:30:00+01:00"},
                        headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    ev = sse(r.text)
    assert ev[0] == {"type": "status", "stage": "searching"} and ev[-1]["type"] == "done"
    assert ev[-1]["used"] == [1]


def test_ask_endpoint_not_found(client, world):
    world.index.hits = []
    ev = sse(client.post("/v1/ask", json={"question": "Boh?"}, headers=AUTH).text)
    assert ev[-1]["not_found"] is True and world.chat_calls == 0


def test_ask_needs_token(client, world):
    assert client.post("/v1/ask", json={"question": "x"}).status_code == 401
    assert client.post("/v1/ask", json={"question": "x"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert world.index.calls == []


@pytest.mark.parametrize("body", [
    {}, {"question": ""}, {"question": "   "}, {"question": 5}, {"question": "x" * 501},
    {"question": "ok", "history": "no"}, {"question": "ok", "history": [{"role": "system", "content": "x"}]},
    {"question": "ok", "history": [{"role": "user"}]}, {"question": "ok", "history": [{"role": "user", "content": "x"}] * 9},
    {"question": "ok", "now": "ieri"}, {"question": "ok", "now": 5}, [],
])
def test_ask_validation(client, world, body):
    assert client.post("/v1/ask", json=body, headers=AUTH).status_code == 400
    assert world.index.calls == []


def test_ask_accepts_500_chars(client, world):
    world.index.hits = []
    assert client.post("/v1/ask", json={"question": "x" * 500}, headers=AUTH).status_code == 200


def test_note_endpoint(client, archive):
    from omy.brain import NOTES
    folder = NOTES / "2026" / "11" / "21"
    folder.mkdir(parents=True)
    (folder / "1814-cena-aafa6412.json").write_text(json.dumps({"id": SESSION, "titolo": "Cena"}))
    r = client.get(f"/v1/brain/note/{SESSION}", headers=AUTH)
    assert r.status_code == 200 and r.json() == {"id": SESSION, "titolo": "Cena"}
    assert client.get("/v1/brain/note/20261121T171433Z-00000000", headers=AUTH).status_code == 404
    assert client.get("/v1/brain/note/..%2Fx", headers=AUTH).status_code in (400, 404)
    assert client.get("/v1/brain/note/abc", headers=AUTH).status_code == 400
    assert client.get(f"/v1/brain/note/{SESSION}").status_code == 401


def test_relevant_needs_lexical_or_close_vector():
    from omy.brain import ask
    near = {"score": 0.016, "lexical": False, "cos": 0.31}
    assert not ask.relevant([near])
    assert ask.relevant([near, {"score": 0.016, "lexical": True, "cos": None}])
    assert ask.relevant([{"score": 0.016, "lexical": False, "cos": 0.62}])
    assert not ask.relevant([])


# --- review fixes ------------------------------------------------------------------------

def test_demand_is_touched_before_retrieval(world, monkeypatch):
    order = []
    monkeypatch.setattr(ask.jobs, "touch_demand", lambda role="chat", at=None: order.append(("touch", role)))
    real = world.index.search
    world.index.search = lambda *a, **k: order.append(("search",)) or real(*a, **k)
    world.index.hits = []
    run("Cosa ho detto?")
    assert order[0] == ("touch", "chat") and ("search",) in order


def test_citation_session_day_from_sidecar_or_first_row(world):
    world.index._read_sidecars = lambda s: {SESSION: {"data": "2026-11-20"}}
    with FakeLlama(["x [1]"]) as llama:
        world.llama = llama
        ev = run("Cosa ho detto?")
    assert {c["session_day"] for c in ev[1]["items"]} == {"2026-11-20"}
    world.index._read_sidecars = lambda s: {}
    world.index._read_rows = lambda s: {SESSION: [{"start": "2026-11-19T23:50:00+01:00"}]}
    with FakeLlama(["x [1]"]) as llama:
        world.llama = llama
        ev = run("Cosa ho detto?")
    assert {c["session_day"] for c in ev[1]["items"]} == {"2026-11-19"}


PEOPLE = {"a1": {"name": "Sara"}, "a2": {"name": "Marco Rossi"}, "a3": {"name": "Marco Bianchi"},
          "a4": {"name": "Marzio"}, "a5": {"name": "Rosalinda"}, "a6": {"name": "Anna"}}


@pytest.mark.parametrize("q,sure,maybe", [
    ("Cosa ho detto a Sara?", ["a1"], []),
    ("Cosa sarà domani?", [], []),                       # the accent: "sarà" is not Sara
    ("Cosa ho fatto a marzo?", [], []),                  # a month is not Marzio
    ("Cosa ho fatto a Marzo?", [], []),
    ("Cosa ho detto a Rosolinda?", ["a5"], []),          # capitalized, mid-sentence: fuzzy
    ("Rosolinda cosa ha detto?", [], []),                # sentence start: no fuzzy evidence
    ("cosa ha detto rosolinda?", [], []),                # not capitalized: no fuzzy
    ("Annalisa cosa ha detto?", [], []),                 # exact respects word boundaries
    ("Cosa ho detto a Marco?", [], ["a2", "a3"]),        # ambiguous first name
    ("Cosa ho detto a Marco Rossi?", ["a2"], []),
])
def test_person_groups(archive, q, sure, maybe):
    write_people(archive, PEOPLE)
    assert ask.person_groups(q) == (sure, maybe)


def test_ambiguous_first_name_does_not_filter_but_boosts(world, archive):
    write_people(archive, PEOPLE)
    a, b = hit(1), hit(2, persons=["a3"])
    world.index.hits = [a, b]
    with FakeLlama(["x"]) as llama:
        world.llama = llama
        ev = run("Cosa ho detto a Marco ieri?")
    assert [c["persons"] for c in world.index.calls] == [None]
    assert [c["row_id"] for c in ev[1]["items"]] == [f"{SESSION}/2", f"{SESSION}/1"]


def test_short_followup_inherits_range_and_person(world, archive):
    write_people(archive, {"aabbccddeeff": {"name": "Rosalinda"}})
    world.index.by_persons = world.index.hits
    hist = [{"role": "user", "content": "Cosa ho detto a Rosalinda ieri?"}, {"role": "assistant", "content": "Boh"}]
    with FakeLlama(["x"]) as llama:
        world.llama = llama
        run("e poi?", hist)
    c = world.index.calls[0]
    assert c["persons"] == ["aabbccddeeff"] and c["day_from"] == c["day_to"] == "2026-11-22"


def test_close_response_and_late_put_on_client_exit(world, monkeypatch):
    closed, outcome = threading.Event(), {}

    class Resp:
        def close(self):
            closed.set()

    def fake_stream(base_url, messages, put, stop, holder):
        import time
        holder["resp"] = Resp()
        put("ciao")
        stop.wait(5)
        time.sleep(0.5)  # the loop is closed by now
        try:
            put("late")
        except Exception as e:  # noqa: BLE001
            outcome["error"] = e
        outcome["done"] = True

    monkeypatch.setattr(ask, "_stream_llama", fake_stream)
    world.llama = SimpleNamespace(url="http://x")

    async def go():
        gen = ask.answer("Cosa ho detto?", [], NOW)
        async for ev in gen:
            if ev["type"] == "delta":
                break
        await gen.aclose()

    asyncio.run(go())
    assert closed.is_set()
    deadline = __import__("time").time() + 5
    while "done" not in outcome and __import__("time").time() < deadline:
        __import__("time").sleep(0.05)
    assert outcome.get("done") and "error" not in outcome
