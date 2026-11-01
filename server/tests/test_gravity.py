"""server/gravity.py: WGS84 normal gravity, the spherical-harmonic engine, EGM2008.

Offline by default: the normal field is checked against its published constants and its exact
closed form, the synthesis engine against the closed form (normal field as a model), finite
differences and high-precision Legendre functions, and the long-wavelength EGM2008 field against a
degree-36 coefficient fixture (fixtures/egm2008_n36.npz, 23 kB, EGM2008 truncated).

BNGE_GRAVITY_REAL=1 also runs the full EGM2008 model (degree 2190; downloads 74 MB once into
~/.cache/beamng-earth/gravity/ if missing) against published values: GeographicLib's Gravity
example on Everest and its EGM2008 geoid heights, plus anomaly sanity values and global ranges.

The earthd side (Provider / Tracker, the 'gravity' message): the fixture model as EGM2008, the
normal fallback, a background download that never blocks (faked), the recompute rule, and an
in-process server (sent after a pose, again after 2 km or 200 m of height, re-sent on hello).
"""
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from decimal import Decimal, getcontext
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import geodesy  # noqa: E402
import gravity as G  # noqa: E402
import test_server  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "egm2008_n36.npz"
MGAL = 1e-5
REAL = os.environ.get("BNGE_GRAVITY_REAL") == "1"

# GeographicLib GeoidEval (egm2008-1 grid of NGA's EGM2008 geoid), queried 2026-10-10
PUBLISHED_GEOID = [
    (16.77583, -3.00944, 28.7881),     # Timbuktu (the GeographicLib documentation example)
    (39.2904, -76.6122, -33.6040),     # Baltimore
    (39.7392, -104.9903, -17.2770),    # Denver
    (-0.1807, -78.4678, 26.7631),      # Quito
    (36.5785, -118.2923, -25.1726),    # Mt Whitney
    (4.7, 78.8, -106.8991),            # Indian Ocean geoid low
    (60.0, -85.0, -45.2072),           # Hudson Bay
    (90.0, 0.0, 14.8980),              # North Pole
    (27.9881, 86.925, -28.4939),       # Everest
    (-60.0, -150.0, -32.0459),         # Southern Ocean
]


