"""build_oilwells_hifld.py [--url LAYER] [--stats] [--merge]: the United States' oil wells from HIFLD's "Oil and Natural
Gas Wells" layer (state well data compiled by the US Department of Homeland Security; public domain, a work of the US
government) into server/oil_wells.npz, in place of OpenStreetMap's wells inside the US (OSM maps a few of the US's
wells; HIFLD has every state's).

    python3 tools/build_oilwells_hifld.py --stats      # the layer's TYPE / STATUS values and counts
    python3 tools/build_oilwells_hifld.py              # download (state by state, cached), then --merge
    python3 tools/build_oilwells_hifld.py --merge      # the npz from the cached download

HIFLD Open's own services closed in 2025; the national layer is mirrored by NASA's NCCS (LAYER below). The raw rows
are kept in ~/.cache/beamng-earth/hifld_wells/<STATE>.tsv (lat, lon, TYPE, STATUS) so a merge never downloads again.
Kept: wells whose TYPE is an oil well and whose STATUS says it produces (KEEP_STATUS); flags as build_oilwells.py
(1 gas: never set here, gas wells are left out; 2 pump=no: flowing wells, STATUS "FLOWING")."""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAYER = "https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/15"
CACHE = os.path.expanduser("~/.cache/beamng-earth/hifld_wells")
PAGE = 2000
PAUSE_S = 0.5               # between requests (a public mirror)
UA = "BeamNG.Earth/build_oilwells_hifld (github.com/TecnaGamer/BeamNG.Earth)"
KEEP_STATUS = ("PRODUCING", "ACTIVE", "FLOWING", "PUMPING", "COMPLETED", "SHUT-IN", "SHUT IN", "TEMPORARILY ABANDONED")
DROP_STATUS = ("PLUGGED", "ABANDONED", "DRY", "PERMIT", "CANCEL", "INJECT", "DISPOSAL", "STORAGE", "OBSERVATION",
               "MONITOR", "WATER", "ORPHAN", "LOCATION", "EXPIRED", "INACTIVE")


def _get(url, params, tries=4):
    q = url + "?" + urllib.parse.urlencode(params)
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(q, headers={"User-Agent": UA}), timeout=120) as r:
                d = json.loads(r.read().decode("utf-8"))
            if "error" in d:
                raise RuntimeError(d["error"])
            return d
        except Exception as e:
            if k == tries - 1:
                raise
            print(f"  retry {k + 1}: {type(e).__name__}: {str(e)[:120]}", flush=True)
            time.sleep(5 * (k + 1))


def _stats(url, fields):
    d = _get(url + "/query", {"where": "1=1", "returnGeometry": "false", "groupByFieldsForStatistics": ",".join(fields),
                              "outStatistics": json.dumps([{"statisticType": "count", "onStatisticField": "OBJECTID",
                                                            "outStatisticFieldName": "n"}]), "f": "json"})
    return sorted(((tuple(f["attributes"].get(x) for x in fields), f["attributes"]["n"]) for f in d["features"]),
                  key=lambda r: -r[1])


def keep(typ, status):
    """(kept, flags) of a well by its HIFLD TYPE and STATUS."""
    t, s = (typ or "").upper(), (status or "").upper()
    if "OIL" not in t or ("GAS" in t and "OIL" not in t):
        return False, 0
    if any(w in s for w in DROP_STATUS) and "TEMPORARILY ABANDONED" not in s:
        return False, 0
    if not any(w in s for w in KEEP_STATUS):
        return False, 0
    return True, (2 if "FLOWING" in s else 0)


def download(url):
    os.makedirs(CACHE, exist_ok=True)
    states = [r[0][0] for r in _stats(url, ["STATE"]) if r[0][0]]
    print(f"{len(states)} states", flush=True)
    for st in states:
        path = os.path.join(CACHE, f"{st}.tsv")
        if os.path.exists(path):
            continue
        rows, off = [], 0
        while True:
            d = _get(url + "/query", {"where": f"STATE='{st}'", "outFields": "LATITUDE,LONGITUDE,TYPE,STATUS",
                                      "returnGeometry": "false", "orderByFields": "OBJECTID", "resultOffset": off,
                                      "resultRecordCount": PAGE, "f": "json"})
            feats = d.get("features", [])
            for f in feats:
                a = f["attributes"]
                rows.append((a.get("LATITUDE"), a.get("LONGITUDE"), a.get("TYPE") or "", a.get("STATUS") or ""))
            off += len(feats)
            time.sleep(PAUSE_S)
            if not feats or not d.get("exceededTransferLimit"):
                break
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            for la, lo, t, s in rows:
                fh.write(f"{la}\t{lo}\t{t}\t{s}\n")
        os.replace(tmp, path)
        print(f"{st}: {len(rows)} wells", flush=True)


def merge():
    lat, lon, flags = [], [], []
    seen = 0
    for name in sorted(os.listdir(CACHE)):
        if not name.endswith(".tsv"):
            continue
        with open(os.path.join(CACHE, name), encoding="utf-8") as fh:
            for line in fh:
                la, lo, t, s = (line.rstrip("\n").split("\t") + ["", "", "", ""])[:4]
                seen += 1
                try:
                    la, lo = float(la), float(lo)
                except ValueError:
                    continue
                ok, fl = keep(t, s)
                if ok and -90 <= la <= 90 and -180 <= lo <= 180 and (la, lo) != (0.0, 0.0):
                    lat.append(round(la * 1e6))
                    lon.append(round(lo * 1e6))
                    flags.append(fl)
    us = (np.array(lat, np.int64), np.array(lon, np.int64), np.array(flags, np.uint8))
    out = os.path.join(ROOT, "server", "oil_wells.npz")
    with np.load(out) as f:
        olat, olon, ofl = f["lat"].astype(np.int64), f["lon"].astype(np.int64), f["flags"]
    # (OSM's wells outside the states HIFLD covers: a box test by the HIFLD wells' own extent per degree cell)
    cells = set(zip((us[0] // 1_000_000).tolist(), (us[1] // 1_000_000).tolist()))
    away = np.array([(a, o) not in cells for a, o in zip((olat // 1_000_000).tolist(), (olon // 1_000_000).tolist())],
                    bool)
    la = np.concatenate([us[0], olat[away]]).astype(np.int32)
    lo = np.concatenate([us[1], olon[away]]).astype(np.int32)
    fl = np.concatenate([us[2], ofl[away]]).astype(np.uint8)
    order = np.lexsort((lo, la))
    np.savez_compressed(out, lat=la[order], lon=lo[order], flags=fl[order])
    print(f"{out}: {len(la)} wells ({len(us[0])} HIFLD of {seen} rows, {int(away.sum())} OSM outside them), "
          f"{os.path.getsize(out) / 1e6:.2f} MB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=LAYER)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--merge", action="store_true")
    a = ap.parse_args()
    if a.stats:
        for k, n in _stats(a.url, ["TYPE", "STATUS"])[:80]:
            print(n, k, "KEEP" if keep(*k)[0] else "")
        return
    if not a.merge:
        download(a.url)
    merge()


if __name__ == "__main__":
    sys.exit(main())
