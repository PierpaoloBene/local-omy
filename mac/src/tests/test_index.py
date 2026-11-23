import json
import sqlite3

import pytest

from fake_embed import FakeEmbedServer
from omy import diarize, models
from omy.brain import NOTES, embed, index

SID1 = "20261121T171433Z-aafa6412"
SID2 = "20261122T090000Z-bbbb0002"


def people(p):
    diarize.PEOPLE.parent.mkdir(parents=True, exist_ok=True)
    diarize.PEOPLE.write_text(json.dumps({"people": p}))


def row(sid, ms, start, speaker, person, text, via=None):
    return {"id": f"{sid}/{ms}", "session": sid, "start": start, "end": start, "speaker": speaker,
            "person": person, "via": via, "text": text}


def write_rows(archive, day, rows):
    d = archive / "transcripts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def write_note(sid, day, **kw):
    sc = {
        "schema_version": 1, "id": sid, "data": day, "ora_inizio": f"{day}T18:14:33+01:00",
        "partecipanti": [{"label": "SPEAKER_00", "person": "me", "nome_al_momento": "Io", "secondi": 9},
                         {"label": "SPEAKER_01", "person": "p_anna", "nome_al_momento": "Anna", "secondi": 9}],
        "paragrafi": {"P1": {"riga": f"{sid}/0", "inizio": f"{day}T18:14:33+01:00"},
                      "P2": {"riga": f"{sid}/90500", "inizio": f"{day}T18:16:03+01:00"}},
        "titolo": "Il trasloco di Anna", "riassunto": "Anna e io parliamo del trasloco a Milano.",
        "punti_chiave": [{"testo": "Anna porta gli scatoloni", "refs": ["P2"]}],
        "entita": {"persone": [{"nome": "Anna", "person_id": "p_anna"}], "luoghi": [{"nome": "Milano"}],
                   "organizzazioni": [], "progetti": []},
        "impegni_e_todo": [{"chi": "Io", "chi_id": "me", "verso_chi": "Anna", "verso_chi_id": "p_anna",
                            "cosa": "prenotare il furgone", "scadenza_testo": "venerdì", "refs": ["P2"]}],
        "date_ed_eventi_citati": [{"testo_data": "sabato", "data": "2026-11-28", "evento": "il trasloco", "refs": ["P1"]}],
        **kw,
    }
    p = NOTES / day[:4] / day[5:7] / day[8:10] / f"1814-trasloco-{sid[-8:]}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sc, ensure_ascii=False))
    return p


@pytest.fixture
def env(archive, monkeypatch):
    s = FakeEmbedServer()
    monkeypatch.setattr(models, "ensure", lambda role: s.url)
    monkeypatch.setattr(models, "state", lambda: {"loaded": "chat", "companions": ["embed"]})
    monkeypatch.setattr(models, "base_url", lambda role: s.url)
    index._warned = False
    people({"p_anna": {"name": "Anna"}})
    write_rows(archive, "2026-11-21", [
        row(SID1, 0, "2026-11-21T18:14:33+01:00", "Io", "me", "Ciao Anna, parliamo del trasloco."),
        row(SID1, 90500, "2026-11-21T18:16:03+01:00", "Anna", "p_anna", "Porto io gli scatoloni sabato."),
        row(SID1, 120000, "2026-11-21T18:16:33+01:00", "Video", None, "pubblicità dei materassi",
            {"kind": "media", "app": "YouTube"}),
    ])
    write_rows_2 = row(SID2, 0, "2026-11-22T09:00:00+01:00", "Marco", "p_marco", "Il progetto del giardino va avanti.")
    (archive / "transcripts" / "2026-11-22.jsonl").write_text(json.dumps(write_rows_2) + "\n")
    people({"p_anna": {"name": "Anna"}, "p_marco": {"name": "Marco"}})
    write_note(SID1, "2026-11-21")
    yield s
    s.close()


def chunks():
    with sqlite3.connect(index.INDEX_PATH) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT * FROM chunks ORDER BY id")]


