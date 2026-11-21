"""Opt-in near-tile storefronts from ordinary building and place cells."""

import hashlib
import csv
from functools import lru_cache
import json
import logging
import os
from typing import NamedTuple
import dmath
import dnp
import struct
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np

import revisit

from threedmr import STOREFRONT_VERSION
from gasstations import GAS_CATEGORIES
from polygon import contains_points

ROOT = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parents[1])
TOOLS = ROOT / "tools" / "storefronts"
if str(TOOLS) not in sys.path:
    sys.path.append(str(TOOLS))
from build_stores import export_store
from store_match import BRANDS, chain, parking_frontage
from chain_profiles import GROUPS

SPECIFIC = {
    "restaurant",
    "fast_food",
    "cafe",
    "supermarket",
    "pharmacy",
    "department_store",
    "hardware",
}
SPECIFIC.update(kind for _, _, types, _ in GROUPS for kind in types)
COMMERCIAL = SPECIFIC | {"commercial", "retail", "shop"}
LOG = logging.getLogger("earthd.storefronts")


class LocalSign(NamedTuple):
    name: str
    style: str


SIGN_STYLES = {
    "smoke_shop": "tobacco", "tobacco": "tobacco", "tobacco_shop": "tobacco",
    "vape_shop": "tobacco", "car_wash": "wash", "carwash": "wash",
    "fuel": "fuel", "gas_station": "fuel", "fueling_station": "fuel",
    "convenience_store": "market", "grocery_store": "market",
}


def local_sign(label, categories):
    if not isinstance(label, str):
        return None
    name = " ".join(label.split())
    if not 2 <= len(name) <= 80 or not name.isprintable():
        return None
    for category, style in SIGN_STYLES.items():
        if category in categories:
            return LocalSign(name, style)
    if categories & (SPECIFIC | {"shop", "retail"}):
        return LocalSign(name, "shop")
    return None


def approach_panel(ring, coords, facing, doors):
    """Select a broad outward-facing wall, preferring a mapped customer door."""
    edges = np.roll(ring, -1, axis=0) - ring
    lengths = np.linalg.norm(edges, axis=1)
    winding = 1 if np.sum(ring[:,0]*edges[:,1]-ring[:,1]*edges[:,0]) >= 0 else -1
    normals = winding * np.column_stack((edges[:,1], -edges[:,0])) / np.maximum(lengths[:,None], 1e-9)
    score = np.maximum(0, normals @ facing / np.linalg.norm(facing))**2 * np.minimum(lengths, 20)
    if doors:
        scale = np.array([111320*dmath.cos(dmath.radians(coords[0,1])), 111132])
        for door in doors:
            offsets = (door-coords)*scale
            fraction = np.clip(np.sum(offsets*edges, axis=1)/np.maximum(lengths**2, 1e-9), 0, 1)
            score += 30 * (np.linalg.norm(offsets-fraction[:,None]*edges, axis=1) <= 1.5)
    score[lengths < 4] = 0
    wall = int(score.argmax())
    if score[wall] <= 0:
        return None
    panel = coords[[wall, (wall+1) % len(coords)]]
    return panel if winding == 1 else panel[::-1]


def businesses(md, keys):
    """Keep all mapped POIs in the cells so another tenant can rule out a whole-building match."""
    for key in keys:
        f = md.cell(key, layers=("places",), kind="demand").layers.get("places")
        if f is None or not len(f):
            continue
        rows = np.flatnonzero(np.diff(f.part_off) > 0)
        first = f.part_off[rows]
        rows = rows[np.diff(f.ring_off)[first] > 0]
        rings = f.ring_off[f.part_off[rows]]
        rows = rows[np.diff(f.coord_off)[rings] > 0]
        yield f, rows, f.lonlat[f.coord_off[f.ring_off[f.part_off[rows]]]]


def mapped_tenants(layers, ring, anchor=None, local=False):
    lo, hi = ring.min(0), ring.max(0)
    tenants = {}
    for f, rows, points in layers:
        near = np.flatnonzero(np.all((points >= lo) & (points <= hi), axis=1))
        if not len(near):
            continue
        for j in near[contains_points(ring, points[near])]:
            i = rows[j]

            def value(key):
                return f.attrs[key][i] if key in f.attrs else None

            if str(value("operating_status")).lower() in {
                "permanently_closed",
                "closed",
            }:
                continue
            categories = {value("basic_category"), value("taxonomy")}
            name = (
                BRANDS.get(str(value("brand_wikidata")))
                or chain(value("brand_name"))
                or chain(value("name"), "movie_theater" in categories)
            )
            if anchor and not name and (
                "atm" in categories or str(value("name") or "").strip().casefold() in {"atm", "pharmacy"}
            ):
                # Generic services inside an identified store are not separate retail frontages.
                continue
            confidence = value("confidence")
            if categories & GAS_CATEGORIES or "drive_in_theater" in categories:
                name = None
            if local and not name and "drive_in_theater" not in categories:
                name = local_sign(value("name"), categories)
            trusted = confidence is not None and float(confidence) >= 0.8
            tenants[str(f.ids[i])] = (name if trusted else None, points[j])
    return list(tenants.values())


