"""OSM lane/surface tags from cached regional PBF extracts, never the editing API or public Overpass.

Geofabrik's polygon index selects the smallest extract containing a cell. Download once in
bounded, resumable range requests, then keep a compact SQLite index of tagged road ways and
their geometry. Whole ways, not proximity to another carriageway, identify Overture roads.
The snapshot stays fixed until explicitly removed/refreshed; driving does not poll OSM.
"""
import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import logging
import math
import os
import queue
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit

import numpy as np

import mapgeom

log = logging.getLogger("earthd.osmlanes")
INDEX_URL = "https://download.geofabrik.de/index-v1.json"
VERSION = 2
CHUNK = 8 * 1024 * 1024
MAX_DOWNLOAD = 2 * 1024 ** 3
LANE_KEYS = ("lanes", "lanes:forward", "lanes:backward", "lanes:both_ways")


class PreparationCancelled(InterruptedError):
    """A newer teleport replaced this preparation; retain reusable partial downloads."""


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise PreparationCancelled('OSM preparation superseded; partial download retained')


@contextmanager
def _thread_lock(lock, cancel=None):
    while True:
        _check_cancel(cancel)
        if lock.acquire(timeout=.2):
            break
    try:
        _check_cancel(cancel)
        yield
    finally:
        lock.release()


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
                    raise TimeoutError('another worker is still preparing OSM lane data')
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


def count(value):
    """Reject ambiguous lists, conditional values, zeroes and implausible lane counts."""
    s = str(value or "").strip()
    return int(s) if re.fullmatch(r"[1-9]|1[0-6]", s) else None


def lane_counts(tags):
    """(OSM-forward, backward, centre turn lanes), or None for incomplete/contradictory tags.

    `lanes` counts both directions on a two-way road, excludes shoulders/cycle/parking lanes.
    Directional counts win; a single remaining direction can be derived from a tagged total.
    Conditional, reversible and centre-turn lanes are never invented as through lanes.
    """
    n, f, b, c = (count(tags.get(k)) for k in LANE_KEYS)
    c = c or 0
    ow = str(tags.get("oneway", "")).lower()
    if ow in ("reversible", "alternating") or count(tags.get("lanes:reversible")):
        return None
    reverse = ow in ("-1", "reverse")
    one = reverse or ow in ("yes", "1", "true") or (ow not in ("no", "0", "false") and
           (tags.get("highway") == "motorway" or tags.get("junction") == "roundabout"))
    if one:
        k = (b if reverse else f) or n
        if k is None or c or ((f if reverse else b) or 0) or (n is not None and n != k):
            return None
        return (0, k, 0) if reverse else (k, 0, 0)
    if f is None and b is None:
        if n is None or n - c < 2 or (n - c) % 2:
            return None
        f = b = (n - c) // 2
    elif f is None or b is None:
        if n is None:
            return None
        if f is None:
            f = n - c - b
        else:
            b = n - c - f
    if not 1 <= f <= 16 or not 1 <= b <= 16 or (n is not None and f + b + c != n):
        return None
    return f, b, c


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
        if urlsplit(url).hostname == "download.geofabrik.de" and url.endswith(".osm.pbf"):
            choices.append((area, prop["id"], url))
    return min(choices)[1:] if choices else None


