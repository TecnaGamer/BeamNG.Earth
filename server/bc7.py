"""BC7 (mode 6) encoding (the Rust kernel earthfast.bc7_encode), for the terrain's base-colour photo (detail near-visual mode).

The TerrainMaterial texture arrays are BC7 (sRGB for colour: DXGI 99, as the engine's own
temp/art/terrainmaterialcache/*.dds). Any other input - PNG, a DXT1 DDS - is encoded to BC7 by the game on
its main thread the first time it is used: 2-8 s for a 2048² photo, a stall at every terrain move. A DDS
that is already BC7 with its mip chain is taken as it is (~30 ms, in-game 2026-10-12). So the server
encodes the photo here, off the game's thread.

Mode 6 only: one RGBA endpoint pair per 4x4 block (7 bits + a shared p-bit per endpoint) and a 4-bit index
per texel. Endpoints: the block's extent along its main axis (the bounding-box diagonal, oriented to the
covariance), indices: the nearest of the 16 interpolated colours. Good for photos; no partitions (sharp
two-colour edges blur a little within a block). ~0.14 s for a 2048² image.

encode(rgb) -> bytes (blocks, row-major), decode(blocks, w, h) -> rgb (tests); dds(rgb) -> a DDS file's bytes
(DX10 header, BC7_UNORM_SRGB, full mip chain, box-filtered mips).
"""
import struct

import numpy as np

import fast

WEIGHTS = np.array([0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64], np.int32)
DXGI_BC7_UNORM_SRGB = 99


def encode(img):
    """BC7 mode-6 blocks of an (h, w, 3|4) uint8 image, row-major, 16 bytes each (the Rust encoder)."""
    img = np.asarray(img, np.uint8)
    if img.ndim != 3 or img.shape[2] not in (3, 4):
        raise ValueError("bc7.encode needs an (h, w, 3|4) image, got %r" % (img.shape,))
    fast.require(0, "bc7_encode")
    return bytes(fast._ef.bc7_encode(np.ascontiguousarray(img)))


def decode(data, w, h):
    """RGBA uint8 (h, w, 4) of BC7 mode-6 blocks (the encoder's output; other modes are not decoded)."""
    nbx, nby = (w + 3) // 4, (h + 3) // 4
    a = np.frombuffer(data, "<u8").reshape(-1, 2)[: nbx * nby]
    lo64, hi64 = a[:, 0], a[:, 1]

    def bits(pos, nb):
        v = np.zeros(len(a), np.int64)
        for b in range(nb):
            p = pos + b
            src = lo64 if p < 64 else hi64
            v |= (((src >> np.uint64(p % 64)) & np.uint64(1)).astype(np.int64)) << b
        return v
    pos = 7
    q = []
    for _ in range(8):
        q.append(bits(pos, 7))
        pos += 7
    p0, p1 = bits(pos, 1), bits(pos + 1, 1)
    pos += 2
    e0 = np.stack([(q[2 * c] << 1) | p0 for c in range(4)], 1)
    e1 = np.stack([(q[2 * c + 1] << 1) | p1 for c in range(4)], 1)
    idx = [bits(pos, 3)]
    pos += 3
    for _ in range(15):
        idx.append(bits(pos, 4))
        pos += 4
    idx = np.stack(idx, 1)
    wgt = WEIGHTS[idx]
    col = (e0[:, None, :] * (64 - wgt[..., None]) + e1[:, None, :] * wgt[..., None] + 32) >> 6
    img = col.reshape(nby, nbx, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(nby * 4, nbx * 4, 4)
    return img[:h, :w].astype(np.uint8)


def mips(img):
    """The mip chain of a square power-of-two image down to 1x1 (imagery.mip_arrays: linear light, sharpened)."""
    import imagery
    return imagery.mip_arrays(np.asarray(img, np.uint8))


_GPU_FAILED = False


def _encode_gpu(chain):
    """The chain's BC7 blocks encoded on the GPU (earthfast.bc7_encode_gpu: a 4096² photo's ~0.7 CPU-s on 8 threads ->
    ~0.05 CPU-s, the same quality), or None: no GPU (fast.gpu(): off on Windows), not RGBA, or it failed (then the
    CPU from there on)."""
    global _GPU_FAILED
    if _GPU_FAILED or not fast.gpu() or any(np.ndim(m) != 3 or np.shape(m)[2] != 4 for m in chain):
        return None
    try:
        return b"".join(bytes(b) for b in fast._ef.bc7_encode_gpu([np.ascontiguousarray(m, np.uint8) for m in chain]))
    except Exception as e:
        _GPU_FAILED = True
        import logging
        logging.getLogger("earthd").info("bc7: the GPU encoder failed (%s); the CPU's from now on", e)
        return None


def dds(rgb):
    """A BC7_UNORM_SRGB DDS (DX10 header, the full mip chain) of a square power-of-two RGB image."""
    img = np.asarray(rgb, np.uint8)
    h, w = img.shape[:2]
    chain = mips(img)
    body = _encode_gpu(chain)
    if body is None:
        body = b"".join(encode(m) for m in chain)
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | 0x80000      # CAPS HEIGHT WIDTH PIXELFORMAT MIPMAPCOUNT LINEARSIZE
    head = struct.pack("<4s7I", b"DDS ", 124, flags, h, w, max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * 16, 1, len(chain))
    head += b"\0" * 44                                                 # reserved
    head += struct.pack("<2I4s5I", 32, 0x4, b"DX10", 0, 0, 0, 0, 0)   # pixel format: FOURCC DX10
    head += struct.pack("<4I", 0x401008, 0, 0, 0)                     # caps: COMPLEX TEXTURE MIPMAP
    head += struct.pack("<I", 0)                                       # reserved2
    head += struct.pack("<5I", DXGI_BC7_UNORM_SRGB, 3, 0, 1, 0)       # DX10: format, TEXTURE2D, -, array 1, -
    return head + body
