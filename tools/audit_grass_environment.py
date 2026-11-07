"""Offline geographic audit of environment-aware grass using an existing Earth cache.

Example (from the repository root):
  python tools/audit_grass_environment.py --cache-dir C:/Users/you/.cache/beamng-earth --offline

The input cache is read-only. Missing imagery, vector layers and DEM samples are
reported, never downloaded. Outputs are diagnostic aerial imagery, palette maps
and a JSON report; this does not run or visually verify BeamNG.drive.
"""
import argparse
import io
import json
import math
import sys
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import dem  # noqa: E402
import geodesy  # noqa: E402
import grass  # noqa: E402
import grass_exclusions  # noqa: E402
import grass_imagery  # noqa: E402
import imagery  # noqa: E402
import mapcells  # noqa: E402
import mapdata  # noqa: E402
import mapgeom  # noqa: E402
import maprasters  # noqa: E402
import netfetch  # noqa: E402
import surface  # noqa: E402

SITES = {
    "cheyenne-prairie": (41.139, -104.950),
    "happy-jack-road": (41.1931691, -105.2014728),
    "curt-gowdy": (41.1773, -105.2306),
    "colorado-grassland": (40.12, -104.95),
    "colorado-cached-north": (40.896, -104.874),
    "denver-cached-green": (39.757, -105.006),
    "columbia-wet-forest": (39.211, -76.864),
    "baltimore-cached-green": (39.289, -76.58),
    "cheyenne-mixed": (41.13036, -104.81211),
}
COLORS = np.array(((179, 144, 76), (136, 165, 89), (46, 99, 49), (90, 90, 90)), np.uint8)


class ReadOnlyImagery(imagery.Imagery):
    def _download(self, src, z, x, y):
        path, marker = self._paths(src, z, x, y)
        try:
            data = Path(path).read_bytes()
        except OSError:
            if Path(marker).exists():
                return None
            self._count(src.name + ":offline_missing")
            raise imagery.ImageryError(f"not cached: {path}") from None
        self._count(src.name + ":disk")
        return data

    def _discard(self, *args):
        pass                       # A damaged input must not be deleted by an audit.

    def _fin_keep(self, key, finished):
        # Match the game's finished JPEG representation, but keep it in memory.
        if not finished.uniform:
            buffer = io.BytesIO()
            Image.fromarray(finished.data).save(buffer, "JPEG", quality=imagery.FIN_QUALITY, subsampling=0)
            finished.data = buffer.getvalue()
        self._fin_put(key, finished)


class CachedMapData:
    """Read current-release map cache files, with an explicit old-road fallback."""
    def __init__(self, cache_dir):
        self.cache_dir = str(cache_dir)
        self.root = Path(cache_dir) / "mapdata" / f"v{mapdata.VERSION}"
        overture = self.root / "overture"
        self.release = sorted(p.name for p in overture.iterdir() if p.is_dir())[-1] if overture.exists() else "absent"

    def path(self, key, layer):
        if layer == "landcover":
            base = self.root / "worldcover" / maprasters.WORLDCOVER_RELEASE / layer
        else:
            name = f"{layer}.{mapdata.LAYER_VERSIONS[layer]}" if layer in mapdata.LAYER_VERSIONS else layer
            base = self.root / "overture" / self.release / name
        path = base / str(key.z) / str(key.x) / f"{key.y}.npz"
        if layer == "roads" and not path.exists():
            candidates = sorted((self.root / "overture" / self.release).glob(f"roads.*/{key.z}/{key.x}/{key.y}.npz"))
            if candidates:
                return str(candidates[-1])
        return str(path)

    @lru_cache(maxsize=64)
    def read(self, key, layer):
        path = Path(self.path(key, layer))
        if not path.exists():
            return None
        try:
            with np.load(path, allow_pickle=False) as data:
                arrays = {k: data[k] for k in data.files}
            if "missing" in arrays:
                return None
            if layer == "landcover":
                return maprasters.Raster(arrays["data"], **json.loads(str(arrays["meta"])))
            return mapgeom.Features.from_arrays(layer, arrays, "")
        except (OSError, ValueError, KeyError):
            return None


