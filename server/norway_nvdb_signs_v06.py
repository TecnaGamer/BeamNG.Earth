"""Nationwide NVDB Norwegian signs, v0.6 (nationwide built-in Norway sign scenery).

Original sign positions and registered road-reference points are supplied by
NVDB type 96 (Skiltplate). The road-ref-to-pole bearing is an approximation
ONLY for 'Parallelt fra veg'. All other facing modes are skipped, not guessed.
The old speed-limit-sign pipeline and AI traffic rules are NOT changed.

Network use: on demand within sign tile worker, bounded to official NVDB V4;
individual ~1 km cells are cached in .git, 14 day TTL. Prewarm via supplied
prefetch_norway_signs_v05.py for latency-free visits.
"""
from __future__ import annotations

from collections import OrderedDict
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler

import numpy as np

ENV = 'BNE_NVDB_SIGNS_V06'
FETCH_ENV = 'BNE_NVDB_SIGNS_FETCH'  # off => use cached data only
BASE = 'https://nvdbapiles.atlas.vegvesen.no/vegobjekter/api/v4/vegobjekter/96'
API_HOST = 'nvdbapiles.atlas.vegvesen.no'
CLIENT = 'BeamNG.Earth-Norway-Signs-v0.6'
LAT_STEP = 0.012  # about 1.3km
LON_STEP = 0.024  # about 1.2km at 63N
MAX_CELLS_TILE = 9
MAX_PAGES = 8
MAX_OBJECTS = 2000
PAGE_SIZE = 250
PAGE_MAX_BYTES = 7 * 1024 * 1024
CACHE_TTL_S = 14 * 86400
FAILURE_RETRY_S = 180          # avoid hammering NVDB after a timeout/offline connection
MIN_OFFSET_M = 2.5
MAX_OFFSET_M = 20.0
SUPPRESS_INFERRED_WITHIN_M = 12.0
NO_BBOX = (3.8, 57.5, 32.6, 72.0)
LOG = logging.getLogger('earthd.nvdb_signs_v06')
# Existing Vienna atlas roles, by official sign numbers; no new material.
# Other official types are deliberately skipped until accurate facing is proven.
SIGN_ROLES = {
    '202': 'prio.give_way',       # Vikeplikt
    '204': 'prio.stop',           # Stopp
    '302': 'reg.no_entry',        # Innkjøring forbudt
    '404.1': 'mand.keep_right',   # Påbudt kjørefelt, høyre
    '404.2': 'mand.keep_left',    # Påbudt kjørefelt, venstre
    '406': 'mand.roundabout',    # Påbudt rundkjøring
}
NUMBER = re.compile(r'^\s*(\d{3}(?:\.\d+)?)\s*(?:-|$)')
POINT = re.compile(r'^\s*POINT\s*(?:Z(?:M)?\s*|M\s*)?\(\s*([-+\d.eE]+)\s+([-+\d.eE]+)(?:\s+([-+\d.eE]+))?\s*\)\s*$', re.I)
_cache_mem = OrderedDict()
_lock = threading.RLock()
_tls = threading.local()
_failure_retry_after = {}


def enabled():
    return os.environ.get(ENV, 'on').strip().lower() in ('on', 'yes', 'true', '1')


def fetch_enabled():
    return os.environ.get(FETCH_ENV, 'on').strip().lower() not in ('off', 'no', 'false', '0')


def cache_dir():
    root = Path(__file__).resolve().parents[1]
    git = root / '.git'
    if git.is_dir():
        return git / 'norway_signs_v05_cache'
    return Path.home() / '.cache' / 'beamng-earth' / 'norway_signs_v05_cache'


def bounds_of_cell(key):
    i, j = key
    return j * LON_STEP, i * LAT_STEP, (j + 1) * LON_STEP, (i + 1) * LAT_STEP


def cell_keys(bbox):
    x0, y0, x1, y1 = bbox
    if not all(math.isfinite(v) for v in bbox) or x0 > x1 or y0 > y1:
        return []
    # Limit to mainland Norway's broad extent; actual records supplied only by NVDB.
    if x1 < NO_BBOX[0] or x0 > NO_BBOX[2] or y1 < NO_BBOX[1] or y0 > NO_BBOX[3]:
        return []
    i0, i1 = math.floor(y0/LAT_STEP), math.floor(y1/LAT_STEP)
    j0, j1 = math.floor(x0/LON_STEP), math.floor(x1/LON_STEP)
    if (i1 - i0 + 1) * (j1 - j0 + 1) > MAX_CELLS_TILE:
        return []
    return [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]


