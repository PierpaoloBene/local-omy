import json
import os
import stat

from brain import db, fixtures, ingest

A = "20261020T170000Z-aaaa0001"
C = "20261023T190000Z-cccc0003"


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_rows(path, items):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in items))


def bump_mtime(path):
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def fts(conn, word):
    return [r[0] for r in conn.execute(
        "SELECT p.id FROM paragraphs_fts f JOIN paragraphs p ON p.pk = f.rowid WHERE paragraphs_fts MATCH ?", (word,))]


def test_first_sync_reads_everything(conn):
    total = sum(len(v) for v in fixtures.paragraphs().values())
    assert conn.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0] == total
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == len(fixtures.SESSIONS)
    assert conn.execute("SELECT COUNT(*) FROM day_files").fetchone()[0] == 6


def test_channels_keep_media_apart(conn):
    channels = dict(conn.execute("SELECT session || '/' || speaker, channel FROM paragraphs").fetchall())
    assert {v for k, v in channels.items() if k.startswith(C)} == {"heard"}
    assert channels[f"{A}/Io"] == "me"
    assert channels[f"{A}/Rosa Linda"] == "said"


def test_unnamed_voices_are_not_people(conn):
    people = {r[0] for r in conn.execute("SELECT name FROM people")}
    assert people == {"Io", "Rosa Linda", "Marco"}
    assert "Persona 1" not in people and "Sconosciuto" not in people


def test_sync_twice_changes_nothing(conn, transcripts):
    report = ingest.sync(conn, transcripts)
    assert report.as_dict() == {"days_read": [], "added": 0, "updated": 0, "removed": 0,
                                "skipped_lines": 0, "sessions_touched": 0}


def test_rewritten_same_content_is_not_reread(conn, transcripts):
    day = transcripts / "2026-10-20.jsonl"
    day.write_text(day.read_text())
    bump_mtime(day)
    report = ingest.sync(conn, transcripts)
    assert report.days_read == [] and not report.sessions_touched


def test_changed_day_replaces_its_paragraphs(conn, transcripts):
    day = transcripts / "2026-10-20.jsonl"
    conn.execute("UPDATE sessions SET summary_status = 'done' WHERE id = ?", (A,))
    items = rows(day)
    removed = items.pop(1)                      # Rosa Linda about Gianni
    items[0]["text"] = "Per il bagno ho chiesto alla ditta Rossi."
    items.append({**items[-1], "id": f"{A}/999000", "text": "Ci sentiamo dopo cena."})
    write_rows(day, items)
    bump_mtime(day)

    report = ingest.sync(conn, transcripts)
    assert (report.added, report.updated, report.removed) == (1, 1, 1)
    assert report.sessions_touched == {A}
    assert conn.execute("SELECT 1 FROM paragraphs WHERE id = ?", (removed["id"],)).fetchone() is None
    assert conn.execute("SELECT summary_status FROM sessions WHERE id = ?", (A,)).fetchone()[0] == "stale"
    # The text indexes follow.
    assert fts(conn, "Gianni") == []
    assert fts(conn, "Rossi") == [items[0]["id"]]
    assert fts(conn, "cena") == [f"{A}/999000"]
    # And once more: nothing.
    assert ingest.sync(conn, transcripts).days_read == []


def test_unchanged_session_keeps_its_summary(conn, transcripts):
    conn.execute("UPDATE sessions SET summary_status = 'done'")
    day = transcripts / "2026-10-28.jsonl"
    items = rows(day)
    items[0]["text"] += " Magari anche Noto."
    write_rows(day, items)
    bump_mtime(day)
    ingest.sync(conn, transcripts)
    status = dict(conn.execute("SELECT id, summary_status FROM sessions").fetchall())
    assert status.pop("20261028T180000Z-eeee0005") == "stale"
    assert set(status.values()) == {"done"}


def test_empty_file_is_a_day_of_silence(conn, transcripts):
    (transcripts / "2026-10-20.jsonl").write_text("")
    bump_mtime(transcripts / "2026-10-20.jsonl")
    report = ingest.sync(conn, transcripts)
    assert report.removed == 4
    assert conn.execute("SELECT COUNT(*) FROM paragraphs WHERE day = '2026-10-20'").fetchone()[0] == 0
    assert conn.execute("SELECT 1 FROM sessions WHERE id = ?", (A,)).fetchone() is None


def test_deleted_file_removes_the_day(conn, transcripts):
    (transcripts / "2026-10-20.jsonl").unlink()
    ingest.sync(conn, transcripts)
    assert conn.execute("SELECT COUNT(*) FROM paragraphs WHERE day = '2026-10-20'").fetchone()[0] == 0
    assert conn.execute("SELECT 1 FROM day_files WHERE day = '2026-10-20'").fetchone() is None


def test_bad_lines_and_newer_format_are_skipped(conn, transcripts):
    day = transcripts / "2026-10-28.jsonl"
    items = rows(day)
    write_rows(day, items + [{**items[0], "id": f"{items[0]['session']}/5", "v": 2}])
    day.write_text(day.read_text() + "{not json\n")
    bump_mtime(day)
    report = ingest.sync(conn, transcripts)
    assert report.skipped_lines == 2
    assert report.added == 0


def test_index_is_private(tmp_path):
    os.umask(0o022)
    try:
        db.connect(tmp_path / "x" / "index.sqlite").close()
    finally:
        os.umask(0o077)
    assert stat.S_IMODE((tmp_path / "x" / "index.sqlite").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "x").stat().st_mode) == 0o700


def test_weights_favor_my_words(conn):
    w = dict(conn.execute("SELECT speaker, MAX(weight) FROM paragraphs GROUP BY speaker").fetchall())
    assert w["Io"] == 1.0 and w["Rosa Linda"] == 0.8 and w["Persona 1"] == 0.5 and w["Video"] == 0.5
