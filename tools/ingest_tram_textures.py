#!/usr/bin/env python3
"""ingest_tram_textures.py: the user's ChatGPT images of the tram track and tram signals (assets/ai_inbox/tram_*.png,
prompts in docs/new/046_prompts_tram-textures_2026-11-13.md) -> game-ready texture sets of the mod, and their materials.

    python tools/ingest_tram_textures.py            # everything that has its inbox file
    python tools/ingest_tram_textures.py strips     # the four street strips (rails baked in)
    python tools/ingest_tram_textures.py ballast signal   # named groups: strips ballast signal black

Street strips (bne_tram_street_<kind>_v1): the image is a cross-section of a paved tram track with its two rails on it, the
rails running along v. Each image is resampled across so that the rails stand at 10 % and 90 % of the width (a strip of
the track's gauge + 1 m wide has them at the gauge: the same 80 % rule the prompts gave), made seamless along v only
(across is the strip's width), no low-frequency flattening (it would erase the rails). They are BASE-layer materials
(a mod's colour map is not read for alpha, 2026-11-06: a rail overlay with alpha came out black), so a strip replaces
both the paving and the rails' ribbon: tramtrack.paving picks the material, rails._tram draws no separate rails on it.
Ballast and signal textures reuse ingest_rail_textures' pipeline. Run mod_textures_dds.py afterwards (DDS, never PNG).
Pure numpy + PIL."""
import json
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ingest_rail_textures as irt  # noqa: E402

INBOX, OUT, SIZE = irt.INBOX, irt.OUT, irt.SIZE
TEX_VFS = irt.TEX_VFS
RAIL_AT = (0.10, 0.90)          # where the rails stand across the finished strip

# kind -> inbox file, rail centre columns as a share of the source width (measured on the images of 2026-11-13),
# normal strength, roughness range, ground type, metallic of the material (the rails are steel, the paving is not)
STRIPS = {
    "asphalt": dict(src="tram_rail_asphalt.png", rails=(0.0941, 0.9078), normal=2.0, rough=(0.65, 0.92), ground="ASPHALT", ver=2),   # (v2: the brighter asphalt, silver rails, 2026-11-14)
    "setts": dict(src="tram_rail_setts.png", rails=(0.0995, 0.9010), normal=3.0, rough=(0.6, 0.9), ground="CONCRETE"),
    "slab": dict(src="tram_rail_slabs.png", rails=(0.1155, 0.8835), normal=2.0, rough=(0.65, 0.92), ground="CONCRETE"),
    "grass": dict(src="tram_rail_grass.png", rails=(0.1100, 0.8875), normal=2.5, rough=(0.7, 0.95), ground="GRASS"),
}
SEAM_BAND = 0.06


def align_rails(a, rails):
    """a (h, w, 3) with its rails at the given shares of the width -> (h, SIZE, 3) with them at RAIL_AT. The source window
    is the one that maps linearly; past the image's edge the edge column repeats."""
    h, w = a.shape[:2]
    cl, cr = rails[0] * w, rails[1] * w
    scale = (cr - cl) / ((RAIL_AT[1] - RAIL_AT[0]) * SIZE)          # source px per output px
    x0 = cl - RAIL_AT[0] * SIZE * scale
    xs = x0 + (np.arange(SIZE) + 0.5) * scale - 0.5
    i0 = np.floor(xs).astype(int)
    f = (xs - i0)[None, :, None]
    i0c, i1c = np.clip(i0, 0, w - 1), np.clip(i0 + 1, 0, w - 1)
    out = a[:, i0c] * (1 - f) + a[:, i1c] * f
    return np.asarray(Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8)).resize((SIZE, SIZE), Image.LANCZOS),
                      np.float64)


def seamless_v(a):
    """The top and bottom edges closed (the half-tile shifted copy cross-faded in over a band at each edge), only along v."""
    n = a.shape[0]
    band = max(2, int(n * SEAM_BAND))
    t = np.ones(n)
    ramp = np.linspace(0.0, 1.0, band)
    t[:band], t[-band:] = ramp, ramp[::-1]
    t = t.reshape(n, 1, 1)
    return a * t + np.roll(a, n // 2, axis=0) * (1.0 - t)


def seam_v(a):
    g = a.mean(2)
    return float(np.abs(g[0] - g[-1]).mean() / max(np.abs(np.diff(g, axis=0)).mean(), 1e-9))


def save_set(a, base, normal, rough):
    Image.fromarray(np.clip(a + 0.5, 0, 255).astype(np.uint8)).save(base + "_b.color.png")
    Image.fromarray(irt.normal_map(a, normal)).save(base + "_nm.normal.png")
    Image.fromarray(irt.roughness_map(a, *rough), "L").save(base + "_r.data.png")


def strips():
    done = []
    for kind, cfg in STRIPS.items():
        src = os.path.join(INBOX, cfg["src"])
        if not os.path.exists(src):
            continue
        a = align_rails(np.asarray(Image.open(src).convert("RGB"), np.float64), cfg["rails"])
        before = seam_v(a)
        if before > 1.15:
            a = seamless_v(a)
        out = f"bne_tram_street_{kind}_v{cfg.get('ver', 1)}"
        save_set(a, os.path.join(OUT, out), cfg["normal"], cfg["rough"])
        link_strip(kind, out, cfg["ground"])
        print(f"strip {kind}: seam along v {before:.2f} -> {seam_v(a):.2f} -> {out}_*.png")
        done.append(kind)
    return done


def link_strip(kind, base, ground):
    """The strip's material in tram.materials.json (created or re-pointed), a base-layer paving."""
    path = os.path.join(OUT, "tram.materials.json")
    with open(path, encoding="utf8") as f:
        d = json.load(f)
    name = f"bne_tram_street_{kind}"
    import uuid
    d[name] = {"name": name, "mapTo": name, "class": "Material", "version": 1.5,
               "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/" + name)),
               "Stages": [{"baseColorMap": TEX_VFS + base + "_b.color.png", "normalMap": TEX_VFS + base + "_nm.normal.png",
                           "roughnessMap": TEX_VFS + base + "_r.data.png", "metallicFactor": 0,
                           "baseColorFactor": [1, 1, 1, 1]}, {}, {}, {}],
               "groundType": ground, "translucent": False}
    with open(path, "w", encoding="utf8") as f:
        json.dump(d, f, indent=2)


