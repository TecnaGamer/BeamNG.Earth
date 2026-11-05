#!/usr/bin/env python3
"""server/bg_yellow_paving.json: the OpenStreetMap ways of Bulgaria's yellow paving (Sofia's жълти павета, the glazed
bricks of Tsar Osvoboditel and the streets round it), shipped with the server (server/bgpaving.py).

    python3 tools/build_bg_yellow_paving.py

Mappers tag these streets surface=paving_stones / sett with colour=yellow (or surface:colour, paving_stones:colour);
the grey granite setts of the same quarter (Totleben, Pencho Slaveykov) carry no colour. OSM US's Layercake highways
file, the server's source of road tags (osmtags.py), has no colour column, so the ways are read once from the Overpass
API here (ODbL, OpenStreetMap contributors), as the way ids the roads' src_record names. Rebuild when the map changes.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

OVERPASS = "https://overpass-api.de/api/interpreter"
BBOX = (41.2, 22.3, 44.25, 28.65)       # Bulgaria (south, west, north, east)
STONES = "^(sett|paving_stones|cobblestone|unhewn_cobblestone|bricks)(:.*)?$"
COLOUR_KEYS = ("colour", "surface:colour", "paving_stones:colour", "sett:colour")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "bg_yellow_paving.json")


def query():
    s, w, n, e = BBOX
    parts = "".join(f'way["highway"]["surface"~"{STONES}"]["{k}"="yellow"];' for k in COLOUR_KEYS)
    q = f"[out:json][timeout:160][bbox:{s},{w},{n},{e}];({parts});out tags center;"
    req = urllib.request.Request(OVERPASS, data=urllib.parse.urlencode({"data": q}).encode(),
                                 headers={"User-Agent": "BeamNG.Earth tools/build_bg_yellow_paving.py"})
    with urllib.request.urlopen(req, timeout=200) as r:
        return json.load(r)


def main():
    for attempt in range(4):        # (Overpass answers 429 / 502 / 504 when busy)
        try:
            data = query()
            break
        except OSError as e:
            if attempt == 3:
                raise
            print(f"Overpass: {e}; again in {30 * (attempt + 1)} s", file=sys.stderr)
            time.sleep(30 * (attempt + 1))
    ways = sorted({int(el["id"]) for el in data.get("elements", ()) if el.get("type") == "way"})
    if not ways:
        sys.exit("no ways: Overpass answered nothing (busy?); the old file is kept")
    out = {"source": "OpenStreetMap contributors (ODbL), Overpass API", "osm_base": data.get("osm3s", {}).get(
        "timestamp_osm_base"), "built": time.strftime("%Y-%m-%d"), "query": {"bbox": BBOX, "surface": STONES,
                                                                              "colour": COLOUR_KEYS}, "ways": ways}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
        f.write("\n")
    print(f"{len(ways)} ways -> {os.path.normpath(OUT)}")


if __name__ == "__main__":
    main()
