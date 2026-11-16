"""Which BeamNG.drive copy the app starts, and how (the user's go, 2026-11-15; research:
.claude/work/codex-launch-without-steam-2026-11-15.md; the direct Windows command from PR #1309 by ProxyFirewall202).

A copy (Install) is its folder, the store it came from ("steam", "epic", "other") and where it was found. The launcher
looks in this order: --game-dir, the saved choice (app.json gameDir, checked again at every start), BeamNG's own INI
and registry key (Windows: %LOCALAPPDATA%\\BeamNG\\BeamNG.drive.ini installPath, HKCU\\Software\\BeamNG\\BeamNG.drive
rootpath), Steam's libraries, the Epic Games Launcher's manifests (Windows: %ProgramData%\\Epic\\EpicGamesLauncher\\
Data\\Manifests\\*.item), then it asks the player for the folder. Several copies: the player picks one (asked once,
kept); no copy is preferred silently. The store comes from what the copy is, not from where it was found: a folder in a
Steam library is Steam's, one an Epic manifest installed is Epic's (an INI path pointing at an Epic copy too).

The launch mode (one setting: app.json launchMode, apppaths.saved_launch_mode; --launch MODE sets it):
  steam   through Steam, as before (Steam's overlay, Steam Input, play time)
  direct  the game's own program, -nosteam -userpath <the user folder>: no Steam client needed (Windows:
          <root>\\Bin64\\BeamNG.drive.x64.exe, which skips BeamNG's launcher window; Linux: <root>/BinLinux/
          BeamNG.drive.x64)
  auto    (unset) a Steam copy through Steam, an Epic copy directly, a copy in a folder of the player's own as before
          (its BeamNG.drive.exe / BinLinux program in its folder: mode "folder")
The resolved record (Launch) is made once, before anything starts; start_game follows it.

Pure functions with their inputs passed in (the registry, the INI, folders), so the tests need no Windows or Steam."""
import glob
import json
import os
from typing import NamedTuple, Optional

STORE_NAME = {"steam": "Steam", "epic": "Epic Games", "other": "a folder of your own"}
MODES = ("steam", "direct")                 # the setting's values (unset: "auto")
WIN_LAUNCHER = "BeamNG.drive.exe"           # BeamNG's launcher window (renderer, safe mode), at the copy's root
WIN_GAME = os.path.join("Bin64", "BeamNG.drive.x64.exe")       # the game itself
LINUX_GAME = os.path.join("BinLinux", "BeamNG.drive.x64")
MANIFEST_MAX = 1 << 20                      # an Epic manifest is a few KB: a bigger file is not read


class Install(NamedTuple):
    root: str
    store: str                  # "steam" | "epic" | "other"
    source: str                 # "saved" | "ini" | "registry" | "steam" | "epic" | "manual"

    def label(self):
        return f"{self.root}  ({STORE_NAME.get(self.store, self.store)})"


class Launch(NamedTuple):
    install: Optional[Install]  # None: not found (through Steam, as before)
    build: str                  # "windows" | "native" | "proton" (Linux: which build of the game)
    user_base: Optional[str]    # the folder -userpath names (the game puts its "current" in it)
    chosen: str                 # the setting: "auto" | "steam" | "direct"
    mode: str                   # what is done: "steam" | "direct" | "folder"
    exe: Optional[str]          # the program a direct / folder start runs
    note: str = ""              # why the mode differs from the setting (said once at the start)


def user_base(user_dir):
    """The folder -userpath names for the user folder user_dir: the game puts its version folder ("current", or an old
    numbered one) in it, so a version folder gives its parent; a player's own folder without that naming as it is."""
    if not user_dir:
        return None
    d = os.path.normpath(user_dir)
    base = os.path.basename(d)
    return os.path.dirname(d) if base == "current" or base.replace(".", "").isdigit() else d


def valid_root(root, windows):
    """root holds the game: the Windows build's BeamNG.drive.exe, or the native Linux program."""
    if not isinstance(root, str) or not root:
        return False
    return os.path.isfile(os.path.join(root, WIN_LAUNCHER if windows else LINUX_GAME))


