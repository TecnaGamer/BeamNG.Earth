"""Norwegian REAL NVDB destination signs v0.7.3.

NVDB type 96 Skiltplate, read-only on demand per geographic cell. No speed signs.
Real positions, sign codes and text; approximate facing for traffic-direction
labels from the right-of-road geometric side (flagged as provisional).
Unknown/empty text, invalid geometry, unsupported types and unreadable directions
are skipped rather than replaced with invented towns.

The existing bne direction renderer supplies the sign mesh protocol. See README.
"""
from __future__ import annotations
from functools import lru_cache
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unicodedata

import numpy as np

import norway_nvdb_signs_v06 as base
from norway_signs_v073 import catalog, coverage, facing as facing_module
from norway_signs_v073 import mounting_v081 as mounting

LOG = logging.getLogger('earthd.no_nvdb_destinations_v07')
ENV = 'BNE_NVDB_DESTINATIONS_V07'
CACHE_TTL = 14 * 86400
RETRY_S = 180
MAX_PER_TILE = 48
MAX_CHARS = 240
SIGN_PREFIXES = ('701', '703', '705', '711', '713', '725', '727', '723')
DESTS = {'701', '711', '713', '725', '727', '723'}
# 703 diagram boards are withheld until a diagram renderer is available.
# 705 motorway exit diagram boards are withheld for the same reason.
PRIORITY = {'206': 'prio.main_road', '208': 'prio.main_road_end'}
SIGN_CODE = re.compile(r'^\s*(\d{3}(?:\.\d+)?)(?:\s*\.\s*([HVhv](?:45|90|135)?|[RUru]))?(?:\s|-|$)')
EMBEDDED_CODE = re.compile(r'^\s*(723\.(?:11|12|13|14|15|16))\s+(.+)$', re.I)
CLEAN_SPACE = re.compile(r'\s+')
NUM_ONLY = re.compile(r'^\d+(?:[.,]\d+)?$')
BAD_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_cache = {}
_retry_after = {}
_lock = threading.RLock()


def enabled():
    return os.environ.get(ENV, 'on').lower() not in ('0', 'off', 'false', 'no')


def cache_dir():
    return base.cache_dir().parent / 'norway_nvdb_destinations_v081_cache'


def _number(raw):
    hit = SIGN_CODE.match(str(raw or ''))
    if not hit:
        return None
    return hit.group(1) + ('.' + hit.group(2).upper() if hit.group(2) else '')


def _kind(code):
    p = code.split('.')[0]
    return p if p in DESTS else (code if code in PRIORITY else None)  # catalogue provides future families


def _rgb_color(val, code):
    """Use NVDB Farge (1294). Never infer motorway colour from road class."""
    color = str(val or '').strip().lower()
    if code == '723.31':
        return 'brun'
    if color in ('gul', 'blå', 'hvit', 'grønn', 'brun'):
        return color
    if code.startswith(('723.11', '723.12', '723.13', '723.14')):
        return 'grønn'
    if code.startswith('723.'):
        return 'hvit'
    if code.startswith('727.'):
        return 'hvit'
    if code in PRIORITY:
        return 'hvit'
    return None  # unknown colour: don't invent a blue / yellow board


def _clean_text(value):
    if not isinstance(value, str):
        return ''
    s = unicodedata.normalize('NFC', value.strip())
    if len(s) > 500 or BAD_CONTROL.search(s):
        return ''
    return s


