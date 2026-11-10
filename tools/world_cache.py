#!/usr/bin/env python3
"""Export, serve and fetch immutable road-plan bundles before starting the game.

The included HTTP server binds only to loopback. Remote hosting needs an operator's
HTTPS service. This is not a BeamMP server or a complete-world terrain cache.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import worldbundle


def endpoint(url):
    u = urlsplit(url)
    if (u.scheme not in ("http", "https") or not u.hostname or u.username or u.password
            or u.query or u.fragment or any(ord(c) < 33 for c in url)):
        raise ValueError("Use an HTTPS base URL without credentials, query or fragment")
    if u.scheme == "http" and u.hostname not in ("127.0.0.1", "::1"):
        raise ValueError("Plain HTTP is allowed only for literal loopback addresses")
    _ = u.port  # reject malformed port before any network activity
    return u


def download(base, path, limit, timeout=20):
    if not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
        raise ValueError("Download timeout must be finite and positive")
    u = endpoint(base)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = cls(u.hostname, u.port, timeout=timeout)
    deadline = time.monotonic() + timeout
    response = transport = None
    expired = threading.Event()

    def expire():
        expired.set()
        # HTTP/1.0 and Connection: close detach conn.sock while the response's
        # makefile still owns it. Retain that socket to interrupt body reads too.
        for current in (transport, conn.sock):
            if current is not None:
                try:
                    current.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                # On Windows shutdown alone does not wake a timed socket's
                # select(). close() also defers while a makefile owns it. Detach
                # invalidates this socket object before closing its descriptor,
                # so response cleanup cannot later close a reused descriptor.
                try:
                    descriptor = current.detach()
                    if descriptor >= 0:
                        socket.close(descriptor)
                except OSError:
                    pass

    # Socket timeouts reset on each recv; a server can otherwise trickle headers
    # indefinitely. The watchdog interrupts blocking header/body reads at the
    # wall deadline. System DNS resolution remains managed by the OS resolver.
    watchdog = threading.Timer(timeout, expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        conn.request("GET", u.path.rstrip("/") + "/" + path,
                     headers={"User-Agent": "BeamNG-Earth-WorldCache/1", "Accept-Encoding": "identity"})
        transport = conn.sock
        if expired.is_set():
            raise TimeoutError("Bundle request timed out")
        response = conn.getresponse()
        # http.client never follows redirects. Only this fixed origin is used.
        if response.status != 200:
            raise ValueError(f"Bundle server returned HTTP {response.status}")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) > limit):
            raise ValueError("Invalid or oversized bundle response")
        data = bytearray()
        while True:
            left = deadline - time.monotonic()
            if expired.is_set() or left <= 0:
                raise TimeoutError("Bundle request timed out")
            if conn.sock is not None:
                conn.sock.settimeout(left)
            part = response.read1(min(65536, limit + 1 - len(data)))
            if expired.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("Bundle request timed out")
            if not part:
                break
            data.extend(part)
            if len(data) > limit:
                raise ValueError("Bundle response exceeds limit")
        if length is not None and len(data) != int(length):
            raise ValueError("Truncated bundle response")
        return bytes(data)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        if expired.is_set() or time.monotonic() >= deadline:
            raise TimeoutError("Bundle request timed out") from exc
        raise
    finally:
        watchdog.cancel()
        watchdog.join()
        if response is not None:
            response.close()
        conn.close()


def fetch(base, destination, expected_id, max_bytes=512 << 20):
    """Return an immutable local bundle directory; leave old bundles intact on failure.

    A warm matching bundle requires no network. Activation is explicit: the caller
    uses the returned directory as BNGE_WORLD_BUNDLE for the next game session.
    """
    endpoint(base)
    if not re.fullmatch(r"[a-f0-9]{64}", expected_id):
        raise ValueError("Expected manifest ID must be a lowercase SHA256")
    if max_bytes < 1:
        raise ValueError("Cache cap must be positive")
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / expected_id
    if target.exists():
        worldbundle.validate_bundle(target, expected_id)
        return target, {"requests": 0, "bytes": 0, "warm": True}
    # A single local installer owns quota accounting and activation. A stale lock
    # is reported, never taken over while another process might still be writing.
    lock = destination / ".fetch.lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    stage = None
    try:
        os.close(fd)
        if target.exists():
            worldbundle.validate_bundle(target, expected_id)
            return target, {"requests": 0, "bytes": 0, "warm": True}
        raw = download(base, "manifest.json", worldbundle.MAX_MANIFEST_BYTES)
        manifest = worldbundle.parse_manifest(raw, expected_id)
        artifacts = list({a["sha256"]: a for a in manifest["artifacts"]}.values())
        used = sum(p.stat().st_size for p in destination.rglob("*") if p.is_file())
        required = len(raw) + sum(a["size"] for a in artifacts)
        if used + required > max_bytes:
            raise ValueError("Bundle cache cap exceeded; remove an unused bundle explicitly")
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=destination))
        (stage / "blobs").mkdir()
        (stage / "manifest.json").write_bytes(raw)

        def one(artifact):
            name = artifact["sha256"] + ".npz"
            data = download(base, "blobs/" + name, artifact["size"])
            worldbundle.validate_artifact(data, artifact)
            (stage / "blobs" / name).write_bytes(data)
            return len(data)

        with ThreadPoolExecutor(max_workers=4) as pool:
            sizes = list(pool.map(one, artifacts))
        worldbundle.validate_bundle(stage, expected_id)
        os.replace(stage, target)
        stage = None
        return target, {"requests": 1 + len(sizes), "bytes": len(raw) + sum(sizes), "warm": False}
    finally:
        if stage is not None:
            # stage is created by mkdtemp directly beneath the verified destination.
            if stage.parent == destination and stage.name.startswith(".stage-"):
                shutil.rmtree(stage)
        lock.unlink(missing_ok=True)


def make_server(bundle, port=47622):
    """Loopback-only immutable artifact service, no listing or filesystem routing."""
    bundle = Path(bundle).resolve()
    manifest = worldbundle.validate_bundle(bundle)
    manifest_bytes = (bundle / "manifest.json").read_bytes()
    routes = {"/blobs/" + a["sha256"] + ".npz": a for a in manifest["artifacts"]}
    slots = threading.BoundedSemaphore(8)
    stats = {"requests": 0, "bytes": 0}
    guard = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args):
            pass  # no client addresses, request targets or credentials in logs

        def do_GET(self):
            if self.path == "/manifest.json":
                data = manifest_bytes
            elif self.path in routes:
                a = routes[self.path]
                path = bundle / "blobs" / (a["sha256"] + ".npz")
                try:
                    with path.open("rb") as f:
                        data = f.read(a["size"] + 1)
                    if len(data) != a["size"] or hashlib.sha256(data).hexdigest() != a["sha256"]:
                        raise ValueError("artifact changed")
                except (OSError, ValueError):
                    self.send_error(503, "Artifact unavailable")
                    return
            else:
                self.send_error(404)
                return
            # Count accepted requests before any response becomes visible to the
            # client. Recording after write races with a client's next request.
            with guard:
                stats["requests"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if self.path == "/manifest.json" else "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            with guard:
                stats["bytes"] += len(data)

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def process_request(self, request, client_address):
            if not slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except Exception:
                slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                slots.release()

    server = Server(("127.0.0.1", port), Handler)
    server.stats = stats
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    export = subs.add_parser("export")
    export.add_argument("plan_dir")
    export.add_argument("output")
    export.add_argument("--sources", required=True, help="JSON file of actual map-data versions")
    export.add_argument("--input-digest", required=True, help="operator-recorded SHA256 of source snapshot")
    export.add_argument("--credit", action="append", required=True)
    serve = subs.add_parser("serve")
    serve.add_argument("bundle")
    serve.add_argument("--port", type=int, default=47622)
    get = subs.add_parser("fetch")
    get.add_argument("url")
    get.add_argument("destination")
    get.add_argument("--manifest-id", required=True)
    get.add_argument("--max-cache-mb", type=int, default=512)
    check = subs.add_parser("verify")
    check.add_argument("bundle")
    versions = subs.add_parser("source-versions", help="Read locally known map-data versions without downloads")
    versions.add_argument("cache_dir")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            m = worldbundle.build_bundle(args.plan_dir, args.output,
                 json.loads(Path(args.sources).read_text(encoding="utf-8")), args.input_digest, args.credit)
            print(json.dumps({"manifest_id": m["id"], "artifacts": len(m["artifacts"])}))
        elif args.command == "fetch":
            path, stats = fetch(args.url, args.destination, args.manifest_id, args.max_cache_mb << 20)
            print(json.dumps({"bundle": str(path), **stats}))
        elif args.command == "verify":
            m = worldbundle.validate_bundle(args.bundle)
            print(json.dumps({"manifest_id": m["id"], "verified": True}))
        elif args.command == "source-versions":
            import mapdata
            import netfetch
            fetcher = netfetch.Fetcher(offline=True)
            try:
                md = mapdata.MapData(args.cache_dir, fetcher=fetcher)
                try:
                    print(json.dumps(md.bundle_versions(), sort_keys=True))
                finally:
                    md.close()
            finally:
                fetcher.close()
        else:
            with make_server(args.bundle, args.port) as server:
                print(f"Road-plan cache listening on http://127.0.0.1:{server.server_port}", flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
        return 0
    except (ValueError, OSError, RuntimeError, http.client.HTTPException) as e:
        print(f"World cache: {type(e).__name__}: operation failed; no bundle activated", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
