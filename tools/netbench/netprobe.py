#!/usr/bin/env python3
"""A game session's network traffic without the game: the real world server (earthd.py of --root), driven as the mod
drives it for a player who teleports from place to place and stands there. Every request the server makes is logged
(BNGE_NETLOG); places.jsonl gives each place's line window in net.log (bench.sh runs it for both builds).

    netprobe.py --root WORKTREE --out DIR [--places lonepine=36.5967,-118.1700 hartford=41.7637,-72.6851 ...]

Per place: prepare_anchor (purpose teleport) -> anchor_ready -> commit_anchor + commit_terrain at the anchor's block
(the mod's sendCommits), then a pose every POSE_S with a chase camera looking north, and one 'where' (the status
bar). Far tiles' pooled slots are let go as the mod does (pose.slotsGone). A place ends once it has loaded and the
server is idle (Settle), or after --cap s, or once --until is reached. marks: s from the teleport to anchor_ready (the
car on its ground), graded_s (the graded first block in: terrain_refresh), far_done, each load_progress part done for
good (ground_s: the graded first block with its final photo, roads_s, trees_s, far_s; loaded_s: all of them, the
mod's loading line ends) and settled_s. hello and graphics are the mod's own with the player's settings (normal
terrain, trees and buildings, 2048 px ground photos, horizon view).

The server's environment is this process's: start it behind the cache proxy (proxy.sh env) to spare the hosts.
--link MBIT:RTT has the proxy deliver at that line's speed (link.py) and keeps the server as a player's: its own
per-host request limits (no sitepatch), the live lidar wait (BNE_LIDAR_GAPS_WAIT unset), so the times are a player's on
that line (the bytes then vary a little run to run).
"""
import argparse
import collections
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

PLACES = ["lonepine=36.596718,-118.169991", "hartford=41.7637,-72.6851", "amsterdam=52.3730,4.8924",
          "lyon=45.7640,4.8357", "sanfrancisco=37.8080,-122.4177"]
RECOMMENDED = [("--deshadow", "full"), ("--near-visual", "detail"), ("--airports", "on"), ("--sea", "on"),
               ("--trees", "on"), ("--buildings", "on"), ("--gas-stations", "on")]     # (start.py)
# no game here: nothing to report or update, no frame rate to protect from the background analyses (pacer.py: the
# colour-seam and de-shadow regions run unpaced, the same work and downloads in a quarter of the time), and no game's
# cores to keep free (cpupin.py: every server pinned to the same last quarter of the cores, the rest idle)
HEADLESS = [("--update-check", "off"), ("--telemetry", "off"), ("--companion-feed", "off"),
            ("--background-duty", "1"), ("--pin", "off")]
PARTS = ("ground", "roads", "trees", "far")     # load_progress's counts (the mod's loading line)
UNTIL = PARTS + ("loaded",)
POSE_S = 0.5                # core.lua POSE_INTERVAL: every pose re-runs the server's far selection (~0.4 s of GIL)
SITEPATCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sitepatch")
USER_SETTINGS = os.path.expanduser("~/.local/share/BeamNG/BeamNG.drive/current/settings/beamngearth.json")
# the mod's hello (core.lua) with its constants: sea.GUARD_VERSION, geotile.Z, roads.ROAD_GEO / FAR_GEO,
# turbinestream.PROTOCOL, farfield.GLOBAL_FAR, distanttraffic.PROTO
HELLO = {"type": "hello", "protocol": 4, "format": 4, "modVersion": "netprobe", "waterguard": 1, "streamLease": 1,
         "sessions": [], "slotsHeld": [], "held": [], "vegFlat": True, "geoTiles": 17, "roadGeo": 1, "farGeo": 1,
         "turbineGeo": 1, "globalFar": 1, "trafficVisuals": 2, "readySet": 1}
