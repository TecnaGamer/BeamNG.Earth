"""OurAirports data (server/airports.py): parsing into runways (both ends, lengths, widths, surfaces ->
BeamNG ground models, elevations ft -> m, headings, displaced thresholds), the placement qualities
(surveyed / derived / arp) and the skip counts, helipads, determinism (row order, ids), the spatial
queries (near, in_cell against a brute-force sampling, groups), the download / weekly refresh / last
good copy logic of Store behind a fake netfetch transport and a fake wall clock, the host policy, the
cache directory's place in cache.py, and the CLI. No network."""
import contextlib
import gzip
import io
import json
import os
import random
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import airportfixtures as af  # noqa: E402
import airports  # noqa: E402
import cache as cachemod  # noqa: E402
import mapcells  # noqa: E402
import netfetch  # noqa: E402
import runwaygeom  # noqa: E402

DAY = 86400.0
T0 = 1_791_700_000.0


class SurfaceTest(unittest.TestCase):
    def test_codes(self):
        cases = {"ASP": ("asphalt", "ASPHALT"), "ASPH-G": ("asphalt", "ASPHALT"), "ASPH-P": ("asphalt", "ASPHALT_OLD"),
                 "CON": ("concrete", "ASPHALT"), "CONC-F": ("concrete", "ASPHALT"), "PEM": ("asphalt", "ASPHALT"),
                 "TURF-G": ("grass", "GRASS"), "Grass": ("grass", "GRASS"), "GRASS / SOD": ("grass", "GRASS"),
                 "Grassed brown clay": ("grass", "GRASS"), "GRE": ("gravel", "GRAVEL"), "GVL": ("gravel", "GRAVEL"),
                 "Piçarra": ("gravel", "GRAVEL"), "Earth": ("dirt", "DIRT"), "TRTD-DIRT": ("dirt", "DIRT"),
                 "SAND": ("sand", "SAND"), "WATER": ("water", None), "MATS": ("mats", "METAL"),
                 "ASPH/ CONC": ("asphalt", "ASPHALT"), "Brick": ("brick", "COBBLESTONE"), "ICE": ("ice", "ICE")}
        for raw, (cls, gm) in cases.items():
            c, _, g, paved, known = airports.surface_class(raw)
            self.assertEqual((c, g), (cls, gm), raw)
            self.assertTrue(known, raw)
            self.assertEqual(paved, cls in airports.PAVED, raw)

    def test_condition(self):
        self.assertEqual(airports.surface_class("TURF-F")[1], "fair")
        self.assertEqual(airports.surface_class("ASPH")[1], None)

    def test_unknown_follows_lighting(self):
        for raw in ("", "UNK", "X", "G", "N"):          # empty, unknown, the undocumented one-letter codes
            c, _, g, paved, known = airports.surface_class(raw, lighted=True)
            self.assertEqual((c, g, paved, known), ("asphalt", "ASPHALT", True, False))
            c, _, g, paved, known = airports.surface_class(raw, lighted=False)
            self.assertEqual((c, g, paved, known), ("dirt", "DIRT", False, False))

    def test_every_ground_model_exists(self):
        names = {"ASPHALT", "ASPHALT_OLD", "COBBLESTONE", "GRASS", "GRAVEL", "DIRT", "SAND", "SNOW", "ICE",
                 "METAL", "WOOD"}            # gameengine.zip art/groundmodels.json (0.39.4)
        self.assertTrue({g for g in airports.GROUND_MODEL.values() if g} <= names)


class ParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = af.db()
        cls.by = {r.id: r for r in cls.db.runways}

    def test_surveyed(self):
        r = self.by[101]
        self.assertEqual(r.quality, "surveyed")
        self.assertAlmostEqual(r.length_m, 3000.0, delta=0.05)
        self.assertAlmostEqual(r.le.heading, 90.0, delta=0.01)
        self.assertAlmostEqual(r.he.heading, 270.0, delta=0.01)
        self.assertAlmostEqual(r.le.elev_m, 330 * 0.3048)
        self.assertEqual(r.le.elev_source, "data")
        self.assertAlmostEqual(r.le.displaced_m, 600 * 0.3048)
        self.assertEqual(r.he.displaced_m, 0.0)
        self.assertEqual((r.surface, r.ground_model, r.paved, r.lighted, r.closed), ("asphalt", "ASPHALT", True, True, False))
        self.assertAlmostEqual(r.width_m, 150 * 0.3048, delta=1e-9)
        self.assertEqual(r.key, "oa:101")
        self.assertEqual(r.name, "KTST 09/27")

    def test_derived_from_one_end(self):
        r = self.by[301]
        self.assertEqual(r.quality, "derived")
        self.assertEqual((r.le.position, r.he.position), ("computed", "data"))
        # the computed le end lies 900 m from the he end, back along the heading
        brg, dist = airports._bearing(r.le.lat, r.le.lon, r.he.lat, r.he.lon)
        self.assertAlmostEqual(dist, round(900 / 0.3048) * 0.3048, delta=0.05)
        self.assertAlmostEqual(brg, 120.0, delta=0.05)

    def test_arp_only_for_a_single_runway(self):
        r = self.by[601]
        self.assertEqual(r.quality, "arp")
        lat, lon = r.center
        self.assertAlmostEqual(lat, 45.3, places=6)
        self.assertAlmostEqual(lon, 7.3, places=6)
        self.assertEqual(r.width_source, "default")
        self.assertEqual(r.width_m, 18.0)               # unpaved, code 2
        self.assertIn("width_default", r.flags)
        self.assertNotIn(302, self.by)                  # SKEW has two runways: no arp placement
        self.assertNotIn(602, self.by)                  # neither ends nor heading

    def test_skip_counts(self):
        st = self.db.stats
        self.assertEqual(st["skip_no_position"], 2)     # 302, 602
        self.assertEqual(st["quality_surveyed"] + st["quality_derived"] + st["quality_arp"], st["runways"])
        self.assertEqual(st["helipads"], 2)
        self.assertEqual(st["runway_rows"], 12)

    def test_helipads_apart(self):
        hp = {h.id: h for h in self.db.helipads}
        self.assertEqual(set(hp), {401, 402})
        self.assertEqual(hp[401].position, "data")
        self.assertEqual((hp[402].lat, hp[402].lon, hp[402].position), (45.0, 7.02, "airport"))
        self.assertNotIn(401, self.by)

    def test_elevation_fallback(self):
        a, r, _ = af.world()
        rows = af.to_csv([af.airport(1, "E1", 10, 10, 1000)], af.AIRPORT_COLS)
        rw = af.to_csv([af.runway(1, af.airport(1, "E1", 10, 10, 1000), 10, 10, 0, 1000, le_elev_ft=None)],
                       af.RUNWAY_COLS)
        x = airports.AirportDB.from_csv(rows, rw).runways[0]
        self.assertEqual((x.le.elev_source, x.he.elev_source), ("airport", "data"))
        self.assertAlmostEqual(x.le.elev_m, 304.8)
        self.assertIn("le_elev_airport", x.flags)

    def test_inconsistent_ends_skipped(self):
        ap = af.airport(1, "BAD", 10, 10)
        row = af.runway(1, ap, 10, 10, 0, 3000, length_ft=1000)      # 3 km apart, "1000 ft" long
        x = airports.AirportDB.from_csv(af.to_csv([ap], af.AIRPORT_COLS), af.to_csv([row], af.RUNWAY_COLS))
        self.assertEqual(x.runways, [])
        self.assertEqual(x.stats["skip_inconsistent_ends"], 1)
        row = af.runway(1, ap, 10, 10, 0, 3000, length_ft=round(2700 / 0.3048))   # 10 % off: kept, flagged
        x = airports.AirportDB.from_csv(af.to_csv([ap], af.AIRPORT_COLS), af.to_csv([row], af.RUNWAY_COLS))
        self.assertIn("length_mismatch", x.runways[0].flags)

    def test_missing_columns(self):
        with self.assertRaises(airports.DataError):
            airports.AirportDB.from_csv(b"id,ident\n1,X\n", af.world()[1])

    def test_row_order_does_not_matter(self):
        a, r, _ = af.world()
        lines = r.decode().splitlines()
        body = lines[1:]
        random.Random(3).shuffle(body)
        r2 = ("\n".join([lines[0]] + body) + "\n").encode()
        d1 = airports.AirportDB.from_csv(a, r)
        d2 = airports.AirportDB.from_csv(a, r2)
        self.assertEqual(d1.runways, d2.runways)
        self.assertEqual(d1.helipads, d2.helipads)
        self.assertEqual([x.id for x in d1.runways], sorted(x.id for x in d1.runways))
        self.assertNotEqual(d1.snapshot, d2.snapshot)       # the snapshot names the bytes

    def test_designator(self):
        self.assertEqual(airports.designator("09", 87.0), (9, "", False))
        self.assertEqual(airports.designator("27R", 0.0), (27, "R", False))
        self.assertEqual(airports.designator("N", 4.0), (36, "", True))
        self.assertEqual(airports.designator("", 123.0), (12, "", True))


class QueryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = af.db()

    def test_near(self):
        res = self.db.near(45.0, 7.0, 3000)
        ids = [r.id for _, r in res]
        self.assertEqual(ids[:2], [101, 102])           # both cross near the point
        self.assertNotIn(103, ids)                      # closed: only on request
        self.assertIn(103, [r.id for _, r in self.db.near(45.0, 7.0, 3000, include_closed=True)])
        d = [x for x, _ in res]
        self.assertEqual(d, sorted(d))
        self.assertAlmostEqual(res[0][0], 0.0, delta=1.0)
        self.assertEqual(self.db.near(45.3, 7.3, 2000), [])                                 # arp excluded
        self.assertEqual([r.id for _, r in self.db.near(45.3, 7.3, 2000, min_quality="arp")], [601])
        self.assertEqual(self.db.near(45.2, 7.2, 2000), [])                                 # closed airport

    def test_near_distance_to_segment(self):
        r = next(x for x in self.db.runways if x.id == 101)
        lat, lon = airports._offset(*r.center, 0.0, 500.0)     # 500 m north of the middle
        (d, got), = [x for x in self.db.near(lat, lon, 600) if x[1].id == 101]
        self.assertAlmostEqual(d, 500.0, delta=0.5)
        self.assertFalse([x for x in self.db.near(lat, lon, 450) if x[1].id == 101])

    def test_in_cell_matches_sampling(self):
        r = next(x for x in self.db.runways if x.id == 102)
        g = runwaygeom.geometry(r)
        s0, s1, th = g.reach_rect(runwaygeom.B_MAX)
        ss, tt = np.meshgrid(np.linspace(s0, s1, 120), np.linspace(-th, th, 40))
        lat, lon = g.latlon(ss.ravel(), tt.ravel())
        cells = {mapcells.cell_of(a, b) for a, b in zip(lat, lon)}
        for k in cells:
            self.assertIn(102, [x.id for x in self.db.in_cell(k)], k)
        far = {mapcells.CellKey(14, k.x + dx, k.y + dy) for k in cells for dx in (-3, 3) for dy in (-3, 3)} - cells
        for k in far:
            self.assertNotIn(102, [x.id for x in self.db.in_cell(k)])

    def test_in_cell_margin(self):
        r = next(x for x in self.db.runways if x.id == 201)
        k = mapcells.cell_of(*r.center)
        self.assertEqual([x.id for x in self.db.in_cell(k, margin_m=0.0)], [201])

    def test_groups(self):
        g = self.db.group(next(x for x in self.db.runways if x.id == 102))
        self.assertEqual([x.id for x in g], [101, 102])         # longest first; 103 is closed
        g = self.db.group(next(x for x in self.db.runways if x.id == 102), include_closed=True)
        self.assertEqual([x.id for x in g], [101, 102, 103])
        k = mapcells.cell_of(45.0, 7.0)
        self.assertEqual([[x.id for x in gg] for gg in self.db.groups_in_cell(k)], [[101, 102]])

    def test_helipads_near(self):
        self.assertEqual([h.id for _, h in self.db.helipads_near(45.0, 7.02, 100)], [401, 402])

    def test_lean_grades_the_same(self):
        """The workers' copy (lean(): runways only) answers the grading's queries as the full snapshot."""
        lean = self.db.lean()
        self.assertEqual(lean.airports, {})
        self.assertEqual(lean.helipads, [])
        for r in self.db.runways:
            k = mapcells.cell_of(*r.center)
            for kk in (k, mapcells.CellKey(14, k.x + 1, k.y), mapcells.CellKey(14, k.x, k.y - 1)):
                for closed in (False, True):
                    self.assertEqual([[x.id for x in g] for g in lean.groups_in_cell(kk, include_closed=closed)],
                                     [[x.id for x in g] for g in self.db.groups_in_cell(kk, include_closed=closed)])
        self.assertTrue(self.db.helipads)               # (the full one keeps them)


