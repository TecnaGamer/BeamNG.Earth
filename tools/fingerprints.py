#!/usr/bin/env python3
"""Which server code line an error fingerprint from the telemetry is (TELEMETRY.md section 5; perfstats.ErrorPrints:
fast.perf_fingerprint of the exception's type name, the logging call's module and line).

  python3 tools/fingerprints.py                     # the collector's last 7 days (earth.tgforever.net/stats/advanced)
  python3 tools/fingerprints.py --rev fd51e001 a0ce47b0 e8382ed7

The line numbers are those of the build that sent them: --rev the commit of that dev build (the release workflow's run
number names it: gh run list -w release.yml). Every error / exception logging line of server/*.py at that commit is
hashed with the built-in exception names and a few library ones until the fingerprints match.
"""
import argparse
import builtins
import json
import os
import re
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import fast  # noqa: E402

KINDS = {n for n in dir(builtins) if isinstance(getattr(builtins, n), type) and issubclass(getattr(builtins, n),
                                                                                            BaseException)}
KINDS |= {"log", "IncompleteRead", "RemoteDisconnected", "HTTPError", "URLError", "JSONDecodeError",
          "BrokenProcessPool", "CancelledError", "SSLError", "CertificateError", "error", "LinAlgError",
          "NativeMissing", "InvalidStateError", "IncompleteReadError", "LimitOverrunError", "BadZipFile",
          "DecompressionBombError", "UnidentifiedImageError", "PanicException"}
LOG_CALL = re.compile(r"\.(error|exception|critical)\(|logging\.(error|exception)|log\.log\(")


def lines_of(rev):
    files = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", rev, "server/"], cwd=ROOT).decode().split()
    for f in files:
        if not f.endswith(".py") or "/tests/" in f:
            continue
        src = subprocess.check_output(["git", "show", f"{rev}:{f}"], cwd=ROOT).decode(errors="replace").splitlines()
        for i, line in enumerate(src, 1):
            if LOG_CALL.search(line):
                yield f, os.path.basename(f)[:-3], i, line.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prints", nargs="*", help="fingerprints (default: the collector's last 7 days)")
    ap.add_argument("--rev", default="origin/dev")
    a = ap.parse_args()
    counts = {}
    if a.prints:
        want = {p.lower(): None for p in a.prints}
    else:
        with urllib.request.urlopen("https://earth.tgforever.net/stats/advanced", timeout=30) as r:
            rows = json.load(r).get("errors7d") or []
        want = {row[0]: row for row in rows}
        counts = {row[0]: row[1:] for row in rows}
    found = {}
    for f, mod, i, line in lines_of(a.rev):
        for k in KINDS:
            h = fast.perf_fingerprint(k, mod, i)
            if h in want and h not in found:
                found[h] = (k, f, i, line[:120])
    for h in want:
        extra = counts.get(h, [])
        print(h, *extra, "->", "%s at %s:%d  %s" % found[h] if h in found else "? (another build's line numbers?)")


if __name__ == "__main__":
    main()
