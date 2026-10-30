"""Road plans shared between the server's processes (2026-10-23).

A cell's road plans (grading._Planner._plan_cell: the profiles, the kept carriageways, the tunnels' covers and open
sections) are a pure function of the cell, the map data and elevation, and the grading settings (in the frame of the
cell's planning region, geotiles.PLAN_Z: no anchor in them since 2026-10-29, so every anchor reads them), and
every process that grades needs them: both terrain-block processes (each warmed the cells ahead of the player for
itself) and the road-tile processes. On a Steam Deck a fresh place's teleport spent ~300 CPU-s planning the same
cells up to four times over, of ~1000 CPU-s for its whole world on four cores. Now the first process to plan a cell
writes it here; the others read it, or wait while one is planning it (a lock file) instead of planning it again.

The files: <cache>/plans/<run>/<signature>/<z>_<x>_<y>.npz, the arrays as npz (no pickle) and the
rest as JSON (Spec as its fields). One server run's own (RUN_ENV, set by earthd before its workers start): a plan made
while some elevation was missing stays out of the next run. A stale lock (xproc.STALE_S: a process that died planning)
is taken over; a wait longer than WAIT_S plans the cell here as before; a process waits only for a planner at least as
favoured as itself (xproc.once). Old runs' folders go at server start (prune).
"""
import json
import os
import shutil
import threading
import time

import numpy as np

import xproc

WAIT_S = 90.0
KEEP_S = 6 * 3600.0         # run folders untouched this long are removed (prune)
RUN_ENV = "BNGE_PLAN_RUN"


def _pack(arrays, g, items):
    """items (arrays of one dtype, shape (n_i, ...)) as one array g (concatenated along the first axis) and g_n
    (the n_i); a dtype per group, as the plans make them (exact round trip)."""
    items = [np.asarray(x) for x in items]
    arrays[g + "_n"] = np.array([len(x) for x in items], np.int64)
    if items:
        if len({(x.dtype.str, x.shape[1:]) for x in items}) != 1:
            raise ValueError(f"plancache: mixed arrays in {g}")
        arrays[g] = np.concatenate(items)


def _split(f, g):
    n = f[g + "_n"]
    if not len(n):
        return []
    return np.split(f[g], np.cumsum(n)[:-1])


def start_run(cache_dir):
    """earthd, before its worker processes start: this run's id in the environment they inherit; old runs pruned."""
    root = os.path.join(cache_dir, "plans")
    prune(root)
    if os.environ.get("BNGE_PLANCACHE") == "off":       # (debugging: each process plans its own, as before)
        os.environ.pop(RUN_ENV, None)
        return
    os.environ[RUN_ENV] = f"{os.getpid()}_{int(time.time())}"


FRAMES = "r10"          # the plans' frames: their planning regions' (geotiles.PLAN_Z), not an anchor's


def for_grader(cache_dir, grader, widths, wet):
    """The PlanCache of a grading.Grader (any anchor's: the plans are its store's, in the planning regions' frames;
    settings that change plans in the signature), or None outside a server run. Complete plans are kept in the revisit
    cache too (persist): a place's next visit (a game started where it was left, or anchored anywhere near) reads them
    instead of planning again."""
    run = os.environ.get(RUN_ENV)
    if not run:
        return None
    md = getattr(grader, "md", None)
    pc = PlanCache(os.path.join(cache_dir, "plans", run), f"w{int(widths is not None)}s{int(wet is not None)}")
    pc.persist = (cache_dir, (FRAMES, pc.signature,
                              md.versions() if md is not None and hasattr(md, "versions") else None))
    return pc


