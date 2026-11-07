"""The faces of Germany's junction signs (server/designs.py's GROUPS): bne_de_signs.png, Zeichen 205, 206, 215, 301,
306, 307 and the ten 1002 plates of designs.PLATES, one to a cell of 256 px, drawn from their construction; and of its zone
signs (server/speedsigns.py's de_zone_mesh): bne_de_zones.png, Zeichen 274.1 and 274.2 for the zones of
designsigns.ZONE_KMH (zone_pictures below).

    python tools/build_de_signs.py [out.png [zones.png]]

The shapes and sizes are the VwV-StVO's (zu den §§ 39 bis 43 Rn. 13: the sizes; designs.SIZE) and the VzKat's (Teil 3
Zeichen 206: the octagon; Teil 7 Zeichen 1002: the ten plates). What the VwV and the VzKat's text do not give, the
widths of the borders, the symbols and the lettering, is measured off the public-domain renderings on Wikimedia
Commons ("Zeichen 205 - Vorfahrt gewähren! StVO 1970.svg", "Zeichen 206 - Halt! Vorfahrt gewähren! StVO 2017.svg",
"Zeichen 301 - Vorfahrt, StVO 1970.svg", "Zeichen 306 - Vorfahrtstraße, StVO 1970.svg", "Zeichen 307 - Ende der
Vorfahrtstraße, StVO 1981.svg", "Zusatzzeichen 1002-10 - Verlauf der Vorfahrtsstraße an Kreuzungen (von unten nach
links), StVO 1992.svg"; fetched 2026-11-06; "Zeichen 215 - Kreisverkehr, StVO 2000.svg", Andreas06, 2026-11-07), in mm at Größe 2 below: their paths are in round millimetres, and
the VzKat's own pictures (vzkat.de, a private reproduction of the VzKat 04/2025) agree. Those are measurements, not
the BASt's construction sheets.

The colours are DIN 6171's (VwV-StVO zu den §§ 39 bis 43 Rn. 20) as the VzKat's own pictures show them (sampled
from vzkat.de's). The lettering is DIN 1451 (Rn. 19): D-DIN Bold (Datto, SIL OFL 1.1, tools/fonts), each letter
fitted to the rendering's box of it.

Each picture is drawn 4x over and averaged down, on white: every face's edge is a white rim, which is what a cell's
edge and its mipmaps then bleed into. tools/build_median_props.py maps the meshes on it by pixel(), speedsigns the zone
signs on bne_de_zones.png by designsigns.zone_uv()."""
import math
import os
import sys

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import designs  # noqa: E402
import designsigns  # noqa: E402

OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth",
                   "bne_de_signs.png")
ZONES_OUT = os.path.join(os.path.dirname(OUT), "bne_de_zones.png")
FONT = os.path.join(HERE, "fonts", "D-DIN-Bold.otf")
CELL, ART, SS = 256, 244, 4     # px: a cell, a picture's longer side in it; drawn SS times over
COLOURS = 48                    # the anti-aliased edges' too; the five below exactly (_quantise)
# the VzKat's own pictures' colours (BASt / BMDV, as vzkat.de shows them, 2026-11-06): the median of a flat patch of
# each, edges avoided; Z 205, 206 and 301 give the same red, 205, 306 and the 1002 plates the same white
# (RAL's paint swatches, ral-farben.de, are darker and greener: 3020 #AB1519, 1023 #E6BE05; a sign's face is
# retroreflective sheeting, whose day colours (DIN 6171) the VzKat's renderings show)
WHITE = (0xEF, 0xEF, 0xEF)      # the white field: 205.gif's middle, 306.gif's ring, 1002-10.gif
RED = (0xDE, 0x00, 0x00)        # the red border: 205.gif's top band, 206.gif over the lettering, 301.gif's base
YELLOW = (0xFF, 0xB5, 0x00)     # 306.gif's and 307.gif's yellow
BLACK = (0x00, 0x00, 0x00)      # 301.gif's symbol, 1002-10.gif's road, 307.gif's bars
BLUE = (0x00, 0x3A, 0xBD)       # 215.gif's blue (Teil 3; its white 205.gif's, its red absent; the rendering's #154889
                                # is paint-like, as its 205's #c1121c against the VzKat's #DE0000)
