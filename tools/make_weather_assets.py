"""make_weather_assets.py: the weather phase B shapes and textures (levels/beamng_earth/art/weather/).

- Rain streak shells (rainstreaks.lua, the weather plan 4.3 phase B layer 2): three open cylinders around the camera
  (SHELL_R m, the camera inside, SHELL_H m high), one material each (bngEarthRainShell0..2): a tileable texture of thin
  vertical streaks used as the opacity (bngEarth_rainstreak.data.png, one channel), scrolled down by the material
  (Material 1.5 animFlags 1: defaultMat.hlsl's texCoordAnim), lit like any translucent mesh (the sun, the sky's ambient,
  the 4 strongest lights near it: headlights and lamps catch it). The streaks are TEX_W_M x TEX_H_M of the world per
  repeat, the same on each shell, so the farther shells draw finer, denser rain (the veil) and the near one single
  streaks. Instance opacity (instanceColor's alpha) sets the rain's strength.
- Lightning (storm.lua): four bolt shapes (bngEarth_bolt.data.png, a 4 x 1 atlas: a branched channel with a soft halo,
  drawn by a seeded random walk) on quads one unit high (bne_bolt1..4.cdae, the base at the origin), and a soft round
  glow (bngEarth_flashglow.data.png on bne_flashglow.cdae) for the cloud lit from inside. Emissive, instance opacity.

    python3 tools/make_weather_assets.py
    python3 tools/mod_textures_dds.py <the three PNGs it prints>

Deterministic: seeded generators (numpy's PCG64), the same bytes every run.
"""
import json
import math
import os
import sys

try:        # (first: numpy's AVX-512 kernels held back before anything imports numpy, tools/npyfirst.py)
    import npyfirst  # noqa: E402,F401
except ImportError:
    pass
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import cdae  # noqa: E402
import staging  # noqa: E402

LEVEL = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth")
OUT = os.path.join(LEVEL, "art", "weather")
REF = "/levels/beamng_earth/art/weather/"

SHELL_R = (4.5, 10.0, 22.0)     # m: the shells' radii, nearest first
SHELL_H = (-45.0, 45.0)         # m: below and above the camera (tilted by the wind and the speed: long enough)
SHELL_NEAR_R = 6.0              # m: the one-shell set's radius
SHELL_SEG = 48
TEX_W_M, TEX_H_M = 3.0, 6.0     # m of the world per texture repeat
STREAK_PX = (256, 1024)         # the streak texture (w, h)
STREAKS = 140
STREAK_LEN_PX = (22, 90)        # 0.13-0.53 m: a drop's path in a 1/60-1/30 s frame at 8 m/s
STREAK_SIGMA_PX = 0.5          # (0.75 drew wide white bars in the first broker shots)
FALL_MS = 8.0                   # m/s: the scroll (a 2 mm drop's terminal speed is ~6.5, 4 mm ~8.8)
SHELL_OPACITY = (0.7, 0.35, 0.15)       # the far shells fainter: a veil, not lines
SHELL_RGB = (0.60, 0.62, 0.66)  # a mid grey: a little lighter than dark backgrounds, a little darker than the sky
BOLT_PX = (256, 1024)           # each bolt's cell (w, h)
BOLTS = 4
GLOW_PX = 128
BOLT_NITS = 150000              # cd/m2: a channel far outshines a daylit sky (~5,000-10,000)
GLOW_NITS = 5000
VEIL_NITS = 3000                # the lit air: a dim veil by day, the whole night view lit
VEIL_R = 40.0                   # m
BOLT_RGB = (0.80, 0.86, 1.0)


def streak_texture():
    w, h = STREAK_PX
    rng = np.random.default_rng(20261125)
    a = np.zeros((h, w), np.float64)
    xs = np.arange(w) + 0.5
    for _ in range(STREAKS):
        x0 = rng.uniform(0, w)
        y0 = rng.uniform(0, h)
        ln = rng.uniform(*STREAK_LEN_PX)
        peak = rng.uniform(0.25, 0.8)
        dx = np.minimum(np.abs(xs - x0), w - np.abs(xs - x0))           # wraps across
        across = np.exp(-0.5 * (dx / STREAK_SIGMA_PX) ** 2)
        cols = across > 0.01
        for k in range(int(ln) + 1):
            v = k / ln
            along = math.sin(math.pi * v) ** 0.7 * (0.5 + 0.5 * v)    # a head brighter than the tail
            y = int(y0 + k) % h
            a[y, cols] = np.maximum(a[y, cols], across[cols] * along * peak)
    return np.clip(a, 0, 1)


