from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'mod' / 'beamng_earth' / 'levels' / 'beamng_earth' / 'art' / 'shapes' / 'beamngEarth'
OUT.mkdir(parents=True, exist_ok=True)


FONT_CANDIDATES = [
    'C:/Windows/Fonts/arialbd.ttf',
    'C:/Windows/Fonts/calibrib.ttf',
    'C:/Windows/Fonts/segoeuib.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf',
]
FONT_PATH: str | None = None    # --font: the digits' typeface; the first installed candidate otherwise


@lru_cache(maxsize=None)
def find_font() -> str:
    """The digits' bold typeface. The PNGs depend on it, so a missing font is an error: Pillow's built-in
    fallback ignores the requested size and drew signs with ~4 px digits without a word."""
    for path in ([FONT_PATH] if FONT_PATH else FONT_CANDIDATES):
        if os.path.exists(path):
            return path
    raise SystemExit('No bold sans-serif font found. Pass --font /path/to/Bold.ttf '
                     '(the committed signs were drawn with Arial Bold, arialbd.ttf).')


def choose_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(find_font(), size=size)


def make_sign(label: str, bg: str, accent: str, accent2: str, text_color: str = 'black') -> Image.Image:
    size = 1024
    img = Image.new('RGBA', (size, size), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)

    cx = cy = size // 2
    panel_radius = 422  # outer white panel

    def disc(radius: int, fill: str) -> None:
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=fill)

    if label == 'NO LIMIT':
        # StVO sign 282 (end of route speed limits): white panel, thin black ring,
        # five parallel black stripes at 45 deg from upper right to lower left.
        outer_white_rim = 20
        black_rim = 25
        black_radius = panel_radius - outer_white_rim
        face_radius = black_radius - black_rim
        disc(panel_radius, bg)
        disc(black_radius, 'black')
        disc(face_radius, bg)

        half_len = 384
        inv_sqrt2 = 1 / math.sqrt(2)
        px, py = inv_sqrt2, inv_sqrt2
        start_x = cx + half_len * inv_sqrt2
        start_y = cy - half_len * inv_sqrt2
        end_x = cx - half_len * inv_sqrt2
        end_y = cy + half_len * inv_sqrt2
        stripe_spacing = 22
        stripe_width = 12
        for i in range(5):
            off = (i - 2) * stripe_spacing
            ox, oy = px * off, py * off
            draw.line(
                (start_x + ox, start_y + oy, end_x + ox, end_y + oy),
                fill='black',
                width=stripe_width,
            )
        return img

    # Vienna-convention speed limit sign (StVO 274 style):
    # white panel, thin white margin, thick RED ring, white face, black digits.
    white_margin = 14
    red_outer = panel_radius - white_margin
    red_width = 78
    face_radius = red_outer - red_width
    disc(panel_radius, bg)
    disc(red_outer, accent)
    disc(face_radius, bg)

    # Fit the digits to the face. The font grows until the text's bounding-box corners reach 90% of the
    # face radius. 1-2 digit values keep their natural proportions; 3-digit values may be condensed
    # horizontally (down to 78%) so they can stay tall instead of shrinking, as on real signs.
    max_corner = face_radius * 1.00
    min_squeeze = 1.0 if len(label) <= 2 else 0.78
    font_size = 500
    while font_size > 40:
        font = choose_font(font_size)
        l, t, r, b = draw.textbbox((0, 0), label, font=font, anchor='lt')
        w, h = r - l, b - t
        if h / 2 < max_corner:
            squeeze = min(1.0, 2 * math.sqrt(max_corner ** 2 - (h / 2) ** 2) / w)
            if squeeze >= min_squeeze - 1e-9:
                break
        font_size -= 4
    # Render the digits on their own mask, condense if needed, and centre the ink box in the face.
    mask = Image.new('L', (r + 4, b + 4), 0)
    ImageDraw.Draw(mask).text((0, 0), label, font=font, fill=255, anchor='lt')
    mask = mask.crop((l, t, r, b)).resize((max(1, round(w * squeeze)), h), Image.LANCZOS)
    img.paste(text_color, (round(cx - mask.width / 2), round(cy - mask.height / 2)), mask)
    return img


