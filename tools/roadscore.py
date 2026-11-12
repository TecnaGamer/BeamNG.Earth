"""Score the roads we generate at a place against a high-resolution aerial photo (the user, 2026-11-12: "Use high res
satellite images to compare and score our generation to real life. And also check places with high quality accurate
osm road data and low quality and less accurate road data").

    python3 tools/roadscore.py LAT LON [--half 300] [--px 2048] [--source auto] [--out DIR] [--cache DIR]
    python3 tools/roadscore.py --places FILE [...]          (one "name lat lon [half]" a line; a summary table)

What is compared: the drawn road of each map road on the ground (the map's line after the server's own smoothing,
roads.road_specs' width, kind and lanes: what the road tiles paint into the ground and mesh), its on-ground pieces by
the map's bridge / tunnel tags (roads.quick_ground_parts), with the photo of tools/ortho_hi.py (the finest public one
there: Maryland 3-inch, the regional sources, NAIP in the US). Nothing needs the game or the DEM.

Per WINDOW_M of road (a straight stretch), the photo's cross-section is sampled at every STATION_M along it, from
-(w/2 + MARGIN_M) to +(w/2 + MARGIN_M) m off our centre line, and the median across the stations taken (cars, shadows
and dashes average out). In that profile:
- edges: the pair of steps in brightness / colour (smoothed gradient maxima) that best frames a band 0.55-1.9 x our
  width with its centre within max(4 m, 0.6 w) of ours, chosen by the weaker edge's strength times the band's contrast
  with 2 m beyond each edge; kept when that edge stands SNR_MIN x over the profile's median gradient.
  -> centre offset (m, + = left of the road's direction) and visible width (m) against ours;
- markings: narrow bright peaks (<= 0.6 m, >= MARK_DL over a 2.5 m running median) of the 80th-percentile profile
  inside the band: lines across the road, so lanes ~ interior lines + 1 where 1-6 interior lines are evenly spaced
  2.4-5 m apart;
- material: the band's median brightness, saturation and excess green (and their spread). Each road's verdict is the
  nearest of this photo's own references by those numbers (the roads the map says are paved, motorway to tertiary
  or a paved surface tag; the ones tagged unpaved / gravel), so a photo's colour cast does not decide; with too few
  references photosurface's fixed rules (tuned on Czechia) decide, or nothing.

Outputs in DIR (default ~/bnge-tests/roads/score/<lat>_<lon>/): photo.jpg, overlay.jpg (our edges cyan, the photo's
edges found magenta, roads whose material verdict disagrees red), roads.json (per road), summary.json, and the summary
printed. Scores are of the detectable windows only (trees, shade and wide junction areas leave windows undetected;
the detection rate is reported with them).
"""
import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server"))

import numpy as np  # noqa: E402

STATION_M = 2.0         # m between the cross-sections of a window
WINDOW_M = 20.0         # m of road per window
DU = 0.2                # m between samples across
MARGIN_M = 9.0          # m sampled beyond our half width either side
MAX_TURN_DEG = 15.0     # a window turning more is not a straight stretch: skipped
SNR_MIN = 2.2           # the weaker edge's gradient over the profile's median gradient
MARK_DL = 14.0          # brightness (0-255) of a marking over its surroundings
PAVED_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary"}
UNPAVED_KINDS = {"gravel", "dirt"}


def _smooth(x, sigma_px):
    """Gaussian smoothing of the last axis."""
    r = max(1, int(3 * sigma_px))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma_px) ** 2)
    k /= k.sum()
    pad = np.pad(x, [(0, 0)] * (x.ndim - 1) + [(r, r)], mode="edge")
    out = np.empty_like(x, dtype=np.float64)
    it = np.nditer(np.zeros(x.shape[:-1]), flags=["multi_index"]) if x.ndim > 1 else None
    if it is None:
        return np.convolve(pad, k, mode="valid")
    for _ in it:
        out[it.multi_index] = np.convolve(pad[it.multi_index], k, mode="valid")
    return out


