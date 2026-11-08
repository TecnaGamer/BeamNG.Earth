"""Offline audit of the approved current OSM source; no game/server control."""
from pathlib import Path
import sys,json
import numpy as np
from PIL import Image,ImageDraw
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
import authoritativeperimeters as ap,geodesy
from shapely.geometry import LineString,mapping
out=ROOT/'probes/warren_authoritative_review';out.mkdir(exist_ok=True)
records,report=ap.sources();paths,topology=ap.chains(records)
frame=geodesy.EnuFrame(41.16,-104.86,0.)
def metric(coords):
 a=np.asarray(coords);e,n,_=frame.geodetic_to_enu(a[:,1],a[:,0],0.);return np.c_[e,n]
lines=[metric(p['coordinates']) for p in paths]
endpoints=[e['coordinates'] for e in topology['endpoints']]
pairs=[]
for i,a in enumerate(endpoints):
 for j,b in enumerate(endpoints[:i]):pairs.append(dict(endpoints=[j,i],distance_m=float(np.linalg.norm(np.diff(metric([a,b]),axis=0)))))
report.update(topology=topology,endpoint_pair_distances=pairs,source_segments=sum(len(r['coordinates'])-1 for r in records),
 approved_routes=[dict(id=p['id'],source_ids=p['source_ids'],vertices=len(p['coordinates']),length_m=float(LineString(xy).length)) for p,xy in zip(paths,lines)],
 validation='Offline source topology. Synthetic placement tests cover alignment, phase, tile ownership, openings and duplicate control. In-game verification pending.',excluded_source='relation/21918541 and way/1576457907: military land-use geometry is not the border-zone outer boundary')
(out/'source_audit.json').write_text(json.dumps(report,indent=2)+'\n')
(out/'approved_routes.geojson').write_text(json.dumps(dict(type='FeatureCollection',features=[dict(type='Feature',properties=dict(id=p['id'],source_ids=p['source_ids']),geometry=mapping(LineString(p['coordinates']))) for p in paths]),indent=2)+'\n')
allxy=np.vstack(lines);low=allxy.min(axis=0);high=allxy.max(axis=0);scale=min(1000/(high[0]-low[0]),1080/(high[1]-low[1]))
def pixel(xy):return (80+float(xy[0]-low[0])*scale,1170-float(xy[1]-low[1])*scale)
im=Image.new('RGB',(1160,1250),'white');d=ImageDraw.Draw(im)
d.text((25,20),'Warren: relation 252043 v17 | 11 outer ways | 2 open chains',fill='black')
d.text((25,42),'Blue: exact approved OSM lines. Red: unjoined source endpoints. No invented joins.',fill='black')
for xy in lines:d.line([pixel(p) for p in xy],fill='#185ba8',width=3)
for i,xy in enumerate(metric(endpoints)):
 x,y=pixel(xy);d.ellipse((x-6,y-6,x+6,y+6),fill='#d92b2b');d.text((x+8,y),str(i+1),fill='black')
im.save(out/'source_topology.png')
print(json.dumps(dict(ways=len(records),paths=len(paths),endpoints=len(endpoints),output=str(out))))

# Replay a reported west-side location against captured provider context.
import facility_perimeter_review as review,perimeters as placement
import time
context,capture=review.snapshot();audit={};events=[]
case_frame=geodesy.EnuFrame(41.17607,-104.88373,0.)
started=time.perf_counter()
rows=placement.rows(case_frame,lambda key:[context[layer] for layer in placement.LAYERS],lambda key:context['infrastructure'],(-180,-180,180,180),None,
 transport_fn=lambda key:context['roads'],surface=lambda xy:np.full(len(xy),10.),audit=audit,diagnostics=events,routing_context=dict(roads=[context['roads']]))
case=dict(location=[41.17607,-104.88373],terrain='synthetic 10m; not in-game validation',panels=len(rows),seconds=time.perf_counter()-started,
 source_ids=sorted({row[8] for row in rows}),placement_report=audit.get('placement_report'),diagnostics=events)
assert rows and all(row[8].startswith('physical:authoritative:warren/') for row in rows)
(out/'west_side_placement.json').write_text(json.dumps(case,indent=2,default=str)+'\n')
print('West-side replay:',len(rows),'panels')

sys.path.insert(0,str(ROOT/'server/tests'))
from test_fence_overhaul import PHOTOS
cases=[]
for i,(lat,lon) in enumerate(PHOTOS,1):
 audit={};f=geodesy.EnuFrame(lat,lon,0.)
 result=placement.rows(f,lambda key:[context[layer] for layer in placement.LAYERS],lambda key:context['infrastructure'],(-180,-180,180,180),None,
  transport_fn=lambda key:context['roads'],surface=lambda xy:np.full(len(xy),10.),audit=audit,routing_context=dict(roads=[context['roads']]))
 cases.append(dict(photo=i,location=[lat,lon],panels=len(result),sources=sorted({r[8] for r in result}),placement_report=audit.get('placement_report')))
(out/'photo_replays.json').write_text(json.dumps(dict(terrain='synthetic 10m; captured provider context; not in-game verification',cases=cases),indent=2,default=str)+'\n')
print('Photo replays:',len(cases))
