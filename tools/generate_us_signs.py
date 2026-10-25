#!/usr/bin/env python3
"""Generate the US road signs (MUTCD sizes and colours; the user, 2026-10-25: "can you add the signs now"): speed limit
(R2-1, 15-85 mph), turn and curve warnings (W1-1, W1-2, left and right), advisory speed plaques (W13-1P, 10-60 mph)
and chevrons (W1-8). Each sign is one model, its face (the PNG, alpha-tested) on a sign-shaped metal back and a square
post, in the mod's art folder beside the km/h signs (tools/generate_kmh_speed_signs.py, whose mesh and COLLADA code
this reuses). The face looks along -Y (the viewer stands at -Y), +X to the viewer's right: roadsigns.py and
furniture.speed_signs place them so (+x to the right of the travel direction they face). A plaque has no post: it
hangs on its warning sign's post, below the sign.
    python3 tools/generate_us_signs.py
Writes us_*.png / us_*.dae and us_signs.materials.json; the Lua catalog is us_signs.lua (CATALOG heights = HEIGHTS).
"""
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_kmh_speed_signs as kmh  # noqa: E402

OUT = kmh.OUT
TEX_DIR = kmh.TEX_DIR
METAL = kmh.METAL_MATERIAL
IN = 0.0254                     # metres per inch
PX_PER_M = 640 / (30 * IN)      # a texture's pixels per metre of face (the speed limit's 30 in = 640 px)
YELLOW, BLACK, WHITE = (247, 196, 0, 255), (17, 17, 17, 255), (255, 255, 255, 255)
SPEEDS = list(range(15, 90, 5))
ADVISORY = list(range(10, 85, 5))
POST = 0.065                    # m, a 2.5 in square post
POST_Y0 = 0.035                 # its front, behind the plate's back (a gap the depth buffer can tell apart)
FACE_Y, BACK_Y = -0.005, 0.005

# the model heights (m): trees.lua scales an item by the row's height over these (us_signs.lua, roadsigns.py)
HEIGHTS = {"speed": 2.60, "warn": 3.05, "advisory": 1.90, "chevron": 2.20}


def font(px):
    return kmh.choose_font(px)


def text_c(d, cx, y, s, px, fill=BLACK):
    f = font(px)
    w = d.textlength(s, font=f)
    d.text((cx - w / 2, y), s, font=f, fill=fill)


# --- faces -------------------------------------------------------------------------------------------------------
def face_speed(mph):
    w, h = round(24 * IN * PX_PER_M), round(30 * IN * PX_PER_M)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=26, fill=WHITE)
    d.rounded_rectangle((10, 10, w - 11, h - 11), radius=20, outline=BLACK, width=10)
    text_c(d, w / 2, 70, "SPEED", 104)
    text_c(d, w / 2, 190, "LIMIT", 104)
    text_c(d, w / 2, 330, str(mph), 250)
    return im


def _diamond(size):
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = size / 2
    d.polygon([(c, 0), (size - 1, c), (c, size - 1), (0, c)], fill=YELLOW)
    m = 0.045 * size
    d.polygon([(c, m), (size - 1 - m, c), (c, size - 1 - m), (m, c)], outline=BLACK, width=round(0.018 * size))
    return im, d, c


def _arrowhead(d, tip, ux, uy, size):
    """A filled arrowhead at tip pointing along (ux, uy)."""
    px, py = -uy, ux
    base = (tip[0] - ux * size, tip[1] - uy * size)
    d.polygon([tip, (base[0] + px * size * 0.6, base[1] + py * size * 0.6), (base[0] - px * size * 0.6, base[1] - py * size * 0.6)],
              fill=BLACK)


