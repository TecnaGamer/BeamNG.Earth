"""winterpacks: the leaf-type and water-share packs read from the tile host (the version the switch names, checked
against the manifest's sizes and SHA-256s, decided once per run) or a local build; lookups by point and by box, NaN
where nothing is known."""
import hashlib
import io
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pagesdata  # noqa: E402
import winterpacks as wp  # noqa: E402

BASE = "https://tiles.example.test"


def png(arr, mode):
    b = io.BytesIO()
    Image.fromarray(arr, mode).save(b, format="PNG")
    return b.getvalue()


def leaf_png():
    """Evergreen 200 (0.8) and trees 125 (0.5) in the cell of 44.25 N, 72.6 W (Vermont); 25 / 250 elsewhere."""
    eg = np.full((wp.LEAF_H, wp.LEAF_W), 25, np.uint8)
    tr = np.zeros((wp.LEAF_H, wp.LEAF_W), np.uint8)
    j, i = int((90 - 44.25) * 20), int((-72.6 + 180) * 20)
    eg[j, i], tr[j, i] = 200, 125
    return png(np.dstack([eg, tr]), "LA")


def water_shard():
    """Shard 43/22 (94 W..92 W, 46 N..44 N): 250 (all water) in its north-west quarter, 255 (no data) in its last
    row, 0 elsewhere."""
    a = np.zeros((500, 500), np.uint8)
    a[:250, :250] = 250
    a[-1, :] = 255
    return png(a, "L")


def manifests(leaf, shard):
    lm = {"format": 1, "name": "leaftype", "version": "v1", "built": "2026-11-25T00:00:00Z", "build": "0123456789abcdef",
          "grid": {"res_deg": 0.05, "width": 7200, "height": 3600, "west": -180, "north": 90, "scale": 250,
                   "bands": ["evergreen", "trees"], "mode": "LA"},
          "files": [{"path": "leaftype.png", "size": len(leaf), "sha256": hashlib.sha256(leaf).hexdigest()}],
          "attribution": "x" * 50}
    wm = {"format": 1, "name": "watershare", "version": "v1", "built": "2026-11-25T00:00:00Z",
          "build": "fedcba9876543210",
          "grid": {"res_deg": 0.004, "cells_per_shard": 500, "shard_deg": 2, "scale": 250, "nodata": 255},
          "shards": {"43/22": [len(shard), hashlib.sha256(shard).hexdigest()]}, "uniform": {"44/22": 0},
          "attribution": "x" * 50}
    return lm, wm


LEAF = leaf_png()
SHARD = water_shard()
LM, WM = manifests(LEAF, SHARD)


class Resp:
    def __init__(self, body):
        self.body = body


class Fetcher:
    def __init__(self, files):
        self.files, self.urls = files, []

    def get(self, url, kind="demand", max_bytes=None, **_):
        self.urls.append(url)
        if url not in self.files:
            import netfetch
            raise netfetch.NotFound("404", url, "tiles.example.test")
        return Resp(self.files[url])


def site(leaf=LEAF, shard=SHARD):
    return {f"{BASE}/leaftype/v1/manifest.json": json.dumps(LM).encode(),
            f"{BASE}/leaftype/v1/leaftype.png": leaf,
            f"{BASE}/watershare/v1/manifest.json": json.dumps(WM).encode(),
            f"{BASE}/watershare/v1/43/22.png": shard}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = self.tmp.name
        pagesdata.session_reset()
        wp.reset()
        env = {k: v for k, v in os.environ.items() if k not in (wp.LOCAL_ENV, pagesdata.SESSION_ENV, *wp.ENV.values())}
        env.update({"BNGE_LEAFTYPE": "v1", "BNGE_WATERSHARE": "v1"})
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.base = mock.patch.object(wp, "cdn_base", lambda: BASE)
        self.base.start()

    def tearDown(self):
        self.base.stop()
        self.env.stop()
        pagesdata.session_reset()
        wp.reset()
        self.tmp.cleanup()


