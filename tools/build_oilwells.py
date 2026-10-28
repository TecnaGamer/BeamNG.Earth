"""build_oilwells.py WELLS.tsv: server/oil_wells.npz from an Overpass CSV export of OpenStreetMap's oil wells (the query
in server/oilwells.py's terms: nwr["man_made"~"^(petroleum_well|pumping_rig)$"]; out center; columns @type @lat @lon
man_made substance pump end_date disused:man_made abandoned:man_made, tab separated, with a header). Closed wells (an
end_date, disused / abandoned) are left out; flags: 1 gas (substance gas, not oil), 2 pump=no. ODbL: the file is a
derived database of OpenStreetMap (c) OpenStreetMap contributors (CREDITS.md)."""
import csv
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(path):
    lat, lon, flags = [], [], []
    with open(path, newline="", encoding="utf-8") as f:
        rows = csv.reader(f, delimiter="\t")
        head = next(rows)
        col = {h: i for i, h in enumerate(head)}
        for r in rows:
            if len(r) < len(head) or not r[col["@lat"]]:
                continue
            if r[col["end_date"]] or r[col["disused:man_made"]] or r[col["abandoned:man_made"]]:
                continue
            sub = r[col["substance"]].lower()
            fl = (1 if "gas" in sub and "oil" not in sub else 0) | (2 if r[col["pump"]].lower() == "no" else 0)
            lat.append(round(float(r[col["@lat"]]) * 1e6))
            lon.append(round(float(r[col["@lon"]]) * 1e6))
            flags.append(fl)
    lat, lon, flags = np.array(lat, np.int32), np.array(lon, np.int32), np.array(flags, np.uint8)
    order = np.lexsort((lon, lat))
    out = os.path.join(ROOT, "server", "oil_wells.npz")
    np.savez_compressed(out, lat=lat[order], lon=lon[order], flags=flags[order])
    print(f"{out}: {len(lat)} wells ({int((flags & 1).sum())} gas, {int((flags & 2).sum())} pump=no), "
          f"{os.path.getsize(out) / 1e6:.2f} MB")


if __name__ == "__main__":
    main(sys.argv[1])
