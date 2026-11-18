"""The interchange audit: the shapes pipeline's steps, each checked on its own before anything is solved (the user,
2026-11-18, over Shanghai's Yan'an West Road junction and the Nanpu Bridge spiral: "you can't solve these in one go just
tweaking one parameter you need to validate individual steps of the process. is the recognition of the road in / out
correct, do we have broken pieces, is level correct for the whole system, or at what point level 'reset' as it becomes a
different interchange").

An instrument, not a fix: it reads what gradeshapes builds (find_systems: the seeds, joins, crossings, ends and dips of each
system; pieces and sample: the unknowns; shape_problem: the constraints the kernel is given) and reports, per system and per
step, pass or fail with every failure named (Earth id, OSM way where the map has one, class, kind, level, lat / lon).
Nothing here changes what the pipeline decides.

  Step 1, in / out: each way's role and where the system meets the roads on the ground; a ramp (a link) must join two
         carriageways, no end of a deck or tunnel hangs in the air (or meets another at another level with no node: taken
         for a separation), the one-way directions agree at every joint.
  Step 2, pieces: no broken part (fewer than two points, zero-length, doubling back, shorter than a joint), no gap
         between one road's parts, no duplicate, one road's parts (its lane sections, "<road>~lanes<a>-<b>") not split
         into roads the system treats as different (a level changing along one road, a road over itself within the run its
         clearance needs), and the sampling the kernel is given: it holds a whole piece to its law at the closest two
         samples (carriageways.solve's `step`).
  Step 3, levels: the system's constraints (grade per step, floors and ceilings, joints, clearances) as a graph of
         differences (z_v - z_u <= w): the system has heights if and only if the graph has no negative cycle, found
         exactly (SPFA, deterministic: plain floats, the same edges in the same order). On failure the cycle is reduced
         to an irreducible one (no crossing or joint of it can be left out and still contradict), its legs named, the
         metres it is short given. The curves (a second difference, not a difference) are not in the graph: a graph with no
         negative cycle is necessary for the shape, not sufficient. Checked twice: with the grade held per step at the
         piece's closest two samples as the kernel is given it, and with each step's own run.
  Step 4, where the level resets: the system's size and, for an oversized one, the chain of crossings and joints that merged
         it. Where a road on the ground between two decks' ends has a stretch with nothing over or under it, every way at
         that node is on its ground: the two sides are two interchanges, and the merge is reported (find_systems' ramp
         rule: CONNECT_M).

Pure, deterministic: dnp / dmath for the geometry, the graph in plain Python numbers."""
import math
from collections import deque, namedtuple

import numpy as np

import dnp
import gradeshapes as gs

GAP_M = 15.0                # m: the ends of one road's parts this far apart (more than a joint: gs.JOIN_M) are a gap in it
SPIKE_COS = -0.985          # a vertex turning back on itself (more than ~170 deg) is a broken line
DUP_NEAR_M = 0.5            # m: two lines this close along their length (and as long) are one line twice
OVERSIZE_PIECES = 60        # a system's pieces (its decks and its dips): more is two interchanges or more (Zurich's largest has 17)
FREE_GROUND_M = 10.0        # m of a road on the ground between two decks' ends with nothing over or under it: a ground node
SHORT_STEP_RATIO = 0.5      # a piece's closest samples under this share of STEP_M make the kernel's grade law that much tighter
EPS_M = 1e-7                # m: a relaxation must gain this much (float noise is no cycle)
CONTRADICTION_M = 1e-6      # m: a cycle this short of closing is a contradiction
MAX_TESTS = 400             # most graphs searched in reducing one cycle (a count, never a time: the answer is the same everywhere)
TOP = 12                    # findings of one kind kept whole in a report

Finding = namedtuple("Finding", "step code level text where refs")
# level: "fail" (the step fails) or "note" (seen, not a failure); where: "lat,lon" or "x,y" in the frame; refs: [member info]


def finding_dict(f):
    return dict(step=f.step, code=f.code, level=f.level, text=f.text, where=f.where, refs=list(f.refs))


class Namer:
    """Names members and places: Earth id, OSM way (meta), class, kind, level, bridge / tunnel, lat / lon. meta:
    {Earth feature id: dict(osm=, name=, layer=, oneway=, dir=)} (what the map has of it; {} in a synthetic system),
    ll(x, y) -> (lat, lon) or None (the frame's x, y are named)."""

    def __init__(self, net, ll=None, meta=None, edge=None):
        # edge(xy) -> whether the point is at the edge of the map data the net was made from (a road met there may go on
        # in a cell that was not read: an end there is no failure of the data)
        self.net, self.ll, self.meta, self.edge = net, ll, meta or {}, edge

    def earth(self, i):
        k = self.net.m[i].key
        return str(k[0] if isinstance(k, tuple) else k)

    def md(self, i):
        e = self.earth(i)
        return self.meta.get(e) or self.meta.get(gs._road_of(self.net.m[i])) or {}

    def info(self, i):
        m = self.net.m[i]
        md = self.md(i)
        structure = {"deck": "bridge", "rail_deck": "rail bridge", "tunnel": "tunnel", "ground": "ground",
                     "rail": "rail"}.get(m.kind, m.kind)
        return dict(earth=self.earth(i), osm=md.get("osm"), name=md.get("name"), cls=m.cls, kind=m.kind,
                    structure=structure, level=float(m.level), layer=md.get("layer"), length_m=round(self.net.L[i], 1))

    def point(self, xy):
        x, y = float(xy[0]), float(xy[1])
        got = self.ll(x, y) if self.ll else None
        return "%.5f,%.5f" % got if got else "x=%.1f,y=%.1f" % (x, y)

    def at(self, i, s):
        net = self.net
        return self.point(gs.at_s(net.lines[i], net.s[i], min(max(float(s), 0.0), net.L[i])))

    def label(self, i, s=None):
        d = self.info(i)
        lv = "%g" % d["level"]
        out = "%s%s %s %s%s %.0f m" % (d["earth"], " (OSM %s)" % d["osm"] if d["osm"] else "", d["cls"], d["structure"],
                                       " level " + lv if d["kind"] != "ground" else "", d["length_m"])
        if d["name"]:
            out += ' "%s"' % d["name"]
        return out + (" at %s" % self.at(i, s) if s is not None else "")


def _members_of(net, sysm):
    """The members a system's checks look at: its seeds, the roads its dips are cut in (and through) and the roads it
    meets on the ground."""
    got = set(sysm["seeds"]) | {d for d, _a, _b in sysm["dips"]} | {j for _i, _si, j, _sj in sysm["ends"]}
    return sorted(got)


# ---- step 1: in / out ----

def _role(m):
    base = gs._base(m.cls)
    if (m.cls or "").endswith("_link"):
        return "ramp"
    if base in ("motorway", "trunk", "primary"):
        return "main line"
    return "street"


def _ends_of(net, rels, i):
    """{0 | 1: [(member, "join" | "cross", s on it)]} what lies at member i's two ends (within JOIN_M), from its relations
    (gradeshapes._relations: the same answer the system was made from)."""
    L = net.L[i]
    out = {0: [], 1: []}
    for j, kind, si, sj, _q in rels:
        if min(si, L - si) <= gs.JOIN_M:
            out[int(si > L / 2)].append((j, kind, sj))
    return out


def _flow(md, end):
    """(vehicles enter the road from the joint at its end `end`, vehicles leave it to the joint there) from the map's
    one-way flag and its direction along the line (+1 with it, -1 against); a two-way or unknown road: both."""
    if not md or not md.get("oneway"):
        return True, True
    d = md.get("dir") or 1
    forward = d > 0
    # (with the line, vehicles enter at its start and leave at its end)
    return (forward and end == 0) or (not forward and end == 1), (forward and end == 1) or (not forward and end == 0)


