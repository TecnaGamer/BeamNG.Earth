"""Real weather (server/weather.py, docs/research/2026-10-10-real-weather.md §4): the Open-Meteo
responses recorded on 2026-10-10 (fixtures/openmeteo_baltimore.json, openmeteo_whitney.json) turned
into 'weather' messages, synthetic weather codes, the request, the cache and the refetch rule, the
in-process server (sent after a pose, re-sent on hello, the credit in hello.attribution, nothing
with --weather off), the mod's mapping of the same messages under luajit (beamngEarth/weather.lua),
and the level objects the mod drives (tools/make_level_assets.py). No network: fetch is injected.
"""
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import modsim  # noqa: E402
import netfetch  # noqa: E402
import test_server  # noqa: E402
import weather  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
REPO = Path(__file__).resolve().parents[2]
LEVEL = REPO / "mod" / "beamng_earth" / "levels" / "beamng_earth"
BALTIMORE = (39.29, -76.53)
WHITNEY = (36.58, -118.29)


def fixture(name):
    return json.loads((FIX / f"openmeteo_{name}.json").read_text())


def synthetic(code=0, interval=900, **cur):
    """An Open-Meteo response with the given current values (the rest dry, clear, calm)."""
    c = {"time": 1791665100, "interval": interval, "temperature_2m": 10.0, "dew_point_2m": 5.0, "is_day": 1,
         "precipitation": 0.0, "rain": 0.0, "showers": 0.0, "snowfall": 0.0, "weather_code": code,
         "cloud_cover": 0, "cloud_cover_low": 0, "cloud_cover_mid": 0, "cloud_cover_high": 0,
         "surface_pressure": 1000.0, "wind_speed_10m": 0.0, "wind_direction_10m": 0, "wind_gusts_10m": 0.0,
         "visibility": 30000.0}
    c.update(cur)
    return {"latitude": 50.0, "longitude": 8.0, "elevation": 100.0, "utc_offset_seconds": 7200,
            "timezone": "Europe/Berlin", "current": c}


class FakeFetch:
    """weather.fetch without the network: the recorded fixture nearest to the point."""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, lat, lon, timeout=weather.TIMEOUT_S):
        self.calls.append((lat, lon))
        if self.fail:
            raise OSError("no network in tests")
        near = min((BALTIMORE, "baltimore"), (WHITNEY, "whitney"),
                   key=lambda p: weather.distance_m(lat, lon, *p[0]))
        return fixture(near[1])


class Clock:
    def __init__(self, t=1791665100.0):
        self.t = t

    def __call__(self):
        return self.t


