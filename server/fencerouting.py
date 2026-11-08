"""Deterministic facility-run DAG optimization. No per-frame analysis or AI service.

Stations parameterize a complete geographic exterior. Physical paths are macro
edges; supported road-side corrections are alternatives, not independent panels.
"""
from dataclasses import dataclass,field
import json,math,os
import numpy as np
import shapely
from shapely.geometry import LineString,Point,Polygon
from shapely.ops import substring,unary_union
from shapely.strtree import STRtree

DEFAULT_WEIGHTS=dict(source=12.,length=.02,boundary=.25,interior=12.,road_side=35.,switch=150.,
                    gap=1.,bend=2.,building=1000.,transport=1000.,duplicate=1000.)

def settings():
    weights=dict(DEFAULT_WEIGHTS)
    supplied=json.loads(os.environ.get('BNE_FENCE_ROUTING_WEIGHTS','{}'))
    if set(supplied)-set(weights):raise ValueError('unknown fence routing weight')
    weights.update({k:float(v) for k,v in supplied.items()})
    if any(not math.isfinite(v) or v<0 for v in weights.values()):raise ValueError('invalid routing weight')
    result=dict(weights=weights,station_m=float(os.environ.get('BNE_FENCE_ROUTE_STATION_M','32')),
        association_m=float(os.environ.get('BNE_FENCE_ROUTE_ASSOCIATION_M','12')),
        road_clearance_m=float(os.environ.get('BNE_FENCE_ROUTE_ROAD_CLEARANCE_M','0.65')))
    if not all(math.isfinite(result[k]) for k in ('station_m','association_m','road_clearance_m')) or result['road_clearance_m']<0:
        raise ValueError('invalid fence routing distance')
    return result

def parts(geometry):
    if geometry.is_empty:return []
    if geometry.geom_type in ('LineString','LinearRing'):return [LineString(geometry)]
    return [line for child in getattr(geometry,'geoms',()) for line in parts(child)]

def canonical(coords):
    points=[tuple(p) for p in np.asarray(coords)]
    if points[0]==points[-1]:
        points=points[:-1];at=min(range(len(points)),key=points.__getitem__)
        one=points[at:]+points[:at];two=[one[0]]+one[:0:-1];points=min(one,two);return points+[points[0]]
    return min(points,list(reversed(points)))

def unit(a,b):
    v=np.asarray(b)-a;n=float(np.linalg.norm(v));return v/max(n,1e-12)

def interior_side(interior,point,tangent,probe=8.):
    normal=np.array([-tangent[1],tangent[0]])
    positive=interior.covers(Point(np.asarray(point)+normal*probe))
    negative=interior.covers(Point(np.asarray(point)-normal*probe))
    return int(positive)-int(negative)

class Roads:
    def __init__(self,records,interior,reach):
        self.records=[dict(r,geometry=LineString(canonical(line.coords))) for r in records for line in parts(r['geometry'])]
        self.interior=interior;self.reach=reach
        self.tree=STRtree([r['geometry'] for r in self.records])
        self._near={}
    def near(self,point,tangent):
        point=np.asarray(point);found=[]
        key=tuple(point)+tuple(tangent)
        if key in self._near:return self._near[key]
        for index in self.tree.query(Point(point),predicate='dwithin',distance=self.reach):
            record=self.records[int(index)];line=record['geometry'];s=line.project(Point(point));q=line.interpolate(s)
            a=np.array(line.interpolate(max(0.,s-.5)).coords[0]);b=np.array(line.interpolate(min(line.length,s+.5)).coords[0])
            direction=unit(a,b)
            if abs(float(direction@tangent))<.7:continue
            # Geographic canonical direction, independent of OSM digitization.
            if direction[0]<-1e-9 or abs(direction[0])<=1e-9 and direction[1]<0:direction=-direction
            normal=np.array([-direction[1],direction[0]])
            delta=point-np.array(q.coords[0])
            # A distant road endpoint is not an infinite parallel centerline.
            if abs(float(delta@direction))>record['radius']+.65:continue
            signed=float(delta@normal)
            inside=0
            for probe in (max(8.,record['radius']+2.),16.,32.,64.,128.):
                inside=interior_side(self.interior,np.array(q.coords[0]),direction,probe)
                if inside:break
            found.append((float(np.linalg.norm(delta)),record['id'],record,np.array(q.coords[0]),normal,signed,inside))
        result=min(found,key=lambda v:v[:2]) if found else None
        if len(self._near)>16384:self._near.clear()
        self._near[key]=result
        return result
    def state(self,path):
        samples=[]
        for fraction in (.2,.5,.8):
            s=path.length*fraction;point=np.array(path.interpolate(s).coords[0])
            tangent=unit(np.array(path.interpolate(max(0,s-.5)).coords[0]),np.array(path.interpolate(min(path.length,s+.5)).coords[0]))
            nearby=self.near(point,tangent)
            if nearby:
                _,rid,record,_,_,signed,inside=nearby
                side=1 if signed>.01 else -1 if signed<-.01 else 0
                samples.append((rid,side*inside if inside else 0,side,inside))
        if not samples:return None
        return max(set(samples),key=lambda x:(samples.count(x),x))

