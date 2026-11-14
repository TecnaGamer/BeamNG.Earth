"""Standalone Interstate/US shields in the existing optional sign stream."""
import numpy as np
import dnp
import exitsigns as signs
import signroads
import roads
import throughsigns

QUERY_MARGIN = 100.


def dimensions(extra):
    return max(1.05, 1.15*len(extra['routes'])), 1.55


def source_pavements(frame, obj, context, left_hand=False):
    return context.pavements(frame, np.asarray(obj['roadside']['approach']), left_hand)


def place(frame, obj, pavements, box, elev, chunks=(), left_hand=False):
    extra = obj['roadside']
    point = signs._xy(frame, [[obj['lon'], obj['lat']]])[0]
    x0, y0, x1, y1 = box
    if not (x0-QUERY_MARGIN <= point[0] < x1+QUERY_MARGIN and y0-QUERY_MARGIN <= point[1] < y1+QUERY_MARGIN):
        return None
    polygons = signs._outlines(pavements)
    approach = signs._xy(frame, extra['approach'])
    source_near = signs._nearest(point, [('', None, approach, 0)])
    if source_near is None:
        return None
    direction = source_near[7]
    choices = []
    for row in pavements:
        fid, sp, line, width = row
        if sp.link:
            continue
        wid = signs._way_id(fid)
        if wid is not None and wid != extra['main_id']:
            continue
        near = signs._nearest(point, [row])
        if near is None or near[0] > 30:
            continue
        travel = near[7]*(sp.dir or 1) if sp.oneway else signs._travel(near, direction)
        if dnp.matmul(travel, direction) >= .8:
            choices.append((near[0], str(fid), near, travel, width))
    if not choices:
        return None
    _, _, near, travel, width = min(choices, key=lambda v: (v[0], v[1]))
    if signs._on_deck(near[6], chunks, 2.):
        return None
    right = np.array([travel[1], -travel[0]])*(-1 if left_hand else 1)
    cross = signroads.cross(near[3])
    k, t = near[4:6]
    edge = abs(dnp.matmul(cross[k]*(1-t)+cross[k+1]*t, right))*width/2
    w, h = dimensions(extra)
    centre = None
    # Entire panel and both supports beyond all generated paved surfaces.
    for offset in np.arange(edge+w/2+1.5, edge+w/2+13., 1.):
        candidate = near[6]+right*offset
        if signs._clear(candidate, w/2+.35, polygons) and not signs._on_deck(candidate, chunks, w/2+1):
            centre = candidate
            break
    if centre is None or not roads.box_owns(box, centre[0], centre[1]):
        return None
    z = signs._ground_z(frame, elev, centre)
    offsets = [0.]
    bases = [signs._ground_z(frame, elev, centre+right*off)-z for off in offsets]
    road_z = signs._ground_z(frame, elev, near[6])-z
    return dict(extra, id=obj['id'], centre=[*centre, z], travel=travel.tolist(), width=w, height=h,
                bottom=max(1.75+max(bases), road_z+1.6), bases=bases, posts=offsets)


class Mesh(throughsigns.Mesh):
    def white_panel(self, width, bottom, height, border=6, fill=1):
        self.rounded(width, bottom, height, .06, .05, 2, back=True, front=False)
        rings = [self.outline(width-2*m, bottom+m, height-2*m, max(.015,.06-m)) for m in (0., .025, .045)]
        for outer, inner, colour in zip(rings, rings[1:], (fill, border)):
            for a, b, c, d in zip(outer, np.roll(outer,-1,axis=0), np.roll(inner,-1,axis=0), inner):
                self.quad([(.05,*a),(.05,*b),(.05,*c),(.05,*d)], colour)
        inner = rings[-1]
        for i in range(1,len(inner)-1):
            self.triangle([(.05,*p) for p in inner[[0,i,i+1]]], fill)

    def support(self, sign):
        for y, z in zip(sign['posts'], sign['bases']):
            self.box((-.13,y-.032,z-.15),(-.045,y+.032,sign['bottom']+sign['height']-.08),2)

    def route_panel(self, sign):
        b = sign['bottom']
        routes = sign['routes']
        for j, route in enumerate(routes):
            y = (j-(len(routes)-1)/2)*1.15
            # Solid back and edge for a standalone shield (existing guide shields were face-only).
            start = len(self.p)
            self.route_shield(dict(route,y=y,bottom=b,width=1.,height=1.13))
            end = len(self.p)
            triangles = [tri for tri in self.t if min(tri) >= start and max(tri) < end]
            shift = -.12*self.face
            # Only solid face triangles form the backing; lettering is excluded.
            for tri in triangles:
                pts = [np.asarray(self.p[i])+self.origin for i in tri]
                index = len(self.p)
                self.p.extend([p+shift-self.origin for p in pts[::-1]])
                self.t.append((index,index+1,index+2)); self.mi.append(2); self.uv.extend([(0.,0.)]*3)
            anchor = self.centre.copy()
            self.centre = anchor+self.right*y
            direction = route.get('direction')
            if direction:
                if route['kind'] == 'interstate':
                    self.white_panel(1.,b+1.18,.36,border=1,fill=4)
                    self.guide_text(direction,0.,b+1.25,.19,.86)
                else:
                    self.white_panel(1.,b+1.18,.36)
                    self.guide_text(direction,0.,b+1.25,.19,.86,black=True)
            self.centre = anchor

    def object(self, sign):
        self.centre = np.asarray(sign['centre'],float)
        travel = np.asarray(sign['travel'],float)
        self.face = np.r_[-travel,0.]
        self.right = np.array([travel[1],-travel[0],0.])
        self.support(sign)
        self.route_panel(sign)

def build(objects, origin):
    mesh = Mesh(origin)
    for obj in objects:
        mesh.object(obj)
    return mesh.result()
