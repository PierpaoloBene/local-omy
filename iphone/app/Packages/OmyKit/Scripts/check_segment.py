"""Checks a .bin/.json pair written by OmyKit against the Mac receiver's own parsers.

Usage (from mac/src): uv run --offline python <this file> SEGMENT.bin SEGMENT.json
Exit 0 and prints "ok <frames> <codec>" if omy_receiver.segment parses the .bin without trailing bytes and
omy_receiver.app._parse_meta accepts the sidecar exactly as the X-Segment-Meta header carries it.
"""

import base64
import hashlib
import sys
from pathlib import Path

from omy_receiver import app, segment

bin_path, json_path = Path(sys.argv[1]), Path(sys.argv[2])
data = bin_path.read_bytes()
meta_bytes = json_path.read_bytes()
seg = segment.parse(data)
assert seg.trailing_bytes == 0, f"trailing bytes: {seg.trailing_bytes}"
sha = hashlib.sha256(data).hexdigest()
raw = base64.urlsafe_b64encode(meta_bytes).decode("ascii")
meta = app._parse_meta(raw, bin_path.stem, sha)
assert meta["frame_count"] == len(seg.records), "frame_count mismatch"
assert meta["codec"] == seg.codec, "codec mismatch"
print("ok", len(seg.records), seg.codec)
