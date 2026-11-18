"""An untagged minor road's surface from the aerial's z17 tiles on the disk (roadsurf.py, earthfast.road_photo_surface):
the kernel's verdict against the photo's own references, the store's verdicts used from the next run on (T0), the
road specs that take them, and the cache keys that carry them."""
import math
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import mapcells  # noqa: E402
import mapgeom  # noqa: E402
import roads  # noqa: E402
import roadsurf  # noqa: E402

Z = 17
N = 2 ** Z * 256
LAT, LON = 33.2520, -96.2250            # (Hunt County, Texas: no regional photo source's box)
FIELD, GREY, TAN = (70, 120, 60), (120, 120, 122), (205, 190, 155)


def _px(lat, lon):
    return (lon + 180) / 360 * N, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * N


def _lat_of_row(y):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / N))))


TX, TY = (int(v // 256) for v in _px(LAT, LON))
# roads across the tile: (row, colour, class, surface, expected verdict)
ROADS = [(20, GREY, "tertiary", None, None), (50, GREY, "tertiary", None, None), (80, GREY, "tertiary", None, None),
         (110, TAN, "residential", "gravel", None), (140, TAN, "residential", "gravel", None),
         (170, TAN, "residential", "gravel", None),
         (200, GREY, "residential", None, roadsurf.PAVED), (230, TAN, "residential", None, roadsurf.UNPAVED),
         (245, GREY, "track", None, roadsurf.PAVED)]


def write_tiles(root):
    """The tile (TX, TY) with the roads drawn 5 px wide across a field, under root/17/x/y.jpg."""
    a = np.zeros((256, 256, 3), np.uint8)
    a[:] = FIELD
    for row, col, *_ in ROADS:
        a[max(0, row - 2):row + 3, :] = col
    d = os.path.join(root, str(Z), str(TX))
    os.makedirs(d, exist_ok=True)
    Image.fromarray(a).save(os.path.join(d, f"{TY}.jpg"), quality=95)


def line(row):
    lat = _lat_of_row(TY * 256 + row + 0.5)
    lon0 = (TX * 256 + 6) / N * 360 - 180
    lon1 = (TX * 256 + 250) / N * 360 - 180
    return [(lon0, lat), ((lon0 + lon1) / 2, lat), (lon1, lat)]


def layer(rows=ROADS):
    coords, part_off, ring_off, coord_off = [], [0], [0], [0]
    for r in rows:
        coords += line(r[0])
        coord_off.append(len(coords))
        ring_off.append(len(coord_off) - 1)
        part_off.append(len(ring_off) - 1)
    n = len(rows)
    surf = [(i, r[3]) for i, r in enumerate(rows) if r[3]]
    return mapgeom.Features(
        layer="roads", ids=np.array([f"rs{i:03d}" for i in range(n)]), kind=np.full(n, mapgeom.LINE, np.uint8),
        part_off=np.array(part_off, np.int64), ring_off=np.array(ring_off, np.int64),
        coord_off=np.array(coord_off, np.int64), lonlat=np.array(coords, np.float64),
        attrs={"class": np.array([r[2] for r in rows]), "subtype": np.array(["road"] * n)},
        rules={"surface": mapgeom.RuleTable(np.array([s[0] for s in surf], np.int32), np.array([s[1] for s in surf], "<U32"),
                                            np.zeros(len(surf), np.float32), np.ones(len(surf), np.float32)),
               "width": mapgeom.RuleTable(np.zeros(0, np.int32), np.zeros(0, np.float32), np.zeros(0, np.float32),
                                          np.zeros(0, np.float32)),
               "level": mapgeom.RuleTable(np.zeros(0, np.int32), np.zeros(0, np.float32), np.zeros(0, np.float32),
                                          np.zeros(0, np.float32)),
               "flags": mapgeom.RuleTable(np.zeros(0, np.int32), np.zeros(0, "<U8"), np.zeros(0, np.float32),
                                          np.zeros(0, np.float32)),
               "subclass": mapgeom.RuleTable(np.zeros(0, np.int32), np.zeros(0, "<U8"), np.zeros(0, np.float32),
                                             np.zeros(0, np.float32))})


class RoadSurfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "imagery", "naipusda")
        write_tiles(self.root)
        roadsurf._entries.clear()
        roadsurf._digests.clear()
        roadsurf._store_seen.clear()
        self.addCleanup(roadsurf._store_seen.clear)
        self.addCleanup(roadsurf._entries.clear)
        self.addCleanup(roadsurf._digests.clear)
        for p in (mock.patch.object(roadsurf, "cache_dir", lambda: self.tmp.name), mock.patch.object(roadsurf, "ON", True)):
            p.start()
            self.addCleanup(p.stop)

    def kernel(self, rows=ROADS, roles=None):
        pts, off, pr = [], [0], []
        for k, r in enumerate(rows):
            pts += line(r[0])
            off.append(len(pts))
            pr.append(k)
        role = np.array(roles if roles is not None else
                        [1 if r[2] == "tertiary" else 2 if r[3] else 0 for r in rows], np.int8)
        return fast._ef.road_photo_surface(
            self.root, Z, np.array(pts, np.float64), np.array(off, np.int64), np.array(pr, np.int64), role,
            list(roadsurf.OFFSETS_M),
            {"step": roadsurf.STEP_M, "end": roadsurf.END_M, "dark": roadsurf.DARK_L, "min_n": roadsurf.MIN_SAMPLES,
             "min_have": roadsurf.MIN_HAVE, "min_refs": roadsurf.MIN_REFS, "margin": roadsurf.MARGIN,
             "outlier": roadsurf.OUTLIER, "verge": roadsurf.VERGE_M, "vis_min": roadsurf.VIS_MIN,
             "verge_sat": roadsurf.VERGE_SAT, "verge_exg": roadsurf.VERGE_EXG, "verge_l": roadsurf.VERGE_L},
            list(roadsurf.FLOOR), list(roadsurf.SCALE))

    def test_kernel_judges_by_the_references(self):
        feats, verdict, refs, n_read = self.kernel()
        self.assertEqual(n_read, 1)
        for (row, col, cls, surf, want), v, f in zip(ROADS, verdict.tolist(), feats):
            if want is not None:
                self.assertEqual(v, want, f"row {row}: {f}")
        self.assertEqual(divmod(int(refs[16]), 1000), (3, 3))
        grey = feats[0]
        self.assertAlmostEqual(float(grey[1]), 0.299 * GREY[0] + 0.587 * GREY[1] + 0.114 * GREY[2], delta=4)
        self.assertEqual(float(grey[5]), 1.0, "all its tiles on the disk")

    def test_kernel_no_references_no_verdict(self):
        roles = [1 if r[2] == "tertiary" else -1 if r[3] else 0 for r in ROADS]     # (no unpaved reference)
        _, verdict, _, _ = self.kernel(roles=roles)
        self.assertTrue(all(v in (0, -1) for v, r in zip(verdict.tolist(), roles) if r == 0))

    def test_kernel_tiles_not_on_the_disk(self):
        os.remove(os.path.join(self.root, str(Z), str(TX), f"{TY}.jpg"))
        feats, verdict, _, n_read = self.kernel()
        self.assertEqual(n_read, 0)
        self.assertTrue((verdict[[6, 7, 8]] == -1).all())
        self.assertEqual(float(feats[6][5]), 0.0)

    def test_specs_take_the_verdicts_from_the_next_run(self):
        f = layer()
        key = mapcells.cell_of(LAT, f.lonlat[0, 0])
        before = [None if s is None else s.kind for s in roads.road_specs(layer())]
        self.assertEqual(before[6:], ["paved", "paved", "dirt"])
        with mock.patch.dict(os.environ, {roadsurf._T0_ENV: repr(time.time() - 60)}):
            got = roadsurf.measure_cell(key, f, [])
            self.assertEqual((got["paved"], got["unpaved"]), (2, 1))
            self.assertEqual(roadsurf.cell_tags(key), (), "made in this run: not used in it")
            self.assertEqual([s.kind for s in roads.road_specs(layer())][6:], ["paved", "paved", "dirt"])
        roadsurf._entries.clear()          # (the next start: a new process)
        roadsurf._digests.clear()
        roadsurf._store_seen.clear()
        with mock.patch.dict(os.environ, {roadsurf._T0_ENV: repr(time.time() + 1)}):
            specs = roads.road_specs(layer())
            self.assertEqual([s.kind for s in specs][6:], ["paved", "gravel", "paved"])
            self.assertEqual(specs[8].surf, "asphalt", "a track the photo shows paved")
            self.assertEqual(len(roadsurf.cell_tags(key)), 1)
            lat0, lon0 = float(f.lonlat[0, 1]), float(f.lonlat[0, 0])
            self.assertEqual(roadsurf.area_tags(lat0, lon0, lat0 + 1e-4, lon0 + 1e-4), roadsurf.cell_tags(key))
            self.assertEqual(roadsurf.area_tags(LAT + 1.0, LON, LAT + 1.0001, LON + 1e-4), (), "far away: none")
            self.assertIsNone(roadsurf.measure_cell(key, layer(), []), "judged once: nothing left")

    def test_off(self):
        f = layer()
        key = mapcells.cell_of(LAT, f.lonlat[0, 0])
        with mock.patch.object(roadsurf, "NOW", True):
            roadsurf.measure_cell(key, f, [])
            self.assertEqual([s.kind for s in roads.road_specs(layer())][6:], ["paved", "gravel", "paved"])
            with mock.patch.object(roadsurf, "ON", False):
                self.assertEqual([s.kind for s in roads.road_specs(layer())][6:], ["paved", "paved", "dirt"])
                self.assertEqual(roadsurf.cell_tags(key), ())


if __name__ == "__main__":
    unittest.main()
