# Road infrastructure migration — 1.6.3

Branch: `port/road-infrastructure-1.6.3`  
Base: `ed2fd3f` on `port/highway-signs-1.6.3`  
Date: 2026-10-26

Ports the road improvements from the separate modified 1.6.2 repository onto 1.6.3. The existing highway-sign, tree-density and per-player autosave migrations remain in the branch's ancestry.

## Lanes, shoulders and merges

- Restore the 1.6.2 travel-lane widths: motorway 4.0 m, trunk 3.8 m, primary/secondary 3.5 m, tertiary 3.3 m, other mapped lanes 3.2 m. Restore its class defaults, parking allowances and 1.25 m rural edge strips.
- Restore asymmetric motorway shoulders: 1.5 m on the driver's left and 4.5 m on the right. Ramp shoulders are 0.875 m left and 2.625 m right. Reversed OSM geometry preserves the driving-side placement; the AI lane centre shifts with the shoulders.
- Widen ramp travel lanes from the modified 1.6.2 value of 4.5 m to **4.8 m**. One-lane ramps are at least 8.3 m paved; two-lane ramps are at least 13.1 m. Mapped motorway/ramp widths cannot squeeze out the minimum lanes and shoulders.
- Restore left-side zipper lane additions/drops. Transitions use the source road chain, taper over up to 240 m per changed lane, and retain the through lanes from the right edge. Changes shorten at intervening junctions or lane changes.
- Restore entrance/exit tapers and extend them by up to 30 m onto existing pavement. Extensions are bounded by pavement, height, direction and adjacent merges. A 1 m gap is reserved between neighboring merge footprints when the original layout permits it; already tighter layouts are not extended into each other.
- Restore the yellow/orange driver-left edge on one-way highways. A ramp's shared edge remains white until the ramp and mainline pavement separate.
- Preserve paint phase across tile cuts, source segments, bridges and tunnels. Ramp and zipper paint remains inside the available pavement.

## OSM lane data

- Prefer the OSM geometry way among Overture sources rather than assuming the first source is the correct way. Exclude colliding node/relation IDs from the way-tag query.
- Use strict integer lane counts, including odd totals. Pass resolved counts to ground roads, decks, tunnels, markings, grading and the AI graph.
- Preserve 1.6.3's same-name/bounding-box fallback for relation-derived roads. If the source way changed, conservatively match current geometry; infer across unbranched continuations only when neighboring counts agree.
- Default unmapped one-way motorway/trunk carriageways to **two lanes** in both paint and AI. Explicit lane counts take precedence. Motorway width defaults remain two 4 m travel lanes in town and country.
- Bump the road-layer cache version from 6 to 7, retaining 1.6.3's railway flags. Retry incomplete OSM enrichment after 60 seconds through memory, mirror and disk caches. Incomplete layouts cannot become durable road-facts/revisit results.

## Marking dimensions

Dimensions follow the project's `ROAD_IMPROVEMENT_NOTES.md`. The user confirmed that the **final Kentucky entry** supersedes its earlier duplicate. Values below are project settings in metres. Dashes are actual solid geometry, so texture repeat ratios cannot alter their length or gap.

| Profile | Travel dash length | Travel gap | Travel width | Ramp dash length | Ramp gap | Ramp width | Solid width |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Default | 3.0 | 8.0 | 0.35 | 1.2 | 2.8 | 0.35 | 0.35 |
| Michigan | 4.5 | 11.5 | 0.40 | 1.5 | 6.0 | 0.35 | 0.40 |
| Minnesota | 3.1 | 12.25 | 0.30 | None | — | — | 0.45 |
| Wisconsin | 5.15 | 10.25 | 0.31 | 1.25 | 2.9 | 0.30 | 0.32 |
| California | 4.6 | 10.5 | 0.35 | 2.3 | 4.9 | 0.25 | 0.25 |
| Kentucky | 3.5 | 8.9 | 0.60 | None | — | — | 0.60 |

State-specific solid widths also apply to generated stop lines. Unlisted regions use the default profile.

## Global two-lane rural road widening

