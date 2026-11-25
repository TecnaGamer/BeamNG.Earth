"""build_drive_tags.py PLANET OUT_DIR: OpenStreetMap's mapped driving features that neither Overture nor OSM US's
Layercake highways file carries, as small gzip JSON shards the server reads per area (server/drivetags.py, dataset
"drive"), rebuilt weekly from the planet by the GitHub Action of TecnaGamer/BeamNG.Earth-data like the rail and lane
shards (the user, 2026-11-22: "feel free to use as many [OSM/Overture data points] as possible if they would improve
the generator").

    python3 tools/build_drive_tags.py planet-latest.osm.pbf out/drive
    python3 tools/build_drive_tags.py https://.../planet-YYMMDD.osm.pbf out/drive     (streamed twice by curl: no disk)
    python3 tools/build_drive_tags.py extract.osm out/drive                           (an OSM XML extract: tests)

Overture's infrastructure layer already has the barriers (gate, lift_gate, swing_gate, bollard, cattle_grid,
toll_booth, height_restrictor, jersey_barrier, kerb), highway=stop / give_way / crossing (with crossing:markings and a
crossing's traffic_calming) and the street lamps (light:colour, light:count, lamp_mount) with their OSM tags: those are
read from it (server/drivefeat.py), not written here. What it drops, and what this writes:

Shards: <OUT_DIR>/<x>/<y>.json.gz per z7 tile (build_rail_tags.SHARD_ZOOM), and <OUT_DIR>/manifest.json. A shard:
- "nodes": [[id, lon, lat, {tags of NODE_TAGS}]] of the nodes with
    traffic_calming=* (bump, hump, table, cushion, chicane, choker, island, rumble_strip, ...),
    highway=turning_circle / turning_loop / mini_roundabout / passing_place / speed_camera,
    ford=* (yes, stepping_stones, ...: not no),
    railway=signal / switch / buffer_stop / derail / railway_crossing;
  their tags cut to NODE_TAGS (a key that is not mapped is left out).
- "rules": {key: {value: [way ids, sorted, delta-coded: the first, then each minus the one before]}} of the highway
  ways with tracktype (grade1-grade5), ford (a way that is the ford itself) or traffic_calming (a short way mapped as
  the table / chicane), the values normalised (norm_way: lower case, a value outside the known set dropped).
  railtags.Rules reads it; the server joins it to Overture's road segments by way id (sources' record_id).
  A way is placed by its first, middle and last nodes (build_lane_tags' rule: they are short), so the second pass
  looks up three nodes a way.
- "ways": {} (railtags.Shards' readers expect the key).
Data: (c) OpenStreetMap contributors, ODbL 1.0.
"""
import argparse
import os
import sys
import time

try:        # (first: numpy's AVX-512 kernels held back before anything imports numpy, tools/npyfirst.py)
    import npyfirst  # noqa: E402,F401
except ImportError:     # (loaded by its path into another process, a test: that process's entry point guards)
    pass
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FORMAT = 1
CALMING = ("bump", "hump", "table", "cushion", "chicane", "choker", "island", "rumble_strip", "dip", "mini_bumps",
           "painted_table", "yes", "bump;chicane", "double_dip")
NODE_HIGHWAY = ("turning_circle", "turning_loop", "mini_roundabout", "passing_place", "speed_camera")
NODE_RAILWAY = ("signal", "switch", "buffer_stop", "derail", "railway_crossing")
# a node's tags kept (the rest dropped: the shards' size matters on a Steam Deck)
NODE_TAGS = ("traffic_calming", "highway", "ford", "railway", "direction", "surface", "maxspeed", "height", "length",
             "width", "diameter", "crossing", "crossing:markings",
             # railway signals and points (OpenStreetMap's German tagging scheme, used worldwide)
             "railway:signal:direction", "railway:signal:position", "railway:signal:main",
             "railway:signal:distant", "railway:signal:combined", "railway:signal:minor",
             "railway:signal:main:form", "railway:signal:combined:form", "railway:signal:distant:form",
             "railway:signal:minor:form", "railway:switch", "railway:turnout_side", "railway:switch:electric")
