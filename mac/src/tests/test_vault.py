import json
import os
import re

import pytest

from omy import diarize
from omy.brain import BRAIN, NOTES, vault


def _people(people):
    diarize.PEOPLE.parent.mkdir(parents=True, exist_ok=True)
    diarize.PEOPLE.write_text(json.dumps({"people": people}))


def sidecar(sid, day, hhmm, *, titolo="Pranzo", partecipanti=None, persone=(), progetti=(), orgs=(),
            impegni=None, da_rivedere=None, luogo="Casa"):
    y, m, d = day.split("-")
    start = f"{day}T{hhmm[:2]}:{hhmm[2:]}:00+01:00"
    sc = {
        "schema_version": 1, "id": sid, "data": day, "ora_inizio": start, "ora_fine": f"{day}T{int(hhmm[:2]) + 1:02d}:{hhmm[2:]}:00+01:00",
        "luogo": {"name": luogo} if luogo else None,
        "partecipanti": partecipanti if partecipanti is not None else [
            {"label": "SPEAKER_00", "person": "me", "nome_al_momento": "Io", "secondi": 100},
            {"label": "SPEAKER_01", "person": "p_anna", "nome_al_momento": "Anna", "secondi": 90}],
        "paragrafi": {"P1": {"inizio": start, "fine": start, "label": "SPEAKER_00", "media": False},
                      "P3": {"inizio": f"{day}T{hhmm[:2]}:{int(hhmm[2:]) + 8:02d}:30+01:00", "fine": start,
                             "label": "SPEAKER_01", "media": False}},
        "titolo": f"{titolo} con Anna", "qualita": "buona", "riassunto": "Anna e io pranziamo.",
        "parti": [{"da": "P1", "a": "P3", "riassunto": "tutto"}],
        "punti_chiave": [{"testo": "Anna propone un viaggio", "refs": ["P3"], "conf": {"media": 0.9, "minima": 0.8}},
                         {"testo": "Dubbio sul budget", "refs": ["P1"], "conf": {"media": 0.5, "minima": 0.2}}],
        "entita": {"persone": [{"nome": n, "person_id": pid, "refs": ["P3"], "conf": {}} for n, pid in persone],
                   "luoghi": [], "organizzazioni": [{"nome": o, "refs": ["P1"]} for o in orgs],
                   "progetti": [{"nome": p, "refs": ["P3"]} for p in progetti]},
        "impegni_e_todo": impegni if impegni is not None else [
            {"chi": "Anna", "chi_id": "p_anna", "verso_chi": "Io", "verso_chi_id": "me", "cosa": "Mandare le foto",
             "scadenza_testo": "venerdì", "scadenza": "2026-11-27", "stato": "aperto", "refs": ["P3"], "conf": {"minima": 0.9}}],
        "date_ed_eventi_citati": [], "ascoltato": [], "tag": ["viaggi"], "collegamenti": [],
        "confidenza": {"media": 0.91, "minima": 0.2}, "da_rivedere": da_rivedere or [],
    }
    path = NOTES / y / m / d / f"{hhmm}-{titolo.lower()}-{sid[-8:]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sc, ensure_ascii=False, indent=2) + "\n")
    return path


A, B, C = "20261121T171433Z-aaaa0001", "20261122T090000Z-bbbb0002", "20261123T100000Z-cccc0003"


@pytest.fixture
def three(archive):
    _people({"p_anna": {"name": "Anna"}, "p_marco": {"name": "Marco"}})
    pa = sidecar(A, "2026-11-21", "1814", persone=[("Anna", "p_anna")], progetti=["Viaggio"], orgs=["Acme"],
                 da_rivedere=[{"campo": "punti_chiave[1]", "motivo": "confidenza 0.20 < 0.40"}])
    marco = [{"label": "SPEAKER_00", "person": "me", "nome_al_momento": "Io", "secondi": 100},
             {"label": "SPEAKER_01", "person": "p_marco", "nome_al_momento": "Marco", "secondi": 50}]
    pb = sidecar(B, "2026-11-22", "1000", titolo="Riunione", partecipanti=marco, progetti=["viaggio"],
                 persone=[("Marco", "p_marco")], impegni=[])
    pc = sidecar(C, "2026-11-23", "1100", titolo="Caffe", orgs=["ACME"])
    return {A: pa, B: pb, C: pc}


