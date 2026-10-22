"""Shared lane-change cross-sections for pavement, paint, terrain materials and grading.

Counts refer to OSM through lanes, not shoulders. At a genuine continuation with a different
count each side approaches the same shared section with zero slope at the join. Smoothstep
eases the change over up to 360 m on highways (180 m either side); junction branches and ramps
are not treated as added through lanes. Profiles are measured on the full OSM way, so tile,
Overture-segment and bridge cuts cannot restart a taper.
"""
import json
from functools import lru_cache

import numpy as np


@lru_cache(maxsize=4096)
def model(value):
    return json.loads(value) if value else {}


def _bridge_way(feats, i):
    import roads
    return any(kind == "bridge" for _, _, kind in roads.level_ranges(feats, i))


def apply_spec(spec, feats, i):
    import roads
    # The selected port batch keeps the newer branch's bridge geometry untouched.
    if _bridge_way(feats, i):
        return spec
    values = [roads._rule_value(feats.rules.get(k), i) for k in
              ("lanes_forward", "lanes_backward", "lanes_both_ways")]
    if values[0] is None or values[1] is None:
        return spec
    f, b, c = (int(v or 0) for v in values)
    if f+b < 1 or f+b+c > 32 or (spec.oneway and bool(f) == bool(b)):
        return spec
    # An authoritative directional lane tag also fixes an inferred one-way direction.
    one = not (f and b)
    direction = (1 if f else -1) if one else 0
    lane_w = roads.MOTORWAY_LANE_M if spec.cls in ("motorway", "trunk") else 3.3
    shoulder = (roads.EDGE_STRIP_M if spec.link else roads.shoulder_width(spec.cls, oneway=one)) if spec.kind in roads.BUILT else 0.
    bias = roads.shoulder_bias(spec.cls, oneway=one) if spec.kind in roads.BUILT and not spec.link else 0.
    parking = (1 if one else 2)*roads.PARKING_M if (
        spec.urban and spec.cls in roads.PARKING_CLASSES and spec.kind in roads.BUILT) else 0.
    return spec._replace(width=(f+b+c)*lane_w+2*shoulder+parking, lanes=(f, b, c), lane_w=lane_w,
                         oneway=one, dir=direction, shoulder=shoulder, bias=bias,
                         lane_model=str(feats.attrs.get("osm_lane_model", [""]*len(feats))[i]))


def _distance(points, line):
    """Distance from a source-way end along its short context line, plus distance off the line."""
    ab = np.diff(line[:, :2], axis=0)
    lengths = np.linalg.norm(ab, axis=1)
    arc = np.r_[0., np.cumsum(lengths)]
    out, off = np.empty(len(points)), np.empty(len(points))
    for start in range(0, len(points), 512):
        q = points[start:start+512, :2]
        t = np.clip(((q[:, None]-line[:-1, :2])*ab).sum(2)/np.maximum(lengths**2, 1e-12), 0, 1)
        dist = np.linalg.norm(q[:, None]-line[:-1, :2]-t[..., None]*ab, axis=2)
        k = dist.argmin(1)
        j = np.arange(len(q))
        out[start:start+len(q)] = arc[k]+t[j, k]*lengths[k]
        off[start:start+len(q)] = dist[j, k]
    return out, off


