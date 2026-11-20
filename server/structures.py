"""Road revamp track RS, "one structure per level" (.claude/work/road-revamp-structures-design-2026-11-18.md): the glue
of the Rust kernels in native/earthfast/src/structures.

Step S1 (earthfast 480): the structure groups of a bridge chain (earthfast.structure_groups: union-find over the chain's
decks with the design's edge table, never across OSM levels, walkways, rail, tunnels; data conflicts recorded).

Step S2a (earthfast 481), the groundwork of the joint height solve; BNE_DECK_STRUCT=shadow is the switch path:
- ONE OWNER: a chain's closure (roads.BridgeProfiles._resolve) also holds every pair of deck parts that could be one
  structure whatever their heights and outlines (touch: earthfast.structure_touch, the grouping's own edge tests up to
  the design's 6 m with every height limit and outline passing) and reads the cells within reach of the chain (halo),
  so every structure edge lies inside one chain and a structure has the chain's owner. S2a fix (BNE_DECK_STRUCT 3):
  the reach allows for the widest deck the kernels take (HALF_MAX_M: half widths capped there), so discovery is the
  same from either deck of a pair; the halo's cells have their own budget (roads.CHAIN_HALO_CELLS), apart from the
  CHAIN_CELLS cells that hold the chain's parts, so empty cells near a cell edge never put a chain in window solves. Retires, on the switch path, the
  closure's 1.5 m beside touch (MERGE_TOUCH_M) as what decides which decks share a solve: pairs that were one structure
  lay in two chains at S1 ("outside" conflicts).
- Members are unique (the kernel's duplicates: one way kept twice under two part keys by two cells' copies).
- Levels: a part's grouping level is its Overture level where level rules cover most of it, else its OSM layer, else 1
  (part_levels; before: the Overture level or 1, so every unmapped layer was "level 1").
- The diagnostics (one JSON per chain under the cache's debug/structures/ folder; nothing reads it) come only from
  the solve that is PUBLISHED: the chain's canonical owner solve (CANON_LOAD) whose parts and structure are the ones
  in `done` / `trusses`, read complete (published(): never a staged, losing, short or fallback solve). The grouping
  and the file are made OFF the chain's critical path: published() hands an immutable snapshot to a bounded
  background writer (one thread per process, its own revisit scope: it never faults a build, never blocks a solve; a
  full queue drops the job, counted in STATS). The file's name holds the chain's key and the digest of its effective
  inputs (members, heights, outlines, frame), so two workers with different snapshots never overwrite each other.

Step S2b part 1 (earthfast 488, BNE_DECK_STRUCT 4), still shadow only:
- Chain discovery is a FIXPOINT (roads.BridgeProfiles._closure): a part's pairs count only once every cell of its line
  and halo is read, nothing is capped or windowed before the closure is complete, and the answer is a function of
  that complete local set (the chain with exactly its own cells + halo read). A window solve's live output (its
  decks, its structure: trusses, their piers) is the legacy path's exactly; the switch only writes diagnostics there.
- ONE joint height solve per structure (heights(): earthfast.structure_heights): the chain profile solved WITHOUT the
  pairwise mechanisms (roads._merge_overlaps / DeckMerge / DUAL_DECK_M, roads._tie_beside; BridgeProfiles._raw_solve,
  in the writer) coupled per group edge, windows and eases, abutments pinned, clearances as an active set, a fallback
  to today's heights recorded, also over the native solve's memory and work budgets (status "budget": checked counts
  before anything is allocated, heights.rs Budget). The JSON gets each member's z_in_mm and z_joint_mm beside
  today's z_mm and a "joint" section (per structure: status, couplings, metrics today vs joint). Nothing is published
  from it.

Step S2b part 2a (earthfast 493, BNE_DECK_STRUCT 5): BNE_DECK_STRUCT=abut is shadow plus ONE abutment height per
structure end in the PLANS: the joint solve pins each deck's abutments where the plans put them (each deck's own
approach road), so twins stayed apart at their ends (Ottawa 0.6-0.7 m, E6 Vist 0.43 m). earthfast.structure_abutments
groups the abutments of decks that are one structure side by side (BESIDE / OVERLAP_ALONG, height-free) facing one way
and gives each end group max(mean ground estimate, highest clearance need); grading._pin_struct_ends brings every
approach road's abutment end to it (eased out along the road), so the decks' pins agree. The end group is a closure over
its ends' cells (halo), the same from whichever end and cell order. Its plans have their own format
(plancache.plans_format: ".ds5").

off (the default) builds the same plans, facts and tiles as before (no code of this module runs). shadow's closure
can put more parts in one chain solve (its heights solved together): the switch is in every cache key (revisit:
SWITCH_VERSIONS, not PLAN_NEUTRAL).

The JSON (FORMAT): the chain's key, the store's frame, the inputs' digest, the members (key, class, level and its
source, kind, half width, length, group, the member it duplicates, line as lat/lon and heights), the groups (members,
the edges inside, a digest), every edge (its kind, |dz|, whether it united two groups), the pairs over a height limit,
the data conflicts (why they never join), the chain's digest. The digests hold the member keys, edge kinds and |dz|
in mm, and the in-chain conflicts: the same across start tiles, cell orders, processes and platforms
(tests/test_structures.py)."""
import atexit
import hashlib
import json
import logging
import os
import queue
import re
import threading
import time

