"""resolve(): turns a date expression as said ("venerdì prossimo") into a date.

The model never resolves dates; this code does. Uses dateparser when installed,
otherwise a small Italian fallback.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta

WEEKDAYS = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"]
NUMBERS = {"un": 1, "uno": 1, "una": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6, "sette": 7,
           "otto": 8, "nove": 9, "dieci": 10, "quindici": 15, "venti": 20, "trenta": 30}


def fold(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c)).replace("'", " ").strip()


def _num(word: str) -> int | None:
    return int(word) if word.isdigit() else NUMBERS.get(word)


def fallback(text: str, reference: datetime) -> date | None:
    t = " ".join(fold(text).split())
    today = reference.date()
    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    if m := re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t):
        y = int(m[3]) if m[3] else today.year
        y += 2000 if y < 100 else 0
        try:
            return date(y, int(m[2]), int(m[1]))
        except ValueError:
            return None
    if re.search(r"\bdopodomani\b", t):
        return today + timedelta(days=2)
    if re.search(r"\bdomani\b", t):
        return today + timedelta(days=1)
    if re.search(r"\boggi\b|\bstasera\b|\bstamattina\b|\bstanotte\b|\badesso\b", t):
        return today
    if re.search(r"\bieri\b", t):
        return today - timedelta(days=1)
    if m := re.search(r"\b(?:tra|fra)\s+(\w+)\s+(giorn[io]|settiman[ae]|mes[ie])\b", t):
        n = _num(m[1])
        if n is not None:
            unit = m[2]
            days = n if unit.startswith("giorn") else 7 * n if unit.startswith("settim") else 30 * n
            return today + timedelta(days=days)
    if m := re.search(r"\b(\w+)\s+(giorn[io]|settiman[ae])\s+fa\b", t):
        n = _num(m[1])
        if n is not None:
            return today - timedelta(days=n if m[2].startswith("giorn") else 7 * n)
    if re.search(r"\bsettimana\s+prossima\b|\bprossima\s+settimana\b", t):
        return today - timedelta(days=today.weekday()) + timedelta(days=7)
    if re.search(r"\bsettimana\s+scorsa\b|\bscorsa\s+settimana\b", t):
        return today - timedelta(days=today.weekday()) - timedelta(days=7)
    for i, name in enumerate(WEEKDAYS):
        if re.search(rf"\b{name}\b", t):
            if re.search(r"\bscors[oa]\b|\bpassat[oa]\b", t):
                return today - timedelta(days=(today.weekday() - i) % 7 or 7)
            return today + timedelta(days=(i - today.weekday()) % 7 or 7)
    return None


def resolve(text_date: str, reference: datetime) -> date | None:
    if not isinstance(text_date, str) or not text_date.strip():
        return None
    try:
        import dateparser
    except ImportError:
        return fallback(text_date, reference)
    got = dateparser.parse(text_date, languages=["it"], settings={
        "RELATIVE_BASE": reference.replace(tzinfo=None), "PREFER_DATES_FROM": "future"})
    return got.date() if got else fallback(text_date, reference)
