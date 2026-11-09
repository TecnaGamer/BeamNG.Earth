"""The world server's Rust kernels (native/earthfast, built by tools/build_native.py).

The road tiles' hot loops were Python over tiny numpy arrays (a 2026-10-15 profile: the islands' curb ray march alone
was ~20 % of a road tile, 0.9 s of ~4 s); the compiled module does them in one call. The server needs it: the Python
twins of the kernels are gone (the user, 2026-10-21: "we dont need 2 versions of the same thing"); require() says
what is missing when it cannot be loaded.
"""
import os
import sys

import numpy as np

try:
    import earthfast as _ef
except ImportError:         # no binary for this platform / Python: require() says so
    _ef = None

AVAILABLE = _ef is not None
MIN_VERSION = 175 # the newest kernel signature the server calls (earthd checks it at start)


class NativeMissing(RuntimeError):
    """The server's Rust module (native/earthfast) is missing or older than a kernel needs."""


def require(version, what):
    """Raise NativeMissing unless the module is loaded at VERSION >= version (what: the kernel, for the message). The
    kernels that have no Python twin any more (the user, 2026-10-21: "we dont need 2 versions of the same thing")."""
    have = getattr(_ef, "VERSION", 0) if _ef is not None else None
    if have is None or have < version:
        raise NativeMissing(f"{what} needs the server's native module (earthfast) version {version} or newer, "
                            f"{'none was loaded' if have is None else f'version {have} was'}: rebuild it with "
                            "python3 tools/build_native.py, or use a release build")


def segments(lines_hw):
    """(segs (m, 4) ax ay bx by, hw (m,)) of [(polyline (k, 2), half width)]."""
    segs, hws = [], []
    for pts, hw in lines_hw:
        p = np.asarray(pts, np.float64)[:, :2]
        if len(p) < 2:
            continue
        segs.append(np.hstack([p[:-1], p[1:]]))
        hws.append(np.full(len(p) - 1, float(hw)))
    if not segs:
        return np.zeros((0, 4)), np.zeros(0)
    return np.ascontiguousarray(np.vstack(segs)), np.ascontiguousarray(np.concatenate(hws))


def island_ring_rays(poly, lines_hw, c, rays, step, rmax):
    """(rays, 2): the island's curb ring by rays from c, stopped by the polygon and the roads."""
    require(1, "island_ring_rays")
    segs, hw = segments(lines_hw)
    return _ef.island_ring_rays(np.ascontiguousarray(poly[:, :2], np.float64), segs, hw, float(c[0]), float(c[1]),
                                int(rays), float(step), float(rmax))


class Others:
    """furniture's (fid, half width, polyline) roads as segment arrays once, for clear() at every candidate."""

    def __init__(self, others):
        self.others = others
        require(1, "Others")
        ids = {}
        segs, half, owner = [], [], []
        for fid, h, line in others:
            p = np.asarray(line, np.float64)[:, :2]
            if len(p) < 2:
                continue
            k = ids.setdefault(fid, len(ids))
            segs.append(np.hstack([p[:-1], p[1:]]))
            half.append(np.full(len(p) - 1, float(h)))
            owner.append(np.full(len(p) - 1, k, np.int64))
        self.ids = ids
        self.segs = np.ascontiguousarray(np.vstack(segs)) if segs else np.zeros((0, 4))
        self.half = np.ascontiguousarray(np.concatenate(half)) if half else np.zeros(0)
        self.owner = np.ascontiguousarray(np.concatenate(owner)) if owner else np.zeros(0, np.int64)

    def clear(self, p, own, margin):
        """Is p (2+,) farther than each road's half width + margin from every road but own?"""
        return _ef.clear_of(self.segs, self.half, self.owner, float(p[0]), float(p[1]), self.ids.get(own, -1),
                            float(margin))


_GPU = None


