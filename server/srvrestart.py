"""Restarting the world server from the game (the user, 2026-11-15: "For any options that require a server restart can
we add a button to restart the server without having to close beamng"): the settings page's "Restart world server".

Some settings are read only when the server starts: the Experimental page's switches (experimental.py: BNE_* build
switches the launcher puts in the server's environment, read at import in every process), the ground grid (the
terrain block's square, heightmap.SQUARE), the cores, the priority and the 3dmr.eu models (cpupin.apply_player_settings)
and the caches' sizes for Max RAM (memwatch.status restart). The flow:

  1. the server tells the mod, in its hello, the start-time values of those settings (start_view: as applied at this
     start), the ones set by hand that the page cannot change (fixed) and whether a supervisor will start it again
     (restartable: ENV, set by launcher.start_server and start.py); the mod compares them with its settings and shows
     the button when one differs (srvrestart.lua);
  2. the mod's 'server_restart' asks for it with the key the server wrote in the game's folder (write_key: a stray
     local client cannot ask), on the connection that said the game's hello; the server refuses when its supervisor's
     limit is reached (budget_refusal: never a stopped server), else answers, closes its connections, the network and
     the download cache (main_async's finally), stops its worker processes and exits with EXIT_CODE (finish);
  3. the supervisor sees EXIT_CODE and starts the server again with the same arguments, reading the settings anew
     (experimental_env, apply_ground), at most MAX_RESTARTS in WINDOW_S (Limiter: never in a loop). Any other exit code
     is a stop or a crash, never restarted;
  4. the mod reconnects as after any server restart (the session folders kept, the anchor's resync), its car held
     meanwhile (srvrestart.lua, core.lua).

Glue only: no world-building logic here (the rules for the values are the settings' own: cpupin, experimental).
"""
import json
import os

EXIT_CODE = 85                          # (not a Python, signal or Windows crash code: a crash is never a restart)
ENV = "BNGEARTH_RESTARTABLE"            # "1": a supervisor starts the server again on EXIT_CODE (not BNE_*: no key)
START_ENV = "BNGEARTH_START_SETTINGS"   # JSON {values, fixed}: the supervisor's part of start_view (what it applied)
TIMES_ENV = "BNGEARTH_RESTART_TIMES"    # the supervisor's restarts in WINDOW_S (time.time(), comma-separated)
KEY_FILE = "restart.key"                # in the game's temp/beamngEarth: the restart capability (write_key)
MAX_RESTARTS = 5                        # restarts a supervisor makes in WINDOW_S; the server refuses more (Limiter)
WINDOW_S = 600.0
HARD_EXIT_S = 25.0                      # s the clean stop may take before the process exits anyway (finish_soon)
CHILD_JOIN_S = 5.0                      # s each worker process gets to end after terminate()

# The settings whose start-time values the server reports: those a supervisor applies (put in the environment before
# the server starts) and those the server applies itself at its start (cpupin.apply_player_settings)
SUPERVISOR_KEYS = ("groundGrid",)       # (+ every experimental.IDS)
SERVER_KEYS = ("serverCores", "serverPriority", "models3dmr")
GROUND_ENV = "BNGE_GROUND_SQUARE"       # (heightmap.ground_square)


def view(settings, environ=None):
    """{setting id: value} of the restart-needing settings in a settings file's dict, as the server takes them (the
    same rules as cpupin.player_settings and experimental.chosen; values as the mod's settings.lua holds them)."""
    import cpupin
    import experimental
    d = settings if isinstance(settings, dict) else {}
    out = {}
    grid = d.get("groundGrid")
    if grid is None and str(d.get("quality", "")) in cpupin.LIGHT_GROUND_PRESETS:
        grid = "2"
    out["groundGrid"] = "2" if str(grid) == "2" else "1"
    cores = str(d.get("serverCores", "auto"))
    out["serverCores"] = cores if cores in cpupin.CORE_CHOICES else "auto"
    out["serverPriority"] = "normal" if str(d.get("serverPriority", "low")) == "normal" else "low"
    out["models3dmr"] = d.get("models3dmr") is not False
    try:
        version = int(d.get("version") or 1)
    except (TypeError, ValueError, OverflowError):
        version = 1
    for sid in experimental.IDS:
        out[sid] = d.get(sid) is True and version >= experimental.SINCE_VERSION.get(sid, 0)
    import cliffsettings
    if cliffsettings.available(environ):
        out.update(cliffsettings.choices(d))
    return out


def apply_ground(env, settings):
    """The settings page's Ground grid into a server environment env (BNGE_GROUND_SQUARE "2" for 2 m), unless one is
    set there already (by hand: it wins). The launcher's and start.py's rule (heightmap.ground_square reads it)."""
    if not env.get(GROUND_ENV) and view(settings)["groundGrid"] == "2":
        env[GROUND_ENV] = "2"
    return env


