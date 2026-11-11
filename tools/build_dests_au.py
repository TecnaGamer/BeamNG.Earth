"""The Australian destination shard the
direction boards read the posted text of a motorway / trunk / primary link from (server/dirsigns.py DESTS_FILE;
server/data/dests_au_<date>.json.gz).

Why: the boards name the places ahead of a junction by a population-ranked guess (the places snapshot). In OpenStreetMap
the link roads of south-east Queensland carry what the real board says (`destination`, `destination:ref`,
`destination:street`, about a third of them), so a board at an exit says "Gold Coast / M1" instead of the biggest place in
a cone.

What it reads: ONE Overpass API request over the Queensland box (the link ways of motorway / trunk / primary that carry a
destination tag or a name "Exit 95 to Tugun", with their geometry; the motorway_junction nodes that carry exit_to or
destination). Players never run this tool; the response is cached (--cache) so a rerun is the same bytes.

What it writes: a gzip JSON, sorted, the same bytes for the same response:

    {"format": 1, "grid_deg": 0.01, "keys": [...], "cells": {"<floor(lat/g)>,<floor(lon/g)>": [row, ...], ...}, ...}
    row = [way id, lat, lon, bearing 0, bearing 1, [destinations], [route refs], [street names]]

lat / lon is the link's FIRST node in the direction of travel (where traffic leaves the junction; for oneway=-1 the last
node), the cell is the one of that point. bearing 0 is the compass bearing (whole degrees, 0 north, clockwise) over the first
30 m, bearing 1 the one of the chord to the link's far end: the server matches the kernel's arms (junction point and
bearing) to these. The lists are the tags split on ';' (destination, destination:forward; destination:ref, :ref:forward,
:ref:to; destination:street, :street:forward, :street:to; for oneway=-1 the :backward ones), at most MAX_ITEMS each.
A link with no tag but the name "Exit 95 to Tugun" (or a motorway_junction node's exit_to / destination at its first
node) gets that text as its destinations.

    python tools/build_dests_au.py --output server/data/dests_au_<date>.json.gz [--cache overpass.json] [--date YYYY-MM-DD]

Source and licence: OpenStreetMap, ODbL 1.0, (c) OpenStreetMap contributors; the shard is a derived database of it and is
itself available under the ODbL (CREDITS.md).
"""
import argparse
import datetime
import gzip
import hashlib
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OVERPASS = "https://overpass-api.de/api/interpreter"
USER_AGENT = "BeamNG.Earth/0.1 (+https://github.com/TecnaGamer/BeamNG.Earth)"
BBOX = (-29.2, 138.0, -9.0, 154.0)          # south, west, north, east: Queensland and its coast
GRID_DEG = 0.01                             # the shard's cell (about 1.1 km): a lookup reads the cells round a junction
HEAD_M = 30.0                               # the first bearing is over this much of the link
MAX_ITEMS = 6                               # destinations / refs / streets kept per link (a board shows 3)
MAX_LEN = 48                                # characters of one item
KEYS = ["way", "lat", "lon", "bearing0", "bearing1", "destination", "ref", "street"]
LINK = "^(motorway|trunk|primary)_link$"
QUERY = f"""[out:json][timeout:600];
(
  way["highway"~"{LINK}"][~"^destination(:ref|:street)?(:to|:forward|:backward)?$"~"."]({{bbox}});
  way["highway"~"{LINK}"]["name"~"^Exit [0-9A-Z]+ to "]({{bbox}});
  node["highway"="motorway_junction"][~"^(exit_to|destination)$"~"."]({{bbox}});
);
out tags geom;"""
EXIT_NAME = re.compile(r"^Exit [0-9A-Z]+ to (.+)$")


def fetch(cache=None, tries=3):
    """The Overpass response (a dict): from the cache file if it exists, else one request, retried politely."""
    if cache and Path(cache).exists():
        return json.loads(Path(cache).read_text(encoding="utf-8"))
    bbox = ",".join(str(v) for v in BBOX)
    body = urllib.parse.urlencode({"data": QUERY.replace("{bbox}", bbox)}).encode()
    req = urllib.request.Request(OVERPASS, data=body, headers={"User-Agent": USER_AGENT})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                doc = json.load(r)
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print("overpass:", e, file=sys.stderr)
            if attempt == tries - 1:
                raise
            time.sleep(60 * (attempt + 1))
    if cache:
        Path(cache).write_text(json.dumps(doc), encoding="utf-8")
    return doc


