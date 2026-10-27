"""Photo-matched merge warnings in the grass between an entrance ramp and freeway.

Uses the renderer's source geometry and pavement, with no new map provider or
per-tile fitting. Placement never changes roads or their physics.
"""
import logging
import math
import time
from concurrent.futures import CancelledError

import numpy as np

import exitsigns
import signstates
import signcontext
import roadlayoutcache
import roads
import signroads

SIDE = 1.22
SPAN = SIDE*math.sqrt(2.)
BOTTOM = 1.8
CLEARANCE = .65
CLOSE_POST_STATES = {'IN','OH','KY','TN','AZ','OK','KS'}
MATERIALS = ['bne_merge_yellow','bne_merge_black','bne_exit_metal']
_layouts = roadlayoutcache.LayoutCache(max_entries=32,max_bytes=16*1024*1024)
_warned = -float('inf')


def post_offsets(state):
    gap = 1.02 if state == 'MI' else .76 if state in CLOSE_POST_STATES else 0.
    return (-gap/2,gap/2) if gap else (0.,)


def _rows(frame, feats, urban, widths, left_hand):
    rows=[]
    for i,sp in enumerate(roads.road_specs(feats,urban,widths,left_hand)):
        if sp is None or sp.kind not in roads.BUILT: continue
        for ring in roads.ground_parts(feats,i):
            p=np.column_stack(frame.geodetic_to_enu(ring[:,1],ring[:,0],0.))
            if len(p)<2: continue
            ss,line,width=signroads.outline(sp,p,frame,left_hand,signroads.is_link(feats,i))
            rows.append((str(feats.ids[i]),ss,line,width))
    return rows


def _place_source(frame, feats, urban, widths, left_hand, owner=None):
    rows=_rows(frame,feats,urban,widths,left_hand)
    mains=[r for r in rows if not r[1].link and r[1].oneway and r[1].cls in ('motorway','trunk')]
    if not mains: return []
    polygons=[exitsigns._outline(r[2],r[3]) for r in rows]
    result=[]
    indices={str(fid):i for i,fid in enumerate(feats.ids)}
    for fid,sp,line,width in rows:
        if owner is not None and fid!=owner:continue
        if not sp.link or not sp.oneway or sp.cls not in ('motorway','trunk'): continue
        # Reject partial ground pieces at abutments: only the true source end
        # in travel direction can be an entrance connection.
        index=indices[fid]
        full=[r for part in feats.parts(index) for r in part]
        ends=[r[-1 if sp.dir>=0 else 0] for r in full]
        end_ll=np.asarray(ends)
        end_xy=np.column_stack(frame.geodetic_to_enu(end_ll[:,1],end_ll[:,0],0.))[:,:2]
        ramp=line if sp.dir>=0 else line[::-1]
        if np.linalg.norm(end_xy-ramp[-1,:2],axis=1).min()>.1: continue
        back=ramp[::-1,:2]
        join=back[0]
        length=exitsigns._station(back,1e12)[2]
        if length<35.: continue
        incoming=join-exitsigns._station(back,min(25.,length))[0]
        incoming/=max(np.linalg.norm(incoming),1e-9)
        joins=[]
        for row in mains:
            near=exitsigns._nearest(join,[(*row[:3],0.)])
            travel=near[7]*(row[1].dir or 1)
            if near[0]<1. and travel@incoming>.65:
                joins.append((row,travel))
        if not joins: continue  # exits, city terminals and flyovers do not qualify
        direction=sorted(joins,key=lambda item:item[0][0])[0][1]
        ramp_poly=exitsigns._outline(line,width)
        radius=SPAN/2+CLEARANCE
        placed=None
        # Start just upstream of the pavement convergence and move back until
        # the complete diamond and posts fit in the grass. No shoulder fallback.
        for distance in np.arange(20.,min(280.,length)+.01,4.):
            q=exitsigns._station(back,distance)[0]
            choices=[]
            for row in mains:
                near=exitsigns._nearest(q,[(*row[:3],0.)])
                travel=near[7]*(row[1].dir or 1)
                if travel@direction<.85 or abs((q-near[6])@travel)>2.:continue
                if (join-near[6])@direction < 10.:continue
                choices.append((near[0],row[0],row,near,travel))
            if not choices: continue
            _,_,row,near,travel=min(choices,key=lambda v:(v[0],v[1]))
            mainline=row[2];k,t=near[4],near[5]
            at=mainline[k]*(1-t)+mainline[k+1]*t
            right=np.array([travel[1],-travel[0]])
            side=1 if (q-at[:2])@right>=0 else -1
            outward=right*side
            cross=signroads.cross(mainline)
            edge=abs((cross[k]*(1-t)+cross[k+1]*t)@outward)*row[3]/2
            spans=[v for v in exitsigns._spans(ramp_poly,at[:2],travel,outward) if v[1]>edge]
            if not spans:continue
            inner=min(v[0] for v in spans)
            if inner-edge<2*radius:continue
            # Stay beside the mainline, as in the reference, rather than the
            # outer ramp shoulder; leave equal space in a narrow grass strip.
            offset=min(edge+radius+.35,(edge+inner)/2)
            centre=at[:2]+outward*offset
            if not exitsigns._clear(centre,radius,polygons):continue
            la,lo,_=frame.enu_to_geodetic(*centre,0.)
            state=str(signstates.states(la,lo))
            placed=dict(id=fid,xy=centre,travel=travel,side=side,state=state,
                        offsets=post_offsets(state),road_point=at[:2])
            break
        if placed is not None:result.append(placed)
    return result


