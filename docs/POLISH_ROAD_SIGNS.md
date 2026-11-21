# Polish road signs

Enable **Experimental → Road signs** in BeamNG. To add surveyed signs, paste your own client access token into **Mapillary access token** and choose **Save token**. Choose **Disable Mapillary** to clear the profile token and use generated positions. Restart the world server after changing the mode or token.

The masked field saves to the local game profile. The launcher command `BeamNG.Earth.exe --configure-mapillary` can also set the profile token. A profile choice takes precedence over `MAPILLARY_ACCESS_TOKEN` and tokens saved by older launchers. Live requests use Mapillary's authorization header.

## Driving and coverage

As road tiles load, the server queries intersecting Mapillary cells in Poland, caches them by location and refreshes them by calendar month. A return visit reuses those results. If the service is offline or rate-limited, previously saved observations remain available. Network latency can increase the generation time on a first visit. Only confirmed classifications with an installed Polish design are rendered. Limit values must match the original artwork exactly.

With a token, the bundled snapshot adds 14,611 OSM nodes, 50 Wrocław inventory records and 145 Mapillary observations with identified Polish designs. Try **53.7281, 17.4980** near Charzykowy. Source coverage and survey dates vary. The Geoportal PRG boundary is checked again after moving a pole to the shoulder. Direction is inferred from the road when no absolute bearing is available. A Mapillary detection without a road nearby uses its surveyed position with a north-facing plate.

## Default signs

**World → Town signs** adds green E-17a entry and E-18a exit boards with the settlement name. Both sides share one two-post body. The exit face crosses out the name. The server reuses its settlement names, road network and land cover to estimate entrances. The location can differ from the physical sign. This layer works without a Mapillary token. It checks the Geoportal boundary at the roadside position and caches a 256 × 64 face for each distinct inscription and direction. D-42 and D-43 built-up area signs are separate road signs.

The Road signs switch controls both surveyed and generated signs. Each generated sign is checked on its own approach, so partial Mapillary coverage fills only matching slots. An empty response, an unavailable service or a missing token leaves the road rules active. In Poland, supported generated designs use the same artwork as surveyed signs. At a Polish roundabout entrance, one model carries A-7 above C-12 on a shared steel post. The server matches both survey classes by approach and emits the assembly once, including across tile edges. A surveyed STOP takes precedence over inferred yield. A-8 remains an advance warning upstream. To suppress a generated duplicate, the surveyed sign must be on the same approach (up to 12 metres apart, within 2.5 metres laterally and 30 degrees of heading). A surveyed STOP or give-way takes precedence over a generated priority instruction, and a surveyed speed limit takes precedence over a generated limit on that approach. Other designs retain their existing placement. This changes sign graphics only. The driving graph, traffic lights and road markings are handled by their original generators.

## Models

The executable includes 53 three-dimensional models, with a steel pole, metal rim, back and an individual 256 × 256 RGBA texture. The original Wikimedia Commons SVG is scaled uniformly. Its visible outline determines the shape of the plate. The fronts of Polish signs, including locality boards, use the material's retroreflectivity at 0.35 for a mild headlight response. Credits, file pages and source hashes are in `tools/sign_art/pl/sources.json`. Rebuild these assets with `python tools/build_pl_signs.py` after installing numpy, Pillow and resvg-py. Rerunning model preparation refreshes the materials of previously prepared extras.

Run `BeamNG.Earth.exe --prepare-pl-models` before starting the game to expand the library. Its manifest lists 364 original designs including the bundled ones. It verifies each source against its Commons checksum and licence, generates its mesh and texture once, and records completed models in a separate unpacked mod. Restart the game and world server to pick them up. This downloads artwork only. It can resume after a download limit. To use originals already downloaded, pass `--originals DIRECTORY`. The mapping from Mapillary classes to exact Polish variants is in `server/data/pl_signs/mapillary-classes.json`.

## Updating positions

Run the importer on a source checkout. Install Shapely 2 or later. Reading a PBF also needs the Python package `osmium`. Input geometry uses WGS84 longitude and latitude. Download the [Geoportal national boundary as GeoJSON](https://ogcapi.geoportal.gov.pl/res/ims/maps/PRG/apiFeatures/collections/GranicaPanstwa/items?f=json&limit=1) to `poland-boundary.geojson`.

```sh
python tools/import_pl_signs.py --boundary poland-boundary.geojson --build-boundary --osm poland.osm.pbf
python tools/import_pl_signs.py --boundary poland-boundary.geojson --mapillary area1.geojson area2.geojson
python tools/import_pl_signs.py --boundary poland-boundary.geojson --wroclaw B-5.geojson B-16.geojson B-18.geojson
```

Each command atomically replaces the named source layer. Include all exports you want to keep for that source. The other layers keep their records. The server reads the files at startup and includes their content in the road tile cache keys when enabled. See the [source notice](../server/data/pl_signs/NOTICE.md) for the exact inputs and their terms.

For Mapillary Graph API data, request a small bounding box in west, south, east, north order. The tool prompts for the client access token without displaying it. It also accepts the local environment variable `MAPILLARY_ACCESS_TOKEN`. It follows result pages and extracts matching traffic sign features. A token is only needed when downloading more data.

```sh
python tools/import_pl_signs.py --boundary poland-boundary.geojson --bbox 17.488 53.724 17.506 53.733
```

To check the integration, run the following commands from the project root.

```sh
python -m unittest discover -s server/tests -p test_plsigns.py
python -m unittest discover -s server/tests -p test_experimental.py
python tools/run_lua_tests.py test_settings test_trees
```
