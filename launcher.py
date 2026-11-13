#!/usr/bin/env python3
"""BeamNG.Earth launcher: one start for everything (the AppImage and the .exe run this; from the source folder:
python launcher.py).

  1. copies the mod into the game's user folder when it differs (one zip, one version),
  2. starts the world server (server/earthd.py) with the recommended options,
  3. starts BeamNG.drive unless it already runs. Linux: the native game directly, with tools/gcpatch preloaded
     (keeps the grass when the physics block moves; it checks the game build and does nothing on another), so no
     Steam launch options are needed — the direct start adopts them anyway (their environment, a wrapper command
     and the game's options after %command%), and puts the game on a hybrid laptop's discrete GPU when the default
     one is the weaker (the integrated one; --gpu off says no); through Steam when that is not possible (Steam not
     running) and for the Windows build under Proton (found from Steam's compatibility setting, else from the user
     folder the game wrote last). Windows: through Steam. Under Wine / Proton (the .exe started with protontricks)
     it cannot start the game: it asks you to press Play in Steam.
  4. stops the server when the game closes (or on Ctrl+C / closing the window).

It runs in a console window, like BeamMP's launcher: the .exe opens one when it has none, the AppImage opens a
terminal when started from a file manager (else it logs to ~/.cache/beamng-earth/logs).

    launcher.py                  everything
    launcher.py --no-game        only the server (start the game yourself)
    launcher.py --no-gcpatch     the game without the engine patches (tools/gcpatch, enginepatch.py)
    launcher.py --proton / --native   Linux: which build of the game to start (default: found)
    launcher.py --gpu off        hybrid graphics: never steer the game's GPU (auto does, discrete forces it)
    launcher.py --clear-cache    delete the map download cache first (instant; the files go in the background)
    launcher.py -- --verbose     anything after -- goes to the server
"""

import argparse
import multiprocessing
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

import apppaths

FROZEN = getattr(sys, "frozen", False)
ROOT = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == "nt"
APP_ID = "284160"
GAME_EXE_LINUX = os.path.join("BinLinux", "BeamNG.drive.x64")
GAME_EXE_WIN = "BeamNG.drive.x64.exe"
GCPATCH_NAME = "libbngearth_gcpatch.so"
# the recommended server options (start.py's; test_gasstations checks they are the same: PR #220 added gas stations
# to start.py only, and the app's server never allowed them)
RECOMMENDED = ["--deshadow", "full", "--near-visual", "detail", "--airports", "on", "--sea", "on", "--trees", "on",
               "--buildings", "on", "--gas-stations", "on"]
SERVER_PORT = 47613         # earthd's default
GAME_GONE_S = 30            # no game process this long: the game closed (wait_for_game) ...
GAME_RESTART_WINDOW_S = 120  # ... while it starts (Steam's container restarts it then); after that GAME_CLOSED_S:
GAME_CLOSED_S = 5           # the launcher sat 30 s after the player closed the game (the user, 2026-11-04)
WINDOWS_GAME_GONE_S = 8     # Windows has no Steam container restart gap; prompt, but a few queries long (a player's
                            # launcher closed with the game still running, 2026-11-05: see game_pids)
GAME_START_S = 180          # waiting for the game's process after asking Steam
SERVER_START_S = 120        # imports/startup must finish before the game is launched
PROCESS_QUERY_S = 3         # a stuck process query must not hang the launcher
GAME_QUERY_S = 10           # tasklist for the game: slow on a loaded machine (the game and the server on 6 cores)
DIRECT_FAIL_S = 20          # a direct start that ends this soon did not work (no Steam client): through Steam


# the system's CA bundle on the common distributions (Debian/Ubuntu/Arch, Fedora/RHEL, openSUSE, Alpine)
CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
              "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem", "/etc/ssl/ca-bundle.pem", "/etc/ssl/cert.pem")


def ensure_ca_certs():
    """Linux, the packaged app: its OpenSSL (built on Ubuntu) looks for the CA certificates in /usr/lib/ssl, which
    only Debian / Ubuntu have; elsewhere (Fedora, Arch...) every HTTPS request failed ("tiles.mapterhorn.com
    unavailable (circuit open)", a player's report on Fedora 44, 2026-10-17). SSL_CERT_FILE (the server inherits
    it) points at the system's bundle, else at certifi's copy in the app."""
    if IS_WIN or os.environ.get("SSL_CERT_FILE"):
        return
    import ssl
    paths = ssl.get_default_verify_paths()
    if (paths.cafile and os.path.isfile(paths.cafile)) or \
            (paths.capath and os.path.isdir(paths.capath) and os.listdir(paths.capath)):
        return
    for f in CA_BUNDLES:
        if os.path.isfile(f):
            os.environ["SSL_CERT_FILE"] = f
            return
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass


def _out(text):
    try:
        print(text, flush=True)
    except (OSError, ValueError):       # the terminal window already closed
        pass


def say(msg=""):
    """A line of the launcher's own (indented under the title; the user, 2026-10-17: 'easier to read')."""
    _out(f"  {msg}" if msg else "")


def row(label, value):
    """A status line: label in a column, then its value."""
    _out(f"  {label:<14}{value}")


def title(text):
    _out("")
    _out(f"  {text}")
    _out("  " + "-" * max(24, len(text)))


DISCORD = "https://discord.gg/tgf"


def support_notice(multiplayer=False):
    """Where to get help (the BeamMP team, 2026-10-27: players kept asking them about BeamNG.Earth): the Discord once at
    every start; once BeamMP is up, what it means for multiplayer (said once each: the user, 2026-10-27)."""
    if not multiplayer:
        say("Join the TGForever Discord to chat, get help, report bugs or help develop")
        say(f"the mod: {DISCORD}")
        return
    _out("")
    _out("  " + "=" * 78)
    _out("  BeamNG.Earth is not like a normal BeamNG mod: it needs this app running")
    _out("  next to the game. If something is not working in multiplayer, it is almost")
    _out("  certainly a BeamNG.Earth problem, not a BeamMP one: ask on the TGForever")
    _out("  Discord above, and do NOT contact the BeamMP team or open BeamMP support")
    _out("  tickets about it.")
    _out("  BeamNG.Earth servers are in the \"BeamNG.Earth\" tab of BeamMP's server browser.")
    _out("  " + "=" * 78)


def memory_notice(notice=None):
    """A computer with too little memory for the game (8 GB, 4 GB: server/budget.py's lean tiers, memory_notice): said
    at every start, set apart, before anything else happens. It never stops the start. True when shown."""
    import textwrap
    if notice is None:
        try:
            sys.path.insert(0, os.path.join(ROOT, "server"))
            import budget
            notice = budget.memory_notice()
        except Exception:           # (never in the way of a start)
            return False
    if not notice:
        return False
    head, text = notice
    _out("")
    _out("  " + "=" * 78)
    _out(f"  {head}:")
    for line in textwrap.wrap(text, 76):
        _out(f"  {line}")
    _out("  " + "=" * 78)
    return True


def problem(msg, hint=None):
    """Something that stops the start: set apart, with what to do about it."""
    import textwrap
    _out("")
    _out("  Problem: " + "\n           ".join(textwrap.wrap(msg, 78)))
    if hint:
        for line in textwrap.wrap(hint, 78):
            _out(f"           {line}")
    _out("")


# where the launcher keeps its files and remembered choices (app.json: cacheDir, gameDir), shared with the server's
# settings page (apppaths.py)
data_dir, app_settings, save_app_settings = apppaths.data_dir, apppaths.app_settings, apppaths.save_app_settings


def cache_dir_arg(given, dd=None):
    """The download cache's folder for the server (GitHub #254: "re locate the map files to a different hard drive"):
    --cache-dir PATH is remembered for the next starts, --cache-dir default forgets it. Returns the folder, or None
    (the server's default, .cache/beamng-earth in the home folder). Files already downloaded stay where they were: the
    player moves the folder over (or they are downloaded again)."""
    if given is not None:
        if given.strip().lower() in ("", "default"):
            save_app_settings(dd, cacheDir=None)
            return None
        bad = apppaths.drive_relative_problem(given)
        if bad:                         # (GitHub #895: not the current folder in silence)
            raise OSError(bad)
        path = os.path.abspath(os.path.expanduser(given.strip().strip('"')))
        os.makedirs(path, exist_ok=True)
        save_app_settings(dd, cacheDir=path)
        return path
    path = app_settings(dd).get("cacheDir")
    return path if isinstance(path, str) and path else None


def clear_cache(extra):
    """--clear-cache (GitHub #1081: deleting the cache's many small files took a player very long): the download cache
    the server is about to use (its --cache-dir in extra, else the default) moved aside at once (cache.clear_all); the
    server deletes it in the background after it starts. Only while no server runs (the caller checked the port)."""
    path = apppaths.default_cache_dir()
    for i, a in enumerate(extra):
        if a == "--cache-dir" and i + 1 < len(extra):
            path = extra[i + 1]
        elif a.startswith("--cache-dir="):
            path = a.split("=", 1)[1]
    sys.path.insert(0, os.path.join(ROOT, "server"))
    try:
        import cache as cachemod
        moved = cachemod.clear_all(path)
    except Exception as e:              # (never in the way of a start)
        row("Download cache", f"not cleared ({type(e).__name__}: {e})")
        return
    row("Download cache", f"cleared ({len(moved)} folders in {path}; their files are deleted in the background)"
        if moved else f"nothing to clear in {path}")


# --- the server -----------------------------------------------------------------------------------

def watch_parent():
    """The server's guard: it ends (with its worker processes) when the launcher that started it is gone, e.g. its
    terminal closed (SIGHUP) or it was killed. A server left behind kept the port, so the next start's server could
    not listen and the game talked to the old one (the user's AppImage, 2026-10-16)."""
    try:
        parent = int(os.environ.get("BNGEARTH_PARENT") or 0)
    except ValueError:
        parent = 0
    if parent <= 0:
        return
    import threading

    def gone():
        if IS_WIN:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            k32.WaitForSingleObject.restype = wintypes.DWORD
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            k32.CloseHandle.restype = wintypes.BOOL
            warned = False
            while True:
                h = k32.OpenProcess(0x00100000, False, parent)  # SYNCHRONIZE
                if not h:
                    error = ctypes.get_last_error()
                    if error == 87:                           # ERROR_INVALID_PARAMETER: PID is gone
                        return True
                else:
                    try:
                        result = k32.WaitForSingleObject(h, 0xFFFFFFFF)
                        if result == 0:                      # WAIT_OBJECT_0: process exited
                            return True
                        error = ctypes.get_last_error()
                    finally:
                        k32.CloseHandle(h)
                # Access denied or a failed wait does not establish that the
                # launcher exited. Retry without killing a possibly live session.
                if not warned:
                    print(f"[BeamNG.Earth] cannot confirm launcher state (Windows error {error}); retrying",
                          flush=True)
                    warned = True
                time.sleep(2)
        while os.getppid() == parent:
            time.sleep(2)
        return True

    def run():
        gone()
        print("[BeamNG.Earth] the launcher is gone: the world server stops", flush=True)
        if IS_WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(os.getpid())], capture_output=True)
        else:
            os.killpg(os.getpgid(0), signal.SIGTERM)
        time.sleep(10)
        os._exit(1)

    threading.Thread(target=run, name="watch-parent", daemon=True).start()


def run_server(argv):
    """In this process: server/earthd.py with argv (the --server mode of the packaged app)."""
    watch_parent()
    sys.path.insert(0, os.path.join(ROOT, "server"))
    import earthd
    sys.argv = ["earthd"] + argv
    earthd.main()


def run_addon(argv):
    """--run-addon FILE ARGS: a companion add-on's Python file run with this app's own Python (addonrun.py: the packaged
    app has no other), ended when the server that started it ends (BNGEARTH_PARENT: watch_parent)."""
    watch_parent()
    import runpy
    script = argv[0]
    sys.argv = [script] + list(argv[1:])
    sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
    runpy.run_path(script, run_name="__main__")


def telemetry_on():
    """Anonymous usage reports (server/telemetry.py): on for players unless BNE_TELEMETRY=off; never under CI or a test
    run (the first night's 'players' were mostly the release workflow's Windows test servers: each a new install)."""
    if os.environ.get("CI") or "unittest" in sys.modules:
        return False
    return os.environ.get("BNE_TELEMETRY", "on").lower() not in ("off", "0", "false", "no")


