#!/usr/bin/env python3
"""build_bg_sign_glyphs.py: the lettering atlas of Bulgaria's Д11 / Д12 town signs (server/bgsigns.py) from the bundled
Roboto Condensed (tools/fonts/RobotoCondensed-wght.ttf, SIL OFL 1.1: tools/fonts/RobotoCondensed-OFL.txt; Google Fonts'
ofl/robotocondensed), at its Medium weight.

    python3 tools/build_bg_sign_glyphs.py

The signs' own lettering is БДС 1517's (not open); Наредба № РД-02-21-1 от 2023's drawing of Д11 (annex 5) shows a
DIN 1451-like grotesque with the standard Cyrillic letter forms, which Roboto Condensed has (Sofia Sans, the other open
candidate, draws the Bulgarian forms: т like m). Writes server/bg_sign_glyphs.json (as tools/build_sign_glyphs.py's
sign_glyphs.json: per character advance, bbox [l, t, r, b] px from the pen on the baseline, uv [u0, v0, u1, v1]) and
the opacity atlas mod/.../roadside/bg_letters_o.data.png (material bne_bg_letters_black, exit.materials.json).
Then run tools/mod_textures_dds.py on that PNG: the game loads only DDS (its source is kept as
mod/texture_sources/.../bg_letters_o.data.webp and made into bg_letters_o.data.dds at install).
"""
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT = os.path.join(ROOT, "tools", "fonts", "RobotoCondensed-wght.ttf")
WEIGHT = b"Medium"
ATLAS = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth",
                     "roadside", "bg_letters_o.data.png")
METRICS = os.path.join(ROOT, "server", "bg_sign_glyphs.json")
SIZE = (1024, 1024)
PX = 96
PAD = 6
# Bulgarian Cyrillic (with Ѝ ѝ and the Ы Э of foreign names), the Latin the Transliteration Act uses, figures and the
# punctuation of place names
CHARS = ("АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЬЮЯабвгдежзийклмнопрстуфхцчшщъьюяЍѝЫыЭэЁё"
         "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 -.,'()\"„“")


def build(font_path=FONT, size=SIZE, px=PX):
    font = ImageFont.truetype(font_path, px)
    font.set_variation_by_name(WEIGHT)
    ascent, descent = font.getmetrics()
    row = ascent + descent + PAD
    sw, sh = size
    image = Image.new("L", (sw, sh), 0)
    draw = ImageDraw.Draw(image)
    glyphs = {}
    x, y = PAD, PAD
    for c in CHARS:
        l, t, r, b = font.getbbox(c, anchor="ls")
        w = r - l
        if x + w + PAD > sw:
            x, y = PAD, y + row
        if y + row > sh:
            raise ValueError("atlas full: lower PX")
        if w > 0 and b > t:
            top = y + ascent + t
            draw.text((x - l, y + ascent), c, font=font, fill=255, anchor="ls")
            uv = [x / sw, 1 - (top + b - t) / sh, (x + w) / sw, 1 - top / sh]
            x += w + PAD
        else:
            uv = [0.0, 0.0, 0.0, 0.0]
        glyphs[c] = {"advance": round(font.getlength(c), 3), "bbox": [l, t, r, b], "uv": [round(v, 6) for v in uv]}
    cap = -font.getbbox("H", anchor="ls")[1]
    meta = {"font": "Roboto Condensed", "weight": WEIGHT.decode(), "license": "OFL-1.1",
            "source": "https://github.com/google/fonts/tree/main/ofl/robotocondensed", "capHeight": cap,
            "glyphs": glyphs}
    return image, meta


def main():
    image, meta = build()
    os.makedirs(os.path.dirname(ATLAS), exist_ok=True)
    image.save(ATLAS, optimize=True)
    with open(METRICS, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(meta, separators=(",", ":"), ensure_ascii=False, sort_keys=True) + "\n")
    print(f"{len(meta['glyphs'])} glyphs, cap height {meta['capHeight']} px -> {ATLAS}, {METRICS}")


if __name__ == "__main__":
    sys.exit(main())
