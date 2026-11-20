"""V3 deck tile adapter. Profiles and bearing depths come from BridgeProfiles.
The native selector separates the structural levels. lane_surface supplies the
same lane faces, nose geometry and markings as a ground tile."""
import numpy as np

import dnp
import fast
import roadsurf3


def assembly(parts, events, exact, depths, country):
    import roadmesh
    import roads
    ef = fast._ef
    profiles = [p for _, _, p, _ in parts]
    widths = [float(sp.width) for _, sp, _, _ in parts]
    by_id = {roadsurf3._base(fid): sp for fid, sp, _, _ in parts}
    rows_xy = ef.deck_slab_rows(profiles, widths, roadmesh.PARAPET_W)
    roads_ = [(fid, sp, row.reshape(-1, 4)) for (fid, sp, _, _), row in zip(parts, rows_xy)]
    supported = []
    for ev, rows, t in events:
        ids = [roadsurf3._base(s) for s in ev["segs"]]
        main = [p for fid, _, p, _ in parts if roadsurf3._base(fid) in ids[:2]]
        if not main or (ev["kind"] in ("merge", "diverge") and (len(ids) < 3 or ids[2] not in by_id)):
            continue
        pick = ef.deck_event_rows(rows, t, main).reshape(-1, 3 + t.shape[1])
        if len(pick) >= 3:
            supported.append((ev, pick[:, :3], pick[:, 3:]))
    # Mesh past the tile so the free-edge walk never sees the tile cut.
    margin = max(widths) + roadsurf3.END_EXT_M
    lo, hi = np.asarray(exact[:2]) - margin, np.asarray(exact[2:]) + margin
    got = roadsurf3._mesh_surface((*lo, *hi), country, supported, by_id, roads_, [])
    m = got["mesh"]
    v, tri = m["v"], m["tri"]
    if not len(tri):
        return None
    z = ef.deck_surface_z(v, profiles, widths)
    x0, y0, x1, y1 = exact
    quad = np.array([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], np.float64)
    xyz, uv, clipped, src = ef.clip_tris_convex(np.c_[v, z], m["uv"], tri, quad, True)
    face_owner = m["owner"][src]
    groups = {}
    for owner in np.unique(face_owner):
        if owner < 0:
            fid, spec = parts[-1 - owner][:2]
        else:
            event, spec = got["pieces"][owner]
            fid = roadsurf3._base(event["segs"][1])
        look = roads._deck_look(spec)
        depth = depths(fid) if depths is not None else None
        material = (look["m"], look["tl"], look["p"], depth or roads.DECK_FULL_DEPTH)
        groups.setdefault(material, []).append(owner)
    chunks = []
    for (mat, tl, priority, depth), owners in groups.items():
        triangles = clipped[np.isin(face_owner, owners)]
        chunk = roadsurf3._tri_chunk(xyz[:, :2], uv, triangles, mat, tl, priority, "base")
        chunk.update(fixed=True, deck_z=xyz[np.unique(triangles), 2], deck_depth=depth)
        chunks.append(chunk)
    # Physical end lines and tile cuts carry no barrier. Clip each run only
    # after walking the free boundary of the complete deck.
    data, offsets = ef.deck_surface_edges(v, tri, profiles, widths)
    data = data.reshape(-1, 5)
    runs = []
    for a, b in zip(offsets, offsets[1:]):
        full = data[a:b]
        ends = full[[0, -1], :2].tolist()
        for r in roads.clip_polyline(full, *exact):
            runs.append(dict(edge=r[:, :2], z=r[:, 2], out=r[:, 3:5], mid=r[:, :2],
                             inner=r[:, :2] - r[:, 3:5] * roadmesh.PARAPET_W,
                             line=r[:, :2] - r[:, 3:5] * (roadmesh.PARAPET_W * 0.5),
                             ends=ends, nose=False, v=None))
    if chunks:
        chunks[0]["deck_runs"] = runs
    marks = []
    if got["kept"]:
        paint = dict(got, mesh=dict(m, tri=np.empty((0, 3), np.int64)))
        for ch in roadsurf3.surface_chunks(paint, exact, country, parts[0][3]):
            p = np.asarray(ch["n"], np.float64)
            p[:, 2] = ef.deck_surface_z(p[:, :2], profiles, widths)
            marks.append(dict(ch, fixed=True, n=p.tolist()))
    return chunks + marks, got["cut"], got["cut_wide"]


