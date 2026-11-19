"""Opt-in ground-level OSM lighting. Distances in metres in the generation frame."""
from collections import defaultdict
import math
import os

import numpy as np

import citylamps
import fast
import furniture
import mapcells
import mapgeom
import standalonepaths
import streetfront

# Spacing, mounting height, and intensity relative to the stock road light.
PROFILES = {"pedestrian": (20., 4., .15), "footway": (20., 4., .15),
            "cycleway": (25., 5., .20), "path": (20., 3.5, .10),
            "garden": (18., 4., .15), "park": (20., 3.5, .10),
            "square": (25., 5.5, .20), "parking": (32., 8., .30)}


def enabled():
    return os.environ.get("BNE_PEDESTRIAN_LIGHTING", "").lower() in {"on", "true", "1"}


class Neighbors:
    """Fixed-radius queries over occupied XY buckets."""
    size = 16.

    def __init__(self):
        self.cells = defaultdict(list)

    def add(self, x, y):
        self.cells[math.floor(x / self.size), math.floor(y / self.size)].append((x, y))

    def near(self, x, y, radius):
        for i in range(math.floor((x-radius)/self.size), math.floor((x+radius)/self.size)+1):
            for j in range(math.floor((y-radius)/self.size), math.floor((y+radius)/self.size)+1):
                if any((a-x)**2 + (b-y)**2 < radius**2 for a,b in self.cells.get((i,j), ())):
                    return True
        return False


def stations(xy, spacing, phase, box):
    """Walk full-feature chainage but allocate stations only on segments near the tile."""
    chain = furniture._arclen(xy)
    lo = np.asarray(box[:2]) - 36.
    hi = np.asarray(box[2:]) + 36.
    near = np.all((np.maximum(xy[:-1], xy[1:]) >= lo) & (np.minimum(xy[:-1], xy[1:]) <= hi), axis=1)
    for j in np.flatnonzero(near & (np.diff(chain) > 1e-6)):
        delta = xy[j+1] - xy[j]
        length = chain[j+1] - chain[j]
        direction = delta / length
        start, end = 0., length
        for axis in range(2):
            if abs(direction[axis]) > 1e-12:
                low, high = sorted(((lo[axis]-xy[j,axis])/direction[axis], (hi[axis]-xy[j,axis])/direction[axis]))
                start, end = max(start, low), min(end, high)
        first = math.ceil((chain[j] + start - phase) / spacing)
        stop = math.ceil((chain[j] + end - phase) / spacing)
        for k in range(first, stop):
            yield k, j, xy[j] + (k*spacing + phase-chain[j])*direction, direction


def path_sites(parts, cells, box):
    profiles = {}
    for f in cells:
        if f is None or "osm_way_lit" not in f.attrs:
            continue
        for i in np.flatnonzero(f.attrs["osm_way_lit"] == "yes"):
            profiles[str(f.ids[i])] = str(f.attrs["class"][i])
    for fid, xy, width, _ in parts:
        kind = profiles.get(fid)
        if kind not in PROFILES:
            continue
        spacing = PROFILES[kind][0]
        phase = (.25 + .5 * furniture._h01(fid, "lighting")) * spacing
        for k, _, (x,y), (dx,dy) in stations(xy, spacing, phase, box):
            offset = width / 2 + .75
            yield (0, fid, k), x - dy * offset, y + dx * offset, furniture._yaw(dy, -dx), kind


def area_kind(tags):
    if tags.get("access") in {"private", "no"} or tags.get("location") == "underground":
        return None
    if tags.get("amenity") == "parking" and tags.get("parking", "surface") == "surface":
        return "parking"
    if tags.get("leisure") == "recreation_ground" or tags.get("landuse") == "recreation_ground":
        return "park"
    if tags.get("place") == "square" or tags.get("highway") == "pedestrian":
        return "square"
    return tags.get("leisure") if tags.get("leisure") in {"garden", "park"} else None