- Apply a **4.25 m minimum per travel lane** to paved, two-way rural trunk, primary, secondary, tertiary, residential and unclassified roads worldwide. Keep **1.25 m shoulders on each side**, giving **11 m minimum pavement**. Larger mapped widths remain larger. These are project gameplay dimensions.
- Resolve missing counts to two on ordinary roads with a two-lane-sized width. Preserve three-or-more-lane counts, urban street widths, ramps, one-way carriageways, roundabouts, service roads, shared living streets and unpaved/cobbled roads. Single-lane/narrow unknown sections retain their treatment unless two-lane through-road continuations bound both ends, as described below.
- Apply the minimum after roundabout/divided-road recognition and after building-based width measurements. Check the surroundings of mapped-width ground roads separately because their existing `urban=False` flag means no assumed parking, not necessarily countryside. Two-way, two-lane bridge and tunnel profiles receive the same clearance.
- Share the resolved width/count through road meshes, native in-ground road painting, material masks, edge lines, grading, vegetation clearance and the AI graph. The widened minimum has 8.5 m of driving space and one lane in each direction, rather than extra lanes inferred from the added width.
- Read-only inspection of cached West Maumee Street geometry near **41.63514, -85.01852** found two mapped lanes with 9.5 m pavement: 3.5 m lanes plus shoulders. This now becomes 11 m pavement with 4.25 m lanes, about **21% wider travel lanes**.
- The packaged source fingerprint changes, so the existing cache fix regenerates derived road geometry automatically without deleting map downloads or saved positions.

## Bridge approaches and rounded intersections

- Bridge intervals within a road inherit the resolved ground cross section, including measured width and edge margins. This fixes a bridge narrowing between the white edge lines even when its total width was close to the approach width.
- Bridge-only features follow uniquely aligned, compatible approach roads. Connected bridge pieces share their approach width; differing endpoint widths transition smoothly over up to 60 m at each end. Deck render/collision geometry, parapets, girders, paint and AI clearance follow the same width profile, including across render-tile seams.
- Pass the actual urban/building-width context into the production bridge generators. Preserve the native 1.6.3 bridge height, underpass and structural kernels.
- Generate rounded pavement corners even where no sidewalks exist. Rural major-road corners start with a 12 m radius; smaller roads start with 6 m, reduced when bends, short arms or nearby roads require it. Roundabouts retain their separate constrained layout.
- Share corner pavement between the in-ground photo, tire-material masks, near road masks and optional road meshes. Curve edge lines into the corners, grade the additional pavement to the approach heights, and clear vegetation and generated props from it. Pavement-only corners do not add sidewalks or crosswalks.
- Offline inspection of the cached junction near **41.52718, -85.05454** confirms four rounded pavement returns in the generated preview (`build/bridge-corners-preview.png`). This is an offline geometry check, not an in-game driving result.

## Rural lane continuity

- Keep a narrow rural section at two lanes when uniquely aligned two-lane through-road continuations connect to **both ends**. This also handles several consecutive narrow segments, reversed source geometry, missing counts and bridge/tunnel intervals. The repaired profile keeps the 11 m pavement minimum, two travel lanes and the region's center/edge markings through paint, grading and AI.
- Require reciprocal matches within 0.75 m, compatible road class and connector IDs. Side roads, forks, dead ends, conflicting lane counts, urban streets, ramps, one-way roads and unpaved roads do not supply false two-ended evidence. Genuine isolated single-lane roads remain single-lane.
- Stop propagating a lone `lanes=1` tag through an open-ended chain of untagged two-way roads. Recheck old cached `connected` guesses at generation time; retain the original downloaded tags and counts for provenance. No raw-map cache deletion or re-download is required.
- Reproduced the reported inference problem from cached **Chandlers Road near 36.89178, -86.72737**: one explicit tag was copied backward through three untagged sections. Those inferred sections now use the two-lane rural layout. Explicit single-lane stretches are only overridden when both two-lane continuations are present in the source context.

## A Collins Road, Logan County, Kentucky

- Apply the requested correction to **A Collins Road near 36.93660, -86.80461**, including its full mapped length and future source splits. Match the road name within a small geographic envelope; similarly named roads elsewhere and neighboring roads are unchanged.
- Use gravel color, stock gravel stone textures, a dedicated terrain material with **GRAVEL** tire physics and gravel mesh material. Keep a draped gravel texture in near mesh mode and the gravel terrain detail in terrain mode. Remove pavement markings/sidewalks from the corrected road.
- Suppress that road's generated utility poles, pole-mounted lamps and wire spans at their source. Neighboring utilities remain eligible.
- Cached OSM way **16282294** already carries `surface=gravel`, while the existing generator had used the absent Overture surface rule and defaulted to pavement. The local override applies the requested appearance/material without changing surface precedence on other roads.

