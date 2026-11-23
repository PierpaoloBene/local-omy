import json

import pytest
from fakellm import FakeLLM, say

from brain import ingest, summarize
from brain.tools import Tools

A = "20261020T170000Z-aaaa0001"   # Io and Rosa Linda, the quote
B = "20261022T080000Z-bbbb0002"   # call with Marco
C = "20261023T190000Z-cccc0003"   # podcast
E = "20261028T180000Z-eeee0005"   # holidays, plus an unknown voice


def extraction(**over):
    base = {"title": "Bagno", "quality": "good", "summary": [], "topics": [], "commitments": [], "facts": [],
            "heard": []}
    return json.dumps({**base, **over})


def test_items_are_backed_by_the_session_paragraphs(conn):
    rows = summarize.session_rows(conn, A)
    data = summarize.check(conn, rows, json.loads(extraction(
        summary=[{"text": "Hai sentito tre ditte", "refs": ["P1"]},
                 {"text": "Inventato", "refs": ["P9"]},
                 {"text": "Senza refs", "refs": []}],
        topics=[{"name": "Bagno", "refs": ["P1"]}, {"name": "bagno", "refs": ["P2"]}])))
    assert [i["text"] for i in data["summary"]] == ["Hai sentito tre ditte"]
    assert data["summary"][0]["refs"] == [f"{A}/0"]
    assert [t["slug"] for t in data["topics"]] == ["bagno"]
    assert data["dropped"] == 2


def test_media_only_backs_heard(conn):
    rows = summarize.session_rows(conn, C)
    data = summarize.check(conn, rows, json.loads(extraction(
        summary=[{"text": "Hai parlato di IA", "refs": ["P1"]}],
        facts=[{"about": "Io", "fact": "ama l'IA", "refs": ["P1"]}],
        heard=[{"summary": "IA e memoria", "refs": ["P1", "P2"]}])))
    assert data["summary"] == [] and data["facts"] == []
    assert data["heard"][0]["app"] == "YouTube" and len(data["heard"][0]["refs"]) == 2

    rows = summarize.session_rows(conn, A)
    data = summarize.check(conn, rows, json.loads(extraction(heard=[{"summary": "x", "refs": ["P1"]}])))
    assert data["heard"] == []


def test_who_promised_is_who_said_it(conn):
    rows = summarize.session_rows(conn, A)
    data = summarize.check(conn, rows, json.loads(extraction(commitments=[
        # The model says Rosa Linda promised; P3 is you speaking.
        {"who": "Rosa Linda", "to": "Rosa Linda", "what": "mandare il preventivo", "due": "2026-10-21",
         "due_text": "domani mattina", "refs": ["P3"]}])))
    c = data["commitments"][0]
    assert c["owner"] == "me" and c["owner_label"] == "Io"
    assert c["counterpart"] == "a1b2c3d4e5f6" and c["due"] == "2026-10-21"


def test_promise_by_a_named_caller(conn):
    rows = summarize.session_rows(conn, B)
    data = summarize.check(conn, rows, json.loads(extraction(commitments=[
        {"who": "Marco", "to": "Io", "what": "girare il contratto", "due": "venerdì", "due_text": "entro venerdì",
         "refs": ["P2"]}])))
    c = data["commitments"][0]
    assert c["owner"] == "0f1e2d3c4b5a" and c["counterpart"] == "me"
    assert c["due"] is None and c["due_text"] == "entro venerdì"  # not a date: kept as said


def test_unnamed_voices_never_become_people(conn):
    rows = summarize.session_rows(conn, E)
    data = summarize.check(conn, rows, json.loads(extraction(facts=[
        {"about": "Sconosciuto", "fact": "cerca un posto", "refs": ["P3"]},
        {"about": "Rosa Linda", "fact": "vuole andare a Siracusa", "refs": ["P1"]}])))
    assert [f["person"] for f in data["facts"]] == [None, "a1b2c3d4e5f6"]


def test_commitment_ids_are_stable(conn):
    rows = summarize.session_rows(conn, A)
    raw = json.loads(extraction(commitments=[{"who": "Io", "to": "Rosa Linda", "what": "mandare il preventivo",
                                              "due": None, "due_text": None, "refs": ["P3"]}]))
    assert summarize.check(conn, rows, raw)["commitments"][0]["id"] == \
        summarize.check(conn, rows, raw)["commitments"][0]["id"]


