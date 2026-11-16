"""The clouds seen from space (the user, 2026-11-06: "improve how the earth looks at extremely high altitudes ... render
everything you would see irl"). Above weather.CLOUD_HIDE_M the game's volumetric clouds are hidden (they composite a
stale picture of themselves into the sky up there), and the cloud-free satellite photos left the Earth bare. This draws
the latest satellite day instead: NASA GIBS's VIIRS true-colour mosaic (public domain, no key; a new day every ~24 h)
round the point under the camera, its bright grey pixels as a cloud layer's colour and opacity (earthfast.cloud_layer),
on a curved shell SHELL_H_M up out to the horizon (earthfast.shell_lonlat), drawn translucent by the mod
(spaceclouds.lua) only while the camera is that high.

build(out_dir, vfs_dir, frame, lat, lon, cam_alt, fetcher) -> the message's fields, or None when no tile could be had.
Kernels in Rust; this is the tiles' fetch, the mosaic's crop and the files."""
import concurrent.futures
import io
import logging
import math
import os
import time

import numpy as np

import fast
import dmath
import dnp

log = logging.getLogger("earthd.spaceclouds")

LAYER = "VIIRS_SNPP_CorrectedReflectance_TrueColor"
TILE_URL = ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/" + LAYER
            + "/default/default/GoogleMapsCompatible_Level9/{z}/{y}/{x}.jpg")     # ("default" time: the latest day)
MAX_ZOOM = 9                # the layer's finest (~300 m a pixel at the equator)
# ... and the night side's city lights: NASA's Black Marble (VIIRS, 2016; public domain), its lit pixels as an additive
# emissive layer on a second shell just over the ground, darkened where the sun is up (earthfast.night_lights)
LIGHTS_URL = ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/VIIRS_Black_Marble/default/default/"
              "GoogleMapsCompatible_Level8/{z}/{y}/{x}.png")
LIGHTS_MAX_ZOOM = 8
LIGHTS_H_M = 300.0
TEX_PX = 2048               # the mosaic's side
MAX_TILES = 144             # a coarser zoom past this many tiles
SHELL_H_M = 6000.0          # the shell's height above sea level (a cloud deck's middle)
RINGS, SEGS = 48, 96
MAX_RADIUS_M = 2_600_000.0  # (farfield.HORIZON_MAX)
FETCH_THREADS = 6
R_EARTH = 6_371_000.0
MERC_LAT = 85.05112878


def radius_for(cam_alt):
    """The shell's reach (m) for a camera cam_alt m above sea level: its line of sight to the shell's height over the
    horizon, at most MAX_RADIUS_M."""
    h = max(0.0, float(cam_alt))
    d = math.sqrt(2 * R_EARTH * h) + math.sqrt(2 * R_EARTH * SHELL_H_M)
    return min(MAX_RADIUS_M, d)


def merc(lat, lon, z):
    """Global pixel coordinates (x east, y south) of lat / lon at zoom z (256 px tiles); lon is not wrapped."""
    lat = np.clip(np.asarray(lat, np.float64), -MERC_LAT, MERC_LAT)
    n = 256.0 * 2 ** z
    x = (np.asarray(lon, np.float64) / 360.0 + 0.5) * n
    y = (0.5 - dnp.arcsinh(dnp.tan(np.radians(lat))) / (2 * np.pi)) * n
    return x, y


def pick_zoom(lat0, lon0, radius):
    """(zoom, x0, y0, x1, y1): the finest zoom whose tile box over the disc (its lat / lon box) holds at most MAX_TILES
    tiles, and that box in global pixels (x unwrapped)."""
    dlat = math.degrees(radius / R_EARTH)
    dlon = min(180.0, dlat / max(0.05, dmath.cos(math.radians(lat0))))
    lats = [max(-MERC_LAT, lat0 - dlat), min(MERC_LAT, lat0 + dlat)]
    for z in range(MAX_ZOOM, -1, -1):
        x0, y1 = merc(lats[0], lon0 - dlon, z)
        x1, y0 = merc(lats[1], lon0 + dlon, z)
        n = (math.floor(float(x1) / 256) - math.floor(float(x0) / 256) + 1) * \
            (math.floor(float(y1) / 256) - math.floor(float(y0) / 256) + 1)
        if n <= MAX_TILES or z == 0:
            return z, float(x0), float(y0), float(x1), float(y1)


