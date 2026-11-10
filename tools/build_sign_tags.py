"""build_sign_tags.py PLANET OUT_DIR: the highway signs' sources from OpenStreetMap as small gzip JSON shards the
server reads per area (server/railtags.py SignTags, dataset "signs"), rebuilt weekly from the planet by the GitHub
Action of TecnaGamer/BeamNG.Earth-data like the rail and lane shards. From cody196's PR #363, whose server
downloaded regional extracts and indexed them on each player's machine (osmregions.py / osmsigns.py): built here
once a week instead, with the PR's own indexing functions (server/exitsigns.py, throughsigns.py, milemarkers.py).

    python3 tools/build_sign_tags.py planet-latest.osm.pbf out/signs
    python3 tools/build_sign_tags.py https://.../planet-YYMMDD.osm.pbf out/signs     (streamed twice by curl: no disk)

Records (server/signshards.py: the z7 shard of each record's point, paths delta-coded):
  - "exit": a motorway / trunk link leaving a mainline (exitsigns.index_records): its posted destinations (label),
    the ~1.8 km approach before the junction, the ramp's first RAMP_KEEP_M, its side, the mainline way ids, the
    destination block of the mainline before it (through_label: throughsigns.annotate_exits, route relations' roles
    and directions);
  - "through": overhead destination stations along directed mainlines with destination tags
    (throughsigns.index_records, every SPACING_M of an equally labelled chain);
  - "mile": Interstate mile references (milemarkers.osm_records): mapped highway=milestone nodes on an Interstate
    carriageway (US:I route relations or an "I nn" ref) and the whole miles between two compatible ones.
Two passes over the planet (as build_rail_tags.py): the junction / milestone nodes, the motorway and trunk ways and
links (their node ids kept) and the route=road relations first, then those nodes' places. Exits are indexed per z7
tile of their junction with the mainline ways within MARGIN_DEG (an approach is 1.8 km): a planet's whole motorway
network never sits in one dict. Data: (c) OpenStreetMap contributors, ODbL 1.0.
"""
import argparse
import gzip
import json
import math
import os
import re
import sys
import time
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.join(os.path.dirname(HERE), "server"))   # (the repo; the Action puts them beside it)
import exitsigns  # noqa: E402
import milemarkers  # noqa: E402
import signshards  # noqa: E402
import throughsigns  # noqa: E402

WAY_KEYS = tuple(dict.fromkeys(throughsigns.WAY_KEYS + milemarkers.WAY_KEYS))
NODE_KEYS = tuple(dict.fromkeys(exitsigns.EXIT_KEYS + milemarkers.TAG_KEYS + ("highway",)))
MAIN, LINKS = set(exitsigns.MAIN_CLASSES), set(exitsigns.LINK_CLASSES)
MARGIN_DEG = 0.05           # mainline ways this far round an exit tile (latitude degrees: ~5.5 km > APPROACH_M)
RAMP_KEEP_M = 320.0         # of a ramp: the gore search reads its first exitsigns.GORE_SEARCH_M
SIMPLIFY_M = 0.5            # the paths' Douglas-Peucker tolerance
MILE_PAD_DEG = 0.003        # milestones this far outside an Interstate's ways (osm_records' own bbox pad)


def _intern(cache, tags):
    """One shared dict per distinct tag set (millions of ways share a few thousand)."""
    key = tuple(sorted(tags.items()))
    got = cache.get(key)
    if got is None:
        got = cache[key] = dict(key)
    return got


def _rail():
    """build_rail_tags (pyosmium: the passes over the planet; the rest of this builder needs neither)."""
    import build_rail_tags
    return build_rail_tags


def first_pass(src):
    """(main ways, links [(id, node refs, tags)], junction node tags {id: tags}, milestones {id: (lon, lat, tags)},
    relations {way id: [{direction, role}]}, route ways [(way id, route, direction, state)])."""
    with _rail()._Source(src) as f:
        return _first_pass(f)


