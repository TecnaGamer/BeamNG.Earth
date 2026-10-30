#!/usr/bin/env python3
"""deckprobe3d.py LAT LON R [--server DIR] [--json OUT] [--cache DIR]: the bridge decks the server plans round a
place (the road tiles' BridgeProfiles, grading's approaches) against the real ones a source measured (server/roadz.py:
US 3DEP lidar along each bridge, IGN BD TOPO in France, NVDB in Norway), per deck:
  ours / real     the planned and the measured deck heights (anchor frame, on our DEM's datum)
  err             mean |ours - real| and the worst, over the stations both have (m), bias: mean (ours - real)
  over_ground     the real deck's highest point over our ungraded ground (m): how much of a bridge it is (a tunnel's
                  floor, "T": its lowest, under the ground)
  clear           at each ground road crossing under it: real deck - the graded road there, and ours - the same
Then a summary (median / 90th percentile of the errors, clearances under 4.5 m). The plan follows the server's
switches: run it with BNE_ROADZ=off (or BNE_LIDAR_DECKS=off) for today's inference, without for the override; the
measurement is made either way (docs/research/2026-10-28-3d-road-data.md has the numbers).

    cd server && python3 ../tools/deckprobe3d.py 34.0617 -118.2468 400
"""
import argparse
import json
import logging
import multiprocessing
import os
import sys

ap = argparse.ArgumentParser()
ap.add_argument("lat", type=float)
ap.add_argument("lon", type=float)
ap.add_argument("r", type=float)
ap.add_argument("--server", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
ap.add_argument("--cache", default=os.path.expanduser("~/.cache/beamng-earth"))
ap.add_argument("--json")
ap.add_argument("--min-len", type=float, default=20.0)
args = ap.parse_args()
sys.path.insert(0, args.server)

import numpy as np                      # noqa: E402
import blockworker                      # noqa: E402
import geodesy                          # noqa: E402
import roads                            # noqa: E402
import roadz                            # noqa: E402
import grading as gradingmod            # noqa: E402

lat, lon, R = args.lat, args.lon, args.r
blockworker.init(args.cache, False, True, True, True, False, multiprocessing.Queue(1), logging.ERROR, True, True)
s = blockworker._s
g, _ = s.anchor(1, lat, lon)
frame = geodesy.EnuFrame(lat, lon)
# the measurement: a provider of its own, on whatever the switches say (the plan uses the server's)
meas = roadz.RoadZ(args.cache, s.md.fetcher, s.md)
meas.enabled, meas.lidar = True, True


def elev(la, lo):
    z = blockworker.ground(frame, la, lo, s.dem.sample(la, lo, 15))
    return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z


def up(xy, zgeo):
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
    return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(zgeo, np.float64))[2], np.float64)


def ground_up(xy, graded=True):
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
    z = elev(la, lo) if graded else s.dem.sample(la, lo, 15)
    return up(xy, z)


def ll(p):
    la, lo, _ = frame.enu_to_geodetic(np.array([p[0]]), np.array([p[1]]), 0.0)
    return round(float(la[0]), 6), round(float(lo[0]), 6)


