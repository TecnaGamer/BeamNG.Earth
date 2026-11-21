import argparse
import io
import json
import math
import struct
import sys
from pathlib import Path

import numpy as np

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root.parents[1] / "server"))
from tempfile import NamedTemporaryFile

import threedmr
import whole_store
from store_match import HEIGHTS, matches


def cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def export_store(path, match, observations, out):
    name, ident, coords, street, fixtures, wall_material, *sign_size = match
    height = HEIGHTS[name]
    center = (coords.min(0) + coords.max(0)) / 2
    xy = (coords - center) * [111320 * math.cos(math.radians(center[1])), 111132]
    overlay = street is None
    if not overlay:
        if sum(cross((0, 0), a, b) for a, b in zip(xy, np.roll(xy, -1, axis=0))) < 0:
            xy = xy[::-1]
        edges = np.roll(xy, -1, axis=0) - xy
        lengths = np.linalg.norm(edges, axis=1)
        normals = np.column_stack((edges[:, 1], -edges[:, 0])) / lengths[:, None]
        front = np.argmax(
            np.maximum(0, normals @ street) ** 3 * np.minimum(lengths, 28)
        )
    data = bytearray()
    views = []
    access = []
    primitives = []

    def view(raw):
        while len(data) % 4:
            data.append(0)
        views.append({"buffer": 0, "byteOffset": len(data), "byteLength": len(raw)})
        data.extend(raw)
        return len(views) - 1

    def attr(arr, typ):
        arr = np.asarray(arr, dtype="<f4")
        access.append(
            {
                "bufferView": view(arr.tobytes()),
                "componentType": 5126,
                "count": len(arr),
                "type": typ,
                "min": arr.min(0).tolist(),
                "max": arr.max(0).tolist(),
            }
        )
        return len(access) - 1

    def emit(points, mat, uv=None):
        att = {"POSITION": attr([(x, z, -y) for x, y, z in points], "VEC3")}
        if uv is not None:
            att["TEXCOORD_0"] = attr(uv, "VEC2")
        indices = len(access)
        access.append(
            {
                "bufferView": view(np.arange(len(points), dtype="<u2").tobytes()),
                "componentType": 5123,
                "count": len(points),
                "type": "SCALAR",
            }
        )
        primitives.append({"attributes": att, "indices": indices, "material": mat})

    def wall(a, b, lo, hi, mat):
        points = [(*a, lo), (*b, lo), (*b, hi), (*a, lo), (*b, hi), (*a, hi)]
        mesh(
            points,
            mat,
            [(0, 1), (1, 1), (1, 0), (0, 1), (1, 0), (0, 0)] if mat == 1 else None,
        )

    from restaurant import MATERIALS, restaurant

    painted = []
    painted_faces = []

    def mesh(points, mat, uv=None):
        if painted and mat >= 3 and (detailed or tenant_sign):
            painted_faces.append((points, mat - 3, uv))
        else:
            emit(points, mat, uv)

    def texture(image):
        painted.append(image)
        return 2 + len(painted)

    detailed = name in whole_store.CHAINS and not overlay
    tenant_sign = overlay and (name in whole_store.CHAINS or bool(sign_size))
    if overlay:
        a, b = xy
        width = np.linalg.norm(b - a)
        if width < 2 and not sign_size:
            return None
        normal = np.array([b[1] - a[1], a[0] - b[0]]) / width
        axis = (b - a) / width
        half_span = axis * min(width * 0.48, 10 if name in whole_store.MARKETS else 5)
        offset = normal * 0.24
        if sign_size:
            offset = np.zeros(2)
            top = sign_size[0]
            bottom = top - min(top * .45, width / 2.4)
            post = min(.24, width * .07)
            for corners, lo, hi, material in [
                ([axis*s*post + normal*t*post for s,t in ((-1,-1),(1,-1),(1,1),(-1,1))], 0., bottom, 2),
                ([axis*s*width/2 + normal*t*.18 for s,t in ((-1,-1),(1,-1),(1,1),(-1,1))], bottom, top, 2),
            ]:
                for left, right in zip(corners, corners[1:] + corners[:1]):
                    wall(left, right, lo, hi, material)
                mesh([(*corners[j], hi) for j in (0,1,2,0,2,3)], material)
            art = texture(whole_store.sign_image(name, width / (top-bottom), top-bottom, "roadside"))
            for side in (-1, 1):
                left = -side * axis * width/2 + normal * side * .19
                right = side * axis * width/2 + normal * side * .19
                mesh([(*left,bottom), (*right,bottom), (*right,top), (*left,bottom), (*right,top), (*left,top)],
                     art, [(0,1),(1,1),(1,0),(0,1),(1,0),(0,0)])
        elif tenant_sign:
            # Ground-floor glazing and an independent fascia with lettering readable across parking.
            panel_width = 2 * np.linalg.norm(half_span)
            # The entrance canopy stays within the tenant's frontage.
            canopy_half = axis * min(panel_width * .38, 4.5)
            outer = normal * .95
            corners = [-canopy_half, -canopy_half + outer, canopy_half + outer, canopy_half]
            for left, right in zip(corners, corners[1:] + corners[:1]):
                wall(left, right, 2.84, 3.04, 2)
            for z, indices in ((3.04, (0,1,2,0,2,3)), (2.84, (2,1,0,3,2,0))):
                mesh([(*corners[j], z) for j in indices], 2)
            for bottom, top, image in (
                (0., 3.1, whole_store.elevation(name, panel_width, 3.1, [panel_width / 2], [], [])),
                (3.1, 3.1 + min(2.5, panel_width / 4), None),
            ):
                art = image if image is not None else whole_store.sign_image(name, panel_width / (top-bottom), top-bottom)
                a1, b1 = -half_span, half_span
                points = [(*a1,bottom), (*b1,bottom), (*b1,top), (*a1,bottom), (*b1,top), (*a1,top)]
                mesh(points, texture(art), [(0,1),(1,1),(1,0),(0,1),(1,0),(0,0)])
        else:
            wall(-half_span, half_span, 0, min(3.2, np.linalg.norm(half_span) * 0.8), 1)
        center = center + offset / [111320 * math.cos(math.radians(center[1])), 111132]
    else:
        if detailed:
            scale = [111320 * math.cos(math.radians(center[1])), 111132]
            points = [((pt - center) * scale, dock) for pt, dock in fixtures]
            material = (wall_material or "").strip().lower()
            photos = []
            if not material:
                for record in observations.get(str(ident), []):
                    ends = (np.asarray(record["wall"]) - center) * scale
                    photos.append((ends, record.get("color")))
            whole_store.build(
                name, xy, street, height, points, mesh, texture, material == "brick", photos
            )
        if name == "McDonald's":
            restaurant(xy, front, height, mesh)
        for i, (a, b) in enumerate(zip(xy, np.roll(xy, -1, axis=0))):
            wall(a, b, 0, height, 0)
            if i == front and name != "McDonald's" and not detailed:
                mid = (a + b) / 2 + normals[i] * 0.06
                dx = (b - a) / lengths[i] * min(lengths[i] * 0.44, height * 1.35)
                depth = normals[i] * min(height * 0.14, 1.2)
                face1, face2 = mid - dx + depth, mid + dx + depth
                top = height * 1.08
                wall(face1, face2, 0, top, 1)
                # Project the entrance canopy beyond the doors. Its span follows the entrance,
                # even on a warehouse with a very long wall.
                awning = normals[i] * min(height * 0.22, 1.4)
                left = mid + depth - dx * 0.73
                right = mid + depth + dx * 0.73
                outer_left, outer_right = left + awning, right + awning
                canopy_z = top * 0.51
                wall(outer_left, outer_right, canopy_z, canopy_z + 0.18, 2)
                wall(left, outer_left, canopy_z, canopy_z + 0.18, 2)
                wall(outer_right, right, canopy_z, canopy_z + 0.18, 2)
                for z in (canopy_z, canopy_z + 0.18):
                    mesh(
                        [
                            (*left, z),
                            (*right, z),
                            (*outer_right, z),
                            (*left, z),
                            (*outer_right, z),
                            (*outer_left, z),
                        ],
                        2,
                    )
                wall(mid - dx, face1, 0, top, 0)
                wall(face2, mid + dx, 0, top, 0)
                mesh(
                    [
                        (*face1, top),
                        (*face2, top),
                        (*(mid + dx), top),
                        (*face1, top),
                        (*(mid + dx), top),
                        (*(mid - dx), top),
                    ],
                    2,
                )
        ids = list(range(len(xy)))
        roof = []
        while len(ids) > 2:
            for j, k in enumerate(ids):
                h, l = ids[j - 1], ids[(j + 1) % len(ids)]
                a, b, c = xy[[h, k, l]]
                if cross(a, b, c) <= 1e-8:
                    continue
                if any(
                    min(cross(a, b, xy[m]), cross(b, c, xy[m]), cross(c, a, xy[m])) >= 0
                    for m in ids
                    if m not in (h, k, l)
                ):
                    continue
                # Subdivide large roof panels for the normal game importer, which filters
                # large isolated flat triangles from third-party building models.
                pending = [(a, b, c)]
                while pending:
                    u, v, w = pending.pop()
                    if cross(u, v, w) > 400:
                        uv, vw, wu = (u + v) / 2, (v + w) / 2, (w + u) / 2
                        pending.extend(
                            [(u, uv, wu), (uv, v, vw), (wu, vw, w), (uv, vw, wu)]
                        )
                    else:
                        roof.extend([(*u, height), (*v, height), (*w, height)])
                ids.pop(j)
                break
            else:
                raise RuntimeError("roof triangulation")
        mesh(roof, 2)
    if painted:
        atlas, rects = whole_store.pack_elevations(painted)
        for points, index, uv in painted_faces:
            x, y, w, h = rects[index]
            mapped = [
                (
                    (x + 0.5 + u * (w - 1)) / atlas.width,
                    (y + 0.5 + v * (h - 1)) / atlas.height,
                )
                for u, v in uv
            ]
            emit(points, 3, mapped)
        painted = [atlas]
    from storefront_art import storefront

    mats = [
        {"doubleSided": True, "pbrMetallicRoughness": x}
        for x in [
            {"baseColorFactor": [0.52, 0.47, 0.41, 1]},
            {"baseColorTexture": {"index": 0}},
            {"baseColorFactor": [0.38, 0.38, 0.36, 1]},
        ]
    ]
    if name == "McDonald's" and not sign_size:
        mats.extend(
            {
                "doubleSided": True,
                "pbrMetallicRoughness": {"baseColorFactor": [*rgb, 1]},
            }
            for rgb in MATERIALS
        )
    images = []
    if painted:
        # Detailed geometry uses its atlas. The legacy single-panel art is unused.
        mats[1] = {
            "doubleSided": True,
            "pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1]},
        }
    else:
        buf = io.BytesIO()
        storefront(name).save(buf, "PNG")
        images.append({"bufferView": view(buf.getvalue()), "mimeType": "image/png"})
    for painting in painted:
        raw = io.BytesIO()
        painting.save(raw, "PNG")
        mats.append(
            {
                "doubleSided": True,
                "pbrMetallicRoughness": {"baseColorTexture": {"index": len(images)}},
            }
        )
        images.append({"bufferView": view(raw.getvalue()), "mimeType": "image/png"})
    gltf = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": primitives}],
        "buffers": [{"byteLength": len(data)}],
        "bufferViews": views,
        "accessors": access,
        "materials": mats,
        "textures": [{"source": i} for i in range(len(images))],
        "images": images,
    }
    js = json.dumps(gltf).encode()
    js += b" " * (-len(js) % 4)
    data.extend(bytes(-len(data) % 4))
    glb = (
        struct.pack("<4sIII4s", b"glTF", 2, 28 + len(js) + len(data), len(js), b"JSON")
        + js
        + struct.pack("<I4s", len(data), b"BIN\0")
        + data
    )
    positions, _, triangles, *_ = threedmr.gltf_mesh(glb)
    # The game writer recenters geometry using its full bounds, including canopies.
    # Translate its anchor by the same amount to retain the original footprint location.
    midpoint = (positions[:, :2].min(axis=0) + positions[:, :2].max(axis=0)) / 2
    center = center + midpoint / [111320 * math.cos(math.radians(center[1])), 111132]
    filename = f"store_{ident}.glb"

    with NamedTemporaryFile(dir=out, delete=False) as staged:
        staged.write(glb)
    Path(staged.name).replace(out / filename)
    print(name, ident, center, len(triangles))
    return dict(
        storefront_source=path.name,
        **({"storefront_overlay": True} if overlay else {}),
        **({"storefront_pylon": True} if sign_size else {}),
        file=filename,
        title=name,
        branded_storefront=True,
        lat=center[1],
        lon=center[0],
    )


