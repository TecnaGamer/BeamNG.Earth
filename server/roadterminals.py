"""Solid approach dividers and road-following lane arrows at ramp/city junctions."""
import json
import math
import numpy as np

SOLID_M = 60.
ARROW_STATIONS = (14., 38.)       # metres behind the stop/crosswalk clearance
CITY = {'primary','secondary','tertiary','residential','unclassified','living_street','service'}
PAINT = 'line_turn_arrow'


def turn_lanes(tags, direction, count):
    """OSM lanes are ordered left to right in the direction of travel, even on reversed ways."""
    value=tags.get('turn:lanes:forward' if direction>=0 else 'turn:lanes:backward',tags.get('turn:lanes',''))
    lanes=str(value).split('|')
    if len(lanes)!=count:return [set() for _ in range(count)]
    aliases={'through':'straight','left':'left','slight_left':'left','sharp_left':'left',
             'right':'right','slight_right':'right','sharp_right':'right'}
    return [{aliases[t.strip()] for t in lane.split(';') if t.strip() in aliases} for lane in lanes]


def assign_turns(movements, lane_tags, owner_dirs, primary, count):
    """Match explicit lane tags, then distribute connected movements over this local lane count."""
    turns=turn_lanes(lane_tags.get(primary,{}),owner_dirs.get(primary,0),count)
    if not any(turns):
        for fid,direction in owner_dirs.items():
            candidate=turn_lanes(lane_tags.get(fid,{}),direction,count)
            if any(candidate):
                turns=candidate
                break
    inferred=not any(turns)
    order=[t for t in ('left','straight','right') if t in movements]
    unknown=[k for k,lane in enumerate(turns) if not lane]
    # A single inferred lane must get one readable marking. Overlaying every
    # connected turn at its centre made a white knot at city-road terminals.
    if inferred and count==1:
        turns[0].add('straight' if 'straight' in order else order[0])
        return turns,inferred
    for k,lane in enumerate(turns):
        if lane:continue
        if count==1:lane.update(order)
        else:
            index=k*(len(order)-1)/(count-1)
            lane.add(order[int(math.floor(index+.5))])
    if 'straight' in movements and unknown and not any('straight' in lane for lane in turns):
        turns[min(unknown,key=lambda k:abs(k-(count-1)/2))].add('straight')
    return turns,inferred


def _key(p):return tuple(np.rint(np.asarray(p[:2])*2).astype(np.int64))


def _arc(p):return np.r_[0.,np.cumsum(np.linalg.norm(np.diff(p[:,:2],axis=0),axis=1))]


def _point(p,s):
    arc=_arc(p)
    return np.array([np.interp(s,arc,p[:,d]) for d in range(3)])


def context(frame,cells,rows,box,urban_fn=None,widths=None,left_hand=False):
    """Extra terminal context only; a junction just outside a tile still owns its arrows inside it."""
    import roads,mapgeom
    out=list(rows);seen={r[0] for r in rows}
    margin=SOLID_M+45.
    for feats in cells:
        if feats is None:continue
        specs=roads.road_specs(feats,urban_fn,widths,left_hand)
        for i,sp in enumerate(specs):
            fid=str(feats.ids[i])
            if fid in seen or sp is None or not (sp.link or sp.cls in CITY):continue
            seen.add(fid)
            for p in roads.ground_parts(feats,i):
                e,n=mapgeom.enu_xy(feats,frame,p)
                if (e.max()<box[0]-margin or e.min()>box[2]+margin or
                    n.max()<box[1]-margin or n.min()>box[3]+margin):continue
                out.append((fid,sp,np.c_[e,n,np.zeros(len(e))],float(p[:,0].mean())))
    return out


