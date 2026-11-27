#!/bin/bash
# The cutover: local-omy moves from the Mac to the hub (docs/cutover.md, docs/hub-migration.md).
# Every step prints what it will do and STOPS to ask before it changes anything. Safe to stop with ^C between
# steps, and to run again from a step: --from N.
#
#   scripts/cutover.sh                run the steps in order (0 to 8)
#   scripts/cutover.sh --from 3       start from step 3 (after an interruption)
#   scripts/cutover.sh --only 5       one step
#   scripts/cutover.sh --dry-run      show what each step would run (rsync -n); nothing is changed
#   scripts/cutover.sh --rollback     go back to the Mac (before new data reached the hub)
#   scripts/cutover.sh --rollback --with-new-data     the same, copying back what the hub received meanwhile
#
# Needs: ssh to hub@hub.local with a key, the hub's omy service up (docker), ~/.config/local-omy/{token,compute_token},
# `uv`, and (for the hub's sudo) ~/.config/home-hub/hub-password.txt, which is only piped to `sudo -S`, never printed.
# macOS bash 3.2 compatible.
set -uo pipefail

HUB="${HUB:-hub@hub.local}"
HUB_DATA="${HUB_DATA:-/srv/hub/data/omy}"
HUB_URL="${HUB_URL:-https://hub.tail7fcf68.ts.net:10000}"
MAC_URL="${MAC_URL:-https://macbook-pro-di-peppo.tail7fcf68.ts.net}"
ARCH="${ARCH:-$HOME/OmiArchive}"
CFG="$HOME/.config/local-omy"
PREFIX="${PREFIX:-com.pierpaolobene.local-omy}"
UIDN="$(id -u)"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
MAC_SRC="$REPO/mac/src"
HUB_PASSWORD_FILE="$HOME/.config/home-hub/hub-password.txt"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/omy-cutover.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT

DRY=0 FROM=0 ONLY="" ROLLBACK=0 WITH_DATA=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --from) FROM="$2"; shift ;;
    --only) ONLY="$2"; shift ;;
    --rollback) ROLLBACK=1 ;;
    --with-new-data) WITH_DATA=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

# --- helpers --------------------------------------------------------------------------------------

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
good() { printf '   \033[32mOK\033[0m  %s\n' "$*"; }
bad()  { printf '   \033[31mNO\033[0m  %s\n' "$*"; }
die()  { printf '\n\033[31mSTOP: %s\033[0m\n' "$*" >&2; exit 1; }

confirm() {  # confirm "question": y to go on; anything else stops the script
  [ "$DRY" = 1 ] && { info "[dry-run] would ask: $1"; return 0; }
  printf '\n   \033[33m? %s [y/N] \033[0m' "$1"
  read -r ans
  [ "$ans" = y ] || [ "$ans" = Y ] || die "stopped by you at: $1"
}

run() {  # run a command, or only show it with --dry-run
  if [ "$DRY" = 1 ]; then info "[dry-run] $*"; return 0; fi
  info "\$ $*"
  "$@"
}

hub() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$HUB" "$@"; }
hub_compose() { hub "cd /srv/hub/repo && docker compose $*"; }
hub_sudo() {  # hub_sudo CMD...: the password goes to sudo -S on stdin and nowhere else
  [ -r "$HUB_PASSWORD_FILE" ] || die "$HUB_PASSWORD_FILE not readable: run this one by hand on the hub with sudo"
  if [ "$DRY" = 1 ]; then info "[dry-run] ssh $HUB sudo $*"; return 0; fi
  info "\$ ssh $HUB sudo $*"
  ssh -o BatchMode=yes "$HUB" "sudo -S -p '' $*" < "$HUB_PASSWORD_FILE"
}
omy() { (cd "$MAC_SRC" && uv run --quiet omy "$@"); }

