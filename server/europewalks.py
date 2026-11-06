"""Street-edge profiles for European scenery, using mapped paths before estimates."""

from functools import lru_cache
import math
import os
from typing import NamedTuple

import numpy as np

VERSION = "european-street-edges-v3"
STREETS = {"primary", "secondary", "tertiary", "unclassified", "residential", "living_street"}
VILLAGE_WIDTH = dict(primary=6.1, secondary=5.8, tertiary=5.2, unclassified=4.9,
                     residential=4.6, living_street=4.0)
PLACE_RADIUS = dict(city=7200., town=3200., village=1250., hamlet=650.)
LOW_EDGE_HEIGHT = .045

# Defaults for unknown surfaces, weighted by repetition. Reference photos
# inform the palette; these weights are scenery choices, not survey statistics.
PALETTES = {
    "ES": ("slabs", "pavers", "pavers", "concrete", "light_asphalt"),
    "FR": ("asphalt", "asphalt", "asphalt", "light_asphalt", "concrete", "slabs"),
    "GB": ("asphalt", "asphalt", "asphalt", "light_asphalt", "slabs"),
    "IE": ("asphalt", "asphalt", "concrete", "light_asphalt"),
    "IT": ("asphalt", "light_asphalt", "slabs", "pavers", "stone"),
    "NL": ("slabs", "slabs", "slabs", "brick", "pavers"),
    "PL": ("pavers", "slabs", "pavers_mixed", "pavers_mixed"),
    "PT": ("stone", "stone", "slabs", "pavers", "light_asphalt"),
    "RO": ("pavers", "pavers", "pavers", "concrete", "light_asphalt", "asphalt"),
}
MAPPED_SURFACE = {
    "asphalt": "asphalt", "chipseal": "light_asphalt", "concrete": "concrete",
    "concrete:plates": "slabs", "paving_stones": "pavers", "sett": "stone",
    "interlocking_pavers": "pavers", "interlocking_stones": "pavers",
    "cobblestone": "stone", "unhewn_cobblestone": "stone", "bricks": "brick",
    "compacted": "gravel", "fine_gravel": "gravel", "gravel": "gravel",
    "dirt": "gravel", "ground": "gravel", "unpaved": "gravel",
}


class Walk(NamedTuple):
    width: float
    setback: float
    height: float
    surface: str


class Street(NamedTuple):
    left: Walk | None
    right: Walk | None
    parking: float
    village: bool

    def side(self, side):
        return self.left if side > 0 else self.right

    def walks(self):
        return tuple((side, walk) for side, walk in ((1, self.left), (-1, self.right)) if walk is not None)

    @classmethod
    def from_json(cls, value):
        left, right, parking, village = value
        return cls(Walk(*left) if left else None, Walk(*right) if right else None, parking, village)


def enabled():
    return os.environ.get("BNE_EU_ROAD_DETAILS", "off").lower() in {"1", "on", "true"}


@lru_cache(maxsize=128)
def _places(x, y):
    import townsigns

    lon, lat = x / 20., y / 20.
    pad_lon = .075 / max(.17, math.cos(math.radians(lat)))
    return tuple(townsigns.bundled_europe().nearby(
        (lon - pad_lon, lat - .075, lon + .05 + pad_lon, lat + .125), places=True))


def _locality(country, lon, lat):
    scale = 111_195. * math.cos(math.radians(lat))
    candidates = []
    for place in _places(math.floor(lon * 20), math.floor(lat * 20)):
        kind = place.get("place")
        if kind not in PLACE_RADIUS or place.get("country") not in (None, country):
            continue
        dist = math.hypot((lon - place["lon"]) * scale, (lat - place["lat"]) * 111_195.)
        if dist <= PLACE_RADIUS[kind]:
            candidates.append((dist / PLACE_RADIUS[kind], str(place["name"]), kind))
    return min(candidates, default=(0., "", ""))[1:]


def country_for(lon, lat):
    from countrybounds import bundled
    from europeinfra import COUNTRIES

    # Covers the supported territories and the eastern end of Russia.
    if lat < 25. or -168. < lon < -33.:
        return None
    country = bundled().at(lat, lon)
    return country if country in COUNTRIES else None


