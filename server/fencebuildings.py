"""Final-mesh geometry interface and local, indexed perimeter/building conflict resolution."""
from collections import OrderedDict
import hashlib
import json
import logging
import math
import os
import threading
from pathlib import Path

import numpy as np
import shapely
import mapgeom
from shapely.geometry import Polygon, LineString, Point, MultiPoint
from shapely.ops import substring
from shapely.strtree import STRtree

log=logging.getLogger('earthd.fencebuildings')
CLEARANCE=max(0.,float(os.environ.get('BNE_FENCE_BUILDING_CLEARANCE','0.35')))
ENVELOPE=CLEARANCE+.31  # stock post caps and asymmetric mesh thickness, not just its centreline
MAX_OFFSET=4.
MAX_EXTRA=12.
_cache=OrderedDict()
_bounds=OrderedDict()
_lock=threading.RLock()
DISCOVERY_HALO=max(8.,float(os.environ.get('BNE_FENCE_BUILDING_DISCOVERY_HALO','128')))


def from_mesh(vertices,triangles,origin=(0.,0.),source=None,original=None):
    """Project the actual completed render triangles, retaining concavities/multiple components."""
    vertices=np.asarray(vertices,float)
    if not len(vertices): return None
    triangles=np.asarray(triangles,int).reshape(-1,3)
    xy=vertices[:,:2]+np.asarray(origin,float)
    parts=xy[triangles]
    area=np.abs((parts[:,1,0]-parts[:,0,0])*(parts[:,2,1]-parts[:,0,1])-
                (parts[:,1,1]-parts[:,0,1])*(parts[:,2,0]-parts[:,0,0]))
    if np.any(area>1e-8):
        shape=shapely.union_all(shapely.polygons(parts[area>1e-8]),grid_size=.005).simplify(.005,preserve_topology=True)
    else:
        shape=MultiPoint(xy).convex_hull.buffer(.01)
    if shape.is_empty: return None
    meta=dict(source or {})
    meta.setdefault('id','mesh:'+hashlib.sha1(xy.tobytes()).hexdigest()[:16])
    meta.setdefault('dataset','final-render-mesh')
    return dict(geometry=shape,source=meta,zmin=float(vertices[:,2].min()),zmax=float(vertices[:,2].max()),
        original_xy=(np.asarray(original,float)+origin).tolist() if original is not None else None,
        transform=dict(position=[float(origin[0]),float(origin[1]),0.],rotation='baked in final mesh',scale=[1.,1.,1.]))


def custom_entries(entries,out_dir):
    """Read final baked .cdae geometry, including custom rotation, bedding and model scale."""
    import cdae
    result=[]
    for entry in entries:
        path=Path(out_dir)/Path(entry['shape']).name
        if not path.exists(): continue
        shape=cdae.read(path)
        for mesh in shape['meshes']:
            if mesh['type']!=0: continue
            arrays=cdae.mesh_arrays(mesh)
            verts=arrays['verts']+np.asarray(entry['pos'],float)
            indices=arrays['indices']
            if len(indices)%3: continue
            record=from_mesh(verts,indices.reshape(-1,3),source=dict(id=entry['shape'],dataset='baked custom/local model'))
            if record:
                record['transform']=dict(position=list(entry['pos']),rotation='identity; rotation/scale already baked into cdae',scale=[1.,1.,1.])
                result.append(record)
    return result


def materialize(records):
    result=[]
    for record in records:
        if 'deferred_mesh' in record:
            record=from_mesh(*record['deferred_mesh'])
            if record is not None: result.append(record)
        elif 'deferred_custom' in record: result.extend(custom_entries(*record['deferred_custom']))
        else: result.append(record)
    return result


def cold_custom(custom,cache_dir):
    """Inspect the renderer's oriented mesh without baking or overwriting session assets.

    write_model centres XY identically; bedding and bridge tilt modify Z only.
    """
    import buildings,threedmr
    records=[];radii=[]
    for entry in custom or []:
        try:
            vertices,_,triangles,*_=buildings._model_mesh(entry,cache_dir)
            if not len(vertices): continue
            dx,dy=np.ptp(vertices[:,:2],axis=0)
            if max(dx,dy)>buildings.MODEL_MAX_M: continue
            record=from_mesh(buildings._centred(vertices),triangles,entry['xy'],
                dict(id=str(entry['id']),dataset='processed custom/local model; XY before Z-only bedding'))
            if record: records.append(record)
            radius=min(max(max(dx,dy)/2+buildings.SUPPRESS_MARGIN_M,buildings.SUPPRESS_MIN_M),buildings.SUPPRESS_MAX_M)
            radii.append((entry['id'],float(entry['xy'][0]),float(entry['xy'][1]),radius))
        except threedmr.UnsupportedModelError:
            pass
        except Exception:
            import revisit
            revisit.fault()
            log.warning('fence building custom model %s unavailable',entry['id'],exc_info=True)
    return records,radii