# settings.serverGraphics() at terrainDetail / treeDistance / buildingDistance 'normal', groundPhoto 2048, horizon
GRAPHICS = {"type": "graphics", "lodScale": 1.0, "treeScale": 1.0, "buildingScale": 1.0, "photoPx": 2048, "viewM": 0,
            "lodPx": 0}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="the BeamNG.Earth checkout whose server runs")
    ap.add_argument("--out", required=True, help="a new folder: net.log, places.jsonl, server.log, cache, user")
    ap.add_argument("--places", nargs="+", default=PLACES, help="label=lat,lon in visiting order")
    ap.add_argument("--python", default=None, help="the server's Python (default ROOT/.venv/bin/python)")
    ap.add_argument("--quiet", type=float, default=15.0, help="s without a request a place needs to end (Settle)")
    ap.add_argument("--cap", type=float, default=1800.0, help="s a place may take at most")
    ap.add_argument("--link", default=None, help="MBIT:RTT_MS: the proxy delivers at this line's speed and latency "
                    "(link.py), the server keeps its own request limits")
    ap.add_argument("--until", choices=UNTIL, default=None, help="a place ends once this part has loaded (quicker "
                    "runs that time one milestone; bytes then stop there too)")
    ap.add_argument("--pyspy", default=None, help="record the server under py-spy into this file (folded stacks of the "
                    "threads holding the GIL, its worker processes too; py-spy from PATH, else uv tool run)")
    ap.add_argument("extra", nargs="*", help="more earthd arguments")
    a = ap.parse_args()
    if os.path.exists(os.path.join(a.out, "net.log")):
        raise SystemExit(f"{a.out} has a run already: use a new folder")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))     # (stopped: the finally below stops the server)
    srv = Server(a)
    try:
        c = srv.client()
        c.send(HELLO)
        c.until(lambda m: m["type"] == "hello", timeout=60)
        c.send(GRAPHICS)
        for n, p in enumerate(a.places, 1):
            label, ll = p.split("=")
            lat, lon = (float(v) for v in ll.split(","))
            start = srv.netlog_lines()
            t0, cpu0, epoch = time.monotonic(), srv.cpu(), time.time()
            marks = Visit(c, srv, n, lat, lon, a).run()
            marks["cpu_s"] = round(srv.cpu() - cpu0, 1)
            marks["wall_s"] = round(time.monotonic() - t0, 1)
            rec = {"label": label, "latlon": ll, "start_line": start, "end_line": srv.netlog_lines(), "t0": round(epoch, 3),
                   "link": a.link, "marks": marks}
            with open(os.path.join(a.out, "places.jsonl"), "a") as f:
                f.write(json.dumps(rec) + "\n")
            print(json.dumps(rec), flush=True)
    finally:
        srv.stop()


class Server:
    """earthd.py of --root on a free port, its own cache and user folder under --out, logging every request."""

    def __init__(self, a):
        self.out, self.root = os.path.abspath(a.out), os.path.abspath(a.root)
        py = a.python or os.path.join(self.root, ".venv", "bin", "python")
        user = os.path.join(self.out, "user")
        os.makedirs(os.path.join(user, "settings"), exist_ok=True)
        os.makedirs(os.path.join(self.out, "cache"), exist_ok=True)
        os.makedirs(os.path.join(self.out, "proc"), exist_ok=True)
        if os.path.exists(USER_SETTINGS):           # (cpupin: the server's cores and priority, as in the game)
            shutil.copy(USER_SETTINGS, os.path.join(user, "settings", "beamngearth.json"))
        self.netlog = os.path.join(self.out, "net.log")
        open(self.netlog, "w").close()
        earthd = os.path.join(self.root, "server", "earthd.py")
        known = subprocess.run([py, earthd, "--help"], capture_output=True, text=True, cwd=self.root).stdout
        cmd = [py, earthd, "--port", "0", "--user-dir", user, "--cache-dir", os.path.join(self.out, "cache"),
               "--status-interval", "1"]
        for flag, value in RECOMMENDED + HEADLESS:
            if flag in known:
                cmd += [flag, value]
        cmd += a.extra
        self.pyspy = a.pyspy
        if a.pyspy:                 # (py-spy as the server's parent: ptrace_scope 1 lets it attach to its own children only)
            spy = [shutil.which("py-spy")] if shutil.which("py-spy") else ["uv", "tool", "run", "py-spy"]
            cmd = spy + ["record", "-r", "50", "--gil", "--subprocesses", "--format", "raw", "-o",
                         os.path.abspath(a.pyspy), "--"] + cmd
        # BNE_LIDAR_GAPS_WAIT: a road build waits for each cell's lidar to the end (as fast pre-cache does), not 8 s and
        # then 1 s with the cell's reads stopped: how much of the lidar a run read followed the machine's load (with ten
        # servers at once a city's 30-110 MB, run to run, 2026-11-12); read whole it is the same every run
        env = dict(os.environ, BNE_TELEMETRY="off", BNGE_NETLOG=self.netlog, PYTHONUNBUFFERED="1",
                   BNE_NETSTATS_PROC=os.path.join(self.out, "proc"))
        if not a.link:      # (a timed run waits as a player's server does: its times are the point, not equal bytes)
            env["BNE_LIDAR_GAPS_WAIT"] = "900"
        if a.link:
            env.update(line_env(env, a.link))
        else:   # sitepatch: netfetch's per-host limits lifted in every server process (the proxy keeps its own upstream)
            env["PYTHONPATH"] = os.pathsep.join(p for p in (SITEPATCH, os.environ.get("PYTHONPATH")) if p)
        self.log = open(os.path.join(self.out, "server.log"), "w", buffering=1)
        self.log.write(" ".join(cmd) + "\n")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                                     cwd=self.root)
        self.port = None
        while self.port is None:
            line = self.proc.stdout.readline()
            if not line:
                raise SystemExit(f"the server did not start (see {self.out}/server.log)")
            self.log.write(line)
            if "listening on 127.0.0.1:" in line:
                self.port = int(line.split("127.0.0.1:")[1].split()[0])
        threading.Thread(target=self._pump, daemon=True).start()
        sys.path.insert(0, os.path.join(self.root, "server", "tests"))

    def _pump(self):
        for ln in self.proc.stdout:
            self.log.write(ln)

    def client(self):
        from netclient import Client
        return Client(self.port)

    def netlog_lines(self):
        with open(self.netlog, "rb") as f:
            return sum(1 for _ in f)

    def cpu(self):
        """CPU seconds the server and its live worker processes have used so far."""
        tick, total, root = os.sysconf("SC_CLK_TCK"), 0.0, self._server_pid()
        for p in [root] + self._descendants(root):
            try:
                with open(f"/proc/{p}/stat") as f:
                    fields = f.read().rsplit(")", 1)[1].split()
                total += (int(fields[11]) + int(fields[12])) / tick
            except (OSError, IndexError, ValueError):
                continue
        return total

    def stop(self):
        from netclient import stop_server
        if self.pyspy:              # (the server first: py-spy writes its file once its child has exited)
            with contextlib.suppress(OSError):
                os.kill(self._server_pid(), signal.SIGTERM)
            try:
                self.proc.wait(120)
            except subprocess.TimeoutExpired:
                pass
        stop_server(self.proc)
        self.log.close()

    def _server_pid(self):
        """earthd's own pid: under --pyspy the process started is uv / py-spy, the server a descendant (not counted)."""
        if not self.pyspy:
            return self.proc.pid
        for pid in self._descendants():
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    args = f.read()
            except OSError:
                continue
            if b"earthd.py" in args and b"py-spy" not in args:
                return pid
        return self.proc.pid

    def _descendants(self, pid=None):
        """Every live process below pid (default the one started; each thread's children: uv starts py-spy from a
        worker thread)."""
        out, todo = [], [self.proc.pid if pid is None else pid]
        while todo:
            p = todo.pop()
            kids = []
            try:
                for tid in os.listdir(f"/proc/{p}/task"):
                    with open(f"/proc/{p}/task/{tid}/children") as f:
                        kids += [int(k) for k in f.read().split()]
            except OSError:
                pass
            out += kids
            todo += kids
        return out


