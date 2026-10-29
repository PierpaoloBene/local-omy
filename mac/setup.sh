#!/bin/bash
# The Mac side of local-omy from a fresh clone: checks what is missing, fixes
# what can be fixed without you, says what needs you. Safe to run again.
# Details: mac/README.md, "Installazione da zero".
set -euo pipefail
cd "$(dirname "$0")/src"

need() {
    command -v "$1" >/dev/null 2>&1 && return
    echo "Manca $1: $2" >&2
    exit 1
}
need xcodebuild "installa Xcode dall'App Store, aprilo una volta e accedi con il tuo Apple ID"
need brew "installa Homebrew da https://brew.sh"
need uv "curl -LsSf https://astral.sh/uv/install.sh | sh"
need flutter "brew install --cask flutter"

uv sync --frozen --quiet

# What omy can fix on its own; the rest needs you (printed at the end).
for item in brew token receiver periodic tailscale models; do
    if ! uv run --quiet omy --json setup check | python3 -c "
import json, sys
sys.exit(0 if next(c for c in json.load(sys.stdin)['checks'] if c['id'] == '$item')['ok'] else 1)"; then
        echo "→ omy setup fix $item"
        uv run --quiet omy setup fix "$item" || echo "  non riuscito: vedi sopra" >&2
    fi
done

./app/build.sh
echo
uv run --quiet omy setup check
cat <<'TXT'

Ancora da fare a mano, se nella lista sopra non è "True":
  diarize  token Hugging Face in ~/.config/local-omy/hf_token (mode 600), poi
           omy setup fix diarize (mac/README.md, Chi parla)
  iphone   iPhone collegato e sbloccato, Apple ID in Xcode, poi
           omy setup fix iphone && omy iphone install
Poi apri mac/build/local-omy.app: Impostazioni → Copia URL / Copia token,
e incollali nell'app iPhone (Impostazioni → Mac).
TXT
