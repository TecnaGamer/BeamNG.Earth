"""Conservative building clearance for generated roadside route markers."""
import numpy as np


def ring_of(points):
    p=np.asarray(points,float)
    if p.ndim!=2 or len(p)<3 or p.shape[1]<2 or not np.isfinite(p).all():
        return None
    p=p[:,:2]
    p=p[np.linalg.norm(p-np.roll(p,1,axis=0),axis=1)>1e-6]
    return p if len(p)>=3 else None


def inside(point,ring):
    other=np.roll(ring,-1,axis=0)
    dy=other[:,1]-ring[:,1]
    crosses=(ring[:,1]>point[1])!=(other[:,1]>point[1])
    x=ring[:,0]+(point[1]-ring[:,1])*(other[:,0]-ring[:,0])/np.where(abs(dy)>1e-12,dy,1.)
    return bool(np.count_nonzero(crosses & (x>point[0]))%2)


def distance(point,ring):
    edges=np.roll(ring,-1,axis=0)-ring
    t=np.clip(((point-ring)*edges).sum(1)/np.maximum((edges*edges).sum(1),1e-12),0,1)
    return float(np.linalg.norm(point-ring-t[:,None]*edges,axis=1).min())


class Context:
    def __init__(self,rings=(),get_footprints=None):
        self.rings=[ring for raw in rings if (ring:=ring_of(raw)) is not None]
        self.get_footprints=get_footprints

    def clear(self,point,pad=.15):
        if self.get_footprints is not None:
            self.rings.extend(ring for raw in self.get_footprints() if (ring:=ring_of(raw)) is not None)
            self.get_footprints=None
        point=np.asarray(point)[:2]
        for ring in self.rings:
            if np.all(ring.min(0)<=point+pad) and np.all(ring.max(0)>=point-pad):
                if inside(point,ring) or distance(point,ring)<pad:
                    return False
        return True


def for_tile(frame,tx,ty,md,items=(),skip=frozenset()):
    def footprints():
        import buildings
        cover=[]
        for ring in buildings.near_rings(frame,tx,ty,md,margin=56.,skip=skip):
            ring=ring_of(ring)
            if ring is None:continue
            # Pitched procedural houses may fill their enclosing rectangle.
            centre,u,length,width=buildings.oriented_box(ring)
            v=np.array([-u[1],u[0]])
            cover.append(np.array([centre-u*length-v*width,centre+u*length-v*width,
                                   centre+u*length+v*width,centre-u*length+v*width]))
        return cover
    return Context(get_footprints=footprints)
