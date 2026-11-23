import json
import stat

import pytest
from fake_llama import FakeLlama, tokens_for
from helpers import record, write_day

from omy import diarize
from omy.brain import NOTES, WORK, confidence, llm, organize

SID = "20261029T080000Z-0000abcd"
DAY = "2026-10-29"
MEDIA = {"kind": "media", "app": "YouTube"}


@pytest.fixture(autouse=True)
def sandbox(archive, monkeypatch):
    monkeypatch.setattr(organize, "_check_preempt", lambda: None)
    diarize.PEOPLE.parent.mkdir(parents=True, exist_ok=True)
    diarize.PEOPLE.write_text(json.dumps({"people": {"p1": {"name": "Rosalinda", "created": "x"},
                                                      "p2": {"name": "Giulia Verdi", "created": "x"}}}))
    yield


def speakers(extra: dict | None = None):
    d = archive_dir() / "transcripts" / DAY / "speakers"
    d.mkdir(parents=True, exist_ok=True)
    clusters = {
        "SPEAKER_00": {"name": "Io", "person": "me", "seconds": 120.0, "context": None},
        "SPEAKER_01": {"name": "Rosalinda", "person": "p1", "seconds": 80.5, "context": None},
        "SPEAKER_02": {"name": "Persona 1", "person": None, "seconds": 5.0, "context": None},
        "SPEAKER_03": {"name": "Video", "person": None, "seconds": 9.0, "context": {"kind": "media", "app": "YouTube"},
                       "source": "media"},
    }
    (d / f"{SID}.json").write_text(json.dumps({"session": SID, "clusters": {**clusters, **(extra or {})},
                                               "turns": []}))


def archive_dir():
    from omy.config import ARCHIVE
    return ARCHIVE


def basic_rows(text1="Ciao Rosalinda, ti mando il preventivo del giardino venerdì prossimo, ci penso io."):
    return [
        record(SID, DAY, "08:00", "Io", "me", text1),
        record(SID, DAY, "08:01", "Rosalinda", "p1", "Perfetto, io chiamo Mario domani per la casa di Savona."),
        record(SID, DAY, "08:02", "Io", "me", "Il tre dicembre abbiamo la cena con tutti quanti.",
               place={"name": "Casa", "lat": 1, "lon": 2, "acc": 5}),
        record(SID, DAY, "08:03", "Video", None, "Oggi vediamo come potare le rose in autunno", via=MEDIA),
        record(SID, DAY, "08:04", "Persona 1", None, "mh"),
    ]


def setup_basic():
    write_day(archive_dir(), DAY, basic_rows())
    speakers()


def part(**over):
    d = {"titolo": "Preventivo del giardino", "qualita": "buona", "riassunto_parte": "Hai promesso il preventivo.",
         "punti_chiave": [], "entita": {"persone": [], "luoghi": [], "organizzazioni": [], "progetti": []},
         "impegni_e_todo": [], "date_ed_eventi_citati": [], "ascoltato": [], "tag": []}
    d.update(over)
    return d


def full_response():
    return part(
        punti_chiave=[{"testo": "Preventivo del giardino", "refs": ["P1", "P4"]},
                      {"testo": "Preventivo del giardino.", "refs": ["P2"]},
                      {"testo": "Solo nel video", "refs": ["P4"]}],
        entita={"persone": [{"nome": "Rosalinda", "refs": ["P2"]}, {"nome": "Mario", "refs": ["P2"]},
                            {"nome": "Giulia", "refs": ["P2"]}, {"nome": "Persona 1", "refs": ["P5"]},
                            {"nome": "Io", "refs": ["P1"]}],
                "luoghi": [{"nome": "Savona", "refs": ["P2"]}], "organizzazioni": [], "progetti": []},
        impegni_e_todo=[
            {"chi": "Marco", "cosa": "mandare il preventivo", "verso_chi": "Rosalinda",
             "scadenza_testo": "venerdì prossimo", "refs": ["P1"]},
            {"chi": "Rosalinda", "cosa": "chiamare Mario", "verso_chi": "null", "scadenza_testo": "domani",
             "refs": ["P2"]},
            {"chi": "Io", "cosa": "fantasma", "verso_chi": None, "scadenza_testo": None, "refs": ["P99"]},
            {"chi": "Io", "cosa": "dal video", "verso_chi": None, "scadenza_testo": None, "refs": ["P4"]}],
        date_ed_eventi_citati=[{"testo_data": "il 3 dicembre", "evento": "cena", "refs": ["P3"]},
                               {"testo_data": "stasera", "evento": "boh", "refs": ["P3"]}],
        ascoltato=[{"riassunto": "Come potare le rose", "refs": ["P4"]}, {"riassunto": "non media", "refs": ["P1"]}],
        tag=["giardino", "Giardino", "cena"])


