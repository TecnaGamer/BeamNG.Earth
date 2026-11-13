"""The Australian place snapshot the
direction boards name their destinations from (server/dirsigns.py PLACES_EXTRA; server/data/places_au_<release>.json.gz).

Why not tools/build_places.py --countries AU: that tool keeps only Overture localities of class city / town (or a village
of 3000+ people), and Overture's Australian suburbs and many towns (Southport, Beenleigh, Coolangatta, Capalaba, Robina ...)
are localities without a class (ABS suburbs); Bundaberg is not in Overture's divisions at all (only its suburbs are); and
most towns and cities (Ipswich, Caloundra, Maroochydore, Noosa Heads, Logan City ...) carry no population. This tool

  1. reads Overture divisions/division for AU (locality and county) like build_places.py does;
  2. fills the missing populations from Wikidata (Australian human settlements with P1082 population and P625
     coordinates; the latest census statement of each item; items are matched to Overture places by normalised name and
     distance, MATCH_KM), and, for a name that is an LGA in Overture (Logan City), from the county's population;
  3. keeps a locality when it is a city or a town (as before), or any other class with at least MIN_POPULATION people;
  4. adds the Wikidata settlements of at least MIN_POPULATION people that Overture has no locality of that name near
     (Bundaberg, ...), marked "src": "wikidata"; the class is "city" for ADD_CITY_POPULATION people on when no city
     is within CITY_GAP_KM (a suburb of Sydney is not a city), else "suburb" (the reader then picks town / village by size).

The output has build_places.py's format (format 1; extra optional keys "src" and "pop_src" are ignored by the reader) and is
sorted, so the file is the same bytes every run for the same inputs.

    python tools/build_places_au.py --output server/data/places_au_<release>.json.gz [--wikidata-cache wd.json]
                                    [--parquet divisions.parquet]

Sources and licences: Overture Maps divisions (CDLA-Permissive-2.0 for the division names/populations' Overture-owned
parts; the places are OpenStreetMap-derived, ODbL-1.0, (c) OpenStreetMap contributors); Wikidata (CC0 1.0): populations,
coordinates and the additional places. The Wikidata Query Service is read sequentially, one query (or one per state if the
whole-country query times out), with a descriptive User-Agent. Players never run this tool.
"""
import argparse
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_places  # noqa: E402  (parquet_url, RELEASE)

RELEASE = build_places.RELEASE
SPARQL = "https://query.wikidata.org/sparql"
USER_AGENT = "BeamNG.Earth/0.1 (+https://github.com/TecnaGamer/BeamNG.Earth)"
MIN_POPULATION = 3000           # a suburb / village / hamlet / Wikidata-only place is kept from this many people on
ADD_CITY_POPULATION = 20000     # a Wikidata-only place is a city from this many people on (and when no city is near)
CITY_GAP_KM = 40.0              # ... else a suburb: there is a city within this many km
MATCH_KM = 5.0                  # an Overture place and a Wikidata item of the same name this near are the same place
DEDUP_KM = 3.0                  # two kept places of the same name this near are one (the better class / bigger stays)
BOX = (96.0, -45.0, 169.0, -9.0)    # lon0, lat0, lon1, lat1 of Australia and its islands (not Antarctica)
STATES = {"QLD": "Q36074", "NSW": "Q3224", "VIC": "Q36687", "WA": "Q3206", "SA": "Q35715", "TAS": "Q34366",
          "NT": "Q3235", "ACT": "Q3258"}
# Wikidata settlements that are statistical or administrative areas, not a place a board names
NOT_A_PLACE = re.compile(r"\b(greater|urban|statistical|region|shire|council|local government|metropolitan|division|"
                         r"county|district|sa[1-4]|lga|municipality|city of|shire of|town of|state)\b", re.I)
CLASS_RANK = {"city": 3, "town": 2, "village": 1}

QUERY = """
SELECT ?item ?itemLabel ?coord ?pop ?when WHERE {
  %s
  ?item wdt:P31/wdt:P279* wd:Q486972 .
  ?item wdt:P625 ?coord .
  ?item p:P1082 ?st . ?st ps:P1082 ?pop .
  OPTIONAL { ?st pq:P585 ?when }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" . }
}
"""


