"""terfast for the Windows game: a terrain block swap's cell scans without their per-sample divisions (the Linux game
gets the same from the preload library, tools/gcpatch/bngearth_terfast.c; README.md there). Applied by the launcher to
the running BeamNG.drive.x64.exe as enginepatch.py applies dtsmooth: nothing on disk changes, no DLL is loaded.

A .ter swap (TerrainBlock::setFile) rebuilds the block's cell tree; for every 64 x 64 leaf the engine scans 65 x 65
samples three times (the cell's box, its layers, does it hold a hole), each sample indexed with two 32-bit divisions:
about 80 % of the swap's main-thread time. The launcher writes a block near the game's code (tools/gcpatch/win:
terfast_win.c compiled to Win64 machine code, a stub, two trampolines, its state) and points at it
- the entries of TerrCell::_updateBounds and TerrCell::_updateMaterials (a 14-byte jump over the three register saves
  each starts with; those 15 bytes run from a trampoline when the stock function is wanted), and
- the hole scan's first instruction in TerrCell::_updateVertexBuffer (cmp eax, 0x40; ja: a 5-byte jump to the stub).
The replacements compute the same box, radius, layer mask and hole flag row by row. They run the stock code instead
outside the BeamNG.Earth level (the level flag, <user>/temp/beamngearth_gcpatch_on), for any input they do not
expect, and with BNGEARTH_TERFAST_CHECK=1 both run and differences are counted in the status file.

The three functions are found by their whole code (call targets and rip-relative displacements masked; one match
each, the constants they load checked); in any other build nothing is patched. The game's threads are suspended for
the writes, and none may be inside the bytes replaced. Status: <user>/temp/beamngEarth_terfast.txt.
Off: BNGEARTH_TERFAST_OFF=1 (as on Linux) or BNGEARTH_TERFAST_WIN=0.

  python enginepatch_terfast.py --scan PATH\\TO\\BeamNG.drive.x64.exe     (offline: does this build match?)
  python enginepatch_terfast.py --attach --user-dir DIR                   (Windows: patch the running game)
"""
import os
import struct
import sys
import threading
import time

import enginepatch as ep

TAG = "enginepatch"
STATUS_FILE = "beamngEarth_terfast.txt"

# the block (tools/gcpatch/win/terfast_stub.asm, terfast.ld)
BLOCK_SIZE = 0x2000
BLOCK_TR_BOUNDS = 0x40
BLOCK_TR_MATS = 0x60
BLOCK_CODE = 0x100
BLOCK_STATE = 0x1000
# struct tf_state (terfast_win.c)
STATE = {"stock_bounds": (0x00, "Q"), "stock_mats": (0x08, "Q"), "xform": (0x10, "Q"), "mats_tail": (0x18, "Q"),
         "vb_after": (0x20, "Q"), "vb_body": (0x28, "Q"), "on": (0x30, "I"), "check": (0x34, "I"),
         "hscale_off": (0x38, "I"), "holder_off": (0x3c, "I"), "transform_slot": (0x40, "I"),
         "pending_flag": (0x44, "I"), "pending_cell": (0x48, "Q"), "n_bounds": (0x50, "Q"), "n_mats": (0x58, "Q"),
         "n_holes": (0x60, "Q"), "n_stock": (0x68, "Q"), "n_diff": (0x70, "Q"), "holes_fn": (0x78, "Q")}
STATE_SIZE = 0x80
ENTRY_LEN = 15          # the three register saves both functions start with (mov [rsp + n], reg: 5 bytes each)
JUMP_LEN = 14           # jmp [rip + 0]; <address>
HSCALE_OFF, HOLDER_OFF, TRANSFORM_SLOT = 0x3c4, 0x20, 0x1c0     # (in the patterns below)



def _p(spec):
    """(the pattern, the bytes it matches) of a '??'-masked byte string."""
    return ep._pat(spec), len(spec.split())


