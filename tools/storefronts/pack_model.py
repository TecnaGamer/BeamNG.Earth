"""Fit a pack's south-facing sample building to an eligible mapped footprint."""
import json
import math
import struct
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
import threedmr


def fitted_store(model, row, identity, path, ident, coords, street, out, match_key):
    origin = (coords.min(0) + coords.max(0)) / 2
    scale = [111320 * math.cos(math.radians(origin[1])), 111132]
    xy = (coords - origin) * scale
    ends = np.roll(xy, -1, axis=0)
    area = sum(xy[:, 0] * ends[:, 1] - xy[:, 1] * ends[:, 0]) / 2
    edges = ends - xy
    lengths = np.linalg.norm(edges, axis=1)
    if min(lengths) < .05 or abs(area) < 4:
        return None
    normals = np.column_stack((edges[:, 1], -edges[:, 0])) * np.sign(area) / lengths[:, None]
    front = normals[np.argmax(np.maximum(0, normals @ street) ** 3 * np.minimum(lengths, 28))]
    right = np.array([-front[1], front[0]])
    inward = -front
    basis = np.column_stack((right, inward))
    box = xy @ basis
    width, depth = np.ptp(box, axis=0)
    if abs(area) < .90 * width * depth:
        return None
    midpoint = basis @ ((box.min(0) + box.max(0)) / 2)
    x, y = right * (width / row["width"]), inward * (depth / row["depth"])
    matrix = [[x[0], 0, -y[0], midpoint[0]], [0, 1, 0, 0],
              [-x[1], 0, y[1], -midpoint[1]], [0, 0, 0, 1]]
    doc, binary = threedmr._parse_glb(model.read_bytes())
    scene = doc["scenes"][doc.get("scene", 0)]
    node = {"matrix": np.array(matrix).T.flatten().tolist(), "children": scene["nodes"]}
    scene["nodes"] = [len(doc["nodes"])]
    doc["nodes"].append(node)
    header = json.dumps(doc, separators=(",", ":")).encode()
    header += b" " * (-len(header) % 4)
    blob = (struct.pack("<4sIII4s", b"glTF", 2, 28 + len(header) + len(binary), len(header), b"JSON") + header
            + struct.pack("<I4s", len(binary), b"BIN\0") + binary)
    points = threedmr.gltf_mesh(blob)[0][:, :2]
    lon, lat = origin + (points.min(0) + points.max(0)) / 2 / scale
    filename = f"store_{ident}.glb"
    with NamedTemporaryFile(dir=out, delete=False) as staged:
        staged.write(blob)
    Path(staged.name).replace(out / filename)
    return dict(file=filename, lat=lat, lon=lon, title=identity.label, branded_storefront=True,
                fictional_identity=identity.key, storefront_style=identity.style, storefront_source=path.name,
                storefront_match=match_key, storefront_variant="pack", storefront_edition=threedmr.STOREFRONT_EDITION)
