"""Select facility centreline topology before stock fence modules are considered.

Land use is evidence of a facility, not proof of a physical security boundary.
All topology is computed in source longitude/latitude on a fixed precision grid;
metric decisions use a deterministic facility frame, never the requesting tile.
"""
from collections import OrderedDict
import hashlib
import copy
import json
import logging
import os
from pathlib import Path
import threading

import numpy as np
import shapely
from shapely.geometry import Polygon,LineString,shape
from shapely.ops import unary_union,substring
from shapely.strtree import STRtree
import geodesy
import perimeterevidence
import mapcells
import mapgeom

log=logging.getLogger('earthd.facilityperimeters')
EVIDENCE=Path(__file__).with_name('perimeter_evidence.geojson')
CONSTRAINTS=Path(__file__).with_name('perimeter_constraints.geojson')
GRID=1e-9
_lock=threading.RLock()
_contexts=OrderedDict()
_evidence_cache=[]
_selections=OrderedDict()
_selection_jobs={}


def evidence_paths():
    return perimeterevidence.paths(EVIDENCE, CONSTRAINTS)


def evidence_signature():
    return perimeterevidence.signature(EVIDENCE, CONSTRAINTS)


def evidence():
    signature=evidence_signature()
    with _lock:
        if _evidence_cache and _evidence_cache[0][0]==signature:return _evidence_cache[0][1]
    result=[]
    for path in evidence_paths():
        if not path.exists() and path==EVIDENCE:continue
        document=json.loads(path.read_text(encoding='utf-8'))
        if document.get('type')!='FeatureCollection':raise ValueError('perimeter overrides must be a GeoJSON FeatureCollection')
        for feature in document['features']:
            props=feature['properties']
            if not props.get('id') or not props.get('source') or not props.get('reason'):
                raise ValueError('perimeter evidence requires id, source and reason')
            geometry=shape(feature['geometry'])
            if geometry.is_empty or not geometry.is_valid:raise ValueError('invalid perimeter evidence '+props['id'])
            if props.get('role') not in ('physical_fence','public_exclusion','mapped_opening','perimeter_override','boundary_constraint'):
                raise ValueError('unknown perimeter evidence role')
            if geometry.geom_type not in ('LineString','MultiLineString','Polygon','MultiPolygon') and not (props.get('role')=='mapped_opening' and geometry.geom_type=='Point'):
                raise ValueError('perimeter evidence must be line or polygon geometry')
            if props['role'] in ('public_exclusion','perimeter_override','boundary_constraint') and not props.get('facility_sources'):
                raise ValueError('exclusions and overrides require facility_sources')
            if props['role']=='perimeter_override' and not props.get('confidence')=='verified':
                raise ValueError('replacement perimeter must explicitly declare verified confidence')
            if props['role']=='boundary_constraint' and (props.get('confidence')!='verified' or props.get('keep_side') not in ('north','south','east','west') or geometry.geom_type!='LineString'):
                raise ValueError('boundary constraint requires verified directional road line')
            result.append(dict(geometry=geometry,properties=props))
    with _lock:_evidence_cache[:]=[(signature,result)]
    return result


def administrative(meta):
    t=meta['tags']
    return t.get('type')=='boundary' or t.get('boundary') in ('administrative','border_zone','protected_area')


def identity(meta):
    t=meta['tags']
    name=' '.join((meta.get('name') or t.get('name','')).casefold().split())
    return name,t.get('wikidata'),t.get('wikipedia')


def distinct(a,b):
    ta,tb=a['tags'],b['tags']
    if any(ta.get(k) and tb.get(k) and ta[k]!=tb[k] for k in ('operator','access')):return True
    # A heliport, depot or independently secured compound is not an administrative subdivision.
    if bool(ta.get('aeroway'))!=bool(tb.get('aeroway')):return True
    if any(ta.get(k) and tb.get(k) and ta[k]!=tb[k] for k in ('military','industrial')):return True
    return False


def secure(meta):
    t=meta['tags']
    return t.get('barrier')=='fence' or t.get('fenced')=='yes' or (
        t.get('access') in ('private','no','military') and t.get('military') not in (None,''))


def related(a,b,sa,sb):
    ia,ib=identity(a),identity(b)
    same=any(x and x==y for x,y in zip(ia,ib))
    if a['category']!=b['category']:
        # The same installation can be classified twice (e.g. industrial and
        # military). Different roles are only coalesced when identity and almost
        # the entire geographic footprint agree; a nested heliport stays distinct.
        conflicting=any(a['tags'].get(k) and b['tags'].get(k) and a['tags'][k]!=b['tags'][k]
            for k in ('operator','access','military'))
        return same and not conflicting and sa.intersection(sb).area/max(sa.union(sb).area,1e-20)>.95
    if distinct(a,b):return False
    if min(sa.area,sb.area)<.9*max(sa.area,sb.area) and (secure(a) or secure(b)) and not (administrative(a) or administrative(b)):
        return False
    if ia[0] and ib[0] and ia[0]!=ib[0] and not (administrative(a) or administrative(b)):
        return False
    if same:return True
    # Duplicate geometry with no contradictory facility identity is safe to coalesce.
    overlap=sa.intersection(sb).area
    if not (ia[0] and ib[0] and ia[0]!=ib[0]) and not (secure(a) or secure(b)) and overlap/max(sa.union(sb).area,1e-20)>.95:return True
    # An unnamed non-secured land-use subdivision cannot establish an internal fence.
    small,large=(a,b) if sa.area<sb.area else (b,a)
    return not identity(small)[0] and not secure(small) and overlap/max(min(sa.area,sb.area),1e-20)>.995


def lines(geometry):
    if geometry.is_empty:return []
    if geometry.geom_type in ('LineString','LinearRing'):return [LineString(geometry)]
    return [line for part in getattr(geometry,'geoms',()) for line in lines(part)]


def polygons(geometry):
    if geometry.geom_type=='Polygon':return [geometry]
    return [p for g in getattr(geometry,'geoms',()) for p in polygons(g)]


