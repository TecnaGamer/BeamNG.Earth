"""Background regional OSM extracts for signs, independent of 1.6's Layercake road tags."""
from contextlib import contextmanager
from types import SimpleNamespace
import json
import logging
import math
import os
import re
import threading
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import numpy as np

log = logging.getLogger('earthd.osmsigns')
INDEX_URL = 'https://download.geofabrik.de/index-v1.json'
CHUNK = 8 * 1024 * 1024
MAX_DOWNLOAD = 2 * 1024 ** 3

class PreparationCancelled(InterruptedError):
    """A newer teleport replaced this preparation; retain reusable partial downloads."""

def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise PreparationCancelled('OSM preparation superseded; partial download retained')

@contextmanager
def _process_lock(path, cancel=None):
    """Workers share one resumable download and importer; OS locks release on process exit."""
    with open(path, 'a+b') as lock:
        if os.path.getsize(path) == 0:
            lock.write(b'0')
            lock.flush()
        deadline = time.monotonic()+600.
        while True:
            _check_cancel(cancel)
            try:
                lock.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('another worker is still preparing OSM sign data')
                time.sleep(.2)
        try:
            _check_cancel(cancel)
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

def _inside(ring, lon, lat):
    p = np.asarray(ring, float)
    a, b = p[:-1], p[1:]
    crossing = ((a[:, 1] > lat) != (b[:, 1] > lat)) & (
        lon < (b[:, 0] - a[:, 0]) * (lat - a[:, 1]) / np.where(b[:, 1] != a[:, 1], b[:, 1]-a[:, 1], 1.) + a[:, 0])
    return bool(np.count_nonzero(crossing) % 2)

def region_at(index, lat, lon):
    choices = []
    for feature in index.get("features", ()):
        geom, prop = feature["geometry"], feature["properties"]
        polygons = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        if not any(_inside(p[0], lon, lat) and not any(_inside(h, lon, lat) for h in p[1:]) for p in polygons):
            continue
        area = 0.
        for p in polygons:
            a, b = np.asarray(p[0])[:-1], np.asarray(p[0])[1:]
            area += abs((a[:, 0]*b[:, 1]-a[:, 1]*b[:, 0]).sum())
        url = prop.get("urls", {}).get("pbf", "")
        target = urlsplit(url)
        if (target.scheme == 'https' and target.hostname == "download.geofabrik.de"
                and target.port in (None, 443) and url.endswith(".osm.pbf")):
            choices.append((area, prop["id"], url))
    return min(choices)[1:] if choices else None


class SameHostRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != 'https' or target.hostname != 'download.geofabrik.de' or target.port not in (None, 443):
            raise OSError('Unexpected OSM extract redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class RegionSource:
    def __init__(self, raw_dir, fetcher):
        # Reuse earlier downloads; never read/write the old lane database.
        self.root = os.path.join(raw_dir, 'osm-lanes')
        self.fetcher = fetcher
        self.lock = threading.Lock()
        self.index = None
        self.background = not getattr(fetcher, 'offline', False)
        self._stop = threading.Event()
        self.failed = {}
        self.download_progress = {}

    def close(self):
        self._stop.set()

    def _get(self, url, headers=None):
        # Use 1.6's admitted session API; its Fetcher.get does not follow redirects.
        with self.fetcher.session(url, kind='background') as session:
            request = Request(url, headers={**session.headers, **(headers or {})})
            timeout = self.fetcher.policy(urlsplit(url).hostname).timeout
            with build_opener(SameHostRedirect()).open(request, timeout=timeout) as response:
                body = response.read(CHUNK + 1)
                if len(body) > CHUNK:
                    raise OSError('OSM response exceeds the requested chunk')
                session.status, session.nbytes = response.status, len(body)
                return SimpleNamespace(status=response.status, body=body,
                    headers={k.lower():v for k,v in response.headers.items()}, url=response.url)

    def prepare(self, lat, lon):
        os.makedirs(self.root, exist_ok=True)
        with self.lock, _process_lock(os.path.join(self.root, 'prepare.lock'), self._stop):
            index_path = os.path.join(self.root, 'regions.json')
            if self.index is None:
                try:
                    with open(index_path, encoding='utf-8') as f:
                        self.index = json.load(f)
                except (OSError, ValueError):
                    self.index = json.loads(self._get(INDEX_URL).body)
                    temp = f'{index_path}.{os.getpid()}.{threading.get_ident()}.tmp'
                    with open(temp, 'w', encoding='utf-8') as f:
                        json.dump(self.index, f)
                    os.replace(temp, index_path)
            region = region_at(self.index, lat, lon)
            if region is None:
                return None
            name, url = region
            if not re.fullmatch(r'[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*', name):
                raise ValueError('Invalid OSM extract name')
            name = name.replace('/', '_')
            path = os.path.join(self.root, name + '.osm.pbf')
            if os.path.exists(path):
                return path
            if time.monotonic() - self.failed.get(name, -math.inf) < 300.:
                return None
            try:
                self._download(url, path, self._stop)
                return path
            except Exception:
                self.failed[name] = time.monotonic()
                raise

    def _download(self, url, path, cancel=None):
        _check_cancel(cancel)
        part, meta = path + ".part", path + ".part.json"
        first = self._get(url, headers={"Range": "bytes=0-0"})
        total = int(first.headers.get("content-range", "0/0").split("/")[-1])
        stamp = first.headers.get("etag") or first.headers.get("last-modified")
        if first.status != 206 or not 0 < total <= MAX_DOWNLOAD or not stamp:
            raise IOError("OSM extract needs byte ranges and a stable snapshot, at most 2 GiB")
        # The -latest URL redirects to a dated snapshot. Reuse that validated URL for
        # all chunks, rather than spending another request and rate token per range.
        download_url = first.url or url
        record = dict(url=url, total=total, stamp=stamp)
        try:
            with open(meta, encoding="utf-8") as f:
                same = json.load(f) == record
        except (OSError, ValueError):
            same = False
        if not same:
            with open(part, "wb"):
                pass
            with open(meta, "w", encoding="utf-8") as f:
                json.dump(record, f)
        start = os.path.getsize(part) if os.path.exists(part) else 0
        self.download_progress = dict(path=path, downloaded=start, total=total)
        log.info("OSM signs: caching %s (%.0f MB once; resuming at %.0f MB)", url, total/1e6, start/1e6)
        with open(part, "ab") as f:
            last_report = start
            while start < total:
                _check_cancel(cancel)
                if self._stop.is_set():
                    raise InterruptedError('OSM sign cache preparation stopped; partial download retained')
                end = min(start + CHUNK, total)-1
                res = self._get(download_url, headers={"Range": f"bytes={start}-{end}", "If-Range": stamp})
                if res.status != 206 or res.headers.get("content-range") != f"bytes {start}-{end}/{total}" or len(res.body) != end-start+1:
                    raise IOError("OSM extract changed during download; resume will start a consistent snapshot")
                f.write(res.body)
                start = end+1
                self.download_progress = dict(path=path, downloaded=start, total=total)
                if start-last_report >= 32*1024*1024 or start == total:
                    log.info('OSM signs: downloaded %.0f / %.0f MB (%.0f%%)', start/1e6, total/1e6, 100*start/total)
                    last_report = start
        os.replace(part, path)
        os.unlink(meta)
