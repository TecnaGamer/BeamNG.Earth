"""The measured shape of real roads and railways (fixtures/truth_*.json, tools/capture_truth_fixture.py): swisstopo's
swissTLM3D lines round a few places, each a 3D line with what it is (a motorway, a 6 m road, a track), whether it is
on a bridge or in a tunnel and its level. The heights a place really has, to hold Earth's decisions (which line is
over which, what stands on the ground) and its solved profiles against, offline: the heuristics on their own, then
the solver given the true facts, then both together (the place's replay: fixtures/truth_*.npz, tests/interchanges.py).

Source: Federal Office of Topography swisstopo (swissTLM3D, free with the source named)."""
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np

FIXTURES = Path(__file__).parent / "fixtures"
SITES = ("zurich_brunau", "zurich_europabruecke", "zurich_duttweilerbruecke")

# OBJEKTART of a road a car drives (the rest are paths, squares, ferries): its Earth class
ROAD_CLASS = {"Autobahn": "motorway", "Autostrasse": "trunk", "Ausfahrt": "motorway_link", "Einfahrt": "motorway_link",
              "Zufahrt": "motorway_link", "Dienstzufahrt": "service", "Verbindung": "service",
              "10m Strasse": "primary", "8m Strasse": "secondary", "6m Strasse": "tertiary", "4m Strasse": "residential",
              "3m Strasse": "service"}
PATH_CLASS = {"2m Weg": "path", "1m Weg": "path", "2m Wegfragment": "path", "1m Wegfragment": "path",
              "Markierte Spur": "path", "Klettersteig": "path"}
HALF_M = {"Autobahn": 5.0, "Autostrasse": 5.0, "10m Strasse": 5.0, "8m Strasse": 4.0, "6m Strasse": 3.0,
          "4m Strasse": 2.0, "3m Strasse": 1.5}
BRIDGE = {"Bruecke", "Bruecke mit Galerie", "Gedeckte Bruecke", "Bruecke mit Treppe"}
TUNNEL = {"Tunnel", "Unterfuehrung", "Unterfuehrung mit Treppe", "Galerie"}
NODE_M = 0.3            # m: two lines' ends this close in plan and height are one node of the network
SAME_LEVEL_M = 1.0      # m: lines crossing nearer in height than this meet (a junction), not one over the other


def load(name):
    """SimpleNamespace(origin (lat, lon), half m, lines [SimpleNamespace(id, layer "road" | "rail", objektart, cls
    (Earth's class, "path", "rail" or None: not a way), kind "ground" | "bridge" | "tunnel", level int, xyz (k, 3),
    attrs)]) of fixtures/truth_NAME.json, in the ENU frame of its origin."""
    with open(FIXTURES / f"truth_{name}.json", encoding="utf-8") as f:
        d = json.load(f)
    lines = []
    for layer, key in (("road", "roads"), ("rail", "rails")):
        for r in d[key]:
            xyz = np.asarray(r["xyz"], np.float64).reshape(-1, 3)
            art = r["objektart"]
            cls = "rail" if layer == "rail" else ROAD_CLASS.get(art) or PATH_CLASS.get(art)
            kb = r["kunstbaute"]
            kind = "bridge" if kb in BRIDGE else "tunnel" if kb in TUNNEL else "ground"
            try:
                level = int(r["stufe"])
            except ValueError:
                level = 0
            lines.append(SimpleNamespace(id=r["id"], layer=layer, objektart=art, cls=cls, kind=kind, level=level,
                                         xyz=xyz, attrs=r))
    return SimpleNamespace(origin=tuple(d["origin"]), half=float(d["half_m"]), lines=lines)


def ways(site, paths=False):
    """The site's lines a vehicle or a train runs on (paths too with paths)."""
    return [ln for ln in site.lines if ln.cls is not None and (paths or ln.cls != "path")]


def arclen(xy):
    return np.r_[0.0, np.cumsum(np.hypot(*np.diff(np.asarray(xy, np.float64)[:, :2], axis=0).T))]