def validate(facility_id,expected,selected,openings=(),internal=(),missing_source=None):
    """Pre-mesh completeness check. Diagnose gaps; never invent a connection.

    Inputs are metric source centreline geometries. Openings are explicit masks,
    not a requirement that every mapped facility be a closed physical fence.
    """
    permitted=unary_union(list(openings))
    missing=expected.difference(permitted).difference(selected.buffer(.005))
    duplicate=round(max(0.,sum(line.length for line in lines(selected))-unary_union(lines(selected)).length),6)
    unsupported=[record for record in internal if not record.get('supported')]
    return dict(facility_id=facility_id,expected_external_m=expected.length,selected_external_m=selected.length,
        explained_opening_m=expected.intersection(permitted).length,unexpected_missing_m=missing.length,
        unexpected_missing_xy=json.loads(shapely.to_geojson(missing)),duplicate_m=duplicate,
        unexplained_internal_ids=[r['id'] for r in unsupported],
        missing_source_reason=missing_source,status='missing_source' if missing_source else
            'implementation_gap' if missing.length>.02 or duplicate>.02 or unsupported else 'consistent')


def canonical(coords):
    points=[tuple(v) for v in np.round(np.asarray(coords),9)]
    if points[0]==points[-1]:
        points=points[:-1]
        i=min(range(len(points)),key=points.__getitem__)
        one=points[i:]+points[:i];two=[one[0]]+one[:0:-1]
        points=min(one,two);points.append(points[0])
    else:points=min(points,list(reversed(points)))
    return np.asarray(points,float)


def physical_chains(physical):
    """Join exact shared source endpoints, never proximity or interior yards.

    line_merge stops at branches. Closed enclosures are deliberately not new
    exterior candidates. The caller still requires both chain ends to anchor
    to the same facility exterior before treating the chain as perimeter.
    """
    pieces=[];sources=[]
    for fid,item in sorted(physical.items()):
        if item['properties']['role']!='physical_fence':continue
        for line in lines(item['geometry']):
            if not line.is_ring:pieces.append(line);sources.append(fid)
    if not pieces:return []
    tree=STRtree(pieces);result=[]
    for chain in lines(shapely.line_merge(shapely.MultiLineString(pieces))):
        if chain.is_ring:continue
        contributors=sorted(set(sources[int(i)] for i in tree.query(chain,predicate='intersects')
            if chain.intersection(pieces[int(i)]).length>1e-10))
        if len(contributors)<2:continue
        fingerprint=hashlib.sha1('|'.join(contributors).encode())
        fingerprint.update(LineString(canonical(chain.coords)).wkb)
        fid='network/'+fingerprint.hexdigest()[:16]
        result.append(dict(geometry=chain,properties=dict(id=fid,role='physical_fence',network_sources=contributors,
            outer_connected_to=sorted(set(parent for x in contributors for parent in physical[x]['properties'].get('outer_member_of',()))),
            source='; '.join(physical[x]['properties']['source'] for x in contributors),
            reason='unbranched mapped fence chain with identical shared source endpoints')))
    return result


def mapped_features(features):
    """Keep generic mapped fences, not just the subset drawn by the legacy forest system."""
    import perimeters as p
    found={}
    for feats in features:
        if feats is None:continue
        for i in range(len(feats)):
            tags=p.tags_of(feats,i)
            cls=str(feats.attrs.get('class',['']*len(feats))[i])
            if tags.get('barrier',cls)!='fence' or feats.kind[i] not in (mapgeom.LINE,mapgeom.POLYGON):continue
            fid=p.osm_id(feats,i) or str(feats.ids[i])
            if fid in found:continue
            geometry=unary_union([LineString(part[0]) for part in feats.parts(i) if part and len(part[0])>1])
            found[fid]=dict(geometry=geometry,properties=dict(id=fid,role='physical_fence',tags=tags,
                source='Overture infrastructure / OpenStreetMap',reason='mapped barrier=fence',legacy_id=str(feats.ids[i])))
    return list(found.values())


def public_features(features):
    """Only an explicit public access tag establishes a public-area exclusion."""
    import perimeters as p
    found={}
    for feats in features:
        if feats is None:continue
        for i in range(len(feats)):
            tags=p.tags_of(feats,i)
            if tags.get('access') not in ('yes','public') or feats.kind[i]!=mapgeom.POLYGON:continue
            if tags.get('amenity')!='parking' and tags.get('highway') not in ('pedestrian','footway'):continue
            fid=p.osm_id(feats,i) or str(feats.ids[i])
            found[fid]=dict(geometry=unary_union([Polygon(part[0],part[1:]).buffer(0) for part in feats.parts(i) if len(part[0])>=4]),
                properties=dict(id=fid,role='public_exclusion',source='Overture source tags',reason='explicit public access area'))
    return list(found.values())


def opening_features(features):
    import perimeters as p
    result={}
    for feats in features:
        if feats is None:continue
        for i in range(len(feats)):
            tags=p.tags_of(feats,i)
            if tags.get('barrier') not in ('gate','lift_gate','swing_gate','entrance') and not tags.get('entrance'):continue
            fid=p.osm_id(feats,i) or str(feats.ids[i])
            for part in feats.parts(i):
                if not part or not len(part[0]):continue
                coords=part[0]
                g=shapely.Point(coords[0]) if len(coords)==1 else LineString(coords)
                result[fid]=dict(geometry=g,properties=dict(id=fid,role='mapped_opening',source='Overture infrastructure / OpenStreetMap',reason='mapped gate or entrance',tags=tags))
    return list(result.values())


def road_context(frame,features,boundary,reach):
    import perimeters as p,fencecrossings
    records={};seen=set()
    bounds=boundary.buffer(reach+20.).bounds
    for feats in features:
        for corridor in fencecrossings.corridors(frame,feats,p.tags_of,p.osm_id,bounds):
            if corridor['kind']!='road' or corridor['grade']!='ground':continue
            line=LineString([corridor['a'][:2],corridor['b'][:2]])
            key=(corridor['osm_id'],line.wkb)
            if key in seen or line.distance(boundary)>reach+20.:continue
            seen.add(key)
            entry=records.setdefault(corridor['osm_id'],dict(radius=0.,lines=[]))
            entry['radius']=max(entry['radius'],corridor['radius']);entry['lines'].append(line)
    result=[]
    for fid,record in sorted(records.items()):
        geometry=shapely.line_merge(unary_union(record['lines']))
        for line in lines(geometry):result.append(dict(id=fid,geometry=line,radius=record['radius']))
    return result


