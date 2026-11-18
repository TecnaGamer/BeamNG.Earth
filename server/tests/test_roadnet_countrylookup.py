"""europewalks.country_for through earthfast's CountryLookup (earthfast 451, made once) against the scalar path it
replaced (europe_countries of one-point arrays with the sorted territories at every call): random points inside and
outside the territories' box and the border bands (NaN, inf, the box's edges), as float, int, bool and numpy scalars:
the same country, the border index asked at the same points. Text is outside the input contract: TypeError.

The Python version is kept here only as the reference (the server has no Python twin: fast.require)."""
import os
import random
import sys
import types
import unittest
from unittest import mock

import numpy as np

os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                                                       "regional"))     # (PR #505's data: the tests' slice)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import countrybounds  # noqa: E402
import europewalks  # noqa: E402
import fast  # noqa: E402


def ref_country_for(lon, lat):
    """europewalks.country_for before earthfast 451, verbatim."""
    _border_path, _countries = europewalks._border_path, europewalks._countries
    fast.require(391, "europe_countries")
    return fast._ef.europe_countries(_border_path, np.array([lon], np.float64), np.array([lat], np.float64),
                                     _countries())[0]


def point(rng):
    r = rng.random()
    if r < 0.6:
        lon, lat = rng.uniform(-12, 40), rng.uniform(35, 71)
    elif r < 0.8:
        lon, lat = rng.uniform(-180, 180), rng.uniform(-90, 90)
    else:
        lon, lat = rng.choice([(float("nan"), 50.0), (10.0, float("inf")), (-168.0, 30.0), (-33.0, 30.0), (10.0, 25.0),
                               (10, 48), (np.float32(11.0), np.float32(48.5)), (np.int64(2), np.int64(48)),
                               (True, 50.0), (np.float64(4.35), 50.85)])
    kind = rng.choice([float, np.float64, lambda v: v])
    return (kind(lon), kind(lat)) if isinstance(lon, float) else (lon, lat)


class CountryLookupParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert not isinstance(countrybounds.bundled(), countrybounds.Absent), "the tests' regional fixture is missing"

    def test_points(self):
        rng = random.Random(451)
        found = 0
        real = countrybounds.bundled
        for k in range(4000):
            lon, lat = point(rng)
            asks = {"ref": 0, "new": 0}
            for side, fn in (("ref", ref_country_for), ("new", europewalks.country_for)):
                def counted(side=side):
                    asks[side] += 1
                    return real()
                with mock.patch.object(countrybounds, "bundled", counted):
                    if side == "ref":
                        want = fn(lon, lat)
                    else:
                        got = fn(lon, lat)
            self.assertEqual(got, want, (k, lon, lat))
            self.assertEqual(asks["new"], asks["ref"], (k, lon, lat))
            found += want is not None
        self.assertGreater(found, 600)

    def test_text_refused(self):
        for lon, lat in (("10.0", 48.0), (10.0, "48"), (None, 48.0), (np.array(10.0), 0), (10.0, np.array([48.0])),
                         (np.ma.masked_array(10.0), 48.0)):
            with self.assertRaises(TypeError):
                europewalks.country_for(lon, lat)

    def test_no_border_data(self):
        with mock.patch.object(countrybounds, "bundled", lambda: countrybounds.Absent()):
            self.assertIsNone(europewalks.country_for(11.0, 48.5))
            self.assertIsNone(ref_country_for(11.0, 48.5))


class VersionSkewTest(unittest.TestCase):
    def test_a_450_extension_is_refused(self):
        self.assertGreaterEqual(fast.MIN_VERSION, 451)
        self.assertGreaterEqual(fast._ef.VERSION, fast.MIN_VERSION)
        europewalks.country_for(11.0, 48.5)             # (the lookup made with the real module)
        with mock.patch.object(fast, "_ef", types.SimpleNamespace(VERSION=450)):
            with self.assertRaises(fast.NativeMissing):
                europewalks.country_for(11.0, 48.5)


if __name__ == "__main__":
    unittest.main()
