"""A snow state change re-renders and re-sends only what it really changes (the user's drive, 2026-11-28 ~01:00: Hungary,
the Stelvio, California, then Baltimore with no snow and a wet road; lag, buildings vanishing and coming back).

What the server log showed and what each test pins:
  - "snowy roads changed: road tiles again" 8 times in 13 min, twice in Baltimore (01:05:34 the teleport, 01:05:50 its
    first state) though no snow lay before or after: a bare road look's winter part ({"q": 1, banks 0}) differed from
    none, and each change gave every connection a NEW road tracker (every tile's content again, the builds running
    dropped). Now a bare look is no winter part (snowroad.bare), and a change rebuilds the skins alone
    (roads.GeoTracker.reskin), the content left as the mod has it.
  - "snow state changed (real): 1403 far tiles to texture again" for a road share moving in one plow class (01:00:01,
    01:01:38): every far tile re-rendered though only the few with texels fine enough for the roads draw road shares.
    Now Server.snow_changed takes the tables before and after and marks only the textures whose look differs
    (snowcover.look_changes); no snow before and after is no change; a tile rendering through the change is made again
    once it is in; tiles nobody shows are left stale, not queued (the "dropped 5688").
Fake fetches and fixtures only (no network, no game).
"""
import asyncio
import sys
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fast  # noqa: E402
import snow  # noqa: E402
import snowcover  # noqa: E402
import snowroad  # noqa: E402
from test_snow import RESPONSES, last_hour  # noqa: E402
from test_snow_place import ATLANTA, LONDON, MILD, VALDEZ, ByCell, Conn, Server, pose  # noqa: E402
from test_snow import FakeCover  # noqa: E402

NATIVE = fast._ef is not None and getattr(fast._ef, "VERSION", 0) >= snowroad.NATIVE


class Recorder(Server):
    """test_snow_place's server, keeping the fields each snow_changed got."""

    def __init__(self, svc):
        super().__init__(svc)
        self.fields = []

    def snow_changed(self, fields=None):
        self.changes += 1
        self.fields.append(fields)


