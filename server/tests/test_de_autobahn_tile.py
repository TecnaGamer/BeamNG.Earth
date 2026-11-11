"""German Autobahn signs in the sign tiles (signstream -> deautobahn -> the kernel): a German tile with German shard
data gets its own field "autobahn"; everywhere else, and with old shards or BNE_AUTOBAHN_SIGNS=off, the tile is what it
was. Germany by real borders (countrybounds, the tests' slice of the border data)."""
import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regional"))
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cdae  # noqa: E402
import countrybounds  # noqa: E402
import deautobahn  # noqa: E402
import deautobahnfont  # noqa: E402
import fast  # noqa: E402
import geodesy  # noqa: E402
import highwaysigns  # noqa: E402
import roads  # noqa: E402
import signstream  # noqa: E402
import test_highway_signs  # noqa: E402
import test_roads  # noqa: E402

DE, CZ, US, FR = (49.02, 12.16), (49.66, 12.56), (39.0, -76.95), (48.6, 7.7)


def flat(la, lo):
    return np.zeros(np.shape(la))


def setUpModule():
    fast.require(deautobahn.KERNEL, "German Autobahn sign tests")
    assert countrybounds.bundled().codes_at([(49.02, 12.16)]) == ["DE"], "the border fixture is missing"


class GermanTags(test_highway_signs.FakeTags):
    """FakeTags with the shards' top-level German marker."""

    def __init__(self, objects=(), marked=True):
        super().__init__(objects)
        self.marked = marked

    def shard(self, x, y):
        return {"objects": [], "de": 1} if self.marked else {"objects": []}


def german_record(md, frame, jid=-20, dx=0.0, de=True, west=False):
    """The fixture's exit (test_highway_signs.fixture: an eastbound motorway at y = 100 and a ramp from x = 0) as a
    German shard record: Neutraubling's tags, a 1.9 km approach (dx moves it east). west: the same exit on a westbound
    carriageway 20 m north (y = 120), its approach coming from the east."""
    def ll(x, y):
        la, lo, _ = frame.enu_to_geodetic(x + dx, y, 0.)
        return [float(lo), float(la)]
    if west:
        approach = [ll(x, 120.) for x in np.arange(1900., -0.1, -100.)]
        ramp = [ll(0., 120.), ll(-100., 240.), ll(-200., 320.)]
    else:
        approach = [ll(x, 100.) for x in np.arange(-1900., 0.1, 100.)]
        ramp = [ll(0., 100.), ll(100., -20.), ll(200., -100.)]
    d = dict(label=dict(ref='102', names=['Neutraubling'], routes=[]), approach=approach[-19:], ramp=ramp,
             main_ids=[10], ramp_id=20, side=1, through_label=None)
    if de:
        d.update(de_approach=approach, de_ramp=ramp, de={
            "v": 1, "node": {"ref": "102", "name": "Neutraubling", "noref": False},
            "edges": [[10, 0.0, 1900.0, {"lanes": "3", "turn:lanes": "none|none|slight_right",
                                         "destination:lanes": "Linz;Passau|Linz;Passau|Neutraubling;Barbing"}]],
            "lanes_src": 0, "link": {"destination": "Neutraubling;Barbing"}, "fork_s": None, "branches": [],
            "through": None, "main": {"ref": "A 3", "int_ref": "E 56"}, "joins": None, "next": None,
            "prev_merge_s": None, "prev_exit_s": None, "stop": "end"})
    lon, lat = ramp[0]
    return dict(id=jid, lon=lon, lat=lat, tags={}, ways=[], exit=d)


class TileCase(unittest.TestCase):
    def tile(self, origin, records=None, env="", marked=True, eu=False, tx=0):
        """The files of sign tile (tx, 0) at origin (lat, lon) with the fixture's roads and the given records."""
        lat, lon = origin
        frame = geodesy.EnuFrame(lat, lon)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.multiple(test_roads, LAT=lat, LON=lon, M_LON=111320. * np.cos(np.radians(lat))), \
                mock.patch.object(test_highway_signs, 'FRAME', frame), mock.patch.object(deautobahn, 'ENV', env), \
                mock.patch.dict(os.environ, {'BNE_EU_ROAD_DETAILS': 'on' if eu else 'off'}):
            md = test_highway_signs.fixture(d)
            recs = records(md, frame) if records else [german_record(md, frame)]
            md.highway_signs = highwaysigns.Source(md, tags=GermanTags(recs, marked))
            self.addCleanup(md.highway_signs.close)
            signstream.build_tile(md, frame, 1, tx, 0, flat, d, '/temp', mph=False, country='DE')
            return {p.name: p.read_bytes() for p in Path(d).iterdir() if p.is_file()}


