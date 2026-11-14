"""Where the app's folders and programs are, chosen by the player (the user, 2026-10-25: "with beamMP if beamNG is
installed in a diffrent place or diffrent launcher let the player set the location. and for setting location for the
beamng, beammp, or the cache. Open the explorer for that operating system but have a text input for the path as
fallback"): the game's folder, the download cache's folder (GitHub #254) and the BeamMP launcher (bmplaunch.remember).

The launcher's remembered choices are app.json in data_dir() ({"cacheDir": ..., "gameDir": ...}); the launcher reads
them at its start, the world server writes them from the settings page (they apply at the next start). pick() opens
the system's own file or folder window: PowerShell's dialogs on Windows, kdialog or zenity on Linux; ("none", None) when
there is none (Wine, Steam's Game Mode, no desktop), and the caller asks for the path as text (ask_path in a console,
the settings page's text box in the game). Beside launcher.py (the server imports it from one folder up)."""
import base64
import json
import os
import shutil
import subprocess
import sys

IS_WIN = os.name == "nt"
APP_SETTINGS = "app.json"       # in data_dir(): the launcher's own remembered choices
PICK_S = 900                    # a file window left open this long: given up (the caller asks for text)
GAME_WIN = "BeamNG.drive.exe"
GAME_LINUX = os.path.join("BinLinux", "BeamNG.drive.x64")


def data_dir():
    """Where the launcher keeps its files (logs, the gcpatch copy, app.json): the server's cache folder's parent."""
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "beamng-earth")
    return os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth")


def default_cache_dir():
    """The server's download cache without a choice (earthd --cache-dir's default)."""
    return os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth")


