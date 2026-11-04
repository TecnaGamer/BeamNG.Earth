"""bridgewidth.py <server dir> LAT LON R: every road bridge of the area (R m round the point) against the roads it
continues on (2026-10-31, the user: "bridge widths dont always match the road widths"). For each deck feature: its
deck width (roads.bridge_specs, what the deck mesh is built at), its own ground width (roads.road_specs with the land
cover and the measured widths, as a tile's build: what its approaches are drawn at) and the ground widths of the roads
joined at its two ends. Prints the summary and the worst mismatches with lat/lon and why the widths differ."""
import logging, multiprocessing, os, sys
sys.path.insert(0, sys.argv[1])
import numpy as np, blockworker, roads, geodesy
lat, lon, R = float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
cache = os.path.expanduser('~/.cache/beamng-earth')
blockworker.init(cache, False, True, True, True, True, multiprocessing.Queue(1), logging.ERROR, True, True)
s = blockworker._s
frame = geodesy.EnuFrame(lat, lon)
TOL = 1.0       # m of width difference that counts


def why(gs, bs):
    out = []
    if gs.urban and not bs.urban:
        out.append("urban (parking lanes) on the ground only")
    if bool(gs.median) != bool(bs.median):
        out.append("divided carriageway (median) on " + ("the ground only" if gs.median else "the deck only"))
    if gs.lanes != bs.lanes:
        out.append(f"lanes {gs.lanes} vs {bs.lanes}")
    if gs.link != getattr(bs, "link", False):
        out.append("link flag differs")
    return ", ".join(out) or "measured / other"


rows, seen, n_bridges = [], set(), 0
keys = set()
for tx, ty in roads.tiles_near(0.0, 0.0, R):
    keys.update(roads.cells_of_tile(frame, tx, ty))
for key in sorted(keys):
    f = s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
    if f is None or not len(f):
        continue
    bspecs = roads.bridge_specs(f)
    gspecs = roads.road_specs(f, s.surface.urban, s.widths) if "--rural" not in sys.argv else roads.road_specs(f)
    ends = {}
    for i in range(len(f)):
        a = f.coord_off[f.ring_off[f.part_off[i]]]
        b = f.coord_off[f.ring_off[f.part_off[i + 1]]]
        if b - a < 2:
            continue
        for p in (f.lonlat[a], f.lonlat[b - 1]):
            ends.setdefault((round(p[0], 6), round(p[1], 6)), []).append(i)
    for i, bs in enumerate(bspecs):
        fid = str(f.ids[i])
        if bs is None or fid in seen:
            continue
        seen.add(fid)
        a = f.coord_off[f.ring_off[f.part_off[i]]]
        b = f.coord_off[f.ring_off[f.part_off[i + 1]]]
        e, n, _ = frame.geodetic_to_enu(f.lonlat[a:b, 1], f.lonlat[a:b, 0], 0.0)
        if np.hypot(np.asarray(e), np.asarray(n)).min() > R:
            continue
        n_bridges += 1
        gs = gspecs[i]
        cmp = []
        if gs is not None:
            cmp.append(("its own ground part", gs))
        for p in (f.lonlat[a], f.lonlat[b - 1]):
            for j in ends.get((round(p[0], 6), round(p[1], 6)), []):
                if j != i and gspecs[j] is not None and bspecs[j] is None and gspecs[j].cls == bs.cls:
                    cmp.append((f"joined road {str(f.ids[j])[:8]}", gspecs[j]))
        for what, g in cmp:
            d = g.width - bs.width
            if abs(d) >= TOL:
                rows.append((abs(d), fid[:8], bs.cls, round(bs.width, 1), round(g.width, 1), what, why(g, bs),
                             (round(float(f.lonlat[a, 1]), 5), round(float(f.lonlat[a, 0]), 5))))
rows.sort(reverse=True)
print(f"{n_bridges} road bridges within {R:.0f} m; {len(rows)} width mismatches of {TOL} m or more")
reasons = {}
for r in rows:
    reasons[r[6]] = reasons.get(r[6], 0) + 1
for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
    print(f"  {v:4d}  {k}")
for r in rows[:25]:
    print(f"  deck {r[3]:5.1f} m vs {r[4]:5.1f} m ({r[5]}): {r[2]} {r[1]} at {r[7][0]}, {r[7][1]}; {r[6]}")
