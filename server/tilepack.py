"""Tile packs: many Web-Mercator image tiles of one zoom in one file, so the project's own tile host (Cloudflare R2
behind its CDN, the user 2026-11-07: "sure lets setup the r2 stuff") serves a whole neighbourhood of tiles in one
request (R2 bills per request; .claude/work/infra_research.md).

A pack holds the (2^k)^2 tiles of zoom z under the tile (z - k, px, py), row by row from its north-west tile:

    b"BNGP", version (u8, 1), k (u8), z (u8), 0 (u8), px (u32), py (u32), then (2^k)^2 x (offset u32, length u32) from
    the start of the data that follows the table, then the tiles' bytes (JPEG). Little-endian. Length 0: no tile there
    (no data: open sea, outside the source).

Its path under a source's base: {z}/{px}/{py}.bngp (px, py: the pack's own tile at z - k).
"""
import struct

MAGIC = b"BNGP"
VERSION = 1
HEAD = struct.Struct("<4sBBBBII")


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
    n = (1 << k) ** 2
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
    n = (1 << k) ** 2
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
