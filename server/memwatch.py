"""The world server's memory watchdog: the whole server held to its RAM limit (the settings page's "Max RAM for
BeamNG.Earth"; budget.LIMIT_MB, native membudget.rs).

Each process's caches have their own caps (budget.apply), but they add up: a 64 GB PC's server reached 29 GB in a
30-minute drive through three cities (game-less, 2026-11-11), a 16 GB PC's 14.6 GB, and the players' reports show the
server's memory in the 8-24 GB bins for most of their hours while 4 in 10 of those hours had less than 2 GB of the
machine free. Every CHECK_S, in a thread of the server's main process, this measures the whole server (Linux: each
process's PSS + swapped PSS, the shared pages split fairly; Windows: private bytes) and hands the numbers to the native
step (fast.ram_watch), which answers:
  - "shed": every process over its share of the limit drops its caches (budget.shed_ask: the workers between jobs or
    when idle, the main process in its tree_publisher thread);
  - "rotate": the pool holding the largest process gets fresh processes (pools.RespawningPool.rotate: its jobs finish
    in the old ones, which then exit and give everything back);
  - "ok" / "wait": nothing now.
While the server is over 90 % of the limit no far-tile burst processes start (budget.OVER, pools.ElasticPool). The
limit can change while the server runs (set_setting, the settings page); the process counts and the caches' sizes
follow it from the next start. Python here is the glue: reading the processes' memory and acting; the rules are native.
"""
import logging
import multiprocessing
import os
import sys
import threading
import time

import budget
import fast

log = logging.getLogger("earthd.memwatch")

CHECK_S = float(os.environ.get("BNGE_MEMWATCH_S", "") or 5.0)
LOG_EVERY_S = 300.0         # a summary line in the log this often
# A shed request goes to the processes holding more than this times an equal share of the limit: a shed costs a worker
# its warm caches (map data cells read again: +17-47 % a road tile, 2026-11-04), so the small ones keep theirs
SHED_SHARE = 1.25
ON = os.environ.get("BNGE_MEMWATCH", "on").lower() not in ("off", "0", "false", "no")

_lock = threading.Lock()
_pools = []                 # callables -> [RespawningPool]: the pools the watchdog may rotate
_state = {"limit": 0.0, "setting": 0.0, "startSetting": 0.0, "total": None, "eff": 0.0, "sheds": 0, "rotations": 0,
          "processes": 0, "peak": 0.0, "avail": None, "action": "ok"}
_thread = None


def _respawning(p):
    """The RespawningPools behind p (a SharedFarPool's local pool, an ElasticPool's base and running extras)."""
    if p is None:
        return []
    p = getattr(p, "local", p)
    if hasattr(p, "base"):
        ex = getattr(p, "extra", None)
        return [p.base] + ([ex] if ex is not None else [])
    return [p] if hasattr(p, "rotate") else []


def register(*pools):
    """The server's pools (any of RespawningPool / ElasticPool / SharedFarPool; None skipped), once each."""
    with _lock:
        for p in pools:
            if p is not None and all(p is not q for q in _pools):
                _pools.append(p)


def process_mb(pid):
    """A process's memory as the watchdog counts it (MB): Linux PSS + SwapPss, Windows private bytes; None."""
    import perfstats
    if sys.platform == "win32":
        try:
            r = perfstats._win_proc(pid)
        except Exception:
            r = None
        return r[1] if r else None
    return perfstats._pss_mb(pid)


def measure():
    """({pid: MB} of the server's main process and every live worker, total MB)."""
    pids = [os.getpid()]
    try:
        pids += [p.pid for p in multiprocessing.active_children() if p.pid]
    except Exception:
        pass
    mbs = {}
    for pid in pids:
        mb = process_mb(pid)
        if mb is not None:
            mbs[pid] = mb
    return mbs, sum(mbs.values())


def _avail_mb():
    gb = budget.free_gb()
    return gb * 1e9 / 2 ** 20 if gb is not None else -1.0


def start(limit_mb=None):
    """The watchdog's thread (the server's main process, after its pools exist). False when off or without a limit."""
    global _thread
    limit_mb = budget.LIMIT_MB if limit_mb is None else limit_mb
    with _lock:
        _state["limit"] = float(limit_mb or 0.0)
        _state["setting"] = _state["startSetting"] = budget.setting_gb()
        _state["tier"] = budget.TIER
    if not ON or not limit_mb or _thread is not None:
        return False
    _thread = threading.Thread(target=_loop, daemon=True, name="memwatch")
    _thread.start()
    log.info("memory: the server's limit %.1f GB (%s; settings page: Max RAM for BeamNG.Earth), tier %s",
             limit_mb / 1024, f"{_state['setting']:g} GB set" if _state["setting"] else "Auto", budget.TIER)
    return True


