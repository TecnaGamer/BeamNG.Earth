"""Shared cliff appearance rules. Eligibility (slope/cover/road support) stays with each generator.

Rules are ordered by explicit priority, then stable key; a higher priority wins.
Contexts distinguish confirmed road cuts from natural faces. Regions are visual
priors, not geological surveys. Add rules here, never in a map-only copy.
"""
from dataclasses import dataclass
import numpy as np

NATURAL, LIMESTONE_CUT, ARID_CUT, CANYON = range(4)
PLATEAU_RING = ((-114.2,35.3),(-112.4,34.3),(-109.1,34.3),(-107.5,36.0),
                (-108.3,39.7),(-111.8,40.2),(-113.7,38.1))
# Conservative appearance envelopes around documented sedimentary exposures.
# Terrain slope, land cover and road exclusions still determine actual coverage.
REGIONS = (
    ("Colorado Plateau", PLATEAU_RING),
    ("Palo Duro Canyon", ((-101.92,35.01),(-101.57,35.04),(-101.12,34.66),
                          (-101.25,34.50),(-101.73,34.78))),
    ("Caprock Canyons", ((-101.18,34.54),(-100.96,34.55),(-100.94,34.39),(-101.16,34.36))),
    ("Kalbarri inland gorges", ((114.20,-27.62),(114.52,-27.61),(114.62,-27.77),
                                (114.49,-27.91),(114.23,-27.84))),
)

@dataclass(frozen=True)
class CliffType:
    key: str
    family: int
    label: str
    color: str
    context: str = "natural"
    priority: int = 0
    regions: tuple = ()
    biomes: tuple = ()
    far_slot: int = -1
    unknown_cover: bool = False

TYPES = (
    CliffType("natural", NATURAL, "Default rock", "#9ec8d5", far_slot=0),
    CliffType("canyon", CANYON, "Layered canyon", "#ed995d", priority=100,
              regions=REGIONS, far_slot=1, unknown_cover=True),
    CliffType("limestone_cut", LIMESTONE_CUT, "Road cut: limestone", "#e7da8a", context="road_cut"),
    CliffType("arid_cut", ARID_CUT, "Road cut: arid rock", "#cf8ddd", context="road_cut",
              priority=100, biomes=(1,)),
)

def contains(lat, lon, ring):
    """Vectorized point-in-polygon for ordinary, non-dateline appearance rings."""
    lat, lon = np.broadcast_arrays(np.asarray(lat, float), np.asarray(lon, float))
    inside = np.zeros(lat.shape, bool)
    for (x0,y0),(x1,y1) in zip(ring, ring[1:]+ring[:1]):
        if y0 != y1:
            inside ^= ((y0 > lat) != (y1 > lat)) & (lon < (x1-x0)*(lat-y0)/(y1-y0)+x0)
    return inside

def select(lat, lon, *, context="natural", biomes=0, rules=None):
    """Select one material family per point; -1 when a context has no matching rule.

    Explicit rules argument supports tests and future registries without global
    mutation. Ascending key breaks equal-priority ties deterministically.
    """
    lat, lon, biomes = np.broadcast_arrays(np.asarray(lat,float), np.asarray(lon,float), np.asarray(biomes))
    result = np.full(lat.shape, -1, np.int16)
    for rule in sorted(TYPES if rules is None else rules, key=lambda r: (r.priority, r.key)):
        if rule.context != context:
            continue
        match = np.isfinite(lat) & np.isfinite(lon)
        if rule.biomes:
            match &= np.isin(biomes, rule.biomes)
        if rule.regions:
            covered = np.zeros(lat.shape, bool)
            for _, ring in rule.regions:
                covered |= contains(lat, lon, ring)
            match &= covered
        result = np.where(match, rule.family, result)
    return result

def compact(families):
    """Keep the common single-family tile on its existing scalar fast path."""
    a = np.asarray(families)
    return int(a.flat[0]) if a.size and np.all(a == a.flat[0]) else a

def far_families(lat, lon, classes, exposed):
    """Map the winning natural family to its far renderer, with cover exclusions."""
    family = select(lat, lon)
    labels = np.full(family.shape, -1, np.int8)
    for rule in TYPES:
        if rule.context == "natural" and rule.far_slot >= 0:
            allowed = np.isin(classes, exposed) | ((np.asarray(classes) == 0) & rule.unknown_cover)
            labels[(family == rule.family) & allowed] = rule.far_slot
    return labels