def grassy_verges(country, lon, lat):
    """Rural streets whose roadside is normally open, even in built-up villages."""
    return country in {"RO", "HU"} and _locality(country, lon, lat)[1] not in {"city", "town"}


def finish_width(spec, width, *, measured_walk=None):
    """Use only spare space already present in a known or measured carriageway."""
    import parking
    import roads

    edge = spec.street
    nominal = roads.carriageway(spec.cls, spec.kind, True, None, spec.oneway, spec.lanes)
    nominal -= roads.assumed_parking(spec)
    curbside = edge.left is not None and edge.right is not None and edge.left.setback == edge.right.setback == 0.
    reserve = parking.SPOT_W_M if (spec.urban and not spec.oneway and not edge.village
                                   and spec.cls in parking.PARK_CLASSES and curbside
                                   and width - nominal >= 2. * parking.SPOT_W_M - 1e-6) else 0.
    if measured_walk is not None:
        # The wall-to-wall estimate has equal edges. Apply it only to
        # attached paths and cap the nominal pedestrian width at 3 m.
        w = min(3., max(.85, measured_walk))
        edge = edge._replace(left=edge.left._replace(width=w) if edge.left else None,
                             right=edge.right._replace(width=w) if edge.right else None)
    edge = edge._replace(parking=reserve)
    attached = [walk.width for _, walk in edge.walks() if walk.setback == 0.]
    return spec._replace(width=width, street=edge, walk=max(attached, default=spec.walk))


def village_walks(spec, country, sidewalk, frontage=(0., 0.), infer=False, buildings=None):
    """Choose each verge separately. OSM's explicit sides take precedence."""
    import roads

    sides = roads.SIDEWALK_TAG.get(sidewalk)
    main = spec.cls in {"primary", "secondary"}
    if country == "FR":
        close, near, along = np.asarray(buildings).T if buildings is not None else (
            (0., 0.), frontage, frontage)
        if sides is None:
            # A village can have a dense street, a flush paved edge in front
            # of homes with setbacks, and an open verge, within one block.
            sides = tuple(side for i, side in enumerate((1, -1))
                          if (buildings is not None and (near[i] >= .23 or (near[i] >= .12 and along[i] >= .34)))
                          or (buildings is None and infer and near[i] >= .50))
            if not sides and sidewalk == "yes":
                sides = (1 if near[0] > near[1] else -1,)
        result = []
        for i, side in enumerate((1, -1)):
            raised = close[i] >= .26 or (buildings is None and near[i] >= .75)
            walk = (Walk(1.35, 0., .11, "asphalt") if raised
                    else Walk(1.35, 0., LOW_EDGE_HEIGHT, "light_asphalt"))
            result.append(walk if side in sides else None)
        return tuple(result)

    if sides is None:
        if country == "RO":
            # WorldCover often counts the whole roadside as developed. A
            # through road alone is insufficient to add a path to its verge.
            if buildings is not None:
                frontage = tuple(side[1] for side in buildings)
            strong = (buildings is not None and main and max(frontage) >= .52
                      and abs(frontage[0] - frontage[1]) >= .16)
            infer_path = strong
        else:
            infer_path = infer and main and (spec.urban or max(frontage) >= .15)
        sides = (1 if frontage[0] > frontage[1] + .05 else -1,) if sidewalk == "yes" or infer_path else ()
    walk = Walk(1.12, .9, LOW_EDGE_HEIGHT, "pavers" if country in {"RO", "PL", "NL"} else "concrete")
    return tuple(walk if side in sides else None for side in (1, -1))


def street_surface(country, name, cls, lon, lat, locality=None):
    """Stable pavement choice, including on the stock profiles of divided streets."""
    import furniture

    if locality is None:
        locality, _ = _locality(country, lon, lat)
    token = (country, locality, name or (round(lon, 2), round(lat, 2)), cls)
    palette = PALETTES.get(country, ("asphalt", "asphalt", "light_asphalt", "concrete", "pavers"))
    return palette[int(furniture._h01("walk-surface", *token) * len(palette))]


