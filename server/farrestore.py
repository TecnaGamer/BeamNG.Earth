"""A cached place's far field put back in bulk (2026-11-12): the glue around earthfast's far_restore (farrestore.rs).

Why: on a warm revisit each far tile still went through the whole per-tile path one at a time: the far gate (3 wide
while driving, 1 while a block builds in the server), a far-tile worker's round trip (its revisit record read, the
geometry unpickled and pickled back, ~470 KB a tile), the shape encoded again in the main interpreter (~6 ms of
numpy and kernels, 12-60 ms under the event loop's other threads) and, for a global tile, its photo rendered raw in
the worker and again finished in the main interpreter. A cached Fells Point far field took 50-75 s on the PC under
load (2026-11-12, ~540 tiles).

Now:
- "farfin" items: a far tile's finished shape (encoded once, in the worker that built it, with PLACEHOLDER as its
  material name: the session's pooled names differ), its height grid and its numbers (pos, err, sink, skirt, err_n,
  for a tile in an anchor's frame its texture's bytes and sources), its texture and road mask kept as files beside
  it (revisit.SIDE_KINDS). Key: the tile's own revisit key (blockworker: the "far" key of an anchor's tile, the
  "fargeo" key of a global one), its texture size and the material name's length. A global tile's texture is the
  finished one the server kept as "fartex" (Server.retexture: keep_texture).
- Server.restore_far (a thread of its own): the far worker finds which of the selection's tiles have one
  (blockworker.far_restore_find: the keys need the worker's map data, as far_tile's do), then one native call reads
  their records from the packs, stamps each tile's pooled material name in, compresses and writes the shapes, links
  the slot paths and the textures, on RESTORE_THREADS threads with the GIL released. The tiles go to the game as
  before (one far_tile each, in the streamer's order; the mod's governor paces the imports).

No Python twin: fast.require(NATIVE)."""
import json
import os

import numpy as np

import fast
import revisit

NATIVE = 270
KIND = "farfin"
ON = os.environ.get("BNGE_FAR_RESTORE", "on") != "off"
RESTORE_THREADS = max(1, min(4, (os.cpu_count() or 2) // 2))
CHUNK = 96              # tiles a native call puts back (the nearest first: the game gets them as each chunk is done)
FIRST_CHUNK = 32        # ... the first call's (a place not kept: 32 tiles' keys looked up before the usual way)
MIN_PX = 32             # the smallest kept texture size a tile is put back with (restore_far: sharpened when sent)
MIN_BATCH = 4           # fewer candidates than this: the usual path


def placeholder(n):
    """The material name a kept shape carries (n characters, the session's pooled names' length): stamped over with
    the tile's own name when put back."""
    base = "bngEarthFIN"
    return (base + "~" * n)[:n] if n >= len(base) else "~" * n


def key(base_key, px, name_len):
    """A farfin item's key: the tile's own revisit key (an anchor's tile: its "far" key; a global tile: its "fargeo"
    key), its texture's size (the shape's UV gutter) and the material name's length."""
    return revisit.key(KIND, base_key, int(px), int(name_len))


def keep(cache_dir, k, built, px, name_len, sides=None, extra=None):
    """A worker's far tile kept as a farfin item (its shape encoded here with the placeholder name, plain: the pack
    compresses the record, the restore the shape's body). sides: {"tex.dds" / "mask.dds": revisit.Side} kept beside
    it; extra: more numbers (tex_bytes, used). The plain shape goes back to the server with the tile
    (built.shape_plain: (bytes, name_len, px)): it stamps its name in (stamp) instead of encoding the mesh again.
    Never raises; False when not kept."""
    try:
        import farfield
        plain = farfield.built_shape(built, placeholder(name_len), px, packed=False)
        built.shape_plain = (plain, int(name_len), int(px))
        grid = b"" if built.grid is None else np.ascontiguousarray(built.grid, np.float32).tobytes()
        gshape = None if built.grid is None else list(np.shape(built.grid))
        meta = {"pos": [float(v) for v in built.pos], "err": float(built.err),
                "sink": None if built.sink is None else float(built.sink), "skirt": float(built.skirt),
                "err_n": None if built.err_n is None else float(built.err_n), "gshape": gshape,
                "sides": sorted((sides or {}).keys())}
        meta.update(extra or {})
        blobs = {"shape": plain, "grid": grid}
        blobs.update(sides or {})
        return revisit.store(cache_dir, KIND, k, blobs, meta)
    except Exception:
        return False


def has(cache_dir, k):
    return revisit.has(cache_dir, KIND, k)


def locate(cache_dir, k):
    """revisit.locate of a farfin item: (loc, {side name: kept file}) or None."""
    return revisit.locate(cache_dir, KIND, k)


def restore(items, warm_read=False):
    """Put far tiles back (earthfast far_restore): items [(loc, placeholder, material, (shape tmp, shape path, slot
    tmp, slot path), [(kept file, tmp, path), ...])] -> per item (shape bytes, extra dict, grid float32 array or None)
    or a str: why not (that tile is built the usual way)."""
    fast.require(NATIVE, "the far field's bulk restore (far_restore)")
    out = []
    for ok, size, text, grid in fast._ef.far_restore(items, RESTORE_THREADS, bool(warm_read)):
        if not ok:
            out.append(text)
            continue
        try:
            extra = json.loads(text)
            gshape = extra.get("gshape")
            g = np.frombuffer(grid, np.float32).reshape(gshape).copy() if gshape else None
            out.append((int(size), extra, g))
        except (ValueError, TypeError) as e:
            out.append(f"extra: {e}")
    return out


def stamp(plain, name_len, material):
    """A shape file's bytes from a kept plain shape (its placeholder replaced by material, compressed as written)."""
    fast.require(NATIVE, "far_shape_stamp")
    return fast._ef.far_shape_stamp(plain, placeholder(name_len), material)