def check_roles(net, sysm, namer, rels=None):
    """Step 1: the system's ways, where it meets the roads on the ground, and whether each end is joined to something. rels:
    {member: gradeshapes._relations}, a cache the caller may share."""
    rels = rels if rels is not None else {}

    def rel(i):
        if i not in rels:
            rels[i] = [r_ for r_ in gs._relations(net, i) if not gs._tunnel_network(net, r_[0])]
        return rels[i]
    finds = []
    roles = {}
    dangling = ramps = 0
    seeds = sysm["seeds"]
    for i in seeds:
        m = net.m[i]
        role = _role(m)
        roles[role] = roles.get(role, 0) + 1
        ends = _ends_of(net, rel(i), i)
        partners = []
        for e in (0, 1):
            joins = [(j, sj) for j, kind, sj in ends[e] if kind == "join"]
            sep = [(j, sj) for j, kind, sj in ends[e] if kind == "cross"]
            where = namer.point(net.lines[i][0 if e == 0 else -1])
            partners.append({gs._road_of(net.m[j]) for j, _sj in joins} - {gs._road_of(m)})
            if joins:
                continue
            end_name = "start" if e == 0 else "end"
            if namer.edge is not None and namer.edge(net.lines[i][0 if e == 0 else -1]):
                finds.append(Finding(1, "end_at_data_edge", "note",
                                     "%s: its %s at %s meets nothing, at the edge of the map data read" % (namer.label(i), end_name,
                                                                                                         where), where,
                                     [namer.info(i)]))
                continue
            dangling += 1
            if sep:
                j, sj = sep[0]
                text = ("%s: its %s at %s meets %s (at another level, no node of both: taken for a separation, not a "
                        "joint)" % (namer.label(i), end_name, where, namer.label(j, sj)))
                code = "end_separated"
            else:
                text = "%s: its %s at %s meets nothing (hangs in the air)" % (namer.label(i), end_name, where)
                code = "dangling_end"
            finds.append(Finding(1, code, "fail", ("a ramp: " if role == "ramp" else "") + text, where,
                                 [namer.info(i)] + [namer.info(j) for j, _s in sep[:1]]))
        if role == "ramp":
            ramps += 1
            if all(partners) and partners[0] == partners[1] and len(partners[0]) == 1:
                finds.append(Finding(1, "ramp_same_road", "note",
                                     "%s: both its ends join the same road (%s)" % (namer.label(i), sorted(partners[0])[0]),
                                     namer.at(i, 0.0), [namer.info(i)]))
    # one-way directions at the joints of the seeds' ends
    seen = set()
    seen_text = set()
    oneway_known = 0
    for i in seeds:
        ends = _ends_of(net, rel(i), i)
        for e in (0, 1):
            here = [(i, e)]
            both = []           # (member, flows both ways: a T onto its middle)
            for j, kind, sj in ends[e]:
                if kind != "join":
                    continue
                if min(sj, net.L[j] - sj) <= gs.JOIN_M:
                    ej = int(sj > net.L[j] / 2)
                    if ej == e and gs._road_of(net.m[j]) == gs._road_of(net.m[i]):
                        continue            # (one road's own part laid over it, step 2's overlap: no way on from the joint)
                    here.append((j, ej))
                else:
                    both.append(j)
            if len(here) + len(both) < 2:
                continue
            ident = tuple(sorted(here))
            if ident in seen:
                continue
            seen.add(ident)
            flows = [(m_, en, _flow(namer.md(m_), en)) for m_, en in here]
            if any(namer.md(m_).get("oneway") for m_, _en, _f in flows):
                oneway_known += 1
            for m_, en, (enters, leaves) in flows:
                if enters and leaves:
                    continue
                others = [f for o, eo, f in flows if (o, eo) != (m_, en)]
                # (traffic arriving at the joint needs a way that takes it on, one that vehicles enter there; traffic
                # leaving it needs one that brings it, one vehicles leave there; a road the end meets the middle of does both)
                ok = bool(both) or any((f[0] if leaves else f[1]) for f in others)
                if not ok:
                    where = namer.point(net.lines[m_][0 if en == 0 else -1])
                    verb = "flows into" if leaves else "flows out of"
                    what = ("nothing there takes it on" if leaves else "nothing there feeds it")
                    names = "; ".join(namer.label(o) for o, eo, _f in flows if (o, eo) != (m_, en))
                    text = ("%s %s the joint at %s: %s (the others there: %s)"
                            % (namer.label(m_), verb, where, what, names))
                    if text in seen_text:       # (one joint met from each of its roads' ends)
                        continue
                    seen_text.add(text)
                    finds.append(Finding(1, "oneway_conflict", "fail", text, where, [namer.info(o) for o, _eo, _f in flows]))
    portals = []
    for i, si, j, sj in sysm["ends"]:
        portals.append(dict(seed=namer.info(i), road=namer.info(j), where=namer.at(i, si)))
    if not portals:
        finds.append(Finding(1, "no_entry", "note", "the system meets no road on the ground (every end joins another deck)",
                             namer.at(seeds[0], 0.0), [namer.info(seeds[0])]))
    stats = dict(roles=roles, ramps=ramps, portals=len(portals), dangling_ends=dangling, oneway_joints=oneway_known)
    return _step(1, finds, stats, portals=portals)


def _step(n, finds, stats, **more):
    out = dict(step=n, ok=not any(f.level == "fail" for f in finds), findings=finds, stats=stats)
    out.update(more)
    return out


# ---- step 2: pieces ----

def _member_defects(net, i):
    """[(code, text)] of what is broken in member i's line."""
    line = net.lines[i]
    out = []
    if len(line) < 2:
        return [("broken_part", "fewer than two points")]
    if not np.isfinite(line).all():
        out.append(("broken_part", "a point is not a number"))
        return out
    seg = np.diff(line, axis=0)
    ln = dnp.hypot(seg[:, 0], seg[:, 1])
    zero = int((ln < 0.01).sum())
    if zero:
        out.append(("zero_length", "%d zero-length segment%s (a repeated point)" % (zero, "s" if zero > 1 else "")))
    if net.L[i] < gs.JOIN_M:
        out.append(("short_part", "%.2f m long: shorter than a joint (%.1f m), its two ends are one joint"
                    % (net.L[i], gs.JOIN_M)))
    keep = ln >= 0.05
    if keep.sum() >= 2:
        u = seg[keep] / ln[keep][:, None]
        cos = (u[:-1] * u[1:]).sum(1)
        turns = int((cos < SPIKE_COS).sum())
        if turns:
            out.append(("spike", "%d vertex%s doubling back on the line (a turn of more than 170 deg)"
                        % (turns, "es" if turns > 1 else "")))
    return out


def _free_end(net, i, e, rel_of):
    """Whether member i's end e has no other member within JOIN_M."""
    L = net.L[i]
    return not any(min(si, L - si) <= gs.JOIN_M and int(si > L / 2) == e for _j, _k, si, _sj, _q in rel_of(i))


