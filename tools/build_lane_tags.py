"""build_lane_tags.py PLANET OUT_DIR: OpenStreetMap's lane-level road tags (turn:lanes, change:lanes, placement,
lanes:forward / backward, overtaking, ...) as small gzip JSON shards the server reads per area (server/lanetags.py),
for the road markings (the user, 2026-10-28: "look at how some maps get detailed lane by lane data"; research:
docs/research/2026-10-28-road-markings-standards.md §5). OSM US's Layercake highways file, which osmtags.py reads, has
`lanes` but none of these.

    python3 tools/build_lane_tags.py planet-latest.osm.pbf out/
    python3 tools/build_lane_tags.py https://.../planet-YYMMDD.osm.pbf out/     (streamed twice by curl: no disk)

Shards: <OUT_DIR>/<x>/<y>.json.gz per z7 tile (build_rail_tags.SHARD_ZOOM), and <OUT_DIR>/manifest.json. A shard:
- "ways": {way id: tags}: the highway ways with any of LANE_KEYS, and the roundabout rings; their tags in TAGS.
- "nodes": [[id, lon, lat, {"traffic_sign", "name", "direction", "traffic_sign:direction"}]]: Germany's mapped town
  signs (Ortstafeln: TOWN_CODES, sign_records), only inside GERMANY_BOX, for the town signs (server/townentry.py);
  the name and the direction keys only where mapped.
A way is placed by its first, middle and last nodes (enough for a way: they are short), so the second pass looks up
three nodes a way instead of all of them. Data: (c) OpenStreetMap contributors, ODbL 1.0.
"""
import argparse
import os
import re
import sys
import time

import numpy as np
import osmium

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_rail_tags as rail  # noqa: E402

LANE_KEYS = ("turn:lanes", "turn:lanes:forward", "turn:lanes:backward", "turn:lanes:both_ways",
             "change:lanes", "change:lanes:forward", "change:lanes:backward",
             "placement", "placement:forward", "placement:backward",
             "lanes:forward", "lanes:backward", "lanes:both_ways",
             "overtaking", "overtaking:forward", "overtaking:backward", "divider", "lane_markings",
             "width:lanes", "width:lanes:forward", "width:lanes:backward",
             "bus:lanes", "bus:lanes:forward", "bus:lanes:backward", "psv:lanes", "busway", "busway:left",
             "busway:right", "busway:both", "hov:lanes", "cycleway:lanes", "access:lanes")
TAGS = LANE_KEYS + ("highway", "lanes", "oneway", "junction")
# roundabout rings too, lane tags or not (roads._rings: their circulatory width; Layercake's highways file has no
# junction column)
RINGS = ("roundabout", "circular")
# Germany's box (lon_min, lat_min, lon_max, lat_max): every point the server's border data (countrybounds) calls
# Germany lies inside it (5.870 .. 15.035 E, 47.275 .. 55.095 N, territorial waters included). The town sign nodes
# are written only inside it: the shards stay as they were elsewhere
GERMANY_BOX = (5.81, 47.22, 15.10, 55.11)
# the town signs (Ortstafel, Zeichen 310 / 311): OpenStreetMap's city_limit, and the VzKat's numbers as some map them
TOWN_CODES = ("city_limit", "DE:310", "DE:311")
# a node's sign keys: traffic_sign, and the two that also say which way the sign faces
SIGN_KEYS = ("traffic_sign", "traffic_sign:forward", "traffic_sign:backward")
_TOWN = re.compile(r"^(?:city_limit|(?:DE:)?31[01])(?=$|[\[\-:])")
# what the first pass asks pyosmium for, a way and a node each only their own keys (an object with none never reaches
# Python)
WAY_KEYS = LANE_KEYS + ("junction",)
NODE_KEYS = SIGN_KEYS


def _codes(value):
    return [c.strip() for c in re.split(r"[;,]", value) if c.strip()]


def lane_way(tags):
    """A highway way's entry in the shards' "ways" (its TAGS), None if it has none: it has a lane key or is a
    roundabout ring (a junction=yes / spui way without lane tags is not)."""
    if "highway" not in tags:
        return None
    if tags.get("junction") not in RINGS and not any(k in tags for k in LANE_KEYS):
        return None
    return {t: tags[t] for t in TAGS if t in tags}