class GermanTileTest(TileCase):
    def test_de_tile_has_autobahn_field(self):
        files = self.tile(DE)
        doc = json.loads(files['signs_1_0_0.json'])
        self.assertIn('autobahn', doc)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, 'a.cdae')
            p.write_bytes(files['autobahn_1_0_0.cdae'])
            self.assertTrue({m['name'] for m in cdae.read(str(p))['materials']} <= set(deautobahnfont.MATERIALS))

    def test_env_off_is_today(self):
        """BNE_AUTOBAHN_SIGNS=off: the German code never runs, so the tile is what it was before it existed."""
        with mock.patch.object(deautobahn, 'build_tile') as build:
            off = self.tile(DE, env='off')
            build.assert_not_called()
        self.assertNotIn('autobahn', json.loads(off['signs_1_0_0.json']))

    def test_old_shards_unchanged(self):
        old = lambda md, frame: [german_record(md, frame, de=False)]      # noqa: E731
        self.assertEqual(self.tile(DE, old, marked=False), self.tile(DE, old, env='off', marked=False))
        self.assertNotIn('autobahn', json.loads(self.tile(DE, old, marked=False)['signs_1_0_0.json']))


class OutsideGermanyTest(TileCase):
    def test_outside_germany_unchanged(self):
        for origin, eu in ((US, False), (CZ, False), (FR, False), (FR, True)):
            with self.subTest(origin=origin, eu=eu):
                on, off = self.tile(origin, eu=eu), self.tile(origin, env='off', eu=eu)
                self.assertEqual(on, off)
                self.assertNotIn('autobahn', json.loads(on['signs_1_0_0.json']))

    def test_no_border_lookup_outside_box(self):
        with mock.patch.object(countrybounds, 'bundled') as bundled:
            self.tile(US)
            bundled.assert_not_called()


class BorderTileTest(unittest.TestCase):
    def test_only_german_junctions(self):
        """A tile on the Waidhaus / Rozvadov border: a German exit 2 km west, a Czech one 2.5 km east of its origin."""
        lat, lon = 49.65, 12.535
        frame = geodesy.EnuFrame(lat, lon)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.multiple(test_roads, LAT=lat, LON=lon, M_LON=111320. * np.cos(np.radians(lat))), \
                mock.patch.object(test_highway_signs, 'FRAME', frame), mock.patch.object(deautobahn, 'ENV', ''):
            md = test_highway_signs.fixture(d)
            recs = [german_record(md, frame, jid=-21, dx=-2000.), german_record(md, frame, jid=-22, dx=2500.)]
            codes = countrybounds.bundled().codes_at([(r['lat'], r['lon']) for r in recs])
            self.assertEqual(codes, ['DE', 'CZ'])
            md.highway_signs = highwaysigns.Source(md, tags=GermanTags(recs))
            self.addCleanup(md.highway_signs.close)
            context = md.highway_signs.context(None, None)
            ids = set()
            for tx in range(-16, 12):
                _, boards = deautobahn.build_tile(md, frame, tx, 0, flat, context)
                ids |= {json.loads(b)['j'] for b in boards}
            self.assertEqual(ids, {-21})




class FollowUpTest(unittest.TestCase):
    """A cloverleaf's later exit of the same junction on one carriageway follows the first: its bound is the first."""

    def test_second_exit_follows_the_first(self):
        frame = geodesy.EnuFrame(*DE)
        md = None
        first = german_record(md, frame, jid=-31, dx=-450.)
        second = german_record(md, frame, jid=-32)
        got = deautobahn.follow_ups([first, second], frame)
        self.assertEqual(set(got), {-32})
        self.assertAlmostEqual(got[-32], 450.0, delta=5.0)

    def test_opposite_carriageway_is_not_a_follow_up(self):
        """The same junction's exit on the other carriageway, 20 m away and 300 m 'upstream' along this approach."""
        frame = geodesy.EnuFrame(*DE)
        other = german_record(None, frame, jid=-33, dx=-300., west=True)
        mine = german_record(None, frame, jid=-32)
        self.assertEqual(deautobahn.follow_ups([other, mine], frame), {})

    def test_other_junction_does_not(self):
        frame = geodesy.EnuFrame(*DE)
        first = german_record(None, frame, jid=-31, dx=-450.)
        first['exit']['de'] = dict(first['exit']['de'], node={"ref": "101", "name": "Regensburg-Ost", "noref": False})
        second = german_record(None, frame, jid=-32)
        self.assertEqual(deautobahn.follow_ups([first, second], frame), {})