def ballast():
    src = os.path.join(INBOX, "tram_ballast.png")
    if not os.path.exists(src):
        return []
    a = np.asarray(Image.open(src).convert("RGB"), np.float64)
    before = irt.seam_ratio(a)
    if before[0] > 1.15 or before[1] > 1.15:
        a = irt.make_seamless(a)
    a = irt.flatten(a)
    a = np.asarray(Image.fromarray(np.clip(a + 0.5, 0, 255).astype(np.uint8)).resize((SIZE, SIZE), Image.LANCZOS), np.float64)
    save_set(a, os.path.join(OUT, "bne_tram_ballast_v1"), 5.0, (0.75, 0.95))
    path = os.path.join(OUT, "tram.materials.json")
    with open(path, encoding="utf8") as f:
        d = json.load(f)
    import uuid
    d["bne_tram_ballast"] = {"name": "bne_tram_ballast", "mapTo": "bne_tram_ballast", "class": "Material", "version": 1.5,
                             "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/bne_tram_ballast")),
                             "Stages": [{"baseColorMap": TEX_VFS + "bne_tram_ballast_v1_b.color.png",
                                         "normalMap": TEX_VFS + "bne_tram_ballast_v1_nm.normal.png",
                                         "roughnessMap": TEX_VFS + "bne_tram_ballast_v1_r.data.png", "metallicFactor": 0,
                                         "baseColorFactor": [1, 1, 1, 1]}, {}, {}, {}],
                             "groundType": "GRAVEL", "translucent": False}
    with open(path, "w", encoding="utf8") as f:
        json.dump(d, f, indent=2)
    print("ballast: seam", [round(x, 2) for x in before], "-> bne_tram_ballast_v1_*.png")
    return ["ballast"]


def signal():
    """The lamp sheet (three bands) -> bne_ts_lamps.color.png (512 x 512, three equal bands, the palette texture unchanged:
    it selects the band by its pure red / green / blue); the post's galvanised tile -> bne_ts_grey, the black paint (shared
    with the poles' bne_tram_black) -> bne_ts_black and bne_ts_dark."""
    done = []
    src = os.path.join(INBOX, "tram_signal_lamps.png")
    if os.path.exists(src):
        im = Image.open(src).convert("RGB")
        w, h = im.size
        tex = 512
        edges = [int(round(k * h / 3)) for k in range(4)]
        sheet = Image.new("RGB", (tex, tex))
        for k in range(3):
            b = int(round(k * tex / 3)), int(round((k + 1) * tex / 3))
            # (a few px off each end of a band: the one-pixel dark line between the bands of the source)
            band = im.crop((0, edges[k] + 4, w, edges[k + 1] - 4)).resize((tex, b[1] - b[0]), Image.LANCZOS)
            sheet.paste(band, (0, b[0]))
        sheet.save(os.path.join(OUT, "bne_ts_lamps.color.png"))
        print("signal lamps: bne_ts_lamps.color.png (3 bands of the user's LED sheet)")
        done.append("lamps")
    for src_name, mats, out, normal, rough, metal in (
            ("tram_signal_post.png", ("bne_ts_grey",), "bne_ts_galv_v1", 1.0, (0.35, 0.6), 0.75),
            ("tram_black_paint.png", ("bne_ts_black", "bne_ts_dark"), "bne_ts_paint_v1", 1.2, (0.4, 0.7), 0.0)):
        p = os.path.join(INBOX, src_name)
        if not os.path.exists(p):
            continue
        a = np.asarray(Image.open(p).convert("RGB"), np.float64)
        if max(irt.seam_ratio(a)) > 1.15:
            a = irt.make_seamless(a)
        a = np.asarray(Image.fromarray(np.clip(a + 0.5, 0, 255).astype(np.uint8)).resize((SIZE, SIZE), Image.LANCZOS),
                       np.float64)
        save_set(a, os.path.join(OUT, out), normal, rough)
        path = os.path.join(OUT, "tram_signals.materials.json")
        with open(path, encoding="utf8") as f:
            d = json.load(f)
        for m in mats:
            st = d[m]["Stages"][0]
            st.update({"baseColorMap": TEX_VFS + out + "_b.color.png", "normalMap": TEX_VFS + out + "_nm.normal.png",
                       "roughnessMap": TEX_VFS + out + "_r.data.png", "baseColorFactor": [1, 1, 1, 1],
                       "metallicFactor": metal})
            st.pop("roughnessFactor", None)
        with open(path, "w", encoding="utf8") as f:
            json.dump(d, f, indent=2)
        print(f"signal {src_name}: -> {out}_*.png for {', '.join(mats)}")
        done.append(out)
    return done


GROUPS = {"strips": strips, "ballast": ballast, "signal": signal}


def main(argv):
    keys = argv or list(GROUPS) + ["black"]
    done = []
    for k in keys:
        if k == "black":            # the poles' black paint: ingest_rail_textures' own entry (bne_tram_black_v2)
            done += ["black"] if irt.ingest("tram_black") else []
        else:
            done += GROUPS[k]()
    if not done:
        print("nothing in", INBOX)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
