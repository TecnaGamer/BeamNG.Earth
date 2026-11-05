"""Bulgaria's yellow paving: Sofia's жълти павета, the glazed yellow bricks of Tsar Osvoboditel and the streets round it.

Which streets are yellow comes from OpenStreetMap's colour tags (colour / surface:colour / paving_stones:colour=yellow
on a sett or paving-stone way), read once into bg_yellow_paving.json by tools/build_bg_yellow_paving.py: the server's
road tags (osmtags.py, OSM US's Layercake) have no colour. A road whose OpenStreetMap way (its src_record) is listed and
whose kind is "cobble" gets the surface token SURF; roads.look_of draws it with LOOK, the stock Italy cobblestone (the
stones in straight rows, as the bricks lie) in BeamNG.Earth's yellow copy (paving.materials.json). Its ground type is
still COBBLESTONE. Pure apart from reading the list once.
"""
import json
import os
from functools import lru_cache

import numpy as np

SURF = "yellow_paving"          # (roads.YELLOW_SURF)
MATERIAL = "bne_yellow_paving"  # (roads.YELLOW_PAVING)
PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bg_yellow_paving.json")


@lru_cache(maxsize=1)
def ways():
    """The listed OpenStreetMap way ids (str, as railtags.way_id gives them); empty if the file is missing."""
    try:
        with open(PATH, encoding="utf-8") as f:
            return frozenset(str(w) for w in json.load(f)["ways"])
    except (OSError, ValueError, KeyError):
        return frozenset()


def yellow(feats):
    """Bool array over feats: its OpenStreetMap way is a listed yellow-paved one."""
    import railtags
    out = np.zeros(len(feats), bool)
    rec = feats.attrs.get("src_record") if hasattr(feats, "attrs") else None
    listed = ways()
    if rec is None or not listed:
        return out
    for i in range(len(feats)):
        out[i] = railtags.way_id(str(rec[i])) in listed
    return out


def apply(feats, specs):
    """specs with SURF on the listed ways' cobble roads (others untouched). Pure."""
    if not len(feats):
        return specs
    hit = yellow(feats)
    if not hit.any():
        return specs
    out = list(specs)
    for j in np.flatnonzero(hit):
        if out[j] is not None and out[j].kind == "cobble":
            out[j] = out[j]._replace(surf=SURF)
    return out