def _first_pass(src):
    import osmium
    main, links, junctions, milestones = [], [], {}, {}
    relations, route_rows, cache = defaultdict(list), [], {}
    main_ids = set()
    t0, k = time.time(), 0
    fp = osmium.FileProcessor(src, osmium.osm.NODE | osmium.osm.WAY | osmium.osm.RELATION)
    for o in fp.with_filter(osmium.filter.KeyFilter("highway", "route")):
        k += 1
        if k % 1_000_000 == 0:
            print(f"  pass 1: {k:,} objects, {len(main):,} mainline ways, {len(links):,} links, "
                  f"{time.time() - t0:.0f} s", flush=True)
        tags = o.tags
        if o.is_node():
            hw = tags.get("highway")
            if hw not in ("motorway_junction", "milestone") or not o.location.valid():
                continue
            t = {key: tags[key] for key in NODE_KEYS if key in tags}
            if hw == "motorway_junction":
                junctions[o.id] = t
            elif "distance" in t:
                milestones[o.id] = (o.location.lon, o.location.lat, t)
        elif o.is_way():
            hw = tags.get("highway")
            if hw not in MAIN and hw not in LINKS or len(o.nodes) < 2:
                continue
            row = (o.id, np.array([n.ref for n in o.nodes], np.int64),
                   _intern(cache, {key: tags[key] for key in WAY_KEYS if key in tags}))
            if hw in MAIN:
                main.append(row)
                main_ids.add(o.id)
            else:
                links.append(row)
        elif o.is_relation():
            if tags.get("route") != "road":
                continue
            ref = str(tags.get("ref", ""))
            direction = milemarkers.cardinal(tags.get("direction"))
            state = tags.get("is_in:state", "")
            interstate = tags.get("network") == "US:I" and re.fullmatch(r"\d{1,3}", ref)
            for m in o.members:
                if m.type != "w" or m.ref not in main_ids:
                    continue
                posted = throughsigns.cardinal(m.role) or throughsigns.cardinal(tags.get("direction"))
                if posted:
                    relations[m.ref].append({"direction": posted, "role": m.role})
                if interstate:
                    route_rows.append((m.ref, int(ref), direction, state))
    return main, links, junctions, milestones, dict(relations), route_rows


class Places:
    """The nodes' places (second pass): sorted ids, lon, lat."""

    def __init__(self, nid, lon, lat):
        self.nid, self.lon, self.lat = nid, lon, lat

    def coords(self, refs):
        """{node id: (lon, lat)} of the refs (an int64 array) that have a place."""
        refs = np.unique(refs)
        j = np.searchsorted(self.nid, refs)
        ok = (j < len(self.nid)) & (self.nid[np.minimum(j, len(self.nid) - 1)] == refs)
        j = j[ok]
        return dict(zip(refs[ok].tolist(), zip(self.lon[j].tolist(), self.lat[j].tolist())))

    def bboxes(self, ways):
        """(n, 4) lon0, lat0, lon1, lat1 of each way's placed nodes (NaN: none placed)."""
        if not ways:
            return np.zeros((0, 4))
        lens = np.array([len(r) for _, r, _ in ways])
        refs = np.concatenate([r for _, r, _ in ways])
        j = np.searchsorted(self.nid, refs)
        ok = (j < len(self.nid)) & (self.nid[np.minimum(j, len(self.nid) - 1)] == refs)
        lon = np.where(ok, self.lon[np.minimum(j, len(self.nid) - 1)], np.nan)
        lat = np.where(ok, self.lat[np.minimum(j, len(self.nid) - 1)], np.nan)
        starts = np.r_[0, np.cumsum(lens)[:-1]]
        with np.errstate(invalid="ignore"):
            return np.c_[np.fmin.reduceat(lon, starts), np.fmin.reduceat(lat, starts),
                         np.fmax.reduceat(lon, starts), np.fmax.reduceat(lat, starts)]

    def point(self, ref):
        j = int(np.searchsorted(self.nid, ref))
        if j < len(self.nid) and self.nid[j] == ref:
            return float(self.lon[j]), float(self.lat[j])
        return None


def _simplify(path, tol_m=SIMPLIFY_M):
    """Douglas-Peucker on a lon/lat path (metres by a local scale), its ends kept."""
    p = np.asarray(path, np.float64)
    if len(p) < 3:
        return p
    q = p * [111320.0 * math.cos(math.radians(float(p[:, 1].mean()))), 110540.0]
    keep = np.zeros(len(p), bool)
    keep[[0, -1]] = True
    stack = [(0, len(p) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ab = q[b] - q[a]
        n = float(np.hypot(*ab))
        seg = q[a + 1:b] - q[a]
        d = np.abs(seg[:, 0] * ab[1] - seg[:, 1] * ab[0]) / n if n > 0 else np.hypot(seg[:, 0], seg[:, 1])
        i = int(np.argmax(d))
        if d[i] > tol_m:
            keep[a + 1 + i] = True
            stack += [(a, a + 1 + i), (a + 1 + i, b)]
    return p[keep]


def _head(path, keep_m):
    """The first keep_m of a lon/lat path (its last point cut at keep_m)."""
    p = np.asarray(path, np.float64)
    if len(p) < 2:
        return p
    q = p * [111320.0 * math.cos(math.radians(float(p[:, 1].mean()))), 110540.0]
    arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(q, axis=0).T))]
    if arc[-1] <= keep_m:
        return p
    k = int(np.searchsorted(arc, keep_m))
    t = (keep_m - arc[k - 1]) / max(arc[k] - arc[k - 1], 1e-9)
    return np.vstack([p[:k], p[k - 1] + t * (p[k] - p[k - 1])])


