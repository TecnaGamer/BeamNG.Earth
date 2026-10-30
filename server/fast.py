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
MIN_VERSION = 114 # the newest kernel signature the server calls (earthd checks it at start)


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
    if _GPU is None and sys.platform == "win32" and os.environ.get("BNGE_GPU") != "1":
        # Windows: off unless BNGE_GPU=1 (2026-10-23). 1.6.0's players on Windows saw the game freeze for seconds in new
        # places (none on Linux or a Steam Deck); a long compute dispatch from the server can hold the GPU the game draws
        # with, and Windows often cannot preempt it. The CPU path is 1.5.5's.
        _GPU = ""
    if _GPU is None:
        try:
            _GPU = _ef.gpu_name() or "" if getattr(_ef, "VERSION", 0) >= 58 else ""
        except Exception:
            _GPU = ""
    return _GPU or None
