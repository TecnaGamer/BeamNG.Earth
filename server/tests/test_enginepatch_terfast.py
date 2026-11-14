"""enginepatch_terfast.py: the Windows game's three terrain cell scans found by their code and the block laid out for
them (the process access itself needs Windows: tools/gcpatch/README.md "terfast"; the machine code is run on Linux by
tools/gcpatch/win/test_terfast.sh)."""
import hashlib
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import enginepatch_terfast as et  # noqa: E402
import enginepatch_terfast_code as tc  # noqa: E402

VA = 0x140001000
XFORM, TAIL, HALF, K = 0x14080d270, 0x140a710c0, 0x141d9ef68, 0x141dadb24


def literal(pat, n):
    """Bytes the '??'-masked pattern matches (masked bytes zero), from its regex source."""
    src, out, i = pat.pattern, bytearray(), 0
    while i < len(src):
        c = src[i:i + 1]
        if c == b".":
            out.append(0); i += 1
        elif c == b"\\":
            out += src[i + 1:i + 2]; i += 2
        else:
            out += c; i += 1
    assert len(out) == n, (len(out), n)
    return out


def put_rel(code, at, opcode_len, insn_len, target):
    struct.pack_into("<i", code, at + opcode_len, target - (VA + at + insn_len))


def synth(twice_mats=False):
    code = bytearray(b"\xcc" * 0x4000)
    b, m, h = 0x100, 0x1000, 0x2000
    code[b:b + et.LEN_BOUNDS] = literal(et.PAT_BOUNDS, et.LEN_BOUNDS)
    put_rel(code, b + et.LEN_BOUNDS - 5, 1, 5, XFORM)
    half_at = code.find(et.BOUNDS_HALF, b)
    put_rel(code, half_at, 4, 8, HALF)
    for at in ([m, 0x3000] if twice_mats else [m]):
        code[at:at + et.LEN_MATS] = literal(et.PAT_MATS, et.LEN_MATS)
        put_rel(code, at + et.LEN_MATS - 5, 1, 5, TAIL)
    code[h:h + et.LEN_HOLES] = literal(et.PAT_HOLES, et.LEN_HOLES)
    put_rel(code, h + et.HOLES_K, 4, 8, K)
    return bytes(code), b, m, h


class TerfastFind(unittest.TestCase):
    def test_found_with_its_addresses(self):
        code, b, m, h = synth()
        floats = {HALF: 0.5, K: 0.015625}
        hook, why = et.find_terfast(code, VA, floats.__getitem__)
        self.assertIsNone(why)
        self.assertEqual((hook["bounds"], hook["mats"]), (VA + b, VA + m))
        self.assertEqual((hook["xform"], hook["mats_tail"]), (XFORM, TAIL))
        site = VA + h + et.HOLES_SITE
        self.assertEqual((hook["site"], hook["vb_body"], hook["vb_after"]), (site, site + 9, site + 9 + 0xfc))
        self.assertEqual(hook["site_bytes"], bytes.fromhex("83f8400f87fc000000"))
        # both entries: three 5-byte register saves (position independent: they run from the trampolines)
        for e in (hook["bounds_entry"], hook["mats_entry"]):
            self.assertEqual(len(e), et.ENTRY_LEN)
            self.assertEqual([e[i:i + 3] for i in (0, 5, 10)], [b"\x48\x89\x5c", b"\x48\x89\x6c", b"\x48\x89\x74"])

    def test_other_builds_refused(self):
        code, b, m, h = synth()
        self.assertIn("constants", et.find_terfast(code, VA, {HALF: 0.5, K: 0.03125}.__getitem__)[1])
        broken = bytearray(code)
        broken[m + 40] ^= 0xff
        self.assertIn("layer scan", et.find_terfast(bytes(broken), VA)[1])
        twice, *_ = synth(twice_mats=True)
        self.assertIn("2 matches", et.find_terfast(twice, VA)[1])
        self.assertIn("0 matches", et.find_terfast(b"\x90" * 0x1000, VA)[1])


class TerfastBlock(unittest.TestCase):
    def test_layout(self):
        code, *_ = synth()
        hook, _ = et.find_terfast(code, VA)
        blk = 0x7ff700000000
        b = et.block_bytes(hook, blk, check=True)
        self.assertEqual(len(b), et.BLOCK_SIZE)
        self.assertEqual(b[:len(tc.STUB)], tc.STUB)
        self.assertEqual(b[et.BLOCK_CODE:et.BLOCK_CODE + len(tc.CODE)], tc.CODE)
        # trampolines: the displaced entry, then an absolute jump to the instruction after it
        for off, fn, entry in ((et.BLOCK_TR_BOUNDS, hook["bounds"], hook["bounds_entry"]),
                               (et.BLOCK_TR_MATS, hook["mats"], hook["mats_entry"])):
            self.assertEqual(b[off:off + et.ENTRY_LEN], entry)
            self.assertEqual(b[off + et.ENTRY_LEN:off + et.ENTRY_LEN + 6], b"\xff\x25\x00\x00\x00\x00")
            self.assertEqual(struct.unpack_from("<Q", b, off + et.ENTRY_LEN + 6)[0], fn + et.ENTRY_LEN)

        def st(k):
            return struct.unpack_from("<" + et.STATE[k][1], b, et.BLOCK_STATE + et.STATE[k][0])[0]
        self.assertEqual((st("stock_bounds"), st("stock_mats")), (blk + et.BLOCK_TR_BOUNDS, blk + et.BLOCK_TR_MATS))
        self.assertEqual((st("xform"), st("mats_tail"), st("vb_after"), st("vb_body")),
                         (XFORM, TAIL, hook["vb_after"], hook["vb_body"]))
        self.assertEqual((st("on"), st("check"), st("hscale_off"), st("holder_off"), st("transform_slot")),
                         (0, 1, 0x3c4, 0x20, 0x1c0))
        self.assertEqual(st("holes_fn"), blk + tc.ENTRY["tf_holes"])
        self.assertEqual(et.block_bytes(hook, blk)[et.BLOCK_STATE + et.STATE["check"][0]], 0)

    def test_code_module(self):
        self.assertEqual(hashlib.sha256(tc.STUB + tc.CODE).hexdigest(), tc.SHA256)
        self.assertLessEqual(len(tc.STUB), et.BLOCK_TR_BOUNDS)
        self.assertLessEqual(et.BLOCK_CODE + len(tc.CODE), et.BLOCK_STATE)
        self.assertTrue(all(et.BLOCK_CODE <= v < et.BLOCK_CODE + len(tc.CODE) for v in tc.ENTRY.values()))
        self.assertEqual(max(off + struct.calcsize(fmt) for off, fmt in et.STATE.values()), et.STATE_SIZE)
        # the stub: its three indirect jumps / the call go through the state's slots
        self.assertEqual(tc.STUB[:6], b"\x48\x89\xf9\x4c\x89\xf2")          # mov rcx, rdi; mov rdx, r14

    def test_env(self):
        import os
        old = {k: os.environ.pop(k, None) for k in ("BNGEARTH_TERFAST_OFF", "BNGEARTH_TERFAST_WIN")}
        try:
            self.assertFalse(et.env_off())
            os.environ["BNGEARTH_TERFAST_WIN"] = "0"
            self.assertTrue(et.env_off())
            del os.environ["BNGEARTH_TERFAST_WIN"]
            os.environ["BNGEARTH_TERFAST_OFF"] = "1"
            self.assertTrue(et.env_off())
        finally:
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