bridges = blockworker._bridges(1, frame, elev)
tunnels = blockworker.tunnel_profiles(1, frame, elev, g)
decks, ground, seen = {}, [], set()
for tx, ty in roads.tiles_near(0.0, 0.0, R):
    keys = roads.cells_of_tile(frame, tx, ty)
    feats = [s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads") for k in keys]
    for level, prof in ((1, bridges), (-1, tunnels)):
        for fid, spec, xy, z, _ in prof.parts(feats, roads.tile_bbox(tx, ty)):
            xy = np.asarray(xy, np.float64)[:, :2]
            if np.hypot(*xy.T).min() > R or (level < 0 and str(fid) in tunnels.open):
                continue
            decks[(str(fid), round(xy[0, 0]), round(xy[0, 1]))] = (xy, np.asarray(z, np.float64), spec, level)
    for f in feats:
        if f is None:
            continue
        specs = roads.road_specs(f)
        for i in range(len(f)):
            fid = str(f.ids[i])
            if fid in seen or specs[i] is None:
                continue
            seen.add(fid)
            for ring in roads.ground_parts(f, i):
                e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                gl = np.stack([np.asarray(e), np.asarray(n)], 1)
                if len(gl) >= 2 and np.hypot(*gl.T).min() < R + 100:
                    ground.append((fid, specs[i].cls, gl))

rows = []
for (fid, *_), (xy, z, spec, level) in sorted(decks.items()):
    arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    if arc[-1] < args.min_len:
        continue
    info = {}
    real = meas.observed(frame, xy, spec.width / 2, level, "", info)
    row = {"fid": fid[:12] + ("" if level > 0 else " T"), "cls": spec.cls, "len": round(float(arc[-1]), 1),
           "at": ll(xy[len(xy) // 2]), "level": level,
           "source": info.get("source"), "cover": round(float(info.get("cover", 0.0)), 2)}
    if real is not None:
        ok = np.isfinite(real)
        zr = np.full(len(xy), np.nan)
        zr[ok] = up(xy[ok], real[ok])
        d = z[ok] - zr[ok]
        graw = ground_up(xy, graded=False)
        pick = np.nanmax if level > 0 else np.nanmin          # (a tunnel: its deepest point under the ground)
        row.update(err=round(float(np.abs(d).mean()), 2), err_max=round(float(np.abs(d).max()), 2),
                   bias=round(float(d.mean()), 2), over_ground=round(float(pick(zr - graw)), 1),
                   ours_over_ground=round(float(pick(z - graw)), 1))
        cl = []
        for gi, cr in enumerate(roads._crossings_by([xy], [gl for _f, _c, gl in ground])):
            for _pi, a, pt in cr:
                if a < 8.0 or a > arc[-1] - 8.0:
                    continue
                zg = float(ground_up(np.asarray([pt], np.float64))[0])
                zre = float(np.interp(a, arc[ok], zr[ok])) if ok.sum() >= 2 else np.nan
                cl.append((round(zre - zg, 2), round(float(np.interp(a, arc, z)) - zg, 2), ground[gi][1]))
        row["clear"] = sorted(cl)[:4]
    rows.append(row)

meas_rows = [r for r in rows if "err" in r]
print(f"place {lat},{lon} R {R}: {len(rows)} decks / tunnels >= {args.min_len:.0f} m, {len(meas_rows)} measured "
      f"(roadz {'on' if roadz.PROVIDER is not None and roadz.PROVIDER.enabled else 'off'}, "
      f"lidar decks {'on' if roadz.LIDAR_ON else 'off'})")
for r in sorted(meas_rows, key=lambda r: -r["err_max"]):
    print(f"  {r['fid']:12s} {r['cls']:13s} {r['len']:6.0f} m cover {r['cover']:.2f}  err {r['err']:5.2f} max "
          f"{r['err_max']:5.2f} bias {r['bias']:+5.2f}  real over ground {r['over_ground']:5.1f} (ours "
          f"{r['ours_over_ground']:5.1f})  clear real/ours {r['clear']}  {r['at']}")
if meas_rows:
    e = np.array([r["err"] for r in meas_rows])
    em = np.array([r["err_max"] for r in meas_rows])
    lowr = sum(1 for r in meas_rows for c in r["clear"] if np.isfinite(c[0]) and c[0] < 4.5)
    lowo = sum(1 for r in meas_rows for c in r["clear"] if c[1] < 4.5)
    ncl = sum(len(r["clear"]) for r in meas_rows)
    print(f"summary: mean err median {np.median(e):.2f} m, p90 {np.percentile(e, 90):.2f} m; worst err median "
          f"{np.median(em):.2f} m, p90 {np.percentile(em, 90):.2f} m; crossings {ncl}: under 4.5 m real {lowr}, "
          f"ours {lowo}")
st = meas.stats
print(f"measured cells built {st['cells']} in {st['seconds']:.1f} s: {st['requests']} requests, "
      f"{st['bytes'] / 1e6:.1f} MB")
if args.json:
    json.dump({"place": [lat, lon, R], "rows": rows}, open(args.json, "w"))