def build_all():
    ids = [A, B, C]
    vault.update_links(ids)
    for s in ids:
        vault.render_note(s)
    vault.render_entities(ids)
    vault.render_indexes()


def snapshot():
    return {str(p.relative_to(BRAIN)): p.read_text() for p in sorted(BRAIN.rglob("*.md"))}


def test_note_rendering(three):
    md = vault.render_note(A)
    assert md == three[A].with_suffix(".md")
    t = md.read_text()
    assert t.startswith("---\n")
    assert f'id: "{A}"' in t and "data: 2026-11-21" in t
    assert 'partecipanti: ["[[persone/Anna]]", "[[persone/Io]]"]' in t
    assert 'luogo: "[[luoghi/Casa]]"' in t and "da_rivedere: true" in t
    assert "## Riassunto" in t and "## Punti chiave" in t and "## Impegni" in t
    assert "Parti" not in t  # one part only
    # clickable timestamp from the start of the session: P3 is 8 min 30 after 18:14:00
    assert f"[18:22](omy://session/{A}?t=510&p=P3)" in t
    assert f"- ⚠️ Dubbio sul budget [18:14](omy://session/{A}?t=0&p=P1)" in t
    m = re.search(r"^- \[ \] Anna → Io: Mandare le foto \(entro 2026-11-27\) \[18:22\]\(.*?\) \^(c[0-9a-f]{8})$", t, re.M)
    assert m
    assert "[[progetti/Viaggio]]" in t and "[[organizzazioni/Acme]]" in t
    # file modes
    assert oct(md.stat().st_mode & 0o777) == "0o600"


def test_rename_participant_and_move_page(three):
    build_all()
    assert (BRAIN / "persone" / "Anna.md").is_file()
    (BRAIN / "persone" / "Anna.md").write_text((BRAIN / "persone" / "Anna.md").read_text() + "\nmie note su lei\n")
    _people({"p_anna": {"name": "Rosalinda"}, "p_marco": {"name": "Marco"}})
    assert vault.rerender_people(["p_anna"]) == sorted([A, C])
    t = three[A].with_suffix(".md").read_text()
    assert "Rosalinda propone un viaggio" in t and "Anna propone" not in t
    assert "Rosalinda → Io: Mandare le foto" in t and "[[persone/Rosalinda]]" in t
    assert "Rosalinda pranziamo" not in t and "Rosalinda e io" in t
    assert not (BRAIN / "persone" / "Anna.md").exists()
    page = (BRAIN / "persone" / "Rosalinda.md").read_text()
    assert 'person_id: "p_anna"' in page and 'alias: ["Anna"]' in page
    assert "mie note su lei" in page            # moved, not copied: the user's text survives
    assert len(list((BRAIN / "persone").glob("*.md"))) == 3  # Rosalinda, Io (me), Marco
    assert "Mandare le foto" in page and "[18:22]" in page
    # the sidecar is untouched
    assert json.loads(three[A].read_text())["partecipanti"][1]["nome_al_momento"] == "Anna"


def test_persona_label_pinned_later(archive):
    _people({"p_marco": {"name": "Marco"}})
    part = [{"label": "SPEAKER_00", "person": "me", "nome_al_momento": "Io", "secondi": 1},
            {"label": "SPEAKER_02", "person": None, "nome_al_momento": "Persona 2", "secondi": 1}]
    sidecar(A, "2026-11-21", "1814", partecipanti=part, impegni=[])
    p = json.loads(next(NOTES.rglob("*.json")).read_text())
    p["punti_chiave"][0]["testo"] = "Persona 2 propone un viaggio"
    next(NOTES.rglob("*.json")).write_text(json.dumps(p))
    vault.render_note(A)
    vault.render_entities([A])
    assert "[[persone/Persona 2]]" in next(NOTES.rglob("*.md")).read_text()
    assert (BRAIN / "persone" / "Persona 2.md").is_file()
    d = archive / "transcripts" / "2026-11-21" / "speakers"
    d.mkdir(parents=True)
    (d / f"{A}.json").write_text(json.dumps({"clusters": {"SPEAKER_02": {"name": "Marco", "person": "p_marco"}}}))
    assert vault.rerender_people(["p_marco"]) == [A]
    t = next(NOTES.rglob("*.md")).read_text()
    assert "Marco propone un viaggio" in t and "[[persone/Marco]]" in t and "Persona 2" not in t
    assert not (BRAIN / "persone" / "Persona 2.md").exists()
    assert 'alias: ["Persona 2"]' in (BRAIN / "persone" / "Marco.md").read_text()


