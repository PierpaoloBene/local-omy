"""local-omy receiver: stores segments uploaded by the phone.

Listens on 127.0.0.1 only; exposed to the tailnet with `tailscale serve`.

Environment:
  OMY_TOKEN_FILE  token file, must be owned by you and mode 600
                  (default ~/.config/local-omy/token)
  OMY_TOKEN       token value (alternative to the file)
  OMY_ARCHIVE     archive root (default ~/OmiArchive)
  OMY_PORT        port on 127.0.0.1 (default 8787)
  OMY_MAX_BODY    max upload size in bytes (default 32 MiB)

After every accepted upload, <archive>/receiver.json records when it arrived
and how many segments the phone still had queued (X-Queue-Remaining, sent by
the phone; absent from older builds).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import math
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect

from . import features
from .segment import SegmentError, parse_header

SEGMENT_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
SHA256 = re.compile(r"[0-9a-f]{64}")
ISO_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
MAX_META_HEADER = 4096
MIN_TOKEN_LEN = 32
LOCAL_TZ = ZoneInfo("Europe/Rome")
MAX_QUEUE = 1_000_000

# The iPhone app reviews transcripts, people and places through the omy
# commands the Mac app runs (python -m omy.cli --json …).
OMY_TIMEOUT = 600
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
VOICE_LABEL = re.compile(r"SPEAKER_[0-9]{2,3}")
VOICE_SOURCES = {"room", "meeting", "media", "auto"}
PERSON_ID = re.compile(r"[0-9a-f]{12}")
MAX_NAME = 80
MAX_QUERY = 100
MAX_JSON_BODY = 4096

log = logging.getLogger("uvicorn.error")

META_FIELDS: dict[str, type | tuple[type, ...]] = {
    "id": str,
    "device_id": str,
    "codec": int,
    "start": str,
    "end": str,
    "frame_count": int,
    "first_packet_index": (int, type(None)),
    "last_packet_index": (int, type(None)),
    "sha256": str,
}
# Optional: calls the phone saw connected during the segment, [{"start", "end"?}].
MAX_CALLS = 20
# Optional: where the phone was, {"lat", "lon", "acc" (m), "source", "at"};
# the pendant battery level, 0-100; its firmware revision, e.g. "3.0.21".
LOCATION_SOURCES = {"visit", "move"}
FIRMWARE = re.compile(r"[0-9A-Za-z._+-]{1,32}")
OPTIONAL_FIELDS = {"calls", "location", "battery", "firmware"}


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _check_location(loc) -> None:
    if (not isinstance(loc, dict) or set(loc) != {"lat", "lon", "acc", "source", "at"}
            or not all(_number(loc[k]) for k in ("lat", "lon", "acc"))
            or not -90 <= loc["lat"] <= 90 or not -180 <= loc["lon"] <= 180 or not 0 <= loc["acc"] <= 100_000
            or loc["source"] not in LOCATION_SOURCES
            or not isinstance(loc["at"], str) or not ISO_UTC.fullmatch(loc["at"])):
        raise ValueError("meta field location has wrong type")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    token: bytes
    archive: Path
    max_body: int = 32 * 1024 * 1024


def load_token() -> bytes:
    value = os.environ.get("OMY_TOKEN")
    if value is None:
        path = Path(os.environ.get("OMY_TOKEN_FILE", "~/.config/local-omy/token")).expanduser()
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as e:
            raise ConfigError(f"cannot open token file {path}: {e}") from e
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise ConfigError(f"{path} is not a regular file")
            if st.st_uid != os.getuid():
                raise ConfigError(f"{path} is not owned by the current user")
            if st.st_mode & 0o077:
                raise ConfigError(f"{path} must be mode 600 (is {oct(st.st_mode & 0o777)})")
            value = f.read().decode("utf-8").strip()
    if len(value) < MIN_TOKEN_LEN:
        raise ConfigError(f"token must be at least {MIN_TOKEN_LEN} characters")
    return value.encode("utf-8")


def load_config() -> Config:
    return Config(
        token=load_token(),
        archive=Path(os.environ.get("OMY_ARCHIVE", "~/OmiArchive")).expanduser(),
        max_body=int(os.environ.get("OMY_MAX_BODY", 32 * 1024 * 1024)),
    )


def _error(status: int, detail: str) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse({"error": detail}, status_code=status, headers=headers)


def _name(v) -> str:
    """A person or place name from the phone; never read as an option by omy."""
    if (not isinstance(v, str) or not v.strip() or len(v) > MAX_NAME or v.lstrip().startswith("-")
            or any(ord(c) < 32 for c in v)):
        raise ValueError("invalid name")
    return v.strip()


def _authorized(request: Request, token: bytes) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    # Hash both sides so the comparison is constant-time and length-independent,
    # and always run it so timing doesn't reveal which check failed.
    ok = hmac.compare_digest(
        hashlib.sha256(presented.encode("utf-8", "surrogateescape")).digest(),
        hashlib.sha256(token).digest(),
    )
    return ok and scheme.lower() == "bearer"


def _parse_meta(raw: str, segment_id: str, sha: str) -> dict:
    if len(raw) > MAX_META_HEADER:
        raise ValueError("meta too large")
    try:
        meta = json.loads(base64.urlsafe_b64decode(raw.encode("ascii") + b"=" * (-len(raw) % 4)))
    except (binascii.Error, UnicodeError, ValueError) as e:
        raise ValueError("meta is not base64url JSON") from e
    if not isinstance(meta, dict) or set(meta) - OPTIONAL_FIELDS != set(META_FIELDS):
        raise ValueError("meta has wrong fields")
    if "calls" in meta:
        calls = meta["calls"]
        if not isinstance(calls, list) or len(calls) > MAX_CALLS:
            raise ValueError("meta field calls has wrong type")
        for c in calls:
            if (not isinstance(c, dict) or not {"start"} <= set(c) <= {"start", "end"}
                    or not all(isinstance(v, str) and ISO_UTC.fullmatch(v) for v in c.values())):
                raise ValueError("meta field calls has wrong type")
    if "location" in meta:
        _check_location(meta["location"])
    if "battery" in meta:
        b = meta["battery"]
        if not isinstance(b, int) or isinstance(b, bool) or not 0 <= b <= 100:
            raise ValueError("meta field battery has wrong type")
    if "firmware" in meta and not (isinstance(meta["firmware"], str) and FIRMWARE.fullmatch(meta["firmware"])):
        raise ValueError("meta field firmware has wrong type")
    for key, typ in META_FIELDS.items():
        # bool is an int subclass: reject it explicitly.
        if not isinstance(meta[key], typ) or isinstance(meta[key], bool):
            raise ValueError(f"meta field {key} has wrong type")
    if meta["id"] != segment_id or meta["sha256"] != sha:
        raise ValueError("meta does not match request")
    for key in ("start", "end"):
        if not ISO_UTC.fullmatch(meta[key]):
            raise ValueError(f"meta field {key} is not ISO 8601 UTC")
    if len(meta["device_id"]) > 128:
        raise ValueError("device_id too long")
    # The id embeds the start second: keeps one id from landing in two day folders.
    start = _utc(meta["start"])
    if start.strftime("%Y%m%dT%H%M%SZ") != segment_id[:16]:
        raise ValueError("meta start does not match segment id")
    return meta


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_state(path: Path, state: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def create_app(config: Config) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    audio_root = config.archive / "raw" / "audio"
    tmp_dir = config.archive / "tmp"
    for d in (config.archive, audio_root, tmp_dir):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    store_lock = asyncio.Lock()
    state_file = config.archive / "receiver.json"

    def record_upload(segment_id: str, request: Request) -> None:
        queue = request.headers.get("x-queue-remaining", "")
        _write_state(state_file, {
            "last_upload_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_segment_id": segment_id,
            "queue_remaining": int(queue) if queue.isdigit() and int(queue) <= MAX_QUEUE else None,
        })

    @app.post("/v1/segments/{segment_id}")
    async def upload(segment_id: str, request: Request):
        if not _authorized(request, config.token):
            return _error(401, "unauthorized")
        if not SEGMENT_ID.fullmatch(segment_id) or request.headers.get("x-segment-id") != segment_id:
            return _error(400, "invalid segment id")
        sha = request.headers.get("x-sha256", "")
        if not SHA256.fullmatch(sha):
            return _error(400, "invalid X-Sha256")
        try:
            meta = _parse_meta(request.headers.get("x-segment-meta", ""), segment_id, sha)
        except ValueError as e:
            return _error(400, str(e))

        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > config.max_body):
            return _error(413, "body too large")

        fd, tmp_name = tempfile.mkstemp(dir=tmp_dir, prefix=f"{segment_id}.", suffix=".bin")
        tmp_bin = Path(tmp_name)
        tmp_json: Path | None = None
        try:
            h = hashlib.sha256()
            size = 0
            head = b""
            with os.fdopen(fd, "wb") as f:
                try:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > config.max_body:
                            return _error(413, "body too large")
                        if len(head) < 14:
                            head += chunk[: 14 - len(head)]
                        h.update(chunk)
                        f.write(chunk)
                except ClientDisconnect:
                    # The phone keeps the segment and retries: nothing to store.
                    log.warning("upload %s interrupted by the phone after %d bytes", segment_id, size)
                    return _error(400, "client disconnected")
                f.flush()
                os.fsync(f.fileno())
            if h.hexdigest() != sha:
                return _error(400, "body sha256 mismatch")
            try:
                parse_header(head)
            except SegmentError as e:
                return _error(400, f"not a segment: {e}")

            day = _utc(meta["start"]).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
            day_dir = audio_root / day
            final_bin = day_dir / f"{segment_id}.bin"
            final_json = day_dir / f"{segment_id}.json"

            async with store_lock:
                if final_bin.exists():
                    if _sha256_file(final_bin) == sha:
                        record_upload(segment_id, request)
                        return {"id": segment_id, "sha256": sha, "status": "duplicate"}
                    return _error(409, "segment id exists with different content")
                day_dir.mkdir(mode=0o700, exist_ok=True)
                jfd, jname = tempfile.mkstemp(dir=tmp_dir, prefix=f"{segment_id}.", suffix=".json")
                tmp_json = Path(jname)
                with os.fdopen(jfd, "w") as jf:
                    json.dump(meta, jf, indent=2)
                    jf.flush()
                    os.fsync(jf.fileno())
                # .json first: a .bin is only visible once its sidecar is in place.
                os.replace(tmp_json, final_json)
                tmp_json = None
                os.replace(tmp_bin, final_bin)
                _fsync_dir(day_dir)
                record_upload(segment_id, request)
            return {"id": segment_id, "sha256": sha, "status": "stored"}
        finally:
            for p in (tmp_bin, tmp_json):
                if p is not None:
                    p.unlink(missing_ok=True)

    # The features on/off, synced with the iPhone (features.py): the phone
    # sends its values, gets back the merged ones.
    @app.get("/v1/settings")
    async def get_settings(request: Request):
        if not _authorized(request, config.token):
            return _error(401, "unauthorized")
        return {"features": features.load(config.archive)}

    @app.put("/v1/settings")
    async def put_settings(request: Request):
        if not _authorized(request, config.token):
            return _error(401, "unauthorized")
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > features.MAX_BODY:
                return _error(413, "body too large")
        try:
            merged = await asyncio.to_thread(features.merge, config.archive, json.loads(body))
        except ValueError as e:
            return _error(400, str(e))
        return {"features": merged}

    omy_lock = asyncio.Lock()
    omy_env = {**os.environ, "OMY_ARCHIVE": str(config.archive)}
    transcribing: list[asyncio.subprocess.Process] = []

    async def omy(*args: str) -> dict:
        """Runs omy --json ARGS; its error, if any, as ValueError."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "omy.cli", "--json", *args,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=omy_env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), OMY_TIMEOUT)
        except TimeoutError:
            proc.kill()
            raise
        try:
            result = json.loads(out)
        except ValueError:
            log.error("omy %s failed: %s", args[:2], err.decode(errors="replace")[-500:])
            raise RuntimeError("omy failed")
        if proc.returncode != 0:
            raise ValueError(result.get("error", "omy failed"))
        return result

    async def run(request: Request, *args: str, change: bool = False):
        if not _authorized(request, config.token):
            return _error(401, "unauthorized")
        try:
            if change:
                # One change at a time: each rewrites people/places and the days.
                async with omy_lock:
                    return await omy(*args)
            return await omy(*args)
        except ValueError as e:
            return _error(400, str(e))
        except (RuntimeError, TimeoutError):
            return _error(500, "omy failed")

    async def json_body(request: Request) -> dict:
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_JSON_BODY:
                raise ValueError("body too large")
        doc = json.loads(body or b"{}")
        if not isinstance(doc, dict):
            raise ValueError("body is not an object")
        return doc

    async def change(request: Request, build) -> JSONResponse | dict:
        """A change whose omy arguments come from the JSON body."""
        if not _authorized(request, config.token):
            return _error(401, "unauthorized")
        try:
            args = build(await json_body(request))
        except (ValueError, TypeError, KeyError):
            return _error(400, "invalid body")
        return await run(request, *args, change=True)

    @app.get("/v1/transcripts")
    async def transcripts(request: Request):
        return await run(request, "transcripts", "list")

    @app.get("/v1/transcripts/{day}")
    async def transcript_day(day: str, request: Request):
        if not DAY.fullmatch(day):
            return _error(400, "invalid day")
        return await run(request, "transcripts", "day", day)

    # POST, so what you search for stays out of the access log.
    @app.post("/v1/search")
    async def search(request: Request):
        if not _authorized(request, config.token):
            return _error(401, "unauthorized")
        try:
            q = (await json_body(request))["q"]
            if not isinstance(q, str) or not q.strip() or len(q) > MAX_QUERY or q.lstrip().startswith("-"):
                raise ValueError
        except (ValueError, KeyError):
            return _error(400, "invalid query")
        return await run(request, "transcripts", "search", q)

    # Starts transcribing the closed sessions now, without waiting for the
    # hourly job; GET /v1/transcripts shows the progress.
    @app.post("/v1/transcribe")
    async def transcribe_now(request: Request):
        if not _authorized(request, config.token):
            return _error(401, "unauthorized")
        if transcribing and transcribing[0].returncode is None:
            return {"started": False, "running": True}
        logfile = open(Path("~/Library/Logs/local-omy-periodic.log").expanduser(), "ab")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "omy.cli", "--json", "transcribe", "--pending",
                stdin=asyncio.subprocess.DEVNULL, stdout=logfile, stderr=logfile, env=omy_env,
            )
        finally:
            logfile.close()
        transcribing[:] = [proc]
        asyncio.get_running_loop().create_task(proc.wait())
        return {"started": True, "running": True}

    @app.get("/v1/people")
    async def people_list(request: Request):
        return await run(request, "people", "list")

    @app.put("/v1/people/{person}")
    async def person_rename(person: str, request: Request):
        if not PERSON_ID.fullmatch(person):
            return _error(400, "invalid person")
        return await change(request, lambda b: ["people", "rename", person, _name(b["name"])])

    @app.delete("/v1/people/{person}")
    async def person_delete(person: str, request: Request):
        if not PERSON_ID.fullmatch(person):
            return _error(400, "invalid person")
        return await run(request, "people", "delete", person, change=True)

    @app.get("/v1/voices/{day}")
    async def voices(day: str, request: Request):
        if not DAY.fullmatch(day):
            return _error(400, "invalid day")
        return await run(request, "speakers", "list", "--day", day)

    @app.put("/v1/voices/{session}/{label}")
    async def voice_rename(session: str, label: str, request: Request):
        if not SEGMENT_ID.fullmatch(session) or not VOICE_LABEL.fullmatch(label):
            return _error(400, "invalid voice")
        return await change(request, lambda b: ["speakers", "rename", session, label, _name(b["name"])])

    # Where voices come from (omy speakers kind): {"source", "labels": [...]}.
    @app.post("/v1/voices/{session}/kind")
    async def voice_kind(session: str, request: Request):
        if not SEGMENT_ID.fullmatch(session):
            return _error(400, "invalid voice")

        def args(b: dict) -> list[str]:
            labels, source = b["labels"], b["source"]
            if (source not in VOICE_SOURCES or not isinstance(labels, list) or not 0 < len(labels) <= 50
                    or not all(isinstance(x, str) and VOICE_LABEL.fullmatch(x) for x in labels)):
                raise ValueError("invalid voices")
            return ["speakers", "kind", session, source, *labels]

        return await change(request, args)

    # A few seconds of a voice, to recognize it: the WAV goes to the phone in
    # the response only (the phone plays it from memory); voice prints never
    # leave the Mac.
    @app.get("/v1/voices/{session}/{label}/sample")
    async def voice_sample(session: str, label: str, request: Request):
        if not SEGMENT_ID.fullmatch(session) or not VOICE_LABEL.fullmatch(label):
            return _error(400, "invalid voice")
        async with omy_lock:
            result = await run(request, "speakers", "play", session, label)
            if isinstance(result, Response):
                return result
            path = Path(result["path"])
            data = await asyncio.to_thread(path.read_bytes)
            path.unlink(missing_ok=True)
        return Response(data, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @app.get("/v1/places")
    async def places_list(request: Request):
        return await run(request, "places", "list")

    def place_args(b: dict) -> list[str]:
        lat, lon, radius = b["lat"], b["lon"], b.get("radius", 150)
        if not all(_number(v) for v in (lat, lon)) or not isinstance(radius, int) or isinstance(radius, bool):
            raise ValueError("invalid place")
        return ["places", "add", _name(b["name"]), repr(float(lat)), repr(float(lon)), "--radius", str(radius)]

    @app.post("/v1/places")
    async def place_add(request: Request):
        return await change(request, place_args)

    @app.put("/v1/places/{name}")
    async def place_rename(name: str, request: Request):
        try:
            name = _name(name)
        except ValueError:
            return _error(400, "invalid name")
        return await change(request, lambda b: ["places", "rename", name, _name(b["name"])])

    @app.delete("/v1/places/{name}")
    async def place_remove(name: str, request: Request):
        try:
            name = _name(name)
        except ValueError:
            return _error(400, "invalid name")
        return await run(request, "places", "remove", name, change=True)

    return app


def main() -> None:
    import uvicorn

    try:
        config = load_config()
    except ConfigError as e:
        print(f"omy-receiver: {e}", file=sys.stderr)
        sys.exit(1)
    os.umask(0o077)
    uvicorn.run(
        create_app(config),
        host="127.0.0.1",
        port=int(os.environ.get("OMY_PORT", "8787")),
        proxy_headers=False,
        server_header=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
