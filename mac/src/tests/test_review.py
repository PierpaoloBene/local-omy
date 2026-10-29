"""Conversations a page at a time, whole days, newest first."""

from helpers import record, write_day


def _three_days(archive):
    for day, n in (("2026-10-27", 3), ("2026-10-28", 2), ("2026-10-29", 4)):
        write_day(archive, day, [
            r for i in range(n) for r in (
                record(f"{day.replace('-', '')}T{i:02d}0000Z-0000000{i}", day, f"1{i}:00", "Io", "me", "ciao"),
                record(f"{day.replace('-', '')}T{i:02d}0000Z-0000000{i}", day, f"1{i}:05", "Marco", "p1",
                       "come stai", place={"name": "Casa", "lat": 0, "lon": 0, "acc": 10}),
            )
        ])


def test_pages_end_on_whole_days(archive):
    from omy import review
    _three_days(archive)
    first = review.conversations(limit=3)
    assert [c["day"] for c in first["conversations"]] == ["2026-10-29"] * 4
    assert first["more"] and first["next"] == "2026-10-29"
    from datetime import date
    second = review.conversations(limit=3, before=date.fromisoformat(first["next"]))
    assert [c["day"] for c in second["conversations"]] == ["2026-10-28"] * 2 + ["2026-10-27"] * 3
    assert not second["more"] and second["next"] is None


def test_people_index(archive):
    from omy import review
    _three_days(archive)
    assert review.people_index() == {"p1": {"types": ["room"], "conversations": 9, "places": ["Casa"]}}


def test_today_is_the_archives(archive):
    from omy import review
    assert len(review.list_days()["today"]) == 10
