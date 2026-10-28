"""build_fra_shards.py OUT_DIR: the US Federal Railroad Administration's Highway-Rail Crossing Inventory (Form 71,
current; public domain), its open at-grade crossings with their warning devices, as gzip JSON shards per z7 tile
(<OUT_DIR>/<x>/<y>.json.gz: {"crossings": [[lat, lon, kit]]}, kit "gate" / "flash" / "cross") and
<OUT_DIR>/manifest.json. Built with the weekly rail file by TecnaGamer/BeamNG.Earth-data's GitHub Action and read by
server/fra.py from its GitHub Pages (one small file per ~300 km instead of a query per map cell to
data.transportation.gov from every player).

    python3 tools/build_fra_shards.py out/fra
"""
import gzip
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

URL = "https://data.transportation.gov/resource/m2f8-22s6.json"
FIELDS = ("latitude", "longitude", "countroadwaygatearms", "countflashinglightpair", "countmastmountedflashinglight",
          "countcantileveroverlane")
PAGE = 50000
SHARD_ZOOM = 7
UA = "BeamNG.Earth data build (github.com/TecnaGamer/BeamNG.Earth-data)"


def kit_of(rec):
    """server/fra.py kit_of: gates, else flashing lights, else crossbucks."""
    n = lambda k: int(float(rec.get(k) or 0))
    if n("countroadwaygatearms") > 0:
        return "gate"
    if n("countflashinglightpair") > 0 or n("countmastmountedflashinglight") > 0 or n("countcantileveroverlane") > 0:
        return "flash"
    return "cross"


def tile(lat, lon, z):
    n = 1 << z
    lat = max(-85.05112878, min(85.05112878, lat))
    x = int((lon + 180.0) / 360.0 * n)
    r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def fetch():
    rows, off = [], 0
    where = "crossingposition = 'At Grade' AND reasondescription != 'Closed' AND latitude IS NOT NULL"
    while True:
        q = URL + "?" + urllib.parse.urlencode({"$select": ",".join(FIELDS), "$where": where, "$order": ":id",
                                                "$limit": PAGE, "$offset": off})
        for k in range(5):
            try:
                with urllib.request.urlopen(urllib.request.Request(q, headers={"User-Agent": UA}), timeout=120) as r:
                    page = json.loads(r.read().decode("utf-8"))
                break
            except Exception as e:
                if k == 4:
                    raise
                print(f"  retry {k + 1}: {e}", flush=True)
                time.sleep(10 * (k + 1))
        rows += page
        off += len(page)
        print(f"  {off:,} crossings", flush=True)
        if len(page) < PAGE:
            return rows


def main():
    out = sys.argv[1]
    rows = fetch()
    shards = {}
    for r in rows:
        try:
            la, lo = float(r["latitude"]), float(r["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= la <= 90 and -180 <= lo <= 180) or (la, lo) == (0.0, 0.0):
            continue
        shards.setdefault(tile(la, lo, SHARD_ZOOM), []).append([round(la, 7), round(lo, 7), kit_of(r)])
    for (x, y), c in shards.items():
        os.makedirs(os.path.join(out, str(x)), exist_ok=True)
        with gzip.open(os.path.join(out, str(x), f"{y}.json.gz"), "wt", encoding="utf-8", compresslevel=9) as fh:
            json.dump({"crossings": c}, fh, separators=(",", ":"))
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"format": 1, "zoom": SHARD_ZOOM, "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "crossings": sum(len(c) for c in shards.values()), "shards": len(shards),
                   "license": "public domain (US Federal Railroad Administration)"}, fh)
    print(f"{sum(len(c) for c in shards.values()):,} crossings in {len(shards)} shards")


if __name__ == "__main__":
    sys.exit(main())
