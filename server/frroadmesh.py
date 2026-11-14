"""French directional/overhead/street scenery using the shared regional atlas.

Capitals use the bundled L1 digitization, also as a visual substitute for L2
on dark faces. Mixed case uses D-DIN, not an official L4 file. No photo textures.
"""
import math
import re
import numpy as np
import dmath
import dnp
import signmesh as sm


def _outline(w,h,z,r):
    out=[]
    for x,zz,start in ((w/2-r,z-h/2+r,-90),(w/2-r,z+h/2-r,0),(-w/2+r,z+h/2-r,90),(-w/2+r,z-h/2+r,180)):
        for a in np.linspace(start,start+90,5):
            a=math.radians(a);out.append((x+r*dmath.cos(a),zz+r*dmath.sin(a)))
    return out


def board(mesh,w,h,z,bg,edge=None,r=.055,body=True):
    """Single tessellated face; no overlapping white/background sheets."""
    edge=(sm.BLACK if bg==sm.WHITE else sm.WHITE) if edge is None else edge
    if body:mesh.panel_body(w,h,z,r)
    rings=[_outline(w-2*m,h-2*m,z,max(.004,r-m)) for m in (0.,.018,.038)]
    for outer,inner,mat in zip(rings,rings[1:],(bg,edge)):
        for i in range(len(outer)):
            j=(i+1)%len(outer)
            mesh.face(mat,[(outer[i][0],-.028,outer[i][1]),(outer[j][0],-.028,outer[j][1]),
                           (inner[j][0],-.028,inner[j][1]),(inner[i][0],-.028,inner[i][1])])
    mesh.face(bg,[(x,-.028,zz) for x,zz in rings[-1]])


def lettering(mesh,text,z,height,width,white=False,x=0.,mixed=False):
    text=sm.clean(text,not mixed).translate(str.maketrans({'’':"'",'‘':"'",'–':'-','‑':'-'}))
    font='din' if mixed else 'l1'
    mesh.text(text,z,-.035,height,width,white=white,uppercase=False,
              x_offset=x,font_key=font,centre_ink=True)


def route_colour(ref):
    ref=ref.upper().replace(' ','')
    return sm.RED if re.fullmatch(r'[AN]\d+[A-Z]?',ref) else sm.GREEN if re.fullmatch(r'E\d+',ref) else sm.YELLOW if ref.startswith('D') else sm.BLUE if ref.startswith('M') else sm.WHITE


def cartouche(mesh,ref,z,x=0.):
    ref=sm.clean(ref)
    if not re.fullmatch(r'[ADNME]\s*\d{1,4}[A-Z]?',ref):return 0.
    bg=route_colour(ref);w=max(.55,sm.measure(ref,'l1')[0]*.15+.14)
    anchor=mesh.offset.copy();mesh.offset=anchor+dnp.matmul(np.array([x,0,0]), mesh.axes)
    board(mesh,w,.25,z,bg,r=.02)
    lettering(mesh,ref,z,.15,w-.12,white=bg in (sm.RED,sm.GREEN,sm.BLUE))
    mesh.offset=anchor
    return w


def motorway_marker(mesh, position, travel, route, pr, bottom, width):
    """A double-faced E52a plate with a red crown and one slim support."""
    radius = width / 2
    seam = bottom + .34
    angles = np.linspace(0., math.pi, 17)

    def outline(r, low):
        return [(-r, low), (r, low)] + [
            (r * dmath.cos(a), seam + r * dmath.sin(a)) for a in angles
        ]

    outer = outline(radius, bottom)
    inner = outline(radius - .012, bottom + .012)
    mesh.transform(position, travel)
    for a, b in zip(outer, outer[1:] + outer[:1]):
        mesh.face(sm.METAL, [(a[0], -.027, a[1]), (a[0], .027, a[1]),
                             (b[0], .027, b[1]), (b[0], -.027, b[1])])
    post_top = seam + .05
    mesh.box(sm.METAL, (0., 0., post_top / 2), (.026, .021, post_top / 2))
    for axis in (travel, -travel):
        mesh.transform(position, axis)
        for i, a in enumerate(outer):
            j = (i + 1) % len(outer)
            mesh.face(sm.WHITE, [(x, -.027, z) for x, z in (a, outer[j], inner[j], inner[i])])
        mesh.face(sm.WHITE, [(x, -.027, z) for x, z in (*inner[:3], inner[-1])])
        mesh.face(sm.RED, [(x, -.027, z) for x, z in inner[2:]])
        lettering(mesh, route, seam + .076, .100, width - .09, white=True)
        lettering(mesh, pr, bottom + .166, .168, width - .055)


