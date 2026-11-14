"""Synthetic CEDD LiDAR sheets for the Hong Kong tests (hklidar.py, roadz "hk", hkdtm.py): no network, no
government data."""
import io
import struct
import zipfile

import numpy as np


def tiff_zip(a, x0, y0, px=1.0, tile=16, compression=1):
    """A sheet as CEDD ships it: a zip of a little-endian GeoTIFF, float32, uncompressed tiles, top-left at x0, y0."""
    h, w = a.shape
    across, down = -(-w // tile), -(-h // tile)
    full = np.full((down * tile, across * tile), -9999.0, "<f4")
    full[:h, :w] = a
    tiles = [full[r * tile:(r + 1) * tile, c * tile:(c + 1) * tile].tobytes()
             for r in range(down) for c in range(across)]
    n = len(tiles)
    entries = [(256, 3, 1, w), (257, 3, 1, h), (258, 3, 1, 32), (259, 3, 1, compression), (277, 3, 1, 1),
               (322, 3, 1, tile), (323, 3, 1, tile), (324, 4, n, None), (325, 4, n, None), (339, 3, 1, 3),
               (33550, 12, 3, None), (33922, 12, 6, None)]
    ifd = 8
    extra = ifd + 2 + 12 * len(entries) + 4
    blobs = {324: None, 325: struct.pack(f"<{n}I", *[len(t) for t in tiles]),
             33550: struct.pack("<3d", px, px, 0.0), 33922: struct.pack("<6d", 0.0, 0.0, 0.0, x0, y0, 0.0)}
    data_at = extra + 4 * n + 4 * n + 24 + 48
    blobs[324] = struct.pack(f"<{n}I", *[data_at + i * len(tiles[0]) for i in range(n)])
    out, at, tail = bytearray(b"II*\x00" + struct.pack("<I", ifd)), extra, b""
    out += struct.pack("<H", len(entries))
    for tag, typ, cnt, val in entries:
        if val is not None:
            out += struct.pack("<HHIHH", tag, typ, cnt, val, 0)
        else:
            out += struct.pack("<HHII", tag, typ, cnt, at)
            at += len(blobs[tag])
            tail += blobs[tag]
    out += struct.pack("<I", 0) + tail + b"".join(tiles)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("SHEET.tif", bytes(out))
    return buf.getvalue()
