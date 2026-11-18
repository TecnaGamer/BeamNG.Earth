"""Top-down preview of an airport's graded runways (`airports.py --preview IDENT out.png`; development).

Left: the graded height field (hillshade) with the zones tinted (runway, shoulders, graded strip, blend)
and the markings drawn on top. Right: the height change graded - DEM (blue: raised, red: cut, grey: none).
Below: each runway's centreline profile, the DEM against the graded surface. The JSON report gives
the plan's numbers and the timings. The DEM comes through dem.Dem (netfetch, the same cache as earthd)
at one fixed zoom per place, so the profile is the one earthd would compute.
"""
import math
import os
import time

import numpy as np
import dmath
from PIL import Image, ImageDraw, ImageFont

import dem as demmod
import geodesy
import runwaygeom
import runwaymarks

SURFACE = (252, 252, 251)
INK, INK2 = (11, 11, 11), (82, 81, 78)
BLUE, RED, NEUTRAL = np.array((42, 120, 214)), np.array((227, 73, 72)), np.array((240, 239, 236))
ZONE_TINT = {4: ((58, 58, 58), 0.75), 3: ((120, 120, 120), 0.55), 2: ((122, 160, 96), 0.30), 1: ((200, 190, 120), 0.18)}
MAX_PANEL = 1600                # px: a larger airport is drawn coarser
PROFILE_H = 170


