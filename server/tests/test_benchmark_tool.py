"""tools/benchmark.py offline (docs/DEVTOOLS.md "Visual benchmark"): the benchmark set (docs/benchmarks.json
against docs/PLACES.md, its coverage of continents and kinds of place, validation), the plan (places,
views, the fixed time), the readiness and safety rules, a whole run against a fake game (numbers, files,
the order of the restore and that it happens on every failure path), the compare metrics (against a
brute-force SSIM) and report, and the GE chunk under luajit (mod/tests/test_benchmark.lua). Nothing
here talks to the game or the network."""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import benchmark as bm  # noqa: E402

LUAJIT = shutil.which("luajit")


# --- fakes -----------------------------------------------------------------------------------------
class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.on_sleep = None

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)
        if self.on_sleep:
            self.on_sleep(self.t)


def _lua_args(argtext):
    """The JSON arguments of `BNGE_BENCH.fn(<long string>, ...)`."""
    return [json.loads(m.group(2)) for m in re.finditer(r"\[(=*)\[(.*?)\]\1\]", argtext, re.S)]


class FakeGame:
    """BNGE_BENCH as the game answers it: a teleport commits a new anchor after `commit_polls` progress
    polls, the far field is done `load_polls` polls after that; the camera, sky and weather modes and
    the user's position are tracked so the tests can see what the run left behind."""

    def __init__(self, tmp, speed=0.2, commit_polls=2, load_polls=3, never_ready=(), refuse=(), fail=None,
                 anchor_error=(), never_commit=(), lost_shots=()):
        self.tmp = Path(tmp) / "game_shots"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.speed, self.commit_polls, self.load_polls = speed, commit_polls, load_polls
        self.never_ready, self.refuse = set(never_ready), set(refuse)
        self.anchor_error, self.never_commit = set(anchor_error), set(never_commit)   # the server's answer
        self.lost_shots = set(lost_shots)   # screenshot numbers whose file never appears
        self.fail = fail or {}              # fn -> exception to raise on its n-th call: (n, exc)
        self.anchor, self.pending, self.target, self.countdown = 7, None, None, 0
        self.player = {"lat": 39.2834, "lon": -76.5934, "z": 3.0, "speed": speed}
        self.camera, self.sky, self.weather = "game", "real", "real"
        self.far = self.ready_far()
        self.calls, self.n, self.shots, self.chunk_loads = [], {}, 0, 0
        self.parked = False                 # zeroInputs sets the parking brake; a teleport resets the vehicle
        self.lua_reload_at = None           # raise "BNGE_BENCH is nil" once at this call number

    def ready_far(self, imports=1000):
        return {"done": True, "queued": 0, "groups": 0, "transitions": 0, "live": 900, "nearVisual": "mesh",
                "underLevel": -2, "exactLevel": -2, "underOld": False, "imports": imports, "failed": 0,
                "textured": imports, "exactMiss": 5, "exactFrames": 100, "pops": 0, "materials": 900, "covered": 900}

    # MCP
    def lua(self, code, timeout=60):
        if code.startswith("-- BeamNG.Earth visual benchmark"):
            self.chunk_loads += 1
            return "ok"
        m = re.match(r"return BNGE_BENCH\.(\w+)\((.*)\)$", code, re.S)
        assert m, code[:80]
        fn, args = m.group(1), _lua_args(m.group(2))
        self.calls.append((fn, args))
        self.n[fn] = self.n.get(fn, 0) + 1
        if self.lua_reload_at == len(self.calls):
            self.lua_reload_at = None
            raise RuntimeError("lua: [string]:1: attempt to index global 'BNGE_BENCH' (a nil value)")
        f = self.fail.get(fn)
        if f and f[0] == self.n[fn]:
            raise f[1]
        return json.dumps(getattr(self, "do_" + fn)(*args))

    def screenshot(self, jpg=True):
        self.shots += 1
        p = self.tmp / f"shot_{self.shots}.{'jpg' if jpg else 'png'}"
        if self.shots in self.lost_shots:
            return str(p)
        # the image depends on what is in view only, so two runs give identical files
        key = sum(map(ord, json.dumps(self.view_cfg, sort_keys=True))) % 200
        img = np.full((48, 64, 3), key, np.uint8)
        img[10:20, 10:30] = 255 - key
        Image.fromarray(img).save(p)
        return str(p)

    # BNGE_BENCH
    def do_info(self):
        return {"mod": True, "active": True, "net": "connected", "speed": self.speed, "player": dict(self.player),
                "nearVisual": "mesh", "level": "beamng_earth",
                "saved": {"cam": {"free": False, "fov": 75}, "sky": {"mode": "real", "tod": {"time": 0.1}},
                          "weather": {"mode": "real"}, "perfmonLoaded": False}}

    def do_zeroInputs(self):
        self.parked = True
        return {"ok": True}

    def do_teleport(self, cfg):
        if (cfg["lat"], cfg["lon"]) in self.refuse:
            return {"ok": False, "status": "Companion server not connected"}
        self.pending, self.target, self.countdown = "teleport", cfg, self.commit_polls
        return {"ok": True, "anchorBefore": self.anchor}

    def do_progress(self):
        tgt = self.target and (self.target["lat"], self.target["lon"])
        if self.pending and tgt in self.anchor_error:
            self.pending = None                 # anchor_error: the request is dropped, the anchor stays
        elif self.pending and tgt in self.never_commit:
            pass
        elif self.pending:
            self.countdown -= 1
            if self.countdown <= 0:
                self.pending, self.anchor, self.parked = None, self.anchor + 1, False
                self.player.update(lat=self.target["lat"], lon=self.target["lon"])
                self.far = dict(self.ready_far(self.far["imports"]), done=False, queued=40, underLevel=1)
                self.countdown = self.load_polls
        elif not self.far["done"]:
            self.countdown -= 1
            if self.countdown <= 0 and (self.target["lat"], self.target["lon"]) not in self.never_ready:
                self.far = self.ready_far(self.far["imports"] + 300)
                self.far["exactMiss"] = 9
        return {"mod": True, "anchorId": self.anchor, "pending": self.pending, "far": dict(self.far),
                "serverBacklog": 0, "player": dict(self.player), "status": "ok"}

    def do_setTime(self, cfg):
        self.sky, self.time = "manual", cfg
        return {"ok": True, "sky": {"mode": "manual"}}

    def do_setWeather(self, cfg):
        self.weather = "manual"
        return {"ok": True}

    def do_view(self, cfg):
        self.camera, self.view_cfg = "free", cfg
        return {"ok": True, "x": cfg.get("e"), "y": cfg.get("n"), "z": 10.0, "groundFrom": "ray"}

    def do_perfStart(self):
        return {"ok": True}

    def do_perfRead(self):
        return {"ok": True, "fps": 60.0, "p50": 16.5, "p95": 18.0, "p99": 22.0, "maxMs": 30.0, "frames": 300}

    def do_restore(self, saved, parts=None):
        parts = parts or {"cam": True, "sky": True, "weather": True, "perfmon": True}
        out = {"ok": True, "errors": []}
        if parts.get("sky"):
            self.sky = out["sky"] = saved["sky"]["mode"]
        if parts.get("weather"):
            self.weather = out["weather"] = saved["weather"]["mode"]
        if parts.get("cam"):
            self.camera = out["cam"] = "game"
        return out

    def fns(self):
        return [c[0] for c in self.calls]


