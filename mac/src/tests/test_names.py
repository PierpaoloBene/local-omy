import json

from omy import diarize
from omy.brain import names


def _people(people):
    diarize.PEOPLE.parent.mkdir(parents=True, exist_ok=True)
    diarize.PEOPLE.write_text(json.dumps({"people": people}))


SC = {
    "id": "20261121T171433Z-aafa6412", "data": "2026-11-21",
    "partecipanti": [
        {"label": "SPEAKER_00", "person": "me", "nome_al_momento": "Io", "secondi": 10},
        {"label": "SPEAKER_01", "person": "p_anna", "nome_al_momento": "Anna", "secondi": 10},
        {"label": "SPEAKER_02", "person": None, "nome_al_momento": "Persona 2", "secondi": 5},
    ],
    "impegni_e_todo": [{"chi": "Anna", "chi_id": "p_anna", "verso_chi": "Io", "verso_chi_id": "me", "cosa": "x"}],
}


def test_current_name(archive):
    _people({"p_anna": {"name": "Rosalinda"}})
    assert names.current_name("p_anna", "Anna") == "Rosalinda"
    assert names.current_name("p_gone", "Anna") == "Anna"      # deleted or merged away
    assert names.current_name(None, "Persona 2") == "Persona 2"
    assert names.current_name("me", "Io") == diarize.my_name()
    _people({"p_a": {"name": "", "merged_into": "p_b"}, "p_b": {"name": "Anna Rossi"}})
    assert names.current_name("p_a", "Anna") == "Anna Rossi"


def test_rewrite_word_boundary_longest_first(archive):
    _people({"p_anna": {"name": "Rosalinda"}})
    sc = {**SC, "partecipanti": SC["partecipanti"] + [
        {"label": "SPEAKER_03", "person": "p_x", "nome_al_momento": "Anna Maria"}]}
    _people({"p_anna": {"name": "Rosalinda"}, "p_x": {"name": "Maria Anna"}})
    out = names.rewrite("Anna e Anna Maria vedono Annalisa, Persona 2 e Persona 22; anna no.", sc)
    assert out == "Rosalinda e Maria Anna vedono Annalisa, Persona 2 e Persona 22; anna no."


def test_rewrite_pinned_persona_label(archive):
    _people({"p_marco": {"name": "Marco"}})
    day = archive / "transcripts" / "2026-11-21" / "speakers"
    day.mkdir(parents=True)
    (day / f"{SC['id']}.json").write_text(json.dumps({"clusters": {"SPEAKER_02": {"name": "Marco", "person": "p_marco"}}}))
    n = names.Names(SC)
    assert n.rewrite("Persona 2 chiede un favore") == "Marco chiede un favore"
    assert "p_marco" in n.person_ids()


def test_chi_verso_chi(archive):
    _people({"p_anna": {"name": "Rosalinda"}})
    n = names.Names(SC)
    it = SC["impegni_e_todo"][0]
    assert n.chi(it) == "Rosalinda"
    assert n.verso_chi(it) == diarize.my_name()
