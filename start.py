#!/usr/bin/env python3
"""Start the BeamNG.Earth world server with the recommended options (Windows and Linux).

    python start.py                 start the server (leave the window open while you play)
    python start.py --keep-mod      leave the game's copy of the mod as it is (by default a copy that differs
                                    from this folder's, an older version, is replaced: one zip, one version)
    python start.py -- --verbose    anything after -- goes to server/earthd.py as is

On Windows it uses the fixed Python set in install.py (PY_DIR); elsewhere the Python in .venv (made by install.py) and the BeamNG.drive user folder install.py found.
"""

import argparse
import os
import re
import subprocess
import sys

import install

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
]


def server_flags(py):
    r = subprocess.run([py, EARTHD, "--help"], capture_output=True, text=True, errors="replace", cwd=ROOT)
    if r.returncode != 0:
        install.fail("the server does not start:\n" + (r.stderr or r.stdout).strip() +
                     "\n  Run the installer again: python install.py")
    return r.stdout


def has_flag(help_text, flag):
    return re.search(re.escape(flag) + r"(?![\w-])", help_text) is not None


def main():
    ap = argparse.ArgumentParser(description="Start the BeamNG.Earth world server with the recommended options.")
    ap.add_argument("--update-mod", action="store_true", help="(the default now) copy the mod into the game when it differs")
    ap.add_argument("--keep-mod", action="store_true", help="leave the game's copy of the mod as it is")
    ap.add_argument("--user-dir", help="BeamNG.drive user folder (default: the one install.py found)")
    ap.add_argument("--yes", "-y", action="store_true", help="with --update-mod: replace the installed mod without asking")
    ap.add_argument("extra", nargs=argparse.REMAINDER, help="after --: more options for server/earthd.py")
    args = ap.parse_args()

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
        install.install_mod(user_dir, True, dry_run=False)

    install.check_python()
    py = install.venv_python()
    if not os.path.isfile(py):
        install.fail("the server's Python environment (.venv) is missing. Run the installer first: python install.py")
    try:
        with open(install.REQ_STAMP, encoding="utf-8") as f:
            stamped = f.read().strip()
    except OSError:
        stamped = None
    if stamped != install.requirements_hash():
        install.say("Python packages: checking the server's packages ...")
        install.setup_venv(dry_run=False)

    help_text = server_flags(py)
    cmd = [py, EARTHD, "--user-dir", user_dir]
    skipped = []
    for flag, value in RECOMMENDED:
        if has_flag(help_text, flag):
            cmd += [flag, value]
        else:
            skipped.append(flag)
    if install.IS_WIN and has_flag(help_text, "--nice"):
        cmd += ["--nice", "0"]          # os.nice does not exist on Windows
    extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
    cmd += extra

    install.say(f"BeamNG.Earth {install.version()}: starting the world server")
    install.say(f"  BeamNG.drive user folder: {user_dir}")
    if skipped:
        install.say(f"  (this server version has no {', '.join(skipped)}; left out)")
    install.say("  Leave this window open while you play; close it (or press Ctrl+C) to stop the server.")
    install.say("  Now start BeamNG.drive and pick the 'BeamNG.Earth' level in Freeroam.")
    install.say("")
    try:
        sys.exit(subprocess.call(cmd, cwd=ROOT))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
