"""seamprobe (road revamp v2, risk 4: window independence): BNE_ROADSHAPE=sheet's forks are worked out from the parts
within a fixed radius of their node, never from the tile window, so two road tiles that share a seam must agree on it.

Real places with bridge forks near tile edges (fixtures/seamprobe_*.npz, tools/capture_seamprobe_fixture.py: the
server's solved bridge parts and the graded ground under them, from the road corpus's seeded caches) are built tile by
tile as the road tile build does (roads._bridge_chunks with a BridgeProfiles stand-in handing out the frozen whole parts
as BridgeProfiles.parts does, roads.deck_pads, roadmesh.parapet_runs, roadmesh.deck), every tile within FORK_TILE_M of a
fork. For each pair of those tiles sharing an edge:
  * the forks both tiles' windows hold (roads._sheet_window_parts, _sheet_records) are the same records, bit for bit;
  * every deck piece cut at the edge goes on in the other tile from the same point (x, y, z within TOL_M), with the
    same end line where the piece's end is skewed;
  * the deck tops' vertices on each cut line agree with the other tile's: the same extent along it (no gap or
    overlap) and the same heights, within TOL_M;
  * the parapet runs ending on a cut go on in the other tile from the same point (xy and z within TOL_M);
  * the junction pads near the seam (each tile's own=False list: the parapets' rims) are the same in both.
And each tile is byte-identical however it is built: after its neighbours, from only the parts its own windows would
read, with a wider fork window (roadshape.SHEET_WINDOW_M); with the parts in another order, the same geometry.

A cut's end line is its part's line's normal there in both tiles (roads._cut_ends, "ct0" / "ct1": square to each
piece's own end segment, the pieces simplified apart, the two sides ended up to 2.7 degrees apart, the same with
BNE_ROADSHAPE off), so the two must be the same line (SQUARE_WEDGE: an end without one). Ground roads, lane shifts
and widths are not in the fixture (None, as in test_roadshape_sheet, whose two-window tests cover them)."""
import ast
import hashlib
import math
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import fast  # noqa: E402
import roadmesh  # noqa: E402
import roads  # noqa: E402
import roadshape  # noqa: E402

HAVE = fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= roadshape.SHEET_VERSION
PLACES = ("fort_pitt", "i95_middle_branch")
FORK_TILE_M = 60.0      # m: the tiles whose box comes this near a fork's split point are built
FOCUS_M = 400.0         # m (Chebyshev) round the place: the forks whose tiles are built (the fixture's parts reach 900)
TOL_M = 1e-3            # m: positions and heights at a seam agree this closely
CUT_REACH_M = 3.0       # m past a cut piece's half width its cut line is looked at
PAD_M = 40.0            # m round a tile its pads are made (earthd's road tile)
SQUARE_WEDGE = 1e-4     # degrees two square ends of one cut may differ (2.7 before roads._cut_ends: the docstring)
WEDGE_GRADE = 0.15      # the steepest deck grade (seamcheck): a wedge's xy offset times this is its height offset
# Mismatches seen and reported, not fixed here: (place, seam, text in it) -> why. (None left: the slab's reach past its
# road, worked out per drawn chunk, saw only its own tile's side of a cut, 2 mm to 1.1 m steps and a nose bending a
# parapet off the cut; now along each deck part's whole line, roads._deck_reach / earthfast deck_reach.)
KNOWN = {}


def load(name):
    f = np.load(os.path.join(HERE, "fixtures", f"seamprobe_{name}.npz"))
    ofs = np.r_[0, np.cumsum(f["n"])]
    parts = [(str(f["fid"][i]), roads.Spec(**ast.literal_eval(str(f["spec"][i]))), f["xyz"][ofs[i]:ofs[i + 1]].copy(),
              float(f["lon"][i])) for i in range(len(f["n"]))]
    gx0, gy0, st = (float(v) for v in f["ground_origin"])
    grid = np.cumsum(np.c_[f["ground_first"].astype(np.int64), f["ground_steps"].astype(np.int64)], axis=1) / 100.0

    def ground(xy):
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        u = np.clip((xy[:, 0] - gx0) / st, 0.0, grid.shape[1] - 1.000001)
        v = np.clip((xy[:, 1] - gy0) / st, 0.0, grid.shape[0] - 1.000001)
        i, j = np.floor(u).astype(np.int64), np.floor(v).astype(np.int64)
        a, b = u - i, v - j
        return (grid[j, i] * (1 - a) * (1 - b) + grid[j, i + 1] * a * (1 - b) + grid[j + 1, i] * (1 - a) * b
                + grid[j + 1, i + 1] * a * b)
    PROFILED[name] = ({str(v) for v in f["profiled"] if str(v)} if "profiled" in f.files else None)
    return parts, ground


PROFILED = {}           # (a fixture's "profiled" ids: captured with BNE_ROADSHAPE=profile, P3e)


