"""Capture an offline replay of a reported interchange: its roads cells and the elevation round it.

    python tools/capture_interchange_fixture.py NAME LAT LON CACHE_DIR [--cells-m M]
    python tools/capture_interchange_fixture.py --repack FILE.npz     (an older capture in this format)

Fetches what is missing into CACHE_DIR (a cache of its own: the map data and DEM tiles go there, nothing else is
written) and writes server/tests/fixtures/interchange_NAME.npz:
  * the "roads" layer of every z14 cell within --cells-m (default 1200 m) of the point (Overture, as the server reads
    it: mapgeom.Features arrays, prefix cell<i>.),
  * the elevation the server's Dem.sample gives at zoom dem.FINE_ZOOM on two grids in the point's ENU frame: FINE_STEP_M
    within FINE_M, COARSE_STEP_M within COARSE_M (the planner plans whole cells: it asks far from the point too).
These are inputs (raw elevation, before road grading), not solved heights. tests/interchanges.py replays them.
Stored compactly: the heights as whole centimetres, each row's steps (int16 unless one does not fit); the features
without their cell-frame ENU copy (Features.localize makes it again) and with "" for a missing text value (no
pickled object arrays).
"""
import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import dem as demmod  # noqa: E402
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import mapdata as mapdatamod  # noqa: E402
import netfetch  # noqa: E402

FINE_M, FINE_STEP_M = 500.0, 2.0
COARSE_M, COARSE_STEP_M = 3200.0, 16.0


def plain(data):
    """data's object arrays as text ("" for None), its cells' ENU copies left out."""
    out = {}
    for k, v in data.items():
        if k.endswith(".en"):
            continue
        if getattr(v, "dtype", None) == object:
            v = np.array(["" if x is None else str(x) for x in v])
        out[k] = v
    return out


def pack_heights(z):
    """(first column cm (int32), row steps cm (int16 / int32)) of a height grid (m)."""
    q = np.rint(np.asarray(z, np.float64) * 100.0).astype(np.int64)
    steps = np.diff(q, axis=1)
    dt = np.int16 if (np.abs(steps).max() if steps.size else 0) < 32000 else np.int32
    return q[:, 0].astype(np.int32), steps.astype(dt)


def unpack_heights(first, steps):
    """pack_heights' grid back (m, float64)."""
    q = np.concatenate([np.asarray(first, np.int64)[:, None], np.asarray(steps, np.int64)], axis=1)
    return np.cumsum(q, axis=1) / 100.0


def save(out, data):
    data = plain(data)
    for g in ("fine", "coarse"):
        if f"{g}_z" in data:
            data[f"{g}_first"], data[f"{g}_steps"] = pack_heights(data.pop(f"{g}_z"))
    np.savez_compressed(out, **data)


def grid(frame, sample, half, step):
    axis = np.arange(-half, half + 1e-9, step)
    x, y = np.meshgrid(axis, axis)
    la, lo, _ = frame.enu_to_geodetic(x.ravel(), y.ravel(), 0.0)
    z = np.asarray(sample(la, lo), np.float64).reshape(x.shape)
    return axis, z.astype(np.float32)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--repack":
        with np.load(sys.argv[2], allow_pickle=True) as a:
            data = {k: a[k] for k in a.files}
        save(sys.argv[2], data)
        print(f"{sys.argv[2]}: {Path(sys.argv[2]).stat().st_size / 1e6:.2f} MB")
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("cache")
    ap.add_argument("--cells-m", type=float, default=1200.0)
    a = ap.parse_args()
    cache = str(Path(a.cache).resolve())
    net = netfetch.Fetcher()
    md = mapdatamod.MapData(cache, fetcher=net)
    dem = demmod.Dem(str(Path(cache) / "dem"), fetcher=net)
    pad = a.cells_m / 111320.0
    keys = sorted(mapcells.cells_for_points(np.array([a.lat]), np.array([a.lon]), pad,
                                            pad / np.cos(np.radians(a.lat))), key=str)
    data = {"origin": np.array([a.lat, a.lon]), "cells": np.array([str(k) for k in keys]),
            "release": np.array(md.release), "dem_zoom": np.array(demmod.FINE_ZOOM)}
    for i, key in enumerate(keys):
        f = md.cell(key, layers=("roads",)).layers.get("roads")
        n = 0 if f is None else len(f)
        print(f"cell {key}: {n} roads", flush=True)
        if f is not None and n:
            data.update(f.to_arrays(f"cell{i}."))
    frame = geodesy.EnuFrame(a.lat, a.lon)

    def sample(la, lo):
        return dem.sample(la, lo, demmod.FINE_ZOOM)
    data["fine_axis"], data["fine_z"] = grid(frame, sample, FINE_M, FINE_STEP_M)
    data["coarse_axis"], data["coarse_z"] = grid(frame, sample, COARSE_M, COARSE_STEP_M)
    out = ROOT / "server" / "tests" / "fixtures" / f"interchange_{a.name}.npz"
    n = data["fine_z"].size + data["coarse_z"].size
    save(out, data)
    print(f"{out}: {len(keys)} cells, {n} elevation samples, "
          f"{out.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
