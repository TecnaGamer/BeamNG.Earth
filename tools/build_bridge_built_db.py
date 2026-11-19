"""Build server/bridges_built_us.npz from the FHWA National Bridge Inventory (public domain, US DOT): every bridge built
or reconstructed since MIN_YEAR, its point, structure length and that year.

  python3 tools/build_bridge_built_db.py NBI.txt     the all-states delimited file (fhwa.dot.gov/bridge/nbi/ascii.cfm:
                                                     "2025hwybronefiledel.zip", comma separated, ' quoted)

roads.BridgeProfiles reads it (roads.OBS_BUILT_*): a US lidar survey flown before a bridge was built or rebuilt
measured the ground or the old structure there, not its deck (Atlanta's GA-400 / I-285 interchange, rebuilt 2020-2023,
under Georgia's 2018 survey). Per bridge: lat, lon (items 16 / 17, DDMMSS.ss packed), structure length (item 49, m) and
the later of the year built (27) and reconstructed (106)."""
import csv
import os
import sys

import numpy as np

MIN_YEAR = 2005         # (the 3DEP surveys the server reads are from 2008 on: an older bridge stood for all of them)
MAX_YEAR = 2030         # (a few records carry 9999)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "bridges_built_us.npz")


def dms(v, deg_digits):
    """NBI packed degrees-minutes-seconds (seconds to hundredths) -> decimal degrees, or None."""
    v = v.strip()
    if not v.isdigit() or len(v) < deg_digits + 4:
        return None
    v = v.zfill(deg_digits + 6)
    d, m, s = int(v[:deg_digits]), int(v[deg_digits:deg_digits + 2]), int(v[deg_digits + 2:]) / 100.0
    if m >= 60 or s >= 60 or d == 0:
        return None
    return d + m / 60.0 + s / 3600.0


def year(v):
    v = (v or "").strip()
    return int(v) if v.isdigit() else 0


def main(path):
    rows = []
    with open(path, encoding="latin-1", newline="") as f:
        for r in csv.DictReader(f, quotechar="'"):
            built = max(year(r.get("YEAR_BUILT_027")), year(r.get("YEAR_RECONSTRUCTED_106")))
            if built < MIN_YEAR or built > MAX_YEAR:
                continue
            la, lo = dms(r["LAT_016"], 2), dms(r["LONG_017"], 3)
            if la is None or lo is None:
                continue
            try:
                ln = float(r["STRUCTURE_LEN_MT_049"])
            except ValueError:
                ln = 0.0
            rows.append((la, -lo, ln, built))
    a = np.array(rows, np.float64)
    a = a[np.argsort(a[:, 0], kind="stable")]
    np.savez_compressed(OUT, lat=a[:, 0].astype(np.float32), lon=a[:, 1].astype(np.float32),
                        length=a[:, 2].astype(np.float32), built=a[:, 3].astype(np.uint16))
    print(len(a), "bridges ->", os.path.normpath(OUT), os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