SEGMENTS = 28
METAL_MATERIAL = 'sign_metal'
METAL_COLOR = (0.62, 0.64, 0.66, 1.0)
TEX_DIR = '/levels/beamng_earth/art/shapes/beamngEarth'
BODY_NAME = 'sign_round_body'     # the model every sign shares: its pole, brackets and the plate's back (trees.lua)


@dataclass(frozen=True)
class RoundSignGeometry:
    """Dimensions (metres) of an upright round sign on its own pole. The plate, the pole and the brackets are
    separate parts (add_plate, add_pole, add_brackets), so another round sign (a different texture, a larger
    plate, a taller pole) is a different instance of this, not a copy of the model code."""
    height: float = 2.47            # total model height (matches the model height in the Lua catalog)
    plate_size: float = 0.90        # the PNG square is mapped onto this many metres
    plate_panel_px: int = 422       # radius of the visible disc in a 1024 px texture
    plate_top_gap: float = 0.08     # pole sticking out above the top of the plate (top bracket + cap live here)
    plate_y_front: float = -0.005
    plate_y_back: float = 0.005
    pole_radius: float = 0.03
    pole_gap: float = 0.05          # clear space between the plate's back and the pole (see pole_y)
    cap_radius: float = 0.0375
    cap_height: float = 0.025
    bracket_offset: float = 0.24    # bracket centres above/below the plate centre
    bracket_half_h: float = 0.02

    @property
    def plate_radius(self) -> float:
        """Visible sign disc radius."""
        return self.plate_size * self.plate_panel_px / 1024

    @property
    def plate_centre_z(self) -> float:
        return self.height - self.plate_top_gap - self.plate_radius

    @property
    def pole_y(self) -> float:
        """Pole centre, behind the plate. The gap keeps the pole's front well clear of the face: a few centimetres
        or less behind it and the depth buffer cannot tell them apart at a distance, and the pole shows through."""
        return self.plate_y_back + self.pole_gap + self.pole_radius

    @property
    def bracket_half_w(self) -> float:
        return self.pole_radius + 0.004


ROUND_SIGN = RoundSignGeometry()
METAL_TILE = 0.5            # metres of sign metal covered by one tile of the metal textures
METAL_TEX_SIZE = 512


def planar_uv(p, n):
    """Planar UVs from the dominant normal axis; one metal tile per METAL_TILE metres."""
    ax, ay, az = abs(n[0]), abs(n[1]), abs(n[2])
    if ay >= ax and ay >= az:
        return (p[0] / METAL_TILE, p[2] / METAL_TILE)
    if ax >= az:
        return (p[1] / METAL_TILE, p[2] / METAL_TILE)
    return (p[0] / METAL_TILE, p[1] / METAL_TILE)


