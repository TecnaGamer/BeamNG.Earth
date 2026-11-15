#!/usr/bin/env python3
"""The binding of Mapterhorn's regional archive copies to its releases (server/mtarchive.py; HOSTING.md "Mapterhorn
elevation"): for each archive Mapterhorn's file list (download_urls.json) names, each mirror's copy (Source
Cooperative's, Mapterhorn's own) is read whole by range reads, every one If-Match the ETag its first answer gave, through
MD5; a copy whose MD5 is the list's gets its ETag recorded. The client reads an archive only from a copy recorded here,
If-Match that ETag: a copy's size, head and ETag alone do not prove which release it holds (Codex review 2026-11-15).

    build_mt_bindings.py --out archives.json [--list download_urls.json] [--names 6-18-24.pmtiles ...]
                         [--mirrors data.source.coop ...] [--max-gb 100] [--merge old.json] [--jobs 4] [--ahead 4]

--mirrors: the hosts to hash (mtarchive.HOSTS, all by default). Each copy is read AHEAD ranges of CHUNK at a time on
connections kept open (one per reading thread: ~4 x one range's speed). Exit status 3 when archives were to be hashed
and none got a mirror bound (a run that bound nothing is a failure).

--merge keeps an older binding's entries whose MD5 and size are still the list's (a new release re-hashes only the
archives that changed). Published as <CDN>/dem/mapterhorn/archives.json ({"format": 1, "archives": {name: {"md5",
"size", "mirrors": {host: ETag}}}}). The whole of every archive is read once per mirror per release (8.9 TiB for all
of release 0.0.13): start with the regions players visit (--names), or --max-gb.
"""
import argparse
import hashlib
import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import mtarchive  # noqa: E402

LIST_URL = "https://download.mapterhorn.com/download_urls.json"
UA = "BeamNG.Earth binding builder (https://github.com/TecnaGamer/BeamNG.Earth)"
CHUNK = 64 * 1024 * 1024


class Changed(RuntimeError):
    pass


_conns = threading.local()


def _conn(host, fresh=False):
    """This thread's open HTTPS connection to host (kept between reads)."""
    d = _conns.__dict__.setdefault("c", {})
    c = d.get(host)
    if fresh and c is not None:
        c.close()
        c = None
    if c is None:
        c = d[host] = http.client.HTTPSConnection(host, timeout=120)
    return c


def _get(url, start, end, etag=None, tries=6):
    """(bytes start..end-1, ETag, total size), on this thread's kept connection."""
    u = urlsplit(url)
    h = {"User-Agent": UA, "Range": f"bytes={start}-{end - 1}", "Connection": "keep-alive"}
    if etag:
        h["If-Match"] = etag
    for attempt in range(tries):
        try:
            c = _conn(u.hostname, fresh=attempt > 0)
            c.request("GET", u.path, headers=h)
            r = c.getresponse()
            body = r.read()
            if r.status == 412:
                raise Changed(f"{url}: changed while read (ETag no longer {etag})")
            if r.status in (429,) or r.status >= 500:
                raise IOError(f"{url}: HTTP {r.status}")
            if r.status != 206 or len(body) != end - start:
                raise ValueError(f"{url}: HTTP {r.status}, {len(body)} bytes for {start}-{end - 1}")
            total = int((r.getheader("Content-Range") or "/0").rsplit("/", 1)[1])
            return body, (r.getheader("ETag") or "").strip(), total
        except Changed:
            raise
        except ValueError:
            raise
        except (OSError, http.client.HTTPException):
            if attempt == tries - 1:
                raise
        time.sleep(min(60, 2 ** attempt))
    raise IOError(f"{url}: no answer")