def cached_heights(cache_dir, lat, lon):
    """Bilinear samples from existing decoded ground DEM, no recomputation/writes."""
    zoom = dem.GLOBAL_ZOOM
    n = 2 ** zoom
    fx = (lon + 180) / 360 * n
    fy = (1 - np.arcsinh(np.tan(np.radians(lat))) / math.pi) * n / 2
    tx, ty = np.floor(fx).astype(int), np.floor(fy).astype(int)
    out = np.full(lat.shape, np.nan)
    root = Path(cache_dir) / "dem" / "decoded" / f"v{dem.DECODED_VERSION}" / "g1" / str(zoom)
    for x, y in set(zip(tx.ravel().tolist(), ty.ravel().tolist())):
        path = root / str(x) / f"{y}.npy"
        if not path.exists():
            continue
        data = np.load(path, mmap_mode="r", allow_pickle=False)
        inside = (tx == x) & (ty == y)
        px = np.clip((fx[inside] - x) * data.shape[1] - .5, 0, data.shape[1] - 1.001)
        py = np.clip((fy[inside] - y) * data.shape[0] - .5, 0, data.shape[0] - 1.001)
        ix, iy = px.astype(int), py.astype(int)
        dx, dy = px - ix, py - iy
        out[inside] = ((data[iy, ix] * (1 - dx) + data[iy, ix + 1] * dx) * (1 - dy)
                       + (data[iy + 1, ix] * (1 - dx) + data[iy + 1, ix + 1] * dx) * dy)
    return out


