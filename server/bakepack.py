"""Baked world packs (2026-11-02, the user: "drive through NYC at 500 mph on all devices"; first visits too).

A first visit's heavy, map-only work is baked offline (tools/bake_area.py; a GitHub Actions job of the data repo) and
shipped as one file per region: each map cell's road plans (grading: 10-30 CPU-s a dense cell) and road facts
(roads._cell_ranges / road_specs: ~2 CPU-s). At 500 mph a run down Manhattan needs a cell every ~8 s, more than a
server's four threads could plan.

A pack is valid only for the inputs it was made from (its "meta"): the map data's versions (Overture release, lanes,
rails ...), the plans' signature and frames, and the formats (plancache.PLANS_FORMAT, roads.FACTS_FORMAT: bumped when
their output changes, tests/test_bake_formats.py checks it) instead of the revisit cache's code hash. A pack whose meta
differs is not read.

The file: MAGIC, the index's length (8 bytes, little endian), the index (JSON: {"meta": {...}, "items": {key:
[offset, length]}}, offsets from the end of the index), then the items. Keys: "plans/<sig>/<z>_<x>_<y>" (plancache's
npz bytes) and "facts/<kind>/<extra>/<cell>" (roads' facts JSON). Packs live in <cache>/bake/*.bnge.
"""
import json
import logging
import os
import struct
import threading

log = logging.getLogger("earthd.bakepack")

MAGIC = b"BNGEBAKE1\n"
SUBDIR = "bake"
SUFFIX = ".bnge"
BAKE_FORMAT = 1

_lock = threading.Lock()
_packs = {}             # cache dir -> [Pack]
_seen = {}              # cache dir -> the pack files' (name, size, mtime) when last listed


def meta_for(md, signature, frames):
    """The meta a pack made with map data md and plans signature / frames carries (and a reader expects)."""
    import plancache
    import roads
    return {"bake_format": BAKE_FORMAT, "plans_format": plancache.PLANS_FORMAT, "facts_format": roads.FACTS_FORMAT,
            "frames": frames, "signature": signature,
            "versions": json.loads(json.dumps(md.versions(), sort_keys=True)) if md is not None else None}


class Writer:
    """Items into a pack file (bake_area.py): add(key, bytes), then close(meta)."""

    def __init__(self, path):
        self.path = path
        self.tmp = path + ".tmp"
        self.body = open(self.tmp + ".body", "wb")
        self.items = {}
        self.off = 0

    def add(self, key, data):
        if key in self.items:
            return
        self.body.write(data)
        self.items[key] = [self.off, len(data)]
        self.off += len(data)

    def close(self, meta):
        self.body.close()
        index = json.dumps({"meta": meta, "items": self.items}, sort_keys=True).encode("utf-8")
        with open(self.tmp, "wb") as out, open(self.tmp + ".body", "rb") as body:
            out.write(MAGIC + struct.pack("<Q", len(index)) + index)
            while True:
                chunk = body.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        os.remove(self.tmp + ".body")
        os.replace(self.tmp, self.path)
        return len(self.items)


class Pack:
    """One pack file, read in place (its index in memory)."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            if f.read(len(MAGIC)) != MAGIC:
                raise ValueError(f"{path}: not a bake pack")
            n, = struct.unpack("<Q", f.read(8))
            index = json.loads(f.read(n).decode("utf-8"))
        self.base = len(MAGIC) + 8 + n
        self.meta = index.get("meta") or {}
        self.items = index.get("items") or {}

    def get(self, key):
        at = self.items.get(key)
        if at is None:
            return None
        with open(self.path, "rb") as f:
            f.seek(self.base + at[0])
            data = f.read(at[1])
        return data if len(data) == at[1] else None


def packs(cache_dir):
    """The valid-looking packs of a cache (listed again when the folder changes)."""
    d = os.path.join(cache_dir, SUBDIR)
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(SUFFIX))
        stamp = tuple((n, os.path.getsize(os.path.join(d, n)), os.path.getmtime(os.path.join(d, n))) for n in names)
    except OSError:
        return []
    with _lock:
        if _seen.get(cache_dir) == stamp:
            return _packs.get(cache_dir, [])
        out = []
        for n in names:
            try:
                out.append(Pack(os.path.join(d, n)))
            except (OSError, ValueError) as e:
                log.warning("bake pack %s: %s", n, e)
        _packs[cache_dir], _seen[cache_dir] = out, stamp
        if out:
            log.info("bake packs: %s", ", ".join(f"{os.path.basename(p.path)} ({len(p.items)} items)" for p in out))
        return out


def _matches(pack, expect):
    m = pack.meta
    return all(m.get(k) == v for k, v in expect.items())


def find(cache_dir, key, expect):
    """The bytes of item key from a pack whose meta has every (key, value) of expect, or None."""
    if not cache_dir:
        return None
    for p in packs(cache_dir):
        if key in p.items and _matches(p, expect):
            got = p.get(key)
            if got is not None:
                return got
    return None


def plans_key(signature, cell):
    return f"plans/{signature}/{int(cell[0])}_{int(cell[1])}_{int(cell[2])}"


def facts_key(kind, extra, cell):
    return f"facts/{kind}/{json.dumps(extra)}/{cell}"
