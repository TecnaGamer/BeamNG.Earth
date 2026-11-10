"""Render the actual generated house meshes/textures outside BeamNG (CPU z-buffer).

This is a geometry/material preview, not a screenshot of an in-game test.
"""
import json
import math
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import buildings as b
import francehouses as fr

ASSETS = ROOT / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/fr_houses"
TEXTURES = {k: np.asarray(Image.open(ASSETS / (k + ".color.png")).convert("RGB"), float) / 255.
            for k in ("stucco", "stone", "brick", "wood", "roller", "tile", "canal", "slate")}


def render(mesh, size=(500, 360)):
    P, UV, T, M, mats, C = mesh[:6]
    C = C.astype(float)[:, :3] / 255
    center = np.array([float(P[:, 0].min() + P[:, 0].max()) / 2,
                       float(P[:, 1].min() + P[:, 1].max()) / 2, float(P[:, 2].max()) / 2])
    camera = np.array([.65, -.85, .60]);camera /= np.linalg.norm(camera)
    right = np.cross([0., 0., 1.], camera); right /= np.linalg.norm(right)
    up = np.cross(camera, right)
    axes = np.array([right, up, camera]).T
    width, height = size
    scale = min(width / 20., height / 17.)
    ground = np.array([[-6, -8, 0], [18, -8, 0], [18, 18, 0], [-6, 18, 0]], float)
    P = np.concatenate([P, ground]); UV = np.concatenate([UV, np.zeros((4, 2))])
    C = np.concatenate([C, np.full((4, 3), .94)])
    n = len(P) - 4
    T = np.concatenate([T, [[n, n + 1, n + 2], [n, n + 2, n + 3]]])
    M = np.concatenate([M, [-1, -1]])
    q = (P - center) @ axes
    screen = np.c_[width / 2 + q[:, 0] * scale, height / 2 - q[:, 1] * scale]
    image = np.full((height, width, 3), .985)
    depth = np.full((height, width), -1e9)
    light = np.array([-.4, -.55, 1.]); light /= np.linalg.norm(light)
    shadow = Image.new("L", size, 0)
    sd = ImageDraw.Draw(shadow)
    for tri, mid in zip(T, M):
        if mid < 0 or np.max(P[tri, 2]) < .05:
            continue
        proj = P[tri].copy()
        proj[:, :2] -= proj[:, 2, None] * light[None, :2] / light[2]
        proj[:, 2] = .002
        view = (proj - center) @ axes
        sd.polygon(list(zip(width / 2 + view[:, 0] * scale, height / 2 - view[:, 1] * scale)), fill=255)
    shadow = np.asarray(shadow.filter(ImageFilter.GaussianBlur(2.0)), float) / 255.
    for tri, mid in zip(T, M):
        pp, ss = P[tri], screen[tri]
        normal = np.cross(pp[1] - pp[0], pp[2] - pp[0])
        length = np.linalg.norm(normal)
        if length < 1e-9 or np.dot(normal, camera) <= 0:
            continue
        normal /= length
        lo = np.maximum(np.floor(ss.min(axis=0)).astype(int), 0)
        hi = np.minimum(np.ceil(ss.max(axis=0)).astype(int), [width - 1, height - 1])
        if (hi < lo).any():
            continue
        yy, xx = np.mgrid[lo[1]:hi[1] + 1, lo[0]:hi[0] + 1]
        a, bb, cc = ss
        den = (bb[1] - cc[1]) * (a[0] - cc[0]) + (cc[0] - bb[0]) * (a[1] - cc[1])
        if abs(den) < 1e-9:
            continue
        w0 = ((bb[1] - cc[1]) * (xx + .5 - cc[0]) + (cc[0] - bb[0]) * (yy + .5 - cc[1])) / den
        w1 = ((cc[1] - a[1]) * (xx + .5 - cc[0]) + (a[0] - cc[0]) * (yy + .5 - cc[1])) / den
        w2 = 1 - w0 - w1
        z = w0 * q[tri[0], 2] + w1 * q[tri[1], 2] + w2 * q[tri[2], 2]
        mask = (w0 >= -1e-7) & (w1 >= -1e-7) & (w2 >= -1e-7) & (z > depth[yy, xx] + 1e-7)
        if not mask.any():
            continue
        weights = np.stack([w0, w1, w2], axis=-1)
        color = weights @ C[tri]
        if mid >= 0:
            kind = mats[int(mid)].removeprefix("bne_fr_house_")
            if kind in TEXTURES:
                tex = TEXTURES[kind]
                texuv = weights @ UV[tri]
                tx = (np.floor(texuv[:, :, 0] * tex.shape[1]).astype(int)) % tex.shape[1]
                ty = (np.floor((1 - texuv[:, :, 1]) * tex.shape[0]).astype(int)) % tex.shape[0]
                color *= tex[ty, tx]
            color *= .59 + .41 * max(0., np.dot(normal, light))
            if kind.startswith("glass"):
                color += .11
        else:
            color *= 1 - shadow[yy, xx, None] * .22
        image[yy[mask], xx[mask]] = color[mask]
        depth[yy[mask], xx[mask]] = z[mask]
    return Image.fromarray(np.clip(image * 255, 0, 255).astype(np.uint8))