## Roundabouts

- Use at least **9.0 m per circulating lane**, plus a 0.5 m edge allowance on each side: **10 m paved for one lane, 19 m for two**. Honor mapped counts and larger explicit widths; default to one circulating lane when its count is missing.
- Read OSM `junction=roundabout`, including partial rings at cell boundaries. When tags are absent, recognize curved rings including short one-way links between curved arcs. Public Square in Angola was previously missed because several connecting arcs were nearly straight; it was drawn as 3.5 m lanes with erroneous divided-road medians.
- Widen directly connected entry/exit segments up to 150 m long to 6 m per lane plus edge allowances, respecting separation from the opposing carriageway. Public Square's eight approaches are approximately 6.6–7 m paved. Keep their widths consistent in paint, grading and AI; omit curbside parking on these approaches.
- Support island cleanup for recognized rings up to ten source segments, 450 m perimeter and 12,000 m² enclosed area. Remove inward sidewalk/gutter runs and conflicting curb returns; clip approach sidewalks using their full width. Preserve mapped outer sidewalks with tighter curve simplification so their inner edges stay off the circulating pavement.
- Remove generated props intruding into the circulating corridor, retain elevated objects and the central island, and omit curbside parking on the ring.
- Keep a continuous central curb even when a recognized roundabout encloses a monument. Splitter islands also clear neighboring circulating arcs. Do not merge short ring arcs into ordinary junction interiors or place crosswalks across circulation.
- Omit guessed sidewalk slabs on roundabouts and their short approaches; retain explicit mapped sidewalks. Reduce the central island band from 1.2 m to a **0.4 m curb**. Do not generate raised splitter-island walkways when none of their roads has sidewalks.
- Reject conflicting curb returns before applying their sidewalk cuts. Use tangent joins between actual outer sidewalk ends, checking the entire slab against road pavement and mapped driveways. Keep driveway openings accessible.
- Expand vegetation exclusion to the generated road width. Keep the native 1.6.3 corridor kernel and its existing minimum class clearance.
- Restrict geometric fallback to curved candidates and their short connecting pieces: a synthetic 760-road street grid took approximately 0.17 s to resolve specs with no false roundabouts, versus 13.8 s with unrestricted cycle search on this machine.

## 1.6.3 compatibility

Retains the native road/deck/grading implementation, railway source flags, bridge/underpass clearance, mapped sidewalk-side tags, adjacent-built-up sidewalk criteria, gas-station cuts and shared road-facts/revisit caches. The native extension and mod assets were not replaced by 1.6.2 copies.

## Rural sidewalks and packaged cache invalidation

- Require sustained nearby development before guessing sidewalks: sample uniformly along the road about every 25 m, require 65% built-up coverage on the centreline and at least one roadside, and at least 35% on the other side for primary, secondary, trunk and unclassified roads. This prevents clustered vertices at a farm entrance from making the entire rural road appear developed. Existing urban width/parking classification is unchanged; explicit sidewalk tags still win.
- Fix frozen EXEs sharing the same generated-world cache identity. The previous cache scanned loose `.py` files, but packaged modules live inside PYZ; with an unchanged native module, the network-fix and roundabout EXEs both produced `3d6d8ad23cb09140` despite different source fingerprints. This allowed old meshes, grading plans and road facts to hide the new geometry.
- Include the packaged `CODE_ID` through `peerwork.code_id()` in the revisit key, retaining native-file and environment hashing. Updated builds automatically regenerate derived world content. Raw downloaded map data and saved player positions are not reset or deleted.

## Validation

