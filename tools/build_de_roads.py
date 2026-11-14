"""Germany's official road axes (ATKIS Basis-DLM, basemap.de Open Data GeoPackages) as z10 shards for BeamNG.Earth.

    python build_de_roads.py OUT_DIR STATE.gpkg [STATE.gpkg ...]

Reads the layer verkehrslinie_bdlm (no GDAL needed: GeoPackage WKB parsed here) and keeps the road, carriageway and
farm-track axes. Each shard OUT_DIR/<x>/<y>.npz (web-mercator tile, zoom 10, by the line's first point) holds:

    off        int32 (n+1) start of each line in the points
    dlat,dlon  int32 (m)   points in 1e-5 degrees (~1 m), delta-coded: lat = cumsum(dlat), lon = cumsum(dlon);
                           simplified by Douglas-Peucker to SIMPLIFY_M
    width    uint16 (n)    paved width in decimetres, 0 = not measured (path width classes are dropped)
    lanes    uint8  (n)    lanes without hard shoulders, 0 = unknown
    surf     uint8  (n)    SURF code, 0 = unknown
    cls      uint8  (n)    CLS code
    flags    uint8  (n)    1 = divided road's centre axis, 2 = one carriageway of a divided road, 4 = bridge,
                           8 = tunnel, 16 = pedestrian zone, 32 = under construction / disused
    level    int8   (n)    ebene: 0 ground, + above, - below

Several states append to the same shards (border tiles): run them in one call, or the later call overwrites.
"""
import math
import os
import sqlite3
import struct
import sys

import numpy as np

Z = 10
CLS = {"Bundesautobahn": 1, "Bundesstraße": 2, "Landesstraße, Staatsstraße": 3, "Kreisstraße": 4,
       "Gemeindestraße": 5, "Sonstige öffentliche Straße": 6, "Nicht öffentliche Straße": 7,
       "Hauptwirtschaftsweg": 8, "Wirtschaftsweg": 9}
SURF = {"Bitumen, Asphalt": 1, "Beton": 2, "Pflaster": 3, "Gestein, zerkleinert": 4, "Leicht befestigt": 5,
        "Unbefestigt": 6, "Rasengitter": 7, "Platten": 8}
KEEP = ("Strassenachse", "Fahrbahnachse", "Fahrwegachse")
REAL_WIDTH = ("Strassenachse", "Fahrbahnachse")     # Fahrwegachse/WegPfadSteig carry width classes only