def line_env(env, spec):
    """The proxy variables of env with this server's line as their user (link.USER): link-MBIT-RTT-ID."""
    mbit, rtt = spec.split(":")
    user = f"link-{float(mbit):g}-{float(rtt):g}-{uuid.uuid4().hex[:8]}"
    out = {}
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        if env.get(var):
            scheme, rest = env[var].split("://", 1)
            out[var] = f"{scheme}://{user}:x@{rest.rsplit('@', 1)[-1]}"
    if not out:
        raise SystemExit("--link needs the cache proxy (HTTPS_PROXY: proxy.sh env)")
    return out


class Visit:
    """One teleport and the stand that follows, until the place has loaded and the network is quiet."""

    def __init__(self, c, srv, req, lat, lon, a):
        self.c, self.srv, self.req, self.lat, self.lon, self.a = c, srv, req, lat, lon, a
        self.slots, self.gone = {}, []      # far tile key -> (slot, serial); slots let go since the last pose
        self.progress = {}                  # the last load_progress: {kind: [done, wanted]}
        self.done_at = {}                   # part -> s after the teleport it has been done since

    def run(self):
        c, t0 = self.c, time.monotonic()
        c.send({"type": "prepare_anchor", "requestId": self.req, "lat": self.lat, "lon": self.lon,
                "purpose": "teleport"})
        ready, _ = c.until(lambda m: m["type"] in ("anchor_ready", "anchor_error") and m.get("requestId") == self.req,
                           timeout=self.a.cap)
        if ready["type"] != "anchor_ready":
            raise SystemExit(f"anchor error: {ready}")
        marks = {"anchor": round(time.monotonic() - t0, 1)}
        aid, gz, tr = ready["anchorId"], float(ready.get("groundZ") or 0.0), ready.get("terrain") or {}
        c.send({"type": "commit_anchor", "anchorId": aid})
        if "cx" in tr:
            c.send({"type": "commit_terrain", "anchorId": aid, "cx": tr["cx"], "cy": tr["cy"]})
        c.send({"type": "where", "id": self.req, "lat": self.lat, "lon": self.lon, "online": True})
        settle, last_pose = Settle(self.srv, self.a.quiet), 0.0
        while True:
            now = time.monotonic()
            if now - last_pose >= POSE_S:
                last_pose = now
                self.pose(aid, gz)
                reached = self.a.until and self.reached(self.a.until)
                if reached or settle.done(now, "far_done" in marks, self.progress) or now - t0 >= self.a.cap:
                    end = "until_s" if reached else "settled_s" if now - t0 < self.a.cap else "capped_s"
                    marks[end] = round(now - t0, 1)
                    marks.update({f"{k}_s": round(v, 1) for k, v in self.done_at.items()})
                    if self.reached("loaded"):
                        marks["loaded_s"] = round(max(self.done_at.values()), 1)
                    marks["progress"] = self.progress
                    return marks
            try:
                m = c.recv(0.05)
            except TimeoutError:
                continue
            self.note(m, aid, marks, t0)

    def pose(self, aid, gz):
        self.c.send({"type": "pose", "anchorId": aid, "x": 0.0, "y": 0.0, "z": gz + 1.0, "vx": 0, "vy": 0,
                     "lat": self.lat, "lon": self.lon, "speed": 0, "follow": True, "held": [], "sessions": [],
                     "slotsGone": self.gone or None,
                     "cam": {"x": 0.0, "y": -6.0, "z": gz + 3.0, "fx": 0.0, "fy": 0.995, "fz": -0.1, "fovDeg": 65,
                             "h": 1080}})
        self.gone = []

    def note(self, m, aid, marks, t0):
        typ = m.get("type")
        if m.get("anchorId") != aid:
            return
        if typ == "far_tile" and m.get("slotSerial") is not None and m.get("material"):
            self.slots[m["key"]] = (int(m["material"].rsplit("_", 1)[1]), m["slotSerial"])
        elif typ in ("far_swap", "far_remove"):
            for k in (m.get("remove", []) if typ == "far_swap" else [m["key"]]):
                if k in self.slots:
                    self.gone.append(list(self.slots.pop(k)))
        elif typ == "terrain_refresh" and "graded_s" not in marks:     # (the graded first block: refine_first)
            marks["graded_s"] = round(time.monotonic() - t0, 1)
        elif typ == "far_done" and "far_done" not in marks:
            marks["far_done"] = round(time.monotonic() - t0, 1)
        elif typ == "load_progress":
            self.progress = {k: m.get(k) for k in PARTS}
            for k, v in self.progress.items():
                if v and v[1] and v[0] >= v[1]:
                    self.done_at.setdefault(k, time.monotonic() - t0)
                else:
                    self.done_at.pop(k, None)

    def reached(self, until):
        """until's part done (loaded: every part with a count; one the server counts nothing for is not waited on)."""
        if until != "loaded":
            return until in self.done_at
        return bool(self.done_at) and all(k in self.done_at or not (self.progress.get(k) or [0, 0])[1] for k in PARTS)