def key(path):
    """One spelling per folder (case and separators on Windows, links resolved)."""
    return os.path.normcase(os.path.realpath(os.path.normpath(path)))


def in_steam_library(path):
    parts = [p.lower() for p in os.path.normpath(path).replace("\\", "/").split("/")]
    return "steamapps" in parts


# --- Windows: BeamNG's own records ---------------------------------------------------------------------------------

def registry_roots(values):
    """Folders from the values of HKCU\\Software\\BeamNG\\BeamNG.drive ([(name, value)]): rootpath first (what
    BeamMP's launcher reads), then any other text value (what this launcher read before)."""
    vals = [(str(n or ""), v) for n, v in values if isinstance(v, str) and v]
    first = [v for n, v in vals if n.lower() == "rootpath"]
    return first + [v for n, v in vals if n.lower() != "rootpath"]


def read_registry_values(path=r"Software\BeamNG\BeamNG.drive"):
    """[(name, value)] under HKEY_CURRENT_USER\\path ([] off Windows or without the key)."""
    try:
        import winreg
    except ImportError:
        return []
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            i = 0
            while True:
                try:
                    name, v, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                out.append((name, v))
                i += 1
    except OSError:
        pass
    return out


def read_steam_path():
    """Steam's folder from HKCU\\Software\\Valve\\Steam SteamPath, or None."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            v = winreg.QueryValueEx(k, "SteamPath")[0]
            return v if isinstance(v, str) and v else None
    except (ImportError, OSError):
        return None


def steam_libraries(steam_path):
    """Steam's library folders: its own folder and those in steamapps/libraryfolders.vdf."""
    import re
    if not steam_path:
        return []
    libs = [steam_path]
    try:
        with open(os.path.join(steam_path, "steamapps", "libraryfolders.vdf"), encoding="utf-8",
                  errors="replace") as f:
            libs += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
    except OSError:
        pass
    return libs


def steam_game_dirs(libraries):
    return [os.path.join(lib, "steamapps", "common", "BeamNG.drive") for lib in libraries]


# --- Windows: the Epic Games Launcher ------------------------------------------------------------------------------

def epic_manifest_dir(environ=None):
    """%ProgramData%\\Epic\\EpicGamesLauncher\\Data\\Manifests (from the variable, not a C: drive), or None."""
    environ = os.environ if environ is None else environ
    base = environ.get("ProgramData") or environ.get("PROGRAMDATA") or environ.get("ALLUSERSPROFILE")
    return os.path.join(base, "Epic", "EpicGamesLauncher", "Data", "Manifests") if base else None


def _is_beamng(manifest):
    """An Epic manifest of BeamNG.drive: by its application's names and its program, never by its folder's name."""
    for k in ("DisplayName", "AppName", "MainGameAppName", "LaunchExecutable"):
        v = manifest.get(k)
        if isinstance(v, str) and "beamng" in v.lower():
            return True
    return False


def epic_installs(manifest_dir):
    """The BeamNG.drive folders the Epic Games Launcher installed (its *.item manifests: JSON with InstallLocation),
    complete ones whose files are there, in file name order."""
    if not manifest_dir or not os.path.isdir(manifest_dir):
        return []
    out = []
    for p in sorted(glob.glob(os.path.join(manifest_dir, "*.item"))):
        try:
            if os.path.getsize(p) > MANIFEST_MAX:
                continue
            with open(p, "rb") as f:
                m = json.loads(f.read().decode("utf-8-sig", errors="replace"))
        except (OSError, ValueError, RecursionError):
            # (RecursionError: deeply nested JSON under MANIFEST_MAX; one bad manifest of another game must not stop
            # the start of any copy, Codex review of ee11b37e)
            continue
        if not isinstance(m, dict) or m.get("bIsIncompleteInstall") is True or not _is_beamng(m):
            continue
        root = m.get("InstallLocation")
        if isinstance(root, str) and valid_root(root, True):
            out.append(os.path.normpath(root))
    return out


