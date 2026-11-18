"""probe.py --server DIR --cache DIR --lat LAT --lon LON [--r M] [--checks a,b] --json OUT [--timing OUT]

One corpus place, one build of the server code (DIR: any checkout's server/, so a git worktree at an old commit can be
scored with today's probes), built from scratch into CACHE (a scratch cache under ~/bnge-tests: never the
player's ~/.cache/beamng-earth, never a link into it) and scored with deterministic probes per problem category of the
road revamp plan (.claude/work/road-revamp-plan-2026-11-16.md section 5):

  deck_grade    each bridge deck's steepest 10 m grade vs its class limit (complaint 10, steep decks)
  end           each deck end vs the graded road it continues on: the step (m), the steepest 10 m grade over the first
                30 m off the deck, the sharpest 2 m lip, the width step deck vs road (complaint 2, abutment seams);
                a deck end that runs into a road with no end there (a roundabout ring, a junction: "joins") gets the
                step to the graded ground 1 m past it
  clearance     deck top over every ground road / drawn rail crossing under it, and deck over deck (complaints 1, 6)
  overlap       two decks whose footprints overlap in plan 0.3-4.5 m apart in height: torn / stacked-too-close ramps
                (complaint 5)
  unbridged     two ground lines crossing (no shared node) with graded heights > 3 m apart: a separation without a
                bridge; rail ones are a rail embankment across a road (complaint 3)
  rail_cross    drawn railways crossing drivable ground roads (at grade or not): complaint 13 (abandoned rails) and 14
                (rail at grade on fast roads)
  spikes        the graded ground along every drivable ground road (centre line and +-(w/2 - 0.5) m): bumps / pits over
                a 6 m chord > 0.25 m, away from junctions and deck ends (complaint 7, terrain through carriageways)
  humps         the same chord test on the through roads within 25 m of junctions (3+ road ends at one node; complaint 11)
  widths        width changes where two roads meet end to end: the jump within 10 m (spec widths and road_tapers) and
                the taper length against the speed rule of the plan's section 2.3 (complaint 16)
  deck_shape    decks of 40 m+ whose profile waves (20 m chord residual > 0.3 m) and decks of 60 m+ whose middle lies
                within 1.5 m of the graded ground (#1371: viaducts wavy and on the ground)
  rail_over     railways as the road tiles lay them (railways.tile_mesh's beds): a rail bridge's rail top over each
                road crossing under it, and a ground track's bed vs each road it crosses (a level crossing must be flush;
                > 0.5 m is an embankment or a pit across the carriageway: complaints 3 and 14)
  rail_bed      along each drawn railway: the graded ground over the ungraded ground (the bed's lift; #1384)
  surface       drivable streets (not service / track) drawn gravel or dirt, by length (#1367 paved streets as dirt)
  missing       mapped road bridge pieces of 10 m+ with no built deck within 4 m of their middle (#1098: the road
                over the water lost)
  tunnels       tools/tunnelscan.py's checks (blocked / portal_open / portal_deep / mouth_step / left_out / opened ...)
                for the tunnels round the place (complaints 4, 8, 9)

Everything is rounded and sorted: the same code + data gives the same JSON byte for byte (the harness checks it);
wall-clock and memory go to --timing only. Items count only inside R of the place."""
import argparse
import json
import logging
import math
import os
import resource
import sys
import time

ROAD_CLASSES = ("motorway", "trunk", "primary", "secondary", "tertiary", "residential", "unclassified",
                "living_street", "service", "motorway_link", "trunk_link", "primary_link", "secondary_link",
                "tertiary_link", "busway", "raceway")
FAST = ("motorway", "trunk", "motorway_link", "trunk_link", "primary")
# grade limits (plan section 2.6: g_max by class, + 1 % for a defect)
G_MAX = {"motorway": 4.0, "trunk": 5.0, "primary": 6.0, "secondary": 7.0, "tertiary": 8.0}
G_LINK = 7.0
G_LOCAL = 10.0
# clearance by the lower road (plan section 2.6), m
C_LOWER = {"motorway": 5.0, "trunk": 5.0, "service": 4.0, "track": 4.0, "rail": 5.0}
C_DEFAULT = 4.7
# design speed by class (plan section 2.3, rural / urban), km/h
V_RURAL = {"motorway": 120, "trunk": 100, "primary": 90, "secondary": 80, "tertiary": 70}
V_URBAN = {"motorway": 90, "trunk": 70, "primary": 50, "secondary": 50, "tertiary": 50}
STEP = 2.0
CHORD = 3                   # samples each side of the chord (6 m)
SPIKE_M = 0.25
JUNCTION_R = 25.0
EXCLUDE_R = 15.0            # m round junction nodes / deck ends / tunnel mouths kept out of the spike test


def r2(v):
    return round(float(v), 2)


def g_max(cls):
    if cls is None:
        return G_LOCAL
    if cls.endswith("_link"):
        return G_LINK
    return G_MAX.get(cls, G_LOCAL)


def design_speed(spec):
    if spec is None:
        return 50
    kmh = getattr(spec, "kmh", None)
    if kmh:
        return min(130, int(kmh))
    cls = spec.cls or ""
    base = cls[:-5] if cls.endswith("_link") else cls
    v = (V_URBAN if getattr(spec, "urban", False) else V_RURAL).get(base, 50 if not getattr(spec, "urban", False)
                                                                       else 30)
    return max(40, int(0.7 * v)) if cls.endswith("_link") else v


def taper_target(dw, v_kmh):
    """The plan's section 2.3 length for a lateral change dw at V: max(merging taper, lateral acceleration floor),
    clamped 20-400 m."""
    v = v_kmh / 3.6
    l_std = abs(dw) * v_kmh * min(v_kmh / 155.0, 0.62)
    l_acc = math.sqrt(5.77 * abs(dw) * v * v / 0.5)
    return min(400.0, max(20.0, l_std, l_acc))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--r", type=float, default=400.0)
    ap.add_argument("--checks", default="all")
    ap.add_argument("--json", required=True)
    ap.add_argument("--timing")
    ap.add_argument("--offline", action="store_true", help="no network: the cache's data only (a seeded cache)")
    ap.add_argument("--no-entrance", action="store_true", help="tunnels: skip the built entrances (mesh) checks")
    a = ap.parse_args()
    real = os.path.realpath(os.path.expanduser("~/.cache/beamng-earth"))
    cache = os.path.realpath(os.path.expanduser(a.cache))
    if cache == real or cache.startswith(real + os.sep):
        sys.exit("refused: the player's cache")
    os.makedirs(cache, exist_ok=True)
    for root, dirs, files in os.walk(cache):
        for n in dirs + files:
            p = os.path.join(root, n)
            if os.path.islink(p) and os.path.realpath(p).startswith(real):
                sys.exit(f"refused: {p} links into the player's cache")
    server = os.path.realpath(a.server)
    sys.path.insert(0, server)
    t0 = time.time()
    checks = set(a.checks.split(",")) if a.checks != "all" else None
    res = probe(server, cache, a.lat, a.lon, a.r, checks, not a.no_entrance, a.offline)
    res["place"] = [a.lat, a.lon, a.r]
    with open(a.json + ".tmp", "w") as f:
        json.dump(res, f, sort_keys=True, separators=(",", ":"), allow_nan=False)
        f.write("\n")
    os.replace(a.json + ".tmp", a.json)
    if a.timing:
        ru = resource.getrusage(resource.RUSAGE_SELF)
        with open(a.timing, "w") as f:
            json.dump({"wall_s": round(time.time() - t0, 1), "cpu_s": round(ru.ru_utime + ru.ru_stime, 1),
                       "max_rss_mb": round(ru.ru_maxrss / 1024.0)}, f, sort_keys=True)
            f.write("\n")


