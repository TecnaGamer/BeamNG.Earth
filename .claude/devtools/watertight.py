"""watertight.py LAT LON [R]: the open edges of what Earth builds within R m (default 120) of the point (2026-11-07, the
user's rule: road, sidewalk, kerb, terrain, walls, barriers, bridge ends and tunnel mouths meet with no gap you can see
through or fall into).

Builds the road tiles round the point offline (as trackcheck.py: blockworker.roads_tile, the revisit cache skipped),
captures every mesh the tiles write (cdae.write: walks, walkcol, road, deck, tunnel, barrier, rail) with the terrain
block's drawn ground (roads.build_tile's ground=), and finds the meshes' boundary edges (welded at 1 mm: an edge used
by one triangle only). A boundary edge is sealed when every sample along it (0.25 m) is at or under the ground
(+ TOL_M) or within TOL_M of another triangle (any mesh: the neighbour tile's, a skirt, the road beside a curb); an
open sample HIGH_M or more above the ground is a structure's edge in the air (a deck's side over a river, a parapet's
top) and counted apart. Prints the open length by kind (sidewalk: the slab's material, kerb: the curb band's, road,
deck, tunnel, wall: barrier meshes, rail) and the worst runs with lat/lon.

Every mesh cdae.write writes is closed (shells.close) unless written close=False (terrain, 3dmr models, prewarm); the
capture closes its copy the same way. Options: --raw  the meshes as built before shells.close (the before); --kinds=walks,deck  only these meshes; --top=N  runs listed (default 12); --json=PATH  the counts written."""
import os, sys, tempfile, logging, collections, math, json
os.environ.setdefault("BNE_REVISIT_SKIP", "roads")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "..", "server")))
logging.basicConfig(level=logging.ERROR)
import numpy as np

TOL_M = 0.02            # m: an edge this close to the ground or another surface is sealed
HIGH_M = 2.5            # m: an edge this far over the ground is a structure's, in the air (counted apart)
SAMPLE_M = 0.25         # m between samples along an edge
WELD_M = 0.001          # m: vertices this close are one


def boundary_edges(P, T):
    """Unique welded boundary edges of the mesh (P (n, 3), T (m, 3)): (edges (k, 2) into the welded points, the
    welded points (w, 3), the triangle of each edge, the welded triangles). Pure."""
    P = np.asarray(P, np.float64)
    T = np.asarray(T, np.int64).reshape(-1, 3)
    q = np.round(P / WELD_M).astype(np.int64)
    _, first, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    inv = inv.ravel()
    W = P[first]
    Tw = inv[T]
    keep = (Tw[:, 0] != Tw[:, 1]) & (Tw[:, 1] != Tw[:, 2]) & (Tw[:, 0] != Tw[:, 2])
    tri = np.flatnonzero(keep)
    Tw = Tw[keep]
    E = np.concatenate([Tw[:, [0, 1]], Tw[:, [1, 2]], Tw[:, [2, 0]]])
    et = np.concatenate([tri, tri, tri])
    Es = np.sort(E, 1)
    u, idx, cnt = np.unique(Es, axis=0, return_index=True, return_counts=True)
    b = cnt == 1
    return u[b], W, et[idx[b]], Tw


def _pt_tri_dist(p, a, b, c):
    """Distances from points p (k, 3) to triangles (a, b, c) (k, 3) each (Ericson's closest point). Pure."""
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = (ab * ap).sum(1), (ac * ap).sum(1)
    bp = p - b
    d3, d4 = (ab * bp).sum(1), (ac * bp).sum(1)
    cp = p - c
    d5, d6 = (ab * cp).sum(1), (ac * cp).sum(1)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2
    den = va + vb + vc
    den = np.where(np.abs(den) < 1e-18, 1e-18, den)
    v, w = vb / den, vc / den
    q = a + ab * v[:, None] + ac * w[:, None]
    # the regions outside the face
    m = (d1 <= 0) & (d2 <= 0)
    q[m] = a[m]
    m = (d3 >= 0) & (d4 <= d3)
    q[m] = b[m]
    m = (d6 >= 0) & (d5 <= d6)
    q[m] = c[m]
    m = (vc <= 0) & (d1 >= 0) & (d3 <= 0) & ~((d1 <= 0) & (d2 <= 0))
    t = d1 / np.where(np.abs(d1 - d3) < 1e-18, 1e-18, d1 - d3)
    q[m] = (a + ab * t[:, None])[m]
    m = (vb <= 0) & (d2 >= 0) & (d6 <= 0) & ~((d1 <= 0) & (d2 <= 0))
    t = d2 / np.where(np.abs(d2 - d6) < 1e-18, 1e-18, d2 - d6)
    q[m] = (a + ac * t[:, None])[m]
    m = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
    t = (d4 - d3) / np.where(np.abs((d4 - d3) + (d5 - d6)) < 1e-18, 1e-18, (d4 - d3) + (d5 - d6))
    q[m] = (b + (c - b) * t[:, None])[m]
    return np.linalg.norm(p - q, axis=1)


