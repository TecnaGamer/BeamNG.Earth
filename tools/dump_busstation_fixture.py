"""Dumps the map data round bus stations as test fixtures (server/tests/data/busstations/<name>.json).

    cd server && ..\\.venv\\Scripts\\python ..\\tools\\dump_busstation_fixture.py [name ...]

Reads Overture cells through mapdata (network on the first run, the cache after) and writes the items
busstations.items_from_layers makes within 260 m of the hub, in metres of an ENU frame at the hub. The hub is the
OpenStreetMap bus_station node/way centre of docs/new/020_bus-stations_2026-11-06.md.
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
    "hk_terminal": (50.21687, 15.81308),
    "nachod": (50.41761, 16.17092),
    "vin_central": (49.23694, 28.48294),
    "vin_west": (49.23703, 28.40386),
    "vin_east": (49.24403, 28.53476),
    "mohyliv": (48.45123, 27.79608),
    "vin3": (49.22919, 28.53192),
    "brno_zvonarka": (49.1860, 16.6180),          # the roof mapped as a building, the stops in it
    "wien_erdberg": (48.1915, 16.4150),           # a viaduct over the yard
    "london_victoria": (51.4925, -0.1485),        # a coach company's office mapped as a bus_station node
    "trutnov": (50.56469, 15.90835),              # plan 033: poles on the platforms
    "rakovnik": (50.10057, 13.73404),             # stock shelters and stop posts beside the bays
    "cesky_krumlov": (48.8117, 14.32253),
    "passau": (48.57262, 13.45604),               # the platform's edge by the railway
}
LAYERS = ("infrastructure", "buildings", "places", "roads", "land_use")
OUT = os.path.join(os.path.dirname(HERE), "server", "tests", "data", "busstations")


def main(names):
    md = mapdata.MapData()
    os.makedirs(OUT, exist_ok=True)
    for name in names or HUBS:
        lat, lon = HUBS[name]
        frame = geodesy.EnuFrame(lat, lon, 0.0)
        keys = mapcells.cells_for_points([lat - 0.003, lat + 0.003], [lon - 0.005, lon + 0.005])
        layers = {ln: [md.cell(k, layers=(ln,)).layers.get(ln) for k in keys] for ln in LAYERS}
        items = busstations.items_from_layers(frame, layers)
        path = os.path.join(OUT, name + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"hub": [lat, lon], "items": busstations.items_to_json(items)}, f, ensure_ascii=False)
        print(name, len(items), "items", os.path.getsize(path) // 1024, "KB")


if __name__ == "__main__":
    main(sys.argv[1:])
