import json

from brain import fixtures
from brain.tools import BUDGET_CHARS, Constraints, Tools, fts_query


def refs_in(text):
    return [line.split("]")[0][1:] for line in text.splitlines() if line.startswith("[R")]


def channels_of(tools, text):
    return {tools.refs.by_ref[r].row["channel"] for r in refs_in(text)}


def test_default_search_leaves_media_out(conn):
    t = Tools(conn)
    text = t.search_text("ristrutturazione")
    assert channels_of(t, text) == {"me"}
    assert "Video" not in text and "MEDIA" not in text


def test_media_only_when_allowed(conn):
    t = Tools(conn, Constraints(("heard",)))
    text = t.search_text("ristrutturazione")
    assert channels_of(t, text) == {"heard"}
    assert "MEDIA YouTube" in text


def test_model_cannot_widen_the_channels(conn):
    t = Tools(conn, Constraints(("me",)))
    text = t.search_text("banche", channel=["said", "heard"])
    assert channels_of(t, text) == {"me"}
    assert "Persona 1" not in text


def test_without_constraint_others_are_found_too(conn):
    t = Tools(conn)
    text = t.search_text("banche")
    assert channels_of(t, text) == {"me", "said"}


def test_person_filter_resolves_names(conn):
    t = Tools(conn)
    text = t.filter(person="rosa linda")
    assert "aaaa0001" not in text  # sessions are shown by reference
    sessions = {r.id for r in t.refs.by_ref.values() if r.kind == "session"}
    assert sessions == {"20261020T170000Z-aaaa0001", "20261028T180000Z-eeee0005"}


def test_unknown_person_is_an_error_for_the_model(conn):
    t = Tools(conn)
    out = t.call("filter", json.dumps({"person": "Giuseppe"}))
    assert out.startswith("Errore") and "search_text" in out
    assert t.calls[-1]["valid"] is False


def test_get_person_falls_back_to_the_name_in_the_text(conn):
    out = Tools(conn).get_person("Gianni")
    assert "non è una persona registrata" in out and "Gianni" in out.split("\n", 1)[1]


def test_dates_filter(conn):
    t = Tools(conn)
    t.filter(date_from="2026-10-26", date_to="2026-10-29")
    days = {r.row["day"] for r in t.refs.by_ref.values()}
    assert days == {"2026-10-27", "2026-10-28"}


def test_bad_date_is_reported(conn):
    assert Tools(conn).call("filter", {"date_from": "ieri"}).startswith("Errore")


def test_refs_are_stable_within_a_conversation(conn):
    t = Tools(conn)
    a = refs_in(t.search_text("preventivo"))
    b = refs_in(t.search_text("preventivo"))
    assert a == b and len(a) == 1


def test_get_session_by_reference(conn):
    t = Tools(conn)
    t.filter(person="Marco")
    sref = next(r.ref for r in t.refs.by_ref.values() if r.kind == "session")
    text = t.get_session(sref)
    assert "contratto del fornitore" in text and "chiamata Teams" in text


def test_get_session_respects_the_channels(conn):
    t = Tools(conn)
    text = t.get_session("20261023T190000Z-cccc0003")
    assert refs_in(text) == []


def test_phrase_and_prefix(conn):
    t = Tools(conn)
    assert len(refs_in(t.search_text("ti mando il preventivo", mode="frase"))) == 1
    assert len(refs_in(t.search_text("mando preventivi"))) == 1  # preventiv* prefix
    assert fts_query("Ho mai detto banche?", "tutte") == '"banch"*'


def test_any_words_when_all_find_nothing(conn):
    out = Tools(conn).search_text("preventivo astronomia")
    assert "modo: qualsiasi" in out


def test_budget_caps_what_the_model_reads(conn):
    t = Tools(conn)
    t.budget = 600
    out = t.call("search_text", {"query": "il", "mode": "qualsiasi"})
    assert out.endswith("(risultati troncati)") and len(out) <= 630
    assert "rispondi ora" in t.call("search_text", {"query": "bagno"})
    assert BUDGET_CHARS > 5000


def test_fixture_people_ids(conn):
    assert Tools(conn).resolve_person("marco")["id"] == fixtures.MARCO