def main():
    parser = argparse.ArgumentParser(
        description="Generate storefront models from OpenStreetMap XML extracts."
    )
    parser.add_argument(
        "extracts",
        nargs="+",
        type=Path,
        help="OSM XML extracts containing building ways and their nodes",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--wall-observation",
        action="append",
        type=Path,
        default=[],
        help="Observation JSON or directory of observation JSON files; repeat to combine collections",
    )
    args = parser.parse_args()
    observations = {}
    observation_files = set()
    for location in args.wall_observation:
        if location.is_dir():
            observation_files.update(location.glob("*.json"))
        else:
            observation_files.add(location)
    for observation_path in sorted(observation_files):
        record = json.loads(observation_path.read_text(encoding="utf-8"))
        if record["material"] == "brick":
            observations.setdefault(str(record["way"]), []).append(record)
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    catalog_path = out / "local_models.json"
    existing = json.loads(catalog_path.read_text()) if catalog_path.exists() else []
    sources = args.extracts
    source_names = {path.name for path in sources}
    if len(source_names) != len(sources):
        parser.error("Each extract in a build needs a distinct filename")
    catalog_by_file = {
        entry["file"]: entry
        for entry in existing
        if entry.get("storefront_source") not in source_names
    }
    seen = set()

    for path in sources:
        for match in matches(path):
            ident = match[1]
            if ident in seen:
                continue
            entry = export_store(path, match, observations, out)
            if entry:
                seen.add(ident)
                catalog_by_file[entry["file"]] = entry
    with NamedTemporaryFile(
        mode="w", dir=out, delete=False, encoding="utf-8"
    ) as staged:
        json.dump(list(catalog_by_file.values()), staged, indent=2)
    Path(staged.name).replace(catalog_path)


if __name__ == "__main__":
    main()
