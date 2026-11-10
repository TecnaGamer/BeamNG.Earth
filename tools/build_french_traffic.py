"""Original European no-entry and French AB3a/M9c + A1 traffic-sign assets.

No reference photograph pixels are redistributed. Geometric sign diagrams and
the already bundled DejaVu font are drawn procedurally. Units: metres, Z up;
front: -local Y. Flat rectangular French supports, three LODs, simple collision.
"""
import argparse
from functools import lru_cache
import json
import math
from pathlib import Path
import uuid

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from build_rail_crossing_props import Mesh, write_dae

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/fr_traffic"
VFS = "/levels/beamng_earth/art/shapes/beamngEarth/fr_traffic"
KINDS = ("fr_noentry", "eu_noentry", "fr_giveway_m9c", "fr_a1a", "fr_a1b", "fr_a1c", "fr_a1d")
H = 2.45
HEIGHTS = {k: (1.8 if k.startswith("fr_a1") else H) for k in KINDS}
FONT = ROOT / "tools/fonts/DejaVuSans-Bold.ttf"
METAL = "bne_frt_metal"
WHITE, RED, BLACK = "#f3f2eb", "#ce1725", "#151719"
M9C_ROWS = (("CÉDEZ LE", .28), ("PASSAGE", .70))


@lru_cache(maxsize=1)
def font_atlas():
    """Use the existing shipped glyph pixels, not the host's font renderer.

    Loading a TrueType font invokes the installed FreeType/layout libraries;
    the same font file need not rasterize identically on Windows and Linux.
    Atlas UVs/metrics and glyph pixels are already part of the regional kit.
    """
    meta = json.loads((ROOT / "server/data/sign_font.json").read_text(encoding="utf-8"))
    with Image.open(OUT.parent / "regional_signs/sign_font_opacity.data.png") as im:
        mask = im.convert("L")
    return meta["glyphs"], mask


def atlas_label(image, text, centre, cap_height):
    glyphs, atlas = font_atlas()
    boxes = []
    pen = 0.
    for ch in text:
        g = glyphs[ch]
        left, bottom, right, top = g["bounds"]
        if right > left and top > bottom:
            boxes.append((g["uv"], (pen + left, -top, pen + right, -bottom)))
        pen += g["advance"]
    if not boxes:
        return
    xmin = min(b[0] for _, b in boxes)
    ymin = min(b[1] for _, b in boxes)
    xmax = max(b[2] for _, b in boxes)
    ymax = max(b[3] for _, b in boxes)
    scale = min(float(cap_height), image.width * .90 / (xmax - xmin))
    dx = centre[0] - (xmin + xmax) * scale / 2
    dy = centre[1] - (ymin + ymax) * scale / 2
    for (u0, v0, u1, v1), (left, top, right, bottom) in boxes:
        src = (round(u0 * atlas.width), round((1 - v1) * atlas.height),
               round(u1 * atlas.width), round((1 - v0) * atlas.height))
        dest = tuple(round(v) for v in (dx + left * scale, dy + top * scale,
                                       dx + right * scale, dy + bottom * scale))
        mask = atlas.crop(src).resize((dest[2] - dest[0], dest[3] - dest[1]), Image.Resampling.LANCZOS)
        image.paste(BLACK, dest, mask)


def texture(kind, size=1024):
    im = Image.new("RGB", (size, size), WHITE)
    d = ImageDraw.Draw(im)
    xy = lambda points: [(int(x * size), int(y * size)) for x, y in points]
    if "noentry" in kind:
        d.ellipse((0, 0, size - 1, size - 1), fill=WHITE)
        rim = .016 if kind.startswith("fr_") else .009
        d.ellipse((int(size * rim), int(size * rim), int(size * (1 - rim)) - 1,
                   int(size * (1 - rim)) - 1), fill=RED)
        d.rectangle((int(.17 * size), int(.41 * size), int(.83 * size), int(.59 * size)), fill=WHITE)
    elif kind == "m9c":
        d.rectangle((8, 8, size - 9, size - 9), outline=BLACK, width=12)
        # Fixed DejaVu atlas glyphs retain the accent without FreeType/RAQM.
        for text, y in M9C_ROWS:
            atlas_label(im, text, (size / 2, size * y), size * .132)
    else:
        inverted = kind == "fr_giveway_m9c"
        outer = [(0., .067), (1., .067), (.5, .933)] if inverted else [(0., .933), (1., .933), (.5, .067)]
        centre = np.asarray(outer).mean(axis=0)
        d.polygon(xy(outer), fill=WHITE)
        for factor, colour in ((.965, RED), (.70, WHITE)):
            poly = centre + (np.asarray(outer) - centre) * factor
            d.polygon(xy(poly), fill=colour)
        if not inverted:
            # Right bend, or successive bends with the first to the right, when
            # read bottom-to-top. Left variants mirror the actual painted face.
            multi = kind in {"fr_a1c", "fr_a1d"}
            if multi:
                points = [(.41, .78), (.41, .70), (.61, .60), (.61, .51), (.5, .44), (.5, .40)]
            else:
                points = [(.41, .78), (.41, .67), (.42, .63), (.46, .59), (.60, .59)]
            # Rounded joins and a proper arrow head, not a thin polyline symbol.
            d.line(xy(points), fill=BLACK, width=int(.082 * size), joint="curve")
            if multi:
                head = [(.43, .46), (.50, .34), (.57, .46)]
            else:
                head = [(.55, .51), (.66, .59), (.55, .67)]
            d.polygon(xy(head), fill=BLACK)
            if kind in {"fr_a1b", "fr_a1d"}:
                im = ImageOps.mirror(im)
    return im.resize((512, 512), Image.Resampling.LANCZOS)


