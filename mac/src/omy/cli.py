"""omy command line. Progress goes to stderr; the result goes to stdout, as JSON
with --json (that's what the Mac app reads)."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from datetime import date, datetime

from omy import battery, enroll, iphone, lock, macsetup, people, places, remote, review, status, transcribe
from omy.config import ARCHIVE, feature_on
from omy_receiver import features
from omy_receiver.l10n import tr


def periodic() -> dict:
    """DEPRECATED (the worker replaces it): renew the iPhone app when needed,
    then, only if the worker is not alive, transcribe the sessions closed since
    the last run. With a fresh worker heartbeat it does no transcription or
    diarization at all; otherwise it holds the same lock as the worker's jobs."""
    print(f"--- {datetime.now().isoformat(timespec='seconds')} periodic", file=sys.stderr)
    result: dict = {}
    try:
        result["iphone"] = iphone.renew()
    except iphone.IphoneError as e:
        days_left = iphone.status().get("days_left")
        # Failures are expected at night while the phone is locked or away;
        # only speak up when the app is about to stop launching.
        if days_left is not None and days_left < iphone.URGENT_BELOW_DAYS:
            iphone.notify(tr(f"Rinnovo dell'app iPhone non riuscito, scade tra {days_left:.1f} giorni: {e}",
                             f"Renewing the iPhone app failed, it expires in {days_left:.1f} days: {e}"))
        result["iphone"] = {"renewed": False, "error": str(e)}
    if not feature_on("auto_transcribe"):
        result["transcribe"] = {"skipped": "auto_transcribe off"}
        return result
    from omy import worker
    if worker.heartbeat_fresh():
        result["transcribe"] = {"skipped": "the worker is running"}
        return result
    try:
        result["transcribe"] = transcribe.transcribe(transcribe.pending_days())
        if result["transcribe"]["failed"]:
            n = len(result["transcribe"]["failed"])
            iphone.notify(tr(f"Trascrizione non riuscita per {n} sessioni", f"Transcription failed for {n} sessions"))
    except transcribe.Busy as e:
        result["transcribe"] = {"skipped": str(e)}
    except transcribe.TranscribeError as e:
        result["transcribe"] = {"error": str(e)}
    return result


