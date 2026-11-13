"""Places the player is likely to go to, loaded ahead (the fastest world generators load the destination ahead; the
user, 2026-11-11: "sub second world gen, even with clear cashe"; outside reviews 2026-11-13: start on intent, and during
the game's own level load).

Modes, by what the player did (Preloader.request):
  hover  a search result, saved or recent place the map's pointer rests on (EarthMap.vue PRELOAD_HOVER_MS): the
         place's signed decision archives (Server.decisions_prefetch), its first block's elevation tiles and the map
         cells of the road tiles within HOVER_M, at the prefetch priority;
  pick   the map's "Teleport here?" box open, a search result picked: those, all of the road tiles' cells (SEND_M)
         and the first block's photos (their aerial tiles and packs on disk when the teleport asks);
  start  the place the game's level start will teleport to (startplace.py), read once the server listens, while the
         game loads: as pick, and the first block's cells planned in the worker processes under an anchor id of its own
         (Server.plan_first's hand-out; only where the stages are shared: earthd.sharing_machine).

Single ownership: every bit of state here lives on the server's asyncio event loop and is touched only there (request,
teleport, close, the running job's coroutine). The blocking parts (downloads, map cells, photos, the start place's
files) run one at a time on the preloader's own thread (its executor) and come back to the loop; between two parts the
job looks whether it is still wanted. A plan anchor is reserved (Server.alloc_anchor_id) and recorded before its jobs
are handed out, on the loop, so a teleport (which also runs on the loop) always sees it: a teleport farther than
CANCEL_M stops the job and abandons its plan anchors through the server's one abandoned-anchors writer
(Server.set_abandoned). Any teleport, and close, makes a start place still being read obsolete (gen).
"""
import asyncio
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

log = logging.getLogger("earthd.preload")

HOVER_M = 256.0             # a hover's map cells: the road tiles within this of the place
CANCEL_M = 3000.0           # a teleport (or a preload of a place) farther than this stops a preload
RANK = {"hover": 0, "pick": 1, "start": 2}
SAME_DEG = 1e-3             # places this close are the same one


def far(a, b):
    """Are places a and b ((lat, lon, ...); None: no) farther apart than CANCEL_M?"""
    if a is None or b is None:
        return False
    dy = (a[0] - b[0]) * 111320.0
    dx = (a[1] - b[1]) * 111320.0 * max(0.05, math.cos(math.radians(b[0])))
    return math.hypot(dx, dy) > CANCEL_M


def same(a, b):
    return a is not None and b is not None and abs(a[0] - b[0]) < SAME_DEG and abs(a[1] - b[1]) < SAME_DEG


class _Stop(Exception):
    pass


class Job:
    def __init__(self, lat, lon, mode):
        self.lat, self.lon, self.mode = lat, lon, mode
        self.dead = False
        self.done = {}              # what it did (the log line; tests)

    @property
    def at(self):
        return (self.lat, self.lon)