def arrow(mesh,x,z,height=.65,exit=False,side=1):
    """Concave arrow built from separate convex shaft and head."""
    y=-.038
    if exit:
        # Diagonal upward exit arrow: rotate the shape in its own plane.
        a=side*math.pi/4
    else:a=math.pi  # straight down toward a lane
    points=[(-.075,0),(.075,0),(.075,height*.58),(-.075,height*.58)]
    head=[(-.24,height*.52),(.24,height*.52),(0,height)]
    for polygon in (points,head):
        pp=[(x+xx*dmath.cos(a)+zz*dmath.sin(a),y,z-xx*dmath.sin(a)+zz*dmath.cos(a))for xx,zz in polygon]
        mesh.face(sm.WHITE,pp)


def direction(mesh,position,travel,labels,arrow_side='right',route='',body=True,start_row=0,colour='white'):
    mesh.transform(position,travel)
    for row,label in enumerate(labels[:4]):
        label=sm.clean(label,False);mixed=not label.isupper() and any(c.islower() for c in label)
        # Local destinations preserve source case; capitals remain capitals.
        key='din' if mixed else 'l1'
        w=max(1.2,min(4.2,sm.measure(label,key)[0]*.18+.48));h=.39
        z=2.55-(start_row+row)*.46;side=1 if arrow_side=='right' else -1
        bg={'white':sm.WHITE,'blue':sm.BLUE,'green':sm.GREEN}.get(colour,sm.WHITE)
        border=sm.BLACK if bg==sm.WHITE else sm.WHITE
        p=[(-w/2,z-h/2),(w/2-.20,z-h/2),(w/2,z),(w/2-.20,z+h/2),(-w/2,z+h/2)]
        if side<0:p=[(-x,zz)for x,zz in reversed(p)]
        if body:
            mesh.face(sm.METAL,[(x,.025,zz)for x,zz in reversed(p)])
            for i,(x,zz)in enumerate(p):
                xx,zzz=p[(i+1)%len(p)];mesh.face(sm.METAL,[(x,-.025,zz),(x,.025,zz),(xx,.025,zzz),(xx,-.025,zzz)])
        # Convex arrow outline: border ring around a smaller interior, coplanar.
        inner=[(x*.975,z+(zz-z)*.80)for x,zz in p]
        for i,(x,zz) in enumerate(p):
            j=(i+1)%len(p)
            mesh.face(border,[(x,-.028,zz),(p[j][0],-.028,p[j][1]),(inner[j][0],-.028,inner[j][1]),(inner[i][0],-.028,inner[i][1])])
        mesh.face(bg,[(x,-.028,zz)for x,zz in inner])
        lettering(mesh,label,z,.18,w-.48,white=bg!=sm.WHITE,x=-side*.065,mixed=mixed)
    if route and start_row==0:cartouche(mesh,route,2.99)


def distance_label(metres, estimated=False):
    km = metres / 1000.
    if estimated:
        step = .5 if km < 5. else 1.
        km = math.floor(km / step + .5) * step
    return f'{km:g}'.replace('.', ',')


def local_layout(panels):
    """Panel widths are also used to check clearance before making the mesh."""
    rows = []
    for panel in panels:
        if panel['turn'] not in ('left', 'right'):
            continue
        mixed = panel.get('local', False)
        values = panel.get('display_distance', ())
        names = {}
        for i, name in enumerate(panel['names']):
            name = sm.clean(name, not mixed)
            if name:
                names.setdefault(name, values[i] if i < len(values) else '')
        key = 'din' if mixed else 'l1'
        labels = list(names)
        for start in range(0, len(labels), 2):
            group = labels[start:start+2]
            distances = [names[name] for name in group]
            distance_w = .15 + .155 * max(sm.measure(d, 'l1')[0] for d in distances) if any(distances) else 0.
            width = max(1.02, min(3.15, .45 + distance_w + .155 * max(sm.measure(s, key)[0] for s in group)))
            rows.append((panel, group, width, .32 if len(group) == 1 else .49, mixed, distances, distance_w))
    rows = rows[:4]
    refs = {p.get('ref', '') for p, *_ in rows}
    ref = next(iter(refs)) if len(refs) == 1 else ''
    return rows, sm.valid_route(ref), max((r[2] for r in rows), default=0.)


