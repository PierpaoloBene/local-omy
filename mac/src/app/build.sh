#!/bin/bash
# Build mac/build/local-omy.app (ad-hoc signed, not sandboxed: it runs uv/omy).
set -euo pipefail
cd "$(dirname "$0")"
swift build -c release --product LocalOmy
APP=../../build/local-omy.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$(swift build -c release --show-bin-path)/LocalOmy" "$APP/Contents/MacOS/LocalOmy"
cp Info.plist "$APP/Contents/Info.plist"
codesign --force --sign - "$APP"
echo "built $(cd "$(dirname "$APP")" && pwd)/local-omy.app"
