"""Offline, position-based country lookup (PR #505's regional scenery; independent of roadside model selection).

The borders are a band file (tools/build_regional_data.py: Overture divisions' territories, including dependencies,
simplified to ~10 m), fetched once from BeamNG.Earth-data (regionaldata.file) when the regional scenery is on; the
lookup is native (earthfast.country_index: countrymap.rs decodes only the latitude bands asked for). No hit, or
parts of two different territories overlapping, returns None. There is deliberately no nearest-airport fallback:
without the data (offline before its first download) every lookup is None and the stock scenery stays.
"""
from functools import lru_cache
import logging

import numpy as np

import fast

log = logging.getLogger("countrybounds")

FILE = "country_bands.bin"
ALGORITHM = "point-country-bands-v1"


class BandIndex:
    """The territories of a band file: at(lat, lon), codes_at([(lat, lon), ...])."""

    def __init__(self, path):
        fast.require(154, "country_index")
        self.path = str(path)
        self.codes = [None] + list(fast._ef.country_codes(self.path))

    def codes_at(self, points):
        pts = np.asarray(points, np.float64).reshape(-1, 2)
        if not len(pts):
            return []
        ix = fast._ef.country_index(self.path, np.ascontiguousarray(pts[:, 0]), np.ascontiguousarray(pts[:, 1]))
        return [self.codes[i] for i in ix.tolist()]

    def at(self, lat, lon):
        try:
            return self.codes_at([(float(lat), float(lon))])[0]
        except (TypeError, ValueError):
            return None


class Absent:
    """No border data (not downloaded yet, offline): unknown everywhere."""
    codes = [None]

    def codes_at(self, points):
        return [None] * len(points)

    def at(self, lat, lon):
        return None


_ABSENT = Absent()


@lru_cache(maxsize=1)
def _loaded(sig):
    import regionaldata
    path = regionaldata.file(FILE)
    if path is None:
        log.warning("country borders (%s) unavailable: the regional scenery keeps the stock models", FILE)
        return _ABSENT
    return BandIndex(path)


def bundled():
    """The border index of the regional data build in use (Absent until one is reachable)."""
    import regionaldata
    return _loaded(regionaldata.signature())


def signature():
    """The border data in use (cache keys)."""
    import regionaldata
    return ALGORITHM + ":" + regionaldata.signature()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lat", type=float)
    parser.add_argument("lon", type=float)
    args = parser.parse_args()
    print(bundled().at(args.lat, args.lon) or "unknown")
    print(signature())