class Photo:
    """An aerial picture of a ground square 2 * half m round (lat, lon), north up: bilinear samples at ENU points."""

    def __init__(self, img, half):
        self.a = np.asarray(img.convert("RGB"), np.float32)
        self.half = float(half)
        self.h, self.w = self.a.shape[:2]
        self.s = 2.0 * self.half / self.w       # ground m per pixel

    def sample(self, e, n):
        """RGB (..., 3) at ENU points e, n (same shapes); NaN outside the picture."""
        c = (np.asarray(e) + self.half) / self.s - 0.5
        r = (self.half - np.asarray(n)) / self.s - 0.5
        ok = (c >= 0) & (c <= self.w - 1.001) & (r >= 0) & (r <= self.h - 1.001)
        c0 = np.clip(np.floor(c).astype(np.int64), 0, self.w - 2)
        r0 = np.clip(np.floor(r).astype(np.int64), 0, self.h - 2)
        fc, fr = (np.clip(c - c0, 0, 1))[..., None], (np.clip(r - r0, 0, 1))[..., None]
        a = self.a
        v = (a[r0, c0] * (1 - fc) * (1 - fr) + a[r0, c0 + 1] * fc * (1 - fr) + a[r0 + 1, c0] * (1 - fc) * fr
             + a[r0 + 1, c0 + 1] * fc * fr)
        v[~ok] = np.nan
        return v


