"""build_map_highways.py [--src ne_10m_roads.geojson] [--out PATH]: the world map's Highways layer (EarthMap.vue,
Layers > Highways): the US Interstates and US routes from Natural Earth's 1:10m roads, as a small JS module
(mod/beamng_earth/ui/ui-vue/mods/beamngEarth/maphighways.js) the map draws when zoomed out, with route shields along
them. Zoomed out, the satellite map shows no roads at all, so a GPS route's line could not be told to follow an
Interstate (Tann2019, 2026-11-05: "i just couldnt tell because i cant see roads").

    python tools/build_map_highways.py                       (downloads Natural Earth v5.1.2's roads, ~50 MB, once)
    python tools/build_map_highways.py --src ne_10m_roads.geojson

Output {"interstates": [[lon, lat, ...], ...], "us": [...], "shields": [[number, lon, lat, minZoom, "I" | "US"]]}:
each route's pieces joined end to end where they meet, simplified (Ramer-Douglas-Peucker, SIMPLIFY_M) and rounded to
DECIMALS; a shield every SHIELD_KM along each route, the zoom it shows from halving the spacing at each step (so at
any zoom the shields are about evenly spread; the map drops those that would overlap), Interstates first.
Data: Natural Earth (public domain).
"""
import argparse
import json
import math
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NE_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_10m_roads.geojson"
OUT = os.path.join(ROOT, "mod", "beamng_earth", "ui", "ui-vue", "mods", "beamngEarth", "maphighways.js")
KINDS = {"Interstate": "I", "Federal": "US"}   # Natural Earth's `level` -> the shield kind
SIMPLIFY_M = 120.0          # ~1 px at the map's highest Highways zoom (z10: ~150 m a pixel at 40 deg)
DECIMALS = 3                # ~100 m: under half a pixel at z10
JOIN_DEG = 1e-4             # piece ends this close: the same point
SHIELD_KM = 20.0            # the densest shield spacing (shown from SHIELD_MAX_Z)
SHIELD_MAX_Z = 9.5          # ... each zoom step down halves how many show (one in 2, 4, 8 ...)
SHIELD_MIN_Z = {"I": 4.5, "I3": 6.5, "US": 6.5}   # the lowest zoom a tier's shields can show from (I3: the 3-digit
                                                 # spurs and loops, after the mainline Interstates)
TIER_ORDER = {"I": 0, "I3": 1, "US": 2}            # at the same zoom, the first kept where shields overlap


def _xy(lon, lat, lat_ref):
    m = 111320.0
    return lon * m * math.cos(math.radians(lat_ref)), lat * m


def simplify(pts, tol_m):
    """pts [(lon, lat)] with the vertices closer than tol_m to their neighbours' line dropped (Ramer-Douglas-
    Peucker, iterative)."""
    n = len(pts)
    if n < 3:
        return list(pts)
    lat_ref = pts[0][1]
    xy = [_xy(lo, la, lat_ref) for lo, la in pts]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2)) if L2 > 0 else 0.0
            d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, bi = d, i
        if bi > 0 and best > tol_m * tol_m:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def _near(p, q):
    return abs(p[0] - q[0]) <= JOIN_DEG and abs(p[1] - q[1]) <= JOIN_DEG


def join(pieces):
    """pieces [[(lon, lat), ...]] of one route as fewer, longer lines: a piece is joined (turned round if need be) to
    another whose end it starts or ends at, until none meet. Greedy; a route's forks stay separate lines."""
    lines = [list(p) for p in pieces if len(p) >= 2]
    merged = True
    while merged:
        merged = False
        i = 0
        while i < len(lines):
            a = lines[i]
            for j in range(i + 1, len(lines)):
                b = lines[j]
                if _near(a[-1], b[0]):
                    a.extend(b[1:])
                elif _near(a[-1], b[-1]):
                    a.extend(reversed(b[:-1]))
                elif _near(a[0], b[-1]):
                    a[:0] = b[:-1]
                elif _near(a[0], b[0]):
                    a[:0] = list(reversed(b[1:]))
                else:
                    continue
                del lines[j]
                merged = True
                break
            else:
                i += 1
    return lines


def length_km(line):
    lat_ref = line[0][1]
    xy = [_xy(lo, la, lat_ref) for lo, la in line]
    return sum(math.hypot(xy[i + 1][0] - xy[i][0], xy[i + 1][1] - xy[i][1]) for i in range(len(xy) - 1)) / 1000