GREY = (0x7B, 0x7B, 0x7B)       # 274-2-50.gif's struck-through ring, digits and ZONE (274-1-50.gif: the red, white and
                                # black above)

# the cells (column, row) of the 1024 px sheet; p<k>: the 1002 plate of designs.PLATES[k]
CELLS = {"z205": (0, 0), "z206": (1, 0), "z301": (2, 0), "z306": (3, 0), "z307": (0, 1), "z215": (3, 3)}
CELLS.update({f"p{k}": ((k + 5) % 4, (k + 5) // 4) for k in range(len(designs.PLATES))})
# the VzKat's number of each plate (Teil 7 Zeichen 1002: "an Kreuzungen 10: von unten nach links, 11: von oben nach
# links, 20: von unten nach rechts, 21: von oben nach rechts"; "an Einmündungen 12: von unten nach links, Einmündung von
# oben, 13: ... von rechts, 14: von oben nach links, Einmündung von unten, 22, 23, 24: the same to the right")
VZKAT_1002 = ("1002-13", "1002-23", "1002-12", "1002-14", "1002-22", "1002-24", "1002-10", "1002-20", "1002-11",
              "1002-21")

# Z 205 / 301 at Größe 2 (side 900): a 10 mm white rim, an 80 mm red border, the white inside sharp-cornered
TRI_RIM, TRI_RED = 0.010, 0.080
# 301's symbol, m from the triangle's middle (z up): the broad road with its point and notched tail, the thin one across
SYMBOL_301 = ((0.0, -0.0668), (-0.068, -0.1347), (-0.068, 0.0012), (-0.1384, 0.0012), (-0.1384, 0.0414),
              (-0.068, 0.0414), (-0.068, 0.1195), (0.0, 0.1874), (0.068, 0.1195), (0.068, 0.0414), (0.1384, 0.0414),
              (0.1384, 0.0012), (0.068, 0.0012), (0.068, -0.1347))
# Z 206 at Größe 2 (900 across its flats): a 45 mm white rim, the red octagon sharp-cornered, STOP in four boxes (x0,
# x1, z0, z1 m from the middle: S and O 323 mm high with their overshoot, T and P 317; strokes 45 mm in the rendering)
OCT_RIM = 0.045
STOP = (("S", -0.3418, -0.1899, -0.1616, 0.1616), ("T", -0.1730, -0.0210, -0.1586, 0.1587),
        ("O", 0.0014, 0.1533, -0.1616, 0.1616), ("P", 0.1982, 0.3446, -0.1586, 0.1587))
# Z 306 / 307 at Größe 2 (side 600): a 14 mm white rim, a 15 mm black edge, the yellow square 320 mm a side with a 10 mm
# black line on its edge; 307's three black bars 35 mm wide, 50 mm apart, across to the black edge (552 mm long)
DIA_RIM, DIA_EDGE, DIA_CORE, DIA_LINE = 0.014, 0.015, 0.160, 0.010
BAR_W, BAR_STEP, BAR_HALF = 0.035, 0.050, 0.276
# Z 215 at Größe 2 (600 across): a 14 mm white rim round the blue disc (286 mm its radius), three white arrows turning
# anticlockwise, each the one below turned by 120 degrees (the rendering's three agree within a millimetre): its path
# from its head's outer point, m from the middle, z up, ("L", point) or ("C", two control points and the end)
DISC_215 = 0.286
ARROW_215 = ((0.1435, -0.1255), ("L", (0.0459, -0.1007)), ("L", (0.0099, -0.1221)), ("L", (0.0799, -0.1414)),
             ("C", (0.0106, -0.1838), (-0.0836, -0.1577), (-0.1243, -0.1053)), ("L", (-0.1517, -0.1280)),
             ("C", (-0.0739, -0.2162), (0.0274, -0.2110), (0.0978, -0.1726)), ("L", (0.0801, -0.2435)),
             ("L", (0.1179, -0.2218)))
# the 1002 plate at Größe 2 (600 x 600): a 10 mm white rim, a 15 mm black frame; the roads from 225 mm off the middle,
# the priority's 100 mm wide, bent on a 100 mm radius (its middle line), the others 50 mm wide from 75 mm off it
PLATE_RIM, PLATE_FRAME = 0.010, 0.015
ROAD_END, BOLD_W, BEND_R, THIN_W, THIN_FROM = 0.225, 0.100, 0.100, 0.050, 0.075
SLOTS = {"D": (0.0, -1.0), "L": (-1.0, 0.0), "A": (0.0, 1.0), "R": (1.0, 0.0)}     # the approach at the bottom
# Z 274.1 / 274.2 at Größe 2 (600 x 600, the plate's edge above: a 10 mm white rim, a 15 mm black frame, a 40 mm
# corner), measured off the public-domain renderings on Wikimedia Commons ("Zeichen 274.1 - Beginn einer Tempo 30-Zone,
# StVO 2013.svg", "Zeichen 274.2 - Ende einer Tempo 30-Zone (einseitig), StVO 2013.svg" and their 274.1-20 /
# 274.2-20, Mediatus; fetched 2026-11-07; 1 px a mm) in m from the face's middle, z up: the round 274 (its middle's
# height, the red ring's outer and inner radius: a 380 mm ring 50 mm wide), the digits' boxes (x0, x1; DIGIT_Z: the
# round ones' bottom, every top; FLAT_Z0 the 1's and 2's flat bottom), ZONE's letters (x0, x1, z0, z1), 274.2's five
# black bars ("/" corner to corner, 10.5 mm wide on a 21.1 mm pitch, out to the frame)
ZONE_RING = (0.0569, 0.190, 0.140)
DIGITS = {30: (("3", -0.1075, -0.0117), ("0", 0.0118, 0.1050)), 20: (("2", -0.0975, -0.0042), ("0", 0.0132, 0.1068))}
DIGIT_Z, FLAT_Z0 = (-0.0262, 0.1400), -0.0247
ZONE_WORD = (("Z", -0.1395, -0.0855, -0.2467, -0.1628), ("O", -0.0735, -0.0135, -0.2475, -0.1620),
             ("N", 0.0030, 0.0660, -0.2467, -0.1628), ("E", 0.0855, 0.1395, -0.2467, -0.1628))
BARS, BAR_STRIPE, BAR_PITCH = 5, 0.0105, 0.0211
# D-DIN Bold is heavier than the renderings' lettering (strokes 0.18 of a digit's height, theirs 0.14: 23 mm of 166):
# its 1200 pt ink eroded by ZONE_THIN px a side, a horizontal then measures theirs (the 0's 23 mm, ZONE's 12 mm), a
# vertical 10-15 % over it
ZONE_THIN = 11


def _picture(name):
    """(cell middle px, px per m, the middle of its edge (m)) of a picture at Größe 2."""
    shape = "plate" if name.startswith("p") else name
    x0, z0, x1, z1 = designs.extent(shape, designs.SIZE[shape][1])
    col, row = CELLS[name]
    return (CELL * col + CELL / 2, CELL * row + CELL / 2), ART / max(x1 - x0, z1 - z0), ((x0 + x1) / 2, (z0 + z1) / 2)


PICTURES = {name: _picture(name) for name in CELLS}


def pixel(name, x, z):
    """The sheet's px (from its top left) of (x, z) m of picture name at Größe 2, round the middle of designs.edge."""
    (cx, cy), k, (mx, mz) = PICTURES[name]
    return cx + (x - mx) * k, cy - (z - mz) * k


def uv(name, x, z):
    px, py = pixel(name, x, z)
    return px / 1024, 1 - py / 1024


class Canvas:
    """One cell drawn SS times over: polygons in m of a picture at Größe 2 (pictures: PICTURES or ZONE_PICTURES)."""

    def __init__(self, name, pictures=None):
        _, k, (self.mx, self.mz) = (PICTURES if pictures is None else pictures)[name]
        self.k = k * SS
        self.im = Image.new("RGB", (CELL * SS, CELL * SS), WHITE)
        self.d = ImageDraw.Draw(self.im)

    def px(self, x, z):
        return CELL * SS / 2 + (x - self.mx) * self.k, CELL * SS / 2 - (z - self.mz) * self.k

    def poly(self, pts, fill):
        self.d.polygon([self.px(x, z) for x, z in pts], fill=fill)

    def shape(self, n, first, r_in, radius, fill, segs=32):
        self.poly(designs.rounded(n, first, r_in, radius, segs if radius > 0 else 0), fill)

    def disc(self, x, z, r, fill):
        self.poly([(x + r * math.cos(a), z + r * math.sin(a)) for a in (2 * math.pi * j / 256 for j in range(256))],
                  fill)

    def clipped(self, polys, clip, fill):
        """polys drawn in fill only where they are inside the polygon clip."""
        size = self.im.size
        ink, inside = Image.new("L", size, 0), Image.new("L", size, 0)
        for pts in polys:
            ImageDraw.Draw(ink).polygon([self.px(x, z) for x, z in pts], fill=255)
        ImageDraw.Draw(inside).polygon([self.px(x, z) for x, z in clip], fill=255)
        self.im.paste(fill, (0, 0), ImageChops.multiply(ink, inside))

    def done(self):
        return self.im.reduce(SS)       # (each px the mean of its SS x SS: no ringing past the four colours)


def triangle(name):
    n, first, r_in, radius = designs.SHAPES[name]
    s = designs.SIZE[name][1]
    c = Canvas(name)
    c.shape(n, first, r_in * s, radius * s, WHITE)
    c.shape(n, first, r_in * s - TRI_RIM, radius * s - TRI_RIM, RED)
    c.shape(n, first, r_in * s - TRI_RIM - TRI_RED, 0.0, WHITE)
    if name == "z301":
        c.poly(SYMBOL_301, BLACK)
    return c.done()


def _letter(c, ch, x0, x1, z0, z1, font, fill=WHITE, thin=0):
    """ch of font, its ink stretched to the box (m), in fill; thin: px its 1200 pt ink is eroded by on every side."""
    im = Image.new("L", (1600, 1600), 0)
    ImageDraw.Draw(im).text((300, 100), ch, font=font, fill=255)
    if thin:                                # (its ink eroded by a square of 2 thin + 1 px: the min along each axis)
        a = np.asarray(im)
        for axis in (0, 1):
            p = np.pad(a, [(thin, thin) if ax == axis else (0, 0) for ax in (0, 1)])
            a = np.minimum.reduce([np.take(p, range(j, j + a.shape[axis]), axis=axis) for j in range(2 * thin + 1)])
        im = Image.fromarray(a)
    ink = im.crop(im.getbbox())
    (a, b), (e, f) = c.px(x0, z1), c.px(x1, z0)
    ink = ink.resize((round(e - a), round(f - b)), Image.LANCZOS)
    c.im.paste(fill, (round(a), round(b)), ink)


def octagon(name):
    n, first, r_in, radius = designs.SHAPES[name]
    s = designs.SIZE[name][1]
    c = Canvas(name)
    c.shape(n, first, r_in * s, radius * s, WHITE)
    c.shape(n, first, r_in * s - OCT_RIM, 0.0, RED)
    font = ImageFont.truetype(FONT, 1200)
    for ch, x0, x1, z0, z1 in STOP:
        _letter(c, ch, x0, x1, z0, z1, font)
    return c.done()


def diamond(name):
    n, first, r_in, radius = designs.SHAPES[name]
    s = designs.SIZE[name][1]
    h, r = r_in * s, radius * s
    c = Canvas(name)
    c.shape(n, first, h, r, WHITE)
    c.shape(n, first, h - DIA_RIM, r - DIA_RIM, BLACK)
    c.shape(n, first, h - DIA_RIM - DIA_EDGE, r - DIA_RIM - DIA_EDGE, WHITE)
    c.shape(n, first, DIA_CORE + DIA_LINE / 2, 0.0, BLACK)
    c.shape(n, first, DIA_CORE - DIA_LINE / 2, 0.0, YELLOW)
    if name == "z307":
        u, v = (2 ** -0.5, 2 ** -0.5), (2 ** -0.5, -(2 ** -0.5))       # along the bars, across them
        for k in (-1, 0, 1):
            o = k * BAR_STEP
            c.poly([(o * v[0] + a * BAR_HALF * u[0] + b * BAR_W / 2 * v[0],
                     o * v[1] + a * BAR_HALF * u[1] + b * BAR_W / 2 * v[1])
                    for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))], BLACK)
    return c.done()


def _path(start, segs, steps=24):
    """The points of a path of straight lines and cubic curves (ARROW_215's form)."""
    pts = [start]
    for seg in segs:
        if seg[0] == "L":
            pts.append(seg[1])
            continue
        (x0, z0), (x1, z1), (x2, z2), (x3, z3) = pts[-1], *seg[1:]
        for j in range(1, steps + 1):
            t = j / steps
            a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
            pts.append((a * x0 + b * x1 + c * x2 + d * x3, a * z0 + b * z1 + c * z2 + d * z3))
    return pts


def roundabout(name):
    """Zeichen 215: the white rim, the blue disc and the three arrows (ARROW_215 turned by 0, 120 and 240 degrees)."""
    c = Canvas(name)
    r = designs.SIZE[name][1] / 2
    c.disc(0.0, 0.0, r, WHITE)
    c.disc(0.0, 0.0, DISC_215, BLUE)
    arrow = _path(ARROW_215[0], ARROW_215[1:])
    for k in range(3):
        a = 2 * math.pi * k / 3
        ca, sa = math.cos(a), math.sin(a)
        c.poly([(x * ca - z * sa, x * sa + z * ca) for x, z in arrow], WHITE)
    return c.done()


def _arm(c, d, near, far, width):
    """A road on the plate: from near to far m off its middle towards d, width m wide."""
    (dx, dz), w = d, width / 2
    c.poly([(dx * near - dz * w, dz * near + dx * w), (dx * far - dz * w, dz * far + dx * w),
            (dx * far + dz * w, dz * far - dx * w), (dx * near + dz * w, dz * near - dx * w)], BLACK)


def plate(k):
    """The 1002 plate of designs.PLATES[k], as the driver coming in at its bottom sees the junction: the priority pair
    bold and bent, every other road thin, nothing where no road is (the arms' slots as native dejunction.rs plate_of
    finds them: the driver's right is the plate's right)."""
    name = f"p{k}"
    others, pair = designs.PLATES[k].split(":")
    n, first, r_in, radius = designs.SHAPES["plate"]
    s = designs.SIZE["plate"][1]
    c = Canvas(name)
    c.shape(n, first, r_in * s, radius * s, WHITE, 16)
    c.shape(n, first, r_in * s - PLATE_RIM, radius * s - PLATE_RIM, BLACK, 16)
    c.shape(n, first, r_in * s - PLATE_RIM - PLATE_FRAME, radius * s - PLATE_RIM - PLATE_FRAME, WHITE, 16)
    p, q = (SLOTS[ch] for ch in pair)
    for d in (p, q):
        _arm(c, d, BEND_R, ROAD_END, BOLD_W)
    cx, cz = (p[0] + q[0]) * BEND_R, (p[1] + q[1]) * BEND_R      # the bend's middle: between the two arms
    a0, a1 = math.atan2(-q[1], -q[0]), math.atan2(-p[1], -p[0])  # (the arms' ends seen from it, 90 degrees apart)
    a1 = a0 + math.copysign(math.pi / 2, math.remainder(a1 - a0, 2 * math.pi))
    arc = [a0 + (a1 - a0) * j / 32 for j in range(33)]
    c.poly([(cx + (BEND_R + BOLD_W / 2) * math.cos(a), cz + (BEND_R + BOLD_W / 2) * math.sin(a)) for a in arc]
           + [(cx + (BEND_R - BOLD_W / 2) * math.cos(a), cz + (BEND_R - BOLD_W / 2) * math.sin(a)) for a in arc[::-1]],
           BLACK)
    for ch in (set(others) | {"D"}) - set(pair):
        _arm(c, SLOTS[ch], THIN_FROM, ROAD_END, THIN_W)
    return c.done()


def pictures():
    """{name: its cell's picture (256 px RGB)}."""
    out = {"z205": triangle("z205"), "z301": triangle("z301"), "z206": octagon("z206"), "z306": diamond("z306"),
           "z307": diamond("z307"), "z215": roundabout("z215")}
    out.update({f"p{k}": plate(k) for k in range(len(designs.PLATES))})
    return out


def _ink(ch, font):
    """(width, height) px of ch's ink in font."""
    im = Image.new("L", (1600, 1600), 0)
    ImageDraw.Draw(im).text((300, 100), ch, font=font, fill=255)
    x0, y0, x1, y1 = im.getbbox()
    return x1 - x0, y1 - y0


def digit_boxes(kmh):
    """[(digit, x0, x1)] m of a zone's limit on its face: the renderings' boxes for 30 and 20; 10 (no VzKat picture:
    the 274.1-20's, its 1 as wide for its ink's height as D-DIN's, the same gap before the 0, the pair where the 20's
    is)."""
    if kmh in DIGITS:
        return list(DIGITS[kmh])
    (_, a0, a1), (zero, b0, b1) = DIGITS[20]
    font = ImageFont.truetype(FONT, 1200)
    (w1, h1), (w2, h2) = _ink("1", font), _ink("2", font)
    width = (a1 - a0) * (w1 / h1) / (w2 / h2)
    shift = (a0 + b1) / 2 - (a1 - width + b1) / 2          # (the 1 ends where the 2 did, then the pair centred)
    return [("1", a1 - width + shift, a1 + shift), (zero, b0 + shift, b1 + shift)]


def zone(sign, kmh):
    """Zeichen 274.1 (sign 1: the round 274 of kmh over ZONE in a white square with a black frame) or 274.2 (2: the
    same in grey, struck through by five black bars) at Größe 2, as the VzKat's pictures (Teil 3: 274-1-50.gif,
    274-2-50.gif) and the renderings above show them."""
    c = Canvas(zone_name(sign, kmh), ZONE_PICTURES)
    n, first, r_in, radius = designs.SHAPES["plate"]
    s = designsigns.ZONE_SIZE[1]
    c.shape(n, first, r_in * s, radius * s, WHITE, 16)
    c.shape(n, first, r_in * s - PLATE_RIM, radius * s - PLATE_RIM, BLACK, 16)
    c.shape(n, first, r_in * s - PLATE_RIM - PLATE_FRAME, radius * s - PLATE_RIM - PLATE_FRAME, WHITE, 16)
    ink = BLACK if sign == 1 else GREY
    zc, outer, inner = ZONE_RING
    c.disc(0.0, zc, outer, RED if sign == 1 else GREY)
    c.disc(0.0, zc, inner, WHITE)
    font = ImageFont.truetype(FONT, 1200)
    for ch, x0, x1 in digit_boxes(kmh):
        _letter(c, ch, x0, x1, DIGIT_Z[0] if ch in "03" else FLAT_Z0, DIGIT_Z[1], font, ink, ZONE_THIN)
    for ch, x0, x1, z0, z1 in ZONE_WORD:
        _letter(c, ch, x0, x1, z0, z1, font, ink, ZONE_THIN)
    if sign == 2:
        u, v = (2 ** -0.5, 2 ** -0.5), (2 ** -0.5, -(2 ** -0.5))        # along the bars, across them
        bars = []
        for k in range(-(BARS // 2), BARS // 2 + 1):
            o = k * BAR_PITCH
            bars.append([(o * v[0] + a * s * u[0] + b * BAR_STRIPE / 2 * v[0],
                          o * v[1] + a * s * u[1] + b * BAR_STRIPE / 2 * v[1])
                         for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))])
        c.clipped(bars, designs.rounded(n, first, r_in * s - PLATE_RIM, radius * s - PLATE_RIM, 16), BLACK)
    return c.done()


def zone_name(sign, kmh):
    return f"z274_{sign}_{kmh}"


def _zone_picture(sign, kmh):
    px = designsigns.zone_pixel(sign, kmh, 0.0, 0.0)
    return px, designsigns.ZONE_ART / designsigns.ZONE_SIZE[1], (0.0, 0.0)


ZONE_PICTURES = {zone_name(s, v): _zone_picture(s, v) for s in (1, 2) for v in designsigns.ZONE_KMH}


def zone_pictures():
    """{(sign, km/h): its cell's picture (256 px RGB)}."""
    return {(s, v): zone(s, v) for s in (1, 2) for v in designsigns.ZONE_KMH}


def _quantise(sheet, base=(WHITE, RED, YELLOW, BLACK, BLUE)):
    """sheet in COLOURS colours, the ones of its signs (base) exactly: they seed the palette, median cut adds the
    anti-aliased edges' (a palette of its own would move each flat colour to its cluster's mean), and each px takes its
    nearest entry, the first of equals (Pillow's own mapping looks up a coarser cache: a flat white went to a
    near-white)."""
    edges = sheet.quantize(COLOURS - len(base), dither=Image.Dither.NONE).getpalette()[:3 * (COLOURS - len(base))]
    pal = np.array(list(base) + [edges[i:i + 3] for i in range(0, len(edges), 3)], np.int64)
    colours, inv = np.unique(np.asarray(sheet, np.int64).reshape(-1, 3), axis=0, return_inverse=True)
    near = ((colours[:, None, :] - pal[None]) ** 2).sum(2).argmin(1).astype(np.uint8)
    out = Image.frombytes("P", sheet.size, near[inv.reshape(-1)].tobytes())
    out.putpalette(pal.astype(np.uint8).ravel().tolist())
    return out


def main(out=OUT, zones_out=ZONES_OUT):
    sheet = Image.new("RGB", (CELL * 4, CELL * 4), WHITE)
    for name, im in pictures().items():
        col, row = CELLS[name]
        sheet.paste(im, (CELL * col, CELL * row))
    _quantise(sheet).save(out, optimize=True)
    print(out, os.path.getsize(out), "bytes")
    sheet = Image.new("RGB", designsigns.ZONE_SHEET, WHITE)
    for (sign, kmh), im in zone_pictures().items():
        x, y = designsigns.zone_pixel(sign, kmh, 0.0, 0.0)
        sheet.paste(im, (round(x - CELL / 2), round(y - CELL / 2)))
    _quantise(sheet, (WHITE, RED, BLACK, GREY)).save(zones_out, optimize=True)
    print(zones_out, os.path.getsize(zones_out), "bytes")


if __name__ == "__main__":
    main(*sys.argv[1:3])