class Surfaces:
    """Every triangle of the captured meshes (world frame), hashed on a 1 m grid for near(): whether a point is within
    tol of a triangle other than the ones of a given (mesh, welded vertex) pair."""

    def __init__(self, meshes, cell=1.0):
        self.cell = cell
        A, B, C, own = [], [], [], []
        for mi, m in enumerate(meshes):
            W, Tw = m["W"], m["Tw"]
            A.append(W[Tw[:, 0]]), B.append(W[Tw[:, 1]]), C.append(W[Tw[:, 2]])
            own.append(np.c_[np.full(len(Tw), mi), Tw])
        self.A = np.concatenate(A) if A else np.zeros((0, 3))
        self.B = np.concatenate(B) if B else np.zeros((0, 3))
        self.C = np.concatenate(C) if C else np.zeros((0, 3))
        self.own = np.concatenate(own) if own else np.zeros((0, 4), np.int64)
        lo = np.minimum(np.minimum(self.A, self.B), self.C)[:, :2]
        hi = np.maximum(np.maximum(self.A, self.B), self.C)[:, :2]
        self.grid = collections.defaultdict(list)
        i0, i1 = np.floor(lo / cell).astype(int), np.floor(hi / cell).astype(int)
        for t in range(len(self.A)):
            for gx in range(i0[t, 0], i1[t, 0] + 1):
                for gy in range(i0[t, 1], i1[t, 1] + 1):
                    self.grid[(gx, gy)].append(t)

    def near(self, p, mi, va, vb, tol=TOL_M):
        """Whether points p (k, 3) are within tol of a triangle that does not hold welded vertex va or vb of mesh
        mi (the edge's own fan)."""
        out = np.zeros(len(p), bool)
        for k in range(len(p)):
            g = (int(math.floor(p[k, 0] / self.cell)), int(math.floor(p[k, 1] / self.cell)))
            cand = self.grid.get(g)
            if not cand:
                continue
            c = np.asarray(cand)
            o = self.own[c]
            mine = (o[:, 0] == mi) & ((o[:, 1:] == va) | (o[:, 1:] == vb)).any(1)
            c = c[~mine]
            if not len(c):
                continue
            d = _pt_tri_dist(np.repeat(p[k:k + 1], len(c), 0), self.A[c], self.B[c], self.C[c])
            out[k] = bool((d <= tol).any())
        return out


def open_edges(meshes, ground_zs, keep=None):
    """The open stretches of the meshes' boundary edges: [(mesh kind, edge kind, length open (m), worst gap over the
    ground (m), midpoint (3,), high)] per edge with an open sample. meshes: [{"kind", "P" (world), "T", "M" (tri
    material) or None}]; ground_zs(xy (k, 2)) -> z; keep(xy) -> bool mask of the edges reported (the area)."""
    for m in meshes:
        E, W, et, Tw = boundary_edges(m["P"], m["T"])
        m.update(E=E, W=W, et=et, Tw=Tw)
    S = Surfaces(meshes)
    out = []
    for mi, m in enumerate(meshes):
        E, W, et = m["E"], m["W"], m["et"]
        if not len(E):
            continue
        a, b = W[E[:, 0]], W[E[:, 1]]
        mid = 0.5 * (a + b)
        sel = np.ones(len(E), bool) if keep is None else keep(mid[:, :2])
        L = np.linalg.norm(b - a, axis=1)
        for e in np.flatnonzero(sel & (L > 1e-4)):
            n = max(2, int(math.ceil(L[e] / SAMPLE_M)) + 1)
            t = np.linspace(0.0, 1.0, n)[:, None]
            s = a[e] + (b[e] - a[e]) * t
            gz = ground_zs(s[:, :2])
            h = s[:, 2] - gz
            bad = ~(np.isnan(gz) | (h <= TOL_M))
            if not bad.any():
                continue
            bad[bad] = ~S.near(s[bad], mi, E[e, 0], E[e, 1])
            if not bad.any():
                continue
            frac = bad.mean() if n > 2 else 1.0
            mat = int(m["M"][m["et"][e]]) if m.get("M") is not None else -1
            out.append((m["kind"], edge_kind(m["kind"], mat), float(L[e] * frac), float(np.nanmax(h[bad])), mid[e],
                        bool(np.nanmax(h[bad]) >= HIGH_M)))
    return out


