#!/usr/bin/env python3
"""Wait for a BeamNG.drive update (Steam installs it by itself: the app's AutoUpdateBehavior 0), then test BeamNG.Earth
on it unattended and write a report (2026-10-19, BeamNG 0.40: the user wants the mod checked as soon as it is out).

    python3 tools/await_update.py --out DIR [--poll 120] [--max-hours 48]

1. polls Steam's app manifest until its buildid differs from the one at the start and the app is fully installed
   (StateFlags 4); the game must not be running meanwhile (Steam does not update a running game);
2. starts the launcher (it installs the mod, starts the world server and the game);
3. waits for the game's MCP server, turns mods back on if the update turned them off
   (core_modmanager.enableModsAfterUpdate), loads the BeamNG.Earth level and waits for the world;
4. runs tools/compat_check.py and a short test drive (tools/drive.py), copies the game's and the server's logs;
5. writes DIR/report.md and exits (0: every step ran; 1: a step failed, the report says which).
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import mcp  # noqa: E402

MANIFEST = os.path.expanduser("~/.local/share/Steam/steamapps/appmanifest_284160.acf")
USER = os.path.expanduser("~/.local/share/BeamNG/BeamNG.drive/current")


def manifest():
    try:
        t = open(MANIFEST, encoding="utf-8", errors="replace").read()
    except OSError:
        return {}
    return dict(re.findall(r'"(\w+)"\s+"([^"]*)"', t))


def lua(code, timeout=20):
    return str(mcp.call("run_lua", {"code": code}))


def wait_for(fn, limit_s, step=5):
    t0 = time.time()
    while time.time() - t0 < limit_s:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--poll", type=float, default=120)
    ap.add_argument("--max-hours", type=float, default=48)
    ap.add_argument("--now", action="store_true", help="test at once (a rehearsal: no waiting for a new build)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rep = []

    def say(line):
        rep.append(line)
        print(line, flush=True)
        with open(os.path.join(a.out, "report.md"), "w") as f:
            f.write("\n".join(rep) + "\n")

    start = manifest().get("buildid")
    say(f"# BeamNG.drive update test\n\nwaiting since {time.strftime('%Y-%m-%d %H:%M')} for a build other than {start}")
    t0 = time.time()
    while not a.now:
        m = manifest()
        if m.get("buildid") and m.get("buildid") != start and m.get("StateFlags") == "4":
            break
        if time.time() - t0 > a.max_hours * 3600:
            say("no update within the time limit")
            return 1
        time.sleep(a.poll)
    if not a.now:
        say(f"- {time.strftime('%H:%M')} new build {m.get('buildid')} installed")
        time.sleep(30)                          # (Steam finishing its own bookkeeping)

    log = open(os.path.join(a.out, "launcher.log"), "w")
    subprocess.Popen([sys.executable, os.path.join(ROOT, "launcher.py"), "--no-terminal"], cwd=ROOT,
                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    if not wait_for(lambda: lua("return 1").strip() == "1", 600):
        say("- FAIL: the game's MCP server did not answer within 10 min (MCP off after the update? the game did "
            "not start?)")
        return 1
    say(f"- {time.strftime('%H:%M')} game up: {lua('return tostring(beamng_versiond)')}")
    # (0.40 no longer exports getModsDisabledAfterUpdate: the setting it keeps says it, 2026-10-19)
    off = lua("return tostring(settings and settings.getValue and settings.getValue('disableModsAfterUpdate'))")
    if off.strip() == "true":
        lua("core_modmanager.enableModsAfterUpdate() return 1")
        say("- the update had turned mods off: turned back on (enableModsAfterUpdate)")
        time.sleep(20)
    if lua("return tostring(beamngEarth_core ~= nil)").strip() != "true" or \
            lua("return tostring(getCurrentLevelIdentifier and getCurrentLevelIdentifier())").strip() != "beamng_earth":
        try:
            mcp.call("run_lua", {"code": "freeroam_freeroam.startFreeroamByName('beamng_earth') return 1"})     # (freeroam's start: load_level skips its extensions)
        except Exception as e:
            say(f"- load_level: {e}")
    if not wait_for(lambda: lua("return tostring(beamngEarth_core ~= nil and getPlayerVehicle(0) ~= nil)").strip()
                    == "true", 600):
        say("- FAIL: the BeamNG.Earth level or the mod did not load within 10 min")
    elif not wait_for(lambda: int(lua("return tostring(#(require('beamngEarth/terrain').slots or {}))").strip()) >= 2,
                      900, 10):
        say("- FAIL: no world terrain within 15 min (the server, the terrain swap?)")
    else:
        say(f"- {time.strftime('%H:%M')} the world loaded")
        time.sleep(60)
    r = subprocess.run([sys.executable, os.path.join(HERE, "compat_check.py"), "--user-dir", USER],
                       capture_output=True, text=True, timeout=300)
    say("\n## compat_check\n```\n" + r.stdout.strip() + "\n```")
    r2 = subprocess.run(["timeout", "600", sys.executable, os.path.join(HERE, "drive.py"), "--out",
                         os.path.join(a.out, "drive"), "--speed", "30", "--km", "3", "--label", "update-check"],
                        capture_output=True, text=True)
    say("\n## test drive (3 km at 67 mph)\n```\n" + (r2.stdout[-1500:] or r2.stderr[-1500:]).strip() + "\n```")
    r3 = subprocess.run([sys.executable, os.path.join(HERE, "compat_check.py"), "--user-dir", USER],
                        capture_output=True, text=True, timeout=300)
    say("\n## compat_check after the drive\n```\n" + r3.stdout.strip() + "\n```")
    for src, dst in ((os.path.join(USER, "beamng.log"), "beamng.log"),):
        try:
            shutil.copy(src, os.path.join(a.out, dst))
        except OSError:
            pass
    say(f"\n{time.strftime('%H:%M')} done (game and server left running)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
