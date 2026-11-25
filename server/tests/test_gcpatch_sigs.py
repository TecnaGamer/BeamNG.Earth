"""The engine patches found by code, and never silent when they do not apply (2026-11-22: BeamNG 0.40.6 moved one
vtable by 0x20 and the Linux terfast / texkeep, pinned to exact addresses, dropped out for every player unseen).

- tools/gcpatch/bngearth_sigscan.h + bngearth_sigs.h: the Linux patches' masked whole-function signatures, searched
  in a BeamNG.drive.x64 laid out as the loader does (tools/gcpatch/test_sigs.c): found with the expected targets, and
  refused for a changed byte and for a second copy (needs gcc and the game's Linux executable: BNGE_GAME_BIN, else the
  Steam library's copy; skipped without them);
- launcher.patch_state: the status lines' states (the mod's enginepatches.lua has the same rules, its own test);
- enginepatch.keep_saying: a Windows patch that does not act says so again when the level starts."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import enginepatch  # noqa: E402
import launcher  # noqa: E402

GCPATCH = ROOT / "tools/gcpatch"
REF_BUILD_ID = "1ec1c227b48210ede9094f925c42793bffe35aef"     # BeamNG.drive 0.40.6 Linux (the signatures' reference)
REF = {"terfast": {"bounds": "0x1735dc0", "mats": "0x1735c00", "vb": "0x1734b30", "xform": "0x12b00d0",
                   "mats_tail": "0x1733c30", "get_transform": "0x80cd80", "vb_scan": "0x1734c55",
                   "vb_body": "0x1734c61", "vb_after": "0x1734d72"},
       "texkeep": {"create": "0xc6ece0", "loader": "0x173ec40", "request_delete": "0xc6a630",
                   "destroy_self": "0xc7d4d0", "ret_cached": "0x173f2ee", "ret_packed": "0x173f71e"}}


def game_binary():
    p = os.environ.get("BNGE_GAME_BIN") or os.path.expanduser(
        "~/.local/share/Steam/steamapps/common/BeamNG.drive/BinLinux/BeamNG.drive.x64")
    return p if os.path.isfile(p) else None


def build_id(path):
    out = subprocess.run(["readelf", "-n", path], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "Build ID:" in line:
            return line.split("Build ID:")[1].strip()
    return None


@unittest.skipUnless(shutil.which("gcc") and game_binary(), "needs gcc and BeamNG.drive.x64 (Linux)")
class LinuxSignatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bnge_sigs_")
        cls.bin = game_binary()
        cls.exe = {}
        for patch in ("terfast", "texkeep"):
            exe = os.path.join(cls.tmp, "test_sigs_" + patch)
            subprocess.run(["gcc", "-O2", "-o", exe, str(GCPATCH / "test_sigs.c"), "-DPATCH_" + patch.upper(),
                            "-lm", "-pthread"], check=True, cwd=GCPATCH, capture_output=True)
            cls.exe[patch] = exe

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_sig(self, patch, mode="ok"):
        r = subprocess.run([self.exe[patch], self.bin, mode], capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout.strip()

    def test_found(self):
        ref = build_id(self.bin) == REF_BUILD_ID
        for patch in ("terfast", "texkeep"):
            code, out = self.run_sig(patch)
            self.assertEqual(code, 0, out)
            self.assertTrue(out.startswith(patch + ":"), out)
            words = out.split()
            got = {words[i]: words[i + 1] for i in range(1, len(words) - 1, 2) if words[i] in REF[patch]}
            if ref:       # (another build: found is all that can be said; the addresses may have moved)
                self.assertEqual(got, REF[patch])
            if patch == "texkeep":
                self.assertEqual(len(out.split("slots")[1].split()), 3 if ref else len(out.split("slots")[1].split()))

    def test_refused(self):
        for patch in ("terfast", "texkeep"):
            for mode, want in (("flip", "0 matches"), ("dup", "2 matches")):
                code, out = self.run_sig(patch, mode)
                self.assertEqual(code, 0, out)          # (the refusal is the pass)
                self.assertIn("refused", out)
                self.assertIn(want, out)

    @unittest.skipUnless(shutil.which("objdump"), "needs objdump")
    def test_header_is_current(self):
        if build_id(self.bin) != REF_BUILD_ID:
            self.skipTest("not the reference build")
        try:
            import elftools  # noqa: F401
        except ImportError:
            self.skipTest("needs pyelftools")
        out = subprocess.run([sys.executable, str(GCPATCH / "mksigs.py"), self.bin], capture_output=True, text=True,
                             check=True, timeout=600).stdout
        self.assertEqual(out, (GCPATCH / "bngearth_sigs.h").read_text())


class PatchState(unittest.TestCase):
    def test_states(self):
        t = 1795300000
        cases = [
            (f"{t} pid 1: applied: grass kept across terrain block moves (BeamNG.Earth level)", "on"),
            (f"{t} pid 1: ready: applied while the BeamNG.Earth level runs", "on"),
            (f"{t} launcher pid 2: ready: applied while the BeamNG.Earth level runs (launcher patch, block at 0x1)", "on"),
            (f"{t} pid 1: not applied: another game build (tf_bounds: 0 matches of its code; build id x); stock", "failed"),
            (f"{t} pid 1: applied (BeamNG.Earth level): x (found by signature; HALF APPLIED: y)", "failed"),
            (f"{t} pid 1: off (BNGEARTH_TERFAST_OFF set)", "off"),
            (f"{t} pid 1: something nobody wrote yet", "failed"),
            ("", "none"), ("junk", "none"),
        ]
        for text, want in cases:
            self.assertEqual(launcher.patch_state(text, t)[0], want, text)
        self.assertEqual(launcher.patch_state(f"{t - 100} pid 1: applied: x", t)[0], "none")   # an earlier run's


class KeepSaying(unittest.TestCase):
    def test_said_again_at_the_level_start(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "temp"))
            name = "beamngEarth_test.txt"
            enginepatch.write_status(d, name, "not applied: x")
            path = os.path.join(d, "temp", name)
            os.utime(path, (0, 0))
            enginepatch.keep_saying(d, name, "not applied: x")
            time.sleep(2 * enginepatch.FLAG_POLL_S)
            self.assertEqual(os.path.getmtime(path), 0)                  # no level: not written again
            open(os.path.join(d, "temp", enginepatch.LEVEL_FLAG), "w").close()
            t0 = time.time()
            while os.path.getmtime(path) == 0 and time.time() - t0 < 5:
                time.sleep(0.05)
            self.assertGreater(os.path.getmtime(path), 0)
            with open(path) as f:
                self.assertIn("not applied: x", f.read())


if __name__ == "__main__":
    unittest.main()
