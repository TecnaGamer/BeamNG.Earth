"""OpenStreetMap's mapped driving features that Overture and the Layercake highways file leave out (traffic calming,
turning circles, mini roundabouts, passing places, fords, railway signals / points / buffer stops, and the tracktype /
ford / traffic_calming of the highway ways), from the weekly "drive" shards tools/build_drive_tags.py writes
(BeamNG.Earth-data's GitHub Pages, like the rail and lane shards: railtags.Shards). Read only while one of the
mapped-feature switches that need them is on (drivefeat.SHARDS): the shards' build is then in MapData.versions()
("drive"), so every plan, cell fact and tile built from them is keyed by it.

A shard covers a z7 tile; nodes() of a box reads the one to four it touches. A shard that cannot be had while a build
is known: revisit.fault() (what is built without it is not kept) and None. Not published yet (no manifest): nothing,
no fault, as the other weekly datasets.

BNE_DRIVETAGS_DIR: a local build's folder (its manifest.json and <x>/<y>.json.gz) instead of Pages (tests, and before
the dataset is published; a relative path is the server folder's). Data (c) OpenStreetMap contributors, ODbL 1.0.
"""
import gzip
import json
import logging
import os
import threading

import numpy as np

import railtags

log = logging.getLogger("earthd.drivetags")

NAME = "drive"
SHARD_ZOOM = railtags.SHARD_ZOOM
_here = os.path.dirname(os.path.abspath(__file__))
_dir = os.environ.get("BNE_DRIVETAGS_DIR") or None
LOCAL_DIR = (_dir if os.path.isabs(_dir) else os.path.join(_here, _dir)) if _dir else None


DEFAULT = []                # [the process's DriveTags] (MapData sets it): readers without a MapData (roadcorners' pads)


class DriveTags(railtags.Shards):
    """The "drive" dataset: shards {"nodes": [[id, lon, lat, tags]], "rules": {key: {value: [way ids, delta-coded]}},
    "ways": {}}."""

    def __init__(self, cache_dir, fetcher, local=None):
        super().__init__(cache_dir, fetcher, NAME)
        self.local = local if local is not None else LOCAL_DIR
        self._local_lock = threading.Lock()

    def version(self):
        if self.local is None:
            return super().version()
        if self.built is None:
            try:
                with open(os.path.join(self.local, "manifest.json"), encoding="utf-8") as fh:
                    self.built = "local:" + str(json.load(fh).get("built"))
            except (OSError, ValueError):
                return None
        return self.built

    def known(self):
        return self.version() if self.local is not None else super().known()

    def shard(self, x, y):
        if self.local is None:
            return super().shard(x, y)
        if self.version() is None:
            return None
        with self.lock:
            got = self.shards.get((x, y))
        if got is None:
            try:
                with gzip.open(os.path.join(self.local, str(x), f"{y}.json.gz"), "rt", encoding="utf-8") as fh:
                    got = json.load(fh)
            except FileNotFoundError:
                got = {}
            except (OSError, ValueError) as e:
                log.debug("drive %s/%s: %s", x, y, e)
                return None
            if isinstance(got.get("rules"), dict):
                got["rules"] = railtags.Rules(got["rules"])
            with self.lock:
                self.shards[(x, y)] = got
        return got

    def _shards(self, lat0, lon0, lat1, lon1):
        """The box's shards [(x, y, shard)] in tile order, None when one cannot be had (a fault while a build is
        known), [] when none is published."""
        import mapcells
        if self.version() is None:
            return []
        out = []
        for key in mapcells.cells_for_bbox(lat0, lon0, lat1, lon1, z=SHARD_ZOOM):
            sh = self.shard(key.x, key.y)
            if sh is None:
                import revisit
                revisit.fault()
                return None
            out.append((key.x, key.y, sh))
        return out

    def nodes(self, lat0, lon0, lat1, lon1):
        """[(id, lon, lat, tags)] of the mapped feature nodes inside the box, ascending by id (one order whatever the
        shards' or the box's), None when a shard of it cannot be had. The tags are the shard's: read, do not change."""
        shs = self._shards(lat0, lon0, lat1, lon1)
        if shs is None:
            return None
        got = {}
        for _x, _y, sh in shs:
            for nd in sh.get("nodes") or ():
                try:
                    i, lo, la, t = int(nd[0]), float(nd[1]), float(nd[2]), nd[3]
                except (TypeError, ValueError, IndexError):
                    continue
                if lon0 <= lo <= lon1 and lat0 <= la <= lat1 and isinstance(t, dict):
                    got[i] = (i, lo, la, t)
        return [got[i] for i in sorted(got)]

    def way_values(self, key, way_ids, lat, lon):
        """[value or None] of rule key (tracktype / ford / traffic_calming) for each OSM way id (strings, None
        unknown), from the shard at (lat, lon) and its neighbours' (a way is in the shards its first, middle and last
        nodes fall in); None when the shard there cannot be had."""
        d = 0.02
        shs = self._shards(lat - d, lon - d, lat + d, lon + d)
        if shs is None:
            return None
        out = [None] * len(way_ids)
        for _x, _y, sh in shs:
            rs = sh.get("rules")
            if not isinstance(rs, railtags.Rules):
                continue
            for j, v in enumerate(rs.get_many(key, list(way_ids))):
                if v is not None and out[j] is None:
                    out[j] = v
        return out


