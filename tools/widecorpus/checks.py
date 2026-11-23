"""Measurements of the emitted CDAE road scene and binary terrain block, in anchor metres."""
from collections import Counter, defaultdict
from pathlib import Path
import subprocess
import sys

from common import HERE, TREE, read

sys.path[:0] = [str(TREE / "server"), str(HERE.parent / "probes")]
import numpy as np
import cdae
import geodesy
import geotiles
import markprobe
import roadmesh
import surface


def cross(a, b):
    return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]


def inside(xy, quad):
    turns = np.stack([cross(b-a, xy-a) for a, b in zip(quad, np.roll(quad, -1, axis=0))])
    return (turns >= 0).all(0) | (turns <= 0).all(0)


class Terrain:
    def __init__(self, path, info):
        raw = path.read_bytes()
        n = int.from_bytes(raw[1:5], "little")
        self.z = np.frombuffer(raw, "<u2", n*n, 5).reshape(n, n) * info["q"] + info["zBase"]
        ids = np.frombuffer(raw, "u1", n*n, 5+2*n*n).reshape(n, n)
        lut = np.full(256, 255)
        mats = info.get("mats") or [surface.DIRT]
        lut[:len(mats)] = mats
        self.mat = lut[ids]
        self.origin = np.array([info["cx"], info["cy"]]) - n//2

    def odd_diagonal(self, x, y):
        # The game places its physics blocks on an even world lattice. This smaller audit block
        # may start on an odd coordinate; its vertices still represent the very same lattice.
        return (x + y + int(self.origin.sum())) % 2

    def sample(self, xy):
        """Torque terrain alternates diagonals with lattice parity. Material 255 is a physical hole."""
        local = xy - self.origin
        ij = np.floor(local).astype(int)
        valid = ((ij >= 0) & (ij < len(self.z)-1)).all(1)
        x, y = np.clip(ij, 0, len(self.z)-2).T
        u, v = (local-ij).T
        a, b = self.z[y,x], self.z[y,x+1]
        c, d = self.z[y+1,x], self.z[y+1,x+1]
        even = np.where(v>u, a+v*(c-a)+u*(d-c), a+u*(b-a)+v*(d-b))
        odd = np.where(u+v<=1, a+u*(b-a)+v*(c-a), d+(1-u)*(c-d)+(1-v)*(b-d))
        z = np.where(self.odd_diagonal(x,y), odd, even)
        mat = np.where(valid, self.mat[y,x], 255)
        return np.where(mat!=255, z, np.nan), mat


def groups(directory, entry, frame, source_frame, override):
    shape = cdae.read(str(directory / Path(override or entry["shape"]).name))
    for m in shape["meshes"]:
        if m["type"] != 0:
            continue
        arrays = cdae.mesh_arrays(m)
        xyz = np.asarray(arrays["verts"], dtype=float) + entry["pos"]
        if source_frame is not None:
            rot, off, _ = geotiles.placement(source_frame, frame, (0, 0, entry["pivot"]))
            xyz = xyz @ rot.T + off
        if not np.isfinite(xyz).all():
            raise ValueError("nonfinite mesh vertex")
        idx = np.asarray(arrays["indices"])
        for start, count, flags in np.asarray(arrays["primitives"]).reshape(-1, 3):
            mat = shape["materials"][flags & cdae.PRIM_MATERIAL_MASK]["name"]
            yield mat, xyz[idx[start:start+count].reshape(-1, 3)[:, ::-1]]


def boundary_rings(xy, triangles):
    """Trace the exterior edges of each v3 triangulated patch, excluding its interior diagonals."""
    edges = Counter()
    for a,b,c in np.asarray(triangles).reshape(-1,3):
        for u,v in ((a,b),(b,c),(c,a)):
            if u != v:
                edges[tuple(sorted((int(u),int(v))))] += 1
    adjacency = defaultdict(set)
    for (a,b), count in edges.items():
        if count==1:
            adjacency[a].add(b)
            adjacency[b].add(a)
    rings=[]
    while adjacency:
        start=min(adjacency)
        ring=[start]
        at=start
        while at in adjacency:
            nxt=min(adjacency[at])
            adjacency[at].remove(nxt)
            adjacency[nxt].remove(at)
            for vertex in (at,nxt):
                if not adjacency[vertex]:
                    del adjacency[vertex]
            if nxt==start:
                if len(ring)>=3:
                    rings.append(np.asarray(xy)[ring,:2].tolist())
                break
            ring.append(nxt)
            at=nxt
    return rings