SET = {"defaults": {"date": "2026-09-22", "hour": 10.0, "weather": "clear",
                    "views": [{"name": "ground", "agl": 1.7, "pitch": -3, "fov": 70},
                              {"name": "aerial", "back": 600, "agl": 250, "pitch": -18, "fov": 70}]},
       "places": [{"id": "a", "name": "A", "lat": 10.0, "lon": 20.0, "heading": 90, "group": "global", "heldout": False},
                  {"id": "b", "name": "B", "lat": -30.0, "lon": 150.0, "group": "global", "heldout": False,
                   "views": [{"name": "photo", "e": 0, "n": -12, "alt": 4416.5, "heading": 170, "pitch": -18, "fov": 65}]},
                  {"id": "h", "name": "H", "lat": 60.0, "lon": 7.0, "group": "global", "heldout": True}]}


def small_plan(which="tuning"):
    return bm.plan(bm.validate_set(json.loads(json.dumps(SET))), which)


# --- the set ---------------------------------------------------------------------------------------
class BenchmarkSetTest(unittest.TestCase):
    def setUp(self):
        self.s = bm.load_set(bm.SET_PATH)
        self.places = {p["id"]: p for p in self.s["places"]}

    def test_places_md_table_is_in_the_set(self):
        """Every place of docs/PLACES.md's first table, at its coordinates."""
        text = (ROOT / "docs" / "PLACES.md").read_text()
        table = text.split("## Photo benchmarks")[0]
        rows = re.findall(r"^\| ([^|]+?) \| (-?[\d.]+), (-?[\d.]+) \|", table, re.M)
        self.assertGreaterEqual(len(rows), 6)
        for name, lat, lon in rows:
            hit = [p for p in self.s["places"] if p.get("group") == "places"
                   and abs(p["lat"] - float(lat)) < 1e-9 and abs(p["lon"] - float(lon)) < 1e-9]
            self.assertEqual(len(hit), 1, f"{name} ({lat}, {lon}) not in docs/benchmarks.json")

    def test_photo_benchmark_camera(self):
        """docs/PLACES.md: 12 m south of the summit at 4416.5 m, heading 170, pitch -18, FOV 65, 10:00 local."""
        p = self.places["whitney_photo_south"]
        self.assertEqual((p["lat"], p["lon"]), (36.5785, -118.2923))
        (v,) = p["views"]
        self.assertEqual((v["e"], v["n"], v["alt"], v["heading"], v["pitch"], v["fov"]), (0, -12, 4416.5, 170, -18, 65))
        t = bm.tod_fields(p, self.s["defaults"])
        self.assertAlmostEqual(t["time"], 0.916667, places=6)            # PLACES.md: time 0.92
        self.assertEqual(t["utcOffset"], -7)
        self.assertIn("wikimedia.org", p["photo"])

    def test_global_set_is_diverse(self):
        g = [p for p in self.s["places"] if p.get("group") == "global"]
        self.assertGreaterEqual(len(g), 18)
        tags = {t for p in g for t in p["tags"]}
        for c in ("europe", "africa", "asia", "oceania", "south-america", "north-america", "antarctica"):
            self.assertIn(c, tags)
        for t in ("data-rich", "data-poor", "flat", "steep", "city", "wilderness", "desert", "water", "ice",
                  "southern-hemisphere"):
            self.assertIn(t, tags)
        held = [p for p in g if p["heldout"]]
        self.assertGreaterEqual(len(held), 4)
        self.assertGreaterEqual(len(g) - len(held), 12)
        # held-out places span several continents too, and none is a tuning place's duplicate
        self.assertGreaterEqual(len({t for p in held for t in p["tags"]} & {"europe", "africa", "asia", "north-america",
                                                                              "antarctica"}), 4)
        coords = [(p["lat"], p["lon"]) for p in g]
        self.assertEqual(len(coords), len(set(coords)))
        # the tuning set alone covers farmland (most of the inhabited land), South Asia and a data-poor city
        tuning = [p for p in g if not p["heldout"]]
        self.assertTrue(any("farmland" in p["tags"] for p in tuning))
        self.assertTrue(any({"city", "data-poor"} <= set(p["tags"]) for p in tuning))
        self.assertTrue(any("south-asia" in p["tags"] for p in tuning))

    def test_every_place_plans(self):
        pl = bm.plan(self.s, "all")
        self.assertEqual(len(pl), len(self.s["places"]))
        for p in pl:
            self.assertTrue(p["views"])
            for v in p["views"]:
                self.assertAlmostEqual(math.hypot(v["dir"]["x"], v["dir"]["y"], v["dir"]["z"]), 1.0, places=9)
        self.assertFalse(any(p["heldout"] for p in bm.plan(self.s, "tuning")))
        self.assertTrue(all(p["heldout"] for p in bm.plan(self.s, "heldout")))

    def test_validation(self):
        def bad(mutate):
            s = json.loads(json.dumps(SET))
            mutate(s)
            with self.assertRaises(bm.SetError):
                bm.validate_set(s)
        bad(lambda s: s["places"].append(dict(s["places"][0])))                         # duplicate id
        bad(lambda s: s["places"][1]["views"][0].update(agl=2))                          # alt and agl
        bad(lambda s: s["places"][1]["views"][0].pop("alt"))                             # neither
        bad(lambda s: s["places"][1]["views"][0].update(pitch=-90))
        bad(lambda s: s["places"][1]["views"][0].update(lat=1.0))                        # lat without lon
        bad(lambda s: s["places"][0].update(lat=95))
        bad(lambda s: s["places"][0].update(date="2026-13-01"))
        bad(lambda s: s["places"][0].update(weather="storm"))
        bad(lambda s: s["places"][0].update(id="Bad Id"))
        bad(lambda s: s["defaults"].update(date=None))
        bm.validate_set(json.loads(json.dumps(SET)))

    def test_select(self):
        s = bm.validate_set(json.loads(json.dumps(SET)))
        self.assertEqual([p["id"] for p in bm.select_places(s)], ["a", "b"])
        self.assertEqual([p["id"] for p in bm.select_places(s, "heldout")], ["h"])
        self.assertEqual([p["id"] for p in bm.select_places(s, "all")], ["a", "b", "h"])
        self.assertEqual([p["id"] for p in bm.select_places(s, "tuning", ["h", "a"])], ["a", "h"])   # set order
        with self.assertRaises(bm.SetError):
            bm.select_places(s, ids=["nope"])


