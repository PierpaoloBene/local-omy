"""Sessions: runs of consecutive segments with no recording gap.

The phone rotates segments every 5 minutes and on disconnect. Consecutive
segments of the same pendant, with the same codec, separated by less than
ROTATION_GAP are one continuous recording: a session. Its id is the id of its
first segment and it belongs to the local day it starts in.

Grouping uses only the .json sidecars (start/end), not the audio.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from omy.config import ARCHIVE

LOCAL_TZ = ZoneInfo("Europe/Rome")
# Below this, the gap between two segments is a rotation, not a disconnection.
ROTATION_GAP = timedelta(seconds=2)
# A session with no new segment for this long is considered finished.
CLOSE_AFTER = timedelta(minutes=10)


@dataclass(frozen=True)
class SegmentInfo:
    id: str
    bin: Path
    device_id: str
    codec: int
    start: datetime
    end: datetime


@dataclass
class Session:
    segments: list[SegmentInfo]

    @property
    def id(self) -> str:
        return self.segments[0].id

    @property
    def start(self) -> datetime:
        return self.segments[0].start

    @property
    def end(self) -> datetime:
        return self.segments[-1].end

    @property
    def day(self) -> date:
        return self.start.astimezone(LOCAL_TZ).date()

    def closed(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(timezone.utc)) - self.end >= CLOSE_AFTER


def _parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _load_dir(day_dir: Path) -> list[SegmentInfo]:
    found = []
    for meta_path in day_dir.glob("*.json"):
        bin_path = meta_path.with_suffix(".bin")
        if not bin_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        found.append(SegmentInfo(
            meta["id"], bin_path, meta["device_id"], meta["codec"],
            _parse_time(meta["start"]), _parse_time(meta["end"]),
        ))
    return found


def _group(segments: list[SegmentInfo]) -> list[Session]:
    sessions: list[Session] = []
    for seg in sorted(segments, key=lambda s: s.start):
        if sessions:
            prev = sessions[-1].segments[-1]
            if (seg.device_id == prev.device_id and seg.codec == prev.codec
                    and seg.start - prev.end < ROTATION_GAP):
                sessions[-1].segments.append(seg)
                continue
        sessions.append(Session([seg]))
    return sessions


def sessions_for_day(day: date, archive: Path = ARCHIVE) -> list[Session]:
    """Sessions starting on this local day, including their continuation into
    the next day. The archive folders are by local day of each segment."""
    audio = archive / "raw" / "audio"
    segments: list[SegmentInfo] = []
    for d in (day - timedelta(days=1), day, day + timedelta(days=1)):
        day_dir = audio / d.isoformat()
        if day_dir.is_dir():
            segments += _load_dir(day_dir)
    return [s for s in _group(segments) if s.day == day]


def recorded_days(archive: Path = ARCHIVE) -> list[date]:
    audio = archive / "raw" / "audio"
    days = []
    for d in audio.glob("????-??-??"):
        try:
            days.append(date.fromisoformat(d.name))
        except ValueError:
            continue
    return sorted(days)