def _segments(lines):
    """(a (n, 3), b (n, 3), line index (n,), segment index (n,)) of the lines' segments."""
    A, B, L, K = [], [], [], []
    for i, ln in enumerate(lines):
        A.append(ln.xyz[:-1])
        B.append(ln.xyz[1:])
        L.append(np.full(len(ln.xyz) - 1, i))
        K.append(np.arange(len(ln.xyz) - 1))
    return np.concatenate(A), np.concatenate(B), np.concatenate(L), np.concatenate(K)


def crossings(lines, box=None):
    """[SimpleNamespace(i, j, xy (2,), si, sj (m along each), zi, zj, dz = zi - zj)] where two of the lines cross in
    plan, each once per place; not where they share a node (ends NODE_M apart in plan and in height). box (half m):
    only those within it of the origin."""
    a, b, li, ki = _segments(lines)
    s = [arclen(ln.xyz) for ln in lines]
    out = []
    n = len(a)
    for lo in range(0, n, 512):
        hi = min(n, lo + 512)
        p, r = a[lo:hi, None, :2], (b[lo:hi] - a[lo:hi])[:, None, :2]
        q, t = a[None, :, :2], (b - a)[None, :, :2]
        den = r[..., 0] * t[..., 1] - r[..., 1] * t[..., 0]
        ok = np.abs(den) > 1e-9
        qp = q - p
        with np.errstate(divide="ignore", invalid="ignore"):
            u = (qp[..., 0] * t[..., 1] - qp[..., 1] * t[..., 0]) / den
            v = (qp[..., 0] * r[..., 1] - qp[..., 1] * r[..., 0]) / den
        hit = ok & (u >= 0) & (u <= 1) & (v >= 0) & (v <= 1) & (li[lo:hi, None] < li[None, :])
        for x, y in zip(*np.nonzero(hit)):
            x = int(x) + lo
            y = int(y)
            uu, vv = float(u[x - lo, y]), float(v[x - lo, y])
            pi, pj = a[x] + uu * (b[x] - a[x]), a[y] + vv * (b[y] - a[y])
            i, j = int(li[x]), int(li[y])
            si = float(s[i][ki[x]] + uu * np.hypot(*(b[x] - a[x])[:2]))
            sj = float(s[j][ki[y]] + vv * np.hypot(*(b[y] - a[y])[:2]))
            at_end = min(si, s[i][-1] - si) <= NODE_M and min(sj, s[j][-1] - sj) <= NODE_M
            if at_end and abs(pi[2] - pj[2]) <= NODE_M:
                continue
            if box is not None and max(abs(pi[0]), abs(pi[1])) > box:
                continue
            out.append(SimpleNamespace(i=i, j=j, xy=pi[:2].copy(), si=si, sj=sj, zi=float(pi[2]), zj=float(pj[2]),
                                       dz=float(pi[2] - pj[2])))
    seen, uniq = set(), []
    for c in sorted(out, key=lambda c: (c.i, c.j, c.si)):
        k = (c.i, c.j, round(c.si))
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def separations(lines, box=None, min_m=SAME_LEVEL_M):
    """crossings where one line really is over the other (their heights min_m or more apart)."""
    return [c for c in crossings(lines, box) if abs(c.dz) >= min_m]


def profile(line, step=2.0):
    """(s (m,), xy (m, 2), z (m,)) of a line every step m."""
    s = arclen(line.xyz)
    sv = np.arange(0.0, s[-1] + 1e-9, step) if s[-1] >= step else np.array([0.0, s[-1]])
    return sv, np.c_[np.interp(sv, s, line.xyz[:, 0]), np.interp(sv, s, line.xyz[:, 1])], np.interp(sv, s, line.xyz[:, 2])


MEMBER_KIND = {("road", "ground"): "ground", ("road", "bridge"): "deck", ("road", "tunnel"): "tunnel",
               ("rail", "ground"): "rail", ("rail", "bridge"): "rail_deck", ("rail", "tunnel"): "rail"}


def node_of(p):
    """A line end's node: lines ending at one point (in plan and in height) share it."""
    return (round(float(p[0]), 1), round(float(p[1]), 1), round(float(p[2]), 1))


