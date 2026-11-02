"""Robustness: a lost and a restarted server (docs/ROADMAP.md "Robustness: no server, lost server, lost
internet" 2, 3 and 7). Offline: the synthetic DEM and imagery of test_server, in-process servers.

SessionAdoptionTest (earthd.Server's session folders, no streaming):
  - at start a folder whose marker is older than SESSION_STALE_S goes (with the engine's .cdae caches),
    a younger one is kept as an orphan;
  - a game's hello / pose `sessions` adopts orphans (markers touched), ignores unknown names and the
    server's own; one no longer named is released: its files go through the delete queue, the folder
    last; a closed connection makes its claims orphans again with a fresh grace;
  - sweep_sessions deletes an orphan nobody claimed for orphan_s whose marker is as old, keeps a
    claimed one and one whose marker a live server keeps touching, and heartbeats the own marker;
  - a default session (no `sessions` in hello) changes nothing;
  - main_async's sweep removes the folders off the event loop; a folder with the new session's own id
    is never an orphan.

RestartDriveTest (fault injection with the mod simulator, modsim): a 64-cell, 1024² (1 m) mesh-mode session;
the mod teleports twice (its anchor 2) or once (its anchor 1: the new server's resync anchor id too, so
no pose or terrain request may name it until the resync commits, core.lua anchorKnown; the resync
commit waits the governor's CALM_TIMEOUT so poses go out meanwhile), drives east, then the server is
killed (nothing cleaned up, as a crash). Without a server the mod keeps driving east at 30 m/s: the
edge guard (modsim.EdgeGuard, the port of edgeguard.lua, run only while core.lua's serverHealthy rule
says the server cannot move the block) warns, then holds the car STOP_M inside the block's collidable
edge. A new server starts in the same user folder: the dead session's folder survives; the mod's hello
names it, the server adopts it; the resync at the same origin (a new session, anchor 1 again) keeps the
old session's tiles until the new ones cover them. Checked on every frame: no hole in the shown tiles within 512 m of the player,
every file a tile object of the mod uses exists; the guard runs only between the kill and the resync
commit (never while the server is healthy, so never against a block commit); once the mod no longer
names the old session, the server deletes its folder (anchor 1: see the known limit at the end).
"""
import asyncio
import math
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import earthd  # noqa: E402
import geodesy  # noqa: E402
import modsim  # noqa: E402
import test_server  # noqa: E402
from modsim import CORE, EDGE, GOV  # noqa: E402
from netclient import Client  # noqa: E402
from test_integration_drive import near_holes  # noqa: E402

LAT, LON = test_server.LAT, test_server.LON
MOD_VERSION = "0.4.0"
SIZE = 1024                     # the 1 km block (1024² at 1 m; it was 512² at 2 m)
DT = 1 / 60
SPEED = 30.0
PACE = 1.5                      # simulated seconds per wall second while a server streams
COSTS = {"cdae": 2.0, "dae": 8.3, "reveal": 0.05, "delete": 0.3, "motion": 0.0, "material": 0.3, "matDelete": 0.3,
         "cdaeTex": 3.0, "cdaeTex128": 3.0, "cdaeTex256": 3.0, "cdaeTex512": 3.0}


def make_session(root, sid, age_s, files=("far_1_0_0_0.cdae", "far_1_0_0_0.dds", "hm_1_0_0.png")):
    """An earlier run's session folder (and an engine .cdae cache of it) whose marker is age_s old."""
    d = os.path.join(root, "temp", "beamngEarth", sid)
    cache = os.path.join(root, *earthd.CDAE_ROOT, sid.lower())
    os.makedirs(d)
    os.makedirs(cache)
    for f in files:
        open(os.path.join(d, f), "w").close()
    open(os.path.join(cache, "far_1_0_0_0.cdae"), "w").close()
    marker = os.path.join(d, earthd.SESSION_MARKER)
    open(marker, "w").close()
    t = time.time() - age_s
    os.utime(marker, (t, t))
    return d, cache


class _Conn:
    """What the session bookkeeping uses of a Connection: its claimed sessions."""

    def __init__(self):
        self.sessions = set()


class SessionAdoptionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.stale = make_session(self.root, "20260101-000000-aaaa", earthd.SESSION_STALE_S + 600)
        self.a = make_session(self.root, "20261010-120000-bbbb", 300)
        self.b = make_session(self.root, "20261010-120500-cccc", 120)
        self.s = earthd.Server(self.root, os.path.join(self.root, "cache"), imagery=None)
        self.addCleanup(self.s.tile_pool.shutdown)
        self.addCleanup(self.s.hm_pool.shutdown)

    def conn(self):
        c = _Conn()
        self.s.connections.add(c)
        return c

    def test_scan_at_start(self):
        self.assertFalse(os.path.exists(self.stale[0]) or os.path.exists(self.stale[1]), "a day-old session goes")
        self.assertTrue(all(os.path.exists(p) for p in self.a + self.b), "younger sessions are kept")
        self.assertEqual(set(self.s.orphans), {"20261010-120000-bbbb", "20261010-120500-cccc"})
        self.assertNotIn(self.s.sid, self.s.orphans)

    def test_claim_release(self):
        c = self.conn()
        marker = os.path.join(self.a[0], earthd.SESSION_MARKER)
        before = os.path.getmtime(marker)
        self.s.claim_sessions(c, ["20261010-120000-bbbb", "nonsense", self.s.sid, 7, None])
        self.assertEqual(c.sessions, {"20261010-120000-bbbb"})
        self.assertGreater(os.path.getmtime(marker), before, "an adopted session's marker is touched")
        self.s.claim_sessions(c, None)                      # a pose without the field: no change
        self.s.claim_sessions(c, "x")
        self.assertEqual(c.sessions, {"20261010-120000-bbbb"})
        # another game claims it too: one letting go releases nothing
        c2 = self.conn()
        self.s.claim_sessions(c2, {"1": "20261010-120000-bbbb"})        # an empty / keyed Lua table as a dict
        self.s.claim_sessions(c, [])
        self.assertTrue(os.path.exists(self.a[0]))
        self.assertIn("20261010-120000-bbbb", self.s.orphans)
        # the last one lets go: released, deleted through the queue (no event loop here: at once)
        self.s.claim_sessions(c2, {})
        self.assertFalse(os.path.exists(self.a[0]) or os.path.exists(self.a[1]), "released: folder and cache deleted")
        self.assertNotIn("20261010-120000-bbbb", self.s.orphans)
        self.assertTrue(os.path.exists(self.b[0]), "the other orphan stays")
        self.assertTrue(os.path.exists(self.s.out_dir))

    def test_release_is_rate_limited_with_a_loop(self):
        """With an event loop the released files go one at a time (the game loaded them), the folder last."""
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        self.s.delete_interval = 0.01
        c = self.conn()
        seen = []

        async def run():
            self.s.claim_sessions(c, ["20261010-120000-bbbb"])
            self.s.claim_sessions(c, [])
            for _ in range(200):
                seen.append(sorted(os.listdir(self.a[0])) if os.path.exists(self.a[0]) else None)
                if seen[-1] is None and not os.path.exists(self.a[1]):
                    return
                await asyncio.sleep(0.005)
        loop.run_until_complete(run())
        self.assertIsNone(seen[-1], "deleted in the end")
        counts = [len(x) for x in seen if x is not None]
        self.assertGreater(len(set(counts)), 2, f"one file at a time: {counts[:20]}")
        self.assertIn([earthd.SESSION_MARKER], [x for x in seen if x is not None] or [[earthd.SESSION_MARKER]],
                      "the marker is the last file in the folder")

    def test_drop_claims(self):
        c = self.conn()
        self.s.claim_sessions(c, ["20261010-120500-cccc"])
        self.s.orphans["20261010-120500-cccc"] = time.monotonic() - 10000
        self.s.connections.discard(c)
        self.s.drop_claims(c)
        self.assertEqual(c.sessions, set())
        self.assertLess(time.monotonic() - self.s.orphans["20261010-120500-cccc"], 5, "a fresh grace")
        self.assertTrue(os.path.exists(self.b[0]), "a closed connection deletes nothing")

    def test_sweep(self):
        c = self.conn()
        self.s.claim_sessions(c, ["20261010-120000-bbbb"])
        self.s.orphan_s = 0.0
        t = time.time() - 50
        os.utime(os.path.join(self.b[0], earthd.SESSION_MARKER), (t, t))
        # a live server touches its marker: with orphan_s 100 its 50 s idle marker keeps it ...
        self.s.orphan_s = 100.0
        for sid in self.s.orphans:
            self.s.orphans[sid] -= 1000
        self.assertEqual(self.s.sweep_sessions(), [])
        self.assertTrue(os.path.exists(self.b[0]))
        # ... once idle for orphan_s and unclaimed since: deleted; the claimed one stays
        self.s.orphan_s = 30.0
        self.assertEqual(self.s.sweep_sessions(), ["20261010-120500-cccc"])
        self.assertFalse(os.path.exists(self.b[0]) or os.path.exists(self.b[1]))
        self.assertTrue(os.path.exists(self.a[0]), "a claimed session is never swept")
        # the heartbeat: the own marker (and the claimed ones) touched every SESSION_HEARTBEAT_S
        own = os.path.join(self.s.out_dir, earthd.SESSION_MARKER)
        t = time.time() - 500
        os.utime(own, (t, t))
        self.s.sweep_sessions()
        self.assertGreater(time.time() - os.path.getmtime(own), 400, "no heartbeat before SESSION_HEARTBEAT_S")
        self.s.heartbeat_at -= earthd.SESSION_HEARTBEAT_S
        self.s.sweep_sessions()
        self.assertLess(time.time() - os.path.getmtime(own), 5, "heartbeat: the own marker touched")

    def test_sweep_deletes_off_the_event_loop(self):
        """An unclaimed orphan is a whole session (~1 GB, thousands of files): main_async's sweep
        (sweep_sessions_async) keeps the bookkeeping on the loop and removes the folders in a worker
        thread, so the streaming never stalls behind an rmtree."""
        self.s.orphan_s = 30.0
        for sid in self.s.orphans:
            self.s.orphans[sid] -= 1000
        t = time.time() - 100
        for d in (self.a[0], self.b[0]):
            os.utime(os.path.join(d, earthd.SESSION_MARKER), (t, t))
        threads = []
        rm = self.s._rmtree_session
        self.s._rmtree_session = lambda sid: (threads.append(threading.get_ident()), rm(sid))
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)

        async def run():
            return threading.get_ident(), await self.s.sweep_sessions_async()
        loop_thread, gone = loop.run_until_complete(run())
        self.assertEqual(sorted(gone), ["20261010-120000-bbbb", "20261010-120500-cccc"])
        self.assertEqual(len(threads), 2)
        self.assertNotIn(loop_thread, threads, "the folders are removed off the event loop")
        self.assertFalse(any(os.path.exists(p) for p in self.a + self.b))
        self.assertEqual(self.s.orphans, {})

    def test_own_sid_never_an_orphan(self):
        """A folder that happens to carry the new session's id (a restart in the same second with the same
        random suffix) is the server's own from now on: never an orphan, so never released or swept."""
        own = "20261011-010203-eeee"
        d, _ = make_session(self.root, own, 60)
        strftime, token_hex = earthd.time.strftime, earthd.secrets.token_hex
        earthd.time.strftime = lambda fmt, *a: "20261011-010203-"
        earthd.secrets.token_hex = lambda n: "eeee"
        try:
            s = earthd.Server(self.root, os.path.join(self.root, "cache"), imagery=None)
        finally:
            earthd.time.strftime, earthd.secrets.token_hex = strftime, token_hex
        self.addCleanup(s.tile_pool.shutdown)
        self.addCleanup(s.hm_pool.shutdown)
        self.assertEqual(s.sid, own)
        self.assertNotIn(own, s.orphans)
        c = _Conn()
        s.connections.add(c)
        s.claim_sessions(c, [own])
        s.claim_sessions(c, [])
        s.orphan_s = 0.0
        s.sweep_sessions()
        self.assertTrue(os.path.isfile(os.path.join(d, earthd.SESSION_MARKER)), "the own folder stays")


