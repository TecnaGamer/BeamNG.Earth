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
from signplacement import distance as distance_to_wall

ROOT = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parents[1])
TOOLS = ROOT / "tools" / "storefronts"
if str(TOOLS) not in sys.path:
    sys.path.append(str(TOOLS))
from build_stores import export_store
from storeidentity import (CATALOG_DIGEST, CATEGORY_INDEX, CATEGORY_RULES, CHAIN_GROUPS, DINING_STYLES,
                           LocalSign, category_family, identity_for, source_key)
from store_match import BRANDS, MAX_TENANT_FRONTAGE_DEPTH_M, chain, parking_frontage

SPECIFIC = {
    "restaurant",
    "fast_food",
    "cafe",
    "supermarket",
    "pharmacy",
    "department_store",
    "hardware",
}
SPECIFIC.update(kind for (_, types), _ in CHAIN_GROUPS for kind in types)
PHARMACY_SOURCES = {name for (tag, kinds), names in CHAIN_GROUPS
                    if tag == "amenity" and "pharmacy" in kinds for name in names}
COMMERCIAL = SPECIFIC | {"commercial", "retail", "shop"}
LOG = logging.getLogger("earthd.storefronts")


MALL_CLASSES = {"mall", "shopping_mall", "shopping_center", "outlet_mall"}

MIN_BUSINESS_CONFIDENCE = 0.75


WALL_PIN_RADIUS_M = 3.25
AMBIGUITY_MARGIN_M = 1.5


def metres_per_degree(latitude):
    return np.array([111320 * dmath.cos(dmath.radians(latitude)), 111132])


def qualifies_business(confidence):
    return confidence is not None and MIN_BUSINESS_CONFIDENCE < float(confidence) <= 1.0


SIGN_STYLES = {
    "smoke_shop": "tobacco", "tobacco": "tobacco", "tobacco_shop": "tobacco",
    "bank": "bank", "bank_or_credit_union": "bank", "credit_union": "bank",
    "bar": "venue", "pub": "venue", "music_venue": "venue", "nightlife_venue": "venue",
    "nightclub": "venue", "dance_club": "venue", "bar_and_grill_restaurant": "venue",
    "vape_shop": "tobacco", "car_wash": "wash", "carwash": "wash",
    "fuel": "fuel", "gas_station": "fuel", "fueling_station": "fuel",
    "convenience_store": "convenience",
}


def local_sign(label, categories):
    if not isinstance(label, str):
        return None
    name = " ".join(label.split())
    if not 2 <= len(name) <= 80 or not name.isprintable():
        return None
    if "atm" in categories or name.casefold().endswith(" atm"):
        return None
    for category, style in SIGN_STYLES.items():
        if category in categories:
            return LocalSign(name, style)
    family = category_family(categories)
    if family:
        return LocalSign(name, family)
    if categories & (SPECIFIC | {"shop", "retail"}) or any(
            isinstance(category, str) and category.endswith("_store") for category in categories):
        return LocalSign(name, "shop")
    return None


def approach_panel(ring, coords, facing, doors, *, neighbors=(), street_offsets=()):
    """Select a broad outward-facing wall, preferring a mapped customer door."""
    edges = np.roll(ring, -1, axis=0) - ring
    lengths = dnp.norm(edges, axis=1)
    winding = 1 if np.sum(ring[:,0]*edges[:,1]-ring[:,1]*edges[:,0]) >= 0 else -1
    normals = winding * np.column_stack((edges[:,1], -edges[:,0])) / np.maximum(lengths[:,None], 1e-9)
    score = dnp.sq(np.maximum(0, dnp.matmul(normals, facing) / dnp.norm(facing))) * np.minimum(lengths, 20)
    if doors:
        scale = metres_per_degree(coords[0,1])
        for door in doors:
            offsets = (door-coords)*scale
            fraction = np.clip(np.sum(offsets*edges, axis=1)/np.maximum(dnp.sq(lengths), 1e-9), 0, 1)
            score += 30 * (dnp.norm(offsets-fraction[:,None]*edges, axis=1) <= 1.5)
    score[lengths < 4] = 0
    wall = int(score.argmax())
    if len(neighbors):
        midpoints = (ring + np.roll(ring,-1,axis=0)) / 2
        probes = midpoints + normals * 2
        blocked = np.zeros(len(ring),dtype=bool)
        for neighbor in neighbors:
            blocked |= contains_points(neighbor,probes)
        if blocked[wall]:
            # The nearest street can be on the far side of the next building.
            # Prefer another exposed facade facing a nearby mapped approach.
            for offset in street_offsets:
                distance = dnp.norm(offset)
                if 1 <= distance <= 80:
                    alignment = np.maximum(0, dnp.matmul(normals,offset) / distance)
                    score = np.maximum(score, dnp.sq(alignment)*np.minimum(lengths,20)/(1+distance/20))
            score[blocked | (lengths < 4)] = 0
            wall = int(score.argmax())
    if score[wall] <= 0:
        return None
    panel = coords[[wall, (wall+1) % len(coords)]]
    return panel if winding == 1 else panel[::-1]


class TradingPlace(NamedTuple):
    name: str
    categories: frozenset
    point: np.ndarray
    address: str
    confidence: float = 0.0


def trading_places(layers, lo, hi):
    """Business evidence shared by frontage matching and suite directories."""
    for features, rows, points in layers:
        attrs = features.attrs
        if "name" not in attrs or "confidence" not in attrs:
            continue
        near = np.flatnonzero(np.all((points >= lo) & (points <= hi), axis=1))
        for j in near:
            i = rows[j]
            if not qualifies_business(attrs["confidence"][i]):
                continue
            if "operating_status" in attrs and str(attrs["operating_status"][i]).casefold() in {"closed", "permanently_closed"}:
                continue
            categories = frozenset(attrs[key][i] for key in ("basic_category", "taxonomy") if key in attrs)
            address = attrs["street_address"][i] if "street_address" in attrs else ""
            yield TradingPlace(str(attrs["name"][i] or "").strip(), categories, points[j], str(address or ""), float(attrs["confidence"][i]))


def mapped_mall(layers, coords, label, *, multipart=False):
    """A mall POI must corroborate the host name as well as lie within the footprint."""
    words = " ".join(re.findall(r"[^\W_]+", label.casefold()))
    if not words:
        return False
    lo, hi = coords.min(0), coords.max(0)
    scale = metres_per_degree(coords[0, 1])
    slack = 8. / scale if multipart else 0.
    for place in trading_places(layers, lo - slack, hi + slack):
        if not place.categories & MALL_CLASSES:
            continue
        title = " ".join(re.findall(r"[^\W_]+", place.name.casefold()))
        if (f" {words} " in f" {title} "
                and contains_points(coords, place.point[None])[0]):
            return sorted(place.categories & MALL_CLASSES)[0]
        if multipart and words == title and place.confidence >= .90:
            # A high-confidence entrance-style mall pin may be just outside a notch in the shell.
            p = (coords - place.point) * scale
            e = np.roll(p, -1, axis=0) - p
            f = np.clip(-np.sum(p * e, axis=1) / np.maximum(np.sum(e * e, axis=1), 1e-12), 0., 1.)
            if np.any(np.sum(dnp.sq(p + f[:, None] * e), axis=1) <= 64.):
                return sorted(place.categories & MALL_CLASSES)[0]
    return False


def unnamed_shopping_center(layers, coords):
    """Identify one center and independent retail tenants inside the same outline."""
    centers = {}
    for place in trading_places(layers, coords.min(0), coords.max(0)):
        if (place.categories & MALL_CLASSES and place.name and place.confidence >= .90
                and contains_points(coords, place.point[None])[0]):
            key = place.name.casefold()
            centers[key] = min(centers.get(key, place.name), place.name)
    if len(centers) != 1:
        return None

    candidates = []
    mapped_tenants(layers, coords, local=True, retail_candidates=candidates)
    names = set()
    pins = set()
    for name, point, _ in candidates:
        if name.casefold() not in centers:
            names.add(name.casefold())
            pins.add(tuple(point))
    if len(names) >= 2 and len(pins) >= 2:
        return next(iter(centers.values()))
    return None


