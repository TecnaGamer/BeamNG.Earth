"""NVDB nationwide on-demand caching for experimental BeamNG.Earth Norway add-on.

Never performs network IO in terrain workers. Pre-fetch with CLI for immediate use.
Only stdlib for downloading; NumPy is loaded in the existing road modules.
"""
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen
import json
import logging
import math
import os
import re
import threading
import time

LOG = logging.getLogger('earthd.nvdb_nationwide_v05')
BASE = 'https://nvdbapiles.atlas.vegvesen.no/vegobjekter/api/v4/vegobjekter/'
# Small geographic tiles; approx 3-4 km per side at central Norway.
STEP = 0.035
CACHE = Path(os.environ.get('BNE_NVDB_CACHE', str(Path.home()/'.cache'/'beamng-earth'/'nvdb-norway-v05')))
POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='nvdb-download')
PENDING = set()
LOCK = threading.Lock()
COORD = re.compile(r'[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')
VERSIONS = ('838', '583', '99')
MAX_PAGES = 100
MAX_OBJECTS = 15000
RETRY_AFTER = 600


def norway(lat, lon):
    return 57.5 <= lat <= 72 and 3.9 <= lon <= 32.5


def key(lat, lon):
    return (int(math.floor(lat / STEP)), int(math.floor(lon / STEP)))


def bounds(k):
    iy, ix = k
    return [round(ix*STEP, 7), round(iy*STEP, 7), round((ix+1)*STEP, 7), round((iy+1)*STEP, 7)]


def path(k, kind):
    return CACHE / f'{k[0]}_{k[1]}_{kind}.geojson'


@lru_cache(maxsize=12)
def _read_cached(k,kind,stamp):
    try:
        doc=json.loads(path(k,kind).read_text(encoding='utf8'))
        if doc.get('type')!='FeatureCollection' or not isinstance(doc.get('features'),list):return None
        return doc['features']
    except (OSError,ValueError,TypeError):return None


def read_tile(k,kind):
    p=path(k,kind)
    try:stamp=p.stat().st_mtime_ns
    except OSError:return None
    return _read_cached(k,kind,stamp)


def parse_wkt(wkt):
    if not isinstance(wkt,str) or not wkt.upper().lstrip().startswith(('LINESTRING','MULTILINESTRING')):
        return []
    body=wkt[wkt.find('(')+1:wkt.rfind(')')]
    pieces=re.split(r'\)\s*,\s*\(',body.strip('()')) if wkt.upper().lstrip().startswith('MULTI') else [body]
    lines=[]
    for piece in pieces:
        pts=[]
        for item in piece.split(','):
            nums=COORD.findall(item)
            if len(nums)<2: continue
            a,b=map(float,nums[:2])
            # NVDB API WKT with srid=4326 is often latitude first; verify axes by Norwegian ranges.
            if 57.5 <= a <= 72 and 3.9 <= b <= 32.5: lat,lon=a,b
            elif 3.9 <= a <= 32.5 and 57.5 <= b <= 72: lon,lat=a,b
            else: continue
            pts.append([lon,lat])
        if len(pts)>=2:lines.append(pts)
    return lines


def properties(obj):
    return {str(p.get('navn','')).casefold():p.get('verdi') for p in obj.get('egenskaper',[]) if isinstance(p,dict)}


def side_of(obj):
    loc=obj.get('lokasjon') or {}
    sites=loc.get('stedfestinger') or loc.get('vegsegmenter') or loc.get('veglenkesekvenser') or []
    if isinstance(sites,dict):sites=[sites]
    if not isinstance(sites,list):return ''
    sides={str(s.get('sideposisjon','')).upper() for s in sites if isinstance(s,dict)}- {''}
    return next(iter(sides)) if len(sides)==1 else ''


def width_field(props, kind):
    """Conservative value selection. 838's property names may differ by data version.

    Return (width, source property name); never invent shoulder additions.
    """
    preferred = (
        'vegbredde, totalt', 'vegbredde, beregnet', 'beregnet vegbredde',
        'vegbredde, gjennomsnitt', 'vegbredde, gj.snitt',
        'gjennomsnittlig vegbredde', 'vegbredde', 'beregnet bredde',
        'bredde, gjennomsnitt', 'bredde',
    )
    secondary = ('dekkebredde, gj.snitt', 'dekkebredde, gjennomsnitt', 'dekkebredde')
    # Only choose paved width when total is not registered. Do not pretend it is total.
    for name in preferred + secondary:
        value = props.get(name)
        if value is None or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and 2.5 <= number <= 15.0:
            return number, name
    return None, None


