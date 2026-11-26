"""Optional visual parked cars on ordinary, existing off-street stalls inside mapped lots."""
import hashlib
import os

import numpy as np

import dnp
import mapgeom
from polygon import contains_points
from streetfront import tags_of


MODELS = (("sedan", 1.45), ("hatchback", 1.48), ("suv", 1.74), ("pickup", 1.79))
CATEGORIES = {
    "dealer": {"car_dealer", "car_dealership", "used_car_dealer", "new_car_dealer"},
    "retail": {"shopping_center", "shopping_mall", "retail", "department_store", "clothing_store",
               "supermarket", "grocery_store", "hardware_store", "home_improvement_store"},
    "restaurant": {"restaurant", "fast_food", "fast_food_restaurant", "cafe"},
    "office": {"office", "office_building", "corporate_office", "business_center"},
}
PER_LOT = 36
PER_TILE = 64


def _sites(frame, cells):
    places = {}
    for feats in cells:
        if feats is None or not len(feats):
            continue
        labels = np.full(len(feats), "", dtype=object)
        for profile, categories in CATEGORIES.items():
            for key in ("basic_category", "taxonomy"):
                if key in feats.attrs:
                    labels[np.isin(feats.attrs[key], list(categories))] = profile
        mask = (labels != "") & (feats.kind == mapgeom.POINT)
        if "confidence" not in feats.attrs:
            continue
        mask &= feats.attrs["confidence"] >= .8
        if "operating_status" in feats.attrs:
            mask &= ~np.isin(feats.attrs["operating_status"], ["closed", "permanently_closed"])
        for i in np.flatnonzero(mask):
            part = next(iter(feats.parts(i)), None)
            if not part or not len(part[0]):
                continue
            lon, lat = part[0][0]
            x, y, _ = frame.geodetic_to_enu(lat, lon, 0.)
            name = str(feats.attrs["name"][i] or "").strip().casefold() if "name" in feats.attrs else ""
            places[str(feats.ids[i])] = (np.array([x, y]), name, labels[i])
    return list(places.values())


def _lots(frame, layers, box):
    lots, reserved, seen = [], [], set()
    for feats in layers:
        if feats is None or "class" not in feats.attrs:
            continue
        classes = feats.attrs["class"]
        boxes = feats.boxes(frame)
        in_tile = (boxes[:,0] < box[2]) & (boxes[:,2] > box[0]) & (boxes[:,1] < box[3]) & (boxes[:,3] > box[1])
        for i in np.flatnonzero(in_tile & np.isin(classes, ["parking", "parking_space"])
                                & (feats.kind == mapgeom.POLYGON)):
            fid = str(feats.ids[i])
            if fid in seen:
                continue
            seen.add(fid)
            tags = tags_of(feats, i)
            if classes[i] == "parking" and (tags.get("parking", "surface") != "surface"
                                            or tags.get("access") in {"no", "private"}):
                continue
            if classes[i] == "parking_space" and tags.get("parking_space", "") in {"", "yes", "normal"} and not any(
                    tags.get(k) in {"yes", "designated", "only"} for k in ("disabled", "charging", "wheelchair")):
                continue
            name = str(tags.get("operator") or tags.get("name") or
                       (feats.attrs["name"][i] if "name" in feats.attrs else "") or "").strip().casefold()
            for part in feats.parts(i):
                if len(part) != 1 or len(part[0]) < 4:
                    continue
                ring = np.column_stack(mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64)))
                if classes[i] == "parking_space":
                    reserved.append(ring)
                else:
                    lots.append((fid, ring, name))
    return lots, reserved


def _profile(ring, name, sites):
    lower, upper = ring.min(0), ring.max(0)
    near = [(p,n,t) for p,n,t in sites if np.all(p >= lower-30) and np.all(p <= upper+30)]
    named = {t for _,n,t in near if name and n == name}
    if len(named) == 1:
        return next(iter(named))
    inside = {t for p,_,t in near if contains_points(ring, np.array([p]))[0]}
    # A separately named lot may serve a different business across the street.
    return next(iter(inside)) if not name and len(inside) == 1 else "general"


def _clearance(xy, lines):
    """Clearance for the 5.2 m by 2 m maximum vehicle, even when perpendicular to an aisle."""
    import fast
    segments = [(line[:-1], line[1:], np.full(len(line)-1, half+2.8)) for line,half in lines if len(line)>1]
    if not segments:
        return np.zeros(len(xy), bool)
    a,b,r = [np.ascontiguousarray(np.concatenate(column), np.float64) for column in zip(*segments)]
    return np.asarray(fast._ef.points_near_segments(np.ascontiguousarray(xy), a, b, r), bool)


def _overlaps(row, xy, headings):
    """Separating axes for a display car and the 5.5 x 2.4 m envelope of another parked car."""
    angle = np.radians(row[3])
    u = np.array([dnp.cos(angle), dnp.sin(angle)])
    v = np.array([-u[1], u[0]])
    du = np.column_stack((dnp.cos(headings), dnp.sin(headings)))
    dv = np.column_stack((-du[:,1], du[:,0]))
    delta = xy - row[:2]
    along = np.abs(dnp.matmul(du, u))
    across = np.abs(dnp.matmul(du, v))
    return ((np.abs(dnp.matmul(delta, u)) < 2.75+2.75*along+1.2*across)
            & (np.abs(dnp.matmul(delta, v)) < 1.2+2.75*across+1.2*along)
            & (np.abs(np.sum(delta*du,axis=1)) < 2.75+2.75*along+1.2*across)
            & (np.abs(np.sum(delta*dv,axis=1)) < 1.2+2.75*across+1.2*along))


