"""Offline French house scenery zones, using published administrative polygons.

These are representative architectural presets, not surveyed house styles.
Departments are a practical zoning approximation; the Basque Country and
Toulouse use their own intercommunal areas, and Alpine presets require altitude.
"""
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import zipfile

import countrybounds

DATA = Path(__file__).with_name("data") / "fr_house_regions.zip"
DATA_SHA256 = "18db140bedce39d9eef1dd9e899eee305dee5c823b9c3e792e1683969991e86c"
VERSION = "fr-house-regions-v1"


class AreaIndex:
    def __init__(self, geojson, label=None):
        self.parts, self.buckets = [], {}
        for f in geojson["features"]:
            tag = label or f["properties"]["code"]
            geom = f["geometry"]
            polygons = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
            for polygon in polygons:
                rings = tuple(countrybounds._Ring(r) for r in polygon)
                i = len(self.parts)
                self.parts.append((tag, rings))
                for k in range(math.floor(rings[0].lo[1] * 4), math.floor(rings[0].hi[1] * 4) + 1):
                    self.buckets.setdefault(k, []).append(i)

    def at(self, lat, lon):
        found, edge = set(), False
        for i in self.buckets.get(math.floor(lat * 4), ()):
            tag, rings = self.parts[i]
            outer = rings[0].locate(lon, lat)
            if outer < 0:
                continue
            holes = [r.locate(lon, lat) for r in rings[1:]]
            edge |= outer == 0 or 0 in holes
            if outer == 1 and not any(h >= 0 for h in holes):
                found.add(tag)
        return next(iter(found)) if len(found) == 1 and not edge else None


@lru_cache(maxsize=1)
def metadata():
    if hashlib.sha256(DATA.read_bytes()).hexdigest() != DATA_SHA256:
        raise ValueError("French house region data checksum mismatch")
    with zipfile.ZipFile(DATA) as z:
        return json.loads(z.read("sources.json"))


@lru_cache(maxsize=3)
def _index(member):
    metadata()
    with zipfile.ZipFile(DATA) as z:
        return AreaIndex(json.loads(z.read(member + ".geojson")), None if member == "departments" else member)


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
