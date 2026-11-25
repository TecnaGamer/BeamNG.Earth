"""Winter W2 reference sheet, offline (no game): a square of ground as the server draws it (the imagery chain, the
season grade, the rock on steep ground: earthd's far-tile steps) with BNE_WINTER's snow term (server/snowcover.py,
a debug preset), beside a cloud-free WINTER Sentinel-2 scene of the same square (Element 84's public STAC of the AWS
Sentinel-2 L2A COGs, the 10 m true-colour "visual" asset, read over byte ranges) and the summer photo:

    python3 tools/snowsheet.py --lat 46.62 --lon 8.03 --size 12000 --preset alpine --name grindelwald \\
        --winter 2025-01-01/2025-02-28

Writes <out>/<name>_sheet.png (summer photo | ours with snow | winter Sentinel-2, each --px square, north up) and
<name>_{photo,snow,s2}.png. --cache: a scratch download cache (default ~/bnge-tests/winter/w2/cache; never the
player's). The heights: the server's DEM (Mapterhorn) at --zoom on a 65 x 65 grid, the far tiles' kind of grid.
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

STAC = "https://earth-search.aws.element84.com/v1/search"
UA = "BeamNG.Earth-research/1.0"


def utm_forward(lat, lon, zone, south):
    """WGS84 lat/lon (deg arrays) -> UTM (E, N) metres of zone (Snyder's series; ~mm inside the zone)."""
    import numpy as np
    a, f, k0 = 6378137.0, 1 / 298.257223563, 0.9996
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    phi, lam = np.radians(lat), np.radians(lon)
    lam0 = math.radians((zone - 1) * 6 - 180 + 3)
    n = a / np.sqrt(1 - e2 * np.sin(phi) ** 2)
    t = np.tan(phi) ** 2
    c = ep2 * np.cos(phi) ** 2
    aa = np.cos(phi) * (lam - lam0)
    m = a * ((1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * phi
             - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * np.sin(2 * phi)
             + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * np.sin(4 * phi)
             - (35 * e2 ** 3 / 3072) * np.sin(6 * phi))
    e = k0 * n * (aa + (1 - t + c) * aa ** 3 / 6 + (5 - 18 * t + t * t + 72 * c - 58 * ep2) * aa ** 5 / 120) + 500000.0
    nn = k0 * (m + n * np.tan(phi) * (aa * aa / 2 + (5 - t + 9 * c + 4 * c * c) * aa ** 4 / 24
                                      + (61 - 58 * t + t * t + 600 * c - 330 * ep2) * aa ** 6 / 720))
    if south:
        nn = nn + 10000000.0
    return e, nn


def stac_items(lat, lon, half_deg, dates, max_cloud):
    body = {"collections": ["sentinel-2-l2a"], "bbox": [lon - half_deg, lat - half_deg, lon + half_deg, lat + half_deg],
            "datetime": dates.replace("/", "T00:00:00Z/") + "T23:59:59Z", "limit": 50,
            "query": {"eo:cloud_cover": {"lt": max_cloud}}}
    req = urllib.request.Request(STAC, json.dumps(body).encode(), {"Content-Type": "application/json",
                                                                    "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        feats = json.load(r)["features"]
    return sorted(feats, key=lambda f: f["properties"].get("eo:cloud_cover", 100))


def s2_square(frame, x0, y0, size, px, dates, cache, max_cloud=8.0, level=None):
    """A winter Sentinel-2 true-colour square (px, px, 3) over [x0, x0 + size]^2 of frame, or None; and its item."""
    import numpy as np
    import cog
    import netfetch
    import rangeread
    t = (np.arange(px) + 0.5) / px
    e, n = np.meshgrid(x0 + t * size, y0 + size - t * size)
    lat, lon, _ = frame.enu_to_geodetic(e.ravel(), n.ravel(), 0.0)
    lat, lon = np.asarray(lat).reshape(px, px), np.asarray(lon).reshape(px, px)
    half = max(float(lat.max() - lat.min()), float(lon.max() - lon.min())) / 2
    items = stac_items(float(lat.mean()), float(lon.mean()), half, dates, max_cloud)
    fetcher = netfetch.Fetcher()
    if level is None:
        level = max(0, int(math.floor(math.log2(max(size / px / 10.0, 1.0)))))
    for it in items:
        props = it["properties"]
        epsg = int(props.get("proj:epsg") or props.get("proj:code", "EPSG:0").split(":")[1])
        if not (32601 <= epsg <= 32660 or 32701 <= epsg <= 32760):
            continue
        href = it["assets"]["visual"]["href"]
        d = os.path.join(cache, "s2", it["id"])
        os.makedirs(d, exist_ok=True)
        try:
            c = cog.Cog(rangeread.RemoteFile(href, None, d, fetcher), level=level)
        except Exception as ex:
            print("  skip", it["id"], ex)
            continue
        ue, un = utm_forward(lat, lon, epsg % 100, epsg >= 32700)
        cols, rows = c.pixel_of(ue, un)
        cols, rows = np.asarray(cols), np.asarray(rows)
        c0, c1 = int(np.floor(cols.min())) - 1, int(np.ceil(cols.max())) + 2
        r0, r1 = int(np.floor(rows.min())) - 1, int(np.ceil(rows.max())) + 2
        if c0 < 0 or r0 < 0 or c1 > c.width or r1 > c.height:
            continue
        win = np.asarray(c.read_window(c0, r0, c1 - c0, r1 - r0))
        if win.ndim != 3:
            continue
        ci = np.clip(np.round(cols - c0).astype(int), 0, win.shape[1] - 1)
        ri = np.clip(np.round(rows - r0).astype(int), 0, win.shape[0] - 1)
        out = win[ri, ci, :3]
        if (out.max(axis=2) == 0).mean() > 0.02:        # (the scene's nodata edge)
            continue
        return out.astype(np.uint8), it
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--size", type=float, default=8000.0, help="square side, m")
    ap.add_argument("--px", type=int, default=1024)
    ap.add_argument("--preset", default="alpine")
    ap.add_argument("--name", required=True)
    ap.add_argument("--winter", default="2025-01-05/2025-02-25", help="the Sentinel-2 date range (YYYY-MM-DD/YYYY-MM-DD)")
    ap.add_argument("--season", default="winter", help="the season grade of our photo (season.py modes)")
    ap.add_argument("--zoom", type=int, default=0, help="DEM zoom (0: by the square's size)")
    ap.add_argument("--out", default=os.path.expanduser("~/bnge-tests/winter/w2"))
    ap.add_argument("--cache", default=os.path.expanduser("~/bnge-tests/winter/w2/cache"))
    ap.add_argument("--no-s2", action="store_true")
    ap.add_argument("--packs", action="store_true", help="the winter packs (cdn.json's, or BNGE_WINTERPACKS_DIR's local "
                    "build): ESA CCI's evergreen share, JRC's water share")
    ap.add_argument("--map", action="store_true", help="the map data's roads and water for the snow (block-sized squares,"
                    " texels to 2 m: as the game's block photos and near far tiles)")
    a = ap.parse_args()
    if os.path.realpath(a.cache).startswith(os.path.realpath(os.path.expanduser("~/.cache/beamng-earth"))):
        sys.exit("snowsheet: use a scratch cache, never the player's")
    os.environ["BNE_WINTER"] = "on"
    os.environ["BNE_SNOW"] = a.preset
    import numpy as np
    from PIL import Image
    import dem as demmod
    import geodesy
    import heightmap
    import imagery as imagerymod
    import netfetch
    import rockface
    import snowcover
    os.makedirs(a.out, exist_ok=True)
    frame = geodesy.EnuFrame(a.lat, a.lon)
    x0 = y0 = -a.size / 2
    net = netfetch.Fetcher()
    im = imagerymod.Imagery(os.path.join(a.cache, "imagery"), fetcher=net, season_mode=a.season)
    t0 = time.perf_counter()
    rgb, used = im.render(frame, x0, y0, a.size, a.px)
    rgb = np.asarray(rgb.pixels if isinstance(rgb, imagerymod.FarPhoto) else rgb, np.uint8)
    print("photo %.1f s %s" % (time.perf_counter() - t0, {k: round(v / a.px ** 2, 2) for k, v in used.items()}))
    dem = demmod.Dem(os.path.join(a.cache, "dem"), fetcher=net)
    zoom = a.zoom or max(8, min(14, int(round(math.log2(40075016.0 * math.cos(math.radians(a.lat)) / (a.size / 64) / 512)))))
    nn = 65
    xs = x0 + np.arange(nn) * (a.size / (nn - 1))
    ys = y0 + np.arange(nn) * (a.size / (nn - 1))
    _, U = heightmap.surface(frame, lambda la, lo: dem.sample(la, lo, zoom), xs, ys)
    U = np.asarray(U, np.float64)
    print("heights z%d %.0f..%.0f m" % (zoom, U.min(), U.max()))
    photo = rockface.rock(rgb, frame, x0, y0, a.size, U, x0, y0, a.size / (nn - 1))
    if os.environ.get("BNGE_WINTERPACKS_DIR") or a.packs:
        import winterpacks
        snowcover.set_packs(winterpacks.packs(a.cache, net))
    spec = snowcover.spec_for_square(frame, x0, y0, a.size)
    print("packs:", snowcover._packs.versions() if snowcover._packs else None, "credits", sorted(spec.credits) if spec else ())
    road = water = None
    if a.map:
        import mapcells
        import mapdata as mapdatamod
        import surface as surfacemod
        md = mapdatamod.MapData(a.cache, net)
        sf = surfacemod.Surface(md)
        s_, w_, n_, e_ = snowcover.box_of(frame, x0, y0, a.size)
        for k in mapcells.cells_for_bbox(s_, w_, n_, e_):
            md.cell(k, layers=mapdatamod.LAYERS, kind="demand")
        sf.prefetch(a.lat, a.lon, a.size)
        sf.ready(a.lat, a.lon, a.size, 120.0)
        road, water = snowcover.masks(frame, x0, y0, a.size, a.px, sf, None, md)
        print("map: road texels %s, water texels %s" % (None if road is None else int((road > 0).sum()),
                                                        None if water is None else
                                                        {int(v): int((water == v).sum()) for v in (1, 2, 3)}))
    t1 = time.perf_counter()
    snow = snowcover.apply(spec, photo, frame, x0, y0, a.size, U, x0, y0, a.size / (nn - 1), road, water)
    print("snow %.0f ms, spec %s perm %s" % (1000 * (time.perf_counter() - t1), spec and spec.sig, spec and spec.perm))
    Image.fromarray(np.asarray(photo, np.uint8)).save(os.path.join(a.out, f"{a.name}_photo.png"))
    Image.fromarray(np.asarray(snow, np.uint8)).save(os.path.join(a.out, f"{a.name}_snow.png"))
    panels = [np.asarray(photo, np.uint8), np.asarray(snow, np.uint8)]
    if not a.no_s2:
        s2, item = s2_square(frame, x0, y0, a.size, a.px, a.winter, a.cache)
        if s2 is not None:
            print("Sentinel-2", item["id"], item["properties"].get("eo:cloud_cover"))
            Image.fromarray(s2).save(os.path.join(a.out, f"{a.name}_s2.png"))
            panels.append(s2)
            with open(os.path.join(a.out, f"{a.name}_s2.json"), "w") as f:
                json.dump({"id": item["id"], "datetime": item["properties"].get("datetime"),
                           "cloud": item["properties"].get("eo:cloud_cover")}, f)
        else:
            print("no winter Sentinel-2 scene")
    gap = np.full((a.px, 8, 3), 255, np.uint8)
    sheet = np.concatenate(sum(([p, gap] for p in panels), [])[:-1], axis=1)
    Image.fromarray(sheet).save(os.path.join(a.out, f"{a.name}_sheet.png"))
    print("wrote", os.path.join(a.out, f"{a.name}_sheet.png"))


if __name__ == "__main__":
    main()
