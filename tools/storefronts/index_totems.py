"""Build the streamed sign index from the global OSM snapshot served by QLever.

Usage: python tools/storefronts/index_totems.py server/data/storefront_totems.csv
"""
import argparse
import csv
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from store_match import chain, sign_bearing


QUERY = """PREFIX osmkey: <https://www.openstreetmap.org/wiki/Key:>
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
SELECT ?node ?point ?brand ?name ?height ?dir WHERE {
  ?node osmkey:advertising "totem"; geo:hasGeometry/geo:asWKT ?point .
  OPTIONAL { ?node osmkey:brand ?brand } OPTIONAL { ?node osmkey:name ?name }
  FILTER(BOUND(?brand) || BOUND(?name))
  OPTIONAL { ?node osmkey:height ?height } OPTIONAL { ?node osmkey:direction ?dir }
}"""


def write_index(data, target):
    rows = {}
    for binding in data["results"]["bindings"]:
        tags = {key: field["value"] for key, field in binding.items()}
        node = re.fullmatch(r"https://www.openstreetmap.org/node/(\d+)", tags["node"])
        point = re.fullmatch(r"POINT\(([-.\d]+) ([-.\d]+)\)", tags["point"])
        if not node or not point:
            continue
        lon, lat = map(float, point.groups())
        label = chain(tags.get("brand")) or chain(tags.get("name"))
        if not label:
            continue
        try:
            height = float(tags.get("height", 6))
        except ValueError:
            continue
        if 2 <= height <= 20:
            direction = sign_bearing(tags.get("dir"))
            rows[node[1]] = (lat, lon, node[1], label, height, direction)
    with target.open("w", newline="", encoding="utf8") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(("lat", "lon", "node", "brand", "height", "direction"))
        writer.writerows(sorted(rows.values()))
    return len(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    url = "https://qlever.dev/api/osm-planet?" + urllib.parse.urlencode({"query": QUERY, "action": "sparql_json_export"})
    with urllib.request.urlopen(url, timeout=120) as response:
        print(write_index(json.load(response), args.target), "mapped signs")