TRACKTYPES = ("grade1", "grade2", "grade3", "grade4", "grade5")
WAY_CALMING = ("table", "hump", "bump", "cushion", "chicane", "choker", "rumble_strip", "island")
FORDS = ("yes", "stepping_stones", "boat")
# what the first pass asks pyosmium for (an object with none of these never reaches Python)
NODE_KEYS = ("traffic_calming", "highway", "ford", "railway")
WAY_KEYS = ("tracktype", "ford", "traffic_calming")


def node_record(tags):
    """The node's entry tags (NODE_TAGS of it), None when it is not one of the shards' features."""
    calm = tags.get("traffic_calming")
    hw = tags.get("highway")
    ford = tags.get("ford")
    rw = tags.get("railway")
    if not ((calm and calm != "no") or hw in NODE_HIGHWAY or (ford and ford != "no") or rw in NODE_RAILWAY):
        return None
    return {t: tags[t] for t in NODE_TAGS if t in tags}


def norm_way(tags):
    """{key: normalised value} of a highway way's tracktype / ford / traffic_calming ({} none): lower case, the first
    of a ;-list, a value outside TRACKTYPES / FORDS / WAY_CALMING dropped."""
    if "highway" not in tags:
        return {}
    out = {}
    for k, ok in (("tracktype", TRACKTYPES), ("ford", FORDS), ("traffic_calming", WAY_CALMING)):
        v = tags.get(k)
        if v:
            v = v.split(";")[0].strip().lower()
            if v in ok:
                out[k] = v
    return out


def first_pass(src):
    """(rules [(way id, {key: value}, node refs: first, middle, last)], nodes [(id, lon, lat, tags)])."""
    import osmium
    import build_rail_tags as rail
    rules, nodes, t0, k = [], [], time.time(), 0
    with rail._Source(src) as f:
        fp = osmium.FileProcessor(f, osmium.osm.NODE | osmium.osm.WAY)
        fp = fp.with_filter(osmium.filter.KeyFilter(*dict.fromkeys(NODE_KEYS + WAY_KEYS)))
        for o in fp:
            k += 1
            if k % 1_000_000 == 0:
                print(f"  pass 1: {k:,} objects, {len(rules):,} ways, {len(nodes):,} nodes, {time.time() - t0:.0f} s",
                      flush=True)
            tags = {t.k: t.v for t in o.tags}
            if o.is_way():
                r = norm_way(tags)
                if r:
                    refs = [n.ref for n in o.nodes]
                    if refs:
                        three = sorted({refs[0], refs[len(refs) // 2], refs[-1]})
                        rules.append((o.id, r, np.array(three, np.int64)))
            elif o.is_node():
                t = node_record(tags)
                if t is not None and o.location.valid():
                    nodes.append((o.id, o.location.lon, o.location.lat, t))
    nodes.sort(key=lambda n: n[0])
    rules.sort(key=lambda r: r[0])
    return rules, nodes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("planet", help="the planet (or an extract): a .osm.pbf / .osm path, an http(s) URL (streamed), '-'")
    ap.add_argument("out")
    ap.add_argument("--second-pass", help="the planet again for the second pass (a path, or '-' stdin: streaming)")
    a = ap.parse_args()
    import build_rail_tags as rail      # (pyosmium: the build's, not the rules' tests)
    t0 = time.time()
    rules, nodes = first_pass(a.planet)
    print(f"pass 1: {len(rules):,} ways, {len(nodes):,} nodes, {time.time() - t0:.0f} s", flush=True)
    ids = np.unique(np.concatenate([r for _i, _t, r in rules])) if rules else np.zeros(0, np.int64)
    nid, nlon, nlat = rail.second_pass(a.second_pass or a.planet, ids)
    print(f"pass 2: {len(nid):,} node places, {time.time() - t0:.0f} s", flush=True)
    rail.write(a.out, [], nodes, nid, nlon, nlat, rules=rules)
    print(f"done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    sys.exit(main())
