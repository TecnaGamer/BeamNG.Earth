"""geo_test_cache: a geotile copy is removed after its test, and one whose owner process is gone by the next."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geo_test_cache as gtc  # noqa: E402


class WorkCacheTest(unittest.TestCase):

    def setUp(self):
        self.disk = tempfile.mkdtemp(prefix="gtc-")
        self.src = tempfile.mkdtemp(prefix="gtc-src-")
        (Path(self.src) / "dem").mkdir()
        (Path(self.src) / "dem" / "a.bin").write_bytes(b"x" * 10)
        self.env = mock.patch.dict(os.environ, {"BNGE_GEO_TEST_TMP": self.disk})
        self.env.start()
        self.srcp = mock.patch.object(gtc, "SOURCE", Path(self.src))
        self.srcp.start()

    def tearDown(self):
        self.srcp.stop()
        self.env.stop()
        gtc._remove(self.disk)
        gtc._remove(self.src)

    def test_a_copy_is_gone_after_its_test_even_one_that_failed(self):
        with gtc.work_cache() as tmp:
            self.assertTrue((Path(tmp) / "dem" / "a.bin").exists())
            self.assertEqual((Path(tmp) / gtc.OWNER).read_text(), str(os.getpid()))
        self.assertFalse(os.path.exists(tmp))
        with self.assertRaises(RuntimeError):
            with gtc.work_cache() as tmp2:
                raise RuntimeError("the test failed")
        self.assertFalse(os.path.exists(tmp2))

    def test_a_copy_whose_owner_is_gone_is_swept_and_a_live_ones_is_kept(self):
        dead = Path(self.disk) / (gtc.PREFIX + "dead")
        live = Path(self.disk) / (gtc.PREFIX + "live")
        for p, pid in ((dead, 2 ** 22 + 12345), (live, os.getpid())):
            p.mkdir()
            (p / gtc.OWNER).write_text(str(pid))
        with gtc.work_cache():
            pass
        self.assertFalse(dead.exists(), "a killed worker's copy")
        self.assertTrue(live.exists(), "a running test's copy")

    def test_alive(self):
        self.assertTrue(gtc._alive(os.getpid()))
        self.assertFalse(gtc._alive(2 ** 22 + 12345))
        self.assertFalse(gtc._alive(0))


if __name__ == "__main__":
    unittest.main()
