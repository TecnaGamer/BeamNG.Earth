"""US state welcome boards and recreation boards (wyo-backroads' PRs #1117 / #1118), built in the sign tiles.

- State welcome boards (BNE_WELCOME_SIGNS, on): a board on the receiving side where an Interstate, US or numbered
  state route crosses a state line (border_welcomes), from the roads, their route memberships and the state outlines
  the sign tiles have anyway. Its face: the state's sign photographed (data/welcome/faces, DDS, licences in
  data/welcome/ATTRIBUTION.md) or, where no photo with clear reuse terms was found, a lettered board (generic_welcome).
  Mapped welcome boards (OSM board_type=welcome_sign nodes, mapped_welcomes) need their tags from the OSM API:
  BNE_OSM_API_SIGNS=1 only (off: no requests to api.openstreetmap.org from players' machines).
- Brown park guide boards beside a motorway (park_exit_boards): from the exits' posted destinations the weekly sign
  index already has; the OSM API fallback for ramps missing from it (osmexitdest) is BNE_OSM_API_SIGNS=1 only.
- Recreation / park / forest entrance boards (BNE_RECREATION_SIGNS, on): where an access road enters a named park,
  reserve, campground or national forest of the cells' land use (whole outlines from the 3 x 3 cells' clipped pieces).
  The OSM US Layercake protected areas (osmparks) add OSM-only forests with BNE_OSM_PARKS=1 (off: a range read of a
  remote file per cell). Agency badges are simple shapes drawn here, not the NPS / Forest Service insignia.

No location-specific data: every board comes from these rules.
"""
from collections import OrderedDict, defaultdict
from concurrent.futures import CancelledError
from functools import lru_cache
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import shutil
import threading
import numpy as np
import dnp
import exitsigns
import forestboards
import recreationrules
import imagery
import mapcells
import osmwelcomes
import osmexitdest
import osmparks
import railtags
import roads
import routeindex
import revisit
import signplacement
import signroads
import signstates
import throughsigns

log=logging.getLogger('earthd.travelboards')

DATA=Path(__file__).with_name('data')/'welcome'
WELCOME_ON = os.environ.get('BNE_WELCOME_SIGNS', '1') != '0'
RECREATION_ON = os.environ.get('BNE_RECREATION_SIGNS', '1') != '0'
OSM_API_ON = os.environ.get('BNE_OSM_API_SIGNS', '0') == '1'     # (api.openstreetmap.org: mapped welcomes, exit tags)
OSM_PARKS_ON = os.environ.get('BNE_OSM_PARKS', '0') == '1'       # (data.openstreetmap.us parks.parquet per cell)
GENERIC_W = (4.2, 7.0)      # m: a lettered welcome board's width range (generic_welcome)
GENERIC_H = 2.6             # m: its height
ACCESS={'tertiary','residential','unclassified','service'}
BORDER_ROADS={'motorway','trunk','primary','secondary','tertiary','unclassified','residential','living_street','service','track'}
CATEGORIES={'park':'RECREATION AREA','national_park':'NATIONAL PARK','nature_reserve':'NATURE RESERVE','camp_site':'CAMPING','campground':'CAMPING','picnic_site':'PICNIC AREA'}

@lru_cache(maxsize=1)
def registry():return json.loads((DATA/'registry.json').read_text(encoding='utf-8'))

def current(state):
    """The state's welcome board: its registry entry with the photographed face ('board': 'photo', 'face': the DDS) or
    a lettered board ('generic', generic_welcome's size), or None (no land border: HI; DC)."""
    value=registry()['states'].get(state)
    if value is None or value.get('board') not in ('photo','generic'):
        return None
    if value['board']=='photo':
        return value if (DATA/value['face']).is_file() else None
    return generic_welcome(state)

@lru_cache(maxsize=64)
def generic_welcome(state):
    """A lettered welcome board for a state without a face we may ship: WELCOME TO over the state's name, white on
    blue, on two metal posts."""
    name=registry()['states'][state]['name']
    w=float(np.clip(exitsigns.guide_width(name.upper(),.62)+1.4,*GENERIC_W))
    return dict(name=name,board='generic',width_m=w,height_m=GENERIC_H,support='metal')

@lru_cache(maxsize=1)
def version():
    """The boards' data and rules (a sign tile's key part: highwaysigns.Source.version)."""
    h=hashlib.sha256((DATA/'registry.json').read_bytes())
    h.update(repr((WELCOME_ON,RECREATION_ON,OSM_API_ON,OSM_PARKS_ON)).encode())
    return 'travelboards-10-'+h.hexdigest()[:16]

def at(path,distance):
    path=np.asarray(path,float);scale=np.array([111320.*dnp.cos(np.radians(path[:,1].mean())),111320.])
    arc=np.r_[0.,np.cumsum(dnp.norm(np.diff(path,axis=0)*scale,axis=1))]
    distance=float(np.clip(distance,0,arc[-1]));i=min(len(path)-2,max(0,int(np.searchsorted(arc,distance,side='right')-1)))
    t=(distance-arc[i])/max(arc[i+1]-arc[i],1e-9)
    return path[i]*(1-t)+path[i+1]*t,i,arc,scale

def transitions(path,classify):
    """[(distance along path, label before, label after)] where classify's label (of points (n, 2) at once) changes:
    found between 50 m samples (and the vertices), then located to ~3 mm in two rounds of 129 samples."""
    arc=at(path,0)[2];samples=np.unique(np.r_[arc,np.arange(0,arc[-1],50.),arc[-1]])
    # (whole source ways can be many kilometres long: their cumulative distances once, not per sample)
    sample=lambda distances:np.column_stack([np.interp(distances,arc,path[:,j]) for j in (0,1)])
    labels=list(classify(sample(samples)))
    result=[]
    for i in range(len(samples)-1):
        before,after=labels[i:i+2]
        if before==after:continue
        lo,hi=samples[i:i+2]
        for _ in range(2):
            d=np.linspace(lo,hi,129)
            got=list(classify(sample(d)))
            k=next((j for j in range(1,129) if got[j]!=before),128)
            lo,hi=d[k-1],d[k]
        result.append(((lo+hi)/2,before,after))
    return result

