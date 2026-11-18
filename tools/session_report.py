#!/usr/bin/env python3
"""What a server session did, from its log: how long the world took to load and what went wrong, as numbers that can
be compared between runs (a setting changed, a fix tried). Reads only; no game and no server needed.

    python tools/session_report.py                    the newest session in detail, then the last sessions side by side
    python tools/session_report.py --label cores=6    ... and name the newest session (kept in the history)
    python tools/session_report.py --last 10          more columns in the comparison
    python tools/session_report.py --log FILE ...     these log files instead of the server's own
    python tools/session_report.py --json             the parsed sessions as JSON
    python tools/session_report.py --no-record        leave the history file alone

One log file is one session (the launcher and start.py keep the last two as server.log.1 / .2), so every session read
is also kept as one line of <data folder>/session_history.jsonl (apppaths.data_dir(): %LOCALAPPDATA%\\beamng-earth on
Windows), by its session id: the comparison is made from that file and goes back further than the logs do. The newest
log's line also gets the repository's commit and branch at the time it is first recorded (the code the session most
likely ran; "+" for uncommitted changes).

Per session: the server's processes and CPUs, each teleport or first load (an "anchor": seconds to the first ground,
to the graded first block, to the first road tile and to the far field), the build times of terrain blocks, road
tiles and tree tiles (median / 95th percentile / worst), the game's frame rate as it reported it each minute, and the
problems counted by kind (errors, dead or hung worker processes, late road tiles and plans, event-loop stalls, files
held on Windows).
"""
import argparse
import datetime
import json
import os
import re
import statistics
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import apppaths  # noqa: E402

HISTORY = "session_history.jsonl"
LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) (\w+) ([\w.]+): (.*)$")
NUM = r"(-?\d+(?:\.\d+)?)"

LISTENING = re.compile(r"^earthd listening on \S+ session (\S+)(?: -> .*?)?(?: \((.*)\))?\s*$")
ROAD_PROCS = re.compile(r"road-tile processes: (\d+)")
FAR_PROCS = re.compile(r"far-tile processes: (\d+)")
PRIORITY = re.compile(r"server priority: (.+)")
CORES = re.compile(r"cores: the server on CPUs ([\d,]+), the game on ([\d,]+)")
ANCHOR_READY = re.compile(rf"anchor (\d+) \({NUM}, {NUM}\) ready in {NUM} s")
GRADED_FIRST = re.compile(rf"anchor (\d+): graded first block in {NUM} s after the request")
FAR_DONE = re.compile(rf"anchor (\d+) far field done: (\d+) tiles \((\d+) sent, (\d+) swaps in {NUM} s")
TERRAIN = re.compile(rf"terrain \((\d+), [^)]*\) z\d+ in {NUM} s \(pre {NUM} queue {NUM} work {NUM}")
TERRAIN_READY = re.compile(r"terrain_ready \((\d+), [^)]*\) after (\d+) ms")
PROVISIONAL = re.compile(rf"provisional block \((\d+), [^)]*\) in {NUM} s")
ROAD_TILE = re.compile(rf"road geotile \S+ in {NUM} s \(queue {NUM}; content {NUM} s {NUM} cpu, skin {NUM} s")
TREES_TILE = re.compile(rf"trees geotile \S+ in {NUM} s \(queue {NUM}")
PERF_MINUTE = re.compile(r"game: perf minute (\d+) fps, 1% low (\d+), worst (\d+) ms, (\d+) hitch")
STALL = re.compile(rf"event loop: the stall above lasted {NUM} s")
PLANS_WAITED = re.compile(r"plans: waited (\d+) s")
NOT_READY = re.compile(r"not ready after (\d+) s")
HUNG = re.compile(r"hung for (\d+) s")
HELD = re.compile(r"was held .* replaced at try (\d+)")

# (name, what a message of that kind contains)
PROBLEMS = (
    ("worker_died", "worker process died"),
    ("workers_restarted", "worker processes restarted"),
    ("worker_no_answer", "no answer from its worker"),
    ("road_tile_not_ready", "not ready after"),
    ("plans_waited", "plans: waited"),
    ("stack_dumps", "threads' stacks"),
    ("far_selection_slow", "far selection took"),
)