@dataclass
class Edge:
    u:int;v:int;geometry:object;source:str;confidence:str
    score:float=0.;state:object=None;reason:str='';terms:dict=field(default_factory=dict)

def optimize(interior,boundary,physical,road_records=(),openings=(),obstacles=(),owner='inferred',config=None):
    """Return ordered selected source runs and an explainable facility routing audit.

    Physical entries are (source ID, geometry). Internal compounds are handled by
    facility classification, not this primary exterior route solver.
    """
    cfg=config or settings();weights=cfg['weights'];reach=cfg['association_m']
    if not 4<=cfg['station_m']<=256 or not 0<reach<=50:raise ValueError('invalid fence route resolution')
    roads=Roads(list(road_records),interior,reach+10.)
    allowed=unary_union(list(openings));obstacle_tree=STRtree(list(obstacles))
    anchors={}
    for _,geometry in physical:
        for line in parts(geometry):
            # A connected physical chain may follow several different roads.
            # Three votes on the entire chain would erase its northern anchor.
            cuts=np.linspace(0.,line.length,max(1,math.ceil(line.length/cfg['station_m']))+1)
            for lo,hi in zip(cuts,cuts[1:]):
                state=roads.state(substring(line,lo,hi))
                if state:anchors.setdefault(state[0],[]).append((state[1],hi-lo))
    preferred={rid:max((-1,1),key=lambda sign:(sum(length for side,length in samples if side==sign),sign))
        for rid,samples in anchors.items() if sum(length for side,length in samples if side)!=0}
    all_runs=[];audit=dict(weights=weights,road_preferences=preferred,candidates=[],selected=[],gaps=[],side_switches=[],source_coverage=[],
        roads=[dict(id=r['id'],radius=r['radius'],geometry_xy=json.loads(shapely.to_geojson(r['geometry']))) for r in roads.records])
    for ring_index,raw_ring in enumerate(parts(boundary)):
        ring=LineString(canonical(raw_ring.coords));length=ring.length
        closed=ring.is_ring
        if length<.02:continue
        stations=set(np.linspace(0.,length,max(1,math.ceil(length/cfg['station_m']))+1))
        cumulative=0.
        for a,b in zip(ring.coords,ring.coords[1:]):stations.add(cumulative);cumulative+=math.dist(a,b)
        macros=[]
        for source,geometry in physical:
            for physical_line in parts(geometry):
                if physical_line.is_ring:
                    if physical_line.hausdorff_distance(ring)>reach:continue
                    # Whole physical rings are already supported, never invented.
                    macros.append((0.,length,LineString(canonical(physical_line.coords)),source));continue
                start,end=Point(physical_line.coords[0]),Point(physical_line.coords[-1])
                if max(start.distance(ring),end.distance(ring))>reach:continue
                s,t=ring.project(start),ring.project(end)
                if closed:
                    if min(s,length-s)<.005:s=0.
                    if min(t,length-t)<.005:t=0.
                if abs(s-t)<.005:continue
                lo,hi=sorted((s,t));middle=substring(ring,lo,hi)
                outside=unary_union([substring(ring,0,lo),substring(ring,hi,length)])
                if middle.hausdorff_distance(physical_line)<=outside.hausdorff_distance(physical_line):
                    oriented=physical_line if s<t else LineString(list(physical_line.coords)[::-1])
                    macros.append((lo,hi,oriented,source));stations.update((lo,hi))
                else:
                    # Split a wrapping physical route at the same stable ring seam.
                    at=physical_line.project(Point(ring.coords[0]))
                    if physical_line.interpolate(at).distance(Point(ring.coords[0]))>reach:continue
                    if s<t:
                        first=substring(physical_line,0,at);last=substring(physical_line,at,physical_line.length)
                        if first.geom_type=='LineString':first=LineString(list(first.coords)[::-1])
                        if last.geom_type=='LineString':last=LineString(list(last.coords)[::-1])
                    else:first=substring(physical_line,at,physical_line.length);last=substring(physical_line,0,at)
                    if lo>.005 and first.geom_type=='LineString':macros.append((0.,lo,first,source))
                    if length-hi>.005 and last.geom_type=='LineString':macros.append((hi,length,last,source))
                    stations.update((lo,hi))
        # GEOS length/projection and math.dist can differ at the last few bits
        # across platforms. A near-duplicate station makes a zero-length DAG
        # edge which evaluate() rejects, falsely reporting missing source.
        # Collapse only sub-micrometre numerical intervals, far below the
        # existing 5 mm endpoint tolerance; retain the exact source endpoints.
        distinct=[]
        for value in sorted(max(0., min(length, float(s))) for s in stations):
            if not distinct or value-distinct[-1]>1e-7:
                distinct.append(value)
        distinct[-1]=length
        stations=distinct;outgoing={i:[] for i in range(len(stations))}
        def station(value):return min(range(len(stations)),key=lambda i:abs(stations[i]-value))
        def evaluate(edge):
            path=edge.geometry
            if path.is_empty or path.length<.001:return False
            state=roads.state(path);edge.state=state
            reference_arc=substring(ring,stations[edge.u],stations[edge.v])
            terms=dict(source=(0. if edge.confidence=='physical' else 1. if edge.confidence=='boundary' else 2.)*path.length/cfg['station_m'],length=path.length,boundary=0. if edge.confidence=='physical' else path.hausdorff_distance(reference_arc),interior=0.,road_side=0.,building=0.,transport=0.)
            blocked=False
            if not path.is_simple:blocked=True;terms['duplicate']=1.
            if len(obstacle_tree.query(path,predicate='intersects')):terms['building']=1.;blocked=True
            side_conflict=bool(state and state[0] in preferred and state[1] and state[1]!=preferred[state[0]])
            if side_conflict:
                terms['road_side']=path.length/cfg['station_m']
                crossing=any(path.crosses(record['geometry']) for record in roads.records if record['id']==state[0])
                if edge.confidence!='physical' and not crossing and not allowed.intersects(path):blocked=True
            for fraction in (.2,.5,.8):
                s=path.length*fraction;point=np.array(path.interpolate(s).coords[0]);tangent=unit(np.array(path.interpolate(max(0,s-.5)).coords[0]),np.array(path.interpolate(min(path.length,s+.5)).coords[0]))
                side=interior_side(interior,point,tangent)
                if side==0:terms['interior']+=.1 # ambiguous is recorded, not forced by winding
                reference_s=stations[edge.u]+fraction*(stations[edge.v]-stations[edge.u])
                reference=np.array(ring.interpolate(reference_s).coords[0])
                reference_tangent=unit(np.array(ring.interpolate(max(0,reference_s-.5)).coords[0]),np.array(ring.interpolate(min(length,reference_s+.5)).coords[0]))
                expected_side=interior_side(interior,reference,reference_tangent)
                if side and expected_side and side!=expected_side:terms['interior']+=1.
                near=roads.near(point,tangent)
                if near and abs(near[5])<near[2]['radius']+.22 and not allowed.covers(Point(point)) and not path.crosses(near[2]['geometry']):
                    terms['transport']+=1.;blocked=True
            edge.terms=terms;edge.score=sum(weights[k]*v for k,v in terms.items())
            edge.reason='rejected: established facility road-side orientation' if blocked and side_conflict else 'rejected: building or parallel carriageway conflict' if blocked else 'explicit physical evidence' if edge.confidence=='physical' else 'supported road-side correction' if edge.confidence=='correction' else 'facility exterior evidence'
            audit['candidates'].append(dict(ring=ring_index,u=edge.u,v=edge.v,source=edge.source,confidence=edge.confidence,score=edge.score,terms=terms,road_state=state,reason=edge.reason,geometry_xy=json.loads(shapely.to_geojson(path))))
            if blocked:return False
            outgoing[edge.u].append(edge);return True
        for i,(lo,hi) in enumerate(zip(stations,stations[1:])):
            base=substring(ring,lo,hi);evaluate(Edge(i,i+1,base,owner,'boundary'))
            near=roads.near(np.array(base.interpolate(.5,normalized=True).coords[0]),unit(base.coords[0],base.coords[-1]))
            if not near:continue
            base_state=roads.state(base)
            wrong=near[1] in preferred and base_state and base_state[1]!=preferred[near[1]]
            touches=False
            for fraction in (0.,.5,1.):
                n=roads.near(np.array(base.interpolate(fraction,normalized=True).coords[0]),unit(base.coords[0],base.coords[-1]))
                if n and abs(n[5])<n[2]['radius']+.22:touches=True
            if not touches and not wrong:continue
            for sign in (-1,1):
                points=[];valid=True
                for fraction in (0.,.5,1.):
                    point=np.array(base.interpolate(fraction,normalized=True).coords[0]);n=roads.near(point,unit(base.coords[0],base.coords[-1]))
                    if n is None or n[1]!=near[1]:points.append(point);continue
                    shifted=n[3]+n[4]*sign*(n[2]['radius']+cfg['road_clearance_m'])
                    if np.linalg.norm(shifted-point)>reach:valid=False;break
                    points.append(shifted)
                if valid:evaluate(Edge(i,i+1,LineString(points),owner,'correction'))
        for lo,hi,path,source in macros:
            u,v=station(lo),station(hi)
            if v>u:evaluate(Edge(u,v,path,source,'physical'))
        for i,(lo,hi) in enumerate(zip(stations,stations[1:])):
            gap=Edge(i,i+1,substring(ring,lo,hi),owner,'gap',score=10000.+weights['gap']*(hi-lo),reason='unresolved: no globally compatible supported route')
            outgoing[i].append(gap)
        switch_log=set()
        feasible=[e for edges in outgoing.values() for e in edges if e.confidence!='gap']
        candidate_tree=STRtree([e.geometry for e in feasible])
        conflicts={}
        for edge in feasible:
            found=set()
            for index in candidate_tree.query(edge.geometry,predicate='crosses'):
                other=feasible[int(index)];intersection=edge.geometry.intersection(other.geometry)
                # Geographic rounding can put a shared source endpoint a few
                # micrometres through its neighbor. Only interior crossings
                # are topology conflicts; the established tolerance is 5 mm.
                points=list(intersection.geoms) if hasattr(intersection,'geoms') else [intersection]
                if any(p.geom_type=='Point' and all(p.distance(Point(endpoint))>.005
                    for line in (edge.geometry,other.geometry) for endpoint in (line.coords[0],line.coords[-1])) for p in points):
                    found.add(id(other))
            conflicts[id(edge)]=found
        def transition(one,two):
            if one.confidence=='gap' or two.confidence=='gap':return 0.
            a,b=np.array(one.geometry.coords[-1]),np.array(two.geometry.coords[0]);distance=float(np.linalg.norm(b-a))
            if distance>reach:return None
            connection=LineString([a,b])
            permitted=allowed.intersects(connection)
            if distance>.005 and len(obstacle_tree.query(connection,predicate='intersects')):return None
            if one.state and two.state and one.state[0]==two.state[0] and one.state[1]*two.state[1]<0 and not permitted:
                token=(one.v,two.u,one.state[0],one.state[1],two.state[1])
                if token not in switch_log:
                    switch_log.add(token);audit['side_switches'].append(dict(node=one.v,road=one.state[0],before=one.state,after=two.state,reason='rejected unexplained road-side switch'))
                return None
            if distance>.005:
                if len(roads.tree.query(connection,predicate='crosses')) and not permitted:return None
            turn=1.-float(unit(one.geometry.coords[-2],a)@unit(b,two.geometry.coords[1]))
            explained_switch=bool(one.state and two.state and one.state[0]==two.state[0] and one.state[1]*two.state[1]<0)
            return weights['gap']*distance+weights['bend']*turn+weights['switch']*explained_switch
        best=None
        for first in sorted(outgoing[0],key=lambda e:(e.score,e.source,e.v)):
            states={first.v:[(first.score,[first])]}
            for i in range(1,len(stations)):
                incoming=states.get(i,[])
                if not incoming:continue
                # Each incoming edge is a DP state; do not greedily retain only a
                # locally cheapest side, which would lose the physical anchor.
                for edge in outgoing.get(i,[]):
                    candidates=[]
                    for cost,path in incoming:
                        if conflicts.get(id(edge),set()).intersection(id(previous) for previous in path):continue
                        extra=transition(path[-1],edge)
                        if extra is not None:candidates.append((cost+edge.score+extra,path+[edge]))
                    if candidates:
                        choice=min(candidates,key=lambda x:(x[0],tuple(e.source for e in x[1])))
                        states.setdefault(edge.v,[]).append(choice)
            for cost,path in states.get(len(stations)-1,[]):
                extra=transition(path[-1],first) if closed else 0.
                if extra is not None and (best is None or cost+extra<best[0]):best=(cost+extra,path)
        if best is None:
            # Virtual unresolved edges make the DAG traversable. A missing
            # solution is an implementation error, never a local greedy route.
            raise RuntimeError('facility routing DAG has no complete solution')
        else:chosen=best[1]
        covered=sum(stations[e.v]-stations[e.u] for e in chosen)
        if abs(covered-length)>.005:raise RuntimeError('facility routing lost a source interval')
        audit['source_coverage'].append(dict(ring=ring_index,expected_m=length,covered_m=covered,
            unresolved_m=sum(stations[e.v]-stations[e.u] for e in chosen if e.confidence=='gap')))
        previous=None
        for edge in chosen:
            if edge.confidence=='gap':
                kind='mapped access opening' if allowed.covers(edge.geometry) else 'unresolved geographic constraints'
                audit['gaps'].append(dict(ring=ring_index,lo=stations[edge.u],hi=stations[edge.v],classification=kind,reason=edge.reason,geometry_xy=json.loads(shapely.to_geojson(edge.geometry))))
                all_runs.append(dict(source=owner,geometry=edge.geometry,confidence='gap',ring=ring_index,render=False));previous=edge;continue
            if previous is not None and previous.confidence!='gap':
                a,b=previous.geometry.coords[-1],edge.geometry.coords[0]
                if math.dist(a,b)>.005:
                    if transition(previous,edge) is None:
                        audit['gaps'].append(dict(ring=ring_index,reason='unsupported endpoint transition',endpoints=[a,b]))
                    else:all_runs.append(dict(source=owner,geometry=LineString([a,b]),confidence='correction',ring=ring_index))
            all_runs.append(dict(source=edge.source,geometry=edge.geometry,confidence=edge.confidence,ring=ring_index))
            audit['selected'].append(dict(ring=ring_index,u=edge.u,v=edge.v,source=edge.source,confidence=edge.confidence,road_state=edge.state,score=edge.score,reason=edge.reason,geometry_xy=json.loads(shapely.to_geojson(edge.geometry))))
            previous=edge
        if closed and chosen and chosen[-1].confidence!='gap' and chosen[0].confidence!='gap':
            a,b=chosen[-1].geometry.coords[-1],chosen[0].geometry.coords[0]
            if math.dist(a,b)>.005 and transition(chosen[-1],chosen[0]) is not None:
                all_runs.append(dict(source=owner,geometry=LineString([a,b]),confidence='correction',ring=ring_index))
    return all_runs,audit
