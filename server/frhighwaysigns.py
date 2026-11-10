"""French motorway sign faces and connected supports, composed in metres.

One plan supplies both the placement envelope and the rendered geometry. Unknown
colours/distances remain unknown; explicit register data takes precedence over
the conservative generated layout. No US sign dimensions or square filler faces.
"""
import copy
import re

import numpy as np

import signmesh as sm
import frdestinations

GAP = .065
MATERIALS = sm.MATERIALS + ['bne_fr_highway_blue', 'bne_fr_highway_green', 'bne_fr_highway_glyph_yellow']
BLUE, GREEN, YELLOW_GLYPH = len(sm.MATERIALS), len(sm.MATERIALS)+1, len(sm.MATERIALS)+2


def full_label(label):
    if not label or not label.get('fr_full_names'):
        return label
    label = dict(label, names=label['fr_full_names'])
    if label.get('fr_full_distances'):
        label['distances'] = label['fr_full_distances']
    return label


def face_font(colour):
    return 'fr_motorway' if colour in ('blue', 'green') else 'l1'


def refs(content):
    result = []
    for value in content.get('routes', ()):
        value = re.sub(r'\s+', '', sm.clean(value))
        if re.fullmatch(r'[ADNME]\d{1,4}[A-Z]?', value) and value not in result:
            result.append(value)
    return result[:4]


def legend_shape(value, font):
    """Unit-cap glyph positions for legends containing raised small letters."""
    source = frdestinations.legend_chars(value)
    if not any(scale != 1. for _, scale in source):
        return None
    chars = sm.font_text(''.join(c for c, _ in source), font)
    face = sm.face_font(font)
    stretch, spacing = face['horizontal_scale'], face['letter_spacing']
    pairs = face.get('kerning', {})
    cursor, previous, old_scale = 0., '', 1.
    shapes, left, right = [], float('inf'), -float('inf')
    for char, (_, scale) in zip(chars, source):
        cursor += pairs.get(previous+char, 0.)*stretch*min(scale, old_scale)
        glyph = sm.glyph_for(char, font)
        l, b, r, t = glyph['bounds']
        if r > l and t > b:
            shapes.append((glyph, cursor, scale))
            left = min(left, cursor+l*stretch*scale)
            right = max(right, cursor+r*stretch*scale)
        cursor += (glyph['advance']+spacing)*stretch*scale
        previous, old_scale = char, scale
    return shapes, left, right


def legend_width(value, font):
    shaped = legend_shape(value, font)
    return shaped[2]-shaped[1] if shaped else sm.measure(value, font)[0]


def registers(content, exit=False):
    """Explicit registers first; otherwise separate listed green/local poles."""
    supplied = content.get('fr_blocks')
    if supplied:
        return [dict(names=[sm.clean(n) for n in b.get('names', ()) if sm.clean(n)],
                     colour=b.get('colour', 'white'), italic=bool(b.get('italic')),
                     distances=b.get('distances', ())) for b in supplied if b.get('names')]
    names = [sm.clean(n) for n in content.get('names', ()) if sm.clean(n)]
    if content.get('route_only'):
        return []                 # a current route number is not a city destination
    if not names:
        return []
    colour = content.get('colour')
    if colour not in ('blue', 'green', 'white', 'yellow'):
        routes = refs(content)
        if exit and not any(r.startswith('A') for r in routes):
            greens = [n for n in names[:6] if frdestinations.is_green(n)]
            whites = [n for n in names[:6] if not frdestinations.is_green(n)]
            return [dict(names=group, colour=colour, italic=False, distances=())
                    for group, colour in ((greens, 'green'), (whites, 'white')) if group]
        colour = 'blue'
    return [dict(names=names[:6], colour=colour, italic=False,
                 distances=content.get('distances', ()))]


