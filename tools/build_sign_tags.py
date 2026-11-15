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
# The German raw block (signshards.DE_VERSION): exit records whose junction lies inside signshards.DE_OUTLINE keep the
# sign text as mapped, for the German Autobahn signs (server/deautobahn.py). Kept outside the shared tag sets.
DE_WAY_KEYS = ("turn:lanes", "int_ref", "destination:to", "destination:symbol", "destination:colour",
               "destination:country", "destination:arrow", "destination:symbol:lanes", "destination:colour:lanes",
               "destination:country:lanes", "destination:arrow:lanes", "destination:to:lanes")
DE_LANE_KEYS = ("lanes", "turn:lanes", "destination:lanes", "destination:ref:lanes", "destination:symbol:lanes",
                "destination:colour:lanes", "destination:country:lanes")
DE_LINK_KEYS = ("destination", "destination:ref", "destination:to", "destination:ref:to", "destination:symbol",
                "destination:colour", "destination:country", "destination:arrow")
DE_THROUGH_KEYS = ("ref", "int_ref", "lanes", "destination", "destination:ref", "destination:symbol",
                   "destination:colour", "destination:country")
DE_MARGIN_DEG = 0.2         # the German graphs round a tile: the next junction lies up to ~12 km away
DE_WALK_M = 2500.0          # the long approach: the 2000 m Kreuz board from 125 m before the gore, and margin
DE_PREV_M = 4000.0          # how far upstream the previous junction's entry is looked for
DE_NEXT_M = 12000.0         # how far downstream the next junction is looked for
DE_LANES_SRC_M = 1000.0     # the advance sign's lane destinations: on a way starting this near the junction
DE_RAMP_M = 800.0           # the link chain kept (to its first fork)
DE_BRANCH_M = 150.0         # of each branch after the fork (the fork boards' arrows)
DE_MERGE_LANE_M = 250.0     # an entry's merge lane after the link joins (Einfaedelungsstreifen, RAA)
DE_REST = re.compile(r"\s*(Rastplatz|Rastst[aä]tte|Rasthof|Parkplatz|Tank- und Rast|Autohof)", re.I)


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


def first_pass(src, de_tags=None):
    """(main ways, links [(id, node refs, tags)], junction node tags {id: tags}, milestones {id: (lon, lat, tags)},
    relations {way id: [{direction, role}]}, route ways [(way id, route, direction, state)]). de_tags, a dict, gets
    {way id: its DE_WAY_KEYS} of the motorway / trunk ways and links that have any (the German block's extra keys)."""
    with _rail()._Source(src) as f:
        return _first_pass(f, de_tags)


def _first_pass(src, de_tags=None):
    import osmium
    main, links, junctions, milestones = [], [], {}, {}
    relations, route_rows, cache, de_cache = defaultdict(list), [], {}, {}
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
            if de_tags is not None:
                extra = {key: tags[key] for key in DE_WAY_KEYS if key in tags}
                if extra:
                    de_tags[o.id] = _intern(de_cache, extra)
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


def exits(main, links, junctions, relations, places, de_tags=None):
    """exitsigns.index_records per z7 tile of the links' first nodes, with the mainline ways near it; the exits inside
    Germany get the German block (_german). de_tags: first_pass's extra keys (None: the ways' own tags have them)."""
    groups = defaultdict(list)
    for row in links:
        refs = row[1]
        start = refs[-1] if exitsigns._direction(row[2]) == -1 else refs[0]
        at = places.point(int(start))
        if at is not None:
            groups[signshards.tile(at[1], at[0])].append(row)
    boxes = places.bboxes(main)
    link_boxes = None
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
        if found and _touches_germany(lon0, lat0, lon1, lat1):
            if link_boxes is None:
                link_boxes = places.bboxes(links)
            _german(found, key, main, links, junctions, places, boxes, link_boxes, de_tags)
        out += found
        if (i + 1) % 200 == 0:
            print(f"  exits: {i + 1:,} of {len(groups):,} tiles, {len(out):,} exits, {time.time() - t0:.0f} s",
                  flush=True)
    return out



def _touches_germany(lon0, lat0, lon1, lat1):
    b = signshards._DE_BOX
    return lon1 >= b[0] and lon0 <= b[2] and lat1 >= b[1] and lat0 <= b[3]


