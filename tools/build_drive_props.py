"""build_drive_props.py: the mapped driving features' props (mod/.../art/shapes/beamngEarth/bne_dp_*.dae and
drive_props.materials.json; plain colours, no textures): the stock game has no lift gate, farm gate, height restrictor,
low bollard or railway signal that fits (the asset survey, .claude/work/game_assets, 2026-11-22).

    python3 tools/build_drive_props.py

Each model stands on the ground at its origin, Z up, metres, with two levels of detail and a collision mesh
(build_rail_crossing_props.write_dae: the "_L" separator before a detail level's size where a name ends in a digit):
  bne_dp_liftgate       a lift gate (barrier=lift_gate): its cabinet at the origin, the boom along +X, 4.6 m, down
  bne_dp_liftgate_up    the same, the boom raised (open)
  bne_dp_gate           a five-bar field gate's leaf (barrier=gate / swing_gate): its hinge post at the origin, the leaf
                        3.6 m along +X (the server turns it across the road, closed, or along its edge, open)
  bne_dp_hr4 .. hr10    a height restrictor's frame for a road of 4 / 6 / 8 / 10 m: posts at +-w/2 on X, the beam
                        (red and white) at 2.2 m under its bottom edge (the commonest posted clearance)
  bne_dp_bollard        a 0.95 m steel bollard with a reflective band
  bne_dp_rsignal        a railway main signal: a 4.6 m mast, a black head with three lenses facing -Y (the train)
  bne_dp_rsignal_dwarf  a ground (shunting) signal: a 0.8 m box with two lenses facing -Y
  bne_dp_point          a point machine beside the switch: a 1.1 m grey box
"""
import json
import os
import sys
import uuid

try:        # (first: numpy's AVX-512 kernels held back before anything imports numpy, tools/npyfirst.py)
    import npyfirst  # noqa: E402,F401
except ImportError:     # (loaded by its path into another process, a test: that process's entry point guards)
    pass

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_rail_crossing_props as rx  # noqa: E402

OUT = rx.OUT
COLOURS = {
    "bne_dp_white": ((0.88, 0.88, 0.86), 0.0, 0.5),
    "bne_dp_red": ((0.66, 0.06, 0.05), 0.0, 0.45),
    "bne_dp_yellow": ((0.86, 0.66, 0.06), 0.0, 0.5),
    "bne_dp_metal": ((0.60, 0.61, 0.62), 0.8, 0.45),
    "bne_dp_dark": ((0.12, 0.12, 0.13), 0.3, 0.55),
    "bne_dp_black": ((0.03, 0.03, 0.03), 0.0, 0.6),
    "bne_dp_lens_red": ((0.70, 0.04, 0.03), 0.0, 0.1),
    "bne_dp_lens_green": ((0.05, 0.55, 0.25), 0.0, 0.1),
    "bne_dp_lens_yellow": ((0.85, 0.55, 0.05), 0.0, 0.1),
    "bne_dp_grey": ((0.45, 0.46, 0.45), 0.2, 0.7),
}
BOOM_L, BOOM_Z = 4.6, 0.95
GATE_L, GATE_H = 3.6, 1.2
HR_WIDTHS = (4, 6, 8, 10)
HR_CLEAR = 2.2
STRIPE = 0.5


def striped_beam(m, x0, x1, y, z, half_y, half_z, axis=0):
    """Red and white bands STRIPE m long along X (axis 0) or Z (axis 2) from x0 to x1."""
    k = 0
    a = x0
    while a < x1 - 1e-9:
        b = min(x1, a + STRIPE)
        mat = "bne_dp_red" if k % 2 == 0 else "bne_dp_white"
        if axis == 0:
            m.box(mat, ((a + b) / 2, y, z), ((b - a) / 2, half_y, half_z))
        else:
            m.box(mat, (y, half_z, (a + b) / 2), (half_y, half_z, (b - a) / 2))
        a, k = b, k + 1


