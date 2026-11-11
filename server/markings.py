"""Road markings by country (the user, 2026-10-28: "revamp the road markings system ... as accurate and detailed as
possible ... official government resources on how they mark lanes"): line colours, widths and dash patterns from the
national manuals, summed up in docs/research/2026-10-28-road-markings-standards.md (MUTCD 11th ed., TSRGD / Traffic Signs
Manual ch. 5, RMS, IISR, MLIT, AS 1742.2), and the broken lines cut into real dashes (earthfast.dash_pieces) instead of
the stock dashed decal (3 m of paint and 9 m of a grey smear, its cycle restarting at every tile's edge).

style(country, lon) -> a Style; roads.extras draws the lines with it. Pure.
"""
from collections import namedtuple

import numpy as np

import fast

# colour: the centre line's material; widths in m; patterns (dash, gap) in m. centre_rural / centre_urban: the centre
# line of a two-way road (None: solid); lane_* the lines between lanes of one direction; edge: the edge line's width;
# left_edge: the material of a one-way carriageway's left edge (the Americas' yellow) or None (white like the right);
# double_centre_urban: a town's two-way road gets a double solid centre line (the US practice on arterials) instead of
# a broken one; double_gap: the space between the two lines of a double line; edge_pattern: a broken edge line (dash,
# gap) off the main roads; warning: the broken line in place of a double solid one on a narrow road; w_urban: a line's
# width in town (else w). fork: double_barrier: a no-overtaking centre line (OpenStreetMap overtaking=no) is drawn as two
# solid lines; double_multilane: so is the solid centre line of an undivided road of four lanes or more.
Style = namedtuple("Style", "centre w w_motorway edge edge_motorway centre_rural centre_urban lane_rural lane_urban "
                            "lane_motorway left_edge double_centre_urban double_gap min_centre_m edge_classes ladder "
                            "edge_pattern warning w_urban double_barrier double_multilane",
               defaults=(None, False, None, None, None, False, False))

_US = Style(centre="line_yellow", w=0.12, w_motorway=0.15, edge=0.12, edge_motorway=0.15,
            centre_rural=(3.05, 9.14), centre_urban=(3.05, 9.14), lane_rural=(3.05, 9.14), lane_urban=(3.05, 9.14),
            lane_motorway=(3.05, 9.14), left_edge="line_yellow", double_centre_urban=True, double_gap=0.12,
            min_centre_m=4.9,
            # edge lines on arterials (MUTCD 3B.10: rural arterials 20 ft and wider); local and collector roads mostly
            # have none (PEMA 6-inch aerials, Lancaster County, 2026-10-28: tertiary roads with a centre line only)
            edge_classes=frozenset({"motorway", "trunk", "primary", "secondary"}), ladder=True,
            double_barrier=True, double_multilane=True)     # fork: (was hard-coded for yellow centres in roads.extras)
_CA = _US._replace(centre_rural=(3.0, 6.0), centre_urban=(3.0, 6.0), lane_rural=(3.0, 6.0), lane_urban=(3.0, 6.0),
                   lane_motorway=(3.0, 6.0))
_GB = Style(centre="line_white", w=0.10, w_motorway=0.15, edge=0.15, edge_motorway=0.20,
            centre_rural=(3.0, 6.0), centre_urban=(2.0, 4.0), lane_rural=(2.0, 7.0), lane_urban=(1.0, 5.0),
            lane_motorway=(2.0, 7.0), left_edge=None, double_centre_urban=False, double_gap=0.175, min_centre_m=5.5,
            double_barrier=True)                            # fork: (was hard-coded for GB in roads.extras)
_DE = Style(centre="line_white", w=0.12, w_motorway=0.15, edge=0.12, edge_motorway=0.30,
            centre_rural=(6.0, 12.0), centre_urban=(3.0, 6.0), lane_rural=(6.0, 12.0), lane_urban=(3.0, 6.0),
            lane_motorway=(6.0, 12.0), left_edge=None, double_centre_urban=False, double_gap=0.12, min_centre_m=5.5)
_FR = Style(centre="line_white", w=0.12, w_motorway=0.15, edge=0.15, edge_motorway=0.225,
            centre_rural=(3.0, 10.0), centre_urban=(1.5, 5.0), lane_rural=(3.0, 10.0), lane_urban=(1.5, 5.0),
            lane_motorway=(3.0, 10.0), left_edge=None, double_centre_urban=False, double_gap=0.12, min_centre_m=5.5)
