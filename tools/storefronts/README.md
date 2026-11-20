# Generate storefront models

The catalog covers 250 chains across restaurants, groceries, pharmacies, home improvement,
discount stores, specialty retail and cinemas. New brands share building designs sized to
the mapped footprint. Examples include In-N-Out Burger, Whataburger, Trader Joe's, Best Buy,
IKEA, PetSmart, AMC Theatres, Regal Cinemas and Cinemark. Standalone multiplexes have a
projecting lobby marquee. A supported tenant with an identifiable exterior wall can use the
shared storefront panel. Standalone Cracker Barrel stores have a timber porch and posts. Convex
four-sided footprints also support a pitched roof. Waffle House uses a low glazed diner, and
IKEA uses a tall blue warehouse with a yellow canopy. The catalog represents typical formats.

Run these offline tools from the repository root using the server Python environment. The generator reads OpenStreetMap XML extracts and writes GLB models plus a local model catalog. Supply a closed building way with its nodes, a supported business name or brand and shop or amenity tag, and a mapped entrance or nearby parking geometry. Supported brands are listed in store_match.py and chain_profiles.py. A ground-floor tenant sharing one mapped exterior edge with a host can receive a facade overlay. A business point inside a retail footprint can also receive a small panel when one exterior wall is distinctly closest and mapped parking faces it. For standalone stores without mapped entrances, the generator checks the addressed street before using nearby parking to choose the front.

```sh
python tools/storefronts/build_stores.py district.osm --output pack
```

Inspect the pack or use the game's threedmr_cache directory as the output to install directly. Existing catalog entries for other models are retained. Use a stable, distinct filename for each extract and run one writer per output directory. Rebuilding an extract replaces its generated catalog entries. [Local model placement and cache location](../../local_models/README.md)

## Generate stores as you drive

Set `BNE_STOREFRONTS=1` in the server environment to enable storefront discovery on near road tiles in a source checkout. There is no city list or extract to prepare. A supported chain name on a low standalone commercial footprint can identify the store directly. For an unnamed commercial footprint, including one mapped only as retail or commercial, the server checks the places layer for a single business inside with confidence of at least 0.8. On an unnamed retail building with multiple tenants, a supported business can receive a ground-floor panel and entrance canopy when one exterior wall is distinctly closest, a mapped parking aisle faces it, and other mapped tenants are clear of the panel. The shared host stays in place. Fuel-category places are excluded from supermarket matching. For standalone retail buildings, multiple parking aisles facing one broad wall can identify the customer approach. Otherwise a nearby road supplies the approximate direction. Large stores with paired entrances can carry a name sign above each entrance. Detailed standalone models can carry a second wall sign on the public approach while keeping the main sign over a mapped entrance. A raised two-sided sign on wider approach walls makes the name legible along the street. Tenant panels use a separate name band above the glazing. In an OSM extract, a named or branded advertising totem can also generate a freestanding two-sided sign at its mapped position, using its height and orientation when available. Installed custom models take priority.

Generated meshes are reused from `threedmr_cache/storefronts`. Data coverage varies by location. The streaming path uses mapped wall material. The offline workflow below also accepts photo observations.

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