def outline_metrics(rings, quad):
    metrics = Counter()
    for ring in rings:
        xy = np.asarray(ring)
        if len(xy) > 1 and np.linalg.norm(xy[0]-xy[-1]) < 1e-6:
            xy = xy[:-1]
        if len(xy) < 3:
            continue
        edges = np.roll(xy, -1, axis=0)-xy
        length = np.linalg.norm(edges, axis=1)
        local = inside(xy, quad)
        metrics["outline_vertices"] += int(local.sum())
        metrics["tiny_edges"] += int(((length < .02) & local).sum())
        unit = edges / np.maximum(length[:, None], 1e-12)
        sharp = (np.roll(unit, 1, axis=0)*unit).sum(1) < -np.cos(np.deg2rad(15))
        metrics["acute_corners"] += int((sharp & local).sum())
        for i, (a, u) in enumerate(zip(xy, edges)):
            j = np.arange(i+2, len(xy)-(i==0))
            v, d = edges[j], xy[j]-a
            det = cross(u, v)
            valid = np.abs(det) > 1e-10
            if not valid.any():
                continue
            t, s = cross(d[valid], v[valid])/det[valid], cross(d[valid], u)/det[valid]
            hits = (t>0)&(t<1)&(s>0)&(s<1)
            metrics["outline_crossings"] += int(inside(a+t[hits,None]*u, quad).sum())
    return metrics


def fragments(tri):
    """Connected components (shared XYZ vertices at 1 mm), using the whole marking per material."""
    parent = list(range(len(tri)))
    first = {}
    def root(i):
        while i != parent[i]:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, points in enumerate(np.rint(tri*1000).astype(np.int64)):
        for point in points:
            other = first.setdefault(tuple(point), i)
            parent[root(i)] = root(other)
    areas = defaultdict(float)
    for i, area in enumerate(np.abs(cross(tri[:,1]-tri[:,0], tri[:,2]-tri[:,0]))*.5):
        areas[root(i)] += area
    return sum(0 < area < .015 for area in areas.values())


def ground_faces(faces, terrain):
    # Only terrain cells near a collider can intersect its triangles.
    n = len(terrain.z)
    mask = np.zeros((n-1,n-1), bool)
    lo = np.floor(faces[:,:,:2].min(1)-terrain.origin).astype(int).clip(0,n-2)
    hi = np.floor(faces[:,:,:2].max(1)-terrain.origin).astype(int).clip(0,n-2)
    for (x,y),(u,v) in zip(lo,hi):
        mask[y:v+1,x:u+1] = True
    y,x = np.where(mask & (terrain.mat[:-1,:-1]!=255))
    ij = np.c_[x,y][:,None]+[[0,0],[1,0],[1,1],[0,1]]
    xyz = np.dstack([ij+terrain.origin,terrain.z[ij[:,:,1],ij[:,:,0]]])
    indices = np.where(terrain.odd_diagonal(x,y)[:,None],[[0,1,3,1,2,3]],[[0,1,2,0,2,3]])
    return xyz[np.arange(len(x))[:,None],indices].reshape(-1,9)


def collider_scan(tris, lanes, terrain, work, scanner):
    counts = Counter()
    if not tris:
        return counts
    faces = np.concatenate(tris)
    with (work/"scan.bin").open("wb") as out:
        np.column_stack((np.ones(len(faces)), np.arange(len(faces)), faces.reshape(-1,9))).tofile(out)
        ground = ground_faces(faces, terrain)
        np.column_stack((np.zeros(len(ground)), -np.ones(len(ground)), ground)).tofile(out)
    np.ascontiguousarray(lanes, dtype="float64").tofile(work/"lanes.bin")
    output = subprocess.check_output([scanner, work/"scan.bin", work/"lanes.bin"], text=True)
    for line in output.splitlines():
        kind, _, _, n, total, *_ = line.split()
        if kind in ("overlap", "laneOverlap"):
            counts["collider_overlap_m2" if kind=="overlap" else "in_lane_overlap_m2"] += float(total)
        else:
            counts[kind] += int(n)
    return counts


def normal(tris):
    return np.cross(tris[:,1]-tris[:,0], tris[:,2]-tris[:,0])


