"""Winter W1 (server/snow.py + native/earthfast/src/snow.rs, BNE_WINTER): the integer snow kernel's determinism (the
platform-independent hash over 10,000 random forcing records: the same number on Linux and Windows), Open-Meteo
hourly responses recorded 2026-11-24 (fixtures/openmeteo_snow_hourly.json: Minneapolis dry and mild, Tromso and Davos
snowing) turned into kernel records and 'snowstate' messages, the presets, the regime from the climate, the shared
fetch cache, the tracker's rule, and the switch being off by default (no service, no message). No network.
"""
import json
import math
import os
import subprocess
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fast  # noqa: E402
import snow  # noqa: E402
import test_server  # noqa: E402
import weather  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "openmeteo_snow_hourly.json"
RESPONSES = {r["name"]: r for r in json.loads(FIX.read_text())["responses"]}
ICY = ("ice", "black_ice", "wet_ice", "frost")
# snow.rs selftest(seed 1, 10,000 records): Linux (rustc, the .so) and Windows (the same file cross-built, run under
# Wine) gave this number (~/bnge-tests/winter/w1/snowdet/build_win.sh); a kernel change that is meant to change the
# state changes it too: run the harness on both, then update it here
SELFTEST_1_10000 = 0xD3418E11011E7D95

NATIVE = getattr(fast._ef, "VERSION", 0) >= snow.NATIVE if fast._ef is not None else False


def last_hour(r):
    return r["hourly"]["time"][-1]


@unittest.skipUnless(NATIVE, "needs earthfast 624 (the snow kernel)")
class KernelTest(unittest.TestCase):
    def test_selftest_is_the_platforms_number(self):
        self.assertEqual(fast._ef.snow_selftest(1, 10000), SELFTEST_1_10000)
        self.assertNotEqual(fast._ef.snow_selftest(2, 1000), fast._ef.snow_selftest(1, 1000))

    def test_conditions_and_regimes(self):
        names = fast._ef.snow_conditions()
        self.assertEqual(names[0], "dry")
        self.assertIn("black_ice", names)
        self.assertEqual(len(names), 13)
        self.assertEqual([fast._ef.snow_regime(k) for k in (1, 15, 25, 26, 27, 30)], [0, 1, 2, 3, 4, 4])

    def test_band_read(self):
        bands = [[100 * b, 10 * b, 20 * b, 5 * b, 3] for b in range(8)]
        self.assertEqual(fast._ef.snow_band_depth(bands, 0, -1), 20)       # the cell's own band (index 2)
        self.assertEqual(fast._ef.snow_band_depth(bands, 0, 1), 40)
        self.assertEqual(fast._ef.snow_band_depth(bands, 0, 0), 30)
        with self.assertRaises(ValueError):
            fast._ef.snow_band_depth(bands[:7], 0, 0)


class RecordsTest(unittest.TestCase):
    def test_units_and_layout(self):
        r = RESPONSES["minneapolis"]
        recs, times, z, off = snow.records(r)
        self.assertEqual(recs.shape, (264, snow.NF))
        self.assertEqual(recs.dtype, np.int32)
        self.assertEqual((z, off, len(times)), (257, 0, 264))
        h = r["hourly"]
        self.assertEqual(recs[5, snow.F["F_T2M"]], round(h["temperature_2m"][5] * 100))
        self.assertEqual(recs[5, snow.F["F_WIND"]], round(h["wind_speed_10m"][5] * 10))
        self.assertEqual(recs[5, snow.F["F_DEPTH"]], 0)
        self.assertEqual(recs[5, snow.F["F_HOUR"]], 5)
        self.assertEqual(recs[0, snow.F["F_MONTH"]], 11)

    def test_missing_fields(self):
        recs, times, z, _ = snow.records(RESPONSES["davos"])
        self.assertEqual(z, 1560)
        self.assertTrue((recs[:, snow.F["F_TD"]] == snow.MISSING).all())     # Davos carries no dew point
        self.assertTrue((recs[:, snow.F["F_T2M"]] != snow.MISSING).all())
        self.assertEqual(snow.records({"hourly": {}})[0].shape, (0, snow.NF))

    def test_hour_index(self):
        times = [3600 * k for k in range(10, 20)]
        self.assertEqual(snow.hour_index(times, 3600 * 12 + 1800), 2)
        self.assertEqual(snow.hour_index(times, 0), 0)
        self.assertEqual(snow.hour_index(times, 3600 * 99), 9)