# --- every copy ------------------------------------------------------------------------------------------------------

def collect(found, windows, steam_dirs=(), epic_dirs=()):
    """found: [(root, source)] in the search order -> [Install] of the valid ones, each folder once (its first
    source), with its store."""
    steam_keys = {key(d) for d in steam_dirs}
    epic_keys = {key(d) for d in epic_dirs}
    out, seen = [], set()
    for root, source in found:
        if not valid_root(root, windows):
            continue
        k = key(root)
        if k in seen:
            continue
        seen.add(k)
        store = "epic" if k in epic_keys else "steam" if (k in steam_keys or in_steam_library(root)) else "other"
        out.append(Install(os.path.normpath(root), store, source))
    return out


def find_windows(saved=None, ini=None, registry=(), steam_path=None, manifest_dir=None):
    """Every BeamNG.drive copy on Windows, in the search order: saved (the player's choice), BeamNG's INI installPath,
    its registry key, Steam's libraries, Epic's manifests."""
    steam_dirs = steam_game_dirs(steam_libraries(steam_path))
    epic_dirs = epic_installs(manifest_dir)
    found = []
    if saved:
        found.append((saved, "saved"))
    if ini and ini.get("installPath"):
        found.append((ini["installPath"], "ini"))
    found += [(r, "registry") for r in registry_roots(registry)]
    found += [(d, "steam") for d in steam_dirs]
    found += [(d, "epic") for d in epic_dirs]
    return collect(found, True, steam_dirs, epic_dirs)


def find_linux(saved=None, steam_roots=()):
    """Every native BeamNG.drive copy on Linux: saved, then Steam's libraries (install._steam_roots)."""
    steam_dirs = steam_game_dirs(steam_roots)
    found = ([(saved, "saved")] if saved else []) + [(d, "steam") for d in steam_dirs]
    return collect(found, False, steam_dirs)


# --- the launch -----------------------------------------------------------------------------------------------------

def resolve_mode(chosen, install, build, gamescope=False):
    """(mode, note): what is done for the setting chosen ("auto" | "steam" | "direct") with this copy."""
    store = install.store if install else None
    if build == "proton":
        # (the Windows build on Linux: only through Steam for now; a runner for an Epic / Heroic prefix is later work)
        return "steam", ("the Windows build on Linux starts through Steam (a start without Steam is for the native "
                         "build)" if chosen == "direct" else "")
    if chosen == "direct" and gamescope:
        # (Game Mode shows only the window Steam started or tagged: as before)
        return resolve_mode("auto", install, build)[0], "Steam Deck Game Mode: the game starts as before (the start " \
                                                        "without Steam applies on the desktop)"
    if chosen == "direct":
        # (no copy: still direct, and direct_start stops with the missing-copy message; never through Steam, which
        # would start Steam or another copy against the player's choice, Codex review of ee11b37e)
        return ("direct", "") if install else ("direct", "BeamNG.drive's folder is not known: a start without Steam "
                                                         "needs it (--game-dir PATH, or --launch steam)")
    if chosen == "steam" and store in ("epic", "other"):
        mode = resolve_mode("auto", install, build)[0]
        return mode, f"this copy is not Steam's ({STORE_NAME[store]}): started {'directly' if mode == 'direct' else 'from its folder'}"
    if store == "epic":
        return "direct", ""
    if store == "other":
        return "folder", ""
    return "steam", ""


def launch_exe(install, mode, windows):
    """The program a direct (the game itself) or folder (Windows: BeamNG's launcher window) start runs, or None."""
    if not install or mode == "steam":
        return None
    if not windows:
        return os.path.join(install.root, LINUX_GAME)
    return os.path.join(install.root, WIN_GAME if mode == "direct" else WIN_LAUNCHER)


def direct_args(base_dir, game_args=()):
    """The game's options for a start without Steam: -nosteam, the user folder, then the rest (a renderer's -gfx
    only when asked for, tests' options, Steam's launch options' own)."""
    return ["-nosteam"] + (["-userpath", base_dir] if base_dir else []) + list(game_args or ())
