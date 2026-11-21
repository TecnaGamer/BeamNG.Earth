#!/usr/bin/env python3
"""Generated ground (server/genground.py, BNE_GEN_GROUND) next to the photo path, offline: one 1 km ground block
centred on LAT LON, as the game's base photo (earthd.Server.write_base: the roofs filled, the ghosts of the roads
removed, enlarged to 2048, the roads / lots / furrows painted in; the cliffs' rock left out, it needs the DEM) made
three ways, and the finest public orthophoto of the same square (tools/ortho_hi.py) as the truth:

    python3 tools/genground_compare.py --lat 39.2826 --lon -76.5927 --name fells --out ~/bnge-tests/genground/compare

- photo:  the default imagery chain (the regional 1 m sources, NAIP...), as the game draws it today;
- s2:     the chain cut to Sentinel-2 (genground.chain) through the same steps, without the generator;
- gen:    the generated ground (genground.generate on that Sentinel-2 render, then the same steps).
Writes <out>/<name>_{truth,photo,s2,gen,kinds}.png (2048 px, 0.5 m a pixel), <name>_crop{256,128}.png (side by side:
truth | photo | s2 | gen, centred), <name>_stats.json (requests and bytes per path, generator timings).

--cache: a scratch download cache (default ~/bnge-tests/genground/cache; never the player's real cache): what it
needs is downloaded there once (map data cells, Sentinel-2 packs, the photo sources' tiles).
"""
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
os.environ.setdefault("BNE_GEN_GROUND", "on")

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import buildings as buildingsmod  # noqa: E402
import cdn as cdnmod  # noqa: E402
import earthd  # noqa: E402
import genground  # noqa: E402
import geodesy  # noqa: E402
import heightmap  # noqa: E402
import imagery as imagerymod  # noqa: E402
import mapcells  # noqa: E402
import mapdata as mapdatamod  # noqa: E402
import netfetch  # noqa: E402
import surface as surfacemod  # noqa: E402

# the kinds map's colours, one per genground kind (KIND_NAMES' order; stage 2's surfaces and patterns each their own)
KIND_RGB = np.array([(0, 0, 0), (20, 80, 20), (110, 130, 60), (120, 200, 90), (220, 200, 90), (200, 120, 120),
                     (190, 170, 140), (250, 250, 250), (40, 90, 200), (60, 160, 150), (150, 240, 120),
                     (90, 90, 90), (200, 40, 40),
                     (170, 160, 140),   # gravel
                     (200, 170, 120),   # path
                     (180, 70, 50),     # brick
                     (210, 200, 185),   # pavers
                     (235, 235, 225),   # concrete
                     (60, 170, 60),     # turf
                     (0, 220, 120),     # artificial turf
                     (230, 110, 40),    # clay
                     (200, 40, 120),    # tartan
                     (0, 200, 255),     # pool
                     (120, 100, 0),     # orchard
                     (140, 0, 140),     # vineyard
                     (255, 140, 200),   # allotment
                     (180, 255, 255),   # greenhouse
                     (100, 100, 160),   # cemetery
                     (60, 120, 160)],   # court
                    np.uint8)
assert len(KIND_RGB) == len(genground.KIND_NAMES)
LAYERS = ("roads", "buildings", "water", "land_use", "infrastructure", "landcover", "canopy")


def dir_bytes(path):
    n, b = 0, 0
    for d, _s, fs in os.walk(path):
        for f in fs:
            try:
                b += os.path.getsize(os.path.join(d, f))
                n += 1
            except OSError:
                pass
    return n, b


def finish(rgb, frame, x0, size, px, md, sf):
    """write_base's steps after the imagery (no rock faces): the photo the game gets (BASE_PX)."""
    rgb = np.asarray(rgb, np.uint8)
    mask = buildingsmod.photo_mask(frame, x0, x0, size, px, md)
    if mask is not None:
        rgb = buildingsmod.fill_photo(rgb, mask, size, px)
    rgb = surfacemod.unghost_square(rgb, frame, x0, x0, size, sf, None)
    rgb = surfacemod.enlarge(rgb, earthd.BASE_PX)
    return np.asarray(surfacemod.paint_square(rgb, frame, x0, x0, size, sf, None), np.uint8)


def label(img, text):
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 8 * len(text) + 8, 18), fill=(0, 0, 0))
    d.text((4, 3), text, fill=(255, 255, 255))
    return img


