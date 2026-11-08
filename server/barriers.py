"""Mapped, ground-level boundaries and passable gate openings, in the near road tiles.

Pure geometry from the pinned infrastructure layer; no inferred parcel boundaries. Gates are openings/posts,
not simulated closed obstacles. Continuous meshes avoid Forest panel thinning and uniform-scale distortions.
"""
import logging
from dataclasses import dataclass

import numpy as np

import geodesy
import mapgeom
import roads
import fences
import roadmesh
from fences import _tags, metres

log = logging.getLogger("earthd.barriers")
MARGIN_M = 16.0
STEP_M = 2.0
MATCH_M = 1.0
GATE_WIDTH_M = 3.5
MAX_VERTS = 60000
PARTS = 4                        # as for buildings: each shape stays under the engine's 16-bit indices
FIELDS = tuple(f"{kind}{n if n > 1 else ''}" for kind in ("boundary", "hedge") for n in range(1, PARTS + 1))
KINDS = {"fence", "wall", "hedge"}
GATES = {"gate", "lift_gate", "entrance"}
DEFAULTS = {"fence": (1.3, 0.12), "wall": (1.5, 0.3), "hedge": (1.8, 0.8)}
WOOD = "bne_boundary_wood"
WALL_NEUTRAL = "bne_wall_neutral"
METAL = "bne_barrier_metal"
HEDGE = "bne_barrier_hedge"
# Reuse the upstream hard-face topology once; tiny posts/rails need only an affine transform of it.
# Avoid rebuilding the general extrusion's face loops for every small post or rail.
_PRISM_P, _PRISM_T, _PRISM_UV = roadmesh.barrier_solid(
    np.array([[0., -.5], [1., -.5]]), np.array([[0., 1.], [0., 1.]]), np.zeros(2), np.ones(2),
    section=((0, 0), (1, 0), (1, 1), (0, 1)))



@dataclass
class Run:
    fid: str
    kind: str
    xy: np.ndarray
    s: np.ndarray               # source arc in ECEF metres: independent of the current anchor
    height: float
    width: float
    style: str
    material: str
    gaps: tuple = ()             # arc-length intervals of resolved openings
    existing: bool = False       # upstream stock chain-link/noise-wall renderer owns this run
    barbed: bool = False
    electric: bool = False


def _arc(xy):
    return np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]


def _at(xy, s, at):
    return np.column_stack([np.interp(at, s, xy[:, j]) for j in range(2)])


def _project(xy, q, stations=None):
    d = np.diff(xy, axis=0)
    l2 = (d * d).sum(1)
    t = np.clip(((q - xy[:-1]) * d).sum(1) / l2, 0, 1)
    p = xy[:-1] + t[:, None] * d
    ds = np.linalg.norm(p - q, axis=1)
    k = int(np.argmin(ds))
    s = _arc(xy) if stations is None else stations
    return float(ds[k]), float(s[k] + t[k] * (s[k + 1] - s[k])), p[k], d[k] / np.sqrt(l2[k])


def _style(kind, tags):
    material = str(tags.get("material", "")).strip().lower()
    if kind == "wall":
        # An explicit material wins over a construction hint. Unrecorded/unsupported material stays neutral.
        hint = material or str(tags.get("wall", "")).strip().lower()
        mat = {"brick": "bne_wall_brick", "stone": "bne_wall_stone", "dry_stone": "bne_wall_stone",
               "limestone": "bne_wall_stone", "sandstone": "bne_wall_stone",
               "concrete": "bne_bridge_concrete", "wood": WOOD, "timber": WOOD}.get(hint, WALL_NEUTRAL)
        return "wall", mat
    if kind == "hedge":
        return "hedge", HEDGE
    ft = str((tags.get("fence_type") or tags.get("fence:type", ""))).strip().lower()
    if ft == "concrete":
        return "panel", "bne_bridge_concrete"
    if ft in fences.CHAIN_TYPES:
        return "mesh", "bne_barrier_mesh"
    if ft in {"slatted", "picket"}:
        return "picket", WOOD
    if ft in {"panel", "privacy", "solid", "corrugated_metal"}:
        return "panel", METAL if ft == "corrugated_metal" or material in {"steel", "metal"} else WOOD
    if ft in {"railing", "bars", "metal"}:
        return "railing", METAL
    if ft == "electric":
        return "electric", METAL
    if ft == "barbed_wire":
        return "barbed", METAL
    if ft == "wire" or material in {"wire", "steel", "metal"}:
        return "wire", METAL
    return "rail", WOOD         # unrecorded construction: low, open timber rails, not an invented solid wall