# TerrCell::_updateBounds, whole (0.40.4 Windows, the 2026-11-06 build: VA 0x140a70a30)
PAT_BOUNDS, LEN_BOUNDS = _p(
    "48 89 5c 24 10 48 89 6c 24 18 48 89 74 24 20 57 41 54 41 55 41 56 41 57 48 83 ec 50 0f 29 74 24 40 48 8b d9 "
    "48 8b 41 50 f3 0f 10 b0 40 03 00 00 8b 71 28 c1 ee 06 c7 41 5c ff ff 7f 7f c7 41 60 ff ff 7f 7f c7 41 64 ff "
    "ff 7f 7f c7 41 68 ff ff 7f ff c7 41 6c ff ff 7f ff c7 41 70 ff ff 7f ff 48 8d 54 24 20 48 8b c8 e8 ?? ?? ?? "
    "?? 4c 8b 78 10 4d 85 ff 74 0f 4d 8b 7f 20 4d 85 ff 74 06 4d 8b 7f 08 eb 03 45 33 ff 48 8d 05 ?? ?? ?? ?? 48 "
    "89 44 24 20 4c 8b 44 24 30 4d 85 c0 74 3d 41 8b 50 10 90 0f 1f 40 00 66 0f 1f 84 00 00 00 00 00 8d 42 ff b9 "
    "ff ff ff ff 83 fa 01 0f 45 c8 8b c2 f0 41 0f b1 48 10 74 04 8b d0 eb e4 83 fa 01 75 0a 49 8b 00 49 8b c8 ff "
    "50 08 90 45 33 d2 4d 8b 6f 40 44 8d 24 36 6b c6 fe 89 84 24 80 00 00 00 45 33 db 0f 1f 40 00 66 0f 1f 84 00 "
    "00 00 00 00 33 ed 45 8b f4 66 66 66 0f 1f 84 00 00 00 00 00 44 8b 4b 20 41 03 c6 41 03 c1 0f 57 e4 f3 48 0f "
    "2a e0 f3 0f 59 e6 8b 53 24 42 8d 0c 1a 0f 57 db f3 48 0f 2a d9 f3 0f 59 de 41 8b 4f 20 42 8d 04 12 33 d2 f7 "
    "f1 44 8b c2 44 0f af c1 41 8d 04 29 33 d2 f7 f1 44 03 c2 43 0f b7 4c 45 00 66 0f 6e d1 0f 5b d2 48 8b 43 50 "
    "f3 0f 59 90 c4 03 00 00 0f 28 c4 f3 0f 5d 43 5c f3 0f 11 43 5c 0f 28 cb f3 0f 5d 4b 60 f3 0f 11 4b 60 0f 28 "
    "c2 f3 0f 5d 43 64 f3 0f 11 43 64 f3 0f 10 6b 68 0f 28 cd f3 0f 5f cc f3 0f 11 4b 68 0f 28 c3 f3 0f 5f 43 6c "
    "f3 0f 11 43 6c 0f 28 ca f3 0f 5f 4b 70 f3 0f 11 4b 70 0f 28 c4 f3 0f 5d 43 5c f3 0f 11 43 5c 0f 28 cb f3 0f "
    "5d 4b 60 f3 0f 11 4b 60 0f 28 c2 f3 0f 5d 43 64 f3 0f 11 43 64 f3 0f 5f ec f3 0f 11 6b 68 f3 0f 5f 5b 6c f3 "
    "0f 11 5b 6c f3 0f 5f 53 70 f3 0f 11 53 70 44 8b 4b 20 41 8b c6 2b c6 41 03 c1 0f 57 e4 f3 48 0f 2a e0 f3 0f "
    "59 e6 8b 53 24 41 8d 0c 13 0f 57 db f3 48 0f 2a d9 f3 0f 59 de 41 8b 4f 20 42 8d 04 12 33 d2 f7 f1 44 8b c2 "
    "44 0f af c1 41 8d 41 01 03 c5 33 d2 f7 f1 44 03 c2 43 0f b7 4c 45 00 66 0f 6e d1 0f 5b d2 48 8b 43 50 f3 0f "
    "59 90 c4 03 00 00 0f 28 c4 f3 0f 5d 43 5c f3 0f 11 43 5c 0f 28 cb f3 0f 5d 4b 60 f3 0f 11 4b 60 0f 28 c2 f3 "
    "0f 5d 43 64 f3 0f 11 43 64 f3 0f 5f ec f3 0f 11 6b 68 0f 28 c3 f3 0f 5f 43 6c f3 0f 11 43 6c 0f 28 ca f3 0f "
    "5f 4b 70 f3 0f 11 4b 70 0f 28 c4 f3 0f 5d 43 5c f3 0f 11 43 5c 0f 28 cb f3 0f 5d 4b 60 f3 0f 11 4b 60 0f 28 "
    "c2 f3 0f 5d 43 64 f3 0f 11 43 64 f3 0f 5f 63 68 f3 0f 11 63 68 f3 0f 5f 5b 6c f3 0f 11 5b 6c f3 0f 5f 53 70 "
    "f3 0f 11 53 70 44 8b 4b 20 43 8d 0c 0e 0f 57 e4 f3 48 0f 2a e1 f3 0f 59 e6 8b 53 24 41 8d 0c 13 0f 57 db f3 "
    "48 0f 2a d9 f3 0f 59 de 41 8b 4f 20 42 8d 04 12 33 d2 f7 f1 44 8b c2 44 0f af c1 41 8d 41 02 03 c5 33 d2 f7 "
    "f1 44 03 c2 43 0f b7 4c 45 00 66 0f 6e d1 0f 5b d2 48 8b 43 50 f3 0f 59 90 c4 03 00 00 0f 28 c4 f3 0f 5d 43 "
    "5c f3 0f 11 43 5c 0f 28 cb f3 0f 5d 4b 60 f3 0f 11 4b 60 0f 28 c2 f3 0f 5d 43 64 f3 0f 11 43 64 0f 28 cc f3 "
    "0f 5f 4b 68 f3 0f 11 4b 68 0f 28 c3 f3 0f 5f 43 6c f3 0f 11 43 6c 0f 28 ca f3 0f 5f 4b 70 f3 0f 11 4b 70 0f "
    "28 c4 f3 0f 5d 43 5c f3 0f 11 43 5c 0f 28 cb f3 0f 5d 4b 60 f3 0f 11 4b 60 0f 28 c2 f3 0f 5d 43 64 f3 0f 11 "
    "43 64 f3 0f 5f 63 68 f3 0f 11 63 68 f3 0f 5f 5b 6c f3 0f 11 5b 6c f3 0f 5f 53 70 f3 0f 11 53 70 44 8b 4b 20 "
    "43 8d 04 0e 03 c6 0f 57 e4 f3 48 0f 2a e0 f3 0f 59 e6 8b 53 24 41 8d 0c 13 0f 57 db f3 48 0f 2a d9 f3 0f 59 "
    "de 41 8b 4f 20 42 8d 04 12 33 d2 f7 f1 44 8b c2 44 0f af c1 41 8d 41 03 03 c5 33 d2 f7 f1 44 03 c2 43 0f b7 "
    "4c 45 00 66 0f 6e d1 0f 5b d2 48 8b 43 50 f3 0f 59 90 c4 03 00 00 0f 28 c4 f3 0f 5d 43 5c f3 0f 11 43 5c 0f "
    "28 cb f3 0f 5d 4b 60 f3 0f 11 4b 60 0f 28 c2 f3 0f 5d 43 64 f3 0f 11 43 64 0f 28 cc f3 0f 5f 4b 68 f3 0f 11 "
    "4b 68 0f 28 c3 f3 0f 5f 43 6c f3 0f 11 43 6c 0f 28 ca f3 0f 5f 4b 70 f3 0f 11 4b 70 0f 28 c4 f3 0f 5d 43 5c "
    "f3 0f 11 43 5c 0f 28 cb f3 0f 5d 4b 60 f3 0f 11 4b 60 0f 28 c2 f3 0f 5d 43 64 f3 0f 11 43 64 f3 0f 5f 63 68 "
    "f3 0f 11 63 68 f3 0f 5f 5b 6c f3 0f 11 5b 6c f3 0f 5f 53 70 f3 0f 11 53 70 44 8b 4b 20 43 8d 04 34 41 03 c1 "
    "0f 57 e4 f3 48 0f 2a e0 f3 0f 59 e6 8b 53 24 41 8d 0c 13 0f 57 db f3 48 0f 2a d9 f3 0f 59 de 41 8b 4f 20 42 "
    "8d 04 12 33 d2 f7 f1 44 8b c2 44 0f af c1 41 8d 41 04 03 c5 33 d2 f7 f1 44 03 c2 43 0f b7 4c 45 00 66 0f 6e "
    "d1 0f 5b d2 48 8b 43 50 f3 0f 59 90 c4 03 00 00 0f 28 c4 f3 0f 5d 43 5c f3 0f 11 43 5c 0f 28 cb f3 0f 5d 4b "
    "60 f3 0f 11 4b 60 0f 28 c2 f3 0f 5d 43 64 f3 0f 11 43 64 0f 28 cc f3 0f 5f 4b 68 f3 0f 11 4b 68 0f 28 c3 f3 "
    "0f 5f 43 6c f3 0f 11 43 6c 0f 28 ca f3 0f 5f 4b 70 f3 0f 11 4b 70 0f 28 c4 f3 0f 5d 43 5c f3 0f 11 43 5c 0f "
    "28 cb f3 0f 5d 4b 60 f3 0f 11 4b 60 0f 28 c2 f3 0f 5d 43 64 f3 0f 11 43 64 f3 0f 5f 63 68 f3 0f 11 63 68 f3 "
    "0f 5f 5b 6c f3 0f 11 5b 6c f3 0f 5f 53 70 f3 0f 11 53 70 83 c5 05 8d 04 b6 44 03 f0 83 fd 41 8b 84 24 80 00 "
    "00 00 0f 82 20 fb ff ff 41 ff c2 44 03 de 41 83 fa 41 0f 82 00 fb ff ff f3 0f 10 4b 68 f3 0f 5c 4b 5c f3 0f "
    "10 53 6c f3 0f 5c 53 60 f3 0f 10 43 70 f3 0f 5c 43 64 f3 0f 59 d2 f3 0f 59 c9 f3 0f 58 d1 f3 0f 59 c0 f3 0f "
    "58 d0 0f 57 c0 f3 0f 51 c2 f3 0f 59 05 ?? ?? ?? ?? f3 0f 11 83 14 01 00 00 48 8b 4b 50 48 8b 01 ff 90 c0 01 "
    "00 00 48 8b d0 48 8d 4b 74 4c 8d 43 5c 4c 8d 5c 24 50 49 8b 5b 38 49 8b 6b 40 49 8b 73 48 0f 28 74 24 40 49 "
    "8b e3 41 5f 41 5e 41 5d 41 5c 5f e9 ?? ?? ?? ?? "
)
# TerrCell::_updateMaterials, whole (0x140a71230)
PAT_MATS, LEN_MATS = _p(
    "48 89 5c 24 08 48 89 6c 24 10 48 89 74 24 18 57 48 83 ec 40 48 8b d9 8b 71 28 c1 ee 06 33 ed 48 89 a9 38 01 "
    "00 00 48 8d 54 24 20 48 8b 49 50 e8 ?? ?? ?? ?? 48 8b 78 10 48 85 ff 74 0f 48 8b 7f 20 48 85 ff 74 06 48 8b "
    "7f 08 eb 03 48 8b fd 48 8d 05 ?? ?? ?? ?? 48 89 44 24 20 4c 8b 44 24 30 4d 85 c0 74 36 41 8b 50 10 90 66 0f "
    "1f 44 00 00 8d 42 ff b9 ff ff ff ff 83 fa 01 0f 45 c8 8b c2 f0 41 0f b1 48 10 74 04 8b d0 eb e4 83 fa 01 75 "
    "0a 49 8b 00 49 8b c8 ff 50 08 90 0f 1f 44 00 00 44 8b 53 20 44 8b 5b 24 8b c5 0f af c6 44 03 d8 41 b9 02 00 "
    "00 00 66 66 0f 1f 84 00 00 00 00 00 44 8b 47 20 41 8d 41 fe 0f af c6 41 03 c2 33 d2 41 f7 f0 8b ca 33 d2 41 "
    "8b c3 41 f7 f0 41 0f af d0 03 ca 48 8b 47 28 0f b6 14 01 80 fa ff 74 17 80 fa 1f 77 12 48 8b 8b 38 01 00 00 "
    "48 0f ab d1 48 89 8b 38 01 00 00 44 8b 47 20 41 8d 41 ff 0f af c6 41 03 c2 33 d2 41 f7 f0 8b ca 33 d2 41 8b "
    "c3 41 f7 f0 41 0f af d0 03 ca 48 8b 47 28 0f b6 14 01 80 fa ff 74 17 80 fa 1f 77 12 48 8b 8b 38 01 00 00 48 "
    "0f ab d1 48 89 8b 38 01 00 00 44 8b 47 20 41 8b c1 0f af c6 41 03 c2 33 d2 41 f7 f0 8b ca 33 d2 41 8b c3 41 "
    "f7 f0 41 0f af d0 03 ca 48 8b 47 28 0f b6 14 01 80 fa ff 74 17 80 fa 1f 77 12 48 8b 8b 38 01 00 00 48 0f ab "
    "d1 48 89 8b 38 01 00 00 44 8b 47 20 41 8d 41 01 0f af c6 41 03 c2 33 d2 41 f7 f0 8b ca 33 d2 41 8b c3 41 f7 "
    "f0 41 0f af d0 03 ca 48 8b 47 28 0f b6 14 01 80 fa ff 74 17 80 fa 1f 77 12 48 8b 8b 38 01 00 00 48 0f ab d1 "
    "48 89 8b 38 01 00 00 44 8b 47 20 41 8d 41 02 0f af c6 41 03 c2 33 d2 41 f7 f0 8b ca 33 d2 41 8b c3 41 f7 f0 "
    "41 0f af d0 03 ca 48 8b 47 28 0f b6 14 01 80 fa ff 74 17 80 fa 1f 77 12 48 8b 8b 38 01 00 00 48 0f ab d1 48 "
    "89 8b 38 01 00 00 44 8b 47 20 41 8d 41 03 0f af c6 41 03 c2 33 d2 41 f7 f0 8b ca 33 d2 41 8b c3 41 f7 f0 41 "
    "0f af d0 03 ca 48 8b 47 28 0f b6 14 01 80 fa ff 74 17 80 fa 1f 77 12 48 8b 8b 38 01 00 00 48 0f ab d1 48 89 "
    "8b 38 01 00 00 44 8b 47 20 41 8d 41 04 0f af c6 41 03 c2 33 d2 41 f7 f0 8b ca 33 d2 41 8b c3 41 f7 f0 41 0f "
    "af d0 03 ca 48 8b 47 28 0f b6 14 01 80 fa ff 74 17 80 fa 1f 77 12 48 8b 8b 38 01 00 00 48 0f ab d1 48 89 8b "
    "38 01 00 00 44 8b 47 20 41 8d 41 05 0f af c6 41 03 c2 33 d2 41 f7 f0 8b ca 33 d2 41 8b c3 41 f7 f0 41 0f af "
    "d0 03 ca 48 8b 47 28 0f b6 14 01 80 fa ff 74 17 80 fa 1f 77 12 48 8b 8b 38 01 00 00 48 0f ab d1 48 89 8b 38 "
    "01 00 00 44 8b 47 20 41 8d 41 06 0f af c6 41 03 c2 33 d2 41 f7 f0 8b ca 33 d2 41 8b c3 41 f7 f0 41 0f af d0 "
    "03 ca 48 8b 47 28 0f b6 14 01 80 fa ff 74 17 80 fa 1f 77 12 48 8b 8b 38 01 00 00 48 0f ab d1 48 89 8b 38 01 "
    "00 00 44 8b 47 20 41 8d 41 07 0f af c6 41 03 c2 33 d2 41 f7 f0 8b ca 33 d2 41 8b c3 41 f7 f0 41 0f af d0 03 "
    "ca 48 8b 47 28 0f b6 14 01 80 fa ff 74 17 80 fa 1f 77 12 48 8b 8b 38 01 00 00 48 0f ab d1 48 89 8b 38 01 00 "
    "00 44 8b 47 20 41 8d 41 08 0f af c6 41 03 c2 33 d2 41 f7 f0 8b ca 33 d2 41 8b c3 41 f7 f0 41 0f af d0 03 ca "
    "48 8b 47 28 0f b6 14 01 80 fa ff 74 17 80 fa 1f 77 12 48 8b 8b 38 01 00 00 48 0f ab d1 48 89 8b 38 01 00 00 "
    "44 8b 47 20 41 8d 41 09 0f af c6 41 03 c2 33 d2 41 f7 f0 8b ca 33 d2 41 8b c3 41 f7 f0 41 0f af d0 03 ca 48 "
    "8b 47 28 0f b6 14 01 80 fa ff 74 17 80 fa 1f 77 12 48 8b 8b 38 01 00 00 48 0f ab d1 48 89 8b 38 01 00 00 44 "
    "8b 47 20 41 8d 41 0a 0f af c6 41 03 c2 33 d2 41 f7 f0 8b ca 33 d2 41 8b c3 41 f7 f0 41 0f af d0 03 ca 48 8b "
    "47 28 0f b6 14 01 80 fa ff 74 17 80 fa 1f 77 12 48 8b 8b 38 01 00 00 48 0f ab d1 48 89 8b 38 01 00 00 41 83 "
    "c1 0d 41 8d 41 fe 83 f8 41 0f 82 55 fc ff ff ff c5 83 fd 41 0f 82 2a fc ff ff 48 8b cb 48 8b 5c 24 50 48 8b "
    "6c 24 58 48 8b 74 24 60 48 83 c4 40 5f e9 ?? ?? ?? ?? "
)
# TerrCell::_updateVertexBuffer from the cell's size read to the call after the hole scan (0x140a728eb)
PAT_HOLES, LEN_HOLES = _p(
    "8b 47 28 f3 0f 10 3d ?? ?? ?? ?? 0f 57 f6 83 f8 40 0f 87 fc 00 00 00 45 8b dd 4c 8b 7f 50 0f 57 db f3 48 0f "
    "2a d8 f3 0f 59 df 66 0f 6e 67 20 0f 5b e4 66 0f 6e 6f 24 0f 5b ed 66 66 66 0f 1f 84 00 00 00 00 00 45 8b cd "
    "4d 8b 87 60 02 00 00 45 8b 56 20 49 8b 76 28 41 8b c3 0f 57 d2 f3 48 0f 2a d0 f3 0f 59 d3 f3 0f 58 d5 4d 85 "
    "c0 74 0f 49 8b 48 20 48 85 c9 74 06 48 8b 49 08 eb 03 49 8b cd 41 8b c1 0f 57 c9 f3 48 0f 2a c8 f3 0f 59 cb "
    "f3 0f 58 cc 8b 41 20 0f 57 c0 f3 48 0f 2a c0 f3 0f 5d c8 f3 0f 5f ce 4d 85 c0 74 0f 49 8b 40 20 48 85 c0 74 "
    "06 48 8b 40 08 eb 03 49 8b c5 8b 40 20 0f 57 c0 f3 48 0f 2a c0 f3 0f 5d c2 f3 0f 5f c6 f3 48 0f 2c c0 33 d2 "
    "41 f7 f2 8b ca 41 0f af ca f3 48 0f 2c c1 33 d2 41 f7 f2 03 ca 80 3c 31 ff 74 0f 41 ff c1 41 83 f9 41 0f 82 "
    "6a ff ff ff eb 04 c6 47 34 01 41 ff c3 41 83 fb 41 0f 82 32 ff ff ff e8 ?? ?? ?? ?? "
)
HOLES_SITE = 14                 # cmp eax, 0x40; ja rel32 (9 bytes): the scan's first instruction
HOLES_K = 3                     # movss xmm7, [rip + K]: 1 / 64
BOUNDS_HALF = b"\xf3\x0f\x59\x05"       # mulss xmm0, [rip + 0.5] (the radius)


