#!/usr/bin/env python3
"""make_paint_dds.py: the runway / apron paint textures (mod: levels/beamng_earth/art/runway/t_paint_*_b.color.dds) of a solid colour.

    python tools/make_paint_dds.py black 16 16 16       # writes t_paint_black_b.color.dds next to the yellow one
    python tools/make_paint_dds.py red 196 24 28

A solid 64 x 64 BC7 sRGB texture with its mips (the same layout as t_paint_yellow_b / t_paint_white_b, whose header it reuses):
every 4 x 4 block is one BC7 mode 6 block whose two endpoints are the colour (7 bits and the p-bit each) and whose indices
are 0, so no encoder (etcpak) is needed. The colour is sRGB, as the paint's base colour map is read."""
import os
import sys

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "runway")


def block(r, g, b):
    """The 16 bytes of a BC7 mode 6 block of the solid colour (r, g, b: 0-255; the low bit is dropped: both p-bits are 0, as
    the yellow and white textures' are) with alpha 254."""
    bits, n = 0, 0

    def put(v, k):
        nonlocal bits, n
        bits |= (v & ((1 << k) - 1)) << n
        n += k
    put(1 << 6, 7)                                  # mode 6: six zero bits, then a one
    for c in (r, g, b, 254):
        put(c >> 1, 7)                              # endpoint 0, then endpoint 1 (the same)
        put(c >> 1, 7)
    put(0, 2)                                       # the two p-bits; the indices (all 0: the first endpoint) fill the rest
    return bits.to_bytes(16, "little")


def main():
    name, r, g, b = sys.argv[1], *(int(v) for v in sys.argv[2:5])
    src = open(os.path.join(ART, "t_paint_yellow_b.color.dds"), "rb").read()
    head, body = src[:148], src[148:]
    out = os.path.join(ART, f"t_paint_{name}_b.color.dds")
    with open(out, "wb") as f:
        f.write(head + block(r, g, b) * (len(body) // 16))
    print("wrote", out)


if __name__ == "__main__":
    main()
