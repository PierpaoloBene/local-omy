"""Current names for the people of a note.

The sidecar keeps the names the organizer saw (`nome_al_momento`, the words of
the generated text). The vault shows the names of today: a person named or
renamed after the note was written is rewritten here, at render time only.
"""

from __future__ import annotations

import json
import re
from functools import cached_property

from omy.config import ARCHIVE
from omy.diarize import load_people, my_name


def current_name(person: str | None, fallback: str | None, people: dict | None = None,
                 me: str | None = None) -> str | None:
    """The name of person ("me", a person id) today; `fallback` when the id is
    gone (deleted, merged away) or there is no person."""
    if person == "me":
        return me or my_name()
    if person:
        people = load_people() if people is None else people
        p = people.get(person)
        for _ in range(5):  # a merged person may point at the one that absorbed it
            if p and not p.get("name") and p.get("merged_into"):
                p = people.get(p["merged_into"])
            else:
                break
        if p and p.get("name"):
            return p["name"]
    return fallback


def _speaker_clusters(sidecar: dict) -> dict:
    """label -> cluster of the session's speakers document (where a voice is
    pinned to a person after the note was written), {} if there is none."""
    day, sid = sidecar.get("data"), sidecar.get("id")
    if not day or not sid:
        return {}
    try:
        doc = json.loads((ARCHIVE / "transcripts" / day / "speakers" / f"{sid}.json").read_text())
        clusters = doc.get("clusters", {})
        return clusters if isinstance(clusters, dict) else {}
    except (OSError, ValueError):
        return {}


class Names:
    def __init__(self, sidecar: dict, people: dict | None = None, me: str | None = None):
        self.sc = sidecar
        self.people = load_people() if people is None else people
        self.me = me or my_name()
        clusters = _speaker_clusters(sidecar)
        self.participants: list[dict] = []
        for p in sidecar.get("partecipanti") or []:
            cl = clusters.get(p.get("label")) or {}
            person = cl.get("person") or p.get("person")  # the pin of today beats the one of then
            old = p.get("nome_al_momento")
            new = current_name(person, old, self.people, self.me)
            self.participants.append({"label": p.get("label"), "person": person, "old": old, "new": new})
        self.map: dict[str, str] = {}
        for p in self.participants:
            if p["old"] and p["new"] and p["old"] != p["new"]:
                self.map[p["old"]] = p["new"]
        for it in sidecar.get("impegni_e_todo") or []:
            for who, pid in ((it.get("chi"), it.get("chi_id")), (it.get("verso_chi"), it.get("verso_chi_id"))):
                new = current_name(pid, who, self.people, self.me) if pid and pid != "me" else who
                if who and new and who != new:
                    self.map.setdefault(who, new)

    @cached_property
    def _pattern(self):
        keys = sorted(self.map, key=lambda k: (-len(k), k))  # longer names first
        return re.compile("|".join(rf"(?<!\w){re.escape(k)}(?!\w)" for k in keys)) if keys else None

    def rewrite(self, text: str | None) -> str | None:
        """text with every participant's name of then replaced by the current one."""
        if not text or not self._pattern:
            return text
        return self._pattern.sub(lambda m: self.map[m.group(0)], text)

    def name(self, person: str | None, fallback: str | None) -> str | None:
        return current_name(person, self.rewrite(fallback), self.people, self.me)

    def chi(self, item: dict) -> str | None:
        return self.name(item.get("chi_id"), item.get("chi"))

    def verso_chi(self, item: dict) -> str | None:
        return self.name(item.get("verso_chi_id"), item.get("verso_chi"))

    def person_ids(self) -> set[str]:
        """Every person id the note involves (participants as pinned today, entities, commitments)."""
        ids = {p["person"] for p in self.participants if p["person"]}
        ids |= {p.get("person_id") for p in (self.sc.get("entita") or {}).get("persone") or []}
        for it in self.sc.get("impegni_e_todo") or []:
            ids |= {it.get("chi_id"), it.get("verso_chi_id")}
        return {i for i in ids if i}


def rewrite(text: str | None, sidecar: dict) -> str | None:
    return Names(sidecar).rewrite(text)