@unittest.skipUnless(NATIVE, "needs earthfast 624 (the snow kernel)")
class MessageTest(unittest.TestCase):
    def msg(self, name, dz=0, regime=2, upto=None):
        r = RESPONSES[name]
        recs, times, z, _ = snow.records(r)
        i = len(times) - 25 if upto is None else upto          # the window's last past hour (the day ahead after it)
        return snow.message(recs, i, times[i], r["latitude"], r["longitude"], z + dz, z, regime)

    def test_layout(self):
        m = self.msg("minneapolis")
        self.assertEqual((m["type"], m["v"], m["src"], m["credit"]), ("snowstate", 1, "real", weather.CREDIT))
        self.assertEqual((len(m["ground"]), len(m["roads"]), len(m["bridges"]), len(m["bands"])), (7, 5, 5, 8))
        self.assertTrue(all(len(b) == 5 for b in m["bands"]))
        self.assertEqual(len(m["lake"]), 3)
        names = fast._ef.snow_conditions()
        for row in m["roads"] + m["bridges"]:
            self.assertEqual(len(row), 7)
            self.assertIn(row[0], names)
            self.assertIn(row[3], names)
        self.assertEqual(json.loads(json.dumps(m)), m)

    def test_mild_dry_city_has_no_snow(self):
        m = self.msg("minneapolis")
        self.assertEqual(m["ground"][0], 0)
        self.assertTrue(all(r[0] in ("dry", "damp", "wet") for r in m["roads"]), m["roads"])

    def test_alpine_snowfall(self):
        # Davos (1,560 m): the model's 0 -> 19 cm over the last three days; the kernel's own pack
        m = self.msg("davos", upto=263)
        depth = m["ground"][0]
        self.assertTrue(40 <= depth <= 400, m["ground"])
        self.assertGreaterEqual(m["ground"][1], 3)                 # D3+: everything white
        bands = m["bands"]
        self.assertEqual([b[0] for b in bands], [1560 + dz for dz in (-300, -150, 0, 150, 300, 600, 1000, 1500)])
        self.assertLessEqual(bands[0][1], bands[7][1])             # more snow higher up
        # 600 m lower the same storm leaves less
        self.assertLess(self.msg("davos", dz=-600, upto=263)["ground"][0], depth)
        # roads: something on the minor ones, the deck no warmer than the road
        self.assertTrue(any(r[0] not in ("dry", "damp") for r in m["roads"]), m["roads"])
        for road, deck in zip(m["roads"], m["bridges"]):
            self.assertLessEqual(deck[2], road[2])

    def test_deterministic(self):
        self.assertEqual(self.msg("tromso"), self.msg("tromso"))

    def test_presets(self):
        light = snow.preset_message("light", 45.0, -93.0)
        self.assertEqual((light["src"], light["credit"], light["lat"]), ("light", "", 45.0))
        self.assertGreaterEqual(light["ground"][1], 2)
        self.assertEqual(light["roads"][0][0], "dry")              # the salted motorway clears
        self.assertIn(light["roads"][3][0], ("dusting", "loose", "packed", "slush"))
        ice = snow.preset_message("ice", 45.0, -93.0)
        self.assertTrue(all(r[0] in ICY for r in ice["roads"] + ice["bridges"]), ice["roads"])
        self.assertEqual(ice, snow.preset_message("ice", 45.0, -93.0))

    def test_regime_from_climate(self):
        self.assertEqual(snow.regime_at(44.97, -93.26), 2)        # Minneapolis-St Paul: Dfa
        self.assertEqual(snow.regime_at(1.35, 103.82), 0)         # Singapore: Af


