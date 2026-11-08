"""Standalone Interstate/US shields in the existing optional sign stream."""
import numpy as np
import exitsigns as signs
import signroads
import roads
import throughsigns
import highwayroutes

QUERY_MARGIN = 100.


def panel_layout(extra):
    sizes=[]
    for route in extra['routes']:
        route=highwayroutes.current_panel(route)
        template=highwayroutes.registry()['templates'].get(route.get('template'),{})
        aspect=template.get('aspect',1.)
        sizes.append((max(1.,aspect),max(1.,1/aspect)) if route['kind']=='state' or template.get('image') else (1.,1.13))
    total=sum(w for w,h in sizes)+.15*max(0,len(sizes)-1)
    cursor=-total/2
    layout=[]
    for w,h in sizes:
        layout.append((cursor+w/2,w,h));cursor+=w+.15
    return layout,total


def dimensions(extra):
    layout,total=panel_layout(extra)
    top=0.
    for route,(_,_,height) in zip(extra['routes'],layout):
        route=highwayroutes.current_panel(route)
        template=highwayroutes.registry()['templates'].get(route.get('template'),{})
        direction_bottom=max(1.18,height+.05)
        top=max(top,height,direction_bottom+.36 if route.get('direction') else 0.,
                direction_bottom+.70 if route.get('modifier') and template.get('modifier_plaque',True) else 0.)
    return max(1.05,total),top+.04


def source_pavements(frame, obj, context, left_hand=False):
    return context.pavements(frame, np.asarray(obj['roadside']['approach']), left_hand)


def place(frame, obj, pavements, box, elev, chunks=(), left_hand=False, obstacles=None):
    extra = obj['roadside']
    point = signs._xy(frame, [[obj['lon'], obj['lat']]])[0]
    x0, y0, x1, y1 = box
    if not (x0-QUERY_MARGIN <= point[0] < x1+QUERY_MARGIN and y0-QUERY_MARGIN <= point[1] < y1+QUERY_MARGIN):
        return None
    polygons = signs._outlines(pavements)
    walks=[]
    for _,sp,line,width in pavements:
        for side in getattr(sp,'sides',()):
            cross=signroads.cross(line)*side
            inner=line[:,:2]+cross*width/2
            outer=line[:,:2]+cross*(width/2+sp.walk+1.)
            walks.append(np.vstack([inner,outer[::-1],inner[:1]]))
    if walks:
        polygons=signroads.Outlines([*polygons,*walks])
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
    # Suppress a station beside any crossing or driveway rather than moving it
    # into a corner. Same-axis continuation segments are harmless.
    for row in pavements:
        if signs._way_id(row[0]) == extra['main_id']:
            continue
        other = signs._nearest(near[6], [row])
        if other is not None and other[0] < 30. and abs(other[7]@travel) < .85:
            return None
    if signs._on_deck(near[6], chunks, 2.):
        return None
    right = np.array([travel[1], -travel[0]])*(-1 if left_hand else 1)
    cross = signroads.cross(near[3])
    k, t = near[4:6]
    edge = abs((cross[k]*(1-t)+cross[k+1]*t)@right)*width/2
    w, h = dimensions(extra)
    centre = None
    # Entire panel and both supports beyond all generated paved surfaces.
    candidates = ([point] if extra.get('mapped_position') else
                  [near[6]+right*offset for offset in np.arange(edge+w/2+3., edge+w/2+13., 1.)])
    for candidate in candidates:
        if (signs._clear(candidate, w/2+.35, polygons) and not signs._on_deck(candidate, chunks, w/2+1)
                and (obstacles is None or obstacles.clear(candidate,w/2+.5))):
            centre = candidate
            break
    if centre is None or not roads.box_owns(box, centre[0], centre[1]):
        return None
    z = signs._ground_z(frame, elev, centre)
    offsets = [y for y,_,_ in panel_layout(extra)[0]]
    bases = [signs._ground_z(frame, elev, centre+right*off)-z for off in offsets]
    road_z = signs._ground_z(frame, elev, near[6])-z
    ground = [signs._ground_z(frame,elev,centre+right*off)-z for off in (-w/2,w/2)]
    if not np.isfinite([z,road_z,*bases,*ground]).all() or abs(ground[1]-ground[0])>w*.6:
        return None
    return dict(extra, id=obj['id'], centre=[*centre, z], travel=travel.tolist(), width=w, height=h,
                bottom=max(1.75+max(bases+ground), road_z+1.6), bases=bases, posts=offsets)