def counts(spec, pts, frame=None):
    base = np.array(spec.lanes, float)
    values = np.tile(base, (len(pts), 1))
    if frame is not None and spec.lane_model:
        data=model(spec.lane_model)
        sections=data.get('sections',())
        if sections:
            ll=np.asarray(data['line'],float)
            line=np.column_stack(frame.geodetic_to_enu(ll[:,1],ll[:,0],0.))
            arc=np.r_[0.,np.cumsum(np.linalg.norm(np.diff(line[:,:2],axis=0),axis=1))]
            at,off=_distance(pts,line)
            profile=np.tile(np.asarray(sections[0]['counts'],float),(len(pts),1))
            for a,b in zip(sections,sections[1:]):
                reach=180. if spec.cls in ('motorway','trunk') else 90.
                before=min(reach,(a['end']-a['start'])*arc[-1]*.45)
                after=min(reach,(b['end']-b['start'])*arc[-1]*.45)
                t=np.clip((at-b['start']*arc[-1]+before)/max(before+after,1e-9),0.,1.)
                profile+=(t*t*(3.-2.*t))[:,None]*(np.asarray(b['counts'])-np.asarray(a['counts']))
            values[off<=20.]=profile[off<=20.]
        chain=data.get('ramp_chain')
        if chain:
            ll=np.asarray(chain['line'],float)
            line=np.column_stack(frame.geodetic_to_enu(ll[:,1],ll[:,0],0.))
            s,off=_distance(pts,line)
            arc=np.r_[0.,np.cumsum(np.linalg.norm(np.diff(line[:,:2],axis=0),axis=1))]
            n=np.full(len(pts),chain['counts'][0],float)
            for boundary,a,b in zip(chain['bounds'],chain['counts'],chain['counts'][1:]):
                at=boundary*arc[-1]
                before=min(180.,at*.8)
                after=min(180.,(arc[-1]-at)*.8)
                t=np.clip((s-at+before)/max(before+after,1e-9),0.,1.)
                n+=(b-a)*t*t*(3.-2.*t)
            # This profile belongs to the whole source chain. A fitted bend can
            # be over 20 m from a sparse raw segment; an off-line cutoff here
            # would abruptly restore its native count in the middle of a taper.
            values[:]=0.
            values[:,0 if spec.dir>=0 else 1]=n
        for end in (() if chain else data.get("ends", ())):
            ll = np.asarray(end["line"], float)
            if len(ll) < 2 or end["reach"] < 1:
                continue
            line = np.column_stack(frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.))
            # Source/Overture geometry may differ slightly, but not by a carriageway's separation.
            s, off = _distance(pts, line)
            t = np.clip(s/end["reach"], 0., 1.)
            # A short source way contributes a small part of the width change; its longer
            # neighbour carries the rest. Short bridge/map fragments cannot compress half
            # of a highway lane change into a few metres.
            blend = end.get('weight', .5)*(1.-t*t*(3.-2.*t))
            blend[off > 20.] = 0.
            values += blend[:, None]*(np.asarray(end["other"])-base)
        for join in model(spec.lane_model).get('joins', ()):
            ll = np.asarray(join['line'],float)
            line = np.column_stack(frame.geodetic_to_enu(ll[:,1],ll[:,0],0.))
            s,off = _distance(pts,line)
            t = np.clip(s/join['reach'],0.,1.)
            blend = 1.-t*t*(3.-2.*t)
            blend[off>20.] = 0.
            values += blend[:,None]*np.asarray(join['delta'])
        for ramp in data.get('ramps',()):
            ll=np.asarray(ramp['line'],float)
            line=np.column_stack(frame.geodetic_to_enu(ll[:,1],ll[:,0],0.))
            s,off=_distance(pts,line)
            t=np.clip(s/ramp['reach'],0.,1.)
            profile=np.asarray(ramp['target'])+(t*t*(3.-2.*t))[:,None]*(
                np.asarray(ramp['native'])-np.asarray(ramp['target']))
            use=(off<=20.)&(s<ramp['reach']+1e-5)
            if chain:
                # The chain may be opening lanes just before another ramp fork.
                # Ease that fork's allocation into the existing profile, rather
                # than replacing it with a native constant at the reach boundary.
                blend=(1.-t*t*(3.-2.*t))[:,None]
                values[use]+=blend[use]*(np.asarray(ramp['target'])-np.asarray(ramp['native']))
            else:values[use]=profile[use]
    return values


