"""Opt-in near-tile storefronts from ordinary building and place cells."""

import hashlib
import csv
from functools import lru_cache
import json
import logging
import re
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


MALL_CLASSES = {"mall", "shopping_mall", "shopping_center", "outlet_mall"}

MIN_BUSINESS_CONFIDENCE = 0.75


def qualifies_business(confidence):
    return confidence is not None and MIN_BUSINESS_CONFIDENCE < float(confidence) <= 1.0


class LocalSign(NamedTuple):
    name: str
    style: str


SIGN_STYLES = {
    "smoke_shop": "tobacco", "tobacco": "tobacco", "tobacco_shop": "tobacco",
    "bank": "bank", "bank_or_credit_union": "bank", "credit_union": "bank", "financial_service": "bank",
    "bar": "venue", "pub": "venue", "music_venue": "venue", "nightlife_venue": "venue",
    "nightclub": "venue", "dance_club": "venue", "bar_and_grill_restaurant": "venue",
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
    if categories & (SPECIFIC | {"shop", "retail", "sporting_goods_store"}):
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


def named_frontage(layers, coords, label):
    """Corroborate a named footprint with a trading name inside it or just across its wall."""
    def words(name):
        return re.findall(r"[^\W_]+", str(name or "").casefold())

    key = words(label)
    if not key:
        return None
    scale = np.array([111320*dmath.cos(dmath.radians(coords[0,1])), 111132])
    pad = 8 / scale
    lo, hi = coords.min(0)-pad, coords.max(0)+pad
    edges = (np.roll(coords,-1,axis=0)-coords)*scale
    lengths2 = np.maximum(np.sum(edges*edges,axis=1), 1e-9)
    for features, rows, points in layers:
        for j in np.flatnonzero(np.all((points >= lo) & (points <= hi),axis=1)):
            i = rows[j]
            def value(field):
                return features.attrs[field][i] if field in features.attrs else None
            if (words(value("name")) != key or not qualifies_business(value("confidence"))
                    or value("operating_status") in {"closed", "permanently_closed"}):
                continue
            sign = local_sign(value("name"), {value("basic_category"),value("taxonomy")})
            if not sign:
                continue
            offsets = (points[j]-coords)*scale
            fractions = np.clip(np.sum(offsets*edges,axis=1)/lengths2,0,1)
            if (contains_points(coords,points[j:j+1])[0]
                    or np.min(np.linalg.norm(offsets-fractions[:,None]*edges,axis=1)) <= 8):
                return sign
    return None


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
            trusted = qualifies_business(confidence)
            tenants[str(f.ids[i])] = (name if trusted else None, points[j])
    return list(tenants.values())


SUITE_ADDRESS = re.compile(r"^([0-9]+[a-z]?\s+.+?)[ ,]+(?:suite|ste\.?|unit|#)\s*([a-z0-9-]+)$", re.I)
STREET_SUFFIX = {"drive":"dr", "street":"st", "avenue":"ave", "road":"rd",
                 "boulevard":"blvd", "lane":"ln", "court":"ct"}


def suite_directory(layers, center, anchor):
    """List known ground-level retail tenants at one numbered address. Suite IDs are labels only."""
    scale = np.array([111320*dmath.cos(dmath.radians(center[1])), 111132])
    anchors, entries = set(), {}
    for feats, rows, points in layers:
        attrs = feats.attrs
        if not {"street_address","name","confidence"} <= attrs.keys():
            continue
        for j in np.flatnonzero(np.all(np.abs((points-center)*scale) <= 50,axis=1)):
            i = rows[j]
            def value(key):
                return attrs[key][i] if key in attrs else None
            addr = SUITE_ADDRESS.fullmatch(str(value("street_address") or "").strip())
            if (not addr or not qualifies_business(value("confidence"))
                    or value("operating_status") in {"closed","permanently_closed"}):
                continue
            name = str(value("name") or "").strip()
            if not name or len(name)>80 or not name.isprintable():
                continue
            words = addr[1].casefold().replace(".","").split()
            words[-1] = STREET_SUFFIX.get(words[-1], words[-1])
            key = tuple(words)
            unit = addr[2].upper()
            if name.casefold() == anchor.casefold() and np.linalg.norm((points[j]-center)*scale)<=15:
                anchors.add((key,unit))
            elif not local_sign(name,{value("basic_category"),value("taxonomy")}):
                continue
            entries.setdefault(key,{}).setdefault(unit,{})[name.casefold()] = name
    if len(anchors)!=1:
        return ()
    key,unit = next(iter(anchors))
    tenants = entries[key]
    if len(tenants[unit])!=1:
        return ()
    rows = tuple((u,next(iter(names.values()))) for u,names in sorted(tenants.items()) if len(names)==1)
    return rows if 2 <= len(rows) <= 8 else ()


def exterior_frontages(layers, coords, ring, label, neighbors, inside_only=False):
    """A high-confidence shop pin just outside one unambiguous wall can identify a tenant bay."""
    from buildings import retail_bays
    scale = np.array([111320*dmath.cos(dmath.radians(coords[0,1])), 111132])
    pad = 3.25/np.maximum(scale, 1.)
    lo, hi = coords.min(0)-pad, coords.max(0)+pad
    edges = np.roll(ring,-1,axis=0)-ring
    widths = np.linalg.norm(edges,axis=1)
    axes = edges / np.maximum(widths[:,None],1e-9)
    bays = {}
    for feats, rows, points in layers:
        near = np.flatnonzero(np.all((points >= lo) & (points <= hi),axis=1))
        for j in near:
            i = rows[j]
            def attr(key):
                return feats.attrs[key][i] if key in feats.attrs else None
            if not qualifies_business(attr("confidence")) or attr("operating_status") in {"permanently_closed","closed"}:
                continue
            sign = local_sign(attr("name"), {attr("basic_category"),attr("taxonomy")})
            if not sign:
                continue
            if label:
                # The building label may include a trademark and a descriptive suffix.
                base = label.replace("\u2122", "").replace("\u00ae", "").casefold()
                trading = sign.name.casefold()
                if base != trading and not (base.startswith(trading) and base[len(trading):].lstrip().startswith(("-", "\u2013", "\u2014"))):
                    continue
            point = ring[0]+(points[j]-coords[0])*scale
            offsets = point-ring
            along = np.clip(np.sum(offsets*axes,axis=1),0,widths)
            distances = np.linalg.norm(offsets-along[:,None]*axes,axis=1)
            wall, second = distances.argsort()[:2]
            if distances[wall]>(7.0 if inside_only else 3.25) or distances[second]<distances[wall]+1.5 or widths[wall]<5:
                continue
            if bool(contains_points(ring,point[None])[0]) != inside_only:
                continue
            # Reject nearby competing footprints, including ones that enclose the pin.
            if any(np.all(point >= lower) and np.all(point <= upper)
                   for lower,upper in neighbors):
                continue
            margin,count,pier,opening = retail_bays(widths[wall])
            bay = int(np.clip(round((along[wall]-margin-opening/2)/(opening+pier)),0,count-1))
            center = margin+bay*(opening+pier)+opening/2
            delta = (coords[(wall+1)%len(ring)]-coords[wall])/widths[wall]
            midpoint = coords[wall]+center*delta
            half = min(3.5,opening*.45)*delta
            bays.setdefault((wall,bay), {})[sign.name.casefold()] = (sign,np.array([midpoint-half,midpoint+half]),wall,center)
    return [next(iter(matches.values())) for matches in bays.values() if len(matches)==1]


def tenant_panels(tenants, ring, parking, entrances=(), walks=()):
    """Leave room for all mapped tenants, including businesses without a brand model."""
    for i, (name, point) in enumerate(tenants):
        if not name:
            continue
        approaches = []
        if len(walks):
            # A long straight promenade may have no vertices beside this tenant.
            scale = np.array([111320*dmath.cos(dmath.radians(point[1])), 111132])
            start = (walks[:,0]-point)*scale
            delta = (walks[:,1]-walks[:,0])*scale
            along = np.clip(-np.sum(start*delta,axis=1)/np.maximum(np.sum(delta*delta,axis=1),1e-12),0,1)
            near = start+along[:,None]*delta
            candidates = point + near[np.sum(near*near,axis=1) <= 32**2]/scale
            approaches = candidates[~contains_points(ring,candidates)] if len(candidates) else []
        panel = parking_frontage(ring, point, parking, entrances=entrances, walks=approaches)
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


def entrance_offsets(coords, ring, points):
    """Place each observation on its closest wall, in metres from the wall start."""
    offsets = {}
    if not points:
        return offsets
    scale = np.array([111320*dmath.cos(dmath.radians(coords[0,1])), 111132])
    edges = (np.roll(coords,-1,axis=0)-coords)*scale
    squared = np.maximum(np.sum(edges*edges,axis=1), 1e-12)
    lengths = np.linalg.norm(np.roll(ring,-1,axis=0)-ring, axis=1)
    for point in points:
        delta = (point-coords)*scale
        along = np.clip(np.sum(delta*edges,axis=1)/squared,0,1)
        wall = int(np.argmin(np.sum((delta-along[:,None]*edges)**2,axis=1)))
        offsets.setdefault(wall, []).append(float(along[wall]*lengths[wall]))
    return offsets


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


def models(items, existing, frame, origin, roads, md, keys, cache_dir, aisles=(), load_walks=None):
    starts = np.concatenate([line[:-1] for line in roads]) if roads else np.empty((0,2))
    ends = np.concatenate([line[1:] for line in roads]) if roads else np.empty((0,2))
    delta = ends - starts
    length2 = np.maximum(np.sum(delta * delta, axis=1), 1e-12)
    layers = None
    local_enabled = os.environ.get("BNE_LOCAL_BUSINESS_SIGNS") == "1"
    parking_points = None
    walk_segments = None
    cache = Path(cache_dir) / "threedmr_cache" / "storefronts"
    if local_enabled:
        lower_bounds = np.array([host.ring.min(0)[:2]-3.25 for host,_,_ in items])
        upper_bounds = np.array([host.ring.max(0)[:2]+3.25 for host,_,_ in items])
    for item_index,(spec, ground_min, ground_max) in enumerate(items):
        if (
            not spec.storefront
            or spec.min_h
            or spec.holes
            or spec.deck
            or ground_max - ground_min > 3
        ):
            continue
        _, label, material, subtype, *sources = spec.storefront
        venue_only = len(sources) > 1 and sources[1]
        if venue_only and not local_enabled:
            continue
        kind = str(spec.cls or "").lower()
        name = chain(label, kind == "cinema")
        mall = kind in MALL_CLASSES or (kind in {"", "commercial", "retail", "shop"} and label.casefold().endswith((" mall", " outlets")))
        generic_host = mall or kind in {"retail", "commercial", "shop"} or (not kind and subtype == "commercial")
        categories = {kind}
        if subtype == "commercial" and label.casefold().endswith("restaurant"):
            categories.add("restaurant")
        fallback = local_sign(label, categories) if local_enabled else None
        # Some commercial footprints carry only a name or a surveyed low flat roof.
        # Probe place records before accepting these hosts; an untyped outline alone proves nothing.
        probe = local_enabled and kind in {"", "office", "commercial"} and (
            bool(label) or (spec.roof == "flat" and 2.8 <= spec.height <= 7 and not spec.fr_house))
        if not venue_only and not probe and kind not in COMMERCIAL and not (local_enabled and kind in SIGN_STYLES) and not generic_host and not spec.retail_front and not (name and not kind):
            continue
        if not venue_only and label and not name and not generic_host and not fallback and not probe:
            continue
        ring = np.asarray(spec.ring)[:, :2]
        if len(ring) > 96 or np.any(np.ptp(ring, axis=0) > 250):
            continue
        centre = ring.mean(0)
        lower, upper = ring.min(0) + origin - 3, ring.max(0) + origin + 3
        if any(np.all((c["xy"] >= lower) & (c["xy"] <= upper)) for c in existing
               if not (c.get("local") or {}).get("storefront_pylon")):
            continue
        facing = None
        if not mall:
            if not len(starts):
                continue
            closest = np.clip(np.sum((centre-starts)*delta,axis=1)/length2,0,1)
            offset = starts+closest[:,None]*delta-centre
            facing = offset[np.argmin(np.sum(offset*offset,axis=1))]
            if not 1 <= dnp.norm(facing) <= 150 + np.max(dnp.norm(ring-centre,axis=1)):
                continue
        ll = frame.enu_to_geodetic(
            ring[:, 0] + origin[0], ring[:, 1] + origin[1], np.zeros(len(ring))
        )
        coords = np.column_stack((ll[1], ll[0]))
        try:
            exterior = []
            doors = mapped_doors(sources[0], coords) if sources and (spec.retail_front or name or mall) else ()
            if spec.retail_front and doors:
                spec.retail_doors = entrance_offsets(coords, ring, doors)
            confirmed = None
            if local_enabled and label and not mall and not name and kind in {"commercial", "retail", "shop"}:
                if layers is None:
                    layers = list(businesses(md, keys))
                confirmed = named_frontage(layers, coords, label)
            if confirmed:
                name = confirmed
            elif venue_only:
                if mall or kind not in {"", "commercial", "retail", "shop", "bar", "pub", "music_venue"}:
                    continue
                if layers is None:
                    layers = list(businesses(md, keys))
                candidates = {t for t, _ in mapped_tenants(layers, coords, local=True)
                              if isinstance(t, LocalSign) and t.style == "venue"
                              and (not label or t.name.casefold() == label.casefold())}
                if len(candidates) != 1:
                    continue
                name = candidates.pop()
            elif generic_host or not name:
                if layers is None:
                    layers = list(businesses(md, keys))
                tenants = mapped_tenants(layers, coords, anchor=name, local=local_enabled)
                if probe and not generic_host and not spec.retail_front and not name:
                    if label:
                        matches_label = [t for t,_ in tenants if isinstance(t,LocalSign)
                                         and t.name.casefold() == label.casefold()]
                        if len(matches_label) != 1 or len(tenants) != 1:
                            continue
                        fallback = matches_label[0]
                    elif len(tenants)==1 and isinstance(tenants[0][0],LocalSign) and tenants[0][0].style in {"bank","wash"}:
                        name = tenants[0][0]
                    else:
                        distinct = {t for t,_ in tenants if t}
                        if spec.windows is not False or len(distinct)<2:
                            continue
                        exterior = exterior_frontages(layers,coords,ring,"",(),inside_only=True)
                        if not exterior:
                            continue
                        spec.retail_front = spec.shop = True
                        spec.plain = False
                        spec.floors, spec.floor_h = 1, spec.height
                if spec.retail_front and local_enabled and not name and not doors and not tenants:
                    nearby = (np.all(upper_bounds >= ring.min(0)-3.25,axis=1)
                              & np.all(lower_bounds <= ring.max(0)+3.25,axis=1))
                    nearby[item_index] = False
                    neighbors = list(zip(lower_bounds[nearby],upper_bounds[nearby]))
                    exterior = exterior_frontages(layers,coords,ring,label,neighbors)
                if mall:
                    name = None  # A single mapped tenant can still occupy just one wing.
                elif name and any(brand != name for brand, _ in tenants):
                    # A retail host may carry its anchor's name but also contain independent stores.
                    name = None
                elif fallback and not name and (not tenants or all(t == fallback for t, _ in tenants)):
                    name = fallback
                elif not name and len(tenants) == 1 and (not label or isinstance(tenants[0][0], LocalSign) and not generic_host):
                    name = tenants[0][0]
                if not name and not exterior and not (generic_host and tenants and (len(aisles) or mall and (doors or load_walks))):
                    continue
            if name and sources and not doors and not spec.retail_front:
                doors = mapped_doors(sources[0], coords)
            entrance_bearing = None
            if name and not doors and kind in {"retail", "supermarket", "department_store", "hypermarket", "wholesale"}:
                parking = parking_direction(ring, aisles)
                if parking is not None:
                    entrance_bearing = round(dmath.degrees(dmath.atan2(parking[1], parking[0])))
            bearing = round(dmath.degrees(dmath.atan2(facing[1], facing[0]))) if facing is not None else None
            signed_wall = None
            if exterior:
                matches = [(sign,panel,None,None) for sign,panel,_,_ in exterior]
            elif isinstance(name, LocalSign):
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
                    observed = (spec.retail_doors or {}).get(wall)
                    if observed:
                        along = float(np.clip(min(observed, key=lambda x: abs(x-along)), opening*.45, length-opening*.45))
                    wall_step = (coords[(wall+1) % len(ring)]-coords[wall])/length
                    center = coords[wall]+along*wall_step
                    half = min(opening*.45, 3.5)*(panel[1]-panel[0])/length
                    panel = np.array([center-half, center+half])
                    signed_wall = wall
                matches = [(name, panel, None, None)]
            elif name:
                matches = [(name, coords, bearing, material)]
            else:
                if parking_points is None and len(aisles):
                    xy = np.concatenate(aisles) + origin
                    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
                    parking_points = np.column_stack((lon, lat))
                if mall and load_walks and walk_segments is None:
                    lines = [line for line in load_walks() if len(line)>1]
                    walk_segments = []
                    if lines:
                        xy = np.concatenate([np.stack((l[:-1],l[1:]),axis=1) for l in lines]).reshape(-1,2)+origin
                        lat,lon,_ = frame.enu_to_geodetic(xy[:,0],xy[:,1],np.zeros(len(xy)))
                        walk_segments = np.column_stack((lon,lat)).reshape(-1,2,2)
                panels = list(tenant_panels(tenants,coords,parking_points if parking_points is not None else (),
                                            doors if mall else (), walk_segments if mall and walk_segments is not None else ()))
                if spec.retail_front and not spec.retail_doors:
                    centers = [panel.mean(0) for _,panel in panels]
                    spec.retail_doors = entrance_offsets(coords, ring, centers)
                matches = ((brand, panel, None, None) for brand, panel in panels)
            for index,(brand, geometry, heading, finish) in enumerate(matches):
                options = {}
                if isinstance(brand, LocalSign):
                    if spec.height < 2.8:
                        continue
                    options["local_panel"] = (brand.style, round(min(4.2, spec.height - .2), 1))
                    if brand.style != "venue" and (signed_wall is not None or exterior):
                        options["local_panel"] = (brand.style, round(spec.height+.65, 2), "raised")
                    if brand.style != "venue" and (signed_wall is not None or exterior) and layers:
                        directory = suite_directory(layers,geometry.mean(0),brand.name)
                        if directory:
                            options["local_panel"] += (directory,)
                    brand = brand.name
                if mall:
                    # An exterior sign is justified by the wall evidence, independently of the brand.
                    style = options.get("local_panel", ("brand",))[0]
                    options["local_panel"] = ("mall_"+style, round(min(spec.height-.45,9.),2))
                entry = cached_entry(cache, brand, geometry, heading, finish, doors=doors, entrance_bearing=entrance_bearing, **options)
                if signed_wall is not None:
                    spec.front = signed_wall
                if exterior:
                    _,_,wall,along = exterior[index]
                    if index == 0:
                        spec.retail_doors = {}
                    spec.retail_doors.setdefault(wall,[]).append(along)
                    spec.front = wall
                local = dict(entry, file="storefronts/" + entry["file"])
                x, y, _ = frame.geodetic_to_enu(entry["lat"], entry["lon"], 0.0)
                yield {
                    "id": "auto_" + Path(entry["file"]).stem,
                    "xy": (float(x), float(y)),
                    "latlon": (entry["lat"], entry["lon"]),
                    "local": local,
                    "source": "local",
                    "storefront_ground": ground_max if heading is None else None,
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
