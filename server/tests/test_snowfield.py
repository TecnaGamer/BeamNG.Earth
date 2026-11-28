"""Local weather S1 (PLAN_FINAL S-1 to S-4): the snow field's kernels (earthfast snowobs.rs, snowfield.rs) and the photos'
S1 tables (snowlook.rs). The snow rule (the user, 2026-11-27): snow only where real data says it snowed or lies."""
import os
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast  # noqa: E402

NATIVE = 655
NF = 14
MISSING = -2 ** 31
F = {"t": 0, "td": 1, "p": 3, "swe": 4, "wind": 5, "sw": 6, "ts": 7, "depth": 9, "rh": 10, "hour": 11, "month": 12,
     "cloud": 13}
# a golden hash of snowfield.rs selftest(1, 24): the same number on Linux and Windows (the kernel is integer-only)
SELFTEST = 16759692160481620274     # (Linux release build, 2026-11-27)


def window(spec, depth_mm=0, month=1):
    """Hourly records [n, NF]: spec = [(hours, t 0.01 degC, precip 0.01 mm/h, snow share permille)]."""
    out = []
    for n, t, p, share in spec:
        for _ in range(n):
            r = [MISSING] * NF
            r[F["t"]], r[F["td"]], r[F["p"]] = t, t - 200, p
            r[F["swe"]] = p * share // 1000 * 7 // 10
            r[F["wind"]], r[F["sw"]], r[F["ts"]], r[F["rh"]] = 20, 150, t, 70
            r[F["hour"]], r[F["month"]], r[F["cloud"]] = 12, month, 30
            r[F["depth"]] = depth_mm if not out else 0
            out.append(r)
    return np.array(out, np.int32)


def need():
    fast.require(NATIVE, "the snow field (S1)")
    return fast._ef


class TableTest(unittest.TestCase):
    def test_mild_winter_no_snowfall_no_snow(self):
        """Atlanta-like: ten dry January days at +6 degC, the maps bare: no row with snow."""
        ef = need()
        f = window([(246, 600, 0, 0)])
        bare = np.array([[20, 0, 20, 0]] * 5, np.int64)
        k, rows, _st, _runs = ef.snowfield_table(f, 239, 100, 3375, 50, 350, [(100, 0)], bare, -1, bare, -1)
        self.assertEqual(k, -1)
        self.assertTrue(all(r[1] == 0 and r[3] == 0 and r[5] == 0 for r in rows), rows)

    def test_rows_on_the_absolute_grid(self):
        ef = need()
        self.assertEqual(ef.snowfield_rows(1820, 4300), (11, 20))
        k, rows, _st, _runs = ef.snowfield_table(window([(246, -500, 0, 0)]), 239, 2000, 3900, 1820, 4300,
                                                 [(2000, 0)])
        self.assertEqual([r[0] for r in rows], list(range(1650, 4501, 150)))

    def test_dry_spell_keeps_observed_snow_on_the_peaks(self):
        """No snowfall in the window and no model snow, the maps white from 2,700 m: thin observed cover there."""
        ef = need()
        f = window([(246, 300, 0, 0)])
        k0, n = ef.snowfield_rows(1200, 4000)
        z = (k0 + np.arange(n)) * 150
        obs = np.array([[0, 6, 0, 6] if h >= 2700 else [6, 0, 6, 0] for h in z], np.int64)
        k, rows, _st, runs = ef.snowfield_table(f, 239, 1500, 4600, 1200, 4000, [(1500, 0)], obs, k0, obs, k0)
        self.assertTrue(all(r[:3] == [0, 0, 0] for r in runs))
        # (the maps' rows read 150 m higher: snowfield.rs OBS_SHIFT_ROWS, P-A's bias correction)
        self.assertEqual(ef.snowfield_line(k, rows, 0)[0], 2775)
        self.assertTrue(all((r[2] > 0) == (r[0] >= 2850) for r in rows), rows)
        k, rows, _st, _ = ef.snowfield_table(f, 239, 1500, 4600, 1200, 4000, [(1500, 0)], obs, k0, obs, k0,
                                             fusion=(499, 750, False, 500, 0, 2000))
        self.assertEqual(ef.snowfield_line(k, rows, 0)[0], 2625)

    def test_bins_settle(self):
        """A one-bin move shows only when it held two evaluations (S-7); the states round-trip."""
        ef = need()
        f = window([(240, -500, 0, 0), (6, -500, 40, 1000)], depth_mm=30)
        k, rows, st, _ = ef.snowfield_table(f, 245, 1000, 4600, 900, 1200, [(1000, 30)])
        self.assertTrue(all(len(s) == 10 for s in st))
        k2, rows2, st2, _ = ef.snowfield_table(f, 245, 1000, 4600, 900, 1200, [(1000, 30)], prev=st)
        self.assertEqual(rows, rows2)

    def test_selftest_deterministic(self):
        ef = need()
        a = ef.snowfield_selftest(1, 24)
        self.assertEqual(a, ef.snowfield_selftest(1, 24))
        if SELFTEST is not None:
            self.assertEqual(a, SELFTEST)


