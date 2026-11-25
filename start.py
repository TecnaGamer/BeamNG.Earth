#!/usr/bin/env python3
"""Start the BeamNG.Earth world server with the recommended options (Windows and Linux).

    python start.py                 start the server (leave the window open while you play)
    python start.py --keep-mod      leave the game's copy of the mod as it is (by default a copy that differs
                                    from this folder's, an older version, is replaced: one zip, one version)
    python start.py -- --verbose    anything after -- goes to server/earthd.py as is

It uses the Python in .venv (made by install.py) and the BeamNG.drive user folder install.py found.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

import install
import launchui          # (the launch screen: colours, banner, timed steps; stdlib only)
import modtextures       # (--status reads the installed textures' stamp)
import statcache         # (the files' cached digests: the launch's up-to-date checks do not re-read them)

ROOT = install.ROOT
EARTHD = os.path.join(ROOT, "server", "earthd.py")

# The recommended options. Ones the server does not know (an older or newer server) are left out.
RECOMMENDED = [
    ("--deshadow", "full"),
    ("--near-visual", "detail"),
    ("--airports", "on"),
    ("--sea", "on"),
    ("--trees", "on"),
    ("--buildings", "on"),
    ("--gas-stations", "on"),
]


def server_flags(py):
    """earthd --help's text, which has_flag only looks flags up in. Running the server's modules to
    print it takes over a second of every launch, and the text changes only with server/earthd.py:
    the flagcache keeps it against that file's stat (its size, mtime and inode; an update reads it
    again). No cache (a data folder that cannot be made): run and return, as before."""
    import apppaths
    key = statcache._stat_key(os.stat(EARTHD))
    path = os.path.join(apppaths.data_dir(), "flagcache.json")
    try:
        with open(path, encoding="utf-8") as f:
            kept = json.load(f)
        if kept.get("key") == key:
            return kept.get("text", "")
    except (OSError, ValueError):
        pass
    r = subprocess.run([py, EARTHD, "--help"], capture_output=True, text=True, errors="replace", cwd=ROOT)
    if r.returncode != 0:
        install.fail("the server does not start:\n" + (r.stderr or r.stdout).strip() +
                     "\n  Run the installer again: python install.py")
    try:
        os.makedirs(apppaths.data_dir(), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"key": key, "text": r.stdout}, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass                                # (no cache: read again next launch, as before)
    return r.stdout


def status():
    """--status: what this machine's BeamNG.Earth is, without starting anything: the versions, the
    mod's state in the game, the download cache's size, the disk room left and the last logs. Read
    only: a launch question ("is it installed? why is the cache so big?") answered from the files."""
    import apppaths
    cfg = install.load_config()
    rows = [("Version", install.version()), ("Folder", ROOT)]
    user_dir = cfg.get("user_dir")
    if user_dir:
        rows.append(("Game folder", user_dir))
        mod = os.path.join(user_dir, "mods", "unpacked", install.MOD_NAME)
        if os.path.isdir(mod):
            stamp = modtextures_stamp(mod)
            rows.append(("Mod", f"installed{f' ({stamp})' if stamp else ''}"))
        else:
            rows.append(("Mod", launchui.paint("not installed (run install.py)", launchui.Color.warn)))
    else:
        rows.append(("Mod", launchui.paint("no game folder known (run install.py)", launchui.Color.warn)))
    cache = apppaths.saved_cache_dir() or apppaths.default_cache_dir()
    if os.path.isdir(cache):
        size, files = folder_size(cache)
        rows.append(("Cache", f"{cache}  ({size / 1e9:.1f} GB, {files} files)"))
    else:
        rows.append(("Cache", f"{cache}  (empty: nothing downloaded yet)"))
    try:
        free = shutil.disk_usage(cache or os.path.expanduser("~")).free
        rows.append(("Disk free", f"{free / 1e9:.1f} GB"))
    except OSError:
        pass
    logs = os.path.join(apppaths.data_dir(), "logs")
    if os.path.isdir(logs):
        for name in ("server.log", "server.log.1"):
            path = os.path.join(logs, name)
            if os.path.isfile(path):
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
                rows.append((f"Log {name}", f"{os.path.getsize(path) / 1e6:.1f} MB, last written {when}"))
    ui = launchui.Screen()
    ui.banner(f"BeamNG.Earth {install.version()}", "status", rows=rows,
              tips=["python start.py starts the world server (python install.py sets it up).",
                    "More help: docs/QUICKSTART.md and docs/DEVTOOLS.md"])


def folder_size(path):
    """A folder's bytes and file count, walked once (the cache's size: what --status is asked)."""
    size = files = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                size += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
            files += 1
    return size, files


def modtextures_stamp(mod):
    """The installed textures' count, one line (the stamp the encoder wrote; "" without one)."""
    try:
        return f"{len(modtextures.read_stamp(mod))} textures" if modtextures.read_stamp(mod) else ""
    except OSError:
        return ""


def has_flag(help_text, flag):
    return re.search(re.escape(flag) + r"(?![\w-])", help_text) is not None