def test_merge_people(three):
    build_all()
    (BRAIN / "persone" / "Marco.md").write_text((BRAIN / "persone" / "Marco.md").read_text() + "\nnota di Marco\n")
    _people({"p_anna": {"name": "Anna"}, "p_marco": {"name": "Anna"}})   # Marco is Anna
    vault.rerender_people(["p_marco"])
    assert not (BRAIN / "persone" / "Marco.md").exists()
    page = (BRAIN / "persone" / "Anna.md").read_text()
    assert 'person_id: ["p_anna", "p_marco"]' in page and 'alias: ["Marco"]' in page
    assert "nota di Marco" in page
    assert vault.rerender_people(["p_marco"]) == [B]


def test_deleted_person_falls_back(three):
    build_all()
    _people({"p_marco": {"name": "Marco"}})                              # Anna deleted
    vault.rerender_people(["p_anna"])
    assert "Anna propone un viaggio" in three[A].with_suffix(".md").read_text()
    assert (BRAIN / "persone" / "Anna.md").is_file()


def test_incremental_equals_full(three):
    for s in (A, B, C):
        vault.render_note(s)
        vault.render_entities([s])
    vault.render_indexes()
    incremental = snapshot()
    assert {"persone/Anna.md", "organizzazioni/Acme.md", "progetti/Viaggio.md", "giorni/2026-11-22.md",
            "impegni.md", "da-rivedere.md"} <= set(incremental)
    # casefold: "viaggio"/"Viaggio" and "ACME"/"Acme" are one page, with both notes
    assert "Riunione" in incremental["progetti/Viaggio.md"] and "Pranzo" in incremental["progetti/Viaggio.md"]
    assert [f.name for f in (BRAIN / "organizzazioni").iterdir()] == ["Acme.md"]
    # change one note, update incrementally
    p = json.loads(three[B].read_text())
    p["progetti"] = None
    p["entita"]["progetti"] = []
    three[B].write_text(json.dumps(p))
    vault.render_note(B)
    vault.render_entities([B])
    vault.render_indexes()
    incremental = snapshot()
    assert "Riunione" not in incremental["progetti/Viaggio.md"]
    # full rebuild from scratch gives the same files
    for f in BRAIN.rglob("*"):
        if f.is_file() and f.suffix == ".md" and f.parent.name in ("persone", "luoghi", "organizzazioni", "progetti"):
            f.unlink()
    vault.INDEX.unlink()
    vault.render_entities([])
    assert snapshot() == incremental


def test_idempotent_no_churn(three):
    build_all()
    first = snapshot()
    files = [p for p in BRAIN.rglob("*") if p.is_file()] + [vault.INDEX]
    stamps = {p: os.stat(p).st_mtime_ns for p in files}
    build_all()
    vault.rerender_people(None)
    assert snapshot() == first
    assert {p: os.stat(p).st_mtime_ns for p in files} == stamps
    for p in files:
        assert oct(p.stat().st_mode & 0o777) == "0o600"
    for d in [BRAIN / "persone", BRAIN / "giorni", vault.CACHE]:
        assert oct(d.stat().st_mode & 0o777) == "0o700"