def compose(content, exit=False, distance='', lanes=2, roadside=False, confirmation=False, side=1):
    content = full_label(content)
    blocks = registers(content, exit)
    if not blocks and not (exit and content.get('ref')):
        return None
    lanes = max(1, min(5, int(lanes)))
    cap = .36 if exit or roadside else .40
    margin = .24
    widths = []
    for b in blocks:
        font = face_font(b['colour'])
        text = max(legend_width(n, font)*cap for n in b['names'])
        widths.append(text + margin*2 + (.9 if confirmation and b['distances'] else 0.))
    width = max(2.8, max(widths, default=0.), lanes*3.25 if not exit and not roadside else 0.)
    width = min(7.2 if exit or roadside else 16., width)
    if max(widths, default=0.) > width:
        cap *= max(.70, (width-margin*2)/(max(widths)-margin*2))
    pitch = cap*1.55
    arrow_band = .90 if not exit and not confirmation and not roadside else 0.
    panels = []
    # Named registers have no empty footer: the exit arrow belongs in the header.
    for i, b in enumerate(blocks):
        height = margin*2+len(b['names'])*pitch-(pitch-cap)
        if i == len(blocks)-1:
            height += arrow_band
        panels.append(dict(b, height=height, cap=cap, margin=margin,
                           arrow_lanes=lanes if arrow_band and i == len(blocks)-1 else 0))
    header = bool(exit and (content.get('ref') or distance or not roadside))
    if header:
        panels.insert(0, dict(colour='white', names=[], height=.58, header=True,
                             ref=sm.clean(content.get('ref', '')), distance=distance,
                             exit_arrow=not bool(distance), side=side))
    route_refs = refs(content)
    route_height = .44 if route_refs else 0.
    height = sum(p['height'] for p in panels)+GAP*max(0, len(panels)-1)
    cursor = height
    for p in panels:
        p['top'], p['bottom'] = cursor, cursor-p['height']
        cursor -= p['height']+GAP
    return dict(width=width, height=height+route_height, panels=panels,
                body_height=height, routes=route_refs, cap=cap, side=side,
                exit=exit, roadside=roadside, confirmation=confirmation, lanes=lanes)


def lane_count(pavements, q, travel, main_ids, tags=None):
    import throughsigns
    raw = str((tags or {}).get('lanes', ''))
    if re.fullmatch(r'[1-5]', raw):
        return int(raw)
    candidate = throughsigns._candidate(pavements, q, travel, main_ids)
    if candidate is None:
        return 2
    # Fallback from the rendered full ribbon (paved shoulders included).
    return max(1, min(5, round((candidate[4]-5.)/3.5)))


def planned_panels(content, exit_label, lanes, distance, side):
    panels = []
    for label, is_exit in ((content, False), (exit_label, True)):
        if not label:
            continue
        plan = compose(label, is_exit, distance if is_exit else '', lanes=lanes, side=side)
        if plan:
            panels.append(dict(label=label, exit=is_exit, side=side, fr_plan=plan,
                               width=plan['width'], height=plan['height'], fr_lanes=lanes))
    return panels


def finalize(sign):
    """Retain planned lateral offsets after merging/repositioning assemblies."""
    sign = copy.deepcopy(sign)
    panels = []
    for p in sign['panels']:
        p['fr_plan'] = compose(p['label'], p['exit'], sign.get('distance', '') if p['exit'] else '',
            lanes=p.get('fr_lanes', sign.get('fr_lanes', 2)), side=p.get('side', sign.get('side', 1)))
        if p['fr_plan']:
            p['width'], p['height'] = p['fr_plan']['width'], p['fr_plan']['height']
            panels.append(p)
    sign['panels'] = panels
    if not panels:
        return None
    # A mainline board spans the traffic lanes; exits sit beyond its right edge.
    main = [p for p in panels if not p['exit']]
    exits = [p for p in panels if p['exit']]
    if len(main) > 1:
        names, routes = [], []
        for p in main:
            names.extend(n for n in p['label'].get('names', ()) if n not in names)
            routes.extend(r for r in p['label'].get('routes', ()) if r not in routes)
        first = main[0]
        first['label'] = dict(first['label'], names=names[:6], routes=routes)
        first['label'].pop('fr_blocks', None)
        plan = compose(first['label'], lanes=first.get('fr_lanes', 2))
        first.update(fr_plan=plan, width=plan['width'], height=plan['height'])
        main = [first]
        panels = main+exits
        sign['panels'] = panels
    for p in main:
        p['offset'] = 0.
    extent = max((p['width']/2 for p in main), default=0.)
    cursors = {-1: -extent, 1: extent}
    for p in sorted(exits, key=lambda p: (p.get('side', 1), str(p['label']))):
        side = p.get('side', 1)
        if main:
            p['offset'] = cursors[side]+side*(.30+p['width']/2)
            cursors[side] += side*(.30+p['width'])
    # If the assembly contains only exit panels, center their combined width.
    if not main:
        width = sum(p['width'] for p in exits)+.3*max(0, len(exits)-1)
        shift = (exits[0].get('side', 1)*(exits[0].get('fr_lanes', 2)-1)*3.25/2
                 if len(exits) == 1 else 0.)
        cursor = -width/2+shift
        for p in exits:
            p['offset'] = cursor+p['width']/2
            cursor += p['width']+.3
    return sign


