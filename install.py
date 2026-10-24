#!/usr/bin/env python3
"""BeamNG.Earth installer (Windows and Linux; Python 3.10+, standard library only).

What it does:
  1. finds your BeamNG.drive user folder (or takes --user-dir),
  2. copies the mod (mod/beamng_earth/) to <user folder>/mods/unpacked/beamng_earth,
  3. installs the server's packages into the bundled Python on Windows, or a .venv on Linux,
  4. remembers the user folder in .beamng_earth.json for start.py and the run_server launchers.

Run it again at any time: it only changes what is out of date.

    python install.py                 (Windows: py install.py also works)
    python install.py --user-dir "D:\\BeamNG user folder\\current" --yes
    python install.py --no-venv       (only the mod)
    python install.py --dry-run       (show what would happen, change nothing)
"""

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
MOD_SRC = os.path.join(ROOT, "mod", "beamng_earth")
MOD_NAME = "beamng_earth"
VENV = os.path.join(ROOT, ".venv")
REQUIREMENTS = os.path.join(ROOT, "server", "requirements.txt")
CONFIG = os.path.join(ROOT, ".beamng_earth.json")
REQ_STAMP = os.path.join(ROOT, ".requirements.sha256")  # start.py uses this stamp on Windows and Linux
STEAM_APP_ID = "284160"
MIN_PY = (3, 10)
IS_WIN = os.name == "nt"

# --- Windows 7 standalone build: the embedded Python lives in the "python" folder next to this file ---
# (override with the BNGE_PYTHON_DIR environment variable). pip is run as "python.exe -m pip": the
# Scripts\\pip.exe launcher has the original install path written into it, so it is not relocatable.
PY_DIR = os.environ.get("BNGE_PYTHON_DIR") or os.path.join(ROOT, "python")
PYTHON_EXE = os.path.join(PY_DIR, "python.exe")
PYTHONW_EXE = os.path.join(PY_DIR, "pythonw.exe")


def say(msg=""):
    print(msg, flush=True)


def fail(msg, code=1):
    say("")
    say("ERROR: " + msg)
    sys.exit(code)


def version():
    try:
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


# --- finding the BeamNG.drive user folder -------------------------------------------------------

def _steam_roots():
    home = os.path.expanduser("~")
    roots = [
        os.path.join(home, ".steam", "steam"),
        os.path.join(home, ".steam", "root"),
        os.path.join(home, ".local", "share", "Steam"),
        os.path.join(home, ".var", "app", "com.valvesoftware.Steam", ".local", "share", "Steam"),  # Flatpak
        os.path.join(home, "snap", "steam", "common", ".local", "share", "Steam"),
    ]
    libs = []
    for r in roots:
        if not os.path.isdir(r):
            continue
        libs.append(r)
        vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                libs += re.findall(r'"path"\s+"([^"]+)"', f.read())
        except OSError:
            pass
    out, seen = [], set()
    for lib in libs:
        lib = lib.replace("\\\\", "\\")
        key = os.path.realpath(lib)
        if key not in seen and os.path.isdir(lib):
            seen.add(key)
            out.append(lib)
    return out


FLATPAK_STEAM = "com.valvesoftware.Steam"


def native_user_dirs():
    """Where the native Linux game keeps its user folder: ~/.local/share, or inside the Steam Flatpak's own home
    (~/.var/app/com.valvesoftware.Steam: the game there never sees ~/.local/share, a player's report 2026-10-17)
    or the Steam snap's. Default first; not checked for existence."""
    home = os.path.expanduser("~")
    tail = ("BeamNG", "BeamNG.drive", "current")
    return [os.path.join(home, ".local", "share", *tail),
            os.path.join(home, ".var", "app", FLATPAK_STEAM, ".local", "share", *tail),
            os.path.join(home, ".var", "app", FLATPAK_STEAM, "data", *tail),
            os.path.join(home, "snap", "steam", "common", ".local", "share", *tail)]


def flatpak_steam():
    """Steam is the Flatpak (its data folder exists)."""
    return os.path.isdir(os.path.join(os.path.expanduser("~"), ".var", "app", FLATPAK_STEAM, ".local", "share",
                                      "Steam"))


