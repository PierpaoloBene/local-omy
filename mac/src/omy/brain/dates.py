"""resolve(): turns a date expression as said ("venerdì prossimo") into a date.

The model never resolves dates; this code does. dateparser's Italian is thin
("dopodomani", "tra due settimane", "il 3 dicembre" all fail), so the common
Italian forms are handled by the rules below and dateparser only gets what is
left (RELATIVE_BASE = start time of the cited paragraph).

Choices:
- a weekday ("venerdì", "venerdì prossimo") is its next occurrence strictly after
  the reference day; "scorso/passato" is the latest one strictly before it;
- "a natale" (and Capodanno, Epifania, Ferragosto, ...) is that holiday's date, the
  next one (the previous one when the text is in the past); Pasqua is null (moves);
- "entro fine mese" is the last day of the reference month;
- vague expressions (stasera, estate, periodo, weekend, "mese prossimo", ...) are null.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date, datetime, timedelta

WEEKDAYS = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
_DAYS = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"]
MONTHS = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre",
          "ottobre", "novembre", "dicembre"]
NUMBERS = {"un": 1, "uno": 1, "una": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6, "sette": 7,
           "otto": 8, "nove": 9, "dieci": 10, "undici": 11, "dodici": 12, "quindici": 15, "venti": 20,
           "trenta": 30}
HOLIDAYS = {"natale": (12, 25), "capodanno": (1, 1), "epifania": (1, 6), "ferragosto": (8, 15),
            "santo stefano": (12, 26), "immacolata": (12, 8), "halloween": (10, 31)}
VAGUE = re.compile(
    r"\b(stasera|stanotte|stamattina|stamani|stasera|sera|mattina|pomeriggio|notte|estate|inverno|primavera|autunno|"
    r"periodo|prossimamente|presto|tardi|weekend|week end|fine settimana|un giorno|qualche|poco|prossimi giorni|"
    r"giorni scorsi|tempo|(?:mese|anno)\s+(?:prossimo|scorso|passato)|(?:prossimo|scorso|passato)\s+(?:mese|anno))\b")
PAST_HINT = re.compile(r"\b(fa|scors[oaie]|passat[oaie]|ieri|altro ieri|l altro ieri)\b")
_FILLER = re.compile(r"^(?:entro|per|di|da|a|il|lo|la|l|del|dal|nel|sul|verso|circa)\b\s*")


def fold(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c)).replace("'", " ").replace("’", " ").strip()


def _num(word: str) -> int | None:
    return int(word) if word.isdigit() else NUMBERS.get(word)


def _norm(text: str) -> str:
    t = " ".join(fold(text).replace(",", " ").split())
    while (m := _FILLER.match(t)) and m.end() < len(t):
        t = t[m.end():]
    return t


MAX_DAYS = 396  # ~13 months: further from the reference than this is not a plausible date
_BARE_MONTH = re.compile(r"^(?:(?:mese di|in|a|di)\s+)?(" + "|".join(MONTHS) + r")$")


def is_duration(text: str) -> bool:
    """"da agosto", "da quattro anni", "per mesi": a duration or a "since", not a date."""
    t = " ".join(fold(text).split())
    if t.startswith("da ") or t == "da":
        return True
    return bool(re.search(r"\b(anni|mesi)\b", t)) and not re.search(r"\b(fa|tra|fra|entro)\b", t)


def bare_month(text: str) -> int | None:
    """1-12 when text is just a month name ("agosto", "il mese di settembre"), else None."""
    t = " ".join(fold(text).split())
    t = re.sub(r"^(?:il|lo|la|l|del|nel|di|a|in|entro|per)\s+", "", t)
    m = _BARE_MONTH.match(t)
    return MONTHS.index(m[1]) + 1 if m else None


def _month_date(month: int, today: date, future: bool) -> date:
    if future:
        return date(today.year if month > today.month else today.year + 1, month, 1)
    return date(today.year if month < today.month else today.year - 1, month, 1)


def is_past(text: str) -> bool:
    return bool(PAST_HINT.search(fold(text)))


def _next_year_date(month: int, day: int, today: date, future: bool, year: int | None = None) -> date | None:
    try:
        if year is not None:
            return date(year, month, day)
        d = date(today.year, month, day)
        if future and d <= today and d != today:
            d = date(today.year + 1, month, day)
        elif not future and d > today:
            d = date(today.year - 1, month, day)
        return d
    except ValueError:  # 30 February, 29 February of a common year
        return None


def _add_months(today: date, n: int) -> date:
    m = today.month - 1 + n
    y, m = today.year + m // 12, m % 12 + 1
    return date(y, m, min(today.day, calendar.monthrange(y, m)[1]))


def rules(t: str, today: date, future: bool) -> date | None:
    """t is already folded and normalised."""
    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    if m := re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t):
        y = int(m[3]) if m[3] else None
        if y is not None and y < 100:
            y += 2000
        return _next_year_date(int(m[2]), int(m[1]), today, future, y)
    for month, name in enumerate(MONTHS, 1):
        if m := re.search(rf"\b(\d{{1,2}})(?:\s*(?:o|°))?\s+{name}\b(?:\s+(\d{{4}}))?", t):
            return _next_year_date(month, int(m[1]), today, future, int(m[2]) if m[2] else None)
    for name, (month, day) in HOLIDAYS.items():
        if re.search(rf"\b{name}\b", t):
            return _next_year_date(month, day, today, future)
    if re.search(r"\bfine (?:del )?mese\b", t):
        return date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    if re.search(r"\bl altro ieri\b|\baltro ieri\b", t):
        return today - timedelta(days=2)
    if re.search(r"\bdopodomani\b|\bdopo domani\b", t):
        return today + timedelta(days=2)
    if re.search(r"\bdomani\b", t):
        return today + timedelta(days=1)
    if re.search(r"\bieri\b", t):
        return today - timedelta(days=1)
    if re.search(r"\boggi\b", t):
        return today
    if m := re.search(r"\b(?:tra|fra|entro)\s+(\w+)\s+(giorn[io]|settiman[ae]|mes[ie]|ann[io])\b", t):
        n = _num(m[1])
        if n is not None:
            u = m[2]
            if u.startswith("giorn"):
                return today + timedelta(days=n)
            if u.startswith("settim"):
                return today + timedelta(days=7 * n)
            if u.startswith("mes"):
                return _add_months(today, n)
            return _add_months(today, 12 * n)
    if m := re.search(r"\b(\w+)\s+(giorn[io]|settiman[ae]|mes[ie]|ann[io])\s+fa\b", t):
        n = _num(m[1])
        if n is not None:
            u = m[2]
            if u.startswith("giorn"):
                return today - timedelta(days=n)
            if u.startswith("settim"):
                return today - timedelta(days=7 * n)
            if u.startswith("mes"):
                return _add_months(today, -n)
            return _add_months(today, -12 * n)
    if re.search(r"\bsettimana\s+prossima\b|\bprossima\s+settimana\b", t):
        return today - timedelta(days=today.weekday()) + timedelta(days=7)
    if re.search(r"\bsettimana\s+scorsa\b|\bscorsa\s+settimana\b", t):
        return today - timedelta(days=today.weekday()) - timedelta(days=7)
    for i, name in enumerate(_DAYS):
        if re.search(rf"\b{name}\b", t):
            if not future:
                return today - timedelta(days=(today.weekday() - i) % 7 or 7)
            return today + timedelta(days=(i - today.weekday()) % 7 or 7)
    return None


def resolve(text: str, reference: datetime, prefer: str | None = None) -> date | None:
    """text as said -> date, or None when vague/unparseable.

    reference: start time of the cited paragraph. prefer: "future" (scadenze) or
    "past"; None = "past" if the text says fa/scorso/ieri/..., else "future"."""
    if not isinstance(text, str) or not text.strip():
        return None
    if is_duration(text):
        return None
    t = _norm(text)
    if not t:
        return None
    if prefer is None:
        prefer = "past" if is_past(text) else "future"
    today = reference.date()
    if (mo := bare_month(text)) is not None:
        got = _month_date(mo, today, prefer == "future")
    else:
        got = rules(t, today, prefer == "future")
    if got is not None:
        return got if abs((got - today).days) <= MAX_DAYS else None
    if VAGUE.search(t) or re.search(r"\b(pasqua|pentecoste)\b", t):
        return None
    try:
        import dateparser
    except ImportError:
        return None
    parsed = dateparser.parse(t, languages=["it"], settings={
        "RELATIVE_BASE": reference.replace(tzinfo=None), "PREFER_DATES_FROM": prefer})
    if not parsed or abs((parsed.date() - today).days) > MAX_DAYS:
        return None
    return parsed.date()


# --- ranges in a QUESTION ("cosa ho detto ieri?", "la settimana scorsa", "dal 3 al 10 novembre") ---

def _month_range(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


UPCOMING_DAYS = 60  # a day/month this close ahead is a question about something upcoming


def _recent_date(month: int, day: int, today: date, year: int | None = None) -> date | None:
    """The day/month to use for a question: the upcoming occurrence when it is within UPCOMING_DAYS
    (today included), else the most recent one not after today (or the date in the given year)."""
    try:
        if year is not None:
            return date(year, month, day)
        cands = []
        for y in (today.year, today.year + 1):
            try:
                cands.append(date(y, month, day))
            except ValueError:
                pass
        up = next((c for c in cands if c >= today), None)
        if up and (up - today).days <= UPCOMING_DAYS:
            return up
        past = [c for c in [date(today.year - 1, month, day) if _valid(today.year - 1, month, day) else None] + cands
                if c and c <= today]
        return max(past) if past else None
    except ValueError:
        return None


def _valid(y: int, m: int, d: int) -> bool:
    try:
        date(y, m, d)
        return True
    except ValueError:
        return False


def _month_year(mo: int, today: date) -> int:
    """Year of a bare month in a question: the upcoming one if it starts within UPCOMING_DAYS, else the
    most recent (the current month is the current year)."""
    if mo == today.month:
        return today.year
    ahead = (mo - today.month) % 12
    first = date(today.year + (1 if mo < today.month else 0), mo, 1)
    if (first - today).days <= UPCOMING_DAYS and ahead:
        return first.year
    return today.year if mo < today.month else today.year - 1


def question_range(text: str, now: datetime) -> tuple[str, str, str] | None:
    """A day range said in a question -> (day_from, day_to, matched_text) as YYYY-MM-DD, or None.

    Choices: "questa settimana" = Monday..today, "questo mese" = 1st..today, "quest'anno" = Jan 1..today,
    "negli ultimi N giorni" = the N days ending today; a weekday is its most recent occurrence, today
    included ("lunedì scorso" never today); "a ottobre" is the whole of the most recent October (the
    current month if it is that one); "il 3 novembre" is its most recent occurrence (today included).
    Exception: a day/month, or a month, starting within UPCOMING_DAYS ahead is its upcoming occurrence.
    matched_text is the matched part of the accent-folded question."""
    t = " ".join(fold(text).replace(",", " ").replace("?", " ").split())
    today = now.date()
    month_re = "|".join(MONTHS)

    def out(a: date, b: date, m: re.Match) -> tuple[str, str, str]:
        return a.isoformat(), b.isoformat(), m.group(0)

    if m := re.search(rf"\bdal (\d{{1,2}})(?: ({month_re}))? al (\d{{1,2}}) ({month_re})(?: (\d{{4}}))?\b", t):
        m2 = MONTHS.index(m[4]) + 1
        m1 = MONTHS.index(m[2]) + 1 if m[2] else m2
        end = _recent_date(m2, int(m[3]), today, int(m[5]) if m[5] else None)
        if end:
            start = None
            for y in (end.year, end.year - 1):  # "dal 28 dicembre al 3 gennaio": the start is the year before
                if _valid(y, m1, int(m[1])) and date(y, m1, int(m[1])) <= end:
                    start = date(y, m1, int(m[1]))
                    break
            if start:
                return out(start, end, m)
    if m := re.search(r"\bnegli ultimi (\w+) giorni\b", t):
        n = _num(m[1])
        if n:
            return out(today - timedelta(days=n - 1), today, m)
    if m := re.search(rf"\b(\d{{1,2}}) ({month_re})(?: (\d{{4}}))?\b", t):
        d = _recent_date(MONTHS.index(m[2]) + 1, int(m[1]), today, int(m[3]) if m[3] else None)
        if d:
            return out(d, d, m)
    if m := re.search(r"\b(?:l )?altro ieri\b", t):
        return out(today - timedelta(days=2), today - timedelta(days=2), m)
    if m := re.search(r"\bieri\b", t):
        return out(today - timedelta(days=1), today - timedelta(days=1), m)
    if m := re.search(r"\b(?:oggi|stamattina|stamani|stasera|stanotte)\b", t):
        return out(today, today, m)
    monday = today - timedelta(days=today.weekday())
    if m := re.search(r"\bsettimana scorsa\b|\bscorsa settimana\b", t):
        return out(monday - timedelta(days=7), monday - timedelta(days=1), m)
    if m := re.search(r"\bquesta settimana\b", t):
        return out(monday, today, m)
    if m := re.search(r"\bmese scorso\b|\bscorso mese\b", t):
        y, mo = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        return out(*_month_range(y, mo), m)
    if m := re.search(r"\bquesto mese\b", t):
        return out(today.replace(day=1), today, m)
    if m := re.search(r"\bquest anno\b|\bquesto anno\b", t):
        return out(date(today.year, 1, 1), today, m)
    if m := re.search(rf"\b({'|'.join(_DAYS)})( scors[oa])?\b", t):
        back = (today.weekday() - _DAYS.index(m[1])) % 7
        if m[2] and back == 0:
            back = 7
        d = today - timedelta(days=back)
        return out(d, d, m)
    if m := re.search(rf"\b(?:a|ad|in|di|nel)\s+({month_re})(?: (\d{{4}}))?\b", t):
        mo = MONTHS.index(m[1]) + 1
        y = int(m[2]) if m[2] else _month_year(mo, today)
        return out(*_month_range(y, mo), m)
    return None
