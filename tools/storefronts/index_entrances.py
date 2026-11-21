"""Rebuild the offline OSM entrance index used by streamed branded standalone stores."""
import argparse
import csv
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import NamedTemporaryFile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from store_match import NAMES

PREFIX = """PREFIX k: <https://www.openstreetmap.org/wiki/Key:>
PREFIX w: <https://www.openstreetmap.org/way/>
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
"""

def fetch(labels, paths):
    public = 'FILTER NOT EXISTS { ?node k:access ?deny FILTER(?deny NOT IN ("customers","yes","public","permissive")) }'
    if paths:
        evidence = """VALUES ?type { "footway" "pedestrian" "path" }
        ?path k:highway ?type; w:member/w:member_id ?node; geo:hasGeometry/geo:asWKT ?line .
        FILTER NOT EXISTS { ?path k:access ?deny FILTER(?deny NOT IN ("customers","yes","public","permissive")) }
        FILTER NOT EXISTS { ?path k:foot "no" }
        FILTER NOT EXISTS { ?node k:entrance ?tag }
        FILTER NOT EXISTS { ?node k:amenity "loading_dock" }
        FILTER NOT EXISTS { ?path k:indoor "yes" }
        FILTER NOT EXISTS { ?path k:level ?lv FILTER(?lv != "0") }"""
    else:
        evidence = 'VALUES ?door { "yes" "main" } ?node k:entrance ?door . FILTER NOT EXISTS { ?node k:amenity "loading_dock" }'
    label_values = " ".join(json.dumps(label, ensure_ascii=False) for label in labels)
    fields = "?way ?point ?line" if paths else "?way ?point"
    query = PREFIX + f"SELECT DISTINCT {fields} WHERE {{ VALUES ?label {{ {label_values} }} " + """
      ?way k:brand ?label; k:building ?building; w:member/w:member_id ?node .
      ?node geo:hasGeometry/geo:asWKT ?point .
      FILTER NOT EXISTS { ?node k:level ?lv FILTER(?lv != "0") }
      FILTER NOT EXISTS { ?node k:foot "no" }
    """ + public + evidence + '}'
    params = urllib.parse.urlencode({"query": query, "action": "sparql_json_export"})
    with urllib.request.urlopen("https://qlever.dev/api/osm-planet?" + params, timeout=180) as response:
        rows = json.load(response)["results"]["bindings"]
    for row in rows:
        point=re.fullmatch(r'POINT\(([-.\d]+) ([-.\d]+)\)',row['point']['value'])
        if not point:
            continue
        xy = tuple(map(float, point.groups()))
        if paths:
            line=row['line']['value']
            if not line.startswith('LINESTRING('):
                continue
            vertices = line.removeprefix("LINESTRING(")[:-1].split(",")
            ends = [tuple(map(float, vertex.split())) for vertex in (vertices[0], vertices[-1])]
            # The node WKT and way WKT can use different decimal precision.
            if not any(max(abs(a-b) for a, b in zip(xy, end)) <= 1.1e-6 for end in ends):
                continue
        yield (int(row['way']['value'].rsplit('/',1)[1]),*xy,int(not paths))

def main(target):
    labels = sorted(set(NAMES.values()))
    rows = set()
    for start in range(0, len(labels), 20):
        batch = labels[start:start+20]
        for paths in (False, True):
            rows.update(fetch(batch, paths))
        print(f"{start+len(batch)}/{len(labels)} brands, {len(rows)} records", flush=True)
    if not rows:
        raise ValueError('Empty snapshot')
    with NamedTemporaryFile(mode='w',dir=target.parent,delete=False,newline='',encoding='utf8') as f:
        writer=csv.writer(f,lineterminator='\n')
        writer.writerow(('way','lon','lat','explicit'))
        writer.writerows(sorted(rows))
    Path(f.name).replace(target)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target',type=Path)
    main(parser.parse_args().target)
