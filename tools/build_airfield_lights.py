"""build_airfield_lights.py: the airfield lights' glow balls (mod/.../art/shapes/beamngEarth/bne_afl_<colour>.dae, one
shape per colour: white, green, red, blue, yellow; airfield_lights.materials.json), night only, the street lamps' far
glow (build_lamp_glow.py: the same ball in the engine's detail levels, one material per level) made small and tinted.
server/airfieldlights.py emits one forest row per light in the group afl_<colour>; trees.lua catalogues the groups
(1 m high shapes, never colliding, off by day) and nightlights.lua gives each bne_afl_<colour><k> its colour and light
after dusk, switching the groups on with the lamps' glow.

A runway edge or taxiway light is a fitting some 15 cm across, not a street lamp's 0.6 m lens: each ball is AFL_SCALE of
the lamp glow's at every level (radii and the speck's range both, so the engine's detail sizes come out the same:
every ball spans the same pixels on screen, its fitting switching in nearer). Near the camera the lens-less speck is
the first ball itself (a lamp's is a 1 cm speck over its lens). Materials: bne_afl_<colour>0 .. 7, nearest first, dark
by day.

    python3 tools/build_airfield_lights.py
"""
import json
import os

import build_lamp_glow as lg

# each ball's size against the lamp glow's, at every level (0.6 drew pinpoints from 100 m off at night: the user,
# 2026-11-17: "make all the lights a bit brighter and larger, the ground based ones": 1.5 times, with nightlights.lua's
# AFL_NITS_FACTOR; the same detail sizes, so every ball switches 1.5 times farther out and reads 1.5 times larger)
AFL_SCALE = 0.9
COLOURS = {                                         # nightlights.lua AFL_COLOURS (its emissive colours; the same)
    "white": (1.0, 0.95, 0.85),
    "green": (0.2, 1.0, 0.35),
    "red": (1.0, 0.12, 0.08),
    "blue": (0.2, 0.4, 1.0),
    "yellow": (1.0, 0.75, 0.15),
}
RADII = tuple(r * AFL_SCALE for r in lg.GLOW_R)     # m
NEAR_M = lg.NEAR_M * AFL_SCALE                      # m: the first ball's own range out (the lamps' speck's)
MATERIALS_FILE = "airfield_lights.materials.json"


def material_names(colour):
    return [f"bne_afl_{colour}{k}" for k in range(len(RADII))]


def main():
    mats = {}
    for colour, rgb in COLOURS.items():
        names = material_names(colour)
        for name in names:
            mats[name] = lg.glow_material(name, rgb)
        # the speck (nearest level) is the first ball in its material; the others a ball each
        xml = lg.shape_xml(RADII, RADII[0], NEAR_M, [names[0]] + names, " ".join(f"{c:g}" for c in rgb))
        out = os.path.join(lg.SHAPES, f"bne_afl_{colour}.dae")
        lg.write_shape(out, xml)
        print(out, os.path.getsize(out), "bytes")
    path = os.path.join(lg.SHAPES, MATERIALS_FILE)
    lg.write_json(path, mats)
    print(path, os.path.getsize(path), "bytes;", len(mats), "materials; detail sizes",
          [round(s, 1) for s in lg.detail_sizes(RADII, NEAR_M)])


if __name__ == "__main__":
    main()
