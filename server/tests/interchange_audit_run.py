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


EDGE_M = 150.0          # m: an end this near a map cell that was not read may meet a road there


class PlannerMeta:
    """What the map has of a road feature, by its Earth id, read from the planner's cells when asked: {osm: the way, name,
    layer, oneway, dir}. A dict by .get()."""

    def __init__(self, planner, net):
        self.p, self.net = planner, net
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
    real_find, real_solve = gs.find_systems, gs.solve_shape
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

    def find(members, reader=None, trace=None):
        tr = {}
        net, systems = real_find(members, reader, tr)
        held.update(net=net, systems=systems, trace=tr, meta=PlannerMeta(planner, net),
                    namer=None, rels={})
        held["namer"] = ia.Namer(net, frame_ll(planner.frame), held["meta"], at_edge)
        per.clear()
        return net, systems

    def audit(net, sysm, pcs, smp, ground, fixed_z, diagnostics=None):
        held["fixed"] = fixed_z
        n = next(k for k, s in enumerate(held["systems"]) if s is sysm)
        per[n] = ia.audit_system(net, sysm, pcs, smp, ground, fixed_z, held["namer"], held["rels"])
        if solve:
            diag = {}
            got = real_solve(net, sysm, pcs, smp, ground, fixed_z, diag)
            per[n][3]["stats"]["kernel"] = dict(solved=got is not None, status=diag.get("status"),
                                                 qp_error=diag.get("qp_error"))
        return None
    with mock.patch.object(gs, "find_systems", find), mock.patch.object(gs, "solve_shape", audit):
        (region_calc or planner._shape_region_calc)(home, str(home))
    net, systems = held["net"], held["systems"]
    # (the systems round the region the planner did not solve: the same steps, on the same ground and fixed heights)
    for n, sysm in enumerate(systems):
        if n not in per and "fixed" in held:
            pcs = gs.pieces(net, sysm)
            smp = gs.sample(net, pcs)
            per[n] = ia.audit_system(net, sysm, pcs, smp, [planner._ground_line(x["xy"]) for x in smp], held["fixed"],
                                     held["namer"], held["rels"])
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
    return ia.audit_members(mem, ground, make_fixed, frame_ll(rep.frame), meta) + (rep,)


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