def ground_square_setting(user_dir):
    """The terrain block's square the player's settings page chose (cpupin.player_settings' ground_square: "2" for
    the lighter ground of the Low and Medium presets), or None (1 m, the server's default). Read at every server start:
    a change on the page holds from the next one (the block size is fixed for a server's run)."""
    try:
        here = os.path.join(ROOT, "server")
        if here not in sys.path:
            sys.path.insert(0, here)
        import cpupin
        return cpupin.player_settings(user_dir).get("ground_square")
    except Exception:           # (an unreadable file: the default)
        return None


def start_server(user_dir, extra, log_path, game_running=None):
    cmd = [sys.executable] + ([] if FROZEN else [os.path.abspath(__file__)])
    cmd += ["--server", "--user-dir", user_dir] + RECOMMENDED + extra
    if telemetry_on() and "--offline" not in extra:
        cmd += ["--telemetry", "on"]
    env = dict(os.environ)
    env["BNGEARTH_PARENT"] = str(os.getpid())      # the server ends when this launcher does (watch_parent)
    if sys.platform.startswith("linux"):    # (gamescope's WSI layer aborted the server's GPU kernels: earthd.main)
        env.setdefault("DISABLE_GAMESCOPE_WSI", "1")
    try:            # (the game's own files: an add-on mod replacing one needs a restart, addonrun.mod_code)
        gd = game_install_dir()
        if gd:
            env["BNGEARTH_GAME_DIR"] = str(gd)
    except Exception:
        pass
    if game_running is not None:    # (whether a game ran before this start: earthd.game_running, its earlier sessions)
        env["BNGEARTH_GAME_AT_START"] = "1" if game_running else "0"
    if not env.get("BNGE_GROUND_SQUARE"):   # the settings page's ground grid (the Low / Medium presets: 2 m, GitHub #943)
        sq = ground_square_setting(user_dir)
        if sq:
            env["BNGE_GROUND_SQUARE"] = sq
    # (dev: BNGE_SERVER_ENV="LD_PRELOAD=/usr/lib/libjemalloc.so MALLOC_CONF=prof:true": variables for the server
    # alone, not the game, e.g. a heap profiler)
    for kv in os.environ.get("BNGE_SERVER_ENV", "").split():
        k, sep, v = kv.partition("=")
        if sep and k:
            env[k] = v
    kw = {"cwd": ROOT, "env": env}
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True          # its worker processes with it: stopped as one group
    # its full log to a file; the console shows only its warnings and errors (echo_server_log): the INFO lines
    # buried the launcher's own (the user, 2026-10-17: 'easier to read')
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    keep_previous_log(log_path)
    with open(log_path, "w", encoding="utf-8") as out:
        kw.update(stdout=out, stderr=subprocess.STDOUT)
        proc = subprocess.Popen(cmd, **kw)
    threading.Thread(target=echo_server_log, args=(log_path, proc), daemon=True).start()
    return proc


ECHO_LEVELS = (" WARNING ", " ERROR ", " CRITICAL ")


def echo_lines(lines, state):
    """The lines of the server's log the console shows: warnings, errors and tracebacks (with their lines up to
    the next log record), and 'Game connected' once. state: a dict kept between calls. Pure but for state."""
    out = []
    for line in lines:
        line = line.rstrip("\n")
        record = line[:4].isdigit() and line[4:5] == "-"        # '2026-10-17 07:04:50,488 INFO earthd: ...'
        if record or line.startswith("earthd listening"):
            state["tb"] = False
        if line.startswith("Traceback"):
            state["tb"] = True
        if "client connected" in line and not state.get("connected"):
            state["connected"] = True
            out.append("  Game          connected to the world server")
        elif state.get("tb") or (record and any(k in line for k in ECHO_LEVELS)):
            out.append("  server: " + (line[11:] if record else line))
    return out


def keep_previous_log(path, keep=2):
    """The last runs' logs as server.log.1 .. .<keep> (GitHub #687: a crashed session's log, with Python's fatal error
    dump, was overwritten by the next start before the player could attach it)."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return
        for i in range(keep, 1, -1):
            older = f"{path}.{i - 1}"
            if os.path.isfile(older):
                os.replace(older, f"{path}.{i}")
        os.replace(path, path + ".1")
    except OSError:
        pass                    # (still open somewhere on Windows: overwritten as before)


def echo_server_log(path, proc):
    state = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            while True:
                lines = f.readlines()
                for text in echo_lines(lines, state):
                    _out(text)
                if not lines:
                    if proc.poll() is not None:
                        return
                    time.sleep(0.5)
    except OSError:
        pass


def server_port_busy(port=SERVER_PORT):
    """Something already listens on the world server's port (a server still running from an earlier start)."""
    import socket
    if IS_WIN:              # (the system's TCP table: a refused connection costs Windows ~1 s, bmplaunch.listening)
        import bmplaunch
        seen = bmplaunch.windows_listening(port)
        if seen is not None:
            return seen
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _startup_tail(log_path):
    """A bounded, complete-line tail: startup failures can produce very large tracebacks/logs."""
    try:
        with open(log_path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            start = max(0, stream.tell() - 65536)
            stream.seek(start)
            tail = stream.read(65536)
        return tail.partition(b"\n")[2] if start else tail
    except OSError:
        return b""


def _startup_phase(tail):
    phase = None
    for line in tail.decode("utf-8", errors="replace").splitlines():
        if " INFO earthd: startup: " in line:
            phase = line.split(" INFO earthd: startup: ", 1)[1][:180]
    return phase


def server_start_diagnostic(proc, log_path):
    """Describe exit versus deadline and the last known step/error, without dumping the whole log."""
    import re
    tail = _startup_tail(log_path)
    code = proc.poll()
    text = (f"The companion stopped before it was ready (exit code {code})." if code is not None else
            "The companion is still running but did not become ready before the startup deadline.")
    phase = _startup_phase(tail)
    if phase:
        text += " Last startup step: " + phase + "."
    error = None
    for line in tail.decode("utf-8", errors="replace").splitlines():
        if " ERROR " in line or " CRITICAL " in line or re.match(r"^[\w.]+(?:Error|Exception):", line):
            error = line.split(": ", 1)[-1] if " ERROR " in line or " CRITICAL " in line else line
    if error:
        error = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", error)[:400]
        text += " Last error: " + error
    return text


def wait_server_ready(proc, log_path, port=SERVER_PORT, timeout=SERVER_START_S):
    """Read the owned child's post-bind marker from its freshly truncated startup log.

    Do not connect to the game protocol just to test readiness: that creates a fake game session.
    Only the last 64 KiB are read each poll, even when startup produced a large log.
    """
    marker = b"earthd listening on 127.0.0.1:"
    deadline = time.monotonic() + timeout
    last_phase = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            tail = _startup_tail(log_path)
            phase = _startup_phase(tail)
            if phase and phase != last_phase:
                row("World server", phase)
                last_phase = phase
            for line in tail.splitlines():
                if not line.startswith(marker):
                    continue
                number, separator, _ = line[len(marker):].partition(b" ")
                if separator and number.isdigit() and 0 < int(number) < 65536:
                    if port == 0 or int(number) == port:
                        return proc.poll() is None
        except OSError:
            pass
        time.sleep(0.25)
    return False


# The game is started before the world server is waited for (run): BNGE_GAME_FIRST=off waits for the server first, as
# before 2026-11-12
GAME_FIRST = os.environ.get("BNGE_GAME_FIRST", "on").lower() not in ("off", "0", "false", "no")


def server_up(server, log_path, port, game_started):
    """The world server's start followed (wait_server_ready's steps); one that does not come up stops this start with
    what went wrong (SystemExit)."""
    if wait_server_ready(server, log_path, port):
        row("World server", "running")
        return
    problem("the world server did not start listening; " + (
        "close the game, then start BeamNG.Earth again." if game_started else "the game was not started."),
        server_start_diagnostic(server, log_path) + f"\nFull startup log: {log_path}")
    raise SystemExit(1)


def stop_server(proc):
    if proc is None or proc.poll() is not None:
        return
    row("World server", "stopping")
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(15)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if IS_WIN:
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


# --- the game -------------------------------------------------------------------------------------

def game_install_dir():
    """The game's install folder (its shaders/, BinLinux/ ...), or None: the chosen one, Windows' registry / Steam
    libraries, else the Steam libraries' BeamNG.drive (the Linux build or the Windows one under Proton)."""
    import install
    own = apppaths.saved_game_dir()
    if own:
        return own
    if os.name == "nt":
        try:
            return windows_game_dir()
        except Exception:
            return None
    for lib in install._steam_roots():
        d = os.path.join(lib, "steamapps", "common", "BeamNG.drive")
        if os.path.isdir(os.path.join(d, "shaders")):
            return d
    return None


def linux_game_exe():
    import install
    own = apppaths.saved_game_dir()         # (the folder the player chose: --game-dir, the start's question)
    if own:
        return os.path.join(own, GAME_EXE_LINUX)
    for lib in install._steam_roots():
        exe = os.path.join(lib, "steamapps", "common", "BeamNG.drive", GAME_EXE_LINUX)
        if os.path.isfile(exe):
            return exe
    return None


def windows_game_main_pids(pids):
    """Exclude only confirmed CEF helpers; inaccessible command lines remain game candidates."""
    if not pids:
        return pids
    import json
    import re
    command = ("Get-CimInstance -ClassName Win32_Process -Filter \"Name='BeamNG.drive.x64.exe'\" "
               "-Property ProcessId,CommandLine -ErrorAction Stop | "
               "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, timeout=PROCESS_QUERY_S,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0:
            return pids
        data = json.loads((result.stdout or b"").decode("utf-8", errors="replace"))
        rows = data if isinstance(data, list) else [data]
        helpers = {row["ProcessId"] for row in rows if isinstance(row, dict)
                   and isinstance(row.get("CommandLine"), str)
                   and re.search(r'(?:^|\s|[\"])--type(?:=|\s)', row["CommandLine"])}
        return [pid for pid in pids if pid not in helpers]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return pids


def game_pids():
    """The game's main processes (not its CEF helpers); None when Windows could not be asked (unknown)."""
    if IS_WIN:
        # bytes, decoded leniently: on a non-English Windows tasklist's "no tasks" message is in the console's code
        # page (German "ü" = 0x81 in cp850), which text=True's cp1252 could not decode (a player's report,
        # 2026-10-17: the launcher stopped with UnicodeDecodeError and r.stdout None)
        # (None, not []: a query that failed or ran out of time does not say the game is gone. A player's launcher,
        # 1.8.1 on a French Windows 11, i5-8600K: it closed and stopped the world server a few minutes in with the
        # game still running, the next start the same; one slow tasklist under load was "no game" for the 3 s grace)
        try:
            r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE_WIN}", "/FO", "CSV", "/NH"],
                               capture_output=True, timeout=GAME_QUERY_S)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        text = (r.stdout or b"").decode("utf-8", errors="replace")
        out = []
        for line in text.splitlines():
            if line.startswith(f'"{GAME_EXE_WIN}"'):
                try:
                    out.append(int(line.split('","')[1]))
                except (IndexError, ValueError):
                    pass
        return windows_game_main_pids(out)
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        exe = args[0].decode(errors="replace") if args else ""
        # by the file name: inside Steam's runtime container (pressure-vessel, a Flatpak Steam) the path differs
        name = exe.replace("\\", "/").rsplit("/", 1)[-1]
        names = (os.path.basename(GAME_EXE_LINUX), GAME_EXE_WIN)
        if name not in names:
            # ... and argv[0] may not name the game at all: the executable itself (the world server's cpupin found a
            # Flatpak Steam's game this way while the launcher saw none, a player's video 2026-10-17: the server
            # stopped ~30 s after the game started, and a second start found no game and launched it again)
            try:
                name = os.path.basename(os.readlink(f"/proc/{d}/exe"))
            except OSError:
                continue
        if name in names and not any(b"--type=" in a for a in args):     # (its CEF helpers: one rewritten argv string)
            out.append(int(d))
    return out