def colour_feats(rgb):
    """(lum, sat, exg, chroma1, chroma2) of RGB samples (..., 3)."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    mx, mn = np.maximum(np.maximum(r, g), b), np.minimum(np.minimum(r, g), b)
    sat = (mx - mn) / np.maximum(mx, 1.0)
    exg = (2 * g - r - b) / np.maximum(r + g + b, 1.0)
    return lum, sat, exg, r - g, 0.5 * (r + g) - b


def resample(xy, step):
    """Points every step m along an ENU polyline (k, 2), and the distance of each."""
    seg = np.diff(xy, axis=0)
    ln = np.hypot(seg[:, 0], seg[:, 1])
    keep = ln > 1e-6
    if not keep.any():
        return np.zeros((0, 2)), np.zeros(0)
    xy = np.vstack([xy[:1], xy[1:][keep]])
    seg, ln = seg[keep], ln[keep]
    cum = np.r_[0.0, np.cumsum(ln)]
    s = np.arange(0.0, cum[-1] + 1e-9, step)
    i = np.clip(np.searchsorted(cum, s, side="right") - 1, 0, len(ln) - 1)
    t = ((s - cum[i]) / ln[i])[:, None]
    return xy[i] + seg[i] * t, s


def window_profiles(photo, xy, half_w):
    """[(centre (2,), normal (2,), u (m,), median rgb (m, 3), p80 lum (m,), stations)] of a road's ENU line xy, one per
    straight WINDOW_M stretch."""
    pts, _ = resample(xy, STATION_M)
    per = int(round(WINDOW_M / STATION_M))
    out = []
    U = half_w + MARGIN_M
    u = np.arange(-U, U + 1e-9, DU)
    for k in range(0, len(pts) - per, per):
        p = pts[k:k + per + 1]
        d = p[-1] - p[0]
        L = math.hypot(*d)
        if L < 0.8 * WINDOW_M:
            continue
        t = d / L
        seg = np.diff(p, axis=0)
        ang = np.degrees(np.arctan2(seg[:, 1], seg[:, 0]))
        dang = (ang - ang[0] + 180) % 360 - 180
        if np.abs(dang).max() > MAX_TURN_DEG:
            continue
        nrm = np.array([-t[1], t[0]])          # left of the direction
        E = p[:, 0][:, None] + u[None] * nrm[0]
        N = p[:, 1][:, None] + u[None] * nrm[1]
        rgb = photo.sample(E, N)                # (stations, m, 3)
        if np.isnan(rgb).any():
            continue
        med = np.median(rgb, axis=0)
        lum = colour_feats(rgb)[0]
        p80 = np.percentile(lum, 80, axis=0)
        out.append((p.mean(0), nrm, u, med, p80, rgb))
    return out


def find_edges(u, med, w):
    """(a, b, snr, contrast) of the band framed by the profile's strongest edge pair (see the module doc), or None."""
    lum, sat, exg, c1, c2 = colour_feats(med)
    sig = 0.35 / DU
    feats = np.stack([lum, 120 * sat, 150 * exg, 0.6 * c1, 0.6 * c2])
    sm = _smooth(feats, sig)
    g = np.gradient(sm, DU, axis=1)
    G = np.sqrt((g ** 2).sum(0))
    base = float(np.median(G)) + 1e-6
    # local maxima
    m = (G[1:-1] > G[:-2]) & (G[1:-1] >= G[2:])
    cand = np.flatnonzero(m) + 1
    cand = cand[G[cand] > 1.3 * base]
    if len(cand) < 2:
        return None
    best = None
    lo_w, hi_w = max(2.2, 0.55 * w), max(4.0, 1.9 * w)
    cmax = max(4.0, 0.6 * w)
    ua = u[cand]
    for i in range(len(cand)):
        for j in range(i + 1, len(cand)):
            a, b = ua[i], ua[j]
            wd = b - a
            if wd < lo_w or wd > hi_w or abs(0.5 * (a + b)) > cmax:
                continue
            ins = (u > a + 0.3) & (u < b - 0.3)
            outl = (u < a - 0.3) & (u > a - 2.3)
            outr = (u > b + 0.3) & (u < b + 2.3)
            if ins.sum() < 5 or outl.sum() < 3 or outr.sum() < 3:
                continue
            fi = np.median(sm[:, ins], axis=1)
            con = min(np.linalg.norm(fi - np.median(sm[:, outl], axis=1)),
                      np.linalg.norm(fi - np.median(sm[:, outr], axis=1)))
            spread = float(np.median(np.abs(sm[0, ins] - fi[0])))
            score = min(G[cand[i]], G[cand[j]]) * (1.0 + con / 20.0) / (1.0 + spread / 15.0)
            if best is None or score > best[0]:
                best = (score, a, b, min(G[cand[i]], G[cand[j]]) / base, con)
    if best is None:
        return None
    if INNER_FIRST:
        # the first strong step outward from the band's middle on each side (a curb before the building line or the
        # parked cars beyond it): the pair search above favours the strongest steps, often the building fronts
        mid = 0.5 * (best[1] + best[2])
        thr = max(SNR_MIN * base, 0.3 * float(G[(u > mid - hi_w / 2) & (u < mid + hi_w / 2)].max()))
        k8 = int(round(0.8 / DU))
        # (a step, not a painted line: the colour 0.8 m either side differs; a lane line returns to the asphalt)
        stepn = np.array([np.linalg.norm(sm[:, min(c + k8, len(u) - 1)] - sm[:, max(c - k8, 0)]) for c in cand])
        # (and not a shadow's edge: the dark side the bright side's own colour scaled down)
        rgbs = _smooth(med.T.astype(np.float64), sig).T
        shade = np.zeros(len(cand), bool)
        for j, c in enumerate(cand):
            p, q = rgbs[max(c - k8, 0)], rgbs[min(c + k8, len(u) - 1)]
            lo_, hi_ = (p, q) if p.sum() < q.sum() else (q, p)
            r = (hi_ + 1.0) / (lo_ + 1.0)
            shade[j] = r.mean() > 1.5 and r.std() / r.mean() < 0.12 and lo_.mean() < 90
        strong = cand[(G[cand] >= thr) & (stepn >= 8.0) & ~shade]
        left = [x for x in u[strong] if x < mid - 0.8]
        right = [x for x in u[strong] if x > mid + 0.8]
        if left and right:
            a, b = max(left), min(right)
            if lo_w * 0.8 <= b - a <= hi_w:
                ins = (u > a + 0.3) & (u < b - 0.3)
                outl = (u < a - 0.3) & (u > a - 2.3)
                outr = (u > b + 0.3) & (u < b + 2.3)
                if ins.sum() >= 5 and outl.sum() >= 3 and outr.sum() >= 3:
                    fi = np.median(sm[:, ins], axis=1)
                    con = min(np.linalg.norm(fi - np.median(sm[:, outl], axis=1)),
                              np.linalg.norm(fi - np.median(sm[:, outr], axis=1)))
                    ga = G[np.argmin(np.abs(u - a))]
                    gb = G[np.argmin(np.abs(u - b))]
                    return a, b, min(ga, gb) / base, con
    return best[1], best[2], best[3], best[4]


