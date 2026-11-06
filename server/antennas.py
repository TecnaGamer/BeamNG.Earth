"""Conservative, mapped ground communications structures, using installed stock shapes.

``antenna.dae`` is West Coast USA's complete lattice tower, not a rooftop aerial.  Its
visible bounds are (-4.510066, -4.510061, -1.8)..(4.510062, 4.510064, 61.14521) m.
Z=0 is the planting datum; the 1.8 m foundation stays buried.  Forest's catalog height
includes the foundation, whereas OSM height is measured above ground.  Mapped heights
therefore scale by ABOVE_GROUND_HEIGHT and emit CATALOG_HEIGHT * scale.

Only an explicitly lattice, ground-mounted communications mast/tower uses that shape.
The existing mobile tower is retained for explicitly mobile ground towers.  Ambiguous
generic towers, radar, guyed, rooftop and incompatible heights are deliberately omitted.
No models, boundaries or geographic points are synthesized here.  Layers are Features
tables, or worldprops' (Features, selected-row-indices) pairs.  Source identities are
deduplicated before half-open tile ownership, and the final, rounded placement XY is
sampled in one batch through the same final-ground callback used by road furniture.
"""
import hashlib
import json
import math
import re

import numpy as np

import mapgeom
import geotiles

GROUP = "antenna"
ASSET_PATH = "/levels/west_coast_usa/art/shapes/buildings/antenna.dae"
CATALOG_HEIGHT = 62.94521
ABOVE_GROUND_HEIGHT = 61.14521
BASE_LIFT = 0.0
MATERIAL_DIRS = ("/art/shapes/objects", "/art/shapes/common")

CELL_GROUP = "celltower"
CELL_ASSET_PATH = "/levels/johnson_valley/art/shapes/misc/celltower.dae"
# Existing catalog height and base lift are retained for renderer compatibility.
CELL_CATALOG_HEIGHT = 28.93
CELL_BASE_LIFT = 0.02
CELL_ABOVE_GROUND_HEIGHT = 28.91397 + CELL_BASE_LIFT
SCALE_RANGES = {GROUP: (0.5, 2.0), CELL_GROUP: (0.5, 1.75)}
MAX_FOOTPRINT_M = 20.0
DUPLICATE_POINT_M = 0.1