def windows_visible_window(pids):
    """Has any of the processes pids a visible top-level window (Windows: EnumWindows; None elsewhere or when it
    could not be asked)? A game process left without one (a crashed game hanging in the background) kept the app
    saying "already running" and never started the game (GitHub #1038)."""
    if not IS_WIN or not pids:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        want, found = set(int(p) for p in pids), []
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def each(hwnd, _lp):
            if user32.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in want:
                    found.append(pid.value)
                    return False
            return True
        user32.EnumWindows(proto(each), 0)
        return bool(found)
    except Exception:
        return None


STUCK_GAME_S = 15.0     # a game "already running" without a window this long after the check: stuck (windowless_game)


def windowless_game(pids_fn=None, window_fn=None, wait_s=STUCK_GAME_S, sleep=time.sleep):
    """True when the game seen running has no visible window now and still none wait_s later (a game process left
    hanging in the background); False / None (it could not be told) otherwise."""
    pids_fn, window_fn = pids_fn or game_pids, window_fn or windows_visible_window
    pids = pids_fn()
    if not pids or window_fn(pids) is not False:
        return False if pids else None
    sleep(wait_s)
    pids = pids_fn()
    return bool(pids) and window_fn(pids) is False


def game_connected():
    """The game's mod is connected to the world server (Linux: an established TCP connection to SERVER_PORT in
    /proc/net/tcp). The game is running however its process looks inside a sandbox."""
    if IS_WIN:
        return False
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                rows = f.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            cols = row.split()
            if len(cols) > 3 and cols[3] == "01" and int(cols[1].rsplit(":", 1)[1], 16) == SERVER_PORT:
                return True
    return False


def game_alive():
    """True / False, or None when it could not be told (a process query that failed: not "gone")."""
    pids = game_pids()
    if pids is None:
        return True if game_connected() else None
    return bool(pids) or game_connected()


GAME_CLOSING_S = 8.0            # game_still_running: a game seen at the start is watched this long for closing


def game_still_running(alive=None, wait_s=GAME_CLOSING_S, step_s=1.5, sleep=time.sleep):
    """A game running when the launcher starts, still running a moment later: one that was closing (the app
    started again right after the game was closed) is not "already running" (the launcher then waited for a game
    that never came, telling the player to start it from Steam; seen 2026-11-05)."""
    alive = alive or game_alive
    if not alive():
        return False
    waited = 0.0
    while waited < wait_s:
        sleep(step_s)
        waited += step_s
        if not alive():
            return False
    return True


def steam_running():
    import install
    pidfiles = []
    for home in install.homes():        # (a container's own home inside the real one: GitHub #709)
        pidfiles += [os.path.join(home, ".steam", "steam.pid"),
                     os.path.join(home, ".var", "app", "com.valvesoftware.Steam", ".steam", "steam.pid")]
    for pidfile in pidfiles:
        try:
            with open(pidfile) as f:
                pid = int(f.read().strip())
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() == "steam":
                    return True
        except (OSError, ValueError):
            pass
    # (a Flatpak Steam runs in its own PID namespace: its steam.pid names a pid the host cannot see — the user's
    # report, 2026-10-26: the app then started the game through Steam, where the GPU choice and the grass patch do
    # not reach the game — so a running client is also recognised by its process)
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/comm") as f:
                if f.read().strip() == "steam":
                    return True
        except OSError:
            continue
    return False


def steam_runtime(exe):
    """The Steam Linux Runtime ("sniper") container's entry point in the game's Steam library, or None. Steam runs the
    native game inside it; started on the host instead, the game's UI (CEF) failed on SteamOS: "failed to initialize
    application, shutting down" right after its window opened (a Steam Deck, 2026-10-20). LD_PRELOAD reaches the
    game through it (pressure-vessel carries preloaded libraries into the container)."""
    common = os.path.dirname(os.path.dirname(os.path.dirname(exe)))       # .../steamapps/common
    entry = os.path.join(common, "SteamLinuxRuntime_sniper", "_v2-entry-point")
    return entry if os.path.isfile(entry) and os.access(entry, os.X_OK) else None


def under_gamescope():
    """Linux: running in a Steam Deck's Game Mode (gamescope's session)."""
    return bool(os.environ.get("GAMESCOPE_WAYLAND_DISPLAY")) or os.environ.get("XDG_CURRENT_DESKTOP") == "gamescope"


def steam_overlay_lib():
    """Steam's 64-bit overlay library (gameoverlayrenderer.so), or None. In Game Mode gamescope shows only the windows
    it tags as the running Steam game, which this library does: the game started directly without it ran, but the
    screen kept Steam's loading spinner (a Steam Deck, 2026-10-20)."""
    import install
    for root in install._steam_roots():
        lib = os.path.join(root, "ubuntu12_64", "gameoverlayrenderer.so")
        if os.path.isfile(lib):
            return lib
    return None


def sandboxed_game(exe):
    """The game is installed by a Flatpak / snap Steam: it only runs inside that sandbox (through Steam)."""
    home = os.path.expanduser("~")
    return any(os.path.realpath(exe).startswith(os.path.realpath(os.path.join(home, *p)) + os.sep)
               for p in ((".var", "app"), ("snap",)))


def gcpatch_lib():
    """A copy of the gcpatch library outside the bundle (an AppImage's files go when it closes), or None."""
    for src in (os.path.join(ROOT, "gcpatch", GCPATCH_NAME),
                os.path.join(ROOT, "tools", "gcpatch", "build", GCPATCH_NAME)):
        if os.path.isfile(src):
            dst = os.path.join(data_dir(), "gcpatch", GCPATCH_NAME)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.isfile(dst) or open(dst, "rb").read() != open(src, "rb").read():
                shutil.copyfile(src, dst + ".tmp")
                os.replace(dst + ".tmp", dst)
            return dst
    return None


def clean_env():
    """The environment for the game: without the frozen app's library path (PyInstaller keeps the original)."""
    env = dict(os.environ)
    for k in ("LD_LIBRARY_PATH",):
        orig = env.pop(k + "_ORIG", None)
        if FROZEN:
            if orig is not None:
                env[k] = orig
            else:
                env.pop(k, None)
    for k in ("APPDIR", "APPIMAGE", "ARGV0", "OWD"):      # AppImage runtime variables
        env.pop(k, None)
    return env


