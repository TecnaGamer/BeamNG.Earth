"""enginepatch.py: the Windows game's dtsmooth hook found by its instructions and the block laid out for it (the
process access itself needs Windows: tools/gcpatch/README.md "Windows" has the checks; the machine code is run on
Linux by tools/gcpatch/win/test_blob.sh)."""
import hashlib
import os
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import enginepatch as ep  # noqa: E402
import enginepatch_code  # noqa: E402

VA = 0x140001000
DTRAW, DTREAL, XR, ORIG, ONE = 0x142c27d00, 0x142c27d08, 0x142a00890, 0x140d55810, 0x141da04a8


def rip(code, at, opcode, target, tail=b""):
    """opcode + disp32 (+ tail bytes) at `at`, the displacement pointing at target."""
    end = at + len(opcode) + 4 + len(tail)
    code[at:end] = opcode + struct.pack("<i", target - (VA + end - len(tail))) + tail
    return end


def synth(dtreal_in_tail=DTREAL, twice=False, xr_off=0x1fc):
    code = bytearray(b"\x90" * 0x800)
    for s in ([0x40, 0x440] if twice else [0x40]):
        k = s
        code[k:k + 3] = b"\x0f\x28\xc8"; k += 3                                   # movaps xmm1,xmm0
        k = rip(code, k, b"\xf2\x0f\x10\x05", ONE)                                 # movsd xmm0,[1.0]
        k = rip(code, k, b"\xe8", 0x141cdc1a0)                                     # call fmin
        code[k:k + 3] = b"\x0f\x28\xd8"; k += 3                                   # movaps xmm3,xmm0
        k = rip(code, k, b"\xf2\x0f\x11\x05", DTRAW)                               # movsd [dtRaw],xmm0
        k = rip(code, k, b"\xf2\x0f\x10\x15", 0x141da0498)                         # movsd xmm2,[-0.1]
        code[k:k + 5] = b"\xf2\x0f\x11\x55\x88"; k += 5                            # movsd [rbp-0x78],xmm2
        k = rip(code, k, b"\xf2\x0f\x10\x05", 0x141db0168)                         # movsd xmm0,[0.1]
        code[k:k + 6] = b"\xf2\x0f\x11\x44\x24\x78"; k += 6                        # movsd [rsp+0x78],xmm0
        k = rip(code, k, b"\xf2\x0f\x10\x0d", DTREAL)                              # movsd xmm1,[dtReal]
        code[k:k + 4] = b"\xf2\x0f\x5c\xcb"; k += 4                                # subsd xmm1,xmm3
        k = rip(code, s + 0x90, b"\xf2\x0f\x11\x0d", DTREAL)                       # movsd [dtReal],xmm1
        k = rip(code, s + 0xa0, b"\x48\x8b\x05", XR)                               # mov rax,[xr]
        t = s + 0x100
        k = rip(code, t, b"\x48\x89\x05", XR)                                      # mov [xr],rax
        k = rip(code, k, b"\xf2\x0f\x10\x05", DTRAW)                               # movsd xmm0,[dtRaw]
        code[k:k + 7] = b"\x80\xb8" + struct.pack("<i", xr_off) + b"\x00"; k += 7  # cmp byte [rax+off],0
        code[k:k + 2] = b"\x74\x08"; k += 2                                        # je +8
        k = rip(code, k, b"\xf2\x0f\x11\x05", dtreal_in_tail)                      # movsd [dtReal],xmm0
        rip(code, k, b"\xe8", ORIG)                                                # call MainLoopTimer add
    return bytes(code)


class FindHook(unittest.TestCase):
    def test_found(self):
        hook, why = ep.find_dtsmooth(synth(), VA)
        self.assertIsNone(why)
        self.assertEqual((hook["dtreal"], hook["dtraw"], hook["xr"], hook["orig"], hook["xr_off"]),
                         (DTREAL, DTRAW, XR, ORIG, 0x1fc))
        self.assertEqual(hook["site"], VA + 0x40 + 0x100 + 7 + 8 + 7 + 2 + 8)

    def test_refused(self):
        self.assertIsNone(ep.find_dtsmooth(synth(twice=True), VA)[0], "two matches: not patched")
        self.assertIsNone(ep.find_dtsmooth(synth(dtreal_in_tail=DTREAL + 8), VA)[0], "another global at the end")
        self.assertIsNone(ep.find_dtsmooth(synth(xr_off=0x7fffffff), VA)[0])
        self.assertIsNone(ep.find_dtsmooth(b"\x90" * 0x800, VA)[0])

    def test_local_game_build(self):
        exe = os.path.expanduser("~/.local/share/Steam/steamapps/common/BeamNG.drive/Bin64/BeamNG.drive.x64.exe")
        if not os.path.exists(exe):
            self.skipTest("no Windows build of the game here")
        hook, why = ep.scan_file(exe)
        self.assertIsNone(why)
        self.assertEqual(hook["xr_off"], 0x1fc)


