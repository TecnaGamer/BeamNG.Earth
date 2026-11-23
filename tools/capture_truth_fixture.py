"""Capture the measured shape of a place's roads and railways: swisstopo's swissTLM3D lines round a point, as a small
fixture the tests read offline (tests/truth.py).

    python tools/capture_truth_fixture.py NAME LAT LON TLM_DIR [--half M]

swissTLM3D (Federal Office of Topography swisstopo, free with its source named) has every road and railway of
Switzerland as a 3D line, bridges and tunnels as parts of the line with their level (KUNSTBAUTE, STUFE), 0.2-1.5 m in
all three dimensions: what a road's height is, where the map and the elevation only let Earth work it out. TLM_DIR
holds the release's shapefiles (swissTLM3D_TLM_STRASSE, swissTLM3D_TLM_EISENBAHN, swissTLM3D_TLM_MAUER: .shp, .shx,
.dbf, taken out of the country's 3.6 GB zip, which stays out of the repository). Writes
server/tests/fixtures/truth_NAME.json: the lines with a point within --half (default 600 m) of the place, and past
it the rest of every bridge and tunnel that begins there up to the road on the ground it ends on (a structure is never
cut at the fixture's edge: its loose end would read as an end in mid-air), in the ENU
frame of (LAT, LON) (x east, y north, m; z the line's own height, LN02), the same frame as the place's replay
(tools/capture_interchange_fixture.py NAME LAT LON CACHE --prefix truth: the map and the elevation Earth reads there).
"""
import argparse
import json
import math
import mmap
import struct
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import geodesy  # noqa: E402

RELEASE = "swissTLM3D 2026-02"
SOURCE = "Federal Office of Topography swisstopo"
# CH1903+ / LV95 (swisstopo, "Formulas and constants for the calculation of the Swiss conformal cylindrical projection")
BESSEL_A, BESSEL_E2 = 6377397.155, 0.006674372230614
PHI0, LAM0 = math.radians(46.0 + 57.0 / 60 + 8.66 / 3600), math.radians(7.0 + 26.0 / 60 + 22.50 / 3600)
SHIFT = (674.374, 15.056, 405.346)          # m: CH1903+ to ETRS89 / WGS84, geocentric
WGS_A, WGS_E2 = 6378137.0, 0.00669438002290


def lv95_to_wgs84(E, N, h=0.0):
    """(lat, lon) deg (WGS84) of LV95 coordinates E, N (m) at height h (m): the projection's own inverse on Bessel's
    ellipsoid, then the datum's shift."""
    E, N, h = (np.asarray(v, np.float64) for v in (E, N, h))
    e = math.sqrt(BESSEL_E2)
    R = BESSEL_A * math.sqrt(1 - BESSEL_E2) / (1 - BESSEL_E2 * math.sin(PHI0) ** 2)
    al = math.sqrt(1 + BESSEL_E2 / (1 - BESSEL_E2) * math.cos(PHI0) ** 4)
    b0 = math.asin(math.sin(PHI0) / al)
    K = (math.log(math.tan(math.pi / 4 + b0 / 2)) - al * math.log(math.tan(math.pi / 4 + PHI0 / 2))
         + al * e / 2 * math.log((1 + e * math.sin(PHI0)) / (1 - e * math.sin(PHI0))))
    lb = (E - 2600000.0) / R
    bb = 2 * (np.arctan(np.exp((N - 1200000.0) / R)) - math.pi / 4)
    b = np.arcsin(math.cos(b0) * np.sin(bb) + math.sin(b0) * np.cos(bb) * np.cos(lb))
    ll = np.arctan2(np.sin(lb), math.cos(b0) * np.cos(lb) - math.sin(b0) * np.tan(bb))
    lam = LAM0 + ll / al
    phi = b.copy()
    for _ in range(8):
        S = (np.log(np.tan(math.pi / 4 + b / 2)) - K) / al + e * np.log(np.tan(math.pi / 4 + np.arcsin(e * np.sin(phi)) / 2))
        phi = 2 * np.arctan(np.exp(S)) - math.pi / 2
    n = BESSEL_A / np.sqrt(1 - BESSEL_E2 * np.sin(phi) ** 2)
    X = (n + h) * np.cos(phi) * np.cos(lam) + SHIFT[0]
    Y = (n + h) * np.cos(phi) * np.sin(lam) + SHIFT[1]
    Z = (n * (1 - BESSEL_E2) + h) * np.sin(phi) + SHIFT[2]
    p = np.hypot(X, Y)
    lat = np.arctan2(Z, p * (1 - WGS_E2))
    for _ in range(8):
        n = WGS_A / np.sqrt(1 - WGS_E2 * np.sin(lat) ** 2)
        lat = np.arctan2(Z + WGS_E2 * n * np.sin(lat), p)
    return np.degrees(lat), np.degrees(np.arctan2(Y, X))