def plan(frame, cells, box, stats=None):
    """Runs intersecting box plus a gate-matching margin. Dedup first; filter each piece before any DEM work."""
    stats = stats if stats is not None else {}
    stats.update(gates=0, resolved=0, ambiguous=0, unmatched=0)
    near = (box[0] - MARGIN_M, box[1] - MARGIN_M, box[2] + MARGIN_M, box[3] + MARGIN_M)
    seen, runs, gates = set(), [], []
    for f in cells:
        if f is None:
            continue
        cls = f.attrs.get("class")
        xy_all = None
        for i, fid in enumerate(f.ids):
            if str(fid) in seen:
                continue
            seen.add(str(fid))
            tags = _tags(f, i)
            existing = fences.kind_of(f, i)
            kind = str(tags.get("barrier") or (cls[i] if cls is not None else ""))
            if existing is not None:
                kind = "fence" if existing == fences.G_CHAIN else "wall"
            if kind not in KINDS | GATES:
                continue
            level = f.attrs.get("level")
            lv = tags.get("layer", level[i] if level is not None and np.isfinite(level[i]) else "0")
            try:
                above = float(lv) != 0
            except (ValueError, TypeError):
                above = True
            if above or any(tags.get(k, "no") not in ("no", "false", "0", "") for k in ("tunnel", "bridge")):
                continue
            if tags.get("wall") in {"retaining", "retaining_wall"}:
                continue
            if xy_all is None:
                xy_all = f.xy_in(frame)
            for geo, part in zip(f.parts(i), f.parts(i, xy_all)):
                if not part or not len(part[0]):
                    continue
                xy = np.asarray(part[0], np.float64)
                if not np.isfinite(xy).all() or (xy.max(0) < near[:2]).any() or (xy.min(0) > near[2:]).any():
                    continue
                if kind in GATES:
                    if f.kind[i] not in (mapgeom.POINT, mapgeom.LINE):
                        continue
                    q = xy.mean(0)
                    width = np.linalg.norm(xy[-1] - xy[0]) if len(xy) > 1 else GATE_WIDTH_M
                    width = metres(tags.get("maxwidth:physical"), None, 0.5, 20) or metres(
                        tags.get("width"), width if 0.5 <= width <= 20 else GATE_WIDTH_M, 0.5, 20)
                    gates.append((str(fid), q, width, kind))
                    continue
                if f.kind[i] != mapgeom.LINE or len(xy) < 2:
                    continue                # hedge areas and point fences are not linear boundary geometry
                keep = np.r_[True, np.linalg.norm(np.diff(xy, axis=0), axis=1) > 1e-6]
                ll, xy = geo[0][keep], xy[keep]
                if len(xy) < 2 or not roads.clip_polyline(xy, *near):
                    continue
                height = f.attrs.get("height")
                h = height[i] if height is not None else None
                h = metres(tags.get("height"), metres(h, DEFAULTS[kind][0]))
                w = metres(tags.get("width"), DEFAULTS[kind][1], 0.03, 4)
                style, mat = _style(kind, tags)
                s = _arc(np.column_stack(geodesy.geodetic_to_ecef(ll[:, 1], ll[:, 0], 0)))
                runs.append(Run(str(fid), kind, xy, s, h, w, style, mat, existing=existing is not None and metres(tags.get("width"), None, 0.03, 4) is None,
                                barbed=tags.get("barrier:barbed_wire") == "yes",
                                electric=tags.get("barrier:electric") == "yes"))
    runs.sort(key=lambda r: (r.fid, tuple(r.xy[0]), tuple(r.xy[-1])))
    for _, q, width, kind in sorted(gates, key=lambda g: g[0]):
        stats["gates"] += 1
        hits = sorted((_project(r.xy, q, r.s) + (n,) for n, r in enumerate(runs)), key=lambda h: h[0])
        if not hits or hits[0][0] > MATCH_M:
            stats["unmatched"] += 1
            continue
        hits = [h for h in hits if h[0] <= min(MATCH_M, hits[0][0] + 0.15)]
        if any(np.linalg.norm(h[2] - hits[0][2]) > 0.25 or abs(h[3] @ hits[0][3]) < 0.97 for h in hits[1:]):
            stats["ambiguous"] += 1
            continue
        stats["resolved"] += 1
        for _, s, _, _, n in hits:
            r = runs[n]
            r.gaps += ((s - width / 2, s + width / 2, kind),)
    for r in runs:
        gaps = list(r.gaps)
        length = r.s[-1]
        if np.linalg.norm(r.xy[0] - r.xy[-1]) < 1e-6:
            gaps += [(lo + length, hi + length, k) for lo, hi, k in r.gaps if lo < 0]
            gaps += [(lo - length, hi - length, k) for lo, hi, k in r.gaps if hi > length]
        merged = []
        for lo, hi, kind in sorted(gaps):
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(hi, merged[-1][1]), merged[-1][2])
            else:
                merged.append((lo, hi, kind))
        r.gaps = tuple(merged)
    if stats["gates"]:
        log.debug("boundary gates: %s", stats)
    return runs