class Settle:
    """When a place has loaded: far_done came, load_progress has every count done (ground, roads, trees, far), no
    request for `quiet` s, and the server is idle (under IDLE_CORES over the last IDLE_S: the photo regions' analyses
    and road plans, which fetch as they go, are done). With many servers sharing the CPU a road plan can go `quiet`
    s without a request and still have downloads ahead: the quiet alone ended places early (2026-11-12, lidar)."""
    IDLE_S, IDLE_CORES = 15.0, 0.25

    def __init__(self, srv, quiet):
        self.srv, self.quiet = srv, quiet
        self.lines, self.changed = srv.netlog_lines(), time.monotonic()
        self.cpu = collections.deque()          # (t, server CPU s), every ~1 s

    def done(self, now, far_done, progress):
        n = self.srv.netlog_lines()
        if n != self.lines:
            self.lines, self.changed = n, now
        if not self.cpu or now - self.cpu[-1][0] >= 1.0:
            self.cpu.append((now, self.srv.cpu()))
        while len(self.cpu) > 2 and now - self.cpu[1][0] >= self.IDLE_S:
            self.cpu.popleft()
        (t_a, c_a), (t_b, c_b) = self.cpu[0], self.cpu[-1]
        idle = t_b - t_a >= self.IDLE_S * 0.9 and (c_b - c_a) / (t_b - t_a) < self.IDLE_CORES
        loaded = bool(progress) and all(not v or v[0] >= v[1] for v in progress.values())
        return far_done and loaded and idle and now - self.changed >= self.quiet


if __name__ == "__main__":
    main()