# Sweden. Lengths: TSFS 2010:171 2 kap. 3, 6, 8 §§ (M1 3 m, M2 1 m, M3 9 m); gaps: Vägmärkesförordningen 2007:90 4 kap.
# (M1 1:3, in town 1:1; M2 1:2; M3 3:1); double lines: their gap the line's width (18, 21 §§). Widths and where: VGU 2024
# 12.2.1, tables 12.4-12.9 (TRVINFRA-00396, remiss version 0.9, 2024-07-01): rural 0.15, town 0.10, motorway edge solid
# 0.30. Its 0.10 on rural roads under 2000 vehicles a day is not here: no traffic counts. Below 5.5 m no centre line.
_SE = Style(centre="line_white", w=0.15, w_motorway=0.15, edge=0.15, edge_motorway=0.30,
            centre_rural=(3.0, 9.0), centre_urban=(3.0, 3.0), lane_rural=(3.0, 9.0), lane_urban=(3.0, 3.0),
            lane_motorway=(3.0, 9.0), left_edge=None, double_centre_urban=False, double_gap=0.15, min_centre_m=5.5,
            edge_pattern=(1.0, 2.0), warning=(9.0, 3.0), w_urban=0.10,
            double_barrier=True, double_multilane=True)     # fork: (was hard-coded for SE in roads.extras)
MIN_LANE_M = 2.75           # no line leaving a lane narrower than the least VGU 2024 allows (K233046; K233040: under 5.5 m none)
WARNING_BELOW_M = 6.5       # a narrower road has a warning line, not a double solid one (TSFS 2010:171 2 kap. 7 §)
# a 2+1 road (VGU 2024 table 12.5, K237686): no centre line, a solid left edge line each side of the barrier, its outer
# edge at least 0.20 m from it (the model's posts are 0.04 m a side), and a solid right edge line
BARRIER_LINE_W, BARRIER_LINE_M, MOTESFRI_EDGE_W = 0.30, 0.04 + 0.20 + 0.15, 0.20
_NL = _DE._replace(centre_rural=(3.0, 9.0), lane_rural=(3.0, 9.0), lane_motorway=(3.0, 9.0), edge_motorway=0.20)
_JP = Style(centre="line_white", w=0.15, w_motorway=0.15, edge=0.15, edge_motorway=0.20,
            centre_rural=(5.0, 5.0), centre_urban=(5.0, 5.0), lane_rural=(6.0, 9.0), lane_urban=(5.0, 5.0),
            lane_motorway=(8.0, 12.0), left_edge=None, double_centre_urban=False, double_gap=0.12, min_centre_m=5.5)
# fork: New Zealand keeps the line paint it had; Australia (_AU) has its own below
_NZ = Style(centre="line_white", w=0.10, w_motorway=0.15, edge=0.12, edge_motorway=0.15,
            centre_rural=(3.0, 9.0), centre_urban=(3.0, 9.0), lane_rural=(3.0, 9.0), lane_urban=(3.0, 9.0),
            lane_motorway=(3.0, 9.0), left_edge=None, double_centre_urban=False, double_gap=0.12, min_centre_m=5.5)
# fork: Australia, AS 1742.2 as TfNSW / IPWEA / TMR MRS45 draw it (docs/research/2026-10-28-road-markings-standards.md
# 4.2, fork/QLD_REFERENCE.md): all white; separation and lane lines 0.10 m, 3 m / 9 m (kept: _NZ's); edge lines 0.15 m
# (QLD's; 4.2 gives 0.12 general and 0.15 freeway; motorway stays 0.15); barrier lines (no overtaking, or an undivided
# road of four lanes or more) two solid 0.10 m lines 0.10 m apart (BB / BS: "double solid ... about 0.08 m lines with
# a 0.08 m gap"; MRS45 52127: two 100 mm lines)
_AU = _NZ._replace(edge=0.15, double_gap=0.10, double_barrier=True, double_multilane=True)
# the manuals' fallback: white centre lines 3 / 6 in towns and 6 / 12 out of them (or yellow where the country paints
# them yellow), lanes 3 / 9
_WHITE = Style(centre="line_white", w=0.12, w_motorway=0.15, edge=0.15, edge_motorway=0.20,
               centre_rural=(6.0, 12.0), centre_urban=(3.0, 6.0), lane_rural=(3.0, 9.0), lane_urban=(3.0, 9.0),
               lane_motorway=(3.0, 9.0), left_edge=None, double_centre_urban=False, double_gap=0.12, min_centre_m=5.5)
_YELLOW = _WHITE._replace(centre="line_yellow", double_barrier=True, double_multilane=True)   # fork: (was hard-coded)

STYLES = {"US": _US, "CA": _CA, "GB": _GB, "IE": _GB, "IM": _GB, "JE": _GB, "GG": _GB, "DE": _DE, "AT": _DE,
          "CH": _DE, "LI": _DE, "LU": _DE, "FR": _FR, "MC": _FR, "BE": _FR, "NL": _NL, "JP": _JP, "AU": _AU, "NZ": _NZ,
          "SE": _SE}
