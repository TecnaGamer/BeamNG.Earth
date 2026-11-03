"""The textures of Sweden's signs: Transportstyrelsen's own artwork on two sheets, bne_se_signs.png (C31, B1) and
bne_se_signs2.png (the signs of server/sesigns.py).

    python tools/build_swedish_signs.py <folder of the downloaded PNGs: c31-3.png ... c31-12.png, b1-1.png,
                                         f17-1.png, f18-2.png, f18-5.png, f18-6.png, t2-1.png, c1-1.png, b3-1.png,
                                         e-11.png, e2-1.png, f2-1.png>

The artwork is the agency's PNGs of the signs (transportstyrelsen.se, vagmarken: C31 30 to 120 km/h as c31-3 to c31-12,
B1 as b1-1; fetched 2026-11-02), drawn in its Tratex. Each stands centred in a cell of 256 px on the signs' red, which
is what a cell's edge and its mipmaps then bleed into. The cells and their scale are server/speedsigns.py's C31_*
and tools/build_median_props.py's B1_* and SE_SIGNS.

Two of the second sheet's signs are not the agency's picture as it stands. F2-1 is an example, "50 FALUN" and "BODA"
on it: the place names are painted out, the junction's diagram stays, since no sign here knows its junction's names. T2
exists as "100 m" only: its plate, its m and the digits of the C31 signs (the same Tratex) make 200, 300 and 400 m.
"""
import os
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth",
                   "bne_se_signs.png")
CELL, ART = 256, 244    # px: a cell, the artwork's longer side in it
RED = (238, 58, 66)     # the artwork's #EE3A42
COLOURS = 48            # enough for the anti-aliased edges; keeps the sheet to some 70 KB


# bne_se_signs2.png: name, its picture, the cell's middle in px (of 1024), the longer side drawn. tools/build_median_props
# .py's SE_SIGNS reads this.
F2_TEXT = ((30, 15, 525, 128), (20, 225, 272, 325))     # px of f2-1.png: the place names
F2_DIAGRAM = (270, 140, 520, 540)                       # ... and its junction
SHEET2 = (("f17", "f17-1.png", (128, 128), 244), ("f18_11", "f18-2.png", (384, 128), 244),
          ("f18_12", "f18-5.png", (640, 128), 244), ("f18_21", "f18-6.png", (896, 128), 244),
          ("t400", "t400", (128, 384), 244), ("t300", "t300", (384, 384), 244), ("t200", "t200", (640, 384), 244),
          ("c1", "c1-1.png", (896, 384), 244), ("b3", "b3-1.png", (128, 640), 244), ("e1", "e-11.png", (384, 640), 244),
          ("e2", "e2-1.png", (128, 896), 244), ("f2", "f2-1.png", (768, 768), 500))


def _on_white(im):
    """im (RGBA) over white: its transparent corners read as nothing, not as black."""
    out = Image.new("RGB", im.size, (255, 255, 255))
    out.paste(im, (0, 0), im)
    return out


def _ink(im, below=90):
    """A picture's near-black pixels as a mask ("L"): a C31's digits, whatever colour is round them."""
    return im.convert("L").point(lambda v: 255 if v < below else 0)


def _boxes(mask, gap=3):
    """The (x0, x1) columns of mask's runs of ink, split where gap columns have none: its characters, left to right."""
    w, h = mask.size
    px = mask.load()
    cols = [any(px[x, y] for y in range(h)) for x in range(w)]
    out, x = [], 0
    while x < w:
        if not cols[x]:
            x += 1
            continue
        a = x
        while x < w and (cols[x] or any(cols[x:x + gap])):
            x += 1
        out.append((a, x))
    return out


def _digit(src, name, k):
    """Character k of a C31 sign's number as a tight mask (the ring is red: _ink leaves it out)."""
    m = _ink(_on_white(Image.open(os.path.join(src, name)).convert("RGBA")))
    a, b = _boxes(m)[k]
    col = m.crop((a, 0, b, m.height))
    return col.crop(col.getbbox())