class PlanTest(unittest.TestCase):
    def test_views(self):
        a, b = small_plan()
        g, air = a["views"]
        self.assertEqual((g["e"], g["n"], g["agl"], g["heading"]), (0.0, 0.0, 1.7, 90.0))   # the place's heading
        self.assertAlmostEqual(air["e"], -600.0, places=6)          # 600 m behind, heading east
        self.assertAlmostEqual(air["n"], 0.0, places=6)
        self.assertAlmostEqual(air["dir"]["x"], math.cos(math.radians(18)), places=12)
        self.assertAlmostEqual(air["dir"]["z"], -math.sin(math.radians(18)), places=12)
        (ph,) = b["views"]
        self.assertEqual((ph["e"], ph["n"], ph["alt"], ph["fov"]), (0.0, -12.0, 4416.5, 65.0))
        self.assertNotIn("agl", ph)
        v = bm.resolve_view({"heading": 0}, {"name": "x", "lat": 1.5, "lon": 2.5, "alt": 100})
        self.assertEqual((v["lat"], v["lon"], v["alt"]), (1.5, 2.5, 100.0))
        self.assertNotIn("e", v)

    def test_camera_dir(self):
        d = bm.camera_dir(0, 0)
        self.assertAlmostEqual(d["y"], 1.0)
        d = bm.camera_dir(90, 0)
        self.assertAlmostEqual(d["x"], 1.0)
        self.assertAlmostEqual(d["y"], 0.0)
        d = bm.camera_dir(180, -90)
        self.assertAlmostEqual(d["z"], -1.0)

    def test_time(self):
        d = SET["defaults"]
        t = bm.tod_fields({"lon": 150.0}, d)
        self.assertEqual((t["year"], t["month"], t["day"], t["utcOffset"]), (2026, 9, 22, 10.0))   # solar: lon / 15
        self.assertAlmostEqual(t["time"], (10 / 24 - 0.5) % 1, places=6)
        t = bm.tod_fields({"lon": -76.5934, "hour": 12.0, "utcOffset": -4, "date": "2026-06-21"}, d)
        self.assertEqual((t["time"], t["utcOffset"], t["month"]), (0.0, -4.0, 6))              # noon = 0
        # the instant: UT = local - offset; solar 10:00 at lon 150 is 00:00 UT
        self.assertAlmostEqual((10 - bm.tod_fields({"lon": 150.0}, d)["utcOffset"]) % 24, 0.0)


class RulesTest(unittest.TestCase):
    def prog(self, **far):
        f = {"done": True, "queued": 0, "groups": 0, "transitions": 0, "nearVisual": "mesh", "underLevel": -2,
             "exactLevel": -2, "underOld": False}
        f.update(far)
        return {"mod": True, "anchorId": 8, "pending": None, "far": f, "serverBacklog": 0}

    def test_readiness_waits_for_roads(self):
        p = self.prog()
        p["roads"] = {"enabled": True, "roads": 10, "queued": 4, "deleting": 0, "quietS": 0.1}
        self.assertTrue(any(r.startswith("roads: 4 to make") for r in bm.readiness(p, 7)))
        p["roads"].update(queued=0, quietS=1.0)
        self.assertIn("roads: still arriving / settling", bm.readiness(p, 7))
        p["roads"]["quietS"] = bm.ROADS_QUIET_S + 1
        self.assertEqual(bm.readiness(p, 7), [])
        p["roads"] = {"enabled": False, "queued": 9, "quietS": 0.0}
        self.assertEqual(bm.readiness(p, 7), [], "roads off: nothing to wait for")

    def test_readiness(self):
        self.assertEqual(bm.readiness(self.prog(), 7), [])
        pending = self.prog()
        pending["messages"] = {"queued": 4}
        self.assertIn("incoming messages: 4 waiting", bm.readiness(pending, 7))
        pending["messages"]["queued"] = 0
        self.assertEqual(bm.readiness(pending, 7), [])
        self.assertIn("teleport not committed yet", bm.readiness(self.prog(), 8))
        self.assertEqual(bm.readiness(self.prog(), 8, need_anchor=False), [])
        p = self.prog()
        p["pending"] = "teleport"
        self.assertIn("anchor request pending (teleport)", bm.readiness(p, 7))
        self.assertTrue(bm.readiness(self.prog(done=False), 7))
        self.assertIn("far field: 3 tiles queued", bm.readiness(self.prog(queued=3), 7))
        self.assertTrue(bm.readiness(self.prog(groups=1), 7))
        self.assertTrue(bm.readiness(self.prog(transitions=2), 7))
        self.assertTrue(bm.readiness(self.prog(underLevel=0), 7))
        self.assertTrue(bm.readiness(self.prog(underOld=True), 7))
        self.assertTrue(bm.readiness(self.prog(underLevel=None), 7))
        self.assertEqual(bm.readiness(self.prog(underLevel=0), 7, exact=False), [])
        self.assertEqual(bm.readiness(self.prog(underLevel=None, nearVisual="terrain"), 7), [])   # no near tiles
        p = self.prog()
        p["serverBacklog"] = 12           # stale without earthd --status-interval: only with --wait-backlog
        self.assertEqual(bm.readiness(p, 7), [])
        self.assertIn("server backlog 12", bm.readiness(p, 7, backlog=True))
        self.assertEqual(bm.readiness({"mod": False}, 7), ["the BeamNG.Earth mod is not running"])

    def test_check_info(self):
        ok = FakeGame(tempfile.mkdtemp()).do_info()
        self.assertEqual(bm.check_info(ok), [])
        fast = dict(ok, speed=3.0)
        self.assertIn("moves", " ".join(bm.check_info(fast)))
        self.assertEqual(bm.check_info(fast, force=True), [])
        self.assertTrue(bm.check_info(dict(ok, net="connecting")))
        self.assertTrue(bm.check_info(dict(ok, active=False)))
        self.assertTrue(bm.check_info(dict(ok, pending="rebase")))
        self.assertTrue(bm.check_info(dict(ok, speed=None)))
        self.assertTrue(bm.check_info(dict(ok, player=None)))
        self.assertTrue(bm.check_info({"mod": False, "speed": 0}))

    def test_run_dir(self):
        import datetime
        with tempfile.TemporaryDirectory() as tmp:
            d = datetime.date(2026, 10, 11)
            p = bm.run_dir(tmp, "before colour/seam", d)
            self.assertEqual(p.name, "2026-10-11-before-colour-seam")
            p.mkdir()
            self.assertEqual(bm.run_dir(tmp, "before colour/seam", d).name, "2026-10-11-before-colour-seam-2")

    def test_lua_long(self):
        for v in ({"x": "abc"}, {"x": "a]]b"}, {"x": "]=]]", "y": ["]==]"]}):
            text = json.dumps(v)
            lit = bm.lua_long(text)
            eq = lit[1:lit.index("[", 1)]
            self.assertNotIn("]" + eq + "]", text)                  # the closing bracket cannot occur inside
            self.assertEqual(lit, "[" + eq + "[" + text + "]" + eq + "]")
            self.assertEqual(_lua_args(lit), [v])

    def test_python_calls_only_what_the_chunk_defines(self):
        lua = (ROOT / "tools" / "benchmark.lua").read_text()
        defined = set(re.findall(r"^function B\.(\w+)\(", lua, re.M))
        called = set(re.findall(r'self\.call\("(\w+)"', (ROOT / "tools" / "benchmark.py").read_text()))
        self.assertTrue(called)
        self.assertLessEqual(called, defined)
        self.assertTrue(lua.startswith("-- BeamNG.Earth visual benchmark"))