class ObsTest(unittest.TestCase):
    def test_projection_and_snowline(self):
        ef = need()
        r, c = ef.snowobs_ims_pixels(np.array([90.0, -10.0, 45.0]), np.array([0.0, 0.0, -80.0]))
        self.assertEqual((int(r[0]), int(c[0])), (12288, 12288))
        self.assertEqual(int(r[1]), -1)
        self.assertEqual(int(c[2]), 12288)          # 80 W runs straight down from the pole
        counts = np.array([[5, 0, 5, 0], [5, 0, 5, 0], [0, 5, 0, 5]], np.int64)
        self.assertEqual(ef.snowobs_snowline(counts, 10, 0), (1725, 0, 15))

    def test_classes(self):
        ef = need()
        self.assertEqual(list(ef.snowobs_classes(np.array([0, 1, 2, 3, 4], np.uint8), "ims")), [0, 0, 1, 0, 2])
        self.assertEqual(list(ef.snowobs_classes(np.array([0, 34, 35, 100, 250, 239], np.uint8), "ndsi")),
                         [1, 1, 2, 2, 0, 0])


class LookTest(unittest.TestCase):
    """The photos read an S1 table by the texel's aspect (the kernel's pole / equator runs) and never beyond a table's
    own relief when another table reaches the height."""

    def look(self, u, tables, idx, ci0=0, cj0=0, lat=40.0, lon=-89.0):
        ef = need()
        import snowcover
        n, g = u.shape[0], 9
        geo = (np.full((g, g), lat), np.full((g, g), lon), np.zeros((g, g)), np.full((g, g), 1e9), np.zeros((g, g)))
        t = np.zeros((len(tables), max(len(x) for x in tables), 6), np.int32)
        counts = np.zeros((len(tables), 17), np.int32)
        for i, rows in enumerate(tables):
            t[i, :len(rows)] = rows
            counts[i, 0] = len(rows)
        field = (0.25, ci0, cj0, np.array(idx, np.int32), t, counts)
        size = 1000.0
        return ef.SnowLook((-500.0, -500.0, size), (lat, lon), u.astype(np.float64), (-500.0, -500.0, size / (n - 1)),
                           geo, field, snowcover.PARAMS)

    def test_aspect_from_the_table(self):
        # 150-300 m: white facing the pole, bare facing the equator
        rows = [(0, 0, 0, 0, 0, 0), (150, 8, 3, 0, 0, 0), (300, 8, 3, 0, 0, 0), (450, 8, 3, 8, 3, 0)]
        n = 65
        v = np.linspace(100, 350, n)
        north = np.tile(v[:, None], (1, n))            # rising north: facing the equator (south)
        south = north[::-1].copy()                     # falling north: facing the pole
        ci, cj = int((-89.0 + 180) / 0.25), int((90 - 40.0) / 0.25)
        a = self.look(north, [rows], [[0]], ci, cj)
        b = self.look(south, [rows], [[0]], ci, cj)
        ys = np.array([0.0])
        xs = np.zeros(1)
        # the same height (225 m) on each slope: the middle of each grid
        ca = int(a.sample(xs, ys)[0][0])
        cb = int(b.sample(xs, ys)[0][0])
        self.assertGreater(cb, 200, "white facing the pole")
        self.assertLess(ca, 120, "barer facing the equator")

    def test_no_table_beyond_its_relief(self):
        # two cells side by side: a lowland one (rows to 300 m, bare) and a mountain one (white above 1,000 m); a texel
        # at 2,000 m on their border takes the mountain's table only (the lowland has no ground there)
        low = [(0, 0, 0, 0, 0, 0), (150, 0, 0, 0, 0, 0), (300, 0, 0, 0, 0, 0)]
        mtn = [(900, 0, 0, 0, 0, 0), (1050, 8, 4, 8, 4, 0), (3000, 8, 4, 8, 4, 0)]
        lat, lon = 40.0, -89.0
        ci, cj = int((lon + 180) / 0.25), int((90 - lat) / 0.25)
        # the texel on the cells' border: lon at the boundary between ci and ci + 1
        lon_b = (ci + 1) * 0.25 - 180.0
        u = np.full((9, 9), 2000.0)
        lk = self.look(u, [low, mtn], [[0, 1]], ci, cj, lat=(90 - (cj + 0.5) * 0.25), lon=lon_b)
        c = int(lk.sample(np.zeros(1), np.zeros(1))[0][0])
        self.assertGreater(c, 240, c)


