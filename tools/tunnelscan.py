"""tunnelscan.py PLACE|LAT,LON [R] [--cache DIR] [--json OUT] [--list]: every road tunnel round a place as the server
builds it (roads.TunnelProfiles: sections, floors, tube heights, portals) checked against the DEM and the roads over
it, the tunnel complaint categories at once (2026-11-12, the user: "tunnels are still need lots of work and are often
broken or out of place"):

  left_out     a tunnel chain dropped whole (under water: _mark_drowned)
  opened       a mapped tunnel part built as an open road all along (no tube: the DEM shows no ground over it)
  joint_rise   a part's end that joins the next part of the same tunnel inside the hill with its floor near the
               ground there (the floor climbs out of the hill to the joint: a tunnel cut in two, steep inside)
  joint_step   two parts' floors at their shared joint apart by more than 0.5 m
  steep        a covered section's floor steeper than 8 % over 20 m
  thin         share of a full tube's length where the ground over its floor is under its height (the terrain holed
               over the tube: the tube stands out of the hill, sky through the terrain)
  portal_open  a full tube's mouth with the ground 10 m inside under its height (a portal standing in the open)
  portal_deep  a mouth with the DEM still TUNNEL_H + 2 m over the floor 10 m out along the approach (the hill goes on
               past the mapped portal: the approach is a deep cut or the terrain closes the mouth)
  blocked      the graded ground 3-15 m out of a mouth more than 1 m over the floor's line (terrain across the opening)
  mouth_step   the graded road 2 m out of a mouth vs the floor at the mouth, more than 0.5 m apart
  breaks_upper a ground road or a deck crossing over a covered section lower than the tube's top + 0.3 m
  untagged     a ground road (motorway..tertiary) with the DEM more than 8 m over its own lower hull for 40 m or more:
               ground over a road the map does not call a tunnel
The entrances as built (2026-11-12, the user: "focus on tunnel entrances"; the road tile's tunnel mesh round each
mouth, roadmesh.tunnel on roads._tunnel_chunks, and the terrain block's holes, roads.tunnel_hole_mask, on a 1 m grid):
  gap          a mouth with 3 or more terrain-hole squares within 30 m that nothing covers (not the opening's floor at
               its level, no mesh over them or under them less than 1 m below their lowest corner): sky or the void
               under the terrain seen through the ground beside / over the mouth
  proud        the portal's structure more than 3 m over the ground beside the opening (2.5 m or more beyond the tube's
               wall up to 4 m further, at the mouth's plane; the highest of the ground there and 1.5, 3 and 5 m behind
               it, the hill it holds back): a slab standing on the hillside ("a concrete bunker", GitHub #403 / #1066)
  bare         a full tube with more than 5 m within 40 m of its mouth where the ground is under its crown + 0.5 m and
               nothing covers the vault from outside (the tube is faced inwards: sky through it, "a floating shell")
Prints a summary and the worst of each with lat/lon; --json writes everything. A scratch cache by default
(~/bnge-tests/tunnels/cache): never the user's."""
import json
import logging
import math
import multiprocessing
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))

