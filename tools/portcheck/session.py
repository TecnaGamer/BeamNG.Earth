#!/usr/bin/env python3
"""session.py: a real world-server session per corpus place, in the players' layout, recorded for the byte-identity
comparisons (tilecmp.py, blkcmp.py, farcmp.py, msgcmp.py; plancmp.py --sub splans).

    python3 tools/portcheck/session.py --cache CORPUS_CACHE --out OUT [--places a,b] [--server-dir TREE/server]
                                       [--online] [--budget small]

Starts the tree's server (earthd.py with the launcher's recommended options: detail ground, deshadow, airports, sea,
trees, buildings, gas stations) on a work copy of the corpus cache (offline) and a fresh user folder, and plays the
mod's side as core.lua does: its hello (every capability of the mod tree beside the server: geotiles, road / far /
global geotiles, flat vegetation, deck guard, ready set ...), the graphics message (the place's view_m), a declared
teleport to the place, commit, then a pose every 0.25 s with a chase camera looking north, pooled shapes given back
(slotsGone) as their far tiles go. It ends settle_s after far_done once the loading line is all done and no message
has named a file for QUIET_S (or at --timeout).

Recorded in OUT/sessions/<place>/:
  messages.jsonl   every message received ({"i", "t", "m"}), each file path in it (a string under /temp/beamngEarth/)
                   replaced by "@<sha256><ext>" of the file's bytes read when the message came (pooled shape files are
                   rewritten later); paths inside a named JSON file are followed the same way; the session's own
                   names masked (its id; the pooled far materials' id and slot, also inside the unpacked shapes)
  blobs/           those files by content (<sha256><ext>)
  server.log
OUT/splans/<place>/: the run's road plans (the plan cache's files) and index.json, as build.py's OUT/plans.

Kept items are never read or written (BNE_REVISIT=off and pcommon.PINNED_ENV: no kept blocks, no CDN decision archives,
lidar road heights or DEM release): the tree's code builds everything.
--online: fill the corpus cache itself (imagery, far elevation; once, before it is frozen). See pcommon.py.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcommon  # noqa: E402

RECOMMENDED = ["--deshadow", "full", "--near-visual", "detail", "--airports", "on", "--sea", "on", "--trees", "on",
               "--buildings", "on", "--gas-stations", "on"]        # (launcher.RECOMMENDED: test_portcheck checks it)
VFS = "/temp/beamngEarth/"
# the session's own names, masked in what is recorded: earthd's session id (Server.sid: time + 4 hex) and the pooled
# far materials named by it and their slot (bngEarthFar_<4 hex>_<slot>: which slot a tile gets is the send order's)
SID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
MAT_RE = re.compile(r"(bngEarth[A-Za-z]*)_[0-9a-f]{4}_\d{5}")
SID_RE_B = re.compile(rb"\d{8}-\d{6}-[0-9a-f]{4}")
MAT_RE_B = re.compile(rb"(bngEarth[A-Za-z]*)_[0-9a-f]{4}_\d{5}")
QUIET_S = 20.0              # s without a message naming a file before the session may end (the horizon ring, the
                            # far bridges and retextures come late: 5 s ended runs before them, other tiles in each)
# the mod's hello capabilities: (field, Lua file under the mod's beamngEarth folder, the constant's pattern)
CAPS = (("geoTiles", "geotile.lua", r"^M\.Z\s*=\s*(\d+)"),
        ("roadGeo", "roads.lua", r"^M\.ROAD_GEO\s*=\s*(\d+)"),
        ("farGeo", "roads.lua", r"^M\.FAR_GEO\s*=\s*(\d+)"),
        ("farPack", "roads.lua", r"^M\.FAR_PACK\s*=\s*(\d+)"),
        ("farHz", "roads.lua", r"^M\.FAR_HZ\s*=\s*(\d+)"),
        ("turbineGeo", "turbinestream.lua", r"PROTOCOL\s*=\s*(\d+)"),
        ("globalFar", "farfield.lua", r"^M\.GLOBAL_FAR\s*=\s*(\d+)"),
        ("deckGuard", "deckguard.lua", r"^M\.PROTO\s*=\s*(\d+)"),
        ("waterguard", "sea.lua", r"^M\.GUARD_VERSION\s*=\s*(\d+)"))


def mod_hello(server_dir):
    """The hello core.lua sends (its constants read from the mod tree beside server_dir)."""
    lua = os.path.join(os.path.dirname(server_dir), "mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth")
    def read(name):
        with open(os.path.join(lua, name), encoding="utf-8") as f:
            return f.read()
    core = read("core.lua")
    proto = int(re.search(r"^local PROTOCOL\s*=\s*(\d+)", core, re.M).group(1))
    fmt = int(re.search(r"^local FORMAT\s*=\s*(\d+)", core, re.M).group(1))
    hello = {"type": "hello", "protocol": proto, "format": fmt, "modVersion": "portcheck", "streamLease": 1,
             "sessions": [], "slotsHeld": [], "held": [], "vegFlat": True, "swapGen": 1, "readySet": 1}
    for field, name, pat in CAPS:
        m = re.search(pat, read(name), re.M)
        if m is None:
            raise SystemExit(f"session.py: the mod's {field} ({name}) not found: update CAPS")
        hello[field] = int(m.group(1))
    return hello


class Recorder:
    """messages.jsonl and blobs/: each named file captured by content when its message comes."""

    def __init__(self, out, user):
        self.out, self.user = out, user
        self.blobs = os.path.join(out, "blobs")
        os.makedirs(self.blobs, exist_ok=True)
        self.f = open(os.path.join(out, "messages.jsonl"), "w", encoding="utf-8")
        self.n, self.t0, self.last_file = 0, time.monotonic(), time.monotonic()
        self.progress = None        # (the last load_progress: the session ends once it is all done)
        self.memo = {}

    def capture(self, vpath, depth=0):
        path = os.path.join(self.user, vpath.lstrip("/"))
        ext = os.path.splitext(vpath)[1].lower()
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return "@missing:" + os.path.basename(vpath)
        data = self.norm_bytes(data, ext, depth)
        name = pcommon.sha(data) + ext
        if name not in self.memo:
            with open(os.path.join(self.blobs, name), "wb") as f:
                f.write(data)
            self.memo[name] = True
        self.last_file = time.monotonic()
        return "@" + name

    def norm_bytes(self, data, ext, depth):
        """A file as compared: a JSON file's paths by content and its session names masked; a shape's body unpacked
        (cdae.plain: the zstd frame) with the session names masked in place (same length); the rest as it is."""
        if ext == ".json" and depth < 3:
            try:
                return (json.dumps(self.norm(json.loads(data), depth + 1), ensure_ascii=False,
                                   separators=(",", ":")) + "\n").encode("utf-8")
            except ValueError:
                return data
        if ext == ".cdae":
            import cdae
            try:
                data = cdae.plain(data)
            except Exception:       # (not a packed shape: as it is)
                pass
            data = SID_RE_B.sub(lambda m: b"S" * len(m.group(0)), data)
            return MAT_RE_B.sub(lambda m: m.group(1) + b"_xxxx_xxxxx", data)
        return data

    def norm(self, v, depth=0):
        if isinstance(v, str):
            if v.startswith(VFS):
                return self.capture(v, depth)
            return MAT_RE.sub(lambda m: m.group(1) + "_xxxx_xxxxx", SID_RE.sub("<session>", v))
        if isinstance(v, list):
            return [self.norm(x, depth) for x in v]
        if isinstance(v, dict):
            return {k: self.norm(x, depth) for k, x in v.items()}
        return v

    def add(self, m):
        rec = {"i": self.n, "t": round(time.monotonic() - self.t0, 3), "m": self.norm(m)}
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.n += 1

    def close(self):
        self.f.close()


def run_session(a, name, place, work, out):
    sys.path.insert(0, a.server_dir)
    sys.path.insert(0, os.path.join(a.server_dir, "tests"))
    from netclient import Client, stop_server
    user = os.path.join(out, "user")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(os.path.join(user, "settings"))
    with open(os.path.join(user, "settings", "beamngEarth.json"), "w") as f:
        json.dump({"teleportPriority": True}, f)
    cmd = [sys.executable, os.path.join(a.server_dir, "earthd.py"), "--port", "0", "--user-dir", user,
           "--cache-dir", work, "--update-check", "off", "--telemetry", "off", "--companion-feed", "off"] + RECOMMENDED
    if not a.online:
        cmd.append("--offline")
    env = dict(os.environ, PYTHONUNBUFFERED="1", BNE_REVISIT="off", **pcommon.PINNED_ENV)
    if a.budget:
        env["BNGE_BUDGET"] = a.budget
    logf = open(os.path.join(out, "server.log"), "w")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=logf, text=True, env=env)
    port = None
    while port is None:
        line = proc.stdout.readline()
        if not line:
            raise SystemExit(f"{name}: the server did not start (see {out}/server.log)")
        logf.write(line)
        if "listening on 127.0.0.1:" in line:
            port = int(line.split("127.0.0.1:")[1].split()[0])
    threading.Thread(target=lambda: [logf.write(ln) for ln in proc.stdout], daemon=True).start()
    rec = Recorder(out, user)
    c = Client(port)
    t0 = time.monotonic()
    try:
        summary = play(a, c, rec, place)
    finally:
        c.close()
        rec.close()
        stop_server(proc)
        logf.close()
    shutil.rmtree(user, ignore_errors=True)
    summary["seconds"] = round(time.monotonic() - t0, 1)
    summary["messages"] = rec.n
    return summary


def play(a, c, rec, place):
    lat, lon = place["lat"], place["lon"]

    def recv(timeout):
        m = c.recv(timeout)
        rec.add(m)
        return m
    c.send(mod_hello(a.server_dir))
    while recv(30)["type"] != "hello":
        pass
    c.send({"type": "graphics", "lodScale": 1.0, "treeScale": 1.0, "buildingScale": 1.0, "photoPx": 2048,
            "viewM": float(place["view_m"])})
    c.send({"type": "prepare_anchor", "requestId": 1, "lat": lat, "lon": lon, "purpose": "teleport"})
    while True:
        m = recv(600)
        if m["type"] == "anchor_error":
            raise SystemExit(f"anchor error: {m}")
        if m["type"] == "anchor_ready":
            break
    aid, gz = m["anchorId"], float(m.get("groundZ") or 0.0)
    c.send({"type": "commit_anchor", "anchorId": aid})
    slots, gone = {}, []
    end, last_pose, done_at = time.monotonic() + a.timeout, 0.0, None
    while time.monotonic() < end:
        now = time.monotonic()
        if done_at is not None and now - done_at >= place.get("settle_s", 20) and now - rec.last_file >= QUIET_S \
                and loaded(rec.progress):
            break
        if now - last_pose >= 0.25:
            last_pose = now
            c.send({"type": "pose", "anchorId": aid, "x": 0.0, "y": 0.0, "z": gz + 1.0, "vx": 0, "vy": 0, "lat": lat,
                    "lon": lon, "speed": 0, "follow": True, "held": [], "sessions": [], "slotsGone": gone or None,
                    "cam": {"x": 0.0, "y": -6.0, "z": gz + 3.0, "fx": 0.0, "fy": 0.995, "fz": -0.1, "fovDeg": 65,
                            "h": 1080}})
            gone = []
        try:
            m = recv(0.05)
        except TimeoutError:
            continue
        typ = m.get("type")
        if m.get("anchorId") != aid:
            continue
        if typ == "load_progress":
            rec.progress = m
        if typ == "far_tile" and m.get("slotSerial") is not None and m.get("material"):
            slots[m["key"]] = (int(m["material"].rsplit("_", 1)[1]), m["slotSerial"])
        elif typ in ("far_swap", "far_remove"):
            for k in (m.get("remove", []) if typ == "far_swap" else [m["key"]]):
                if k in slots:
                    gone.append(list(slots.pop(k)))
        elif typ == "far_done" and done_at is None:
            done_at = time.monotonic()
    return {"anchor": aid, "far_done": done_at is not None}


def loaded(p):
    """The loading line's counts all done (ground, roads, trees, far: [done, wanted]) and no download in flight."""
    if p is None:
        return False
    try:
        return all(p[k][0] >= p[k][1] for k in ("ground", "roads", "trees", "far")) and not p.get("net", [0])[0]
    except (KeyError, TypeError, IndexError):
        return True             # (another shape: the quiet rule alone)


