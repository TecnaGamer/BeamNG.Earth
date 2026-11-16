"""Experimental Norwegian NVDB per-vertex width profiles.

Compatible with BeamNG.Earth's existing taper_widths geometry pipeline.
Values derive from nearby direction-aligned NVDB painted-width records, and
fall back to BeamNG.Earth's original values on ambiguous/uncovered roads.
"""
from functools import lru_cache
import logging
import math
import numpy as np

LOG = logging.getLogger('earthd.nvdb_v03')
LAT0, LON0 = 62.89, 7.67
SX = 111320.0 * math.cos(math.radians(LAT0))
SY = 111320.0
MAX_OFFSET_M = 3.25
MIN_ALIGNMENT = 0.89
AMBIGUITY_M = 0.9


def xy(lonlat):
    a = np.asarray(lonlat, dtype=np.float64)[:, :2]
    return np.column_stack(((a[:, 0] - LON0) * SX, (a[:, 1] - LAT0) * SY))


def records(lat, lon):
    """NVDB 838 first, then 583 for unmatched road points; read nearby cached cells.

    All requests are nonblocking inside road workers. Cache data become visible
    when the region is revisited after prefetch/async download.
    """
    import nvdb_nationwide_v05 as region
    la,lo=float(lat),float(lon)
    matches=[]
    for kind in ('838','583'):
        region.features(la,lo,kind) # queue the central tile without waiting
        sig=region.signature(region.near_keys(la,lo),kind)
        if not any(item[1] for item in sig):continue
        matches.extend(_indexed_records(kind,sig))
    return tuple(matches)


@lru_cache(maxsize=24)
def _indexed_records(kind,sig):
    import nvdb_nationwide_v05 as region
    import numpy as np
    result=[]
    for feature in region._combined(kind,sig):
        prop,geometry=feature.get('properties',{}),feature.get('geometry',{})
        width=prop.get('total_width_m')
        if geometry.get('type')!='LineString' or not isinstance(width,(int,float)) or not 2.5<=width<=15:continue
        a=np.asarray(geometry.get('coordinates',[]),dtype=np.float64)
        if a.ndim!=2 or len(a)<2 or a.shape[1]<2:continue
        path=xy(a)
        v=np.diff(path,axis=0)
        l2=np.einsum('ij,ij->i',v,v)
        good=l2>1e-8
        if not good.any():continue
        s,v,l2=path[:-1][good],v[good],l2[good]
        box=(float(path[:,0].min()),float(path[:,1].min()),float(path[:,0].max()),float(path[:,1].max()))
        result.append((kind,str(prop.get('nvdb_id','unknown')),float(width),box,s,v,l2))
    return tuple(result)


