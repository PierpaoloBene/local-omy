"""`omy` on the Mac as a client of the hub (docs/hub-migration.md, PR 10).

After the move the archive lives on the hub. The menu bar app still runs `omy --json ...` (it holds no logic of its
own), so when `hub_url` is set in ~/.config/local-omy/config.toml the commands that read or change the archive are
sent to the hub's API (the same one the iPhone uses, with the same token) and print the same JSON as before. Without
`hub_url` nothing changes: omy works on ~/OmiArchive as it always did.

Stays local: iphone, setup, voice enroll (it needs pyannote; the profile it builds is then uploaded), job,
worker, compute-worker, serve.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from omy import macsetup
from omy.config import CONFIG_DIR, load_config

REMOTE_CMDS = {"transcripts", "speakers", "people", "places", "features", "battery", "brain", "jobs", "status",
               "organize", "index", "transcribe"}


class RemoteError(macsetup.SetupError):
    """The hub said no, or could not be reached; printed as {"error": ...} like any omy error."""


def hub_url() -> str | None:
    """Only the config file turns remote mode on (OMY_HUB_URL is for the compute worker's tests)."""
    return load_config().get("hub_url") or None


def enabled(cmd: str) -> bool:
    return cmd in REMOTE_CMDS and os.environ.get("OMY_ROLE") != "hub" and hub_url() is not None


class Remote:
    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

    def call(self, method: str, path: str, body: dict | None = None, query: dict | None = None,
             raw: bool = False, timeout: float = 120.0):
        if query:
            path += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error") or f"HTTP {e.code}"
            except ValueError:
                msg = f"HTTP {e.code}"
            raise RemoteError(f"the hub answered: {msg}") from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise RemoteError(f"the hub is not reachable ({e}): is Tailscale on?") from e
        return payload if raw else json.loads(payload or b"{}")


def client() -> Remote:
    token = macsetup.read_token()  # the same token the iPhone has
    return Remote(hub_url(), token)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run(args) -> dict:
    """The result of `omy --json ARGS` for a command that is remote, from the hub."""
    c = client()
    cmd, act = args.cmd, getattr(args, "action", None)
    if cmd == "transcripts":
        if act == "list":
            return c.call("GET", "/v1/transcripts")
        if act == "day":
            return c.call("GET", f"/v1/transcripts/{args.day}")
        if act == "search":
            return c.call("POST", "/v1/search", {"q": args.query, **_filters(args)})
        return c.call("GET", "/v1/conversations", query={
            "person": args.person, "type": args.type, "day": args.day, "before": args.before,
            "limit": args.limit})
    if cmd == "speakers":
        if act == "list":
            return c.call("GET", f"/v1/voices/{args.day}")
        if act == "todo":
            return c.call("GET", "/v1/unlabeled", query={"hours": args.hours})
        if act == "play":
            wav = c.call("GET", f"/v1/voices/{args.session}/{args.label}/sample", raw=True)
            fd, path = tempfile.mkstemp(prefix="voice-", suffix=".wav")
            with os.fdopen(fd, "wb") as f:
                f.write(wav)
            return {"path": path}
        if act == "rename":
            return c.call("PUT", f"/v1/voices/{args.session}/{args.label}", {"name": args.name})
        if act == "kind":
            return c.call("POST", f"/v1/voices/{args.session}/kind", {"source": args.source, "labels": args.labels})
        return c.call("POST", f"/v1/voices/{args.session}/unknown", {"labels": args.labels, "unknown": not args.off})
    if cmd == "people":
        if act == "list":
            return c.call("GET", "/v1/people")
        if act == "rename":
            return c.call("PUT", f"/v1/people/{args.id}", {"name": args.name})
        if act == "delete":
            return c.call("DELETE", f"/v1/people/{args.id}")
        return c.call("POST", f"/v1/people/{args.id}/kind", {"source": args.source})
    if cmd == "places":
        quote = urllib.parse.quote
        if act == "list":
            return c.call("GET", "/v1/places")
        if act == "add":
            return c.call("POST", "/v1/places", {"name": args.name, "lat": args.lat, "lon": args.lon,
                                                 "radius": args.radius})
        if act == "rename":
            return c.call("PUT", f"/v1/places/{quote(args.name, safe='')}", {"name": args.new})
        return c.call("DELETE", f"/v1/places/{quote(args.name, safe='')}")
    if cmd == "features":
        from omy_receiver import features
        if act == "set":
            merged = c.call("PUT", "/v1/settings", {"features": {args.key: {"on": args.state == "on", "at": _now()}}})
        else:
            merged = c.call("GET", "/v1/settings")
        data = merged["features"]
        return {"features": [{"key": k, "device": dev, "name": name, "description": what, **data[k]}
                             for k in features.FEATURES for dev, name, what in [features.described(k)]]}
    if cmd == "battery":
        return c.call("GET", "/v1/battery")
    if cmd == "jobs":
        return c.call("GET", "/v1/brain/status")
    if cmd == "brain":
        doc = c.call("GET", "/v1/brain/summary")
        mirror = Path("~/OmiBrain").expanduser()   # the read-only copy of the vault (docs/cutover.md)
        if mirror.is_dir():
            doc["vault"] = str(mirror)
        return doc
    if cmd == "organize":
        return c.call("POST", "/v1/brain/organize", {"since": args.since.isoformat(), "force": args.force})
    if cmd == "index":
        return c.call("POST", "/v1/brain/index")
    if cmd == "transcribe":
        if args.day or args.force or args.render:
            raise RemoteError("transcription runs on the hub now: only `omy transcribe --pending` (wake it) is available")
        return {**c.call("POST", "/v1/transcribe"), "transcribed": [], "failed": {}, "open": [], "days": []}
    if cmd == "status":
        return status(c)
    raise RemoteError(f"no remote form of {cmd}")


def _filters(args) -> dict:
    return {k: v for k, v in (("person", args.person), ("type", args.type)) if v}


def status(c: Remote) -> dict:
    """omy status for the app: the Mac's own pieces (compute worker, iPhone app, Tailscale) and, for the archive,
    the upload and the transcripts, what the hub says. The keys are the ones the app reads."""
    from omy import iphone, status as local
    hub = c.call("GET", "/v1/status")
    listing = c.call("GET", "/v1/transcripts")
    mac = hub.get("mac") or {}
    compute = macsetup.agent_info(macsetup.COMPUTE) if macsetup._plist_path(macsetup.COMPUTE).exists() \
        else macsetup.agent_info("worker")
    queue = hub.get("queue") or {}
    worker = {**compute, "state": mac.get("state"), "job": mac.get("job"), "loaded_model": None,
              "chat_active": None, "last_error": mac.get("last_error"), "queued": queue.get("queued", 0),
              "by_kind": queue.get("by_kind", {}), "failed": queue.get("failed_jobs", []), "heartbeat_age": mac.get("last_seen_s")}
    tailscale = macsetup.tailscale_info()
    pending = listing.get("pending") or {}
    transcripts = {"path": hub_url(), "days": [d["day"] for d in listing.get("days", [])],
                   "pending_sessions": pending.get("sessions", 0), "pending_minutes": pending.get("minutes", 0),
                   "open_sessions": pending.get("open_sessions", 0), "progress_file": "", "progress": pending.get("progress") or {"running": False}}
    iph = iphone.status()
    if hub.get("last_segment_start") and iph.get("installed_at"):
        # the app was reopened if a segment started after the install (the segments are on the hub)
        iph["needs_reopen"] = hub["last_segment_start"] < iph["installed_at"].replace("+00:00", "Z")
    return {
        # the "receiver" is the hub now: reachable, since this call just worked
        "receiver": {"loaded": True, "running": True, "pid": None, "last_exit": None, "up_to_date": True},
        "worker": worker,
        "tailscale": {**tailscale, "serving": True, "url": hub_url()},
        "archive": {**hub["archive"], "path": hub_url()},
        "upload": hub["upload"], "iphone": iph, "transcripts": transcripts,
        "battery": hub["battery"], "firmware": hub["firmware"], "logs": local.LOGS, "mac_online": mac.get("online"),
        "compute": compute,
    }


def upload_profile() -> dict:
    """voices/me.npy, after `omy voice enroll` (it needs pyannote: it runs here), to the hub."""
    from omy import diarize
    from omy.compute import hub_from_config
    hub = hub_from_config()
    r = hub.request("PUT", "/compute/v1/profile", data=diarize.PROFILE.read_bytes())
    if r.status != 200:
        raise RemoteError(f"the hub did not take the voice profile: {r.status}")
    return {"uploaded": True}
