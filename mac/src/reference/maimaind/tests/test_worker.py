import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fakellm import FakeEmbedder, FakeLLM, say, tool

from brain import config, gate, summarize
from brain.llm import LLM, Aborted
from brain.worker import Deps, Question, Worker

EXTRACTION = json.dumps({"title": "T", "quality": "good", "summary": [{"text": "x", "refs": ["P1"]}], "topics": [],
                         "commitments": [], "facts": [], "heard": []})


class FakeServer:
    name = "fake"

    def __init__(self, client, pid=None):
        self.client, self.starts, self.stops, self._pid = client, 0, 0, pid

    def pid(self):
        return self._pid

    def start(self, profile=None):
        self.starts += 1
        return self.client

    def stop(self):
        self.stops += 1


class FakeEmbedClient:
    """What EmbedServer.start returns: something embed.Embedder can call."""

    def _request(self, path, body):
        vecs = FakeEmbedder().embed(body["input"])
        return {"data": [{"index": i, "embedding": v} for i, v in enumerate(vecs)]}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def world(tmp_path, conn, transcripts):
    s = config.Settings(data_dir=tmp_path / "data", transcripts=transcripts, vault=tmp_path / "vault",
                        omy_src=tmp_path, models={"gemma": config.MODELS["gemma"]},
                        embed_models=config.EMBED_MODELS)
    state = {"progress": {"running": False}, "gate": True, "killed": [], "ran": [], "started": [],
             "alive": True, "cmdline": "uv run omy --json periodic", "notes": []}
    clock = Clock()
    llm = FakeLLM(*[say(EXTRACTION)] * 50)
    deps = Deps(
        settings=s,
        gate=lambda loaded=False: gate.Gate(state["gate"], "ok" if state["gate"] else "memoria sotto pressione", {}, {}),
        progress=lambda: state["progress"],
        features=lambda: {"auto_transcribe": True},
        omy_run=lambda *a: state["ran"].append(a) or {},
        omy_start=lambda *a: state["started"].append(a),
        people=lambda: {"me": {"name": "Io"}, "people": [{"id": "a1b2c3d4e5f6", "name": "Rosa Linda"},
                                                         {"id": "0f1e2d3c4b5a", "name": "Marco"}]},
        chat=FakeServer(llm), embedder=FakeServer(FakeEmbedClient()),
        kill=lambda pid, sig: state["killed"].append((pid, sig)) or state.update(alive=False),
        alive=lambda pid: state["alive"], cmdline=lambda pid: state["cmdline"],
        notify=state["notes"].append, clock=clock, sleep=clock.sleep)
    w = Worker(deps, conn)
    return w, state, clock, llm


def test_nothing_runs_while_local_omy_transcribes(world):
    w, state, _, _ = world
    state["progress"] = {"running": True, "pid": 42}
    w.tick()
    assert w.d.chat.starts == 0 and "trascrivendo" in w.status
    assert summarize.pending(w.conn)


def test_a_round_summarizes_and_embeds_then_unloads_when_idle(world):
    w, state, clock, _ = world
    w.tick()
    assert summarize.pending(w.conn) == []
    assert w.conn.execute("SELECT COUNT(*) FROM vec_map WHERE kind = 'summary'").fetchone()[0] > 0
    assert (w.s.vault / "commitments.md").is_file()
    assert w.llm is not None
    clock.t += w.s.idle_minutes * 60 + 1
    w.tick()
    assert w.llm is None and w.d.chat.stops >= 1


def test_local_omy_starting_stops_the_round(world):
    w, state, _, llm = world
    calls = {"n": 0}

    def first_then_omy(messages):
        calls["n"] += 1
        state["progress"] = {"running": True, "pid": 42}
        return {"content": EXTRACTION}
    llm.steps = [first_then_omy] * 10
    w.tick()
    assert calls["n"] == 1                       # the job in progress finished…
    assert w.llm is None                         # …then the model was unloaded
    assert len(summarize.pending(w.conn)) == 4   # the others wait


def question(text="Ho mai detto di non fidarmi delle banche?"):
    events = []
    return Question(text, on_event=lambda k, d: events.append((k, d))), events


def test_a_question_pauses_a_long_transcription_and_resumes_it(world):
    w, state, clock, llm = world
    state["progress"] = {"running": True, "pid": 42, "minutes_total": 120, "minutes_done": 10, "session": "s"}
    llm.steps = [tool("search_text", query="banche"), say("Sì [R1].")]
    q, events = question()
    w.submit(q)
    state_after_kill = {"running": False}
    w.d.progress = lambda: state_after_kill if state["killed"] else state["progress"]
    w.tick()
    assert state["killed"] == [(42, signal.SIGINT)]
    assert q.done.is_set() and q.answer.found and q.answer.citations[0]["channel"] == "me"
    assert any(k == "tool" for k, _ in events) and w.paused_omy
    assert state["notes"]
    # Idle: the model goes, local-omy comes back (day files first).
    clock.t += w.s.idle_minutes * 60 + 1
    w.tick()
    assert state["ran"] == [("transcribe", "--pending", "--render")]
    assert state["started"] == [("transcribe", "--pending")]
    assert not w.paused_omy