def app_settings(dd=None):
    try:
        with open(os.path.join(dd or data_dir(), APP_SETTINGS), encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_app_settings(dd=None, **kw):
    dd = dd or data_dir()
    d = app_settings(dd)
    d.update(kw)
    d = {k: v for k, v in d.items() if v is not None}
    os.makedirs(dd, exist_ok=True)
    with open(os.path.join(dd, APP_SETTINGS), "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def drive_relative(path):
    """Is path a Windows drive-relative path ("c:games\\x", "c:games"): a drive letter with no separator after it?
    That is what c:\\games\\beamng-cache becomes when a shell drops its backslashes (Git Bash, MSYS, unquoted), and
    Windows takes it as relative to that drive's current folder: GitHub #895, the cache went to the home folder."""
    p = (path or "").strip().strip('"').strip("'").strip()
    return len(p) >= 2 and p[0].isalpha() and p[1] == ":" and (len(p) == 2 or p[2] not in "\\/")


def drive_relative_problem(path):
    """The player's message for a drive_relative path, or None."""
    if not drive_relative(path):
        return None
    p = (path or "").strip().strip('"').strip("'").strip()
    return (f"{p} has no \\ or / after the drive letter, so Windows would put it in the current folder (were the "
            f"backslashes removed by the terminal?). Write it with / (e.g. {p[:2]}/games/beamng-cache) or in quotes")


def clean(path):
    """A path as typed or pasted: quotes and spaces around it gone, ~ and variables expanded, absolute."""
    path = (path or "").strip().strip('"').strip("'").strip()
    return os.path.abspath(os.path.expandvars(os.path.expanduser(path))) if path else ""


# --- the game's user folder, as the game itself reported it -----------------------------------------

def saved_user_dir(dd=None):
    """The BeamNG user folder (its version folder, "current") the game said it uses (the mod's hello userPath, saved by
    the world server when it differed from the one it was given), or None."""
    p = app_settings(dd).get("gameUserDir")
    return p if isinstance(p, str) and p and os.path.isdir(p) else None


def set_user_dir(path, dd=None):
    save_app_settings(dd, gameUserDir=os.path.normpath(path) if path else None)


def same_dir(a, b):
    """Two folder paths naming the same folder (case and separators on Windows; links resolved)."""
    if not a or not b:
        return False
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


# --- the download cache ---------------------------------------------------------------------------

def saved_cache_dir(dd=None):
    p = app_settings(dd).get("cacheDir")
    return p if isinstance(p, str) and p else None


def set_cache_dir(path, dd=None):
    """Keep path as the download cache's folder for the next starts ("" or "default": the default folder again).
    Returns (the folder or None for the default, error text or None). The folder is made and must be writable; files
    already downloaded stay where they were (the player moves them over, or they are downloaded again)."""
    if (path or "").strip().strip('"').lower() in ("", "default"):
        save_app_settings(dd, cacheDir=None)
        return None, None
    bad = drive_relative_problem(path)
    if bad:
        return None, bad
    path = clean(path)
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".bnge-write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        return None, f"cannot use {path}: {e.strerror or e}"
    save_app_settings(dd, cacheDir=path)
    return path, None


# --- the game --------------------------------------------------------------------------------------

def game_folder(path):
    """The game's folder for path: the folder with BeamNG.drive.exe (Windows) or BinLinux/BeamNG.drive.x64 (Linux),
    given as that folder, a folder in it (Bin64, BinLinux) or the program itself; None when the game is not there."""
    path = clean(path)
    if not path:
        return None
    if os.path.isfile(path):
        path = os.path.dirname(path)
    exe = GAME_WIN if IS_WIN else GAME_LINUX
    for d in (path, os.path.dirname(path)):
        if os.path.isfile(os.path.join(d, exe)):
            return d
    return None


def saved_game_dir(dd=None):
    p = app_settings(dd).get("gameDir")
    return p if isinstance(p, str) and game_folder(p) else None


def set_game_dir(path, dd=None):
    """Keep the game's folder (a game outside Steam, or in a Steam library not found): returns it, or None when the
    game is not there. "default" forgets it (found as before)."""
    if (path or "").strip().strip('"').lower() == "default":
        save_app_settings(dd, gameDir=None)
        return None
    d = game_folder(path)
    if d:
        save_app_settings(dd, gameDir=d)
    return d


def in_steam_library(path):
    """Whether a game folder is a Steam library's (started through Steam as before) or one of the player's own."""
    parts = [p.lower() for p in os.path.normpath(path).split(os.sep)]
    return "steamapps" in parts


# --- the system's file window ----------------------------------------------------------------------

def _env():
    """The environment for a dialog program: without the packaged app's own libraries (bmplaunch.child_env: in the
    AppImage a system program found the app's older libraries first)."""
    try:
        import bmplaunch
        return bmplaunch.child_env()
    except Exception:
        return dict(os.environ)


def _linux_tool():
    """The dialog program to use on this desktop, or None (no desktop, Steam's Game Mode: no windows to show)."""
    env = os.environ
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return None
    if env.get("GAMESCOPE_WAYLAND_DISPLAY") or "gamescope" in env.get("XDG_CURRENT_DESKTOP", "").lower():
        return None
    kde = "kde" in env.get("XDG_CURRENT_DESKTOP", "").lower()
    order = ("kdialog", "zenity") if kde else ("zenity", "kdialog")
    for name in order:
        if shutil.which(name):
            return name
    return None


# Windows' own folder window (the Explorer-style one: IFileOpenDialog with FOS_PICKFOLDERS; WinForms'
# FolderBrowserDialog is the old tree, 2026-10-25), owned by a hidden topmost window so it opens in front
PS_FOLDER = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class BnePickFolder {
    [ComImport, Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")] class FileOpenDialog {}
    [ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IShellItem {
        void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
        void GetParent(out IShellItem ppsi);
        void GetDisplayName(uint sigdn, [MarshalAs(UnmanagedType.LPWStr)] out string name);
        void GetAttributes(uint mask, out uint attribs);
        void Compare(IShellItem psi, uint hint, out int order);
    }
    [ComImport, Guid("42f85136-db7e-439c-85f1-e4075d135fc8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IFileDialog {
        [PreserveSig] int Show(IntPtr parent);
        void SetFileTypes(uint count, IntPtr specs);
        void SetFileTypeIndex(uint index);
        void GetFileTypeIndex(out uint index);
        void Advise(IntPtr events, out uint cookie);
        void Unadvise(uint cookie);
        void SetOptions(uint fos);
        void GetOptions(out uint fos);
        void SetDefaultFolder(IShellItem psi);
        void SetFolder(IShellItem psi);
        void GetFolder(out IShellItem psi);
        void GetCurrentSelection(out IShellItem psi);
        void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string name);
        void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string name);
        void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string title);
        void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string text);
        void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetResult(out IShellItem psi);
    }
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHCreateItemFromParsingName(string path, IntPtr pbc, ref Guid riid, out IShellItem item);
    public static string Pick(IntPtr owner, string title, string start) {
        IFileDialog d = (IFileDialog)new FileOpenDialog();
        uint fos; d.GetOptions(out fos);
        d.SetOptions(fos | 0x20 | 0x40 | 0x800);       // FOS_PICKFOLDERS, FOS_FORCEFILESYSTEM, FOS_PATHMUSTEXIST
        if (!String.IsNullOrEmpty(title)) d.SetTitle(title);
        if (!String.IsNullOrEmpty(start)) {
            try { Guid g = typeof(IShellItem).GUID; IShellItem it; SHCreateItemFromParsingName(start, IntPtr.Zero, ref g, out it); d.SetFolder(it); } catch {}
        }
        if (d.Show(owner) != 0) return null;           // cancelled (HRESULT_FROM_WIN32(ERROR_CANCELLED))
        IShellItem r; d.GetResult(out r);
        string path; r.GetDisplayName(0x80058000, out path);      // SIGDN_FILESYSPATH
        return path;
    }
}
'@
$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true; ShowInTaskbar = $false}
$start = if ($env:BNGE_PICK_START -and (Test-Path -LiteralPath $env:BNGE_PICK_START)) { $env:BNGE_PICK_START } else { "" }
$path = [BnePickFolder]::Pick($owner.Handle, $env:BNGE_PICK_TITLE, $start)
if ($path) { [Console]::Out.Write($path) }
"""

PS_FILE = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true; ShowInTaskbar = $false}
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Title = $env:BNGE_PICK_TITLE
$d.Filter = $env:BNGE_PICK_FILTER
if ($env:BNGE_PICK_START -and (Test-Path -LiteralPath $env:BNGE_PICK_START)) { $d.InitialDirectory = $env:BNGE_PICK_START }
if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($d.FileName) }
"""


