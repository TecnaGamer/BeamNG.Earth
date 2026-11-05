#!/usr/bin/env python3
"""ingest_rail_textures.py: ChatGPT-made albedo tiles of the railway work (assets/ai_inbox/*.png) -> the mod's
game-ready texture sets: <name>_b.color.png (sRGB), <name>_nm.normal.png, <name>_r.data.png (roughness).

    python3 tools/ingest_rail_textures.py            # every texture in TEXTURES that has its inbox file
    python3 tools/ingest_rail_textures.py rubber     # one

Per texture: the tile made seamless (a cross-faded band over the seam, then checked), its low frequencies divided out
(the baked light of an image generator), resized to SIZE, a normal map from the blurred luminance (periodic, float: no
8-bit steps) and a roughness map from the luminance. The game caches textures by name: a changed result gets a new
suffix (OUT_SUFFIX) rather than the same file name again. Pure numpy + PIL."""
import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
INBOX = os.path.join(ROOT, "assets", "ai_inbox")
OUT = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth")
SIZE = 1024
OUT_SUFFIX = ""
SEAM_BAND = 0.08            # share of the tile the seam's cross-fade covers
FLATTEN_SIGMA = 0.25        # of the tile: the blur whose ratio is divided out (the baked light)

# name -> inbox file, output base name, normal strength (height range in texels), roughness range (dark, light),
# whether to flatten the low frequencies
TEXTURES = {
    "rubber": dict(src="rail_xing_rubber.png", out="bne_rail_xing_rubber", normal=2.2, rough=(0.82, 0.95), flatten=True),
    "ballast": dict(src="rail_ballast.png", out="bne_rail_ballast_stone", normal=5.0, rough=(0.75, 0.95), flatten=True),
    "galv": dict(src="rail_galv_steel.png", out="bne_rail_galv", normal=1.0, rough=(0.35, 0.6), flatten=True),
    "concrete": dict(src="rail_concrete_mast.png", out="bne_rail_mast_concrete", normal=2.5, rough=(0.8, 0.95),
                     flatten=True),
}


def blur(a, sigma_px):
    """Periodic gaussian blur (FFT) of a 2-D or 3-D float array over its first two axes."""
    h, w = a.shape[:2]
    fy = np.fft.fftfreq(h)[:, None]
    fx = np.fft.fftfreq(w)[None, :]
    k = np.exp(-2.0 * (np.pi * sigma_px) ** 2 * (fx * fx + fy * fy))
    if a.ndim == 3:
        k = k[:, :, None]
        return np.real(np.fft.ifft2(np.fft.fft2(a, axes=(0, 1)) * k, axes=(0, 1)))
    return np.real(np.fft.ifft2(np.fft.fft2(a) * k))