class CoverTest(unittest.TestCase):
    """snowcover carries S1 tables to the look with their own key; W2's keys stay what they were."""

    def test_s1_field_through_snowcover(self):
        need()
        import importlib
        import geodesy
        import snowcover
        old = os.environ.get("BNE_WINTER")
        os.environ["BNE_WINTER"] = "on"
        try:
            importlib.reload(snowcover)
            sc = snowcover
            lat, lon = 39.875, -88.875          # (a cell's centre: its neighbours have no table)
            ci, cj = int((lon + 180) / 0.25), int((90 - lat) / 0.25)
            rows = ((0, 0, 0, 0, 0, 0), (150, 8, 3, 0, 0, 0), (300, 8, 3, 0, 0, 0), (450, 8, 3, 8, 3, 0))
            sc.set_provider(lambda s, w, n, e: sc.Field(0.25, cells={(ci, cj): sc.Table(rows, sc.NO_ROAD, 0)}))
            frame = geodesy.EnuFrame(lat, lon)
            sp = sc.spec_for_square(frame, -500, -500, 1000)
            self.assertIsNotNone(sp)
            self.assertEqual(sp.field[4].shape, (1, 4, 6))
            v = np.linspace(100, 350, 65)
            facing_eq = np.tile(v[:, None], (1, 65))
            facing_pole = facing_eq[::-1].copy()
            look = lambda u: sc.look(sp, frame, -500, -500, 1000, u, -500, -500, 1000 / 64)
            ce = int(look(facing_eq).sample(np.zeros(1), np.zeros(1))[0][0])
            cp = int(look(facing_pole).sample(np.zeros(1), np.zeros(1))[0][0])
            self.assertGreater(cp, 200)
            self.assertLess(ce, 120)
            # the same cover as a W2 table: another key
            sc.set_provider(lambda s, w, n, e: sc.Field(0.25, cells={(ci, cj): sc.Table(((150, 8, 3, 0),), sc.NO_ROAD,
                                                                                       0)}))
            self.assertNotEqual(sc.spec_for_square(frame, -500, -500, 1000).sig, sp.sig)
        finally:
            snowcover.set_provider(None)
            if old is None:
                os.environ.pop("BNE_WINTER", None)
            else:
                os.environ["BNE_WINTER"] = old
            importlib.reload(snowcover)


