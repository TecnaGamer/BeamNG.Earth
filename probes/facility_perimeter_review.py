"""Offline facility/transport evidence review; synthetic terrain, no game controls."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time
import numpy as np
import shapely
from shapely.geometry import Polygon,LineString,mapping,box
from PIL import Image,ImageDraw
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
import facilityperimeters as fp,perimeters as p,mapgeom,geodesy


def snapshot():
    compact=ROOT/'probes/perimeter_fixture_context'
    if compact.exists():
        metadata=json.loads((compact/'capture.json').read_text())
        data={}
        for layer in metadata['layers']:
            with np.load(compact/(layer+'.npz')) as arrays:data[layer]=mapgeom.Features.from_arrays(layer,arrays,'')
        return data,metadata
    directory=ROOT/'probes/warren_facility_context'
    metadata=json.loads((directory/'capture.json').read_text())
    data={}
    for layer in metadata['layers']:
        features=[]
        for path in sorted(directory.glob('*_'+layer+'.npz')):
            # Avoid matching perimeter_infrastructure when reading infrastructure.
            if path.name.split('_',3)[-1]!=layer+'.npz':continue
            with np.load(path) as arrays:features.append(mapgeom.Features.from_arrays(layer,arrays,''))
        data[layer]=fp.combine(features,layer)
    return data,metadata


def main():
    data,capture=snapshot();output=ROOT/'probes/facility_perimeter_review';output.mkdir(exist_ok=True)
    live=json.loads((ROOT/'probes/warren_facility_osm.json').read_text(encoding='utf-8'))
    parents={}
    for entry in live['elements']:
        if entry['type']!='relation':continue
        for member in entry['members']:parents.setdefault((member['type'],member['ref']),[]).append(entry['id'])
    report=dict(generator=p.VERSION,model=p.MODEL,source_release=capture['release'],osm_timestamp=live['osm3s']['timestamp_osm_base'],
        terrain='synthetic elevation 10 m; in-game verification pending',inventory=[],cases=[])
    inventory_frame=geodesy.EnuFrame(41.16,-104.86,0.)
    seen=set()
    for layer in p.LAYERS:
        feats=data[layer]
        for i in range(len(feats)):
            fid=p.osm_id(feats,i);tags=p.tags_of(feats,i)
            if fid in seen or not fid or not p.classify(tags,str(feats.attrs['class'][i])):continue
            seen.add(fid);polygons=[]
            for part in feats.parts(i):
                if len(part[0])<4:continue
                e,n=mapgeom.enu_xy(feats,inventory_frame,part[0]);polygons.append(Polygon(np.c_[e,n]).buffer(0))
            kind,identifier=fid.split('/')
            report['inventory'].append(dict(osm_id=fid,tags=tags,name=str(feats.attrs['name'][i]),
                geometry_type='MultiPolygon' if len(polygons)>1 else 'Polygon',area_m2=shapely.union_all(polygons).area,
                parent_relations=parents.get((kind,int(identifier)),[]),administrative=fp.administrative(dict(tags=tags))))
    cases=[('military_road',41.13458,-104.88113,(-600,-400,600,600)),
        ('carey_avenue',41.14897,-104.82727,(-450,-450,450,450)),
        ('carey_surroundings',41.14897,-104.82727,(-1200,-1200,1200,1200)),
        ('southeast',41.1395,-104.847,(-900,-1100,1100,1000)),
        ('whole_installation',41.16,-104.86,(-2500,-3300,2200,4800))]
    for name,lat,lon,bounds in cases:
        frame=geodesy.EnuFrame(lat,lon,0.);audit={};events=[]
        started=time.perf_counter()
        rows=p.rows(frame,lambda key:[data[layer] for layer in p.LAYERS],lambda key:data['infrastructure'],bounds,None,
            transport_fn=lambda key:data['roads'],surface=lambda xy:np.full(len(xy),10.),audit=audit,diagnostics=events,routing_context=dict(roads=[data['roads']]))
        elapsed=time.perf_counter()-started
        audit.update(case=name,bounds=bounds,seconds=elapsed,source_counts=dict(Counter(r[8] for r in rows)),crossing_events=events)
        report['cases'].append(audit)
        x0,y0,x1,y1=bounds;width=1400;height=max(800,round(1400*(y1-y0)/(x1-x0)))
        image=Image.new('RGB',(width,height+75),'#101821');draw=ImageDraw.Draw(image)
        scale=min((width-60)/(x1-x0),(height-40)/(y1-y0))
        def coordinates(xy):return [(round(30+(x-x0)*scale),round(75+height-30-(y-y0)*scale)) for x,y in xy]
        for layer in p.LAYERS:
            feats=data[layer]
            for i in range(len(feats)):
                for part in feats.parts(i):
                    e,n=mapgeom.enu_xy(feats,frame,part[0]);draw.line(coordinates(np.c_[e,n]),fill='#717b8a',width=2)
        for row in rows:draw.line(coordinates([row[1:3],row[4:6]]),fill='#69b9ff' if row[8].startswith('physical:') else '#50e29b',width=3)
        draw.text((25,15),f'{name}: {lat}, {lon} | {len(rows)} sections | {elapsed:.3f}s offline',fill='white')
        draw.text((25,35),'Grey: raw facility polygons | Blue: mapped physical fence | Green: selected inference',fill='white')
        draw.text((25,55),'Synthetic elevation; road-source openings only here; final generated transport tested separately.',fill='white')
        image.save(output/(name+'.png'))
        print(name,len(rows),'sections',round(elapsed,3),'seconds',flush=True)
    report['snapshot_hashes']={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((ROOT/'probes/warren_facility_context').glob('*.npz'))}
    (output/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':main()