PLACES = {      # name: (lat, lon, R m) - the survey's places (complaints and varied tunnel kinds)
    "tromso": (69.6489, 18.9560, 1500.0),
    "bergen": (60.3930, 5.3300, 1500.0),
    "gotthard": (46.6400, 8.5900, 1500.0),         # GitHub #1157 #1160 #1161 (Schoellenen / Goeschenen)
    "hochschwab": (47.6800, 15.1600, 3500.0),     # B24 Hochschwab Strasse, Weichselboden (Discord 11-10)
    "pittsburgh": (40.4330, -80.0100, 1500.0),    # Fort Pitt, Liberty, bi-level decks
    "portland": (45.5231, -122.6700, 1200.0),     # Burnside (Discord 11-10, 11-11)
    "paris": (48.8161, 2.3603, 1200.0),           # Porte d'Italie, GitHub #1172
    "vienna": (48.2000, 16.3100, 1500.0),         # lids, GitHub #1083 (Kennedybruecke) / Gürtel
    "dc": (38.8960, -77.0130, 1200.0),            # I-395 under Capitol Crossing (US urban underpass)
    "manazuru": (35.1560, 139.1330, 2000.0),      # Japan coast road tunnels (Discord: "my benchmark")
    "compiegne": (49.4222, 2.8065, 800.0),        # Discord 11-11
    "kanan": (34.1057, -118.8079, 800.0),         # GitHub #660 twin tunnels, one without a liner
    "munich": (48.1398, 11.5558, 1000.0),         # GitHub #362
    "stuttgart": (48.7700, 9.1700, 1500.0),       # GitHub #490 underground junctions
    "perth": (-31.9435, 115.8539, 1200.0),        # GitHub #464 / #1018 Northbridge
    "berga": (42.1050, 1.8450, 2500.0),           # GitHub #486 Pyrenees C-16 (Túnel de Berga)
    "moirana": (66.2950, 14.1500, 2500.0),        # Korgfjelltunnelen ends (Discord 11-08/09)
    "folkestone": (51.0960, 1.1430, 1500.0),      # Eurotunnel terminal (Discord 11-11)
}
ENTRANCES = {   # the entrance survey's extra places (2026-11-12, the user: "focus on tunnel entrances"): small radii
    "goeschenen": (46.6672, 8.5838, 500.0),       # Gotthard road tunnel north portal (Discord 10-28)
    "loibl": (46.4420, 14.2610, 900.0),           # Loiblpass tunnel (Discord 10-23/25: "entrances blocked")
    "laerdal": (61.0660, 7.5060, 900.0),          # Laerdalstunnelen, east portal
    "arlberg": (47.1290, 10.2560, 900.0),         # Arlberg road tunnel, St. Anton portal
    "sasago": (35.6090, 138.7900, 900.0),         # Chuo Expressway Sasago tunnel, east portals (twin bores)
    "nagao": (35.2540, 139.0010, 700.0),          # Nagao pass tunnel, Hakone (Discord 10-24: blocked)
    "eisenhower": (39.6793, -105.9200, 700.0),    # Eisenhower-Johnson west portals (twin bores, mountain)
    "caldecott": (37.8566, -122.2140, 600.0),     # Caldecott east portals (four bores)
    "lantos": (37.5775, -122.5115, 500.0),        # Tom Lantos tunnels north portals (CA-1)
    "fortpitt": (40.4376, -80.0139, 400.0),       # Fort Pitt north portal onto its bridge
    "centralpark": (40.7797, -73.9660, 500.0),    # the 79th Street Transverse's arches (short underpasses)
    "mysen": (59.56722, 11.36478, 400.0),         # R4 Braatenbrua underpass (the user's report 2026-10-21)
    "malaga": (36.7180, -4.4780, 600.0),          # GitHub #450 underpass under the A-7
    "sydney": (-33.8745, 151.2190, 700.0),        # Cross City Tunnel east approach (Discord 11-09)
    "oresund": (55.6290, 12.6480, 900.0),         # Drogden tunnel's Kastrup approach (Discord 11-08: flooded)
    "konrad": (48.77682, 9.18387, 350.0),         # Stuttgart Konrad-Adenauer-Strasse underpass ramps
    "northbridge": (-31.9437, 115.8536, 500.0),   # Perth Graham Farmer Freeway tunnel (GitHub #464 / #1018)
    "i395": (38.9020, -77.0144, 400.0),           # DC I-395 3rd Street tunnel, north mouths
    "ported": (48.8161, 2.3603, 500.0),           # Paris Porte d'Italie ramps (GitHub #1172)
    "floyfjell": (60.3890, 5.3399, 400.0),        # Bergen Floyfjellstunnelen south portals (twin, urban)
}
PLACES.update(ENTRANCES)
TH_FULL = 6.8
DUMP = False
PROFILE = None      # --profile M: every built tunnel section within M m of the place printed sample by sample
STEEP = 0.08
UNTAGGED_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary", "motorway_link", "trunk_link",
                    "primary_link"}


def main():
    args = [a for a in sys.argv[1:]]
    if "--list" in args:
        for k, v in PLACES.items():
            print(k, *v)
        return
    cache = os.path.expanduser("~/bnge-tests/tunnels/cache")
    out_json = None
    if "--cache" in args:
        k = args.index("--cache")
        cache = os.path.expanduser(args[k + 1])
        del args[k:k + 2]
    global DUMP, PROFILE
    if "--profile" in args:
        k = args.index("--profile")
        PROFILE = float(args[k + 1])
        del args[k:k + 2]
    if "--no-entrance" in args:
        global ENTRANCE
        ENTRANCE = False
        args.remove("--no-entrance")
    if "--dump" in args:
        DUMP = True
        args.remove("--dump")
    if "--json" in args:
        k = args.index("--json")
        out_json = args[k + 1]
        del args[k:k + 2]
    if os.path.realpath(cache) == os.path.realpath(os.path.expanduser("~/.cache/beamng-earth")):
        sys.exit("not the user's cache")
    os.makedirs(cache, exist_ok=True)
    name = args[0]
    if name in PLACES:
        lat, lon, R = PLACES[name]
    else:
        lat, lon = (float(v) for v in name.split(","))
        R = 1000.0
    if len(args) > 1:
        R = float(args[1])
    res = scan(cache, lat, lon, R)
    res["place"] = [name, lat, lon, R]
    report(res)
    if out_json:
        with open(out_json, "w") as f:
            json.dump(res, f)