def allocate(park, lots, box, sites=(), lines=(), reserved=(), blockers=()):
    if not park:
        return park
    xy = np.asarray([r[:2] for r in park], dtype=np.float64)
    headings = np.radians([r[3] for r in park])
    x0,y0,x1,y1 = box
    # Every vehicle envelope involved in a reservation must be owned by this tile.
    allowed = np.array([len(r)>4 and r[4]==1 and x0+6<r[0]<x1-6 and y0+6<r[1]<y1-6 for r in park])
    if not allowed.any():
        return park
    allowed &= ~_clearance(xy, lines)
    for ring in reserved:
        lower, upper = ring.min(0)-2.8, ring.max(0)+2.8
        allowed &= ~np.all((xy >= lower) & (xy <= upper), axis=1)
    if blockers:
        import parking
        u = np.column_stack((dnp.cos(headings), dnp.sin(headings)))
        v = np.column_stack((-u[:,1], u[:,0]))
        paths = [parking.Path(r) for r in blockers if len(r) >= 3]
        for along,across in ((0,0), (-2.65,-1.1), (-2.65,1.1), (2.65,-1.1), (2.65,1.1)):
            allowed &= ~parking._hits(paths, xy + along*u + across*v)
        lines = [(np.vstack([r,r[0]]), 0.) for r in blockers if len(r) >= 3]
        allowed &= ~_clearance(xy, lines)
    if hasattr(box, "owns"):
        import geotiles
        for dx,dy in ((-6,-6), (-6,6), (6,-6), (6,6)):
            allowed &= geotiles.box_owns(box, xy[:,0]+dx, xy[:,1]+dy)
    blocked = np.zeros(len(park), bool)
    protected = np.array([len(r)>4 and r[4]==2 for r in park])
    display = []
    for fid,ring,name in sorted(lots, key=lambda lot: lot[0]):
        idx = np.flatnonzero(allowed & ~blocked & contains_points(ring, xy))
        if not len(idx):
            continue
        profile = _profile(ring, name, sites)
        first_edge = np.roll(ring,-1,axis=0)-ring
        edge = first_edge[np.argmax(dnp.norm(first_edge, axis=1))]
        norm = dnp.norm(edge)
        if norm < .1:
            continue
        u = edge/norm
        v = np.array([-u[1],u[0]])
        options = []
        for j in idx:
            p = xy[j]-ring[0]
            salt = f"{fid}/{round(float(dnp.dot(p, u)))}/{round(float(dnp.dot(p, v)))}".encode()
            digest = hashlib.blake2s(salt, digest_size=6).digest()
            options.append((digest, j))
        count = 0
        # Keep a share of spaces for live parking even with a densely stocked dealership.
        share = .85 if profile == "dealer" else .75
        quota = min(PER_LOT, max(0, len(idx) - 2), int(share * len(idx)))
        for digest, j in sorted(options):
            if count >= quota or len(display) >= PER_TILE:
                break
            if blocked[j]:
                continue
            overlaps = _overlaps(park[j], xy, headings)
            if protected[overlaps].any():
                continue
            if np.count_nonzero(~blocked[idx] & ~overlaps[idx]) < 2:
                continue
            model,height = MODELS[digest[1] % len(MODELS)]
            rank = int.from_bytes(digest[2:4], "big") / 65536
            display.append([*park[j][:4], 3, model, height, profile, round(rank,5)])
            blocked |= overlaps
            count += 1
    if not display:
        return park
    return [r for r, skip in zip(park, blocked) if not skip] + display


def apply(frame, tx, ty, md, park, items, drives):
    if os.environ.get("BNE_PARKING_DISPLAYS") != "1" or not any(len(r)>4 and r[4]==1 for r in park or ()):
        return park
    import buildings
    import roads
    box = roads.tile_bbox(tx,ty)
    keys = list(roads.cells_of_tile(frame,tx,ty,margin=30))
    cells = [md.cell(k, layers=("infrastructure", "roads"), kind="demand") for k in keys]
    lots,reserved = _lots(frame,[c.layers.get("infrastructure") for c in cells],box)
    if not lots:
        return park
    sites = _sites(frame,(md.cell(k,layers=("places",),kind="demand").layers.get("places") for k in keys))
    roads_xy = buildings.road_lines(frame,[c.layers.get("roads") for c in cells],box[0],box[1],
                                    size=max(box[2]-box[0],box[3]-box[1]))
    origin = np.array(box[:2])
    lines = [(line+origin,half) for line,half in zip(roads_xy,roads_xy.half)]
    lines.extend((np.asarray(d["n"])[:,:2],float(d.get("w",3))/2) for d in drives if d.get("n"))
    blockers = [np.asarray(spec.ring) + origin for spec,*_ in items if not spec.min_h]
    return allocate(park,lots,box,sites,lines,reserved,blockers)
