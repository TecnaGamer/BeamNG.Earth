"""Opt-in near-tile storefronts from ordinary building and place cells."""

import hashlib
import json
import logging
import math
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np

from polygon import contains_points

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "storefronts"
if str(TOOLS) not in sys.path:
    sys.path.append(str(TOOLS))
from build_stores import export_store
from store_match import BRANDS, CHAIN_GROUPS

NAMES = {name.casefold(): name for _, names in CHAIN_GROUPS for name in names}
NAMES.update(
    {
        "walmart supercenter": "Walmart",
        "walmart neighborhood market": "Walmart",
        "cvs pharmacy": "CVS",
        "lowe's home improvement": "Lowe's",
        "home depot": "The Home Depot",
        "costco wholesale": "Costco",
        "publix super market": "Publix",
    }
)
SPECIFIC = {
    "restaurant",
    "fast_food",
    "cafe",
    "supermarket",
    "pharmacy",
    "department_store",
    "hardware",
}
COMMERCIAL = SPECIFIC | {"commercial", "retail", "shop"}
VERSION = 3
LOG = logging.getLogger("earthd.storefronts")


def chain(value):
    return NAMES.get(str(value or "").strip().replace("\u2019", "'").casefold())


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


def sole_brand(layers, ring):
    lo, hi = ring.min(0), ring.max(0)
    tenants = {}
    for f, rows, points in layers:
        near = np.flatnonzero(np.all((points >= lo) & (points <= hi), axis=1))
        if not len(near):
            continue
        for i in rows[near[contains_points(ring, points[near])]]:

            def value(key):
                return f.attrs[key][i] if key in f.attrs else None

            if str(value("operating_status")).lower() in {
                "permanently_closed",
                "closed",
            }:
                continue
            name = (
                BRANDS.get(str(value("brand_wikidata")))
                or chain(value("brand_name"))
                or chain(value("name"))
            )
            confidence = value("confidence")
            tenants[str(f.ids[i])] = (
                name if confidence is not None and float(confidence) >= 0.8 else None
            )
            if len(tenants) > 1:
                return None
    return next(iter(tenants.values()), None)


def cached_entry(cache, name, coords, bearing, material):
    # Round away sub-centimetre anchor differences. The key describes the geometry actually exported.
    coords = coords.round(7)
    signature = json.dumps([VERSION, name, coords.tolist(), bearing, material]).encode()
    ident = hashlib.sha256(signature).hexdigest()[:24]
    meta = cache / f"store_{ident}.json"
    mesh = cache / f"store_{ident}.glb"
    if meta.is_file() and mesh.is_file():
        return json.loads(meta.read_text())
    cache.mkdir(parents=True, exist_ok=True)
    angle = math.radians(bearing)
    direction = np.array([math.cos(angle), math.sin(angle)])
    match = (name, ident, coords, direction, [], material)
    entry = export_store(Path("streamed"), match, {}, cache)
    with NamedTemporaryFile(mode="w", dir=cache, delete=False) as handle:
        json.dump(entry, handle)
    Path(handle.name).replace(meta)
    return entry


def models(items, existing, frame, origin, roads, md, keys, cache_dir):
    if not roads:
        return
    starts = np.concatenate([line[:-1] for line in roads])
    ends = np.concatenate([line[1:] for line in roads])
    delta = ends - starts
    length2 = np.maximum(np.sum(delta * delta, axis=1), 1e-12)
    if not len(starts):
        return
    layers = None
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
        _, label, material, subtype = spec.storefront
        kind = str(spec.cls or "").lower()
        name = chain(label)
        if label:
            if not name or (kind and kind not in COMMERCIAL):
                continue
        elif kind not in COMMERCIAL and not (not kind and subtype == "commercial"):
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
        if not 1 <= np.linalg.norm(facing) <= 150 + np.max(np.linalg.norm(ring - centre, axis=1)):
            continue
        ll = frame.enu_to_geodetic(
            ring[:, 0] + origin[0], ring[:, 1] + origin[1], np.zeros(len(ring))
        )
        coords = np.column_stack((ll[1], ll[0]))
        try:
            if not name:
                if layers is None:
                    layers = list(businesses(md, keys))
                name = sole_brand(layers, coords)
            if not name:
                continue
            bearing = round(math.degrees(math.atan2(facing[1], facing[0])))
            entry = cached_entry(cache, name, coords, bearing, material)
            local = dict(entry, file="storefronts/" + entry["file"])
            x, y, _ = frame.geodetic_to_enu(entry["lat"], entry["lon"], 0.0)
            yield {
                "id": "auto_" + Path(entry["file"]).stem,
                "xy": (float(x), float(y)),
                "latlon": (entry["lat"], entry["lon"]),
                "local": local,
                "source": "local",
                "storefront_host": tuple(centre + origin),
            }
        except (OSError, ValueError, RuntimeError) as error:
            LOG.warning(
                "Could not generate storefront for %s: %s", spec.storefront[0], error
            )
