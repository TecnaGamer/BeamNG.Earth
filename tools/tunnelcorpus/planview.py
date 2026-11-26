"""Plan views of the tunnel corpus's mouths: aerial | LABEL_A | LABEL_B (north up, HALF m round the real portal), from
the builds run.py kept (KEEP_TILES): the physics terrain shaded by height with its holes red, the drawn roads grey,
the tunnel meshes (floor yellow, the tube's roof and walls blue, translucent), the mapped tunnel lines dashed, the
real portal (refs.json) a white ring, the built portals (the audit's PORTAL lines) magenta.

    python3 tools/tunnelcorpus/planview.py OUT_DIR LABEL_A LABEL_B [--ids a,b] [--half 60]

The aerial comes from tools/ortho_hi.py (cached in ROOT/aerial)."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
TREE = HERE.parents[1]
sys.path[:0] = [str(TREE / "tools/widecorpus"), str(TREE / "server")]
os.environ.setdefault("WIDE_CORPUS_ROOT", str(Path.home() / "bnge-tests/tunnelcorpus"))
import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(os.environ.get("TUNNEL_CORPUS_ROOT") or Path.home() / "bnge-tests/tunnelcorpus")
PX = 0.25


def tile_scene(p, directory):
    """[(what, triangles (k, 3, 3))], terrains [(Terrain)], lines [(level, xy)] in the place's frame."""
    import checks
    import geodesy
    import geotiles
    from common import read
    frame = geodesy.EnuFrame(p["lat"], p["lon"])
    tris, ters, lines = [], [], []
    for tile in read(directory / "tiles.json"):
        key = tile["key"]
        ctx = read(directory / (key + ".source.json"))
        ters.append(checks.Terrain(directory / (key + ".ter"), ctx["block"]))
        lines += [(r["level"], np.asarray(r["xy"])) for r in ctx["lines"]]
        src = geotiles.frame(*geotiles.parse(key)[1:])
        for file, sf in ((f"rg_{key}.json", src), (f"rs_1_{key}.json", None)):
            data = read(directory / file)
            for what, entry in data.items():
                if not isinstance(entry, dict) or "shape" not in entry:
                    continue
                if what not in ("road", "deck", "tunnel", "walk"):
                    continue
                for mat, tri in checks.groups(directory, entry, frame, sf, tile["exact"].get(what)):
                    tris.append((what, mat, np.asarray(tri, float)))
    return tris, ters, lines


