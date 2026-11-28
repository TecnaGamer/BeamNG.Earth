#!/usr/bin/env python3
"""Which code last wrote each height sample of a BeamNG.Earth terrain block: a map coloured by hash(call stack) % 256.

Earth's own blockworker.build runs, unchanged, on the same disk cache the game's server used (the Earth tree is only
imported, never modified). Before it runs, every function and method of the Earth server modules it has loaded, and
every native kernel of earthfast, is wrapped. A wrapped call is traced when two of its float array arguments are
coordinates of the block's samples: latitude / longitude, the anchor frame's x / y, or a planning region's frame's x / y
(the 1 m lattice, matched exactly). Its other float arrays of the same size are the heights it was given; after the call:

  - an argument array changed in place (the native kernels), or
  - its returned array of the same size (compared with the heights it was given; with none given, a source),

is a write to the samples whose value changed. Each sample keeps the last write: a nested call keeps it when the outer
call returns the value that nested call wrote; an outer call that changed the value itself takes it; a sample a call
leaves as it was given goes back to its writer from before the call (an array of weights or masks returned by a call
inside is not a write). A source (no heights given) only claims samples that have no writer yet.

The colour of a sample is PALETTE[crc32(stack) % 256], the stack being the Earth frames of the call, innermost first
(file:function:line). Writes outside Earth's files are not seen by the tracing: the game's runtime shaping, EarthRally's ground grader,
is run on the built block afterwards with --rally (rally_grader.py: the mod's own shape.lua on the stages of --geometry, as the game
would, without the game's colliders), and every sample it moves is drawn over Earth's writers in RALLY_RGB (magenta), the points of
the line it keeps as Earth made them (beside a tunnel) as cyan dots and the bridge or tunnel spans it skips as yellow ones.

  writers.png      the block (and --box zoomed), coloured by writer, the tunnels' holes of the .ter in black
  writers.txt      every writer: colour index, samples (block, box), its stack; the check of the build against the
                   session's PNG and of the writers' last values against the heights the block was built from
  rally_profile.png  (--rally) along each stage line in the box: Earth's ground and the ground after the grader, the stretches it
                   keeps (beside a tunnel) and skips (a span) shaded
  undone.png       the samples where a call gave back the value it was given after a call inside it had written there
                   (a put-back), coloured by the call that gave it back
  writers.npz      label (int, index into writers.txt's list; -1 none), stacks (label -> its stack), undone_by /
                   undone_what, the heights, the holes, rally (--rally: the grader's change of each sample, m)

  python server/evals/earth_writers.py --earth . --anchor 42.17,13.82 --block 640,896 \
      --box 470,780,680,960 --session <game user folder>/temp/beamngEarth/<session> \
      --geometry server/evals/geometry/ss1.json --rally --out writers-out
"""
import argparse
import colorsys
import functools
import inspect
import json
import os
import sys
import threading
import types
import zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import earth_trace  # noqa: E402  (load_earth, the session's block, the log's block parameters)

# coordinate helpers: never wrapped (the tracker uses them)
SKIP_MODULES = {"geodesy", "geotiles", "mapcells", "mercator", "runtimeprofiler", "staging", "budget", "perfstats",
                "telemetry", "sampler", "ioprofiler", "stackdump", "protocol", "netfetch", "netstate", "cache"}
EXACT_ENU = 1e-6        # m: a sample's x / y in the anchor frame (the lattice itself)
EXACT_GEO = 1e-4        # m: through latitude / longitude and back
EXACT_REGION = 1e-3     # m: through a planning region's frame
RALLY_RGB = (1.0, 0.0, 1.0)     # the samples EarthRally's grader moves (--rally)
RALLY_MIN_M = 0.02              # ... by this much at least (shape.MIN_CHANGE_M)


def palette(n=256):
    out = np.zeros((n, 3))
    for i in range(n):
        h = (i * 0.6180339887) % 1.0
        s = 0.55 + 0.45 * ((i >> 1) & 1)
        v = 0.65 + 0.35 * (i & 1)
        out[i] = colorsys.hsv_to_rgb(h, s, v)
    return out


PALETTE = palette()


