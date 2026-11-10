"""Mapped OSM wind turbines worldwide, from a compact bundled spatial index.

No runtime Overpass requests, farm polygons or inferred turbine layouts. The
verified Happy Jack fixture remains a fallback if the global index is absent.
ETK's installation origin is at grade; its extended lower tower skirt is buried
as in the stock map. Model dimensions below are asset measurements, not claims
about the real Suzlon turbine (the selected OSM node supplies no dimensions).
"""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import logging
import math
import os
import re
import threading

import numpy as np

import geotiles

log = logging.getLogger("earthd.windturbines")
SELECTED_OSM_ID = "osm:node:1175681635"
DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wind_turbine_phase1.json")
# Pin the snapshot in Python as well as the data file: updating it must change
# this source fingerprint, so the existing revisit cache cannot reuse old rows.
DATA_SHA256 = "9b1b4947d983b4b42d7961b47c04a318fb8ed05bf70ab61279a19b65226c627b"
INDEX_PATH = os.path.join(os.path.dirname(DATA_PATH), "wind_turbines.npz")
# Updated with the reproducible export; included in the revisit source fingerprint.
INDEX_SHA256 = "f325ec7bc8ee841da15eaf34cd1b62403b9ad3d0a2326f1dd8d13c497000a3a3"
INDEX_COLUMNS = ("lat", "lon", "osm_id", "osm_type", "height", "hub_height", "rotor_diameter")
_index_lock = threading.Lock()
MODEL_HUB_M = 101.410286
MODEL_ROTOR_M = 69.158482
MODEL_TIP_M = MODEL_HUB_M + MODEL_ROTOR_M / 2.0
MIN_SCALE, MAX_SCALE = 0.005, 5.0     # include explicitly measured small domestic turbines
_LENGTH = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(m|metres?|meters?|ft|feet|')?\s*$", re.I)


@dataclass(frozen=True)
class Turbine:
    id: str
    lat: float
    lon: float
    tags: dict
    version: int


def _active_tags(tags):
    """Individual operating generators only; a wind farm/plant is never a turbine."""
    if not isinstance(tags, dict) or tags.get("power") != "generator":
        return False
    source, method = tags.get("generator:source", ""), tags.get("generator:method", "")
    if not (source == "wind" or (not source and method == "wind_turbine")):
        return False
    inactive = ("disused", "abandoned", "demolished", "construction", "proposed", "removed", "razed")
    if any(str(tags.get(k, "")).lower() in ("yes", "true", "1") for k in inactive):
        return False
    if any(k + ":power" in tags for k in inactive):
        return False
    if tags.get("end_date") or tags.get("generator:status") in inactive + ("decommissioned",):
        return False
    if tags.get("type") == "site" or tags.get("site") in ("wind_farm", "windfarm"):
        return False
    return tags.get("generator:type") not in ("wind_farm", "windfarm", "wind_park")


def _way_ring(element):
    """A small, closed individual-generator footprint, never a farm area or a line."""
    try:
        ring = np.array([(float(p["lon"]), float(p["lat"])) for p in element["geometry"]])
        if len(ring) < 4 or len(ring) > 512 or not np.isfinite(ring).all():
            return None
        if not np.array_equal(ring[0], ring[-1]) or (np.abs(ring[:, 1]) > 85).any() or (np.abs(ring[:, 0]) > 180).any():
            return None
        ring[:, 0] = np.rad2deg(np.unwrap(np.deg2rad(ring[:, 0])))
        metres = np.array([111320 * math.cos(math.radians(float(ring[:, 1].mean()))), 110540])
        xy = (ring - ring[0]) * metres
        # Large generator areas can describe a farm; only an individual tower footprint is suitable.
        if np.ptp(xy, axis=0).max() > 100.0:
            return None
        # A self-crossing footprint has no unambiguous tower centre. Reject it
        # rather than constructing a plausible-looking but unmapped position.
        for i in range(len(xy) - 1):
            a, b = xy[i], xy[i + 1]
            if np.linalg.norm(b - a) < 1e-6:
                return None
            for j in range(i + 2, len(xy) - 1):
                if i == 0 and j == len(xy) - 2:
                    continue
                c, d = xy[j], xy[j + 1]
                if (np.maximum(np.minimum(a, b), np.minimum(c, d)) >
                        np.minimum(np.maximum(a, b), np.maximum(c, d)) + 1e-9).any():
                    continue
                def cross(u, v):
                    return u[0] * v[1] - u[1] * v[0]
                if cross(b - a, c - a) * cross(b - a, d - a) <= 0 and cross(d - c, a - c) * cross(d - c, b - c) <= 0:
                    return None
        cross = xy[:-1, 0] * xy[1:, 1] - xy[1:, 0] * xy[:-1, 1]
        area2 = float(cross.sum())
        if abs(area2) < 0.01:
            return None
        centre = ((xy[:-1] + xy[1:]) * cross[:, None]).sum(axis=0) / (3 * area2)
        from polygon import Path
        if not Path(xy).contains_points(centre[None])[0]:
            return None
        ll = centre / metres + ring[0]
        return ring, (float(ll[1]), float((ll[0] + 180) % 360 - 180))
    except (KeyError, ValueError, TypeError, IndexError):
        return None


def is_wind_turbine(element):
    """An active wind-generator node or a small closed, explicitly tagged footprint."""
    if not isinstance(element, dict) or not _active_tags(element.get("tags")):
        return False
    try:
        if int(element["id"]) <= 0:
            return False
        if element.get("type") == "way":
            return _way_ring(element) is not None
        if element.get("type") != "node":
            return False
        lat, lon = float(element["lat"]), float(element["lon"])
        return (math.isfinite(lat) and math.isfinite(lon)
                and -85.0 <= lat <= 85.0 and -180.0 <= lon <= 180.0)
    except (KeyError, ValueError, TypeError, OverflowError):
        return False


def features_from_document(document):
    """Normalize a developer OSM export; preserve type/ID and prefer nodes over duplicate footprints.

    Node positions remain exact. A way position is its mapped footprint's area
    centroid; only closed, small, nondegenerate footprints are accepted.
    """
    latest = {}
    for element in document.get("elements", ()) if isinstance(document, dict) else ():
        if not isinstance(element, dict) or element.get("type") not in ("node", "way"):
            continue
        try:
            key = (element["type"], int(element["id"]))
            version = int(element.get("version", 0))
            if key not in latest or version > int(latest[key].get("version", 0)):
                latest[key] = element
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    nodes, ways, bins = [], [], {}
    for (kind, fid), element in latest.items():
        if not is_wind_turbine(element):
            continue
        if kind == "node":
            t = Turbine(f"osm:node:{fid}", float(element["lat"]), float(element["lon"]),
                        dict(element["tags"]), int(element.get("version", 0)))
            nodes.append(t)
            bins.setdefault((math.floor(t.lat * 1000), math.floor((t.lon + 180) * 1000) % 360000), []).append(t)
        else:
            ways.append(element)
    node_ids = {int(t.id.rsplit(":", 1)[1]) for t in nodes}
    out = list(nodes)
    for element in ways:
        # Explicitly reusing a turbine node is an unambiguous duplicate mapping.
        if any(n in node_ids for n in element.get("nodes", ())):
            continue
        ring, (lat, lon) = _way_ring(element)
        candidates = []
        for a in range(math.floor(float(ring[:, 1].min()) * 1000) - 1, math.floor(float(ring[:, 1].max()) * 1000) + 2):
            for o in range(math.floor((float(ring[:, 0].min()) + 180) * 1000) - 1,
                           math.floor((float(ring[:, 0].max()) + 180) * 1000) + 2):
                candidates.extend(bins.get((a, o % 360000), ()))
        from polygon import Path
        duplicate = False
        for node in candidates:
            reference = float(ring[0, 0])
            nl = reference + (node.lon - reference + 180) % 360 - 180
            if Path(ring).contains_points(np.array([[nl, node.lat]]))[0] or math.hypot(
                    ((node.lon - lon + 180) % 360 - 180) * 111320 * math.cos(math.radians(lat)),
                    (node.lat - lat) * 110540) < 2:
                duplicate = True
                break
        if not duplicate:
            out.append(Turbine(f"osm:way:{int(element['id'])}", lat, lon, dict(element["tags"]),
                               int(element.get("version", 0))))
    # Distinct OSM IDs sometimes map the exact same physical point. Keep one
    # deterministic identity, preferring a node and the newest mapped version;
    # do not merge nearby, separately mapped turbines or round coordinates.
    points = {}
    for t in out:
        position = (t.lat, -180.0 if t.lon == 180.0 else t.lon)
        rank = (0 if t.id.startswith("osm:node:") else 1, -t.version, int(t.id.rsplit(":", 1)[1]))
        if position not in points or rank < points[position][0]:
            points[position] = (rank, t)
    return tuple(sorted((t for _, t in points.values()), key=lambda t: t.id))


def selected_from_document(document):
    """Read/deduplicate only the allowlisted feature, retaining its exact coordinates and OSM ID."""
    return tuple(t for t in features_from_document(document) if t.id == SELECTED_OSM_ID)


@lru_cache(maxsize=1)
def _load():
    try:
        with open(DATA_PATH, "rb") as source:
            raw = source.read()
        # Git/Windows may check out text as CRLF; the verified feature is the
        # same snapshot. Pin its LF representation, not the checkout's endings.
        if hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest() != DATA_SHA256:
            raise ValueError("Phase 1 OSM snapshot differs from its pinned fingerprint")
        features = selected_from_document(json.loads(raw))
        if len(features) != 1:
            raise ValueError("Phase 1 snapshot must contain the selected turbine")
        return features
    except (OSError, ValueError, TypeError) as error:
        log.warning("wind turbine omitted: %s", error)
        return ()


def _mirror_dir():
    """The uncompressed copy's folder in the server's cache (revisit.CACHE_DIR, set in every server process), keyed by
    the index's hash, or None outside a server run."""
    import revisit
    base = getattr(revisit, "CACHE_DIR", None)
    return os.path.join(base, "turbines", INDEX_SHA256[:16]) if base else None


def _mapped():
    """The index's columns memory-mapped read-only from the cache's uncompressed copy (one copy in the page cache for
    all the server's processes: the bundled npz is deflated, 17.5 MB decompressed in each road process otherwise), or
    None (no cache folder, not written yet, damaged)."""
    d = _mirror_dir()
    if d is None:
        return None
    try:
        return {name: np.asarray(np.load(os.path.join(d, name + ".npy"), mmap_mode="r", allow_pickle=False))
                for name in INDEX_COLUMNS}
    except (OSError, ValueError, EOFError):
        return None


def _decompressed():
    """The index's columns read from the bundled npz (its hash checked by the caller), written to the cache's copy
    for the next processes (atomically, each column)."""
    with np.load(INDEX_PATH, allow_pickle=False) as data:
        arrays = {name: np.array(data[name]) for name in INDEX_COLUMNS}
    d = _mirror_dir()
    if d is not None:
        try:
            import staging
            os.makedirs(d, exist_ok=True)
            for name, a in arrays.items():
                path = os.path.join(d, name + ".npy")
                if not os.path.exists(path):
                    tmp = staging.tmp_for(path)
                    with open(tmp, "wb") as f:
                        np.save(f, a, allow_pickle=False)
                    staging.replace(tmp, path)
        except OSError as error:        # (no room: each process keeps its own copy, as before)
            log.debug("wind turbine index copy not written: %s", error)
    return arrays


@lru_cache(maxsize=1)
def _index():
    """Read the global index once per worker, with no pickle or runtime network."""
    try:
        with open(INDEX_PATH, "rb") as source:
            digest = hashlib.sha256()
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
            if digest.hexdigest() != INDEX_SHA256:
                raise ValueError("global turbine index differs from its source fingerprint")
        arrays = _mapped() or _decompressed()
        n = len(arrays["lat"])
        if not n or any(a.shape != (n,) for a in arrays.values()):
            raise ValueError("invalid global turbine array lengths")
        if arrays["osm_id"].dtype.kind not in "iu" or arrays["osm_type"].dtype.kind not in "iu":
            raise ValueError("non-integer OSM identities")
        lat, lon = arrays["lat"], arrays["lon"]
        if (not np.isfinite(lat).all() or not np.isfinite(lon).all() or (np.abs(lat) > 85).any()
                or (np.abs(lon) > 180).any() or (lat[1:] < lat[:-1]).any()
                or (arrays["osm_id"] <= 0).any() or ((arrays["osm_type"] < 0) | (arrays["osm_type"] > 1)).any()):
            raise ValueError("invalid coordinates, order or OSM identities")
        for array in arrays.values():
            array.flags.writeable = False
        return arrays
    except (OSError, ValueError, TypeError, KeyError) as error:
        log.warning("global wind turbine index unavailable; retaining verified Happy Jack fallback: %s", error)
        return None


def near(la0, lo0, la1, lo1):
    """Mapped turbines in a latitude/longitude window; lo0>lo1 wraps the dateline.

    Binary search the latitude index before selecting longitude. Only local
    records become Python objects, even when the database contains hundreds of
    thousands of turbines. Both endpoints are inclusive; tile ownership is
    subsequently resolved by the existing exact half-open geometry test.
    """
    with _index_lock:
        data = _index()
    if data is None:
        return tuple(t for t in _load() if la0 <= t.lat <= la1 and
                     (lo0 <= t.lon <= lo1 if lo0 <= lo1 else t.lon >= lo0 or t.lon <= lo1))
    i = int(np.searchsorted(data["lat"], la0, side="left"))
    j = int(np.searchsorted(data["lat"], la1, side="right"))
    lon = data["lon"][i:j]
    sel = (lon >= lo0) & (lon <= lo1) if lo0 <= lo1 else (lon >= lo0) | (lon <= lo1)
    if lo0 == -180:
        sel |= lon == 180
    if lo1 == 180:
        sel |= lon == -180
    out = []
    for k in np.flatnonzero(sel) + i:
        tags = {}
        for column, tag in (("height", "height"), ("hub_height", "height:hub"), ("rotor_diameter", "rotor:diameter")):
            length = float(data[column][k])
            if math.isfinite(length) and length > 0:
                tags[tag] = str(length)
        kind = "node" if data["osm_type"][k] == 0 else "way"
        out.append(Turbine(f"osm:{kind}:{int(data['osm_id'][k])}", float(data["lat"][k]),
                           float(data["lon"][k]), tags, 0))
    return tuple(out)


def length_m(value):
    """Positive OSM metres or explicit feet; malformed, unknown and non-finite values are absent."""
    match = _LENGTH.fullmatch(str(value)) if value is not None else None
    if not match:
        return None
    length = float(match[1])
    if (match[2] or "").lower() in ("ft", "feet", "'"):
        length *= 0.3048
    return length if math.isfinite(length) and length > 0 else None


def dimensions(tags):
    """Uniform scale from hub height, else rotor diameter, else overall height; model defaults otherwise.

    Independent hub/rotor proportions are not fabricated. The chosen basis and
    resulting model dimensions are carried with the record for inspection.
    """
    scale, basis = 1.0, "model_default"
    for key, default in (("height:hub", MODEL_HUB_M), ("rotor:diameter", MODEL_ROTOR_M), ("height", MODEL_TIP_M)):
        length = length_m(tags.get(key))
        if length is not None and MIN_SCALE <= length / default <= MAX_SCALE:
            scale, basis = length / default, "osm:" + key
            break
    return {"basis": basis, "hub_height_m": MODEL_HUB_M * scale,
            "rotor_diameter_m": MODEL_ROTOR_M * scale, "tip_height_m": MODEL_TIP_M * scale,
            "scale": scale}


# Small turbines are left out: a rotor under SMALL_ROTOR_M or a hub under SMALL_HUB_M is a rooftop, mast or tower-mounted
# one (the Eiffel Tower's two 3.2 m turbines, mapped at height 134 on its second floor, stood under it scaled down to a
# toy of the stock utility turbine, Discord 2026-11-06); the model is a utility-scale turbine standing on the ground.
SMALL_ROTOR_M = 10.0
SMALL_HUB_M = 15.0


def small(tags):
    """Is this a small (rooftop, mast, structure-mounted) turbine by its mapped rotor or hub (SMALL_*)?"""
    rotor, hub = length_m(tags.get("rotor:diameter")), length_m(tags.get("height:hub"))
    return (rotor is not None and rotor < SMALL_ROTOR_M) or (hub is not None and hub < SMALL_HUB_M)


def rows_for_features(frame, features, elev, collide=True):
    """One batched terrain sample and coordinate conversion for a set of mapped turbines."""
    if not features:
        return []
    lat = np.array([t.lat for t in features], np.float64)
    lon = np.array([t.lon for t in features], np.float64)
    try:
        height = np.broadcast_to(np.asarray(elev(lat, lon), np.float64), lat.shape)
        valid = np.isfinite(height)
        if not valid.all():
            import revisit
            revisit.fault()
        x, y, z = geotiles.geodetic_to_world(frame, lat, lon, height)
    except Exception as error:
        import revisit
        revisit.fault()
        log.debug("wind turbine terrain unavailable: %s", error)
        return []
    out = []
    for k, turbine in enumerate(features):
        if not valid[k] or small(turbine.tags):
            continue
        size = dimensions(turbine.tags)
        out.append({"id": turbine.id, "lat": turbine.lat, "lon": turbine.lon,
                    "x": float(x[k]), "y": float(y[k]), "z": float(z[k]), "yaw": 0.0,
                    "scale": size["scale"], "dimensions": size, "osm_version": turbine.version,
                    "collide": bool(collide)})
    return out


def tile_turbines(frame, tx, ty, elev):
    """Mapped turbine records owned by this existing road tile (legacy square or road geotile).

    x/y use the project's height-zero world convention; z uses the existing
    graded terrain elevation at the exact mapped point. No slope normal tilts
    the tower, and no elevation is requested for tiles that do not own it.
    """
    import roads
    box = roads.tile_bbox(tx, ty)
    if hasattr(tx, "gx"):
        s, w, n, e = geotiles.info(tx.gx, tx.gy, tx.z)[2:]
        candidates = near(s, w, n, e)
    else:
        x0, y0, x1, y1 = box
        corners = [[x0, y0, 0], [x1, y0, 0], [x0, y1, 0], [x1, y1, 0],
                   [(x0 + x1) / 2, y0, 0], [(x0 + x1) / 2, y1, 0],
                   [x0, (y0 + y1) / 2, 0], [x1, (y0 + y1) / 2, 0]]
        la, lo, _ = geotiles.world_to_geodetic(frame, corners)
        # Longitude unwrapped about the anchor keeps dateline-adjacent tiles local.
        lo = frame.lon0 + (lo - frame.lon0 + 180) % 360 - 180
        candidates = near(float(la.min()) - 1e-7, float((lo.min() + 180) % 360 - 180),
                          float(la.max()) + 1e-7, float((lo.max() + 180) % 360 - 180))
    if not candidates:
        return []
    lat, lon = np.array([t.lat for t in candidates]), np.array([t.lon for t in candidates])
    x, y, _ = geotiles.geodetic_to_world(frame, lat, lon, 0.0)
    keep = np.asarray(roads.box_owns(box, x, y))
    return rows_for_features(frame, [t for t, own in zip(candidates, keep) if own], elev)