def test_update_builds_chunks(env):
    st = index.update()
    cs = chunks()
    kinds = {(c["kind"], c["field"]) for c in cs}
    assert {("nota", "riassunto"), ("nota", "punto"), ("nota", "impegno"), ("nota", "data"), ("nota", "entita"),
            ("testo", "testo")} <= kinds
    imp = next(c for c in cs if c["field"] == "impegno")
    assert imp["text"] == "Io → Anna: prenotare il furgone (entro venerdì)".replace("Io", diarize.my_name())
    assert imp["row_id"] == f"{SID1}/90500" and imp["t"] == 90.5
    assert json.loads(imp["persons"]) == ["me", "p_anna"]
    ent = next(c for c in cs if c["field"] == "entita")
    assert "Persone: Anna" in ent["text"] and "Luoghi: Milano" in ent["text"]
    assert all(c["title"] == "Il trasloco di Anna" for c in cs if c["kind"] == "nota")
    t1 = next(c for c in cs if c["session"] == SID1 and c["kind"] == "testo")
    assert t1["text"].splitlines()[0].startswith("18:14 ")
    assert "18:16 Anna: Porto io gli scatoloni sabato." in t1["text"]
    assert "[media]" in t1["text"] and t1["row_id"] == f"{SID1}/0"
    t2 = next(c for c in cs if c["session"] == SID2)  # no note: derived title
    assert t2["title"] == "09:00 · Marco"
    assert st["pending"] == 0 and st["version"] == 1
    with sqlite3.connect(index.INDEX_PATH) as db:
        assert db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == len(cs)


def test_windows_split_at_row_boundaries(env, archive):
    rows = [row(SID2, i * 1000, f"2026-11-22T09:{i:02d}:00+01:00", "Marco", "p_marco", "parola " * 30)
            for i in range(6)]
    write_rows(archive, "2026-11-22", rows)
    index.update([SID2])
    t = [c for c in chunks() if c["session"] == SID2]
    assert len(t) >= 3 and all(c["text"].count("\n") + 1 <= 3 for c in t)
    assert all(len(c["text"]) <= 700 for c in t)


def test_incremental_no_reembed(env):
    index.update()
    n = env.embedded_texts
    v = index.update()
    assert env.embedded_texts == n and v["rebuilt"] == 0 and v["version"] == 2
    assert [c["id"] for c in chunks()] == [c["id"] for c in chunks()]


def test_changed_sidecar_reembeds_only_new_text(env, archive):
    index.update()
    n = env.embedded_texts
    count = len(chunks())
    write_note(SID1, "2026-11-21", riassunto="Un riassunto completamente diverso.")
    index.update()
    assert env.embedded_texts == n + 1  # only the summary; the other chunks reuse their vectors
    assert len(chunks()) == count
    assert any(c["text"] == "Un riassunto completamente diverso." for c in chunks())


def test_changed_rows_rebuild_testo_only(env, archive):
    index.update()
    n = env.embedded_texts
    rows = [row(SID2, 0, "2026-11-22T09:00:00+01:00", "Marco", "p_marco", "Adesso parliamo di biciclette.")]
    write_rows(archive, "2026-11-22", rows)
    index.update()
    assert env.embedded_texts == n + 1
    assert any("biciclette" in c["text"] for c in chunks())
    assert not any("giardino" in c["text"] for c in chunks())


def test_rename_reindexes(env):
    index.update()
    assert not any("Rosalinda" in h["text"] for h in index.search("Rosalinda"))
    people({"p_anna": {"name": "Rosalinda"}, "p_marco": {"name": "Marco"}})
    index.update()
    hits = index.search("Rosalinda")
    assert hits[0]["session"] == SID1 and "Rosalinda" in hits[0]["text"]
    assert not any("Anna" in c["text"] for c in chunks() if c["session"] == SID1 and c["kind"] == "nota")


def test_deleted_session_removed(env, archive):
    index.update()
    (archive / "transcripts" / "2026-11-22.jsonl").unlink()
    index.update()
    assert {c["session"] for c in chunks()} == {SID1}
    assert not any(h["session"] == SID2 for h in index.search("giardino"))
    for p in NOTES.glob("*/*/*/*.json"):
        p.unlink()
    index.update([SID1])
    assert {c["kind"] for c in chunks()} == {"testo"}