def of(md):
    """The map data's DriveTags, or None (none of the switches that read them on, or no map data)."""
    return getattr(md, "drive_tags", None) if md is not None else None


def ways_of(feats):
    """The OSM way id (string) of each feature of a layer (its src_record "w<id>@<v>"), None where none."""
    rec = feats.attrs.get("src_record") if feats is not None else None
    if rec is None:
        return [None] * (len(feats) if feats is not None else 0)
    return [railtags.way_id(str(r)) for r in np.asarray(rec).astype(str)]


# a track's surface where only its tracktype is mapped (BNE_UNPAVED; OpenStreetMap's tracktype: grade1 solid, usually
# paved or heavily compacted; grade2 mostly solid, gravel; grade3 an even mix of hard and soft; grade4 mostly soft,
# soil or grass with some hard material; grade5 soft, soil, sand or grass): roads.surface_kind's tokens
TRACKTYPE_SURFACE = {"grade1": "compacted", "grade2": "gravel", "grade3": "unpaved", "grade4": "ground",
                     "grade5": "grass"}
RULE_KEYS = ("tracktype", "ford", "traffic_calming")


def join(t, feats, key):
    """The drive shards' way tags on cell key's road features, in place: attrs osm_tracktype / osm_ford /
    osm_calming (None where none) and, with BNE_UNPAVED, osm_surface where OpenStreetMap maps no surface but a
    tracktype (TRACKTYPE_SURFACE). True joined, None nothing to join (no build), False a shard could not be had (a
    revisit fault: joined again next time)."""
    import drivefeat
    if t is None or feats is None or not len(feats):
        return None
    if t.version() is None:
        return None
    lon0, lat0, lon1, lat1 = (float(v) for v in key.bounds())
    ids = ways_of(feats)
    cols = {}
    for k in RULE_KEYS:
        got = t.way_values(k, ids, 0.5 * (lat0 + lat1), 0.5 * (lon0 + lon1))
        if got is None:
            return False
        cols[k] = np.array(got, object)
    feats.attrs["osm_tracktype"] = cols["tracktype"]
    feats.attrs["osm_ford"] = cols["ford"]
    feats.attrs["osm_calming"] = cols["traffic_calming"]
    if drivefeat.UNPAVED:
        # (the map's own surfaces kept beside: a layer joined again, a later build's tracktypes, starts from them)
        if "osm_surface_map" not in feats.attrs:
            raw = feats.attrs.get("osm_surface")
            feats.attrs["osm_surface_map"] = (np.full(len(feats), None, object) if raw is None
                                              else np.asarray(raw, object).copy())
        sf = np.asarray(feats.attrs["osm_surface_map"], object).copy()
        # (the surface's source kept apart, the evidence model's ranks: mapped "S1", derived from the tracktype "S3")
        src = np.array([None if v is None or v == "" or (isinstance(v, float) and v != v) else "S1:surface"
                        for v in sf.tolist()], object)
        for i, g in enumerate(cols["tracktype"].tolist()):
            if g in TRACKTYPE_SURFACE and src[i] is None:
                sf[i] = TRACKTYPE_SURFACE[g]
                src[i] = "S3:tracktype"
        feats.attrs["osm_surface"] = sf
        feats.attrs["surface_src"] = src
        for memo in ("_map_surface_values", "_road_specs"):
            feats.__dict__.pop(memo, None)
    return True