# --- a run against the fake game -------------------------------------------------------------------
class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.clock = FakeClock()
        self.logs = []

    def run_bench(self, game, places=None, **kw):
        opts = bm.options(**kw)
        out = Path(self.tmp) / "run"
        return bm.run(game, places or small_plan(), out, opts, clock=self.clock, log=self.logs.append,
                      meta={"label": "t"}), out

    def assert_restored(self, game, home=(39.2834, -76.5934)):
        fns = game.fns()
        restores = [i for i, c in enumerate(game.calls) if c[0] == "restore"]
        self.assertEqual(len(restores), 2, fns)
        first, last = game.calls[restores[0]][1], game.calls[restores[1]][1]
        self.assertEqual(first[1], {"weather": True, "sky": True, "perfmon": True})
        self.assertEqual(last[1], {"cam": True})
        self.assertEqual(restores[1], len(game.calls) - 1, "the camera comes back last")
        # the player goes home between the two, then the inputs are zeroed
        tps = [c[1][0] for c in game.calls[restores[0]:restores[1]] if c[0] == "teleport"]
        self.assertEqual(tps, [{"lat": home[0], "lon": home[1]}])
        self.assertIn("zeroInputs", fns[restores[0]:restores[1]])
        self.assertEqual((game.camera, game.sky, game.weather), ("game", "real", "real"))

    def test_run(self):
        game = FakeGame(self.tmp)
        rec, out = self.run_bench(game)
        self.assert_restored(game)
        self.assertTrue(rec["restore"]["ok"], rec["restore"])
        self.assertEqual([p["id"] for p in rec["places"]], ["a", "b"])
        a, b = rec["places"]
        self.assertTrue(a["ok"] and b["ok"])
        # load times: polls 1 s apart, commit after 2 polls, ready after 3 more and 2 stable polls
        self.assertEqual(a["load"]["anchor_s"], 1.0)
        self.assertEqual(a["load"]["farDone_s"], 4.0)
        self.assertEqual(a["load"]["exact_s"], 4.0)
        self.assertEqual(a["load"]["ready_s"], 4.0)
        self.assertFalse(a["load"]["timedOut"])
        self.assertEqual(a["tiles"]["imports"], 300)
        self.assertEqual(a["tiles"]["live"], 900)
        self.assertEqual(a["exact"]["missLoad"], 4)            # 5 at the commit, 9 at ready
        self.assertEqual(a["exact"]["missAfter"], 0)
        # files: one image per view, run.json and state.json
        self.assertEqual([v["file"] for v in a["views"]], ["a__ground.jpg", "a__aerial.jpg"])
        self.assertEqual([v["file"] for v in b["views"]], ["b__photo.jpg"])
        for v in a["views"] + b["views"]:
            self.assertTrue((out / v["file"]).is_file())
            self.assertEqual(v["perf"]["fps"], 60.0)
        on_disk = json.loads((out / "run.json").read_text())
        self.assertEqual(on_disk["places"][1]["views"][0]["camera"]["alt"], 4416.5)
        state = json.loads((out / "state.json").read_text())
        self.assertEqual(state["home"], {"lat": 39.2834, "lon": -76.5934})
        self.assertEqual(state["saved"]["sky"]["mode"], "real")
        # each place: teleport, then its time and weather, then the views
        fns = game.fns()
        self.assertEqual(fns[:2], ["info", "zeroInputs"])
        i = fns.index("teleport")
        self.assertLess(i, fns.index("setTime"))
        self.assertLess(fns.index("setTime"), fns.index("view"))
        times = [c[1][0] for c in game.calls if c[0] == "setTime"]
        self.assertEqual(times[0]["utcOffset"], round(20.0 / 15, 4))
        self.assertEqual(times[1]["utcOffset"], 10.0)
        self.assertEqual(fns.count("setWeather"), 2)
        self.assertEqual(game.chunk_loads, 1)

    def test_polls_wait_between(self):
        game = FakeGame(self.tmp)
        stamps = []
        orig = game.do_progress

        def stamped():
            stamps.append(self.clock.t)
            return orig()
        game.do_progress = stamped
        self.run_bench(game, small_plan()[:1])
        loop = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(loop)
        # within a wait loop polls are a poll interval apart; only the place's own first reads follow at once
        self.assertGreaterEqual(sum(1 for d in loop if d >= 1.0), len(loop) - 4)

    def test_refuses_while_moving(self):
        game = FakeGame(self.tmp, speed=3.0)
        with self.assertRaises(bm.BenchError) as e:
            self.run_bench(game)
        self.assertIn("moves", str(e.exception))
        self.assertEqual(game.fns(), ["info"])                 # nothing changed, nothing to restore
        self.assertFalse((Path(self.tmp) / "run").exists())
        game = FakeGame(self.tmp, speed=3.0)
        rec, _ = self.run_bench(game, force=True)
        self.assertTrue(rec["places"][0]["ok"])
        self.assert_restored(game)

    def test_mcp_failure_restores(self):
        game = FakeGame(self.tmp, fail={"view": (2, RuntimeError("MCP timeout"))})
        with self.assertRaises(RuntimeError):
            self.run_bench(game)
        self.assert_restored(game)
        rec = json.loads((Path(self.tmp) / "run" / "run.json").read_text())
        self.assertTrue(rec["restore"]["ok"])
        self.assertIn("finished", rec)

    def test_interrupt_restores(self):
        game = FakeGame(self.tmp)

        def interrupt(t):
            if game.n.get("progress", 0) == 4:
                self.clock.on_sleep = None
                raise KeyboardInterrupt
        self.clock.on_sleep = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_bench(game)
        self.assert_restored(game)

    def test_restore_goes_on_after_failures(self):
        game = FakeGame(self.tmp, refuse={(39.2834, -76.5934)},             # home refused
                        fail={"restore": (1, RuntimeError("weather restore died"))})
        rec, _ = self.run_bench(game)
        r = rec["restore"]
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["errors"]), 2, r["errors"])
        self.assertIn("weather restore died", r["errors"][0])
        self.assertIn("home", r["errors"][1])
        self.assertEqual(game.calls[-1][0], "restore")
        self.assertEqual(game.calls[-1][1][1], {"cam": True})
        self.assertEqual(game.camera, "game")
        self.assertIn("RESTORE PROBLEMS", self.logs[-1])

    def test_home_timeout(self):
        game = FakeGame(self.tmp, commit_polls=4)
        rec, _ = self.run_bench(game, small_plan()[:1], home_timeout=0.5)
        # the home teleport commits after 4 polls (3 s): with 0.5 s it gives up, the camera still returns
        self.assertIn("home", " ".join(rec["restore"]["errors"]))
        self.assertEqual(game.camera, "game")

    def test_place_timeout_and_refused_teleport(self):
        game = FakeGame(self.tmp, never_ready={(10.0, 20.0)}, refuse={(-30.0, 150.0)})
        rec, out = self.run_bench(game, timeout=20)
        a, b = rec["places"]
        self.assertTrue(a["load"]["timedOut"])
        self.assertFalse(a["ok"])
        self.assertIn("far field: waiting for far_done", a["load"]["reasons"])
        self.assertEqual(len(a["views"]), 2)                   # shots taken anyway
        self.assertTrue(all((out / v["file"]).is_file() for v in a["views"]))
        self.assertIn("teleport refused", b["error"])
        self.assertFalse(b["ok"])
        self.assert_restored(game)

    def test_lua_reload_mid_run(self):
        game = FakeGame(self.tmp)
        game.lua_reload_at = 6
        rec, _ = self.run_bench(game)
        self.assertEqual(game.chunk_loads, 2)
        self.assertTrue(all(p["ok"] for p in rec["places"]))

    def test_weather_keep_and_png(self):
        game = FakeGame(self.tmp)
        rec, out = self.run_bench(game, weather="keep", png=True)
        self.assertNotIn("setWeather", game.fns())
        self.assertTrue(rec["places"][0]["views"][0]["file"].endswith(".png"))

    def test_region_wait(self):
        log = Path(self.tmp) / "earthd.log"
        log.write_text("old colormatch region 1 line\n")
        game = FakeGame(self.tmp)
        lines = iter([2, 3])

        def write_line(t):                  # region lines while the first place loads
            if game.n.get("progress", 0) in (2, 3) and next(lines, None):
                with open(log, "a") as f:
                    f.write("INFO colormatch region (12, 3): 2 seam(s) in reach\nINFO other\n")
        self.clock.on_sleep = write_line
        rec, _ = self.run_bench(game, small_plan()[:1], server_log=str(log), region_quiet=5)
        self.assertGreater(rec["places"][0]["load"]["regionsWait_s"], 0.0)
        self.assertLessEqual(rec["places"][0]["load"]["regionsWait_s"], 5.0)


    def test_failed_teleport_takes_no_shots(self):
        """The server answers the teleport with anchor_error (the anchor stays where it was): the place
        fails at once, with no shots of the previous place filed under its id, and the run goes on."""
        game = FakeGame(self.tmp, anchor_error={(10.0, 20.0)})
        rec, out = self.run_bench(game, timeout=300)
        a, b = rec["places"]
        self.assertFalse(a["ok"])
        self.assertIn("teleport failed", a["error"])
        self.assertEqual(a.get("views", []), [])
        self.assertLess(a["wall_s"], 10)                       # not the whole timeout
        self.assertFalse(any(f.name.startswith("a__") for f in out.iterdir()))
        self.assertTrue(b["ok"])
        self.assert_restored(game)

    def test_uncommitted_teleport_takes_no_shots(self):
        """The anchor never commits (server stuck): after the timeout no shots, since the camera would
        show the previous place."""
        game = FakeGame(self.tmp, never_commit={(10.0, 20.0)})
        rec, out = self.run_bench(game, timeout=20)
        a, b = rec["places"]
        self.assertFalse(a["ok"])
        self.assertIn("not committed", a["error"])
        self.assertEqual(a.get("views", []), [])
        self.assertTrue(a["load"]["timedOut"])
        self.assertFalse(any(f.name.startswith("a__") for f in out.iterdir()))
        self.assertTrue(b["ok"])

    def test_failed_home_teleport_fails_fast(self):
        game = FakeGame(self.tmp, anchor_error={(39.2834, -76.5934)})
        t0 = self.clock.t
        rec, _ = self.run_bench(game, small_plan()[:1], home_timeout=120)
        self.assertIn("home", " ".join(rec["restore"]["errors"]))
        self.assertLess(self.clock.t - t0, 100)
        self.assertEqual(game.camera, "game")

    def test_user_drives_mid_run(self):
        """The user takes the car back during a run: the run stops before the next teleport, puts
        back the camera, sky and weather, and neither teleports the car home nor touches its inputs."""
        game = FakeGame(self.tmp)
        orig = game.do_perfRead

        def drive_off():
            if game.n["perfRead"] == 2:                    # the last view of the first place
                game.player["speed"] = 8.0
            return orig()
        game.do_perfRead = drive_off
        rec, _ = self.run_bench(game)
        self.assertEqual([p["id"] for p in rec["places"]], ["a"])
        self.assertIn("driv", rec["aborted"])
        tps = [c[1][0] for c in game.calls if c[0] == "teleport"]
        self.assertEqual(tps, [{"lat": 10.0, "lon": 20.0}])   # no second place, no home
        last_zero = max(i for i, c in enumerate(game.calls) if c[0] == "zeroInputs")
        first_restore = min(i for i, c in enumerate(game.calls) if c[0] == "restore")
        self.assertLess(last_zero, first_restore)
        self.assertEqual((game.camera, game.sky, game.weather), ("game", "real", "real"))
        self.assertTrue(rec["restore"]["ok"], rec["restore"])
        self.assertIn("driving", rec["restore"]["home"])
        # ... also when they drive off during the last place: no teleport home
        game = FakeGame(self.tmp)
        orig2 = game.do_perfRead
        game.do_perfRead = lambda: (game.player.update(speed=8.0) if game.n["perfRead"] == 3 else None) or orig2()
        rec = bm.run(game, small_plan(), Path(self.tmp) / "last", bm.options(), clock=self.clock, log=self.logs.append)
        self.assertEqual(len(rec["places"]), 2)
        self.assertEqual(len([c for c in game.calls if c[0] == "teleport"]), 2)
        self.assertIn("driving", rec["restore"]["home"])
        self.assertEqual(game.camera, "game")
        # --force: the user said the car may move; the run goes on
        game = FakeGame(self.tmp)
        game.player["speed"] = 8.0
        rec = bm.run(game, small_plan(), Path(self.tmp) / "forced", bm.options(force=True), clock=self.clock,
                     log=self.logs.append)
        self.assertEqual(len(rec["places"]), 2)
        self.assertNotIn("aborted", rec)

    def test_reparked_after_each_teleport(self):
        """A teleport resets the vehicle (spawn.safeTeleport): the inputs are zeroed and the parking
        brake set again before the shots."""
        game = FakeGame(self.tmp)
        parked_at_shot = []
        orig = game.screenshot

        def shot(jpg=True):
            parked_at_shot.append(game.parked)
            return orig(jpg)
        game.screenshot = shot
        self.run_bench(game)
        self.assertEqual(parked_at_shot, [True, True, True])

    def test_mcp_timeout_is_retried(self):
        """One MCP call timing out (the game stalls, e.g. compiling shaders) does not end the run:
        every BNGE_BENCH call is idempotent and is sent again."""
        game = FakeGame(self.tmp, fail={"progress": (5, bm.McpError("timed out", timeout=True)),
                                        "view": (2, bm.McpError("timed out", timeout=True))})
        rec, _ = self.run_bench(game)
        self.assertTrue(all(p["ok"] for p in rec["places"]), rec["places"])
        self.assertTrue(rec["restore"]["ok"])

    def test_mcp_timeouts_in_a_row_end_the_run(self):
        game = FakeGame(self.tmp)
        orig = game.lua

        def dead(code, timeout=60):
            if game.n.get("view", 0) >= 1 and "BNGE_BENCH.restore" not in code:
                game.n["dead"] = game.n.get("dead", 0) + 1
                raise bm.McpError("timed out", timeout=True)
            return orig(code, timeout)
        game.lua = dead
        with self.assertRaises(bm.McpError):
            self.run_bench(game)
        self.assertEqual(game.camera, "game")              # restore's own calls got through

    def test_game_maps_transport_errors(self):
        """Game turns urllib / socket failures into McpError (timeout or not) and RPC errors into
        RuntimeError."""
        import socket
        import urllib.error

        class FakeMcp:
            def __init__(self, exc):
                self.exc = exc

            def rpc(self, *a, **k):
                raise self.exc

            def call(self, *a, **k):
                raise self.exc
        g = bm.Game.__new__(bm.Game)
        for exc, timeout in ((socket.timeout("timed out"), True), (TimeoutError(), True),
                             (urllib.error.URLError(socket.timeout("timed out")), True),
                             (urllib.error.URLError(ConnectionRefusedError()), False),
                             (ConnectionResetError(), False)):
            g.mcp = FakeMcp(exc)
            for fn in (lambda: g.lua("return 1"), lambda: g.screenshot()):
                with self.assertRaises(bm.McpError) as e:
                    fn()
                self.assertEqual(e.exception.timeout, timeout, exc)
        g.mcp = FakeMcp(SystemExit("RPC error: x"))
        with self.assertRaises(RuntimeError):
            g.lua("return 1")

    def test_lost_screenshot_keeps_the_place(self):
        """One screenshot never written: that view has an error, the place keeps its numbers and its
        other views."""
        game = FakeGame(self.tmp, lost_shots={1})
        rec, out = self.run_bench(game)
        a = rec["places"][0]
        self.assertIn("load", a)
        self.assertIn("not written", a["views"][0]["error"])
        self.assertTrue(a["views"][1]["file"])
        self.assertFalse(a["ok"])
        self.assertTrue(rec["places"][1]["ok"])

    def test_interrupt_during_restore(self):
        """Ctrl-C while the car is on its way home: the inputs are still zeroed, the camera still comes
        back and run.json is finished."""
        game = FakeGame(self.tmp, commit_polls=5)
        state = {"restoring": False}
        orig = game.do_restore

        def restore(saved, parts=None):
            state["restoring"] = True
            return orig(saved, parts)
        game.do_restore = restore

        def interrupt(t):
            if state["restoring"] and game.n.get("teleport", 0) == 2:
                self.clock.on_sleep = None
                raise KeyboardInterrupt
        self.clock.on_sleep = interrupt
        rec, out = self.run_bench(game, small_plan()[:1])
        fns = game.fns()
        self.assertEqual(fns[-1], "restore")
        self.assertIn("zeroInputs", fns[fns.index("restore"):])
        self.assertEqual(game.camera, "game")
        self.assertIn("interrupted", " ".join(rec["restore"]["errors"]))
        self.assertIn("finished", json.loads((out / "run.json").read_text()))