# where centre lines are yellow (outside the US and Canada): the Americas, Norway, Finland, South Korea, China, Taiwan,
# Thailand (research table 4.4)
YELLOW_CENTRE = {"MX", "GT", "BZ", "HN", "SV", "NI", "CR", "PA", "CU", "DO", "HT", "PR", "JM", "BS", "TT", "CO", "VE",
                 "EC", "PE", "BO", "CL", "AR", "UY", "PY", "BR", "GY", "SR", "NO", "FI", "KR", "CN", "TW", "TH"}


def style(country=None, lon=None):
    """The marking Style of a country (ISO 3166-1 alpha-2); unknown: by longitude (the Americas yellow, else white)."""
    c = str(country or "").upper()
    if c in STYLES:
        return STYLES[c]
    if c in YELLOW_CENTRE:
        return _YELLOW
    if not c and lon is not None and -170.0 <= float(lon) <= -30.0:
        return _US
    return _WHITE


def dashes(pts, pattern, phase=0.0):
    """The dashes of a broken line along pts (k, 3): [(m, 3) arrays], pattern (dash, gap) m, its cycle starting phase
    m before pts[0] (earthfast.dash_pieces)."""
    pts = np.ascontiguousarray(np.asarray(pts, np.float64)[:, :3] if np.shape(pts)[1] >= 3 else
                               np.c_[np.asarray(pts, np.float64)[:, :2], np.zeros(len(pts))])
    if len(pts) < 2:
        return []
    fast.require(93, "dash_pieces")
    P, starts = fast._ef.dash_pieces(pts, float(pattern[0]), float(pattern[1]), float(phase))
    return [P[a:b] for a, b in zip(starts[:-1], starts[1:]) if b - a >= 2]


def _count(v):
    import roadlanes
    return roadlanes.count(v, 0)


def _vals(v):
    return [p.strip() for p in str(v).split("|")] if v not in (None, "") else None


def _no_change(a, b):
    """change:lanes values of two neighbouring lanes (a left of b, in their travel direction): is crossing between
    them not allowed? (only_left / not_right on a, only_right / not_left on b, or no)"""
    a, b = (a or "yes"), (b or "yes")
    return a in ("no", "not_right", "only_left") or b in ("no", "not_left", "only_right")


_BUS_LANE = {"designated", "yes"}


def _bus_flags(osm, n, *keys):
    """Which of n lanes (left to right in their travel direction) are bus lanes, from the first of keys whose value
    has n entries (bus:lanes / psv:lanes, designated or yes), else None."""
    for key in keys:
        vals = _vals(osm.get(key))
        if vals is not None and len(vals) == n:
            return [v.lower() in _BUS_LANE for v in vals]
    return None


def _bus_boundary(flags, a, b):
    """A solid line where a bus lane meets a general lane (the two flags differ)."""
    return flags is not None and bool(flags[a]) != bool(flags[b])


