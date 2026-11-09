"""The direction boards step back on German motorways where the German Autobahn signs stand (dirsigns.steps_back,
signstream passing autobahn=True only for a German tile with German shard data); trunk, rural and urban boards stay."""
import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regional"))
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import countrybounds  # noqa: E402
import deautobahn  # noqa: E402
import dirsigns  # noqa: E402
import geodesy  # noqa: E402
import signstream  # noqa: E402
import speedsigns  # noqa: E402
from test_de_autobahn_tile import DE, CZ, TileCase, german_record  # noqa: E402

M_LAT = 111320.0


def setUpModule():
    assert countrybounds.bundled().codes_at([(49.02, 12.16)]) == ["DE"], "the border fixture is missing"


def road(xy, rank, link=False):
    """A speedsigns road row: (lon/lat line (unused here), xy line, meta [half width, dir, rank, urban, link], ...)."""
    xy = np.asarray(xy, np.float64)
    return (None, xy, (9.0, 1, rank, 0.0, 1.0 if link else 0.0), [], [], [])


def board(x, y, road_x, road_y, kind=2):
    b = np.zeros(12)
    b[0:2], b[4:6], b[7] = (x, y), (road_x, road_y), kind
    return b


class StepBackTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(*DE)
        self.rows = {"m": road([(-2000, 0), (2000, 0)], speedsigns.RANK["motorway"]),
                     "t": road([(-2000, 500), (2000, 500)], speedsigns.RANK["trunk"]),
                     "p": road([(-2000, 1000), (2000, 1000)], speedsigns.RANK["primary"]),
                     "l": road([(0, -30), (300, -200)], speedsigns.RANK["motorway"], link=True)}

    def test_motorway_board_dropped_in_germany(self):
        b = np.array([board(100, -12, 100, 0)])
        self.assertEqual(dirsigns.steps_back(b, self.rows, self.frame).tolist(), [True])

    def test_trunk_board_kept(self):
        b = np.array([board(100, 488, 100, 500)])           # the kernel's freeway kind on a rural one-way trunk
        self.assertEqual(dirsigns.steps_back(b, self.rows, self.frame).tolist(), [False])

    def test_rural_and_urban_kept(self):
        b = np.array([board(100, 988, 100, 1000, kind=1), board(100, 988, 100, 1000, kind=0)])
        self.assertEqual(dirsigns.steps_back(b, self.rows, self.frame).tolist(), [False, False])

    def test_link_board_kept(self):
        b = np.array([board(150, -110, 150, -100)])
        self.assertEqual(dirsigns.steps_back(b, self.rows, self.frame).tolist(), [False])

    def test_outside_germany_unchanged(self):
        frame = geodesy.EnuFrame(*CZ)
        b = np.array([board(100, -12, 100, 0)])
        self.assertEqual(dirsigns.steps_back(b, self.rows, frame).tolist(), [False])


class SignstreamFlagTest(TileCase):
    def flag(self, origin, marked=True, records=None):
        with mock.patch.object(dirsigns, 'build_tile', return_value=(None, {})) as build, \
                mock.patch.object(signstream, 'build_tile', wraps=signstream.build_tile):
            orig = signstream.build_tile

            def with_dirs(*a, **kw):
                kw['dirs'] = True
                return orig(*a, **kw)
            with mock.patch.object(signstream, 'build_tile', side_effect=with_dirs):
                self.tile(origin, records, marked=marked)
            self.assertTrue(build.called)
            return build.call_args.kwargs.get('autobahn', False)

    def test_german_tile_steps_back(self):
        self.assertTrue(self.flag(DE))

    def test_old_shards_keep_boards(self):
        old = lambda md, frame: [german_record(md, frame, de=False)]      # noqa: E731
        self.assertFalse(self.flag(DE, marked=False, records=old))

    def test_outside_germany_keeps_boards(self):
        self.assertFalse(self.flag(CZ))


if __name__ == "__main__":
    unittest.main()