def parse_text(text, *, distance=False):
    """Decode NVDB 1910 sign content, including 723.xx¤route number¤ shields.

    @ separates components and @@ separates rows. The '¤' characters delimit
    a miniature sign's payload, not literal characters for the painted board.
    Numeric direction/icon codes we can't draw are *not* printed as text.
    """
    text = _clean_text(text)
    if not text:
        return []
    shield = re.compile(r'723\.(11|12|13|14|15|16)(?:\s*¤([^¤]{1,16})¤|\s+([^@¤]{1,16}))', re.I)
    # NVDB also separates the miniature code and its payload with @.
    text = re.sub(r'(723\.(?:11|12|13|14|15|16))\s*@+\s*(?=¤)', r'\1', text)
    # Legacy records place the route payload after one or two @ delimiters.
    # Consume only the number, never the following destination.
    text = re.sub(r'(723\.(?:11|12|13|14|15|16))\s*@{1,2}\s*((?:E\s*)?\d{1,4})(?=\s|@|$)',
                  lambda m:m[1]+'¤'+m[2].replace(' ','')+'¤', text, flags=re.I)
    lines = []
    for ln in text.split('@@')[:8]:
        runs = []
        for part in ln.split('@')[:16]:
            part = CLEAN_SPACE.sub(' ', part.strip())
            if not part:
                continue
            icon = re.fullmatch(r'(77[12](?:\.0)?|790\.15|723\.31)(?:\s*¤[^¤]*¤)?', part)
            if icon:
                runs.append(['symbol', icon.group(1).removesuffix('.0')])
                continue
            pos = 0
            for match in shield.finditer(part):
                before = part[pos:match.start()].strip(' ¤')
                if before and not re.fullmatch(r'\d{3}(?:\.\d+)?', before):
                    runs.append(['text', before])
                label = (match.group(2) or match.group(3) or '').strip()
                if re.fullmatch(r'[A-Za-zÆØÅæøå0-9 /.-]{1,15}', label):
                    runs.append(['route', '723.' + match.group(1), label])
                pos = match.end()
            rest = part[pos:].strip()
            if '¤' in rest:
                # Unsupported/malformed markups aren't visible text.
                rest = re.sub(r'¤[^¤]*¤', '', rest).strip(' ¤')
            if not rest:
                continue
            if distance and re.fullmatch(r'\d{1,3}(?:[,.]\d+)?', rest) and any(
                    p[0]=='text' and any(c.isalpha() for c in p[1])
                    for p in (runs or (lines[-1] if lines else []))):
                runs.append(['text',rest])
                continue
            if re.fullmatch(r'\d{3}(?:\.\d+)?', rest):
                # Unknown sign pictogram or arrow code; never print as place text.
                # An isolated 1-2 digit distance is permitted.
                continue
            if len(rest) > 90 or len(rest) + sum(len(r[-1]) for r in runs) > MAX_CHARS:
                continue
            if re.fullmatch(r'(?:3[0-9]|4[0-4])', rest):
                # NVDB 1910: codes 30..44 designate ARROW pictograms, not distances.
                runs.append(['arrow', rest])
                continue
            if not any(ch.isalpha() for ch in rest) and not NUM_ONLY.match(rest):
                continue
            runs.append(['text', rest])
        if runs:
            lines.append(runs)
    return lines[:8]


def _render_lines(row):
    """For distance signs, combine a recorded distance with its destination."""
    if row['code'] == '723.31':
        lines=parse_text(row.get('text',''))
        if not any(p[0]=='symbol' and p[1]=='723.31' for line in lines for p in line):
            lines.insert(0,[['symbol','723.31']])
        return lines
    if row['kind'] == '723':
        txt = _clean_text(row.get('text', ''))
        # A 3-digit number on an actual 723 route shield is not an NVDB
        # pictogram code (this broke national/county routes such as 116).
        if re.fullmatch(r'(?:E\s*)?\d{1,4}', txt, re.I):
            return [[['text',txt]]]
    lines = parse_text(row['text'], distance=row['kind']=='725')
    if row['kind'] != '725':return lines
    result=[]
    for line in lines:
        numeric=(len(line)==1 and line[0][0]=='text' and
                 re.fullmatch(r'\d{1,3}(?:[,.]\d+)?',line[0][1]))
        if numeric and result and any(p[0]=='text' and any(c.isalpha() for c in p[1]) for p in result[-1]):
            result[-1].extend(line)
        else:result.append(list(line))
    return result


def _validate_point(obj):
    geom = obj.get('geometri') or {}
    if geom.get('egengeometri') is not True:
        return None
    p = base._wkt_point(geom)
    road = base._wkt_point((obj.get('lokasjon') or {}).get('geometri'))
    if p is None or road is None:
        return None
    dx = (p[0] - road[0]) * 111320 * math.cos(math.radians(p[1]))
    dy = (p[1] - road[1]) * 111320
    d = math.hypot(dx, dy)
    if not 1.2 <= d <= 20.0:
        return None
    return [round(p[0], 8), round(p[1], 8), round(road[0], 8), round(road[1], 8)]


def normalize(obj):
    """Compact validated record (JSON-serializable dict), or None."""
    if not isinstance(obj, dict):
        return None
    if (obj.get('metadata') or {}).get('type', {}).get('id', 96) != 96:
        return None
    code = _number(base.prop(obj, 5530).get('verdi'))
    kind = _kind(code) if code else None
    if kind is None or kind == '701':
        # The sign needs a layout diagram; a plain text panel would be wrong.
        return None
    facing = str(base.prop(obj, 1894).get('verdi') or '').strip().lower()
    if facing not in ('parallelt fra veg', 'parallelt mot veg', 'trafikk i metreringsretning',
                      'trafikk mot metreringsretning', 'tosidig'):
        return None
    if facing == 'tosidig' and not catalog.double_sided_supported(code):
        return None
    points = _validate_point(obj)
    if points is None:
        return None
    color = _rgb_color(base.prop(obj, 1294).get('verdi'), code)
    if color is None:
        return None
    text = _clean_text(base.prop(obj, 1910).get('verdi'))
    if kind not in PRIORITY and not (code == '723.31' or parse_text(text) or
            (kind == '723' and bool(re.fullmatch(r'(?:E\s*)?\d{1,4}', text, re.I)))):
        return None
    if kind == '713' and catalog.arrow_side(code) is None:
        # Arrow geometry requires the official left/right variant.
        return None
    try:
        sid = int(obj['id'])
    except (KeyError, TypeError, ValueError):
        return None
    if sid <= 0:
        return None
    parent_ids = [int(n) for relation in (obj.get('relasjoner') or {}).get('foreldre', [])
                  if (relation.get('type') or {}).get('id') == 95
                  for n in relation.get('vegobjekter', []) if isinstance(n, int) and n > 0]
    return {
        'pole_id': min(parent_ids) if parent_ids else None,
        'metering_side': mounting.metering_side(obj),
        'compass': str(base.prop(obj,1897).get('verdi') or '').strip().lower(),
        'different_faces': str(base.prop(obj,9484).get('verdi') or '').strip().lower()=='ja',
        'id': sid, 'code': code, 'kind': kind, 'point': points[:2], 'road': points[2:],
        'facing': facing, 'color': color, 'text': text,
        'width_mm': base.prop(obj, 1328).get('verdi'),
        'height_mm': base.prop(obj, 1588).get('verdi'),
        'text_height_mm': base.prop(obj, 1912).get('verdi'),
        'position_code': base.prop(obj, 5128).get('verdi'),
    }


