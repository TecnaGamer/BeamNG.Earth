"""Normalize the Ministry of the Interior's fixed speed camera CSV for offline tiles."""
import argparse
import csv
import gzip
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from radars import DATA, HARDWARE  # noqa: E402

SOURCE = "https://www.data.gouv.fr/datasets/liste-des-radars-fixes-en-france"
RESOURCE = "17f7cfd9-a5fe-4b6a-9f5d-3625feaa396e"


def build(path):
    by_id = {}
    with path.open(encoding="cp1252", newline="") as file:
        rows = csv.reader(file, delimiter=";")
        header = [v.strip() for v in next(rows)]
        if header != ["Numéro de radar", "Type de radar", "Date de mise en service", "VMA", "Latitude", "Longitude"]:
            raise ValueError("Unexpected fixed speed camera CSV header")
        for ident, kind, _, limit, lat, lon in rows:
            ident, kind = ident.strip(), kind.strip()
            if kind not in HARDWARE or not limit.strip().isdigit():
                continue
            limit, lat, lon = int(limit), float(lat), float(lon)
            if not (ident and 20 <= limit <= 130 and math.isfinite(lat) and math.isfinite(lon)
                    and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon)):
                raise ValueError(f"Invalid surveyed speed camera {ident}")
            if ident in by_id:
                raise ValueError(f"Duplicate speed camera {ident}")
            by_id[ident] = [ident, kind, limit, lat, lon]
    if len(by_id) < 2_000:
        raise ValueError("Incomplete fixed speed camera catalogue")
    doc = {"source": SOURCE, "resource": RESOURCE, "published": "2025-12-30", "cameras": list(by_id.values())}
    DATA.write_bytes(gzip.compress(json.dumps(doc, separators=(",", ":")).encode(), mtime=0))
    print(f"{len(by_id)} surveyed fixed speed cameras -> {DATA.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    build(parser.parse_args().csv)
