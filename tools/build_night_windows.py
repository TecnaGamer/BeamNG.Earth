"""build_night_windows.py: the buildings' lit windows at night (nightlights.lua turns them on after dusk), emissive maps
beside the building materials (mod/.../art/shapes/beamngEarth/), each in three steps of the night (STEPS: the evening's,
from 23:15 and from 3:00; nightlights.lua M.LATE swaps a material's map between them):

  bne_facade_night_e*.png  the plaster window sheet (m_highrise_plaster, bne_bld_facade): about 40 % of its windows lit
                          in the evening (West Coast USA at night: most of its windows dark), each at its own level,
                          a room's light (warm, now and then cool white), brighter toward the ceiling, through the
                          curtains the sheet draws (the stock map lights only one row of it, four windows)
  bne_office_night_e*.png  the metal sheet (offices' curtain walls and shopfronts, bne_bld_office / bne_bld_shop): the
                          curtain walls' panes lit room by room, the shopfronts unit by unit, each at its own level

Each window or room has its own level (the user, 2026-11-08: "the brightness of windows should be randomized at
night"; buildings.py WALL_CELLS starts each floor at a hashed cell of its band) and its own hour to go dark (OUT: "have
less lit windows depending on how late it is, peaking at 5 am"): a later step's map lights a subset of the one before
(a window dark in the evening stays dark), at 3 am about a third of the evening's. (After S-moreo's PR #916: its levels
and its curtain-wall rooms; one map per step instead of a material per variant.)

The windows are picked per sheet cell (buildings.py's bands and bays), so a cell lit is lit wherever it is used.
Deterministic. Needs the game's trim textures (the game folder's content/assets/materials/trim.zip).

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
# the night's steps: a map's file name suffix (nightlights.lua M.MAPS); a window with out k (> 0) is dark from step k
STEPS = ("", "_late", "_dawn")

# The plaster sheet's window openings (x0, y0, x1, y1 px of 1024, image top first), measured from the image, per band
# (buildings.py BAND_TALL, BAND_PANEL, BAND_SASH, BAND_BRICK): each one's level at night (0: dark) and the step it goes
# dark at (0: lit all night). Band 2's bays 1 and 4 are open (black) windows buildings.py never uses. The glow
# saturates the screen from ~0.3 of full (factor 0.4 looked as 1 in game, #916), so the levels go well below that to
# show. The sash band's three are also the laid-over windows' cells (buildings.py WIN_CELLS).
# (The bright ones go too, not only the dim: of the 11 lit in the evening, 4 bright, 7 are lit from 23:15 (3 bright),
# 4 from 3:00 (1 bright).)
OPENINGS = [
    [((80, 26, 198, 236), 1.0, 1), ((330, 26, 446, 236), 0, 0), ((578, 26, 988, 236), 0.12, 0)],
    [((52, 306, 202, 460), 0.06, 2), ((308, 306, 458, 460), 0, 0), ((566, 306, 716, 460), 0.18, 1),
     ((822, 306, 972, 460), 1.0, 2)],
    [((60, 566, 146, 716), 1.0, 2), None, ((466, 566, 554, 716), 0.05, 1), ((670, 566, 756, 716), 0.18, 0), None],
    [((46, 814, 154, 946), 0, 0), ((252, 814, 360, 946), 0.15, 1), ((458, 814, 566, 946), 0.05, 0),
     ((662, 814, 770, 946), 0, 0), ((866, 814, 974, 946), 1.0, 0)],
]
WARM = (1.0, 0.70, 0.38)       # incandescent / warm LED, ~2700 K
WARMER = (1.0, 0.62, 0.30)
COOL = (0.78, 0.86, 1.0)       # cool white, ~5000 K
# The metal sheet (512 px): its bands 128 px high (BAND_OFFICE: 5 bays, BAND_OFFICE2: 4, BAND_SHOP: 2, the fourth
# unused). The curtain-wall bands (CURTAIN) drawn anew, not the stock map dimmed (its lit bays glowed flat grey from edge
# to edge: towers read as grey static, #916; before, Manhattan's towers glowed white all over, TGForever Discord
# 2026-10-28): their panes (between the stock map's mullions) in rooms of 1-3 panes, OFFICE_LIT of them lit at one of
# OFFICE_LEVELS (share, level), fluorescent white or now and then warm. The shopfronts (SHOPS) the same in units of
# two panes (an awning's), each at one of SHOP_LEVELS. Each room or unit goes dark at a step drawn from OUT_SHARES.
CURTAIN, SHOPS, REST = (0, 1), 2, 3
OFFICE_LIT = 0.32
OFFICE_LEVELS = ((0.4, 0.85), (0.35, 0.25), (0.25, 0.08))
SHOP_LEVELS = ((0.4, 1.0), (0.35, 0.35), (0.25, 0.1))
REST_LEVEL = 0.6
OFFICE_WHITE, OFFICE_WARM = (0.86, 0.92, 1.0), (1.0, 0.80, 0.55)
OUT_SHARES = (0.34, 0.33, 0.33)        # lit all night, dark from 23:15, dark from 3:00 (shares of the lit)
SHOP_OUT_SHARES = (0.3, 0.45, 0.25)    # (most shops close in the evening)


def _read(zf, name):
    return Image.open(io.BytesIO(zf.read(name)))


def _lit(level, out, step):
    return level if level and (out == 0 or step < out) else 0.0


def facade(base, step=0):
    """The plaster sheet's night map at step (STEPS) from its base colour (1024 x 1024 RGB)."""
    b = np.asarray(base.convert("RGB"), np.float32) / 255.0
    luma = 0.3 * b[..., 0] + 0.59 * b[..., 1] + 0.11 * b[..., 2]
    out = np.zeros_like(b)
    k = 0
    for band in OPENINGS:
        for cell in band:
            if cell is None:
                continue
            (x0, y0, x1, y1), level, dark = cell
            k += 1
            lvl = _lit(level, dark, step)
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