class Preloader:
    """One per server (Server.preloader). parts: the server's (duck-typed for tests): decisions_prefetch, dem, layout,
    mapdata, warm_place, imagery, near_visual, plan_first, alloc_anchor_id, set_abandoned, sharing (a callable: does
    this machine plan a first block's cells in its processes). executor: where the blocking parts run (one thread of
    its own by default)."""

    def __init__(self, server, executor=None, sharing=None, render_px=(), dem_zoom=None, dem_reach=0.0,
                 block_m=2048.0):
        self.s = server
        self.block_m = block_m              # m: the first block's photos' side
        self.ex = executor
        self.sharing = sharing or (lambda: False)
        self.render_px = tuple(render_px)
        self.dem_zoom = dem_zoom            # (lat) -> zoom of the first block's elevation tiles
        self.dem_reach = dem_reach          # m round the place they cover
        self.job = None                     # the running one
        self.queued = None                  # the next one (a newer request replaces it)
        self.last = None                    # (lat, lon, mode) of the last one started
        self.plans = []                     # (anchor id, (lat, lon)) handed out and not abandoned
        self.gen = 0                        # bumped by every teleport and close: an older start read is dropped
        self.closed = False
        self.task = None

    # --- the loop's side --------------------------------------------------------------------------------------
    def request(self, lat, lon, mode):
        """The player likely goes to (lat, lon) (mode: RANK). Event loop only."""
        if self.closed:
            return None
        job = Job(lat, lon, mode)
        for other in (self.job, self.queued):
            if other is not None and same(other.at, job.at) and RANK[mode] <= RANK[other.mode]:
                return None             # (that one does as much)
        if self.queued is not None:
            self.queued.dead = True
        self.queued = job
        if self.job is not None and not same(self.job.at, job.at):
            self.job.dead = True        # (another place: the running one stops at its next part)
            if far(self.job.at, job.at):
                self._abandon(lambda at: far(at, job.at))
        if self.task is None:
            self.task = asyncio.get_running_loop().create_task(self._drain())
        return job

    def teleport(self, lat, lon):
        """A teleport to (lat, lon) was asked for: a start place still being read is obsolete; a preload of a place
        farther than CANCEL_M stops (a queued one is dropped) and its plan anchors are abandoned. Event loop only."""
        self.gen += 1
        to = (lat, lon)
        if self.queued is not None and far(self.queued.at, to):
            self.queued.dead = True
            self.queued = None
        if self.job is not None and far(self.job.at, to):
            self.job.dead = True
        if far(self.last, to):
            self.last = None            # (a later preload of that place does its parts again)
        self._abandon(lambda at: far(at, to))

    def close(self):
        """No more preloads (the server stops): the running one stops, a start place being read is dropped."""
        self.closed = True
        self.gen += 1
        for j in (self.job, self.queued):
            if j is not None:
                j.dead = True
        self.queued = None

    async def start_place(self, user_dir, read=None):
        """The place the game's level start will teleport to, read on the preloader's thread (startplace.start_point)
        and preloaded ("start") unless a teleport or close came meanwhile. Returns it, or None."""
        if read is None:
            import startplace
            read = startplace.start_point
        gen = self.gen
        try:
            got = await asyncio.get_running_loop().run_in_executor(self._executor(), read, user_dir)
        except Exception as e:      # (only the head start is lost)
            log.info("level start place: %s: %s", type(e).__name__, e)
            return None
        if got is None or self.closed or gen != self.gen:
            return None
        log.info("level start place %.5f, %.5f (%s): loaded while the game loads", *got)
        self.request(got[0], got[1], "start")
        return got

    def _abandon(self, which):
        gone = [p for p in self.plans if which(p[1])]
        if not gone:
            return
        self.plans = [p for p in self.plans if p not in gone]
        self.s.set_abandoned(add=[a for a, _at in gone])
        for _a, at in gone:
            log.info("preload: the plans for %.5f, %.5f dropped (the player goes elsewhere)", *at)

    def _executor(self):
        if self.ex is None:
            self.ex = ThreadPoolExecutor(1, thread_name_prefix="preload")
        return self.ex

    async def _drain(self):
        try:
            while self.queued is not None and not self.closed:
                job, self.queued = self.queued, None
                if job.dead:
                    continue
                self.job = job
                try:
                    await self._run(job)
                finally:
                    self.job = None
        finally:
            self.task = None

    async def _part(self, job, fn, *args):
        """fn(*args) on the preloader's thread, the job still wanted before and after."""
        if job.dead or self.closed:
            raise _Stop
        r = await asyncio.get_running_loop().run_in_executor(self._executor(), fn, *args)
        if job.dead or self.closed:
            raise _Stop
        return r

    async def _run(self, job):
        last = self.last
        if same(last, job.at) and RANK[job.mode] <= RANK[last[2]]:
            return
        self.last = (job.lat, job.lon, job.mode)
        s, la, lo = self.s, job.lat, job.lon
        t0 = time.monotonic()
        try:
            if getattr(s, "decisions_prefetch", None) is not None:
                await self._part(job, s.decisions_prefetch, la, lo)     # (signed plans: none of those planned here)
            if job.mode == "start" and self.sharing():
                self._preplan(job)
            if getattr(s, "dem", None) is not None and self.dem_zoom is not None:
                keys = s.dem.keys_around(la, lo, self.dem_reach, self.dem_zoom(la))
                job.done["dem"] = (await self._part(job, s.dem.prefetch, keys))[0]
            if getattr(s, "mapdata", None) is not None:
                spot = SimpleNamespace(frame=_frame(la, lo))
                if job.mode == "hover":
                    job.done["cells"] = await self._part(job, s.warm_place, spot, 0.0, 0.0, HOVER_M, "prefetch")
                else:
                    job.done["cells"] = await self._part(job, s.warm_place, spot, 0.0, 0.0)
            if job.mode != "hover" and getattr(s, "imagery", None) is not None and s.near_visual == "detail":
                size = self.block_m
                spot = SimpleNamespace(frame=_frame(la, lo))
                for px in self.render_px:
                    await self._part(job, s.imagery.render, spot.frame, -size / 2, -size / 2, size, px, set())
                    job.done.setdefault("photos", []).append(px)
        except _Stop:
            job.done["stopped"] = True
        except Exception as e:          # (only the head start is lost)
            log.info("preload: %s: %s", type(e).__name__, e)
        log.info("preload (%s) %.5f, %.5f: %s (%.1f s)", job.mode, la, lo, job.done, time.monotonic() - t0)

    def _preplan(self, job):
        """The first block's cells handed to the worker processes under an anchor id of its own, recorded first (event
        loop: a teleport comes before or after this, never in the middle)."""
        aid = self.s.alloc_anchor_id()
        self.plans.append((aid, job.at))
        job.done["plan_aid"] = aid
        try:
            job.done["planned"] = self.s.plan_first(SimpleNamespace(id=aid, lat=job.lat, lon=job.lon), 0.0, 0.0)
        except Exception as e:          # (the teleport's block plans them)
            log.info("preload: plans not handed out: %s: %s", type(e).__name__, e)


def _frame(lat, lon):
    import geodesy
    return geodesy.EnuFrame(lat, lon)