def _grid_of(value):
    """The Ground grid setting ("1" / "2") a BNGE_GROUND_SQUARE value puts in force (heightmap.ground_square's rule)."""
    try:
        return "2" if float(str(value or "").strip() or 1.0) == 2.0 else "1"
    except ValueError:
        return "1"


def supervisor_part(settings, env, base_env):
    """What a supervisor applied, for START_ENV: {"values": the experimental switches as the settings file asked for
    them, "fixed": the settings the page cannot change (their variables set by hand in base_env, the environment
    before the supervisor's, or differing in env from what the file asks for: a dev's BNGE_SERVER_ENV)}. The ground
    grid's value is the server's own (heightmap.SQUARE); only whether it was set by hand comes from here."""
    import experimental
    d = settings if isinstance(settings, dict) else {}
    v = view(d, env)
    wanted = experimental.env_for(d)
    fixed = []
    for sid, feature_env, _needs in experimental.FEATURES:
        if any(base_env.get(k) or env.get(k) != wanted.get(k) for k in feature_env):
            fixed.append(sid)
    if base_env.get(GROUND_ENV) or _grid_of(env.get(GROUND_ENV)) != v["groundGrid"]:
        fixed.append("groundGrid")      # (by hand, or a dev's BNGE_SERVER_ENV square other than the page's)
    import cliffsettings
    values = {sid: v[sid] for sid in experimental.IDS}
    if cliffsettings.available(env):
        values.update(cliffsettings.choices(d))
    return {"values": values, "fixed": fixed}


def supervisor_env(env, settings, base_env=None, times=()):
    """A supervisor's server environment env (a dict, its settings applied) marked restartable, with what it applied
    (START_ENV; base_env: the environment before it applied anything) and its restarts so far (TIMES_ENV)."""
    base_env = {} if base_env is None else base_env
    env[ENV] = "1"
    env[START_ENV] = json.dumps(supervisor_part(settings, env, base_env), sort_keys=True, separators=(",", ":"))
    env[TIMES_ENV] = ",".join(f"{t:.3f}" for t in times)
    return env


def start_view(user_dir, environ=None, square=None, hand=()):
    """This server's start-time view: ({setting id: value}, [ids the page cannot change]). The values: the server's
    own settings as it read them now (cpupin), the ground grid as it is in force (square: heightmap.SQUARE), the
    experimental switches as the supervisor applied them (START_ENV; it read the file before this process started).
    hand: the server's own settings set by hand (an environment variable or option the page's value cannot beat)."""
    import experimental
    environ = os.environ if environ is None else environ
    import cliffsettings
    out = view(experimental.read_settings(user_dir), environ)
    if square is not None:
        out["groundGrid"] = "2" if float(square) == 2.0 else "1"
    try:
        given = json.loads(environ.get(START_ENV) or "{}")
    except ValueError:
        given = {}
    fixed = {k for k in hand if k in out}
    if isinstance(given, dict):
        values = given.get("values")
        if isinstance(values, dict):
            for k, v in values.items():
                if k in out and (k in experimental.IDS or k in cliffsettings.KEYS) and type(v) is type(out[k]):
                    out[k] = v
        if isinstance(given.get("fixed"), list):
            fixed |= {k for k in given["fixed"] if isinstance(k, str) and (k in experimental.IDS or k in SUPERVISOR_KEYS)}
    return out, sorted(fixed)


def restartable(environ=None):
    """A supervisor starts this server again when it exits with EXIT_CODE."""
    environ = os.environ if environ is None else environ
    return environ.get(ENV) == "1"


class Limiter:
    """A supervisor's restarts: at most MAX_RESTARTS in any WINDOW_S (a server that asks again and again, a bug, stays
    stopped instead of restarting in a loop). Times are time.time(): the server gets them (TIMES_ENV, from_env) and
    refuses a request the supervisor would not honour (would_allow), so asking too often never leaves the player with a
    stopped server; times only age out, so the supervisor, deciding a moment later, never refuses what it allowed."""

    def __init__(self, most=MAX_RESTARTS, window_s=WINDOW_S, times=()):
        self.most, self.window_s, self.times = most, window_s, list(times)

    @classmethod
    def from_env(cls, environ=None):
        environ = os.environ if environ is None else environ
        times = []
        for part in (environ.get(TIMES_ENV) or "").split(","):
            try:
                t = float(part)
            except ValueError:
                continue
            if t == t:
                times.append(t)
        return cls(times=times)

    def _recent(self, now):
        return [t for t in self.times if now - t < self.window_s]

    def would_allow(self, now):
        return len(self._recent(now)) < self.most

    def allow(self, now):
        self.times = self._recent(now)
        if len(self.times) >= self.most:
            return False
        self.times.append(now)
        return True

    def wait_s(self, now):
        """s until one more is allowed (0: now)."""
        recent = sorted(self._recent(now))
        if len(recent) < self.most:
            return 0.0
        return max(0.0, recent[len(recent) - self.most] + self.window_s - now)


