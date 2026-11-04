#!/usr/bin/env python3
"""A release's GitHub notes: its CHANGELOG.md section, then the launch steps (docs/LAUNCH_STEPS.md) at the end.

The user, 2026-11-04: "Add this at the end to every future release change log" (the "How to launch BeamNG.Earth"
section first written for 1.8.1-beta).

    python3 tools/release_notes.py 1.8.2-beta > .claude/work/release_notes_1.8.2-beta.md
    python3 tools/release_notes.py --launch-only 1.8.2-beta     # just the launch steps (the release workflow)
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCH = os.path.join(ROOT, "docs", "LAUNCH_STEPS.md")
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")


def launch_steps(version):
    """docs/LAUNCH_STEPS.md for `version`, without its leading HTML comment."""
    with open(LAUNCH, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r"\A\s*<!--.*?-->\s*", "", text, flags=re.S)
    return text.replace("{version}", version).strip() + "\n"


def changelog_section(version, text=None):
    """The body of CHANGELOG.md's "## <x.y.z> beta (date)" section for `version` ("1.8.2-beta"), heading left out."""
    if text is None:
        with open(CHANGELOG, encoding="utf-8") as f:
            text = f.read()
    number, _, kind = version.partition("-")
    head = re.compile(r"^## " + re.escape(number) + (r" " + re.escape(kind) if kind else "") + r"\b.*$", re.M)
    m = head.search(text)
    if not m:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    nxt = re.compile(r"^## ", re.M).search(text, m.end())
    return text[m.end():nxt.start() if nxt else len(text)].strip() + "\n"


def notes(version, changelog_text=None):
    return changelog_section(version, changelog_text) + "\n---\n\n" + launch_steps(version)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("version", help='the release version, e.g. "1.8.2-beta"')
    ap.add_argument("--launch-only", action="store_true", help="print only the launch steps")
    args = ap.parse_args()
    sys.stdout.write(launch_steps(args.version) if args.launch_only else notes(args.version))


if __name__ == "__main__":
    main()