class GlueRobustnessTest(unittest.TestCase):
    def test_absurd_lanes_clamp(self):
        for v, want in (("1e12", 8), ("inf", None), ("-4", 1), ("3", 3), ("x", None)):
            self.assertEqual(deautobahn.lanes_at([[1, 0.0, 100.0, {"lanes": v}]], 50.0), want, v)

    def test_layout_gets_the_drawn_lanes(self):
        """lanes=3 with turn:lanes=none|none|slight_right: the layout spreads the boards over the 3 lanes the road is
        drawn with (the exit lane included), the mounting counts the 2 through lanes (Codex review of #999: the
        through arrows sat over the exit lane)."""
        lat, lon = DE
        frame = geodesy.EnuFrame(lat, lon)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.multiple(test_roads, LAT=lat, LON=lon, M_LON=111320. * np.cos(np.radians(lat))), \
                mock.patch.object(test_highway_signs, 'FRAME', frame), mock.patch.object(deautobahn, 'ENV', ''):
            md = test_highway_signs.fixture(d)
            rec = german_record(md, frame)
            md.highway_signs = highwaysigns.Source(md, tags=GermanTags([rec]))
            self.addCleanup(md.highway_signs.close)
            got = deautobahn._junction(rec, frame, md.highway_signs.context(None, None), False)
            rows = got["rows"]
            col = deautobahn.PROFILE_COLS.index
            on = rows[:, col("s")] < 1900.0
            self.assertTrue((rows[on, col("lanes")] == 3).all(), rows[on, col("lanes")][:5])
            self.assertTrue((rows[on, col("through")] == 2).all(), rows[on, col("through")][:5])

    def test_one_bad_junction_does_not_blank_the_tile(self):
        lat, lon = DE
        frame = geodesy.EnuFrame(lat, lon)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.multiple(test_roads, LAT=lat, LON=lon, M_LON=111320. * np.cos(np.radians(lat))), \
                mock.patch.object(test_highway_signs, 'FRAME', frame), mock.patch.object(deautobahn, 'ENV', ''):
            md = test_highway_signs.fixture(d)
            good = german_record(md, frame, jid=-41)
            bad = german_record(md, frame, jid=-42, dx=-300.)
            bad['exit']['de']['edges'] = "not a list of edges"
            md.highway_signs = highwaysigns.Source(md, tags=GermanTags([good, bad]))
            self.addCleanup(md.highway_signs.close)
            context = md.highway_signs.context(None, None)
            ids = set()
            for tx in range(-8, 2):
                _, boards = deautobahn.build_tile(md, frame, tx, 0, flat, context)
                ids |= {json.loads(b)['j'] for b in boards}
            self.assertIn(-41, ids)

    def test_fork_spot_where_the_branches_part(self):
        d = np.arange(0.0, 151.0, 5.0)
        left = np.c_[d, d * 0.15]                  # two branches parting at 0.3 m per metre
        right = np.c_[d, -d * 0.15]
        got = deautobahn.fork_spot(left, right, 8.0, 8.0, 7.0)          # (need 7 m: the test's own)
        self.assertIsNotNone(got)
        at, s = got
        self.assertGreaterEqual(2 * 0.15 * s - 8.0, 7.0 - 1e-6)    # the gap fits the boards
        self.assertLess(s, 80.0)
        self.assertIsNone(deautobahn.fork_spot(left[:5], right[:5], 8.0, 8.0, 7.0))


