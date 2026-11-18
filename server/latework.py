"""Road content a session built short, built again in the background once its inputs are in (2026-11-13).

A first visit plans the cells whose 3DEP lidar (roadz) is not on disk yet on the rule (the provisional data), and keeps
nothing made on it (revisit faults): a cold Manhattan visit left 53-65 of its ~100 cells on the rule and planned all
of them again on the next visit (graded first block 63 s instead of 23 s). The server's own process now reads the
lidar ahead (roadz.RoadZ.prefetch) and nothing waits for it, so the rule cells grow; this is what gets them kept.

Every map cell whose plans this run made short (its ".short" marker in the run's plan cache: Server.late_scan) is
planned again (blockworker.late_plan), and every road geotile whose content a session built and did not keep
(Server.build_roads_geo -> note) is built again, once the lidar of their cells is in (on disk, or its read failed;
the cells first: the blocks and tiles read them), in processes of their own at the lowest priority
(earthd's late pool: blockworker.init_late, its own plan and lidar-decision run, so it reads none of the run's rule
decisions nor its short plans and facts, and waits for any lidar it still lacks: roadz.precache_gaps_wait), nearest
the player first, one at a time. What it makes complete (the cells' plans, their facts, the geotile's content) is kept
in the revisit cache as a pre-cache's is (blockworker.prebuild_geo), so:
  - the next visit (a teleport back, the next game) reads it: warm where the session had time;
  - this session's processes read the kept plans of a cell they had not planned, and of one they planned short
    when they read it from disk again (plancache.PlanCache.load);
  - nothing is swapped under the player: the game keeps what it was sent until a tile or block is built again.
Never kept when still short (the build's own faults): tried again LATE_RETRY_S later, LATE_TRIES times at most.
BNGE_LATE=off: none. Glue only: the builds are the road processes' own code."""
import logging
import math
import os
import threading
import time

log = logging.getLogger("earthd.latework")

ON = os.environ.get("BNGE_LATE", "on") != "off"
LATE_TRIES = 3              # builds of a geotile still short before it is left to the next visit
LATE_RETRY_S = 60.0         # s after a short build before the next (times the tries so far)
LATE_FAR_M = 6000.0         # m: a geotile this far from the player when its turn comes is dropped (the player left)
LATE_PAD_M = 300.0          # m round a geotile whose cells' lidar is waited for (its roads' neighbours: CLIP_PAD_M ...)
LATE_POLL_S = 2.0           # s between looks when nothing is due
LATE_WAIT_S = 5.0           # s before a geotile whose lidar is not in yet is looked at again
LATE_MAX = 600              # items queued at most (the nearest kept)
LATE_SCAN_S = 10.0          # s between looks for the run's plan cells made short (scan)
CELL_FIRST_M = 500.0        # m: a cell's plans before a geotile this much nearer (the blocks and tiles read them)


def _name(k):
    return f"road geotile {k[1]}_{k[2]}" if k[0] == "g" else f"cell {k[1]}/{k[2]}/{k[3]}'s plans"


def processes():
    """The late pool's processes (builds at once): BNGE_LATE_PROCS, else 2 on 12 cores or more, else 1."""
    try:
        return max(1, int(os.environ.get("BNGE_LATE_PROCS") or 0)) if os.environ.get("BNGE_LATE_PROCS") else (
            2 if (os.cpu_count() or 4) >= 12 else 1)
    except ValueError:
        return 1


