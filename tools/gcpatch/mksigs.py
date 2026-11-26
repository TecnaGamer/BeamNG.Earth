#!/usr/bin/env python3
"""Masked whole-function signatures for the Linux preload patches (bngearth_terfast.c, bngearth_texkeep.c; README.md).

    python3 tools/gcpatch/mksigs.py BinLinux/BeamNG.drive.x64 > tools/gcpatch/bngearth_sigs.h

A signature is every byte of one engine function (its range from the executable's .eh_frame) except the parts a
rebuild of unchanged source moves: rip-relative displacements (data, constants, vtables) and the rel32 of calls and
jumps that leave the function. Everything else must be equal: the instructions, the registers, the structure offsets,
the immediates and the jumps inside the function. The patches search the game's code for each signature at startup
and act only on a single match (bngearth_sigscan.h); the addresses they need beyond the function (the functions it
calls, the constants it loads) are read from the matched code at fixed offsets of the signature and checked there.

The reference build's address of each function goes in the header too (a hint for the report, never trusted).
Needs objdump (binutils) and pyelftools. A function that holds a rel8 jump out of its own range, or bytes objdump
cannot decode, is refused (its signature would not be position independent).
"""
import re
import struct
import subprocess
import sys

from elftools.dwarf.callframe import FDE
from elftools.elf.elffile import ELFFile

# name: (reference vaddr inside the function, what it is). The vaddrs are of BeamNG.drive 0.40.6 Linux
# (BinLinux/BeamNG.drive.x64 sha256 3ba92b56db55189b..., 2026-11-19); a later build needs only a vaddr inside each.
FUNCS = {
    "tf_bounds": (0x1735dc0, "TerrCell::_updateBounds()"),
    "tf_mats": (0x1735c00, "TerrCell::_updateMaterials()"),
    "tf_xform": (0x12b00d0, "the cell's world box from its object box (cell + 0x74, matrix, box)"),
    "tf_mats_tail": (0x1733c30, "_updateMaterials' tail call (cells with a vertex buffer slot)"),
    "tf_vb": (0x1734b30, "TerrCell::_updateVertexBuffer()"),
    "tf_get_transform": (0x80cd80, "SceneObject::getTransform (lea rax, [rdi + 0x130])"),
    "tk_create": (0xc6ece0, "GFXTextureManager::createTexture(const Path&, GFXTextureProfile*)"),
    "tk_loader": (0x173f2df, "TerrainCellMaterial's pack loader (both createTexture calls)"),
    "tk_request_delete": (0xc6a630, "GFXTextureManager::requestDeleteTexture(GFXTextureObject*)"),
    "tk_destroy_self": (0xc7d4d0, "GFXTextureObject::destroySelf (vtable[2])"),
}

JUMPS = re.compile(r"^(call|jmp|j[a-z]{1,3})\b")


def fde_ranges(elf, vaddrs):
    out = {}
    for e in elf.get_dwarf_info().EH_CFI_entries():
        if not isinstance(e, FDE):
            continue
        a, n = e.header["initial_location"], e.header["address_range"]
        for v in vaddrs:
            if a <= v < a + n:
                out[v] = (a, n)
    return out


def disasm(binary, start, end):
    """[(vaddr, bytes, text)] of [start, end)."""
    txt = subprocess.run(["objdump", "-d", "--insn-width=16", "-w",
                          f"--start-address={start:#x}", f"--stop-address={end:#x}", binary],
                         check=True, capture_output=True, text=True).stdout
    out = []
    for line in txt.splitlines():
        m = re.match(r"^\s*([0-9a-f]+):\t([0-9a-f ]+?)\s*\t(.*)$", line)
        if m:
            out.append((int(m.group(1), 16), bytes.fromhex(m.group(2).replace(" ", "")), m.group(3)))
    return out