def download_cell(key, fetch=None):
    """Official NVDB type-96 query with full parent relationships and per-cell audit.

    Separate coverage module is intentionally independent of sign design.
    """
    rows, audit = coverage.scan(base, key, normalize, fetch)
    stats = audit['statistics']
    LOG.info('NVDB signs cell %s: %s raw, %s accepted; reasons: %s', key,
             stats.get('raw_records', 0), stats.get('unique_accepted', 0),
             {k: v for k, v in stats.items() if k not in ('raw_records', 'pages', 'unique_accepted')})
    return rows


def _cache_file(key):
    return cache_dir() / ('cell_%s_%s.json' % key)


def cached_cell(key, *, fetch=None, allow_fetch=True):
    """Cache only fully-read pages. Offline errors do not remove older good cells."""
    now = time.time()
    with _lock:
        item = _cache.get(key)
        if item and (not allow_fetch or now - item['saved'] < CACHE_TTL):
            return item['signs']
        path = _cache_file(key)
        if item is None:
            try:
                disk = json.loads(path.read_text(encoding='utf-8'))
                if disk.get('version') == 81 and disk.get('cell') == list(key):
                    item = disk
                    _cache[key] = disk
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if item and (not allow_fetch or now - item['saved'] < CACHE_TTL):
            return item['signs']
        if not allow_fetch or not enabled() or not base.fetch_enabled() or now < _retry_after.get(key, 0):
            return item['signs'] if item else []
        try:
            signs = download_cell(key, fetch=fetch)
            doc = {'version': 81, 'cell': list(key), 'saved': now, 'signs': signs}
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix='.nvdbdest_', dir=str(path.parent))
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(doc, f, ensure_ascii=False, separators=(',', ':'))
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            _cache[key] = doc
            if len(_cache) > 80:
                _cache.pop(next(iter(_cache)))
            _retry_after.pop(key, None)
            return signs
        except (OSError, RuntimeError, ValueError, TimeoutError, TypeError) as exc:
            _retry_after[key] = now + RETRY_S
            LOG.warning('NVDB Norwegian destination cell %s unavailable: %s', key, exc)
            return item['signs'] if item else []


def tile_records(frame, box, allow_fetch=True):
    """Only records whose PHYSICAL pole is inside the sign tile's owned bbox."""
    xs = np.array([box[0], box[0], box[2], box[2]], np.float64)
    ys = np.array([box[1], box[3], box[1], box[3]], np.float64)
    lat, lon, _ = frame.enu_to_geodetic(xs, ys, np.zeros(4))
    bounds = (min(lon), min(lat), max(lon), max(lat))
    keys = base.cell_keys(tuple(map(float, bounds)))
    by_id = {}
    for key in keys:
        for row in cached_cell(key, allow_fetch=allow_fetch):
            by_id.setdefault(row['id'], row)
    return sorted(by_id.values(), key=lambda r: r['id'])


def face_of(row, frame):
    """Sign front, in 2D ENU. First two modes are geometrically constrained.

    Traffic faces use recorded metering side and compass when available.
    The perpendicular to the road offset is still a tangent approximation;
    curves and ambiguous road references remain provisional.
    """
    lon, lat = row['point']
    rlon, rlat = row['road']
    e, n, _ = frame.geodetic_to_enu(np.array([lat, rlat]), np.array([lon, rlon]), np.zeros(2))
    q = np.array([e[0], n[0]], float)
    ref = np.array([e[1], n[1]], float)
    return _face_from_xy(row, q, ref)


def _face_from_xy(row, q, ref):
    off = q - ref
    dist = float(np.linalg.norm(off))
    if not np.isfinite(off).all() or not 1.2 <= dist <= 25.0:
        return None
    outward = off / dist
    mode = row['facing']
    normal = facing_module.facing(q, ref, mode, catalog.family(row['code']))
    normal=mounting.normal(row,q,ref,normal)
    if normal is None:
        return None
    return q, ref, np.array(normal, float)


