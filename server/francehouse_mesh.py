"""Footprint-fitted French house models: real geometry, shared native tile/LOD path."""
import math

import numpy as np
import dmath
import dnp


def model_box(ring):
    import buildings as b
    c, u, hl, hw = b.oriented_box(ring)
    axis = 0 if abs(u[0]) > abs(u[1]) + 1e-6 else 1
    if u[axis] < 0:
        u = -u
    return c, u, hl, hw


_UV4 = ((0., 0.), (1., 0.), (1., 1.), (0., 1.))
LIT_SHARE = .3              # of the windows lit at night
LIT_GLASS_CELLS = 8         # the lit glass map's cells (tools/build_french_houses.py GLASS_LIT)


def face(m, pts, material, outward, uv=None):
    """Convex 3D face, explicitly outward wound (including vertical gables). (Plain floats: np.cross and asarray
    on 3-4 points made up ~60 % of a French house tile's build, PR #505 merge profile.)"""
    p = [(float(q[0]), float(q[1]), float(q[2])) for q in pts]
    uv = [(float(t[0]), float(t[1])) for t in (uv if uv is not None else _UV4[:len(p)])]
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = p[0], p[1], p[2]
    ux, uy, uz, vx, vy, vz = bx - ax, by - ay, bz - az, cx - ax, cy - ay, cz - az
    if ((uy * vz - uz * vy) * float(outward[0]) + (uz * vx - ux * vz) * float(outward[1])
            + (ux * vy - uy * vx) * float(outward[2])) < 0:
        p, uv = p[::-1], uv[::-1]
    if len(p) == 4:
        m.quad(*p, *uv, material)
    else:
        for j in range(1, len(p) - 1):
            m.tri(p[0], p[j], p[j + 1], uv[0], uv[j], uv[j + 1], material)


def panel(m, a, d, out, s0, s1, z0, z1, material, offset=.025, tile=2.4, uv=None):
    """uv: one point for all its corners (a lit window's cell of the glass map), else tiled along the wall."""
    ax, ay, dx, dy, ox, oy = float(a[0]), float(a[1]), float(d[0]), float(d[1]), float(out[0]), float(out[1])
    px, py = ax + dx * s0 + ox * offset, ay + dy * s0 + oy * offset
    qx, qy = ax + dx * s1 + ox * offset, ay + dy * s1 + oy * offset
    face(m, [(px, py, z0), (qx, qy, z0), (qx, qy, z1), (px, py, z1)],
         material, (ox, oy, 0.), [uv] * 4 if uv is not None else
         [(s0 / tile, 0.), (s1 / tile, 0.), (s1 / tile, (z1 - z0) / tile), (s0 / tile, (z1 - z0) / tile)])


def box(m, c, u, hx, hy, z0, z1, material):
    import buildings as b
    b._box_tiled(m, np.asarray(c), np.asarray(u), hx, hy, z0, z1, material, tile=2.4)


def beam(m, a, d, out, s0, z0, s1, z1, material, width=.13, coarse=False):
    ax, ay, dx, dy = float(a[0]), float(a[1]), float(d[0]), float(d[1])
    p = (ax + dx * s0, ay + dy * s0, float(z0))
    q = (ax + dx * s1, ay + dy * s1, float(z1))
    n = (float(out[0]), float(out[1]), 0.)
    axis = (q[0] - p[0], q[1] - p[1], q[2] - p[2])
    length = math.sqrt(axis[0] * axis[0] + axis[1] * axis[1] + axis[2] * axis[2])
    if length < .05:
        return
    e = (axis[0] / length, axis[1] / length, axis[2] / length)
    v = ((e[1] * n[2] - e[2] * n[1]) * width / 2, (e[2] * n[0] - e[0] * n[2]) * width / 2,
         (e[0] * n[1] - e[1] * n[0]) * width / 2)

    def at(r, sv, k):
        return tuple(r[i] + sv * v[i] + n[i] * k for i in range(3))
    back = [at(p, -1, .015), at(q, -1, .015), at(q, 1, .015), at(p, 1, .015)]
    front = [tuple(r[i] + n[i] * .075 for i in range(3)) for r in back]
    face(m, front, material, n)
    if coarse:
        return
    cx, cy, cz = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2, (p[2] + q[2]) / 2
    for i in range(4):
        j = (i + 1) % 4
        pts = [back[i], back[j], front[j], front[i]]
        mid = (sum(t[0] for t in pts) / 4 - cx - n[0] * .05, sum(t[1] for t in pts) / 4 - cy - n[1] * .05,
               sum(t[2] for t in pts) / 4 - cz - n[2] * .05)
        face(m, pts, material, mid)


