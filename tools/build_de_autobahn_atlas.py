"""The German Autobahn signs' own lettering atlas: DINish (DIN 1451 Mittelschrift / Engschrift lookalike, OFL 1.1) and
the RWB/RWBA pictograms (public domain) in one opacity texture, with materials of its own; the shared sign atlas
(tools/build_town_fonts.py) is not touched. A developer tool: run it with a Python that has Pillow and fontTools
(here /usr/bin/python3) and ImageMagick's `magick` (librsvg) on the PATH:

    /usr/bin/python3 tools/build_de_autobahn_atlas.py

Writes mod/.../beamngEarth/de_autobahn/{de_letters_o.data.png, de_autobahn.materials.json, OFL.txt, NOTICE.md} and
server/data/de_sign_font.json (read by server/deautobahnfont.py). The glyph loop and the shelf packer follow
tools/build_town_fonts.py. Glyph bounds are in cap-height units, y up; pictograms are unit squares.
"""
from pathlib import Path
import hashlib
import io
import json
import subprocess
import uuid

from PIL import Image, ImageDraw, ImageFont
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "tools/fonts/de_autobahn"
OUT = ROOT / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/de_autobahn"
VFS = "/levels/beamng_earth/art/shapes/beamngEarth/de_autobahn"
DATA = ROOT / "server/data/de_sign_font.json"
SIZE = 56                          # px per em when rendering
PICTO_PX = 160
WIDTH, HEIGHT = 2048, 1024
PAD = 3
FACES = (("DINish-Medium.otf", "DINish Medium"), ("DINishCondensed-Medium.otf", "DINish Condensed Medium"))
EXTRA = [0x1E9E, *range(0x2010, 0x201F), 0x20AC]
# OSM destination:symbol -> (code point, file, ink): below U+E010 white ink on the blue panel, from U+E010 black ink on a
# white square (the kernel draws the square)
PICTOGRAMS = {
    "motorway_junction": (0xE000, "ausfahrt.svg", "white"),
    "interchange": (0xE001, "kreuz.svg", "white"),
    "airport": (0xE010, "flughafen.svg", "black"),
    "harbour": (0xE011, "faehre.svg", "black"),
    "ferry": (0xE011, "faehre.svg", "black"),
    "industrial": (0xE012, "industriegebiet.svg", "black"),
    "train_station": (0xE013, "bahnhof.svg", "black"),
    "railroad_freight_terminal": (0xE014, "gvz.svg", "black"),
    "stadium": (0xE015, "stadion.svg", "black"),
    "park_and_ride": (0xE016, "park_and_ride.svg", "black"),
}
# baseColorFactor in the existing sign materials' convention (linear values; the sRGB targets in comments; tuned in
# game against photos)
COLOURS = {
    # sRGB #1F89BF, 0.24 of the white's luminance: tuned live in the game against the 2023 A93 gantry photos at Kreuz
    # Regensburg (0.24-0.35, r:g:b 0.03 : 0.43 : 1) on a sunlit, a shaded and a backlit face (2026-11-09: a shaded
    # face gets about a quarter of the light; the game shifts the blue towards violet, hence a little more green).
    # The Commons drawings' #154889 (0.07) and #1B6EA2 (0.15) were near black in the shade.
    "bne_de_blue": [0.014, 0.25, 0.52],
    "bne_de_white": [0.913, 0.932, 0.913],       # sRGB 0.96 0.97 0.96
    "bne_de_yellow": [0.930, 0.565, 0.0],        # sRGB #F7C600
    "bne_de_green": [0.0, 0.242, 0.089],         # sRGB #008754
    "bne_de_black": [0.005, 0.005, 0.005],       # sRGB 0.06
}


def chars_of(cmap):
    want = set(range(32, 127)) | set(range(160, 0x250)) | set(EXTRA)
    return sorted(chr(cp) for cp in want if cp in cmap)


