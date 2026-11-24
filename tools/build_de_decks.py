#!/usr/bin/env python3
"""Germany's bridge-deck samples: the shards server/dedom.py reads, made once on the build machine from the states'
open surface and terrain models (GitHub #1079: the game never asks a state's service itself).

  python tools/build_de_decks.py cells DEST STATE [S W N E]   resumable: DEST/cells/<state>/<z>_<x>_<y>.npz
  python tools/build_de_decks.py status DEST                  each state's cells built / to build
  python tools/build_de_decks.py shards DEST [KEEP [OUT]]     DEST/dedecks/<x>/<y>.npz + manifest.json (every KEEP-th sample)

STATE is a dedom.SERVICES name (nw, be, bb, mv, he, st: their WCS; by, bw, ni, rp, sn, th, hh, hb, sh: their tiles,
tools/de_dom_tiles.py). A state's cells are those within BORDER_M of its border (BKG's VG2500, fetched once into
DEST/vg2500): a bridge across the border is sampled from both sides, and the shards keep one sample per place.

Each cell's road bridges (the server's own map data, roadz.RoadZ._bridges) are sampled on dedom's 1 m grid across
their width, in requests of at most BOX_M round each bridge (WCS) or from the tiles they fall on. Uses the server's
cache (BNE_CACHE, default ~/.cache/beamng-earth) for the map data; a fresh cache of its own makes a build that another
machine can repeat (the map cells there keep the OSM road tags of whenever they were made).

Needs, besides the server's packages: tifffile and imagecodecs (the states' DOM / DGM GeoTIFFs are compressed:
without imagecodecs every cell with a bridge fails), pyshp (the state borders).
"""
import io
import json
import os
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
sys.path.insert(0, HERE)
import npyguard  # noqa: E402,F401  (first: numpy's AVX-512 kernels held back before anything imports numpy)
import numpy as np  # noqa: E402

import dedom  # noqa: E402

BOX_M = 250.0
DEDUPE_DEG = 5e-6          # samples of two cells (a bridge across their border) closer than this are one
BORDER_M = 1000.0          # a cell this near a state counts as the state's
BLOCK_CELLS = 4            # cells are built in blocks of this many a side (the tiles they share stay decoded)
VG2500 = "https://daten.gdz.bkg.bund.de/produkte/vg/vg2500/aktuell/vg2500_12-31.utm32s.shape.zip"
STATE_CODES = {"sh": "01", "hh": "02", "ni": "03", "hb": "04", "nw": "05", "he": "06", "rp": "07", "bw": "08",
               "by": "09", "sl": "10", "be": "11", "bb": "12", "mv": "13", "sn": "14", "st": "15", "th": "16"}

# the states' hosts, asked by this build alone: a steady two requests a second, no daily budget (the game's default
# policy halves its rate past 5000 a day, which held NRW's build to half speed)
BUILD_HOSTS = ["www.wcs.nrw.de", "isk.geobasis-bb.de", "www.geodaten-mv.de", "inspire-hessen.de",
               "www.geodatenportal.sachsen-anhalt.de", "opengeodata.lgl-bw.de", "download1.bayernwolke.de",
               "dom1.s3.eu-de.cloud-object-storage.appdomain.cloud",
               "dgm1.s3.eu-de.cloud-object-storage.appdomain.cloud", "geobasis-rlp.de",
               "geocloud.landesvermessung.sachsen.de", "geoportal.geoportal-th.de", "daten-hamburg.de",
               "www.daten-hamburg.de", "gdi2.geo.bremen.de", "geodaten.schleswig-holstein.de",
               "raw.githubusercontent.com", "daten.gdz.bkg.bund.de"]


