"""What the Mac was doing with audio: calls vs media.

The Mac app samples, every 10 s, which apps use the microphone ("i") and the
speakers ("o") into ~/OmiArchive/context/audio-YYYY-MM-DD.jsonl. Lines at
most JOIN apart make one interval per app:
  call   the app used the microphone (Teams, Zoom, FaceTime, Meet in a browser)
  media  the app only played audio (YouTube in a browser, Music, Spotify)
diarize labels the voices that aren't yours with them: a voice heard while a
call was on is a call participant, one heard while media played is media.

Calls on the iPhone (phone, FaceTime, WhatsApp: anything using CallKit) come
from the segment sidecars, where the phone copies the calls overlapping each
segment ("calls": [{"start", "end"?}]): app "iPhone".

A TV in the room, or anything on another computer, leaves no trace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from omy.config import ARCHIVE
from omy.sessions import LOCAL_TZ

CONTEXT = ARCHIVE / "context"
JOIN = timedelta(seconds=30)
# The app samples every 10 s: an interval covers half a period on each side.
PAD = timedelta(seconds=5)

# Bundle id prefix -> name shown in the transcript.
APPS = {
    "com.microsoft.teams": "Teams",
    "us.zoom": "Zoom",
    "com.apple.FaceTime": "FaceTime",
    "com.tinyspeck.slackmacgap": "Slack",
    "net.whatsapp": "WhatsApp",
    "com.google.Chrome": "Chrome",
    "com.apple.Safari": "Safari",
    "com.apple.WebKit": "Safari",
    "company.thebrowser": "Arc",
    "org.mozilla.firefox": "Firefox",
    "com.microsoft.edgemac": "Edge",
    "com.apple.Music": "Musica",
    "com.spotify": "Spotify",
    "com.apple.TV": "Apple TV",
    "com.apple.podcasts": "Podcast",
    "com.apple.QuickTimePlayerX": "QuickTime",
}
# Audio of the system and of this project: not media.
IGNORE = {"say", "com.apple.CoreSpeech", "com.apple.siri", "com.apple.SpeechRecognitionCore",
          "com.apple.WindowServer", "systemsoundserver", "com.apple.coreaudio"}


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime
    app: str      # display name
    kind: str     # "call" or "media"


def app_name(bundle: str) -> str:
    for prefix, name in APPS.items():
        if bundle.startswith(prefix):
            return name
    return bundle.rsplit(".", 1)[-1] if "." in bundle else bundle


def intervals(start: datetime, end: datetime) -> list[Interval]:
    """Calls and media overlapping [start, end)."""
    samples: list[tuple[datetime, dict[str, str]]] = []
    day = start.astimezone(LOCAL_TZ).date() - timedelta(days=1)
    while day <= end.astimezone(LOCAL_TZ).date():
        path = CONTEXT / f"audio-{day.isoformat()}.jsonl"
        if path.is_file():
            for line in path.read_text().splitlines():
                try:
                    row = json.loads(line)
                    t = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
                except (ValueError, KeyError):
                    continue
                if start - JOIN <= t <= end + JOIN:
                    samples.append((t, row.get("apps", {})))
        day += timedelta(days=1)
    samples.sort(key=lambda s: s[0])

    # Per app, the runs of samples at most JOIN apart; a run is a call if the
    # app used the microphone in any of its samples.
    runs: dict[str, list[list]] = {}
    for t, apps in samples:
        for bundle, flags in apps.items():
            if bundle in IGNORE:
                continue
            name = app_name(bundle)
            r = runs.setdefault(name, [])
            if r and t - r[-1][1] <= JOIN:
                r[-1][1] = t
                r[-1][2] |= "i" in flags
            else:
                r.append([t, t, "i" in flags])
    out = [Interval(a - PAD, b + PAD, name, "call" if mic else "media")
           for name, rs in runs.items() for a, b, mic in rs]
    out += phone_calls(start, end)
    return [i for i in out if i.end > start and i.start < end]


def phone_calls(start: datetime, end: datetime) -> list[Interval]:
    """iPhone calls from the sidecars of the segments in [start, end). A call
    without end (still on, or the app was killed) lasts until the last segment
    that lists it."""
    calls: dict[datetime, datetime] = {}
    day = start.astimezone(LOCAL_TZ).date()
    while day <= end.astimezone(LOCAL_TZ).date():
        for meta_path in (ARCHIVE / "raw" / "audio" / day.isoformat()).glob("*.json"):
            try:
                meta = json.loads(meta_path.read_text())
            except ValueError:
                continue
            for c in meta.get("calls", []):
                s = _t(c["start"])
                e = _t(c["end"]) if "end" in c else _t(meta["end"])
                calls[s] = max(calls.get(s, e), e)
        day += timedelta(days=1)
    return [Interval(s, e, "iPhone", "call") for s, e in sorted(calls.items())]


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def classify(turns: list[tuple[datetime, datetime]], context: list[Interval]) -> tuple[str, str] | None:
    """(kind, app) covering at least half of these turns' time, calls first:
    a call's audio plays on the speakers too."""
    total = sum((e - s).total_seconds() for s, e in turns)
    if not total or not context:
        return None
    for kind in ("call", "media"):
        cover: dict[str, float] = {}
        for s, e in turns:
            for i in context:
                if i.kind == kind:
                    ov = (min(e, i.end) - max(s, i.start)).total_seconds()
                    if ov > 0:
                        cover[i.app] = cover.get(i.app, 0) + ov
        if cover:
            app = max(cover, key=cover.get)
            if cover[app] >= total / 2:
                return kind, app
    return None


def days_with_context() -> list[date]:
    return sorted(date.fromisoformat(p.stem[6:]) for p in CONTEXT.glob("audio-????-??-??.jsonl"))
