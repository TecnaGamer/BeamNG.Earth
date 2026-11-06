"""build_au_guideposts.py: Australia's guide posts (mod/.../art/shapes/beamngEarth/bne_au_guidepost_red.dae and
bne_au_guidepost_white.dae, au_guideposts.materials.json), placed by server/kantstolpar.py (fork: Queensland).

    python tools/build_au_guideposts.py

AS 1742.2 / Austroads Guide to Road Design Part 6: a white post 1.05 m above the ground (sunk 0.3 m) and 0.10 m across,
the flat flexible kind the Warrego Highway photos show (fork/reference/roads_rural), with a 0.05 x 0.15 m reflector
near its top: red on the post at a driver's left, white on his right. Z up from the ground at its origin; the traffic it
faces comes along local +X, so the reflector that driver sees is on the -X face. Each model carries the other colour on
its back (+X) face, for the traffic the other way on a two-way road: the red model is red front / white back, the white
one white front / red back. Nothing to hit: a flexible post gives way (the Swedish kantstolpe's rule).
"""
import json
import os
import uuid

from build_rail_crossing_props import OUT, Mesh, write_dae

POST_H = 1.05
POST_SINK = 0.3
POST_X, POST_Y = 0.04, 0.10         # m: thickness along the traffic, width across the road
REFLECTOR_W, REFLECTOR_H = 0.05, 0.15
REFLECTOR_Z = 0.92                  # m: its middle (top at 0.995 m)
COLOURS = {
    "bne_au_post": ((0.88, 0.89, 0.87), 0.0, 0.6),              # white, matte
    "bne_au_reflect_red": ((0.78, 0.04, 0.04), 0.1, 0.25),      # retroreflective red
    "bne_au_reflect_white": ((0.95, 0.95, 0.92), 0.1, 0.25),    # retroreflective white
}


def post(front, back):
    """A guide post: reflector materials on its -X (front, the traffic's) and +X (back) faces."""
    m = Mesh(4)
    m.box("bne_au_post", (0.0, 0.0, (POST_H - POST_SINK) / 2), (POST_X / 2, POST_Y / 2, (POST_H + POST_SINK) / 2))
    for mat, side in ((front, -1.0), (back, 1.0)):
        m.box(mat, (side * (POST_X / 2 + 0.001), 0.0, REFLECTOR_Z), (0.001, REFLECTOR_W / 2, REFLECTOR_H / 2))
    return m


def materials():
    out = {}
    pid = lambda n: str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/" + n))
    for k, (col, met, rough) in COLOURS.items():
        out[k] = {"name": k, "mapTo": k, "class": "Material", "version": 1.5, "persistentId": pid(k),
                  "Stages": [{"baseColorFactor": [*col, 1.0], "metallicFactor": met, "roughnessFactor": rough},
                             {}, {}, {}]}
    with open(os.path.join(OUT, "au_guideposts.materials.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)


def main():
    materials()
    for name, front, back in (("bne_au_guidepost_red", "bne_au_reflect_red", "bne_au_reflect_white"),
                              ("bne_au_guidepost_white", "bne_au_reflect_white", "bne_au_reflect_red")):
        path = os.path.join(OUT, name + ".dae")
        m = post(front, back)
        write_dae(path, name, [(4, m)], Mesh(4))
        print(path, os.path.getsize(path), "bytes", sum(len(t) for t in m.tri.values()), "triangles")


if __name__ == "__main__":
    main()
