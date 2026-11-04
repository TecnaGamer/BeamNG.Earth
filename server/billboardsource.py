"""Billboards from OpenStreetMap, for a map cell.

Overture has no billboard class, and release 2026-09-23.1 does not keep advertising=billboard in
source_tags (the cached cells have none). Layercake's POIs file only includes a feature that carries
amenity, shop, tourism and the like, so a plain advertising=billboard node is not in it either.
The points here are an ODbL extract of that tag (nodes, and way centres) for southern California,
kept beside this module and filtered to the cell. © OpenStreetMap contributors.
"""
import json
import logging
import os

import numpy as np

import mapgeom

log = logging.getLogger("earthd.billboards")

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "billboards.npz")


class Source:
    """Billboard points of a map cell, from the bundled extract."""

    def __init__(self, path=PATH):
        self.path = path
        self._lat = self._lon = self._id = None
        self._ready = False

    def cells(self, keys):
        """Features of the billboards in these map cells (empty when the extract is missing)."""
        self._load()
        if self._lat is None or len(self._lat) == 0:
            return []
        found = []
        lat, lon, ids = self._lat, self._lon, self._id
        for key in keys or ():
            lon0, lat0, lon1, lat1 = key.bounds()
            i0 = int(np.searchsorted(lat, lat0, side="left"))
            i1 = int(np.searchsorted(lat, lat1, side="left"))
            if i1 <= i0:
                continue
            sl = slice(i0, i1)
            keep = (lon[sl] >= lon0) & (lon[sl] < lon1)
            if not keep.any():
                continue
            found.append(features_of(ids[sl][keep], lat[sl][keep], lon[sl][keep]))
        return found

    def _load(self):
        if self._ready:
            return
        self._ready = True
        try:
            with np.load(self.path, allow_pickle=False) as z:
                self._lat = np.asarray(z["lat"], np.float64)
                self._lon = np.asarray(z["lon"], np.float64)
                self._id = np.asarray(z["id"], np.int64)
        except FileNotFoundError:
            log.warning("billboards: %s is missing", self.path)
            self._lat = np.zeros(0, np.float64)
        except Exception as ex:
            log.warning("billboards: %s unreadable (%s: %s)", self.path, type(ex).__name__, ex)
            self._lat = np.zeros(0, np.float64)


def _label(fid):
    fid = int(fid)
    kind = "w" if fid < 0 else "n"
    return f"osm-billboard-{kind}{abs(fid):012d}"


def features_of(ids, lat, lon):
    """Features of billboard points. A negative id is a way; the geometry is its centre."""
    ids = np.asarray(ids, np.int64).ravel()
    lat = np.asarray(lat, np.float64).ravel()
    lon = np.asarray(lon, np.float64).ravel()
    if len(ids) == 0:
        return mapgeom.empty("infrastructure")
    labels = [_label(i) for i in ids]
    order = np.argsort(np.array(labels))
    labels = [labels[i] for i in order]
    lat, lon = lat[order], lon[order]
    geoms = [(mapgeom.POINT, [[np.array([[lo, la]], np.float64)]]) for la, lo in zip(lat, lon)]
    feats = mapgeom.build("infrastructure", mapgeom.text_column(labels), geoms)
    tag = json.dumps({"advertising": "billboard"})
    feats.attrs = {
        "class": mapgeom.text_column(["billboard"] * len(labels)),
        "subtype": mapgeom.text_column(["advertising"] * len(labels)),
        "tags": mapgeom.text_column([tag] * len(labels)),
    }
    return feats