class DeriveTest(unittest.TestCase):
    def test_baltimore(self):
        """Overcast mid deck with cirrus above, dry, 36 km visibility, a light easterly."""
        m = weather.derive(fixture("baltimore"), *BALTIMORE)
        self.assertEqual((m["type"], m["v"], m["code"], m["lat"], m["lon"]), ("weather", 1, 3, *BALTIMORE))
        self.assertEqual(m["cover"], {"total": 100, "low": 0, "mid": 100, "high": 100})
        self.assertGreaterEqual(m["baseM"], weather.MID_BASE_M)          # mid deck: >= 3 km ASL
        self.assertAlmostEqual(m["baseAglM"], m["baseM"] - m["elevM"])
        self.assertEqual((m["rainMmH"], m["snowCmH"], m["fog"], m["thunder"]), (0, 0, False, False))
        self.assertEqual(m["visM"], 36300)
        self.assertEqual(m["wind"], {"ms": 3.11, "fromDeg": 98, "gustMs": 6.7})
        self.assertEqual(m["aloft"], {"ms": 5.62, "fromDeg": 211})
        self.assertEqual((m["utcOffsetS"], m["tz"]), (-4 * 3600, "America/New_York"))
        self.assertAlmostEqual(m["slTempC"], 20.5 + weather.LAPSE * 33, places=2)
        self.assertEqual(m["credit"], "Weather data by Open-Meteo.com")
        self.assertEqual(m["t"], 1791665100)
        json.dumps(m)                                                   # wire-safe

    def test_whitney(self):
        """Partly cloudy at the summit (grid 4042 m), strong SW wind, 3.5 C."""
        m = weather.derive(fixture("whitney"), *WHITNEY)
        self.assertEqual(m["code"], 1)
        self.assertEqual(m["cover"]["mid"], 43)
        self.assertEqual(m["wind"]["fromDeg"], 232)
        self.assertGreaterEqual(m["wind"]["gustMs"], m["wind"]["ms"])
        self.assertEqual(m["elevM"], 4042)
        self.assertAlmostEqual(m["slTempC"], 3.5 + 0.0065 * 4042, places=2)
        # a mid deck: LCL above the summit (125 m/K * 18.2 K) since that is above 3 km ASL
        self.assertAlmostEqual(m["baseAglM"], 125 * (3.5 + 14.7), places=1)
        self.assertEqual(m["utcOffsetS"], -7 * 3600)

    def test_fog(self):
        m = weather.derive(synthetic(45, visibility=24000))
        self.assertTrue(m["fog"])
        self.assertEqual(m["visM"], weather.FOG_VIS_M)              # fog code: at most 800 m
        self.assertEqual(weather.derive(synthetic(48, visibility=300))["visM"], 300)
        self.assertEqual(weather.derive(synthetic(45, visibility=None))["visM"], weather.FOG_VIS_M)
        self.assertIsNone(weather.derive(synthetic(0, visibility=None))["visM"])

    def test_rain_rate_from_interval(self):
        """15-minute sums -> per hour; showers count as rain; a code with no sum gets its floor."""
        self.assertEqual(weather.derive(synthetic(63, rain=0.5, showers=0.25))["rainMmH"], 3.0)
        self.assertEqual(weather.derive(synthetic(63, interval=3600, rain=0.5))["rainMmH"], 1.0)   # floor 1.0
        self.assertEqual(weather.derive(synthetic(63, interval=3600, rain=2.0))["rainMmH"], 2.0)
        self.assertEqual(weather.derive(synthetic(63))["rainMmH"], 1.0)                            # code only
        self.assertEqual(weather.derive(synthetic(53))["rainMmH"], 0.3)                            # drizzle
        self.assertEqual(weather.derive(synthetic(0, precipitation=0.1))["rainMmH"], 0.4)          # sum, no split
        self.assertEqual(weather.derive(synthetic(2))["rainMmH"], 0)

    def test_snow(self):
        m = weather.derive(synthetic(73, snowfall=0.35, temperature_2m=-3))
        self.assertAlmostEqual(m["snowCmH"], 1.4)
        self.assertEqual(m["rainMmH"], 0)
        self.assertEqual(weather.derive(synthetic(85))["snowCmH"], 0.5)

    def test_thunder(self):
        m = weather.derive(synthetic(95, rain=0.2))
        self.assertTrue(m["thunder"])
        self.assertEqual(m["rainMmH"], 4.0)

    def test_cloud_base(self):
        """Low deck: the LCL above ground (clamped); mid deck: at least 3 km above sea level."""
        self.assertEqual(weather.cloud_base(20, 12, 80, 10, 100), (1100, 1000))
        self.assertEqual(weather.cloud_base(20, 20, 80, 10, 100), (100 + weather.BASE_MIN_AGL, weather.BASE_MIN_AGL))
        self.assertEqual(weather.cloud_base(20, 18, 10, 80, 100), (3000, 2900))
        self.assertEqual(weather.cloud_base(5, -60, 10, 80, 0)[1], weather.BASE_MAX_AGL)

    def test_missing_fields(self):
        m = weather.derive({"current": {}})
        self.assertEqual((m["code"], m["rainMmH"], m["visM"], m["tempC"]), (0, 0, None, 15.0))
        self.assertNotIn("aloft", m)
        self.assertNotIn("utcOffsetS", m)