def _one(pat, code, what):
    hits = [m.start() for m in pat.finditer(code)]
    if len(hits) != 1:
        return None, f"{what} was not found in this build ({len(hits)} matches)"
    return hits[0], None


def find_terfast(code, code_va, read_float=None):
    """The hooks in `code` (the game's .text mapped at code_va): a dict of virtual addresses, or (None, reason).
    read_float(va): the float at a virtual address (the constants' check); None skips it (offline, .rdata not read)."""
    b, why = _one(PAT_BOUNDS, code, "the cell bounds scan")
    if why:
        return None, why
    m, why = _one(PAT_MATS, code, "the cell layer scan")
    if why:
        return None, why
    h, why = _one(PAT_HOLES, code, "the cell hole scan")
    if why:
        return None, why
    xform = ep._rip(code, b + LEN_BOUNDS - 5, 1, 5, code_va)            # (both end in a tail jump)
    mats_tail = ep._rip(code, m + LEN_MATS - 5, 1, 5, code_va)
    half_at = code.find(BOUNDS_HALF, b, b + LEN_BOUNDS)
    if half_at < 0:
        return None, "the bounds scan's radius constant was not found"
    half = ep._rip(code, half_at, 4, 8, code_va)
    k = ep._rip(code, h + HOLES_K, 4, 8, code_va)
    site = h + HOLES_SITE
    rel = struct.unpack_from("<i", code, site + 5)[0]
    hook = {"bounds": code_va + b, "mats": code_va + m, "xform": xform, "mats_tail": mats_tail,
            "site": code_va + site, "vb_body": code_va + site + 9, "vb_after": code_va + site + 9 + rel,
            "half": half, "k": k,
            "bounds_entry": bytes(code[b:b + ENTRY_LEN]), "mats_entry": bytes(code[m:m + ENTRY_LEN]),
            "site_bytes": bytes(code[site:site + 9])}
    if not code_va + h <= hook["vb_after"] <= code_va + h + LEN_HOLES:
        return None, "the hole scan's end is not where expected"
    if read_float is not None:
        try:
            if read_float(half) != 0.5 or read_float(k) != 0.015625:
                return None, "the scans' constants differ in this build"
        except OSError as e:
            return None, f"the scans' constants could not be read ({e})"
    return hook, None