def curb_metrics(tri, terrain, lanes):
    norm = normal(tri)
    steep = np.abs(norm[:,2]) < .45*np.linalg.norm(norm,axis=1)
    walls = tri[steep]
    if not len(walls):
        return {}
    c = walls.mean(1)
    above = walls[:,:,2]-terrain.sample(walls[:,:,:2].reshape(-1,2))[0].reshape(-1,3)
    margin = np.full(len(c), -np.inf)
    for ax, ay, bx, by, width in lanes:
        u = np.array([bx-ax, by-ay])
        s = (c[:,:2]-[ax,ay]) @ u / max(u@u, 1e-12)
        dist = np.linalg.norm(c[:,:2]-[ax,ay]-s[:,None]*u, axis=1)
        margin = np.where((s>=0)&(s<=1), np.maximum(margin, width/2-dist), margin)
    lips = (margin>=.75)&(above.min(1)<.05)&(above.max(1)>.02)&(np.ptp(walls[:,:,2],axis=1)>.02)
    # Subtract the local ground slope and the buried skirt from the exposed kerb face.
    exposed = np.maximum(above,0.)
    heights = np.ptp(exposed,axis=1)
    heights = heights[np.isfinite(heights)]
    return dict(kerb_faces=len(walls), kerb_height_max_m=float(heights.max(initial=0)),
                kerb_high_faces=int((heights>.22).sum()), kerb_in_lane_lips=int(lips.sum()),
                kerb_overhang_faces=int((norm[steep,2]<-.05*np.linalg.norm(norm[steep],axis=1)).sum()))


def road_samples(lines, quad):
    batches = []
    for road in lines:
        xy = np.asarray(road["xy"])
        dist = np.r_[0, np.cumsum(np.linalg.norm(np.diff(xy,axis=0),axis=1))]
        count = min(16, max(1,int(road["lanes"])))
        # Bound long ways before sampling. Keep station distances relative to the original feature.
        lo, hi = quad.min(0)-20, quad.max(0)+20
        near = ((np.maximum(xy[:-1],xy[1:])>=lo)&(np.minimum(xy[:-1],xy[1:])<=hi)).all(1)
        for j in np.flatnonzero(near & (np.diff(dist)>.001)):
            tangent = (xy[j+1]-xy[j])/(dist[j+1]-dist[j])
            perp = np.array([-tangent[1], tangent[0]])
            # Project the tile onto this segment to avoid allocating distant stations.
            projections = (quad-xy[j]) @ tangent
            begin = max(dist[j], dist[j]+projections.min()-20)
            end = min(dist[j+1], dist[j]+projections.max()+20)
            s = np.arange(np.ceil(begin/2)*2, end, 2.)
            for lane in range(count):
                offset = ((lane+.5)/count-.5)*road["width"]
                pts = xy[j]+(s-dist[j])[:,None]*tangent+offset*perp
                ok = inside(pts, quad)
                if ok.any():
                    batches.append((road, lane, s[ok], pts[ok], perp))
    return batches


def ray_height(tri, pts, ref=None):
    z = np.full(len(pts), np.nan)
    if len(tri):
        ids, _, values = markprobe.lookup(tri, pts, cell=8)
        if ref is None:
            np.fmax.at(z,ids,values)
        else:
            # Select the lowest underside above this roadway, including low-clearance crossings.
            keep = values > ref[ids]+.05
            np.fmin.at(z,ids[keep],values[keep])
    return z