def board(m, kind, detail):
    top = HEIGHTS[kind]
    if "noentry" in kind:
        n = (40, 24, 16)[detail]
        radius = .325
        points = [(radius * math.cos(a), top - radius + radius * math.sin(a))
                  for a in np.linspace(0, 2 * math.pi, n, endpoint=False)]
        bounds = (-radius, top - 2 * radius, 2 * radius, 2 * radius)
    else:
        side = .9
        height = side * math.sqrt(3) / 2
        points = [(-side / 2, top), (0., top - height), (side / 2, top)] if kind == "fr_giveway_m9c" else \
            [(-side / 2, top - height), (side / 2, top - height), (0., top)]
        # Texture triangles use an equilateral triangle inside a square.
        bounds = (-side / 2, top - height - .067 * side, side, side)
    # Ensure a -Y front and +Y back independently of shape order.
    points = np.asarray(points)
    a, b = points[1] - points[0], points[2] - points[0]
    if a[0] * b[1] - a[1] * b[0] < 0:
        points = points[::-1]
    front = [(x, -.033, z) for x, z in points]
    uv = [((x - bounds[0]) / bounds[2], (z - bounds[1]) / bounds[3]) for x, z in points]
    m.face("bne_frt_" + kind, front, uv)
    m.face(METAL, [(x, -.012, z) for x, z in points[::-1]])
    if detail < 2:
        for a, b in zip(points, np.roll(points, -1, axis=0)):
            m.face(METAL, [(a[0], -.033, a[1]), (a[0], -.012, a[1]),
                          (b[0], -.012, b[1]), (b[0], -.033, b[1])])
    if kind == "fr_giveway_m9c":
        z = top - .9 * math.sqrt(3) / 2 - .20
        m.box(METAL, (0., -.017, z), (.25, .009, .12))
        m.face("bne_frt_m9c", [(-.25, -.038, z - .12), (.25, -.038, z - .12),
                              (.25, -.038, z + .12), (-.25, -.038, z + .12)],
               [(0., 0.), (1., 0.), (1., 1.), (0., 1.)])


def model(kind, detail=0):
    m = Mesh((12, 8, 6)[detail])
    post = HEIGHTS[kind] - .2
    if kind.startswith("fr_"):
        m.box(METAL, (0., .016, post / 2), (.04, .02, post / 2))
    else:
        m.cylinder(METAL, 0., .016, 0., post, .03)
    board(m, kind, detail)
    return m


def build(out=OUT):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    materials = {}
    for kind in (*KINDS, "m9c"):
        texture(kind).save(out / (kind + ".color.png"))
        name = "bne_frt_" + kind
        materials[name] = {"name": name, "mapTo": name, "class": "Material", "version": 1.5,
                           "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "bne:fr-traffic:" + name)),
                           "Stages": [{"baseColorMap": VFS + "/" + kind + ".color.png",
                                       "baseColorFactor": [1., 1., 1., 1.], "metallicFactor": 0.,
                                       "roughnessFactor": .42, "retroreflectivity": .8}], "doubleSided": False}
    materials[METAL] = {"name": METAL, "mapTo": METAL, "class": "Material", "version": 1.5,
                        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "bne:fr-traffic:" + METAL)),
                        "Stages": [{"baseColorFactor": [.47, .49, .5, 1.], "metallicFactor": .65,
                                    "roughnessFactor": .5}], "doubleSided": False}
    # Geometry defines the paint; no opacity mask may erase small MRE polygons.
    name = "line_bne_fr_white"
    materials[name] = {"name": name, "mapTo": name, "class": "Material", "version": 1.5,
                       "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "bne:fr-traffic:" + name)),
                       "Stages": [{"baseColorFactor": [.9, .9, .86, 1.], "metallicFactor": 0.,
                                   "roughnessFactor": .8}], "doubleSided": False,
                       "translucent": False, "castShadows": False}
    (out / "main.materials.json").write_bytes((json.dumps(materials, indent=2) + "\n").encode())
    for kind in KINDS:
        collision = Mesh()
        post = HEIGHTS[kind] - .2
        collision.box(METAL, (0., .016, post / 2), (.04, .025, post / 2))
        path = out / ("bne_" + kind + ".dae")
        write_dae(path, "bne_" + kind, [(96, model(kind, 0)), (24, model(kind, 1)), (2, model(kind, 2))], collision)
        path.write_bytes(path.read_text(encoding="utf-8").replace("tools/build_rail_crossing_props.py",
                                                                "tools/build_french_traffic.py").encode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT)
    build(parser.parse_args().out)
    print("Built 7 sign models, 3 LODs each, and 8 procedural sign faces.")