def run(respond, sid=SID):
    with FakeLlama(respond) as srv:
        path = organize.organize_session(sid, srv.url)
    return path, srv


def load(path):
    return json.loads(path.read_text())


def test_single_part_note_and_post_checks():
    setup_basic()
    path, srv = run(lambda b, n: (full_response(), {"chiamare Mario": 0.3}))
    assert len(srv.calls) == 1
    note = load(path)
    assert path == NOTES / "2026/10/29" / "0800-preventivo-del-giardino-0000abcd.json"
    assert organize.note_path(SID) == path
    assert note["schema_version"] == 1 and note["id"] == SID and note["data"] == DAY
    assert note["ora_inizio"] == "2026-10-29T08:00:00+01:00" and note["luogo"] == {"name": "Casa"}
    assert note["titolo"] == "Preventivo del giardino" and note["qualita"] == "buona"
    assert note["riassunto"] == "Hai promesso il preventivo."
    assert note["parti"] == [{"da": "P1", "a": "P5", "riassunto": "Hai promesso il preventivo."}]
    assert note["collegamenti"] == [] and note["elaborazione"]["n_parti"] == 1
    assert note["elaborazione"]["prompt_version"] == 1 and note["elaborazione"]["input_hash"] == organize.input_hash(SID)
    # participants: the media voice is not one
    assert [(p["label"], p["person"], p["nome_al_momento"]) for p in note["partecipanti"]] == [
        ("SPEAKER_00", "me", "Io"), ("SPEAKER_01", "p1", "Rosalinda"), ("SPEAKER_02", None, "Persona 1")]
    assert note["paragrafi"]["P4"]["media"] is True and note["paragrafi"]["P4"]["label"] is None
    assert note["paragrafi"]["P2"] == {"riga": f"{SID}/08:01", "inizio": "2026-10-29T08:01:00+01:00",
                                       "fine": "2026-10-29T08:01:30+01:00", "label": "SPEAKER_01", "media": False}
    # media refs only in ascoltato; duplicate points merged
    assert note["punti_chiave"] == [{"testo": "Preventivo del giardino", "refs": ["P1", "P2"],
                                     "conf": note["punti_chiave"][0]["conf"]}]
    assert note["ascoltato"] == [{"riassunto": "Come potare le rose", "app": "YouTube", "refs": ["P4"]}]
    # names -> ids; Persona N and yourself dropped; Giulia unknown to the session but a known person's first name
    pers = {p["nome"]: p["person_id"] for p in note["entita"]["persone"]}
    assert pers == {"Rosalinda": "p1", "Mario": None, "Giulia": "p2"}
    assert note["entita"]["luoghi"][0]["nome"] == "Savona"
    # commitments: the speaker of the cited paragraph wins over the model's "Marco"
    todo = note["impegni_e_todo"]
    assert [t["cosa"] for t in todo] == ["mandare il preventivo", "chiamare Mario"]
    assert (todo[0]["chi"], todo[0]["chi_id"], todo[0]["verso_chi_id"]) == ("Io", "me", "p1")
    assert todo[0]["scadenza_testo"] == "venerdì prossimo" and todo[0]["scadenza"] == "2026-10-30"
    assert (todo[1]["chi"], todo[1]["chi_id"], todo[1]["verso_chi"], todo[1]["scadenza"]) == (
        "Rosalinda", "p1", None, "2026-10-30")
    assert todo[0]["stato"] == "aperto"
    ev = note["date_ed_eventi_citati"]
    assert [(e["testo_data"], e["data"]) for e in ev] == [("il 3 dicembre", "2026-12-03"), ("stasera", None)]
    assert note["tag"] == ["giardino", "cena"]
    # confidence: the weak commitment is flagged
    assert todo[1]["conf"]["minima"] == pytest.approx(0.3, abs=0.01) and todo[1]["conf"]["media"] < 0.97
    assert todo[0]["conf"]["minima"] > 0.9
    assert note["confidenza"]["minima"] <= 0.97 and 0 < note["confidenza"]["media"] <= 1
    assert note["da_rivedere"] == [{"campo": "impegni_e_todo[1]", "motivo": "confidenza 0.30 < 0.40"}]
    # files: 600, dirs 700, no leftovers
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for d in (path.parent, path.parent.parent, path.parent.parent.parent, NOTES):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == [path.name]
    assert not (WORK / SID).exists()