def scan_file(path):
    """Offline: find_terfast on an executable file as the loader would map it (constants read from the file)."""
    with open(path, "rb") as f:
        data = f.read()
    pe = struct.unpack_from("<I", data, 0x3c)[0]
    opt = struct.unpack_from("<H", data, pe + 20)[0]
    image_base = struct.unpack_from("<Q", data, pe + 24 + 24)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    secs = []
    for i in range(nsec):
        o = pe + 24 + opt + 40 * i
        vsize, rva, rsize, roff = struct.unpack_from("<IIII", data, o + 8)
        secs.append((data[o:o + 8].rstrip(b"\0"), rva, vsize, roff, rsize))

    def read_float(va):
        for _, rva, vsize, roff, rsize in secs:
            if image_base + rva <= va < image_base + rva + min(vsize, rsize):
                return struct.unpack_from("<f", data, roff + va - image_base - rva)[0]
        raise OSError(f"{va:#x} is in no section")
    for name, rva, vsize, roff, rsize in secs:
        if name == b".text":
            return find_terfast(data[roff: roff + min(vsize, rsize)], image_base + rva, read_float)
    return None, "no .text section"


def _jump(target):
    return b"\xff\x25\x00\x00\x00\x00" + struct.pack("<Q", target)


def block_bytes(hook, blk, check=False):
    """The block to write at `blk`: the stub, both trampolines, the code; its state on the data page."""
    import enginepatch_terfast_code as tc
    b = bytearray(BLOCK_SIZE)
    b[0:len(tc.STUB)] = tc.STUB
    b[BLOCK_TR_BOUNDS:BLOCK_TR_BOUNDS + ENTRY_LEN + JUMP_LEN] = hook["bounds_entry"] + _jump(hook["bounds"] + ENTRY_LEN)
    b[BLOCK_TR_MATS:BLOCK_TR_MATS + ENTRY_LEN + JUMP_LEN] = hook["mats_entry"] + _jump(hook["mats"] + ENTRY_LEN)
    b[BLOCK_CODE:BLOCK_CODE + len(tc.CODE)] = tc.CODE
    st = {"stock_bounds": blk + BLOCK_TR_BOUNDS, "stock_mats": blk + BLOCK_TR_MATS, "xform": hook["xform"],
          "mats_tail": hook["mats_tail"], "vb_after": hook["vb_after"], "vb_body": hook["vb_body"], "on": 0,
          "check": 1 if check else 0, "hscale_off": HSCALE_OFF, "holder_off": HOLDER_OFF,
          "transform_slot": TRANSFORM_SLOT, "holes_fn": blk + tc.ENTRY["tf_holes"]}
    for k, v in st.items():
        off, fmt = STATE[k]
        struct.pack_into("<" + fmt, b, BLOCK_STATE + off, v)
    return bytes(b)