class Mesh:
    def __init__(self, segments: int = SEGMENTS):
        self.segments = segments        # around every disc and cylinder; fewer for the distant levels of detail
        self.pos, self.nrm, self.uv = [], [], []
        self.tri = {'plate': [], 'metal': []}

    def vert(self, p, n, uv=(0.0, 0.0)):
        self.pos.extend(p)
        self.nrm.extend(n)
        self.uv.extend(uv)
        return len(self.pos) // 3 - 1

    def _p(self, i):
        return tuple(self.pos[3 * i:3 * i + 3])

    def _n(self, i):
        return tuple(self.nrm[3 * i:3 * i + 3])

    def tri_add(self, kind, a, b, c):
        pa, pb, pc = self._p(a), self._p(b), self._p(c)
        u = tuple(x - y for x, y in zip(pb, pa))
        v = tuple(x - y for x, y in zip(pc, pa))
        g = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
        ref = [sum(t) for t in zip(self._n(a), self._n(b), self._n(c))]
        if sum(x * y for x, y in zip(g, ref)) < 0:
            b, c = c, b
        self.tri[kind].extend([a, b, c])

    def quad(self, kind, corners, normal):
        """corners: 4 x (pos, uv); one flat normal."""
        ids = [self.vert(p, normal, uv) for p, uv in corners]
        self.tri_add(kind, ids[0], ids[1], ids[2])
        self.tri_add(kind, ids[0], ids[2], ids[3])

    def disc(self, kind, center, radius, normal, plane):
        """Filled circle. plane='xy' (normal along Z) or 'xz' (normal along Y)."""
        c = self.vert(center, normal, planar_uv(center, normal))
        ring = []
        for i in range(self.segments):
            a = 2 * math.pi * i / self.segments
            ca, sa = math.cos(a) * radius, math.sin(a) * radius
            p = (center[0] + ca, center[1] + sa, center[2]) if plane == 'xy' else (center[0] + ca, center[1], center[2] + sa)
            ring.append(self.vert(p, normal, planar_uv(p, normal)))
        for i in range(self.segments):
            self.tri_add(kind, c, ring[i], ring[(i + 1) % self.segments])

    def cylinder(self, kind, center, radius, a0, a1, axis):
        """Smooth side wall. axis='z': centre=(x,y), spans z a0..a1. axis='y': centre=(x,z), spans y a0..a1."""
        repeat = max(1, round(2 * math.pi * radius / METAL_TILE))   # whole tiles around, so the seam matches
        for i in range(self.segments):
            t0 = 2 * math.pi * i / self.segments
            t1 = 2 * math.pi * (i + 1) / self.segments
            ids = []
            for t, lo_hi in ((t0, a0), (t1, a0), (t1, a1), (t0, a1)):
                c, s = math.cos(t), math.sin(t)
                if axis == 'z':
                    p, n = (center[0] + c * radius, center[1] + s * radius, lo_hi), (c, s, 0.0)
                else:
                    p, n = (center[0] + c * radius, lo_hi, center[1] + s * radius), (c, 0.0, s)
                ids.append(self.vert(p, n, (t / (2 * math.pi) * repeat, lo_hi / METAL_TILE)))
            self.tri_add(kind, ids[0], ids[1], ids[2])
            self.tri_add(kind, ids[0], ids[2], ids[3])

    def box(self, kind, x0, x1, y0, y1, z0, z1):
        faces = [
            ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1), (0, -1, 0)),
            ((x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1), (0, 1, 0)),
            ((x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1), (-1, 0, 0)),
            ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1), (1, 0, 0)),
            ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1), (0, 0, 1)),
            ((x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (0, 0, -1)),
        ]
        for a, b, c, d, n in faces:
            self.quad(kind, [(p, planar_uv(p, n)) for p in (a, b, c, d)], n)


def drop_front_facing(m: Mesh, kind: str, start: int) -> None:
    """Remove the triangles of m.tri[kind] from index `start` on that face the viewer (-Y). Nothing that sits behind
    the plate can be seen from the front, but a surface a centimetre behind the face still competes with it in the
    depth buffer at a distance, so the ones that cannot be seen are not built."""
    ids = m.tri[kind]
    kept = ids[:start]
    for i in range(start, len(ids), 3):
        tri = ids[i:i + 3]
        if sum(m._n(v)[1] for v in tri) / 3 > -0.5:
            kept.extend(tri)
    m.tri[kind] = kept


def add_face(m: Mesh, g: RoundSignGeometry) -> None:
    """The textured front (alpha-tested, so the PNG's corners are cut away): the one part that differs per sign."""
    r = g.plate_size / 2
    zc = g.plate_centre_z
    yf = g.plate_y_front
    m.quad('plate', [((-r, yf, zc - r), (0, 0)), ((r, yf, zc - r), (1, 0)),
                     ((r, yf, zc + r), (1, 1)), ((-r, yf, zc + r), (0, 1))], (0, -1, 0))


def add_plate_back(m: Mesh, g: RoundSignGeometry) -> None:
    """The plate's bare-metal back and rim."""
    zc = g.plate_centre_z
    yf, yb = g.plate_y_front, g.plate_y_back
    m.disc('metal', (0.0, yb, zc), g.plate_radius, (0, 1, 0), 'xz')
    m.cylinder('metal', (0.0, zc), g.plate_radius, yf, yb, 'y')


