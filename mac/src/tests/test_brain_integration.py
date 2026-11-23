"""organize -> vault end to end with the fake llama server: renames, ticks and re-organizations."""

import json
import os

import pytest
from fake_llama import FakeLlama
from helpers import record, write_day

from omy import diarize
from omy.brain import BRAIN, organize, vault
from omy.config import ARCHIVE

DAY = "2026-10-29"
S1 = "20261029T080000Z-0000abcd"
S2 = "20261029T150000Z-0000ef01"
COSA = "mandare il preventivo del giardino"


@pytest.fixture(autouse=True)
def sandbox(archive, monkeypatch):
    monkeypatch.setattr(organize, "_check_preempt", lambda: None)
    people("Rosalinda")
    rows = [
        record(S1, DAY, "08:00", "Io", "me", "Ciao Rosalinda, ti mando il preventivo del giardino venerdì."),
        record(S1, DAY, "08:01", "Rosalinda", "p1", "Perfetto, grazie mille, aspetto il tuo preventivo."),
        record(S2, DAY, "15:00", "Io", "me", "Oggi pomeriggio sistemo le fatture del trimestre con calma."),
        record(S2, DAY, "15:01", "Rosalinda", "p1", "Va bene, ci sentiamo domani per la riunione di lavoro."),
    ]
    write_day(ARCHIVE, DAY, rows)
    for sid in (S1, S2):
        speakers(sid)
    yield


def people(name):
    diarize.PEOPLE.parent.mkdir(parents=True, exist_ok=True)
    diarize.PEOPLE.write_text(json.dumps({"people": {"p1": {"name": name, "created": "x"}}}))


def speakers(sid, name="Rosalinda"):
    d = ARCHIVE / "transcripts" / DAY / "speakers"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.json").write_text(json.dumps({"session": sid, "turns": [], "clusters": {
        "SPEAKER_00": {"name": "Io", "person": "me", "seconds": 60.0, "context": None},
        "SPEAKER_01": {"name": name, "person": "p1", "seconds": 40.0, "context": None}}}))


def rename(new):
    """What people.rename -> relabel -> render_day does: the people file, the speakers documents, the JSONL."""
    people(new)
    for sid in (S1, S2):
        speakers(sid, new)
    f = ARCHIVE / "transcripts" / f"{DAY}.jsonl"
    rows = [json.loads(ln) for ln in f.read_text().splitlines()]
    for r in rows:
        if r["person"] == "p1":
            r["speaker"] = new
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))


def reply(cosa):
    def respond(body, n):
        user = body["messages"][-1]["content"]
        mine = "08:00" in user
        return {"titolo": "Preventivo" if mine else "Fatture", "qualita": "buona",
                "riassunto_parte": "Parlano con Rosalinda." if mine else "Fatture.", "punti_chiave": [],
                "entita": {"persone": [{"nome": "Rosalinda", "refs": ["P2"]}] if mine else [],
                           "luoghi": [], "organizzazioni": [], "progetti": []},
                "impegni_e_todo": [{"chi": "Io", "cosa": cosa, "verso_chi": "Rosalinda", "scadenza_testo": None,
                                    "refs": ["P1"]}] if mine else [],
                "date_ed_eventi_citati": [], "ascoltato": [], "tag": []}
    return respond


def build(cosa=COSA):
    with FakeLlama(reply(cosa)) as srv:
        for sid in (S1, S2):
            organize.organize_session(sid, srv.url)
    render_all()
    return srv


def render_all():
    sids = [S1, S2]
    for sid in sids:
        vault.render_note(sid)
    vault.update_links(sids)
    for sid in sids:
        vault.render_note(sid)
    vault.render_entities(sids)
    vault.render_indexes()


def md(sid=S1):
    return organize.note_path(sid).with_suffix(".md")


def sidecar(sid=S1):
    return json.loads(organize.note_path(sid).read_text())


def bump(path, ns):
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + ns))


def tick(on: bool, ns: int):
    imp = BRAIN / "impegni.md"
    a, b = ("- [ ] ", "- [x] ") if on else ("- [x] ", "- [ ] ")
    imp.write_text(imp.read_text().replace(a, b))
    bump(imp, ns)


def test_rename_does_not_reorganize_and_note_shows_new_name():
    srv = build()
    n = len(srv.calls)
    assert not organize.needs_organize(S1) and not organize.needs_organize(S2)
    assert "Rosalinda" in md().read_text()
    rename("Rosa Linda")
    assert not organize.needs_organize(S1) and not organize.needs_organize(S2)
    render_all()
    text = md().read_text()
    assert "Rosa Linda" in text and "Rosalinda" not in text
    assert len(srv.calls) == n


def test_ticks_stick_and_untick():
    build()
    iid = vault.impegno_ids(sidecar())[0]
    tick(True, 10**9)
    render_all()
    assert sidecar()["impegni_e_todo"][0]["stato"] == "fatto"
    render_all()
    assert sidecar()["impegni_e_todo"][0]["stato"] == "fatto"
    assert "- [x]" in md().read_text() and f"^{iid}" in md().read_text()
    tick(False, 3 * 10**9)
    render_all()
    assert sidecar()["impegni_e_todo"][0]["stato"] == "aperto"
    assert "- [x]" not in (BRAIN / "impegni.md").read_text()


def test_reorganize_with_reworded_cosa_keeps_stato_and_id():
    build()
    iid = vault.impegno_ids(sidecar())[0]
    tick(True, 10**9)
    render_all()
    links = sidecar()["collegamenti"]
    assert links
    with FakeLlama(reply("mandare il preventivo per il giardino")) as srv:
        organize.organize_session(S1, srv.url)
    it = sidecar()["impegni_e_todo"][0]
    assert it["cosa"] == "mandare il preventivo per il giardino"
    assert it["stato"] == "fatto" and vault.impegno_ids(sidecar())[0] == iid
    assert sidecar()["collegamenti"] == links
    render_all()
    assert "- [x]" in md().read_text() and f"^{iid}" in md().read_text()


def test_rehash_migrates_old_scheme():
    build()
    p = organize.note_path(S1)
    sc = json.loads(p.read_text())
    sc["elaborazione"]["input_hash"] = "old"
    p.write_text(json.dumps(sc))
    assert organize.needs_organize(S1)
    assert organize.rehash(S1) and not organize.needs_organize(S1)
    sc = json.loads(p.read_text())
    sc["paragrafi"]["P1"]["inizio"] = "2000-01-01T00:00:00+01:00"
    sc["elaborazione"]["input_hash"] = "old"
    p.write_text(json.dumps(sc))
    assert not organize.rehash(S1)
