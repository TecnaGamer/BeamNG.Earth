"""Developer-only extraction of pinned Overture territorial country areas.

Uses byte-range reads through the repository's Parquet reader. Requires the
server dependencies plus Shapely; players never run this tool. Requests use
urllib's proxy/CA configuration, not DuckDB extensions. Cache is release-scoped.
"""
import argparse
import json
import gzip
from pathlib import Path
import sys
from types import SimpleNamespace
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import overture

RELEASE = "2026-09-23.1"


class Fetcher:
    def get(self, url, kind="demand", headers=None):
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=60) as r:
            return SimpleNamespace(body=r.read(), status=r.status,
                                   headers={k.lower(): v for k, v in r.headers.items()})

    def miss_alive(self, *args):
        return False


def fetch(cache, output):
    from shapely import from_wkb, to_geojson
    reader = overture.Reader(str(cache), Fetcher(), release=RELEASE)
    exported = output
    exported.mkdir(parents=True, exist_ok=True)
    try:
        hits = reader.index.files("divisions", "division_area", (-180, -90, 180, 90))
        for name, urls, size in hits:
            saved = exported / (name + ".geojson.gz")
            if not saved.exists():
                remote = reader._remote("divisions", "division_area", name, urls, size)
                con, proto, errors = reader._connect({name: remote}, "demand")
                src = f"read_parquet('{proto}://{name}', file_row_number=true)"
                try:
                    ids = con.execute(f"SELECT file_row_number FROM {src} WHERE subtype IN ('country','dependency') "
                                      f"AND is_territorial").fetchall()
                    rows = []
                    if ids:
                        selected = ",".join(str(i[0]) for i in ids)
                        rows = con.execute(f"SELECT id, country, version, subtype, sources, ST_AsWKB(geometry) "
                                           f"FROM {src} WHERE file_row_number IN ({selected})").fetchall()
                    fs = [{"type": "Feature", "properties": {
                        "id": r[0], "country": r[1], "version": r[2], "subtype": r[3], "sources": [q for q in r[4] if q["property"] in ("", "/geometry", "/properties/country")]},
                        "geometry": json.loads(to_geojson(from_wkb(r[5])))} for r in rows]
                    saved.write_bytes(gzip.compress(json.dumps({"type":"FeatureCollection", "features":fs}, separators=(",", ":")).encode(), mtime=0))
                except Exception:
                    if errors:
                        print("Last fetch error:", errors[-1], file=sys.stderr)
                    raise
                finally:
                    con.close()
            print(name, saved.stat().st_size, "bytes", flush=True)
    finally:
        reader.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fetch(args.cache_dir, args.output)
