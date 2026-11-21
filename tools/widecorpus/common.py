"""Scratch layout and run identity for the wide road corpus."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

HERE = Path(__file__).resolve().parent
TREE = HERE.parents[1]
ROOT = Path.home() / "bnge-tests/widecorpus"
CACHE = ROOT / "cache"
PRIVATE = ROOT / "places_private.json"
RAW = ("dem", "mapdata", "pagesdata", "roadz", "gravity", "cdn", "airports")
REVAMPED = {f"BNE_{name}": "on" for name in (
    "SPEED_TAPERS", "DECK_K_SPEED", "FOOT_CLEAR", "RAIL_LIFECYCLE", "RACEWAY_DIG",
    "TUNNEL_ROAD_OVER", "FAST_DECK_ENDS", "FAST_GAP", "FAST_DECK_GROUND",
    "RACEWAY_PROPS", "RAIL_ROAD_FIT", "RAIL_SEAM_NATURAL", "SURFACE_V2", "SIDEWALK_V2")}
REVAMPED.update(BNE_BATTER="vanilla", BNE_ROADSHAPE="sheet", BNE_ROADSURF_V3="surface")


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, sort_keys=True, indent=1, allow_nan=False) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=path.name+".", suffix=".tmp", delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(serialized)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


def places(ids=None, public_only=False):
    data = read(HERE / "places.json")
    if not public_only and PRIVATE.exists():
        data += read(PRIVATE)
    by_id = {}
    for p in data:
        key = p["id"]
        if not re.fullmatch("[a-z0-9-]+", key) or key in by_id or not (
                -85 < p["lat"] < 85 and -180 <= p["lon"] <= 180):
            raise ValueError(f"invalid or duplicate place {key}")
        by_id[key] = p
    return [by_id[i] for i in ids.split(",")] if ids else data