def face_warn(kind, side):
    """W1-1 (turn: a right-angle arrow) or W1-2 (curve: a bent arrow), turning left or right."""
    size = round(30 * IN * 2 ** 0.5 * PX_PER_M)         # the diamond's bounding square
    im, d, c = _diamond(size)
    s = size / 512.0
    lw = round(30 * s)
    if kind == "turn":
        pts = [(c - 40 * s, c + 120 * s), (c - 40 * s, c - 40 * s), (c + 60 * s, c - 40 * s)]
        d.line(pts, fill=BLACK, width=lw, joint="curve")
        _arrowhead(d, (c + 120 * s, c - 40 * s), 1, 0, 60 * s)
    else:
        # a stem, then a bend to the right (PIL arcs run clockwise from 3 o'clock, y down)
        import math
        x0, r = c - 40 * s, 160 * s
        cx, cy = x0 + r, c + 40 * s
        d.line([(x0, c + 130 * s), (x0, cy)], fill=BLACK, width=lw)
        e = lw / 2                                          # (PIL draws a thick arc inside its box)
        d.arc((cx - r - e, cy - r - e, cx + r + e, cy + r + e), 180, 245, fill=BLACK, width=lw)
        a = math.radians(245)
        end = (cx + r * math.cos(a), cy + r * math.sin(a))
        ux, uy = -math.sin(a), math.cos(a)                # the arc's direction at its end
        _arrowhead(d, (end[0] + ux * 45 * s, end[1] + uy * 45 * s), ux, uy, 62 * s)
    if side == "l":
        im = im.transpose(Image.FLIP_LEFT_RIGHT)
    return im


def face_advisory(mph):
    n = round(18 * IN * PX_PER_M)
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, n - 1, n - 1), radius=18, fill=YELLOW)
    d.rounded_rectangle((8, 8, n - 9, n - 9), radius=12, outline=BLACK, width=8)
    text_c(d, n / 2, 40, str(mph), 200)
    text_c(d, n / 2, n - 140, "MPH", 100)
    return im


def face_chevron(side):
    w, h = round(18 * IN * PX_PER_M), round(24 * IN * PX_PER_M)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=16, fill=YELLOW)
    d.rounded_rectangle((7, 7, w - 8, h - 8), radius=11, outline=BLACK, width=7)
    x0, x1, t = 0.28 * w, 0.72 * w, 0.17 * w
    d.polygon([(x0, 0.16 * h), (x1, 0.5 * h), (x0, 0.84 * h), (x0 + t, 0.84 * h), (x1 + t, 0.5 * h), (x0 + t, 0.16 * h)],
              fill=BLACK)
    if side == "l":
        im = im.transpose(Image.FLIP_LEFT_RIGHT)
    return im


# --- models ------------------------------------------------------------------------------------------------------
def build_mesh(face_w, face_h, zc, back, post_top):
    """The face quad (w x h m centred at height zc), the metal back in its outline (back: [(x, z)...] around the face)
    and a square post to post_top (None: none)."""
    m = kmh.Mesh(8)
    hw, hh = face_w / 2, face_h / 2
    m.quad("plate", [((-hw, FACE_Y, zc - hh), (0, 0)), ((hw, FACE_Y, zc - hh), (1, 0)),
                     ((hw, FACE_Y, zc + hh), (1, 1)), ((-hw, FACE_Y, zc + hh), (0, 1))], (0, -1, 0))
    for a, b in zip(back, back[1:] + back[:1]):             # the back, a fan around its centre
        m.quad("metal", [((0.0, BACK_Y, zc), (0, 0)), ((b[0], BACK_Y, b[1]), (0, 0)), ((a[0], BACK_Y, a[1]), (0, 0)),
                         ((0.0, BACK_Y, zc), (0, 0))], (0, 1, 0))
    if post_top is not None:
        m.box("metal", -POST / 2, POST / 2, POST_Y0, POST_Y0 + POST, 0.0, post_top)
    return m


def rect(w, h, zc):
    return [(-w / 2, zc - h / 2), (w / 2, zc - h / 2), (w / 2, zc + h / 2), (-w / 2, zc + h / 2)]


def diamond(side, zc):
    r = side / 2 ** 0.5
    return [(0.0, zc - r), (r, zc), (0.0, zc + r), (-r, zc)]


def specs():
    """name -> (face image, face w, face h, centre z, back outline, post top)."""
    out = {}
    sw, sh = 24 * IN, 30 * IN
    for v in SPEEDS:
        zc = HEIGHTS["speed"] - 0.04 - sh / 2
        out[f"us_speed{v}"] = (face_speed(v), sw, sh, zc, rect(sw, sh, zc), HEIGHTS["speed"])
    dw = 30 * IN * 2 ** 0.5
    zc = HEIGHTS["warn"] - 0.03 - dw / 2
    for kind in ("turn", "curve"):
        for side in ("l", "r"):
            out[f"us_warn_{kind}_{side}"] = (face_warn(kind, side), dw, dw, zc, diamond(30 * IN, zc), HEIGHTS["warn"])
    aw = 18 * IN
    zc = HEIGHTS["advisory"] - aw / 2
    for v in ADVISORY:
        out[f"us_advisory{v}"] = (face_advisory(v), aw, aw, zc, rect(aw, aw, zc), None)
    cw, ch = 18 * IN, 24 * IN
    zc = HEIGHTS["chevron"] - 0.03 - ch / 2
    for side in ("l", "r"):
        out[f"us_chevron_{side}"] = (face_chevron(side), cw, ch, zc, rect(cw, ch, zc), HEIGHTS["chevron"])
    return out


