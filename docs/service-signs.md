# Interstate exit service signs: completion report

Implemented November 8, 2026. The user subsequently confirmed the Wyoming sample works in-game. A [national coverage pipeline](service-signs-us.md) is now configured and building; nationwide coverage remains incomplete and unverified. No running server was restarted and no active cache was cleared.

## 1–2. Reused and new assets

Reuses `exitsigns.Mesh`, rounded sign contours, metal posts, terrain sampling, `bne_exit_blue`, white/metal materials, and the existing licensed Roadgeek/guide glyph atlases. No existing font or sign assets were changed. New assets are procedural general-service/business-panel geometry, a policy file and an inferred Wyoming association index. No third-party logo artwork was added.

Variable content uses shared cached atlas lettering, following the existing exit renderer, rather than new per-business raster textures. It therefore needs no per-name materials or per-frame texture generation. `probes/service-signs/preview.png` is an offline rasterization of the actual mesh triangles, colours and glyph UVs, not an in-game screenshot.

## 3. Supported OSM tags

| Category | Tags |
| --- | --- |
| FOOD | `amenity=restaurant`, `fast_food`, `cafe` |
| GAS | `amenity=fuel` |
| LODGING | `tourism=hotel`, `motel`, `guest_house` |
| CAMPING | `tourism=camp_site`, `caravan_site` |
| HOSPITAL | `amenity=hospital` |

Closed ways and multipolygon business areas are supported, including polygon holes. Exit detection reuses `exitsigns.index_records`: motorway/link topology, junction `ref`/`junction:ref`, ramp `junction:ref`, destination/destination:ref tags and the existing directional destination fallbacks. Mainline `ref`, travel geometry and one-way direction are retained. Unnumbered exits are omitted rather than given invented numbers.

## 4. Business association

The offline builder joins roads by shared OSM node IDs. Each directed outgoing ramp receives an independent Dijkstra search, including ramp distance. Motorways/trunks cannot be traversed after taking the ramp; reaching another exit by freeway does not qualify. Category limits bound the search. Reverse one-way roads, roundabouts, motorcar/motor_vehicle/vehicle/access precedence, private/no access, conditional tags, barriers and restriction relations affect eligibility.

A business must be on a ground-level local-road node or contain a local-road vertex in its mapped area. There is no nearest-road or straight-line association. Unknown gates are excluded unless permitted motor access is explicitly mapped. Restrictions conservatively block via nodes/via-way nodes, including `only_*` restrictions, so valid routes can be omitted. Military areas are excluded; an incomplete military boundary uses its enclosing box conservatively, potentially omitting adjacent public services.

Evidence includes actual OSM name/brand, OSM ID/URL, routed metres, access node and route way IDs. Missing road or POI connections cause omissions. Incorrect/missing OSM restrictions or category tags remain a limitation.

## 5–6. General and specific signs

General blue boards show an exit number and only categories having qualified businesses. Specific boards show a category, exit number and actual business names. They support up to six panels in two rows and two or three columns, measured/wrapped text, duplicate-name suppression and deterministic selection. Names that cannot fit in three lines at a minimum 0.20 m cap height are omitted from specific panels; their qualifying general category can remain. Names are neither invented nor truncated into a different business identity.

No reusable, licensed business-logo collection was established. Text panels are the implemented fallback. WYDOT's public program page does not supply a complete sign/business inventory or a reusable logo licence.

## 7. Inferred versus verified

All bundled signs and associations have `provenance=inferred`. They do not establish paid logo-program participation or reproduce verified physical signs. Provenance remains in the index and placement metadata.

Optional `--verified evidence.json` accepts an audited JSON list with `ramp_id`, `business_id`, HTTPS `source`, `checked_by`, and `checked_at`. It can verify participation only for an already reachable business; it cannot add an unreachable business. Verified associations receive selection priority and retain evidence. Physical sign position/layout remain inferred, and verification never grants logo rights. No participation evidence is bundled.

## Policy and placement

`server/data/service_sign_policy.json` supplies global defaults/state overrides for eligible categories, network-distance limits, panel/board counts, dimensions, advance distance, spacing, shoulder clearance and existing-sign separation. Wyoming dimensions reference WYDOT's published 17 by 10 foot six-panel board. Initial 3/6 mile search limits and 1,350 m advance/200 m spacing are scenery heuristics, **not a transcription of Wyoming eligibility regulations or certification of MUTCD compliance**. Wyoming-specific behavior is not embedded in the global generator.

Stations project onto matching generated motorway ribbons and traffic direction. Boards use the right shoulder and face approaching vehicles. Full panel radii are checked against pavement, buildings, bridge decks, mapped barrier lines and existing guide signs. Each post samples terrain; non-finite/steep sites are omitted. Source stations are chosen before half-open tile ownership; repeated source IDs collapse. Existing guide signs take priority, and failed service placements are omitted. There is no comprehensive collision index for every unrelated scenery prop; unrepresented props require in-game review.

Design references:

