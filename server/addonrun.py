"""Companion add-ons run beside the game (kind "companion": Live GPS), and what a game mod's code is (mod_code) so the
mod can load and unload it live (the user, 2026-11-07: "when a lua mod is added/removed make sure the game gets properly
reloaded so it applies"). docs/ADDONS.md.

A companion is one Python file of its author's release (or a local file: addons' local list), installed into
<data>/addons/<id>/ and run by BeamNG.Earth's own Python (the packaged app's: `BeamNG.Earth --run-addon FILE ARGS`, the
dev tree's: `python3 launcher.py --run-addon FILE ARGS`; launcher.run_addon), so a player needs no Python of their own.
It gets the companion feed's address (BNE_FEED, companionfeed.py: the car's position, read-only) and ends with the
server (BNGEARTH_PARENT: launcher.watch_parent), and every run's output goes to run.log beside it. Start / Stop from
the Add-ons page; "Start with BeamNG.Earth" (autostart) starts it when the server starts.
"""
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import zipfile

log = logging.getLogger("addons")

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.join(os.path.dirname(HERE), "launcher.py")
STATE = "installed.json"
LOG = "run.log"
LOG_MAX = 4 << 20           # a run's log is cut back to its last part past this many bytes (at the next start)
STOP_WAIT_S = 3.0           # Stop: this long after terminate() before the process is killed
_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,120}\.py")

# Top folders of lua/ge/extensions/ that are the game's own (a mod's file there replaces the game's: it is not
# unloaded live, the page asks for a restart). Used when the game's own folder is not known (BNGEARTH_GAME_DIR).
STOCK_TOP = {"core", "ui", "gameplay", "career", "editor", "util", "freeroam", "scenario", "campaign", "flowgraph",
             "render", "trackbuilder", "spawn", "test", "tech", "physicsBenchmark", "dynamicDecals", "map", "mp",
             "simTimeAuthority", "ge_utils", "extensions", "commands", "settings", "screenshot", "photoMode",
             "quickrace", "bigMap", "missions", "hints", "levels", "multiplayer"}
CODE_MAX = 6000             # names sent at most (the Trucking mod has ~910)


def mod_code(path, game_dir=None):
    """What a mod zip's GE Lua is, for loading it live: {extensions: [names of its own extensions: lua/ge/extensions/a/b.lua
    -> a_b], modules: [their require names: a/b], overrides: n (the game's own files it replaces: a restart applies
    those), ui: has ui/ files, vehicle: has vehicle Lua or vehicles}. BeamNG.Earth's own names are never listed."""
    manifest = None
    try:
        with zipfile.ZipFile(path) as z:
            names = [n.replace("\\", "/") for n in z.namelist()]
            if "beamngEarth_addon.json" in names:      # (the author's own declaration: its options; addons.py)
                try:
                    raw = z.read("beamngEarth_addon.json")
                    manifest = json.loads(raw.decode("utf-8")) if len(raw) <= 64 * 1024 else None
                except (ValueError, KeyError, RuntimeError):
                    manifest = None
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return {"extensions": [], "modules": [], "overrides": 0, "ui": False, "vehicle": False, "manifest": None}
    ext, mods, over = [], [], 0
    pre = "lua/ge/extensions/"
    for n in names:
        if not (n.startswith(pre) and n.endswith(".lua")) or ".." in n.split("/"):
            continue
        rel = n[len(pre):-4]
        top = rel.split("/")[0]
        stock = os.path.isfile(os.path.join(game_dir, *n.split("/"))) if game_dir else top in STOCK_TOP
        if stock or rel.lower().startswith("beamngearth"):
            over += 1 if stock else 0
            continue
        ext.append(rel.replace("/", "_"))
        mods.append(rel)
    return {"extensions": ext[:CODE_MAX], "modules": mods[:CODE_MAX], "overrides": over,
            "ui": any(n.startswith("ui/") for n in names),
            "vehicle": any(n.startswith(("lua/vehicle/", "vehicles/")) for n in names),
            "manifest": manifest if isinstance(manifest, dict) else None}


