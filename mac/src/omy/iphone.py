"""Build, sign (free Personal Team) and install the iPhone app without AltStore.

A free Apple ID gives provisioning profiles valid for 7 days: the app stops
launching when the profile embedded in it expires. Renewing means building again
with a fresh profile and reinstalling. Reinstalling kills the running app and iOS
does not relaunch it, so renewal happens at night, only when needed, and the user
is notified to reopen the app.
"""

from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from omy.config import (
    ARCHIVE, BUNDLE_ID, IPHONE_BUILD, IPHONE_SRC, load_config, load_state, save_config, save_state,
)

PROFILES_DIR = Path("~/Library/Developer/Xcode/UserData/Provisioning Profiles").expanduser()
APP = IPHONE_BUILD / "local-omy.app"
DERIVED = IPHONE_BUILD / "DerivedData"

RENEW_BELOW_DAYS = 3
# Renewal kills the app on the phone: do it at night (local time) unless the
# profile is about to expire.
RENEW_HOURS = range(1, 8)
URGENT_BELOW_DAYS = 1


class IphoneError(Exception):
    pass


def log(msg: str) -> None:
    # Progress goes to stderr; stdout is reserved for the command result (--json).
    print(msg, file=sys.stderr, flush=True)


def run(cmd: list[str], what: str, cwd: Path | None = None) -> None:
    log(f"$ {' '.join(cmd)}")
    # Tool output is streamed to stderr as it arrives.
    result = subprocess.run(cmd, cwd=cwd, stdout=sys.stderr, stderr=sys.stderr)
    if result.returncode != 0:
        raise IphoneError(f"{what} failed (exit {result.returncode}), see the output above")


def notify(message: str) -> None:
    # Message passed as an argument, never interpolated into the script.
    subprocess.run(
        ["osascript", "-e", "on run argv", "-e",
         'display notification (item 1 of argv) with title "local-omy"', "-e", "end run", message],
        check=False, capture_output=True,
    )


def read_profile(path: Path) -> dict:
    out = subprocess.run(["security", "cms", "-D", "-i", str(path)], capture_output=True, check=True)
    return plistlib.loads(out.stdout)


def _profiles() -> list[tuple[Path, dict]]:
    found = []
    for p in sorted(PROFILES_DIR.glob("*.mobileprovision")):
        try:
            found.append((p, read_profile(p)))
        except (subprocess.CalledProcessError, plistlib.InvalidFileException):
            continue
    return found


def detect_team() -> str:
    """Team id of the free Personal Team, from Xcode's cached profiles."""
    teams = {
        team
        for _, prof in _profiles()
        if prof.get("Name", "").startswith("iOS Team Provisioning Profile")
        for team in prof.get("TeamIdentifier", [])
    }
    if len(teams) != 1:
        raise IphoneError(
            f"cannot detect the Personal Team ({len(teams)} candidates: {sorted(teams)}); "
            f"set team_id in ~/.config/local-omy/config.toml"
        )
    return teams.pop()