def organize(since: date, force: bool = False, now: bool = False) -> dict:
    """Queue an organize job for every transcribed session since this day. They
    run now, whatever the time and the power; notes that are up to date are
    skipped, unless force."""
    from omy import jobs
    from omy.sessions import recorded_days, sessions_for_day
    queued = []
    for day in (d for d in recorded_days() if d >= since):
        for session in sessions_for_day(day):
            if transcribe._load_doc(session) is not None:
                jobs.enqueue("organize", session.id, force=force, ignore_window=True)
                queued.append(session.id)
    if now:
        jobs.wake()
    return {"queued": len(queued), "sessions": queued, "force": force}


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
    fix.add_argument("item", choices=["brew", "token", *macsetup.AGENTS, "tailscale", "models", "iphone", "diarize",
                                        "compute", "compute-on", "compute-off", "vault-mirror", "vault-mirror-on",
                                        "vault-mirror-off"])
    setup.add_parser("token", help="print the receiver token (to paste into the iPhone app)")
    sh = setup.add_parser("hub", help="use the hub: omy reads and changes the archive there (hub_url in config.toml)")
    sh.add_argument("url", nargs="?", help="https://hub.<tailnet>.ts.net:10000")
    sh.add_argument("--off", action="store_true", help="back to ~/OmiArchive")
    setup.add_parser("models", help="download the whisper and VAD models (same as fix models)")

    tr = sub.add_parser("transcribe", help="transcribe closed sessions")
    which = tr.add_mutually_exclusive_group(required=True)
    which.add_argument("--day", type=date.fromisoformat, help="YYYY-MM-DD (local day)")
    which.add_argument("--pending", action="store_true",
                       help=f"sessions not yet transcribed in the last {transcribe.PENDING_DAYS} recorded days")
    tr.add_argument("--force", action="store_true", help="transcribe again sessions already done")
    tr.add_argument("--render", action="store_true", help="only rebuild the day markdown from the JSONs")

    ts = sub.add_parser("transcripts", help="read the transcripts (what the iPhone app shows)") \
        .add_subparsers(dest="action", required=True)
    ts.add_parser("list", help="the days with a summary, and what is waiting to be transcribed")
    ts.add_parser("day", help="the paragraphs of a day").add_argument("day", type=date.fromisoformat)
    tss = ts.add_parser("search", help="paragraphs whose text or speaker contains QUERY")
    tss.add_argument("query")
    tsc = ts.add_parser("conversations", help="the sessions with a person or of a type, newest first")
    for x in (tss, tsc):
        x.add_argument("--person", help="a person id, or me")
        x.add_argument("--type", choices=review.TYPES)
    tsc.add_argument("--day", type=date.fromisoformat, help="YYYY-MM-DD")
    tsc.add_argument("--before", type=date.fromisoformat, help="only days before YYYY-MM-DD (the next page)")
    tsc.add_argument("--limit", type=int, default=review.CONVERSATIONS_LIMIT, help=f"at most {review.CONVERSATIONS_MAX}")

    voice = sub.add_parser("voice", help="your voice profile for diarization").add_subparsers(dest="action", required=True)
    voice.add_parser("enroll", help="build it from the P: lines of the clips in ~/OmiArchive/voices/enroll")

    sp = sub.add_parser("speakers", help="the voices of each session, to name them") \
        .add_subparsers(dest="action", required=True)
    spl = sp.add_parser("list", help="voices of the sessions of a day, with what they said")
    spl.add_argument("--day", type=date.fromisoformat, required=True, help="YYYY-MM-DD (local day)")
    spt = sp.add_parser("todo", help=f"sessions of the last {people.TODO_DAYS} recorded days with voices to label")
    spt.add_argument("--hours", type=int, help="only the sessions started in the last HOURS")
    spp = sp.add_parser("play", help="write a few seconds of a voice to a WAV")
    spr = sp.add_parser("rename", help="this voice is NAME, in this and the other sessions")
    for x in (spp, spr):
        x.add_argument("session")
        x.add_argument("label", help="the voice's label, as in speakers list")
    spr.add_argument("name")
    spk = sp.add_parser("kind", help="where these voices come from: in the room, an online meeting, a video, a TV")
    spk.add_argument("session")
    spk.add_argument("source", choices=["room", "meeting", "media", "auto"])
    spk.add_argument("labels", nargs="+", help="the voices' labels, as in speakers list")
    spu = sp.add_parser("unknown", help="somebody you don't know: no name, no voice print, not asked again")
    spu.add_argument("session")
    spu.add_argument("labels", nargs="+", help="the voices' labels, as in speakers list")
    spu.add_argument("--off", action="store_true", help="not unknown any more: unnamed again")
    pe = sub.add_parser("people", help="the people you named").add_subparsers(dest="action", required=True)
    pe.add_parser("list", help="you and the named people")
    per = pe.add_parser("rename", help="new name (same as another person: merged)")
    ped = pe.add_parser("delete", help="delete the voice print and the name")
    pek = pe.add_parser("kind", help="where all their voices come from; media: it was a video, the name and print go")
    pek.add_argument("id")
    pek.add_argument("source", choices=["room", "meeting", "media", "auto"])
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
    plr = pl.add_parser("rename", help="give a place another name")
    plr.add_argument("name")
    plr.add_argument("new")
    pl.add_parser("remove", help="forget a place name").add_argument("name")
    bat = sub.add_parser("battery", help="pendant battery: level, drain, how long a charge lasts")
    bat.add_argument("--days", type=int, default=battery.DAYS)

    fe = sub.add_parser("features", help="turn features on and off (synced with the iPhone)") \
        .add_subparsers(dest="action", required=True)
    fe.add_parser("list", help="every feature, on or off, and where it applies")
    fes = fe.add_parser("set", help="turn one on or off")
    fes.add_argument("key", choices=list(features.FEATURES))
    fes.add_argument("state", choices=["on", "off"])

    sub.add_parser("worker", help="run the worker: job queue, models, scans (LaunchAgent)")
    sub.add_parser("hub-worker", help="run the hub's scheduler (OMY_ROLE=hub, in the hub container)")
    cw = sub.add_parser("compute-worker", help="run the Mac's compute worker: pulls jobs from the hub (LaunchAgent)")
    cw.add_argument("--once", action="store_true", help="take one job, run it and exit")
    cw.add_argument("--dry-run", action="store_true", help="with --once: report the job and give it back, run nothing")
    cw.add_argument("--kind", action="append", help="with --once: only these kinds")
    job = sub.add_parser("job", help="run one queued job in this process (what the worker spawns)") \
        .add_subparsers(dest="action", required=True)
    for name in ("transcribe", "diarize", "diarize-compute"):
        job.add_parser(name, help=f"{name} one session").add_argument("session")
    og = sub.add_parser("organize", help="queue the second-brain notes of the sessions since a day; "
                    "they run now, whatever the time of day and the power")
    og.add_argument("--since", type=date.fromisoformat, required=True, help="YYYY-MM-DD (local day)")
    og.add_argument("--force", action="store_true",
                    help="also redo notes that are up to date (without it they are skipped)")
    og.add_argument("--now", action="store_true", help="wake the worker now")
    sub.add_parser("jobs", help="the job queue and the worker")
    ix = sub.add_parser("index", help="queue the rebuild of the search index and wake the worker")
    ix.add_argument("--all", action="store_true", required=True, help="every session")
    br = sub.add_parser("brain", help="the second brain").add_subparsers(dest="action", required=True)
    br.add_parser("status", help="notes, last note, index, vault path, night window")

    sub.add_parser("periodic", help="DEPRECATED, the worker replaces it: hourly job run by the LaunchAgent")

    args = p.parse_args()
    # Transcripts, caches and config are private, like the raw archive.
    os.umask(0o077)

    if args.cmd == "serve":
        from omy_receiver.app import main as serve
        serve()
        return 0

    # What rewrites people, places and the days: one writer at a time.
    changes = (args.cmd in ("people", "places") and args.action != "list"
               or args.cmd == "speakers" and args.action in ("rename", "kind", "unknown")
               or args.cmd == "transcribe" and args.render)
    try:
        if remote.enabled(args.cmd):  # hub_url in config.toml: the archive is on the hub (docs/hub-migration.md)
            result = remote.run(args)
        else:
            with lock.writing() if changes else contextlib.nullcontext():
                result = _run(args)
    except (iphone.IphoneError, transcribe.TranscribeError, macsetup.SetupError) as e:
        if args.json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"omy: {e}", file=sys.stderr)
        return transcribe.BUSY_EXIT if args.cmd == "job" and isinstance(e, transcribe.Busy) else 1

    if args.json:
        print(json.dumps(result))
    else:
        for k, v in result.items():
            print(f"{k:14} {v}")
    return 0


