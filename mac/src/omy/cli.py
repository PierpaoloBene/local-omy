"""omy command line. Progress goes to stderr; the result goes to stdout, as JSON
with --json (that's what the Mac app reads)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

from omy import iphone, transcribe


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

    ip = sub.add_parser("iphone", help="install and renew the iPhone app").add_subparsers(dest="action", required=True)
    ip.add_parser("status", help="profile expiry and device")
    inst = ip.add_parser("install", help="build, sign and install now")
    inst.add_argument("--no-build", action="store_true", help="install the last build again")
    renew = ip.add_parser("renew", help=f"reinstall if the profile expires within {iphone.RENEW_BELOW_DAYS} days")
    renew.add_argument("--force", action="store_true", help="reinstall regardless of expiry and time of day")

    setup = sub.add_parser("setup", help="prepare this Mac").add_subparsers(dest="action", required=True)
    setup.add_parser("models", help="download the whisper and VAD models (checksummed)")

    tr = sub.add_parser("transcribe", help="transcribe closed sessions")
    which = tr.add_mutually_exclusive_group(required=True)
    which.add_argument("--day", type=date.fromisoformat, help="YYYY-MM-DD (Europe/Rome)")
    which.add_argument("--pending", action="store_true",
                       help=f"sessions not yet transcribed in the last {transcribe.PENDING_DAYS} recorded days")
    tr.add_argument("--force", action="store_true", help="transcribe again sessions already done")

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
        elif args.cmd == "setup":
            result = transcribe.download_models()
        elif args.cmd == "transcribe":
            days = [args.day] if args.day else transcribe.pending_days()
            result = transcribe.transcribe(days, force=args.force)
        else:
            result = periodic()
    except (iphone.IphoneError, transcribe.TranscribeError) as e:
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