def render(p, directory, portals, half):
    n = int(2 * half / PX)
    img = np.zeros((n, n, 3), float)
    have = np.zeros((n, n), bool)
    xs = (np.arange(n) + 0.5) * PX - half
    gx, gy = np.meshgrid(xs, -xs)          # row 0 = north
    if not directory.is_dir():
        im = Image.new("RGB", (n, n), (40, 40, 40))
        ImageDraw.Draw(im).text((10, 10), "not built", fill=(255, 255, 255))
        return im
    tris, ters, lines = tile_scene(p, directory)
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    z = np.full(len(pts), np.nan)
    hole = np.zeros(len(pts), bool)
    for t in ters:
        zz, mat = t.sample(pts)
        ok = np.isfinite(zz)
        z = np.where(ok & ~np.isfinite(z), zz, z)
        local = pts - t.origin
        inb = ((local >= 0) & (local < len(t.z) - 1)).all(1)
        hole |= inb & (mat == 255)
    zf = z.reshape(n, n)
    if np.isfinite(zf).any():
        lo, hi = np.nanpercentile(zf, 2), np.nanpercentile(zf, 98)
        dzdx = np.gradient(np.nan_to_num(zf, nan=lo), axis=1)
        dzdy = np.gradient(np.nan_to_num(zf, nan=lo), axis=0)
        shade = np.clip(0.6 + 1.5 * (dzdy - dzdx), 0.2, 1.0)
        v = np.clip((zf - lo) / max(hi - lo, 1e-3), 0, 1)
        base = np.stack([0.35 + 0.35 * v, 0.45 + 0.25 * v, 0.30 + 0.2 * v], -1) * shade[..., None]
        img = np.where(np.isfinite(zf)[..., None], base, img)
    hm = hole.reshape(n, n)
    img[hm] = [0.85, 0.1, 0.1]

    def fill(tri, color, alpha):
        for t in tri:
            x, y = t[:, 0], t[:, 1]
            c = ((x + half) / PX, (half - y) / PX)
            c0, c1 = int(max(0, np.floor(c[0].min()))), int(min(n - 1, np.ceil(c[0].max())))
            r0, r1 = int(max(0, np.floor(c[1].min()))), int(min(n - 1, np.ceil(c[1].max())))
            if c1 < c0 or r1 < r0:
                continue
            X, Y = np.meshgrid(np.arange(c0, c1 + 1) + 0.5, np.arange(r0, r1 + 1) + 0.5)
            (ax, ay), (bx, by), (cx, cy) = zip(c[0], c[1])
            d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if abs(d) < 1e-9:
                continue
            w1 = ((by - cy) * (X - cx) + (cx - bx) * (Y - cy)) / d
            w2 = ((cy - ay) * (X - cx) + (ax - cx) * (Y - cy)) / d
            m = (w1 >= 0) & (w2 >= 0) & (w1 + w2 <= 1)
            sub = img[r0:r1 + 1, c0:c1 + 1]
            sub[m] = sub[m] * (1 - alpha) + np.asarray(color) * alpha
    for what, mat, tri in tris:
        if not len(tri):
            continue
        nz = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        up = nz[:, 2] > 0.45 * np.linalg.norm(nz, axis=1)
        if what == "road":
            fill(tri[up], (0.55, 0.55, 0.55), 0.8)
        elif what == "deck":
            fill(tri[up], (0.75, 0.6, 0.45), 0.8)
        elif what == "tunnel":
            fill(tri[up], (0.95, 0.85, 0.2), 0.7)
            fill(tri[~up], (0.25, 0.45, 0.95), 0.25)
    im = Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8))
    dr = ImageDraw.Draw(im)
    toq = lambda x, y: ((x + half) / PX, (half - y) / PX)     # noqa: E731
    for level, xy in lines:
        if level != "tunnel" or len(xy) < 2:
            continue
        q = [toq(*v) for v in xy]
        for a, b in zip(q[:-1], q[1:]):
            dr.line([a, b], fill=(255, 255, 255), width=1)
    cx, cy = toq(0, 0)
    dr.ellipse([cx - 10, cy - 10, cx + 10, cy + 10], outline=(255, 255, 255), width=2)
    for x, y in portals:
        px, py = toq(x, y)
        dr.ellipse([px - 6, py - 6, px + 6, py + 6], outline=(255, 0, 255), width=3)
    return im


def aerial(p, half):
    out = ROOT / "aerial" / f"{p['id']}_{int(half)}.jpg"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, str(TREE / "tools/ortho_hi.py"), str(p["lat"]), str(p["lon"]), str(half), str(out)],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    n = int(2 * half / PX)
    if out.exists():
        return Image.open(out).convert("RGB").resize((n, n))
    return Image.new("RGB", (n, n), (30, 30, 30))


def portals_of(pid, label):
    f = ROOT / "run" / pid / (label + ".portals.txt")
    out = []
    if f.exists():
        for ln in f.read_text().splitlines():
            w = ln.split()
            if w and w[0] == "PORTAL":
                out.append((float(w[1]), float(w[2])))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("labels", nargs="+")
    ap.add_argument("--ids")
    ap.add_argument("--half", type=float, default=60.0)
    a = ap.parse_args()
    rows = json.loads((HERE / "refs.json").read_text())["mouths"]
    places = [dict(id="tun-" + r["id"], lat=r["lat"], lon=r["lon"], style=r.get("style_class")) for r in rows]
    if a.ids:
        want = set(a.ids.split(","))
        places = [p for p in places if p["id"] in want or p["id"][4:] in want]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for p in places:
        panels = [aerial(p, a.half)]
        for lab in a.labels:
            panels.append(render(p, ROOT / "tiles" / p["id"] / lab, portals_of(p["id"], lab), a.half))
        w, h = panels[0].size
        sheet = Image.new("RGB", (w * len(panels), h + 22), (0, 0, 0))
        dr = ImageDraw.Draw(sheet)
        for i, (im, name) in enumerate(zip(panels, ["aerial"] + a.labels)):
            sheet.paste(im, (i * w, 22))
            dr.text((i * w + 6, 4), f"{p['id'][4:]}  {name}", fill=(255, 255, 255))
        sheet.save(out / f"{p['id'][4:]}.jpg", quality=88)
        print(out / f"{p['id'][4:]}.jpg", flush=True)


if __name__ == "__main__":
    main()