class _Running:
    """An in-process server (mesh mode, 64 cells, 1024² block at 1 m, synthetic DEM and imagery) on its own loop."""

    def __init__(self, user_dir):
        s = earthd.Server(user_dir, os.path.join(user_dir, "cache"), tile_workers=3, imagery=None, near_visual="mesh",
                          cells=64, block=SIZE)
        s.imagery = test_server.FakeImagery(os.path.join(user_dir, "cache", "imagery"))
        s.dem = test_server.FakeDem()
        s.prune_delay, s.prune_recheck, s.delete_interval = 0.5, 0.05, 0.002
        self.server = s
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

        async def start():
            return await asyncio.start_server(lambda r, w: earthd.Connection(s, r, w).run(), "127.0.0.1", 0)
        self.srv = asyncio.run_coroutine_threadsafe(start(), self.loop).result(10)
        self.port = self.srv.sockets[0].getsockname()[1]
        self.alive = True

    def call(self, fn, *args):
        """Run fn on the server's loop (its state is the loop's) and return the result."""
        async def run():
            return fn(*args)
        return asyncio.run_coroutine_threadsafe(run(), self.loop).result(10)

    def kill(self):
        """A crash: connections drop, nothing is cleaned up (queued deletes never run), files stay."""
        if not self.alive:
            return
        self.alive = False

        async def stop():
            self.srv.close()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0.05)
        asyncio.run_coroutine_threadsafe(stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()
        self.server.tile_pool.shutdown(wait=True, cancel_futures=True)
        self.server.hm_pool.shutdown(wait=True, cancel_futures=True)
        self.server.imagery.close()


class RestartDriveTest(unittest.TestCase):
    def test_kill_and_restart_mid_drive(self):
        """The mod's anchor 2: none of the new server's anchor ids is the mod's until it rebases."""
        self._drive(teleports=2)

    def test_kill_and_restart_anchor_1(self):
        """The mod's anchor 1 (one teleport, no rebase: the usual case), the new server's resync anchor is
        1 too: until the resync commits nothing may name it (core.lua anchorKnown), or the new server
        switches to it early and streams tiles the far field files under the old session."""
        self._drive(teleports=1)

    def _drive(self, teleports):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        user = tmp.name
        fs = (lambda vfs: os.path.join(user, vfs.lstrip("/")))
        srv1 = _Running(user)
        self.addCleanup(srv1.kill)
        sid1 = srv1.server.sid
        far = modsim.FarField(COSTS, fs)
        far.cells = modsim.set_cells(64)
        gov = modsim.Governor()
        frame0 = geodesy.EnuFrame(LAT, LON)        # every anchor here has this origin (teleport / resync)
        frames = {}
        st = {"c": None, "session": None, "anchor": None, "block": None, "tp": None, "pending": None, "next": 1,
              "clock": 0.0, "pose_t": 0.0, "error_at": None, "t_lat": None, "wall": None, "phase": "start",
              "anchor_session": None, "resync": False, "guard_paused": False}
        player = {"p": [0.0, 0.0, 0.0], "v": [0.0, 0.0], "want": 0.0}
        guard = modsim.EdgeGuard()
        stats = {"holes": [], "vanished": [], "unhealthy": {}, "guard_modes": {}, "edge_min": math.inf,
                 "kept_while_held": 0, "held_frames": 0, "commits": 0, "frames": 0, "near_checks": 0}

        self.addCleanup(lambda: st["c"] is not None and st["c"].close())

        def rid():
            st["next"] += 1
            return st["next"] - 1

        def send(m):
            if st["c"] is not None:
                st["c"].send(m)

        def handle(m):
            t = m["type"]
            if t == "hello":
                st["session"] = m["sessionId"]
                st["resync"] = st["anchor"] is not None     # core.lua: the resync is requested at hello
                self.assertEqual(m.get("blockSize"), SIZE)
                far.set_near_visual(m["nearVisual"])
            elif t == "anchor_ready":
                p = st["pending"]
                if p and m["requestId"] == p["requestId"]:
                    p["ready"], p["msg"], p["ready_at"] = True, m, st["clock"]
            elif t == "terrain_ready":
                p = st["tp"]
                if p and m["requestId"] == p["requestId"] and m["anchorId"] == st["anchor"]:
                    p["ready"], p["msg"] = True, m
                    st["t_lat"] = st["clock"] - p["sent"]
                    st["error_at"] = None
            elif t == "terrain_error":
                p = st["tp"]
                if p and m["requestId"] == p["requestId"]:
                    st["tp"], st["error_at"] = None, st["clock"]
            elif t in ("far_tile", "far_swap", "far_remove", "far_done"):
                getattr(far, {"far_tile": "on_tile", "far_swap": "on_swap", "far_remove": "on_remove",
                              "far_done": "on_done"}[t])(m)

        def pump(until_wall):
            c = st["c"]
            while c is not None:
                try:
                    handle(c.recv(max(until_wall - time.monotonic(), 0.0005)))
                except TimeoutError:
                    if time.monotonic() >= until_wall:
                        return
                except (ConnectionError, OSError):          # the server died (core.lua '_disconnected')
                    c.close()
                    st["c"], st["session"], st["tp"], st["pending"] = None, None, None, None
                    return

        def connect(port):
            st["c"] = Client(port)
            st["c"].send({"type": "hello", "protocol": int(CORE["PROTOCOL"]), "format": int(CORE["FORMAT"]),
                          "modVersion": MOD_VERSION, "sessions": far.held_sessions()})

        def request_anchor(purpose):
            st["pending"] = {"requestId": rid(), "purpose": purpose}
            cx, cy = (0, 0) if purpose == "teleport" else modsim.follow_target(*player["p"][:2], *player["v"], SIZE)
            send({"type": "prepare_anchor", "requestId": st["pending"]["requestId"], "lat": LAT, "lon": LON,
                  "cx": cx, "cy": cy})

        def commit_anchor(m):
            tr = m["terrain"]
            keep = st["pending"]["purpose"] != "teleport"
            frames[m["anchorId"]] = frame0
            if not keep:
                player["p"], player["v"] = [0.0, 0.0, 0.0], [0.0, 0.0]
            far.switch_anchor(m["anchorId"], keep, (tr["cx"], tr["cy"]), st["session"])
            st["anchor"], st["block"], st["tp"], st["pending"] = m["anchorId"], (tr["cx"], tr["cy"]), None, None
            st["anchor_session"], st["resync"] = st["session"], False
            guard.new_frame()
            send({"type": "commit_anchor", "anchorId": m["anchorId"]})
            send({"type": "commit_terrain", "anchorId": m["anchorId"], "cx": tr["cx"], "cy": tr["cy"]})
            st["pose_t"] = CORE["POSE_INTERVAL"]

        def healthy():
            return modsim.server_healthy(st["c"] is not None, st["session"], st["tp"], st["clock"], st["error_at"],
                                         block=st["block"] is not None, resync=st["resync"])

        def known():
            return modsim.anchor_known(st["anchor"], st["anchor_session"], st["session"])

        def check_files():
            insts = list(far.live.values()) + list(far.old.values()) + list(far.pending.values()) + far.doomed
            gone = [i.shape for i in insts if i.loaded and not os.path.exists(fs(i.shape))]
            # materials in use (an unused pooled one keeps a deleted tile's texture name: harmless)
            gone += [t for n, t in far.mat_tex.items() if t and far.mats.get(n) and not os.path.exists(fs(t))]
            if gone:
                stats["vanished"].append((round(st["clock"], 2), st["phase"], gone[:3]))

        def frame(pace=True):
            stats["frames"] += 1
            st["clock"] += DT
            gov.begin_frame(far.has_work(), st["wall"])
            if pace and st["c"] is not None:
                pump(st["t0"] + st["clock"] / PACE)
            elif st["c"] is not None:
                pump(time.monotonic())
            x, y, z = player["p"]
            # the driver holds its speed east (the throttle); the guard pauses the game before the edge
            if player["want"] > 0:
                player["v"] = [player["want"], 0.0]
            ok = healthy()
            stats["unhealthy"].setdefault(st["phase"], 0)
            if not ok:
                stats["unhealthy"][st["phase"]] += 1
            elif st["guard_paused"]:
                st["guard_paused"] = False      # core.lua: the server healthy again releases the guard's pause
            if st["anchor"] is not None and not ok and st["block"] is not None:
                # core.lua updateGuard: only while the server cannot move the block; on the ground (0.5 m)
                box = modsim.ground_box(*st["block"], SIZE)
                inside = box[0] <= x <= box[2] and box[1] <= y <= box[3]
                d = min(x - box[0], box[2] - x, y - box[1], box[3] - y)
                if st["guard_paused"] and d > EDGE["STOP_M"] + EDGE["RESUME_INSIDE_M"]:
                    st["guard_paused"] = False  # edgeguard.update: the block followed
                if st["guard_paused"]:
                    mode = "hold"               # edgeguard.update: paused by the guard, no step
                else:
                    mode, _, _, pause = guard.step(st["clock"], x, y, z, *player["v"], box, 0.5 if inside else None)
                    st["guard_paused"] = pause
                stats["guard_modes"].setdefault(st["phase"], {}).setdefault(mode, 0)
                stats["guard_modes"][st["phase"]][mode] += 1
            if st["guard_paused"]:
                player["v"] = [0.0, 0.0]        # the game paused: the car does not move
            x, y = x + player["v"][0] * DT, y + player["v"][1] * DT
            player["p"] = [x, y, z]
            if st["block"] is not None:
                gx0, gy0, gx1, gy1 = modsim.ground_box(*st["block"], SIZE)
                stats["edge_min"] = min(stats["edge_min"], x - gx0, gx1 - x, y - gy0, gy1 - y)
            # core.lua: a teleport commits on its anchor_ready; updateWorld: the pose (only for an anchor
            # the server knows), the resync commit in a heavy slot (governor), else terrain-follow
            p = st["pending"]
            if p and p.get("ready") and p["purpose"] == "teleport":
                commit_anchor(p["msg"])
            elif st["anchor"] is not None and st["c"] is not None:
                st["pose_t"] += DT
                if st["pose_t"] >= CORE["POSE_INTERVAL"] and known():
                    st["pose_t"] = 0.0
                    lat, lon, _ = frame0.enu_to_geodetic(x, y, z)
                    held = far.held_anchors() if far.session == st["session"] else []
                    send({"type": "pose", "anchorId": st["anchor"], "x": x, "y": y, "z": z, "vx": player["v"][0],
                          "vy": player["v"][1], "lat": float(lat), "lon": float(lon), "speed": math.hypot(*player["v"]),
                          "held": held, "sessions": far.held_sessions()})
                    check_files()
                b, tp = st["block"], st["tp"]
                fa = None
                if p and p.get("ready"):
                    # the heavy slot comes once streaming calmed down: at most CALM_TIMEOUT frames
                    # later, the slowest case (poses go out meanwhile)
                    if st["clock"] - p["ready_at"] >= GOV["CALM_TIMEOUT"] * DT and gov.request_heavy():
                        commit_anchor(p["msg"])
                else:
                    fa = modsim.follow_decide(x, y, *player["v"], b[0], b[1], tp, st["clock"], 0.0, None, st["t_lat"],
                                              SIZE)
                if fa and fa[0] == "prepare" and not known():
                    pass                                    # core.lua: no request naming an unknown anchor
                elif fa and fa[0] == "prepare":
                    st["tp"] = {"requestId": rid(), "cx": fa[1], "cy": fa[2], "sent": st["clock"]}
                    send({"type": "prepare_terrain", "requestId": st["tp"]["requestId"], "anchorId": st["anchor"],
                          "cx": fa[1], "cy": fa[2]})
                elif fa and fa[0] == "commit":
                    st["block"], st["tp"] = (tp["cx"], tp["cy"]), None
                    stats["commits"] += 1
                    send({"type": "commit_terrain", "anchorId": st["anchor"], "cx": tp["cx"], "cy": tp["cy"]})
                elif fa and fa[0] == "discard":
                    st["tp"] = None
            cam = {"x": x - 8, "y": y, "z": z + 3, "fx": 1.0, "fy": 0.0, "fz": -0.05, "fovDeg": 60}
            far.update(gov, DT, cam)
            st["wall"] = 1000 / 60 + gov.frame_spent
            if st["phase"] not in ("start", "stand1") and st["anchor"] is not None:
                stats["near_checks"] += 1
                h = near_holes(far, frames, st["anchor"], (x, y))
                if h:
                    stats["holes"].append((round(st["clock"], 2), st["phase"], len(h), h[:2], (round(x), round(y))))
            if sid1 in far.held_sessions():
                stats["held_frames"] += 1
                if os.path.isdir(os.path.join(user, "temp", "beamngEarth", sid1)):
                    stats["kept_while_held"] += 1

        def settled():
            return far.done and not far.has_work() and not far.old

        def run_until(cond, max_wall, what, pace=True):
            end = time.monotonic() + max_wall
            while not cond():
                self.assertLess(time.monotonic(), end, f"{what}: done {far.done}, old {len(far.old)} "
                                f"{sorted(far.old)[:4]}, groups {len(far.groups)}, work {far.has_work()}, "
                                f"pending {len(far.pending)}, held {far.held_sessions()}, anchor {st['anchor']}")
                frame(pace)

        # --- 1. session 1: hello, two teleports (the mod's anchor 2), the far field settles ---------
        st["t0"] = time.monotonic()
        connect(srv1.port)
        run_until(lambda: st["session"] == sid1, 10, "hello 1")
        for _ in range(teleports):
            request_anchor("teleport")
            run_until(lambda: st["pending"] is None, 60, "teleport")
        self.assertEqual(st["anchor"], teleports)
        st["phase"] = "stand1"
        run_until(settled, 120, "the first far field")
        # --- 2. drive east, the server healthy ----------------------------------------------------
        st["phase"], player["want"] = "drive1", SPEED
        st["t0"] = time.monotonic() - st["clock"] / PACE
        run_until(lambda: player["p"][0] > 500, 120, "drive 1")
        self.assertGreater(stats["commits"], 0, "terrain-follow commits in the healthy drive")
        held_before = far.held_sessions()
        self.assertEqual(held_before, [sid1])
        # --- 3. the server dies; the mod keeps driving east for 25 s --------------------------------
        srv1.kill()
        st["phase"] = "gap"
        for _ in range(int(25 / DT)):
            frame(pace=False)
        self.assertIsNone(st["c"], "the connection is gone")
        block = st["block"]
        gx1 = modsim.ground_box(*block, SIZE)[2]
        self.assertLessEqual(player["p"][0], gx1 - EDGE["STOP_M"] + 0.5, "paused before the edge")
        self.assertTrue(st["guard_paused"], "the game paused by the guard")
        self.assertLess(abs(player["v"][0]), 0.2, "not moving while paused")
        modes = stats["guard_modes"]["gap"]
        self.assertGreater(modes.get("warn", 0), 0, modes)
        self.assertGreater(modes.get("hold", 0), 0, modes)
        sess1 = os.path.join(user, "temp", "beamngEarth", sid1)
        self.assertTrue(os.path.isdir(sess1))
        # --- 4. a new server in the same user folder -----------------------------------------------
        srv2 = _Running(user)
        self.addCleanup(srv2.kill)
        sid2 = srv2.server.sid
        self.assertNotEqual(sid1, sid2)
        self.assertIn(sid1, srv2.server.orphans, "the dead session is kept for the game to claim")
        check_files()
        self.assertFalse(stats["vanished"], stats["vanished"][:3])
        st["phase"] = "reconnect"
        st["t0"] = time.monotonic() - st["clock"] / PACE
        connect(srv2.port)
        run_until(lambda: st["session"] == sid2, 10, "hello 2")
        self.assertIn(sid1, srv2.call(srv2.server.claimed_sessions), "hello.sessions: the old session adopted")
        request_anchor("resync")
        run_until(lambda: st["pending"] is None, 60, "the resync")
        self.assertEqual((st["anchor"], far.session), (1, sid2), "the new session's anchor 1")
        self.assertGreater(len(far.old), 0, "the old session's tiles are kept")
        # back to normal once the first terrain of the new session is committed
        n = stats["commits"]
        st["phase"] = "resync"
        run_until(lambda: stats["commits"] > n, 60, "the first terrain after the restart")
        st["phase"] = "drive2"
        start_x = player["p"][0]
        run_until(lambda: player["p"][0] > start_x + 600, 120, "drive 2")
        player["want"], player["v"] = 0.0, [0.0, 0.0]
        st["phase"] = "settle"
        if teleports > 1:
            run_until(lambda: settled() and sid1 not in far.held_sessions(), 120, "the old tiles go")
            run_until(lambda: not os.path.isdir(sess1), 60, "the server deletes the released session")
        else:
            # Known limit (docs/ARCHITECTURE.md "Robustness"): the new session's anchor 1 repeats the
            # old one's imagery material names (bngEarthSat_<anchor>_<key>), and a material in use keeps
            # its texture, so a new tile that took over such a material holds the old folder's texture
            # until it goes. Every tile shape is the new session's; the old folder stays while named
            # (stats["kept_while_held"], every frame).
            run_until(settled, 120, "the old tiles go")
            insts = list(far.live.values()) + list(far.pending.values()) + far.doomed
            self.assertFalse([i.shape for i in insts if modsim.folder_of(i.shape) == sid1], "no old tile shape left")

        print(f"\n  restart drive: {stats['frames']} frames, {stats['commits']} commits, guard modes "
              f"{stats['guard_modes']}, unhealthy frames {stats['unhealthy']}, closest to an edge "
              f"{stats['edge_min']:.1f} m, {stats['near_checks']} coverage checks, old session held "
              f"{stats['held_frames']} frames", file=sys.stderr)
        self.assertFalse(stats["holes"], stats["holes"][:3])
        self.assertFalse(stats["vanished"], stats["vanished"][:3])
        self.assertEqual(stats["kept_while_held"], stats["held_frames"], "the old folder never went while held")
        for ph in ("stand1", "drive1", "drive2", "settle"):
            self.assertEqual(stats["unhealthy"].get(ph, 0), 0, f"the guard never runs while healthy ({ph})")
            self.assertNotIn(ph, stats["guard_modes"])
        self.assertGreater(stats["unhealthy"]["gap"], 0)
        self.assertGreaterEqual(stats["edge_min"], EDGE["STOP_M"] - 1, "never off the block")
        self.assertIn(sid2, far.held_sessions())
        self.assertTrue(os.path.isdir(srv2.server.out_dir))


if __name__ == "__main__":
    unittest.main()


class TerrainCacheGoesWithItsSessionTest(unittest.TestCase):
    def test_base_photos_cache_entries_removed(self):
        # detail mode's packed base photos (terraincache.py) left by an earlier run: 3.9 GB had piled up (2026-10-18)
        import terraincache
        with tempfile.TemporaryDirectory() as root:
            sid = "20260101-000000-dddd"
            make_session(root, sid, earthd.SESSION_STALE_S + 600, files=("hm_1_0_0_base.png", "hm_1_0_0_base_r2.png"))
            cdir = terraincache.cache_dir(root)
            os.makedirs(cdir)
            keys = [terraincache.base_key(f"/temp/beamngEarth/{sid}/{n}", earthd.BASE_AO_VFS)
                    for n in ("hm_1_0_0_base.png", "hm_1_0_0_base_r2.png")]
            for k in keys + ["0123456789abcdef"]:                  # (the last: the game's own, kept)
                open(os.path.join(cdir, k + ".dds"), "w").close()
            s = earthd.Server(root, os.path.join(root, "cache"), imagery=None)
            self.addCleanup(s.tile_pool.shutdown)
            self.addCleanup(s.hm_pool.shutdown)
            self.assertEqual(sorted(os.listdir(cdir)), ["0123456789abcdef.dds"])
