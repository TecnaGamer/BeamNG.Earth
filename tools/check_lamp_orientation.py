"""Isolated orientation audit from Overpass JSON; no running server or game cache.

Example: python tools/check_lamp_orientation.py probes/lamps-exit310.osm.json.gz
         --output probes/lamps-exit310-orientation.json
"""
import argparse
import gzip
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
import furniture
import geodesy
import mapgeom
import worldprops


def features(layer,elements):
    ids,geometries,tags,classes,sources=[],[],[],[],[]
    for e in elements:
        t=e.get('tags',{})
        if layer=='roads':
            if e['type']!='way' or not t.get('highway') or len(e.get('geometry',[]))<2: continue
            ring=np.array([[p['lon'],p['lat']] for p in e['geometry']])
            kind=mapgeom.LINE
        else:
            if e['type']!='node' or t.get('highway')!='street_lamp': continue
            ring=np.array([[e['lon'],e['lat']]])
            kind=mapgeom.POINT
        ids.append(str(e['id']))
        geometries.append((kind,[[ring]]))
        classes.append(t['highway'])
        tags.append(json.dumps(t))
        sources.append(f"{e['type'][0]}{e['id']}@1")
    f=mapgeom.build(layer,np.array(ids),geometries)
    f.attrs.update(tags=np.array(tags),**{'class':np.array(classes),'src_record':np.array(sources)})
    return f


def audit(data):
    elements=data['elements']
    lamps=[e for e in elements if e['type']=='node' and e.get('tags',{}).get('highway')=='street_lamp']
    if not lamps: raise ValueError('No mapped street lamps in input')
    frame=geodesy.EnuFrame(float(np.mean([e['lat'] for e in lamps])),float(np.mean([e['lon'] for e in lamps])))
    roads=features('roads',elements)
    infra=features('infrastructure',elements)
    lines=worldprops._lamp_roads(frame,[(roads,None)])
    placed=[]
    worldprops._street_lamps(frame,infra,None,(-1e6,-1e6,1e6,1e6),placed,lambda:lines)
    rows=[]
    for lamp in placed:
        if lamp['group']=='streetlight_high_mast': continue
        point=np.array([lamp['x'],lamp['y']])
        delta=worldprops._road_delta(point,[r[1] for r in lines],worldprops.LAMP_ROAD_M)
        if delta is None: continue
        heads=furniture.MAPPED_LAMP_HEADS.get(lamp['group']) or (furniture.LAMP_HEADS[lamp['group']],)
        inward=[]
        for h in heads:
            offset=np.array(worldprops._local_offset(lamp['yaw'],h[0],h[1]))
            inward.append(float(offset@delta/np.linalg.norm(delta)))
        rows.append(dict(id=lamp['lamp_id'],group=lamp['group'],yaw=round(lamp['yaw'],3),
                         road_distance_m=round(float(np.linalg.norm(delta)),3),
                         arm_inward_m=round(max(inward),3)))
    return dict(osm_timestamp=data.get('osm3s',{}).get('timestamp_osm_base'),
                attribution='© OpenStreetMap contributors, ODbL 1.0',
                mapped_nodes=len(lamps),oriented_nodes=len(rows),
                arms_toward_road=sum(r['arm_inward_m']>0 for r in rows),lamps=rows,
                note='Offline OSM geometry check, not an in-game or pavement-clearance verification')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('input',type=Path)
    p.add_argument('--output',type=Path)
    args=p.parse_args()
    opener=gzip.open if args.input.suffix=='.gz' else open
    with opener(args.input,'rt',encoding='utf-8') as f: result=audit(json.load(f))
    if args.output: args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='lamps'},ensure_ascii=True))


if __name__=='__main__': main()
