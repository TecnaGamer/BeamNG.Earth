"""The real snow state belongs to its place (local weather plan L1a, ~/bnge-tests/localweather/PLAN.md 1 and 7; the
user, 2026-11-27: "as i was driving down pikes peak all terrain suddenly changed to non snow", "snow should only be
shown when its actually snowed"): a teleport never carries the last place's band table, a deferred or failed fetch is
tried again within seconds, the climatic permanent snow is gone from the real state, the bands stop at the cell's real
relief, what changed is logged, and mild-winter places without snowfall show no snow. Fake fetches and fixtures only
(openmeteo_snow_hourly.json recorded, openmeteo_mild_january.json synthetic): no network.
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fast  # noqa: E402
import geodesy  # noqa: E402
import netfetch  # noqa: E402
import snow  # noqa: E402
import weather  # noqa: E402
from test_snow import RESPONSES, FakeCover, last_hour  # noqa: E402
from test_snowcover import winter  # noqa: E402

MILD = {r["name"]: r for r in json.loads((Path(__file__).resolve().parent / "fixtures" / "openmeteo_mild_january.json")
                                         .read_text())["responses"]}
NATIVE = fast._ef is not None and getattr(fast._ef, "VERSION", 0) >= snow.NATIVE
RELIEF = fast._ef is not None and getattr(fast._ef, "VERSION", 0) >= snow.NATIVE_RELIEF

VALDEZ = (61.13, -146.36, 100.0)        # (points off the 0.1-degree cells' edges)
PIKES = (38.84, -105.04, 4299.0)
ATLANTA = (33.76, -84.39, 320.0)
LONDON = (51.51, -0.13, 23.0)


def box(at, d=0.05):
    return (at[0] - d, at[1] - d, at[0] + d, at[1] + d)


class ByCell:
    """A fake hourly fetch: the response named for each place's cell; `fail` = how many first calls there raise."""

    def __init__(self, names, fail=None, raise_=None):
        self.names = {weather.cell(p[0], p[1]): n for p, n in names.items()}
        self.fail = {weather.cell(p[0], p[1]): k for p, k in (fail or {}).items()}
        self.raise_ = raise_ or (lambda: netfetch.Deferred("api.open-meteo.com: busy, background dropped"))
        self.calls = []

    def __call__(self, lat, lon, past=240, ahead=6):
        k = (lat, lon)
        self.calls.append(k)
        if self.fail.get(k, 0) > 0:
            self.fail[k] -= 1
            raise self.raise_()
        n = self.names[k]
        return MILD[n] if n in MILD else RESPONSES[n]


class Server:
    def __init__(self, svc):
        self.snow, self.connections, self.changes, self.road_changes = svc, [], 0, 0

    def snow_changed(self):
        self.changes += 1

    def snow_roads_changed(self):
        self.road_changes += 1


class Conn:
    """earthd's Connection as the Tracker uses it: s, loop, send, spawn."""

    def __init__(self, server, loop):
        self.s, self.loop, self.sent, self.tasks = server, loop, [], []
        server.connections.append(self)

    def send(self, m):
        self.sent.append(m)

    def spawn(self, coro):
        self.tasks.append(self.loop.create_task(coro))

    def settle(self):
        if self.tasks:
            self.loop.run_until_complete(asyncio.gather(*self.tasks))
        self.tasks.clear()


def pose(tracker, conn, at):
    tracker.on_pose(conn, geodesy.EnuFrame(at[0], at[1]), 0.0, 0.0, at[2])