def main():
    ap = argparse.ArgumentParser(description="Start the BeamNG.Earth world server with the recommended options.")
    ap.add_argument("--update-mod", action="store_true", help="(the default now) copy the mod into the game when it differs")
    ap.add_argument("--keep-mod", action="store_true", help="leave the game's copy of the mod as it is")
    ap.add_argument("--user-dir", help="BeamNG.drive user folder (default: the one install.py found)")
    ap.add_argument("--yes", "-y", action="store_true", help="with --update-mod: replace the installed mod without asking")
    ap.add_argument("--status", action="store_true", help="what this machine's BeamNG.Earth is, without starting anything")
    ap.add_argument("extra", nargs=argparse.REMAINDER, help="after --: more options for server/earthd.py")
    args = ap.parse_args()

    ui = launchui.Screen()          # colours on for the console this window owns; plain text without one

    if args.status:
        status()
        return

    cfg = install.load_config()
    user_dir = args.user_dir or cfg.get("user_dir")
    if not user_dir:
        found = install.candidate_user_dirs()
        user_dir = found[0] if found else None
    if not user_dir:
        install.fail("no BeamNG.drive user folder known. Run the installer first: python install.py")

    # a new version unzipped over (or next to) the old one: the game gets this folder's mod, the server this
    # folder's packages, without running the installer again (2026-10-15: easy setup)
    if not args.keep_mod:
        with ui.step("Preparing the mod"):
            install.install_mod(user_dir, True, dry_run=False)

    py = install.venv_python()
    if not os.path.isfile(py):
        install.fail("the server's Python environment (.venv) is missing. Run the installer first: python install.py")
    try:
        with open(install.REQ_STAMP, encoding="utf-8") as f:
            stamped = f.read().strip()
    except OSError:
        stamped = None
    if stamped != install.requirements_hash():
        with ui.step("Checking the Python packages"):
            install.setup_venv(dry_run=False)

    with ui.step("Reading the server's options"):
        help_text = server_flags(py)
    cmd = [py, EARTHD, "--user-dir", user_dir]
    skipped = []
    for flag, value in RECOMMENDED:
        if has_flag(help_text, flag):
            cmd += [flag, value]
        else:
            skipped.append(flag)
    extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
    cmd += extra
    # anonymous usage reports (server/telemetry.py), as the app's launcher: BNE_TELEMETRY=off turns them off
    telemetry = os.environ.get("BNE_TELEMETRY", "on").lower() not in ("off", "0", "false", "no") \
        and not os.environ.get("CI")        # (never the CI's or a test's servers: they are not players)
    if telemetry and has_flag(help_text, "--telemetry") and "--offline" not in extra and "--telemetry" not in extra:
        cmd += ["--telemetry", "on"]

    base_env = dict(os.environ)
    pick = overture_pick(extra)     # (the map data's release: decided once, while the start goes on)
    experimental = experimental_env(user_dir, dict(base_env))   # (for the lines below; run_restarting applies them)

    # the launch screen: what was checked, the settings that matter, how to get into the world
    log_path = server_log_path(rotate=False)
    ui.banner(f"BeamNG.Earth {install.version()}", "world server", rows=[
        ("Game folder", user_dir),
        ("Map data", None),             # (the Pick's release, said when the server starts below)
        ("Experimental", ", ".join(experimental) if experimental else None),
        ("Telemetry", "on (anonymous player count; BNE_TELEMETRY=off turns it off)"
         if telemetry and "--telemetry" in cmd else "off"),
        ("Log", log_path),
    ] + ([("Server options", "this server version has no " + ", ".join(skipped) + "; left out")] if skipped else []),
    tips=["Leave this window open while you play; close it (or press Ctrl+C) to stop the server.",
             "Now start BeamNG.drive and pick the 'BeamNG.Earth' level in Freeroam."])
    ui.summary("starting the server")
    ui.note("")
    try:
        sys.exit(run_restarting(cmd, user_dir, base_env, pick=pick))
    except KeyboardInterrupt:
        sys.exit(0)


def run_restarting(cmd, user_dir, base_env, run=None, limiter=None, pick=None):
    """Run the server (run_logged), and again with the same options when it exits asking for it (srvrestart.EXIT_CODE:
    the settings page's "Restart world server"), each time in an environment made anew from base_env with the settings
    read again (server_env: the Experimental switches, the ground grid), at most srvrestart.MAX_RESTARTS in WINDOW_S
    (the server is told its restarts so far and refuses one past it). Any other exit code is returned. pick: the map
    data's release (overturepick.Pick), the same for every start of this run (server_env)."""
    import time
    import cliffstock
    from launcher import game_install_dir
    sr = _srvrestart()
    run = run or (lambda c, path, e: run_logged(c, path, e))
    limiter = limiter or sr.Limiter()
    first = True
    while True:
        env, ids = server_env(user_dir, base_env, limiter.times, pick.release() if pick is not None else None)
        if first and pick is not None:
            install.say("  Map data: Overture " + launchui.paint(pick.release(), launchui.Color.title)
                        + f" ({pick.why})")
        # Use the same lazy game lookup and private cache as the app launcher.
        # Complete conversion before the child starts, also on --keep-mod/restart.
        if pick is not None and env.get("BNE_CLIFFS", "off").strip().lower() in {"1", "on", "true"}:
            env["BNGE_CACHE_DIR"] = pick.cache_dir
        cliffstock.prepare_enabled(game_install_dir, user_dir, env=env, diagnostic=install.say)
        if not first and ids:
            install.say("  Experimental features on (the settings page): " + ", ".join(ids))
        first = False
        code = run(cmd, server_log_path(), env)
        if not sr.wants_restart(code):
            return code
        if not limiter.allow(time.time()):
            install.say(launchui.paint(f"The world server exited asking to restart more than {sr.MAX_RESTARTS} times in "
                                       f"{sr.WINDOW_S / 60:g} minutes; it stays stopped. Start it again with this script.",
                                       launchui.Color.bad))
            return code
        install.say("")
        install.say("Restarting the world server (asked from the game's settings page): the settings are read again")