def candidate_user_dirs():
    """Every BeamNG.drive user folder that exists on this machine, best first."""
    cands = []
    if IS_WIN:
        local = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        cands.append(os.path.join(local, "BeamNG", "BeamNG.drive", "current"))     # 0.32 and later
        old = sorted(glob.glob(os.path.join(local, "BeamNG.drive", "[0-9]*")), reverse=True)
        cands += [d for d in old if os.path.isdir(d)]                               # before 0.32: one per version
    else:
        cands += native_user_dirs()
        for lib in _steam_roots():                                                  # Windows build under Proton
            pfx = os.path.join(lib, "steamapps", "compatdata", STEAM_APP_ID, "pfx", "drive_c", "users", "steamuser",
                               "AppData", "Local")
            cands.append(os.path.join(pfx, "BeamNG", "BeamNG.drive", "current"))
            cands += sorted(glob.glob(os.path.join(pfx, "BeamNG.drive", "[0-9]*")), reverse=True)
    out, seen = [], set()
    for c in cands:
        key = os.path.realpath(c)
        if os.path.isdir(c) and key not in seen:
            seen.add(key)
            out.append(c)
    return out


def ask(prompt, assume_yes):
    if assume_yes:
        say(prompt + " yes (--yes)")
        return True
    try:
        return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def choose_user_dir(args):
    if args.user_dir:
        d = os.path.abspath(os.path.expanduser(args.user_dir))
        if not os.path.isdir(d):
            if args.dry_run:
                say(f"(dry run) the folder {d} does not exist yet; it would be created.")
            elif ask(f"The folder {d} does not exist. Create it?", args.yes):
                os.makedirs(d, exist_ok=True)
            else:
                fail("no BeamNG.drive user folder.")
        return d
    found = candidate_user_dirs()
    if not found:
        where = (r"%LOCALAPPDATA%\BeamNG\BeamNG.drive\current" if IS_WIN else
                 "~/.local/share/BeamNG/BeamNG.drive/current (native) or Steam's "
                 f"compatdata/{STEAM_APP_ID}/pfx/.../AppData/Local/BeamNG/BeamNG.drive/current (Proton)")
        fail("could not find your BeamNG.drive user folder.\n"
             "  Start BeamNG.drive once (it creates the folder), then run this again.\n"
             f"  It is usually {where}.\n"
             "  In the game: Options > Other > 'User folder' shows where it is; or pass it with\n"
             '  python install.py --user-dir "<that folder>"')
    if len(found) == 1:
        say(f"BeamNG.drive user folder: {found[0]}")
        return found[0]
    say("Found more than one BeamNG.drive user folder:")
    for i, d in enumerate(found, 1):
        say(f"  {i}. {d}")
    if args.yes:
        say(f"Using the first one (--yes): {found[0]}")
        return found[0]
    while True:
        try:
            pick = input(f"Which one does your game use? [1-{len(found)}, Enter = 1] ").strip() or "1"
        except EOFError:
            pick = "1"
        if pick.isdigit() and 1 <= int(pick) <= len(found):
            return found[int(pick) - 1]
        say("Please type one of the numbers.")


# --- the mod ------------------------------------------------------------------------------------

def tree_hash(path):
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            h.update(os.path.relpath(full, path).replace(os.sep, "/").encode())
            with open(full, "rb") as f:
                h.update(hashlib.sha256(f.read()).digest())
    return h.hexdigest()


def ensure_mod_active(user_dir):
    """Turn the mod back on in the game's mod list (mods/db.json) if something turned it off: joining a BeamMP server
    turns off every mod the server doesn't send and leaves them off afterwards (2026-10-22). Only this mod's entry
    changes. True if it did."""
    path = os.path.join(user_dir, "mods", "db.json")
    try:
        with open(path, encoding="utf-8") as f:
            db = json.load(f)
    except (OSError, ValueError):
        return False
    entry = db.get("mods", {}).get(MOD_NAME) if isinstance(db, dict) else None
    if not isinstance(entry, dict) or entry.get("active") is not False:
        return False
    entry["active"] = True
    tmp = path + ".bnge-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)
    os.replace(tmp, path)
    return True