@unittest.skipUnless(NATIVE, "needs earthfast 640 (snowroad_look)")
class BareRoadsTest(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.on = mock.patch.object(snowroad, "ON", True)
        self.on.start()

    def tearDown(self):
        self.on.stop()
        self.loop.close()

    def session(self, fetch, now):
        self.now = [now]
        svc = snow.Service(fetch=fetch, mode="real", clock=lambda: self.now[0], cover=FakeCover(), winter=True)
        server = Recorder(svc)
        return svc, server, Conn(server, self.loop), snow.Tracker()

    def test_bare_look_is_no_winter_part(self):
        look = dict(fast._ef.snowroad_look([[0, 0, 500, 0, 0, 0, 0, 0]] * 10, 0, 0, 999, False, 500, 2, [-1] * 5))
        self.assertTrue(snowroad.bare(look), look)
        self.assertIsNone(snowroad.wear_snow(look), "no snow on the roads: the skins as without winter")
        self.assertEqual(snowroad.skin_key({"tier": 2}), ())
        # snow on the ground: paint hidden, banks, covered walks: a winter part
        snowy = dict(fast._ef.snowroad_look([[10, 800, -300, 10, 900, 0, 0, 2000]] * 10, 1500, 8, 2, False, -500, 2,
                                            [-1] * 5))
        self.assertFalse(snowroad.bare(snowy), snowy)
        self.assertEqual(snowroad.wear_snow(snowy)["q"], 1)
        for part, v in (("bank_bin", [0, 1, 0, 0, 0]), ("marking", [1000, 1000, 640, 1000, 1000]),
                        ("deck_cover", [0, 0, 0, 125, 0]), ("walks", [[3, 0], [1, 5], [3, 0], [3, 0]])):
            self.assertFalse(snowroad.bare(dict(look, **{part: v})), part)

    def test_baltimore_teleport_sends_no_road_tile_again(self):
        """California (dry, no snow) then Baltimore (no snow, its first fetch deferred): before, 3 road resends
        (California's first state, the teleport, Baltimore's first state); now none, and no texture either."""
        fetch = ByCell({ATLANTA: "atlanta_dry", LONDON: "london_dry"}, fail={LONDON: 1})
        svc, server, conn, tr = self.session(fetch, MILD["atlanta_dry"]["hourly"]["time"][-7])
        pose(tr, conn, ATLANTA)
        conn.settle()
        self.assertIsNotNone(svc.table)
        self.assertIsNone(svc.road_snow)
        pose(tr, conn, LONDON)                  # (the teleport: its first fetch deferred, "busy, background dropped")
        conn.settle()
        self.now[0] += snow.RETRY_FAST_S + 1
        pose(tr, conn, LONDON)
        conn.settle()
        self.assertEqual(tr.fails, 0)
        states = [m for m in conn.sent if m["type"] == "snowstate"]
        self.assertEqual(len(states), 2)
        self.assertTrue(all("look" in m for m in states), "W3's look still sent (the mod's materials)")
        self.assertEqual(server.road_changes, 0, "no snow on any road before or after: no road tile again")
        self.assertEqual(server.changes, 0, "no snow drawn before or after: no texture again")

    def test_snow_still_changes_the_roads(self):
        """Snow where it snowed (the user's rule): a snowy state's roads take their winter part; leaving for a bare
        place takes it off (one skin refresh each way); the textures follow with both tables."""
        fetch = ByCell({VALDEZ: "davos", ATLANTA: "atlanta_dry"})
        svc, server, conn, tr = self.session(fetch, last_hour(RESPONSES["davos"]) - 24 * 3600 + 600)
        pose(tr, conn, VALDEZ[:2] + (1500.0,))      # (the forcing's snow lies at 1,500 m, not at 100 m)
        conn.settle()
        self.assertIsNotNone(svc.road_snow, "snow on the roads: a winter part")
        self.assertEqual(server.road_changes, 1)
        self.assertEqual(server.changes, 1)
        old, new = server.fields[-1]
        self.assertIsNone(old)
        self.assertIsNotNone(new, "the new table as the provider's Field")
        self.now[0] = MILD["atlanta_dry"]["hourly"]["time"][-7]
        pose(tr, conn, ATLANTA)
        self.assertEqual(server.road_changes, 2, "the teleport: the snowy skins off")
        self.assertEqual(server.fields[-1][1], None)
        conn.settle()
        self.assertIsNone(svc.road_snow)
        self.assertEqual(server.road_changes, 2, "Atlanta's bare state: nothing more")
        self.assertEqual(server.changes, 2)


@unittest.skipUnless(fast._ef is not None and getattr(fast._ef, "VERSION", 0) >= snow.NATIVE, "earthfast 624")
class NewAnchorTest(unittest.TestCase):
    """Codex on 17fb1d0e: the mod drops the origin's state at a teleport (winter.lua onTeleport) and takes the next one
    as the destination's; the server sends a state for here after every new anchor, and one still in flight for the
    last anchor is dropped."""

    def test_a_state_after_every_new_anchor(self):
        import geodesy
        loop = asyncio.new_event_loop()
        try:
            a = (61.13, -146.36, 100.0)
            b = (61.24, -146.36, 100.0)         # (12 km north: under NEW_PLACE_M and the mod's PLACE_M)
            c = (61.33, -146.36, 100.0)
            fetch = ByCell({a: "davos", b: "davos", c: "davos"})
            now = [last_hour(RESPONSES["davos"]) - 24 * 3600 + 600]
            svc = snow.Service(fetch=fetch, mode="real", clock=lambda: now[0], cover=FakeCover(), winter=True)
            server = Recorder(svc)
            conn, tr = Conn(server, loop), snow.Tracker()

            def states():
                return [m for m in conn.sent if m["type"] == "snowstate"]
            pose(tr, conn, a)
            conn.settle()
            self.assertEqual((len(states()), len(fetch.calls)), (1, 1))
            pose(tr, conn, a)                   # (the same anchor, the same place: nothing new)
            conn.settle()
            self.assertEqual(len(states()), 1)
            # a rebase (a new anchor, the car where it was): this place's state again, from the cached forcing
            f2 = geodesy.EnuFrame(a[0] + 0.002, a[1])
            x, y, _ = f2.geodetic_to_enu(a[0], a[1], 0.0)
            tr.on_pose(conn, f2, float(x), float(y), a[2])
            conn.settle()
            self.assertEqual((len(states()), len(fetch.calls)), (2, 1), "sent again, no new fetch")
            # a teleport to b, then on to c before b's state is in: b's is dropped, c's sent
            pose(tr, conn, b)
            pose(tr, conn, c)
            conn.settle()
            got = states()
            self.assertEqual(len(got), 3, [m["lat"] for m in got])
            self.assertLess(abs(got[-1]["lat"] - c[0]), 0.1)
        finally:
            loop.close()

    def test_a_snapped_teleport_under_a_new_anchor_id(self):
        """Codex on 2ebc7b8f: a snapped teleport lands under a NEW anchor id whose frame has the same origin as the
        last, 2 km from the origin and 10 m higher (under REFETCH_M and REZ_M): the destination's first pose still
        gets a state (the mod dropped the origin's at the teleport), not one at the next UTC hour."""
        import geodesy
        loop = asyncio.new_event_loop()
        try:
            a = (61.13, -146.36, 100.0)
            fetch = ByCell({a: "davos"})
            now = [last_hour(RESPONSES["davos"]) - 24 * 3600 + 600]
            svc = snow.Service(fetch=fetch, mode="real", clock=lambda: now[0], cover=FakeCover(), winter=True)
            server = Recorder(svc)
            conn, tr = Conn(server, loop), snow.Tracker()
            frame = geodesy.EnuFrame(a[0], a[1])
            same = geodesy.EnuFrame(a[0], a[1])         # (anchor 2's frame: the snapped grid point, the same origin)

            def states():
                return [m for m in conn.sent if m["type"] == "snowstate"]
            tr.on_pose(conn, frame, 0.0, 0.0, a[2], 1)
            conn.settle()
            tr.on_pose(conn, frame, 0.0, 300.0, a[2], 1)      # (driving under anchor 1: nothing new)
            conn.settle()
            self.assertEqual(len(states()), 1)
            self.assertLess(2000.0, snow.REFETCH_M)
            self.assertLess(10.0, snow.REZ_M)
            tr.on_pose(conn, same, 0.0, 2000.0, a[2] + 10.0, 2)      # (2 km north: the same forcing cell)
            conn.settle()
            self.assertEqual(len(states()), 2, "the destination's first pose: its state at once")
            tr.on_pose(conn, same, 0.0, 2050.0, a[2] + 10.0, 2)
            conn.settle()
            self.assertEqual(len(states()), 2, "then as before: by the hour, the distance, the height")
            self.assertEqual(len(fetch.calls), 1, "the same cell and hour: from the cache")
        finally:
            loop.close()


def table(rows, road=(8,) * 15, ice=0):
    return snowcover.Field(0.1, uniform=snowcover.Table(rows, road, ice))


class LookChangeTest(unittest.TestCase):
    ROWS = ((0, 6, 2, 0), (2000, 8, 4, 0))

    def test_effective_look(self):
        a = table(self.ROWS, (0, 5, 5, 5, 8, 0, 2, 2, 2, 6, 0, 5, 5, 6, 8))
        b = table(self.ROWS, (0, 5, 5, 5, 8, 0, 2, 2, 2, 6, 0, 5, 5, 8, 8))     # (the Stelvio's 01:00:01 change)
        self.assertEqual(snowcover.uniform_changes(a, b), (True, False), "road shares: the road texels' only")
        self.assertEqual(snowcover.uniform_changes(None, None), (False, False))
        bare = table(((0, 0, 0, 0), (3000, 0, 0, 1)))
        self.assertEqual(snowcover.uniform_changes(None, bare), (False, False), "zero snow to zero snow")
        self.assertEqual(snowcover.uniform_changes(a, None), (True, True))
        self.assertEqual(snowcover.uniform_changes(a, table(((0, 6, 3, 0), (2000, 8, 4, 0)))), (True, True))
        self.assertEqual(snowcover.uniform_changes(a, table(self.ROWS, a.uniform.road, 4)), (True, True), "lake ice")
        # by cell: per box
        cells = snowcover.Field(0.1, cells={(1800, 400): snowcover.Table(self.ROWS, (8,) * 15)})
        self.assertIsNone(snowcover.uniform_changes(cells, None))
        far = (10.0, 10.0, 10.05, 10.05)
        near = (49.95, 0.0, 50.0, 0.05)         # (cell 1800, 400: lon 0.0-0.1, lat 50.0-49.9)
        self.assertFalse(snowcover.look_changes(cells, None, far, True))
        self.assertTrue(snowcover.look_changes(cells, None, near, True))

    def test_server_marks_only_what_changes(self):
        import earthd
        import surface
        if not surface.far_photo_roads(256.0, 1024):
            self.skipTest("roads not painted in the ground (BNGE_ROADS_IN_GROUND=0)")
        tile_far = SimpleNamespace(level=6, size=16384.0)       # (16 m texels: no road shares)
        tile_near = SimpleNamespace(level=0, size=256.0)        # (0.25 m texels: the roads' plow classes)

        def info(tile):
            return SimpleNamespace(material="m", texture="t.dds", stale=False, tile=tile, tex_px=0, retex_repeat=0)
        tiles = {(1, "far"): info(tile_far), (1, "near"): info(tile_near), (1, "hidden"): info(tile_near),
                 (1, "bare"): SimpleNamespace(material=None, texture=None, stale=False)}
        queued = []
        srv = SimpleNamespace(tiles=tiles, near_visual="detail", hm={(1, 0, 0): {}, (2, 0, 0): {}},
                              anchors={1: object()}, base_stale=set(), tex_size=lambda level: 1024,
                              map_on=lambda: False)
        srv.retex_request = lambda ck, pump=True: queued.append(ck)
        srv._retex_pump = lambda: None
        srv._retex_viewers = lambda ck: ck[1] != "hidden"
        a = table(self.ROWS, (0, 5, 5, 5, 8) * 3)
        b = table(self.ROWS, (0, 5, 5, 6, 8) * 3)
        # zero snow to zero snow: nothing
        self.assertEqual(earthd.Server.snow_changed(srv, fields=(None, None)), 0)
        self.assertEqual((queued, srv.base_stale), ([], set()))
        # a road share: the tiles fine enough for the roads and the block photos; the far one keeps its texture
        self.assertEqual(earthd.Server.snow_changed(srv, fields=(a, b)), 2)
        self.assertEqual(queued, [(1, "near")], "nobody shows 'hidden': stale, not queued")
        self.assertFalse(tiles[(1, "far")].stale)
        self.assertTrue(tiles[(1, "hidden")].stale)
        self.assertEqual(srv.base_stale, {(1, 0, 0)})
        self.assertEqual((srv.snow_gen, sorted(srv.snow_hist)), (2, [1, 2]), "each change a generation")
        # snow gone: everything with a texture
        queued.clear()
        self.assertEqual(earthd.Server.snow_changed(srv, fields=(b, None)), 3)
        self.assertTrue(tiles[(1, "far")].stale)
        self.assertNotIn((1, "bare"), queued)

    def server(self, tiles=None):
        import earthd
        queued = []
        srv = SimpleNamespace(tile_inflight={}, tiles=dict(tiles or {}), degraded=set(), near_visual="far", hm={},
                              anchors={}, base_stale=set(), matslots=SimpleNamespace(short=lambda: False),
                              tile_cache=10 ** 6, tex_size=lambda level: 1024, map_on=lambda: False)
        srv.note_lod_err = lambda ck, info: None
        srv.retex_request = lambda ck, pump=True: queued.append(ck)
        srv._retex_pump = lambda: None
        return earthd.Server, srv, queued

    def built(self, tile):
        fut = Future()
        fut.set_result(SimpleNamespace(material="m", texture="t.dds", stale=False, retex_repeat=0,
                                       cm_pending=frozenset(), tile=tile))
        return fut

    def test_a_first_render_through_a_change_is_made_again(self):
        """Codex on 17fb1d0e: a FIRST build (nothing cached yet) submitted before a snow change read the old state;
        when it is in it is stale (the far stream sends it and asks for its re-render, farstate.rs), unless the change
        leaves its look as it was."""
        near, far = SimpleNamespace(level=0, size=256.0), SimpleNamespace(level=6, size=16384.0)
        a = table(self.ROWS, (0, 5, 5, 5, 8) * 3)
        b = table(self.ROWS, (0, 5, 5, 6, 8) * 3)
        for tile, fields, stale in ((near, (None, a), True), (far, (None, a), True), (near, (a, b), True),
                                    (far, (a, b), False), (near, (None, None), False)):
            S, srv, queued = self.server()
            ck = (1, "k")
            S._note_snow_gen(srv, ck)               # (tile(): its build submitted)
            fut = self.built(tile)
            srv.tile_inflight[ck] = fut
            self.assertEqual(S.snow_changed(srv, fields=fields), 0, "not cached: nothing to mark yet")
            S._tile_done(srv, ck, fut)
            self.assertIs(srv.tiles[ck], fut.result())
            self.assertEqual(srv.tiles[ck].stale, stale, (tile.size, fields is None))
            self.assertEqual(queued, [ck] if stale else [])
            self.assertEqual(srv.tile_gen, {}, "its generation forgotten once in")
        # submitted after the change: as fresh as it is
        S, srv, queued = self.server()
        S.snow_changed(srv, fields=(None, a))
        S._note_snow_gen(srv, (1, "k"))
        fut = self.built(near)
        S._tile_done(srv, (1, "k"), fut)
        self.assertFalse(fut.result().stale)
        # older than the kept generations: made again
        S, srv, queued = self.server()
        S._note_snow_gen(srv, (1, "k"))
        for _ in range(earthd_hist_max() + 1):
            S.snow_changed(srv, fields=(a, b))
        fut = self.built(far)
        S._tile_done(srv, (1, "k"), fut)
        self.assertTrue(fut.result().stale)


def earthd_hist_max():
    import earthd
    return earthd.SNOW_HIST_MAX


class ReskinTest(unittest.TestCase):
    """roads.GeoTracker.reskin: the skins again, never the content (its buildings); earthd's snow_roads_changed keeps
    each connection's tracker."""

    def test_snow_roads_changed_keeps_the_trackers(self):
        import earthd
        import roads
        geo, anchor = roads.GeoTracker(), roads.Tracker()
        geo.sent, geo.skins = {(1, 2), (3, 4)}, {(1, 2), (3, 4)}
        c1, c2 = SimpleNamespace(roads=geo), SimpleNamespace(roads=anchor)
        srv = SimpleNamespace(connections=[c1, c2, SimpleNamespace(roads=None)])
        self.assertEqual(earthd.Server.snow_roads_changed(srv), 1)
        self.assertIs(c1.roads, geo, "the same tracker: no content again, no build dropped")
        self.assertIs(c2.roads, anchor, "anchor road tiles never read the winter part")
        self.assertFalse(geo.closed)
        self.assertEqual(geo.sent, {(1, 2), (3, 4)})
        self.assertEqual(geo.skins, set())
        self.assertEqual(geo.reskinning, {(1, 2), (3, 4)})

    def test_skin_started_before_a_reskin_stays_due(self):
        import roads
        tr = roads.GeoTracker()
        tr.aid, key = 1, (5, 6)
        tr.sent, tr.skins = {key}, {key}
        tr.reskin()
        msgs = []
        conn = SimpleNamespace(send=msgs.append)

        def done(h):
            f = Future()
            f.set_result({"content": None, "skin": ("/x/rs.json", {"h": h}), "exact": {}, "time": {}})
            return f
        tr._built(conn, 1, key, done("old"), False, True, None, 0)     # (started before the reskin)
        self.assertEqual([m["type"] for m in msgs], ["roads_skin"])
        self.assertNotIn(key, tr.skins, "made with the old winter part: still due")
        self.assertIn(key, tr.reskinning)
        tr._built(conn, 1, key, done("new"), False, True, None, tr.skin_gen)
        self.assertIn(key, tr.skins)
        self.assertEqual(tr.reskinning, set())
        self.assertFalse([m for m in msgs if m["type"] == "roads_tile"], "no content sent")

    @unittest.skipUnless(fast._ef is not None and getattr(fast._ef, "VERSION", 0) >= 588, "earthfast 588 (road_geo)")
    def test_reskin_rebuilds_skins_only(self):
        """A scripted drive to steady state, a reskin, the same poses: every skin built and sent again, no content
        built or sent, the loading progress whole throughout."""
        import threading
        from concurrent.futures import wait
        import geodesy
        import roads
        lat, lon = 39.2834, -76.5934
        jobs, lock = [], threading.Lock()

        class Loop:
            def __init__(self):
                self.futs = []

            def run_in_executor(self, pool, fn, *args):
                f = pool.submit(fn, *args)
                self.futs.append(f)
                return f

        msgs = []
        conn = SimpleNamespace(send=lambda m: msgs.append(m))

        def build(aid, gx, gy, content, skin, exact):
            with lock:
                jobs.append((gx, gy, content, skin))
            return {"content": ("/x/rg.json", 1, {"h": f"h{gx}_{gy}", "parts": {}}, []) if content else None,
                    "skin": ("/x/rs.json", {}) if skin else None, "exact": {}, "time": {}}
        frame = geodesy.EnuFrame(lat, lon)
        tr, loop = roads.GeoTracker(), Loop()

        def drive():
            n0 = len(jobs)
            for _ in range(60):
                tr.on_pose(conn, 1, 0.0, 0.0, loop, build, frame=frame)
                wait(loop.futs)
                if not tr.building and all(k in tr.sent and k in tr.skins for k in tr.car) and \
                        not any(tr.need.get((k, 1)) for k in tr.car):
                    break
            return jobs[n0:]
        drive()
        done = tr.progress()
        self.assertEqual(done[0], done[1])
        skinned = set(tr.skins)
        m0 = len(msgs)
        tr.reskin()
        self.assertEqual(tr.progress(), done, "a winter look change is no loading")
        again = drive()
        tr.close()
        self.assertEqual([j for j in again if j[2]], [], "no content built again")
        self.assertEqual({(j[0], j[1]) for j in again if j[3]}, skinned & {(j[0], j[1]) for j in again})
        self.assertTrue(skinned <= tr.skins)
        types = [m["type"] for m in msgs[m0:]]
        self.assertNotIn("roads_tile", types)
        self.assertEqual(types.count("roads_skin"), len(skinned))


if __name__ == "__main__":
    unittest.main()
