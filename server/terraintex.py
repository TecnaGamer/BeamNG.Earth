"""The stock tileable textures the drawn block's square layers use, copied as PNG from the player's own game files (the
user, 2026-11-11: "instead could we paint the plaza and apply a texture to it like the roads": surface.PAVERS / BRICK).

The game's terrain material packing reads its textures through GBitmap, which has no DDS reader ("unable to find
registration for extension [dds]"), and no stock terrain texture has pavers, brick or setts: the building tileables
that do (West Coast USA's brutalist slabs, the red herringbone brick, Italy's setts) ship as DDS only (BC7 colour, BC5
normals). So the server decodes them once (Pillow) into <user folder>/temp/beamngEarth/terraintex/ (the level's
materials name them under VFS_DIR; tools/make_level_assets.py, terrain.lua LOOKS); a BC5 normal map gets its z
rebuilt. Nothing of the game's is shipped. Without the game's folder (BNGEARTH_GAME_DIR) or a file, that layer draws
without its detail texture.

Zero-mean grain (2026-11-16, the user: "the texture between the ground with collision vs no collision is very
different. and there's a visible border"): the terrain shader adds (2 t - 1) x strength of each detail and macro colour
texture's STORED value t to the photo in linear light (the packs are BC7_UNORM: terrainprefill.py), so a texture whose
mean is not 0.5 shifts the whole layer's colour: the stock t_macro_grass (mean 0.432, nearly flat) took 0.011-0.016 off
every grass texel out to the macro's 1.5 km, about half the brightness of a dark conifer forest's photo (sRGB 35), while
the far tiles past the block's edge show the photo as it is. RECENTRED_STEMS: copies of the stock colour textures the
ground layers use with each channel moved to a mean of exactly 127.5, so a layer adds grain and no colour: the block's
mean is the photo's, as the far tiles'. Named rc_name(stem) = bne_rc_<the stock path below /assets/materials/terrain/,
'/' as '__'>.png; terrain.lua points a layer back at the stock file when its copy is missing (stock_of).
"""
import io
import logging
import os
import zipfile

import numpy as np

log = logging.getLogger("earthd.terraintex")

VERSION = 3
VFS_DIR = "/temp/beamngEarth/terraintex/"
ARCHIVE = ("content", "assets", "materials", "tileable.zip")
TERRAIN_ARCHIVE = ("content", "assets", "materials", "terrain.zip")
STOCK_TERRAIN = "assets/materials/terrain/"
# The stock colour textures (below STOCK_TERRAIN, without .png) the ground layers take as zero-mean copies: every
# detail / macro colour map of tools/make_level_assets.py's materials and terrain.lua's LOOKS but the squares' own
# tileables (tests/test_terraintex.py checks both)
RECENTRED_STEMS = (
    "asphalt/asphalt/t_asphalt_b",
    "asphalt/macro_asphalt/t_macro_asphalt_b",
    "forest/t_forest_ground_02/t_forest_ground_02_b",
    "forest/t_macro_forest/t_macro_forest_b",
    "grass/macro_dry_grass_02/t_macro_dry_grass_02_b",
    "grass/t_dirt_dry_grass/t_dirt_dry_grass_b",
    "grass/t_grass_01/t_grass_01_b",
    "mud/mud/t_mud_b",
    "rock/macro_rocky/t_macro_rocky_b",
    "rock/t_dirt_rocky/t_dirt_rocky_b",
    "sand/t_macro_sand/t_macro_sand_b",
    "sand/t_sand/t_sand_b",
    "soil/dirt_crumbly/t_dirt_crumbly_b",
    "soil/dirt_loose_dusty/t_dirt_loose_dusty_b",
    "soil/macro_clumply/t_macro_clumpy_b",
    "soil/macro_holes/t_macro_holes_b",
    "soil/t_gravel/t_gravel_b",
    "soil/t_macro_flows/t_macro_flows_b",
)
# The squares' roughness and ambient occlusion (pavers, brick, cobble; 2026-11-16, they had the asphalt's): the layer's
# value is the level's flat base (roughness 240/255, ao 251/255: tools/make_level_assets.py) plus (2 t - 1) x strength,
# so a tileable's own map t0 is stored as 0.5 + (t0 - base) / 2: at the near strength 1 the layer shows t0 itself (the
# slabs' 0.74, the brick's 0.80 with rough joints; dark ao in the joints), fading toward the base with the detail.
BASE_R, BASE_AO = 240 / 255, 251 / 255


