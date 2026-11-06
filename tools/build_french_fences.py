"""Build the mapped French boundary panels with the project's COLLADA exporter."""

import json
from pathlib import Path
import sys
import uuid

import numpy as np

from build_rail_crossing_props import Mesh, write_dae

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from fences import FR_HEIGHTS, PANEL_M  # noqa: E402

VFS = "/levels/beamng_earth/art/shapes/beamngEarth"
OUT = ROOT / "mod/beamng_earth" / VFS.lstrip("/") / "fr_fences"
CATALOG = ROOT / "mod/beamng_earth/lua/ge/extensions/beamngEarth/frFenceAssets.lua"
HALF = PANEL_M / 2
COLOURS = {
    "limestone": ((1.02, .98, .90), "stone", 0.),
    "dark_stone": ((.62, .61, .59), "stone", 0.),
    "brick": ((.76, .43, .34), "brick", 0.),
    "render": ((.67, .63, .55), "stucco", 0.),
    "iron": ((.15, .19, .16), None, .45),
    "zinc": ((.47, .50, .48), None, .55),
    "anthracite": ((.18, .20, .21), None, .35),
    "wood": ((.67, .51, .42), "wood", 0.),
    "white": ((.85, .85, .81), None, 0.),
}


def material(name):
    return "bne_fr_fence_mat_" + name


def box(mesh, name, x, y, z, dx, dy, dz, axes=None):
    start = len(mesh.p)
    mesh.box(material(name), (x, y, z), (dx, dy, dz), axes)
    if COLOURS[name][1]:
        # Project in the plane of each face to keep the masonry courses level.
        for i in range(start, len(mesh.p), 4):
            axis = int(np.argmax(np.abs(mesh.n[i])))
            uv_axes = [(1, 2), (0, 2), (0, 1)][axis]
            mesh.uv[i:i + 4] = [tuple(p[list(uv_axes)]) for p in mesh.p[i:i + 4]]


def rail(mesh, name, a, b, radius):
    a, b = np.array(a, dtype=float), np.array(b, dtype=float)
    centre = (a + b) / 2
    length = float(np.linalg.norm(b - a))
    axis = (b - a) / length
    across = np.array([0., 1., 0.])
    normal = np.cross(axis, across)
    box(mesh, name, *centre, length / 2, radius, radius, (axis, across, normal))


def panel(style, far=False):
    mesh = Mesh()
    h = FR_HEIGHTS[style]
    if style in {"limestone", "dark_stone"}:
        box(mesh, style, 0., 0., h / 2 - .035, HALF, .135, h / 2 - .015)
        box(mesh, style, 0., 0., h - .027, HALF, .16, .027)
        return mesh
    base = .44 if style == "brick" else .30 if style in {"iron", "mesh", "rails", "slats"} else .18
    masonry = "brick" if style == "brick" else "render"
    box(mesh, masonry, 0., 0., base / 2 - .025, HALF, .11, base / 2 + .025)
    box(mesh, masonry, 0., 0., base - .015, HALF, .135, .025)
    for x in (-HALF + .06, HALF - .06):
        box(mesh, masonry, x, 0., (h - .06) / 2 - .03, .075, .14, h / 2)
        box(mesh, "limestone", x, 0., h - .034, .092, .16, .034)
    width = HALF - .115
    if style in {"brick", "rails"}:
        fill = "wood" if style == "brick" else "anthracite"
        for z in np.linspace(base + .14, h - .13, 2 if style == "brick" else 4):
            box(mesh, fill, 0., 0., z, width, .025, .041 if style == "brick" else .053)
    elif style in {"wood", "slats", "iron"}:
        fill = {"wood": "wood", "slats": "white", "iron": "iron"}[style]
        for z in (base + .12, h - .19):
            box(mesh, fill, 0., .026, z, width, .024, .023)
        count = 12 if far else 18 if style == "slats" else 13
        for x in np.linspace(-width + .055, width - .055, count):
            top = h - .065 - (.08 * (1 - abs(x) / width) if style == "wood" else 0.)
            bar_width = .023 if style == "iron" else .043 if style == "slats" else .056
            box(mesh, fill, x, -.015, (base + top) / 2, bar_width, .022, (top - base) / 2)
            if style == "iron" and not far:
                rail(mesh, fill, (x - .034, -.015, base + .20), (x + .034, -.015, base + .29), .009)
    else:
        for z in (base + .025, h - .09):
            box(mesh, "zinc", 0., 0., z, width, .022, .022)
        height = h - base - .115
        step = .24 if far else .14
        for side in (-1., 1.):
            for start in np.arange(-width - height, width, step):
                lo, hi = max(start, -width), min(start + height, width)
                if hi - lo > .045:
                    rail(mesh, "zinc", (side * lo, .0, base + lo - start),
                         (side * hi, .0, base + hi - start), .010 if far else .006)
    return mesh


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    materials = {}
    for key, (colour, texture, metallic) in COLOURS.items():
        name = material(key)
        stage = {"baseColorFactor": [*colour, 1.], "roughnessFactor": .66 if metallic else .88,
                 "metallicFactor": metallic}
        if texture:
            stage["baseColorMap"] = f"{VFS}/fr_houses/{texture}.color.png"
            stage["normalMap"] = f"{VFS}/fr_houses/{texture}.normal.png"
        materials[name] = {"name": name, "mapTo": name, "class": "Material", "version": 1.5, "Stages": [stage],
                           "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth:" + name))}
    (OUT / "main.materials.json").write_text(json.dumps(materials, indent=2) + "\n", encoding="utf-8")
    lua = ["-- Generated by tools/build_french_fences.py", "return {"]
    for style, height in FR_HEIGHTS.items():
        group = f"fr_fence_{style}"
        name = "bne_" + group
        collision = Mesh()
        box(collision, "render", 0., 0., height / 2, HALF, .10, height / 2)
        write_dae(OUT / f"{name}.dae", name, [(48, panel(style)), (2, panel(style, True))], collision)
        lua.append(f"  {group} = {{ '{VFS}/fr_fences/{name}', {height:g} }},")
    CATALOG.write_text("\n".join([*lua, "}", ""]), encoding="utf-8")


if __name__ == "__main__":
    build()