import numpy as np

log = logging.getLogger("earthd.structures")

MODE = os.environ.get("BNE_DECK_STRUCT", "off")         # off / shadow (S1, S2a, S2b part 1) / abut (S2b part 2a)
# abut: shadow, and the plans give the decks of one structure side by side one abutment height per common end
# (grading._struct_end / _pin_struct_ends, earthfast.structure_abutments): it changes plans (plancache.plans_format)
ABUT = MODE == "abut"
SHADOW = MODE == "shadow" or ABUT
FORMAT = 3                  # (3: the joint heights, S2b part 1)
EDGE_KINDS = ("JOIN", "END_ON", "OVERLAP_ALONG", "OVERLAP_CROSS", "BESIDE", "BESIDE_OUTLINE")
REASONS = ("level", "dz", "walk", "rail", "tunnel", "outside")
ROAD, WALK, RAIL, TUNNEL = 0, 1, 2, 3
KIND_NAMES = ("road", "walk", "rail", "tunnel")
WALK_CLASSES = frozenset(("footway", "path", "cycleway", "pedestrian", "steps", "bridleway", "corridor", "track_foot"))
OUTSIDE_NEAR_M = 8.0    # m (+ the widest half width) round the chain's box: the pool's other parts offered as outside
TOUCH_M = 6.0           # m edge to edge: the farthest structure edge (the design's; the kernel's OUTLINE_GAP_M)
# m: the widest half width the grouping and the closure take (structure_touch / structure_groups' half widths are
# capped at it: a wider deck groups as a 60 m one). It bounds how far a deck's centre line can lie from a chain line
# and still be one structure with it, so the closure's cell halo (halo) reaches every such deck from EITHER side.
# S2a's halo allowed for a 40 m other deck while the specs make wider ones (lanes x lane width, no cap) and the
# kernels took their real widths: a 60 m deck found an 8 m one beside it across a cell edge, never the other way
# round, and the chain published first decided the structure (Codex review of S2a). The structures corpus's widest
# deck: 26 m (SF's Yerba Buena stack).
HALF_MAX_M = 30.0
DEBUG_DIR = ("debug", "structures")
QUEUE_MAX = 4096        # jobs waiting for the writer (more: dropped, STATS["dropped"], a dropped-<pid>.json marker)
LEVEL_SOURCES = ("overture", "osm", "default")
STATS = {"queued": 0, "written": 0, "dropped": 0, "failed": 0, "skipped": 0}
# A developer's probe (structprobe.py: offline corpus seeds without the sea's bathymetry make every coastal chain's
# solve short) may take the published solve's JSON when it read something short too, marked "short" (its file name
# too). Never set by the server: the published solve's JSON only when it is complete.
ACCEPT_SHORT = False


# ---- inputs -------------------------------------------------------------------------------------------------------