def _font(size):
    for name in ("DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def dem_sampler(cache_dir, fetcher, lat):
    """elev_fn(lat, lon) on the DEM at the zoom of ~2 m pixels (z15 at most): fixed for the place."""
    d = demmod.Dem(os.path.join(cache_dir, "dem"), fetcher=fetcher)
    zoom = demmod.zoom_for(lat, 2.0)
    return (lambda la, lo: d.sample(la, lo, zoom)), zoom, d


def _hillshade(z, px):
    gy, gx = np.gradient(z, px)
    out = np.zeros_like(z)
    for az, wgt in ((315, 0.6), (270, 0.2), (0, 0.2)):          # a soft multi-directional light
        a = math.radians(az)
        lx, ly, lz = dmath.sin(a) * dmath.cos(math.radians(45)), dmath.cos(a) * dmath.cos(math.radians(45)), dmath.sin(math.radians(45))
        # gradient rows run north -> south (row 0 north): flip gy for a north-up normal
        nx, ny, nz = -gx, gy, np.ones_like(z)
        out += wgt * np.clip((nx * lx + ny * ly + nz * lz) / np.sqrt(nx * nx + ny * ny + 1), 0, 1)
    return out


def preview(db, ident, path, cache_dir, fetcher, px=2.0, include_closed=True):
    t0 = time.perf_counter()
    if ident.startswith("oa:"):
        rws = [r for r in db.runways if r.key == ident]
        if not rws:
            raise KeyError(f"no runway {ident}")
    else:
        rws = db.runways_of(ident, min_quality="arp", include_closed=include_closed)
        if not rws:
            raise KeyError(f"no placeable runway at {ident}")
    found = {r.id: r for r in rws}
    for r in rws:                                    # and whatever is graded together with them
        for o in db.group(r, min_quality="arp", include_closed=include_closed):
            found.setdefault(o.id, o)
    groups = runwaygeom.groups(found.values())
    lat0 = float(np.mean([r.le.lat for r in rws]))
    elev_fn, zoom, dem = dem_sampler(cache_dir, fetcher, lat0)
    lon0 = float(np.mean([r.le.lon for r in rws]))
    elev_fn(np.array([lat0]), np.array([lon0]))
    # the DEM's native resolution here: Mapterhorn tiles at this zoom exist only where national
    # high-resolution data does; the AWS fallback outside lidar areas is SRTM-class (~30 m)
    served = {v for k, v in dem.served.items() if k[0] == zoom}
    dem_res = 30.0 if served and served <= {"aws-terrarium", None} else None
    t_plan = time.perf_counter()
    plans = []
    plan_ms = {}
    for g in groups:
        gp = []
        for i, rw in enumerate(g):
            t = time.perf_counter()
            done = tuple(gp)
            fn = elev_fn if not done else (lambda la, lo, done=done: runwaygeom.apply(done, la, lo, elev_fn(la, lo))[0])
            gp.append(runwaygeom.plan(rw, fn, order=i, dem_res=dem_res))
            plan_ms[rw.key] = round((time.perf_counter() - t) * 1000, 1)
        plans.append(gp)
    t_plan = time.perf_counter() - t_plan
    allp = [p for g in plans for p in g]
    # the raster: a local ENU grid over every plan's influence rectangle
    b = np.array([runwaygeom.bounds(p, 40.0) for p in allp])
    la_c, lo_c = (b[:, 0].min() + b[:, 2].max()) / 2, (b[:, 1].min() + b[:, 3].max()) / 2
    f = geodesy.EnuFrame(la_c, lo_c, 0.0)
    ce, cn, _ = f.geodetic_to_enu(np.r_[b[:, 0], b[:, 2], b[:, 0], b[:, 2]], np.r_[b[:, 1], b[:, 3], b[:, 3], b[:, 1]], 0.0)
    e0, e1, n0, n1 = float(np.min(ce)), float(np.max(ce)), float(np.min(cn)), float(np.max(cn))
    px = max(px, (e1 - e0) / MAX_PANEL, (n1 - n0) / MAX_PANEL)
    nx, ny = int((e1 - e0) / px) + 1, int((n1 - n0) / px) + 1
    E, N = np.meshgrid(e0 + px * (np.arange(nx) + 0.5), n1 - px * (np.arange(ny) + 0.5))
    lat, lon, _ = f.enu_to_geodetic(E, N, 0.0)
    t = time.perf_counter()
    base = elev_fn(lat, lon)
    t_dem = time.perf_counter() - t
    t = time.perf_counter()
    z = base.copy()
    wmax = np.zeros(base.shape)
    zone = np.zeros(base.shape, np.int8)
    for gp in plans:
        z, w, zn = runwaygeom.apply(gp, lat, lon, z)
        wmax, zone = np.maximum(wmax, w), np.maximum(zone, zn)
    t_apply = time.perf_counter() - t
    delta = z - base

    # left panel: hillshade + zones + markings
    shade = _hillshade(z, px)
    rgb = np.stack([shade * 215 + 30] * 3, axis=-1)
    for zc, (col, a) in ZONE_TINT.items():
        m = zone == zc
        rgb[m] = rgb[m] * (1 - a) + np.array(col) * a * (0.6 + 0.4 * shade[m, None])
    left = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))
    dr = ImageDraw.Draw(left)
    marks_report = {}
    for p in allp:
        rw = next(r for r in found.values() if r.key == p.key)
        mk = runwaymarks.markings(rw)
        marks_report[p.key] = {"class": mk.mclass, "style": mk.style, "designators": list(mk.designators),
                               "strokes": len(mk.strokes), "by_kind": mk.by_kind(), "notes": list(mk.notes)}
        for s in mk.strokes:
            poly = s.polygon()
            la, lo = p.geom.latlon(poly[:, 0], poly[:, 1])
            pe, pn, _ = f.geodetic_to_enu(la, lo, 0.0)
            pts = [((x - e0) / px, (n1 - y) / px) for x, y in zip(np.atleast_1d(pe), np.atleast_1d(pn))]
            col = (255, 255, 255) if s.color == "white" else (250, 200, 30)
            dr.polygon(pts, fill=col)
    # right panel: delta
    lim = max(0.5, float(np.percentile(np.abs(delta[wmax > 0]), 99)) if (wmax > 0).any() else 0.5)
    x = np.clip(delta / lim, -1, 1)[..., None]
    col = np.where(x > 0, NEUTRAL + (BLUE - NEUTRAL) * x, NEUTRAL + (RED - NEUTRAL) * (-x))
    col = col * (0.75 + 0.25 * shade[..., None])
    right = Image.fromarray(np.clip(col, 0, 255).astype(np.uint8))
    # profiles
    W = left.width * 2 + 30
    font, small = _font(16), _font(13)
    head = 64
    H = head + max(left.height, 1) + 40 + PROFILE_H * len(allp)
    img = Image.new("RGB", (W, H), SURFACE)
    d = ImageDraw.Draw(img)
    names = ", ".join(sorted({r.airport_ident for r in found.values()}))
    d.text((10, 8), f"{names}: graded runways (left: hillshade, zones, markings; right: graded - DEM, "
                    f"blue raised / red cut, +-{lim:.1f} m)", fill=INK, font=font)
    d.text((10, 32), f"{px:.1f} m/px, DEM z{zoom}, OurAirports {db.snapshot}; zones: dark runway, grey shoulders, "
                     f"green graded strip, yellow blend", fill=INK2, font=small)
    img.paste(left, (0, head))
    img.paste(right, (left.width + 30, head))
    # scale bar
    sb = 500.0 if (e1 - e0) > 1500 else 100.0
    y = head + left.height + 10
    d.rectangle((10, y, 10 + sb / px, y + 5), fill=INK)
    d.text((16 + sb / px, y - 6), f"{sb:.0f} m", fill=INK2, font=small)
    y0 = head + left.height + 40
    for p in allp:
        _profile(d, p, 10, y0, W - 20, PROFILE_H - 20, small)
        y0 += PROFILE_H
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    img.save(path)
    return {"png": path, "px_m": round(px, 2), "dem_zoom": zoom, "dem_res_hint_m": dem_res, "raster": [nx, ny],
            "seconds": {"total": round(time.perf_counter() - t0, 2), "plans": round(t_plan, 3),
                        "dem_raster": round(t_dem, 2), "apply": round(t_apply, 3)},
            "dem_net": dem.stats_snapshot(),
            "runways": [{"key": p.key, "name": next(r.name for r in found.values() if r.key == p.key),
                         "order": p.order, "target_source": p.target_source, "steep": p.steep,
                         "targets_m": [round(v, 2) for v in p.targets], "lambda": p.lam,
                         "plan_ms": plan_ms[p.key], **p.stats, "markings": marks_report[p.key]} for p in allp]}