class LeafTest(Base):
    def test_evergreen_share_and_tree_cover_of_a_cell(self):
        p = wp.Packs(self.cache, Fetcher(site()))
        self.assertAlmostEqual(p.evergreen_share(44.25, -72.6), 0.8)
        self.assertAlmostEqual(p.tree_cover(44.25, -72.6), 0.5)
        got = p.evergreen_share(np.array([44.25, 10.0]), np.array([-72.6, 10.0]))
        self.assertEqual(got.tolist(), [0.8, 0.1])
        g = p.leaf_grid()
        self.assertEqual((g.evergreen.shape, g.res, g.west, g.north, g.scale), ((3600, 7200), 0.05, -180.0, 90.0, 250))

    def test_versions_name_each_build(self):
        p = wp.Packs(self.cache, Fetcher(site()))
        self.assertEqual(p.versions(), {"leaftype": "v1-0123456789abcdef", "watershare": "v1-fedcba9876543210"})

    def test_off_is_nan_and_no_version(self):
        with mock.patch.dict(os.environ, {"BNGE_LEAFTYPE": "off"}):
            f = Fetcher(site())
            p = wp.Packs(self.cache, f)
            self.assertTrue(math.isnan(p.evergreen_share(44.25, -72.6)))
            self.assertIsNone(p.versions()["leaftype"])
            self.assertFalse(any("leaftype" in u for u in f.urls))

    def test_a_file_unlike_its_manifest_is_not_kept(self):
        bad = LEAF[:-1] + bytes([LEAF[-1] ^ 1])
        p = wp.Packs(self.cache, Fetcher(site(leaf=bad)))
        with mock.patch("revisit.fault") as fault:
            self.assertTrue(math.isnan(p.evergreen_share(44.25, -72.6)))
            self.assertTrue(fault.called)
        self.assertFalse(any(n == "leaftype.png" for _, _, fs in os.walk(self.cache) for n in fs))

    def test_files_are_kept_on_disk_per_build(self):
        f = Fetcher(site())
        wp.Packs(self.cache, f).evergreen_share(44.25, -72.6)
        pagesdata.session_reset()
        f2 = Fetcher({})            # (offline: the pinned manifest and the kept file)
        self.assertAlmostEqual(wp.Packs(self.cache, f2).evergreen_share(44.25, -72.6), 0.8)
        self.assertEqual(f2.urls, [])
        kept = os.path.join(self.cache, "pagesdata", "winterpacks", "leaftype", "v1-0123456789abcdef", "leaftype.png")
        self.assertTrue(os.path.exists(kept))

    def test_the_runs_decision_is_kept_while_the_run_lasts(self):
        sess = os.path.join(self.cache, "pagesdata", "session", "run1")
        with mock.patch.dict(os.environ, {pagesdata.SESSION_ENV: sess}):
            os.makedirs(sess)
            wp.Packs(self.cache, Fetcher({})).versions()        # (no manifest: none for the run)
            pagesdata.session_reset()                           # (another process of the same run)
            p = wp.Packs(self.cache, Fetcher(site()))
            self.assertIsNone(p.versions()["leaftype"])


class WaterTest(Base):
    def test_point_lookups(self):
        p = wp.Packs(self.cache, Fetcher(site()))
        lat = np.array([45.9, 44.0001, 44.5, 45.0, 0.0])
        lon = np.array([-93.9, -93.5, -93.0, -91.0, 0.0])
        got = p.water_share(lat, lon)
        self.assertEqual(got[0], 1.0)               # the north-west quarter
        self.assertTrue(math.isnan(got[1]))         # the last row: no data
        self.assertEqual(got[2], 0.0)
        self.assertEqual(got[3], 0.0)               # shard 44/22: uniform 0, never fetched
        self.assertTrue(math.isnan(got[4]))         # no shard: no data
        self.assertEqual(p.water_share(45.9, -93.9), 1.0)

    def test_uniform_and_unnamed_shards_are_not_fetched(self):
        f = Fetcher(site())
        wp.Packs(self.cache, f).water_share(np.array([45.0, 0.0]), np.array([-91.0, 0.0]))
        self.assertEqual([u for u in f.urls if u.endswith(".png")], [])

    def test_grid_and_box_mean(self):
        p = wp.Packs(self.cache, Fetcher(site()))
        g = p.water_grid(45.0, -94.0, 46.0, -93.0)
        self.assertEqual(g.values.shape, (251, 251))
        self.assertAlmostEqual(g.north, 46.0)
        self.assertAlmostEqual(g.west, -94.0)
        self.assertEqual(int(g.values[0, 0]), 250)
        self.assertAlmostEqual(p.water_share_box(45.001, -94.0, 45.996, -93.004), 1.0)
        across = p.water_grid(44.5, -92.5, 45.5, -91.5)        # over two shards
        self.assertEqual(across.values.shape, (251, 251))
        self.assertEqual(int(across.values.max()), 0)
        self.assertTrue(math.isnan(p.water_share_box(0.0, 0.0, 0.5, 0.5)))


class LocalTest(Base):
    def test_a_local_build_is_read_without_the_network(self):
        d = os.path.join(self.cache, "build")
        os.makedirs(os.path.join(d, "leaftype"))
        os.makedirs(os.path.join(d, "watershare", "43"))
        for rel, body in (("leaftype/leaftype.png", LEAF), ("leaftype/manifest.json", json.dumps(LM).encode()),
                          ("watershare/43/22.png", SHARD), ("watershare/manifest.json", json.dumps(WM).encode())):
            with open(os.path.join(d, *rel.split("/")), "wb") as fh:
                fh.write(body)
        with mock.patch.dict(os.environ, {wp.LOCAL_ENV: d}):
            f = Fetcher({})
            p = wp.Packs(self.cache, f)
            self.assertAlmostEqual(p.evergreen_share(44.25, -72.6), 0.8)
            self.assertEqual(p.water_share(45.9, -93.9), 1.0)
            self.assertEqual(f.urls, [])


class ManifestTest(unittest.TestCase):
    def test_a_manifest_of_another_shape_is_refused(self):
        self.assertIsNotNone(wp.check_manifest(LM, "leaftype", "v1"))
        self.assertIsNone(wp.check_manifest(LM, "leaftype", "v2"))
        self.assertIsNone(wp.check_manifest(LM, "watershare"))
        self.assertIsNone(wp.check_manifest(dict(WM, uniform={"43/22": 300}), "watershare"))
        self.assertIsNone(wp.check_manifest(dict(WM, shards={"../x": [1, "0" * 64]}), "watershare"))


if __name__ == "__main__":
    unittest.main()