def liftgate(segs, up=False, detail=True):
    m = rx.Mesh(segs)
    m.box("bne_dp_yellow", (0.0, 0.0, 0.5), (0.18, 0.18, 0.5))          # the cabinet
    m.box("bne_dp_dark", (0.0, 0.0, 1.02), (0.2, 0.2, 0.02))
    if up:
        if detail:
            k, a = 0, BOOM_Z
            while a < BOOM_Z + BOOM_L - 1e-9:
                b = min(BOOM_Z + BOOM_L, a + STRIPE)
                m.box("bne_dp_red" if k % 2 == 0 else "bne_dp_white", (0.25, 0.0, (a + b) / 2), (0.04, 0.05, (b - a) / 2))
                a, k = b, k + 1
        else:
            m.box("bne_dp_white", (0.25, 0.0, BOOM_Z + BOOM_L / 2), (0.04, 0.05, BOOM_L / 2))
    elif detail:
        striped_beam(m, 0.2, 0.2 + BOOM_L, 0.0, BOOM_Z, 0.05, 0.04)
        m.box("bne_dp_metal", (0.2 + BOOM_L - 0.05, 0.0, BOOM_Z / 2), (0.03, 0.03, BOOM_Z / 2))   # the rest post
    else:
        m.box("bne_dp_white", (0.2 + BOOM_L / 2, 0.0, BOOM_Z), (BOOM_L / 2, 0.05, 0.04))
    return m


def field_gate(segs, detail=True):
    m = rx.Mesh(segs)
    m.cylinder("bne_dp_metal", 0.0, 0.0, 0.0, GATE_H + 0.15, 0.06)       # the hinge post
    bars = 5 if detail else 2
    for k in range(bars):
        z = 0.15 + (GATE_H - 0.2) * k / max(1, bars - 1)
        m.box("bne_dp_metal", (0.08 + GATE_L / 2, 0.0, z), (GATE_L / 2, 0.02, 0.02))
    for x in (0.1, GATE_L + 0.06):
        m.box("bne_dp_metal", (x, 0.0, 0.15 + (GATE_H - 0.2) / 2), (0.025, 0.025, (GATE_H - 0.2) / 2 + 0.03))
    if detail:                                                             # the brace
        d = np.array([GATE_L - 0.1, 0.0, GATE_H - 0.25])
        L = float(np.linalg.norm(d))
        ax = [d / L, np.array([0.0, 1.0, 0.0]), np.cross(d / L, [0.0, 1.0, 0.0])]
        m.box("bne_dp_metal", (0.1 + d[0] / 2, 0.0, 0.15 + d[2] / 2), (L / 2, 0.02, 0.02), axes=ax)
    return m


def height_restrictor(segs, w, detail=True):
    m = rx.Mesh(segs)
    top = HR_CLEAR + 0.3
    for x in (-w / 2 - 0.15, w / 2 + 0.15):
        m.box("bne_dp_metal", (x, 0.0, top / 2), (0.08, 0.08, top / 2))
    if detail:
        striped_beam(m, -w / 2 - 0.1, w / 2 + 0.1, 0.0, HR_CLEAR + 0.15, 0.08, 0.15)
    else:
        m.box("bne_dp_white", (0.0, 0.0, HR_CLEAR + 0.15), (w / 2 + 0.1, 0.08, 0.15))
    return m


def bollard(segs, detail=True):
    m = rx.Mesh(segs)
    m.cylinder("bne_dp_dark", 0.0, 0.0, 0.0, 0.95, 0.07)
    if detail:
        m.cylinder("bne_dp_white", 0.0, 0.0, 0.78, 0.86, 0.072, caps=False)
    return m


def rail_signal(segs, detail=True):
    m = rx.Mesh(segs)
    m.cylinder("bne_dp_grey", 0.0, 0.0, 0.0, 4.6, 0.08)                  # the mast
    m.box("bne_dp_black", (0.0, -0.1, 4.2), (0.22, 0.06, 0.55))          # the head
    for z, mat in ((4.55, "bne_dp_lens_red"), (4.2, "bne_dp_lens_green"), (3.85, "bne_dp_lens_yellow")):
        m.disc_y(mat, 0.0, -0.17, z, 0.1)
        if detail:
            m.box("bne_dp_black", (0.0, -0.21, z + 0.1), (0.13, 0.05, 0.015))   # the hood
    if detail:
        m.box("bne_dp_white", (0.0, -0.08, 2.6), (0.18, 0.02, 0.25))      # the number plate
    return m


def dwarf_signal(segs, detail=True):
    m = rx.Mesh(segs)
    m.box("bne_dp_grey", (0.0, 0.0, 0.2), (0.1, 0.1, 0.2))
    m.box("bne_dp_black", (0.0, -0.05, 0.6), (0.18, 0.08, 0.2))
    for x in (-0.08, 0.08):
        m.disc_y("bne_dp_lens_red" if x < 0 else "bne_dp_white", x, -0.135, 0.65, 0.05)
    return m