def signature(binary, start, n):
    """(bytes, mask, refs): mask 1 = must match; refs = [(offset of the 4 masked bytes, end of the instruction,
    kind, target)]."""
    code = bytearray()
    mask = bytearray()
    refs = []
    end = start + n
    for at, raw, text in disasm(binary, start, end):
        if "(bad)" in text:
            raise SystemExit(f"{at:#x}: undecodable bytes")
        off = at - start
        m = [1] * len(raw)
        mn = text.split()[0] if text.split() else ""
        if "(%rip)" in text:
            d = re.search(r"(-?0x[0-9a-f]+)\(%rip\)", text)
            disp = int(d.group(1), 16) if d else 0
            enc = struct.pack("<i", disp)
            k = raw.find(enc)
            if k < 0:
                raise SystemExit(f"{at:#x}: rip displacement not found in {raw.hex()} ({text})")
            for j in range(len(raw) - 3):        # (an immediate with the same bytes is masked too: never too strict)
                if raw[j:j + 4] == enc:
                    for i in range(j, j + 4):
                        m[i] = 0
            refs.append((off + k, off + len(raw), "rip", at + len(raw) + disp))
        elif JUMPS.match(mn) and "*" not in text:
            t = re.search(r"\s([0-9a-f]+)\s*(<|$)", text)
            target = int(t.group(1), 16) if t else None
            rel32 = raw[0] in (0xe8, 0xe9) or (len(raw) >= 6 and raw[0] == 0x0f and 0x80 <= raw[1] <= 0x8f)
            if target is not None and not (start <= target < end):
                if not rel32:
                    raise SystemExit(f"{at:#x}: a short jump out of the function ({text})")
                for i in range(len(raw) - 4, len(raw)):
                    m[i] = 0
                refs.append((off + len(raw) - 4, off + len(raw), mn, target))
        code += raw
        mask += bytes(m)
    if len(code) != n:
        raise SystemExit(f"{start:#x}: disassembled {len(code)} of {n} bytes")
    return bytes(code), bytes(mask), refs


def anchor(mask):
    """(offset, length) of the longest run of matched bytes (at most 16): the search's first test."""
    best = (0, 0)
    i = 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j < len(mask) and mask[j]:
                j += 1
            if j - i > best[1]:
                best = (i, j - i)
            i = j
        else:
            i += 1
    return best[0], min(best[1], 16)


def main():
    binary = sys.argv[1]
    with open(binary, "rb") as f:
        elf = ELFFile(f)
        ranges = fde_ranges(elf, [v for v, _ in FUNCS.values()])
    out = ["// Generated by tools/gcpatch/mksigs.py from BeamNG.drive.x64 (see that script): masked whole-function",
           "// signatures of the engine functions the Linux patches use. Do not edit by hand.",
           "#pragma once", "#include \"bngearth_sigscan.h\"", ""]
    for name, (v, what) in FUNCS.items():
        if v not in ranges:
            raise SystemExit(f"{name}: no FDE covers {v:#x}")
        start, n = ranges[v]
        code, mask, refs = signature(binary, start, n)
        a_off, a_len = anchor(mask)
        if a_len < 8:
            raise SystemExit(f"{name}: no anchor")
        masked = n - sum(mask)
        out.append(f"// {name}: {what}; reference {start:#x}, {n} bytes, {masked} masked")
        if n <= 1024:          # (the big ones' references would be hundreds of lines; the patches use none of them)
            for off, endo, kind, target in refs:
                out.append(f"//   +{off:#05x} ({kind}, insn end +{endo:#05x}) -> {target:#x}")
        out.append(f"static const unsigned char {name}_code[{n}] = {{")
        for i in range(0, n, 24):
            out.append("    " + ", ".join(f"0x{b:02x}" for b in code[i:i + 24]) + ",")
        out.append("};")
        bits = bytearray((n + 7) // 8)
        for i, b in enumerate(mask):
            if b:
                bits[i // 8] |= 1 << (i % 8)
        out.append(f"static const unsigned char {name}_mask[{len(bits)}] = {{")
        for i in range(0, len(bits), 24):
            out.append("    " + ", ".join(f"0x{b:02x}" for b in bits[i:i + 24]) + ",")
        out.append("};")
        out.append(f"static const struct bnge_sig {name} = {{\"{name}\", {n}, {name}_code, {name}_mask, "
                   f"{a_off}, {a_len}, {start:#x}UL}};")
        out.append("")
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