def test_prompt_has_static_prefix_and_context_at_the_end():
    setup_basic()
    _, srv = run(lambda b, n: part())
    system = srv.calls[0]["messages"][0]["content"]
    user = srv.calls[0]["messages"][1]["content"]
    assert system.index("Estrai SOLO") < system.index("CONTESTO DI QUESTA CONVERSAZIONE")
    assert "giovedì 2026-10-29" in system and "Rosalinda" in system
    assert system.split("CONTESTO DI QUESTA CONVERSAZIONE")[0].count("{") == 0
    assert "l'utente" in system  # only as a prohibition
    assert user.splitlines()[0].startswith("[P1] 08:00 Io: Ciao Rosalinda")
    assert "[P4] 08:03 [MEDIA YouTube]: Oggi vediamo" in user
    schema = srv.calls[0]["response_format"]["json_schema"]["schema"]
    assert "confidence" not in json.dumps(schema) and "riassunto_parte" in schema["properties"]


def test_call_rows_are_marked():
    rows = basic_rows()
    rows[1] = record(SID, DAY, "08:01", "Rosalinda", "p1", "Perfetto, io chiamo Mario domani.",
                     via={"kind": "call", "app": "FaceTime"})
    write_day(archive_dir(), DAY, rows)
    speakers()
    _, srv = run(lambda b, n: part())
    assert "[P2] 08:01 Rosalinda (in chiamata): Perfetto" in srv.calls[0]["messages"][1]["content"]


def test_trivial_session_is_noise_without_llm():
    write_day(archive_dir(), DAY, [record(SID, DAY, "08:00", "Io", "me", "Sì, ok."),
                                   record(SID, DAY, "08:01", "Video", None, "x" * 500, via=MEDIA)])
    path, srv = run(lambda b, n: part())
    assert srv.calls == []
    note = load(path)
    assert note["qualita"] == "rumore" and note["titolo"] == "Sì, ok."
    assert note["punti_chiave"] == [] and note["impegni_e_todo"] == [] and note["ascoltato"] == []
    assert note["entita"] == {"persone": [], "luoghi": [], "organizzazioni": [], "progetti": []}
    assert note["elaborazione"]["n_parti"] == 0 and not organize.needs_organize(SID)


def long_rows(n=30, size=700):
    rows = []
    for i in range(n):
        who, pid = ("Io", "me") if i % 2 == 0 else ("Rosalinda", "p1")
        rows.append(record(SID, DAY, f"08:{i:02d}", who, pid, (f"parola{i} " * 200)[:size]))
    return rows


