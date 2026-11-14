#!/usr/bin/env python3
"""make_station_textures.py: the railway stations' textures (task 037) from ChatGPT-made albedo tiles in assets/ai_inbox
(platform_pavers.png, platform_tactile_yellow.png, canopy_steel_neutral.png) -> the mod's texture sets
(<name>_b.color.png, _nm.normal.png, _r.data.png, as ingest_rail_textures.py makes them) and railstation.materials.json:

    bne_platform_pavers         the platform's top (concrete pavers)
    bne_platform_tactile        the yellow tactile band along the track edge
    bne_canopy_<colour>         the canopies and their columns: the neutral steel tinted to each colour of CANOPY_PALETTE
                                (railstations.canopy_look picks the nearest to the colour the map gives a roof)

    python tools/make_station_textures.py
"""
import json
import os
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server"))

import ingest_rail_textures as irt  # noqa: E402
import railstations  # noqa: E402

irt.TEXTURES.update({
    "pavers": dict(src="platform_pavers.png", out="bne_platform_pavers", normal=3.0, rough=(0.72, 0.92), flatten=True),
    "tactile": dict(src="platform_tactile_yellow.png", out="bne_platform_tactile", normal=4.5, rough=(0.65, 0.88),
                    flatten=True),
    "steel": dict(src="canopy_steel_neutral.png", out="bne_canopy_steel", normal=0.8, rough=(0.45, 0.7), flatten=True),
})
MATERIALS = os.path.join(irt.OUT, "railstation.materials.json")


def material(name, base, tint=(1.0, 1.0, 1.0), ground="CONCRETE", metallic=0.0):
    return {
        "name": name, "mapTo": name, "class": "Material", "version": 1.5,
        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/railstation/" + name)),
        "Stages": [{
            "baseColorMap": irt.TEX_VFS + base + "_b.color.png",
            "normalMap": irt.TEX_VFS + base + "_nm.normal.png",
            "roughnessMap": irt.TEX_VFS + base + "_r.data.png",
            "metallicFactor": metallic,
            "baseColorFactor": [round(tint[0], 4), round(tint[1], 4), round(tint[2], 4), 1.0],
        }, {}, {}, {}],
        "groundType": ground,
        "translucent": False,
    }


def main():
    for key in ("pavers", "tactile", "steel"):
        if not irt.ingest(key):
            print("missing", irt.TEXTURES[key]["src"], "in", irt.INBOX)
            return 1
    mats = {
        railstations.PAVERS: material(railstations.PAVERS, "bne_platform_pavers", ground="ASPHALT"),
        railstations.TACTILE: material(railstations.TACTILE, "bne_platform_tactile", ground="ASPHALT"),
    }
    # the neutral steel is light grey (~#B4B6B8): a colour c is that grey times c / grey
    grey = 0.71
    for look, rgb in railstations.CANOPY_PALETTE.items():
        tint = tuple(min(1.6, v / grey) for v in rgb)
        name = railstations.canopy_material(look)
        mats[name] = material(name, "bne_canopy_steel", tint, ground="METAL", metallic=0.2)
    with open(MATERIALS, "w", encoding="utf8") as f:
        json.dump(mats, f, indent=2)
    print("materials:", ", ".join(mats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