def within(point,parts):
    for part in parts:
        inside=False
        for ring in part:
            a=np.asarray(ring);b=np.roll(a,-1,axis=0);dy=b[:,1]-a[:,1]
            hit=(a[:,1]>point[1])!=(b[:,1]>point[1])
            x=a[:,0]+(point[1]-a[:,1])*(b[:,0]-a[:,0])/np.where(abs(dy)>1e-14,dy,1.)
            inside ^= bool(np.count_nonzero(hit & (point[0]<x))%2)
        if inside:return True
    return False

class AreaIndex:
    """Whole rings for membership; real boundary edges for crossing rejection."""
    def __init__(self,parts):
        self.parts=[];low=[];high=[]
        for part in parts:
            rings=[]
            for ring in part:
                ring=np.ascontiguousarray(ring,dtype=np.float64)
                if len(ring)<3:continue
                rings.append((ring,ring.min(0),ring.max(0)))
                following=np.roll(ring,-1,axis=0)
                low.append(np.minimum(ring,following));high.append(np.maximum(ring,following))
            if rings:self.parts.append(rings)
        self.low=np.vstack(low) if low else np.empty((0,2))
        self.high=np.vstack(high) if high else np.empty((0,2))

    def crosses_box(self,lo,hi):
        return bool(np.any(np.all(self.low<=hi,axis=1)&np.all(self.high>=lo,axis=1)))

    def contains(self,point):
        return bool(self.contains_many(np.asarray(point,np.float64)[None])[0])

    def contains_many(self,points):
        """(n,) bool: points (n, 2) inside any part (even-odd over its rings: earthfast.points_inside per ring, on
        the points within the ring's box)."""
        import fast
        pts=np.ascontiguousarray(points,dtype=np.float64).reshape(-1,2)
        out=np.zeros(len(pts),bool)
        for rings in self.parts:
            # (a polygon's holes cannot hold a point outside its outer ring's box)
            sel=~out&np.all(pts>=rings[0][1],axis=1)&np.all(pts<=rings[0][2],axis=1)
            if not sel.any():continue
            inside=np.zeros(len(pts),bool)
            for ring,lo,hi in rings:
                m=sel&np.all(pts>=lo,axis=1)&np.all(pts<=hi,axis=1)
                if m.any():inside[m]^=np.asarray(fast._ef.points_inside(ring,np.ascontiguousarray(pts[m])),bool)
            out|=inside
        return out

@lru_cache(maxsize=1)
def _state_edges():
    """The state outlines' edges as boxes: (polygon lower (p, 2), upper (p, 2), [(edge lows (m, 2), highs (m, 2))])."""
    polygons=signstates._polygons()
    edges=[]
    for _,_,_,rings in polygons:
        a=np.vstack([r[:-1] for r in rings]);b=np.vstack([r[1:] for r in rings])
        edges.append((np.minimum(a,b),np.maximum(a,b)))
    return polygons.lower,polygons.upper,edges

def boundary_near(path):
    """Does a state outline's edge come within the path's box?"""
    lo=path.min(0);hi=path.max(0)
    lower,upper,edges=_state_edges()
    for n in np.flatnonzero(~((lower>hi).any(1)|(upper<lo).any(1))).tolist():
        el,eh=edges[n]
        if np.any(np.all(el<=hi,axis=1)&np.all(eh>=lo,axis=1)):return True
    return False

def record(kind,row,path,distance,key,**extra):
    point,i,arc,scale=at(path,distance);axis=(path[i+1]-path[i])*scale
    if dnp.norm(axis)<1e-8:return None
    axis/=dnp.norm(axis);a=max(0,distance-60);b=min(arc[-1],distance+60)
    approach=np.vstack([at(path,a)[0],path[(arc>a)&(arc<b)],at(path,b)[0]])
    return dict(id=routeindex.uid(('travelboard-v1',kind,key)),lon=float(point[0]),lat=float(point[1]),ways=[],tags={},
                travelboard=dict(kind=kind,feature_id=row['fid'],main_id=row['wid'],approach=approach.tolist(),axis=axis.tolist(),**extra))

def candidates(rows,areas=(),welcomes=True):
    indexed=[(area,AreaIndex(area['parts'])) for area in areas]
    paths=[]
    for row in rows:
        sp=row['spec']
        for direction in ((sp.dir or 1,) if sp.oneway else (1,-1)):
            paths.append((row,row['path'] if direction>0 else row['path'][::-1]))
    starts=defaultdict(list)
    for b,pb in paths:starts[tuple(np.round(pb[0],7))].append((b,pb))
    # Source segments frequently end precisely on a state line.
    joined=list(paths) if welcomes else []
    for a,pa in paths if welcomes else []:
        va=pa[-1]-pa[-2];va/=max(dnp.norm(va),1e-9)
        for b,pb in starts.get(tuple(np.round(pa[-1],7)),[]):
            if a['fid']==b['fid'] or a['spec'].cls!=b['spec'].cls or dnp.norm((pa[-1]-pb[0])*111320)>1.:continue
            vb=pb[1]-pb[0];vb/=max(dnp.norm(vb),1e-9)
            if dnp.matmul(va, vb)>.9:joined.append((b,np.vstack([pa,pb[1:]])))
    result=[];classify=lambda p:signstates.states(p[:,1],p[:,0])
    for row,path in joined if welcomes else []:
        if row['spec'].cls not in ('motorway','trunk','primary','secondary') or not boundary_near(path):continue
        arc=at(path,0)[2]
        for distance,before,state in transitions(path,classify):
            if not before or not state or state==before or not current(str(state)):continue
            if distance<80 or distance+170>arc[-1]:continue
            if list(classify(np.array([at(path,distance-80)[0],at(path,distance+170)[0]])))!=[before,state]:continue
            crossing=at(path,distance)[0]
            obj=record('welcome',row,path,distance+90,(str(before),str(state),tuple(np.round(crossing,6))),state=str(state))
            if obj:result.append(obj)
    entries=list(paths) if indexed else []
    for a,pa in paths if indexed else []:
        if a['spec'].cls=='motorway':continue
        for b,pb in starts.get(tuple(np.round(pa[-1],7)),[]):
            if a['fid']==b['fid'] or a['spec'].cls!=b['spec'].cls:continue
            va=pa[-1]-pa[-2];vb=pb[1]-pb[0]
            if dnp.matmul(va, vb)/max(dnp.norm(va)*dnp.norm(vb),1e-9)>.9:
                entries.append((a,np.vstack([pa,pb[1:]])))
    crossings={}            # (a two-way road's other direction: its transitions reversed, not searched again)

    def area_transitions(area,index,path,length):
        a,b=tuple(path[0]),tuple(path[-1])
        got=crossings.get((area['id'],b,a,len(path)))
        if got is not None:
            return [(length-d,after,before) for d,before,after in reversed(got)]
        got=crossings[(area['id'],a,b,len(path))]=transitions(path,index.contains_many)
        return got

    alo=np.array([a['lo'] for a,_ in indexed],np.float64).reshape(-1,2)
    ahi=np.array([a['hi'] for a,_ in indexed],np.float64).reshape(-1,2)
    for row,path in entries:
        if row['spec'].cls=='motorway':continue
        lo,hi=path.min(0),path.max(0)
        near=np.flatnonzero(~((hi<alo).any(1)|(lo>ahi).any(1)))     # (the areas whose boxes meet the road's)
        if not len(near):continue
        arc=at(path,0)[2]
        for area,index in (indexed[n] for n in near.tolist()):
            if row['spec'].cls not in ACCESS and not area.get('entrance'):continue
            # Roads wholly inside a huge forest cannot mark a new entrance.
            # This tests actual edges, never clipped tile or polygon bounds.
            if not index.crosses_box(lo,hi):continue
            for distance,before,after in area_transitions(area,index,path,float(arc[-1])):
                if before or not after or distance<100 or distance+30>arc[-1]:continue
                if not index.contains(at(path,distance+30)[0]):continue
                style=area.get('entrance')
                station=distance-65
                if station>at(row['path'],0)[2][-1] and len(path)>len(row['path']):continue
                fields=style if style else dict(name=area['name'],category=area['category'])
                obj=record('forest_entrance' if style else 'recreation',row,path,station,
                           (area['id'],tuple(np.round(at(path,distance)[0],6)),tuple(np.round(path[0],6))),**fields)
                if obj:result.append(obj)
    # Suppression is a duplicate rule, never an arbitrary repeat interval.
    found=[]
    for obj in sorted({o['id']:o for o in result}.values(),key=lambda o:o['id']):
        e=obj['travelboard'];scale=np.array([111320.*dnp.cos(np.radians(obj['lat'])),111320.])
        if any(old['travelboard'].get('state')==e.get('state') and old['travelboard'].get('name')==e.get('name') and
               old['travelboard']['kind']==e['kind'] and dnp.dot(old['travelboard']['axis'],e['axis'])>.8 and
               dnp.norm((np.array([old['lon'],old['lat']])-np.array([obj['lon'],obj['lat']]))*scale)<150 for old in found):continue
        found.append(obj)
    return found