def part_levels(feats, i, spans, rings):
    """The grouping level of each bridge part of feature i (bridge_levels' order: spans [(start, end, level)] repeated
    per ring), with its source: the Overture level where the feature's level rules cover at least half the span (the
    level roads.bridge_levels gives it), else the feature's OSM layer (>= 1, as a bridge's level), else the span's
    level (1 unless mapped). [(level, source)].
    The switch path's alone (roads.BridgeProfiles._cell_entries)."""
    t = getattr(feats, "rules", {}).get("level")
    rows = list(t.rows(i)) if t is not None and len(t.feature) else []
    layer = feats.attrs.get("osm_layer")
    osm = None
    if layer is not None:
        try:
            v = float(layer[i])
        except (TypeError, ValueError):
            v = float("nan")
        if np.isfinite(v):
            osm = max(1.0, float(int(v)))
    out = []
    for a, b, lv in spans:
        # (the share of the span the rules cover: the earthfast span builder merges an explicit level next to an
        # uncovered stretch of the same default level, so a rule over part of it is the span's level only over at
        # least half of it, Codex review)
        a, b = float(a), float(b)
        cov = sorted((max(a, float(t.start[r])), min(b, float(t.end[r]))) for r in rows if _valid_rule(t, r))
        got, upto = 0.0, a
        for c0, c1 in cov:
            c0 = max(c0, upto)
            if c1 > c0:
                got += c1 - c0
                upto = c1
        if b > a and got >= 0.5 * (b - a):
            out.append((float(lv), 0))
        elif osm is not None:
            out.append((osm, 1))
        else:
            out.append((float(lv), 2))
    return out * rings


def _valid_rule(t, r):
    """Level rule row r as earthfast's span builder takes it (levels.rs level_rule): a number, a range not empty;
    a row it ignores covers nothing here either (Codex review: a NaN row hid the OSM layer)."""
    try:
        v, a, b = float(str(t.value[r])), float(t.start[r]), float(t.end[r])
    except (TypeError, ValueError):
        return False
    return bool(np.isfinite(v) and np.isfinite(a) and np.isfinite(b) and b > a)


def level_of(v):
    """A pool entry's grouping level (part_levels' where the switch's _cell_entries kept it, else the store's)."""
    return float(v[6][0]) if len(v) > 6 else float(v[3])


def level_source(v):
    return LEVEL_SOURCES[v[6][1]] if len(v) > 6 else None


def half_of(width):
    """A deck's half width as the grouping and the closure take it: capped at HALF_MAX_M."""
    return min(float(width) / 2, HALF_MAX_M)


def kind_of(spec):
    return WALK if str(getattr(spec, "cls", "")) in WALK_CLASSES else ROAD


def touch(entries, keys):
    """(a, b), a < b (indices into keys): the deck parts (pool entries, in keys' order) that could be one structure
    whatever their heights and outlines (earthfast.structure_touch: the grouping's own tests with every height limit
    and outline test passing): BridgeProfiles._resolve solves them in one chain."""
    import fast
    fast.require(481, "structure_touch")
    if len(entries) < 2:
        return []
    pts, off = [], [0]
    for v in entries:
        xy = np.asarray(v[1], np.float64)[:, :2]
        pts.append(xy)
        off.append(off[-1] + len(xy))
    fids = sorted({str(k[0]) for k in keys})
    fid_of = {f: n for n, f in enumerate(fids)}
    got = fast._ef.structure_touch(
        np.ascontiguousarray(np.concatenate(pts)), np.asarray(off, np.int64),
        np.asarray([half_of(v[0].width) for v in entries], np.float64),
        np.asarray([kind_of(v[0]) for v in entries], np.int64),
        np.asarray([level_of(v) for v in entries], np.float64),
        np.asarray([fid_of[str(k[0])] for k in keys], np.int64))
    return [(int(a), int(b)) for a, b in got]


def halo(xy, half):
    """Points whose map cells hold every cell within reach of the chain line points xy (k, 2, one part's line) of half
    width half: reach = min(half, HALF_MAX_M) + HALF_MAX_M + TOUCH_M (+ 10 %), the farthest any deck's centre line
    (its half width capped at HALF_MAX_M, as the kernels take it) can lie from this line and still be one structure
    with it: the same bound seen from either deck of a pair. The line is sampled every reach / 2 and each sample
    gives the corners and edge middles of the square of side 2 x reach round it (far smaller than a map cell: the
    cells it touches are its corners' and middles'), so the union of the squares covers the buffer; Codex review:
    eight fixed offsets missed a wide deck's cell and cell corners."""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    if not len(xy):
        return xy
    reach = 1.1 * (min(float(half), HALF_MAX_M) + HALF_MAX_M + TOUCH_M)
    pts = [xy]
    seg = np.diff(xy, axis=0)
    n = np.ceil(np.sqrt(seg[:, 0] * seg[:, 0] + seg[:, 1] * seg[:, 1]) / (reach / 2)).astype(np.int64)
    for k in np.nonzero(n > 1)[0]:
        t = (np.arange(1, n[k]) / n[k])[:, None]
        pts.append(xy[k] + t * seg[k])
    q = np.concatenate(pts)
    offs = reach * np.array([(1, 1), (1, -1), (-1, 1), (-1, -1), (1, 0), (-1, 0), (0, 1), (0, -1)], np.float64)
    return np.concatenate([q] + [q + o for o in offs])