def constrain_interior(interior,records,convert):
    """Clip a main exterior against explicitly reviewed road-side evidence.

    Split by the actual offset road, extending its end tangents beyond the
    facility. No latitude rectangle, road-name heuristic or global side rule.
    """
    from shapely.ops import split
    selected=interior;reports=[];masks=[]
    directions=dict(north=np.array([0.,1.]),south=np.array([0.,-1.]),east=np.array([1.,0.]),west=np.array([-1.,0.]))
    for item in sorted(records,key=lambda x:x['properties']['id']):
        props=item['properties'];line=convert(item['geometry']);coords=np.asarray(line.coords)
        direction=directions[props['keep_side']]
        tangent=(coords[-1]-coords[0]);normal=np.array([-tangent[1],tangent[0]])
        signed=1. if normal@direction>0 else -1.
        distance=float(props.get('offset_m',6.))
        if not 0.<distance<=30.:raise ValueError('invalid boundary road clearance')
        offset=line.offset_curve(signed*distance,join_style=2)
        if offset.geom_type!='LineString':raise ValueError('ambiguous boundary road offset')
        points=np.asarray(offset.coords);span=max(interior.bounds[2]-interior.bounds[0],interior.bounds[3]-interior.bounds[1])*4+100.
        first=(points[1]-points[0]);first=first/np.linalg.norm(first)
        last=(points[-1]-points[-2]);last=last/np.linalg.norm(last)
        cutter=LineString(np.vstack((points[0]-first*span*4,points,points[-1]+last*span*4)))
        extent=shapely.box(*interior.buffer(span).bounds)
        halves=list(split(extent,cutter).geoms)
        if len(halves)!=2:raise ValueError('boundary constraint must define two unambiguous sides')
        middle=offset.interpolate(.5,normalized=True)
        probe=shapely.Point(np.asarray(middle.coords[0])+direction*max(1.,distance))
        keep=next((poly for poly in halves if poly.covers(probe)),None)
        if keep is None:raise ValueError('boundary constraint has no keep side')
        before=selected.area;selected=selected.intersection(keep);masks.append(keep)
        reports.append(dict(id=props['id'],source=props['source'],keep_side=props['keep_side'],offset_m=distance,
            removed_area_m2=before-selected.area,confidence='high',reason=props['reason']))
    return selected,unary_union(masks) if len(masks)==1 else shapely.intersection_all(masks) if masks else None,reports


def combine(features,layer):
    """Deduplicate complete source geometry from several cells without clipping."""
    unique={}
    for feats in features:
        for i,fid in enumerate(feats.ids):unique.setdefault(str(fid),(feats,i))
    ordered=sorted(unique)
    result=mapgeom.build(layer,np.asarray(ordered),[(int(unique[fid][0].kind[unique[fid][1]]),unique[fid][0].parts(unique[fid][1])) for fid in ordered])
    names=set().union(*(set(f.attrs) for f in features)) if features else set()
    for name in names:
        column=next(f.attrs[name] for f in features if name in f.attrs)
        default='' if column.dtype.kind in ('U','O','S') else False if column.dtype.kind=='b' else np.nan
        result.attrs[name]=np.asarray([f.attrs[name][i] if name in f.attrs else default for f,i in (unique[fid] for fid in ordered)])
    return result


def select(frame,areas,metadata,infrastructure=(),extra_evidence=None,routing_context=None,debug=False):
    """Return complete canonical runs plus allowed centreline geometry and development audit.

    The allowed geometry is selected before section generation. Ring phases still
    refer to complete exteriors, so holes/openings never restart module spacing.
    """
    import authoritativeperimeters as authoritative
    areas=authoritative.seed_area(frame,areas,metadata,infrastructure)
    ids=sorted(fid for fid,_,_ in areas)
    if not ids:return [],{},[],set()
    signature=hashlib.sha256()
    signature.update(authoritative.signature().encode())
    signature.update(repr([bool(authoritative.policy_for([fid],metadata)) for fid in ids]).encode())
    signature.update(str(bool(debug)).encode())
    import fencerouting
    routing_config=fencerouting.settings()
    signature.update(json.dumps(routing_config,sort_keys=True).encode())
    for feats in (routing_context or {}).get('roads',()):
        signature.update(feats.ids.tobytes());signature.update(feats.lonlat.tobytes())
        for array in (feats.kind,feats.part_off,feats.ring_off,feats.coord_off):signature.update(array.tobytes())
        for name,array in sorted(feats.attrs.items()):
            signature.update(name.encode());signature.update(array.tobytes())
        for name,rule in sorted(feats.rules.items()):
            signature.update(name.encode())
            for array in (rule.feature,rule.value,rule.start,rule.end):signature.update(array.tobytes())
    for fid,category,rings in sorted(areas):
        signature.update(repr((fid,category,metadata[fid].get('name'),sorted(metadata[fid]['tags'].items()))).encode())
        for geo,_ in rings:signature.update(np.asarray(geo).tobytes())
        if 'geometry_lonlat' in metadata[fid]:signature.update(metadata[fid]['geometry_lonlat'].wkb)
    for feats in infrastructure:
        signature.update(feats.ids.tobytes());signature.update(feats.lonlat.tobytes())
        for name in ('tags','class','name'):
            if name in feats.attrs:signature.update(feats.attrs[name].tobytes())
    if extra_evidence is None:signature.update(evidence_signature().encode())
    else:
        for record in extra_evidence:
            signature.update(record['geometry'].wkb);signature.update(json.dumps(record['properties'],sort_keys=True).encode())
    cache_key=signature.hexdigest()
    with _lock:cached=_selections.get(cache_key)
    if cached is not None:
        cached_areas,cached_meta,cached_audit,cached_claimed=cached
        if any(f['completeness']['status']=='implementation_gap' for f in cached_audit):
            import revisit
            revisit.fault() # an in-memory hit must not turn an incomplete build into a disk-cache success
        selected=[];meta={}
        def requested(xy):
            e,n,_=frame.geodetic_to_enu(xy[:,1],xy[:,0],np.zeros(len(xy)))
            return np.c_[e,n]
        for fid,category,rings in cached_areas:
            selected.append((fid,category,[(geo,requested(geo)) for geo in rings]))
            meta[fid]=dict(cached_meta[fid],allowed=shapely.transform(cached_meta[fid]['allowed_lonlat'],requested))
        return selected,meta,copy.deepcopy(cached_audit),set(cached_claimed)
    from concurrent.futures import Future
    with _lock:
        future=_selection_jobs.get(cache_key)
        owner=future is None
        if owner:future=Future();_selection_jobs[cache_key]=future
    if not owner:
        future.result()  # wait for this source solve, not the unrelated facilities
        return select(frame,areas,metadata,infrastructure,extra_evidence,routing_context,debug)
    try:
        result=_resolve_selection(frame,areas,metadata,infrastructure,extra_evidence,routing_context,debug,ids,routing_config,cache_key)
        future.set_result(None)
        return result
    except BaseException as error:
        future.set_exception(error)
        raise
    finally:
        with _lock:_selection_jobs.pop(cache_key,None)


