"""build_night_windows.py: the buildings' lit windows at night (nightlights.lua turns them on after dusk), two emissive
maps beside the building materials (mod/.../art/shapes/beamngEarth/):

  bne_facade_night_e.png  the plaster window sheet (m_highrise_plaster, bne_bld_facade): about 40 % of its windows lit (West Coast USA at night: most of its windows dark),
                          each a room's light (warm, now and then cool white; its own brightness, some dim), brighter
                          toward the ceiling, through
                          the curtains the sheet draws (the stock map lights only one row of it, four windows)
  bne_facade_night_e_b.png, _c.png  the same sheet's windows, each band's levels turned by one and two openings
                          (bne_bld_facade_b / _c: the same sheet by day; buildings.py gives each floor of a wall one
                          of the three, so a lit window column is not lit all the way up)
  bne_office_night_e.png  the metal sheet (offices' curtain walls and shopfronts, bne_bld_office / bne_bld_shop): the
                          stock map (every window lit) with some bays dimmed, a few dark

The windows are picked per sheet cell (buildings.py's bands and bays: a wall's bays take their cells in turn or hashed),
so a cell lit is lit wherever it is used. Deterministic. Needs the game's trim textures (the game folder's
content/assets/materials/trim.zip).

    python3 tools/build_night_windows.py [--game DIR]
"""
import argparse
import io
import os
import zipfile

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth")
PLASTER = "assets/materials/trim/plaster/t_highrise_plaster/t_highrise_plaster_b.color.dds"
METAL_E = "assets/materials/trim/metal/t_highrise_metal/t_highrise_metal_e.color.dds"

# The plaster sheet's window openings (x0, y0, x1, y1 px of 1024, image top first), measured from the image, per band
# (buildings.py BAND_TALL, BAND_PANEL, BAND_SASH, BAND_BRICK), and each one's brightness at night (0: dark). Band 2's
# bays 1 and 4 are open (black) windows buildings.py never uses.
# (Each lit window its own brightness, the user, 2026-11-08: "the brightness of windows should be randomized at night":
# every lit window glowed alike. The glow saturates the screen from ~0.3 of full (factor 0.4 looked as 1 in game), so
# the levels go well below that to show. The sash band's three are also the laid-over windows' cells, buildings.py
# WIN_CELLS_PX, one hashed per window: bright, dim, very dim.)
OPENINGS = [
    [((80, 26, 198, 236), 1.0), ((330, 26, 446, 236), 0), ((578, 26, 988, 236), 0.12)],
    [((52, 306, 202, 460), 0.06), ((308, 306, 458, 460), 0), ((566, 306, 716, 460), 0), ((822, 306, 972, 460), 1.0)],
    [((60, 566, 146, 716), 1.0), None, ((466, 566, 554, 716), 0.05), ((670, 566, 756, 716), 0.18), None],
    [((46, 814, 154, 946), 0), ((252, 814, 360, 946), 0.15), ((458, 814, 566, 946), 0), ((662, 814, 770, 946), 0),
     ((866, 814, 974, 946), 1.0)],
]
# the plaster sheet's night maps (buildings.py FACADE_VARIANTS' materials) and their turns
FACADE_MAPS = (("bne_facade_night_e.png", 0), ("bne_facade_night_e_b.png", 1), ("bne_facade_night_e_c.png", 2))
WARM = (1.0, 0.70, 0.38)       # incandescent / warm LED, ~2700 K
WARMER = (1.0, 0.62, 0.30)
COOL = (0.78, 0.86, 1.0)       # cool white, ~5000 K
# The metal sheet (512 px): its bands 128 px high (BAND_OFFICE: 5 bays, BAND_OFFICE2: 4, BAND_SHOP: 2) and a factor
# per bay (1 lit, lower dimmed, 0 dark)
# (fewer and dimmer than the stock map's every bay: every floor of a curtain wall takes the same band, so a lit bay is lit
# from street to roof, and Manhattan's towers glowed white all over, TGForever Discord 2026-10-28)
OFFICE_BAYS = [(5, (0.55, 0.0, 0.8, 0.0, 0.3)), (4, (0.0, 0.7, 0.25, 0.0)), (2, (1.0, 1.0)), (1, (0.6,))]


def _read(zf, name):
    return Image.open(io.BytesIO(zf.read(name)))


def facade(base, turn=0):
    """The plaster sheet's night map from its base colour (1024 x 1024 RGB), each band's levels turned by turn
    openings (FACADE_MAPS)."""
    b = np.asarray(base.convert("RGB"), np.float32) / 255.0
    luma = 0.3 * b[..., 0] + 0.59 * b[..., 1] + 0.11 * b[..., 2]
    out = np.zeros_like(b)
    k = 0
    for band in OPENINGS:
        cells = [c for c in band if c is not None]
        for j, ((x0, y0, x1, y1), _) in enumerate(cells):
            lvl = cells[(j - turn) % len(cells)][1]
            k += 1
            if not lvl:
                continue
            col = np.array(COOL if k % 5 == 0 else (WARMER if k % 3 == 0 else WARM), np.float32)
            h, w = y1 - y0, x1 - x0
            yy = np.linspace(0.0, 1.0, h)[:, None]
            xx = np.linspace(-1.0, 1.0, w)[None, :]
            # the room's light: brighter near the ceiling and the middle, the curtains (the sheet's bright cloth)
            # brighter still
            glow = (0.75 + 0.25 * (1.0 - yy)) * (0.8 + 0.2 * (1.0 - xx * xx))
            cloth = 0.45 + 0.55 * np.clip((luma[y0:y1, x0:x1] - 0.45) / 0.5, 0.0, 1.0)
            out[y0:y1, x0:x1] = col[None, None, :] * (lvl * glow * cloth)[..., None]
    return Image.fromarray(np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8))


def office(stock_e):
    """The metal sheet's night map: the stock one, some bays dimmed."""
    e = np.asarray(stock_e.convert("RGB"), np.float32)
    H, W = e.shape[:2]
    bh = H // len(OFFICE_BAYS)
    for i, (n, f) in enumerate(OFFICE_BAYS):
        for j in range(n):
            x0, x1 = j * W // n, (j + 1) * W // n
            e[i * bh:(i + 1) * bh, x0:x1] *= f[j]
    return Image.fromarray(np.clip(e + 0.5, 0, 255).astype(np.uint8))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", default=os.path.expanduser("~/.local/share/Steam/steamapps/common/BeamNG.drive"))
    a = ap.parse_args()
    with zipfile.ZipFile(os.path.join(a.game, "content", "assets", "materials", "trim.zip")) as zf:
        base = _read(zf, PLASTER)
        out = [(facade(base, turn), name) for name, turn in FACADE_MAPS]
        out.append((office(_read(zf, METAL_E)), "bne_office_night_e.png"))
    for img, name in out:
        p = os.path.join(OUT, name)
        img.save(p, optimize=True)
        print(p, img.size, os.path.getsize(p), "bytes")


if __name__ == "__main__":
    main()