def add_pole(m: Mesh, g: RoundSignGeometry) -> None:
    """A round pole, closed at both ends, with a wider cap on top."""
    cap_z0 = g.height - g.cap_height
    m.cylinder('metal', (0.0, g.pole_y), g.pole_radius, 0.0, cap_z0, 'z')
    m.disc('metal', (0.0, g.pole_y, 0.0), g.pole_radius, (0, 0, -1), 'xy')
    m.cylinder('metal', (0.0, g.pole_y), g.cap_radius, cap_z0, g.height, 'z')
    m.disc('metal', (0.0, g.pole_y, cap_z0), g.cap_radius, (0, 0, -1), 'xy')
    m.disc('metal', (0.0, g.pole_y, g.height), g.cap_radius, (0, 0, 1), 'xy')


def add_brackets(m: Mesh, g: RoundSignGeometry) -> None:
    """Two brackets clamping the plate to the pole (top and bottom)."""
    zc = g.plate_centre_z
    y_far = g.pole_y + g.pole_radius + 0.006
    for dz in (-g.bracket_offset, g.bracket_offset):
        start = len(m.tri['metal'])
        m.box('metal', -g.bracket_half_w, g.bracket_half_w, g.plate_y_back, y_far,
              zc + dz - g.bracket_half_h, zc + dz + g.bracket_half_h)
        drop_front_facing(m, 'metal', start)        # the face towards the viewer is hidden behind the plate


@dataclass(frozen=True)
class SignLod:
    """One level of detail. `size` is the shape's detail size, the number the game reads from the end of the mesh
    node's name (sign128, sign48...): the level is drawn while the shape covers at least that many pixels, and the
    smallest one up to the point the shape is too small to draw at all (so it is kept low, not to shorten the
    300 m a sign is placed out to, trees.lua GROUP_MAX_M). Tune the sizes in the game's shape editor."""
    size: int
    segments: int
    brackets: bool


# triangles of the body: 304, 120, 80 (10 per segment + 24, minus the brackets' 24; the face adds 2). The pole and
# the brackets are a few pixels wide a few tens of metres out; the face is what is seen from afar.
SIGN_LODS = (SignLod(128, 28, True), SignLod(48, 12, False), SignLod(4, 8, False))


def build_round_sign_mesh(g: RoundSignGeometry = ROUND_SIGN, lod: SignLod = SIGN_LODS[0]) -> Mesh:
    """The sign's body, Z up: the plate's bare-metal back and rim, the pole and (nearest level) the brackets. It is
    the same for every sign of one geometry, so it is one model (BODY_NAME) that all the signs share; each sign's
    own model is just its face (build_face_lods), and trees.lua puts the two at one spot."""
    m = Mesh(lod.segments)
    add_plate_back(m, g)
    add_pole(m, g)
    if lod.brackets:
        add_brackets(m, g)
    return m


def build_sign_lods(g: RoundSignGeometry = ROUND_SIGN, lods: tuple[SignLod, ...] = SIGN_LODS) -> list[tuple[SignLod, Mesh]]:
    """The meshes of every level of detail, nearest first."""
    return [(lod, build_round_sign_mesh(g, lod)) for lod in lods]


# The face is 2 triangles whatever the distance: one level, which the game draws at any size from 4 px up.
FACE_LODS = (SignLod(4, SEGMENTS, False),)


def build_face_lods(g: RoundSignGeometry = ROUND_SIGN, lods: tuple[SignLod, ...] = FACE_LODS) -> list[tuple[SignLod, Mesh]]:
    """The face's meshes (the textured quad: the same for every sign, only its texture differs)."""
    out = []
    for lod in lods:
        m = Mesh(lod.segments)
        add_face(m, g)
        out.append((lod, m))
    return out