def probe(server, cache, lat, lon, R, checks, entrance, offline=False):
    import numpy as np
    import blockworker
    import geodesy
    import grading as gradingmod
    import roads
    try:
        import railways
    except ImportError:
        railways = None
    want = (lambda k: True) if checks is None else (lambda k: k in checks)
    blockworker.init(cache, bool(offline), True, True, True, False, None, logging.ERROR, False, True)
    s = blockworker._s
    g, _ = s.anchor(1, lat, lon)
    frame = geodesy.EnuFrame(lat, lon)
    errors = []

    def to_ll(xy):
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return np.asarray(la, np.float64), np.asarray(lo, np.float64)

    def ll(p):
        la, lo = to_ll(p)
        return [round(float(la[0]), 6), round(float(lo[0]), 6)]

    def elev(la, lo):           # the physics ground (graded, ROAD_LIFT): what a car drives on off the decks
        z = blockworker.ground(frame, la, lo, s.dem.sample(la, lo, 15))
        return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z

    def elev_xy(xy):
        la, lo = to_ll(xy)
        if not len(la):
            return np.zeros(0)
        return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2], np.float64)

    def inside(p, extra=0.0):
        return float(np.hypot(p[0], p[1])) <= R + extra

    # ---- the map's features round the place
    feats, seen_cells = [], set()
    for tx, ty in roads.tiles_near(0.0, 0.0, R + 150.0):
        for k in roads.cells_of_tile(frame, tx, ty):
            if str(k) in seen_cells:
                continue
            seen_cells.add(str(k))
            f = s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
            if f is not None and len(f):
                feats.append((str(k), f))
    feats.sort(key=lambda kv: kv[0])
    flist = [f for _k, f in feats]
    box = (-R - 150.0, -R - 150.0, R + 150.0, R + 150.0)
    bridges = blockworker._bridges(1, frame, elev)
    decks = {}
    try:
        for fid, spec, xy, z, _lon in bridges.parts(flist, box):
            xy = np.asarray(xy, np.float64)[:, :2]
            key = (str(fid), int(round(xy[0, 0])), int(round(xy[0, 1])))
            if key not in decks:
                decks[key] = (xy, np.asarray(z, np.float64), spec)
    except Exception as ex:          # (an old checkout without the API)
        errors.append(f"decks: {type(ex).__name__}: {ex}")
    decks = dict(sorted(decks.items()))
    ground, seen = [], set()        # (fid, sub, cls, spec, xy) of ground roads and drawn rails
    for f in flist:
        specs = roads.road_specs(f)
        for i in range(len(f)):
            fid = str(f.ids[i])
            if fid in seen:
                continue
            seen.add(fid)
            sub = str(f.attrs["subtype"][i])
            if sub == "road":
                if specs[i] is None:
                    continue
                cls = specs[i].cls
            elif sub == "rail":
                if railways is None or railways.drawn(f, i) is None:
                    continue
                cls = "rail"
            else:
                continue
            for ring in roads.ground_parts(f, i):
                ring = np.asarray(ring, np.float64)
                e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                gl = np.stack([np.asarray(e), np.asarray(n)], 1)
                if len(gl) >= 2 and np.hypot(*gl.T).min() < R + 100:
                    ground.append((fid, sub, cls, specs[i] if sub == "road" else None, gl))
    ground.sort(key=lambda q: (q[0], round(float(q[4][0, 0]), 2), round(float(q[4][0, 1]), 2)))
    res = {"errors": errors, "n": {"decks": len(decks), "ground_lines": len(ground)}}

    def arc(xy):
        return np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]

    def at(xy, s_, t):
        return np.stack([np.interp(t, s_, xy[:, 0]), np.interp(t, s_, xy[:, 1])], 1)

    def crossings(a_xy, b_xy):
        """(arc on a, arc on b, point) where polyline a crosses polyline b (proper crossings of segments)."""
        out = []
        if len(a_xy) < 2 or len(b_xy) < 2:
            return out
        amin, amax = a_xy.min(0), a_xy.max(0)
        bmin, bmax = b_xy.min(0), b_xy.max(0)
        if (amin > bmax).any() or (bmin > amax).any():
            return out
        sa, sb = arc(a_xy), arc(b_xy)
        p, r = a_xy[:-1], np.diff(a_xy, axis=0)
        q, sv = b_xy[:-1], np.diff(b_xy, axis=0)
        for j in range(len(q)):
            lo_, hi_ = np.minimum(b_xy[j], b_xy[j + 1]), np.maximum(b_xy[j], b_xy[j + 1])
            near = np.nonzero((np.maximum(p, p + r) >= lo_).all(1) & (np.minimum(p, p + r) <= hi_).all(1))[0]
            for i in near:
                den = r[i, 0] * sv[j, 1] - r[i, 1] * sv[j, 0]
                if abs(den) < 1e-12:
                    continue
                d = q[j] - p[i]
                t = (d[0] * sv[j, 1] - d[1] * sv[j, 0]) / den
                u = (d[0] * r[i, 1] - d[1] * r[i, 0]) / den
                if -1e-9 <= t <= 1.0 + 1e-9 and -1e-9 <= u <= 1.0 + 1e-9:
                    pt = p[i] + t * r[i]
                    if any(math.hypot(pt[0] - q_[2][0], pt[1] - q_[2][1]) < 0.5 for q_ in out):
                        continue        # (a crossing through a shared vertex: found on both segments)
                    out.append((float(sa[i] + t * (sa[i + 1] - sa[i])), float(sb[j] + u * (sb[j + 1] - sb[j])), pt))
        return out

    deck_list, nan_decks = [], []
    for k, (xy, z, spec) in decks.items():
        bad = ~np.isfinite(z) | ~np.isfinite(xy).all(1)
        if bad.any():               # (a deck with no height somewhere: itself a defect; its finite points kept)
            if inside(xy[int(np.argmax(bad))]):
                nan_decks.append([int(bad.sum()), int(len(z)), k[0], getattr(spec, "cls", None),
                                  ll(np.nan_to_num(xy[int(np.argmax(bad))]))])
            xy, z = xy[~bad], z[~bad]
            if len(z) < 2:
                continue
        deck_list.append((k[0], xy, z, spec, arc(xy)))
    res["deck_nan"] = sorted(nan_decks, key=lambda r: (r[2], r[4]))

    # ---- steep decks
    if want("deck_grade"):
        rows = []
        for fid, xy, z, spec, s_ in deck_list:
            if s_[-1] < 12:
                continue
            t = np.arange(0.0, s_[-1], 2.0)
            zz = np.interp(t, s_, z)
            if len(zz) <= 5:
                continue
            gr = np.abs(zz[5:] - zz[:-5]) / 10.0
            k = int(np.argmax(gr))
            p = at(xy, s_, t[k:k + 1])[0]
            if inside(p):
                cls = getattr(spec, "cls", None)
                rows.append([round(100 * float(gr[k]), 1), round(g_max(cls), 1), fid, cls, ll(p)])
        res["deck_grade"] = sorted(rows, key=lambda r: (-r[0], r[2], r[4]))

    # ---- deck shape: waviness (20 m chord) and long decks lying on the ground
    if want("deck_shape"):
        waves, low = [], []
        for fid, xy, z, spec, s_ in deck_list:
            if s_[-1] < 40:
                continue
            t = np.arange(0.0, s_[-1], 2.0)
            zz = np.interp(t, s_, z)
            p = at(xy, s_, t)
            if len(zz) > 10:
                rr = zz[5:-5] - 0.5 * (zz[:-10] + zz[10:])
                k = int(np.argmax(np.abs(rr)))
                if inside(p[k + 5]):
                    waves.append([r2(rr[k]), fid, getattr(spec, "cls", None), ll(p[k + 5])])
            if s_[-1] >= 60:
                mid = (t > 15.0) & (t < s_[-1] - 15.0)
                if mid.any() and inside(p[int(np.nonzero(mid)[0][len(np.nonzero(mid)[0]) // 2])]):
                    h = zz[mid] - elev_xy(p[mid])
                    low.append([r2(np.median(h)), round(float(s_[-1])), fid, getattr(spec, "cls", None),
                                ll(p[int(np.nonzero(mid)[0][len(np.nonzero(mid)[0]) // 2])])])
        res["deck_wave"] = sorted(waves, key=lambda r: (-abs(r[0]), r[1], r[3]))
        res["deck_height"] = sorted(low, key=lambda r: (r[0], r[2], r[4]))

    # ---- deck ends
    deck_ends = []
    if want("end"):
        rows = []
        for fid, xy, z, spec, s_ in deck_list:
            if s_[-1] < 4:
                continue
            for end, zend, nxt in ((xy[0], z[0], xy[1]), (xy[-1], z[-1], xy[-2])):
                deck_ends.append(end)
                if not inside(end):
                    continue
                # the ground road the deck continues on: a ground line with an END within 3 m of the deck end,
                # heading away from the deck (never a road crossing under or beside the abutment), the deck's own
                # feature first, then the nearest
                best = None
                away = end - nxt
                for gid, sub, gcls, gspec, gl in ground:
                    if sub != "road" or len(gl) < 2:
                        continue
                    for k, nb in ((0, gl[1]), (len(gl) - 1, gl[-2])):
                        d = float(np.hypot(*(gl[k] - end)))
                        if d >= 3.0 or np.dot(nb - gl[k], away) <= 0:
                            continue
                        rank = (0 if gid == fid else 1, d)
                        if best is None or rank < best[0]:
                            best = (rank, gid, gl, k, gspec)
                if best is None:
                    # no ground road ends here (a deck ending on a roundabout ring or a junction it runs into, #1374):
                    # the step to the graded ground 1 m past the end, along the deck's own direction
                    if any(float(np.hypot(*(gl - end).T).min()) < 3.0 for _g, sub, _c, _s, gl in ground if sub == "road"):
                        u = away / max(1e-9, float(np.hypot(*away)))
                        z1 = float(elev_xy(np.asarray(end + u))[0])
                        rows.append([r2(zend - z1), 0.0, 0.0, 0.0, fid, getattr(spec, "cls", None), ll(end), "joins"])
                    continue
                _, gid, gl, k, gspec = best
                fwd = gl[1:] if k == 0 else gl[:-1][::-1]
                if not len(fwd):
                    continue
                path = np.vstack([end, fwd])
                sp = arc(path)
                tt = np.arange(1.0, min(31.0, sp[-1]) + 1e-9, 2.0)
                if not len(tt):
                    continue        # (a ground piece under 1 m: a short stub up to a roundabout ring is still scored)
                zr = elev_xy(at(path, sp, tt))
                gr = np.abs(zr[5:] - zr[:-5]) / 10.0 if len(zr) > 5 else np.zeros(1)
                lip = float(np.abs(np.diff(zr, 2)).max()) if len(zr) > 2 else 0.0
                dw = (float(spec.width) - float(gspec.width)) if (spec is not None and gspec is not None) else 0.0
                rows.append([r2(zend - zr[0]), round(100 * float(gr.max()), 1), r2(lip), r2(dw), fid,
                             getattr(spec, "cls", None), ll(end), "road"])
        res["end"] = sorted(rows, key=lambda r: (-abs(r[0]), r[4], r[6]))
    else:
        for fid, xy, z, spec, s_ in deck_list:
            deck_ends += [xy[0], xy[-1]]

    # ---- clearance: decks over ground lines and over other decks
    if want("clearance"):
        rows, rows_dd = [], []
        for fid, xy, z, spec, s_ in deck_list:
            for gid, sub, gcls, gspec, gl in ground:
                for a_, _b, pt in crossings(xy, gl):
                    if a_ < 8.0 or a_ > s_[-1] - 8.0 or not inside(pt):
                        continue
                    zd = float(np.interp(a_, s_, z))
                    zg = float(elev_xy(np.asarray(pt))[0])
                    need = C_LOWER.get(gcls, C_DEFAULT)
                    rows.append([r2(zd - zg), need, fid, getattr(spec, "cls", None), gcls, ll(pt)])
        for i, (fa, xa, za, spa, sa) in enumerate(deck_list):
            for fb, xb, zb, spb, sb in deck_list[i + 1:]:
                if fa == fb:
                    continue
                for a_, b_, pt in crossings(xa, xb):
                    if not inside(pt) or min(a_, sa[-1] - a_, b_, sb[-1] - b_) < 2.0:
                        continue        # (a joint, not a crossing)
                    dz = float(np.interp(a_, sa, za) - np.interp(b_, sb, zb))
                    rows_dd.append([r2(abs(dz)), fa, getattr(spa, "cls", None), fb,
                                    getattr(spb, "cls", None), ll(pt)])
        res["clearance"] = sorted(rows, key=lambda r: (r[0], r[2], r[5]))
        res["deck_deck"] = sorted(rows_dd, key=lambda r: (r[0], r[1], r[5]))

    # ---- decks overlapping in plan at a small height difference (torn / merged ramps)
    if want("overlap"):
        samples = []
        for di, (fid, xy, z, spec, s_) in enumerate(deck_list):
            if s_[-1] < 2:
                continue
            t = np.arange(0.0, s_[-1] + 1e-9, 2.0)
            p = at(xy, s_, t)
            zz = np.interp(t, s_, z)
            w = float(getattr(spec, "width", 8.0) or 8.0)
            for k in range(len(t)):
                samples.append((di, p[k, 0], p[k, 1], zz[k], w, min(t[k], s_[-1] - t[k])))
        grid = {}
        for k, smp in enumerate(samples):
            grid.setdefault((int(math.floor(smp[1] / 20.0)), int(math.floor(smp[2] / 20.0))), []).append(k)
        best = {}
        for k, (di, x, y, zz, w, dend) in enumerate(samples):
            if dend < 3.0 or not inside((x, y)):
                continue
            gx, gy = int(math.floor(x / 20.0)), int(math.floor(y / 20.0))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for k2 in grid.get((gx + dx, gy + dy), ()):
                        dj, x2, y2, z2, w2, dend2 = samples[k2]
                        if dj <= di or dend2 < 3.0 or deck_list[dj][0] == deck_list[di][0]:
                            continue
                        d = math.hypot(x2 - x, y2 - y)
                        if d >= (w + w2) / 2.0 - 0.5:
                            continue
                        dz = abs(z2 - zz)
                        if 0.3 < dz < 4.5:
                            key = (di, dj)
                            if key not in best or dz < best[key][0]:
                                best[key] = (dz, (x, y))
        rows = [[r2(dz), deck_list[di][0], deck_list[dj][0], ll(p)] for (di, dj), (dz, p) in best.items()]
        res["overlap"] = sorted(rows, key=lambda r: (r[0], r[1], r[2]))

    # ---- ground lines crossing without a bridge, and rail crossings
    if want("unbridged") or want("rail_cross"):
        rows, rails = [], []
        for i in range(len(ground)):
            gi = ground[i]
            for j in range(i + 1, len(ground)):
                gj = ground[j]
                if gi[0] == gj[0] or (gi[1] == "rail" and gj[1] == "rail"):
                    continue
                for a_, b_, pt in crossings(gi[4], gj[4]):
                    if not inside(pt):
                        continue
                    ends = [gi[4][0], gi[4][-1], gj[4][0], gj[4][-1]]
                    at_end = min(np.hypot(*(np.asarray(ends) - pt).T)) < 2.0
                    if at_end and "rail" not in (gi[1], gj[1]):
                        continue        # (a junction; a level crossing at a shared node is still a rail crossing)
                    zs = []
                    for gl, aa in ((gi[4], a_), (gj[4], b_)):
                        s_ = arc(gl)
                        tt = np.array([aa - 20, aa - 10, aa + 10, aa + 20])
                        tt = tt[(tt >= 0) & (tt <= s_[-1])]
                        zs.append(float(np.median(elev_xy(at(gl, s_, tt)))) if len(tt) else None)
                    dz = abs(zs[0] - zs[1]) if None not in zs else None
                    if "rail" in (gi[1], gj[1]):
                        road = gj if gi[1] == "rail" else gi
                        rails.append([road[2], None if dz is None else round(dz, 1), road[0], ll(pt)])
                    if dz is not None and dz > 3.0 and not at_end:
                        rows.append([round(dz, 1), gi[0], gi[2], gj[0], gj[2], ll(pt)])
        res["unbridged"] = sorted(rows, key=lambda r: (-r[0], r[1], r[5]))
        res["rail_cross"] = sorted(rails, key=lambda r: (r[0], r[2], r[3]))

    # ---- junction nodes (3+ road line ends at one point) and spikes / humps along drivable ground roads
    if want("spikes") or want("humps"):
        ends = {}
        for gid, sub, gcls, gspec, gl in ground:
            if sub != "road" or gcls not in ROAD_CLASSES:
                continue
            for e in (gl[0], gl[-1]):
                ends.setdefault((int(round(e[0] / 1.0)), int(round(e[1] / 1.0))), []).append(gid)
        nodes = np.asarray(sorted(k for k, v in ends.items() if len(set(v)) >= 3), np.float64).reshape(-1, 2)
        dends = np.asarray(deck_ends, np.float64).reshape(-1, 2)
        spikes, humps, length = [], [], 0.0
        for gid, sub, gcls, gspec, gl in ground:
            if sub != "road" or gcls not in ROAD_CLASSES or gcls == "raceway":
                continue
            s_ = arc(gl)
            if s_[-1] < 4 * STEP * CHORD:
                continue
            t = np.arange(0.0, s_[-1] + 1e-9, STEP)
            c = at(gl, s_, t)
            keep = np.hypot(*c.T) <= R
            if not keep.any():
                continue
            length += float(keep.sum()) * STEP
            tan = np.gradient(c, axis=0)
            tan /= np.maximum(np.hypot(*tan.T), 1e-9)[:, None]
            nrm = np.stack([-tan[:, 1], tan[:, 0]], 1)
            off = max(0.0, float(gspec.width) / 2.0 - 0.5)
            dn = (np.min(np.hypot(*(c[:, None, :] - nodes[None]).transpose(2, 0, 1)), axis=1)
                  if len(nodes) else np.full(len(c), 1e9))
            de = (np.min(np.hypot(*(c[:, None, :] - dends[None]).transpose(2, 0, 1)), axis=1)
                  if len(dends) else np.full(len(c), 1e9))
            for lat_off, tag in ((0.0, "c"), (off, "l"), (-off, "r")):
                if tag != "c" and off < 1.0:
                    continue
                zz = elev_xy(c + lat_off * nrm)
                if len(zz) <= 2 * CHORD:
                    continue
                mid = zz[CHORD:-CHORD]
                rr = mid - 0.5 * (zz[:-2 * CHORD] + zz[2 * CHORD:])
                idx = np.arange(CHORD, len(zz) - CHORD)
                for k, rv in zip(idx, rr):
                    if abs(rv) <= SPIKE_M or not keep[k] or de[k] < EXCLUDE_R:
                        continue
                    row = [r2(rv), gid, gcls, tag, ll(c[k])]
                    if dn[k] < JUNCTION_R:
                        if tag == "c":
                            humps.append(row)
                    elif dn[k] >= EXCLUDE_R:
                        spikes.append(row)
        # one row per road line and side: its worst (a 6 m bump is three samples of one bump)
        def worst(rows):
            by = {}
            for r in rows:
                k = (r[1], r[3])
                if k not in by or abs(r[0]) > abs(by[k][0]) or (abs(r[0]) == abs(by[k][0]) and r[4] < by[k][4]):
                    by[k] = r
            return sorted(by.values(), key=lambda r: (-abs(r[0]), r[1], r[3]))
        res["spikes"] = worst(spikes)
        res["humps"] = worst(humps)
        res["n"]["road_m"] = int(round(length))
        res["n"]["junctions"] = int(sum(1 for p in nodes if np.hypot(*p) <= R))

    # ---- width changes where two roads meet end to end
    if want("widths"):
        rows = []
        for f in flist:
            specs = roads.road_specs(f)
            try:
                tapers = roads.road_tapers(f)
            except Exception as ex:
                tapers = [()] * len(f)
                errors.append(f"road_tapers: {type(ex).__name__}")
            endpts = {}
            for i in range(len(f)):
                sp = specs[i]
                if sp is None or sp.cls not in ROAD_CLASSES or str(f.attrs["subtype"][i]) != "road":
                    continue
                a = f.coord_off[f.ring_off[f.part_off[i]]]
                b = f.coord_off[f.ring_off[f.part_off[i] + 1]]
                line = np.asarray(f.lonlat[a:b], np.float64)
                if len(line) < 2:
                    continue
                for e, nb in ((line[0], line[1]), (line[-1], line[-2])):
                    endpts.setdefault((round(float(e[0]), 6), round(float(e[1]), 6)), []).append((i, e, nb))
            for key in sorted(endpts):
                lst = endpts[key]
                if len(lst) != 2:
                    continue
                (i, e, nb1), (j, _e, nb2) = lst
                if str(f.ids[i]) == str(f.ids[j]):
                    continue
                si, sj = specs[i], specs[j]
                dw = float(sj.width) - float(si.width)
                if abs(dw) < 0.3:
                    continue
                ee, nn, _ = frame.geodetic_to_enu(np.array([e[1]]), np.array([e[0]]), 0.0)
                p = np.array([float(ee[0]), float(nn[0])])
                if not inside(p):
                    continue
                # the turn at the joint (tapers.rs only joins < 35 deg)
                v1 = np.asarray(e) - np.asarray(nb1)
                v2 = np.asarray(nb2) - np.asarray(e)
                cosl = math.cos(math.radians(float(e[1])))
                v1 = v1 * [cosl, 1.0]
                v2 = v2 * [cosl, 1.0]
                turn = math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(v1, v2) / max(1e-12, np.linalg.norm(v1)
                                                                                            * np.linalg.norm(v2)))))))
                if turn > 35.0:
                    continue
                L = 0.0
                for k in (i, j):
                    for tp in (tapers[k] or ()):
                        if abs(tp[0] - e[0]) < 2e-5 and abs(tp[1] - e[1]) < 2e-5:
                            L += float(tp[3])
                v = max(design_speed(si), design_speed(sj))
                jump10 = abs(dw) if L <= 0.0 else abs(dw) * min(1.0, 1.5 * 10.0 / L)   # (smoothstep's peak slope)
                rows.append([r2(dw), round(L, 1), round(taper_target(dw, v), 1), r2(jump10), v,
                             str(f.ids[i]), str(f.ids[j]), si.cls, ll(p)])
        uniq = {}
        for r in rows:
            uniq.setdefault((r[5], r[6]) if r[5] < r[6] else (r[6], r[5]), r)
        res["widths"] = sorted(uniq.values(), key=lambda r: (-r[3], r[5], r[6]))

    # ---- rail beds: the graded ground along drawn railways over the ungraded ground (#1384: beds too tall)
    if want("rail_bed"):
        rows = []

        def raw_xy(xy):
            la, lo = to_ll(xy)
            z = blockworker.ground(frame, la, lo, s.dem.sample(la, lo, 15))
            return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(z, np.float64))[2], np.float64)
        for gid, sub, gcls, gspec, gl in ground:
            if sub != "rail":
                continue
            s_ = arc(gl)
            t = np.arange(0.0, s_[-1] + 1e-9, 4.0)
            p = at(gl, s_, t)
            keep = np.hypot(*p.T) <= R
            if keep.sum() < 3:
                continue
            p = p[keep]
            lift = elev_xy(p) - raw_xy(p)
            k = int(np.argmax(np.abs(lift)))
            rows.append([r2(lift[k]), r2(np.median(lift)), round(float(keep.sum()) * 4.0), gid, ll(p[k])])
        res["rail_bed"] = sorted(rows, key=lambda r: (-abs(r[0]), r[3], r[4]))

    # ---- railways over / across roads as the road tiles lay them (railways.tile_mesh's beds: complaint 3, 14)
    if want("rail_over"):
        rows_b, rows_g = [], []
        try:
            def cell_fn(key):
                return s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
            rground = railways.point_ground(frame, elev, ("roadcorpus", lat, lon))
            tunnels = railways.road_underpasses(frame, flist, box)
            rails = [r for r in railways.parts(frame, flist, box, clip=True, underpasses=tunnels) if r[1] != "tram"]
            prof = railways._beds(frame, rails, rground, tunnels + railways.coarse_underpasses(frame, flist, box, rails),
                                  cell_fn, getattr(getattr(s, "plan_store", None), "elev", None)) if rails else []
            roads_g = [(gid, gcls, gl) for gid, sub, gcls, gspec, gl in ground if sub == "road"]
            for (rfid, rcls, kind, rxy), (zb, _zg) in zip(rails, prof):
                rxy = np.asarray(rxy, np.float64)
                zb = np.asarray(zb, np.float64)
                if len(rxy) < 2 or not np.isfinite(zb).all():
                    continue
                sr = arc(rxy)
                for gid, gcls, gl in roads_g:
                    for a_, b_, pt in crossings(rxy, gl):
                        if not inside(pt):
                            continue
                        zr = float(np.interp(a_, sr, zb))
                        zg = float(elev_xy(np.asarray(pt))[0])
                        if kind == "bridge":
                            rows_b.append([r2(zr - zg), gcls, str(rfid), gid, ll(pt)])
                        else:
                            rows_g.append([r2(zr - zg), gcls, str(rfid), gid, ll(pt)])
        except Exception as ex:
            errors.append(f"rail_over: {type(ex).__name__}: {ex}")
        res["rail_bridge_clear"] = sorted(rows_b, key=lambda r: (r[0], r[2], r[4]))
        res["rail_ground_cross"] = sorted(rows_g, key=lambda r: (-abs(r[0]), r[2], r[4]))

    # ---- surfaces (paved streets drawn as dirt, #1367) and mapped road bridges without a built deck (#1098)
    if want("surface") or want("missing"):
        unpaved, missing, seen_s, seen_b = [], [], set(), set()
        dsamp = []
        for fid, xy, z, spec, s_ in deck_list:
            t = np.arange(0.0, s_[-1] + 1e-9, 2.0)
            dsamp.append(at(xy, s_, t))
        dsamp = np.vstack(dsamp) if dsamp else np.zeros((0, 2))
        for f in flist:
            specs = roads.road_specs(f)
            try:
                bspecs = roads.bridge_specs(f)
            except Exception:
                bspecs = [None] * len(f)
            for i in range(len(f)):
                fid = str(f.ids[i])
                if str(f.attrs["subtype"][i]) != "road":
                    continue
                sp = specs[i]
                if want("surface") and sp is not None and fid not in seen_s and sp.cls in ROAD_CLASSES \
                        and sp.cls not in ("service", "track") and getattr(sp, "surf", None) in ("gravel", "dirt"):
                    seen_s.add(fid)
                    for ring in roads.ground_parts(f, i):
                        ring = np.asarray(ring, np.float64)
                        e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                        gl = np.stack([np.asarray(e), np.asarray(n)], 1)
                        if len(gl) >= 2 and inside(gl[len(gl) // 2]):
                            unpaved.append([round(float(arc(gl)[-1])), sp.surf, sp.cls, fid, ll(gl[len(gl) // 2])])
                if want("missing") and bspecs[i] is not None and fid not in seen_b:
                    seen_b.add(fid)
                    for ring in roads.bridge_parts(f, i):
                        ring = np.asarray(ring, np.float64)
                        e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                        bl = np.stack([np.asarray(e), np.asarray(n)], 1)
                        L = float(arc(bl)[-1]) if len(bl) >= 2 else 0.0
                        if L < 10.0:
                            continue
                        mid = at(bl, arc(bl), np.array([L / 2.0]))[0]
                        if not inside(mid):
                            continue
                        d = float(np.hypot(*(dsamp - mid).T).min()) if len(dsamp) else 1e9
                        if d > 4.0:
                            missing.append([round(L), r2(min(d, 999.0)), bspecs[i].cls, fid, ll(mid)])
        if want("surface"):
            res["unpaved"] = sorted(unpaved, key=lambda r: (-r[0], r[3], r[4]))
        if want("missing"):
            res["missing_deck"] = sorted(missing, key=lambda r: (-r[0], r[3], r[4]))

    # ---- ride (opt-in: --checks ride): the height profile a car drives along fast carriageways, ground and decks
    # chained, and the clearances round them (bridge clearance study 2026-11-17)
    if checks is not None and "ride" in checks:
        try:
            res["ride"] = ride_check(np, roads, gradingmod, blockworker, s, frame, ground, deck_list, deck_ends, R,
                                     ll, inside, to_ll, elev_xy, arc, at, crossings)
        except Exception as ex:
            errors.append(f"ride: {type(ex).__name__}: {ex}")

    # ---- racetracks (road revamp R1k: the track surface wins)
    if want("track"):
        try:
            res["track"] = track_check(np, roads, gradingmod, blockworker, s, g, frame, flist, ground, R, ll, inside,
                                       to_ll, elev)
        except Exception as ex:
            errors.append(f"track: {type(ex).__name__}: {ex}")

    # ---- tunnels (tools/tunnelscan.py on this process's state)
    if want("tunnels"):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(1, here)
        try:
            import tunnelscan
        finally:
            for q in [q for q in sys.path if os.path.realpath(q) == os.path.realpath(os.path.join(here, "..",
                                                                                                    "server"))]:
                if os.path.realpath(q) != os.path.realpath(server):
                    sys.path.remove(q)
            sys.path.remove(here)
        tunnelscan.ENTRANCE = bool(entrance)
        orig = blockworker.init
        blockworker.init = lambda *a_, **k_: None      # (this process's state, already set up)
        import io
        import contextlib
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                tr = tunnelscan.scan(cache, lat, lon, R)
            keep_keys = ("left_out", "opened", "joint_rise", "joint_step", "steep", "thin", "portal_open",
                         "portal_deep", "blocked", "mouth_step", "breaks_upper", "untagged", "gap", "proud", "bare")
            tun = {k: sorted(json.loads(json.dumps(tr.get(k, []))), key=lambda r: json.dumps(r, sort_keys=True))
                   for k in keep_keys if k in tr}
            tun["parts"] = tr.get("parts", 0)
            tun["built"] = tr.get("built", 0)
            tun["length_m"] = int(round(tr.get("length_m", 0.0)))
            res["tunnels"] = tun
        except Exception as ex:
            errors.append(f"tunnels: {type(ex).__name__}: {ex}")
        finally:
            blockworker.init = orig
    res["summary"] = summarise(res)
    return res


RIDE_V = {"motorway": 36.0, "trunk": 28.0, "primary": 25.0}    # m/s a car drives each class at (130, 100, 90 km/h)
RIDE_JOIN_M = 6.0           # m: piece ends this near, heading on (RIDE_JOIN_COS), are one carriageway
RIDE_JOIN_COS = math.cos(math.radians(40.0))
RIDE_H = (10, 5)            # m: half chords of the vertical acceleration (the first: a launch's, the second: a jolt's)
RIDE_EVENT_G = 0.3          # g: |vertical acceleration| reported as an event
RIDE_LAUNCH_G = 0.5         # g over a crest: a launch (defect) ...
RIDE_AIR_G = 1.0            # ... and the wheels leave the road at the class's speed (blocker)
RIDE_MERGE_M = 40.0         # m: events nearer than this are one
RIDE_UNDER_M = 60.0         # m from a deck crossing over the road: an event "under" an overpass
RIDE_END_M = 15.0           # m from a deck end: an event at a deck end


def ride_check(np, roads, gradingmod, blockworker, s, frame, ground, deck_list, deck_ends, R, ll, inside, to_ll,
               elev_xy, arc, at, crossings):
    """The ride along fast carriageways (RIDE_V's classes, links left out): ground pieces on the graded physics ground,
    deck pieces on their profiles, chained end to end; the vertical acceleration of a car at the class's speed over
    20 m and 10 m chords (+ a crest, - a sag), the 10 m grade, the graded ground vs the bare elevation (dig < 0, lift
    > 0; on lidar the bare elevation is the real road), and the clearance of every deck over / under them. Pure given
    the state."""
    def raw_xy(xy):
        la, lo = to_ll(xy)
        z = blockworker.ground(frame, la, lo, s.dem.sample(la, lo, 15))
        return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(z, np.float64))[2], np.float64)
    def rcls(spec, cls):    # (a link's class "<cls>_link": RoadSpec.cls is its road's)
        return None if cls is None else (cls + "_link" if getattr(spec, "link", False) else cls)
    pieces = []         # (kind, fid, cls, xy, deck (s, z) or None)
    for gid, sub, gcls, gspec, gl in ground:
        if sub == "road" and rcls(gspec, gcls) in RIDE_V and len(gl) >= 2 and arc(gl)[-1] > 1.0:
            pieces.append(("g", gid, gcls, gl, None))
    for fid, xy, z, spec, s_ in deck_list:
        cls = rcls(spec, getattr(spec, "cls", None))
        if cls in RIDE_V and s_[-1] > 1.0:
            pieces.append(("d", fid, cls, xy, (s_, z)))

    def end_dir(xy, e):     # (end point, unit direction out of the piece there)
        sa = arc(xy)
        if e == 0:
            p, q = xy[0], at(xy, sa, np.array([min(5.0, sa[-1])]))[0]
        else:
            p, q = xy[-1], at(xy, sa, np.array([max(0.0, sa[-1] - 5.0)]))[0]
        u = p - q
        return p, u / max(1e-9, float(np.hypot(*u)))
    ends = [[end_dir(pc[3], e) for e in (0, 1)] for pc in pieces]
    best = {}
    for i, pc in enumerate(pieces):
        for e in (0, 1):
            p, u = ends[i][e]
            cand = None
            for j, pj in enumerate(pieces):
                if j == i or pj[2] != pc[2]:
                    continue
                for f in (0, 1):
                    q, w = ends[j][f]
                    d = float(np.hypot(*(q - p)))
                    if d >= RIDE_JOIN_M or float(np.dot(u, -w)) < RIDE_JOIN_COS:
                        continue
                    if cand is None or (d, j, f) < cand[0]:
                        cand = ((d, j, f), (j, f))
            if cand is not None:
                best[(i, e)] = cand[1]
    link = {k: v for k, v in best.items() if best.get(v) == k}
    used, chains = set(), []
    order = sorted(range(len(pieces)), key=lambda i: ((i, 0) in link and (i, 1) in link, pieces[i][1],
                                                      round(float(pieces[i][3][0, 0]), 2),
                                                      round(float(pieces[i][3][0, 1]), 2)))
    for i0 in order:
        if i0 in used:
            continue
        e_in = 0 if (i0, 0) not in link else (1 if (i0, 1) not in link else 0)
        seq, i, e = [], i0, e_in
        while i not in used:
            used.add(i)
            seq.append((i, e != 0))     # (entered at its end e: reversed when e = 1)
            nxt = link.get((i, 1 - e))
            if nxt is None:
                break
            i, e = nxt
        chains.append(seq)
    dends = np.asarray(deck_ends, np.float64).reshape(-1, 2)
    # (deck crossings over fast ground pieces, and fast decks over any ground line)
    under_rows, over_rows, xpts = [], [], []
    for fid, xy, z, spec, s_ in deck_list:
        depth = float(roads.deck_depth(roads.geom_span([xy])))
        dcls = rcls(spec, getattr(spec, "cls", None))
        for gid, sub, gcls, gspec, gl in ground:
            if gid == fid:
                continue
            gcls = rcls(gspec, gcls) if sub == "road" else gcls
            fast_under = sub == "road" and gcls in RIDE_V
            if not fast_under and dcls not in RIDE_V:
                continue
            for a_, _b, pt in crossings(xy, gl):
                if a_ < 8.0 or a_ > s_[-1] - 8.0:
                    continue
                if fast_under:
                    xpts.append(pt)
                if not inside(pt):
                    continue
                top = float(np.interp(a_, s_, z)) - float(elev_xy(np.asarray(pt))[0])
                row = [round(top - depth, 2), round(top, 2), round(depth, 2), gcls, dcls, fid, gid, ll(pt)]
                (under_rows if fast_under else over_rows).append(row)
    xpts = np.asarray(xpts, np.float64).reshape(-1, 2)
    events, jolts, chain_rows, devs, gl_m, dig1_m, humps = [], [], [], [], 0.0, 0.0, []
    for seq in chains:
        P, Z, K, D = [], [], [], []
        for i, rev in seq:
            kind, fid, cls, xy, dk = pieces[i]
            sa = arc(xy)
            t = np.arange(0.0, sa[-1] + 1e-9, 1.0)
            if t[-1] < sa[-1] - 1e-6:
                t = np.r_[t, sa[-1]]
            if rev:
                t = sa[-1] - t
            p = at(xy, sa, t)
            if kind == "d":
                z = np.interp(t, dk[0], dk[1])
                dig = np.full(len(t), np.nan)
            else:
                z = elev_xy(p)
                dig = z - gradingmod.ROAD_LIFT - raw_xy(p)
            if P:
                last, prev = P[-1][-1], P[-1][-2] if len(P[-1]) > 1 else P[-1][-1]
                u = last - prev
                u = u / max(1e-9, float(np.hypot(*u)))
                ahead = ((p - last) @ u) > 0.5
                p, z, dig = p[ahead], z[ahead], dig[ahead]
                if not len(p):
                    continue
            P.append(p)
            Z.append(z)
            K.append(np.full(len(p), kind == "d"))
            D.append(dig)
        if not P:
            continue
        P, Z, K, D = np.vstack(P), np.concatenate(Z), np.concatenate(K), np.concatenate(D)
        sP = arc(P)
        if sP[-1] < 40.0:
            continue
        cls = pieces[seq[0][0]][2]
        v = RIDE_V[cls]
        t = np.arange(0.0, sP[-1] + 1e-9, 1.0)
        p = at(P, sP, t)
        z = np.interp(t, sP, Z)
        kd = np.interp(t, sP, K.astype(np.float64)) > 0.5
        dg = np.interp(t, sP, np.nan_to_num(D, nan=0.0))
        dgv = np.interp(t, sP, np.isfinite(D).astype(np.float64)) > 0.99
        ins = np.hypot(*p.T) <= R
        de = (np.min(np.hypot(*(p[:, None, :] - dends[None]).transpose(2, 0, 1)), axis=1)
              if len(dends) else np.full(len(p), 1e9))
        dx = (np.min(np.hypot(*(p[:, None, :] - xpts[None]).transpose(2, 0, 1)), axis=1)
              if len(xpts) else np.full(len(p), 1e9))
        lifts = []
        for h in RIDE_H:
            lg = np.zeros(len(z))
            if len(z) > 2 * h:
                lg[h:-h] = v * v * (2.0 * z[h:-h] - z[:-2 * h] - z[2 * h:]) / (h * h) / 9.81
            lifts.append(lg)
        gr = np.zeros(len(z))
        if len(z) > 10:
            gr[5:-5] = np.abs(z[10:] - z[:-10]) / 10.0
        far = (t >= 12.0) & (t <= t[-1] - 12.0) & ins

        def event(k):
            lo_, hi_ = max(0, k - 60), min(len(z), k + 61)
            dd = dg[lo_:hi_][dgv[lo_:hi_]]
            where = "deck" if kd[k] else ("end" if de[k] < RIDE_END_M else "ground")
            return [round(float(lifts[0][k]), 2), round(float(lifts[1][k]), 2),
                    where, bool(dx[k] < RIDE_UNDER_M), round(float(dd[np.argmax(np.abs(dd))]), 2) if len(dd)
                    else None, round(float(z[lo_:hi_].max() - z[lo_:hi_].min()), 2),
                    round(100 * float(gr[k]), 1), cls, ll(p[k])]
        # (each chord's crests and its sags found apart, each merged only with its own kind within RIDE_MERGE_M: a
        # crest next to a deeper sag is its own event. The 20 m chord's are the events (launch / airborne / sag); the
        # 10 m chord's, a short jolt the 20 m chord averages away, are listed as jolts)
        for c, out_ in ((0, events), (1, jolts)):
            for sign in (1.0, -1.0):
                val = sign * lifts[c]
                cand = np.flatnonzero(far & (val >= RIDE_EVENT_G))
                cand = cand[np.argsort(-val[cand], kind="stable")]
                taken = []
                for k in cand:
                    if any(abs(t[k] - t[q]) < RIDE_MERGE_M for q in taken):
                        continue
                    taken.append(int(k))
                    out_.append(event(int(k)))
        dump = os.environ.get("RIDE_DUMP")      # (debug, "lat,lon,r,file": the chains passing there, sample by sample)
        if dump:
            q_la, q_lo, q_r, q_file = dump.split(",")
            pla, plo = to_ll(p)
            near = np.flatnonzero(np.hypot((pla - float(q_la)) * 111320.0, (plo - float(q_lo)) * 111320.0
                                           * math.cos(math.radians(float(q_la)))) <= float(q_r))
            if len(near):
                rng = list(range(max(0, near[0] - 150), min(len(t), near[-1] + 150)))
                rawd, gd = raw_xy(p[rng]), elev_xy(p[rng])
                with open(q_file, "a") as df:
                    df.write(f"# chain {cls} len {t[-1]:.0f}\n")
                    for q, k in enumerate(rng):
                        df.write(f"{t[k]:.0f} {pla[k]:.6f} {plo[k]:.6f} z={z[k]:.2f} raw={rawd[q]:.2f} graded={gd[q]:.2f} "
                                 f"dig={dg[k] if dgv[k] else float('nan'):.2f} deck={int(kd[k])} "
                                 f"lift10={lifts[0][k]:.2f} lift5={lifts[1][k]:.2f} gr={gr[k]:.3f}\n")
        gm = far & dgv & ~kd & (de > 30.0) & (dx > 30.0)      # (ground away from decks: the bare elevation is the road)
        gl_m += float(gm.sum())
        dig1_m += float((np.abs(dg[gm]) > 1.0).sum())
        devs.extend(np.abs(dg[gm]).tolist())
        chain_rows.append([round(float(t[-1])), int(ins.sum()), cls, round(100 * float(gr[far].max()), 1) if far.any()
                           else 0.0, round(float(lifts[0][far].max()), 2) if far.any() else 0.0,
                           round(float(lifts[0][far].min()), 2) if far.any() else 0.0, ll(p[len(p) // 2])])
    for fid, xy, z, spec, s_ in deck_list:
        cls = rcls(spec, getattr(spec, "cls", None))
        if cls not in RIDE_V or s_[-1] < 20.0 or not inside(xy[len(xy) // 2]):
            continue
        line = z[0] + (z[-1] - z[0]) * s_ / s_[-1]
        k = int(np.argmax(z - line))
        humps.append([round(float(z[k] - line[k]), 2), round(float(s_[-1])), cls, fid, ll(xy[k])])
    devs = np.asarray(devs, np.float64)
    return {"events": sorted(events, key=lambda r: (-abs(r[0]), r[8])),
            "jolts": sorted(jolts, key=lambda r: (-abs(r[1]), r[8])),
            "chains": sorted(chain_rows, key=lambda r: (-r[0], r[6])),
            "under": sorted(under_rows, key=lambda r: (r[0], r[5], r[7])),
            "over": sorted(over_rows, key=lambda r: (r[0], r[5], r[7])),
            "humps": sorted(humps, key=lambda r: (-r[0], r[3], r[4])),
            "ground_m": int(round(gl_m)), "dig1_m": int(round(dig1_m)),
            "dev_mean": round(float(devs.mean()), 3) if len(devs) else None,
            "dev_p95": round(float(np.percentile(devs, 95)), 2) if len(devs) else None}


TRACK_DZ_M = 0.05           # m the drawn track may leave its own smoothed surface (nothing else grades it)
TRACK_BREACH_M = 0.3        # m: less than this between a tube's vault and the track over it: the tube through the track
TRACK_STEP_M = 2.0


def track_check(np, roads, gradingmod, blockworker, s, g, frame, flist, ground, R, ll, inside, to_ll, elev):
    """Racetracks (highway=raceway; road revamp R1k, "the track surface wins"): along every raceway on the ground (its
    centre line and both edges less 0.5 m, every TRACK_STEP_M) the terrain block's ground (graded, the low arches'
    soil raised over them: roads.arch_raise) against the track's own smoothed surface (grading's raceway smoothing on
    the bare ground): surface [max |dz| m, samples over TRACK_DZ_M, fid, where]; a tunnel tube under it with less than
    TRACK_BREACH_M between its vault and the track: breach [m, tunnel part, where]; a tunnel's open section (no tube)
    across it: open_under [fid, where]; a bridge on a raceway the map does not tag (a grade separation): bridges [m,
    fid, where]; a road's mapped tunnel crossing under a raceway built as no tunnel: opened [fid, where]."""
    import roadmesh
    out = {"surface": [], "breach": [], "open_under": [], "bridges": [], "opened": [], "track_m": 0}
    races = [(fid, spec, gl) for fid, sub, cls, spec, gl in ground if cls == "raceway"]
    box = (-R - 150.0, -R - 150.0, R + 150.0, R + 150.0)
    # the map's raceway bridges vs the ones built; mapped tunnels under raceways vs the ones built
    rlines = [gl for _f, _s, gl in races]
    seen = set()

    def dist(line, p):          # (point p to polyline line)
        a, ab = line[:-1], np.diff(line, axis=0)
        t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
        return float(np.hypot(*(a + t[:, None] * ab - p).T).min())
    for f in flist:
        for i in range(len(f)):
            fid = str(f.ids[i])
            if fid in seen or str(f.attrs["subtype"][i]) != "road":
                continue
            seen.add(fid)
            is_race = "class" in f.attrs and str(f.attrs["class"][i]) == "raceway"
            want = "bridge" if is_race else "tunnel"
            tagged = roads._parts_by_level_new(f, i, want, rngs=roads.tag_level_ranges(f, i))
            built = roads._parts_by_level(f, i, want)

            def enu(ring):
                ring = np.asarray(ring, np.float64)
                e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                return np.stack([np.asarray(e), np.asarray(n)], 1)

            def mid(xy):
                sa = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
                t = sa[-1] / 2
                return np.array([np.interp(t, sa, xy[:, 0]), np.interp(t, sa, xy[:, 1])]), float(sa[-1])
            tl = [enu(r) for r in tagged if len(r) >= 2]
            bl = [enu(r) for r in built if len(r) >= 2]
            if is_race:
                for xy in bl:
                    m, L = mid(xy)
                    if inside(m) and not any(dist(t, m) < 3.0 for t in tl):
                        out["bridges"].append([round(L, 1), fid, ll(m)])
            elif tl and rlines:
                for xy, cr in zip(tl, roads._crossings_by(rlines, tl)):
                    m, _L = mid(xy)
                    if cr and inside(m) and not any(dist(b, m) < 3.0 for b in bl):
                        out["opened"].append([fid, ll(m)])
    if not races:
        return out
    tun = blockworker.tunnel_profiles(None, frame, elev, g)
    parts = list(tun.parts(flist, box))
    heights = dict(getattr(tun, "heights", {}) or {})
    mouths = getattr(tun, "mouths", None)
    opens = list(tun.open_parts(flist, box))
    for fid, spec, gl in races:
        line, _st = gradingmod.resample(gl, TRACK_STEP_M)
        if len(line) < 2:
            continue
        t = np.gradient(line, axis=0)
        t /= np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
        nrm = np.stack([-t[:, 1], t[:, 0]], axis=1)
        o = max(spec.width / 2 - 0.5, 0.0)
        pts = np.concatenate([line, line + nrm * o, line - nrm * o])
        keep = np.hypot(pts[:, 0], pts[:, 1]) <= R
        if not keep.any():
            continue
        pts = pts[keep]
        out["track_m"] += int(round(keep[:len(line)].sum() * TRACK_STEP_M))
        la, lo = to_ll(pts)
        base = np.asarray(blockworker.ground(frame, la, lo, s.dem.sample(la, lo, 15)), np.float64)
        g.apply(la, lo, base, gradingmod.ROAD_LIFT)
        # (again, its cells planned now: a point on two raceways' bands takes the nearest one whose cell is planned
        # (grading.raceway_ground), so the first answer can differ from a later one by which cells were planned)
        got = np.asarray(g.apply(la, lo, base, gradingmod.ROAD_LIFT), np.float64)
        # (the planners' own smoothing, each in its region's frame, on the ground apply smooths: decks' bridges out)
        b2 = np.asarray(g.deck_ground(la, lo, base), np.float64) if hasattr(g, "deck_ground") else base
        ref = b2.copy()
        on = np.zeros(len(la), bool)    # (on the drawn band: the planners' own raceway lines, as placed and rounded)
        import inspect
        import racedig as racedigmod
        # (BNE_RACEWAY_DIG in a checkout that has it: the raceways laid by their dominance where they meet, as apply)
        joined = getattr(racedigmod, "ON", False) and "layered" in inspect.signature(
            type(g.store.planners_of(la[:1], lo[:1])[0][0])._raceway_ground).parameters
        for pl, idx in g.store.planners_of(la, lo):
            idx = np.arange(len(la)) if idx is None else idx
            pe, pn, _ = pl.frame.geodetic_to_enu(la[idx], lo[idx], 0.0)
            if joined:
                ref[idx] = pl._raceway_ground(np.asarray(pe), np.asarray(pn), b2[idx], layered=True)[0]
            else:
                ref[idx] = pl._raceway_ground(np.asarray(pe), np.asarray(pn), b2[idx])
            m = pl._on_kept(np.asarray(pe, np.float64), np.asarray(pn, np.float64))
            if m is not None:
                on[idx] = m

        def up(z):
            return np.asarray(frame.geodetic_to_enu(la, lo, z)[2], np.float64)
        zr, zg = up(ref), up(got)
        zb = np.asarray(roads.arch_raise(parts, heights, pts[:, 0], pts[:, 1], zg, mouths), np.float64)
        dz = np.where(on, zb - zr, 0.0)
        bad = np.abs(dz) > TRACK_DZ_M
        if bad.any():
            k = int(np.argmax(np.abs(dz)))
            out["surface"].append([round(float(dz[k]), 2), int(bad.sum()), fid, ll(pts[k])])
        # tubes under it
        for pid, pspec, pxy, pz, _lon in parts:
            pxy = np.asarray(pxy, np.float64)[:, :2]
            pz = np.asarray(pz, np.float64)
            if len(pxy) < 2 or not np.isfinite(pz).all():
                continue
            hw = pspec.width / 2 + roadmesh.TUNNEL_SHOULDER
            lo_, hi_ = pxy.min(0) - hw, pxy.max(0) + hw
            sel = np.flatnonzero((pts[:, 0] >= lo_[0]) & (pts[:, 0] <= hi_[0]) & (pts[:, 1] >= lo_[1])
                                 & (pts[:, 1] <= hi_[1]))
            if not len(sel):
                continue
            th = float(heights.get(pid, roadmesh.TUNNEL_H))
            worst = None
            for j in sel.tolist():
                zf = roads.tunnel_floor_at([(pid, pspec, pxy, pz, None)], pts[j, 0], pts[j, 1])
                if zf is None:
                    continue
                room = float(zb[j] - (zf + th))
                if worst is None or room < worst[0]:
                    worst = (room, j)
            if worst is not None and worst[0] < TRACK_BREACH_M:
                out["breach"].append([round(worst[0], 2), str(pid)[:40], ll(pts[worst[1]])])
        for pid, pspec, pxy, _pz, _lon in opens:
            pxy = np.asarray(pxy, np.float64)[:, :2]
            if len(pxy) >= 2 and roads._crossings_by([gl], [pxy])[0]:
                c = roads._crossings_by([gl], [pxy])[0][0][2]
                if inside(c):
                    out["open_under"].append([str(pid)[:40], ll(c)])
    for k in out:
        if isinstance(out[k], list):
            out[k] = sorted(out[k], key=lambda r: json.dumps(r))
    return out


def summarise(res):
    """Blockers and defects per category (the plan's section 5.3 scorecard)."""
    out = {}
    if "track" in res:
        t = res["track"]
        blk = len(t["breach"]) + len(t["bridges"]) + sum(1 for r in t["surface"] if abs(r[0]) > 0.3)
        out["track"] = {"blocker": blk, "defect": blk + len(t["opened"]) + len(t["open_under"])
                        + sum(1 for r in t["surface"] if abs(r[0]) <= 0.3),
                        "worst_dz": max((abs(r[0]) for r in t["surface"]), default=0.0), "track_m": t["track_m"]}
    if "deck_grade" in res:
        dg = res["deck_grade"]
        out["steep_deck"] = {"defect": sum(1 for r in dg if r[0] > r[1] + 1.0), "blocker": sum(1 for r in dg if r[0] > 15),
                             "worst": dg[0][0] if dg else None, "n": len(dg)}
    if "deck_wave" in res:
        w = res["deck_wave"]
        out["deck_shape"] = {"wavy": sum(1 for r in w if abs(r[0]) > 0.3), "worst_wave": w[0][0] if w else None,
                             "on_ground": sum(1 for r in res["deck_height"] if r[0] < 1.5)}
    if "rail_bridge_clear" in res:
        rb, rg = res["rail_bridge_clear"], res["rail_ground_cross"]
        # a rail bridge over a road: rail top vs road surface (deck depth not taken off); a ground track across a road
        # more than 0.5 m off it is an embankment or a pit across the carriageway (a level crossing must be flush)
        out["rail_block"] = {"blocker": sum(1 for r in rb if r[0] < 2.5) + sum(1 for r in rg if abs(r[0]) > 0.5),
                             "defect": sum(1 for r in rb if r[0] < 5.0 + 0.6) + sum(1 for r in rg if abs(r[0]) > 0.1),
                             "bridges": len(rb), "at_grade": len(rg),
                             "lowest_bridge": rb[0][0] if rb else None,
                             "worst_grade": rg[0][0] if rg else None}
    if "rail_bed" in res:
        rb = res["rail_bed"]
        out["rail_bed"] = {"tall3": sum(1 for r in rb if abs(r[0]) > 3.0), "worst": rb[0][0] if rb else None,
                           "lines": len(rb)}
    if "unpaved" in res:
        out["unpaved_m"] = sum(r[0] for r in res["unpaved"])
    if "missing_deck" in res:
        out["missing_deck"] = {"blocker": len(res["missing_deck"]), "defect": len(res["missing_deck"]),
                               "m": sum(r[0] for r in res["missing_deck"])}
    if "end" in res:
        e = res["end"]
        out["end_step"] = {"blocker": sum(1 for r in e if abs(r[0]) > 0.3), "defect": sum(1 for r in e if abs(r[0]) > 0.05),
                           "ramp12": sum(1 for r in e if r[1] > 12), "lip25": sum(1 for r in e if r[2] > 0.25),
                           "width30": sum(1 for r in e if abs(r[3]) > 0.3),
                           "worst": max((abs(r[0]) for r in e), default=None), "n": len(e)}
    if "clearance" in res:
        c = res["clearance"]
        out["clearance"] = {"blocker": sum(1 for r in c if r[0] < 2.5), "defect": sum(1 for r in c if r[0] < r[1] - 0.1),
                            "lowest": c[0][0] if c else None, "n": len(c)}
        dd = res["deck_deck"]
        out["deck_deck"] = {"blocker": sum(1 for r in dd if 0.3 < r[0] < 2.5), "defect": sum(1 for r in dd if 0.3 < r[0] < 4.6),
                            "n": len(dd)}
    if "overlap" in res:
        o = res["overlap"]
        out["overlap"] = {"blocker": sum(1 for r in o if r[0] < 2.5), "defect": len(o)}
    if "unbridged" in res:
        u = res["unbridged"]
        out["unbridged"] = {"defect": len(u), "blocker": sum(1 for r in u if "rail" in (r[2], r[4])),
                            "worst": u[0][0] if u else None}
        rc = res["rail_cross"]
        out["rail_cross"] = {"n": len(rc), "fast_at_grade": sum(1 for r in rc if r[0] in FAST and r[1] is not None
                                                                  and r[1] < 1.0)}
    if "spikes" in res:
        sp = res["spikes"]
        out["spikes"] = {"defect": len(sp), "blocker": sum(1 for r in sp if abs(r[0]) > 0.6),
                         "worst": sp[0][0] if sp else None, "road_m": res["n"].get("road_m")}
        h = res["humps"]
        out["humps"] = {"defect": len(h), "blocker": sum(1 for r in h if abs(r[0]) > 0.6),
                        "worst": h[0][0] if h else None, "junctions": res["n"].get("junctions")}
    if "widths" in res:
        w = res["widths"]
        out["widths"] = {"n": len(w), "defect": sum(1 for r in w if r[3] > 0.3),
                         "steps": sum(1 for r in w if r[1] <= 0.0),
                         "short_vs_speed": sum(1 for r in w if r[1] < r[2])}
    if "tunnels" in res:
        t = res["tunnels"]
        blk = sum(len(t.get(k, [])) for k in ("blocked", "left_out", "portal_open"))
        dfc = sum(len(t.get(k, [])) for k in ("blocked", "left_out", "portal_open", "portal_deep", "mouth_step", "opened",
                                               "joint_step", "steep", "thin", "breaks_upper", "gap", "proud", "bare"))
        out["tunnels"] = {"blocker": blk, "defect": dfc, "parts": t.get("parts"), "built": t.get("built")}
    if "ride" in res:
        r = res["ride"]
        ev, un = r["events"], r["under"]
        out["ride"] = {"blocker": sum(1 for e in ev if e[0] >= RIDE_AIR_G),
                       "defect": sum(1 for e in ev if e[0] >= RIDE_LAUNCH_G or e[0] <= -RIDE_LAUNCH_G),
                       "launch": sum(1 for e in ev if e[0] >= RIDE_LAUNCH_G),
                       "sag": sum(1 for e in ev if e[0] <= -RIDE_LAUNCH_G),
                       "worst_crest": max((e[0] for e in ev), default=0.0),
                       "jolt_crest": sum(1 for e in r.get("jolts", ()) if e[1] >= RIDE_AIR_G),
                       "worst_jolt": max((e[1] for e in r.get("jolts", ())), default=0.0),
                       "under_mw_lt40": sum(1 for u in un if u[3] in ("motorway", "trunk") and u[0] < 4.0),
                       "under_mw_lt45": sum(1 for u in un if u[3] in ("motorway", "trunk") and u[0] < 4.5),
                       "under_n": len(un), "over_n": len(r["over"]),
                       "hump_max": max((h[0] for h in r["humps"]), default=0.0),
                       "dig1_m": r["dig1_m"], "ground_m": r["ground_m"], "dev_mean": r["dev_mean"]}
    out["deck_nan"] = len(res.get("deck_nan", []))
    out["errors"] = len(res.get("errors", []))
    return out


if __name__ == "__main__":
    main()