def members(site, paths=False):
    """(the site's ways as gradeshapes Members with the kinds and levels the survey gives them, the ways): what
    Earth's shapes would be handed were the map's tags all true (a road bridge a deck at its STUFE, a road tunnel a
    tunnel, a railway bridge a rail deck; a track in a tunnel has no kind of its own: a track)."""
    import gradeshapes
    ws = ways(site, paths)
    out = []
    for ln in ws:
        kind = MEMBER_KIND[(ln.layer, ln.kind)]
        half = 2.0 if ln.layer == "rail" else HALF_M.get(ln.objektart, 1.5)
        out.append(gradeshapes.Member(ln.id, ln.xyz[:, :2].copy(), kind, ln.cls, None, float(ln.level), half, None,
                                      frozenset((node_of(ln.xyz[0]), node_of(ln.xyz[-1])))))
    return out, ws


def z_at(line, s):
    """The line's measured height at arc length s (m)."""
    return float(np.interp(s, arclen(line.xyz), line.xyz[:, 2]))


# ---- Earth's side of a place: its replay (the map and the elevation it reads there), its decisions, its shapes ------
MATCH_M = 8.0           # m from a surveyed line's point one of Earth's lines lies and is still that line ...
MATCH_COS = 0.85        # ... going its way within ~32 deg (the crossing line under it is not)
THIN_DECK_M = 0.9       # m: the thinnest deck the laws give a bridge (gradeshapes.DECK_MIN_DEPTH_M)
GRADE_BASE_M = 20.0     # m over which a surveyed road's grade is read (its heights are good to a few dm)


def replay(name):
    """The place's replay (tests/interchanges.load: frame, cells, sample)."""
    import interchanges
    return interchanges.load(name, prefix="truth")


def ground_fn(rep, ws):
    """ground(xy (k, 2)) -> m: the elevation Earth reads at the place, moved by its median distance from the survey's
    heights along the roads on the ground (the two height systems' offset: centimetres at Zurich)."""
    def dem(xy):
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        la, lo, _ = rep.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return np.asarray(rep.sample(la, lo), np.float64)
    d = np.concatenate([ln.xyz[:, 2] - dem(ln.xyz[:, :2]) for ln in ws if ln.kind == "ground" and ln.layer == "road"])
    off = float(np.median(d))
    return lambda xy: dem(xy) + off


def _near(line, p):
    """(distance m, unit tangent) of polyline line (k, 2) at its nearest point to p."""
    a, ab = line[:-1], np.diff(line, axis=0)
    t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
    d = np.hypot(*(a + t[:, None] * ab - p).T)
    k = int(np.argmin(d))
    return float(d[k]), ab[k] / max(float(np.hypot(*ab[k])), 1e-9)


def earth_members(name, site):
    """Earth's own gradeshapes members round the place (grading._Planner._shape_members of the replay's cells: what
    its reading of the map hands the shapes), each once."""
    from unittest import mock
    import grading
    import mapcells
    import revisit
    rep = replay(name)
    with mock.patch.object(revisit, "ON", False):
        md = SimpleNamespace(cell=lambda key, **kw: SimpleNamespace(layers={"roads": rep.cells.get(str(key))}))
        g = grading._Planner(rep.frame, md, lambda la, lo, *a: rep.sample(la, lo))
        seen, out = set(), []
        for k in sorted(rep.cells):
            for m in g._shape_members(mapcells.CellKey(*(int(v) for v in k.split("/")))):
                if m.key not in seen and (np.abs(np.asarray(m.line)[:, :2]) <= site.half + 50.0).all(axis=1).any():
                    seen.add(m.key)
                    out.append(m)
    return out


