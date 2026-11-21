# Generate storefront models

![Twelve examples from the storefront catalog](catalog/overview.jpg)

[Browse all 250 exported models by name](catalog/README.md).

| Map evidence | What appears in the world |
| --- | --- |
| Supported standalone store | A building fitted to the mapped outline with the brand's materials, entrance and signs. |
| Tenant with a clear, parking-facing exterior wall | A facade panel and canopy on its part of the existing building. |
| Recognized freestanding sign node | A separate two-sided sign at the mapped position, even across a tile boundary from the store. |
| Mapped material or a supplied brick observation | Material applied to the building or observed wall. The offline importer accepts photo observations. |

The catalog covers 250 chains across restaurants, groceries, pharmacies, home improvement,
discount stores, specialty retail and cinemas. New brands share building designs sized to
the mapped footprint. Examples include In-N-Out Burger, Whataburger, Trader Joe's, Best Buy,
IKEA, PetSmart, AMC Theatres, Regal Cinemas and Cinemark. Standalone multiplexes have a
projecting lobby marquee. A supported tenant with an identifiable exterior wall can use the
shared storefront panel. Standalone Cracker Barrel stores have a timber porch and posts. Convex
four-sided footprints also support a pitched roof. Waffle House uses a low glazed diner, and
IKEA uses a tall blue warehouse with a yellow canopy. Bass Pro Shops can use a projecting lodge
entrance with a timber truss and stone bases when one customer entrance has enough wall on each side.
An additional 51 brands use 26 entrance designs in `entrance_designs.py`, including
striped gables, low hipped roofs, castle towers, three-arch arcades, porches, outfitter gables,
market halls, coffee canopies, diner fins, stepped signs, glazed showrooms and an arched cinema lobby. A standalone AMC with at least 16 m of clear entrance frontage can get a tall glass arch, paired doors and a deep marquee. Each entrance
fits between its neighbouring doors and wall ends. Publix and Harris Teeter use different
crowns above their entrance windows. Lidl uses a sloped glazed entrance. Old Navy,
Hollister and Uniqlo also adapt their display windows to the available tenant frontage.
Uniqlo uses a projecting red sign cabinet on standalone stores. A wide tenant fascia
can carry the Japanese and English square marks side by side. The large roadside
format draws on the [Maebashi Minami IC store](https://www.fastretailing.com/eng/ir/library/pdf/ar2023_en_05.pdf).
A narrow entrance or a shared service wall
uses the existing building design. The catalog represents typical formats, including some
classic restaurant designs. Map records rarely identify the renovation era of an individual branch.

Run these offline tools from the repository root using the server Python environment. The generator reads OpenStreetMap XML extracts and writes GLB models plus a local model catalog. Supply a closed building way with its nodes, a supported business name or brand and shop or amenity tag, and a mapped entrance or nearby parking geometry. Supported brands are listed in store_match.py and chain_profiles.py. A ground-floor tenant sharing one mapped exterior edge with a host can receive a facade overlay. A business point inside a retail footprint can also receive a small panel when one exterior wall is distinctly closest and mapped parking faces it. For standalone stores without mapped entrances, the generator checks the addressed street before using nearby parking to choose the front.

```sh
python tools/storefronts/build_stores.py district.osm --output pack
```

Inspect the pack or use the game's threedmr_cache directory as the output to install directly. Existing catalog entries for other models are retained. Use a stable, distinct filename for each extract and run one writer per output directory. Rebuilding an extract replaces its generated catalog entries. [Local model placement and cache location](../../local_models/README.md)

## Generate stores as you drive

Set `BNE_STOREFRONTS=1` before starting the server to enable storefront discovery on near road tiles. The feature branch includes the generator, sign data, and fonts in the source ZIP and frozen player builds. The offline photo and index-building tools run from a source checkout. There is no city list or extract to prepare. A supported chain name on a low standalone commercial footprint can identify the store directly. For an unnamed commercial footprint, including one mapped only as retail or commercial, the server checks the places layer for a single business inside with confidence of at least 0.8. On a shared retail building, including a named plaza, a supported business can receive a ground-floor panel and entrance canopy when one exterior wall is distinctly closest, a mapped parking aisle faces it, and other mapped tenants are clear of the panel. The shared host stays in place. Fuel-category places are excluded from supermarket matching. For standalone retail buildings, multiple parking aisles facing one broad wall can identify the customer approach. Otherwise a nearby road supplies the approximate direction. Large stores with paired entrances can carry a name sign above each entrance. Detailed standalone models can carry a second wall sign on the public approach while keeping the main sign over a mapped entrance. A raised two-sided sign on wider approach walls makes the name legible along the street. Tenant panels use a separate name band above the glazing. In an OSM extract, a named or branded advertising totem can also generate a freestanding two-sided sign at its mapped position, using its height and orientation when available. Installed custom models take priority.

Generated meshes are reused from `threedmr_cache/storefronts`. Data coverage varies by location. The streaming path uses mapped wall material. The offline workflow below also accepts photo observations.

## Use a wall photo

Choose a roughly front-on crop that contains several courses of visible brick. Crop coordinates are pixels from the upper left after applying the photo orientation. Camera coordinates are longitude then latitude. Supply the true-north bearing from the camera **to the crop centre**. An off-centre crop can have a different bearing from the centre of the original image.

```sh
python tools/storefronts/detect_wall_material.py wall.jpg --crop 40 80 320 280 --osm district.osm --camera -86.75 36.15 --bearing 90 --source local-photo-1 --output wall.json
python tools/storefronts/build_stores.py district.osm --wall-observation wall.json --output pack
```

The coordinates above illustrate the argument format. Use the actual camera position and bearing for your photo. The matcher checks mapped closed building ways up to 80 metres away. It selects the first wall hit by the centre ray and rays five degrees to either side. An ambiguous direction produces an error. For a surveyed wall, supply `--way ID --wall LON1 LAT1 LON2 LAT2` in place of the three camera arguments. Pass a directory to `--wall-observation` to build from several records.

The material detector recognizes staggered brickwork or returns unknown. A mapped building material takes priority. Accepted brick observations affect one wall of a supported standalone store. McDonald's uses separate geometry. Photos are processed locally at build time. Street imagery retrieval, multipolygon visibility and automatic crop selection are outside the current tool.

### Freestanding roadside signs

With storefront streaming enabled, the server looks up branded OpenStreetMap sign nodes in a bundled geographic index. Each sign belongs to one road tile and can appear even when its restaurant is on the next tile. A mapped height controls the model (default 6 m). A numeric direction or a nearby road sets the orientation of both faces. An installed local sign at the same position takes priority.

The index contains recognized `advertising=totem` nodes from the global QLever OSM snapshot. Coverage depends on someone mapping the sign itself. It can be refreshed offline with `python tools/storefronts/index_totems.py server/data/storefront_totems.csv`. After publishing changed data, update `STOREFRONT_VERSION`. Index data © OpenStreetMap contributors, available under the [Open Database License](https://www.openstreetmap.org/copyright).

[See both approaches to the mapped Burger King on Mount Vernon Avenue in Marion, Ohio](marion-roadside-preview.jpg). The image shows the exported building and sign from driver height.

## Validation

```sh
python -m unittest discover -s tools/storefronts -p test_photo_match.py
python tools/storefronts/test_wall_observations.py
python -m unittest discover -s server/tests -p test_buildings.py -k CustomModelSuppressionTest
```

### Street-facing signs

On a branded standalone footprint the streaming server checks the OSM way ID for mapped ground-floor customer entrances. A point must still lie within 1.5 metres of the current wall. When no tagged door survives, a pedestrian way ending at a shared footprint node can supply an inferred entrance. The street bearing stays available for secondary signage when the entrance faces the parking lot. The offline XML importer reads the same types of evidence from the supplied extract. For a mapped tenant unit, a door on its straight shared exterior wall positions the frontage glazing and canopy. A business point alone keeps the existing inferred panel.

The bundled snapshot covers building ways whose OSM `brand` matches a catalog name. Refresh it with `python tools/storefronts/index_entrances.py server/data/storefront_entrances.csv`, then increment `STOREFRONT_VERSION`. Streaming uses this local index without querying OSM. Entrance data © OpenStreetMap contributors under the [Open Database License](https://www.openstreetmap.org/copyright).

Keep the canonical chain name for map matching and exported metadata. Add a display name to `brand_signs.LETTERING` when the storefront usually shows a shorter name or distinctive casing. Register a standalone symbol in `SYMBOL_ONLY`, a script wordmark in `SCRIPT_MARKS`, or a symbol with optional lettering in `SYMBOL_AND_NAME`. The exporter uses these profiles for standalone buildings, shared frontages, and mapped roadside signs. Check the mark at square and wide aspect ratios and on a driver-height render before adding it to a brand profile. Individual locations may use other sign variants. After a change that alters streamed models, bump `threedmr.STOREFRONT_VERSION` so both the model and tile caches pick it up.

For a brand that uses both a symbol and a name, `symbol_and_name` chooses a horizontal or stacked layout using the available width and height. The estimated physical letter height determines whether the full mark fits. The current roadside and facade thresholds are tuning values. Projecting signs for these brands use the standalone symbol. Apple and Freddy's keep their respective symbol and script treatments.
With storefronts enabled, set `BNE_LOCAL_BUSINESS_SIGNS=1` before launching the server to label independent businesses. A named retail footprint or a confidently mapped business inside a commercial building supplies the text. Tobacco and vape shops, car washes, fuel stations, markets, and other supported retail categories use category colors. Wall signs use four triangles and one cached texture. A named low retail building can carry a twelve-triangle raised panel aligned with its entrance bay. Shared tenants use the same parking and separation checks as chain panels. Fuel signs attach to a shop wall. The additional sign layer defaults to off.
For a raised local-business sign, nearby place records with the same numbered street address and distinct labeled suites can also produce a small directory beside the entrance. The directory lists mapped tenants without assigning extra doors. Conflicting suite listings are skipped. Older cached place cells without addresses continue to render the usual sign.