def _merged(tags, wid, de_tags):
    extra = de_tags.get(wid) if de_tags is not None else None
    return {**tags, **extra} if extra else tags


def _pick(tags, keys):
    return {k: tags[k] for k in keys if k in tags}


def _length(coords, nodes):
    q = np.asarray([coords[n] for n in nodes], np.float64)
    if len(q) < 2:
        return 0.0
    q = q * [111320.0 * math.cos(math.radians(float(q[:, 1].mean()))), 110540.0]
    return float(np.hypot(*np.diff(q, axis=0).T).sum())


class _Graph:
    """The German graphs round a tile: the mainline ways' incoming / outgoing edges, the links by their first node
    (in their driving direction), the nodes links end at (entries) and the mainline ways through each node."""

    def __init__(self, mains, lks, places, de_tags):
        refs = [r for _, r, _ in mains + lks]
        self.coords = places.coords(np.concatenate(refs)) if refs else {}
        self.incoming, self.outgoing, self.ways_at = defaultdict(list), defaultdict(list), defaultdict(list)
        self.tags, self.links, self.link_from, self.link_to = {}, {}, defaultdict(list), defaultdict(list)
        for wid, r, tags in mains:
            t = self.tags[wid] = _merged(tags, wid, de_tags)
            r = r.tolist()
            for d in ((1, -1) if exitsigns._direction(tags) == 0 else (exitsigns._direction(tags),)):
                path = r if d == 1 else r[::-1]
                for a, b in zip(path[:-1], path[1:]):
                    if a in self.coords and b in self.coords and a != b:
                        self.incoming[b].append((a, wid, t))
                        self.outgoing[a].append((b, wid, t))
            for n in r:
                self.ways_at[n].append(wid)
        for wid, r, tags in lks:
            self.tags[wid] = _merged(tags, wid, de_tags)
            r = r.tolist()[::-1] if exitsigns._direction(tags) == -1 else r.tolist()
            r = [n for n in r if n in self.coords]
            if len(r) < 2:
                continue
            self.links[wid] = r
            self.link_from[r[0]].append(wid)
            self.link_to[r[-1]].append(wid)

    def follow(self, wid, limit):
        """The link chain from link wid along its single continuations up to limit metres: (nodes, the ways, the
        following links where it forks or [])."""
        nodes, ways = list(self.links[wid]), [wid]
        while _length(self.coords, nodes) < limit:
            nxt = sorted(w for w in self.link_from.get(nodes[-1], ()) if w not in ways)
            if len(nxt) != 1:
                return nodes, ways, nxt
            nodes += self.links[nxt[0]][1:]
            ways.append(nxt[0])
        return nodes, ways, []

    def link_tags(self, ways):
        """The destination keys of the first of the ways that has a destination ({}: none has)."""
        for w in ways:
            if "destination" in self.tags[w]:
                return _pick(self.tags[w], DE_LINK_KEYS)
        return {}

    def origins(self, node, junctions, limit=3000.0):
        """The junction nodes {(ref, name)} the link chains entering at node come from (walked back up to limit m)."""
        out, todo, seen = set(), [(node, 0.0)], set()
        while todo:
            at, run = todo.pop()
            for w in self.link_to.get(at, ()):
                if w in seen:
                    continue
                seen.add(w)
                start, length = self.links[w][0], run + _length(self.coords, self.links[w])
                t = junctions.get(start)
                if t is not None:
                    out.add((t.get("ref"), t.get("name")))
                elif start not in self.ways_at and length < limit:
                    todo.append((start, length))
        return out

    def joins(self, wid, own_ref):
        """The ref of the motorway the link chain from wid merges into (another than own_ref), or None."""
        nodes, ways, fork = self.follow(wid, 3000.0)
        ends = [nodes[-1]] + [self.follow(w, 3000.0)[0][-1] for w in fork]
        for n in ends:
            for w in self.ways_at.get(n, ()):
                t = self.tags[w]
                if t.get("highway") == "motorway" and t.get("ref") and t.get("ref") != own_ref:
                    return t["ref"]
        return None


