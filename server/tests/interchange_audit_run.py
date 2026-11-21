"""Run the interchange audit (interchange_audit.py) on the places the tests replay offline, from their cached data.

    python tests/interchange_audit_run.py popoli | replay NAME | truth NAME | list  [--out DIR]

popoli / replay NAME: a replay of tests/interchanges.py (fixtures/interchange_NAME.npz: the roads cells and the elevation
round a reported place) planned as the server plans it (grading._Planner._shape_region_calc), its solve replaced by the
audit: the members, the find, the pieces, the samples and the ground are the pipeline's own. truth NAME: a Swiss survey
place (tests/truth.py), its ways as the members the survey's own facts give, on Earth's ground. Prints the markdown and,
with --out, writes DIR/<place>.md. A region of the server's own (a Shanghai cell) is run the same way: capture(planner,
home) on the planner of the cache, see the eval's script.

No downloads; the replays' data is in tests/fixtures."""
import json
import os
import sys
from types import SimpleNamespace
from unittest import mock

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import gradeshapes as gs  # noqa: E402
import interchange_audit as ia  # noqa: E402


SURVEY_NODE_M = 1.0     # m: a survey line this near a way's end meets it
SURVEY_EDGE_M = 3.0     # m: an end this near the survey's own box leaves it
EDGE_M = 150.0          # m: an end this near a map cell that was not read may meet a road there