def _face_colors(name):
    return {
        'gul': ((245, 205, 18), (10, 10, 10)),
        'blå': ((0, 70, 155), (255, 255, 255)),
        'hvit': ((250, 250, 246), (10, 10, 10)),
        'grønn': ((0, 115, 68), (255, 255, 255)),
        'brun': ((104, 70, 50), (255, 255, 255)),
    }[name]


def _font_path():
    return Path(__file__).resolve().parent / 'data' / 'fonts' / 'Overpass-Bold.ttf'


def _text_font(draw, label, max_size, max_width):
    """Find a large readable Norwegian-style sign font without overflowing."""
    from PIL import ImageFont
    path = str(_font_path()) if Path(_font_path()).is_file() else 'DejaVuSans.ttf'
    for size in range(int(max_size), 9, -2):
        try: font = ImageFont.truetype(path, size)
        except OSError: font = ImageFont.load_default()
        box = draw.textbbox((0, 0), label, font=font)
        if box[2]-box[0] <= max_width:
            return font
    return ImageFont.load_default()


def _draw_shield(draw, xywh, code, label):
    """Norwegian 723.11/13 green, 723.15 white route plate (not a billboard)."""
    x, y, w, h = [int(v) for v in xywh]
    route = str(label).strip().upper().replace(' ', '')
    if not route or len(route) > 9:
        raise ValueError('Invalid route shield content')
    green = code.startswith(('723.11', '723.12', '723.13', '723.14'))
    bg = (0, 115, 63) if green else (255, 255, 253)
    fg = (255, 255, 255) if green else (5, 5, 5)
    draw.rounded_rectangle((x,y,x+w,y+h), radius=max(0,int(h*.065)),fill=bg,
                           outline=fg,width=max(2,int(h*.055)))
    # Route signs: E39, 5, 116; use official route colours rather than black
    # letters accidentally embedded in a yellow direction sign.
    display = re.sub(r'^E(?=\d)', 'E ', route)
    font = _text_font(draw, display, int(h*.60), int(w*.88))
    box = draw.textbbox((0, 0), display, font=font)
    tw, th=box[2]-box[0], box[3]-box[1]
    draw.text((x+(w-tw)/2-box[0],y+(h-th)/2-box[1]),display,font=font,fill=fg)


def _runs_plain(lines, *, shape):
    """Convert NVDB's serialized text into ordered glyphs; no raw markup."""
    rows=[]
    for line in lines:
        clean=[]
        for run in line:
            if run[0]=='arrow' and shape=='713':
                # 713 has a physical pointed end: painting another arrow is wrong.
                continue
            if run[0]=='route' and len(run)>=3:
                clean.append(('route',run[1],run[2]))
            elif run[0]=='text':
                t=run[1].replace('¤','').strip()
                if t: clean.append(('text',t))
            elif run[0]=='symbol':
                clean.append(('symbol',run[1]))
            elif run[0]=='arrow':
                clean.append(('arrow',run[1]))
        if clean: rows.append(clean)
    return rows[:8]


def _draw_face(row, *, canvas=None):
    """Visual-only v0.7.4 renderer; NVDB interpretation and facing are unchanged."""
    from norway_signs_v073 import visual_v074
    canvas = canvas or _texture_canvas(row)
    if row['kind'] in PRIORITY:
        from PIL import Image
        asset = Path(__file__).parent / 'norway_signs_v073' / ('priority_' + row['kind'] + '.png')
        with Image.open(asset) as art:
            return art.convert('RGB').resize(canvas, Image.Resampling.LANCZOS)
    return visual_v074.render(row, _runs_plain(_render_lines(row), shape=row['kind']),
                              canvas=canvas, physical_height_m=_tile_dimensions(row)[1])


def _arrow_symbol(draw, code, x, y, size, color):
    """NVDB Tekst (1910) glyphs, codes 30..44; never print code numbers."""
    directions = {'30':(-1,0),'31':(1,0),'32':(-1,0),'33':(1,0),
                  '34':(0,-1),'35':(-.8,-.6),'36':(.8,-.6),
                  '40':(-.75,-.75),'41':(.75,-.75),'44':(0,1)}
    if code in directions:
        dx,dy=directions[code]
        norm=max(1e-9,(dx*dx+dy*dy)**.5);dx/=norm;dy/=norm
        length=size*.68
        begin=(x-dx*length*.45,y-dy*length*.45)
        tip=(x+dx*length*.55,y+dy*length*.55)
        thick=max(3,int(size*.09))
        draw.line([begin,tip],fill=color,width=thick)
        backx,backy=tip[0]-dx*length*.35,tip[1]-dy*length*.35
        perp=(-dy,dx)
        wing=length*.24
        draw.line([tip,(backx+perp[0]*wing,backy+perp[1]*wing)],fill=color,width=thick)
        draw.line([tip,(backx-perp[0]*wing,backy-perp[1]*wing)],fill=color,width=thick)
    else:
        # Unknown composite arrow — mark to prevent rendering a misleading symbol.
        raise ValueError('Composite arrow layout not yet supported: '+code)


