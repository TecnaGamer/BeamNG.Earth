"""Saved areas (areas.py, native areacover.rs): the box checks and the deterministic plan, the estimate, the list's
store (resume after a restart, pause / resume / delete), the download thread's gates (never while the live pools are
busy or a teleport waits, a refused request tried again, a host's daily share used up: waiting for tomorrow), the pins
(cache keys in a saved area) and netfetch's PREDOWNLOAD_SHARE. No network: stub producers."""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import areas  # noqa: E402
import fast  # noqa: E402
import netfetch  # noqa: E402
import pacer  # noqa: E402

BOX = (39.27, -76.62, 39.30, -76.58)        # ~3.4 km x 3.3 km round Baltimore's harbour (a benchmark place)
ROUTE = ([39.2834, 39.2900, 39.3000, 39.3076], [-76.5934, -76.6000, -76.6100, -76.6157])  # ~4 km through it


class StubDem:
    def __init__(self):
        self.have, self.got = set(), []
        self.fail = None

    def has(self, z, x, y):
        return (z, x, y) in self.have

    def predownload(self, z, x, y):
        if self.fail is not None:
            e, self.fail = self.fail, None
            raise e
        self.got.append((z, x, y))
        self.have.add((z, x, y))
        return True


class StubMapData:
    def __init__(self):
        self.have, self.got = set(), []

    def has(self, key, layer):
        return (tuple(key), layer) in self.have

    def predownload(self, key, layer):
        self.got.append((tuple(key), layer))
        self.have.add((tuple(key), layer))
        return True


class StubCache:
    def __init__(self, cap=20e9, keys=()):
        self.cap = cap
        self.ready = True
        self.pinned = None
        self.pinned_bytes = None
        self._sizes = [(k, 1000) for k in keys]

    def sizes(self, keys=None):
        return list(self._sizes)


