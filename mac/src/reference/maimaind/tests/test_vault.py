import json
import os
import stat

import pytest
from fakellm import FakeLLM, say

from brain import db, ingest, summarize, vault

A = "20261020T170000Z-aaaa0001"


def extraction(**over):
    base = {"title": "Bagno", "quality": "good", "summary": [{"text": "Hai sentito tre ditte", "refs": ["P1"]}],
            "topics": [{"name": "ristrutturazione", "refs": ["P1"]}],
            "commitments": [{"who": "Io", "to": "Rosa Linda", "what": "mandare il preventivo", "due": "2026-10-21",
                             "due_text": "domani", "refs": ["P3"]}],
            "facts": [{"about": "Rosa Linda", "fact": "ha una sorella", "refs": ["P2"]}], "heard": []}
    return json.dumps({**base, **over})


@pytest.fixture
def built(conn, tmp_path):
    summarize.summarize(conn, FakeLLM(say(extraction())), A, "fake")
    root = tmp_path / "SecondBrain"
    vault.render_all(conn, root)
    return root


def note(root):
    return root / "sessions/2026/10/20" / f"{A}.md"


def test_notes_are_written_private(built):
    assert note(built).is_file()
    for p in [built, *built.rglob("*")]:
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == (0o700 if p.is_dir() else 0o600), p


def test_private_even_with_a_wide_umask(conn, tmp_path):
    old = os.umask(0o022)
    try:
        vault.render_all(conn, tmp_path / "v")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "v/sessions/2026").stat().st_mode) == 0o700


def test_session_note_cites_paragraphs(built):
    text = note(built).read_text()
    assert "# Bagno" in text and "[[#^p0|19:00]]" in text
    assert "**19:02 · Io** Va bene, ti mando il preventivo" in text and "^p90000" in text
    assert '[[a1b2c3d4e5f6|Rosa Linda]]' in text
    assert (built / "people/a1b2c3d4e5f6.md").read_text().count("ha una sorella") == 1
    assert "[[20261020T170000Z-aaaa0001|Bagno]]" in (built / "days/2026-10-20.md").read_text()
    assert (built / "topics/ristrutturazione.md").is_file()


def test_your_text_outside_blocks_survives(conn, built):
    p = note(built)
    p.write_text(p.read_text().replace("<!-- brain:begin summary", "Nota mia: richiamare Gianni.\n\n"
                                       "<!-- brain:begin summary"))
    summarize.summarize(conn, FakeLLM(say(extraction(title="Bagno nuovo"))), A, "fake")
    r = vault.render_all(conn, built)
    text = p.read_text()
    assert "Nota mia: richiamare Gianni." in text and "# Bagno nuovo" in text
    assert r.conflicts == []


def test_an_edited_block_becomes_yours(conn, built):
    p = note(built)
    p.write_text(p.read_text().replace("Hai sentito tre ditte", "Ho sentito tre ditte, la migliore è Rossi"))
    summarize.summarize(conn, FakeLLM(say(extraction(summary=[{"text": "Altro", "refs": ["P1"]}]))), A, "fake")
    r = vault.render_all(conn, built)
    assert "la migliore è Rossi" in p.read_text() and "- Altro" not in p.read_text()
    assert (str(p), "summary", "edited") in r.conflicts
    assert ("sessions/2026/10/20/" + f"{A}.md", "summary", "edited") in vault.conflicts(built)
    # h=0 gives it back.
    p.write_text(__import__("re").sub(r"brain:begin summary h=[0-9a-f]+", "brain:begin summary h=0", p.read_text()))
    vault.render_all(conn, built)
    assert "- Altro" in p.read_text()


def test_a_deleted_block_stays_deleted(conn, built):
    p = note(built)
    text = p.read_text()
    start, end = text.index("<!-- brain:begin facts"), text.index("<!-- brain:end facts -->") + 24
    p.write_text(text[:start] + text[end:])
    r = vault.render_all(conn, built)
    assert "brain:begin facts" not in p.read_text()
    assert (str(p), "facts", "removed") in r.conflicts


def test_your_frontmatter_keys_survive(conn, built):
    p = note(built)
    p.write_text(p.read_text().replace("---\n", "---\nmio: importante\n", 1))
    vault.render_all(conn, built)
    fm, _ = vault.split_frontmatter(p.read_text())
    assert "mio: importante" in fm and 'type: "session"' in fm


def test_ticks_are_read_back(conn, built):
    p = built / "commitments.md"
    text = p.read_text()
    assert "- [ ] **[[me|Io]] → [[a1b2c3d4e5f6|Rosa Linda]]**: mandare il preventivo" in text
    p.write_text(text.replace("- [ ]", "- [x]", 1))
    r = vault.render_all(conn, built)
    assert r.conflicts == []  # a tick is not an edit
    text = p.read_text()
    mine = text[text.index("## Ho promesso io"):text.index("## Mi hanno promesso")]
    assert "_Niente di aperto._" in mine and "## Chiusi" in text and "- [x]" in text
    assert conn.execute("SELECT status FROM commitment_status").fetchone()[0] == "done"


def test_reindex_from_the_vault_without_the_model(conn, built, tmp_path, transcripts):
    (built / "commitments.md").write_text((built / "commitments.md").read_text().replace("- [ ]", "- [-]", 1))
    fresh = db.connect(tmp_path / "fresh.sqlite")
    ingest.sync(fresh, transcripts)
    counts = vault.import_vault(fresh, built)
    assert counts["summaries"] == 1 and counts["ticks"] == 1
    assert fresh.execute("SELECT title FROM summaries WHERE session = ?", (A,)).fetchone()[0] == "Bagno"
    assert fresh.execute("SELECT summary_status FROM sessions WHERE id = ?", (A,)).fetchone()[0] == "done"
    assert fresh.execute("SELECT status FROM commitment_status").fetchone()[0] == "dropped"


def test_reindex_marks_changed_sessions_stale(conn, built, tmp_path, transcripts):
    day = transcripts / "2026-10-20.jsonl"
    day.write_text(day.read_text().replace("tre ditte", "quattro ditte"))
    fresh = db.connect(tmp_path / "fresh.sqlite")
    ingest.sync(fresh, transcripts)
    assert vault.import_vault(fresh, built)["stale"] == 1
    assert fresh.execute("SELECT summary_status FROM sessions WHERE id = ?", (A,)).fetchone()[0] == "stale"


def test_orphans_are_marked_not_deleted(conn, built, transcripts):
    (transcripts / "2026-10-20.jsonl").write_text("")
    st = (transcripts / "2026-10-20.jsonl").stat()
    os.utime(transcripts / "2026-10-20.jsonl", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    ingest.sync(conn, transcripts)
    vault.render_all(conn, built)
    assert note(built).read_text().startswith('---\norphan: true')


def test_data_comment_survives_double_dashes():
    data = {"x": "a -- b --> c"}
    assert vault.read_data(vault.data_comment(data)) == data
    assert "-->" not in vault.data_comment(data)[:-3]