def local_direction(mesh, position, travel, layout, yield_top=None):
    """One driver-facing stack of D21-style chevrons."""
    rows, ref, _ = layout
    mesh.transform(position, travel)
    z = 1.78 if yield_top is None else yield_top + .12
    for panel, names, w, h, mixed, distances, distance_w in reversed(rows):
        centre = z + h / 2
        bg = {'blue': sm.BLUE, 'green': sm.GREEN}.get(panel.get('colour'), sm.WHITE)
        ink = sm.BLACK if bg == sm.WHITE else sm.WHITE
        side = 1 if panel['turn'] == 'right' else -1
        tip = min(.24, h * .60)
        edge = [(-w/2, -h/2), (w/2-tip, -h/2), (w/2, 0.),
                (w/2-tip, h/2), (-w/2, h/2)]
        if side < 0:
            edge = [(-x, y) for x, y in reversed(edge)]
        inner = [(x * (1-.05/w) - (side*.083 if abs(x) == w/2 and y == 0. else 0.),
                  y * (1-.05/h)) for x, y in edge]
        mesh.face(sm.METAL, [(x,.028,centre+y) for x,y in reversed(edge)])
        for (x,y), (xx,yy), (a,b), (aa,bb) in zip(edge, edge[1:]+edge[:1], inner, inner[1:]+inner[:1]):
            mesh.face(sm.METAL, [(x,-.028,centre+y),(x,.028,centre+y),
                                 (xx,.028,centre+yy),(xx,-.028,centre+yy)])
            mesh.face(ink, [(x,-.028,centre+y),(xx,-.028,centre+yy),
                            (aa,-.028,centre+bb),(a,-.028,centre+b)])
        mesh.face(bg, [(x,-.028,centre+y) for x,y in inner])
        text_x = -.058 * side
        for i, (name, distance) in enumerate(zip(names, distances)):
            zz = centre + (.105-.21*i if len(names) == 2 else 0.)
            lettering(mesh, name, zz, .155, w-.42-distance_w, white=bg != sm.WHITE,
                      x=text_x-side*distance_w/2, mixed=mixed)
            if distance:
                x = text_x + side * ((w-.42)/2-distance_w/2)
                lettering(mesh, distance, zz, .155, distance_w-.09, white=bg != sm.WHITE, x=x)
        z += h + .032
    if ref:
        cartouche(mesh, ref, z + .125)
        z += .25
    # A shared support starts inside the existing yield pole, above the ground.
    base = 0.0 if yield_top is None else yield_top - .23
    offset, half = (.062, .046) if yield_top is None else (0.0, .032)
    mesh.box(sm.METAL, (0., offset, (z + base) / 2), (half, half, (z - base) / 2))


def roundabout_layout(panels):
    exits = {}
    for panel in panels:
        if 'exit' not in panel:
            continue
        key = panel['turn']
        if key in exits and panel['exit'] != exits[key]['exit']:
            continue
        current = exits.setdefault(key, dict(panel, names=[]))
        current['names'] = list(dict.fromkeys(current['names'] + panel['names']))[:2]
    if len(exits) < 2:
        return None
    longest = max(sm.measure(sm.clean(name), 'l1')[0] for p in exits.values() for name in p['names'])
    return exits, min(3.65, max(2.78, 1.62 + longest * .16)), 1.92


def roundabout(mesh, position, travel, layout):
    """A diagrammatic advance board, built with the same lettering and metal as local signs."""
    exits, w, h = layout
    bottom = 1.48
    mesh.transform(position, travel)
    board(mesh, w, h, bottom + h / 2, sm.WHITE, r=.035)
    cx = .31 if 'left' in exits and 'right' not in exits else -.31 if 'right' in exits and 'left' not in exits else 0.
    cz = bottom + .68
    ring = np.array([cx, cz])

    def ink(points):
        p = np.asarray(points)
        area = np.sum(p[:, 0] * np.roll(p[:, 1], -1) - p[:, 1] * np.roll(p[:, 0], -1))
        if area < 0.:
            p = p[::-1]
        mesh.face(sm.BLACK, [(x, -.035, z) for x, z in p])

    def stroke(a, b, half):
        n = b - a
        n = np.array([-n[1], n[0]]) * (half / max(dnp.norm(n), .001))
        ink([a - n, b - n, b + n, a + n])

    radius = .34
    radians = np.deg2rad(np.linspace(-90., 203., 36))
    circle = np.c_[dnp.cos(radians), dnp.sin(radians)]
    for a, b in zip(circle, circle[1:]):
        ink([ring + a*radius, ring + b*radius, ring + b*.237, ring + a*.237])
    stroke(np.array([cx, bottom + .043]), ring + np.array([0., -.27]), .042)
    for turn, panel in exits.items():
        horizontal = -1. if turn == 'left' else 1. if turn == 'right' else 0.
        if horizontal:
            # Preserve the side of the real exit, with enough space for the label block.
            dy = .24 if abs(panel.get('angle', 90.)) < 75. else .05
            ray = np.array([horizontal, dy])
            ray /= dnp.norm(ray)
            end = ring + ray * .69
            label_x = horizontal * (w/2 - .68)
            label_z = bottom + 1.16
            limit = min(1.17, w*.42)
        else:
            ray = np.array([0., 1.])
            end = ring + ray * .69
            label_x, label_z, limit = cx, bottom + 1.65, w - .4 - abs(cx)
        shaft = end - ray * .10
        normal = np.array([-ray[1], ray[0]]) * .105
        stroke(ring + ray*.275, shaft, .042)
        ink([shaft + normal, shaft - normal, end])
        for i, name in enumerate(panel['names']):
            zz = label_z - i*.19
            bg = {'green': sm.GREEN, 'blue': sm.BLUE}.get(panel.get('colour'))
            if bg is not None:
                mesh.face(bg, [(label_x-limit/2,-.033,zz-.088),(label_x+limit/2,-.033,zz-.088),
                               (label_x+limit/2,-.033,zz+.088),(label_x-limit/2,-.033,zz+.088)])
            lettering(mesh, name, zz, .138, limit-.09, white=bg is not None,
                      x=label_x, mixed=panel.get('local', False))
    for x in (-w*.29, w*.29):
        top = bottom + h - .09
        mesh.box(sm.METAL, (x, .078, top/2), (.055, .054, top/2))


