"""The viewer's OSM reader (src/osm.rs, /api/osm) against the server's own (mapdata._unpack): every way's id,
coordinates and attributes must be equal. Read only: it opens the mirror and asks the running viewer.

    .venv/Scripts/python native/viewer/tests/osm_golden.py X Y [X Y ...]     (z14 cells; the viewer on :47700)
"""
import json
import math
import os
import sys
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "server"))
import mapdata      # noqa: E402
import numpy as np  # noqa: E402

VIEWER = os.environ.get("VIEWER", "http://127.0.0.1:47700")


def cell_dir(cache, x, y):
    base = os.path.join(cache, "mapdata-mirror")
    for ver in sorted(os.listdir(base), reverse=True):
        ov = os.path.join(base, ver, "overture")
        for rel in sorted(os.listdir(ov), reverse=True) if os.path.isdir(ov) else []:
            layers = sorted((d for d in os.listdir(os.path.join(ov, rel)) if d.startswith("roads")),
                            key=lambda d: int(d.split(".")[1]) if "." in d else 0, reverse=True)
            for lay in layers:
                d = os.path.join(ov, rel, lay, "14", str(x), str(y))
                if os.path.isfile(os.path.join(d, "ok")):
                    return d
    return None


def check(x, y, cache):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    got = json.load(opener.open(f"{VIEWER}/api/osm?z=14&x={x}&y={y}"))
    d = cell_dir(cache, x, y)
    if d is not None:
        header = json.loads(open(os.path.join(d, "ok"), encoding="utf-8").read().splitlines()[2])
        a = mapdata._unpack(os.path.join(d, mapdata.PACK_FILE), header)
    else:       # (no valid mirror copy: the compressed cache, as earthd and the viewer fall back to it)
        import glob
        npz = sorted(glob.glob(os.path.join(cache, "mapdata", "v*", "overture", "*", "roads*", "14", str(x), f"{y}.npz")))
        if not npz:
            print(f"{x} {y}: not cached (viewer missing={got.get('missing')})")
            return bool(got.get("missing"))
        a = dict(np.load(npz[-1]))
    if got.get("missing"):
        print(f"{x} {y}: cached, but the viewer says missing")
        return False
    n, ways, bad, i = len(a["kind"]), got["ways"], 0, 0
    for f in range(n):
        if a["kind"][f] != 2:
            continue
        w = ways[i]
        i += 1
        bad += w["id"] != str(a["ids"][f])
        parts = [a["lonlat"][a["coord_off"][r]:a["coord_off"][r + 1]].ravel().tolist()
                 for p in range(a["part_off"][f], a["part_off"][f + 1])
                 for r in range(a["ring_off"][p], a["ring_off"][p + 1])]
        bad += parts != w["g"]
        for k, col in a.items():
            if not k.startswith("attr.") or k in ("attr.cx", "attr.cy") or col.shape != (n,):
                continue
            v = col[f]
            if isinstance(v, (str, np.str_)):
                exp = str(v) or None
            elif isinstance(v, (bool, np.bool_)):
                exp = bool(v)
            else:
                exp = None if math.isnan(float(v)) else float(v)
            g = w["tags"].get(k[5:])
            if exp != g and not (isinstance(g, (int, float)) and exp == float(g)):
                bad += 1
                print(f"  way {i - 1} {k}: {exp!r} != {g!r}")
    bad += i != len(ways)
    print(f"{x} {y}: {len(ways)} ways, {len(got['nodes'])} nodes, {bad} mismatches")
    return bad == 0


def main():
    cfg = json.load(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"{VIEWER}/api/config"))
    xy = [int(v) for v in sys.argv[1:]] or [2819, 6545]
    ok = all([check(xy[k], xy[k + 1], cfg["cacheDir"]) for k in range(0, len(xy) - 1, 2)])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
