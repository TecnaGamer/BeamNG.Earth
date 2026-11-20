# Generate storefront models

Run these offline tools from the repository root using the server Python environment. The generator reads OpenStreetMap XML extracts and writes GLB models plus a local model catalog. Supply a closed building way with its nodes, a supported business name or brand and shop or amenity tag, and a mapped entrance or nearby parking geometry. Supported brands are listed in store_match.py. A ground-floor tenant sharing one mapped exterior edge with a host can receive a facade overlay.

```sh
python tools/storefronts/build_stores.py district.osm --output pack
```

Inspect the pack or use the game's threedmr_cache directory as the output to install directly. Existing catalog entries for other models are retained. Use a stable, distinct filename for each extract and run one writer per output directory. Rebuilding an extract replaces its generated catalog entries. [Local model placement and cache location](../../local_models/README.md)

## Use a wall photo

Choose a roughly front-on crop that contains several courses of visible brick. Crop coordinates are pixels from the upper left after applying the photo orientation. Camera coordinates are longitude then latitude. Supply the true-north bearing from the camera **to the crop centre**. An off-centre crop can have a different bearing from the centre of the original image.

```sh
python tools/storefronts/detect_wall_material.py wall.jpg --crop 40 80 320 280 --osm district.osm --camera -86.75 36.15 --bearing 90 --source local-photo-1 --output wall.json
python tools/storefronts/build_stores.py district.osm --wall-observation wall.json --output pack
```

The coordinates above illustrate the argument format. Use the actual camera position and bearing for your photo. The matcher checks mapped closed building ways up to 80 metres away. It selects the first wall hit by the centre ray and rays five degrees to either side. An ambiguous direction produces an error. For a surveyed wall, supply `--way ID --wall LON1 LAT1 LON2 LAT2` in place of the three camera arguments. Pass a directory to `--wall-observation` to build from several records.

The material detector recognizes staggered brickwork or returns unknown. A mapped building material takes priority. Accepted brick observations affect one wall of a supported standalone store. McDonald's uses separate geometry. Photos are processed locally at build time. Street imagery retrieval, multipolygon visibility and automatic crop selection are outside the current tool.

## Validation

```sh
python -m unittest discover -s tools/storefronts -p test_photo_match.py
python tools/storefronts/test_wall_observations.py
python -m unittest discover -s server/tests -p test_buildings.py -k CustomModelSuppressionTest
```