class Mesh(throughsigns.Mesh):
    def __init__(self, origin=(0.,0.,0.), out_dir=None, vfs_dir=None, faces=None):
        super().__init__(origin)
        self.out_dir, self.vfs_dir = out_dir, vfs_dir
        self.faces = faces if faces is not None else {}
        self.materials = list(signs.GUIDE_MATERIALS)

    def route_shield(self, route, bottom_offset=0.):
        template=highwayroutes.registry()['templates'].get(route.get('template'),{})
        if route['kind'] != 'state' and (not route.get('modifier') or
                (template.get('renderer')=='existing_mesh' and route.get('modifier') in template.get('modifiers',()))):
            return super().route_shield(dict(route,number=route.get('display_number',route['number'])),bottom_offset)
        y,b,w,h=route['y'],route['bottom']+bottom_offset,route['width'],route['height']
        if self.out_dir is not None:
            import routefaces
            name,texture=routefaces.face(route,self.out_dir,self.vfs_dir)
            self.faces[name]=texture
            if name not in self.materials:
                self.materials.append(name)
            # One front, with a metal backing added by route_panel.
            ring=self.outline(w,b,h,.06,offset=y)
            profile=routefaces.contour(route.get('template')) if not route.get('modifier') or route.get('modifier') in template.get('modifiers',()) else None
            if profile is not None:
                ring=profile[0]*[w,h]+[y,b]
                triangles=profile[1]
            else:
                triangles=[(0,i,i+1) for i in range(1,len(ring)-1)]
            material=self.materials.index(name)
            for tri in triangles:
                points=ring[list(tri)]
                self.triangle([(.067,*p) for p in points],material)
                self.uv[-3:]=[((float(p[0])-y)/w+.5,(float(p[1])-b)/h) for p in points]
        else:
            self.white_panel(w,b,h)
            self.guide_text(route.get('network','US:'+route.get('state','')),y,b+h*.76,h*.12,w*.9,black=True)
            self.guide_text(route['number'],y,b+h*.25,h*.4,w*.8,black=True)

    def result(self):
        result=super().result()
        return (*result[:3],self.materials,result[4]) if result is not None else None

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
        for route,(y,width,height) in zip(routes,panel_layout(sign)[0]):
            route=highwayroutes.current_panel(route)
            # Solid back and edge for a standalone shield (existing guide shields were face-only).
            start = len(self.p)
            template=highwayroutes.registry()['templates'].get(route.get('template'),{})
            self.route_shield(dict(route,y=y,bottom=b,width=width,height=height))
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
            modifier = route.get('modifier')
            direction_bottom=b+max(1.18,height+.05)
            if modifier and template.get('modifier_plaque',True):
                self.white_panel(1.,direction_bottom+.40,.30)
                self.guide_text(modifier,0.,direction_bottom+.46,.16,.9,black=True)
            if direction:
                if route['kind'] == 'interstate':
                    self.white_panel(1.,direction_bottom,.36,border=1,fill=4)
                    self.guide_text(direction,0.,direction_bottom+.07,.19,.86)
                else:
                    self.white_panel(1.,direction_bottom,.36)
                    self.guide_text(direction,0.,direction_bottom+.07,.19,.86,black=True)
            self.centre = anchor

    def object(self, sign):
        self.centre = np.asarray(sign['centre'],float)
        travel = np.asarray(sign['travel'],float)
        self.face = np.r_[-travel,0.]
        self.right = np.array([travel[1],-travel[0],0.])
        self.support(sign)
        self.route_panel(sign)

def build(objects, origin, out_dir=None, vfs_dir=None, faces=None):
    mesh = Mesh(origin,out_dir,vfs_dir,faces)
    for obj in objects:
        mesh.object(obj)
    return mesh.result()
