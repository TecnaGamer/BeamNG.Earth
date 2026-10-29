# Fix highway lane counts, restore mile markers and improve exit-sign layouts

Untagged urban highways could gain a third lane, and nearby same-named roads could supply the wrong highway lane count. Mile markers previously supplied by the custom build's bundled reference inventory were also missing from 1.7.1's OSM-only sign path. Independently placed exit records could produce duplicate or overlapping signs. This change restores the relevant 1.7.0 behavior and adds shared exit-sign layout planning while retaining 1.7.1's road, navigation and optional sign-streaming pipelines.

## Changes

- Default untagged motorway/trunk ways to **two lanes**, in towns and in the countryside. On divided highways this means two lanes per carriageway; on two-way roads it means two lanes total. Untagged one-way ramps default to one lane.
- Give valid mapped OSM counts priority over defaults. Reject fractional, conditional, out-of-range and ambiguous list values instead of truncating them into a lane count.
- Recover changed OSM way IDs using closely aligned current highway geometry, matching road class, ramp status and travel direction. Reject conflicting nearby matches.
- Fill missing highway counts only when aligned, connected approaches at both ends agree. Keep explicit lane additions/drops; do not borrow counts from opposing carriageways, ramps, forks or crossing roads.
- Use the same resolved count for ground roads, bridges, tunnels, lane markings and AI. Preserve mapped odd totals, directional splits and shared turn lanes; conflicting directional tags cannot increase the total.
- Consume total counts from the lane-detail shards as well as Layercake. Refresh derived road/structure specs when tags change, and advance the roads cache format to version 8.
- Restore the narrowly scoped I-65 southbound Exit 76 correction from the previous build: one generated ramp lane for OSM way `16257189` within its Upton bounds. The downloaded OSM tag remains intact.
- Restore bundled Michigan and Kentucky mile references, plus the southern Indiana I-65 extension. Whole-mile shoulder markers retain their previous green-panel design, Interstate shields and posted directions. Bundled references remain available when OSM sign shards are empty, unpublished or unavailable; duplicate DOT/OSM references produce one marker.
- Restore Kentucky I-65/I-75's tall Jersey barriers and blue, two-sided markers at calibrated 0.2-mile stations on eligible concrete runs. Restore the I-65 cable section, its median-shoulder switch, the Louisville concrete restart and continuation through Indiana Exit 9. Preserve the Ohio River bridge exclusion, crossovers and ramp openings.
- Mount blue markers on the actual generated wall top. Stream barriers as a separate colliding mesh, with narrow collision safety boxes, complete tile cleanup and shape pooling. Suppress overlapping generic median modules while retaining unrelated guardrails and bridge rails.
- Package the reference inventories, corridor data and restored barrier materials with the application. Preserve source credits and snapshot metadata in the datasets and document them in `SOURCES.md`.
- Deduplicate repeated exit records using their labels, junction location and incoming direction. Keep separate exits and opposing carriageways distinct.
- Place paired destinations at the same junction on a shared overhead gantry, preserving each destination's route shields, exit number and left/right decision arrow. The Exit 72 Lansing/Grand Rapids versus Detroit case is covered by a regression fixture.
- Resolve overlapping roadside panels and overhead assemblies before tile ownership, so adjacent tiles cannot emit duplicate replacements. Recheck pavement clearance when a residual conflict requires moving a sign; retain separate roadside destinations when an overhead assembly cannot fit.
- Restore 1.7.0's content-sized exit panels and compact lettering proportions. Panel height follows the actual route and destination rows, keeping simple signs short and multi-row signs taller. Give text-only route references their own readable row while retaining 1.7.1's unified lettering atlas.
- Restore two roadside poles for single-destination signs and three for multiple destinations (or multiple routes when no destination names are present). Gore and rest-area signs retain two poles. Sample terrain separately under every pole and preserve shared overhead gantries for paired destinations.

## Validation

- **122 of 123 Python tests passed across 14 sign suites** after the size/pole follow-up, including new compact-panel, lettering-clearance and terrain-following pole checks. No tests were skipped.
- Before the size/pole follow-up, **626 of 627 Python tests passed across 39 suites**, using Python 3.12 and the supplied version-100 native module; no tests were skipped.
- The remaining font-atlas regeneration test also fails against the unchanged `74ebb34` files in the same Windows environment: regenerated font metrics differ from the committed atlas metadata. The bundled font artwork and metrics are unchanged by this PR.
- **126 Lua assertions passed** in the road and highway-sign client suites under LuaJIT 2.1, using a high-resolution test clock for Windows.
- Coverage includes lane defaults and OSM resolution, Parquet decoding, directional paint/AI agreement, bridges, tunnels, terrain grading, vegetation, cache refresh and the captured I-80/Otto Road replay. New checks cover offline mile references, actual bundled I-65 corridor placement, two-sided blue markers, treatment boundaries, barrier collision and duplicate suppression, packaged data, paired exit gantries and tile ownership.
- Visually inspected previews rendered from the generated blue-marker, two-/three-pole roadside and paired-exit meshes with the bundled lettering atlas.
- The existing corner-grading fixture explicitly retains its original 13 m road width, keeping that geometry test independent of the changed lane defaults.
- `git diff --check` passed.
- An in-game drive has not been performed.

## Scope and data

Based on `8aaf4eb` (1.7.1-beta). As in the previous custom build, the lane fallback uses the shared motorway/trunk classes worldwide; it has no US-only geographic filter. The restored offline mile inventory covers its documented Michigan/Kentucky routes and southern Indiana extension; other locations continue to use mapped OSM references. Other custom lighting, road-width profiles and curve changes are outside this PR.

OSM `lanes` counts all motor-vehicle lanes on a way, including both directions for a two-way road; directional tags refine that total. See the [OSM lane-tag documentation](https://wiki.openstreetmap.org/wiki/Key:lanes). Geometry and connected-road matching are conservative fallbacks, not proof that every mapped road is up to date.
