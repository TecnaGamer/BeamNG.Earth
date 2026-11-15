"""Restarting the world server from the game (the user, 2026-11-15: "For any options that require a server restart can
we add a button to restart the server without having to close beamng"): the settings page's "Restart world server".

Some settings are read only when the server starts: the Experimental page's switches (experimental.py: BNE_* build
switches the launcher puts in the server's environment, read at import in every process), the ground grid (the
terrain block's square, heightmap.SQUARE), the cores, the priority and the 3dmr.eu models (cpupin.apply_player_settings)
and the caches' sizes for Max RAM (memwatch.status restart). The flow:

  1. the server tells the mod, in its hello, the start-time values of those settings (start_view: the settings file
     as the server's supervisor and the server read it at this start) and whether a supervisor will start it again
     (restartable: ENV, set by launcher.start_server and start.py); the mod compares them with its settings and shows
     the button when one differs (srvrestart.lua);
  2. the mod's 'server_restart' asks for it; the server answers, closes its connections, the network and the download
     cache (main_async's finally), stops its worker processes and exits with EXIT_CODE (finish);
  3. the supervisor sees EXIT_CODE and starts the server again with the same arguments, reading the settings anew
     (experimental_env), at most MAX_RESTARTS in WINDOW_S (Limiter: never in a loop). Any other exit code is a stop or
     a crash, never restarted;
  4. the mod reconnects as after any server restart (the session folders kept, the anchor's resync), its car held
     meanwhile (srvrestart.lua, core.lua).

Glue only: no world-building logic here (the rules for the values are the settings' own: cpupin, experimental).
"""
import json
import os

EXIT_CODE = 85                          # (not a Python, signal or Windows crash code: a crash is never a restart)
ENV = "BNGEARTH_RESTARTABLE"            # "1": a supervisor starts the server again on EXIT_CODE (not BNE_*: no key)
START_ENV = "BNGEARTH_START_SETTINGS"   # JSON: the supervisor's part of start_view (the values it applied)
MAX_RESTARTS = 5                        # restarts a supervisor makes in WINDOW_S; more: the server stays stopped
WINDOW_S = 600.0
HARD_EXIT_S = 25.0                      # s the clean stop may take before the process exits anyway (finish_soon)
CHILD_JOIN_S = 5.0                      # s each worker process gets to end after terminate()

# The settings whose start-time values the server reports: those a supervisor applies (put in the environment before
# the server starts) and those the server applies itself at its start (cpupin.apply_player_settings)
SUPERVISOR_KEYS = ("groundGrid",)       # (+ every experimental.IDS)
SERVER_KEYS = ("serverCores", "serverPriority", "models3dmr")


def view(settings):
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
    return out


def supervisor_part(settings):
    """The part of view() a supervisor applies (the experimental switches, the ground grid), for START_ENV."""
    import experimental
    v = view(settings)
    return {k: v[k] for k in SUPERVISOR_KEYS + tuple(experimental.IDS)}


def supervisor_env(env, settings):
    """A supervisor's server environment env (a dict) marked restartable, with the values it applied (START_ENV)."""
    env[ENV] = "1"
    env[START_ENV] = json.dumps(supervisor_part(settings), sort_keys=True, separators=(",", ":"))
    return env


def start_view(user_dir, environ=None):
    """This server's start-time view: the settings file now (the server's own part, read as cpupin read it), the
    supervisor's part as it applied it (START_ENV; it read the file before this process started)."""
    import experimental
    environ = os.environ if environ is None else environ
    out = view(experimental.read_settings(user_dir))
    try:
        given = json.loads(environ.get(START_ENV) or "{}")
    except ValueError:
        given = {}
    if isinstance(given, dict):
        mine = set(SUPERVISOR_KEYS) | set(experimental.IDS)      # (the server's own part is what it read itself)
        for k, v in given.items():
            if k in mine and k in out and type(v) is type(out[k]):
                out[k] = v
    return out


def restartable(environ=None):
    """A supervisor starts this server again when it exits with EXIT_CODE."""
    environ = os.environ if environ is None else environ
    return environ.get(ENV) == "1"


class Limiter:
    """A supervisor's restarts: at most MAX_RESTARTS in any WINDOW_S (a server that asks again and again, a bug, stays
    stopped instead of restarting in a loop)."""

    def __init__(self, most=MAX_RESTARTS, window_s=WINDOW_S):
        self.most, self.window_s, self.times = most, window_s, []

    def allow(self, now):
        self.times = [t for t in self.times if now - t < self.window_s]
        if len(self.times) >= self.most:
            return False
        self.times.append(now)
        return True


def wants_restart(code):
    """The exit code of a server that asked to be started again (and nothing else: a crash, a stop, a kill)."""
    return code == EXIT_CODE


def stop_children(log=None, join_s=CHILD_JOIN_S):
    """This process's worker processes ended (terminated, then killed): none may keep the port or the shared memory
    for the next server. Returns how many there were."""
    import multiprocessing
    kids = multiprocessing.active_children()
    for p in kids:
        try:
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
            stop_children(None, join_s=1.0)
        finally:
            os._exit(EXIT_CODE)
    threading.Thread(target=guard, name="restart-guard", daemon=True).start()
