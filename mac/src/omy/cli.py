"""omy command line. Progress goes to stderr; the result goes to stdout, as JSON
with --json (that's what the Mac app reads)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

from omy import battery, evaluate, iphone, macsetup, people, places, status, transcribe
from omy.config import ARCHIVE, feature_on
from omy_receiver import features


def periodic() -> dict:
    """Hourly job (LaunchAgent): renew the iPhone app when needed, then
    transcribe the sessions closed since the last run."""
    print(f"--- {datetime.now().isoformat(timespec='seconds')} periodic", file=sys.stderr)
    result: dict = {}
    try:
        result["iphone"] = iphone.renew()
    except iphone.IphoneError as e:
        days_left = iphone.status().get("days_left")
        # Failures are expected at night while the phone is locked or away;
        # only speak up when the app is about to stop launching.
        if days_left is not None and days_left < iphone.URGENT_BELOW_DAYS:
            iphone.notify(f"Rinnovo dell'app iPhone non riuscito, scade tra {days_left:.1f} giorni: {e}")
        result["iphone"] = {"renewed": False, "error": str(e)}
    if not feature_on("auto_transcribe"):
        result["transcribe"] = {"skipped": "auto_transcribe off"}
        return result
    try:
        result["transcribe"] = transcribe.transcribe(transcribe.pending_days())
        if result["transcribe"]["failed"]:
            iphone.notify(f"Trascrizione non riuscita per {len(result['transcribe']['failed'])} sessioni")
    except transcribe.TranscribeError as e:
        result["transcribe"] = {"error": str(e)}
    return result


def main() -> int:
    p = argparse.ArgumentParser(prog="omy", description=__doc__)
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the segment receiver (same as omy-receiver)")
    sub.add_parser("status", help="receiver, tailscale, archive, iPhone, transcripts")

    ip = sub.add_parser("iphone", help="install and renew the iPhone app").add_subparsers(dest="action", required=True)
    ip.add_parser("status", help="profile expiry and device")
    inst = ip.add_parser("install", help="build, sign and install now")
    inst.add_argument("--no-build", action="store_true", help="install the last build again")
    renew = ip.add_parser("renew", help=f"reinstall if the profile expires within {iphone.RENEW_BELOW_DAYS} days")
    renew.add_argument("--force", action="store_true", help="reinstall regardless of expiry and time of day")

    setup = sub.add_parser("setup", help="prepare this Mac").add_subparsers(dest="action", required=True)
    setup.add_parser("check", help="checklist of what this Mac needs")
    fix = setup.add_parser("fix", help="fix one checklist item")
    fix.add_argument("item", choices=["brew", "token", *macsetup.AGENTS, "tailscale", "models", "iphone", "diarize"])
    setup.add_parser("token", help="print the receiver token (to paste into the iPhone app)")
    setup.add_parser("models", help="download the whisper and VAD models (same as fix models)")

    tr = sub.add_parser("transcribe", help="transcribe closed sessions")
    which = tr.add_mutually_exclusive_group(required=True)
    which.add_argument("--day", type=date.fromisoformat, help="YYYY-MM-DD (Europe/Rome)")
    which.add_argument("--pending", action="store_true",
                       help=f"sessions not yet transcribed in the last {transcribe.PENDING_DAYS} recorded days")
    tr.add_argument("--force", action="store_true", help="transcribe again sessions already done")
    tr.add_argument("--render", action="store_true", help="only rebuild the day markdown from the JSONs")

    ev = sub.add_parser("eval", help="transcription accuracy on a hand-corrected sample") \
        .add_subparsers(dest="action", required=True)
    ev.add_parser("make", help="cut the sample clips into ~/OmiArchive/eval, with texts to correct")
    ev.add_parser("models", help="download the optional models the configs use")
    evr = ev.add_parser("run", help="word error rate of each config against the corrected texts")
    evr.add_argument("--config", action="append", choices=list(evaluate.CONFIGS),
                     help="only this config (repeatable)")
    evr.add_argument("--fresh", action="store_true", help="transcribe again instead of reusing eval/runs")
    ev.add_parser("diarize", help="how well your words are attributed to you, on the eval clips")
    voice = sub.add_parser("voice", help="your voice profile for diarization").add_subparsers(dest="action", required=True)
    voice.add_parser("enroll", help="build it from the P: lines of the eval clips")

    sp = sub.add_parser("speakers", help="the voices of each session, to name them") \
        .add_subparsers(dest="action", required=True)
    spl = sp.add_parser("list", help="voices of the sessions of a day, with what they said")
    spl.add_argument("--day", type=date.fromisoformat, required=True, help="YYYY-MM-DD (Europe/Rome)")
    spp = sp.add_parser("play", help="write a few seconds of a voice to a WAV")
    spr = sp.add_parser("rename", help="this voice is NAME, in this and the other sessions")
    for x in (spp, spr):
        x.add_argument("session")
        x.add_argument("label", help="the voice's label, as in speakers list")
    spr.add_argument("name")
    pe = sub.add_parser("people", help="the people you named").add_subparsers(dest="action", required=True)
    pe.add_parser("list", help="you and the named people")
    per = pe.add_parser("rename", help="new name (same as another person: merged)")
    ped = pe.add_parser("delete", help="delete the voice print and the name")
    for x in (per, ped):
        x.add_argument("id")
    per.add_argument("name")

    pl = sub.add_parser("places", help="names for the places where you record").add_subparsers(dest="action", required=True)
    pl.add_parser("list", help="named places, and where you spent time without a name")
    pla = pl.add_parser("add", help="name a place (same name: replaced)")
    pla.add_argument("name")
    pla.add_argument("lat", type=float)
    pla.add_argument("lon", type=float)
    pla.add_argument("--radius", type=int, default=places.DEFAULT_RADIUS, help="meters")
    pl.add_parser("remove", help="forget a place name").add_argument("name")
    bat = sub.add_parser("battery", help="pendant battery: level, drain, how long a charge lasts")
    bat.add_argument("--days", type=int, default=battery.DAYS)

    fe = sub.add_parser("features", help="turn features on and off (synced with the iPhone)") \
        .add_subparsers(dest="action", required=True)
    fe.add_parser("list", help="every feature, on or off, and where it applies")
    fes = fe.add_parser("set", help="turn one on or off")
    fes.add_argument("key", choices=list(features.FEATURES))
    fes.add_argument("state", choices=["on", "off"])

    sub.add_parser("periodic", help="hourly job run by the LaunchAgent")

    args = p.parse_args()
    # Transcripts, caches and config are private, like the raw archive.
    os.umask(0o077)

    if args.cmd == "serve":
        from omy_receiver.app import main as serve
        serve()
        return 0

    try:
        if args.cmd == "iphone":
            result = {
                "status": iphone.status,
                "install": lambda: iphone.install(iphone.APP if args.no_build else None),
                "renew": lambda: iphone.renew(force=args.force),
            }[args.action]()
        elif args.cmd == "status":
            result = status.status()
        elif args.cmd == "setup":
            if args.action == "check":
                result = {"checks": macsetup.checks()}
            elif args.action == "fix":
                result = macsetup.fix(args.item)
            elif args.action == "token":
                result = {"token": macsetup.read_token()}
            else:
                result = transcribe.download_models()
        elif args.cmd == "transcribe":
            days = [args.day] if args.day else transcribe.pending_days()
            if args.render:
                result = {"days": [d.isoformat() for d in days if transcribe.render_day(d)]}
            else:
                result = transcribe.transcribe(days, force=args.force)
        elif args.cmd == "eval":
            if args.action == "make":
                result = evaluate.make()
            elif args.action == "models":
                result = transcribe.download_models(list(transcribe.OPTIONAL_MODELS))
            elif args.action == "diarize":
                result = evaluate.diarization()
            else:
                result = evaluate.run(args.config, fresh=args.fresh)
        elif args.cmd == "voice":
            result = evaluate.enroll()
        elif args.cmd == "speakers":
            result = {
                "list": lambda: people.list_day(args.day),
                "play": lambda: people.play(args.session, args.label),
                "rename": lambda: people.rename(args.session, args.label, args.name),
            }[args.action]()
        elif args.cmd == "places":
            result = {
                "list": places.list_places,
                "add": lambda: places.add(args.name, args.lat, args.lon, args.radius),
                "remove": lambda: places.remove(args.name),
            }[args.action]()
        elif args.cmd == "features":
            if args.action == "set":
                features.set_feature(ARCHIVE, args.key, args.state == "on")
            result = {"features": [
                {"key": k, "device": dev, "label": label, **features.load(ARCHIVE)[k]}
                for k, (dev, label) in features.FEATURES.items()
            ]}
        elif args.cmd == "battery":
            result = battery.battery(args.days)
        elif args.cmd == "people":
            result = {
                "list": people.list_people,
                "rename": lambda: people.rename_person(args.id, args.name),
                "delete": lambda: people.delete_person(args.id),
            }[args.action]()
        else:
            result = periodic()
    except (iphone.IphoneError, transcribe.TranscribeError, macsetup.SetupError) as e:
        if args.json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"omy: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result))
    else:
        for k, v in result.items():
            print(f"{k:14} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
