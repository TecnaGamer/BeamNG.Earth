"""Build the regional scenery's data set (PR #505: European poles / delineators / signs, Quebec ARRET, French
direction signs) for BeamNG.Earth-data's Pages, from the PR's offline archives:

    python tools/build_regional_data.py SRC_DIR OUT_DIR

SRC_DIR holds country_bounds.zip, quebec_bounds.zip, europe_places.zip, fr_local_directions.zip and
fr_priority_directions-NN.zip (+ fr_priority_directions.json), as PR #505 shipped them in server/data. OUT_DIR gets
the layout server/regionaldata.py reads (served at <PAGES>/regional/, or read straight from BNE_REGIONAL_DIR):

    manifest.json                    {"built", "files": {name: sha256}, "datasets": {name: {"cell", "cells", "meta"}}}
    country_bands.bin, quebec_bands.bin   borders as latitude bands (native/earthfast/src/countrymap.rs's format),
                                     simplified to SIMPLIFY_DEG (~10 m): a process decodes only the bands it asks
    places/X_Y.json.gz, fr_local/X_Y.json.gz, fr_priority/X_Y.json.gz   one cell each (the archives' own cells)

Instead of ~310 MB inside every download, a player who turns the regional scenery on fetches the borders (~10 MB)
and the few cells round where they drive. Deterministic: the same archives give the same bytes.
"""
import gzip
import hashlib
import json
import math
import os
import struct
import sys
import zipfile

import numpy as np

try:
    from compression import zstd as _zstd       # Python 3.14+

    def zcompress(b):
        return _zstd.compress(b, 19)
except ImportError:                             # (older Pythons: the zstandard package)
    import zstandard

    def zcompress(b):
        return zstandard.ZstdCompressor(level=19).compress(b)

SIMPLIFY_DEG = 1e-4         # Douglas-Peucker tolerance (~11 m north-south): a sign's style picked 10 m off a border
BANDS_PER_DEG = 16          # latitude bands (~7 km): what one lookup decodes
MAGIC = b"BNECB\x01\0\0"