def test_a_transcription_about_to_end_is_waited_for(world):
    w, state, clock, llm = world
    state["progress"] = {"running": True, "pid": 42, "minutes_total": 20, "minutes_done": 19}  # ~4 s left
    llm.steps = [tool("search_text", query="banche"), say("Sì [R1].")]
    original = clock.sleep

    def sleep(s):
        original(s)
        state["progress"] = {"running": False}
    w.d.sleep = sleep
    q, _ = question()
    w.submit(q)
    w.tick()
    assert state["killed"] == [] and q.done.is_set()


def test_without_interrupting_the_question_waits_with_text_search(world):
    w, state, _, llm = world
    w.s.interrupt_omy = False
    state["progress"] = {"running": True, "pid": 42, "minutes_total": 120, "minutes_done": 10}
    q, events = question()
    w.submit(q)
    w.tick()
    assert not q.done.is_set() and state["killed"] == []
    kind, data = events[0]
    assert kind == "queued" and "trascrivendo" in data["reason"] and "banche" in data["text_search"]
    # local-omy finishes: the next tick answers.
    state["progress"] = {"running": False}
    llm.steps = [tool("search_text", query="banche"), say("Sì [R1].")]
    w.tick()
    assert q.done.is_set() and q.answer.found
    assert [k for k, _ in events].count("queued") == 1


def test_a_pid_that_is_not_omy_is_never_signalled(world):
    w, state, _, _ = world
    state["cmdline"] = "/usr/bin/python something_else.py"
    state["progress"] = {"running": True, "pid": 42, "minutes_total": 120, "minutes_done": 10}
    q, _ = question()
    w.submit(q)
    w.tick()
    assert state["killed"] == [] and not q.done.is_set()


def test_a_question_cuts_off_a_job_call(world):
    w, _, _, _ = world

    class Abortable:
        aborted = False

        def abort(self):
            self.aborted = True

    w.llm = Abortable()
    with w.busy:
        w.submit(Question("x"))
    assert w.llm.aborted


# --- the client against a real HTTP server -------------------------------------------------

def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_streaming_collects_text_and_tool_calls():
    chunks = [{"choices": [{"delta": {"content": "Ciao "}}]}, {"choices": [{"delta": {"content": "Io"}}]},
              {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "search_text",
                                                                                          "arguments": "{\"q"}}]}}]},
              {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "uery\": \"x\"}"}}]}}],
               "timings": {"predicted_n": 4}}]

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *a):
            pass

    srv = serve(H)
    got = []
    try:
        r = LLM(f"http://127.0.0.1:{srv.server_port}").chat([{"role": "user", "content": "x"}], on_token=got.append)
    finally:
        srv.shutdown()
    assert got == ["Ciao ", "Io"] and r["message"]["content"] == "Ciao Io"
    assert r["message"]["tool_calls"][0]["function"] == {"name": "search_text", "arguments": '{"query": "x"}'}
    assert r["timings"] == {"predicted_n": 4}


def test_abort_cuts_a_request_off():
    class Slow(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(3)

        def log_message(self, *a):
            pass

    srv = serve(Slow)
    llm = LLM(f"http://127.0.0.1:{srv.server_port}")
    threading.Timer(0.3, llm.abort).start()
    t = time.monotonic()
    try:
        with pytest.raises(Aborted):
            llm.chat([{"role": "user", "content": "x"}])
    finally:
        srv.shutdown()
    assert time.monotonic() - t < 2


def test_gate_tolerates_the_model_s_own_pressure(monkeypatch, tmp_path):
    from brain import gate as g
    s = config.Settings(data_dir=tmp_path, transcripts=tmp_path, vault=tmp_path, omy_src=tmp_path,
                        models={"gemma": config.MODELS["gemma"]}, embed_models=config.EMBED_MODELS)
    monkeypatch.setattr(g.omy, "transcription", lambda s: {"running": False})
    for level, loaded, open_ in ((1, False, True), (2, False, False), (2, True, True), (4, True, False)):
        monkeypatch.setattr(g, "memory", lambda level=level: {"pressure_level": level, "available_percent": 60})
        assert g.check(s, model_loaded=loaded).open is open_, (level, loaded)


def test_servers_left_by_a_previous_run_are_stopped(world):
    w, _, _, _ = world
    w.d.chat._pid = 4242  # a llama-server still running from a daemon that was killed
    w.clear_leftovers()
    assert w.d.chat.stops == 1 and w.d.embedder.stops == 1
    w.d.chat._pid = None
    w.clear_leftovers()
    assert w.d.chat.stops == 1  # nothing left: nothing stopped
