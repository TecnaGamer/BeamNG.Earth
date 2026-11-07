#!/usr/bin/env python3
"""build_de_gemeinden.py GV100AD.txt [OUT]: Germany's Gemeinden and which are a Stadt or a Markt, from the Federal
Statistical Office's municipality directory (Destatis, Gemeindeverzeichnis GV100AD, the quarterly "Gebietsstand"
text file: https://www.destatis.de/DE/Themen/Laender-Regionen/Regionales/Gemeindeverzeichnis/_inhalt.html), for the
small line of an Ortsteil's Ortstafel (server/townentry.py de_kind: "Stadt Schrobenhausen", "Markt Donaustauf",
"Gemeinde Barbing"; VwV-StVO zu den Zeichen 310 und 311 Rn. 6) and which of a village's localities is its Gemeinde
(de_town: Overture names a Verwaltungsgemeinschaft as its seat, "Schrobenhausen" over Gachenbach).

    python3 tools/build_de_gemeinden.py GV100AD_30092026.txt server/data/de_gemeinden.json.gz

The file's records (Datensatzbeschreibung GV100AD, fixed columns, UTF-8 since the Sorbian names): Satzart 40, a
Kreis (its key in columns 11-15, its name 23-72); Satzart 60, a Gemeinde (its Kreis' key 11-15, its name 23-72 with
Destatis' suffix after ", ": "Neutraubling, St", "Bad Abbach, M"; Textkennzeichen 123-124: 60 Markt, 61 / 62 a
kreisfreie Stadt, 63 Stadt, 67 Große Kreisstadt, 64 any other Gemeinde, 65 / 66 no Gemeinde: an unincorporated
area). Written: {"source", "stand", "rows": [[Kreis, Gemeinde, "S" | "M" | "G"]]}, sorted. Data: (c) Statistisches
Bundesamt (Destatis), reproduction and distribution with the source given permitted (the file's own notice).
"""
import argparse
import gzip
import json
import os
import sys

KINDS = {"60": "M", "61": "S", "62": "S", "63": "S", "67": "S", "64": "G"}


def rows_of(lines):
    """(stand YYYY-MM-DD, [[Kreis, Gemeinde, kind]]) of the GV100AD's lines."""
    kreise, out, stand = {}, [], ""
    for line in lines:
        line = line.rstrip("\r\n")
        sa = line[0:2]
        if not stand and len(line) >= 10 and line[2:10].isdigit():
            stand = f"{line[2:6]}-{line[6:8]}-{line[8:10]}"
        if sa == "40":
            kreise[line[10:15]] = line[22:72].strip()
        elif sa == "60":
            kind = KINDS.get(line[122:124])
            if kind is None:
                continue
            name = line[22:72].strip().split(", ")[0].strip()
            kreis = kreise.get(line[10:15], "")
            if name and kreis:
                out.append([kreis, name, kind])
    return stand, sorted(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gv100ad", help="GV100AD_DDMMYYYY.txt (from the Destatis zip)")
    ap.add_argument("out", nargs="?", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
        __file__))), "server", "data", "de_gemeinden.json.gz"))
    a = ap.parse_args()
    with open(a.gv100ad, encoding="utf-8-sig") as fh:
        stand, rows = rows_of(fh)
    doc = {"source": "Statistisches Bundesamt (Destatis), Gemeindeverzeichnis GV100AD", "stand": stand, "rows": rows}
    with gzip.open(a.out + ".part", "wt", encoding="utf-8", compresslevel=9) as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(a.out + ".part", a.out)
    print(f"{len(rows):,} Gemeinden (Gebietsstand {stand}) -> {a.out}, {os.path.getsize(a.out):,} B")


if __name__ == "__main__":
    sys.exit(main())
