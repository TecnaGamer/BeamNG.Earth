"""Intersection-only perimeter openings; leaves existing road/rail geometry untouched."""
import math
import numpy as np
import mapgeom

ACTIVE_RAIL = {'rail', 'light_rail', 'tram', 'narrow_gauge', 'preserved'}
INACTIVE_RAIL = {'disused', 'abandoned', 'razed', 'dismantled', 'construction', 'proposed', 'planned'}
VEHICLE_ROADS = {'motorway','trunk','primary','secondary','tertiary','residential','service','unclassified',
                 'living_street','track','unknown'}


def cross(a,b):
    return float(a[0]*b[1]-a[1]*b[0])


def intersection(a,b,c,d):
    """(fence fraction, transport fraction) for a genuine segment intersection, including collinear overlap."""
    u,v=b[:2]-a[:2],d[:2]-c[:2]
    det=cross(u,v)
    if abs(det)<1e-10:
        length2=float(u@u)
        if length2<1e-10 or abs(cross(c[:2]-a[:2],u))>1e-6: return None
        left,right=sorted((float((c[:2]-a[:2])@u)/length2,float((d[:2]-a[:2])@u)/length2))
        lo,hi=max(0.,left),min(1.,right)
        if hi<lo: return None
        t=(lo+hi)/2
        v2=float(v@v)
        return t, float((a[:2]+t*u-c[:2])@v)/v2 if v2 else 0.
    t,s=cross(c[:2]-a[:2],v)/det,cross(c[:2]-a[:2],u)/det
    if -1e-9<=t<=1+1e-9 and -1e-9<=s<=1+1e-9:
        return float(np.clip(t,0,1)),float(np.clip(s,0,1))
    return None


def active_rail(feats,i,tags):
    value=str(feats.attrs.get('osm_railway',['']*len(feats))[i] or '') or tags.get('railway','')
    if value in INACTIVE_RAIL: return False
    if value: return value in ACTIVE_RAIL
    return str(feats.attrs.get('class',['']*len(feats))[i]) in ('standard_gauge','narrow_gauge','light_rail','tram')


def corridors(frame,feats,tags_fn,id_fn,bounds=None):
    """Use complete transport features and mapped bridge/tunnel ranges, never clipped tile roads."""
    import roads
    result=[]
    if feats is None: return result
    far=None
    if bounds is not None:
        boxes=feats.boxes(frame)
        far=(boxes[:,2]<bounds[0])|(boxes[:,0]>bounds[2])|(boxes[:,3]<bounds[1])|(boxes[:,1]>bounds[3])
    for i in range(len(feats)):
        if far is not None and far[i]:continue
        subtype=str(feats.attrs.get('subtype',['']*len(feats))[i])
        tags=tags_fn(feats,i)
        cls=str(feats.attrs.get('class',['']*len(feats))[i])
        rail=subtype=='rail' or tags.get('railway') in ACTIVE_RAIL|INACTIVE_RAIL
        if rail:
            if not active_rail(feats,i,tags): continue
            # 7 m corridor for one active track; parallel tracks contribute unioned openings.
            radius=3.5
        else:
            if subtype!='road' or cls not in VEHICLE_ROADS: continue
            half=2. if cls in ('service','track') else (5. if cls in ('motorway','trunk') else 3.)
            width_rule=feats.rules.get('width')
            if width_rule is not None:
                index=width_rule.major(i)
                if index>=0:
                    try:
                        mapped=float(width_rule.value[index])
                        if math.isfinite(mapped) and 1.<=mapped<=40.: half=mapped/2
                    except (TypeError,ValueError): pass
            radius=half+.5
        fid=id_fn(feats,i) or str(feats.attrs.get('src_record',['']*len(feats))[i]) or str(feats.ids[i])
        ranges=roads.raw_level_ranges(feats,i)
        inactive=[]
        flags=feats.rules.get('flags')
        if rail and flags is not None:
            inactive=[(float(flags.start[j]),float(flags.end[j])) for j in flags.rows(i)
                      if any(k in str(flags.value[j]) for k in ('is_disused','is_abandoned','is_under_construction'))]
        for grade in ('ground','bridge','tunnel'):
            spans=roads.ground_ranges(ranges) if grade=='ground' else [(a,b) for a,b,k in ranges if k==grade]
            for cutlo,cuthi in inactive:
                spans=[piece for lo,hi in spans for piece in
                       ([(lo,hi)] if cuthi<=lo or cutlo>=hi else
                        ([(lo,cutlo)] if cutlo>lo else [])+([(cuthi,hi)] if cuthi<hi else []))]
            for part in feats.parts(i):
                for ring in part:
                    if len(ring)<2: continue
                    for lo,hi in spans:
                        piece=ring if (lo,hi)==(0.,1.) else roads.sub_polyline(ring,lo,hi)
                        e,n=mapgeom.enu_xy(feats,frame,piece)
                        xy=np.column_stack((e,n))
                        for a,b in zip(xy,xy[1:]):
                            result.append(dict(a=a,b=b,radius=radius+.12,kind='railway' if rail else 'road',
                                               osm_id=fid,grade=grade))
    return result