def audit_site(name, location, args, im, md):
    lat0, lon0 = location
    frame = geodesy.EnuFrame(lat0, lon0)
    start = time.perf_counter()
    rgb, used, colors = grass_imagery.render_base(im, frame, -args.size / 2, -args.size / 2,
                                                 args.size, args.px, set())
    render_s = time.perf_counter() - start
    n = colors.shape[0]
    step = args.size / n
    xs = -args.size / 2 + (np.arange(n) + .5) * step
    ys = args.size / 2 - (np.arange(n) + .5) * step
    east, north = np.meshgrid(xs, ys)
    lat, lon, _ = frame.enu_to_geodetic(east, north, 0.0)
    classes = np.zeros(lat.shape, np.uint8)
    cache_layers = {key: [] for key in ("landcover", "roads", *grass_exclusions.LAYERS)}
    keys = list(mapcells.cells_for_points(lat, lon))
    roads, lots = [], []
    for key in keys:
        for layer in cache_layers:
            data = md.read(key, layer)
            if data is not None:
                cache_layers[layer].append(str(key))
            if layer == "landcover" and data is not None:
                w, s, e, nn = key.bounds()
                inside = (lon >= w) & (lon < e) & (lat >= s) & (lat < nn)
                classes[inside] = data.sample(lon[inside], lat[inside])
            elif layer == "roads" and data is not None:
                roads.append(data)
            elif layer in ("infrastructure", "land_use") and data is not None:
                lots.append(data)
    heights = cached_heights(args.cache_dir, lat, lon)
    dem_complete = bool(np.isfinite(heights).all())
    # Missing DEM is explicitly reported; flat heights here mean no slope veto
    # can be assessed for this diagnostic, never that actual terrain is flat.
    idx = surface.classify(classes, heights if dem_complete else np.zeros(lat.shape), step, lat, lon)
    before = np.isin(idx, (surface.GRASS, surface.DUSTY))
    if roads:
        def urban(la, lo):
            out = np.zeros(np.shape(la), bool)
            for key in mapcells.cells_for_points(la, lo):
                raster = md.read(key, "landcover")
                if raster is not None:
                    out |= raster.sample(lo, la) == 50
            return out
        idx = surface.paint_roads(idx, lat, lon, roads, step, urban)
    if lots:
        idx = surface.paint_lots(idx, lat, lon, lots)
    eligible = np.isin(idx, (surface.GRASS, surface.DUSTY))
    polygon_veto = grass_exclusions.blocked(md, lat, lon, eligible)
    eligible &= ~polygon_veto
    # Lawns retain the existing short-grass rule; the environmental palette
    # applies to grassland and shrubland, exactly as grass_layers.apply does.
    dry_prior = (idx == surface.DUSTY) | surface.dry_west(lat, lon)
    prior = np.where(dry_prior[..., None], np.array([1, 0, 0]), np.array([0, 0, 1]))
    weights = grass.color_weights(colors, prior)
    selected = grass.choose(weights, lat, lon, eligible)
    counts = np.bincount(selected[eligible].ravel(), minlength=3)
    shares = counts / max(1, counts.sum())
    real = sum(value for key, value in used.items() if key in imagery.SOURCES)
    report = {
        "site": name, "lat": lat0, "lon": lon0, "size_m": args.size,
        "image_sources": used, "imagery_real_fraction": real / (args.px * args.px),
        "imagery_render_s": round(render_s, 3), "analysis_px": n,
        "eligible_samples": int(eligible.sum()), "eligible_fraction": float(eligible.mean()),
        "surface_veto_samples": int((before & ~np.isin(idx, (surface.GRASS, surface.DUSTY))).sum()),
        "polygon_veto_samples": int(polygon_veto.sum()),
        "lawn_samples_outside_environment_palette": int((idx == surface.LAWN).sum()),
        "palette_counts": dict(zip(grass.PALETTES, counts.tolist())),
        "palette_shares": dict(zip(grass.PALETTES, np.round(shares, 4).tolist())),
        "worldcover_counts": {maprasters.CLASSES.get(int(k), str(k)): int(v)
                              for k, v in zip(*np.unique(classes, return_counts=True))},
        "cached_map_cells": {layer: len(found) for layer, found in cache_layers.items()},
        "total_map_cells": len(keys), "dem_complete": dem_complete,
        "dem_fraction": float(np.isfinite(heights).mean()),
        "limitations": ["Diagnostic cached aerial imagery; no in-game visual verification.",
                        "Existing cached source data, tone=1, season=off; no de-shadow or color-match analysis.",
                        "Road widths use normal class rules without measured-width cache.",
                        "Missing vector cache layers cannot be checked; no new requests are made."],
    }
    if real == 0:
        report["limitations"].append("No real imagery cached: palette result is fallback-only and not a geographic validation.")
    if not dem_complete:
        report["limitations"].append("Decoded ground DEM absent: slope exclusion could not be fully assessed.")
    panel = Image.new("RGB", (768, 420), "#f5f3ea")
    panel.paste(Image.fromarray(rgb).resize((384, 384), Image.Resampling.BOX), (0, 28))
    palette = np.where(eligible, selected, 3)
    panel.paste(Image.fromarray(COLORS[palette]).resize((384, 384), Image.Resampling.NEAREST), (384, 28))
    draw = ImageDraw.Draw(panel)
    draw.text((8, 8), f"{name}: cached aerial imagery | grass palette (gray = excluded)", fill="#111111")
    draw.text((8, 406), "Dry gold / light green / lush dark green; diagnostic only", fill="#111111")
    panel.save(args.output / f"{name}.png")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "beamng-earth")
    parser.add_argument("--offline", action="store_true", default=True, help="Always offline; included for explicit reproducibility.")
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "grass-environment")
    parser.add_argument("--site", action="append", choices=SITES, help="Repeat to audit a subset.")
    parser.add_argument("--size", type=float, default=1024.0)
    parser.add_argument("--px", type=int, default=1024)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    fetcher = netfetch.Fetcher(offline=True)
    im = ReadOnlyImagery(str(args.cache_dir / "imagery"), imagery.BASE_CHAIN, fetcher=fetcher,
                         tone=1.0, season_mode="off")
    md = CachedMapData(args.cache_dir)
    reports = []
    try:
        for name in args.site or SITES:
            try:
                report = audit_site(name, SITES[name], args, im, md)
            except Exception as error:
                report = {"site": name, "error": f"{type(error).__name__}: {error}"}
            reports.append(report)
            print(json.dumps(report), flush=True)
        result = {"offline": True, "cache_dir": str(args.cache_dir), "overture_release": md.release,
                  "imagery_attribution": im.attribution(), "sites": reports, "fetch_statistics": im.stats_snapshot()}
        (args.output / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    finally:
        im.close()


if __name__ == "__main__":
    main()