def fetcher():
    import netfetch
    pol = netfetch.HostPolicy(rate=2.0, burst=4.0, max_conc=2, daily_budget=None, timeout=180.0, max_attempts=4,
                              base_delay=2.0, miss_ttl=None)
    policies = dict(netfetch.POLICIES)
    policies.update({h: pol for h in BUILD_HOSTS})
    cache = os.environ.get("BNE_CACHE", os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth"))
    fx = netfetch.Fetcher(stats_dir=os.path.join(cache, "netstats"), policies=policies)
    fx.transport._tls_context = tls_context()
    return fx, cache


# Schleswig-Holstein's download host has sent its certificate without the intermediate (D-TRUST BR CA 1-20-1 2020).
# The intermediate is shipped beside this file (fetched once from D-Trust over HTTPS; the same bytes the host sends now
# and the ones our build had downloaded) and checked against its SHA-256 (of the DER) before it is loaded. It only helps
# build the chain: partial chains are not accepted (Python 3.13's default context accepts them, which would make a
# certificate in the store a trust anchor of its own), so the chain must still end at a root the system trusts.
SH_INTERMEDIATE = os.path.join(HERE, "d-trust-br-ca-1-20-1-2020.pem")
SH_INTERMEDIATE_SHA256 = "199ab2aaafff40401e0a3b7b87ee9964659effa94a1fecbe918ae136e4b4e0a8"


def tls_context(path=SH_INTERMEDIATE, sha256=SH_INTERMEDIATE_SHA256):
    """The build's TLS context: the system's roots, no partial chains, the pinned intermediate for chain building.
    Raises ValueError when the file is not the pinned certificate (nothing is loaded then)."""
    import hashlib
    import ssl
    with open(path, encoding="ascii") as fh:
        der = ssl.PEM_cert_to_DER_cert(fh.read())
    got = hashlib.sha256(der).hexdigest()
    if got != sha256:
        raise ValueError(f"{os.path.basename(path)}: sha256 {got}, not the pinned {sha256}")
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    ctx.load_verify_locations(cadata=der)
    return ctx


def cell_keys(south, west, north, east):
    import mapcells
    a, b = mapcells.cell_of(north, west), mapcells.cell_of(south, east)
    for x in range(min(a.x, b.x), max(a.x, b.x) + 1):
        for y in range(min(a.y, b.y), max(a.y, b.y) + 1):
            yield mapcells.CellKey(a.z, x, y)


def state_paths(dest, fetch):
    """{state code: [matplotlib Path in UTM 32]} of VG2500's states (land and water parts)."""
    import shapefile
    from matplotlib.path import Path
    d = os.path.join(dest, "vg2500")
    os.makedirs(d, exist_ok=True)
    z = os.path.join(d, "vg2500.zip")
    if not os.path.exists(z):
        with open(z + ".part", "wb") as fh:
            fh.write(fetch(VG2500))
        os.replace(z + ".part", z)
    with zipfile.ZipFile(z) as zf:
        stem = next(n for n in zf.namelist() if n.endswith("VG2500_LAN.shp"))[:-4]
        r = shapefile.Reader(shp=io.BytesIO(zf.read(stem + ".shp")), dbf=io.BytesIO(zf.read(stem + ".dbf")),
                             shx=io.BytesIO(zf.read(stem + ".shx")), encoding="utf-8")
        out = {}
        fields = [f[0] for f in r.fields[1:]]
        for sr in r.iterShapeRecords():
            code = str(dict(zip(fields, sr.record))["SN_L"]).zfill(2)
            pts, parts = np.asarray(sr.shape.points), list(sr.shape.parts) + [len(sr.shape.points)]
            for i in range(len(parts) - 1):
                out.setdefault(code, []).append(Path(pts[parts[i]:parts[i + 1]]))
    return out


def state_cells(dest, state, fetch, box=None):
    """The cells of the state (within BORDER_M of it), in blocks of BLOCK_CELLS."""
    import bcproj
    svc = next(s for s in dedom.SERVICES if s["name"] == state)
    keys = list(cell_keys(*(box or svc["box"])))
    paths = state_paths(dest, fetch)[STATE_CODES[state]]
    pts = []
    for k in keys:
        lon0, lat0, lon1, lat1 = k.bounds()
        la = np.array([lat0, lat0, lat1, lat1, (lat0 + lat1) / 2])
        lo = np.array([lon0, lon1, lon0, lon1, (lon0 + lon1) / 2])
        x, y = bcproj.utm(la, lo, 32)
        x, y = np.asarray(x), np.asarray(y)
        # (the cell's box grown by BORDER_M: its corners, edge middles and centre)
        x0, x1, y0, y1 = x.min() - BORDER_M, x.max() + BORDER_M, y.min() - BORDER_M, y.max() + BORDER_M
        xs, ys = np.meshgrid([x0, (x0 + x1) / 2, x1], [y0, (y0 + y1) / 2, y1])
        pts.append(np.stack([xs.ravel(), ys.ravel()], 1))
    allp = np.concatenate(pts) if pts else np.zeros((0, 2))
    inside = np.zeros(len(allp), bool)
    for p in paths:
        inside |= p.contains_points(allp)
    inside = inside.reshape(-1, 9).any(1) if len(allp) else inside
    keys = [k for k, ok in zip(keys, inside) if ok]
    keys.sort(key=lambda k: (k.x // BLOCK_CELLS, k.y // BLOCK_CELLS, k.x, k.y))
    return keys


def cell_path(dest, state, key):
    return os.path.join(dest, "cells", state, f"{key.z}_{key.x}_{key.y}.npz")


def sampler(state, dest, fx):
    """sample(lat, lon) -> (dom, dgm) for the state: its WCS, or its tiles."""
    import netfetch
    svc = next(s for s in dedom.SERVICES if s["name"] == state)
    if "dom" in svc:
        return lambda la, lo: dedom.sample(lambda u: fx.get(u).body, svc, la, lo, BOX_M)
    import de_dom_tiles

    def fetch_range(url, a, b):
        res = fx.get(url, headers={"Range": f"bytes={a}-{b}"})
        total = None
        cr = res.headers.get("content-range", "")
        if "/" in cr and cr.rsplit("/", 1)[1].strip().isdigit():
            total = int(cr.rsplit("/", 1)[1])
        return res.status, res.body, total

    def fetch(url):
        return fx.get(url).body

    s = de_dom_tiles.Sampler(state, svc["zone"], os.path.join(dest, "index"), fetch, fetch_range)
    assert netfetch
    return s.sample


def cells(dest, state, box=None):
    import mapdata
    import roadz
    fx, cache = fetcher()
    md = mapdata.MapData(cache, fetcher=fx)
    rz = roadz.RoadZ(cache, fx, md)
    svc = next(s for s in dedom.SERVICES if s["name"] == state)
    keys = state_cells(dest, state, lambda u: fx.get(u).body, box)
    os.makedirs(os.path.join(dest, "cells", state), exist_ok=True)
    sample = sampler(state, dest, fx)
    t0, done, samples, failed = time.time(), 0, 0, 0
    todo = [k for k in keys if not os.path.exists(cell_path(dest, state, k))]
    print(f"{state}: {len(keys)} cells, {len(keys) - len(todo)} built before", flush=True)
    for k, key in enumerate(todo):
        p = cell_path(dest, state, key)
        try:
            la, lo, dom, dgm = rz.dedom_samples(key, svc, BOX_M, sample=sample)
        except Exception as e:          # (the service or the map data unreachable: this cell again next run)
            failed += 1
            print("cell", key, type(e).__name__, str(e)[:200], flush=True)
            continue
        roads_layer = md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
        if roads_layer is not None and roads_layer.__dict__.get("_bnge_fallback"):
            # (the cell's roads without the run's OSM road tags, a stand-in: its bridges not those; again next run)
            failed += 1
            print("cell", key, "map data without its OSM road tags: again next run", flush=True)
            continue
        ok = np.isfinite(dom)
        with open(p + ".part", "wb") as fh:
            fh.write(dedom.encode(la[ok], lo[ok], dom[ok], dgm[ok]))
        os.replace(p + ".part", p)
        done += 1
        samples += int(ok.sum())
        if done % 20 == 0:
            el = time.time() - t0
            print(f"{state} {k + 1}/{len(todo)} cells, {done} built, {failed} failed, {samples} samples, "
                  f"{el:.0f} s, {el / done:.1f} s a cell", flush=True)
    print(f"{state} cells done {done} failed {failed} samples {samples}", flush=True)
    return failed


def read_cell(p):
    """(lat, lon, dom, dgm) of a cell file: dedom's shard encoding, or the first NRW build's plain arrays."""
    with open(p, "rb") as fh:
        body = fh.read()
    with np.load(io.BytesIO(body), allow_pickle=False) as z:
        if "lat" in z.files:
            return z["lat"], z["lon"], z["dom"], z["dgm"]
    d = dedom.decode(body)
    if not d:
        return tuple(np.zeros(0) for _ in range(4))
    return d["lat"], d["lon"], d["dom"], d["dgm"]


def status(dest):
    fx, _cache = fetcher()
    out = {}
    for s in dedom.SERVICES:
        keys = state_cells(dest, s["name"], lambda u: fx.get(u).body)
        built = sum(os.path.exists(cell_path(dest, s["name"], k)) for k in keys)
        out[s["name"]] = {"cells": len(keys), "built": built}
        print(s["name"], built, "/", len(keys), flush=True)
    with open(os.path.join(dest, "status.json"), "w") as fh:
        json.dump(out, fh, indent=1)
    return out


def write_shards(dest, lat, lon, dom, dgm):
    """Samples held in memory as dedom's shards under dest/<x>/<y>.npz (every DGM_EVERY-th keeps its DGM height):
    a small build's (the tests'); counts."""
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
        os.makedirs(os.path.join(dest, str(x)), exist_ok=True)
        body = dedom.encode(lat[i], lon[i], dom[i], dgm[i])
        with open(os.path.join(dest, str(x), f"{y}.npz"), "wb") as fh:
            fh.write(body)
        files += 1
        total += len(body)
    return {"samples": int(len(lat)), "shards": files, "bytes": total}


def tile_bounds(x, y, zoom=dedom.ZOOM):
    """(south, west, north, east) of a z/x/y tile."""
    import math
    n = 1 << zoom
    lat = lambda t: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * t / n))))  # noqa: E731
    return lat(y + 1), x / n * 360.0 - 180.0, lat(y), (x + 1) / n * 360.0 - 180.0