def plan(spec, country, lon, lat, name, sidewalk, mapped_width, frontage, infer):
    """Choose street width and one layout per side of an ordinary European road."""
    import roads

    locality, kind = _locality(country, lon, lat)
    village = kind in ("village", "hamlet")
    raw = sidewalk.strip().lower()
    sides = roads.SIDEWALK_TAG.get(raw)
    if not (spec.urban or village or sides is not None or raw == "yes"):
        return spec

    width = spec.width if mapped_width is None else mapped_width
    if mapped_width is None:
        if village:
            if spec.oneway or spec.lanes == 1:
                width = max(3.1, (spec.lanes or 1) * 3.)
            elif (spec.lanes or 2) > 2:
                width = spec.lanes * 3.
            else:
                width = VILLAGE_WIDTH[spec.cls]
        elif spec.urban:
            width -= roads.assumed_parking(spec)

    if village:
        left, right = village_walks(spec, country, raw, frontage, infer)
    else:
        surface = street_surface(country, name, spec.cls, lon, lat, locality)
        detached = (country in {"GB", "IE"} and spec.cls == "residential"
                    and width <= 5.5 and (sides or (spec.urban and max(frontage) >= .2)))
        if sides is None and (spec.urban or raw == "yes"):
            sides = (1, -1) if raw != "yes" or abs(frontage[0] - frontage[1]) < .3 else (
                1 if frontage[0] > frontage[1] else -1,)
        walk = (Walk(1.45, 1.25, .035, "asphalt") if detached else
                Walk(1.95 if spec.cls in {"primary", "secondary"} else 1.65, 0., .13, surface))
        sides = sides or ()
        left, right = (walk if side in sides else None for side in (1, -1))
    street = Street(left, right, 0., village)
    return finish_width(spec._replace(street=street), width)


def mapped_walk(feats, i):
    """Profile of a sidewalk mapped as its own line."""
    import mapgeom
    import roads

    classes, subclasses = feats.attrs.get("class"), feats.attrs.get("subclass")
    if classes is None or str(classes[i]) != "footway" or int(feats.kind[i]) != mapgeom.LINE:
        return None
    footways = feats.attrs.get("osm_footway")
    if not ((subclasses is not None and str(subclasses[i]) == "sidewalk")
            or (footways is not None and str(footways[i]) == "sidewalk")
            or roads._rule_value(feats.rules.get("subclass"), i) == "sidewalk"):
        return None
    if roads.unbuilt(feats, i):
        return None
    a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
    b = feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]
    if b <= a or country_for(*feats.lonlat[(a + b) // 2]) is None:
        return None
    width = roads._osm(feats, "width", i, .55, 12.)
    if width is None:
        width = roads._rule_value(feats.rules.get("width"), i)
    width = float(width) if width is not None and .55 <= float(width) <= 12. else 1.4
    raw = str(roads._surface_value(feats, i) or "").strip().lower()
    surface = MAPPED_SURFACE.get(raw, MAPPED_SURFACE.get(roads.surface_token(raw), "light_asphalt"))
    return Walk(width, .2, .035, surface)


def mapped_walks(frame, cells, near):
    """Ground sidewalk lines already present in Overture's road layer."""
    import mapgeom
    import roads

    result = {}
    for feats in cells:
        if feats is None or not len(feats):
            continue
        boxes = feats.boxes(frame)
        x0, y0, x1, y1 = near
        eligible = np.flatnonzero((boxes[:, 2] >= x0) & (boxes[:, 0] <= x1)
                                  & (boxes[:, 3] >= y0) & (boxes[:, 1] <= y1))
        for i in eligible:
            fid = str(feats.ids[i])
            if fid in result:
                continue
            walk = mapped_walk(feats, i)
            if walk is None:
                continue
            for ring in roads.ground_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                points = np.c_[e, n, np.zeros(len(e))]
                for line in roads.clip_polyline(points, *near):
                    result.setdefault(fid, []).append((line, walk))
    return [(fid, line, walk) for fid, paths in result.items() for line, walk in paths]
