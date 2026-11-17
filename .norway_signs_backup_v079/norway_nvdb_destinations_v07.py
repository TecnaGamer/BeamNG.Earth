"""Norwegian REAL NVDB destination signs v0.7.3.

NVDB type 96 Skiltplate, read-only on demand per geographic cell. No speed signs.
Real positions, sign codes and text; approximate facing for traffic-direction
labels from the right-of-road geometric side (flagged as provisional).
Unknown/empty text, invalid geometry, unsupported types and unreadable directions
are skipped rather than replaced with invented towns.

The existing bne direction renderer supplies the sign mesh protocol. See README.
"""
from __future__ import annotations
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
    return base.cache_dir().parent / 'norway_nvdb_destinations_v077_cache'


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


def parse_text(text):
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
    lines = []
    for ln in text.split('@@')[:8]:
        runs = []
        for part in ln.split('@')[:16]:
            part = CLEAN_SPACE.sub(' ', part.strip())
            if not part:
                continue
            icon = re.fullmatch(r'(771(?:\.0)?|723\.31)(?:\s*¤[^¤]*¤)?', part)
            if icon:
                runs.append(['symbol', '771' if icon.group(1).startswith('771') else '723.31'])
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
    lines = parse_text(row['text'])
    if row['kind'] != '725' or len(lines) < 2:
        return lines
    for n, line in enumerate(lines):
        if len(line) == 1 and line[0][0] == 'text' and re.fullmatch(r'\d{1,3}(?:[,.]\d)?', line[0][1]):
            other = next((i for i, entry in enumerate(lines) if i != n and
                          any(k[0] == 'text' and any(c.isalpha() for c in k[1]) for k in entry)), None)
            if other is not None:
                result = [list(entry) for entry in lines]
                result[other].extend(line)
                result.pop(n)
                return result
    return lines


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
                if disk.get('version') == 77 and disk.get('cell') == list(key):
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
            doc = {'version': 77, 'cell': list(key), 'saved': now, 'signs': signs}
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

    Directional signs face the approaching traffic. For NVDB's 'trafikk'
    labels we use right-side-of-carriageway inference, NOT a metering-grade
    heading. This can be wrong for medians/oneways; retain as provisional.
    """
    lon, lat = row['point']
    rlon, rlat = row['road']
    e, n, _ = frame.geodetic_to_enu(np.array([lat, rlat]), np.array([lon, rlon]), np.zeros(2))
    q = np.array([e[0], n[0]], float)
    ref = np.array([e[1], n[1]], float)
    off = q - ref
    dist = float(np.linalg.norm(off))
    if not np.isfinite(off).all() or not 1.2 <= dist <= 25.0:
        return None
    outward = off / dist
    mode = row['facing']
    normal = facing_module.facing(q, ref, mode, catalog.family(row['code']))
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
    return visual_v074.render(row, _runs_plain(_render_lines(row), shape=row['kind']), canvas=canvas or _texture_canvas(row))


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
    from PIL import Image
    import imagery
    key = hashlib.sha256(json.dumps(['0.7.8',row['code'],row['text'],row['color'],_texture_canvas(row)],
                                    ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:18]
    name = 'bne_no_nvdb_%s' % key
    path = Path(out_dir) / (name + '.dds')
    img = _draw_face(row)
    if not path.is_file():
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
    """Prefer trustworthy measured sizes; clamp obvious oversize entries."""
    kind=row['kind']
    default={'723':(.68,.43),'727':(1.2,.36),'713':(1.65,.43),
             '701':(2.5,1.1),'711':(1.85,.60),'725':(1.9,.70)}.get(kind,(1.5,.65))
    if kind=='711':default=(1.85,_compact_direction_height(row,1.85))
    if row['code']=='723.31':default=(1.6,.65)
    try:
        mw=float(row.get('width_mm'))/1000.
        raw_h=row.get('height_mm')
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
    longest=2048
    scale=longest/max(w,h)
    return max(8,4*round(w*scale/4)),max(8,4*round(h*scale/4))


def _panel_mesh(row, pos, z, face, mat, origin, w, h, bottom, *, posts=True, post_top=None):
    """Thin arrow/rectangle sign with texture-correct outline and shared support.

    The pole is at the *butt* of a 713 arrow, not its geometric center.
    This prevents the point from intruding through adjacent panels on the pole.
    """
    arrow=row['kind']=='713' and catalog.arrow_side(row['code']) is not None
    is_right=catalog.arrow_side(row['code'])=='H'
    two_sided=row.get('facing')=='tosidig' and catalog.double_sided_supported(row['code'])
    if arrow:
        points=([(-.5,-.5),(.19,-.5),(.5,0),(.19,.5),(-.5,.5)] if is_right else
                [(.5,-.5),(-.19,-.5),(-.5,0),(-.19,.5),(.5,.5)])
    else:
        points=[(-.5,-.5),(.5,-.5),(.5,.5),(-.5,.5)]
    centre=np.asarray(pos,float)
    front=np.asarray(face,float)
    front=front/max(1e-9,float(np.linalg.norm(front)))
    right=np.array([-front[1],front[0]])
    # For the two arrow directions, keep the mounting end by the post.
    def horizontal(y):
        if arrow:
            return (y+.5)*w-.10 if is_right else (y+.5)*w-(w-.10)
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
        positions=[0.] if w<=2.05 or arrow else [-w*.35,w*.35]
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


def build_tile(frame, box, elev, decks, out_dir, vfs_dir):
    """Measured-position destination faces; stacked plates share supports."""
    if not enabled(): return None,{}
    import exitsigns
    records=tile_records(frame,box)
    priorities=[]
    for r in records:
        if r['kind'] in PRIORITY:
            xy=face_of(r,frame)
            if xy is not None: priorities.append(xy[0])
    destinations=[]
    for row in records:
        if row['kind'] in PRIORITY: continue
        # Do not pretend to draw a complex junction diagram as a text poster.
        if row['kind']=='701': continue
        projected=face_of(row,frame)
        if projected is None: continue
        q,ref,face=projected
        if not base.in_box(q[0],q[1],box) or exitsigns._on_deck(q,decks,1.2): continue
        # v0.7.2: do NOT drop 723 route shields co-located with 206/208.
        # Place these above the priority diamond in the shared support area.
        distance=float(np.linalg.norm(q-ref))
        if distance>(12.0 if row['kind'] in ('713','723','727') else 18.):
            # Avoid large map-offset artefacts on remote embankments.
            continue
        if not _render_lines(row): continue
        destinations.append((row,q,ref,face))
    destinations=destinations[:MAX_PER_TILE]
    if not destinations:return None,{}
    mats=['bne_dir_metal']; faces={}; prepared=[]
    for row,q,ref,face in destinations:
        try:
            name,path,_=_face_filename(row,out_dir)
        except (OSError,ValueError,TypeError) as exc:
            LOG.debug('Skip illegible NVDB sign %s: %s',row['id'],exc)
            continue
        if name not in mats:
            mats.append(name);faces[name]=f'{vfs_dir}/{path.name}'
        w,h=_tile_dimensions(row)
        prepared.append((row,q,ref,face,mats.index(name),w,h))
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
    groups=[]
    for rec in indexed:
        hit=next((g for g in groups if
                  ((rec[0].get('pole_id') and rec[0].get('pole_id') == g[0][0].get('pole_id')) or
                   (not rec[0].get('pole_id') and not g[0][0].get('pole_id') and
                    np.linalg.norm(rec[1]-g[0][1]) < .35))),None)
        if hit is None:groups.append([rec])
        else:hit.append(rec)
    meshes=[]
    for grp in groups:
        grp.sort(key=lambda k:_mount_order(k[0]))
        ordered=list(reversed(grp))
        # The priority renderer mounts 206/208 at ~1.5 m; if a route shield
        # shares this Skiltpunkt, move the destination stack above the diamond.
        shares_priority = any(float(np.linalg.norm(grp[0][1]-p)) < 1.5 for p in priorities)
        bottom=2.55 if shares_priority else 1.78
        bases={}
        for n,rec in enumerate(ordered):
            bases[rec[0]['id']]=bottom
            next_height=ordered[n+1][5] if n+1<len(ordered) else rec[5]
            bottom+=rec[5]+_stack_gap(rec[5],next_height)
        max_top=max(bases[rec[0]['id']]+rec[5] for rec in grp)
        for i,rec in enumerate(grp):
            row,q,face,idx,w,h,ground=rec
            p,t,uv,mi=_panel_mesh(row,q,ground,face,idx,[box[0],box[1]],w,h,
                                   ground+bases[row['id']],posts=(i==0),post_top=ground+max_top-.05)
            meshes.append((p,t,uv,mats,mi))
    return _merge_meshes(meshes),faces


def build_priority_tile(frame, box, elev, decks, sys_row):
    """Render the additional real 206/208 priority signs using existing Vienna atlas."""
    if not enabled() or sys_row is None or sys_row.get('atlas') != 'vienna_w':
        return None
    import exitsigns, fast, roadsigns
    records=tile_records(frame,box)
    _,systems,by_id=roadsigns.tables()
    cand=[]
    for row in records:
        if row['kind'] not in PRIORITY:
            continue
        p=face_of(row,frame)
        if p is None:
            continue
        q,_,face=p
        if not base.in_box(q[0],q[1],box) or exitsigns._on_deck(q,decks,1.2):
            continue
        role=PRIORITY[row['kind']]
        if role not in by_id:
            continue
        cand.append((row,q,face,role))
    if not cand:
        return None
    pts=np.asarray([q for _,q,_,_ in cand],float)
    la,lo,_=frame.enu_to_geodetic(pts[:,0],pts[:,1],np.zeros(len(pts)))
    h=np.asarray(elev(la,lo),float).ravel()
    z=np.asarray(frame.geodetic_to_enu(la,lo,h)[2],float).ravel()
    if len(z)!=len(pts) or not np.isfinite(z).all():
        return None
    tab=[]
    for i,(row,q,face,role) in enumerate(cand):
        cell,meta=by_id[role]
        w=float(systems['sizes']['default'][meta['shape']][1])
        tab.append([q[0],q[1],z[i],z[i],-face[0],-face[1],cell,roadsigns.SHAPES[meta['shape']],w,1.5])
    roles=roadsigns.tables()[0]
    p,t,uv,mi=fast._ef.road_sign_mesh(np.asarray(tab,float),roles['cols'],roles['rows'],
                                      [float(box[0]),float(box[1]),0.])
    if not len(t):
        return None
    return p,t,uv,['bne_signs_vienna_w',roadsigns.METAL],mi


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