INNER_FIRST = os.environ.get("RS_INNER", "1") == "1"


def find_markings(u, p80, a, b):
    """Positions (m) of narrow bright lines inside (a, b)."""
    sel = (u >= a - 0.2) & (u <= b + 0.2)
    if sel.sum() < 10:
        return []
    x = p80.copy()
    k = int(round(2.5 / DU)) | 1
    pad = np.pad(x, k // 2, mode="edge")
    base = np.array([np.median(pad[i:i + k]) for i in range(len(x))])
    d = _smooth(x - base, 0.08 / DU)
    out = []
    m = (d[1:-1] > d[:-2]) & (d[1:-1] >= d[2:]) & (d[1:-1] > MARK_DL)
    for i in np.flatnonzero(m) + 1:
        if not sel[i]:
            continue
        half = d[i] / 2
        lo = i
        while lo > 0 and d[lo] > half:
            lo -= 1
        hi = i
        while hi < len(d) - 1 and d[hi] > half:
            hi += 1
        if (hi - lo) * DU <= 0.6:
            out.append(float(u[i]))
    return out


def lanes_from(marks, a, b):
    """Lanes by the interior lines (None when they do not read as lane lines)."""
    inner = [m for m in marks if a + 1.2 < m < b - 1.2]
    if not inner or len(inner) > 6:
        return None
    pos = [a] + inner + [b]
    gaps = np.diff(pos)
    if (gaps < 2.0).any() or (gaps > 5.5).any():
        return None
    return len(inner) + 1


def load_roads(lat, lon, half, cache_dir):
    """[(fid, cls, spec, [ENU polylines on the ground], surface tag)] of the map's roads in the square, and the frame."""
    os.environ.setdefault("BNGE_CACHE_DIR", cache_dir)
    import geodesy
    import mapcells
    import mapdata
    import roads
    import surface as surfacemod
    md = mapdata.MapData(cache_dir)
    frame = geodesy.EnuFrame(lat, lon)
    dlat = (half + 50) / 110540.0
    dlon = (half + 50) / (111320.0 * math.cos(math.radians(lat)))
    keys = mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon)
    surf = surfacemod.Surface(md)
    surf.prefetch(lat, lon, half + 500)
    surf.ready(lat, lon, half + 500, 300)
    out, seen = [], set()
    for key in keys:
        f = md.cell(key, layers=("roads",)).layers.get("roads")
        if f is None or not len(f):
            continue
        specs = roads.road_specs(f, surf.urban, None)
        vals = roads._map_surface_values(f)
        inp = roads._spec_inputs(f)
        cls = f.attrs.get("class")
        for i, sp in enumerate(specs):
            fid = str(f.ids[i])
            if sp is None or fid in seen:
                continue
            seen.add(fid)
            lines = []
            for ring in roads.quick_ground_parts(f, i):
                ring = np.asarray(ring, np.float64)
                if len(ring) < 2:
                    continue
                e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                xy = np.stack([np.asarray(e, float), np.asarray(n, float)], 1)
                if (np.abs(xy) <= half).all(1).any():
                    lines.append(xy)
            if lines:
                # what the width came from (a mapped width / lane count, else the class default: the width model's
                # calibration reads the untagged roads)
                c = inp["cols"]
                meta = {"w_tag": bool(c["has_w"][i]) or bool(np.isfinite(c["osm_w"][i])),
                        "lanes_tag": bool(np.isfinite(c["osm_lanes"][i])) or int(c["over"][i]) >= 0
                        or int(c["gen"][i]) >= 0,
                        "urban": bool(sp.urban), "mountain": bool(getattr(sp, "mountain", False)),
                        "link": bool(sp.link), "parking": float(roads.parking_width(sp)),
                        "median": None if sp.median is None else float(sp.median)}
                out.append((fid, str(cls[i]) if cls is not None else sp.cls, sp, lines,
                            None if vals[i] is None else str(vals[i]), meta))
    return out, frame