def _profile(part, shape, widths=None):
    """Pass the existing ribbon's actual (possibly skewed or banked) cross-sections to lane_surface."""
    import roadmesh
    import roads
    fid, spec, p, lon = part
    ch = dict(roads._deck_look(spec), n=p, w=spec.width, deck=True, fixed=True, kr=True)
    if widths is not None:
        ch["ws"] = widths
    if shape:
        roads._deck_shape_ends(ch, p, shape, False)
    left, right, _, _, _, z = roadmesh.ribbons([ch])[0]
    mid = (left + right) * 0.5
    axis = (left - right) * 0.5
    dz = np.zeros_like(z)
    if shape and shape.get("bank") is not None:
        bank, _ = roads._bank_on(p, shape["bank"], mid)
        dz = roadmesh.tilted(left, right, z, bank, left) - z
    profile = np.c_[mid, z, axis, dz]
    if roads._travel(spec) < 0:
        profile = profile[::-1].copy()
        profile[:, 3:] *= -1
    return fid, spec, profile, lon


def prepare(records, shapes, exact, context, depths, frame, country, cell_fn, memo=None):
    import roads
    fast.require(553, "deck_surface_groups")
    walks, shift_of, width_of = context
    parts, eligible, sections, keys, spans, source = [], [], [], [], [], []
    # Two cells may supply the same part with millimetre differences in
    # its solved rows. Their iteration order changes across tile windows.
    def order(i):
        fid, sp, p, lon = records[i]
        return roads._sheet_key(fid, p), np.asarray(p, dtype="<f8").tobytes(), lon, repr(sp)
    ordered = sorted(range(len(records)), key=order)
    for i in ordered:
        fid, sp, p, lon = records[i]
        key = roads._sheet_key(fid, p)
        if keys and keys[-1] == key:
            continue
        keys.append(key)
        ok = sp.cls in roadsurf3.V3_CLASSES and sp.kind == "paved" and not walks.get(fid)
        source.append(p)
        w = None
        if ok:
            shift = shift_of(fid, p) if shift_of else None
            p = roads.offset_polyline(p, shift) if shift is not None else p
            w = width_of(fid, p) if width_of else None
        spans.append(w)
        parts.append((fid, sp, p, lon))
        sections.append(shapes[i] if shapes else None)
        eligible.append(bool(ok))
    widths = [part[1].width if w is None else float(max(w)) for part, w in zip(parts, spans)]
    groups = fast._ef.deck_surface_groups(source, widths, eligible, exact)
    offsets = {}
    for group in groups:
        corrections = fast._ef.deck_ramp_offsets(
            [source[i] for i in group], [parts[i][1].width for i in group], [bool(parts[i][1].link) for i in group])
        for i, dz in zip(group, corrections):
            offsets[i] = dz
            if any(dz):
                fid, sp, p, lon = parts[i]
                p = p.copy()
                p[:, 2] += dz
                parts[i] = (fid, sp, p, lon)
    covered, out, skip = {}, [], []
    events = roadsurf3._tile_events(frame, exact, cell_fn, {} if memo is None else memo) if groups else []
    for group in groups:
        members = [_profile(parts[i], sections[i], spans[i]) for i in group]
        result = assembly(members, events, exact, depths, country)
        if result is None:
            continue
        chunks, cut, wide = result
        out.extend(chunks)
        for i in group:
            covered[keys[i]] = (group[0], cut, wide, offsets[i])
        if len(group) > 1:
            ends = [p[at, :3] for i in group for p in [source[i]] for at in (0, -1)]
            for j, q in enumerate(ends):
                if any(dnp.norm(q - u) < 0.01 for u in ends[:j]):
                    skip.append(q.tolist())
    return covered, out, skip