def window(m, a, d, out, s, bottom, width, height, mats, style, seed, lite=False, attic=False):
    import buildings as b
    trim = mats[style["trim"]]
    lit = b._hash01(seed, round(s, 4), "lit")
    dark = mats[b.custom_look("bne_fr_house_glass_lit" if lit < LIT_SHARE else "bne_fr_house_glass", (.31, .38, .40))]
    # (a lit one: one cell of the lit glass's map, from the same hash: each lit window its own level and hour to go
    # dark, tools/build_french_houses.py GLASS_LIT; in UVs, one material)
    cell = ((int(lit / LIT_SHARE * LIT_GLASS_CELLS) + .5) / LIT_GLASS_CELLS, .5) if lit < LIT_SHARE else None
    # Whole frames behind inset panes: no alpha masks or stretched facade sheets.
    panel(m, a, d, out, s - width / 2 - .07, s + width / 2 + .07, bottom - .06, bottom + height + .07, trim, .035)
    panel(m, a, d, out, s - width / 2, s + width / 2, bottom, bottom + height, dark, .05, uv=cell)
    if lite:
        if not style["roller"]:
            for sign in (-1, 1):
                center = s + sign * (width * .75 + .10)
                panel(m, a, d, out, center - width * .235, center + width * .235,
                      bottom, bottom + height, mats[style["shutter"]], .065)
        return
    panel(m, a, d, out, s - .022, s + .022, bottom, bottom + height, trim, .06)
    for y in (height / 3, height * 2 / 3) if style["panes"] == 2 else ():
        panel(m, a, d, out, s - width / 2, s + width / 2, bottom + y - .018, bottom + y + .018, trim, .06)
    box(m, a + d * s + out * .09, d, width / 2 + .12, .15, bottom - .12, bottom - .055, trim)
    if attic:
        return
    shutter = mats[style["shutter"]]
    if style["roller"]:
        box(m, a + d * s + out * .08, d, width / 2 + .12, .09, bottom + height + .08, bottom + height + .23, trim)
        value = b._hash01(seed, round(s, 4), "blind")
        fraction = 1. if value < .20 else .50 if value < .35 else .20 if value < .50 else 0.
        if fraction:
            panel(m, a, d, out, s - width / 2, s + width / 2,
                  bottom + height * (1 - fraction), bottom + height, shutter, .075)
    else:
        for sign in (-1, 1):
            c = a + d * (s + sign * (width * .75 + .10)) + out * .065
            box(m, c, d, width * .235, .033, bottom - .02, bottom + height + .02, shutter)


def roof(m, spec, ze, mats):
    import buildings as b
    ring = b.drawn_ring(spec)
    if spec.roof == "flat":
        m.poly([(*p, ze) for p in ring], [(p[0] / 2.4, p[1] / 2.4) for p in ring], mats[spec.roof_mat])
        return
    style = spec.fr_house
    c, u, hl, hw = model_box(ring)
    v = np.array([-u[1], u[0]])
    eave, tangent = style["eave"], dmath.tan(math.radians(spec.pitch))
    L, W, z = hl + eave, hw + eave, ze - eave * tangent
    rise, top = W * tangent, ze + hw * tangent
    rm, wall = mats[spec.roof_mat], mats[style["wall"]]
    if style["family"] == "chalet":
        wall = mats[style["timber"]]
    def p(s, t, h):
        xy = c + u * s + v * t
        return (*xy, h)
    def facet(stz):
        stz = [entry for i, entry in enumerate(stz) if i == 0 or entry != stz[i - 1]]
        if stz[-1] == stz[0]:
            stz.pop()
        uv = [(s / 2.4, (W - abs(t)) / max(dmath.cos(math.radians(spec.pitch)), .1) / 2.4) for s, t, _ in stz]
        face(m, [p(*r) for r in stz], rm, (0, 0, 1), uv)
    if spec.roof == "hipped":
        r = 0. if L - W < .01 else L - W
        facet([(-L, -W, z), (L, -W, z), (r, 0, top), (-r, 0, top)])
        facet([(L, W, z), (-L, W, z), (-r, 0, top), (r, 0, top)])
        for sign in (-1, 1):
            facet([(sign * L, -W, z), (sign * L, W, z), (sign * r, 0, top)])
        return
    half = spec.roof == "half-hipped"
    cut = W * .42 if half else 0.
    inset = min(W * .45, L * .40) if half else 0.
    zcut = top - cut * tangent
    for sign in (-1, 1):
        stz = [(-L, sign * W, z), (L, sign * W, z)]
        if half:
            stz += [(L, sign * cut, zcut), (L - inset, 0, top), (-L + inset, 0, top), (-L, sign * cut, zcut)]
        else:
            stz += [(L, 0, top), (-L, 0, top)]
        facet(stz)
    for sign in (-1, 1):
        if half:
            facet([(sign * L, -cut, zcut), (sign * L, cut, zcut), (sign * (L - inset), 0, top)])
            # The wall's truncation follows the underside of that little hip.
            f = min(eave / max(inset, .001), 1.)
            end_z, end_w = zcut + (top - zcut) * f, cut * (1 - f)
            pts = [p(sign * hl, -hw, ze), p(sign * hl, hw, ze),
                   p(sign * hl, end_w, end_z), p(sign * hl, -end_w, end_z)]
        else:
            pts = [p(sign * hl, -hw, ze), p(sign * hl, hw, ze), p(sign * hl, 0, top)]
        face(m, pts, wall, (* (u * sign), 0.), [(0, 0), (2 * hw / 2.4, 0)] +
             ([(hw / 2.4, (top - ze) / 2.4)] if not half else [(2 * hw / 2.4, (end_z - ze) / 2.4), (0, (end_z - ze) / 2.4)]))