def plans(frame,cells,urban=None,widths=None,left_hand=False,box=None,context=None):
    """One source neighbourhood per ramp; adjacent tiles agree on its owner."""
    source=signcontext.merge(cells)
    result=[]
    for i,fid in enumerate(source.ids):
        if not signroads.is_link(source,i):continue
        rings=[r for part in source.parts(i) for r in part if len(r)>=2]
        if not rings:continue
        points=np.concatenate(rings)
        ends=np.concatenate([r[[0,-1]] for r in rings])
        if box is not None:
            e,n,_=frame.geodetic_to_enu(ends[:,1],ends[:,0],0.)
            if not np.any((e>=box[0]-320)&(e<=box[2]+320)&(n>=box[1]-320)&(n<=box[3]+320)):continue
        full=context.around(points) if context is not None else source
        owner=getattr(urban,'__self__',None)
        variant=('merge-signs',urban,widths,bool(left_hand),getattr(owner,'epoch',0),str(fid))
        got=_layouts.get(frame,[full],variant,
            lambda:_place_source(frame,full,urban,widths,left_hand,str(fid)))
        result.extend(got)
    return result


def place(frame,cells,box,elev,chunks=(),urban=None,widths=None,left_hand=False,context=None):
    result=[]
    for plan in plans(frame,cells,urban,widths,left_hand,box,context):
        xy=plan['xy']
        if not (box[0]<=xy[0]<box[2] and box[1]<=xy[1]<box[3]):continue
        if exitsigns._on_deck(xy,chunks,SPAN/2+CLEARANCE):continue
        right=np.array([plan['travel'][1],-plan['travel'][0]])
        bases=[exitsigns._ground_z(frame,elev,xy+right*s) for s in plan['offsets']]
        ground=exitsigns._ground_z(frame,elev,xy)
        road=exitsigns._ground_z(frame,elev,plan['road_point'])
        if not np.isfinite([ground,road,*bases]).all() or max(bases)-min(bases)>1.2:continue
        result.append(dict(plan,centre=np.r_[xy,ground],bases=bases,
                           bottom=max(max(bases)+BOTTOM,road+1.6)))
    return result


def _diamond(side,margin=0.):
    ring=exitsigns.Mesh.outline(side-2*margin,-side/2+margin,side-2*margin,max(.01,.045-margin))
    return ring@np.array([[1.,1.],[-1.,1.]])/math.sqrt(2.)


def _symbol(side=1):
    # The supplied warning face: broad straight arrow and curved joining stem.
    ring=np.array([[-.075,-.47],[.075,-.47],[.075,-.19],[.22,-.36],[.29,-.29],
                   [.18,-.16],[.12,-.06],[.085,.06],[.075,.22],[.21,.18],
                   [.235,.205],[.015,.57],[-.015,.57],[-.235,.205],[-.21,.18],[-.075,.22]])
    ring[:,0]*=side
    return ring