def subsolar(t=None):
    """(lat, lon) degrees of the point the sun stands over at unix time t (now): the declination and the equation of
    time from the day of the year (NOAA's low-precision formulae, ~0.1 degree)."""
    t = time.time() if t is None else float(t)
    tm = time.gmtime(t)
    g = 2 * math.pi / 365.0 * (tm.tm_yday - 1 + (tm.tm_hour - 12) / 24.0)
    decl = (0.006918 - 0.399912 * dmath.cos(g) + 0.070257 * dmath.sin(g) - 0.006758 * dmath.cos(2 * g)
            + 0.000907 * dmath.sin(2 * g) - 0.002697 * dmath.cos(3 * g) + 0.00148 * dmath.sin(3 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * dmath.cos(g) - 0.032077 * dmath.sin(g) - 0.014615 * dmath.cos(2 * g)
                    - 0.040849 * dmath.sin(2 * g))
    minutes = tm.tm_hour * 60 + tm.tm_min + tm.tm_sec / 60.0
    lon = -(minutes + eqt - 720.0) / 4.0
    return math.degrees(decl), (lon + 540.0) % 360.0 - 180.0


def subsolar_from(lat0, lon0, az_deg, el_deg):
    """(lat, lon) of the point the sun stands over, from its azimuth (deg clockwise from north) and elevation seen at
    (lat0, lon0): the great-circle step of 90 - elevation degrees along the azimuth (the game's own sun, whatever its
    clock)."""
    d = math.radians(90.0 - float(el_deg))
    p1, l1, th = math.radians(lat0), math.radians(lon0), math.radians(float(az_deg))
    p2 = dmath.asin(max(-1.0, min(1.0, dmath.sin(p1) * dmath.cos(d) + dmath.cos(p1) * dmath.sin(d) * dmath.cos(th))))
    l2 = l1 + dmath.atan2(dmath.sin(th) * dmath.sin(d) * dmath.cos(p1), dmath.cos(d) - dmath.sin(p1) * dmath.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def merc_lat(y, z):
    """Latitudes (deg) of global pixel rows y at zoom z."""
    n = 256.0 * 2 ** z
    return np.degrees(dnp.arctan(dnp.sinh(np.pi * (1 - 2 * np.asarray(y, np.float64) / n))))


def mosaic(fetcher, z, x0, y0, x1, y1, url=TILE_URL, px=TEX_PX, kind="demand"):
    """The (px, px, 3) uint8 image of the global pixel box at zoom z from the layer's tiles (black where a tile could
    not be had), and how many tiles came (globe.py: the whole world from EOX)."""
    from PIL import Image
    tx0, tx1 = int(math.floor(x0 / 256)), int(math.floor(x1 / 256))
    ty0, ty1 = int(math.floor(y0 / 256)), int(math.floor(y1 / 256))
    n = 2 ** z
    canvas = np.zeros(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256, 3), np.uint8)

    def one(tx, ty):
        if not 0 <= ty < n:
            return tx, ty, None
        try:
            body = fetcher.get(url.format(z=z, x=tx % n, y=ty), kind=kind).body
            return tx, ty, np.asarray(Image.open(io.BytesIO(body)).convert("RGB"))
        except Exception as e:          # (a tile the day has no pass over yet, offline: no clouds there)
            log.debug("space clouds tile %s/%s/%s: %s", z, tx, ty, e)
            return tx, ty, None
    got = 0
    with concurrent.futures.ThreadPoolExecutor(FETCH_THREADS, thread_name_prefix="spaceclouds") as pool:
        for tx, ty, im in pool.map(lambda t: one(*t), [(tx, ty) for ty in range(ty0, ty1 + 1)
                                                           for tx in range(tx0, tx1 + 1)]):
            if im is not None and im.shape[:2] == (256, 256):
                canvas[(ty - ty0) * 256:(ty - ty0 + 1) * 256, (tx - tx0) * 256:(tx - tx0 + 1) * 256] = im
                got += 1
    cx0, cy0 = int(round(x0 - tx0 * 256)), int(round(y0 - ty0 * 256))
    cx1, cy1 = int(round(x1 - tx0 * 256)), int(round(y1 - ty0 * 256))
    crop = Image.fromarray(canvas[cy0:max(cy1, cy0 + 1), cx0:max(cx1, cx0 + 1)])
    return np.asarray(crop if crop.size == (px, px) else crop.resize((px, px), Image.BILINEAR)), got


def build(out_dir, vfs_dir, frame, lat0, lon0, cam_alt, fetcher, sun=None):
    """Write the cloud shell (shape, colour and opacity DDS) for a camera cam_alt m over (lat0, lon0) into out_dir;
    the 'space_clouds' message's fields (game paths under vfs_dir, the material's name), or None without any tile."""
    import cdae
    import imagery
    fast.require(151, "space clouds")
    radius = radius_for(cam_alt)
    z, x0, y0, x1, y1 = pick_zoom(lat0, lon0, radius)
    img, got = mosaic(fetcher, z, x0, y0, x1, y1)
    if not got:
        return None
    col, op = fast._ef.cloud_layer(np.ascontiguousarray(img))
    stamp = f"{int(time.time())}"
    name = f"spaceclouds_{stamp}"
    os.makedirs(out_dir, exist_ok=True)
    imagery.write_dds(os.path.join(out_dir, name + "_c.dds"), np.asarray(col))
    imagery.write_dds(os.path.join(out_dir, name + "_o.dds"), np.repeat(np.asarray(op)[:, :, None], 3, axis=2))
    ll, tris = fast._ef.shell_lonlat(float(lat0), float(lon0), float(radius), RINGS, SEGS)
    ll = np.asarray(ll)
    e, nn, u = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.full(len(ll), SHELL_H_M))
    pos = np.stack([e, nn, u], axis=1).astype(np.float64)
    mx, my = merc(ll[:, 1], ll[:, 0], z)
    # (x unwrapped as the box: a vertex across the antimeridian from the centre comes back next to it)
    w = 256.0 * 2 ** z
    mx = np.where(mx < x0 - w / 2, mx + w, np.where(mx > x1 + w / 2, mx - w, mx))
    uv = np.stack([(mx - x0) / (x1 - x0), (y1 - my) / (y1 - y0)], axis=1)
    material = "bne_" + name
    cdae.write(os.path.join(out_dir, name + ".cdae"), pos, np.asarray(tris, np.int64), uvs=uv, material=material,
               object_name="clouds")
    log.info("space clouds: %d tiles at zoom %d, %.0f km round %.3f, %.3f (camera %.0f km up)", got, z,
             radius / 1000, lat0, lon0, cam_alt / 1000)
    out = {"shape": f"{vfs_dir}/{name}.cdae", "color": f"{vfs_dir}/{name}_c.dds", "opacity": f"{vfs_dir}/{name}_o.dds",
           "material": material, "lat": float(lat0), "lon": float(lon0), "radius": float(radius)}
    try:
        out.update(_lights(out_dir, vfs_dir, frame, name, ll, tris, z, x0, y0, x1, y1, fetcher, sun))
    except Exception as e:                  # (the clouds without the lights)
        log.debug("space clouds: night lights: %s: %s", type(e).__name__, e)
    return out