def find(frame,rows,tags):
    """Trace each outgoing ramp back from a true city junction, without crossing ramp forks."""
    import roads,roadlanes
    nodes={}
    for ri,(_,sp,p,_) in enumerate(rows):
        for vi,point in enumerate(p):nodes.setdefault(_key(point),[]).append((ri,vi))
    result=[]
    for ri,(fid,sp,p,_) in enumerate(rows):
        if not sp.link or not sp.oneway or sp.kind not in roads.BUILT:continue
        terminal=len(p)-1 if sp.dir>=0 else 0
        inc=nodes.get(_key(p[terminal]),())
        town=[(j,v) for j,v in inc if j!=ri and not rows[j][1].link and rows[j][1].cls in CITY
              and rows[j][1].kind in roads.BUILT]
        if not town:continue
        # Do not paint a freeway-style merge or a ramp continuation as a stop approach.
        following=[j for j,v in inc if j!=ri and rows[j][1].oneway and not rows[j][1].link and
                   v==(0 if rows[j][1].dir>=0 else len(rows[j][2])-1) and
                   rows[j][1].cls in ('motorway','trunk')]
        if following:continue
        oriented=p if sp.dir>=0 else p[::-1]
        incoming=oriented[-1,:2]-_point(oriented,max(0.,_arc(oriented)[-1]-8.))[:2]
        incoming/=max(np.linalg.norm(incoming),1e-9)
        movements=set();clear=0.
        departures=town+[(j,v) for j,v in inc if j!=ri and rows[j][1].link and rows[j][1].oneway and
                         v==(0 if rows[j][1].dir>=0 else len(rows[j][2])-1)]
        for j,v in departures:
            _,city,line,_=rows[j]
            for step in (-1,1):
                if not 0<=v+step<len(line) or (city.oneway and step!=(1 if city.dir>=0 else -1)):continue
                arm=line[v+step,:2]-line[v,:2]
                arm/=max(np.linalg.norm(arm),1e-9)
                angle=math.degrees(math.atan2(incoming[0]*arm[1]-incoming[1]*arm[0],incoming@arm))
                if abs(angle)>150.:continue
                movements.add('straight' if abs(angle)<35. else 'left' if angle>0 else 'right')
                if not city.link:clear=max(clear,roads.pavement_half(city)+2.)
        if not movements:continue
        # Full source geometry is kept until after stationing, so tile cuts never
        # restart arrows or the solid/dashed boundary.
        path=oriented[::-1].copy();owner_rows=[ri]
        current=ri;seen={ri};length=_arc(path)[-1]
        while length<SOLID_M+clear+8.:
            _,csp,cp,_=rows[current]
            start=0 if csp.dir>=0 else len(cp)-1
            candidates=[j for j,v in nodes.get(_key(cp[start]),()) if j not in seen and
                        rows[j][1].link and rows[j][1].oneway and
                        v==(len(rows[j][2])-1 if rows[j][1].dir>=0 else 0)]
            if len(set(candidates))!=1:break
            j=candidates[0];_,nsp,np_,_=rows[j]
            # A split is a separate lane allocation, not another turn approach.
            outgoing={k for k,v in nodes.get(_key(cp[start]),()) if rows[k][1].link and rows[k][1].oneway and
                      v==(0 if rows[k][1].dir>=0 else len(rows[k][2])-1)}
            if len(outgoing)>1:break
            back=np_[::-1] if nsp.dir>=0 else np_
            path=np.vstack([path,back[1:]])
            owner_rows.append(j);seen.add(j);current=j;length=_arc(path)[-1]
        n=max(1,int(round(roadlanes.counts(sp,p[[terminal]],frame)[0].sum()))) if sp.lanes else sum(roads.lane_counts(sp))
        owner_dirs={rows[k][0]:rows[k][1].dir for k in owner_rows}
        lane_tags={owner:tags.get(owner,{}) for owner in owner_dirs}
        turns,inferred=assign_turns(movements,lane_tags,owner_dirs,fid,n)
        result.append(dict(fid=fid,path=path,owners=set(owner_dirs),owner_rows=owner_rows,
                           owner_dirs=owner_dirs,lane_tags=lane_tags,movements=movements,clear=clear,
                           turns=turns,inferred=inferred))
    return result


def solid_runs(fid,look,line,approaches):
    """Replace only travel dividers; freeway merge dots keep their own independent pattern."""
    import roads,roadlanes
    if 'lane_index' not in look or look.get('ramp_guidance') or not look['m'].startswith('line_dashed'):
        return [(look,line)]
    matches=[a for a in approaches if fid in a['owners']]
    if not matches:return [(look,line)]
    a=matches[0];p=roads.densify(line,2.)
    station,_=roadlanes._distance(p,a['path'])
    # Insert the exact style boundaries rather than snapping to a 2 m sample.
    points=[p[0]];distance=[station[0]]
    for j in range(len(p)-1):
        crossings=[]
        for boundary in (a['clear'],a['clear']+SOLID_M):
            if (station[j]-boundary)*(station[j+1]-boundary)<0.:
                t=(boundary-station[j])/(station[j+1]-station[j]);crossings.append((t,boundary))
        for t,boundary in sorted(crossings):
            points.append(p[j]+t*(p[j+1]-p[j]));distance.append(boundary)
        points.append(p[j+1]);distance.append(station[j+1])
    points=np.asarray(points);distance=np.asarray(distance)
    mid=(distance[:-1]+distance[1:])/2
    states=np.where(mid<a['clear'],-1,np.where(mid<=a['clear']+SOLID_M,1,0))
    cuts=np.r_[0,np.flatnonzero(states[1:]!=states[:-1])+1,len(states)]
    solid={k:v for k,v in look.items() if k not in ('dash','gap','highway_lane')}
    solid.update(m='line_white',terminal_solid=True)
    return [(solid if states[i]==1 else look,points[i:j+1]) for i,j in zip(cuts[:-1],cuts[1:]) if states[i]>=0]


