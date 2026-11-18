"""Build server/bridges_us.npz from the FHWA National Bridge Inventory (public domain, US DOT).

  python3 tools/build_bridge_db.py NBI.txt     the all-states delimited file (fhwa.dot.gov/bridge/nbi/ascii.cfm:
                                               "2025hwybronefiledel.zip", comma separated, ' quoted)

Keeps the bridges whose main span (item 43B) is a structure drawn over the deck: trusses, arches, suspension,
cable-stayed and movable spans (bridgetypes.py), and any bridge over a navigation channel (its clearance sets the
deck's height over the water). Per bridge: lat, lon (item 16/17, DDMMSS.ss packed), the type code,
the longest span (item 48), the structure length (49), the deck width (52) and the navigation channel's vertical
clearance (39, 0 where no channel is controlled), in metres."""
import csv
import os
import sys

try:        # (first: numpy's AVX-512 kernels held back before anything imports numpy, tools/npyfirst.py)
    import npyfirst  # noqa: E402,F401
except ImportError:     # (loaded by its path into another process, a test: that process's entry point guards)
    pass
import numpy as np

KEEP = {"09", "10", "11", "12", "13", "14", "15", "16", "17"}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "bridges_us.npz")


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


def num(v):
    try:
        return float(v)
    except ValueError:
        return 0.0


def main(path):
    rows = []
    with open(path, encoding="latin-1", newline="") as f:
        for r in csv.DictReader(f, quotechar="'"):
            t = r["STRUCTURE_TYPE_043B"].strip()
            nav = num(r["NAV_VERT_CLR_MT_039"]) if r["NAVIGATION_038"].strip() == "1" else 0.0
            if t not in KEEP and nav <= 0 or not t.isdigit():
                continue
            la, lo = dms(r["LAT_016"], 2), dms(r["LONG_017"], 3)
            if la is None or lo is None:
                continue
            rows.append((la, -lo, int(t), num(r["MAX_SPAN_LEN_MT_048"]), num(r["STRUCTURE_LEN_MT_049"]),
                         num(r["DECK_WIDTH_MT_052"]), nav))
    a = np.array(rows, np.float64)
    o = np.argsort(a[:, 0])
    a = a[o]
    np.savez_compressed(OUT, lat=a[:, 0].astype(np.float32), lon=a[:, 1].astype(np.float32),
                        type=a[:, 2].astype(np.uint8), span=a[:, 3].astype(np.float32),
                        length=a[:, 4].astype(np.float32), width=a[:, 5].astype(np.float32),
                        nav=a[:, 6].astype(np.float32))
    print(len(a), "bridges ->", os.path.normpath(OUT), os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