def env_off():
    return os.environ.get("BNGEARTH_TERFAST_OFF", "") not in ("", "0") or os.environ.get("BNGEARTH_TERFAST_WIN") == "0"


def report(user_dir, msg, log=None):
    line = f"[{TAG}] terfast: {msg}"
    (log or (lambda s: print(s, file=sys.stderr)))(line)
    _status(user_dir, msg)


def _status(user_dir, msg):
    if user_dir:
        try:
            os.makedirs(os.path.join(user_dir, "temp"), exist_ok=True)
            with open(os.path.join(user_dir, "temp", STATUS_FILE), "w") as f:
                f.write(f"{int(time.time())} launcher pid {os.getpid()}: {msg}\n")
        except OSError:
            pass


def thread_rips(proc):
    """The instruction pointers of the process's threads (call with them suspended), or None when one cannot be read."""
    ct, w = proc.ct, proc.w
    k = proc.k
    k.GetThreadContext.argtypes = [w.HANDLE, ct.c_void_p]
    THREAD_GET_CONTEXT, CONTEXT_CONTROL = 0x0008, 0x00100001
    out = []
    for tid in proc.threads():
        th = k.OpenThread(THREAD_GET_CONTEXT, False, tid)
        if not th:
            return None
        try:
            raw = ct.create_string_buffer(0x4d0 + 16)
            base = (ct.addressof(raw) + 15) & ~15           # (CONTEXT is 16-byte aligned)
            ct.memset(base, 0, 0x4d0)
            ct.c_uint32.from_address(base + 0x30).value = CONTEXT_CONTROL
            if not k.GetThreadContext(th, ct.c_void_p(base)):
                return None
            out.append(ct.c_uint64.from_address(base + 0xf8).value)
        finally:
            k.CloseHandle(th)
    return out