def multi_respond(body, n):
    props = body["response_format"]["json_schema"]["schema"]["properties"]
    if "riassunto_parte" not in props:
        return {"titolo": "Titolo finale", "qualita": "parziale", "riassunto": "Riassunto finale."}
    if n == 0:
        return part(riassunto_parte="Prima parte.", punti_chiave=[{"testo": "Punto A", "refs": ["P1"]}],
                    entita={"persone": [{"nome": "Davide", "refs": ["P2"]}], "luoghi": [], "organizzazioni": [],
                            "progetti": []},
                    impegni_e_todo=[{"chi": "Io", "cosa": "mandare il preventivo", "verso_chi": None,
                                     "scadenza_testo": None, "refs": ["P1"]}])
    return part(riassunto_parte="Seconda parte.", punti_chiave=[{"testo": "Punto B", "refs": ["P25"]}],
                entita={"persone": [{"nome": "davide", "refs": ["P26"]}], "luoghi": [], "organizzazioni": [],
                        "progetti": []},
                impegni_e_todo=[{"chi": "Io", "cosa": "Mandare il preventivo!", "verso_chi": None,
                                 "scadenza_testo": None, "refs": ["P27"]}])


def test_multi_part_merge_and_reduce():
    write_day(archive_dir(), DAY, long_rows())
    speakers()
    path, srv = run(multi_respond)
    assert len(srv.calls) == 3  # two parts + reduce
    s0, s1 = (c["messages"][0]["content"] for c in srv.calls[:2])
    assert s0 == s1  # identical prefix across the parts
    assert "Parte 1" in srv.calls[2]["messages"][1]["content"] and "Seconda parte." in srv.calls[2]["messages"][1]["content"]
    note = load(path)
    assert note["titolo"] == "Titolo finale" and note["qualita"] == "parziale"
    assert note["riassunto"] == "Riassunto finale."
    assert len(note["parti"]) == 2 and note["elaborazione"]["n_parti"] == 2
    p0, p1 = note["parti"]
    assert p0["da"] == "P1" and p1["a"] == "P30" and int(p1["da"][1:]) == int(p0["a"][1:]) + 1
    assert [p["riassunto"] for p in note["parti"]] == ["Prima parte.", "Seconda parte."]
    assert [(p["nome"], p["refs"]) for p in note["entita"]["persone"]] == [("Davide", ["P2", "P26"])]
    assert [t["testo"] for t in note["punti_chiave"]] == ["Punto A", "Punto B"]
    assert len(note["impegni_e_todo"]) == 1 and note["impegni_e_todo"][0]["refs"] == ["P1", "P27"]
    lines = organize.Session(SID).lines  # parts are of a sensible size
    a, b = organize.split_parts(lines)
    size = lambda r: sum(organize._tokens(x) for x in lines[r[0]:r[1] + 1])  # noqa: E731
    assert 2000 <= size(a) <= 4000 and 2000 <= size(b) <= 4000


def test_resume_from_checkpoint_after_unavailable():
    write_day(archive_dir(), DAY, long_rows())
    speakers()
    with FakeLlama(lambda b, n: None if n == 1 else multi_respond(b, n)) as srv:
        with pytest.raises(llm.LLMUnavailable):
            organize.organize_session(SID, srv.url)
        assert len(srv.calls) == 2
        assert (WORK / SID / "part-1.json").is_file() and not (WORK / SID / "part-2.json").exists()
        assert stat.S_IMODE((WORK / SID / "part-1.json").stat().st_mode) == 0o600
        assert organize.note_path(SID) is None
        srv.respond = lambda b, n: multi_respond(b, 0 if n == 2 else 1 if n == 3 else 2)
        path = organize.organize_session(SID, srv.url)
    assert len(srv.calls) == 4  # part 2 and reduce only: part 1 was not asked again
    assert load(path)["parti"][0]["riassunto"] == "Prima parte."
    assert not (WORK / SID).exists()


