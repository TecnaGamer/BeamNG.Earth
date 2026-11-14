"""build_station_tags.py PLANET OUT_DIR: OpenStreetMap's fuel and charging stations as small gzip JSON shards the server
reads per area (server/railtags.py StationTags, dataset "stations"), rebuilt weekly from the planet by
TecnaGamer/BeamNG.Earth-data's GitHub Action like the rail, lane and sign shards.

    python3 tools/build_station_tags.py planet-latest.osm.pbf out/
    python3 tools/build_station_tags.py https://.../planet-YYMMDD.osm.pbf out/     (streamed twice by curl: no disk)

Why: the station finder's map lists only the cells whose map data is downloaded, and the EV chargers' count and spots
are guessed (Overture's places and OSM Layercake's POI file have no capacity). Bauer33333 on #716 (2026-11-04): "Almost
3/4 of `amenity=charging_station` on OSM got a `capacity=*` tag, same for `man_made=charge_point`."

Shards: <OUT_DIR>/<x>/<y>.json.gz per z7 tile (SHARD_ZOOM), FORMAT 1: {"s": [[id, kind, lon, lat, name, brand, cap],
...], "p": [[lon, lat], ...]}: every amenity=fuel ("fuel") and amenity=charging_station ("ev") as a node or a way's
centre (id "n123" / "w45"; brand: brand, else operator, else network; cap: capacity's leading integer, or null), and
every man_made=charge_point node (the single chargers), each in the shard of its point; <OUT_DIR>/manifest.json. Two
passes over the planet: the tagged nodes and ways first (their node ids kept), then those nodes' places (the planet's
ways come after its nodes: one streaming pass cannot place them). Data: (c) OpenStreetMap contributors, ODbL 1.0.
"""
import argparse
import gzip
import json
import os
import re
import sys
import time
from collections import defaultdict

import numpy as np
import osmium

import build_rail_tags as rail      # (the planet's streaming, its second pass and the tile maths; the data job fetches it)

FORMAT = 1
SHARD_ZOOM = rail.SHARD_ZOOM
KINDS = {"fuel": "fuel", "charging_station": "ev"}
_LEADING = re.compile(r"^\s*(\d+)")


def capacity_of(text):
    """capacity=* as an integer: its leading number ("2;4" -> 2, "8 (4 dual)" -> 8), None for none ("yes", "")."""
    m = _LEADING.match(str(text or ""))
    return int(m.group(1)) if m else None


def shard_of(lon, lat):
    """The z7 shard (x, y) of a point."""
    x, y = rail.cell_xy(np.float64(lat), np.float64(lon))
    shift = rail.ZOOM - SHARD_ZOOM
    return int(x) >> shift, int(y) >> shift


def _fields(tags):
    """(kind, name, brand, cap) of a station's tags."""
    kind = KINDS[tags.get("amenity")]
    brand = tags.get("brand") or tags.get("operator") or tags.get("network") or ""
    cap = capacity_of(tags.get("capacity")) if kind == "ev" else None
    return kind, tags.get("name") or "", brand, cap


class _Source(rail._Source):
    """rail._Source, but a local file with an OSM extension (.osm, .osm.pbf, .osm.bz2: an extract, the tests'
    fixture) is handed to osmium as it is (rail's opener reads any other name as a PBF pipe)."""

    def __enter__(self):
        if os.path.isfile(self.src) and re.search(r"\.osm(\.pbf|\.bz2|\.gz)?$|\.pbf$", self.src):
            return self.src
        return super().__enter__()


def first_pass(src):
    """(stations [record], ways [(id, (kind, name, brand, cap), node refs)], points [(lon, lat)])."""
    with _Source(src) as f:
        return _first_pass(f)


def second_pass(src, ids):
    """rail.second_pass through this opener."""
    with _Source(src) as f:
        return rail._second_pass(f, ids)