def _dp(p, tol):
    """Douglas-Peucker of an open polyline p (n, 2): the kept points (ends always)."""
    n = len(p)
    keep = np.zeros(n, bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        d = p[b] - p[a]
        seg = p[a + 1:b] - p[a]
        length = math.hypot(d[0], d[1])
        dist = (np.abs(seg[:, 0] * d[1] - seg[:, 1] * d[0]) / length) if length > 0 else np.hypot(seg[:, 0], seg[:, 1])
        i = int(np.argmax(dist))
        if dist[i] > tol:
            m = a + 1 + i
            keep[m] = True
            stack += [(a, m), (m, b)]
    return p[keep]


def simplify_ring(r, tol=SIMPLIFY_DEG):
    """A closed ring (n, 2) simplified in two halves (a ring's ends coincide: DP needs a chord), still closed."""
    p = np.asarray(r, np.float64)
    if len(p) <= 8:
        return p
    h = len(p) // 2
    q = np.vstack([_dp(p[:h + 1], tol)[:-1], _dp(p[h:], tol)])
    return q if len(q) >= 4 else p


def band_file(parts, codes, bpd=BANDS_PER_DEG, tol=SIMPLIFY_DEG):
    """parts: [(code, [outer ring, hole rings...])] (rings as [[lon, lat], ...], closed) -> the band file's bytes."""
    code_ix = {c: i for i, c in enumerate(codes)}
    nb = 180 * bpd
    runs = [[] for _ in range(nb)]          # band -> [(ring, [pts])]
    last = [None] * nb                      # band -> (ring, edge index) its last run ended at
    rings, part_codes = [], []
    for pi, (code, part) in enumerate(parts):
        part_codes.append(code_ix[code])
        for hi, ring in enumerate(part):
            p = np.asarray(ring, np.float64).copy()
            p[:, 0] = np.degrees(np.unwrap(np.radians(p[:, 0])))       # (unwrapped across the date line first: a
            p = simplify_ring(p, tol)                                   # simplified edge can span 360 degrees)
            ri = len(rings)
            rings.append((pi, hi > 0, float((p[:, 0].min() + p[:, 0].max()) / 2)))
            q = np.round(p * 1e6).astype(np.int64)
            ylo = np.minimum(q[:-1, 1], q[1:, 1]) / 1e6
            yhi = np.maximum(q[:-1, 1], q[1:, 1]) / 1e6
            k0 = np.clip(np.floor((ylo + 90) * bpd).astype(int), 0, nb - 1)
            k1 = np.clip(np.floor((yhi + 90) * bpd).astype(int), 0, nb - 1)
            for e in range(len(q) - 1):
                for k in range(k0[e], k1[e] + 1):
                    if last[k] == (ri, e - 1):
                        runs[k][-1][1].append(q[e + 1])
                    else:
                        runs[k].append((ri, [q[e], q[e + 1]]))
                    last[k] = (ri, e)
    head = bytearray(MAGIC)
    head += struct.pack("<4I", bpd, len(codes), len(rings), len(part_codes))
    for c in codes:
        head += c.encode("ascii")[:8].ljust(8, b"\0")
    for part, hole, centre in rings:
        head += struct.pack("<IB3xf", part, int(hole), centre)
    head += struct.pack(f"<{len(part_codes)}H", *part_codes)
    table_at = len(head)
    head += bytes(nb * 16)
    frames = []
    off = len(head)
    for k in range(nb):
        if not runs[k]:
            struct.pack_into("<QII", head, table_at + k * 16, 0, 0, 0)
            continue
        raw = bytearray()
        for ring, pts in runs[k]:
            a = np.asarray(pts, np.int64)
            d = np.diff(a, axis=0, prepend=np.zeros((1, 2), np.int64)).astype("<i4")
            raw += struct.pack("<II", ring, len(a)) + d.tobytes()
        z = zcompress(bytes(raw))
        struct.pack_into("<QII", head, table_at + k * 16, off, len(z), len(runs[k]))
        frames.append(z)
        off += len(z)
    return bytes(head) + b"".join(frames)


def world_parts(path):
    """[(code, rings)] of a WorldIndex archive (PR #505's country_bounds.zip / quebec_bounds.zip), in its order."""
    with zipfile.ZipFile(path) as z:
        meta = json.loads(z.read("index.json"))
        return [(p["country"], json.loads(z.read(p["member"]))) for p in meta["parts"]], meta


def _cells(path):
    """(index metadata, {(x, y): rows}) of a cell archive (x_y.json members + index.json)."""
    with zipfile.ZipFile(path) as z:
        meta = json.loads(z.read("index.json"))
        cells = {}
        for n in z.namelist():
            if n == "index.json":
                continue
            x, y = n[:-5].split("_")
            cells[(int(x), int(y))] = json.loads(z.read(n))
    return meta, cells


def _gz(obj):
    return gzip.compress(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 9, mtime=0)


def main(src, out):
    os.makedirs(out, exist_ok=True)
    files, datasets = {}, {}

    def put(name, data):
        p = os.path.join(out, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
        return hashlib.sha256(data).hexdigest()

    for name, archive in (("country_bands.bin", "country_bounds.zip"), ("quebec_bands.bin", "quebec_bounds.zip")):
        parts, meta = world_parts(os.path.join(src, archive))
        codes = sorted({c for c, _ in parts})
        files[name] = put(name, band_file(parts, codes))
        print(name, len(parts), "parts", os.path.getsize(os.path.join(out, name)) // 1000, "kB", flush=True)
    for ds, archives in (("places", ["europe_places.zip"]), ("fr_local", ["fr_local_directions.zip"]),
                         ("fr_priority", sorted(n for n in os.listdir(src)
                                                if n.startswith("fr_priority_directions-") and n.endswith(".zip")))):
        merged, meta = {}, None
        for a in archives:
            m, cells = _cells(os.path.join(src, a))
            meta = meta or m
            for k, rows in cells.items():
                merged.setdefault(k, []).extend(rows)
        for (x, y), rows in sorted(merged.items()):
            put(f"{ds}/{x}_{y}.json.gz", _gz(rows))
        datasets[ds] = {"cell": meta.get("cell", 1), "cells": sorted([x, y] for x, y in merged),
                        "meta": {k: v for k, v in meta.items() if k not in ("cells", "cell")}}
        print(ds, len(merged), "cells", flush=True)
    built = hashlib.sha256(json.dumps([files, datasets], sort_keys=True).encode()).hexdigest()[:16]
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"built": built, "files": files, "datasets": datasets}, fh, separators=(",", ":"))
    print("built", built)




def house_regions(zip_path, out_dir):
    """server/data's French house zones (PR #505's fr_house_regions.zip: departments, the Basque Country, Toulouse)
    as band files fr_house_<member>.bin + fr_house_regions.json (its sources.json: bounds, provenance)."""
    with zipfile.ZipFile(zip_path) as z:
        for member in ("departments", "basque", "toulouse"):
            gj = json.loads(z.read(member + ".geojson"))
            parts = []
            for f in gj["features"]:
                tag = f["properties"]["code"] if member == "departments" else member
                g = f["geometry"]
                for poly in ([g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]):
                    parts.append((str(tag), poly))
            data = band_file(parts, sorted({c for c, _ in parts}))
            with open(os.path.join(out_dir, f"fr_house_{member}.bin"), "wb") as fh:
                fh.write(data)
            print(member, len(parts), "parts", len(data) // 1000, "kB")
        with open(os.path.join(out_dir, "fr_house_regions.json"), "wb") as fh:
            fh.write(z.read("sources.json"))


if __name__ == "__main__":
    if sys.argv[1] == "--house-regions":        # build_regional_data.py --house-regions ZIP OUT_DIR
        house_regions(sys.argv[2], sys.argv[3])
    else:
        main(sys.argv[1], sys.argv[2])


def subset_band_file(data, keep):
    """A band file with only the bands in keep (the others empty): the tests' fixture (server/tests/fixtures/regional),
    a few hundred kB instead of the whole world."""
    bpd, ncodes, nrings, nparts = struct.unpack_from("<4I", data, 8)
    table_at = 24 + ncodes * 8 + nrings * 12 + nparts * 2
    nb = 180 * bpd
    head = bytearray(data[:table_at + nb * 16])
    frames, off = [], len(head)
    for k in range(nb):
        o, clen, nruns = struct.unpack_from("<QII", data, table_at + k * 16)
        if k in keep and nruns:
            frames.append(data[o:o + clen])
            struct.pack_into("<QII", head, table_at + k * 16, off, clen, nruns)
            off += clen
        else:
            struct.pack_into("<QII", head, table_at + k * 16, 0, 0, 0)
    return bytes(head) + b"".join(frames)
