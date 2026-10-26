"""The Pennsylvania-style license plate (the user, 2026-10-26: "make a PA license plate model for the game and add it to
the license plate options"): a 30 x 15 cm (US) plate design for BeamNG's plate system (Skia template, version 3): a
white face with a dark blue band and PENNSYLVANIA across the top, a yellow band along the bottom, and the number in
dark blue between (drawn by the game from the template, so any plate text works). Writes the background, the template
and the part file into mod/beamng_earth/vehicles/common/licenseplates/bne_pennsylvania/.
"""
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate_kmh_speed_signs as kmh  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "bne_pennsylvania"
OUT = os.path.join(ROOT, "mod", "beamng_earth", "vehicles", "common", "licenseplates", NAME)
W, H = 512, 256
BLUE, YELLOW, WHITE, INK = (20, 40, 98), (247, 196, 31), (250, 250, 247), (22, 42, 104)


def background():
    s = 4                                           # drawn large, scaled down: smooth edges
    im = Image.new("RGBA", (W * s, H * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, W * s - 1, H * s - 1], radius=22 * s, fill=WHITE)
    face = Image.new("L", im.size, 0)
    ImageDraw.Draw(face).rounded_rectangle([0, 0, W * s - 1, H * s - 1], radius=22 * s, fill=255)
    bands = Image.new("RGBA", im.size, (0, 0, 0, 0))
    b = ImageDraw.Draw(bands)
    b.rectangle([0, 0, W * s, 58 * s], fill=BLUE)                   # top band
    b.rectangle([0, (H - 34) * s, W * s, H * s], fill=YELLOW)       # bottom band
    b.rectangle([0, (H - 38) * s, W * s, (H - 34) * s], fill=BLUE)  # its thin blue edge
    im.paste(bands, (0, 0), Image.composite(bands, Image.new("RGBA", im.size), face))
    f = kmh.choose_font(40 * s)
    t = "PENNSYLVANIA"
    tw = d.textlength(t, font=f)
    d.text(((W * s - tw) / 2, 8 * s), t, font=f, fill=WHITE)
    for x in (70, W - 70):                                         # bolt holes
        for y in (36, H - 22):
            d.ellipse([(x - 9) * s, (y - 6) * s, (x + 9) * s, (y + 6) * s], fill=(160, 160, 160))
    d.rounded_rectangle([3 * s, 3 * s, (W - 4) * s, (H - 4) * s], radius=20 * s, outline=(120, 124, 132), width=3 * s)
    im = im.resize((W, H), Image.LANCZOS)
    return im.filter(ImageFilter.SMOOTH)


def template():
    return {
        "name": NAME, "version": 3, "type": "licenseplate",
        "vars": {"plate": {"type": "string", "default": "{L}{L}{L}-{d}{d}{d}{d}",
                           "charsets": {"L": "ABCDEFGHJKLMNPRSTUVWXYZ", "d": "0123456789"}, "maxLength": 8}},
        "format": {"30-15": {
            "size": [W, H],
            "background": f"vehicles/common/licenseplates/{NAME}/{NAME}_background_d.png",
            "emboss": 3.0,
            "finish": {"roughness": 0.35, "metallic": 0.1, "fgRoughness": 0.3, "fgMetallic": 0.2},
            "root": {"style": {"width": "100%", "height": "100%", "alignItems": "center", "justifyContent": "center",
                               "paddingTop": 34, "paddingBottom": 30},
                     "children": [{"type": "text", "name": "plate", "text": "{plate}", "font": "plate", "fontSize": 92,
                                   "fit": "shrink", "color": "#162a68", "letterSpacing": 2,
                                   "style": {"width": "92%", "height": 120}}]}}},
    }


def part():
    return {f"{NAME}_2_1": {
        "information": {"authors": "BeamNG.Earth", "name": "Pennsylvania (BeamNG.Earth)", "value": 50},
        "slotType": "licenseplate_design_2_1",
        "licenseplate_path": f"vehicles/common/licenseplates/{NAME}/{NAME}.sktemplate.json"}}


def main():
    os.makedirs(OUT, exist_ok=True)
    background().save(os.path.join(OUT, f"{NAME}_background_d.png"))
    with open(os.path.join(OUT, f"{NAME}.sktemplate.json"), "w", encoding="utf-8") as fh:
        json.dump(template(), fh, indent=2)
    with open(os.path.join(OUT, f"{NAME}-2-1.jbeam"), "w", encoding="utf-8") as fh:
        json.dump(part(), fh, indent=2)
    print("plate:", OUT)


if __name__ == "__main__":
    main()