class Block(unittest.TestCase):
    def test_layout(self):
        hook, _ = ep.find_dtsmooth(synth(), VA)
        b = ep.block_bytes(hook, {"repay": 0.0})
        self.assertEqual(len(b), ep.BLOCK_SIZE)
        self.assertEqual(b[:len(enginepatch_code.TRAMP)], enginepatch_code.TRAMP)
        self.assertEqual(b[ep.BLOCK_FILTER:ep.BLOCK_FILTER + len(enginepatch_code.FILTER)], enginepatch_code.FILTER)
        self.assertEqual(struct.unpack_from("<Q", b, ep.BLOCK_ORIG)[0], ORIG)

        def field(k):
            off, fmt = ep.DTF[k]
            return struct.unpack_from("<" + fmt, b, ep.BLOCK_STATE + off)[0]
        self.assertEqual(field("dtreal"), DTREAL)
        self.assertEqual(field("xr"), XR)
        self.assertEqual(field("enabled"), 0, "written disabled: stock until the level flag")
        self.assertEqual(field("repay"), 0.0)
        self.assertEqual(field("n"), ep.DTF_DEFAULTS["n"])
        self.assertEqual(field("top_n"), 16)
        self.assertLessEqual(ep.BLOCK_STATE + ep.DTF_SIZE, ep.BLOCK_SIZE)

    def test_defaults_as_the_c_filter(self):
        import re
        text = (ROOT / "tools" / "gcpatch" / "bngearth_dtfilter.h").read_text()
        body = text[text.index("bnge_dtf_defaults(struct bnge_dtf *f) {"):]
        body = body[:body.index("}")]
        c = {k: float(v) for k, v in re.findall(r"f->(\w+) = ([0-9.]+);", body)}
        self.assertEqual(c, {k: float(v) for k, v in ep.DTF_DEFAULTS.items()})
        size = re.search(r"\}; +// (0x[0-9a-f]+) bytes", text)
        self.assertEqual(int(size.group(1), 16), ep.DTF_SIZE)

    def test_code_matches_its_hash(self):
        self.assertEqual(hashlib.sha256(enginepatch_code.TRAMP + enginepatch_code.FILTER).hexdigest(),
                         enginepatch_code.SHA256)
        self.assertLessEqual(len(enginepatch_code.TRAMP), ep.BLOCK_FILTER)
        self.assertLessEqual(ep.BLOCK_FILTER + len(enginepatch_code.FILTER), ep.BLOCK_ORIG)
        # the trampoline addresses the state and the original callee where the block puts them
        tr = enginepatch_code.TRAMP
        i = tr.index(b"\x48\x8d\x0d")              # lea rcx,[rip+state]
        self.assertEqual(i + 7 + struct.unpack_from("<i", tr, i + 3)[0], ep.BLOCK_STATE)
        j = tr.index(b"\xff\x25")                  # jmp [rip+orig]
        self.assertEqual(j + 6 + struct.unpack_from("<i", tr, j + 2)[0], ep.BLOCK_ORIG)


class Switches(unittest.TestCase):
    def test_env(self):
        env = dict(os.environ)
        try:
            for k in ("BNGEARTH_DTSMOOTH_OFF", "BNGEARTH_DTSMOOTH", "BNGEARTH_DTSMOOTH_WIN", "BNGEARTH_DTSMOOTH_REPAY"):
                os.environ.pop(k, None)
            self.assertFalse(ep.env_off())
            self.assertFalse(ep.env_on_windows(), "off on Windows by default until tested there")
            os.environ["BNGEARTH_DTSMOOTH"] = "0"
            self.assertTrue(ep.env_off())
            os.environ["BNGEARTH_DTSMOOTH_REPAY"] = "0"
            self.assertEqual(ep.env_params(), {"repay": 0.0})
            self.assertIsNone(ep.attach(None, wait_s=0, log=lambda s: None))
        finally:
            os.environ.clear()
            os.environ.update(env)


if __name__ == "__main__":
    unittest.main()