def scan(cache, lat, lon, R):
    import numpy as np
    import blockworker
    import geodesy
    import grading as gradingmod
    import roads
    blockworker.init(cache, False, True, True, True, False, None, logging.ERROR, False, True)
    s = blockworker._s
    g, _ = s.anchor(1, lat, lon)
    frame = geodesy.EnuFrame(lat, lon)

    def ll(p):
        la, lo, _ = frame.enu_to_geodetic(np.array([p[0]]), np.array([p[1]]), 0.0)
        return [round(float(la[0]), 6), round(float(lo[0]), 6)]

    def to_ll(xy):
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return np.asarray(la), np.asarray(lo)

    def enu_z(la, lo, z):
        return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(z, np.float64))[2], np.float64)

    def raw_xy(xy):             # the tunnels' cover ground (TunnelProfiles.raw: PROFILE_ZOOM, lids in)
        la, lo = to_ll(xy)
        return enu_z(la, lo, blockworker.ground(None, la, lo, s.dem.sample(la, lo, gradingmod.PROFILE_ZOOM)))

    def dem_xy(xy):             # the bare DEM (no lids, no bumps)
        la, lo = to_ll(xy)
        return enu_z(la, lo, s.dem.sample(la, lo, gradingmod.PROFILE_ZOOM))

    def elev(la, lo):           # the physics ground (graded, ROAD_LIFT)
        z = blockworker.ground(frame, la, lo, s.dem.sample(la, lo, 15))
        return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z

    def graded_xy(xy):
        la, lo = to_ll(xy)
        return enu_z(la, lo, elev(la, lo))

    tun = roads.TunnelProfiles(frame, lambda key: s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads"),
                               elev)
    sea = getattr(s, "sea", None)
    tun.wet = sea.depth if sea is not None else None
    tun.water = sea.water if sea is not None else None
    tun.raw = lambda la, lo: blockworker.ground(None, la, lo, s.dem.sample(la, lo, gradingmod.PROFILE_ZOOM))
    if g is not None:
        tun.ends = lambda la, lo: g.apply(la, lo, tun.raw(la, lo), gradingmod.ROAD_LIFT, roads_only=True)
    bridges = blockworker._bridges(1, frame, elev)

    keys = set()
    for tx, ty in roads.tiles_near(0.0, 0.0, R):
        keys.update(str(k) for k in roads.cells_of_tile(frame, tx, ty))
    import mapcells
    cells, cellkeys, cellobj = [], {}, {}
    for tx, ty in roads.tiles_near(0.0, 0.0, R):
        for k in roads.cells_of_tile(frame, tx, ty):
            if str(k) not in cellkeys:
                cellobj[str(k)] = k
                cellkeys[str(k)] = s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
    cells = list(cellkeys.values())
    box = (-R, -R, R, R)
    closed = tun.parts(cells, box)
    opened = tun.open_parts(cells, box)
    decks = bridges.parts(cells, box)

    # every mapped tunnel part near the place (to count the ones left out)
    mapped, ground_lines = {}, []
    seen = set()
    for f in cells:
        if f is None or not len(f):
            continue
        ts = roads.tunnel_specs(f)
        rs = roads.road_specs(f)
        for i in range(len(f)):
            fid = str(f.ids[i])
            if ts[i] is not None:
                for j, ring in roads._keyed(roads.TunnelProfiles.keys_of, roads.tunnel_parts, f, i):
                    e, n, _ = frame.geodetic_to_enu(np.asarray(ring)[:, 1], np.asarray(ring)[:, 0], 0.0)
                    xy = np.stack([np.asarray(e), np.asarray(n)], 1)
                    if len(xy) >= 2 and np.abs(xy).max(axis=1).min() < R:
                        pid = f"{fid}.{j}"
                        if pid not in mapped or len(xy) > len(mapped[pid][1]):
                            mapped[pid] = (ts[i], xy)
            if rs[i] is not None and fid not in seen:
                seen.add(fid)
                for ring in roads.ground_parts(f, i):
                    e, n, _ = frame.geodetic_to_enu(np.asarray(ring)[:, 1], np.asarray(ring)[:, 0], 0.0)
                    gl = np.stack([np.asarray(e), np.asarray(n)], 1)
                    if len(gl) >= 2 and np.abs(gl).max(axis=1).min() < R + 100:
                        ground_lines.append((fid, rs[i].cls, rs[i].width / 2, gl))

    # the built sections per part
    subs = {}
    for kind_open, lst in ((False, closed), (True, opened)):
        for fid, spec, xy, z, _lon in lst:
            pid, _, n = fid.rpartition("#")
            subs.setdefault(pid, []).append((int(n), fid, spec, np.asarray(xy, np.float64)[:, :2],
                                             np.asarray(z, np.float64), kind_open))
    # one part clipped differently in two cells (two keys, the same road over the same ground): the built / longer
    byfid = {}
    for pid in mapped:
        byfid.setdefault(pid.rpartition(".")[0], []).append(pid)
    for pids in byfid.values():
        pids.sort(key=lambda q: (q not in subs, -len(mapped[q][1])))
        for k, q in enumerate(pids):
            mid = mapped[q][1][len(mapped[q][1]) // 2]
            if any(q2 in mapped and np.hypot(*(mapped[q2][1] - mid).T).min() < 3.0 for q2 in pids[:k]):
                del mapped[q]
    res = {k: [] for k in ("left_out", "opened", "joint_rise", "joint_step", "steep", "thin", "portal_open",
                           "portal_deep", "blocked", "mouth_step", "breaks_upper", "untagged") + ENTRANCE_CATS}
    mouth_pts = []          # (p, out, fid, cls) of every mouth: entrance_checks
    res["parts"] = len(mapped)
    res["built"] = len(subs)
    res["length_m"] = 0.0

    def arc(xy):
        return np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]

    # part ends shared by two tunnel parts (joints inside the hill), by mapped geometry
    ends = {}
    for pid, (_spec, xy) in mapped.items():
        for e in (0, -1):
            ends.setdefault((round(xy[e][0] / 1.0), round(xy[e][1] / 1.0)), []).append(pid)

    def joint(p):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                got = ends.get((round(p[0]) + dx, round(p[1]) + dy), [])
                for q in got:
                    for e in (0, -1):
                        if np.hypot(*(mapped[q][1][e] - p)) < 1.0:
                            yield q

    joint_floor = {}
    res["dump"] = []
    res["mouths"] = []      # every covered section's mouth: where, which way out, its floor, the ground round it
    gsecs = []              # the grading's own covered sections (their ends: the floor the terrain is cut to)
    if g is not None:
        seen_g = set()
        for ck, f in cellkeys.items():
            if f is None or not len(f):
                continue
            try:
                pl = g.store.planner_of_cell(cellobj[ck])
                for gfid, _sp, gxy, gz, kind in pl._tunnel_sections(f):
                    if not kind:
                        continue
                    gla, glo, _h = pl.frame.enu_to_geodetic(np.asarray(gxy)[:, 0], np.asarray(gxy)[:, 1], 0.0)
                    ge, gn, _u = frame.geodetic_to_enu(gla, glo, 0.0)
                    key = (gfid, round(float(ge[0]), 1), round(float(gn[0]), 1))
                    if key not in seen_g:
                        seen_g.add(key)
                        gsecs.append((np.c_[np.asarray(ge), np.asarray(gn)], np.asarray(gz, np.float64)))
            except Exception as ex:
                print("grader sections:", type(ex).__name__, ex)

    def grader_floor(p):
        best = None
        for gxy, gz in gsecs:
            for e in (0, -1):
                d = float(np.hypot(*(gxy[e] - p)))
                if d < 3.0 and (best is None or d < best[0]):
                    best = (d, float(gz[e]))
        return None if best is None else best[1]

    for pid, (spec, xy) in sorted(mapped.items()):
        L = float(arc(xy)[-1])
        res["length_m"] += L
        if DUMP:
            line = [pid[:14], spec.cls, round(L), ll(xy[0]), ll(xy[-1]), "drowned" if tun._drowned.get(pid) else ""]
            for n, fid, _sp, sxy, sz, is_open in sorted(subs.get(pid, [])):
                kd = "open" if is_open else ("low%.1f" % tun.height(fid) if tun.height(fid) < TH_FULL - 0.05 else "full")
                cv = raw_xy(sxy) - sz
                line.append((n, kd, round(float(arc(sxy)[-1])), round(float(sz[0]), 1), round(float(sz[-1]), 1),
                             round(float(np.nanmax(cv)), 1), tun.mouth(fid) if not is_open else ""))
            res["dump"].append(line)
            print("DUMP", *line)
        if pid not in subs:
            drowned = tun._drowned.get(pid)
            res["left_out"].append((round(L), pid[:14], spec.cls, ll(xy[len(xy) // 2]), bool(drowned)))
            continue
        ss = sorted(subs[pid])
        if all(o for *_, o in ss):
            xyall = np.vstack([q[3] for q in ss])
            zall = np.concatenate([q[4] for q in ss])
            cov = raw_xy(xyall) - zall
            res["opened"].append((round(L), pid[:14], spec.cls, round(float(np.nanmax(cov)), 1), ll(xy[len(xy) // 2])))
        # the whole part's floor (sections share their end samples)
        pxy = np.vstack([q[3] if k == 0 else q[3][1:] for k, q in enumerate(ss)])
        pz = np.concatenate([q[4] if k == 0 else q[4][1:] for k, q in enumerate(ss)])
        for e in (0, -1):
            others = [q for q in joint(pxy[e]) if q != pid]
            if not others:
                continue
            joint_floor.setdefault(tuple(np.round(pxy[e], 0)), []).append((pid, float(pz[e])))
            cv = float(raw_xy(pxy[[e]])[0] - pz[e])
            covall = raw_xy(pxy) - pz
            if cv < 3.0 and float(np.nanmax(covall)) > 12.0:
                res["joint_rise"].append((round(cv, 1), round(float(np.nanmax(covall))), pid[:14], spec.cls,
                                          ll(pxy[e])))
        for n, fid, spec_, sxy, sz, is_open in ss:
            if is_open or len(sxy) < 2:
                continue
            th = float(tun.height(fid))
            mouths = tun.mouth(fid)
            s_ = arc(sxy)
            cov = raw_xy(sxy) - sz
            # steep
            if s_[-1] > 25:
                t = np.arange(0.0, s_[-1], 4.0)
                zz = np.interp(t, s_, sz)
                if len(zz) > 5:
                    gr = np.abs(zz[5:] - zz[:-5]) / 20.0
                    k = int(np.argmax(gr))
                    if gr[k] > STEEP:
                        res["steep"].append((round(100 * float(gr[k]), 1), fid[:16], spec.cls, round(s_[-1]),
                                             ll(np.array([np.interp(t[k], s_, sxy[:, 0]), np.interp(t[k], s_, sxy[:, 1])]))))
            if th >= TH_FULL - 0.05 and s_[-1] > 20:
                ds = np.diff(s_)
                w = np.r_[ds, 0] / 2 + np.r_[0, ds] / 2
                thin = float(np.sum(w[cov < th]) / max(s_[-1], 1e-6))
                if thin > 0.1:
                    res["thin"].append((round(100 * thin), fid[:16], spec.cls, round(s_[-1]), ll(sxy[len(sxy) // 2])))
            # mouths
            for e, (at, nb) in enumerate(((0, 1), (-1, -2))):
                if not mouths[e]:
                    continue
                p = sxy[at]
                tin = sxy[nb] - p
                tin = tin / max(float(np.hypot(*tin)), 1e-9)
                hw = spec_.width / 2
                zf = float(sz[at])
                # the floor's slope at the mouth (for the line out of it)
                k2 = min(len(sz) - 1, 3) if e == 0 else max(0, len(sz) - 4)
                slope = (float(sz[k2]) - zf) / max(float(abs(s_[k2] - s_[at])), 1e-6)
                inside = p + tin * 10.0
                cin = float(raw_xy(inside[None])[0] - (zf + slope * 10.0))
                if th >= TH_FULL - 0.05 and cin < th - 0.5:
                    res["portal_open"].append((round(cin, 1), fid[:16], spec_.cls, ll(p)))
                outs = np.array([p - tin * d for d in (10.0,)])
                cout = float(dem_xy(outs)[0] - (zf - slope * 10.0))
                if cout > TH_FULL + 2.0:
                    res["portal_deep"].append((round(cout, 1), fid[:16], spec_.cls, ll(p)))
                nrm = np.array([-tin[1], tin[0]])
                dd = np.arange(3.0, 15.01, 2.0)
                pts = np.concatenate([p - tin * d + nrm * o for o in (-hw * 0.7, 0.0, hw * 0.7) for d in dd[:, None]])
                lines_z = np.concatenate([zf - slope * dd for _o in range(3)])
                over = graded_xy(pts) - lines_z
                if float(np.nanmax(over)) > 1.0:
                    res["blocked"].append((round(float(np.nanmax(over)), 1), fid[:16], spec_.cls, ll(p)))
                zo = float(graded_xy((p - tin * 2.0)[None])[0])
                step = zo - (zf - slope * 2.0)
                if abs(step) > 0.5:
                    res["mouth_step"].append((round(step, 2), fid[:16], spec_.cls, ll(p)))
                # the entrance's record: the ground round the mouth over the floor's line (out of the tube: dout)
                hwt = hw + 1.2
                r1 = lambda v: [round(float(x), 1) for x in np.asarray(v).ravel()]     # noqa: E731
                din = np.array([0.0, 3.0, 6.0, 12.0, 20.0])
                dout = np.array([2.0, 5.0, 10.0, 20.0, 40.0])
                lat_o = np.array([1.0, 4.0, 8.0])
                gz_m = grader_floor(p)
                mouth_pts.append((p.copy(), -tin, fid, spec_.cls))
                res["mouths"].append({
                    "fid": fid, "cls": spec_.cls, "ll": ll(p), "out": [round(float(-tin[0]), 3), round(float(-tin[1]), 3)],
                    "hw": round(hw, 1), "th": round(th, 1), "zf": round(zf, 2), "slope": round(slope, 3),
                    "len": round(float(s_[-1])), "up": round(float(tun.portal_ups(fid)[e]), 1),
                    "cover_in": r1(raw_xy(np.array([p + tin * d for d in din])) - (zf + slope * din)),
                    "face_l": r1(graded_xy(np.array([p + nrm * (hwt + o) for o in lat_o])) - zf),
                    "face_r": r1(graded_xy(np.array([p - nrm * (hwt + o) for o in lat_o])) - zf),
                    "axis_out": r1(graded_xy(np.array([p - tin * d for d in dout])) - (zf - slope * dout)),
                    "dem_out": r1(dem_xy(np.array([p - tin * d for d in dout])) - (zf - slope * dout)),
                    "side_l": r1(graded_xy(np.array([p - tin * d + nrm * (hwt + 2.0) for d in dout]))
                                 - (zf - slope * dout)),
                    "side_r": r1(graded_xy(np.array([p - tin * d - nrm * (hwt + 2.0) for d in dout]))
                                 - (zf - slope * dout)),
                    "grader": None if gz_m is None else round(gz_m - zf, 2),
                    "blocked": round(float(np.nanmax(over)), 1), "step": round(step, 2),
                    "open": round(cin, 1), "deep": round(cout, 1)})
            # roads and decks over it
            lines = [gl for *_x, gl in ground_lines]
            for gi, cr in enumerate(roads._crossings_by([sxy], lines)):
                gfid, gcls, ghw, gl = ground_lines[gi]
                if gfid == pid.split(".")[0]:
                    continue
                for _pi, a, pt in cr:
                    if a < 2.0 or a > s_[-1] - 2.0:
                        continue
                    # (a junction at a ground road's end at the tunnel's own end: not over it)
                    if min(np.hypot(*(gl[0] - pt)), np.hypot(*(gl[-1] - pt))) < 2.0:
                        continue
                    top = float(np.interp(a, s_, sz)) + th
                    zr = float(graded_xy(np.asarray(pt)[None])[0])
                    if zr < top + 0.3:
                        res["breaks_upper"].append((round(zr - top, 2), fid[:16], spec_.cls, "road:" + gcls, ll(pt)))
            for dfid, dspec, dxy, dz, _l in decks:
                dxy = np.asarray(dxy, np.float64)[:, :2]
                ds_ = arc(dxy)
                for _pi, a, pt in roads._crossings_by([sxy], [dxy])[0]:
                    if a < 2.0 or a > s_[-1] - 2.0:
                        continue
                    # the deck's height at that point
                    k = int(np.argmin(np.hypot(*(dxy - pt).T)))
                    zd = float(np.asarray(dz)[k])
                    top = float(np.interp(a, s_, sz)) + th
                    if zd < top + 0.3 and zd > float(np.interp(a, s_, sz)) - 1.0:
                        res["breaks_upper"].append((round(zd - top, 2), fid[:16], spec_.cls,
                                                    "deck:" + dspec.cls, ll(pt)))
    if PROFILE is not None:
        for pid in sorted(subs):
            for n, fid, spec_, sxy, sz, is_open in sorted(subs[pid]):
                d = np.hypot(sxy[:, 0], sxy[:, 1])
                if d.min() > PROFILE:
                    continue
                th = None if is_open else float(tun.height(fid))
                print(f"PROFILE {fid} {spec_.cls} {'open' if is_open else 'th=%.1f' % th} mouths "
                      f"{None if is_open else tun.mouth(fid)} ups {None if is_open else tun.portal_ups(fid)}")
                s_ = arc(sxy)
                r, dm, gr = raw_xy(sxy), dem_xy(sxy), graded_xy(sxy)
                for q in range(0, len(sxy), max(1, len(sxy) // 25)):
                    print(f"   s {s_[q]:6.1f} floor {sz[q]:7.2f} raw {r[q]:7.2f} dem {dm[q]:7.2f} graded {gr[q]:7.2f}"
                          f"  {ll(sxy[q])}")
        if g is not None:          # the grading's own sections of the same tunnels (they must agree with the meshes')
            seen_g = set()
            for ck, f in cellkeys.items():
                if f is None or not len(f):
                    continue
                pl = g.store.planner_of_cell(cellobj[ck])
                for gfid, _sp, gxy, gz, kind in pl._tunnel_sections(f):
                    gla, glo, _h = pl.frame.enu_to_geodetic(np.asarray(gxy)[:, 0], np.asarray(gxy)[:, 1], 0.0)
                    ge, gn, _u = frame.geodetic_to_enu(gla, glo, 0.0)
                    gxy = np.c_[np.asarray(ge), np.asarray(gn)]
                    key = (gfid, round(float(gxy[0, 0]), 1), round(float(gxy[0, 1]), 1))
                    if key in seen_g or np.hypot(gxy[:, 0], gxy[:, 1]).min() > PROFILE:
                        continue
                    seen_g.add(key)
                    print(f"GRADER {gfid[:14]} kind {kind} L {float(arc(gxy)[-1]):.0f} z {float(gz[0]):.2f}.."
                          f"{float(gz[-1]):.2f} from {ll(gxy[0])} to {ll(gxy[-1])}")
        for gfid, gcls, ghw, gl in ground_lines:
            d = np.hypot(gl[:, 0], gl[:, 1])
            if d.min() < PROFILE * 0.5:
                gz, dz = graded_xy(gl), dem_xy(gl)
                print(f"GROUND {gfid[:14]} {gcls} " + " ".join(f"{a:.1f}/{b:.1f}" for a, b in zip(gz, dz)))
    if ENTRANCE:
        entrance_checks(res, mouth_pts, tun, cells, graded_xy, ll)
    for p, lst in joint_floor.items():
        if len(lst) >= 2:
            zs = [z for _p, z in lst]
            if max(zs) - min(zs) > 0.5:
                res["joint_step"].append((round(max(zs) - min(zs), 2), lst[0][0][:14], ll(np.asarray(p))))
    # ground over roads the map has on the ground
    for gfid, gcls, ghw, gl in ground_lines:
        if gcls not in UNTAGGED_CLASSES:
            continue
        s_ = arc(gl)
        if s_[-1] < 60:
            continue
        t = np.arange(0.0, s_[-1], 4.0)
        xy = np.stack([np.interp(t, s_, gl[:, 0]), np.interp(t, s_, gl[:, 1])], 1)
        if np.abs(xy).max(axis=1).min() > R:
            continue
        z = dem_xy(xy)
        f, _k = roads.tunnel_sections(t, z)
        over = (z - f) > 8.0
        best, run, start = 0.0, 0, 0
        for q in range(len(over)):
            if over[q]:
                if run == 0:
                    start = q
                run += 1
                if run * 4.0 > best:
                    best, bq = run * 4.0, (start + q) // 2
            else:
                run = 0
        if best >= 40.0:
            res["untagged"].append((round(best), round(float(np.max(z - f))), gfid[:14], gcls, ll(xy[bq])))
    return res


ENTRANCE = True        # --no-entrance: skip the built entrances' checks
ENTRANCE_CATS = ("gap", "proud", "bare")
ENT_HALF = 30.0        # m round a mouth: its holes' and mesh's box
GAP_MIN = 3            # hole squares
PROUD_M = 3.0
PROUD_X = 2.5          # m beyond the tube's wall
BARE_M = 5.0
BARE_REACH = 40.0


def _covered(pts, tri_xy, tri_z=None, over=None):
    """For points pts (n, 2): whether a triangle's plan (tri_xy (m, 3, 2)) contains it, and (tri_z (m, 3) and over
    (n,) given) the triangle lies over `over` there."""
    import numpy as np
    hit = np.zeros(len(pts), bool)
    if not len(tri_xy) or not len(pts):
        return hit
    a, b, c = tri_xy[:, 0], tri_xy[:, 1], tri_xy[:, 2]
    det = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
    ok = np.abs(det) > 1e-6
    a, b, c, det = a[ok], b[ok], c[ok], det[ok]
    tz = tri_z[ok] if tri_z is not None else None
    for i0 in range(0, len(pts), 256):
        q = pts[i0:i0 + 256]
        l1 = ((b[None, :, 1] - c[None, :, 1]) * (q[:, None, 0] - c[None, :, 0])
              + (c[None, :, 0] - b[None, :, 0]) * (q[:, None, 1] - c[None, :, 1])) / det[None]
        l2 = ((c[None, :, 1] - a[None, :, 1]) * (q[:, None, 0] - c[None, :, 0])
              + (a[None, :, 0] - c[None, :, 0]) * (q[:, None, 1] - c[None, :, 1])) / det[None]
        l3 = 1.0 - l1 - l2
        ins = (l1 >= -1e-6) & (l2 >= -1e-6) & (l3 >= -1e-6)
        if tz is not None:
            z = l1 * tz[None, :, 0] + l2 * tz[None, :, 1] + l3 * tz[None, :, 2]
            ins &= z > over[i0:i0 + 256, None]
        hit[i0:i0 + 256] = ins.any(1)
    return hit


def entrance_checks(res, mouth_pts, tun, cells, graded_xy, ll):
    """The built entrances' categories (the module's head: gap, proud, bare) and per mouth res["entrances"]."""
    import numpy as np
    import roadmesh
    import roads
    res["entrances"] = []
    done = []
    for p, out, fid, cls in mouth_pts:
        if any(np.hypot(*(p - q)) < 1.0 for q in done):
            continue
        done.append(p)
        cx, cy = (np.round(p / 2.0) * 2.0).tolist()
        ex = (cx - ENT_HALF - 16, cy - ENT_HALF - 16, cx + ENT_HALF + 16, cy + ENT_HALF + 16)
        try:
            chunks = roads._tunnel_chunks(tun, cells, ex)
            mesh = roadmesh.tunnel(chunks, np.zeros(3), [])
        except Exception as e:
            print("entrance mesh:", type(e).__name__, e)
            continue
        nn = int(2 * ENT_HALF)
        ax = cx - ENT_HALF + np.arange(nn + 1)
        ay = cy - ENT_HALF + np.arange(nn + 1)
        gx, gy = np.meshgrid(ax, ay)
        u = graded_xy(np.c_[gx.ravel(), gy.ravel()]).reshape(gx.shape)
        near_parts = tun.parts(cells, ex)
        u = roads.arch_raise(near_parts, tun.heights, gx.ravel(), gy.ravel(), u.ravel(), tun.mouths).reshape(gx.shape)
        hole = roads.tunnel_hole_mask(near_parts, u[:-1, :-1].copy(), cx, cy, 1.0, tun.heights, tun.mouths)
        if mesh is not None:
            Pm, Tm = np.asarray(mesh[0]), np.asarray(mesh[1])
            tri = Pm[Tm]
        else:
            Pm = np.zeros((0, 3))
            tri = np.zeros((0, 3, 3))
        # the floors: every tunnel chunk's ribbon (w / 2 + the tube's shoulder each side)
        floors = []
        for c in chunks:
            if c.get("tunnel") and c.get("deck"):
                n = np.asarray(c["n"], np.float64)
                n = n.copy()
                # (at a mouth the approach's road surface goes on over the terrain's holes there: 2 m of it)
                for e_, (a_, b_) in enumerate(((0, 1), (-1, -2))):
                    if (c.get("portal") or (False, False))[e_]:
                        t_ = n[a_, :2] - n[b_, :2]
                        n = np.insert(n, len(n) if e_ else 0, np.r_[n[a_, :2] + t_ / max(float(np.hypot(*t_)), 1e-9) * 2.0,
                                                                    n[a_, 2]], axis=0)
                floors.append((n[:, :2], float(c["w"]) / 2 + roadmesh.TUNNEL_SHOULDER, n[:, 2]))

        def floor_z(q):
            """The floor's height under each point on a tunnel floor's ribbon (NaN: none)."""
            got = np.full(len(q), np.nan)
            for xy, hw, zz in floors:
                a, b = xy[:-1], xy[1:]
                d = b - a
                L2 = np.maximum((d * d).sum(1), 1e-12)
                t = ((q[:, None, :] - a[None]) * d[None]).sum(2) / L2[None]
                inside = (t >= 0.0) & (t <= 1.0)
                pr = a[None] + np.clip(t, 0, 1)[..., None] * d[None]
                dist = np.hypot(*(q[:, None, :] - pr).transpose(2, 0, 1))
                ok = (dist <= hw + 0.3) & inside
                zs = np.where(ok, zz[:-1][None] + np.clip(t, 0, 1) * (zz[1:] - zz[:-1])[None], np.nan)
                with np.errstate(all="ignore"):
                    zmin = np.nanmin(np.where(ok, zs, np.inf), axis=1)
                hit = ok.any(1)
                got[hit] = np.fmin(got[hit], zmin[hit])
            return got
        rec = {"ll": ll(p), "fid": fid[:16], "cls": cls, "holes": 0, "gap": 0, "proud": 0.0, "bare": 0.0}
        if hole is not None and hole.any():
            iy, ix = np.nonzero(hole)
            q = np.c_[cx - ENT_HALF + ix + 0.5, cy - ENT_HALF + iy + 0.5]
            # each square's lowest corner (the hole's bottom edge)
            lo = np.minimum.reduce([u[iy, ix], u[iy + 1, ix], u[iy, ix + 1], u[iy + 1, ix + 1]])
            keep = np.hypot(*(q - p).T) <= ENT_HALF
            q, lo = q[keep], lo[keep]
            rec["holes"] = int(len(q))
            if len(q):
                # covered: the square lies on a floor at the floor's level (the opening's own floor), or some of the
                # mesh is over / under it no more than a metre below its lowest corner (a headwall's top, a cover's,
                # a wing's: what one sees through the hole); else the void under the terrain shows
                fz = floor_z(q)
                cov = np.isfinite(fz) & (lo <= fz + 1.0)
                cov |= _covered(q, tri[:, :, :2], tri[:, :, 2], lo - 1.0)
                rec["gap"] = int((~cov).sum())
                # where (the mouth's frame: x to the left of the way out, s out of the mouth, the square's lowest
                # corner over the floor there; the floor under it if any)
                lv = np.array([-out[1], out[0]])
                zf0 = float(np.nanmin(fz)) if np.isfinite(fz).any() else float("nan")
                rec["gap_at"] = [[round(float((v - p) @ lv), 1), round(float((v - p) @ out), 1),
                                  round(float(lz - zf0), 1), None if not np.isfinite(f_) else round(float(f_ - zf0), 1)]
                                 for v, lz, f_ in zip(q[~cov][:40], lo[~cov][:40], fz[~cov][:40])]
        # proud: the structure's vertices at the mouth's plane beyond the tube's wall over the ground there
        hw0 = th = None
        for c in chunks:
            if c.get("tunnel") and (c.get("deck") or c.get("tube_only")):
                n = np.asarray(c["n"], np.float64)
                if min(np.hypot(*(n[0, :2] - p)), np.hypot(*(n[-1, :2] - p))) < 1.0:
                    hw0 = float(c["w"]) / 2 + roadmesh.TUNNEL_SHOULDER
                    th = float(c.get("tunnel_h") or roadmesh.TUNNEL_H)
                    break
        if hw0 is not None and len(tri):
            lv = np.array([-out[1], out[0]])
            sv = (Pm[:, :2] - p) @ out
            xv = (Pm[:, :2] - p) @ lv
            sel = (sv > -1.5) & (sv < 2.0) & (np.abs(xv) >= hw0 + PROUD_X) & (np.abs(xv) < hw0 + PROUD_X + 4.0)
            if sel.any():
                # (over the ground in front and the ground it holds back behind it, the higher)
                gz = np.maximum.reduce([graded_xy(Pm[sel, :2] - out[None] * b) for b in (0.0, 1.5, 3.0, 5.0)])
                k = int(np.argmax(Pm[sel, 2] - gz))
                rec["proud"] = round(float(Pm[sel, 2][k] - gz[k]), 1)
                rec["proud_at"] = [round(float(xv[sel][k]), 1), round(float(sv[sel][k]), 1), round(float(Pm[sel, 2][k]), 1),
                                   round(float(gz[k]), 1)]
            # bare: the tube's rows within BARE_REACH of the mouth, thin ground and nothing over the crown
            if th >= TH_FULL - 0.05:
                bare = 0.0
                for c in chunks:
                    if not (c.get("tunnel") and (c.get("deck") or c.get("tube_only"))) or c.get("no_tube"):
                        continue
                    if float(c.get("tunnel_h") or roadmesh.TUNNEL_H) < TH_FULL - 0.05:
                        continue
                    n = np.asarray(c["n"], np.float64)
                    sl = np.r_[0.0, np.cumsum(np.hypot(*np.diff(n[:, :2], axis=0).T))]
                    t = np.arange(0.0, sl[-1], 1.0)
                    if not len(t):
                        continue
                    q = np.c_[np.interp(t, sl, n[:, 0]), np.interp(t, sl, n[:, 1])]
                    zq = np.interp(t, sl, n[:, 2])
                    near = np.hypot(*(q - p).T) <= BARE_REACH
                    if not near.any():
                        continue
                    q, zq = q[near], zq[near]
                    thin = graded_xy(q) - zq < th + 0.5
                    if thin.any():
                        cov = _covered(q[thin], tri[:, :, :2], tri[:, :, 2], zq[thin] + th + 0.05)
                        bare += float((~cov).sum())
                rec["bare"] = bare
        res["entrances"].append(rec)
        if rec["gap"] >= GAP_MIN:
            res["gap"].append((rec["gap"], fid[:16], cls, rec["ll"]))
        if rec["proud"] > PROUD_M:
            res["proud"].append((rec["proud"], fid[:16], cls, rec["ll"]))
        if rec["bare"] > BARE_M:
            res["bare"].append((rec["bare"], fid[:16], cls, rec["ll"]))


def report(res):
    name, lat, lon, R = res["place"]
    print(f"{name} {lat},{lon} R {R:.0f}: {res['parts']} tunnel parts ({res['length_m'] / 1000:.1f} km), "
          f"{res['built']} built")
    for k in ("left_out", "opened", "joint_rise", "joint_step", "steep", "thin", "portal_open", "portal_deep",
              "blocked", "mouth_step", "breaks_upper", "untagged") + ENTRANCE_CATS:
        if k not in res:
            continue
        v = res[k]
        if k in ("mouth_step", "breaks_upper"):
            worst = sorted(v, key=lambda x: x[0] if k == "breaks_upper" else -abs(x[0]))[:3]
        else:
            worst = sorted(v, key=lambda x: -x[0] if isinstance(x[0], (int, float)) else 0)[:3]
        print(f"  {k:13s} {len(v):4d}  {worst}")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