def converted(objects, kind):
    features=[]
    input_objects=0
    missing_width=0
    width_fields=set()
    for obj in objects:
        input_objects+=1
        props=properties(obj)
        try:
            if kind in ('583','838'):
                total, name = width_field(props, kind)
                if total is None:
                    missing_width+=1
                    if len(width_fields)<40: width_fields.update(props.keys())
                    continue
                paved=props.get('dekkebredde')
                try: paved=float(paved) if paved is not None else None
                except (TypeError, ValueError): paved=None
                values={'nvdb_id':obj.get('id'),'total_width_m':total,'paved_width_m':paved,
                        'source_type':kind,'width_field':name}
            else:
                typ=str(props.get('type') or '')
                if not typ: continue
                values={'nvdb_id':obj.get('id'),'type':typ,'sideposisjon':side_of(obj),
                        'bredde_enkeltlinje':props.get('bredde, enkeltlinje') or 0.10,
                        'bruksomrade':props.get('bruksområde'),'farge':props.get('farge')}
        except (TypeError,ValueError):continue
        g=obj.get('geometri') or {}
        for line in parse_wkt(g.get('wkt','')):
            features.append({'type':'Feature','id':obj.get('id'), 'properties':values,
                             'geometry':{'type':'LineString','coordinates':line}})
    return {'type':'FeatureCollection','features':features,
            'stats':{'input_objects':input_objects,'unparsed_width_objects':missing_width,
                     'unparsed_width_fields':sorted(width_fields)}}


def request(url):
    if urlsplit(url).hostname != 'nvdbapiles.atlas.vegvesen.no' or urlsplit(url).scheme!='https':
        raise ValueError('Unsafe NVDB pagination URL')
    req=Request(url,headers={'Accept':'application/json','X-Client':'BeamNGEarth-NorwegianRoads/0.5.1',
                             'User-Agent':'BeamNGEarth-NorwegianRoads/0.5.1'})
    with urlopen(req,timeout=25) as r:return json.load(r)


def download(k, kind):
    params={'kartutsnitt':','.join(map(str,bounds(k))),'srid':'4326',
            'inkluder':'egenskaper,lokasjon,geometri','antall':200}
    url=BASE+kind+'?'+urlencode(params)
    seen=set();objects={};pages=0
    while url and pages < MAX_PAGES:
        if url in seen:raise RuntimeError('NVDB pagination loop')
        seen.add(url)
        doc=request(url);pages+=1
        rows=doc.get('objekter',[])
        if not isinstance(rows,list):raise RuntimeError('Unexpected NVDB response')
        for rec in rows:
            if isinstance(rec,dict) and rec.get('id') is not None:objects[str(rec['id'])]=rec
        if len(objects)>MAX_OBJECTS:raise RuntimeError('NVDB tile exceeded safe object cap')
        meta=doc.get('metadata') or {}; nxt=meta.get('neste') or {}
        href=nxt.get('href') if isinstance(nxt,dict) else None
        url=urljoin(BASE,href) if href and rows else None
    if url:raise RuntimeError('NVDB tile exceeded page cap')
    return converted(objects.values(),kind)


def write(k, kind, doc):
    p=path(k,kind);p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_name(p.name+f'.{os.getpid()}.tmp')
    try:
        tmp.write_text(json.dumps(doc,ensure_ascii=False,separators=(',',':')),encoding='utf8')
        os.replace(tmp,p)
    finally:
        tmp.unlink(missing_ok=True)


def fetch(k,kind):
    """Background-only fetch. Cross-process lock prevents duplicate worker downloads."""
    lockfile=path(k,kind).with_suffix('.lock')
    failfile=path(k,kind).with_suffix('.retry')
    acquired=False
    try:
        if read_tile(k,kind) is not None:return 0
        if failfile.exists() and time.time()-failfile.stat().st_mtime<RETRY_AFTER:return None
        lockfile.parent.mkdir(parents=True,exist_ok=True)
        try:
            fd=os.open(str(lockfile),os.O_CREAT | os.O_EXCL | os.O_WRONLY,0o600)
            os.close(fd);acquired=True
        except FileExistsError:
            # If a previous process died mid-download, discard stale locks.
            try:
                if time.time()-lockfile.stat().st_mtime>180:
                    lockfile.unlink(missing_ok=True)
            except OSError:pass
            return None
        doc=download(k,kind)
        write(k,kind,doc)
        failfile.unlink(missing_ok=True)
        LOG.info('NVDB v0.5 cached %s tile %s (%d features)',kind,k,len(doc['features']))
        return len(doc['features'])
    except Exception as exc:
        LOG.warning('NVDB v0.5 %s tile %s download failed: %s',kind,k,exc)
        try:failfile.touch()
        except OSError:pass
        return None
    finally:
        if acquired:
            try:lockfile.unlink(missing_ok=True)
            except OSError:pass
        with LOCK:PENDING.discard((k,kind))