class RequestTest(unittest.TestCase):
    def test_url(self):
        u = urllib.parse.urlsplit(weather.url(39.29, -76.53))
        self.assertEqual((u.scheme, u.netloc, u.path), ("https", "api.open-meteo.com", "/v1/forecast"))
        q = urllib.parse.parse_qs(u.query)
        self.assertEqual(q["latitude"], ["39.2900"])
        self.assertEqual(q["current"][0].split(","), list(weather.CURRENT))
        self.assertEqual(q["timezone"], ["auto"])                      # utc_offset_seconds
        self.assertEqual(q["wind_speed_unit"], ["ms"])
        self.assertEqual(q["timeformat"], ["unixtime"])
        self.assertNotIn("apikey", q)
        # the recorded fixtures were fetched with this query
        for name in ("baltimore", "whitney"):
            self.assertEqual(set(fixture(name)["current"]) - {"time", "interval"}, set(weather.CURRENT))

    def test_project_user_agent(self):
        """fetch() goes through netfetch as a background request with the project's User-Agent."""
        seen = []

        class Transport:
            def request(self, url, headers, timeout):
                seen.append((url, dict(headers), timeout))
                return 200, {"content-type": "application/json"}, json.dumps(fixture("baltimore")).encode()
        net = netfetch.Fetcher(transport=Transport())
        self.assertEqual(weather.fetch(*BALTIMORE, net=net), fixture("baltimore"))
        url, headers, timeout = seen[0]
        self.assertEqual(url, weather.url(*BALTIMORE))
        self.assertEqual(headers, {"User-Agent": netfetch.USER_AGENT})
        self.assertEqual(timeout, weather.TIMEOUT_S)
        self.assertEqual(net.snapshot()["api.open-meteo.com"]["requests"], 1)

    def test_shared_fetch_per_cell(self):
        """Two connections asking for the same cell at once: one request."""
        import threading
        gate, started = threading.Event(), threading.Event()
        calls = []

        def slow(lat, lon):
            calls.append((lat, lon))
            started.set()
            gate.wait(5)
            return fixture("baltimore")
        svc = weather.Service(fetch=slow)
        out = []
        t1 = threading.Thread(target=lambda: out.append(svc.get(*BALTIMORE)))
        t1.start()
        started.wait(5)
        t2 = threading.Thread(target=lambda: out.append(svc.get(*BALTIMORE)))
        t2.start()
        gate.set()
        t1.join(5)
        t2.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(out), 2)
        self.assertIs(out[0], out[1])
        self.assertEqual(svc.inflight, {})

    def test_cache_and_cells(self):
        f, clock = FakeFetch(), Clock()
        svc = weather.Service(fetch=f, clock=clock)
        a = svc.get(39.291, -76.532)
        b = svc.get(39.31, -76.49)                  # same 0.1 deg cell, same 15-min slot
        self.assertIs(a, b)
        self.assertEqual(f.calls, [(39.3, -76.5)])  # fetched at the cell
        self.assertEqual((a["lat"], a["lon"]), (39.3, -76.5))
        svc.get(39.36, -76.5)                       # the next cell
        self.assertEqual(len(f.calls), 2)
        clock.t += weather.SLOT_S                   # the next slot
        svc.get(39.291, -76.532)
        self.assertEqual(len(f.calls), 3)
        self.assertEqual(weather.cell(-0.04, -179.96), (-0.0, -180.0))

    def test_refetch_rule(self):
        """No weather yet; then every 600 s or after 25 km; one in flight; retry 120 s after a failure."""
        svc, tr = weather.Service(fetch=FakeFetch()), weather.Tracker()
        t0 = 1000.0
        self.assertTrue(tr.due(svc, 39.29, -76.53, t0))
        tr.inflight = True
        self.assertFalse(tr.due(svc, 39.29, -76.53, t0))
        tr.done(svc, 39.29, -76.53, t0, {"type": "weather"})
        self.assertFalse(tr.due(svc, 39.29, -76.53, t0 + 599))
        self.assertTrue(tr.due(svc, 39.29, -76.53, t0 + 600))
        lat24 = 39.29 + 24000 / 111195.0             # 24 km north, 26 km north
        self.assertFalse(tr.due(svc, lat24, -76.53, t0 + 10))
        self.assertTrue(tr.due(svc, 39.29 + 26000 / 111195.0, -76.53, t0 + 10))
        tr.done(svc, 40.0, -76.53, t0 + 20, None)    # a failure: the last weather stays
        self.assertEqual(tr.msg, {"type": "weather"})
        self.assertFalse(tr.due(svc, 41.0, -76.53, t0 + 20 + 119))
        self.assertTrue(tr.due(svc, 41.0, -76.53, t0 + 20 + 120))
        tr.done(svc, 40.0, -76.53, t0 + 200, None, retry_after=300.0)  # the host said 300 s (Retry-After)
        self.assertFalse(tr.due(svc, 41.0, -76.53, t0 + 200 + 299))
        self.assertTrue(tr.due(svc, 41.0, -76.53, t0 + 200 + 300))

    def test_distance(self):
        self.assertAlmostEqual(weather.distance_m(0, 0, 1, 0), 111195, delta=5)
        self.assertAlmostEqual(weather.distance_m(*BALTIMORE, *BALTIMORE), 0)