class FakeCover:
    """W2's snowcover interface (its branch claude-winter-w2: set_provider, Field, Table) without its renders."""

    class Field:
        def __init__(self, res=0.25, cells=None, uniform=None):
            self.res, self.cells, self.uniform = res, dict(cells or {}), uniform

    def Table(self, rows, road, ice=0):
        return ("Table", rows, road, ice)

    def __init__(self):
        self.provider = None

    def set_provider(self, fn):
        self.provider = fn


@unittest.skipUnless(NATIVE, "needs earthfast 624 (the snow kernel)")
class W2InterfaceTest(unittest.TestCase):
    """The band table W2's snow on the photos reads (snowcover.set_provider: Field(uniform=Table(rows, road))), its
    quantization (only a bin or an age class moving changes it), the change hook (Server.snow_changed + 'snow'), the
    lake ice for W2's frozen lakes."""

    class Server:
        def __init__(self):
            self.changes, self.connections = 0, []

        def snow_changed(self):
            self.changes += 1

    class Conn:
        def __init__(self, s):
            self.s, self.sent = s, []

        def send(self, m):
            self.sent.append(m)

    def setUp(self):
        self.cover = FakeCover()
        self.svc = snow.Service(fetch=FakeHourly(), mode="light", cover=self.cover)
        self.server = self.Server()
        self.conn = self.Conn(self.server)
        self.server.connections.append(self.conn)

    def state(self):
        raw = {}
        self.svc.get(45.0, -93.0, 200, -1, raw)
        return raw

    def test_provider_field(self):
        self.assertIs(self.cover.provider.__self__, self.svc)
        self.assertIsNone(self.cover.provider(44, -94, 46, -92))      # no state yet
        self.assertTrue(self.svc.commit(self.conn, self.state()))
        f = self.cover.provider(44, -94, 46, -92)
        _, rows, road, _ice = f.uniform
        self.assertEqual(len(rows), 8)
        self.assertEqual([r[0] for r in rows], sorted(r[0] for r in rows))     # lowest first
        for z, cover, depth, age in rows:
            self.assertTrue(0 <= cover <= 8 and 0 <= depth <= 6 and 0 <= age <= 3, rows)
        cell = [r for r in rows if r[0] == 200][0]
        self.assertGreaterEqual(cell[2], 2)                 # the light preset: D2 at the cell
        self.assertEqual(len(road), 5)
        self.assertEqual(road[0], 0)                        # the salted motorway keeps none of it
        self.assertGreater(road[4], 0)                      # the service road keeps loose snow
        self.assertEqual((self.server.changes, self.conn.sent[-1]["type"]), (1, "snow"))
        self.assertEqual(set(self.conn.sent[-1]["snow"]), {"mode", "cover", "depth"})

    def test_unchanged_table_no_rerender(self):
        self.svc.commit(self.conn, self.state())
        self.assertFalse(self.svc.commit(self.conn, self.state()))
        self.assertEqual((self.server.changes, len(self.conn.sent)), (1, 1))

    def test_bins_hold(self):
        raw = self.state()
        rows, road, bins = snow.w2_table(raw, [-1] * 8)
        held, _, _ = snow.w2_table(raw, [6] * 8)
        self.assertTrue(all(h[2] >= r[2] for h, r in zip(held, rows)))
        self.assertEqual(snow.w2_table(raw, bins)[0], rows)

    def test_no_snowcover_no_hook(self):
        svc = snow.Service(fetch=FakeHourly(), mode="light", cover=None)
        if svc.cover is None:                           # (W2's module absent, or not in its real mode)
            self.assertFalse(svc.commit(self.conn, self.state()))

    def test_frozen_water_share(self):
        self.svc.commit(self.conn, self.state())
        t = self.cover.provider(44, -94, 46, -92).uniform
        self.assertEqual(len(t), 4)                     # Table(rows, road, ice)
        self.assertTrue(0 <= t[3] <= 8 and t[3] % 2 == 0)
        self.assertEqual(fast._ef.snow_lake_share(200, -1), 8)
        self.assertEqual(fast._ef.snow_lake_share(55, 2), 2)       # hysteresis

    def test_lake(self):
        ice = snow.preset_message("ice", 45.0, -93.0)["lake"]
        self.assertGreater(ice[0], 0)                   # freezing degree-days from the cold hours
        self.assertEqual(ice[1], 27 * math.isqrt(round(ice[0] * 100)) // 10)      # 2.7 sqrt(FDD) cm
        self.assertGreaterEqual(ice[2], 0)


class FakeHourly:
    def __init__(self, name="davos", fail=False):
        self.calls, self.name, self.fail = [], name, fail

    def __call__(self, lat, lon, past=240, ahead=6):
        self.calls.append((lat, lon))
        self.window = (past, ahead)
        if self.fail:
            raise OSError("no network in tests")
        return RESPONSES[self.name]


@unittest.skipUnless(NATIVE, "needs earthfast 624 (the snow kernel)")
class ServiceTest(unittest.TestCase):
    def test_one_fetch_per_cell_and_hour(self):
        now = [last_hour(RESPONSES["davos"]) - 24 * 3600 + 600]
        f = FakeHourly()
        svc = snow.Service(fetch=f, mode="real", clock=lambda: now[0])
        a = svc.get(46.79, 9.84, 1560)
        b = svc.get(46.81, 9.83, 1600)                 # the same 0.1-degree cell
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(f.calls[0], weather.cell(46.79, 9.84))
        self.assertEqual(a["t"], now[0] // 3600 * 3600)
        self.assertNotEqual(a["elevM"], b["elevM"])
        now[0] += 3600                                 # the next hour: fetched again
        svc.get(46.79, 9.84, 1560)
        self.assertEqual(len(f.calls), 2)

    def test_wet_roads_alone(self):
        # BNE_WET_ROADS without BNE_WINTER: a day's window, the water film only, no snow parts, no W2 hook
        now = [last_hour(RESPONSES["davos"]) - 6 * 3600 + 600]
        f = FakeHourly()
        cover = FakeCover()
        svc = snow.Service(fetch=f, mode="real", clock=lambda: now[0], winter=False, cover=cover)
        self.assertIsNone(svc.cover)
        m = svc.get(46.79, 9.84, 1560)
        self.assertEqual(f.window, snow.WINDOW_WET)
        self.assertFalse(m["winter"])
        self.assertTrue(all(r[0] in ("dry", "damp", "wet") and r[3] in ("dry", "damp", "wet")
                            for r in m["roads"] + m["bridges"]), m["roads"])
        self.assertEqual((m["bands"], m["lake"], m["ground"][0], m["ground"][1]), ([], [], 0, 0))
        w = snow.Service(fetch=f, mode="real", clock=lambda: now[0], winter=True)
        self.assertTrue(w.get(46.79, 9.84, 1560)["winter"])
        self.assertEqual(f.window, snow.WINDOW_WINTER)

    def test_preset_needs_no_network(self):
        f = FakeHourly(fail=True)
        m = snow.Service(fetch=f, mode="ice").get(40.0, -75.0, 10)
        self.assertEqual((m["src"], f.calls), ("ice", []))

    def test_failure_raises(self):
        with self.assertRaises(OSError):
            snow.Service(fetch=FakeHourly(fail=True), mode="real").get(46.79, 9.84, 1560)


class TrackerTest(unittest.TestCase):
    def test_due(self):
        svc = snow.Service(fetch=FakeHourly(), mode="real", clock=lambda: 0)
        t = snow.Tracker()
        self.assertTrue(t.due(svc, 46.8, 9.8, 1500, 7200))
        t.last = (46.8, 9.8, 1500, 2)
        self.assertFalse(t.due(svc, 46.8, 9.8, 1510, 7300))            # same hour, 10 m higher
        self.assertTrue(t.due(svc, 46.8, 9.8, 1530, 7300))             # 30 m higher
        self.assertTrue(t.due(svc, 46.8, 9.8, 1500, 3 * 3600))         # the next hour
        self.assertTrue(t.due(svc, 46.9, 9.8, 1500, 7300))             # 11 km away
        t.inflight = True
        self.assertFalse(t.due(svc, 46.9, 9.8, 1500, 7300))

    def test_nothing_without_the_service(self):
        class Conn:
            s = type("S", (), {"snow": None})()

            def spawn(self, _c):
                raise AssertionError("no work with winter off")
        snow.Tracker().on_pose(Conn(), None, 0.0, 0.0, 0.0)


@unittest.skipUnless(NATIVE, "needs earthfast 624 (the snow kernel)")
class ServerSnowTest(test_server.ServerHarness):
    """earthd: no snowstate without the service (BNE_WINTER off: Server().snow is None), one after the first pose on an
    anchor with it, not again while nothing changed, again on a hello."""

    def anchor(self):
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        self.c.until(lambda m: m["type"] == "hello", timeout=10)
        self.c.send({"type": "prepare_anchor", "requestId": 1, "lat": test_server.LAT, "lon": test_server.LON,
                     "cx": 0, "cy": 0})
        aid = self.c.until(lambda m: m["type"] == "anchor_ready", 120)[0]["anchorId"]
        self.c.send({"type": "commit_anchor", "anchorId": aid})
        return aid

    def test_off_sends_nothing(self):
        self.assertIsNone(self.server.snow)
        aid = self.anchor()
        self.pose(aid, 0, 0)
        self.pose(aid, 10, 0)
        self.assertFalse([x for x in self.c.drain(1.0) if x["type"] == "snowstate"])
        self.assertIsNone(self.conn().snow.msg)

    def test_preset_session(self):
        self.server.snow = snow.Service(mode="ice")
        aid = self.anchor()
        self.pose(aid, 0, 0)
        m, _ = self.c.until(lambda m: m["type"] == "snowstate", 30)
        self.assertEqual(m["src"], "ice")
        self.assertTrue(all(r[0] in ICY for r in m["roads"]))
        self.pose(aid, 10, 0)
        self.assertFalse([x for x in self.c.drain(0.5) if x["type"] == "snowstate"])     # not due again
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        again, _ = self.c.until(lambda m: m["type"] == "snowstate", 10)
        self.assertEqual(again, m)


class OffTest(unittest.TestCase):
    def test_off_by_default(self):
        env = {k: v for k, v in os.environ.items() if k != "BNE_WINTER"}
        code = "import snow; print(snow.ON, snow.MODE)"
        out = subprocess.run([sys.executable, "-c", code], cwd=str(Path(snow.__file__).parent), env=env,
                             capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(out, ["False", "None"])
        out = subprocess.run([sys.executable, "-c", "import snow; print(snow.ON, snow.MODE, snow.WINTER, snow.WET_ROADS)"],
                             cwd=str(Path(snow.__file__).parent), env={**env, "BNE_WET_ROADS": "on"},
                             capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(out, ["True", "real", "False", "True"])
        for value, mode in (("on", "real"), ("ice", "ice"), ("light", "light"), ("off", "None"), ("bogus", "None")):
            out = subprocess.run([sys.executable, "-c", code], cwd=str(Path(snow.__file__).parent),
                                 env={**env, "BNE_WINTER": value}, capture_output=True, text=True,
                                 check=True).stdout.split()
            self.assertEqual(out[1], mode, value)

    def test_not_a_build_switch(self):
        import revisit
        for k in ("BNE_WINTER", "BNE_WET_ROADS"):
            self.assertIn(k, revisit.NOT_SWITCHES)
            self.assertFalse(revisit.keyed_env(k))


if __name__ == "__main__":
    unittest.main()