class Bridges:
    """BridgeProfiles' face the deck build reads: parts(cells, box) the whole parts coming within roads.NEAR_M of box
    (in the given order; only those within keep of it, when given: a tile's own windows), _ground the graded ground."""
    cell_fn = None

    def __init__(self, parts, ground, order=None, keep=None, profiled=None):
        self.p = [parts[i] for i in order] if order is not None else list(parts)
        if profiled is not None:
            self.profiled_ids = lambda: set(profiled)
        self.bb = [(p[2][:, :2].min(0), p[2][:, :2].max(0)) for p in self.p]
        self._ground = ground
        if keep is not None:
            x0, y0, x1, y1 = keep
            k = [i for i, (lo, hi) in enumerate(self.bb) if not (hi[0] < x0 or lo[0] > x1 or hi[1] < y0 or lo[1] > y1)]
            self.p, self.bb = [self.p[i] for i in k], [self.bb[i] for i in k]

    def parts(self, cells, box):
        x0, y0, x1, y1 = box
        m = roads.NEAR_M
        return [(fid, sp, p[:, :2].copy(), p[:, 2].copy(), lon) for (fid, sp, p, lon), (lo, hi) in zip(self.p, self.bb)
                if not (hi[0] < x0 - m or lo[0] > x1 + m or hi[1] < y0 - m or lo[1] > y1 + m)]


class Tile:
    def __init__(self, box, chunks, ctx, runs, pads_near, mesh, recs):
        self.box, self.chunks, self.ctx, self.runs, self.pads_near, self.mesh = box, chunks, ctx, runs, pads_near, mesh
        self.recs = recs
        self.tops = np.zeros((0, 3))
        if mesh is not None:
            pp, tt, _uu, mats, mi = mesh
            pp = np.asarray(pp, np.float64) + (box[0], box[1], 0.0)
            if roadmesh.DECK_TOP in mats:
                self.tops = pp[np.unique(np.asarray(tt)[np.asarray(mi) == mats.index(roadmesh.DECK_TOP)])]

    def blob(self):
        """The hash of the tile's whole output as bytes (byte identity)."""
        out = []
        for c in self.chunks + self.ctx:
            out.append(repr(sorted((k, v) for k, v in c.items() if k != "n")).encode())
            out.append(np.ascontiguousarray(np.asarray(c.get("n", ()), np.float64)).tobytes())
        for r in self.runs:
            out += [np.ascontiguousarray(np.asarray(r[k], np.float64)).tobytes() for k in ("line", "z")]
        for ring, centre, walls, *_js in self.pads_near:
            out += [np.asarray(ring, np.float64).tobytes(), np.asarray(centre, np.float64).tobytes()]
            out += [np.asarray(w[0], np.float64).tobytes() + np.asarray(w[1], np.float64).tobytes() for w in walls]
        if self.mesh is not None:
            pp, tt, uu, mats, mi = self.mesh
            out += [np.ascontiguousarray(np.asarray(a)).tobytes() for a in (pp, tt, uu, mi)] + [repr(mats).encode()]
        return hashlib.sha256(b"".join(out)).hexdigest()

    def shape(self):
        """The tile's geometry as a set (order free): chunks, parapet runs, pads, deck triangles, rounded to 1e-9."""
        r = lambda a: tuple(np.round(np.asarray(a, np.float64), 9).ravel().tolist())     # noqa: E731
        canon = lambda k, v: r(v) if k == "n" else sorted(map(repr, v)) if k == "abut" else v     # noqa: E731
        out = sorted(repr(sorted((k, canon(k, v)) for k, v in c.items())) for c in self.chunks + self.ctx)
        out += sorted(repr((r(q["line"]), r(q["z"]))) for q in self.runs)
        out += sorted(repr((r(p[0]), r(p[1]))) for p in self.pads_near)
        if self.mesh is not None:
            pp, tt, _uu, mats, mi = self.mesh
            pp = np.asarray(pp, np.float64)
            out += sorted(repr((mats[m], sorted(r(pp[i]) for i in t))) for t, m in zip(np.asarray(tt), np.asarray(mi)))
        return out


def records(br, box):
    """The forks this tile's window holds, by their arms' ids (records' indices are the window list's)."""
    wide = roads._sheet_window_parts(br, None, [], box)
    recs, _dz = roads._sheet_records(wide)
    out = {}
    for r in recs:
        key = (wide[r["t"]][0], wide[r["b"]][0], tuple(np.round(np.asarray(r["at"], np.float64), 6).tolist()))
        out[key] = repr(sorted((k, v) for k, v in r.items() if k not in ("t", "b", "cover"))) + repr(
            [(wide[p][0],) + tuple(q) for p, *q in r["cover"]])
    return out


def build(br, box):
    """One road tile's bridge decks as the tile build makes them (earthd's road tile, without ground roads)."""
    marks = []
    allc = roads._bridge_chunks(None, {}, None, box, bridges=br, cells=[], sheet_marks=marks)
    chunks = [c for c in allc if not c.get("ctx")]
    ctx = [c for c in allc if c.get("ctx")]
    near = br.parts([], (box[0] - PAD_M, box[1] - PAD_M, box[2] + PAD_M, box[3] + PAD_M))
    pads_near = roads.deck_pads([(f, sp, xy, z) for f, sp, xy, z, _lon in near], [], box, br._ground, own=False,
                                skip=marks)
    pads = [p for p in pads_near if roads.box_owns(box, float(p[1][0]), float(p[1][1]))]
    runs = roadmesh.parapet_runs(chunks, ctx, pads=pads_near)
    mesh = roadmesh.deck(chunks, (box[0], box[1], 0.0), (), ctx, (), parapets=runs, pads=pads)
    return Tile(box, chunks, ctx, runs, pads_near, mesh, records(br, box))


