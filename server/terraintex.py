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
    "grass/t_grass_01/t_grass_01_b",
    "grass/t_macro_grass/t_macro_grass_b",
    "mud/mud/t_mud_b",
    "rock/macro_rocky/t_macro_rocky_b",
    "rock/t_dirt_rocky/t_dirt_rocky_b",
    "sand/t_macro_sand/t_macro_sand_b",
    "sand/t_sand/t_sand_b",
    "soil/dirt_crumbly/t_dirt_crumbly_b",
    "soil/dirt_loose_dusty/t_dirt_loose_dusty_b",
    "soil/macro_clumply/t_macro_clumpy_b",
    "soil/t_gravel/t_gravel_b",
)


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
