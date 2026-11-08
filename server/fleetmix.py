"""Parked and moving model weights from the CBS motor-vehicle fleet of a gemeente (CC BY).

Table 37209hvv, 1 January 2022: cars, vans, lorries (including tractors) and motorcycles registered
in that gemeente. A point in no listed gemeente keeps today's mix. The same models are used; only
the weights change.

    shares(52.37, 4.89)     # {"car", "van", "truck", "motorcycle"} or None
"""

# (name, south, west, north, east, (car, van, truck, motorcycle))
PLACES = (
    ("Amsterdam", 52.278, 4.728, 52.431, 5.079, (249771, 21249, 1695, 20298)),
    ("Leeuwarden", 53.10, 5.65, 53.32, 5.95, (57846, 6119, 608, 4400)),
)
_KEYS = ("car", "van", "truck", "motorcycle")


def shares(lat, lon):
    """The gemeente's fleet shares, or None where this table has no gemeente. Pure."""
    if lat is None or lon is None:
        return None
    lat, lon = float(lat), float(lon)
    for _name, south, west, north, east, counts in PLACES:
        if south <= lat <= north and west <= lon <= east:
            total = float(sum(counts))
            return {key: counts[i] / total for i, key in enumerate(_KEYS)}
    return None