def load(frame, tx, ty, md):
    cells = []
    for k in roads.cells_of_tile(frame, tx, ty, MARGIN_M):
        try:
            cells.append(md.cell(k, layers=("infrastructure",), kind="demand").layers.get("infrastructure"))
        except Exception as exc:        # optional map data unavailable: roads must still load (as mapped signals)
            log.debug("boundary infrastructure unavailable in %s: %s", k, exc)
    return plan(frame, cells, roads.tile_bbox(tx, ty))


def suppress_fences(props, runs):
    """Only overlapping procedural front-fence segments go; doors, driveways and unrelated yards stay."""
    def keep(row):
        if row[0] != "fence":
            return True
        p = np.asarray(row[1:3])
        d = np.array([np.cos(np.radians(row[4])), np.sin(np.radians(row[4]))]) * 2.7
        for r in runs:
            # Include the mapped run's gate gaps: a made-up panel must not fill a mapped opening.
            if any(_project(r.xy, q)[0] < 1.0 for q in (p - d, p, p + d)):
                return False
        return True
    props[:] = [r for r in props if keep(r)]


def stock_clearance(runs):
    """Keep stock panels out of mapped gate gaps and spans owned by the dimensioned mesh."""
    by_id = {}
    for run in runs:
        if not run.existing or run.gaps:
            by_id.setdefault(run.fid, []).append(run)
    if not by_id:
        return None

    def keep(fid, centre, length, deck_hint):
        if deck_hint is True:
            return True
        for run in by_id.get(fid, ()):
            distance, station, _, _ = _project(run.xy, centre, run.s)
            if distance <= MATCH_M and (not run.existing or any(
                    station + length / 2 > lo and station - length / 2 < hi for lo, hi, _ in run.gaps)):
                return False
        return True
    return keep