class TerFastWin:
    """terfast in one running Windows game: apply() once, then follow the level flag until the game ends."""

    def __init__(self, proc, user_dir, log=None):
        self.p, self.user_dir, self.log = proc, user_dir, log
        self.block = None
        self.stop = threading.Event()

    def apply(self, check=False, tries=5):
        import enginepatch_terfast_code as tc
        base = self.p.module_base(ep.GAME_EXE)
        if not base:
            return f"{ep.GAME_EXE} is not loaded in process {self.p.pid}"
        text = [s for s in ep.pe_sections(self.p.read, base) if s[0] == ".text"]
        if not text:
            return "no .text section in the game"
        _, rva, vsize, _ = text[0]
        code = self.p.read(base + rva, vsize)
        hook, why = find_terfast(code, base + rva, lambda va: struct.unpack("<f", self.p.read(va, 4))[0])
        if not hook:
            return why
        blk = self.p.alloc_near(hook["site"], BLOCK_SIZE)
        if not blk:
            return "no memory near the game's code"
        rel = blk - (hook["site"] + 5)
        if not -0x80000000 <= rel < 0x80000000:
            self.p.free(blk)
            return "the block is out of reach"
        writes = [(hook["bounds"], hook["bounds_entry"], _jump(blk + tc.ENTRY["tf_bounds"]) + hook["bounds_entry"][JUMP_LEN:]),
                  (hook["mats"], hook["mats_entry"], _jump(blk + tc.ENTRY["tf_mats"]) + hook["mats_entry"][JUMP_LEN:]),
                  (hook["site"], hook["site_bytes"], b"\xe9" + struct.pack("<i", rel) + b"\x90" * 4)]
        try:
            self.p.write(blk, block_bytes(hook, blk, check))
            self.p.protect(blk, 0x1000, ep.WinProcess.PAGE_EXECUTE_READ)       # code page; the data page stays RW
            self.p.flush(blk, 0x1000)
            done = False
            for _ in range(tries):
                with self.p.suspended():
                    rips = thread_rips(self.p)
                    if rips is None or any(a <= r < a + len(old) for r in rips for a, old, _ in writes):
                        continue                    # (a thread inside the bytes to replace: once more)
                    if any(self.p.read(a, len(old)) != old for a, old, _ in writes):
                        self.p.free(blk)
                        return "the code changed under us"
                    for a, old, new in writes:
                        prot = self.p.protect(a, len(new), ep.WinProcess.PAGE_EXECUTE_READWRITE)
                        try:
                            self.p.write(a, new)
                        finally:
                            self.p.protect(a, len(new), prot)
                        self.p.flush(a, len(new))
                    done = True
                    break
                time.sleep(0.05)
            if not done:
                self.p.free(blk)
                return "the game's threads could not be stopped outside the code to replace"
        except OSError as e:
            return f"writing failed ({e})"
        self.block, self.hook, self.check = blk, hook, check
        return None

    def set_on(self, on):
        self.p.write(self.block + BLOCK_STATE + STATE["on"][0], struct.pack("<I", 1 if on else 0))

    def stats(self):
        raw = self.p.read(self.block + BLOCK_STATE, STATE_SIZE)
        return {k: struct.unpack_from("<" + STATE[k][1], raw, STATE[k][0])[0]
                for k in ("n_bounds", "n_mats", "n_holes", "n_stock", "n_diff")}

    def line(self, on):
        s = self.stats()
        msg = "%s: cell scans fast %d bounds / %d layers / %d holes, stock %d" % (
            "applied (BeamNG.Earth level; launcher patch)" if on else "ready (stock outside the BeamNG.Earth level)",
            s["n_bounds"], s["n_mats"], s["n_holes"], s["n_stock"])
        if self.check:
            msg += "; check: %d differences" % s["n_diff"]
        return msg

    def follow(self):
        """Until the game ends (or stop): the fast scans on while the level flag exists; the status file kept."""
        flag = os.path.join(self.user_dir, "temp", ep.LEVEL_FLAG) if self.user_dir else None
        on, shown, ticks = None, None, 0
        while not self.stop.is_set() and self.p.alive():
            want = bool(flag and os.path.exists(flag))
            try:
                if want != on:
                    self.set_on(want)
                ticks += 1
                if want != on or ticks >= 20:
                    msg = self.line(want)
                    if want != on:
                        report(self.user_dir, msg, self.log)
                    elif msg != shown:
                        _status(self.user_dir, msg)
                    shown, ticks = msg, 0
            except OSError:
                break
            on = want
            self.stop.wait(ep.FLAG_POLL_S)