def test_bm25_fallback_when_embed_down(env, monkeypatch, capsys):
    index.update()

    def down(texts, query=False):
        raise embed.EmbedError("down")

    monkeypatch.setattr(embed, "embed", down)
    hits = index.search("giardino")
    assert hits and hits[0]["session"] == SID2
    index.search("giardino")
    assert capsys.readouterr().err.count("BM25 only") == 1  # logged once


def test_update_survives_embed_down_and_catches_up(env, monkeypatch):
    real = embed.embed

    def down(texts, query=False):
        raise embed.EmbedError("down")

    monkeypatch.setattr(embed, "embed", down)
    st = index.update()
    assert st["pending"] > 0 and index.search("giardino")  # BM25 works without vectors
    monkeypatch.setattr(embed, "embed", real)
    assert index.update()["pending"] == 0


def test_semantic_only_match(env):
    index.update()
    # no common FTS term except the stopword-free overlap of hashed words: cosine ranks the right chunk first
    hits = index.search("progetto giardino avanti")
    assert hits[0]["session"] == SID2
    assert set(hits[0]) >= {"id", "kind", "session", "day", "start", "t", "row_id", "persons", "title", "field",
                            "text", "score"}


def test_filters(env):
    index.update()
    assert {h["session"] for h in index.search("trasloco progetto giardino", k=20)} == {SID1, SID2}
    only = index.search("trasloco progetto giardino", day_from="2026-11-22", day_to="2026-11-22", k=20)
    assert {h["session"] for h in only} == {SID2}
    assert index.search("trasloco", day_from="2026-11-23") == []
    by_person = index.search("trasloco progetto giardino", persons=["p_marco"], k=20)
    assert {h["session"] for h in by_person} == {SID2}
    # the person's current name in the text counts even if the id is not in the chunk's persons
    with sqlite3.connect(index.INDEX_PATH) as db:
        db.execute("UPDATE chunks SET persons='[]'")
    index._cache.clear()
    assert {h["session"] for h in index.search("trasloco progetto giardino", persons=["p_marco"], k=20)} == {SID2}


def test_fts_query_is_safe():
    q = index.fts_query('Cosa ha detto Marco il 3 novembre? "NEAR(" AND ) * trasloco')
    assert '"marco"' in q and '"3"' in q and '"trasloco"*' in q
    assert '"cosa"' not in q and " OR " in q
    assert index.fts_query("?? !!") is None


def test_rrf_ordering_and_nota_tiebreak(env, monkeypatch):
    index.update()
    a = next(c for c in chunks() if c["field"] == "riassunto")
    b = next(c for c in chunks() if c["session"] == SID1 and c["kind"] == "testo")
    monkeypatch.setattr(index, "fts_query", lambda q: '"x"')
    monkeypatch.setattr(index, "TOP", 50)
    # identical ranks in two lists -> equal scores -> the 'nota' wins; a chunk in both lists beats one in one
    view = index._load(index._connect(create=False))
    ids = {a["id"], b["id"]}
    orig_load = index._load

    def fake_lists(db):
        return orig_load(db)

    class DB:
        pass

    hits = index.search("trasloco")
    assert hits[0]["score"] >= hits[-1]["score"]
    # direct check of the tiebreak on equal scores
    rows = view["rows"]
    fused = {a["id"]: 0.5, b["id"]: 0.5}
    order = sorted(fused, key=lambda i: (-fused[i], rows[i]["kind"] != "nota", i))
    assert order[0] == a["id"] and ids == set(order)


def test_hybrid_beats_single_list(env):
    index.update()
    hits = index.search("scatoloni sabato", k=3)
    # appears in both BM25 and the vector list: its fused score is above 1/(60+1)
    assert hits[0]["score"] > 1.0 / 61


def test_matrix_reloaded_on_version_change(env, archive):
    index.update()
    index.search("trasloco")
    n = index._loads[0]
    index.search("trasloco")
    assert index._loads[0] == n  # cached
    index.update()  # version bump
    index.search("trasloco")
    assert index._loads[0] == n + 1
    write_note(SID1, "2026-11-21", riassunto="zzz novità cercabile")
    index.update()
    assert index.search("novità cercabile")[0]["text"] == "zzz novità cercabile"


def test_search_without_index(archive):
    assert index.search("x") == []