_COMM = {"communication", "communications", "telecommunication", "telecommunications"}
_MOBILE_CLASSES = {"mobile_phone_tower", "cell_tower", "cellular_tower"}
_NO = {"", "no", "false", "0", "none"}
_HEIGHT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(m|metres?|meters?|ft|feet|'|foot)?\s*$", re.I)
_OSM = re.compile(r"(?:^|[^a-z])(node|way|relation|n|w|r)[/ :_-]*(\d+)(?:@\d+)?(?:$|[^0-9])", re.I)


def tags_of(feats, i):
    """Normalized source tags, without mutating shared decoded feature tables."""
    val = _attr(feats, "tags", i, {})
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except (TypeError, ValueError):
            val = {}
    return {str(k).strip().lower(): str(v).strip().lower() for k, v in val.items()} if isinstance(val, dict) else {}


def height_m(value):
    """One unambiguous positive height in metres; malformed values stay unknown."""
    if value is None:
        return None
    m = _HEIGHT.fullmatch(str(value))
    if m is None:
        return None
    h = float(m[1]) * (0.3048 if (m[2] or "").lower() in {"ft", "feet", "'", "foot"} else 1.0)
    return h if math.isfinite(h) and h > 0.0 else None


def _selection(tags, cls=""):
    t = {str(k).strip().lower(): str(v).strip().lower() for k, v in tags.items()}
    cls = str(cls).strip().lower()
    mm, tt = t.get("man_made", ""), t.get("tower:type", "")
    construction = t.get("tower:construction", "")
    if (t.get("historic", "") not in _NO or t.get("power", "") not in _NO
            or any(t.get(k, "") not in _NO for k in ("abandoned", "disused", "demolished", "razed"))
            or any(k.startswith(("abandoned:", "disused:", "demolished:", "razed:")) for k in t)):
        return None, "historic_power_or_inactive"
    if cls == "radar" or t.get("radar", "") not in _NO or mm == "radar":
        return None, "radar_not_supported"
    if mm and mm not in {"tower", "mast", "communications_tower"}:
        return None, "unrelated_structure"
    if tt and tt not in _COMM:
        return None, "unrelated_tower_type"
    if "guy" in construction or t.get("guyed", "") not in _NO:
        return None, "guyed_not_supported"
    mount = " ".join(t.get(k, "") for k in ("location", "support", "mounting", "tower:mounting"))
    if (any(v in mount for v in ("roof", "building", "indoor", "underground", "wall"))
            or t.get("building", "") not in _NO
            or height_m(t.get("min_height")) is not None):
        return None, "not_ground_mounted"
    comm = tt in _COMM or any(t.get("communication:" + k) == "yes"
                             for k in ("radio", "television", "mobile_phone"))
    if mm in {"tower", "mast", "communications_tower"} and construction in {"lattice", "freestanding_lattice"} and comm:
        return GROUP, "explicit_ground_lattice_communication"
    mobile = cls in _MOBILE_CLASSES or (mm in {"tower", "mast"} and t.get("communication:mobile_phone") == "yes")
    if mobile:
        # A confirmed lattice structure must never turn into a monopole because its
        # other tags or height were incomplete.  Rooftop/radar is excluded above.
        if construction and construction not in {"monopole", "pole", "tubular", "freestanding"}:
            return None, "mobile_structure_not_supported"
        return CELL_GROUP, "explicit_mobile_ground_tower"
    return None, "structure_not_confirmed"


def classify(tags, cls=""):
    """Forest group, or None when the available model cannot represent the feature."""
    return _selection(tags, cls)[0]


def _attr(feats, key, i, default=None):
    values = feats.attrs.get(key)
    return values[i] if values is not None and i < len(values) else default


def _layers(layers):
    if layers is None:
        return
    if hasattr(layers, "kind"):
        yield layers, range(len(layers))
        return
    if isinstance(layers, tuple) and len(layers) == 2 and hasattr(layers[0], "kind"):
        feats, selected = layers
        yield feats, range(len(feats)) if selected is None else selected
        return
    for layer in layers:
        if layer is None:
            continue
        if isinstance(layer, tuple) and len(layer) == 2 and hasattr(layer[0], "kind"):
            feats, selected = layer
            yield feats, range(len(feats)) if selected is None else selected
        else:
            yield layer, range(len(layer))


def _osm_key(value):
    m = _OSM.search(str(value).lower())
    return (m[1][0], str(int(m[2]))) if m is not None else None


def _source_keys(feats, i, fid, tags):
    keys = {("feature", fid)}
    source = _osm_key(fid)
    if source:
        keys.add(source)
    if "openstreetmap" in str(_attr(feats, "src_dataset", i, "")).lower():
        source = _osm_key(_attr(feats, "src_record", i, ""))
        if source:
            keys.add(source)
    for key in ("osm_id", "osm_node_id", "osm_way_id", "source_id", "source_osm"):
        v = _attr(feats, key, i, tags.get(key))
        if v is None:
            continue
        source = _osm_key(v)
        if source:
            keys.add(source)
        elif key in {"osm_id", "osm_node_id", "osm_way_id"} and str(v).isdigit():
            keys.add(("w" if key == "osm_way_id" else "n", str(int(v))))
    sources = _attr(feats, "sources", i, [])
    if isinstance(sources, str):
        try:
            sources = json.loads(sources)
        except (TypeError, ValueError):
            sources = []
    if isinstance(sources, (list, tuple)):
        for s in sources:
            if isinstance(s, dict) and "openstreetmap" in str(s.get("dataset", "")).lower():
                source = _osm_key(s.get("record_id", ""))
                if source:
                    keys.add(source)
    return keys


def _geometry(frame, feats, i):
    if int(feats.kind[i]) not in (mapgeom.POINT, mapgeom.POLYGON):
        return None
    parts = feats.parts(i)
    if not parts or not parts[0] or not len(parts[0][0]):
        return None
    ring = np.asarray(parts[0][0], np.float64)
    e, n = mapgeom.enu_xy(feats, frame, ring)
    xy = np.column_stack((e, n))
    if len(xy) > 1 and np.allclose(xy[0], xy[-1]):
        xy = xy[:-1]
    if not len(xy) or not np.isfinite(xy).all() or np.ptp(xy, axis=0).max() > MAX_FOOTPRINT_M:
        return None
    return xy, xy.mean(axis=0)


def _inside(point, ring):
    """Small pure even-odd check; called only for the few tower/building overlaps."""
    x, y = point
    hit = False
    for a, b in zip(ring, np.roll(ring, -1, axis=0)):
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            hit = not hit
    return hit


def _building_rings(frame, layers):
    for feats, selected in _layers(layers):
        for i in selected:
            if int(feats.kind[i]) != mapgeom.POLYGON:
                continue
            for part in feats.parts(int(i)):
                rings = []
                for ring in part:
                    if len(ring) >= 3:
                        e, n = mapgeom.enu_xy(feats, frame, np.asarray(ring, np.float64))
                        rings.append(np.column_stack((e, n)))
                if rings:
                    yield rings


def _emit(diagnostics, record):
    if diagnostics is not None:
        diagnostics(record) if callable(diagnostics) else diagnostics.append(record)


def _info(feats, i, tags, keys, reason):
    osm_ids = sorted({{"n": "node", "w": "way", "r": "relation"}[k] + "/" + v
                      for k, v in keys if k in {"n", "w", "r"}})
    return {"feature_id": str(feats.ids[i]), "classification": str(_attr(feats, "class", i, "")),
            "tags": tags, "reason": reason, "osm_ids": osm_ids,
            "source_record": str(_attr(feats, "src_record", i, osm_ids[0] if len(osm_ids) == 1 else ""))}


def _preferred_layers(layers, diagnostics):
    """Choose source records before compatibility, including negative newer tags.

    Direct supplemental OSM metadata supersedes a sparse Overture source copy.
    Among direct records (or among source copies), the greatest known OSM version
    wins, then richer tags.  A rejected newer structure must not expose an older
    eligible copy.  Selection is independent of cell/input order.
    """
    records = []
    for feats, selected in _layers(layers):
        for index in selected:
            i = int(index)
            fid = str(feats.ids[i])
            tags = tags_of(feats, i)
            keys = _source_keys(feats, i, fid, tags)
            record = str(_attr(feats, "src_record", i, ""))
            version = re.search(r"@(\d+)$", record)
            priority = (not fid.startswith("osm-"), -int(version[1]) if version else 0,
                        -len(tags), -int(height_m(tags.get("height")) is not None), fid)
            records.append((priority, feats, i, tags, keys))
    records.sort(key=lambda r: r[0])
    seen = set()
    for _, feats, i, tags, keys in records:
        if keys & seen:
            _emit(diagnostics, dict(_info(feats, i, tags, keys, "superseded_source_record"), generated=False))
            continue
        seen.update(keys)
        yield feats, (i,)


def rows(frame, layers, box, elev, ground=None, *, buildings=(), diagnostics=None):
    """Owned Forest rows. ground(x,y) returns final ENU Z; elev(lat,lon) is its fallback.

    ``diagnostics`` is None by default, or a list/callback receiving decisions and
    exact emitted coordinates.  Distinct nearby point towers are kept; duplicate
    point/footprint representations and matching OSM source IDs emit only once.
    """
    candidates = []
    x0, y0, x1, y1 = map(float, box)
    for feats, selected in _preferred_layers(layers, diagnostics):
        for index in selected:
            i = int(index)
            fid = str(feats.ids[i])
            tags = tags_of(feats, i)
            cls = str(_attr(feats, "class", i, ""))
            group, reason = _selection(tags, cls)
            keys = _source_keys(feats, i, fid, tags)
            info = _info(feats, i, tags, keys, reason)
            if group is None:
                _emit(diagnostics, dict(info, generated=False))
                continue
            geom = _geometry(frame, feats, i)
            if geom is None:
                _emit(diagnostics, dict(info, generated=False, reason="unsupported_geometry"))
                continue
            ring, centre = geom
            # Nearby candidates must participate in cross-cell dedup before ownership.
            if not (x0 - MAX_FOOTPRINT_M <= centre[0] <= x1 + MAX_FOOTPRINT_M
                    and y0 - MAX_FOOTPRINT_M <= centre[1] <= y1 + MAX_FOOTPRINT_M):
                continue
            total, above, lift, asset = ((CATALOG_HEIGHT, ABOVE_GROUND_HEIGHT, BASE_LIFT, ASSET_PATH)
                                        if group == GROUP else
                                        (CELL_CATALOG_HEIGHT, CELL_ABOVE_GROUND_HEIGHT, CELL_BASE_LIFT, CELL_ASSET_PATH))
            raw_height = tags.get("height", _attr(feats, "height", i))
            h = height_m(raw_height)
            if raw_height is not None and str(raw_height).strip().lower() not in {"", "nan", "none"} and h is None:
                _emit(diagnostics, dict(info, generated=False, reason="invalid_height"))
                continue
            scale = h / above if h is not None else 1.0
            low, high = SCALE_RANGES[group]
            if not low <= scale <= high:
                _emit(diagnostics, dict(info, generated=False, reason="height_incompatible_with_asset"))
                continue
            candidates.append(dict(info, group=group, ring=ring, centre=centre, kind=int(feats.kind[i]),
                                   keys=keys, catalog_height=total,
                                   above_height=above, scale=scale, lift=lift, asset=asset,
                                   height_source="mapped" if h is not None else "model_default"))
    if not candidates:
        return []
    # Explicit points take precedence over a polygon centre. Remaining ties are
    # stable across source ordering, cells and adjacent tiles.
    candidates.sort(key=lambda p: (p["kind"] != mapgeom.POINT,
                                  not p["feature_id"].startswith("osm-"),
                                  p["height_source"] != "mapped", -len(p["tags"]),
                                  p["feature_id"], p["group"]))
    selected, seen = [], set()
    for p in candidates:
        duplicate = bool(p["keys"] & seen)
        if not duplicate:
            for other in selected:
                if p["group"] != other["group"]:
                    continue
                if (p["kind"] == other["kind"] == mapgeom.POINT and p["tags"] == other["tags"]
                        and np.linalg.norm(p["centre"] - other["centre"]) <= DUPLICATE_POINT_M):
                    duplicate = True
                    break
                if (p["kind"] == mapgeom.POLYGON and _inside(other["centre"], p["ring"])) \
                        or (other["kind"] == mapgeom.POLYGON and _inside(p["centre"], other["ring"])):
                    duplicate = True
                    break
        if duplicate:
            _emit(diagnostics, {k: v for k, v in dict(p, generated=False, reason="duplicate_source_or_footprint").items()
                                if k not in {"keys", "ring", "centre"}})
            continue
        seen.update(p["keys"])
        selected.append(p)
    owned = [p for p in selected if geotiles.box_owns(box, p["centre"][0], p["centre"][1])]
    if not owned:
        return []
    bld = list(_building_rings(frame, buildings))
    supported = []
    for p in owned:
        if any(_inside(p["centre"], rings[0]) and not any(_inside(p["centre"], hole) for hole in rings[1:])
               for rings in bld):
            _emit(diagnostics, {k: v for k, v in dict(p, generated=False, reason="overlaps_building").items()
                                if k not in {"keys", "ring", "centre"}})
        else:
            supported.append(p)
    if not supported:
        return []
    xy = np.round(np.asarray([p["centre"] for p in supported], np.float64), 2)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    if ground is not None:
        z = np.broadcast_to(np.asarray(ground(xy[:, 0], xy[:, 1]), np.float64), (len(xy),))
    else:
        _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
        z = np.broadcast_to(np.asarray(z, np.float64), (len(xy),))
    out = []
    for p, point, la, lo, zz in zip(supported, xy, lat, lon, z):
        if not np.isfinite(zz):
            _emit(diagnostics, dict(feature_id=p["feature_id"], generated=False, reason="missing_final_ground"))
            continue
        digest = hashlib.sha256((p["feature_id"] + "|antenna_yaw").encode()).digest()
        yaw = int.from_bytes(digest[:8], "big") / 2 ** 64 * 360.0
        row = [p["group"], float(point[0]), float(point[1]), round(float(zz) + p["lift"] * p["scale"], 3),
               round(yaw, 1) % 360.0, 1.0, round(p["catalog_height"] * p["scale"], 6), 0.0]
        out.append(row)
        if diagnostics is not None:
            _emit(diagnostics, {k: v for k, v in dict(p, generated=True, latitude=float(la), longitude=float(lo),
                                terrain_z=float(zz), model_scale=p["scale"], above_ground_height=p["above_height"] * p["scale"],
                                placement=row[1:4], tile_box=list(box)).items() if k not in {"keys", "ring", "centre"}})
    return out


def tile_rows(frame, tx, ty, md, elev, ground):
    """Mapped tower rows from the existing worldwide Overture infrastructure.

    The 20 m source margin keeps tower footprints and duplicate representations
    available on both sides of a tile edge. Ownership still uses the exact tile
    box. ``ground`` is the caller's final drawn terrain callback, shared with
    road furniture. No network query or regional supplemental provider is added.
    """
    if md is None:
        return []
    import antenna_debug as debug
    import roads
    import worldprops

    keys = roads.cells_of_tile(frame, tx, ty, margin=MAX_FOOTPRINT_M)
    return rows(frame, worldprops._layer(md, keys, "infrastructure"), roads.tile_bbox(tx, ty), elev, ground,
                buildings=worldprops._layer(md, keys, "buildings"),
                diagnostics=debug.antenna if debug.ENABLED else None)
