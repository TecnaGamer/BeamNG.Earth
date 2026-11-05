"""Build the bundled worldwide OSM turbine index; no network calls happen at runtime.

Run in the project's server environment (including earthfast). DuckDB is also
needed for GeoParquet input; the OSM JSON input path only needs server dependencies.
Examples:
  python tools/build_windturbines.py --download
  python tools/build_windturbines.py generators.parquet --catalog collection.json
  python tools/build_windturbines.py turbines.json --min-count 100000

The default developer download is GMWID's complete worldwide generator layer.
Only original OSM rows are used; registry additions and derived region columns
are excluded. Nodes retain their exact CRS84 point. A way must have one closed
ring, with no holes or multiple outer polygons, then pass windturbines.py's
shared individual-footprint and duplicate checks. Farm polygons never create
an invented layout. Coincident source mappings resolve to one deterministic
representative without rounding or moving nearby distinct points. OSM data and
this derived index are ODbL 1.0.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import urllib.request

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import windturbines

SOURCE_URL = "https://parquetry.geomermaids.com/gmwid/latest/power_generator.parquet"
CATALOG_URL = "https://parquetry.geomermaids.com/gmwid/catalog/power/power_generator/collection.json"
ATTRIBUTION_URL = "https://parquetry.geomermaids.com/gmwid/ATTRIBUTION.txt"
DEFAULT_OUTPUT = ROOT / "server" / "wind_turbines.npz"
SCHEMA = 1
KINDS = {"node": 0, "way": 1}
DIMENSIONS = {"height": "height", "hub_height": "height:hub", "rotor_diameter": "rotor:diameter"}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _geometry(raw):
    """Decode only bounded 2D OGC WKB. Unknown encodings cannot become positions."""
    data, pos = memoryview(raw), 0

    def take(fmt):
        nonlocal pos
        size = struct.calcsize(fmt)
        if pos + size > len(data):
            raise ValueError("truncated WKB")
        values = struct.unpack_from(fmt, data, pos)
        pos += size
        return values

    def read(depth=0):
        if depth > 2:
            raise ValueError("nested WKB is not an individual turbine")
        order = take("B")[0]
        if order not in (0, 1):
            raise ValueError("invalid WKB endian flag")
        endian = "<" if order else ">"
        kind = take(endian + "I")[0]
        if kind == 1:
            return kind, take(endian + "dd")
        if kind not in (2, 3, 6):
            raise ValueError("unsupported WKB geometry")
        count = take(endian + "I")[0]
        if count > min(1000000, len(data)):
            raise ValueError("unbounded WKB geometry")
        if kind == 2:
            return kind, [take(endian + "dd") for _ in range(count)]
        if kind == 3:
            rings = []
            for _ in range(count):
                n = take(endian + "I")[0]
                if n > min(1000000, len(data) // 16):
                    raise ValueError("unbounded WKB ring")
                rings.append([take(endian + "dd") for _ in range(n)])
            return kind, rings
        return kind, [read(depth + 1) for _ in range(count)]

    parsed = read()
    if pos != len(data):
        raise ValueError("unexpected trailing WKB data")
    return parsed


def osm_element(kind, fid, tags, geometry):
    """Adapt one original OSM GeoParquet row, never its rounded bbox/derived centre."""
    if kind not in KINDS or not isinstance(tags, dict):
        return None
    try:
        shape, coords = _geometry(geometry)
        element = {"type": kind, "id": int(fid), "tags": tags}
        if kind == "node":
            if shape != 1:
                return None
            element.update(lon=coords[0], lat=coords[1])
        else:
            if shape == 6:
                if len(coords) != 1 or coords[0][0] != 3:
                    return None
                shape, coords = coords[0]
            if shape == 3:
                if len(coords) != 1:
                    return None
                coords = coords[0]
            elif shape != 2:
                return None
            element["geometry"] = [{"lon": x, "lat": y} for x, y in coords]
        return element
    except (ValueError, TypeError, struct.error, OverflowError):
        return None


def read_parquet(path, catalog=None):
    import duckdb
    with duckdb.connect() as database:
        rows = database.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()[0]
        if catalog is not None and rows != catalog.get("table:row_count"):
            raise ValueError("GeoParquet row count differs from its release catalog")
        query = """SELECT osm_type, osm_id, tags, geometry FROM read_parquet(?)
                   WHERE origin = 'osm' AND lifecycle = 'active' AND osm_type IN ('node', 'way')
                   AND (tags['generator:source'] = 'wind' OR tags['generator:method'] = 'wind_turbine')"""
        cursor = database.execute(query, [str(path)])
        elements, invalid, candidates = [], 0, 0
        while batch := cursor.fetchmany(4096):
            for kind, fid, tags, geometry in batch:
                candidates += 1
                element = osm_element(kind, fid, tags, geometry)
                if element is None:
                    invalid += 1
                else:
                    elements.append(element)
    return {"elements": elements}, {"source_rows": rows, "candidate_rows": candidates,
                                    "unsupported_geometry_rows": invalid}


def read_json(path):
    with Path(path).open(encoding="utf-8-sig") as source:
        document = json.load(source)
    if not isinstance(document, dict) or not isinstance(document.get("elements"), list):
        raise ValueError("OSM export must contain an elements array")
    if document.get("remark") or document.get("error"):
        raise ValueError("OSM export reports an error or incomplete query")
    return document, {"source_rows": len(document["elements"])}


def arrays_from_document(document, min_count=100000):
    features = sorted(windturbines.features_from_document(document),
                      key=lambda t: (t.lat, t.lon, KINDS[t.id.split(":")[1]], int(t.id.rsplit(":", 1)[1])))
    if len(features) < min_count:
        raise ValueError(f"only {len(features)} turbines; expected at least {min_count}; output left untouched")
    lat = np.array([t.lat for t in features], dtype=np.float64)
    lon = np.array([t.lon for t in features], dtype=np.float64)
    identity = [t.id.rsplit(":", 2)[1:] for t in features]
    arrays = {"lat": lat, "lon": lon,
              "osm_id": np.array([int(fid) for _, fid in identity], dtype=np.int64),
              "osm_type": np.array([KINDS[kind] for kind, _ in identity], dtype=np.uint8)}
    for column, tag in DIMENSIONS.items():
        values = [windturbines.length_m(t.tags.get(tag)) for t in features]
        arrays[column] = np.array([v if v is not None and v <= np.finfo(np.float32).max else np.nan
                                   for v in values], dtype=np.float32)
    return arrays


def build(source, output=DEFAULT_OUTPUT, catalog=None, source_url=None,
          retrieved_at=None, min_count=100000):
    source, output = Path(source), Path(output)
    if catalog is not None:
        if not isinstance(catalog, dict):
            catalog = json.loads(Path(catalog).read_text(encoding="utf-8"))
        if catalog.get("license") != "ODbL-1.0":
            raise ValueError("unexpected source license")
        if catalog.get("assets", {}).get("data", {}).get("file:size") != source.stat().st_size:
            raise ValueError("source size differs from its release catalog")
    if source.suffix.lower() == ".parquet":
        document, stats = read_parquet(source, catalog)
    else:
        document, stats = read_json(source)
    arrays = arrays_from_document(document, min_count)
    stats.update(turbines=len(arrays["lat"]), nodes=int((arrays["osm_type"] == 0).sum()),
                 ways=int((arrays["osm_type"] == 1).sum()),
                 dimensions={key: int(np.isfinite(arrays[key]).sum()) for key in DIMENSIONS})
    metadata = {"schema": SCHEMA, "description": "Mapped individual operating OSM wind generators worldwide",
                "source_url": source_url or (SOURCE_URL if source.suffix.lower() == ".parquet" else None),
                "catalog_url": CATALOG_URL if catalog is not None else None,
                "attribution_url": ATTRIBUTION_URL if catalog is not None else None,
                "source_version": catalog.get("version") if catalog else None,
                "source_updated": catalog.get("updated") if catalog else None,
                "osm_snapshot": catalog.get("extent", {}).get("temporal", {}).get("interval") if catalog else None,
                "retrieved_at_utc": retrieved_at or datetime.now(timezone.utc).isoformat(),
                "source_bytes": source.stat().st_size, "source_sha256": sha256(source),
                "license": "ODbL-1.0", "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
                "attribution": "© OpenStreetMap contributors", "osm_copyright_url": "https://www.openstreetmap.org/copyright",
                "filter": "origin=osm; lifecycle=active; power=generator; wind source/method; individual nodes or suitable small closed ways",
                "coordinate_system": "OGC:CRS84 (WGS84 longitude, latitude); node coordinates are exact source float64",
                "way_position": "Area centroid of one mapped small closed ring; multiple polygons/holes, large farm areas and duplicate node footprints rejected",
                "dimensions": "Metres from height, height:hub, rotor:diameter; NaN means missing or malformed; no dimensions inferred",
                "osm_type": {"0": "node", "1": "way"},
                "osm_versions": "Not supplied by the GeoParquet source; stable element type/ID retained",
                "sort": "latitude, longitude, element type, element ID", "counts": stats}
    metadata["deduplication"] = "Element type/ID; node inside individual way; exact coincident positions prefer node, newest version, lowest numeric ID. Distinct nearby points are not rounded or merged."
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".npz", delete=False) as temporary:
        temp_path = Path(temporary.name)
    try:
        np.savez_compressed(temp_path, **arrays)
        metadata["index_sha256"] = sha256(temp_path)
        metadata["index_bytes"] = temp_path.stat().st_size
        sidecar = output.with_suffix(".provenance.json")
        with tempfile.NamedTemporaryFile(mode="w", dir=output.parent, suffix=".json", encoding="utf-8",
                                         newline="\n", delete=False) as temporary:
            json.dump(metadata, temporary, indent=2, ensure_ascii=False)
            temporary.write("\n")
            temp_metadata = Path(temporary.name)
        os.replace(temp_metadata, sidecar)
        os.replace(temp_path, output)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return metadata


def download(url, path):
    """Developer-only public export download; never called by server code."""
    # Optional Windows system roots, without disabling TLS certificate checks.
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass
    request = urllib.request.Request(url, headers={"User-Agent": "BeamNGEarth-OSM-Index-DeveloperBuild/1.0"})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(request, timeout=120) as response, path.open("wb") as target:
        count = 0
        for block in iter(lambda: response.read(1024 * 1024), b""):
            target.write(block)
            count += len(block)
        expected = response.headers.get("Content-Length")
        if expected is not None and count != int(expected):
            raise ValueError("incomplete developer export download")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path)
    parser.add_argument("--download", action="store_true", help="download current complete GMWID layer and catalog (developer only)")
    parser.add_argument("--scratch-dir", type=Path, default=ROOT / ".tmp-python" / "wind-turbines-source")
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source-url")
    parser.add_argument("--retrieved-at")
    parser.add_argument("--min-count", type=int, default=100000)
    args = parser.parse_args()
    if args.download:
        args.source = args.source or args.scratch_dir / "power_generator.parquet"
        args.catalog = args.catalog or args.scratch_dir / "collection.json"
        download(SOURCE_URL, args.source)
        download(CATALOG_URL, args.catalog)
    if args.source is None:
        parser.error("supply an OSM export or --download")
    if args.min_count <= 0:
        parser.error("--min-count must be positive")
    metadata = build(args.source, args.output, args.catalog, args.source_url, args.retrieved_at, args.min_count)
    print(json.dumps({"output": str(args.output), "counts": metadata["counts"],
                      "bytes": metadata["index_bytes"], "sha256": metadata["index_sha256"]}, indent=2))


if __name__ == "__main__":
    main()
