"""Relative dates spelled out for the model: small models get "last week" wrong."""

from __future__ import annotations

from datetime import date, timedelta

WEEKDAYS = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
WEEKDAYS_SHORT = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def ranges(today: date) -> dict[str, tuple[date, date]]:
    monday = today - timedelta(days=today.weekday())
    first = today.replace(day=1)
    last_month_end = first - timedelta(days=1)
    return {
        "oggi": (today, today),
        "ieri": (today - timedelta(days=1), today - timedelta(days=1)),
        "questa settimana": (monday, today),
        "settimana scorsa": (monday - timedelta(days=7), monday - timedelta(days=1)),
        "questo mese": (first, today),
        "mese scorso": (last_month_end.replace(day=1), last_month_end),
        "quest'anno": (today.replace(month=1, day=1), today),
        "anno scorso": (date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)),
    }


def context(today: date) -> str:
    lines = [f"Oggi è {WEEKDAYS[today.weekday()]} {today.isoformat()}."]
    for name, (a, b) in ranges(today).items():
        if name != "oggi":
            lines.append(f"- {name}: {a.isoformat()}" + (f" → {b.isoformat()}" if a != b else ""))
    return "\n".join(lines)


def short(day: str) -> str:
    """2026-10-29 -> gio 29/10/2026"""
    d = date.fromisoformat(day)
    return f"{WEEKDAYS_SHORT[d.weekday()]} {d.day:02d}/{d.month:02d}/{d.year}"