def sign_code(code):
    """A traffic_sign code as the shards keep it, None if it is not a town sign (TOWN_CODES): city_limit as it is, a
    number with "DE:" where the mapper left it out (310 -> DE:310)."""
    if not _TOWN.match(code):
        return None
    return code if code.startswith(("DE:", "city_limit")) else "DE:" + code


def sign_node(tags, key="traffic_sign"):
    """{"traffic_sign": the town signs of tags[key] joined by ",", the node's name, which way they face} of a node,
    None when key holds none. Which way: for traffic_sign, its direction / traffic_sign:direction as mapped; for
    traffic_sign:forward / :backward, the mapped direction, else the key's own (forward / backward)."""
    kept = [c for c in (sign_code(v) for v in _codes(tags.get(key, ""))) if c is not None]
    if not kept:
        return None
    out = {"traffic_sign": ",".join(kept)}
    if tags.get("name"):
        out["name"] = tags["name"]
    if key == "traffic_sign":
        for k in ("direction", "traffic_sign:direction"):
            if k in tags:
                out[k] = tags[k]
    else:
        out["direction"] = tags.get("direction", key.rsplit(":", 1)[1])
    return out


def sign_records(tags):
    """[sign_node] of a node's SIGN_KEYS that hold a town sign: a node with signs on two of them (both ways) writes
    one record for each."""
    return [s for s in (sign_node(tags, k) for k in SIGN_KEYS) if s is not None]


def in_box(lon, lat):
    """True where (lon, lat) (floats or arrays) lies inside GERMANY_BOX."""
    return (GERMANY_BOX[0] <= lon) & (lon <= GERMANY_BOX[2]) & (GERMANY_BOX[1] <= lat) & (lat <= GERMANY_BOX[3])


def first_pass(src):
    """(ways, nodes): ways [(id, lane_way tags, (first, middle, last) node refs)]; nodes [(id, lon, lat, a
    sign_records record)] inside GERMANY_BOX."""
    with rail._Source(src) as f:
        fp = osmium.FileProcessor(f, osmium.osm.NODE | osmium.osm.WAY)
        fp = fp.with_filter(osmium.filter.KeyFilter(*WAY_KEYS).enable_for(osmium.osm.WAY))
        return _first_pass(fp.with_filter(osmium.filter.KeyFilter(*NODE_KEYS).enable_for(osmium.osm.NODE)))


def _first_pass(objects):
    ways, nodes, t0, k = [], [], time.time(), 0
    for o in objects:
        k += 1
        if k % 500_000 == 0:
            print(f"  pass 1: {k:,} objects, {len(ways):,} ways, {len(nodes):,} nodes, {time.time() - t0:.0f} s",
                  flush=True)
        if o.is_node():
            if o.location.valid() and in_box(o.location.lon, o.location.lat):
                nodes.extend((o.id, o.location.lon, o.location.lat, s) for s in sign_records(o.tags))
            continue
        tags = o.tags
        if len(o.nodes) < 2:
            continue
        lane = lane_way(tags)
        if lane is None:
            continue
        refs = [n.ref for n in o.nodes]
        ways.append((o.id, lane, np.array([refs[0], refs[len(refs) // 2], refs[-1]], np.int64)))
    return ways, nodes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("planet", help="the planet (or an extract): a .osm.pbf path, an http(s) URL (streamed), '-'")
    ap.add_argument("out")
    ap.add_argument("--second-pass", help="the planet again for the second pass (a path, or '-' stdin: streaming)")
    a = ap.parse_args()
    t0 = time.time()
    ways, nodes = first_pass(a.planet)
    print(f"pass 1: {len(ways):,} ways, {len(nodes):,} town sign nodes, {time.time() - t0:.0f} s", flush=True)
    ids = np.unique(np.concatenate([r for _i, _t, r in ways])) if ways else np.zeros(0, np.int64)
    nid, nlon, nlat = rail.second_pass(a.second_pass or a.planet, ids)
    print(f"pass 2: {len(nid):,} node places, {time.time() - t0:.0f} s", flush=True)
    rail.write(a.out, ways, nodes, nid, nlon, nlat)
    print(f"done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    sys.exit(main())
