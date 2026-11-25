"""Winter W2 end to end: an in-process server session (mesh mode, synthetic imagery and DEM) with BNE_WINTER and a
heavy snow preset draws white far tiles and says so in its hello; a live state change (snow_mode) answers 'snow' and
textures every shown far tile again, now without snow, through the live retexture."""
import importlib
import os
import struct
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import snowcover  # noqa: E402
import test_retex  # noqa: E402
from test_imagery import bc1_decode  # noqa: E402


def dds_mean(path):
    """Mean RGB of a far texture's top mip (BC1, DX10 or DXT1 header)."""
    data = Path(path).read_bytes()
    h, w = struct.unpack_from("<II", data, 12)
    off = 148 if data[84:88] == b"DX10" else 128
    n = max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * 8
    return bc1_decode(data[off:off + n], w, h).reshape(-1, 3).mean(0)


class WinterSessionTest(test_retex.MeshHarness):
    def setUp(self):
        self.old = {k: os.environ.get(k) for k in ("BNE_WINTER", "BNE_SNOW")}
        os.environ["BNE_WINTER"], os.environ["BNE_SNOW"] = "on", "heavy"
        importlib.reload(snowcover)
        super().setUp()
        self.server.retex_rate = 200.0

    def tearDown(self):
        super().tearDown()
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(snowcover)

    def textures(self):
        s = self.server
        return {ck: os.path.join(s.out_dir, info.texture) for ck, info in s.tiles.items()
                if getattr(info, "texture", None) and info.material is not None
                and os.path.exists(os.path.join(s.out_dir, info.texture))}

    def test_white_far_tiles_and_a_live_change(self):
        c = self.c
        c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        hello, _ = c.until(lambda m: m["type"] == "hello", timeout=10)
        self.assertEqual(hello.get("snow"), {"mode": "heavy", "cover": 8, "depth": 4})
        aid = self.prepare_anchor(1, test_retex.test_server.LAT, self.LON)["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 0, "cy": 0})
        self.pose(aid, 0.0, 0.0)
        c.until(lambda m: m["type"] == "far_done" and m["anchorId"] == aid, timeout=120)
        snowy = self.textures()
        self.assertGreater(len(snowy), 4)
        means = [dds_mean(p).mean() for p in snowy.values()]
        self.assertGreater(float(np.median(means)), 180.0, f"far tiles under heavy snow: {np.round(means)}")
        # a live change to no snow: 'snow' to the mod, every shown tile textured again (far_retex), without snow
        c.send({"type": "snow_mode", "mode": "none"})
        m, _ = c.until(lambda m: m["type"] == "snow", timeout=10)
        self.assertEqual(m["snow"], {"mode": "none"})
        want = set(snowy)
        got = set()

        def retexed(m):
            if m["type"] == "far_retex":
                got.add(self.server.far_ck(m["anchorId"], tuple(m["key"]) if isinstance(m["key"], list)
                                           else m["key"]))
            return want <= got or len(got) >= len(want)
        c.until(retexed, timeout=120)
        after = self.textures()
        bare = [dds_mean(after[ck]).mean() for ck in want if ck in after]
        self.assertLess(float(np.median(bare)), float(np.median(means)) - 40.0, f"{np.round(bare)}")


if __name__ == "__main__":
    unittest.main()
