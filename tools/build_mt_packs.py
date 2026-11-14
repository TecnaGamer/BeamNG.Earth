#!/usr/bin/env python3
"""Tile packs (server/tilepack.py) of Mapterhorn's elevation tiles z0-12 for the project's own tile host (server/dem.py
MT_CDN_*; the data repo's dem workflow), repacked from Mapterhorn's planet.pmtiles as they are: the tiles' bytes
unchanged (the same as tiles.mapterhorn.com serves: compared tile for tile at 8 places, 2026-11-11). Mapterhorn
(https://mapterhorn.com/attribution) allows storing and redistributing its tiles with attribution; its maintainer asked
apps to use the archives rather than the tile endpoint.

The archive (v0.0.13: 331 GiB, 13.5 M tiles z0-12, clustered: tile data in tile-id order) is read by HTTP range reads,
never stored whole: its directories (~21 MB) once, then each shard's tiles in reads of up to CHUNK_MB. A pack is the 8 x
8 tiles of a zoom under one tile three zooms up (zooms 0-2: the whole zoom); its tiles are one run of tile ids (the
Hilbert curve visits a block's tiles together), so a shard (a run of tile ids, plan) is one run of bytes.

    build_mt_packs.py plan --shards 128                      # JSON: the archive's ETag, size and the shards' tile ids
    build_mt_packs.py build OUT --range T0 T1 --etag E       # the packs of tile ids T0..T1 -> OUT/{z}/{px}/{py}.bngp
    build_mt_packs.py index OUT/index.bin                    # which packs exist (tilepack.index_*)
    build_mt_packs.py md5 --expect MD5                       # the whole archive streamed through MD5 (no disk)
    build_mt_packs.py stats                                  # packs per zoom, their bytes (the cost estimate)

--source: the archive's URL (default Source Cooperative's mirror; download.mapterhorn.com holds the same bytes). Every
read carries If-Match with the ETag the plan saw: a changed archive fails the build instead of mixing two releases.
"""
import argparse
import gzip
import hashlib
import http.client
import json
import os
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import tilepack  # noqa: E402

SOURCE = "https://data.source.coop/mapterhorn/mapterhorn/planet.pmtiles"
K = 3                       # packs of 8 x 8 tiles
MAX_ZOOM = 12
CHUNK_MB = 64               # bytes per range read (whole packs)
UA = "BeamNG.Earth tile builder (https://github.com/TecnaGamer/BeamNG.Earth)"
ZACC = [0]
for _z in range(32):
    ZACC.append(ZACC[-1] + 4 ** _z)


class Changed(RuntimeError):
    pass


def _get(url, start=None, end=None, etag=None, tries=6):
    """Bytes start..end-1 of url (whole without a range). 412 (the ETag changed) raises Changed."""
    h = {"User-Agent": UA}
    if start is not None:
        h["Range"] = f"bytes={start}-{end - 1}"
    if etag:
        h["If-Match"] = etag
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=120) as r:
                body = r.read()
            if start is not None and len(body) != end - start:
                raise http.client.IncompleteRead(body, end - start - len(body))
            return body
        except urllib.error.HTTPError as e:
            if e.code == 412:
                raise Changed(f"{url}: the archive changed (ETag no longer {etag})") from None
            if e.code < 500 and e.code != 429 or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, OSError):
            if attempt == tries - 1:
                raise
        time.sleep(min(60.0, 2.0 * 2 ** attempt))


def head(url):
    """(size, ETag) of the archive."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers["Content-Length"]), r.headers.get("ETag")


# --- PMTiles v3 -------------------------------------------------------------------------------------------------
def _varints(b):
    out, v, s = [], 0, 0
    for c in b:
        v |= (c & 0x7F) << s
        if c & 0x80:
            s += 7
        else:
            out.append(v)
            v, s = 0, 0
    return out


def _directory(b, comp):
    """[(tile id, run length, offset, length)] of a directory's bytes (run length 0: a leaf directory)."""
    if comp == 2:
        b = gzip.decompress(b)
    elif comp != 1:
        raise ValueError(f"directory compression {comp} not handled")
    v = _varints(b)
    n = v[0]
    ids, runs, lens, offs = v[1:1 + n], v[1 + n:1 + 2 * n], v[1 + 2 * n:1 + 3 * n], v[1 + 3 * n:1 + 4 * n]
    t, out = 0, []
    for i in range(n):
        t += ids[i]
        o = out[-1][2] + out[-1][3] if offs[i] == 0 and i > 0 else offs[i] - 1
        out.append((t, runs[i], o, lens[i]))
    return out


