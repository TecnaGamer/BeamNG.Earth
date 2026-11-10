"""The German Autobahn signs' lettering atlas, read for the kernel (earthfast autobahn_signs): DINish, DINish Condensed
and the RWB/RWBA pictograms in de_autobahn/de_letters_o.data.png, drawn by tools/build_de_autobahn_atlas.py into
server/data/de_sign_font.json. No fallback to the shared sign atlas (signmesh.py): a missing character is reported by
coverage() and left out by the kernel."""
from functools import lru_cache
import json
from pathlib import Path

import numpy as np

PATH = Path(__file__).resolve().parent / "data" / "de_sign_font.json"
FACE_TEXT, FACE_CONDENSED, FACE_PICTOGRAM = 0, 1, 2
# the kernel's material ids index this tuple: the atlas' letter materials, the flat panel colours, the gantries' metal
MATERIALS = ("bne_de_letters_white", "bne_de_letters_black", "bne_de_blue", "bne_de_white", "bne_de_yellow",
             "bne_de_green", "bne_de_black", "bne_exit_metal")
WHITE_INK_BELOW = 0xE010     # pictograms below are white ink on the blue panel, from here black ink on a white square


@lru_cache(maxsize=1)
def _data():
    return json.loads(PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def tables():
    """(cps int64 (g,), glyphs float64 (g, 9) [advance, left, bottom, right, top, u0, v0, u1, v1] in cap units y up,
    faces int64 (g,), face_params float64 (3, 2) [horizontal_scale, letter_spacing], kern float64 (k, 4)
    [face, cp_left, cp_right, adjust]). C-contiguous, as the kernel takes them."""
    d = _data()
    rows = np.asarray(d["glyphs"], np.float64)
    cps = np.ascontiguousarray(rows[:, 1].astype(np.int64))
    faces = np.ascontiguousarray(rows[:, 0].astype(np.int64))
    glyphs = np.ascontiguousarray(rows[:, 2:11])
    params = np.ascontiguousarray(np.asarray(d["face_params"], np.float64).reshape(3, 2))
    kern = np.ascontiguousarray(np.asarray(d["kerning"], np.float64).reshape(-1, 4))
    return cps, glyphs, faces, params, kern


def _pictograms():
    return {k: int(v) for k, v in sorted(_data()["pictograms"].items())}


PICTOGRAMS = _pictograms()      # OSM destination:symbol value -> the pictogram's code point (face 2)


def coverage(text, face=FACE_TEXT):
    """The characters of text (spaces aside) the face has no glyph for."""
    cps, _, faces, _, _ = tables()
    have = set(cps[faces == face].tolist())
    return {ch for ch in text if not ch.isspace() and ord(ch) not in have}