class _Mesh:
    def __init__(self, origin):
        self.origin = np.asarray(origin)
        self.p, self.t, self.uv, self.mi, self.materials = [], [], [], [], []
        self.n = 0
        self.parts = []
        self.boxes = []

    def add(self, p, tri, uv, material):
        if len(p) > MAX_VERTS:
            raise ValueError("boundary primitive exceeds its vertex budget")
        if self.n + len(p) > MAX_VERTS:
            self.parts.append((self.result(), self.boxes))
            self.p, self.t, self.uv, self.mi, self.materials, self.n = [], [], [], [], [], 0
            self.boxes = []
        if material not in self.materials:
            self.materials.append(material)
        local = p - self.origin
        self.p.append(local)
        self.t.append(tri + self.n)
        self.uv.append(uv)
        self.mi.extend([self.materials.index(material)] * len(tri))
        self.boxes.append(np.r_[local.min(0) + self.origin, local.max(0) + self.origin].tolist())
        self.n += len(p)

    def prism(self, a, b, width, za, zb, height, material, station=0.0):
        d = b - a
        length = np.linalg.norm(d)
        if length < 1e-6:
            return
        normal = np.array([-d[1], d[0]]) / length
        p = np.empty_like(_PRISM_P)
        p[:, :2] = a + _PRISM_P[:, 0, None] * d + _PRISM_P[:, 1, None] * (normal * width)
        p[:, 2] = za + _PRISM_P[:, 0] * (zb - za) + _PRISM_P[:, 2] * height
        uv = _PRISM_UV.copy()
        uv[:-8, 0] = uv[:-8, 0] * length + station * roadmesh.UV_M
        uv[:-8, 1] = np.interp(uv[:-8, 1] / roadmesh.UV_M, [0, 1, 2, 3, 4],
                              [0, width, width + height, 2 * width + height, 2 * (width + height)]) * roadmesh.UV_M
        uv[-8:] *= [width, height]     # planar end caps
        self.add(p, _PRISM_T, uv, material)

    def wall(self, run, spans, z):
        """Continuous sides between true bends/gaps; shared source mitres and UV phase survive tile cuts."""
        delta = np.diff(run.xy, axis=0)
        tangent = delta / np.linalg.norm(delta, axis=1)[:, None]
        segment_n = np.c_[-tangent[:, 1], tangent[:, 0]]
        vertex_n = roadmesh._normals(run.xy)
        if np.linalg.norm(run.xy[0] - run.xy[-1]) < 1e-6:
            vertex_n = roadmesh._normals(np.r_[run.xy[-2:-1], run.xy, run.xy[1:2]])[1:-1]
        # Each side owns its normal at a cusp: never collapse the cross-section at an out-and-back node.
        cusp = np.linalg.norm(vertex_n, axis=1) < 0.1
        limit = (MAX_VERTS - 8) // 8       # barrier_solid's four face strips and two caps
        if limit < 2:
            raise ValueError("boundary vertex budget cannot hold a wall section")

        def emit(group):
            ss = np.r_[[s[0] for s, _ in group], group[-1][0][1]]
            xy = np.r_[[s[2][0] for s, _ in group], group[-1][0][2][-1:]]
            zz = np.r_[[v[0] for _, v in group], group[-1][1][1]]
            seg = np.clip(np.searchsorted(run.s, ss, side="right") - 1, 0, len(segment_n) - 1)
            normals = segment_n[seg].copy()
            node = np.clip(np.searchsorted(run.s, ss), 0, len(run.s) - 1)
            prev = np.maximum(node - 1, 0)
            node = np.where(np.abs(run.s[prev] - ss) < np.abs(run.s[node] - ss), prev, node)
            at_node = np.abs(run.s[node] - ss) < 1e-6
            normals[at_node] = vertex_n[node[at_node]]
            normals[at_node & cusp[node]] = segment_n[seg[at_node & cusp[node]]]
            if at_node[-1] and cusp[node[-1]]:
                normals[-1] = segment_n[max(0, node[-1] - 1)]
            width = run.width * np.linalg.norm(normals, axis=1)
            for start in range(0, len(ss) - 1, limit - 1):
                sl = slice(start, start + limit)
                profile = np.zeros((len(ss[sl]), 4, 2))
                profile[:, 1:3, 0] = width[sl, None]
                profile[:, 2:, 1] = 1
                p, tri, uv = roadmesh.barrier_solid(xy[sl] - normals[sl] * run.width / 2, normals[sl],
                    zz[sl] - 0.1, np.full(len(ss[sl]), run.height + 0.1), v=ss[sl], section=profile)
                self.add(p, tri, uv, run.material)

        group = []
        for span, heights in zip(spans, z):
            if group:
                prev = group[-1][0]
                a, b = prev[2][1] - prev[2][0], span[2][1] - span[2][0]
                straight = a @ b > 0 and abs(a[0] * b[1] - a[1] * b[0]) <= 1e-7 * np.linalg.norm(a) * np.linalg.norm(b)
                if abs(prev[1] - span[0]) > 1e-6 or not straight:
                    emit(group)
                    group = []
            group.append((span, heights))
        if group:
            emit(group)


    def result(self):
        return (np.concatenate(self.p), np.concatenate(self.t), np.concatenate(self.uv),
                self.materials, np.asarray(self.mi, np.int32)) if self.p else None

    def finish(self):
        out = self.parts + ([(self.result(), self.boxes)] if self.p else [])
        if len(out) > PARTS:
            raise ValueError("boundary tile exceeds four 60000-vertex meshes")
        return out


