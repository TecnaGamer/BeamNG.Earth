"""The GPU service (earthfast 650, gpu.rs; research report moves 1 and 2, 2026-11-27): the BC7 photo's mips in integers
(the same bytes whichever of the GPU and the CPU encodes the blocks, and a new kept-photo key), the Steam Deck's gate
(the GPU only while parked or loading), one device in the server (BNGE_GPU_OWNER), the block processes' photos packed
in the server's process, and the per-call log."""
import logging
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import bc7  # noqa: E402
import fast  # noqa: E402
import imagery  # noqa: E402

EF = fast._ef
HAVE = fast.AVAILABLE and getattr(EF, "VERSION", 0) >= 650


def photo_like(rng, n, nc=4):
    lum = np.cumsum(np.cumsum(rng.normal(0, 1.0, (n, n)), 0), 1)
    lum = (lum - lum.min()) / (np.ptp(lum) + 1e-9) * 180 + 40
    rgb = np.clip(lum[..., None] * np.array([1.0, 0.9, 0.75]) + rng.normal(0, 6, (n, n, 3)), 0, 255).astype(np.uint8)
    if nc == 3:
        return rgb
    return np.ascontiguousarray(np.concatenate([rgb, np.full((n, n, 1), 251, np.uint8)], 2))


def int_mips_reference(img, lut, sharpen):
    """bc1fast.rs mip_levels_int written in numpy (int64): the colour in 2^20 linear units from the LUT, the alpha in
    2^12ths of a byte, each level the rounded 2 x 2 mean of the last unsharpened one, the colour sharpened in 1/65536ths
    where both sides are >= 4 and back to sRGB by the float path's thresholds, the alpha rounded."""
    one = 1 << 20
    lin_lut = np.round(np.clip(lut.astype(np.float64), 0, 1) * one).astype(np.int64)
    # the sRGB byte of a linear value: the count of thresholds at or under it (the bytes of the float path's formula)
    img = np.asarray(img, np.uint8)
    nc = img.shape[2]
    lin = np.empty(img.shape, np.int64)
    lin[..., :3] = lin_lut[img[..., :3]]
    if nc == 4:
        lin[..., 3] = img[..., 3].astype(np.int64) << 12
    sq = int(round(float(np.float32(sharpen)) * 65536))
    out = []
    while max(lin.shape[:2]) > 1:
        h, w = lin.shape[:2]
        if h > 1 and w > 1:
            lin = (lin[0::2, 0::2] + lin[0::2, 1::2] + lin[1::2, 0::2] + lin[1::2, 1::2] + 2) >> 2
        elif h > 1:
            lin = (lin[0::2] + lin[1::2] + 1) >> 1
        else:
            lin = (lin[:, 0::2] + lin[:, 1::2] + 1) >> 1
        h, w = lin.shape[:2]
        col = lin[..., :3]
        if min(h, w) >= 4 and sq > 0:
            p = np.pad(col, ((1, 1), (1, 1), (0, 0)), mode="edge")
            s = sum(p[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3))
            num = sq * (9 * col - s) + 9 * 65536 // 2
            col = col + np.floor_divide(num, 9 * 65536)
        col = np.clip(col, 0, one)
        b = np.empty(lin.shape, np.uint8)
        b[..., :3] = _SRGB[col]
        if nc == 4:
            b[..., 3] = np.minimum((lin[..., 3] + 2048) >> 12, 255)
        out.append(b)
    return out


def _srgb_table():
    """The sRGB byte of every linear value 0 .. 2^20, from the float path (earthfast.mip_levels on flat images):
    found by bisection per byte on the float kernel's own output, so the reference shares no code with the integer
    kernel."""
    one = 1 << 20
    lut = np.zeros(256, np.float32)
    table = np.zeros(one + 1, np.uint8)
    # the float path's byte for linear value v: a 2 x 2 flat image whose LUT maps byte 0 to v / 2^20 (no sharpen on
    # 1 x 1); bisect the 255 thresholds over v
    def byte(v):
        lut[0] = np.float32(v / one)
        return int(EF.mip_levels(np.zeros((2, 2, 3), np.uint8), lut, 0.0)[0][0, 0, 0])
    lo = 0
    for k in range(1, 256):
        a, b = lo, one + 1
        while a < b:
            m = (a + b) // 2
            if byte(m) >= k:
                b = m
            else:
                a = m + 1
        table[lo:a] = k - 1
        lo = a
    table[lo:] = 255
    return table


_SRGB = _srgb_table() if HAVE else None