def crops(name, out, imgs, metres, px_out=512):
    """Side by side crops (metres square, centred) of the 2048 px images (0.5 m a pixel)."""
    n = imgs[0][1].shape[0]
    half = int(metres / (1024.0 / n) / 2)
    c = n // 2
    tiles = []
    for lab, a in imgs:
        im = Image.fromarray(np.asarray(a, np.uint8)[c - half:c + half, c - half:c + half]).resize(
            (px_out, px_out), Image.LANCZOS)
        tiles.append(label(im, f"{lab} ({metres} m)"))
    sheet = Image.new("RGB", (px_out * len(tiles) + 4 * (len(tiles) - 1), px_out), (255, 255, 255))
    for i, t in enumerate(tiles):
        sheet.paste(t, (i * (px_out + 4), 0))
    p = os.path.join(out, f"{name}_crop{metres}.png")
    sheet.save(p)
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--out", default=os.path.expanduser("~/bnge-tests/genground/compare"))
    ap.add_argument("--cache", default=os.path.expanduser("~/bnge-tests/genground/cache"))
    ap.add_argument("--no-truth", action="store_true")
    ap.add_argument("--label", default="gen", help="this generator's column and file names")
    ap.add_argument("--no-trees", dest="trees", action="store_false", help="as a server with --trees off")
    ap.add_argument("--no-shade", dest="shade", action="store_false",
                    help="(stage 5) the generator without taking the capture sun's shadows out (the before column)")
    ap.add_argument("--season", default="off", help="the generator's season grade (earthd --season: auto, fall, ...)")
    ap.add_argument("--also", nargs="*", default=[], help="earlier columns' finished images to show too (by label)")
    ap.add_argument("--truth-source", default="auto", help="tools/ortho_hi.py's SOURCE (naip: one export request; "
                    "auto may read a regional tile cache at z19-20: hundreds of tiles for a 1 km square)")
    a = ap.parse_args()
    if os.path.realpath(a.cache).startswith(os.path.realpath(mapdatamod.DEFAULT_CACHE)):
        sys.exit("--cache must be a scratch folder, not the player's cache")
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(a.cache, exist_ok=True)
    f = netfetch.Fetcher()
    md = mapdatamod.MapData(a.cache, f)
    sf = surfacemod.Surface(md)
    frame = geodesy.EnuFrame(a.lat, a.lon)
    size = 1024 * heightmap.SQUARE
    x0 = -size / 2 - earthd.BASE_SHIFT_M
    px = earthd.BASE_RENDER_PX
    stats = {"lat": a.lat, "lon": a.lon, "size_m": size, "px": px}

    t0 = time.perf_counter()
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0 + size]), np.array([x0, x0 + size]), 0.0)
    keys = mapcells.cells_for_bbox(float(la.min()) - 0.002, float(lo.min()) - 0.003, float(la.max()) + 0.002,
                                   float(lo.max()) + 0.003)
    for k in keys:
        md.cell(k, layers=LAYERS, kind="demand")
    stats["mapdata_s"] = round(time.perf_counter() - t0, 2)
    stats["cells"] = len(keys)
    print(f"map data: {len(keys)} cells in {stats['mapdata_s']} s")

    full = cdnmod.chain(imagerymod.DEFAULT_CHAIN, f, a.cache)
    results = {}
    for tag, names in (("photo", full), ("s2", genground.chain(full))):
        im = imagerymod.Imagery(os.path.join(a.cache, "imagery"), names, fetcher=f)
        before = {s: dir_bytes(os.path.join(a.cache, "imagery", s)) for s in names}
        t1 = time.perf_counter()
        im.fetch_lazy(frame, x0, x0, size, px)
        im.fetch_needed(frame, x0, x0, size, px)
        rgb, used = im.render(frame, x0, x0, size, px, set())
        t2 = time.perf_counter()
        after = {s: dir_bytes(os.path.join(a.cache, "imagery", s)) for s in names}
        got = {s: (after[s][0] - before[s][0], after[s][1] - before[s][1]) for s in names
               if after[s] != before[s] or s in used}
        stats[tag] = {"chain": list(names), "render_s": round(t2 - t1, 2),
                      "used_texels": {k: int(v) for k, v in used.items()},
                      "new_files": {s: v[0] for s, v in got.items()},
                      "new_bytes": {s: v[1] for s, v in got.items()},
                      "on_disk": {s: after[s] for s in names if after[s][0]}}
        results[tag] = np.asarray(rgb, np.uint8)
        im.close()
        print(tag, stats[tag]["render_s"], "s", stats[tag]["used_texels"], stats[tag]["new_bytes"])

    # the generator (genground.render: the Sentinel-2 render with its halo, the regions, the kernel), timed warm (the
    # tiles on disk, the cells in memory)
    s2 = results["s2"]
    im = imagerymod.Imagery(os.path.join(a.cache, "imagery"), genground.chain(full), fetcher=f, season_mode=a.season)
    veg = None
    if a.trees:                 # (the game's trees, as the server's: its cells built here first, into the scratch cache)
        import vegetation as vegetationmod
        veg = vegetationmod.Vegetation(md, a.cache)
        veg.surface = sf
        t0 = time.perf_counter()
        hx0_, _hy, hs_, _hp, _h = genground.halo_square(x0, x0, size, px)
        la_, lo_, _ = frame.enu_to_geodetic(np.array([hx0_ - 20, hx0_ + hs_ + 20]),
                                             np.array([hx0_ - 20, hx0_ + hs_ + 20]), 0.0)
        for k in mapcells.cells_for_bbox(float(la_.min()), float(lo_.min()), float(la_.max()), float(lo_.max())):
            veg.cell(k)
        stats["trees_build_s"] = round(time.perf_counter() - t0, 1)
    dem = None
    if a.shade:                 # (stage 5: the ground's heights for the terrain's shadows, fetched here first)
        import dem as demmod
        dem = demmod.Dem(os.path.join(a.cache, "dem"), fetcher=f)
        t0 = time.perf_counter()
        e = genground.SHADE_PAD_M + genground.HALO_M + 2600.0       # (and the data cells' fits round the block)
        la_, lo_, _ = frame.enu_to_geodetic(np.array([x0 - e, x0 + size + e]), np.array([x0 - e, x0 + size + e]), 0.0)
        dem.prefetch(genground._dem_keys(la_, lo_))
        e2 = genground.HALO_M + 2600.0
        im.fetch_needed(frame, x0 - e2, x0 - e2, size + 2 * e2, int((size + 2 * e2) / genground.FIT_RES_B))
        stats["shade_prefetch_s"] = round(time.perf_counter() - t0, 1)
    else:
        genground.shade = lambda rgb, *args, **kw: (rgb, args[6])
    times = []
    for _ in range(3):
        c0, w0 = time.process_time(), time.perf_counter()
        pend = set()
        gen, _used = genground.render(im, frame, x0, x0, size, px, md, sf, None, pend, veg, dem)
        times.append((time.perf_counter() - w0, time.process_time() - c0))
    hx0, _hy0, hsize, hpx, h = genground.halo_square(x0, x0, size, px)
    c0, w0 = time.process_time(), time.perf_counter()
    kind, region, canopy, complete = genground.inputs(frame, hx0, hx0, hsize, hpx, md, sf, None, veg)[:4]
    stats["gen_inputs_wall_ms"] = round(1000 * (time.perf_counter() - w0), 1)
    stats["gen_inputs_cpu_ms"] = round(1000 * (time.process_time() - c0), 1)
    again, _ = genground.render(im, frame, x0, x0, size, px, md, sf, None, set(), veg, dem)
    stats["shade_fits"] = [v[3] for v in genground._fits.values()]
    stats["pending"] = sorted(str(p) for p in pend)
    im.close()
    stats["deterministic_rerun"] = bool(np.array_equal(again, gen))
    stats["complete"] = bool(complete) and not pend
    tm = np.array(times)
    stats["gen_total_wall_ms"] = round(1000 * float(np.median(tm[:, 0])), 1)
    stats["gen_total_cpu_ms"] = round(1000 * float(np.median(tm[:, 1])), 1)
    kind, canopy = kind[h:h + px, h:h + px], canopy[h:h + px, h:h + px]
    stats["kinds_share"] = {genground.KIND_NAMES[k]: round(float(np.mean(kind == k)), 4)
                            for k in range(len(genground.KIND_NAMES)) if np.any(kind == k)}
    stats["canopy_known"] = round(float(np.mean(canopy != genground.CANOPY_NODATA)), 4)
    print("generator", times[-1], stats["kinds_share"])

    imgs = []
    for tag, rgb in (("photo", results["photo"]), ("s2", s2), (a.label, gen)):
        fin = finish(rgb, frame, x0, size, px, md, sf)
        Image.fromarray(fin).save(os.path.join(a.out, f"{a.name}_{tag}.png"))
        imgs.append((tag, fin))
    for tag in a.also:           # (earlier versions' finished images, kept: side by side with this one)
        p = os.path.join(a.out, f"{a.name}_{tag}.png")
        if os.path.exists(p):
            imgs.insert(len(imgs) - 1, (tag, np.asarray(Image.open(p).convert("RGB"), np.uint8)))
    Image.fromarray(KIND_RGB[np.minimum(kind, len(KIND_RGB) - 1)]).resize((2048, 2048), Image.NEAREST).save(
        os.path.join(a.out, f"{a.name}_{a.label}_kinds.png"))
    truth = None
    if not a.no_truth:
        tp = os.path.join(a.out, f"{a.name}_truth.png")
        if not os.path.exists(tp):
            subprocess.run([sys.executable, os.path.join(ROOT, "tools", "ortho_hi.py"), str(a.lat), str(a.lon),
                            str(size / 2), tp, a.truth_source, "2048"], check=False)
        if os.path.exists(tp):
            truth = np.asarray(Image.open(tp).convert("RGB").resize((2048, 2048), Image.LANCZOS), np.uint8)
    sheet = ([("truth", truth)] if truth is not None else []) + imgs
    for m in (1024, 256, 128):
        print(crops(f"{a.name}_{a.label}", a.out, sheet, m))
    with open(os.path.join(a.out, f"{a.name}_stats.json"), "w") as fh:
        json.dump(stats, fh, indent=1)
    print(json.dumps(stats, indent=1))
    sf.close()


if __name__ == "__main__":
    main()
