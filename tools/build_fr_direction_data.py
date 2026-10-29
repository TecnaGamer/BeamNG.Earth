"""Compile mapped OSM destination_sign relations into a small French direction kit.

Input: relation bodies, direct member nodes, member highway ways with geometry.
Names are always explicitly mapped destinations. Only white/black local boards;
unsupported colour systems and explicit foot/cycle-only relations are skipped.
"""
import argparse,gzip,json,math,hashlib,sys
from pathlib import Path
from collections import Counter
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
import countrybounds

CAR={'primary','secondary','tertiary','unclassified','residential','living_street',
     'primary_link','secondary_link','tertiary_link'}

def build(source,output):
    doc=json.loads(gzip.decompress(Path(source).read_bytes()));idx=countrybounds.bundled()
    if doc.get('remark'):raise ValueError(doc['remark'])
    nodes={e['id']:e for e in doc['elements'] if e['type']=='node'}
    ways={e['id']:e for e in doc['elements'] if e['type']=='way'}
    out=[];skipped=Counter()
    def coordinate(member):
        n=nodes.get(member['ref'],{})
        return [n['lon'],n['lat']] if 'lat' in n else None
    def dist(a,b):return ((a[0]-b[0])*math.cos(math.radians(b[1])))**2+(a[1]-b[1])**2
    for r in sorted((e for e in doc['elements'] if e['type']=='relation'),key=lambda e:e['id']):
        tags=r.get('tags',{});name=tags.get('destination','').strip()
        if not name:skipped['no_destination']+=1;continue
        if any(tags.get(k,default).lower() not in allowed for k,default,allowed in (
            ('colour:back','white',{'white','#ffffff'}),('colour:arrow','black',{'black','#000000'}),
            ('colour:text','black',{'black','#000000'}))):skipped['other_colour_system']+=1;continue
        members=r.get('members',[])
        travelways=[ways.get(m['ref']) for m in members if m['type']=='way' and m['role'] in ('from','to')]
        if travelways and not any(w and w.get('tags',{}).get('highway') in CAR for w in travelways):
            skipped['non_car_route']+=1;continue
        signs=[(m['ref'],coordinate(m)) for m in members if m['type']=='node' and m['role']=='sign']
        intersections=[coordinate(m) for m in members if m['type']=='node' and m['role'] in ('intersection','via')]
        intersections=[c for c in intersections if c]
        if not signs and intersections:signs=[('junction',intersections[0])]
        for ident,pos in signs:
            if not pos or idx.at(pos[1],pos[0])!='FR':continue
            origin=min(intersections,key=lambda c:dist(c,pos)) if intersections else pos
            targets=[]
            for m in members:
                if m['role']!='to':continue
                if m['type']=='node':
                    c=coordinate(m)
                    if c and dist(c,origin)>1e-12:targets.append(c)
                elif m['type']=='way':
                    w=ways.get(m['ref'],{})
                    if w.get('tags',{}).get('highway') not in CAR:continue
                    coords=[[p['lon'],p['lat']] for p in w.get('geometry',[]) if p]
                    if len(coords)<2:continue
                    j=min(range(len(coords)),key=lambda j:dist(coords[j],origin))
                    # A to-way should depart the junction from an endpoint. Interior
                    # membership is ambiguous without more routing information.
                    if j==0:targets.append(coords[1])
                    elif j==len(coords)-1:targets.append(coords[-2])
            if not targets:skipped['no_direction_geometry']+=1;continue
            target=min(targets,key=lambda c:dist(c,origin))
            out.append({'id':str(r['id'])+'/'+str(ident),'relation':r['id'],'lat':pos[1],'lon':pos[0],
                        'origin':origin,'towards':target,'labels':[s.strip() for s in name.split(';') if s.strip()],
                        'ref':tags.get('destination:ref',''),'mapped_position':ident!='junction'})
    result={'format':1,'source':'OpenStreetMap destination_sign relations via Overpass','license':'ODbL-1.0',
            'timestamp':doc.get('osm3s',{}).get('timestamp_osm_base'),'signs':out,'skipped':dict(skipped)}
    data=gzip.compress(json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode(),mtime=0)
    Path(output).write_bytes(data)
    print(json.dumps({'signs':len(out),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'skipped':dict(skipped)},indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source');p.add_argument('output');a=p.parse_args();build(a.source,a.output)