def shield_points(line, spacing_km):
    """(lon, lat, k) every spacing_km along the line (k: the count from the line's first, half a spacing in)."""
    lat_ref = line[0][1]
    xy = [_xy(lo, la, lat_ref) for lo, la in line]
    step = spacing_km * 1000
    out, done, s, k = [], 0.0, step / 2, 0
    for i in range(len(line) - 1):
        seg = math.hypot(xy[i + 1][0] - xy[i][0], xy[i + 1][1] - xy[i][1])
        while seg > 0 and s <= done + seg:
            f = (s - done) / seg
            out.append((line[i][0] + f * (line[i + 1][0] - line[i][0]),
                        line[i][1] + f * (line[i + 1][1] - line[i][1]), k))
            k += 1
            s += step
        done += seg
    return out


def tier(kind, num):
    return "I3" if kind == "I" and len(num) >= 3 else kind


def shield_min_zoom(k, kind):
    """The zoom shield k (shield_points) shows from: every one at SHIELD_MAX_Z, every 2nd a zoom step lower, every
    4th two lower ..., never below the kind's SHIELD_MIN_Z."""
    z = SHIELD_MAX_Z
    while k % 2 == 0 and z - 1 >= SHIELD_MIN_Z[kind]:
        z -= 1
        k //= 2
        if k == 0:
            z = SHIELD_MIN_Z[kind]
            break
    return z


def _lines_of(geom):
    if geom["type"] == "LineString":
        return [geom["coordinates"]]
    if geom["type"] == "MultiLineString":
        return geom["coordinates"]
    return []


def build(features):
    routes = {}                 # (kind, number) -> [piece]
    for f in features:
        p = f.get("properties") or {}
        kind = KINDS.get(p.get("level"))
        num = (p.get("name") or "").strip()
        if p.get("sov_a3") != "USA" or not kind or p.get("featurecla") != "Road" or not num or not f.get("geometry"):
            continue
        for coords in _lines_of(f["geometry"]):
            routes.setdefault((kind, num), []).append([(float(c[0]), float(c[1])) for c in coords])
    out = {"interstates": [], "us": [], "shields": []}
    shields = []
    for (kind, num), pieces in sorted(routes.items()):
        # Natural Earth splits a route at its junctions (pieces meet mid-line, not end to end): the shields are
        # counted over the whole route, longest piece first, so its pieces do not each start a shield at the
        # lowest zoom
        base = 0
        for line in sorted((simplify(l, SIMPLIFY_M) for l in join(pieces)), key=length_km, reverse=True):
            flat = []
            for lo, la in line:
                flat += [round(lo, DECIMALS), round(la, DECIMALS)]
            out["interstates" if kind == "I" else "us"].append(flat)
            if length_km(line) < SHIELD_KM / 2:
                continue
            pts = shield_points(line, SHIELD_KM)
            for lo, la, k in pts:
                shields.append([num, round(lo, DECIMALS), round(la, DECIMALS), shield_min_zoom(base + k, tier(kind, num)), kind])
            base += len(pts)
    # the map's label pass keeps the first of overlapping shields: the lowest zoom first, mainline Interstates, then
    # their spurs and loops, then US routes
    shields.sort(key=lambda s: (s[3], TIER_ORDER[tier(s[4], s[0])], s[0], s[1], s[2]))
    out["shields"] = shields
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", help="Natural Earth ne_10m_roads.geojson (default: download v5.1.2)")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    if args.src:
        with open(args.src, encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        print("downloading", NE_URL, file=sys.stderr)
        req = urllib.request.Request(NE_URL, headers={"User-Agent": "BeamNG.Earth build_map_highways"})
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read().decode("utf-8"))
    out = build(data["features"])
    body = json.dumps(out, separators=(",", ":"))
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("// Natural Earth (public domain, naturalearthdata.com) v5.1.2: 1:10m roads, the US Interstates and US "
                 "routes,\n// with route shields along them. Built by tools/build_map_highways.py (see CREDITS.md).\n")
        fh.write("export default " + body + "\n")
    nv = sum(len(l) // 2 for l in out["interstates"] + out["us"])
    print(f"{args.out}: {len(out['interstates'])} Interstate lines, {len(out['us'])} US route lines, {nv} vertices, "
          f"{len(out['shields'])} shields, {len(body) / 1e6:.2f} MB", file=sys.stderr)


if __name__ == "__main__":
    main()
