"""build_lane_tags.py PLANET OUT_DIR: OpenStreetMap's lane-level road tags (turn:lanes, change:lanes, placement,
lanes:forward / backward, overtaking, ...) as small gzip JSON shards the server reads per area (server/railtags.py,
LaneTags), for the road markings (the user, 2026-10-28: "look at how some maps get detailed lane by lane data";
research: docs/research/2026-10-28-road-markings-standards.md §5), and Germany's road rules and junction signs. OSM
US's Layercake highways file, which osmtags.py reads, has `lanes` but none of these.

    python3 tools/build_lane_tags.py planet-latest.osm.pbf out/
    python3 tools/build_lane_tags.py https://.../planet-YYMMDD.osm.pbf out/     (streamed twice by curl: no disk)

Shards: <OUT_DIR>/<x>/<y>.json.gz per z7 tile (build_rail_tags.SHARD_ZOOM), and <OUT_DIR>/manifest.json. A shard:
- "ways": {way id: tags}: the highway ways with any of LANE_KEYS, and the roundabout rings; their tags in TAGS. Only
  these: a reader of "ways" gets the same data as before the road rules came.
- "rules": {"priority_road": {"designated" / "end" / "yes_unposted": [way ids]}, "zone:maxspeed": {"DE:30" / "DE:20"
  / "DE:10": [way ids]}}: every highway way with a road rule, lane way or not (rule_tags, normalised at build time:
  the raw values are never written, the shards' size matters on a Steam Deck), only around Germany (GERMANY_BOX: a
  way with a node in it). Each id list sorted and delta-coded (the first id, then each minus the one before: 100,
  105, 230 is [100, 5, 125]). Left out where a shard has none.
- "nodes": [[id, lon, lat, {"traffic_sign", "name", "direction", "traffic_sign:direction"}]]: the nodes with a German
  junction sign (SIGN_BASES) or a mapped town sign (Ortstafeln: TOWN_CODES, for the town signs, server/townentry.py;
  sign_records), only inside GERMANY_BOX; the name (town signs only) and the direction keys only where mapped. Each
  reader keeps the codes it knows (designs.mapped_rows, townentry.Towns.mapped_nodes).
Outside the box a shard holds what the tool wrote before the road rules; only a shard a rule way touches that also
reaches inside the box differs (a way across the box's edge, with a node inside, can add a rules-only shard in the
tile next door).
Design of the rules and the nodes (A2): docs/research/2026-11-06-german-junctions-design.md
A way is placed, in "ways" and "rules" alike, by its first, middle and last nodes (enough for a way: they are short),
so the second pass looks up three nodes a way instead of all of them. Data: (c) OpenStreetMap contributors, ODbL 1.0.
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
# Germany's road rules (docs/research/2026-11-06-german-junctions-design.md): read from the way, written normalised
# (rule_tags) to the shards' "rules", never raw: the shards' size matters on a Steam Deck (railtags.Ways)
RULE_KEYS = ("priority_road", "zone:maxspeed", "maxspeed:type", "source:maxspeed", "traffic_sign")
PRIORITY = ("designated", "end", "yes_unposted")
ZONE_SPEEDS = ("30", "20", "10")
# Germany's box (lon_min, lat_min, lon_max, lat_max): server/germany.BOX, the same (this tool is curl'd alone by the
# data repo's Action, so it cannot import it; test_lane_rules keeps the two equal). Every point the server's border
# data (countrybounds) calls Germany lies inside it: 5.870 .. 15.035 E, 47.275 .. 55.095 N in the regional build
# c02f3d4300959998 (territorial waters included). The rules and the sign nodes are written only inside it: the shards
# stay small elsewhere (Sweden's ~41k unused huvudled ids grew its shards 30 %)
GERMANY_BOX = (5.81, 47.22, 15.10, 55.11)
# the traffic signs of the German junctions, by their number in the VzKat (205 Vorfahrt gewähren, 206 Halt, 301
# Vorfahrt, 306 / 307 Vorfahrtstraße begin / end, 274.1 / 274.2 Tempo-30-Zone begin / end)
SIGN_BASES = ("205", "206", "301", "306", "307", "274.1", "274.2")
SIGN_CODES = tuple("DE:" + b for b in SIGN_BASES)
# the town signs (Ortstafel, Zeichen 310 / 311): OpenStreetMap's city_limit, and the VzKat's numbers as some map them
TOWN_CODES = ("city_limit", "DE:310", "DE:311")
# a node's sign keys: traffic_sign, and the two that also say which way the sign faces
SIGN_KEYS = ("traffic_sign", "traffic_sign:forward", "traffic_sign:backward")
# a junction sign's code: "DE:" or not (mappers leave it out: 205), and any variant after its number (DE:274.1:30,
# DE:274.1[30], DE:274.1-20: a zone's limit)
_SIGN = re.compile(r"^(?:DE:)?(?:" + "|".join(re.escape(b) for b in SIGN_BASES) + r")(?=$|[\[\-:])")
_TOWN = re.compile(r"^(?:city_limit|(?:DE:)?31[01])(?=$|[\[\-:])")
_PLATE = re.compile(r"^(?:DE:)?1002-[\w.]+$")           # a Zusatzzeichen 1002-xx plate (1002-12: "right turn")
_ZONE_TYPE = re.compile(r"^DE:zone:?(30|20|10)$")
# what the first pass asks pyosmium for, a way and a node each only their own keys (an object with none never reaches
# Python; every tagged node is checked: three keys, not all of these, keep that cheap)
WAY_KEYS = tuple(dict.fromkeys(LANE_KEYS + ("junction",) + RULE_KEYS))
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


def rule_tags(tags):
    """The normalised German road-rule tags of a way, {} when none apply: priority_road (designated, end,
    yes_unposted; or designated from a way traffic_sign DE:306) and zone:maxspeed (DE:30 / DE:20 / DE:10, from
    zone:maxspeed itself or from maxspeed:type / source:maxspeed = DE:zone30 / DE:zone:30)."""
    out = {}
    pr = tags.get("priority_road")
    if pr in PRIORITY:
        out["priority_road"] = pr
    elif pr is None and "DE:306" in _codes(tags.get("traffic_sign", "")):
        out["priority_road"] = "designated"
    z = tags.get("zone:maxspeed")
    if z in ["DE:" + v for v in ZONE_SPEEDS]:
        out["zone:maxspeed"] = z
    else:
        for k in ("maxspeed:type", "source:maxspeed"):
            m = _ZONE_TYPE.match(tags.get(k, ""))
            if m:
                out["zone:maxspeed"] = "DE:" + m.group(1)
                break
    return out


def sign_code(code):
    """A traffic_sign code as the shards keep it, None if it is neither a junction sign (SIGN_BASES) nor a town sign
    (TOWN_CODES): a junction sign with "DE:" where the mapper left it out (205 -> DE:205), and with its variant
    (DE:274.1:30, DE:274.1[30], DE:274.1-20: the zone's limit, for the reader to parse); a town sign's city_limit as it
    is, its number with "DE:" where the mapper left it out (310 -> DE:310)."""
    if _SIGN.match(code):
        return code if code.startswith("DE:") else "DE:" + code
    if _TOWN.match(code):
        return code if code.startswith(("DE:", "city_limit")) else "DE:" + code
    return None


def sign_node(tags, key="traffic_sign"):
    """{"traffic_sign": the junction signs of tags[key] (sign_code, and the 1002-* plates after one) and its town
    signs joined by ",", the node's name where it holds a town sign, + which way they face} of a node, None when key
    holds none. Which way: for traffic_sign, its direction / traffic_sign:direction as mapped; for
    traffic_sign:forward / :backward, the mapped direction, else the key's own (forward / backward)."""
    kept, on, town = [], False, False
    for c in _codes(tags.get(key, "")):
        s = sign_code(c)
        if s is not None:
            kept.append(s)
            on = _SIGN.match(c) is not None         # (a plate follows a junction sign only)
            town = town or not on
        elif on and _PLATE.match(c):
            kept.append(c)
        else:
            on = False
    if not kept:
        return None
    out = {"traffic_sign": ",".join(kept)}
    if town and tags.get("name"):
        out["name"] = tags["name"]
    if key == "traffic_sign":
        for k in ("direction", "traffic_sign:direction"):
            if k in tags:
                out[k] = tags[k]
    else:
        out["direction"] = tags.get("direction", key.rsplit(":", 1)[1])
    return out


def sign_records(tags):
    """[sign_node] of a node's SIGN_KEYS that hold a junction or a town sign: a node with signs on two of them (both
    ways) writes one record for each."""
    return [s for s in (sign_node(tags, k) for k in SIGN_KEYS) if s is not None]


def in_box(lon, lat):
    """True where (lon, lat) (floats or arrays) lies inside GERMANY_BOX."""
    return (GERMANY_BOX[0] <= lon) & (lon <= GERMANY_BOX[2]) & (GERMANY_BOX[1] <= lat) & (lat <= GERMANY_BOX[3])


def rules_in_box(rules, nid, nlon, nlat):
    """The rules whose first / middle / last node has a place (second_pass's nid, nlon, nlat) inside GERMANY_BOX."""
    keep = []
    if not len(nid):
        return keep
    for rule in rules:
        j = np.searchsorted(nid, rule[2])
        ok = (j < len(nid)) & (nid[np.minimum(j, len(nid) - 1)] == rule[2])
        j = j[ok]
        if len(j) and in_box(nlon[j], nlat[j]).any():
            keep.append(rule)
    return keep


def first_pass(src):
    """(ways, rules, nodes): ways [(id, lane_way tags, (first, middle, last) node refs)]; rules [(id, rule_tags, the
    same refs)] of every highway way with a road rule, lane way or not (rules_in_box keeps the German ones after the
    second pass); nodes [(id, lon, lat, a sign_records record)] inside GERMANY_BOX."""
    with rail._Source(src) as f:
        fp = osmium.FileProcessor(f, osmium.osm.NODE | osmium.osm.WAY)
        fp = fp.with_filter(osmium.filter.KeyFilter(*WAY_KEYS).enable_for(osmium.osm.WAY))
        return _first_pass(fp.with_filter(osmium.filter.KeyFilter(*NODE_KEYS).enable_for(osmium.osm.NODE)))


def _first_pass(objects):
    ways, rules, nodes, t0, k = [], [], [], time.time(), 0
    for o in objects:
        k += 1
        if k % 500_000 == 0:
            print(f"  pass 1: {k:,} objects, {len(ways):,} ways, {len(rules):,} with rules, {len(nodes):,} nodes, "
                  f"{time.time() - t0:.0f} s", flush=True)
        if o.is_node():
            if o.location.valid() and in_box(o.location.lon, o.location.lat):
                nodes.extend((o.id, o.location.lon, o.location.lat, s) for s in sign_records(o.tags))
            continue
        tags = o.tags
        if "highway" not in tags or len(o.nodes) < 2:
            continue
        lane, rule = lane_way(tags), rule_tags(tags)
        if lane is None and not rule:
            continue
        refs = [n.ref for n in o.nodes]
        refs = np.array([refs[0], refs[len(refs) // 2], refs[-1]], np.int64)
        if lane is not None:
            ways.append((o.id, lane, refs))
        if rule:
            rules.append((o.id, rule, refs))
    return ways, rules, nodes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("planet", help="the planet (or an extract): a .osm.pbf path, an http(s) URL (streamed), '-'")
    ap.add_argument("out")
    ap.add_argument("--second-pass", help="the planet again for the second pass (a path, or '-' stdin: streaming)")
    a = ap.parse_args()
    t0 = time.time()
    ways, rules, nodes = first_pass(a.planet)
    print(f"pass 1: {len(ways):,} ways, {len(rules):,} with road rules, {len(nodes):,} sign nodes (junction and town signs), "
          f"{time.time() - t0:.0f} s", flush=True)
    refs = [r for _i, _t, r in ways] + [r for _i, _t, r in rules]
    ids = np.unique(np.concatenate(refs)) if refs else np.zeros(0, np.int64)
    nid, nlon, nlat = rail.second_pass(a.second_pass or a.planet, ids)
    print(f"pass 2: {len(nid):,} node places, {time.time() - t0:.0f} s", flush=True)
    rules = rules_in_box(rules, nid, nlon, nlat)
    print(f"{len(rules):,} road rules in Germany's box", flush=True)
    rail.write(a.out, ways, nodes, nid, nlon, nlat, rules=rules)
    print(f"done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    sys.exit(main())
