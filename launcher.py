#!/usr/bin/env python3
"""BeamNG.Earth launcher: one start for everything (the AppImage and the .exe run this; from the source folder:
python launcher.py).

  1. copies the mod into the game's user folder when it differs (one zip, one version),
  2. starts the world server (server/earthd.py) with the recommended options,
  3. starts BeamNG.drive unless it already runs. Linux: the native game directly, with tools/gcpatch preloaded
     (keeps the grass when the physics block moves; it checks the game build and does nothing on another), so no
     Steam launch options are needed; through Steam when that is not possible (Steam not running) and for the
     Windows build under Proton (found from Steam's compatibility setting, else from the user folder the game
     wrote last). Windows: through Steam. Under Wine / Proton (the .exe started with protontricks) it cannot start
     the game: it asks you to press Play in Steam.
  4. stops the server when the game closes (or on Ctrl+C / closing the window).

It runs in a console window, like BeamMP's launcher: the .exe opens one when it has none, the AppImage opens a
terminal when started from a file manager (else it logs to ~/.cache/beamng-earth/logs).

    launcher.py                  everything
    launcher.py --no-game        only the server (start the game yourself)
    launcher.py --no-gcpatch     the game without the grass patch
    launcher.py --proton / --native   Linux: which build of the game to start (default: found)
    launcher.py -- --verbose     anything after -- goes to the server
"""

import argparse
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

FROZEN = getattr(sys, "frozen", False)
ROOT = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == "nt"
APP_ID = "284160"
GAME_EXE_LINUX = os.path.join("BinLinux", "BeamNG.drive.x64")
GAME_EXE_WIN = "BeamNG.drive.x64.exe"
GCPATCH_NAME = "libbngearth_gcpatch.so"
# the recommended server options (start.py's)
RECOMMENDED = ["--deshadow", "full", "--near-visual", "detail", "--airports", "on", "--sea", "on", "--trees", "on",
               "--buildings", "on"]
SERVER_PORT = 47613         # earthd's default
GAME_GONE_S = 30            # no game process this long: the game closed (wait_for_game)
GAME_START_S = 180          # waiting for the game's process after asking Steam
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


def problem(msg, hint=None):
    """Something that stops the start: set apart, with what to do about it."""
    import textwrap
    _out("")
    _out("  Problem: " + "\n           ".join(textwrap.wrap(msg, 78)))
    if hint:
        for line in textwrap.wrap(hint, 78):
            _out(f"           {line}")
    _out("")


def data_dir():
    """Where the launcher keeps its files (logs, the gcpatch copy): the server's cache folder's parent."""
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "beamng-earth")
    return os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth")


# --- the server -----------------------------------------------------------------------------------

def watch_parent():
    """The server's guard: it ends (with its worker processes) when the launcher that started it is gone, e.g. its
    terminal closed (SIGHUP) or it was killed. A server left behind kept the port, so the next start's server could
    not listen and the game talked to the old one (the user's AppImage, 2026-10-16)."""
    try:
        parent = int(os.environ.get("BNGEARTH_PARENT") or 0)
    except ValueError:
        parent = 0
    if not parent:
        return
    import threading

    def gone():
        if IS_WIN:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x00100000, False, parent)      # SYNCHRONIZE
            if not h:
                return True
            k32.WaitForSingleObject(h, 0xFFFFFFFF)
            return True
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


def start_server(user_dir, extra, log_path):
    if IS_WIN and not FROZEN:
        import install
        install.check_python()
        cmd = [install.PYTHON_EXE, os.path.abspath(__file__)]
    else:
        cmd = [sys.executable] + ([] if FROZEN else [os.path.abspath(__file__)])
    cmd += ["--server", "--user-dir", user_dir] + RECOMMENDED + extra
    env = dict(os.environ)
    env["BNGEARTH_PARENT"] = str(os.getpid())      # the server ends when this launcher does (watch_parent)
    kw = {"cwd": ROOT, "env": env}
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True          # its worker processes with it: stopped as one group
    # its full log to a file; the console shows only its warnings and errors (echo_server_log): the INFO lines
    # buried the launcher's own (the user, 2026-10-17: 'easier to read')
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    out = open(log_path, "w", encoding="utf-8")
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
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def stop_server(proc):
    if proc is None or proc.poll() is not None:
        return
    row("World server", "stopping")
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(15)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if IS_WIN:
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