def _font_for(draw, text, path, preferred, max_w):
    from PIL import ImageFont
    for size in range(max(9,preferred),8,-2):
        try: ft=ImageFont.truetype(str(path) if path.is_file() else 'DejaVuSans.ttf',size)
        except OSError: ft=ImageFont.load_default()
        if draw.textbbox((0,0),text,font=ft)[2]<=max_w:
            return ft
    raise ValueError('Text too wide for road sign panel')


def _face_filename(row, out_dir):
    import imagery
    from norway_signs_v073.visual_v074 import letter_height_mm
    canvas = _texture_canvas(row)
    key_data = ['0.8.2', row['code'], row['text'], row['color'], canvas]
    if row['kind'] != '723' or row['code'] == '723.31':
        key_data = ['0.8.4', *key_data[1:], _tile_dimensions(row), letter_height_mm(row)]
    key = hashlib.sha256(json.dumps(key_data, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:18]
    name = 'bne_no_nvdb_%s' % key
    path = Path(out_dir) / (name + '.dds')
    # Existing output already has the exact content/version/dimensions key.
    # Avoid expensive Pillow rendering on cache hits; validate the DDS header.
    try:
        import struct
        with path.open('rb') as f: header=f.read(128)
        if len(header)==128 and header[:4]==b'DDS ' and struct.unpack_from('<I',header,4)[0]==124:
            height,width=struct.unpack_from('<II',header,12)
            if (width,height)==canvas and path.stat().st_size>128:return name,path,canvas
    except OSError:pass
    img = _draw_face(row, canvas=canvas)
    imagery.write_dds(str(path), np.asarray(img, np.uint8))
    return name, path, img.size


def _compact_direction_height(row, width):
    """Estimate missing 711 height from text; retain real measured heights."""
    from PIL import Image, ImageDraw
    from norway_signs_v073 import shields_v074 as sh
    from norway_signs_v073.layout_v077 import content_rows, wrap
    if not row.get('text'):return max(.28,min(1.5,width*.195))
    lines=_runs_plain(_render_lines(row),shape=row['kind'])
    rows=content_rows(lines,'711')
    draw=ImageDraw.Draw(Image.new('RGB',(1,1)))
    path=Path(__file__).parent/'data'/'fonts'/'Overpass-Bold.ttf'
    if not path.is_file():path=Path('DejaVuSansCondensed.ttf')
    font=sh.font(100,path)
    # Nominal letter size scales with width so missing data yields consistent
    # compact strips, independent of geographic location or destination names.
    nominal=width*.105
    badges=any(bs for _,_,bs in rows)
    available=width*(.64 if badges else .78)
    count=sum(len(wrap(draw,t,font,available/nominal*100)) for t,_,_ in rows)
    return max(.28,min(1.5,width*(.195+.115*(max(1,count)-1))))


def _stack_gap(upper, lower):
    """A narrow physical mounting seam between panels on the same support."""
    return min(.025, max(.012, min(upper,lower)*.05))


def _tile_dimensions(row):
    # Only inputs actually used by dimension inference belong in this key.
    fields=('kind','code','text','width_mm','height_mm','text_height_mm')
    values=tuple(row.get(k) for k in fields)
    try:hash(values)
    except TypeError:return _compute_tile_dimensions(row)
    return _cached_tile_dimensions(values)

@lru_cache(maxsize=2048)
def _cached_tile_dimensions(values):
    fields=('kind','code','text','width_mm','height_mm','text_height_mm')
    return _compute_tile_dimensions(dict(zip(fields,values)))

def _compute_tile_dimensions(row):
    """Prefer trustworthy measured sizes; clamp obvious oversize entries."""
    kind=row['kind']
    default={'723':(.68,.43),'727':(1.2,.36),'713':(1.9,.36),
             '701':(2.5,1.1),'711':(1.85,.60),'725':(1.9,.70)}.get(kind,(1.5,.65))
    if kind in PRIORITY:
        # The mesh is measured tip-to-tip. Prefer a plausible recorded
        # footprint and keep both axes equal for a true diamond.
        for field in ('width_mm','height_mm'):
            try:
                span = float(row.get(field)) / 1000
            except (TypeError, ValueError, OverflowError):
                continue
            if .35 <= span <= 1.6:
                return span, span
        return .72, .72
    if kind=='725' and row.get('text'):
        from norway_signs_v073.layout_v077 import content_rows
        rs=content_rows(_render_lines(row),'725')
        header=bool(len(rs)>1 and any(bs for _,_,bs in rs))
        from norway_signs_v073.visual_v074 import letter_height_mm
        cap = letter_height_mm(row) / 1000
        row_pitch = max(.23, 1.90 * cap)
        default = (1.9, max(.36, .12 + row_pitch * (len(rs) + int(header))))
    if kind=='711':default=(1.85,_compact_direction_height(row,1.85))
    if row['code']=='723.31':default=(1.6,.65)
    if kind in ('713','727','725') and row.get('text') and not row.get('width_mm'):
        from norway_signs_v073 import visual_v074 as art, shields_v074 as shields
        from norway_signs_v073.layout_v077 import content_rows
        rows = content_rows(_render_lines(row),kind)
        cap = art.letter_height_mm(row) / 1000
        path = art.sign_font_path()
        metric = shields.font(200,path)
        em = art.nominal_em(cap,path)
        badges = [badge for _,_,bs in rows for badge in bs]
        longest = max((metric.getlength(t) * em / 200 +
                       (metric.getlength(d)*em/200 + em*.6 if d else 0)
                       for t,d,_ in rows), default=0)
        reserve = sum(cap*(2.45 if b[0]=='route' else 1.75) for b in badges)
        if kind=='725' and len(rows)>1:
            reserve = 0  # shared badge above the distance table
        width = (longest + reserve + (.57*default[1] if kind=='713' else 0))/.87
        default = min(5.5,max(default[0],width)),default[1]
    try:
        mw=float(row.get('width_mm'))/1000.
        raw_h=row.get('height_mm')
        if kind == '725' and .5 <= mw <= 5.5:
            try:
                height = float(raw_h)/1000
            except (TypeError, ValueError, OverflowError):
                height = 0
            return mw, height if .25 <= height <= 3.0 else default[1]
        if raw_h is None and .5<=mw<=5.5:
            try: th=float(row.get('text_height_mm'))/1000.
            except (TypeError,ValueError):th=0
            inferred=_compact_direction_height(row,mw) if kind=='711' else (max(.35,th*3.8) if 0<th<.8 else default[1])
            return mw,inferred
        mh=float(raw_h)/1000.
        if kind=='723':
            
            if .15<=mw<=5.5 and .15<=mh<=3.0:
                return mw,mh
            return default
        if kind=='713' and .7<=mw<=3.2:
            return mw,min(max(mh,.3),.85) if .25<=mh<=1.4 else default[1]
        if .50<=mw<=5.5 and .25<=mh<=3.0:
            return mw,mh
    except (TypeError,ValueError,OverflowError):
        pass
    return default


def _texture_canvas(row):
    """One pixel has the same physical scale on both mesh axes."""
    w,h=_tile_dimensions(row)
    longest=512 if row['kind'] in PRIORITY else 2048
    scale=longest/max(w,h)
    return max(8,4*round(w*scale/4)),max(8,4*round(h*scale/4))


def _panel_mesh(row, pos, z, face, mat, origin, w, h, bottom, *, posts=True, post_top=None):
    """Rectangular signboards with painted direction chevrons, not pointed meshes."""
    arrow=row['kind']=='713' and catalog.arrow_side(row['code']) is not None
    two_sided=row.get('facing')=='tosidig' and catalog.double_sided_supported(row['code'])
    points=[(-.5,-.5),(.5,-.5),(.5,.5),(-.5,.5)]
    if row['kind'] in PRIORITY:
        points=[(0.,-.5),(.5,0.),(0.,.5),(-.5,0.)]
    centre=np.asarray(pos,float)
    front=np.asarray(face,float)
    front=front/max(1e-9,float(np.linalg.norm(front)))
    right=np.array([-front[1],front[0]])
    # NVDB point is the board/support location; rectangular boards are centred.
    def horizontal(y):
        return y*w
    verts=[];uv=[]
    for depth in (.018,-.028):
        for xx,yy in points:
            p=centre+right*horizontal(xx)+front*depth
            verts.append([p[0]-origin[0],p[1]-origin[1],bottom+(yy+.5)*h])
            uv.append(([xx+.5,yy+.5] if depth>0 else [1.-(xx+.5),yy+.5])
                      if depth>0 or two_sided else [0.,0.])
    n=len(points)
    # Two separate opaque untextured rear surfaces (opposite windings) keep
    # any two-sided face shader from exposing mirrored letters through the back.
    # Their geometry is only 3-4 extra triangles per side, not new sign models.
    for xx,yy in points:
        p=centre+right*horizontal(xx)+front*(-.055)
        verts.append([p[0]-origin[0],p[1]-origin[1],bottom+(yy+.5)*h])
        uv.append([0.,0.])
    triangles=[[0,i,i+1] for i in range(1,n-1)]
    mats=[mat]*(n-2)
    triangles += [[n,n+i+1,n+i] for i in range(1,n-1)]
    mats += [mat if two_sided else 0]*(n-2)
    # An opaque backing is appropriate only for one-sided plates: otherwise
    # it would hide the second, properly oriented face.
    if not two_sided:
        triangles += [[2*n,2*n+i,2*n+i+1] for i in range(1,n-1)]
        mats += [0]*(n-2)
    for i in range(n):
        j=(i+1)%n
        triangles.extend([[i,j,n+j],[i,n+j,n+i]])
        mats.extend([0,0])
    if posts:
        # One support per actual pole; big independent boards can have two.
        top=post_top if post_top is not None else bottom+h-.06
        positions=[-w*.24,w*.24] if arrow and w>=1.5 else ([0.] if w<=2.05 else [-w*.35,w*.35])
        positions=row.get('_post_offsets',positions)
        for offset in positions:
            r=.04
            x0=centre+right*offset-front*.115
            k=len(verts)
            for dx,dy in ((-r,-r),(r,-r),(r,r),(-r,r)):
                p=x0+front*dx+right*dy
                verts.append([p[0]-origin[0],p[1]-origin[1],z-.25])
                verts.append([p[0]-origin[0],p[1]-origin[1],top])
                uv.extend([[0,0],[0,0]])
            for i in range(4):
                j=(i+1)%4
                triangles.extend([[k+2*i,k+2*j,k+2*j+1],[k+2*i,k+2*j+1,k+2*i+1]])
                mats.extend([0,0])
    return (np.asarray(verts,np.float64),np.asarray(triangles,np.int64),
            np.asarray(uv,np.float64),np.asarray(mats,np.int64))


def _assembled_stack_meshes(grp, bases, origin, *, posts=True):
    """Common metal backing and central supports for coplanar 711 stacks.

    Each texture/section remains separate, preserving NVDB colors and content.
    Ineligible stacks retain independent-board mounting.
    """
    if len(grp)<2 or any(r[0]['kind']!='711' or r[0].get('facing')=='tosidig' for r in grp):
        return None
    widths=[r[4] for r in grp]
    if max(widths)-min(widths)>.03:return None
    q=np.mean([r[1] for r in grp],axis=0)
    front=np.asarray(grp[0][2],float)
    front=front/max(1e-9,float(np.linalg.norm(front)))
    for r in grp:
        other=np.asarray(r[2],float);other=other/max(1e-9,float(np.linalg.norm(other)))
        if float(np.dot(front,other))<.995 or float(np.linalg.norm(r[1]-q))>.35:
            return None
    grounds=[r[6] for r in grp]
    if max(grounds)-min(grounds)>.15:return None
    ground=float(np.mean(grounds))
    low=min(bases[r[0]['id']] for r in grp)
    top=max(bases[r[0]['id']]+r[5] for r in grp)
    rim=.035
    backing={'kind':'mount','code':'mount','_post_offsets':[-.14,.14]}
    result=[_panel_mesh(backing,q-front*.10,ground,front,0,origin,
                       max(widths)+2*rim,top-low+2*rim,ground+low-rim,
                       posts=posts,post_top=ground+top)]
    for row,_,_,idx,w,h,_ in grp:
        result.append(_panel_mesh(row,q,ground,front,idx,origin,w,h,
                                  ground+bases[row['id']],posts=False))
    return result


def _shared_support_mesh(plan,origin):
    # Reuse the tested post geometry, stripping the temporary panel entirely.
    offsets=[-.14,.14] if any(r[0]['kind']=='711' for r in plan['records']) or plan['width']>2.7 else [0.]
    row={'kind':'mount','code':'mount','_post_offsets':offsets}
    p,t,uv,m=_panel_mesh(row,plan['center'],plan['ground'],plan['normal'],0,origin,
                        plan['width'],.01,plan['ground']+plan['top'],posts=True,
                        post_top=plan['ground']+plan['top']-.04)
    keep=np.all(t>=12,axis=1)
    return p[12:],t[keep]-12,uv[12:],m[keep]


def _mounting_meshes(indexed,priorities,origin):
    meshes=[]
    for plan in mounting.plan(indexed,_stack_gap,priorities):
        meshes.append(_shared_support_mesh(plan,origin))
        for batch in plan['batches']:
            grp=batch['records'];bases=batch['bases']
            assembly=_assembled_stack_meshes(grp,bases,origin,posts=False)
            if assembly is not None:meshes.extend(assembly);continue
            for row,q,face,idx,w,h,ground in grp:
                meshes.append(_panel_mesh(row,q,ground,face,idx,origin,w,h,
                                         ground+bases[row['id']],posts=False))
    return meshes


def _arrow_mesh(row, pos, z, front, material, origin, w, h):
    """Compatibility helper used by earlier offline tests."""
    return _panel_mesh(row,pos,z,front,material,origin,w,h,z+1.8)


def _merge_meshes(meshes):
    if not meshes:
        return None
    import busstationsign
    merged=None
    for mesh in meshes:
        merged = busstationsign.merge(merged, mesh)
    return merged


def _group_records(destinations, tolerance=.95):
    """Group only physically coincident signplates (one Skiltpunkt/pole)."""
    groups=[]
    for item in destinations:
        _row,q,_,_=item
        found=next((g for g in groups if float(np.linalg.norm(q-g[0][1]))<tolerance),None)
        if found is None: groups.append([item])
        else: found.append(item)
    return groups


def _mount_order(row):
    """NVDB 5128: smallest Plasseringskode is highest on a pole."""
    try: return (int(row.get('position_code')),row['id'])
    except (TypeError,ValueError): return (99,row['id'])


def _owned_points(records, frame, box):
    """Project a region's poles in one batch and resolve facing only in this tile."""
    if not records:
        return
    points = np.asarray([r['point'] for r in records], dtype=np.float64)
    lon, lat = np.ascontiguousarray(points.T)
    east, north, _ = frame.geodetic_to_enu(lat, lon, 0.0)
    # Same half-open ownership rule as base.in_box. Test exact edge coordinates.
    inside = (box[0] <= east) & (east < box[2]) & (box[1] <= north) & (north < box[3])
    owners = [i for i in np.flatnonzero(inside) if records[i]['kind'] != '701']
    if not owners:
        return
    refs = np.asarray([records[i]['road'] for i in owners], dtype=np.float64)
    rlon, rlat = np.ascontiguousarray(refs.T)
    rx, ry, _ = frame.geodetic_to_enu(rlat, rlon, 0.0)
    for i, ex, ny in zip(owners, rx, ry):
        yield records[i], np.array([east[i], north[i]]), np.array([ex, ny])


def build_tile(frame, box, elev, decks, out_dir, vfs_dir):
    """Measured-position destination faces; stacked plates share supports."""
    if not enabled(): return None,{}
    import exitsigns
    records=tile_records(frame,box)
    priorities=[]
    destinations=[]
    for row,q,ref in _owned_points(records, frame, box):
        projected = _face_from_xy(row, q, ref)
        if projected is None or exitsigns._on_deck(q, decks, 1.2):
            continue
        _, _, face = projected
        if row['kind'] in PRIORITY:
            priorities.append((row,q,ref,face))
            continue
        distance=float(np.linalg.norm(q-ref))
        if distance>(12.0 if row['kind'] in ('713','723','727') else 18.):
            continue
        if _render_lines(row):
            destinations.append((row,q,ref,face))
    destinations=destinations[:MAX_PER_TILE]+priorities
    if not destinations:return None,{}
    mats=['bne_dir_metal']; material_indices={'bne_dir_metal':0}; faces={}; prepared=[]
    for row,q,ref,face in destinations:
        try:
            name,path,_=_face_filename(row,out_dir)
        except (OSError,ValueError,TypeError) as exc:
            LOG.debug('Skip illegible NVDB sign %s: %s',row['id'],exc)
            continue
        if name not in material_indices:
            material_indices[name]=len(mats)
            mats.append(name);faces[name]=f'{vfs_dir}/{path.name}'
        w,h=_tile_dimensions(row)
        prepared.append((row,q,ref,face,material_indices[name],w,h))
    if not prepared:return None,{}
    pts=np.array([rec[1] for rec in prepared],float)
    la,lo,_=frame.enu_to_geodetic(pts[:,0],pts[:,1],np.zeros(len(pts)))
    hz=np.asarray(elev(la,lo),float).ravel()
    if len(hz)!=len(pts) or not np.isfinite(hz).all():return None,{}
    z=np.asarray(frame.geodetic_to_enu(la,lo,hz)[2],float).ravel()
    if len(z)!=len(pts) or not np.isfinite(z).all():return None,{}
    # A shared NVDB parent95 ID identifies one support. Legacy cache lacks it:
    # group only extremely close coordinates, not unrelated signs 0.95 m apart.
    indexed=[(r,q,face,idx,w,h,float(z[i])) for i,(r,q,_,face,idx,w,h) in enumerate(prepared)]
    meshes=[(p,t,uv,mats,mi) for p,t,uv,mi in
            _mounting_meshes(indexed,(),[box[0],box[1]])]
    return _merge_meshes(meshes),faces


def build_priority_tile(frame, box, elev, decks, sys_row):
    """Compatibility hook. build_tile now mounts the surveyed 206/208 plates."""
    return None


def suppress_inferred_norway(signs):
    """No guessed regulatory sign when we're using surveyed NVDB signage."""
    return signs[:0] if enabled() else signs


def suppress_furniture_yields(rows, frame):
    """No guessed yield sign in Norway, preserve other countries' props."""
    if not enabled() or frame is None or not rows:
        return rows
    try:
        import countrybounds
        idx=[k for k,r in enumerate(rows) if r and r[0]=='giveway']
        if not idx:
            return rows
        xy=np.asarray([rows[k][1:4] for k in idx],float)
        la,lo,_=frame.enu_to_geodetic(xy[:,0],xy[:,1],xy[:,2])
        codes=countrybounds.bundled().codes_at([(float(a),float(b)) for a,b in zip(la,lo)])
        drop={k for k,c in zip(idx,codes) if c=='NO'}
        return [r for k,r in enumerate(rows) if k not in drop]
    except Exception:
        return rows