def continuations(feats, specs):
    """Repair missing short map fragments and join mismatched counts on a unique continuation.

    Overture can split/conflate a way so the exact OSM match is unavailable on one fragment.
    Infer a missing highway count/surface only when the straight continuation agrees at both
    ends (or a short dead-end fragment has one known continuation). Never cross a ramp branch.
    Any residual width mismatch receives a shared taper, including tagged-to-default joins.
    """
    import geodesy
    import roads
    if not len(feats):
        return specs
    ref = feats.lonlat[0]
    frame = geodesy.EnuFrame(ref[1],ref[0])
    lines,nodes,lengths = {},{},{}
    ramp_nodes=set()
    city_nodes=set()
    for i,sp in enumerate(specs):
        if sp is None or _bridge_way(feats, i):
            # Bridge ways are left to bridge_specs; do not reshape their surface or
            # let their lane tags change neighboring deck cross-sections in this batch.
            continue
        rings = [r for p in feats.parts(i) for r in p if len(r)>1]
        if not rings:
            continue
        ll = max(rings,key=len)
        points = np.column_stack(frame.geodetic_to_enu(ll[:,1],ll[:,0],0.))
        if not sp.link and sp.cls not in ('motorway','trunk'):
            city_nodes.update(tuple(np.rint(p[:2]*2).astype(np.int64)) for p in points)
        if sp.link:
            if sp.oneway and sp.cls in ('motorway','trunk'):
                ramp_nodes.update(tuple(np.rint(p[:2]*2).astype(np.int64)) for p in points[[0,-1]])
        lines[i] = (ll,points)
        lengths[i] = np.linalg.norm(np.diff(points[:,:2],axis=0),axis=1).sum()
        for end,k in ((0,0),(1,-1)):
            key = tuple(np.rint(points[k,:2]*2).astype(np.int64))
            nodes.setdefault(key,[]).append((i,end))
    def along(i,end):
        p = lines[i][1]
        v = p[min(2,len(p)-1),:2]-p[0,:2] if end==0 else p[-1,:2]-p[max(0,len(p)-3),:2]
        return v/max(np.linalg.norm(v),1e-9)
    joins = {}
    # Link-to-link and link-to-highway degree-two joins are continuations too.
    # Excluding all links left multi-lane interstate connectors with square width
    # changes and incorrectly added another auxiliary lane at their end.
    ramp_nodes={key for key in ramp_nodes if len(nodes.get(key,()))>2}
    for node,group in nodes.items():
        for i,end in group:
            sp = specs[i]
            v = along(i,end)*(1 if end==0 else -1)
            choices=[]
            for j,other in group:
                osp = specs[j]
                if sp.link and osp.link and node in city_nodes:continue
                ramp_pair=sp.link and osp.link and len(group)==2 and sp.oneway and osp.oneway
                if i==j or (sp.cls!=osp.cls and not ramp_pair) or sp.oneway!=osp.oneway:
                    continue
                if sp.link!=osp.link and len(group)!=2:
                    continue
                dot = along(i,end)@along(j,other)
                # A degree-two ramp can bend at a source-way boundary. The old
                # 23-degree test left square width steps at 1 -> 2 -> 3 lane
                # turning approaches even though their traffic is continuous.
                minimum=.1 if ramp_pair else .92
                if sp.oneway and dot*sp.dir*osp.dir < (minimum if ramp_pair else .9):
                    continue
                alignment = -(v@along(j,other)*(1 if other==0 else -1))
                if alignment>minimum:
                    choices.append((alignment,j,other,1 if dot>0 else -1))
            choices.sort(reverse=True)
            if choices and (len(choices)==1 or choices[0][0]-choices[1][0]>.02):
                _,j,other,sign=choices[0]
                joins[i,end]=(j,other,sign)
    original=list(specs)
    def known(i,end,field):
        sign,seen,distance=1,{i},0.
        while (i,end) in joins:
            j,other,flip=joins[i,end]
            if j in seen:
                break
            seen.add(j);sign*=flip
            sp=original[j]
            value=getattr(sp,field)
            if value:
                if field=='lanes' and sign<0:value=(value[1],value[0],value[2])
                return value
            distance+=lengths[j]
            if distance>3000. or len(seen)>32:
                break
            i,end=j,1-other
        return None
    for i in lines:
        sp=specs[i]
        if not sp.oneway or sp.cls not in ('motorway','trunk'):
            continue
        changes={}
        for field in ('lanes','surface'):
            if getattr(sp,field):continue
            if field=='lanes' and roads._rule_value(feats.rules.get('width'),i) is not None:
                continue
            a,b=known(i,0,field),known(i,1,field)
            value=a if a and a==b else (a or b) if lengths[i]<180. and ((i,0) not in joins or (i,1) not in joins) else None
            if field=='lanes' and a and b and not value:
                value=min((a,b),key=sum)   # differing ends taper from the narrower through section
            if value:changes[field]=value
        if 'lanes' in changes:
            lane_w=roads.MOTORWAY_LANE_M
            changes.update(lane_w=lane_w,width=sp.width-roads.driving_width(sp)+sum(changes['lanes'])*lane_w)
        if changes:specs[i]=sp._replace(**changes)
    # Smooth the complete ramp chain before solving endpoint sections. Short
    # turn-lane fragments share one taper instead of each compressing a lane
    # addition into half its own length. Never cross a fork or a city junction.
    chain_seen=set()
    def chain_join(i,end):
        key=tuple(np.rint(lines[i][1][0 if end==0 else -1,:2]*2).astype(np.int64))
        return joins.get((i,end)) if len(nodes.get(key,()))==2 else None
    for first in sorted(lines,key=lambda i:str(feats.ids[i])):
        sp=specs[first]
        if first in chain_seen or not sp.link or not sp.oneway:continue
        start=0 if sp.dir>=0 else 1
        chain_first=first;walked=set()
        while chain_join(chain_first,start) is not None:
            j,other,_=chain_join(chain_first,start)
            if j in walked or not specs[j].link or not specs[j].oneway:break
            walked.add(chain_first);chain_first=j;start=1-other
        path=[];owners=[];ns=[];bounds=[];length=0.;i=chain_first;at=start
        while i not in owners:
            if not specs[i].link or not specs[i].oneway:break
            ll=lines[i][0] if at==0 else lines[i][0][::-1]
            if path:bounds.append(length)
            path.extend(ll.tolist() if not path else ll[1:].tolist())
            owners.append(i);ns.append(float(sum(specs[i].lanes or roads.lane_counts(specs[i]))))
            length+=lengths[i]
            next_join=chain_join(i,1-at)
            if next_join is None:break
            j,other,_=next_join
            if joins.get((j,other),())[:2]!=(i,1-at):break
            i,at=j,other
        chain_seen.update(owners)
        if len(owners)<2 or max(ns)-min(ns)<.01 or length<10.:continue
        # Link classes occasionally change at the same continuous ramp. Keep
        # their lane/shoulder cross-section identical at those shared nodes.
        lane_w=max(specs[k].lane_w or (3.6 if specs[k].cls in ('motorway','trunk') else 3.3) for k in owners)
        for k in owners:
            sp=specs[k]
            if sp.lanes is None:
                f,b=roads.lane_counts(sp)
                sp=sp._replace(lanes=(f,b,0) if sp.dir>=0 else (b,f,0))
            data=dict(model(sp.lane_model))
            data['ramp_chain']=dict(line=path,bounds=[v/length for v in bounds],counts=ns)
            specs[k]=sp._replace(lane_w=lane_w,shoulder=roads.EDGE_STRIP_M,bias=0.,
                width=sum(sp.lanes)*lane_w+2*roads.EDGE_STRIP_M,
                lane_model=json.dumps(data,separators=(',',':')))
    # Compute all endpoints before applying corrections, so the result is independent of order.
    corrections={}
    ramp_ends=[]
    for (i,end),(j,other,sign) in sorted(joins.items()):
        if i>j or joins.get((j,other),())[:2]!=(i,end):continue
        a,b=specs[i],specs[j]
        if a.lanes is None and b.lanes is None:continue
        for k in (i,j):
            sp=specs[k]
            if sp.lanes is None:
                f,bk=roads.lane_counts(sp)
                if sp.oneway and sp.dir<0:f,bk=bk,f
                specs[k]=sp._replace(lanes=(f,bk,0),lane_w=roads.driving_width(sp)/max(1,f+bk))
        a,b=specs[i],specs[j]
        ca=counts(a,lines[i][1][[0 if end==0 else -1]],frame)[0]
        cb=counts(b,lines[j][1][[0 if other==0 else -1]],frame)[0]
        if sign<0:cb=cb[[1,0,2]]
        node=tuple(np.rint(lines[i][1][0 if end==0 else -1,:2]*2).astype(np.int64))
        native_a=np.asarray(a.lanes,float)
        native_b=np.asarray(b.lanes if sign>0 else (b.lanes[1],b.lanes[0],b.lanes[2]),float)
        at_ramp=(node in ramp_nodes and a.oneway and a.cls in ('motorway','trunk') and
                 abs(native_a.sum()-native_b.sum())>.5)
        if not at_ramp and np.max(np.abs(ca-cb))<.005:continue
        ra=min(180. if a.cls in ('motorway','trunk') else 90.,lengths[i]*.45)
        rb=min(180. if b.cls in ('motorway','trunk') else 90.,lengths[j]*.45)
        if min(ra,rb)<1.:continue
        # A counted exit/entry lane peels away or joins at the ramp node. Keep
        # the narrower through section there; the ramp band supplies that lane.
        # A symmetric count taper on BOTH sides created a second merge after
        # the gore, overlapping the ramp's own taper and dotted separator.
        target=np.minimum(native_a,native_b) if at_ramp else ca+(cb-ca)*ra/(ra+rb)
        if at_ramp:
            ramp_ends.extend(((i,end,target),(j,other,target if sign>0 else target[[1,0,2]])))
            continue  # the absolute ramp profile owns this correction, once
        for k,e,delta in ((i,end,target-ca),(j,other,(target-cb) if sign>0 else (target-cb)[[1,0,2]])):
            ll=lines[k][0] if e==0 else lines[k][0][::-1]
            corrections.setdefault(k,[]).append(dict(line=ll.tolist(),reach=ra if k==i else rb,delta=delta.tolist()))
    ramp_profiles={}
    for first,end,target in ramp_ends:
        native=np.asarray(specs[first].lanes,float)
        i,at,orientation=first,end,1
        path,owners,seen=[],[],set()
        length=0.
        reach=180.
        while i not in seen:
            seen.add(i)
            ll=lines[i][0] if at==0 else lines[i][0][::-1]
            path.extend(ll.tolist() if not path else ll[1:].tolist())
            owners.append((i,orientation))
            length+=lengths[i]
            if length>=reach or (i,1-at) not in joins:break
            j,other,flip=joins[i,1-at]
            nc=np.asarray(specs[j].lanes if specs[j].lanes is not None else (0,0,0),float)
            if orientation*flip<0:nc=nc[[1,0,2]]
            if np.max(np.abs(nc-native))>.01:break
            far=lines[i][1][-1 if at==0 else 0,:2]
            if tuple(np.rint(far*2).astype(np.int64)) in ramp_nodes:break
            i,at,orientation=j,other,orientation*flip
        reach=min(reach,length*.9)
        if reach<2.:continue
        for i,orientation in owners:
            ramp_profiles.setdefault(i,[]).append(dict(line=path,reach=reach,
                native=(native if orientation>0 else native[[1,0,2]]).tolist(),
                target=(target if orientation>0 else target[[1,0,2]]).tolist()))
    for i in corrections.keys()|ramp_profiles.keys():
        data=dict(model(specs[i].lane_model))
        if i in corrections:data['joins']=corrections[i]
        if i in ramp_profiles:data['ramps']=ramp_profiles[i]
        specs[i]=specs[i]._replace(lane_model=json.dumps(data,separators=(',',':')))
    return specs


