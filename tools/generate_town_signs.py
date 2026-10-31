"""The body every town entry and exit sign of a country shares (server/townsigns.py; the mod's townsigns.lua): two
posts and the plate's bare-metal back, in this mod's art folder. Each commune's face (the plate's front with its name)
is made by the server at run time (townsigns.write_face): only the bodies are files of the mod. France's plate is
PLATE_W x PLATE_H; a country whose plate has other proportions has its own body (PLATES: Belgium's F1a is 1.67 wide
for 1 high, Wikimedia Commons' drawing of it).

    python tools/generate_town_signs.py    -> mod/.../art/shapes/beamngEarth/sign_town_body.dae, sign_town_body_be.dae

It uses the speed-limit signs' generator (generate_kmh_speed_signs.py: its mesh, levels of detail, metal material and
Collada writer), so the two kinds of sign are built and lit alike. Keep PLATE_* and SIGN_H in step with
server/townsigns.py (server/tests/test_townsigns.py checks it).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_kmh_speed_signs as kmh  # noqa: E402

BODY_NAME = 'sign_town_body'
PLATE_W, PLATE_H, PLATE_Z0 = 1.3, 0.6, 1.4     # the plate (m): server townsigns.PLATE_W, PLATE_H, PLATE_Z0
SIGN_H = 2.1                                    # the posts' tops: server townsigns.SIGN_H
PLATE_Y_FRONT, PLATE_Y_BACK = -0.006, 0.006     # the plate's thickness (the face is in front of it, at -0.012)
POST_X = 0.45                                   # the posts' centres either side of the plate's middle
POST_R = 0.03
POST_GAP = 0.02                                 # clear space between the plate's back and the posts
# country (lower case, its body's suffix) -> (plate width, posts' centres either side): server townsigns.PLATES
PLATES = {'be': (1.0, 0.33), 'lu': (1.74, 0.6), 'de': (0.9, 0.3), 'ch': (0.84, 0.28), 'it': (2.0, 0.7),
          'at': (0.9, 0.3), 'nl': (2.0, 0.7), 'es': (1.8, 0.6), 'pt': (1.6, 0.55), 'cz': (1.2, 0.4), 'sk': (1.0, 0.33),
          'hu': (1.6, 0.55), 'si': (1.0, 0.33), 'dk': (1.2, 0.4), 'se': (1.2, 0.4), 'fi': (1.0, 0.33),
          'pl': (1.0, 0.33), 'ee': (1.0, 0.33), 'lv': (1.6, 0.55), 'lt': (1.6, 0.55), 'ro': (1.4, 0.48),
          'bg': (1.2, 0.4), 'mk': (1.0, 0.33), 'rs': (1.4, 0.48), 'gr': (1.4, 0.48), 'hr': (1.2, 0.4), 'ba': (1.2, 0.4),
          'no': (1.4, 0.48), 'is': (1.0, 0.33), 'me': (1.2, 0.4), 'al': (1.4, 0.48), 'xk': (1.2, 0.4), 'md': (1.2, 0.4),
          'ad': (1.8, 0.6), 'li': (0.84, 0.28), 'sm': (2.0, 0.7), 'ua': (1.6, 0.55), 'by': (1.2, 0.4),
          'gb': (1.6, 0.55), 'ie': (1.6, 0.55), 'mt': (1.0, 0.33), 'cy': (1.4, 0.48), 'us': (1.2, 0.4),
          'ca': (1.4, 0.48), 'mx': (1.8, 0.6), 'gt': (1.8, 0.6), 'bz': (1.8, 0.6), 'sv': (1.8, 0.6), 'hn': (1.8, 0.6),
          'ni': (1.8, 0.6), 'cr': (1.8, 0.6), 'pa': (1.8, 0.6), 'cu': (1.4, 0.48), 'ht': (1.4, 0.48), 'do': (1.8, 0.6),
          'pr': (1.8, 0.6), 'jm': (1.8, 0.6), 'bs': (1.8, 0.6), 'tt': (1.8, 0.6), 'bb': (1.8, 0.6), 'lc': (1.8, 0.6),
          'vc': (1.8, 0.6), 'gd': (1.8, 0.6), 'dm': (1.8, 0.6), 'ag': (1.8, 0.6), 'kn': (1.8, 0.6), 'aw': (2.0, 0.7),
          'ar': (1.8, 0.6), 'bo': (1.8, 0.6), 'py': (1.8, 0.6), 'uy': (1.8, 0.6), 'br': (1.8, 0.6), 'cl': (1.8, 0.6),
          'co': (1.8, 0.6), 'pe': (1.8, 0.6), 'ec': (1.8, 0.6), 've': (1.8, 0.6), 'gy': (1.8, 0.6), 'sr': (1.8, 0.6),
          'ke': (1.6, 0.55), 'ug': (1.6, 0.55), 'tz': (1.6, 0.55), 'ng': (1.6, 0.55), 'gh': (1.6, 0.55),
          'sl': (1.6, 0.55), 'lr': (1.6, 0.55), 'gm': (1.6, 0.55), 'za': (1.6, 0.55), 'na': (1.6, 0.55),
          'bw': (1.6, 0.55), 'zw': (1.6, 0.55), 'zm': (1.6, 0.55), 'mw': (1.6, 0.55), 'ls': (1.6, 0.55),
          'sz': (1.6, 0.55), 'so': (1.6, 0.55), 'ss': (1.6, 0.55), 'mu': (1.6, 0.55), 'sc': (1.6, 0.55),
          'ao': (1.6, 0.55), 'mz': (1.6, 0.55), 'gw': (1.6, 0.55), 'cv': (1.6, 0.55), 'st': (1.6, 0.55),
          'gq': (1.8, 0.6), 'ma': (1.4, 0.48), 'dz': (1.4, 0.48), 'tn': (1.4, 0.48), 'ly': (1.4, 0.48),
          'mr': (1.4, 0.48), 'eg': (1.4, 0.48), 'sd': (1.4, 0.48), 'et': (1.4, 0.48), 'er': (1.4, 0.48),
          'tr': (1.4, 0.48), 'sy': (1.4, 0.48), 'lb': (1.4, 0.48), 'jo': (1.4, 0.48), 'iq': (1.4, 0.48),
          'sa': (1.4, 0.48), 'kw': (1.4, 0.48), 'qa': (1.4, 0.48), 'om': (1.4, 0.48), 'ge': (1.4, 0.48),
          'am': (1.4, 0.48), 'bh': (1.4, 0.48), 'ae': (1.4, 0.48), 'ye': (1.4, 0.48), 'il': (1.4, 0.48),
          'ir': (1.4, 0.48), 'az': (1.4, 0.48), 'xw': (1.4, 0.48), 'uz': (1.4, 0.48), 'tm': (1.4, 0.48),
          'af': (1.4, 0.48), 'in': (1.4, 0.48), 'bd': (1.4, 0.48), 'lk': (1.4, 0.48), 'kr': (1.4, 0.48),
          'mm': (1.4, 0.48), 'la': (1.4, 0.48), 'kh': (1.4, 0.48), 'tl': (1.4, 0.48), 'id': (1.4, 0.48),
          'mn': (1.4, 0.48), 'bn': (1.4, 0.48), 'au': (1.4, 0.48), 'pg': (1.4, 0.48), 'fj': (1.4, 0.48),
          'vu': (1.4, 0.48), 'to': (1.4, 0.48), 'cn': (1.4, 0.48),
          'jp': (1.4, 0.48), 'my': (1.4, 0.48), 'pk': (1.4, 0.48), 'np': (1.4, 0.48), 'hk': (1.4, 0.48),
          'ph': (1.4, 0.48), 'bt': (1.4, 0.48), 'mv': (1.4, 0.48), 'kp': (1.4, 0.48), 'sb': (1.4, 0.48),
          'ws': (1.4, 0.48), 'gu': (1.4, 0.48), 'vn': (1.4, 0.48), 'ru': (1.4, 0.48), 'kz': (1.4, 0.48),
          'kg': (1.4, 0.48), 'tj': (1.4, 0.48), 'tw': (1.4, 0.48), 'th': (1.4, 0.48), 'nz': (1.4, 0.48),
          'im': (1.6, 0.55), 'je': (1.6, 0.55), 'gg': (1.6, 0.55), 'gi': (1.6, 0.55), 'bm': (1.6, 0.55),
          'ky': (1.6, 0.55), 'vg': (1.6, 0.55), 'ai': (1.6, 0.55), 'ms': (1.6, 0.55), 'tc': (1.6, 0.55),
          'fk': (1.6, 0.55), 'sh': (1.6, 0.55), 'io': (1.6, 0.55), 'pn': (1.6, 0.55), 'vi': (1.2, 0.4),
          'as': (1.2, 0.4), 'mp': (1.2, 0.4), 'cw': (2.0, 0.7), 'sx': (2.0, 0.7), 'bq': (2.0, 0.7), 'ax': (1.0, 0.33),
          'fo': (1.2, 0.4), 'gl': (1.2, 0.4), 'sj': (1.4, 0.48), 'va': (2.0, 0.7), 'eh': (1.4, 0.48), 'nf': (1.4, 0.48),
          'cx': (1.4, 0.48), 'cc': (1.4, 0.48), 'ck': (1.4, 0.48), 'nu': (1.4, 0.48), 'tk': (1.4, 0.48),
          'fm': (1.4, 0.48), 'mh': (1.4, 0.48), 'pw': (1.4, 0.48), 'nr': (1.4, 0.48), 'tv': (1.4, 0.48),
          'ki': (1.4, 0.48)}


def build_town_body(lod: kmh.SignLod, plate_w: float = PLATE_W, post_x: float = POST_X) -> kmh.Mesh:
    """The body, Z up, the face's side toward -Y: the plate's back and edges (no front: the face is it) and two
    capped posts behind it."""
    m = kmh.Mesh(lod.segments)
    x, z0, z1 = plate_w / 2, PLATE_Z0, PLATE_Z0 + PLATE_H
    start = len(m.tri['metal'])
    m.box('metal', -x, x, PLATE_Y_FRONT, PLATE_Y_BACK, z0, z1)
    kmh.drop_front_facing(m, 'metal', start)
    post_y = PLATE_Y_BACK + POST_GAP + POST_R
    for px in (-post_x, post_x):
        m.cylinder('metal', (px, post_y), POST_R, 0.0, SIGN_H, 'z')
        m.disc('metal', (px, post_y, SIGN_H), POST_R, (0, 0, 1), 'xy')
        m.disc('metal', (px, post_y, 0.0), POST_R, (0, 0, -1), 'xy')
        if lod.brackets:            # a clamp across each post at the plate's top and foot
            for zc in (z0 + 0.08, z1 - 0.08):
                start = len(m.tri['metal'])
                m.box('metal', px - POST_R - 0.004, px + POST_R + 0.004, PLATE_Y_BACK, post_y + POST_R + 0.006,
                      zc - 0.02, zc + 0.02)
                kmh.drop_front_facing(m, 'metal', start)
    return m


def write_body(suffix: str = '', plate_w: float = PLATE_W, post_x: float = POST_X) -> Path:
    name = BODY_NAME + (f'_{suffix}' if suffix else '')
    path = kmh.OUT / f'{name}.dae'
    lods = [(lod, build_town_body(lod, plate_w, post_x)) for lod in kmh.SIGN_LODS]
    path.write_text(kmh.dae_xml(name, None, lods), encoding='utf-8')
    return path


if __name__ == '__main__':
    print(f'  {write_body()}')
    for cc, (w, px) in PLATES.items():
        print(f'  {write_body(cc, w, px)}')
