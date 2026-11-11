"""revisit: a far tile's texture kept as a file beside its item (SIDE_KINDS), hard-linked in when kept and out on a
hit: nothing written either way (a warm drive rewrote every far texture, 2026-11-04). Zips of before (the texture
inside) still load; a zip whose file was cut back alone is a miss."""
import os
import sys
import tempfile
import unittest
import zipfile
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import revisit  # noqa: E402
import revisitpack  # noqa: E402


class SideTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = os.path.join(self.tmp.name, "cache")
        self.session = os.path.join(self.tmp.name, "user", "current", "temp", "beamngEarth", "s1")   # (staging: links)
        os.makedirs(self.session)
        self.on = revisit.ON
        revisit.ON = True
        self.addCleanup(setattr, revisit, "ON", self.on)

    def session_file(self, name, data):
        p = os.path.join(self.session, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_linked_in_and_out(self):
        tex = self.session_file("far_1_x.dds", b"DDS texture bytes")
        self.assertTrue(revisit.store(self.cache, "far", "abcdef", {"built.npz": b"npz", "tex.dds": revisit.Side(tex)},
                                      {"tex_bytes": 17}))
        zp = revisit._path(self.cache, "far", "abcdef")
        side = revisit._side(zp, "tex.dds")
        self.assertEqual(os.stat(side).st_ino, os.stat(tex).st_ino)          # linked in: nothing written
        self.assertFalse(os.path.exists(zp))                                 # (kept in a pack, not a zip)
        raw = revisitpack.store_for(self.cache).get("far", "abcdef")
        self.assertNotIn(b"DDS texture bytes", raw)                          # (the texture beside it, not inside)
        blobs, extra = revisit.load(self.cache, "far", "abcdef")
        self.assertEqual(blobs["built.npz"], b"npz")
        self.assertIsInstance(blobs["tex.dds"], revisit.Side)
        out = os.path.join(self.session, "far_2_x.dds")
        revisit.place(blobs["tex.dds"], out)
        self.assertEqual(os.stat(out).st_ino, os.stat(side).st_ino)          # linked out
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"DDS texture bytes")
        revisit.place(b"plain bytes", os.path.join(self.session, "x.bin"))
        with open(os.path.join(self.session, "x.bin"), "rb") as f:
            self.assertEqual(f.read(), b"plain bytes")

    def test_bytes_kept_as_a_file_too(self):
        self.assertTrue(revisit.store(self.cache, "fartex", "ab12", {"tex.dds": b"T", "mask.dds": b"M"}, {}))
        blobs, _ = revisit.load(self.cache, "fartex", "ab12")
        self.assertEqual((blobs["tex.dds"].read(), blobs["mask.dds"].read()), (b"T", b"M"))

    def test_other_kinds_inside(self):
        self.assertTrue(revisit.store(self.cache, "roads", "cd34", {"tex.dds": b"T"}, {}))
        blobs, _ = revisit.load(self.cache, "roads", "cd34")
        self.assertEqual(blobs["tex.dds"], b"T")

    def test_old_zip_and_lost_file(self):
        zp = revisit._path(self.cache, "far", "ef56")
        os.makedirs(os.path.dirname(zp))
        with zipfile.ZipFile(zp, "w") as z:     # (a zip of before: the texture inside)
            z.writestr("tex.dds", b"old")
            z.writestr("meta.json", json.dumps({"names": ["tex.dds"], "extra": None}))
        self.assertEqual(revisit.load(self.cache, "far", "ef56")[0]["tex.dds"], b"old")
        tex = self.session_file("t.dds", b"new")
        revisit.store(self.cache, "far", "ef57", {"tex.dds": revisit.Side(tex)}, {})
        os.remove(revisit._side(revisit._path(self.cache, "far", "ef57"), "tex.dds"))
        self.assertIsNone(revisit.load(self.cache, "far", "ef57"))


    def test_no_staging_copies(self):
        """Outside a user folder's temp tree (no staging folder) a hit is copied: the fallback tmp name is shared."""
        tex = self.session_file("a.dds", b"T1")
        revisit.store(self.cache, "far", "aa11", {"tex.dds": revisit.Side(tex)}, {})
        blobs, _ = revisit.load(self.cache, "far", "aa11")
        out = os.path.join(self.tmp.name, "plain_dir_x.dds")
        revisit.place(blobs["tex.dds"], out)
        self.assertNotEqual(os.stat(out).st_ino, os.stat(blobs["tex.dds"].path).st_ino)


if __name__ == "__main__":
    unittest.main()