def tile(lat, lon):
    n = 1 << Z
    x = int((lon + 180.0) / 360.0 * n)
    r = math.radians(max(-85.0, min(85.0, lat)))
    y = int((1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * n)
    return x, y


ENVELOPE = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


def lines(blob):
    """[[(lon, lat), ...], ...] of a GeoPackage geometry blob (LineString / MultiLineString, any Z/M)."""
    if blob is None or blob[:2] != b"GP":
        return []
    flags = blob[3]
    pos = 8 + ENVELOPE[(flags >> 1) & 7]
    out = []

    def geom(p):
        bo = "<" if blob[p] == 1 else ">"
        t, = struct.unpack_from(bo + "I", blob, p + 1)
        p += 5
        ewkb = t & 0xC0000000
        t &= 0x0FFFFFFF
        base, iso = t % 1000, t // 1000
        dims = 2 + (iso in (1, 2)) + 2 * (iso == 3) + bool(ewkb & 0x80000000) + bool(ewkb & 0x40000000)
        if base == 2:
            n, = struct.unpack_from(bo + "I", blob, p)
            p += 4
            v = struct.unpack_from(bo + "d" * (n * dims), blob, p)
            out.append([(v[i], v[i + 1]) for i in range(0, n * dims, dims)])
            return p + 8 * n * dims
        if base == 5:
            k, = struct.unpack_from(bo + "I", blob, p)
            p += 4
            for _ in range(k):
                p = geom(p)
            return p
        raise ValueError(f"geometry type {t}")

    geom(pos)
    return out


SIMPLIFY_M = 1.5            # Douglas-Peucker tolerance: well inside the axes' own +-3 m


def simplify(ln, tol=SIMPLIFY_M):
    """ln [(lon, lat)] reduced by Douglas-Peucker in local metres."""
    if len(ln) < 3:
        return ln
    k = math.cos(math.radians(ln[0][1])) * 111320.0
    p = np.array([(lon * k, lat * 110540.0) for lon, lat in ln])
    keep = np.zeros(len(p), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(p) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        d = p[b] - p[a]
        n = math.hypot(*d)
        seg = p[a + 1:b] - p[a]
        dist = np.abs(seg[:, 0] * d[1] - seg[:, 1] * d[0]) / n if n > 0 else np.hypot(seg[:, 0], seg[:, 1])
        i = int(np.argmax(dist))
        if dist[i] > tol:
            keep[a + 1 + i] = True
            stack += [(a, a + 1 + i), (a + 1 + i, b)]
    return [q for q, k_ in zip(ln, keep) if k_]


def rows(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    q = ("select objektart, klasse, breite, anzahl, oberflaeche_name, fahrbahn, fahrbahnachse, bauwerk_name, "
         "funktion, zustand, ebene, geom from verkehrslinie_bdlm where objektart in (?,?,?)")
    yield from c.execute(q, KEEP)


def main(out, paths):
    shards = {}
    stats = {"lines": 0, "width": 0, "lanes": 0, "surf": 0}
    for path in paths:
        for art, klasse, breite, anzahl, ofm, fahrbahn, fba, bauwerk, funktion, zustand, ebene, g in rows(path):
            width = int(round(breite * 10)) if breite and art in REAL_WIDTH and 1.0 <= breite <= 60.0 else 0
            lanes = int(anzahl) if anzahl and 1 <= anzahl <= 12 else 0
            flags = (1 if fahrbahn == 2000 and art == "Strassenachse" else 0) | (2 if art == "Fahrbahnachse" else 0)
            flags |= 4 if bauwerk and "Brücke" in bauwerk else 0
            flags |= 8 if bauwerk and "Tunnel" in bauwerk else 0
            flags |= 16 if funktion == 1808 else 0
            flags |= 32 if zustand in (2100, 4000) else 0
            attrs = (width, lanes, SURF.get(ofm, 0), CLS.get(klasse, 0), flags, int(ebene or 0))
            for ln in lines(g):
                if len(ln) < 2:
                    continue
                ln = simplify(ln)
                key = tile(ln[0][1], ln[0][0])
                s = shards.setdefault(key, ([], [], [], []))
                s[0].append(len(s[1]))
                s[1].extend(round(lat * 1e5) for _, lat in ln)
                s[2].extend(round(lon * 1e5) for lon, _ in ln)
                s[3].append(attrs)
                stats["lines"] += 1
                stats["width"] += width > 0
                stats["lanes"] += lanes > 0
                stats["surf"] += attrs[2] > 0
        print(os.path.basename(path), stats, len(shards), "tiles", flush=True)
    for (x, y), (off, lat, lon, attrs) in shards.items():
        a = np.array(attrs, dtype=np.int32).reshape(-1, 6)
        d = os.path.join(out, str(x))
        os.makedirs(d, exist_ok=True)
        np.savez_compressed(os.path.join(d, f"{y}.npz"),
                            off=np.array(off + [len(lat)], dtype=np.int32),
                            dlat=np.diff(np.array(lat, dtype=np.int64), prepend=0).astype(np.int32),
                            dlon=np.diff(np.array(lon, dtype=np.int64), prepend=0).astype(np.int32),
                            width=a[:, 0].astype(np.uint16), lanes=a[:, 1].astype(np.uint8),
                            surf=a[:, 2].astype(np.uint8), cls=a[:, 3].astype(np.uint8),
                            flags=a[:, 4].astype(np.uint8), level=a[:, 5].astype(np.int8))
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(out) for f in fs)
    print("done", stats, len(shards), "tiles", round(size / 1e6, 1), "MB", flush=True)


def merge(out, parts):
    """Join per-state shard folders (each built alone: a big state's lines never share memory with another's) into
    out, concatenating the tiles two states share."""
    tiles = {}
    for part in parts:
        for x in os.listdir(part):
            for f in os.listdir(os.path.join(part, x)):
                tiles.setdefault((x, f), []).append(os.path.join(part, x, f))
    for (x, f), paths in sorted(tiles.items()):
        ds = [dict(np.load(p)) for p in paths]
        if len(ds) == 1:
            got = ds[0]
        else:
            got = {k: np.concatenate([d[k] for d in ds]) for k in ("width", "lanes", "surf", "cls", "flags", "level")}
            pts = [(np.cumsum(d["dlat"]), np.cumsum(d["dlon"])) for d in ds]
            lat, lon = np.concatenate([p[0] for p in pts]), np.concatenate([p[1] for p in pts])
            got["dlat"], got["dlon"] = np.diff(lat, prepend=0).astype(np.int32), np.diff(lon, prepend=0).astype(np.int32)
            offs, base = [], 0
            for d in ds:
                offs.append(d["off"][:-1] + base)
                base += int(d["off"][-1])
            got["off"] = np.concatenate(offs + [np.array([base], np.int32)]).astype(np.int32)
        os.makedirs(os.path.join(out, x), exist_ok=True)
        np.savez_compressed(os.path.join(out, x, f), **got)
    print("merged", len(tiles), "tiles from", len(parts), "states", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "merge":
        merge(sys.argv[2], sys.argv[3:])
    else:
        main(sys.argv[1], sys.argv[2:])
