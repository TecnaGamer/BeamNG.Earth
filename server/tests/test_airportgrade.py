"""airportgrade: runways graded into a DEM sampler. Away from airports the DEM bit for bit; on a runway the
graded field (runwaygeom), the same whichever box asks first (plans per group, cached); the wrapped DEM
(far tiles) gives the same heights as apply."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import airportfixtures as fx  # noqa: E402
import airportgrade  # noqa: E402
import runwaygeom  # noqa: E402


class PlaneDem:
    """dem.Dem-like: a bumpy plane (zoom ignored); counts calls."""

    def __init__(self):
        self.fn = fx.plane(100.0, 0.01, -0.005, bumps=3.0)
        self.calls = 0

    def sample(self, lat, lon, zoom, progress=None):
        self.calls += 1
        return self.fn(lat, lon)


class AirportGradeTest(unittest.TestCase):
    def setUp(self):
        self.db = fx.db()
        self.dem = PlaneDem()
        self.ag = airportgrade.AirportGrader(self.db, self.dem)

    def grid(self, lat0, lon0, half=0.01, n=81):
        la, lo = np.meshgrid(np.linspace(lat0 - half, lat0 + half, n), np.linspace(lon0 - half, lon0 + half, n), indexing="ij")
        return la, lo

    def test_runway_pads_cover_the_paved_runways_end_to_end(self):
        # (GitHub #337: only the markings were drawn; the runway's own surface came from the photo alone)
        import geodesy
        la, lo = self.grid(45.0, 7.0)
        pads = self.ag.runway_pads(la, lo)
        rws = [r for g in self.ag._groups(la, lo) for r in g if r.paved and not r.closed and r.surface != "water"]
        self.assertEqual(len(pads), len(rws))
        self.assertTrue(rws)
        for (pla, plo), rw in zip(pads, rws):
            f = geodesy.EnuFrame(float(np.mean(pla)), float(np.mean(plo)), 0.0)
            e, n, _ = f.geodetic_to_enu(pla, plo, np.zeros(4))
            sides = np.hypot(np.diff(np.r_[e, e[:1]]), np.diff(np.r_[n, n[:1]]))
            self.assertAlmostEqual(float(max(sides)), rw.length_m, delta=1.0)
            self.assertAlmostEqual(float(min(sides)), rw.width_m + 2 * airportgrade.RUNWAY_PAD_SHOULDER_M, delta=0.5)
        self.assertEqual(self.ag.runway_pads(*self.grid(46.0, 8.0)), [], "no runway: no pads")

    def test_far_from_airports_untouched(self):
        la, lo = self.grid(46.0, 8.0)
        z = self.dem.fn(la, lo)
        out = self.ag.apply(la, lo, z)
        self.assertIs(out, z)
        self.assertEqual(self.ag.stats["plans"], 0)

    def test_runway_graded_and_cached(self):
        la, lo = self.grid(45.0, 7.0)
        z = self.dem.fn(la, lo)
        out = self.ag.apply(la, lo, z)
        self.assertGreater(np.abs(out - z).max(), 0.1, "the bumps are graded away on the runways")
        plans = self.ag.stats["plans"]
        self.assertGreater(plans, 0)
        # the same as runwaygeom with the group's plans
        groups = self.ag._groups(la, lo)
        want = runwaygeom.apply([p for g in groups for p in self.ag.plans_for(g)], la, lo, z)[0]
        np.testing.assert_array_equal(out, want)
        # a smaller box over the same runway: same heights there, no new plans
        sub = self.ag.apply(la[30:50, 30:50], lo[30:50, 30:50], z[30:50, 30:50])
        np.testing.assert_array_equal(sub, out[30:50, 30:50])
        self.assertEqual(self.ag.stats["plans"], plans)

    def test_marking_chunks(self):
        """A runway's markings as road chunks: white / yellow line materials, pieces of at most
        MARK_PIECE_M, each in exactly one road tile (its midpoint's), z from the elevation function."""
        import geodesy
        import roads
        frame = geodesy.EnuFrame(45.0, 7.0)
        elev = lambda la, lo: self.ag.apply(la, lo, self.dem.fn(la, lo))     # noqa: E731
        tiles = [(tx, ty) for tx in range(-8, 8) for ty in range(-8, 8)]
        per = {t: self.ag.tile_chunks(frame, *t, elev, roads.tile_bbox) for t in tiles}
        allc = [c for cs in per.values() for c in cs]
        self.assertGreater(len(allc), 50)
        self.assertLessEqual({c["m"] for c in allc}, {"line_runway_white", "line_runway_yellow"})
        for (tx, ty), cs in per.items():
            x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
            for c in cs:
                n = np.array(c["n"])
                L = float(np.hypot(*(n[-1, :2] - n[0, :2])))
                self.assertLessEqual(L, airportgrade.MARK_PIECE_M + 1e-6)
                mx, my = (n[0, :2] + n[-1, :2]) / 2
                self.assertTrue(x0 - 0.01 <= mx < x1 + 0.01 and y0 - 0.01 <= my < y1 + 0.01)
        c = allc[0]
        la, lo, _ = frame.enu_to_geodetic(np.array([c["n"][0][0]]), np.array([c["n"][0][1]]), np.zeros(1))
        _, _, z = frame.geodetic_to_enu(la, lo, elev(la, lo))
        self.assertAlmostEqual(c["n"][0][2], float(z[0]), places=2)
        far = self.ag.tile_chunks(frame, 400, 400, elev, roads.tile_bbox)
        self.assertEqual(far, [], "no runway, no markings")

    def test_wrapped_dem(self):
        la, lo = self.grid(45.0, 7.0, half=0.004, n=41)
        src = self.ag.wrap(self.dem)
        np.testing.assert_array_equal(src.sample(la, lo, 15), self.ag.apply(la, lo, self.dem.fn(la, lo)))


if __name__ == "__main__":
    unittest.main()