def make_seamless(a):
    """Seam of a tile closed on both axes: the half-tile shifted copy is continuous at the old edges (they are the
    original's middle) and the original is continuous in the middle, so the result is the original with the shifted
    copy cross-faded in over a band at each edge."""
    for axis in (1, 0):
        n = a.shape[axis]
        band = max(2, int(n * SEAM_BAND))
        t = np.ones(n)
        ramp = np.linspace(0.0, 1.0, band)
        t[:band], t[-band:] = ramp, ramp[::-1]
        shape = [1] * a.ndim
        shape[axis] = n
        t = t.reshape(shape)
        a = a * t + np.roll(a, n // 2, axis=axis) * (1.0 - t)
    return a


def seam_ratio(a):
    """(edge difference / neighbour difference) left-right and top-bottom: near 1.0 is seamless."""
    g = a.mean(2) if a.ndim == 3 else a
    lr = np.abs(g[:, 0] - g[:, -1]).mean() / max(np.abs(np.diff(g, axis=1)).mean(), 1e-9)
    tb = np.abs(g[0] - g[-1]).mean() / max(np.abs(np.diff(g, axis=0)).mean(), 1e-9)
    return float(lr), float(tb)


def flatten(a):
    """Divide out the low frequencies (a baked light), keeping the tile's own mean."""
    h = a.shape[0]
    low = blur(a, h * FLATTEN_SIGMA)
    return np.clip(a / np.maximum(low, 1.0) * a.mean((0, 1)), 0.0, 255.0)


def normal_map(a, strength):
    """Tangent-space normal map (RGB uint8, +Z out) from a's luminance as a height field (periodic)."""
    g = a.mean(2) / 255.0
    g = blur(g, 0.8)
    dx = (np.roll(g, -1, axis=1) - np.roll(g, 1, axis=1)) * 0.5
    dy = (np.roll(g, -1, axis=0) - np.roll(g, 1, axis=0)) * 0.5
    sc = strength * g.shape[0] / 64.0
    n = np.stack([-dx * sc, dy * sc, np.ones_like(g)], axis=-1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    return ((n * 0.5 + 0.5) * 255.0 + 0.5).astype(np.uint8)


def roughness_map(a, lo, hi):
    """Roughness (uint8): lighter grains a little smoother than the dark pits."""
    g = a.mean(2)
    g = (g - g.min()) / max(g.max() - g.min(), 1e-9)
    return ((hi - (hi - lo) * g) * 255.0 + 0.5).astype(np.uint8)


TEX_VFS = "/levels/beamng_earth/art/shapes/beamngEarth/"
# texture set -> (materials file, material) whose maps it becomes
LINKS = {"concrete": ("catenary.materials.json", "bne_cat_concrete"), "galv": ("catenary.materials.json", "bne_cat_galv")}


def link_material(file, material, base, tint=(1.0, 1.0, 1.0)):
    """Point material (in the mod's materials file) at the texture set <base>_b / _nm / _r."""
    import json
    path = os.path.join(OUT, file)
    with open(path, encoding="utf8") as f:
        d = json.load(f)
    st = d[material]["Stages"][0]
    st.update({"baseColorMap": TEX_VFS + base + "_b.color.png", "normalMap": TEX_VFS + base + "_nm.normal.png",
               "roughnessMap": TEX_VFS + base + "_r.data.png", "baseColorFactor": [*tint, 1.0]})
    st.pop("roughnessFactor", None)
    with open(path, "w", encoding="utf8") as f:
        json.dump(d, f, indent=2)


def ingest(key):
    cfg = TEXTURES[key]
    src = os.path.join(INBOX, cfg["src"])
    if not os.path.exists(src):
        return False
    a = np.asarray(Image.open(src).convert("RGB"), np.float64)
    before = seam_ratio(a)
    if before[0] > 1.15 or before[1] > 1.15:
        a = make_seamless(a)
    a = flatten(a) if cfg["flatten"] else a
    after = seam_ratio(a)
    im = Image.fromarray(np.clip(a + 0.5, 0, 255).astype(np.uint8)).resize((SIZE, SIZE), Image.LANCZOS)
    a = np.asarray(im, np.float64)
    base = os.path.join(OUT, cfg["out"] + OUT_SUFFIX)
    im.save(base + "_b.color.png")
    Image.fromarray(normal_map(a, cfg["normal"])).save(base + "_nm.normal.png")
    Image.fromarray(roughness_map(a, *cfg["rough"]), "L").save(base + "_r.data.png")
    if key in LINKS:
        link_material(*LINKS[key], cfg["out"] + OUT_SUFFIX)
    print(f"{key}: seam LR/TB {before[0]:.2f}/{before[1]:.2f} -> {after[0]:.2f}/{after[1]:.2f}, "
          f"mean {a.mean():.1f} -> {os.path.basename(base)}_*.png")
    return True


def main(argv):
    keys = argv or list(TEXTURES)
    done = [k for k in keys if ingest(k)]
    if not done:
        print("nothing in", INBOX)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