class Tracker:
    def __init__(self, frame, cx, cy, size, server_dir, region_frames):
        self.frame = frame
        self.x0 = cx - size / 2.0
        self.y0 = cy - size / 2.0
        self.n = size
        self.server_dir = os.path.normcase(os.path.abspath(server_dir))
        self.region_frames = region_frames          # () -> [EnuFrame]
        self.lock = threading.RLock()
        self.active = False
        self.reset()

    def reset(self):
        n2 = self.n * self.n
        self.label = np.full(n2, -1, np.int32)
        self.undone_by = np.full(n2, -1, np.int32)      # the call that last gave back a value written inside it
        self.undone_what = np.full(n2, -1, np.int32)    # ... the writer whose value it gave back
        self.val = np.full(n2, np.nan)
        self.serial = np.zeros(n2, np.int64)
        self.counter = 0
        self.stacks = []
        self.stack_id = {}
        self.calls = 0

    # -- which samples are these coordinates
    def _lattice(self, a, b, tol):
        fi = a - self.x0
        fj = b - self.y0
        i = np.rint(fi)
        j = np.rint(fj)
        ok = (np.abs(fi - i) < tol) & (np.abs(fj - j) < tol) & (i >= 0) & (i < self.n) & (j >= 0) & (j < self.n)
        idx = np.where(ok, j * self.n + i, -1).astype(np.int64)
        return idx if ok.any() else None

    def pixels(self, a, b):
        """Sample indices (-1: not a sample) of the points (a, b), or None when they are not the block's samples."""
        a = np.asarray(a, np.float64).ravel()
        b = np.asarray(b, np.float64).ravel()
        if a.size == 0 or a.size != b.size or not (np.isfinite(a[0]) and np.isfinite(b[0])):
            return None
        a0, b0 = a[:1], b[:1]
        # the anchor frame
        if self._lattice(a0, b0, EXACT_ENU) is not None:
            return self._lattice(a, b, EXACT_ENU)
        # latitude, longitude
        if abs(a0[0]) <= 90.0 and abs(b0[0]) <= 180.0:
            e, n, _ = self.frame.geodetic_to_enu(a0, b0, 0.0)
            if self._lattice(np.asarray(e), np.asarray(n), EXACT_GEO) is not None:
                e, n, _ = self.frame.geodetic_to_enu(a, b, 0.0)
                return self._lattice(np.asarray(e), np.asarray(n), EXACT_GEO)
        # a planning region's frame
        regions = self.region_frames()
        if not regions:
            return None
        import geotiles
        for rf in regions:
            xy = geotiles.world_xy_map(rf, self.frame, np.c_[a0, b0])
            if self._lattice(xy[:, 0], xy[:, 1], EXACT_REGION) is not None:
                xy = geotiles.world_xy_map(rf, self.frame, np.c_[a, b])
                return self._lattice(xy[:, 0], xy[:, 1], EXACT_REGION)
        return None

    def find_coords(self, arrs):
        """(position of the first coordinate array, of the second or None for an (n, 2) one, sample indices)."""
        for k in range(len(arrs)):
            x = arrs[k]
            if x.ndim == 2 and x.shape[1] == 2 and x.shape[0] > 0:
                got = self.pixels(x[:, 0], x[:, 1])
                if got is not None:
                    return k, None, got
            if k + 1 < len(arrs) and arrs[k + 1].size == x.size:
                got = self.pixels(x, arrs[k + 1])
                if got is not None:
                    return k, k + 1, got
        return None

    def stack(self, name):
        frames = [name]
        f = sys._getframe(2)
        while f is not None:
            fn = os.path.normcase(os.path.abspath(f.f_code.co_filename))
            if os.path.dirname(fn) == self.server_dir:
                frames.append(f"{os.path.basename(fn)}:{f.f_code.co_name}:{f.f_lineno}")
            f = f.f_back
        key = tuple(frames)
        with self.lock:
            sid = self.stack_id.get(key)
            if sid is None:
                sid = self.stack_id[key] = len(self.stacks)
                self.stacks.append(key)
        return sid

    def resolve(self, pix, new, base, sid, s0, snap):
        """The samples pix (indices, -1 none) after a call that began at serial s0: new their value from it, base the
        value they were given (None: a source), snap (label, val, serial) of them before the call."""
        ok = pix >= 0
        if not ok.any():
            return
        p = pix[ok]
        new = np.asarray(new, np.float64).ravel()[ok]
        b = None if base is None else np.asarray(base, np.float64).ravel()[ok]
        lab0, val0, ser0 = (x[ok] for x in snap)
        with self.lock:
            inner = (self.serial[p] > s0) & (self.val[p] == new)
            if b is None:
                mine = ~inner & (lab0 < 0) & np.isfinite(new)
            else:
                mine = ~inner & (new != b) & ~(np.isnan(new) & np.isnan(b))
            back = ~inner & ~mine
            if mine.any():
                q = p[mine]
                self.counter += 1
                self.label[q] = sid
                self.val[q] = new[mine]
                self.serial[q] = self.counter
            undo = back & (self.serial[p] > s0) & (b is not None)
            if undo.any():
                q = p[undo]
                self.undone_what[q] = self.label[q]
                self.undone_by[q] = sid
            if back.any():
                q = p[back]
                self.label[q] = lab0[back]
                self.val[q] = val0[back]
                self.serial[q] = ser0[back]

    def snapshot(self, pix):
        ok = pix >= 0
        safe = np.where(ok, pix, 0)
        with self.lock:
            return self.label[safe].copy(), self.val[safe].copy(), self.serial[safe].copy()