def gpu():
    """The GPU's name when the kernels can run there (earthfast gpu.rs: Vulkan / DX12 / Metal), else None (none, an old
    module, or BNGE_GPU=0): the callers keep their CPU path. Found once (~0.4 s, the first call)."""
    global _GPU
    # (on Windows too since 2026-11-01, the user: "Gpu should be used where possible on both". It was off there from
    # 2026-10-23: 1.6.0's Windows players saw the game freeze in new places, a long compute dispatch possibly holding the
    # GPU the game draws with; the kernels now run in short submissions, each waited for: gpu.rs encode_blocks)
    if _GPU is None and os.environ.get("BNGE_GPU") == "0":
        _GPU = ""
    if _GPU is None:
        try:
            _GPU = _ef.gpu_name() or "" if getattr(_ef, "VERSION", 0) >= 58 else ""
        except Exception:
            _GPU = ""
    return _GPU or None


def perf_clean(v, strings=False, allowed=None):
    """v as an anonymous report may carry it, or None (native perfclean.rs: numbers, lists of numbers, small tables
    under plain names; strings only with strings=True; allowed: {name: None | [name, ...]}, the names it may have;
    perfstats.py)."""
    require(167, "perf_clean")
    return _ef.perf_clean(v, bool(strings), allowed)


def perf_step(v, steps):
    """The largest of steps (ascending) not above v."""
    require(167, "perf_step")
    return _ef.perf_step(float(v), [float(x) for x in steps])


def perf_fingerprint(kind, module, line):
    """An error's fingerprint: 8 hex characters of a hash of kind:module:line (no message, no value)."""
    require(167, "perf_fingerprint")
    return _ef.perf_fingerprint(str(kind), str(module), int(line))


def proc_tree(root):
    """(CPU seconds, resident MB) of process root and every process under it, or None (not Linux)."""
    require(167, "proc_tree")
    return _ef.proc_tree(int(root))


def md_html(text, base, fold_details=False):
    """(html, [url, ...]): an add-on's Markdown (README, release notes) as the Add-ons page's safe HTML (native
    mdhtml.rs: our own tags only, links as indexes into the URL list; addons.py)."""
    require(166, "md_html")
    return _ef.md_html(str(text), str(base), bool(fold_details))


def _rows(parts):
    """(rows (n, 2) float64 contiguous, offsets (k + 1,) int64) of polylines (m, 2+)."""
    pts = [np.asarray(p, np.float64).reshape(len(p), -1)[:, :2] for p in parts]
    off = np.r_[0, np.cumsum([len(p) for p in pts])].astype(np.int64)
    return np.ascontiguousarray(np.concatenate(pts) if pts else np.zeros((0, 2))), off


class DeckMerge:
    """roads._merge_overlaps' pair pulls (native deckmerge.rs): lines (deck parts (k, 2+)), widths (m), pairs [(i, j)]
    of related parts, dual[p]: the pair's parts are one level's (the lower lifted to the higher)."""

    def __init__(self, lines, widths, pairs, dual, touch, dual_m):
        require(169, "deck_merge_round")
        self.xy, self.off = _rows(lines)
        self.widths = np.ascontiguousarray(np.asarray(widths, np.float64))
        self.pairs = np.ascontiguousarray(np.asarray(pairs, np.int64).reshape(-1, 2))
        self.dual = np.ascontiguousarray(np.asarray([bool(d) for d in dual], np.uint8))
        self.touch, self.dual_m = float(touch), float(dual_m)

    def round(self, zs):
        """([acc], [cnt], [up]) per part, one value per height of zs: the pairs' weighted heights of the other part
        (acc), their weights (cnt) and the lift of one level's twin carriageways (up)."""
        z = [np.asarray(v, np.float64) for v in zs]
        zoff = np.r_[0, np.cumsum([len(v) for v in z])].astype(np.int64)
        acc, cnt, up = _ef.deck_merge_round(self.xy, self.off, np.ascontiguousarray(np.concatenate(z) if z else
                                                                                      np.zeros(0)), zoff,
                                            self.widths, self.pairs, self.dual, self.touch, self.dual_m)
        cut = lambda a: [a[zoff[k]:zoff[k + 1]] for k in range(len(z))]      # noqa: E731
        return cut(acc), cut(cnt), cut(up)