def test_checkbox_state_persists(three):
    build_all()
    sc = json.loads(three[A].read_text())
    assert sc["impegni_e_todo"][0]["stato"] == "aperto"
    imp = BRAIN / "impegni.md"
    t = imp.read_text()
    assert "## Entro 2026-11-27" in t and "- [ ] Anna → Io: Mandare le foto" in t
    imp.write_text(t.replace("- [ ] Anna", "- [x] Anna"))
    vault.render_indexes()
    assert json.loads(three[A].read_text())["impegni_e_todo"][0]["stato"] == "fatto"
    t = imp.read_text()
    assert "## Fatti" in t and "- [x] Anna → Io" in t and "## Entro 2026-11-27" not in t
    assert not vault.CONFLICTS.is_file() or not json.loads(vault.CONFLICTS.read_text())   # a tick is no edit
    # the note is rendered with the tick, and a tick in the note file counts too
    vault.render_note(A)
    assert "- [x] Anna → Io: Mandare le foto" in three[A].with_suffix(".md").read_text()
    sc = json.loads(three[A].read_text())
    sc["impegni_e_todo"][0]["stato"] = "aperto"      # e.g. the organizer ran again
    three[A].write_text(json.dumps(sc))
    vault.render_note(A)
    assert json.loads(three[A].read_text())["impegni_e_todo"][0]["stato"] == "fatto"
    vault.render_entities([A])
    assert "- ✅ Anna → Io: Mandare le foto" in (BRAIN / "persone" / "Anna.md").read_text()


def test_conflicts_preserved(three):
    build_all()
    note = three[A].with_suffix(".md")
    t = note.read_text()
    note.write_text(t.replace("Anna e io pranziamo.", "Riassunto scritto da me.") + "\nuna riga mia\n")
    p = json.loads(three[A].read_text())
    p["riassunto"] = "Un riassunto nuovo."
    p["punti_chiave"][0]["testo"] = "Anna propone una cena"
    three[A].write_text(json.dumps(p))
    vault.render_note(A)
    t = note.read_text()
    assert "Riassunto scritto da me." in t and "Un riassunto nuovo." not in t and "una riga mia" in t
    assert "Anna propone una cena" in t                              # the other blocks go on
    assert json.loads(vault.CONFLICTS.read_text()) == {f"note/2026/11/21/1814-pranzo-{A[-8:]}.md": ["riassunto"]}
    # giving the block back (delete the edit's block text so the hash matches again) clears the conflict
    note.write_text(t.replace("Riassunto scritto da me.", "Un riassunto nuovo."))
    vault.render_note(A)
    assert json.loads(vault.CONFLICTS.read_text()) == {}


def test_links(three):
    vault.update_links([A, B, C])
    by = {s: json.loads(p.read_text())["collegamenti"] for s, p in three.items()}
    # A and C share Anna and Acme; A and B share the project (casefold) and Marco is only in B
    assert by[A] == [{"nota": C, "motivo": "persona:p_anna"}, {"nota": B, "motivo": "progetto:Viaggio"}]
    assert by[B] == [{"nota": A, "motivo": "progetto:viaggio"}]
    assert by[C] == [{"nota": A, "motivo": "persona:p_anna"}]
    assert "me" not in json.dumps(by).replace("motivo", "")  # never linked through "me"
    t = three[A].with_suffix(".md").read_text()
    assert "## Collegamenti" in t and "persona Anna" in t and "[[note/2026/11/23/1100-caffe-cccc0003|Caffe con Anna]]" in t


def test_links_max_ten(archive):
    _people({"p_anna": {"name": "Anna"}})
    ids = []
    for i in range(1, 14):
        sid = f"2026112{i % 10}T0900{i:02d}Z-dddd{i:04d}"
        sidecar(sid, f"2026-11-{i:02d}", "0900", impegni=[])
        ids.append(sid)
    vault.update_links(ids)
    first = json.loads(next(p for p in NOTES.rglob("*.json") if ids[0][-8:] in p.name).read_text())
    assert len(first["collegamenti"]) == 10
    assert first["collegamenti"][0]["nota"] == ids[-1]                # most recent first


def test_da_rivedere_and_days(three):
    build_all()
    t = (BRAIN / "da-rivedere.md").read_text()
    assert "punti_chiave[1]: confidenza 0.20 < 0.40 — Dubbio sul budget" in t and "omy://session/" in t
    d = (BRAIN / "giorni" / "2026-11-21.md").read_text()
    assert "18:14–19:14 [[note/2026/11/21/1814-pranzo-aaaa0001|Pranzo con Anna]] (Casa)" in d