class ServerWeatherTest(test_server.ServerHarness):
    """earthd with an injected weather service: the message after the first pose, again on a
    hello, the credit in hello.attribution; a failed fetch sends nothing and keeps the connection."""

    def use(self, svc):
        self.server.weather = svc

    def hello(self):
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        return self.c.until(lambda m: m["type"] == "hello", timeout=10)[0]

    def test_sent_after_pose_and_on_hello(self):
        f = FakeFetch()
        self.use(weather.Service(fetch=f))
        h = self.hello()
        self.assertIn(weather.CREDIT, h["attribution"])
        self.pose(1, 0, 0)                          # an unknown anchor: the lat/lon still count
        m, _ = self.c.until(lambda m: m["type"] == "weather", 10)
        self.assertEqual(m, weather.derive(fixture("whitney"), *weather.cell(test_server.LAT, test_server.LON)))
        self.pose(1, 0, 0)
        self.assertFalse([x for x in self.c.drain(0.5) if x["type"] == "weather"])     # not due again
        self.assertEqual(len(f.calls), 1)
        self.hello()
        again, _ = self.c.until(lambda m: m["type"] == "weather", 10)
        self.assertEqual(again, m)

    def test_failure(self):
        self.use(weather.Service(fetch=FakeFetch(fail=True)))
        self.hello()
        self.pose(1, 0, 0)
        self.assertFalse([x for x in self.c.drain(0.5) if x["type"] == "weather"])
        self.hello()                                # the connection is alive
        self.assertEqual(self.conn().weather.msg, None)
        self.assertGreater(self.conn().weather.retry_at, 0)

    def test_off(self):
        h = self.hello()
        self.assertNotIn(weather.CREDIT, h["attribution"])
        self.pose(1, 0, 0)
        self.assertFalse([x for x in self.c.drain(0.5) if x["type"] == "weather"])


def lua_value(v):
    """A Python JSON value as a Lua constructor."""
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(float(v))
    if isinstance(v, str):
        return json.dumps(v)
    return "{" + ", ".join(f"[{json.dumps(k)}] = {lua_value(x)}" for k, x in v.items()) + "}"


