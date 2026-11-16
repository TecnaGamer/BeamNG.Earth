"""Developer-only: the compact place snapshot the direction signs name their destinations from
(server/dirsigns.py; server/data/places_<release>.json.gz).

Reads Overture's divisions/division (one Parquet file, localities: city / town / village with their population and
names) through DuckDB over HTTPS, keeps the places a road sign would name, and writes them sorted, so the file is the
same bytes every run. Players never run this tool: they use the bundled snapshot.

    python tools/build_places.py --output server/data/places_<release>.json.gz [--countries UA,CZ,PL,DE]

A place is kept when it is a city or a town, or a village of at least MIN_VILLAGE_POPULATION people. The English name
is kept only where it differs from the local one (Overture has `names.common.en` where an exonym or a transliteration
exists), so a sign writes its second line only when there is something to say.
"""
import argparse
import gzip
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import overture  # noqa: E402
RELEASE = overture.RELEASE    # (the pinned release: the snapshot is rebuilt with every bump)
STAC_ITEM = f"https://stac.overturemaps.org/{RELEASE}/divisions/division/00000/00000.json"
COUNTRIES = ("UA", "CZ", "PL", "DE")
MIN_VILLAGE_POPULATION = 3000


def parquet_url():
    with urllib.request.urlopen(STAC_ITEM, timeout=60) as r:
        item = json.load(r)
    return item["assets"]["aws"]["href"]


def fetch(countries, parquet=None):
    import duckdb
    con = duckdb.connect()
    if parquet:
        src = "read_parquet('" + str(Path(parquet).resolve()).replace("'", "''") + "')"
    else:
        con.execute("INSTALL httpfs; LOAD httpfs;")
        src = f"read_parquet('{parquet_url()}')"
    marks = ",".join("?" * len(countries))
    rows = con.execute(f"""
        SELECT id, country, class, names."primary", names.common['en'], population,
               (bbox.xmin + bbox.xmax) / 2, (bbox.ymin + bbox.ymax) / 2
        FROM {src}
        WHERE country IN ({marks}) AND subtype = 'locality' AND names."primary" IS NOT NULL
          AND (class IN ('city', 'town') OR (class = 'village' AND population >= {MIN_VILLAGE_POPULATION}))
        """, list(countries)).fetchall()
    places = []
    for pid, cc, cls, name, en, pop, lon, lat in rows:
        name = name.strip()
        if not name:
            continue
        en = (en or "").strip()
        places.append({"id": pid, "cc": cc, "class": cls, "pop": int(pop or 0), "name": name,
                       "en": en if en and en != name else "", "lat": round(float(lat), 5), "lon": round(float(lon), 5)})
    places.sort(key=lambda p: (p["cc"], p["id"]))
    return places


def build(output, countries, parquet=None):
    places = fetch(countries, parquet)
    counts = {}
    for p in places:
        counts[p["cc"]] = counts.get(p["cc"], 0) + 1
    doc = {"format": 1, "source": "Overture Maps divisions/division", "release": RELEASE,
           "license": "ODbL-1.0 (OpenStreetMap contributors), see places.NOTICE.md",
           "selection": f"locality city/town, village with population >= {MIN_VILLAGE_POPULATION}",
           "counts": dict(sorted(counts.items())), "places": places}
    raw = gzip.compress(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)
    print(len(places), "places;", len(raw), "bytes; SHA256", hashlib.sha256(raw).hexdigest())
    print(doc["counts"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--countries", default=",".join(COUNTRIES))
    ap.add_argument("--parquet", type=Path, help="a downloaded copy of the divisions file (skips the range reads)")
    a = ap.parse_args()
    build(a.output, tuple(c.strip().upper() for c in a.countries.split(",") if c.strip()), a.parquet)
