"""Conservative sign-facing geometry for Norwegian NVDB signs.

Input points are two-dimensional local ENU coordinates (metres):
``point`` is the physical Skiltpunkt, ``road_reference`` is the NVDB road
reference coordinate. The result is the unit vector pointing OUT of the
painted sign face, or None when the available evidence is insufficient.

For traffic-direction labels, the sign direction is provisional: the model
uses right-hand Norwegian road traffic and the pole's lateral side, not an
unavailable NVDB metering tangent. It must not be used for safety navigation.
"""
from __future__ import annotations
import math


def facing(point, road_reference, mode, family):
    """Return a normalized 2D outward sign-face vector, or None.

    Supported ``mode`` values correspond to NVDB property 1894.
    A 'tosidig' sign is eligible only for a route-number marker; the mesh
    must create both front and back textured sides separately.
    """
    try:
        x, y = (float(v) for v in point)
        rx, ry = (float(v) for v in road_reference)
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(map(math.isfinite, (x, y, rx, ry))):
        return None
    dx, dy = x - rx, y - ry
    length = math.hypot(dx, dy)
    if not (1.2 <= length <= 25.0):
        return None
    ox, oy = dx / length, dy / length
    mode = str(mode or '').strip().lower()
    if mode == 'parallelt fra veg':
        return (ox, oy)
    if mode == 'parallelt mot veg':
        return (-ox, -oy)
    if mode in ('trafikk i metreringsretning', 'trafikk mot metreringsretning'):
        # The metering direction is not provided here. This is only a
        # provisional road-side inference, as in the working v0.7.2 build.
        return (oy, -ox)
    if mode == 'tosidig' and family == 'route':
        # Faces both travel directions on the road; its in-plane axis is
        # perpendicular to the pole's road-side offset. The mesh supplies
        # the second, oppositely oriented face.
        return (oy, -ox)
    return None