def import_pbf(path, target):
    """Two streaming passes: tagged ways first, then only their referenced node locations.

    Does not allocate a planet-sized node-location cache. Also accepts .osm XML for local imports.
    The complete index replaces its predecessor atomically, so another process sees no half import.
    """
    import osmium
    ways, needed = {}, set()
    fp = osmium.FileProcessor(str(path), osmium.osm.WAY).with_filter(osmium.filter.KeyFilter(*LANE_KEYS, 'surface'))
    for w in fp:
        tags = dict(w.tags)
        counts = lane_counts(tags) if "highway" in tags else None
        surface = str(tags.get('surface', '')).strip().lower()
        if 'highway' not in tags or (counts is None and not surface) or len(w.nodes) < 2:
            continue
        refs = tuple(n.ref for n in w.nodes)
        ways[w.id] = (w.version, tags["highway"], counts or (None, None, None), refs, surface)
        needed.update(refs)
    nodes = {}
    for n in osmium.FileProcessor(str(path), osmium.osm.NODE).with_filter(osmium.filter.IdFilter(needed)):
        if n.location.valid():
            nodes[n.id] = (n.location.lon, n.location.lat)
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    tmp = f"{target}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with closing(sqlite3.connect(tmp)) as con:
            con.execute("CREATE TABLE meta(version INTEGER, imported REAL, source TEXT)")
            con.execute("INSERT INTO meta VALUES(?,?,?)", (VERSION, time.time(), os.path.basename(path)))
            con.execute("CREATE TABLE ways(id INTEGER PRIMARY KEY, version INTEGER, highway TEXT, f INTEGER, b INTEGER, c INTEGER, first INTEGER, last INTEGER, geometry BLOB, surface TEXT)")
            rows = []
            for wid, (ver, highway, counts, refs, surface) in ways.items():
                if all(n in nodes for n in refs):
                    xy = np.asarray([nodes[n] for n in refs], dtype="<f8")
                    rows.append((wid, ver, highway, *counts, refs[0], refs[-1], xy.tobytes(), surface))
            con.executemany("INSERT INTO ways VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
            con.execute("CREATE INDEX first_node ON ways(first)")
            con.execute("CREATE INDEX last_node ON ways(last)")
            con.commit()
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    log.info("OSM lanes/surfaces: indexed %d tagged road ways from %s", len(rows), path)
    return len(rows)


def _xy(ll, origin):
    return (np.asarray(ll, float) - origin) * [111320 * math.cos(math.radians(origin[1])), 110540]


def _orientation(road, osm):
    """Project two interior samples onto the actual OSM way, including split/reversed segments."""
    origin = osm[0]
    xy, p = _xy(osm, origin), _xy(road, origin)
    d = np.diff(xy, axis=0)
    lens = np.linalg.norm(d, axis=1)
    s = np.r_[0., np.cumsum(lens)]
    values, distances = [], []
    for q in (p[0], p[-1]):
        t = np.clip(((q-xy[:-1])*d).sum(1)/np.maximum(lens*lens, 1e-12), 0, 1)
        dist = np.linalg.norm(xy[:-1]+t[:, None]*d-q, axis=1)
        k = int(dist.argmin())
        values.append(s[k]+t[k]*lens[k])
        distances.append(dist[k])
    if max(distances) > 20 or abs(values[1]-values[0]) < .5:
        return 0
    return 1 if values[1] > values[0] else -1


def _ranged_tags(feats, i, sources, con):
    """Disjoint Overture source ranges describe successive OSM ways, not an ambiguity.

    Validate each range against its own way geometry and direction. Overlapping/incomplete
    provenance stays on defaults; route/property sources must never act as geometry sources.
    """
    road = next((r for p in feats.parts(i) for r in p if len(r)>=2),None)
    if road is None:return None
    ranges=[]
    for src in sources:
        bounds=src.get('between')
        if not isinstance(bounds,list) or len(bounds)!=2:return None
        try:a,b=map(float,bounds)
        except (TypeError,ValueError):return None
        if not 0.<=a<b<=1.:return None
        ranges.append((a,b,src))
    ranges.sort(key=lambda r:r[:2])
    if not ranges or ranges[0][0]>1e-5 or ranges[-1][1]<1.-1e-5:return None
    if any(abs(a[1]-b[0])>1e-5 for a,b in zip(ranges,ranges[1:])):return None
    xy=_xy(road,road[0])
    arc=np.r_[0.,np.cumsum(np.linalg.norm(np.diff(xy,axis=0),axis=1))]
    if arc[-1]<1.:return None
    sections=[];surfaces={}
    for a,b,src in ranges:
        wid=int(src['record_id'].split('@')[0][1:])
        row=con.execute('SELECT * FROM ways WHERE id=?',(wid,)).fetchone()
        if row is None:return None
        cuts=np.r_[a*arc[-1],arc[(arc>a*arc[-1])&(arc<b*arc[-1])],b*arc[-1]]
        piece=np.column_stack([np.interp(cuts,arc,road[:,d]) for d in (0,1)])
        osm=np.frombuffer(row[8],dtype='<f8').reshape(-1,2)
        direction=_orientation(piece,osm)
        if not direction:return None
        counts=None if row[3] is None else list(row[3:6] if direction>0 else (row[4],row[3],row[5]))
        sections.append(dict(start=a,end=b,counts=counts))
        if row[9]:surfaces[row[9]]=surfaces.get(row[9],0.)+b-a
    surface=max(surfaces,key=surfaces.get) if surfaces else ''
    return road,sections,surface


class LaneSource:
    def __init__(self, raw_dir, fetcher, snapshot=None, background=True):
        self.root = os.path.join(raw_dir, "osm-lanes")
        self.fetcher = fetcher
        self.lock = threading.Lock()       # only one region download/import per companion
        self.failed = {}
        self.index = None
        self.regions = {}
        # Freeze the ready indexes for this server run. Geometry/physics workers receive
        # this exact manifest, including workers spawned after a background import finishes.
        # A new road width must never appear under an already-built terrain block.
        if snapshot is None:
            snapshot = []
            try:
                for entry in os.scandir(self.root):
                    if entry.name.endswith(f'.v{VERSION}.sqlite') and entry.is_file():
                        stat = entry.stat()
                        snapshot.append((entry.name, stat.st_size, stat.st_mtime_ns))
            except FileNotFoundError:
                pass
        self.snapshot = sorted(tuple(row) for row in snapshot)
        self.snapshot_key = hashlib.sha256(json.dumps(self.snapshot).encode()).hexdigest()[:12] if self.snapshot else ''
        self.available = {row[0]: row for row in self.snapshot}
        self.background = background
        self._stop = threading.Event()
        self._jobs = queue.Queue(maxsize=8)
        self._job_lock = threading.Lock()
        self._requested = {}
        self._thread = None
        self.download_progress = {}   # immutable snapshots; readable while self.lock is held by a download

    def close(self):
        self._stop.set()             # never wait for a slow network request on shutdown

    def cached(self, lat, lon):
        """Ready-at-start index only. No HTTP, import, process lock or downloader lock."""
        if not self.available:
            return None
        index = self.index
        if index is None:
            try:
                with open(os.path.join(self.root, 'regions.json'), encoding='utf-8') as f:
                    index = json.load(f)
                self.index = index
            except (OSError, ValueError):
                return None
        region = region_at(index, lat, lon)
        if region is None:
            return None
        name = region[0].replace('/', '_')+f'.v{VERSION}.sqlite'
        if name not in self.available or not re.fullmatch(r'[a-zA-Z0-9_-]+\.v\d+\.sqlite', name):
            return None
        path = os.path.join(self.root, name)
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return path if (stat.st_size, stat.st_mtime_ns) == self.available[name][1:] else None

    def request_background(self, lat, lon):
        """Queue a missing region without occupying a terrain/map-data worker."""
        if not self.background or self._stop.is_set() or getattr(self.fetcher, 'offline_flag', False) is True:
            return
        key = (math.floor(lat), math.floor(lon))
        with self._job_lock:
            if time.monotonic()-self._requested.get(key, -math.inf) < 300.:
                return
            try:
                self._jobs.put_nowait((lat, lon))
            except queue.Full:
                return
            self._requested[key] = time.monotonic()
            if self._thread is None:
                self._thread = threading.Thread(target=self._background_loop, daemon=True, name='osm-lanes-cache')
                self._thread.start()

    def _background_loop(self):
        while not self._stop.is_set():
            try:
                lat, lon = self._jobs.get(timeout=.25)
            except queue.Empty:
                continue
            try:
                db = self.prepare(lat, lon)
                if db:
                    log.info('OSM lane cache ready: %s; available on the next companion start. Current terrain keeps its road layout.', db)
            except Exception as e:
                if not self._stop.is_set():
                    log.warning('OSM lane background preparation: %s; terrain continues with existing road defaults', e)
            finally:
                self._jobs.task_done()

    def _download(self, url, path, cancel=None):
        _check_cancel(cancel)
        part, meta = path + ".part", path + ".part.json"
        first = self.fetcher.get(url, headers={"Range": "bytes=0-0"}, redirect_hosts=("download.geofabrik.de",))
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
        log.info("OSM lanes: caching %s (%.0f MB once; resuming at %.0f MB)", url, total/1e6, start/1e6)
        with open(part, "ab") as f:
            last_report = start
            while start < total:
                _check_cancel(cancel)
                if self._stop.is_set():
                    raise InterruptedError('OSM lane cache preparation stopped; partial download retained')
                end = min(start + CHUNK, total)-1
                res = self.fetcher.get(download_url, headers={"Range": f"bytes={start}-{end}", "If-Range": stamp},
                                       redirect_hosts=("download.geofabrik.de",))
                if res.status != 206 or res.headers.get("content-range") != f"bytes {start}-{end}/{total}" or len(res.body) != end-start+1:
                    raise IOError("OSM extract changed during download; resume will start a consistent snapshot")
                f.write(res.body)
                start = end+1
                self.download_progress = dict(path=path, downloaded=start, total=total)
                if start-last_report >= 32*1024*1024 or start == total:
                    log.info('OSM lanes: downloaded %.0f / %.0f MB (%.0f%%)', start/1e6, total/1e6, 100*start/total)
                    last_report = start
        os.replace(part, path)
        os.unlink(meta)

    def prepare(self, lat, lon):
        os.makedirs(self.root, exist_ok=True)
        with self.lock, _process_lock(os.path.join(self.root, 'prepare.lock')):
            key = (round(lat, 3), round(lon, 3))
            cached = self.regions.get(key)
            if cached is not None:
                return cached
            index_path = os.path.join(self.root, "regions.json")
            index = self.index
            if index is None:
                try:
                    with open(index_path, encoding="utf-8") as f:
                        index = json.load(f)
                except (OSError, ValueError):
                    index = json.loads(self.fetcher.get(INDEX_URL).body)
                    temp_index = f'{index_path}.{os.getpid()}.{threading.get_ident()}.tmp'
                    with open(temp_index, "w", encoding="utf-8") as f:
                        json.dump(index, f)
                    os.replace(temp_index, index_path)
                self.index = index
            region = region_at(index, lat, lon)
            if region is None:
                return None
            name, url = region
            if not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", name):
                raise ValueError("invalid extract name")
            name = name.replace("/", "_")
            db = os.path.join(self.root, name + f".v{VERSION}.sqlite")
            if os.path.exists(db):
                self.regions[key] = db
                return db
            if time.monotonic() - self.failed.get(name, -math.inf) < 300:
                return None
            try:
                pbf = os.path.join(self.root, name + ".osm.pbf")
                if not os.path.exists(pbf):
                    self._download(url, pbf)
                import_pbf(pbf, db)
                self.regions[key] = db
                return db
            except Exception:
                self.failed[name] = time.monotonic()
                raise

    def enrich(self, feats, lat, lon, db=None):
        sources = feats.attrs.get("osm_sources")
        if sources is None or not len(feats):
            return True
        parsed = []
        for s in sources:
            try:
                parsed.append([r for r in json.loads(str(s) or "[]") or [] if
                               r.get("dataset") == "OpenStreetMap" and r.get("property") in (None, "", "/geometry") and
                               re.fullmatch(r"w\d+(?:@\d+)?", r.get("record_id") or "")])
            except (ValueError, TypeError):
                parsed.append([])
        if not any(parsed):
            return True
        db = db or self.cached(lat, lon)
        if db is None:
            self.request_background(lat, lon)
            return False
        rules = {k: [] for k in ("lanes_forward", "lanes_backward", "lanes_both_ways")}
        models = [""] * len(feats)
        surfaces = [''] * len(feats)
        with closing(sqlite3.connect(db)) as con:
            for i, srcs in enumerate(parsed):
                # A complete partition of successive source ways is safe when each piece
                # matches its own way. Previously these multi-way highways fell back to 2 lanes.
                if len(srcs) != 1 or srcs[0].get("between") not in (None, [0, 1], [0., 1.]):
                    ranged=_ranged_tags(feats,i,srcs,con)
                    if ranged is not None:
                        road,sections,surfaces[i]=ranged
                        if all(s['counts'] is not None for s in sections):
                            for s in sections:
                                for key,n in zip(rules,s['counts']):
                                    rules[key].append((i,n,[s['start'],s['end']]))
                            models[i]=json.dumps(dict(line=road.tolist(),sections=sections),separators=(',',':'))
                    continue
                wid = int(srcs[0]["record_id"].split("@")[0][1:])
                row = con.execute("SELECT * FROM ways WHERE id=?", (wid,)).fetchone()
                if row is None:
                    continue
                osm = np.frombuffer(row[8], dtype="<f8").reshape(-1, 2)
                road = next((r for p in feats.parts(i) for r in p if len(r) >= 2), None)
                direction = _orientation(road, osm) if road is not None else 0
                if not direction:
                    continue
                surfaces[i] = row[9]
                if row[3] is None:
                    continue   # surface-only way: retain lane defaults and no invented taper
                counts = row[3:6] if direction > 0 else (row[4], row[3], row[5])
                for key, n in zip(rules, counts):
                    rules[key].append((i, n, None))
                ends = []
                for end, node in ((0, row[6]), (-1, row[7])):
                    away = osm if end == 0 else osm[::-1]
                    xy = _xy(away, away[0])
                    s = np.r_[0., np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]
                    if s[-1] < 5:
                        continue
                    v = xy[min(3, len(xy)-1)]
                    v /= max(np.linalg.norm(v), 1e-9)
                    candidates = []
                    for other in con.execute("SELECT * FROM ways WHERE (first=? OR last=?) AND id!=?", (node, node, wid)):
                        if other[2] != row[2] or other[3] is None:
                            continue
                        ll = np.frombuffer(other[8], dtype="<f8").reshape(-1, 2)
                        same = other[6] != node if end == 0 else other[6] == node
                        nc = other[3:6] if same else (other[4], other[3], other[5])
                        # Never turn a split to the opposite carriageway into an added lane.
                        if bool(nc[0]) != bool(row[3]) or bool(nc[1]) != bool(row[4]):
                            continue
                        llaway = ll if other[6] == node else ll[::-1]
                        ov = _xy(llaway[min(3, len(llaway)-1)], llaway[0])
                        alignment = float(v @ ov / max(np.linalg.norm(ov), 1e-9))
                        if alignment < -.92:
                            olength = np.linalg.norm(np.diff(_xy(ll, ll[0]), axis=0), axis=1).sum()
                            candidates.append((alignment, other[0], nc, olength))
                    if not candidates:
                        continue
                    candidates.sort()
                    if len(candidates) > 1 and candidates[1][0]-candidates[0][0] < .02:
                        continue
                    nc = candidates[0][2]
                    if tuple(nc) == tuple(row[3:6]):
                        continue
                    reach = min(180. if row[2] in ("motorway", "trunk") else 90., s[-1]*.45)
                    other_reach = min(180. if row[2] in ("motorway", "trunk") else 90., candidates[0][3]*.45)
                    take = np.flatnonzero(s <= reach+15)
                    take = np.r_[take, min(take[-1]+1, len(s)-1)]
                    ends.append(dict(line=away[take].tolist(), reach=reach, weight=reach/max(reach+other_reach, 1e-9),
                                     other=list(nc if direction > 0 else (nc[1], nc[0], nc[2]))))
                models[i] = json.dumps(dict(ends=ends), separators=(",", ":"))
        for key, rows in rules.items():
            feats.rules[key] = mapgeom.rule_table(rows, "float")
        feats.attrs["osm_lane_model"] = mapgeom.text_column(models)
        feats.attrs['osm_surface'] = mapgeom.text_column(surfaces)
        return True


if __name__ == "__main__":
    import netfetch
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache", required=True)
    p.add_argument("--prepare", help="latitude,longitude: cache a regional lane index before starting the game")
    p.add_argument("--import-pbf", help="local .osm.pbf or .osm file")
    p.add_argument("--region", help="Geofabrik region ID for a local import, e.g. michigan")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO)
    source = LaneSource(os.path.join(a.cache, "mapdata", "raw"), netfetch.Fetcher())
    if a.import_pbf and a.region and re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", a.region):
        print(import_pbf(a.import_pbf, os.path.join(source.root, a.region.replace('/', '_')+f".v{VERSION}.sqlite")))
    elif a.prepare:
        print(source.prepare(*map(float, a.prepare.split(","))))
    else:
        p.error("use --prepare lat,lon or --import-pbf file --region id")