def _key(md,frame,tx,ty,signature=()):
    import roads
    return (md,repr(md.versions()) if hasattr(md,'versions') else '',float(frame.lat0),float(frame.lon0),tuple(roads.tile_bbox(tx,ty)),signature)


def publish(md,frame,tx,ty,records,signature=()):
    with _lock:
        key=_key(md,frame,tx,ty,signature)
        _cache[key]=(frame,records)
        _cache.move_to_end(key)
        while len(_cache)>96: _cache.popitem(last=False)


def _signature(out_dir):
    import revisit,threedmr
    return revisit.code_key(),threedmr.local_signature(os.path.dirname(out_dir)) if out_dir else None


def publish_bounds(md,frame,tx,ty,records,out_dir=None):
    """Cheap final-vertex broad phase for rendered tiles, even those without fences of their own."""
    boxes=[]
    for record in records:
        if 'deferred_mesh' in record:
            vertices,_,origin,*_=record['deferred_mesh']
            xy=np.asarray(vertices)[:,:2]
            boxes.append((float(xy[:,0].min()+origin[0]),float(xy[:,1].min()+origin[1]),
                          float(xy[:,0].max()+origin[0]),float(xy[:,1].max()+origin[1])))
        elif 'geometry' in record: boxes.append(record['geometry'].bounds)
    with _lock:
        key=_key(md,frame,tx,ty,_signature(out_dir))
        _bounds[key]=(frame,tx,ty,boxes)
        _bounds.move_to_end(key)
        while len(_bounds)>128: _bounds.popitem(last=False)