def balcony(m, a, d, out, length, base, mats, style):
    import buildings as b
    width, depth = min(7., length - .8), 1.0
    if width < 2.:
        return
    timber = style["family"] in {"chalet", "basque_frame"}
    rail = mats[style["timber"]] if timber else mats[b.custom_look("bne_fr_house_metal", (.12, .13, .13))]
    slab = mats[style["timber"]] if timber else mats[style["trim"]]
    c = a + d * length / 2
    box(m, c + out * depth / 2, d, width / 2, depth / 2, base - .18, base, slab)
    box(m, c + out * depth, d, width / 2 + .04, .045, base + .88, base + .96, rail)
    for s in np.linspace(-width / 2, width / 2, max(3, int(width / (.24 if timber else .32)) + 1)):
        box(m, c + d * s + out * depth, d, .035 if timber else .018, .035, base + .03, base + .90, rail)
    for sign in (-1, 1):
        box(m, c + d * sign * width / 2 + out * depth / 2, out, depth / 2, .035, base + .88, base + .96, rail)


def roof_details(m, spec, ze, mats):
    import buildings as b
    if spec.roof == "flat":
        return
    st = spec.fr_house
    c, u, L, W = model_box(b.drawn_ring(spec))
    v = np.array([-u[1], u[0]])
    tangent, peak = dmath.tan(math.radians(spec.pitch)), ze + W * dmath.tan(math.radians(spec.pitch))
    if st["chimney"]:
        brick = mats[b.custom_look("bne_fr_house_brick", (.65, .42, .33))]
        for sign in (-1, 1) if st["region"] == "brittany" else (1,):
            x = sign * max(.2, L - .65)
            y = .35 if spec.roof != "hipped" else .2
            z = ze + (W - abs(y)) * tangent
            if spec.roof == "hipped":
                z = ze + min(W - abs(y), L - abs(x)) * tangent
            box(m, c + u * x + v * y, u, .28, .25, z - .15, z + .95, brick)
            box(m, c + u * x + v * y, u, .34, .31, z + .94, z + 1.04, mats[st["trim"]])
    if st["dormer"] and L >= 4 and W >= 3:
        y = -W * .57
        z = ze + (W - abs(y)) * tangent - .05
        if z + 1.5 <= peak:
            center = c + u * (-L * .25) + v * (y + .35)
            box(m, center, u, .72, .47, z, z + 1.08, mats[st["wall"]])
            front = center - v * .47
            window(m, front - u * .72, u, -v, .72, z + .20, .72, .72, mats, st, spec.seed, attic=True)
            for sign in (-1, 1):
                pts = [(* (center + u * sign * .80 - v * .55), z + 1.04),
                       (* (center + u * sign * .80 + v * .55), z + 1.04),
                       (* (center + v * .55), z + 1.48), (* (center - v * .55), z + 1.48)]
                face(m, pts, mats[spec.roof_mat], (0, 0, 1))
    # Gutters/downpipes are small, opaque geometry; omitted from the coarse LOD.
    gutter = mats[b.custom_look("bne_fr_house_metal", (.48, .49, .47))]
    for sign in (-1, 1):
        box(m, c + v * sign * (W + st["eave"]), u, L + st["eave"], .045, ze - .13, ze - .045, gutter)


