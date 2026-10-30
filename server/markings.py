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
# a broken one; double_gap: the space between the two lines of a double line.
Style = namedtuple("Style", "centre w w_motorway edge edge_motorway centre_rural centre_urban lane_rural lane_urban "
                            "lane_motorway left_edge double_centre_urban double_gap min_centre_m edge_classes ladder",
                   defaults=(None, False))

_US = Style(centre="line_yellow", w=0.12, w_motorway=0.15, edge=0.12, edge_motorway=0.15,
            centre_rural=(3.05, 9.14), centre_urban=(3.05, 9.14), lane_rural=(3.05, 9.14), lane_urban=(3.05, 9.14),
            lane_motorway=(3.05, 9.14), left_edge="line_yellow", double_centre_urban=True, double_gap=0.12,
            min_centre_m=4.9,
            # edge lines on arterials (MUTCD 3B.10: rural arterials 20 ft and wider); local and collector roads mostly
            # have none (PEMA 6-inch aerials, Lancaster County, 2026-10-28: tertiary roads with a centre line only)
            edge_classes=frozenset({"motorway", "trunk", "primary", "secondary"}), ladder=True)
_CA = _US._replace(centre_rural=(3.0, 6.0), centre_urban=(3.0, 6.0), lane_rural=(3.0, 6.0), lane_urban=(3.0, 6.0),
                   lane_motorway=(3.0, 6.0))
_GB = Style(centre="line_white", w=0.10, w_motorway=0.15, edge=0.15, edge_motorway=0.20,
            centre_rural=(3.0, 6.0), centre_urban=(2.0, 4.0), lane_rural=(2.0, 7.0), lane_urban=(1.0, 5.0),
            lane_motorway=(2.0, 7.0), left_edge=None, double_centre_urban=False, double_gap=0.175, min_centre_m=5.5)
_DE = Style(centre="line_white", w=0.12, w_motorway=0.15, edge=0.12, edge_motorway=0.30,
            centre_rural=(6.0, 12.0), centre_urban=(3.0, 6.0), lane_rural=(6.0, 12.0), lane_urban=(3.0, 6.0),
            lane_motorway=(6.0, 12.0), left_edge=None, double_centre_urban=False, double_gap=0.12, min_centre_m=5.5)
_FR = Style(centre="line_white", w=0.12, w_motorway=0.15, edge=0.15, edge_motorway=0.225,
            centre_rural=(3.0, 10.0), centre_urban=(1.5, 5.0), lane_rural=(3.0, 10.0), lane_urban=(1.5, 5.0),
            lane_motorway=(3.0, 10.0), left_edge=None, double_centre_urban=False, double_gap=0.12, min_centre_m=5.5)
_NL = _DE._replace(centre_rural=(3.0, 9.0), lane_rural=(3.0, 9.0), lane_motorway=(3.0, 9.0), edge_motorway=0.20)
_JP = Style(centre="line_white", w=0.15, w_motorway=0.15, edge=0.15, edge_motorway=0.20,
            centre_rural=(5.0, 5.0), centre_urban=(5.0, 5.0), lane_rural=(6.0, 9.0), lane_urban=(5.0, 5.0),
            lane_motorway=(8.0, 12.0), left_edge=None, double_centre_urban=False, double_gap=0.12, min_centre_m=5.5)
_AU = Style(centre="line_white", w=0.10, w_motorway=0.15, edge=0.12, edge_motorway=0.15,
            centre_rural=(3.0, 9.0), centre_urban=(3.0, 9.0), lane_rural=(3.0, 9.0), lane_urban=(3.0, 9.0),
            lane_motorway=(3.0, 9.0), left_edge=None, double_centre_urban=False, double_gap=0.12, min_centre_m=5.5)
# the manuals' fallback: white centre lines 3 / 6 in towns and 6 / 12 out of them (or yellow where the country paints
# them yellow), lanes 3 / 9
_WHITE = Style(centre="line_white", w=0.12, w_motorway=0.15, edge=0.15, edge_motorway=0.20,
               centre_rural=(6.0, 12.0), centre_urban=(3.0, 6.0), lane_rural=(3.0, 9.0), lane_urban=(3.0, 9.0),
               lane_motorway=(3.0, 9.0), left_edge=None, double_centre_urban=False, double_gap=0.12, min_centre_m=5.5)
_YELLOW = _WHITE._replace(centre="line_yellow")

STYLES = {"US": _US, "CA": _CA, "GB": _GB, "IE": _GB, "IM": _GB, "JE": _GB, "GG": _GB, "DE": _DE, "AT": _DE,
          "CH": _DE, "LI": _DE, "LU": _DE, "FR": _FR, "MC": _FR, "BE": _FR, "NL": _NL, "JP": _JP, "AU": _AU, "NZ": _AU}
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
    try:
        n = int(str(v).split(";")[0].strip())
        return n if 0 <= n <= 12 else None
    except (TypeError, ValueError):
        return None


def _vals(v):
    return [p.strip() for p in str(v).split("|")] if v not in (None, "") else None


def _no_change(a, b):
    """change:lanes values of two neighbouring lanes (a left of b, in their travel direction): is crossing between
    them not allowed? (only_left / not_right on a, only_right / not_left on b, or no)"""
    a, b = (a or "yes"), (b or "yes")
    return a in ("no", "not_right", "only_left") or b in ("no", "not_left", "only_right")


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
    fwd, bwd, both = _count(osm.get("lanes:forward")), _count(osm.get("lanes:backward")), \
        _count(osm.get("lanes:both_ways")) or 0
    if not two_way:
        n = lanes
        ch = _vals(osm.get("change:lanes"))
        lw = drive / max(n, 1)
        out = []
        for k in range(1, n):
            solid = ch is not None and len(ch) == n and _no_change(ch[k - 1], ch[k])
            # lanes left to right in travel: lane k's left edge at -drive/2 + k * lw from the right? (offsets are left
            # positive: the leftmost lane starts at +drive/2)
            out.append((drive / 2 - k * lw, "lane", solid))
        return out
    if fwd is None and bwd is None:
        if lanes and lanes - both >= 2:
            fwd = bwd = None
            half = (lanes - both) / 2
            fwd, bwd = int(round(half + 0.25)), int(lanes - both - int(round(half + 0.25)))
        else:
            return None
    if fwd is None:
        fwd = max(1, (lanes or 2) - both - bwd)
    if bwd is None:
        bwd = max(1, (lanes or 2) - both - fwd)
    n = fwd + bwd + both
    lw = drive / max(n, 1)
    # right-hand traffic: the backward lanes on the left of the way's direction (offsets positive), then the turn lane,
    # then the forward lanes; left-hand traffic the other way round
    left_n, right_n = (bwd, fwd) if not left_hand else (fwd, bwd)
    out = []
    edge = drive / 2
    ch_l = _vals(osm.get("change:lanes:backward" if not left_hand else "change:lanes:forward"))
    ch_r = _vals(osm.get("change:lanes:forward" if not left_hand else "change:lanes:backward"))
    # the left-side lanes (travel against the way in right-hand traffic: their left to right is the way's right to left)
    for k in range(1, left_n):
        off = edge - k * lw
        solid = ch_l is not None and len(ch_l) == left_n and _no_change(ch_l[left_n - k - 1], ch_l[left_n - k])
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
        solid = ch_r is not None and len(ch_r) == right_n and _no_change(ch_r[k - 1], ch_r[k])
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