CASES = [
    ("Alsace · enduit et volets roulants", 48.77, 7.85, 150, "render"),
    ("Alsace · toiture en demi-croupe", 48.77, 7.85, 150, "halfhip"),
    ("Alsace · colombages", 48.77, 7.85, 150, "timber"),
    ("Bretagne · pierre et ardoise", 48.39, -4.49, 30, "granite"),
    ("Normandie · pans de bois", 49.15, .23, 120, "timber"),
    ("Nord · brique sombre", 50.63, 3.06, 30, "brick"),
    ("Bassin parisien · meulière", 48.84, 2.10, 100, "meuliere"),
    ("Toulouse · brique et tuiles", 43.60, 1.44, 140, "brick"),
    ("Pays basque · bois et enduit blanc", 43.48, -1.55, 30, "basque_frame"),
    ("Alpes · partie haute en bois", 45.92, 6.87, 1030, "chalet"),
    ("Pyrénées · pierre et toiture sombre", 43.30, -.37, 200, "stone"),
    ("Méditerranée · enduit et tuiles canal", 43.61, 3.88, 60, "render"),
    ("Loire · pierre claire et volets", 47.39, .69, 60, "limestone"),
    ("Sud-Ouest · maison enduite", 44.02, 1.35, 90, "render"),
    ("Est · enduit clair", 48.69, 6.18, 210, "render"),
    ("Centre · maison en pierre", 46.13, 3.46, 300, "stone"),
]


def build(output):
    fr.ON = True
    font = ROOT / "tools/fonts/DejaVuSans-Bold.ttf"
    titlefont = ImageFont.truetype(str(font), 34)
    textfont = ImageFont.truetype(str(font), 18)
    smallfont = ImageFont.truetype(str(font), 16)
    width, cell_h, head = 2048, 410, 136
    sheet = Image.new("RGB", (width, head + 4 * cell_h + 70), "#f4f3ef")
    d = ImageDraw.Draw(sheet)
    d.rectangle((0, 0, width, head - 1), fill="#17252c")
    d.text((28, 22), "Maisons françaises · variantes régionales", font=titlefont, fill="#ffffff")
    d.text((30, 78), "Aperçu des géométries et textures générées hors jeu — apparences représentatives", font=textfont, fill="#c9d7d9")
    results = []
    for i, (label, lat, lon, alt, family) in enumerate(CASES):
        chosen = None
        w, depth, floors = [(11, 9, 2), (10, 11, 2), (8, 12, 2), (13, 7, 1),
                            (14, 8, 2), (6, 11, 2), (10, 10, 2), (12, 8, 2),
                            (12, 10, 2), (10, 9, 2), (14, 7, 2), (11, 10, 1),
                            (13, 7, 1), (12, 8, 1), (10, 9, 2), (12, 8, 2)][i]
        for seed in range(i * 7, i * 7 + 400):
            spec = b.plan(np.array([[0, 0], [w, 0], [w, depth], [0, depth]], float),
                          {"class": "house", "num_floors": floors, "seed_id": str(seed), "_fr_altitude": alt}, lat=lat, lon=lon)
            if spec.fr_house and spec.fr_house["family"] == family:
                if i == 0 and (not spec.fr_house["roller"] or not any(c in spec.fr_house["wall"] for c in ("0.790_0.880_0.950", "0.970_0.830_0.850"))):
                    continue
                if i == 11 and spec.roof != "hipped":
                    continue
                chosen = spec
                break
        if chosen is None:
            raise ValueError("No preview model for " + label)
        image = render(b.build([(chosen, 0., 0.)]))
        x, y = (i % 4) * 512, head + (i // 4) * cell_h
        sheet.paste(image, (x + 6, y + 4))
        d.text((x + 18, y + 365), label, font=textfont, fill="#20323a")
        results.append((label, chosen.fr_house["family"], chosen.roof))
    d.text((28, head + 4 * cell_h + 18), "Les données de hauteur, couleur et toiture connues restent prioritaires. Les immeubles et autres bâtiments sont exclus.", font=smallfont, fill="#4c5a5e")
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output)
    return results


if __name__ == "__main__":
    target = ROOT / "docs/france_houses_preview.png"
    print(json.dumps(build(target), ensure_ascii=False))
    print(target)