def _panes(lum):
    """A band's panes (x0, x1) from its stock map's luminance (h, w): the runs of columns brighter than the mullions."""
    col = lum.mean(0)
    glass = col >= col.max() * 0.45
    edges = np.flatnonzero(np.diff(np.concatenate(([0], glass.astype(np.int8), [0]))))
    runs = [(int(x0), int(x1)) for x0, x1 in zip(edges[::2], edges[1::2]) if x1 - x0 >= 4]
    if not runs:
        return runs
    # (a run over a faint mullion is several panes: cut at the usual pane's width)
    w = float(np.median([x1 - x0 for x0, x1 in runs]))
    out = []
    for x0, x1 in runs:
        n = max(1, int(round((x1 - x0) / w)))
        out += [(x0 + (x1 - x0) * j // n, x0 + (x1 - x0) * (j + 1) // n) for j in range(n)]
    return out


def _deal(rng, n, shares):
    """n picks of shares' indices in their shares (rounded; shuffled): a band's few rooms come out at the shares, not
    as a draw happens to fall (one of the curtain bands had 1 lit room of 10 at a 32 % share)."""
    picks = np.repeat(np.arange(len(shares)), np.round(np.asarray(shares) * n).astype(int))
    picks = np.resize(picks, n) if len(picks) else np.zeros(n, int)
    return rng.permutation(picks).tolist()


def office_rooms(stock_e):
    """The metal sheet's lit rooms and shop units: [(band, panes [(x0, x1)], level, colour, out)], drawn once for every
    step (a later step lights a subset)."""
    e = np.asarray(stock_e.convert("RGB"), np.float32)
    bh = e.shape[0] // 4
    rng = np.random.default_rng(20261108)
    rooms = []
    for i in CURTAIN + (SHOPS,):
        panes = _panes(e[i * bh:(i + 1) * bh].mean(2))
        groups, k = [], 0
        while k < len(panes):
            groups.append(panes[k:k + (2 if i == SHOPS else int(rng.integers(1, 4)))])
            k += len(groups[-1])
        lit = sorted(rng.permutation(len(groups))[:len(groups) if i == SHOPS else
                                                   max(1, round(OFFICE_LIT * len(groups)))].tolist())
        levels = SHOP_LEVELS if i == SHOPS else OFFICE_LEVELS
        lv = _deal(rng, len(lit), [s for s, _ in levels])
        outs = _deal(rng, len(lit), SHOP_OUT_SHARES if i == SHOPS else OUT_SHARES)
        for g, li, o in zip(lit, lv, outs):
            col = OFFICE_WARM if (i != SHOPS and rng.random() < 0.25) else OFFICE_WHITE
            rooms.append((i, groups[g], levels[li][1], col, o))
    return rooms


def office(stock_e, rooms, step=0):
    """The metal sheet's night map at step: rooms (office_rooms) lit in the curtain-wall and shop bands, the stock
    map's shape (its lights' falloff) in each pane; the fourth band the stock map dimmed."""
    e = np.asarray(stock_e.convert("RGB"), np.float32)
    bh = e.shape[0] // 4
    out = np.zeros_like(e)
    for i in CURTAIN + (SHOPS,):
        lum = e[i * bh:(i + 1) * bh].mean(2)
        shape = np.clip(lum / max(np.percentile(lum, 98), 1.0), 0.0, 1.0)
        band, stock = out[i * bh:(i + 1) * bh], e[i * bh:(i + 1) * bh]
        for j, panes, level, col, dark in rooms:
            lvl = _lit(level, dark, step) if j == i else 0.0
            if not lvl:
                continue
            c = np.array(col, np.float32) * 255.0 * lvl
            for x0, x1 in panes:
                # (a shop: the stock map's own light, its material's factor tints it warm, nightlights.lua)
                band[:, x0:x1] = stock[:, x0:x1] * lvl if i == SHOPS else c[None, None, :] * shape[:, x0:x1, None]
    out[REST * bh:] = e[REST * bh:] * REST_LEVEL
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", default=os.path.expanduser("~/.local/share/Steam/steamapps/common/BeamNG.drive"))
    a = ap.parse_args()
    with zipfile.ZipFile(os.path.join(a.game, "content", "assets", "materials", "trim.zip")) as zf:
        base, stock = _read(zf, PLASTER), _read(zf, METAL_E)
    rooms = office_rooms(stock)
    for step, suffix in enumerate(STEPS):
        for img, name in ((facade(base, step), "bne_facade_night_e"), (office(stock, rooms, step), "bne_office_night_e")):
            p = os.path.join(OUT, name + suffix + ".png")
            img.save(p, optimize=True)
            lit = (np.asarray(img).max(2) > 8).mean()
            print(p, img.size, os.path.getsize(p), "bytes,", f"{lit:.1%} of pixels lit")


if __name__ == "__main__":
    main()
