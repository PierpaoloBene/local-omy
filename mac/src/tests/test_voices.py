"""Naming voices: unknown is nobody, each on its own."""

import json

from helpers import SESSION, vec, write_speakers


def test_unknown_is_never_matched_and_numbered():
    from omy import diarize
    me = vec(1, 0, 0)
    result = {"turns": [(0, 1, "A"), (1, 2, "B"), (2, 3, "C")],
              "embeddings": {"A": vec(1, 0, 0), "B": vec(0, 1, 0), "C": vec(0, 0, 1)},
              "seconds": {"A": 1, "B": 1, "C": 1}}
    marco = {"p1": ("Marco", vec(0, 1, 0))}
    # A sounds exactly like you, B like Marco: marked unknown, they are neither.
    c = diarize.name_clusters(result, me, people=marco, unknown={"A", "B"})
    assert (c["A"]["person"], c["B"]["person"]) == (None, None)
    assert (c["A"]["name"], c["B"]["name"]) == ("Sconosciuto 1", "Sconosciuto 2")
    assert c["A"]["unknown"] and "unknown" not in c["C"]


def test_one_unknown_has_no_number():
    from omy import diarize
    result = {"turns": [(0, 1, "A"), (1, 2, "B")], "embeddings": {}, "seconds": {"A": 1, "B": 1}}
    c = diarize.name_clusters(result, None, unknown={"B"})
    assert (c["A"]["name"], c["B"]["name"]) == ("Persona 1", "Sconosciuto")


def test_media_wins_over_unknown():
    from omy import diarize
    result = {"turns": [(0, 1, "A")], "embeddings": {}, "seconds": {"A": 1}}
    c = diarize.name_clusters(result, None, sources={"A": "media"}, unknown={"A"})
    assert c["A"]["name"] == "Video" and "unknown" not in c["A"]


def test_mark_unknown_then_name(archive):
    from omy import people
    write_speakers(archive, "2026-10-29", SESSION, {"SPEAKER_00": vec(1, 0), "SPEAKER_01": vec(0, 1)})
    assert sum(people.todo(v) for v in people.list_day(_day())["sessions"][0]["voices"]) == 2

    people.set_unknown(SESSION, ["SPEAKER_01"])
    voices = {v["label"]: v for v in people.list_day(_day())["sessions"][0]["voices"]}
    assert voices["SPEAKER_01"]["unknown"] and voices["SPEAKER_01"]["name"] == "Sconosciuto"
    assert not people.todo(voices["SPEAKER_01"]) and people.todo(voices["SPEAKER_00"])
    # No person was made of it.
    assert people.list_people()["people"] == []

    # Found out who it was: a name, not unknown any more.
    people.rename(SESSION, "SPEAKER_01", "Marco")
    voices = {v["label"]: v for v in people.list_day(_day())["sessions"][0]["voices"]}
    assert voices["SPEAKER_01"]["name"] == "Marco" and not voices["SPEAKER_01"]["unknown"]


def test_unknown_off(archive):
    from omy import people
    path = write_speakers(archive, "2026-10-29", SESSION, {"SPEAKER_00": vec(1, 0)})
    people.set_unknown(SESSION, ["SPEAKER_00"])
    people.set_unknown(SESSION, ["SPEAKER_00"], unknown=False)
    assert "unknown" not in json.loads(path.read_text())["clusters"]["SPEAKER_00"]


def test_queue_of_the_last_hours(archive, monkeypatch):
    from datetime import date

    from omy import people, sessions
    monkeypatch.setattr(sessions, "recorded_days", lambda: [date(2026, 10, 28), date(2026, 10, 29)])
    write_speakers(archive, "2026-10-29", SESSION, {"SPEAKER_00": vec(1, 0)}, start="2026-10-29T08:00:00.000Z")
    old = "20261028T080000Z-0000beef"
    write_speakers(archive, "2026-10-28", old, {"SPEAKER_00": vec(1, 0)}, start="2026-10-28T08:00:00.000Z")
    assert {s["session"] for s in people.unlabeled()["sessions"]} == {SESSION, old}
    from datetime import datetime, timezone
    hours = int((datetime.now(timezone.utc) - datetime(2026, 10, 29, 7, tzinfo=timezone.utc)).total_seconds() // 3600)
    assert {s["session"] for s in people.unlabeled(hours=hours)["sessions"]} == {SESSION}


def _day():
    from datetime import date
    return date(2026, 10, 29)


def test_heard_again_from_a_video():
    from omy import diarize
    me = vec(1, 0, 0, 0)
    result = {"turns": [(0, 1, "A"), (1, 2, "B"), (2, 3, "C"), (3, 4, "D")],
              "embeddings": {"A": vec(1, 0, 0, 0), "B": vec(0, 1, 0, 0), "C": vec(0, 0, 1, 0.2), "D": vec(0, 0, 0, 1)},
              "seconds": {"A": 1, "B": 1, "C": 1, "D": 1}}
    media = [vec(1, 0, 0, 0), vec(0, 0, 1, 0)]
    c = diarize.name_clusters(result, me, media=media, sources={"D": "room"})
    # You are never a video; C sounds like a video marked elsewhere.
    assert c["A"]["person"] == "me" and "media" not in c["A"]
    assert c["C"]["name"] == "Video" and c["C"]["media"] > diarize.MEDIA_THRESHOLD
    assert c["B"]["name"] == "Persona 1" and "media" not in c["B"]
    # Set by hand wins: D is in the room whatever it sounds like.
    c = diarize.name_clusters(result, me, media=[vec(0, 0, 0, 1)], sources={"D": "room"})
    assert "media" not in c["D"]
    # Closer to a named person: that person.
    c = diarize.name_clusters(result, None, people={"p1": ("Marco", vec(0, 0, 1, 0.2))}, media=media)
    assert c["C"]["name"] == "Marco" and "media" not in c["C"]


def test_mark_media_finds_it_in_other_sessions(archive):
    from omy import people
    other = "20261029T120000Z-0000beef"
    write_speakers(archive, "2026-10-29", SESSION, {"SPEAKER_00": vec(1, 0, 0), "SPEAKER_01": vec(0, 1, 0)})
    write_speakers(archive, "2026-10-29", other, {"SPEAKER_00": vec(0.1, 1, 0), "SPEAKER_01": vec(0, 0, 1)},
                   start="2026-10-29T12:00:00.000Z")
    people.set_source(SESSION, ["SPEAKER_01"], "media")
    voices = {(s["session"], v["label"]): v for s in people.list_day(_day())["sessions"] for v in s["voices"]}
    seen = voices[(other, "SPEAKER_00")]
    assert seen["kind"] == "media" and seen["name"] == "Video" and not people.todo(seen)
    assert people.todo(voices[(other, "SPEAKER_01")])
    # Back to auto: nothing to hear it from any more.
    people.set_source(SESSION, ["SPEAKER_01"], "auto")
    voices = {(s["session"], v["label"]): v for s in people.list_day(_day())["sessions"] for v in s["voices"]}
    assert people.todo(voices[(other, "SPEAKER_00")])
