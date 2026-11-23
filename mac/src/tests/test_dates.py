from datetime import date, datetime

import pytest

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
