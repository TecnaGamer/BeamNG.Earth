# OSM outdoor lighting

Both placement switches are off by default. In Experimental, enable **OSM Pedestrian and Recreational Lighting** to light explicitly tagged ground paths and public areas. Enable **Strict OSM Lighting** to require direct lighting evidence for ordinary street lamps and far street halos. Enable either or both, then use **Restart world server**. Individually mapped lamp nodes remain eligible with either switch off. The separate standalone-paths setting controls the rendered path surface.

**Street Lamp Brightness (%)** and **Street Lamp Glow (%)** are independent live quality controls. At 100%, a stock street lamp uses its original intensity. New path and area fixtures multiply the stock intensity by the profile below. Changing glow scales the street lens, lantern glass and halo materials, including distant road halos. Coverage distance, beam angles, the spot-light budget, vehicle headlights and exposure retain their existing settings.

| Feature | Spacing | Post height | Stock intensity |
|---|---:|---:|---:|
| Pedestrian street, footway, promenade | 20 m | 4 m | 15% |
| Independent path, park or recreation ground perimeter | 20 m | 3.5 m | 10% |
| Cycleway | 25 m | 5 m | 20% |
| Garden perimeter | 18 m | 4 m | 15% |
| Square perimeter | 25 m | 5.5 m | 20% |
| Surface parking perimeter | 32 m | 8 m | 30% |

Urban roads retain the regional spacing and mounting profiles. Use the brightness control to compare an ordinary street at 25%, 40%, 50%, 75% and 100%. The path generator uses the mapped highway class. A promenade mapped as a cycleway takes the cycleway profile.

## Development checkpoints

| Checkpoint | Change and verification | Limitation and where to resume |
|---|---|---|
| CP1 Â· Map data | Verified highway-tag joins and both Overture polygon layers. Road cell format 10 in enhanced modes carries the direct source way's lighting separately from name-matched road attributes. The cached Chojnice-area sample contained 33 garden, one park and 39 parking records with no lighting tag. A pier and a bus stop retained their explicit lighting tags. | These are cached records, including possible overlapping cell copies. They do not establish whether a physical site is lit. Review newer source tags if a site appears missing. |
| CP2 Â· Paths | Stations follow PR #1362 ground geometry, excluding bridges, tunnels and roadside sidewalk duplicates. Width and local tangent set the offset and aim. Curved four-metre routes, explicit yes, no, missing and ambiguous values have regression tests. | Separate tags for times of operation have no schedule interpretation. Inspect the direct source way and accepted path pieces before adjusting spacing. |
| CP3 Â· Areas | Both builders consume cached source tags for land use and infrastructure. Tests exercise lit gardens, parks, squares and parking polygons, negative areas and inward-facing fixtures. Rings with holes exclude points in the holes. Artificial clipping edges are skipped. | Area lighting follows outer edges. Large interiors need mapped lamps or explicitly lit paths. Concave, very dense and clipped real polygons need field review. |
| CP4 Â· Position and coverage | Half-open tile ownership, nearby mapped points, road stations and deterministic candidate priorities limit duplicates. Tests compare split and whole tiles and check road coverage, footprints, orientation and mapped-lamp precedence. Road and deck clearance uses the existing collider index. Vegetation generation reserves potential post footprints. | A conservative rejection can leave a gap. Vegetation can reserve a site that a later building or coverage check rejects. Coverage estimates are based on distance rather than a shadow simulation. Inspect the candidate, covering fixture and obstacles together. |
| CP5 Â· Runtime | The Lua night-light suite exercised live intensity at 0, 25, 40, 50, 75 and 100%, pool reuse, range and lens versus window emissions. All 96 assertions passed. Settings passed 547 assertions. | Lua engine stubs verify numeric behaviour. A real night drive is still needed for shadow visibility, HDR and bloom. |
| CP6 Â· Configuration and caches | The two generation switches reach both builders through the restart settings. Build keys include the environment switches. Direct-evidence road cells and vegetation cells have distinct cache names. Brightness and glow are player-local. Tests cover independent switches and default settings. | Placement needs a server restart. With the placement switches off, the worker now forwards the mapped lamps it was already collecting, matching the in-process builder. This corrects missing mapped poles in that worker. |
| CP7 Â· Compatibility and performance | Integrated PR #1362 head 0e13b03 with development head 4915490. The final focused Python run passed 185 tests. On a synthetic flat tile with a lit cycleway and unlit residential road, both builders produced zero lenses off and eight on. Worker median over 20 warm builds was 6.94 ms off and 7.63 ms on. | These timings cover tile generation on a small fixture. In-game FPS, cold-cache city generation and the requested visual comparison are pending. The game and workspace volumes had insufficient headroom for an isolated install and capture. |
| CP8 Â· Review | The integration of PR #1362 is a separate commit below the lighting changes. Run the commands below and compare the feature commit with that integration commit when reviewing. | Keep the PR in draft until a real-game run records the location, source tags, game build, graphics settings, time of day, frame-time distribution and screenshots. Recheck the base when #1362 lands. |

## Reproduction

With the server dependencies and native module installed, run from the repository root:

~~~sh
python -m unittest discover -s server/tests -p test_pedestrianlighting.py
python -m unittest discover -s server/tests -p test_standalone_paths.py
luajit mod/tests/test_nightlights.lua
luajit mod/tests/test_settings.lua
~~~

For the visual pass, choose a surveyed lit feature and a nearby unlit control in Charzykowy, Warsaw, GdaĹ„sk, Amsterdam, Paris or Vienna. Record the source IDs and tags first. Keep the camera, route, time, weather, exposure settings and light budget fixed. Repeat the same drive with both generation switches off, pedestrian lighting on, and strict mode on. Compare the five intensity levels at fixed glow, then vary glow at fixed intensity. Record median FPS and 95th-percentile frame time after the same warm-up. Include a tile crossing and one mapped lamp beside an otherwise unlit road.

## Follow-up validation on dev b0be90fd

The generators now respect lighting ranges on merged OSM ways. Waterfront paths use the dry lakeside verge when the water geometry supports it. On Szkolna in Charzykowy, the cached map contained 14 roadside lamps. The road-priority rule aimed all 14 at the carriageway. It reuses the existing fixtures.

Graphics includes live beam width (50–150%, default 100%) and an optional half-density mode. Half density immediately halves the street spotlight budget. Restart the world server to generate every other procedural station. Individually mapped poles keep their positions.

The runtime keeps a bounding box per loaded lamp tile and refreshes it after an origin rebase. A moving-camera LuaJIT benchmark used 625 tiles of 24 lenses, a 900 m radius and a 128-light limit. The median of nine 200-query batches went from 0.230 to 0.105 ms per query. The coordinate checksums matched. These timings measure CPU selection only. In-game frame times remain to be measured.