def kerning(tt, chars, factor):
    """{(a, b): advance adjustment in cap units} from the font's kern feature (pair lookups, formats 1 and 2)."""
    cmap = tt.getBestCmap()
    glyph = {ch: cmap[ord(ch)] for ch in chars}
    byname = {}
    for ch, g in glyph.items():
        byname.setdefault(g, []).append(ch)
    gpos = tt["GPOS"].table
    lookups = sorted({i for f in gpos.FeatureList.FeatureRecord if f.FeatureTag == "kern"
                      for i in f.Feature.LookupListIndex})
    pairs = {}
    for li in lookups:
        lookup = gpos.LookupList.Lookup[li]
        if lookup.LookupType != 2:
            continue
        done = set()
        for sub in lookup.SubTable:
            cover = sub.Coverage.glyphs
            if sub.Format == 1:
                for gi, first in enumerate(cover):
                    for rec in sub.PairSet[gi].PairValueRecord:
                        v = getattr(rec.Value1, "XAdvance", 0) if rec.Value1 is not None else 0
                        for a in byname.get(first, ()):
                            for b in byname.get(rec.SecondGlyph, ()):
                                if (a, b) not in done and v:
                                    pairs[(a, b)] = pairs.get((a, b), 0.0) + v * factor
                                    done.add((a, b))
            elif sub.Format == 2:
                c1, c2 = sub.ClassDef1.classDefs, sub.ClassDef2.classDefs
                for first in cover:
                    if first not in byname:
                        continue
                    row = sub.Class1Record[c1.get(first, 0)].Class2Record
                    for second, chs in byname.items():
                        rec = row[c2.get(second, 0)]
                        v = getattr(rec.Value1, "XAdvance", 0) if rec.Value1 is not None else 0
                        if not v:
                            continue
                        for a in byname[first]:
                            for b in chs:
                                if (a, b) not in done:
                                    pairs[(a, b)] = pairs.get((a, b), 0.0) + v * factor
                                    done.add((a, b))
    return {k: v for k, v in pairs.items() if abs(v) >= 0.005}


def pictogram_mask(path, ink):
    """The ink of a pictogram SVG as an L mask PICTO_PX square (rendered by ImageMagick / librsvg)."""
    png = subprocess.run(["magick", "-background", "none", "-density", "600", str(path), "-resize",
                          f"{PICTO_PX}x{PICTO_PX}!", "png:-"], check=True, capture_output=True).stdout
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    out = Image.new("L", im.size)
    px, dst = im.load(), out.load()
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, a = px[x, y]
            lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
            k = (lum - 0.4) / 0.4 if ink == "white" else (0.6 - lum) / 0.4
            dst[x, y] = int(round(255 * max(0.0, min(1.0, k)) * a / 255.0))
    return out


