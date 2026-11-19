"""The grade a road may hold, by class and speed: the one law everything that climbs or digs a road reads (the shapes'
embankments and cuts, the road laws, and findground's cost of moving a way: which of two ways crossing leaves the
ground is the one whose move costs less earth, by its own grade).

AASHTO's design grades for a speed (4 % at 120 km/h to 15 % at 20; a link at least 6 %), a little more generous for a
game (GAME_GRADE_PLUS: AASHTO's are for a loaded truck); a path 12 %. A track's is railways.GRADE (the caller's: this
module has no railways). Pure."""
import numpy as np

import dnp

PATHS = {"footway", "path", "cycleway", "pedestrian", "steps", "bridleway", "track"}
DEFAULT_KMH = {"motorway": 110, "trunk": 90, "primary": 70, "secondary": 60, "tertiary": 50, "residential": 40,
               "unclassified": 50, "service": 20, "living_street": 15, "road": 40, "track": 20}
LINK_KMH = 60               # km/h of a link (a ramp, a slip road) with no limit mapped: not its road's (110 on a motorway's)
GAME_GRADE_PLUS = 0.02


def base(cls):
    c = cls or "road"
    return c[:-5] if c.endswith("_link") else c


def speed(cls, kmh=None):
    """km/h a road of class cls is driven at: its limit, else its class's (a link's LINK_KMH)."""
    if kmh:
        return kmh
    if (cls or "").endswith("_link"):
        return LINK_KMH
    return DEFAULT_KMH.get(base(cls), 40)


def road_grade(cls, kmh=None):
    """The steepest grade a road of class cls (limit kmh, or its class's) may hold."""
    if cls in PATHS:
        return 0.12
    g = float(dnp.interp(speed(cls, kmh), [20, 30, 40, 50, 60, 80, 100, 120], [0.15, 0.12, 0.10, 0.09, 0.08, 0.06, 0.05, 0.04]))
    if (cls or "").endswith("_link"):
        g = max(g, 0.06)
    return g + GAME_GRADE_PLUS
