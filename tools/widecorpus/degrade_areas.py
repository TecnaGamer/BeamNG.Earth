"""OSM's mapped carriageway areas (area:highway=*) round a corpus place, for the degradation test's edge error against
the mapped road (degrade_metrics._area_stats), read from a staged planet build on disk (WIDE_AREAS_DIR; nothing is
fetched). Data (c) OpenStreetMap contributors, ODbL 1.0."""
import os
from pathlib import Path
import zlib

import numpy as np

from common import read

# a staged build of tools/build_osm_road_areas.py (branch claude-areahighway: manifest.json and its PMTiles parts of
# OAH1 tiles at zoom 14), read from disk
AREAS_DIR = Path(os.environ.get("WIDE_AREAS_DIR") or Path.home() / "bnge-tests/areahighway/build/planet")
ZOOM = 14
HALF_M = 400.0
# carriageway surfaces (the drawn road): the driving classes and their lanes, not footways, islands or parking
ROAD_VALUES = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "service",
               "living_street", "road", "busway", "bus", "motorway_link", "trunk_link", "primary_link",
               "secondary_link", "tertiary_link", "junction", "emergency", "shoulder", "access_road"}


def _uv(b, i):
    v = s = 0
    while True:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << s
        if c < 0x80:
            return v, i
        s += 7


def _zz(v):
    return (v >> 1) ^ -(v & 1)


def decode_tile(blob):
    """[(area:highway value, flags, [(role, (k, 2) lat / lon)])] of an OAH1 tile (the build's docstring)."""
    if blob[:4] != b"OAH1":
        raise ValueError("not an OAH1 tile")
    i = 4
    n, i = _uv(blob, i)
    texts = []
    for _ in range(n):
        ln, i = _uv(blob, i)
        texts.append(blob[i:i + ln].decode("utf-8"))
        i += ln
    count, i = _uv(blob, i)
    out = []
    for _ in range(count):
        _key, i = _uv(blob, i)
        value, i = _uv(blob, i)
        for _t in range(3):
            _, i = _uv(blob, i)
        _layer, i = _uv(blob, i)
        flags = blob[i]
        i += 1
        nl, i = _uv(blob, i)
        for _ in range(nl):
            _, i = _uv(blob, i)
        nr, i = _uv(blob, i)
        rings = []
        for _ in range(nr):
            role = blob[i]
            i += 1
            k, i = _uv(blob, i)
            xy = np.zeros((k, 2), np.int64)
            px = py = 0
            for j in range(k):
                dx, i = _uv(blob, i)
                dy, i = _uv(blob, i)
                px += _zz(dx)
                py += _zz(dy)
                xy[j] = px, py
            rings.append((role, np.column_stack((xy[:, 1], xy[:, 0])) / 1e7))
        out.append((texts[value], flags, rings))
    return out


def _tile_of(lat, lon, z=ZOOM):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - np.log(np.tan(np.radians(lat)) + 1 / np.cos(np.radians(lat))) / np.pi) / 2.0 * n)
    return x, y


def fetch(p):
    """The road-area polygons round a place from the staged build: [(value, flags, rings)] (deduplicated across
    tiles), or None without the build."""
    import fast
    manifest = AREAS_DIR / "manifest.json"
    if not manifest.is_file():
        return None
    parts = read(manifest)["parts"]
    lat, lon = p["lat"], p["lon"]
    dlat = HALF_M / 111320.0
    dlon = HALF_M / (111320.0 * max(0.1, float(np.cos(np.radians(lat)))))
    x0, y1 = _tile_of(lat - dlat, lon - dlon)
    x1, y0 = _tile_of(lat + dlat, lon + dlon)
    got, seen = [], set()
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            t = fast._ef.pmt_tile_id(ZOOM, x, y)
            part = next((q for q in parts if q["first"] <= t <= q["last"]), None)
            if part is None:
                continue
            blob = _pm_tile(AREAS_DIR / part["archive"], t)
            if blob is None:
                continue
            for value, flags, rings in decode_tile(blob):
                key = (value, flags, tuple(rings[0][1][:2].round(7).ravel().tolist()) if rings else ())
                if key not in seen:
                    seen.add(key)
                    got.append((value, flags, rings))
    return got


def _pm_tile(path, tile_id):
    import fast
    with open(path, "rb") as f:
        head = f.read(16384)
        h = fast._ef.pmt_header(head)
        d = head[h["root_offset"]:h["root_offset"] + h["root_length"]]
        for _ in range(4):
            e = fast._ef.pmt_find(d, tile_id)
            if e is None:
                return None
            off, ln, leaf = e
            if not leaf:
                f.seek(h["data_offset"] + off)
                raw = f.read(ln)
                return zlib.decompress(raw, -15) if h["tile_compression"] in (0, 1) else zlib.decompress(raw, 31)
            f.seek(h["leaf_offset"] + off)
            d = f.read(ln)
    return None


def _rings(members):
    """Closed rings from way geometries ([(lat, lon)] each), joined end to end."""
    segs = [list(m) for m in members if len(m) >= 2]
    rings = []
    while segs:
        ring = segs.pop(0)
        changed = True
        while ring[0] != ring[-1] and changed:
            changed = False
            for i, s in enumerate(segs):
                if s[0] == ring[-1]:
                    ring += s[1:]
                elif s[-1] == ring[-1]:
                    ring += s[::-1][1:]
                elif s[-1] == ring[0]:
                    ring = s[:-1] + ring
                elif s[0] == ring[0]:
                    ring = s[::-1][:-1] + ring
                else:
                    continue
                segs.pop(i)
                changed = True
                break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def polygons(data, frame):
    """[(outer (k, 2), [holes])] in frame's metres of the road-surface areas (fetch's records)."""
    out = []

    def enu(ring):
        a = np.asarray(ring, np.float64)
        x, y, _ = frame.geodetic_to_enu(a[:, 0], a[:, 1], 0.)
        return np.column_stack((x, y))
    for value, _flags, rings in data or ():
        if value not in ROAD_VALUES:
            continue
        holes = [enu(r) for role, r in rings if role == 1]
        for role, r in rings:
            if role == 0 and len(r) >= 4:
                out.append((enu(r), holes))
    return out


def areas_for(p, out):
    """{tile key: polygons} for the place's tiles (out/D0/tiles.json), or None without mapped road areas."""
    import geodesy
    data = fetch(p)
    polys = polygons(data, geodesy.EnuFrame(p["lat"], p["lon"])) if data else []
    if not polys:
        return None
    keys = [t["key"] for t in read(Path(out) / "D0" / "tiles.json")]
    return {k: polys for k in keys}
