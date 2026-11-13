#!/usr/bin/env python3
"""The players' reports sent from the game ("Send to the developers"; collector native/serverlist reports.rs on the
project's server): list them, or download them into folders laid out like the game's own report folders, so
tools/reports.py and the usual triage read them like the user's own.

  python3 tools/pull_reports.py list [filters]          one line per report, newest first
  python3 tools/pull_reports.py pull [filters] [--id ID ...]   download into --out/<ID>/ (already there: skipped)

  filters: --since YYYY-MM-DD  --until YYYY-MM-DD  --version 1.9.1-beta  --kind map|perf|bug|idea  --limit N
  --url   the collector (default https://earth.tgforever.net; BNGE_REPORTS_URL)
  --token-file PATH   a file holding the private token (the token alone, or a BNGE_TELEMETRY_TOKEN=... line, as
                      the collector's telemetry.env); else the environment's BNGE_REPORTS_TOKEN. Never printed.
  --out   default ~/bnge-tests/player_reports

Each folder: meta.json (id, received, version, install id, the player's choices), report.json, hardware.json,
server.json, screenshot.jpg (when sent), logs.json (the logs part, decompressed), issue.md (tools/reports.py's
fallback text). Then: python3 tools/reports.py --root ~/bnge-tests/player_reports list
"""
import argparse
import gzip
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_URL = os.environ.get("BNGE_REPORTS_URL", "https://earth.tgforever.net")
DEFAULT_OUT = os.path.expanduser("~/bnge-tests/player_reports")
ID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{4}-?[0-9A-HJKMNP-TV-Z]{4}")
FILES = ("report.json", "hardware.json", "server.json", "screenshot.jpg", "logs.gz")
MAX_FILE = 4 << 20          # bytes read of one file (the collector's caps are lower)
MAX_LOGS = 16 << 20         # the logs decompressed


def read_token(path):
    """The token from a file (the token alone or KEY=value lines) or BNGE_REPORTS_TOKEN; never printed."""
    if path:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            text = fh.read()
        for line in text.splitlines():
            line = line.strip()
            if "=" in line and line.split("=", 1)[0].strip().endswith("TOKEN"):
                return line.split("=", 1)[1].strip().strip('"\'')
        if text.strip() and "\n" not in text.strip():
            return text.strip()
        sys.exit("no token in that file")
    tok = os.environ.get("BNGE_REPORTS_TOKEN", "").strip()
    if not tok:
        sys.exit("give the token: --token-file PATH or BNGE_REPORTS_TOKEN")
    return tok


def get(base, path, token, limit=MAX_FILE):
    req = urllib.request.Request(base.rstrip("/") + path, headers={"Authorization": f"Bearer {token}",
                                                                   "User-Agent": "BeamNG.Earth pull_reports"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit("the collector refused the token (401)")
        raise
    if len(data) > limit:
        raise ValueError(f"{path}: over {limit} bytes")
    return data


def listing(base, token, args):
    q = {"limit": "200"}
    for k, v in (("from", args.since), ("to", args.until), ("version", args.version), ("kind", args.kind)):
        if v:
            q[k] = v
    out, after = [], None
    while True:
        if after:
            q["after"] = after
        page = json.loads(get(base, "/reports/list?" + urllib.parse.urlencode(q), token))
        out += page.get("reports") or []
        after = page.get("next")
        if not after or (args.limit and len(out) >= args.limit):
            break
    return out[:args.limit] if args.limit else out


def line(m):
    sent = m.get("sent") or {}
    flags = "".join(c for c, k in (("S", "screenshot"), ("L", "exactLocation"), ("G", "logs")) if sent.get(k))
    return f"{m.get('id', '?')[:4]}-{m.get('id', '?')[4:]}  {m.get('receivedAt', '')}  {m.get('v', '')[:22]:22s} " \
           f"{m.get('kind', ''):5s} {flags:3s} {m.get('country', '') or '--':2s} {m.get('title', '')[:70]}"


def pull_one(base, token, m, out):
    rid = str(m.get("id", ""))
    if not ID_RE.fullmatch(rid):
        print(f"skipped an odd id: {rid!r}")
        return False
    d = os.path.join(out, rid.replace("-", ""))
    if os.path.isfile(os.path.join(d, "meta.json")):
        return False
    tmp = d + ".part"
    os.makedirs(tmp, exist_ok=True)
    have = m.get("files") or {}
    for name in FILES:
        if name not in have:
            continue
        data = get(base, f"/reports/{rid}/{name}", token)
        if name == "logs.gz":
            with gzip.GzipFile(fileobj=__import__("io").BytesIO(data)) as g:
                text = g.read(MAX_LOGS + 1)
            if len(text) > MAX_LOGS:
                raise ValueError(f"{rid}: logs over {MAX_LOGS} bytes")
            name, data = "logs.json", text
        with open(os.path.join(tmp, name), "wb") as fh:
            fh.write(data)
    try:                        # (a ready-to-read summary like the game's issue.md)
        sys.path.insert(0, HERE)
        import reports
        with open(os.path.join(tmp, "report.json"), encoding="utf-8") as fh:
            rep = json.load(fh)
        title, body = reports.split_issue(reports.fallback_issue(rep))
        with open(os.path.join(tmp, "issue.md"), "w", encoding="utf-8") as fh:
            fh.write(f"# {title}\n\n_Player report {rid}, received {m.get('receivedAt')}, version {m.get('v')}._\n\n"
                     f"{body}\n")
    except Exception as e:
        print(f"{rid}: no issue.md ({type(e).__name__}: {e})")
    with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=1)
    os.replace(tmp, d)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("list", "pull"), nargs="?", default="list")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--token-file")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--version")
    ap.add_argument("--kind")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--id", action="append", default=[])
    args = ap.parse_args(argv)
    token = read_token(args.token_file)
    reps = listing(args.url, token, args)
    if args.id:
        want = {i.replace("-", "").upper() for i in args.id}
        reps = [m for m in reps if str(m.get("id", "")).replace("-", "") in want]
    if args.cmd == "list":
        for m in reps:
            print(line(m))
        print(f"{len(reps)} report(s)")
        return 0
    os.makedirs(args.out, exist_ok=True)
    n = 0
    for m in reps:
        if pull_one(args.url, token, m, args.out):
            n += 1
            print("pulled", line(m))
    print(f"{n} new report(s) in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