def lane_plan(osm, two_way, drive, lanes, left_hand=False):
    """The lines across a carriageway from OpenStreetMap's lane tags (osm: {key: value} of railtags.LANE_COLUMNS, may
    be empty): [(offset m, left of the line's direction, positive left; role, solid)]. role: "centre" (between the
    directions), "twltl" (a two-way turn lane's edge: solid outside, broken inside), "lane" (between lanes of one
    direction). solid: for "lane" True / False; for "centre" None (broken), "double", "solid_right" / "solid_left" (the
    side that may not pass); for "twltl" the through lanes' side (+1 left, -1 right). Without lane tags: None
    (roads.extras' own count). Pure."""
    osm = {k: v for k, v in (osm or {}).items() if v not in (None, "")}
    if not osm:
        return None
    if not two_way:
        n = lanes
        ch = _vals(osm.get("change:lanes"))
        bus = _bus_flags(osm, n, "bus:lanes", "psv:lanes")
        lw = drive / max(n, 1)
        out = []
        for k in range(1, n):
            solid = (ch is not None and len(ch) == n and _no_change(ch[k - 1], ch[k])
                     or _bus_boundary(bus, k - 1, k))
            # lanes left to right in travel: lane k's left edge at -drive/2 + k * lw from the right? (offsets are left
            # positive: the leftmost lane starts at +drive/2)
            out.append((drive / 2 - k * lw, "lane", solid))
        return out
    import roadlanes
    # the mapped total first (OpenStreetMap's lanes), then the road's count, then lanes:forward + backward + both_ways
    # where the road's is under two (a narrow road's 1 + 1: its width alone gives one lane; a 2 + 1 road's "lanes" 3)
    f, b = roadlanes.count(osm.get("lanes:forward")), roadlanes.count(osm.get("lanes:backward"))
    total = roadlanes.count(osm.get("lanes")) or (lanes if (lanes or 0) >= 2 else None) or \
        (f + b + (roadlanes.count(osm.get("lanes:both_ways"), 0) or 0) if f and b else None)
    if not total or total < 2:
        return None
    fwd, bwd, both = roadlanes.directions(total, osm)
    n = fwd + bwd + both
    lw = drive / max(n, 1)
    # right-hand traffic: the backward lanes on the left of the way's direction (offsets positive), then the turn lane,
    # then the forward lanes; left-hand traffic the other way round
    left_n, right_n = (bwd, fwd) if not left_hand else (fwd, bwd)
    out = []
    edge = drive / 2
    back, fore = ("backward", "forward") if not left_hand else ("forward", "backward")
    ch_l = _vals(osm.get("change:lanes:" + back))
    ch_r = _vals(osm.get("change:lanes:" + fore))
    bus_l = _bus_flags(osm, left_n, "bus:lanes:" + back, "psv:lanes:" + back, "bus:lanes", "psv:lanes")
    bus_r = _bus_flags(osm, right_n, "bus:lanes:" + fore, "psv:lanes:" + fore, "bus:lanes", "psv:lanes")
    # the left-side lanes (travel against the way in right-hand traffic: their left to right is the way's right to left)
    for k in range(1, left_n):
        off = edge - k * lw
        solid = (ch_l is not None and len(ch_l) == left_n and _no_change(ch_l[left_n - k - 1], ch_l[left_n - k])
                 or _bus_boundary(bus_l, left_n - k - 1, left_n - k))
        out.append((off, "lane", solid))
    c_left = edge - left_n * lw
    no_pass = osm.get("overtaking") == "no"
    pass_f = osm.get("overtaking:forward", osm.get("overtaking"))
    pass_b = osm.get("overtaking:backward", osm.get("overtaking"))
    if both:
        out.append((c_left, "twltl", 1))                # (how: the through lanes' side, +1 left)
        out.append((c_left - both * lw, "twltl", -1))
    else:
        # the centre: solid where nobody may pass, solid on one side where one direction may not (the forward side is
        # the right of the line in right-hand traffic)
        out.append((c_left, "centre", ("double" if no_pass or (pass_f == "no" and pass_b == "no") else
                                       "solid_right" if pass_f == "no" else "solid_left" if pass_b == "no" else
                                       None)))
    c_right = c_left - both * lw
    for k in range(1, right_n):
        off = c_right - k * lw
        solid = (ch_r is not None and len(ch_r) == right_n and _no_change(ch_r[k - 1], ch_r[k])
                 or _bus_boundary(bus_r, k - 1, k))
        out.append((off, "lane", solid))
    return out


GORE_MAJOR = {"motorway", "trunk"}
GORE_MIN_M, GORE_MAX_M = 0.4, 9.0       # the gap between the two edges where the gore is painted (beyond: the nose)
GORE_STEP_M = 12.0                      # m between stripes (MUTCD practice on freeways: 20-50 ft)
GORE_REACH_M = 300.0                    # m of the ramp looked along from the shared end
GORE_W = 0.30                           # stripe width (MUTCD 3B.25: at least 12 in at 45 mph and over)
GORE_CHANNEL_W = 0.20                   # the channelizing line (wide: 8 in)


def _nearest(poly, p):
    a, b = poly[:-1, :2], poly[1:, :2]
    ab = b - a
    L2 = np.maximum((ab * ab).sum(1), 1e-12)
    t = np.clip(((p[:2] - a) * ab).sum(1) / L2, 0, 1)
    q = a + t[:, None] * ab
    d = np.hypot(*(q - p[:2]).T)
    k = int(np.argmin(d))
    return q[k], float(d[k])


def _resample(pts, step):
    seg = np.hypot(*np.diff(pts[:, :2], axis=0).T)
    s = np.r_[0.0, np.cumsum(seg)]
    if s[-1] < step:
        return pts.copy(), s
    t = np.arange(0.0, s[-1], step)
    return np.stack([np.interp(t, s, pts[:, c]) for c in range(3)], 1), t