def welcome_state(tags):
    """Recognize all state names without treating towns/counties as state welcomes."""
    raw=str(tags.get('inscription','') or tags.get('name',''))
    parts=[re.sub(r'[^a-z]+',' ',p.lower()).strip() for p in re.split(r'[|;\n]',raw)]
    prefixes=[' '.join(parts[:i+1]).strip() for i in range(len(parts))]
    matched=[]
    for state,info in registry()['states'].items():
        name=re.escape(info['name'].lower())
        patterns=(r'welcome(?: to)?(?: the (?:free )?state of)?(?: colorful| sweet home| legendary)? '+name,
                  r'(?:the people of )?'+name+r' welcomes? you',r'welcome '+name,name)
        patterns+=tuple(re.escape(re.sub(r'[^a-z]+',' ',alias.lower()).strip()) for alias in info.get('inscription_aliases',()))
        if any(re.fullmatch(pattern,text) for text in prefixes for pattern in patterns):matched.append(state)
    return matched[0] if len(matched)==1 else None

def welcome_highway(row,point=None):
    """Large-board eligibility from Interstate, US or numbered state membership.

    County and farm/ranch routes do not receive welcome boards unless concurrent
    with a qualifying highway. Road class alone never establishes membership.
    """
    if row['spec'].link:return False
    fraction=None
    if point is not None:
        path=np.asarray(row.get('route_path',row['path']),float)
        xy=(path-point)*[111320.*dnp.cos(np.radians(point[1])),111320.]
        delta=np.diff(xy,axis=0);length=dnp.norm(delta,axis=1)
        t=np.clip(-dnp.vecdot(xy[:-1],delta)/np.maximum(length * length,1e-12),0,1)
        k=int(dnp.norm(xy[:-1]+t[:,None]*delta,axis=1).argmin())
        fraction=float((length[:k].sum()+t[k]*length[k])/max(length.sum(),1e-12))
    for network,ref,start,end in row.get('routes',()):
        bits=str(network).upper().split(':');ref=str(ref).strip()
        if not ref or re.match(r'^(?:FM|RM)\s*[- ]?\d',ref,re.I):continue
        if not re.fullmatch(r'(?:[A-Z]{1,4}[ -]?)?\d{1,4}[A-Z]?',ref,re.I):continue
        allowed=bits in (['US','I'],['US','US'])
        if len(bits)>=2 and bits[0]=='US' and bits[1] in registry()['states'] and bits[1]!='DC':
            allowed=len(bits)==2 or (len(bits)==3 and bits[2] in
                {'LOOP','SPUR','BUSINESS','TRUCK','ALTERNATE','BYPASS','TOLL','PRIMARY','SECONDARY'})
        if allowed and (fraction is None or float(start)-1e-6<=fraction<=float(end)+1e-6):return True
    return False

def welcome_info(extra):
    return current(extra['state'])

def border_road(row):
    return (not row['spec'].link and row['spec'].cls in BORDER_ROADS and
            row.get('access') not in ('private','no') and
            row.get('subclass') not in ('driveway','parking_aisle'))