def groups(members, outlines=()):
    """The structure groups of members [{"key", "fid", "xy" (k, 2), "z" (k,), "half", "kind", "level", "outside"}] in
    their keys' order, the chain's first (earthfast.structure_groups); outlines: OSM bridge outline rings (k, 2) in
    the same frame. {"root": [index of each member's group root], "dup": [the member each duplicates (itself: none)],
    "edges": [(a, b, kind, united, dz)], "rejected": [(a, b, kind, dz)], "conflicts": [(a, b, reason, kind, dz)]}
    (indices into members, kinds / reasons as numbers: EDGE_KINDS, REASONS)."""
    import fast
    fast.require(481, "structure_groups")
    pts, off = [], [0]
    for m in members:
        xy = np.asarray(m["xy"], np.float64).reshape(-1, 2)
        z = np.asarray(m["z"], np.float64).reshape(-1)
        pts.append(np.c_[xy, z])
        off.append(off[-1] + len(xy))
    fids = sorted({str(m["fid"]) for m in members})
    fid_of = {f: k for k, f in enumerate(fids)}
    rings = [np.asarray(o, np.float64).reshape(-1, 2) for o in outlines if len(o) >= 3]
    ooff = np.r_[0, np.cumsum([len(r) for r in rings])].astype(np.int64)
    root, dup, edges, rejected, conflicts = fast._ef.structure_groups(
        np.ascontiguousarray(np.concatenate(pts) if pts else np.zeros((0, 3))), np.asarray(off, np.int64),
        np.asarray([half_of(2.0 * float(m["half"])) for m in members], np.float64),
        np.asarray([int(m["kind"]) for m in members], np.int64),
        np.asarray([np.nan if m.get("level") is None else float(m["level"]) for m in members], np.float64),
        np.asarray([fid_of[str(m["fid"])] for m in members], np.int64),
        np.asarray([1 if m.get("outside") else 0 for m in members], np.int64),
        np.ascontiguousarray(np.concatenate(rings) if rings else np.zeros((0, 2))), ooff)
    return {"root": list(root), "dup": list(dup), "edges": edges, "rejected": rejected, "conflicts": conflicts}


def _mm(v):
    return None if not np.isfinite(v) else int(round(float(v) * 1000.0))


def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def document(members, got, n_chain, ll=None):
    """The shadow JSON's body of groups() got for members (the first n_chain the chain's own, the rest outside).
    ll(xy (k, 2)) -> [[lat, lon]] for the members' lines (None: left out)."""
    keys = [m["key"] for m in members]
    root = got["root"]
    dup = got.get("dup") or list(range(len(members)))
    by = {}
    for i in range(n_chain):
        if dup[i] == i:
            by.setdefault(root[i], []).append(i)
    edges = [{"a": keys[a], "b": keys[b], "kind": EDGE_KINDS[k], "dz": _mm(dz), "united": bool(u)}
             for a, b, k, u, dz in got["edges"]]
    out_groups = []
    for r in sorted(by):
        mem = by[r]
        inner = sorted([EDGE_KINDS[k], keys[a], keys[b], _mm(dz)] for a, b, k, _u, dz in got["edges"]
                       if root[a] == r)
        body = {"members": [keys[i] for i in mem], "edges": inner}
        dups = sorted(keys[i] for i in range(n_chain) if dup[i] != i and root[i] == r)
        out_groups.append({"id": keys[r], "n": len(mem), "members": body["members"], "edges": inner, "dups": dups,
                           "classes": sorted({str(members[i]["cls"]) for i in mem}),
                           "levels": sorted({members[i]["level"] for i in mem if members[i].get("level") is not None}),
                           "digest": _digest(body)})
    conflicts = [{"a": keys[a], "b": keys[b], "reason": REASONS[r], "kind": EDGE_KINDS[k], "dz": _mm(dz)}
                 for a, b, r, k, dz in got["conflicts"]]
    rejected = [{"a": keys[a], "b": keys[b], "kind": EDGE_KINDS[k], "dz": _mm(dz)} for a, b, k, dz in got["rejected"]]
    own = sorted([c["reason"], c["kind"], c["a"], c["b"], c["dz"]] for c in conflicts if c["reason"] != "outside")
    mlist = []
    for i, m in enumerate(members):
        xy = np.asarray(m["xy"], np.float64).reshape(-1, 2)
        z = np.asarray(m["z"], np.float64).reshape(-1)
        d = np.diff(xy, axis=0)
        s = float(np.sqrt(d[:, 0] * d[:, 0] + d[:, 1] * d[:, 1]).sum()) if len(xy) > 1 else 0.0
        item = {"key": keys[i], "cls": str(m["cls"]), "level": m.get("level"), "level_src": m.get("level_src"),
                "kind": "outside" if m.get("outside") else KIND_NAMES[int(m["kind"])],
                "half": _mm(m["half"]), "len_m": round(s, 1), "group": keys[root[i]] if i < n_chain else None,
                "dup_of": keys[dup[i]] if dup[i] != i else None, "z_mm": [_mm(v) for v in z]}
        if ll is not None:
            item["ll"] = ll(xy)
        mlist.append(item)
    return {"format": FORMAT, "members": mlist, "groups": out_groups, "edges": edges, "rejected": rejected,
            "conflicts": conflicts,
            "digest": _digest({"groups": [g["digest"] for g in out_groups], "conflicts": own})}