# --- compare --------------------------------------------------------------------------------------
def brute_ssim(a, b, k=7, peak=255.0):
    c1, c2 = (0.01 * peak) ** 2, (0.03 * peak) ** 2
    vals = []
    for i in range(a.shape[0] - k + 1):
        for j in range(a.shape[1] - k + 1):
            x, y = a[i:i + k, j:j + k], b[i:i + k, j:j + k]
            mx, my = x.mean(), y.mean()
            vx, vy, cxy = x.var(), y.var(), ((x - mx) * (y - my)).mean()
            vals.append(((2 * mx * my + c1) * (2 * cxy + c2)) / ((mx * mx + my * my + c1) * (vx + vy + c2)))
    return float(np.mean(vals))


class MetricsTest(unittest.TestCase):
    def test_ssim(self):
        rng = np.random.default_rng(3)
        a = rng.uniform(0, 255, (30, 41))
        b = np.clip(a + rng.normal(0, 20, a.shape), 0, 255)
        self.assertAlmostEqual(bm.ssim(a, a), 1.0, places=9)
        self.assertAlmostEqual(bm.ssim(a, b), brute_ssim(a, b), places=9)
        self.assertAlmostEqual(bm.ssim(a, b), bm.ssim(b, a), places=12)
        self.assertLess(bm.ssim(a, b), 0.99)
        self.assertAlmostEqual(bm.ssim(a[:3, :3], a[:3, :3]), 1.0)       # smaller than a window
        np.testing.assert_allclose(bm.box_mean(a, 7)[0, 0], a[:7, :7].mean())

    def test_image_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            rng = np.random.default_rng(1)
            img = rng.integers(0, 200, (60, 80, 3), dtype=np.uint8)
            pa, pb, pc, pd = (os.path.join(tmp, n) for n in ("a.png", "b.png", "c.png", "d.png"))
            Image.fromarray(img).save(pa)
            Image.fromarray(img + 10).save(pb)                       # brighter by 10
            Image.fromarray(np.kron(img, np.ones((2, 2, 1), np.uint8))).save(pc)   # same at twice the size
            same = bm.image_metrics(pa, pa)
            self.assertEqual((same["mad"], same["ssim"], same["changed"], same["resized"]), (0.0, 1.0, 0.0, False))
            m = bm.image_metrics(pa, pb, diff_path=pd)
            self.assertAlmostEqual(m["mad"], 10.0, places=6)
            self.assertEqual(m["changed"], 0.0)                      # 10 < CHANGED_DY
            self.assertLess(m["ssim"], 1.0)
            with Image.open(pd) as im:
                self.assertEqual(im.size, (80, 60))
            r = bm.image_metrics(pa, pc)
            self.assertTrue(r["resized"])
            self.assertEqual(r["size"], [80, 60])
            self.assertLess(r["mad"], 25)
            w = bm.image_metrics(pc, pc, width=40)                   # compared at 40 px wide
            self.assertEqual(w["size"], [40, 30])

    def test_local_change_is_flagged(self):
        """A small object moved on a textured scene: the mean SSIM stays above the flag, the
        changed-pixel share flags it."""
        self.assertTrue(bm.shot_changed({"ssim": 0.99, "changed": 0.02}))
        self.assertTrue(bm.shot_changed({"ssim": 0.95, "changed": 0.0}))
        self.assertFalse(bm.shot_changed({"ssim": 0.99, "changed": 0.005}))
        with tempfile.TemporaryDirectory() as tmp:
            bg = np.random.default_rng(0).integers(40, 140, (200, 300, 1)).repeat(3, axis=2).astype(np.uint8)
            img, moved = bg.copy(), bg.copy()
            img[80:100, 100:140] = 250
            moved[80:100, 120:160] = 250
            pa, pb = os.path.join(tmp, "a.png"), os.path.join(tmp, "b.png")
            Image.fromarray(img).save(pa)
            Image.fromarray(moved).save(pb)
            m = bm.image_metrics(pa, pb)
            self.assertGreater(m["ssim"], bm.SSIM_FLAG)
            self.assertGreater(m["changed"], bm.CHANGED_FLAG)
            self.assertTrue(bm.shot_changed(m))
            self.assertFalse(bm.shot_changed(m, changed_flag=0.5))
            self.assertFalse(bm.shot_changed(bm.image_metrics(pa, pa)))

    def test_number_delta(self):
        d = bm.number_delta("perf.fps", 60.0, 50.0)
        self.assertEqual((d["delta"], d["verdict"]), (-10.0, "worse"))
        self.assertAlmostEqual(d["pct"], -16.67, places=2)
        self.assertEqual(bm.number_delta("perf.fps", 60.0, 59.0)["verdict"], "same")      # within 5 %
        self.assertEqual(bm.number_delta("load.ready_s", 30.0, 20.0)["verdict"], "better")
        self.assertEqual(bm.number_delta("load.ready_s", 3.0, 4.0)["verdict"], "same")     # +33 % but < 2 s
        self.assertEqual(bm.number_delta("tiles.failed", 0, 1)["verdict"], "worse")
        self.assertIsNone(bm.number_delta("tiles.live", 900, 950)["verdict"])              # no direction
        self.assertIsNone(bm.number_delta("perf.fps", None, 50.0)["delta"])