def _first_pass(src):
    stations, ways, points = [], [], []
    fp = osmium.FileProcessor(src, osmium.osm.NODE | osmium.osm.WAY)
    for o in fp.with_filter(osmium.filter.KeyFilter("amenity", "man_made")):
        tags = o.tags
        if o.is_node():
            if tags.get("amenity") in KINDS and o.location.valid():
                stations.append(_record(f"n{o.id}", o.location.lon, o.location.lat, _fields(tags)))
            elif tags.get("man_made") == "charge_point" and o.location.valid():
                points.append((o.location.lon, o.location.lat))
        elif o.is_way() and tags.get("amenity") in KINDS:
            ways.append((o.id, _fields(tags), np.array([n.ref for n in o.nodes], np.int64)))
    return stations, ways, points


def _record(sid, lon, lat, fields):
    kind, name, brand, cap = fields
    return [sid, kind, round(float(lon), 6), round(float(lat), 6), name, brand, cap]


def way_records(ways, nid, nlon, nlat):
    """The ways as records at their nodes' mean (a closed way's last node, its first again, counted once)."""
    out = []
    for wid, fields, refs in ways:
        if len(refs) > 1 and refs[0] == refs[-1]:
            refs = refs[:-1]
        j = np.searchsorted(nid, refs)
        ok = (j < len(nid)) & (nid[np.minimum(j, max(len(nid) - 1, 0))] == refs) if len(nid) else np.zeros(0, bool)
        if not ok.any():
            continue
        j = j[ok]
        out.append(_record(f"w{wid}", nlon[j].mean(), nlat[j].mean(), fields))
    return out


def write(out, stations, points, stats):
    shards = defaultdict(lambda: {"s": [], "p": []})
    for r in stations:
        shards[shard_of(r[2], r[3])]["s"].append(r)
    for lon, lat in points:
        shards[shard_of(lon, lat)]["p"].append([round(float(lon), 6), round(float(lat), 6)])
    total = 0
    for (sx, sy), sh in sorted(shards.items()):
        d = os.path.join(out, str(sx))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{sy}.json.gz")
        sh["s"].sort(key=lambda r: r[0])
        sh["p"].sort()
        body = json.dumps(sh, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        with open(path + ".part", "wb") as fh:          # (mtime 0: the same planet gives the same bytes)
            with gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=9, mtime=0) as gz:
                gz.write(body)
        os.replace(path + ".part", path)
        total += os.path.getsize(path)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"format": FORMAT, "zoom": SHARD_ZOOM, "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "shards": len(shards), **stats, "license": "ODbL 1.0, (c) OpenStreetMap contributors"}, fh)
    print(f"{stats}, {len(shards):,} shards, {total / 1e6:.1f} MB", flush=True)


def build(src, out, second=None):
    """The shards of src (a path, URL or '-'; second: the planet again for the second pass when src is a stream)."""
    t0 = time.time()
    stations, ways, points = first_pass(src)
    print(f"pass 1: {len(stations):,} nodes, {len(ways):,} ways, {len(points):,} charge points, "
          f"{time.time() - t0:.0f} s", flush=True)
    ids = np.unique(np.concatenate([r for _i, _f, r in ways])) if ways else np.zeros(0, np.int64)
    nid, nlon, nlat = second_pass(second or src, ids) if len(ids) else (ids, np.zeros(0), np.zeros(0))
    stations += way_records(ways, nid, nlon, nlat)
    write(out, stations, points, {"stations": len(stations), "points": len(points),
                                  "with_capacity": sum(1 for r in stations if r[6] is not None)})
    print(f"done in {time.time() - t0:.0f} s", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("planet", help="the planet (or an extract): a .osm.pbf path, an http(s) URL (streamed), '-'")
    ap.add_argument("out")
    ap.add_argument("--second-pass", help="the planet again for the second pass (a path, or '-' stdin: streaming)")
    a = ap.parse_args()
    build(a.planet, a.out, a.second_pass)


if __name__ == "__main__":
    sys.exit(main())
