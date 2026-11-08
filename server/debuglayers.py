"""Read-only map debug providers. Never generate assets or fetch external data.

Each provider returns geographic polygons, a categorical legend and an honest
coverage note. This protocol is shared by cliff and future regional providers.
"""
import json
import math
from pathlib import Path
import numpy as np
import clifftypes
import debugcontours

MAX_TILES = 96
MAX_FEATURES = 6000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_QUERY_BYTES = 16 * 1024 * 1024
LAYERS = (
    {"id": "cliff_regions", "label": "Natural cliff regions"},
    {"id": "cliff_generated", "label": "Generated cliffs & road cuts"},
    {"id": "cliff_rocks", "label": "Cliff outcrops & rubble"},
    {"id": "cliff_confirmed_cuts", "label": "Confirmed road cuts"},
    {"id": "cliff_cuts", "label": "Road-cut material regions"},
    {"id": "building_styles", "label": "Building style regions"},
    {"id": "vegetation_trees", "label": "Regional tree variants"},
    {"id": "vegetation_shrubs", "label": "Regional shrub variants"},
    {"id": "vegetation_rocks", "label": "Regional scatter-rock variants"},
)

def legend(context=None):
    # One renderer is reused for arid natural faces and road cuts. Context-specific
    # legends retain provenance; recorded family-only footprints use a shared label.
    rows = {}
    for r in clifftypes.TYPES:
        if context is None or r.context == context:
            rows[r.family] = {"id":r.family,"label":r.label,"color":r.color}
    return list(rows.values())

def footprint_grid(keep, families, xs, ys):
    """Compact rows from the existing coarse detector, not the refined render mesh.

    No work proportional to rendered polygon count. These are detection cells,
    deliberately labelled approximate: toe fitting/contours can adjust the rim.
    """
    runs = []
    for y,x in zip(*np.nonzero(keep)):
        family = int(families[y,x])
        west,south,east,north = xs[x]/111320.,ys[y]/111320.,xs[x+1]/111320.,ys[y+1]/111320.
        if runs and runs[-1][0] == family and runs[-1][2] == south and runs[-1][3] == west:
            runs[-1][3] = east
        else:
            runs.append([family,west,south,east,north])
    return [[r[0], *[round(v,7) for v in r[1:]]] for r in runs]


def _box(msg):
    try:
        s,w,n,e = [float(msg[k]) for k in ("s","w","n","e")]
    except (ValueError, TypeError, KeyError):
        raise ValueError("Invalid debug map bounds") from None
    if not all(math.isfinite(x) for x in (s,w,n,e)) or not -90 <= s < n <= 90 or not 0 < e-w <= 360.001 or abs(w)>540 or abs(e)>540:
        raise ValueError("Invalid debug map bounds")
    return s,w,n,e

def _ring(s,w,n,e):
    return [[w,s],[e,s],[e,n],[w,n]]

def _overlap(lat, lon, bounds, pad=0):
    s,w,n,e = bounds
    lon = (lon-(w+e)/2+180)%360-180+(w+e)/2
    return s-pad <= lat <= n+pad and w-pad <= lon <= e+pad