def install_mod(user_dir, assume_yes, dry_run, quiet=False):
    """Copy the mod into the user folder. Returns 'current', 'enabled' (current, but it had been turned off in the
    game's mod list), 'updated', 'installed', 'kept' or None (dry run); quiet: no messages but the notes (the launcher
    prints one status line)."""
    def note(msg):
        if not quiet:
            say(msg)
    if not os.path.isdir(MOD_SRC):
        fail(f"the mod folder is missing: {MOD_SRC}\n  Run install.py from the unzipped BeamNG.Earth folder.")
    dest = os.path.join(user_dir, "mods", "unpacked", MOD_NAME)
    packed = os.path.join(user_dir, "mods", MOD_NAME + ".zip")
    if os.path.isfile(packed):
        say(f"Note: there is also a zipped copy at {packed}; remove it so the game does not load two versions.")
    enabled = not dry_run and ensure_mod_active(user_dir)
    if enabled:
        say("Mod: it had been turned off in the game's mod list (a BeamMP server does that); turned it back on.")
    if os.path.isdir(dest):
        if tree_hash(dest) == tree_hash(MOD_SRC):
            note(f"Mod: already up to date in {dest}")
            return "enabled" if enabled else "current"
        if not (assume_yes and quiet) and not ask(f"Mod: an older copy is installed in {dest}. Replace it?", assume_yes):
            note("Mod: kept the installed copy (it may not match this server version).")
            return "kept"
        if dry_run:
            say(f"(dry run) would replace {dest}")
            return None
        old_times = _unchanged_times(dest, MOD_SRC)
        shutil.rmtree(dest)
        status = "updated"
    elif dry_run:
        say(f"(dry run) would copy the mod to {dest}")
        return None
    else:
        status = "installed"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copytree(MOD_SRC, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    if status == "updated":
        _stamp_times(dest, old_times)
    note(f"Mod: installed to {dest}")
    return status


# The game packs a terrain material's textures into its own cache (temp/art/terrainMaterialCache) and packs them again,
# 1.5-3 s each on its main thread, whenever a source file is newer than its cache entry ("cache outdated" in its log).
# An update copied every file with the build's times (a fresh checkout's: new), so the first session after each one
# repacked every ground material as it first appeared: freezes of ~2.7 s driving into new ground (the user,
# 2026-10-21: "freezes" in Pécs and Brisbane on 1.5.4, 13 repacks in the game's log). An unchanged file keeps the
# time of the copy it replaces; a changed one gets now, newer than any cache entry made from its old content.
def _unchanged_times(dest, src):
    """{relative path: (atime, mtime)} of the files in dest whose content is the same in src."""
    import filecmp
    out = {}
    for dirpath, _, filenames in os.walk(dest):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, dest)
            other = os.path.join(src, rel)
            try:
                if os.path.isfile(other) and filecmp.cmp(full, other, shallow=False):
                    st = os.stat(full)
                    out[rel] = (st.st_atime, st.st_mtime)
            except OSError:
                pass
    return out


def _stamp_times(dest, old_times):
    """The installed files' times: an unchanged file's (old_times) as before, the others now."""
    for dirpath, _, filenames in os.walk(dest):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                os.utime(full, old_times.get(os.path.relpath(full, dest)))
            except OSError:
                pass


# --- a game already running: its Lua reloaded so it mounts the mod ------------------------------

