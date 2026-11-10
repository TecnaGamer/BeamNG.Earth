"""Offline French house scenery zones, using published administrative polygons.

These are representative architectural presets, not surveyed house styles.
Departments are a practical zoning approximation; the Basque Country and
Toulouse use their own intercommunal areas, and Alpine presets require altitude.
"""
from functools import lru_cache
import json
import math
from pathlib import Path

import countrybounds

DATA = Path(__file__).with_name("data")
VERSION = "fr-house-regions-v2"     # 2: band files (tools/build_regional_data.py --house-regions), the native lookup


@lru_cache(maxsize=1)
def metadata():
    return json.loads((DATA / "fr_house_regions.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=3)
def _index(member):
    return countrybounds.BandIndex(DATA / f"fr_house_{member}.bin")


def profile(lat, lon, altitude=None):
    if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-5.3 < lon < 9.8 and 41.3 < lat < 51.2) or countrybounds.bundled().at(lat, lon) != "FR":
        return None
    meta = metadata()
    for special in ("basque", "toulouse"):
        x0, y0, x1, y1 = meta["bounds"][special]
        if x0 <= lon <= x1 and y0 <= lat <= y1 and _index(special).at(lat, lon):
            return special
    dep = _index("departments").at(lat, lon)
    if dep in {"67", "68"}:
        return "alsace"
    if dep in {"22", "29", "35", "56"}:
        return "brittany"
    if dep in {"14", "27", "50", "61", "76"}:
        return "normandy"
    if dep in {"02", "59", "62", "80"}:
        return "north"
    if dep in {"75", "77", "78", "91", "92", "93", "94", "95"}:
        return "paris_basin"
    if dep in {"04", "05", "06", "38", "73", "74"} and altitude is not None and altitude >= 650.:
        return "alpine"
    if dep in {"09", "64", "65"} or (dep == "66" and altitude is not None and altitude >= 650.):
        return "pyrenees"
    if dep in {"04", "05", "06", "11", "13", "2A", "2B", "30", "34", "66", "83", "84"}:
        return "mediterranean"
    if dep in {"16", "17", "31", "32", "33", "40", "47", "81", "82", "85"}:
        return "southwest"
    if dep in {"28", "37", "41", "44", "45", "49", "53", "72", "79", "86"}:
        return "loire"
    if dep in {"08", "10", "21", "25", "38", "39", "51", "52", "54", "55", "57", "70", "73", "74", "88", "89", "90"}:
        return "east"
    return "central"