def _spans(r, box):
    """Exact gap and tile cuts; sample phase stays on the original piece, not the clipped tile."""
    s = r.s
    # Clip before resampling: a huge feature may contain many kilometres outside this tile.
    for piece in roads.clip_polyline(np.c_[r.xy, s], *box):
        start, end = piece[0, 2], piece[-1, 2]
        cuts = [start, end, *piece[:, 2]]
        cuts.extend(np.arange(np.ceil(start / STEP_M) * STEP_M, end, STEP_M))
        for lo, hi, _ in r.gaps:
            cuts.extend([np.clip(lo, start, end), np.clip(hi, start, end)])
        cuts = np.unique(cuts)
        for a, b in zip(cuts[:-1], cuts[1:]):
            if b - a > 1e-5 and not any(lo < (a + b) / 2 < hi for lo, hi, _ in r.gaps):
                yield a, b, _at(r.xy, s, np.array([a, b]))


def tile_meshes(runs, box, ground, road_lines=()):
    """{boundary[2..4], hedge[2..4]: roadmesh mesh}. Ground receives only visible local sample points.

    Walls and fences collide with their visible geometry; leaf-textured hedge volumes only render.
    No random ranks or panel caps: simplification must never make a hole in a physical boundary.
    """
    meshes = {k: _Mesh((box[0], box[1], 0)) for k in ("boundary", "hedge")}
    # Only drawn vehicle-road classes clear boundaries; untyped legacy footprints still count.
    others = {n: (xy, half) for n, (xy, half, *extra) in enumerate(road_lines)
              if not extra or not extra[0] or extra[0] in roads.STYLE}
    conflicts = 0
    def blocked(xy, gap):
        return roads._on_carriageways(xy, others, gap) if others else np.zeros(len(xy), bool)
    for r in runs:
        if r.existing:
            continue
        spans = list(_spans(r, box))
        if spans and others:
            probes = np.concatenate([np.array([p[0], p.mean(0), p[1]]) for _, _, p in spans])
            hit = blocked(probes, r.width / 2 + 0.05).reshape(-1, 3).any(1)
            conflicts += int(hit.sum())
            spans = [span for span, cut in zip(spans, hit) if not cut]
        if not spans:
            continue
        xy = np.concatenate([p for _, _, p in spans])
        z = np.asarray(ground(xy), np.float64).reshape(-1, 2)
        if not np.isfinite(z).all():
            continue
        mesh = meshes["hedge" if r.kind == "hedge" else "boundary"]
        if r.kind == "wall":
            meshes["boundary"].wall(r, spans, z)
            continue
        for (sa, sb, (a, b)), (za, zb) in zip(spans, z):
            if r.style in {"wall", "hedge", "panel", "mesh"}:
                mesh.prism(a, b, r.width, za - 0.1, zb - 0.1, r.height + 0.1, r.material, sa)
                if r.style in {"wall", "hedge"}:
                    continue
            # Rails follow slopes; posts are at stable source arc stations (including gate edges), never tile edges.
            heights = (0.25, 0.55, 0.85) if r.style == "electric" else (0.38, 0.85)
            for h in heights:
                mesh.prism(a, b, 0.07 if r.style == "rail" else 0.025,
                           za + h * r.height, zb + h * r.height, 0.09 if r.style == "rail" else 0.025,
                           WOOD if r.style in {"rail", "picket"} else METAL, sa)
            if r.electric or r.barbed:
                mesh.prism(a, b, 0.02, za + r.height, zb + r.height, 0.02, METAL)
            if r.style == "barbed" or r.barbed:
                levels = (0.38, 0.85) if r.style == "barbed" else (1.0,)
                for at in np.arange(np.ceil(sa / 0.5) * 0.5, sb - 1e-6, 0.5):
                    p = _at(r.xy, r.s, [at])[0]
                    gz = za + (zb - za) * (at - sa) / (sb - sa)
                    for h in levels:
                        mesh.prism(p - [0.05, 0.05], p + [0.05, 0.05], 0.015,
                                   gz + h * r.height - 0.03, gz + h * r.height + 0.03, 0.015, METAL)
            spacing = 0.25 if r.style == "railing" else 0.18 if r.style == "picket" else STEP_M
            posts = list(np.arange(np.ceil(sa / spacing) * spacing, sb - 1e-6, spacing))
            posts.extend(v for gap in r.gaps for v in gap[:2] if sa - 1e-6 <= v < sb - 1e-6)
            s = r.s
            for at in sorted(set(posts)):
                if any(lo - 0.06 <= at <= hi + 0.06 for lo, hi, _ in r.gaps):
                    continue
                p = _at(r.xy, s, [at])[0]
                # Ownership by centre: no duplicated posts on adjoining tiles.
                if not (box[0] <= p[0] < box[2] and box[1] <= p[1] < box[3]):
                    continue
                if blocked(p[None, :], r.width / 2 + 0.05)[0]:
                    continue
                gz = za + (zb - za) * (at - sa) / (sb - sa)
                w = min(r.width, 0.07 if r.style == "picket" else 0.04 if r.style == "railing" else 0.08)
                mat = r.material if r.style == "panel" else WOOD if r.style in {"rail", "picket"} else METAL
                mesh.prism(p - [w / 2, 0], p + [w / 2, 0], w, gz - 0.1, gz - 0.1, r.height + 0.1, mat)
                if r.style == "electric" or r.electric:
                    for h in heights if r.style == "electric" else (1.0,):
                        mesh.prism(p - [0.05, 0], p + [0.05, 0], 0.1,
                                   gz + h * r.height - 0.025, gz + h * r.height - 0.025, 0.05,
                                   "m_gm_concrete_plain")
        if r.kind in {"wall", "hedge"}:
            # Fill the outer wedge between butt-ended prisms at sharp source vertices.
            s = r.s
            for j, p in enumerate(r.xy[1:-1], 1):
                if not (box[0] <= p[0] < box[2] and box[1] <= p[1] < box[3]):
                    continue
                if blocked(p[None, :], r.width / 2 + 0.05)[0]:
                    continue
                if any(lo - r.width <= s[j] <= hi + r.width for lo, hi, _ in r.gaps):
                    continue
                a, b = r.xy[j] - r.xy[j - 1], r.xy[j + 1] - r.xy[j]
                if abs(a[0] * b[1] - a[1] * b[0]) < 1e-6:
                    continue
                gz = float(ground(p[None, :])[0])
                mesh.prism(p - [r.width / 2, 0], p + [r.width / 2, 0], r.width,
                           gz - 0.1, gz - 0.1, r.height + 0.1, r.material)
        if r.kind != "wall":
            s = r.s
            at = [v for lo, hi, _ in r.gaps for v in (lo - 0.1, hi + 0.1) if 0 <= v <= s[-1]]
            if at:
                p = _at(r.xy, s, at)
                p = p[((p >= box[:2]) & (p < box[2:])).all(1)]
                p = p[~blocked(p, 0.1)]
                if len(p):
                    z = ground(p)
                    for q, gz in zip(p, z):
                        meshes["boundary"].prism(q - [0.06, 0], q + [0.06, 0], 0.12,
                                               gz - 0.1, gz - 0.1, r.height + 0.1, METAL)
    out = {}
    for kind, mesh in meshes.items():
        for n, part in enumerate(mesh.finish(), 1):
            out[f"{kind}{n if n > 1 else ''}"] = part
    if conflicts:
        log.debug("boundary spans omitted inside ground carriageways: %s", conflicts)
    return out


def tile(frame, tx, ty, md, ground, road_lines, props, exclude_fids=()):
    """One optional boundary build shared by both tile producers; failures preserve existing scenery."""
    runs = load(frame, tx, ty, md)
    if exclude_fids:
        runs = [r for r in runs if r.fid not in exclude_fids]
    try:
        meshes = tile_meshes(runs, roads.tile_bbox(tx, ty), ground, road_lines)
    except (OSError, ValueError) as exc:
        log.warning("boundary tile %s_%s omitted: %s", tx, ty, exc)
        return {}, None
    suppress_fences(props, runs)
    return meshes, stock_clearance(runs)