class StubServer:
    def __init__(self, cap=20e9, keys=()):
        self.dem = StubDem()
        self.mapdata = StubMapData()
        self.imagery = None
        self.net = netfetch.Fetcher(offline=True)
        self.cache = StubCache(cap, keys)
        self.busy = False
        self.connections = []

    def live_busy(self):
        return self.busy

    # the world phase (earthd.Server.geo_prebuilt / prebuild_geo): a geotile's content "built" once
    built = None

    def geo_prebuilt(self, gx, gy):
        return f"k{gx}_{gy}" if self.built is not None and (gx, gy) in self.built else None

    def prebuild_geo(self, gx, gy):
        if self.built is None:
            self.built = set()
        self.built.add((gx, gy))
        self.prebuilds = getattr(self, "prebuilds", 0) + 1
        return f"k{gx}_{gy}"

    def driving(self):
        return False


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class AreasTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, v in (("IDLE_WAIT_S", 0.02), ("DEFER_WAIT_S", 0.01), ("STATUS_S", 0.0)):
            p = mock.patch.object(areas, name, v)
            p.start()
            self.addCleanup(p.stop)

    def make(self, server=None, start=False):
        s = server or StubServer()
        a = areas.Areas(self.tmp.name, s)
        if start:
            a.start()
            self.addCleanup(a.close)
        return a, s

    def test_boxes(self):
        self.assertEqual(areas.check_box(*BOX), BOX)
        for bad in ((10, 0, 9, 1), (0, 10, 1, 9), (-86, 0, -84, 1), (0, 179.9, 1, 180.5), ("a", 0, 1, 1),
                    (float("nan"), 0, 1, 1), (0, 0, 1e-4, 1e-4)):
            with self.assertRaises(ValueError, msg=bad):
                areas.check_box(*bad)
        with self.assertRaisesRegex(ValueError, "square miles"):
            areas.check_box(39.0, -77.0, 40.0, -76.0)             # ~9,600 km2
        self.assertAlmostEqual(fast._ef.area_km2(*BOX), 11.4, delta=0.5)

    def test_plan_is_deterministic_and_ordered(self):
        a, _ = self.make()
        steps = a.steps(BOX)
        self.assertEqual(steps, a.steps(BOX))
        kinds = [st[0] for st in steps]
        self.assertEqual(kinds, sorted(kinds, key=["cell", "dem", "img"].index), "cells, then DEM, then imagery")
        cells = {st[1:4] for st in steps if st[0] == "cell"}
        core = fast._ef.area_tiles(*BOX, 14, 0)
        self.assertEqual(len(cells), len(fast._ef.area_tiles(*BOX, 14, areas.CELL_RING)))
        self.assertTrue({(14, x, y) for x, y in core} <= cells)
        layers = {st[4] for st in steps if st[0] == "cell"}
        self.assertEqual(layers, set(areas.area_layers()))
        dz = {st[1] for st in steps if st[0] == "dem"}
        self.assertTrue(set(areas.DEM_ZOOMS_FAR) <= dz)
        self.assertEqual(len({st[4] for st in steps if st[0] == "img"}), len(areas.IMG_STEPS))

    def test_estimate_counts_what_is_missing(self):
        a, s = self.make()
        p = a.plan(*BOX)
        self.assertTrue(p["ok"], p)
        self.assertGreater(p["downloadBytes"], 0)
        self.assertEqual(p["hosts"][0][0] in ("tiles.mapterhorn.com", "overturemaps-us-west-2.s3.us-west-2.amazonaws.com"),
                         True)
        # everything on disk: nothing to download
        for st in a.steps(BOX):
            if st[0] == "dem":
                s.dem.have.add(st[1:])
            elif st[0] == "cell":
                s.mapdata.have.add((st[1:4], st[4]))
        p2 = a.plan(*BOX)
        self.assertEqual((p2["downloadBytes"], p2["hosts"]), (0, []))
        self.assertFalse(a.plan(10, 0, 9, 1)["ok"])

    def test_add_download_resume_and_delete(self):
        a, s = self.make(start=True)
        ok, aid = a.add(*BOX, name="Harbour")
        self.assertTrue(ok, aid)
        self.assertTrue(wait_for(lambda: a.message()["areas"][0]["state"] == "done"))
        n_cells = sum(1 for st in a.steps(BOX) if st[0] == "cell")
        self.assertEqual(len(s.mapdata.got), n_cells)
        m = a.message()["areas"][0]
        self.assertEqual((m["name"], m["done"], m["total"]), ("Harbour", len(a.steps(BOX)), len(a.steps(BOX))))
        # the job is its mode's phases ("data": the downloads; "world": them, then the road geotiles: test_world_mode)
        self.assertEqual(m["mode"], "data")
        self.assertEqual(a.steps(BOX, "data"), a.phase_steps("data", BOX))
        self.assertEqual(a.steps(BOX, "world"), a.phase_steps("data", BOX) + a.phase_steps("world", BOX))
        with self.assertRaises(ValueError):
            a.phase_steps("nope", BOX)
        import json
        with open(os.path.join(self.tmp.name, areas.SUBDIR, areas.STORE)) as f:
            self.assertEqual(json.load(f)["areas"][0]["mode"], "data")
        a.close()
        # a restart: the list comes back, the done area is not walked again
        b, s2 = self.make(server=s, start=True)
        self.assertEqual(b.message()["areas"][0]["state"], "done")
        # resume: the plan walked again, nothing fetched (all on disk)
        before = len(s.dem.got)
        self.assertTrue(b.command(aid, "resume"))
        self.assertTrue(wait_for(lambda: b.message()["areas"][0]["state"] == "done"))
        self.assertEqual(len(s.dem.got), before)
        self.assertTrue(b.command(aid, "delete"))
        self.assertEqual(b.message()["areas"], [])
        self.assertFalse(b.command(aid, "pause"))
        self.assertFalse(b.command("x", "explode"))

    def test_a_stopped_server_resumes_where_it_left_off(self):
        s = StubServer()
        a, _ = self.make(server=s)
        ok, aid = a.add(*BOX)
        steps = a.steps(BOX)
        half = steps[: len(steps) // 2]
        for st in half:                     # (half of it downloaded before the restart)
            if st[0] == "dem":
                s.dem.have.add(st[1:])
            elif st[0] == "cell":
                s.mapdata.have.add((st[1:4], st[4]))
        b, _ = self.make(server=s, start=True)
        self.assertTrue(wait_for(lambda: b.message()["areas"][0]["state"] == "done"))
        fetched = len(s.dem.got) + len(s.mapdata.got)
        self.assertEqual(fetched, sum(1 for st in steps[len(half):] if st[0] in ("dem", "cell")))

    def test_gates_hold_the_downloads(self):
        s = StubServer()
        s.busy = True
        a, _ = self.make(server=s, start=True)
        a.add(*BOX)
        time.sleep(0.3)
        self.assertEqual(s.mapdata.got, [], "nothing while the live pools are busy")
        pacer.foreground_begin()
        try:
            s.busy = False
            time.sleep(0.3)
            self.assertEqual(s.mapdata.got, [], "nothing while a teleport's first block is built")
        finally:
            pacer.foreground_end()
        self.assertTrue(wait_for(lambda: len(s.mapdata.got) > 0))
        self.assertTrue(a.command(a.message()["areas"][0]["id"], "pause"))
        time.sleep(0.1)
        n = len(s.mapdata.got) + len(s.dem.got)
        time.sleep(0.2)
        self.assertEqual(len(s.mapdata.got) + len(s.dem.got), n, "paused: no more downloads")

    def test_refused_requests_wait_and_budgets_wait_for_tomorrow(self):
        s = StubServer()
        for st in areas.Areas(self.tmp.name, s).steps(BOX):     # (only the DEM left to fetch)
            if st[0] == "cell":
                s.mapdata.have.add((st[1:4], st[4]))
        s.dem.fail = netfetch.Deferred("bucket low")
        a, _ = self.make(server=s, start=True)
        a.add(*BOX)
        self.assertTrue(wait_for(lambda: len(s.dem.got) > 3), "a refused request is tried again")
        s.dem.fail = netfetch.BudgetExceeded("past 50 %", host="tiles.mapterhorn.com")
        self.assertTrue(wait_for(lambda: a.message()["areas"][0]["state"] in ("waiting", "done")))
        m = a.message()["areas"][0]
        if m["state"] == "waiting":
            self.assertIn("tomorrow", m["msg"])
            self.assertIn("tiles.mapterhorn.com", m["msg"])

    def test_pins_and_room(self):
        keys = ["dem/mapterhorn/16/18823/24979.webp", "dem/mapterhorn/16/1/1.webp", "mapdata/raw/x.blk"]
        s = StubServer(cap=20e9, keys=keys)
        a, _ = self.make(server=s, start=True)
        self.assertEqual(s.cache.pinned, a.pinned)
        self.assertEqual(a.pinned(keys), [False, False, False], "no saved areas: nothing pinned")
        a.add(*BOX)
        self.assertEqual(a.pinned(keys), [True, False, False])
        a.count_pinned()
        self.assertEqual((a.pinned_bytes, s.cache.pinned_bytes), (1000, 1000))
        self.assertEqual(a.message()["pinnedBytes"], 1000)
        # a small cache: an area that would pin more than PIN_SHARE of it is refused
        small, _ = areas.Areas(os.path.join(self.tmp.name, "b"), StubServer(cap=1e6)), None
        ok, why = small.add(*BOX)
        self.assertFalse(ok)
        self.assertIn("not enough room", why)
        # at most MAX_AREAS
        with mock.patch.object(areas, "MAX_AREAS", 1):
            ok, why = a.add(*BOX)
            self.assertFalse(ok)
            self.assertIn("at most", why)

    def test_routes(self):
        # a corridor: the route's points and a half width; its plan walks the corridor's tiles in order along it
        lats, lons = list(ROUTE[0]), list(ROUTE[1])
        self.assertEqual(areas.check_route(lats, lons, 800), (lats, lons, 800.0))
        for bad in (([39.0], [-76.6], 800), (lats, lons[:1], 800), (lats, lons, 50), (lats, lons, 9000),
                    ([39.0, 99.0], lons, 800), ([39.0, float("nan")], lons, 800), (lats, lons, "x")):
            with self.assertRaises(ValueError, msg=bad):
                areas.check_route(*bad)
        with self.assertRaisesRegex(ValueError, "square miles"):
            areas.check_route([30.0, 45.0], [-100.0, -80.0], 5000)     # ~2,400 km long, 10 km wide
        a, s = self.make(server=StubServer(cap=20e9, keys=["dem/mapterhorn/16/18823/24979.webp",
                                                           "dem/mapterhorn/16/1/1.webp"]), start=True)
        route = {"lat": lats, "lon": lons, "half": 800.0}
        box = areas.route_bounds(route)
        steps = a.steps(box, route=route)
        self.assertEqual(steps, a.steps(box, route=route))
        self.assertEqual([st[0] for st in steps], sorted((st[0] for st in steps), key=["cell", "dem", "img"].index))
        cells = [st[1:4] for st in steps if st[0] == "cell"]
        ring = (2 * areas.CELL_RING + 1) ** 2      # (the start's ring tiles share its place along the route)
        self.assertIn((14,) + fast._ef.corridor_tiles(lats[:1], lons[:1], 0.0, 14, 0)[0][:2],
                      list(dict.fromkeys(cells))[:ring], "the route's start first")
        # a corridor has fewer cells than its bounds' box (the route is diagonal)
        self.assertLess(len(set(cells)), len(fast._ef.area_tiles(*box, 14, areas.CELL_RING)))
        p = a.plan_route(lats, lons, 800)
        self.assertTrue(p["ok"], p)
        self.assertAlmostEqual(p["km"], fast._ef.corridor_size(lats, lons, 800)[0] / 1000, delta=0.01)
        self.assertFalse(a.plan_route(lats[:1], lons[:1], 800)["ok"])
        ok, aid = a.add_route(lats, lons, 800, name="Commute")
        self.assertTrue(ok, aid)
        self.assertTrue(wait_for(lambda: a.message()["areas"][0]["state"] == "done"))
        m = a.message()["areas"][0]
        self.assertEqual((m["name"], m["total"]), ("Commute", len(steps)))
        self.assertEqual(m["route"][0], [round(lats[0], 6), round(lons[0], 6)])
        self.assertEqual(m["route"][-1], [round(lats[-1], 6), round(lons[-1], 6)])
        # pins: a key under the corridor's cells, not one far off; a DEM tile containing them (an ancestor) too
        x, y = fast._ef.corridor_tiles(lats[:1], lons[:1], 0.0, 16, 0)[0][:2]
        self.assertEqual(a.pinned([f"dem/mapterhorn/16/{x}/{y}.webp", "dem/mapterhorn/16/1/1.webp",
                                   f"dem/mapterhorn/10/{x >> 6}/{y >> 6}.webp", "mapdata/raw/x.blk"]),
                         [True, False, True, False])
        a.close()
        # the store keeps the route
        b, _ = self.make(server=s)
        self.assertEqual(b.areas[0]["route"], route)
        self.assertEqual(b.message()["areas"][0]["state"], "done")
        self.assertFalse(b.add_route(lats, lons, 20, "")[0])

    def test_world_mode(self):
        # "world": the downloads, then every road geotile's content built ahead (z17 tiles of the box), listed in
        # <id>.world (pinned_world), none built twice
        a, s = self.make(start=True)
        p = a.plan(*BOX, mode="world")
        self.assertTrue(p["ok"], p)
        n17 = len(fast._ef.area_tiles(*BOX, areas.WORLD_Z, 0))
        self.assertEqual((p["worldTiles"], p["worldMissing"]), (n17, n17))
        self.assertGreater(p["diskBytes"], a.plan(*BOX)["diskBytes"] + 0.9 * n17 * areas.WORLD_TILE_BYTES - 1)
        self.assertEqual(a.plan(*BOX)["worldTiles"], 0, "data only: no world tiles")
        self.assertFalse(a.plan(*BOX, mode="nope")["ok"])
        ok, aid = a.add(*BOX, name="Harbour", mode="world")
        self.assertTrue(ok, aid)
        self.assertTrue(wait_for(lambda: a.message()["areas"][0]["state"] == "done"))
        self.assertEqual(s.prebuilds, n17)
        self.assertEqual(a.message()["areas"][0]["mode"], "world")
        self.assertEqual(len(areas.pinned_world(self.tmp.name)), n17)
        a.close()
        b, _ = self.make(server=s, start=True)          # a restart: done, nothing built again
        self.assertTrue(b.command(aid, "resume"))
        self.assertTrue(wait_for(lambda: b.message()["areas"][0]["state"] == "done"))
        self.assertEqual(s.prebuilds, n17)
        self.assertEqual(len(areas.pinned_world(self.tmp.name)), n17, "the list rewritten, the same keys")
        # a route can build its world too; too large an area cannot
        lats, lons = list(ROUTE[0]), list(ROUTE[1])
        self.assertGreater(b.plan_route(lats, lons, 800, "world")["worldTiles"], 0)
        with mock.patch.object(areas, "MAX_WORLD_KM2", 1.0):
            self.assertIn("too large", b.plan(*BOX, mode="world")["error"])
        self.assertTrue(b.command(aid, "delete"))
        self.assertEqual(areas.pinned_world(self.tmp.name), set(), "deleted: unpinned")

    def test_a_damaged_store_is_no_list(self):
        os.makedirs(os.path.join(self.tmp.name, areas.SUBDIR))
        with open(os.path.join(self.tmp.name, areas.SUBDIR, areas.STORE), "w") as f:
            f.write("{not json")
        a, _ = self.make()
        self.assertEqual(a.message()["areas"], [])
        ok, aid = a.add(*BOX)
        self.assertTrue(ok)


class ProducerHooksTest(unittest.TestCase):
    """The producers' download-ahead entry points: the imagery chain for a square (lazy sources and hosts without
    offline use left out and named), a DEM tile and a map data cell layer stored without being loaded."""

    def test_imagery_sources_for_a_square(self):
        import imagery
        with tempfile.TemporaryDirectory() as d:
            im = imagery.Imagery(d, ["virginia", "naip", "usgs", "eox2023"], fetcher=netfetch.Fetcher(offline=True))
            excluded = set()
            got = list(im.predownload_sources(13, 2353, 3122, 17, fetch_probes=False, excluded=excluded))
            # NAIP (lazy: only fetched while driving otherwise) at z17: 16 x 16 under the z13 square; USGS at z16 (its
            # deepest): 8 x 8; then EOX at z14: 2 x 2
            self.assertEqual([(s.name, len(k), k[0][1]) for s, k in got],
                             [("naip", 256, 17), ("usgs", 64, 16), ("eox2023", 4, 14)])
            self.assertEqual(excluded, {"virginia"}, "a regional service: no bulk downloads")
            key = got[1][1][0]
            self.assertFalse(im.on_disk_or_missing(key))
            jpg, miss = im._paths(*key)
            os.makedirs(os.path.dirname(miss), exist_ok=True)
            open(miss, "wb").close()
            self.assertTrue(im.on_disk_or_missing(key))
            self.assertFalse(im.on_disk(key))

    def test_dem_tile_ahead(self):
        import dem as demmod
        from test_netfetch import fetcher
        f, _, _, tr = fetcher({"mapterhorn": [(200, {}, b"RIFF\x00\x00\x00\x00WEBPVP8 tile", 0.0)]})
        with tempfile.TemporaryDirectory() as d:
            dm = demmod.Dem(d, fetcher=f)
            self.assertTrue(dm.predownload(12, 1176, 1561))
            self.assertTrue(dm.has(12, 1176, 1561))
            self.assertFalse(dm.predownload(12, 1176, 1561), "on disk: nothing asked")
            self.assertEqual(len(tr.calls), 1)

    def test_map_data_cell_ahead(self):
        import numpy as np
        import mapcells
        import mapdata as mapdatamod
        with tempfile.TemporaryDirectory() as d:
            md = mapdatamod.MapData(d, fetcher=netfetch.Fetcher(offline=True))
            key = mapcells.CellKey(14, 4705, 6244)
            with mock.patch.object(md, "_build", return_value={"x": np.zeros(3)}) as b:
                self.assertTrue(md.predownload(key, "water"))
                self.assertEqual(b.call_args[0][2], "predownload")
                self.assertTrue(md.has(key, "water"))
                self.assertFalse(md.predownload(key, "water"))
                self.assertEqual(b.call_count, 1)
            self.assertFalse(os.path.exists(os.path.join(d, "mapdata-mirror")), "not loaded or mirrored")


import test_server  # noqa: E402


class AreaMessagesTest(test_server.ServerHarness):
    """area_plan / area_add / area_cmd through a session, answered with area_plan_result and areas (the list after
    hello, and the add's result); without saved areas (no cache) an error."""

    def test_area_session(self):
        self.hello()
        self.c.send({"type": "area_plan", "id": 1, "s": BOX[0], "w": BOX[1], "n": BOX[2], "e": BOX[3]})
        m, _ = self.c.until(lambda m: m["type"] == "area_plan_result", timeout=10)
        self.assertEqual((m["id"], m["ok"]), (1, False))
        self.assertIn("download cache", m["error"])
        self.server.areas = areas.Areas(self.tmp.name, StubServer())
        self.c.send({"type": "area_plan", "id": 2, "s": BOX[0], "w": BOX[1], "n": BOX[2], "e": BOX[3]})
        m, _ = self.c.until(lambda m: m["type"] == "area_plan_result" and m["id"] == 2, timeout=10)
        self.assertTrue(m["ok"], m)
        self.assertGreater(m["items"], 0)
        self.c.send({"type": "area_add", "id": 3, "s": BOX[0], "w": BOX[1], "n": BOX[2], "e": BOX[3], "name": "Harbour"})
        m, _ = self.c.until(lambda m: m["type"] == "areas" and (m.get("result") or {}).get("id") == 3, timeout=10)
        self.assertTrue(m["result"]["ok"])
        self.assertEqual([a["name"] for a in m["areas"]], ["Harbour"])
        aid = m["result"]["area"]
        self.c.send({"type": "area_add", "id": 4, "s": 10, "w": 0, "n": 9, "e": 1})
        m, _ = self.c.until(lambda m: m["type"] == "areas" and (m.get("result") or {}).get("id") == 4, timeout=10)
        self.assertFalse(m["result"]["ok"])
        # a saved route: the session's last planned GPS route (none planned here: an error)
        self.c.send({"type": "area_plan", "id": 6, "route": "current", "half": 800})
        m, _ = self.c.until(lambda m: m["type"] == "area_plan_result" and m["id"] == 6, timeout=10)
        self.assertFalse(m["ok"])
        self.assertIn("no route", m["error"])
        self.c.send({"type": "area_cmd", "id": 5, "area": aid, "op": "delete"})
        m, _ = self.c.until(lambda m: m["type"] == "areas" and (m.get("result") or {}).get("id") == 5, timeout=10)
        self.assertEqual((m["result"]["ok"], m["areas"]), (True, []))

    def test_live_busy_and_driving(self):
        """The gates' answers: a worker pool with jobs in flight or a loading line not full is busy; a player moving
        faster than a walk drives."""
        s = self.server
        self.hello()
        self.assertFalse(s.live_busy())
        self.assertFalse(s.driving())
        pool = mock.Mock(inflight=1)
        with mock.patch.object(s, "road_pool", pool):
            self.assertTrue(s.live_busy())
        c = next(iter(s.connections))
        c.progress_sent = ((1, 1), (3, 26), (0, 0), (10, 10))
        self.assertTrue(s.live_busy())
        c.progress_sent = ((1, 1), (26, 26), (0, 0), (10, 10))
        self.assertFalse(s.live_busy())
        c.vel = (12.0, 5.0)
        self.assertTrue(s.driving())


class PredownloadShareTest(unittest.TestCase):
    """netfetch: "predownload" requests stop at PREDOWNLOAD_SHARE of a host's daily budget (the rest is live play's),
    only on hosts that allow offline use, and their failures mark no build incomplete (revisit.fault)."""

    def test_share_and_policy(self):
        import revisit
        from test_netfetch import fetcher
        pol = {"tiles.example": netfetch.HostPolicy(rate=1000.0, burst=1000.0, max_conc=8, daily_budget=10,
                                                    allow_prefetch=True, offline_ok=True),
               "closed.example": netfetch.HostPolicy(rate=1000.0, burst=1000.0, daily_budget=10)}
        f, _, _, _ = fetcher(policies=pol)
        for i in range(5):
            f.get(f"https://tiles.example/{i}.png", kind="predownload")
        n = revisit.faults()
        with self.assertRaises(netfetch.BudgetExceeded):
            f.get("https://tiles.example/9.png", kind="predownload")
        self.assertEqual(revisit.faults(), n, "a download ahead's refusal is no build's fault")
        f.get("https://tiles.example/10.png")            # demand still goes
        with self.assertRaises(netfetch.PolicyDenied):
            f.get("https://closed.example/1.png", kind="predownload")


if __name__ == "__main__":
    unittest.main()