def tenant_panels(tenants, ring, parking):
    """Leave room for all mapped tenants, including businesses without a brand model."""
    for i, (name, point) in enumerate(tenants):
        if not name:
            continue
        panel = parking_frontage(ring, point, parking)
        if panel is None:
            continue
        scale = np.array([111320 * dmath.cos(dmath.radians(point[1])), 111132])
        start, end = panel * scale
        length = dnp.norm(end - start)
        axis = (end - start) / length
        for j, (_, other) in enumerate(tenants):
            if j == i:
                continue
            offset = other * scale - start
            along = np.clip(dnp.dot(offset, axis), 0, length)
            if dnp.norm(offset - along * axis) < 15:
                break
        else:
            yield name, panel


@lru_cache(maxsize=1)
def entrance_index():
    by_way = {}
    try:
        with (Path(__file__).parent / "data" / "storefront_entrances.csv").open(encoding="utf8", newline="") as f:
            for row in csv.DictReader(f):
                by_way.setdefault("w"+row["way"], []).append((float(row["lon"]), float(row["lat"]), int(row["explicit"])))
    except (OSError, KeyError, ValueError) as exc:
        LOG.warning("Mapped entrance data unavailable: %s", exc)
        return {}
    return by_way


def mapped_doors(source, coords):
    """Use only evidence on the same OSM way and still on the current footprint."""
    way = source.partition("@")[0]
    if not way.startswith("w") or not way[1:].isdigit():
        return ()
    rows = entrance_index().get(way, ())
    if not rows:
        return ()
    scale = np.array([111320*dmath.cos(dmath.radians(coords[0,1])), 111132])
    edges = (np.roll(coords, -1, axis=0)-coords)*scale
    length2 = np.maximum(np.sum(edges*edges, axis=1), 1e-12)
    public, paths = [], []
    for lon, lat, explicit in rows:
        offset = (np.array([lon,lat])-coords)*scale
        fraction = np.clip(np.sum(edges*offset, axis=1)/length2, 0, 1)
        residual = offset-edges*fraction[:,None]
        if np.min(np.sum(residual*residual, axis=1)) <= 1.5*1.5:
            (public if explicit else paths).append((lon,lat))
    return tuple(sorted(set(public or paths)))


def cached_entry(cache, name, coords, bearing, material, sign_height=None, doors=(), entrance_bearing=None, local_panel=None):
    # Round away sub-centimetre anchor differences. The key describes the geometry actually exported.
    coords = coords.round(7)
    parameters = [STOREFRONT_VERSION, name, coords.tolist(), bearing, material]
    if sign_height is not None:
        parameters.append(sign_height)
    if doors:
        parameters.append(doors)
    if entrance_bearing is not None:
        parameters.append({"entrance_bearing": entrance_bearing})
    if local_panel:
        parameters.append({"local_panel": local_panel})
    signature = json.dumps(parameters).encode()
    ident = hashlib.sha256(signature).hexdigest()[:24]
    meta = cache / f"store_{ident}.json"
    mesh = cache / f"store_{ident}.glb"
    try:
        entry = json.loads(meta.read_text(encoding="utf8"))
        if (isinstance(entry, dict) and entry.get("file") == mesh.name and entry.get("title") == name
                and entry.get("branded_storefront") is True
                and bool(entry.get("storefront_overlay")) == (bearing is None)
                and bool(entry.get("storefront_pylon")) == (sign_height is not None)
                and all(isinstance(entry.get(k), (int, float)) and dmath.isfinite(entry[k]) for k in ("lat", "lon"))):
            with mesh.open("rb") as stream:
                size = mesh.stat().st_size
                if 28 <= size < 2**32 and stream.read(12) == struct.pack("<4sII", b"glTF", 2, size):
                    return entry
    except (FileNotFoundError, ValueError):
        # A partial pair, truncated download, or damaged manifest can be rebuilt from the map.
        pass
    cache.mkdir(parents=True, exist_ok=True)
    direction = None
    if bearing is not None:
        angle = dmath.radians(bearing)
        direction = np.array([dmath.cos(angle), dmath.sin(angle)])
    match = (name, ident, coords, direction, [(np.array(p), False) for p in doors], material)
    if sign_height is not None:
        match += (sign_height,)
    parking = None
    if entrance_bearing is not None:
        angle = dmath.radians(entrance_bearing)
        parking = np.array([dmath.cos(angle), dmath.sin(angle)])
    entry = export_store(Path("streamed"), match, {}, cache, entrance_direction=parking, **({"local_panel": local_panel} if local_panel else {}))
    with NamedTemporaryFile(mode="w", dir=cache, delete=False) as handle:
        json.dump(entry, handle)
    Path(handle.name).replace(meta)
    return entry