def rc_name(stem):
    """The zero-mean copy's file name of a stock colour texture stem ('grass/t_grass_01/t_grass_01_b')."""
    return "bne_rc_" + stem.replace("/", "__") + ".png"


def stock_of(name):
    """rc_name's inverse: the stock VFS path of a zero-mean copy's file name, or None for any other name."""
    if not (name.startswith("bne_rc_") and name.endswith(".png")):
        return None
    return "/" + STOCK_TERRAIN + name[len("bne_rc_"):-len(".png")].replace("__", "/") + ".png"


# PNG name -> (the game archive's member, "color" | "normal" | "recentre", the archive)
TEXTURES = {
    "bne_pavers_b.png": ("assets/materials/tileable/concrete/slabs_brutalist/t_slabs_brutalist_b.color.dds", "color"),
    "bne_pavers_nm.png": ("assets/materials/tileable/concrete/slabs_brutalist/t_slabs_brutalist_nm.normal.dds",
                          "normal"),
    "bne_brick_b.png": ("assets/materials/tileable/brick/t_bricks_herringbone_01/t_bricks_herringbone_01_b.color.dds",
                        "color"),
    "bne_brick_nm.png": ("assets/materials/tileable/brick/t_bricks_herringbone_01/t_bricks_herringbone_01_nm.normal.dds",
                         "normal"),
    # setts in rows (Italy's, the cobble roads' mesh material before ROADS_IN_GROUND): the cobble layer, cobbled
    # roads and squares mapped sett / cobblestone (the user, 2026-11-11: "can we do something similar to cobblestone
    # roads?"); 512 in the game, resized to SIZE
    "bne_cobble_b.png": ("assets/materials/tileable/paving/t_italy_cobblestone/t_italy_cobblestone_d.color.dds",
                         "color"),
    "bne_cobble_nm.png": ("assets/materials/tileable/paving/t_italy_cobblestone/t_italy_cobblestone_n.normal.dds",
                          "normal"),
    # roughness / ao (BASE_R): the slabs' and brick's own 512 maps, resized; the setts have none: roughness from their
    # specular map (1 - s / 2), ao from their colour's luminance (the joints dark)
    "bne_pavers_r.png": ("assets/materials/tileable/concrete/slabs_brutalist/t_slabs_brutalist_r.data.dds", "rough"),
    "bne_pavers_ao.png": ("assets/materials/tileable/concrete/slabs_brutalist/t_slabs_brutalist_ao.data.dds", "ao"),
    "bne_brick_r.png": ("assets/materials/tileable/brick/t_bricks_herringbone_01/t_bricks_herringbone_01_r.data.dds",
                        "rough"),
    "bne_brick_ao.png": ("assets/materials/tileable/brick/t_bricks_herringbone_01/t_bricks_herringbone_01_ao.data.dds",
                         "ao"),
    "bne_cobble_r.png": ("assets/materials/tileable/paving/t_italy_cobblestone/t_italy_cobblestone_s.color.dds",
                         "spec_rough"),
    "bne_cobble_ao.png": ("assets/materials/tileable/paving/t_italy_cobblestone/t_italy_cobblestone_d.color.dds",
                          "luma_ao"),
}
TEXTURES = {n: v + (ARCHIVE,) for n, v in TEXTURES.items()}
TEXTURES.update({rc_name(s): (STOCK_TERRAIN + s + ".png", "recentre", TERRAIN_ARCHIVE) for s in RECENTRED_STEMS})
SIZE = 1024             # the texture set's detail size (a mismatch costs a resize at every material rebuild)


def out_dir(user_dir):
    return os.path.join(user_dir, "temp", "beamngEarth", "terraintex")


def recentre(a):
    """An (h, w, 3) uint8 colour image with each channel moved to a mean of 127.5 (rounded, clipped). float64 sums of
    integers: the same bytes on any machine."""
    a = np.asarray(a, np.uint8)
    mean = a.reshape(-1, 3).astype(np.float64).sum(0) / (a.shape[0] * a.shape[1])
    return np.clip(np.floor(a.astype(np.float64) + (127.5 - mean) + 0.5), 0, 255).astype(np.uint8)


