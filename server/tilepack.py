"""Tile packs: many Web-Mercator image tiles of one zoom in one file, so the project's own tile host (Cloudflare R2
behind its CDN, the user 2026-11-07: "sure lets setup the r2 stuff") serves a whole neighbourhood of tiles in one
request (R2 bills per request; .claude/work/infra_research.md).

A pack holds the (2^k)^2 tiles of zoom z under the tile (z - k, px, py), row by row from its north-west tile:

    b"BNGP", version (u8, 1), k (u8), z (u8), 0 (u8), px (u32), py (u32), then (2^k)^2 x (offset u32, length u32) from
    the start of the data that follows the table, then the tiles' bytes (JPEG). Little-endian. Length 0: no tile there
    (no data: open sea, outside the source).

Its path under a source's base: {z}/{px}/{py}.bngp (px, py: the pack's own tile at z - k).

A source's index (index.bin next to its zoom folders; the DEM packs, dem.MT_CDN_*) says which packs exist, so a pack it
does not list is known to be empty without a request (open sea):

    b"BNGI", version (u8, 1), k (u8), max zoom (u8), 0 (u8), then per zoom z = 0 .. max zoom a bitmap of its packs'
    grid (2^(z - k) x 2^(z - k), one pack for z <= k), row by row from the north-west, bit (py * n + px) at byte >> 3,
    bit & 7 (least significant first), each zoom's bitmap starting on a byte.
"""
import struct
import dnp

MAGIC = b"BNGP"
VERSION = 1
HEAD = struct.Struct("<4sBBBBII")
INDEX_MAGIC = b"BNGI"
INDEX_HEAD = struct.Struct("<4sBBBB")


class PackError(ValueError):
    pass


def pack_of(z, x, y, k):
    """(z, px, py) of the pack holding tile (z, x, y), and the tile's index in it."""
    n = 1 << k
    return (z, x >> k, y >> k), (y & (n - 1)) * n + (x & (n - 1))


def tile_at(z, px, py, k, i):
    """The tile (z, x, y) at index i of pack (z, px, py)."""
    n = 1 << k
    return z, (px << k) + i % n, (py << k) + i // n


def path(z, px, py):
    return f"{z}/{px}/{py}.bngp"


def build(z, px, py, k, tiles):
    """A pack's bytes from tiles: {index: bytes} (missing indices: no tile)."""
    n = dnp.sq(1 << k)
    table, body, off = [], [], 0
    for i in range(n):
        b = tiles.get(i) or b""
        table.append((off, len(b)))
        body.append(b)
        off += len(b)
    return (HEAD.pack(MAGIC, VERSION, k, z, 0, px, py) + b"".join(struct.pack("<II", o, ln) for o, ln in table)
            + b"".join(body))


def parse(data):
    """(z, px, py, k, {index: bytes}) of a pack's bytes. Raises PackError on anything else."""
    if len(data) < HEAD.size:
        raise PackError("short pack")
    magic, ver, k, z, _, px, py = HEAD.unpack_from(data, 0)
    if magic != MAGIC or ver != VERSION or k > 6:
        raise PackError("not a pack")
    n = dnp.sq(1 << k)
    base = HEAD.size + 8 * n
    if len(data) < base:
        raise PackError("short pack table")
    out = {}
    for i in range(n):
        o, ln = struct.unpack_from("<II", data, HEAD.size + 8 * i)
        if ln:
            if base + o + ln > len(data):
                raise PackError("tile past the pack's end")
            out[i] = data[base + o:base + o + ln]
    return z, px, py, k, out


def _index_layout(k, max_zoom):
    """[(byte offset, grid side)] per zoom 0 .. max_zoom of an index's bitmaps, and its whole size."""
    out, off = [], INDEX_HEAD.size
    for z in range(max_zoom + 1):
        n = 1 << max(0, z - k)
        out.append((off, n))
        off += (n * n + 7) // 8
    return out, off


def index_build(k, max_zoom, packs):
    """An index's bytes from packs: an iterable of (z, px, py) that exist."""
    layout, size = _index_layout(k, max_zoom)
    b = bytearray(size)
    b[:INDEX_HEAD.size] = INDEX_HEAD.pack(INDEX_MAGIC, VERSION, k, max_zoom, 0)
    for z, px, py in packs:
        off, n = layout[z]
        if not (0 <= px < n and 0 <= py < n):
            raise PackError(f"pack {z}/{px}/{py} outside the grid")
        i = py * n + px
        b[off + (i >> 3)] |= 1 << (i & 7)
    return bytes(b)


def index_check(data):
    """(k, max zoom) of an index's bytes; PackError when it is none (or cut short)."""
    if len(data) < INDEX_HEAD.size:
        raise PackError("short index")
    magic, ver, k, max_zoom, _ = INDEX_HEAD.unpack_from(data, 0)
    if magic != INDEX_MAGIC or ver != VERSION or k > 6 or max_zoom > 20:
        raise PackError("not a pack index")
    if len(data) != _index_layout(k, max_zoom)[1]:
        raise PackError("index of the wrong size")
    return k, max_zoom


def index_has(data, z, px, py):
    """Does the index list pack (z, px, py)? False beyond its zooms or grid."""
    k, max_zoom = INDEX_HEAD.unpack_from(data, 0)[2:4]
    if not 0 <= z <= max_zoom:
        return False
    n = 1 << max(0, z - k)
    if not (0 <= px < n and 0 <= py < n):
        return False
    off = INDEX_HEAD.size + sum((dnp.sq(1 << max(0, zz - k)) + 7) // 8 for zz in range(z))
    i = py * n + px
    return bool(data[off + (i >> 3)] >> (i & 7) & 1)
