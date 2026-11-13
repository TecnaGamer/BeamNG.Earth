"""Tapered park / forest entrance boards with opaque panel materials (wyo-backroads' PR #1118): the board's
shape, timber posts and lettering after the Forest Service family; the agency badge is a simple drawn shape (a tree
for forests and grasslands, peaks for parks), not the Forest Service shield or the NPS arrowhead (protected insignia).
"""
import numpy as np
import dnp


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
            result.append(dnp.sq(1-t)*a + 2*t*(1-t)*p + t*t*b)
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
             'bne_forest_letters_cream', 'bne_forest_letters_brown')
    for name in names:
        if name not in mesh.materials:
            mesh.materials.append(name)
    brown, cream, wood, letters, dark = [mesh.materials.index(n) for n in names]
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

    emblem = e.get('emblem', 'usfs')
    if emblem is None:
        return
    badge(mesh, emblem, w * .405, main_bottom + .25, .64, .72, cream)


def badge(mesh, emblem, y, z, width, height, material):
    """A plain cream badge on the brown half: a fir (usfs: forests, grasslands) or two peaks (nps: parks)."""
    def tri(a, b, c):
        mesh.triangle(np.c_[np.full(3, .07), [a, b, c]], material)
    if emblem == 'nps':
        tri((y - width / 2, z), (y + width * .1, z), (y - width * .2, z + height * .8))
        tri((y - width * .15, z), (y + width / 2, z), (y + width * .2, z + height))
        return
    trunk = height * .18
    mesh.quad([(.07, y - width * .07, z), (.07, y + width * .07, z), (.07, y + width * .07, z + trunk),
               (.07, y - width * .07, z + trunk)], material)
    for base, top, half in ((.18, .62, .5), (.38, .84, .38), (.6, 1., .26)):
        tri((y - width * half, z + height * base), (y + width * half, z + height * base), (y, z + height * top))
