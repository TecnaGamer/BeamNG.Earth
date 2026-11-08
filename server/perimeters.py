"""Global OSM facility perimeters, complete polygons through mapdata, existing road tile stream.

Rows: ['perimeter', ax, ay, az, bx, by, bz, stable_id, osm_id]. No location allowlist.
Only outer rings are fenced; holes remain holes. Never fence clipped cell edges.
"""
import hashlib
import json
import logging
import math
import os
import re

import numpy as np
import mapcells
import mapgeom
import geodesy

VERSION = 8
class PerimeterPending(RuntimeError):
    """A valid tile must be retried; an empty optional result is not complete."""
MODEL = '/levels/Industrial/art/shapes/buildings/ind_mesh_fence_4m.dae'
LENGTH = 4.0  # fence fabric x=-2..2; post caps extend to -2.078..2.066
LAYERS = ('perimeter_areas', 'perimeter_infrastructure')
log = logging.getLogger('earthd.perimeters')


def tags_of(feats, i):
    try:
        value = json.loads(str(feats.attrs.get('tags', ['{}'] * len(feats))[i]))
        return {str(k).lower(): str(v).lower() for k, v in value.items()} if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def classify(tags, cls=''):
    if tags.get('building') not in (None, '', 'no'):
        return None
    if tags.get('landuse') in ('industrial', 'military', 'quarry'):
        return tags['landuse']
    if tags.get('military') == 'base':
        return 'military'
    if tags.get('aeroway') == 'aerodrome':
        return 'airport'
    if tags.get('industrial') == 'mine' and tags.get('disused') != 'yes' and tags.get('abandoned') != 'yes':
        return 'mine'
    if tags.get('aeroway') or tags.get('military') or tags.get('industrial') == 'mine':
        return None
    # Normalized class survives when Overture has not retained the source tag.
    return {'industrial': 'industrial', 'military': 'military', 'quarry': 'quarry',
            'mine': 'mine', 'airport': 'airport', 'aerodrome': 'airport'}.get(cls)


def osm_id(feats, i):
    record = str(feats.attrs.get('src_record', [''] * len(feats))[i])
    dataset = str(feats.attrs.get('src_dataset', [''] * len(feats))[i]).lower()
    match = re.fullmatch(r'([wr])(\d+)(?:@\d+)?', record)
    if not match or dataset and 'openstreetmap' not in dataset and dataset != 'osm':
        return None
    return ('way/' if match[1] == 'w' else 'relation/') + match[2]


def closed_ring(ring):
    ring = np.asarray(ring, float)
    if len(ring) < 4 or not np.isfinite(ring).all() or not np.allclose(ring[0], ring[-1], atol=1e-10, rtol=0):
        return False
    return len(np.unique(ring[:-1], axis=0)) >= 3


def canonical_ring(ring):
    """Same edge order for a reversed or cyclically shifted outer ring."""
    pts = [tuple(p) for p in np.asarray(ring)[:-1]]
    pts = [p for i,p in enumerate(pts) if p != pts[i-1]]
    first = min(range(len(pts)), key=pts.__getitem__)
    forward = pts[first:] + pts[:first]
    backward = [forward[0]] + forward[:0:-1]
    result = min(forward, backward)
    return np.asarray(result + [result[0]], float)


def subtract(intervals, cuts):
    for lo, hi in sorted(cuts):
        result = []
        for a,b in intervals:
            if hi <= a or lo >= b:
                result.append((a,b))
            else:
                if lo > a: result.append((a,min(b,lo)))
                if hi < b: result.append((max(a,hi),b))
        intervals = result
    return intervals


def capsule(a, direction, length, c, d, radius):
    """Arc intervals on a fence edge inside a buffered road/fence/gate segment."""
    c,d = np.asarray(c,float),np.asarray(d,float)
    delta = d-c
    span = float(np.linalg.norm(delta))
    cuts = []
    # Circular ends.
    for centre in (c,d):
        v = centre-a
        along = float(v@direction)
        perp2 = max(0.,float(v@v)-along*along)
        if perp2 <= radius*radius:
            half = math.sqrt(max(0.,radius*radius-perp2))
            cuts.append((max(0.,along-half),min(length,along+half)))
    if span > 1e-8:
        tangent = delta/span
        normal = np.array([-tangent[1],tangent[0]])
        lo,hi = 0.,length
        for axis,lower,upper in ((tangent,0.,span),(normal,-radius,radius)):
            offset,slope = float((a-c)@axis),float(direction@axis)
            if abs(slope)<1e-12:
                if not lower <= offset <= upper: hi = -1.; break
            else:
                v1,v2 = (lower-offset)/slope,(upper-offset)/slope
                lo,hi = max(lo,min(v1,v2)),min(hi,max(v1,v2))
        if hi > lo: cuts.append((lo,hi))
    return [(lo,hi) for lo,hi in cuts if hi>lo]


