"""Build original, texture-free corn-like GroundCover meshes and their stock-style definition.

Run from any directory: python tools/make_crop_asset.py
The output is deterministic and needs only the Python standard library. An optional
--preview FILE renders a small planted patch using matplotlib (a development-only dependency).
No game files or downloaded assets are used.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "mod/beamng_earth/art/shapes/crops"
MATERIAL = "bngEarthCropPlant"
CROP_COLOR = (0.32, 0.36, 0.05, 1.0)  # muted yellow-green foliage
NS = "http://www.collada.org/2005/11/COLLADASchema"
ET.register_namespace("", NS)


def elem(parent, tag, text=None, **attrs):
    node = ET.SubElement(parent, "{" + NS + "}" + tag, attrs)
    if text is not None:
        node.text = str(text)
    return node


def text_floats(values):
    return " ".join(f"{v:.7g}" for v in values)


def plant(variant=0, low=False):
    """Triangle soup in metres, Z up; leaves have a folded midrib for real shaded volume."""
    faces = []
    phase = variant * 0.39
    lean = (0.035 + variant * 0.013, -0.018 + variant * 0.017)

    def stem_center(z):
        return lean[0] * z * z, lean[1] * z * z, z

    def triangle(a, b, c):
        faces.append((a, b, c))

    sides = 3 if low else 4
    zs = (0, 0.9, 1.75) if low else (0, 0.58, 1.16, 1.75)
    rings = []
    for z in zs:
        x, y, _ = stem_center(z)
        radius = 0.015 * (1 - 0.55 * z / 1.75)
        rings.append([(x + radius * math.cos(phase + i * math.tau / sides),
                       y + radius * math.sin(phase + i * math.tau / sides), z) for i in range(sides)])
    for lower, upper in zip(rings, rings[1:]):
        for i in range(sides):
            j = (i + 1) % sides
            triangle(lower[i], lower[j], upper[j])
            triangle(lower[i], upper[j], upper[i])
    for i in range(1, sides - 1):
        triangle(rings[-1][0], rings[-1][i], rings[-1][i + 1])

    for leaf in range(6):
        z0 = 0.27 + leaf * 0.225
        length = (0.64, 0.72, 0.70, 0.63, 0.54, 0.43)[leaf] * (1 + 0.035 * (variant - 1))
        half_width = (0.075, 0.083, 0.080, 0.075, 0.065, 0.052)[leaf]
        angle = phase + leaf * math.pi + (0.32 if leaf % 2 else -0.32) + variant * (leaf - 2.5) * 0.025
        dx, dy = math.cos(angle), math.sin(angle)
        side = (-dy, dx)
        start = stem_center(z0)

        def section(t):
            curve = 0.08 * math.sin(math.pi * t) * (-1 if leaf % 2 else 1)
            x = start[0] + dx * length * t + side[0] * curve
            y = start[1] + dy * length * t + side[1] * curve
            # Lower leaves arch and droop; upper leaves remain raised like young maize.
            z = z0 + (0.22 + 0.015 * leaf) * math.sin(math.pi * t * 0.85)
            z += (0.04 + 0.025 * leaf) * t - (0.24 - 0.032 * leaf) * t * t
            width = half_width * math.sin(math.pi * t) ** 0.7
            center = (x, y, z + 0.024 * math.sin(math.pi * t))
            return ((x + side[0] * width, y + side[1] * width, z), center,
                    (x - side[0] * width, y - side[1] * width, z))

        stations = (0.48,) if low else (0.16, 0.35, 0.55, 0.74, 0.89)
        sections = [section(t) for t in stations]
        left, middle, right = sections[0]
        triangle(start, right, middle)
        triangle(start, middle, left)
        for previous, following in zip(sections, sections[1:]):
            for i in range(2):
                triangle(previous[i], previous[i + 1], following[i + 1])
                triangle(previous[i], following[i + 1], following[i])
        tip = section(1)[1]
        left, middle, right = sections[-1]
        triangle(left, middle, tip)
        triangle(middle, right, tip)
    return faces


def normal(face):
    a, b, c = face
    u, v = [b[i] - a[i] for i in range(3)], [c[i] - a[i] for i in range(3)]
    n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    size = math.sqrt(sum(x * x for x in n))
    if size < 1e-10:
        raise ValueError("degenerate plant triangle")
    return tuple(x / size for x in n)


def geometry(parent, name, faces):
    geom = elem(parent, "geometry", id=name, name=name)
    mesh = elem(geom, "mesh")
    values = {"positions": [v for face in faces for vertex in face for v in vertex],
              "normals": [v for face in faces for _ in range(3) for v in normal(face)]}
    for semantic, data in values.items():
        source = elem(mesh, "source", id=name + "-" + semantic)
        elem(source, "float_array", text_floats(data), id=name + "-" + semantic + "-array", count=str(len(data)))
        accessor = elem(elem(source, "technique_common"), "accessor", source="#" + name + "-" + semantic + "-array",
                        count=str(len(data) // 3), stride="3")
        for axis in "XYZ":
            elem(accessor, "param", name=axis, type="float")
    vertices = elem(mesh, "vertices", id=name + "-vertices")
    elem(vertices, "input", semantic="POSITION", source="#" + name + "-positions")
    triangles = elem(mesh, "triangles", count=str(len(faces)), material=MATERIAL)
    elem(triangles, "input", semantic="VERTEX", source="#" + name + "-vertices", offset="0")
    elem(triangles, "input", semantic="NORMAL", source="#" + name + "-normals", offset="1")
    elem(triangles, "p", " ".join(str(i) for i in range(len(faces) * 3) for _ in range(2)))


def write_dae(path, variant):
    doc = ET.Element("{" + NS + "}COLLADA", {"version": "1.4.1"})
    asset = elem(doc, "asset")
    elem(elem(asset, "contributor"), "authoring_tool", "BeamNG.Earth original procedural crop generator")
    elem(asset, "created", "2026-11-20T00:00:00Z")
    elem(asset, "modified", "2026-11-20T00:00:00Z")
    elem(asset, "unit", name="meter", meter="1")
    elem(asset, "up_axis", "Z_UP")
    effect = elem(elem(doc, "library_effects"), "effect", id="crop-effect")
    phong = elem(elem(elem(effect, "profile_COMMON"), "technique", sid="common"), "phong")
    elem(elem(phong, "diffuse"), "color", text_floats(CROP_COLOR))
    elem(elem(phong, "specular"), "color", "0.04 0.04 0.04 1")
    elem(elem(phong, "shininess"), "float", "8")
    material = elem(elem(doc, "library_materials"), "material", id=MATERIAL, name=MATERIAL)
    elem(material, "instance_effect", url="#crop-effect")
    geometries = elem(doc, "library_geometries")
    for label, low in (("plant64", False), ("plant8", True)):
        geometry(geometries, label + "-mesh", plant(variant, low))
    scene = elem(elem(doc, "library_visual_scenes"), "visual_scene", id="Scene", name="Scene")
    base = elem(scene, "node", id="base00", name="base00", type="NODE")
    start = elem(base, "node", id="start01", name="start01", type="NODE")
    for label in ("plant64", "plant8"):
        node = elem(start, "node", id=label, name=label, type="NODE")
        instance = elem(node, "instance_geometry", url="#" + label + "-mesh")
        binding = elem(elem(instance, "bind_material"), "technique_common")
        elem(binding, "instance_material", symbol=MATERIAL, target="#" + MATERIAL)
    elem(elem(doc, "scene"), "instance_visual_scene", url="#Scene")
    ET.indent(doc, space="  ")
    path.write_bytes(ET.tostring(doc, encoding="utf-8", xml_declaration=True) + b"\n")


def build(output):
    output.mkdir(parents=True, exist_ok=True)
    for variant in range(3):
        write_dae(output / f"crop_{variant + 1:02d}.dae", variant)
    materials = {"bngEarthCropPlant": {"name": "bngEarthCropPlant", "mapTo": MATERIAL, "class": "Material",
                 "version": 1.5, "doubleSided": True, "invertBackFaceNormals": True, "castShadows": False,
                 "Stages": [{"baseColorFactor": list(CROP_COLOR), "roughnessFactor": 0.8,
                             "metallicFactor": 0}], "groundType": "LEAVES_THIN"}}
    (output / "main.materials.json").write_text(json.dumps(materials, indent=2) + "\n", encoding="utf-8", newline="\n")
    cover = {"name": "corn_like_crop", "class": "GroundCover", "material": "bngEarthCropPlant",
             "radius": 50, "dissolveRadius": 35, "gridSize": 6, "maxElements": 12000,
             "shapeCullRadius": 50, "shapesCastShadows": False, "seed": 27,
             "Types": [{"layer": "Crop", "shapeFilename": f"/art/shapes/crops/crop_{i + 1:02d}.dae",
                        "sizeMin": 0.85, "sizeMax": 1.15, "probability": 1,
                        "minClumpCount": 1, "maxClumpCount": 1, "clumpRadius": 0.3,
                        "windScale": 0.05} for i in range(3)]}
    (output / "items.level.json").write_text(json.dumps(cover, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
    return {"variants": 3, "height_m": round(max(v[2] for f in plant() for v in f), 3),
            "triangles_near": len(plant()), "triangles_far": len(plant(low=True))}


def preview(path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    import numpy as np

    fig = plt.figure(figsize=(12, 7), facecolor="#e9eee4")
    ax = fig.add_subplot(111, projection="3d")
    light = np.array((-0.4, -0.6, 1.0)); light /= np.linalg.norm(light)
    def display_color(linear):
        return np.where(linear <= 0.0031308, 12.92 * linear, 1.055 * linear ** (1 / 2.4) - 0.055)
    for row in range(3):
        for col in range(4):
            faces = plant((row + col) % 3)
            colors = [display_color(np.clip(np.array(CROP_COLOR[:3]) * (0.65 + 0.45 * abs(np.dot(normal(f), light))), 0, 1))
                      for f in faces]
            placed = [[[x + col * 0.58, y + row * 0.76, z] for x, y, z in face] for face in faces]
            ax.add_collection3d(Poly3DCollection(placed, facecolors=colors, edgecolors="none"))
    ax.set(xlim=(-0.6, 2.4), ylim=(-0.65, 2.2), zlim=(0, 1.9))
    ax.set_box_aspect((3, 2.85, 1.9)); ax.view_init(elev=15, azim=-55)
    ax.set_axis_off(); ax.set_title(
        f"Original corn-like crop · 3 variants · {len(plant())} near / {len(plant(low=True))} far triangles", pad=-2)
    fig.tight_layout(); path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160, bbox_inches="tight", facecolor=fig.get_facecolor()); plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.output), sort_keys=True))
    if args.preview:
        preview(args.preview)


if __name__ == "__main__":
    main()
