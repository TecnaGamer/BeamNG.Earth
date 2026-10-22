#!/usr/bin/env python3
"""A kunstwerk's road paint laid out as the road is, not traced off the photo (which gave fragments that did not follow
the lanes, the user, 2026-10-21): OSM's ways (ref/osm_map.xml) say where each carriageway runs, which way and with how
many lanes; the plan's kerbs (tools/kunstwerk_solid.py's Plan) say how wide it is at each step. From those, by the Dutch
rules (RVV / CROW):

- lane lines between the lanes of one direction: 0.10 m, dashed 3 m on 9 m; solid over the last LANE_SOLID_M before a
  stop line (the lane choice made);
- a centre line between the two directions of a two-way road: dashed 3 on 9;
- a stop line (0.30 m) across the lanes in at each OSM signal on the way;
- turn arrows from turn:lanes, LANE_ARROWS_M before the stop line, in each lane's middle;
- a zebra at each OSM zebra (and a signalled walkers' crossing): 0.50 m bars, 0.50 m gaps, ZEBRA_L long, kerb to kerb;
- a cycle crossing where a cycle path crosses a carriageway: red, and its blocks (0.50 m, 0.50 m apart) along both
  edges;
- nothing in a junction's middle (where no kerb is within JUNCTION_WIDE_M either side) and nothing in the trench (the
  trench's own lines are tools/kunstwerk_solid.py's).

    python3 tools/kunstwerk_paint.py ../design/kunstwerken/droogleever/ref

Writes ref/markings.json ({"marks": [{"kind": "white" | "red", "rings": [outer]}]}, as tools/kunstwerk_solid.py reads
it); the photo's (tools/kunstwerk_markings.py) is kept as ref/markings_photo.json the first time.
"""
import argparse
import json
import os
import shutil
import sys
import xml.etree.ElementTree as ET

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server"))

EDGE_IN_M = 0.3         # m an edge line lies inside its kerb
SMOOTH_N = 25           # samples (of STEP) a kerb's distance is smoothed over
STEP = 0.5              # m between a way's samples
LINE_W, STOP_W = 0.10, 0.30
DASH = (3.0, 9.0)       # m: a lane line's dash and gap
LANE_SOLID_M = 12.0     # m before a stop line the lane lines are solid
LANE_ARROWS_M = (8.0, 30.0)   # m before the stop line the arrows stand
JUNCTION_WIDE_M = 13.0  # m: no kerb within this either side: a junction's middle, unpainted
ZEBRA_L = 4.0           # m: a zebra's bars' length (along the road)
TEETH_BEFORE_M = 1.5    # m before a tram track a row of shark's teeth stands
TEETH_NO_SIGNAL_M = 0.0   # (the teeth stand where OSM has a tram crossing, signalled or not: Street View C250, C150)
TEETH_AT_CROSSING_M = 30.0  # m from an OSM tram stop a car way's crossing of the tracks gets its teeth
_OLD_NO_SIGNAL = 15.0  # m: no signal this near (a signalled tram crossing has its stop line instead; round 6)
_OLD_TEETH = 0.0   # m: (none: the teeth stand before a tram crossing with a signal too, Street View C250, C150)
CYCLE_W = 2.2           # m: a cycle crossing's width
EDGE_HIGHWAYS = {"motorway", "trunk", "motorway_link", "trunk_link"}   # (edge lines: urban kerbs have none)
MINOR = {"unclassified", "residential", "service"}
CARS = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
        "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}


def unit(v):
    v = np.asarray(v, np.float64)
    return v / max(np.hypot(*v), 1e-9)


def resample(line, step):
    s = np.r_[0, np.cumsum(np.hypot(*np.diff(line, axis=0).T))]
    if s[-1] < step:
        return line, s
    t = np.arange(0, s[-1] + 1e-9, step)
    return np.c_[np.interp(t, s, line[:, 0]), np.interp(t, s, line[:, 1])], t