def _german(found, key, main, links, junctions, places, boxes, link_boxes, de_tags):
    """The German raw block (signshards.DE_VERSION) of the found exits whose junction is inside DE_OUTLINE, in their
    extras: "de" and the paths "de_approach" (DE_WALK_M upstream) and "de_ramp" (the link chain to its first fork)."""
    inside = signshards.in_germany(np.array([o["lon"] for o in found]), np.array([o["lat"] for o in found]))
    if not inside.any():
        return
    lon0, lat0, lon1, lat1 = signshards.tile_bounds(*key)
    pad_lon = DE_MARGIN_DEG / max(0.05, math.cos(math.radians(max(abs(lat0), abs(lat1)))))

    def near(b):
        return np.flatnonzero((b[:, 2] >= lon0 - pad_lon) & (b[:, 0] <= lon1 + pad_lon)
                               & (b[:, 3] >= lat0 - DE_MARGIN_DEG) & (b[:, 1] <= lat1 + DE_MARGIN_DEG))
    g = _Graph([main[j] for j in near(boxes)], [links[j] for j in near(link_boxes)], places, de_tags)
    for obj, here in zip(found, inside):
        if here:
            block = _german_block(obj["ways"][0], g, junctions)
            if block is not None:
                obj["ways"][0].update(block)


def _german_block(e, g, junctions):
    """{"de", "de_approach", "de_ramp"} of one exit's extras, or None (its link or first step is not in the graph, or it
    leaves a trunk: Autobahnen only, a Bundesstrasse's exits keep their plain record)."""
    coords, chain = g.coords, g.links.get(e["ramp_id"])
    if chain is None or g.tags.get(e["main_id"], {}).get("highway") != "motorway":
        return None
    nid = chain[0]
    behind = tuple(e["approach"][-2])
    previous = next((a for a, wid, _ in g.incoming.get(nid, ())
                     if wid == e["main_id"] and tuple(coords[a]) == behind), None)
    if previous is None:
        return None
    nodes, s, edges, stop = exitsigns.de_walk(g.incoming, coords, nid, previous, e["main_id"], DE_WALK_M)
    edge_rows = [[wid, round(a, 1), round(b, 1), _pick(g.tags[wid], DE_LANE_KEYS)] for wid, a, b in edges]
    lanes_src = next((i for i, r in enumerate(edge_rows)
                      if r[1] < DE_LANES_SRC_M and "destination:lanes" in r[3]), -1)
    # the link chain to its first fork, and 150 m into each branch
    ramp_nodes, ramp_ways, fork = g.follow(e["ramp_id"], DE_RAMP_M)
    fork_s = round(_length(coords, ramp_nodes), 1) if fork else None
    branches = []
    for w in fork:
        b_nodes, b_ways, _ = g.follow(w, DE_RAMP_M)
        path = _head([coords[n] for n in [ramp_nodes[-1]] + b_nodes[1:]], DE_BRANCH_M).tolist()
        branches.append({"s0": fork_s, "tags": g.link_tags(b_ways), "path": path})
    main_tags = g.tags[e["main_id"]]
    own_ref = main_tags.get("ref", "")
    # the through way: the outgoing mainline edge best in line with the approach
    travel = exitsigns._de_unit(exitsigns._de_delta(coords, previous, nid))
    ahead = sorted(((float(exitsigns._de_unit(exitsigns._de_delta(coords, nid, b)) @ travel), b, wid)
                    for b, wid, _ in g.outgoing.get(nid, ())), key=lambda c: (-c[0], c[2], c[1]))
    through, nxt = None, None
    if ahead and ahead[0][0] >= .65:
        _, b, wid = ahead[0]
        through = {"way": wid, **_pick(g.tags[wid], DE_THROUGH_KEYS)}
        jt = junctions.get(nid, {})
        d_nodes, d_s, _, _ = exitsigns.de_walk(g.outgoing, coords, nid, b, wid, DE_NEXT_M, downstream=True)
        for n, sn in zip(d_nodes[1:], d_s[1:]):
            t = junctions.get(n)
            if t is None or n not in g.link_from:
                continue
            name, ref = t.get("name"), t.get("ref")
            if not (name or ref) or (name and DE_REST.match(name)) or (ref and ref == jt.get("ref")) \
                    or (name and name == jt.get("name")):
                continue
            nxt = {"name": name or "", "ref": ref or "", "s": round(sn, 1)}
            break
    # the previous junction's entry: the first merge upstream that is not this junction's own (a cloverleaf loop)
    # and the previous junction's exit (the dense-junction bound: real boards stand between it and its entry)
    jt = junctions.get(nid, {})
    prev = prev_exit = None
    u_nodes, u_s, _, _ = exitsigns.de_walk(g.incoming, coords, nid, previous, e["main_id"], DE_PREV_M)
    for n, sn in zip(u_nodes[1:], u_s[1:]):
        t = junctions.get(n)
        if prev_exit is None and t is not None and n in g.link_from and (t.get("ref") or t.get("name")) \
                and not (t.get("name") and DE_REST.match(t.get("name"))) \
                and not ((t.get("ref") and t.get("ref") == jt.get("ref")) or (t.get("name") and t.get("name") == jt.get("name"))):
            prev_exit = round(sn, 1)
        if prev is None and n in g.link_to and not any((r and r == jt.get("ref")) or (m and m == jt.get("name"))
                                                       for r, m in g.origins(n, junctions)):
            prev = round(max(0.0, sn - DE_MERGE_LANE_M), 1)
        if prev is not None and prev_exit is not None:
            break
    de = {"v": signshards.DE_VERSION,
          "node": {"ref": jt.get("ref"), "name": jt.get("name"), "noref": str(jt.get("noref", "")).lower() == "yes"},
          "edges": edge_rows, "lanes_src": lanes_src,
          "link": g.link_tags(ramp_ways), "fork_s": fork_s, "branches": branches,
          "through": through, "main": {"ref": own_ref, "int_ref": main_tags.get("int_ref", "")},
          "joins": g.joins(e["ramp_id"], own_ref), "next": nxt, "prev_merge_s": prev, "prev_exit_s": prev_exit,
          "stop": stop}
    return {"de": de, "de_approach": [coords[n] for n in nodes[::-1]],
            "de_ramp": _head([coords[n] for n in ramp_nodes], DE_RAMP_M).tolist()}

