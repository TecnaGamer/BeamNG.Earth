"""BeamMP (multiplayer) started by the BeamNG.Earth app (the user, 2026-10-24: "bundle BeamMP into the earth
launcher", the official BeamMP, unmodified). The official BeamMP launcher is found, or on Windows downloaded from
BeamMP's GitHub releases and checked against the .sha256 published with it, then started with --no-launch before the
game: it installs or updates BeamMP's mod in the game's mods/multiplayer and waits for the game, which connects to it
by itself (port 4444). It is stopped when the game closes. BeamMP's code (AGPL-3.0) runs as BeamMP ships it; nothing of
it is changed or kept in this project. Linux: BeamMP publishes no Linux release, and BeamNG.Earth doesn't hand out
BeamMP's program (the user, 2026-10-24: "i dont want to redsribute their launcher. just have our launcher build it
itelf"): it is built on the player's computer from BeamMP's source (build_linux), at the newest commit of BeamMP's
master branch whose own Linux build passed ("only use working builds from main"), inside an Ubuntu 24.04 container
(podman or docker: no compiler needed; SteamOS ships podman). One built by the player is used when found
(BEAMMP_LAUNCHER, ~/BeamMP-Launcher, a BeamMP-Launcher on the PATH).
Whether BeamMP starts with BeamNG.Earth is the mod's setting beammpOnLaunch (the settings page; on_launch below).
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

IS_WIN = os.name == "nt"
CORE_PORT = 4444                # the BeamMP launcher's port for the game (its Launcher.cfg default)
READY_S = 120                   # its mod update (a few MB) and start, at most this long
RELEASES = "https://api.github.com/repos/BeamMP/BeamMP-Launcher/releases/latest"
# Linux: BeamMP's newest master commit whose Linux build passed (its GitHub Actions, readable without an account)
PASSING_RUNS = ("https://api.github.com/repos/BeamMP/BeamMP-Launcher/actions/workflows/cmake-linux.yml/runs"
                "?branch=master&event=push&status=success&per_page=1")
SOURCE_TGZ = "https://github.com/BeamMP/BeamMP-Launcher/archive/{sha}.tar.gz"
BUILD_IMAGE = "docker.io/library/ubuntu:24.04"
HTTPLIB_H = "https://raw.githubusercontent.com/yhirose/cpp-httplib/v0.14.3/httplib.h"   # (as BeamMP's vcpkg one)
# In the container: Ubuntu's build tools and libraries, httplib as its single header (Ubuntu's is a separate shared
# library a player's system wouldn't have; BeamMP's CMakeLists only needs it found), BeamMP's own CMake build.
BUILD_SCRIPT = (
    "set -e; apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "
    "g++ make cmake curl ca-certificates libcurl4-openssl-dev libssl-dev zlib1g-dev nlohmann-json3-dev >/dev/null; "
    "mkdir -p /tmp/hl/inc; curl -sSfL " + HTTPLIB_H + " -o /tmp/hl/inc/httplib.h; "
    "echo 'add_library(httplib::httplib INTERFACE IMPORTED)' > /tmp/hl/httplib-config.cmake; "
    "cmake -S /src -B /tmp/b -DCMAKE_BUILD_TYPE=Release -Dhttplib_DIR=/tmp/hl -DCMAKE_CXX_FLAGS=-I/tmp/hl/inc >/dev/null; "
    "cmake --build /tmp/b -j\"$(nproc)\" >/dev/null; cp /tmp/b/BeamMP-Launcher /out/BeamMP-Launcher")
EXE = "BeamMP-Launcher.exe" if IS_WIN else "BeamMP-Launcher"
SETTINGS = "beammp.json"        # in the app's data folder: {"declined": true} once the player said no


def _settings(data_dir):
    try:
        with open(os.path.join(data_dir, SETTINGS), encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data_dir, **kw):
    d = _settings(data_dir)
    d.update(kw)
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, SETTINGS), "w", encoding="utf-8") as f:
        json.dump(d, f)


def rebuild_due(data_dir):
    """Linux: the launcher built here was too old for BeamMP's current mod ("Invalid lua communication"): build it again
    at the next start."""
    return bool(_settings(data_dir).get("rebuild"))


def declined(data_dir):
    return bool(_settings(data_dir).get("declined"))


# The mod's settings file in the game's user folder (settings.lua M.FILE): beammpOnLaunch, the settings page's "Start
# BeamMP with BeamNG.Earth" (default on). "No, and don't ask again" at the install question turns it off; turning it on
# again in the game asks again at the next start.
MOD_SETTINGS = os.path.join("settings", "beamngEarth.json")
ON_LAUNCH = "beammpOnLaunch"


def _mod_settings(user_dir):
    try:
        with open(os.path.join(user_dir, MOD_SETTINGS), encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def on_launch(user_dir, data_dir=None):
    """Start BeamMP with the game? The mod's setting; an older app's "never ask" (beammp.json declined) counts as off
    until the setting is written."""
    v = _mod_settings(user_dir).get(ON_LAUNCH)
    if isinstance(v, bool):
        return v
    return not (data_dir and declined(data_dir))


def set_on_launch(user_dir, value):
    d = _mod_settings(user_dir)
    d[ON_LAUNCH] = bool(value)
    path = os.path.join(user_dir, MOD_SETTINGS)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def candidates(data_dir, env=None, home=None):
    """Where a BeamMP launcher may be, in order: BEAMMP_LAUNCHER, BeamMP's own installer's folder (Windows), ours,
    a build from source in the home folder, the PATH."""
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    out = []
    if env.get("BEAMMP_LAUNCHER"):
        out.append(env["BEAMMP_LAUNCHER"])
    if IS_WIN:
        appdata = env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        out.append(os.path.join(appdata, "BeamMP-Launcher", EXE))
    out.append(os.path.join(data_dir, "beammp", EXE))
    if not IS_WIN:
        out += [os.path.join(home, "BeamMP-Launcher", "bin", EXE), os.path.join(home, EXE),
                os.path.join(home, ".local", "bin", EXE)]
        # (BeamMP-Launcher or BeamMP-Launcher-<version> only: a folder renamed aside, BeamMP-Launcher.bak-..., was
        # picked up, 2026-10-24)
        out += sorted(p for p in (os.path.join(home, d, "bin", EXE) for d in _listdir(home)
                                  if re.fullmatch(r"BeamMP-Launcher(-[0-9][0-9.]*)?", d)) if p not in out)
    on_path = shutil.which(EXE)
    if on_path:
        out.append(on_path)
    return out


def _listdir(d):
    try:
        return os.listdir(d)
    except OSError:
        return []


def find(data_dir, env=None):
    """The launcher to start: BEAMMP_LAUNCHER when set, else the newest one found (by file time: an old build next to a
    new one cannot talk to BeamMP's current mod, which every launcher downloads; a v2.7.0 left in ~/BeamMP-Launcher was
    picked over a v2.8.1 build, 2026-10-24, and the game reconnected every 2 s: "Invalid lua communication")."""
    env = os.environ if env is None else env
    found = [p for p in candidates(data_dir, env=env) if os.path.isfile(p) and (IS_WIN or os.access(p, os.X_OK))]
    if not found:
        return None
    if env.get("BEAMMP_LAUNCHER") and found[0] == env["BEAMMP_LAUNCHER"]:
        return found[0]
    return max(found, key=lambda p: os.path.getmtime(p))


VERSION_RE = re.compile(r"BeamMP Launcher v([0-9][0-9.]*)")
TOO_OLD = "Invalid lua communication"     # its log, again and again: the launcher cannot talk to BeamMP's mod


def read_log(log_path):
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def log_version(text):
    """The launcher's version from its log ("BeamMP Launcher v2.8.1"), or None."""
    m = VERSION_RE.search(text or "")
    return m.group(1) if m else None