# The iPhone's token goes to curl through its config on stdin, so it never shows up in `ps`.
api() {  # api PATH [curl args]: GET on the hub with the iPhone token
  local path="$1"; shift
  printf 'header = "Authorization: Bearer %s"\n' "$(cat "$CFG/token")" | curl -sS -m 60 --config - "$@" "$HUB_URL$path"
}
widget() { api /v1/widget; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

launch_label() { echo "$PREFIX.$1"; }
agent_loaded() { launchctl list | awk '{print $3}' | grep -qx "$(launch_label "$1")"; }

step_selected() {  # step_selected N
  if [ -n "$ONLY" ]; then [ "$ONLY" = "$1" ]; else [ "$1" -ge "$FROM" ]; fi
}

# --- step 0: before anything -----------------------------------------------------------------------

step0() {
  say "0. Preflight (nothing is changed)"
  local fail=0
  hub true 2>/dev/null && good "ssh to $HUB" || { bad "ssh to $HUB"; fail=1; }
  [ -r "$CFG/token" ] && good "iPhone token present" || { bad "$CFG/token missing"; fail=1; }
  if [ -r "$CFG/compute_token" ] && [ "$(stat -f %Lp "$CFG/compute_token")" = 600 ]; then good "compute token present (600)"
  else bad "$CFG/compute_token missing or not 600 (docs/cutover.md, section Prima)"; fail=1; fi
  local mine hubs
  mine="$(git -C "$REPO" rev-parse --short HEAD)"
  hubs="$(hub 'git -C /srv/hub/apps/local-omy rev-parse --short HEAD' 2>/dev/null)"
  if [ "$mine" = "$hubs" ]; then good "same code on the Mac and on the hub ($mine)"
  else bad "the Mac is at $mine, the hub's clone at $hubs: git push, then on the hub 'git -C /srv/hub/apps/local-omy pull && docker compose up -d --build omy'"; fail=1; fi
  hub_compose ps omy 2>/dev/null | grep -q "Up" && good "hub: omy container up" || { bad "hub: omy container not up"; fail=1; }
  hub 'tailscale serve status 2>&1' | grep -q ':10000' && good "hub: tailscale serve on :10000" || { bad "hub: no serve on :10000 (sudo bash bootstrap/serve.sh)"; fail=1; }
  local code i
  for i in 1 2 3 4 5; do  # the container may have just been restarted
    code="$(api /v1/transcripts -o /dev/null -w '%{http_code}' 2>/dev/null)"; [ "$code" = 200 ] && break; sleep 3
  done
  [ "$code" = 200 ] && good "hub API answers with the iPhone token" || { bad "hub API answered '$code' on $HUB_URL"; fail=1; }
  hub "grep -q '^OMY_HUB_SCHEDULER=off' /srv/hub/secrets/omy.env" 2>/dev/null && good "hub scheduler is still OFF (parallel phase)" || info "hub scheduler is not 'off' (already on?)"
  hub "df -h /srv/hub | tail -1" | awk '{print "   hub disk: " $4 " free, " $5 " used"}'
  for a in receiver worker; do agent_loaded "$a" && good "Mac $a agent loaded (it will be stopped in step 1)" || info "Mac $a agent not loaded"; done
  agent_loaded compute && { bad "the compute agent is already loaded: the Mac worker would compete with it"; fail=1; } || good "compute agent not loaded yet"
  if [ -r "$ARCH/cache/worker.json" ]; then
    python3 - "$ARCH/cache/worker.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"   Mac worker: {d.get('state')}, job: {d.get('job')}, queued: {d['queue']['queued']}")
PY
  fi
  info "Tailscale serve on the Mac, as it is now:"; tailscale serve status 2>&1 | sed 's/^/     /'
  [ "$fail" = 0 ] || die "fix the points marked NO and run again"
  info "Do it when the iPhone is at home, nothing is being recorded, and you have 20 minutes."
  confirm "Preflight is clean. Go on?"
}

# --- step 1: stop the Mac's receiver and worker -----------------------------------------------------

step1() {
  say "1. Stop the Mac's receiver and worker (the iPhone keeps its segments and retries later)"
  info "The running job goes back in the queue (the worker handles SIGTERM). The agents are also DISABLED, so that a login does not start them again."
  confirm "Stop the worker and the receiver now?"
  for a in worker receiver; do
    run launchctl bootout "gui/$UIDN/$(launch_label "$a")"
    run launchctl disable "gui/$UIDN/$(launch_label "$a")"
  done
  [ "$DRY" = 1 ] || { sleep 2; agent_loaded worker && die "the worker is still loaded"; agent_loaded receiver && die "the receiver is still loaded"; }
  good "receiver and worker stopped"
  info "The old endpoint ($MAC_URL) is closed next, so that nobody reaches it by mistake."
  tailscale serve status 2>&1 | sed 's/^/     /'
  confirm "Remove the Mac's serve on :443 (tailscale serve --https=443 off)?"
  run tailscale serve --https=443 off
}

# --- step 2: final copy ----------------------------------------------------------------------------

RSYNC_DIRS=(raw transcripts brain voices context)
RSYNC_FILES=(places.json settings.json receiver.json)

step2() {
  say "2. Final copy Mac -> hub (checksums, with --delete: the hub becomes an exact copy of ~/OmiArchive)"
  info "models/ and the rebuildable caches are not copied. voices/enroll (hand-annotated clips) goes too."
  info "First the hub's omy is stopped: nothing may write while the files are replaced."
  confirm "Stop the hub's omy container and look at what rsync would change?"
  run hub_compose stop omy
  info "Preview (rsync -n): files that would change."
  (cd "$ARCH" && rsync -ain --checksum --delete -e "ssh -o BatchMode=yes" "${RSYNC_DIRS[@]}" "${RSYNC_FILES[@]}" "$HUB:$HUB_DATA/" \
     | grep -E '^([<>]f|\*deleting)' | head -40)
  info "(an empty list means the hub already has everything)"
  confirm "Copy for real?"
  run bash -c "cd '$ARCH' && rsync -a --checksum --delete --stats -e 'ssh -o BatchMode=yes' ${RSYNC_DIRS[*]} ${RSYNC_FILES[*]} '$HUB:$HUB_DATA/' | grep -E 'Number of regular files transferred|Number of files transferred|Total transferred file size'"
  # The search index: a consistent copy (sqlite .backup), not the live file; stale -wal/-shm of the hub's own are removed.
  run sqlite3 "$ARCH/cache/brain-index.sqlite" ".backup $SCRATCH/brain-index.sqlite"
  run hub "rm -f $HUB_DATA/cache/brain-index.sqlite-wal $HUB_DATA/cache/brain-index.sqlite-shm"
  run scp -q "$SCRATCH/brain-index.sqlite" "$HUB:$HUB_DATA/cache/brain-index.sqlite"
  run hub "chmod 600 $HUB_DATA/cache/brain-index.sqlite; rm -f $HUB_DATA/db/jobs.sqlite*"
  info "The hub's queue starts empty: its first scan queues what is missing (as the Mac's own scan would)."
  good "copied"
}

# --- step 3: verify ----------------------------------------------------------------------------------

step3() {
  say "3. Verify: counts, a hash of every file, sidecars against audio, permissions"
  [ "$DRY" = 1 ] && { info "[dry-run] would run tools/verify_archive.py on both sides and compare"; return 0; }
  python3 "$MAC_SRC/tools/verify_archive.py" "$ARCH" > "$SCRATCH/mac.json"; local rc1=$?
  hub "python3 - $HUB_DATA" < "$MAC_SRC/tools/verify_archive.py" > "$SCRATCH/hub.json"; local rc2=$?
  python3 - "$SCRATCH/mac.json" "$SCRATCH/hub.json" "$rc1" "$rc2" <<'PY' || die "the archives differ: do not go on (rollback is trivial: nothing has changed on the Mac)"
import json, sys
a, b = (json.load(open(p)) for p in sys.argv[1:3])
ok = int(sys.argv[3]) == 0 and int(sys.argv[4]) == 0
for k in ("bin", "json", "days", "transcript_days", "sessions_json", "speakers_json", "notes", "voices_files", "manifest_sha256"):
    same = a[k] == b[k]
    ok &= same
    print(f"   {'OK' if same else 'NO'}  {k:16} mac={str(a[k])[:20]:20} hub={str(b[k])[:20]}")
for side, d in (("mac", a), ("hub", b)):
    if d["problems"]:
        ok = False
        print(f"   NO  problems on {side}: {d['problems'][:3]}")
sys.exit(0 if ok else 1)
PY
  local diffs
  diffs="$(cd "$ARCH" && rsync -ain --checksum --delete -e 'ssh -o BatchMode=yes' "${RSYNC_DIRS[@]}" "$HUB:$HUB_DATA/" | grep -E '^([<>]f|\*deleting)' | wc -l | tr -d ' ')"
  [ "$diffs" = 0 ] && good "rsync -n --checksum lists no difference" || die "rsync still sees $diffs differences"
  local loose
  loose="$(hub "find $HUB_DATA -type f ! -perm 600 -not -path '*/cache/*' | wc -l; find $HUB_DATA ! -user hub | wc -l" | tr '\n' ' ')"
  info "files not 600 / not owned by hub: $loose (both should be 0 0)"
  confirm "Counts and hashes are identical. Go on?"
}

# --- step 4: the hub takes over ------------------------------------------------------------------------

step4() {
  say "4. Hub: start omy with the scheduler ON"
  confirm "Switch OMY_HUB_SCHEDULER to on and start the hub's omy?"
  run hub "sed -i 's/^OMY_HUB_SCHEDULER=.*/OMY_HUB_SCHEDULER=on/' /srv/hub/secrets/omy.env"
  run hub_compose up -d omy
  [ "$DRY" = 1 ] && return 0
  local i code=""
  for i in 1 2 3 4 5 6 7 8 9 10; do
    code="$(api /v1/transcripts -o /dev/null -w '%{http_code}' 2>/dev/null)"; [ "$code" = 200 ] && break; sleep 3
  done
  [ "$code" = 200 ] && good "the hub answers on $HUB_URL" || die "the hub does not answer (docker compose logs omy on the hub)"
  # the same days, the same speakers as the Mac
  api /v1/transcripts > "$SCRATCH/hub-list.json"
  omy --json transcripts list > "$SCRATCH/mac-list.json" 2>/dev/null
  python3 - "$SCRATCH/mac-list.json" "$SCRATCH/hub-list.json" <<'PY' && good "transcripts list: same days on the Mac and on the hub" || die "different transcripts list: timezone or language of the hub?"
import json, sys
a, b = (json.load(open(p)) for p in sys.argv[1:3])
sys.exit(0 if a["days"] == b["days"] else 1)
PY
  hub_compose logs --tail 5 omy 2>&1 | sed 's/^/     /'
}

# --- step 5: the Mac works for the hub -------------------------------------------------------------------

step5() {
  say "5. Mac: compute worker ON, omy as a client of the hub, vault copy ON"
  info "From now on the app and omy on the Mac read and change the archive on the hub (hub_url in config.toml)."
  confirm "Enable the compute worker and the vault mirror on this Mac?"
  run omy --json setup hub "$HUB_URL"
  run omy --json setup fix compute-on
  run omy --json setup fix vault-mirror-on
  [ "$DRY" = 1 ] && return 0
  local i on=""
  for i in $(seq 1 20); do
    on="$(widget | json "d['mac']['online']" 2>/dev/null)"; [ "$on" = True ] && break; sleep 5
  done
  [ "$on" = True ] && good "the hub sees the Mac online" || die "the hub does not see the Mac (tail ~/Library/Logs/local-omy-compute.log)"
  widget | json "'   mac: ' + str(d['mac']['state']) + ', kinds ' + str(d['mac']['kinds']) + ', version_ok ' + str(d['mac']['version_ok']) + ', queue ' + str(d['queue']['by_kind'])"
  info "Tail of the compute log:"; tail -n 5 "$HOME/Library/Logs/local-omy-compute.log" 2>/dev/null | sed 's/^/     /'
}

# --- step 6: the iPhone ---------------------------------------------------------------------------------------

step6() {
  say "6. iPhone: change the URL (by hand, 30 seconds)"
  info "Open the app -> Impostazioni -> URL del Mac:  $HUB_URL"
  info "The token stays as it is (leave the field empty). The segments waiting on the phone go up by themselves."
  confirm "Done on the iPhone?"
  [ "$DRY" = 1 ] && return 0
  local before after i
  before="$(widget | json "d['iphone_upload']['last_upload_at']")"
  info "Waiting for the iPhone's first upload to reach the hub (up to 5 minutes; open the app to speed it up)..."
  for i in $(seq 1 30); do
    after="$(widget | json "d['iphone_upload']['last_upload_at']")"
    [ "$after" != "$before" ] && break; sleep 10
  done
  [ "$after" != "$before" ] && good "an upload reached the hub at $after (queue left on the phone: $(widget | json "d['iphone_upload']['queue_remaining']"))" \
    || die "no upload yet: is the URL right? Tailscale on the phone? (rollback: scripts/cutover.sh --rollback)"
}

# --- step 7: smoke test ----------------------------------------------------------------------------------------

step7() {
  say "7. Smoke test: one real segment arrives and the Mac transcribes it"
  info "Say a few sentences to the pendant now. A session closes 10 minutes after its last segment; then the Mac transcribes it."
  confirm "Recorded something and ready to wait (up to 30 minutes)?"
  [ "$DRY" = 1 ] && return 0
  local newest start i found=""
  newest="$(hub "ls -t $HUB_DATA/raw/audio/*/*.bin | head -1")"
  info "newest segment on the hub: $(basename "$newest")"
  start="$(date +%s)"
  api -X POST /v1/transcribe -o /dev/null
  for i in $(seq 1 90); do
    found="$(hub "find $HUB_DATA/transcripts -name '*.json' -newer '$newest' -not -path '*/speakers/*' | head -1")"
    [ -n "$found" ] && break
    widget | json "'   ' + str(d['queue']['by_kind']) + ' mac ' + ('online' if d['mac']['online'] else 'OFF') + ' ' + str(d['mac']['state'])"
    sleep 20
  done
  [ -n "$found" ] || die "no transcript yet after 30 minutes: see the widget Omi on the dashboard and $HOME/Library/Logs/local-omy-compute.log"
  good "transcribed by the Mac and committed on the hub: $(basename "$found") after $(( ($(date +%s) - start) / 60 )) min"
  hub "python3 -c \"import json;d=json.load(open('$found'));print('   pipeline',d['pipeline'],'segments',len(d['segments']),'speech lines',len(d['speech']))\""
}

# --- step 8: after -------------------------------------------------------------------------------------------------

step8() {
  say "8. Done. What is left to you"
  cat <<EOF
   - Night wake of the Mac (needs sudo, not run here):   sudo pmset repeat wakeorpoweron MTWRFSU 00:55:00
     check with: pmset -g sched      (leave the Mac on its charger, lid open or a display attached, at night)
   - Keep ~/OmiArchive as the cold copy for 4 WEEKS (until $(date -v+28d +%F 2>/dev/null || echo "four weeks from today")): do not delete it, do not run the old agents.
   - Watch for 24-48 h: the Omi widget on the dashboard, a night with the organizer (01-07), the 03:30 backup, 'sudo systemctl start hub-restore-test'.
   - Rollback is still possible: scripts/cutover.sh --rollback --with-new-data
EOF
}

# --- rollback --------------------------------------------------------------------------------------------------------

rollback() {
  say "ROLLBACK: back to the Mac"
  info "1) the hub stops receiving and working (data is frozen); 2) the Mac's compute worker stops; 3) the Mac's own receiver and worker start again; 4) you change the iPhone's URL back."
  [ "$WITH_DATA" = 1 ] && info "With --with-new-data: what the hub received and made since the cutover is copied back into ~/OmiArchive (no deletions)."
  confirm "Roll back now?"
  run hub_compose stop omy
  hub_sudo tailscale serve --https=10000 off
  run omy --json setup fix compute-off
  run omy --json setup fix vault-mirror-off
  run omy --json setup hub --off
  if [ "$WITH_DATA" = 1 ]; then
    info "Copying back (the files with the same name keep the newer version, the audio is identical by construction)..."
    run bash -c "rsync -a --checksum -e 'ssh -o BatchMode=yes' $(printf "'$HUB:$HUB_DATA/%s' " "${RSYNC_DIRS[@]}" "${RSYNC_FILES[@]}") '$ARCH/'"
    info "The hub's transcripts for days that changed are re-rendered by the Mac's worker; the index is rebuilt:"
    run omy --json index --all
  fi
  for a in receiver worker; do
    run launchctl enable "gui/$UIDN/$(launch_label "$a")"
    run launchctl bootstrap "gui/$UIDN" "$HOME/Library/LaunchAgents/$(launch_label "$a").plist"
  done
  run tailscale serve --bg http://127.0.0.1:8787
  [ "$DRY" = 1 ] && return 0
  sleep 3
  local code
  code="$(printf 'header = "Authorization: Bearer %s"\n' "$(cat "$CFG/token")" | curl -sS -m 30 --config - -o /dev/null -w '%{http_code}' "$MAC_URL/v1/transcripts")"
  [ "$code" = 200 ] && good "the Mac answers on $MAC_URL" || bad "the Mac answered '$code' on $MAC_URL"
  info "Now on the iPhone: Impostazioni -> URL del Mac:  $MAC_URL   (the segments waiting on the phone go up)."
  info "The hub's omy stays stopped; its data stays where it is (nothing is deleted)."
}

# --- main ----------------------------------------------------------------------------------------------------------------

[ "$DRY" = 1 ] && info "DRY RUN: nothing is changed"
if [ "$ROLLBACK" = 1 ]; then rollback; exit 0; fi
for n in 0 1 2 3 4 5 6 7 8; do
  step_selected "$n" && "step$n"
done
