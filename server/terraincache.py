"""The game's terrain texture cache, pre-filled by the server (detail near-visual mode, 2026-10-12).

A TerrainMaterial's textures reach the GPU as BC7 texture arrays. The game packs each source map it has not
seen before into BC7 on its main thread and keeps the result in <user>/temp/art/terrainMaterialCache/
<KEY>.dds: 1.5 s for a 1024² photo, a stall at every terrain move in detail mode, where every block has a new
photo. Its log names the cases: "Texture added for packing, because cache not found / cache outdated / size
differs from cache". A cache file that exists, is newer than its sources and has their size is used as it is.

The key, from the 0.39.4 binary (the function formatting "art/terrainMaterialCache/%s.png"; its hasher's
constants are XXH64's, seed 0) and checked against a cache miss in the game's log: XXH64 of the four
source paths of the packed texture, concatenated. The base-colour texture packs RGB from baseColorBaseTex
and A from aoBaseTex, so its key is xxh64(base + base + base + ao) (paths exactly as the material's fields,
VFS form), printed as 16 hex digits; the files on disk are lower case.

base_entry(user_dir, base_vfs, ao_vfs, rgb, ao_value) writes that cache file for a photo (BC7 sRGB with its
mips, as the game's own: DXGI 99, bc7.py), after the photo itself exists. Returns its path.
"""
import os
import struct

import numpy as np
import staging

import bc7

M64 = 0xFFFFFFFFFFFFFFFF
P1, P2, P3, P4, P5 = (11400714785074694791, 14029467366897019727, 1609587929392839161, 9650029242287828579,
                      2870177450012600261)


def _rotl(x, r):
    return ((x << r) | (x >> (64 - r))) & M64


def _round(acc, v):
    acc = (acc + v * P2) & M64
    return (_rotl(acc, 31) * P1) & M64


def _merge(acc, v):
    acc ^= _round(0, v)
    return (acc * P1 + P4) & M64


def xxh64(data, seed=0):
    """XXH64 of bytes (pure Python; the keys are a few hundred bytes)."""
    n, i = len(data), 0
    if n >= 32:
        v1, v2, v3, v4 = (seed + P1 + P2) & M64, (seed + P2) & M64, seed, (seed - P1) & M64
        while i + 32 <= n:
            a, b, c, d = struct.unpack_from("<4Q", data, i)
            v1, v2, v3, v4 = _round(v1, a), _round(v2, b), _round(v3, c), _round(v4, d)
            i += 32
        h = (_rotl(v1, 1) + _rotl(v2, 7) + _rotl(v3, 12) + _rotl(v4, 18)) & M64
        for v in (v1, v2, v3, v4):
            h = _merge(h, v)
    else:
        h = (seed + P5) & M64
    h = (h + n) & M64
    while i + 8 <= n:
        h ^= _round(0, struct.unpack_from("<Q", data, i)[0])
        h = (_rotl(h, 27) * P1 + P4) & M64
        i += 8
    if i + 4 <= n:
        h ^= (struct.unpack_from("<I", data, i)[0] * P1) & M64
        h = (_rotl(h, 23) * P2 + P3) & M64
        i += 4
    while i < n:
        h ^= (data[i] * P5) & M64
        h = (_rotl(h, 11) * P1) & M64
        i += 1
    h ^= h >> 33
    h = (h * P2) & M64
    h ^= h >> 29
    h = (h * P3) & M64
    return h ^ (h >> 32)


def base_key(base_vfs, ao_vfs):
    """The cache file name (without .dds) of a base-colour texture: 16 lower-case hex digits."""
    return f"{xxh64((base_vfs * 3 + ao_vfs).encode()):016x}"


def cache_dir(user_dir):
    return os.path.join(user_dir, "temp", "art", "terrainmaterialcache")


def stored_png(rgb):
    """PNG bytes of an RGB image (uint8 [h, w, 3]): rows unfiltered, deflate level 0 (stored). The base photo's file,
    which the game reads only when its packed cache entry is stale: Pillow at level 0 still filtered every row (0.43 s
    for a 4096² photo, this 0.10 s; 2026-10-28)."""
    import struct
    import zlib
    rgb = np.ascontiguousarray(rgb, np.uint8)
    h, w = rgb.shape[:2]
    raw = np.empty((h, w * 3 + 1), np.uint8)
    raw[:, 0] = 0
    raw[:, 1:] = rgb.reshape(h, -1)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), 0)) + chunk(b"IEND", b""))


# The base photo's own file: a JPEG (4:4:4, PHOTO_QUALITY), the game's packed cache entry made from the exact pixels
# (base_entry). A stored PNG was 12.6 MB a 2048² photo, two or three a block (the stand-in, the photo, a refresh): ~3.4 GB
# of a warm 8 min drive at 150 mph; this ~1.2 MB in 19 ms (the PNG 13 ms). BNGE_PHOTO_PNG=1: the PNG (A/B)
PHOTO_PNG = os.environ.get("BNGE_PHOTO_PNG") == "1"
PHOTO_QUALITY = 92
PHOTO_EXT = ".png" if PHOTO_PNG else ".jpg"
# (tests: BNGE_NO_BASE_PACK=1 writes no cache entry, so the game packs the photo file itself)
NO_PACK = os.environ.get("BNGE_NO_BASE_PACK") == "1"


def stored_photo(rgb):
    """The base photo's file bytes (PHOTO_EXT): a JPEG, or stored_png with BNGE_PHOTO_PNG=1."""
    if PHOTO_PNG:
        return stored_png(rgb)
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb, np.uint8), "RGB").save(b, "JPEG", quality=PHOTO_QUALITY, subsampling=0)
    return b.getvalue()


def base_entry(user_dir, base_vfs, ao_vfs, rgb, ao_value):
    """Write the packed base texture (RGB = the photo, A = the flat AO value) into the game's cache, atomically.
    Call after the photo file itself is written (the cache must be the newer one)."""
    if NO_PACK:
        return None
    d = cache_dir(user_dir)
    os.makedirs(d, exist_ok=True)
    rgb = np.asarray(rgb, np.uint8)
    rgba = np.concatenate([rgb, np.full(rgb.shape[:2] + (1,), ao_value, np.uint8)], 2)
    path = os.path.join(d, base_key(base_vfs, ao_vfs) + ".dds")
    tmp = staging.tmp_for(path)
    with open(tmp, "wb") as f:
        f.write(bc7.dds(rgba))
    staging.replace(tmp, path)
    return path