def _geometry_xml(name: str, lod: SignLod, m: Mesh) -> str:
    n = len(m.pos) // 3
    p = f'{name}_{lod.size}'

    def fmt(vals):
        return ' '.join(repr(round(float(v), 5)) for v in vals)

    def tri_block(kind, mat):
        ids = m.tri[kind]
        if not ids:
            return ''
        return (f'        <triangles count="{len(ids) // 3}" material="{mat}_material">\n'
                f'          <input semantic="VERTEX" source="#{p}_vertices" offset="0"/>\n'
                f'          <input semantic="NORMAL" source="#{p}_normals" offset="0"/>\n'
                f'          <input semantic="TEXCOORD" source="#{p}_uv" offset="0" set="0"/>\n'
                f'          <p>{" ".join(map(str, ids))}</p>\n'
                f'        </triangles>\n')

    return f"""    <geometry id="{p}_geom">
      <mesh>
        <source id="{p}_positions">
          <float_array id="{p}_positions_array" count="{n * 3}">{fmt(m.pos)}</float_array>
          <technique_common>
            <accessor source="#{p}_positions_array" count="{n}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{p}_normals">
          <float_array id="{p}_normals_array" count="{n * 3}">{fmt(m.nrm)}</float_array>
          <technique_common>
            <accessor source="#{p}_normals_array" count="{n}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{p}_uv">
          <float_array id="{p}_uv_array" count="{n * 2}">{fmt(m.uv)}</float_array>
          <technique_common>
            <accessor source="#{p}_uv_array" count="{n}" stride="2">
              <param name="S" type="float"/>
              <param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{p}_vertices">
          <input semantic="POSITION" source="#{p}_positions"/>
        </vertices>
{tri_block('plate', name)}{tri_block('metal', METAL_MATERIAL)}      </mesh>
    </geometry>
"""


def _node_xml(name: str, lod: SignLod, face: bool) -> str:
    """One mesh node per level; the game takes the detail size from the digits ending the node's name (sign128 for
    the body, face4 for a face). A face has its sign's texture, the body the metal."""
    if face:
        material = (f'<instance_material symbol="{name}_material" target="#{name}_material">\n'
                    '                <bind_vertex_input semantic="UVMap" input_semantic="TEXCOORD" input_set="0"/>\n'
                    '              </instance_material>')
    else:
        material = f'<instance_material symbol="{METAL_MATERIAL}_material" target="#{METAL_MATERIAL}_material"/>'
    return f"""      <node id="{name}_node{lod.size}" name="{'face' if face else 'sign'}{lod.size}" type="NODE">
        <instance_geometry url="#{name}_{lod.size}_geom">
          <bind_material>
            <technique_common>
              {material}
            </technique_common>
          </bind_material>
        </instance_geometry>
      </node>
"""


def dae_xml(name: str, png_name: str | None, lods: list[tuple[SignLod, Mesh]]) -> str:
    """A sign's face model, textured with png_name; or, with None, the metal body every sign shares."""
    face = png_name is not None
    mr, mg, mb, _ = METAL_COLOR
    geometries = ''.join(_geometry_xml(name, lod, m) for lod, m in lods)
    nodes = ''.join(_node_xml(name, lod, face) for lod, _ in lods)
    images = f"""  <library_images>
    <image id="{name}_image">
      <init_from>{png_name}</init_from>
    </image>
  </library_images>
""" if face else ''
    face_effect = f"""    <effect id="{name}_effect">
      <profile_COMMON>
        <newparam sid="{name}_surface">
          <surface type="2D">
            <init_from>{name}_image</init_from>
          </surface>
        </newparam>
        <newparam sid="{name}_sampler">
          <sampler2D>
            <source>{name}_surface</source>
          </sampler2D>
        </newparam>
        <technique sid="common">
          <lambert>
            <diffuse>
              <texture texture="{name}_sampler" texcoord="UVMap"/>
            </diffuse>
          </lambert>
        </technique>
      </profile_COMMON>
    </effect>
""" if face else ''
    metal_effect = '' if face else f"""    <effect id="{METAL_MATERIAL}_effect">
      <profile_COMMON>
        <technique sid="common">
          <lambert>
            <diffuse>
              <color>{mr} {mg} {mb} 1</color>
            </diffuse>
          </lambert>
        </technique>
      </profile_COMMON>
    </effect>
"""
    material = f"""    <material id="{name}_material" name="{name}">
      <instance_effect url="#{name}_effect"/>
    </material>
""" if face else f"""    <material id="{METAL_MATERIAL}_material" name="{METAL_MATERIAL}">
      <instance_effect url="#{METAL_MATERIAL}_effect"/>
    </material>
"""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor>
      <author>BeamNG Earth</author>
      <authoring_tool>Python generator</authoring_tool>
    </contributor>
    <created>2026-10-23T00:00:00</created>
    <modified>2026-10-23T00:00:00</modified>
    <unit meter="1" name="meter"/>
    <up_axis>Z_UP</up_axis>
  </asset>
{images}  <library_effects>
{face_effect}{metal_effect}  </library_effects>
  <library_materials>
{material}  </library_materials>
  <library_geometries>
{geometries}  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="{name}_scene" name="{name}_scene">
{nodes}    </visual_scene>
  </library_visual_scenes>
  <scene>
    <instance_visual_scene url="#{name}_scene"/>
  </scene>