def test_summarize_stores_and_marks_done(conn):
    llm = FakeLLM(say(extraction(summary=[{"text": "Hai sentito tre ditte", "refs": ["P1"]}],
                                 commitments=[{"who": "Io", "to": "Rosa Linda", "what": "mandare il preventivo",
                                               "due": "2026-10-21", "due_text": "domani", "refs": ["P3"]}])))
    r = summarize.summarize(conn, llm, A, "fake")
    assert r["calls"] == 1 and r["title"] == "Bagno"
    assert conn.execute("SELECT summary_status FROM sessions WHERE id = ?", (A,)).fetchone()[0] == "done"
    assert A not in summarize.pending(conn)
    schema = llm.requests[0]
    assert "[P3]" in schema["messages"][1]["content"]
    # The tools now see it.
    t = Tools(conn)
    assert "«Bagno»" in t.filter(person="Rosa Linda")
    assert "Riassunto:" in t.get_session(A)
    out = t.list_commitments(owner="me")
    assert "mandare il preventivo" in out and "entro mer 21/10/2026" in out and "aperto" in out


def test_session_changed_while_summarizing_stays_stale(conn):
    def changed_meanwhile(_):
        conn.execute("UPDATE sessions SET content_hash = 'new' WHERE id = ?", (A,))
        return {"content": extraction()}
    summarize.summarize(conn, FakeLLM(changed_meanwhile), A, "fake")
    assert conn.execute("SELECT summary_status FROM sessions WHERE id = ?", (A,)).fetchone()[0] == "stale"


def test_long_sessions_are_cut_and_reduced(conn, monkeypatch):
    monkeypatch.setattr(summarize, "WINDOW_CHARS", 150)
    calls = []

    def step(messages):
        calls.append(messages[1]["content"])
        if messages[1]["content"].startswith("Punti estratti"):
            return {"content": json.dumps({"title": "Finale", "quality": "good",
                                           "summary": [{"text": "tutto", "refs": ["P1", "P4"]}]})}
        n = messages[1]["content"].split("]")[0][2:]
        return {"content": extraction(summary=[{"text": f"parte {n}", "refs": [f"P{n}"]}])}

    data, stats = summarize.extract(conn, FakeLLM(*[step] * 10), A)
    assert stats["windows"] > 1 and stats["calls"] == stats["windows"] + 1
    assert data["title"] == "Finale" and data["summary"][0]["refs"] == [f"{A}/0", f"{A}/135000"]


def test_trivial_sessions_skip_the_model(conn, transcripts):
    day = transcripts / "2026-10-29.jsonl"
    day.write_text(json.dumps({"v": 1, "id": "20261029T100000Z-ffff0006/0", "session": "20261029T100000Z-ffff0006",
                               "start": "2026-10-29T11:00:00+01:00", "end": "2026-10-29T11:00:02+01:00",
                               "speaker": "Io", "person": "me", "via": None, "place": None, "language": "it",
                               "text": "Ok, ciao."}) + "\n")
    ingest.sync(conn, transcripts)
    llm = FakeLLM()
    r = summarize.summarize(conn, llm, "20261029T100000Z-ffff0006", "fake")
    assert r["calls"] == 0 and llm.requests == []


def test_invalid_json_fails_the_session(conn):
    with pytest.raises(summarize.SummarizeError):
        summarize.summarize(conn, FakeLLM(say("non è json")), A, "fake")
    assert conn.execute("SELECT summary_status FROM sessions WHERE id = ?", (A,)).fetchone()[0] == "failed"


def test_profiles_after_enough_new_facts(conn):
    facts = [{"about": "Rosa Linda", "fact": f"fatto {i}", "refs": ["P2"]} for i in range(3)]
    summarize.summarize(conn, FakeLLM(say(extraction(facts=facts))), A, "fake")
    assert summarize.profiles_due(conn) == [("a1b2c3d4e5f6", 3)]
    summarize.write_profile(conn, FakeLLM(say("Rosa Linda ha una sorella.")), "a1b2c3d4e5f6")
    assert summarize.profiles_due(conn) == []


def test_nobody_is_not_a_name(conn):
    rows = summarize.session_rows(conn, A)
    data = summarize.check(conn, rows, json.loads(extraction(commitments=[
        {"who": "Io", "to": "null", "what": "far partire il test", "due": None, "due_text": None, "refs": ["P1"]},
        {"who": "Io", "to": "Persona N", "what": "rinviare", "due": None, "due_text": None, "refs": ["P3"]}])))
    assert [c["counterpart_label"] for c in data["commitments"]] == [None, None]