TRACK = None
_inside = threading.local()


def wrap(fn, name):
    if getattr(fn, "__earth_writers__", False):
        return fn

    @functools.wraps(fn)
    def w(*args, **kw):
        T = TRACK
        if T is None or not T.active or getattr(_inside, "on", False):
            return fn(*args, **kw)
        caller = os.path.splitext(os.path.basename(sys._getframe(1).f_code.co_filename))[0]
        if caller in SKIP_MODULES:          # (the coordinate helpers' own kernels: not height writes)
            return fn(*args, **kw)
        _inside.on = True                   # (the tracker's own geodesy runs earthfast too: not traced)
        try:
            arrs = [x for x in (*args, *kw.values()) if isinstance(x, np.ndarray) and x.dtype.kind == "f" and x.size]
            got = None
            if arrs and (len(arrs) >= 2 or (arrs[0].ndim == 2 and arrs[0].shape[-1] == 2)):
                try:
                    got = T.find_coords(arrs)
                except Exception:
                    got = None
        finally:
            _inside.on = False
        if got is None:
            return fn(*args, **kw)
        ka, kb, pix = got
        npts = pix.size
        coords = {ka} | ({kb} if kb is not None else set())
        vals = [x for k, x in enumerate(arrs) if k not in coords and x.size == npts]
        before = [x.copy() for x in vals]
        snap = T.snapshot(pix)
        s0 = T.counter
        r = fn(*args, **kw)
        _inside.on = True
        try:
            T.calls += 1
            if T.calls % 200 == 0:
                print(f"  {T.calls} traced calls ({name})", flush=True)
            sid = None
            for x, x0 in zip(vals, before):         # in place
                if not np.array_equal(x, x0, equal_nan=True):
                    sid = T.stack(name) if sid is None else sid
                    T.resolve(pix, x, x0, sid, s0, snap)
                    snap = T.snapshot(pix)
            if isinstance(r, np.ndarray) and r.dtype.kind == "f" and r.size == npts:
                sid = T.stack(name) if sid is None else sid
                T.resolve(pix, r, before[0] if before else None, sid, s0, snap)
        finally:
            _inside.on = False
        return r
    w.__earth_writers__ = True
    w.__wrapped_original__ = fn
    return w