</COLLADA>
"""


def write_textures() -> bool:
    """Procedural textures from seeded noise (the same files every run): the sign metal's base colour, normal and
    roughness maps. They tile. Returns False without numpy."""
    try:
        import numpy as np
    except ImportError:
        print('  numpy not found: skipping the metal textures (a flat metal colour is used instead)')
        return False

    n = METAL_TEX_SIZE
    rng = np.random.default_rng(7)
    fx = np.fft.fftfreq(n)[None, :]
    fy = np.fft.fftfreq(n)[:, None]

    def blur(a, sx, sy=None):
        """Gaussian blur that wraps around the edges, so what it makes tiles (sx/sy in pixels)."""
        sy = sx if sy is None else sy
        return np.real(np.fft.ifft2(np.fft.fft2(a) * np.exp(-2 * np.pi ** 2 * ((sx * fx) ** 2 + (sy * fy) ** 2))))

    def field(sx, sy=None):
        """Tileable gaussian-filtered noise, normalised to unit std."""
        out = blur(rng.standard_normal((n, n)), sx, sy)
        return (out - out.mean()) / out.std()

    def crystals(cells):
        """Tileable cellular noise, the spangle of galvanized zinc: the index (0..cells**2) of the nearest of a
        jittered feature point per grid cell, for each pixel."""
        jitter = rng.random((cells, cells, 2))
        py, px = np.mgrid[0:n, 0:n] / (n / cells)
        gy, gx = np.floor(py).astype(int), np.floor(px).astype(int)
        best = np.full((n, n), np.inf)
        ident = np.zeros((n, n), int)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                cy, cx = (gy + dy) % cells, (gx + dx) % cells
                d = np.hypot(py - (gy + dy + jitter[cy, cx, 0]), px - (gx + dx + jitter[cy, cx, 1]))
                closer = d < best
                best = np.where(closer, d, best)
                ident = np.where(closer, cy * cells + cx, ident)
        return ident

    def scratches(count):
        """Thin scratches, mostly along the rolling direction (vertical), drawn wrapped so they tile, 0..1."""
        img = Image.new('L', (n, n), 0)
        draw = ImageDraw.Draw(img)
        for _ in range(count):
            x, y = rng.random(2) * n
            ang = rng.normal(0.0, 0.25) if rng.random() < 0.7 else rng.random() * math.pi
            half = rng.uniform(15, 90)
            dx, dy = math.sin(ang) * half, math.cos(ang) * half
            level = int(rng.uniform(90, 255))
            for ox in (-n, 0, n):
                for oy in (-n, 0, n):
                    draw.line((x + ox - dx, y + oy - dy, x + ox + dx, y + oy + dy), fill=level, width=1)
        a = blur(np.asarray(img, float) / 255.0, 0.7)
        return a / a.max()

    def save(a, name):
        arr = (np.clip(a, 0, 1) * 255).astype(np.uint8)
        Image.fromarray(arr).save(OUT / name)

    def normal_map(height, strength, tilt_x=0.0, tilt_y=0.0):
        dx = (np.roll(height, -1, axis=1) - np.roll(height, 1, axis=1)) * 0.5
        dy = (np.roll(height, -1, axis=0) - np.roll(height, 1, axis=0)) * 0.5
        nm = np.stack([-dx * strength + tilt_x, dy * strength + tilt_y, np.ones_like(height)], axis=-1)
        nm /= np.linalg.norm(nm, axis=-1, keepdims=True)
        return nm * 0.5 + 0.5

    # --- sign metal: brushed, spangled zinc with mottling, dirt and scratches
    mottling = field(36)         # slow blotchy variation
    dirt = np.clip(field(60), 0, None)
    streaks = field(0.9, 28)     # fine vertical brushing/rolling marks
    grain = field(0.8)           # small speckle
    cell = crystals(22)
    cell_tone = rng.standard_normal(22 * 22)[cell]              # a crystal's brightness...
    cell_tilt = rng.standard_normal((22 * 22, 2))[cell]         # ...and the way its facet leans
    scratch = scratches(260)

    lum = 1 + 0.050 * mottling + 0.040 * streaks + 0.035 * grain + 0.025 * cell_tone - 0.035 * dirt + 0.16 * scratch
    tint = 0.006 * cell_tone[..., None] * np.array([1.0, 0.0, -1.0])      # crystals a touch warmer or cooler
    base = (np.array([0.74, 0.76, 0.79]) + tint) * lum[..., None]
    save(base, 'sign_metal_basecolor.png')

    height = 0.6 * streaks + 0.4 * grain - 2.2 * scratch
    save(normal_map(height, 0.8, 0.10 * cell_tilt[..., 0], 0.10 * cell_tilt[..., 1]), 'sign_metal_normal.png')

    rough = (0.34 + 0.040 * mottling + 0.025 * grain + 0.030 * cell_tone + 0.060 * dirt + 0.30 * scratch)
    save(rough, 'sign_metal_roughness.png')

    print('  sign_metal_{basecolor,normal,roughness}.png')
    return True


# The faces stay in the plain (version 1.0) material format: the game cuts the round shape out of the texture's
# alpha there, and the PBR (1.5) format showed the whole square. The gloss is a mild specular, kept low so the
# red keeps its colour; raise FACE_SPECULAR (0..1) or lower FACE_SPECULAR_POWER for more shine.
FACE_SPECULAR = 0.50
FACE_SPECULAR_POWER = 20


def write_materials(has_metal_textures: bool) -> None:
    """sign_speed.materials.json covering every sign PNG in the folder, plus the shared metal material."""
    if has_metal_textures:
        metal = {
            'name': METAL_MATERIAL,
            'mapTo': METAL_MATERIAL,
            'class': 'Material',
            'version': 1.5,
            'Stages': [{
                'baseColorMap': f'{TEX_DIR}/sign_metal_basecolor.png',
                'normalMap': f'{TEX_DIR}/sign_metal_normal.png',
                'roughnessMap': f'{TEX_DIR}/sign_metal_roughness.png',
                'baseColorFactor': [1.0, 1.0, 1.0, 1.0],
                'metallicFactor': 1.0,
                'roughnessFactor': 1.0,         # the map holds the roughness
                'normalMapStrength': 0.8,
                'useAnisotropic': True,
            }, {}, {}, {}],
            'doubleSided': False,
        }
    else:
        metal = {
            'name': METAL_MATERIAL,
            'mapTo': METAL_MATERIAL,
            'class': 'Material',
            'Stages': [{'diffuseColor': list(METAL_COLOR), 'specular': [0.75, 0.77, 0.8, 1.0],
                        'specularPower': 48, 'pixelSpecular': True}, {}, {}, {}],
            'doubleSided': False,
        }
    mats = {METAL_MATERIAL: metal}
    for png in sorted(OUT.glob('sign_speed*.png')):
        n = png.stem
        stage = {'colorMap': f'{TEX_DIR}/{png.name}', 'diffuseColor': [1, 1, 1, 1],
                 'specular': [FACE_SPECULAR, FACE_SPECULAR, FACE_SPECULAR, 1.0],
                 'specularPower': FACE_SPECULAR_POWER, 'pixelSpecular': True}
        mats[n] = {
            'name': n,
            'mapTo': n,
            'class': 'Material',
            'Stages': [stage, {}, {}, {}],
            'alphaTest': True,
            'alphaRef': 127,
            'doubleSided': False,
        }
    (OUT / 'sign_speed.materials.json').write_text(json.dumps(mats, indent=2), encoding='utf-8')


def parse_speed(value: str) -> int | str:
    text = value.strip().lower()
    if text in {'none', 'no-limit', 'nolimit', 'infinite', 'inf'}:
        return 'none'
    try:
        speed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("speed must be an integer from 5 to 200 or 'none'") from exc
    if not 5 <= speed <= 200:
        raise argparse.ArgumentTypeError("speed must be between 5 and 200 inclusive")
    return speed


# Keep in step with server/furniture.py's SIGN_KMH and trees.lua's speedk<n> groups (a server test checks it).
DEFAULT_SPEEDS = [30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150, 'none']


def build_sign_specs(speeds: Iterable[int | str] | None = None) -> dict[str, tuple[str, str, str, str, str]]:
    requested = list(speeds) if speeds is not None else DEFAULT_SPEEDS
    sign_specs: dict[str, tuple[str, str, str, str, str]] = {}
    for speed in requested:
        if speed == 'none':
            sign_specs['sign_speed_none'] = ('NO LIMIT', '#F7F7F7', 'black', 'black', 'black')
        else:
            sign_specs[f'sign_speed{int(speed)}kph'] = (str(int(speed)), '#F7F7F7', '#CC1B1B', '#CC1B1B', 'black')
    return sign_specs


def write_body() -> None:
    """The model every sign shares: the plate's back and rim, the pole and the brackets."""
    path = OUT / f'{BODY_NAME}.dae'
    path.write_text(dae_xml(BODY_NAME, None, build_sign_lods()), encoding='utf-8')
    print(f'  {path.name}')


