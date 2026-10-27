"""omy command line. Progress goes to stderr; the result goes to stdout, as JSON
with --json (that's what the Mac app reads)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from omy import iphone


def periodic() -> dict:
    """Hourly job (LaunchAgent): renew the iPhone app when needed."""
    print(f"--- {datetime.now().isoformat(timespec='seconds')} periodic", file=sys.stderr)
    try:
        return {"iphone": iphone.renew()}
    except iphone.IphoneError as e:
        days_left = iphone.status().get("days_left")
        # Failures are expected at night while the phone is locked or away;
        # only speak up when the app is about to stop launching.
        if days_left is not None and days_left < iphone.URGENT_BELOW_DAYS:
            iphone.notify(f"Rinnovo dell'app iPhone non riuscito, scade tra {days_left:.1f} giorni: {e}")
        return {"iphone": {"renewed": False, "error": str(e)}}


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

    sub.add_parser("periodic", help="hourly job run by the LaunchAgent")

    args = p.parse_args()

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
        else:
            result = periodic()
    except iphone.IphoneError as e:
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