def _ts(day, ms):
    return datetime.datetime.strptime(day, "%Y-%m-%d %H:%M:%S").timestamp() + int(ms) / 1000.0


def dist(values):
    """{n, median, p95, max} of values (None for none): the nearest-rank 95th percentile."""
    if not values:
        return None
    v = sorted(values)
    return {"n": len(v), "median": round(statistics.median(v), 2),
            "p95": round(v[min(len(v) - 1, int(0.95 * len(v)))], 2), "max": round(v[-1], 2)}


def parse(lines, name=""):
    """The session of one server log (its lines), as a dict of plain values (see the module's docstring)."""
    s = {"file": name, "id": None, "label": None, "git": None, "start": None, "end": None, "settings": None,
         "road_processes": None, "far_processes": None, "priority": None, "server_cpus": None, "game_cpus": None,
         "connected_after_s": None}
    anchors, order = {}, []
    terrain, terrain_queue, ready_ms, provisional = [], [], [], []
    roads, road_queue, road_work, trees, trees_queue = [], [], [], [], []
    road_times = []                             # when each road tile was done (the first after an anchor's request)
    fps, low, worst, hitches = [], [], [], 0
    problems = {k: 0 for k, _ in PROBLEMS}
    errors, stalls, waited, late, hung, held = {}, [], [], [], [], 0
    first = last = None
    for raw in lines:
        raw = raw.rstrip("\r\n")
        m = LINE.match(raw)
        if not m:
            g = LISTENING.match(raw)
            if g:
                s["id"], s["settings"] = g.group(1), g.group(2)
            continue
        t, level, msg = _ts(m.group(1), m.group(2)), m.group(3), m.group(5)
        first = t if first is None else first
        last = t
        if level in ("ERROR", "CRITICAL"):
            key = re.sub(r"\(\d+, -?[\d.]+, -?[\d.]+\)", "(..)", msg)[:110]
            errors[key] = errors.get(key, 0) + 1
        if level != "INFO":
            for key, text in PROBLEMS:
                if text in msg:
                    problems[key] += 1
            for rx, into in ((STALL, stalls), (PLANS_WAITED, waited), (NOT_READY, late), (HUNG, hung)):
                g = rx.search(msg)
                if g:
                    into.append(float(g.group(1)))
            continue
        g = ANCHOR_READY.search(msg)
        if g:
            a = int(g.group(1))
            ready = float(g.group(4))
            anchors[a] = {"anchor": a, "lat": float(g.group(2)), "lon": float(g.group(3)), "requested": t - ready,
                          "ground_s": ready, "graded_first_s": None, "first_road_s": None, "far_field_s": None,
                          "far_tiles": None}
            order.append(a)
            continue
        g = GRADED_FIRST.search(msg)
        if g:
            if int(g.group(1)) in anchors:
                anchors[int(g.group(1))]["graded_first_s"] = float(g.group(2))
            continue
        g = FAR_DONE.search(msg)
        if g:
            a = anchors.get(int(g.group(1)))
            if a is not None and a["far_field_s"] is None:      # (the first: later ones are top-ups while driving)
                a["far_field_s"], a["far_tiles"] = round(t - a["requested"], 1), int(g.group(2))
            continue
        g = TERRAIN.search(msg)
        if g:
            terrain.append(float(g.group(2)))
            terrain_queue.append(float(g.group(4)))
            continue
        g = TERRAIN_READY.search(msg)
        if g:
            ready_ms.append(int(g.group(2)) / 1000.0)
            continue
        g = PROVISIONAL.search(msg)
        if g:
            provisional.append(float(g.group(2)))
            continue
        g = ROAD_TILE.search(msg)
        if g:
            roads.append(float(g.group(1)))
            road_queue.append(float(g.group(2)))
            road_work.append(float(g.group(3)) + float(g.group(5)))
            road_times.append(t)
            continue
        g = TREES_TILE.search(msg)
        if g:
            trees.append(float(g.group(1)))
            trees_queue.append(float(g.group(2)))
            continue
        g = PERF_MINUTE.search(msg)
        if g:
            fps.append(int(g.group(1)))
            low.append(int(g.group(2)))
            worst.append(int(g.group(3)))
            hitches += int(g.group(4))
            continue
        g = HELD.search(msg)
        if g:
            held += 1
            continue
        if "client connected" in msg and s["connected_after_s"] is None:
            s["connected_after_s"] = round(t - first, 1)
            continue
        for rx, key in ((ROAD_PROCS, "road_processes"), (FAR_PROCS, "far_processes")):
            g = rx.search(msg)
            if g:
                s[key] = int(g.group(1))
        g = PRIORITY.search(msg)
        if g:
            s["priority"] = g.group(1).strip()
        g = CORES.search(msg)
        if g:
            s["server_cpus"], s["game_cpus"] = g.group(1), g.group(2)

    loads = []
    for i, a in enumerate(order):
        rec = anchors[a]
        until = anchors[order[i + 1]]["requested"] if i + 1 < len(order) else float("inf")
        after = [t for t in road_times if rec["requested"] <= t < until]
        if after:
            rec["first_road_s"] = round(after[0] - rec["requested"], 1)
        rec["requested"] = datetime.datetime.fromtimestamp(rec["requested"]).strftime("%H:%M:%S")
        loads.append(rec)
    s["start"] = datetime.datetime.fromtimestamp(first).strftime("%Y-%m-%d %H:%M:%S") if first else None
    s["end"] = datetime.datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S") if first else None
    s["minutes"] = round((last - first) / 60.0, 1) if first else 0.0
    if s["id"] is None and first:           # (ended before it listened: named by its start)
        s["id"] = "log-" + datetime.datetime.fromtimestamp(first).strftime("%Y%m%d-%H%M%S")
    s["loads"] = loads
    s["terrain_s"], s["terrain_queue_s"] = dist(terrain), dist(terrain_queue)
    s["terrain_ready_s"], s["provisional_s"] = dist(ready_ms), dist(provisional)
    s["road_tile_s"], s["road_queue_s"], s["road_work_s"] = dist(roads), dist(road_queue), dist(road_work)
    s["trees_tile_s"], s["trees_queue_s"] = dist(trees), dist(trees_queue)
    s["fps"] = ({"minutes": len(fps), "mean": round(statistics.mean(fps)), "lowest_1pct_low": min(low),
                 "worst_frame_ms": max(worst), "hitches": hitches} if fps else None)
    problems["errors"] = sum(errors.values())
    problems["stalls"] = len(stalls)
    problems["stall_worst_s"] = max(stalls) if stalls else 0
    problems["stall_total_s"] = round(sum(stalls), 1)
    problems["plans_waited_worst_s"] = max(waited) if waited else 0
    problems["road_tile_late_worst_s"] = max(late) if late else 0
    problems["worker_hung_worst_s"] = max(hung) if hung else 0
    problems["files_held_retried"] = held
    s["problems"] = problems
    s["errors"] = [{"count": n, "message": k} for k, n in sorted(errors.items(), key=lambda kv: -kv[1])]
    return s