STATUS = ("solved", "pinned", "infeasible", "clearance", "grade", "solver", "budget")   # (budget: earthfast's limits)
CLASS_DECK = -1         # earthfast.structure_heights' class code of a deck over a deck of the chain


def heights(members, n_chain, got, raw):
    """S2b part 1 (shadow): the joint heights of a chain's structures (earthfast.structure_heights) and what they WOULD
    give against today's. members / n_chain / got: _chain_job's (the chain's first n_chain members, today's heights in
    "z"); raw: roads.BridgeProfiles._raw_solve's (the profile without the pairwise mechanisms "z", the crossings'
    "floors" (part index, x, y, floor m, class), per part "under" m, class "rank", "link"). A dict for the JSON:
    {"z_joint": [mm per vertex] per member (outside members: None), "structures": [per group: its root's key, status,
    couplings, pairs dropped by a pinned end, clearance windows, unmet ones, rounds, today's and the joint metrics in mm
    (twin_dz; twin_free: the twin pairs not within their ease of a pinned abutment; seam) and grade change, clearance
    margins per class in mm], "conflicts_same_level": the groups' same-level
    dz conflicts (never coupled: recorded), "classes"}."""
    import fast
    fast.require(488, "structure_heights")
    ms = members[:n_chain]
    pts, off, zin, today = [], [0], [], []
    for m, z in zip(ms, raw["z"]):
        xy = np.asarray(m["xy"], np.float64).reshape(-1, 2)
        z = np.asarray(z, np.float64).reshape(-1)
        t = np.asarray(m["z"], np.float64).reshape(-1)
        if len(z) != len(xy):
            z = np.full(len(xy), np.nan)
        pts.append(xy)
        zin.append(z)
        today.append(t)
        off.append(off[-1] + len(xy))
    root = [int(r) for r in got["root"][:n_chain]]
    dup = [int(d) for d in (got.get("dup") or list(range(len(members))))[:n_chain]]
    root = [r if r < n_chain else i for i, r in enumerate(root)]
    dup = [d if d < n_chain else i for i, d in enumerate(dup)]
    edges = [(int(a), int(b), int(k)) for a, b, k, _u, _dz in got["edges"]
             if a < n_chain and b < n_chain and root[a] == root[b]]
    classes = sorted({str(f[4]) for f in raw["floors"]})
    code = {c: k for k, c in enumerate(classes)}
    fl = [f for f in raw["floors"] if 0 <= int(f[0]) < n_chain]
    z, rows, mt, mj, ct, cj = fast._ef.structure_heights(
        np.ascontiguousarray(np.concatenate(pts) if pts else np.zeros((0, 2))), np.asarray(off, np.int64),
        np.ascontiguousarray(np.concatenate(zin) if zin else np.zeros(0)),
        np.ascontiguousarray(np.concatenate(today) if today else np.zeros(0)),
        np.asarray([half_of(2.0 * float(m["half"])) for m in ms], np.float64),
        np.asarray([np.nan if m.get("level") is None else float(m["level"]) for m in ms], np.float64),
        np.asarray(root, np.int64), np.asarray(dup, np.int64),
        np.asarray(raw["rank"], np.int64), np.asarray([1 if v else 0 for v in raw["link"]], np.int64),
        np.asarray(raw["under"], np.float64), np.asarray(edges, np.int64).reshape(-1, 3),
        np.asarray([(f[1], f[2], f[3]) for f in fl], np.float64).reshape(-1, 3),
        np.asarray([int(f[0]) for f in fl], np.int64), np.asarray([code[str(f[4])] for f in fl], np.int64))
    keys = [m["key"] for m in members]
    cname = lambda c: "deck" if c == CLASS_DECK else classes[c]      # noqa: E731
    met_t = {r: (a, f, b, c) for r, a, f, b, c in mt}
    met_j = {r: (a, f, b, c) for r, a, f, b, c in mj}
    cl_t, cl_j = {}, {}
    for out, rows_ in ((cl_t, ct), (cl_j, cj)):
        for r, c, v in rows_:
            out.setdefault(r, {})[cname(c)] = _mm(v)
    structs = []
    for r, st, npairs, pruned, cwin, unmet, rounds, resid in rows:
        a, f, b, c = met_t.get(r, (0.0, 0.0, 0.0, 0.0))
        a2, f2, b2, c2 = met_j.get(r, (0.0, 0.0, 0.0, 0.0))
        structs.append({"id": keys[r], "status": STATUS[st], "pairs": int(npairs), "pruned": int(pruned),
                        "cwin": int(cwin), "unmet": int(unmet), "rounds": int(rounds), "resid_mm": _mm(resid),
                        "members": sum(1 for i in range(n_chain) if root[i] == r and dup[i] == i),
                        "today": {"twin_dz_mm": _mm(a), "twin_free_mm": _mm(f), "seam_mm": _mm(b),
                                  "grade_chg": round(float(c), 4), "clear_mm": cl_t.get(r, {})},
                        "joint": {"twin_dz_mm": _mm(a2), "twin_free_mm": _mm(f2), "seam_mm": _mm(b2),
                                  "grade_chg": round(float(c2), 4), "clear_mm": cl_j.get(r, {})}})
    zj = []
    for i in range(len(members)):
        if i < n_chain:
            zj.append([_mm(v) for v in z[off[i]:off[i + 1]]])
        else:
            zj.append(None)
    same = sum(1 for _a, _b, rs, _k, _dz in got["conflicts"] if REASONS[rs] == "dz")
    return {"z_joint": zj, "structures": structs, "classes": classes, "conflicts_same_level": same,
            "policy": "same-level dz conflicts and rejected pairs are never coupled (recorded)",
            "digest": _digest([zj[:n_chain], structs])}


