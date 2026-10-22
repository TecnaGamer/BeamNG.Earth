#!/usr/bin/env python3
"""Where a kunstwerk's street furniture stands, as the rules for Dutch junctions put it (not on the map's points, which
sit on the road's middle): ref/placement.json, for tools/kunstwerk_props.py's modules.

- a traffic light: at each stop line (a solid bar in the paint, ref/markings.json, square to the lane dashes, near an
  OSM signal: a signalled one). Its traffic comes from its side away from the junction (the signals' middle round it).
  The primary stands at the line's right end, just off the carriageway (LIGHT_OFF_M) and PAST_LINE_M past the line,
  facing that traffic; where a median (at most ISLAND_MAX_M wide) bounds the line's left end, a repeater stands on it.
- an L2 (zebra): at OSM's zebra, on the right of each way's traffic, just off the carriageway, facing that traffic.
- a D2 (keep right): on each island at a junction, at its nose toward the traffic (its point farthest from the
  junction), facing that traffic.

    python3 tools/kunstwerk_placement.py ../design/kunstwerken/droogleever/ref

Writes ref/placement.json: {"items": [{"module": "traffic_light" | "sign", "code": ..., "role": ..., "at": [x, y],
"facing": [fx, fy]}]}; a light's "bike" when a cycle path runs beside it.
"""
import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server"))

JUNCTION_M = 30.0       # m round a signal the signals whose middle is its junction's
AXIS_REACH_M = 14.0     # m round a signal whose lane dashes give the road's direction
LIGHT_OFF_M = 0.8       # m a light stands off its carriageway's edge
ISLAND_MAX_M = 6.0      # m: a median at most this wide carries a repeater
REACH_M = 25.0          # m a signal's carriageway edge is looked for
DEDUP_M = 2.5           # m: two lights nearer than this are one
STOP_W = (0.2, 0.6)     # m: a stop line's width (as the photo draws it)
STOP_MIN_M = 2.4        # m: and its length at least (one lane)
STOP_NEAR_SIGNAL_M = 25.0  # m: a stop line this near an OSM signal is a signalled one
HAND_REACH_M = 3.0      # m: a hand-read stop line's end is this near its kerb
PAST_LINE_M = 0.8       # m a primary light stands past its stop line
OSM_MATCH_M = 15.0      # m: a primary this near an OSM signal, facing its way's traffic, is that signal's
MEDIAN_MIN_M = 1.5      # m: a strip off the road this wide on the lanes' left (a median, an island, the tram's grass)
                        # carries their mast, its arm over them to the right (Street View A269, B20)
MAST_LANES = 3          # lanes in (OSM's lanes) from which the primary is a mast with an arm over them
OSM_CARS = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "service",
            "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}


