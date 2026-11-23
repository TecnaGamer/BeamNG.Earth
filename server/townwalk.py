"""Road surface S5. Marshal the tile's mapped pedestrian areas and exclusions for the district mesh."""
import numpy as np

import fast
import mapgeom
import plazas
import roads
import sidewalks
import streetfront
import surface


def junction_sections(corners, runs):
    """Fit the return to the street's measured kerb before snapping adjacent pieces to it."""
    fast.require(561, "sidewalk junction tangent sections")
    branches = {}
    for run in runs:
        if not any(run["j"]) or run.get("mapped") or run.get("median"):
            continue
        width = run["w"]
        curb, outward = sidewalks.run_geometry(run["pts"], run["side"], width)
        ends = branches.setdefault((run["fid"], run["side"]), [])
        for i, cut in zip((0, -1), run["j"]):
            if cut:
                ends.append((curb[i, :2], outward[i], curb[i, :2] + width * outward[i]))
    returns = {}
    for c in corners:
        for i, key in zip((0, -1), c.get("road_sides", ())):
            candidates = branches.get(key)
            if candidates and len(c["outer"]) >= 2:
                cut = (i == 0, i == -1)
                arc, fitted = fast._ef.sidewalk_join_run(c["arc"], 0., 1., cut,
                                                        [(q, n) for q, n, _ in candidates])
                if fitted[i] is not None:
                    outer, paired = fast._ef.sidewalk_join_run(np.asarray(c["outer"], np.float64), 0., 1.,
                                                              cut[::-1], [(b, n) for _, n, b in candidates])
                    if paired[-1 if i == 0 else 0] is not None:
                        c["arc"], c["outer"] = arc, outer.tolist()
                        c["inward"][i] = fitted[i]
            returns.setdefault(key, []).append((c["arc"][i, :2], c["inward"][i]))
    return returns


def paving(frame, tile, md, walks, rings, carriageways, drives, margin, photo_cache, road_chunks):
    import buildings

    tx, ty = tile
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    box = (x0 - margin, y0 - margin, x1 + margin, y1 + margin)
    cells = roads.cells_of_tile(frame, tx, ty, margin + 2)
    land = [md.cell(k, layers=("land_use",), kind="demand").layers.get("land_use") for k in cells]
    areas = []
    def xy(ll):
        return np.column_stack(frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)[:2])

    for ring, holes, surface_value in plazas.polygons(land):
        poly = xy(ring)
        if (poly[:, 0].max() < box[0] or poly[:, 0].min() > box[2] or
                poly[:, 1].max() < box[1] or poly[:, 1].min() > box[3]):
            continue
        look = plazas.paving(surface_value, ring[0, 1], ring[0, 0])
        if surface_value in ("concrete", "concrete:plates", "concrete_plates", "paved"):
            look = "concrete"
        if look is not None and look != plazas.ASPHALT:
            areas.append((poly, [xy(h) for h in holes], look))
    if not areas:
        return None

    photo = buildings.roof_photo(frame, x0, y0, x1, y1, photo_cache, online=False) if photo_cache else None
    ambiguous = [a[0] for a in areas if a[2] == plazas.US_AMBIGUOUS]
    fast.require(561, "town paving materials")
    looks = plazas.us_looks(ambiguous, photo)
    if ambiguous and photo is not None and buildings.brick_share(frame.lat0, frame.lon0) >= .7:
        try:
            colours = photo.colours(ambiguous)
        except (ValueError, OSError):
            colours = ()
        for i, rgb in enumerate(colours):
            if fast._ef.town_brick(rgb, True):
                looks[i] = plazas.BRICK, plazas.BRICK_REPEAT_M
    votes = iter(looks)
    mats = {"concrete": sidewalks.TOWN_MATERIALS.index("m_gm_concrete_plain"), plazas.PAVERS: sidewalks.TOWN_PAVERS, plazas.STONE: sidewalks.TOWN_SETT,
            (plazas.BRICK, plazas.BRICK_REPEAT_M): sidewalks.BRICK}
    rows = [(p, h, mats[next(votes) if look == plazas.US_AMBIGUOUS else look]) for p, h, look in areas]

    layers = land + [f for k in cells for f in md.cell(k, layers=("infrastructure", "water"), kind="demand").layers.values()]
    excluded = streetfront._areas(frame, layers, box, include_paving_holes=False)[1]
    for f in layers:
        if f is None:
            continue
        parking = surface._parking(f)
        if parking is not None:
            for i in np.flatnonzero(parking & (f.kind == mapgeom.POLYGON)):
                excluded.extend(np.column_stack(mapgeom.enu_xy(f, frame, part[0])) for part in f.parts(i) if part)
    # V3 ground base triangles include the widened carriageway at forks.
    # The ordinary streets in a town use their mapped carriageway edges.
    for chunk in road_chunks:
        if chunk.get("tri") and chunk.get("layer") == "base":
            excluded.extend(np.asarray(chunk["xy"], np.float64)[chunk["T"]])
    lanes = [(p, spec.width / 2) for _, spec, p, _ in carriageways]
    lanes.extend((np.asarray(ch["n"], np.float64), ch["w"] / 2) for ch in drives if len(ch.get("n", ())) > 1)
    if rings is None:
        rings = buildings.near_rings(frame, tx, ty, md, margin=margin + 2.)
    coverage = streetfront._coverage(box, walks, rings, excluded, lanes)
    fast.require(561, "raised pedestrian areas")
    pos, tri, mat, edges = fast._ef.town_paving(box, rows, coverage._c)
    if len(tri):
        return {"town": {"pts": np.column_stack((pos, np.zeros(len(pos)))), "tri": tri, "mat": mat, "edges": edges}}