def list_devices() -> list[dict]:
    """Physical iOS devices known to CoreDevice (paired over USB or Wi-Fi)."""
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "devices.json"
        subprocess.run(["xcrun", "devicectl", "list", "devices", "--json-output", str(out)],
                       capture_output=True, check=False)
        try:
            data = json.loads(out.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            raise IphoneError("devicectl list devices failed")
    devices = []
    for dev in data.get("result", {}).get("devices", []):
        hw, props = dev.get("hardwareProperties", {}), dev.get("deviceProperties", {})
        conn = dev.get("connectionProperties", {})
        if hw.get("platform") != "iOS" or hw.get("reality") == "simulated":
            continue
        devices.append({
            "udid": hw.get("udid"),
            "name": props.get("name"),
            "paired": conn.get("pairingState") == "paired",
            "transport": conn.get("transportType"),
        })
    return devices


def ensure_config() -> dict[str, str]:
    """Fill in team_id and device_udid on first use, then keep them fixed."""
    config = load_config()
    changed = False
    if "team_id" not in config:
        config["team_id"] = detect_team()
        changed = True
    if "device_udid" not in config:
        paired = [d for d in list_devices() if d["paired"]]
        if len(paired) != 1:
            raise IphoneError(
                f"{len(paired)} paired iPhones; set device_udid in ~/.config/local-omy/config.toml"
            )
        config["device_udid"] = paired[0]["udid"]
        changed = True
    if changed:
        save_config(config)
    return config


def profile_expiry(app: Path) -> datetime:
    exp = read_profile(app / "embedded.mobileprovision")["ExpirationDate"]
    return exp.replace(tzinfo=timezone.utc) if exp.tzinfo is None else exp


def _drop_cached_profiles(team: str) -> None:
    """Remove Xcode's cached profiles for this app so that the build fetches a
    new one instead of reusing a profile that expires soon."""
    app_id = f"{team}.{BUNDLE_ID}"
    for path, prof in _profiles():
        if prof.get("Entitlements", {}).get("application-identifier") == app_id:
            log(f"removing cached profile {path.name}")
            path.unlink()


def build(team: str, udid: str | None) -> Path:
    """With udid, build for that phone and register it in the team (needs the
    phone unlocked). Without, build generically: the profile covers the devices
    already registered, so this works with the phone locked or away."""
    run(["flutter", "build", "ios", "--release", "--config-only"], "flutter build", cwd=IPHONE_SRC)
    destination = ["-destination", f"id={udid}", "-allowProvisioningDeviceRegistration"] if udid \
        else ["-destination", "generic/platform=iOS"]
    # The team is passed on the command line, never written to the project.
    run([
        "xcodebuild", "-workspace", "ios/Runner.xcworkspace", "-scheme", "Runner",
        "-configuration", "Release", *destination,
        "-derivedDataPath", str(DERIVED), "-allowProvisioningUpdates", "-quiet",
        f"DEVELOPMENT_TEAM={team}", "CODE_SIGN_STYLE=Automatic",
        "build",
    ], "xcodebuild", cwd=IPHONE_SRC)
    built = DERIVED / "Build" / "Products" / "Release-iphoneos" / "Runner.app"
    if APP.exists():
        shutil.rmtree(APP)
    shutil.copytree(built, APP, symlinks=True)
    return APP


def install(app: Path | None = None) -> dict:
    """Build for the phone (registering it if needed) unless app is given, then install."""
    config = ensure_config()
    udid = config["device_udid"]
    if app is None:
        app = build(config["team_id"], udid)
    elif not app.exists():
        raise IphoneError(f"{app} not found: build it first")
    expires = profile_expiry(app)
    run(["xcrun", "devicectl", "device", "install", "app", "--device", udid, str(app)], "install on the iPhone")
    state = load_state()
    state.update({
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "expires": expires.isoformat(timespec="seconds"),
    })
    save_state(state)
    notify("App aggiornata sull'iPhone: riaprila per riprendere la registrazione.")
    return {"installed": True, "expires": state["expires"]}


def _last_segment_start() -> datetime | None:
    """Start time of the newest segment in the archive, from its id."""
    days = sorted((ARCHIVE / "raw" / "audio").glob("????-??-??"), reverse=True)
    for day in days[:3]:
        ids = sorted(p.stem for p in day.glob("*.bin"))
        if ids:
            return datetime.strptime(ids[-1][:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return None


def status() -> dict:
    config, state = load_config(), load_state()
    now = datetime.now(timezone.utc)
    result: dict = {
        "team_id": config.get("team_id"),
        "device_udid": config.get("device_udid"),
        "expires": state.get("expires"),
        "days_left": None,
        "installed_at": state.get("installed_at"),
        "needs_reopen": False,
    }
    if state.get("expires"):
        result["days_left"] = round((datetime.fromisoformat(state["expires"]) - now).total_seconds() / 86400, 2)
    if state.get("installed_at"):
        # The app was reopened if a segment started after the install.
        last = _last_segment_start()
        result["needs_reopen"] = last is None or last < datetime.fromisoformat(state["installed_at"])
    try:
        devices = list_devices()
    except IphoneError:
        devices = []
    result["device"] = next((d for d in devices if d["udid"] == config.get("device_udid")), None)
    return result


def renew(force: bool = False) -> dict:
    state = load_state()
    if not state.get("expires"):
        raise IphoneError("the app was never installed from here: run omy iphone install")
    if not force:
        days_left = (datetime.fromisoformat(state["expires"]) - datetime.now(timezone.utc)).total_seconds() / 86400
        if days_left >= RENEW_BELOW_DAYS:
            return {"renewed": False, "reason": f"{days_left:.1f} days left"}
        if days_left >= URGENT_BELOW_DAYS and datetime.now().hour not in RENEW_HOURS:
            return {"renewed": False, "reason": f"{days_left:.1f} days left, waiting for the night"}

    config = ensure_config()
    old = datetime.fromisoformat(state["expires"])
    _drop_cached_profiles(config["team_id"])
    app = build(config["team_id"], None)
    new = profile_expiry(app)
    if not force and (new - old).total_seconds() < 86400:
        # Reinstalling would kill the app for nothing.
        raise IphoneError(f"Xcode did not issue a newer profile (expires {new.isoformat()}); not installing")
    return {"renewed": True, **install(app)}