def at_grade(corridor,transport_fraction,fence_z=None):
    """Use actual 3D heights when supplied; otherwise respect mapped grade separation."""
    a,b=corridor['a'],corridor['b']
    if len(a)>=3 and len(b)>=3 and fence_z is not None:
        z=float(a[2]+transport_fraction*(b[2]-a[2]))
        return fence_z-.5<=z<=fence_z+3.35
    return corridor.get('grade','ground')=='ground'


class SurfaceIndex:
    """Final road-builder ribbon/pad envelopes, including fixed deck heights.

    Raw OSM grade tags cannot establish clearance to a bridge whose generated
    profile meets the ground. These are the same ribbon outlines used to draw it.
    """
    def __init__(self,chunks,surface=None,meshes=(),rail_corridors=None):
        import roadmesh
        from shapely.geometry import Polygon
        from shapely.strtree import STRtree
        self.records=[]
        candidates=[c for c in chunks if c.get('n') is not None and len(c['n'])>=2 and
                    c.get('w',0)>0 and not c.get('tunnel') and roadmesh.layer_of(c)=='base']
        for ribbon,chunk in roadmesh.ribbon_pairs(candidates) if candidates else []:
            left,right,_,_,_,z=ribbon
            for i in range(len(left)-1):
                coords=np.array([left[i],right[i],right[i+1],left[i+1]])
                polygon=Polygon(coords).buffer(0)
                if polygon.is_empty:continue
                fixed=bool(chunk.get('fixed'))
                heights=np.asarray([z[i],z[i+1]],float) if fixed and z is not None else None
                self.records.append(dict(geometry=polygon,heights=heights,sample_xy=coords,
                    slab=.6 if chunk.get('deck') else .15,source=str(chunk.get('fid','generated-road')),
                    grade='bridge' if chunk.get('deck') else 'ground'))
        # Generated junction/pad polygons can extend beyond their centreline ribbons.
        for chunk in chunks:
            if chunk.get('poly') is None or chunk.get('tunnel') or roadmesh.layer_of(chunk)!='base':continue
            points=np.asarray(chunk['poly'],float)
            if len(points)<3:continue
            polygon=Polygon(points[:,:2]).buffer(0)
            heights=points[:,2] if points.shape[1]>=3 and chunk.get('fixed') else None
            self.records.append(dict(geometry=polygon,heights=heights,sample_xy=points[:,:2],slab=.6,
                source=str(chunk.get('fid','generated-junction')),grade='bridge' if chunk.get('deck') else 'ground'))
        sample=[r for r in self.records if r['heights'] is None]
        if sample and surface is not None:
            counts=[len(r['sample_xy']) for r in sample]
            heights=np.asarray(surface(np.concatenate([r['sample_xy'] for r in sample])),float)
            at=0
            for record,count in zip(sample,counts):record['heights']=heights[at:at+count];at+=count
        valid=[]
        for record in self.records:
            heights=record.pop('heights');record.pop('sample_xy')
            if heights is None or not np.isfinite(heights).all():continue
            record.update(lower=float(heights.min())-record.pop('slab'),upper=float(heights.max())+.15)
            valid.append(record)
        self.records=valid
        # Actual completed railway render triangles, not just the source track.
        rail_tree=None
        if rail_corridors is not None:
            from shapely.geometry import LineString
            rail_tree=STRtree([LineString([c['a'][:2],c['b'][:2]]).buffer(c['radius']) for c in rail_corridors])
        for item in meshes:
            mesh,origin,identifier=item[:3]
            kind=item[3] if len(item)>3 else 'railway'
            if mesh is None:continue
            positions=np.asarray(mesh[0],float)+np.asarray(origin,float)
            triangles=positions[np.asarray(mesh[1],int)]
            for vertices in triangles:
                polygon=Polygon(vertices[:,:2])
                if polygon.area<1e-8:
                    from shapely.geometry import LineString
                    polygon=LineString(vertices[:,:2]).buffer(.01)
                if kind=='railway' and rail_tree is not None and not len(rail_tree.query(polygon,predicate='intersects')):continue
                self.records.append(dict(geometry=polygon,lower=float(vertices[:,2].min())-.15,
                    upper=float(vertices[:,2].max())+.15,source=identifier,grade='final_'+kind+'_mesh'))
        self.tree=STRtree([r['geometry'] for r in self.records])

    def conflicts(self,a,b,z0,z1):
        if not self.records:return []
        import fencebuildings
        panel=fencebuildings.mesh_footprint(a,b,z0,z1)
        lower=min(z0,z1)-1.11;upper=max(z0,z1)+2.863
        return [self.records[int(i)] for i in self.tree.query(panel,predicate='intersects')
                if self.records[int(i)]['lower']<=upper and self.records[int(i)]['upper']>=lower]

    def routing_obstacles(self,surface):
        if surface is None:return []
        result=[]
        if not self.records:return result
        points=[r['geometry'].centroid for r in self.records]
        grounds=np.asarray(surface(np.array([[p.x,p.y] for p in points])),float)
        for record,ground in zip(self.records,grounds):
            if record['lower']<=ground+2.863 and record['upper']>=ground-1.11:
                result.append(record['geometry'])
        if not result:return []
        import shapely
        return [shapely.union_all(result,grid_size=.005)]