def fork_tiles(parts):
    recs, _dz = roads._sheet_records(parts)
    tiles = set()
    for r in recs:
        at = np.asarray(r["at"], np.float64)
        if np.abs(at).max() > FOCUS_M:
            continue            # (the parts reach farther: the tiles' windows differ, as neighbours' do in the game)
        tiles.update(roads.tiles_near(float(at[0]), float(at[1]), FORK_TILE_M))
    return sorted(tiles), len(recs)


def seams(tiles):
    """(a, b, axis, at, lo, hi) per pair of tiles sharing an edge: axis 0 an edge x = at, 1 y = at, over [lo, hi]."""
    have, T = set(tiles), roads.TILE
    out = []
    for tx, ty in tiles:
        if (tx + 1, ty) in have:
            out.append(((tx, ty), (tx + 1, ty), 0, (tx + 1) * T, ty * T, (ty + 1) * T))
        if (tx, ty + 1) in have:
            out.append(((tx, ty), (tx, ty + 1), 1, (ty + 1) * T, tx * T, (tx + 1) * T))
    return out


def cut_ends(tile, axis, at, lo, hi):
    """(chunk, point (3,), end line direction (2,), skewed) of the tile's deck pieces cut at the edge."""
    out = []
    for c in tile.chunks:
        n = np.asarray(c.get("n", ()), np.float64)
        if not c.get("deck") or len(n) < 2:
            continue
        for e, ks in ((0, ("sk0", "ct0")), (-1, ("sk1", "ct1"))):
            p = n[e]
            if abs(p[axis] - at) > 1e-9 or not lo - 1e-9 <= p[1 - axis] <= hi + 1e-9:
                continue
            k = next((k for k in ks if c.get(k) is not None), None)     # (a skewed end's line, a cut's: _cut_ends)
            if k is not None:
                d = np.asarray(c[k], np.float64)[:2]
            else:
                t = n[1, :2] - n[0, :2] if e == 0 else n[-1, :2] - n[-2, :2]
                d = np.array([-t[1], t[0]])
            out.append((c, p, d / math.hypot(*d), k is not None))
    return out


def on_line(pts, p, d, reach, tol):
    """pts (k, 3) within tol of the line through p along d and within reach of p: (along, z) sorted by along."""
    w = pts[:, :2] - p[:2]
    k = (np.abs(w[:, 0] * d[1] - w[:, 1] * d[0]) < tol) & (np.abs(w @ d) <= reach)
    got = np.c_[w[k] @ d, pts[k, 2]]
    return got[np.argsort(got[:, 0], kind="stable")]


def run_ends(tile):
    return [(np.asarray(r["line"], np.float64)[e, :2], float(np.asarray(r["z"], np.float64)[e]))
            for r in tile.runs if len(r["line"]) for e in (0, -1)]


def seam_problems(name, ta, tb, axis, at, lo, hi):
    """The seam's mismatches between tiles ta and tb: [(where, text)] (none when they agree)."""
    bad = []
    where = f"seam {'x' if axis == 0 else 'y'} = {at:g}"
    for k in set(ta.recs) & set(tb.recs):
        if ta.recs[k] != tb.recs[k]:
            bad.append((where, f"fork records differ: through {k[0]} branch {k[1]} at {k[2]}"))
    for one, other in ((ta, tb), (tb, ta)):
        oends = cut_ends(other, axis, at, lo, hi)
        for c, p, d, skew in cut_ends(one, axis, at, lo, hi):
            tag = f"{c.get('fid')} cut at ({p[0]:.3f}, {p[1]:.3f})"
            mate = [o for o in oends if o[0].get("fid") == c.get("fid") and np.all(np.abs(o[1][:3] - p[:3]) < TOL_M)]
            if not mate:
                bad.append((where, f"no piece goes on in the next tile: {tag}"))
                continue
            od, oskew = mate[0][2], mate[0][3]
            ang = math.degrees(math.asin(min(1.0, abs(float(d[0] * od[1] - d[1] * od[0])))))
            if skew or oskew:
                if skew != oskew or ang > 1e-4:
                    bad.append((where, f"skewed end lines differ ({ang:.4f} degrees): {tag}"))
                    continue
                wedge = 0.0
            elif ang > SQUARE_WEDGE:
                bad.append((where, f"square end lines {ang:.2f} degrees apart: {tag}"))
                continue
            reach = float(c.get("w", 0.0)) / 2.0 + CUT_REACH_M
            wedge = reach * math.sin(math.radians(ang))
            tol, ztol = TOL_M + wedge, TOL_M + wedge * WEDGE_GRADE
            va = on_line(one.tops, p, d, reach, TOL_M)
            vb = on_line(other.tops, p, od, reach, TOL_M)
            if not len(va) and not len(vb):
                continue            # (no slab of its own on either side here: a union's, drawn by its through deck)
            if not len(va) or not len(vb):
                bad.append((where, f"deck top on the cut in {len(va)} / {len(vb)} vertices: {tag}"))
                continue
            if abs(va[0, 0] - vb[0, 0]) > tol or abs(va[-1, 0] - vb[-1, 0]) > tol:
                bad.append((where, f"deck top cut spans [{va[0, 0]:.4f}, {va[-1, 0]:.4f}] / [{vb[0, 0]:.4f}, "
                                   f"{vb[-1, 0]:.4f}] m along it (a gap or an overlap): {tag}"))
                continue
            dz = np.abs(np.interp(va[:, 0], vb[:, 0], vb[:, 1]) - va[:, 1])
            if dz.max() > ztol:
                bad.append((where, f"deck top steps {dz.max():.4f} m at the cut: {tag}"))
            theirs = run_ends(other)
            for q, z in run_ends(one):
                w = q - p[:2]
                if abs(float(w @ d)) > reach or abs(float(w[0] * d[1] - w[1] * d[0])) > TOL_M:
                    continue
                if not any(math.hypot(*(q - r)) < tol and abs(z - rz) < ztol for r, rz in theirs):
                    bad.append((where, f"parapet end ({q[0]:.3f}, {q[1]:.3f}, {z:.3f}) does not go on: {tag}"))

    def near_pads(t):
        out = {}
        for ring, centre, *_rest in t.pads_near:
            if abs(float(centre[axis]) - at) <= PAD_M and lo - PAD_M <= float(centre[1 - axis]) <= hi + PAD_M:
                out[(round(float(centre[0]), 2), round(float(centre[1]), 2))] = np.asarray(ring, np.float64)
        return out
    pa, pb = near_pads(ta), near_pads(tb)
    if set(pa) != set(pb):
        bad.append((where, f"pads near it differ: {sorted(set(pa) ^ set(pb))}"))
    for k in sorted(set(pa) & set(pb)):
        if pa[k].shape != pb[k].shape or np.abs(pa[k] - pb[k]).max() > TOL_M:
            bad.append((where, f"pad rims differ at {k}"))
    return [(name, w, t) for w, t in bad]