# A game started before the app (by the BeamMP launcher, or by hand) read its mod list when it started: a mod installed,
# updated or turned back on since would load only on its next start (the user, 2026-10-23: "If the game is already
# running and you launch the .earth server. Force the game to reload so it loads the mod properly"). The game takes
# beamng: commands on a named pipe (BinLinux/BeamNG_scheme_handler.sh writes them there; Windows: the same name under
# \\.\pipe\), and one fires its Reload Lua action (Ctrl+L): a Lua reload reads the mod list again and mounts what is on.
# A level open through the reload is left behind the main menu (freeroam's state gone), so a one-shot script in the user
# folder (every modScript.lua runs when the mod list is read) starts it again and removes itself.
GAME_PIPE = r"\\.\pipe\BEAMNGCOMMANDLISTENER" if IS_WIN else "/tmp/BEAMNGCOMMANDLISTENER"
RELOAD_COMMAND = b"v1/core_input_actions/triggerDownUp/lua_reload"
RELOADED_FILES = {
    "scripts/beamng_earth_reload/modScript.lua": """\
-- Written by the BeamNG.Earth app, which asked the running game to reload its Lua (to mount the mod): loads
-- beamngEarthReloaded once after that reload (it removes both files).
setExtensionUnloadMode('beamngEarthReloaded', 'manual')
""",
    "lua/ge/extensions/beamngEarthReloaded.lua": """\
-- Written by the BeamNG.Earth app; runs once after the Lua reload it asked for (the game was already running when the
-- app started, so it had read its mod list without the mod). A freeroam level open through the reload is left behind
-- the main menu with freeroam's state gone: it is started again. Then this file and its modScript are removed.
local M = {}
local frames, since, missionId, nudged, inAt, hides = 0, nil, nil, false, nil, 0
local HIDE_AT = { 3, 8, 15, 25, 40 }      -- s after the level is in

local function done()
  FS:removeFile('/lua/ge/extensions/beamngEarthReloaded.lua')
  FS:removeFile('/scripts/beamng_earth_reload/modScript.lua')
  FS:directoryRemove('/scripts/beamng_earth_reload')
  extensions.unload('beamngEarthReloaded')
end

function M.onUpdate(dtReal)
  frames = frames + 1
  if since then
    -- the restart: the UI is itself starting again after the reload and can miss the loading screen's messages
    since = since + math.min(dtReal or 0, 0.25)
    if not nudged and since > 2 and scenetree.MissionGroup and scenetree.MissionGroup:getId() == missionId then
      nudged = true                       -- the old level still there: the UI never confirmed the loading screen
      core_gamestate.loadingScreenActive()
    end
    if freeroam_freeroam.state.freeroamActive and not core_gamestate.loading() then
      -- the level in: its loading screen hidden again, a few times (a UI still starting misses the hides before it is
      -- up, ~10 s on a Steam Deck; one more when it is already hidden changes nothing)
      inAt = inAt or since
      if hides < #HIDE_AT and since - inAt > HIDE_AT[hides + 1] then
        hides = hides + 1
        guihooks.trigger('LoadingScreen', { active = false, gotoMainMenu = false, gameState = core_gamestate.state })
        if hides == #HIDE_AT then done() end
      end
    end
    if since > 180 then done() end
    return
  end
  if frames < 10 or not (core_modmanager and core_modmanager.isReady()) then return end
  local mission = getMissionFilename() or ''
  local level = mission:match('^/?(levels/[^/]+/)')
  local freeroamOn = freeroam_freeroam and freeroam_freeroam.state and freeroam_freeroam.state.freeroamActive
  local career = career_career and career_career.isActive and career_career.isActive()
  log('I', 'beamngEarth', 'Lua reloaded by the BeamNG.Earth app to mount the mod (level: ' .. (level or 'none') .. ')')
  if level and scenetree.MissionGroup and not freeroamOn and not career and freeroam_freeroam then
    missionId = scenetree.MissionGroup:getId()
    since = 0
    freeroam_freeroam.startFreeroam(level)
    return
  end
  done()
end

return M
""",
}


def _reload_files(user_dir):
    return [os.path.join(user_dir, *rel.split("/")) for rel in RELOADED_FILES]


def clear_reload_files(user_dir):
    """The one-shot reload script removed (left over from a reload that did not happen)."""
    for path in _reload_files(user_dir):
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        os.rmdir(os.path.dirname(_reload_files(user_dir)[0]))
    except OSError:
        pass


def send_game_command(command):
    """A beamng: command (without 'beamng:') to the running game through its pipe. False if no game takes it (none
    running; the Windows build under Proton, whose pipe is inside Wine; a sandbox with its own /tmp)."""
    try:
        if IS_WIN:
            with open(GAME_PIPE, "wb", buffering=0) as f:
                f.write(command)
            return True
        # (non-blocking: a pipe left by a game that is gone has no reader, and the open fails instead of waiting)
        fd = os.open(GAME_PIPE, os.O_WRONLY | os.O_NONBLOCK)
        try:
            return os.write(fd, command) == len(command)
        finally:
            os.close(fd)
    except OSError:
        return False


def reload_running_game(user_dir):
    """The running game's Lua reloaded, so it mounts the mod as it is now; an open freeroam level is started again.
    False if the game could not be reached."""
    for path, text in zip(_reload_files(user_dir), RELOADED_FILES.values()):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    if send_game_command(RELOAD_COMMAND):
        return True
    clear_reload_files(user_dir)
    return False


# --- the server's Python environment ------------------------------------------------------------

def check_python():
    """The embedded Python (python.exe, pythonw.exe) must exist; says which one is missing."""
    if not IS_WIN:
        return
    missing = [p for p in (PYTHON_EXE, PYTHONW_EXE) if not os.path.isfile(p)]
    if missing:
        fail("Python not found where expected:\n  " + "\n  ".join(missing) +
             "\n  Edit PY_DIR in install.py, or set the BNGE_PYTHON_DIR environment variable.")


def venv_python():
    """The interpreter the server runs with: the fixed Python on Windows (no virtual environment)."""
    return PYTHON_EXE if IS_WIN else os.path.join(VENV, "bin", "python")


def venv_pythonw():
    """Same, without a console window (pythonw.exe); python.exe elsewhere."""
    return PYTHONW_EXE if IS_WIN else venv_python()