class Archive:
    def __init__(self, url, etag=None):
        self.url = url
        self.size, got = head(url)
        if etag and got != etag:
            raise Changed(f"{url}: ETag {got}, the plan saw {etag}")
        self.etag = got
        h = _get(url, 0, 16384, self.etag)
        if h[:7] != b"PMTiles" or h[7] != 3:
            raise ValueError("not a PMTiles v3 archive")
        (self.root_off, self.root_len, _, _, self.leaf_off, self.leaf_len, self.data_off, self.data_len, self.n_addr,
         self.n_entries, self.n_contents) = struct.unpack_from("<11Q", h, 8)
        self.clustered, self.icomp, self.tcomp, self.ttype, self.minz, self.maxz = h[96:102]
        if self.tcomp not in (0, 1) or self.ttype != 4:
            raise ValueError(f"tiles compressed ({self.tcomp}) or not WebP ({self.ttype})")
        if self.maxz > MAX_ZOOM:
            raise ValueError(f"zooms to {self.maxz}: this builder packs z0-{MAX_ZOOM}")
        self.root = _directory(h[self.root_off:self.root_off + self.root_len], self.icomp)
        self._leaves = None

    def entries(self, t0=0, t1=None):
        """[(tile id, run, offset, length)] of the tiles with ids t0..t1-1 (runs cut to it), in tile-id order."""
        if self._leaves is None:
            self._leaves = _get(self.url, self.leaf_off, self.leaf_off + self.leaf_len, self.etag)
        t1 = ZACC[MAX_ZOOM + 1] if t1 is None else t1
        out = []

        def walk(es):
            for i, (t, run, off, ln) in enumerate(es):
                nxt = es[i + 1][0] if i + 1 < len(es) else None
                if run == 0:
                    if (nxt is None or nxt > t0) and t < t1:
                        walk(_directory(self._leaves[off:off + ln], self.icomp))
                    continue
                a, b = max(t, t0), min(t + run, t1)
                if a < b:
                    out.append((a, b - a, off, ln))
        walk(self.root)
        return out


def tile_zd(tid):
    """(zoom, Hilbert index within it) of a tile id."""
    z = 0
    while tid >= ZACC[z + 1]:
        z += 1
    return z, tid - ZACC[z]


def hilbert_xy(z, d):
    """(x, y) of Hilbert index d at zoom z (the PMTiles curve)."""
    x = y = 0
    s = 1
    n = 1 << z
    while s < n:
        rx = 1 & (d >> 1)
        ry = 1 & (d ^ rx)
        if ry == 0:
            if rx == 1:
                x, y = s - 1 - x, s - 1 - y
            x, y = y, x
        x += s * rx
        y += s * ry
        d >>= 2
        s <<= 1
    return x, y


def pack_key(tid):
    """((z, px, py), k, index in the pack) of a tile id: packs of 2^k x 2^k (k = min(K, z))."""
    z, d = tile_zd(tid)
    k = min(K, z)
    p = d >> (2 * k)
    px, py = hilbert_xy(z - k, p)
    x, y = hilbert_xy(z, d)
    return (z, px, py), k, ((y & ((1 << k) - 1)) << k) + (x & ((1 << k) - 1))


def pack_first(tid):
    """The first tile id of tid's pack."""
    z, d = tile_zd(tid)
    k = min(K, z)
    return ZACC[z] + ((d >> (2 * k)) << (2 * k))


# --- commands ---------------------------------------------------------------------------------------------------
def packs_of(entries):
    """{pack key: (k, [(index, offset, length)])} of entries, in tile-id order."""
    out = {}
    for t, run, off, ln in entries:
        for j in range(run):
            key, k, i = pack_key(t + j)
            out.setdefault(key, (k, []))[1].append((i, off, ln))
    return out