def colour_index(colour):
    return {'blue': BLUE, 'green': GREEN, 'white': sm.WHITE, 'yellow': sm.YELLOW}.get(colour, sm.WHITE)


def text(mesh, value, x, z, cap, width, colour='white', left=False, italic=False):
    font = face_font(colour)
    shaped = legend_shape(value, font)
    ink = legend_width(value, font)*cap
    if left:
        x += min(ink, width)/2
    start = len(mesh.p)
    if shaped:
        shapes, lbound, rbound = shaped
        height = min(cap, width/max(rbound-lbound, .01))
        stretch = sm.face_font(font)['horizontal_scale']
        origin = x-(lbound+rbound)*height/2
        material = sm.GLYPH_WHITE if colour in ('blue', 'green') else sm.GLYPH
        for glyph, cursor, scale in shapes:
            l, b, r, t = glyph['bounds']
            u0, v0, u1, v1 = glyph['uv']
            baseline = z+height/2-height*scale
            left_x = origin+(cursor+l*stretch*scale)*height
            right_x = origin+(cursor+r*stretch*scale)*height
            low_z, high_z = baseline+b*height*scale, baseline+t*height*scale
            mesh.face(material, [(left_x,-.045,low_z),(right_x,-.045,low_z),
                                 (right_x,-.045,high_z),(left_x,-.045,high_z)],
                      [(u0,v0),(u1,v0),(u1,v1),(u0,v1)])
    else:
        mesh.text(value, z, -.045, cap, width, white=colour in ('blue', 'green'),
                  font_key=font, x_offset=x, centre_ink=True, uppercase=False)
    if italic:
        # A geometric slant is an illustration of L4, not a supplied L4 font.
        inv = np.linalg.inv(mesh.axes)
        p = (np.asarray(mesh.p[start:])-mesh.offset)@inv
        p[:, 0] += .18*(p[:, 2]-z)
        mesh.p[start:] = list(p@mesh.axes+mesh.offset)


def arrow(mesh, x, z, height=.68, side=0, colour='blue'):
    """Broad French lane arrow; side=0 points down, ±1 points to an exit."""
    scale = height/.8
    shaft = [(-.085, .32), (.085, .32), (.085, .78), (-.085, .78)]
    head = [(-.34, .38), (0., 0.), (.34, .38)]
    angle = side*np.pi/4 if side else 0.
    rot = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    mat = sm.WHITE if colour in ('blue', 'green') else sm.BLACK
    for poly in (shaft, head):
        p = np.asarray(poly)*scale
        if side:
            p -= [0., .4*scale]
            p = p@rot.T
            p += [0., .4*scale]
        mesh.face(mat, [(x+a, -.050, z+b) for a, b in p])


def exit_badge(mesh, ref, x, z):
    import frroadmesh as fr
    width = max(.88, .55+sm.measure(ref, 'l1')[0]*.23)
    anchor = mesh.offset.copy()
    mesh.offset = anchor+np.array([x, -.012, 0.])@mesh.axes
    fr.board(mesh, width, .38, z, sm.WHITE, edge=sm.BLACK, r=.18)
    # Original fork pictogram, kept separate from its number.
    left = -width/2+.13
    for pts in ([(0., -.12), (.04, -.12), (.04, .11), (0., .11)],
                [(.065, -.12), (.105, -.12), (.105, -.01), (.21, .10), (.18, .14), (.065, .015)],
                [(.15, .14), (.26, .14), (.26, .035)]):
        mesh.face(sm.BLACK, [(left+a, -.049, z+b) for a, b in pts])
    text(mesh, ref, .14, z, .23, width-.46)
    mesh.offset = anchor