def set_setting(gb):
    """The settings page's Max RAM changed while the server runs (gb: GB, 0 Auto): the watchdog's limit now (the
    processes and caches follow it from the next start). Returns the new limit in MB."""
    try:
        gb = float(gb or 0.0)
    except (TypeError, ValueError):
        gb = 0.0
    gb = gb if 0 < gb < 1e6 else 0.0
    mb = budget.limit_mb(setting=gb)
    with _lock:
        _state["setting"], _state["limit"] = gb, mb
    os.environ[budget.SETTING_ENV] = str(gb) if gb else "auto"     # (a pool's new processes size their caches by it)
    budget.LIMIT_MB = mb
    log.info("memory: the server's limit now %.1f GB (%s)", mb / 1024, f"{gb:g} GB set" if gb else "Auto")
    if _thread is None and ON and mb:
        start(mb)
    return mb


def status():
    """The settings page's line: {usedMB, limitMB, effMB, autoMB, setting, tier, processes, sheds, rotations, peakMB,
    availMB, restart} (restart: the setting differs from the one the server started with)."""
    with _lock:
        s = dict(_state)
    if s["total"] is None:          # (not measured yet, or the watchdog off: measure now)
        try:
            mbs, s["total"] = measure()
            s["processes"] = len(mbs)
        except Exception:
            s["total"] = 0.0
    auto = budget.limit_mb(setting=0.0)
    out = {"usedMB": round(s["total"] or 0.0), "limitMB": round(s["limit"]), "effMB": round(s["eff"] or s["limit"]),
           "autoMB": round(auto), "setting": s["setting"], "tier": s.get("tier") or budget.TIER,
           "processes": s["processes"], "sheds": s["sheds"], "rotations": s["rotations"], "peakMB": round(s["peak"]),
           "restart": bool(s["setting"] != s["startSetting"])}
    if s["avail"] is not None and s["avail"] >= 0:
        out["availMB"] = round(s["avail"])
    return out


class _Watch:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.over_since = None
        self.last_shed = self.last_rotate = -1e9
        self.last_log = clock()

    def step(self, mbs, total, avail, pools):
        """One check: the native step's action carried out. pools: [RespawningPool]. Returns the action."""
        now = self.clock()
        with _lock:
            limit = _state["limit"]
        reserve = budget.LOW_FREE_GB * 1e9 / 2 ** 20
        cands = []
        for p in pools:
            try:
                pids = list((getattr(p._pool, "_processes", None) or {}).keys())
            except Exception:
                pids = []
            big = max([mbs.get(pid, 0.0) for pid in pids] or [0.0])
            cands.append((big, p.inflight > p.workers))
        eff = fast.ram_effective_mb(limit, total, avail, reserve) if limit else 0.0
        if limit and total >= eff:
            if self.over_since is None:
                self.over_since = now
        elif not limit or total < 0.9 * eff:
            self.over_since = None
        over_s = now - self.over_since if self.over_since is not None else 0.0
        action, idx, eff = fast.ram_watch(limit, total, avail, reserve, over_s, now - self.last_shed,
                                          now - self.last_rotate, cands)
        budget.OVER = bool(limit) and total >= eff     # (over the limit itself: no far-tile burst processes)
        if action == "shed":
            self.last_shed = now
            share = SHED_SHARE * eff / max(1, len(mbs))    # (the biggest processes: a shed costs a worker its warm caches)
            budget.shed_ask(share)
            log.info("memory: the server at %.0f MB of %.0f (%d processes): caches dropped where over %.0f MB",
                     total, eff, len(mbs), share)
        elif action == "rotate":
            self.last_rotate = now
            p = pools[idx]
            p.rotate(f"the server at {total:.0f} MB of its {eff:.0f} MB limit for {over_s:.0f} s, a process at "
                     f"{cands[idx][0]:.0f} MB", now)
        with _lock:
            _state.update(total=total, eff=eff, processes=len(mbs), avail=avail, action=action,
                          peak=max(_state["peak"], total))
            if action == "shed":
                _state["sheds"] += 1
            elif action == "rotate":
                _state["rotations"] += 1
        if now - self.last_log >= LOG_EVERY_S:
            self.last_log = now
            log.info("memory: the server at %.0f MB of %.0f (%d processes, peak %.0f, %s available; %d sheds, %d "
                     "replacements so far)", total, eff, len(mbs), _state["peak"],
                     f"{avail:.0f} MB" if avail >= 0 else "unknown", _state["sheds"], _state["rotations"])
        return action


def _loop():
    w = _Watch()
    while True:
        time.sleep(CHECK_S)
        try:
            with _lock:
                pools = [rp for p in _pools for rp in _respawning(p)]
            mbs, total = measure()
            w.step(mbs, total, _avail_mb(), pools)
        except Exception as e:      # (never let the watchdog die)
            log.debug("memwatch: %s: %s", type(e).__name__, e)