def cmd_plan(a):
    arc = Archive(a.source)
    es = arc.entries()
    # bytes per pack, in tile-id order (a run's duplicates are stored once: counted once)
    packs = []
    for t, run, off, ln in es:
        first = pack_first(t)
        if packs and packs[-1][0] == first:
            packs[-1][1] += ln
        else:
            packs.append([first, ln])
        # (a run crossing into the next pack: its tiles there cost nothing more)
        last = pack_first(t + run - 1)
        if last != first:
            packs.append([last, 0])
    total = sum(b for _, b in packs)
    per = total / a.shards
    shards, cur, acc = [], packs[0][0], 0
    for i, (first, b) in enumerate(packs):
        if acc >= per and len(shards) < a.shards - 1:
            shards.append([cur, first])
            cur, acc = first, 0
        acc += b
    shards.append([cur, ZACC[MAX_ZOOM + 1]])
    json.dump({"source": a.source, "etag": arc.etag, "size": arc.size, "packs": len(packs), "bytes": total,
               "shards": shards}, sys.stdout)
    print()


def cmd_stats(a):
    arc = Archive(a.source)
    per = {}
    sizes = []
    for key, (k, tiles) in packs_of(arc.entries()).items():
        seen, b = set(), 0
        for _, off, ln in tiles:
            if off not in seen:
                seen.add(off)
                b += ln
        b += tilepack.HEAD.size + 8 * (1 << k) ** 2
        p = per.setdefault(key[0], [0, 0, 0])
        p[0] += 1
        p[1] += len(tiles)
        p[2] += b
        sizes.append(b)
    for z in sorted(per):
        n, t, b = per[z]
        print(f"z{z}: {n} packs, {t} tiles, {b / 1e9:.2f} GB")
    sizes.sort()
    print(f"total: {len(sizes)} packs, {sum(sizes) / 1e9:.2f} GB, median {sizes[len(sizes) // 2] / 1e6:.2f} MB, "
          f"largest {sizes[-1] / 1e6:.2f} MB")


def span(tiles, gap=65536):
    """(start, end) of the largest run of bytes among a pack's tiles [(index, offset, length)] (gaps up to gap)."""
    runs, cur = [], None
    for off, ln in sorted({(off, ln) for _, off, ln in tiles}):
        if cur is not None and off <= cur[1] + gap:
            cur[1] = max(cur[1], off + ln)
            cur[2] += ln
        else:
            cur = [off, off + ln, ln]
            runs.append(cur)
    best = max(runs, key=lambda r: r[2])
    return best[0], best[1]


def _write(out, key, k, tiles, data_of):
    """One pack written (identical tiles stored once: the table points both at the same bytes). Bytes written."""
    z, px, py = key
    n = (1 << k) ** 2
    table = [(0, 0)] * n
    body, at, pos = [], {}, 0
    for i, off, ln in tiles:
        if off not in at:
            b = data_of(off, ln)
            if len(b) != ln:
                raise ValueError(f"tile at {off}: {len(b)} bytes, {ln} expected")
            at[off] = pos
            body.append(b)
            pos += ln
        table[i] = (at[off], ln)
    blob = (tilepack.HEAD.pack(tilepack.MAGIC, tilepack.VERSION, k, z, 0, px, py)
            + b"".join(struct.pack("<II", o, ln) for o, ln in table) + b"".join(body))
    path = os.path.join(out, tilepack.path(z, px, py))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(blob)
    os.replace(path + ".tmp", path)
    return len(blob)