def steam_launch(game_args=()):
    """Through Steam, with game_args for the game (the Windows build's launcher skips its renderer dialog when
    given -gfx)."""
    if IS_WIN:
        os.startfile(f"steam://run/{APP_ID}//{' '.join(game_args)}/")
    else:
        steam = shutil.which("steam")
        cmd = [steam, "-applaunch", APP_ID, *game_args] if steam else \
            ["xdg-open", f"steam://run/{APP_ID}//{' '.join(game_args)}/"]
        if any(os.environ.get(k) for k in STEER_VARS) and steam_running():
            say("(a Steam client is already running: it starts the game in its own environment, where the GPU "
                "variables this app set do not reach it — quit Steam and start again for the direct start, or set "
                "the variables on Steam itself)")
        subprocess.Popen(cmd, env=clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


# --- the hand-off to Steam (Windows): watched, retried, a direct start as the last resort -------------------------

STEAM_RETRY_S = 12.0        # no game and no word from Steam about it this long after steam://rungameid: asked again
STEAM_DIRECT_S = 30.0       # ... and still nothing this long after: the game started directly (BeamNG.drive.exe)
STEAM_COLD_S = 90.0         # added to both when Steam itself was not running yet (its own start: ~25 s on a test PC)
STEAM_SETTLE_S = 3.0        # Steam asking the player something must still ask after this long to be said
STEAM_SLOW_S = 6.0          # Steam's steps for the game are shown when the game has not come after this long
STEAM_WATCH_S = 900.0       # how long Steam's later steps are still echoed (a game update before the start)
STEAM_GAME_LINE = re.compile(r"GameAction \[AppID %s, ActionID \d+\] : (.*)" % APP_ID)


def windows_steam():
    """(steam.exe, its console log) on Windows from the registry (HKCU\\Software\\Valve\\Steam), each None if unknown."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            def get(name):
                try:
                    return winreg.QueryValueEx(k, name)[0]
                except OSError:
                    return None
            exe, root = get("SteamExe"), get("SteamPath")
    except (OSError, ImportError):
        return None, None
    exe = os.path.normpath(exe) if exe and os.path.isfile(exe) else None
    log = os.path.normpath(os.path.join(root, "logs", "console_log.txt")) if root else None
    return exe, log


def windows_steam_running():
    """Is Steam's client (steam.exe) running? None when Windows could not be asked."""
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe", "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=PROCESS_QUERY_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return '"steam.exe"' in (r.stdout or b"").decode("utf-8", errors="replace").lower()


class SteamLog:
    """Steam's console log (logs/console_log.txt) read from where it ended at the hand-off: what Steam does with the
    game. read() -> its new complete lines ([] when it cannot be read)."""

    def __init__(self, path):
        self.path = path
        try:
            self.pos = os.path.getsize(path) if path else None
        except OSError:
            self.pos = None

    def read(self):
        if self.path is None:
            return []
        try:
            size = os.path.getsize(self.path)
            if self.pos is None or size < self.pos:     # (written anew: Steam restarted)
                self.pos = 0
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return []
        cut = data.rfind(b"\n") + 1
        self.pos += cut
        return data[:cut].decode("utf-8", errors="replace").splitlines()


def steam_news(lines):
    """Steam's console log lines -> [(kind, text)] about this game: ("asked", url) Steam got our link (or -applaunch),
    ("step", what) a step of its start ("LaunchApp changed task to SynchronizingCloud with ..."), ("started", "") the
    game's process created."""
    out = []
    for line in lines:
        if APP_ID not in line:
            continue
        if "ExecuteSteamURL" in line or "ExecCommandLine" in line:
            out.append(("asked", line.split("]", 1)[-1].strip()))
        elif "Game process added" in line:
            out.append(("started", ""))
        else:
            m = STEAM_GAME_LINE.search(line)
            if m:
                out.append(("step", m.group(1).strip()))
    return out


def steam_step_text(step):
    """A step of Steam's start of the game in plain words, or None for the ordinary quick ones."""
    m = re.match(r"LaunchApp waiting for user response to (\w+)", step)
    if m:
        return f"waiting for you in Steam's window ({m.group(1)}): answer it there and the game starts"
    m = re.match(r"LaunchApp changed task to (\w+)", step)
    if m:
        task = m.group(1)
        if task in ("Completed", "CreatingProcess", "WaitingGameWindow"):
            return None
        return {"SynchronizingCloud": "synchronizing your Steam Cloud saves",
                "UpdatingApp": "updating BeamNG.drive first (Steam's Downloads page shows how far)",
                "WaitingForUpdate": "updating BeamNG.drive first (Steam's Downloads page shows how far)",
                }.get(task, task)
    if re.search(r"fail|error|cancel", step, re.I):
        return step
    return None


def windows_direct_start(win_args=(), options_line=None):
    """The game's own launcher (BeamNG.drive.exe) started directly, as BeamMP's launcher does, with the options of
    Steam's launch options line (a player's -userpath among them: the same user folder as through Steam). A Popen, or
    None when the game's folder is not known or it could not start."""
    d = windows_game_dir()
    if not d:
        return None
    line = steam_launch_options() if options_line is None else options_line
    opts = split_launch_options(line)[2] if line else []
    try:
        return subprocess.Popen([os.path.join(d, "BeamNG.drive.exe")] + list(win_args) + opts, cwd=d)
    except OSError as e:
        say(f"  could not start {os.path.join(d, 'BeamNG.drive.exe')} ({e})")
        return None


def steam_handoff(ask, retry, direct, alive, steam_log, steam_up=None, clock=time.monotonic, sleep=time.sleep,
                  report=None, meanwhile=None):
    """The game started through Steam (ask: steam://rungameid), watched until its process appears: Steam's console log
    (steam_log: SteamLog) tells what Steam does with it. Steam silent about the game (the link lost: Steam busy or
    still starting, a game it thinks still runs) and no game after STEAM_RETRY_S: asked again (retry: steam.exe
    -applaunch); after STEAM_DIRECT_S: the game started directly (direct() -> a Popen or None). Steam working on it (a
    cloud sync, an update, a question in its window) is never overtaken: what it does is said and the game waited for.
    A player's dev 401 start stopped at "starting through Steam" with nothing following, four times (Discord
    2026-11-12). Returns (how, proc): "started" (the game or its launcher runs), "steam" (Steam is on it), "direct"
    (proc: the direct start), "none" (nothing worked: the player is told what to do)."""
    report = report or (lambda text: row("Steam", text))
    up = steam_up() if steam_up else True
    extra = STEAM_COLD_S if up is False else 0.0
    if up is False:
        say("  (Steam is not running: it starts first, then the game)")
    t0 = clock()
    ask()
    if meanwhile:
        meanwhile()
    retried, busy, said, pending = False, None, set(), {}
    while True:
        now = clock() - t0
        if alive():
            return "started", None
        for kind, text in steam_news(steam_log.read()):
            if kind == "started":
                return "started", None
            if kind == "step":
                busy = now if busy is None else busy
                m = re.match(r"LaunchApp (?:waiting for user response to|continues with user response) \"?(\w+)", text)
                if m and text.startswith("LaunchApp waiting"):
                    pending[m.group(1)] = now
                elif m:
                    pending.pop(m.group(1), None)
                else:
                    msg = steam_step_text(text)
                    if msg and msg not in said and now >= STEAM_SLOW_S:
                        said.add(msg)
                        report(msg)
        for what, since in list(pending.items()):
            if now - since >= STEAM_SETTLE_S:
                msg = steam_step_text(f"LaunchApp waiting for user response to {what}")
                pending.pop(what)
                if msg not in said:
                    said.add(msg)
                    report(msg)
        if busy is not None and now - busy >= STEAM_SETTLE_S + 1:
            return "steam", None
        if busy is None and not retried and now >= STEAM_RETRY_S + extra:
            retried = True
            report("no word from Steam about the game yet: asking it again")
            retry()
        if busy is None and now >= STEAM_DIRECT_S + extra:
            report(f"Steam did not start the game within {STEAM_DIRECT_S + extra:.0f} s: starting BeamNG.drive "
                   "directly (without Steam Input and Steam's overlay)")
            proc = direct()
            return ("direct", proc) if proc is not None else ("none", None)
        sleep(0.5)


def steam_echo(steam_log, alive, until_s=STEAM_WATCH_S, clock=time.monotonic, sleep=time.sleep, report=None):
    """In the background after steam_handoff's "steam": Steam's later steps of the start said as they come (a game update
    first, a question in its window), until the game runs or until_s."""
    report = report or (lambda text: row("Steam", text))

    def run():
        t0, said = clock(), set()
        while clock() - t0 < until_s:
            if alive():
                return
            for kind, text in steam_news(steam_log.read()):
                if kind == "started":
                    return
                msg = steam_step_text(text) if kind == "step" else None
                if msg and msg not in said and not text.startswith("LaunchApp waiting"):
                    said.add(msg)
                    report(msg)
            sleep(2)

    threading.Thread(target=run, name="steam-echo", daemon=True).start()


def windows_steam_start(win_args=(), meanwhile=None):
    """Windows: the game through Steam (steam://rungameid), watched (steam_handoff). Returns a Popen when it had to be
    started directly after all, else None."""
    row("Game", "starting through Steam")
    steam_exe, log_path = windows_steam()
    steam_log = SteamLog(log_path)

    def ask():
        os.startfile(f"steam://rungameid/{APP_ID}")

    def retry():
        try:
            if steam_exe:
                subprocess.Popen([steam_exe, "-applaunch", APP_ID], close_fds=True)
            else:
                os.startfile(f"steam://rungameid/{APP_ID}")
        except OSError as e:
            say(f"  ({e})")

    def alive():
        return bool(game_pids()) or game_launcher_running()

    how, proc = steam_handoff(ask, retry, lambda: windows_direct_start(win_args), alive, steam_log,
                              steam_up=windows_steam_running, meanwhile=meanwhile)
    if how == "steam":
        steam_echo(steam_log, alive)
    elif how == "direct":
        row("Game", "started directly")
    elif how == "none":
        problem("the game did not start: Steam did not start it, and BeamNG.drive's folder was not found to start it "
                "directly.", "Press Play on BeamNG.drive in Steam: the world server keeps running for it.")
    return proc


def _ini(path):
    """key = value lines of an ini file (BeamNG's own, with a BOM)."""
    out = {}
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return out
    # UTF-8, else the Windows ANSI code page (a user folder named in it, "C:\Users\Mikołaj\..." in cp1250, read with
    # errors="replace" named a folder that did not exist: GitHub #770)
    for enc in ("utf-8-sig", "mbcs", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith((";", "#", "[")):
            k, v = line.split("=", 1)
            out[k.strip()] = os.path.expandvars(v.strip())
    return out


def windows_beamng_ini():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return _ini(os.path.join(base, "BeamNG", "BeamNG.drive.ini"))


def windows_game_dir():
    """The game's folder on Windows (also inside a Proton prefix), as BeamMP's launcher finds it: installPath in
    %LOCALAPPDATA%\\BeamNG\\BeamNG.drive.ini, else the game's registry key, else Steam's libraries."""
    ok = lambda d: d and os.path.isfile(os.path.join(d, "BeamNG.drive.exe"))
    own = apppaths.saved_game_dir()         # (the folder the player chose: --game-dir, the start's question)
    if own:
        return own
    d = windows_beamng_ini().get("installPath")
    if ok(d):
        return d
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\BeamNG\BeamNG.drive") as k:
            i = 0
            while True:
                try:
                    _, v, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                if isinstance(v, str) and ok(v):
                    return v
                i += 1
    except OSError:
        pass
    try:
        import winreg
        import re
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = winreg.QueryValueEx(k, "SteamPath")[0]
        libs = [steam]
        with open(os.path.join(steam, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="replace") as f:
            libs += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
        for lib in libs:
            d = os.path.join(lib, "steamapps", "common", "BeamNG.drive")
            if ok(d):
                return d
    except OSError:
        pass
    return None


# --gfx -> the game's -gfx value. The game knows "dx11" and "vk" but not "dx12" (2026-10-24, a Windows 10 / RTX 3060
# test PC: "-gfx dx12" listed no adapter at all, "There is no valid rendering device available."): DirectX 12 is
# "d3d12" (its log then: "NVIDIA GeForce RTX 3060 (D3D12)").
GFX_ARG = {"dx11": "dx11", "dx12": "d3d12", "vk": "vk"}


# --- the GPUs (Linux: a hybrid laptop's game on the discrete one, the launch options adopted) ------

STEER_VARS = ("DRI_PRIME", "__NV_PRIME_RENDER_OFFLOAD", "__GLX_VENDOR_LIBRARY_NAME", "MESA_VK_DEVICE_SELECT",
              "VK_DRIVER_FILES", "VK_ICD_FILENAMES")     # one set: the player (or their launch options) steer
GPU_VENDORS = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}


def gpu_scan():
    """Every PCI display controller, in DRM card order: (vendor, boot, vram, driver) — vendor from the PCI ID
    ("nvidia"/"amd"/"intel"/"other"), boot: the firmware starts it (the GPU apps get by default), vram: the bytes
    the driver reports (0: it does not say; NVIDIA's proprietary one never does), driver: the bound kernel
    module's name ("" none)."""
    import glob
    out = []
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        d = os.path.join(card, "device")
        try:
            with open(os.path.join(d, "class")) as f:
                if not f.read().strip().startswith("0x03"):    # a display controller (VGA, or NVIDIA's 3D one)
                    continue
            with open(os.path.join(d, "vendor")) as f:
                vendor = GPU_VENDORS.get(f.read().strip().lower(), "other")
        except OSError:
            continue                    # a non-PCI card (simpledrm, a VM's): nothing to read
        boot, vram, driver = False, 0, ""
        try:
            with open(os.path.join(d, "boot_vga")) as f:
                boot = f.read().strip() == "1"
        except OSError:
            pass
        for n in ("mem_info_vram_size", "mem_info_vram_total"):        # amdgpu's name, then nouveau's
            try:
                with open(os.path.join(d, n)) as f:
                    vram = int(f.read())
                    break
            except (OSError, ValueError):
                pass
        try:
            driver = os.path.basename(os.path.realpath(os.path.join(d, "driver")))
        except OSError:
            pass
        out.append((vendor, boot, vram, driver))
    return out


def _gb(n):
    """VRAM bytes as text (0: "unknown")."""
    return f"{n / (1 << 30):.1f} GB" if n else "unknown"


def gpu_plan(mode, environ=None, scan=None):
    """(env, note) that put the game (and the server's GPU work) on the discrete GPU of a hybrid-graphics machine,
    (None, note) when there is nothing to do (the note says why, for the log). mode: "auto" steers only when the
    default GPU looks weaker than another (a laptop's integrated one — the user's report, 2026-10-26: the game
    started directly rendered on the integrated GPU where Steam's start used the dedicated one), "discrete" does it
    whenever a discrete one can be named, "off" never. A steering variable already set (the player's, or Steam's
    launch options') wins: nothing is overridden.

    The variables: NVIDIA's driver wants __NV_PRIME_RENDER_OFFLOAD (the Vulkan loader then offers the NVIDIA device
    first) and the GL one __GLX_VENDOR_LIBRARY_NAME; Mesa's follow DRI_PRIME=1 (render offload: the GPU that is not
    the default one) through its device-select layer, which the server's wgpu compute sees too. Which of two Mesa
    GPUs is the discrete one comes from the VRAM they report; without that (and without the NVIDIA driver) auto
    gives up and discrete guesses DRI_PRIME=1."""
    environ = os.environ if environ is None else environ
    for k in STEER_VARS:
        if environ.get(k):
            return None, f"{k} is set already: the player steers, nothing done"
    if mode == "off":
        return None, ""
    gpus = scan if scan is not None else gpu_scan()
    if len(gpus) < 2:
        return None, ("one GPU: nothing to steer" if gpus else "no GPU seen: nothing to steer")
    boot = next((g for g in gpus if g[1]), gpus[0])
    nv = [g for g in gpus if g[0] == "nvidia" and g[3] == "nvidia"]
    if nv:
        if boot not in nv or mode == "discrete":
            return ({"__NV_PRIME_RENDER_OFFLOAD": "1", "__GLX_VENDOR_LIBRARY_NAME": "nvidia"},
                    "NVIDIA PRIME render offload: the game renders on the NVIDIA GPU" +
                    (" (--gpu discrete; the default GPU is the NVIDIA one already)" if boot in nv else ""))
        return None, "the default GPU is the NVIDIA one already: nothing to steer"
    big = max(gpus, key=lambda g: g[2])
    if big[2] <= boot[2]:
        if mode == "discrete" and not big[2] and not boot[2]:
            return {"DRI_PRIME": "1"}, "DRI_PRIME=1 (--gpu discrete; the drivers do not say which GPU has more VRAM)"
        return None, ("the default GPU has the most VRAM: nothing to steer" if big[2] else
                      "cannot tell which GPU has more VRAM: nothing steered (set DRI_PRIME=1, or --gpu discrete)")
    return {"DRI_PRIME": "1"}, (f"DRI_PRIME=1: the game renders on the {big[0]} GPU ({_gb(big[2])}; the default "
                                f"{boot[0]} one has {_gb(boot[2])})")


def _vdf(text):
    """Steam's VDF format as a dict tree ("key" "value" pairs in { } braces; // comments; a duplicate key keeps
    the last)."""
    import re
    text = re.sub(r"(^|\s)//[^\n]*", r"\1", text)
    out, stack, key = {}, [], None
    cur = out
    for quoted, brace in re.findall(r'"((?:\\.|[^"\\])*)"|([{}])', text):
        if brace == "{":
            stack.append((cur, key))
            cur, key = {}, None
        elif brace == "}":
            done, (cur, key) = cur, stack.pop()
            cur[key] = done
            key = None
        elif key is None:
            key = re.sub(r"\\(.)", r"\1", quoted)
        else:
            cur[key] = re.sub(r"\\(.)", r"\1", quoted)
            key = None
    return out


def _vdf_apps_options(tree, appid):
    """LaunchOptions of appid in a parsed VDF tree: the value under the appid key's dict, wherever it nests
    (config.vdf's InstallConfigStore/Software/Valve/Steam/apps, registry.vdf's HKCU/.../apps)."""
    found = []

    def walk(node):
        if not isinstance(node, dict):
            return
        for k, v in node.items():
            if k == appid and isinstance(v, dict) and "LaunchOptions" in v:
                found.append(str(v["LaunchOptions"] or ""))
            walk(v)

    walk(tree)
    return found[0] if found else ""


def steam_launch_options(roots=None):
    """The game's launch options line from Steam's config: config.vdf's apps (this client's per-game settings) and
    registry.vdf's apps (an older client's, this machine's ~/.steam one too), "" for none, None when there is no
    Steam config here. Steam writes the file when it saves the setting (a start through Steam applies the options
    itself, so only the direct start adopts them; the Windows game always starts through Steam)."""
    import install
    seen = False
    for root in roots if roots is not None else install._steam_roots():
        for name in (os.path.join("config", "config.vdf"), "registry.vdf"):
            try:
                with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                    tree = _vdf(f.read())
            except OSError:
                continue
            seen = True
            line = _vdf_apps_options(tree, APP_ID)
            if line:
                return line
    return "" if seen else None


def steam_user_path(line):
    """The version folder of a -userpath in Steam's launch options line (quoted or not), or None: -userpath names the
    folder the game puts its version folder in ("current"; a folder without one as it is)."""
    import re
    m = re.search(r'(?:^|\s)-userpath\s+(?:"([^"]+)"|(\S+))', line or "")
    if not m:
        return None
    p = apppaths.clean(m.group(1) or m.group(2))
    if not p or not os.path.isdir(p):
        return None
    cur = os.path.join(p, "current")
    return cur if os.path.isdir(cur) else p


def split_launch_options(line):
    """(env, wrapper, options) of Steam's launch options line: NAME=VALUE tokens before %command% are variables for
    the game's environment, other tokens before it are a command that wraps it (gamemoderun, mangohud), the tokens
    after it are the game's own options. Without %command% the whole line is the game's options (Steam needs
    %command% to export variables too, so none was meant)."""
    import re
    pre, have_cmd, post = line.partition("%command%")
    if not have_cmd:
        return {}, [], pre.split()
    env, wrapper = {}, []
    for t in pre.split():
        name, eq, value = t.partition("=")
        if eq and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            env[name] = value
        else:
            wrapper.append(t)
    return env, wrapper, post.split()


def start_game(use_gcpatch, build="native", gfx="", user_dir=None, steam_args=({}, [], []), meanwhile=None):
    """Start the game; returns a Popen when this process started it directly, else None (through Steam).

    meanwhile: called once the game has been asked to start, before the start is watched (a direct Linux start's
    DIRECT_FAIL_S, the Windows hand-off to Steam): run follows the world server's start there.

    steam_args: (env, wrapper, options) adopted from Steam's launch options for the direct start (through Steam,
    Steam applies them itself): variables for the game's environment, a command that wraps it (gamemoderun,
    mangohud), the game's own options after %command%."""
    steam_env, wrapper, steam_opts = steam_args
    # (dev: BNGE_GAME_WRAPPER="perf record ... --" profiles the game alone, Linux direct start)
    wrapper = os.environ.get("BNGE_GAME_WRAPPER", "").split() + list(wrapper or [])
    win_args = ["-gfx", GFX_ARG.get(gfx, gfx)] if gfx else []
    extra = os.environ.get("BNGE_GAME_ARGS", "").split()     # (tests: e.g. "-bngetest car,damage")
    win_args += extra
    # a game folder the player chose outside Steam's libraries (another launcher, a copy of its own): started directly
    own = apppaths.saved_game_dir()
    own = own if own and not apppaths.in_steam_library(own) else None
    if IS_WIN and not win_args and not under_wine() and not own:
        # through Steam on Windows itself: Steam Input (a Steam Controller), the overlay and Steam's play time reach
        # only a game Steam started (GitHub #218, 1.6.1 on Windows: "Steam doesn't pass input from the Steam
        # Controller into the game"). Not with start options (--gfx, tests): Steam asks the player whether to allow
        # them; not under Wine / Proton, where Steam's links do not reach Steam. Watched: a link Steam lost is asked
        # again, then the game started directly (windows_steam_start)
        return windows_steam_start(meanwhile=meanwhile)
    if IS_WIN:
        # as BeamMP's launcher: the game's own launcher (BeamNG.drive.exe) in its folder, waited on. Works under
        # Wine / Proton too (the .exe started with protontricks), where Steam's links do not reach Steam
        d = windows_game_dir()
        if d:
            row("Game", f"starting ({d})")
            try:
                return subprocess.Popen([os.path.join(d, "BeamNG.drive.exe")] + win_args, cwd=d)
            except OSError as e:
                say(f"could not start it ({e}); asking Steam")
    elif build == "proton":
        row("Game", "starting through Steam (the Windows build, Proton)")
        steam_launch(win_args)
        return None
    if not IS_WIN:
        exe = linux_game_exe()
        if exe and sandboxed_game(exe):
            row("Game", "starting through Steam (a Flatpak / snap Steam: without the grass patch)")
            exe = None
        if exe and (steam_running() or own):
            env = clean_env()
            env["SteamAppId"] = APP_ID
            # (Game Mode: started from a Steam shortcut, the shortcut's id is the game gamescope shows)
            env["SteamGameId"] = os.environ.get("SteamGameId") if under_gamescope() and os.environ.get("SteamGameId") \
                else APP_ID
            overlay = steam_overlay_lib() if under_gamescope() else None
            if overlay and overlay not in env.get("LD_PRELOAD", ""):
                env["LD_PRELOAD"] = overlay + (":" + env["LD_PRELOAD"] if env.get("LD_PRELOAD") else "")
            lib = gcpatch_lib() if use_gcpatch else None
            if lib:
                env["LD_PRELOAD"] = lib + (":" + env["LD_PRELOAD"] if env.get("LD_PRELOAD") else "")
                if user_dir:        # where the mod raises the patch's level flag (tools/gcpatch)
                    env["BNGEARTH_USER_DIR"] = user_dir
            runtime = steam_runtime(exe)
            env.update(steam_env)       # the launch options' variables (after the patch's: the player's win)
            row("Game", "starting" + ("  (with the grass patch)" if lib else ""))
            if wrapper or steam_opts:
                row("Steam options", " ".join(wrapper + ["%command%"] + steam_opts))
            # -userpath is the folder the game puts its version folder in ("current"): the launcher's user folder is
            # that version folder, so the game gets its parent (a Flatpak Steam's folder, the user's report
            # 2026-10-26 — without it the game made a second "current" in it, without the mod). A player's own
            # folder without the version naming goes as it is.
            base = os.path.basename(user_dir or "")
            game_user = os.path.dirname(user_dir) if user_dir and (base == "current" or
                                                                   base.replace(".", "").isdigit()) else user_dir
            proc = subprocess.Popen(([*wrapper] if wrapper else []) +
                                    ([runtime, "--verb=waitforexitandrun", "--"] if runtime else []) +
                                    [exe] + (["-userpath", game_user] if game_user else []) + extra +
                                    list(steam_opts),
                                    cwd=os.path.dirname(os.path.dirname(exe)), env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            if meanwhile:
                meanwhile()
            t0 = time.time()
            while time.time() - t0 < DIRECT_FAIL_S:
                if proc.poll() is not None:
                    break
                time.sleep(1)
            if proc.poll() is None:
                return proc
            say(f"the direct start ended (code {proc.returncode}); starting the game through Steam instead")
        elif exe:
            row("Game", "Steam is not running: starting through Steam (without the grass patch)")
    row("Game", "starting through Steam")
    steam_launch(win_args if IS_WIN else ())
    return None


# A game update turns every mod off (core_modmanager.getModsDisabledAfterUpdate: "Mods will be disabled after updating
# from version ...") until the player turns them back on in the main menu; without that BeamNG.Earth is just not there
# (BeamNG 0.40, 2026-10-19). The game's log is read while it starts and the player told what to do.
MODS_OFF_LINES = ("Mods will be disabled after updating",       # (the first start after the update)
                  "Mods disabled by setting disableModsAfterUpdate")  # (each start while the setting is on)
GAME_LOG_WATCH_S = 600       # s after the game's start the log is watched


def clear_level_flag(user_dir):
    """The engine patches' level flag (temp/beamngearth_gcpatch_on, groundcover.lua) removed before a fresh game start:
    a game that crashed or was killed on the BeamNG.Earth map leaves it behind, and the patches would then run in the
    menus and on other maps until the mod next loads and unloads our level."""
    if not user_dir:
        return
    try:
        os.remove(os.path.join(user_dir, "temp", "beamngearth_gcpatch_on"))
    except OSError:
        pass


def watch_game_log(user_dir, since):
    """In the background: the game's log (written anew at each start) read for the mods-off line."""
    path = os.path.join(user_dir, "beamng.log")

    def run():
        t0, pos = time.time(), 0
        while time.time() - t0 < GAME_LOG_WATCH_S:
            time.sleep(2)
            try:
                if os.path.getmtime(path) < since:
                    continue                    # the last run's log: the game has not started writing yet
                with open(path, "rb") as f:
                    if os.path.getsize(path) < pos:
                        pos = 0
                    f.seek(pos)
                    data = f.read()
                    pos += len(data)
            except OSError:
                continue
            if any(line.encode() in data for line in MODS_OFF_LINES):
                problem("BeamNG.drive was updated and turned all mods off, BeamNG.Earth too.",
                        "In the game's main menu, choose to enable your mods again (or Mods > Manage, turn "
                        "BeamNG.Earth on), then pick Freeroam > BeamNG.Earth. The world server keeps running; "
                        "from the next start BeamNG.Earth turns them back on itself.")
                return

    threading.Thread(target=run, name="watch-game-log", daemon=True).start()


def game_launcher_running():
    """Windows: BeamNG's own launcher (BeamNG.drive.exe, its renderer / launch options window) is open: the game is
    starting, however long the player takes there."""
    if not IS_WIN:
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq BeamNG.drive.exe", "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=PROCESS_QUERY_S)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return '"BeamNG.drive.exe"' in (r.stdout or b"").decode("utf-8", errors="replace")


def wait_for_game(proc, server=None):
    """Follow the actual game, with bounded failed startup and cleanup when our server exits."""
    deadline = time.monotonic() + GAME_START_S
    grace = WINDOWS_GAME_GONE_S if IS_WIN else GAME_GONE_S
    started, dialog_seen, said = False, False, False
    gone_since = dialog_closed_since = first_seen = None
    while True:
        if server is not None and server.poll() is not None:
            say("The world server stopped. See its log for details; restart BeamNG.Earth to reconnect.")
            return False
        now = time.monotonic()
        direct_alive = proc is not None and proc.poll() is None
        alive = game_alive()
        if alive is None:               # (could not be told: neither seen nor gone, the clocks keep their state)
            time.sleep(1 if IS_WIN else 3)
            continue
        if alive or (not IS_WIN and direct_alive):
            started = True
            gone_since = None
            if first_seen is None:
                first_seen = now
        elif started:
            # (the long grace only while the game starts: Steam's container restarts it then; a game that ran a
            # while and is gone was closed)
            wait = grace if IS_WIN or now - first_seen < GAME_RESTART_WINDOW_S else min(grace, GAME_CLOSED_S)
            if gone_since is None:
                gone_since = now
            elif now - gone_since >= wait:
                return True
        elif game_launcher_running() or (IS_WIN and direct_alive):
            # Never time out an open BeamNG launch dialog. Once the game has been seen, its bootstrap
            # process no longer keeps this launcher alive after the actual game closes.
            dialog_seen = True
            dialog_closed_since = None
        elif dialog_seen:
            if dialog_closed_since is None:
                dialog_closed_since = now
            elif now - dialog_closed_since >= grace:
                say("The BeamNG launch window closed without starting the game; stopping the world server.")
                return False
        elif now >= deadline and not said:
            # (said once; the server keeps waiting: a Steam start can take minutes, a game update or the first
            # shader build, and on Linux the launch options window is not seen; the 2026-10-24 rule)
            say("the game has not started yet (no BeamNG.drive process): start it from Steam; the server keeps "
                "running for it, Ctrl+C stops it")
            said = True
        time.sleep(1 if IS_WIN else 3)


# --- the console window ---------------------------------------------------------------------------

TERMINALS = [("konsole", ["-e"]), ("kgx", ["--"]), ("gnome-terminal", ["--"]), ("xfce4-terminal", ["-x"]),
             ("mate-terminal", ["-x"]), ("tilix", ["-e"]), ("alacritty", ["-e"]), ("kitty", []), ("foot", []),
             ("wezterm", ["start", "--"]), ("x-terminal-emulator", ["-e"]), ("xterm", ["-e"])]


def open_terminal():
    """Linux, started without a terminal (a file manager): run this again in a terminal window. True when one
    opened (this process then ends)."""
    if os.environ.get("BNGEARTH_TERMINAL") or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    me = [os.environ["APPIMAGE"]] if os.environ.get("APPIMAGE") else \
        [sys.executable] + ([] if FROZEN else [os.path.abspath(__file__)])
    desk = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    order = sorted(TERMINALS, key=lambda t: 0 if (("KDE" in desk and t[0] == "konsole") or
                                                  ("GNOME" in desk and t[0] in ("kgx", "gnome-terminal"))) else 1)
    term = os.environ.get("TERMINAL")
    if term and shutil.which(term):
        order.insert(0, (term, ["-e"]))
    env = clean_env()
    env["BNGEARTH_TERMINAL"] = "1"
    for name, flag in order:
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            subprocess.Popen([exe] + flag + me + sys.argv[1:], env=env, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            continue
    return False


LOG_KEEP_BYTES = 512 * 1024     # launcher.log: the last starts kept (a start appends; trimmed at the next start)


class _Tee:
    """sys.stdout / stderr written to the console and to the log file too (attributes otherwise the console's)."""

    def __init__(self, stream, log):
        self._stream, self._log = stream, log

    def write(self, text):
        try:
            self._log.write(text)
            self._log.flush()
        except (OSError, ValueError):
            pass
        return self._stream.write(text) if self._stream is not None else len(text)

    def flush(self):
        if self._stream is not None:
            self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def tee_log(dd=None, windows=None):
    """Windows: what the launcher prints also goes to <data dir>/logs/launcher.log (appended per start, the last
    LOG_KEEP_BYTES kept), so a window that closed at once still leaves a log to send (a player's .exe opened "for a
    split second then closes", Discord 2026-11-05). Linux without a terminal logs there already (main). The log
    file's path, or None."""
    if not (IS_WIN if windows is None else windows):
        return None
    try:
        path = os.path.join(dd or data_dir(), "logs", "launcher.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > LOG_KEEP_BYTES:
                with open(path, "rb") as f:
                    f.seek(-LOG_KEEP_BYTES // 2, os.SEEK_END)
                    tail = f.read()
                with open(path, "wb") as f:
                    f.write(tail)
        except OSError:
            pass
        log = open(path, "a", encoding="utf-8", errors="replace")
        import datetime
        log.write(f"\n===== {datetime.datetime.now().isoformat(timespec='seconds')} {sys.executable} "
                  f"{' '.join(sys.argv[1:])}\n")
        log.flush()
        sys.stdout, sys.stderr = _Tee(sys.stdout, log), _Tee(sys.stderr, log)
        return path
    except Exception:           # never a reason not to start
        return None


def windows_console():
    """Windows: a console window of our own when there is none (Wine / Proton started us without one)."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        if not k32.GetConsoleWindow():
            k32.FreeConsole()
            if k32.AllocConsole():
                con = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")
                if isinstance(sys.stdout, _Tee):        # (tee_log: the log goes on)
                    sys.stdout._stream = sys.stderr._stream = con
                else:
                    sys.stdout = sys.stderr = con
                sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        k32.SetConsoleTitleW("BeamNG.Earth")
    except (OSError, AttributeError):
        pass


def own_window():
    """Whether this console window closes with us (opened for us): then wait before closing on an error."""
    if os.environ.get("BNGEARTH_TERMINAL"):
        return True
    if IS_WIN:
        try:
            import ctypes
            arr = (ctypes.c_uint * 8)()
            return ctypes.windll.kernel32.GetConsoleProcessList(arr, 8) <= 2
        except (OSError, AttributeError):
            return False
    return False


def pause(msg):
    say(msg)
    try:
        input()
    except (EOFError, OSError):
        time.sleep(30)


# --- main -----------------------------------------------------------------------------------------

def under_wine():
    if not IS_WIN:
        return False
    try:
        import ctypes
        return hasattr(ctypes.WinDLL("ntdll"), "wine_get_version")
    except (OSError, AttributeError):
        return False


def steam_compat_tool():
    """Linux: the compatibility tool Steam is set to use for the game (its name: a Proton, or a Steam Linux Runtime
    container that runs the native build), "" for none, None when Steam's config does not say (it writes the setting
    when it exits)."""
    import install
    import re
    for root in install._steam_roots():
        try:
            with open(os.path.join(root, "config", "config.vdf"), encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        m = re.search(r'"CompatToolMapping"\s*\{(.*?)\n\t{4}\}', text, re.S)
        if not m:
            continue
        e = re.search(r'"%s"\s*\{[^}]*?"name"\s*"([^"]*)"' % APP_ID, m.group(1))
        return e.group(1) if e else ""
    return None


def is_proton(tool):
    """Is the compatibility tool tool (steam_compat_tool) a Proton, the Windows build's? (Steam also offers the Steam
    Linux Runtime containers, which run the native build: steamlinuxruntime_4 was taken for a Proton and a stale
    2024 Proton prefix used, GitHub #56.)"""
    return bool(tool) and "proton" in tool.lower()


def last_written(user_dir):
    """When the game last wrote its log in this user folder (0: never)."""
    import glob
    t = [os.path.getmtime(p) for p in glob.glob(os.path.join(user_dir, "beamng*.log"))]
    return max(t) if t else 0.0


def native_folder():
    """The native game's user folder: of those that exist, the one the game wrote its log in last (a Flatpak
    Steam's game writes inside the Flatpak's home; an earlier launcher may have made ~/.local/share's without the
    game ever using it); none yet: the Flatpak's when Steam is the Flatpak, else ~/.local/share's."""
    import install
    dirs = install.native_user_dirs()
    found = [d for d in dirs if os.path.isdir(d)]
    used = [d for d in found if last_written(d) > 0]
    if used:
        return max(used, key=last_written)
    if install.flatpak_steam() and not os.path.isdir(os.path.join(os.path.expanduser("~"), ".local", "share",
                                                                  "Steam")):
        return dirs[1]
    return found[0] if found else dirs[0]


def linux_build(force=None):
    """("native" | "proton", the BeamNG.drive user folder of that build) on Linux."""
    import install
    native = native_folder()
    natives = {os.path.realpath(d) for d in install.native_user_dirs()}
    dirs = install.candidate_user_dirs()
    proton_dirs = [d for d in dirs if os.path.realpath(d) not in natives]
    mode = force
    if mode is None:
        tool = steam_compat_tool()
        if is_proton(tool):
            mode = "proton"
        elif tool:                  # a Steam Linux Runtime container (steamlinuxruntime_4, _sniper): the native build
            mode = "native"
        elif not proton_dirs:
            mode = "native"
        elif tool is None or not os.path.isdir(native):
            # Steam has not saved the setting: the build that ran last
            mode = "proton" if max(map(last_written, proton_dirs)) > last_written(native) else "native"
        else:
            mode = "native"
    if mode == "proton":
        d = max(proton_dirs, key=last_written) if proton_dirs else None
        if d is None:
            say("no Proton user folder of BeamNG.drive yet: start the game once from Steam, then run this again")
            d = native
        return "proton", d
    os.makedirs(native, exist_ok=True)
    return "native", native


def user_folder():
    import install
    # (first the folder the game itself said it uses, when it ever differed from ours: the world server saves it,
    # apppaths.set_user_dir; GitHub #770: the game's user folder elsewhere, our files written where it never looked)
    told = apppaths.saved_user_dir()
    if told and os.path.isdir(os.path.join(told, "mods")):
        return told
    if IS_WIN:
        uf = windows_beamng_ini().get("userFolder")       # a user folder moved in the game's launcher
        if uf and os.path.isdir(uf):
            return os.path.join(uf, "current") if os.path.isdir(os.path.join(uf, "current")) else uf
    found = install.candidate_user_dirs()
    if found:
        return found[0]
    # the game never ran: its default folder (the game uses it on its first start)
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        d = os.path.join(base, "BeamNG", "BeamNG.drive", "current")
    else:
        d = os.path.join(os.path.expanduser("~"), ".local", "share", "BeamNG", "BeamNG.drive", "current")
    os.makedirs(d, exist_ok=True)
    return d


def hand_over():
    """A packaged copy with a newer one downloaded beside it by the in-game "Update now" (server selfupdate.py,
    GitHub #140): start that one with these arguments instead. True when it was started."""
    if not (FROZEN or os.environ.get("APPIMAGE")):
        return False
    try:
        sys.path.insert(0, os.path.join(ROOT, "server"))
        import install
        import selfupdate
        new = selfupdate.staged(install.version())
    except Exception:                   # the old copy runs as before
        return False
    if not new:
        return False
    env = clean_env()                   # (the new copy without this one's AppImage variables and library path)
    old = selfupdate.packaged_path()
    if old:                             # (the new copy keeps this file: the player's shortcut starts it, GitHub #900)
        env[selfupdate.HANDED_OVER_FROM] = old
    print(f"Starting the updated BeamNG.Earth: {os.path.basename(new)}", flush=True)
    try:
        proc = subprocess.Popen([new] + sys.argv[1:], env=env, cwd=os.path.dirname(new),
                                **({"creationflags": subprocess.CREATE_NEW_CONSOLE} if IS_WIN
                                   else {"start_new_session": True}))
    except OSError as e:
        code = str(e)
    else:
        # (a new copy that dies at once, blocked or removed by an antivirus, or a damaged download, made every start
        # of this one flash a window and close: "the .exe opens for a split second then closes", Discord 2026-11-05)
        try:
            code = proc.wait(timeout=HANDOVER_CHECK_S)
        except subprocess.TimeoutExpired:
            return True
        if code == 0:
            return True
    sac = smart_app_control_on() or "4551" in str(code)
    if sac:
        problem(f"Windows Smart App Control blocked the updated BeamNG.Earth ({os.path.basename(new)}).",
                "It only runs apps from a verified publisher, and BeamNG.Earth is not code-signed yet (it is "
                "planned). This version keeps working, and that version will not be offered again.")
    else:
        problem(f"The updated BeamNG.Earth ({os.path.basename(new)}) closed right after starting ({code}).",
                "Starting this version instead. An antivirus may have blocked it: the in-game Update now (Settings, "
                "About) downloads it again.")
    try:
        selfupdate.drop_staged(blocked_by="smart_app_control" if sac else None)
    except Exception:
        pass
    return False


def smart_app_control_on():
    """Is Windows 11's Smart App Control enforcing (it blocks unsigned apps without a "run anyway"; GitHub #823)?
    HKLM\\SYSTEM\\CurrentControlSet\\Control\\CI\\Policy VerifiedAndReputablePolicyState: 0 off, 1 on, 2 evaluation."""
    if not IS_WIN:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\CI\Policy") as k:
            return int(winreg.QueryValueEx(k, "VerifiedAndReputablePolicyState")[0]) == 1
    except (OSError, ValueError):
        return False


def prune_old_copies(environ=None):
    """The packaged copy that runs (no newer one to hand over to): the old copies beside it that nothing needs are
    deleted (selfupdate.prune_old, GitHub #900), the one a hand-over started this from kept. Never fails the start."""
    environ = os.environ if environ is None else environ
    if not (FROZEN or environ.get("APPIMAGE")):
        return []
    try:
        sys.path.insert(0, os.path.join(ROOT, "server"))
        import install
        import selfupdate
        selfupdate.note_started_from(environ.get(selfupdate.HANDED_OVER_FROM))
        gone = selfupdate.prune_old(install.version())
    except Exception:
        return []
    if gone:
        print(f"Removed {len(gone)} old BeamNG.Earth file(s): {', '.join(gone)}", flush=True)
    return gone


HANDOVER_CHECK_S = 5.0          # hand_over: the new copy must still run this long after its start (else: this one)
UPDATE_TITLE = "BeamNG.Earth update"


def update_popup(text, run=None, which=None, windows=None):
    """A pop-up the player sees before the game starts (yes / no): True, False, or None when none could be shown
    (then the question goes to the console window). Windows: the system's message box, in front of everything;
    Linux: zenity or kdialog when installed."""
    windows = IS_WIN if windows is None else windows
    if windows:
        try:
            import ctypes
            # MB_YESNO | MB_ICONQUESTION | MB_SETFOREGROUND | MB_TOPMOST; IDYES = 6
            return ctypes.windll.user32.MessageBoxW(None, text, UPDATE_TITLE, 0x4 | 0x20 | 0x10000 | 0x40000) == 6
        except Exception:
            return None
    which, run = which or shutil.which, run or subprocess.run
    for cmd in (["zenity", "--question", "--title", UPDATE_TITLE, "--text", text, "--no-wrap"],
                ["kdialog", "--title", UPDATE_TITLE, "--yesno", text]):
        if not which(cmd[0]):
            continue
        try:
            # (clean_env: under the AppImage's own library path kdialog could not load its Qt libraries and exited 1,
            # read as "No": the update was never offered on KDE desktops, 2026-11-07)
            r = run(cmd, timeout=600, env=clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except (OSError, subprocess.SubprocessError):
            continue
        err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else str(r.stderr or "")
        if POPUP_BROKEN.search(err):
            continue                    # (the tool did not start: not an answer)
        if r.returncode in (0, 1):
            return r.returncode == 0
    return None


# a pop-up tool that failed to load (the dynamic loader's own messages), not the player's "No"
POPUP_BROKEN = re.compile(r"error while loading shared libraries|symbol lookup error|version `[^']*' not found")


UPDATE_CHECK_S = 4.0     # GitHub's answer about a newer version waited for this long at the start (then: none)


def bounded(fn, seconds):
    """fn() in a thread, its result if it ends within seconds, else None (it finishes on its own). The start's update
    question waited for api.github.com however slow it was (12 s and more while GitHub was having trouble), before
    anything else happened."""
    out = []
    t = threading.Thread(target=lambda: out.append(fn()), name="update-check", daemon=True)
    t.start()
    t.join(seconds)
    return out[0] if out else None


def update_at_start(args, ask=input, popup=update_popup, packaged=None, check=None, download=None, start=None):
    """Before the game starts: when GitHub has a newer BeamNG.Earth, ask in a pop-up (update_popup; the console window
    when no pop-up can be shown) whether to install it now; yes downloads it beside this file (selfupdate.download,
    the same as the in-game "Update now") and starts it instead. True when the new copy was started. Offline, up to
    date, a source copy, --no-update-check or nowhere to ask: False and nothing asked."""
    packaged = (FROZEN or bool(os.environ.get("APPIMAGE"))) if packaged is None else packaged
    if getattr(args, "no_update_check", False) or not packaged:
        return False
    try:
        sys.path.insert(0, os.path.join(ROOT, "server"))
        import install
        import netfetch
        import selfupdate
        import updates
        net = netfetch.default_fetcher()
        current = install.version()
        got = bounded(lambda: (check or updates.check)(current, net), UPDATE_CHECK_S)
    except Exception:                   # never a reason not to start
        return False
    if not got:
        return False
    row("Update", f"BeamNG.Earth {got['latest']} is out (you have {got['current']})")
    yes = popup(f"BeamNG.Earth {got['latest']} is out (you have {got['current']}).\n\n"
                "Install it now, before the game starts?\n\n"
                "Yes downloads it and starts the new version.\nNo starts this version.")
    if yes is None:
        if not (sys.stdin and sys.stdin.isatty()):
            return False
        try:
            yes = ask("Install it now, before the game starts? [Y/n] ").strip().lower() in ("", "y", "yes")
        except (EOFError, OSError, KeyboardInterrupt):
            return False
    if not yes:
        say("Starting this version. (The game offers the update too: Settings, About.)")
        return False

    def progress(done, total):
        if total:
            _out(f"\r  Downloading... {done * 100 // total}% ({done >> 20} of {total >> 20} MB)")

    try:
        version, _ = (download or selfupdate.download)(net, current, None, progress)
    except Exception as e:              # selfupdate.UpdateError or anything else: this version starts
        say()
        problem(f"The update could not be downloaded: {e}", "Starting this version instead.")
        return False
    say()
    row("Update", f"BeamNG.Earth {version} downloaded")
    return (start or hand_over)()


def beammp_wait_seconds(value):
    import math
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("BeamMP wait must be seconds greater than 0 and at most 600")
    if not math.isfinite(seconds) or not 0 < seconds <= 600:
        raise argparse.ArgumentTypeError("BeamMP wait must be seconds greater than 0 and at most 600")
    return seconds


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--run-addon":     # (before tee_log: its output is its own run.log)
        ensure_ca_certs()
        run_addon(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--server":
        # (not tee_log: its output goes to server.log already. Teed, every line of the server's log was written a second
        # time into launcher.log, flushed line by line, and the launcher's own lines were trimmed out of it within a
        # session: a Windows test PC's launcher.log was 290 KB of server lines, 2026-11-12)
        ensure_ca_certs()
        run_server(sys.argv[2:])
        return
    tee_log()
    ensure_ca_certs()
    if len(sys.argv) > 1 and sys.argv[1] == "--world-cache":
        from tools import world_cache
        raise SystemExit(world_cache.main(sys.argv[2:]))
    if hand_over():
        return
    prune_old_copies()
    ap = argparse.ArgumentParser(description="Start the BeamNG.Earth world server and BeamNG.drive.")
    ap.add_argument("--no-game", action="store_true", help="only the server (start the game yourself)")
    ap.add_argument("--no-gcpatch", action="store_true",
                    help="the game without the engine patches (Linux: the preload library; Windows: enginepatch.py)")
    ap.add_argument("--keep-mod", action="store_true", help="leave the game's copy of the mod as it is")
    ap.add_argument("--no-beammp", action="store_true",
                    help="do not start BeamMP (multiplayer) with the game (bmplaunch.py)")
    ap.add_argument("--beammp-wait", type=beammp_wait_seconds, default=30.0, metavar="SECONDS",
                    help="wait up to this long for BeamMP, then continue in single player (default: 30; maximum: 600)")
    ap.add_argument("--cache-dir", metavar="PATH",
                    help="where the map download cache goes, e.g. another drive (kept for the next starts; "
                         "'default' goes back to .cache/beamng-earth in your home folder)")
    ap.add_argument("--clear-cache", action="store_true",
                    help="delete the map download cache before starting (at once: its folders are moved aside and "
                         "deleted in the background while you play; saved areas download again)")
    ap.add_argument("--beammp-launcher", metavar="PATH",
                    help="the BeamMP launcher you already have (its file or folder; kept for the next starts)")
    ap.add_argument("--game-dir", metavar="PATH",
                    help="where BeamNG.drive is installed, when it is not found in Steam's libraries (another "
                         "launcher, a folder of its own; kept for the next starts, 'default' forgets it)")
    ap.add_argument("--user-dir", help="BeamNG.drive user folder (default: found)")
    ap.add_argument("--proton", dest="build", action="store_const", const="proton",
                    help="Linux: the game is the Windows build under Proton")
    ap.add_argument("--native", dest="build", action="store_const", const="native",
                    help="Linux: the game is the native Linux build")
    # default: BeamNG's launch options dialog (renderer, safe mode...) shows, as when started from Steam (the user,
    # 2026-10-16); --gfx skips it
    ap.add_argument("--gfx", default="", choices=["dx11", "dx12", "vk", ""],
                    help="the Windows build's renderer, skipping its launch options dialog (default: the dialog)")
    ap.add_argument("--gpu", default="auto", choices=["auto", "discrete", "off"], metavar="MODE",
                    help="Linux: which GPU the game renders on when the machine has two (hybrid graphics): auto "
                         "(default) puts it on the discrete one when the default would be the weaker (the "
                         "integrated one), discrete forces that, off never steers; a GPU variable you or Steam's "
                         "launch options already set wins")
    ap.add_argument("--no-terminal", action="store_true", help="Linux: no terminal window (log to a file)")
    ap.add_argument("--no-update-check", action="store_true",
                    help="do not ask GitHub for a newer BeamNG.Earth before the game starts")
    ap.add_argument("extra", nargs=argparse.REMAINDER, help="after --: more options for the server")
    args = ap.parse_args()
    if IS_WIN:
        windows_console()
    elif not (sys.stdout and sys.stdout.isatty()):
        if not args.no_terminal and open_terminal():
            return
        log = os.path.join(data_dir(), "logs", "launcher.log")     # no terminal at all: a log
        os.makedirs(os.path.dirname(log), exist_ok=True)
        sys.stdout = sys.stderr = open(log, "w", encoding="utf-8")
    if not IS_WIN:
        # the terminal window closed (SIGHUP) or a stop request: as Ctrl+C, so the server is stopped first
        def stop(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGHUP, stop)
        signal.signal(signal.SIGTERM, stop)
    try:
        run(args)
    except KeyboardInterrupt:
        pass
    except SystemExit as e:
        if e.code not in (None, 0) and own_window():
            pause("Press Enter to close this window.")
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        if own_window():
            problem("BeamNG.Earth stopped with an error (above).")
            pause("Press Enter to close this window.")
        sys.exit(1)


def ask_game_dir(ask=None):
    """The game is in none of Steam's libraries (another launcher, a folder of its own): the player is asked where it
    is (the system's folder window, or the path typed), kept for the next starts. Skipped: through Steam as before."""
    say("BeamNG.drive was not found in Steam's libraries.")
    exe = "BeamNG.drive.exe" if IS_WIN else "BinLinux/BeamNG.drive.x64"
    d = apppaths.ask_path(f"Where is BeamNG.drive installed? (the folder with {exe})", "folder",
                          apppaths.set_game_dir, ask=ask, say=say)
    if d:
        say(f"  Using {d} (from now on; --game-dir PATH changes it, --game-dir default forgets it).")
    else:
        say("  Not set: the game is started through Steam.")
    return d


def beammp_install(user_dir, dd, ask=None):
    """BeamMP isn't installed: the player is asked (the user, 2026-10-24: "Yes/No/No and dont ask again", on both
    systems). Yes: the official launcher downloaded (bmplaunch.download). No: not this time. Don't ask again: the
    setting "Start BeamMP with BeamNG.Earth" turned off (turning it on again in the game asks again). Returns the
    launcher's path, or None."""
    import bmplaunch
    ask = ask or input
    say("BeamMP (multiplayer: play on BeamNG.Earth servers with other players) is not installed.")
    question = ("  Install the official BeamMP launcher now (a few MB from GitHub)?" if IS_WIN else
                "  Build BeamMP's launcher from its source now (a few minutes, once)?")
    try:
        answer = ask(question + " [Y]es / [N]o / [D]on't ask again / [H]ave it already: ")
    except (EOFError, OSError):
        return None                         # (no console to ask in: asked next time)
    answer = (answer or "").strip().lower()
    if answer in ("h", "have", "have it", "have it already"):
        # (a player's own launcher in a folder of their own: they did not want a second one, Discord 2026-10-24)
        exe = apppaths.ask_path(f"Where is your BeamMP launcher ({bmplaunch.EXE}, or its folder)?", "file",
                                lambda p: bmplaunch.remember(dd, p), ask=ask, say=say, name=bmplaunch.EXE)
        if exe:
            say(f"  Using {exe} (from now on; --beammp-launcher PATH changes it).")
        else:
            say(f"  No {bmplaunch.EXE} there: asked again next time (or start with --beammp-launcher PATH).")
        return exe
    if answer in ("d", "don't", "dont", "never"):
        bmplaunch.set_on_launch(user_dir, False)
        say("  Not installed, and not asked again. To use multiplayer later, turn on \"Start BeamMP with BeamNG.Earth\"")
        say("  in the game: Pause menu, Mods, BeamNG.Earth.")
        return None
    if answer in ("n", "no"):
        say("  Not installed this time.")
        return None
    import install
    ua = f"BeamNG.Earth/{install.version()}"
    if not IS_WIN:
        return bmplaunch.build_linux(dd, ua, say=lambda m: say("  " + m))
    exe = bmplaunch.download(dd, ua, say=lambda m: say("  " + m))
    if exe:          # (BeamMP's own installer sets the firewall up; started from here Windows asks once)
        say("  Windows Firewall may ask whether the BeamMP launcher may use the network: allow it, or multiplayer "
            "cannot connect.")
    return exe


def beammp_up(user_dir, build, args):
    """BeamMP for multiplayer (bmplaunch.py): the official BeamMP launcher started before the game (it updates BeamMP's
    mod in the game, which then connects to it). Returns its process (stopped with the server), or None."""
    import bmplaunch
    if getattr(args, "no_beammp", False):
        return None
    if under_wine() or (not IS_WIN and build == "proton"):
        row("Multiplayer", "BeamMP not started with the Windows build under Proton / Wine (start it yourself)")
        return None
    if bmplaunch.running():
        row("Multiplayer", "BeamMP is already running")
        return None
    dd = data_dir()
    own = apppaths.saved_game_dir()
    if not IS_WIN and own and not apppaths.in_steam_library(own):
        # (BeamMP's launcher looks for the game in Steam's libraries only on Linux, and has no option for a folder)
        row("Multiplayer", "BeamMP not started: on Linux BeamMP's launcher finds the game only in Steam's libraries")
        return None
    if not bmplaunch.on_launch(user_dir, dd):
        row("Multiplayer", "BeamMP not started (turned off: BeamNG.Earth settings, \"Start BeamMP with BeamNG.Earth\")")
        return None
    if getattr(args, "beammp_launcher", None):
        if bmplaunch.remember(dd, args.beammp_launcher):
            say(f"  BeamMP launcher: {bmplaunch.remembered(dd)} (kept for the next starts)")
        else:
            say(f"  --beammp-launcher: no {bmplaunch.EXE} at {args.beammp_launcher}")
    exe = bmplaunch.find(dd)
    if exe and not IS_WIN and bmplaunch.rebuild_due(dd) and exe == os.path.join(dd, "beammp", bmplaunch.EXE):
        import install          # (the one built here was too old for BeamMP's mod: built again from BeamMP's source)
        exe = bmplaunch.build_linux(dd, f"BeamNG.Earth/{install.version()}", say=lambda m: say("  " + m)) or exe
    if not exe:
        exe = beammp_install(user_dir, dd)
    if exe and not IS_WIN:
        too_old = bmplaunch.system_too_old(exe)
        if too_old:
            row("Multiplayer", f"BeamMP cannot start: {too_old}")
            return None
    if not exe:
        row("Multiplayer", "BeamMP not installed")
        return None
    log_path = os.path.join(dd, "logs", "beammp-launcher.log")
    proc = bmplaunch.start(exe, user_dir, log_path)
    timeout = getattr(args, "beammp_wait", 30.0)
    try:
        row("Multiplayer", f"starting BeamMP (up to {timeout:g}s; --no-beammp skips this) ...")
        ready = bmplaunch.wait_ready(proc, timeout=timeout)
    except BaseException:
        bmplaunch.stop(proc)
        raise
    if not ready:
        bmplaunch.stop(proc)
        row("Multiplayer", f"BeamMP did not become ready; continuing in single player. "
                           f"Use --beammp-wait for a longer wait, or --no-beammp to skip it. "
                           f"Logs: {log_path}; {os.path.join(os.path.dirname(exe), 'Launcher.log')}")
        return None
    try:
        version = bmplaunch.log_version(bmplaunch.read_log(log_path, exe))
        row("Multiplayer", f"BeamMP{' v' + version if version else ''} ready: in the game, Multiplayer, then a server "
            "running BeamNG.Earth")
        support_notice(multiplayer=True)
    except BaseException:
        bmplaunch.stop(proc)
        raise

    def watch():                            # (a launcher too old for BeamMP's current mod: said once, plainly)
        for _ in range(60):
            time.sleep(5)
            if proc.poll() is not None:
                return
            if bmplaunch.log_too_old(bmplaunch.read_log(log_path, exe)):
                if not IS_WIN and exe == os.path.join(dd, "beammp", bmplaunch.EXE):
                    bmplaunch.save_settings(dd, rebuild=True)
                    problem("BeamMP's launcher built here is older than BeamMP's mod (\"Invalid lua communication\").",
                            "It is built again from BeamMP's newest source at the next start of BeamNG.Earth.")
                    return
                problem(f"BeamMP's launcher{' v' + version if version else ''} ({exe}) cannot talk to BeamMP's mod in "
                        "the game (\"Invalid lua communication\" in its log): it is older than the mod it downloaded.",
                        "Update BeamMP's launcher (on Linux: build the latest from github.com/BeamMP/BeamMP-Launcher)."
                        " Multiplayer will not work until then; single player is not affected.")
                return
    try:
        threading.Thread(target=watch, daemon=True, name="beammp-watch").start()
    except BaseException:
        bmplaunch.stop(proc)
        raise
    return proc


def run(args):
    import install
    title(f"BeamNG.Earth {install.version()}")
    support_notice()
    if update_at_start(args):           # the new copy runs now (in its own window)
        return
    memory_notice()                     # (8 GB or less: what to expect; never stops the start)
    wine = under_wine()
    build = "native"
    if args.user_dir:
        user_dir = args.user_dir
        build = args.build or "native"
    elif not IS_WIN:
        build, user_dir = linux_build(args.build)
    else:
        user_dir = user_folder()
    tool = steam_compat_tool() if not IS_WIN and not args.user_dir else None
    if not args.user_dir and build != "proton":
        # the player's own -userpath in Steam's launch options: the game uses that folder whatever we pass, so the
        # mod and the world's files go there too (GitHub #770; Discord 2026-11-05: "the fix was removing the custom
        # launch option from steam")
        own = steam_user_path(steam_launch_options())
        if own and not apppaths.same_dir(own, user_dir):
            row("Game folder", f"{own}  (your Steam launch options' -userpath)")
            user_dir = own
    steam_args = ({}, [], [])
    if not IS_WIN:
        line = steam_launch_options()
        if line:
            steam_args = split_launch_options(line)
            if any(steam_args):
                row("Steam options", line)
        gpu_env, gpu_note = gpu_plan(args.gpu, environ=dict(os.environ, **steam_args[0]))
        if gpu_env:
            os.environ.update(gpu_env)      # the server inherits them too (its own GPU work: earthfast's compute)
        if gpu_note:
            row("GPU", gpu_note)
    row("Game folder", user_dir + ("  (the Windows build, Proton)" if build == "proton" else "")
        + (f"  (Steam compatibility tool: {tool})" if tool else ""))       # (which was read: GitHub #56)
    running = game_still_running()
    if not running:
        install.clear_reload_files(user_dir)
    reload_game = False
    if not args.keep_mod:
        status = install.install_mod(user_dir, True, dry_run=False, quiet=True, game_running=running)
        # the game read its mod list without this copy: its Lua is reloaded once the server runs (install.py)
        reload_game = running and status in ("updated", "installed", "enabled")
        row("Mod", {"current": "up to date", "enabled": "turned back on", "updated": "updated",
                    "installed": "installed", "kept": "kept the installed copy"}.get(status, status or ""))
        # the sky's haze from above the atmosphere (the 3D map from orbit; skypatch.py): from the game's own shader
        try:
            import skypatch
            sky = skypatch.apply(game_install_dir(), os.path.join(user_dir, "mods", "unpacked", install.MOD_NAME),
                                 os.path.join(apppaths.data_dir(), "skypatch"), user_dir)
            if sky == "patched":
                row("Sky", "the view from orbit fixed (two lines of the game's sky shader; original kept; the game "
                           "builds its shaders again once)")
        except Exception as e:      # (never in the way of a start)
            row("Sky", f"not patched ({type(e).__name__}: {e})")
    if getattr(args, "game_dir", None):
        if args.game_dir.strip().strip('"').lower() == "default":
            apppaths.set_game_dir("default")
            row("Game", "found as before (the chosen folder forgotten)")
        elif apppaths.set_game_dir(args.game_dir):
            row("Game", f"{apppaths.saved_game_dir()} (kept for the next starts)")
        else:
            say(f"  --game-dir: BeamNG.drive is not in {args.game_dir}")
    if not (args.no_game or running or wine or build != "native") and not (
            windows_game_dir() if IS_WIN else linux_game_exe()):
        ask_game_dir()
    extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
    port_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    port_parser.add_argument("--port", type=int, default=SERVER_PORT)
    port = port_parser.parse_known_args(extra)[0].port
    if "--cache-dir" not in extra:
        try:
            cache = cache_dir_arg(args.cache_dir)
        except OSError as e:
            problem(f"the download cache folder {args.cache_dir} cannot be made ({e}).",
                    "Pick another folder (--cache-dir PATH), or --cache-dir default.")
            sys.exit(1)
        if cache:
            extra = ["--cache-dir", cache] + extra
            row("Download cache", cache)
    if server_port_busy(port):
        problem(f"another BeamNG.Earth world server is already running (port {port} is in use), maybe "
                "left over from an earlier start.",
                "Close its window (or end 'BeamNG.Earth' in the task manager), then start again.")
        sys.exit(1)
    if getattr(args, "clear_cache", False):
        clear_cache(extra)
    log_path = os.path.join(data_dir(), "logs", "server.log")
    server = start_server(user_dir, extra, log_path, game_running=bool(running))
    beammp = None
    try:
        row("World server", "starting")
        row("Server log", log_path)
        row("Telemetry", "anonymous, no location: as chosen on the game's welcome page, nothing before that "
            "(BNE_TELEMETRY=off turns it off)" if telemetry_on() else "off")
        # The game starts as early as it can (GAME_FIRST): the world server starts beside it, while BeamMP and the game
        # start (the mod connects to it when it listens, and retries until then; the game takes far longer to reach its
        # menu). Waited for in turn, the server's start, BeamMP's and the game's added up to ten seconds of a start on a
        # Windows PC (a player: "a week ago ... within 2 seconds it was asking what render ... now its closer to 10
        # seconds", Discord 2026-11-12). BeamMP still before the game: its launcher puts BeamMP's mod in place before
        # the game reads its mods. A server that cannot start is said as before; the game is then already open.
        early = GAME_FIRST and not args.no_game and not running
        followed = []

        def follow_server():            # (once: while start_game watches the start, or after it)
            if not followed:
                followed.append(True)
                server_up(server, log_path, port, game_started=True)
        if not early:
            server_up(server, log_path, port, game_started=False)
        try:
            beammp = None if args.no_game else beammp_up(user_dir, build, args)
        except OSError as e:
            # (multiplayer is optional: a damaged BeamMP launcher folder, WinError 1392 "the file or directory is
            # corrupted and unreadable", stopped the app before the game started, GitHub #282)
            row("Multiplayer", f"BeamMP not started ({e}); if this repeats, delete the BeamMP-Launcher folder in "
                               "%LOCALAPPDATA% (BeamNG.Earth downloads it again)")
            beammp = None
        if args.no_game:
            say()
            say("Start BeamNG.drive and pick 'BeamNG.Earth' in Freeroam. Ctrl+C stops the server.")
            server.wait()
            return
        watch_game_log(user_dir, time.time() - 5)
        if running:
            if IS_WIN and windowless_game():
                problem("a BeamNG.drive process is running in the background without a window (left over from a "
                        "game that crashed or did not close?), so the game cannot start.",
                        "Open Task Manager (Ctrl+Shift+Esc) > Details, end every BeamNG.drive.x64.exe, then start "
                        "BeamNG.Earth again.")
            if not reload_game:
                row("Game", "already running")
            elif install.reload_running_game(user_dir):
                row("Game", "already running: reloaded its Lua so it loads the mod")
            else:
                row("Game", "already running: it loads the mod on its next start")
            proc = None
        else:
            if wine:
                say("(under Wine / Proton: on Linux the AppImage runs the server natively, faster)")
            clear_level_flag(user_dir)
            proc = start_game(not args.no_gcpatch, build, args.gfx, user_dir, steam_args,
                              meanwhile=follow_server if early else None)
        if IS_WIN and not args.no_gcpatch:
            # the Windows game's engine patches, written into its memory once it runs (enginepatch.py: dtsmooth; off
            # with BNGEARTH_DTSMOOTH_WIN=0 or BNGEARTH_DTSMOOTH_OFF=1)
            try:
                import enginepatch
                threading.Thread(target=enginepatch.attach, args=(user_dir,), kwargs={"log": say},
                                 name="enginepatch", daemon=True).start()
            except Exception as e:      # (never in the way of the game)
                say(f"(engine patches: {e})")
        if early:
            follow_server()
        say()
        say("In the game: Freeroam > BeamNG.Earth.  Closing the game stops the server.")
        say()
        if wait_for_game(proc, server):
            row("Game", "closed")
    finally:
        if beammp is not None:
            import bmplaunch
            bmplaunch.stop(beammp)
        stop_server(server)


if __name__ == "__main__":
    multiprocessing.freeze_support()        # the server's worker processes in the packaged app
    main()