def test_checkpoint_ignored_when_the_input_changed():
    write_day(archive_dir(), DAY, long_rows())
    speakers()
    with FakeLlama(lambda b, n: None if n == 1 else multi_respond(b, n)) as srv:
        with pytest.raises(llm.LLMUnavailable):
            organize.organize_session(SID, srv.url)
        rows = long_rows()
        rows[0]["text"] = "testo cambiato " * 40
        write_day(archive_dir(), DAY, rows)
        srv.respond = multi_respond
        organize.organize_session(SID, srv.url)
    assert len(srv.calls) == 5  # part 1 again: its key no longer matches


def test_idempotency_and_needs_organize():
    setup_basic()
    assert organize.needs_organize(SID)
    run(lambda b, n: part())
    assert not organize.needs_organize(SID)
    h = organize.input_hash(SID)
    assert organize.input_hash(SID) == h
    rows = basic_rows("Ciao Rosalinda, ti mando il preventivo del giardino lunedì, ci penso io.")
    write_day(archive_dir(), DAY, rows)
    assert organize.needs_organize(SID) and organize.input_hash(SID) != h
    path = organize.note_path(SID)
    run(lambda b, n: part(titolo="Altro titolo"))
    assert organize.note_path(SID) == path  # same file, no orphan
    assert not organize.needs_organize(SID)


def test_unknown_session():
    assert not organize.needs_organize("20261029T080000Z-ffffffff")
    with pytest.raises(organize.OrganizeError):
        organize.organize_session("20261029T080000Z-ffffffff", "http://127.0.0.1:1")


def test_no_speakers_document_uses_row_names():
    write_day(archive_dir(), DAY, basic_rows())
    path, _ = run(lambda b, n: part())
    names = {p["nome_al_momento"] for p in load(path)["partecipanti"]}
    assert names == {"Io", "Rosalinda", "Persona 1"}


def test_item_confidence_spans_with_accents_and_refs_excluded():
    data = {"impegni_e_todo": [{"chi": "Io", "cosa": "andare à Perugia", "refs": ["P1"]},
                               {"chi": "Rosalinda", "cosa": "chiamare", "refs": ["P2"]}],
            "titolo": "Città"}
    text = json.dumps(data, ensure_ascii=False)
    toks = tokens_for(text, {"à Perugia": 0.2, '"P2"': 0.01}, size=3)
    spans = confidence.value_spans(text)
    assert (("impegni_e_todo", 0, "cosa")) in [p for p, s, e in spans]
    confs = confidence.item_confs(text, toks)
    assert confs["impegni_e_todo[0]"]["minima"] == pytest.approx(0.2, abs=0.01)
    assert confs["impegni_e_todo[1]"]["minima"] > 0.9  # the weak "P2" ref is not content
    assert "titolo" not in confs
    note = confidence.note_conf(text, toks)
    assert note["minima"] == pytest.approx(0.2, abs=0.01)


def test_da_rivedere_only_for_listed_fields():
    low = {"media": 0.5, "minima": 0.1}
    note = {"impegni_e_todo": [{"conf": low}], "date_ed_eventi_citati": [{"conf": {"media": 1, "minima": 0.9}}],
            "entita": {"persone": [{"conf": low}], "luoghi": [{"conf": low}]}, "punti_chiave": [{"conf": low}]}
    assert [d["campo"] for d in confidence.da_rivedere(note)] == ["impegni_e_todo[0]", "entita.persone[0]"]
    assert confidence.THRESHOLD == 0.40


def test_split_parts_boundaries():
    lines = [f"[P{i}] 08:00 Io: " + "x" * 500 for i in range(1, 11)]
    assert organize.split_parts(lines[:3]) == [(0, 2)]
    parts = organize.split_parts(lines * 6)
    assert parts[0][0] == 0 and parts[-1][1] == 59
    assert all(b[0] == a[1] + 1 for a, b in zip(parts, parts[1:]))