def thin(n, keep):
    """The samples kept of n when every keep-th is (a shard's size against the decks' accuracy)."""
    return np.arange(n) % keep == 0 if keep > 1 else np.ones(n, bool)


def shards(dest, keep=1, out=None, only=None):
    """DEST/dedecks: one shard per z10 tile, made a tile at a time (all of Germany's samples do not fit in memory):
    each tile reads the cells that reach into it (a bridge's samples run past its cell by up to reach degrees).
    only: the tiles (x, y) to make, all by default."""
    import mapcells
    reach = 0.03
    src = os.path.join(dest, "cells")
    out = out or os.path.join(dest, "dedecks")
    files = []          # (state, path, south, west, north, east)
    for state in sorted(os.listdir(src)):
        d = os.path.join(src, state)
        if os.path.isdir(d):
            # (sorted: two cells' samples at one place keep the first's, and every keep-th of a tile's samples is kept
            # in this order; a folder's listing order is its file system's, another on another machine)
            for name in sorted(os.listdir(d)):
                if name.endswith(".npz") and os.path.getsize(os.path.join(d, name)) > 300:
                    z, x, y = map(int, name[:-4].split("_"))
                    lon0, lat0, lon1, lat1 = mapcells.CellKey(z, x, y).bounds()
                    files.append((state, os.path.join(d, name), lat0, lon0, lat1, lon1))
    tiles = {}
    for f in files:
        for t in dedom.tiles_over(f[2] - reach, f[3] - reach, f[4] + reach, f[5] + reach):
            tiles.setdefault(t, []).append(f)
    states, total, samples, n = set(), 0, 0, 0
    for (x, y), fs in sorted(tiles.items()):
        if only is not None and (x, y) not in only:
            continue
        s, w, nn, e = tile_bounds(x, y)
        parts = [[], [], [], []]
        for f in fs:
            la, lo, dom, dgm = (np.asarray(a, np.float64) for a in read_cell(f[1]))
            k = (la >= s) & (la < nn) & (lo >= w) & (lo < e)
            if k.any():
                states.add(f[0])
                for o, a in zip(parts, (la, lo, dom, dgm)):
                    o.append(a[k])
        if not parts[0]:
            continue
        lat, lon, dom, dgm = (np.concatenate(o) for o in parts)
        key = np.round(lat / DEDUPE_DEG).astype(np.int64) * (1 << 32) + np.round(lon / DEDUPE_DEG).astype(np.int64)
        _, first = np.unique(key, return_index=True)
        first.sort()
        first = first[thin(len(first), keep)]
        lat, lon, dom, dgm = lat[first], lon[first], dom[first], dgm[first]
        if not len(lat):
            continue
        dgm = np.where(np.arange(len(dgm)) % dedom.DGM_EVERY == 0, dgm, np.nan)
        os.makedirs(os.path.join(out, str(x)), exist_ok=True)
        body = dedom.encode(lat, lon, dom, dgm)
        with open(os.path.join(out, str(x), f"{y}.npz"), "wb") as fh:
            fh.write(body)
        n += 1
        total += len(body)
        samples += len(lat)
    got = {"samples": samples, "shards": n, "bytes": total, "keep": keep, "built": time.strftime("%Y-%m-%d"),
           "states": sorted(states), "credit": [v["credit"] for v in dedom.SERVICES if v["name"] in states]}
    with open(os.path.join(out, "manifest.json"), "w") as fh:
        json.dump(got, fh, indent=1)
    print(got, flush=True)
    return got


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "cells":
        sys.exit(1 if cells(sys.argv[2], sys.argv[3],
                            tuple(map(float, sys.argv[4:8])) if len(sys.argv) >= 8 else None) else 0)
    elif len(sys.argv) == 3 and sys.argv[1] == "status":
        status(sys.argv[2])
    elif len(sys.argv) in (3, 4, 5) and sys.argv[1] == "shards":
        shards(sys.argv[2], int(sys.argv[3]) if len(sys.argv) >= 4 else 1, sys.argv[4] if len(sys.argv) >= 5 else None)
    else:
        raise SystemExit(__doc__)
