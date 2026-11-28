#!/usr/bin/env python3
"""make_font_atlas.py: the airport paint's lettering (the stand numbers, the type labels, the taxiways' names) as a font texture.

    python tools/make_font_atlas.py            writes art/runway/bne_paint_font_o.data.png (then converts it: tools/mod_textures_dds.py),
                                               server/data/paint_font.json and the three font materials of art/runway/main.materials.json

The characters 0-9, A-Z, - / . and the arrows (left, up, right, down and the four diagonals: the direction markings), in DejaVu Sans Bold (the typeface that ships with matplotlib,
matplotlib.get_data_path()/fonts/ttf/DejaVuSans-Bold.ttf: Bitstream Vera licence with public-domain additions, free to use, change and
ship; the glyphs of the atlas are renderings of it) on a 1024 x 1024 sheet of 128 px cells, 8 by 8, drawn four times over and reduced.
Every glyph stands in its cell with the baseline at BASE_PX and the capital height at CAP_PX, its ink centred, and never touches the
cell's edge (a mip level bleeds a neighbour's texels in at that distance: PAD_PX of margin is kept). The sheet is ONE channel, the
letter's opacity (the engine reads an opacityMap's .r: BC4, tools/mod_textures_dds.py): the colour is the paint's own solid colour map
(t_paint_<colour>_b.color.dds), so one sheet serves line_runway_font_black (the stand numbers on their yellow boxes),
line_runway_font_yellow (the taxiways' names on their black boxes) and line_runway_font_white (the equipment boxes' numbers). Their
materials are copies of line_runway_black's with that sheet as the opacity map (no depth written, like all the paint).

server/data/paint_font.json is the table standmarks reads: for each character `uv` (u0, v0, u1, v1: the quad's extent in the sheet,
V up from the bottom), `box` (x0, y0, x1, y1: the same extent in capital heights from the pen position on the baseline, y up) and `adv`
(the advance, in capital heights); `solid` is the (u, v) of a cell that is opaque all over. A line of text is quads placed along the baseline by the advances (standmarks.text_quads)."""
import json
import os
import site
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ART = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "runway")
TABLE = os.path.join(ROOT, "server", "data", "paint_font.json")
VFS = "/levels/beamng_earth/art/runway"
SHEET = "bne_paint_font_o.data.png"
CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-/.←↑→↓↖↗↘↙"
SIZE, CELL, SS = 1024, 128, 4       # the sheet, a cell, the supersampling
CAP_PX = 80                         # a capital's height in a cell,
BASE_PX = 100                       # the baseline's distance from the cell's top (the room below: a Q's tail, a slash),
PAD_PX = 4                          # the quad's margin round the ink (a clear border of texels, the opacity zero there)
MATERIALS = {"line_runway_font_black": ("t_paint_black_b.color.dds", "7b1d0c4e-2f63-4a8b-9e51-3c6d8a0f1b31"),
             "line_runway_font_yellow": ("t_paint_yellow_b.color.dds", "7b1d0c4e-2f63-4a8b-9e51-3c6d8a0f1b32"),
             "line_runway_font_white": ("t_paint_white_b.color.dds", "7b1d0c4e-2f63-4a8b-9e51-3c6d8a0f1b33")}


def font_path():
    try:
        import matplotlib
    except ImportError:                             # (the venv has none: the user's site-packages)
        sys.path.append(site.getusersitepackages())
        import matplotlib
    path = os.path.join(matplotlib.get_data_path(), "fonts", "ttf", "DejaVuSans-Bold.ttf")
    if not os.path.exists(path):
        sys.exit(f"{path}: not found (pip install matplotlib)")
    return path