def _record(kind, oid, lon, lat, d):
    lon, lat = round(float(lon), 7), round(float(lat), 7)
    d = dict(d)
    for p in signshards.PATHS:
        if p in d:
            d[p] = signshards.encode_path(_simplify(d[p]), lon, lat)
    return {"id": int(oid), "k": kind, "lon": lon, "lat": lat, "d": d}


def exits(main, links, junctions, relations, places):
    """exitsigns.index_records per z7 tile of the links' first nodes, with the mainline ways near it."""
    groups = defaultdict(list)
    for row in links:
        refs = row[1]
        start = refs[-1] if exitsigns._direction(row[2]) == -1 else refs[0]
        at = places.point(int(start))
        if at is not None:
            groups[signshards.tile(at[1], at[0])].append(row)
    boxes = places.bboxes(main)
    out, t0 = [], time.time()
    for i, (key, part) in enumerate(sorted(groups.items())):
        lon0, lat0, lon1, lat1 = signshards.tile_bounds(*key)
        pad_lon = MARGIN_DEG / max(0.05, math.cos(math.radians(max(abs(lat0), abs(lat1)))))
        near = np.flatnonzero((boxes[:, 2] >= lon0 - pad_lon) & (boxes[:, 0] <= lon1 + pad_lon)
                              & (boxes[:, 3] >= lat0 - MARGIN_DEG) & (boxes[:, 1] <= lat1 + MARGIN_DEG))
        ways = [(main[j][0], main[j][1].tolist(), main[j][2]) for j in near]
        part = [(w, r.tolist(), t) for w, r, t in part]
        refs = np.concatenate([np.asarray(r, np.int64) for _, r, _ in ways + part])
        coords = places.coords(refs)
        found = exitsigns.index_records(ways, part, coords, junctions)
        throughsigns.annotate_exits(found, relations)
        out += found
        if (i + 1) % 200 == 0:
            print(f"  exits: {i + 1:,} of {len(groups):,} tiles, {len(out):,} exits, {time.time() - t0:.0f} s",
                  flush=True)
    return out


def exit_records(found):
    """The shard records of the exits: what exitsigns / throughsigns place them by (the ramp's head, no tags)."""
    out = []
    for obj in found:
        e = obj["ways"][0]
        d = {"label": e["label"], "approach": e["approach"], "ramp": _head(e["ramp"], RAMP_KEEP_M).tolist(),
             "main_ids": e["main_ids"], "ramp_id": e["ramp_id"], "side": e["side"],
             "through_label": e.get("through_label")}
        # Keep the current mainline identity even without destination names.
        # French portiques can then show A4 without borrowing an exit's cities.
        d["main_tags"] = {k: e.get("main_tags", {})[k] for k in ("highway", "ref")
                          if k in e.get("main_tags", {})}
        out.append(_record("exit", obj["id"], obj["lon"], obj["lat"], d))
    return out


def through(main, found, relations, places):
    """throughsigns.index_records over the mainline ways with a destination block (few: a planet in one go)."""
    ways = [(w, r.tolist(), t) for w, r, t in main if throughsigns.label(t, relations.get(w, ())) is not None]
    if not ways:
        return []
    coords = places.coords(np.concatenate([np.asarray(r, np.int64) for _, r, _ in ways]))
    light = [o for o in found if o["ways"][0].get("through_label")]
    rows = throughsigns.index_records(ways, coords, light, relations)
    return [_record("through", o["id"], o["lon"], o["lat"], o["ways"][0]) for o in rows]