def check(test, found):
    """found [(place, seam, text)] against KNOWN: no new mismatch, no known one gone (fixed: drop it from KNOWN)."""
    new = [f"{p} {w}: {t}" for p, w, t in found if not any(p == kp and w == kw and kt in t for kp, kw, kt in KNOWN)]
    gone = [k for k in KNOWN if not any(p == k[0] and w == k[1] and k[2] in t for p, w, t in found)]
    test.assertEqual(new, [], "seam mismatches")
    test.assertEqual(gone, [], "KNOWN mismatches no longer seen: drop them from KNOWN")


@unittest.skipUnless(HAVE, "earthfast.deck_sheets missing")
class SeamProbe(unittest.TestCase):
    maxDiff = None
    built = {}

    def setUp(self):
        old = roadshape.SHEET, roadshape.SHEET_WINDOW_M

        def restore():
            roadshape.SHEET, roadshape.SHEET_WINDOW_M = old
        self.addCleanup(restore)
        roadshape.SHEET = True

    PLACES = PLACES

    def br(self, name, parts, ground, order=None, keep=None):
        return Bridges(parts, ground, order, keep, PROFILED.get(name))

    def places(self):
        for name in self.PLACES:
            parts, ground = load(name)
            tiles, nrec = fork_tiles(parts)
            self.assertGreater(nrec, 0, f"{name}: no fork in the fixture (the probe would check nothing)")
            yield name, parts, ground, tiles

    def test_neighbours_agree_at_seams(self):
        found, cuts, shared = [], 0, 0
        for name, parts, ground, tiles in self.places():
            br = self.br(name, parts, ground)
            built = {t: build(br, roads.tile_bbox(*t)) for t in tiles}
            for a, b, axis, at, lo, hi in seams(tiles):
                cuts += len(cut_ends(built[a], axis, at, lo, hi))
                shared += len(set(built[a].recs) & set(built[b].recs))
                found += seam_problems(name, built[a], built[b], axis, at, lo, hi)
        self.assertGreater(cuts, 10, "too few decks cross the checked seams")
        self.assertGreater(shared, 10, "too few forks seen from both sides of a seam")
        check(self, found)

    def test_tile_same_from_any_window_and_order(self):
        for name, parts, ground, tiles in self.places():
            ref = {}
            br = self.br(name, parts, ground)
            for t in tiles:
                ref[t] = build(br, roads.tile_bbox(*t))
            for t in reversed(tiles):           # (after its neighbours, from only the parts its windows read)
                x0, y0, x1, y1 = roads.tile_bbox(*t)
                m = max(roadshape.SHEET_WINDOW_M, 3 * roadmesh.JUNCTION_MAX_PART_M + roads.DECK_CONTEXT_M,
                        PAD_M) + roads.NEAR_M
                own = self.br(name, parts, ground, keep=(x0 - m, y0 - m, x1 + m, y1 + m))
                self.assertEqual(build(own, roads.tile_bbox(*t)).blob(), ref[t].blob(), f"{name} {t}: own windows")
            roadshape.SHEET_WINDOW_M += 2 * roads.TILE
            for t in tiles:
                self.assertEqual(build(br, roads.tile_bbox(*t)).blob(), ref[t].blob(), f"{name} {t}: wider window")
            roadshape.SHEET_WINDOW_M -= 2 * roads.TILE
            mid = sorted(range(len(parts)), key=lambda i: (i * 7919) % len(parts))
            br = self.br(name, parts, ground, mid)
            for t in tiles:
                self.assertEqual(build(br, roads.tile_bbox(*t)).shape(), ref[t].shape(), f"{name} {t}: parts order")