def _outline_rings(infra):
    """The OSM bridge outline rings (man_made=bridge: the infrastructure layer's bridge polygons) of the
    infrastructure Features infra as lon/lat arrays, each feature once (by its id), in a canonical order. In the
    solving thread: copies, nothing read that the solve's structure had not read."""
    seen = {}
    for inf in infra or ():
        if inf is None or not len(inf):
            continue
        sub = inf.attrs.get("subtype")
        if sub is None:
            continue
        for i in range(len(inf.kind)):
            if inf.kind[i] != 3 or str(sub[i]) != "bridge":
                continue
            fid = str(inf.ids[i])
            if fid in seen:
                continue
            seen[fid] = [np.array(part[0], np.float64) for part in inf.parts(i) if len(part[0]) >= 4]
    return [r for f in sorted(seen) for r in seen[f]]


def _name(ck, block, digest):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{ck[0]}_{ck[1]}")[:120]
    return s + (f"_w{block[0]}_{block[1]}" if block is not None else "") + f"_{digest}.json"


def _write(cache_dir, name, doc):
    """doc as the JSON name in cache_dir's debug/structures/ folder (written whole: a new file renamed into place)."""
    d = os.path.join(cache_dir, *DEBUG_DIR)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, sort_keys=True, separators=(",", ":"), allow_nan=False)
        f.write("\n")
    os.replace(tmp, path)
    return path


def _input_digest(members, rings, frame, extra=b""):
    """The digest of a chain's effective inputs: its members (key, class, kind, level, half width, the line and its
    heights in mm, outside), the outline rings (lon/lat to 1e-7 degree), the frame and extra (S2b: the joint solve's
    inputs)."""
    h = hashlib.sha256(extra)
    h.update(repr((round(float(frame.lat0), 9), round(float(frame.lon0), 9))).encode())
    for m in members:
        h.update(repr((m["key"], m["cls"], int(m["kind"]), m.get("level"), _mm(m["half"]),
                       bool(m.get("outside")))).encode())
        xy = np.asarray(m["xy"], np.float64)
        z = np.asarray(m["z"], np.float64)
        h.update(np.round(xy * 1000.0).astype(np.int64).tobytes())
        h.update(np.where(np.isfinite(z), np.round(np.nan_to_num(z) * 1000.0), -(1 << 62)).astype(np.int64).tobytes())
    for r in rings:
        h.update(b"|")
        h.update(np.round(np.asarray(r, np.float64) * 1e7).astype(np.int64).tobytes())
    return h.hexdigest()[:16]