def street_layout(name):
    text=sm.clean(name,True);words=text.split()
    heads={'RUE','AVENUE','BOULEVARD','PLACE','IMPASSE','CHEMIN','ROUTE','ALLÉE','ALLEE','QUAI','PASSAGE','SQUARE'}
    prefix=words[0] if words and words[0] in heads else ''
    rest=' '.join(words[1:]) if prefix else text
    lines=[rest];parts=rest.split()
    if sm.measure(rest,'din')[0]*.072>1.12 and len(parts)>1:
        cut=min(range(1,len(parts)),key=lambda i:max(sm.measure(' '.join(parts[:i]),'din')[0],sm.measure(' '.join(parts[i:]),'din')[0]))
        lines=[' '.join(parts[:cut]),' '.join(parts[cut:])]
    w=max(.42,min(1.25,max(sm.measure(line,'din')[0]for line in lines)*.085+.10))
    h=(.34 if prefix else .27)if len(lines)>1 else (.27 if prefix else .21)
    return prefix,lines,w,h


def street(mesh,position,travel,name,centre_height=2.30,body=True):
    mesh.transform(position,travel)
    prefix,lines,w,h=street_layout(name)
    z=centre_height
    board(mesh,w,h,z,sm.BLUE,r=.018,body=body)
    if prefix:lettering(mesh,prefix,z+(.115 if len(lines)>1 else .070),.045,w-.09,True,mixed=True)
    for i,line in enumerate(lines):
        zz=z+(.015 if prefix else .048)-i*.095 if len(lines)>1 else z-.035 if prefix else z
        lettering(mesh,line,zz,.064 if len(lines)>1 else .072,w-.09,True,mixed=True)
    return w


def sos(mesh,position,travel):
    """Original orange French SOS silhouette; no invented operator or ID."""
    mesh.transform(position,travel)
    orange=len(sm.MATERIALS)
    mesh.materials=sm.MATERIALS+['bne_fr_road_orange']
    mesh.box(orange,(0,0,.88),(.18,.15,.88))
    mesh.box(sm.WHITE,(0,0,1.77),(.18,.15,.055))
    mesh.box(sm.BLACK,(0,-.153,1.12),(.115,.005,.19))
    for z in (1.02,1.20):mesh.rect(sm.WHITE,.07,.06,z,-.160,.007)
    mesh.text('SOS',1.76,-.160,.072,.30,uppercase=False,font_key='din',centre_ink=True)
    mesh.rect(sm.WHITE,.24,.025,.57,-.155,.005)


