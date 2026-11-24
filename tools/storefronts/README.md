# Fictional storefronts

The storefront generator fits fictional businesses to mapped locations. A chain has a consistent name, mark and sign colors across standalone buildings, tenants, banners, roadside signs and directories. Its entrances can vary by site. The bundled catalog maps 100 source names to 100 fictional identities and 23 shared building styles. The artwork is generated locally from lettering, simple marks, materials and geometry.

[Browse the storefront catalog with all 100 map references, fictional names and building renders](catalog/buildings.md).

[How to contribute names, regional chains and reusable entrance variants](catalog/README.md#how-to-grow-the-catalog).

## Offline generation

Run these offline tools from the repository root using the server Python environment. The generator reads OpenStreetMap XML extracts and writes GLB models plus a local model catalog. Supply a closed building way with its nodes, a supported business name or brand and shop or amenity tag, and a mapped entrance or nearby parking geometry. The catalog lists recognized chains and exact categories for other businesses. A named, explicitly typed standalone storefront can reuse a family with neutral signage. Verified exterior units can reuse the family within their own frontage. See the [category table and example exports](catalog/buildings.md#businesses-outside-the-chain-catalog). A ground-floor tenant sharing one mapped exterior edge with a host can receive a facade overlay. A business point inside a retail footprint can also receive a small panel when one exterior wall is distinctly closest and mapped parking faces it. For standalone stores without mapped entrances, the generator checks the addressed street before using nearby parking to choose the front.

```sh
python tools/storefronts/build_stores.py district.osm --output pack
```

Inspect the pack or use the game's threedmr_cache directory as the output to install directly. Existing catalog entries for other models are retained. Use a stable, distinct filename for each extract and run one writer per output directory. Rebuilding an extract replaces its generated catalog entries. [Local model placement and cache location](../../local_models/README.md)

## Generate stores as you drive

In BeamNG.Earth Settings under Experimental, enable **Storefronts and business signs** and restart the world server. On a standalone server, set `BNE_STOREFRONTS=1` for catalog businesses and `BNE_LOCAL_BUSINESS_SIGNS=1` for independent businesses and mall signs. A named footprint, a matched exterior frontage or a bank at a mapped customer door can display the mapped business name in neutral lettering. A category model needs evidence that the named business owns the footprint. A tenant pin alone can support an eligible sign. Qualified mall and suite directories can list those names. The Café fallback requires a mapped café or coffee shop category. A business name containing “Cafe” alone supplies no category evidence. A tenant inferred from a shared row gets a category label such as Bookshop. ATM markers and mall entrance headings keep their generic labels. Recognized catalog chains use their fictional identities throughout.

To use category labels for all uncataloged businesses, set `BNE_MAPPED_BUSINESS_NAMES=0` before starting the world server and reload the area. This also applies to directory rows. The naming mode is part of the streamed model and tile cache keys.

Mapped doors, parking aisles and street approaches guide placement. Market and warehouse styles can use paired entrances on sufficiently wide walls. A tenant uses its own frontage on a shared host. Directories preserve suite numbers where supplied. Generated meshes are cached in `threedmr_cache/storefronts`. On restart, catalog edits invalidate generated models and tile revisits. Offline packs from the earlier real-brand generator can be rebuilt with the same command and source extract.

## Test in BeamNG.drive

1. Start the dev build on this branch. In BeamNG.Earth Settings, open **Experimental**, enable **Storefronts and business signs**, then select **Restart world server**. The setting starts off.
2. Drive past a mapped standalone store. Around Nashville or Clarksville, look for Grover at a mapped Kroger or Widecart at a Walmart. Check that the name is legible from the approach and the entrance faces a plausible customer route.
3. Try a shopping commons and an enclosed mall such as Opry Mills. Look for signs along customer-facing walls at the commons and compare exterior tenants with the entrance directory at the mall. A tenant mapped deep inside an enclosed mall is omitted from exterior signs and the entrance directory.
4. At a bank, check the facade and any mapped exterior ATM or drive-through lane. Leave the tile and return to check sign placement and identity on the revisit.

When reporting a mismatch, include coordinates, approach direction, a screenshot, and the sign or entrance you expected to see. Mapped door, lane and tenant evidence varies by site. The separate parking scenery experiment uses its own server flag.

## Use a wall photo

Choose a roughly front-on crop that contains several courses of visible brick. Crop coordinates are pixels from the upper left after applying the photo orientation. Camera coordinates are longitude then latitude. Supply the true-north bearing from the camera **to the crop centre**. An off-centre crop can have a different bearing from the centre of the original image.

```sh
python tools/storefronts/detect_wall_material.py wall.jpg --crop 40 80 320 280 --osm district.osm --camera -86.75 36.15 --bearing 90 --source local-photo-1 --output wall.json
python tools/storefronts/build_stores.py district.osm --wall-observation wall.json --output pack
```

The coordinates above illustrate the argument format. Use the actual camera position and bearing for your photo. The matcher checks mapped closed building ways up to 80 metres away. It selects the first wall hit by the centre ray and rays five degrees to either side. An ambiguous direction produces an error. For a surveyed wall, supply `--way ID --wall LON1 LAT1 LON2 LAT2` in place of the three camera arguments. Pass a directory to `--wall-observation` to build from several records.

The material detector recognizes staggered brickwork or returns unknown. A mapped building material takes priority. Accepted brick observations affect one wall of a supported standalone store. Photos are processed locally at build time. Street imagery retrieval, multipolygon visibility and automatic crop selection are outside the current tool.

### Freestanding roadside signs

With storefront streaming enabled, the server looks up branded OpenStreetMap sign nodes in a bundled geographic index. Each sign belongs to one road tile and can appear even when its restaurant is on the next tile. A mapped height controls the model (default 6 m). A numeric direction or a nearby road sets the orientation of both faces. An installed local sign at the same position takes priority.

The index contains recognized `advertising=totem` nodes from the global QLever OSM snapshot. Coverage depends on someone mapping the sign itself. It can be refreshed offline with `python tools/storefronts/index_totems.py server/data/storefront_totems.csv`. After publishing changed data, update `STOREFRONT_VERSION`. Index data © OpenStreetMap contributors, available under the [Open Database License](https://www.openstreetmap.org/copyright).

## Validation

```sh
python -m unittest discover -s tools/storefronts -p test_photo_match.py
python tools/storefronts/test_wall_observations.py
python -m unittest discover -s server/tests -p test_buildings.py -k CustomModelSuppressionTest
```

### Street-facing signs

On a branded standalone footprint the streaming server checks the OSM way ID for mapped ground-floor customer entrances. A point must still lie within 1.5 metres of the current wall. When no tagged door survives, a pedestrian way ending at a shared footprint node can supply an inferred entrance. The street bearing stays available for secondary signage when the entrance faces the parking lot. The offline XML importer reads the same types of evidence from the supplied extract. For a mapped tenant unit, a door on its straight shared exterior wall positions the frontage glazing and canopy. A business point alone keeps the existing inferred panel.

The bundled snapshot covers building ways whose OSM `brand` matches a catalog name. Refresh it with `python tools/storefronts/index_entrances.py server/data/storefront_entrances.csv`, then increment `STOREFRONT_VERSION`. Streaming uses this local index without querying OSM. Entrance data © OpenStreetMap contributors under the [Open Database License](https://www.openstreetmap.org/copyright).

Mall exterior signage can also use a qualified tenant pin within two metres of an explicitly mapped customer door. For operator-documented exterior access, `server/data/mall_exterior_access.csv` pairs an OSM building way with the tenant listing name and a source URL. Include only a tenant-specific exterior entrance. A recommended shared mall entry is insufficient. Each record still needs a current qualifying place pin on a distinguishable wall.

With local business signs enabled, shopping commons can place tenant signs along several walls of a shared building. Each row faces a mapped customer walkway, parking aisle, or entrance. Signs follow the order of the tenant pins and share the available width. Open shopping centers can also fill visible bays with banners, budgeting one inferred retail identity per 38 m of visible frontage. Precisely mapped tenants can justify additional bays.

At an enclosed mall, a banner needs a high-confidence pin within eight metres of a single visible bay. The entrance directory omits pins more than 15 metres inside the outer wall, beyond the depth used to match a tenant frontage. A pin near that wall can qualify for the directory even when its exact door is unknown. Multipart malls attach signs to the exterior walls that the game actually draws.

Banks can have a facade sign and a separate drive-through canopy when a mapped one-way drive-through passes beside the branch. A bank inside another building can receive a sign at a mapped exterior door. An ATM pin beside a mapped exterior door receives a small ATM marker.

Set `BNE_PARKING_DISPLAYS=1` before starting the server to enable parked scenery. Mapped dealer lots use four low-poly vehicle shapes. Retail, office, and restaurant lots vary their visible occupancy with the in-game clock. The generator reserves room for live parking and caps display cars at 36 per lot and 64 per tile.


## Drop-in storefront packs

With Storefronts enabled in Experimental settings, copy a folder named `storefront-pack` into
`<BeamNG user folder>/settings/beamngEarth/`. It contains one file, `pack.zip`. Keep that file zipped.
Replace it to switch packs, or move the folder out to restore the built-in catalog. The server checks
while connected and queues visible road tiles for regeneration. The new buildings appear as those tiles arrive.
A complete replacement can be staged next to the active file and renamed into place.

The archive contains a UTF-8 `pack.json`, optional PNG signs and self-contained GLB models.
Use flat lowercase filenames. An example manifest for a single business is shown below.

```json
{
  "version": 1,
  "chains": {
    "Example Market": {
      "label": "Maple Market",
      "style": "shopfront",
      "paint": "#365041",
      "ink": "#ffffff",
      "aliases": ["Example Market Express"],
      "sign": "maple.png",
      "model": "maple.glb",
      "width": 30,
      "depth": 20
    }
  },
  "sites": []
}
```

A template uses glTF metres, with a centered footprint, Y up and its entrance facing +Z (south).
Width and depth describe the base footprint. Standalone sites with a rectangular footprint can fit
the full model to the chosen frontage. Irregular footprints, mapped drive-throughs and tenant
frontages use the procedural generator with the pack's identity and sign. An omitted chain keeps
the built-in behavior. An optional `sites` entry places an exact GLB using `file`, `lat`, `lon`,
and `title`. Its optional `replaces` array lists local catalog filenames superseded while active.