def build():
    """(the sheet (1024 x 1024, "L"), the table {character: {uv, box, adv}}, cap height in sheet px)."""
    probe = ImageFont.truetype(font_path(), 400)
    cap = -probe.getbbox("H", anchor="ls")[1]       # (a capital's height at 400 px)
    font = ImageFont.truetype(font_path(), round(400.0 * (CAP_PX * SS) / cap))
    cap_ss = -font.getbbox("H", anchor="ls")[1]
    scale = CAP_PX / float(cap_ss)                  # (the supersampled font's pixels to the sheet's)
    sheet = Image.new("L", (SIZE, SIZE), 0)
    table, per_row = {}, SIZE // CELL
    for i, ch in enumerate(CHARS):
        cx, cy = (i % per_row) * CELL, (i // per_row) * CELL
        l, t, r, b = font.getbbox(ch, anchor="ls")  # (the ink, from the pen on the baseline, supersampled px)
        ox = CELL * SS / 2.0 - (l + r) / 2.0        # (the pen's x in the supersampled cell: the ink centred)
        cell = Image.new("L", (CELL * SS, CELL * SS), 0)
        ImageDraw.Draw(cell).text((ox, BASE_PX * SS), ch, font=font, fill=255, anchor="ls")
        sheet.paste(cell.resize((CELL, CELL), Image.LANCZOS), (cx, cy))
        pen = ox * scale                            # (in cell px, y down from the cell's top)
        x0, x1 = max(pen + l * scale - PAD_PX, 0.0), min(pen + r * scale + PAD_PX, CELL - 1.0)
        top, bot = max(BASE_PX + t * scale - PAD_PX, 0.0), min(BASE_PX + b * scale + PAD_PX, CELL - 1.0)
        table[ch] = {"uv": [round((cx + x0) / SIZE, 6), round(1.0 - (cy + bot) / SIZE, 6),
                            round((cx + x1) / SIZE, 6), round(1.0 - (cy + top) / SIZE, 6)],
                     "box": [round((x0 - pen) / CAP_PX, 5), round((BASE_PX - bot) / CAP_PX, 5),
                             round((x1 - pen) / CAP_PX, 5), round((BASE_PX - top) / CAP_PX, 5)],
                     "adv": round(font.getlength(ch) * scale / CAP_PX, 5)}
    i = len(CHARS)                                  # (a cell of solid opacity: the rings and rules drawn in the letters' material take their UVs from its middle)
    cx, cy = (i % per_row) * CELL, (i // per_row) * CELL
    sheet.paste(255, (cx, cy, cx + CELL, cy + CELL))
    return sheet, table, [round((cx + CELL / 2.0) / SIZE, 6), round(1.0 - (cy + CELL / 2.0) / SIZE, 6)]


def main():
    sheet, table, solid = build()
    png = os.path.join(ART, SHEET)
    sheet.save(png)
    with open(TABLE, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"generator": "tools/make_font_atlas.py", "font": "DejaVu Sans Bold (Bitstream Vera licence)", "sheet": SHEET,
                   "size": SIZE, "cap_px": CAP_PX, "solid": solid, "chars": table}, f, ensure_ascii=False, sort_keys=True, indent=1)
        f.write("\n")
    mats = os.path.join(ART, "main.materials.json")
    with open(mats, encoding="utf-8") as f:
        doc = json.load(f)
    for name, (colour, pid) in MATERIALS.items():
        m = json.loads(json.dumps(doc["line_runway_black"]))
        m["name"] = m["mapTo"] = name
        m["persistentId"] = pid
        m["Stages"][0]["baseColorMap"] = f"{VFS}/{colour}"
        m["Stages"][0]["opacityMap"] = f"{VFS}/{SHEET}"      # (tools/mod_textures_dds.py rewrites it to the .dds)
        doc[name] = m
    with open(mats, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")
    print("wrote", png, TABLE, mats)
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import mod_textures_dds
    mod_textures_dds.convert([png])
    for f in (mats, os.path.join(ROOT, "mod", "texture_sources", "textures.json")):     # (the converter writes text files with the platform's line ends)
        with open(f, "rb") as fh:
            data = fh.read().replace(b"\r\n", b"\n")
        with open(f, "wb") as fh:
            fh.write(data)


if __name__ == "__main__":
    main()