def module_count(a, b):
    """Anchor-independent count from the original geographic edge, not its ENU projection."""
    lon1,lat1,lon2,lat2=map(math.radians,(*a,*b))
    h=math.sin((lat2-lat1)/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return max(1,round(12742000*math.asin(min(1.,math.sqrt(h)))/LENGTH))


def ring_phase(geo):
    radians=np.radians(geo)
    d=np.diff(radians,axis=0)
    h=np.sin(d[:,1]/2)**2+np.cos(radians[:-1,1])*np.cos(radians[1:,1])*np.sin(d[:,0]/2)**2
    lengths=12742000*np.arcsin(np.minimum(1.,np.sqrt(h)))
    arc=np.r_[0.,np.cumsum(lengths)]
    return arc,float(arc[-1])/max(1,round(float(arc[-1])/LENGTH))


def ring_sections(a,b,offset,span,step,cuts,box):
    """A single phase for the complete ring; corners split modules without restarting spacing."""
    delta=b-a
    length=float(np.linalg.norm(delta))
    if length<1e-6 or span<1e-6: return
    direction=delta/length
    x0,y0,x1,y1=box
    corners=np.array([[x0-5,y0-5],[x0-5,y1+5],[x1+5,y0-5],[x1+5,y1+5]])
    projected=(corners-a)@direction/length*span+offset
    first=max(int(math.floor(offset/step)),int(math.floor(float(projected.min())/step))-1)
    last=min(int(math.ceil((offset+span)/step)),int(math.ceil(float(projected.max())/step))+1)
    for k in range(first,last):
        lo,hi=max(offset,k*step),min(offset+span,(k+1)*step)
        if hi<=lo: continue
        # Snap a grid tick very close to a real corner to that corner, preserving both adjacent edges.
        if hi<offset+span and hi-offset<.25: continue
        if lo>offset and offset+span-lo<.25: continue
        if 0<lo-offset<.25: lo=offset
        if 0<offset+span-hi<.25: hi=offset+span
        left,right=(lo-offset)/span*length,(hi-offset)/span*length
        for l,r in subtract([(left,right)],cuts):
            if r-l<.02: continue
            pa,pb=a+direction*l,a+direction*r
            centre=(pa+pb)/2
            owned=bool(box.owns(centre[0],centre[1])) if hasattr(box,'owns') else x0<=centre[0]<x1 and y0<=centre[1]<y1
            if owned: yield pa,pb


def clean_cuts(cuts,length):
    merged=[]
    for lo,hi in sorted((max(0.,lo),min(length,hi)) for lo,hi in cuts if hi>lo):
        if merged and lo-merged[-1][1]<.75:
            merged[-1]=(merged[-1][0],max(hi,merged[-1][1]))
        else: merged.append((lo,hi))
    if merged:
        if merged[0][0]<.75: merged[0]=(0.,merged[0][1])
        if length-merged[-1][1]<.75: merged[-1]=(merged[-1][0],length)
    return merged


def sections(a, b, cuts=(), box=None, count=None):
    """Distribute residuals within each real edge; no corner shortcuts; clip work, not geometry."""
    delta = b-a
    length = float(np.linalg.norm(delta))
    if length < 1e-5: return
    tangent = delta/length
    count = max(1,round(length/LENGTH)) if count is None else count
    step = length/count
    for lo,hi in [(0.,length)]:
        start,end = 0,count
        if box is not None:
            # Bound candidate indices by projecting the expanded tile's corners onto this edge.
            x0,y0,x1,y1 = box
            corners = np.array([[x0-5,y0-5],[x0-5,y1+5],[x1+5,y0-5],[x1+5,y1+5]])
            arc = (corners-a)@tangent
            start = max(0,int(math.floor((float(arc.min())-lo)/step)))
            end = min(count,int(math.ceil((float(arc.max())-lo)/step))+1)
        for i in range(start,end):
            for left,right in subtract([(i*step,(i+1)*step)],cuts):
                if right-left < .02: continue
                pa,pb = a+tangent*left,a+tangent*right
                centre = (pa+pb)/2
                if box is None or box[0]<=centre[0]<box[2] and box[1]<=centre[1]<box[3]:
                    yield pa,pb


def terrain_sections(frame, pending, elev, surface=None):
    """Adapt to curved/sloped grade, preserving end posts instead of applying a height lift."""
    approved,failures = [],{}
    for depth in range(6):
        if not pending: break
        xy = np.array([point for _,_,_,a,b in pending for point in (a,(a+b)/2,b)])
        lat,lon,_ = frame.enu_to_geodetic(xy[:,0],xy[:,1],np.zeros(len(xy)))
        if surface is not None:
            zz=np.asarray(surface(xy),float)
        else:
            heights = np.asarray(elev(lat,lon),float)
            if heights.shape != (len(xy),): raise ValueError('perimeter elevation shape mismatch')
            _,_,zz = frame.geodetic_to_enu(lat,lon,heights)
        if np.shape(zz)!=(len(xy),): raise ValueError('perimeter surface shape mismatch')
        next_level=[]
        for item,z in zip(pending,np.asarray(zz).reshape(-1,3)):
            fid,ri,ei,a,b = item
            if not np.isfinite(z).all():
                failures[fid]=failures.get(fid,0)+1
            elif abs(z[1]-(z[0]+z[2])/2) <= .05:
                approved.append((item,z))
            elif depth<5 and float(np.linalg.norm(b-a)) > .30:
                mid=(a+b)/2
                next_level.extend(((fid,ri,ei,a,mid),(fid,ri,ei,mid,b)))
            else:
                failures[fid]=failures.get(fid,0)+1
        pending=next_level
    return approved,failures


def rows(frame, area_fn, infra_fn, box, elev, roads=(), transport_fn=None, surface=None, diagnostics=None, building_fn=None, audit=None, selected_mapped=None, transport_geometry=(), extra_evidence=None, transport_meshes=(),routing_context=None,stage_report=None,exclusive_geometry_out=None):
    import fencecrossings as crossing
    if os.environ.get('BNE_PERIMETERS','1') == '0': return []
    if diagnostics is None and os.environ.get('BNE_PERIMETER_DEBUG_DIR'): diagnostics=[]
    x0,y0,x1,y1 = map(float,box)
    la,lo,_ = frame.enu_to_geodetic(np.array([x0-48,x1+48]),np.array([y0-48,y1+48]),np.zeros(2))
    keys = mapcells.cells_for_points(la,lo,pad_lat=.0001,pad_lon=.0001,max_cells=64)
    areas,seen,area_meta = [],set(),{}
    infrastructure=[];seen_infra=set();seen_transport_batches=set()
    protected=[]
    blockers,transport,seen_transport = [],[],set()
    # Fetch every intersecting cell, not only the cell owning the facility centre.
    for key in keys:
        for feats in area_fn(key):
            if feats is None: continue
            boxes = feats.boxes(frame)
            for i in range(len(feats)):
                fid = osm_id(feats,i)
                tags = tags_of(feats,i)
                cls = str(feats.attrs.get('class',['']*len(feats))[i]).lower()
                category = classify(tags,cls)
                if not fid or not category or feats.kind[i] != mapgeom.POLYGON or fid in seen: continue
                if feats.clipped is not None and feats.clipped[i]:
                    log.warning('perimeter %s skipped clipped geometry',fid); continue
                parts = feats.parts(i)
                if not parts or any(not part or not closed_ring(part[0]) for part in parts):
                    log.warning('perimeter %s skipped incomplete outer ring',fid); continue
                seen.add(fid)
                rings = []
                for ring in sorted((canonical_ring(part[0]) for part in parts),key=lambda r:(tuple(r[0]),len(r))):
                    e,n = mapgeom.enu_xy(feats,frame,ring)
                    rings.append((ring,np.column_stack((e,n))))
                areas.append((fid,category,rings))
                from shapely.geometry import Polygon
                from shapely.ops import unary_union
                area_meta[fid]=dict(tags=tags,name=str(feats.attrs.get('name',['']*len(feats))[i]),polygon_id=str(feats.ids[i]),category=category,
                    geometry_lonlat=unary_union([Polygon(part[0],part[1:]).buffer(0) for part in parts]))
        feats = infra_fn(key) if infra_fn else None
        if feats is not None and id(feats) not in seen_infra:
            token=id(feats)
            if token not in seen_infra:infrastructure.append(feats);seen_infra.add(token)
            for i in range(len(feats)):
                tags = tags_of(feats,i)
                cls = str(feats.attrs.get('class',['']*len(feats))[i]).lower()
                barrier = tags.get('barrier',cls)
                aeroway=tags.get('aeroway',cls)
                if aeroway in ('runway','taxiway'):
                    from shapely.geometry import Polygon,LineString
                    for part in feats.parts(i):
                        if not part or len(part[0])<2: continue
                        e,n=mapgeom.enu_xy(feats,frame,part[0]);xy=np.column_stack((e,n))
                        protected.append(Polygon(xy).buffer(0) if feats.kind[i]==mapgeom.POLYGON else
                                         LineString(xy).buffer(30. if aeroway=='runway' else 10.))
                if barrier not in ('gate','lift_gate','swing_gate','entrance','fence','wall') and not tags.get('entrance'): continue
                if barrier in ('fence','wall'):
                    # Physical fence topology is selected once below. It is not
                    # a proximity blocker competing with inferred perimeters.
                    continue
                for part in feats.parts(i):
                    if not part or not len(part[0]): continue
                    e,n = mapgeom.enu_xy(feats,frame,part[0]); xy = np.column_stack((e,n))
                    if len(xy)==1: blockers.append((xy[0],xy[0],3.))
                    else:
                        blockers.extend((a,b,.65) for a,b in zip(xy,xy[1:]))
        if transport_fn is not None:
            feats=transport_fn(key)
            if feats is None:raise ValueError('perimeter transport source unavailable for '+str(key))
            if id(feats) in seen_transport_batches:continue
            seen_transport_batches.add(id(feats))
            for corridor in crossing.corridors(frame,feats,tags_of,osm_id,bounds=(x0-48,y0-48,x1+48,y1+48)):
                identity=(corridor['osm_id'],tuple(corridor['a']),tuple(corridor['b']),corridor['grade'])
                if identity not in seen_transport:
                    seen_transport.add(identity);transport.append(corridor)
    import facilityperimeters as facilities
    import fencenetwork
    source_areas,source_meta=areas,area_meta
    network=fencenetwork.reconstruct(frame,areas,area_meta,infrastructure,extra_evidence,routing_context,
        debug=audit is not None or bool(os.environ.get('BNE_PERIMETER_DEBUG_DIR')))
    areas,area_meta,facility_audit,claimed=network.areas,network.metadata,list(network.facilities),network.claimed
    if stage_report is None:stage_report={}
    stage_report.update(network_digest=network.digest,geographic_paths=len(network.routes),geographic_status='validated_partial' if network.report['issues'] or network.report['unresolved_gaps'] else 'validated',placement_status='not_started')
    stage_report['authoritative_sources']=network.report.get('authoritative_sources',[])
    if any(r['source_status']!='available' for r in stage_report['authoritative_sources']):stage_report['geographic_status']='awaiting_authoritative_source'
    if audit is not None:audit['geographic_network']=network.report
    if selected_mapped is not None:selected_mapped.update(claimed)
    if exclusive_geometry_out is not None:
        exclusive_geometry_out.extend(record['authoritative_lonlat'] for record in area_meta.values() if record.get('authoritative'))
    ground_chunks=[dict(m='road_asphalt_2lane',w=float(road[1].width),n=np.c_[np.asarray(road[2])[:,:2],np.zeros(len(road[2]))],fid=str(road[0]))
                   for road in roads if len(road)>=3 and hasattr(road[1],'width')]
    for road in roads:
        if len(road) >= 3 and hasattr(road[1],'width'):
            pts,halfwidth = road[2],float(road[1].width)/2
        else:
            pts,halfwidth = road[0],float(road[1])
        pts = np.asarray(pts,float)[:,:2]
        # Generated carriageways supplement source roads, including real width
        # and altered final centreline, even when raw transport is available.
        if transport_fn is None or hasattr(road[1],'width'):
            transport.extend(dict(a=a[:2],b=b[:2],radius=max(2.,halfwidth)+.62,kind='road',osm_id=str(road[0]) if hasattr(road[1],'width') else 'unavailable',grade='ground')
                             for a,b in zip(pts,pts[1:]))
    edges = []
    for corridor in transport:
        c,d=corridor['a'],corridor['b']
        corridor['bounds']=(min(c[0],d[0]),min(c[1],d[1]),max(c[0],d[0]),max(c[1],d[1]))
    for fid,category,rings in sorted(areas):
        for ri,(geo,xy) in enumerate(rings):
            arc,step=ring_phase(geo)
            phases=area_meta[fid].get('ring_phases')
            if phases is not None:
                offset,step=phases[ri];arc=arc+offset
            for ei,(a,b) in enumerate(zip(xy,xy[1:])):
                if max(a[0],b[0])<x0-48 or min(a[0],b[0])>x1+48 or max(a[1],b[1])<y0-48 or min(a[1],b[1])>y1+48: continue
                edges.append((fid,ri,ei,a,b,(float(arc[ei]),float(arc[ei+1]-arc[ei]),step)))
    final_transport=crossing.SurfaceIndex(list(transport_geometry)+ground_chunks if edges else (),surface,transport_meshes if edges else (),
        rail_corridors=[c for c in transport if c['kind']=='railway'])
    if edges:protected.extend(final_transport.routing_obstacles(surface))
    building_index=None
    if building_fn is not None and edges:
        import fencebuildings
        building_index=fencebuildings.Index(building_fn())
    initial_cuts={};extra_edges=[];selection_sections_removed=0;selection_length_removed=0.
    original_edges=list(edges)
    for fid,ri,ei,a,b,phase in original_edges:
        length=float(np.linalg.norm(b-a))
        if length<1e-6: continue
        tangent=(b-a)/length
        cuts=facilities.intervals(a,b,area_meta[fid]['allowed'])
        if audit is not None or os.environ.get('BNE_PERIMETER_DIAGNOSTICS')=='1':
            selection_cuts=clean_cuts(cuts,length)
            selection_length_removed+=sum(hi-lo for lo,hi in selection_cuts)
            selection_sections_removed+=sum(1 for _ in ring_sections(a,b,*phase,[],box))-sum(1 for _ in ring_sections(a,b,*phase,selection_cuts,box))
        other_edges=[]
        if building_index is not None and building_index.records:
            other_edges=[(c,d) for other,rj,ej,c,d,_ in original_edges if (other,rj,ej)!=(fid,ri,ei)]
            other_edges.extend((c,d) for _,_,_,c,d,_ in extra_edges)
        paths=[]
        if building_index is not None:
            for lo,hi in subtract([(0.,length)],clean_cuts(cuts,length)):
                before=len(building_index.events)
                building_cuts,detours=building_index.resolve(a+tangent*lo,a+tangent*hi,transport,blockers,other_edges,protected)
                cuts.extend((lo+l,lo+r) for l,r in building_cuts)
                if not area_meta[fid].get('authoritative'):paths.extend((lo+l,route) for l,r,route in detours)
                elif building_cuts:
                    stage_report['authoritative_building_omissions']=stage_report.get('authoritative_building_omissions',0)+len(building_cuts)
                    for event in building_index.events[before:]:event['authoritative_resolution']='placement omitted; source alignment retained; detour disabled'
                for event in building_index.events[before:]: event.update(facility=fid,ring_id=ri,source_edge=ei)
        initial_cuts[(fid,ri,ei)]=clean_cuts(cuts,length)
        for offset,route in paths:
            cumulative=phase[0]+offset/length*phase[1]
            for c,d in zip(route,route[1:]):
                span=float(np.linalg.norm(d-c))
                if span<.02: continue
                lat,lon,_=frame.enu_to_geodetic(np.array([c[0],d[0]]),np.array([c[1],d[1]]),np.zeros(2))
                token=repr((fid,ri,ei,tuple(np.round(np.column_stack((lon,lat)).ravel(),7))))
                virtual=1000000000+int(hashlib.sha1(token.encode()).hexdigest()[:12],16)
                extra_edges.append((fid,ri,virtual,c,d,(cumulative,span,phase[2])))
                cumulative+=span
    edges.extend(extra_edges)
    if os.environ.get('BNE_PERIMETER_DIAGNOSTICS')=='1' or audit is not None:
        for record in facility_audit:log.info('perimeter facility selection %s',json.dumps(record,sort_keys=True))
        if building_index is not None:
            for event in building_index.events:
                event.update(frame_latitude=frame.lat0,frame_longitude=frame.lon0,run_id=event['facility']+'/outer/'+str(event['ring_id']))
                la,lo,_=frame.enu_to_geodetic(*event['intersection_xy'],0.)
                event.update(intersection_latitude=float(la),intersection_longitude=float(lo))
                event['segment_ids']=[]
                for source,rj,ej,c,d,phase in original_edges:
                    if (source,rj,ej)!=(event['facility'],event['ring_id'],event['source_edge']):continue
                    vector=d-c;divisor=float(vector@vector)
                    for pa,pb in ring_sections(c,d,*phase,[],box):
                        if not building_index.conflicts(pa,pb):continue
                        ident=(VERSION,source,rj,ej,round(float((pa-c)@vector)/divisor,8),round(float((pb-c)@vector)/divisor,8))
                        event['segment_ids'].append(hashlib.sha1(repr(ident).encode()).hexdigest()[:20])
                log.info('perimeter building conflict %s',json.dumps(event,sort_keys=True))
    pending = []
    for fid,ri,ei,a,b,count in edges:
        length = float(np.linalg.norm(b-a))
        if length<1e-5: continue
        tangent = (b-a)/length
        cuts = list(initial_cuts.get((fid,ri,ei),()))
        events=[]
        bounds=(min(a[0],b[0]),min(a[1],b[1]),max(a[0],b[0]),max(a[1],b[1]))
        for corridor in transport:
            cb=corridor['bounds']
            if cb[2]<bounds[0]-1e-6 or cb[0]>bounds[2]+1e-6 or cb[3]<bounds[1]-1e-6 or cb[1]>bounds[3]+1e-6: continue
            hit=crossing.intersection(a,b,corridor['a'],corridor['b'])
            if hit is None: continue
            point=a+hit[0]*(b-a)
            fence_z=float(np.asarray(surface(np.array([point])))[0]) if surface is not None and len(corridor['a'])>=3 else None
            if not crossing.at_grade(corridor,hit[1],fence_z): continue
            intervals=clean_cuts(capsule(a,tangent,length,corridor['a'][:2],corridor['b'][:2],corridor['radius']),length)
            cuts.extend(intervals)
            events.extend((lo,hi,corridor,point) for lo,hi in intervals)
        for c,d,radius in blockers:
            # Broad phase before analytic capsule intersection.
            if max(c[0],d[0])+radius<min(a[0],b[0]) or min(c[0],d[0])-radius>max(a[0],b[0]) or max(c[1],d[1])+radius<min(a[1],b[1]) or min(c[1],d[1])-radius>max(a[1],b[1]): continue
            cuts.extend(capsule(a,tangent,length,c,d,radius))
        cuts=clean_cuts(cuts,length)
        if events and (diagnostics is not None or os.environ.get('BNE_PERIMETER_DIAGNOSTICS')=='1'):
            logged=set()
            for pa,pb in ring_sections(a,b,*count,[],box):
                left,right=float((pa-a)@tangent),float((pb-a)@tangent)
                for lo,hi,corridor,point in events:
                    if hi<=left or lo>=right: continue
                    la,ln,_=frame.enu_to_geodetic(point[0],point[1],0.)
                    segment=hashlib.sha1(repr((VERSION,fid,ri,ei,round(left/length,8),round(right/length,8))).encode()).hexdigest()[:20]
                    record=dict(facility=fid,segment_id=segment,transport_osm_id=corridor['osm_id'],
                        longitude=float(ln),latitude=float(la),opening_m=hi-lo,reason=corridor['kind']+'_crossing',at_grade=True)
                    removed=np.array([a+tangent*max(left,lo),a+tangent*min(right,hi)])
                    rlat,rlon,_=frame.enu_to_geodetic(removed[:,0],removed[:,1],np.zeros(2))
                    record['omitted_lonlat']=np.column_stack((rlon,rlat)).tolist()
                    event_key=(segment,corridor['osm_id'],round(float(ln),7),round(float(la),7))
                    if event_key in logged: continue
                    logged.add(event_key)
                    if diagnostics is not None: diagnostics.append(record)
                    if os.environ.get('BNE_PERIMETER_DIAGNOSTICS')=='1': log.info('perimeter omission %s',json.dumps(record,sort_keys=True))
        for pa,pb in ring_sections(a,b,*count,cuts,box):
            pending.append((fid,ri,ei,pa,pb))
    stage_report.update(planned_sections=len(pending),generated_sections=0,placement_status='no_sections_in_tile' if not pending else 'pending')
    if not pending and audit is None and not os.environ.get('BNE_PERIMETER_DEBUG_DIR'): return []
    approved,failures = terrain_sections(frame,pending,elev,surface)
    result,counts = [],{}
    def final_omission(reason,fid,stable,a,b,**details):
        omitted=stage_report.setdefault('clearance_omissions',{})
        omitted[reason]=omitted.get(reason,0)+1
        if diagnostics is None and audit is None and os.environ.get('BNE_PERIMETER_DIAGNOSTICS')!='1':return
        la,lo,_=frame.enu_to_geodetic(np.array([a[0],b[0]]),np.array([a[1],b[1]]),np.zeros(2))
        record=dict(reason=reason,fence_source=fid,segment_id=stable,latitude=float(la.mean()),longitude=float(lo.mean()),
            omitted_lonlat=np.c_[lo,la].tolist(),**details)
        if diagnostics is not None:diagnostics.append(record)
        if audit is not None:audit.setdefault('final_clearance_omissions',[]).append(record)
        if os.environ.get('BNE_PERIMETER_DIAGNOSTICS')=='1':log.info('perimeter final omission %s',json.dumps(record,sort_keys=True))
    edge_lookup={(fid,ri,ei):(a,b) for fid,ri,ei,a,b,_ in edges}
    for (fid,ri,ei,a,b),z in approved:
        # The stock origin is grade level: its concrete feet deliberately extend below z=0.
        # Original edge fractions are invariant under frame changes, unlike rounded ENU points.
        start,end=edge_lookup[(fid,ri,ei)]
        delta=end-start
        divisor=float(delta@delta)
        identity = (VERSION,fid,ri,ei,round(float((a-start)@delta)/divisor,8),round(float((b-start)@delta)/divisor,8))
        stable = hashlib.sha1(repr(identity).encode()).hexdigest()[:20]
        if building_index is not None and building_index.conflicts(a,b,float(z[0]),float(z[2])):
            final_omission('final_mesh_clearance',fid,stable,a,b,
                building_ids=[building_index.records[i]['source']['id'] for i in building_index.conflicts(a,b,float(z[0]),float(z[2]))])
            continue
        conflicts=final_transport.conflicts(a,b,float(z[0]),float(z[2]))
        if conflicts:
            final_omission('final_transport_envelope',fid,stable,a,b,transport_ids=sorted({c['source'] for c in conflicts}))
            if audit is not None or os.environ.get('BNE_PERIMETER_DIAGNOSTICS')=='1':
                record=dict(fence_source=fid,segment_id=stable,reason='final_transport_envelope',
                    transport_ids=sorted({c['source'] for c in conflicts}),grades=sorted({c['grade'] for c in conflicts}),at_grade=True)
                if audit is not None:audit.setdefault('final_transport_omissions',[]).append(record)
                log.info('perimeter final transport omission %s',json.dumps(record,sort_keys=True))
            continue
        row=['perimeter',float(a[0]),float(a[1]),float(z[0]),float(b[0]),float(b[1]),float(z[2]),stable,fid]
        if os.environ.get('BNE_PERIMETER_DIAGNOSTICS')=='1' or os.environ.get('BNE_PERIMETER_DEBUG_DIR') or audit is not None:
            row.extend([fid+'/outer/'+str(ri),ri,ei,area_meta[fid]['polygon_id'],area_meta[fid]['category'],area_meta[fid]['boundary_class']])
        result.append(row)
        counts[fid] = counts.get(fid,0)+1
    if audit is not None:
        import shapely
        from shapely.geometry import LineString,Point,box as shape_box
        view=shape_box(*box)
        run_shapes={}
        for fid,kind,rings in areas:
            for ri,(geo,xy) in enumerate(rings): run_shapes[(fid,ri)]=(LineString(xy),geo,kind)
        runs=[]
        for facility in facility_audit:
            facility['generated_objects']=sum(1 for row in result if area_meta[row[8]]['facility_id']==facility['facility_id'])
            facility['instantiated_objects']=None # only the game registry can establish this
            facility['tile_ownership']=dict(bounds=list(box),frame=[frame.lat0,frame.lon0],rule='geographic-owner or half-open tile centre')
            for decision in facility['source_decisions']:
                decision['generated_objects']=sum(row[8]=='physical:'+decision['osm_id'] for row in result)
        for (fid,ri),(line,geo,kind) in run_shapes.items():
            local=line.intersection(view)
            if local.is_empty:continue
            neighbours=[]
            for (other,rj),(other_line,_,_) in run_shapes.items():
                if (other,rj)==(fid,ri):continue
                distances=[local.interpolate(float(s)).distance(other_line) for s in np.linspace(0,local.length,21)]
                neighbours.append(dict(source=other,ring_id=rj,minimum_m=min(distances),median_m=float(np.median(distances)),
                    same_boundary_inferred=False,same_facility=area_meta[fid]['facility_id']==area_meta[other]['facility_id']))
            segments=[]
            for row in result:
                if row[8]!=fid:continue
                # New rows carry ring diagnostics when auditing; normal production rows stay compact.
                if len(row)>10 and row[10]!=ri:continue
                la,lo,_=frame.enu_to_geodetic(np.array([row[1],row[4]]),np.array([row[2],row[5]]),np.zeros(2))
                segments.append(dict(id=row[7],coordinates=np.column_stack((lo,la)).tolist()))
            runs.append(dict(osm_id=fid,classification=kind,polygon_id=area_meta[fid]['polygon_id'],ring_id=ri,
                run_id=fid+'/outer/'+str(ri),facility_id=area_meta[fid]['facility_id'],source_ids=area_meta[fid]['source_ids'],
                confidence=area_meta[fid]['confidence'],original_coordinates=geo.tolist(),segments=segments,neighbours=neighbours))
            runs[-1].update(boundary_class=area_meta[fid]['boundary_class'],selected_geometry=json.loads(shapely.to_geojson(area_meta[fid]['allowed_lonlat'])))
        audit.update(version=VERSION,frame_latitude=frame.lat0,frame_longitude=frame.lon0,runs=runs,
            original_runs=sum(not LineString(xy).intersection(view).is_empty for _,_,rings in source_areas for _,xy in rings),
            original_runs_context=sum(len(rings) for _,_,rings in source_areas),selected_runs=len(runs),selected_runs_context=sum(len(rings) for _,_,rings in areas),facilities=facility_audit,
            source_polygons=[dict(osm_id=fid,tags=source_meta[fid]['tags'],name=source_meta[fid]['name'],polygon_id=source_meta[fid]['polygon_id'],
                original_geometry=json.loads(shapely.to_geojson(source_meta[fid]['geometry_lonlat'])),
                facility_id=next((f['facility_id'] for f in facility_audit if fid in f['source_ids']),None),
                reason='related sources reconciled before placement; redundant internal boundaries are not independent runs') for fid,_,_ in source_areas],
            selection_sections_removed=selection_sections_removed,selection_context_length_removed_m=selection_length_removed,
            building_conflicts=building_index.events if building_index is not None else [],generated=len(result))
    for fid,count in failures.items():
        log.warning('perimeter %s terrain invalid/nonlinear; %d sections omitted',fid,count)
    if failures:
        # A transient absent/nonlinear terrain sample must not turn into a
        # persistent, successful tile with an empty perimeter on later visits.
        import revisit
        revisit.fault()
        if audit is not None:
            audit['placement_failures']=dict(failures)
            audit['placement_status']='geometry-processing/terrain failure; tile not cacheable'
    stage_report.update(generated_sections=len(result),terrain_failures=dict(failures),placement_status='terrain_failure' if failures else 'clearance_omissions' if stage_report.get('clearance_omissions') else 'complete')
    if audit is not None:audit['placement_report']=dict(stage_report,pass_number=2,engine_instantiation='unverified')
    debug_dir=os.environ.get('BNE_PERIMETER_DEBUG_DIR')
    if debug_dir:
        from pathlib import Path
        import shapely
        from shapely.geometry import mapping,shape,LineString
        features=[]
        for fid,category,rings in source_areas:
            features.extend(dict(type='Feature',properties=dict(layer='source_polygon',osm_id=fid,category=category),
                geometry=dict(type='LineString',coordinates=geo.tolist())) for geo,_ in rings)
        for facility in facility_audit:
            local_frame=geodesy.EnuFrame(*facility['validation_frame'],0.)
            def route_geo(geometry):
                def convert(coords):
                    la,lo,_=local_frame.enu_to_geodetic(coords[:,0],coords[:,1],np.zeros(len(coords)))
                    return np.c_[lo,la]
                return mapping(shapely.transform(shape(geometry),convert))
            features.append(dict(type='Feature',properties=dict(layer='facility_interior',facility_id=facility['facility_id']),geometry=facility['interior_lonlat']))
            for candidate in facility.get('routing',{}).get('candidates',[]):
                features.append(dict(type='Feature',properties=dict({k:v for k,v in candidate.items() if k!='geometry_xy'},layer='route_candidate',facility_id=facility['facility_id']),geometry=route_geo(candidate['geometry_xy'])))
            for layer,key in (('selected_route','selected'),('road_centerline','roads')):
                for record in facility.get('routing',{}).get(key,[]):
                    if 'geometry_xy' in record:
                        features.append(dict(type='Feature',properties=dict({k:v for k,v in record.items() if k!='geometry_xy'},layer=layer,facility_id=facility['facility_id']),geometry=route_geo(record['geometry_xy'])))
            features.append(dict(type='Feature',properties=dict(layer='mapped_openings',facility_id=facility['facility_id']),geometry=facility['opening_masks_lonlat']))
            for proposal in facility.get('routing',{}).get('proposals',[]):
                if 'geometry_lonlat' in proposal:
                    features.append(dict(type='Feature',properties=dict({k:v for k,v in proposal.items() if k!='geometry_lonlat'},layer='proposal_review_only',facility_id=facility['facility_id']),geometry=proposal['geometry_lonlat']))
            for gap in facility.get('routing',{}).get('gaps',[]):
                if 'geometry_xy' in gap:
                    features.append(dict(type='Feature',properties=dict(layer='unresolved_route',reason=gap['reason'],facility_id=facility['facility_id']),geometry=route_geo(gap['geometry_xy'])))
            merged=shape(facility['union_lonlat'])
            raw=shapely.union_all([shapely.set_precision(LineString(geo),facilities.GRID)
                for fid,_,rings in source_areas if fid in facility['source_ids'] for geo,_ in rings])
            features.append(dict(type='Feature',properties=dict(layer='suppressed_internal',facility_id=facility['facility_id']),
                geometry=mapping(raw.difference(merged.boundary))))
            for layer,key in (('expected_external','expected_external_lonlat'),('selected_centerline','selected_external_lonlat'),('unexpected_missing','unexpected_missing_lonlat')):
                features.append(dict(type='Feature',properties=dict(layer=layer,facility_id=facility['facility_id'],status=facility['completeness']['status']),
                    geometry=facility['completeness'][key]))
            for decision in facility['source_decisions']:
                features.append(dict(type='Feature',properties=dict({k:v for k,v in decision.items() if k not in ('original_lonlat','selected_lonlat')},layer='physical_source_decision'),geometry=decision['original_lonlat']))
        for fid,category,rings in areas:
            features.extend(dict(type='Feature',properties=dict(layer='boundary',osm_id=fid,category=category,confidence=area_meta[fid]['confidence']),
                geometry=dict(type='LineString',coordinates=geo.tolist())) for geo,_ in rings)
        for row in result:
            lat,lon,_=frame.enu_to_geodetic(np.array([row[1],row[4]]),np.array([row[2],row[5]]),np.zeros(2))
            features.append(dict(type='Feature',properties=dict(layer='generated',segment_id=row[7],osm_id=row[8]),
                geometry=dict(type='LineString',coordinates=np.column_stack((lon,lat)).tolist())))
        if building_index is not None:
            import shapely
            from shapely.geometry import mapping
            def geographic(coords):
                la,lo,_=frame.enu_to_geodetic(coords[:,0],coords[:,1],np.zeros(len(coords)))
                return np.c_[lo,la]
            for record in building_index.records:
                features.append(dict(type='Feature',properties=dict(layer='final_building',source=record['source'],transform=record['transform']),
                    geometry=mapping(shapely.transform(record['geometry'],geographic))))
        for record in diagnostics or []:
            features.append(dict(type='Feature',properties=dict(record,layer='opening'),
                geometry=dict(type='Point',coordinates=[record['longitude'],record['latitude']])))
            features.append(dict(type='Feature',properties=dict(record,layer='omitted'),
                geometry=dict(type='LineString',coordinates=record['omitted_lonlat'])))
        target=Path(debug_dir);target.mkdir(parents=True,exist_ok=True)
        name=hashlib.sha1(repr((frame.lat0,frame.lon0,box)).encode()).hexdigest()[:16]
        (target/(name+'.geojson')).write_text(json.dumps(dict(type='FeatureCollection',features=features)),encoding='utf-8')
    if os.environ.get('BNE_PERIMETER_DIAGNOSTICS') == '1':
        for fid,category,rings in sorted(areas):
            perimeter = sum(float(np.linalg.norm(np.diff(xy,axis=0),axis=1).sum()) for _,xy in rings)
            planned = sum(1 for item in pending if item[0]==fid)
            log.info('perimeter %s detected=1 boundary=reconstructed class=%s geometry=%s rings=%d vertices=%d perimeter_m=%.1f planned_local=%d generated=%d '
                     'queued_for_tile=%d instantiated=unknown rendering=UNVERIFIED model=%s terrain=%s '
                     'owner=half-open-parent-centre box=%s streaming=road-tile cache_version=%d',fid,category,
                     'MultiPolygon' if len(rings)>1 else 'Polygon',len(rings),
                     sum(len(g)-1 for g,_ in rings),perimeter,planned,counts.get(fid,0),counts.get(fid,0),MODEL,
                     'visible-triangulated' if surface is not None else 'graded-fallback',box,VERSION)
    return result


def tile_rows(frame, md, infra_fn, box, elev, roads, surface=None, building_fn=None, selected_mapped=None, transport_geometry=(), transport_meshes=(),strict=False,status_out=None,exclusive_geometry_out=None):
    if os.environ.get('BNE_PERIMETERS','1') == '0': return []
    stages={}
    try:
        import facilityperimeters as facilities
        context=facilities.context(md,frame,box,LAYERS)
        infrastructure=facilities.combine([context['infrastructure'],context['land_use']],'infrastructure')
        claimed=set()
        result=rows(frame,lambda key:[context[layer] for layer in LAYERS],lambda key:infrastructure,box,elev,roads,
                    lambda key:md.cell(key,layers=('roads',),kind='demand').layers.get('roads'),surface,building_fn=building_fn,
                    selected_mapped=claimed,transport_geometry=transport_geometry,transport_meshes=transport_meshes,
                    routing_context=dict(roads=context['roads']),stage_report=stages,exclusive_geometry_out=exclusive_geometry_out)
        if stages.get('terrain_failures') and strict:
            raise PerimeterPending('pass 2 terrain placement failed: '+json.dumps(stages,sort_keys=True))
        if strict and stages.get('planned_sections',0)>0 and not result and stages.get('clearance_omissions'):
            raise PerimeterPending('pass 1 route exists but pass 2 rejected every mesh: '+json.dumps(stages,sort_keys=True))
        if selected_mapped is not None:selected_mapped.update(claimed)
        if status_out is not None:status_out.update(stages,version=VERSION)
        return result
    except Exception as ex:
        # Keep existing road/terrain generation usable, but do not cache the missing
        # optional geometry as a successful complete road tile.
        import revisit
        revisit.fault()
        phase='mesh_placement' if stages.get('network_digest') else 'source_context_or_reconstruction'
        if status_out is not None:status_out.update(stages,failure_phase=phase,error=str(ex),version=VERSION)
        log.warning('perimeters unavailable phase=%s: %s: %s',phase,type(ex).__name__,ex)
        if strict:raise PerimeterPending(phase+' incomplete; retry tile: '+str(ex)) from ex
        return []
