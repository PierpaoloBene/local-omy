#!/bin/bash
# Build mac/build/local-omy.app (ad-hoc signed, not sandboxed: it runs uv/omy).
set -euo pipefail
cd "$(dirname "$0")"
swift build -c release --product LocalOmy
APP=../../build/local-omy.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$(swift build -c release --show-bin-path)/LocalOmy" "$APP/Contents/MacOS/LocalOmy"
cp Info.plist "$APP/Contents/Info.plist"
# Made by tools/make_icon.py; shown in Finder and Spotlight.
cp AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
codesign --force --sign - "$APP"
echo "built $(cd "$(dirname "$APP")" && pwd)/local-omy.app"