def draw(mesh, plan, x=0., bottom=0.):
    import frroadmesh as fr
    anchor = mesh.offset.copy()
    mesh.offset = anchor+np.array([x, 0., 0.])@mesh.axes
    width = plan['width']
    for p in plan['panels']:
        z = bottom+(p['top']+p['bottom'])/2
        fr.board(mesh, width, p['height'], z, colour_index(p['colour']),
                 edge=sm.WHITE if p['colour'] in ('blue', 'green') else sm.BLACK, r=.035)
        if p.get('header'):
            if p['ref']:
                badge_width = max(.88, .55+sm.measure(p['ref'], 'l1')[0]*.23)
                exit_badge(mesh, p['ref'], -width/2+.16+badge_width/2, z)
            if p['distance']:
                text(mesh, p['distance'], width/2-.65, z, .24, 1.05)
            elif p['exit_arrow']:
                arrow(mesh, p['side']*(width/2-.45), z-.23, .48, p['side'], 'white')
            continue
        if p['colour'] == 'yellow' and len(p['names']) == 1 and re.fullmatch(r'S\s*\d+', p['names'][0]):
            # Alternate itinerary: a yellow S-number in a black inset.
            cap = p['cap']
            chip = max(1.1, sm.measure(p['names'][0], 'fr_motorway')[0]*cap+.22)
            local = mesh.offset.copy()
            mesh.offset = local+np.array([-width/2+.10+chip/2, -.010, 0.])@mesh.axes
            fr.board(mesh, chip, p['height']-.18, z, sm.BLACK, edge=sm.BLACK, r=.012)
            start = len(mesh.tm)
            mesh.text(p['names'][0], z, -.045, cap, chip-.12, white=True,
                      font_key='fr_motorway', centre_ink=True)
            mesh.tm[start:] = [YELLOW_GLYPH if m == sm.GLYPH_WHITE else m for m in mesh.tm[start:]]
            mesh.offset = local
            continue
        cap = p['cap']
        for i, name in enumerate(p['names']):
            baseline = bottom+p['top']-p['margin']-cap/2-i*cap*1.55
            if plan['confirmation'] and i < len(p.get('distances', ())):
                text(mesh, name, -width/2+.24, baseline, cap, width-1.35,
                     p['colour'], left=True, italic=p.get('italic', False))
                text(mesh, str(p['distances'][i]), width/2-.50, baseline, cap, .65, p['colour'])
            else:
                text(mesh, name, 0., baseline, cap, width-.48, p['colour'], italic=p.get('italic', False))
        n = p.get('arrow_lanes', 0)
        for i in range(n):
            arrow(mesh, width*((i+.5)/n-.5), bottom+p['bottom']+.13, .68, colour=p['colour'])
    if plan['routes']:
        sizes = [max(.70, sm.measure(r, 'l1')[0]*.22+.18) for r in plan['routes']]
        cursor = -(sum(sizes)+.04*(len(sizes)-1))/2
        z = bottom+plan['body_height']+.25
        for ref, w in zip(plan['routes'], sizes):
            local = mesh.offset.copy()
            centre = cursor+w/2
            mesh.offset = local+np.array([centre, 0., 0.])@mesh.axes
            c = 'green' if ref.startswith('E') else 'white' if ref.startswith('D') else 'blue' if ref.startswith('M') else 'red'
            mat = GREEN if c == 'green' else BLUE if c == 'blue' else sm.YELLOW if ref.startswith('D') else sm.RED
            fr.board(mesh, w, .36, z, mat, edge=sm.WHITE if c != 'white' else sm.BLACK, r=.014)
            mesh.text(ref, z, -.045, .23, w-.12, white=not ref.startswith('D'),
                      font_key='fr_motorway' if c != 'white' else 'l1', centre_ink=True)
            # The cartouche has its own short metal stays, joined to the stack.
            for xx in (-w*.28, w*.28):
                mesh.box(sm.METAL, (xx, .05, z-.23), (.018, .035, .10))
            mesh.offset = local
            cursor += w+.04
    mesh.offset = anchor