def _generated(conn, bounds, *, cuts_only=False, rocks=False):
    """Only tiles already delivered to this connection; no scan of the world/cache."""
    if rocks and not getattr(conn, "cliff_rocks", True):
        return [], "Cliff rocks are disabled in graphics settings."
    if not conn.cliff_faces:
        return [], "Cliff faces are disabled. Enable them in graphics settings to generate and display faces."
    import geotiles
    import roads
    candidates = []
    tracker = conn.roads
    for x,y in list(tracker.sent):
        if getattr(tracker, "geo", False):
            lat,lon = geotiles.centre(x,y)
            name = f"rg_{geotiles.key(x,y)}.json"
        else:
            anchor = conn.s.anchors.get(conn.active)
            if anchor is None:
                continue
            frame = anchor.frame
            x0,y0,x1,y1 = roads.tile_bbox(x,y)
            lat,lon,_ = frame.enu_to_geodetic((x0+x1)/2,(y0+y1)/2,0)
            name = f"roads_{conn.active}_{x}_{y}.json"
        if _overlap(lat,lon,bounds, .01):
            centre_lat,centre_lon = (bounds[0]+bounds[2])/2,(bounds[1]+bounds[3])/2
            delta_lon = (lon-centre_lon+180)%360-180
            candidates.append(((lat-centre_lat)**2+delta_lon**2, name))
    candidates.sort()
    features, missing, read_bytes, byte_limited = [], 0, 0, False
    for _,name in candidates[:MAX_TILES]:
        path = Path(conn.s.out_dir)/name
        try:
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                missing += 1
                continue
            if read_bytes + size > MAX_QUERY_BYTES:
                byte_limited = True
                break
            read_bytes += size
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            missing += 1
            continue
        debug = data.get("debugCliffRocks" if rocks else "debugCliffCuts" if cuts_only else "debugCliffs")
        if debug is None:
            missing += 1
            continue
        if rocks:
            for kind,lon,lat,radius in debug:
                if _overlap(lat,lon,bounds,.001):
                    # Small round footprints avoid detector grid lines and distinguish
                    # actual placement records from broad geographic rule previews.
                    dy=max(radius,.5)/111320.; dx=dy/max(.01,math.cos(math.radians(lat)))
                    features.append({"type":kind,"ring":[[lon+dx*math.cos(a),lat+dy*math.sin(a)]
                        for a in np.linspace(0,2*math.pi,12,endpoint=False)]})
                if len(features)>=MAX_FEATURES: break
            if len(features)>=MAX_FEATURES: break
            continue
        for family,west,south,east,north in debug:
            if _overlap((south+north)/2,(west+east)/2,bounds,.001):
                features.append({"type":family,"ring":_ring(south,west,north,east)})
            if len(features) >= MAX_FEATURES:
                break
        if len(features) >= MAX_FEATURES:
            break
    note = "Detected near faces and confirmed road cuts in loaded tiles; approximate detector cells, not exact mesh edges."
    if rocks:
        note = "Recorded embedded arid face outcrops using native large rock models, surrounding small rocks and loose cliff-toe rubble. Approximate rock extents; footprint-scale relief, changing normals, road clearance, land-cover and graphics settings gate placement."
    if cuts_only:
        note = "Confirmed road-cut detector footprints in loaded tiles, including profiled tapered banks and rural service-road cuts; slope, road-facing profile, crest, land cover and pavement clearance apply. Approximate cells, not exact mesh edges."
    if not features:
        note += " No recorded cliffs in this view; explore here with cliff faces enabled."
    if missing:
        note += " Some tiles predate debug metadata; regenerated tiles will appear."
    if byte_limited or len(candidates)>MAX_TILES or len(features)>=MAX_FEATURES:
        note += " Display limited; zoom in for more coverage."
    if rocks: return features,note
    return debugcontours.features([
        [f["type"], f["ring"][0][0], f["ring"][0][1], f["ring"][2][0], f["ring"][2][1]]
        for f in features]),note

def query(conn, msg):
    bounds = _box(msg)
    layer = msg.get("layer")
    result = {"layers":list(LAYERS), "legend":legend(), "features":[], "note":""}
    if layer == "cliff_regions":
        result.update(_sampled(layer,bounds))
        # Exact higher-priority canyon envelopes overlay the bounded climate preview.
        result["features"] += [{"type":rule.family,"ring":ring,"label":name,"outline":True}
            for rule in sorted(clifftypes.TYPES,key=lambda r:(r.priority,r.key)) if rule.context == "natural"
            for name,ring in rule.regions]
        result["legend"] = legend("natural")
        result["note"] = "Sampled arid climate rock; exact layered canyon envelopes take precedence. Appearance priors, not geology. Slope, land cover and settings still gate near and distant faces."
    elif layer in ("cliff_generated", "cliff_confirmed_cuts", "cliff_rocks"):
        result["features"],result["note"] = _generated(conn,bounds, cuts_only=layer == "cliff_confirmed_cuts", rocks=layer == "cliff_rocks")
        if layer == "cliff_rocks":
            result["legend"] = [{"id":0,"label":"Loose toe rubble","color":"#d5bc88"},
                                {"id":1,"label":"Embedded arid outcrops","color":"#b580ca"},
                                {"id":2,"label":"Outcrop companion rocks","color":"#88bcca"}]
        if layer == "cliff_confirmed_cuts":
            result["legend"] = legend("road_cut")
    elif layer in {"cliff_cuts", "building_styles", "vegetation_trees", "vegetation_shrubs", "vegetation_rocks"}:
        result.update(_sampled(layer,bounds))
    else:
        raise ValueError("Unknown debug layer")
    return result


