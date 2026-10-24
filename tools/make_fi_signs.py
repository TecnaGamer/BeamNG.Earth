"""Make the Finnish speed-limit sign models (the Finnish fork): a round sign (yellow, red ring, black numbers: Finland's
C32 nopeusrajoitus) 640 mm across on a single grey pole, one model and texture per limit, into the mod's
art/shapes/beamngEarth/signs_fi/ with their materials. The models face their local +y (as the game's stock signs:
a -y face showed its back to the traffic in the game).

    python tools/make_fi_signs.py
"""
import json
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import dae  # noqa: E402

OUT = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth", "signs_fi")
VFS = "/levels/beamng_earth/art/shapes/beamngEarth/signs_fi"
LIMITS = (20, 30, 40, 50, 60, 70, 80, 90, 100, 120)
TEX = 512
R = 0.40                # m: the sign's radius (800 mm: main roads' signs looked small at 640, 2026-10-24)
CENTRE_Z = 2.6          # m: its centre over the ground
POLE_R = 0.038          # m: a 76 mm pole ...
POLE_Y = -0.07          # ... behind the sign as the game shows it (it imports the model mirrored: +0.07 stood in
                        # front of the face, 2026-10-24)
SIDES = 32
YELLOW, RED, BLACK, GREY = (255, 204, 0), (210, 30, 30), (20, 20, 20), (150, 152, 155)
# the texture: the sign face in the left 3/4 square (u 0..0.75), a grey strip at the right (the pole, the sign's back)
FACE_U = 0.75


def font(px):
    """A DIN-like condensed bold (Finnish signs' lettering): Bahnschrift's Bold Condensed where Windows has it."""
    try:
        f = ImageFont.truetype("C:/Windows/Fonts/bahnschrift.ttf", px)
        f.set_variation_by_name("Bold Condensed")
        return f
    except (OSError, ValueError):
        pass
    for name in ("DejaVuSansCondensed-Bold.ttf", "arialbd.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default(px)


def texture(limit, path):
    img = Image.new("RGB", (TEX, TEX), GREY)
    d = ImageDraw.Draw(img)
    s = int(TEX * FACE_U)
    d.rectangle([0, 0, s, TEX], fill=YELLOW)        # (bleed past the disc's edge)
    c, r = s / 2, s / 2
    d.ellipse([c - r, c - r, c + r, c + r], fill=RED)
    ri = r * (1 - 0.19)                             # the red ring: about a fifth of the radius
    d.ellipse([c - ri, c - ri, c + ri, c + ri], fill=YELLOW)
    text = str(limit)
    f = font(int(s * (0.62 if limit < 100 else 0.56)))
    box = d.textbbox((0, 0), text, font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    if w > 2 * ri * 0.86:                           # three digits: kept inside the ring
        f = font(int(s * 0.56 * 2 * ri * 0.86 / w))
        box = d.textbbox((0, 0), text, font=f)
        w, h = box[2] - box[0], box[3] - box[1]
    d.text((c - w / 2 - box[0], c - h / 2 - box[1]), text, fill=BLACK, font=f)
    img.save(path)


def mesh():
    """(positions, uvs, tris, normals) flat: the face (-y), the back (+y), the pole."""
    P, UV, N, T = [], [], [], []

    def vert(p, uv, n):
        P.extend(p)
        UV.extend(uv)
        N.extend(n)
        return len(P) // 3 - 1

    grey = (0.9, 0.5)
    h = FACE_U / 2                  # the face's radius in the texture; its centre (h, 1 - h): COLLADA's v runs up
    for face, y, ny in ((True, -0.03, -1.0), (False, -0.025, 1.0)):
        c = vert((0.0, y, CENTRE_Z), (h, 1 - h) if face else grey, (0.0, ny, 0.0))
        ring = []
        for k in range(SIDES):
            a = 2 * math.pi * k / SIDES
            x, z = R * math.cos(a), R * math.sin(a)
            # u against x, v with z: what reads right in the game (found by trying: it showed the numbers upside
            # down, mirrored, then upside down again)
            uv = (h - h * math.cos(a), 1 - h + h * math.sin(a)) if face else grey
            ring.append(vert((x, y, CENTRE_Z + z), uv, (0.0, ny, 0.0)))
        for k in range(SIDES):
            a, b = ring[k], ring[(k + 1) % SIDES]
            T.extend((c, b, a) if face else (c, a, b))   # counter-clockwise seen from the side it faces
    top = CENTRE_Z + R * 0.6
    for k in range(8):                                  # the pole, behind the sign
        a0, a1 = 2 * math.pi * k / 8, 2 * math.pi * (k + 1) / 8
        n0, n1 = (math.cos(a0), math.sin(a0)), (math.cos(a1), math.sin(a1))
        q = []
        for (cx, cy), z in ((n0, 0.0), (n1, 0.0), (n1, top), (n0, top)):
            q.append(vert((POLE_R * cx, POLE_Y + POLE_R * cy, z), grey, (cx, cy, 0.0)))
        T.extend((q[0], q[1], q[2], q[0], q[2], q[3]))
    # turned half a turn about the pole: the face to +y (in the game the -y face showed its back to the traffic,
    # 2026-10-24); a rotation keeps the numbers reading right, a mirror would not
    for arr in (P, N):
        for i in range(0, len(arr), 3):
            arr[i], arr[i + 1] = -arr[i], -arr[i + 1]
    return P, UV, T, N


def main():
    os.makedirs(OUT, exist_ok=True)
    P, UV, T, N = mesh()
    mats = {}
    for n in LIMITS:
        name = f"bne_fi_speed{n}"
        texture(n, os.path.join(OUT, f"{name}.color.png"))
        dae.write_grid_dae(os.path.join(OUT, f"fi_speed{n}.dae"), P, UV, T, material=name, normals=N)
        mats[name] = {"name": name, "mapTo": name, "class": "Material", "version": 1.5,
                      "Stages": [{"baseColorMap": f"{VFS}/{name}.color.png", "roughnessFactor": 0.5}, {}, {}, {}],
                      "groundType": "METAL", "translucent": False}
    with open(os.path.join(OUT, "main.materials.json"), "w", newline="\n") as f:
        json.dump(mats, f, indent=2)
    print(f"{len(LIMITS)} signs in {OUT}")


if __name__ == "__main__":
    main()