def sparql(scope, tries=3):
    """The bindings of the settlement query for scope (a triple pattern), one request, retried politely."""
    url = SPARQL + "?" + urllib.parse.urlencode({"query": QUERY % scope, "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)["results"]["bindings"]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print("wikidata:", e, file=sys.stderr)
            if attempt == tries - 1:
                raise
            time.sleep(30 * (attempt + 1))


def fetch_wikidata(cache=None):
    """[{q, name, lon, lat, pop}] of Australian settlements: pop is the largest statement of the item's latest date."""
    if cache and Path(cache).exists():
        rows = json.loads(Path(cache).read_text(encoding="utf-8"))
    else:
        try:
            rows = sparql("?item wdt:P17 wd:Q408 .")
        except Exception:                                   # (the whole-country query timed out: state by state)
            rows = []
            for code, q in STATES.items():
                time.sleep(5)
                rows += sparql(f"?item wdt:P131* wd:{q} .")
        if cache:
            Path(cache).write_text(json.dumps(rows), encoding="utf-8")
    stats = {}
    for b in rows:
        m = re.match(r"Point\(([-\d.eE+]+) ([-\d.eE+]+)\)", b["coord"]["value"])
        if not m:
            continue
        q = b["item"]["value"].rsplit("/", 1)[1]
        it = stats.setdefault(q, {"q": q, "name": b.get("itemLabel", {}).get("value", "").strip(),
                                  "lon": float(m[1]), "lat": float(m[2]), "stats": []})
        it["stats"].append((b.get("when", {}).get("value", ""), float(b["pop"]["value"])))
    out = []
    for it in stats.values():
        newest = max(w for w, _ in it["stats"])
        pop = max(p for w, p in it["stats"] if w == newest)
        if pop > 0 and it["name"] and not re.fullmatch(r"Q\d+", it["name"]) \
                and BOX[0] <= it["lon"] <= BOX[2] and BOX[1] <= it["lat"] <= BOX[3]:
            out.append({"q": it["q"], "name": it["name"], "lon": it["lon"], "lat": it["lat"], "pop": int(pop)})
    return sorted(out, key=lambda i: i["q"])


def fetch_overture(parquet=None):
    import duckdb
    con = duckdb.connect()
    if parquet:
        src = "read_parquet('" + str(Path(parquet).resolve()).replace("'", "''") + "')"
    else:
        con.execute("INSTALL httpfs; LOAD httpfs;")
        src = f"read_parquet('{build_places.parquet_url()}')"
    return con.execute(f"""
        SELECT id, subtype, class, names."primary", names.common['en'], population,
               (bbox.xmin + bbox.xmax) / 2, (bbox.ymin + bbox.ymax) / 2
        FROM {src}
        WHERE country = 'AU' AND subtype IN ('locality', 'county') AND names."primary" IS NOT NULL
        """).fetchall()


def norm(name):
    s = name.casefold().replace("’", "'")
    s = re.sub(r"\bmt\.?\b", "mount", s)
    s = re.sub(r"\bst\.?\b", "saint", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def km(lon0, lat0, lon1, lat1):
    dx = math.radians(lon1 - lon0) * math.cos(math.radians((lat0 + lat1) / 2))
    return 6371.0 * math.hypot(dx, math.radians(lat1 - lat0))


class ByName:
    """Items by normalised name, nearest-name lookups within a radius."""

    def __init__(self, items):
        self.d = {}
        for it in items:
            self.d.setdefault(norm(it["name"]), []).append(it)

    def near(self, name, lon, lat, radius):
        return [it for it in self.d.get(norm(name), ()) if km(lon, lat, it["lon"], it["lat"]) <= radius]


def build_places_list(overture_rows, wikidata):
    wd = ByName(wikidata)
    localities, counties = [], []
    for pid, sub, cls, name, en, pop, lon, lat in overture_rows:
        name = name.strip()
        if not name:
            continue
        en = (en or "").strip()
        row = {"id": pid, "cc": "AU", "class": cls or "suburb", "pop": int(pop or 0), "name": name,
               "en": en if en and en != name else "", "lat": round(float(lat), 5), "lon": round(float(lon), 5)}
        (counties if sub == "county" else localities).append(row)
    county_by = ByName([c for c in counties if c["pop"] > 0])
    matched_wd, kept, stats = set(), [], {"wikidata_pop": 0, "county_pop": 0, "matched": 0}
    for p in localities:
        hits = wd.near(p["name"], p["lon"], p["lat"], MATCH_KM)
        matched_wd.update(h["q"] for h in hits)
        if hits:
            stats["matched"] += 1
        if p["pop"] <= 0:
            cands = [(h["pop"], "wikidata") for h in hits] + \
                    [(c["pop"], "overture-lga") for c in county_by.near(p["name"], p["lon"], p["lat"], MATCH_KM)]
            if cands:
                p["pop"], src = max(cands)
                p["pop_src"] = src
                stats["wikidata_pop" if src == "wikidata" else "county_pop"] += 1
        if p["class"] in ("city", "town") or p["pop"] >= MIN_POPULATION:
            kept.append(p)
    # the same place twice (Overture has e.g. two "Mount Isa"s a day's drive apart, and copies of one point): the better stays
    kept.sort(key=lambda p: (-CLASS_RANK.get(p["class"], 0), -p["pop"], p["id"]))
    unique, index = [], ByName([])
    for p in kept:
        if not index.near(p["name"], p["lon"], p["lat"], DEDUP_KM):
            unique.append(p)
            index.d.setdefault(norm(p["name"]), []).append(p)
    # Wikidata settlements Overture has no locality of that name near
    present = ByName(localities)
    added = 0
    for w in sorted(wikidata, key=lambda i: (-i["pop"], i["q"])):
        if w["pop"] < MIN_POPULATION or NOT_A_PLACE.search(w["name"]) or w["q"] in matched_wd:
            continue
        if present.near(w["name"], w["lon"], w["lat"], MATCH_KM) or index.near(w["name"], w["lon"], w["lat"], DEDUP_KM):
            continue
        city = w["pop"] >= ADD_CITY_POPULATION and not any(
            c["class"] == "city" and km(w["lon"], w["lat"], c["lon"], c["lat"]) <= CITY_GAP_KM for c in unique)
        p = {"id": "wd:" + w["q"], "cc": "AU", "class": "city" if city else "suburb",
             "pop": w["pop"], "name": w["name"], "en": "", "lat": round(w["lat"], 5), "lon": round(w["lon"], 5),
             "src": "wikidata"}
        unique.append(p)
        index.d.setdefault(norm(p["name"]), []).append(p)
        added += 1
    stats["added"] = added
    unique.sort(key=lambda p: (p["cc"], p["id"]))
    return unique, stats


def build(output, parquet=None, cache=None):
    places, stats = build_places_list(fetch_overture(parquet), fetch_wikidata(cache))
    doc = {"format": 1, "source": "Overture Maps divisions/division + Wikidata populations", "release": RELEASE,
           "license": "ODbL-1.0 (OpenStreetMap contributors) for the Overture places; CC0-1.0 (Wikidata) for populations and"
                      " the places with src=wikidata",
           "selection": f"AU locality city/town, any other locality with population >= {MIN_POPULATION} (Overture, else "
                        f"Wikidata matched within {MATCH_KM:g} km, else the LGA of the same name); Wikidata settlements "
                        f"with population >= {MIN_POPULATION} that Overture lacks (src=wikidata)",
           "counts": {"AU": len(places)}, "places": places}
    raw = gzip.compress(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)
    qld = sum(1 for p in places if -29.2 < p["lat"] < -10 and 138.0 < p["lon"] < 154.0)
    print(len(places), "places (~QLD box", qld, ");", len(raw), "bytes; SHA256", hashlib.sha256(raw).hexdigest())
    print(stats)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--parquet", type=Path, help="a downloaded copy of the divisions file (skips the range reads)")
    ap.add_argument("--wikidata-cache", type=Path, help="read the Wikidata rows from here if it exists, else fetch and write")
    a = ap.parse_args()
    build(a.output, a.parquet, a.wikidata_cache)
