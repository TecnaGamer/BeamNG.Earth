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
import logging
import json
import hashlib
import os
import shutil
import threading
import time

import numpy as np

import xproc

log = logging.getLogger("earthd.plancache")

WAIT_S = 90.0
LONG_WAIT_S = 20.0          # s: a wait for another process's plans this long is logged (claim)
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


# The plans' output format: their key in the revisit cache (kept across updates) and in baked packs (bakepack.py), in
# place of the code hash. Bump it whenever a change makes other plans (grading, its inputs' rules), or players keep the
# old ones; tests/test_bake_formats.py fails until it is bumped (its fixture's digest is stored with the number).
PLANS_FORMAT = 14     # 14: past an abutment end the ground falls away under a still-climbing deck (ABUT_UNDER_GRADE)
                      # 13: centimetre-scale dirt wear and bounded edge displacement from random banking
                      # 12: dirt roads toned down (#783, #784; dev); PR #811's own 12: bounded, local dirt damage, independent of real
                      #     grades and DEM extensions
                      # 11: pass roads with two lanes graded as mountain roads again (the Stelvio); 10: worn dirt roads (PR #677: damage, lean, 2 m vertices, 2.5 m smoothing of minor dirt); 9: roundabout rings level across (RING_LEVEL_ON); 8: PR #753 (a deck end's approaches rise with it, slip roads tied to their overpass road); 7: PR #471 and #686 lanes (wider / narrower roads graded); 6: the dip under a deck takes its ends on their embankments (#684's fill); 5: one half width a bridge (each was appended twice: under-deck ground and carriageways read the wrong bridge's); 4: junction pads, steep side-road ramps, rail bridges the DEM predates (PR #482/#671); 3:
                      # bridge ends on embankments (#694), the ground under decks (#698), junction corridors (#682)


def plans_format():
    """The plans' revisit key format: PLANS_FORMAT and the road facts' (roads.FACTS_FORMAT: the plans are made from
    them), "<plans>.<facts>"."""
    import roads
    return f"{PLANS_FORMAT}.{roads.FACTS_FORMAT}"


FRAMES = "r10"          # the plans' frames: their planning regions' (geotiles.PLAN_Z), not an anchor's


def for_grader(cache_dir, grader, widths, wet):
    """The PlanCache of a grading.Grader (any anchor's: the plans are its store's, in the planning regions' frames;
    settings that change plans in the signature), or None outside a server run. Complete plans are kept in the revisit
    cache too (persist): a place's next visit (a game started where it was left, or anchored anywhere near) reads them
    instead of planning again."""
    md = getattr(grader, "md", None)
    signature = f"w{int(widths is not None)}s{int(wet is not None)}"
    bundle = os.environ.get("BNGE_WORLD_BUNDLE")
    if bundle:
        from worldbundle import PinnedPlanCache
        versions = getattr(md, "bundle_versions", None) or getattr(md, "versions", None)
        return PinnedPlanCache(bundle, signature, versions() if versions is not None else {})
    run = os.environ.get(RUN_ENV)
    if not run:
        return None
    pc = PlanCache(os.path.join(cache_dir, "plans", run), signature)
    # Resolve after generation: loading source layers can discover their actual shard releases.
    pc.source_versions = getattr(md, "bundle_versions", None) or getattr(md, "versions", None)
    pc.persist = (cache_dir, (FRAMES, pc.signature,
                              md.versions() if md is not None and hasattr(md, "versions") else None))
    return pc


