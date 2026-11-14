"""Whether a place is in Germany, for the German road signs and markings. navgraph.country_near is the nearest
airport's country: wrong at border towns (Kehl and Strasbourg, Konstanz and Kreuzlingen, Buesingen), and the signs
must change exactly at the border.

The borders are countrybounds' (PR #505: Overture's divisions, the territories with their waters, simplified to
~10 m, looked up natively by countrymap.rs), fetched once from BeamNG.Earth-data (regionaldata) and kept on disk: a
point is in Germany where countrybounds.bundled().codes_at says "DE". No hit or two territories overlapping there
(None), and no border data at all (countrybounds.Absent: not downloaded yet, offline, regionaldata not configured, a
failed fetch or checksum) are not Germany: the German behaviour is off there, today's output. BOX is asked first: a
point outside it is not Germany without countrybounds or regionaldata being asked anything (the German junction
signs are on by default the world over: a player in Baltimore downloads no borders). Never raises. sweden.contains
is the rough counterpart (a hand-made outline, good to some kilometres)."""
import logging
import time

import numpy as np

log = logging.getLogger("germany")

# Germany's box (lon_min, lat_min, lon_max, lat_max). Every DE point of the border data lies inside it (the published
# build c02f3d4300959998 sampled every 0.005 deg over the box and 0.4-0.6 deg round it: DE from 5.870 to 15.035 E and
# 47.275 to 55.095 N, its territorial waters north of Sylt included; none outside), with 0.015 deg or more to spare.
# tools/build_lane_tags.GERMANY_BOX is the same box: the data repo's Action fetches that tool alone, so it cannot
# import this one (test_lane_rules keeps the two equal).
BOX = (5.81, 47.22, 15.10, 55.11)
ABSENT = ":absent"      # signature()'s mark where the border index is countrybounds.Absent
RETRY_S = 60.0          # s after a border lookup that raised before the border index is asked for again
                        # (regionaldata's RETRY_S); meanwhile not Germany, and the builds that ask are not kept (_fail).
                        # Each further failure in a row doubles it, up to RETRY_MAX_S; a lookup that answers starts it
                        # over. A write of the 11 MB download that keeps failing (a full disk, a read-only or locked
                        # pagesdata) would otherwise fetch it from the shared Pages host every RETRY_S in every process
                        # asking (~660 MB an hour each): netfetch's budget for the host is soft, and its breaker counts
                        # failed requests only
RETRY_MAX_S = 1800.0
_failed = [-float("inf"), 0]    # (time.monotonic() until which the border index is not asked, failures in a row)


def in_box(lon, lat):
    """Bool array (1-D): (lon, lat) inside BOX (scalars or arrays; NaN is outside)."""
    lon, lat = np.broadcast_arrays(np.asarray(lon, np.float64), np.asarray(lat, np.float64))
    lon, lat = lon.ravel(), lat.ravel()
    return (lon >= BOX[0]) & (lon <= BOX[2]) & (lat >= BOX[1]) & (lat <= BOX[3])


def touches(lon, lat):
    """Whether the lon / lat bounds of these points (a box's corners) meet BOX: the box may hold German points."""
    lon, lat = np.asarray(lon, np.float64), np.asarray(lat, np.float64)
    return bool(lon.max() >= BOX[0] and lon.min() <= BOX[2] and lat.max() >= BOX[1] and lat.min() <= BOX[3])


def _fail(e):
    """A border lookup that raised (a corrupt band file, a failed write of its download, an earthfast without
    country_index, a band file gone from under the index): logged; not Germany for RETRY_S (doubled for each failure
    in a row, up to RETRY_MAX_S); the builds running now not kept (revisit.fault: their keys may name the border data,
    designs.cache_parts, while they go on without it); and countrybounds' index dropped, so the next try after that
    fetches the band file again through regionaldata. (The file sits in the cache's evictable pagesdata, and the
    settings page's Clear cache removes it too, while the index countrybounds keeps, lru-cached per regional build, goes
    on reading the old path: every band it has not decoded yet then raises. Its _loaded is upstream's private name:
    where it is gone the index is simply kept.)"""
    _failed[1] += 1
    wait = min(RETRY_S * 2.0 ** (_failed[1] - 1), RETRY_MAX_S)
    _failed[0] = time.monotonic() + wait
    log.warning("country borders: %s: %s (not Germany for %.0f s)", type(e).__name__, e, wait)
    import revisit
    revisit.fault()
    try:
        import countrybounds
        clear = getattr(getattr(countrybounds, "_loaded", None), "cache_clear", None)
        if clear is not None:
            clear()
    except Exception as e2:
        log.debug("country borders: index kept: %s: %s", type(e2).__name__, e2)


def _index():
    """countrybounds.bundled() (its Absent until the border data is reachable), or None where it raised (_fail) and
    for the back-off after: never raises."""
    if time.monotonic() < _failed[0]:
        return None
    try:
        import countrybounds
        return countrybounds.bundled()
    except Exception as e:
        _fail(e)
        return None


def contains(lon, lat):
    """Bool array (1-D): inside Germany (countrybounds' "DE"; outside BOX, None and no border data: False). lon / lat:
    scalars or arrays. One codes_at call for all the points inside BOX, none when no point is. A point inside BOX asked
    while a lookup failed (_fail, for its back-off) is not Germany and the build asking is not kept (revisit.fault): its
    key may have been taken before the failure, with the border data's signature. A lookup that answers ends a run of
    failures (the next one waits RETRY_S again)."""
    lon, lat = np.broadcast_arrays(np.asarray(lon, np.float64), np.asarray(lat, np.float64))
    lon, lat = lon.ravel(), lat.ravel()
    out = np.zeros(len(lon), bool)
    ask = np.flatnonzero(in_box(lon, lat))
    if not len(ask):
        return out
    idx = _index()
    if idx is None:
        import revisit
        revisit.fault()
        return out
    try:
        codes = idx.codes_at(np.column_stack([lat[ask], lon[ask]]))     # (countrybounds' points are (lat, lon))
    except Exception as e:
        _fail(e)
        return out
    _failed[1] = 0              # (not the time: a lookup that began before a failure leaves its back-off as it is)
    out[ask] = [c == "DE" for c in codes]
    return out


def de_at(frame, x, y):
    """contains() for frame-local metres (geodesy.EnuFrame east, north)."""
    lat, lon, _ = frame.enu_to_geodetic(np.asarray(x, np.float64), np.asarray(y, np.float64), 0.0)
    return contains(lon, lat)


def effective_country(country, inside):
    """"DE" where the point is inside Germany; otherwise the country as it was."""
    return "DE" if inside else country


def signature():
    """The border data Germany's lookups use, for the cache keys of German output (designs.cache_parts):
    countrybounds.signature() (its algorithm and the regional build's stamp), with ABSENT where the index is
    countrybounds.Absent or could not be had: countrybounds keeps an Absent for a stamp for the process's life (a
    manifest that came without its band file), and tiles built then must not be taken for ones built with the borders.
    Asking it fetches the border data where it is not on disk yet (the key needs the data it names). Never raises."""
    idx = _index()
    try:
        import countrybounds
        sig = countrybounds.signature()
        absent = idx is None or isinstance(idx, countrybounds.Absent)
    except Exception as e:
        log.warning("country borders: %s: %s", type(e).__name__, e)
        sig, absent = "unknown", True
    return sig + ABSENT if absent else sig