def fetch_photo(lat, lon, half, px, source):
    """(PIL image, source name) of tools/ortho_hi.py's finest picture of the square, or (None, None)."""
    import ortho_hi
    import netfetch
    import imagery
    if "naipusda" not in ortho_hi.SOURCES:      # (USDA's own NAIP service: the USGS one answers errors at times)
        u = imagery.SOURCES["naipusda"].url
        ortho_hi.SOURCES["naipusda"] = ("export", u, (24.0, -125.0, 50.0, -66.0), None)
        ortho_hi.ORDER = tuple(n for n in ortho_hi.ORDER if n != "naip") + ("naipusda", "naip")
    if source == "auto":
        names = [n for n in ortho_hi.ORDER if ortho_hi.SOURCES[n][2][0] <= lat <= ortho_hi.SOURCES[n][2][2]
                 and ortho_hi.SOURCES[n][2][1] <= lon <= ortho_hi.SOURCES[n][2][3]]
    else:
        names = [source]
    from PIL import Image
    k = 1.0 / math.cos(math.radians(lat))
    cx, cy = ortho_hi.merc(lat, lon)
    f = netfetch.Fetcher()
    n = max(1, int(math.ceil(px / 2048)))         # (an export service caps one picture's size: a grid of them)
    sub = px // n
    import time

    def get(name, x0, y0, x1, y1, size):        # (a few tries: the services answer errors at times)
        for attempt in range(3):
            got = ortho_hi.fetch(f, name, x0, y0, x1, y1, size)
            if got is not None:
                return got
            time.sleep(2.0 * (attempt + 1))
        return None
    for name in names:
        if n == 1:
            img = get(name, cx - half * k, cy - half * k, cx + half * k, cy + half * k, px)
        else:
            img = Image.new("RGB", (sub * n, sub * n))
            step = 2 * half * k / n
            for j in range(n):              # rows from the north
                for i in range(n):
                    x0, y1 = cx - half * k + i * step, cy + half * k - j * step
                    part = get(name, x0, y1 - step, x0 + step, y1, sub)
                    if part is None:
                        img = None
                        break
                    img.paste(part, (i * sub, j * sub))
                if img is None:
                    break
        if img is not None:
            return img, name
    return None, None


def material_feats(rgb_windows, inner):
    """Median (lum, sat, exg, lum spread) of the road band over its windows' stations, or None."""
    vals = []
    for u, rgb in rgb_windows:
        sel = np.abs(u) <= inner
        if sel.sum() < 3:
            continue
        vals.append(rgb[:, sel].reshape(-1, 3))
    if not vals:
        return None
    v = np.concatenate(vals)
    lum, sat, exg = colour_feats(v)[:3]
    ok = lum > 55          # (deep shade / canopy)
    if ok.sum() < 30:
        return None
    lm = float(np.median(lum[ok]))
    return {"lum": lm, "sat": float(np.median(sat[ok])), "exg": float(np.median(exg[ok])),
            "spread": float(np.median(np.abs(lum[ok] - lm))), "n": int(ok.sum())}


def material_verdicts(rows):
    """Each row's photo verdict ("paved" / "unpaved" / None) and how ("refs" / "rules")."""
    import photosurface
    paved = [r["mat"] for r in rows if r["mat"] and r["ref"] == "paved"]
    unp = [r["mat"] for r in rows if r["mat"] and r["ref"] == "unpaved"]
    keys = ("lum", "sat", "exg", "spread")
    scale = {"lum": 25.0, "sat": 0.04, "exg": 0.03, "spread": 6.0}
    if len(paved) >= 3 and len(unp) >= 3:
        cp = {k: float(np.median([m[k] for m in paved])) for k in keys}
        cu = {k: float(np.median([m[k] for m in unp])) for k in keys}
        sep = math.sqrt(sum(((cp[k] - cu[k]) / scale[k]) ** 2 for k in keys))
        for r in rows:
            m = r["mat"]
            if not m:
                r["photo"] = None
                continue
            dp = math.sqrt(sum(((m[k] - cp[k]) / scale[k]) ** 2 for k in keys))
            du = math.sqrt(sum(((m[k] - cu[k]) / scale[k]) ** 2 for k in keys))
            r["photo"] = None if abs(dp - du) < 0.25 * sep else ("paved" if dp < du else "unpaved")
            r["photo_by"] = "refs"
        return {"paved_ref": cp, "unpaved_ref": cu, "n_paved_ref": len(paved), "n_unpaved_ref": len(unp)}
    # too few references of one kind: photosurface's fixed rules (tuned on one Czech photo) only with RS_RULES=1; they
    # called Amsterdam's brick streets and Münster's shaded asphalt unpaved (2026-11-12), so by default no verdict
    for r in rows:
        m = r["mat"]
        c = None
        if m and os.environ.get("RS_RULES") == "1":
            c = photosurface.classify({**m, "n": max(m["n"], photosurface.MIN_SAMPLES), "vexg": None})
        r["photo"] = {"paved": "paved", "gravel": "unpaved", "grass": "unpaved"}.get(c)
        r["photo_by"] = "rules" if c else None
    return {"n_paved_ref": len(paved), "n_unpaved_ref": len(unp)}


