#!/usr/bin/env python3
"""Germany's bridge-deck samples: the shards server/dedom.py reads, made once on the build machine from the states'
open surface and terrain models (GitHub #1079: the game never asks a state's service itself).

  python tools/build_de_decks.py cells DEST nw [S W N E]   resumable: DEST/cells/<z>_<x>_<y>.npz, one per cell
  python tools/build_de_decks.py shards DEST               DEST/dedecks/<x>/<y>.npz and DEST/dedecks/manifest.json

Each cell's road bridges (the server's own map data, roadz.RoadZ._bridges) are sampled on dedom's 1 m grid across
their width, in requests of at most BOX_M round each bridge (a cell's ~15 MB of whole-km boxes came down to the
bridges themselves). Uses the server's cache (BNE_CACHE, default ~/.cache/beamng-earth) for the map data.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import numpy as np  # noqa: E402

import dedom  # noqa: E402

BOX_M = 250.0
DEDUPE_DEG = 5e-6          # samples of two cells (a bridge across their border) closer than this are one


def cell_keys(south, west, north, east):
    import mapcells
    a, b = mapcells.cell_of(north, west), mapcells.cell_of(south, east)
    for x in range(min(a.x, b.x), max(a.x, b.x) + 1):
        for y in range(min(a.y, b.y), max(a.y, b.y) + 1):
            yield mapcells.CellKey(a.z, x, y)


def cells(dest, state, box=None):
    import mapdata
    import netfetch
    import roadz
    svc = next(s for s in dedom.SERVICES if s["name"] == state)
    cache = os.environ.get("BNE_CACHE", os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth"))
    fetcher = netfetch.Fetcher(stats_dir=os.path.join(cache, "netstats"))
    md = mapdata.MapData(cache, fetcher=fetcher)
    rz = roadz.RoadZ(cache, fetcher, md)
    out = os.path.join(dest, "cells")
    os.makedirs(out, exist_ok=True)
    keys = list(cell_keys(*(box or svc["box"])))
    t0, done, samples = time.time(), 0, 0
    for k, key in enumerate(keys):
        p = os.path.join(out, f"{key.z}_{key.x}_{key.y}.npz")
        if os.path.exists(p):
            continue
        lat, lon = key.center()
        if dedom.service_at(lat, lon) is not svc:
            continue
        try:
            la, lo, dom, dgm = rz.dedom_samples(key, svc, BOX_M)
        except Exception as e:          # (the service or the map data unreachable: this cell again next run)
            print("cell", key, type(e).__name__, e, flush=True)
            continue
        ok = np.isfinite(dom)
        np.savez_compressed(p + ".part.npz", lat=la[ok], lon=lo[ok], dom=dom[ok], dgm=dgm[ok])
        os.replace(p + ".part.npz", p)
        done += 1
        samples += int(ok.sum())
        if done % 20 == 0:
            print(f"{k + 1}/{len(keys)} cells, {done} built, {samples} samples, {rz.stats['requests']} requests, "
                  f"{rz.stats['bytes'] / 1e6:.0f} MB, {time.time() - t0:.0f} s", flush=True)
    print("cells done", done, "samples", samples, "MB", round(rz.stats["bytes"] / 1e6), flush=True)


def write_shards(dest, lat, lon, dom, dgm):
    """The samples as dedom's shards under dest/<x>/<y>.npz (every DGM_EVERY-th keeps its DGM height); counts."""
    key = np.round(lat / DEDUPE_DEG).astype(np.int64) * (1 << 32) + np.round(lon / DEDUPE_DEG).astype(np.int64)
    _, first = np.unique(key, return_index=True)
    first.sort()
    lat, lon, dom, dgm = lat[first], lon[first], dom[first], dgm[first]
    dgm = np.where(np.arange(len(dgm)) % dedom.DGM_EVERY == 0, dgm, np.nan)
    n = 1 << dedom.ZOOM
    tx = ((lon + 180.0) / 360.0 * n).astype(np.int64)
    ty = ((1.0 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2.0 * n).astype(np.int64)
    t = tx * n + ty
    files = total = 0
    for v in np.unique(t):
        i = np.flatnonzero(t == v)
        x, y = divmod(int(v), n)
        d = os.path.join(dest, str(x))
        os.makedirs(d, exist_ok=True)
        body = dedom.encode(lat[i], lon[i], dom[i], dgm[i])
        with open(os.path.join(d, f"{y}.npz"), "wb") as fh:
            fh.write(body)
        files += 1
        total += len(body)
    return {"samples": int(len(lat)), "shards": files, "bytes": total}


def shards(dest):
    src = os.path.join(dest, "cells")
    parts = [[], [], [], []]
    for name in sorted(os.listdir(src)):
        if not name.endswith(".npz") or ".part" in name:
            continue
        with np.load(os.path.join(src, name)) as z:
            for o, k in zip(parts, ("lat", "lon", "dom", "dgm")):
                o.append(z[k])
    lat, lon, dom, dgm = (np.concatenate(o) if o else np.zeros(0) for o in parts)
    out = os.path.join(dest, "dedecks")
    got = write_shards(out, lat, lon, dom, dgm)
    got.update({"built": time.strftime("%Y-%m-%d"), "states": [s["name"] for s in dedom.SERVICES],
                "credit": [s["credit"] for s in dedom.SERVICES]})
    with open(os.path.join(out, "manifest.json"), "w") as fh:
        json.dump(got, fh, indent=1)
    print(got)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "cells":
        cells(sys.argv[2], sys.argv[3], tuple(map(float, sys.argv[4:8])) if len(sys.argv) >= 8 else None)
    elif len(sys.argv) == 3 and sys.argv[1] == "shards":
        shards(sys.argv[2])
    else:
        raise SystemExit(__doc__)
