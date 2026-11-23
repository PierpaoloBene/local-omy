import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fakellm import FakeLLM, say, tool

from brain import ask, evaluate
from brain.llm import LLM

TODAY = date(2026, 10, 29)


@pytest.mark.parametrize("q, channels", [
    ("Ho mai detto Y?", ("me",)),
    ("ho detto che non mi fido delle banche?", ("me",)),
    ("Did I ever say that?", ("me",)),
    ("Cosa ho sentito in quel podcast su X?", ("heard",)),
    ("Di cosa parlava il video di ieri?", ("heard",)),
    ("Ho sentito Marco ieri?", ("me", "said", "heard")),
    ("Ho parlato con Rosa Linda di X?", ("me", "said")),
])
def test_constraints_from_the_question(q, channels):
    assert ask.constraints_for(q).channels == channels


def test_answer_cites_what_was_found(conn):
    llm = FakeLLM(tool("search_text", query="preventivo"),
                  lambda m: say("Sì, le hai promesso il preventivo [R1]."))
    a = ask.ask("Ho promesso qualcosa ad Rosa Linda?", conn, llm, today=TODAY)
    assert a.found and a.text == "Sì, le hai promesso il preventivo [1]."
    assert a.citations[0]["session"] == "20261020T170000Z-aaaa0001"
    assert a.citations[0]["speaker"] == "Io" and a.citations[0]["day"] == "2026-10-20"
    # The first search is already made when the model starts: on the question.
    first = llm.requests[0]["messages"]
    assert first[-2]["tool_calls"][0]["function"]["name"] == "search" and first[-1]["role"] == "tool"
    assert llm.requests[0]["tool_choice"] == "auto"


def test_invented_references_are_stripped(conn):
    llm = FakeLLM(tool("search_text", query="preventivo"), say("Sì [R1], e anche altro [R7] [S9]."))
    a = ask.ask("Ho promesso qualcosa?", conn, llm, today=TODAY)
    assert a.text == "Sì [1], e anche altro."
    assert a.invalid_refs == ["R7", "S9"]


def test_no_valid_citation_means_not_found(conn):
    llm = FakeLLM(tool("search_text", query="astronomia"),
                  say("Sì, avete parlato di stelle e pianeti per un'ora."))
    a = ask.ask("Ho parlato con Rosa Linda di astronomia?", conn, llm, today=TODAY)
    assert not a.found and a.text == ask.NOT_FOUND and a.citations == []


def test_did_i_say_only_sees_my_words(conn):
    llm = FakeLLM(tool("search_text", query="banche", channel=["said"]), say("Sì [R1]."))
    a = ask.ask("Ho mai detto di non fidarmi delle banche?", conn, llm, today=TODAY)
    results = llm.tool_results()[0]
    assert "Persona 1" not in results and "Io" in results
    assert [c["channel"] for c in a.citations] == ["me"]
    assert "solo paragrafi del canale me" in llm.requests[0]["messages"][1]["content"]


def test_media_questions_only_see_media(conn):
    llm = FakeLLM(tool("search_text", query="intelligenza artificiale"), say("Parlava di memoria [R1]."))
    a = ask.ask("Cosa ho sentito in quel podcast sull'intelligenza artificiale?", conn, llm, today=TODAY)
    assert [c["channel"] for c in a.citations] == ["heard"]


def test_media_never_answers_a_conversation_question(conn):
    # The model goes for the podcast's paragraph: it was never returned, so it cannot be cited.
    llm = FakeLLM(tool("search_text", query="ristrutturazione", channel=["heard"]),
                  say("Ne avete parlato [R1] [R2]."))
    a = ask.ask("Ho parlato con Rosa Linda della ristrutturazione?", conn, llm, today=TODAY)
    assert {c["channel"] for c in a.citations} <= {"me", "said"}
    assert "MEDIA" not in llm.tool_results()[0]


def test_bad_tool_arguments_go_back_to_the_model(conn):
    def broken(_):
        return {"tool_calls": [{"id": "x", "type": "function",
                                "function": {"name": "search_text", "arguments": "{query: banche"}}]}
    llm = FakeLLM(broken, tool("search_text", query="banche"), say("Sì [R1]."))
    a = ask.ask("Le banche?", conn, llm, today=TODAY)
    assert a.found
    assert [c["valid"] for c in a.tool_calls] == [True, False, True]
    assert llm.tool_results()[1].startswith("Errore")