def schedule(k,kind):
    with LOCK:
        if (k,kind) in PENDING:return
        PENDING.add((k,kind))
    POOL.submit(fetch,k,kind)


def features(lat,lon,kind):
    """Never block for network; use cached data, queue request and fall back otherwise."""
    lat=float(lat);lon=float(lon)
    if not norway(lat,lon):return []
    k=key(lat,lon)
    cached=read_tile(k,kind)
    if cached is not None:return cached
    bad=path(k,kind).with_suffix('.retry')
    try:
        if bad.exists() and time.time()-bad.stat().st_mtime<RETRY_AFTER:return []
    except OSError:pass
    schedule(k,kind)
    return []


def prefetch(lat,lon,radius_km=2):
    if not norway(lat,lon):raise ValueError('Outside Norwegian coverage')
    dlat=max(0.0,radius_km)/111.32
    dlon=max(0.0,radius_km)/(111.32*max(.15,math.cos(math.radians(lat))))
    low=key(lat-dlat,lon-dlon);high=key(lat+dlat,lon+dlon)
    ks={(iy,ix) for iy in range(low[0],high[0]+1) for ix in range(low[1],high[1]+1)}
    if len(ks)>250:raise ValueError('Requested too many NVDB tiles')
    count=0;failure=0
    for k in sorted(ks):
        for kind in VERSIONS:
            if read_tile(k,kind) is not None:continue
            ok=fetch(k,kind)
            if ok is None:failure+=1
            else:count+=1
    return count,failure


def seed(k,kind,features):
    write(k,kind,{'type':'FeatureCollection','features':features})


# Nationwide v0.5.1: use multiple nearby cached cells when matching roads
# that straddle geographic tile boundaries. Only the central cell queues downloads.
def near_keys(lat, lon):
    cy, cx = key(float(lat), float(lon))
    return tuple((cy+dy, cx+dx) for dy in (-1,0,1) for dx in (-1,0,1))


def signature(keys, kind):
    out=[]
    for k in keys:
        p=path(k,kind)
        try:
            st=p.stat()
            out.append((k,st.st_mtime_ns,st.st_size))
        except OSError:
            out.append((k,0,0))
    return tuple(out)


@lru_cache(maxsize=24)
def _combined(kind, sig):
    out=[]
    seen=set()
    for k, _, _ in sig:
        for f in read_tile(k,kind) or ():
            geo=f.get('geometry') or {}
            coords=geo.get('coordinates') or []
            fid=str((f.get('properties') or {}).get('nvdb_id',f.get('id','')))
            if not coords:continue
            ident=(fid,tuple(coords[0][:2]),tuple(coords[-1][:2]))
            if ident in seen:continue
            seen.add(ident);out.append(f)
    return tuple(out)


def near_features(lat,lon,kind):
    # Nonblocking queue only for the tile being traversed; prefetch CLI
    # can seed its neighbours before entering BeamNG.Earth.
    features(lat,lon,kind)
    return _combined(kind,signature(near_keys(lat,lon),kind))


def report(lat,lon):
    """Offline, nonblocking diagnostic for the cached cells near a location."""
    la=float(lat);lo=float(lon)
    result={'cache':str(CACHE),'latitude':la,'longitude':lo,'central_tile':key(la,lo)}
    for kind in VERSIONS:
        sig=signature(near_keys(la,lo),kind)
        found=sum(st[1]!=0 for st in sig)
        records=_combined(kind,sig)
        result[kind]={'cached_tiles':found,'nearby_records':len(records),
                      'centre_cached':path(key(la,lo),kind).is_file(),
                      'error_marker':path(key(la,lo),kind).with_suffix('.retry').is_file()}
        # The original API may have records, but their property labels may not
        # match our conservative width parser. Surface that in diagnostics.
        try:
            content=json.loads(path(key(la,lo),kind).read_text(encoding='utf8'))
            if isinstance(content.get('stats'),dict): result[kind]['download_stats']=content['stats']
        except (OSError,ValueError,TypeError):pass
    return result