@unittest.skipUnless(HAVE, "earthfast 650")
class IntegerMipsTest(unittest.TestCase):
    def test_matches_the_reference_bit_for_bit(self):
        lut, sh = imagery._TO_LIN, float(imagery.MIP_SHARPEN)
        for n, seed in ((256, 1), (64, 2), (8, 3), (1, 4)):
            img = photo_like(np.random.default_rng(seed), n)
            got = bc7.mips(img)
            np.testing.assert_array_equal(got[0], img)
            want = int_mips_reference(img, lut, sh)
            self.assertEqual(len(got) - 1, len(want), n)
            for k, (a, b) in enumerate(zip(got[1:], want)):
                np.testing.assert_array_equal(a, b, f"{n}² level {k + 1}")

    def test_within_one_step_of_the_float_chain(self):
        img = photo_like(np.random.default_rng(7), 512)
        ref = imagery.mip_arrays(img)
        got = bc7.mips(img)
        for a, b in zip(ref[1:], got[1:]):
            d = np.abs(a.astype(int) - b.astype(int))
            self.assertLessEqual(int(d.max()), 1)
            self.assertLess(float((d > 0).mean()), 0.01)

    def test_the_cpu_and_the_gpu_paths_share_the_mips(self):
        """The mips come before the encoder: the GPU's and the CPU's files differ only in a few float32 blocks, never
        in the levels' texels (their decoded levels within the encoders' own error)."""
        img = photo_like(np.random.default_rng(3), 256)
        chain = bc7.mips(img)
        cpu = b"".join(bc7.encode(m) for m in chain)
        with mock.patch.object(bc7, "_GPU_FAILED", True):
            cpu_file = bc7.dds(img)
        self.assertEqual(cpu_file[148:], cpu)
        if not fast.gpu() or not fast.gpu_now("bc7", count=False):
            self.skipTest("no GPU here")
        gpu = bc7._encode_gpu(chain)
        self.assertEqual(len(gpu), len(cpu))
        same = sum(gpu[i:i + 16] == cpu[i:i + 16] for i in range(0, len(cpu), 16))
        self.assertGreater(same / (len(cpu) // 16), 0.9)


class KeptPhotoKeyTest(unittest.TestCase):
    def test_the_integer_mips_have_their_own_key(self):
        import earthd
        import revisit
        self.assertGreaterEqual(earthd.BASE_PHOTO_VERSION, 3)
        from tests.test_base_photo_revisit import fake_server
        with tempfile.TemporaryDirectory() as d, mock.patch.object(revisit, "ON", True):
            srv = fake_server(d)
            a = SimpleNamespace(id=1, lat=39.28, lon=-76.59, zooms=[17])
            now = srv.base_revisit_key(a, 0.0, 384.0)
            with mock.patch.object(earthd, "BASE_PHOTO_VERSION", 2):
                old = srv.base_revisit_key(a, 0.0, 384.0)
            self.assertIsNotNone(now)
            self.assertNotEqual(now, old, "a photo kept with the float mips is not taken")


@unittest.skipUnless(HAVE, "earthfast 650")
class DeckGateTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"BNGE_GPU_DECK": "1", "BNGE_GPU_PARK_S": "0.2"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(EF.gpu_note_pose, 0.0, True)       # (leave the gate open for the other tests)

    def test_driving_parked_loading(self):
        import time
        EF.gpu_note_pose(25.0, False)
        self.assertFalse(EF.gpu_allowed(), "driving")
        self.assertEqual(EF.gpu_gate()[:1], (True,))
        EF.gpu_note_pose(0.0, False)
        self.assertFalse(EF.gpu_allowed(), "stopped a moment")
        time.sleep(0.25)
        self.assertTrue(EF.gpu_allowed(), "parked")
        EF.gpu_note_pose(25.0, True)
        self.assertTrue(EF.gpu_allowed(), "a teleport loading")
        with mock.patch.dict(os.environ, {"BNGE_GPU_DECK": "0"}):
            EF.gpu_note_pose(25.0, False)
            self.assertTrue(EF.gpu_allowed(), "not a Deck: always")

    def test_photo_encodes_on_the_cpu_while_driving(self):
        with mock.patch.object(fast, "gpu", lambda: "Test GPU (Vulkan)"), \
                mock.patch.object(fast._ef, "bc7_encode_gpu", side_effect=AssertionError("no GPU call")), \
                mock.patch.object(bc7, "_GPU_FAILED", False), \
                mock.patch.dict(fast._gpu_sum, {"t0": None, "fallbacks": {}}):
            EF.gpu_note_pose(25.0, False)
            self.assertFalse(bc7.gpu_ok())
            img = photo_like(np.random.default_rng(1), 32)
            d = bc7.dds(img)            # (the CPU's encoder: bc7_encode_gpu never called)
            self.assertEqual(d[148:], b"".join(bc7.encode(m) for m in bc7.mips(img)))
            self.assertEqual(fast._gpu_sum["fallbacks"].get(("bc7", "driving")), 1)
            self.assertFalse(bc7._GPU_FAILED, "a gate's refusal is no failure")

    def test_the_pose_handler_feeds_the_gate(self):
        """host/pose.rs motion: every pose's speed and teleport_pending reach the gate (gpu.rs note_pose)."""
        import earthd
        with open(os.path.join(os.path.dirname(earthd.__file__), "..", "native", "earthfast", "src", "host",
                               "pose.rs")) as f:
            src = f.read()
        self.assertIn("crate::gpu::note_pose(speed, loading)", src)
        self.assertIn('getattr("teleport_pending")', src)


@unittest.skipUnless(HAVE, "earthfast 650")
class OwnerTest(unittest.TestCase):
    def test_another_process_makes_no_device(self):
        import subprocess
        code = ("import sys; sys.path.insert(0, %r); import fast; print(fast._ef.gpu_name())"
                % os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env = dict(os.environ, BNGE_GPU_OWNER="1")       # (pid 1: never this child)
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "None", out.stderr)

    def test_earthd_claims_it_at_its_start(self):
        import earthd
        import inspect
        main = inspect.getsource(earthd.main)
        self.assertLess(main.index("fast.gpu_owner()"), main.index("argparse.ArgumentParser"),
                        "before anything starts a process")


class BlockProcessPackTest(unittest.TestCase):
    """With the server's GPU on, a block process's photo comes back unpacked and the server packs it (one device)."""

    def test_pack_flag(self):
        import blockworker
        import inspect
        self.assertIn("pack", inspect.signature(blockworker.base_photo).parameters)
        import earthd
        for fn in (earthd.Server._base_in, earthd.Server._quick_elsewhere):
            src = inspect.getsource(fn)
            self.assertIn("pack=not pack_here", src, fn.__name__)
            self.assertIn("Server._base_pack(self", src, fn.__name__)

    def test_base_pack_writes_the_entry(self):
        import earthd
        import terraincache
        with tempfile.TemporaryDirectory() as d:
            srv = SimpleNamespace(user_dir=d, vfs_dir="/temp/beamngEarth/s1", base_px=64)
            rgb = photo_like(np.random.default_rng(2), 64, 3)
            with mock.patch.object(bc7, "_GPU_FAILED", True):
                earthd.Server._base_pack(srv, "hm_1_0_0_base.jpg", rgb, None, 512.0)
                want = bc7.dds(fast._ef.block_rgba(rgb, earthd.BASE_AO))
            path = os.path.join(terraincache.cache_dir(d), terraincache.base_key(
                "/temp/beamngEarth/s1/hm_1_0_0_base.jpg", earthd.base_ao_vfs(64)) + ".dds")
            with open(path, "rb") as f:
                self.assertEqual(f.read(), want)


@unittest.skipUnless(HAVE, "earthfast 650")
class LogTest(unittest.TestCase):
    def test_summary_and_fallback_lines(self):
        with mock.patch.dict(fast._gpu_sum, {"t0": 0.0, "fallbacks": {("bc7", "driving"): 3}}), \
                mock.patch.object(EF, "gpu_stats", lambda reset: {"bc7": {
                    "calls": 4, "fails": 0, "submits": 180, "wait_s": 0.3, "wall_s": 0.5, "max_submit_s": 0.004,
                    "up_bytes": 1, "down_bytes": 1}}), \
                self.assertLogs("earthd", logging.INFO) as cm:
            fast._gpu_summary(now=fast.GPU_SUMMARY_S + 1)
        self.assertTrue(any("bc7 4 calls" in m and "longest 4.0 ms" in m and "bc7 driving 3" in m
                            for m in cm.output), cm.output)

    def test_last_call_counters(self):
        if not fast.gpu():
            self.skipTest("no GPU here")
        img = photo_like(np.random.default_rng(4), 128)
        EF.bc7_encode_gpu([img])
        st = EF.gpu_last()
        self.assertEqual(st["kernel"], "bc7")
        self.assertTrue(st["ok"])
        self.assertGreaterEqual(st["submits"], 2)
        self.assertEqual(st["down_bytes"], 32 * 32 * 16)
        self.assertIn("describe", EF.gpu_info())


if __name__ == "__main__":
    unittest.main()