class CompareTest(unittest.TestCase):
    def make_run(self, base, name, places):
        d = Path(base) / name
        d.mkdir()
        rec = {"label": name, "places": []}
        for pid, views, nums in places:
            pr = {"id": pid, "name": pid.upper(), "views": [], **nums}
            for vname, img, fps in views:
                f = f"{pid}__{vname}.png"
                Image.fromarray(img).save(d / f)
                pr["views"].append({"name": vname, "file": f, "perf": {"fps": fps, "p95": 18.0}})
            rec["places"].append(pr)
        (d / "run.json").write_text(json.dumps(rec))
        return d

    def test_compare(self):
        rng = np.random.default_rng(7)
        img = rng.integers(0, 255, (60, 80, 3), dtype=np.uint8)
        noisy = np.clip(img.astype(int) + rng.integers(-60, 60, img.shape), 0, 255).astype(np.uint8)
        with tempfile.TemporaryDirectory() as tmp:
            a = self.make_run(tmp, "A", [("x", [("ground", img, 60.0), ("aerial", img, 60.0)],
                                          {"load": {"ready_s": 30.0}, "tiles": {"failed": 0}}),
                                         ("y", [("ground", img, 60.0)], {"load": {"ready_s": 10.0}})])
            b = self.make_run(tmp, "B", [("x", [("ground", img, 50.0), ("aerial", noisy, 60.0)],
                                          {"load": {"ready_s": 20.0}, "tiles": {"failed": 2}}),
                                         ("z", [("ground", img, 60.0)], {})])
            out = Path(tmp) / "cmp"
            logs = []
            rep = bm.compare(a, b, out, log=logs.append)
            x, y, z = rep["places"]
            self.assertEqual([p["id"] for p in rep["places"]], ["x", "y", "z"])
            g, air = x["views"]
            self.assertEqual((g["metrics"]["mad"], g["metrics"]["ssim"]), (0.0, 1.0))
            self.assertFalse(g["changed"])
            self.assertTrue(air["changed"])
            self.assertGreater(air["metrics"]["mad"], 10)
            self.assertEqual(g["numbers"]["perf.fps"]["verdict"], "worse")
            self.assertEqual(x["numbers"]["load.ready_s"]["verdict"], "better")
            self.assertEqual(x["numbers"]["tiles.failed"]["verdict"], "worse")
            self.assertFalse(y["inB"])
            self.assertIsNone(y["views"][0]["metrics"])
            self.assertFalse(z["inA"])
            s = rep["summary"]
            self.assertEqual((s["shots"], s["changedShots"], s["missing"], s["worse"], s["better"]), (2, 1, 2, 2, 1))
            for p in rep["places"]:
                for v in p["views"]:
                    for k in ("fileA", "fileB", "diff"):
                        if v.get(k):
                            self.assertTrue((out / v[k]).is_file(), v[k])
            page = (out / "index.html").read_text()
            self.assertIn("img/diff_x__aerial.png", page)
            self.assertIn("prefers-color-scheme: dark", page)
            self.assertEqual(json.loads((out / "compare.json").read_text())["summary"], s)
            self.assertTrue(any("missing in B" in ln for ln in logs))

    def test_timed_out_or_failed_place_is_worse(self):
        """B never got ready (no ready_s) or failed where A was fine: flagged worse, not left blank."""
        img = np.zeros((20, 30, 3), np.uint8)
        with tempfile.TemporaryDirectory() as tmp:
            a = self.make_run(tmp, "A", [("x", [("ground", img, 60.0)], {"ok": True, "load": {"ready_s": 30.0, "timedOut": False}}),
                                         ("y", [("ground", img, 60.0)], {"ok": True, "load": {"ready_s": 20.0, "timedOut": False}})])
            b = self.make_run(tmp, "B", [("x", [("ground", img, 60.0)], {"ok": False, "load": {"ready_s": None, "timedOut": True}}),
                                         ("y", [], {"ok": False, "error": "teleport failed: Anchor request failed"})])
            rep = bm.compare(a, b, Path(tmp) / "cmp", log=lambda *_: None)
            x, y = rep["places"]
            self.assertEqual(x["numbers"]["load.timedOut"]["verdict"], "worse")
            self.assertEqual(x["numbers"]["ok"]["verdict"], "worse")
            self.assertEqual(y["numbers"]["ok"]["verdict"], "worse")
            self.assertGreaterEqual(rep["summary"]["worse"], 3)
            rep = bm.compare(b, a, Path(tmp) / "cmp2", log=lambda *_: None)
            self.assertEqual(rep["places"][0]["numbers"]["load.timedOut"]["verdict"], "better")

    def test_camera_mismatch_is_noted(self):
        """The set moved a camera between the runs: the pair is not comparable, and the page says so."""
        img = np.zeros((20, 30, 3), np.uint8)
        with tempfile.TemporaryDirectory() as tmp:
            a = self.make_run(tmp, "A", [("x", [("ground", img, 60.0), ("aerial", img, 60.0)], {})])
            b = self.make_run(tmp, "B", [("x", [("ground", img, 60.0), ("aerial", img, 60.0)], {})])
            for d, n in ((a, 0.0), (b, -12.0)):
                rec = json.loads((d / "run.json").read_text())
                rec["setDigest"] = "d" + str(n)
                for v in rec["places"][0]["views"]:
                    v["camera"] = {"e": 0.0, "n": n if v["name"] == "aerial" else 0.0, "agl": 1.7}
                (d / "run.json").write_text(json.dumps(rec))
            rep = bm.compare(a, b, Path(tmp) / "cmp", log=lambda *_: None)
            g, air = rep["places"][0]["views"]
            self.assertNotIn("note", g)
            self.assertIn("camera", air["note"])
            self.assertIn("set", rep["summary"]["note"])
            self.assertIn("camera differs", (Path(tmp) / "cmp" / "index.html").read_text())

    def test_unfocused_window(self):
        """A run with the window in the background (30 fps cap): no frame-time verdicts, a note."""
        img = np.zeros((20, 30, 3), np.uint8)
        with tempfile.TemporaryDirectory() as tmp:
            a = self.make_run(tmp, "A", [("x", [("ground", img, 60.0)], {})])
            b = self.make_run(tmp, "B", [("x", [("ground", img, 30.0)], {})])
            rec = json.loads((b / "run.json").read_text())
            rec["places"][0]["views"][0]["perf"]["focused"] = False
            (b / "run.json").write_text(json.dumps(rec))
            rep = bm.compare(a, b, Path(tmp) / "cmp", log=lambda *_: None)
            v = rep["places"][0]["views"][0]
            self.assertIn("not focused", v["note"])
            self.assertIsNone(v["numbers"]["perf.fps"]["verdict"])
            self.assertEqual(rep["summary"]["worse"], 0)
            self.assertIn("not focused", (Path(tmp) / "cmp" / "index.html").read_text())
        self.assertIn("window not focused", bm.summary_line(
            {"load": {"ready_s": 3}, "views": [{"name": "g", "perf": {"fps": 30, "p95": 34, "focused": False}}]}))
        self.assertIn("active cap 60 fps", bm.summary_line(
            {"views": [{"name": "g", "perf": {"focused": False, "fpsLimit": {"fpsCap": 60}}}]}))

    def test_compare_two_fake_runs(self):
        """Two runs of the same build against the fake game: every shot pair identical."""
        with tempfile.TemporaryDirectory() as tmp:
            outs = []
            for n in ("one", "two"):
                game = FakeGame(tmp)
                out = Path(tmp) / n
                bm.run(game, small_plan(), out, bm.options(), clock=FakeClock(), log=lambda *_: None)
                outs.append(out)
            rep = bm.compare(outs[0], outs[1], Path(tmp) / "cmp", log=lambda *_: None)
            self.assertEqual(rep["summary"]["shots"], 3)
            self.assertEqual(rep["summary"]["meanMad"], 0.0)
            self.assertEqual(rep["summary"]["changedShots"], 0)


class CliTest(unittest.TestCase):
    def test_plan(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(bm.main(["plan", "--set", "all"]), 0)
        text = buf.getvalue()
        self.assertIn("whitney_photo_south", text)
        self.assertIn("HELD-OUT", text)


@unittest.skipUnless(LUAJIT, "luajit not installed")
class LuaChunkTest(unittest.TestCase):
    def test_chunk_under_luajit(self):
        r = subprocess.run([LUAJIT, str(ROOT / "mod" / "tests" / "test_benchmark.lua")], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 failed", r.stdout)


if __name__ == "__main__":
    unittest.main()