def parse_file(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse(f, os.path.basename(path))


def default_logs():
    d = os.path.join(apppaths.data_dir(), "logs")
    return [p for p in (os.path.join(d, n) for n in ("server.log.2", "server.log.1", "server.log")) if os.path.exists(p)]


def git_state():
    """"<branch>@<commit>" of this checkout ("+" appended with uncommitted changes), or None."""
    def run(*a):
        r = subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, timeout=20)
        return r.stdout.strip() if r.returncode == 0 else None
    try:
        commit, branch = run("rev-parse", "--short", "HEAD"), run("rev-parse", "--abbrev-ref", "HEAD")
        if not commit:
            return None
        dirty = run("status", "--porcelain", "--untracked-files=no")
        return f"{branch}@{commit}" + ("+" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return None


def read_history(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return out


def record(path, sessions):
    """sessions merged into the history file by id (a session read again replaces its line; its label and commit
    are kept unless given anew). The whole history, oldest first."""
    old = {h.get("id"): h for h in read_history(path)}
    for s in sessions:
        if not s.get("id"):
            continue
        before = old.get(s["id"]) or {}
        s["label"] = s.get("label") or before.get("label")
        s["git"] = before.get("git") or s.get("git")
        old[s["id"]] = s
    hist = sorted(old.values(), key=lambda h: h.get("start") or "")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for h in hist:
            f.write(json.dumps(h, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return hist


def _d(d, unit="s"):
    return "-" if not d else f"{d['median']:g} / {d['p95']:g} / {d['max']:g} {unit} (n={d['n']})"


def _n(v, unit=""):
    return "-" if v is None else f"{v:g}{unit}"


def _cpus(c):
    return "-" if not c else f"{len(c.split(','))} ({c})"


def detail(s):
    """The newest session, in full (text)."""
    p = s["problems"]
    out = [f"Session {s['id']}" + (f"  [{s['label']}]" if s.get("label") else ""),
           f"  {s['start']} to {(s['end'] or '')[11:]} ({s['minutes']:g} min), log {s['file']}"
           + (f", code {s['git']}" if s.get("git") else ""),
           f"  server CPUs {_cpus(s['server_cpus'])}, game CPUs {_cpus(s['game_cpus'])}, priority {s['priority'] or '-'}",
           f"  road-tile processes {_n(s['road_processes'])}, far-tile processes {_n(s['far_processes'])}", "",
           "Loads (seconds after the request)",
           f"  {'at':8} {'place':>20} {'ground':>7} {'graded':>7} {'1st road':>8} {'far field':>9}"]
    for a in s["loads"]:
        out.append(f"  {a['requested']:8} {a['lat']:9.4f},{a['lon']:10.4f} {_n(a['ground_s']):>7} "
                   f"{_n(a['graded_first_s']):>7} {_n(a['first_road_s']):>8} {_n(a['far_field_s']):>9}")
    if not s["loads"]:
        out.append("  (none: the game never asked for a place)")
    out += ["", "Build times (median / 95th percentile / worst)",
            f"  terrain block       {_d(s['terrain_s'])}",
            f"    of which queued   {_d(s['terrain_queue_s'])}",
            f"  terrain ready       {_d(s['terrain_ready_s'])}",
            f"  provisional block   {_d(s['provisional_s'])}",
            f"  road tile           {_d(s['road_tile_s'])}",
            f"    of which queued   {_d(s['road_queue_s'])}",
            f"    of which working  {_d(s['road_work_s'])}",
            f"  trees tile          {_d(s['trees_tile_s'])}",
            f"    of which queued   {_d(s['trees_queue_s'])}", "", "Game"]
    f = s["fps"]
    out.append("  (no frame-rate minutes reported)" if not f else
               f"  {f['mean']} fps mean over {f['minutes']} min, lowest 1% low {f['lowest_1pct_low']}, "
               f"worst frame {f['worst_frame_ms']} ms, {f['hitches']} hitches")
    out += ["", "Problems",
            f"  errors {p['errors']}, worker processes died {p['worker_died']}, restarted after a hang "
            f"{p['workers_restarted']} (worst hang {p['worker_hung_worst_s']:g} s)",
            f"  road tiles late {p['road_tile_not_ready']} (worst {p['road_tile_late_worst_s']:g} s), plans waited for "
            f"{p['plans_waited']} (worst {p['plans_waited_worst_s']:g} s), stack dumps {p['stack_dumps']}",
            f"  event-loop stalls {p['stalls']} (worst {p['stall_worst_s']:g} s, {p['stall_total_s']:g} s in all), "
            f"slow far selections {p['far_selection_slow']}, held files retried {p['files_held_retried']}"]
    for e in s["errors"][:8]:
        out.append(f"    {e['count']} x {e['message']}")
    return "\n".join(out)


def _first_load(s, key):
    return s["loads"][0].get(key) if s.get("loads") else None


def _worst_load(s, key):
    v = [a[key] for a in s.get("loads") or [] if a.get(key) is not None]
    return max(v) if v else None


def _med(s, key, what="median"):
    return (s.get(key) or {}).get(what)


ROWS = (
    ("label", lambda s: s.get("label") or "-"),
    ("code", lambda s: s.get("git") or "-"),
    ("minutes", lambda s: _n(s.get("minutes"))),
    ("server CPUs", lambda s: "-" if not s.get("server_cpus") else str(len(s["server_cpus"].split(",")))),
    ("loads", lambda s: str(len(s.get("loads") or []))),
    ("first load: ground s", lambda s: _n(_first_load(s, "ground_s"))),
    ("first load: graded s", lambda s: _n(_first_load(s, "graded_first_s"))),
    ("first load: 1st road s", lambda s: _n(_first_load(s, "first_road_s"))),
    ("first load: far field s", lambda s: _n(_first_load(s, "far_field_s"))),
    ("worst load: graded s", lambda s: _n(_worst_load(s, "graded_first_s"))),
    ("terrain block median s", lambda s: _n(_med(s, "terrain_s"))),
    ("terrain block p95 s", lambda s: _n(_med(s, "terrain_s", "p95"))),
    ("road tile median s", lambda s: _n(_med(s, "road_tile_s"))),
    ("road tile p95 s", lambda s: _n(_med(s, "road_tile_s", "p95"))),
    ("road tile working median s", lambda s: _n(_med(s, "road_work_s"))),
    ("trees tile median s", lambda s: _n(_med(s, "trees_tile_s"))),
    ("fps mean", lambda s: _n((s.get("fps") or {}).get("mean"))),
    ("fps lowest 1% low", lambda s: _n((s.get("fps") or {}).get("lowest_1pct_low"))),
    ("errors", lambda s: _n(s["problems"].get("errors"))),
    ("workers died", lambda s: _n(s["problems"].get("worker_died"))),
    ("workers hung", lambda s: _n(s["problems"].get("workers_restarted"))),
    ("road tiles late", lambda s: _n(s["problems"].get("road_tile_not_ready"))),
    ("plans waited for", lambda s: _n(s["problems"].get("plans_waited"))),
    ("stalls (worst s)", lambda s: f"{s['problems'].get('stalls', 0)} ({s['problems'].get('stall_worst_s', 0):g})"),
    ("held files retried", lambda s: _n(s["problems"].get("files_held_retried"))),
)


def compare(sessions):
    """The sessions side by side, oldest left (text)."""
    heads = [(s.get("start") or "?")[5:16] for s in sessions]
    cols = [[fn(s) for _, fn in ROWS] for s in sessions]
    w0 = max(len(r[0]) for r in ROWS)
    ws = [max(len(h), *(len(c) for c in col)) for h, col in zip(heads, cols)]
    out = ["  ".join([" " * w0] + [h.rjust(w) for h, w in zip(heads, ws)])]
    for i, (name, _) in enumerate(ROWS):
        out.append("  ".join([name.ljust(w0)] + [col[i].rjust(w) for col, w in zip(cols, ws)]))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Load times and problems of server sessions, from their logs.")
    ap.add_argument("--log", action="append", help="a server log to read (default: the server's own, with .1 and .2)")
    ap.add_argument("--label", help="a name for the newest session read (kept in the history)")
    ap.add_argument("--last", type=int, default=5, help="sessions in the comparison (default 5)")
    ap.add_argument("--history", default=os.path.join(apppaths.data_dir(), HISTORY), help="the history file")
    ap.add_argument("--no-record", action="store_true", help="do not write the history file")
    ap.add_argument("--json", action="store_true", help="print the sessions read as JSON")
    args = ap.parse_args(argv)

    paths = args.log or default_logs()
    if not paths:
        print("no server log found (run the server once, or give --log FILE)", file=sys.stderr)
        return 1
    sessions = [s for s in (parse_file(p) for p in paths) if s["start"]]
    if not sessions:
        print("no session in " + ", ".join(paths), file=sys.stderr)
        return 1
    sessions.sort(key=lambda s: s["start"])
    newest = sessions[-1]
    newest["label"] = args.label
    if not args.log:                    # (the server's own newest log: most likely this checkout's code)
        newest["git"] = git_state()
    hist = sessions if args.no_record else record(args.history, sessions)
    if args.no_record:                  # (still compared with what was kept before)
        seen = {s["id"] for s in sessions}
        hist = sorted([h for h in read_history(args.history) if h.get("id") not in seen] + sessions,
                      key=lambda h: h.get("start") or "")
    newest = next(h for h in hist if h.get("id") == newest["id"])
    if args.json:
        print(json.dumps(sessions, indent=1, sort_keys=True))
        return 0
    print(detail(newest))
    print()
    print(f"Last {min(args.last, len(hist))} of {len(hist)} sessions" +
          ("" if args.no_record else f" ({args.history})"))
    print(compare(hist[-args.last:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