def cross(pts):
    import roads
    return pts[:, 3:5] if pts.shape[1] == 5 else roads.offset_polyline(pts, 1.)[:, :2]-pts[:, :2]


def offset(pts, delta):
    out = pts.copy()
    out[:, :2] += cross(pts)*np.asarray(delta)[..., None]
    return out


def pavement(spec, pts, frame=None, left_hand=False):
    """(variable-width ribbon encoded in cross vectors, nominal width), shared with surface.py."""
    import roads
    if spec.lanes is None:
        return roads.pavement_line(spec, pts, left_hand), spec.width
    widths = spec.width + (counts(spec, pts, frame).sum(1)-sum(spec.lanes))*spec.lane_w
    centre = roads.pavement_line(spec, pts, left_hand)
    width = float(max(spec.width, widths.max()))
    return np.c_[centre[:, :3], cross(pts)*(widths/width)[:, None]], width


def pavement_widths(spec, pts, frame=None):
    """Per-node pavement widths for a DecalRoad, retaining shoulders while lane counts taper."""
    if spec.lanes is None:
        return np.full(len(pts), float(spec.width))
    fixed = max(0.0, float(spec.width) - sum(spec.lanes) * spec.lane_w)
    values = counts(spec, pts, frame).sum(axis=1) * spec.lane_w + fixed
    return np.maximum(values, max(1.0, fixed))