def check_pieces(net, sysm, pcs, smp, namer, rels=None):
    """Step 2: the parts and pieces the system is made of."""
    rels = rels if rels is not None else {}

    def rel_of(i):
        if i not in rels:
            rels[i] = [r_ for r_ in gs._relations(net, i) if not gs._tunnel_network(net, r_[0])]
        return rels[i]
    scope = sorted(set(_members_of(net, sysm)) | {j for pc in pcs for j, _a, _b in pc["segs"]})
    finds = []
    # 2a. broken parts
    broken = 0
    for i in scope:
        for code, text in _member_defects(net, i):
            # (a repeated point is a segment of no length the line's arc lengths and tangents never see: seen, not a failure)
            level = "note" if code == "zero_length" else "fail"
            broken += level == "fail"
            finds.append(Finding(2, code, level, "%s: %s" % (namer.label(i), text), namer.at(i, 0.0), [namer.info(i)]))
    # 2b. gaps between one road's parts, 2c. duplicates, 2d. one road split
    by_road = {}
    for j, m in enumerate(net.m):
        by_road.setdefault(gs._road_of(m), []).append(j)
    gaps = dups = splits = 0
    scope_set = set(scope)
    done = set()
    for a in scope:
        ma = net.m[a]
        if len(net.lines[a]) < 2:
            continue
        for b in by_road.get(gs._road_of(ma), ()):
            if b == a or (min(a, b), max(a, b)) in done or len(net.lines[b]) < 2:
                continue
            done.add((min(a, b), max(a, b)))
            mb = net.m[b]
            for ea in (0, 1):
                for eb in (0, 1):
                    pa, pb = net.lines[a][0 if ea == 0 else -1], net.lines[b][0 if eb == 0 else -1]
                    d = float(math.hypot(float(pa[0] - pb[0]), float(pa[1] - pb[1])))
                    if gs.JOIN_M < d <= GAP_M and _free_end(net, a, ea, rel_of) and _free_end(net, b, eb, rel_of):
                        gaps += 1
                        finds.append(Finding(2, "gap", "fail",
                                             "one road's parts %.1f m apart: %s (its %s) and %s (its %s), nothing joins "
                                             "either end" % (d, namer.label(a), "start" if ea == 0 else "end",
                                                             namer.label(b), "start" if eb == 0 else "end"),
                                             namer.point(pa), [namer.info(a), namer.info(b)]))
            if mb.kind == ma.kind == "deck" and ma.level != mb.level:
                # (a road's own parts at two levels, joined end to end: one road going on in the joints
                # (gradeshapes._end_meets), two levels in the order of its crossings (gradeshapes._over))
                for ea in (0, 1):
                    for eb in (0, 1):
                        pa, pb = net.lines[a][0 if ea == 0 else -1], net.lines[b][0 if eb == 0 else -1]
                        if math.hypot(float(pa[0] - pb[0]), float(pa[1] - pb[1])) <= gs.JOIN_M:
                            splits += 1
                            # (a note: the joint is one road going on since BeamNG.Earth#47 (gradeshapes._end_meets); what
                            # still turns on the two levels is the order of its crossings, and a road over itself too
                            # close is its own finding below)
                            finds.append(Finding(2, "level_split", "note",
                                                 "one road's parts joined end to end at two map levels (%g and %g), the "
                                                 "road going on in the joint and two levels in the order of its "
                                                 "crossings: %s and %s" % (ma.level, mb.level, namer.label(a),
                                                                           namer.label(b)),
                                                 namer.point(pa), [namer.info(a), namer.info(b)]))
    # duplicates: the same kind and level, the same line (a road's own parts: any stretch of them over one another), found
    # by each one's points within DUP_NEAR_M of the other's line
    def points(i):
        sv = np.linspace(0.0, net.L[i], max(2, int(net.L[i] // gs.STEP_M) + 1))
        return np.stack([np.interp(sv, net.s[i], net.lines[i][:, 0]), np.interp(sv, net.s[i], net.lines[i][:, 1])], axis=1)
    pts = {}
    for a in scope:
        ma = net.m[a]
        if len(net.lines[a]) < 2 or net.L[a] < 1.0:
            continue
        for b in net.near(a, 1.0):
            mb = net.m[b]
            if (b < a and b in scope_set) or mb.kind != ma.kind or mb.level != ma.level or len(net.lines[b]) < 2                     or net.L[b] < 1.0:
                continue
            same = gs._road_of(ma) == gs._road_of(mb)
            for i in (a, b):
                if i not in pts:
                    pts[i] = points(i)
            ina = float((gs._proj_many(net.lines[b], net.s[b], pts[a])[1] <= DUP_NEAR_M).mean())
            inb = float((gs._proj_many(net.lines[a], net.s[a], pts[b])[1] <= DUP_NEAR_M).mean())
            over = max(ina * net.L[a], inb * net.L[b])
            whole = min(ina, inb) >= 0.9 and abs(net.L[a] - net.L[b]) <= 0.1 * max(net.L[a], net.L[b])
            if whole or (same and over >= 2 * gs.STEP_M):
                dups += 1
                finds.append(Finding(2, "duplicate" if whole else "overlap", "fail",
                                     ("the same line twice" if whole else "one road's parts over one another for %.0f m" % over)
                                     + ": %s and %s" % (namer.label(a), namer.label(b)),
                                     namer.at(a, net.L[a] / 2), [namer.info(a), namer.info(b)]))
    # one road over itself, within the run its clearance takes (a split road: its own parts, not a loop)
    along = None
    for c in sysm["crossings"]:
        u, d = c["u"], c["d"]
        if gs._road_of(net.m[u]) != gs._road_of(net.m[d]):
            continue
        lane = "~lanes" in namer.earth(u) and "~lanes" in namer.earth(d)
        if along is None:
            along = gs._deck_paths(net, sysm)
        run = along(u, c["su"], d, c["sd"]) if net.m[u].kind == net.m[d].kind == "deck" else math.inf
        need = gs.clearance_need(net, u, d, c["q"]) / max(gs.deck_grade(net.m[u]), gs.deck_grade(net.m[d]))
        if lane or run < need:
            splits += 1
            finds.append(Finding(2, "lane_sections_cross" if lane else "road_over_itself_too_close", "fail",
                                 ("lane sections of one road cross each other" if lane else
                                  "one road over itself within %.0f m along it, its clearance takes %.0f m at its grade"
                                  % (run, need)) + ": %s over %s at %s" % (namer.label(u, c["su"]), namer.label(d, c["sd"]),
                                                                            namer.point(c["q"])),
                                 namer.point(c["q"]), [namer.info(u), namer.info(d)]))
    # 2e. the pieces' samples
    short = jumps = zero_steps = 0
    mins = []
    for p, (pc, x) in enumerate(zip(pcs, smp)):
        d = np.asarray(x["d"], np.float64)
        dd = np.diff(d)
        if not len(dd):
            finds.append(Finding(2, "broken_piece", "fail", "a piece of %d sample (%s)" % (len(d), namer.label(pc["main"])),
                                 namer.at(pc["main"], 0.0), [namer.info(pc["main"])]))
            continue
        z0 = int((dd <= 1e-6).sum())
        if z0:
            zero_steps += z0
            k = int(np.flatnonzero(dd <= 1e-6)[0])
            finds.append(Finding(2, "duplicate_sample", "fail",
                                 "%s piece of %s: %d sample%s on the same spot (a zero step) at %s"
                                 % (pc["kind"], namer.label(pc["main"]), z0, "s" if z0 > 1 else "", namer.point(x["xy"][k])),
                                 namer.point(x["xy"][k]), [namer.info(pc["main"])]))
        pos = dd[dd > 1e-6]
        if not len(pos):
            continue
        lo, med = float(pos.min()), float(np.median(pos))
        mins.append(lo)
        if lo < SHORT_STEP_RATIO * gs.STEP_M:
            short += 1
            k = int(np.flatnonzero(dd == lo)[0])
            mem = int(x["mem"][k + 1])
            finds.append(Finding(2, "short_step", "fail",
                                 "%s piece of %s (%d samples, %.0f m): its closest two samples are %.2f m apart (median step "
                                 "%.2f m) at %s on %s; the kernel holds the whole piece to grade x %.2f m, a grade of %.1f%% "
                                 "of the law (the curve limit %.0f%% of its)"
                                 % (pc["kind"], namer.label(pc["main"]), len(d), float(d[-1]), lo, med, namer.point(x["xy"][k]),
                                    namer.label(mem), lo, 100.0 * lo / med, 100.0 * (lo / med) * (lo / med)),
                                 namer.point(x["xy"][k]), [namer.info(pc["main"]), namer.info(mem)]))
        if float(pos.max()) > gs.STEP_M + gs.JOIN_M:
            jumps += 1
            k = int(np.flatnonzero(dd == float(pos.max()))[0])
            finds.append(Finding(2, "gap_in_piece", "fail",
                                 "%s piece of %s: two samples %.1f m apart at %s (a gap between its parts)"
                                 % (pc["kind"], namer.label(pc["main"]), float(pos.max()), namer.point(x["xy"][k])),
                                 namer.point(x["xy"][k]), [namer.info(pc["main"])]))
    stats = dict(members=len(scope), pieces=len(pcs), decks=sum(1 for pc in pcs if pc["kind"] == "deck"),
                 dips=sum(1 for pc in pcs if pc["kind"] == "dip"), broken=broken, gaps=gaps, duplicates=dups, splits=splits,
                 short_step_pieces=short, gap_pieces=jumps, duplicate_samples=zero_steps,
                 min_step_m=round(min(mins), 3) if mins else None)
    return _step(2, finds, stats)


# ---- step 3: levels ----

class Graph:
    """The system's difference constraints: an edge u -> v of weight w says z_v - z_u <= w (node n is height 0)."""

    def __init__(self, n):
        self.n = n + 1
        self.zero = n
        self.eu, self.ev, self.ew, self.kind, self.ref, self.gid = [], [], [], [], [], []
        self.groups = []
        self.adj = None

    def add(self, u, v, w, kind, ref, group=None):
        k = len(self.eu)
        self.eu.append(u)
        self.ev.append(v)
        self.ew.append(float(w))
        self.kind.append(kind)
        self.ref.append(ref)
        if group is None:
            group = len(self.groups)
            self.groups.append([])
        self.groups[group].append(k)
        self.gid.append(group)
        return group

    def finish(self):
        self.adj = [[] for _ in range(self.n)]
        for k, u in enumerate(self.eu):
            self.adj[u].append(k)
        return self


def _spfa(g, skip=frozenset(), lam=1.0):
    """(potentials, None) when the graph has no negative cycle (heights that satisfy every constraint), else (None,
    [edge indices of a negative cycle in order]). The queue starts with every node at height 0, in index order. lam: the
    grade law's edges (kind "grade") weigh lam times what they do."""
    n, adj, ev, eu = g.n, g.adj, g.ev, g.eu
    ew = g.ew if lam == 1.0 else [w * lam if k == "grade" else w for w, k in zip(g.ew, g.kind)]
    dist = [0.0] * n
    pred = [-1] * n
    inq = [True] * n
    queue = deque(range(n))
    since, every = 0, max(n, 2000)
    while queue:
        u = queue.popleft()
        inq[u] = False
        du = dist[u]
        for k in adj[u]:
            if k in skip:
                continue
            v = ev[k]
            nd = du + ew[k]
            if nd < dist[v] - EPS_M:
                dist[v] = nd
                pred[v] = k
                since += 1
                if not inq[v]:
                    inq[v] = True
                    queue.append(v)
        if since >= every:
            since = 0
            cyc = _pred_cycle(n, pred, eu)
            if cyc is not None:
                return None, cyc
    return dist, None


def _pred_cycle(n, pred, eu):
    stamp = [0] * n
    for s in range(n):
        if stamp[s]:
            continue
        v = s
        while v != -1 and stamp[v] == 0:
            stamp[v] = s + 1
            k = pred[v]
            v = eu[k] if k >= 0 else -1
        if v != -1 and stamp[v] == s + 1:
            cyc, u = [], v
            while True:
                k = pred[u]
                cyc.append(k)
                u = eu[k]
                if u == v:
                    break
            cyc.reverse()
            return cyc
    return None


def certificate(g, dist):
    """The most any constraint of g is violated by heights dist (the potentials _spfa returns when there is no negative cycle:
    a certificate that they exist, checked edge by edge), m."""
    return max([dist[g.ev[k]] - dist[g.eu[k]] - g.ew[k] for k in range(len(g.eu))] + [0.0])


def negative_cycle(g, skip=frozenset(), lam=1.0):
    """A negative cycle of g (edge indices in order) with its weight, or None: the constraints have heights (within
    CONTRADICTION_M)."""
    _d, cyc = _spfa(g, skip, lam)
    if cyc is None:
        return None
    w = math.fsum(g.ew[k] * lam if g.kind[k] == "grade" else g.ew[k] for k in cyc)
    return (cyc, w) if w < -CONTRADICTION_M else None


def minimal_contradiction(g, cycle, fixed=("grade",), order=None):
    """The cycle reduced until no constraint of it (but the grade law along the roads: kinds in fixed) can be left out and
    the rest still contradict: each in turn left out of the whole graph (with the ones already left out), the cycle the
    search finds then taking the place of the old one while there is one. order(edges) sorts the candidates (the first
    is tried first; by default, as the cycle has them). (cyc, weight, graphs searched)."""
    skip = set()
    cur = list(cycle)
    kept = set()
    tests = 0
    while tests < MAX_TESTS:
        cand = [k for k in cur if g.kind[k] not in fixed and g.gid[k] not in kept]
        if not cand:
            break
        k = (order(cand) if order else cand)[0]
        trial = skip | set(g.groups[g.gid[k]])
        tests += 1
        got = negative_cycle(g, trial)
        if got is not None:
            skip, cur = trial, got[0]
        else:
            kept.add(g.gid[k])
    return cur, math.fsum(g.ew[k] for k in cur), tests


class Constraints:
    """shape_problem's constraints of one system as a Graph, each edge knowing where it came from. spacing "kernel": the
    grade law per step as the kernel is given it (the piece's closest two samples), "true": each step's own run."""

    def __init__(self, net, sysm, pcs, smp, ground, fixed_z, spacing="kernel", problem=None):
        if problem is None:
            trace = {}
            problem = gs.shape_problem(net, sysm, pcs, smp, ground, fixed_z, trace) + (trace,)
        self.chains, self.cps, trace = problem
        self.net, self.sysm, self.pcs, self.smp, self.ground, self.spacing = net, sysm, pcs, smp, ground, spacing
        self.trace = trace
        self.off = [0]
        for ch in self.chains:
            self.off.append(self.off[-1] + len(ch["t"]))
        g = self.g = Graph(self.off[-1])
        Z = g.zero
        bound = trace["bound"]
        for c, ch in enumerate(self.chains):
            m = len(ch["t"])
            for k in range(m - 1):
                w = self.allow(c, k, k + 1)
                grp = g.add(self.off[c] + k, self.off[c] + k + 1, w, "grade", (c, k))
                g.add(self.off[c] + k + 1, self.off[c] + k, w, "grade", (c, k), grp)
            lo, hi = np.asarray(ch["lo"], np.float64), np.asarray(ch["hi"], np.float64)
            for k in np.flatnonzero(np.isfinite(lo)).tolist():
                src = bound.get((c, k, "lo"))
                g.add(self.off[c] + k, Z, -float(lo[k]), "floor", (c, k, src[1] if src and src[0] >= lo[k] - 1e-9 else None))
            for k in np.flatnonzero(np.isfinite(hi)).tolist():
                src = bound.get((c, k, "hi"))
                g.add(Z, self.off[c] + k, float(hi[k]), "ceiling", (c, k, src[1] if src and src[0] <= hi[k] + 1e-9 else None))
        for n_, cp in enumerate(self.cps):
            a, i, b, j, kind, val = cp[:6]
            ia, ib = self.node(a, i), self.node(b, j)
            why = trace["cp"][n_]
            if kind == "eq":
                grp = g.add(ib, ia, 0.0, why[0], (n_,) + why[1:])
                g.add(ia, ib, 0.0, why[0], (n_,) + why[1:], grp)
            elif kind == "ge":
                g.add(ia, ib, -float(val), why[0], (n_,) + why[1:])
        g.finish()

    def allow(self, c, k1, k2):
        """The rise the grade law allows along chain c between its samples k1 and k2."""
        ch = self.chains[c]
        if self.spacing == "true":
            d = self.smp[c]["d"]
            return ch["grade"] * abs(float(d[k2]) - float(d[k1]))
        return ch["grade"] * float(ch["step"]) * abs(k2 - k1)

    def node(self, c, k):
        n = len(self.chains[c]["t"])
        return self.off[c] + (k if k >= 0 else n + k)

    def of(self, v):
        """(chain, sample) of node v, None for the zero node."""
        if v == self.g.zero:
            return None
        lo, hi = 0, len(self.off) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.off[mid] <= v:
                lo = mid
            else:
                hi = mid
        return lo, v - self.off[lo]

    def reduced(self, gids):
        """The constraints of groups gids (a set of g's non-grade groups) with the grade law along each piece between the
        nodes they touch only: a graph of a few nodes with the same cycles through them, each leg of a piece one edge.
        Its edges keep where they came from: ref (the original edge) for a constraint, (chain, from, to) for a run."""
        g0 = self.g
        idx, order = {}, []
        for gid in sorted(gids):
            for k in g0.groups[gid]:
                for v in (g0.eu[k], g0.ev[k]):
                    if v not in idx:
                        idx[v] = len(idx)
                        order.append(v)
        T = Graph(len(idx) - 1)
        T.nodes = order
        T.orig = []
        for gid in sorted(gids):
            grp = None
            for k in g0.groups[gid]:
                grp = T.add(idx[g0.eu[k]], idx[g0.ev[k]], g0.ew[k], g0.kind[k], g0.ref[k], grp)
                T.orig.append(k)
        by_chain = {}
        for v in order:
            got = self.of(v)
            if got is not None:
                by_chain.setdefault(got[0], []).append((got[1], v))
        for c in sorted(by_chain):
            lst = sorted(by_chain[c])
            for (k1, v1), (k2, v2) in zip(lst, lst[1:]):
                w = self.allow(c, k1, k2)
                grp = T.add(idx[v1], idx[v2], w, "grade", (c, k1, k2))
                T.orig.append(None)
                T.add(idx[v2], idx[v1], w, "grade", (c, k2, k1), grp)
                T.orig.append(None)
        return T.finish()

    def legs(self, T, cyc, namer):
        """[leg dict] of a cycle of a reduced graph: its runs along a piece (the grade law) and its constraints, in
        order, each with its weight, the Earth ids that made it and where."""
        out = []
        for k in cyc:
            kind = T.kind[k]
            if kind == "grade":
                text, refs = self._run(T.ref[k], T.ew[k], namer)
            else:
                text, refs = self._leg(kind, T.ref[k], T.ew[k], T.nodes[T.eu[k]], T.nodes[T.ev[k]], namer)
            out.append(dict(kind=kind, w=round(T.ew[k], 4), text=text, refs=refs))
        return out

    def _run(self, ref, w, namer):
        c, k1, k2 = ref
        x = self.smp[c]
        m1, m2 = int(x["mem"][k1]), int(x["mem"][k2])
        run = abs(float(x["d"][k2]) - float(x["d"][k1]))
        g = self.chains[c]["grade"]
        return ("along the %s piece of %s, %.0f m from %s to %s: at most %.2f m higher at the end (grade law %.1f%%, %s)"
                % (self.pcs[c]["kind"], namer.label(self.pcs[c]["main"]), run, namer.at(m1, x["ms"][k1]),
                   namer.at(m2, x["ms"][k2]), w, 100.0 * g,
                   "%.2f m a step" % self.chains[c]["step"] if self.spacing == "kernel" else "each step's own run"),
                [namer.info(m1)] + ([namer.info(m2)] if m2 != m1 else []))

    def _leg(self, kind, ref, w, u, v, namer):
        net, smp = self.net, self.smp
        if kind in ("clear", "stacked"):
            ua, lb = self.of(u), self.of(v)
            mu = int(smp[ua[0]]["mem"][ua[1]])
            ml = int(smp[lb[0]]["mem"][lb[1]])
            return ("clearance %.2f m: %s over %s at %s%s" % (-w, namer.label(mu, smp[ua[0]]["ms"][ua[1]]),
                                                             namer.label(ml, smp[lb[0]]["ms"][lb[1]]),
                                                             namer.point(smp[ua[0]]["xy"][ua[1]]),
                                                             "" if kind == "clear" else " (one deck along another)"),
                    [namer.info(mu), namer.info(ml)])
        if kind in ("join", "overlap"):
            a, b = self.of(u), self.of(v)
            ma, mb = int(smp[a[0]]["mem"][a[1]]), int(smp[b[0]]["mem"][b[1]])
            return ("%s: %s at the same height as %s at %s" % ("joint" if kind == "join" else "two pieces on one road",
                                                                namer.label(ma, smp[a[0]]["ms"][a[1]]),
                                                                namer.label(mb, smp[b[0]]["ms"][b[1]]),
                                                                namer.point(smp[a[0]]["xy"][a[1]])),
                    [namer.info(ma), namer.info(mb)])
        c, k = self.of(u if kind == "floor" else v)
        mem, s = int(smp[c]["mem"][k]), float(smp[c]["ms"][k])
        ci = ref[2] if len(ref) > 2 else None
        gh = float(self.ground[c][k])
        if kind == "floor":
            if ci is not None:
                cr = self.sysm["crossings"][ci]
                return ("floor %.2f m: %s held over %s at %.2f m there (a road the shape does not move)"
                        % (-w, namer.label(mem, s), namer.label(cr["d"], cr["sd"]),
                           -w - gs.clearance_need(net, cr["u"], cr["d"], cr["q"])), [namer.info(mem), namer.info(cr["d"])])
            return ("floor %.2f m: %s over its ground (%.2f m) by its girders and the air under them"
                    % (-w, namer.label(mem, s), gh), [namer.info(mem)])
        if ci is not None:
            cr = self.sysm["crossings"][ci]
            return ("ceiling %.2f m: %s held under %s at %.2f m (a part the shape does not move)"
                    % (w, namer.label(mem, s), namer.label(cr["u"], cr["su"]),
                       w + gs.clearance_need(net, cr["u"], cr["d"], cr["q"])), [namer.info(mem), namer.info(cr["u"])])
        return ("ceiling %.2f m: %s never rises over its ground (%.2f m)" % (w, namer.label(mem, s), gh), [namer.info(mem)])


def tightest(T):
    """For a reduced graph with a contradiction: (the smallest factor lam of the grade law that gives it heights, the
    cycle that is the last to close as lam falls to it (the most contradicting: its weight at lam 1 is the metres it is
    short), None for lam when no grade law does (a floor over a ceiling)). Bisection on SPFA over a few nodes."""
    hi = 1.0
    while negative_cycle(T, lam=hi) is not None:
        hi *= 2.0
        if hi > 1e6:
            return None, negative_cycle(T, lam=1.0)[0]
    lo = hi / 2.0 if hi > 1.0 else 1.0
    if hi == 1.0:
        return 1.0, None
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if negative_cycle(T, lam=mid) is None:
            hi = mid
        else:
            lo = mid
    got = negative_cycle(T, lam=lo)
    return hi, (got[0] if got else None)


def contradiction(cons, cyc, namer):
    """What the first negative cycle cyc of cons.g comes to: its constraints reduced to an irreducible set (no one of them can
    be left out and the rest still contradict: the deletion filter, on the few nodes they touch, in three orders, the
    fewest kept), the cycle of them that is short by most (the grade law at lam 1) and the factor of the law that gives
    them heights."""
    g = cons.g
    gids = sorted({g.gid[k] for k in cyc if g.kind[k] != "grade"})
    T0 = cons.reduced(gids)
    first = negative_cycle(T0)
    best = None
    orders = (None, lambda c: c[::-1], lambda c: sorted(c, key=lambda k: (-abs(T0.ew[k]), k)))
    tests = 0
    for order in orders:
        small, _w, n = minimal_contradiction(T0, first[0], order=order)
        tests += n
        keep = sorted({T0.gid[k] for k in small if T0.kind[k] != "grade"})
        orig = sorted({g.gid[T0.orig[k]] for k in small if T0.kind[k] != "grade"})
        if best is None or len(orig) < len(best):
            best = orig
    T = cons.reduced(best)
    lam, crit = tightest(T)
    cyc2 = crit if crit is not None else negative_cycle(T)[0]
    legs = cons.legs(T, cyc2, namer)
    w1 = math.fsum(T.ew[k] for k in cyc2)
    demand = -math.fsum(T.ew[k] for k in cyc2 if T.kind[k] != "grade")
    allowed = math.fsum(T.ew[k] for k in cyc2 if T.kind[k] == "grade")
    run = math.fsum(abs(float(cons.smp[T.ref[k][0]]["d"][T.ref[k][2]]) - float(cons.smp[T.ref[k][0]]["d"][T.ref[k][1]]))
                    for k in cyc2 if T.kind[k] == "grade")
    return dict(legs=legs, short_m=round(-w1, 3), demand_m=round(demand, 3), allowed_m=round(allowed, 3), run_m=round(run, 1),
                grade_factor=None if lam is None else round(lam, 3), constraints=len(best), first_cycle=len(cyc),
                searched=tests, groups=best, T=T, cycle=cyc2)


def check_levels(net, sysm, pcs, smp, ground, fixed_z, namer):
    """Step 3: do the system's constraints have heights? Fails with the irreducible contradiction, named and measured."""
    if not pcs:
        return _step(3, [], dict(nodes=0, edges=0, feasible=True))
    finds = []
    stats = {}
    result = {}
    trace = {}
    problem = gs.shape_problem(net, sysm, pcs, smp, ground, fixed_z, trace) + (trace,)
    for spacing in ("kernel", "true"):
        cons = Constraints(net, sysm, pcs, smp, ground, fixed_z, spacing, problem)
        g = cons.g
        kinds = {}
        for k in g.kind:
            kinds[k] = kinds.get(k, 0) + 1
        dist, cyc = _spfa(g)
        got = None
        if cyc is not None:
            w = math.fsum(g.ew[k] for k in cyc)
            got = (cyc, w) if w < -CONTRADICTION_M else None
        result[spacing] = (cons, got)
        stats[spacing] = dict(nodes=g.n - 1, edges=len(g.eu), kinds=kinds, feasible=got is None)
        if dist is not None:
            stats[spacing]["certificate_violation_m"] = certificate(g, dist)
    cons, got = result["kernel"]
    stats["feasible"] = got is None
    if got is not None:
        cont = contradiction(cons, got[0], namer)
        legs = cont["legs"]
        stats.update({k: v for k, v in cont.items() if k not in ("T", "cycle", "groups")})
        names = []
        for leg in legs:
            for r in leg["refs"]:
                if r not in names:
                    names.append(r)
        cr = [cons.g.ref[cons.g.groups[gid][0]][1] for gid in cont["groups"] if cons.g.kind[cons.g.groups[gid][0]] == "clear"
              and len(cons.g.ref[cons.g.groups[gid][0]]) > 1]
        where = namer.point(sysm["crossings"][cr[0]]["q"]) if cr else namer.at(cons.pcs[0]["main"], 0.0)
        tcons, tgot = result["true"]
        if tgot is None:
            note = "with every step's own run the system has heights: the kernel's uniform step is the cause"
        else:
            tcont = contradiction(tcons, tgot[0], namer)
            stats["true_spacing"] = {k: v for k, v in tcont.items() if k not in ("T", "cycle", "groups")}
            note = "with every step's own run it is still short by %.2f m" % tcont["short_m"]
        stats["true_spacing_feasible"] = tgot is None
        finds.append(Finding(3, "negative_cycle", "fail",
                             "no heights: %d constraints close %.2f m short (%.1f m asked over %.0f m of road that allows %.1f m, "
                             "the grade law %s as a factor of it); %s"
                             % (cont["constraints"], cont["short_m"], cont["demand_m"], cont["run_m"], cont["allowed_m"],
                                "alone cannot" if cont["grade_factor"] is None else "x%.2f" % cont["grade_factor"], note),
                             where, names))
    return _step(3, finds, stats)


# ---- step 4: where the level resets ----

def check_resets(net, systems, trace, namer):
    """Step 4 of every system: its size, what merged it, and where a ground node should have kept it apart. Returns
    {system index: step dict}, plus the sizes."""
    seeds_of = {}
    for n, sysm in enumerate(systems):
        for i in sysm["seeds"]:
            seeds_of[i] = n
    merges = {n: [] for n in range(len(systems))}
    for a, b, why, detail in trace.get("merges", ()):
        if a in seeds_of:
            merges[seeds_of[a]].append((a, b, why, detail))
    rels = {}

    def rel_of(i):
        if i not in rels:
            rels[i] = [r_ for r_ in gs._relations(net, i) if not gs._tunnel_network(net, r_[0])]
        return rels[i]
    out = {}
    for n, sysm in enumerate(systems):
        seeds = sysm["seeds"]
        pts = np.concatenate([net.lines[i] for i in seeds])
        lo, hi = pts.min(0), pts.max(0)
        extent = float(math.hypot(float(hi[0] - lo[0]), float(hi[1] - lo[1])))
        kinds = {}
        edges = []              # (a, b, why, detail, separable reason or None)
        seen = set()
        for a, b, why, detail in merges[n]:
            if a == b:
                continue
            kind = why if why != "relation" else detail[0]
            if why == "dip":
                detail = _shared_road(net, a, b, rel_of)
            # (a joint or crossing is found from both its parts, and two parts can be joined at both ends: one link
            # between them, whichever way, is the merge graph's edge)
            if (min(a, b), max(a, b)) in seen:
                continue
            seen.add((min(a, b), max(a, b)))
            kinds[kind] = kinds.get(kind, 0) + 1
            edges.append([a, b, kind, detail, None])
        # a road on the ground between two decks' ends: the level resets on it where nothing is over or under it
        dug = {d for d, _a, _b in sysm["dips"]}
        for e in edges:
            a, b, kind, j, _ = e
            if kind != "ramp":
                continue
            pa = [sj for jj, k, _si, sj, _q in rel_of(a) if jj == j and k == "join"]
            pb = [sj for jj, k, _si, sj, _q in rel_of(b) if jj == j and k == "join"]
            if not pa or not pb:
                continue
            sa, sb = min(pa, key=lambda s: min(abs(s - t) for t in pb)), None
            sb = min(pb, key=lambda t: abs(t - sa))
            lo_s, hi_s = sorted((sa, sb))
            # (anything structural touching the road between the two ends: a deck over it, a tunnel under it, a deck's
            # end on it: the road is not on its ground there)
            others = [k for k, _kind, s_on_j, _sk, _q in rel_of(j)
                      if net.m[k].kind in gs.STRUCTURES and k not in (a, b) and lo_s + gs.JOIN_M < s_on_j < hi_s - gs.JOIN_M]
            free = hi_s - lo_s
            if not others and j not in dug and free >= FREE_GROUND_M:
                e[4] = dict(road=j, s=(lo_s + hi_s) / 2.0, free_m=free)
            else:
                e[4] = dict(road=j, blocked=("a road dug under a deck" if j in dug else
                                             "%d part(s) over or under it between the ends" % len(others)
                                             if others else "only %.1f m between the ends" % free), free_m=free)
        # components without the merges a ground node separates; bridges of the merge graph
        adj = {i: [] for i in seeds}
        for k, (a, b, kind, detail, sep) in enumerate(edges):
            adj[a].append((b, k))
            adj[b].append((a, k))
        comp = _components(seeds, adj, {k for k, e in enumerate(edges) if e[4] and "s" in e[4]})
        finds = []
        size = sum(1 for i in seeds if net.m[i].kind == "deck") + len(sysm["dips"])
        size_fail = size > OVERSIZE_PIECES
        reset = [(k, e) for k, e in enumerate(edges) if e[4] and "s" in e[4]]
        for k, e in reset[:TOP * 4]:
            a, b, kind, j, info = e
            where = namer.at(j, info["s"])
            finds.append(Finding(4, "merged_over_a_ground_node", "fail",
                                 "%s and %s are one system through %s (%.0f m on the ground between their ends, nothing over "
                                 "or under it, a ground node at %s): the level resets there, find_systems joined them (a road "
                                 "of at most %.0f m between two ends)"
                                 % (namer.label(a), namer.label(b), namer.label(j), info["free_m"], where, gs.CONNECT_M),
                                 where, [namer.info(a), namer.info(b), namer.info(j)]))
        chain = []
        if size_fail:
            chain = _chain(net, seeds, edges, adj)
            br = _bridges(seeds, edges, adj)
            for side, k in br[:5]:
                a, b, kind, detail, sep = edges[k]
                where = _edge_where(net, namer, edges[k])
                finds.append(Finding(4, "merging_link", "fail",
                                     "oversized system (%d pieces, %d seeds, %.0f m across): the %s of %s and %s is the "
                                     "only link between two parts of %d and %d seeds%s"
                                     % (size, len(seeds), extent, _kind_words(kind, detail), namer.label(a), namer.label(b), side,
                                        len(seeds) - side, _why_no_reset(sep)), where,
                                     [namer.info(a), namer.info(b)] + ([namer.info(sep["road"])] if sep else [])
                                     + ([namer.info(detail[0])] if kind == "dip" and detail else [])))
            if not br:
                finds.append(Finding(4, "oversized", "fail",
                                     "oversized system (%d pieces, %d seeds, %.0f m across), no single link holds it "
                                     "together: it is a mesh of %d joints and crossings"
                                     % (size, len(seeds), extent, len(edges)), namer.at(seeds[0], 0.0), [namer.info(seeds[0])]))
        stats = dict(seeds=len(seeds), extent_m=round(extent, 0), merges=len(edges), by_kind=dict(sorted(kinds.items())),
                     components_if_reset=len(set(comp.values())), ground_nodes=len(reset),
                     crossings=len(sysm["crossings"]), joins=len(sysm["joins"]), ends=len(sysm["ends"]),
                     pieces=size, oversized=size_fail)
        # where the system ends: the roads on the ground it meets (find_systems' ends)
        boundary = ["%s meets %s at %s" % (namer.label(i), namer.label(j), namer.at(i, si))
                    for i, si, j, sj in sysm["ends"]]
        out[n] = _step(4, finds, stats, chain=[_edge_text(net, namer, edges[k]) for k in chain], boundary=boundary)
    return out


def _shared_road(net, a, b, rel_of):
    """(the road on the ground that decks a and b both cross over (the first by key), the crossing's point on a), or None:
    what a shared dip is about."""
    over_a = {j: q for j, kind, _si, _sj, q in rel_of(a) if kind == "cross" and net.m[j].kind == "ground"}
    both = [j for j, kind, _si, _sj, _q in rel_of(b) if kind == "cross" and j in over_a]
    if not both:
        return None
    j = min(both, key=lambda x: str(net.m[x].key))
    return j, over_a[j]


def _kind_words(kind, detail):
    return {"join": "joint", "cross": "crossing", "ramp": "ramp road", "dip": "shared dip"}.get(kind, kind)


def _why_no_reset(sep):
    if not sep:
        return " (no road on the ground between them: structure meets structure, no ground node)"
    if "s" in sep:
        return " (a ground node is on it)"
    return " (no ground node on the road between them: %s)" % sep["blocked"]


def _edge_where(net, namer, e):
    a, b, kind, detail, _sep = e
    if kind in ("join", "cross"):
        return namer.at(a, detail[1])
    if kind == "ramp":
        return namer.at(detail, net.L[detail] / 2.0)
    if kind == "dip" and detail:
        return namer.point(detail[1])
    return namer.at(a, net.L[a] / 2.0)


def _edge_text(net, namer, e):
    a, b, kind, detail, sep = e
    base = "%s: %s -> %s at %s" % (_kind_words(kind, detail), namer.label(a), namer.label(b), _edge_where(net, namer, e))
    if kind == "ramp":
        base += " via " + namer.label(detail)
    elif kind == "dip" and detail:
        base += " over " + namer.label(detail[0])
    return base


def _components(nodes, adj, skip):
    comp = {}
    for s in nodes:
        if s in comp:
            continue
        comp[s] = s
        stack = [s]
        while stack:
            v = stack.pop()
            for w, k in adj[v]:
                if k in skip or w in comp:
                    continue
                comp[w] = s
                stack.append(w)
    return comp


def _bridges(nodes, edges, adj):
    """[(size of the smaller side, edge index)] of the merge graph's bridges (the one link between two parts), the most
    even splits first. Iterative lowlink, the nodes and edges in index order."""
    order, low, parent_edge, size = {}, {}, {}, {}
    out = []
    for root in nodes:
        if root in order:
            continue
        stack = [(root, iter(adj[root]))]
        order[root] = low[root] = len(order)
        parent_edge[root] = None
        size[root] = 1
        total_nodes = [root]
        while stack:
            v, it = stack[-1]
            adv = False
            for w, k in it:
                if k == parent_edge[v]:
                    continue
                if w in order:
                    low[v] = min(low[v], order[w])
                else:
                    order[w] = low[w] = len(order)
                    parent_edge[w] = k
                    size[w] = 1
                    total_nodes.append(w)
                    stack.append((w, iter(adj[w])))
                    adv = True
                    break
            if adv:
                continue
            stack.pop()
            if stack:
                p = stack[-1][0]
                low[p] = min(low[p], low[v])
                size[p] += size[v]
                if low[v] > order[p]:
                    out.append((v, parent_edge[v]))
        n_comp = len(total_nodes)
        out = [(min(size[v], n_comp - size[v]), k) if v in size else (0, k) for v, k in out]
    out.sort(key=lambda t: (-t[0], t[1]))
    return out


def _chain(net, seeds, edges, adj):
    """Edge indices of the merge graph's path between the two seeds farthest apart in plan (the first found through the
    one farthest from the first seed), fewest links."""
    mid = {i: net.lines[i][len(net.lines[i]) // 2] for i in seeds}

    def far(src):
        return max(seeds, key=lambda i: (float(math.hypot(float(mid[i][0] - mid[src][0]), float(mid[i][1] - mid[src][1]))),
                                         str(net.m[i].key)))
    u = far(seeds[0])
    v = far(u)
    prev = {u: None}
    queue = deque([u])
    while queue and v not in prev:
        x = queue.popleft()
        for w, k in adj[x]:
            if w not in prev:
                prev[w] = (x, k)
                queue.append(w)
    path = []
    while prev.get(v):
        x, k = prev[v]
        path.append(k)
        v = x
    return path[::-1]


# ---- the whole audit ----

def audit_system(net, sysm, pcs, smp, ground, fixed_z, namer, rels=None):
    """Steps 1 to 3 of one system (step 4 is the region's: audit_region)."""
    return {1: check_roles(net, sysm, namer, rels), 2: check_pieces(net, sysm, pcs, smp, namer, rels),
            3: check_levels(net, sysm, pcs, smp, ground, fixed_z, namer)}


def audit_region(net, systems, trace, namer, per_system):
    """Every system's four steps: per_system {index: audit_system's answer} (a system not solved is missing: steps 1, 2
    and 3 not run on it)."""
    resets = check_resets(net, systems, trace, namer)
    out = []
    for n, sysm in enumerate(systems):
        steps = dict(per_system.get(n, {}))
        steps[4] = resets[n]
        out.append(dict(index=n, seeds=len(sysm["seeds"]), pieces=steps[4]["stats"]["pieces"], crossings=len(sysm["crossings"]), joins=len(sysm["joins"]),
                        ends=len(sysm["ends"]), dips=len(sysm["dips"]), steps=steps,
                        first=namer.label(sysm["seeds"][0]), where=namer.at(sysm["seeds"][0], 0.0)))
    return out


def audit_members(members, ground=None, make_fixed=None, ll=None, meta=None):
    """The whole audit of a set of gradeshapes members on flat ground (or ground(xy) -> z): find_systems, its pieces and
    samples, the four steps. make_fixed(net) -> fixed(i, s) (the height of what the shapes do not move: a road at its ground,
    by default). Returns (net, systems, audits, namer)."""
    ground = ground or (lambda xy: np.zeros(len(xy)))

    def fixed_of(net):
        if make_fixed is not None:
            return make_fixed(net)

        def f(i, s):
            sv = np.atleast_1d(np.asarray(s, np.float64))
            z = ground(np.stack([np.interp(sv, net.s[i], net.lines[i][:, 0]), np.interp(sv, net.s[i], net.lines[i][:, 1])],
                                axis=1))
            return np.asarray(z, np.float64) if np.ndim(s) else float(z[0])
        return f
    trace = {}
    net, systems = gs.find_systems(members, lambda n_: (ground, fixed_of(n_)), trace)
    namer = Namer(net, ll, meta)
    rels = {}
    per = {}
    fixed = fixed_of(net)
    for n, sysm in enumerate(systems):
        pcs = gs.pieces(net, sysm)
        smp = gs.sample(net, pcs)
        per[n] = audit_system(net, sysm, pcs, smp, [ground(x["xy"]) for x in smp], fixed, namer, rels)
    return net, systems, audit_region(net, systems, trace, namer, per), namer


# ---- the report ----

def summary(audits):
    """{step: (systems passing, failing, not run)} and the findings by code."""
    out = {}
    codes = {}
    for step in (1, 2, 3, 4):
        ok = bad = none = 0
        for a in audits:
            s = a["steps"].get(step)
            if s is None:
                none += 1
            elif s["ok"]:
                ok += 1
            else:
                bad += 1
            for f in (s or {}).get("findings", ()):
                if f.level == "fail":
                    codes.setdefault((step, f.code), []).append((a["index"], f))
        out[step] = (ok, bad, none)
    return out, codes


STEP_NAMES = {1: "in / out", 2: "pieces", 3: "levels", 4: "where the level resets"}


def render(place, audits, header=()):
    """Markdown of one place's audit."""
    sm, codes = summary(audits)
    lines = ["# Interchange audit: %s" % place, ""]
    lines += list(header) + [""] if header else []
    lines += ["%d systems." % len(audits), "", "| step | pass | fail | not run |", "|---|---|---|---|"]
    for step in (1, 2, 3, 4):
        lines.append("| %d %s | %d | %d | %d |" % ((step, STEP_NAMES[step]) + sm[step]))
    lines.append("")
    first = next((s for s in (1, 2, 3, 4) if sm[s][1]), None)
    lines.append("First step to fail: %s." % ("none" if first is None else "step %d (%s), in %d of %d systems"
                                                % (first, STEP_NAMES[first], sm[first][1], len(audits))))
    lines.append("")
    for step in (1, 2, 3, 4):
        lines += ["## Step %d: %s" % (step, STEP_NAMES[step]), ""]
        mine = sorted(((c, v) for (s, c), v in codes.items() if s == step), key=lambda t: (-len(t[1]), t[0]))
        if not mine:
            lines += ["Pass in every system%s." % (" that ran" if sm[step][2] else ""), ""]
        for code, items in mine:
            lines += ["### %s: %d" % (code, len(items)), ""]
            for idx, f in items[:TOP]:
                lines.append("- system %d: %s" % (idx, f.text))
            if len(items) > TOP:
                lines.append("- ... and %d more" % (len(items) - TOP))
            lines.append("")
        notes = {}
        for a in audits:
            for f in (a["steps"].get(step) or {}).get("findings", ()):
                if f.level == "note":
                    notes.setdefault(f.code, []).append((a["index"], f))
        for code, items in sorted(notes.items()):
            lines += ["Note, %s: %d (not a failure)." % (code, len(items))] +                      ["- system %d: %s" % (idx, f.text) for idx, f in items[:3]] + [""]
        if step == 3:
            big = [a for a in audits if a["steps"].get(3) and a["steps"][3]["stats"].get("kernel") and a["seeds"] >= 8]
            if big:
                lines += ["### Exact feasibility of the largest systems (difference constraints on the kernel's own rows; "
                          "independent of any solver status)", "",
                          "| system | seeds | pieces | nodes | edges | kernel's step | each step's own run | certificate violation (m) |",
                          "|---|---|---|---|---|---|---|---|"]
                for a in big:
                    st = a["steps"][3]["stats"]
                    k, t = st["kernel"], st.get("true") or {}
                    lines.append("| %d | %d | %d | %d | %d | %s | %s | %s |" % (
                        a["index"], a["seeds"], a["pieces"], k["nodes"], k["edges"],
                        "feasible" if k["feasible"] else "INFEASIBLE", "feasible" if t.get("feasible") else "INFEASIBLE",
                        "%.1e" % k["certificate_violation_m"] if "certificate_violation_m" in k else "-"))
                lines.append("")
            for a in audits:
                s = a["steps"].get(3)
                if s and s["stats"].get("legs"):
                    st = s["stats"]
                    lines += ["### Contradiction, system %d (%d seeds, %d pieces)" % (a["index"], a["seeds"], a["pieces"]), "",
                              "%d constraints that no heights satisfy (the first cycle found had %d edges, reduced until no "
                              "constraint of it can be left out). They ask %.2f m of rise over %.0f m of road that allows %.2f m: "
                              "short by %.3f m%s. The cycle:"
                              % (st["constraints"], st["first_cycle"], st["demand_m"], st["run_m"], st["allowed_m"], st["short_m"],
                                 "; the grade law would have to be x%.2f of what it is" % st["grade_factor"]
                                 if st["grade_factor"] else "; no grade law alone gives them heights"), ""]
                    run = 0.0
                    for n, leg in enumerate(st["legs"], 1):
                        run += leg["w"]
                        lines.append("%d. %s (bound %+.3f m, running %+.3f m)" % (n, leg["text"], leg["w"], run))
                    lines.append("")
                    ts = st.get("true_spacing")
                    lines += ["With each step's own run (not the kernel's closest two samples) the system %s." %
                              ("has heights: the kernel's uniform step makes the contradiction" if st.get("true_spacing_feasible")
                               else "is still short by %.3f m (%d constraints)" % (ts["short_m"], ts["constraints"])), ""]
        if step == 4:
            for a in audits:
                s = a["steps"].get(4)
                if s and s["stats"]["oversized"]:
                    lines += ["### Chain through system %d (%d seeds)" % (a["index"], a["seeds"]), ""]
                    lines += ["%d. %s" % (n, t) for n, t in enumerate(s.get("chain", ()), 1)] + [""]
                if s and (s["stats"]["oversized"] or s["stats"]["seeds"] >= 8):
                    lines += ["### Where system %d ends (%d roads on the ground)" % (a["index"], len(s["boundary"])), ""]
                    lines += ["- %s" % t for t in s["boundary"][:TOP * 2]] + ["" if len(s["boundary"]) <= TOP * 2 else
                                                                               "- ... and %d more" % (len(s["boundary"]) - TOP * 2), ""]
    lines += ["## Systems", "", "| # | seeds | crossings | joins | ends | first member | at | 1 | 2 | 3 | 4 |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for a in sorted(audits, key=lambda a: (-a["seeds"], a["index"]))[:60]:
        marks = ["" if a["steps"].get(s) is None else ("pass" if a["steps"][s]["ok"] else "FAIL") for s in (1, 2, 3, 4)]
        lines.append("| %d | %d | %d | %d | %d | %s | %s | %s |" % (a["index"], a["seeds"], a["crossings"], a["joins"], a["ends"],
                                                                    a["first"], a["where"], " | ".join(marks)))
    return "\n".join(lines) + "\n"