def items(text):
    """The cleaned items of a tag value: split on ';', blanks and repeats (any case) dropped, order kept."""
    out, seen = [], set()
    for part in str(text or "").split(";"):
        part = " ".join("".join(c if c.isprintable() else " " for c in part).split())[:MAX_LEN]
        if part and part.casefold() not in seen:
            seen.add(part.casefold())
            out.append(part)
    return out[:MAX_ITEMS]


def tag_items(tags, bases, side):
    """The items of the first of the keys that has any: for each base, the key with the side (":forward" ...), the plain key,
    then the ':to' one (AU has destination:ref:to and destination:street:to)."""
    for base in bases:
        for key in (base + side, base, base + ":to"):
            got = items(tags.get(key))
            if got:
                return got
    return []


def compass(lat0, lon0, lat1, lon1):
    """Whole degrees, 0 north, clockwise, from point 0 to point 1."""
    dx = math.radians(lon1 - lon0) * math.cos(math.radians((lat0 + lat1) / 2))
    dy = math.radians(lat1 - lat0)
    return int(round(math.degrees(math.atan2(dx, dy)))) % 360


def metres(lat0, lon0, lat1, lon1):
    dx = math.radians(lon1 - lon0) * math.cos(math.radians((lat0 + lat1) / 2))
    return 6371000.0 * math.hypot(dx, math.radians(lat1 - lat0))


def cell(lat, lon):
    return f"{math.floor(lat / GRID_DEG)},{math.floor(lon / GRID_DEG)}"


def build_rows(doc):
    """({cell: [row]}, stats) of an Overpass response (ways with geometry, nodes)."""
    nodes = {}
    for e in doc.get("elements", ()):
        if e["type"] == "node":
            tags = e.get("tags", {})
            text = tags.get("exit_to") or tags.get("destination")
            if text and items(text):
                nodes[(round(e["lat"], 6), round(e["lon"], 6))] = items(text)
    cells, stats = {}, {"ways": 0, "kept": 0, "from_name": 0, "from_node": 0, "nodes": len(nodes)}
    for e in sorted((e for e in doc.get("elements", ()) if e["type"] == "way"), key=lambda e: e["id"]):
        stats["ways"] += 1
        geom = [(g["lat"], g["lon"]) for g in e.get("geometry", ()) if g]
        if len(geom) < 2:
            continue
        tags = e.get("tags", {})
        backward = tags.get("oneway") in ("-1", "reverse")
        if backward:
            geom.reverse()
        side = ":backward" if backward else ":forward"
        dests = tag_items(tags, ("destination",), side)
        refs = tag_items(tags, ("destination:ref",), side)
        streets = tag_items(tags, ("destination:street",), side)
        lat, lon = round(geom[0][0], 6), round(geom[0][1], 6)
        if not dests:
            m = EXIT_NAME.match(tags.get("name", ""))
            named = items(m.group(1)) if m else []
            if named:
                dests = named
                stats["from_name"] += 1
            elif not streets and (lat, lon) in nodes:
                dests = nodes[lat, lon]
                stats["from_node"] += 1
        if not (dests or refs or streets):
            continue
        head = next((g for g in geom[1:] if metres(lat, lon, *g) >= HEAD_M), geom[-1])
        row = [e["id"], lat, lon, compass(lat, lon, *head), compass(lat, lon, *geom[-1]), dests, refs, streets]
        cells.setdefault(cell(lat, lon), []).append(row)
        stats["kept"] += 1
    return {k: cells[k] for k in sorted(cells)}, stats


def build(output, cache=None, date=None):
    resp = fetch(cache)
    cells, stats = build_rows(resp)
    date = date or datetime.date.today().isoformat()
    doc = {"format": 1, "source": "OpenStreetMap via the Overpass API (link ways of motorway / trunk / primary with "
                                  "destination tags; motorway_junction exit_to)",
           "osm_base": resp.get("osm3s", {}).get("timestamp_osm_base", ""), "date": date,
           "license": "ODbL-1.0, (c) OpenStreetMap contributors (https://www.openstreetmap.org/copyright)",
           "bbox": list(BBOX), "grid_deg": GRID_DEG, "keys": KEYS, "counts": {"rows": stats["kept"]}, "cells": cells}
    raw = gzip.compress(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)
    print(stats["kept"], "rows in", len(cells), "cells;", len(raw), "bytes; SHA256", hashlib.sha256(raw).hexdigest())
    print(stats)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--cache", type=Path, help="read the Overpass response from here if it exists, else fetch and write")
    ap.add_argument("--date", help="the date in the shard (default today)")
    a = ap.parse_args()
    build(a.output, a.cache, a.date)