def normal_model(nmax=20):
    """The WGS84 normal field written as a 'gravity model': its disturbing potential is zero."""
    c = np.zeros(G.tri_len(nmax))
    nz = G.normal_zonal_coefficients(min(20, nmax))
    c[np.arange(len(nz)) * (np.arange(len(nz)) + 1) // 2] = nz
    return G.GravityModel(c, np.zeros_like(c), gm=G.GM, a=G.A, height_offset=0.0)


def legendre_decimal(n, m, t, digits=40):
    """Fully normalised P_nm(t) (geodesy normalisation) in Decimal: no under/overflow at all."""
    getcontext().prec = digits
    t = Decimal(t)
    u = (1 - t * t).sqrt()
    D = Decimal
    p = D(1)
    for k in range(1, m + 1):
        p = p * u * ((D(3) if k == 1 else D(2 * k + 1) / D(2 * k))).sqrt()
    if n == m:
        return p
    p2, p1 = p, D(2 * m + 3).sqrt() * t * p
    for j in range(m + 2, n + 1):
        a = (D((2 * j - 1) * (2 * j + 1)) / D((j - m) * (j + m))).sqrt()
        b = (D((2 * j + 1) * (j + m - 1) * (j - m - 1)) / D((j - m) * (j + m) * (2 * j - 3))).sqrt()
        p2, p1 = p1, a * t * p1 - b * p2
    return p1


class NormalGravityTest(unittest.TestCase):
    def test_published_constants(self):
        self.assertEqual(float(G.normal_gravity(0.0)), 9.7803253359)
        self.assertAlmostEqual(float(G.normal_gravity(90.0)), 9.8321849378, places=10)
        self.assertAlmostEqual(float(G.normal_gravity(-90.0)), 9.8321849378, places=10)
        # TR8350.2 Table 3.4 (k from the rounded gamma_e, gamma_p: 6e-12 off, i.e. 6e-11 m/s^2)
        self.assertAlmostEqual(G.K_SOMIGLIANA, 0.00193185265241, delta=1e-11)
        self.assertAlmostEqual(G.M_RATIO, 0.00344978650684, places=13)
        # the closed form, from the four defining constants alone, reproduces both
        self.assertAlmostEqual(G.normal_gravity_exact(0.0), 9.7803253359, places=9)
        self.assertAlmostEqual(G.normal_gravity_exact(90.0), 9.8321849378, places=9)
        # J2 / C20 of WGS84 (TR8350.2 Table 3.3: C20 = -0.484166774985e-3)
        self.assertAlmostEqual(G.normal_zonal_coefficients()[2], -0.484166774985e-3, places=14)

    def test_somigliana_equals_closed_form_on_ellipsoid(self):
        for lat in np.linspace(-90, 90, 37):
            self.assertAlmostEqual(float(G.normal_gravity(lat)), G.normal_gravity_exact(lat), places=9)

    def test_height_series(self):
        # TR8350.2 eq. 4-3 is first order in f and m: about 1.1e-10 m/s^2 per metre of height
        # (0.05 mGal at 4.4 km, 0.11 mGal at 10 km) against the exact closed form
        for lat in (0, 23.4, 45, 66.5, 90):
            for h in (-430, 1000, 4421, 10000):
                self.assertAlmostEqual(float(G.normal_gravity(lat, h)), G.normal_gravity_exact(lat, h),
                                       delta=1.2e-10 * abs(h) + 1e-9, msg=(lat, h))
        np.testing.assert_allclose(G.normal_gravity([0, 45, 90], [0, 1000, 2000]),
                                   [G.normal_gravity_exact(0, 0), G.normal_gravity_exact(45, 1000),
                                    G.normal_gravity_exact(90, 2000)], atol=1e-7)

    def test_free_air_gradient(self):
        self.assertAlmostEqual(float(G.free_air_gradient(45)), -3.086e-6, delta=1e-9)
        self.assertAlmostEqual(float(G.free_air_gradient(0)), -3.0877e-6, delta=1e-9)
        self.assertAlmostEqual(float(G.free_air_gradient(90)), -3.0832e-6, delta=1e-9)
        for lat in (0, 45, 90):   # the closed form's own vertical derivative
            fd = (G.normal_gravity_exact(lat, 1.0) - G.normal_gravity_exact(lat, -1.0)) / 2
            self.assertAlmostEqual(float(G.free_air_gradient(lat)), fd, delta=1.5e-10)  # O(f^2) terms

    def test_normal_vector(self):
        n0, u0 = G.normal_gravity_exact(45, 0.0, vector=True)
        self.assertAlmostEqual(n0, 0.0, delta=1e-12)               # on the level ellipsoid
        self.assertAlmostEqual(-u0, G.normal_gravity_exact(45, 0.0), places=12)
        n10, _ = G.normal_gravity_exact(45, 10000.0, vector=True)
        # curvature of the normal plumb line: 0.17" per km * sin(2 lat), towards the equator
        self.assertAlmostEqual(n10 / 9.775 * 206265, -0.17 * 10, delta=0.03)


class EngineTest(unittest.TestCase):
    """The spherical-harmonic synthesis, without real data."""

    @classmethod
    def setUpClass(cls):
        cls.normal = normal_model()
        cls.fix = G.GravityModel.from_npz(FIXTURE, dtype=np.float64)   # for finite differences

    def test_normal_field_as_a_model(self):
        """Zonal series J2..J20 + centrifugal == the exact closed form, magnitude and direction."""
        for lat in (-89.9, -60, -30, 0, 15, 45, 75, 90):
            for h in (0.0, 4400.0, 10000.0):
                f = self.normal.field(lat, 33.0, h)
                self.assertAlmostEqual(f.g, G.normal_gravity_exact(lat, h), delta=2e-9, msg=(lat, h))
                self.assertAlmostEqual(f.disturbance, 0.0, delta=2e-9)
                self.assertAlmostEqual(f.anomaly, 0.0, delta=1e-12)
                north, up = G.normal_gravity_exact(lat, h, vector=True)
                self.assertAlmostEqual(f.north, north, delta=2e-9, msg=(lat, h))
                self.assertAlmostEqual(f.up, up, delta=2e-9)
                self.assertAlmostEqual(f.east, 0.0, delta=1e-12)
                self.assertAlmostEqual(f.geoid, 0.0, delta=1e-6)

    def test_legendre_scaling_beyond_underflow(self):
        """P_2190,760 at 68.4 deg: P_mm ~ 1e-329 underflows a plain float64 recursion, while the
        value itself is O(0.1). The per-order log scale must reproduce it."""
        n, m, N = 2190, 760, 2190
        c = np.zeros(G.tri_len(N))
        nz = G.normal_zonal_coefficients()
        c[np.arange(21) * np.arange(1, 22) // 2] = nz
        c[n * (n + 1) // 2 + m] = 1.0
        model = G.GravityModel(c, np.zeros_like(c), gm=G.GM, a=G.A)
        for lat_c in (68.38, 30.0, 85.0):
            t = math.sin(math.radians(lat_c))
            out, _ = model._row_sums(t, math.cos(math.radians(lat_c)), 0.0)
            want = float(legendre_decimal(n, m, repr(t)))
            got = float(out[n, 0, 0])
            self.assertAlmostEqual(got, want, delta=1e-6 * max(abs(want), 1e-30), msg=lat_c)
            if lat_c == 68.38:
                self.assertGreater(abs(want), 1e-3)            # significant, not an underflow 0
        # every other degree sees nothing
        self.assertEqual(float(np.abs(out[:n, 0, 0]).max()), 0.0)

    def test_gradient_matches_finite_differences(self):
        """dT/dr, dT/dphi, dT/dlam (incl. the shifted-order derivative columns) against T."""
        for r_off, latc, lon in ((0.0, 37.0, -118.0), (3000.0, -61.0, 20.0), (0.0, 89.95, 45.0),
                                 (10000.0, 0.0, 179.9)):
            lat0, lon0, _ = geodesy.ecef_to_geodetic(*geodesy.geodetic_to_ecef(latc, lon, 0.0))
            x, y, z = geodesy.geodetic_to_ecef(latc, lon, r_off)
            r0 = math.sqrt(x * x + y * y + z * z)
            phic0 = math.atan2(z, math.hypot(x, y))
            lam0 = math.atan2(y, x)

            def T(r, phic, lam):
                p = np.array([r * math.cos(phic) * math.cos(lam), r * math.cos(phic) * math.sin(lam),
                              r * math.sin(phic)])
                la, lo, hh = geodesy.ecef_to_geodetic(*p)
                return self.fix.disturbing_potential(float(la), float(lo), float(hh))

            d = T(r0, phic0, lam0)
            hr, ha = 1.0, 1e-6
            fd_r = (T(r0 + hr, phic0, lam0)["T"] - T(r0 - hr, phic0, lam0)["T"]) / (2 * hr)
            fd_p = (T(r0, phic0 + ha, lam0)["T"] - T(r0, phic0 - ha, lam0)["T"]) / (2 * ha)
            fd_l = (T(r0, phic0, lam0 + ha)["T"] - T(r0, phic0, lam0 - ha)["T"]) / (2 * ha)
            scale = abs(d["T"]) + 1.0
            self.assertAlmostEqual(d["dT_dr"], fd_r, delta=1e-6 * scale / 1e3, msg="dr")
            self.assertAlmostEqual(d["dT_dphi"], fd_p, delta=1e-5 * scale, msg="dphi")
            self.assertAlmostEqual(d["dT_dlam"], fd_l, delta=1e-5 * scale, msg="dlam")

    def test_pole_is_continuous(self):
        a, b = self.fix.field(90.0, 0.0, 0.0), self.fix.field(89.9999, 0.0, 0.0)
        self.assertAlmostEqual(a.g, b.g, delta=1e-7)
        self.assertAlmostEqual(a.geoid, b.geoid, delta=1e-3)
        c = self.fix.field(90.0, 123.0, 0.0)           # longitude is meaningless on the pole
        self.assertAlmostEqual(a.g, c.g, delta=1e-7)
        self.assertAlmostEqual(math.hypot(a.east, a.north), math.hypot(c.east, c.north), delta=1e-7)

    def test_msl_heights(self):
        f = self.fix.field(39.29, -76.61, 10.0, msl=True)
        self.assertAlmostEqual(f.h, 10.0 + f.geoid, places=9)
        g = self.fix.field(39.29, -76.61, f.h)
        self.assertAlmostEqual(f.g, g.g, delta=1e-8)   # same point, given either way
        self.assertAlmostEqual(f.geoid, g.geoid, delta=1e-3)


class FixtureFieldTest(unittest.TestCase):
    """EGM2008 to degree 36: the long wavelengths of the real field."""

    @classmethod
    def setUpClass(cls):
        cls.m = G.GravityModel.from_npz(FIXTURE)

    def test_geoid_long_wavelengths(self):
        for lat, lon, want in PUBLISHED_GEOID:
            if lat in (27.9881, -0.1807, 36.5785):   # mountains: short wavelengths dominate
                continue
            self.assertAlmostEqual(self.m.field(lat, lon, 0.0).geoid, want, delta=3.0, msg=(lat, lon))

    def test_regional_anomalies(self):
        # Hudson Bay (post-glacial rebound) and the Indian Ocean geoid low: negative
        self.assertTrue(-60 < self.m.field(60.0, -85.0, 0.0).anomaly / MGAL < -25)
        self.assertTrue(-70 < self.m.field(4.7, 78.8, 0.0).anomaly / MGAL < -30)
        # the central Andes stand out positive even at degree 36
        self.assertGreater(self.m.field(-15.0, -70.0, 4000.0, msl=True).anomaly / MGAL, 30)

    def test_global_ranges(self):
        rng = np.random.default_rng(7)
        for _ in range(60):
            lat = math.degrees(math.asin(rng.uniform(-1, 1)))
            f = self.m.field(lat, rng.uniform(-180, 180), rng.uniform(0, 3000), msl=True)
            self.assertTrue(9.76 < f.g < 9.84, f)
            self.assertLess(abs(f.disturbance), 120 * MGAL)
            self.assertTrue(-110 < f.geoid < 90)
            self.assertAlmostEqual(f.g, f.normal + f.disturbance, places=12)

    def test_float32_sums(self):
        m64 = G.GravityModel.from_npz(FIXTURE, dtype=np.float64)
        for lat, lon, h in ((0, 0, 0), (36.6, -118.3, 4400), (-89.5, 10, 0), (60, -85, 10000)):
            a, b = self.m.field(lat, lon, h), m64.field(lat, lon, h)
            self.assertAlmostEqual(a.g, b.g, delta=1e-8)                # 0.001 mGal
            self.assertAlmostEqual(a.north, b.north, delta=1e-8)
            self.assertAlmostEqual(a.geoid, b.geoid, delta=1e-5)

    def test_cli(self):
        out = subprocess.run([sys.executable, str(Path(G.__file__)), "38.5", "-98", "10000", "--msl",
                              "--coefficients", str(FIXTURE)], capture_output=True, text=True, check=True)
        self.assertIn("gravity      9.76", out.stdout)
        self.assertIn("10000.00 m MSL", out.stdout)
        self.assertIn("anomaly", out.stdout)

    def test_coefficient_file_layout(self):
        """read_cof's column-major -> row-major conversion on a tiny synthetic .cof."""
        import io
        import struct
        N, M = 3, 2
        cvals = {(n, m): 10 * n + m for m in range(M + 1) for n in range(m, N + 1)}
        svals = {(n, m): -(10 * n + m) for m in range(1, M + 1) for n in range(m, N + 1)}
        buf = io.BytesIO()
        buf.write(b"TESTCOF1")
        buf.write(struct.pack("<ii", N, M))
        buf.write(np.array([cvals[n, m] for m in range(M + 1) for n in range(m, N + 1)], "<f8").tobytes())
        buf.write(np.array([svals[n, m] for m in range(1, M + 1) for n in range(m, N + 1)], "<f8").tobytes())
        buf.write(struct.pack("<ii", -1, -1))
        buf.seek(0)
        d = G.read_cof(buf)
        for n in range(N + 1):
            for m in range(n + 1):
                i = n * (n + 1) // 2 + m
                self.assertEqual(d["c"][i], cvals.get((n, m), 0.0))
                self.assertEqual(d["s"][i], svals.get((n, m), 0.0))


@unittest.skipUnless(REAL, "set BNGE_GRAVITY_REAL=1 for the full EGM2008 checks (74 MB download once)")
class RealEgm2008Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = G.load_egm2008(2190)

    def test_everest_geographiclib(self):
        """GeographicLib Gravity(1) man page: echo 27:59:17N 86:55:32E 8820 | Gravity -n egm2008
        => -0.00001 0.00103 -9.76782 (east, north, up in m/s^2, printed to 5 decimals)."""
        f = self.m.field(27 + 59 / 60 + 17 / 3600, 86 + 55 / 60 + 32 / 3600, 8820.0)
        self.assertAlmostEqual(f.east, -0.00001, delta=5.5e-6)
        self.assertAlmostEqual(f.north, 0.00103, delta=5.5e-6)
        self.assertAlmostEqual(f.up, -9.76782, delta=5.5e-6)

    def test_geoid_published(self):
        for lat, lon, want in PUBLISHED_GEOID:
            self.assertAlmostEqual(self.m.field(lat, lon, 0.0).geoid, want, delta=0.003, msg=(lat, lon))

    def test_anomaly_sanity(self):
        def anom(lat, lon, H):
            return self.m.field(lat, lon, H, msl=True).anomaly / MGAL
        # high mountains: large positive free-air anomalies on the summits
        for name, lat, lon, H in (("Everest", 27.9881, 86.925, 8849),
                                  ("Aconcagua", -32.6532, -70.0109, 6961),
                                  ("Chimborazo", -1.4692, -78.8175, 6263),
                                  ("Whitney", 36.5785, -118.2923, 4421)):
            self.assertTrue(100 < anom(lat, lon, H) < 400, name)
        # Hudson Bay: -30..-60; the Indian Ocean geoid low; the deep trenches
        self.assertTrue(-60 < anom(60.0, -85.0, 0) < -30)
        self.assertTrue(-130 < anom(4.7, 78.8, 0) < -30)
        self.assertLess(anom(19.7, -66.5, 0), -200)              # Puerto Rico Trench
        # flat interior lowlands: small
        self.assertLess(abs(anom(38.5, -98.0, 450)), 30)         # Kansas
        self.assertLess(abs(anom(39.2904, -76.6122, 10)), 30)    # Baltimore

    def test_global_ranges_and_speed(self):
        rng = np.random.default_rng(11)
        times = []
        for _ in range(40):
            lat = math.degrees(math.asin(rng.uniform(-1, 1)))
            t0 = time.perf_counter()
            f = self.m.field(lat, rng.uniform(-180, 180), rng.uniform(0, 4000), msl=True)
            times.append(time.perf_counter() - t0)
            self.assertTrue(9.75 < f.g < 9.85, f)
            self.assertLess(abs(f.anomaly), 700 * MGAL, f)
            self.assertLess(max(abs(f.xi), abs(f.eta)), 120, f)
            self.assertTrue(-110 < f.geoid < 90, f)
        print(f"\n  EGM2008/2190 point: median {1e3 * sorted(times)[20]:.1f} ms", file=sys.stderr)
        self.assertLess(sorted(times)[20], 0.2)    # 45 ms on the dev machine; loose for CI


# ================================================================================================
# earthd: Provider, Tracker, the 'gravity' message
# ================================================================================================

DEG_M = 2 * math.pi * 6371008.8 / 360         # metres per degree of latitude in G.distance_m


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.model = G.GravityModel.from_npz(FIXTURE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_model_message(self):
        p = G.Provider(self.tmp.name, model=self.model, download=False)
        self.assertEqual(p.source(), "egm2008")
        m = p.message(36.5785, -118.2923, 4400.0)
        self.assertEqual(set(m), {"type", "v", "g", "lat", "lon", "h", "source"})
        self.assertEqual((m["type"], m["v"], m["source"], m["h"]), ("gravity", G.MSG_VERSION, "egm2008", 4400.0))
        self.assertAlmostEqual(m["g"], self.model.field(36.5785, -118.2923, 4400.0, msl=True).g, places=6)

    def test_normal_without_coefficients(self):
        p = G.Provider(self.tmp.name, download=False)
        self.assertEqual(p.source(), "normal")
        m = p.message(45.0, 7.0, 1000.0)
        self.assertEqual(m["source"], "normal")
        self.assertAlmostEqual(m["g"], float(G.normal_gravity(45.0, 1000.0)), places=6)
        self.assertIsNone(p.fetcher)                    # download=False: no thread

    def test_background_download_never_blocks(self):
        release, calls = threading.Event(), []

        def fake_ensure(cache_dir, log=print, net=None):    # the 74 MB download, held until released
            calls.append(cache_dir)
            release.wait(10)
            for n in ("egm2008_rowmajor.npy", "egm2008_rowmajor.json"):
                open(os.path.join(cache_dir, n), "w").close()
            return os.path.join(cache_dir, "egm2008_rowmajor.npy")

        with mock.patch.object(G, "ensure_egm2008", fake_ensure), \
                mock.patch.object(G, "load_egm2008", lambda nmax, cache_dir, log: self.model):
            p = G.Provider(self.tmp.name)
            t0 = time.monotonic()
            first = p.message(36.5785, -118.2923, 4400.0)
            second = p.message(36.5785, -118.2923, 4400.0)
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertEqual((first["source"], second["source"]), ("normal", "normal"))
            fetcher = p.fetcher
            self.assertIsNotNone(fetcher)
            release.set()
            fetcher.join(10)
            self.assertEqual(calls, [self.tmp.name])    # one download for both
            self.assertEqual(p.source(), "egm2008")
            m = p.message(36.5785, -118.2923, 4400.0)
            self.assertEqual(m["source"], "egm2008")
            self.assertAlmostEqual(m["g"], self.model.field(36.5785, -118.2923, 4400.0, msl=True).g, places=6)

    def test_failed_download_retried_later(self):
        calls = []

        def failing(cache_dir, log=print, net=None):
            calls.append(1)
            raise OSError("no network in tests")

        with mock.patch.object(G, "ensure_egm2008", failing):
            p = G.Provider(self.tmp.name, log=mock.Mock())
            self.assertEqual(p.message(0.0, 0.0, 0.0)["source"], "normal")
            p.fetcher.join(10)
            self.assertIsNotNone(p.failed_at)
            p.message(0.0, 0.0, 0.0)
            self.assertIsNone(p.fetcher)                # within DOWNLOAD_RETRY_S: no new attempt
            p.failed_at -= G.DOWNLOAD_RETRY_S + 1
            p.message(0.0, 0.0, 0.0)
            p.fetcher.join(10)
            self.assertEqual(len(calls), 2)

    def test_recompute_rule(self):
        p, t = G.Provider(self.tmp.name, model=self.model, download=False), G.Tracker()
        self.assertTrue(t.due(p, 36.5, -118.3, 100.0))            # nothing sent yet
        t.last = (36.5, -118.3, 100.0, "egm2008")
        self.assertFalse(t.due(p, 36.5 + 1900 / DEG_M, -118.3, 299.0))
        self.assertFalse(t.due(p, 36.5, -118.3, -99.0))
        self.assertTrue(t.due(p, 36.5 + 2100 / DEG_M, -118.3, 100.0))
        self.assertTrue(t.due(p, 36.5, -118.3, 301.0))
        self.assertTrue(t.due(p, 36.5, -118.3, -101.0))
        t.last = (36.5, -118.3, 100.0, "normal")
        self.assertTrue(t.due(p, 36.5, -118.3, 100.0))            # EGM2008 became available
        t.inflight = True
        self.assertFalse(t.due(p, 40.0, -118.3, 100.0))


class ServerGravityTest(test_server.ServerHarness):
    """earthd with a gravity provider on the fixture model: the message after the first pose on an
    anchor, recomputed after > 2 km or > 200 m of height only, re-sent on hello; none when off."""

    def pose_at(self, aid, x, y, z):
        self.c.send({"type": "pose", "anchorId": aid, "x": x, "y": y, "z": z, "lat": test_server.LAT,
                     "lon": test_server.LON, "speed": 0, "vx": 0, "vy": 0})

    def gravity_msgs(self, wait=0.6):
        return [m for m in self.c.drain(wait) if m["type"] == "gravity"]

    def start(self):
        self.hello()
        aid = self.prepare_anchor(1, test_server.LAT, test_server.LON)["anchorId"]
        self.c.send({"type": "commit_anchor", "anchorId": aid})
        return aid, self.server.anchors[aid].frame

    def test_sent_recomputed_and_resent(self):
        prov = G.Provider(self.tmp.name, model=G.GravityModel.from_npz(FIXTURE), download=False)
        self.server.gravity = prov
        aid, frame = self.start()
        self.pose_at(aid, 0, 0, 1500)
        m, _ = self.c.until(lambda m: m["type"] == "gravity", 10)
        self.assertEqual(m, prov.message(*(float(v) for v in frame.enu_to_geodetic(0, 0, 1500))))
        self.pose_at(aid, 1500, 0, 1650)                         # 1.5 km, 150 m: not due
        self.assertFalse(self.gravity_msgs())
        self.pose_at(aid, 2500, 0, 1500)                         # 2.5 km
        moved, _ = self.c.until(lambda m: m["type"] == "gravity", 10)
        self.assertNotEqual(moved["lon"], m["lon"])
        self.pose_at(aid, 2500, 0, 1750)                         # 250 m higher
        climbed, _ = self.c.until(lambda m: m["type"] == "gravity", 10)
        self.assertLess(climbed["g"], moved["g"])
        self.hello()
        again, _ = self.c.until(lambda m: m["type"] == "gravity", 10)
        self.assertEqual(again, climbed)

    def test_off(self):
        aid, _ = self.start()
        self.pose_at(aid, 0, 0, 1500)
        self.assertFalse(self.gravity_msgs(1.0))
        self.assertIsNone(self.conn().gravity.msg)


class DownloadTlsTest(unittest.TestCase):
    """GitHub #567: "certificate has expired" on the EGM2008 download on Windows. Its own urllib connections did not take
    the system's certificate check the other requests use (netfetch.windows_ssl_context): now every attempt does."""

    def test_every_attempt_uses_the_platform_tls_context(self):
        import io
        import hashlib
        import gravity
        import netfetch
        body = b"egm" * 1000
        seen = []

        class Resp(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None, context=None):
            seen.append(context)
            if len(seen) == 1:
                raise OSError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired")
            return Resp(body)

        ctx = object()
        net = netfetch.Fetcher(transport=object(), sleep=lambda s: None)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(netfetch, "windows_ssl_context", return_value=ctx), \
                mock.patch.object(gravity.urllib.request, "urlopen", urlopen), \
                mock.patch.object(gravity.time, "sleep", lambda s: None):
            dest = os.path.join(d, "egm.tar.bz2")
            gravity._download("https://downloads.sourceforge.net/x", dest, hashlib.sha256(body).hexdigest(), len(body),
                              log=lambda m: None, net=net)
            with open(dest, "rb") as f:
                self.assertEqual(f.read(), body)
        self.assertEqual(seen, [ctx, ctx], "the retry too")


if __name__ == "__main__":
    unittest.main()