def write_signs(speeds: Iterable[int | str] | None = None) -> None:
    sign_specs = build_sign_specs(speeds)
    write_body()
    lods = build_face_lods()            # a sign's own model is its face: the same quad, its texture
    for name, (label, bg, accent, accent2, text_color) in sign_specs.items():
        img = make_sign(label, bg, accent, accent2, text_color)
        # The material alpha-tests (alphaRef 127), so only the cut-out matters: a face pixel with alpha below 255
        # would otherwise let the pole behind it show faintly through. Every pixel is fully opaque or fully clear.
        img = img.convert('RGBA')
        img.putalpha(img.getchannel('A').point(lambda v: 255 if v >= 128 else 0))
        png = OUT / f'{name}.png'
        img.save(png)
        dae = OUT / f'{name}.dae'
        dae.write_text(dae_xml(name, png.name, lods), encoding='utf-8')
        print(f'  {png.name} ({png.stat().st_size} bytes)')
    write_materials(write_textures())
    print('  sign_speed.materials.json')


def main() -> None:
    parser = argparse.ArgumentParser(description='Generate European speed-limit sign artwork.')
    parser.add_argument(
        '--speed',
        type=parse_speed,
        nargs='+',
        help="Generate signs for these speeds (each 5 to 200, or 'none' for the no-limit sign). Defaults to the full set.",
    )
    parser.add_argument(
        '--font',
        help='Bold TrueType font for the digits. Defaults to the first installed of Arial Bold, Calibri Bold, '
             'Segoe UI Bold, DejaVu Sans Bold and Liberation Sans Bold.',
    )
    args = parser.parse_args()

    global FONT_PATH
    FONT_PATH = args.font
    write_signs(args.speed)

    print(f'Created European speed sign assets in {OUT}')


if __name__ == '__main__':
    main()