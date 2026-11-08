"""Pass 1 contract: immutable geographic routes, topology, confidence and gaps.

No terrain sampler, generated mesh, tile owner or engine object participates in
reconstruction. Pass 2 can report failures without rewriting this manifest.
"""
from dataclasses import dataclass
import hashlib,json
import numpy as np
import shapely
from shapely.geometry import LineString,Point,shape
from shapely.strtree import STRtree
import facilityperimeters as facilities

@dataclass(frozen=True)
class Route:
    source:str
    facility:str
    category:str
    classification:str
    coordinates:tuple
    source_ids:tuple
    confidence:float
    component:str

@dataclass(frozen=True)
class Network:
    routes:tuple
    digest:str
    report:dict
    areas:tuple
    metadata:dict
    facilities:tuple
    claimed:frozenset


def topology(routes,facility_audit):
    """Validate endpoint connectivity with a 5 mm numerical snap, not a repair halo."""
    groups={};issues=[];nodes=[];components=[];edges=[]
    by_facility={f['facility_id']:f for f in facility_audit}
    for route in routes:groups.setdefault(route.component,[]).append(route)
    for group,paths in sorted(groups.items()):
        facility=by_facility[paths[0].facility]
        import geodesy
        frame=geodesy.EnuFrame(*facility['validation_frame'],0.)
        def metric(c):
            e,n,_=frame.geodetic_to_enu(c[:,1],c[:,0],0.);return np.c_[e,n]
        opening=shapely.transform(shape(facility['opening_masks_lonlat']),metric)
        interior=shapely.transform(shape(facility['interior_lonlat']),metric)
        geometries=[shapely.transform(LineString(route.coordinates),metric) for route in paths]
        endpoints=[Point(p) for line in geometries for p in (line.coords[0],line.coords[-1])]
        tree=STRtree(endpoints);parent=list(range(len(endpoints)))
        def root(i):
            while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
            return i
        for i,point in enumerate(endpoints):
            for j in tree.query(point,predicate='dwithin',distance=.005):
                a,b=sorted((root(i),root(int(j))));parent[b]=a
        members={}
        for i in range(len(endpoints)):members.setdefault(root(i),[]).append(i)
        node_ids={};degrees={};adj={}
        for indices in members.values():
            coordinates=min(tuple(paths[i//2].coordinates[0 if i%2==0 else -1]) for i in indices)
            nid=hashlib.sha1(repr((group,coordinates)).encode()).hexdigest()[:16]
            for i in indices:node_ids[i]=nid
            degrees[nid]=len(indices);adj[nid]=set()
            point=endpoints[indices[0]];kind='junction' if len(indices)>2 else 'connected' if len(indices)==2 else 'mapped_opening' if opening.distance(point)<=.01 else 'unexplained_endpoint'
            incident=[paths[i//2] for i in indices]
            recovery=kind=='connected' and any(r.confidence==1. for r in incident) and any(r.confidence<1. for r in incident)
            nodes.append(dict(id=nid,component=group,coordinates=coordinates,degree=len(indices),classification=kind,
                sources=sorted(set(r.source for r in incident)),recovery='high-confidence source-boundary continuation of mapped physical fence' if recovery else None))
            if kind in ('unexplained_endpoint','junction'):issues.append(dict(component=group,node=nid,classification=kind,coordinates=coordinates))
        edge_tree=STRtree(geometries)
        for i,line in enumerate(geometries):
            a,b=node_ids[2*i],node_ids[2*i+1];adj[a].add(b);adj[b].add(a)
            import fencerouting
            middle=line.length/2
            tangent=fencerouting.unit(np.asarray(line.interpolate(max(0.,middle-.5)).coords[0]),np.asarray(line.interpolate(min(line.length,middle+.5)).coords[0]))
            inside=fencerouting.interior_side(interior,np.asarray(line.interpolate(middle).coords[0]),tangent)
            edges.append(dict(id=hashlib.sha1(repr(paths[i]).encode()).hexdigest()[:16],source=paths[i].source,source_ids=paths[i].source_ids,
                component=group,start=a,end=b,confidence=paths[i].confidence,interior_side=inside,
                interior_orientation='positive-normal' if inside>0 else 'negative-normal' if inside<0 else 'ambiguous; physical/source evidence retained',
                coordinates=paths[i].coordinates))
            if not line.is_simple:issues.append(dict(component=group,source=paths[i].source,classification='invalid_self_intersection'))
            for j in edge_tree.query(line,predicate='overlaps'):
                if int(j)>i:issues.append(dict(component=group,sources=[paths[i].source,paths[int(j)].source],classification='overlapping_edges'))
        unseen=set(adj);count=0
        while unseen:
            todo=[min(unseen)];count+=1
            while todo:
                current=todo.pop()
                if current not in unseen:continue
                unseen.remove(current);todo.extend(adj[current]&unseen)
        components.append(dict(id=group,facility=paths[0].facility,paths=len(paths),nodes=len(adj),connected_components=count,
            status='valid_partial' if any(i['component']==group for i in issues) else 'validated',
            owner_rule='geographic panel midpoint in pass 2; no display-tile clipping in pass 1'))
    gaps=[dict(gap,facility=f['facility_id'],confidence='low')
        for f in facility_audit for gap in f.get('routing',{}).get('gaps',[])]
    return dict(nodes=nodes,edges=edges,components=components,issues=issues,unresolved_gaps=gaps,
        road_preferences={f['facility_id']:f.get('routing',{}).get('road_preferences',{}) for f in facility_audit},
        repairs='supported source arcs and physical chains selected by facility-wide DAG; no endpoint-distance chords')


def reconstruct(frame,areas,metadata,infrastructure=(),extra_evidence=None,routing_context=None,debug=False):
    selected,meta,audit,claimed=facilities.select(frame,areas,metadata,infrastructure,extra_evidence,routing_context,debug=debug)
    if any(f['completeness']['status']=='implementation_gap' for f in audit):
        raise ValueError('pass 1 failed geographic coverage validation')
    routes=[]
    for source,category,rings in selected:
        record=meta[source];classification=record['boundary_class']
        component=record['facility_id']+('/enclosure/'+','.join(record.get('enclosure_ids') or record['source_ids']) if classification=='secured_compound' else '/main')
        confidence=1. if record['confidence']=='explicit' else .9 if classification=='corroborated_exterior' else .85
        # Approved geometry includes actual gate masks. Preserve original arcs
        # separately for stock-module phase; the mesh adapter cannot close them.
        for line in facilities.lines(record['allowed_lonlat']):
            if line.length<1e-10:continue
            if not line.is_valid or not line.is_simple:raise ValueError('invalid approved perimeter component '+source)
            routes.append(Route(source,record['facility_id'],category,classification,tuple(tuple(float(v) for v in p) for p in line.coords),tuple(record['source_ids']),confidence,component))
    routes=tuple(routes)
    digest=hashlib.sha256(repr(routes).encode()).hexdigest()
    report=topology(routes,audit)
    report['authoritative_sources']=[f['routing']['authoritative'] for f in audit if f.get('authoritative_policy')]
    report['proposals']=[dict(candidate,facility=f['facility_id'],validation_frame=f['validation_frame'],confidence='medium',render=False,
        review_reason='supported alternative; not selected by complete-run optimizer') for f in audit
        for candidate in f.get('routing',{}).get('candidates',[]) if candidate['confidence']=='correction' and not candidate['reason'].startswith('rejected')
        and not any(candidate['geometry_xy']==s.get('geometry_xy') for s in f['routing'].get('selected',[]))]
    report['proposals'].extend(dict(proposal,facility=f['facility_id']) for f in audit for proposal in f.get('routing',{}).get('proposals',[]))
    report.update(pass_number=1,digest=digest,approved_paths=len(routes),confidence_policy=dict(high='approved explicit or validated source-supported route',medium='review-only alternative; not a mesh source',low='unresolved; never rendered'))
    return Network(routes,digest,report,tuple(selected),meta,tuple(audit),frozenset(claimed))
