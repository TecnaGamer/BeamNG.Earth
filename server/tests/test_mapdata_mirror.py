"""Disk-mirror format and recovery, using local arrays and no network sources."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mapdata


class MirrorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mapdata-mirror-test-")
        self.addCleanup(self.tmp.cleanup)
        self.md = self.reader()
        self.path = Path(self.tmp.name) / "mapdata/v2/test/landcover.npz"
        self.path.parent.mkdir(parents=True)
        self.arrays = {
            "provenance": np.array('{"source":"fixture"}'),
            "tags.name": np.array(["Auckland", "東京", ""]),
            "empty": np.empty((0, 2), dtype=np.float64),
            "scalar": np.array(7, dtype=np.int16),
            "float": np.array([np.nan, np.inf, -0.0], dtype=">f8"),
            "boundary": np.arange(mapdata.MIRROR_SMALL, dtype=np.uint8),
            "large": np.arange(mapdata.MIRROR_SMALL + 1, dtype=np.uint8),
        }
        np.savez_compressed(self.path, **self.arrays)
        self.directory = Path(self.md._mirror_dir(self.path))

    def reader(self):
        md = object.__new__(mapdata.MapData)
        md.cache_dir = self.tmp.name
        md._mirror_note = mock.Mock()
        return md

    def build(self):
        return self.md._mirrored(None, "landcover", str(self.path), self.arrays)

    def assert_arrays(self, actual):
        self.assertIsNotNone(actual)
        self.assertEqual(list(actual), list(self.arrays))
        for name, expected in self.arrays.items():
            with self.subTest(array=name):
                self.assertEqual(actual[name].dtype, expected.dtype)
                self.assertEqual(actual[name].shape, expected.shape)
                self.assertEqual(actual[name].tobytes(), expected.tobytes())

    def test_roundtrip_and_independent_reader_preserve_bits_and_large_mappings(self):
        result = self.build()
        self.assert_arrays(result)
        self.assertFalse(result["large"].flags.writeable)
        self.assertFalse(result["large"].flags.owndata)
        self.assertIsInstance(result["large"].base, np.memmap)
        self.assert_arrays(self.reader()._mirror_load(str(self.path)))
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"ok", "small.npz", "large.npy"})
        with zipfile.ZipFile(self.directory / "small.npz") as archive:
            self.assertEqual(set(archive.namelist()), {n + ".npy" for n in self.arrays if n != "large"})
            self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED for i in archive.infolist()))

    def test_many_small_fields_need_only_one_bundle_and_marker(self):
        self.arrays = {"provenance": np.array("null"), **{f"rule.{i}": np.array(i) for i in range(80)}}
        np.savez_compressed(self.path, **self.arrays)
        self.assert_arrays(self.build())
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"ok", "small.npz"})

    def test_missing_or_truncated_bundle_is_rebuilt_from_source(self):
        self.build()
        bundle = self.directory / "small.npz"
        for damaged in (None, b"PK\x03\x04truncated"):
            with self.subTest(damaged=damaged):
                if damaged is None:
                    bundle.unlink()
                else:
                    bundle.write_bytes(damaged)
                self.assertIsNone(self.md._mirror_load(str(self.path)))
                raw = self.md._load(str(self.path))
                self.assert_arrays(self.md._mirrored(None, "landcover", str(self.path), raw))
                self.assert_arrays(self.md._mirror_load(str(self.path)))

    def test_missing_member_is_a_cache_miss(self):
        self.build()
        with zipfile.ZipFile(self.directory / "small.npz", "w") as archive:
            with archive.open("provenance.npy", "w") as fh:
                np.save(fh, self.arrays["provenance"], allow_pickle=False)
        self.assertIsNone(self.md._mirror_load(str(self.path)))

    def test_missing_large_array_is_a_cache_miss(self):
        self.build()
        (self.directory / "large.npy").unlink()
        self.assertIsNone(self.md._mirror_load(str(self.path)))

    def test_source_replacement_invalidates_and_rebuilds_mirror(self):
        self.build()
        old = self.path.stat()
        self.arrays["scalar"] = np.array(9, dtype=np.int16)
        np.savez_compressed(self.path, **self.arrays)
        os.utime(self.path, ns=(old.st_atime_ns, old.st_mtime_ns + 2_000_000_000))
        self.assertIsNone(self.md._mirror_load(str(self.path)))
        self.assert_arrays(self.build())

    def test_old_build_mirror_is_left_available(self):
        legacy = Path(self.tmp.name) / "mapdata-mirror/v2/test/landcover"
        legacy.mkdir(parents=True)
        marker = legacy / "ok"
        marker.write_text("old build's layout")
        self.assert_arrays(self.build())
        self.assertNotEqual(legacy, self.directory)
        self.assertEqual(marker.read_text(), "old build's layout")

    def test_disabled_mirror_returns_source_without_writing(self):
        with mock.patch.object(mapdata, "MIRROR_MB", 0):
            self.assertIs(self.build(), self.arrays)
            self.assertIsNone(self.md._mirror_load(str(self.path)))
        self.assertFalse(self.directory.exists())


if __name__ == "__main__":
    unittest.main()