def _wkt_point(value):
    if not isinstance(value, dict):
        return None
    try:
        if int(value.get('srid') or 0) != 4326:
            return None
    except (ValueError, TypeError):
        return None
    s = value.get('wkt')
    hit = POINT.match(s) if isinstance(s, str) else None
    if not hit:
        return None
    try:
        a, b = float(hit.group(1)), float(hit.group(2))
        if not math.isfinite(a) or not math.isfinite(b):
            return None
        if 57.5 <= a <= 72.0 and 3.8 <= b <= 32.6:
            return (b, a)  # NVDB CRS 4326 WKT uses latitude-longitude axis order
        if 3.8 <= a <= 32.6 and 57.5 <= b <= 72.0:
            return (a, b)
    except (TypeError, ValueError, OverflowError):
        return None
    return None


def prop(obj, pid):
    for p in obj.get('egenskaper') or ():
        if isinstance(p, dict) and p.get('id') == pid:
            return p
    return {}


def plate_of(obj):
    """Normalize one NVDB 96 object to (id, role, sign longitude/latitude, road ref longitude/latitude)."""
    if not isinstance(obj, dict):
        return None
    meta = obj.get('metadata') or {}
    if (meta.get('type') or {}).get('id', 96) != 96:
        return None
    raw = prop(obj, 5530).get('verdi')
    hit = NUMBER.match(raw) if isinstance(raw, str) else None
    code = hit.group(1) if hit else None
    if code not in SIGN_ROLES:
        return None
    facing = prop(obj, 1894)
    label = str(facing.get('verdi', '')).strip().lower()
    if label != 'parallelt fra veg':
        return None
    if facing.get('enum_id') not in (None, 2838):
        return None
    geom = obj.get('geometri') or {}
    if geom.get('egengeometri') is not True:
        return None
    pt = _wkt_point(geom)
    road = _wkt_point((obj.get('lokasjon') or {}).get('geometri'))
    if pt is None or road is None:
        return None
    try:
        pid = int(obj['id'])
        if pid <= 0:
            return None
        dlon = (pt[0]-road[0]) * 111320.0 * math.cos(math.radians(pt[1]))
        dlat = (pt[1]-road[1]) * 111320.0
        distance = math.hypot(dlon, dlat)
        if not (math.isfinite(distance) and MIN_OFFSET_M <= distance <= MAX_OFFSET_M):
            return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return [pid, SIGN_ROLES[code], round(pt[0], 8), round(pt[1], 8), round(road[0], 8), round(road[1], 8)]


def build_url(key, start=None):
    bounds = bounds_of_cell(key)
    if not (3.0 < bounds[0] < 33.0 and 57.0 < bounds[1] < 73.0):
        raise ValueError('Outside Norwegian NVDB search bounds')
    query = {'kartutsnitt': ','.join(f'{v:.7f}' for v in bounds), 'srid': '4326',
             'inkluder': 'metadata,egenskaper,geometri,lokasjon',
             'inkluderAntall': 'false', 'antall': PAGE_SIZE}
    if start is not None:
        if not isinstance(start, str) or not (1 <= len(start) <= 1024) or any(c.isspace() for c in start):
            raise ValueError('Invalid NVDB cursor')
        query['start'] = start
    return BASE + '?' + urlencode(query)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('NVDB redirected outside pinned API endpoint')


def request_page(url, opener=None):
    if not url.startswith(BASE + '?'):
        raise ValueError('Unexpected non-NVDB request URL')
    req = Request(url, headers={'Accept': 'application/json', 'X-Client': CLIENT, 'User-Agent': CLIENT})
    open_fn = opener or build_opener(NoRedirect()).open
    with open_fn(req, timeout=6) as response:
        body = response.read(PAGE_MAX_BYTES + 1)
    if len(body) > PAGE_MAX_BYTES:
        raise ValueError('NVDB page over size cap')
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get('objekter'), list):
        raise ValueError('Unexpected NVDB page JSON')
    return data


def download_cell(key, fetch=request_page):
    """All-or-nothing: never cache a truncated page sequence as a complete survey."""
    seen, records, cursor, n_objects = set(), {}, None, 0
    for page_no in range(MAX_PAGES):
        url = build_url(key, cursor)
        if url in seen:
            raise ValueError('NVDB pagination loop')
        seen.add(url)
        data = fetch(url)
        rows = data['objekter']
        if not rows:
            return sorted(records.values(), key=lambda r: r[0])
        for obj in rows:
            try:
                rec = plate_of(obj)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                rec = None  # One malformed sign must never discard the entire cell.
            if rec is not None:
                records[rec[0]] = rec
        n_objects += len(rows)
        if n_objects > MAX_OBJECTS:
            raise ValueError('Too many NVDB objects in grid cell')
        nxt = (data.get('metadata') or {}).get('neste')
        if not nxt:
            return sorted(records.values(), key=lambda r: r[0])
        token = nxt.get('start') if isinstance(nxt, dict) else None
        if not isinstance(token, str) or not token:
            raise ValueError('Missing official NVDB start cursor')
        cursor = token
    raise ValueError('NVDB grid cell exceeded page budget')


