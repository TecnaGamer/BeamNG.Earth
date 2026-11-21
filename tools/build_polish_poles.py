"""Build the Polish power-pole snapshot from Geoportal BDOT10k Parquet and PRG WFS GML.

Usage: python tools/build_polish_poles.py OT_BUWT_P.parquet border.gml [mapillary.geojson ...]
Build dependencies: pyarrow, pyproj, shapely, numpy. See SOURCES.md for source URLs.
"""
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import numpy as np
import pyarrow.parquet as pq
from pyproj import Transformer
import shapely
from shapely.geometry import Polygon, box

OUT = Path(__file__).resolve().parents[1] / "server/data/polish_poles.npz"


def build(poles, border, *mapillary_exports):
    t = pq.read_table(poles, columns=["GEOM"], filters=[
        ("RODZAJ", "=", "słup energetyczny"), ("KATEGORIAISTNIENIA", "=", "eksploatowany")])
    crs = json.loads(t.schema.metadata[b"geo"])["columns"]["GEOM"]["crs"]
    project = Transformer.from_crs(crs, 4326, always_xy=True)
    points = shapely.from_wkb(t["GEOM"].to_pylist())
    assert np.all(shapely.get_type_id(points) == 0)
    lon, lat = project.transform(shapely.get_x(points), shapely.get_y(points))
    # Latitude first for bounded binary searches at runtime.
    coords = np.unique(np.rint(np.column_stack((lat, lon)) * 1e7).astype("<i4"), axis=0)
    gml = "{http://www.opengis.net/gml}"
    polygons = ET.parse(border).findall(".//" + gml + "Polygon")
    assert len(polygons) == 1 and polygons[0].get("srsName", "").endswith("2180")
    rings = polygons[0].findall(".//" + gml + "LinearRing/" + gml + "coordinates")
    project = Transformer.from_crs(2180, 4326, always_xy=True)
    outlines = []
    for ring in rings:
        xy = np.fromstring(ring.text.replace(",", " "), sep=" ").reshape(-1, 2)
        outlines.append(np.column_stack(project.transform(*xy.T)))
    country = Polygon(outlines[0], outlines[1:])
    # A quarter-degree grid keeps individual point checks near the border inexpensive.
    cells, vertices, offsets = {}, [], [0]
    for x in range(56, 97):
        for y in range(195, 223):
            square = box(x/4, y/4, (x+1)/4, (y+1)/4)
            if country.covers(square):
                cells[f"{x},{y}"] = True
            elif country.intersects(square):
                start = len(offsets)-1
                for part in shapely.get_parts(country.intersection(square)):
                    if part.geom_type == "Polygon":
                        for ring in (part.exterior, *part.interiors):
                            vertices.extend(ring.coords)
                            offsets.append(len(vertices))
                cells[f"{x},{y}"] = [start, len(offsets)-1]
    observed = []
    for filename in mapillary_exports:
        for feature in json.loads(Path(filename).read_text(encoding="utf-8"))["features"]:
            props = feature.get("properties") or {}
            geom = feature.get("geometry") or {}
            if geom.get("type") != "Point" or props.get("object_value", props.get("value")) != "object--support--utility-pole":
                continue
            lon, lat = geom["coordinates"][:2]
            if shapely.contains_xy(country, lon, lat):
                observed.append((lat, lon))
    observed = np.unique(np.rint(np.asarray(observed).reshape(-1, 2)*1e7).astype("<i4"), axis=0)
    np.savez_compressed(OUT, poles=coords, mapillary=observed, cells=json.dumps(cells), offsets=offsets,
                        border=np.rint(np.asarray(vertices)*1e7).astype("<i4"))


if __name__ == "__main__":
    build(*sys.argv[1:])

