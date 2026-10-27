"""Traffic-facing roadside panels and mapped lamp meshes in the sign stream."""
import math
import re

import numpy as np

import exitsigns as signs
import signroads
import throughsigns

QUERY_MARGIN = 100.
MATERIALS = signs.GUIDE_MATERIALS + ['bne_roadside_blue_letters', 'bne_roadside_lamp_glow']


def dimensions(extra):
    if extra['type'] == 'speed':
        return 1.05, 2.65 if extra.get('truck') else 1.55
    if extra['type'] == 'adopt':
        return 1.65, 2.35
    return max(1.05, 1.15*len(extra['routes'])), 1.55


def _number(value, default, low, high):
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(m|ft|\')?\s*', str(value or ''))
    n = float(match[1])*(.3048 if match[2] in ('ft', "'") else 1.) if match else default
    return min(high, max(low, n))


def source_pavements(frame, obj, context, left_hand=False):
    extra = obj['roadside']
    points = extra.get('approach', [[obj['lon'], obj['lat']]])
    return context.pavements(frame, np.asarray(points), left_hand)


def place(frame, obj, pavements, box, elev, chunks=(), left_hand=False):
    extra = obj['roadside']
    point = signs._xy(frame, [[obj['lon'], obj['lat']]])[0]
    x0, y0, x1, y1 = box
    if not (x0-QUERY_MARGIN <= point[0] < x1+QUERY_MARGIN and y0-QUERY_MARGIN <= point[1] < y1+QUERY_MARGIN):
        return None
    polygons = [signs._outline(r[2], r[3]) for r in pavements]
    if extra['type'] == 'lamp':
        # Preserve mapped locations; a pole inside generated asphalt is omitted.
        if not (x0 <= point[0] < x1 and y0 <= point[1] < y1):
            return None
        tags = extra['tags']
        high = (tags.get('lamp_mount') == 'high_mast' or tags.get('lamp_type') == 'high_mast' or
                tags.get('tower:type') == 'lighting')
        height = _number(tags.get('height', tags.get('light:height')), 35. if high else 9., 3., 60.)
        high = high or height >= 20
        radius = .65 if high else .25
        if not signs._clear(point, radius, polygons) or signs._on_deck(point, chunks, radius+2):
            return None
        near = signs._nearest(point, pavements)
        toward = near[6]-point if near else np.array([0., 1.])
        toward /= max(float(np.linalg.norm(toward)), 1e-6)
        try:
            angle = math.radians(float(tags.get('light:direction', '')))
            toward = np.array([math.sin(angle), math.cos(angle)])
        except ValueError:
            pass
        return dict(id=obj['id'], type='lamp', centre=[*point, signs._ground_z(frame, elev, point)],
                    travel=(-toward).tolist(), height=height, high=high,
                    count=int(_number(tags.get('light:count'), 8 if high else 1, 1, 12)),
                    glowing=tags.get('light:lit') not in ('no', 'disused'))

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
        if travel@direction >= .8:
            choices.append((near[0], str(fid), near, travel, width))
    if not choices:
        return None
    _, _, near, travel, width = min(choices, key=lambda v: (v[0], v[1]))
    if signs._on_deck(near[6], chunks, 2.):
        return None
    right = np.array([travel[1], -travel[0]])*(-1 if left_hand else 1)
    cross = signroads.cross(near[3])
    k, t = near[4:6]
    edge = abs((cross[k]*(1-t)+cross[k+1]*t)@right)*width/2
    w, h = dimensions(extra)
    centre = None
    # Entire panel and both supports beyond all generated paved surfaces.
    for offset in np.arange(edge+w/2+1.5, edge+w/2+13., 1.):
        candidate = near[6]+right*offset
        if signs._clear(candidate, w/2+.35, polygons) and not signs._on_deck(candidate, chunks, w/2+1):
            centre = candidate
            break
    if centre is None or not (x0 <= centre[0] < x1 and y0 <= centre[1] < y1):
        return None
    z = signs._ground_z(frame, elev, centre)
    offsets = [-w*.32, w*.32] if extra['type'] in ('speed', 'adopt') else [0.]
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

    def blue_text(self, value, y, bottom, height, width):
        start = len(self.mi)
        self.guide_text(value, y, bottom, height, width, black=True)
        self.mi[start:] = [9]* (len(self.mi)-start)

    def support(self, sign):
        for y, z in zip(sign['posts'], sign['bases']):
            self.box((-.13,y-.032,z-.15),(-.045,y+.032,sign['bottom']+sign['height']-.08),2)

    def speed_panel(self, sign):
        b, w, h = sign['bottom'], sign['width'], sign['height']
        self.white_panel(w,b,h)
        self.guide_text('SPEED',0,b+h-.36,.23,w-.15,black=True)
        self.guide_text('LIMIT',0,b+h-.69,.23,w-.15,black=True)
        self.guide_text(str(sign['speed']),0,b+h-1.36,.53,w-.16,black=True)
        if sign.get('truck'):
            self.guide_text('TRUCKS',0,b+.92,.22,w-.15,black=True)
            self.guide_text(str(sign['truck']),0,b+.16,.53,w-.16,black=True)

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

    def adopt_panel(self, sign):
        b, w = sign['bottom'], sign['width']
        self.white_panel(w,b+.91,1.44,4)
        self.white_panel(w,b,.88,4)
        self.blue_text('ADOPT-A-HIGHWAY',0,b+2.10,.145,w-.15)
        self.rectangle(w-.17,b+2.02,.018,.071,4)
        self.blue_text('MDOT',0,b+1.01,.17,.72)
        # Evergreen silhouettes and a curved divided road, like the Michigan panel.
        for y, z, size in ((-.53,b+1.2,.78),(-.31,b+1.27,.61),(.54,b+1.3,.72)):
            self.rectangle(.035,z,.35,.071,0,y)
            for level, wide in ((.18,.23),(.34,.19),(.50,.14)):
                self.triangle([(.071,y-wide*size,z+level*size),(.071,y+wide*size,z+level*size),
                               (.071,y,z+(level+.36)*size)],0)
        for yoff in (-.12,.12):
            ts = np.linspace(0,1,18)
            curve = np.array([(1-t)**2*np.array([-.22,1.22])+2*t*(1-t)*np.array([-.05,1.73])+t*t*np.array([.33,1.84]) for t in ts])
            curve += [yoff,b]
            for i, (a,c) in enumerate(zip(curve[:-1],curve[1:])):
                width = .052*(1-ts[i])+.008
                self.quad([(.071,a[0]-width,a[1]),(.071,a[0]+width,a[1]),
                           (.071,c[0]+width*.93,c[1]),(.071,c[0]-width*.93,c[1])],6)
        for text, offset, size in (('NEXT 2 MILES',.62,.17),('COMMUNITY',.36,.16),('VOLUNTEERS',.12,.16)):
            self.blue_text(text,0,b+offset,size,w-.15)

    def lamp(self, sign):
        h = sign['height']
        # Tapered octagonal steel mast with a foundation and top ring.
        radius = .28 if sign['high'] else .10
        self.box((-.38,-.38,-.12),(.38,.38,.20),2)
        for a,b in zip(np.linspace(0,2*math.pi,9)[:-1],np.linspace(0,2*math.pi,9)[1:]):
            self.quad([(radius*math.cos(a),radius*math.sin(a),.15),
                       (radius*math.cos(b),radius*math.sin(b),.15),
                       (radius*.38*math.cos(b),radius*.38*math.sin(b),h),
                       (radius*.38*math.cos(a),radius*.38*math.sin(a),h)],2)
        glow = 10 if sign.get('glowing', True) else 1
        count = sign['count']
        if sign['high']:
            radius = 1.65
            ring = [np.array([radius*math.cos(a),radius*math.sin(a),h-.7]) for a in np.linspace(0,2*math.pi,13)]
            for a,b in zip(ring[:-1],ring[1:]):
                self.bar(a,b,.075)
            for a in np.linspace(0,2*math.pi,count,endpoint=False):
                x,y = radius*math.cos(a),radius*math.sin(a)
                self.bar((0,0,h-.7),(x,y,h-.7),.055)
                self.box((x-.33,y-.23,h-1.05),(x+.33,y+.23,h-.8),2)
                self.quad([(x-.29,y-.19,h-1.055),(x-.29,y+.19,h-1.055),
                           (x+.29,y+.19,h-1.055),(x+.29,y-.19,h-1.055)],glow)
        else:
            for i in range(min(count,2)):
                side = 1 if i == 0 else -1
                self.bar((0,0,h-.6),(side*1.7,0,h+.12),.045)
                self.box((side*1.7-.45,-.18,h-.03),(side*1.7+.45,.18,h+.18),2)
                self.quad([(side*1.7-.4,-.14,h-.035),(side*1.7-.4,.14,h-.035),
                           (side*1.7+.4,.14,h-.035),(side*1.7+.4,-.14,h-.035)],glow)

    def object(self, sign):
        self.centre = np.asarray(sign['centre'],float)
        travel = np.asarray(sign['travel'],float)
        self.face = np.r_[-travel,0.]
        self.right = np.array([travel[1],-travel[0],0.])
        if sign['type'] == 'lamp':
            self.lamp(sign)
            return
        self.support(sign)
        {'speed':self.speed_panel,'shield':self.route_panel,'adopt':self.adopt_panel}[sign['type']](sign)

    def result(self):
        if not self.t:
            return None
        return np.asarray(self.p),np.asarray(self.t),np.asarray(self.uv),MATERIALS,np.asarray(self.mi)


def build(objects, origin):
    mesh = Mesh(origin)
    for obj in objects:
        mesh.object(obj)
    return mesh.result()