def _file_for(key):
    return cache_dir() / ('cell_%d_%d.json' % key)


def _load_disk(key):
    path = _file_for(key)
    try:
        obj = json.loads(path.read_text(encoding='utf8'))
        if obj.get('version') == 5 and obj.get('cell') == list(key):
            return float(obj['saved']), obj['signs']
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return None


def _save_disk(key, rows):
    path = _file_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps({'version': 5, 'cell': list(key), 'saved': time.time(), 'signs': rows},
                       separators=(',', ':')) + '\n').encode('utf8')
    fd, temp = tempfile.mkstemp(prefix='.nvdb05_', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(body)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def cached_cell(key, allow_fetch=True):
    with _lock:
        got = _cache_mem.get(key)
        if got is not None and (time.time() - got[0] < CACHE_TTL_S or not allow_fetch):
            _cache_mem.move_to_end(key)
            return got[1]
        disk = _load_disk(key)
        if disk is not None and (time.time() - disk[0] < CACHE_TTL_S or not allow_fetch):
            _cache_mem[key] = disk
            while len(_cache_mem) > 96:
                _cache_mem.popitem(last=False)
            return disk[1]
        if not allow_fetch or not fetch_enabled():
            return disk[1] if disk else []
        if time.time() < _failure_retry_after.get(key, 0):
            return disk[1] if disk else []
        try:
            rows = download_cell(key)
            _save_disk(key, rows)
            _failure_retry_after.pop(key, None)
            value = (time.time(), rows)
            _cache_mem[key] = value
            while len(_cache_mem) > 96:
                _cache_mem.popitem(last=False)
            return rows
        except (OSError, ValueError, RuntimeError, TimeoutError, json.JSONDecodeError) as exc:
            _failure_retry_after[key] = time.time() + FAILURE_RETRY_S
            LOG.warning('Norway NVDB sign cell %s unavailable (keeping earlier signed scenery): %s', key, exc)
            return disk[1] if disk else []


def in_box(x, y, box):
    return box[0] <= x < box[2] and box[1] <= y < box[3]


def _tile_records(frame, box, allow_fetch=True):
    xs = np.array([box[0], box[0], box[2], box[2]], float)
    ys = np.array([box[1], box[3], box[1], box[3]], float)
    lat, lon, _ = frame.enu_to_geodetic(xs, ys, np.zeros(4))
    bounds = (float(np.min(lon)), float(np.min(lat)), float(np.max(lon)), float(np.max(lat)))
    keys = cell_keys(bounds)
    by_id = {}
    for key in keys:
        for rec in cached_cell(key, allow_fetch=allow_fetch):
            if rec[0] not in by_id:
                by_id[rec[0]] = rec
    # Honor the eight already-confirmed v0.4 source locations if NVDB is unavailable.
    # The fixture is a pinned local sample, not a source of additional guessed signs.
    fixture = Path(__file__).with_name('data') / 'norway_nvdb_signpoints_v04.json'
    try:
        raw = json.loads(fixture.read_text(encoding='utf8'))
        for obj in raw.get('signs', []):
            if obj['number'] != '202' or obj['orientation'] != 'parallelt_fra_veg':
                continue
            pid = int(obj['plate_id'])
            if pid not in by_id:
                pt, rr = obj['point_lonlat'], obj['road_ref_lonlat']
                by_id[pid] = [pid,'prio.give_way',float(pt[0]),float(pt[1]),float(rr[0]),float(rr[1])]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return list(by_id.values())


def projected(frame, box, allow_fetch=True):
    items = _tile_records(frame, box, allow_fetch=allow_fetch)
    if not items:
        return []
    la = np.array([v for row in items for v in (row[3],row[5])], np.float64)
    lo = np.array([v for row in items for v in (row[2],row[4])], np.float64)
    e, n, _ = frame.geodetic_to_enu(la, lo, np.zeros(len(la),np.float64))
    e, n = np.asarray(e).ravel(), np.asarray(n).ravel()
    out = []
    for k, (pid, role, *_rest) in enumerate(items):
        q = np.array([e[2*k],n[2*k]])
        ref = np.array([e[2*k+1],n[2*k+1]])
        face = q - ref
        length = float(np.linalg.norm(face))
        if (in_box(float(q[0]),float(q[1]),box) and
                np.isfinite(face).all() and MIN_OFFSET_M <= length <= MAX_OFFSET_M):
            out.append((pid,role,q,ref,face/length))
    return out


def build_tile(frame, box, elev, decks, sys_row):
    """Return low-cost native sign mesh, cache exact displayed signs for deduping in same call."""
    _tls.rendered = []
    if not enabled() or sys_row is None or sys_row.get('atlas')!='vienna_w':
        return None
    picks = projected(frame, box)
    if not picks:
        return None
    import roadsigns
    import fast
    import exitsigns
    roles, systems, by_id = roadsigns.tables()
    candidates = []
    locations = set()
    for pid,role,q,ref,face in picks:
        entry = by_id.get(role)
        if entry is None or role not in systems['rules']:
            continue
        cell, meta = entry
        # Multiple physical plates on one pole must eventually be stacked.
        # For now, do not draw overlapping panels on top of each other.
        lk = (round(float(q[0]),1), round(float(q[1]),1))
        if lk in locations or exitsigns._on_deck(q,decks,1.5):
            continue
        locations.add(lk)
        candidates.append((pid,role,cell,meta,q,ref,face))
    if not candidates:
        return None
    xy = np.array([obj[4] for obj in candidates],np.float64)
    rr = np.array([obj[5] for obj in candidates],np.float64)
    both = np.vstack([xy,rr])
    la,lo,_ = frame.enu_to_geodetic(both[:,0],both[:,1],np.zeros(len(both)))
    la,lo = np.asarray(la,np.float64), np.asarray(lo,np.float64)
    z_m = np.asarray(elev(la,lo),np.float64).ravel()
    if len(z_m)!=len(both) or not np.isfinite(z_m).all():
        return None
    z = np.asarray(frame.geodetic_to_enu(la,lo,z_m)[2],np.float64).ravel()
    if len(z)!=len(both) or not np.isfinite(z).all():
        return None
    table = np.zeros((len(candidates),10),np.float64)
    for i,(pid,role,cell,meta,q,ref,face) in enumerate(candidates):
        size=float(systems['sizes']['default'][meta['shape']][1])
        table[i]=[float(q[0]),float(q[1]),float(z[i]),float(z[len(candidates)+i]),
                  -float(face[0]),-float(face[1]),float(cell),
                  float(roadsigns.SHAPES[meta['shape']]),size,float(sys_row['mount'][1])]
    p,t,uv,mi=fast._ef.road_sign_mesh(table,int(roles['cols']),int(roles['rows']),
                                    [float(box[0]),float(box[1]),0.0])
    if not len(t):
        return None
    _tls.rendered = [(role,q) for _pid,role,_cell,_meta,q,_ref,_face in candidates]
    return p,t,uv,['bne_signs_vienna_w',roadsigns.METAL],mi


def suppress_inferred(signs, frame):
    """Only suppress inferred signs of identical role near successfully rendered real signs."""
    if not enabled() or len(signs)==0:
        return signs
    rendered = getattr(_tls,'rendered',[])
    if not rendered:
        return signs
    import roadsigns
    rules = roadsigns.tables()[1]['rules']
    keep = np.ones(len(signs), bool)
    for i,row in enumerate(signs):
        rule = rules[int(row[4])]
        q = np.asarray(row[:2],np.float64)
        if any(rule==role and float(np.linalg.norm(q-xy)) <= SUPPRESS_INFERRED_WITHIN_M
               for role,xy in rendered):
            keep[i] = False
    return signs[keep]


def suppress_furniture_yields(rows, frame):
    """Opt-in suppression of cached Forest yields; OFF by default to avoid disappearing signs.

    Network fetches never occur in the furniture build; unrelated props stay.
    """
    if not (enabled() and rows and frame is not None and
            os.environ.get('BNE_ROADSIGNS','off').strip().lower()=='on' and
            os.environ.get('BNE_NVDB_SIGNS_HIDE_FURNITURE','off').strip().lower()=='on'):
        return rows
    potential = [r for r in rows if r[0]=='giveway']
    if not potential:
        return rows
    xy = np.array([r[1:3] for r in potential],float)
    bounds=(float(xy[:,0].min()-20),float(xy[:,1].min()-20),
            float(xy[:,0].max()+20),float(xy[:,1].max()+20))
    points = [(role,q) for _pid,role,q,_ref,_face in projected(frame,bounds,allow_fetch=False)
              if role=='prio.give_way']
    if not points:
        return rows
    return [r for r in rows if r[0]!='giveway' or
            all(float(np.linalg.norm(np.asarray(r[1:3],float)-q))>SUPPRESS_INFERRED_WITHIN_M
                for _role,q in points)]
