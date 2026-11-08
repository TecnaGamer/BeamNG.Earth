"""build_au_street_amenities.py: Australia's street payphone and post box (mod/.../art/shapes/beamngEarth/bne_au_payphone.dae,
bne_au_postbox.dae, au_street_amenities.materials.json), placed by server/streetamenities.py (fork: Queensland).

    python tools/build_au_street_amenities.py

Generic shapes of the kinds, with no operator's logo or wordmark. Z up from the ground at the origin (both sunk 0.1 m),
the front towards -Y (the layout of build_rail_crossing_props.py: the server turns it to the road).
  bne_au_payphone  the classic street booth: a slim open-fronted steel frame 0.9 x 0.8 x 2.2 m, glass side and back panels,
                   a blue band and a rounded orange hood on top, a dark phone unit on the back wall
  bne_au_postbox   an upright red box 0.5 x 0.45 x 1.25 m with a domed top, a white band, a dark slot near the top of its
                   front, on a short dark plinth
"""
import json
import os
import uuid

from build_rail_crossing_props import OUT, Mesh, write_dae

SINK = 0.1
COLOURS = {      # (base colour, metallic, roughness)
    "bne_au_amen_steel": ((0.62, 0.64, 0.66), 0.7, 0.35),      # brushed stainless
    "bne_au_amen_orange": ((0.95, 0.38, 0.08), 0.1, 0.35),
    "bne_au_amen_blue": ((0.05, 0.20, 0.55), 0.1, 0.4),
    "bne_au_amen_phone": ((0.55, 0.57, 0.60), 0.6, 0.4),      # the phone's silver body
    "bne_au_amen_kick": ((0.30, 0.31, 0.33), 0.4, 0.6),       # the lower panels
    "bne_au_amen_red": ((0.80, 0.06, 0.05), 0.1, 0.4),
    "bne_au_amen_white": ((0.92, 0.92, 0.90), 0.0, 0.5),
    "bne_au_amen_dark": ((0.05, 0.05, 0.06), 0.0, 0.6),
}
GLASS = "bne_au_amen_glass"
GLASS_RGBA = (0.70, 0.76, 0.78, 0.18)       # clear, faintly grey


def prism(m, mat, profile, y0, y1):
    """The (x, z) profile (counter-clockwise seen from -Y) extruded along Y from y0 to y1, faces outward."""
    m.face(mat, [(x, y0, z) for x, z in profile])
    m.face(mat, [(x, y1, z) for x, z in reversed(profile)])
    for i, (a, b) in enumerate(zip(profile, profile[1:] + profile[:1])):
        m.face(mat, [(a[0], y0, a[1]), (a[0], y1, a[1]), (b[0], y1, b[1]), (b[0], y0, b[1])])


def payphone():
    m = Mesh(4)
    S, O, B, P = "bne_au_amen_steel", "bne_au_amen_orange", "bne_au_amen_blue", "bne_au_amen_phone"
    for sx in (-1, 1):
        for sy in (-1, 1):                                          # the four corner posts
            m.box(S, (sx * 0.425, sy * 0.375, 0.85), (0.025, 0.025, 0.95))
    m.box(S, (0.0, 0.0, -0.04), (0.45, 0.40, 0.06))                 # the floor
    # glass: one double-sided pane a side and the back (a thin glass box sorted its faces badly: a dark triangle across
    # each pane in game), over a steel kick panel
    for sx in (-1, 1):
        m.face(GLASS, [(sx * 0.425, -0.35, 0.50), (sx * 0.425, 0.35, 0.50), (sx * 0.425, 0.35, 1.80),
                       (sx * 0.425, -0.35, 1.80)])
        m.box("bne_au_amen_kick", (sx * 0.425, 0.0, 0.27), (0.008, 0.35, 0.23))
    m.face(GLASS, [(-0.40, 0.375, 0.50), (0.40, 0.375, 0.50), (0.40, 0.375, 1.80), (-0.40, 0.375, 1.80)])
    m.box("bne_au_amen_kick", (0.0, 0.375, 0.27), (0.40, 0.008, 0.23))
    m.box(B, (0.0, 0.0, 1.86), (0.46, 0.41, 0.06))                  # the band under the hood
    prism(m, O, [(-0.48, 1.92), (0.48, 1.92), (0.48, 2.05), (0.43, 2.14), (0.30, 2.19), (0.0, 2.20),
                 (-0.30, 2.19), (-0.43, 2.14), (-0.48, 2.05)], -0.43, 0.43)
    m.box(P, (0.0, 0.30, 1.30), (0.17, 0.06, 0.32))                 # the phone unit on the back wall
    m.box("bne_au_amen_dark", (0.0, 0.239, 1.48), (0.10, 0.002, 0.06))       # its screen
    m.box("bne_au_amen_dark", (0.03, 0.239, 1.25), (0.06, 0.002, 0.08))      # its keypad
    m.box("bne_au_amen_dark", (-0.13, 0.22, 1.32), (0.035, 0.035, 0.11))     # its handset
    return m


def postbox():
    m = Mesh(4)
    m.box("bne_au_amen_dark", (0.0, 0.0, 0.0), (0.28, 0.25, 0.10))  # the plinth
    prism(m, "bne_au_amen_red", [(-0.25, 0.1), (0.25, 0.1), (0.25, 1.0), (0.2378, 1.0695), (0.2023, 1.1323), (0.1469, 1.182), (0.0773, 1.214), (0.0, 1.225), (-0.0773, 1.214), (-0.1469, 1.182), (-0.2023, 1.1323), (-0.2378, 1.0695), (-0.25, 1.0)], -0.225, 0.225)     # a round top
    m.box("bne_au_amen_white", (0.0, 0.0, 0.70), (0.256, 0.231, 0.06))      # the band
    m.box("bne_au_amen_dark", (0.0, -0.2265, 0.92), (0.19, 0.003, 0.05))    # the slot's plate
    m.box("bne_au_amen_dark", (0.0, -0.229, 0.92), (0.15, 0.003, 0.0175))   # the slot
    return m


def collision(hx, hy, top):
    m = Mesh(4)
    m.box("bne_au_amen_steel", (0.0, 0.0, (top - SINK) / 2), (hx, hy, (top + SINK) / 2))
    return m


def materials():
    out = {}
    pid = lambda n: str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/" + n))
    for k, (col, met, rough) in COLOURS.items():
        out[k] = {"name": k, "mapTo": k, "class": "Material", "version": 1.5, "persistentId": pid(k),
                  "Stages": [{"baseColorFactor": [*col, 1.0], "metallicFactor": met, "roughnessFactor": rough},
                             {}, {}, {}]}
    out[GLASS] = {"name": GLASS, "mapTo": GLASS, "class": "Material", "version": 1.5, "persistentId": pid(GLASS),
                  "Stages": [{"baseColorFactor": list(GLASS_RGBA), "metallicFactor": 0.0, "roughnessFactor": 0.1},
                             {}, {}, {}], "translucent": True, "castShadows": False, "doubleSided": True}
    with open(os.path.join(OUT, "au_street_amenities.materials.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)


def main():
    materials()
    for name, m, col in (("bne_au_payphone", payphone(), collision(0.45, 0.40, 2.2)),
                         ("bne_au_postbox", postbox(), collision(0.28, 0.25, 1.25))):
        path = os.path.join(OUT, name + ".dae")
        write_dae(path, name, [(4, m)], col)
        print(path, os.path.getsize(path), "bytes", sum(len(t) for t in m.tri.values()), "triangles")


if __name__ == "__main__":
    main()
