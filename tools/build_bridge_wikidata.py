"""Build server/bridges_wd.npz: bridges with a structural type from Wikidata (CC0), worldwide.

  python3 tools/build_bridge_wikidata.py

One SPARQL query per type class (instance of it or a subclass, with coordinates): suspension, cable-stayed, arch
(tied arch: through), truss, cantilever, beam / girder (plain decks: a site with no structure), bascule, lift,
swing. Per item: lat, lon, the type as bridgetypes' code (NBI item 43B where it has one; ARCH_ANY for an arch
whose deck position is unknown), its longest span (P2787) and length (P2043) in metres (0 when not given). An item
in two classes keeps the more specific: a suspension bridge also listed as a beam bridge is a suspension bridge.
Bridges with a demolition date (P576) or demolished (P5817) are left out (Genoa's Ponte Morandi, cable-stayed,
fell in 2018: its stays stood over the girders of the San Giorgio bridge that replaced it, 2026-10-18).
Wikidata corrects the map's tags where they are wrong (OSM calls Tokyo's Rainbow Bridge, a suspension bridge,
cable-stayed; 2026-10-18) and covers the world beyond the US inventory."""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import bridgetypes as bt  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "bridges_wd.npz")
ENDPOINT = "https://query.wikidata.org/sparql"
UA = "BeamNG.Earth/1.4 (bridge types build tool; https://github.com/TecnaGamer)"
# class -> code, in order of precedence (earlier wins for an item in two classes)
CLASSES = [
    ("Q12570", bt.NBI_SUSPENSION),      # suspension bridge
    ("Q158555", bt.NBI_STAYED),         # cable-stayed bridge
    ("Q818882", bt.NBI_THROUGH_ARCH),   # tied-arch bridge
    ("Q158438", bt.ARCH_ANY),           # arch bridge
    ("Q1429218", bt.NBI_THROUGH_TRUSS),  # cantilever bridge
    ("Q158218", bt.NBI_THROUGH_TRUSS),  # truss bridge
    ("Q911663", bt.NBI_BASCULE),        # bascule bridge
    ("Q1898246", bt.NBI_LIFT),          # vertical-lift bridge
    ("Q950431", bt.NBI_SWING),          # swing bridge
    ("Q1055465", bt.PLAIN),             # girder bridge
    ("Q158626", bt.PLAIN),              # beam bridge
]
QUERY = """SELECT ?item ?c ?span ?len ?col WHERE {{
  ?item wdt:P31/wdt:P279* wd:{cls} .
  ?item wdt:P625 ?c .
  FILTER NOT EXISTS {{ ?item wdt:P576 ?gone . }}
  FILTER NOT EXISTS {{ ?item wdt:P5817 wd:Q56556915 . }}
  OPTIONAL {{ ?item p:P2787/psn:P2787/wikibase:quantityAmount ?span . }}
  OPTIONAL {{ ?item p:P2043/psn:P2043/wikibase:quantityAmount ?len . }}
  OPTIONAL {{ ?item wdt:P462 ?ci . ?ci rdfs:label ?col . FILTER(lang(?col) = "en") }}
  OPTIONAL {{ ?item wdt:P186 ?mat . }}
}}"""
# A cantilever bridge is a steel truss only where Wikidata says it is steel or iron (P186): Norway's, most of Europe's
# and Asia's are concrete box girders built out from their piers (GitHub #1066: Andoybrua, concrete, drawn as a 750 m
# through truss); the US ones come from the NBI first. CANTILEVER and STEEL_MATS: the class and the materials.
CANTILEVER = "Q1429218"
STEEL_MATS = {"Q11427", "Q677", "Q1048771", "Q2493636", "Q11426"}     # steel, iron, wrought iron, cast iron, ...
PAINT_NAMES = list(bt.PAINTS)       # the paint column: an index into these, 255 without a colour (P462)


def query(cls):
    data = urllib.parse.urlencode({"query": QUERY.format(cls=cls)}).encode()
    req = urllib.request.Request(ENDPOINT, data=data, headers={"Accept": "application/sparql-results+json",
                                                               "User-Agent": UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)["results"]["bindings"]
        except Exception as e:      # (the endpoint times out or throttles now and then)
            print(f"  {cls}: {e}; again in {10 * (attempt + 1)} s")
            time.sleep(10 * (attempt + 1))
    raise SystemExit(f"{cls}: no answer")


def point(wkt):
    """'Point(lon lat)' -> (lat, lon), None for another globe or form."""
    if not wkt.startswith("Point("):
        return None
    lon, lat = map(float, wkt[6:-1].split())
    return lat, lon


def main():
    items = {}
    for rank, (cls, code0) in enumerate(CLASSES):
        rows = query(cls)
        steel = set()
        if cls == CANTILEVER:
            steel = {b["item"]["value"].rsplit("/", 1)[-1] for b in rows
                     if "mat" in b and b["mat"]["value"].rsplit("/", 1)[-1] in STEEL_MATS}
        n = 0
        for b in rows:
            code = code0
            if cls == CANTILEVER and b["item"]["value"].rsplit("/", 1)[-1] not in steel:
                code = bt.PLAIN
            q = b["item"]["value"].rsplit("/", 1)[-1]
            p = point(b["c"]["value"])
            if p is None:
                continue
            span = float(b["span"]["value"]) if "span" in b else 0.0
            length = float(b["len"]["value"]) if "len" in b else 0.0
            pt = bt.paint(b["col"]["value"]) if "col" in b else None
            pi = PAINT_NAMES.index(pt) if pt else 255
            old = items.get(q)
            if old is None or rank < old[2]:
                items[q] = (p, code, rank, max(span, old[3] if old else 0.0), max(length, old[4] if old else 0.0),
                            pi if pi != 255 else (old[5] if old else 255))
                n += 1
            else:
                items[q] = old[:3] + (max(span, old[3]), max(length, old[4]), old[5] if old[5] != 255 else pi)
        print(f"{cls}: {len(rows)} rows, {n} kept")
        time.sleep(2)
    rows = sorted((p[0], p[1], code, span, length, pi) for p, code, _, span, length, pi in items.values())
    a = np.array(rows, np.float64)
    np.savez_compressed(OUT, lat=a[:, 0].astype(np.float32), lon=a[:, 1].astype(np.float32),
                        type=a[:, 2].astype(np.uint8), span=a[:, 3].astype(np.float32),
                        length=a[:, 4].astype(np.float32), paint=a[:, 5].astype(np.uint8),
                        paints=np.array(PAINT_NAMES))
    print(len(a), "bridges ->", os.path.normpath(OUT), os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