def bolt_cell(seed):
    """A branched lightning channel in one BOLT_PX cell: the alpha (0..1), the base at the cell's bottom middle."""
    w, h = BOLT_PX
    rng = np.random.default_rng(seed)
    a = np.zeros((h, w), np.float64)
    yy, xx = np.mgrid[0:h, 0:w] + 0.5

    def stroke(x, y, x1, y1, width, glow, strength):
        # a segment's core (width px) and its halo (glow px), max-blended
        lo_x, hi_x = int(max(0, min(x, x1) - 3 * glow)), int(min(w, max(x, x1) + 3 * glow + 1))
        lo_y, hi_y = int(max(0, min(y, y1) - 3 * glow)), int(min(h, max(y, y1) + 3 * glow + 1))
        if lo_x >= hi_x or lo_y >= hi_y:
            return
        px, py = xx[lo_y:hi_y, lo_x:hi_x], yy[lo_y:hi_y, lo_x:hi_x]
        sx, sy = x1 - x, y1 - y
        L2 = max(sx * sx + sy * sy, 1e-9)
        t = np.clip(((px - x) * sx + (py - y) * sy) / L2, 0, 1)
        d = np.hypot(px - (x + t * sx), py - (y + t * sy))
        v = strength * np.maximum(np.exp(-0.5 * (d / width) ** 2), 0.35 * np.exp(-0.5 * (d / glow) ** 2))
        a[lo_y:hi_y, lo_x:hi_x] = np.maximum(a[lo_y:hi_y, lo_x:hi_x], v)

    def walk(x, y, y_end, width, glow, strength, depth):
        while y < y_end:
            step = rng.uniform(10, 26)
            nx = float(np.clip(x + rng.normal(0, 9), 12, w - 12))
            ny = y + step
            stroke(x, y, nx, ny, width, glow, strength)
            if depth < 2 and rng.random() < (0.10 if depth == 0 else 0.05):
                walk(nx, ny, min(h, ny + rng.uniform(80, 260)), width * 0.6, glow * 0.6, strength * 0.6, depth + 1)
            x, y = nx, ny

    walk(w / 2 + rng.normal(0, 20), 0.0, h - 2, 4.5, 18.0, 1.0, 0)     # (1.6 / 7 px: a smear of mips at 3 km)
    # the channel ends at the bottom middle (where the shape's origin is)
    return np.clip(a, 0, 1)


def glow_texture():
    t = (np.arange(GLOW_PX) + 0.5) / GLOW_PX * 2 - 1
    x, y = np.meshgrid(t, t)
    r = np.sqrt(x * x + y * y)
    return np.clip(np.exp(-3.0 * r * r) * np.clip(1 - r, 0, 1) * 1.25, 0, 1)


def save_gray(a, path):
    Image.fromarray(np.round(a * 255).astype(np.uint8), "L").save(path)
    print(path)


def shells(radii=SHELL_R):
    """The three open cylinders: positions, tris (seen from inside: the camera's side), uvs, the shell of each tri."""
    P, T, UV, TM = [], [], [], []
    for k, r in enumerate(radii):
        reps = max(1, round(2 * math.pi * r / TEX_W_M))          # whole repeats around: no seam
        base = len(P)
        for i in range(SHELL_SEG + 1):
            a = 2 * math.pi * i / SHELL_SEG
            for z in SHELL_H:
                P.append((r * math.cos(a), r * math.sin(a), z))
                UV.append((reps * i / SHELL_SEG, z / TEX_H_M))
        for i in range(SHELL_SEG):
            v0, v1, v2, v3 = base + 2 * i, base + 2 * i + 1, base + 2 * i + 2, base + 2 * i + 3
            T += [(v0, v1, v2), (v1, v3, v2)]                         # counter-clockwise seen from the axis
            TM += [k, k]
    return np.array(P, np.float64), np.array(T, np.int64), np.array(UV, np.float64), np.array(TM, np.int64)


def quad(w, h, u0, u1, centred=False):
    z0 = -h / 2 if centred else 0.0
    P = np.array([(-w / 2, 0, z0), (w / 2, 0, z0), (w / 2, 0, z0 + h), (-w / 2, 0, z0 + h)], np.float64)
    UV = np.array([(u0, 0), (u1, 0), (u1, 1), (u0, 1)], np.float64)
    T = np.array([(0, 1, 2), (0, 2, 3)], np.int64)
    return P, T, UV