def pick_command(want, title, start, name=None, tool=None):
    """(argv, extra environment) for the system's window choosing a folder (want "folder") or a file (want "file",
    name: the program looked for), or None when there is none."""
    start = start if start and os.path.isdir(start) else os.path.expanduser("~")
    if IS_WIN:
        ps = shutil.which("powershell") or shutil.which("powershell.exe")
        if not ps:
            return None
        flt = (f"{name}|{name}|Programs (*.exe)|*.exe|All files|*.*" if name else "All files|*.*")
        script = PS_FOLDER if want == "folder" else PS_FILE
        enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")     # (no quoting through the command line)
        return ([ps, "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", enc],
                {"BNGE_PICK_TITLE": title, "BNGE_PICK_START": start, "BNGE_PICK_FILTER": flt})
    tool = tool or _linux_tool()
    if tool == "zenity":
        argv = ["zenity", "--file-selection", "--title", title, "--filename", start.rstrip("/") + "/"]
        return (argv + (["--directory"] if want == "folder" else []), {})
    if tool == "kdialog":
        flag = "--getexistingdirectory" if want == "folder" else "--getopenfilename"
        return ([tool, flag, start, "--title", title], {})
    return None


def pick(want, title, start=None, name=None, run=None):
    """The system's own window to choose a folder or a file: ("ok", path), ("cancel", None) or ("none", None) when
    there is no such window here (the caller asks for the path as text)."""
    if IS_WIN and os.environ.get("WINEPREFIX"):
        return "none", None
    cmd = pick_command(want, title, start, name)
    if not cmd:
        return "none", None
    argv, extra = cmd
    env = dict(_env(), **extra)
    run = run or subprocess.run
    kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if IS_WIN else {}
    try:
        r = run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=PICK_S, **kw)
    except (OSError, subprocess.SubprocessError):
        return "none", None
    out = (r.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    path = out[-1].strip() if out else ""
    if r.returncode == 0 and path:
        return "ok", path
    if r.returncode in (0, 1) or r.returncode < 0:      # cancelled (zenity / kdialog: 1; PowerShell: nothing
        return "cancel", None                           # written) or the window closed from outside
    return "none", None


def ask_path(question, want, accept, ask=None, say=print, name=None, start=None, tries=3):
    """In a console: the path of something, from the system's window (Enter) or typed / pasted. accept(path) gives the
    value to keep or None when it is not there; returns that value, or None (skipped: an empty answer with no window,
    "s", or tries wrong answers)."""
    ask = ask or input
    window = True
    for _ in range(tries):
        hint = "Enter: choose it in a window, or type / paste the path (S: skip): " if window else \
            "Type or paste the path (Enter: skip): "
        try:
            answer = ask(f"  {question}\n  {hint}")
        except (EOFError, OSError):
            return None
        answer = (answer or "").strip()
        if answer.lower() in ("s", "skip"):
            return None
        if not answer:
            if not window:
                return None
            state, path = pick(want, question, start=start, name=name)
            if state == "none":
                say("  (no file window here: type the path instead)")
                window = False
                continue
            if state == "cancel":
                say("  (nothing chosen)")
                continue
            answer = path
        value = accept(answer)
        if value:
            return value
        say(f"  Not found there: {answer}")
    return None
