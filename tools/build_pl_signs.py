"""Build Polish Forest models from the untouched Commons originals."""
import hashlib
import io
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import resvg_py

from build_rail_crossing_props import Mesh, write_dae

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "tools/sign_art/pl"
VFS = "levels/beamng_earth/art/shapes/beamngEarth/pl_signs"
OUT = ROOT / "mod/beamng_earth" / VFS
METAL = "bne_cable_steel"
TOP = 2.75
PIXELS = 512


def outline_of(mask):
    """Convex hull of occupied pixel centres, plus a flag for shapes needing exact spans."""
    ys = np.flatnonzero(mask.any(axis=1))
    if not len(ys):
        raise ValueError("Empty sign")
    left = mask[ys].argmax(axis=1)
    right = mask.shape[1] - 1 - mask[ys, ::-1].argmax(axis=1)
    pts = sorted(set(zip(left.tolist(), ys.tolist())) | set(zip(right.tolist(), ys.tolist())))
    def chain(points):
        result = []
        for x, y in points:
            while len(result) >= 2:
                ax, ay = result[-2]
                bx, by = result[-1]
                if (bx - ax) * (y - ay) > (by - ay) * (x - ax):
                    break
                result.pop()
            result.append((x, y))
        return result
    hull = chain(pts)[:-1] + chain(reversed(pts))[:-1]
    if len(hull) < 3:
        raise ValueError("Degenerate sign")
    filled = Image.new("1", (mask.shape[1], mask.shape[0]))
    ImageDraw.Draw(filled).polygon(hull, fill=1)
    missing = np.asarray(filled) & ~mask.astype(bool)
    return np.asarray(hull, dtype=float) + 0.5, missing.sum() > 0.006 * mask.sum()


def make_model(code, source, art=ART, out=OUT):
    path = art / (code + ".svg")
    if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
        raise ValueError(f"Source checksum changed: {path}")
    # Set just one dimension. resvg preserves the full viewport and its aspect ratio.
    face = Image.open(io.BytesIO(resvg_py.svg_to_bytes(svg_path=str(path), width=PIXELS))).convert("RGBA")
    if face.height > PIXELS:
        face = Image.open(io.BytesIO(resvg_py.svg_to_bytes(svg_path=str(path), height=PIXELS))).convert("RGBA")
    canvas = Image.new("RGBA", (PIXELS, PIXELS))
    left, top = (PIXELS - face.width) // 2, (PIXELS - face.height) // 2
    canvas.paste(face, (left, top))
    group = "pl_" + code.lower().replace("-", "_").replace(".", "_")
    # Downsample the whole viewport. Keep the original mesh and UV coordinate grid.
    canvas.resize((256, 256), Image.Resampling.BOX).save(out / (group + "_face_256.png"))

    mask = np.uint8(np.asarray(face.getchannel("A")) > 127)
    ring, rectangular_strips = outline_of(mask)
    # A single metres-per-pixel factor governs both axes, including the transparent margins.
    scale = (0.90 if code.startswith("A-") else 0.75) / face.width
    xmid = (ring[:, 0].min() + ring[:, 0].max()) / 2
    ztop = ring[:, 1].min()
    def xyz(x, y, depth):
        return ((x - xmid) * scale, depth, TOP - (y - ztop) * scale)

    front, back = -0.046, -0.032
    mesh = Mesh(12)
    mesh.cylinder(METAL, 0, 0, -0.12, TOP - 0.14, 0.032)
    if rectangular_strips:
        # Nonconvex arrows and multipart silhouettes: merge equal horizontal spans.
        # This preserves holes instead of filling the sign's convex hull.
        spans = {}
        for y in range(mask.shape[0] + 1):
            row = mask[y] if y < mask.shape[0] else np.zeros(mask.shape[1], dtype=np.uint8)
            ends = np.flatnonzero(np.diff(np.pad(row, 1)))
            current = set(zip(ends[::2], ends[1::2]))
            for (x0, x1), y0 in list(spans.items()):
                if (x0, x1) in current:
                    continue
                corners = [(x0, y), (x1, y), (x1, y0), (x0, y0)]
                mesh.face(group, [xyz(x, yy, front) for x, yy in corners],
                          [((left + x) / PIXELS, 1 - (top + yy) / PIXELS) for x, yy in corners])
                mesh.face(METAL, [xyz(x, yy, back) for x, yy in reversed(corners)])
                for a, b in zip(corners, corners[1:] + corners[:1]):
                    mesh.face(METAL, [xyz(*b, front), xyz(*a, front), xyz(*a, back), xyz(*b, back)])
                del spans[x0, x1]
            for span in current:
                spans.setdefault(span, y)
        col = Mesh(6)
        col.cylinder(METAL, 0, 0, -0.12, TOP - 0.14, 0.032)
        write_dae(out / (group + "_plate.dae"), group, [(2, mesh)], col)
        return group
    # Positive signed area in image coordinates gives the rear face's outward normal (+Y).
    centre = ring.mean(axis=0)
    for a, b in zip(ring, np.roll(ring, -1, axis=0)):
        mesh.face(METAL, [xyz(*centre, back), xyz(*a, back), xyz(*b, back)])
        mesh.face(METAL, [xyz(*b, back), xyz(*a, back), xyz(*a, front), xyz(*b, front)])
    # Share the rim with the metal plate. Forest can draw the diffuse layer opaque.
    outline = ring[::-1]
    mesh.face(group, [xyz(x, y, front) for x, y in outline],
              [((left + x) / PIXELS, 1 - (top + y) / PIXELS) for x, y in outline])
    col = Mesh(6)
    col.cylinder(METAL, 0, 0, -0.12, TOP - 0.14, 0.032)
    write_dae(out / (group + "_plate.dae"), group, [(2, mesh)], col)
    return group


def material(group, vfs=VFS):
    return {"name": group, "mapTo": group, "class": "Material", "version": 1.5,
            "alphaTest": True, "alphaRef": 1,
            "Stages": [{"baseColorMap": f"/{vfs}/{group}_face_256.png",
                        "baseColorFactor": [1, 1, 1, 1], "roughnessFactor": 0.6,
                        "metallicFactor": 0, "retroreflectivity": 0.35}, {}, {}, {}]}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    materials = {}
    for code, source in json.loads((ART / "sources.json").read_text(encoding="utf8")).items():
        group = make_model(code, source)
        materials[group] = material(group)
    (OUT / "pl_signs.materials.json").write_text(json.dumps(materials, indent=2) + "\n", encoding="utf8")


if __name__ == "__main__":
    main()