class PlanCache:
    def __init__(self, root, signature):
        self.dir = os.path.join(root, FRAMES, signature)
        self.signature = signature
        self.persist = None     # (cache dir, key parts) of the revisit cache's copy (for_grader), or None
        self.source_versions = None  # callable for the versions actually known after a complete cell was built
        self.stats = {"read": 0, "written": 0, "waited": 0, "wait_s": 0.0}
        self._lock = threading.Lock()

    def _path(self, key):
        return os.path.join(self.dir, f"{int(key[0])}_{int(key[1])}_{int(key[2])}.npz")

    def load(self, key):
        """(plans, keeps, covers, opens) of cell key from the cache, or None."""
        from grading import Plan, set_widen
        from roads import Spec
        try:
            src = self._path(key)
            if not os.path.exists(src):
                src = self._kept(key)
                if src is None:
                    src = self._baked(key)
                if src is None:
                    return None
            with np.load(src, allow_pickle=False) as f:
                meta = json.loads(bytes(f["meta"]).decode("utf-8"))
                lists = {g: _split(f, g) for g in ("p", "z", "c", "k", "v", "o")}
                lists["b"] = _split(f, "b") if "b_n" in f.files else []
                lists["w"] = _split(f, "w") if "w_n" in f.files else []
        except (OSError, ValueError, KeyError, TypeError):
            return None
        plans, cuts, banks, widens = [], iter(lists["c"]), iter(lists["b"]), iter(lists["w"])
        for m, xy, z in zip(meta["plans"], lists["p"], lists["z"]):
            pl = Plan(xy, z, m["half"], tuple(m["abut"]), next(cuts) if m["cut"] else None, m["road_half"])
            pl.rid, pl.max_wall = m["rid"], m["max_wall"]
            pl.steep = bool(m.get("steep", False))
            pl.bank = next(banks) if m.get("bank") else None
            if m.get("widen"):
                set_widen(pl, next(widens))
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
        # (by PLANS_FORMAT, not the code: kept across updates, the user, 2026-11-02)
        return revisit.format_key("plans", plans_format(), *self.persist[1],
                                  (int(key[0]), int(key[1]), int(key[2])))

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

    def has(self, key):
        """Are cell key's plans on disk (this run's, or the revisit cache's copy)? A stat or two, nothing read."""
        if os.path.exists(self._path(key)):
            return True
        if self.persist is None:
            return False
        import revisit
        if os.path.exists(revisit._path(self.persist[0], "plans", self._revisit_key(key))):
            return True
        import bakepack
        k = bakepack.plans_key(self.signature, key)
        return any(k in p.items and bakepack._matches(p, self._bake_expect()) for p in bakepack.packs(self.persist[0]))

    def _bake_expect(self):
        import bakepack
        return {"bake_format": bakepack.BAKE_FORMAT, "plans_format": PLANS_FORMAT, "frames": FRAMES,
                "signature": self.signature,
                "versions": json.loads(json.dumps(self.persist[1][2], sort_keys=True))}

    def _baked(self, key):
        """A baked pack's plans of cell key (bakepack.py: made offline for this map data and signature), or None."""
        if self.persist is None:
            return None
        import io
        import bakepack
        got = bakepack.find(self.persist[0], bakepack.plans_key(self.signature, key), self._bake_expect())
        if got is None:
            return None
        with self._lock:
            self.stats["baked"] = self.stats.get("baked", 0) + 1
        return io.BytesIO(got)

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
                           "bank": getattr(p, "bank", None) is not None, "steep": bool(getattr(p, "steep", False)),
                           "widen": getattr(p, "widen", None) is not None}
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
            _pack(arrays, "w", [p.widen for _f, p in plans if getattr(p, "widen", None) is not None])
            _pack(arrays, "k", [xy for xy, _hw in keeps])
            _pack(arrays, "v", [xy for xy, _w in covers])
            _pack(arrays, "o", [xy for xy, _sp in opens])
            p = self._path(key)
            os.makedirs(self.dir, exist_ok=True)
            tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as fh:
                np.savez(fh, **arrays)
            provenance = None
            if persist:
                from worldbundle import canonical, completion_record
                with open(tmp, "rb") as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
                try:
                    sources = self.source_versions() if self.source_versions is not None else {}
                    provenance = canonical(completion_record(key, self.signature, sources, digest))
                except (OSError, ValueError, TypeError):
                    # Optional export provenance must not disable normal local caching. Without a
                    # valid marker this cell remains unexportable, including any stale earlier marker.
                    pass
            os.replace(tmp, p)
            # Export only plans whose complete-input condition the grader actually observed.
            # The digest binds the marker to this version; an interrupted replacement fails closed.
            marker = p + ".complete"
            if provenance is not None:
                with open(tmp, "wb") as fh:
                    fh.write(provenance)
                os.replace(tmp, marker)
            else:
                try:
                    os.remove(marker)
                except FileNotFoundError:
                    pass
            if persist and self.persist is not None:
                import revisit
                with open(p, "rb") as fh:
                    revisit.store(self.persist[0], "plans", self._revisit_key(key), {"plans.npz": fh.read()},
                                  where=self._where(key), code=revisit.format_marker("plans", plans_format()))
                import mpshare          # (a BeamMP session shares them: the hub sends them to the server)
                mpshare.note_built(self.persist[0], self.signature, key)
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
        if self.persist is not None:
            import mpshare      # (a BeamMP session: the server is sending this cell's plans; waited for a little)
            if mpshare.coming(self.persist[0], self.signature, key):
                while time.monotonic() - t0 < mpshare.COMING_WAIT_S:
                    if self.has(key):
                        got = self.load(key)
                        if got is not None:
                            with self._lock:
                                self.stats["shared"] = self.stats.get("shared", 0) + 1
                            return got, False
                    if not mpshare.coming(self.persist[0], self.signature, key):
                        break   # (missed: planned here)
                    time.sleep(0.1)

        holder = []

        def waiting():
            with self._lock:
                self.stats["waited"] += 1
            h = xproc._holder(self._path(key) + ".lock")
            holder.append(h[2] if h else None)
        got, mine = xproc.once(self._path(key) + ".lock", lambda: self.load(key), WAIT_S, on_wait=waiting)
        waited = time.monotonic() - t0
        if got is not None and waited > xproc.POLL_S:
            with self._lock:
                self.stats["wait_s"] += waited
        if waited > LONG_WAIT_S:            # (a road tile "may be stuck": whose plan it waited for)
            log.warning("cell %s's plans: waited %.0f s for process %s making them (%s)", key, waited,
                        holder[0] if holder else "?", "they came" if got is not None else "made here after all")
        return got, mine

    def release(self, key):
        xproc.release(self._path(key) + ".lock")


    # ---- carriageway regions (grading._Planner._cw_region): solved once a run, read by every process ----

    def _region_path(self, tag, home):
        return os.path.join(self.dir, "cw", tag, f"{int(home[0])}_{int(home[1])}_{int(home[2])}.npz")

    def region_load(self, tag, home):
        """{"parts": [(key, line, is_bridge, cls, fid)], "prof": {key: (xy, z)}} of region home (planner frame tag) or
        None."""
        try:
            with np.load(self._region_path(tag, home), allow_pickle=False) as f:
                meta = json.loads(bytes(f["meta"]).decode("utf-8"))
                lines, cx, cz = _split(f, "l"), _split(f, "x"), _split(f, "q")
        except (OSError, ValueError, KeyError, TypeError):
            return None
        parts, prof = [], {}
        for m, ln in zip(meta["parts"], lines):
            key = tuple([m["key"][0]] + [float(v) for v in m["key"][1:]])
            parts.append((key, ln, bool(m["br"]), m["cls"], m["fid"], float(m.get("lv", 1.0 if m["br"] else 0.0)),
                          float(m.get("half", 5.0))))
            if m["chain"] >= 0:
                prof[key] = (cx[m["chain"]], cz[m["chain"]])
        return {"parts": parts, "prof": prof}

    def region_store(self, tag, home, got):
        chains, idx = [], {}
        for key, (xy, z) in got["prof"].items():
            if id(xy) not in idx:
                idx[id(xy)] = len(chains)
                chains.append((xy, z))
        meta = {"parts": [{"key": list(q[0]), "br": bool(q[2]), "cls": q[3],
                           "fid": q[4] if isinstance(q[4], (str, int, type(None))) else str(q[4]),
                           "lv": float(q[5]) if len(q) > 5 else (1.0 if q[2] else 0.0),
                           "half": float(q[6]) if len(q) > 6 else 5.0,
                           "chain": idx[id(got["prof"][q[0]][0])] if q[0] in got["prof"] else -1}
                          for q in got["parts"]]}
        arrays = {"meta": np.frombuffer(json.dumps(meta).encode("utf-8"), np.uint8)}
        try:
            _pack(arrays, "l", [np.asarray(q[1], np.float64) for q in got["parts"]])
            _pack(arrays, "x", [np.asarray(xy, np.float64) for xy, _z in chains])
            _pack(arrays, "q", [np.asarray(z, np.float64) for _xy, z in chains])
            p = self._region_path(tag, home)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as fh:
                np.savez(fh, **arrays)
            os.replace(tmp, p)
        except (OSError, ValueError, TypeError):
            return False
        return True

    def region_claim(self, tag, home):
        """As claim, for a carriageway region."""
        return xproc.once(self._region_path(tag, home) + ".lock", lambda: self.region_load(tag, home), WAIT_S)

    def region_release(self, tag, home):
        xproc.release(self._region_path(tag, home) + ".lock")


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
