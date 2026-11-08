"""Lazy building-footprint clearance for optional service boards."""
import numpy as np
import exitsigns


class Context:
    def __init__(self, rings=(), get_footprints=None):
        self.rings = list(rings)
        self.get_footprints = get_footprints

    def clear(self, point, pad=.15):
        if self.get_footprints is not None:
            self.rings = list(self.get_footprints())
            self.get_footprints = None
        polygons = []
        for ring in self.rings:
            ring = np.asarray(ring,float)[:,:2]
            if len(ring)<3 or not np.isfinite(ring).all(): continue
            if not np.array_equal(ring[0],ring[-1]): ring=np.vstack([ring,ring[0]])
            polygons.append(ring)
        return exitsigns._clear(np.asarray(point,float)[:2],pad,polygons)


def for_tile(frame,tx,ty,md,items=()):
    import buildings
    return Context(get_footprints=lambda: buildings.near_rings(frame,tx,ty,md,margin=56.))
