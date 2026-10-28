"""build_rail_tags.py PLANET OUT_DIR: OpenStreetMap's railways as small gzip JSON shards the server reads per area
(server/railtags.py), rebuilt weekly from the planet (the user, 2026-10-27: "Build our own weekly rail file";
Overture keeps rail geometry but no gauge, electrification or service, and calls proposed lines "unknown").

    python3 tools/build_rail_tags.py planet-latest.osm.pbf out/
    python3 tools/build_rail_tags.py https://.../planet-YYMMDD.osm.pbf out/     (streamed twice by curl: no disk)

Shards: <OUT_DIR>/<x>/<y>.json.gz per z7 tile (SHARD_ZOOM) with rails, {"ways": {way id: tags}, "nodes": [[id,
lon, lat, tags]]} (ways with railway=*, nodes railway=level_crossing / crossing and power=catenary_mast / _portal; the
tags in TAGS), and <OUT_DIR>/manifest.json. Served by GitHub Pages from TecnaGamer/BeamNG.Earth-data, rebuilt weekly
by its GitHub Action (the user, 2026-10-27: "could we automate some of the data with github actions?").
A way is in every shard its points fall in. Two passes over the planet: the
ways and the nodes of interest first (their node ids kept), then those nodes' places (the planet's ways come after
its nodes: one streaming pass cannot place them). Data: (c) OpenStreetMap contributors, ODbL 1.0.
"""
import argparse
import math
import os
import sys
import time

import numpy as np
import osmium

ZOOM = 14
SHARD_ZOOM = 7             # one file per z7 tile (~300 km): a place needs one to four
TAGS = ("railway", "usage", "service", "gauge", "tracks", "electrified", "voltage", "frequency", "maxspeed",
        "bridge", "tunnel", "embankment", "cutting", "layer", "name", "operator", "ref", "highspeed",
        "crossing", "crossing:barrier", "crossing:light", "crossing:bell", "crossing:saltire", "crossing:supervision",
        "power", "structure", "disused:railway", "abandoned:railway", "razed:railway", "construction:railway",
        "proposed:railway")
NODE_RAILWAY = {"level_crossing", "crossing"}
NODE_POWER = {"catenary_mast", "catenary_portal"}


def _open(src):
    """An osmium file of a path, '-' (stdin) or a pipe (/dev/fd/..: a download streamed)."""
    if src == "-":
        return osmium.io.File("", "pbf")        # (libosmium: an empty name is stdin)
    if not src.endswith(".pbf"):
        return osmium.io.File(src, "pbf")       # (a pipe: <(curl ...))
    return src


def cell_xy(lat, lon):
    n = 1 << ZOOM
    lat = np.clip(lat, -85.05112878, 85.05112878)
    x = np.floor((lon + 180.0) / 360.0 * n).astype(np.int64)
    r = np.radians(lat)
    y = np.floor((1.0 - np.log(np.tan(r) + 1.0 / np.cos(r)) / math.pi) / 2.0 * n).astype(np.int64)
    return np.clip(x, 0, n - 1), np.clip(y, 0, n - 1)


class _Source:
    """A pass's input: a path, '-', a pipe, or an http(s) URL streamed by curl (started when the pass starts, its exit
    checked when it ends: a cut-off download fails the build instead of making a partial file)."""

    def __init__(self, src):
        self.src, self.proc = src, None

    def __enter__(self):
        if self.src.startswith(("http://", "https://")):
            import subprocess
            self.proc = subprocess.Popen(["curl", "-sfL", "--retry", "5", "-A", "BeamNG.Earth rail build",
                                          self.src], stdout=subprocess.PIPE)
            return osmium.io.File(f"/proc/self/fd/{self.proc.stdout.fileno()}", "pbf")
        return _open(self.src)

    def __exit__(self, *exc):
        if self.proc is not None:
            self.proc.stdout.close()
            rc = self.proc.wait()
            if rc not in (0, None) and exc[0] is None:
                raise SystemExit(f"curl {self.src}: exit {rc}")


def first_pass(src):
    """(ways [(id, tags dict, node refs)], nodes [(id, lon, lat, tags)]) of interest."""
    with _Source(src) as f:
        return _first_pass(f)


