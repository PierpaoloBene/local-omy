#!/bin/bash
# The vault of the hub (the master) -> ~/OmiBrain, READ-ONLY copy for Obsidian. Run every 10 minutes by the
# `vault-mirror` LaunchAgent. Anything changed in ~/OmiBrain is overwritten, and what was deleted on the hub is
# deleted here (--delete), except Obsidian's own folders (.obsidian keeps your plugins and layout).
#   HUB (default hub@hub.local), SRC (default /srv/hub/data/omy/brain/), DEST (default ~/OmiBrain/)
set -euo pipefail
HUB="${HUB:-hub@hub.local}"
SRC="${SRC:-/srv/hub/data/omy/brain/}"
DEST="${DEST:-$HOME/OmiBrain/}"
mkdir -p "$DEST"
# BatchMode: never ask for a password; the hub off or away is just a failed run (nothing is deleted then).
# An empty or missing vault on the hub must never empty this copy.
ssh -o BatchMode=yes -o ConnectTimeout=10 "$HUB" "[ -d '$SRC' ] && [ -n \"\$(ls -A '$SRC')\" ]" \
    || { echo "$(date '+%F %T') the hub has no vault at $SRC: $DEST left as it is" >&2; exit 1; }
# The files are made read-only after each run (the system rsync does not know --chmod), so Obsidian does not let
# you edit what the next run overwrites; the folders stay writable for rsync, which replaces files by renaming.
rsync -a --delete \
    --exclude='.obsidian/' --exclude='.trash/' \
    -e "ssh -o BatchMode=yes -o ConnectTimeout=10" \
    "$HUB:$SRC" "$DEST"
find "$DEST" -type f -not -path '*/.obsidian/*' -not -path '*/.trash/*' -exec chmod a-w {} +
echo "$(date '+%F %T') vault mirrored from $HUB:$SRC"
