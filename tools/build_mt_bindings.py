#!/usr/bin/env python3
"""The binding of Mapterhorn's regional archive copies to its releases (server/mtarchive.py; HOSTING.md "Mapterhorn
elevation"): for each archive Mapterhorn's file list (download_urls.json) names, each mirror's copy (Source
Cooperative's, Mapterhorn's own) is read whole by range reads, every one If-Match the ETag its first answer gave, through
MD5; a copy whose MD5 is the list's gets its ETag recorded. The client reads an archive only from a copy recorded here,
If-Match that ETag: a copy's size, head and ETag alone do not prove which release it holds (Codex review 2026-11-15).

    build_mt_bindings.py --out archives.json [--list download_urls.json] [--names 6-18-24.pmtiles ...]
                         [--max-gb 100] [--merge old.json] [--jobs 4]

--merge keeps an older binding's entries whose MD5 and size are still the list's (a new release re-hashes only the
archives that changed). Published as <CDN>/dem/mapterhorn/archives.json ({"format": 1, "archives": {name: {"md5",
"size", "mirrors": {host: ETag}}}}). The whole of every archive is read once per mirror per release (8.9 TiB for all
of release 0.0.13): start with the regions players visit (--names), or --max-gb.
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import mtarchive  # noqa: E402

LIST_URL = "https://download.mapterhorn.com/download_urls.json"
UA = "BeamNG.Earth binding builder (https://github.com/TecnaGamer/BeamNG.Earth)"
CHUNK = 64 * 1024 * 1024


class Changed(RuntimeError):
    pass


def _get(url, start, end, etag=None, tries=6):
    """(bytes start..end-1, ETag, total size)."""
    h = {"User-Agent": UA, "Range": f"bytes={start}-{end - 1}"}
    if etag:
        h["If-Match"] = etag
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=120) as r:
                body = r.read()
                if r.status != 206 or len(body) != end - start:
                    raise IOError(f"{url}: HTTP {r.status}, {len(body)} bytes for {start}-{end - 1}")
                total = int((r.headers.get("Content-Range") or "/0").rsplit("/", 1)[1])
                return body, (r.headers.get("ETag") or "").strip(), total
        except urllib.error.HTTPError as e:
            if e.code == 412:
                raise Changed(f"{url}: changed while read (ETag no longer {etag})") from None
            if e.code < 500 and e.code != 429:
                raise
        except (OSError, ValueError):
            if attempt == tries - 1:
                raise
        time.sleep(min(60, 2 ** attempt))
    raise IOError(f"{url}: no answer")


def hash_copy(url, size, chunk=CHUNK):
    """(MD5 hex, ETag) of the copy at url, read whole, every read If-Match its first ETag."""
    md5 = hashlib.md5()
    etag = None
    for a in range(0, size, chunk):
        b = min(size, a + chunk)
        body, tag, total = _get(url, a, b, etag)
        if total != size:
            raise Changed(f"{url}: {total} bytes, the list says {size}")
        if etag is None:
            if not tag:
                raise Changed(f"{url}: no ETag (cannot be pinned)")
            etag = tag
        md5.update(body)
    return md5.hexdigest(), etag


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--list", default=LIST_URL, help="download_urls.json: a URL or a file")
    ap.add_argument("--names", nargs="*")
    ap.add_argument("--max-gb", type=float, default=None, help="skip archives larger than this")
    ap.add_argument("--merge", default=None)
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
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
        mirrors = {}
        for tmpl, host in zip(mtarchive.MIRRORS, mtarchive.HOSTS):
            url = tmpl.format(name=it["name"])
            try:
                md5, etag = hash_copy(url, it["size"])
            except (Changed, IOError, urllib.error.URLError) as e:
                print(f"{it['name']} {host}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            if md5 == it["md5"]:
                mirrors[host] = etag
            else:
                print(f"{it['name']} {host}: MD5 {md5}, the list's {it['md5']}: not bound", file=sys.stderr)
        return it, mirrors
    with ThreadPoolExecutor(max(1, a.jobs)) as pool:
        for it, mirrors in pool.map(one, todo):
            if mirrors:
                out[it["name"]] = {"md5": it["md5"], "size": it["size"], "mirrors": mirrors}
            print(json.dumps({"name": it["name"], "size": it["size"], "bound": sorted(mirrors)}))
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"format": 1, "list_version": doc.get("version"), "archives": out}, f, indent=1, sort_keys=True)
    os.replace(tmp, a.out)
    print(json.dumps({"archives": len(out), "out": a.out}))


if __name__ == "__main__":
    main()