def wgs84_to_lv95(lat, lon):
    """(E, N) m of a WGS84 point: swisstopo's approximate formulas (about 1 m), enough to find the place's box."""
    p = (lat * 3600.0 - 169028.66) / 10000.0
    q = (lon * 3600.0 - 26782.5) / 10000.0
    E = 2600072.37 + 211455.93 * q - 10938.51 * q * p - 0.36 * q * p * p - 44.54 * q ** 3
    N = 1200147.07 + 308807.95 * p + 3745.25 * q * q + 76.63 * p * p - 194.56 * q * q * p + 119.79 * p ** 3
    return E, N


def _fields(dbf):
    """(records, header length, record length, [(name, offset, length)]) of an open .dbf's header."""
    head = dbf[:32]
    nrec, hlen, rlen = struct.unpack("<IHH", head[4:12])
    out, at, off = [], 32, 1
    while dbf[at] != 0x0D:
        d = dbf[at:at + 32]
        out.append((d[:11].split(b"\0")[0].decode(), off, d[16]))
        off += d[16]
        at += 32
    return nrec, hlen, rlen, out


def lines_in(stem, box):
    """[(attributes {field: text}, xyz (k, 3) LV95)] of the PolyLineZ shapefile stem (.shp, .shx, .dbf) whose parts
    meet box (E0, N0, E1, N1); a record of several parts gives one line each."""
    with open(stem + ".shx", "rb") as f:
        shx = np.frombuffer(f.read()[100:], ">i4").reshape(-1, 2)
    offs = shx[:, 0].astype(np.int64) * 2
    out = []
    with open(stem + ".shp", "rb") as f, open(stem + ".dbf", "rb") as g:
        shp = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        dbf = mmap.mmap(g.fileno(), 0, access=mmap.ACCESS_READ)
        raw = np.frombuffer(shp, np.uint8)
        bb = raw[(offs + 12)[:, None] + np.arange(32)[None]].copy().view("<f8").reshape(-1, 4)
        hit = np.flatnonzero((bb[:, 0] <= box[2]) & (bb[:, 2] >= box[0]) & (bb[:, 1] <= box[3]) & (bb[:, 3] >= box[1]))
        nrec, hlen, rlen, fields = _fields(dbf)
        for i in hit.tolist():
            o = int(offs[i]) + 8
            if struct.unpack_from("<i", shp, o)[0] != 13:
                continue
            nparts, npts = struct.unpack_from("<2i", shp, o + 36)
            parts = list(struct.unpack_from(f"<{nparts}i", shp, o + 44)) + [npts]
            xy = np.frombuffer(shp, "<f8", 2 * npts, o + 44 + 4 * nparts).reshape(-1, 2)
            z = np.frombuffer(shp, "<f8", npts, o + 44 + 4 * nparts + 16 * npts + 16)
            rec = dbf[hlen + i * rlen:hlen + (i + 1) * rlen]
            attrs = {name: rec[off:off + ln].decode("utf-8", "replace").strip() for name, off, ln in fields}
            for a, b in zip(parts[:-1], parts[1:]):
                if b - a >= 2:
                    out.append((attrs, np.c_[xy[a:b], z[a:b]].astype(np.float64)))
        del raw, bb
    return out


