import sys
from pathlib import Path
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cliffdecals
import cliffrocks as cr
import geodesy


class CliffDecalsTest(unittest.TestCase):
    def fixture(self, box=(0,0,160,160), slope=55, code=60, blocked=False, cliff=True, color_at=None):
        xs = np.arange(41)*4.
        keep = np.zeros((40,40), bool)
        if cliff:
            keep[8:32,16:28] = True
        def elev(lat, lon):
            return np.asarray(lon)*cr.WORLD_M_PER_DEG*np.tan(np.radians(slope))+20
        return cliffdecals.rows(geodesy.EnuFrame(0,0), box, xs, xs, keep,
                               np.full((41,41),code), np.full((40,40),blocked),
                               np.zeros((40,40),int), elev, color_at)

    def test_overlapping_patches_follow_only_the_boundary(self):
        rows = self.fixture()
        self.assertGreater(len(rows),50)
        self.assertEqual(rows,self.fixture())
        frame = geodesy.EnuFrame(0,0)
        west, _, _ = frame.geodetic_to_enu(0,64/cr.WORLD_M_PER_DEG,0)
        east, _, _ = frame.geodetic_to_enu(0,112/cr.WORLD_M_PER_DEG,0)
        _, south, _ = frame.geodetic_to_enu(32/cr.WORLD_M_PER_DEG,0,0)
        _, north, _ = frame.geodetic_to_enu(128/cr.WORLD_M_PER_DEG,0,0)
        for row in rows:
            self.assertEqual(len(row),9)
            self.assertTrue(abs(row[1]-west)<.01 or abs(row[1]-east)<.01
                            or abs(row[2]-south)<.01 or abs(row[2]-north)<.01)
            self.assertAlmostEqual(np.linalg.norm(row[4:7]),1,places=5)
            self.assertGreaterEqual(row[8],cliffdecals.MIN_SIZE_M)
        # The steep upper/lower rims need multiple stamps per horizontal cell.
        upper = sorted((r for r in rows if abs(r[2]-north)<.01),key=lambda r:r[1])
        gaps = np.linalg.norm(np.diff(np.array([r[1:4] for r in upper]),axis=0),axis=1)
        self.assertLessEqual(gaps.max(),cliffdecals.SPACING_M+.01)

    def test_safe_high_slope_and_qualified_cliff_are_mandatory(self):
        for kwargs in ({'slope':39},{'code':50},{'code':80},{'blocked':True},{'cliff':False}):
            self.assertEqual(self.fixture(**kwargs),[])

    def test_adjacent_tiles_have_no_artificial_border_or_duplicate_stamps(self):
        freeze = lambda rs: {tuple(r) for r in rs}
        all_rows = freeze(self.fixture())
        left = freeze(self.fixture(box=(0,0,88,160)))
        right = freeze(self.fixture(box=(88,0,160,160)))
        self.assertFalse(left & right)
        self.assertEqual(all_rows,left|right)

    def test_no_colour_sampling_when_rim_belongs_to_another_tile(self):
        def unexpected(lat, lon):
            self.fail('empty decal set must not request a regional colour field')
        self.assertEqual(self.fixture(box=(200,200,300,300), color_at=unexpected), [])


if __name__ == '__main__':
    unittest.main()
