"""Dumps the map data round railway stations as test fixtures (server/tests/data/railstations/<name>.json).

    cd server && ..\\.venv\\Scripts\\python ..\\tools\\dump_railstation_fixture.py [name ...]

Reads Overture cells through mapdata (network on the first run, the cache after) and writes the items
busstations.items_from_layers makes within 260 m of the hub, in metres of an ENU frame at the hub. The hub is the
OpenStreetMap railway=station node of docs/new/037_rail-stations_2026-11-09.md. The rail lines are in the roads layer.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))

import geodesy      # noqa: E402
import mapcells     # noqa: E402
import mapdata      # noqa: E402
import busstations  # noqa: E402

HUBS = {
    "vinnytsia": (49.23926, 28.51108),       # underpass (tunnel=yes footway + steps), tram platforms beside it
    "kalynivka": (49.43487, 28.52917),       # plain station: platforms and crossings only
    "kozyatyn": (49.70507, 28.82993),        # footbridge with stairs, 6 platform polygons
    "kyiv_pas": (50.44001, 30.48848),        # 16 platform polygons, bridges, tunnels, levels -1 / +1 / +2
    "hk_hln": (50.21454, 15.81012),          # CZ: bench / bin / lit / shelter, a corridor bridge, tunnels
    "trutnov": (50.5652, 15.91106),
    "pardubice": (50.03171, 15.75609),       # platform_edge, 8 bridges, 13 tunnels, 11 lifts
    "passau": (48.57371, 13.45047),          # DE: platform_edge, height, level, tunnels (corridor)
}
LAYERS = ("infrastructure", "buildings", "places", "roads", "land_use")
OUT = os.path.join(os.path.dirname(HERE), "server", "tests", "data", "railstations")


KEEP_INFRA = {"transit", "pedestrian", "bridge"}          # (sub) bench / bin / lift points, platforms, stops, bridges
KEEP_LAND = {"transportation", "pedestrian", "developed", "park"}
KEEP_BUILDINGS = {"transportation", "outbuilding", "civic", "commercial", "service", ""}
KEEP_ROADS_NEAR_M = 150.0                                 # the streets round the hub; rails and footways at any distance


def slim(items):
    """The items a railway station's generator reads, not the town round them (the files are test data in git)."""
    import numpy as np
    out = []
    for it in items:
        lay, sub, cls = it["layer"], it["sub"], it["cls"]
        near = float(np.hypot(it["xy"][:, 0], it["xy"][:, 1]).min())
        if lay == "places" or (lay == "infrastructure" and sub not in KEEP_INFRA and cls not in ("crossing", "street_lamp")):
            continue
        if lay == "land_use" and sub not in KEEP_LAND:
            continue
        if lay == "buildings" and sub not in KEEP_BUILDINGS and near > 120.0:
            continue
        if lay == "roads" and sub != "rail" and cls not in ("footway", "steps", "path", "pedestrian", "corridor", "cycleway")                 and near > KEEP_ROADS_NEAR_M:
            continue
        out.append(it)
    return out


def main(names):
    md = mapdata.MapData()
    os.makedirs(OUT, exist_ok=True)
    for name in names or HUBS:
        lat, lon = HUBS[name]
        frame = geodesy.EnuFrame(lat, lon, 0.0)
        keys = mapcells.cells_for_points([lat - 0.004, lat + 0.004], [lon - 0.006, lon + 0.006])
        layers = {ln: [md.cell(k, layers=(ln,)).layers.get(ln) for k in keys] for ln in LAYERS}
        items = slim(busstations.items_from_layers(frame, layers, radius=380.0))
        path = os.path.join(OUT, name + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"hub": [lat, lon], "items": busstations.items_to_json(items)}, f, ensure_ascii=False)
        print(name, len(items), "items", os.path.getsize(path) // 1024, "KB")


if __name__ == "__main__":
    main(sys.argv[1:])