class PavementTest(unittest.TestCase):
    """No post foot on any pavement: the fixture's motorway and ramp plus a service road 1 m outside the motorway's
    right edge all along the approach."""

    def test_no_post_on_a_pavement(self):
        import exitsigns
        import signroads
        import test_blockworker
        lat, lon = DE
        frame = geodesy.EnuFrame(lat, lon)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.multiple(test_roads, LAT=lat, LON=lon, M_LON=111320. * np.cos(np.radians(lat))), \
                mock.patch.object(test_highway_signs, 'FRAME', frame), mock.patch.object(deautobahn, 'ENV', ''):
            f = test_roads.features([[(-1900., 100.), (700., 100.)], [(0., 100.), (100., -20.), (200., -100.)],
                                     [(-1900., 86.), (-60., 86.)]],
                                    ['motorway', 'motorway', 'service'], ids=['osm:w10:0', 'osm:w20:0', 'osm:w30:0'])
            f.rules['oneway'] = test_roads.rules([(0, 1.), (1, 1.)], 'float')
            f.rules['subclass'] = test_roads.rules([(1, 'link')])
            f.attrs['osm_lanes'] = np.array([3., 1., 1.])
            md = test_blockworker.FakeMapData(f, roads.cells_of_tile(frame, 0, 0)[0])
            md.cache_dir, md.fetcher = d, None
            md.highway_signs = highwaysigns.Source(md, tags=GermanTags([german_record(md, frame)]))
            self.addCleanup(md.highway_signs.close)
            context = md.highway_signs.context(None, None)
            boards = []
            for tx in range(-9, 2):
                for ty in range(-1, 2):
                    boards += [json.loads(b) for b in deautobahn.build_tile(md, frame, tx, ty, flat, context)[1]]
            self.assertTrue(boards)
            la, lo, _ = frame.enu_to_geodetic(np.array([-2000., 800.]), np.array([-200., 300.]), 0.)
            pav = context.pavements(frame, np.c_[lo, la], False)
            polys = [exitsigns._outline(r[2], r[3]) for r in pav]
            def inside(pt):
                x, y = pt
                for poly in polys:
                    a, b = poly[:-1, :2], poly[1:, :2]
                    cross = ((a[:, 1] > y) != (b[:, 1] > y))
                    with np.errstate(divide='ignore', invalid='ignore'):
                        at = a[:, 0] + (y - a[:, 1]) * (b[:, 0] - a[:, 0]) / (b[:, 1] - a[:, 1])
                    if (cross & (x < at)).sum() % 2 == 1:
                        return True
                return False
            bad = [(b['board'], p) for b in boards for p in b['posts'] if inside(p)]
            self.assertEqual(bad, [])

class BorderLookupTest(unittest.TestCase):
    """Whether a tile is German is germany.contains' answer (dev's shared border lookup: the box first, real borders
    inside it; no border data: not Germany, today's output), asked where the tile is built (a road process); the sign
    tiles are a session's own, so a session without border data builds today's tiles and the next one the German signs
    (Codex review of #999: a version folded in from a worker-local flag never reached the main process's Tracker)."""

    def tile_at(self, lat, lon):
        frame = geodesy.EnuFrame(lat, lon)
        return frame

    def test_asks_germanys_lookup(self):
        frame = self.tile_at(48.58, 7.75)                   # Strasbourg: inside the box, France by the borders
        import germany
        with mock.patch.object(deautobahn, 'ENV', ''), \
                mock.patch.object(germany, 'contains', side_effect=lambda lon, lat: np.ones(np.size(lon), bool)):
            self.assertTrue(deautobahn.german_tile(frame, 0, 0))
        with mock.patch.object(deautobahn, 'ENV', ''), \
                mock.patch.object(germany, 'contains', side_effect=lambda lon, lat: np.zeros(np.size(lon), bool)):
            self.assertFalse(deautobahn.german_tile(self.tile_at(*DE), 0, 0))

    def test_no_border_data_is_not_germany(self):
        import germany
        with mock.patch.object(deautobahn, 'ENV', ''), \
                mock.patch.object(germany, '_index', return_value=countrybounds.Absent()):
            self.assertFalse(deautobahn.german_tile(self.tile_at(*DE), 0, 0))
        with mock.patch.object(deautobahn, 'ENV', 'on'), \
                mock.patch.object(germany, '_index', return_value=countrybounds.Absent()):
            self.assertTrue(deautobahn.german_tile(self.tile_at(*DE), 0, 0))


if __name__ == '__main__':
    unittest.main()
