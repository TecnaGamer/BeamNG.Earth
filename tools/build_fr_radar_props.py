"""French roadside camera shapes and SR3 signs. Source faces are in the Cerema IISR annex 27."""
import argparse
import json
from pathlib import Path
import sys
import uuid

from PIL import Image, ImageDraw

from build_rail_crossing_props import Mesh, write_dae

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from radars import HARDWARE, SIGN_HEIGHT  # noqa: E402

VFS = "/levels/beamng_earth/art/shapes/beamngEarth/fr_radars"
OUT = ROOT / "mod/beamng_earth" / VFS.lstrip("/")
CATALOG = ROOT / "mod/beamng_earth/lua/ge/extensions/beamngEarth/frRadarAssets.lua"
PREFIX = "bne_fr_radar_"
SIGN_FACE_SIZE = (512, 512)
TEXTURE_FILES = {"sr3a": "sr3a_512.color.png", "sr3b": "sr3b_512.color.png", "stripe": "stripe.color.png"}


def box(mesh, name, x, y, z, dx, dy, dz):
    mesh.box(PREFIX + name, (x, y, z), (dx / 2, dy / 2, dz / 2))


def face(mesh, name, x0, x1, z0, z1, y):
    mesh.face(PREFIX + name, [(x0, y, z0), (x1, y, z0), (x1, y, z1), (x0, y, z1)],
              [(0., 0.), (1., 0.), (1., 1.), (0., 1.)])


def window(mesh, x, z, w, h, y):
    box(mesh, "black", x, y, z, w + .026, .045, h + .026)
    box(mesh, "glass", x, y - .027, z, w, .012, h)


def cabinet(height, lens):
    m = Mesh(10)
    box(m, "zinc", 0, 0, .24, .16, .18, .49)
    box(m, "black", 0, .035, 1., .71, .49, 1.12)
    box(m, "silver", 0, -.22, .98, .655, .042, 1.07)
    # Sun hood above the black/yellow outline of the instrument face.
    box(m, "black", 0, -.04, height - .035, .81, .64, .07)
    for x in (-.29, .29):
        face(m, "stripe", x - .028, x + .028, .55, 1.43, -.243)
    for z in (.57, 1.40):
        face(m, "stripe", -.265, .265, z - .026, z + .026, -.244)
    window(m, -.132, lens, .17, .155, -.25)
    window(m, .135, lens, .17, .155, -.25)
    window(m, -.132, .95, .17, .188, -.25)
    return m


def chamfered(mesh, material, w, d, z0, z1):
    x = w / 2
    c = min(.08, w / 4)
    ring = [(-x + c, z0), (x - c, z0), (x, z0 + c), (x, z1 - c),
            (x - c, z1), (-x + c, z1), (-x, z1 - c), (-x, z0 + c)]
    front = [(x, -d / 2, z) for x, z in ring]
    back = [(x, d / 2, z) for x, z in ring]
    mesh.face(PREFIX + material, front)
    mesh.face(PREFIX + "black", back[::-1])
    for i in range(len(ring)):
        j = (i + 1) % len(ring)
        mesh.face(PREFIX + "black", (front[i], back[i], back[j], front[j]))


def pole_camera(kind, height, lens):
    m = Mesh(12)
    length, width, depth, mast = {
        "ETD": (1.18, .48, .31, .082),
        "ETT": (1.10, .61, .42, .126),
        "ETU": (.60, .29, .25, .062),
    }[kind]
    bottom = height - length
    m.cylinder(PREFIX + "zinc", 0, 0, -.08, bottom + .02, mast)
    for z in (bottom * .48, bottom * .56):
        m.cylinder(PREFIX + "yellow", 0, 0, z, z + .042, mast + .001)
    chamfered(m, "silver", width, depth, bottom, height)
    window(m, 0., lens, width * .43, .16 if kind == "ETU" else .23, -depth / 2 - .009)
    if kind != "ETU":
        window(m, 0., lens - .32, width * .42, .18, -depth / 2 - .009)
    return m


def warning(group, height, width):
    m = Mesh(10)
    z = height - width * 1.5
    for x in ((-.70, .70) if width > 2 else (0.,)):
        m.cylinder(PREFIX + "zinc", x, .06, -.10, height - .2, .055 if width > 2 else .042)
    box(m, "zinc", 0, 0, (z + height) / 2, width, .04, height - z)
    face(m, group[-4:], -width / 2, width / 2, z, height, -.022)
    return m


def materials():
    palette = {
        "zinc": (.31, .34, .35), "silver": (.66, .69, .69), "black": (.075, .08, .084),
        "glass": (.035, .06, .075), "yellow": (.91, .62, .05),
    }
    stages = {name: {"baseColorFactor": [*colour, 1.], "roughnessFactor": .24 if name == "glass" else .64,
                     "metallicFactor": .40 if name in {"zinc", "silver"} else 0.}
              for name, colour in palette.items()}
    for name, filename in TEXTURE_FILES.items():
        stages[name] = {"baseColorMap": f"{VFS}/{filename}", "roughnessFactor": .7}
    result = {}
    for key, stage in stages.items():
        name = PREFIX + key
        result[name] = {"name": name, "mapTo": name, "class": "Material", "version": 1.5, "Stages": [stage],
                        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth:" + name))}
    return result


def build(a=None, b=None):
    OUT.mkdir(parents=True, exist_ok=True)
    for name, source in (("sr3a", a), ("sr3b", b)):
        if source:
            with Image.open(source) as im:
                # BeamNG's Texture Cooker requires power-of-two dimensions.
                # The full-face UVs restore the portrait proportions on the sign.
                face_texture = im.convert("RGB").resize(SIGN_FACE_SIZE, Image.Resampling.LANCZOS)
                face_texture.save(OUT / TEXTURE_FILES[name], optimize=True)
    stripe = Image.new("RGB", (64, 64), (42, 42, 35))
    draw = ImageDraw.Draw(stripe)
    for x in range(-64, 64, 16):
        draw.polygon(((x, 0), (x + 8, 0), (x + 72, 64), (x + 64, 64)), fill=(244, 190, 37))
    stripe.save(OUT / TEXTURE_FILES["stripe"], optimize=True)
    (OUT / "main.materials.json").write_text(json.dumps(materials(), indent=2) + "\n", encoding="utf-8")
    assets = []
    for kind, (group, height, lens) in HARDWARE.items():
        mesh = cabinet(height, lens) if kind == "ETF" else pole_camera(kind, height, lens)
        assets.append((group, height, 530, mesh))
    for group, height in SIGN_HEIGHT.items():
        width = 2.4 if group.endswith("sr3a") else 1.2
        assets.append((group, height, 760, warning(group, height, width)))

    lua = ["-- Generated by tools/build_fr_radar_props.py", "return {"]
    for group, height, reach, mesh in assets:
        name = "bne_" + group
        col = Mesh()
        box(col, "zinc", 0, 0, height / 2, .18, .16, height)
        write_dae(OUT / f"{name}.dae", name, [(2, mesh)], col)
        lua.append(f"  {group} = {{ '{VFS}/{name}', {height:g}, {reach} }},")
    CATALOG.write_text("\n".join([*lua, "}", ""]), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sr3a", type=Path)
    parser.add_argument("--sr3b", type=Path)
    args = parser.parse_args()
    build(args.sr3a, args.sr3b)