def log_too_old(text, at_least=3):
    """Its log says it cannot talk to the game's BeamMP mod (a launcher older than the mod it downloaded)."""
    return (text or "").count(TOO_OLD) >= at_least


def running(port=CORE_PORT):
    """A BeamMP launcher already listens (started by the player, or left over)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _get(url, user_agent, timeout=60):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/octet-stream, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def pick_assets(release, name="BeamMP-Launcher.exe"):
    """(file url, sha256 url, version) of asset name (and name.sha256) in a GitHub release's JSON, or None."""
    urls = {a.get("name"): a.get("browser_download_url") for a in (release or {}).get("assets") or []}
    exe, sha = urls.get(name), urls.get(name + ".sha256")
    if not exe or not sha:
        return None
    return exe, sha, str(release.get("tag_name") or "")


def parse_sha256(text):
    """The hex digest in a .sha256 file ("<hex>" or "<hex>  name")."""
    for tok in text.replace("*", " ").split():
        t = tok.strip().lower()
        if len(t) == 64 and all(c in "0123456789abcdef" for c in t):
            return t
    return None


def download(data_dir, user_agent, say=print):
    """Windows: the latest official BeamMP launcher into <data>/beammp, checked against its published SHA-256.
    Returns its path, or None (no network, a bad checksum: nothing is kept)."""
    try:
        release = json.loads(_get(RELEASES, user_agent, 30).decode("utf-8"))
        picked = pick_assets(release)
        if not picked:
            say("BeamMP: no launcher download in its latest release")
            return None
        exe_url, sha_url, version = picked
        want = parse_sha256(_get(sha_url, user_agent, 30).decode("utf-8", "replace"))
        blob = _get(exe_url, user_agent, 300)
    except Exception as e:                  # (no network, GitHub's rate limit...)
        say(f"BeamMP: the download failed ({e})")
        return None
    got = hashlib.sha256(blob).hexdigest()
    if not want or got != want:
        say(f"BeamMP: the download's checksum does not match the one BeamMP published ({got} != {want}); not used")
        return None
    dest_dir = os.path.join(data_dir, "beammp")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, EXE)
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, dest)
    say(f"BeamMP: launcher {version} installed ({max(1, len(blob) // 1048576)} MB, checksum verified)")
    return dest