@unittest.skipUnless(NATIVE, "needs earthfast 624 (the snow kernel)")
class PlaceTest(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.now = [last_hour(RESPONSES["davos"]) - 24 * 3600 + 600]

    def tearDown(self):
        self.loop.close()

    def session(self, fetch, mode="real"):
        cover = FakeCover()
        svc = snow.Service(fetch=fetch, mode=mode, clock=lambda: self.now[0], cover=cover, winter=True)
        server = Server(svc)
        return svc, server, Conn(server, self.loop), snow.Tracker(), cover

    def test_teleport_drops_the_old_table(self):
        """The user's drive: Valdez (deep snow) then Pikes Peak, its first fetch dropped by the busy network policy.
        Pikes Peak's boxes never get Valdez's table; the next try comes within seconds, not RETRY_S."""
        fetch = ByCell({VALDEZ: "davos", PIKES: "minneapolis"}, fail={PIKES: 1})
        svc, server, conn, tr, cover = self.session(fetch)
        pose(tr, conn, VALDEZ)
        conn.settle()
        valdez = svc.table
        self.assertIsNotNone(cover.provider(*box(VALDEZ)), "Valdez: its own snow")
        self.assertEqual(server.changes, 1)
        self.assertFalse(cover.climate, "the real state's provider: no climatic snow")
        # the teleport: the table goes at the first pose there, before its state is in
        self.now[0] += 360
        pose(tr, conn, PIKES)
        self.assertIsNone(svc.table)
        self.assertIsNone(cover.provider(*box(PIKES)), "Pikes Peak: never Valdez's snow")
        self.assertEqual(server.changes, 2, "what was drawn with Valdez's snow drawn again")
        self.assertEqual(conn.sent[-1], {"type": "snow", "snow": {"mode": "real", "cover": 0, "depth": 0}})
        self.assertIsNone(tr.msg, "a hello is not answered with Valdez's state")
        conn.settle()                               # (the first fetch there: dropped, "busy, background dropped")
        self.assertIsNone(svc.table)
        self.assertEqual(tr.fails, 1)
        self.assertAlmostEqual(tr.retry_at - self.now[0], snow.RETRY_FAST_S, delta=1.0)
        self.assertLess(snow.RETRY_FAST_S, 30.0)
        self.now[0] += 5
        pose(tr, conn, PIKES)
        self.assertFalse(conn.tasks, "not before the retry")
        self.now[0] += snow.RETRY_FAST_S
        pose(tr, conn, PIKES)
        conn.settle()
        self.assertIsNotNone(svc.table)
        self.assertNotEqual(svc.table, valdez)
        self.assertEqual(tr.fails, 0)
        self.assertLess(weather.distance_m(svc.site[0], svc.site[1], PIKES[0], PIKES[1]), 1000.0)
        states = [m for m in conn.sent if m["type"] == "snowstate"]
        self.assertEqual(len(fetch.calls), 3)
        self.assertLess(abs(states[-1]["lat"] - PIKES[0]), 0.1)

    def test_a_state_for_the_old_place_is_dropped(self):
        """Valdez's fetch still in flight at the teleport: its state, when it comes, is neither sent nor drawn."""
        fetch = ByCell({VALDEZ: "davos", PIKES: "minneapolis"})
        svc, server, conn, tr, cover = self.session(fetch)
        pose(tr, conn, VALDEZ)                      # (in flight ...)
        pose(tr, conn, PIKES)                       # (... when the player arrives at Pikes Peak)
        conn.settle()
        states = [m for m in conn.sent if m["type"] == "snowstate"]
        self.assertEqual(len(states), 1)
        self.assertLess(abs(states[0]["lat"] - PIKES[0]), 0.1)
        self.assertLess(weather.distance_m(svc.site[0], svc.site[1], PIKES[0], PIKES[1]), 1000.0)
        alone = snow.Service(fetch=ByCell({PIKES: "minneapolis"}), mode="real", winter=True, clock=lambda: self.now[0],
                             cover=FakeCover())
        raw = {}
        alone.get(*PIKES, -1, raw)
        self.assertEqual(svc.table[0], snow.w2_table(raw, [-1] * 8)[0], "Pikes Peak's own table")

    def test_a_new_game_elsewhere_drops_the_table(self):
        """The server's last place was Valdez; a game connects again and starts at Pikes Peak ("Where you left off"):
        its first pose drops the table; one that starts at Valdez keeps it."""
        fetch = ByCell({VALDEZ: "davos", PIKES: "minneapolis"}, fail={PIKES: 1})
        svc, server, conn, tr, cover = self.session(fetch)
        pose(tr, conn, VALDEZ)
        conn.settle()
        again = Conn(server, self.loop)
        pose(snow.Tracker(), again, VALDEZ)
        self.assertIsNotNone(svc.table)
        again.settle()
        pose(snow.Tracker(), Conn(server, self.loop), PIKES)
        self.assertIsNone(svc.table)
        self.assertIsNone(cover.provider(*box(PIKES)))
        for c in server.connections:
            c.settle()

    def test_a_short_hop_keeps_the_table(self):
        """A rebase or a hop of a few miles is the same weather: the table stays until the next state replaces it."""
        fetch = ByCell({VALDEZ: "davos", (VALDEZ[0] + 0.2, VALDEZ[1], 100.0): "davos"})
        svc, server, conn, tr, cover = self.session(fetch)
        pose(tr, conn, VALDEZ)
        conn.settle()
        t = svc.table
        pose(tr, conn, (VALDEZ[0] + 0.2, VALDEZ[1], 100.0))     # 14 mi north
        self.assertEqual(svc.table, t)
        self.assertEqual(server.changes, 1)
        self.assertEqual(svc.places_left, 0)
        conn.settle()
        # another player's jump that lands near the table's place keeps it too (the table is that place's)
        self.assertFalse(svc.leave(conn, VALDEZ[0], VALDEZ[1]))

    def test_presets_are_the_same_everywhere(self):
        svc, server, conn, tr, cover = self.session(ByCell({}), mode="light")
        self.assertTrue(cover.climate, "a preset's provider: its climatic snow too")
        pose(tr, conn, VALDEZ)
        conn.settle()
        t = svc.table
        pose(tr, conn, PIKES)
        conn.settle()
        self.assertEqual(svc.table, t)
        self.assertEqual(svc.places_left, 0)

    def test_backoff(self):
        svc = snow.Service(fetch=ByCell({}), mode="real")
        self.assertEqual([svc.backoff(n) for n in range(1, 8)], [15.0, 30.0, 60.0, 120.0, 240.0, 300.0, 300.0])
        # a host that says how long: that, when longer
        fetch = ByCell({PIKES: "minneapolis"}, fail={PIKES: 1},
                       raise_=lambda: netfetch.HostUnavailable("api.open-meteo.com: unavailable", retry_after=100.0))
        svc, server, conn, tr, cover = self.session(fetch)
        pose(tr, conn, PIKES)
        conn.settle()
        self.assertAlmostEqual(tr.retry_at - self.now[0], 100.0, delta=1.0)

    def test_change_is_logged(self):
        """Which part of the table changed (the plan's unexplained 14:53:44 re-texture): bands, roads, lake, grass."""
        fetch = ByCell({VALDEZ: "davos"})
        svc, server, conn, tr, cover = self.session(fetch)
        with self.assertLogs("earthd.snow", "INFO") as logs:
            pose(tr, conn, VALDEZ)
            conn.settle()
        line = [m for m in logs.output if "snow table changed" in m][0]
        self.assertIn("a first table for this place", line)
        old = (((100, 0, 0, 0), (400, 8, 3, 1)), (8,) * 5, 0)          # (rows, road shares, lake ice)
        new = (((100, 0, 0, 0), (400, 8, 4, 1)), (8, 8, 8, 6, 8), 2)
        text = snow.table_change(old, new, {"cover": 8, "depth": 3}, {"cover": 8, "depth": 4})
        self.assertIn("bands 400 m 8/8 D3 a1 -> 8/8 D4 a1", text)
        self.assertIn("road shares", text)
        self.assertIn("lake ice 0 -> 2", text)
        self.assertIn("grass", text)
        self.assertNotIn("100 m", text)
        self.assertEqual(snow.table_change(old, old, None, None), "nothing")

    def test_no_snow_before_or_after_draws_nothing_again(self):
        """A first state without snow (or one bare state after another) changes no texture: no snow_changed."""
        fetch = ByCell({ATLANTA: "atlanta_dry"})
        svc, server, conn, tr, cover = self.session(fetch)
        self.now[0] = MILD["atlanta_dry"]["hourly"]["time"][-7]
        pose(tr, conn, ATLANTA)
        conn.settle()
        self.assertIsNotNone(svc.table)
        self.assertEqual(server.changes, 0)


class ReliefGrid:
    """A fake dem.Dem: heights rising to the north east, NaN in one corner."""

    def __init__(self):
        self.calls = []

    def sample(self, lat, lon, zoom):
        self.calls.append((np.shape(lat), zoom))
        z = np.asarray(100.0 + 1000.0 * (lat - 40.0) + 500.0 * (lon + 105.1), np.float64)
        z[0, 0] = np.nan
        return z


class ReliefTest(unittest.TestCase):
    def test_cell_relief(self):
        dem = ReliefGrid()
        z = snow.cell_relief(dem, 40.05, -105.05)       # (the cell 40.0-40.1 N: its own ground only)
        self.assertEqual(dem.calls, [((snow.RELIEF_N, snow.RELIEF_N), snow.RELIEF_ZOOM)])
        self.assertTrue(250 <= z <= 251, z)           # (the north east corner: 100 + 100 + 50)

        class Sea:
            def sample(self, lat, lon, zoom):
                return np.full(np.shape(lat), np.nan)
        self.assertIsNone(snow.cell_relief(Sea(), 0.0, 0.0))

    @unittest.skipUnless(RELIEF, "needs earthfast 644 (snow_band_table_relief)")
    def test_relief_is_kept_per_cell_and_a_failure_is_not(self):
        asked = []

        def relief(lat, lon):
            asked.append((lat, lon))
            if len(asked) == 1:
                raise OSError("no DEM tile")
            return 150
        svc = snow.Service(fetch=ByCell({LONDON: "london_showers"}), mode="real", winter=True, relief=relief, cover=FakeCover(),
                           clock=lambda: MILD["london_showers"]["hourly"]["time"][-7])
        raw = {}
        svc.get(*LONDON, -1, raw)
        self.assertIsNone(raw["relief"], "the DEM failed: the bands uncut this time")
        for _ in range(2):
            raw = {}
            svc.get(LONDON[0] + 0.01, LONDON[1], LONDON[2], -1, raw)
            self.assertEqual(raw["relief"], 150)
        self.assertEqual(len(asked), 2)


@unittest.skipUnless(RELIEF, "needs earthfast 644 (snow_band_table_relief)")
class ClampTest(unittest.TestCase):
    def state(self, name, at, relief):
        svc = snow.Service(fetch=ByCell({at: name}), mode="real", winter=True, relief=relief, cover=FakeCover(),
                           clock=lambda: MILD[name]["hourly"]["time"][-7])
        raw = {}
        svc.get(*at, -1, raw)
        return raw, svc

    def test_bands_stop_at_the_cells_relief(self):
        """London-like showers: rain at the cell, snow on the kernel's bands 1,000-1,500 m above it (extrapolated by a
        lapse rate: no ground there). Cut to the cell's real relief, the table claims no snow anywhere."""
        raw, _ = self.state("london_showers", LONDON, None)
        rows, _, _ = snow.w2_table(raw, [-1] * 8)
        self.assertEqual(rows[-1][0], 23 + 1500)
        self.assertGreater(rows[-1][1], 0, "uncut: the top band white (the bug)")
        raw, _ = self.state("london_showers", LONDON, lambda la, lo: 150)
        rows, road, bins = snow.w2_table(raw, [-1] * 8)
        self.assertLessEqual(max(r[0] for r in rows), 150 + 200)
        self.assertTrue(all(r[1] == 0 for r in rows), rows)
        self.assertEqual(len(bins), 8)
        # a mountain cell keeps every band its ground reaches
        raw, _ = self.state("london_showers", LONDON, lambda la, lo: 2000)
        self.assertEqual(snow.w2_table(raw, [-1] * 8)[0][-1][0], 23 + 1500)

    def test_in_the_photos(self):
        with winter("real") as sc:
            for relief, white in ((None, True), (lambda la, lo: 150, False)):
                svc = snow.Service(fetch=ByCell({LONDON: "london_showers"}), mode="real", winter=True, relief=relief, cover=sc,
                                   clock=lambda: MILD["london_showers"]["hourly"]["time"][-7])
                raw = {}
                svc.get(*LONDON, -1, raw)
                svc.commit(Conn(Server(svc), None), raw)
                self.assertEqual(sc.spec_for_box(*box(LONDON)) is not None, white, relief)


@unittest.skipUnless(RELIEF, "needs earthfast 644 (snow_band_table_relief)")
class MildWinterTest(unittest.TestCase):
    """Mild-winter places in a dry January week (no snowfall in the window, the model's snow depth 0): no snow on any
    photo or far tile round them, from the band table or the climate (the user, 2026-11-27: "many places during winter
    rarly get snowfall")."""

    def test_no_snow(self):
        for name, at, relief in (("atlanta_dry", ATLANTA, 450), ("london_dry", LONDON, 150),
                                 ("london_showers", LONDON, 150)):
            with winter("real") as sc:
                svc = snow.Service(fetch=ByCell({at: name}), mode="real", winter=True, relief=lambda la, lo, z=relief: z,
                                   cover=sc, clock=lambda n=name: MILD[n]["hourly"]["time"][-7])
                raw = {}
                m = svc.get(*at, -1, raw)
                server = Server(svc)
                svc.commit(Conn(server, None), raw)
                self.assertEqual(m["ground"][:3], [0.0, 0, 0], name)
                self.assertTrue(all(r[1] == 0 for r in svc.table[0]), (name, svc.table))
                self.assertEqual(server.changes, 0, name)
                for d in (0.05, 0.5):
                    for dlat, dlon in ((0, 0), (1.0, 0), (0, 1.5), (-1.0, -1.0)):
                        self.assertIsNone(sc.spec_for_box(*box((at[0] + dlat, at[1] + dlon), d)), (name, dlat, dlon))
                frame = geodesy.EnuFrame(at[0], at[1])
                self.assertIsNone(sc.spec_for_square(frame, -20000, -20000, 40000), name)

    def test_no_climatic_snow_in_the_alps_without_a_state(self):
        with winter("real") as sc:
            svc = snow.Service(fetch=ByCell({}), mode="real", winter=True, cover=sc)
            self.assertIsNone(svc.table)
            self.assertIsNone(sc.spec_for_box(46.5, 7.9, 46.6, 8.0), "the Bernese Oberland: no state, no snow")


if __name__ == "__main__":
    unittest.main()
