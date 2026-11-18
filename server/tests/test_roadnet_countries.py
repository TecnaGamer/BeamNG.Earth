"""The European streets' country lookup (earthfast 391, roadnet::countries: the road revamp's R2a) against
europewalks.country_for's Python and _spec_inputs' loop of it, value for value: random points over Europe, North
Africa, the Middle East, the world, on the box test's edges (25 N, 168 W, 33 W), NaN / inf points; random road layers
(features of 0-7 points in several parts and rings) for the middle points; no border data (countrybounds.Absent); the
border data asked only once a point passes the box test; malformed offsets raise IndexError, never a panic.

The Python versions are kept here only as the reference (the server has no Python twin: fast.require)."""
import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                                                       "regional"))     # (PR #505's data: the tests' slice)
import random  # noqa: E402
import sys  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

import numpy as np  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import countrybounds  # noqa: E402
import europewalks  # noqa: E402
import mapgeom  # noqa: E402


def ref_country_for(lon, lat):
    from countrybounds import bundled
    from europeinfra import COUNTRIES

    # Covers the supported territories and the eastern end of Russia.
    if lat < 25. or -168. < lon < -33.:
        return None
    country = bundled().at(lat, lon)
    return country if country in COUNTRIES else None


def ref_eu_column(feats, mask):
    """_spec_inputs' loop (1f93e283), the country itself kept (eu[i] was `is not None` of it)."""
    n = len(feats)
    po, ro, co = (np.ascontiguousarray(x, np.int64) for x in (feats.part_off, feats.ring_off, feats.coord_off))
    ll = np.ascontiguousarray(feats.lonlat, np.float64).reshape(-1, 2)
    out = [None] * n
    for i in np.flatnonzero(mask).tolist():
        p0, p1 = co[ro[po[i]]], co[ro[po[i + 1]]]
        if p1 > p0:
            out[i] = ref_country_for(*ll[p0 + (p1 - p0) // 2])
    return out


def point(rng):
    w = rng.random()
    if w < 0.5:
        return rng.uniform(-12, 42), rng.uniform(34, 71)           # Europe
    if w < 0.6:
        return rng.uniform(-10, 60), rng.uniform(20, 40)           # North Africa, the Middle East
    if w < 0.7:                                                    # the box test's edges
        return (rng.choice([-168.0, -33.0, rng.uniform(-180, 180), np.nextafter(-33.0, 0), np.nextafter(-168.0, -180)]),
                rng.choice([25.0, np.nextafter(25.0, 0), rng.uniform(-90, 90)]))
    if w < 0.75:
        return (rng.choice([float("nan"), float("inf"), -float("inf"), rng.uniform(-12, 42)]),
                rng.choice([float("nan"), float("inf"), -float("inf"), 90.0, rng.uniform(34, 71)]))
    return rng.uniform(-180, 180), rng.uniform(-90, 90)


def layer(rng):
    n = rng.randint(0, 9)
    po, ro, co, ll = [0], [0], [0], []
    for _ in range(n):
        lon, lat = point(rng)
        for _p in range(rng.choice([1, 1, 2])):
            for _r in range(rng.choice([1, 1, 2])):
                for _ in range(rng.choice([0, 1, 2, 3, 7])):
                    ll.append((lon, lat))
                    lon += rng.uniform(-1e-2, 1e-2)
                    lat += rng.uniform(-1e-2, 1e-2)
                co.append(len(ll))
            ro.append(len(co) - 1)
        po.append(len(ro) - 1)
    return mapgeom.Features("roads", np.array([f"r{i}" for i in range(n)]), np.full(n, 2, np.uint8),
                            np.array(po, np.int64), np.array(ro, np.int64), np.array(co, np.int64),
                            np.array(ll, np.float64).reshape(-1, 2))


def outcome(fn):
    try:
        return "ok", fn()
    except Exception as e:      # noqa: BLE001
        return "raised", type(e)


class CountryParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        idx = countrybounds.bundled()
        assert not isinstance(idx, countrybounds.Absent), "the tests' regional fixture is missing"

    def test_points(self):
        rng = random.Random(388)
        seen = set()
        for k in range(20000):
            lon, lat = point(rng)
            want = ref_country_for(lon, lat)
            got = europewalks.country_for(lon, lat)
            self.assertEqual(got, want, (k, lon, lat))
            # (numpy scalars, as the layer's lonlat gave them)
            self.assertEqual(europewalks.country_for(np.float64(lon), np.float64(lat)), want)
            seen.add(want)
        self.assertGreater(len(seen), 25)       # (many territories met, and None)
        self.assertIn(None, seen)

    def test_road_middle_points(self):
        rng = random.Random(3880)
        met = 0
        for k in range(3000):
            f = layer(rng)
            mask = np.array([rng.random() < 0.7 for _ in range(len(f))], bool)
            want = ref_eu_column(f, mask)
            got = europewalks.road_countries(f, mask)
            self.assertEqual(got, want, k)
            met += sum(c is not None for c in want)
        self.assertGreater(met, 500)

    def test_no_border_data(self):
        with mock.patch.object(countrybounds, "bundled", return_value=countrybounds.Absent()):
            rng = random.Random(9)
            for _ in range(500):
                lon, lat = point(rng)
                self.assertIsNone(europewalks.country_for(lon, lat))
                self.assertEqual(ref_country_for(lon, lat), None)
            f = layer(random.Random(4))
            self.assertEqual(europewalks.road_countries(f, np.ones(len(f), bool)), [None] * len(f))

    def test_border_data_asked_only_past_the_box(self):
        def refused(*a, **k):
            raise AssertionError("border data asked")
        with mock.patch.object(countrybounds, "bundled", refused):
            for lon, lat in ((-100.0, 40.0), (10.0, 24.9), (-34.0, 60.0), (0.0, -float("inf"))):
                self.assertIsNone(europewalks.country_for(lon, lat))
            # (a road layer in the US: asked for none of its roads)
            f = mapgeom.Features("roads", np.array(["a", "b"]), np.full(2, 2, np.uint8), np.array([0, 1, 2]),
                                 np.array([0, 1, 2]), np.array([0, 2, 4]),
                                 np.array([[-100, 40], [-100.1, 40], [-90, 35], [-90, 35.1]], np.float64))
            self.assertEqual(europewalks.road_countries(f, np.ones(2, bool)), [None, None])
            # ... and its error raised where a point needs it, as the Python's was
            with self.assertRaises(AssertionError):
                europewalks.country_for(10.0, 50.0)

    def test_border_index_asked_again_after_none(self):
        """Codex on 98e07733: a border index missing at one road (a manifest still being fetched on a cold start: Absent)
        and there at the next. The Python asked bundled() at every point past the box test; so does the port."""
        real = countrybounds.bundled()
        rng = random.Random(3881)
        changed = 0
        for k in range(300):
            f = layer(rng)
            mask = np.ones(len(f), bool)
            gaps = [rng.random() < 0.3 for _ in range(60)]

            def flaky():
                state = {"k": 0}

                def bundled():
                    state["k"] += 1
                    return countrybounds.Absent() if state["k"] <= len(gaps) and gaps[state["k"] - 1] else real
                return bundled
            with mock.patch.object(countrybounds, "bundled", flaky()):
                want = ref_eu_column(f, mask)
            with mock.patch.object(countrybounds, "bundled", flaky()):
                got = europewalks.road_countries(f, mask)
            self.assertEqual(got, want, k)
            changed += want != ref_eu_column(f, mask)
        self.assertGreater(changed, 20)
        # one point missing the index, the next finding it
        seq = iter([countrybounds.Absent(), real])
        with mock.patch.object(countrybounds, "bundled", lambda: next(seq)):
            import fast
            self.assertEqual(fast._ef.europe_countries(europewalks._border_path, np.array([11.0, 11.0]),
                                                       np.array([48.5, 48.5]), europewalks._countries()), [None, "DE"])

    def test_malformed_offsets_raise_as_numpy(self):
        rng = random.Random(77)
        n_raised = 0
        for k in range(600):
            f = layer(rng)
            if not len(f):
                continue
            arr = rng.choice(["part_off", "ring_off", "coord_off"])
            a = getattr(f, arr).copy()
            j = rng.randrange(len(a))
            a[j] += rng.choice([-50, -3, 3, 50])
            setattr(f, arr, a)
            mask = np.ones(len(f), bool)
            want = outcome(lambda: ref_eu_column(f, mask))
            got = outcome(lambda: europewalks.road_countries(f, mask))
            self.assertEqual(got, want, (k, arr, j))
            n_raised += want[0] == "raised"
        self.assertGreater(n_raised, 50)

    def test_extreme_offsets_wrap_as_numpy(self):
        """Coordinate offsets near the int64 ends (map data never makes them, but no kernel may panic or differ by
        build on them: Codex on 490d400d): numpy's p0 + (p1 - p0) // 2 wraps, a negative index counts from the end."""
        import warnings
        big, small = np.iinfo(np.int64).max, np.iinfo(np.int64).min
        rng = random.Random(78)
        seen = set()
        for k in range(400):
            f = layer(rng)
            if not len(f) or len(f.lonlat) < 2:
                continue
            co = f.coord_off.copy()
            j = rng.randrange(len(co))
            co[j] = rng.choice([big, big - 1, big - 2, small, small + 1, small + 3, -1, -2])
            if rng.random() < 0.5 and len(co) > 1:
                j2 = rng.randrange(len(co))
                co[j2] = rng.choice([big, small, small + 2, big - 5])
            f.coord_off = co
            mask = np.ones(len(f), bool)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                want = outcome(lambda: ref_eu_column(f, mask))
            got = outcome(lambda: europewalks.road_countries(f, mask))
            self.assertEqual(got, want, (k, co.tolist()))
            seen.add(want[0])
        self.assertEqual(seen, {"ok", "raised"})


if __name__ == "__main__":
    unittest.main()