def gores(roads_):
    """The painted gores where a one-way ramp (spec.link) leaves or joins a motorway / trunk carriageway at a shared
    end: [(kind, width, line (2+, 3))] with kind "stripe" (an arm of a chevron across the wedge between the two edges,
    every GORE_STEP_M where the gap is GORE_MIN_M .. GORE_MAX_M), "channel" (the ramp's edge along it, wide) and "fill"
    (width: half widths per point of the wedge's middle line: it is paved) and "channel" (the ramp's edge along it, wide). roads_:
    build_tile's (fid, spec, centreline (k, 3), lon). Pure."""
    majors = [(fid, sp, np.asarray(p, np.float64)) for fid, sp, p, _ in roads_
              if sp.cls in GORE_MAJOR and sp.oneway and not getattr(sp, "link", False) and len(p) >= 2]
    out = []
    if not majors:
        return out
    for fid, sp, p, _ in roads_:
        if not getattr(sp, "link", False) or not sp.oneway or len(p) < 2:
            continue
        p = np.asarray(p, np.float64)
        for end in (0, -1):
            e = p[end]
            hit = None
            for mf, ms, mp in majors:
                if mf == fid:
                    continue
                _, d = _nearest(mp, e)
                if d < 1.5 and (hit is None or d < hit[2]):
                    hit = (ms, mp, d)
            if hit is None:
                continue
            ms, mp, _ = hit
            q, s = _resample(p if end == 0 else p[::-1], 1.0)
            q = q[s <= GORE_REACH_M]
            gaps, sides = [], []
            for x in q:
                m, d = _nearest(mp, x)
                gaps.append(d - ms.width / 2 - sp.width / 2)
                sides.append((x[:2] - m) / max(d, 1e-9))
            gaps = np.asarray(gaps)
            inside = np.flatnonzero((gaps >= GORE_MIN_M) & (gaps <= GORE_MAX_M))
            if len(inside) < 3:
                continue
            k0 = int(inside[0])
            k1 = k0
            while k1 + 1 < len(q) and GORE_MIN_M <= gaps[k1 + 1] <= GORE_MAX_M:
                k1 += 1
            eL = np.array([np.r_[q[k][:2] - sides[k] * sp.width / 2, q[k][2]] for k in range(k0, k1 + 1)])
            out.append(("channel", GORE_CHANNEL_W, eL))
            # the wedge itself, paved (surface.photo_roads / paint_roads): its middle line and half width per point
            mids = np.array([np.r_[q[k][:2] - sides[k] * (sp.width / 2 + gaps[k] / 2), q[k][2]]
                             for k in range(max(k0 - 3, 0), k1 + 1)])
            out.append(("fill", np.array([gaps[k] / 2 + 0.5 for k in range(max(k0 - 3, 0), k1 + 1)]), mids))
            # chevrons (MUTCD 3B.25): a V across the wedge, its point toward the traffic coming at it: at an exit
            # (the ramp starts at the shared end) toward that end, at an entrance away from it
            up = -1.0 if end == 0 else 1.0
            for k in range(k0, k1 + 1, int(GORE_STEP_M)):
                a = np.r_[q[k][:2] - sides[k] * sp.width / 2, q[k][2]]
                m, _ = _nearest(mp, q[k])
                b = np.r_[m + sides[k] * ms.width / 2, q[k][2]]
                kk = min(k + 1, len(q) - 1)
                t = q[kk][:2] - q[max(kk - 1, 0)][:2]
                t = t / max(float(np.hypot(*t)), 1e-9)
                apex = 0.5 * (a + b)
                apex[:2] += up * t * float(np.hypot(*(a - b)[:2])) / 2      # (45 degrees each arm)
                out.append(("stripe", GORE_W, np.stack([a, apex])))
                out.append(("stripe", GORE_W, np.stack([apex, b])))
    return out


# Sweden's give-way line M14: "ska utföras där märke B1, väjningsplikt, är uppsatt", triangles 0.70 m high on a base of
# 0.50 m, 0.50 m apart (TSFS 2010:171 4 kap. 5, 7 §§), at least 1.0 m from the crossing road's edge (VGU 2024 12.2.1.4.2)
M14_H, M14_BASE, M14_GAP, M14_KERB_M = 0.70, 0.50, 0.50, 1.0
M14_LOOK_M = 40.0       # m along an arm a crosswalk moves the line
M14_CROSSWALK_M = 1.0   # m it stands before a crosswalk (junctions.STOP_GAP, the stop line's)
CAR_ROOM_M = 5.0        # m from the line to a crossing behind it, for a car between them: with less the line goes before
                        # the crossing (VGU 2024 12.2.1.4.1, 12.2.1.4.2 råd, both lines)
# M13: 0.40 m wide (TSFS 2010:171 4 kap. 4 §), at signals at least 2.0 m before a crossing (VGU 2024 K237740)
M13_W, M13_SIGNAL_CROSSWALK_M = 0.40, 2.0
# Germany (docs/research/2026-10-28-road-markings-standards.md 3.1): the Haltlinie Z 294 0.50 m wide (RMS 4.6), at a 206
# (VwV-StVO zu Zeichen 206 Rn. 4); the Wartelinie Z 341 a Breitstrich 0.50 m wide, its dashes 0.50 m with 0.25 m gaps
# (RMS 4.7.2), where a 205 stands (VwV-StVO zu Zeichen 341 Rn. 1)
Z294_W = 0.50
Z341_W, Z341_DASH, Z341_GAP = 0.50, 0.50, 0.25