class PlanCache:
    def __init__(self, root, signature):
        self.dir = os.path.join(root, FRAMES, signature)
        self.signature = signature
        self.persist = None     # (cache dir, key parts) of the revisit cache's copy (for_grader), or None
        self.stats = {"read": 0, "written": 0, "waited": 0, "wait_s": 0.0}
        self._lock = threading.Lock()

    def _path(self, key):
        return os.path.join(self.dir, f"{int(key[0])}_{int(key[1])}_{int(key[2])}.npz")

    def load(self, key):
        """(plans, keeps, covers, opens) of cell key from the cache, or None."""
        from grading import Plan
        from roads import Spec
        try:
            src = self._path(key)
            if not os.path.exists(src):
                src = self._kept(key)
                if src is None:
                    return None
            with np.load(src, allow_pickle=False) as f:
                meta = json.loads(bytes(f["meta"]).decode("utf-8"))
                lists = {g: _split(f, g) for g in ("p", "z", "c", "k", "v", "o")}
                lists["b"] = _split(f, "b") if "b_n" in f.files else []
        except (OSError, ValueError, KeyError, TypeError):
            return None
        plans, cuts, banks = [], iter(lists["c"]), iter(lists["b"])
        for m, xy, z in zip(meta["plans"], lists["p"], lists["z"]):
            pl = Plan(xy, z, m["half"], tuple(m["abut"]), next(cuts) if m["cut"] else None, m["road_half"])
            pl.rid, pl.max_wall = m["rid"], m["max_wall"]
            pl.bank = next(banks) if m.get("bank") else None
            plans.append((m["fid"], pl))
        keeps = list(zip(lists["k"], meta["keeps"]))
        covers = list(zip(lists["v"], meta["covers"]))
        opens = [(xy, Spec(**{**sp, "sides": tuple(sp["sides"]) if sp.get("sides") is not None else None}))
                 for xy, sp in zip(lists["o"], meta["opens"])]   # (JSON gave sides back as a list)
        with self._lock:
            self.stats["read"] += 1
        return plans, keeps, covers, opens

    def _revisit_key(self, key):
        import revisit
        return revisit.key("plans", *self.persist[1], (int(key[0]), int(key[1]), int(key[2])))

    def _where(self, key):
        """revisit.where of cell key's plans (the map's cache view), or None."""
        import mapcells
        import revisit
        try:
            w, s, e, n = mapcells.CellKey(int(key[0]), int(key[1]), int(key[2])).bounds()
            _frames, sig, versions = self.persist[1]
            # (the sidecar's place: the cell's own centre, no anchor)
            return revisit.where((s + n) / 2, (w + e) / 2, s, w, n, e, revisit.sig(plans=sig, md=versions))
        except Exception:
            return None

    def _kept(self, key):
        """The revisit cache's copy of cell key's plans (a file-like npz), or None."""
        if self.persist is None:
            return None
        import io
        import revisit
        got = revisit.load(self.persist[0], "plans", self._revisit_key(key))
        if got is None or "plans.npz" not in got[0]:
            return None
        return io.BytesIO(got[0]["plans.npz"])

    def store(self, key, plans, keeps, covers, opens, persist=False):
        """Write cell key's plans for the run's other processes; persist: also into the revisit cache (complete plans:
        nothing they read failed or was missing, Grader._plan_cell)."""
        meta = {"plans": [{"fid": fid, "half": p.half, "abut": list(p.abut), "road_half": p.road_half,
                           "rid": int(p.rid), "max_wall": float(p.max_wall), "cut": p.cut is not None,
                           "bank": getattr(p, "bank", None) is not None}
                          for fid, p in plans],
                "keeps": [float(hw) for _xy, hw in keeps], "covers": [float(w) for _xy, w in covers],
                "opens": [{k: (v.item() if hasattr(v, "item") else v) for k, v in sp._asdict().items()}
                          for _xy, sp in opens]}
        arrays = {"meta": np.frombuffer(json.dumps(meta).encode("utf-8"), np.uint8)}
        try:
            _pack(arrays, "p", [p.xy for _f, p in plans])
            _pack(arrays, "z", [p.z for _f, p in plans])
            _pack(arrays, "c", [p.cut for _f, p in plans if p.cut is not None])
            _pack(arrays, "b", [p.bank for _f, p in plans if getattr(p, "bank", None) is not None])
            _pack(arrays, "k", [xy for xy, _hw in keeps])
            _pack(arrays, "v", [xy for xy, _w in covers])
            _pack(arrays, "o", [xy for xy, _sp in opens])
            p = self._path(key)
            os.makedirs(self.dir, exist_ok=True)
            tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as fh:
                np.savez(fh, **arrays)
            os.replace(tmp, p)
            if persist and self.persist is not None:
                import revisit
                with open(p, "rb") as fh:
                    revisit.store(self.persist[0], "plans", self._revisit_key(key), {"plans.npz": fh.read()},
                                  where=self._where(key))
        except (OSError, ValueError, TypeError):
            return False
        with self._lock:
            self.stats["written"] += 1
        return True

    def claim(self, key):
        """Before planning cell key: (state, False) when it is in the cache (or another process planned it while we
        waited); (None, True) when this process plans it (call release after store); (None, False) when it plans it
        without the lock (xproc.once)."""
        t0 = time.monotonic()

        def waiting():
            with self._lock:
                self.stats["waited"] += 1
        got, mine = xproc.once(self._path(key) + ".lock", lambda: self.load(key), WAIT_S, on_wait=waiting)
        if got is not None and time.monotonic() - t0 > xproc.POLL_S:
            with self._lock:
                self.stats["wait_s"] += time.monotonic() - t0
        return got, mine

    def release(self, key):
        xproc.release(self._path(key) + ".lock")


def _newest(d, depth=2):
    """The newest mtime of folder d and the folders up to depth levels below it (a run's anchor and signature
    folders: new cells change only their own folder's time)."""
    t = os.path.getmtime(d)
    if depth:
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        t = max(t, _newest(e.path, depth - 1))
        except OSError:
            pass
    return t


def prune(root, keep_s=KEEP_S, now=None):
    """Remove the run folders under root not written to for keep_s (server start)."""
    now = time.time() if now is None else now
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    n = 0
    for name in names:
        d = os.path.join(root, name)
        try:
            if now - _newest(d) > keep_s:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            continue
    return n