def layer_data(rgb, kind):
    """A SIZE x SIZE uint8 detail map of a tileable's (h, w, 3) map for the layer's roughness / ao (BASE_R): "rough" /
    "ao" its grey value, "spec_rough" 1 - its specular / 2, "luma_ao" its colour's luminance over its 95th percentile;
    stored as 0.5 + (t0 - base) / 2, then resized (bilinear, on the float values)."""
    from PIL import Image
    a = np.asarray(rgb, np.float64) / 255.0
    if kind == "luma_ao":
        y = a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722   # (element-wise, not BLAS: test_determinism)
        t0 = np.clip(y / max(float(np.percentile(y, 95)), 1e-6), 0.0, 1.0)
    elif kind == "spec_rough":
        t0 = 1.0 - a[..., 0] / 2.0
    else:
        t0 = a[..., 0]
    base = BASE_AO if kind in ("ao", "luma_ao") else BASE_R
    v = np.clip(0.5 + (t0 - base) / 2.0, 0.0, 1.0).astype(np.float32)
    if v.shape != (SIZE, SIZE):
        v = np.asarray(Image.fromarray(v, "F").resize((SIZE, SIZE), Image.BILINEAR), np.float32)
    return np.clip(np.floor(v.astype(np.float64) * 255.0 + 0.5), 0, 255).astype(np.uint8)


def convert(data, kind):
    """A DDS (or PNG) file's bytes as a SIZE x SIZE RGB PIL image: a two-channel (BC5) normal map with its z rebuilt;
    "recentre": the colour zero-mean (recentre, after the resize)."""
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.load()
    if kind == "recentre":
        im = im.convert("RGB")
        if im.size != (SIZE, SIZE):
            im = im.resize((SIZE, SIZE), Image.LANCZOS)
        return Image.fromarray(recentre(np.asarray(im)), "RGB")
    if kind in ("rough", "ao", "spec_rough", "luma_ao"):
        return Image.fromarray(np.repeat(layer_data(np.asarray(im.convert("RGB")), kind)[..., None], 3, 2), "RGB")
    a = np.asarray(im.convert("RGBA"), np.float32)[..., :3]
    if kind == "normal":
        x, y = a[..., 0] / 127.5 - 1.0, a[..., 1] / 127.5 - 1.0
        z = np.sqrt(np.clip(1.0 - x * x - y * y, 0.0, 1.0))
        a[..., 2] = (z + 1.0) * 127.5
    out = Image.fromarray(np.clip(a + 0.5, 0, 255).astype(np.uint8), "RGB")
    if out.size != (SIZE, SIZE):
        out = out.resize((SIZE, SIZE), Image.LANCZOS)
    return out


def ensure(user_dir, game_dir):
    """Write the TEXTURES missing under out_dir(user_dir) from game_dir's archives. -> the names written; never
    raises. A file is rewritten only when it is missing or the stamp of this VERSION is (the game watches files it
    has loaded)."""
    done = []
    if not user_dir or not game_dir or os.environ.get("BNGE_TERRAINTEX") == "off":
        return done
    d = out_dir(user_dir)
    stamp = os.path.join(d, f"v{VERSION}")
    missing = [n for n in TEXTURES if not (os.path.exists(stamp) and os.path.exists(os.path.join(d, n)))]
    if not missing:
        return done
    by_archive = {}
    for n in missing:
        member, kind, arch = TEXTURES[n]
        by_archive.setdefault(arch, []).append((n, member, kind))
    ok = True
    for arch, items in sorted(by_archive.items()):
        path = os.path.join(game_dir, *arch)
        try:
            os.makedirs(d, exist_ok=True)
            with zipfile.ZipFile(path) as z:
                names = {n.lower(): n for n in z.namelist()}
                for n, member, kind in items:
                    real = names.get(member.lower())
                    if real is None:
                        log.warning("terrain textures: %s not in %s", member, path)
                        continue
                    tmp = os.path.join(d, n + ".tmp")
                    convert(z.read(real), kind).save(tmp, "PNG")
                    os.replace(tmp, os.path.join(d, n))
                    done.append(n)
        except Exception as e:              # (a layer without its texture rather than no server)
            log.warning("terrain textures from %s: %s", path, e)
            ok = False
    if ok:
        try:
            open(stamp, "w").close()
        except OSError:
            pass
    return done
