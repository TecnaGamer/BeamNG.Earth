"""Capture the frozen input of server/tests/test_seamprobe.py: a place's bridge deck parts and the ground under them.

    python tools/capture_seamprobe_fixture.py NAME LAT LON R CACHE_DIR

Reads CACHE_DIR offline (a work copy of a seeded cache, e.g. tools/roadcorpus's ROOT/seed/<place>: the server writes its
derived caches there) and writes server/tests/fixtures/seamprobe_NAME.npz:
  * every bridge part the server's BridgeProfiles (blockworker._bridges, the anchor at LAT LON) gives for the box of
    half-side R round the point: id, Spec, solved (x, y, z) in the point's ENU frame and lon, exactly (float64);
  * the ground the decks' abutments and pads read (BridgeProfiles._ground: graded, ROAD_LIFT) on a GROUND_STEP_M grid
    over the parts' box plus GROUND_PAD_M, as whole centimetres (row steps int16).
These are the sheet code's inputs (roads._deck_chunks, deck_pads, roadmesh.deck), not its output: the test builds the
tiles from them. Run it with BNE_ROADSHAPE=sheet (the switch the test turns on). With BNE_ROADSHAPE=profile (road
revamp v2 P3e) the parts are at their chains' joint profile and the fixture also keeps "profiled": the ids of the parts
BridgeProfiles.profiled_ids gives (the test's profile class builds the tiles as BNE_ROADSHAPE=profile does).
"""
import argparse
import logging
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

GROUND_STEP_M = 4.0
GROUND_PAD_M = 64.0


def plain(v):
    """A Spec field as Python literals (tuples kept), for repr / ast.literal_eval."""
    if isinstance(v, (tuple, list)):
        return tuple(plain(x) for x in v)
    if isinstance(v, np.generic):
        return v.item()
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("r", type=float)
    ap.add_argument("cache")
    ap.add_argument("--ground-m", type=float, default=None,
                    help="half-side of the box round the point the ground is kept for (default R; the test clamps "
                         "beyond it: only the tiles it builds read the ground)")
    a = ap.parse_args()
    import blockworker
    import geodesy
    import grading
    import roads
    blockworker.init(a.cache, True, True, True, True, False, None, logging.ERROR, False, True)
    s = blockworker._s
    g, _ = s.anchor(1, a.lat, a.lon)
    frame = geodesy.EnuFrame(a.lat, a.lon)

    def elev(la, lo):
        z = blockworker.ground(frame, la, lo, s.dem.sample(la, lo, 15))
        return g.apply(la, lo, z, grading.ROAD_LIFT) if g is not None else z
    br = blockworker._bridges(1, frame, elev)
    keys = set()
    for tx, ty in roads.tiles_near(0.0, 0.0, a.r):
        keys.update(roads.cells_of_tile(frame, tx, ty))
    feats = [s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads") for k in sorted(keys)]
    parts = [(str(f), sp, np.c_[xy, z], float(lon)) for f, sp, xy, z, lon in br.parts(feats, (-a.r, -a.r, a.r, a.r))
             if np.isfinite(z).all()]
    if not parts:
        sys.exit("no bridge parts")
    allxy = np.concatenate([p[2][:, :2] for p in parts])
    gm = a.r if a.ground_m is None else a.ground_m
    allxy = np.clip(allxy, -gm, gm)
    lo_ = np.floor((allxy.min(0) - GROUND_PAD_M) / GROUND_STEP_M) * GROUND_STEP_M
    hi_ = np.ceil((allxy.max(0) + GROUND_PAD_M) / GROUND_STEP_M) * GROUND_STEP_M
    gx = np.arange(lo_[0], hi_[0] + 1e-9, GROUND_STEP_M)
    gy = np.arange(lo_[1], hi_[1] + 1e-9, GROUND_STEP_M)
    X, Y = np.meshgrid(gx, gy)
    z = np.asarray(br._ground(np.c_[X.ravel(), Y.ravel()]), np.float64).reshape(X.shape)
    q = np.rint(z * 100.0).astype(np.int64)
    steps = np.diff(q, axis=1)
    if steps.size and np.abs(steps).max() >= 32000:
        sys.exit("ground steps do not fit int16")
    out = ROOT / "server" / "tests" / "fixtures" / f"seamprobe_{a.name}.npz"
    import roadshape
    prof = {}
    if roadshape.PROFILE_BUILT:
        got = getattr(br, "profiled_ids", None)
        ids = sorted(str(f) for f in (got() if got is not None else ()) or ())
        prof["profiled"] = np.array(ids if ids else [""])
    np.savez_compressed(
        out, fid=np.array([p[0] for p in parts]),
        spec=np.array([repr({k: plain(v) for k, v in p[1]._asdict().items()}) for p in parts]),
        n=np.array([len(p[2]) for p in parts], np.int32), xyz=np.concatenate([p[2] for p in parts]),
        lon=np.array([p[3] for p in parts]), place=np.array([a.lat, a.lon, a.r]),
        ground_origin=np.array([gx[0], gy[0], GROUND_STEP_M]), ground_first=q[:, 0].astype(np.int32),
        ground_steps=steps.astype(np.int16), **prof)
    print(out, os.path.getsize(out), "bytes,", len(parts), "parts, ground", X.shape)


if __name__ == "__main__":
    main()
