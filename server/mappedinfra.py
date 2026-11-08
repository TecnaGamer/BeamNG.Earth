"""Mapped infrastructure asset selection; no changes to the source OSM classification.

Stock mesh bounds/anchors: tools/audit_infrastructure_assets.py and the numeric
fixture. The two user-provided spellings do not exist in the installed game;
the real stems are italy_bld_antenna and electric_pole_wood_old_01.
"""
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np

VERSION = "mapped-infrastructure-assets-v4-supported-wire-spans"
WWVB_PATH = Path(__file__).with_name("data") / "wwvb_antennas.json"
MODEL = {
    "celltower": (28.93660, -0.02262997),
    "mast": (29.95665, -0.42177),
    "tower": (62.94521, -1.8),
    "pole": (10.04585, -0.08358),
}
POLE_ANCHORS = ((-0.5226234, 0.003783733, 9.209692),
                (0.6694779, -0.125222, 7.245391), (-0.6304421, -0.125222, 7.245351))
_LENGTH = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(m|metres?|meters?|ft|feet|')?\s*$", re.I)


def source_id(source, fallback=""):
    """Keep OSM type and ID, stripping only the revision, otherwise retain Overture identity."""
    if str(source).startswith("overture:"):
        return str(source)
    m = re.fullmatch(r"([nwr])(\d+)(?:@[^\s]+)?", str(source))
    if m:
        return "osm:" + {"n": "node", "w": "way", "r": "relation"}[m[1]] + ":" + m[2]
    m = re.fullmatch(r"osm:(node|way|relation):\d+", str(source))
    return str(source) if m else "overture:" + str(fallback)


@lru_cache(maxsize=1)
def wwvb():
    return json.loads(WWVB_PATH.read_text(encoding="utf-8"))["elements"]


def wwvb_mast(fid, lat, lon):
    for e in wwvb():
        if e["type"] == "node" and fid == f"osm:node:{e['id']}":
            # ID AND independently recorded site position; never a Fort Collins radius rule.
            return math.hypot((lat - e["lat"]) * 111320,
                              (lon - e["lon"]) * 111320 * math.cos(math.radians(lat))) < 10.
    return False


def wwvb_radio_line(fid):
    return any(e["type"] == "way" and fid == f"osm:way:{e['id']}" for e in wwvb())


def signature():
    import mappedpoles
    return VERSION, mappedpoles.EVERYWHERE, hashlib.sha256(WWVB_PATH.read_bytes()).hexdigest()


def asset_for(cls, tags, fid="", lat=0., lon=0.):
    import mappedpoles
    if not mappedpoles._active(tags):
        return None
    # A power tower is never a communications antenna, even with contradictory man_made tags.
    if tags.get("power") or cls in {"power_tower", "power_pole"}:
        return None
    # Explicit mobile communications select the stock phone tower before generic mast/tower rules.
    if str(tags.get("communication:mobile_phone", "")).strip().lower() == "yes":
        return "celltower"
    if cls in {"water_tower", "cooling_tower", "radar"}:
        return None
    if tags.get("advertising") or tags.get("man_made") in {"water_tower", "chimney", "lighthouse", "windmill"}:
        return None
    subtype = str(tags.get("tower:type", "")).lower()
    if subtype and subtype not in {"communication", "communications", "telecommunication", "radio"}:
        return None
    man_made = tags.get("man_made")
    if man_made == "mast" or cls in {"mast", "communication_mast"}:
        return "mast"
    if man_made == "tower" or cls in {"tower", "communication_tower", "mobile_phone_tower"}:
        return "mast" if wwvb_mast(fid, lat, lon) else "tower"
    return None


def height(tags, value, asset):
    for raw in (tags.get("height"), value):
        match = _LENGTH.fullmatch(str(raw))
        if match:
            h = float(match[1]) * (0.3048 if (match[2] or "").lower() in {"ft", "feet", "'"} else 1.)
            if math.isfinite(h) and 0.5 <= h <= 1000.:
                return h
    return MODEL[asset][0]


def record(asset, fid, x, y, z, tags=None, value=None, yaw=0., lat=None, lon=None):
    h = height(tags or {}, value, asset)
    modeled, bottom = MODEL[asset]
    scale = h / modeled
    return {"asset": asset, "id": fid, "x": float(x), "y": float(y), "z": float(z - bottom * scale),
            "yaw": float(yaw), "scale": scale, "height": h, "lat": lat, "lon": lon}


def tile_structures(frame, tx, ty, md, elev):
    import roads
    import worldprops as wp
    if md is None:
        return []
    box = roads.tile_bbox(tx, ty)
    candidates = {}
    footprints = {}
    for f, rows in wp._layer(md, roads.cells_of_tile(frame, tx, ty), "infrastructure"):
        cls = wp._cls(f)
        if cls is None:
            continue
        for i in (range(len(f.kind)) if rows is None else rows):
            if int(f.kind[i]) not in (1, 3):
                continue
            tags = wp._tags_of(f, i)
            if asset_for(str(cls[i]), tags) is None:
                continue
            source = f.attrs.get("src_record", [""] * len(f.kind))[i]
            fid = source_id(source, f.ids[i])
            xy = wp._ring_xy(f, i, frame)
            if xy is None:
                continue
            x, y = wp._centre(xy)
            if not wp._inside(x, y, box):
                continue
            la, lo = wp._ll(frame, x, y)
            asset = asset_for(str(cls[i]), tags, fid, la, lo)
            if asset:
                val = f.attrs.get("height", [None] * len(f.kind))[i]
                candidates.setdefault(fid, (asset, x, y, la, lo, tags, val))
                if int(f.kind[i]) == 3:
                    footprints[fid] = xy
    # Vetted OSM snapshot also covers an older Overture release or stripped source tags.
    # IDs/coordinates/heights are unchanged, not an inferred station layout.
    for e in wwvb():
        if e["type"] != "node":
            continue
        x, y, _ = frame.geodetic_to_enu(e["lat"], e["lon"], 0.)
        if wp._inside(x, y, box):
            candidates.setdefault(f"osm:node:{e['id']}", ("mast", x, y, e["lat"], e["lon"], e["tags"], None))
    # A mapped node inside the same tower's small footprint is its geographic placement.
    # Keep both source identities as provenance, draw once at the node rather than the polygon centre.
    aliases = {}
    from polygon import Path as Polygon
    for fid, ring in footprints.items():
        asset = candidates[fid][0]
        for node, candidate in sorted(candidates.items()):
            if node in footprints or node == fid or candidate[0] != asset:
                continue
            if Polygon(ring).contains_points(np.array([[candidate[1], candidate[2]]])).any():
                aliases.setdefault(node, []).append(fid)
                break
    redundant = {fid for ids in aliases.values() for fid in ids}
    occupied, out = set(), []
    for fid, (asset, x, y, la, lo, tags, val) in sorted(candidates.items()):
        if fid in redundant:
            continue
        coordinate = (round(la, 7), round(lo, 7))
        if coordinate in occupied:
            continue
        occupied.add(coordinate)
        _, _, z = frame.geodetic_to_enu(la, lo, float(np.asarray(elev(np.array([la]), np.array([lo])))[0]))
        if math.isfinite(float(z)):
            out.append(record(asset, fid, x, y, z, tags, val, lat=la, lon=lo))
            if fid in aliases:
                out[-1]["sourceIds"] = [fid] + sorted(aliases[fid])
    return out


def wire_record(a, b, pa, pb, conductor):
    span = math.dist(pa, pb)
    return {"id": f"wire:{a}:{b}:{conductor}", "asset": "wire30" if span <= 35. else "wire40",
            "x": float(pa[0]), "y": float(pa[1]), "z": float(pa[2]), "end": list(map(float, pb)),
            "scale": 1., "yaw": 0., "supports": [a, b]}


def span_clear(frame, a, b, elev, rings=()):
    """Conservative: omit a span over a building or with inadequate terrain clearance; never move its supports."""
    t = np.linspace(0., 1., max(3, int(math.ceil(math.dist(a, b) / 2.)) + 1))
    pts = np.asarray(a)[None] * (1. - t[:, None]) + np.asarray(b)[None] * t[:, None]
    # Stock 30/40 m meshes have the same measured 1.278578 m sag, left unscaled in Z.
    pts[:, 2] -= 4. * 1.278578 * t * (1. - t)
    la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], pts[:, 2])
    _, _, ground = frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), float))
    if not np.isfinite(ground).all() or np.any(pts[:, 2] - ground < 4.5):
        return False
    from polygon import Path as Polygon
    for ring in rings:
        ring = np.asarray(ring, float)[:, :2]
        if len(ring) < 3 or np.any(ring.max(0) < pts[:, :2].min(0)) or np.any(ring.min(0) > pts[:, :2].max(0)):
            continue
        if Polygon(ring).contains_points(pts[:, :2]).any():
            return False
        # Also catch a narrow footprint between terrain samples.
        for p, q in zip(ring, np.roll(ring, -1, axis=0)):
            d, e = np.asarray(b)[:2] - np.asarray(a)[:2], q - p
            cross = d[0] * e[1] - d[1] * e[0]
            if abs(cross) < 1e-10:
                continue
            v = p - np.asarray(a)[:2]
            u, w = (v[0] * e[1] - v[1] * e[0]) / cross, (v[0] * d[1] - v[1] * d[0]) / cross
            if 0. <= u <= 1. and 0. <= w <= 1.:
                return False
    return True
