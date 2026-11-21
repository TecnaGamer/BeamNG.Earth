"""Compact index of mapped OSM advertising totems, shared by all streamed road tiles."""
import csv
import logging
from bisect import bisect_left
from functools import lru_cache
from pathlib import Path

import numpy as np

from geotiles import box_owns
from store_match import chain, sign_endpoints


@lru_cache(maxsize=1)
def rows():
    try:
        with (Path(__file__).parent / "data" / "storefront_totems.csv").open(encoding="utf8", newline="") as source:
            return sorted((float(r["lat"]), float(r["lon"]), r["node"], r["brand"],
                           float(r["height"]), float(r["direction"]) if r["direction"] else None)
                          for r in csv.DictReader(source))
    except (OSError, ValueError, KeyError) as error:
        logging.getLogger("earthd.storefronts").warning("Mapped sign index unavailable: %s", error)
        return []


def matches(keys, frame, box, streets, existing):
    index = rows()
    x0, y0 = box[:2]
    for key in set(keys):
        west, south, east, north = key.bounds()
        for lat, lon, node, label, height, facing in index[bisect_left(index, (south,)):bisect_left(index, (north,))]:
            if not west <= lon < east:
                continue
            x, y, _ = frame.geodetic_to_enu(lat, lon, 0.)
            if not box_owns(box, x, y):
                continue
            if any((c.get("local") or {}).get("storefront_pylon") and
                   np.hypot(x-c["xy"][0], y-c["xy"][1]) < 1. for c in existing):
                continue
            name = chain(label)
            if name is None:
                continue
            point = np.array([x-x0, y-y0])
            endpoints = sign_endpoints(np.array([lon, lat]), height, facing, (road-point for road in streets))
            if endpoints is not None:
                yield node, name, endpoints, height, (x, y), (lat, lon)