def earth_orders(name):
    """How Earth's reading of the map orders the place's real separations: {"right", "reversed", "at grade" (no
    order: no clearance held), "unmatched" (one of the two lines has no line of Earth's along it, or both have the
    same one)} -> [(upper's tag, lower's tag, Earth's two members' kind/level)]. A railway's tunnel is left out (Earth
    draws none)."""
    import gradeshapes
    site = load(name)
    ws = ways(site)
    mem = earth_members(name, site)
    lines = [np.asarray(m.line, np.float64)[:, :2] for m in mem]
    boxes = np.array([np.r_[ln.min(0) - MATCH_M, ln.max(0) + MATCH_M] for ln in lines])

    def match(ln, p):
        _d, tt = _near(ln.xyz[:, :2], p)
        best = None
        for q in np.flatnonzero((boxes[:, 0] <= p[0]) & (boxes[:, 2] >= p[0]) & (boxes[:, 1] <= p[1]) & (boxes[:, 3] >= p[1])):
            if (mem[q].kind in ("rail", "rail_deck")) != (ln.layer == "rail"):
                continue
            d, t = _near(lines[q], p)
            if d <= MATCH_M and abs(float(t @ tt)) >= MATCH_COS and (best is None or d < best[0]):
                best = (d, int(q))
        return None if best is None else best[1]
    out = {"right": [], "reversed": [], "at grade": [], "unmatched": []}
    for c in separations(ws, box=site.half):
        a, b = ws[c.i], ws[c.j]
        if (a.layer == "rail" and a.kind == "tunnel") or (b.layer == "rail" and b.kind == "tunnel"):
            continue
        u, d = (a, b) if c.dz > 0 else (b, a)
        tag = (f"{u.cls}/{u.kind}/{u.level}", f"{d.cls}/{d.kind}/{d.level}")
        ea, eb = match(a, c.xy), match(b, c.xy)
        if ea is None or eb is None or ea == eb:
            out["unmatched"].append(tag + ("",))
            continue
        got = gradeshapes._over(mem[ea], mem[eb])
        how = f"{mem[ea].kind}/{mem[ea].level:g} | {mem[eb].kind}/{mem[eb].level:g}"
        out["right" if got == ("i" if c.dz > 0 else "j") else "at grade" if got is None else "reversed"].append(tag + (how,))
    return out


def over_wrong(name):
    """The real separations gradeshapes._over orders wrongly given the survey's own kinds and levels: [(upper's tag,
    lower's tag, its answer)]."""
    import gradeshapes
    site = load(name)
    mem, ws = members(site)
    out = []
    for c in separations(ws, box=site.half):
        got = gradeshapes._over(mem[c.i], mem[c.j])
        if got != ("i" if c.dz > 0 else "j"):
            u, d = (ws[c.i], ws[c.j]) if c.dz > 0 else (ws[c.j], ws[c.i])
            out.append((f"{u.cls}/{u.kind}/{u.level}", f"{d.cls}/{d.kind}/{d.level}", got))
    return out


def levels_wrong(name):
    """The real separations the survey's levels alone do not order (the upper line's STUFE not over the lower's)."""
    site = load(name)
    ws = ways(site)
    return [c for c in separations(ws, box=site.half) if (ws[c.i].level > ws[c.j].level) != (c.dz > 0)
            or ws[c.i].level == ws[c.j].level]


def solved(name):
    """The shapes gradeshapes solves from the survey's facts on Earth's ground: (systems, how many with no answer,
    {(piece kind, class): solved - real heights (m) at the samples}, joins between lines really SAME_LEVEL_M apart)."""
    import gradeshapes as ix
    site = load(name)
    mem, ws = members(site)
    ground = ground_fn(replay(name), ws)
    net, systems = ix.find_systems(mem)
    err, failed, false_joins = {}, 0, 0
    for sysm in systems:
        false_joins += sum(1 for a, sa, b, sb in sysm["joins"] if abs(z_at(ws[a], sa) - z_at(ws[b], sb)) > SAME_LEVEL_M)
        pcs = ix.pieces(net, sysm)
        smp = ix.sample(net, pcs)

        def fixed(i, s):
            sv = np.atleast_1d(np.asarray(s, np.float64))
            z = ground(np.stack([np.interp(sv, net.s[i], net.lines[i][:, 0]), np.interp(sv, net.s[i], net.lines[i][:, 1])], axis=1))
            return np.asarray(z, np.float64) if np.ndim(s) else float(z[0])
        zs = ix.solve_shape(net, sysm, pcs, smp, [ground(x["xy"]) for x in smp], fixed)
        if zs is None:
            failed += 1
            continue
        for pc, x, z in zip(pcs, smp, zs):
            for k in range(len(z)):
                ln = ws[int(x["mem"][k])]
                j = int(np.argmin(np.hypot(*(ln.xyz[:, :2] - x["xy"][k]).T)))
                if np.hypot(*(ln.xyz[j, :2] - x["xy"][k])) <= 3.0:      # (a sample near a surveyed vertex)
                    err.setdefault((pc["kind"], ln.cls), []).append(float(z[k] - ln.xyz[j, 2]))
    return len(systems), failed, {k: np.array(v) for k, v in err.items()}, false_joins