class PlannerMeta:
    """What the map has of a road feature, by its Earth id, read from the planner's cells when asked: {osm: the way, name,
    layer, oneway, dir}. A dict by .get()."""

    END_TOL_M = 3.0         # m: a road's end this near a member's end meets it
    OWN_GAP_M = 15.0        # m: a part of its own road ending this near (and not END_TOL_M) is a gap in the road's parts

    def __init__(self, planner, net, loaded=()):
        self.p, self.net = planner, net
        self.loaded = loaded            # the map cells the region read (the keys the planner's cells go by)
        self._uni = None
        self._conn = None
        self.ids = {}                   # Earth id -> a member index
        for i, m in enumerate(net.m):
            self.ids.setdefault(str(m.key[0] if isinstance(m.key, tuple) else m.key), i)
        self.cells = {}                 # cell -> (feature index by id, specs)
        self.memo = {}

    def _cell(self, k):
        got = self.cells.get(str(k))
        if got is None:
            import roads
            f = self.p.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
            index = {} if f is None else {str(f.ids[i]): i for i in range(len(f))}
            specs = roads.road_specs(f, None, self.p.widths) if f is not None and len(f) else []
            bspecs = roads.bridge_specs(f) if f is not None and len(f) else []
            got = self.cells[str(k)] = (f, index, specs, bspecs)
        return got

    def get(self, eid, default=None):
        if eid in self.memo:
            return self.memo[eid] or default
        import mapcells
        out = None
        i = self.ids.get(eid)
        if i is not None:
            mid = self.net.lines[i][len(self.net.lines[i]) // 2]
            la, lo, _ = self.p.frame.enu_to_geodetic(np.array([mid[0]]), np.array([mid[1]]), 0.0)
            home = mapcells.cell_of(float(la[0]), float(lo[0]))
            lat0, lon0 = float(la[0]), float(lo[0])
            for k in [home] + sorted(mapcells.cells_for_bbox(lat0 - 0.02, lon0 - 0.03, lat0 + 0.02, lon0 + 0.03), key=str):
                f, index, specs, bspecs = self._cell(k)
                if eid in index:
                    out = self._feature(f, index[eid], specs, bspecs)
                    break
        self.memo[eid] = out
        return out or default

    def _locate(self, eid):
        """(cell feature set, its index of eid) of a road feature, from the cells round its member (as get)."""
        import mapcells
        i = self.ids.get(eid)
        if i is None:
            return None
        mid = self.net.lines[i][len(self.net.lines[i]) // 2]
        la, lo, _ = self.p.frame.enu_to_geodetic(np.array([mid[0]]), np.array([mid[1]]), 0.0)
        lat0, lon0 = float(la[0]), float(lo[0])
        home = mapcells.cell_of(lat0, lon0)
        for k in [home] + sorted(mapcells.cells_for_bbox(lat0 - 0.02, lon0 - 0.03, lat0 + 0.02, lon0 + 0.03), key=str):
            f, index, _specs, _bspecs = self._cell(k)
            if eid in index:
                return f, index[eid]
        return None

    def _cell_keys(self):
        import mapcells
        return [mapcells.parse(k) if isinstance(k, str) else k for k in sorted(self.loaded, key=str)]

    def universe(self):
        """Every member the cells read give (the region's net keeps those in its box only): {key: Member}, and per member
        the ends (n, 2, 2) for a nearest look."""
        if self._uni is None:
            by = {}
            for k in self._cell_keys():
                for m in self.p._shape_members(k):
                    old = by.get(m.key)
                    if old is None or len(m.line) > len(old.line):
                        by[m.key] = m
            keys = sorted(by, key=str)
            ends = np.array([[np.asarray(by[q].line)[0, :2], np.asarray(by[q].line)[-1, :2]] for q in keys]).reshape(-1, 2, 2)
            self._uni = (by, keys, ends, {m.key for m in self.net.m})
        return self._uni

    def connectors(self):
        """{connector id: [(feature id, 0 | 1 its end the connector is at, cell feature set, index)]} over the cells read."""
        if self._conn is None:
            out = {}
            for k in self._cell_keys():
                f = self._cell(k)[0]
                con = None if f is None or not len(f) else f.rules.get("connectors")
                if con is None:
                    continue
                for n in range(len(f)):
                    for r in con.rows(n):
                        at = float(con.start[r])
                        if at < 1e-6 or at > 1.0 - 1e-6:
                            out.setdefault(str(con.value[r]), []).append((str(f.ids[n]), int(at > 0.5), f, n, k))
            self._conn = out
        return self._conn

    def endinfo(self, i, end):
        """What the map has at member i's end (0 start, 1 end), for the audit's loose-end check: dict(near: [the members of
        the cells read with an end within END_TOL_M, not in the net: (label parts)], inside: [the same in the net], others:
        [(feature id, class, layer, OSM way) the map's roads sharing the end's connector, the shapes' members or not],
        connector: its id or None)."""
        by, keys, ends, netkeys = self.universe()
        me = self.net.m[i]
        P = np.asarray(self.net.lines[i][0 if end == 0 else -1], np.float64)[:2]
        d = np.sqrt(np.sum((ends - P) ** 2, axis=2)).min(axis=1)
        near, inside = [], []
        for n in np.flatnonzero(d <= self.END_TOL_M).tolist():
            q = keys[n]
            if q == me.key:
                continue
            m = by[q]
            row = (str(q[0]), m.kind, m.cls, float(m.level), float(d[n]), q)
            (inside if q in netkeys else near).append(row)
        out = dict(near=near, inside=inside, others=[], connector=None, feature_end=None, own_gap=None, own_next=None)
        eid = self.earth_of(i)
        # (its own road's other part whose end is nearer than OWN_GAP_M but not END_TOL_M: a gap in the road's own parts)
        gaps = np.flatnonzero((d > self.END_TOL_M) & (d <= self.OWN_GAP_M)).tolist()
        for n in sorted(gaps, key=lambda n: (float(d[n]), str(keys[n]))):
            q = keys[n]
            if q != me.key and str(q[0]) == eid:
                out["own_gap"] = (str(q[0]), by[q].kind, by[q].cls, float(by[q].level), float(d[n]), q)
                break
        got = self._locate(eid)
        if got is None:
            return out
        f, n = got
        parts = f.parts(n)
        ll = np.asarray(parts[0][0], np.float64)
        e_, n_, _ = self.p.frame.geodetic_to_enu(ll[[0, -1], 1], ll[[0, -1], 0], 0.0)
        fe = np.stack([np.asarray(e_, np.float64), np.asarray(n_, np.float64)], axis=1)
        which = [int(k) for k in (0, 1) if float(np.sqrt(np.sum((fe[k] - P) ** 2))) <= self.END_TOL_M * 5]
        if not which:
            # (a part's end inside its map road: the road goes on as the range of another kind next to it, a tunnel the
            # shapes do not take as a member (a tunnel network) or a part too short to be one)
            import roads
            xy = np.stack([np.asarray(self.p.frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)[k], np.float64) for k in (0, 1)], axis=1)
            sd = np.r_[0.0, np.cumsum(np.sqrt(np.sum(np.diff(xy, axis=0) ** 2, axis=1)))]
            if sd[-1] > 0:
                j = int(np.argmin(np.sqrt(np.sum((xy - P) ** 2, axis=1))))
                t = float(sd[j] / sd[-1])
                tol = 6.0 / float(sd[-1])
                for a_, b_, kind in roads.level_ranges(f, n):
                    if kind != {"deck": "bridge", "tunnel": "tunnel", "ground": "ground"}.get(me.kind, me.kind) and                             (abs(a_ - t) <= tol or abs(b_ - t) <= tol):
                        out["own_next"] = (kind, float(a_), float(b_))
                        break
        if which:
            out["feature_end"] = which[0]
            con = f.rules.get("connectors")
            cid = None
            if con is not None:
                for r in con.rows(n):
                    at = float(con.start[r])
                    if abs(at - which[0]) < 1e-6:
                        cid = str(con.value[r])
            out["connector"] = cid
            seen = set()
            for fid2, e2, f2, n2, k2 in self.connectors().get(cid, []):
                if fid2 == eid or (fid2, e2) in seen:
                    continue
                seen.add((fid2, e2))
                a = f2.attrs
                col = lambda name: None if a.get(name) is None else str(a[name][n2]).strip() or None
                _f, _ix, specs, bspecs = self._cell(k2)
                md = self._feature(f2, n2, specs, bspecs)
                out["others"].append((fid2, col("class"), col("osm_layer"), col("src_record"), fid2 in by, e2, md))
        return out

    def earth_of(self, i):
        k = self.net.m[i].key
        return str(k[0] if isinstance(k, tuple) else k)

    @staticmethod
    def _feature(f, n, specs, bspecs):
        a = f.attrs

        def col(name):
            v = a.get(name)
            return None if v is None else str(v[n]).strip() or None
        sp = specs[n] if n < len(specs) and specs[n] is not None else (bspecs[n] if n < len(bspecs) else None)
        rec = col("src_record")
        return dict(osm=(rec if rec else None), name=col("name"), layer=col("osm_layer"),
                    oneway=bool(getattr(sp, "oneway", False)), dir=getattr(sp, "dir", None),
                    tags={k[4:]: col(k) for k in sorted(a) if k.startswith("osm_") and col(k)})


def frame_ll(frame):
    def ll(x, y):
        la, lo, _ = frame.enu_to_geodetic(np.array([x]), np.array([y]), 0.0)
        return float(la[0]), float(lo[0])
    return ll


def near_place(planner, net, systems, namer, lat, lon, radius):
    """Where a named place lies in the systems: [(system index, nearest member label, distance m)] for every system with a
    member (its seeds, any piece of the line) within `radius` m of lat, lon (the frame's own ground distance)."""
    e, n, _ = planner.frame.geodetic_to_enu(lat, lon, 0.0)
    here = np.array([float(np.ravel(e)[0]), float(np.ravel(n)[0])])
    out = []
    for k, sysm in enumerate(systems):
        best = None
        for i in sysm["seeds"]:
            xy = np.asarray(net.lines[i], float)[:, :2]
            d = float(np.min(np.sqrt(np.sum((xy - here) ** 2, axis=1))))
            if d <= radius and (best is None or d < best[1]):
                best = (i, d)
        if best:
            out.append((k, namer.label(best[0]), best[1]))
    return out


def capture(planner, home, solve=False, region_calc=None):
    """The shapes region of cell `home` planned as the server plans it with every system's solve replaced by the audit (or
    run first with solve: the answer the kernel gives, kept in the system's step 3 stats). region_calc(home, key): the
    function that plans the region (a caller that wrapped the planner's own). Returns (net, systems, audits, namer, trace)."""
    held = {}
    per = {}
    real_find, real_solve = gs.find_interchanges, gs.solve_shape
    loaded = set()
    shape_members = planner._shape_members

    def members_of_cell(k):
        out = shape_members(k)
        if out:
            loaded.add(str(k))
        return out
    planner._shape_members = members_of_cell

    def at_edge(xy):
        """Whether a point is within EDGE_M of a map cell the region did not read."""
        import mapcells
        for dx, dy in ((0, 0), (EDGE_M, 0), (-EDGE_M, 0), (0, EDGE_M), (0, -EDGE_M)):
            la, lo, _ = planner.frame.enu_to_geodetic(np.array([float(xy[0]) + dx]), np.array([float(xy[1]) + dy]), 0.0)
            if str(mapcells.cell_of(float(la[0]), float(lo[0]))) not in loaded:
                return True
        return False

    def find(members, reader=None, trace=None, want=None):
        if held:        # (a later find: an interchange followed whole from the region's piece of it, not the region's)
            return real_find(members, reader, trace, want)
        tr = {}                 # (every interchange: want is not asked, the audit reads those round the region too)
        net, found = real_find(members, reader, tr)
        systems = [x.sysm for x in found]
        held.update(net=net, systems=systems, trace=tr, meta=PlannerMeta(planner, net, loaded),
                    namer=None, rels={})
        held["namer"] = ia.Namer(net, frame_ll(planner.frame), held["meta"], at_edge, held["meta"].endinfo)
        per.clear()
        return net, found

    def audit(net, sysm, pcs, smp, ground, fixed_z, diagnostics=None):
        return None         # (the interchanges are solved whole, in nets of their own: the region's pieces are audited below)
    with mock.patch.object(gs, "find_interchanges", find), mock.patch.object(gs, "solve_shape", audit):
        (region_calc or planner._shape_region_calc)(home, str(home))
    net, systems = held["net"], held["systems"]
    # (the systems round the region the planner did not solve: the same steps, on the same ground and fixed heights)
    import ix.s2_extent as s2
    fixed = s2.make_fixed(planner, net)
    for n, sysm in enumerate(systems):
        if n not in per:
            pcs = gs.pieces(net, sysm)
            smp = gs.sample(net, pcs)
            ground = [planner._ground_line(x["xy"]) for x in smp]
            per[n] = ia.audit_system(net, sysm, pcs, smp, ground, fixed, held["namer"], held["rels"])
            if solve:
                diag = {}
                got = real_solve(net, sysm, pcs, smp, ground, fixed, diag)
                per[n][3]["stats"]["kernel"] = dict(solved=got is not None, status=diag.get("status"),
                                                     qp_error=diag.get("qp_error"))
    audits = ia.audit_region(net, systems, held["trace"], held["namer"], per)
    return net, systems, audits, held["namer"], held["trace"]


def planner_of(rep):
    import grading
    md = SimpleNamespace(cell=lambda key, **kw: SimpleNamespace(layers={"roads": rep.cells.get(str(key))}),
                         dem=SimpleNamespace(coarse_at=lambda la, lo: False))
    return grading._Planner(rep.frame, md, lambda la, lo, *a: rep.sample(la, lo))


def replay_site(name, solve=False):
    """The audit of a replay's place (tests/interchanges.py: fixtures/interchange_NAME.npz)."""
    import interchanges
    import mapcells
    import revisit
    rep = interchanges.load(name)
    with mock.patch.object(revisit, "ON", False):
        planner = planner_of(rep)
        home = mapcells.cell_of(*rep.origin)
        got = capture(planner, home, solve)
    return got + (rep,)


def truth_site(name):
    """The audit of a surveyed place: the survey's ways as members (truth.members), Earth's ground there."""
    import truth
    site = truth.load(name)
    mem, ws = truth.members(site)
    rep = truth.replay(name)
    ground = truth.ground_fn(rep, ws)

    def make_fixed(net):
        def fixed(i, s):
            sv = np.atleast_1d(np.asarray(s, np.float64))
            if ws[i].layer == "rail":
                z = np.interp(sv, truth.arclen(ws[i].xyz), ws[i].xyz[:, 2])
            else:
                z = ground(np.stack([np.interp(sv, net.s[i], net.lines[i][:, 0]), np.interp(sv, net.s[i], net.lines[i][:, 1])],
                                    axis=1))
            return np.asarray(z, np.float64) if np.ndim(s) else float(z[0])
        return fixed
    meta = {ln.id: dict(name=ln.objektart, layer=str(ln.level)) for ln in ws}
    mine = {id(ln) for ln in ws}
    others = [ln for ln in site.lines if id(ln) not in mine]        # (the survey's paths and what is no way: not members)
    by_id = {ln.id: ln for ln in ws}

    def endinfo(i, e):
        """What the survey has at a member's end: a path or a line that is no way ending (or running) there is a road the
        shapes do not take; at the survey's own edge the road goes on outside it; else nothing."""
        ln = by_id[net_of["net"].m[i].key]
        p = ln.xyz[0 if e == 0 else -1]
        near = []
        for o in others:
            d = np.sqrt(np.sum((o.xyz[:, :2] - p[:2]) ** 2, axis=1))
            if float(d.min()) <= SURVEY_NODE_M:
                near.append((str(o.id), o.objektart, str(o.level), None, False, 0, {}))
        return dict(near=[], inside=[], others=near, connector=str(truth.node_of(p)), feature_end=e)
    net_of = {}

    class Capture(ia.Namer):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            net_of["net"] = self.net
    half = float(site.half)
    with mock.patch.object(ia, "Namer", Capture):
        got = ia.audit_members(mem, ground, make_fixed, frame_ll(rep.frame), meta, endinfo,
                               lambda xy: max(abs(float(xy[0])), abs(float(xy[1]))) >= half - SURVEY_EDGE_M)
    return got + (rep,)


def header(place, source, rep=None):
    import subprocess
    try:
        rev = subprocess.run(["git", "-C", os.path.dirname(HERE), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True).stdout.strip()
    except OSError:
        rev = "?"
    return ["Source: %s." % source, "Code: gradeshapes at %s, interchange_audit.py. Run: python tests/interchange_audit_run.py %s."
            % (rev, place)]


def main(argv):
    out = None
    if "--out" in argv:
        k = argv.index("--out")
        out = argv[k + 1]
        argv = argv[:k] + argv[k + 2:]
    what = argv[0] if argv else "list"
    import interchanges
    import truth
    if what == "list":
        print("popoli replay:", sorted(interchanges.SITES), "truth:", list(truth.SITES))
        return
    if what == "popoli":
        place, got = "popoli", replay_site("popoli_ss17_underpass")
        src = "tests/fixtures/interchange_popoli_ss17_underpass.npz (the PopoliTest replay)"
    elif what == "replay":
        place, got = argv[1], replay_site(argv[1])
        src = "tests/fixtures/interchange_%s.npz (tests/interchanges.py)" % argv[1]
    else:
        place, got = argv[1], truth_site(argv[1])
        src = "tests/fixtures/truth_%s (tests/truth.py: the survey's ways with the survey's levels, Earth's ground)" % argv[1]
    audits = got[2]
    text = ia.render(place, audits, header(place, src))
    print(text)
    if out:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, place + ".md"), "w", encoding="utf-8") as fh:
            fh.write(text)


if __name__ == "__main__":
    main(sys.argv[1:])