def area_sites(frame, box, md, keys, exclusions=None):
    layers = ("land_use", "infrastructure", "water") if exclusions is not None else ("land_use", "infrastructure")
    keys = list(keys)
    for key, cd in zip(keys, mapcells.load_cells(md, keys, layers)):
        bounds = key.bounds()
        for layer in layers:
            f = cd.layers.get(layer)
            if f is None:
                continue
            bb = f.boxes(frame)
            near = ((bb[:, 2] >= box[0]-20) & (bb[:, 0] <= box[2]+20) &
                    (bb[:, 3] >= box[1]-20) & (bb[:, 1] <= box[3]+20))
            for i in np.flatnonzero(near & (f.kind == mapgeom.POLYGON)):
                tags = streetfront.tags_of(f, i)
                kind = area_kind(tags)
                blocked = exclusions is not None and (layer == "water" or (kind and tags.get("lit") == "no"))
                if not blocked and (not kind or tags.get("lit") != "yes"):
                    continue
                if str(tags.get("layer", "0")) != "0" or tags.get("bridge") == "yes" or tags.get("tunnel") == "yes":
                    continue
                for part in f.parts(i):
                    if not part or len(part[0]) < 3:
                        continue
                    rings = [np.ascontiguousarray(np.column_stack(mapgeom.enu_xy(f, frame, r))) for r in part]
                    if blocked:
                        exclusions.append(rings)
                        continue
                    ring = rings[0]
                    sense = 1 if np.sum(ring[:,0]*np.roll(ring[:,1],-1)-ring[:,1]*np.roll(ring[:,0],-1)) > 0 else -1
                    spacing = PROFILES[kind][0]
                    if not np.array_equal(ring[0], ring[-1]):
                        ring = np.vstack((ring, ring[0]))
                    for k,j,point,direction in stations(ring, spacing, spacing/2, box):
                        # A clipped cell boundary is not a surveyed plaza edge.
                        ends = part[0][[j, (j+1) % len(part[0])]]
                        if f.clipped is not None and f.clipped[i] and any(
                            np.all(np.abs(ends[:,axis%2]-edge)<1e-8) for axis,edge in enumerate(bounds)):
                            continue
                        dx,dy = direction
                        inward = sense * np.array([-dy,dx])
                        point = point + .8*inward
                        q = point.reshape(1,2)
                        if fast._ef.points_inside(ring,q)[0] and not any(fast._ef.points_inside(hole,q)[0] for hole in rings[1:]):
                            yield (1,str(f.ids[i]),k), *point, furniture._yaw(*inward), kind


def tile_rows(frame, box, cells, urban, widths, md, keys, elev, props, mapped, rings,
              clearance, road_rows, near_lamps, country_at):
    parts = standalonepaths.parts(frame, box, cells, urban, widths, lighting=True)
    candidates = list(path_sites(parts, cells, box))
    exclusions = []
    candidates.extend(area_sites(frame, box, md, keys, exclusions))
    candidates = [c for c in candidates if box[0]-20 <= c[1] <= box[2]+20 and box[1]-20 <= c[2] <= box[3]+20]
    if not candidates:
        return []
    paths = [(xy, width/2) for _,xy,width,_ in parts]
    # Include the mapped sidewalk on both sides in the footprint exclusion.
    for _, sp, line, _ in road_rows:
        walk = max((w.setback+w.width for _,w in sp.street.walks()), default=0.) if sp.street else sp.walk
        paths.append((line[:,:2], sp.width/2 + walk))
    candidates.sort()
    pts = np.array([c[1:3] for c in candidates])
    keep = standalonepaths.keep_outside(pts, paths, .25)
    for outer, *holes in exclusions:
        idx = np.flatnonzero(keep & np.all((pts >= outer.min(0)) & (pts <= outer.max(0)), axis=1))
        if len(idx):
            blocked = np.asarray(fast._ef.points_inside(outer, pts[idx]), bool)
            for hole in holes:
                blocked &= ~np.asarray(fast._ef.points_inside(hole, pts[idx]), bool)
            keep[idx[blocked]] = False
    lat,lon,_ = frame.enu_to_geodetic(pts[:,0],pts[:,1],np.zeros(len(pts)))
    z = frame.geodetic_to_enu(lat,lon,elev(lat,lon))[2]
    covered, occupied = Neighbors(), Neighbors()
    for x, y in near_lamps or ():
        covered.add(x, y)
    for row in mapped or ():
        covered.add(*row[1:3])
    for row in props:
        occupied.add(*row[1:3])
        if row[0] in furniture.LAMP_HEADS or row[0] in furniture.MAPPED_LAMP_HEADS:
            covered.add(*row[1:3])
    # A higher-priority candidate reserves its neighbourhood before tile-local obstacle
    # rejection. This one-hop rule gives both owners the same decision at a seam.
    reserved = Neighbors()
    rows = []
    for ok, (_,x,y,yaw,kind), height in zip(keep,candidates,z):
        if not ok or not math.isfinite(height):
            continue
        spacing, mount, power = PROFILES[kind]
        competing = reserved.near(x,y,spacing*.63)
        reserved.add(x,y)
        if competing or covered.near(x,y,spacing*.63) or occupied.near(x,y,1.5):
            continue
        row = [furniture.G_LAMP, float(x), float(y), float(height), yaw, 1., mount, 0.]
        row = citylamps.localize([row], country_at)[0]
        if clearance and not clearance.clear(row):
            continue
        if furniture._owns(box,x,y):
            rows.append((row,power))
    posts = furniture.off_buildings([r for r,_ in rows], rings, furniture.LAMP_HEADS, clear=.65)
    power_at = {id(r):power for r,power in rows}
    out = []
    for post in posts:
        lens, glow = furniture.lamp_lenses([post])
        out.extend((post, lens + [power_at[id(post)]], glow))
    return out