class FieldTest(unittest.TestCase):
    """snowfield.SnowField (stage A glue) with a fake DEM, fake Open-Meteo answers and fake IMS windows. update()
    stages, publish() takes effect (snow.Service._publish on the event loop)."""

    T0 = 1768910400             # 2026-01-20 12:00 UTC

    class Dem:
        """A 3,500 m peak at (39.875, -105.125) on a 1,500 m plain."""

        def sample(self, la, lo, zoom):
            la, lo = np.broadcast_arrays(np.asarray(la, float), np.asarray(lo, float))
            d2 = ((la - 39.875) / 0.1) ** 2 + ((lo + 105.125) / 0.1) ** 2
            return (1500.0 + 2000.0 * np.exp(-d2)).astype(np.float32)

    def om(self, n, t, p, depth_m=0.0):
        times = [self.T0 - 3600 * (240 - i) for i in range(247)]
        h = {"time": times, "temperature_2m": [t] * 247, "dew_point_2m": [t - 3] * 247,
             "precipitation": [p] * 247, "snowfall_water_equivalent": [p * 0.7 if t < 0 else 0.0] * 247,
             "wind_speed_10m": [2.0] * 247, "shortwave_radiation": [150.0] * 247, "soil_temperature_0cm": [t] * 247,
             "snow_depth": [depth_m] + [0.0] * 246, "relative_humidity_2m": [70.0] * 247, "cloud_cover": [30.0] * 247}
        return [{"hourly": h, "elevation": 1500.0, "utc_offset_seconds": 0} for _ in range(n)]

    def field(self, t, p, ims_class, depth_m=0.0, cap=None, near_km=30.0, far_km=60.0):
        need()
        import tempfile
        import snowfield
        import urllib.parse
        self.calls = []
        self.now = self.T0

        def fetch_json(url, kind="background"):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            n = len(q["latitude"][0].split(","))
            self.calls.append(n)
            return self.om(n, t, p, depth_m)

        def reader(_body, boxes):
            return [np.full((b[1] - b[0] + 1, b[3] - b[2] + 1), ims_class, np.uint8) for b in boxes]

        d = tempfile.mkdtemp(prefix="snowfield-test-")
        self.addCleanup(__import__("shutil").rmtree, d, True)
        old = (snowfield.NEAR_KM, snowfield.FAR_KM, snowfield.WEIGHT_CAP)
        snowfield.NEAR_KM, snowfield.FAR_KM = near_km, far_km
        if cap is not None:
            snowfield.WEIGHT_CAP = cap
        self.addCleanup(lambda: setattr(snowfield, "NEAR_KM", old[0]) or setattr(snowfield, "FAR_KM", old[1])
                        or setattr(snowfield, "WEIGHT_CAP", old[2]))
        return snowfield, snowfield.SnowField(self.Dem(), fetch_json, lambda url: b"ims", d,
                                              clock=lambda: self.now, ims_reader=reader)

    @staticmethod
    def run_ring(f, lat, lon, ring="near"):
        st = f.update(lat, lon, ring)
        return f.publish(st) if st is not None else []

    def gibs(self, f, viirs_bare=True):
        """GIBS answers: the colour map, a bare clear VIIRS day (NDSI 0 everywhere), no MODIS; IMS's file as given."""
        import io
        from PIL import Image
        cmap = (b'<ColorMaps><ColorMap><Entries><ColorMapEntry rgb="0,255,0" sourceValue="[0]"/>'
                b'<ColorMapEntry rgb="0,191,255" sourceValue="[250]"/></Entries></ColorMap></ColorMaps>')
        im = Image.new("P", (512, 512), 0)
        im.putpalette([0, 255, 0, 0, 191, 255] + [0] * (254 * 3))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        tile = buf.getvalue()
        f.fetch_bytes = lambda url: cmap if url.endswith(".xml") else tile if "VIIRS" in url else \
            None if "MODIS" in url else b"ims"

    def test_rings_nearest_first(self):
        import snowfield
        near, far = snowfield.rings(39.9, -105.1, 30.0, 60.0)
        self.assertEqual(near[0], snowfield.cell_of(39.9, -105.1))
        self.assertTrue(far)
        self.assertFalse(set(near) & {c for v in far.values() for c in v})

    def test_mild_and_bare_is_no_snow(self):
        sf, f = self.field(6.0, 0.0, 2)
        self.assertTrue(f.due(39.9, -105.1))
        changed = self.run_ring(f, 39.9, -105.1, "near") + self.run_ring(f, 39.9, -105.1, "far")
        self.assertIsNone(f.provider(39.0, -106.0, 41.0, -104.0))
        self.assertEqual(changed, [])
        self.assertEqual(len(self.calls), 2)        # (the near ring and the far ring: one request each)
        self.assertFalse(f.due(39.9, -105.1))

    def test_observed_snow_without_snowfall_is_thin_cover(self):
        sf, f = self.field(2.0, 0.0, 4)
        st = f.update(39.9, -105.1)
        # staged: nothing drawn changes until it is published
        self.assertIsNone(f.provider(39.0, -106.0, 41.0, -104.0))
        self.assertTrue(f.publish(st))
        fld = f.provider(39.0, -106.0, 41.0, -104.0)
        self.assertIsNotNone(fld)
        t = next(iter(fld.cells.values()))
        self.assertTrue(all(len(r) == 6 for r in t.rows))
        self.assertTrue(any(r[2] in (1, 2) for r in t.rows))
        # nothing to do again within the hour: no request
        n = len(self.calls)
        self.assertEqual(self.run_ring(f, 39.9, -105.1), [])
        self.assertEqual(len(self.calls), n)

    def test_a_clear_satellite_day_overrules_ims(self):
        """IMS white everywhere, the clear VIIRS day before (GIBS) bare: no snow (the clear-satellite rule)."""
        sf, f = self.field(2.0, 0.0, 4)
        self.gibs(f)
        self.run_ring(f, 39.9, -105.1, "near")
        self.assertIsNone(f.provider(39.0, -106.0, 41.0, -104.0))
        # the far ring reads IMS alone: white there
        self.run_ring(f, 39.9, -105.1, "far")
        fld = f.provider(39.0, -106.0, 41.0, -104.0)
        self.assertIsNotNone(fld)
        self.assertTrue(all(f.cells[k].far for k in fld.cells))

    def test_far_to_near_takes_the_clear_day(self):
        """Codex on 70e2880b: a far cell that comes into the near ring takes the near tier both ways (its ground
        again from the near zoom, the clear day read): white by IMS while far, bare by the clear VIIRS day once near."""
        sf, f = self.field(2.0, 0.0, 4)
        self.gibs(f)
        self.run_ring(f, 39.9, -105.1, "near")
        self.run_ring(f, 39.9, -105.1, "far")
        x = next(k for k, c in f.cells.items() if c.far and c.table is not None)
        far_ground = f.cells[x].ground
        s_, w_, n_, e_ = sf.cell_box(*x)
        lat, lon = 0.5 * (s_ + n_), 0.5 * (w_ + e_)
        self.now += 3600.0          # (the next hour: the near ring due round the new anchor)
        self.run_ring(f, lat, lon, "near")
        self.assertFalse(f.cells[x].far)
        self.assertIsNot(f.cells[x].ground, far_ground)
        self.assertIsNone(f.cells[x].table, "the clear day moved the snowline: bare")
        # and back out to the far ring: far again, IMS's white
        self.now += 7 * 3600.0
        self.run_ring(f, 39.9, -105.1, "near")
        self.run_ring(f, 39.9, -105.1, "far")
        self.assertTrue(f.cells[x].far)
        self.assertIsNotNone(f.cells[x].table)

    def test_bare_two_days_vetoes_model_snow(self):
        sf, f = self.field(-3.0, 0.0, 2, depth_m=0.3)
        self.run_ring(f, 39.9, -105.1, "near")
        self.run_ring(f, 39.9, -105.1, "far")
        self.assertIsNone(f.provider(39.0, -106.0, 41.0, -104.0))

    def test_weight_cap_defers_until_the_day_refills(self):
        sf, f = self.field(2.0, 0.0, 4, cap=1.0)
        self.assertIsNone(f.update(39.9, -105.1, "near"))
        self.assertIsNone(f.update(39.9, -105.1, "far"))
        self.assertEqual(self.calls, [])
        # both held back until the next UTC midnight: not due on every pose meanwhile
        self.assertFalse(f.due(39.9, -105.1))
        self.now = (self.now // 86400 + 1) * 86400 + 60
        self.assertTrue(f.due(39.9, -105.1))

    def stationary_day(self, lat, lon):
        """A day of poses every 10 min at (lat, lon) with the production rings and cap: (the field, requests by group
        (core, rest, far) as location counts, the anchor cell's table times)."""
        import snowfield
        sf, f = self.field(2.0, 0.0, 2, near_km=snowfield.NEAR_KM, far_km=snowfield.FAR_KM)
        near, far = sf.rings(lat, lon, sf.NEAR_KM, sf.FAR_KM)
        n_core, n_rest, n_far = min(sf.CORE_CELLS, len(near)), max(0, len(near) - sf.CORE_CELLS), len(far)
        self.assertEqual(len({n_core, n_rest, n_far}), 3, "the groups' sizes tell their requests apart")
        day0 = (self.T0 // 86400) * 86400
        anchor = sf.cell_of(lat, lon)
        made = []
        for step in range(144):
            self.now = day0 + 30 + step * 600
            for ring in ("near", "far"):
                if f.due(lat, lon):
                    self.run_ring(f, lat, lon, ring)
            c = f.cells.get(anchor)
            if c is not None and (not made or made[-1] != c.made):
                made.append(c.made)
        groups = {"core": [x for x in self.calls if x == n_core], "rest": [x for x in self.calls if x == n_rest],
                  "far": [x for x in self.calls if x == n_far]}
        return sf, f, groups, made, (n_core, n_rest, n_far), day0

    def test_a_stationary_alpine_day_stays_under_the_cap(self):
        """Codex on 70e2880b: at 46.5 N 10.5 E the real rings (48 near cells, ~90 far points) every 10 min for a day:
        under the day's weight cap; the anchor's nearest cells hourly, the rest of the near ring every 3 h, the far ring
        every 6 h; forcing reused within its slot (a move inside the hour asks only for the new points)."""
        sf, f, groups, made, (n_core, n_rest, n_far), day0 = self.stationary_day(46.5, 10.5)
        self.assertLessEqual(f.weight_day[1], sf.WEIGHT_CAP)
        self.assertEqual(len(groups["core"]), 24)
        self.assertEqual(len(groups["rest"]), 8)
        self.assertEqual(len(groups["far"]), 4)
        self.assertEqual(len(made), 24, "the anchor's table made every hour")
        # a move within the hour into the next cell asks only for the new points
        n0 = len(self.calls)
        self.run_ring(f, 46.5, 10.75, "near")
        self.assertLess(sum(self.calls[n0:]), n_core + n_rest, self.calls[n0:])

    def test_a_stationary_day_at_tromso_keeps_the_anchor_hourly(self):
        """Codex on a656188c: at Tromso (69.65 N, 18.96 E) the near ring has 96 cells (2,765 a day hourly): with the
        production cap the anchor and its nearest cells are refreshed every hour all day, the rest of the near ring and
        the far ring as the reserve allows, the day under the cap."""
        sf, f, groups, made, (n_core, n_rest, n_far), day0 = self.stationary_day(69.65, 18.96)
        self.assertGreaterEqual(n_core + n_rest, 90)
        used = f.weight_day[1]
        self.assertLessEqual(used, sf.WEIGHT_CAP, f"{used}")
        self.assertEqual(len(groups["core"]), 24, "the core's request every hour")
        self.assertEqual(len(made), 24, "the anchor's table made every hour")
        self.assertTrue(all(m - day0 < (i + 1) * 3600 for i, m in enumerate(made)), "each in its own hour")
        self.assertGreaterEqual(len(groups["rest"]), 1)
        print(f"  (Tromso: {used:.1f} weighted calls; core {len(groups['core'])} x {n_core}, rest "
              f"{len(groups['rest'])} x {n_rest}, far {len(groups['far'])} x {n_far})")

    def test_a_refresh_replaces_an_expired_snowy_table(self):
        """Codex on a656188c: a kept cell whose table is past TABLE_TTL_S, refreshed in the same update: the fresh
        table is published (it was dropped as expired, then its replacement skipped: snow vanished as it came in)."""
        sf, f = self.field(2.0, 0.0, 4)
        self.run_ring(f, 39.9, -105.1, "near")
        k = sf.cell_of(39.9, -105.1)
        fresh = f.cells[k].table
        self.assertIsNotNone(fresh)
        import snowcover
        old = snowcover.Table(((1500, 8, 6, 8, 6, 3),), snowcover.NO_ROAD, 0)
        f.cells[k].table, f.cells[k].made = old, self.now - sf.TABLE_TTL_S - 60
        self.now += 3600.0
        st = f.update(39.9, -105.1, "near")
        self.assertIn(k, st["gone"])
        self.assertIn(k, st["tables"])
        changed = f.publish(st)
        self.assertIn(k, changed)
        self.assertIn(k, f.cells)
        self.assertEqual(f.cells[k].table, fresh)
        self.assertIn(k, f.provider(39.0, -106.0, 41.0, -104.0).cells)
        # an expired cell that is not refreshed (its forcing failed) goes
        f.cells[k].made = self.now - sf.TABLE_TTL_S - 60
        st = {"ring": "near", "key": None, "tables": {}, "gone": [k]}
        self.assertIn(k, f.publish(st))
        self.assertNotIn(k, f.cells)


class WiringTest(unittest.TestCase):
    """The server side (S-6 / S-7): snow.Service draws the field's tables and runs its rings; Server.snow_changed(cells)
    re-renders only the tiles and photos over the changed cells."""

    def test_snow_changed_by_cells(self):
        need()
        from types import SimpleNamespace
        import earthd
        import farfield
        import geodesy
        import snowfield
        frame = geodesy.EnuFrame(39.875, -105.125)
        here = SimpleNamespace(material="m", texture="t", stale=False, tile=object())
        far_ = SimpleNamespace(material="m", texture="t", stale=False, tile=object())
        where = {id(here.tile): (frame, -1000.0, -1000.0, 2000.0), id(far_.tile): (frame, 200000.0, 0.0, 2000.0)}
        queued = []
        srv = SimpleNamespace(tiles={(1, "a"): here, (1, "b"): far_}, near_visual="detail", anchors={1: SimpleNamespace(
            frame=frame)}, hm={(1, 0.0, 0.0): {}, (1, 300000.0, 0.0): {}}, base_stale=set(),
            layout=SimpleNamespace(block=256))
        srv.tile_where = lambda anchor, tile: where[id(tile)]
        srv.retex_request = lambda ck, pump=True: queued.append(ck)
        srv._retex_pump = lambda: None
        self.assertTrue(hasattr(farfield, "GTile"))
        import snowcover
        cell = snowfield.cell_of(39.875, -105.125)
        rows = ((0, 0, 0, 0, 0, 0), (1500, 8, 3, 8, 3, 0))
        new = snowcover.Field(0.25, cells={cell: snowcover.Table(rows, snowcover.NO_ROAD, 0)})
        # the field's change (none before, a table at the cell after) with the changed cells: the tile and the
        # block over it, not the ones 200-300 km away (the cells' pre-filter, then the look's comparison)
        self.assertEqual(earthd.Server.snow_changed(srv, fields=(None, new), cells=[cell]), 1)
        self.assertEqual((queued, here.stale, far_.stale), ([(1, "a")], True, False))
        self.assertEqual(srv.base_stale, {(1, 0.0, 0.0)})
        # the same fields: no look changed, nothing; no fields (a mode set live): everything
        srv.base_stale.clear()
        self.assertEqual(earthd.Server.snow_changed(srv, fields=(new, new), cells=[cell]), 0)
        self.assertEqual(earthd.Server.snow_changed(srv), 2)

    class FakeField:
        """update stages {"ring": ...}; publish says which cells changed; a ring may raise or wait on an event."""

        def __init__(self, fail=(), gate=None):
            self.calls, self.published, self.fail, self.gate = [], [], set(fail), gate

        def provider(self, s, w, n, e):
            return ("field", len(self.published))

        def due(self, lat, lon):
            return True

        def update(self, lat, lon, ring="near"):
            self.calls.append(ring)
            if self.gate is not None:
                self.gate.wait(5)
            if ring in self.fail:
                raise IOError("a ring failed")
            return {"ring": ring}

        def publish(self, staged):
            self.published.append(staged["ring"])
            return [(1, 2)] if staged["ring"] == "near" else []

    def service(self, field):
        import snow

        class Cover:
            def set_provider(self, fn, climate=False):
                self.provider = fn

        cover = Cover()
        return snow.Service(fetch=lambda *a: {}, mode="real", cover=cover, winter=True, field=field), cover

    def loop_conn(self, snow_changed):
        import asyncio
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        server = SimpleNamespace(snow_changed=snow_changed)
        return loop, SimpleNamespace(loop=loop, s=server)

    def test_service_draws_the_field(self):
        need()
        import snow
        field = self.FakeField()
        svc, cover = self.service(field)
        self.assertEqual(cover.provider(1, 2, 3, 4), ("field", 0))
        # a preset's snow stays the preset's
        self.assertIsNone(snow.Service(fetch=lambda *a: {}, mode="light", cover=type(cover)(), field=field).field)
        changed = []
        loop, conn = self.loop_conn(lambda fields=None, cells=None: changed.append((fields, cells)))
        svc.field_tick(conn, 39.9, -105.1, arrived=True)
        loop.run_until_complete(svc.field_task)
        self.assertEqual(field.calls, ["near", "far"])
        self.assertEqual(field.published, ["near", "far"])
        # the near ring's change: the field before and after its publish, with its cells, in one step
        self.assertEqual(changed, [((("field", 0), ("field", 1)), [(1, 2)])])
        self.assertFalse(svc.field_busy)

    def test_a_disconnect_during_a_ring_loses_no_invalidation(self):
        """Codex on 70e2880b: the ring job is the server's: the player's connection going away while a ring runs
        (earthd cancels its tasks) neither stops the publish nor its renders' invalidation."""
        need()
        import threading
        gate = threading.Event()
        field = self.FakeField(gate=gate)
        svc, _cover = self.service(field)
        changed = []
        loop, conn = self.loop_conn(lambda fields=None, cells=None: changed.append(cells))
        conn_tasks = []
        conn.spawn = lambda coro: conn_tasks.append(loop.create_task(coro))
        svc.field_tick(conn, 39.9, -105.1, arrived=True)
        self.assertEqual(conn_tasks, [], "not the connection's task")
        del conn                    # (the connection gone mid-ring)
        loop.call_later(0.05, gate.set)
        loop.run_until_complete(svc.field_task)
        self.assertEqual(field.published, ["near", "far"])
        self.assertEqual(changed, [[(1, 2)]])

    def test_a_failure_during_a_ring_publishes_nothing_half(self):
        """A ring that fails in the executor publishes nothing (no table changes without its invalidation); the next
        ring still runs; an invalidation that fails falls back to every texture."""
        need()
        field = self.FakeField(fail={"near"})
        svc, _cover = self.service(field)
        changed = []
        loop, conn = self.loop_conn(lambda fields=None, cells=None: changed.append(cells))
        svc.field_tick(conn, 39.9, -105.1, arrived=True)
        loop.run_until_complete(svc.field_task)
        self.assertEqual(field.published, ["far"])
        self.assertEqual(changed, [])

        calls = []

        def flaky(fields=None, cells=None):
            calls.append(cells)
            if cells is not None:
                raise RuntimeError("compare failed")

        field2 = self.FakeField()
        svc2, _ = self.service(field2)
        loop2, conn2 = self.loop_conn(flaky)
        svc2.field_tick(conn2, 39.9, -105.1, arrived=True)
        loop2.run_until_complete(svc2.field_task)
        self.assertEqual(calls, [[(1, 2)], None], "the cells' invalidation failed: every texture")

from types import SimpleNamespace  # noqa: E402


if __name__ == "__main__":
    unittest.main()