def guide_panel(mesh,content,w,h,z,exit=False,distance='',side=1):
    # An exit is white unless an explicit motorway reference makes it blue.
    refs=content.get('routes',[])
    motorway=any(re.fullmatch(r'A\s*\d+[A-Z]?',r,re.I)for r in refs)
    ordinary_route=any(re.fullmatch(r'[NDM]\s*\d+[A-Z]?',r,re.I)for r in refs)
    bg=sm.BLUE if motorway else sm.WHITE if exit else sm.GREEN if ordinary_route else sm.BLUE
    board(mesh,w,h,z,bg,r=.10)
    names=content.get('names',[])[:3] or (['SORTIE '+content['ref']] if content.get('ref') else refs)
    top=z+h/2
    # A dedicated lower band keeps arrows and distances clear of every name.
    pitch=min(.62,max(.30,(h-1.65)/max(len(names),1)))
    cap=min(.42,pitch*.70)
    for i,name in enumerate(names):lettering(mesh,name,top-.55-i*pitch,cap,w-.60,bg!=sm.WHITE)
    if bg==sm.WHITE:
        # Black arrow on a white panel, without changing shared materials.
        start=len(mesh.tm);arrow(mesh,side*(w/2-.55),z-h/2+.38,.68,True,side)
        mesh.tm[start:]=[sm.BLACK]*len(mesh.tm[start:])
    else:arrow(mesh,0,z-h/2+(1.03 if not exit else .40),.65,exit,side)
    if distance:lettering(mesh,distance,z-h/2+.22,.20,w-.40,bg!=sm.WHITE,mixed=True)
    # Route cartouches above each block; uniformly sized, never US shields.
    valid=[r for r in refs[:2] if re.fullmatch(r'[ADNME]\s*\d{1,4}[A-Z]?',sm.clean(r))]
    sizes=[max(.55,sm.measure(sm.clean(r),'l1')[0]*.15+.14)for r in valid]
    cursor=-(sum(sizes)+.045*max(0,len(sizes)-1))/2
    for ref,rw in zip(valid,sizes):
        cartouche(mesh,ref,top+.17,cursor+rw/2);cursor+=rw+.045
    if exit and content.get('ref'):
        lettering(mesh,'SORTIE '+content['ref'],top-.21,.17,w-.30,bg!=sm.WHITE)


def build_highways(exits,overheads,origin):
    mesh=sm.Mesh(origin)
    for sign in exits:
        travel=np.asarray(sign['travel']);p=np.asarray(sign['centre'],float)
        mesh.transform(p,travel)
        label=sign['label'];kind=sign['kind']
        if kind=='ramp':
            w,h=1.55,.65;bottom=2.1
            board(mesh,w,h,bottom+h/2,sm.WHITE)
            lettering(mesh,'SORTIE '+label.get('ref',''),bottom+h/2,.19,w-.2)
        else:
            w=min(6.,max(2.7,max((sm.measure(n,'l1')[0]*.42+.65 for n in label.get('names',())),default=2.7)))
            h=max(2.45,1.65+min(3,len(label.get('names',())))*.62);bottom=2.2
            guide_panel(mesh,label,w,h,bottom+h/2,True,sign.get('distance',''),sign.get('side',1))
        for x in (-w*.30,w*.30):mesh.box(sm.METAL,(x,.12,(bottom+h)/2),(.045,.045,(bottom+h)/2))
    for sign in overheads:
        p=np.asarray(sign['centre'],float);mesh.transform(p,np.asarray(sign['travel']))
        bottom=float(sign['bottom']-p[2]);panels=sorted(sign['panels'],key=lambda v:v.get('offset',0.))
        # Keep the source placement's pavement clearance, but use compact FR faces.
        widths=[min(6.,max(3.,max((sm.measure(n,'l1')[0]*.42+.7 for n in v['label'].get('names',())),default=3.))) for v in panels]
        total=sum(widths)+.25*(len(widths)-1);cursor=-total/2
        maxh=0.
        anchor=mesh.offset.copy()
        for panel,w in zip(panels,widths):
            h=max(2.45,1.65+.62*min(3,len(panel['label'].get('names',()))))
            maxh=max(maxh,h);x=cursor+w/2;cursor+=w+.25
            mesh.offset=anchor+dnp.matmul(np.array([x,0,0]), mesh.axes)
            guide_panel(mesh,panel['label'],w,h,bottom+h/2,panel['exit'],sign.get('distance','')if panel['exit']else'',sign.get('side',1))
        mesh.offset=anchor
        # Columns retain the generated assembly's off-carriageway positions.
        beam=bottom+maxh-.25;left,right=sign['poles']
        for x,base in zip((left,right),sign['bases']):
            start=float(base-p[2]);height=beam+.5-start
            mesh.box(sm.METAL,(x,.55,start+height/2),(.16,.16,height/2))
        mesh.box(sm.METAL,((left+right)/2,.55,beam),((right-left)/2,.25,.20))
    if not mesh.t:return None
    return np.asarray(mesh.p),np.asarray(mesh.t),np.asarray(mesh.uv),sm.MATERIALS,np.asarray(mesh.tm)