def pip_cmd():
    return [venv_python(), "-m", "pip"]


def setup_venv(dry_run):
    py = venv_python()
    if dry_run:
        say(f"(dry run) would pip-install {os.path.relpath(REQUIREMENTS, ROOT)} with {py} -m pip")
        return
    check_python()
    if not IS_WIN and not os.path.isfile(py):
        say(f"Python packages: creating a virtual environment in {VENV} ...")
        r = subprocess.run([sys.executable, "-m", "venv", VENV])
        if r.returncode != 0 or not os.path.isfile(py):
            hint = ("On Debian/Ubuntu: sudo apt install python3-venv" if not IS_WIN else
                    "Reinstall Python from python.org (keep the default options, tick 'Add python.exe to PATH').")
            fail("could not create the virtual environment. " + hint)
    say("Python packages: installing the server's requirements (numpy, Pillow, duckdb, fsspec) ...")
    cmd = pip_cmd() + ["install", "--disable-pip-version-check"]
    if IS_WIN:
        # Without a matching wheel (e.g. a newer Python), pip tries to compile DuckDB and fails without MSVC.
        cmd.append("--only-binary=duckdb")
    r = subprocess.run(cmd + ["-r", REQUIREMENTS])
    if r.returncode != 0:
        hint = "  Check your internet connection (and proxy/firewall), then run install.py again."
        if IS_WIN:
            hint = ("  For connection errors, check your internet connection (and proxy/firewall), then retry."
                    "\n  If pip reports 'No matching distribution found' for duckdb, the bundled Python version"
                    "\n  may not have a compatible prebuilt package for this Windows version/architecture."
                    "\n  Check the embedded Python in the 'python' folder and use a compatible package/Python build."
                    "\n  The mod is installed already; only the server's packages are missing.")
        fail("pip could not install the packages (see the messages above).\n"
             + hint + "\n"
             "  The mod is installed already; only the server's packages are missing.")
    r = subprocess.run([py, "-c", "import numpy, PIL; print('numpy', numpy.__version__, '/ Pillow', PIL.__version__)"],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        fail("the packages were installed but do not import:\n" + r.stderr)
    say("Python packages: ready (" + r.stdout.strip() + ")")
    with open(REQ_STAMP, "w", encoding="utf-8") as f:     # start.py updates the packages when the list changes
        f.write(requirements_hash())


def requirements_hash():
    with open(REQUIREMENTS, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def save_config(user_dir, dry_run):
    cfg = {"user_dir": user_dir, "version": version()}
    if dry_run:
        say(f"(dry run) would write {CONFIG}: {cfg}")
        return
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main():
    ap = argparse.ArgumentParser(description=f"Install BeamNG.Earth {version()}: the mod into BeamNG.drive and "
                                             "the companion server's packages into bundled Python (Windows) or .venv (Linux).")
    ap.add_argument("--user-dir", help="BeamNG.drive user folder (default: found automatically)")
    ap.add_argument("--yes", "-y", action="store_true", help="answer yes to every question (replace an older mod copy, "
                                                             "pick the first user folder found)")
    ap.add_argument("--no-venv", action="store_true", help="only install the mod; skip Python package installation")
    ap.add_argument("--dry-run", action="store_true", help="show what would be done without changing anything")
    args = ap.parse_args()

    if sys.version_info < MIN_PY:
        fail(f"Python {MIN_PY[0]}.{MIN_PY[1]} or newer is needed (this is {sys.version.split()[0]}). "
             "Get it from https://www.python.org/downloads/")
    if not args.no_venv:
        check_python()
    say(f"BeamNG.Earth {version()} installer")
    say("")
    user_dir = choose_user_dir(args)
    try:
        install_mod(user_dir, args.yes, args.dry_run)
    except OSError as e:
        fail(f"could not copy the mod: {e}\n  Close BeamNG.drive if it is running and try again.")
    if args.no_venv:
        say("Python packages: skipped (--no-venv)")
    else:
        setup_venv(args.dry_run)
    save_config(user_dir, args.dry_run)

    launcher = "run_server.bat (double-click it)" if IS_WIN else "./run_server.sh"
    say("")
    say("Done. Next:")
    say(f"  1. Start the world server: {launcher}   (or: python start.py)")
    say("     Leave its window open while you play.")
    say("  2. Start BeamNG.drive, open Freeroam and pick the 'BeamNG.Earth' level.")
    say("  More help: docs/QUICKSTART.md")


if __name__ == "__main__":
    main()