def hash_copy(url, size, chunk=CHUNK, ahead=4):
    """(MD5 hex, ETag) of the copy at url, read whole: the first range alone (its ETag), then `ahead` ranges at a time
    (hashed in order), every one If-Match that ETag."""
    md5 = hashlib.md5()
    first = min(size, chunk)
    body, etag, total = _get(url, 0, first)
    if total != size:
        raise Changed(f"{url}: {total} bytes, the list says {size}")
    if not etag:
        raise Changed(f"{url}: no ETag (cannot be pinned)")
    md5.update(body)
    offs = list(range(first, size, chunk))
    t = time.time()
    with ThreadPoolExecutor(max(1, ahead)) as ex:
        futs = {}
        for i, off in enumerate(offs):
            for j in range(i, min(i + max(1, ahead), len(offs))):
                if j not in futs:
                    futs[j] = ex.submit(_get, url, offs[j], min(size, offs[j] + chunk), etag)
            body, _tag, total = futs.pop(i).result()
            if total != size:
                raise Changed(f"{url}: {total} bytes, the list says {size}")
            md5.update(body)
            if i % 100 == 99:
                done = min(size, off + chunk)
                print(f"  {url}: {done / 1e9:.1f} / {size / 1e9:.1f} GB, "
                      f"{done / 1e6 / max(1e-3, time.time() - t):.0f} MB/s", file=sys.stderr, flush=True)
    return md5.hexdigest(), etag


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--list", default=LIST_URL, help="download_urls.json: a URL or a file")
    ap.add_argument("--names", nargs="*")
    ap.add_argument("--max-gb", type=float, default=None, help="skip archives larger than this")
    ap.add_argument("--merge", default=None)
    ap.add_argument("--jobs", type=int, default=4, help="archives hashed at once")
    ap.add_argument("--mirrors", nargs="*", default=None, help=f"hosts to hash (of {' '.join(mtarchive.HOSTS)})")
    ap.add_argument("--ahead", type=int, default=4, help="ranges in flight per copy")
    ap.add_argument("--chunk-mb", type=int, default=CHUNK // (1024 * 1024))
    a = ap.parse_args()
    hosts = list(mtarchive.HOSTS) if not a.mirrors else a.mirrors
    unknown = [h for h in hosts if h not in mtarchive.HOSTS]
    if unknown or not hosts:
        raise SystemExit(f"--mirrors {unknown or hosts}: not among {list(mtarchive.HOSTS)}")
    mirrors = [(m, h) for m, h in zip(mtarchive.MIRRORS, mtarchive.HOSTS) if h in hosts]
    if a.list.startswith(("http://", "https://")):
        with urllib.request.urlopen(urllib.request.Request(a.list, headers={"User-Agent": UA}), timeout=60) as r:
            doc = json.loads(r.read())
    else:
        with open(a.list, "rb") as f:
            doc = json.loads(f.read())
    arcs = {v["name"]: v for v in mtarchive.archives_of(doc).values()}
    out = {}
    if a.merge and os.path.exists(a.merge):
        with open(a.merge, "rb") as f:
            for name, e in mtarchive.bindings_of(f.read()).items():
                it = arcs.get(name)
                if it and it["md5"] == e["md5"] and it["size"] == e["size"]:
                    out[name] = e
    todo = [it for name, it in sorted(arcs.items()) if (not a.names or name in a.names) and name not in out
            and (a.max_gb is None or it["size"] <= a.max_gb * 1e9)]

    def one(it):
        bound = {}
        for tmpl, host in mirrors:
            url = tmpl.format(name=it["name"])
            try:
                md5, etag = hash_copy(url, it["size"], a.chunk_mb * 1024 * 1024, a.ahead)
            except (Changed, IOError, ValueError, http.client.HTTPException) as e:
                print(f"{it['name']} {host}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            if md5 == it["md5"]:
                bound[host] = etag
            else:
                print(f"{it['name']} {host}: MD5 {md5}, the list's {it['md5']}: not bound", file=sys.stderr)
        return it, bound
    new = 0
    with ThreadPoolExecutor(max(1, a.jobs)) as pool:
        for it, bound in pool.map(one, todo):
            if bound:
                new += 1
                out[it["name"]] = {"md5": it["md5"], "size": it["size"], "mirrors": bound}
            print(json.dumps({"name": it["name"], "size": it["size"], "bound": sorted(bound)}))
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"format": 1, "list_version": doc.get("version"), "archives": out}, f, indent=1, sort_keys=True)
    os.replace(tmp, a.out)
    print(json.dumps({"archives": len(out), "bound_now": new, "asked": len(todo), "out": a.out}))
    if todo and not new:
        raise SystemExit(3)             # (asked to hash, nothing bound: a failed run)


if __name__ == "__main__":
    main()
