"""the texture of Australia's speed-limit sign R4-1 (AS 1742.4): a white upright plate (3:4) with a thin black
border, a red ring and black numerals, one cell per value on bne_au_speed.png. server/speedsigns.py (au_mesh) maps a
cell onto a flat plate; its cells and their scale are speedsigns.AU_*.

    python tools/build_au_speedsigns.py

Drawn, not traced: proportions from the standard's R4-1 drawing (ring 0.90 of the plate's width, its stroke 0.10,
numerals 0.42 high), lettered in Roadgeek 2014 Series E Modified (tools/fonts, the AS 1744 Series E's FHWA ancestor).
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import speedsigns  # noqa: E402

OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth",
                   "bne_au_speed.png")
FONT = os.path.join(HERE, "fonts", "RG2014EEM.ttf")
SS = 4                          # supersampling
RED, BLACK, WHITE = (200, 16, 46), (20, 20, 20), (255, 255, 255)
COLOURS = 32


def plate(value, w, h):
    """One R4-1 of value at w x h px."""
    W, H = w * SS, h * SS
    im = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(im)
    b = round(0.02 * W)
    d.rounded_rectangle((0, 0, W - 1, H - 1), radius=round(0.05 * W), fill=BLACK)
    d.rounded_rectangle((b, b, W - 1 - b, H - 1 - b), radius=round(0.04 * W), fill=WHITE)
    r, s = 0.45 * W, 0.10 * W
    cx, cy = W / 2, H / 2
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=RED)
    d.ellipse((cx - r + s, cy - r + s, cx + r - s, cy + r - s), fill=WHITE)
    text = str(value)
    room = 2 * (r - s) * 0.90
    cap = 0.42 * W                  # numeral height
    font = ImageFont.truetype(FONT, 200)
    x0, y0, x1, y1 = font.getbbox(text)
    font = ImageFont.truetype(FONT, round(200 * cap / (y1 - y0)))
    x0, y0, x1, y1 = font.getbbox(text)
    layer = Image.new("L", (x1 - x0, y1 - y0), 0)
    ImageDraw.Draw(layer).text((-x0, -y0), text, font=font, fill=255)
    if layer.width > room:          # three digits: condensed, as the standard's 100 / 110
        layer = layer.resize((round(room), layer.height), Image.LANCZOS)
    im.paste(BLACK, (round(cx - layer.width / 2), round(cy - layer.height / 2)), layer)
    return im.resize((w, h), Image.LANCZOS)


def main():
    cw, ch = int(speedsigns.AU_CELL_W), int(speedsigns.AU_CELL_H)
    pw, ph = int(speedsigns.AU_PLATE_W), int(speedsigns.AU_PLATE_H)
    sheet = Image.new("RGB", (int(speedsigns.AU_SHEET_W), int(speedsigns.AU_SHEET_H)), WHITE)
    for k, value in enumerate(speedsigns.AU_VALUES):
        col, row = k % speedsigns.AU_COLS, k // speedsigns.AU_COLS
        sheet.paste(plate(value, pw, ph), (col * cw + (cw - pw) // 2, row * ch + (ch - ph) // 2))
    sheet.quantize(COLOURS, dither=Image.Dither.NONE).save(OUT, optimize=True)
    print(OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
