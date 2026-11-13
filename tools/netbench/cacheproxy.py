"""Read-through HTTP(S) cache for repeatable network measurements (a mitmproxy addon; proxy.sh runs it).

The world server's requests go through this proxy (HTTPS_PROXY, SSL_CERT_FILE with the proxy's CA). An answer stored
before is served from the store; anything else goes upstream once, is stored, and is logged in misses.log. The server's
own BNGE_NETLOG still counts every request it makes, so measurements see the same requests and bytes as live, while
the public hosts see each distinct request once across all runs.

Key: method, URL, Range, Accept-Encoding (and the body's hash for a POST). Bodies are stored as they came over the
wire (still gzip-encoded when they were), so byte counts match. HEAD is answered from a stored GET. Conditional
requests (If-None-Match / If-Modified-Since) are sent upstream without their condition on a miss, and answered here:
a 304 when the stored ETag matches. Only clean answers are stored (2xx, 3xx redirects, 404, 410): a 429 or a 5xx passes
through and is asked again next time. Concurrent misses for one key wait for the first one's answer.

A range read that is not stored as such is put together from stored pieces of the same file (RangeIndex: 206s and
whole 200s, unencoded, of one ETag) when they cover it: a server that batches its range reads by timing (pmtiles,
parquet) asks other ranges each run, of bytes already fetched.
"""
import asyncio
import hashlib
import json
import os
import re
import time

from mitmproxy import ctx, http

STORE_STATUS = {200, 203, 204, 206, 301, 302, 303, 307, 308, 404, 410}
CONDITIONAL = ("if-none-match", "if-modified-since")
DROP_ON_REPLAY = ("transfer-encoding", "connection", "keep-alive")
DROP_ON_ASSEMBLY = DROP_ON_REPLAY + ("content-length", "content-range", "content-encoding")
# Mirrors of one file: stored and fetched under the first's URL. pmtiles.py races Overture's three tile mirrors once
# per process and reads from the fastest; behind this proxy the fastest is whichever answer happens to be stored, so
# without the alias each run would read the same bytes under another mirror's keys (and its ETag).
ALIASES = ((re.compile(r"^https://overturemaps-extras-us-west-2\.s3(?:\.us-west-2)?\.amazonaws\.com/tiles/"),
            "https://tiles.overturemaps.org/"),)
# Answers a host gives now and then that are not the data asked for: passed through, never stored (stored, every later
# ask would get the same broken answer, where live the next one is fine). A Prometheus page: one of IGN's Geoplateforme
# backends answered a WFS GetFeature with its JMX exporter's metrics (2026-11-12, ~1 in 19 asks).
BAD_CONTENT_TYPES = ("text/plain; version=0.0.4",)
# Misses a host gets at once from here: the servers run with their own per-host limits lifted (sitepatch), so a cold
# place's misses would otherwise reach a host as fast as the server asks. 4: the regional photo hosts' own limit.
UPSTREAM_CONC = 4


def _bad(resp):
    ct = resp.headers.get("content-type", "")
    return any(ct.startswith(b) for b in BAD_CONTENT_TYPES)


def canonical(url):
    for pattern, to in ALIASES:
        url = pattern.sub(to, url)
    return url


def _key(req, method=None):
    parts = [method or req.method, canonical(req.pretty_url), req.headers.get("range", ""),
             req.headers.get("accept-encoding", "")]
    if req.method not in ("GET", "HEAD"):
        parts.append(hashlib.sha256(req.raw_content or b"").hexdigest())
    return "\n".join(parts)


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Store:
    """The answers on disk: <h[:2]>/<h>.json (status, reason, headers) and <h>.body, h = sha256 of the key."""

    def __init__(self, root):
        self.root = root

    def paths(self, h):
        d = os.path.join(self.root, h[:2])
        return os.path.join(d, h + ".json"), os.path.join(d, h + ".body")

    def meta(self, h):
        try:
            with open(self.paths(h)[0]) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def get(self, key):
        m = self.meta(_sha(key))
        if m is None:
            return None
        try:
            with open(self.paths(_sha(key))[1], "rb") as f:
                return m, f.read()
        except OSError:
            return None

    def read(self, h, start, n):
        with open(self.paths(h)[1], "rb") as f:
            f.seek(start)
            return f.read(n)

    def put(self, key, resp):
        meta, body = self.paths(_sha(key))
        os.makedirs(os.path.dirname(meta), exist_ok=True)
        _write(body, resp.raw_content or b"")
        m = {"key": key, "status": resp.status_code, "reason": resp.reason, "headers": list(resp.headers.items()),
             "stored": time.time()}
        _write(meta, json.dumps(m).encode())