def copy_plans(work, dst):
    """The run's plan files (the plan cache's <work>/plans/<run>/<frames>/<signature>/z_x_y.npz) and their index."""
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst)
    index = {}
    for p in sorted(glob.glob(os.path.join(work, "plans", "*", "*", "*", "*.npz"))):
        cell = os.path.basename(p)[:-4]
        shutil.copyfile(p, os.path.join(dst, cell + ".npz"))
        index[cell] = {"complete": not os.path.exists(p + ".short")}
    pcommon.write_json(os.path.join(dst, "index.json"), {"plans": index})
    return len(index)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", required=True, help="the corpus cache (a folder of its own)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--places", default="", help="comma-separated corpus places (default: all)")
    ap.add_argument("--server-dir", default=pcommon.SERVER)
    ap.add_argument("--online", action="store_true", help="fill the corpus cache (downloads) instead of a work copy")
    ap.add_argument("--budget", default="small", help="BNGE_BUDGET for the server ('' for the machine's own; the same "
                    "on both sides of a comparison)")
    ap.add_argument("--timeout", type=float, default=900.0, help="s of posing at most after the anchor")
    a = ap.parse_args()
    a.cache, a.out = os.path.abspath(os.path.expanduser(a.cache)), os.path.abspath(os.path.expanduser(a.out))
    a.server_dir = os.path.abspath(a.server_dir)
    pcommon.check_cache(a.cache)
    pcommon.check_apart(a.cache, a.out)
    pcommon.check_cache(a.out)
    pcommon.nice_self()
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(a.cache, exist_ok=True)
    for name, place in pcommon.places(a.places):
        work = a.cache if a.online else pcommon.work_copy(a.cache, os.path.join(a.out, "work-cache"))
        try:
            s = run_session(a, name, place, work, os.path.join(a.out, "sessions", name))
            s["plans"] = copy_plans(work, os.path.join(a.out, "splans", name))
        finally:
            if a.online:
                pcommon.prune_derived(a.cache)
            else:
                shutil.rmtree(work, ignore_errors=True)
        pcommon.write_json(os.path.join(a.out, "sessions", name, "summary.json"), s)
        print(f"{name}: {json.dumps(s)}", flush=True)


if __name__ == "__main__":
    main()
