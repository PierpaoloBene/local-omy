from datetime import date, datetime

import pytest

from omy.brain import dates
from omy.brain.dates import resolve

REF = datetime(2026, 11, 23, 10, 0)  # a Monday


@pytest.mark.parametrize("text,expected", [
    ("domani", date(2026, 11, 24)),
    ("dopodomani", date(2026, 11, 25)),
    ("venerdì", date(2026, 11, 27)),
    ("venerdì prossimo", date(2026, 11, 27)),
    ("lunedì", date(2026, 11, 30)),               # same weekday as today: next week
    ("martedì scorso", date(2026, 11, 17)),
    ("tra due settimane", date(2026, 12, 7)),
    ("due giorni fa", date(2026, 11, 21)),
    ("il 3 dicembre", date(2026, 12, 3)),
    ("il 3 ottobre", date(2027, 10, 3)),          # scadenze look forward
    ("entro fine mese", date(2026, 11, 30)),
    ("a natale", date(2026, 12, 25)),             # documented choice: the holiday's date
    ("domani sera", date(2026, 11, 24)),
    ("venerdì mattina", date(2026, 11, 27)),
    ("ieri", date(2026, 11, 22)),
    ("3 dicembre 2027", date(2027, 12, 3)),
    ("tra 3 giorni", date(2026, 11, 26)),
])
def test_resolve(text, expected):
    assert resolve(text, REF) == expected


@pytest.mark.parametrize("text", ["stasera", "d'inverno", "in estate", "in questo periodo", "il weekend",
                                  "il mese prossimo", "a Pasqua", "", "boh"])
def test_vague_is_none(text):
    assert resolve(text, REF) is None


def test_past_preference():
    assert resolve("il 3 ottobre", REF, prefer="past") == date(2026, 10, 3)
    assert resolve("venerdì", REF, prefer="past") == date(2026, 11, 20)


def test_reference_is_paragraph_start():
    assert resolve("domani", datetime(2026, 12, 31, 23, 30)) == date(2027, 1, 1)


@pytest.mark.parametrize("text", ["da agosto", "da quattro anni", "da due mesi", "tra 20 anni", "il 3 dicembre 2030"])
def test_durations_and_far_dates_are_none(text):
    assert resolve(text, REF) is None


def test_bare_month():
    assert resolve("settembre", REF, prefer="past") == date(2026, 9, 1)
    assert resolve("il mese di settembre", REF, prefer="past") == date(2026, 9, 1)
    assert resolve("agosto", REF, prefer="future") == date(2027, 8, 1)
    assert resolve("dicembre", REF, prefer="future") == date(2026, 12, 1)
    assert resolve("dicembre", REF, prefer="past") == date(2025, 12, 1)


# --- question_range: upcoming vs past, cross-year ---------------------------------------

@pytest.mark.parametrize("now,text,want", [
    ("2026-11-23", "il 3 dicembre", ("2026-12-03", "2026-12-03")),     # upcoming within 60 days
    ("2026-11-23", "a dicembre", ("2026-12-01", "2026-12-31")),
    ("2026-11-23", "a gennaio", ("2027-01-01", "2027-01-31")),         # starts in 39 days
    ("2026-11-23", "a marzo", ("2026-03-01", "2026-03-31")),           # far ahead: the most recent past one
    ("2026-11-23", "il 3 gennaio", ("2027-01-03", "2027-01-03")),      # 41 days ahead, next year
    ("2026-11-23", "il 3 marzo", ("2026-03-03", "2026-03-03")),        # far ahead: the most recent past one
    ("2026-11-23", "il 3 novembre", ("2026-11-03", "2026-11-03")),
    ("2026-11-23", "il 23 novembre", ("2026-11-23", "2026-11-23")),
    ("2026-01-05", "il 3 dicembre", ("2025-12-03", "2025-12-03")),
    ("2026-01-05", "a dicembre", ("2025-12-01", "2025-12-31")),
    ("2026-01-05", "a febbraio", ("2026-02-01", "2026-02-28")),
    ("2026-01-05", "dal 28 dicembre al 3 gennaio", ("2025-12-28", "2026-01-03")),
    ("2026-11-23", "dal 28 dicembre al 3 gennaio", ("2026-12-28", "2027-01-03")),
    ("2026-11-23", "dal 3 al 10 novembre", ("2026-11-03", "2026-11-10")),
])
def test_question_range_upcoming_and_cross_year(now, text, want):
    got = dates.question_range(text, datetime.fromisoformat(now + "T10:00:00"))
    assert got is not None and got[:2] == want
