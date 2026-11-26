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
from store_match import chain, matches
from storeidentity import LocalSign, STYLES, identity_for, source_key, variant_for


def directory_rows(rows):
    """Keep mapped suite numbers and resolve each tenant before drawing the board."""
    result = []
    for row in rows:
        unit, source = ("", row) if isinstance(row, str) else row
        identity = identity_for(chain(source) or source, mapped_name=True)
        result.append((unit, identity.label))
    return result


def cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


@whole_store.sign_font_scope()
def export_store(path, match, observations, out, *, entrance_direction=None, local_panel=None, drive_through=None, carhop=False, mapped_name=False, category=None):
    name, ident, coords, street, fixtures, wall_material, *sign_size = match
    if isinstance(name, LocalSign):
        category, name = name.style, name.name
        mapped_name = not str(ident).startswith("poi_")
    identity = identity_for(chain(name) or name, local_panel[0] if local_panel else category or "shop", mapped_name=mapped_name)
    from storefrontpack import active
    pack = active.get()
    row = pack.assets.get(identity.key) if pack else None
    if row and "model" in row and street is not None and not (carhop or drive_through or local_panel):
        from pack_model import fitted_store
        entry = fitted_store(pack.folder / row["model"], row, identity, path, ident, coords,
                             entrance_direction if entrance_direction is not None else street, out, source_key(name))
        if entry:
            return entry
    height = STYLES[identity.style]["height"]
    center = (coords.min(0) + coords.max(0)) / 2
    # The naming switch changes the sign on a category model, independent of its entrance.
    variant = variant_for(identity, center, site_key=source_key(name) if category else None)
    scale = [111320 * math.cos(math.radians(center[1])), 111132]
    xy = (coords - center) * scale
    overlay = street is None
    if not overlay:
        if sum(cross((0, 0), a, b) for a, b in zip(xy, np.roll(xy, -1, axis=0))) < 0:
            xy = xy[::-1]
        edges = np.roll(xy, -1, axis=0) - xy
        lengths = np.linalg.norm(edges, axis=1)
        normals = np.column_stack((edges[:, 1], -edges[:, 0])) / lengths[:, None]
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

    painted = []
    painted_faces = []

    def mesh(points, mat, uv=None):
        if mat >= 3:
            painted_faces.append((points, mat - 3, uv))
        else:
            emit(points, mat, uv)

    def texture(image):
        painted.append(image)
        return 2 + len(painted)

    if carhop:
        whole_store.carhop_row(xy, street, identity, mesh, texture)
    elif overlay:
        a, b = xy
        width = np.linalg.norm(b - a)
        if width < 2 and not sign_size:
            return None
        normal = np.array([b[1] - a[1], a[0] - b[0]]) / width
        axis = (b - a) / width
        # An explicitly mapped unit wall with its own doors gives us the tenant's full frontage.
        half_span = axis * (width/2 if fixtures else min(width * 0.48, 10 if STYLES[identity.style].get("wide_frontage") else 5))
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
            art = texture(whole_store.sign_image(identity, width / (top-bottom), top-bottom, "roadside"))
            for side in (-1, 1):
                left = -side * axis * width/2 + normal * side * .19
                right = side * axis * width/2 + normal * side * .19
                mesh([(*left,bottom), (*right,bottom), (*right,top), (*left,bottom), (*right,top), (*left,top)],
                     art, [(0,1),(1,1),(1,0),(0,1),(1,0),(0,0)])
        elif local_panel:
            style, top, *raised = local_panel
            directory = ()
            if style == "mall_entrance":
                directory, = raised
                raised = []
            if style == "atm":
                half_span = axis * .55
            elif style.startswith("mall_"):
                half_span = axis * min(width*.46, 7.5)
            elif raised:
                half_span = axis * width/2
            span = 2 * np.linalg.norm(half_span)
            tall = min(1.25, span / 4)
            if style.startswith("mall_"):
                tall = min(2.35, span/4)
            if style == "mall_banner":
                art = whole_store.tenant_banner(identity, span/tall, tall)
            else:
                art = whole_store.sign_image(identity, span/tall)
            # A narrow fascia support also locates the model's ground plane for the game writer.
            wall(-half_span, -half_span + axis * .08, 0, top, 2)
            left, right = -half_span, half_span
            bottom = top - tall
            if raised:
                offset = normal * .48
                # The exposed top, sides and rear give the parapet sign depth from the road.
                back = -normal*.35
                corners = [left, left+back, right+back, right]
                for i in range(3):
                    wall(corners[i], corners[i+1], bottom, top, 2)
                mesh([(*corners[k],top) for k in (0,1,2,0,2,3)], 2)
            mesh([(*left,bottom), (*right,bottom), (*right,top), (*left,bottom), (*right,top), (*left,top)],
                 texture(art), [(0,1),(1,1),(1,0),(0,1),(1,0),(0,0)])
            if style == "bank" and drive_through:
                a, b = (np.asarray(drive_through)-center)*scale
                edge = b-a
                outward = np.array([edge[1], -edge[0]])/np.linalg.norm(edge)
                corners = [a, a+5.2*outward, b+5.2*outward, b]
                for p, q in zip(corners, corners[1:]+corners[:1]):
                    wall(p, q, 3.8, 4.12, 2)
                for z, order in ((3.8, (0,2,1,0,3,2)), (4.12, (0,1,2,0,2,3))):
                    mesh([(*corners[k],z) for k in order], 2)
            if style == "mall_entrance":
                door_height = min(3.1, bottom-.2)
                door_span = min(span*.65, 7.2)
                facade = whole_store.elevation(identity, door_span, door_height,
                                              [door_span*.3, door_span*.7], [], [])
                def front_quad(a, b, low, high, art):
                    mesh([(*a,low), (*b,low), (*b,high), (*a,low), (*b,high), (*a,high)],
                         texture(art), [(0,1),(1,1),(1,0),(0,1),(1,0),(0,0)])
                front_quad(-axis*door_span/2, axis*door_span/2, 0, door_height, facade)
                canopy = [left, left+normal*1.6, right+normal*1.6, right]
                for a,b in zip(canopy,canopy[1:]+canopy[:1]):
                    wall(a,b,door_height,door_height+.18,2)
                for z,indices in ((door_height,(2,1,0,3,2,0)),(door_height+.18,(0,1,2,0,2,3))):
                    mesh([(*canopy[k],z) for k in indices],2)
                if directory:
                    # Each sheet keeps the same texture resolution and type scale.
                    # Spread sheets over both jambs before subdividing either board.
                    pages = [directory[i:i+6] for i in range(0,len(directory),6)]
                    board_width = min(2.4,(span-door_span)/2)
                    per_side = (len(pages)+1)//2
                    columns = int(np.ceil(np.sqrt(per_side)))
                    rows = (per_side+columns-1)//columns
                    for i, names in enumerate(pages):
                        side = right if i%2 == 0 else left+axis*board_width
                        row,col = divmod(i//2,columns)
                        edge = side+normal*.04-axis*(columns-col-1)*board_width/columns
                        high = 2.15-row*1.4/rows
                        board = whole_store.directory_image(directory_rows(names))
                        front_quad(edge-axis*board_width/columns,edge,high-1.4/rows,high,board)
            if style == "venue" and top >= 3.4:
                reach = min(1.45, span * .35)
                blade_height = min(.64, reach / 2)
                blade_art = texture(whole_store.sign_image(identity, reach / blade_height))
                root = left + axis * .12
                tip = root + normal * reach
                for side in (-1, 1):
                    a, b = (tip, root) if side == 1 else (root, tip)
                    a, b = a + axis * side * .06, b + axis * side * .06
                    mesh([(*a,top-blade_height), (*b,top-blade_height), (*b,top),
                          (*a,top-blade_height), (*b,top), (*a,top)], blade_art,
                         [(0,1),(1,1),(1,0),(0,1),(1,0),(0,0)])
                wall(tip - axis * .06, tip + axis * .06, top-blade_height, top, 2)
            if len(raised) == 2:
                rows = raised[1]
                board = whole_store.directory_image(directory_rows(rows))
                # Place the shared directory near eye level, beside the entrance.
                hi = min(2.1, bottom-.15)
                lo = hi-.70
                right = half_span + normal*.015
                left = right-axis*min(1.4,span*.42)
                mesh([(*left,lo), (*right,lo), (*right,hi), (*left,lo), (*right,hi), (*left,hi)],
                     texture(board), [(0,1),(1,1),(1,0),(0,1),(1,0),(0,0)])
        else:
            # Ground-floor glazing and an independent fascia with lettering readable across parking.
            panel_width = 2 * np.linalg.norm(half_span)
            panel_doors = sorted(float(((pt - center) * scale) @ axis) + panel_width/2
                                 for pt, dock in fixtures if not dock) or [panel_width/2]
            # Each canopy covers its own door and stops before the neighbouring bay.
            outer = normal * .95
            reach = min(panel_width * .38, 4.5)
            for j, door in enumerate(panel_doors):
                low = (panel_doors[j-1]+door)/2 if j else 0.
                high = (panel_doors[j+1]+door)/2 if j+1 < len(panel_doors) else panel_width
                left = axis * (max(low, door-reach)-panel_width/2)
                right = axis * (min(high, door+reach)-panel_width/2)
                corners = [left, left+outer, right+outer, right]
                for a, b in zip(corners, corners[1:] + corners[:1]):
                    wall(a, b, 2.84, 3.04, 2)
                for z, indices in ((3.04, (0,1,2,0,2,3)), (2.84, (2,1,0,3,2,0))):
                    mesh([(*corners[k], z) for k in indices], 2)
            windows = whole_store.window_bays(STYLES[identity.style]["windows"], panel_width, panel_doors) if fixtures else []
            for bottom, top, image in (
                (0., 3.1, whole_store.elevation(identity, panel_width, 3.1, panel_doors, [], windows)),
                (3.1, 3.1 + min(2.5, panel_width / 4), None),
            ):
                art = image if image is not None else whole_store.sign_image(identity, panel_width / (top-bottom), top-bottom)
                a1, b1 = -half_span, half_span
                points = [(*a1,bottom), (*b1,bottom), (*b1,top), (*a1,bottom), (*b1,top), (*a1,top)]
                mesh(points, texture(art), [(0,1),(1,1),(1,0),(0,1),(1,0),(0,0)])
        center = center + offset / scale
    else:
        points = [((pt - center) * scale, dock) for pt, dock in fixtures]
        material = (wall_material or "").strip().lower()
        photos = []
        if not material:
            for record in observations.get(str(ident), []):
                ends = (np.asarray(record["wall"]) - center) * scale
                photos.append((ends, record.get("color")))
        whole_store.build(
            identity, variant, xy, street, height, points, mesh, texture, material == "brick", photos, entrance_direction,
            use_family_finish=not material,
        )
        for a, b in zip(xy, np.roll(xy, -1, axis=0)):
            wall(a, b, 0, height, 0)
        if drive_through:
            order, pickup = (np.asarray(drive_through) - center) * scale
            delta = pickup-order
            axis = delta / np.linalg.norm(delta)
            outward = normals[np.argmin(np.linalg.norm(xy + np.clip(((pickup-xy)*edges).sum(1)/lengths**2,
                                             0, 1)[:, None]*edges-pickup, axis=1))]
            def panel(point, half, depth, low, high, mat):
                mid = point + depth*outward
                wall(mid-half*axis, mid+half*axis, low, high, mat)
            panel(order, .65, .30, .88, 2.10, 1)
            panel(order, .56, .32, 1.04, 2.00, 2)
            for z in (1.30, 1.53, 1.76):
                for side in (-.25, .25):
                    panel(order+side*axis, .17, .33, z, z+.045, 0)
            panel(pickup, 1.18, .30, .90, 2.28, 1)
            panel(pickup, 1.05, .32, 1.04, 2.16, 2)
            panel(pickup, .035, .34, 1.04, 2.16, 1)
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

    mats = [
        {"doubleSided": True, "pbrMetallicRoughness": x}
        for x in [
            {"baseColorFactor": [0.52, 0.47, 0.41, 1]},
            {"baseColorFactor": [0.62, 0.65, 0.65, 1]},
            {"baseColorFactor": [0.38, 0.38, 0.36, 1]},
        ]
    ]
    images = []
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
    print(identity.label, ident, center, len(triangles))
    return dict(
        storefront_source=path.name,
        **({"carhop_stalls": True} if carhop else {}),
        **({"storefront_overlay": True} if overlay else {}),
        **({"storefront_pylon": True} if sign_size else {}),
        file=filename,
        title=identity.label,
        fictional_identity=identity.key,
        storefront_style=identity.style,
        storefront_edition=threedmr.STOREFRONT_EDITION,
        storefront_match=source_key(name),
        **({"storefront_variant": variant} if not overlay and not carhop else {}),
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