def budget_refusal(environ=None, now=None):
    """Why this server must not restart now (its supervisor's limit reached: it would stay stopped), or None."""
    import time
    now = time.time() if now is None else now
    lim = Limiter.from_env(environ)
    if lim.would_allow(now):
        return None
    return (f"the world server was restarted {MAX_RESTARTS} times in the last {WINDOW_S / 60:g} minutes: try again in "
            f"{max(1, round(lim.wait_s(now) / 60))} min, or start BeamNG.Earth again")


def write_key(root):
    """A fresh restart capability (a random key) written to root/KEY_FILE (the game's temp/beamngEarth: the mod reads
    it as /temp/beamngEarth/restart.key; private to the user on Linux), or None when it cannot be written. Only a
    client that can read the game's own folder can ask for a restart: not another user's process, not a stray tool."""
    import secrets
    key = secrets.token_hex(16)
    path = os.path.join(root, KEY_FILE)
    tmp = path + ".tmp"
    try:
        os.makedirs(root, exist_ok=True)
        try:
            os.remove(tmp)
        except OSError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(key)
        os.replace(tmp, path)
    except OSError:
        return None
    return key


def key_ok(expected, given):
    """The request's key is this server's (constant time)."""
    import hmac
    return isinstance(expected, str) and isinstance(given, str) and bool(expected) and \
        hmac.compare_digest(expected.encode(), given.strip().encode())


def wants_restart(code):
    """The exit code of a server that asked to be started again (and nothing else: a crash, a stop, a kill)."""
    return code == EXIT_CODE


def _stop_name(pid):
    return rf"Local\BeamNGEarth-stop-{pid}"


def wait_parent_or_stop(parent):
    """Windows parent watcher. Wait for the parent handle (0) or our named stop event (1)."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.WaitForMultipleObjects.restype = wintypes.DWORD
    k32.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
                                          wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    stop = k32.CreateEventW(None, True, False, _stop_name(os.getpid()))
    handles = (wintypes.HANDLE * 2)(parent, stop)
    try:
        return k32.WaitForMultipleObjects(2 if stop else 1, handles, False, 0xFFFFFFFF)
    finally:
        if stop:
            k32.CloseHandle(stop)


def request_stop(pid):
    """Windows TerminateProcess skips exit hooks. Wake the owned process's parent watcher to drain and exit."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenEventW.restype = wintypes.HANDLE
    k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.SetEvent.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenEventW(0x0002, False, _stop_name(pid))  # EVENT_MODIFY_STATE
    if not handle:
        return False
    try:
        return bool(k32.SetEvent(handle))
    finally:
        k32.CloseHandle(handle)


def stop_children(log=None, join_s=CHILD_JOIN_S):
    """This process's worker processes ended (terminated, then killed): none may keep the port or the shared memory
    for the next server. Returns how many there were."""
    import multiprocessing
    kids = multiprocessing.active_children()
    for p in kids:
        try:
            if os.name != "nt" or not request_stop(p.pid):
                p.terminate()
        except Exception:
            pass
    for p in kids:
        try:
            p.join(join_s)
            if p.is_alive():
                p.kill()
                p.join(1.0)
        except Exception:
            pass
    if log is not None and kids:
        log.info("restart: %d worker processes stopped", len(kids))
    return len(kids)


def drain_before_kill(join_s):
    """Give both the local listener and worker processes time to flush before forced tree cleanup."""
    import threading
    import serverlog
    drain = threading.Thread(target=serverlog.shutdown, name="log-drain", daemon=True)
    drain.start()
    try:
        stop_children(join_s=join_s)
    finally:
        drain.join(join_s)


def finish(log=None):
    """The end of a restart's clean stop (after main_async's own clean-up): the workers stopped, the exit handlers run
    (the add-ons' programs, the shared memory tables, the download counters), the log flushed; exits with EXIT_CODE
    (os._exit: the thread pools' jobs in flight are not waited for, as when the launcher stops the server)."""
    import atexit
    import logging
    import sys
    try:
        stop_children(log)
    except Exception:
        pass
    if log is not None:
        log.warning("restart: the world server stops now and is started again (exit code %d)", EXIT_CODE)
    try:
        atexit._run_exitfuncs()         # (logging's own shutdown among them: the log flushed)
    except Exception:
        pass
    try:
        logging.shutdown()
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(EXIT_CODE)


def finish_soon(hard_s=HARD_EXIT_S):
    """A guard for the clean stop: if it has not exited within hard_s (a connection that does not close, a pool that
    hangs), the process exits with EXIT_CODE anyway, its workers stopped first."""
    import threading
    import time

    def guard():
        time.sleep(hard_s)
        try:
            drain_before_kill(join_s=1.0)
        finally:
            os._exit(EXIT_CODE)
    threading.Thread(target=guard, name="restart-guard", daemon=True).start()