# ---- the background writer ----------------------------------------------------------------------------------------

_wlock = threading.Lock()
_writer = {"pid": None, "q": None, "thread": None}


def _queue():
    """This process's writer queue (a new thread after a fork: the parent's is not running here)."""
    with _wlock:
        if _writer["pid"] != os.getpid():
            q = queue.Queue(maxsize=QUEUE_MAX)
            t = threading.Thread(target=_run, args=(q,), name="structures-writer", daemon=True)
            _writer.update(pid=os.getpid(), q=q, thread=t)
            t.start()
        return _writer["q"]


def _run(q):
    import revisit
    while True:
        job = q.get()
        try:
            # (a scope of its own: whatever it might read short is its own, never the process's orphan fault that
            # would make every build running uncachable)
            with revisit.scope():
                if job() is not None:
                    STATS["written"] += 1
                _mark_drops()
        except Exception as ex:         # (a shadow: never in anyone's way)
            STATS["failed"] += 1
            log.debug("structure groups (writer): %s: %s", type(ex).__name__, ex)
        finally:
            q.task_done()


def skipped(why):
    """A chain solve whose JSON is not written (roads.BridgeProfiles._chain_solve): why ("short": it read something
    short; "owner": not its owner's solve; "lost": not the solve published), counted in STATS."""
    STATS["skipped"] += 1
    STATS["skipped_" + why] = STATS.get("skipped_" + why, 0) + 1


_drops = {"cache": None, "written": 0}


def _mark_drops():
    """In the writer: the jobs dropped so far (a full queue) as dropped-<pid>.json, so a corpus run knows its
    JSONs are incomplete (the chain is published and is not solved again this session)."""
    n = STATS["dropped"]
    if n and n != _drops["written"] and _drops["cache"] is not None:
        _write(_drops["cache"], f"dropped-{os.getpid()}.json", {"format": FORMAT, "dropped": n})
        _drops["written"] = n


def _submit(job, cache_dir):
    q = _queue()
    _drops["cache"] = cache_dir
    try:
        q.put_nowait(job)
        STATS["queued"] += 1
        return True
    except queue.Full:
        STATS["dropped"] += 1
        return False


def flush(timeout=None):
    """Wait (up to timeout s; None: until done) for this process's writer to finish what it was handed. True when
    nothing is left."""
    with _wlock:
        q = _writer["q"] if _writer["pid"] == os.getpid() else None
    if q is None:
        return True
    end = None if timeout is None else time.monotonic() + timeout
    with q.all_tasks_done:
        while q.unfinished_tasks:
            left = None if end is None else end - time.monotonic()
            if left is not None and left <= 0:
                return False
            q.all_tasks_done.wait(left)
    return True


atexit.register(flush, 10.0)


# ---- the chain solve's hand-off -----------------------------------------------------------------------------------

def published(prof, comp, pool, zs, infra, block, cache_dir, short=False, raw=None):
    """BNE_DECK_STRUCT=shadow: the chain comp was PUBLISHED by this solve (roads.BridgeProfiles._chain_solve: its
    owner store's canonical solve, every part and the structure its own, read complete): its groups (block None; the
    heights zs in comp's order, pool the solve's parts {key: (spec, xy, lon, level, ...)}, infra the infrastructure
    Features its structure read) or, for a window solve (block: a chain over roads.CHAIN_CELLS cells, the legacy path
    in v1), a marker naming the window's parts, made and written by the background writer. Only copies taken here
    (the pool's arrays are the cells' cached ones, never changed). False when not handed over (a full queue: counted,
    marked by the writer). raw (S2b): a callable the writer calls for the chain's profile without the pairwise
    mechanisms (roads.BridgeProfiles._raw_solve: the joint height solve's input, heights()). Never raises."""
    try:
        if cache_dir is None or not comp:
            return False
        ck = min(comp)
        if block is not None:
            parts = [f"{k[0]}:{k[1]}" for k in comp]
            region = getattr(prof, "region", None)
            blk = [int(v) for v in block]
            return _submit(lambda: _capped_job(cache_dir, ck, blk, parts, region, short), cache_dir)
        chain = []
        for k, z in zip(comp, zs):
            v = pool[k]
            chain.append((k, v[0], v[1], np.array(z, np.float64), level_of(v), level_source(v)))
        inside = set(comp)
        others = [(k, pool[k][0], pool[k][1], level_of(pool[k]), level_source(pool[k]))
                  for k in sorted((k for k in pool if k not in inside), key=str)]
        rings = _outline_rings(infra)
        frame, region = prof.frame, getattr(prof, "region", None)
        return _submit(lambda: _chain_job(cache_dir, ck, chain, others, rings, frame, region, short, raw),
                       cache_dir)
    except Exception as ex:             # (a shadow: never in the way of the chain's solve)
        STATS["failed"] += 1
        log.debug("structure groups (hand-off): %s: %s", type(ex).__name__, ex)
        return False


