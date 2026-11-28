"""Import measured Polish signs. Source builds only: shapely >= 2; osmium for a .pbf input."""
import argparse
from collections import Counter
import getpass
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "tools/sign_art/pl/sources.json"
STORE = ROOT / "server/data/pl_signs"
COMPASS = "N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW".split()
import sys
sys.path.insert(0, str(ROOT / "server"))
from plmapillary import mapillary_code, read_pages


def bearing(value):
    compass = str(value).strip().upper()
    if compass in COMPASS:
        return COMPASS.index(compass) * 22.5
    try:
        n = float(value)
    except (ValueError, TypeError):
        return None
    return n % 360 if math.isfinite(n) and -360 <= n <= 360 else None


def osm_code(tags):
    value = str(tags.get("traffic_sign", "")).strip().removeprefix("PL:")
    # A qualifier/second face needs a different physical assembly. Never strip it.
    if value == "maxspeed":
        return "B-33-" + str(tags.get("maxspeed", ""))
    if value in ("stop", "give_way"):
        return {"stop": "B-20", "give_way": "A-7"}[value]
    speed = re.fullmatch(r"B-33\[(\d+)\]", value)
    if speed:
        return "B-33-" + speed[1]
    limit = re.fullmatch(r"(B-16|B-18|B-5)\[(\d+(?:[.,]\d+)?)\s*([mt]?)\]", value)
    if limit:
        unit = "m" if limit[1] == "B-16" else "t"
        if limit[3] and limit[3] != unit:
            return None
        return f"{limit[1]}-{limit[2].replace(',', '.')}{unit}"
    return value


def osm_points(path):
    if path.suffix.lower() == ".pbf":
        import osmium
        processor = osmium.FileProcessor(str(path), osmium.osm.NODE).with_filter(osmium.filter.KeyFilter("traffic_sign"))
        for n in processor:
            yield n.id, n.location.lon, n.location.lat, dict(n.tags)
    else:
        for n in json.loads(path.read_text(encoding="utf-8-sig"))["elements"]:
            if n.get("type") == "node":
                yield n["id"], n["lon"], n["lat"], n.get("tags", {})


def graph_features(bbox, token):
    """Follow cursors on the fixed Mapillary origin. The access token stays in the header."""
    west, south, east, north = bbox
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("Expected west south east north")
    def read(url):
        with urlopen(Request(url, headers={"Authorization": "OAuth " + token}), timeout=40) as response:
            return json.load(response)
    for f in read_pages(bbox, read):
        yield {"geometry": f.get("geometry"), "properties": f}


def feature_rows(features, source):
    for f in features:
        g, p = f.get("geometry") or {}, f.get("properties") or {}
        if g.get("type") != "Point" or len(g.get("coordinates", [])) < 2:
            continue
        if source == "mapillary":
            code = mapillary_code(p.get("value") or p.get("object_value"))
            ident = p.get("id", f.get("id"))
        else:
            base = p.get("ZNAK")
            unit = {"B-5": "t", "B-16": "m", "B-18": "t"}.get(base)
            raw = str(p.get("WARTOSC", "")).strip().replace(",", ".")
            value = re.fullmatch(r"(\d+(?:\.\d+)?)\s*" + (unit or "") + "?", raw) if unit else None
            if not value or not p.get("DATA_WYN"):
                continue
            number = float(value[1])
            # GIS float32 artefacts may be rounded to millimetres, never to the nearest sign value.
            code = f"{base}-{round(number, 3):g}{unit}"
            ident = str(base) + "/" + str(p["OBJECTID"])
        if ident is not None:
            yield str(ident), *g["coordinates"][:2], code, None