- [FHWA current MUTCD, 11th Edition with Revision 1, December 2025](https://mutcd.fhwa.dot.gov/kno_11th_Editionr1.htm), Parts 2I/2J.
- [WYDOT Specific Service Signs](https://www.dot.state.wy.us/home/business_with_wydot/specific-service-signs-logo-signs.html).
- [OpenStreetMap copyright and ODbL attribution](https://www.openstreetmap.org/copyright).

## 8. Wyoming locations tested

The isolated OSM sample covers buffered Cheyenne and Laramie areas, fetched November 8, 2026, with a military/barrier supplement. Its 34,458 elements yield 24 explicitly connected candidate businesses and **17 directed exits with associations**, spanning all five categories. Not every candidate business reaches an eligible exit. These are index counts, not counts of physical signs rendered.

Offline associations were examined around Cheyenne I-80 exits 358/364/367, I-25 exits, and Laramie I-80 exits 310/311/313/316. The I-80/I-25 freeway connectors 359A/359C and 8B/8D receive no service association through another motorway. Northern I-25 candidates 16/17 also have no qualifying association in this buffered input. Those negative results do not prove that no real services exist. Rural exits outside these boxes were not surveyed. Placement is tested with isolated road fixtures; no in-game drive was performed.

## 9. Files changed

Added:

- `server/servicesigns.py`: source, templates, placement and shared-atlas meshes.
- `server/serviceobstacles.py`: lazy building-footprint clearance.
- `server/servicesigns_index.py`: directed routing, area/access rules, audited evidence support.
- `server/data/service_sign_policy.json` and `server/data/service_signs.json`.
- `server/tests/test_service_signs.py`: 14 isolated tests.
- `tools/build_service_signs.py`, `tools/fetch_service_sign_sample.py`, `tools/preview_service_signs.py`.
- `probes/service-signs/wyoming-services.osm.json.gz`, `wyoming-index.json`, `preview.png`: locally generated source, rebuild and preview artifacts, reproduced by the tools rather than committed.
- `docs/service-signs.md`: this report.

Updated `server/highwaysigns.py` for source/version/mesh integration and `server/earthd.py` for local sign revision refresh. Roads, bridges, terrain and other prop generators were not modified. Existing guide layout calls are retained; optional service rendering failures retain existing signs.

## 10. Tests

- All **14 new service tests pass**, covering routing direction, disconnected/private access, distance limits, restrictions/gates, military exclusion, multipolygon holes, evidence bounds, deterministic output, panel limits, source revisions, ownership/orientation, barrier/building exclusions, actual mesh export and pipeline failure isolation.
- Existing upstream highway-sign regression file: **13 tests pass**, including worker/fallback streaming.
- Broader focused-PR sign suite: **176 run, 175 pass, 1 fails**. The untouched font-atlas reproducibility check produces metrics differing from the committed atlas under the local Pillow/FreeType environment; the same check also fails on the unchanged upstream snapshot. No existing font assets or generation code were modified. Use the repository venv for this suite.
- An isolated rebuild is byte-identical to the bundled JSON.
- Changed Python files pass `py_compile`. Actual-mesh offline previews were inspected.

## 11. Performance

Service geometry merges into the existing `exits` mesh and `roads_tile` stream, inheriting visibility, distance rendering, bounded workers, retirement, rebase and unloading. No Lua/protocol extension, separate stream, road rebuild or physics change is needed. Graph searches occur only in the offline builder.

Runtime loads a compact index with geographic buckets, reuses cached road contexts and shared font textures, and includes index/policy hashes in sign refreshes. Pose-side local-file checks are throttled to five seconds. On this workstation, the final 132,826-byte/17-record sample performed 1,000 warm local queries in about 79 ms, roughly 0.08 ms/query. A deterministic 34,458-element rebuild took approximately 2–3 seconds. These are isolated measurements, not in-game FPS results. Nationwide deployment should partition regional indexes and use buffered regional inputs instead of loading a planet graph into this sample builder.

## 12. Refresh instructions and limitations

From the repository root:

```powershell
python tools/fetch_service_sign_sample.py
python tools/build_service_signs.py probes/service-signs/wyoming-services.osm.json.gz server/data/service_signs.json
python tools/preview_service_signs.py server/data/service_signs.json probes/service-signs/preview.png
.venv/Scripts/python.exe -m unittest discover -s server/tests -p test_service_signs.py
```

The fetch writes only the isolated probe file. The builder atomically replaces its explicitly named output. Preserve OSM attribution/ODbL obligations when redistributing derived data.

The running server was not restarted or modified in memory; code takes effect on the next normal launch of this updated source. Once loaded, replacing the index or editing policy is detected within about five seconds and ordinary sign jobs refresh visible tiles. No destructive cache purge is needed. Smaller runtime distance limits filter existing associations; larger limits or access-rule changes require rebuilding the offline index to recover additional businesses.

Coverage is a Cheyenne/Laramie sample, not statewide/nationwide. Conservative explicit-access matching omits many standalone POIs, especially food; incorrect OSM tags can still misclassify facilities. Conservative base/restriction handling omits some valid routes. No verified program-participation dataset or licensed logo collection is supplied. Distant readability, terrain contact, unrepresented props and actual streaming behavior still require user verification in BeamNG.