def _write(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


class RangeIndex:
    """Per URL, the stored unencoded pieces of its file: ranges/<sha(url)[:2]>/<sha(url)>.idx, one JSON line a piece:
    [first byte, last byte, file size or None, ETag or None, the piece's key hash]."""

    def __init__(self, store):
        self.store = store
        self.root = os.path.join(store.root, "ranges")
        if not os.path.isdir(self.root):
            self._rebuild()

    def _path(self, url):
        h = _sha(url)
        return os.path.join(self.root, h[:2], h + ".idx")

    def add(self, url, meta, h):
        hdr = {k.lower(): v for k, v in meta["headers"]}
        if hdr.get("content-encoding", "identity") != "identity":
            return
        if meta["status"] == 206:
            m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", hdr.get("content-range", "").strip())
            if not m:
                return
            piece = [int(m[1]), int(m[2]), None if m[3] == "*" else int(m[3])]
        elif meta["status"] == 200 and "content-length" in hdr and not meta["key"].split("\n")[2]:
            n = int(hdr["content-length"])
            if n == 0:
                return
            piece = [0, n - 1, n]
        else:
            return
        path = self._path(url)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(piece + [hdr.get("etag"), h]) + "\n")

    def _rebuild(self):
        """The index of a store made before it existed."""
        os.makedirs(self.root, exist_ok=True)
        for d in os.listdir(self.store.root):
            if len(d) != 2 or not os.path.isdir(os.path.join(self.store.root, d)):
                continue
            for name in os.listdir(os.path.join(self.store.root, d)):
                if name.endswith(".json"):
                    m = self.store.meta(name[:-5])
                    if m and m["key"].startswith("GET\n"):
                        self.add(m["key"].split("\n")[1], m, name[:-5])

    def assemble(self, req):
        """A 206 for req's single Range from stored pieces of one ETag that cover it, or None."""
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", req.headers.get("range", "").strip())
        if req.method != "GET" or not m or not (m[1] or m[2]):
            return None
        try:
            with open(self._path(canonical(req.pretty_url))) as f:
                pieces = [json.loads(line) for line in f]
        except (OSError, ValueError):
            return None
        for etag in {p[3] for p in pieces}:
            same = [p for p in pieces if p[3] == etag]
            total = next((p[2] for p in same if p[2] is not None), None)
            span = _span(m[1], m[2], total)
            if span is None:
                continue
            body, last = self._cover(same, *span)
            if body is not None:
                return self._response(last, span, total, body)
        return None

    def _cover(self, pieces, a, b):
        """(the bytes a..b from the pieces, the last piece used), or (None, None) when they leave a gap."""
        out, pos, last = [], a, None
        while pos <= b:
            best = max((p for p in pieces if p[0] <= pos <= p[1]), key=lambda p: p[1], default=None)
            if best is None:
                return None, None
            end = min(best[1], b)
            try:
                out.append(self.store.read(best[4], pos - best[0], end - pos + 1))
            except OSError:
                return None, None
            pos, last = end + 1, best
        return b"".join(out), last

    def _response(self, piece, span, total, body):
        meta = self.store.meta(piece[4])
        hdrs = http.Headers([(k.encode(), v.encode()) for k, v in meta["headers"]
                             if k.lower() not in DROP_ON_ASSEMBLY])
        resp = http.Response.make(206, b"", hdrs)
        resp.reason = "Partial Content"
        resp.headers["content-range"] = f"bytes {span[0]}-{span[1]}/{total if total is not None else '*'}"
        resp.headers["content-length"] = str(len(body))
        resp.raw_content = body
        return resp


def _span(first, last, total):
    """(a, b) of a Range header's 'first-last' (either may be empty) for a file of total bytes (None: unknown)."""
    if not first:
        if total is None:
            return None
        return max(0, total - int(last)), total - 1
    a = int(first)
    b = int(last) if last else (total - 1 if total is not None else None)
    if b is None:
        return None
    if total is not None:
        b = min(b, total - 1)
    return (a, b) if a <= b else None


