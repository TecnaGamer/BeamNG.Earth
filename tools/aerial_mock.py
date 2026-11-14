#!/usr/bin/env python3
"""A local stand-in for the 1 m aerial packs' tile host and the mini PC's build service (tests only; server/aerialpacks.py,
tools/build_aerial_packs.py):

    aerial_mock.py ROOT [--port 8765] [--build "python3 tools/build_aerial_packs.py"] [--build-delay S]

GET  /aerial/v1/<path>     ROOT/<path> (packs g{gen}/{z}/{px}/{py}.bngp, coverage cov/{x}/{y}.json); 404 when missing
POST /aerial/request       {"v": 1, "cell": [x, y]}: the build service's answer ({"state": "queued" | "known" |
                           "refused"}); with --build each new cell is built into ROOT by that command (+ " ROOT --cells
                           x,y --cov-in ROOT"), one at a time, as the mini PC would
GET  /stats                bytes and requests served, build requests seen (JSON)

Point a server at it: BNGE_AERIAL_BASE=http://127.0.0.1:28766/aerial/v1 BNGE_AERIAL_REQUEST_URL=http://127.0.0.1:28766/aerial/request
BNGE_AERIAL=on. Binds 127.0.0.1 only.
"""
import argparse
import json
import os
import queue
import shlex
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATS = {"requests": 0, "bytes": 0, "not_found": 0, "asked": [], "built": []}
LOCK = threading.RLock()
JOBS = queue.Queue()


def make_handler(root, build):
    seen = set()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/octet-stream"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            with LOCK:
                STATS["requests"] += 1
                STATS["bytes"] += len(body)

        def do_GET(self):
            p = self.path.split("?", 1)[0]
            if p == "/stats":
                with LOCK:
                    return self._send(200, json.dumps(STATS).encode(), "application/json")
            if not p.startswith("/aerial/v1/"):
                return self._send(404, b"")
            rel = os.path.normpath(p[len("/aerial/v1/"):])
            if rel.startswith("..") or os.path.isabs(rel):
                return self._send(404, b"")
            f = os.path.join(root, rel)
            try:
                with open(f, "rb") as fh:
                    body = fh.read()
            except OSError:
                with LOCK:
                    STATS["not_found"] += 1
                return self._send(404, b"")
            self._send(200, body, "application/json" if f.endswith(".json") else "application/octet-stream")

        def do_POST(self):
            if self.path.split("?", 1)[0] != "/aerial/request":
                return self._send(404, b"")
            n = int(self.headers.get("Content-Length") or 0)
            try:
                doc = json.loads(self.rfile.read(min(n, 4096)))
                cx, cy = (int(v) for v in doc["cell"])
                if doc.get("v") != 1 or not (0 <= cx < 2048 and 0 <= cy < 2048):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                return self._send(400, b'{"state":"refused"}', "application/json")
            with LOCK:
                STATS["asked"].append([cx, cy])
                new = (cx, cy) not in seen
                seen.add((cx, cy))
            if new and build:
                JOBS.put((cx, cy))
            self._send(200, json.dumps({"state": "queued" if new else "known"}).encode(), "application/json")

    return H


def builder(root, build, delay):
    while True:
        cx, cy = JOBS.get()
        cmd = shlex.split(build) + [root, "--cells", f"{cx},{cy}", "--cov-in", root, "--gen", "1"]
        if delay:
            threading.Event().wait(delay)
        r = subprocess.run(cmd, capture_output=True, text=True)
        with LOCK:
            STATS["built"].append({"cell": [cx, cy], "status": r.returncode, "out": r.stdout.strip()[-400:],
                                   "err": r.stderr.strip()[-400:]})


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root")
    ap.add_argument("--port", type=int, default=28766)
    ap.add_argument("--build", default="")
    ap.add_argument("--build-delay", type=float, default=0.0)
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    if a.build:
        threading.Thread(target=builder, args=(root, a.build, a.build_delay), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(root, a.build))
    print(f"aerial mock on http://127.0.0.1:{a.port}/aerial/v1 serving {root}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