def deck_ground_pins(ends, ground_lines, pin_road_m, pin_m):
    """(m, 2): of the deck ends (e, 2) the joints (another end within 0.5 m) an end of a ground line (k, 2+)
    pin_road_m or longer lies within pin_m of, per line and its first then last end (roads._ground_pins)."""
    require(169, "deck_ground_pins")
    gl, goff = _rows(ground_lines)
    return _ef.deck_ground_pins(np.ascontiguousarray(np.asarray(ends, np.float64).reshape(-1, 2)), gl, goff,
                                float(pin_road_m), float(pin_m))
def ucs4(values):
    """values (a numpy unicode array, else each one's str) as (n, width) uint32: its UCS-4 code points, NUL padded
    (the native string kernels' input: no Python string per value)."""
    a = np.asarray(values)
    if a.dtype.kind != "U":
        a = np.array([str(v) for v in a.tolist()], dtype=str)
    a = np.ascontiguousarray(a.reshape(-1), a.dtype.newbyteorder("="))
    w = a.dtype.itemsize // 4
    return a.view(np.uint32).reshape(len(a), w) if w else np.zeros((len(a), 0), np.uint32)


def way_tag_rows(codes, ids, idx):
    """(n,) int64 per record (codes: ucs4 of Overture source records, "w<id>@<version>") the tag set index of its way in
    a lane / rail shard (railtags.Ways: its sorted ids, their tag set indices): -1 without a way id or not in the
    shard, -2 for railtags.way_id to decide (non-ASCII after the digits, past int64). Native waytags.rs."""
    require(169, "way_tag_rows")
    return _ef.way_tag_rows(codes, np.ascontiguousarray(ids, np.int64), np.ascontiguousarray(idx, np.int32))


def lane_parts(codes, feature, start, end, ids, idx, tags, ask):
    """railtags.LaneTags.join's parts (native waytags.rs): (features (f,), [[(start, end, way id, tags), ...] each])
    of the rule rows codes (ucs4 of their records) of features feature (sorted), tags the shard's tag sets (its
    tags[k] or a new {} without), ask(k) -> (way id, tags) where railtags.way_id decides."""
    require(169, "lane_parts")
    return _ef.lane_parts(codes, np.ascontiguousarray(feature, np.int64), np.ascontiguousarray(start, np.float64),
                          np.ascontiguousarray(end, np.float64), np.ascontiguousarray(ids, np.int64),
                          np.ascontiguousarray(idx, np.int32), tags, ask)


def rail_seam_couple(xy, z, half, rxy, rz, off, rh, reach, slope, tol, max_move, grade):
    """railroadseam.couple's native work (railseam.rs): the moved bed heights (n,), or None when nothing moved. The
    roads flattened: their points rxy (m, 2), heights rz (m,), road r = rows off[r]:off[r + 1], half widths rh."""
    require(172, "rail_seam_couple")
    return _ef.rail_seam_couple(np.ascontiguousarray(xy, np.float64), np.ascontiguousarray(z, np.float64), float(half),
                                np.ascontiguousarray(rxy, np.float64), np.ascontiguousarray(rz, np.float64),
                                np.ascontiguousarray(off, np.int64), np.ascontiguousarray(rh, np.float64), float(reach),
                                float(slope), float(tol), float(max_move), float(grade))


def deck_capped(pts, z, la, box, test_box, flat, max_m, side_m, min_cross):
    """grading.deck_capped's native work (underdeck.rs): (code, out) with code 0 z as it is, 1 z as float64, 2 out."""
    require(169, "deck_capped")
    off, q, sv, lb, half = flat
    return _ef.deck_capped(pts, np.ascontiguousarray(z, np.float64), np.ascontiguousarray(la, np.float64),
                           np.ascontiguousarray(box, np.float64), bool(test_box), off, q, sv,
                           np.ascontiguousarray(lb, np.float64), half, float(max_m), float(side_m), float(min_cross))
