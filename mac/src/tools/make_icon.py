"""The app icons: eight black dots on a circle, on white.

  uv run python tools/make_icon.py

writes the iPhone icons (iphone/src/ios/Runner/Assets.xcassets/AppIcon.appiconset,
full square: iOS rounds them) and the Mac one (app/AppIcon.icns: a rounded
square with margin and shadow, as macOS does not round icons).
"""

import json
import math
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[3]
IOS = ROOT / "iphone/src/ios/Runner/Assets.xcassets/AppIcon.appiconset"
MAC = ROOT / "mac/src/app/AppIcon.icns"
S = 4096


def dots(img: Image.Image, center: float, scale: float) -> None:
    d = ImageDraw.Draw(img)
    ring, dot = 0.245 * scale, 0.046 * scale
    for k in range(8):
        a = math.pi / 2 - k * math.pi / 4
        x, y = center + ring * math.cos(a), center - ring * math.sin(a)
        d.ellipse((x - dot, y - dot, x + dot, y + dot), fill="black")


def ios() -> None:
    img = Image.new("RGB", (S, S), "white")
    dots(img, S / 2, S)
    for i in json.loads((IOS / "Contents.json").read_text())["images"]:
        px = round(float(i["size"].split("x")[0]) * int(i["scale"][0]))
        img.resize((px, px), Image.LANCZOS).save(IOS / i["filename"])


def mac() -> None:
    # Apple's grid: an 824/1024 rounded square, radius ~185/1024.
    box, radius = 0.805 * S, 0.18 * S
    o = (S - box) / 2
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((o, o + 0.012 * S, o + box, o + box + 0.012 * S), radius,
                                             fill=(0, 0, 0, 90))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(0.012 * S)))
    ImageDraw.Draw(img).rounded_rectangle((o, o, o + box, o + box), radius, fill="white")
    dots(img, S / 2, box)
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for px in (16, 32, 128, 256, 512):
            img.resize((px, px), Image.LANCZOS).save(iconset / f"icon_{px}x{px}.png")
            img.resize((px * 2, px * 2), Image.LANCZOS).save(iconset / f"icon_{px}x{px}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(MAC)], check=True)


if __name__ == "__main__":
    ios()
    mac()
    print(f"wrote {IOS} and {MAC}")