def nearby(frame,tx,ty,md,ground,own_records,out_dir=None,vfs_dir=None):
    """Share finalized own geometry; request only owner tiles of intersecting neighbouring buildings.

    Cold neighbours use the SAME road_tile generator, not a second implementation of building planning.
    Geometry is cached separately and mapped between the real owner frames.
    """
    import buildings,roads,geotiles
    import threedmr
    signature=_signature(out_dir)
    own_records=materialize(own_records)
    publish(md,frame,tx,ty,own_records,signature)
    box=roads.tile_bbox(tx,ty)
    query=shapely.box(box[0]-48,box[1]-48,box[2]+48,box[3]+48)
    owners={}
    def register_owner(cx,cy):
        if hasattr(tx,'key'):
            lat,lon,_=frame.enu_to_geodetic(cx,cy,0.)
            gx,gy=geotiles.tile_of(lat,lon)
            tile=geotiles.GeoBox(gx,gy)
            owner_frame,ox,oy=tile.frame,tile,None
        else:
            side=float(roads.tile_bbox(0,0)[2]-roads.tile_bbox(0,0)[0])
            owner_frame,ox,oy=frame,math.floor(cx/side),math.floor(cy/side)
        owners[_key(md,owner_frame,ox,oy,signature)]=(owner_frame,ox,oy)
    for cell in roads.cells_of_tile(frame,tx,ty,margin=DISCOVERY_HALO):
        feats=md.cell(cell,layers=('buildings',),kind='demand').layers.get('buildings')
        if feats is None: continue
        boxes=feats.boxes(frame);cx,cy=feats.outer_centres(frame)
        for i,b in enumerate(boxes):
            if not np.isfinite([cx[i],cy[i]]).all(): continue
            broad=shapely.box(*b)
            if not query.intersects(broad):
                # Pitched/processed footprints can exceed the raw AABB. Check the same oriented box
                # used by the building generator before deciding that an owner is irrelevant.
                if query.distance(broad)>math.hypot(b[2]-b[0],b[3]-b[1])+8: continue
                if not feats.parts(i): continue
                e,n=mapgeom.enu_xy(feats,frame,feats.parts(i)[0][0])
                try: centre,axis,half_l,half_w=buildings.oriented_box(np.c_[e,n])
                except ValueError: continue
                perpendicular=np.array([-axis[1],axis[0]])
                potential=Polygon([centre+axis*s*half_l+perpendicular*t*half_w for s,t in ((-1,-1),(1,-1),(1,1),(-1,1))]).buffer(8.)
                if not query.intersects(potential): continue
            register_owner(cx[i],cy[i])
    with _lock: published=list(_bounds.items())
    for key,(owner_frame,ox,oy,boxes) in published:
        if key[0] is not md or key[-1]!=signature: continue
        # Avoid mapping retained metadata from a distant teleport back through the Earth.
        if abs(owner_frame.lat0-frame.lat0)>.05 or abs((owner_frame.lon0-frame.lon0+180)%360-180)>.1: continue
        for bounds in boxes:
            poly=shapely.box(*bounds)
            if owner_frame is not frame:
                def project_box(coords):
                    return geotiles.world_map(owner_frame,frame,np.c_[coords,np.zeros(len(coords))])[:,:2]
                poly=shapely.transform(poly,project_box)
            if poly.intersects(query): owners[key]=(owner_frame,ox,oy);break
    if out_dir:
        import threedmr
        cache_dir=os.path.dirname(out_dir)
        lat,lon,_=frame.enu_to_geodetic((box[0]+box[2])/2,(box[1]+box[3])/2,0.)
        positions=[]
        for mid in threedmr.region_ids(float(lat),float(lon),cache_dir):
            try:
                mlat,mlon,_=threedmr.model_position(int(mid),cache_dir)
                positions.append((mlat,mlon))
            except Exception as error:
                log.warning('fence building model %s position unavailable: %s',mid,error)
        positions.extend((float(entry['lat']),float(entry['lon'])) for entry in threedmr.local_models(cache_dir))
        for mlat,mlon in positions:
            mx,my,_=frame.geodetic_to_enu(mlat,mlon,0.)
            if query.distance(Point(mx,my))<=buildings.MODEL_MAX_M/math.sqrt(2)+8:
                register_owner(mx,my)
    result=[];seen=set()
    for key,(owner_frame,ox,oy) in owners.items():
        with _lock: cached=_cache.get(key)
        if cached is None:
            records=[]
            items,custom=buildings.road_tile_items(owner_frame,ox,oy,md,ground,
                cache_dir=os.path.dirname(out_dir) if out_dir else None)
            custom_records,radii=cold_custom(custom,os.path.dirname(out_dir) if out_dir else None)
            buildings.road_tile(owner_frame,ox,oy,md,ground,items=items,custom=custom,
                entries=[],radii=radii,fence_geometry=records)
            records.extend(custom_records)
            publish(md,owner_frame,ox,oy,records,signature)
        else:
            owner_frame,records=cached
        for record in records:
            identity=(record['source']['id'],hashlib.sha1(record['geometry'].wkb).hexdigest())
            if identity in seen: continue
            seen.add(identity)
            if owner_frame is frame:
                mapped=record
            else:
                def project(coords):
                    return geotiles.world_map(owner_frame,frame,np.c_[coords,np.zeros(len(coords))])[:,:2]
                mapped=dict(record,geometry=shapely.transform(record['geometry'],project),
                    transform=dict(record['transform'],source_frame=[owner_frame.lat0,owner_frame.lon0],
                                   target_frame=[frame.lat0,frame.lon0],placement='geotiles.world_map'))
            if mapped['geometry'].intersects(query): result.append(mapped)
    # Own custom models can extend outside all ordinary source-building polygons.
    for record in own_records:
        identity=(record['source']['id'],hashlib.sha1(record['geometry'].wkb).hexdigest())
        if identity not in seen and record['geometry'].intersects(query): result.append(record)
    return result


def mesh_footprint(a,b,z0=None,z1=None):
    """Verified model's swept horizontal rectangle, including post caps and rear-biased mesh."""
    delta=b-a
    length=float(np.linalg.norm(delta))
    if length<1e-9: return Point(a).buffer(.21)
    centre=(a+b)/2
    along=delta/4.
    normal=np.array([-delta[1],delta[0]])/length
    if z0 is not None and z1 is not None:
        dz=float(z1-z0);slope_length=math.hypot(length,dz)
        # Match perimeters.lua's pitched local up: tops/footings move horizontally on a slope.
        up_xy=-delta/length*dz/slope_length
        corners=[centre+along*x+normal*y+up_xy*z for x in (-2.078,2.067)
                 for y in (-.209,.078) for z in (-1.11,2.863)]
        return MultiPoint(corners).convex_hull
    return Polygon([centre+along*x+normal*y for x,y in ((-2.078,-.209),(2.067,-.209),(2.067,.078),(-2.078,.078))])


