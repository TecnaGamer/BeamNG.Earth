"""Local street lamp posts (the user, 2026-11-07: "the lamps are not the local ones"): in Europe and its neighbours a
street lamp is a galvanised steel tube or a concrete post with an up-swept bracket (tools/build_city_lamps.py), not the
American eca_lamppost that furniture.place puts everywhere. furniture.place makes its rows as before (positions, spacing,
height) and this module changes the group of the rows that stand in a country of the poles' set (poleprofiles
"poles" / "europe" modes), by a hash of the place, so every tile and anchor makes the same choice.

The lens under the luminaire (furniture.lamp_lenses) follows LAMP_HEADS, the colliders COLLISION_BOXES (the post only: the
arm is overhead), both of furniture.py, which takes the tables from here.
"""
import hashlib

VERSION = "city-lamps-v1"                    # (in the poles profile token: tiles made without them are made again)
G_STEEL, G_CONCRETE = "eu_lamp_steel", "eu_lamp_concrete"
GROUPS = (G_STEEL, G_CONCRETE)
HEIGHT = 9.0                                # the catalog height (trees.lua): a row's height over it is its scale
HEAD = (2.10, 0.0, 8.77)                    # the luminaire's underside (tools/build_city_lamps.py LENS_AT)
# the share of concrete posts by country (the rest steel): Germany lights with steel, the post-Soviet and Visegrad
# towns have many concrete ones (the user's photo of Vinnytsia: a concrete post with a bracket lamp)
CONCRETE_SHARE = {"DE": 0.1, "AT": 0.1, "CH": 0.0, "NL": 0.1, "BE": 0.1, "LU": 0.1, "DK": 0.1, "SE": 0.1, "NO": 0.1,
                  "FI": 0.1, "GB": 0.15, "IE": 0.15, "FR": 0.3, "ES": 0.25, "IT": 0.25, "PT": 0.25}
DEFAULT_SHARE = 0.55
COLLISION_BOXES = {
    G_STEEL: (HEIGHT, ((-0.14, -0.14, -0.2, 0.14, 0.14, 7.9),
                       (-0.14, -0.14, 7.9, 2.65, 0.14, 8.95))),
    G_CONCRETE: (HEIGHT, ((-0.17, -0.12, -0.2, 0.17, 0.12, 7.9),
                          (-0.17, -0.12, 7.9, 2.65, 0.14, 8.95))),
}


def _h01(*vals):
    h = hashlib.blake2b(repr(vals).encode(), digest_size=8).digest()
    return int.from_bytes(h, "little") / 2.0 ** 64


def group_for(country, x, y):
    """The lamp group of a lamp post at (x, y) metres of the tile's frame in `country`."""
    share = CONCRETE_SHARE.get(country, DEFAULT_SHARE)
    return G_CONCRETE if _h01("lamp", country, round(x, 1), round(y, 1)) < share else G_STEEL


def localize(rows, country_at, lamp_group="streetlight"):
    """rows with every lamp post row (group `lamp_group`) in a country of the set turned into a local lamp group.
    country_at(x, y, z) -> an ISO code or None (stock). The rows' heights are kept: the model's catalog height is the
    stock post's, 9.0 m, so a scaled highway post scales alike."""
    out = []
    for r in rows:
        if r[0] == lamp_group:
            country = country_at(float(r[1]), float(r[2]), float(r[3]))
            if country:
                r = [group_for(country, float(r[1]), float(r[2]))] + list(r[1:])
        out.append(r)
    return out