def instrument(server_dir, done):
    """Wrap the functions of the Earth modules loaded (not in done yet) and earthfast's kernels; references to the
    originals in every Earth module's globals are pointed at the wrappers. Returns the module names wrapped now."""
    server_dir = os.path.normcase(os.path.abspath(server_dir))
    orig_to_wrap = {}
    now = []
    for mname, mod in list(sys.modules.items()):
        if mod is None or mname in done:
            continue
        f = getattr(mod, "__file__", None)
        if not f or os.path.dirname(os.path.normcase(os.path.abspath(f))) != server_dir:
            continue
        base = mname.split(".")[-1]
        done.add(mname)
        if base in SKIP_MODULES:
            continue
        if base == "earthfast":
            for k in dir(mod):
                v = getattr(mod, k)
                if callable(v) and isinstance(v, types.BuiltinFunctionType):
                    wv = wrap(v, f"earthfast.{k}")
                    orig_to_wrap[id(v)] = wv
                    setattr(mod, k, wv)
            now.append(mname)
            continue
        now.append(mname)
        for k, v in list(vars(mod).items()):
            if inspect.isfunction(v) and getattr(v, "__module__", None) == mod.__name__:
                wv = wrap(v, f"{base}.{v.__qualname__}")
                orig_to_wrap[id(v)] = wv
                setattr(mod, k, wv)
            elif inspect.isclass(v) and getattr(v, "__module__", None) == mod.__name__:
                for ak, av in list(vars(v).items()):
                    if isinstance(av, staticmethod):
                        fn = av.__func__
                        if inspect.isfunction(fn):
                            setattr(v, ak, staticmethod(wrap(fn, f"{base}.{v.__qualname__}.{ak}")))
                    elif isinstance(av, classmethod):
                        fn = av.__func__
                        if inspect.isfunction(fn):
                            setattr(v, ak, classmethod(wrap(fn, f"{base}.{v.__qualname__}.{ak}")))
                    elif inspect.isfunction(av):
                        wv = wrap(av, f"{base}.{v.__qualname__}.{ak}")
                        orig_to_wrap[id(av)] = wv
                        setattr(v, ak, wv)
    # references taken before the wrapping (from x import f; a class attribute set to another class's method)
    for mname, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None) if mod is not None else None
        if not f or os.path.dirname(os.path.normcase(os.path.abspath(f))) != server_dir:
            continue
        for k, v in list(vars(mod).items()):
            if id(v) in orig_to_wrap and v is not orig_to_wrap[id(v)]:
                setattr(mod, k, orig_to_wrap[id(v)])
    return now


def log_quantum(log_path, aid):
    """The last height quantum the server log gives anchor aid ("anchor 2 height quantum 1/64 m"), or None."""
    import re
    pat = re.compile(rf"anchor {int(aid)} height quantum 1/(\d+) m")
    got = None
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = pat.search(line)
                if m:
                    got = 1.0 / int(m.group(1))
    except OSError:
        pass
    return got


