"""Replay sidewalk development context from compact fixtures or read-only local map caches.

No MapData/network client is constructed. --capture copies selected original Features into a
portable NPZ plus a JSON provenance manifest; it never changes the source download cache.
"""
import argparse
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import date
import json
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import mapgeom  # noqa: E402


FIXTURE = Path(__file__).resolve().parents[1] / "server/tests/fixtures/sidewalk_development.npz"
LAYERS = ("buildings", "land_use", "places")
CASES = (
    ("cheyenne_downtown", "Cheyenne downtown", 41.1335, -104.8150, "Capitol Avenue", "rich"),
    ("cheyenne_residential", "Cheyenne residential", 41.1477, -104.8290, "Bent Avenue", "limited"),
    ("laramie_downtown", "Laramie downtown", 41.3110, -105.5940, "South 2nd Street", "rich"),
    ("horse_creek", "Horse Creek Road", 41.2183589, -104.8330190, "Horse Creek Road", "rural"),
    ("round_top", "Round Top Road", 41.2182404, -104.9002486, "Roundtop Road", "rural"),
    ("happy_jack", "Happy Jack Road", 41.1931691, -105.2014728, "Happy Jack Road", "rural"),
)



def _cached_cell(view, key, layer):
    """A cached map-data cell file as Features, or None (this tool reads the cache only; never a download)."""
    import numpy as np
    import mapgeom
    path = Path(view.path(key, layer))
    if not path.is_file():
        return None
    with np.load(path, allow_pickle=False) as z:
        arrays = {n: z[n] for n in z.files}
    try:
        return mapgeom.Features.from_arrays(layer, arrays, "")
    except (KeyError, ValueError):
        return None

def subset(f, indexes):
    """Copy complete source geometry, attributes and rules for selected source rows."""
    indexes = np.asarray(indexes, dtype=np.int64)
    out = mapgeom.build(f.layer, f.ids[indexes], [(int(f.kind[i]), f.parts(i)) for i in indexes])
    out.attrs = {name: values[indexes] for name, values in f.attrs.items()}
    out.own = None if f.own is None else f.own[indexes]
    out.clipped = None if f.clipped is None else f.clipped[indexes]
    for name, rule in f.rules.items():
        rows = [(j, value, (start, end)) for j, i in enumerate(indexes)
                for value, start, end in rule.for_feature(i)]
        out.rules[name] = mapgeom.rule_table(rows, "text" if rule.value.dtype.kind == "U" else "float")
    return out


def cache_path(cache_dir, release, layer, key):
    root = Path(cache_dir) / "mapdata/v2/overture" / release
    names = [layer] if layer != "roads" else [f"roads.{v}" for v in (9, 8, 7, 6, 4)]
    for name in names:
        path = root / name / str(key.z) / str(key.x) / f"{key.y}.npz"
        if path.is_file():
            return path
    return None


def read_features(path, layer):
    with np.load(path, allow_pickle=False) as archive:
        arrays = {name: archive[name] for name in archive.files}
    if "missing" in arrays:
        return None, {}
    provenance = json.loads(str(arrays["provenance"])) if "provenance" in arrays else {}
    return mapgeom.Features.from_arrays(layer, arrays, ""), provenance


def nearby_keys(frame, radius):
    x = np.array([-radius, radius, radius, -radius])
    y = np.array([-radius, -radius, radius, radius])
    lat, lon, _ = frame.enu_to_geodetic(x, y, 0.0)
    return mapcells.cells_for_bbox(float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max()))


def road_near(cache_dir, release, frame, name):
    candidates = []
    seen = set()
    for key in nearby_keys(frame, 700.0):
        path = cache_path(cache_dir, release, "roads", key)
        if path is None:
            continue
        f, provenance = read_features(path, "roads")
        if f is None:
            continue
        names = f.attrs.get("name", np.full(len(f), ""))
        for i in np.flatnonzero(np.char.lower(names.astype(str)) == name.lower()):
            if str(f.ids[i]) in seen:
                continue
            seen.add(str(f.ids[i]))
            for part in f.parts(i, f.xy_in(frame)):
                if not part or len(part[0]) < 2:
                    continue
                xy = part[0]
                d = np.diff(xy, axis=0)
                length2 = np.sum(d * d, axis=1)
                t = np.clip(-np.sum(xy[:-1] * d, axis=1) / np.maximum(length2, 1e-12), 0.0, 1.0)
                points = xy[:-1] + t[:, None] * d
                j = int(np.argmin(np.linalg.norm(points, axis=1)))
                if length2[j] < 0.01:
                    continue
                tangent = d[j] / np.sqrt(length2[j])
                candidates.append((float(np.linalg.norm(points[j])), f, i, points[j], tangent, path, provenance))
    if not candidates:
        raise ValueError(f"No cached road named {name!r} within 700 m")
    return min(candidates, key=lambda c: c[0])