def mounts(sign):
    """All panels, including shorter stacks, connect physically to one beam."""
    bottom = float(sign['bottom']-sign['centre'][2])
    beam = bottom+max(p['height'] for p in sign['panels'])*.43
    rows = []
    for p in sign['panels']:
        top = bottom+p['fr_plan']['body_height']
        lo, hi = bottom+.08, max(top-.06, beam+.18)
        for xx in (-p['width']*.30, p['width']*.30):
            rows.append(dict(x=p['offset']+xx, bottom=lo, top=hi, beam=beam))
    return beam, rows


def support_plan(sign):
    beam, _ = mounts(sign)
    left, right = sign['poles']
    posts = list(zip((left, right), sign['bases']))
    if sign.get('fr_support') == 'cantilever':
        left = min(p['offset']-p['width']/2 for p in sign['panels'])-.35
        posts = posts[1:]
    return dict(beam=beam, left=left, right=right, posts=posts)


def build(exits, overheads, origin):
    mesh = sm.Mesh(origin)
    mesh.materials = MATERIALS
    for sign in exits:
        mesh.transform(sign['centre'], sign['travel'])
        if sign['kind'] == 'ramp':
            import frroadmesh as fr
            bottom = float(sign['bottom']-sign['centre'][2])
            fr.board(mesh, 1.55, .65, bottom+.325, sm.WHITE, r=.035)
            text(mesh, 'SORTIE '+sign['label'].get('ref', ''), 0., bottom+.325, .18, 1.35)
            for off, base in zip(sign.get('post_offsets', (-.32, .32)), sign['bases']):
                z = float(base-sign['centre'][2])-.22
                mesh.box(sm.METAL, (off*1.55, .075, (bottom+.6+z)/2), (.035, .055, (bottom+.6-z)/2))
            continue
        else:
            confirm = sign['kind'] == 'confirmation'
            plan = compose(sign['label'], not confirm, sign.get('distance', ''), roadside=True, confirmation=confirm,
                           side=sign.get('side', 1))
        if not plan:
            continue
        bottom = float(sign.get('bottom', sign['centre'][2]+2.2)-sign['centre'][2])
        draw(mesh, plan, bottom=bottom)
        # Posts stand at the planner's actual ground samples, not a fixed road Z.
        bases = sign.get('bases', [sign['centre'][2]]*2)
        offsets = sign.get('post_offsets', (-.32, .32))
        for i, x in enumerate(off*plan['width'] for off in offsets):
            base = float(bases[min(i, len(bases)-1)]-sign['centre'][2])-.22
            top = bottom+plan['body_height']-.07
            mesh.box(sm.METAL, (x, .10, (base+top)/2), (.045, .055, (top-base)/2))
            for p in plan['panels']:
                mesh.box(sm.METAL, (x, .045, bottom+(p['top']+p['bottom'])/2), (.09, .035, .035))
    for source in overheads:
        sign = finalize(source)
        if sign is None:
            continue
        mesh.transform(sign['centre'], sign['travel'])
        bottom = float(sign['bottom']-sign['centre'][2])
        beam, rails = mounts(sign)
        structure = support_plan(sign)
        left, right = structure['left'], structure['right']
        # Box-section gantry: continuous columns and a crossbeam behind the faces.
        for x, base in structure['posts']:
            start = float(base-sign['centre'][2])
            top = beam+.29
            mesh.box(sm.METAL, (x, .56, (start+top)/2), (.18, .24, (top-start)/2))
            mesh.box(sm.METAL, (x, .56, start+.055), (.30, .35, .055))
        mesh.box(sm.METAL, ((left+right)/2, .56, beam), ((right-left)/2, .23, .22))
        for rail in rails:
            lo, hi = rail['bottom'], rail['top']
            mesh.box(sm.METAL, (rail['x'], .10, (lo+hi)/2), (.035, .045, (hi-lo)/2))
            mesh.box(sm.METAL, (rail['x'], .32, beam), (.045, .24, .065))
        for p in sign['panels']:
            for register in p['fr_plan']['panels']:
                z = bottom+(register['top']+register['bottom'])/2
                for off in (-p['width']*.30, p['width']*.30):
                    mesh.box(sm.METAL, (p['offset']+off, .05, z), (.07, .045, .045))
            draw(mesh, p['fr_plan'], p['offset'], bottom)
    if not mesh.t:
        return None
    return (np.asarray(mesh.p), np.asarray(mesh.t), np.asarray(mesh.uv), MATERIALS, np.asarray(mesh.tm))