def at_points(lon, lat, original_width, tapered=None):
    """Return per-point widths or None when no reliable match.

    lon/lat in geodetic coordinates; width in metres, tapered optional preexisting
    width array from BeamNG.Earth. Only changes covered vertices. If a polyline
    crosses several NVDB sections, different vertices may receive different widths.
    """
    lon = np.asarray(lon, np.float64)
    lat = np.asarray(lat, np.float64)
    if lon.ndim != 1 or lon.shape != lat.shape or len(lon) < 2:
        return None
    if not (57.5 <= float(np.mean(lat)) <= 72 and 3.9 <= float(np.mean(lon)) <= 32.5):
        return None
    p = xy(np.column_stack((lon, lat)))
    d = np.gradient(p, axis=0)
    dn = np.linalg.norm(d, axis=1)
    valid_heading = dn > 1e-5
    h = np.divide(d, dn[:, None], out=np.zeros_like(d), where=valid_heading[:, None])
    base = np.full(len(lon), float(original_width)) if tapered is None else np.asarray(tapered, np.float64).copy()
    if base.shape != lon.shape:
        return None
    matching_records=records(float(np.mean(lat)),float(np.mean(lon)))
    if not matching_records:
        return None
    nearest = np.full(len(p), np.inf)
    second = np.full(len(p), np.inf)
    result = base.copy()
    alt = np.full(len(p), np.nan)
    for source, _, width, box, s, v, l2 in matching_records:
        if (p[:, 0].max() < box[0] - MAX_OFFSET_M or p[:, 0].min() > box[2] + MAX_OFFSET_M or
                p[:, 1].max() < box[1] - MAX_OFFSET_M or p[:, 1].min() > box[3] + MAX_OFFSET_M):
            continue
        # For small local datasets, geometry distance to segments is accurate and inexpensive.
        for start in range(0, len(p), 48):
            end = min(start + 48, len(p))
            q = p[start:end]
            t = np.clip(np.einsum('ijk,jk->ij', q[:, None] - s[None], v) / l2[None], 0, 1)
            foot = s[None] + t[:, :, None] * v[None]
            dist = np.linalg.norm(q[:, None] - foot, axis=2)
            align = np.abs(np.einsum('ik,jk->ij', h[start:end], v / np.sqrt(l2)[:, None]))
            dist = np.where(align >= MIN_ALIGNMENT, dist, np.inf)
            candidate = np.min(dist, axis=1)
            for k, distance in enumerate(candidate):
                i = start + k
                if not np.isfinite(distance) or distance > MAX_OFFSET_M or not valid_heading[i]:
                    continue
                # Prefer the newer 838 record whenever it is a plausible match.
                rank = distance + (4.0 if source == '583' else 0.0)
                if rank < nearest[i]:
                    second[i], alt[i] = nearest[i], result[i]
                    nearest[i], result[i] = rank, width
                elif rank < second[i]:
                    second[i], alt[i] = rank, width
    found = np.isfinite(nearest)
    ambiguous = found & ((second - nearest) < AMBIGUITY_M) & np.isfinite(alt) & (np.abs(alt - result) > 0.35)
    found &= ~ambiguous
    if not found.any():
        return None
    # Leave the existing joint taper near feature ends, avoiding abrupt edge jumps.
    out = base.copy()
    out[found] = result[found]
    # Keep the tested v0.5.8 isolated-width-sample filter inline, so this
    # road-only contribution does not depend on the experimental bridge patch.
    out = stabilise_road_widths(out, base, found, lon, lat)
    return out if np.isfinite(out).all() else None



def stabilise_road_widths(out, base, found, lon, lat):
    """Fix isolated, obviously conflicting vertices without smoothing real tapers.

    Port of the road-width-only routine from local v0.5.8 safety patch.
    """
    out = np.asarray(out, np.float64).copy()
    base = np.asarray(base, np.float64)
    found = np.asarray(found, bool)
    if (out.ndim != 1 or len(out) < 3 or out.shape != base.shape
            or found.shape != out.shape or not np.isfinite(out).all()):
        return out
    lon = np.asarray(lon, np.float64)
    lat = np.asarray(lat, np.float64)
    if lon.shape != out.shape or lat.shape != out.shape:
        return out
    dy = np.diff(lat) * 111_000.0
    dx = np.diff(lon) * 111_000.0 * np.cos(np.deg2rad((lat[1:] + lat[:-1]) / 2))
    distance = np.r_[0.0, np.cumsum(np.hypot(dx, dy))]
    source = out.copy()
    for i in range(1, len(out) - 1):
        span = distance[i+1] - distance[i-1]
        if span <= 0.01 or span > 28.0:
            continue
        if found[i-1] and found[i+1] and abs(source[i-1] - source[i+1]) <= 0.65:
            expected = float(np.interp(distance[i], distance[[i-1,i+1]], source[[i-1,i+1]]))
            if not found[i] or abs(source[i] - expected) > 1.8:
                out[i] = expected
        elif (found[i] and not found[i-1] and not found[i+1]
              and abs(base[i-1] - base[i+1]) <= 0.3
              and abs(source[i] - base[i]) > 2.0):
            out[i] = base[i]
    return out
