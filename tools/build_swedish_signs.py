"""The texture of Sweden's C31 and B1 (bne_se_signs.png): Transportstyrelsen's own artwork of the signs on one sheet.

    python tools/build_swedish_signs.py <folder of the downloaded PNGs: c31-3.png ... c31-12.png, b1-1.png>

The artwork is the agency's PNGs of the signs (transportstyrelsen.se, vagmarken: C31 30 to 120 km/h as c31-3 to c31-12,
B1 as b1-1; fetched 2026-11-02), drawn in its Tratex. Each stands centred in a cell of 256 px on the signs' red, which
is what a cell's edge and its mipmaps then bleed into. The cells and their scale are server/speedsigns.py's C31_*
and tools/build_median_props.py's B1_*.
"""
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth",
                   "bne_se_signs.png")
CELL, ART = 256, 244    # px: a cell, the artwork's longer side in it
RED = (238, 58, 66)     # the artwork's #EE3A42
COLOURS = 48            # enough for the anti-aliased edges; keeps the sheet to some 70 KB


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


if __name__ == "__main__":
    main(sys.argv[1])