def material(name, stage):
    return {"name": name, "mapTo": name, "class": "Material", "version": 1.5,
            "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/" + name)),
            "Stages": [stage, {}, {}, {}], "castShadows": False}


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    rows, images, kern, faces_meta = [], [], [], []
    for face, (file, label) in enumerate(FACES):
        path = SOURCES / file
        font, tt = ImageFont.truetype(str(path), SIZE), TTFont(path)
        cap = font.getbbox("H", anchor="ls")[3] - font.getbbox("H", anchor="ls")[1]
        chars = chars_of(tt.getBestCmap())
        faces_meta.append({"face": face, "font": label, "file": file,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        for ch in chars:
            l, t, r, b = font.getbbox(ch, anchor="ls")
            row = [face, ord(ch), font.getlength(ch) / cap, l / cap, -b / cap, r / cap, -t / cap, 0, 0, 0, 0]
            rows.append(row)
            if r - l > 0 and b - t > 0:
                im = Image.new("L", (r - l, b - t))
                ImageDraw.Draw(im).text((-l, -t), ch, font=font, fill=255, anchor="ls")
                images.append((row, im))
        for (a, b), v in sorted(kerning(tt, chars, SIZE / (tt["head"].unitsPerEm * cap)).items()):
            kern.append([face, ord(a), ord(b), round(v, 4)])
    pictos, seen = {}, {}
    for osm, (cp, file, ink) in PICTOGRAMS.items():
        pictos[osm] = cp
        if cp in seen:
            continue
        row = [2, cp, 1.0, 0.0, 0.0, 1.0, 1.0, 0, 0, 0, 0]
        rows.append(row)
        images.append((row, pictogram_mask(SOURCES / "pictograms" / file, ink)))
        seen[cp] = {"file": file, "ink": ink}
    x = y = rowh = 0
    placed = []
    for row, im in sorted(images, key=lambda v: (-v[1].height, v[0][0], v[0][1])):
        w, h = im.size
        if x + w + PAD * 2 > WIDTH:
            x, y, rowh = 0, y + rowh, 0
        placed.append((row, im, x + PAD, y + PAD))
        x += w + PAD * 2
        rowh = max(rowh, h + PAD * 2)
    if y + rowh > HEIGHT:
        raise ValueError(f"atlas needs {y + rowh} rows, more than {HEIGHT}")
    alpha = Image.new("L", (WIDTH, HEIGHT))
    for row, im, px, py in placed:
        alpha.paste(im, (px, py))
        row[7:11] = [px / WIDTH, 1 - (py + im.height) / HEIGHT, (px + im.width) / WIDTH, 1 - py / HEIGHT]
    alpha.save(OUT / "de_letters_o.data.png")
    rows.sort(key=lambda r: (r[0], r[1]))
    meta = {"format": 1, "generator": "tools/build_de_autobahn_atlas.py", "atlas_size": [WIDTH, HEIGHT],
            "faces": faces_meta, "face_params": [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]],
            "columns": ["face", "cp", "advance", "left", "bottom", "right", "top", "u0", "v0", "u1", "v1"],
            "glyphs": [[r[0], r[1]] + [round(v, 6) for v in r[2:]] for r in rows], "kerning": kern,
            "pictograms": pictos, "pictogram_files": {f"{cp:04X}": v for cp, v in sorted(seen.items())}}
    DATA.write_text(json.dumps(meta, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    mats = {}
    for name, colour in (("bne_de_letters_white", COLOURS["bne_de_white"]), ("bne_de_letters_black", COLOURS["bne_de_black"])):
        m = material(name, {"baseColorFactor": colour + [1], "roughnessFactor": 0.65, "metallicFactor": 0.0,
                            "opacityMap": VFS + "/de_letters_o.data.png", "opacityFactor": 1.0})
        m.update({"alphaTest": True, "alphaRef": 64, "translucent": False})
        mats[name] = m
    for name, colour in COLOURS.items():
        m = material(name, {"baseColorFactor": colour + [1], "roughnessFactor": 0.65, "metallicFactor": 0.0})
        m["castShadows"] = True
        mats[name] = m
    (OUT / "de_autobahn.materials.json").write_text(json.dumps(mats, indent=2) + "\n")
    (OUT / "OFL.txt").write_bytes((SOURCES / "OFL.txt").read_bytes())
    (OUT / "NOTICE.md").write_text(
        "# German Autobahn sign lettering and pictograms\n\n"
        "`de_letters_o.data.png` is drawn by `tools/build_de_autobahn_atlas.py` from:\n\n"
        "- DINish Medium and DINish Condensed Medium v4.007 (https://github.com/playbeing/dinish), Copyright 2021 The "
        "DINish Project Authors, SIL Open Font License 1.1 (`OFL.txt`).\n"
        "- The RWB / RWBA 2000 pictograms from Wikimedia Commons (public domain; German traffic sign designs are "
        "official works, Paragraph 5(1) UrhG): " + ", ".join(sorted({v[1] for v in PICTOGRAMS.values()})) +
        " (sources in `tools/fonts/de_autobahn/SOURCE.md`).\n", encoding="utf-8")
    print(len(rows), "glyphs,", len(kern), "kerning pairs,", "rows used", y + rowh, "of", HEIGHT)


if __name__ == "__main__":
    build()
