"""Reusable tapered Forest Service entrance boards, with opaque panel materials."""
import numpy as np


def _ccw(points):
    p = np.asarray(points, float)
    area = np.sum(p[:, 0] * np.roll(p[:, 1], -1) - p[:, 1] * np.roll(p[:, 0], -1))
    return p if area > 0 else p[::-1]


def outline(width, bottom, height):
    corners = np.array([[-.43, 0], [.43, 0], [.5, 1], [-.5, 1]])
    result = []
    for i, p in enumerate(corners):
        a = p + (corners[i-1] - p) * .10
        b = p + (corners[(i+1) % 4] - p) * .10
        for t in np.linspace(0, 1, 9):
            result.append((1-t)**2*a + 2*t*(1-t)*p + t*t*b)
    return _ccw(np.asarray(result) * [width, height] + [0, bottom])


def _half(points, z, upper):
    out = []
    for a, b in zip(points, np.roll(points, -1, axis=0)):
        ina, inb = (a[1] >= z, b[1] >= z) if upper else (a[1] <= z, b[1] <= z)
        if ina:
            out.append(a)
        if ina != inb:
            out.append(a + (b-a) * ((z-a[1]) / (b[1]-a[1])))
    return np.asarray(out)


def _face(mesh, polygon, material, depth=.055, back=False):
    p = _ccw(polygon)
    centre = p.mean(0)
    for a, b in zip(p, np.roll(p, -1, axis=0)):
        tri = np.c_[np.full(3, depth), [centre, a, b]]
        mesh.triangle(tri[::-1] if back else tri, material)


def render(mesh, sign):
    e = sign['extra']; w, h, bottom = sign['width'], sign['height'], sign['bottom']
    names = ('bne_recreation_brown_opaque', 'bne_forest_cream', 'bne_forest_timber',
             'bne_forest_letters_cream', 'bne_forest_letters_brown', 'bne_forest_shield')
    for name in names:
        if name not in mesh.materials:
            mesh.materials.append(name)
    brown, cream, wood, letters, dark, badge = [mesh.materials.index(n) for n in names]
    main_bottom = bottom + .48
    main_h = h - .48
    for offset, base in zip(sign['posts'], sign['bases']):
        mesh.box((-.28, offset-.12, base-.2), (-.05, offset+.12, main_bottom+main_h*.56), wood)
    for z in (bottom+.12, main_bottom+.18):
        mesh.box((-.26, -w*.49, z-.10), (-.04, w*.49, z+.10), wood)
    panel = outline(w, main_bottom, main_h)
    split = main_bottom + main_h*.54
    _face(mesh, _half(panel, split, False), brown)
    _face(mesh, _half(panel, split, True), cream)
    _face(mesh, panel, wood, -.04, back=True)
    for a, b in zip(panel, np.roll(panel, -1, axis=0)):
        mesh.quad([(-.04, *a), (-.04, *b), (.055, *b), (.055, *a)], wood)

    def text(value, y, baseline, cap, width, material, italic=False):
        start, vertex = len(mesh.mi), len(mesh.p)
        mesh.guide_text(value, y, baseline, cap, width)
        mesh.mi[start:] = [material] * (len(mesh.mi)-start)
        if italic:
            for i in range(vertex, len(mesh.p)):
                mesh.p[i] = mesh.p[i] + mesh.right * .18 * (mesh.p[i][2] + mesh.origin[2] - mesh.centre[2] - baseline)

    text(e['name'], 0, main_bottom+main_h*.82, .36, w-.6, dark)
    text(e['category'], 0, main_bottom+main_h*.61, .38, w-.4, dark)
    text(e['forest'], -.35, main_bottom+main_h*.36, .32, w-1.35, letters)
    text(e['designation'], -.35, main_bottom+main_h*.14, .43, w-1.35, letters, italic=True)
    plaque = np.array([[-w*.49, bottom], [w*.49, bottom], [w*.49, bottom+.34], [-w*.49, bottom+.34]])
    _face(mesh, plaque, brown)
    for z in (bottom+.02, bottom+.30):
        mesh.rectangle(w*.97, z, .02, .06, cream)
    text(e['agency'], 0, bottom+.09, .18, w-.35, letters)

    emblem=e.get('emblem','usfs')
    if emblem is None:return
    if emblem=='nps':
        name='bne_nps_arrowhead'
        if name not in mesh.materials:mesh.materials.append(name)
        material=mesh.materials.index(name);x=w*.405;z=main_bottom+.25
        mesh.quad([(.07,x-.32,z),(.07,x+.32,z),(.07,x+.32,z+.83),(.07,x-.32,z+.83)],
                  material,[(0,0),(1,0),(1,1),(0,1)])
        return

    # Clip the unmodified public-domain emblem to its shield silhouette. The
    # underlying panel stays opaque; no alpha-blended board or square white patch.
    pixels = np.array([(80,0),(150,24),(250,34),(355,24),(434,3),(504,108),(478,177),
                       (470,245),(505,406),(490,470),(457,505),(370,528),(290,543),
                       (250,566),(216,543),(110,527),(45,503),(10,460),(0,406),
                       (35,245),(33,190),(7,106)], float)
    uv = pixels / [506.,568.]
    bw, bh = .64, .72
    yz = np.c_[(uv[:,0]-.5)*bw+w*.405, (1-uv[:,1])*bh+main_bottom+.25]
    centre, middle = yz.mean(0), uv.mean(0)
    for i in range(len(yz)):
        j = (i+1) % len(yz)
        points = np.c_[np.full(3,.07), [centre,yz[j],yz[i]]]
        mesh.triangle(points, badge)
        mesh.uv[-3:] = [(u,1-v) for u,v in (middle,uv[j],uv[i])]