def cmd_build(a):
    arc = Archive(a.source, a.etag)
    t0, t1 = a.range
    if pack_first(t0) != t0 or (t1 < ZACC[MAX_ZOOM + 1] and pack_first(t1) != t1):
        raise SystemExit("the range must start and end on pack boundaries (plan)")
    packs = packs_of(arc.entries(t0, t1))
    keys = list(packs)
    base = arc.data_off
    # batches of whole packs whose bytes lie together, up to CHUNK_MB each (a pack's span: its largest run of bytes;
    # tiles stored elsewhere, the archive's deduplicated copies, are read on their own)
    batches, cur, lo, hi = [], [], None, None
    for key in keys:
        plo, phi = span(packs[key][1])
        if cur and (phi - lo > a.chunk_mb * 1e6 or plo < lo or plo > hi + 1_000_000):
            batches.append((cur, lo, hi))
            cur, lo, hi = [], None, None
        cur.append(key)
        lo = plo if lo is None else min(lo, plo)
        hi = phi if hi is None else max(hi, phi)
    if cur:
        batches.append((cur, lo, hi))
    deadline = a.deadline or None
    stats = {"packs": 0, "bytes": 0, "read": 0, "extra": 0, "batches": 0}
    lock = threading.Lock()
    memo = {}                                   # offset -> bytes of tiles read on their own (shared copies)

    def run(batch):
        bkeys, lo, hi = batch
        if deadline and time.time() > deadline:
            return False
        chunk = _get(arc.url, base + lo, base + hi, arc.etag)
        extra = [0]

        def data_of(off, ln):
            if lo <= off and off + ln <= hi:
                return chunk[off - lo:off - lo + ln]
            with lock:
                b = memo.get(off)
            if b is None:                       # (a tile stored once, elsewhere: deduplicated by the archive)
                extra[0] += 1
                b = _get(arc.url, base + off, base + off + ln, arc.etag)
                with lock:
                    if len(memo) < 20000:
                        memo[off] = b
            return b
        b = 0
        for key in bkeys:
            k, tiles = packs[key]
            b += _write(a.out, key, k, tiles, data_of)
        with lock:
            stats["packs"] += len(bkeys)
            stats["bytes"] += b
            stats["read"] += hi - lo
            stats["extra"] += extra[0]
            stats["batches"] += 1
            if stats["batches"] % 20 == 0:
                print(f"  {stats['batches']}/{len(batches)} reads, {stats['packs']} packs, "
                      f"{stats['bytes'] / 1e9:.2f} GB", flush=True)
        return True
    t = time.time()
    with ThreadPoolExecutor(a.workers) as ex:
        done = list(ex.map(run, batches))
    stats["seconds"] = round(time.time() - t, 1)
    stats["complete"] = all(done)
    print(json.dumps(stats), flush=True)
    if not all(done):
        sys.exit(75)                            # (out of time: the rest next run)


def cmd_index(a):
    arc = Archive(a.source, a.etag)
    keys = set()
    for t, run, off, ln in arc.entries():
        first = pack_first(t)
        keys.add(pack_key(first)[0])
        last = pack_first(t + run - 1)
        p = first
        while p != last:                        # (a run over several packs: every pack it reaches)
            z, d = tile_zd(p)
            k = min(K, z)
            p += 4 ** k
            if p >= ZACC[MAX_ZOOM + 1]:
                break
            keys.add(pack_key(p)[0])
    data = tilepack.index_build(K, MAX_ZOOM, keys)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "wb") as f:
        f.write(data)
    print(json.dumps({"packs": len(keys), "bytes": len(data), "etag": arc.etag}))


def cmd_md5(a):
    size, etag = head(a.source)
    if a.etag and etag != a.etag:
        raise Changed(f"ETag {etag}, the plan saw {a.etag}")
    step = a.chunk_mb * 1_000_000
    m = hashlib.md5()
    t = time.time()
    with ThreadPoolExecutor(4) as ex:           # (four reads ahead, hashed in order)
        offs = list(range(0, size, step))
        futs = {}
        for i, off in enumerate(offs):
            for j in range(i, min(i + 4, len(offs))):
                if j not in futs:
                    futs[j] = ex.submit(_get, a.source, offs[j], min(size, offs[j] + step), etag)
            m.update(futs.pop(i).result())
            if i % 50 == 0:
                done = min(size, off + step)
                print(f"  {done / 1e9:.1f} / {size / 1e9:.1f} GB, {done / 1e6 / max(1e-3, time.time() - t):.0f} MB/s",
                      flush=True)
    got = m.hexdigest()
    print(json.dumps({"md5": got, "expected": a.expect, "size": size, "etag": etag}))
    if a.expect and got != a.expect:
        raise SystemExit(f"md5 {got}, expected {a.expect}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=SOURCE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--shards", type=int, default=128)
    sub.add_parser("stats")
    p = sub.add_parser("build")
    p.add_argument("out")
    p.add_argument("--range", type=int, nargs=2, required=True)
    p.add_argument("--etag")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--chunk-mb", type=float, default=CHUNK_MB)
    p.add_argument("--deadline", type=float, default=0.0, help="unix s: no read started after it (exit 75)")
    p = sub.add_parser("index")
    p.add_argument("out")
    p.add_argument("--etag")
    p = sub.add_parser("md5")
    p.add_argument("--expect")
    p.add_argument("--etag")
    p.add_argument("--chunk-mb", type=int, default=256)
    a = ap.parse_args()
    {"plan": cmd_plan, "stats": cmd_stats, "build": cmd_build, "index": cmd_index, "md5": cmd_md5}[a.cmd](a)


if __name__ == "__main__":
    main()