def lane_metrics(source, quad, terrain, tops, soffits, floors):
    counts = Counter()
    batches = road_samples(source, quad)
    if not batches:
        return counts
    pts = np.concatenate([b[3] for b in batches])
    side = np.concatenate([np.broadcast_to(b[4],b[3].shape) for b in batches])
    levels = np.concatenate([np.full(len(b[2]),b[0]["level"]) for b in batches])
    ground, mats = terrain.sample(pts)
    def hits(points):
        z = terrain.sample(points)[0]
        for level, tri in (("bridge",tops), ("tunnel",floors)):
            ids = levels==level
            if ids.any():
                z[ids] = ray_height(tri, points[ids])
        return z
    height = hits(pts)
    bank = (hits(pts+side*.7)-hits(pts-side*.7))/1.4
    ceiling = ray_height(soffits, pts, height)
    under = (levels=="ground") & np.isfinite(ceiling) & np.isfinite(height)
    counts["clearance_samples"] = int(under.sum())
    counts["clearance_under_4m"] = int(((ceiling-height<4)&under).sum())
    counts["lane_samples"] = len(pts)
    counts["drive_hole_samples"] = int((~np.isfinite(height)).sum())
    paved = np.concatenate([np.full(len(b[2]),b[0]["paved"]) for b in batches]) & (levels=="ground")
    counts["pavement_samples"] = int(paved.sum())
    counts["missing_pavement_samples"] = int((paved & ~np.isin(mats,[surface.ASPHALT,surface.COBBLE,surface.PAVERS,surface.BRICK])).sum())
    tracks = defaultdict(list)
    start = 0
    for road, lane, station, points, perp in batches:
        idx = np.arange(start, start+len(station))
        tracks[(road["id"],road["level"],lane,tuple(road["xy"][0]))].extend(zip(station, height[idx], bank[idx]))
        start += len(station)
    for track in tracks.values():
        s,z,b = np.asarray(sorted(track)).T
        adjacent = np.diff(s)==2
        counts["grade_over_15pct"] += int(((np.abs(np.diff(z))>.30)&adjacent).sum())
        counts["bank_jump_5pct"] += int(((np.abs(np.diff(b))>.05)&adjacent).sum())
        curv = np.diff(z,2)[adjacent[1:]&adjacent[:-1]]/4
        for mph in (30,50,70):
            g = abs(curv)*(mph*.44704)**2/9.80665
            counts[f"ride_{mph}mph_over_0.5g"] += int((g>.5).sum())
            counts[f"ride_{mph}mph_max_g"] = max(counts[f"ride_{mph}mph_max_g"], np.max(g[np.isfinite(g)],initial=0))
    for road in source:
        if road["level"]!="bridge":
            continue
        for xy in (road["xy"], road["xy"][::-1]):
            a,b = np.array(xy[:2])
            if not inside(a[None,:],quad)[0] or np.linalg.norm(b-a)<.01:
                continue
            u=(b-a)/np.linalg.norm(b-a)
            pts=np.array([a+u,a-u])
            deck=ray_height(tops,pts)
            if np.isfinite(deck[0]) and not np.isfinite(deck[1]) and inside(pts,quad).all():
                ground=terrain.sample(pts)[0][1]
                if np.isfinite(ground):
                    counts["deck_ends_sampled"]+=1
                    counts["deck_end_steps_5cm"]+=int(abs(deck[0]-ground)>.05)
    return counts


# The M0 audit (native/roadsys/src/audit.rs: double owners, envelope intrusions, blocked openings, piers in faces,
# parapets in the carriageway, hand-over lips, clearance against the floors, T-cracks, folds): its kinds and flags.
AUDIT_KINDS = {"road": 0, "walk": 1, "walkcol": 2, "deck": 3, "tunnel": 4, "barrier": 5, "buscol": 6}
AUDIT_LEVELS = {"ground": 0, "bridge": 1, "tunnel": 2}


def audit_kind(what):
    if what in AUDIT_KINDS:
        return AUDIT_KINDS[what]
    return 8 if what.startswith("railcol") else 7 if what.startswith("rail") else 9


def audit_input(path, quad, terrain, entries, lines):
    """The audit's input blob (audit.rs): the tile quad, the physics terrain, every emitted mesh as
    (kind, collides, [(flags, triangles)]) and the source map's lines."""
    import struct
    q = np.asarray(quad, dtype=float).reshape(4, 2)
    n = len(terrain.z)
    with open(path, "wb") as f:
        f.write(b"RSAUDIT1")
        f.write(q.astype("<f8").tobytes())
        f.write(struct.pack("<Iqq", n, int(terrain.origin[0]), int(terrain.origin[1])))
        f.write(np.ascontiguousarray(terrain.z, dtype="<f4").tobytes())
        f.write(np.ascontiguousarray(terrain.mat == 255, dtype="u1").tobytes())
        f.write(struct.pack("<I", len(entries)))
        for kind, collides, groups_ in entries:
            total = sum(len(t) for _flags, t in groups_)
            f.write(struct.pack("<BBI", kind, 1 if collides else 0, total))
            for flags, tri in groups_:
                rows = np.zeros(len(tri), dtype=[("f", "u1"), ("p", "<f8", (9,))])
                rows["f"] = flags
                rows["p"] = np.asarray(tri, dtype=float).reshape(-1, 9)
                f.write(rows.tobytes())
        f.write(struct.pack("<I", len(lines)))
        for road in lines:
            xy = np.asarray(road["xy"], dtype="<f8").reshape(-1, 2)
            f.write(struct.pack("<BdI", AUDIT_LEVELS.get(road["level"], 0), float(road["width"]), len(xy)))
            f.write(xy.tobytes())


def audit(directory, quad, terrain, entries, lines, auditor):
    if auditor is None:
        return {}
    src, out = directory / "audit.bin", directory / "audit.json"
    audit_input(src, quad, terrain, entries, lines)
    subprocess.run([str(auditor), "audit", str(src), str(out)], check=True)
    return read(out)