def unit(v):
    v = np.asarray(v, np.float64)
    return v / max(np.hypot(*v), 1e-9)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ref")
    a = ap.parse_args(argv)
    import ahn as ahnmod
    import kunstwerk_solid as ks
    from kunstwerk_design import carriageways
    ref = json.load(open(os.path.join(a.ref, "ref.json"), encoding="utf-8"))
    ks.REF_DIR[:] = [a.ref]
    frame, _ = carriageways(a.ref)
    c0, u, n = frame
    corners = np.array([c0 + u * t + n * w for t in ks.AREA_T for w in (-ks.AREA_W, ks.AREA_W)])
    box = (*np.floor(corners.min(0)), *np.ceil(corners.max(0)))
    plan = ks.Plan(ref, frame, box)
    names = [k[0] for k in ks.KINDS]
    ROAD, CYCLE, ISLAND = names.index("road"), names.index("cycle"), names.index("island")
    TRAM = names.index("tram")
    osm = json.load(open(os.path.join(a.ref, "osm_signals.json")))["elements"]

    def local(e):
        rx, ry = ahnmod.wgs_to_rd(e["lat"], e["lon"])
        return np.array([rx - ref["origin_rd"][0], ry - ref["origin_rd"][1]])

    def inside_box(p):
        return box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]
    # the lane dashes: the marks longer than wide by far, their axes
    dashes, bars, solid = [], [], []
    for m in json.load(open(os.path.join(a.ref, "markings.json")))["marks"]:
        r = np.asarray(m["rings"][0], np.float64)
        if len(r) != 4:
            continue
        e1, e2 = r[1] - r[0], r[2] - r[1]
        L, W = max(np.hypot(*e1), np.hypot(*e2)), min(np.hypot(*e1), np.hypot(*e2))
        if W <= 0.2 and 1.5 <= L <= 6.0:
            dashes.append((r.mean(0), unit(e1 if np.hypot(*e1) > np.hypot(*e2) else e2)))
        elif 0.35 <= W <= 1.0 and 2.0 <= L <= 6.0:           # a zebra's bar: along the road too
            bars.append((r.mean(0), unit(e1 if np.hypot(*e1) > np.hypot(*e2) else e2)))
        if STOP_W[0] <= W <= STOP_W[1] and L >= STOP_MIN_M:  # maybe a stop line (if it runs across the road)
            ax_ = unit(e1 if np.hypot(*e1) > np.hypot(*e2) else e2)
            c_ = r.mean(0)
            solid.append((c_ - ax_ * L / 2, c_ + ax_ * L / 2))
    dpos = np.array([d[0] for d in dashes]) if dashes else np.zeros((0, 2))
    ddir = np.array([d[1] for d in dashes]) if dashes else np.zeros((0, 2))

    bpos = np.array([b[0] for b in bars]) if bars else np.zeros((0, 2))
    bdir = np.array([b[1] for b in bars]) if bars else np.zeros((0, 2))

    def road_axis(p, zebra=False):
        """The road's direction at p (an axis: sign arbitrary): its lane dashes' mean (at a zebra its bars'), or
        None."""
        pos, dirs, reach = (bpos, bdir, 6.0) if zebra else (dpos, ddir, AXIS_REACH_M)
        if not len(pos):
            return None
        near = np.hypot(*(pos - p).T) < reach
        if near.sum() < 2:
            return None if zebra else road_axis(p, zebra=True) if len(bpos) else None
        ang = np.arctan2(dirs[near, 1], dirs[near, 0])
        m = np.angle(np.mean(np.exp(2j * ang))) / 2      # (an axis: angles doubled, averaged, halved)
        return np.array([np.cos(m), np.sin(m)])

    def edge(p, d):
        """From p along d: the carriageway's edge (its last road point) and what lies beyond: (edge, kind beyond,
        width of that non-road strip (inf: no road again within ISLAND_MAX_M))."""
        s = np.arange(0.0, REACH_M, 0.2)
        ray = p + np.outer(s, d)
        k = plan.kind(*ray.T)
        off = np.flatnonzero(k != ROAD)
        if not len(off):
            return None, None, None
        i = off[0]
        back = np.flatnonzero(k[i:] == ROAD)
        width = s[i + back[0]] - s[i] if len(back) else np.inf
        return ray[max(i - 1, 0)], int(k[i]), width

    sigs = [local(e) for e in osm if e["tags"].get("highway") == "traffic_signals"
            or e["tags"].get("crossing") == "traffic_signals"]
    sigs = [p for p in sigs if inside_box(p)]
    # each signal's junction: the middle of the signals round it (chaining them ran round the whole ring and put the
    # middle in the oval, 2026-10-21)
    S = np.array(sigs) if sigs else np.zeros((0, 2))
    centre = {i: S[np.hypot(*(S - p).T) < JUNCTION_M].mean(0) for i, p in enumerate(sigs)}
    jid = list(range(len(sigs)))
    items, skipped = [], []

    def add(item):
        p = np.asarray(item["at"])
        if any(np.hypot(*(p - np.asarray(o["at"]))) < DEDUP_M and o["module"] == item["module"] for o in items):
            return
        item["at"] = [round(float(v), 3) for v in item["at"]]
        item["facing"] = [round(float(v), 4) for v in item["facing"]]
        items.append(item)
    # the ways in read off the photo by hand (ref/approaches.json: each stop line from its left end to its right end
    # as its drivers see it) where there are such; else found round each signalled junction (below)
    hand = os.path.join(a.ref, "approaches.json")
    if os.path.exists(hand):
        for ap_ in json.load(open(hand))["approaches"]:
            lft, rgt = np.asarray(ap_["left"], np.float64), np.asarray(ap_["right"], np.float64)
            right = unit(rgt - lft)
            h = np.array([-right[1], right[0]])            # the way in: the line's right turned a quarter left
            e, _, _ = edge(rgt - right * 0.8, right)
            if e is not None and np.hypot(*(e - rgt)) <= HAND_REACH_M:
                q = e + right * LIGHT_OFF_M + h * PAST_LINE_M
                near = plan.kind(*(q + 2.5 * np.c_[np.cos(np.linspace(0, 6.28, 12)),
                                                   np.sin(np.linspace(0, 6.28, 12))]).T)
                add({"module": "traffic_light", "role": "primary", "at": q, "facing": -h,
                     "bike": bool((near == CYCLE).any())})
            el, _, wl = edge(lft + right * 0.8, -right)
            if el is not None and np.hypot(*(el - lft)) <= HAND_REACH_M:     # (its kerb, not a road beyond)
                q2 = el - right * (min(LIGHT_OFF_M, wl / 2) if np.isfinite(wl) else LIGHT_OFF_M) + h * PAST_LINE_M
                if plan.kind(*q2[:, None])[0] != ROAD:
                    add({"module": "traffic_light", "role": "repeater", "at": q2, "facing": -h, "bike": False})
    # the approaches: round each signalled junction (its signals' middle) a circle at its signals' distance; every
    # carriageway crossing it is a mouth. Of two mouths with a median between them the one with the median on its
    # left is the way in (keeping right); a lone mouth's way in is its right half. A way in with an OSM signal on it
    # gets its lights: the primary on the kerb at its right, level with the signal (OSM puts it on the stop line),
    # facing the traffic; a repeater on the median at its left.
    juncs = []
    for c in ([] if os.path.exists(hand) else centre.values()):
        if all(np.hypot(*(c - d)) > 15 for d in juncs):
            juncs.append(c)
    ang = np.radians(np.arange(0, 360, 0.5))
    for C in juncs:
        mine = S[np.hypot(*(S - C).T) < JUNCTION_M]
        R = float(np.clip(np.median(np.hypot(*(mine - C).T)), 8.0, 25.0))
        ring = C + R * np.c_[np.cos(ang), np.sin(ang)]
        hit = plan.kind(*ring.T) == ROAD
        if hit.all() or not hit.any():
            continue
        k0 = int(np.argmin(hit))
        runs, cur = [], []
        for j in range(len(ang)):
            jj = (k0 + j) % len(ang)
            if hit[jj]:
                cur.append(jj)
            elif cur:
                runs.append(cur)
                cur = []
        if cur:
            runs.append(cur)
        runs = [r for r in runs if np.hypot(*(ring[r[-1]] - ring[r[0]])) >= 2.5]
        nr = len(runs)
        for i, run in enumerate(runs):
            a0, a1 = ring[run[0]], ring[run[-1]]           # a1: the inbound drivers' right (counter-clockwise)
            w = float(np.hypot(*(a1 - a0)))
            nxt, prv = runs[(i + 1) % nr], runs[(i - 1) % nr]
            gap_prev = float(np.hypot(*(ring[prv[-1]] - a0))) if nr > 1 else np.inf
            gap_next = float(np.hypot(*(ring[nxt[0]] - a1))) if nr > 1 else np.inf
            if gap_next <= ISLAND_MAX_M and gap_prev > ISLAND_MAX_M:
                continue                                    # its median on its right: the way out
            if gap_prev <= ISLAND_MAX_M:
                lin, rin, median = a0, a1, True             # all of it in, the median on its left
            else:
                lin, rin, median = (a0 + a1) / 2, a1, False   # a two-way road: its right half in
            mid = (lin + rin) / 2
            h = unit(C - mid)                               # the way in
            right = np.array([h[1], -h[0]])
            # its signal: the OSM one nearest the way in's middle
            dsig = np.hypot(*(S - mid).T)
            if not len(S) or dsig.min() > 14.0:
                skipped.append(("no signal on a way in", mid))
                continue
            sig = S[np.argmin(dsig)]
            # the stop line: level with the signal along the way in
            along = float(np.dot(sig - mid, h))
            line_r = rin + h * along
            e, _, _ = edge(line_r - right * 1.0, right)
            if e is None:
                skipped.append(("no kerb", line_r))
                continue
            q = e + right * LIGHT_OFF_M + h * PAST_LINE_M
            if plan.kind(*q[:, None])[0] == ROAD:
                skipped.append(("kerb spot on the road", q))
                continue
            near = plan.kind(*(q + 2.5 * np.c_[np.cos(np.linspace(0, 6.28, 12)),
                                               np.sin(np.linspace(0, 6.28, 12))]).T)
            add({"module": "traffic_light", "role": "primary", "at": q, "facing": -h,
                 "bike": bool((near == CYCLE).any())})
            if median:
                el, _, wl = edge(lin + h * along + right * 1.0, -right)
                if el is not None and wl <= ISLAND_MAX_M:
                    q2 = el - right * min(LIGHT_OFF_M, wl / 2) + h * PAST_LINE_M
                    if plan.kind(*q2[:, None])[0] != ROAD:
                        add({"module": "traffic_light", "role": "repeater", "at": q2, "facing": -h, "bike": False})
    # OSM's ways through its signals (ref/osm_map.xml, the API's map call): each signal's way says which way its traffic
    # comes (traffic_signals:direction, else the way's oneway, else both) and how many lanes come in. A way in with no
    # primary yet facing its traffic gets one at its right kerb; a primary over three lanes or more becomes a mast with
    # an arm over them; a cycle path's signal gets its cyclists' light at the path's right
    osm_xml = os.path.join(a.ref, "osm_map.xml")
    n_osm = n_mast = 0
    if os.path.exists(osm_xml):
        import xml.etree.ElementTree as ET
        root = ET.parse(osm_xml).getroot()
        nodes = {n_.get("id"): n_ for n_ in root.findall("node")}
        ways = []
        for w_ in root.findall("way"):
            tags = {t.get("k"): t.get("v") for t in w_.findall("tag")}
            if tags.get("highway") in OSM_CARS or tags.get("highway") == "cycleway":
                ways.append(([nd.get("ref") for nd in w_.findall("nd")], tags))
        xy = {}

        def at(nid):
            if nid not in xy:
                xy[nid] = local({"lat": float(nodes[nid].get("lat")), "lon": float(nodes[nid].get("lon"))})
            return xy[nid]
        for nid, nd_ in nodes.items():
            ntags = {t.get("k"): t.get("v") for t in nd_.findall("tag")}
            if ntags.get("highway") != "traffic_signals" and ntags.get("crossing") != "traffic_signals":
                continue
            p = at(nid)
            if not inside_box(p):
                continue
            sd = ntags.get("traffic_signals:direction")
            for refs, wt in ways:
                if nid not in refs:
                    continue
                i = refs.index(nid)
                ow = wt.get("oneway")
                ins = []
                if i > 0 and (sd == "forward" or (sd is None and ow != "-1")):
                    ins.append(p - at(refs[i - 1]))
                if i < len(refs) - 1 and (sd == "backward" or (sd is None and ow not in ("yes", "1", "true"))):
                    ins.append(p - at(refs[i + 1]))
                try:
                    lanes = int(wt.get("lanes:forward") or (wt.get("lanes") if ow in ("yes", "1", "true")
                                                            else int(wt.get("lanes", "2")) // 2))
                except (TypeError, ValueError):
                    lanes = 1
                cyc = wt.get("highway") == "cycleway"
                for d_ in ins:
                    if np.hypot(*d_) < 0.5:
                        continue
                    h = unit(d_)                                # the way in
                    right = np.array([h[1], -h[0]])
                    mine = [o for o in items if o["module"] == "traffic_light" and o.get("role") == "primary"
                            and np.hypot(*(np.asarray(o["at"]) - p)) < OSM_MATCH_M
                            and np.dot(np.asarray(o["facing"]), -h) > 0.7]
                    if cyc:
                        if any(o.get("bike") and np.hypot(*(np.asarray(o["at"]) - p)) < 6.0 for o in items):
                            continue
                        s = np.arange(0.2, 4.0, 0.2)
                        off = np.flatnonzero(plan.kind(*(p + np.outer(s, right)).T) != CYCLE)
                        q = p + right * ((s[off[0]] if len(off) else 1.4) + 0.5) + h * PAST_LINE_M
                        if plan.kind(*q[:, None])[0] != ROAD:
                            add({"module": "traffic_light", "role": "primary", "at": q, "facing": -h, "bike": True})
                            n_osm += 1
                        continue
                    side = None
                    if lanes >= MAST_LANES:
                        # the mast on the side with the wider verge (the tram's grass, a median), its arm over the
                        # lanes to the other: A269 has it on the drivers' left, Street View 2026-10-21
                        er, kr_, wr = edge(p, right)
                        el, kl_, wl = edge(p, -right)
                        wr = 0.0 if er is None else (wr if np.isfinite(wr) else 8.0)
                        wl = 0.0 if el is None else (wl if np.isfinite(wl) else 8.0)
                        side = "left" if (el is not None and wl >= MEDIAN_MIN_M) else "right"
                    if mine:
                        if side:
                            for o in mine:
                                o["mast"] = lanes
                                o["arm"] = "right" if side == "left" else "left"
                                if side == "left":
                                    o["at"] = [round(float(v), 3) for v in el - right * LIGHT_OFF_M + h * PAST_LINE_M]
                        continue
                    e, _, _ = edge(p, right)
                    if e is None or np.hypot(*(e - p)) > 12.0:
                        continue
                    q = e + right * LIGHT_OFF_M + h * PAST_LINE_M
                    if plan.kind(*q[:, None])[0] == ROAD:
                        continue
                    near = plan.kind(*(q + 2.5 * np.c_[np.cos(np.linspace(0, 6.28, 12)),
                                                       np.sin(np.linspace(0, 6.28, 12))]).T)
                    item = {"module": "traffic_light", "role": "primary", "at": q, "facing": -h,
                            "bike": bool((near == CYCLE).any())}
                    if side:
                        item["mast"] = lanes
                        item["arm"] = "right" if side == "left" else "left"
                        if side == "left":
                            item["at"] = el - right * LIGHT_OFF_M + h * PAST_LINE_M
                    n0 = len(items)
                    add(item)
                    n_osm += len(items) - n0
        n_mast = sum(1 for o in items if o.get("mast"))
    print(f"OSM's ways: {n_osm} lights added, {n_mast} masts")
    n_l2 = 0
    for e_ in osm:
        if e_["tags"].get("crossing") != "zebra":
            continue
        p = local(e_)
        if not inside_box(p):
            continue
        ax = road_axis(p, zebra=True)
        if ax is None:
            ax = road_axis(p)
        if ax is None:
            continue
        for h in (ax, -ax):                                 # each way's traffic, its sign on its right
            right = np.array([h[1], -h[0]])
            e, _, _ = edge(p, right)
            q = None if e is None else e + right * LIGHT_OFF_M
            if q is not None and plan.kind(*q[:, None])[0] != ROAD and np.hypot(*(q - p)) < 15.0:
                add({"module": "sign", "code": "L2", "role": "zebra", "at": q, "facing": -h})
                n_l2 += 1
    # D2: each island near a junction, its nose toward the traffic
    from scipy import ndimage as ndi
    isl = np.argmax(plan.share, axis=0) == ISLAND
    lbl, nis = ndi.label(isl)
    n_d2 = 0
    cs = []
    for c in centre.values():                       # (the junctions, each once)
        if all(np.hypot(*(c - d)) > 15 for d in cs):
            cs.append(c)
    for i in range(1, nis + 1):
        rr, cc = np.nonzero(lbl == i)
        if len(rr) * ks.RES ** 2 < 4.0 or not cs:
            continue
        P = np.c_[plan.gx[cc], plan.gy[rr]]
        cen = min(cs, key=lambda c: np.hypot(*(c - P.mean(0))))
        if np.hypot(*(cen - P.mean(0))) > 60:
            continue
        nose = P[np.argmax(np.hypot(*(P - cen).T))]
        inward = unit(P.mean(0) - nose)
        q = nose + inward * 0.6
        add({"module": "sign", "code": "D2", "role": "island", "at": q, "facing": unit(nose - cen)})
        n_d2 += 1
    out = os.path.join(a.ref, "placement.json")
    json.dump({"items": items, "junctions": [[round(float(v), 2) for v in c] for c in cs]}, open(out, "w"), indent=1)
    kinds = {}
    for it in items:
        key = it["module"] + ":" + it.get("role", "")
        kinds[key] = kinds.get(key, 0) + 1
    print(f"{out}: {kinds}; signals skipped: {len(skipped)} {[s[0] for s in skipped]}")


if __name__ == "__main__":
    main()