def edge_kind(mesh_kind, mat):
    if mesh_kind in ("walks", "walkcol"):
        return "kerb" if mat == 0 else "sidewalk"
    return {"road": "road", "deck": "deck", "tunnel": "tunnel", "barrier": "wall", "rail": "rail"}.get(mesh_kind,
                                                                                                    mesh_kind)


def main():
    try:                    # below normal priority (the machine is shared with the game)
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x4000)
    except Exception:
        pass
    import blockworker as bw
    import roads as roadsmod
    import geodesy
    import cdae
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = dict(a[2:].split("=", 1) if "=" in a else (a[2:], "1") for a in sys.argv[1:] if a.startswith("--"))
    LAT, LON = float(args[0]), float(args[1])
    R = float(args[2]) if len(args) > 2 else 120.0
    kinds = set(opts["kinds"].split(",")) if "kinds" in opts else {"walks", "walkcol", "road", "deck", "tunnel",
                                                                    "barrier", "rail", "bld", "bldcol", "railcol"}
    if "raw" in opts:       # --raw: the meshes as built, not closed (shells.close off): the before
        import shells
        shells.close = lambda P, T, TM=None, per_vertex=(), mouths=True, sink=0.0: (P, T, TM, list(per_vertex))
    st = bw._State(os.path.expanduser("~/.cache/beamng-earth"), False, True, True, True, False, True, True)
    bw._s = st
    frame = geodesy.EnuFrame(LAT, LON)
    grounds, meshes = [], []
    orig_build = roadsmod.build_tile

    def grab_build(frame_, tx, ty, cells, elev, *a, **kw):
        if kw.get("ground") is not None:
            grounds.append((roadsmod.tile_bbox(tx, ty), kw["ground"]))
        return orig_build(frame_, tx, ty, cells, elev, *a, **kw)
    roadsmod.build_tile = grab_build
    orig_write = cdae.write

    def grab_write(path, P, T, *a, **kw):
        base = os.path.basename(path)
        kind = base.split("_", 1)[0]
        if kind in kinds and len(P):
            parts = base[:-5].split("_")
            tx, ty = int(parts[-2]), int(parts[-1])
            x0, y0, _, _ = roadsmod.tile_bbox(tx, ty)
            M = kw.get("tri_materials")
            how = kw.get("close", True)
            Pc, Tc, Mc = P, T, M
            if how:             # what cdae.encode writes: the mesh closed (shells.close)
                import shells
                Pc, Tc, Mc, _ = shells.close(P, T, M, mouths=how != "under")
            meshes.append({"kind": kind, "P": np.asarray(Pc, np.float64) + [x0, y0, 0.0],
                           "T": np.asarray(Tc, np.int64).reshape(-1, 3),
                           "M": None if Mc is None else np.asarray(Mc).ravel()})
        return orig_write(path, P, T, *a, **kw)
    cdae.write = grab_write
    out = tempfile.mkdtemp()
    import atexit, shutil
    atexit.register(shutil.rmtree, out, True)
    T_ = roadsmod.TILE
    m = R + 20.0
    need = [(tx, ty) for tx in range(int(math.floor(-m / T_)), int(math.floor(m / T_)) + 1)
            for ty in range(int(math.floor(-m / T_)), int(math.floor(m / T_)) + 1)]
    print(f"building {len(need)} road tiles round {LAT},{LON} (R {R:.0f} m)", flush=True)
    for tx, ty in need:
        try:
            bw.roads_tile("p", LAT, LON, 15, tx, ty, out, "/x", True, 0.0, buildings=True)
        except Exception as e:
            print("tile", tx, ty, "failed:", type(e).__name__, e)

    def ground_zs(P):
        z = np.full(len(P), np.nan)
        for bb, fn in grounds:
            mm = (P[:, 0] >= bb[0]) & (P[:, 0] <= bb[2]) & (P[:, 1] >= bb[1]) & (P[:, 1] <= bb[3]) & np.isnan(z)
            if mm.any():
                z[mm] = np.asarray(fn(P[mm, 0], P[mm, 1]), np.float64).ravel()
        return z

    def latlon(p):
        la, lo, _ = frame.enu_to_geodetic(np.array([float(p[0])]), np.array([float(p[1])]), 0.0)
        return round(float(la[0]), 6), round(float(lo[0]), 6)
    tris = collections.Counter()
    for m in meshes:
        tris[m["kind"]] += len(m["T"])
    print("triangles written by mesh:", dict(sorted(tris.items())), "total", sum(tris.values()))
    rows = open_edges(meshes, ground_zs, keep=lambda xy: np.hypot(xy[:, 0], xy[:, 1]) <= R)
    import shells
    shell = collections.defaultdict(lambda: [0, 0.0])     # the shells' openings in the area: boundary loops'
    for m in meshes:                                       # edges (a T-junction's slit, no area, left out)
        P, T = m["P"], m["T"]
        tri_of = np.zeros(len(P), np.int64)
        tri_of[T.ravel()] = np.repeat(np.arange(len(T)), 3)
        for loop in shells.loops(P, T):
            Q = P[loop]
            if np.linalg.norm(np.cross(Q, np.roll(Q, -1, 0)).sum(0)) * 0.5 < shells.SLIT_M2:
                continue
            mid = 0.5 * (Q + np.roll(Q, -1, 0))
            L = np.linalg.norm(np.roll(Q, -1, 0) - Q, axis=1)
            inside = np.hypot(mid[:, 0], mid[:, 1]) <= R
            mat = int(m["M"][tri_of[loop[0]]]) if m.get("M") is not None else -1
            k = (m["kind"], edge_kind(m["kind"], mat))
            shell[k][0] += int(inside.sum())
            shell[k][1] += float(L[inside].sum())
    print("open shell edges (boundary loops' edges, used by one triangle; tile seams included; T-junction slits left out) by mesh / kind:")
    for k in sorted(shell):
        print(f"  {k[0]:8s} {k[1]:9s} {shell[k][0]:6d} {shell[k][1]:9.1f} m")
    tot = collections.defaultdict(lambda: [0, 0.0])
    high = collections.defaultdict(lambda: [0, 0.0])
    for mk, ek, L, h, mid, hi in rows:
        d = high if hi else tot
        d[(mk, ek)][0] += 1
        d[(mk, ek)][1] += L
    print("exposed: open edges over the ground, not on another surface (count, m open) by mesh / kind:")
    for k in sorted(tot):
        print(f"  {k[0]:8s} {k[1]:9s} {tot[k][0]:6d} {tot[k][1]:9.1f} m")
    print(f"  total             {sum(v[0] for v in tot.values()):6d} {sum(v[1] for v in tot.values()):9.1f} m")
    if high:
        print(f"structure edges in the air (>= {HIGH_M} m over the ground, not counted):",
              ", ".join(f"{k[0]}/{k[1]} {v[0]} ({v[1]:.0f} m)" for k, v in sorted(high.items())))
    # the worst runs: open edges clustered on a 5 m grid, by open length
    cl = collections.defaultdict(lambda: [0.0, 0.0, None, collections.Counter()])
    for mk, ek, L, h, mid, hi in rows:
        if hi:
            continue
        c = cl[(int(mid[0] // 5), int(mid[1] // 5))]
        c[0] += L
        if h > c[1]:
            c[1], c[2] = h, mid
        c[3][f"{mk}/{ek}"] += L
    top = int(opts.get("top", 12))
    print(f"worst {top} spots (5 m squares): open m, largest gap over the ground, lat, lon, kinds")
    for k, (L, h, mid, kc) in sorted(cl.items(), key=lambda kv: -kv[1][0])[:top]:
        la, lo = latlon(mid)
        print(f"  {L:6.1f} m  gap {h:5.2f} m  {la},{lo}  " + ", ".join(f"{a} {b:.1f}" for a, b in kc.most_common(3)))
    if "json" in opts:
        with open(opts["json"], "w") as fh:
            json.dump({"lat": LAT, "lon": LON, "R": R, "tris": dict(tris), "shell": {"/".join(k): v for k, v in shell.items()},
                       "open": {"/".join(k): v for k, v in tot.items()},
                       "high": {"/".join(k): v for k, v in high.items()}}, fh, indent=1)


if __name__ == "__main__":
    main()