def capture(cache_dir, release, fixture, extent=450.0):
    arrays = {}
    manifest = {"captured": date.today().isoformat(), "release": release, "pipeline": 2,
                "extent_m": extent, "source_cache": "mapdata/v2/overture/" + release,
                "source": "Original decoded local Overture mapdata; no network requests or source-cache writes.",
                "cases": []}
    for case_id, label, lat, lon, road_name, expectation in CASES:
        frame = geodesy.EnuFrame(lat, lon)
        distance, road, i, point, tangent, path, provenance = road_near(cache_dir, release, frame, road_name)
        half_width = {"primary": 4.0, "secondary": 4.0, "tertiary": 3.1, "residential": 3.0}.get(
            str(road.attrs.get("class", [""] * len(road))[i]), 3.0)
        case = {"id": case_id, "name": label, "latitude": lat, "longitude": lon,
                "expected_pattern": expectation, "road_name": road_name, "road_id": str(road.ids[i]),
                "road_distance_m": distance, "query_xy": point.tolist(),
                "outward": [-float(tangent[1]), float(tangent[0])], "road_half_width": half_width,
                "box": [-128.0, -128.0, 128.0, 128.0], "layers": {}, "sources": []}
        arrays.update(subset(road, [i]).to_arrays(case_id + "/road/"))
        case["sources"].append({"layer": "roads", "path": path.relative_to(Path(cache_dir)).as_posix(),
                                "provenance": provenance})
        for layer in LAYERS:
            case["layers"][layer] = []
            for slot, key in enumerate(nearby_keys(frame, extent)):
                path = cache_path(cache_dir, release, layer, key)
                if path is None:
                    case["sources"].append({"layer": layer, "cell": str(key), "missing": True})
                    continue
                f, provenance = read_features(path, layer)
                if f is None:
                    continue
                boxes = f.boxes(frame)
                select = np.flatnonzero((boxes[:, 0] <= extent) & (boxes[:, 2] >= -extent)
                                        & (boxes[:, 1] <= extent) & (boxes[:, 3] >= -extent))
                selected = subset(f, select)
                prefix = f"{case_id}/{layer}/{slot}/"
                arrays.update(selected.to_arrays(prefix))
                case["layers"][layer].append(prefix)
                case["sources"].append({"layer": layer, "cell": str(key), "rows": len(selected),
                                        "path": path.relative_to(Path(cache_dir)).as_posix(), "provenance": provenance})
        manifest["cases"].append(case)
    fixture = Path(fixture)
    fixture.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(fixture, **arrays)
    fixture.with_suffix(".json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def fixture_cases(fixture):
    fixture = Path(fixture)
    manifest = json.loads(fixture.with_suffix(".json").read_text(encoding="utf-8"))
    with np.load(fixture, allow_pickle=False) as archive:
        arrays = {name: archive[name] for name in archive.files}
    for case in manifest["cases"]:
        frame = geodesy.EnuFrame(case["latitude"], case["longitude"])
        layers = {layer: [mapgeom.Features.from_arrays(layer, arrays, prefix) for prefix in prefixes]
                  for layer, prefixes in case["layers"].items()}
        yield case, frame, layers


def serializable(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return value
    return vars(value)


def audit(fixture, radius=100.0, repeats=1, source_cache=None, release="2026-09-23.1"):
    import development
    report = []
    for case, frame, layers in fixture_cases(fixture):
        context_layers = layers
        if source_cache is not None:
            # Exercise the production cache reader with complete original cells. This view
            # deliberately has no cell/fetch/prefetch APIs and cannot start a download.
            class CachedView:
                def path(self, key, layer):
                    got = cache_path(source_cache, release, layer, key)
                    return str(got if got is not None else Path(source_cache) / "mapdata/v2/overture" / release
                               / layer / str(key.z) / str(key.x) / f"{key.y}.npz")
            view = CachedView()
            keys = nearby_keys(frame, 128 + development._halo(radius))
            layers = {}

            def load():
                layers.update({name: [] for name in LAYERS})
                for key in keys:
                    for name in LAYERS:
                        f = _cached_cell(view, key, name)
                        if f is not None:
                            layers[name].append(f)
                return layers
            context_layers = load
        start = time.perf_counter()
        context = development.Context(frame, tuple(case["box"]), context_layers, radius=radius)
        built_ms = (time.perf_counter() - start) * 1000
        point = np.asarray(case["query_xy"])
        outward = np.asarray(case["outward"])
        cold_start = time.perf_counter()
        context.sample(point, outward, case["road_half_width"])
        cold_ms = (time.perf_counter() - cold_start) * 1000
        sample_start = time.perf_counter()
        samples = []
        for _ in range(repeats):
            samples = [context.sample(point, side * outward, case["road_half_width"]) for side in (1, -1)]
        sample_ms = (time.perf_counter() - sample_start) * 1000 / (2 * repeats)
        counts = {layer: len({str(fid) for f in fs for fid in f.ids}) for layer, fs in layers.items()}
        categories = Counter()
        seen = set()
        for f in layers.get("places", []):
            xy = f.xy_in(frame)
            for i in range(len(f)):
                fid = str(f.ids[i])
                if fid in seen:
                    continue
                seen.add(fid)
                parts = f.parts(i, xy)
                if parts and parts[0] and len(parts[0][0]) and np.linalg.norm(parts[0][0][0] - point) <= radius:
                    categories[str(f.attrs.get("basic_category", [""] * len(f))[i])] += 1
        report.append({"id": case["id"], "name": case["name"], "road_name": case["road_name"],
                       "input": "download_cache" if source_cache is not None else "portable_fixture",
                       "expected_pattern": case["expected_pattern"], "source_unique_features": counts,
                       "context_ms": round(built_ms, 3), "first_sample_ms": round(cold_ms, 3),
                       "warm_sample_ms": round(sample_ms, 4),
                       "nearby_poi_categories": dict(categories.most_common()),
                       "sides": [serializable(s) for s in samples]})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--capture", action="store_true", help="copy original cached local Features into fixture")
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/beamng-earth")
    parser.add_argument("--source-cache", action="store_true", help="audit complete cached cells instead of fixture vectors")
    parser.add_argument("--release", default="2026-09-23.1")
    parser.add_argument("--extent", type=float, default=450.0)
    parser.add_argument("--radius", type=float, default=100.0)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--capture-only", action="store_true")
    args = parser.parse_args()
    if args.capture:
        capture(args.cache_dir, args.release, args.fixture, args.extent)
    if args.capture_only:
        return
    report = audit(args.fixture, args.radius, max(1, args.repeats),
                   args.cache_dir if args.source_cache else None, args.release)
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