@unittest.skipIf(shutil.which("luajit") is None, "luajit not installed")
class ModMappingTest(unittest.TestCase):
    """The messages derive() makes from the fixtures, through beamngEarth/weather.lua targets()."""

    def targets(self, msg):
        lua = (f"package.path = '{modsim.MOD_DIR.parent}/?.lua;' .. package.path\n"
               f"dofile('{modsim.MOD_ROOT.parent / 'tests' / 'stubs.lua'}')\n"
               "local W = require('beamngEarth/weather')\n"
               f"local t = W.targets({lua_value(msg)})\n"
               "local keys = {}\n"
               "for k in pairs(t) do keys[#keys + 1] = k end\n"
               "table.sort(keys)\n"
               "for _, k in ipairs(keys) do print(k, string.format('%.9g', t[k])) end\n")
        out = subprocess.run(["luajit", "-e", lua], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return {k: float(v) for k, v in (ln.split("\t") for ln in out.stdout.splitlines())}

    def test_fixtures(self):
        b = self.targets(weather.derive(fixture("baltimore"), *BALTIMORE))
        self.assertGreaterEqual(b["coverage"], 1.5)                    # overcast: the top of the curve
        self.assertEqual(b["cirrusCoverage"], 1)
        self.assertAlmostEqual(b["cloudAboveKm"], 2.967, places=6)
        self.assertAlmostEqual(b["fogSigma"], 3.912 / 36300, places=12)
        self.assertAlmostEqual(b["tempC"], 20.71, places=6)
        # an east wind (98 deg) blows west: -x, slightly north (+y)
        self.assertAlmostEqual(b["windDirX"], -math.sin(math.radians(98)), places=6)
        self.assertAlmostEqual(b["windDirY"], -math.cos(math.radians(98)), places=6)
        self.assertEqual((b["rainPct"], b["snowPct"]), (0, 0))
        w = self.targets(weather.derive(fixture("whitney"), *WHITNEY))
        self.assertGreater(w["coverage"], 0.9)                         # 43 % partly cloudy
        self.assertLess(w["coverage"], b["coverage"])
        self.assertAlmostEqual(w["cirrusCoverage"], 0.3, places=6)
        self.assertGreater(w["windDirX"], 0)                           # SW wind blows NE
        self.assertGreater(w["windDirY"], 0)
        self.assertAlmostEqual(w["windMs"], 8.81, places=6)
        self.assertAlmostEqual(w["gustMs"], 13.2, places=6)

    def test_precipitation(self):
        r = self.targets(weather.derive(synthetic(63, rain=0.5)))     # 2 mm/h
        self.assertAlmostEqual(r["rainPct"], 0.5, places=6)
        s = self.targets(weather.derive(synthetic(73, snowfall=0.125)))   # 0.5 cm/h
        self.assertAlmostEqual(s["snowPct"], 0.5, places=6)
        f = self.targets(weather.derive(synthetic(45, visibility=24000)))
        self.assertAlmostEqual(f["fogSigma"], 3.912 / weather.FOG_VIS_M, places=9)


class LevelObjectsTest(unittest.TestCase):
    """The level carries the weather objects every official level has, once each, under the names
    the mod uses, and the committed files are what tools/make_level_assets.py writes."""

    def objects(self, root=LEVEL):
        out = []
        for p in sorted((root / "main").rglob("items.level.json")):
            out += [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
        return out

    def test_a_real_day(self):
        tod = [o for o in self.objects() if o["class"] == "TimeOfDay"][0]
        self.assertEqual(tod["dayLength"], 86400, "#791: the engine's 30-minute day ran in Manual")
        line = [ln for ln in (LEVEL / "main/MissionGroup/Sky/items.level.json").read_text().splitlines()
                if '"TimeOfDay"' in ln][0]
        self.assertRegex(line, r'"dayLength"\s*:\s*86400', "core_environment reads it per line (authoredDayLength)")

    def test_classes(self):
        objs = self.objects()
        by_class = {}
        for o in objs:
            by_class.setdefault(o["class"], []).append(o)
        for cls in ("CloudLayer", "ForestWindEmitter", "SFXEmitter", "LevelInfo", "TimeOfDay", "ScatterSky"):
            self.assertEqual(len(by_class.get(cls, [])), 1, cls)
        self.assertEqual(len(by_class["Precipitation"]), 2)
        groups = {o["name"] for o in by_class["SimGroup"]}
        for o in objs:
            if "__parent" in o:
                self.assertIn(o["__parent"], groups, o.get("name"))
        ids = [o["persistentId"] for o in objs if "persistentId" in o]
        self.assertEqual(len(ids), len(set(ids)))
        names = {o.get("name"): o for o in objs}
        lua_sky = (modsim.MOD_DIR / "sky.lua").read_text()
        self.assertIn(f"M.CLOUD_NAME = '{by_class['CloudLayer'][0]['name']}'", lua_sky)
        rain, snow = (modsim.lua_string("weather.lua", "RAIN_NAME"), modsim.lua_string("weather.lua", "SNOW_NAME"))
        self.assertEqual(names[rain]["dataBlock"], "rain_drop")
        self.assertEqual(names[snow]["dataBlock"], "Snow_menu")
        for p in (names[rain], names[snow]):
            self.assertIs(p["followCam"], True)
            self.assertEqual(p["numDrops"], 0)                         # dry until real weather says otherwise
            self.assertNotIn("numOfDrops", p)                          # (BWM_Weather keeps its own rain object)
        rev = by_class["SFXEmitter"][0]
        self.assertTrue(rev["track"].startswith("snapshot:>Reverb_") and rev["is3D"] is False)
        li = by_class["LevelInfo"][0]
        self.assertEqual(li["temperatureCurveC"], [0, 15, 1, 15])     # ISA until real weather
        self.assertEqual(li["gravity"], -9.81000042)

    def test_generator_matches_committed(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "beamng_earth"
            res = subprocess.run([sys.executable, str(REPO / "tools" / "make_level_assets.py"), str(out)],
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(res.returncode, 0, res.stderr)
            for p in sorted(out.rglob("*.json")):
                rel = p.relative_to(out)
                self.assertEqual(p.read_text(), (LEVEL / rel).read_text(), str(rel))


class AgreementTest(unittest.TestCase):
    """The nested fields the mod reads exist in the message (the top level is in
    test_protocol_agreement.SERVER_TO_MOD)."""

    def test_nested_reads(self):
        m = weather.derive(fixture("baltimore"), *BALTIMORE)
        text = (modsim.MOD_DIR / "weather.lua").read_text()
        reads = {}
        for var, field in re.findall(r"\b(cover|wind|aloft|w)\.(\w+)\b", text):
            reads.setdefault({"w": "wind"}.get(var, var), set()).add(field)
        for k, fields in reads.items():
            self.assertLessEqual(fields, set(m[k]), k)
        self.assertEqual(set(m["aloft"]), {"ms", "fromDeg"})


if __name__ == "__main__":
    unittest.main()