def command(script, args=()):
    """The command that runs a companion's file with this app's own Python."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-addon", script, *args]
    return [sys.executable, LAUNCHER, "--run-addon", script, *args]


class Runner:
    """The companions' processes: one at most per add-on, ended with the server (stop_all, and their own parent
    watch)."""

    def __init__(self, folder, feed_url="", clock=time.time):
        self.folder = folder                # <data>/addons
        self.feed_url = feed_url
        self.clock = clock
        self.procs = {}                     # id -> (Popen, started)
        self.exits = {}                     # id -> the last run's exit code
        self.lock = threading.Lock()

    def dir(self, aid):
        return os.path.join(self.folder, aid)

    # --- what is installed ---
    def state(self, aid):
        try:
            with open(os.path.join(self.dir(aid), STATE), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) and _SAFE.fullmatch(str(d.get("file", ""))) else None
        except (OSError, ValueError):
            return None

    def save_state(self, aid, **kw):
        d = self.state(aid) or {}
        d.update(kw)
        p = os.path.join(self.dir(aid), STATE)
        os.makedirs(self.dir(aid), exist_ok=True)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(p + ".tmp", p)

    def installed(self, aid):
        st = self.state(aid)
        return st if st and os.path.isfile(os.path.join(self.dir(aid), st["file"])) else None

    def remove(self, aid):
        """Stop it and delete its files (its data folder too: the add-on's own records live beside it)."""
        self.stop(aid)
        import shutil
        shutil.rmtree(self.dir(aid), ignore_errors=True)

    # --- running ---
    def running(self, aid):
        with self.lock:
            got = self.procs.get(aid)
            if got is None:
                return False
            if got[0].poll() is None:
                return True
            self.exits[aid] = got[0].returncode
            del self.procs[aid]
            return False

    def start(self, aid, args=(), extra_env=None):
        """Run the installed file; raises RuntimeError with the reason for the player."""
        st = self.installed(aid)
        if st is None:
            raise RuntimeError("not installed")
        if self.running(aid):
            return
        d = self.dir(aid)
        script = os.path.join(d, st["file"])
        logp = os.path.join(d, LOG)
        try:
            if os.path.getsize(logp) > LOG_MAX:
                with open(logp, "rb") as f:
                    f.seek(-LOG_MAX // 4, os.SEEK_END)
                    tail = f.read()
                with open(logp, "wb") as f:
                    f.write(tail)
        except OSError:
            pass
        env = dict(os.environ)
        env["BNGEARTH_PARENT"] = str(os.getpid())
        env["PYTHONUNBUFFERED"] = "1"
        if self.feed_url:
            env["BNE_FEED"] = self.feed_url
        env.update(extra_env or {})
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000 | 0x00000200     # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        out = open(logp, "ab")
        out.write(f"\n--- started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
        out.flush()
        try:
            p = subprocess.Popen(command(script, args), cwd=d, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                 stderr=subprocess.STDOUT, **kw)
        except OSError as e:
            raise RuntimeError(f"could not start it ({e})") from e
        finally:
            out.close()
        with self.lock:
            self.procs[aid] = (p, self.clock())
        log.info("add-ons: %s started (pid %d): %s", aid, p.pid, st["file"])

    def stop(self, aid):
        with self.lock:
            got = self.procs.pop(aid, None)
        if got is None:
            return
        p = got[0]
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                p.kill()
                try:
                    p.wait(STOP_WAIT_S)
                except subprocess.TimeoutExpired:
                    pass
        # (stopped by the player: no error to show, whatever terminate() left as its exit code: -15 here, 1 on
        # Windows; the card said "stopped with an error" after Stop, 2026-11-07)
        self.exits.pop(aid, None)
        log.info("add-ons: %s stopped", aid)

    def stop_all(self):
        for aid in list(self.procs):
            self.stop(aid)

    def info(self, aid):
        """{installed, version, sha256, running, autostart, exit, log} for the page."""
        st = self.installed(aid)
        if st is None:
            return {"installed": False, "running": False, "autostart": False}
        run = self.running(aid)
        return {"installed": True, "version": str(st.get("tag", "")), "sha256": str(st.get("sha256", "")),
                "running": run, "autostart": st.get("autostart") is True,
                "exit": None if run else self.exits.get(aid), "log": os.path.join(self.dir(aid), LOG)}

    def log_tail(self, aid, n=20):
        try:
            with open(os.path.join(self.dir(aid), LOG), "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 16384))
                text = f.read().decode("utf-8", "replace").replace("\r", "\n")
        except OSError:
            return []
        return [ln for ln in text.split("\n") if ln.strip()][-n:]