def main():
    global TRACK
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--earth", default=os.environ.get("BEAMNG_EARTH_DIR"), help="Earth source tree (its server/)")
    ap.add_argument("--cache", default=os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth"))
    ap.add_argument("--anchor", required=True, type=earth_trace.parse_pair)
    ap.add_argument("--aid", type=int, default=4)
    ap.add_argument("--block", required=True, type=earth_trace.parse_pair)
    ap.add_argument("--size", type=int, default=1024, help="the block's samples per edge (the server's layout)")
    ap.add_argument("--q", type=float, help="the anchor's height quantum (default: the server log's 'anchor <aid> height quantum', "
                                            "else 1/32)")
    ap.add_argument("--box", help="x0,y0,x1,y1 drawn zoomed (anchor frame, m)")
    ap.add_argument("--session", help="the game's session folder, to check the build against its PNG")
    ap.add_argument("--server-log", default=os.path.join(os.environ.get("LOCALAPPDATA", ""), "beamng-earth", "logs",
                                                         "server.log"))
    ap.add_argument("--geometry", action="append", default=[],
                    help="a EarthRally stage geometry json drawn over the maps (thin white line); repeatable")
    ap.add_argument("--rally", action="store_true",
                    help="run EarthRally's ground grader (shape.lua) on the built block along the --geometry stages and colour what it moves")
    ap.add_argument("--rally-edition", help="the edition's legs.json, for its shaping limits (shaping: spikeM)")
    ap.add_argument("--earthrally", help="an EarthRally checkout: --rally runs its tools/rally_grader.py")
    ap.add_argument("--no-airports", action="store_true")
    ap.add_argument("--no-sea", action="store_true")
    ap.add_argument("--out", default="earth-writers")
    a = ap.parse_args()
    if a.rally and not a.earthrally:
        ap.error("--rally needs --earthrally (the grader is EarthRally's)")
    if not a.earth:
        sys.exit("--earth (or BEAMNG_EARTH_DIR) is needed")
    earth_trace.AID = a.aid
    if a.q is None:
        a.q = log_quantum(a.server_log, a.aid) or 1 / 32
        print(f"height quantum {a.q:g} m", flush=True)
    # (a block from Earth's disk cache of graded blocks is read, not built: nothing would be traced)
    os.environ["BNGE_BLOCKCACHE"] = "off"
    earth_trace.load_earth(a.earth)
    server_dir = os.path.join(os.path.abspath(a.earth), "server")
    import blockworker
    import geodesy
    import grading  # noqa: F401
    import heightmap
    import roads  # noqa: F401
    import surface  # noqa: F401
    import dem  # noqa: F401
    import fast
    fast._ef  # noqa: B018  (loaded)

    os.makedirs(a.out, exist_ok=True)
    blockworker._s = blockworker._State(a.cache, False, True, True, True, True,       # (what the cache lacks fetched)
                                        airports=not a.no_airports, sea=not a.no_sea)
    S = blockworker._s
    lat0, lon0 = a.anchor
    frame = geodesy.EnuFrame(lat0, lon0)
    cx, cy = a.block
    S.anchor(a.aid, lat0, lon0)

    def region_frames():
        st = getattr(S, "plan_store", None)
        if st is None:
            return []
        for _ in range(5):              # (not under its lock: a planning thread holds it while it calls in here)
            try:
                return [pl.frame for pl in list(st.planners.values())]
            except RuntimeError:
                continue
        return []
    TRACK = Tracker(frame, cx, cy, a.size, server_dir, region_frames)

    done = set()
    final = {}
    orig_generate = heightmap.generate

    def generate(frame_, elev_fn, path, *args, **kw):
        def elev_top(la, lo):           # the heights the block is built from (then made ENU z and quantised)
            z = elev_fn(la, lo)
            _inside.on = True
            try:
                pix = TRACK.pixels(la, lo)
            finally:
                _inside.on = False
            if pix is not None:
                final["pix"], final["z"] = pix, np.asarray(z, np.float64).ravel().copy()
            return z
        return orig_generate(frame_, elev_top, path, *args, **kw)
    heightmap.generate = generate

    name = f"hm_{a.aid}_{int(cx)}_{int(cy)}.png"
    ter = f"hm_{a.aid}_{int(cx)}_{int(cy)}_trace.ter"
    wrapped = []
    for attempt in range(4):
        new = instrument(server_dir, done)
        wrapped += new
        if attempt and not new:
            break
        TRACK.reset()
        S.chunks[a.aid] = heightmap.ChunkCache()
        TRACK.active = True
        print(f"pass {attempt + 1}: {len(new)} modules wrapped now, building block ({cx:g}, {cy:g}) ...", flush=True)
        info = blockworker.build(a.aid, lat0, lon0, earth_trace.HM_ZOOM, a.q, cx, cy, a.size, a.out, name,
                                 ter_name=ter)
        TRACK.active = False
    print(f"{TRACK.calls} traced calls, {len(TRACK.stacks)} writers", flush=True)

    # the build against the session, the writers' values against the heights the block was built from
    from PIL import Image
    built = np.asarray(Image.open(os.path.join(a.out, name)), np.float64)[::-1]
    checks = []
    if a.session:
        sp = os.path.join(a.session, name)
        if os.path.isfile(sp):
            sess = np.asarray(Image.open(sp), np.float64)[::-1]
            checks.append(f"built PNG against the session's {name}: {int((sess != built).sum())} of {sess.size} samples "
                          f"differ (largest {np.abs(sess - built).max():g} quanta)")
        else:
            checks.append(f"no {name} in the session folder")
    zfin = np.full(a.size * a.size, np.nan)
    if "pix" in final:
        ok = final["pix"] >= 0
        zfin[final["pix"][ok]] = final["z"][ok]
    agree = np.isfinite(zfin) & (TRACK.val == zfin)
    checks.append(f"samples whose last writer's value is the height the block was built from: {int(agree.sum())} of "
                  f"{zfin.size}; no writer: {int((TRACK.label < 0).sum())}")

    if a.session and os.path.isfile(os.path.join(a.session, name)):
        d = (sess != built)
        if d.any():
            jj, ii = np.nonzero(d)
            x0b, y0b = cx - a.size / 2.0, cy - a.size / 2.0
            checks.append(f"  where they differ: x {x0b + ii.min():g} .. {x0b + ii.max():g}, y {y0b + jj.min():g} .. "
                          f"{y0b + jj.max():g}; by writer: " + ", ".join(
                              f"[{k}] {int(v)}" for k, v in zip(*np.unique(TRACK.label.reshape(a.size, a.size)[d],
                                                                            return_counts=True))))
    rally = None
    if a.rally:
        rally = rally_pass(a, frame, cx, cy, zfin, built, info)
        checks += rally["report"]
    holes = None
    terp = os.path.join(a.out, ter)
    if os.path.isfile(terp):
        with open(terp, "rb") as f:
            raw = f.read()
        n = int.from_bytes(raw[1:5], "little")
        lay = np.frombuffer(raw, np.uint8, n * n, 5 + 2 * n * n).reshape(n, n)
        holes = lay == 255
        holes[0, :] = holes[-1, :] = holes[:, 0] = holes[:, -1] = False     # (the edge ring: always holes)

    lab = TRACK.label.reshape(a.size, a.size)
    xs = TRACK.x0 + np.arange(a.size)
    ys = TRACK.y0 + np.arange(a.size)
    box = None
    if a.box:
        bx0, by0, bx1, by1 = (float(v) for v in a.box.split(","))
        box = (slice(max(0, int(by0 - ys[0])), int(by1 - ys[0]) + 1), slice(max(0, int(bx0 - xs[0])), int(bx1 - xs[0]) + 1))

    rows = []
    for sid, st in enumerate(TRACK.stacks):
        nb = int((lab == sid).sum())
        nbox = int((lab[box] == sid).sum()) if box else 0
        if nb:
            rows.append((sid, zlib.crc32("|".join(st).encode()) % 256, nb, nbox, st))
    rows.sort(key=lambda r: (-r[3], -r[2]))
    hues = {}
    for r in rows:
        hues.setdefault(r[1], []).append(r[0])
    out = [f"block ({cx:g}, {cy:g}) of anchor {a.aid} at {lat0}, {lon0}, {a.size}² samples at 1 m; "
           f"info zBase {info.get('zBase')} maxHeight {info.get('maxHeight')}",
           f"Earth modules wrapped: {', '.join(sorted(m.split('.')[-1] for m in wrapped))}", *checks,
           f"tunnel holes in the built .ter: {0 if holes is None else int(holes.sum())} samples", "",
           "writers (colour = PALETTE[crc32(stack) % 256]; stack innermost first), most samples in the box first:"]
    for sid, h, nb, nbox, st in rows:
        same = "" if len(hues[h]) == 1 else f"  (colour shared with writers {[x for x in hues[h] if x != sid]})"
        out.append(f"[{sid}] colour {h:3d}: {nb} samples in the block, {nbox} in the box{same}")
        for fr in st:
            out.append(f"      {fr}")
    und = TRACK.undone_by.reshape(a.size, a.size)
    out.append("")
    out.append("writes given back (a call returned the value it was given, after a call inside it had written there):")
    for sid in np.unique(und[und >= 0]):
        what = TRACK.undone_what.reshape(a.size, a.size)[und == sid]
        whats = ", ".join(f"[{k}] {int(v)}" for k, v in zip(*np.unique(what, return_counts=True)))
        nbox = int((und[box] == sid).sum()) if box else 0
        out.append(f"[{sid}] colour {zlib.crc32('|'.join(TRACK.stacks[sid]).encode()) % 256:3d} gave back "
                   f"{int((und == sid).sum())} samples ({nbox} in the box), written by: {whats}")
        if sid not in {r[0] for r in rows}:
            for fr in TRACK.stacks[sid]:
                out.append(f"      {fr}")
    rep = "\n".join(out)
    with open(os.path.join(a.out, "writers.txt"), "w", encoding="utf-8") as f:
        f.write(rep + "\n")
    print(rep[:6000])
    np.savez_compressed(os.path.join(a.out, "writers.npz"), label=lab, x=xs, y=ys, undone_by=und,
                        undone_what=TRACK.undone_what.reshape(a.size, a.size),
                        stacks=np.array(["|".join(st) for st in TRACK.stacks]),
                        hash=np.array([zlib.crc32("|".join(st).encode()) % 256 for st in TRACK.stacks], np.int32),
                        z=zfin.reshape(a.size, a.size), holes=holes if holes is not None else np.zeros(0),
                        rally=rally["change"] if rally else np.zeros(0))
    draw(a, lab, xs, ys, box, rows, holes, frame, rally=rally)
    if rally:
        draw_profiles(a, rally, box, xs, ys)
    draw(a, und, xs, ys, box, [(sid, zlib.crc32("|".join(TRACK.stacks[sid]).encode()) % 256, 1, 1, TRACK.stacks[sid])
                               for sid in np.unique(und[und >= 0])], holes, frame, "undone.png",
         "the call that gave back a value written inside it")


def rally_pass(a, frame, cx, cy, zfin, built, info):
    """EarthRally's grader on the block as built (rally_grader.py): its change of each sample, the lines as it saw them, the report."""
    sys.path.insert(0, os.path.join(a.earthrally, "tools"))
    import rally_grader
    n = a.size
    z = zfin.reshape(n, n).copy()
    if not np.isfinite(z).all():           # (a sample the trace has no height for: the built PNG's, the quantum and the base)
        zq = built * a.q + float(info.get("zBase") or 0.0)
        z[~np.isfinite(z)] = zq[~np.isfinite(z)]
    x0, y0 = cx - n / 2.0, cy - n / 2.0
    geoms = rally_grader.load_geometries(a.geometry)
    shaping = None
    if a.rally_edition:
        with open(a.rally_edition, encoding="utf-8") as f:
            shaping = json.load(f).get("shaping")
    tunnels = rally_grader.tunnel_points(frame, (x0, y0, x0 + n, y0 + n))
    print(f"rally grader: {len(geoms)} stage lines, {len(tunnels)} tunnel points, running shape.lua ...", flush=True)
    res = rally_grader.run(z, x0, y0, frame, geoms, tunnels, shaping)
    change = res["heights"] - z
    moved = np.abs(change) >= RALLY_MIN_M
    rep = ["", f"EarthRally's grader (shape.lua, --rally; no colliders offline: deck ends and overpasses the game's rays find are not shaped "
           f"here): {int(moved.sum())} samples moved {RALLY_MIN_M:g} m or more, largest {np.abs(change).max():.2f} m, "
           f"{len(tunnels)} tunnel points, {res['frames']} frames"]
    for ln in res["lines"]:
        kept = np.asarray(ln["kept"])
        sk = np.asarray([v is not None for v in ln["skip"]])
        s = np.asarray(ln["s"] or np.zeros(len(kept)))

        def ranges(mask):
            out, i = [], 0
            while i < len(mask):
                if mask[i]:
                    j = i
                    while j + 1 < len(mask) and mask[j + 1]:
                        j += 1
                    out.append(f"{s[i]:.0f}-{s[j]:.0f} m")
                    i = j + 1
                else:
                    i += 1
            return ", ".join(out) or "none"
        rep.append(f"  {ln['leg']}: kept as Earth made it (beside a tunnel) {ranges(kept)}; skipped (a span) {ranges(sk)}")
    rep += ["  the grader's log:"] + [f"    {m}" for m in res["logs"]]
    return {"change": change, "moved": moved, "before": z, "after": res["heights"], "lines": res["lines"], "report": rep,
            "x0": x0, "y0": y0}


def draw_profiles(a, rally, box, xs, ys):
    """Along each stage line within the box (or the block): Earth's ground and the ground after the grader, kept and skipped stretches shaded."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    n = rally["before"].shape[0]

    def at(u, x, y):
        fx, fy = x - rally["x0"], y - rally["y0"]
        i, j = np.floor(fx).astype(int), np.floor(fy).astype(int)
        ok = (i >= 0) & (j >= 0) & (i + 1 < n) & (j + 1 < n)
        i, j = np.clip(i, 0, n - 2), np.clip(j, 0, n - 2)
        p, q = fx - i, fy - j
        v = u[j, i] * (1 - p) * (1 - q) + u[j, i + 1] * p * (1 - q) + u[j + 1, i] * (1 - p) * q + u[j + 1, i + 1] * p * q
        return np.where(ok, v, np.nan)
    bx = (xs[box[1]][0], xs[box[1]][-1], ys[box[0]][0], ys[box[0]][-1]) if box else (xs[0], xs[-1], ys[0], ys[-1])
    lines = []
    for ln in rally["lines"]:
        x, y = np.asarray(ln["x"]), np.asarray(ln["y"])
        s = np.asarray(ln["s"]) if ln["s"] else np.concatenate([[0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))])
        inside = (x >= bx[0]) & (x <= bx[1]) & (y >= bx[2]) & (y <= bx[3])
        if inside.any():
            lines.append((ln, x, y, s, inside))
    if not lines:
        return
    fig, axs = plt.subplots(len(lines), 1, figsize=(14, 4.5 * len(lines)), squeeze=False)
    for ax, (ln, x, y, s, inside) in zip(axs[:, 0], lines):
        idx = np.nonzero(inside)[0]
        i0, i1 = max(0, idx[0] - 3), min(len(s) - 1, idx[-1] + 3)
        ss = np.arange(s[i0], s[i1], 1.0)
        px, py = np.interp(ss, s, x), np.interp(ss, s, y)
        ax.plot(ss, at(rally["before"], px, py), color="0.4", lw=1.2, label="Earth's ground on the line")
        ax.plot(ss, at(rally["after"], px, py), color="m", lw=1.2, label="after EarthRally's grader")
        for q in range(i0, i1):
            if ln["kept"][q]:
                ax.axvspan(s[q], s[q + 1], color="c", alpha=0.15, lw=0)
            if ln["skip"][q] is not None:
                ax.axvspan(s[q], s[q + 1], color="y", alpha=0.25, lw=0)
        ax.set_title(f"{ln['leg']}: the ground along the line; cyan: kept as Earth made it (beside a tunnel); yellow: a span skipped",
                     fontsize=10)
        ax.set_xlabel("m along the line")
        ax.set_ylabel("height (anchor frame, m)")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(a.out, "rally_profile.png"), dpi=80)
    plt.close(fig)


def draw(a, lab, xs, ys, box, rows, holes, frame, fname="writers.png", what="last writer of each height sample", rally=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
    hash_of = {sid: h for sid, h, _nb, _nbox, _st in rows}
    rgb = np.zeros(lab.shape + (3,))
    for sid, h in hash_of.items():
        rgb[lab == sid] = PALETTE[h]
    if holes is not None:
        rgb[holes] = 0.0
    if rally is not None:
        rgb[rally["moved"]] = RALLY_RGB
    stages = []
    for gp in a.geometry:
        with open(gp, encoding="utf-8") as f:
            pts = np.asarray(json.load(f)["points"], np.float64)
        e, n, _ = frame.geodetic_to_enu(pts[:, 0], pts[:, 1], 0.0)
        stages.append((np.asarray(e), np.asarray(n)))
    views = [("block", (slice(None), slice(None)))] + ([("box", box)] if box else [])
    fig, axs = plt.subplots(1, len(views), figsize=(11 * len(views), 11), squeeze=False)
    for ax, (title, sl) in zip(axs[0], views):
        sx, sy = xs[sl[1]], ys[sl[0]]
        ax.imshow(rgb[sl], origin="lower", extent=(sx[0] - 0.5, sx[-1] + 0.5, sy[0] - 0.5, sy[-1] + 0.5),
                  interpolation="nearest")
        for st in stages:
            ax.plot(st[0], st[1], color="white", lw=0.6)
        if rally is not None:
            for ln in rally["lines"]:
                k = np.asarray(ln["kept"])
                sk = np.asarray([v is not None for v in ln["skip"]])
                ax.plot(ln["x"][k], ln["y"][k], "o", color="c", ms=2.5)
                ax.plot(ln["x"][sk], ln["y"][sk], "o", color="y", ms=2.5)
        if stages:
            ax.set_xlim(sx[0] - 0.5, sx[-1] + 0.5)
            ax.set_ylim(sy[0] - 0.5, sy[-1] + 0.5)
        extra = "; magenta: moved by EarthRally's grader; cyan dots: kept as Earth made it; yellow: a span" if rally is not None else ""
        ax.set_title(f"{what} ({title}); black: tunnel holes; white: stage line{extra}", fontsize=10)
    top = [r for r in rows if (r[3] if box else r[2])][:24]
    handles = [Patch(color=PALETTE[h], label=f"[{sid}] {h}: {st[0]} < {st[1] if len(st) > 1 else ''}"[:110])
               for sid, h, _nb, _nbox, st in top]
    fig.legend(handles=handles, loc="lower center", ncol=2, fontsize=8, frameon=False)
    fig.tight_layout(rect=(0, 0.04 + 0.012 * len(handles) / 2, 1, 1))
    fig.savefig(os.path.join(a.out, fname), dpi=80)
    plt.close(fig)


if __name__ == "__main__":
    main()