class Mesh(exitsigns.Mesh):
    def panel(self,sign):
        self.centre=np.asarray(sign['centre']);travel=sign['travel']
        self.face=np.r_[-travel,0.];self.right=np.r_[travel[1],-travel[0],0.]
        bottom=sign['bottom']-self.centre[2];zc=bottom+SPAN/2
        for offset,base in zip(sign['offsets'],sign['bases']):
            low=base-self.centre[2]-.2
            # Galvanized channel posts; the recessed web and folded flanges are
            # actual geometry. Punched holes remain visible below the panel.
            top=zc+SPAN/2-abs(offset)-.10
            for y in (offset-.031,offset+.026):
                self.box((-.10,y,low),(-.045,y+.005,top),2)
            for z in np.arange(low,top,.0508):
                end=min(top,z+.0508);hole=min(end,z+.009)
                self.quad([(-.075,offset-.026,z),(-.075,offset-.0045,z),(-.075,offset-.0045,hole),(-.075,offset-.026,hole)],2)
                self.quad([(-.075,offset+.0045,z),(-.075,offset+.026,z),(-.075,offset+.026,hole),(-.075,offset+.0045,hole)],2)
                if end>hole:self.quad([(-.075,offset-.026,hole),(-.075,offset+.026,hole),(-.075,offset+.026,end),(-.075,offset-.026,end)],2)
        rings=[_diamond(SIDE,m)+[0.,zc] for m in (0.,.014,.034)]
        outer=rings[0]
        for a,b in zip(outer,np.roll(outer,-1,axis=0)):
            self.quad([(-.04,*a),(-.04,*b),(.01,*b),(.01,*a)],2)
        for i in range(1,len(outer)-1):self.triangle([(-.04,*outer[0]),(-.04,*outer[i+1]),(-.04,*outer[i])],2)
        for outside,inside,mat in zip(rings,rings[1:],(0,1)):
            for a,b,c,d in zip(outside,np.roll(outside,-1,axis=0),np.roll(inside,-1,axis=0),inside):
                self.quad([(.01,*a),(.01,*b),(.01,*c),(.01,*d)],mat)
        # Partition the yellow field around the black symbol at one depth.
        # No stacked decal sheets to flicker or turn the whole sign black at distance.
        symbol=_symbol(sign['side'])+[0.,zc]
        inner=rings[-1]
        levels=np.unique(np.r_[inner[:,1],symbol[:,1]])
        for low,high in zip(levels[:-1],levels[1:]):
            mid=(low+high)/2;edges=[]
            for poly in (inner,symbol):
                for a,b in zip(poly,np.roll(poly,-1,axis=0)):
                    if min(a[1],b[1])<mid<max(a[1],b[1]):
                        def x(z,a=a,b=b):return a[0]+(z-a[1])*(b[0]-a[0])/(b[1]-a[1])
                        edges.append((x(mid),x(low),x(high)))
            edges.sort()
            for k,(a,b) in enumerate(zip(edges,edges[1:])):
                self.quad([(.01,a[1],low),(.01,b[1],low),(.01,b[2],high),(.01,a[2],high)],k%2)

    def result(self):
        mesh=super().result()
        if mesh is None:return None
        p,t,uv,_,mi=mesh
        # Strip collapsed scanline corner triangles, preserving every visible face.
        area=np.linalg.norm(np.cross(p[t[:,1]]-p[t[:,0]],p[t[:,2]]-p[t[:,0]]),axis=1)
        keep=area>1e-12
        return p,t[keep],uv,MATERIALS,mi[keep]


def build(signs,origin):
    mesh=Mesh(origin)
    for sign in signs:mesh.panel(sign)
    return mesh.result()


def build_tile(frame,tx,ty,cells,elev,chunks=(),urban=None,widths=None,left_hand=False,context=None):
    global _warned
    try:
        box=roads.tile_bbox(tx,ty)
        return build(place(frame,cells,box,elev,chunks,urban,widths,left_hand,context),(box[0],box[1],0.))
    except CancelledError:
        raise
    except Exception as error:
        now=time.monotonic()
        if now-_warned>=60.:
            _warned=now
            logging.getLogger(__name__).warning('Merge signs unavailable; roads continue: %s: %s',type(error).__name__,error)
        return None