@unittest.skipUnless(HAVE and getattr(fast._ef, "VERSION", 0) >= 536, "earthfast 536")
class SeamProbeProfile(SeamProbe):
    """The same seams with BNE_ROADSHAPE=profile (road revamp v2 P3e): the parts at their chains' joint profile (the
    twins' and the junctions' coupled heights), the coplanar trim (roadmesh.trim_overlaps: one seam between two decks
    at one height, read past a chunk end inside its part), a branch's surface on its union's through deck."""
    PLACES = ("fort_pitt_profile",)

    def setUp(self):
        super().setUp()
        old = roadshape.PROFILE_BUILT

        def restore():
            roadshape.PROFILE_BUILT = old
        self.addCleanup(restore)
        roadshape.PROFILE_BUILT = True

    def test_coplanar_cut_somewhere(self):
        # (the fixture must hold decks the coplanar trim cuts, or the seams check nothing of it)
        name = self.PLACES[0]
        parts, ground = load(name)
        tiles, _n = fork_tiles(parts)
        br = self.br(name, parts, ground)
        rows = 0
        for t in tiles:
            tile = build(br, roads.tile_bbox(*t))
            rb = [(r, c) for r, c in roadmesh.ribbon_pairs([c for c in tile.chunks if c.get("deck")])
                  if r[5] is not None]
            ctx = [(r, c) for r, c in roadmesh.ribbon_pairs(tile.ctx) if r[5] is not None]
            out = roadmesh.trim_overlaps(rb, ctx, roadmesh._junction_surfaces(tile.chunks, tile.ctx), coplanar=True)
            rows += sum(int(np.asarray(c["cop"]).any(axis=1).sum()) for _r, c in out if c.get("cop") is not None)
        self.assertGreater(rows, 10)


def surface_at(chunks, origin, pts, mat):
    """At the points pts (n, 2): (covered (n,) bool, height (n,), points held twice) of material mat's triangles in
    the road surface roadmesh.build draws of chunks."""
    sys.path.insert(0, os.path.join(HERE, "..", "..", "tools", "probes"))
    import markprobe
    cov, z = np.zeros(len(pts), bool), np.full(len(pts), np.nan)
    mesh = roadmesh.build(None, chunks, None, origin)
    if mesh is None or mat not in mesh[3]:
        return cov, z, 0
    P = np.asarray(mesh[0], np.float64) + np.asarray(origin, np.float64)
    T = np.asarray(mesh[1], np.int64).reshape(-1, 3)[np.asarray(mesh[4]) == mesh[3].index(mat)]
    pi, _ti, zz = markprobe.lookup(P[T], pts)
    cov[pi] = True
    z[pi] = zz
    return cov, z, int(len(pi) - len(np.unique(pi)))


class NoslabLift(unittest.TestCase):
    """A branch's road surface on an exit union's slab (P2b "noslab": the chunks in the union's branch intervals, from
    the fork's record) lies roadmesh.NOSLAB_LIFT over its through road's, pinned to the same heights: no point of the two
    opaque skins within 1 mm (they crossed in bands, 378 m2 at the I-95's Middle Branch: revampfix), and the same from
    the two tiles either side of a seam as from one build spanning it, from a row wholly inside the through road to a
    straddling one too (Codex reviews of revampfix / revampfix2)."""

    def test_lifted_over_the_through_road_at_any_seam(self):
        # (level: on a grade, the branch's rows (flat across, square to its own line) and the through road's meet at
        # heights a grade x offset x sin(angle) apart, the lift's margin to spend; I-95 measured in the commit)
        line = np.array([[-30.0, 4.5, 10.5], [-6.0, 0.0, 10.5], [6.0, 0.0, 10.5], [30.0, 4.5, 10.5]])
        xs = np.array([-40.0, -30.0, -6.0, 6.0, 30.0, 40.0])
        through = np.c_[xs, np.zeros(len(xs)), np.full(len(xs), 10.5)]
        tm, bm = roads.DECK_CONCRETE["m"], roads.DECK["m"]

        def chunks(box):
            out = []
            for pts, look, w, fid, ns in ((through, roads.DECK_CONCRETE, 7.0, "t", False),
                                          (line, roads.DECK, 4.0, "b", True)):
                for piece in roads.clip_polyline(pts, *box):
                    if len(piece) >= 2:
                        ch = roads._chunk({**look, "deck": True}, w, np.asarray(piece, np.float64))
                        ch.update(fixed=True, deck=True, fid=fid)
                        if ns:
                            ch["noslab"] = True
                        out.append(ch)
            return out
        g = np.mgrid[-32.0:32.0:0.137, -4.0:8.0:0.137].reshape(2, -1).T + (0.0123457, 0.0098765)
        everything = (-100.0, -100.0, 100.0, 100.0)
        cb, zb, twice = surface_at(chunks(everything), (0.0, 0.0, 0.0), g, bm)
        ct, zt, _t = surface_at(chunks(everything), (0.0, 0.0, 0.0), g, tm)
        both = cb & ct
        self.assertGreater(int(both.sum()), 1000)
        self.assertEqual(twice, 0)
        dz = zb[both] - zt[both]
        self.assertGreater(float(dz.min()), 0.001)
        np.testing.assert_allclose(dz, roadmesh.NOSLAB_LIFT, atol=5e-4)
        for seam in (-12.3, 3.1):
            got, zs = np.zeros(len(g), bool), np.full(len(g), np.nan)
            for box in ((-100.0, -100.0, seam, 100.0), (seam, -100.0, 100.0, 100.0)):
                c, z, t = surface_at(chunks(box), (box[0] + 7.0, 0.0, 0.0), g, bm)
                self.assertEqual(t + int((c & got).sum()), 0, f"seam {seam}")
                got |= c
                zs[c] = z[c]
            near = np.abs(g[:, 0] - seam) < 1e-6
            np.testing.assert_array_equal(got & ~near, cb & ~near, f"seam {seam}")
            # (on each triangle's own plane: the rows either side of a seam lie elsewhere along a ribbon whose heights
            # bend at its vertices, the quads' diagonals a fraction of a mm apart, as on any deck ribbon cut there)
            np.testing.assert_allclose(zs[got], zb[got], atol=1e-3)
            self.assertGreater(float((zs[both & got] - zt[both & got]).min()), 0.001, f"seam {seam}")