def measure(p, directory, scanner, auditor=None):
    frame = geodesy.EnuFrame(p["lat"],p["lon"])
    tiles = {}
    for tile in read(directory/"tiles.json"):
        key = tile["key"]
        ctx = read(directory/(key+".source.json"))
        terrain = Terrain(directory/(key+".ter"), ctx["block"])
        quad = np.asarray(ctx["quad"])
        lanes = np.load(directory/(key+".lanes.npy"))
        metrics = outline_metrics(ctx["outlines"],quad)
        colliders, tops, soffits, floors = [], [], [], []
        markings = defaultdict(list)
        bases = []
        src = geotiles.frame(*geotiles.parse(key)[1:])
        entries = []
        for file, src_frame in ((f"rg_{key}.json", src), (f"rs_1_{key}.json",None)):
            data = read(directory/file)
            for what, entry in data.items():
                if not isinstance(entry,dict) or "shape" not in entry:
                    continue
                if what not in ("road","walk","walkcol","deck","tunnel","barrier","buscol") and not what.startswith("rail"):
                    continue
                collision = what!="road" and not (what=="walk" and "walkcol" in data) and not (
                    what.startswith("rail") and not what.startswith("railcol") and "railcol" in data)
                audited = []
                entries.append((audit_kind(what), collision, audited))
                for mat, tri in groups(directory,entry,frame,src_frame,tile["exact"].get(what)):
                    audited.append(((1 if mat==roadmesh.DECK_TOP else 0) | (2 if mat==roadmesh.DECK_SIDE else 0) |
                                    (4 if mat=="curbs" else 0) |
                                    (8 if ctx["layers"].get(mat)=="mark" or mat.startswith(("line_","crossing")) else 0),
                                    tri))
                    if not what.endswith("col") and not what.startswith("railcol"):
                        metrics["triangles"] += len(tri)
                        metrics["draw_calls"] += 1
                    if collision:
                        colliders.append(tri)
                    n = normal(tri)
                    upward = n[:,2]>.45*np.linalg.norm(n,axis=1)
                    if what=="deck":
                        if mat==roadmesh.DECK_TOP:
                            tops.extend(tri[upward])
                        soffits.extend(tri[n[:,2]<-.7*np.linalg.norm(n,axis=1)])
                    if what=="deck" and mat==roadmesh.DECK_SIDE:
                        floors.extend(tri[upward])
                    if what=="walkcol" or (what=="walk" and collision and mat=="curbs"):
                        for k,v in curb_metrics(tri,terrain,lanes).items():
                            if k.endswith("_max_m"):
                                metrics[k]=max(metrics[k],v)
                            else:
                                metrics[k]+=v
                    if what=="road":
                        if ctx["layers"].get(mat)=="base" and len(tri[upward]):
                            bases.append(tri[upward])
                        centers = tri[upward].mean(1)
                        # Overlays above a physical deck are intentionally elevated. Exclude those separately below.
                        if ctx["layers"].get(mat)=="mark" or mat.startswith(("line_","crossing")):
                            markings[mat].append(tri[upward])
                        if len(centers):
                            deck = ray_height(tops,centers[:,:2])
                            z,_ = terrain.sample(centers[:,:2])
                            ground = ~np.isfinite(deck) | (abs(deck-centers[:,2])>.5)
                            dz = centers[:,2]-z
                            metrics["surface_gap_over_10cm"] += int(((dz>.10)&ground).sum())
                            metrics["surface_buried_2cm"] += int(((dz<-.02)&ground).sum())
        metrics["marking_fragments"] = sum(fragments(np.concatenate(parts)) for parts in markings.values())
        metrics.update(collider_scan(colliders,lanes,terrain,directory,scanner))
        if bases:
            base = np.concatenate(bases).reshape(-1,9)
            np.column_stack((np.ones(len(base)),np.arange(len(base)),base)).tofile(directory/"base.bin")
            np.ascontiguousarray(lanes,dtype="float64").tofile(directory/"lanes.bin")
            scanned=subprocess.check_output([scanner,directory/"base.bin",directory/"lanes.bin","area"],text=True)
            metrics["visual_base_overlap_m2"]=sum(float(line.split()[4]) for line in scanned.splitlines()
                                                    if line.startswith("overlap "))
        metrics.update(lane_metrics(ctx["lines"],quad,terrain,tops,soffits,floors))
        metrics.update(audit(directory, quad, terrain, entries, ctx["lines"], auditor))
        metrics["source_faults"] = tile.get("source_faults",0)
        metrics["lidar_waits"] = tile.get("lidar_waits",0)
        tiles[key] = dict(metrics={k:round(float(v),6) for k,v in metrics.items()},
                          seconds=tile["seconds"])
    if not tiles or not any(t["metrics"].get("lane_samples",0) for t in tiles.values()):
        raise ValueError("no source driving lanes inside selected tiles")
    return tiles
