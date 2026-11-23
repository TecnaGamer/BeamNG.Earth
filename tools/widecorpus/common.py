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
def _revamped():
    """The Experimental "Revamped roads" switches, read from the server's own table (server/experimental.py FEATURES),
    so the corpus always tests exactly what players get (2026-11-21: a hard-coded copy kept the surface overlay and
    the new sidewalks after they moved to their own toggles)."""
    import ast
    tree = ast.parse((TREE / "server" / "experimental.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "FEATURES" for t in node.targets):
            for entry in ast.literal_eval(node.value):
                if entry[0] == "expRoadRevamp":
                    return dict(entry[1])
    raise RuntimeError("expRoadRevamp not found in server/experimental.py")


REVAMPED = _revamped()


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


def places(ids=None, public_only=False, extra=()):
    """The catalogue's places, the private ones unless public_only, and those of every extra file (JSON arrays kept
    outside Git, such as the bridge study's sites: tools/widecorpus/run.py --extra)."""
    data = read(HERE / "places.json")
    if not public_only and PRIVATE.exists():
        data += read(PRIVATE)
    for path in extra:
        data += read(Path(path))
    by_id = {}
    for p in data:
        key = p["id"]
        if not re.fullmatch("[a-z0-9-]+", key) or key in by_id or not (
                -85 < p["lat"] < 85 and -180 <= p["lon"] <= 180):
            raise ValueError(f"invalid or duplicate place {key}")
        by_id[key] = p
    return [by_id[i] for i in ids.split(",")] if ids else data
