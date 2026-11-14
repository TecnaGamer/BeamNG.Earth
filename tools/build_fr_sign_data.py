"""Compile an OSM/Overpass JSON snapshot into compact French town-sign data.

No downloaded image/model. Input is a directory of .json.gz Overpass responses
including sign nodes, their parent highway ways (out geom), and settlement nodes.
Country polygons filter the bounding-box extraction to metropolitan FR/Corsica.
No nearest-country guess; dependencies keep their own codes.
"""
import argparse
from collections import Counter,defaultdict
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys
import unicodedata
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
import countrybounds

ROAD_TYPES={'primary','secondary','tertiary','unclassified','residential','living_street'}

def norm(s):return ''.join(c for c in unicodedata.normalize('NFD',s.upper()) if not unicodedata.combining(c)).replace('Œ','OE')

def compile_data(source,output):
    nodes={};ways={};timestamps=set()
    for path in sorted(Path(source).glob('*.json.gz')):
        doc=json.loads(gzip.decompress(path.read_bytes()))
        if doc.get('remark'):raise ValueError('Incomplete Overpass response: '+str(path))
        timestamps.add(doc.get('osm3s',{}).get('timestamp_osm_base','unknown'))
        for e in doc['elements']:
            if e['type']=='node':nodes[e['id']]=e
            elif e['type']=='way' and e.get('tags',{}).get('highway') in ROAD_TYPES:ways[e['id']]=e
    idx=countrybounds.bundled();signs={};places=[];named=defaultdict(list)
    for ident,e in sorted(nodes.items(), key=lambda pair:(round(pair[1]["lat"],1), round(pair[1]["lon"],1),pair[0])):
        t=e.get('tags',{});lat,lon=e['lat'],e['lon']
        if idx.at(lat,lon)!='FR':continue
        if t.get('place') in ('city','town','village','hamlet') and t.get('name'):
            p={'id':ident,'lat':lat,'lon':lon,'name':t['name'],'place':t['place']}
            places.append(p);named[norm(p['name'])].append(p)
        elif t.get('highway')=='city_limit' or t.get('traffic_sign')=='city_limit' or 'FR:EB' in t.get('traffic_sign',''):
            kind=t.get('city_limit','both')
            if 'EB20' in t.get('traffic_sign','') and 'EB10' not in t.get('traffic_sign',''):kind='end'
            if kind not in ('begin','end','both'):kind='both'
            signs[ident]={'id':ident,'lat':lat,'lon':lon,'name':t.get('name',''),'kind':kind,'direction':t.get('direction','')}
    # Many mapped entrances have no name (e.g. the north approach to Lembach).
    # Keep the surveyed position; infer only the label when a nearby settlement
    # is unambiguous. Do not assign the closest name anywhere in the country.
    grid=defaultdict(list)
    for p in places:
        grid[(math.floor(p['lon']*50),math.floor(p['lat']*50))].append(p)
    for ident,s in list(signs.items()):
        if s['name']:continue
        gx,gy=math.floor(s['lon']*50),math.floor(s['lat']*50)
        candidates=[]
        for x in range(gx-2,gx+3):
            for y in range(gy-1,gy+2):
                for p in grid.get((x,y),()):
                    d=111320*math.hypot((p['lon']-s['lon'])*math.cos(math.radians(s['lat'])),p['lat']-s['lat'])
                    if d<1500:candidates.append((d,p['id'],p))
        candidates.sort(key=lambda v:(v[0],v[1]))
        if not candidates or (len(candidates)>1 and candidates[1][0]<max(candidates[0][0]*1.5,candidates[0][0]+250)):
            del signs[ident];continue
        p=candidates[0][2]
        s.update(name=p['name'],name_estimated=True,centre=[p['lon'],p['lat']],place_id=p['id'])
    # Parent geometry gives a directed tangent and the road ref without guessing.
    for wid,e in sorted(ways.items()):
        ns=e.get('nodes',[]);geo=e.get('geometry',[])
        if len(geo)!=len(ns):continue
        for i,ident in enumerate(ns):
            if ident not in signs or 'way' in signs[ident]:continue
            a,b=geo[max(0,i-1)],geo[min(len(geo)-1,i+1)]
            if not a or not b:continue
            dx=(b['lon']-a['lon'])*math.cos(math.radians(signs[ident]['lat']));dy=b['lat']-a['lat']
            if dx*dx+dy*dy<1e-16:continue
            signs[ident].update(way=wid,ref=e.get('tags',{}).get('ref',''),
                                bearing=math.degrees(math.atan2(dx,dy))%360)
    missing=Counter()
    for s in signs.values():
        matches=named.get(norm(s['name']),[])
        if matches:
            p=min(matches,key=lambda p:(p['lat']-s['lat'])**2+((p['lon']-s['lon'])*math.cos(math.radians(s['lat'])))**2)
            if abs(p['lat']-s['lat'])<.15 and abs(p['lon']-s['lon'])<.25:
                s['centre']=[p['lon'],p['lat']];s['place_id']=p['id']
        if not s['direction']:missing['orientation_estimated']+=1
        if 'centre' not in s:missing['no_matching_settlement']+=1
        if 'way' not in s:missing['off_way']+=1
    needed={s.get('place_id') for s in signs.values()}
    places=[p for p in places if p['place'] in ('city','town','village') or p['id'] in needed]
    result={'format':1,'license':'ODbL-1.0','source':'OpenStreetMap contributors via Overpass API',
            'timestamps':sorted(timestamps),'selection':'named city-limit signs and settlements, FR polygon filter',
            'signs':sorted(signs.values(),key=lambda r:r['id']),'places':sorted(places,key=lambda r:r['id']),'statistics':dict(missing)}
    raw=json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
    data=gzip.compress(raw,mtime=0);Path(output).write_bytes(data)
    print(json.dumps({'signs':len(signs),'settlements':len(places),'bytes':len(data),
                      'sha256':hashlib.sha256(data).hexdigest(),'stats':dict(missing)},indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source');p.add_argument('output');a=p.parse_args()
    compile_data(a.source,a.output)