def mapped_welcomes(nodes, rows):
    """Exact mapped positions; inscription selects the state even before its border."""
    result=[]
    for node in nodes:
        tags=node.get('tags',{})
        if tags.get('board_type')!='welcome_sign':continue
        state=welcome_state(tags)
        if not state or state not in registry()['states']:continue
        point=np.array([node['lon'],node['lat']],float)
        if not np.isfinite(point).all():continue
        scale=np.array([111320.*dnp.cos(np.radians(point[1])),111320.]);choices=[]
        for row in rows:
            sp=row['spec']
            if not border_road(row):continue
            path=np.asarray(row['path']);xy=(path-point)*scale;d=np.diff(xy,axis=0)
            lengths=dnp.norm(d,axis=1);t=np.clip(-dnp.vecdot(xy[:-1],d)/np.maximum(lengths * lengths,1e-9),0,1)
            near=xy[:-1]+t[:,None]*d;dist=dnp.norm(near,axis=1);k=int(dist.argmin())
            if dist[k]>100 or lengths[k]<1e-6:continue
            tangent=d[k]/lengths[k]
            for direction in ((sp.dir or 1,) if sp.oneway else (1,-1)):
                axis=tangent*direction;right=np.array([axis[1],-axis[0]])
                # Off-way boards normally stand to the approaching driver's right.
                if dnp.matmul(-near[k], right) < sp.width/2:continue
                station=float(lengths[:k].sum()+t[k]*lengths[k]);distance=station if direction>0 else float(lengths.sum()-station)
                obj=record('welcome',row,path if direction>0 else path[::-1],distance,
                           ('osm-node',int(node['id'])),state=state,mapped=True,osm_node_id=int(node['id']))
                if obj:
                    obj.update(lon=float(point[0]),lat=float(point[1]),tags=tags)
                    choices.append((float(dist[k]),row['fid'],obj,row))
        if choices:
            # A node closer to a track must not become a large board on a
            # second-nearest freeway; the local-road board is simply omitted.
            _,_,obj,row=min(choices,key=lambda x:x[:2])
            if current(state) and welcome_highway(row,point):result.append(obj)
    return result

def border_welcomes(rows):
    """One receiving-state board per directed, connected highway crossing.

    Whole road geometry detects borders on bridges; only ground sections can
    receive the board. No OSM information node is required.
    """
    edges=[];starts=defaultdict(list);seen=set();junctions=[]
    for row in rows:
        sp=row['spec']
        line=np.asarray(row['path'])
        if not sp.link and len(line)>1:
            junctions.append((row['fid'],line,line.min(0),line.max(0)))
        if not border_road(row):continue
        for direction in ((sp.dir or 1,) if sp.oneway else (1,-1)):
            path=np.asarray(row['path'] if direction>0 else row['path'][::-1])
            if len(path)<2:continue
            signature=(row['fid'],tuple(np.round(path[0],7)),tuple(np.round(path[-1],7)))
            if signature in seen:continue
            seen.add(signature)
            edge=(row,path);edges.append(edge);starts[tuple(np.round(path[0],6))].append(edge)
    found=[]
    for row,initial in edges:
        if not boundary_near(initial):continue
        path=initial.copy();owners=[row]*(len(path)-1);used={row['fid']};added=0.
        for _ in range(32):
            if added>=1800:break
            tangent=path[-1]-path[-2];tangent/=max(dnp.norm(tangent),1e-12)
            options=[]
            for other,line in starts.get(tuple(np.round(path[-1],6)),()):
                if other['fid'] in used:continue
                vector=line[1]-line[0];score=float(dnp.matmul(tangent, vector)/max(dnp.norm(vector),1e-12))
                if score>.7:options.append((score,other['fid'],other,line))
            if not options:break
            options.sort(key=lambda x:(-x[0],x[1]))
            if len(options)>1 and options[0][0]-options[1][0]<.05:break
            _,_,other,line=options[0];used.add(other['fid']);added+=at(line,0)[2][-1]
            path=np.vstack([path,line[1:]]);owners.extend([other]*(len(line)-1))
        arc=at(path,0)[2]
        classify=lambda p:signstates.states(p[:,1],p[:,0])
        for distance,before,state in transitions(path,classify):
            if not before or not state or before==state or str(state) not in registry()['states'] or distance<60:continue
            crossing=at(path,distance)[0]
            # Move beyond a river bridge/tunnel rather than putting a board on
            # its deck or dropping the crossing because its ground parts stop.
            for offset in np.arange(90.,1501.,50.):
                station=distance+offset
                if station+30>arc[-1]:break
                point,k,_,scale=at(path,station);owner=owners[k]
                crossing_owner=owners[min(len(owners)-1,at(path,distance)[1])]
                large=bool(current(str(state)) and welcome_highway(crossing_owner,crossing) and welcome_highway(owner,point))
                if not large:break
                if list(classify(np.array([point,at(path,station+30)[0]])))!=[state,state]:continue
                ground=owner.get('ground')
                if ground is not None:
                    safe=False
                    for line in ground:
                        xy=(np.asarray(line)-point)*scale;delta=np.diff(xy,axis=0)
                        t=np.clip(-dnp.vecdot(xy[:-1],delta)/np.maximum((delta*delta).sum(1),1e-12),0,1)
                        if np.any(dnp.norm(xy[:-1]+t[:,None]*delta,axis=1)<.5):safe=True;break
                    if not safe:continue
                # The first reassurance station may coincide with a driveway
                # or crossroads. Try the next station before giving up on the
                # entire crossing (placement rejects these intersections too).
                tangent=(path[k+1]-path[k])*scale
                tangent/=max(dnp.norm(tangent),1e-12)
                near_junction=False
                margin=30./scale
                for fid,line,lower,upper in junctions:
                    if fid==owner['fid'] or np.any(point<lower-margin) or np.any(point>upper+margin):continue
                    xy=(line-point)*scale;delta=np.diff(xy,axis=0);length2=dnp.vecdot(delta,delta)
                    t=np.clip(-dnp.vecdot(xy[:-1],delta)/np.maximum(length2,1e-12),0,1)
                    near=dnp.norm(xy[:-1]+t[:,None]*delta,axis=1)
                    if np.any((near<30.) & (np.abs(dnp.matmul(delta, tangent))<.85*np.sqrt(length2))):
                        near_junction=True;break
                if near_junction:continue
                obj=record('welcome',owner,path,station,
                           ('border-fallback',str(before),str(state),tuple(np.round(crossing,6))),
                           state=str(state),inferred=True,border=[float(v) for v in crossing],
                           corridor_ids=sorted(used),source='road crossing of state boundary')
                if obj:found.append(obj)
                break
    result=[]
    for obj in sorted(found,key=lambda o:o['id']):
        e=obj['travelboard'];scale=np.array([111320.*dnp.cos(np.radians(obj['lat'])),111320.])
        if any(old['travelboard']['state']==e['state'] and dnp.dot(old['travelboard']['axis'],e['axis'])>.8 and
               dnp.norm((np.array([old['lon'],old['lat']])-np.array([obj['lon'],obj['lat']]))*scale)<180 for old in result):continue
        result.append(obj)
    return result