class FakeTransport:
    def __init__(self, files, gz=True):
        self.files, self.gz, self.calls, self.fail = dict(files), gz, [], None

    def request(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        if self.fail:
            return self.fail, {}, b"no"
        name = url.rsplit("/", 1)[1]
        body = self.files.get(name)
        if body is None:
            return 404, {}, b"missing"
        if self.gz and "gzip" in headers.get("Accept-Encoding", ""):
            return 200, {"Content-Type": "text/csv", "Content-Encoding": "gzip"}, gzip.compress(body)
        return 200, {"Content-Type": "text/csv"}, body


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        a, r, _ = af.world()
        self.files = {"airports.csv": a, "runways.csv": r}
        self.tr = FakeTransport(self.files)
        self.clock = Clock()
        pol = {h: replace(p, rate=1e9, burst=1e9, base_delay=0.0) for h, p in netfetch.POLICIES.items()}
        self.fetcher = netfetch.Fetcher(transport=self.tr, sleep=lambda s: None, policies=pol, wall=self.clock)
        self.store = airports.Store(self.tmp.name, self.fetcher, wall=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def gens(self):
        return sorted(d for d in os.listdir(self.store.root) if os.path.isdir(os.path.join(self.store.root, d)))

    def test_first_download_and_cache(self):
        db = self.store.load()
        self.assertEqual(len(self.tr.calls), 2)
        self.assertTrue(all(h["Accept-Encoding"] == "gzip" for _, h in self.tr.calls))
        self.assertTrue(all(u.startswith(airports.DATA_URL) for u, _ in self.tr.calls))
        self.assertEqual(db.stats["runways"], af.db().stats["runways"])
        p = self.store.pointer()
        self.assertEqual(p["snapshot"], db.snapshot)
        self.assertLess(p["wire_bytes"], sum(p["bytes"].values()))
        self.assertEqual(len(self.gens()), 1)
        # within the week: no request, same object
        self.clock.t += 6 * DAY
        self.assertIs(self.store.load(), db)
        self.assertEqual(len(self.tr.calls), 2)
        # a new Store (a restart) reads the disk copy
        db2 = airports.Store(self.tmp.name, self.fetcher, wall=self.clock).load()
        self.assertEqual(db2.runways, db.runways)
        self.assertEqual(len(self.tr.calls), 2)

    def test_weekly_refresh_new_content(self):
        self.store.load()
        old = self.gens()
        self.clock.t += 8 * DAY
        a, r, aps = af.world()
        self.tr.files["runways.csv"] = r + af.to_csv([af.runway(999, aps["KTST"], 45.01, 7.0, 90.0, 1200.0)],
                                                     af.RUNWAY_COLS).split(b"\n", 1)[1]
        db = self.store.load()
        self.assertEqual(len(self.tr.calls), 4)
        self.assertIn(999, [x.id for x in db.runways])
        self.assertEqual(len(self.gens()), 1)
        self.assertNotEqual(self.gens(), old)           # the old generation is gone
        self.assertTrue(all(c[1].get("Accept-Encoding") for c in self.tr.calls[2:]))

    def test_same_content_moves_the_check_time_only(self):
        self.store.load()
        gen = self.gens()
        self.clock.t += 8 * DAY
        self.store.load()
        self.assertEqual(self.gens(), gen)
        self.assertEqual(self.store.pointer()["checked"], self.clock.t)
        self.clock.t += 6 * DAY
        self.store.load()
        self.assertEqual(len(self.tr.calls), 4)

    def test_failed_refresh_keeps_the_copy_and_waits(self):
        db = self.store.load()
        self.clock.t += 8 * DAY
        self.tr.fail = 503
        with self.assertLogs("earthd.airports", "WARNING"):
            db2 = self.store.load()
        self.assertEqual(db2.runways, db.runways)
        n = len(self.tr.calls)
        p = self.store.pointer()
        self.assertTrue(p["error"])
        self.clock.t += airports.RETRY_S - 60          # not again before RETRY_S
        self.store.load()
        self.assertEqual(len(self.tr.calls), n)
        self.tr.fail = None
        self.clock.t += 120
        self.store.load()
        self.assertGreater(len(self.tr.calls), n)
        self.assertIsNone(self.store.pointer()["error"])

    def test_truncated_or_garbage_refused(self):
        self.store.load()
        gen = self.gens()
        self.clock.t += 8 * DAY
        lines = self.files["runways.csv"].split(b"\n")
        self.tr.files["runways.csv"] = b"\n".join(lines[:3]) + b"\n"         # 2 of 12 rows
        with self.assertLogs("earthd.airports", "WARNING"):
            self.store.load()
        self.assertEqual(self.gens(), gen)
        self.assertIn("refused", self.store.pointer()["error"])
        self.clock.t += airports.RETRY_S + 1
        self.tr.files["runways.csv"] = b"<html>sorry</html>"
        with self.assertLogs("earthd.airports", "WARNING"):
            self.store.load()
        self.assertEqual(self.gens(), gen)

    def test_offline(self):
        off = netfetch.Fetcher(offline=True)
        with self.assertRaises(airports.AirportsUnavailable):
            airports.Store(self.tmp.name, off, wall=self.clock).load()
        self.store.load()
        self.clock.t += 30 * DAY
        with self.assertLogs("earthd.airports", "WARNING"):
            db = airports.Store(self.tmp.name, off, wall=self.clock).load()     # the last good copy
        self.assertTrue(db.runways)

    def test_uncompressed_answer(self):
        self.tr.gz = False
        self.assertTrue(self.store.load().runways)

    def test_damaged_pointer_is_no_copy(self):
        self.store.load()
        with open(self.store.pointer_path, "w") as f:
            f.write("{not json")
        self.assertIsNone(self.store.pointer())
        self.store.load()                               # downloads again
        self.assertEqual(len(self.tr.calls), 4)


class PolicyTest(unittest.TestCase):
    def test_host_policy(self):
        p = netfetch.POLICIES[netfetch.host_of(airports.DATA_URL)]
        self.assertLessEqual(p.max_conc, 1)
        self.assertLessEqual(p.rate, 1.0)
        self.assertIsNotNone(p.daily_hard)
        self.assertTrue(p.offline_ok)

    def test_cache_keeps_the_copy(self):
        self.assertIn("airports", cachemod.KEPT)
        self.assertNotIn("airports", cachemod.MANAGED)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        a, r, _ = af.world()
        st = airports.Store(self.tmp.name, netfetch.Fetcher(offline=True), wall=lambda: T0)
        gen = "20260101T000000Z-test"
        d = os.path.join(st.root, gen)
        os.makedirs(d)
        for n, b in (("airports.csv", a), ("runways.csv", r)):
            with open(os.path.join(d, n), "wb") as f:
                f.write(b)
        st._write_pointer({"gen": gen, "downloaded": 4e9, "checked": 4e9, "rows": {}})   # "fresh" forever

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("logging.basicConfig"):
            rc = airports.main(["--cache-dir", self.tmp.name, "--offline", *args])
        return rc, out.getvalue()

    def test_near(self):
        rc, out = self.run_cli("--near", "45.0,7.0", "--radius", "3")
        self.assertEqual(rc, 0)
        self.assertIn("KTST", out)
        self.assertIn("09/27", out)
        rc, out = self.run_cli("--near", "45.0,7.0", "--radius", "3", "--json")
        j = json.loads(out)
        self.assertEqual([r["key"] for r in j["runways"]][:2], ["oa:101", "oa:102"])
        self.assertEqual(j["runways"][0]["ground_model"], "ASPHALT")

    def test_preview(self):
        import airportpreview

        class FakeDem:
            served = {}

            def stats_snapshot(self):
                return {}
        fn = af.plane(100.0, 0.002, 0.0, bumps=1.5)
        png = os.path.join(self.tmp.name, "p.png")
        with mock.patch.object(airportpreview, "dem_sampler", lambda c, f, lat: (fn, 15, FakeDem())):
            rc, out = self.run_cli("--preview", "KTST", png)
        self.assertEqual(rc, 0)
        rep = json.loads(out)
        self.assertEqual([r["key"] for r in rep["runways"]], ["oa:101", "oa:102", "oa:103"])
        self.assertEqual(rep["runways"][2]["markings"]["class"], "closed")
        from PIL import Image
        with Image.open(png) as im:
            self.assertGreater(im.width, 500)



class BandTest(unittest.TestCase):
    def test_band_holds_every_point_within_reach(self):
        rng = np.random.default_rng(7)
        lat = rng.uniform(-85, 85, 20000)
        lon = rng.uniform(-180, 180, 20000)
        order = np.argsort(lat, kind="stable")
        for _ in range(300):
            qlat, qlon = rng.uniform(-88, 88), rng.uniform(-180, 180)
            r = rng.choice([1e3, 2e4, 2e5, 1e6])
            # points near the query too
            near_lat = np.clip(qlat + rng.normal(0, r / 111e3, 50), -89.9, 89.9)
            near_lon = ((qlon + rng.normal(0, r / 60e3, 50)) + 180) % 360 - 180
            la, lo = np.r_[lat, near_lat], np.r_[lon, near_lon]
            o = np.argsort(la, kind="stable")
            c = airports._band(la[o], o, qlat, qlon, lo, r)
            want = np.flatnonzero(airports._hav(qlat, qlon, la, lo) <= r)
            if c is None:
                continue
            self.assertTrue(set(want.tolist()) <= set(c.tolist()), (qlat, qlon, r))
            self.assertTrue((np.diff(c) > 0).all())

if __name__ == "__main__":
    unittest.main()