def _arrow(turns):
    """Convex paint pieces in (left, forward) metres: no fragile transparent texture atlas."""
    shapes=[np.array([[-.14,-2.],[.14,-2.],[.14,.1],[-.14,.1]])]
    if 'straight' in turns:
        shapes.extend((np.array([[-.14,0],[.14,0],[.14,1.1],[-.14,1.1]]),
                       np.array([[-.58,.8],[.58,.8],[0,2.]])))
    for turn,side in (('left',1.),('right',-1.)):
        if turn not in turns:continue
        stem=np.array([[0,-.2],[0,.2],[.5,.65],[.85,.65]])
        for p,q in zip(stem,stem[1:]):
            d=q-p;n=np.array([-d[1],d[0]])/np.linalg.norm(d)*.14
            shapes.append(np.array([p+n,p-n,q-n,q+n])*[side,1.])
        shapes.append(np.array([[.62,.15],[.62,1.15],[1.35,.65]])*[side,1.])
    return shapes


def arrows(frame,rows,approaches,box):
    import roads
    import roadlanes
    out=[]
    for a in approaches:
        path=a['path'];arc=_arc(path)
        for back in ARROW_STATIONS:
            station=a['clear']+back
            if station+3.>arc[-1]:continue
            point=_point(path,station)
            if not box[0]-3.<=point[0]<=box[2]+3. or not box[1]-3.<=point[1]<=box[3]+3.:continue
            candidates=[]
            for row_index in a.get('owner_rows',()):
                if not 0<=row_index<len(rows):continue
                off=roadlanes._distance(point[None],rows[row_index][2])[1][0]
                candidates.append((off,row_index))
            if not candidates:continue
            off,row_index=min(candidates);fid,sp,_,_=rows[row_index]
            if off>25.:continue
            lane_count=float(roadlanes.counts(sp,point[None],frame)[0].sum()) if sp.lanes else float(sum(roads.lane_counts(sp)))
            n=int(round(lane_count))
            # Paint only after a lane has fully opened; fractional taper lanes
            # previously pulled arrows toward the center or left them stacked.
            if n<1 or abs(lane_count-n)>.18:continue
            turns,_=assign_turns(a['movements'],a['lane_tags'],a['owner_dirs'],fid,n)
            forward=_point(path,max(0.,station-1.))[:2]-_point(path,station+1.)[:2]
            forward/=max(np.linalg.norm(forward),1e-9);left=np.array([-forward[1],forward[0]])
            lane_w=sp.lane_w or 3.6
            for k,lane_turns in enumerate(turns):
                centre=point[:2]+left*((n/2-k-.5)*lane_w)
                for shape in _arrow(lane_turns):
                    xy=centre+shape[:,0,None]*left+shape[:,1,None]*forward
                    # Clip convex arrow pieces exactly to this tile, so neither
                    # adjacent tile owns a second coplanar copy of the paint.
                    for axis,bound,sign in ((0,box[0],1),(0,box[2],-1),(1,box[1],1),(1,box[3],-1)):
                        clipped=[]
                        for p,q in zip(xy,np.roll(xy,-1,axis=0)):
                            inside=(p[axis]-bound)*sign>=0;other=(q[axis]-bound)*sign>=0
                            if inside:clipped.append(p)
                            if inside!=other:clipped.append(p+(q-p)*(bound-p[axis])/(q[axis]-p[axis]))
                        xy=np.asarray(clipped)
                        if len(xy)<3:break
                    if len(xy)>=3:
                        out.append(dict(m=PAINT,w=0.,tl=1.,p=8,n=[],poly=np.c_[xy,np.full(len(xy),point[2])].round(3).tolist(),
                                        turn_arrow=True))
    return out


def tags_from_cells(cells):
    out={}
    for feats in cells:
        if feats is None:continue
        for fid,value in zip(feats.ids,feats.attrs.get('osm_tags',['']*len(feats))):
            if value:
                try:out[str(fid)]=json.loads(str(value))
                except (ValueError,TypeError):pass
    return out
