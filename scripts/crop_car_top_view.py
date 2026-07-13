#!/usr/bin/env python3
"""Crop a top-down car screenshot into a background image for volvo-car-card.

The card (`custom:volvo-car-card`) draws its door/window/lock overlays at fixed
positions tuned to a top-view render where the car is centred, front-up, and
fills ~83% of the canvas height. Screenshots taken from the Volvo app (with the
background removed) usually have the car small and off-centre, so they need to
be re-placed to line up with the overlays.

This script trims a transparent-background PNG to the car, then re-centres and
scales it onto a canvas matching the card's aspect ratio (1248 / 2687).

Usage:
    python scripts/crop_car_top_view.py INPUT.png OUTPUT.webp

Then copy OUTPUT.webp into Home Assistant's `www/` and set it on the card:
    image: /local/OUTPUT.webp

Requires Pillow (`pip install Pillow`).
"""
import argparse
from PIL import Image

OUT_W, OUT_H = 844, 1816   # ~0.4647, same aspect as the card canvas (1248/2687)
CAR_H_FRAC = 0.83          # car height as a fraction of the canvas
TOP_FRAC = 0.113           # top margin as a fraction of the canvas
ALPHA_THRESHOLD = 24       # ignore faint halo pixels when locating the car


def crop(src: str, dst: str) -> None:
    im = Image.open(src).convert("RGBA")
    mask = im.split()[3].point(lambda p: 255 if p > ALPHA_THRESHOLD else 0)
    bbox = mask.getbbox()
    if bbox is None:
        raise SystemExit(f"{src}: no non-transparent pixels found (is the "
                         "background removed?)")
    car = im.crop(bbox)
    cw, ch = car.size
    target_h = int(CAR_H_FRAC * OUT_H)
    target_w = max(1, round(cw * target_h / ch))
    car = car.resize((target_w, target_h), Image.LANCZOS)
    canvas = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    canvas.paste(car, ((OUT_W - target_w) // 2, int(TOP_FRAC * OUT_H)), car)
    if dst.lower().endswith(".webp"):
        canvas.save(dst, "WEBP", quality=90, method=6)
    else:
        canvas.save(dst)
    print(f"{dst}: car {cw}x{ch} -> {target_w}x{target_h} on {OUT_W}x{OUT_H}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="transparent-background top-down car PNG")
    ap.add_argument("output", help="output image (.webp recommended, or .png)")
    args = ap.parse_args()
    crop(args.input, args.output)
