#!/usr/bin/env python3
"""Read-only NVDB type-96 coverage check; can prewarm v0.7.3's real sign cache.

Place beside run_server.bat in the BeamNG.Earth checkout. This tool does NOT
create signs that aren't represented in the source, and doesn't touch roads.
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'server'))
import norway_nvdb_signs_v06 as base
import norway_nvdb_destinations_v07 as d
from norway_signs_v073 import coverage


def cells_near(lat, lon, radius_m, max_cells=12):
    if not (57.5 <= lat <= 72 and 3.8 <= lon <= 32.6):
        raise ValueError('Choose a position inside mainland Norway.')
    if radius_m < 50 or radius_m > 1400:
        raise ValueError('radius-m must be 50-1400 metres')
    dlat = radius_m / 111320.0
    dlon = radius_m / (111320.0 * math.cos(math.radians(lat)))
    bounds = (lon-dlon,lat-dlat,lon+dlon,lat+dlat)
    i0,i1=math.floor(bounds[1]/base.LAT_STEP), math.floor(bounds[3]/base.LAT_STEP)
    j0,j1=math.floor(bounds[0]/base.LON_STEP), math.floor(bounds[2]/base.LON_STEP)
    keys=[(i,j) for i in range(i0,i1+1) for j in range(j0,j1+1)]
    if len(keys)>max_cells:
        raise ValueError(f'Area touches {len(keys)} NVDB cells; use a smaller radius (max {max_cells}).')
    return keys


def write_cell_cache(key, signs):
    dest = d._cache_file(key)
    dest.parent.mkdir(parents=True,exist_ok=True)
    doc={'version':73,'cell':list(key),'saved':time.time(),'signs':signs}
    fd,tmp=tempfile.mkstemp(prefix='.nvdb_sign073_',dir=str(dest.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(doc,f,ensure_ascii=False,separators=(',',':'))
        os.replace(tmp,dest)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    with d._lock:
        d._cache[key]=doc


def run(lat,lon,radius_m, cache_only=False):
    keys=cells_near(lat,lon,radius_m)
    total=Counter(); by_kind=Counter(); reports=[]; records={}
    for key in keys:
        try:
            if cache_only:
                fp=d._cache_file(key)
                doc=json.loads(fp.read_text('utf-8'))
                if doc.get('version')!=73 or doc.get('cell')!=list(key):
                    raise ValueError('Cache has old schema')
                signs=doc['signs']
                audit={'cell':list(key),'statistics':{'unique_accepted':len(signs)},
                       'accepted_by_family':dict(Counter(x['kind'] for x in signs)),
                       'note':'Cached accepted records only; use without --cache-only for rejection reasons'}
            else:
                signs,audit=coverage.scan(base,key,d.normalize)
                write_cell_cache(key,signs)
            for r in signs:
                records[r['id']]=r
            total.update(audit['statistics']);by_kind.update(audit['accepted_by_family'])
            reports.append(dict(cell=list(key),status='ok',**audit))
            print(f"cell {key}: {len(signs)} supported signs, {audit['statistics'].get('raw_records','cached')} raw objects")
        except (OSError,ValueError,RuntimeError,TypeError) as e:
            reports.append({'cell':list(key),'status':'error','error':str(e)})
            print(f'cell {key}: unavailable: {e}',file=sys.stderr)
    return {'version':'0.7.3','centre':{'lat':lat,'lon':lon,'radius_m':radius_m},
            'source':'NVDB API Les v4, Skiltplate 96',
            'cells_requested':len(keys),'cells_ok':sum(r['status']=='ok' for r in reports),
            'unique_accepted_in_area_cells':len(records),
            'totals':dict(total),'accepted_by_family':dict(by_kind),'cells':reports,
            'note':'The cells overlap the requested circle; counts refer to whole cache cells. No fictitious signs were added. NVDB coverage and renderer eligibility are separate.'}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--lat',type=float,required=True)
    p.add_argument('--lon',type=float,required=True)
    p.add_argument('--radius-m',type=int,default=500)
    p.add_argument('--cache-only',action='store_true')
    p.add_argument('--output',default=None)
    a=p.parse_args(argv)
    try:
        report=run(a.lat,a.lon,a.radius_m,a.cache_only)
    except ValueError as e:
        p.error(str(e))
    target=Path(a.output) if a.output else ROOT / f'nvdb_coverage_{a.lat:.4f}_{a.lon:.4f}_v073.json'
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Saved:',target)
    print('Cells OK:',report['cells_ok'],'/',report['cells_requested'], 'Eligible:',report['unique_accepted_in_area_cells'])
    print('Rejected reasons:', {k:v for k,v in report['totals'].items() if k not in ('raw_records','pages','accepted','unique_accepted')})
    return 0 if report['cells_ok']==report['cells_requested'] else 1


if __name__=='__main__':
    raise SystemExit(main())