def draw(m, spec, gmin, gmax, mats, lite=False, collision=False):
    import buildings as b
    st = spec.fr_house
    ring = b.drawn_ring(spec)
    base = b.base_z(spec, gmin, gmax)
    bottom = gmin - (b.FOUNDATION_M if collision else b.SKIRT_LITE_M if lite else b.SKIRT_M)
    top = base + spec.height
    front = spec.door[0] if spec.door else max(range(len(ring)), key=lambda i: dnp.norm(ring[(i + 1) % len(ring)] - ring[i]))
    for i, a in enumerate(ring):
        bb = ring[(i + 1) % len(ring)]
        length = float(dnp.norm(bb - a))
        d = (bb - a) / length
        out = np.array([d[1], -d[0]])
        panel(m, a, d, out, 0, length, bottom, top, mats[st["wall"]], 0.)
        if collision:
            continue
        panel(m, a, d, out, 0, length, max(bottom, base - .4), base + .43, mats[st["plinth"]], .008)
        if st["family"] == "chalet":
            panel(m, a, d, out, 0, length, base + spec.floor_h, top, mats[st["timber"]], .012)
        n = max(1, min(7, int(round(round(length, 2) / 3.0))))
        door_s = spec.door[1] if spec.door and i == front else length * (.22 if length < 9 else .5)
        for floor in range(spec.floors):
            for j in range(n):
                s = length * (j + .5) / n
                if floor == 0 and i == front and abs(s - door_s) < 1.15:
                    continue
                width = min(1.15, length / n * .46)
                height = min(1.45 if not st["roller"] else 1.25, spec.floor_h - 1.0)
                low = base + floor * spec.floor_h + .85
                window(m, a, d, out, s, low, width, height, mats, st, (spec.seed, i, floor), lite)
        if i == front:
            trim, door = mats[st["trim"]], mats[st["shutter"]]
            panel(m, a, d, out, door_s - .60, door_s + .60, base + .08, base + 2.27, trim, .035)
            panel(m, a, d, out, door_s - .51, door_s + .51, base + .10, base + 2.20, door, .055)
            if not lite:
                for z in (.50, 1.35):
                    panel(m, a, d, out, door_s - .39, door_s + .39, base + z, base + z + .50, trim, .06)
                if st["balcony"]:
                    balcony(m, a, d, out, length, base + spec.floor_h + .08, mats, st)
        if st["family"] in {"timber", "basque_frame"}:
            timber = mats[st["timber"]]
            start = 0 if st["family"] == "timber" else 1
            for floor in range(start, spec.floors + 1):
                z = base + min(spec.height, floor * spec.floor_h) + .03
                beam(m, a, d, out, 0, z, length, z, timber, .15, coarse=lite)
            for j in range(n + 1):
                s = length * j / n
                beam(m, a, d, out, s, base + start * spec.floor_h + .02, s, top, timber, coarse=lite)
            for floor in range(start, spec.floors):
                lo = base + floor * spec.floor_h + .12
                for sign in (-1, 1):
                    s = .10 if sign == 1 else length - .10
                    beam(m, a, d, out, s, lo + .1, s + sign * .65, lo + 1.0, timber, .10, coarse=lite)
    roof(m, spec, top, mats)
    if collision or lite:
        return
    if spec.roof in {"gabled", "half-hipped"}:
        c, u, L, W = model_box(ring)
        v = np.array([-u[1], u[0]])
        rise = W * dmath.tan(math.radians(spec.pitch))
        allowance = rise * (.55 if spec.roof == "half-hipped" else 1.)
        if allowance > 1.4:
            for sign in (-1, 1):
                a = c + u * sign * L - v * W
                shifts = (-.95, .95) if st["family"] == "timber" and W >= 3 else (0.,)
                for shift in shifts:
                    window(m, a, v, u * sign, W + shift, top + .35, .65, .78, mats, st, spec.seed, attic=True)
                if st["family"] in {"timber", "basque_frame"}:
                    end_top = top + allowance
                    beam(m, a, v, u * sign, W, top, W, end_top - .12, mats[st["timber"]])
                    if spec.roof == "gabled":
                        for edge in (0., 2 * W):
                            beam(m, a, v, u * sign, edge, top + .05, W, end_top - .10, mats[st["timber"]])
    roof_details(m, spec, top, mats)


def prepared_parts(items, lite, with_lod, max_verts):
    """Keep the existing four native mesh slots; simplify houses before any drop.

    Other buildings' meshes are unchanged. The regional silhouette/materials
    remain on the coarse model; only small details are reduced on dense tiles.
    """
    import buildings as b
    import roads
    prepared = []
    for item in items:
        high = b.build([item], lite)
        low = b.build([item], True) if not lite and (with_lod or item[0].fr_house) else None
        prepared.append([high, low])
    if lite:
        return prepared
    def slots():
        used, count = 0, 0
        for high, _ in prepared:
            n = len(high[0]) if high is not None else 0
            if n:
                if not count or used + n > max_verts:
                    count += 1
                    used = 0
                used += n
        return count
    candidates = [(len(high[0]) - len(low[0]), i) for i, ((spec, _, _), (high, low)) in
                  enumerate(zip(items, prepared)) if spec.fr_house and high is not None and low is not None]
    for _, i in sorted(candidates, reverse=True):
        if slots() <= roads.BLD_PARTS_MAX:
            break
        prepared[i][0] = prepared[i][1]
    return prepared
