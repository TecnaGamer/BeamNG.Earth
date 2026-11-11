"""Road roughness (bumps.py): off by default, a pure function of the place, continuous, faded at bridges."""
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bumps  # noqa: E402
import geodesy
import grading
import roads

FRAME = geodesy.EnuFrame(52.03, 21.43)
FLAT = staticmethod(lambda la, lo: np.full(np.shape(la), 100.0))


def line(length=2000.0, step=4.0, frame=FRAME):
    e = np.arange(0.0, length + step, step)
    la, lo, _ = frame.enu_to_geodetic(e, 40.0 * np.sin(e / 700.0), 0.0)
    return np.c_[lo, la]


def spec(cls="tertiary", kind="paved"):
    return roads.Spec(7.0, kind, False, cls)


class OffTest(unittest.TestCase):
    def test_off_by_default_plans_unchanged(self):
        self.assertEqual(bumps.SCALE, 0.0)
        ll = line(600.0)
        a = grading.plan_road(FRAME, ll, spec(), FLAT)
        with mock.patch.object(bumps, "SCALE", 0.0):
            b = grading.plan_road(FRAME, ll, spec(), FLAT)
        np.testing.assert_array_equal(a.z, b.z)
        np.testing.assert_allclose(a.z, 100.0)
        self.assertIsNone(a.bank)


@mock.patch.object(bumps, "SCALE", 1.0)
class OnTest(unittest.TestCase):
    def test_plan_gets_roughness_within_bounds(self):
        p = grading.plan_road(FRAME, line(), spec(), FLAT)
        dz = p.z - 100.0
        self.assertGreater(dz.std(), 0.02)
        self.assertLess(np.abs(dz).max(), 0.6)
        self.assertIsNotNone(p.bank)

    def test_same_place_same_offset_in_any_frame(self):
        ll = line(1000.0)
        far = geodesy.EnuFrame(52.2, 21.0)
        for fr in (FRAME, far):
            p = grading.plan_road(fr, ll, spec(), FLAT)
            la, lo, _ = fr.enu_to_geodetic(p.xy[:, 0], p.xy[:, 1], 0.0)
            dz, _ = bumps.offsets(la, lo, bumps._gain(spec()), "mid")
            np.testing.assert_allclose(p.z - 100.0, dz, atol=1e-9)

    def test_continuous_everywhere(self):
        x = np.linspace(2_000_000.0, 2_006_000.0, 300_001)          # 2 cm apart
        y = np.full_like(x, 5_800_000.0)
        lat = y / bumps._M_PER_DEG
        lon = x / (bumps._M_PER_DEG * np.cos(np.radians(lat)))
        for group in ("major", "mid", "minor"):
            dz, roll = bumps.offsets(lat, lon, 1.0, group)
            self.assertLess(np.abs(np.diff(dz)).max(), 0.01, group)     # under 1 cm per 2 cm: no steps
            self.assertLess(np.abs(np.diff(roll)).max(), 1e-4, group)

    def test_faded_before_a_bridge(self):
        p = grading.plan_road(FRAME, line(300.0), spec(), FLAT)
        with mock.patch.object(bumps, "SCALE", 0.0):
            q = grading.plan_roads(FRAME, [(line(300.0), spec())], FLAT, abut=[(True, True)])[0]
        r = grading.plan_roads(FRAME, [(line(300.0), spec())], FLAT, abut=[(True, True)])[0]
        self.assertAlmostEqual(float(r.z[0]), float(q.z[0]), places=9)
        self.assertAlmostEqual(float(r.z[-1]), float(q.z[-1]), places=9)
        self.assertGreater(np.abs(p.z - 100.0)[10:-10].max(), 0.01)

    def test_sections_mostly_long_bad_stretches_rare(self):
        ll = line(40000.0, 10.0)
        X, Y = bumps._metres(ll[:, 1], ll[:, 0])
        gain, _share, _lift = bumps.sections(X, Y, "mid")
        self.assertGreaterEqual(gain.min(), 1.8 - 1e-9)
        self.assertLessEqual(gain.max(), 4.0 + 1e-9)
        very_bad = (gain > 3.9).mean()
        self.assertGreater(very_bad, 0.0)
        self.assertLess(very_bad, 0.15)

    def test_classes_rank(self):
        ll = line(3000.0)
        rough = {c: grading.plan_road(FRAME, ll, spec(c), FLAT).z.std() for c in ("motorway", "tertiary")}
        self.assertLess(rough["motorway"], rough["tertiary"])


if __name__ == "__main__":
    unittest.main()