# --- the game -------------------------------------------------------------------------------------

def linux_game_exe():
    import install
    for lib in install._steam_roots():
        exe = os.path.join(lib, "steamapps", "common", "BeamNG.drive", GAME_EXE_LINUX)
        if os.path.isfile(exe):
            return exe
    return None


def game_pids():
    """The game's main processes (not its CEF helpers)."""
    if IS_WIN:
        # bytes, decoded leniently: on a non-English Windows tasklist's "no tasks" message is in the console's code
        # page (German "ü" = 0x81 in cp850), which text=True's cp1252 could not decode (a player's report,
        # 2026-10-17: the launcher stopped with UnicodeDecodeError and r.stdout None)
        try:
            r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE_WIN}", "/FO", "CSV", "/NH"],
                               capture_output=True)
        except OSError:
            return []
        text = (r.stdout or b"").decode("utf-8", errors="replace")
        out = []
        for line in text.splitlines():
            if line.startswith(f'"{GAME_EXE_WIN}"'):
                try:
                    out.append(int(line.split('","')[1]))
                except (IndexError, ValueError):
                    pass
        return out
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
    return bool(game_pids()) or game_connected()


def steam_running():
    home = os.path.expanduser("~")
    for pidfile in (os.path.join(home, ".steam", "steam.pid"),
                    os.path.join(home, ".var", "app", "com.valvesoftware.Steam", ".steam", "steam.pid")):
        try:
            with open(pidfile) as f:
                pid = int(f.read().strip())
            if os.path.exists(f"/proc/{pid}"):
                return True
        except (OSError, ValueError):
            pass
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
        subprocess.Popen(cmd, env=clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


def _ini(path):
    """key = value lines of an ini file (BeamNG's own, with a BOM)."""
    out = {}
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith((";", "#", "[")):
                    k, v = line.split("=", 1)
                    out[k.strip()] = os.path.expandvars(v.strip())
    except OSError:
        pass
    return out


def windows_beamng_ini():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return _ini(os.path.join(base, "BeamNG", "BeamNG.drive.ini"))


def windows_game_dir():
    """The game's folder on Windows (also inside a Proton prefix), as BeamMP's launcher finds it: installPath in
    %LOCALAPPDATA%\\BeamNG\\BeamNG.drive.ini, else the game's registry key, else Steam's libraries."""
    ok = lambda d: d and os.path.isfile(os.path.join(d, "BeamNG.drive.exe"))
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


def start_game(use_gcpatch, build="native", gfx="", user_dir=None):
    """Start the game; returns a Popen when this process started it directly, else None (through Steam)."""
    win_args = ["-gfx", gfx] if gfx else []
    extra = os.environ.get("BNGE_GAME_ARGS", "").split()     # (tests: e.g. "-bngetest car,damage")
    win_args += extra
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
        if exe and steam_running():
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
            row("Game", "starting" + ("  (with the grass patch)" if lib else ""))
            proc = subprocess.Popen(([runtime, "--verb=waitforexitandrun", "--"] if runtime else []) + [exe] + extra,
                                    cwd=os.path.dirname(os.path.dirname(exe)), env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
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
MODS_OFF_LINE = "Mods will be disabled after updating"
GAME_LOG_WATCH_S = 600       # s after the game's start the log is watched


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
            if MODS_OFF_LINE.encode() in data:
                problem("BeamNG.drive was updated and turned all mods off, BeamNG.Earth too.",
                        "In the game's main menu, choose to enable your mods again (or Mods > Manage, turn "
                        "BeamNG.Earth on), then pick Freeroam > BeamNG.Earth. The world server keeps running.")
                return

    threading.Thread(target=run, name="watch-game-log", daemon=True).start()


def wait_for_game(proc):
    """Until the game closes."""
    if proc is not None:
        proc.wait()
        while game_pids():          # the Windows build's launcher may end before the game
            time.sleep(3)
        return
    t0 = time.time()
    while not game_pids():
        if time.time() - t0 > GAME_START_S:
            say("the game did not start (no BeamNG.drive process); the server keeps running, Ctrl+C stops it")
            while True:
                time.sleep(3600)
        time.sleep(2)
    gone_since = None
    while True:
        # closed only once no game process was seen for GAME_GONE_S: under Steam's runtime container (a Flatpak
        # Steam's above all) the game's process can end and start again during its start (a player's report,
        # 2026-10-17: the server stopped right after the game launched)
        if game_alive():
            gone_since = None
        elif gone_since is None:
            gone_since = time.time()
        elif time.time() - gone_since > GAME_GONE_S:
            return
        time.sleep(3)


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


def windows_console():
    """Windows: a console window of our own when there is none (Wine / Proton started us without one)."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        if not k32.GetConsoleWindow():
            k32.FreeConsole()
            if k32.AllocConsole():
                sys.stdout = sys.stderr = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")
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
    env = {k: v for k, v in os.environ.items() if k not in ("APPDIR", "APPIMAGE", "ARGV0", "OWD")}
    try:
        subprocess.Popen([new] + sys.argv[1:], env=env, cwd=os.path.dirname(new),
                         **({"creationflags": subprocess.CREATE_NEW_CONSOLE} if IS_WIN else {"start_new_session": True}))
    except OSError:
        return False
    print(f"Starting the updated BeamNG.Earth: {os.path.basename(new)}", flush=True)
    return True


def main():
    ensure_ca_certs()
    if len(sys.argv) > 1 and sys.argv[1] == "--server":
        run_server(sys.argv[2:])
        return
    if hand_over():
        return
    ap = argparse.ArgumentParser(description="Start the BeamNG.Earth world server and BeamNG.drive.")
    ap.add_argument("--no-game", action="store_true", help="only the server (start the game yourself)")
    ap.add_argument("--no-gcpatch", action="store_true", help="Linux: start the game without the grass patch")
    ap.add_argument("--keep-mod", action="store_true", help="leave the game's copy of the mod as it is")
    ap.add_argument("--user-dir", help="BeamNG.drive user folder (default: found)")
    ap.add_argument("--proton", dest="build", action="store_const", const="proton",
                    help="Linux: the game is the Windows build under Proton")
    ap.add_argument("--native", dest="build", action="store_const", const="native",
                    help="Linux: the game is the native Linux build")
    # default: BeamNG's launch options dialog (renderer, safe mode...) shows, as when started from Steam (the user,
    # 2026-10-16); --gfx skips it
    ap.add_argument("--gfx", default="", choices=["dx11", "dx12", "vk", ""],
                    help="the Windows build's renderer, skipping its launch options dialog (default: the dialog)")
    ap.add_argument("--no-terminal", action="store_true", help="Linux: no terminal window (log to a file)")
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


def run(args):
    import install
    title(f"BeamNG.Earth {install.version()}")
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
    row("Game folder", user_dir + ("  (the Windows build, Proton)" if build == "proton" else "")
        + (f"  (Steam compatibility tool: {tool})" if tool else ""))       # (which was read: GitHub #56)
    running = game_alive()
    if not running:
        install.clear_reload_files(user_dir)
    reload_game = False
    if not args.keep_mod:
        status = install.install_mod(user_dir, True, dry_run=False, quiet=True)
        # the game read its mod list without this copy: its Lua is reloaded once the server runs (install.py)
        reload_game = running and status in ("updated", "installed", "enabled")
        row("Mod", {"current": "up to date", "enabled": "turned back on", "updated": "updated",
                    "installed": "installed", "kept": "kept the installed copy"}.get(status, status or ""))
    extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
    if "--port" not in extra and server_port_busy():
        problem(f"another BeamNG.Earth world server is already running (port {SERVER_PORT} is in use), maybe "
                "left over from an earlier start.",
                "Close its window (or end 'BeamNG.Earth' in the task manager), then start again.")
        sys.exit(1)
    log_path = os.path.join(data_dir(), "logs", "server.log")
    server = start_server(user_dir, extra, log_path)
    row("World server", "running")
    row("Server log", log_path)
    try:
        if args.no_game:
            say()
            say("Start BeamNG.drive and pick 'BeamNG.Earth' in Freeroam. Ctrl+C stops the server.")
            server.wait()
            return
        watch_game_log(user_dir, time.time() - 5)
        if running:
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
            proc = start_game(not args.no_gcpatch, build, args.gfx, user_dir)
        say()
        say("In the game: Freeroam > BeamNG.Earth.  Closing the game stops the server.")
        say()
        wait_for_game(proc)
        row("Game", "closed")
    finally:
        stop_server(server)


if __name__ == "__main__":
    multiprocessing.freeze_support()        # the server's worker processes in the packaged app
    main()