def _profile(d, p, x0, y0, w, h, font):
    """One runway's centreline: DEM (grey) and graded (blue), elevation against distance from le."""
    s, zd, zg = p.s, p.dem, p.z
    lo, hi = float(min(zd.min(), zg.min())), float(max(zd.max(), zg.max()))
    if hi - lo < 2.0:
        mid = (hi + lo) / 2
        lo, hi = mid - 1.0, mid + 1.0
    pad = 70
    X = lambda v: x0 + pad + (v / s[-1]) * (w - pad - 10)
    Y = lambda v: y0 + 22 + (1 - (v - lo) / (hi - lo)) * (h - 30)
    d.line([(X(0), y0 + h - 8), (X(s[-1]), y0 + h - 8)], fill=(200, 200, 196), width=1)
    d.line([(X(a), Y(b)) for a, b in zip(s, zd)], fill=(150, 150, 146), width=2)
    d.line([(X(a), Y(b)) for a, b in zip(s, zg)], fill=tuple(int(c) for c in BLUE), width=2)
    st = p.stats
    d.text((x0, y0), f"{p.key} {p.geom.length:.0f} x {p.geom.width:.0f} m, code {st['code']}: overall "
                     f"{st['overall_grade_pct']:+.2f} %, max {st['max_grade_pct']:.2f} %, min radius "
                     f"{st['min_radius_m'] or 0:.0f} m, DEM rmse {st['dem_rmse_m']:.2f} m, ends from {p.target_source}"
                     f"{', steep' if p.steep else ''}.  grey: DEM, blue: graded", fill=INK, font=font)
    d.text((x0, Y(hi) - 6), f"{hi:.0f} m", fill=INK2, font=font)
    d.text((x0, Y(lo) - 6), f"{lo:.0f} m", fill=INK2, font=font)
    d.text((X(0) - 4, y0 + h - 6), "le", fill=INK2, font=font)
    d.text((X(s[-1]) - 14, y0 + h - 6), "he", fill=INK2, font=font)