def smooth(line, k=9):
    if len(line) < k:
        return line
    pad = np.pad(line, ((k // 2, k // 2), (0, 0)), mode="edge")
    out = np.c_[np.convolve(pad[:, 0], np.ones(k) / k, "valid"), np.convolve(pad[:, 1], np.ones(k) / k, "valid")]
    out[0], out[-1] = line[0], line[-1]
    return out


def strip(a, b, w):
    """A strip w wide along the polyline a..b (k, 2) as one ring."""
    pts = np.r_[a, b] if b is not None else a
    d = np.gradient(pts, axis=0)
    n = np.c_[-d[:, 1], d[:, 0]] / np.maximum(np.hypot(*d.T), 1e-9)[:, None]
    return np.r_[pts + n * w / 2, (pts - n * w / 2)[::-1]]


def rect(c, along, L, W):
    a = unit(along)
    n = np.array([-a[1], a[0]])
    return np.array([c - a * L / 2 - n * W / 2, c + a * L / 2 - n * W / 2, c + a * L / 2 + n * W / 2,
                     c - a * L / 2 + n * W / 2])


ARROWS = {   # Dutch arrows (CROW), in a 5 m x 1.2 m box: x across (+: right), y along (+: ahead); the shaft and heads
    "through": [(-0.1, 0), (0.1, 0), (0.1, 3.4), (0.4, 3.4), (0, 5.0), (-0.4, 3.4), (-0.1, 3.4)],
    "left": [(-0.1, 0), (0.1, 0), (0.1, 3.2), (-0.3, 3.9), (-0.25, 3.45), (-0.75, 4.1), (-0.45, 3.2), (-0.1, 3.0)],
    "right": [(0.1, 0), (-0.1, 0), (-0.1, 3.2), (0.3, 3.9), (0.25, 3.45), (0.75, 4.1), (0.45, 3.2), (0.1, 3.0)],
}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ref")
    a = ap.parse_args(argv)
    import ahn as ahnmod
    import kunstwerk_solid as ks
    from kunstwerk_design import carriageways
    ref = json.load(open(os.path.join(a.ref, "ref.json"), encoding="utf-8"))
    ks.REF_DIR[:] = [a.ref]
    frame, lines = carriageways(a.ref)
    c0, u, n = frame
    corners = np.array([c0 + u * t + n * w for t in ks.AREA_T for w in (-ks.AREA_W, ks.AREA_W)])
    box = (*np.floor(corners.min(0)), *np.ceil(corners.max(0)))
    plan = ks.Plan(ref, frame, box)
    names = [k[0] for k in ks.KINDS]
    ROAD = names.index("road")

    def in_area(p):
        d = np.atleast_2d(p) - c0
        t, w = d @ u, d @ n
        return (t >= ks.AREA_T[0]) & (t <= ks.AREA_T[1]) & (np.abs(w) <= ks.AREA_W)
    # the trench's strips (its own lines are the solid tool's)
    trench = []
    for nm, (f, ta, tb, wd) in lines.items():
        trench.append((f, ta, tb, max(wd, 2 * ks.LANE) + 2 * ks.SHOULDER))

    def in_trench(p):
        d = np.atleast_2d(p) - c0
        t, w = d @ u, d @ n
        out = np.zeros(len(t), bool)
        for f, ta, tb, W in trench:
            out |= (t >= ta) & (t <= tb) & (np.abs(w - f(t)) <= W / 2 + 0.5)
        return out

    from matplotlib.path import Path as MPath
    deck_rings = [MPath(np.asarray(d["ring"])) for d in json.load(open(os.path.join(os.path.dirname(
        os.path.abspath(a.ref)), "decks.json")))["decks"]] if os.path.exists(os.path.join(os.path.dirname(
            os.path.abspath(a.ref)), "decks.json")) else []

    def on_road(p):
        p = np.atleast_2d(p)
        deck = np.zeros(len(p), bool)
        for r_ in deck_rings:                       # (a deck over the trench is its street's: painted)
            deck |= r_.contains_points(p)
        return (plan.kind(*p.T) == ROAD) & (~in_trench(p) | deck)

    root = ET.parse(os.path.join(a.ref, "osm_map.xml")).getroot()
    nodes = {nd.get("id"): nd for nd in root.findall("node")}
    ntags = {k: {t.get("k"): t.get("v") for t in v.findall("tag")} for k, v in nodes.items()}
    o = ref["origin_rd"]
    xy = {}

    def at(nid):
        if nid not in xy:
            rx, ry = ahnmod.wgs_to_rd(float(nodes[nid].get("lat")), float(nodes[nid].get("lon")))
            xy[nid] = np.array([rx - o[0], ry - o[1]])
        return xy[nid]
    ways = []
    for w_ in root.findall("way"):
        tags = {t.get("k"): t.get("v") for t in w_.findall("tag")}
        refs = [nd.get("ref") for nd in w_.findall("nd")]
        # (at grade only: a way in a tunnel, on a bridge or on another layer is not painted on these streets, nor does
        # it cross them: the cycle path under the ring drew a crossing over its road, Street View B221)
        if tags.get("tunnel") not in (None, "no") or tags.get("bridge") not in (None, "no")                 or tags.get("covered") not in (None, "no") or tags.get("layer", "0") != "0":
            continue
        if refs and all(r in nodes for r in refs):
            ways.append((refs, tags))
    marks = []

    def white(ring, on=None):
        """A white mark, kept only where its surface is (the carriageway, or `on`'s kinds): no paint on the grass."""
        r_ = np.asarray(ring)
        smp = r_[np.linspace(0, len(r_) - 1, min(len(r_), 9)).astype(int)] * 0.8 + r_.mean(0) * 0.2
        kinds = plan.kind(*smp.T)
        ok_ = on_road(smp) if on is None else np.isin(kinds, on)
        if ok_.mean() < 0.7:
            return
        if in_area(r_.mean(0))[0]:
            marks.append({"kind": "white", "rings": [np.round(ring, 3).tolist()]})

    red_at = []

    def red(ring):
        c_ = np.asarray(ring).mean(0)
        if any(np.hypot(*(c_ - q)) < 1.5 for q in red_at):       # (one crossing drawn once: several ways cross there)
            return
        red_at.append(c_)
        if in_area(c_)[0]:
            marks.append({"kind": "red", "rings": [np.round(ring, 3).tolist()]})
    count = {"lane": 0, "centre": 0, "stop": 0, "arrow": 0, "zebra": 0, "cycle": 0}
    signal = {k for k, t in ntags.items() if t.get("highway") == "traffic_signals"}

    def kerbs(p, nrm, reach=JUNCTION_WIDE_M):
        """From p (on or near the carriageway) the distances to its kerbs on the left (+nrm) and the right (none within
        reach: a junction's middle)."""
        s = np.arange(0.0, reach, 0.2)
        if not on_road(p)[0]:                          # (OSM's line a little off the carriageway: onto it first)
            for t in np.arange(0.2, 5.0, 0.2):
                for sg in (1, -1):
                    if on_road(p + nrm * t * sg)[0]:
                        p = p + nrm * t * sg
                        break
                else:
                    continue
                break
            else:
                return None
        out = []
        for sg in (1, -1):
            ok = on_road(p + np.outer(s, nrm * sg))
            if ok.all():
                return None                            # (a junction's middle)
            out.append(s[np.argmin(ok)])
        return p, out[0], out[1]
    for refs, tags in ways:
        if tags.get("highway") not in CARS or tags.get("lane_markings") == "no":
            continue
        P = np.array([at(r) for r in refs])
        line, s = resample(P, STEP)
        line = smooth(line)
        if len(line) < 4:
            continue
        ow = tags.get("oneway") in ("yes", "1", "true")
        try:
            lanes = int(tags.get("lanes", "2" if not ow else "1"))
        except ValueError:
            lanes = 2 if not ow else 1
        lanes_fw = int(tags.get("lanes:forward", lanes if ow else max(1, lanes // 2)) or 1)
        lanes_bw = 0 if ow else int(tags.get("lanes:backward", max(1, lanes - lanes_fw)) or 1)
        d = np.gradient(line, axis=0)
        d /= np.maximum(np.hypot(*d.T), 1e-9)[:, None]
        nl = np.c_[-d[:, 1], d[:, 0]]                  # to the left of the way's direction
        # distance to the next stop line ahead (forward) and behind (backward), from the OSM signals on the way
        sig_s = [s[np.argmin(np.hypot(*(line - at(r)).T))] for r in refs if r in signal]
        ahead = np.full(len(line), np.inf)
        behind = np.full(len(line), np.inf)
        for ss in sig_s:
            ahead = np.where(s <= ss, np.minimum(ahead, ss - s), ahead)
            behind = np.where(s >= ss, np.minimum(behind, s - ss), behind)
        # per sample the carriageway's edges: its lane boundaries
        bounds = {}
        reach = max(JUNCTION_WIDE_M, (lanes_fw + lanes_bw) * 3.6 + 3.0)
        for i in range(len(line)):
            k = kerbs(line[i], nl[i], reach)
            if k is None:
                continue
            p, dl, dr = k
            if not in_area(p)[0]:
                continue
            bounds[i] = (p, dl, dr)
        if len(bounds) < 6:
            continue
        idx = np.array(sorted(bounds))
        raw_w = {i: bounds[i][1] + bounds[i][2] for i in idx}     # (the measured width, before smoothing)
        # the kerbs' distances smoothed along the way (a lane line follows its lane, not every jag of the plan's
        # edge: the lines kinked where the measure jumped, round 4 B20, C250); runs broken where a kerb is missing
        dl_ = np.array([bounds[i][1] for i in idx])
        dr_ = np.array([bounds[i][2] for i in idx])
        brk = np.r_[0, np.flatnonzero(np.diff(idx) > 2) + 1, len(idx)]
        for b0, b1 in zip(brk[:-1], brk[1:]):
            k_ = int(min(SMOOTH_N, b1 - b0))
            if k_ >= 3:
                for arr in (dl_, dr_):
                    seg = arr[b0:b1]
                    med = np.array([np.median(seg[max(0, j - k_ // 2):j + k_ // 2 + 1]) for j in range(len(seg))])
                    arr[b0:b1] = np.convolve(np.pad(med, k_ // 2, mode="edge"), np.ones(k_) / k_, "valid")[:len(seg)]
        for j, i in enumerate(idx):
            p0 = bounds[i][0]
            # (the sample's middle: back onto the way's own line, its offsets then from there)
            off = float(np.dot(p0 - line[i], nl[i]))
            bounds[i] = (line[i], dl_[j] + off, dr_[j] - off)
        if "lanes" not in tags:          # (untagged: as many lanes as its width holds, at the narrowest third)
            wd = np.sort([bounds[i][1] + bounds[i][2] for i in idx])
            if tags.get("highway") in MINOR and np.median(wd) < 6.0:
                continue                 # (a narrow minor road: no paint, Baden Powelllaan)
            n_w = max(1, int(round(wd[len(wd) // 3] / 3.3)))
            if ow:
                lanes = lanes_fw = n_w
            else:
                lanes_fw, lanes_bw = max(1, n_w // 2), max(1, n_w - n_w // 2)
        else:                            # (tagged too few for its width: the width's, Street View B221 has 3 where OSM 1)
            wd = np.sort([bounds[i][1] + bounds[i][2] for i in idx])
            n_w = int(round(wd[len(wd) // 2] / 3.3))
            if n_w >= lanes_fw + lanes_bw + 1 and len(wd) * STEP > 20.0:
                if ow:
                    lanes = lanes_fw = n_w
                else:
                    lanes_fw, lanes_bw = max(1, n_w // 2), max(1, n_w - n_w // 2)
        # the lines: offsets from the right kerb (as the way runs)
        n_all = lanes_fw + lanes_bw
        lines_off = []                                  # (fraction of the width from the right kerb, kind, solid_near)
        for kk in range(1, n_all):
            if not ow and kk == lanes_fw:
                lines_off.append((kk / n_all, "centre", None))
            elif kk < lanes_fw:
                lines_off.append((kk / n_all, "lane", ahead))
            else:
                lines_off.append((kk / n_all, "lane", behind))
        for frac, kind, near in lines_off:
            pts, on = [], []
            for i in idx:
                p, dl, dr = bounds[i]
                width = dl + dr
                if width < n_all * 2.4 or raw_w[i] > n_all * 3.6 + 3.0:   # (a merge; or another carriageway or turning
                    pts.append(None)                                       # lane counted in: the line would leave its lane)
                    continue
                # lanes of a standard width from the right kerb (where the road widens at a junction's mouth the
                # extra width is the turning lanes' and the kerb's: the lines keep to the lanes, Street View A330)
                lw = float(np.clip(width / n_all, 2.8, 3.6))
                if width > n_all * 3.6 and kind == "lane" and near is behind:
                    q = p + nl[i] * dl - nl[i] * lw * (n_all * (1 - frac))      # (the backward lanes from the left)
                else:
                    q = p - nl[i] * dr + nl[i] * lw * n_all * frac
                pts.append(q if on_road(q)[0] else None)
            # dashes by the way's own distance; solid near a stop line
            run = []
            for j, i in enumerate(idx):
                q = pts[j]
                solid = near is not None and near[i] < LANE_SOLID_M
                dash_on = (s[i] % sum(DASH)) < DASH[0]
                if q is not None and (solid or dash_on or kind == "centre" and dash_on):
                    run.append(q)
                    continue
                if len(run) >= 3:                       # (pieces under a metre: dropped)
                    white(strip(np.array(run), None, LINE_W))
                    count[kind] += 1
                run = []
            if len(run) >= 3:
                white(strip(np.array(run), None, LINE_W))
                count[kind] += 1
        # the edge lines: solid, EDGE_IN_M inside the kerb on the right (and on the left of a one-way road), where the
        # kerb is the way's own (not across a junction's mouth: there the kerb is far and the line stops)
        for side in (((1, -1) if ow else (-1,)) if tags.get("highway") in EDGE_HIGHWAYS else ()):
            run = []
            for i in idx:
                p, dl, dr = bounds[i]
                dist = dr if side < 0 else dl
                lanes_here = lanes_fw if (side < 0 or ow) else lanes_bw
                if dist > (lanes_here if ow else n_all / 2) * 3.6 + 1.0 + (0 if ow else 0) or dist < 1.0:
                    if len(run) >= 3:
                        white(strip(np.array(run), None, LINE_W))
                        count["edge"] = count.get("edge", 0) + 1
                    run = []
                    continue
                run.append(p + nl[i] * side * (dist - EDGE_IN_M))
            if len(run) >= 3:
                white(strip(np.array(run), None, LINE_W))
                count["edge"] = count.get("edge", 0) + 1
        # stop lines and arrows at the signals on the way (for the direction coming to each)
        for r in refs:
            if r not in signal:
                continue
            j = int(np.argmin(np.hypot(*(line - at(r)).T)))
            dirs = []
            sd = ntags[r].get("traffic_signals:direction")
            if sd != "backward" and (ow or sd == "forward" or sd is None):
                dirs.append(1)
            if not ow and sd != "forward":
                dirs.append(-1)
            # (the stop line before a zebra or cycle crossing at the signal: OSM puts the signal on it, round 6)
            xs_ = [s[np.argmin(np.hypot(*(line - at(z)).T))] for z in refs
                   if ntags[z].get("highway") == "crossing" or ntags[z].get("crossing")]
            near_x = [x for x in xs_ if abs(x - s[j]) < 8.0]
            for sg in dirs:
                jj = j
                if near_x:
                    s_stop = (min(near_x) - ZEBRA_L / 2 - 1.0) if sg > 0 else (max(near_x) + ZEBRA_L / 2 + 1.0)
                    jj = int(np.argmin(np.abs(s - s_stop)))
                k = None
                for back_m in np.arange(0, 10.5, 0.5):     # (the way's reach; stepped back out of a junction's middle)
                    jb = int(np.clip(jj - sg * int(back_m / STEP), 0, len(line) - 1))
                    k = kerbs(line[jb], nl[jb], reach)
                    if k is not None:
                        jj = jb
                        break
                if k is None:
                    continue
                p, dl, dr = k
                j_ = jj
                fwd = d[j_] * sg
                left = nl[j_] * sg
                (dr_, dl_) = (dr, dl) if sg > 0 else (dl, dr)
                n_in = lanes_fw if sg > 0 else lanes_bw
                w_in = (dl_ + dr_) * (n_in / max(n_all, 1)) if not ow else dl_ + dr_
                w_in = min(w_in, n_in * 3.6)          # (never past its own lanes: over a median or the other way)
                right_edge = p - left * dr_
                a_ = right_edge + left * 0.2
                b_ = right_edge + left * max(w_in - 0.2, 0.5)
                ring = rect((a_ + b_) / 2, b_ - a_, np.hypot(*(b_ - a_)), STOP_W)
                white(ring)
                count["stop"] += 1
                turns = (tags.get("turn:lanes:forward" if sg > 0 and not ow else "turn:lanes:backward" if sg < 0
                                  else "turn:lanes") or "").split("|")
                lw = w_in / max(n_in, 1)
                for li in range(n_in):
                    # (turn:lanes reads from the left; lane li counted from the left)
                    t_ = turns[li] if li < len(turns) else ""
                    kind = "left" if "left" in t_ else "right" if "right" in t_ else "through"
                    cen = right_edge + left * (w_in - lw * (li + 0.5))
                    for back in LANE_ARROWS_M:
                        base = cen - fwd * (back + 5.0)
                        if not on_road(base)[0]:
                            continue
                        rr = np.array([base + left * (-x) + fwd * y for x, y in ARROWS[kind]])
                        white(rr)
                        count["arrow"] += 1
    # zebras: at OSM's zebras and signalled walkers' crossings on a carriageway: bars along the road, kerb to kerb
    for nid, t in ntags.items():
        if not (t.get("crossing") in ("zebra", "marked") or t.get("crossing:markings") in ("zebra", "yes")
                or (t.get("highway") == "crossing"
                                                             and t.get("crossing") == "traffic_signals")):
            continue
        p = at(nid)
        if not in_area(p)[0] or not on_road(p)[0]:
            continue
        # the road's direction: the car way through the node
        dirv = None
        for refs, tags in ways:
            if nid in refs and tags.get("highway") in CARS:
                i = refs.index(nid)
                a0, a1 = at(refs[max(i - 1, 0)]), at(refs[min(i + 1, len(refs) - 1)])
                dirv = unit(a1 - a0)
                break
        if dirv is None:
            continue
        across = np.array([-dirv[1], dirv[0]])
        k = kerbs(p, across)
        if k is None:
            continue
        p, dl, dr = k
        t_ = -dr + 0.25
        while t_ < dl - 0.25:
            white(rect(p + across * t_, dirv, ZEBRA_L, 0.5))
            count["zebra"] += 1
            t_ += 1.0
    # cycle crossings: where a cycle path crosses a car way, red over the carriageway and the blocks along both edges
    cyc = [(np.array([at(r) for r in refs]), tags) for refs, tags in ways if tags.get("highway") == "cycleway"]
    car = [(np.array([at(r) for r in refs]), tags) for refs, tags in ways if tags.get("highway") in CARS]
    for C, _ in cyc:
        for i in range(len(C) - 1):
            for R, _ in car:
                for j in range(len(R) - 1):
                    p1, p2, q1, q2 = C[i], C[i + 1], R[j], R[j + 1]
                    dpp, dqq = p2 - p1, q2 - q1
                    den = dpp[0] * dqq[1] - dpp[1] * dqq[0]
                    if abs(den) < 1e-9:
                        continue
                    tt = ((q1[0] - p1[0]) * dqq[1] - (q1[1] - p1[1]) * dqq[0]) / den
                    uu = ((q1[0] - p1[0]) * dpp[1] - (q1[1] - p1[1]) * dpp[0]) / den
                    if not (0 <= tt <= 1 and 0 <= uu <= 1):
                        continue
                    x = p1 + dpp * tt
                    if not in_area(x)[0] or not on_road(x)[0]:
                        continue
                    along = unit(dpp)                       # across the carriageway
                    k = kerbs(x, along)
                    if k is None:
                        continue
                    x, dl, dr = k
                    road_dir = np.array([-along[1], along[0]])
                    a_, b_ = x - along * dr, x + along * dl
                    n_red = len(red_at)
                    red(rect((a_ + b_) / 2, b_ - a_, dl + dr, CYCLE_W))
                    if len(red_at) == n_red:
                        continue                                  # (drawn already, its blocks with it)
                    for side in (-1, 1):
                        t_ = -dr + 0.25
                        while t_ < dl - 0.25:
                            white(rect(x + along * t_ + road_dir * side * (CYCLE_W / 2 - 0.25), along, 0.5, 0.5))
                            t_ += 1.0
                    count["cycle"] += 1
    # shark's teeth where a carriageway crosses the tram's tracks with no signal near: a row across the lanes coming to
    # the tracks, TEETH_BEFORE_M before them, their points toward those drivers (Street View C250, C150)
    tracks = [np.asarray(f["rings"][0], np.float64) for f in ref["features"] if f["layer"] == "spoor" and f["level"] == 0]
    sig_pts = np.array([at(k) for k in signal]) if signal else np.zeros((0, 2))
    # (OSM tags only the walkers' tram crossings; the cars' ones with teeth are by the tram stops, Street View C250,
    # C150: elsewhere the tracks the roads cross are old stubs with no teeth, round 6)
    tram_x = np.array([at(k) for k, t in ntags.items() if t.get("railway") == "tram_stop"]).reshape(-1, 2)
    n_teeth = 0
    for refs, tags in ways:
        if tags.get("highway") not in CARS:
            continue
        R = np.array([at(r) for r in refs])
        ow = tags.get("oneway") in ("yes", "1", "true")
        for T_ in tracks:
            for i in range(len(R) - 1):
                for j in range(len(T_) - 1):
                    p1, p2, q1, q2 = R[i], R[i + 1], T_[j], T_[j + 1]
                    dpp, dqq = p2 - p1, q2 - q1
                    den = dpp[0] * dqq[1] - dpp[1] * dqq[0]
                    if abs(den) < 1e-9:
                        continue
                    tt = ((q1[0] - p1[0]) * dqq[1] - (q1[1] - p1[1]) * dqq[0]) / den
                    uu = ((q1[0] - p1[0]) * dpp[1] - (q1[1] - p1[1]) * dpp[0]) / den
                    if not (0 <= tt <= 1 and 0 <= uu <= 1):
                        continue
                    x = p1 + dpp * tt
                    if not in_area(x)[0] or (len(sig_pts) and np.hypot(*(sig_pts - x).T).min() < TEETH_NO_SIGNAL_M):
                        continue
                    if not len(tram_x) or np.hypot(*(tram_x - x).T).min() > TEETH_AT_CROSSING_M:
                        continue                # (only where OSM has a tram crossing: the old tracks' stubs have none)
                    fwd = unit(dpp)
                    track_dir = unit(dqq)
                    for sg in ((1,) if ow else (1, -1)):
                        f_ = fwd * sg
                        # the row: square to the road, TEETH_BEFORE_M before the track (its rails 0.72 m either side)
                        cos_ = abs(np.dot(f_, np.array([-track_dir[1], track_dir[0]])))
                        back = (0.72 + TEETH_BEFORE_M) / max(cos_, 0.3)
                        c_ = x - f_ * back
                        left = np.array([-f_[1], f_[0]])
                        k = kerbs(c_, left)
                        if k is None:
                            continue
                        c_, dl, dr = k
                        lo, hi = -dr + 0.2, (dl - 0.2 if ow else -0.2)
                        t_ = lo
                        while t_ + 0.5 <= hi:
                            base = c_ + left * (t_ + 0.25)
                            tri = np.array([base - left * 0.25, base + left * 0.25, base - f_ * 0.6])
                            white(tri[::-1] if np.cross(tri[1] - tri[0], tri[2] - tri[0]) < 0 else tri)
                            t_ += 0.75
                            n_teeth += 1
    count["teeth"] = n_teeth
    # (no lane line through a row of teeth: the lines stop before it, round 7)
    teeth_c = np.array([np.asarray(m_["rings"][0]).mean(0) for m_ in marks if len(m_["rings"][0]) == 3]).reshape(-1, 2)
    if len(teeth_c):
        keep_m = []
        for m_ in marks:
            r_ = np.asarray(m_["rings"][0])
            if len(r_) > 6 and np.hypot(*(teeth_c[:, None, :] - r_[None, :, :]).transpose(2, 0, 1)).min() < 1.0:
                continue
            keep_m.append(m_)
        marks[:] = keep_m
    # arrows where a one-way approach with turn:lanes reaches a junction with no signal (the signalled ones have theirs)
    use = {}
    for refs, tags in ways:
        if tags.get("highway") in CARS:
            for r in refs:
                use[r] = use.get(r, 0) + 1
    n_arr = 0
    for refs, tags in ways:
        if tags.get("highway") not in CARS or tags.get("oneway") not in ("yes", "1", "true") or "turn:lanes" not in tags:
            continue
        end = refs[-1]
        if use.get(end, 0) < 3 or any(r in signal for r in refs):     # (a real junction, and not a signalled one)
            continue
        line, s_ = resample(np.array([at(r) for r in refs]), STEP)
        if len(line) < 10:
            continue
        turns = tags["turn:lanes"].split("|")
        nlanes = len(turns)
        for back in LANE_ARROWS_M:
            j = int(np.searchsorted(s_, s_[-1] - back - 5.0))
            if j <= 0 or j >= len(line) - 1:
                continue
            fwd = unit(line[j + 1] - line[j - 1])
            left = np.array([-fwd[1], fwd[0]])
            k = kerbs(line[j], left)
            if k is None:
                continue
            p, dl, dr = k
            w_in = dl + dr
            if w_in > nlanes * 4.6:
                continue
            lw = w_in / nlanes
            for li, t_ in enumerate(turns):
                kind = "left" if "left" in t_ else "right" if "right" in t_ else "through"
                cen = p - left * dr + left * (w_in - lw * (li + 0.5))
                rr = np.array([cen + left * (-x) + fwd * y for x, y in ARROWS[kind]])
                white(rr)
                n_arr += 1
    count["arrow_free"] = n_arr
    # two-way cycle paths: their dashed centre line (1 m on 1 m), on the path's own middle between its edges
    CYC = names.index("cycle")
    for refs, tags in ways:
        if tags.get("highway") != "cycleway" or tags.get("oneway") in ("yes", "1", "true"):
            continue
        line, s_ = resample(np.array([at(r) for r in refs]), STEP)
        line = smooth(line)
        if len(line) < 4:
            continue
        d_ = np.gradient(line, axis=0)
        nl_ = np.c_[-d_[:, 1], d_[:, 0]] / np.maximum(np.hypot(*d_.T), 1e-9)[:, None]
        pts = []
        for i in range(len(line)):
            p = line[i]
            if plan.kind(*p[:, None])[0] != CYC or not in_area(p)[0]:
                pts.append(None)
                continue
            st = np.arange(0.0, 3.0, 0.1)
            e1 = st[np.argmin(plan.kind(*(p + np.outer(st, nl_[i])).T) == CYC)]
            e2 = st[np.argmin(plan.kind(*(p - np.outer(st, nl_[i])).T) == CYC)]
            pts.append(p + nl_[i] * (e1 - e2) / 2)
        run = []
        for i, q in enumerate(pts):
            if q is not None and (s_[i] % 2.0) < 1.0:
                run.append(q)
                continue
            if len(run) >= 2:
                white(strip(np.array(run), None, LINE_W), on=[CYC])
                count["cycle_line"] = count.get("cycle_line", 0) + 1
            run = []
    dst = os.path.join(a.ref, "markings.json")
    keep = os.path.join(a.ref, "markings_photo.json")
    if os.path.exists(dst) and not os.path.exists(keep):
        shutil.copy(dst, keep)
    json.dump({"marks": marks}, open(dst, "w"))
    print(f"{dst}: {len(marks)} marks; {count}")


if __name__ == "__main__":
    main()