def _resolve_selection(frame,areas,metadata,infrastructure,extra_evidence,routing_context,debug,ids,routing_config,cache_key):
    import fencerouting,authoritativeperimeters as authoritative
    area_by_id={fid:(category,rings) for fid,category,rings in areas}
    source_shapes={fid:shapely.set_precision(metadata[fid].get('geometry_lonlat',
        unary_union([Polygon(geo) for geo,_ in area_by_id[fid][1]])),GRID) for fid in ids}
    ids=[fid for fid in ids if not source_shapes[fid].is_empty and source_shapes[fid].area>0]
    if not ids:return [],{},[],set()
    for fid in ids:metadata[fid].setdefault('category',area_by_id[fid][0])
    parent={fid:fid for fid in ids}
    def root(fid):
        while parent[fid]!=fid:fid=parent[fid]
        return fid
    overlaps=[]
    tree=STRtree([source_shapes[fid] for fid in ids])
    for i,one in enumerate(ids):
        for j in sorted(int(v) for v in tree.query(source_shapes[one],predicate='intersects') if int(v)>i):
            two=ids[j];a,b=source_shapes[one],source_shapes[two]
            overlap=a.intersection(b).area
            # Touching related parcels may share a perimeter; unrelated ones remain separate.
            merge=related(metadata[one],metadata[two],a,b)
            overlaps.append(dict(sources=[one,two],overlap_fraction=overlap/max(min(a.area,b.area),1e-20),grouped=merge))
            if merge:
                x,y=sorted((root(one),root(two)));parent[y]=x
    groups={}
    for fid in ids:groups.setdefault(root(fid),[]).append(fid)
    physical=mapped_features(infrastructure)+public_features(infrastructure)+opening_features(infrastructure)
    physical.extend(evidence() if extra_evidence is None else extra_evidence)
    physical.extend(authoritative.openings())
    # Supplements win over provider copies by source identity, not by coordinates.
    physical={item['properties']['id']:item for item in physical}
    physical.update({item['properties']['id']:item for item in physical_chains(physical)})
    physical_ids=sorted(physical)
    physical_tree=STRtree([physical[fid]['geometry'] for fid in physical_ids])
    selected=[];meta={};audit=[];claimed=set();owned=shapely.GeometryCollection();authoritative_emitted=set()
    for members in sorted(groups.values(),key=lambda members:(authoritative.SOURCE_ID not in members,members)):
        owner=members[0]
        exclusive=authoritative.policy_for(members,metadata)
        auth_records,auth_report=authoritative.sources(infrastructure) if exclusive else ([],None)
        auth_paths,auth_topology=authoritative.chains(auth_records) if exclusive else ([],None)
        auth_by_id={p['id']:p for p in auth_paths}
        facility_id='facility:'+hashlib.sha1('|'.join(members).encode()).hexdigest()[:16]
        merged=shapely.union_all([source_shapes[fid] for fid in members],grid_size=GRID)
        bounds=merged.bounds
        local=geodesy.EnuFrame(round((bounds[1]+bounds[3])/2,7),round((bounds[0]+bounds[2])/2,7),0.)
        def metric(coords):
            e,n,_=local.geodetic_to_enu(coords[:,1],coords[:,0],np.zeros(len(coords)))
            return np.c_[e,n]
        def geo(coords):
            la,lo,_=local.enu_to_geodetic(coords[:,0],coords[:,1],np.zeros(len(coords)))
            return np.c_[lo,la]
        convert=lambda g:shapely.transform(g,metric)
        constraints=[] if exclusive else [item for item in physical.values() if item['properties']['role']=='boundary_constraint' and set(item['properties'].get('facility_sources',())).intersection(members)]
        metric_area,constraint_mask,constraint_audit=constrain_interior(convert(merged),constraints,convert)
        if exclusive:
            closed=[Polygon(p['coordinates']) for p in auth_paths if p['coordinates'][0]==p['coordinates'][-1] and len(p['coordinates'])>=4]
            if closed:metric_area=convert(unary_union(closed))
        if metric_area.is_empty:raise ValueError('reviewed boundary constraints removed entire facility')
        metric_boundary=unary_union([LineString(poly.exterior) for poly in polygons(metric_area)])
        direct=[source_shapes[fid] for fid in members if not administrative(metadata[fid])]
        # Administrative bounds may cover public land. They cannot extend the
        # inferred security perimeter where a direct parcel supplies no support.
        if any(administrative(metadata[fid]) for fid in members):
            support=convert(unary_union(direct)).buffer(2.) if direct else shapely.GeometryCollection()
            inferred=metric_boundary.intersection(support)
        else:inferred=metric_boundary
        declared_unfenced=any(metadata[fid]['tags'].get('fenced')=='no' and
            source_shapes[fid].intersection(merged).area/max(merged.area,1e-20)>.995 for fid in members)
        if declared_unfenced:inferred=shapely.GeometryCollection()
        # A distinct military subarea is an identity, not automatically another
        # security fence. Keep its mapped physical lines; infer an internal loop
        # only when explicit fencing/restricted compound tags support it.
        nested_parent=next((fid for fid in ids if fid not in members and area_by_id[fid][0]==area_by_id[owner][0] and
            source_shapes[fid].area>merged.area*1.01 and source_shapes[fid].intersection(merged).area/max(merged.area,1e-20)>.995),None)
        unsupported_internal=area_by_id[owner][0]=='military' and nested_parent is not None and not any(secure(metadata[fid]) for fid in members)
        if unsupported_internal:inferred=shapely.GeometryCollection()
        if exclusive:inferred=shapely.GeometryCollection()
        explicit=[];mapped_support=[];exclusions=[];overrides=[];decisions=[];outer_witnesses=[]
        merged_near=merged.buffer(.003)
        boundary_halo=metric_boundary.buffer(10.)
        metric_inside=metric_area.buffer(.1)
        secure_halos=[convert(source_shapes[fid]).boundary.buffer(10.) for fid in ids if fid not in members and secure(metadata[fid])]
        secure_tree=STRtree(secure_halos)
        candidates=sorted(physical_ids[int(i)] for i in physical_tree.query(merged_near,predicate='intersects'))
        for fid in candidates:
            item=physical[fid]
            props=item['properties'];g=item['geometry']
            targets=set(props.get('facility_sources',()))
            if targets and not targets.intersection(members):
                ancestor=props['role']!='perimeter_override' and any(target in source_shapes and
                    source_shapes[target].intersection(merged).area/max(merged.area,1e-20)>.995 for target in targets)
                if not ancestor:continue
            if not g.intersects(merged_near):continue
            mg=convert(g)
            role=props['role']
            if role=='boundary_constraint':continue
            if exclusive and role in ('public_exclusion','perimeter_override'):continue
            if role=='mapped_opening':
                # Same three metre point-gate clearance as the mesh stage.
                exclusions.append((fid,mg.buffer(3. if mg.geom_type=='Point' else .65)));continue
            if role=='physical_fence' and mg.geom_type in ('Polygon','MultiPolygon'):
                mg=unary_union([LineString(poly.exterior) for poly in polygons(mg)])
            if role=='public_exclusion':
                if mg.geom_type in ('LineString','MultiLineString'):
                    width=float(props.get('width_m',0.))
                    if not 0.<width<=100.:raise ValueError('public corridor requires width_m in (0,100]')
                    mg=mg.buffer(width/2,cap_style=2)
                exclusions.append((fid,mg));continue
            if role=='perimeter_override':
                if mg.geom_type in ('Polygon','MultiPolygon'):mg=unary_union([LineString(poly.exterior) for poly in polygons(mg)])
                overrides.append((fid,mg,props));continue
            segments=lines(mg)
            length=mg.length
            boundary_share=mg.intersection(boundary_halo).length/max(length,1e-9)
            internal=metric_inside.covers(mg)
            declared_compound=bool(props.get('enclosure_of')) or props.get('boundary_class')=='secured_compound' or (
                props.get('tags',{}).get('access') in ('private','no','military') and
                bool(props.get('tags',{}).get('military') or props.get('tags',{}).get('security')))
            if not targets and not (boundary_share>=.70 or internal or declared_compound):continue
            outer_member=bool(set(props.get('outer_member_of',())).intersection(members))
            boundary_attached=all(not line.is_ring and all(shapely.Point(endpoint).distance(metric_boundary)<=12.
                for endpoint in (line.coords[0],line.coords[-1])) for line in lines(mg))
            connected_outer=bool(set(props.get('outer_connected_to',())).intersection(members))
            if props.get('network_sources') and not (boundary_attached or connected_outer):continue
            supported_compound=declared_compound
            if not supported_compound:
                for candidate in secure_tree.query(mg,predicate='intersects'):
                    if mg.intersection(secure_halos[int(candidate)]).length/max(length,1e-9)>=.70:
                        supported_compound=True;break
            independent_enclosure=bool(props.get('enclosure_of')) or props.get('boundary_class')=='secured_compound'
            classification='secured_compound' if independent_enclosure else 'external_physical' if outer_member or connected_outer or boundary_share>=.70 or boundary_attached or props.get('boundary_class')=='external' else (
                'secured_compound' if supported_compound else 'internal_nonperimeter')
            decisions.append(dict(osm_id=fid,facility_id=facility_id,classification=classification,
                original_lonlat=json.loads(shapely.to_geojson(g)),
                reason='mapped exterior association' if classification=='external_physical' else
                    'mapped fence member of independently identified enclosure' if props.get('enclosure_of') else 'separately secured compound evidence' if supported_compound else
                    'containment alone does not establish a facility security perimeter',
                retained=classification!='internal_nonperimeter',nearby_source_polygons=members))
            if exclusive and classification=='external_physical':
                decisions[-1].update(retained=False,reason='main perimeter exclusively owned by confirmed OSM relation outer ways; no property/physical alternative inferred')
                continue
            if classification=='internal_nonperimeter':continue
            props=dict(props,boundary_class=classification)
            if classification=='external_physical':mapped_support.append(mg)
            if outer_member:outer_witnesses.append(fid)
            if fid in claimed:continue
            explicit.append((fid,mg,props));claimed.add(fid)
            claimed.update(props.get('network_sources',()))
        corroborated=bool(direct and outer_witnesses and len(members)>1 and any(identity(metadata[fid])[0] for fid in members))
        if corroborated and not exclusive and not unsupported_internal and not declared_unfenced:
            # Restore a named, reconciled facility exterior corroborated by
            # actual physical outer members. An administrative tag alone is not
            # sufficient, and nested source loops remain absent from this outline.
            inferred=metric_boundary
        if overrides:
            if len(overrides)>1:raise ValueError('multiple verified perimeter overrides for '+facility_id)
            inferred=shapely.GeometryCollection()
            fid,mg,props=overrides[0];explicit.append((fid,mg,props));claimed.add(fid)
        explicit_shape=unary_union([g for _,g,_ in explicit])
        excluded=unary_union([g for _,g in exclusions])
        primary=[(fid,g) for fid,g,props in explicit if props.get('boundary_class')=='external_physical']
        if constraint_mask is not None:primary=[(fid,g.intersection(constraint_mask)) for fid,g in primary if not g.intersection(constraint_mask).is_empty]
        road_records=road_context(local,(routing_context or {}).get('roads',()),metric_boundary,routing_config['association_m'])
        if exclusive:
            routed=[]
            routing_audit=dict(weights={},candidates=[],selected=[],gaps=[],side_switches=[],road_preferences={},source_coverage=[],authoritative=auth_report,source_topology=auth_topology)
            paths_to_emit=auth_paths if exclusive['key'] not in authoritative_emitted else []
            authoritative_emitted.add(exclusive['key'])
            for ri,path in enumerate(paths_to_emit):
                raw=LineString(path['coordinates']);g=convert(raw);fid=path['id']
                physical[fid]=dict(geometry=raw,properties=dict(id=fid,role='physical_fence',source='confirmed OSM relation outer ways',reason='exclusive manually mapped perimeter',network_sources=list(path['source_ids']),authoritative=True))
                claimed.update(path['source_ids']);routed.append(dict(source=fid,geometry=g,confidence='physical',ring=ri))
                routing_audit['source_coverage'].append(dict(ring=ri,expected_m=g.length,covered_m=g.length,unresolved_m=0.))
            if not auth_paths:log.warning('authoritative perimeter unavailable label=%r: main fence withheld; no polygon fallback',exclusive['label'])
            for endpoint in auth_topology['endpoints']:
                point=convert(shapely.Point(endpoint['coordinates']))
                if excluded.distance(point)>.01:log.warning('authoritative perimeter %s: %s',exclusive['label'],endpoint)
        elif not inferred.is_empty:
            routed,routing_audit=fencerouting.optimize(metric_area,inferred,primary,road_records,[excluded],owner=owner,config=routing_config)
        else:
            routed=[dict(source=fid,geometry=line,confidence='physical',ring=0) for fid,g in primary for line in lines(g)]
            routing_audit=dict(weights=routing_config['weights'],candidates=[],selected=[],gaps=[],side_switches=[],road_preferences={})
        # Retain independently mapped, genuinely distinct parallel physical
        # lines. They are not duplicate land-use inference or graph alternatives
        # with a road/building conflict.
        chosen_physical=[run for run in routed if run['confidence']=='physical']
        # An authoritative physical outer member can extend beyond the grouped
        # land-use outline (notably entrance recesses). Lack of a nearby graph
        # anchor cannot erase that mapping. Preserve its non-overlapping source
        # remainder, without fabricating a connector back to the inference.
        selected_physical=unary_union([r['geometry'] for r in chosen_physical])
        for fid,g,props in explicit:
            if props.get('boundary_class')!='external_physical':continue
            has_authoritative_evidence=bool(props.get('outer_member_of') or props.get('boundary_class')=='external' or props.get('network_sources'))
            if not has_authoritative_evidence:continue
            remainder=g.difference(selected_physical.buffer(.005,cap_style=2))
            if constraint_mask is not None:remainder=remainder.intersection(constraint_mask)
            if remainder.length>.01:
                if constraints and props.get('tags',{}).get('boundary')=='census' and not props.get('network_sources'):
                    # A reviewed physical-side constraint outranks an older
                    # census member that cannot anchor to the approved exterior.
                    # Keep it inspectable, not as a second internal stock fence.
                    routing_audit.setdefault('proposals',[]).append(dict(source=fid,confidence='medium',render=False,
                        reason='unanchored census-derived line conflicts with reviewed main-boundary reconstruction',
                        geometry_lonlat=json.loads(shapely.to_geojson(shapely.transform(remainder,geo)))))
                    continue
                routed.extend(dict(source=fid,geometry=line,confidence='physical',ring=20000+len(routed)) for line in lines(remainder))
                routing_audit.setdefault('unanchored_physical',[]).append(dict(source=fid,length_m=remainder.length,
                    reason='authoritative physical source retained; no invented connection to administrative exterior'))
                selected_physical=unary_union([selected_physical,remainder])
        for fid,g in primary:
            if any(run['source']==fid for run in chosen_physical):continue
            valid=any(c['source']==fid and not c['reason'].startswith('rejected') for c in routing_audit['candidates'])
            if valid and any(.005<g.distance(run['geometry']) and g.hausdorff_distance(run['geometry'])<=routing_config['association_m'] for run in chosen_physical):
                routed.extend(dict(source=fid,geometry=line,confidence='physical',ring=10000+len(routed)) for line in lines(g))
        if not debug:
            from collections import Counter
            routing_audit['candidate_counts']=dict(Counter(c['reason'] for c in routing_audit['candidates']))
            routing_audit['selected_count']=len(routing_audit['selected'])
            for key in ('candidates','selected','roads'):routing_audit.pop(key,None)
        expected=unary_union([run['geometry'] for run in routed])
        def emit(run_id,coords,allowed,confidence,source_ids,phases=None):
            rings=[]
            for coordinates in coords:
                e,n=mapgeom.enu_xy(None,frame,coordinates);rings.append((coordinates,np.c_[e,n]))
            def requested(xy):
                e,n,_=frame.geodetic_to_enu(xy[:,1],xy[:,0],np.zeros(len(xy)))
                return np.c_[e,n]
            selected.append((run_id,area_by_id[owner][0],rings))
            meta[run_id]=dict(metadata[owner],polygon_id=facility_id,facility_id=facility_id,source_ids=source_ids,
                confidence=confidence,allowed=shapely.transform(allowed,requested),allowed_lonlat=allowed,
                boundary_class='corroborated_exterior' if corroborated else 'inferred_exterior')
            if phases is not None:meta[run_id]['ring_phases']=phases
        import perimeters as perimeter_module
        merged_runs=[]
        for run in routed:
            if merged_runs and all((merged_runs[-1].get(k)==run.get(k)) for k in ('source','ring','render')) and (
                (merged_runs[-1]['confidence']=='physical')==(run['confidence']=='physical')) and (
                merged_runs[-1]['geometry'].coords[-1]==run['geometry'].coords[0]):
                merged_runs[-1]=dict(run,geometry=LineString(list(merged_runs[-1]['geometry'].coords)+list(run['geometry'].coords)[1:]))
            else:merged_runs.append(run)
        routed=merged_runs
        prepared=[];totals={}
        for run in routed:
            coordinates=np.asarray(auth_by_id[run['source']]['coordinates'],float) if exclusive else np.round(np.asarray(shapely.transform(run['geometry'].simplify(.001),geo).coords),9)
            arc,_=perimeter_module.ring_phase(coordinates);span=float(arc[-1]);ri=run['ring']
            at=totals.get(ri,0.);totals[ri]=at+span
            prepared.append((run,coordinates,at))
        groups_routed={}
        for run,coordinates,at in prepared:
            if run.get('render',True) is False:continue
            fid='physical:'+run['source'] if run['confidence']=='physical' else owner
            group=groups_routed.setdefault(fid,dict(coords=[],phases=[],geometries=[],physical=run['confidence']=='physical'))
            group['coords'].append(coordinates);group['phases'].append((at,totals[run['ring']]/max(1,round(totals[run['ring']]/4.))))
            group['geometries'].append(LineString(coordinates))
        for fid,group in groups_routed.items():
            allowed=unary_union(group['geometries'])
            # Exclusions remain masks, never new inferred roadside fences.
            allowed=allowed.difference(shapely.transform(excluded,geo))
            emit(fid,group['coords'],allowed,'explicit' if group['physical'] else 'inferred',
                physical[fid.removeprefix('physical:')]['properties'].get('network_sources',[fid.removeprefix('physical:')]) if group['physical'] else members,group['phases'])
            meta[fid]['boundary_class']='external_physical' if group['physical'] else 'optimized_exterior'
            if exclusive:
                meta[fid].update(boundary_class='authoritative_osm',authoritative=True,
                    authoritative_lonlat=unary_union(group['geometries']),authoritative_label=exclusive['label'])
        for fid,mg,props in explicit:
            if props.get('boundary_class')=='external_physical':continue
            permitted=mg.difference(excluded)
            geog=shapely.set_precision(shapely.transform(mg,geo),GRID)
            allowed=shapely.set_precision(shapely.transform(permitted,geo),GRID)
            emit('physical:'+fid,[canonical(line.coords) for line in lines(geog)],allowed,'explicit', [fid])
            meta['physical:'+fid]['enclosure_ids']=[x['id'] for x in props.get('enclosure_of',())]
            meta['physical:'+fid]['boundary_class']=props.get('boundary_class','verified_override')
            if props.get('role')=='perimeter_override':expected=unary_union([expected,mg])
        selected_external=unary_union([run['geometry'].difference(excluded) for run in routed if run.get('render',True)])
        completeness=validate(facility_id,expected,selected_external,[excluded],
            internal=[dict(id=fid,supported=props.get('boundary_class')=='secured_compound') for fid,_,props in explicit
                if props.get('boundary_class')!='external_physical' and props.get('role')!='perimeter_override'],
            missing_source=('authoritative OSM relation unavailable; main fence withheld' if not auth_paths else 'authoritative source has disconnected endpoints' if auth_topology['endpoints'] else None) if exclusive else 'routing constraints unresolved' if routing_audit['gaps'] else None if overrides or declared_unfenced else 'internal subarea has no supported fence loop' if unsupported_internal else
                'administrative exterior has no independent perimeter corroboration' if any(administrative(metadata[fid]) for fid in members) and not corroborated else None)
        completeness['expected_external_lonlat']=json.loads(shapely.to_geojson(shapely.transform(expected,geo)))
        completeness['selected_external_lonlat']=json.loads(shapely.to_geojson(shapely.transform(selected_external,geo)))
        completeness['unexpected_missing_lonlat']=json.loads(shapely.to_geojson(shapely.transform(shape(completeness['unexpected_missing_xy']),geo)))
        before=sum(convert(LineString(geo)).length for fid in members for geo,_ in area_by_id[fid][1])
        audit.append(dict(facility_id=facility_id,name=metadata[owner].get('name',''),source_ids=members,
            inferred_source_runs_before=sum(len(area_by_id[fid][1]) for fid in members),
            inferred_external_rings_after=len(polygons(merged)),
            overlapping_polygon_count=sum(1 for item in overlaps if set(item['sources']).issubset(members)),
            overlap_measurements=[v for v in overlaps if set(v['sources']).intersection(members)],
            source_area_m2={fid:convert(source_shapes[fid]).area for fid in members},
            union_area_m2=metric_area.area,external_boundary_m=metric_boundary.length,
            original_boundary_m=before,internal_boundary_suppressed_m=max(0.,before-metric_boundary.length),
            explicit_fence_ids=[fid for fid,_,_ in explicit],explicit_m=explicit_shape.length,
            inferred_m=inferred.length,public_exclusions=[dict(id=fid,area_m2=g.area) for fid,g in exclusions],
            unsupported_internal_inference=unsupported_internal,parent_source=nested_parent,
            source_decisions=decisions,corroborating_outer_members=outer_witnesses,completeness=completeness,
            routing=routing_audit,interior_lonlat=json.loads(shapely.to_geojson(shapely.transform(metric_area,geo))),boundary_constraints=constraint_audit,authoritative_policy=exclusive,
            declared_unfenced=declared_unfenced,validation_frame=[local.lat0,local.lon0],
            opening_masks_lonlat=json.loads(shapely.to_geojson(shapely.transform(excluded,geo))),
            administrative_unsupported_m=max(0.,metric_boundary.length-(metric_boundary.intersection(convert(unary_union(direct)).buffer(2.)).length if direct else 0.)) if any(administrative(metadata[fid]) for fid in members) and not corroborated else 0.,
            legacy_deduplication=False,union_lonlat=json.loads(shapely.to_geojson(merged))))
    legacy=set(claimed)|{item['properties'].get('legacy_id',fid) for fid,item in physical.items() if fid in claimed}
    # One topological owner for exact shared linework across facilities as well.
    # Explicit mapping wins; distinct parallel physical lines are never buffered
    # against each other or classified as duplicates by distance.
    owned=shapely.GeometryCollection()
    for fid in sorted(meta,key=lambda fid:(not meta[fid].get('authoritative',False),meta[fid]['confidence']!='explicit',fid)):
        # A 2e-8 degree numerical envelope (at most 2.3 mm) accounts for
        # differently vertexed versions of the same mapped straight edge.
        allowed=meta[fid]['allowed_lonlat'].difference(owned.buffer(2e-8,cap_style=2))
        owned=unary_union([owned,allowed])
        def requested(xy):
            e,n,_=frame.geodetic_to_enu(xy[:,1],xy[:,0],np.zeros(len(xy)))
            return np.c_[e,n]
        meta[fid]['allowed']=shapely.transform(allowed,requested)
        meta[fid]['allowed_lonlat']=allowed
    for facility in audit:
        local=geodesy.EnuFrame(*facility['validation_frame'],0.)
        def metric(coords):
            e,n,_=local.geodetic_to_enu(coords[:,1],coords[:,0],np.zeros(len(coords)))
            return np.c_[e,n]
        def geographic(coords):
            la,lo,_=local.enu_to_geodetic(coords[:,0],coords[:,1],np.zeros(len(coords)))
            return np.c_[lo,la]
        expected=shapely.transform(shape(facility['completeness']['expected_external_lonlat']),metric)
        masks=shapely.transform(shape(facility['opening_masks_lonlat']),metric)
        # Preserve the multiset until validation, so duplicate runs cannot be
        # hidden by unary_union. Shared-owner coverage can belong to a neighbor.
        selected_lines=[]
        for record in meta.values():
            if record['boundary_class']=='secured_compound':continue
            selected_metric=shapely.transform(record['allowed_lonlat'],metric)
            selected_lines.extend(lines(selected_metric.intersection(expected.buffer(.005))))
        chosen=shapely.MultiLineString(selected_lines) if selected_lines else shapely.GeometryCollection()
        checked=validate(facility['facility_id'],expected,chosen,[masks],
            internal=[dict(id=identifier,supported=False) for identifier in facility['completeness']['unexplained_internal_ids']],
            missing_source=facility['completeness']['missing_source_reason'])
        checked.update(expected_external_lonlat=facility['completeness']['expected_external_lonlat'],
            selected_external_lonlat=json.loads(shapely.to_geojson(shapely.transform(chosen,geographic))),
            unexpected_missing_lonlat=json.loads(shapely.to_geojson(shapely.transform(shape(checked['unexpected_missing_xy']),geographic))))
        facility['completeness']=checked
        if checked['status']=='implementation_gap':
            log.warning('perimeter completeness facility=%s missing_m=%.3f duplicate_m=%.3f',facility['facility_id'],checked['unexpected_missing_m'],checked['duplicate_m'])
            import revisit
            revisit.fault()
        for decision in facility['source_decisions']:
            record=meta.get('physical:'+decision['osm_id'])
            if record is None:
                record=next((r for r in meta.values() if r['confidence']=='explicit' and
                    r['facility_id']==facility['facility_id'] and decision['osm_id'] in r['source_ids']),None)
            chosen_source=record['allowed_lonlat'].intersection(shape(decision['original_lonlat']).buffer(2e-8)) if record else shapely.GeometryCollection()
            decision['selected_lonlat']=json.loads(shapely.to_geojson(chosen_source))
            decision['routed']=not chosen_source.is_empty
            decision['owner_facility_id']=record['facility_id'] if record else None
        facility['boundary_selection_reason']='current OSM border-zone relation outer ways only; no inference or relocation' if facility.get('authoritative_policy') else 'named same-facility exterior corroborated by mapped physical outer members' if facility['corroborating_outer_members'] else 'direct source or available physical evidence; unsupported administrative extensions withheld'
    with _lock:
        _selections[cache_key]=([(fid,category,[geo for geo,_ in rings]) for fid,category,rings in selected],
            {fid:{k:v for k,v in record.items() if k!='allowed'} for fid,record in meta.items()},copy.deepcopy(audit),set(legacy))
        while len(_selections)>24:_selections.popitem(last=False)
    return selected,meta,audit,legacy