def move_roadside(spec, pts, extras, frame):
    """Keep sidewalks, gutters and their seams beside the same gradually changing pavement."""
    if frame is None or not spec.lane_model:
        return extras
    delta = (counts(spec, pts, frame).sum(1)-sum(spec.lanes))*spec.lane_w/2
    return [(look, w, offset(line, look['side']*delta) if 'side' in look else line)
            for look, w, line in extras]


def _runs(line, available):
    """Clip an added divider where its lane tapers into the outer edge, interpolating the tip."""
    current = []
    for k in range(len(line)-1):
        a, b = available[k], available[k+1]
        if a > 0:
            current.append(line[k])
        if (a > 0) != (b > 0):
            current.append(line[k]+(line[k+1]-line[k])*(a/(a-b)))
            if a > 0:
                if len(current) > 1:
                    yield np.asarray(current)
                current = []
    if available[-1] > 0:
        current.append(line[-1])
    if len(current) > 1:
        yield np.asarray(current)


def markings(spec, pts, lon, left_hand=False, frame=None):
    import roads
    values = counts(spec, pts, frame)
    lane_line_w = roads.lane_marking_width(pts, frame)
    f, b, c = values.T
    total = f+b+c
    drive = total*spec.lane_w
    out = []
    flip = -1 if left_hand else 1
    centre = flip*(f-b)*spec.lane_w/2
    if not spec.oneway and spec.cls in roads.CENTRE_CLASSES:
        look = {"m": roads.centre_colour(lon), "tl": roads.CENTRE["tl"], "p": roads.CENTRE["p"]}
        if look['m'] == 'line_white' and not np.any(c):
            out.append((roads.LANE_LINE, lane_line_w, offset(pts, centre)))
        else:
            # A mapped centre turn lane has two boundaries; never count it as a through lane.
            for side in (-1, 1):
                out.append((look, roads.CENTRE["w"], offset(pts, centre+side*(c*spec.lane_w/2+roads.CENTRE["gap"]))))
    if spec.cls in roads.LANE_CLASSES:
        look = {**roads.LANE_LINE, "highway_lane": True} if spec.cls in ("motorway", "trunk") and not spec.link else roads.LANE_LINE
        if spec.oneway:
            n = f+b
            # Anchor separators to the median-side lane boundary, including reversed OSM ways.
            inner = (1 if spec.dir >= 0 else -1)*flip
            for k in range(1, int(np.ceil(n.max()))):
                line = offset(pts, inner*(drive/2-k*spec.lane_w))
                for run in _runs(line, n-k-.015):
                    out.append(({**look,'lane_index':k}, lane_line_w, run))
        else:
            for side, ns in ((-flip, f), (flip, b)):
                for k in range(1, int(np.ceil(ns.max()))):
                    line = offset(pts, centre+side*(c/2+k)*spec.lane_w)
                    for run in _runs(line, ns-k-.015):
                        out.append((look, lane_line_w, run))
    if (not spec.urban or not roads.has_sidewalks(spec)) and spec.cls in roads.EDGE_LINE_CLASSES:
        yellow = (1 if spec.dir >= 0 else -1) if spec.oneway and spec.cls in ("motorway", "trunk") and not spec.link and not left_hand and roads.centre_colour(lon) == "line_yellow" else None
        for side in (-1, 1):
            look = {**roads.EDGE_LINE, "edge_side": side}
            if side == yellow:
                look["m"] = "line_yellow"
            out.append((look, roads.EDGE_LINE["w"], offset(pts, side*drive/2)))
    return out


def grading_spec(spec):
    """Conservatively flatten the entire taper; never leave its widened pavement on a verge."""
    if spec.lanes is None or not spec.lane_model:
        return spec
    largest = max([sum(spec.lanes)] + [sum(e["other"]) for e in model(spec.lane_model).get("ends", ())])
    largest = max([largest]+[sum(s['counts']) for s in model(spec.lane_model).get('sections',())])
    largest = max([largest]+model(spec.lane_model).get('ramp_chain',{}).get('counts',[]))
    largest = max([largest]+[max(sum(s['native']),sum(s['target'])) for s in model(spec.lane_model).get('ramps',())])
    largest += sum(max(0.,sum(j['delta'])) for j in model(spec.lane_model).get('joins',()))
    return spec._replace(width=spec.width+max(0, largest-sum(spec.lanes))*spec.lane_w)
