"""The stock tileable textures the drawn block's square layers use, copied as PNG from the player's own game files (the
user, 2026-11-11: "instead could we paint the plaza and apply a texture to it like the roads": surface.PAVERS / BRICK).

The game's terrain material packing reads its textures through GBitmap, which has no DDS reader ("unable to find
registration for extension [dds]"), and no stock terrain texture has pavers, brick or setts: the building tileables
that do (West Coast USA's brutalist slabs, the red herringbone brick, Italy's setts) ship as DDS only (BC7 colour, BC5
normals). So the server decodes them once (Pillow) into <user folder>/temp/beamngEarth/terraintex/ (the level's
materials name them under VFS_DIR; tools/make_level_assets.py, terrain.lua LOOKS); a BC5 normal map gets its z
rebuilt. Nothing of the game's is shipped. Without the game's folder (BNGEARTH_GAME_DIR) or a file, that layer draws
without its detail texture."""
import io
import logging
import os
import zipfile

import numpy as np

log = logging.getLogger("earthd.terraintex")

VERSION = 2
VFS_DIR = "/temp/beamngEarth/terraintex/"
ARCHIVE = ("content", "assets", "materials", "tileable.zip")
# PNG name -> (the game archive's member, "color" or "normal")
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
SIZE = 1024             # the texture set's detail size (a mismatch costs a resize at every material rebuild)


def out_dir(user_dir):
    return os.path.join(user_dir, "temp", "beamngEarth", "terraintex")


def convert(data, kind):
    """A DDS file's bytes as a SIZE x SIZE RGB PIL image; a two-channel (BC5) normal map with its z rebuilt."""
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.load()
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
    """Write the TEXTURES missing under out_dir(user_dir) from game_dir's archive. -> the names written; never raises."""
    done = []
    if not user_dir or not game_dir or os.environ.get("BNGE_TERRAINTEX") == "off":
        return done
    d = out_dir(user_dir)
    stamp = os.path.join(d, f"v{VERSION}")
    missing = [n for n in TEXTURES if not (os.path.exists(stamp) and os.path.exists(os.path.join(d, n)))]
    if not missing:
        return done
    path = os.path.join(game_dir, *ARCHIVE)
    try:
        os.makedirs(d, exist_ok=True)
        with zipfile.ZipFile(path) as z:
            names = {n.lower(): n for n in z.namelist()}
            for n in missing:
                member, kind = TEXTURES[n]
                real = names.get(member.lower())
                if real is None:
                    log.warning("terrain textures: %s not in %s", member, path)
                    continue
                tmp = os.path.join(d, n + ".tmp")
                convert(z.read(real), kind).save(tmp, "PNG")
                os.replace(tmp, os.path.join(d, n))
                done.append(n)
        open(stamp, "w").close()
    except Exception as e:              # (no squares' detail texture rather than no server)
        log.warning("terrain textures from %s: %s", path, e)
    return done