def intervals(a,b,allowed):
    """Excluded source-centreline intervals; no mesh objects exist at this stage."""
    line=LineString([a,b]);length=line.length
    # Projection through two ENU frames loses micrometres. Snap only at 2 mm.
    pieces=lines(line.intersection(allowed.buffer(.002,cap_style=2)))
    spans=sorted(tuple(sorted((line.project(shapely.Point(piece.coords[0])),line.project(shapely.Point(piece.coords[-1]))))) for piece in pieces)
    cuts=[];at=0.
    for lo,hi in spans:
        if lo>at+.00001:cuts.append((at,lo))
        at=max(at,hi)
    if at<length-.00001:cuts.append((at,length))
    return cuts


def context(md,frame,box,area_layers):
    """Read complete facility context independently of display-tile loading order.

    Expand through intersecting complete source bounds until fixed point. A
    bounded request fails explicitly instead of silently selecting partial topology.
    """
    import perimeters as p
    import authoritativeperimeters as authoritative
    x0,y0,x1,y1=box
    la,lo,_=frame.enu_to_geodetic(np.array([x0-48,x1+48]),np.array([y0-48,y1+48]),np.zeros(2))
    initial=tuple(mapcells.cells_for_points(la,lo,max_cells=64))
    key=(md,repr(md.versions()),initial,evidence_signature(),authoritative.signature())
    with _lock:cached=_contexts.get(key)
    if cached is not None:return cached
    pending=set(initial);done=set();by_layer={layer:[] for layer in area_layers+('infrastructure','land_use','roads')}
    reviewed_sources={fid for record in evidence() if record['properties']['role']=='perimeter_override' and
        record['properties'].get('confidence')=='verified' for fid in record['properties']['facility_sources']}
    relevant=set();extent=shapely.box(float(lo.min()),float(la.min()),float(lo.max()),float(la.max()))
    authoritative_bounds=authoritative.bounds()
    if authoritative_bounds and extent.intersects(shapely.box(*authoritative_bounds).buffer(.001)):
        w,s,e,n=authoritative_bounds
        if e-w>2. or n-s>2.:raise ValueError('authoritative source exceeds bounded facility context')
        extent=extent.union(shapely.box(*authoritative_bounds))
        pending.update(mapcells.cells_for_bbox(s,w,n,e,max_cells=256))
    while pending:
        if len(done|pending)>256:raise ValueError('facility context exceeds 256 cells; explicit perimeter evidence required')
        batch=sorted(pending);pending.clear()
        for cellkey in batch:
            if cellkey in done:continue
            cell=md.cell(cellkey,layers=tuple(by_layer),kind='demand');done.add(cellkey)
            for layer in by_layer:
                feats=cell.layers.get(layer)
                if feats is None:raise ValueError('facility context missing '+layer+' for '+str(cellkey))
                by_layer[layer].append(feats)
                if layer not in area_layers:continue
                for i in range(len(feats)):
                    fid=p.osm_id(feats,i)
                    if not fid or fid in relevant or feats.kind[i]!=mapgeom.POLYGON or not p.classify(p.tags_of(feats,i),str(feats.attrs.get('class',['']*len(feats))[i])):continue
                    parts=feats.parts(i)
                    if not parts:continue
                    polygon=unary_union([Polygon(part[0]).buffer(0) for part in parts if len(part[0])>=4])
                    if not polygon.intersects(extent):continue
                    relevant.add(fid)
                    if fid in reviewed_sources:continue
                    w,s,e,n=polygon.bounds
                    # No longitude wrap or huge accidental whole-continent queries.
                    if e-w>2. or n-s>2.:raise ValueError('facility exceeds bounded context; use explicit evidence')
                    extent=extent.union(polygon)
                    pending.update(set(mapcells.cells_for_bbox(s,w,n,e,max_cells=256))-done)
    # Concat does not change complete source geometry. Deduplication happens by OSM source in rows.
    result={layer:combine(features,layer) if layer!='roads' else features for layer,features in by_layer.items()}
    with _lock:
        _contexts[key]=result
        while len(_contexts)>48:_contexts.popitem(last=False)
    return result
