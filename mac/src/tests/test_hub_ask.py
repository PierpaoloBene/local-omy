"""Chiedi on the hub: queued while the Mac is off, answered when it comes back, relayed live when it is there."""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from helpers import record, write_day
from test_compute import COMPUTE, MAC, PHONE, TOKEN, caps, lease_body
from test_compute_client import TestHub

from omy import asks, compute, hubstate, jobs, models
from omy.brain import ask as ask_mod, embed, index

SID = "20261029T080000Z-0000abcd"
Q = {"question": "Che progetto abbiamo discusso per la casa nuova?", "history": []}


@pytest.fixture
def hub(archive, monkeypatch):
    from omy_receiver.app import Config, create_app
    monkeypatch.setenv("OMY_ROLE", "hub")
    hubstate.PATH.unlink(missing_ok=True)
    write_day(archive, "2026-10-29", [record(SID, "2026-10-29", "10:00", "Io", "me",
                                             "Parliamo del progetto per la casa nuova, il giardino e la cucina " * 3)])
    index.update()
    with TestClient(create_app(Config(token=TOKEN.encode(), archive=archive, compute_token=COMPUTE.encode()))) as c:
        yield c


def sse(client, body=None):
    events = []
    with client.stream("POST", "/v1/ask", json=body or Q, headers=PHONE) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    return events


def mac_worker(client, tmp_path, monkeypatch, answer="La casa nuova [1].", qvec_seen=None):
    w = compute.ComputeWorker(TestHub(client), workspace=tmp_path / "ws", context_dir=tmp_path / "ctx")
    qvec_seen = [] if qvec_seen is None else qvec_seen
    monkeypatch.setattr(embed, "embed", lambda texts, query=False: qvec_seen.append(texts) or
                        __import__("numpy").ones((len(texts), embed.EMBED_DIM), dtype="float32"))
    monkeypatch.setattr(models, "request_chat", lambda: "http://fake-chat")

    def stream(base_url, messages, put, stop, holder):
        for piece in (answer[:5], answer[5:]):
            put(piece)
        put(ask_mod._DONE)
    monkeypatch.setattr(ask_mod, "_stream_llama", stream)
    return w


def test_with_the_mac_off_the_question_is_queued_and_answered_later(hub, tmp_path, monkeypatch):
    events = sse(hub)
    assert [e["type"] for e in events] == ["status", "status", "citations", "delta", "done"]
    ask_id = events[1]["stage"].removeprefix("queued:")
    assert events[-1]["queued"] is True and events[-1]["ask_id"] == ask_id
    [a] = hub.get("/v1/asks", headers=PHONE).json()["asks"]
    assert a["id"] == ask_id and a["state"] == "pending" and a["answer"] == ""
    assert asks.summary()["waiting"] == 1

    # the Mac comes back: its ask loop takes the question and answers it
    seen = []
    w = mac_worker(hub, tmp_path, monkeypatch, qvec_seen=seen)
    nxt = hub.get("/compute/v1/ask/next?wait=0", headers=MAC).json()["ask"]
    assert nxt["id"] == ask_id and nxt["embed_text"] == Q["question"]
    w.answer_ask(nxt)
    assert seen == [[Q["question"]]]                      # the Mac embedded the retrieval query
    done = hub.get(f"/v1/ask/{ask_id}", headers=PHONE).json()
    assert done["state"] == "done" and done["answer"] == "La casa nuova [1]." and done["used"] == [1]
    assert done["citations"] and done["citations"][0]["session"] == SID
    assert hub.get("/compute/v1/ask/next?wait=0", headers=MAC).status_code == 204
    assert asks.summary()["waiting"] == 0


def test_with_the_mac_there_the_answer_streams_live(hub, tmp_path, monkeypatch):
    hubstate.record("mac", "b", caps())                   # the Mac is online
    w = mac_worker(hub, tmp_path, monkeypatch)

    def mac():
        for _ in range(100):
            r = hub.get("/compute/v1/ask/next?wait=0", headers=MAC)
            if r.status_code == 200:
                w.answer_ask(r.json()["ask"])
                return
            time.sleep(0.05)
    t = threading.Thread(target=mac)
    t.start()
    events = sse(hub)
    t.join()
    types = [e["type"] for e in events]
    assert types[0] == "status" and "citations" in types and types[-1] == "done"
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "La casa nuova [1]."
    assert events[-1]["used"] == [1] and "queued" not in events[-1]
    assert not any(a["state"] == "pending" for a in hub.get("/v1/asks", headers=PHONE).json()["asks"])


def test_nothing_relevant_is_answered_by_the_hub_alone(hub, tmp_path, monkeypatch):
    w = mac_worker(hub, tmp_path, monkeypatch)
    events = sse(hub, {"question": "zzzz qqqq xxxx", "history": []})
    ask_id = events[1]["stage"].removeprefix("queued:")
    w.answer_ask(hub.get("/compute/v1/ask/next?wait=0", headers=MAC).json()["ask"])
    a = hub.get(f"/v1/ask/{ask_id}", headers=PHONE).json()
    assert a["state"] == "done" and a["not_found"] is True and a["answer"] == ask_mod.NOT_FOUND


def test_a_lease_that_runs_out_gives_the_question_to_the_next_poll(hub):
    ask_id = asks.create("q?", [], "2026-10-29T10:00:00+01:00")
    t = time.time()
    a = asks.next_for("mac", now=t)
    assert asks.next_for("mac", now=t + 1) is None                        # being answered
    b = asks.next_for("mac", now=t + asks.LEASE_S + 1)                    # the Mac died: another try
    assert b["id"] == ask_id and b["lease_id"] != a["lease_id"]
    assert not asks.append(ask_id, a["lease_id"], "late text")            # the old lease cannot write


def test_old_questions_expire_and_can_be_cancelled(hub):
    old = asks.create("old", [], None)
    cancelled = asks.create("never mind", [], None)
    assert hub.delete(f"/v1/ask/{cancelled}", headers=PHONE).json() == {"cancelled": True}
    assert asks.expire(now=time.time() + asks.EXPIRE_S + 5) == 1
    assert asks.get(old)["state"] == "expired" and asks.next_for("mac") is None


def test_ask_endpoints_need_the_right_token(hub):
    assert hub.get("/v1/asks").status_code == 401
    assert hub.get("/v1/ask/0123456789abcdef", headers=PHONE).status_code == 404
    assert hub.get("/v1/ask/nope", headers=PHONE).status_code == 404
    assert hub.get("/compute/v1/ask/next", headers=PHONE).status_code == 401
    assert hub.post("/compute/v1/ask/0123456789abcdef/events", json={"lease_id": "x", "events": []},
                    headers=MAC).status_code == 409