def game_user_path(user_dir):
    """The user folder BeamMP wants (the folder above the game version's, as --user-path)."""
    d = os.path.normpath(user_dir)
    base = os.path.basename(d)
    if base == "current" or base[:1].isdigit():
        return os.path.dirname(d)
    return d


def child_env(env=None, frozen=None):
    """The environment for programs started from here (BeamMP's launcher, podman): without the packaged app's own
    library path. In the AppImage, BeamMP's launcher found the app's older libstdc++ before the system's and refused to
    start ("GLIBCXX_3.4.32 missing" on a system that has it, 2026-10-24). PyInstaller keeps the original path in
    LD_LIBRARY_PATH_ORIG."""
    env = dict(os.environ if env is None else env)
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if frozen:
        if orig is not None:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    for k in ("APPDIR", "APPIMAGE", "ARGV0", "OWD"):        # (the AppImage runtime's own)
        env.pop(k, None)
    return env


def container_tool(which=None):
    """podman, else docker (a path), or None: what build_linux builds in."""
    which = which or shutil.which
    return which("podman") or which("docker")


def passing_commit(user_agent):
    """The newest commit of BeamMP's launcher on master whose Linux build passed (its GitHub Actions)."""
    runs = json.loads(_get(PASSING_RUNS, user_agent, 30).decode("utf-8")).get("workflow_runs") or []
    sha = runs[0].get("head_sha") if runs else None
    return sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def build_linux(data_dir, user_agent, say=print, run=None, tool=None):
    """Linux: BeamMP's launcher built here from its source (see the top). Returns its path, or None (no container
    tool, no network, a failed build: what is there is kept). Takes a few minutes the first time (the container image
    and Ubuntu's packages), about a minute after."""
    import tarfile
    import tempfile
    run = run or subprocess.run
    tool = tool or container_tool()
    if not tool:
        say("BeamMP: building its launcher needs podman or docker (SteamOS has podman; on other systems install one, "
            "or build it yourself: github.com/BeamMP/BeamMP-Launcher)")
        return None
    try:
        sha = passing_commit(user_agent)
        if not sha:
            say("BeamMP: no passing build of its launcher found on GitHub")
            return None
        say(f"BeamMP: building its launcher from its source (commit {sha[:8]}): a few minutes the first time ...")
        blob = _get(SOURCE_TGZ.format(sha=sha), user_agent, 300)
    except Exception as e:                  # (no network, GitHub's rate limit...)
        say(f"BeamMP: the source download failed ({e})")
        return None
    dest_dir = os.path.join(data_dir, "beammp")
    os.makedirs(dest_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest_dir) as work:
        src = os.path.join(work, "src")
        out = os.path.join(work, "out")
        os.makedirs(out)
        with tarfile.open(fileobj=__import__("io").BytesIO(blob), mode="r:gz") as tf:
            try:
                tf.extractall(work, filter="data")      # (no paths out of the folder)
            except TypeError:                           # (a Python without extraction filters)
                tf.extractall(work)
        top = [d for d in os.listdir(work) if d.startswith("BeamMP-Launcher-")]
        if not top:
            say("BeamMP: the source archive is not what was expected")
            return None
        os.rename(os.path.join(work, top[0]), src)
        cmd = [tool, "run", "--rm", "-v", f"{src}:/src:ro,Z", "-v", f"{out}:/out:Z", BUILD_IMAGE, "bash", "-c",
               BUILD_SCRIPT]
        try:
            r = run(cmd, capture_output=True, text=True, timeout=1800, env=child_env())
        except (OSError, subprocess.SubprocessError) as e:
            say(f"BeamMP: the build did not run ({e})")
            return None
        built = os.path.join(out, "BeamMP-Launcher")
        if r.returncode != 0 or not os.path.isfile(built):
            tail = " ".join(((r.stderr or "") + (r.stdout or "")).strip().splitlines()[-3:])
            say(f"BeamMP: the build failed ({tail[-300:]})")
            return None
        dest = os.path.join(dest_dir, EXE)
        shutil.copyfile(built, dest + ".part")
        os.chmod(dest + ".part", 0o755)
        os.replace(dest + ".part", dest)
    save_settings(data_dir, builtFrom=sha, rebuild=False)
    say(f"BeamMP: launcher built from BeamMP's source, commit {sha[:8]}")
    return dest