def attach(user_dir, wait_s=600.0, log=None, block=True):
    """Windows: wait for the game process, patch it and follow the level flag (in a thread unless block). The
    TerFastWin, or None (the status file says why)."""
    if env_off():
        report(user_dir, "off (BNGEARTH_TERFAST_OFF / BNGEARTH_TERFAST_WIN=0)", log)
        return None
    t0 = time.monotonic()
    pid = None
    while time.monotonic() - t0 < wait_s:
        pid = ep.game_pid()
        if pid:
            break
        time.sleep(1.0)
    if not pid:
        report(user_dir, "not applied: the game process was not found", log)
        return None
    time.sleep(3.0)         # (the executable mapped and relocated)
    try:
        proc = ep.WinProcess(pid)
    except OSError as e:
        report(user_dir, f"not applied: {e}", log)
        return None
    t = TerFastWin(proc, user_dir, log)
    why = t.apply(check=os.environ.get("BNGEARTH_TERFAST_CHECK", "") not in ("", "0"))
    if why:
        report(user_dir, f"not applied: {why}; stock", log)
        proc.close()
        return None
    report(user_dir, "ready: applied while the BeamNG.Earth level runs (launcher patch, block at %#x)" % t.block, log)
    if block:
        t.follow()
    else:
        threading.Thread(target=t.follow, name="terfast-flag", daemon=True).start()
    return t


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scan", metavar="EXE", help="offline: find the hooks in an executable file")
    ap.add_argument("--attach", action="store_true", help="Windows: patch the running game and follow the level flag")
    ap.add_argument("--user-dir", help="the game's user folder (the level flag and status file)")
    ap.add_argument("--wait", type=float, default=600.0)
    a = ap.parse_args(argv)
    if a.scan:
        hook, why = scan_file(a.scan)
        if not hook:
            print("not found:", why)
            return 1
        print({k: (hex(v) if isinstance(v, int) else v.hex()) for k, v in hook.items()})
        return 0
    if a.attach:
        return 0 if attach(a.user_dir, a.wait) else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
