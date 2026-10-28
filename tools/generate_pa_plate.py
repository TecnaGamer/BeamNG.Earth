"""The Pennsylvania-style license plate (the user, 2026-10-26: "make a PA license plate model for the game and add it to
the license plate options"): a 30 x 15 cm (US) plate design for BeamNG's plate system (Skia template, version 3): as the
real one looks: a white reflective face, a navy band with PENNSYLVANIA across the top, a yellow band with the web
address along the bottom, and the number in navy with the keystone between the letters and the digits (drawn by the game from the template, so any plate text works). Writes the background, the template
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
    face = Image.new("L", im.size, 0)
    ImageDraw.Draw(face).rounded_rectangle([0, 0, W * s - 1, H * s - 1], radius=18 * s, fill=255)
    art = Image.new("RGBA", im.size, WHITE)
    d = ImageDraw.Draw(art)
    for y in range(H * s):                          # a faint sheen down the face (reflective sheeting)
        k = 1.0 - 0.06 * abs(y / (H * s) - 0.35)
        d.line([(0, y), (W * s, y)], fill=tuple(int(c * k) for c in WHITE) + (255,))
    d.rectangle([0, 0, W * s, 50 * s], fill=BLUE)                       # the navy band, PENNSYLVANIA
    d.rectangle([0, (H - 40) * s, W * s, H * s], fill=YELLOW)           # the yellow band, the web address
    d.rectangle([0, (H - 43) * s, W * s, (H - 40) * s], fill=BLUE)
    f = kmh.choose_font(34 * s)
    t, track = "PENNSYLVANIA", 5 * s                                     # letter-spaced, between the bolt holes
    tw = sum(d.textlength(c, font=f) for c in t) + track * (len(t) - 1)
    x = (W * s - tw) / 2
    for c in t:
        d.text((x, 6 * s), c, font=f, fill=WHITE)
        x += d.textlength(c, font=f) + track
    f2 = kmh.choose_font(26 * s)
    t2 = "visitPA.com"
    d.text(((W * s - d.textlength(t2, font=f2)) / 2, (H - 38) * s), t2, font=f2, fill=BLUE)
    for x in (40, W - 40):                                              # bolt holes (slots, as stamped)
        for y in (25, H - 20):
            d.rounded_rectangle([(x - 12) * s, (y - 6) * s, (x + 12) * s, (y + 6) * s], radius=6 * s,
                                fill=(70, 72, 78))
    for i, col in enumerate(((150, 154, 162), (225, 228, 232))):        # the stamped rim: shade, then light
        o = (4 + i * 2) * s
        d.rounded_rectangle([o, o, W * s - 1 - o, H * s - 1 - o], radius=15 * s, outline=col, width=2 * s)
    im.paste(art, (0, 0), face)
    return im.resize((W, H), Image.LANCZOS)


def keystone():
    """The keystone between the letters and the numbers (navy, on transparent)."""
    s, w, h = 4, 40, 48
    im = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # Pennsylvania's keystone: wide at the top, narrowing to the base, the top corners clipped
    pts = [(4, 2), (36, 2), (40, 8), (32, 46), (8, 46), (0, 8)]
    d.polygon([(x * s, y * s) for x, y in pts], fill=INK)
    return im.resize((w, h), Image.LANCZOS)


def template():
    text = {"type": "text", "font": "plate", "fontSize": 104, "fit": "shrink", "color": "#16295f", "letterSpacing": 3,
            "style": {"height": 118}}
    return {
        "name": NAME, "version": 3, "type": "licenseplate",
        "vars": {"plate": {"type": "string", "default": "{L}{L}{L}{d}{d}{d}{d}",
                           "charsets": {"L": "ABCDEFGHJKLMNPRSTUVWXYZ", "d": "0123456789"}, "maxLength": 8}},
        "format": {"30-15": {
            "size": [W, H],
            "background": f"vehicles/common/licenseplates/{NAME}/{NAME}_background_d.png",
            "emboss": 4.0, "limit": 3, "limit2": 5,
            "finish": {"roughness": 0.3, "metallic": 0.15, "fgRoughness": 0.25, "fgMetallic": 0.25},
            "root": {"style": {"width": "100%", "height": "100%", "flexDirection": "row", "alignItems": "center",
                               "justifyContent": "center", "paddingTop": 26, "paddingBottom": 30, "gap": 8},
                     "children": [
                         dict(text, name="left", text="{line1}"),
                         {"type": "image", "name": "keystone", "src": f"vehicles/common/licenseplates/{NAME}/{NAME}_keystone.png",
                          "style": {"width": 40, "height": 48}},
                         dict(text, name="right", text="{line2}")]}}},
    }


def part():
    return {f"{NAME}_2_1": {
        "information": {"authors": "BeamNG.Earth", "name": "Pennsylvania (BeamNG.Earth)", "value": 50},
        "slotType": "licenseplate_design_2_1",
        "licenseplate_path": f"vehicles/common/licenseplates/{NAME}/{NAME}.sktemplate.json"}}


def main():
    os.makedirs(OUT, exist_ok=True)
    background().save(os.path.join(OUT, f"{NAME}_background_d.png"))
    keystone().save(os.path.join(OUT, f"{NAME}_keystone.png"))
    with open(os.path.join(OUT, f"{NAME}.sktemplate.json"), "w", encoding="utf-8") as fh:
        json.dump(template(), fh, indent=2)
    with open(os.path.join(OUT, f"{NAME}-2-1.jbeam"), "w", encoding="utf-8") as fh:
        json.dump(part(), fh, indent=2)
    print("plate:", OUT)


if __name__ == "__main__":
    main()
