"""Who has the server's cores: the CPU each group of processes used since the last look, in cores.

At 300 km/h a road geotile's thread got a quarter of its wall time as CPU and the log could not say whether it waited
for data or for a core: the server's ~20 processes share its few cores (cpupin: 3 of 8), and nothing said which of them
had them. While a player drives fast the server logs, every EVERY_S, the cores each pool's processes used meanwhile
(pools.process_cpu_s: /proc or GetProcessTimes) and their sum: "cores at 87 m/s: 2.9 in use (far tile 0.9, blocks 0.7,
road tile 0.6, ...)". A sum at the server's core count says the pools wait for each other."""
import logging
import os
import time

import pools as poolsmod

log = logging.getLogger("earthd.cpuload")

FAST_V = 40.0           # m/s: logged while a player drives at least this fast (arrival.FAST_V)
EVERY_S = 20.0          # s between two lines


class Load:
    """groups: {name: () -> [pid]} (a pool's processes now); read: pid -> CPU s or None. sample() -> {name: cores}
    used since the last sample (None at the first): each process's CPU s gained over the wall time; a process first
    seen after the first sample was started meanwhile (a pool's processes replaced) and all its CPU so far counts,
    one that went counts nothing since its last sample."""

    def __init__(self, groups, read=None, clock=time.monotonic):
        self.groups, self.read, self.clock = dict(groups), read or poolsmod.process_cpu_s, clock
        self._at, self._cpu = None, {}
        self._said = -1e9

    def sample(self):
        now = self.clock()
        cpu, gained = {}, {}
        for name, pids in self.groups.items():
            total = 0.0
            try:
                ids = list(pids())
            except Exception:
                ids = []
            for pid in ids:
                c = self.read(pid)
                if c is None:
                    continue
                cpu[pid] = c
                total += max(0.0, c - self._cpu.get(pid, c if self._at is None else 0.0))
            gained[name] = total
        at, self._at, self._cpu = self._at, now, cpu
        if at is None or now <= at:
            return None
        return {name: g / (now - at) for name, g in gained.items()}

    def tick(self, v, cores=None):
        """A pose of a player at v m/s: at FAST_V or more, one line every EVERY_S (the first look only starts the
        count). cores: the server's own cores (cpupin), for the line. -> the line's {name: cores}, or None."""
        if v < FAST_V:
            self._at = None             # (a slow stretch between two fast ones is not averaged into the next line)
            return None
        now = self.clock()
        if self._at is not None and now - self._at < EVERY_S:
            return None
        got = self.sample()
        if got is None:
            return None
        rows = sorted(((c, n) for n, c in got.items() if c >= 0.05), reverse=True)
        log.info("cores at %.0f m/s: %.1f in use%s (%s)", v, sum(got.values()),
                 f" of the server's {cores}" if cores else "", ", ".join(f"{n} {c:.1f}" for c, n in rows) or "idle")
        return got


def of_server(pools):
    """Load over the server's own process and its pools (objects with .name and ._pids(); None skipped; pools of one
    name counted together)."""
    groups = {"server": lambda: [os.getpid()]}
    by_name = {}
    for p in pools:
        if p is not None and not any(p is q for qs in by_name.values() for q in qs):    # (a pool given twice: once)
            name = getattr(p, "name", None) or getattr(getattr(p, "local", None), "name", None) or "pool"
            by_name.setdefault(name, []).append(p)
    for name, ps in by_name.items():
        groups[name] = lambda ps=ps: [pid for p in ps for pid in _pids(p)]
    return Load(groups)


def _pids(pool):
    """The processes of a pools.RespawningPool, of an ElasticPool's base and burst pools, or of the local pool a
    wrapper holds (peerwork.SharedFarPool's .local)."""
    if hasattr(pool, "_pids"):
        return pool._pids()
    out = []
    for part in (getattr(pool, "base", None), getattr(pool, "extra", None), getattr(pool, "local", None)):
        if part is not None:
            out += _pids(part)
    return out