def real_clearances(name):
    """[(real height of the upper road over the lower m, what the laws ask at least m, upper's tag, lower's tag)] of
    the place's bridges over roads and tracks on the ground: the laws' headroom for the line under (gradeshapes.headroom)
    and the thinnest deck (a railway's bridge: its own depth)."""
    import gradeshapes as ix
    site = load(name)
    mem, ws = members(site)
    out = []
    for c in separations(ws, box=site.half):
        u, d = (c.i, c.j) if c.dz > 0 else (c.j, c.i)
        if ws[u].kind != "bridge" or ws[d].kind != "ground":
            continue
        need = ix.headroom(mem[d]) + (ix.RAIL_DECK_DEPTH_M if ws[u].layer == "rail" else THIN_DECK_M)
        out.append((abs(c.dz), need, f"{ws[u].cls}/{ws[u].kind}/{ws[u].level}", f"{ws[d].cls}/{ws[d].kind}/{ws[d].level}"))
    return out


def real_grades(name):
    """[(the steepest grade of a surveyed road over GRADE_BASE_M, the laws' max grade for it, class)] of the place's
    roads long enough to read one."""
    import gradeshapes as ix
    site = load(name)
    mem, ws = members(site)
    out = []
    for m, ln in zip(mem, ws):
        if ln.layer != "road":
            continue
        _s, _xy, z = profile(ln, 2.0)
        n = int(GRADE_BASE_M / 2.0)
        if len(z) > n:
            out.append((float(np.abs(z[n:] - z[:-n]).max() / GRADE_BASE_M), ix.max_grade(m), ln.cls))
    return out


def report(name):
    """The place's measurements (what holds today: tests/test_truth.py's numbers)."""
    import collections
    n, failed, err, fj = solved(name)
    eo = earth_orders(name)
    cl = real_clearances(name)
    gr = real_grades(name)
    return {"site": name, "levels_wrong": len(levels_wrong(name)),
            "over_wrong": dict(collections.Counter(f"{u} over {d}: {g}" for u, d, g in over_wrong(name))),
            "systems": n, "infeasible": failed, "false_joins": fj,
            "solved_minus_real": {f"{k[0]} {k[1]}": [round(float(np.median(v)), 2), round(float(np.percentile(np.abs(v), 95)), 2),
                                                    round(float(np.abs(v).max()), 2), len(v)] for k, v in sorted(err.items())},
            "earth_orders": {k: len(v) for k, v in eo.items()},
            "earth_orders_wrong": dict(collections.Counter(f"{u} over {d}: {k} ({how})" for k in ("reversed", "at grade")
                                                           for u, d, how in eo[k])),
            "real_clearances": len(cl), "real_under_the_laws": sum(1 for dz, need, _u, _d in cl if dz < need),
            "real_clearance_min_median": [round(min(c[0] for c in cl), 2), round(float(np.median([c[0] for c in cl])), 2)] if cl else None,
            "roads_steeper_than_the_laws": sum(1 for g, law, _c in gr if g > law + 0.01), "roads_graded": len(gr)}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    for nm in sys.argv[1:] or SITES:
        print(json.dumps(report(nm), indent=1), flush=True)