def point_machine(segs, detail=True):
    m = rx.Mesh(segs)
    m.box("bne_dp_grey", (0.0, 0.0, 0.18), (0.55, 0.2, 0.18))
    if detail:
        m.box("bne_dp_dark", (0.0, 0.0, 0.37), (0.5, 0.17, 0.01))
    return m


def collision(kind, w=0.0):
    m = rx.Mesh(6)
    if kind == "liftgate":
        m.box("bne_dp_yellow", (0.0, 0.0, 0.5), (0.18, 0.18, 0.5))
    elif kind == "liftgate_closed":
        m.box("bne_dp_yellow", (0.0, 0.0, 0.5), (0.18, 0.18, 0.5))
        m.box("bne_dp_white", (0.2 + BOOM_L / 2, 0.0, BOOM_Z), (BOOM_L / 2, 0.05, 0.05))
    elif kind == "gate":
        m.box("bne_dp_metal", (0.0, 0.0, (GATE_H + 0.15) / 2), (0.06, 0.06, (GATE_H + 0.15) / 2))
        m.box("bne_dp_metal", (0.08 + GATE_L / 2, 0.0, 0.15 + (GATE_H - 0.2) / 2), (GATE_L / 2, 0.03, (GATE_H - 0.2) / 2))
    elif kind == "hr":
        top = HR_CLEAR + 0.3
        for x in (-w / 2 - 0.15, w / 2 + 0.15):
            m.box("bne_dp_metal", (x, 0.0, top / 2), (0.08, 0.08, top / 2))
        m.box("bne_dp_white", (0.0, 0.0, HR_CLEAR + 0.15), (w / 2 + 0.1, 0.08, 0.15))
    elif kind == "bollard":
        m.box("bne_dp_dark", (0.0, 0.0, 0.475), (0.07, 0.07, 0.475))
    elif kind == "signal":
        m.box("bne_dp_grey", (0.0, 0.0, 2.3), (0.08, 0.08, 2.3))
    elif kind == "dwarf":
        m.box("bne_dp_grey", (0.0, 0.0, 0.4), (0.15, 0.1, 0.4))
    elif kind == "point":
        m.box("bne_dp_grey", (0.0, 0.0, 0.18), (0.55, 0.2, 0.18))
    return m


def materials():
    out = {}
    pid = lambda n: str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/" + n))
    for k, (col, met, rough) in COLOURS.items():
        out[k] = {"name": k, "mapTo": k, "class": "Material", "version": 1.5, "persistentId": pid(k),
                  "Stages": [{"baseColorFactor": [*col, 1.0], "metallicFactor": met, "roughnessFactor": rough},
                             {}, {}, {}]}
    with open(os.path.join(OUT, "drive_props.materials.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)


def models():
    out = {
        "bne_dp_liftgate": ([(64, liftgate(16)), (8, liftgate(6, False, False))], collision("liftgate_closed")),
        "bne_dp_liftgate_up": ([(64, liftgate(16, True)), (8, liftgate(6, True, False))], collision("liftgate")),
        "bne_dp_gate": ([(64, field_gate(16)), (8, field_gate(6, False))], collision("gate")),
        "bne_dp_bollard": ([(64, bollard(12)), (8, bollard(6, False))], collision("bollard")),
        "bne_dp_rsignal": ([(64, rail_signal(16)), (8, rail_signal(6, False))], collision("signal")),
        "bne_dp_rsignal_dwarf": ([(64, dwarf_signal(12)), (8, dwarf_signal(6, False))], collision("dwarf")),
        "bne_dp_point": ([(64, point_machine(8)), (8, point_machine(6, False))], collision("point")),
    }
    for w in HR_WIDTHS:
        out[f"bne_dp_hr{w}"] = ([(64, height_restrictor(12, w)), (8, height_restrictor(6, w, False))],
                                collision("hr", w))
    return out


def main():
    materials()
    for name, (lods, col) in models().items():
        path = os.path.join(OUT, name + ".dae")
        rx.write_dae(path, name, lods, col)
        print(path, os.path.getsize(path), "bytes", sum(len(t) for t in lods[0][1].tri.values()), "triangles")


if __name__ == "__main__":
    main()