def _srvrestart():
    here = os.path.join(ROOT, "server")
    if here not in sys.path:
        sys.path.insert(0, here)
    import srvrestart
    return srvrestart


def overture_pick(extra):
    """The map data's Overture release for this run (server/overturepick.py), started now in a thread of its own: the
    first start takes it (waiting overturepick.PICK_S from now at most), every restart the same. None: the server's
    own default."""
    try:
        here = os.path.join(ROOT, "server")
        if here not in sys.path:
            sys.path.insert(0, here)
        import overturepick
        import apppaths         # (earthd's rules: the last --cache-dir, relative to its folder ROOT, else the saved one)
        saved = apppaths.app_settings().get("cacheDir")
        default = saved if isinstance(saved, str) and saved else apppaths.default_cache_dir()
        return overturepick.Pick(overturepick.cache_dir_of(extra, default, ROOT), offline="--offline" in extra)
    except Exception:
        return None


def server_env(user_dir, base_env, times=(), overture_release=None):
    """(the server's environment, the Experimental ids on): base_env with the settings page's start-time settings as
    the app's launcher applies them (the Experimental switches, the ground grid: a variable set by hand wins), marked
    as a server this script starts again when it asks (srvrestart.supervisor_env, from what was applied)."""
    env = dict(base_env)
    if overture_release:        # (the run's map data release: overture_pick)
        env["BNGE_OVERTURE_RELEASE"] = overture_release
    ids = experimental_env(user_dir, env)
    try:
        import experimental
        sr = _srvrestart()
        settings = experimental.read_settings(user_dir)
        sr.apply_ground(env, settings)
        sr.supervisor_env(env, settings, base_env=base_env, times=times)
    except Exception:
        pass                            # (no restart button then)
    return env, ids


def experimental_env(user_dir, env):
    """The settings page's Experimental switches (server/experimental.py) into env before the server starts, as the
    app's launcher does (launcher.experimental_env): its modules read them at import, in every process alike. Returns
    the ids turned on ([] when the file cannot be read: the defaults)."""
    try:
        here = os.path.join(ROOT, "server")
        if here not in sys.path:
            sys.path.insert(0, here)
        import experimental
        return experimental.apply(env, user_dir)
    except Exception:
        return []


def server_log_path(rotate=True):
    """The server's log file: the launcher's (apppaths.data_dir()/logs/server.log, the downloaded app's), with the last
    two runs kept beside it (rotate: moved aside now, for a run about to start); None when its folder cannot be made.
    (Tann2019, 2026-11-05: a source run's log was only in its console window, which cannot be copied from easily.)"""
    try:
        import apppaths
        path = os.path.join(apppaths.data_dir(), "logs", "server.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except (ImportError, OSError):
        return None
    if not rotate:
        return path
    try:                                # (the launcher's keep_previous_log: server.log.1, .2)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            if os.path.isfile(path + ".1"):
                os.replace(path + ".1", path + ".2")
            os.replace(path, path + ".1")
    except OSError:
        pass                            # (still open somewhere: overwritten)
    return path


def run_logged(cmd, log_path, env=None):
    """Run the server (in env, default this process's), its output shown here as before and written to log_path too;
    its exit code."""
    env = dict(os.environ if env is None else env)
    if not log_path:
        return subprocess.call(cmd, cwd=ROOT, env=env)
    env["PYTHONUNBUFFERED"] = "1"       # (lines as they come, not in blocks through the pipe)
    try:
        log = open(log_path, "wb")
    except OSError:
        return subprocess.call(cmd, cwd=ROOT, env=env)
    with log:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = getattr(sys.stdout, "buffer", None)
        try:
            for line in iter(proc.stdout.readline, b""):
                log.write(line)
                log.flush()
                if out is not None:
                    out.write(line)
                    out.flush()
        except KeyboardInterrupt:       # (Ctrl+C reaches the server too: it stops, its last lines still logged)
            for line in proc.stdout:
                log.write(line)
            raise
        finally:
            proc.stdout.close()
        return proc.wait()


if __name__ == "__main__":
    main()