def parking_direction(ring, aisles):
    """Prefer a broad wall facing several nearby parking aisles. Ties keep the road fallback."""
    if len(aisles) < 2:
        return None
    edges = np.roll(ring, -1, axis=0) - ring
    widths = dnp.norm(edges, axis=1)
    axes = edges / np.maximum(widths[:, None], 1e-9)
    normals = np.column_stack((axes[:, 1], -axes[:, 0]))
    if np.sum(ring[:, 0] * edges[:, 1] - ring[:, 1] * edges[:, 0]) < 0:
        normals *= -1
    votes = np.zeros(len(ring), dtype=int)
    for line in aisles:
        offsets = line[:, None, :] - ring
        depth = np.sum(offsets * normals, axis=2)
        along = np.sum(offsets * axes, axis=2)
        valid = (widths >= 18) & (along >= 0) & (along <= widths) & (depth >= 3) & (depth <= 55)
        if valid.any():
            _, wall = np.unravel_index(np.argmin(np.where(valid, depth, np.inf)), depth.shape)
            votes[wall] += 1
    best = int(votes.argmax())
    if votes[best] >= 2 and votes[best] > votes.sum() / 2:
        return normals[best]
    return None


def models(items, existing, frame, origin, roads, md, keys, cache_dir, aisles=()):
    if not roads:
        return
    starts = np.concatenate([line[:-1] for line in roads])
    ends = np.concatenate([line[1:] for line in roads])
    delta = ends - starts
    length2 = np.maximum(np.sum(delta * delta, axis=1), 1e-12)
    if not len(starts):
        return
    layers = None
    local_enabled = os.environ.get("BNE_LOCAL_BUSINESS_SIGNS") == "1"
    parking_points = None
    cache = Path(cache_dir) / "threedmr_cache" / "storefronts"
    for spec, ground_min, ground_max in items:
        if (
            not spec.storefront
            or spec.min_h
            or spec.holes
            or spec.deck
            or ground_max - ground_min > 3
        ):
            continue
        _, label, material, subtype, *sources = spec.storefront
        kind = str(spec.cls or "").lower()
        name = chain(label, kind == "cinema")
        generic_host = kind in {"retail", "commercial", "shop"} or (not kind and subtype == "commercial")
        categories = {kind}
        if subtype == "commercial" and label.casefold().endswith("restaurant"):
            categories.add("restaurant")
        fallback = local_sign(label, categories) if local_enabled else None
        if kind not in COMMERCIAL and not (local_enabled and kind in SIGN_STYLES) and not generic_host and not (name and not kind):
            continue
        if label and not name and not generic_host and not fallback:
            continue
        ring = np.asarray(spec.ring)[:, :2]
        if len(ring) > 96 or np.any(np.ptp(ring, axis=0) > 250):
            continue
        centre = ring.mean(0)
        lower, upper = ring.min(0) + origin - 3, ring.max(0) + origin + 3
        if any(np.all((c["xy"] >= lower) & (c["xy"] <= upper)) for c in existing
               if not (c.get("local") or {}).get("storefront_pylon")):
            continue
        offset = (
            starts
            + np.clip(np.sum((centre - starts) * delta, axis=1) / length2, 0, 1)[
                :, None
            ]
            * delta
            - centre
        )
        facing = offset[np.argmin(np.sum(offset * offset, axis=1))]
        if not 1 <= dnp.norm(facing) <= 150 + np.max(dnp.norm(ring - centre, axis=1)):
            continue
        ll = frame.enu_to_geodetic(
            ring[:, 0] + origin[0], ring[:, 1] + origin[1], np.zeros(len(ring))
        )
        coords = np.column_stack((ll[1], ll[0]))
        try:
            if generic_host or not name:
                if layers is None:
                    layers = list(businesses(md, keys))
                tenants = mapped_tenants(layers, coords, anchor=name, local=local_enabled)
                if name and any(brand != name for brand, _ in tenants):
                    # A retail host may carry its anchor's name but also contain independent stores.
                    name = None
                elif fallback and not name and (not tenants or all(t == fallback for t, _ in tenants)):
                    name = fallback
                elif not name and len(tenants) == 1 and (not label or isinstance(tenants[0][0], LocalSign) and not generic_host):
                    name = tenants[0][0]
                if not name and not (generic_host and tenants and len(aisles)):
                    continue
            doors = mapped_doors(sources[0], coords) if name and sources else ()
            entrance_bearing = None
            if name and not doors and kind in {"retail", "supermarket", "department_store", "hypermarket", "wholesale"}:
                parking = parking_direction(ring, aisles)
                if parking is not None:
                    entrance_bearing = round(dmath.degrees(dmath.atan2(parking[1], parking[0])))
            bearing = round(dmath.degrees(dmath.atan2(facing[1], facing[0])))
            signed_wall = None
            if isinstance(name, LocalSign):
                panel = approach_panel(ring, coords, facing, doors)
                if panel is None:
                    continue
                if spec.retail_front and label and fallback == name:
                    from buildings import retail_bays
                    ends = [int(np.argmin(np.linalg.norm(coords-pt, axis=1))) for pt in panel]
                    wall = ends[0] if (ends[0]+1) % len(ring) == ends[1] else ends[1]
                    length = float(np.linalg.norm(ring[ends[1]]-ring[ends[0]]))
                    margin, bays, pier, opening = retail_bays(length)
                    along = margin + ((bays-1)//2)*(opening+pier) + opening/2
                    delta = (coords[(wall+1) % len(ring)]-coords[wall])/length
                    center = coords[wall]+along*delta
                    half = min(opening*.45, 3.5)*(panel[1]-panel[0])/length
                    panel = np.array([center-half, center+half])
                    signed_wall = wall
                matches = [(name, panel, None, None)]
            elif name:
                matches = [(name, coords, bearing, material)]
            else:
                if parking_points is None:
                    xy = np.concatenate(aisles) + origin
                    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
                    parking_points = np.column_stack((lon, lat))
                matches = ((brand, panel, None, None) for brand, panel in tenant_panels(tenants, coords, parking_points))
            for brand, geometry, heading, finish in matches:
                options = {}
                if isinstance(brand, LocalSign):
                    if spec.height < 2.8:
                        continue
                    options["local_panel"] = (brand.style, round(min(4.2, spec.height - .2), 1))
                    if signed_wall is not None:
                        options["local_panel"] = (brand.style, round(spec.height+.65, 2), "raised")
                    brand = brand.name
                entry = cached_entry(cache, brand, geometry, heading, finish, doors=doors, entrance_bearing=entrance_bearing, **options)
                if signed_wall is not None:
                    spec.front = signed_wall
                local = dict(entry, file="storefronts/" + entry["file"])
                x, y, _ = frame.geodetic_to_enu(entry["lat"], entry["lon"], 0.0)
                yield {
                    "id": "auto_" + Path(entry["file"]).stem,
                    "xy": (float(x), float(y)),
                    "latlon": (entry["lat"], entry["lon"]),
                    "local": local,
                    "source": "local",
                    "storefront_host": tuple(centre + origin) if heading is not None else None,
                }
        except (OSError, ValueError, RuntimeError) as error:
            if isinstance(error, OSError):
                revisit.fault()
            LOG.warning(
                "Could not generate storefront for %s: %s", spec.storefront[0], error
            )


def pylons(keys, frame, box, streets, existing, cache_dir):
    import storefront_totems
    cache = Path(cache_dir) / "threedmr_cache" / "storefronts"
    for node, name, coords, height, xy, latlon in storefront_totems.matches(keys, frame, box, streets, existing):
        try:
            entry = cached_entry(cache, name, coords, None, None, sign_height=height)
        except (OSError, ValueError, RuntimeError) as error:
            if isinstance(error, OSError):
                revisit.fault()
            LOG.warning("Could not generate mapped storefront sign %s: %s", node, error)
            continue
        yield {"id": "osm_storefront_totem_" + node, "xy": xy, "latlon": latlon,
               "local": dict(entry, file="storefronts/" + entry["file"]), "source": "local"}