def _member(k, spec, xy, z, level, src, outside):
    xy = np.asarray(xy, np.float64)[:, :2]
    z = np.asarray(z, np.float64).reshape(-1)
    if len(z) != len(xy):
        z = np.full(len(xy), np.nan)
    return {"key": f"{k[0]}:{k[1]}", "fid": str(k[0]), "cls": str(getattr(spec, "cls", "")), "xy": xy, "z": z,
            "half": float(spec.width) / 2, "kind": kind_of(spec), "level": float(level), "level_src": src,
            "outside": outside}


def _chain_job(cache_dir, ck, chain, others, rings, frame, region, short=False, raw=None):
    """The writer's work for one published chain: its members (the chain's, then the pool's other parts near it as
    outside), the groups, the joint heights (raw: S2b), the JSON named by the chain's key and its inputs' digest."""
    members = [_member(k, spec, xy, z, lv, src, False) for k, spec, xy, z, lv, src in chain]
    n_chain = len(members)
    allp = np.concatenate([m["xy"] for m in members])
    grow = OUTSIDE_NEAR_M + max([float(c[1].width) for c in chain] + [float(o[1].width) for o in others]) / 2
    lo, hi = allp.min(0) - grow, allp.max(0) + grow
    for k, spec, xy, lv, src in others:
        xy = np.asarray(xy, np.float64)[:, :2]
        if len(xy) < 2 or (xy.max(0) < lo).any() or (xy.min(0) > hi).any():
            continue
        members.append(_member(k, spec, xy, np.full(len(xy), np.nan), lv, src, True))
    enu = []
    for r in rings:
        e, n, _ = frame.geodetic_to_enu(r[:, 1], r[:, 0], 0.0)
        enu.append(np.c_[np.asarray(e, np.float64), np.asarray(n, np.float64)])
    got = groups(members, enu)

    def ll(xy):
        la, lo_, _ = frame.enu_to_geodetic(np.round(xy[:, 0], 3), np.round(xy[:, 1], 3), 0.0)
        return [[round(float(a), 7), round(float(b), 7)] for a, b in zip(la, lo_)]
    doc = document(members, got, n_chain, ll)
    extra = b""
    if raw is not None:
        r = raw()
        doc["joint"] = heights(members, n_chain, got, r)
        for m, z in zip(doc["members"], doc["joint"].pop("z_joint")):
            m["z_joint_mm"] = z
        for m, z in zip(doc["members"], r["z"]):
            m["z_in_mm"] = [_mm(v) for v in np.asarray(z, np.float64)]
        extra = json.dumps([doc["members"][i]["z_in_mm"] for i in range(n_chain)] + [list(f) for f in r["floors"]]
                           + [r["under"], r["rank"], r["link"]]).encode()
    dig = _input_digest(members, rings, frame, extra)
    doc.update({"chain": f"{ck[0]}:{ck[1]}", "capped": None, "inputs": dig,
                "frame": [float(frame.lat0), float(frame.lon0)], "region": region})
    if short:
        doc["short"] = True
    return _write(cache_dir, ("short_" if short else "") + _name(ck, None, dig), doc)


def _capped_job(cache_dir, ck, block, parts, region, short=False):
    """A window solve's marker: no groups (the legacy path in v1, the design's "Determinism / keys"), the window's
    parts; the same content whichever solve of that window publishes first."""
    dig = hashlib.sha256(repr((block, parts)).encode()).hexdigest()[:16]
    doc = {"format": FORMAT, "chain": f"{ck[0]}:{ck[1]}", "capped": block, "skipped": "window-capped chain",
           "parts": parts, "region": region, "inputs": dig}
    if short:
        doc["short"] = True
    return _write(cache_dir, ("short_" if short else "") + _name(ck, block, dig), doc)