def node_xml(name, lod):
    """kmh._node_xml binds one material; a US sign has its face and the metal in one model."""
    return f"""      <node id="{name}_node{lod.size}" name="sign{lod.size}" type="NODE">
        <instance_geometry url="#{name}_{lod.size}_geom">
          <bind_material>
            <technique_common>
              <instance_material symbol="{name}_material" target="#{name}_material">
                <bind_vertex_input semantic="UVMap" input_semantic="TEXCOORD" input_set="0"/>
              </instance_material>
              <instance_material symbol="{METAL}_material" target="#{METAL}_material"/>
            </technique_common>
          </bind_material>
        </instance_geometry>
      </node>
"""


def collision_mesh(face_w, face_h, zc, post_top):
    """The solid parts (the user, 2026-10-25: indestructible): the post and a thin slab where the sign plate is."""
    m = kmh.Mesh(8)
    if post_top is not None:
        m.box("metal", -POST / 2, POST / 2, POST_Y0, POST_Y0 + POST, 0.0, post_top)
    m.box("metal", -face_w / 2, face_w / 2, FACE_Y, POST_Y0, zc - face_h / 2, zc + face_h / 2)
    return m


def dae(name, png, lod, mesh, col=None):
    """kmh.dae_xml's face model, with the metal effect and material added and both bound; col: a collision mesh, a
    Colmesh-1 node (the game's collision detail)."""
    face = kmh.dae_xml(name, png, [(lod, mesh)])
    if col is not None:
        geo = kmh._geometry_xml(name + "_col", lod, col)
        face = face.replace("  </library_geometries>", geo + "  </library_geometries>", 1)
    mr, mg, mb, _ = kmh.METAL_COLOR
    face = face.replace("  </library_effects>", f"""    <effect id="{METAL}_effect">
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
  </library_effects>""", 1)
    face = face.replace("  </library_materials>", f"""    <material id="{METAL}_material" name="{METAL}">
      <instance_effect url="#{METAL}_effect"/>
    </material>
  </library_materials>""", 1)
    start = face.index("    <visual_scene")
    a, b = face.index(">", start) + 2, face.index("    </visual_scene>")
    nodes = node_xml(name, lod)
    if col is not None:
        nodes += f"""      <node id="{name}_colmesh" name="Colmesh-1" type="NODE">
        <instance_geometry url="#{name}_col_{lod.size}_geom">
          <bind_material>
            <technique_common>
              <instance_material symbol="{METAL}_material" target="#{METAL}_material"/>
            </technique_common>
          </bind_material>
        </instance_geometry>
      </node>
"""
    return face[:a] + nodes + face[b:]


def materials(names):
    mats = {}
    for n in names:
        mats[n] = {"name": n, "mapTo": n, "class": "Material",
                   "Stages": [{"colorMap": f"{TEX_DIR}/{n}.png", "diffuseColor": [1, 1, 1, 1],
                               "specular": [kmh.FACE_SPECULAR] * 3 + [1.0], "specularPower": kmh.FACE_SPECULAR_POWER,
                               "pixelSpecular": True}, {}, {}, {}],
                   "alphaTest": True, "alphaRef": 127, "doubleSided": False}
    return mats


def main():
    lod = kmh.SignLod(4, 8, False)
    sp = specs()
    for name, (im, w, h, zc, back, top) in sp.items():
        im = im.convert("RGBA")
        im.putalpha(im.getchannel("A").point(lambda v: 255 if v >= 128 else 0))
        png = OUT / f"{name}.png"
        im.save(png)
        (OUT / f"{name}.dae").write_text(dae(name, png.name, lod, build_mesh(w, h, zc, back, top),
                                             collision_mesh(w, h, zc, top)), encoding="utf-8")
    (OUT / "us_signs.materials.json").write_text(json.dumps(materials(sorted(sp)), indent=2), encoding="utf-8")
    print(f"{len(sp)} US signs in {OUT}")


if __name__ == "__main__":
    main()
