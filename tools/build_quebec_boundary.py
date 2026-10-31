"""Convert Statistics Canada's province 24 cartographic boundary to our ZIP index.

Input: ArcGIS JSON, EPSG:3347, maxAllowableOffset=10 metres, OBJECTID=5.
Keeps islands and holes; no extra simplification. Build-only shapely/pyproj.
The runtime requires country CA AND the province polygon, never a bounding box.
"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile
import numpy as np
from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.strtree import STRtree

SOURCE='https://geo.statcan.gc.ca/geo_wa/rest/services/2021/Cartographic_boundary_files/MapServer/0'

def build(source,output):
    raw=Path(source).read_bytes();doc=json.loads(raw)
    f=doc['features'][0]
    assert f['attributes']['PRUID']=='24' and doc['spatialReference']['wkid']==3347
    outer=[];holes=[]
    for ring in f['geometry']['rings']:
        a=np.asarray(ring,float);area=np.sum(a[:-1,0]*a[1:,1]-a[1:,0]*a[:-1,1])
        (outer if area<0 else holes).append(a)
    polygons=[Polygon(r) for r in outer];index=STRtree(polygons);assigned=[[] for _ in outer]
    degenerate=0
    for hole in holes:
        point=Polygon(hole).representative_point()
        hits=[int(i) for i in index.query(point) if polygons[int(i)].contains(point)]
        if not hits:
            # The service's 10 m generalisation can leave metre-sized reversed
            # shoreline slivers outside the simplified exterior. Discard only
            # those, never a material island or interior hole.
            if Polygon(hole).area<=25:degenerate+=1;continue
            raise ValueError('Unassigned province hole')
        assigned[min(hits,key=lambda i:polygons[i].area)].append(hole)
    transform=Transformer.from_crs(3347,4326,always_xy=True)
    meta={'format':2,'source':SOURCE,'source_sha256':hashlib.sha256(raw).hexdigest(),
          'source_parameters':{'objectIds':'5','maxAllowableOffset':10,'returnGeometry':True,'f':'json'},
          'license':'Statistics Canada Open Licence','province':'Quebec','codes':['QC'],'parts':[],
          'degenerate_exterior_slivers_under_25m2':degenerate,
          'note':'Cartographic census province; 10 m service generalisation; shoreline and boundary uncertainty remain.'}
    def put(z,name,value):
        info=zipfile.ZipInfo(name,(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16
        z.writestr(info,json.dumps(value,separators=(',',':')).encode(),compresslevel=9)
    with zipfile.ZipFile(output,'w') as z:
        for i,(ring,inner) in enumerate(zip(outer,assigned)):
            rings=[]
            for r in [ring]+inner:
                x,y=transform.transform(r[:,0],r[:,1]);rings.append(np.c_[x,y].tolist())
            a=np.array(rings[0]);member=f'QC/{i}.json'
            meta['parts'].append({'country':'QC','member':member,'bounds':[*a.min(0),*a.max(0)]})
            put(z,member,rings)
        meta['point_count']=sum(len(r) for r in outer+holes);put(z,'index.json',meta)
    print(len(outer),'parts',len(holes),'holes','SHA256',hashlib.sha256(Path(output).read_bytes()).hexdigest())

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',type=Path);p.add_argument('output',type=Path)
    a=p.parse_args();build(a.source,a.output)