def exit_records(found):
    """The shard records of the exits: what exitsigns / throughsigns place them by (the ramp's head, no tags); inside
    Germany also the German block and its two paths (_german)."""
    out = []
    for obj in found:
        e = obj["ways"][0]
        d = {"label": e["label"], "approach": e["approach"], "ramp": _head(e["ramp"], RAMP_KEEP_M).tolist(),
             "main_ids": e["main_ids"], "ramp_id": e["ramp_id"], "side": e["side"],
             "through_label": e.get("through_label")}
        if "de" in e:
            d["de_approach"], d["de_ramp"] = e["de_approach"], e["de_ramp"]
            d["de"] = json.loads(json.dumps(e["de"]))          # (a copy: its branch paths are encoded below)
        rec = _record("exit", obj["id"], obj["lon"], obj["lat"], d)
        for b in rec["d"].get("de", {}).get("branches", ()):
            b["path"] = signshards.encode_path(_simplify(b["path"]), rec["lon"], rec["lat"])
        out.append(rec)
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
        shard = {"objects": recs}
        if any("de" in r["d"] for r in recs):
            shard["de"] = signshards.DE_VERSION           # (the server's direction boards step back here)
        body = json.dumps(shard, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
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
    de_tags = {}
    main_ways, links, junctions, milestones, relations, route_rows = first_pass(a.planet, de_tags)
    print(f"pass 1: {len(main_ways):,} mainline ways, {len(links):,} links, {len(junctions):,} junctions, "
          f"{len(milestones):,} milestones, {len(route_rows):,} Interstate members, {time.time() - t0:.0f} s",
          flush=True)
    ids = (np.unique(np.concatenate([r for _, r, _ in main_ways + links])) if main_ways or links
           else np.zeros(0, np.int64))
    places = Places(*_rail().second_pass(a.second_pass or a.planet, ids))
    print(f"pass 2: {len(places.nid):,} node places, {time.time() - t0:.0f} s", flush=True)
    found = exits(main_ways, links, junctions, relations, places, de_tags)
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