- **43 Python test modules: 686 tests, 683 passed, 3 skipped.** The skipped tests are gas-station checks requiring live data.
- Covers lane enrichment, markings and mesh dimensions, ramps, zipper transitions, roundabout clearance, road generation, AI, grading, bridges, tunnels, sidewalks, vegetation, parking, railways, gas stations, caching and the previously migrated signs.
- Final suite logs: `build/road-connections-final-tests/`.
- Five Lua test files passed, with 203 assertions: surface layers (including the new gravel layer), terrain swaps/patches, road loading and near-tile materials. Log: `build/road-connections-lua-tests.log`.
- Rural-road regressions cover northern/southern hemispheres and left/right-hand traffic, mapped/inferred lane counts, mapped/building width overrides, urban and non-target exceptions, bridge/tunnel clearance, actual mesh/material/photo painting, edge lines, grading and AI lane widths. Existing width expectations were updated for the intentional rural minimum.
- Added an offline regression from cached Overture/OSM geometry at **41.63481, -84.99966**, the user's Public Square screenshot. It checks all eight circulating arcs, all eight approaches, shared AI/grading widths, a monument in the island, full sidewalk clearance, crosswalks, props and suppression of untagged sidewalks. The AI test verifies one directed ring and all eight entry/exit connections.
- Test sparse farm development, roads developed on one side, mapped rural sidewalks and mapped versus inferred roundabout sidewalks. Simulate bytecode-only builds with different `CODE_ID` values and verify old roads, plans and cell facts are missed without deleting them. The EXE smoke check repeats cache invalidation using its actual archived `peerwork` and `revisit` bytecode.
- Added bridge width/taper, rural corner pavement/grade/paint, two-ended lane-continuity and A Collins Road material/utility regressions, including cached real-road fixtures.
- `git diff --check` passed.
- EXE built successfully; launcher and bundled-server `--help` smoke tests exited 0. No standalone server ZIP was built.
- BeamNG visual/driving validation remains to be performed, especially real-world tight interchanges and roundabouts. Geometric fallback requires curved arcs when OSM roundabout tags are absent. Unknown odd two-way counts use a balanced split with the extra lane along source geometry; directional lane tags are not supplied by the current source.

The branch-specific PR template is [`.github/PULL_REQUEST_TEMPLATE/road-infrastructure.md`](.github/PULL_REQUEST_TEMPLATE/road-infrastructure.md).

## Built EXE

- File: [`dist/BeamNG.Earth-1.6.3-beta.exe`](dist/BeamNG.Earth-1.6.3-beta.exe)
- Size: 73,991,258 bytes (approximately 74 MB), rebuilt with matching bridge approaches, rounded junctions, rural lane continuity, A Collins Road gravel/utility corrections and all earlier branch changes.
- SHA-256: `7d0cf07c9142d8ffc4b0a6f7d0d4670e81aaff350e570c0e1dda14343abb8280`
- Bundled source fingerprint: `ebd51a72311b2896`, verified against the finished server sources.
- Frozen revisit identity: `ac67359648b92e08`; verified to differ from the old custom builds and to change when the bundled source fingerprint changes.
- Verified the finished source fingerprint against the EXE and checked that its archived terrain/mesh gravel materials and Lua terrain configuration exactly match the tested files.
- Verified the archive contains the new road modules, native extension, state boundaries and highway-sign assets.
- Build log: `build/road-connections-build.log`; smoke results: `build/road-port/exe-verification.json`, `launcher-help.log`, `server-help.log`.

## Follow-up: custom EXE HTTPS failure

The custom builds reported multiple map-data hosts as unreachable while the original release worked. Reproduced this in a frozen diagnostic: `ssl.SSLContext` raised `FileNotFoundError` before making any connection. The build interpreter's `ssl.py` requires a sibling `codex-rejected-cas.pem` certificate-policy file, which PyInstaller did not collect. Bundling certifi alone did not satisfy this constructor dependency.

`tools/build_app.py` now includes that runtime policy file beside frozen `ssl.py` when present and checks HTTPS initialization before packaging. Certificate verification, hostname checks and the runtime's certificate-rejection policy remain enabled. Stock Python builds do not acquire an extra dependency.

Validation: 65 focused tests passed; 11 Linux-only tests skipped on Windows. The fixed frozen diagnostic received HTTPS responses from all five hosts in the report (USDA, USGS, Meta's S3 bucket, S3 and Mapterhorn). The full EXE's archived SSL bytecode successfully creates a verified context using its archived policy file, and both launcher/server CLI smoke tests passed.

Previous copies were delivered as `BeamNG.Earth-1.6.3-beta-roads-network-fix.exe`, `BeamNG.Earth-1.6.3-beta-roundabouts.exe`, `BeamNG.Earth-1.6.3-beta-wide-roundabouts.exe` and `BeamNG.Earth-1.6.3-beta-wide-rural-roads.exe`. The new [`../builds/BeamNG.Earth-1.6.3-beta-road-connections.exe`](../builds/BeamNG.Earth-1.6.3-beta-road-connections.exe) includes the bridge, junction, lane-continuity and A Collins Road fixes together with those earlier improvements; its checksum matches the EXE above. The original release was not replaced. Existing older custom EXEs require rebuilding with the corrected packaging tool.