def write_shape(path, P, T, UV, material, tm=None):
    data = cdae.encode(P, T, uvs=UV, material=material, tri_materials=tm, close=False)
    tmp = staging.tmp_for(path)
    with open(tmp, "wb") as f:
        f.write(data)
    staging.replace(tmp, path)
    print(path, len(data), "bytes")


def materials():
    def emissive(name, tex, nits):
        return {"name": name, "mapTo": name, "class": "Material", "version": 1.5,
                "Stages": [{"baseColorFactor": [0, 0, 0, 1], "metallicFactor": 0, "roughnessFactor": 1,
                            "emissiveFactor": list(BOLT_RGB), "emissiveIntensityNits": nits,
                            "instanceEmissive": True, "instanceOpacity": True, "opacityFactor": 1,
                            "opacityMap": REF + tex}, {}, {}, {}],
                "translucent": True, "translucentBlendOp": "LerpAlpha", "translucentZWrite": False,
                "castShadows": False, "doubleSided": True}
    out = {}
    for k, op in enumerate(SHELL_OPACITY):
        name = f"bngEarthRainShell{k}"
        out[name] = {"name": name, "mapTo": name, "class": "Material", "version": 1.5,
                     "Stages": [{"baseColorFactor": [*SHELL_RGB, 1], "metallicFactor": 0, "roughnessFactor": 0.35,
                                 "opacityMap": REF + "bngEarth_rainstreak.data.dds", "opacityFactor": op,
                                 "instanceOpacity": True, "animFlags": 1, "scrollDir": [0, 1],
                                 "scrollSpeed": FALL_MS / TEX_H_M}, {}, {}, {}],
                     "translucent": True, "translucentBlendOp": "LerpAlpha", "translucentZWrite": False,
                     "castShadows": False, "doubleSided": True}
    out["bngEarthBolt"] = emissive("bngEarthBolt", "bngEarth_bolt.data.dds", BOLT_NITS)
    out["bngEarthFlashGlow"] = emissive("bngEarthFlashGlow", "bngEarth_flashglow.data.dds", GLOW_NITS)
    # the lit air around the camera in a flash (storm.lua: a cylinder 40 m out, its opacity the flash; no texture)
    veil = emissive("bngEarthFlashVeil", "bngEarth_flashglow.data.dds", VEIL_NITS)
    del veil["Stages"][0]["opacityMap"]
    out["bngEarthFlashVeil"] = veil
    path = os.path.join(OUT, "weather.materials.json")
    with open(path, "w", newline="\n") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    print(path)


def main():
    os.makedirs(OUT, exist_ok=True)
    save_gray(streak_texture(), os.path.join(OUT, "bngEarth_rainstreak.data.png"))
    cells = [bolt_cell(9100 + k) for k in range(BOLTS)]
    save_gray(np.concatenate(cells, axis=1), os.path.join(OUT, "bngEarth_bolt.data.png"))
    save_gray(glow_texture(), os.path.join(OUT, "bngEarth_flashglow.data.png"))
    P, T, UV, TM = shells()
    write_shape(os.path.join(OUT, "bne_rain_shell.cdae"), P, T, UV,
                [f"bngEarthRainShell{k}" for k in range(len(SHELL_R))], TM)
    # the one-shell set (rainstreaks.lua SHELLS "near": a third of the fill, the Deck paid ~4.3 ms for the three)
    P, T, UV, TM = shells((SHELL_NEAR_R,))
    write_shape(os.path.join(OUT, "bne_rain_shell1.cdae"), P, T, UV, ["bngEarthRainShell0"], TM)
    aspect = BOLT_PX[0] / BOLT_PX[1]
    for k in range(BOLTS):
        P, T, UV = quad(aspect, 1.0, k / BOLTS, (k + 1) / BOLTS)
        write_shape(os.path.join(OUT, f"bne_bolt{k + 1}.cdae"), P, T, UV, "bngEarthBolt")
    P, T, UV = quad(1.0, 1.0, 0.0, 1.0, centred=True)
    write_shape(os.path.join(OUT, "bne_flashglow.cdae"), P, T, UV, "bngEarthFlashGlow")
    P, T, UV, TM = shells((VEIL_R,))
    write_shape(os.path.join(OUT, "bne_flashveil.cdae"), P, T, UV, ["bngEarthFlashVeil"], TM)
    materials()


if __name__ == "__main__":
    main()