T2_DIGITS = {2: ("c31-12.png", 1), 3: ("c31-3.png", 0), 4: ("c31-4.png", 0)}    # (a C31 of 20 km/h there is not)
ZERO = ("c31-3.png", 1)


def _plate(src, metres):
    """T2 with metres on it: the agency's "100 m" plate, its outlined characters filled, the 1 and two 0s replaced."""
    base = _on_white(Image.open(os.path.join(src, "t2-1.png")).convert("RGBA"))
    w, h = base.size
    inner = (int(w * 0.07), int(h * 0.16), int(w * 0.93), int(h * 0.84))
    ink = _ink(base.crop(inner), 235)               # (the artwork's characters are thin outlines)
    chars = _boxes(ink, 6)
    tall = ink.crop((chars[0][0], 0, chars[0][1], ink.height)).getbbox()
    top, bottom = tall[1], tall[3]
    a, b = chars[-1]                                # the m: filled from outside the character
    box = ink.crop((a - 2, 0, b + 2, ink.height))
    ImageDraw.floodfill(box, (0, 0), 128)
    m = box.point(lambda v: 0 if v == 128 else 255)
    out = base.copy()
    ImageDraw.Draw(out).rectangle(inner, fill=(255, 255, 255))
    x = inner[0] + chars[0][0]
    for name, k in (T2_DIGITS[metres // 100], ZERO, ZERO):
        d = _digit(src, name, k)
        d = d.resize((round(d.width * (bottom - top) / d.height), bottom - top), Image.LANCZOS)
        out.paste((0, 0, 0), (x, inner[1] + top), d)
        x += d.width + round(0.12 * (bottom - top))
    out.paste((0, 0, 0), (inner[0] + a - 2, inner[1]), m)
    return out.convert("RGBA")


def _f2(src):
    """F2-1 without its place names: they painted out and the diagram moved to the middle of the sign they left."""
    im = Image.open(os.path.join(src, "f2-1.png")).convert("RGBA")
    blue = im.getpixel((im.width // 8, im.height - im.height // 8))
    d = ImageDraw.Draw(im)
    for r in F2_TEXT:
        d.rectangle(r, fill=blue)
    diagram = im.crop(F2_DIAGRAM)
    d.rectangle(F2_DIAGRAM, fill=blue)
    x, y = (im.width - diagram.width) // 2, (im.height - diagram.height) // 2
    im.paste(diagram, (x, y))
    return im


def sheet2(src):
    sheet = Image.new("RGB", (CELL * 4, CELL * 4), (255, 255, 255))
    ImageDraw.Draw(sheet).rectangle((896 - CELL // 2, 384 - CELL // 2, 896 + CELL // 2 - 1, 384 + CELL // 2 - 1),
                                    fill=RED)     # (C1's cell: its edge is its red)
    for name, pic, (cx, cy), side in SHEET2:
        if name == "f2":
            im = _f2(src)
        elif pic.startswith("t"):
            im = _plate(src, int(pic[1:]))
        else:
            im = Image.open(os.path.join(src, pic)).convert("RGBA")
        s = side / max(im.size)
        im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
        sheet.paste(im, (cx - im.width // 2, cy - im.height // 2), im)
    out = OUT.replace("bne_se_signs.png", "bne_se_signs2.png")
    sheet.quantize(COLOURS, dither=Image.Dither.NONE).save(out, optimize=True)
    print(out, os.path.getsize(out), "bytes")


def main(src):
    sheet = Image.new("RGB", (CELL * 4, CELL * 4), RED)
    names = [f"c31-{n}.png" for n in range(3, 13)] + ["b1-1.png"]
    for k, name in enumerate(names):
        im = Image.open(os.path.join(src, name)).convert("RGBA")
        s = ART / max(im.size)
        im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
        x, y = (k % 4) * CELL + (CELL - im.width) // 2, (k // 4) * CELL + (CELL - im.height) // 2
        sheet.paste(im, (x, y), im)
    sheet.quantize(COLOURS, dither=Image.Dither.NONE).save(OUT, optimize=True)
    print(OUT, os.path.getsize(OUT), "bytes")
    sheet2(src)


if __name__ == "__main__":
    main(sys.argv[1])