def _run(args: argparse.Namespace) -> dict:
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
        elif args.action == "hub":
            result = macsetup.set_hub_url(None if args.off else args.url)
        else:
            result = transcribe.download_models()
    elif args.cmd == "transcribe":
        days = [args.day] if args.day else transcribe.pending_days()
        if args.render:
            result = {"days": [d.isoformat() for d in days if transcribe.render_day(d)]}
        else:
            result = transcribe.transcribe(days, force=args.force)
    elif args.cmd == "transcripts":
        result = {
            "list": review.list_days,
            "day": lambda: review.day(args.day),
            "search": lambda: review.search(args.query, args.person, args.type),
            "conversations": lambda: review.conversations(args.person, args.type, args.limit, args.day,
                                                          args.before),
        }[args.action]()
    elif args.cmd == "voice":
        result = enroll.enroll()
        if remote.hub_url():  # the profile is used where the voices are named: the hub
            result["hub"] = remote.upload_profile()
    elif args.cmd == "speakers":
        result = {
            "list": lambda: people.list_day(args.day),
            "todo": lambda: people.unlabeled(hours=args.hours),
            "play": lambda: people.play(args.session, args.label),
            "rename": lambda: people.rename(args.session, args.label, args.name),
            "kind": lambda: people.set_source(args.session, args.labels, args.source),
            "unknown": lambda: people.set_unknown(args.session, args.labels, not args.off),
        }[args.action]()
    elif args.cmd == "places":
        result = {
            "list": places.list_places,
            "add": lambda: places.add(args.name, args.lat, args.lon, args.radius),
            "rename": lambda: places.rename(args.name, args.new),
            "remove": lambda: places.remove(args.name),
        }[args.action]()
    elif args.cmd == "features":
        if args.action == "set":
            features.set_feature(ARCHIVE, args.key, args.state == "on")
        result = {"features": [
            {"key": k, "device": dev, "name": name, "description": what, **features.load(ARCHIVE)[k]}
            for k in features.FEATURES for dev, name, what in [features.described(k)]
        ]}
    elif args.cmd == "worker":
        from omy import worker
        worker.main()
        result = {}
    elif args.cmd == "hub-worker":
        from omy import hubworker
        hubworker.main()
        result = {}
    elif args.cmd == "compute-worker":
        from omy import compute
        result = compute.main(once=args.once, dry_run=args.dry_run, kinds=args.kind)
    elif args.cmd == "job":
        result = {"transcribe": transcribe.transcribe_job, "diarize": transcribe.diarize_job,
                  "diarize-compute": transcribe.diarize_compute_job}[args.action](args.session)
    elif args.cmd == "organize":
        result = organize(args.since, args.force, args.now)
    elif args.cmd == "index":
        from omy import jobs
        jobs.enqueue("index", "all")
        jobs.wake()
        result = {"queued": True}
    elif args.cmd == "brain":
        result = status.brain_status()
    elif args.cmd == "jobs":
        from omy import jobs, models, worker
        result = {"worker": worker.read_status(), "jobs": jobs.summary(), "models": models.state()}
    elif args.cmd == "battery":
        result = battery.battery(args.days)
    elif args.cmd == "people":
        result = {
            "list": people.list_people,
            "rename": lambda: people.rename_person(args.id, args.name),
            "delete": lambda: people.delete_person(args.id),
            "kind": lambda: people.set_person_source(args.id, args.source),
        }[args.action]()
    else:
        result = periodic()
    return result


if __name__ == "__main__":
    sys.exit(main())
