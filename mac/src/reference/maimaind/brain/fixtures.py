"""Fake transcripts in the local-omy JSONL format, for the tests and brain eval.

Built to trip the model up: the podcast also says "ristrutturazione", an unnamed
"Persona 1" talks about banks too, and the last day is silence.
"""

from __future__ import annotations

import json
from pathlib import Path

ANNA = "a1b2c3d4e5f6"
MARCO = "0f1e2d3c4b5a"

PEOPLE = {
    "me": {"name": "Io", "minutes": 30.0, "sessions": 4, "last_seen": "2026-10-28"},
    "people": [
        {"id": ANNA, "name": "Rosa Linda", "minutes": 12.0, "sessions": 2, "last_seen": "2026-10-28"},
        {"id": MARCO, "name": "Marco", "minutes": 8.0, "sessions": 1, "last_seen": "2026-10-22"},
    ],
}

CASA = {"name": "Casa", "lat": 45.4642, "lon": 9.19, "acc": 20}
PALESTRA = {"name": "Palestra", "lat": 45.47, "lon": 9.2, "acc": 30}
TEAMS = {"kind": "call", "app": "Teams"}
YOUTUBE = {"kind": "media", "app": "YouTube"}

# session id -> (day, local start "HH:MM", utc offset, place, [(speaker, person, via, text), ...])
SESSIONS = {
    "20261020T170000Z-aaaa0001": ("2026-10-20", "19:00", "+02:00", CASA, [
        ("Io", "me", None, "Allora Rosa Linda, per la ristrutturazione del bagno ho sentito tre ditte."),
        ("Rosa Linda", ANNA, None, "Io preferirei quella di Gianni, ha rifatto il bagno di mia sorella e le "
                                  "piastrelle sono venute benissimo."),
        ("Io", "me", None, "Va bene, ti mando il preventivo domani mattina così lo guardiamo insieme."),
        ("Rosa Linda", ANNA, None, "Perfetto, e chiedi anche quanto tempo ci mettono a finire i lavori."),
    ]),
    "20261022T080000Z-bbbb0002": ("2026-10-22", "10:00", "+02:00", None, [
        ("Io", "me", None, "Marco, sul progetto Atlante siamo indietro di due settimane e il budget non torna."),
        ("Marco (Teams)", MARCO, TEAMS, "Lo so, ti giro il contratto del fornitore entro venerdì così vediamo "
                                        "le penali."),
        ("Io", "me", None, "La riunione di revisione del progetto la spostiamo a giovedì prossimo."),
    ]),
    "20261023T190000Z-cccc0003": ("2026-10-23", "21:00", "+02:00", CASA, [
        ("Video", None, YOUTUBE, "In questa puntata del podcast parliamo di intelligenza artificiale e di come "
                                 "la memoria a lungo termine cambia gli assistenti personali."),
        ("Video", None, YOUTUBE, "Secondo gli studi la ristrutturazione dei ricordi avviene soprattutto durante "
                                 "il sonno profondo."),
    ]),
    "20261027T090000Z-dddd0004": ("2026-10-27", "10:00", "+01:00", PALESTRA, [
        ("Io", "me", None, "Sinceramente non mi fido delle banche, preferisco tenere i risparmi diversificati."),
        ("Persona 1", None, None, "Le banche sono affidabili, io invece non mi fido delle criptovalute."),
        ("Io", "me", None, "In palestra vengo il martedì e il giovedì, di solito la mattina."),
    ]),
    "20261028T180000Z-eeee0005": ("2026-10-28", "19:00", "+01:00", CASA, [
        ("Rosa Linda", ANNA, None, "Per le vacanze pensavo alla Sicilia, a Siracusa, a giugno."),
        ("Io", "me", None, "Mi piace l'idea, prenoto io il volo entro fine mese."),
        ("Sconosciuto", None, None, "Scusate, è libero questo posto?"),
    ]),
}
SILENT_DAYS = ["2026-10-29"]
TODAY = "2026-10-29"


def paragraphs() -> dict[str, list[dict]]:
    """day -> JSONL rows, as local-omy writes them."""
    days: dict[str, list[dict]] = {d: [] for d in SILENT_DAYS}
    for session, (day, hhmm, offset, place, lines) in SESSIONS.items():
        h, m = map(int, hhmm.split(":"))
        for i, (speaker, person, via, text) in enumerate(lines):
            ms = i * 45_000
            start = f"{day}T{h:02d}:{m + i:02d}:00{offset}"
            end = f"{day}T{h:02d}:{m + i:02d}:40{offset}"
            days.setdefault(day, []).append({
                "v": 1, "id": f"{session}/{ms}", "session": session, "start": start, "end": end,
                "speaker": speaker, "person": person, "via": via, "place": place,
                "language": "it", "text": text,
            })
    return days


def write(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for day, rows in paragraphs().items():
        (directory / f"{day}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return directory