def _lights(out_dir, vfs_dir, frame, name, ll, tris, z, x0, y0, x1, y1, fetcher, sun=None):
    """The night-lights shell over the clouds' box: Black Marble at zoom min(z, LIGHTS_MAX_ZOOM), the lit pixels where
    the sun is down (sun: its (lat, lon) subsolar point, the game's; None: the real one now); {} without any tile."""
    import cdae
    import imagery
    zl = min(z, LIGHTS_MAX_ZOOM)
    k = 2.0 ** (zl - z)
    img, got = mosaic(fetcher, zl, x0 * k, y0 * k, x1 * k, y1 * k, LIGHTS_URL)
    if not got:
        return {}
    rows = y0 * k + (np.arange(TEX_PX) + 0.5) * (y1 - y0) * k / TEX_PX
    cols = x0 * k + (np.arange(TEX_PX) + 0.5) * (x1 - x0) * k / TEX_PX
    lon = cols / (256.0 * 2 ** zl) * 360.0 - 180.0
    slat, slon = sun if sun is not None else subsolar()
    lit = fast._ef.night_lights(np.ascontiguousarray(img), np.ascontiguousarray(merc_lat(rows, zl)),
                                np.ascontiguousarray(lon), slat, slon)
    imagery.write_dds(os.path.join(out_dir, name + "_l.dds"), np.asarray(lit))
    e, nn, u = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.full(len(ll), LIGHTS_H_M))
    mx, my = merc(ll[:, 1], ll[:, 0], z)
    w = 256.0 * 2 ** z
    mx = np.where(mx < x0 - w / 2, mx + w, np.where(mx > x1 + w / 2, mx - w, mx))
    uv = np.stack([(mx - x0) / (x1 - x0), (y1 - my) / (y1 - y0)], axis=1)
    material = "bne_" + name + "_lights"
    cdae.write(os.path.join(out_dir, name + "_lights.cdae"), np.stack([e, nn, u], axis=1).astype(np.float64),
               np.asarray(tris, np.int64), uvs=uv, material=material, object_name="lights")
    return {"lightsShape": f"{vfs_dir}/{name}_lights.cdae", "lights": f"{vfs_dir}/{name}_l.dds",
            "lightsMaterial": material}