def _way_in(line, s, at, spec, left_hand):
    """(point on the centreline, unit vector of the way in, unit vector to its kerb, (lo, hi) m along that from the
    centreline of the half of the road coming in) at m along an arm's centreline line (s: its stations)."""
    c = np.array([np.interp(at, s, line[:, k]) for k in range(3)])
    q = np.array([np.interp(at + 1.0, s, line[:, k]) for k in range(2)])
    t = (c[:2] - q) / max(float(np.hypot(*(c[:2] - q))), 1e-6)
    n = np.array([t[1], -t[0]]) * (-1.0 if left_hand else 1.0)
    half = spec.width / 2
    return c, t, n, ((0.3 - half, half - 0.3) if spec.oneway else (0.2, half - 0.3))


def _past_crossing(along, st, crosswalks, spec, at, gap, room):
    """at moved past the crosswalks on an arm (along, st: its points and stations up to M14_LOOK_M) that have less than
    room m between them and the line, to gap m past them."""
    start = at
    for cw in crosswalks:
        if cw.get("stop"):
            continue
        d = np.hypot(*(along[:, :2] - 0.5 * (cw["a"][:2] + cw["b"][:2])).T)
        i = int(np.argmin(d))
        if d[i] < spec.width / 2 + 1.0 and float(st[i]) - cw["w"] / 2 - start < room:
            at = max(at, float(st[i]) + cw["w"] / 2 + gap)
    return at


def stop_lines(arms, crosswalks, mapped=None, left_hand=False, w=M13_W):
    """(stop lines [{"a", "b", "w", "stop": True}], line cuts {fid: [(junction point, u, m along)]}) of arms [(fid, spec,
    centreline from the junction, half the crossing road's width, what)] (furniture.give_way_arms): M13 across the lanes
    coming in, where the map has it (mapped: (n, 2), secrossings.nodes) or else at least 1.0 m from the crossing road's
    edge (VGU 2024 12.2.1.4.1), and at signals before any crossing. The road's lines end at it. w: the line's width
    (Germany's Haltlinie: Z294_W)."""
    import furniture
    out, cuts = [], {}
    for fid, spec, line, wide, what in arms:
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(line[:, :2], axis=0).T))]
        if s[-1] < 1.0:
            continue
        st = np.arange(0.0, min(s[-1], M14_LOOK_M), 1.0)
        along = np.stack([np.interp(st, s, line[:, k]) for k in range(3)], 1)
        at = wide + M14_KERB_M
        if mapped is not None and len(mapped):      # (within half the arm: further up it is the next junction's)
            d = np.hypot(along[:, None, 0] - mapped[:, 0], along[:, None, 1] - mapped[:, 1])
            on = d.min(0) < spec.width / 2
            near = st[d[:, on].argmin(0)] if on.any() else st[:0]
            near = near[near <= min(s[-1] / 2, wide + furniture.SIGNAL_NEAR_M)]
            if len(near):
                at = max(at, float(near.min()))
        signal = what == furniture.G_SIGNAL
        at = _past_crossing(along, st, crosswalks, spec, at, M13_SIGNAL_CROSSWALK_M if signal else M14_CROSSWALK_M,
                            float("inf") if signal else CAR_ROOM_M)
        if at + 2.0 > s[-1]:
            continue                        # (a stub: nowhere to stop)
        c, _, n, (lo, hi) = _way_in(line, s, at, spec, left_hand)
        out.append({"a": np.r_[c[:2] + n * lo, c[2]], "b": np.r_[c[:2] + n * hi, c[2]], "w": w, "stop": True})
        u = (along[1, :2] - along[0, :2]) if len(along) > 1 else line[1, :2] - line[0, :2]
        cuts.setdefault(fid, []).append((line[0, :2].copy(), u / max(float(np.hypot(*u)), 1e-6), at))
    return out, cuts


