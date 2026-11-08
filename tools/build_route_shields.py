"""Build a reproducible regional inventory from Overpass JSON (out body geom).

No queries at runtime. Keep the source snapshot outside the distributable data;
the output contains OSM-derived sign stations, licensed ODbL 1.0.
Usage: python tools/build_route_shields.py snapshot.json server/data/wyoming_route_shields.json
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import gzip
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
import routeindex


def build(data):
    ways,coords,relations=[],{},defaultdict(list)
    for element in data['elements']:
        if element['type']=='way' and element.get('tags',{}).get('highway'):
            refs=element.get('nodes',[])
            geometry=element.get('geometry',[])
            if len(refs)!=len(geometry):
                continue
            for nid,p in zip(refs,geometry):
                if p:
                    coords[nid]=(p['lon'],p['lat'])
            ways.append((element['id'],refs,element['tags']))
        elif element['type']=='relation':
            tags=element.get('tags',{})
            if tags.get('type')!='route' or tags.get('route')!='road':
                continue
            for member in element['members']:
                if member['type']=='way':
                    relations[member['ref']].append(dict(tags,role=member.get('role','')))
    nodes=[(e['id'],(e['lon'],e['lat']),e.get('tags',{})) for e in data['elements']
           if e['type']=='node' and 'lon' in e]
    objects=routeindex.unique(routeindex.mapped_records(ways,coords,relations,nodes)+
                             routeindex.index_records(ways,coords,relations))
    for obj in objects:
        obj['roadside']=obj.pop('ways')[0]
    counts=Counter(r['network'] for o in objects for r in o['roadside']['routes'])
    return objects,dict(sorted(counts.items()))


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('source',type=Path)
    ap.add_argument('output',type=Path)
    ap.add_argument('--bounds',help='lon0,lat0,lon1,lat1; keep full input geometry, clip output stations only')
    args=ap.parse_args()
    raw=gzip.decompress(args.source.read_bytes()) if args.source.suffix=='.gz' else args.source.read_bytes()
    data=json.loads(raw)
    objects,counts=build(data)
    if args.bounds:
        x0,y0,x1,y1=map(float,args.bounds.split(','))
        objects=[o for o in objects if x0<=o['lon']<x1 and y0<=o['lat']<y1]
        counts=dict(sorted(Counter(r['network'] for o in objects for r in o['roadside']['routes']).items()))
    result=dict(version=1,source='OpenStreetMap / Overpass',license='ODbL-1.0',
                source_sha256=hashlib.sha256(raw).hexdigest(),
                osm_timestamp=data.get('osm3s',{}).get('timestamp_osm_base'),
                counts=counts,objects=objects)
    if args.bounds: result['bounds']=[x0,y0,x1,y1]
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,separators=(',',':'))+'\n',encoding='utf-8')
    print(json.dumps(dict(assemblies=len(objects),panels=counts),indent=2))


if __name__=='__main__':
    main()