def score(lat, lon, half=300.0, px=2048, source="auto", out=None, cache_dir=None, quiet=False):
    """Score the place; returns the summary dict (and writes DIR's files)."""
    from PIL import Image, ImageDraw
    cache_dir = cache_dir or os.path.expanduser("~/bnge-tests/roads/cache")
    out = out or os.path.expanduser("~/bnge-tests/roads/score/%.5f_%.5f" % (lat, lon))
    os.makedirs(out, exist_ok=True)
    pic = os.path.join(out, "photo.jpg")
    src = None
    if os.path.exists(pic) and os.path.exists(os.path.join(out, "photo.src")):
        img = Image.open(pic)
        src = open(os.path.join(out, "photo.src")).read().strip()
    else:
        img, src = fetch_photo(lat, lon, half, px, source)
        if img is None:
            return {"lat": lat, "lon": lon, "error": "no photo"}
        img.save(pic, quality=92)
        open(os.path.join(out, "photo.src"), "w").write(src)
    photo = Photo(img, half)
    roads_, frame = load_roads(lat, lon, half, cache_dir)
    rows = []
    det_marks = []
    for fid, cls, sp, lines, tag, meta in roads_:
        w = float(sp.width)
        wins, mats = [], []
        for xy in lines:
            for c, nrm, u, med, p80, rgb in window_profiles(photo, xy, w / 2):
                if (np.abs(c) > half - 5).any():
                    continue
                mats.append((u, rgb))
                e = find_edges(u, med, w)
                if e is None or e[2] < SNR_MIN:
                    wins.append({"c": c.tolist(), "ok": False})
                    continue
                a, b, snr, con = e
                marks = find_markings(u, p80, a, b)
                wins.append({"c": c.tolist(), "n": nrm.tolist(), "ok": True, "off": 0.5 * (a + b), "wid": b - a,
                             "snr": snr, "con": con, "marks": marks, "lanes": lanes_from(marks, a, b), "a": a,
                             "b": b})
        if not wins:
            continue
        kind = sp.kind
        ref = None
        if tag is not None:
            import roads as roadsmod
            k2 = roadsmod.surface_kind(cls, tag)
            ref = "unpaved" if k2 in UNPAVED_KINDS else ("paved" if k2 in ("paved",) else None)
        elif cls in PAVED_CLASSES:
            ref = "paved"
        ok = [x for x in wins if x["ok"]]
        lanes_ours = sp.lanes if sp.lanes else (1 if sp.oneway else 2)
        rows.append({"fid": fid, "cls": cls, "kind": kind, "tag": tag, "ref": ref, "width": w, "lanes": sp.lanes,
                     "lanes_ours": lanes_ours, "oneway": bool(sp.oneway), **meta, "n_win": len(wins), "n_ok": len(ok),
                     "off": float(np.median([x["off"] for x in ok])) if ok else None,
                     "abs_off": float(np.median([abs(x["off"]) for x in ok])) if ok else None,
                     "wid": float(np.median([x["wid"] for x in ok])) if ok else None,
                     "lanes_photo": (int(np.median([x["lanes"] for x in ok if x["lanes"]]))
                                     if sum(1 for x in ok if x["lanes"]) >= 2 else None),
                     "mat": material_feats(mats, max(0.8, 0.3 * w)), "wins": wins,
                     "mid": lines[0][len(lines[0]) // 2].tolist()})
    refs = material_verdicts(rows)
    # per window statistics, length weighted (a window is WINDOW_M)
    allw = [x for r in rows for x in r["wins"]]
    okw = [(r, x) for r in rows for x in r["wins"] if x["ok"]]
    offs = np.array([abs(x["off"]) for _, x in okw]) if okw else np.zeros(0)
    ratio = np.array([x["wid"] / r["width"] for r, x in okw]) if okw else np.zeros(0)
    lane_rows = [r for r in rows if r["lanes_photo"] is not None and r["kind"] in ("paved",)]
    mat_rows = [r for r in rows if r["photo"] is not None]
    agree = sum(1 for r in mat_rows if (r["kind"] in UNPAVED_KINDS) == (r["photo"] == "unpaved"))
    conf = {}
    for r in mat_rows:
        k = ("ours_unpaved" if r["kind"] in UNPAVED_KINDS else "ours_paved") + "/photo_" + r["photo"]
        conf[k] = conf.get(k, 0) + 1
    by_cls = {}
    for r, x in okw:
        d = by_cls.setdefault(r["cls"], {"off": [], "ratio": []})
        d["off"].append(abs(x["off"]))
        d["ratio"].append(x["wid"] / r["width"])
    summary = {
        "lat": lat, "lon": lon, "half": half, "source": src, "m_per_px": photo.s, "roads": len(rows),
        "windows": len(allw), "detected": len(okw), "detect_rate": len(okw) / max(1, len(allw)),
        "offset_median_m": float(np.median(offs)) if len(offs) else None,
        "offset_p90_m": float(np.percentile(offs, 90)) if len(offs) else None,
        "within_1m": float((offs <= 1.0).mean()) if len(offs) else None,
        "within_2m": float((offs <= 2.0).mean()) if len(offs) else None,
        "width_ratio_median": float(np.median(ratio)) if len(ratio) else None,
        "width_ratio_iqr": [float(np.percentile(ratio, 25)), float(np.percentile(ratio, 75))] if len(ratio) else None,
        "width_within_20pct": float((np.abs(ratio - 1) <= 0.2).mean()) if len(ratio) else None,
        "lanes_checked": len(lane_rows),
        "lanes_agree": sum(1 for r in lane_rows if r["lanes_photo"] == r["lanes_ours"]),
        "lanes_photo_more": sum(1 for r in lane_rows if r["lanes_photo"] > r["lanes_ours"]),
        "lanes_photo_fewer": sum(1 for r in lane_rows if r["lanes_photo"] < r["lanes_ours"]),
        "material_checked": len(mat_rows), "material_agree": agree, "material_confusion": conf,
        # (the roads the photo was not calibrated on: no surface tag, a minor class: what the guesses decide)
        "material_untagged_checked": sum(1 for r in mat_rows if r["ref"] is None),
        "material_untagged_agree": sum(1 for r in mat_rows if r["ref"] is None
                                       and (r["kind"] in UNPAVED_KINDS) == (r["photo"] == "unpaved")),
        "material_untagged_km": round(sum(r["n_win"] for r in mat_rows if r["ref"] is None) * WINDOW_M / 1000, 2),
        "material_refs": refs,
        "by_class": {c: {"windows": len(d["off"]), "offset_median_m": round(float(np.median(d["off"])), 2),
                         "width_ratio_median": round(float(np.median(d["ratio"])), 2)} for c, d in by_cls.items()},
        "material_mismatch": [{"fid": r["fid"][-8:], "cls": r["cls"], "ours": r["kind"], "photo": r["photo"],
                               "tag": r["tag"], "at": list(frame.enu_to_geodetic(np.array([r["mid"][0]]),
                                                                                np.array([r["mid"][1]]), 0.0)[:2])}
                              for r in mat_rows if (r["kind"] in UNPAVED_KINDS) != (r["photo"] == "unpaved")],
    }
    for m in summary["material_mismatch"]:
        m["at"] = [round(float(np.asarray(m["at"][0]).ravel()[0]), 6), round(float(np.asarray(m["at"][1]).ravel()[0]), 6)]
    # overlay
    ov = img.convert("RGB").copy()
    dr = ImageDraw.Draw(ov)

    def pix(p):
        return ((p[0] + half) / photo.s, (half - p[1]) / photo.s)
    for r in rows:
        bad = r["photo"] is not None and (r["kind"] in UNPAVED_KINDS) != (r["photo"] == "unpaved")
        for x in r["wins"]:
            c = np.array(x["c"])
            if not x["ok"]:
                dr.ellipse([pix(c)[0] - 2, pix(c)[1] - 2, pix(c)[0] + 2, pix(c)[1] + 2], outline=(255, 255, 0))
                continue
            n = np.array(x["n"])
            t = np.array([n[1], -n[0]]) * WINDOW_M / 2
            for o, col in ((r["width"] / 2, (0, 255, 255)), (-r["width"] / 2, (0, 255, 255)),
                           (x["a"], (255, 0, 255)), (x["b"], (255, 0, 255))):
                p0, p1 = c + n * o - t, c + n * o + t
                dr.line([pix(p0), pix(p1)], fill=(255, 0, 0) if bad and col[0] == 0 else col, width=2)
    ov.save(os.path.join(out, "overlay.jpg"), quality=90)
    for r in rows:
        r["wins"] = [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in x.items()
                      if k in ("off", "wid", "snr", "con", "lanes", "c")} for x in r.pop("wins") if x["ok"]]
    json.dump(rows, open(os.path.join(out, "roads.json"), "w"), indent=0, default=str)
    json.dump(summary, open(os.path.join(out, "summary.json"), "w"), indent=1, default=str)
    if not quiet:
        print(json.dumps({k: v for k, v in summary.items() if k not in ("by_class", "material_mismatch")}, default=str))
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("lat", type=float, nargs="?")
    ap.add_argument("lon", type=float, nargs="?")
    ap.add_argument("--half", type=float, default=300.0)
    ap.add_argument("--px", type=int, default=2048)
    ap.add_argument("--source", default="auto")
    ap.add_argument("--out")
    ap.add_argument("--cache")
    ap.add_argument("--places")
    a = ap.parse_args()
    if a.places:
        res = []
        root = a.out or os.path.expanduser("~/bnge-tests/roads/score")
        for line in open(a.places):
            p = line.split("#")[0].split()
            if len(p) < 3:
                continue
            half = float(p[3]) if len(p) > 3 else a.half
            s = score(float(p[1]), float(p[2]), half, a.px, a.source, os.path.join(root, p[0]), a.cache, quiet=True)
            s["name"] = p[0]
            res.append(s)
            print("%-22s %s" % (p[0], line_of(s)), flush=True)
        json.dump(res, open(os.path.join(root, "places_summary.json"), "w"), indent=1, default=str)
        return
    s = score(a.lat, a.lon, a.half, a.px, a.source, a.out, a.cache)
    print(line_of(s))


def line_of(s):
    if s.get("error"):
        return s["error"]
    f = (lambda v, d=2: "-" if v is None else round(v, d))
    return ("src %-10s roads %3d det %3d/%-4d off med %s p90 %s <=1m %s <=2m %s | width x%s (iqr %s) +-20%% %s | "
            "lanes %d/%d (photo more %d fewer %d) | material %d/%d untagged %d/%d %s" % (
                s["source"], s["roads"], s["detected"], s["windows"], f(s["offset_median_m"]), f(s["offset_p90_m"]),
                f(s["within_1m"]), f(s["within_2m"]), f(s["width_ratio_median"]),
                None if s["width_ratio_iqr"] is None else [round(v, 2) for v in s["width_ratio_iqr"]],
                f(s["width_within_20pct"]), s["lanes_agree"], s["lanes_checked"], s["lanes_photo_more"],
                s["lanes_photo_fewer"], s["material_agree"], s["material_checked"], s.get("material_untagged_agree", 0),
                s.get("material_untagged_checked", 0), s["material_confusion"]))


if __name__ == "__main__":
    main()