def welcome_preference(inferred,mapped):
    """Attach the matching mapped placement; rendering checks that it is safe."""
    for obj in inferred:
        e=obj['travelboard'];scale=np.array([111320.*dnp.cos(np.radians(obj['lat'])),111320.]);axis=np.array(e['axis'])
        choices=[]
        for candidate in mapped:
            m=candidate['travelboard']
            if m['state']!=e['state'] or dnp.dot(m['axis'],axis)<.6:continue
            delta=(np.array([candidate['lon'],candidate['lat']])-np.array([obj['lon'],obj['lat']]))*scale
            distance=dnp.norm(delta)
            if distance>1500:continue
            if m['feature_id'] not in e.get('corridor_ids',()) and abs(dnp.matmul(delta, np.array([axis[1],-axis[0]])))>35:continue
            choices.append((distance,candidate['id'],candidate))
        if choices:e['mapped_alternative']=min(choices,key=lambda c:c[:2])[2]
    return inferred+mapped

def park_exit_boards(objects):
    """Separate brown supplemental panels for explicitly posted park destinations."""
    result=[]
    for source in objects:
        e=source.get('exit')
        if not e:continue
        label=e.get('label') or {};names=[n for n in label.get('names',()) if recreationrules.destination(n)]
        ref=str(label.get('ref','')).strip();side=e.get('side')
        path=np.asarray(e.get('approach',()),float)
        if not names or not ref or side not in (-1,1) or path.ndim!=2 or len(path)<2:continue
        arc=at(path,0)[2]
        if arc[-1]<250:continue
        station=max(80.,arc[-1]-650.)
        point,index,_,scale=at(path,station);axis=(path[index+1]-path[index])*scale
        if dnp.norm(axis)<1e-8:continue
        state=str(signstates.states(point[1],point[0]).item())
        if not state:continue
        lines=[]
        for name in names[:2]:
            words=name.split();middle=max(1,len(words)//2)
            lines.extend([name] if exitsigns.guide_width(name,.42)<4.5 else [' '.join(words[:middle]),' '.join(words[middle:])])
        record_id=routeindex.uid(('park-exit-board-v1',source['id'],tuple(names)))
        obj=dict(id=record_id,lat=float(point[1]),lon=float(point[0]),tags={},ways=[],travelboard=dict(
            kind='recreation_exit',feature_id='',main_id=e.get('main_id'),main_ids=e.get('main_ids',()),
            approach=path.tolist(),axis=(axis/dnp.norm(axis)).tolist(),name='; '.join(names),lines=lines,
            exit=ref,instruction='NEXT RIGHT' if side==1 else 'NEXT LEFT',side=side,
            width_m=max(3.66,min(6.,max(exitsigns.guide_width(line,.42) for line in lines)+.6)),
            height_m=max(3.4,1.95+.62*len(lines)),source='OSM posted exit destination',allow_mainline_match=True))
        if not any(o.get('travelboard',{}).get('kind')=='recreation_exit' and
                   str(o['travelboard'].get('exit'))==ref and
                   obj['travelboard']['name'].casefold()==o['travelboard'].get('name','').casefold() and
                   dnp.dot(o['travelboard']['axis'],obj['travelboard']['axis'])>.8 and
                   dnp.norm((np.array([o['lon'],o['lat']])-point)*scale)<1200 for o in objects):result.append(obj)
    return list({o['id']:o for o in result}.values())

RECREATION_CLASSES = ('park','national_park','nature_reserve','campground','camp_site','protected_area')
FOREST_NAME = re.compile(r'national[_ ](?:forest|grassland)', re.I)


def recreation_area(f, i):
    """(name, tags) of land-use feature i when it is a recreation area a board may name (the land-use classes and
    OSM tags wyo-backroads' recreation_areas layer selected), else None."""
    cls = str(f.attrs['class'][i]) if 'class' in f.attrs else ''
    try:
        tags = json.loads(str(f.attrs['tags'][i]) or '{}') if 'tags' in f.attrs else {}
    except (ValueError, TypeError):
        return None
    if not isinstance(tags, dict):
        tags = {}
    name = str(f.attrs['name'][i]).strip() if 'name' in f.attrs else ''
    if not name or name == 'None':
        return None
    if not (cls in RECREATION_CLASSES or tags.get('leisure') in ('park', 'nature_reserve') or
            tags.get('boundary') == 'national_park' or tags.get('tourism') in ('camp_site', 'picnic_site') or
            FOREST_NAME.search(str(tags.get('designation', ''))) or FOREST_NAME.search(name)):
        return None
    return name, tags


class Source:
    """The boards of the sign tiles, by map cell (32 kept): highwaysigns.Source.objects asks for a box."""
    def __init__(self,md):
        self.md=md;self.cache=OrderedDict();self.lock=threading.Lock()
        net=all(hasattr(md,k) for k in ('raw_dir','cache_dir','fetcher'))
        self.mapped=osmwelcomes.Source(md) if net and OSM_API_ON else None
        self.exits=osmexitdest.Source(md) if net and OSM_API_ON else None
        self.parks=osmparks.Parks(md) if net and OSM_PARKS_ON else None

    def _areas(self, keys):
        """{id: area} of the named recreation areas of the cells: their land use's clipped pieces of one area joined
        (contains() is true inside any piece, so the cells' shared edges are no boundary), and the Layercake
        protected areas (BNE_OSM_PARKS). (areas, complete)."""
        areas,complete={},True
        loaders=[('land_use',lambda k:self.md.cell(k,layers=('land_use',),kind='demand').layers.get('land_use'))]
        if self.parks is not None:
            loaders.append(('osm',lambda k:self.parks.cell(k)))
        for k in keys:
            for source,load in loaders:
                try:
                    f=load(k)
                    if f is None:
                        complete=False;continue
                    for i,fid in enumerate(f.ids):
                        if int(f.kind[i])!=3:
                            continue
                        got=recreation_area(f,i) if source=='land_use' else (
                            str(f.attrs['name'][i]),json.loads(str(f.attrs['tags'][i]) or '{}'))
                        if got is None:
                            continue
                        name,tags=got
                        if tags.get('access') in ('no','private') or tags.get('disused')=='yes':
                            continue
                        parts=f.parts(i)
                        if not parts:
                            continue
                        points=np.vstack([p[0] for p in parts])
                        old=areas.get(str(fid))
                        if old is not None:
                            old['parts']=list(old['parts'])+list(parts)
                            old['lo']=np.minimum(old['lo'],points.min(0));old['hi']=np.maximum(old['hi'],points.max(0))
                            continue
                        kind=next((v for v in (tags.get('boundary'),tags.get('tourism'),tags.get('leisure'),
                                               str(f.attrs['class'][i]) if 'class' in f.attrs else '') if v in CATEGORIES),None)
                        middle=(points.min(0)+points.max(0))/2
                        state=str(signstates.states(middle[1],middle[0]).item())
                        style=recreationrules.entrance(name,tags,state)
                        if kind is None and style is None:
                            continue
                        areas[str(fid)]=dict(id=str(fid),name=name,category=CATEGORIES.get(kind,'RECREATION AREA'),
                                             entrance=style,parts=list(parts),lo=points.min(0),hi=points.max(0))
                except CancelledError:
                    raise
                except Exception as error:
                    complete=False;revisit.fault()
                    log.debug('Recreation areas unavailable for %s (%s): %s',k,source,error)
        return areas,complete

    def _cell(self,key):
        if key in self.cache:self.cache.move_to_end(key);return self.cache[key]
        unique={};links={};borders={};complete=True
        keys=[key,*key.neighbours()]
        for k in keys:
            f=self.md.cell(k,layers=('roads',),kind='demand').layers.get('roads')
            if f is None:return []
            specs=signroads.cell_specs(f)
            for i,fid in enumerate(f.ids):
                sp=specs[i]
                if sp is None or roads.unbuilt(f,i):continue
                target=links if signroads.is_link(f,i) else unique
                wid=railtags.way_id(str(f.attrs['src_record'][i])) if 'src_record' in f.attrs else None
                parts=f.parts(i)
                route_path=np.asarray(max(parts,key=lambda p:len(p[0]))[0],float) if parts else None
                ground=[np.asarray(p,float) for p in roads.ground_parts(f,i) if len(p)>1]
                info=dict(fid=signroads.feature_id(f,i),wid=int(wid) if wid else None,spec=sp,route_id=str(fid),
                          route_path=route_path,subclass=str(f.attrs['subclass'][i]) if 'subclass' in f.attrs else '',
                          access=str(f.attrs['osm_access'][i]) if 'osm_access' in f.attrs else '')
                if not sp.link and sp.cls in BORDER_ROADS:
                    for j,part in enumerate(parts):
                        line=np.asarray(part[0],float)
                        if len(line)>1 and np.isfinite(line).all():
                            borders.setdefault((str(fid),j),dict(info,path=line,ground=ground))
                for j,line in enumerate(ground):
                    if len(line)>1 and np.isfinite(line).all():
                        target.setdefault((str(fid),j),dict(info,path=line))
        x0,y0,x1,y1=key.bounds()
        corners=[k.bounds() for k in keys]
        low=[min(b[0] for b in corners),min(b[1] for b in corners)]
        high=[max(b[2] for b in corners),max(b[3] for b in corners)]
        objects=[]
        if RECREATION_ON and any(r['spec'].cls!='motorway' for r in unique.values()):
            areas,done=self._areas(keys)
            complete=complete and done
            if areas:
                objects+=candidates(list(unique.values()),list(areas.values()),welcomes=False)
        if self.exits is not None and links and any(
                str(signstates.states(r['path'][0,1],r['path'][0,0]).item()) for r in links.values()):
            try:objects+=park_exit_boards(self.exits.objects(list(links.values()),list(unique.values())))
            except CancelledError:raise
            except Exception as error:
                complete=False;revisit.fault()
                log.debug('Recreation exit tags unavailable for %s: %s',key,error)
        supported=WELCOME_ON and any(current(state) and np.all(lower<=high) and np.all(upper>=low)
                                     for state,lower,upper,_ in signstates._polygons())
        if supported and boundary_near(np.array([low,high])):
            import dirsigns
            routes={}
            for k in keys:
                try:
                    layer=self.md.cell(k,layers=('road_routes',),kind='demand').layers.get('road_routes')
                    if layer is None:complete=False;continue
                    text=layer.attrs.get('routes_txt')
                    if text is not None:
                        for i,fid in enumerate(layer.ids):routes[str(fid)]=dirsigns.parse_routes(text[i])
                except CancelledError:raise
                except Exception as error:
                    complete=False;revisit.fault()
                    log.debug('Welcome highway memberships unavailable for %s: %s',k,error)
            for row in [*borders.values(),*unique.values()]:row['routes']=routes.get(row['route_id'],())
            inferred=border_welcomes(list(borders.values()));mapped=[]
            if self.mapped is not None and any(welcome_highway(row) for row in borders.values()):
                nodes={}
                # (a mapped sign may be on the other side of a cell edge from its border fallback)
                for k in keys:
                    try:nodes.update((n['id'],n) for n in self.mapped.cell(k))
                    except CancelledError:raise
                    except Exception as error:
                        complete=False;revisit.fault()
                        log.debug('Mapped welcome signs unavailable for %s: %s',k,error)
                mapped=mapped_welcomes(list(nodes.values()),list(unique.values()))
            objects+=welcome_preference(inferred,mapped)
        objects=[o for o in objects if x0<=o['lon']<x1 and y0<o['lat']<=y1]
        if complete:
            self.cache[key]=objects
            while len(self.cache)>32:self.cache.popitem(last=False)
        return objects

    def query(self,lon0,lat0,lon1,lat1):
        if not (WELCOME_ON or RECREATION_ON or OSM_API_ON):return []
        if lon1 < -180 or lon0 > -60 or lat1 < 18 or lat0 > 72:return []
        found={}
        with self.lock:
            for key in mapcells.cells_for_bbox(lat0,lon0,lat1,lon1):
                found.update((o['id'],o) for o in self._cell(key) if lon0<=o['lon']<=lon1 and lat0<=o['lat']<=lat1)
        return list(found.values())

@lru_cache(maxsize=64)
def _face_name(state,src):
    return 'bne_welcome_'+state.lower()+'_'+hashlib.sha256(Path(src).read_bytes()).hexdigest()[:12]

def face(state,out_dir,vfs):
    """(material name, game path) of the state's photographed face: its shipped DDS (data/welcome/faces, made
    offline from the photos in ATTRIBUTION.md) copied into the session folder once; None for a lettered board."""
    info=current(state)
    if info is None or info.get('board')!='photo':return None
    src=DATA/info['face']
    name=_face_name(state,str(src))
    target=Path(out_dir)/(name+'.dds')
    if not target.is_file():
        tmp=target.with_suffix('.dds.tmp')
        shutil.copyfile(src,tmp);os.replace(tmp,target)
    return name,vfs+'/'+target.name

@lru_cache(maxsize=64)
def welcome_outline(points):
    """Triangulate photographed cutout faces once, including concave state shapes."""
    from buildings import triangulate
    contour=np.asarray(points,float)
    # Image coordinates run down; mesh faces run up.
    area=np.sum(contour[:,0]*np.roll(contour[:,1],-1)-contour[:,1]*np.roll(contour[:,0],-1))
    if area>0:contour=contour[::-1].copy()
    triangles=triangulate(contour*[1.,-1.])
    if len(triangles)!=len(contour)-2:raise ValueError('Invalid welcome sign outline')
    return contour,triangles

class Mesh(throughsigns.Mesh):
    def __init__(self,origin):super().__init__(origin);self.materials=list(exitsigns.GUIDE_MATERIALS)
    def result(self):
        result=super().result()
        return (*result[:3],self.materials,result[4]) if result is not None else None

def render(signs,origin,out_dir,vfs,faces):
    mesh=Mesh(origin)
    for sign in signs:
        extra=sign['extra'];w,h=sign['width'],sign['height'];b=sign['bottom']
        mesh.centre=np.asarray(sign['centre']);travel=np.asarray(sign['travel']);mesh.face=np.r_[-travel,0.];mesh.right=np.r_[travel[1],-travel[0],0.]
        if extra['kind']=='forest_entrance':
            forestboards.render(mesh,sign)
            continue
        if extra['kind']=='welcome' and extra.get('welcome_size')=='small':continue
        timber=extra['kind']=='welcome' and current(extra['state']).get('support')=='timber'
        if extra['kind']=='welcome' and current(extra['state']).get('support')=='monument':
            mesh.box((-.45,-w*.58,min(sign['bases'])-.15),(.25,w*.58,b+.04),2)
        elif timber:
            timber_name='bne_welcome_timber_v1';target=Path(out_dir)/(timber_name+'.dds')
            if not target.is_file():
                grain=np.random.default_rng(17).normal(0,4,(64,1,1))+dnp.sin(np.arange(64)[None,:,None]*2.1)*5
                pixels=np.clip(np.array([102,69,36])+grain,0,255).astype(np.uint8)
                imagery.write_dds(str(target),pixels)
            faces[timber_name]=vfs+'/'+target.name
            if timber_name not in mesh.materials:mesh.materials.append(timber_name)
            material=mesh.materials.index(timber_name)
            for off,base in zip(sign['posts'],sign['bases']):
                start=len(mesh.mi);mesh.bar((-.13,off,base-.2),(-.13,off,b+h+.65),.13);mesh.mi[start:]=[material]*(len(mesh.mi)-start)
            for z in (b-.1,b+h+.3):
                start=len(mesh.mi);mesh.bar((-.13,-w*.7,z),(-.13,w*.7,z),.12);mesh.mi[start:]=[material]*(len(mesh.mi)-start)
        else:
            post_top=current(extra['state']).get('post_top',1.) if extra['kind']=='welcome' else 1.
            for off,base in zip(sign['posts'],sign['bases']):mesh.box((-.13,off-.06,base-.2),(-.02,off+.06,b+h*post_top-.1),2)
        if extra['kind']=='welcome':
            info=current(extra['state'])
            if info.get('board')=='generic':
                # (a lettered board: WELCOME TO over the state's name, white on blue with a white edge)
                mesh.rounded_panel(w,b,h,4)
                mesh.guide_text('WELCOME TO',0,b+h*.62,h*.16,w-.8)
                mesh.guide_text(info['name'].upper(),0,b+h*.22,h*.24,w-.8)
                continue
            asset=face(extra['state'],out_dir,vfs)
            if asset is None:continue
            name,url=asset;faces[name]=url
            if name not in mesh.materials:mesh.materials.append(name)
            material=mesh.materials.index(name);uv=lambda s,t:(s,t)
            if info.get('outline'):
                contour,triangles=welcome_outline(tuple(map(tuple,info['outline'])))
                points=np.c_[np.full(len(contour),.06),(contour[:,0]-.5)*w,b+(1-contour[:,1])*h]
                for indices in triangles:
                    mesh.triangle(points[indices],material)
                    mesh.uv[-3:]=[(float(s),float(1-t)) for s,t in contour[indices]]
                    back=points[indices[::-1]].copy();back[:,0]=-.01
                    mesh.triangle(back,2)
                for a,c in zip(points,np.roll(points,-1,axis=0)):
                    mesh.quad([a,c,[-.01,c[1],c[2]],[-.01,a[1],a[2]]],2)
                continue
            mesh.rounded(w,b,h,.02,-.01,2,front=False,back=True)
            for j in range(12):
                for i in range(12):
                    s0,s1=i/12,(i+1)/12;t0,t1=j/12,(j+1)/12
                    if info.get('shape')=='zigzag':
                        left0=.02 if j%2 else .055;left1=.02 if (j+1)%2 else .055
                        coords=[(left1+(1-2*left1)*s0,t1),(left1+(1-2*left1)*s1,t1),
                                (left0+(1-2*left0)*s1,t0),(left0+(1-2*left0)*s0,t0)]
                    else:coords=[(s0,t1),(s1,t1),(s1,t0),(s0,t0)]
                    mesh.quad([(.06,(s-.5)*w,b+(1-t)*h) for s,t in coords],material,[(float(uv(s,t)[0]),1-float(uv(s,t)[1])) for s,t in coords])
        else:
            # A solid PBR material: no picture-material alpha or stale runtime material.
            name='bne_recreation_brown_opaque'
            if name not in mesh.materials:mesh.materials.append(name)
            brown=mesh.materials.index(name)
            if extra['kind']=='recreation_exit':
                # Separate exit plaque over the rounded destination/instruction panel.
                header=.68
                mesh.rounded_panel(w,b,h-header,brown)
                mesh.rounded_panel(w,b+h-header+.06,header-.06,brown)
                mesh.guide_text('EXIT '+extra['exit'],0,b+h-.49,.34,w-.5)
                for i,line in enumerate(extra['lines']):
                    mesh.guide_text(line,0,b+h-header-.72-i*.62,.46,w-.5)
                mesh.guide_text(extra['instruction'],0,b+.30,.30,w-.5)
                continue
            mesh.rounded_panel(w,b,h,brown)
            words=extra['name'].split();middle=max(1,len(words)//2)
            lines=[extra['name']] if exitsigns.guide_width(extra['name'],.43)<w-.5 else [' '.join(words[:middle]),' '.join(words[middle:])]
            for i,line in enumerate(lines):mesh.guide_text(line,0,b+h-.6-i*.55,.43,w-.6)
            mesh.guide_text(extra['category'],0,b+.3,.25,w-.6)
    return mesh.result()

def place(frame,obj,context,box,elev,chunks=(),existing=(),obstacles=None):
    e=obj['travelboard'];info=welcome_info(e) if e['kind']=='welcome' else None
    if e['kind']=='welcome' and (info is None or e.get('welcome_size')=='small'):return None
    w,h=(info['width_m'],info['height_m']) if info else (min(6.,max(3.2,exitsigns.guide_width(e['name'],.43)+.6)),2.)
    if e['kind'] in ('recreation_exit','forest_entrance'):w,h=e['width_m'],e['height_m']
    point=exitsigns._xy(frame,[[obj['lon'],obj['lat']]])[0];axis=np.asarray(e['axis'])
    pavement=context.pavements(frame,np.asarray(e['approach']),False);choices=[]
    for row in pavement:
        fid,sp,line,width=row
        matches=fid==e['feature_id'] or (e['main_id'] is not None and exitsigns._way_id(fid)==e['main_id']) or exitsigns._way_id(fid) in e.get('main_ids',())
        if sp.link or (not matches and not (e.get('allow_mainline_match') and sp.cls in ('motorway','trunk'))):continue
        near=exitsigns._nearest(point,[row])
        if near is None or near[0]>(100 if e.get('mapped') else 25):continue
        travel=near[7]*(sp.dir or 1) if sp.oneway else exitsigns._travel(near,axis)
        if dnp.matmul(travel, axis)>.8:choices.append((near[0],str(fid),near,travel,width))
    if not choices:return None
    _,fid,near,travel,width=min(choices,key=lambda p:p[:2])
    if exitsigns._on_deck(near[6],chunks,2):return None
    if any((other:=exitsigns._nearest(near[6],[row])) is not None and other[0]<30 and abs(dnp.matmul(other[7], travel))<.85 for row in pavement if str(row[0])!=fid):return None
    right=np.array([travel[1],-travel[0]])*e.get('side',1);polygons=exitsigns._outlines(pavement)
    walks=[]
    for _,sp,line,road_width in pavement:
        for side in getattr(sp,'sides',()):
            cross=signroads.cross(line)*side
            inner=line[:,:2]+cross*road_width/2
            outer=line[:,:2]+cross*(road_width/2+sp.walk+1.)
            walks.append(np.vstack([inner,outer[::-1],inner[:1]]))
    if walks:polygons=signroads.Outlines([*polygons,*walks])
    centres=[point] if e.get('mapped') else [near[6]+right*offset for offset in np.arange(width/2+w/2+3.,width/2+w/2+14.,1.)]
    for centre in centres:
        if not exitsigns._clear(centre,w/2+.6,polygons) or exitsigns._on_deck(centre,chunks,w/2+1):continue
        if obstacles is not None and not obstacles.clear(centre,w/2+.6):continue
        import signlayout
        # Use the actual panel envelopes for inferred boards too. A nearby
        # advance guide can be clear of the welcome panel despite its centre
        # falling inside the old 20 m crowding radius.
        occupied=dict(centre=[*centre,0.],travel=travel,width=w*(1.4 if info and info.get('support')=='timber' else 1.),
                      height=h,bottom=0.)
        clearance=1. if e.get('mapped') else 2.
        if any(signlayout.overlap(occupied,dict(s,bottom=0.),clearance=clearance) for s in existing):continue
        if e['kind']=='welcome' and not e.get('mapped'):
            lat,lon,_=frame.enu_to_geodetic(*centre,0.)
            if str(signstates.states(lat,lon).item())!=e['state']:continue
        z=exitsigns._ground_z(frame,elev,centre);post_offset=.62 if info and info.get('support')=='timber' else .32;posts=[-w*post_offset,w*post_offset]
        bases=[exitsigns._ground_z(frame,elev,centre+np.array([travel[1],-travel[0]])*p)-z for p in posts];road_z=exitsigns._ground_z(frame,elev,near[6])-z
        if not np.isfinite([z,road_z,*bases]).all() or max(bases)-min(bases)>w*.3:continue
        if not roads.box_owns(box,*centre):return None
        clearance=.8 if e['kind']=='forest_entrance' or (info and info.get('support')=='monument') else 2.2
        if info:clearance=float(info.get('clearance_m',clearance))
        return dict(id=obj['id'],extra=e,centre=[*centre,z],travel=travel.tolist(),width=w,height=h,bottom=max(clearance+max(bases),road_z+clearance),posts=posts,bases=bases)
    return None

def build_tile(md,frame,objects,context,box,elev,chunks,out_dir,vfs,faces,existing=()):
    if out_dir is None or vfs is None:return None
    placed=[]
    for obj in sorted((o for o in objects if 'travelboard' in o),key=lambda o:(
            o['travelboard']['kind']!='welcome',not o['travelboard'].get('mapped',False),o['id'])):
        if obj['travelboard'].get('welcome_size')=='small':continue
        alternative=obj['travelboard'].get('mapped_alternative')
        if alternative:
            if any(s['id']==alternative['id'] for s in placed):continue
            ap=exitsigns._xy(frame,[[alternative['lon'],alternative['lat']]])[0]
            ak=tuple(np.floor(ap/roads.TILE).astype(int))
            ao=signplacement.for_tile(frame,*ak,md,())
            # Test the mapped sign in its own tile. A failed mapped position
            # must not suppress a usable fallback on the receiving shoulder.
            if place(frame,alternative,context,roads.tile_bbox(*ak),elev,chunks,list(existing)+placed,ao) is not None:continue
        point=exitsigns._xy(frame,[[obj['lon'],obj['lat']]])[0]
        key=tuple(np.floor(point/roads.TILE).astype(int))
        obstacles=signplacement.for_tile(frame,*key,md,())
        sign=place(frame,obj,context,box,elev,chunks,list(existing)+placed,obstacles)
        if sign is not None:placed.append(sign)
    return render(placed,(box[0],box[1],0.),out_dir,vfs,faces)