def test_rounds_are_capped(conn):
    llm = FakeLLM(*[tool("search_text", query="bagno")] * (ask.MAX_ROUNDS + 3))
    ask.ask("Il bagno?", conn, llm, today=TODAY)
    assert len(llm.requests) == ask.MAX_ROUNDS + 1
    assert llm.requests[-1]["tools"] is None  # last round: answer with what there is


def test_system_prompt_has_dates_and_people(conn):
    p = ask.system_prompt(conn, TODAY)
    assert "giovedì 2026-10-29" in p and "settimana scorsa: 2026-10-19 → 2026-10-25" in p
    assert "Rosa Linda, Marco" in p


def test_session_citation(conn):
    llm = FakeLLM(tool("filter", person="Marco"), say("Il 22 ottobre [S1]."))
    a = ask.ask("Che giorno ho parlato con Marco?", conn, llm, today=TODAY)
    assert a.citations[0]["kind"] == "session" and a.citations[0]["day"] == "2026-10-22"


def test_eval_scoring(conn):
    llm = FakeLLM(tool("search_text", query="banche"), say("Sì [R1]."))
    a = ask.ask("Ho mai detto di non fidarmi delle banche?", conn, llm, today=TODAY)
    row = evaluate.score({"id": "x", "sessions": ["20261027T090000Z-dddd0004"], "channels": ["me"]}, a)
    assert row["correct"] and row["tool_calls_valid"] == 2
    assert not evaluate.score({"id": "y", "not_found": True}, a)["correct"]


def test_llm_client_sends_the_key_and_reads_the_answer():
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["auth"] = self.headers.get("Authorization")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                               "timings": {"prompt_n": 3}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = LLM(f"http://127.0.0.1:{srv.server_port}", api_key="k" * 40).chat(
            [{"role": "user", "content": "ciao"}], tools=[{"type": "function"}], tool_choice="required")
    finally:
        srv.shutdown()
    assert r["message"]["content"] == "ok" and r["timings"]["prompt_n"] == 3
    assert seen["auth"] == "Bearer " + "k" * 40
    assert seen["body"]["tool_choice"] == "required"


def test_empty_answer_is_asked_again_without_tools(conn):
    llm = FakeLLM(tool("search_text", query="preventivo"), say(""), say("Il preventivo [R1]."))
    a = ask.ask("Cosa ho promesso?", conn, llm, today=TODAY)
    assert a.found and a.text == "Il preventivo [1]."
    assert llm.requests[-1]["tools"] is None


def test_eval_accepts_a_no_with_context(conn):
    llm = FakeLLM(tool("search_text", query="bagno"),
                  say("Non ho trovato niente sull'astronomia; con Rosa Linda hai parlato del bagno [R1]."))
    a = ask.ask("Ho parlato con Rosa Linda di astronomia?", conn, llm, today=TODAY)
    assert a.found
    assert evaluate.score({"id": "x", "not_found": True}, a)["correct"]
    llm = FakeLLM(tool("search_text", query="bagno"), say("Sì, del bagno [R1]."))
    a = ask.ask("Ho parlato con Rosa Linda di astronomia?", conn, llm, today=TODAY)
    assert not evaluate.score({"id": "x", "not_found": True}, a)["correct"]


def test_the_first_search_can_be_enough(conn):
    llm = FakeLLM(say("A Siracusa, a giugno [R1]."))
    a = ask.ask("Dove pensavamo di andare in vacanza?", conn, llm, today=TODAY)
    assert a.found and a.rounds == 1 and a.citations[0]["session"] == "20261028T180000Z-eeee0005"


def test_a_short_follow_up_searches_with_the_question_before():
    history = [{"role": "user", "content": "Ho il contratto di locazione?"}, {"role": "assistant", "content": "Sì [1]."}]
    assert ask.first_query("Me lo mandi?", history) == "Ho il contratto di locazione? Me lo mandi?"
    assert ask.first_query("Me lo mandi?", None) == "Me lo mandi?"