class Index:
    def __init__(self,records):
        self.records=list(records)
        self.shapes=[r['geometry'] for r in self.records]
        self.tree=STRtree(self.shapes)
        self.events=[]

    def conflicts(self,a,b,z0=None,z1=None):
        panel=mesh_footprint(a,b,z0,z1).buffer(CLEARANCE)
        return [int(i) for i in self.tree.query(panel,predicate='intersects')]

    def clear(self,path,transport=(),blockers=(),other_edges=(),protected=()):
        import fencecrossings as fc
        for a,b in zip(path,path[1:]):
            if self.conflicts(a,b): return False
            panel=mesh_footprint(a,b)
            if any(panel.intersects(shape) for shape in protected): return False
            for corridor in transport:
                if corridor.get('grade','ground')=='ground' and panel.intersects(LineString([corridor['a'][:2],corridor['b'][:2]]).buffer(corridor['radius'])): return False
            for c,d,radius in blockers:
                if LineString([a,b]).distance(LineString([c,d]))<=radius: return False
            for c,d in other_edges:
                hit=fc.intersection(a,b,c,d)
                if hit is not None and 1e-5<hit[0]<1-1e-5: return False
        return True

    def resolve(self,a,b,transport=(),blockers=(),other_edges=(),protected=()):
        """Return centreline cuts and safe short detours; leave unconflicting geometry untouched."""
        line=LineString([a,b]);length=line.length
        cuts=[];detours=[];accepted=[]
        for index in self.conflicts(a,b):
            record=self.records[index]
            obstacle=record['geometry'].buffer(ENVELOPE,join_style=2)
            intersection=line.intersection(obstacle)
            pieces=list(intersection.geoms) if hasattr(intersection,'geoms') else [intersection]
            for piece in pieces:
                if piece.is_empty: continue
                if piece.geom_type=='Point':
                    lo=max(0.,line.project(piece)-.1);hi=min(length,line.project(piece)+.1)
                elif piece.geom_type=='LineString':
                    lo,hi=sorted((line.project(Point(piece.coords[0])),line.project(Point(piece.coords[-1]))))
                else: continue
                if hi-lo<.001: continue
                cuts.append((lo,hi));choice='terminate_at_building';chosen=None
                start,end=line.interpolate(lo),line.interpolate(hi)
                polys=list(obstacle.geoms) if hasattr(obstacle,'geoms') else [obstacle]
                for polygon in polys:
                    if polygon.geom_type!='Polygon' or polygon.boundary.distance(start)>.01 or polygon.boundary.distance(end)>.01: continue
                    ring=polygon.exterior
                    s,t=ring.project(start),ring.project(end)
                    first=substring(ring,min(s,t),max(s,t))
                    coords=list(first.coords)
                    if s>t: coords=coords[::-1]
                    rest=list(substring(ring,max(s,t),ring.length).coords)+list(substring(ring,0,min(s,t)).coords)[1:]
                    if s<t: rest=rest[::-1]
                    for coords in (coords,rest):
                        route=np.asarray(coords,float)
                        route[0],route[-1]=np.asarray(start.coords[0]),np.asarray(end.coords[0])
                        candidate=LineString(route)
                        extra=candidate.length-(hi-lo)
                        offset=max(Point(p).distance(line) for p in route)
                        if hi-lo<=24. and extra<=MAX_EXTRA and offset<=MAX_OFFSET and self.clear(route,transport,blockers,list(other_edges)+accepted,protected):
                            if chosen is None or candidate.length<LineString(chosen).length:
                                chosen=route;choice='small_local_adjustment' if offset<=1.5 else 'local_detour'
                if chosen is not None:
                    detours.append((lo,hi,chosen))
                    accepted.extend(zip(chosen,chosen[1:]))
                self.events.append(dict(building=record['source'],original_footprint=record.get('original_xy'),
                    final_footprint=shapely.to_geojson(record['geometry']),transform=record['transform'],
                    intersection_xy=list(start.coords[0]),clearance_m=CLEARANCE,resolution=choice,
                    removed_m=hi-lo,moved=chosen.tolist() if chosen is not None else [],omitted=chosen is None))
        return cuts,detours