# Categorical colours, shared across sampled providers. The label is always
# shown too: colour is not the only way to identify a regional rule.
PALETTE = ("#9ec8d5", "#ed995d", "#b6d98b", "#cf8ddd", "#e7da8a", "#83c8b0",
           "#e2a3b2", "#9aadeb", "#e0ba90", "#b9b9b9", "#cae0bc", "#ebc6e2", "#bddce9", "#e5dbb4")

def _sampled(layer, bounds):
    """Sample existing production selectors; never duplicate regional boundaries.

    Synthetic asset inputs explicitly expose appearance rules, not a claim that
    a particular building/tree exists at the sample. Real density/land-cover,
    seed, road exclusions and user settings still govern actual placement.
    """
    s,w,n,e = bounds
    edges_x,edges_y = np.linspace(w,e,41),np.linspace(s,n,25)
    lon,lat = np.meshgrid((edges_x[:-1]+edges_x[1:])/2,(edges_y[:-1]+edges_y[1:])/2)
    lo = (lon.ravel()+180)%360-180
    la = lat.ravel()
    if layer == "cliff_regions":
        values = clifftypes.select(la,lo)
        rows = legend("natural")
        note = "Natural cliff appearance from local climate and higher-priority canyon regions."
    elif layer == "cliff_cuts":
        import climate
        climates = np.array([climate.koppen_at(a,b) for a,b in zip(la,lo)])
        values = clifftypes.select(la,lo,context="road_cut",biomes=((climates>=4)&(climates<=7)).astype(int))
        rows = legend("road_cut")
        note = "Material choice for confirmed man-made cuts: arid climate vs limestone fallback. This does not predict where roads cut terrain."
    elif layer == "building_styles":
        import buildings
        keys = list(buildings.STYLES)
        values = np.array([keys.index(buildings.region_style(a,b).name) for a,b in zip(la,lo)])
        rows = [{"id":i,"label":key.replace("_"," ").title(),"color":PALETTE[i%len(PALETTE)]} for i,key in enumerate(keys)]
        note = "Building appearance defaults from the production style selector. Mapped buildings, explicit materials and per-building variation still take precedence."
    else:
        import vegetation as veg
        group = {"vegetation_trees":veg.G_DECIDUOUS,"vegetation_shrubs":veg.G_SHRUB,"vegetation_rocks":veg.G_ROCK}[layer]
        cover = 10 if layer == "vegetation_trees" else 60 if layer == "vegetation_rocks" else 20
        values = veg.regional(np.full(len(la),group), 8.0 if cover==10 else 1.0, la,lo,cover,.5)
        rows = [{"id":i,"label":name.replace("_"," ").title(),"color":PALETTE[i%len(PALETTE)]} for i,name in enumerate(veg.GROUPS)]
        note = "Production regional variant selector, using a fixed representative seed and " + {10:"deciduous tree on tree cover",20:"shrub on shrubland",60:"rock on bare ground"}[cover] + ". This is a rule preview, not vegetation density or exact spawn positions."
    present = set(values.tolist())
    rectangles = []
    for j in range(24):
        start = 0
        for i in range(1,41):
            if i < 40 and values[j*40+i] == values[j*40+start]:
                continue
            rectangles.append([int(values[j*40+start]), edges_x[start], edges_y[j], edges_x[i], edges_y[j+1]])
            start = i
    return {"features":debugcontours.features(rectangles, {r["id"]:r["label"] for r in rows}),
            "legend":[r for r in rows if r["id"] in present],
            "note":"Sampled regions; zoom in for boundary detail. " + note}