def mall_entrance_panels(coords, walks, occupied):
    """Select separated entrances where mapped approaches meet broad exterior walls."""
    if not len(walks):
        return []
    scale = metres_per_degree(coords[0,1])
    lo, hi = coords.min(0)-2/scale, coords.max(0)+2/scale
    walks = np.asarray(walks)
    near = np.any(np.all((walks >= lo) & (walks <= hi),axis=2),axis=1)
    candidates = []
    approaches = np.concatenate((walks[near],walks[near,::-1]))
    doors = approaches[:,0]
    vectors = approaches[:,1]-doors
    lengths = dnp.norm(vectors*scale,axis=1)
    outside = doors+vectors*np.minimum(1.,8./np.maximum(lengths,1e-9))[:,None]
    valid = (lengths>=2) & ~contains_points(coords,outside)
    # An entrance panel has to be within two metres of the door. Reject distant walkway
    # endpoints in batches before doing the full wall and sight-line calculation.
    walls = (coords-coords[0])*scale
    ends = np.roll(walls,-1,axis=0)
    low,high = np.minimum(walls,ends)-2,np.maximum(walls,ends)+2
    for batch in np.array_split(np.flatnonzero(valid), max(1,(int(valid.sum())+63)//64)):
        xy = ((doors[batch]-coords[0])*scale)[:,None,:]
        close = np.any(np.all((xy>=low)&(xy<=high),axis=2),axis=1)
        for j in batch[close]:
            panel = parking_frontage(coords,doors[j],(),walks=[outside[j]])
            if panel is None:
                continue
            center = panel.mean(0)
            if dnp.norm((center-doors[j])*scale)>2:
                continue
            if any(dnp.norm((center-p.mean(0))*scale)<22 for _,p in occupied):
                continue
            candidates.append((dnp.norm((panel[1]-panel[0])*scale),center,panel))
    selected = []
    for _,center,panel in sorted(candidates,key=lambda c: (-c[0], *c[1])):
        if all(dnp.norm((center-p.mean(0))*scale) >= 55 for p in selected):
            selected.append(panel)
        if len(selected) == 4:
            break
    return selected


def mall_directory(tenants, candidates=(), host=""):
    """Alphabetical directory of distinct qualified businesses associated with this footprint."""
    names = {}
    def include(name):
        if isinstance(name, LocalSign):
            name = name.name
        if isinstance(name, str):
            name = " ".join(name.split())
            if name and name.casefold() != host.casefold():
                names.setdefault(name.casefold(), name)
    for name, _ in tenants:
        include(name)
    for name, _, _ in candidates:
        include(name)
    return tuple(names[key] for key in sorted(names))


def named_frontage(layers, coords, label):
    """Corroborate a named footprint with a trading name inside it or just across its wall."""
    def words(name):
        return re.findall(r"[^\W_]+", str(name or "").casefold())

    key = words(label)
    if not key:
        return None
    scale = metres_per_degree(coords[0,1])
    pad = 8 / scale
    lo, hi = coords.min(0)-pad, coords.max(0)+pad
    edges = (np.roll(coords,-1,axis=0)-coords)*scale
    lengths2 = np.maximum(np.sum(edges*edges,axis=1), 1e-9)
    for place in trading_places(layers, lo, hi):
        if words(place.name) != key:
            continue
        sign = local_sign(place.name, place.categories)
        if not sign:
            continue
        offsets = (place.point-coords)*scale
        fractions = np.clip(np.sum(offsets*edges,axis=1)/lengths2,0,1)
        if (contains_points(coords,place.point[None])[0]
                or np.min(dnp.norm(offsets-fractions[:,None]*edges,axis=1)) <= 8):
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


LARGE_RETAIL_CATEGORIES = {"superstore", "discount_store", "department_store", "warehouse_club",
                           "hypermarket", "supermarket"}


def mapped_tenants(layers, ring, anchor=None, local=False, retail_candidates=None, *, outside_scale=None, large_retailers=None, enclosed=False):
    lo, hi = ring.min(0), ring.max(0)
    if outside_scale is not None:
        pad = WALL_PIN_RADIUS_M / outside_scale
        lo, hi = lo - pad, hi + pad
        metric_ring = (ring - ring[0]) * outside_scale
    if enclosed:
        scale = metres_per_degree(ring[0, 1])
        exterior_ring = (ring - ring[0]) * scale
    tenants = {}
    for f, rows, points in layers:
        near = np.flatnonzero(np.all((points >= lo) & (points <= hi), axis=1))
        if not len(near):
            continue
        if outside_scale is None:
            near = near[contains_points(ring, points[near])]
        else:
            near = [j for j in near
                    if distance_to_wall((points[j] - ring[0]) * outside_scale, metric_ring) <= WALL_PIN_RADIUS_M]
        for j in near:
            i = rows[j]

            # A deep indoor pin cannot support an outside sign or an exterior directory row.
            if enclosed and distance_to_wall((points[j] - ring[0]) * scale, exterior_ring) > MAX_TENANT_FRONTAGE_DEPTH_M:
                continue

            def value(key):
                return f.attrs[key][i] if key in f.attrs else None

            if str(value("operating_status")).lower() in {
                "permanently_closed",
                "closed",
            }:
                continue
            categories = {value("basic_category"), value("taxonomy")}
            if local and ("atm" in categories or str(value("name") or "").strip().casefold().endswith(" atm")):
                # A machine can have its own entrance marker but never owns a retail bay.
                continue
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
            if trusted and (large_retailers is not None or retail_candidates is not None):
                title = name.name if isinstance(name, LocalSign) else name or value("name")
                retail = any(str(c).endswith("_store") or c in {"shop", "shopping", "retail"} for c in categories)
                if large_retailers is not None:
                    # A large-store listing can establish ownership. A distinct retail shop can
                    # dispute it, while hosted services such as an in-store pharmacy cannot.
                    if categories & LARGE_RETAIL_CATEGORIES or (
                            retail and title != anchor and "pharmacy_and_drug_store" not in categories):
                        large_retailers.add(title)
                if retail_candidates is not None and retail:
                    if isinstance(title,str) and 2 <= len(title.strip()) <= 80:
                        retail_candidates.append((title.strip(),points[j],float(confidence)))
            tenants[str(f.ids[i])] = (name if trusted else None, points[j])
    return list(tenants.values())


SUITE_ADDRESS = re.compile(r"^([0-9]+[a-z]?\s+.+?)[ ,]+(?:suite|ste\.?|unit|#)\s*([a-z0-9-]+)$", re.I)
STREET_SUFFIX = {"drive":"dr", "street":"st", "avenue":"ave", "road":"rd",
                 "boulevard":"blvd", "lane":"ln", "court":"ct"}


def suite_directory(layers, center, anchor):
    """List known ground-level retail tenants at one numbered address. Suite IDs are labels only."""
    scale = metres_per_degree(center[1])
    anchors, entries = set(), {}
    addressed = (layer for layer in layers if "street_address" in layer[0].attrs)
    for place in trading_places(addressed, center-50/scale, center+50/scale):
        addr = SUITE_ADDRESS.fullmatch(place.address.strip())
        name = place.name
        if not addr or not name or len(name)>80 or not name.isprintable():
            continue
        words = addr[1].casefold().replace(".","").split()
        words[-1] = STREET_SUFFIX.get(words[-1], words[-1])
        key = tuple(words)
        unit = addr[2].upper()
        if name.casefold() == anchor.casefold() and dnp.norm((place.point-center)*scale)<=15:
            anchors.add((key,unit))
        elif not local_sign(name,place.categories):
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


def exterior_frontages(layers, coords, ring, label, neighbors, inside_only=False, *, chain_host_id=None, get_bounds=None, retail_corner=False, aisles=()):
    """A high-confidence shop pin just outside one unambiguous wall can identify a tenant bay."""
    from buildings import retail_bays
    scale = metres_per_degree(coords[0,1])
    radius = 8.0 if chain_host_id is not None else WALL_PIN_RADIUS_M
    search_radius = 18.0 if retail_corner else radius
    pad = search_radius/np.maximum(scale, 1.)
    lo, hi = coords.min(0)-pad, coords.max(0)+pad
    edges = np.roll(ring,-1,axis=0)-ring
    widths = dnp.norm(edges,axis=1)
    axes = edges / np.maximum(widths[:,None],1e-9)
    bays = {}
    candidates = []
    for place in trading_places(layers, lo, hi):
        sign = chain(place.name) if chain_host_id is not None else local_sign(place.name, place.categories)
        if not sign:
            continue
        title = sign if isinstance(sign, str) else sign.name
        if label:
            # The building label may include a trademark and a descriptive suffix.
            base = label.replace("\u2122", "").replace("\u00ae", "").casefold()
            trading = title.casefold()
            if base != trading and not (base.startswith(trading) and base[len(trading):].lstrip().startswith(("-", "\u2013", "\u2014"))):
                continue
        point = ring[0]+(place.point-coords[0])*scale
        offsets = point-ring
        along = np.clip(np.sum(offsets*axes,axis=1),0,widths)
        distances = dnp.norm(offsets-along[:,None]*axes,axis=1)
        wall, second = distances.argsort(kind="stable")[:2]
        ambiguous = distances[second] < distances[wall]+AMBIGUITY_MARGIN_M
        corner = (retail_corner and ambiguous and distances[wall] <= search_radius
                  and (wall-second) % len(ring) in {1, len(ring)-1}
                  and all(min(along[w], widths[w]-along[w]) < .25 for w in (wall,second)))
        if not corner and (distances[wall]>(7.0 if inside_only else radius) or ambiguous or widths[wall]<5):
            continue
        if bool(contains_points(ring,point[None])[0]) != inside_only:
            continue
        # Reject nearby competing footprints, including ones that enclose the pin.
        if any(np.all(point >= lower) and np.all(point <= upper)
               for lower,upper in neighbors):
            continue
        if chain_host_id is not None:
            bounds = get_bounds()
            slack = (distances[wall] + AMBIGUITY_MARGIN_M)/scale
            if not bounds or any(np.any((ids != chain_host_id) & np.all(lo <= place.point+slack,axis=1)
                                        & np.all(hi >= place.point-slack,axis=1)) for ids,lo,hi in bounds):
                continue
        candidates.append((sign,title,along,wall,second,corner))

    def claim(sign, title, along, wall):
        margin,count,pier,opening = retail_bays(widths[wall])
        bay = int(np.clip(round((along[wall]-margin-opening/2)/(opening+pier)),0,count-1))
        center = margin+bay*(opening+pier)+opening/2
        delta = (coords[(wall+1)%len(ring)]-coords[wall])/widths[wall]
        midpoint = coords[wall]+center*delta
        half = min(3.5,opening*.45)*delta
        bays.setdefault((wall,bay), {})[title.casefold()] = (sign,np.array([midpoint-half,midpoint+half]),wall,center)
    # Two tenant pins spilling around the same corner can both project onto the
    # roadside wall even when the customer parking approaches the adjoining face.
    # Change walls only for a pair with one corner pin and one direct pin.
    if retail_corner and len(candidates) == 2 and len(aisles):
        direct_pin = next((c for c in candidates if not c[-1]), None)
        corner_pin = next((c for c in candidates if c[-1]), None)
        if direct_pin and corner_pin and direct_pin[0] != corner_pin[0]:
            w = direct_pin[3]
            adjacent = set(corner_pin[3:5])
            if w in adjacent:
                other = next(iter(adjacent - {w}))
                winding = np.sign(np.sum(ring[:,0]*np.roll(ring[:,1],-1)-ring[:,1]*np.roll(ring[:,0],-1)))
                points = np.concatenate(aisles)
                def parking_at(k):
                    relative = points-ring[k]
                    depth = winding*(relative[:,0]*axes[k,1]-relative[:,1]*axes[k,0])
                    along = dnp.matmul(relative, axes[k])
                    slack = depth * dmath.tan(dmath.pi/8)  # allow a parking approach up to 22.5 degrees off perpendicular
                    return np.any((depth >= 3) & (depth <= 28) & (along >= -slack) & (along <= widths[k]+slack))
                if (widths[other] >= 10 and not parking_at(w) and parking_at(other)
                        and abs(direct_pin[2][w]-corner_pin[2][w]) <= 25):
                    # Divide the available wall between tenants. The street-corner
                    # tenant takes the nearest half; both fascia panels face the lot.
                    corner_at_start = corner_pin[2][other] < widths[other]/2
                    for j, candidate in enumerate((corner_pin, direct_pin)):
                        sign,title,along,*_ = candidate
                        along[other] = widths[other]*(.25 if (j == 0) == corner_at_start else .75)
                        claim(sign,title,along,other)
                    return [next(iter(v.values())) for v in bays.values() if len(v)==1]
    for sign,title,along,wall,second,corner in candidates:
        if not corner:
            claim(sign, title, along, wall)
    # Only uncontested tenant bays give independent evidence about a public wall.
    direct = {}
    for (wall, _), tenants in bays.items():
        if len(tenants) == 1:
            direct.setdefault(wall, set()).update(tenants)
    for sign,title,along,wall,second,corner in candidates:
        if corner:
            supported = [w for w in (wall,second) if direct.get(w, set()) - {title.casefold()}]
            if len(supported) == 1:
                claim(sign, title, along, supported[0])
    return [next(iter(matches.values())) for matches in bays.values() if len(matches)==1]


@lru_cache(maxsize=1)
def exterior_access_index():
    """Operator-confirmed tenant access, scoped to the mapped building, read once per worker."""
    index = {}
    path = Path(__file__).parent / "data" / "mall_exterior_access.csv"
    try:
        with path.open(encoding="utf8", newline="") as source:
            for row in csv.DictReader(source):
                index.setdefault(row["building"], set()).add(row["tenant"].casefold())
    except OSError as error:
        LOG.warning("Mall exterior access data unavailable: %s", error)
    return index


def open_mall_approach(center, points, starts, ends):
    """Does any mapped approach reach the bay without crossing a footprint wall?"""
    for point in points:
        low, high = np.minimum(center,point), np.maximum(center,point)
        near = np.all((starts <= high) | (ends <= high),axis=1) & np.all((starts >= low) | (ends >= low),axis=1)
        a, b = starts[near]-center, ends[near]-center
        ray = point-center
        side_a = ray[0]*a[:,1]-ray[1]*a[:,0]
        side_b = ray[0]*b[:,1]-ray[1]*b[:,0]
        crossing = (side_a > 0) != (side_b > 0)
        if crossing.any():
            t = side_a[crossing]/(side_a[crossing]-side_b[crossing])
            hits = a[crossing] + t[:,None]*(b[crossing]-a[crossing])
            along = dnp.matmul(hits, ray)
            if np.any((along > 1e-5) & (along <= dnp.matmul(ray, ray))):
                continue
        return True
    return False


def mall_tenant_banners(coords, candidates, occupied, parking, walks, neighbors=(), *, enclosed=False):
    """Estimate visible frontage in metres and fill open bays with nearby indoor retail identities."""
    if not candidates:
        return []
    scale = metres_per_degree(coords[0,1])
    ring = (coords-coords[0])*scale
    samples = []
    if parking is not None and len(parking):
        samples.append(np.asarray(parking))
    if walks is not None and len(walks):
        samples.extend((walks[:,0],walks.mean(axis=1),walks[:,1]))
    if not samples:
        return []
    access, first = np.unique(np.concatenate(samples),axis=0,return_index=True)
    is_parking = first < (len(parking) if parking is not None else 0)
    pad = 50/scale
    nearby = np.all((access>=coords.min(0)-pad)&(access<=coords.max(0)+pad),axis=1)
    access, is_parking = access[nearby], is_parking[nearby]
    outside = ~contains_points(coords,access)
    access, is_parking = (access[outside]-coords[0])*scale, is_parking[outside]
    edges = np.roll(ring,-1,axis=0)-ring
    winding = np.sign(np.sum(ring[:,0]*edges[:,1]-ring[:,1]*edges[:,0]))
    slot_width = 12.0
    outlines = [ring] + [(outline-coords[0])*scale for outline in neighbors]
    starts = np.concatenate(outlines)
    ends = np.concatenate([np.roll(outline,-1,axis=0) for outline in outlines])
    slots, parking_view, visible = [], [], 0.
    for start,edge in zip(ring,edges):
        length = dnp.norm(edge)
        if length < 10:
            continue
        axis = edge/length
        normal = winding*np.array([axis[1],-axis[0]])
        ahead = dnp.matmul(access-start, normal)
        front = (ahead>=1)&(ahead<=45)
        approaches, from_parking = access[front], is_parking[front]
        along = dnp.matmul(approaches-start, axis)
        bays = max(1, int(length//slot_width))
        for bay in range(bays):
            center = (bay+.5)*length/bays
            near = abs(along-center)<=10
            bay = start+axis*center
            roadside = open_mall_approach(bay, approaches[near & from_parking], starts, ends)
            if roadside or open_mall_approach(bay, approaches[near & ~from_parking], starts, ends):
                parking_view.append(roadside)
                slots.append((start+axis*center,axis*winding,min(5.,length/bays*.43)))
                visible += length/bays
    def title(n):
        return (n.name if isinstance(n,LocalSign) else n).casefold()
    used = {title(n) for n,_ in occupied if not isinstance(n,LocalSign) or n.style!="entrance"}
    inferred_budget = int(visible//38)
    if not slots:
        return []
    centers = np.array([c for c,_,_ in slots])
    blocked = np.zeros(len(slots),dtype=bool)
    half_spans = np.array([half for _,_,half in slots])
    for _,p in occupied:
        existing_half = dnp.norm((p[1]-p[0])*scale)/2
        blocked |= dnp.norm(centers-(p.mean(0)-coords[0])*scale,axis=1) < half_spans+existing_half+1.5
    ranked = []
    for name,pt,confidence in candidates:
        distance = dnp.norm(centers-(pt-coords[0])*scale,axis=1)
        nearest = int(distance.argmin())
        # An enclosed mall banner needs a nearby exterior bay.
        if enclosed and distance[nearest] > 8:
            continue
        # A precise pin can justify its own bay even on a short facade.
        # Shared pins and ambiguous corners cannot unlock extra adjacent bays.
        clear_bay = (confidence >= .95 and distance[nearest] <= 8 and
                     np.count_nonzero(distance < distance[nearest]+AMBIGUITY_MARGIN_M) == 1)
        for k in np.flatnonzero((distance<=min(45.,distance.min()+(slot_width if confidence >= .95 else 8)))&~blocked):
            if enclosed and (not clear_bay or k != nearest):
                continue
            ranked.append((not (clear_bay and k == nearest), confidence < .95, not parking_view[k], distance[k]/45 + (1-confidence)*2, name.casefold(), int(k), name))
    extra = []
    for inferred,_,_,_,key,k,name in sorted(ranked):
        if inferred and len(used) >= inferred_budget:
            break
        if key in used or blocked[k]:
            continue
        c,axis,half = slots[k]
        panel = coords[0]+(c+np.array([-half,half])[:,None]*axis)/scale
        extra.append((LocalSign(name,"banner"),panel))
        used.add(key)
        blocked |= dnp.norm(centers-c,axis=1) < half_spans+half+1.5
    return extra


def confirmed_mall_panels(layers, coords, names, doors=()):
    """Exterior access identifies the tenant. Its live pin must independently identify a wall."""
    if not names and not doors:
        return []
    names = names or ()
    scale = metres_per_degree(coords[0,1])
    lo, hi = coords.min(0)-4/scale, coords.max(0)+4/scale
    edges = (np.roll(coords,-1,axis=0)-coords)*scale
    widths = dnp.norm(edges,axis=1)
    axes = edges/np.maximum(widths[:,None],1e-9)
    ring = (coords-coords[0])*scale
    winding = np.sum(ring[:,0]*edges[:,1]-ring[:,1]*edges[:,0])
    matches = {}
    for place in trading_places(layers, lo, hi):
        label = place.name
        documented = label.casefold() in names
        brand = chain(label) or local_sign(label, place.categories)
        if not documented and (not doors or not brand or not any(
                dnp.norm((place.point-door)*scale)<=2 for door in doors)):
            continue
        offsets = (place.point-coords)*scale
        along = np.clip(np.sum(offsets*axes,axis=1),0,widths)
        distances = dnp.norm(offsets-along[:,None]*axes,axis=1)
        wall, second = distances.argsort(kind="stable")[:2]
        # Near a corner, require the perpendicular projection to land inside the edge.
        if (distances[wall]>8 or distances[second]-distances[wall]<.75
                or min(along[wall],widths[wall]-along[wall])<1.5 or widths[wall]<8):
            continue
        half = min(5., widths[wall]/2-1.)
        center = np.clip(along[wall],half+1,widths[wall]-half-1)
        bounds = center+np.array([-half,half])
        panel = coords[wall]+bounds[:,None]*axes[wall]/scale
        brand = brand or LocalSign(label,"shop")
        matches.setdefault(label.casefold(),{})[wall] = brand, panel if winding>0 else panel[::-1]
    candidates = [next(iter(m.values())) for m in matches.values() if len(m)==1]
    return [(name,p) for name,p in candidates if all(other==name or
            dnp.norm((q.mean(0)-p.mean(0))*scale)>11 for other,q in candidates)]


def tenant_panels(tenants, ring, parking, entrances=(), walks=()):
    """Leave room for all mapped tenants, including businesses without a brand model."""
    for i, (name, point) in enumerate(tenants):
        if not name:
            continue
        approaches = []
        if len(walks):
            # A long straight promenade may have no vertices beside this tenant.
            scale = metres_per_degree(point[1])
            start = (walks[:,0]-point)*scale
            delta = (walks[:,1]-walks[:,0])*scale
            along = np.clip(-np.sum(start*delta,axis=1)/np.maximum(np.sum(delta*delta,axis=1),1e-12),0,1)
            near = start+along[:,None]*delta
            candidates = point + near[np.sum(near*near,axis=1) <= 32**2]/scale
            approaches = candidates[~contains_points(ring,candidates)] if len(candidates) else []
        panel = parking_frontage(ring, point, parking, entrances=entrances, walks=approaches)
        if panel is None:
            continue
        scale = metres_per_degree(point[1])
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


def commons_panels(tenants, coords, paths, doors, neighbors=()):
    """Project each tenant onto a customer-facing wall and divide that wall between neighbours."""
    scale = metres_per_degree(coords[0, 1])
    ring = (coords - coords[0]) * scale
    ends = np.roll(ring, -1, axis=0)
    edges = ends - ring
    lengths = dnp.norm(edges, axis=1)
    axes = edges / np.maximum(lengths[:, None], 1e-9)
    winding = np.sign(np.sum(ring[:, 0]*edges[:, 1] - ring[:, 1]*edges[:, 0]))
    normals = winding * np.column_stack((axes[:, 1], -axes[:, 0]))
    paths = (np.asarray(paths).reshape(-1, 2, 2) - coords[0]) * scale
    near = (np.all(paths.max(1) >= ring.min(0)-45, axis=1)
            & np.all(paths.min(1) <= ring.max(0)+45, axis=1))
    paths = paths[near]
    steps = paths[:, 1] - paths[:, 0]
    squared = np.maximum(np.sum(steps*steps, axis=1), 1e-9)
    doors = (np.asarray(doors).reshape(-1, 2) - coords[0]) * scale
    outlines = [ring, *neighbors]
    starts = np.concatenate(outlines)
    stops = np.concatenate([np.roll(outline, -1, axis=0) for outline in outlines])
    rows = {}
    for name, point in tenants:
        offset = (point-coords[0])*scale - ring
        along = np.sum(offset*axes, axis=1)
        depths = -np.sum(offset*normals, axis=1)
        possible = (along >= 3) & (along <= lengths-3) & (depths >= -.1) & (depths <= 15)
        supported = []
        for wall in np.flatnonzero(possible):
            center = ring[wall] + along[wall]*axes[wall]
            if not open_mall_approach(center, [(point-coords[0])*scale], ring, ends):
                continue
            fraction = np.clip(np.sum((center-paths[:, 0])*steps, axis=1)/squared, 0, 1)
            approach = paths[:, 0] + fraction[:, None]*steps
            ahead = approach - center
            depth = dnp.matmul(ahead, normals[wall])
            near = (depth >= 1) & (depth <= 45) & (abs(dnp.matmul(ahead, axes[wall])) <= 12)
            door = np.any(dnp.norm(doors-center, axis=1) <= 3)
            if door or open_mall_approach(center, approach[near], starts, stops):
                supported.append((not door, depths[wall], wall))
        supported.sort()
        if not supported:
            continue
        first = supported[0]
        if len(supported) > 1 and supported[1][0] == first[0] and supported[1][1]-first[1] < 3:
            continue
        wall = first[2]
        rows.setdefault(wall, []).append((along[wall], name))

    for wall, row in sorted(rows.items()):
        row.sort(key=lambda entry: entry[0])
        for i, (center, name) in enumerate(row):
            left = (center-row[i-1][0])/2 if i else center
            right = (row[i+1][0]-center)/2 if i+1 < len(row) else lengths[wall]-center
            half = min(8., left-.6, right-.6)
            if name and half >= 2:
                along = center + winding*np.array([-half, half])
                yield name, coords[0] + (ring[wall]+along[:, None]*axes[wall])/scale


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


def mapped_doors(source, coords, *, explicit_only=False):
    """Use only evidence on the same OSM way and still on the current footprint."""
    way = source.partition("@")[0]
    if not way.startswith("w") or not way[1:].isdigit():
        return ()
    rows = entrance_index().get(way, ())
    if not rows:
        return ()
    scale = metres_per_degree(coords[0,1])
    edges = (np.roll(coords, -1, axis=0)-coords)*scale
    length2 = np.maximum(np.sum(edges*edges, axis=1), 1e-12)
    public, paths = [], []
    for lon, lat, explicit in rows:
        offset = (np.array([lon,lat])-coords)*scale
        fraction = np.clip(np.sum(edges*offset, axis=1)/length2, 0, 1)
        residual = offset-edges*fraction[:,None]
        if np.min(np.sum(residual*residual, axis=1)) <= 1.5*1.5:
            (public if explicit else paths).append((lon,lat))
    return tuple(sorted(set(public if explicit_only else public or paths)))


def entrance_offsets(coords, ring, points):
    """Place each observation on its closest wall, in metres from the wall start."""
    offsets = {}
    if not points:
        return offsets
    scale = metres_per_degree(coords[0,1])
    edges = (np.roll(coords,-1,axis=0)-coords)*scale
    squared = np.maximum(np.sum(edges*edges,axis=1), 1e-12)
    lengths = dnp.norm(np.roll(ring,-1,axis=0)-ring, axis=1)
    for point in points:
        delta = (point-coords)*scale
        along = np.clip(np.sum(delta*edges,axis=1)/squared,0,1)
        wall = int(np.argmin(np.sum(dnp.sq(delta-along[:,None]*edges),axis=1)))
        offsets.setdefault(wall, []).append(float(along[wall]*lengths[wall]))
    return offsets


def cached_entry(cache, name, coords, bearing, material, sign_height=None, doors=(), entrance_bearing=None, local_panel=None, drive_through=None, carhop=False, mapped_name=False, category=None):
    # Round away sub-centimetre anchor differences. The key describes the geometry actually exported.
    coords = coords.round(7)
    parameters = [STOREFRONT_VERSION, CATALOG_DIGEST, name, coords.tolist(), bearing, material]
    if sign_height is not None:
        parameters.append(sign_height)
    if doors:
        parameters.append(doors)
    if entrance_bearing is not None:
        parameters.append({"entrance_bearing": entrance_bearing})
    if local_panel:
        parameters.append({"local_panel": local_panel})
    if category:
        parameters.append({"category": category})
    if mapped_name:
        parameters.append("mapped_name")
    if carhop:
        parameters.append("carhop")
    if drive_through:
        parameters.append({"drive_through": drive_through})
    signature = json.dumps(parameters).encode()
    ident = hashlib.sha256(signature).hexdigest()[:24]
    meta = cache / f"store_{ident}.json"
    mesh = cache / f"store_{ident}.glb"
    identity = identity_for(chain(name) or name, local_panel[0] if local_panel else category or "shop", mapped_name=mapped_name)
    try:
        entry = json.loads(meta.read_text(encoding="utf8"))
        if (isinstance(entry, dict) and entry.get("file") == mesh.name and entry.get("fictional_identity") == identity.key
                and entry.get("title") == identity.label
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
    entry = export_store(Path("streamed"), match, {}, cache, entrance_direction=parking, mapped_name=mapped_name, **({"category": category} if category else {}), **({"local_panel": local_panel} if local_panel else {}), **({"drive_through": drive_through} if drive_through else {}), **({"carhop": True} if carhop else {}))
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


def street_frontage(offsets, priorities):
    """Break near-distance ties in favour of public commercial streets, avoiding motorway ramps."""
    squared = np.sum(offsets*offsets, axis=1)
    if priorities is None:
        return offsets[np.argmin(squared)]
    usable = priorities < 3
    if not usable.any():
        return offsets[np.argmin(squared)]
    nearest = np.sqrt(squared[usable].min())
    near = usable & (squared <= dnp.sq(nearest + 8.))
    rank = priorities[near].min()
    return offsets[np.argmin(np.where(near & (priorities == rank), squared, np.inf))]


def footprint_bounds(layers):
    """Include competing buildings owned by adjacent render tiles."""
    for f in layers:
        if f is None or not len(f):
            continue
        offsets = f.coord_off[f.ring_off[f.part_off]]
        rows = np.flatnonzero(np.diff(offsets) > 0)
        if len(rows):
            yield (f.ids[rows], np.minimum.reduceat(f.lonlat, offsets[rows], axis=0),
                   np.maximum.reduceat(f.lonlat, offsets[rows], axis=0))


def unclassified_building_tenant(tenants, label, measured_chain_host):
    """An unclassified footprint needs a single record and corroborating host evidence."""
    if len(tenants) != 1:
        return None
    tenant, _ = tenants[0]
    if isinstance(tenant, LocalSign):
        if label:
            return tenant if tenant.name.casefold() == label.casefold() else None
        return tenant if tenant.style in {"bank", "wash"} else None
    return tenant if measured_chain_host and isinstance(tenant, str) else None


def building_tenant(tenants, *, label, anchor, fallback, mall, generic_host):
    """Choose a whole-building identity. Individual frontage signs are placed separately."""
    if mall:
        return None
    sole = tenants[0][0] if len(tenants) == 1 else None
    if anchor:
        return anchor if all(tenant == anchor for tenant, _ in tenants) else None
    if fallback and all(tenant == fallback for tenant, _ in tenants):
        return fallback
    if not label or (isinstance(sole, LocalSign) and not generic_host):
        return sole
    return None


def hosted_pharmacy(layers, coords, panels):
    """A numbered in-store service record corroborates a nearby pharmacy's footprint."""
    brands = {brand for brand, *_ in panels}
    if len(brands) != 1 or not brands <= PHARMACY_SOURCES:
        return None
    brand = next(iter(brands))
    confirmed = False
    for place in trading_places(layers, coords.min(0), coords.max(0)):
        if not contains_points(coords, place.point[None])[0]:
            continue
        if (place.categories & {"financial_service", "money_transfer_service", "atm"} and
                re.fullmatch(re.escape(brand) + r"\s*#\s*\d+", place.name, re.I)):
            confirmed = True
        elif not place.categories & {"atm", "money_transfer_service", "propane_supplier",
                                     "video_and_video_game_rental"}:
            return None
    return brand if confirmed else None


def carhop_approach(layers, coords, ring, service_lanes):
    """Infer an open stall row from a slender empty outline, a drive-in POI and a parallel access lane."""
    from buildings import oriented_box
    center, axis, half_length, half_width = oriented_box(ring)
    double = 4.6 <= half_width <= 6.8
    if not (7 <= half_length <= 27 and (double or 2.2 <= half_width <= 3.8)
            and half_length >= (2 if double else 3)*half_width):
        return None
    if abs(np.sum(ring[:,0]*np.roll(ring[:,1],-1)-ring[:,1]*np.roll(ring[:,0],-1))) < 7.6*half_length*half_width:
        return None
    scale = metres_per_degree(coords[0,1])
    midpoint = coords[0] + (center-ring[0])/scale
    normal = np.array([-axis[1], axis[0]])
    side = None
    owner = None
    for place in trading_places(layers, midpoint-32/scale, midpoint+32/scale):
        delta = (place.point-midpoint)*scale
        across = dnp.matmul(delta, normal)
        if dnp.norm(delta) > 32:
            continue
        source = chain(place.name)
        if not source or identity_for(source).style != "drive_in" or (owner and source != owner):
            return None
        owner = source
        if abs(across) <= half_width+4 or abs(dnp.matmul(delta, axis)) > half_length+half_width:
            return None
        sign = np.sign(across)
        if side is not None and side != sign:
            return None
        side = sign
    if side is None or mapped_tenants(layers, coords):
        return None
    served_sides = set()
    for line in service_lanes():
        for start, end in zip(line, line[1:]):
            delta = end-start
            if abs(dnp.matmul(delta, axis)) < .96*dnp.norm(delta):
                continue
            along = np.sort(dnp.matmul(np.array([start,end])-center, axis))
            overlap = min(half_length,along[1])-max(-half_length,along[0])
            offsets = dnp.matmul(np.array([start,end])-center, normal)
            for face in (-1, 1):
                gaps = offsets*face-half_width
                if overlap >= half_length and min(gaps) >= 1.4 and max(gaps) <= 9:
                    served_sides.add(face)
    if side in served_sides and (not double or -side in served_sides):
        return owner, normal*side
    return None


def drive_through_fixtures(ring, lanes, left_hand):
    """Fit two fixtures on one exterior wall alongside a directed, uninterrupted lane segment."""
    edges = np.roll(ring, -1, axis=0) - ring
    winding = np.sign(np.sum(ring[:, 0]*edges[:, 1] - ring[:, 1]*edges[:, 0]))
    matches = {}
    for wall, (a, edge) in enumerate(zip(ring, edges)):
        length = dnp.norm(edge)
        if length < 10:
            continue
        axis = edge / length
        outward = winding * np.array([axis[1], -axis[0]])
        for lane in lanes:
            for start, end in zip(lane, lane[1:]):
                delta = end-start
                run = dnp.matmul(delta, axis)
                if abs(run) < 9 or abs(run) < .99*dnp.norm(delta):
                    continue
                driver_side = np.array([-delta[1], delta[0]]) * (-1 if left_hand else 1)
                if dnp.matmul(driver_side, outward) >= 0:
                    continue
                gaps = dnp.matmul(np.array([start-a, end-a]), outward)
                if min(gaps) < 1.8 or max(gaps) > 4.5:
                    continue
                lo, hi = sorted((dnp.matmul(start-a, axis), dnp.matmul(end-a, axis)))
                lo, hi = max(1.5, lo), min(length-1.5, hi)
                if hi-lo < 8:
                    continue
                pair = np.array([lo+1, hi-1]) / length
                if wall not in matches or hi-lo > matches[wall][0]:
                    matches[wall] = (hi-lo, pair if run > 0 else pair[::-1])
    if len(matches) == 1:
        wall, (_, pair) = next(iter(matches.items()))
        return wall, pair


def nearby_chain_tenant(layers, coords, building_id, get_bounds):
    """Accept one trusted chain just outside the wall if no other footprint could own its pin."""
    scale = metres_per_degree(coords[0, 1])
    candidates = mapped_tenants(layers, coords, outside_scale=scale)
    if len(candidates) != 1 or not isinstance(candidates[0][0], str):
        return []
    _, point = candidates[0]
    pad = (WALL_PIN_RADIUS_M + AMBIGUITY_MARGIN_M) / scale
    bounds = get_bounds()
    for ids, lo, hi in bounds:
        overlaps = np.all(lo <= point + pad, axis=1) & np.all(hi >= point - pad, axis=1)
        if np.any(overlaps & (ids != building_id)):
            return []
    return candidates if bounds else []


def models(items, existing, frame, origin, roads, md, keys, cache_dir, aisles=(), load_walks=None, building_layers=(), load_drive_lanes=None, left_hand=False, load_service_lanes=None, load_bank_lanes=None):
    starts = np.concatenate([line[:-1] for line in roads]) if roads else np.empty((0,2))
    ends = np.concatenate([line[1:] for line in roads]) if roads else np.empty((0,2))
    delta = ends - starts
    length2 = np.maximum(np.sum(delta * delta, axis=1), 1e-12)
    priorities = None
    classes = getattr(roads, "classes", ())
    if classes and len(classes) == len(roads):
        ranks = {"primary": 0, "secondary": 0, "tertiary": 0, "service": 2, "motorway": 3}
        priorities = np.repeat([ranks.get(cls,1) for cls in classes], [len(line)-1 for line in roads])
    layers = None
    @lru_cache(maxsize=1)
    def place_layers():
        return list(businesses(md, keys))
    local_enabled = os.environ.get("BNE_LOCAL_BUSINESS_SIGNS") == "1"
    service_lanes = lru_cache(maxsize=1)(load_service_lanes) if load_service_lanes else None
    bank_lanes = lru_cache(maxsize=1)(load_bank_lanes) if load_bank_lanes else None
    drive_lanes = None
    parking_points = None
    walk_segments = None
    @lru_cache(maxsize=1)
    def customer_paths():
        lines = [line for line in aisles if len(line) > 1]
        segments = [np.stack((line[:-1], line[1:]), axis=1) for line in lines]
        result = []
        if segments:
            xy = np.concatenate(segments).reshape(-1, 2) + origin
            lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
            result.append(np.column_stack((lon, lat)).reshape(-1, 2, 2))
        if walk_segments is not None and len(walk_segments):
            result.append(walk_segments)
        return np.concatenate(result) if result else np.empty((0, 2, 2))
    cache = Path(cache_dir) / "threedmr_cache" / "storefronts"
    @lru_cache(maxsize=1)
    def source_bounds():
        return list(footprint_bounds(building_layers))

    @lru_cache(maxsize=1)
    def item_bounds():
        return (np.array([other.ring.min(0)[:2]-WALL_PIN_RADIUS_M for other,_,_ in items]),
                np.array([other.ring.max(0)[:2]+WALL_PIN_RADIUS_M for other,_,_ in items]))
    for item_index,(spec, ground_min, ground_max) in enumerate(items):
        if (
            not spec.storefront
            or spec.min_h
            or spec.holes
            or spec.deck
        ):
            continue
        host = spec.storefront
        label, material, subtype = host.label, host.material, host.subtype
        venue_only = host.venue_only
        if venue_only and not local_enabled:
            continue
        kind = str(spec.cls or "").lower()
        name = chain(label, kind == "cinema" or subtype == "entertainment")
        if subtype == "entertainment" and not (local_enabled and kind in {"cinema", "movie_theater"}) and name not in {"AMC Theatres", "Regal Cinemas", "Cinemark"}:
            continue
        mall = kind in MALL_CLASSES or (kind in {"", "commercial", "retail", "shop"} and label.casefold().endswith((" mall", " outlets")))
        enclosed = kind in {"mall", "shopping_mall"} or label.casefold().endswith(" mall")
        generic_host = mall or kind in {"retail", "commercial", "shop"} or (not kind and subtype == "commercial")
        categories = {kind}
        if subtype == "commercial" and label.casefold().endswith("restaurant"):
            categories.add("restaurant")
        fallback = local_sign(label, categories) if local_enabled else None
        # Some commercial footprints carry only a name or a surveyed low flat roof.
        # Probe place records before accepting these hosts; an untyped outline alone proves nothing.
        chain_host = (not kind and not label and host.measured_low_rise
                      and spec.roof == "flat" and not spec.fr_house)
        probe = chain_host or local_enabled and kind in {"", "office", "commercial"} and (
            bool(label) or (spec.roof == "flat" and 2.8 <= spec.height <= 7 and not spec.fr_house))
        carhop_candidate = (service_lanes and not label and kind in {"", "roof", "carport"}
                            and len(spec.ring) == 4 and 2 <= spec.height <= 5.5)
        eligible = (venue_only or probe or kind in COMMERCIAL or (local_enabled and (kind in SIGN_STYLES or kind in CATEGORY_INDEX))
                    or generic_host or spec.retail_front or (name and not kind))
        if not eligible and not carhop_candidate:
            continue
        if not venue_only and label and not name and not generic_host and not fallback and not probe:
            continue
        ring = np.asarray(spec.ring)[:, :2]
        span = np.max(np.ptp(ring,axis=0))
        if len(ring) > 512 or span > 1500:
            continue
        ll = frame.enu_to_geodetic(
            ring[:, 0] + origin[0], ring[:, 1] + origin[1], np.zeros(len(ring))
        )
        coords = np.column_stack((ll[1], ll[0]))
        carhop = False
        if carhop_candidate and 14 <= span <= 55:
            if layers is None:
                layers = place_layers()
            carhop_site = carhop_approach(layers, coords, ring, service_lanes)
            if carhop_site is not None:
                carhop = True
                name, approach = carhop_site
        if not carhop and not eligible:
            continue
        if not mall and generic_host and span > 180:
            if layers is None:
                layers = place_layers()
            if label:
                mall = mapped_mall(layers, coords, label)
                enclosed = mall in {"mall", "shopping_mall"}
            else:
                label = unnamed_shopping_center(layers, coords) or ""
                mall = bool(label)
                enclosed = mall and mapped_mall(layers, coords, label) in {"mall", "shopping_mall"}
        # Mall signs attach to the existing shell at its own base elevation.
        if not mall and ground_max - ground_min > 3:
            continue
        if mall and spec.roof == "flat" and spec.height >= 5.5 and not material and "|c_" not in spec.facade:
            spec.mall_cladding = True
        if not mall and (len(ring) > 96 or span > 250):
            continue
        centre = ring.mean(0)
        lower, upper = ring.min(0) + origin - 3, ring.max(0) + origin + 3
        if not mall and any(np.all((c["xy"] >= lower) & (c["xy"] <= upper)) for c in existing
               if not (c.get("local") or {}).get("storefront_pylon")):
            continue
        facing = None
        if not mall:
            if not len(starts) and not (local_enabled and generic_host and load_walks):
                continue
            if len(starts):
                closest = np.clip(np.sum((centre-starts)*delta,axis=1)/length2,0,1)
                offset = starts+closest[:,None]*delta-centre
                facing = approach * 10 if carhop else street_frontage(offset, priorities if name else None)
                if not 1 <= dnp.norm(facing) <= 150 + np.max(dnp.norm(ring-centre,axis=1)):
                    continue
        try:
            exterior = []
            chain_panels = []
            doors = mapped_doors(host.source_record, coords) if (spec.retail_front or name or mall) else ()
            if spec.retail_front and doors:
                spec.retail_doors = entrance_offsets(coords, ring, doors)
            pharmacy_front = None
            confirmed = None
            if local_enabled and label and not mall and not name and kind in {"commercial", "retail", "shop"}:
                if layers is None:
                    layers = place_layers()
                confirmed = named_frontage(layers, coords, label)
            category_owner = False
            if confirmed:
                name = confirmed
                if confirmed.style in CATEGORY_RULES:
                    occupants = mapped_tenants(layers, coords, local=True)
                    category_owner = bool(occupants) and all(t == confirmed for t, _ in occupants)
            elif venue_only and not mall:
                if mall or kind not in {"", "commercial", "retail", "shop", "bar", "pub", "music_venue"}:
                    continue
                if layers is None:
                    layers = place_layers()
                candidates = {t for t, _ in mapped_tenants(layers, coords, local=True)
                              if isinstance(t, LocalSign) and t.style == "venue"
                              and (not label or t.name.casefold() == label.casefold())}
                if len(candidates) != 1:
                    continue
                name = candidates.pop()
            elif not carhop and (generic_host or not name):
                if layers is None:
                    layers = place_layers()
                retail_candidates = [] if mall and local_enabled else None
                large_retailers = set() if name and not mall and generic_host else None
                tenants = mapped_tenants(layers, coords, anchor=name, local=local_enabled,
                                         retail_candidates=retail_candidates, large_retailers=large_retailers,
                                         enclosed=bool(mall and enclosed))
                unclassified = probe and not generic_host and not spec.retail_front and not name
                if unclassified and chain_host and not tenants and building_layers:
                    tenants = nearby_chain_tenant(layers, coords, host.building_id, source_bounds)
                if unclassified:
                    name = unclassified_building_tenant(tenants, label, chain_host)
                    category_owner = (not kind and subtype == "commercial" and bool(label)
                                      and isinstance(name, LocalSign) and name.style in CATEGORY_RULES)
                if not label and not name and not mall and (generic_host or (chain_host and tenants)) and building_layers:
                    chain_panels = exterior_frontages(layers, coords, ring, "", (), chain_host_id=host.building_id,
                                                      get_bounds=source_bounds, retail_corner=generic_host and bool(tenants), aisles=aisles)
                if unclassified and chain_host and chain_panels:
                    name = hosted_pharmacy(layers, coords, chain_panels)
                    if name:
                        pharmacy_front = chain_panels[0][2] if len(chain_panels) == 1 else None
                        chain_panels = []
                if unclassified and not name and not chain_panels:
                    distinct = {tenant for tenant, _ in tenants if tenant}
                    if label or spec.windows is not False or len(distinct) < 2:
                        continue
                    exterior = exterior_frontages(layers, coords, ring, "", (), inside_only=True)
                    if not exterior:
                        continue
                    spec.retail_front = spec.shop = True
                    spec.plain = False
                    spec.floors, spec.floor_h = 1, spec.height
                if spec.retail_front and local_enabled and not name and not doors and not tenants:
                    lower_bounds, upper_bounds = item_bounds()
                    nearby = (np.all(upper_bounds >= ring.min(0)-WALL_PIN_RADIUS_M,axis=1)
                              & np.all(lower_bounds <= ring.max(0)+WALL_PIN_RADIUS_M,axis=1))
                    nearby[item_index] = False
                    neighbors = list(zip(lower_bounds[nearby],upper_bounds[nearby]))
                    exterior = exterior_frontages(layers,coords,ring,label,neighbors)
                if not unclassified and not chain_panels:
                    anchor = name
                    name = building_tenant(tenants, label=label, anchor=name, fallback=fallback,
                                           mall=mall, generic_host=generic_host)
                    if isinstance(name, LocalSign) and name.style in CATEGORY_RULES and not mall:
                        # A POI on its own can identify a sign, but cannot claim a shared shell.
                        host_family = category_family({kind})
                        compatible = (not host_family or host_family == name.style
                                      or host_family in {"counter", "dining_room"} and name.style in DINING_STYLES)
                        category_owner = (bool(label) and label.casefold() == name.name.casefold()
                                          and compatible and (host_family or any(t == name for t, _ in tenants))
                                          and all(t == name for t, _ in tenants))
                    # A named big-box footprint and its own large-retailer listing agree on
                    # the host identity even when pharmacies or kiosks trade inside.
                    if not name and anchor and large_retailers == {anchor}:
                        name = anchor
                    if not name and anchor and facing is not None and any(t == anchor for t,_ in tenants):
                        panel = approach_panel(ring, coords, facing, doors)
                        if panel is not None:
                            chain_panels.append((anchor,panel,None,None))
                if not name and not exterior and not chain_panels and not (generic_host and (tenants or (mall and local_enabled)) and (len(aisles) or (mall or local_enabled) and (doors or load_walks))):
                    continue
            if name and facing is None:
                continue
            if name and not doors and not spec.retail_front:
                doors = mapped_doors(host.source_record, coords)
            entrance_bearing = None
            if name and not doors and kind in {"retail", "supermarket", "department_store", "hypermarket", "wholesale"}:
                parking = parking_direction(ring, aisles)
                if parking is not None:
                    entrance_bearing = round(dmath.degrees(dmath.atan2(parking[1], parking[0])))
            if pharmacy_front is not None and not doors and entrance_bearing is None:
                edge = ring[(pharmacy_front+1) % len(ring)] - ring[pharmacy_front]
                winding = np.sign(np.sum(ring[:,0]*np.roll(ring[:,1],-1)-ring[:,1]*np.roll(ring[:,0],-1)))
                entrance_bearing = round(dmath.degrees(dmath.atan2(-edge[0]*winding, edge[1]*winding)))
            bearing = round(dmath.degrees(dmath.atan2(facing[1], facing[0]))) if facing is not None else None
            signed_wall = None
            verified = []
            if exterior:
                matches = [(sign,panel,None,None) for sign,panel,_,_ in exterior]
            elif category_owner and not venue_only and not mall and spec.height >= 2.8:
                matches = [(name, coords, bearing, material)]
            elif isinstance(name, LocalSign):
                lower_bounds, upper_bounds = item_bounds()
                nearby = np.all(lower_bounds <= ring.max(0),axis=1) & np.all(upper_bounds >= ring.min(0),axis=1)
                nearby[item_index] = False
                neighbors = [items[j][0].ring[:,:2] for j in np.flatnonzero(nearby) if not items[j][0].min_h]
                panel = approach_panel(ring, coords, facing, doors, neighbors=neighbors,
                                       street_offsets=offset if not mall else ())
                if panel is None:
                    continue
                if spec.retail_front and label and fallback == name:
                    from buildings import retail_bays
                    ends = [int(np.argmin(dnp.norm(coords-pt, axis=1))) for pt in panel]
                    wall = ends[0] if (ends[0]+1) % len(ring) == ends[1] else ends[1]
                    length = float(dnp.norm(ring[ends[1]]-ring[ends[0]]))
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
                commons = local_enabled and not enclosed and generic_host and len(tenants) >= 2
                if (mall or commons) and load_walks and walk_segments is None:
                    lines = [line for line in load_walks() if len(line)>1]
                    walk_segments = []
                    if lines:
                        xy = np.concatenate([np.stack((l[:-1],l[1:]),axis=1) for l in lines]).reshape(-1,2)+origin
                        lat,lon,_ = frame.enu_to_geodetic(xy[:,0],xy[:,1],np.zeros(len(xy)))
                        walk_segments = np.column_stack((lon,lat)).reshape(-1,2,2)
                if commons:
                    lower_bounds, upper_bounds = item_bounds()
                    nearby = np.all(lower_bounds <= ring.max(0)+45,axis=1) & np.all(upper_bounds >= ring.min(0)-45,axis=1)
                    nearby[item_index] = False
                    obstacles = [items[j][0].ring[:,:2]-ring[0] for j in np.flatnonzero(nearby) if not items[j][0].min_h]
                    panels = list(commons_panels(tenants, coords, customer_paths(), doors, obstacles))
                else:
                    panels = list(tenant_panels(tenants,coords,parking_points if parking_points is not None else (),
                                                doors if mall else (), walk_segments if mall and walk_segments is not None else ()))
                if mall and local_enabled:
                    access = exterior_access_index().get(host.source_record.partition("@")[0])
                    verified = confirmed_mall_panels(layers, coords, access,
                                                     mapped_doors(host.source_record,coords,explicit_only=True))
                    if verified:
                        scale = metres_per_degree(coords[0,1])
                        panels = [(n,p) for n,p in panels if all(n != v and
                                  dnp.norm((p.mean(0)-q.mean(0))*scale) > 16 for v,q in verified)]
                        panels.extend(verified)
                if mall and local_enabled:
                    scale = metres_per_degree(coords[0,1])
                    pad = 15/scale
                    for place in trading_places(layers,coords.min(0)-pad,coords.max(0)+pad):
                        brand = chain(place.name)
                        if not brand or not any(str(c).endswith("_store") for c in place.categories):
                            continue
                        if contains_points(coords,place.point[None])[0]:
                            continue
                        gap = distance_to_wall((place.point-coords[0])*scale,(coords-coords[0])*scale)
                        if gap > 15:
                            continue
                        slack = (gap+AMBIGUITY_MARGIN_M)/scale
                        bounds = source_bounds()
                        if bounds and not any(np.any((ids!=host.building_id)&np.all(lo<=place.point+slack,axis=1)
                                                     &np.all(hi>=place.point-slack,axis=1)) for ids,lo,hi in bounds):
                            retail_candidates.append((brand,place.point,place.confidence))
                    mall_roster = mall_directory(tenants, retail_candidates, label or "")
                    neighbors = []
                    for buildings in building_layers:
                        if buildings is None:
                            continue
                        boxes = buildings.boxes(frame)
                        near = (np.all(boxes[:,:2] <= ring.max(0)+origin+50,axis=1) &
                                np.all(boxes[:,2:] >= ring.min(0)+origin-50,axis=1) &
                                (buildings.ids != host.building_id))
                        for j in np.flatnonzero(near):
                            neighbors.extend(part[0] for part in buildings.parts(j) if part and len(part[0])>=3)
                    banners = mall_tenant_banners(coords,retail_candidates,panels,parking_points,walk_segments,neighbors,
                                                 enclosed=enclosed)
                    if label and spec.height >= 5.5 and walk_segments is not None:
                        entrances = mall_entrance_panels(coords,walk_segments,panels+banners)
                        if not entrances and mall_roster and banners:
                            # Reserve one mapped entrance when banners would occupy every approach.
                            entrances = mall_entrance_panels(coords,walk_segments,panels)[:1]
                            if entrances:
                                center = entrances[0].mean(0)
                                banners = [(n,p) for n,p in banners if dnp.norm((p.mean(0)-center)*scale)>=22]
                        panels.extend((LocalSign(label,"entrance"),p) for p in entrances)
                    panels.extend(banners)
                if spec.retail_front and not spec.retail_doors:
                    centers = [panel.mean(0) for _,panel in panels]
                    spec.retail_doors = entrance_offsets(coords, ring, centers)
                matches = ((brand, panel, None, None) for brand, panel in panels)
            if chain_panels:
                matches = list(matches)
                scale = metres_per_degree(coords[0,1])
                for brand, panel, _, _ in chain_panels:
                    if all(brand != other and dnp.norm((panel.mean(0)-geom.mean(0))*scale) > 4
                           for other,geom,_,_ in matches):
                        matches.append((brand,panel,None,None))
            prior_models = len(existing)
            for index,(brand, geometry, heading, finish) in enumerate(matches):
                options = {}
                if isinstance(brand, LocalSign):
                    if spec.height < 2.8:
                        continue
                    # An inferred tenant row can establish a category without establishing an exact unit.
                    if ((not mall and label.casefold() == brand.name.casefold())
                            or index < len(exterior)
                            or any(tenant == brand and wall is geometry for tenant, wall in verified)):
                        options["mapped_name"] = True
                    if heading is None:
                        options["local_panel"] = (brand.style, round(min(4.2, spec.height - .2), 1))
                    else:
                        options["category"] = brand.style
                    if brand.style != "venue" and (signed_wall is not None or exterior):
                        options["local_panel"] = (brand.style, round(spec.height+.65, 2), "raised")
                    if brand.style != "venue" and (signed_wall is not None or exterior) and layers:
                        directory = suite_directory(layers,geometry.mean(0),brand.name)
                        if directory:
                            options["local_panel"] += (directory,)
                    if (brand.style == "bank" and name == brand and bank_lanes and not venue_only and not mall
                            and (kind in {"bank", "credit_union", "bank_or_credit_union"}
                                 or label.casefold() == brand.name.casefold())
                            and spec.height >= 4.2):
                        placement = drive_through_fixtures(ring, bank_lanes(), left_hand)
                        if placement:
                            wall, positions = placement
                            end = (wall+1) % len(ring)
                            axis = coords[end]-coords[wall]
                            mid = coords[wall]+np.mean(positions)*axis
                            half = min(abs(positions[1]-positions[0])/2,
                                       4./dnp.norm(ring[end]-ring[wall]))
                            winding = np.sign(np.sum(ring[:,0]*np.roll(ring[:,1],-1)-ring[:,1]*np.roll(ring[:,0],-1)))
                            canopy = mid + winding*np.array([-half, half])[:, None]*axis
                            options["drive_through"] = canopy.round(7).tolist()
                    brand = brand.name
                if mall:
                    panel_xy = np.asarray(frame.geodetic_to_enu(geometry[:,1],geometry[:,0],np.zeros(2))[:2]).mean(1)
                    # Banner spacing was checked against panel widths during layout.
                    # Keep the exclusion around independently placed models.
                    limit = prior_models if options.get("local_panel", ("",))[0] == "banner" else len(existing)
                    if any(dnp.norm(panel_xy-existing[j]["xy"]) < 15 for j in range(limit)):
                        continue
                    # An exterior sign is justified by the wall evidence, independently of the brand.
                    style = options.get("local_panel", ("brand",))[0]
                    options["local_panel"] = ("mall_"+style, mall_sign_top(spec.height))
                    if style == "entrance":
                        options["local_panel"] += (mall_roster,)
                if carhop:
                    options["carhop"] = True
                elif heading is not None and load_drive_lanes and identity_for(brand, options.get("category", "shop")).style in DINING_STYLES:
                    if drive_lanes is None:
                        drive_lanes = load_drive_lanes()
                    placement = drive_through_fixtures(ring, drive_lanes, left_hand)
                    if placement:
                        wall, along = placement
                        end = (wall + 1) % len(ring)
                        locations = coords[wall] + along[:, None] * (coords[end] - coords[wall])
                        if not any(distance_to_wall((np.asarray(p)-coords[wall])*metres_per_degree(coords[wall,1]),
                                                    (locations-coords[wall])*metres_per_degree(coords[wall,1])) < 2.5 for p in doors):
                            options["drive_through"] = locations.round(7).tolist()
                entry = cached_entry(cache, brand, geometry, heading, finish, doors=doors, entrance_bearing=entrance_bearing, **options)
                if signed_wall is not None:
                    spec.front = signed_wall
                if index < len(exterior):
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
                "Could not generate storefront for %s: %s", host.building_id, error
            )

    if local_enabled:
        yield from hosted_bank_services(items, existing, frame, origin, place_layers, cache)


def hosted_bank_services(items, existing, frame, origin, place_layers, cache):
    """An ATM or tenant branch needs a pin at an explicitly mapped exterior entrance."""
    for spec, ground_min, ground_max in items:
        host = spec.storefront
        if not host or not host.source_record or spec.min_h or spec.holes or spec.deck or ground_max-ground_min > 3:
            continue
        if host.source_record.partition("@")[0] not in entrance_index():
            continue
        ring = np.asarray(spec.ring)[:, :2]
        xy = ring + origin
        lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
        coords = np.column_stack((lon, lat))
        doors = mapped_doors(host.source_record, coords, explicit_only=True)
        if not doors:
            continue
        scale = metres_per_degree(coords[0, 1])
        emitted = set()
        for place in trading_places(place_layers(), coords.min(0)-1/scale, coords.max(0)+1/scale):
            atm = "atm" in place.categories or place.name.casefold().endswith(" atm")
            sign = local_sign(place.name, place.categories)
            if not atm and (not sign or sign.style != "bank"):
                continue
            if not any(dnp.norm((place.point-door)*scale) <= 3 for door in doors):
                continue
            panel = parking_frontage(coords, place.point, (), entrances=doors)
            if panel is None:
                continue
            title = "ATM" if atm else sign.name
            center = panel.mean(0)
            location = np.array(frame.geodetic_to_enu(center[1], center[0], 0.)[:2])
            match_key = source_key(title)
            if any((row.get("local") or {}).get("storefront_match") == match_key and
                   (dnp.norm(location-row["xy"]) <= 12 or
                    (not atm and np.all(row["xy"] >= xy.min(0)) and np.all(row["xy"] <= xy.max(0))))
                   for row in existing):
                continue
            key = (title, tuple(center.round(6)))
            if key in emitted:
                continue
            emitted.add(key)
            try:
                entry = cached_entry(cache, title, panel, None, None,
                                     local_panel=("atm", 2.1) if atm else ("bank", 3.4), mapped_name=not atm)
            except (OSError, ValueError, RuntimeError) as error:
                if isinstance(error, OSError):
                    revisit.fault()
                LOG.warning("Could not generate bank service at %s: %s", host.building_id, error)
                continue
            x, y, _ = frame.geodetic_to_enu(entry["lat"], entry["lon"], 0.)
            yield {"id": "auto_" + Path(entry["file"]).stem, "xy": (float(x), float(y)),
                   "latlon": (entry["lat"], entry["lon"]), "source": "local",
                   "local": dict(entry, file="storefronts/"+entry["file"]),
                   "storefront_ground": ground_max, "storefront_host": None}


def mall_sign_top(wall_height):
    """Top of a mall sign above its supporting wall's base, in metres."""
    # Keep the existing roof clearance on low storefronts. Above 6 m, reserve
    # one third of the additional wall for breathing room below the roofline.
    roof_clearance = .45 + max(0., wall_height - 6.) / 3
    return round(min(wall_height - roof_clearance, 9.), 2)


def multipart_mall_wall(panel, walls, obstacles, paths):
    """Find one exposed, drawn wall supporting the complete sign and a public approach."""
    if not len(paths):
        return None
    starts = np.concatenate(obstacles)
    ends = np.concatenate([np.roll(ring, -1, axis=0) for ring in obstacles])
    path_start = paths[:, 0]
    delta = paths[:, 1] - path_start
    length2 = np.maximum(np.sum(delta * delta, axis=1), 1e-12)
    matches = []
    for ring, ground, height in walls:
        for left, right in zip(ring, np.roll(ring, -1, axis=0)):
            edge = right - left
            length = dnp.norm(edge)
            if length < 5:
                continue
            axis = edge / length
            normal = np.array([axis[1], -axis[0]])
            along = dnp.matmul(panel - left, axis)
            if (along[0] < .2 or along[1] > length - .2 or along[1] - along[0] < 3
                    or np.max(np.abs(dnp.matmul(panel - left, normal))) > .5):
                continue
            snapped = left + along[:, None] * axis
            center = snapped.mean(0) + normal * .30
            if any(contains_points(o, center[None])[0] for o in obstacles):
                continue
            frac = np.clip(np.sum((center - path_start) * delta, axis=1) / length2, 0, 1)
            approaches = path_start + frac[:, None] * delta
            ahead = approaches - center
            depth = dnp.matmul(ahead, normal)
            near = (depth >= 1) & (depth <= 45) & (np.abs(dnp.matmul(ahead, axis)) <= 15)
            if not open_mall_approach(center, approaches[near], starts, ends):
                continue
            matches.append((snapped, ground, height))
    if not matches:
        return None
    # Grow the usable face from its lowest full-width wall. Duplicate or overlapping
    # records describe the same surface. A gap or change of plane ends that surface.
    matches.sort(key=lambda row: row[1])
    face, base, height = matches[0]
    top = base + height
    for upper_face, z, h in matches[1:]:
        if z > top + .02:
            break
        if not np.allclose(upper_face, face, rtol=0, atol=.02):
            # Ambiguous at the same level. Above it the lower wall is still usable.
            top = min(top, z)
            break
        top = max(top, z + h)
    height = top - base
    if height < 3.5:
        return None
    return face, base, height


def multipart_mall_models(hosts, items, existing, frame, origin, md, keys, cache_dir, aisles, load_walks, building_layers):
    """Sign overlays on corroborated multipart malls without changing any building spec."""
    from buildings import base_z, drawn_ring

    local = os.environ.get("BNE_LOCAL_BUSINESS_SIGNS") == "1"
    road_paths = [line for line in aisles if len(line) > 1]
    walks = [line for line in load_walks() if len(line) > 1]
    if not road_paths and not walks:
        return
    paths = np.concatenate([np.stack((line[:-1], line[1:]), axis=1) for line in road_paths + walks])
    origin = np.asarray(origin, np.float64)

    def to_lonlat(points):
        shape = points.shape
        xy = points.reshape(-1, 2) + origin
        lat, lon, _ = frame.enu_to_geodetic(xy[:,0], xy[:,1], np.zeros(len(xy)))
        return np.column_stack((lon, lat)).reshape(shape)

    def to_tile(points):
        x, y, _ = frame.geodetic_to_enu(points[:,1], points[:,0], np.zeros(len(points)))
        return np.column_stack((x, y)) - origin

    walking = (to_lonlat(np.concatenate([np.stack((line[:-1], line[1:]), axis=1) for line in walks]))
               if walks else np.empty((0,2,2)))
    layers = list(businesses(md, keys))
    cache = Path(cache_dir) / "threedmr_cache" / "storefronts"
    for building_id, (label, source, outline) in hosts.items():
        walls = []
        for spec, lo, hi in items:
            if spec.mall_sign_parent != building_id or spec.deck or spec.holes or spec.portal or spec.ship or spec.fr_house:
                continue
            height = float(spec.height)
            if height > 0:
                walls.append((np.asarray(drawn_ring(spec))[:, :2], base_z(spec, lo, hi) + float(spec.min_h or 0), height))
        if not walls:
            continue
        coords = to_lonlat(outline)
        mall_kind = mapped_mall(layers, coords, label, multipart=True)
        if not mall_kind:
            continue

        doors = mapped_doors(source, coords, explicit_only=True)
        # Confirmed exterior tenants have first claim on their mapped walls.
        access = exterior_access_index().get(source.partition("@")[0])
        panels = confirmed_mall_panels(layers, coords, access, doors)
        verified_count = len(panels)
        if not local:
            panels = [(name, panel) for name, panel in panels if isinstance(name, str)]

        neighbors = []
        low, high = outline.min(0) + origin - 50, outline.max(0) + origin + 50
        for features in building_layers:
            if features is None:
                continue
            bounds = features.boxes(frame)
            near = (features.ids != building_id) & np.all(bounds[:,:2] <= high, axis=1) & np.all(bounds[:,2:] >= low, axis=1)
            for i in np.flatnonzero(near):
                neighbors.extend(part[0][:-1] for part in features.parts(i) if part and len(part[0]) >= 4)
        obstacles = [outline, *[ring for ring, _, _ in walls], *[to_tile(p) for p in neighbors]]
        if local:
            panels.extend((LocalSign(label, "entrance"), panel) for panel in mall_entrance_panels(coords, walking, panels))
            candidates = []
            enclosed = mall_kind in {"mall", "shopping_mall"} or label.casefold().endswith(" mall")
            tenants = mapped_tenants(layers, coords, local=True, retail_candidates=candidates, enclosed=enclosed)
            roster = mall_directory(tenants, candidates, label)
            parking = to_lonlat(np.concatenate(road_paths)) if road_paths else None
            panels.extend(mall_tenant_banners(coords, candidates, panels, parking, walking, neighbors,
                                              enclosed=enclosed))

        used = set()
        for index, (name, panel) in enumerate(panels):
            style = "mall_" + name.style if isinstance(name, LocalSign) else "mall_brand"
            title = name.name if isinstance(name, LocalSign) else name
            if title.casefold() in used and style != "mall_entrance":
                continue
            supported = multipart_mall_wall(to_tile(panel), walls, obstacles, paths)
            if supported is None:
                continue
            snapped, ground, height = supported
            xy = snapped.mean(0) + origin
            if any(dnp.norm(xy - row["xy"]) < 15 for row in existing):
                continue
            try:
                panel_options = (style, mall_sign_top(height))
                if style == "mall_entrance":
                    panel_options += (roster,)
                entry = cached_entry(cache, title, to_lonlat(snapped), None, None, local_panel=panel_options,
                                     mapped_name=isinstance(name, LocalSign) and index < verified_count)
            except (OSError, ValueError, RuntimeError) as error:
                if isinstance(error, OSError):
                    revisit.fault()
                LOG.warning("Could not generate multipart mall sign for %s: %s", building_id, error)
                continue
            x, y, _ = frame.geodetic_to_enu(entry["lat"], entry["lon"], 0.0)
            used.add(title.casefold())
            yield {"id": "auto_" + Path(entry["file"]).stem, "xy": (float(x), float(y)),
                   "latlon": (entry["lat"], entry["lon"]),
                   "local": dict(entry, file="storefronts/" + entry["file"]), "source": "local",
                   "storefront_ground": ground, "storefront_host": None}


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