def _first_pass(src):
    ways, nodes, t0, k = [], [], time.time(), 0
    fp = osmium.FileProcessor(src, osmium.osm.NODE | osmium.osm.WAY)
    fp = fp.with_filter(osmium.filter.KeyFilter("railway", "power"))
    for o in fp:
        k += 1
        if k % 1_000_000 == 0:
            print(f"  pass 1: {k:,} objects, {len(ways):,} ways, {len(nodes):,} nodes, {time.time() - t0:.0f} s",
                  flush=True)
        tags = o.tags
        if o.is_way():
            if "railway" not in tags:
                continue
            ways.append((o.id, {t: tags[t] for t in TAGS if t in tags}, np.array([n.ref for n in o.nodes], np.int64)))
        elif o.is_node():
            if tags.get("railway") in NODE_RAILWAY or tags.get("power") in NODE_POWER:
                nodes.append((o.id, o.location.lon, o.location.lat, {t: tags[t] for t in TAGS if t in tags}))
    return ways, nodes


def second_pass(src, ids):
    """(sorted node ids, lon, lat) of the ids (np.int64, unique, sorted)."""
    with _Source(src) as f:
        return _second_pass(f, ids)


def _second_pass(src, ids):
    got_id, got_lon, got_lat, t0 = [], [], [], time.time()
    fp = osmium.FileProcessor(src, osmium.osm.NODE).with_filter(osmium.filter.IdFilter(ids.tolist()))
    for o in fp:
        if o.location.valid():
            got_id.append(o.id)
            got_lon.append(o.location.lon)
            got_lat.append(o.location.lat)
        if len(got_id) % 5_000_000 == 0 and got_id:
            print(f"  pass 2: {len(got_id):,} of {len(ids):,} nodes, {time.time() - t0:.0f} s", flush=True)
    i = np.asarray(got_id, np.int64)
    o = np.argsort(i)
    return i[o], np.asarray(got_lon, np.float64)[o], np.asarray(got_lat, np.float64)[o]


def write(out, ways, nodes, nid, nlon, nlat):
    """The shards: <out>/<x>/<y>.json.gz per SHARD_ZOOM tile with rails, {"ways": {id: tags}, "nodes": [[id, lon,
    lat, tags]]} (a way in every shard its cells are in), and <out>/manifest.json."""
    import gzip
    import json
    shards = {}
    shift = ZOOM - SHARD_ZOOM
    missing = 0
    for oid, tags, refs in ways:
        j = np.searchsorted(nid, refs)
        ok = (j < len(nid)) & (nid[np.minimum(j, len(nid) - 1)] == refs)
        if not ok.any():
            missing += 1
            continue
        j = j[ok]
        x, y = cell_xy(nlat[j], nlon[j])
        for sx, sy in set(zip((x >> shift).tolist(), (y >> shift).tolist())):
            shards.setdefault((sx, sy), ({}, []))[0][str(oid)] = tags
    for oid, lon, lat, tags in nodes:
        x, y = cell_xy(np.array([lat]), np.array([lon]))
        shards.setdefault((int(x[0]) >> shift, int(y[0]) >> shift), ({}, []))[1].append(
            [oid, round(lon, 7), round(lat, 7), tags])
    total = 0
    for (sx, sy), (w, n) in shards.items():
        d = os.path.join(out, str(sx))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{sy}.json.gz")
        with gzip.open(path + ".part", "wt", encoding="utf-8", compresslevel=9) as fh:
            json.dump({"ways": w, "nodes": n}, fh, separators=(",", ":"), ensure_ascii=False)
        os.replace(path + ".part", path)
        total += os.path.getsize(path)
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"format": 1, "zoom": SHARD_ZOOM, "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "ways": len(ways), "nodes": len(nodes), "shards": len(shards),
                   "license": "ODbL 1.0, (c) OpenStreetMap contributors"}, fh)
    print(f"{len(ways):,} ways ({missing} without nodes), {len(nodes):,} nodes, {len(shards):,} shards, "
          f"{total / 1e6:.1f} MB", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("planet", help="the planet (or an extract): a .osm.pbf path, an http(s) URL (streamed), '-'")
    ap.add_argument("out")
    ap.add_argument("--second-pass", help="the planet again for the second pass (a path, or '-' stdin: streaming)")
    a = ap.parse_args()
    t0 = time.time()
    ways, nodes = first_pass(a.planet)
    print(f"pass 1: {len(ways):,} ways, {len(nodes):,} nodes, {time.time() - t0:.0f} s", flush=True)
    ids = np.unique(np.concatenate([r for _i, _t, r in ways])) if ways else np.zeros(0, np.int64)
    nid, nlon, nlat = second_pass(a.second_pass or a.planet, ids)
    print(f"pass 2: {len(nid):,} node places, {time.time() - t0:.0f} s", flush=True)
    write(a.out, ways, nodes, nid, nlon, nlat)
    print(f"done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    sys.exit(main())