class ReachStation(unittest.TestCase):
    """roads._reach_tag's node stations on a loop ramp passing over itself (a grade-separated self-crossing, 10 m
    apart): each node takes its own pass's station, never the other's (Codex review of deckreach)."""

    LINE = np.array([[-50.0, 0.0, 0.0], [50.0, 0.0, 4.0], [50.0, 40.0, 8.0], [0.0, 40.0, 10.0], [0.0, -40.0, 14.0]])

    def test_crossing_takes_its_own_pass(self):
        self.assertAlmostEqual(roads._station_at_height(self.LINE, np.array([0.0, 0.0, 2.0])), 50.0, places=6)
        self.assertAlmostEqual(roads._station_at_height(self.LINE, np.array([0.0, 0.0, 12.0])), 230.0, places=6)

    def test_upper_pass_chunk_keeps_its_reach(self):
        ln = self.LINE
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(ln[:, :2], axis=0).T))]
        st = np.c_[np.arange(0.0, s[-1] + 1e-9, 2.0), np.full(int(s[-1] // 2) + 1, 0.45),
                   np.full(int(s[-1] // 2) + 1, 0.45), np.full(int(s[-1] // 2) + 1, 16.0)]
        ch = {"n": [[0.0, 10.0, 11.0], [0.0, 0.0, 12.0], [0.0, -10.0, 13.0]]}
        roads._reach_tag(ch, ln, st)
        self.assertIn("rch", ch)
        np.testing.assert_allclose(ch["rch"]["st"], [220.0, 230.0, 240.0], atol=1e-9)


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= 550, "earthfast 550")
class SeamProbeSurfaceV3(unittest.TestCase):
    """Road surface v3 S2 (BNE_ROADSURF_V3=surface, roadsurf3.py): two neighbouring road tiles at MD-295's 3 -> 4 lane
    merge (fixtures/seamprobe_v3s2_md295.npz: each tile's kernel inputs as roadsurf3.tile_surface gathered them from the
    map cells round it, tools/probes/surfprobe.py --fixture; two lane pieces cross the seam). The tiles' inputs are the
    same (gathered from the cells round each tile, never its window), the meshes each tile keeps round the seam the
    same bits, and both cut at the shared edge (clip_tris_convex, as roadmesh.build) leave the same points on it."""

    def setUp(self):
        self.load_pair("seamprobe_v3s2_md295.npz")

    def load_pair(self, filename):
        import roadsurf3
        f = np.load(os.path.join(HERE, "fixtures", filename))
        self.addCleanup(f.close)
        self.box = {t: [float(v) for v in f[f"box_{t}"]] for t in "ab"}
        self.inp = {t: {k[2:]: f[k] for k in f.files if k.startswith(t + "_")} for t in "ab"}
        self.got = {t: roadsurf3.run_kernel(self.inp[t]) for t in "ab"}

    def test_same_inputs(self):
        for k, v in self.inp["a"].items():
            if k == "box":
                continue
            self.assertEqual(np.asarray(v).tobytes(), np.asarray(self.inp["b"][k]).tobytes(), k)

    def test_ramp_past_the_nose(self):
        self.load_pair("seamprobe_v3s2b_md295.npz")
        self.assertTrue(any(flag.startswith("ramp_ease:") for s in self.got["a"]["status"]
                            for flag in s.get("flags", ())))
        self.check_seam()

    def test_seam(self):
        self.check_seam()

    def check_seam(self):
        ba, bb = self.box["a"], self.box["b"]
        axis, at = (0, ba[2]) if ba[2] == bb[0] else (1, ba[3])
        self.assertTrue(ba[2] == bb[0] or ba[3] == bb[1])

        def band(got, lo, hi):
            v, t = got["mesh"]["v"], got["mesh"]["tri"]
            c = v[t]
            m = (c[:, :, axis].min(1) >= lo) & (c[:, :, axis].max(1) <= hi)
            return sorted(x.tobytes() for x in c[m])
        ta, tb = band(self.got["a"], at - 6.0, at + 6.0), band(self.got["b"], at - 6.0, at + 6.0)
        self.assertGreater(len(ta), 50)
        self.assertEqual(ta, tb)

        def cut(got, box):
            v, t = got["mesh"]["v"], got["mesh"]["tri"]
            poly = np.array([[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]], np.float64)
            P, _uv, T, _src = fast._ef.clip_tris_convex(np.ascontiguousarray(np.c_[v, np.zeros(len(v))]),
                                                       np.zeros((len(v), 2)), np.ascontiguousarray(t, np.int64), poly, True)
            P = np.asarray(P)[np.unique(T)]
            return np.unique(P[np.abs(P[:, axis] - at) < 1e-7], axis=0)
        ea, eb = cut(self.got["a"], ba), cut(self.got["b"], bb)
        self.assertGreater(len(ea), 4)
        self.assertEqual(ea.tobytes(), eb.tobytes())

    def test_bit_stable(self):
        import roadsurf3
        again = roadsurf3.run_kernel(self.inp["a"])
        self.assertEqual(again["mesh"]["v"].tobytes(), self.got["a"]["mesh"]["v"].tobytes())
        self.assertEqual(again["mesh"]["tri"].tobytes(), self.got["a"]["mesh"]["tri"].tobytes())
        self.assertEqual(again["status"], self.got["a"]["status"])


class SurfaceDeckSeam(unittest.TestCase):
    def chunks(self, box, fork=False):
        from unittest.mock import patch
        from test_roadsurf3 import piece, quintic
        spec = roads.Spec(5.5, "paved", False, "motorway", True, dir=1, lanes=1)
        def line(a, b, y):
            x = np.arange(a, b + 1., 5.)
            return np.c_[x, np.full_like(x, y), 8. + x * .02]
        parts = [("main", spec, line(0, 155, 3.25), -76.)]
        events = []
        if fork:
            parts.append(("exit", spec, line(80, 155, -3.25), -76.))
            ev = dict(kind="diverge", segs=["main", "main", "exit"], lanes=[2, 1, 1], lane_w=3500,
                      side="right", strips=["through", "gore", "branch"])
            rows, bounds = piece(kinds=(1, 2, 3), widths=(None, lambda s: 4.*quintic(s/100.), None))
            events.append((ev, rows, bounds))
        with patch("roadsurf3._tile_events", return_value=events):
            return roads._bridge_chunks(None, {}, None, box, Bridges(parts, lambda xy: np.zeros(len(xy))),
                                        cells=[], surface={"cell_fn": lambda k: None}, country="US")

    def test_profile_and_free_edges_across_tile_cut(self):
        seam = 34.25
        points = []
        for box in ((-10, -20, seam, 20), (seam, -20, 165, 20)):
            chunks = self.chunks(box)
            self.assertTrue(any(c.get("surface_ref") for c in chunks))
            faces = [c for c in chunks if "deck_z" in c]
            self.assertEqual(len(faces), 1)
            f = faces[0]
            p = np.c_[f["xy"], f["deck_z"]]
            np.testing.assert_allclose(p[:, 2], 8.+.02*p[:, 0], atol=1e-10)
            points.append(np.unique(p[p[:, 0] == seam], axis=0))
            runs = roadmesh.parapet_runs(chunks)
            self.assertGreaterEqual(len(runs), 2)
            for r in runs:
                self.assertGreater(np.ptp(r["edge"][:, 0]), 1.)
            mesh = roadmesh.deck(chunks, np.zeros(3), parapets=runs)
            self.assertIn(roadmesh.DECK_TOP, mesh[3])
            top = mesh[1][mesh[4] == mesh[3].index(roadmesh.DECK_TOP)]
            slab_xyz = mesh[0][np.unique(top)]
            np.testing.assert_allclose(slab_xyz[:, 2], 8. + .02 * slab_xyz[:, 0], atol=1e-6)
            skin = roadmesh.build(None, faces, None, np.zeros(3), clip=box, follow=True)
            skin_xyz = skin[0][np.unique(skin[1])]
            np.testing.assert_allclose(skin_xyz[:, 2], 8. + .02 * skin_xyz[:, 0] + roadmesh.DECK_SKIN, atol=1e-6)
        self.assertGreaterEqual(len(points[0]), 2)
        np.testing.assert_array_equal(points[0], points[1])

    def test_skewed_banked_abutment_keeps_both_edge_heights(self):
        import roadsurf3_decks
        spec = roads.Spec(8., "paved", False, "motorway", True, dir=1, lanes=2)
        x = np.arange(0., 80., 3.)
        p = np.c_[x, np.zeros(len(x)), 10. + x * .02]
        shape = dict(skew=[[.3, 1.], [.3, 1.]], ov=[None, None], bank=np.full(len(x), .03))
        part = roadsurf3_decks._profile(("deck", spec, p, -76.), shape)
        for row in part[2][[0, -1]]:
            edges = np.stack([row[:2] + row[3:5], row[:2] - row[3:5]])
            z = fast._ef.deck_surface_z(edges, [part[2]], [8.])
            np.testing.assert_allclose(z, row[2] + np.array([row[5], -row[5]]), rtol=0, atol=1e-10)

    def test_parapet_opens_for_a_ground_approach_at_deck_height(self):
        chunks = self.chunks((-10, -20, 165, 20))
        approach = dict(n=[[40., -18., 8.8], [40., 10., 8.8]], w=7., m="asphalt", layer="base")
        runs = roadmesh.parapet_runs(chunks, ground=[approach])
        self.assertTrue(runs)
        for run in runs:
            self.assertFalse(run["edge"][:, 0].min() < 40. < run["edge"][:, 0].max())

    def test_diverge_opens_at_the_nose(self):
        chunks = self.chunks((-10, -30, 165, 30), fork=True)
        f, = [c for c in chunks if "deck_z" in c]
        pts = f["xy"][f["T"]]
        def intervals(x):
            # Cross the triangles away from row vertices.
            spans = []
            for tri in pts:
                y = [a[1] + (x-a[0])*(b[1]-a[1])/(b[0]-a[0])
                     for a,b in zip(tri, np.roll(tri, 1, axis=0)) if min(a[0],b[0]) < x < max(a[0],b[0])]
                if len(y) == 2:
                    spans.append(sorted(y))
            merged = []
            for lo, hi in sorted(spans):
                if merged and lo <= merged[-1][1] + 1e-6:
                    self.assertAlmostEqual(merged[-1][1], lo, delta=1e-6)
                    merged[-1][1] = hi
                else:
                    merged.append([lo, hi])
            return merged
        self.assertEqual(len(intervals(50.25)), 1)
        aft = intervals(110.25)
        self.assertEqual(len(aft), 2)
        self.assertGreater(aft[1][0] - aft[0][1], .5)
        early = intervals(50.25)[0]
        for r in roadmesh.parapet_runs(chunks):
            for a, b in zip(r["edge"], r["edge"][1:]):
                if min(a[0],b[0]) < 50.25 < max(a[0],b[0]):
                    y = a[1]+(50.25-a[0])*(b[1]-a[1])/(b[0]-a[0])
                    self.assertLess(min(abs(y-e) for e in early), 1e-6)


    def test_stack_cannot_join_via_connecting_ramp(self):
        # Both ends of the climb meet one deck. Its ends alone would miss
        # the different elevations where the two sparse main spans cross.
        lower = np.array([[-100., 0., 0.], [100., 0., 0.]])
        upper = np.array([[0., -100., 8.], [0., 100., 8.]])
        ramp = np.array([[100., 0., 0.], [95., 30., 0.], [35., 97., 8.], [0., 100., 8.]])
        groups = fast._ef.deck_surface_groups([lower, upper, ramp], [9.] * 3, [True] * 3, [-120.] * 2 + [120.] * 2)
        self.assertEqual(sorted(i for group in groups for i in group), [0, 1, 2])
        self.assertFalse(any(0 in group and 1 in group for group in groups))

    def test_shared_bearing_does_not_join_a_later_stacked_crossing(self):
        main = np.array([[0., 0., 10.], [120., 0., 10.]])
        exit_ = np.array([[0., 0., 10.], [25., 8., 11.], [50., 40., 13.], [100., -15., 13.]])
        groups = fast._ef.deck_surface_groups([main, exit_], [8., 8.], [True, True], [-10., -20., 125., 45.])
        self.assertEqual(groups, [[0], [1]])

    def test_ramp_follows_main_until_split_and_preserves_far_bearing(self):
        x = np.arange(0., 145., 5.)
        main = np.c_[x, np.zeros_like(x), 8. + .025 * x]
        ramp = np.c_[x, np.maximum(x - 40., 0.) * .6, 8. + .065 * x]
        main_dz, ramp_dz = fast._ef.deck_ramp_offsets([main, ramp], [12., 8.], [False, True])
        np.testing.assert_array_equal(main_dz, np.zeros_like(x))
        adjusted = ramp[:, 2] + ramp_dz
        np.testing.assert_allclose(adjusted[x <= 55.], main[x <= 55., 2], atol=1e-12)
        np.testing.assert_array_equal(adjusted[x >= 100.], ramp[x >= 100., 2])

    def test_long_wide_taper_across_stacked_tile_seam(self):
        from unittest.mock import patch
        spec = roads.Spec(44., "paved", False, "motorway", True, dir=1, lanes=8)
        parts = []
        for i in range(10):
            x = np.arange(50.*i, 50.*i + 51., 5.)
            parts.append((str(i), spec, np.c_[x, np.zeros_like(x), np.full_like(x, 8.)], -76.))
        y = np.arange(-80., 81., 5.)
        parts.append(("upper", spec, np.c_[np.full_like(y, 225.), y, np.full_like(y, 16.)], -76.))
        # Adjacent cells also have two copies of section 4 at the same
        # rounded key, solved a few millimetres apart.
        duplicate = parts[4][2].copy()
        duplicate[:, 1] += .003
        parts.append(("4", spec, duplicate, -76.))
        seam = 225.25
        cuts = []
        def width(fid, pts):
            return np.full(len(pts), 10.) if fid == "upper" else 44. + pts[:, 0] * .02
        for box, data in [((100., -90., seam, 90.), parts), ((seam, -90., 350., 90.), parts[::-1])]:
            with patch("roadsurf3._tile_events", return_value=[]):
                chunks = roads._bridge_chunks(None, {}, None, box, Bridges(data, lambda xy: np.zeros(len(xy))),
                                              cells=[], width_of=width, surface={"cell_fn": lambda k: None})
            xyz = np.concatenate([np.c_[ch["xy"], ch["deck_z"]] for ch in chunks if "deck_z" in ch])
            cut = np.unique(xyz[xyz[:, 0] == seam], axis=0)
            self.assertEqual(set(cut[:, 2].round(6)), {8., 16.})
            lower_y = cut[cut[:, 2] == 8., 1]
            self.assertAlmostEqual(np.ptp(lower_y), 44. + seam * .02 + 2 * roadmesh.PARAPET_W, places=2)
            cuts.append(cut)
        np.testing.assert_array_equal(*cuts)

    def test_fork_seams_before_and_beyond_nose(self):
        for seam in (50.25, 110.25):
            points = []
            for box in ((-10, -30, seam, 30), (seam, -30, 165, 30)):
                face, = [c for c in self.chunks(box, fork=True) if "deck_z" in c]
                xyz = np.c_[face["xy"], face["deck_z"]]
                points.append(np.unique(xyz[xyz[:, 0] == seam], axis=0))
            self.assertGreaterEqual(len(points[0]), 4)
            self.assertEqual(points[0].tobytes(), points[1].tobytes())


if __name__ == "__main__":
    unittest.main()