CLOSE_M = 1500.0        # m past the place a structure that begins there is followed


def _node(p):
    return (round(float(p[0]), 2), round(float(p[1]), 2), round(float(p[2]), 2))


def _structure(attrs):
    """Whether a line is on a bridge, in a tunnel, or not said (KUNSTBAUTE k_W: a connector between such parts)."""
    return attrs.get("KUNSTBAUTE", "Keine") not in ("Keine", "")


ROAD_FIELDS = {"UUID": "id", "OBJEKTART": "objektart", "KUNSTBAUTE": "kunstbaute", "STUFE": "stufe",
               "RICHTUNGSG": "richtungsgetrennt", "BELAGSART": "belagsart", "VERKEHRSBD": "verkehrsbedeutung",
               "VERKEHRSBE": "verkehrsbeschraenkung", "KREISEL": "kreisel", "BEFAHRBARK": "befahrbarkeit", "NAME": "name"}
RAIL_FIELDS = {"UUID": "id", "OBJEKTART": "objektart", "KUNSTBAUTE": "kunstbaute", "STUFE": "stufe",
               "ANZAHL_SPU": "anzahl_spuren", "VERKEHRSMI": "verkehrsmittel", "AUF_STRASS": "auf_strasse",
               "AUSSER_BET": "ausser_betrieb", "NAME": "name"}
WALL_FIELDS = {"UUID": "id", "OBJEKTART": "objektart"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("tlm", help="the folder of the release's shapefiles")
    ap.add_argument("--half", type=float, default=600.0)
    a = ap.parse_args()
    E0, N0 = wgs84_to_lv95(a.lat, a.lon)
    frame = geodesy.EnuFrame(a.lat, a.lon)
    reach = a.half + CLOSE_M
    box = (E0 - reach, N0 - reach, E0 + reach, N0 + reach)
    out = {"source": SOURCE, "release": RELEASE, "licence": "free use with the source named (swisstopo OGD)",
           "origin": [a.lat, a.lon], "half_m": a.half, "frame": "ENU of origin: x east, y north (m); z LN02 (m)"}
    for key, layer, fields in (("roads", "swissTLM3D_TLM_STRASSE", ROAD_FIELDS),
                               ("rails", "swissTLM3D_TLM_EISENBAHN", RAIL_FIELDS),
                               ("walls", "swissTLM3D_TLM_MAUER", WALL_FIELDS)):
        cand = []
        for attrs, xyz in lines_in(str(Path(a.tlm) / layer), box):
            la, lo = lv95_to_wgs84(xyz[:, 0], xyz[:, 1], xyz[:, 2])
            e, n, _ = frame.geodetic_to_enu(la, lo, 0.0)
            cand.append((attrs, np.c_[np.asarray(e, np.float64), np.asarray(n, np.float64), xyz[:, 2]],
                         (_node(xyz[0]), _node(xyz[-1]))))
        keep = {k for k, (_a, enu, _n) in enumerate(cand) if (np.abs(enu[:, :2]) <= a.half).all(axis=1).any()}
        at = {}
        for k, (_a, _e, nodes) in enumerate(cand):
            for nd in nodes:
                at.setdefault(nd, []).append(k)
        todo = [k for k in keep if _structure(cand[k][0])]
        while todo:                     # (a structure's chain followed to the lines on the ground it ends on)
            k = todo.pop()
            for nd in cand[k][2]:
                for j in at[nd]:
                    if j not in keep:
                        keep.add(j)
                        if _structure(cand[j][0]):
                            todo.append(j)
        rows = []
        for k in sorted(keep):
            attrs, enu, _n = cand[k]
            row = {new: attrs.get(old, "") for old, new in fields.items()}
            row["xyz"] = [round(float(v), 2) for v in enu.ravel()]
            rows.append(row)
        rows.sort(key=lambda r: r["id"])
        out[key] = rows
        print(f"{key}: {len(rows)} lines", flush=True)
    path = ROOT / "server" / "tests" / "fixtures" / f"truth_{a.name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{path}: {path.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