def wait_lines(arms, crosswalks, left_hand=False):
    """(dashes [(4, 3) quads], line cuts {fid: [(junction point, u, m along)]}) of Germany's Wartelinie Z 341 on arms
    [(fid, spec, centreline from the junction, half the crossing road's width)] (designs.line_arms: the 205 arms and a
    roundabout's entries): where stop_lines stands a line (at least 1.0 m from the crossing road's edge, past a
    crosswalk with no room for a car before it; a stub none), across the half coming in, whole 0.50 m dashes 0.25 m
    apart (markings.dashes) in the middle of it, each Z341_W wide. The road's lines end at it, as at a stop line."""
    import furniture
    lines, cuts = stop_lines([(fid, spec, line, wide, furniture.G_STOP) for fid, spec, line, wide in arms], crosswalks,
                             None, left_hand, Z341_W)
    pitch = Z341_DASH + Z341_GAP
    out = []
    for ln in lines:
        a, b = np.asarray(ln["a"], np.float64), np.asarray(ln["b"], np.float64)
        d = b - a
        span = float(np.hypot(*d[:2]))
        count = int((span + Z341_GAP) // pitch)
        if count < 1:
            continue
        n = d / span                                        # (across the lane: d's z is 0)
        t = np.array([-n[1], n[0], 0.0]) * Z341_W / 2      # (along the road: the Breitstrich's half width)
        spare = (span - (count * pitch - Z341_GAP)) / 2
        for p in dashes(np.stack([a + n * spare, b - n * spare]), (Z341_DASH, Z341_GAP)):
            out.append(np.stack([p[0] + t, p[-1] + t, p[-1] - t, p[0] - t]))
    return out, cuts


def give_way_lines(arms, crosswalks, left_hand=False):
    """(triangles [(3, 3) array], indices of the crosswalks rows that go) of arms [(fid, spec, centreline from the
    junction, half the crossing road's width)] (furniture.give_way_arms): across the lanes coming in, points to the
    drivers; past a crosswalk on the arm, and in place of its stop line (M13 is for B2 and signals: 4 kap. 1, 3 §§)."""
    teeth, gone = [], set()
    pitch = M14_BASE + M14_GAP
    for _, spec, line, wide in arms:
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(line[:, :2], axis=0).T))]
        if s[-1] < 1.0:
            continue
        st = np.arange(0.0, min(s[-1], M14_LOOK_M), 1.0)
        along = np.stack([np.interp(st, s, line[:, k]) for k in range(3)], 1)
        at = wide + M14_KERB_M
        for j, cw in enumerate(crosswalks):
            d = np.hypot(*(along[:, :2] - 0.5 * (cw["a"][:2] + cw["b"][:2])).T)
            i = int(np.argmin(d))
            if cw.get("stop") and d[i] < spec.width / 2 + 1.0:
                gone.add(j)
                at = max(at, float(st[i]))
        at = _past_crossing(along, st, crosswalks, spec, at, M14_CROSSWALK_M, CAR_ROOM_M)
        if at + M14_H + 2.0 > s[-1]:
            continue                        # (a stub: nowhere to stop)
        c, t, n, (lo, hi) = _way_in(line, s, at, spec, left_hand)
        count = int((hi - lo + M14_GAP) // pitch)
        for k in range(count):
            mid = c[:2] + n * ((lo + hi) / 2 + (k - (count - 1) / 2) * pitch)
            tri = [mid + n * M14_BASE / 2, mid - n * M14_BASE / 2, mid - t * M14_H]
            teeth.append(np.c_[np.array(tri), np.full(3, c[2])])
    return teeth, gone


# M20 at 7.5 m long, a road above 60 km/h (TSFS 2010:171 5 kap. 4 §; VGU 2024 figure 12.2.1.5.3-2, whose dimensions give
# its tail 2.4 m straight, 0.30 m wide and 1.05 m off the tip's line, and a head 1.5 m wide, 2.0 m long; its arc, R 7.5,
# as three straights): m along from the tail, m to the left, the tip at (7.5, 0)
M20_SHAFT = ((0.0, 1.20), (2.40, 1.20), (3.40, 1.13), (4.20, 0.98), (4.83, 0.78))
M20_HEAD = ((5.50, 1.16), (4.16, 0.39), (7.50, 0.0))
M20_LANE_M = 0.6            # m to the left of the tip's line the arrow's middle is: on the middle of its lane


def m20(tip, t):
    """Polygons [(k, 3)] of an M20 arrow with its tip at tip (x, y) on the lane's middle, travel direction t, turning
    right (the lane on the right goes on: VGU's figure 12.2.1.5.3-1)."""
    tip, t = np.asarray(tip, np.float64)[:2], np.asarray(t, np.float64)[:2]
    n = np.array([-t[1], t[0]])
    place = lambda pts: np.c_[np.array([tip + t * (x - 7.5) + n * (y - M20_LANE_M) for x, y in pts]), np.zeros(len(pts))]   # noqa: E731
    w = 0.15
    quads = [((a[0], a[1] + w), (b[0], b[1] + w), (b[0], b[1] - w), (a[0], a[1] - w))
             for a, b in zip(M20_SHAFT, M20_SHAFT[1:])]
    return [place(q) for q in quads] + [place(M20_HEAD)]


# Turn arrows (MUTCD 3B.20: two per turn lane, near the start of the full lane and before the stop line) from
# OpenStreetMap's turn:lanes, drawn with West Coast USA's road-marking decal sheet (roadmarkings1, 4 x 4 cells:
# row 0 left, right and straight arrows; cell rows from the top: arrow_quad turns them into the shape's V)
ARROW_MATERIAL = "roadmarkings1"
ARROW_CELLS = {"left": (0.0, 0.0), "right": (0.25, 0.0), "through": (0.5, 0.0)}
ARROW_CELL_M = (4.0, 3.0)           # the cell's quad: m along, across (the arrow ~2.8 m by 0.8 m in it)
ARROW_AT_M = (12.0, 45.0)           # m before the lane's end (the stop line, then one further back)


def _arrow_kind(token):
    vals = set(str(token).split(";"))
    if "through" in vals:
        return "through"
    if vals & {"left", "slight_left", "sharp_left"}:
        return "left"
    if vals & {"right", "slight_right", "sharp_right"}:
        return "right"
    return None


def _point_back(line, d):
    """(point (3,), unit direction (2,) of travel) d m before the end of line (k, 3), or None (shorter)."""
    seg = np.hypot(*np.diff(line[:, :2], axis=0).T)
    s = np.r_[0.0, np.cumsum(seg)]
    at = s[-1] - d
    if at < 2.0:
        return None
    j = int(np.clip(np.searchsorted(s, at) - 1, 0, len(seg) - 1))
    f = (at - s[j]) / max(seg[j], 1e-9)
    p = line[j] + f * (line[j + 1] - line[j])
    t = (line[j + 1, :2] - line[j, :2]) / max(seg[j], 1e-9)
    return p, t


def arrows(osm, pts, two_way, drive, lanes, left_hand=False):
    """[(centre (3,), travel direction (2,), "left" | "right" | "through")] of the turn arrows of a road (pts its
    centreline (k, 3)) from its turn:lanes tags (osm); none without a turn lane. Pure."""
    osm = osm or {}
    import roads as roadsmod
    out = []
    plan = []          # (turn:lanes value, the lanes' offsets left to right in their travel, travel along the way?)
    if not two_way:
        v = osm.get("turn:lanes")
        if v:
            n = len(str(v).split("|"))
            lw = drive / max(n, 1)
            plan.append((v, [drive / 2 - (k + 0.5) * lw for k in range(n)], True))
    else:
        fw, bw = osm.get("turn:lanes:forward"), osm.get("turn:lanes:backward")
        nf = len(str(fw).split("|")) if fw else None
        nb = len(str(bw).split("|")) if bw else None
        f_ = nf or _count(osm.get("lanes:forward")) or max(1, lanes // 2)
        b_ = nb or _count(osm.get("lanes:backward")) or max(1, lanes - f_)
        both = _count(osm.get("lanes:both_ways")) or 0
        lw = drive / max(f_ + b_ + both, 1)
        if not left_hand:
            c_left = drive / 2 - b_ * lw
            c_right = c_left - both * lw
            if fw:
                plan.append((fw, [c_right - (k + 0.5) * lw for k in range(f_)], True))
            if bw:
                plan.append((bw, [c_left + (k + 0.5) * lw for k in range(b_)], False))
        else:
            if fw:
                plan.append((fw, [drive / 2 - (k + 0.5) * lw for k in range(f_)], True))
            if bw:
                plan.append((bw, [-drive / 2 + (k + 0.5) * lw for k in range(b_)], False))
    for value, offs, along in plan:
        toks = str(value).split("|")
        kinds = [_arrow_kind(t) for t in toks]
        if len(toks) != len(offs) or not any(k in ("left", "right") for k in kinds):
            continue
        for kind_, off in zip(kinds, offs):
            if kind_ is None:
                continue
            line = roadsmod.offset_polyline(pts, off) if along else roadsmod.offset_polyline(pts, off)[::-1].copy()
            for d in ARROW_AT_M:
                got = _point_back(line, d)
                if got is not None:
                    out.append((got[0], got[1], kind_))
    return out


def arrow_quad(centre, t, kind_):
    """(corners (4, 3), uvs (4, 2)) of an arrow's quad: tip ahead (travel direction t)."""
    L, W = ARROW_CELL_M
    n = np.array([-t[1], t[0]])
    c = np.asarray(centre, np.float64)
    fwd, side = np.r_[t * L / 2, 0.0], np.r_[n * W / 2, 0.0]
    corners = np.stack([c + fwd + side, c + fwd - side, c - fwd - side, c - fwd + side])
    u0, v0 = ARROW_CELLS[kind_]
    # (the shape's V runs up from the sheet's bottom: the cell's top (1 - v0) at the arrow's tip. Read from the top
    # down, every arrow showed the bottom row upside down: blank in the old sheet, game 0.40's "EXIT" for the
    # straight ones, Baltimore 2026-10-29)
    top, bottom = 1.0 - v0, 1.0 - v0 - 0.25
    uv = np.array([[u0, top], [u0 + 0.25, top], [u0 + 0.25, bottom], [u0, bottom]])
    return corners, uv