def write_layer(source, rows, boundary, supported, out):
    from shapely import contains_xy, prepare
    prepare(boundary)
    kept, skipped = {}, Counter()
    for ident, lon, lat, code, angle in rows:
        if code not in supported:
            skipped["unsupported"] += 1
            continue
        if not (isinstance(lon, (int, float)) and isinstance(lat, (int, float))
                and math.isfinite(lon) and math.isfinite(lat) and contains_xy(boundary, lon, lat)):
            skipped["outside"] += 1
            continue
        kept[str(ident)] = [str(ident), round(lon, 7), round(lat, 7), code, angle]
    out.mkdir(exist_ok=True, parents=True)
    dest = out / (source + ".json")
    # Build each input source atomically; other source files survive a refresh.
    import tempfile
    with tempfile.NamedTemporaryFile("w", encoding="utf8", dir=out, delete=False) as temp:
        temp.write("[\n")
        temp.write(",\n".join(json.dumps(kept[k], separators=(",", ":")) for k in sorted(kept)))
        temp.write("\n]\n")
    os.replace(temp.name, dest)
    print(f"{source}: {len(kept)} records; skipped {dict(skipped)}")


def write_boundary(boundary, out):
    """Keep every PRG edge, including islands and holes, for final shoulder placements."""
    import tempfile
    import numpy as np
    from shapely import get_parts
    edges = []
    for polygon in get_parts(boundary):
        for ring in (polygon.exterior, *polygon.interiors):
            points = np.asarray(ring.coords)[:, :2]
            edges.append(np.column_stack((points[:-1], points[1:])))
    out.mkdir(exist_ok=True, parents=True)
    with tempfile.NamedTemporaryFile("wb", dir=out, delete=False) as f:
        np.savez_compressed(f, edges=np.concatenate(edges))
    os.replace(f.name, out / "prg.npz")


def main():
    from shapely.geometry import shape
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--boundary", type=Path, required=True, help="Geoportal PRG GranicaPanstwa, WGS84 GeoJSON")
    ap.add_argument("--osm", type=Path, help="Poland .osm.pbf or Overpass JSON node export")
    ap.add_argument("--wroclaw", type=Path, nargs="+", help="SIP B-5, B-16 and/or B-18 point GeoJSON, WGS84")
    mp = ap.add_mutually_exclusive_group()
    mp.add_argument("--mapillary", type=Path, nargs="+", help="Mapillary traffic sign GeoJSON exports")
    mp.add_argument("--bbox", type=float, nargs=4, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    ap.add_argument("--out", type=Path, default=STORE)
    ap.add_argument("--build-boundary", action="store_true", help="Rebuild the game's exact PRG boundary")
    args = ap.parse_args()
    if not any((args.osm, args.wroclaw, args.mapillary, args.bbox, args.build_boundary)):
        ap.error("Choose a source")
    features = json.loads(args.boundary.read_text(encoding="utf-8-sig"))["features"]
    boundary = shape(features[0]["geometry"])
    if len(features) != 1 or boundary.geom_type not in {"Polygon", "MultiPolygon"}:
        ap.error("Expected the Polish national border from GranicaPanstwa")
    if args.build_boundary:
        write_boundary(boundary, args.out)
    supported = set(json.loads(ART.read_text(encoding="utf8")))
    confirmed = supported | set(json.loads((STORE / "commons.json").read_bytes()))
    if args.osm:
        rows = ((i, lon, lat, osm_code(t), bearing(t.get("direction"))) for i, lon, lat, t in osm_points(args.osm))
        write_layer("osm", rows, boundary, supported, args.out)
    for source in ("wroclaw", "mapillary"):
        files = getattr(args, source)
        if files:
            fs = (f for path in files for f in json.loads(path.read_text(encoding="utf-8-sig"))["features"])
            write_layer(source, feature_rows(fs, source), boundary, confirmed if source == "mapillary" else supported, args.out)
    if args.bbox:
        token = os.environ.get("MAPILLARY_ACCESS_TOKEN") or getpass.getpass("Mapillary client access token: ")
        write_layer("mapillary", feature_rows(graph_features(args.bbox, token), "mapillary"), boundary, confirmed, args.out)


if __name__ == "__main__":
    main()