def system_too_old(exe, run=None):
    """Linux: why this system can't run the launcher (BeamMP's build needs glibc 2.38 and GCC 13's libstdc++, Ubuntu
    24.04 or newer, SteamOS 3.6 or newer), or None. Asks the system's loader, by starting it with --help."""
    if IS_WIN:
        return None
    run = run or subprocess.run
    try:
        r = run([exe, "--help"], capture_output=True, text=True, timeout=10, env=child_env())
    except (OSError, subprocess.SubprocessError):
        return None                     # (it started: a slow --help is not the loader's refusal)
    err = (r.stderr or "") + (r.stdout or "")
    m = re.search(r"version `(GLIBC(?:XX)?_[0-9.]+)' not found", err)
    return f"this system's libraries are too old for it ({m.group(1)} missing)" if m else None


def start(exe, user_dir, log_path):
    """The BeamMP launcher, without starting the game itself (the app does), its output to log_path."""
    args = [exe, "--no-launch"]
    if not IS_WIN:
        # (a build from source: its self-update would fetch the Windows launcher; the game's folder named, as BeamMP
        # cannot find a Linux one by itself)
        args += ["--no-update", "--user-path", game_user_path(user_dir)]
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "w", encoding="utf-8", errors="replace")
    kw = {"cwd": os.path.dirname(exe), "stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if IS_WIN:
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    else:
        kw["start_new_session"] = True
        kw["env"] = dict(child_env(), TERM=os.environ.get("TERM") or "xterm")
    return subprocess.Popen(args, **kw)


def wait_ready(proc, timeout=READY_S):
    """Until it listens for the game (its mod updated); False if it ended or took too long."""
    end = time.time() + timeout
    while time.time() < end:
        if running():
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def stop(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


if __name__ == "__main__":                  # (a check by hand: where a launcher is found)
    dd = sys.argv[1] if len(sys.argv) > 1 else "."
    print(find(dd), "running" if running() else "not running")
