#!/usr/bin/env python3
"""The guide signs' atlas (the user, 2026-10-25: big green overhead signs, Pennsylvania's general highway guide signs):
one texture with every glyph the signs write (white, and black for the US route shield's numbers), the route shields
without numbers (Interstate, US, state), the arrows, and solid blocks for the green panel and its white border, so a
road tile's signs are one mesh with one material whatever they say (server guidesigns.py lays the text out from
guide_atlas.json's metrics).
    python3 tools/generate_guide_atlas.py
Writes guide_atlas.png, guide_atlas.json and guide.materials.json in the mod's shapes folder.
"""
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_kmh_speed_signs as kmh  # noqa: E402

OUT = kmh.OUT
TEX_DIR = kmh.TEX_DIR
W, H = 2048, 1024
CELL = 96                       # px: a glyph cell's height (ascent + descent)
FONT_PX = 76
CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.,-/&'()+:"
GREEN = (0, 112, 60, 255)       # MUTCD guide green
WHITE = (255, 255, 255, 255)
BLACK = (17, 17, 17, 255)
NAVY = (29, 59, 110, 255)    # the Welcome to ... County sign
BLUE, RED = (31, 78, 163, 255), (200, 16, 46, 255)
SPRITE = 192                    # px: a shield / arrow sprite's square


def main():
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    f = kmh.choose_font(FONT_PX)
    asc, desc = f.getmetrics()
    meta = {"size": [W, H], "cell": CELL, "glyphs": {"white": {}, "black": {}}, "sprites": {}}
    x, y = 4, 4

    def place(w, h):
        nonlocal x, y
        if x + w + 4 > W:
            x, y = 4, y + h + 6
        r = (x, y, x + w, y + h)
        x += w + 6
        return r
    for colour, fill in (("white", WHITE), ("black", BLACK)):
        x, y = 4, y if colour == "white" else y + CELL + 10
        for ch in CHARS:
            adv = int(round(d.textlength(ch, font=f)))
            r = place(adv + 4, CELL)
            d.text((r[0] + 2, r[1] + (CELL - asc - desc) // 2), ch, font=f, fill=fill)
            meta["glyphs"][colour][ch] = {"r": r, "adv": adv + 4}
        meta["glyphs"][colour][" "] = {"r": None, "adv": int(FONT_PX * 0.32)}
    x, y = 4, y + CELL + 12

    def sprite(name):
        r = place(SPRITE, SPRITE)
        meta["sprites"][name] = r
        return r
    # solid blocks (inset sampling keeps their edges clean)
    for name, col in (("green", GREEN), ("white", WHITE), ("black", BLACK), ("navy", NAVY)):
        r = sprite(name)
        d.rectangle(r, fill=col)
    # Interstate shield (no number: the numbers are white glyphs over it)
    r = sprite("interstate")
    x0, y0, x1, y1 = r
    s = SPRITE / 150.0

    def P(px, py):
        return (x0 + px * s, y0 + py * s)
    d.polygon([P(10, 18), P(40, 8), P(75, 4), P(110, 8), P(140, 18), P(140, 70), P(120, 120), P(75, 146), P(30, 120),
               P(10, 70)], fill=WHITE)
    d.polygon([P(16, 23), P(43, 14), P(75, 10), P(107, 14), P(134, 23), P(134, 70), P(116, 116), P(75, 139), P(34, 116),
               P(16, 70)], fill=BLUE)
    d.polygon([P(16, 23), P(43, 14), P(75, 10), P(107, 14), P(134, 23), P(134, 48), P(16, 48)], fill=RED)
    # US route shield (black square, white shield; black numbers over it)
    r = sprite("us")
    x0, y0, x1, y1 = r
    d.rectangle(r, fill=BLACK)
    d.polygon([P(22, 16), P(128, 16), P(126, 30), P(136, 38), P(130, 62), P(134, 84), P(134, 104), P(75, 138),
               P(16, 104), P(16, 84), P(20, 62), P(14, 38), P(24, 30)], fill=WHITE)
    # state route (the generic circle: black square, white circle; black numbers)
    r = sprite("state")
    x0, y0, x1, y1 = r
    d.rectangle(r, fill=BLACK)
    d.ellipse(P(12, 12) + P(138, 138), fill=WHITE)
    # arrows: up (through) and up-right (exit), white on transparent
    r = sprite("arrow_up")
    x0, y0, x1, y1 = r
    d.polygon([P(75, 8), P(130, 70), P(95, 70), P(95, 142), P(55, 142), P(55, 70), P(20, 70)], fill=WHITE)
    r = sprite("arrow_exit")
    x0, y0, x1, y1 = r
    d.polygon([P(140, 10), P(140, 90), P(112, 62), P(42, 132), P(18, 108), P(88, 38), P(60, 10)], fill=WHITE)
    im.save(OUT / "guide_atlas.png")
    (OUT / "guide_atlas.json").write_text(json.dumps(meta, separators=(",", ":")), encoding="utf-8")
    mats = {
        "bne_guide_atlas": {"name": "bne_guide_atlas", "mapTo": "bne_guide_atlas", "class": "Material",
                            "Stages": [{"colorMap": f"{TEX_DIR}/guide_atlas.png", "diffuseColor": [1, 1, 1, 1],
                                        "specular": [0.35, 0.35, 0.35, 1.0], "specularPower": 24,
                                        "pixelSpecular": True}, {}, {}, {}],
                            "alphaTest": True, "alphaRef": 127, "doubleSided": False},
        "bne_guide_steel": {"name": "bne_guide_steel", "mapTo": "bne_guide_steel", "class": "Material",
                            "Stages": [{"diffuseColor": [0.58, 0.6, 0.62, 1.0], "specular": [0.6, 0.62, 0.65, 1.0],
                                        "specularPower": 40, "pixelSpecular": True}, {}, {}, {}],
                            "doubleSided": False},
    }
    (OUT / "guide.materials.json").write_text(json.dumps(mats, indent=2), encoding="utf-8")
    print(f"atlas: {len(CHARS)} glyphs x 2, {len(meta['sprites'])} sprites, rows to y={y + SPRITE}")


if __name__ == "__main__":
    main()