def miles(main, milestones, route_rows, places):
    """milemarkers.osm_records one Interstate at a time, with the milestones near its ways."""
    by_way = defaultdict(set)
    for wid, route, _d, _s in route_rows:
        by_way[wid].add(route)
    routes = defaultdict(list)
    for row in main:
        if row[2].get("highway") != "motorway":
            continue
        rs = by_way.get(row[0]) or {milemarkers.interstate(row[2].get("ref"))} - {None}
        for r in rs:
            routes[r].append(row)
    if not milestones or not routes:
        return []
    ms = sorted(milestones.items())
    mlon = np.array([v[0] for _, v in ms])
    mlat = np.array([v[1] for _, v in ms])
    mroute = [milemarkers.interstate(v[2].get("carriageway_ref") or v[2].get("ref", "")) for _, v in ms]
    out = {}
    for route, rows in sorted(routes.items()):
        box = places.bboxes(rows)
        if not np.isfinite(box).any():
            continue
        lon0, lat0 = np.nanmin(box[:, 0]) - MILE_PAD_DEG, np.nanmin(box[:, 1]) - MILE_PAD_DEG
        lon1, lat1 = np.nanmax(box[:, 2]) + MILE_PAD_DEG, np.nanmax(box[:, 3]) + MILE_PAD_DEG
        pick = np.flatnonzero((mlon >= lon0) & (mlon <= lon1) & (mlat >= lat0) & (mlat <= lat1))
        markers = {ms[i][0]: ms[i][1] for i in pick if mroute[i] in (None, route)}
        if not markers:
            continue
        ways = [(w, r.tolist(), t) for w, r, t in rows]
        coords = places.coords(np.concatenate([r for _, r, _ in rows]))
        rr = [row for row in route_rows if row[1] == route]
        for o in milemarkers.osm_records(ways, coords, markers, rr):
            out[o["id"]] = _record("mile", o["id"], o["lon"], o["lat"], o["mile_marker"])
    return [out[k] for k in sorted(out)]


def write(out, records, stats):
    shards = defaultdict(list)
    for r in records:
        shards[signshards.tile(r["lat"], r["lon"])].append(r)
    total = 0
    for (sx, sy), recs in sorted(shards.items()):
        d = os.path.join(out, str(sx))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{sy}.json.gz")
        recs.sort(key=lambda r: (r["k"], r["id"]))
        body = json.dumps({"objects": recs}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        with open(path + ".part", "wb") as fh:          # (mtime 0: the same planet gives the same bytes)
            with gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=9, mtime=0) as gz:
                gz.write(body)
        os.replace(path + ".part", path)
        total += os.path.getsize(path)
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"format": signshards.FORMAT, "zoom": signshards.ZOOM,
                   "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "shards": len(shards), **stats,
                   "license": "ODbL 1.0, (c) OpenStreetMap contributors"}, fh)
    print(f"{stats}, {len(shards):,} shards, {total / 1e6:.1f} MB", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("planet", help="the planet (or an extract): a .osm.pbf path, an http(s) URL (streamed), '-'")
    ap.add_argument("out")
    ap.add_argument("--second-pass", help="the planet again for the second pass (a path, or '-' stdin: streaming)")
    a = ap.parse_args()
    t0 = time.time()
    main_ways, links, junctions, milestones, relations, route_rows = first_pass(a.planet)
    print(f"pass 1: {len(main_ways):,} mainline ways, {len(links):,} links, {len(junctions):,} junctions, "
          f"{len(milestones):,} milestones, {len(route_rows):,} Interstate members, {time.time() - t0:.0f} s",
          flush=True)
    ids = (np.unique(np.concatenate([r for _, r, _ in main_ways + links])) if main_ways or links
           else np.zeros(0, np.int64))
    places = Places(*_rail().second_pass(a.second_pass or a.planet, ids))
    print(f"pass 2: {len(places.nid):,} node places, {time.time() - t0:.0f} s", flush=True)
    found = exits(main_ways, links, junctions, relations, places)
    print(f"exits: {len(found):,}, {time.time() - t0:.0f} s", flush=True)
    over = through(main_ways, found, relations, places)
    print(f"overhead stations: {len(over):,}, {time.time() - t0:.0f} s", flush=True)
    mile = miles(main_ways, milestones, route_rows, places)
    print(f"mile references: {len(mile):,}, {time.time() - t0:.0f} s", flush=True)
    write(a.out, exit_records(found) + over + mile,
          {"exits": len(found), "through": len(over), "miles": len(mile)})
    print(f"done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    sys.exit(main())