def _replay(req, m, body, head=False):
    hdrs = http.Headers([(k.encode(), v.encode()) for k, v in m["headers"]
                         if k.lower() not in DROP_ON_REPLAY and k.lower() != "content-length"])
    resp = http.Response.make(m["status"], b"", hdrs)
    resp.reason = m.get("reason") or ""
    etag = resp.headers.get("etag")
    inm = req.headers.get("if-none-match")
    if m["status"] in (200, 206) and etag and inm and etag in [t.strip() for t in inm.split(",")]:
        resp.status_code, resp.reason, body = 304, "Not Modified", b""
    elif m["status"] in (200, 206) and not inm and req.headers.get("if-modified-since") \
            and req.headers.get("if-modified-since") == resp.headers.get("last-modified"):
        resp.status_code, resp.reason, body = 304, "Not Modified", b""
    resp.headers["content-length"] = str(len(body))
    resp.raw_content = b"" if head else body
    return resp


class CacheProxy:
    def __init__(self):
        self.inflight = {}              # key -> asyncio.Event of the miss being fetched
        self.gates = {}                 # host -> asyncio.Semaphore: UPSTREAM_CONC misses at once
        self.counts = {"hit": 0, "assembled": 0, "miss": 0}
        self.store = self.ranges = None

    def load(self, loader):
        loader.add_option("cache_dir", str, "", "the store's folder")

    def configure(self, updated):
        if "cache_dir" in updated and ctx.options.cache_dir:
            self.store = Store(ctx.options.cache_dir)
            self.ranges = RangeIndex(self.store)

    def _log(self, name, req, status, n):
        with open(os.path.join(self.store.root, name), "a") as f:
            f.write(f"{time.time():.3f} {req.method} {status} {n} {req.pretty_url} {req.headers.get('range', '-')}\n")

    async def request(self, flow):
        req = flow.request
        key = _key(req)
        while True:
            got = self.store.get(key)
            if got is None and req.method == "HEAD":
                got = self.store.get(_key(req, "GET"))
            if got is not None:
                flow.response = _replay(req, *got, head=req.method == "HEAD")
                self.counts["hit"] += 1
                return
            if not any(h in req.headers for h in CONDITIONAL):
                resp = self.ranges.assemble(req)
                if resp is not None:
                    flow.response = resp
                    self.counts["assembled"] += 1
                    return
            ev = self.inflight.get(key)
            if ev is None:
                break
            await ev.wait()
        self.inflight[key] = asyncio.Event()
        if canonical(req.pretty_url) != req.pretty_url:
            req.url = canonical(req.pretty_url)     # (a mirror's miss is fetched from the first mirror)
        flow.metadata["cacheproxy_key"] = key
        flow.metadata["cacheproxy_cond"] = {h: req.headers[h] for h in CONDITIONAL if h in req.headers}
        for h in CONDITIONAL:           # (the full answer upstream, so it can be stored; the condition is answered here)
            req.headers.pop(h, None)
        gate = self.gates.setdefault(req.host, asyncio.Semaphore(UPSTREAM_CONC))
        await gate.acquire()
        flow.metadata["cacheproxy_gate"] = gate

    def response(self, flow):
        key = flow.metadata.get("cacheproxy_key")
        if key is None:
            return
        req, resp = flow.request, flow.response
        self.counts["miss"] += 1
        self._log("misses.log", req, resp.status_code, len(resp.raw_content or b""))
        if resp.status_code in STORE_STATUS and not _bad(resp):
            self.store.put(key, resp)
            if req.method == "GET":
                self.ranges.add(canonical(req.pretty_url), self.store.meta(_sha(key)), _sha(key))
            cond = flow.metadata.get("cacheproxy_cond") or {}
            if cond:                    # (the client asked conditionally: answer its condition from what was stored)
                req.headers.update(cond)
                flow.response = _replay(req, *self.store.get(key), head=req.method == "HEAD")
        self._release(flow, key)

    def error(self, flow):
        key = flow.metadata.get("cacheproxy_key")
        if key is not None:
            self._log("errors.log", flow.request, "ERR", 0)
            self._release(flow, key)

    def _release(self, flow, key):
        gate = flow.metadata.pop("cacheproxy_gate", None)
        if gate is not None:
            gate.release()
        ev = self.inflight.pop(key, None)
        if ev is not None:
            ev.set()

    def done(self):
        ctx.log.info(f"cacheproxy: {self.counts}")


addons = [CacheProxy()]