class LateWork:
    """Items: ("g", gx, gy) a road geotile's content, ("c", z, x, y) a map cell's road plans. build(item) -> True
    (kept) / False (still short) / None (could not run): the late pool's job (earthd.Server.late_build); kept(item) ->
    bool: in the revisit cache already; cells(item) -> the map cells whose lidar it reads; lidar: roadz.RoadZ (or None)
    whose prefetch state says when they are in; scan() -> items to note (the run's plan cells made short), asked
    every LATE_SCAN_S."""

    def __init__(self, build, kept, cells, lidar=None, workers=1, scan=None, idle=None):
        self.build, self.kept, self.cells, self.lidar, self.scan = build, kept, cells, lidar, scan
        self.idle = idle            # () -> bool: the live work leaves room (no build started meanwhile otherwise)
        self.where = None           # item -> m from the player (the server's latest focus), or None
        self.q = {}                 # item -> [tries, not before (monotonic)]
        self.busy = set()           # items being looked at / built now
        self.cv = threading.Condition()
        self.thread = None
        self.workers = max(1, int(workers))     # builds at once (the late pool's processes)
        self.stats = {"noted": 0, "kept": 0, "short": 0, "given_up": 0, "dropped": 0, "already": 0, "busy_s": 0.0}
        self.stop = False
        self.seen = set()           # items scan() named once (a cell's short plans file stays: noted once)
        self.scanned = 0.0

    def note(self, gx, gy):
        """Geotile (gx, gy)'s content was built this session: built again later if it was not kept."""
        self.note_item(("g", int(gx), int(gy)))

    def note_item(self, k):
        if not ON:
            return
        k = tuple(k)
        with self.cv:
            if k in self.q:
                return
            if len(self.q) >= LATE_MAX:
                far = self._farthest()
                if far is None:
                    return
                del self.q[far]
            self.q[k] = [0, time.monotonic()]
            self.stats["noted"] += 1
            self.start()            # (the lock is re-entrant: threading.Condition's RLock)
            self.cv.notify()

    def _scan(self):
        """Note the items scan() names (one thread at a time, every LATE_SCAN_S)."""
        if self.scan is None:
            return
        with self.cv:
            if time.monotonic() - self.scanned < LATE_SCAN_S:
                return
            self.scanned = time.monotonic()
        try:
            got = [tuple(k) for k in self.scan()]
        except Exception as e:
            log.debug("late work: scan: %s: %s", type(e).__name__, e)
            return
        for k in got:
            with self.cv:
                new = k not in self.seen
                self.seen.add(k)
            if new:
                self.note_item(k)

    def start(self):
        """The workers' threads (note starts them too): scan() asked from now on."""
        with self.cv:
            if self.thread is None:
                for _ in range(self.workers):
                    self.thread = threading.Thread(target=self._loop, name="late-work", daemon=True)
                    self.thread.start()

    def _dist(self, k):
        w = self.where
        if w is None:
            return 0.0
        try:
            return float(w(k)) - (CELL_FIRST_M if k[0] == "c" else 0.0)
        except Exception:
            return math.inf

    def _farthest(self):
        if not self.q:
            return None
        return max(self.q, key=lambda k: (self._dist(k), k))

    def ready(self, k):
        """Is the lidar of geotile k's cells in (on disk, or its read failed: the build waits for or does without
        what is left)? Cells never asked for are asked of the prefetch."""
        prov = self.lidar
        if prov is None:
            return True
        waiting, todo = False, []
        for c in self.cells(k):
            if not prov.lidar_wanted(c) or prov.lidar_on_disk(c):
                continue
            st = prov.prefetch_state(c)
            if st in ("done", "failed"):
                continue
            waiting = True
            if st is None:
                todo.append(c)
        if todo:
            prov.prefetch(todo)
        return not waiting

    def _pick(self):
        """The nearest geotile due and ready (under the lock), or None; the ones far behind dropped."""
        now = time.monotonic()
        best = None
        for k, (tries, due) in sorted(self.q.items(), key=lambda kv: (self._dist(kv[0]), kv[0])):
            if self._dist(k) > LATE_FAR_M:
                del self.q[k]
                self.stats["dropped"] += 1
                continue
            if best is None and due <= now and k not in self.busy:
                best = k
        return best

    def _loop(self):
        while not self.stop:
            self._scan()
            try:
                room = self.idle is None or self.idle()
            except Exception:
                room = True
            if not room:            # (the player's current needs first: the near ring, a teleport's first block)
                with self.cv:
                    self.cv.wait(LATE_POLL_S)
                continue
            with self.cv:
                k = self._pick()
                if k is None:
                    self.cv.wait(LATE_POLL_S)
                    continue
                tries = self.q[k][0]
                self.busy.add(k)
            try:
                self._one(k, tries)
            finally:
                with self.cv:
                    self.busy.discard(k)

    def _one(self, k, tries):
        """Look at geotile k (tries builds so far): waits on, already kept, or built (outside the lock)."""
        try:
            if not self.ready(k):
                with self.cv:
                    if k in self.q:
                        self.q[k][1] = time.monotonic() + LATE_WAIT_S
                return
            if self.kept(k):
                with self.cv:
                    self.q.pop(k, None)
                    if tries == 0:
                        self.stats["already"] += 1
                return
            t0 = time.monotonic()
            ok = self.build(k)
            took = time.monotonic() - t0
        except Exception as e:      # (tried again later)
            log.debug("late work: geotile %s: %s: %s", k, type(e).__name__, e)
            ok, took = False, 0.0
        with self.cv:
            self.stats["busy_s"] += took
            if ok is None:          # (could not run now: the pool gone)
                if k in self.q:
                    self.q[k][1] = time.monotonic() + LATE_RETRY_S
                return
            if ok:
                self.q.pop(k, None)
                self.stats["kept"] += 1
                log.info("late work: %s built again and kept (%.1f s, try %d; %d kept, %d queued)", _name(k), took,
                         tries + 1, self.stats["kept"], len(self.q))
                return
            self.stats["short"] += 1
            tries += 1
            if tries >= LATE_TRIES:
                self.q.pop(k, None)
                self.stats["given_up"] += 1
                log.info("late work: %s still short after %d builds: left to the next visit", _name(k), tries)
            elif k in self.q:
                self.q[k] = [tries, time.monotonic() + LATE_RETRY_S * tries]
