"""Offline fixtures for the staged solver (server/staged*.py): a shapes region as the planner hands it to find_systems,
read back from a pickle (earth-evals/staged/dump_region.py wrote it from the eval's cached data: members, what the map
has of each road, the ground under every member as the shapes read it, today's systems, the frame), the replays and
the Swiss survey places in tests/, and the small synthetic places the unit tests build.

    region(NAME)   NAME: shanghai_13719_6695 | shanghai_13716_6692 | shanghai_nanpu_spiral (dir: env STAGED_FIXTURES, else
                   C:/Users/loren/earth-evals/staged/fix; None where it is not there: the tests skip)
    replay(NAME)   a tests/interchanges.py replay (popoli_ss17_underpass, a20_921 ...) planned as the server plans it
    truth(NAME)    a Swiss survey place (tests/truth.py), its ways as the members the survey's facts give

Each returns a Place: net (gradeshapes.Net), meta (by Earth id), ll (x, y -> lat, lon), edge, ground (Ground: the
elevation along the members), today (today's find_systems answer, or None to run it), name."""
import os
import pickle
from types import SimpleNamespace

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
os.sys.path.insert(0, os.path.dirname(HERE))
os.sys.path.insert(0, HERE)
import dnp  # noqa: E402,F401
import gradeshapes as gs  # noqa: E402

FIX_DIR = os.environ.get("STAGED_FIXTURES", r"C:\Users\loren\earth-evals\staged\fix")


class Ground:
    """The elevation under the members: at(i, s) along member i (arrays), xy(points) by the nearest sample (the ends of a
    deck are read this way), fixed(i, s) what the shapes do not move (a road at its ground, a track at its bed: the
    fixtures keep what the planner was asked). Built from per-member tables (s, z) or from a callable ground(xy)."""

    def __init__(self, net, tables=None, func=None, fixed=None, fixed_func=None):
        self.net, self.tables, self.func, self.fixed_tab = net, tables, func, fixed or {}
        self.fixed_func = fixed_func            # (i, s) -> height of what the shapes do not move: a surveyed track (truth.py)
        self._grid = None

    def remap(self, net, source, offset=None):
        """The same ground for a net whose member k is input member source[k] with a line cleaned of repeated points (a metre at
        most shorter: the tables' arc lengths scaled to the new length)."""
        old = self.net
        new = Ground(net, func=self.func, fixed=None)
        if self.tables is not None:
            new.tables = []
            for k, i in enumerate(source):
                ts, tz = self.tables[i]
                off = 0.0 if offset is None else offset[k]
                f = 1.0 if off else net.L[k] / max(old.L[i], 1e-9)       # (a stretch cut out of a road: its own metres)
                new.tables.append((ts * f - off, tz))
        new.fixed_tab = {}
        for k, i in enumerate(source):
            if i in self.fixed_tab:
                off = 0.0 if offset is None else offset[k]
                f = 1.0 if off else net.L[k] / max(old.L[i], 1e-9)
                new.fixed_tab[k] = {s_ * f - off: z for s_, z in self.fixed_tab[i].items()}
        new.fixed_func = self.fixed_func
        return new

    def at(self, i, s):
        sv = np.asarray(s, np.float64)
        if self.tables is not None:
            ts, tz = self.tables[i]
            return np.interp(sv, ts, tz)
        xy = np.stack([np.interp(np.atleast_1d(sv), self.net.s[i], self.net.lines[i][:, 0]),
                       np.interp(np.atleast_1d(sv), self.net.s[i], self.net.lines[i][:, 1])], axis=1)
        z = np.asarray(self.func(xy), np.float64)
        return z if np.ndim(s) else float(z[0])

    def xy(self, pts):
        pts = np.asarray(pts, np.float64).reshape(-1, 2)
        if self.func is not None:
            return np.asarray(self.func(pts), np.float64)
        if self._grid is None:
            cells = {}
            for i, (ts, tz) in enumerate(self.tables):
                line, s = self.net.lines[i], self.net.s[i]
                if len(line) < 2:
                    continue
                x, y = np.interp(ts, s, line[:, 0]), np.interp(ts, s, line[:, 1])
                for k in range(len(ts)):
                    cells.setdefault((int(x[k] // 16), int(y[k] // 16)), []).append((x[k], y[k], float(tz[k])))
            self._grid = cells
        out = np.zeros(len(pts))
        for n, p in enumerate(pts):
            best, cx, cy = None, int(p[0] // 16), int(p[1] // 16)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for x, y, z in self._grid.get((cx + dx, cy + dy), ()):
                        d = (x - p[0]) ** 2 + (y - p[1]) ** 2
                        if best is None or d < best[0]:
                            best = (d, z)
            out[n] = best[1] if best else 0.0
        return out

    def fixed(self, i, s):
        """member i's height where the shapes do not move it at arc length s (float: float or None; array: array, NaN: not
        known). A track's from what was asked of the planner (interpolated along the track), else the ground's."""
        if self.fixed_func is not None:
            return self.fixed_func(i, s)
        m = self.net.m[i]
        scalar = np.ndim(s) == 0
        sv = np.atleast_1d(np.asarray(s, np.float64))
        if m.kind in ("rail", "rail_deck"):
            tab = self.fixed_tab.get(i)
            if not tab:
                z = np.full(len(sv), np.nan)
            else:
                ks = np.array(sorted(k for k, v in tab.items() if np.isfinite(v)))
                z = np.full(len(sv), np.nan) if not len(ks) else np.interp(sv, ks, [tab[k] for k in ks.tolist()])
        else:
            z = np.asarray(self.at(i, sv), np.float64)
        if scalar:
            return None if not np.isfinite(z[0]) else float(z[0])
        return z


def _members(d):
    out = []
    for t, line in zip(d["members"], d["lines"]):
        m = gs.Member(*t)._replace(line=np.asarray(line, np.float64))
        out.append(m)
    return out


def _limit(near):
    import roads
    return lambda q: roads.road_sign_at(near, q)


def region(name):
    path = os.path.join(FIX_DIR, name + ".pkl")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        d = pickle.load(fh)
    mem = _members(d)
    for i, near in d.get("limits", {}).items():
        mem[i] = mem[i]._replace(limit=_limit(near))
    net = gs.Net(mem)
    frame = d["frame"]

    def ll(x, y):
        la, lo, _ = frame.enu_to_geodetic(np.array([x]), np.array([y]), 0.0)
        return float(la[0]), float(lo[0])
    tables = [(np.asarray(s, np.float64), np.asarray(z, np.float64)) for s, z in d["ground"]]
    ground = Ground(net, tables=tables, fixed={int(i): v for i, v in d["fixed"].items()})
    systems = d["systems"]
    for s in systems:
        s["digs"] = set(s["digs"])
    allp = np.concatenate([l for l in net.lines if len(l)])
    lo_, hi_ = allp.min(0), allp.max(0)

    def edge(xy):
        """Whether a point is within 150 m of the edge of the members' box (the cells the planner read end there: a road
        met there may go on in a cell that was not read)."""
        p = np.asarray(xy, np.float64)
        return bool((p < lo_ + 150.0).any() or (p > hi_ - 150.0).any())
    return SimpleNamespace(name=name, net=net, members=mem, meta=d["meta"], ll=ll, edge=edge, ground=ground, today=systems,
                           trace=d["trace"], audits=d.get("audits"), home=d["home"])
