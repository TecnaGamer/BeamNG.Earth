# Road details and autosave port

Branch: `port/road-details-autosave`, based on the initial feature port at `3ea50c6` (1.5.5-beta).

## Changes in this batch

- Port the remaining ground-road curves, rounded junction corners and median crossovers from the modified 1.5.3 build.
- Use OSM lane counts with gradual width changes, coordinated ramp tapers and longer entrance/exit auxiliary lanes.
- Keep yellow/orange inside edges and white outside edges aligned with the driving direction and actual pavement width.
- In Michigan, Minnesota, Wisconsin, Idaho and California, use **4.5 m painted dashes, 12 m blank gaps and 0.45 m width**, as clarified by the user. Use bundled state polygons instead of a rectangular region test.
- Give ramp guidance its own pattern: **1.8 m dashes, 5.5 m gaps and 0.25 m width**.
- Port solid white approach dividers and lane turn arrows at ramp terminals.
- Use the same fitted curves and pavement footprint for rendering, terrain grading, tyre materials and the AI graph, including widened lanes and outer shoulders.
- Save the driven position, height and heading every five seconds and on level exit. The existing resume option controls this; explicit saved-place and Random spawn choices keep their priority.
- Restore the saved heading after collision is ready and verify it against the settled vehicle direction.
- Bundle the arrow material and state boundaries with the EXE, with regression checks for packaged assets.
- Keep the level-asset generator consistent with the darker grass already ported, and correct the terrain-blend test to account for the engine's terrain edge holes.

The newer upstream bridge/tunnel profiles and approach solver are retained. This batch does not port the older bridge geometry fixes. The previous concrete texture port remains in place.

## Ramp, curve and bridge-paint corrections

- Remove duplicate endpoints introduced by floating-point rule cuts, which collapsed pavement and edge stripes into stretched triangles at a ramp join.
- Project road XY at the same reference height as shared cross-sections and cached ramp paint. Elevated roads far from the anchor now retain their fitted solid edges instead of falling back to early, overlapping native paint.
- Clip ramp pavement against the complete connected highway context before tile clipping.
- Spread endpoint direction changes over a longer approach, retain fitted vertices in the road mesh and reduce curve simplification from 25 mm to 5 mm.
- Apply the same lane counts, traffic direction, yellow/white edge colors, line widths and regional dash pattern to bridges. Dash phase continues across deck boundaries. Keep the newer vertical profile solver; a deck is widened only when its tagged travel lanes require more room.

These corrections were reproduced against cached I-69 data from the user's session. The sharpest sampled direction change on the affected through-road approach fell from about 0.72 degrees to 0.14 degrees. Offline renders no longer show the collapsed join.

## Duplicate signs and highway cell seams

- Choose one suitable small exit/rest-area sign position on the complete generated pavement, then let its single owning tile draw it. Neighboring tiles cannot search farther down the same ramp and create another panel.
- Check clearance against the actual lane widths, shoulders and auxiliary pavement. Keep distinct OSM exits and all three advance-sign distances.
- Use a consistent source neighbourhood for highway curves, lane-count profiles and ramp tapers. Rendering, grading, surface materials and navigation share it, so a cell boundary cannot select a different fit or restart a taper.
- Cache the source neighbourhoods and sign ribbons. Neighbourhoods use road-only cells around source vertices, including a 320 m approach margin; the cache keeps at most 12 entries with a 64 MiB estimated budget. A cold cache can fetch adjacent road cells; failed reads are retried instead of keeping an incomplete fit.
- Put this and subsequent test EXEs in `BeamNG.Earth-main/builds`.

The current Overture I-69 cache audit covered 220 source cells and 65 motorway features. It reproduced four cell-boundary disagreements larger than 1 cm, the largest about 0.97 m, and found none with the corrected fitting. A separate audit of the older OSM cache covered 160 cells and 123 motorway features, reducing six disagreements (up to 0.98 m) to none. With each provider's cached geometry, the welcome-center test generated one small panel and the three advance signs across 39 rendering tiles. The Overture sign test had complete cached coverage; missing neighbouring cells in the older OSM audit were reported by the scripts. In normal operation MapData fetches or retries missing cells.

## Validation

- Latest sign/seam correction: 242 focused Python tests passed, including road contexts, actual tile sign ownership, left/right gore clearance, distinct nearby exits, narrow splits, map-data resolver integration and deterministic cache content.
- The sign/seam EXE passed 19 regression tests against its embedded modules, generated 16,576 road triangles, retained its lettering/state assets and passed the packaged server command-line smoke check. The final EXE copy matched the staging build's SHA-256 hash.
- 219 focused Python tests passed across road geometry, paint, ramps, terminals, OSM lanes, grading, mesh generation, AI graph, bridges, tunnels and EXE packaging.
- All 47 Lua test suites passed, including autosave and startup selection.
- Terrain blend and generated-level consistency: four tests passed after correcting the stale test fixture and grass generator.
- The final EXE's embedded Python modules and extracted assets were exercised directly: regional dash rules, exit signs, terminal arrows, widened-road grading, five new ramp/bridge regression tests and 16,576 generated road triangles passed. The packaged command-line smoke check passed.
- The broad Python run attempted 1,273 tests and reported 21 failures, nine errors and 25 skips before the two stale terrain checks were corrected. Other failures include unavailable external DEM services, Windows-incompatible Linux/file-cleanup/encoding checks and timing/snapshot expectations. The server deletion failures and terrain snapshot mismatch were reproduced against the unchanged starting commit in an isolated copy.
- In-game driving and visual checks remain to be performed by the user.

## In-game checklist

- [ ] Close the current game and companion, then launch `BeamNG.Earth-main/builds/BeamNG.Earth-1.5.5-beta-signs-road-seams.exe`.
- [ ] Drive past the I-69 Michigan welcome center: one small blue sign in the gore, with the 1-mile, 1/2-mile and 1/4-mile advance signs retained.
- [ ] Recheck the sideways jump between the welcome center and the later I-69 position. The reproduced source-cell seams include approximately `42.081913, -84.995605` and `42.295677, -84.968264`.
- [ ] Check a road in each selected state and a neighboring state; verify the wide dashes stay continuous across streamed tile boundaries.
- [ ] Check one-way roads in both geometry directions, two-way roads and OSM lane-count changes.
- [ ] Drive entrance and exit ramps, interstate connectors and ramp terminals; inspect the tapers, white terminal dividers and individual lane arrows.
- [ ] Recheck the reported I-69 ramp: full-width pavement through its join, a continuous outside solid white edge and no early left ramp stripe through the highway lanes.
- [ ] Drive the widened outer lane and shoulder; confirm the tyres remain on the paved ground material.
- [ ] Check rounded intersections and median crossovers for seams and abrupt corners.
- [ ] Drive away from the starting point, face a recognizable direction, quit and reload using the default spawn; check position and heading. Also check an explicit saved place and Random spawn.
- [ ] Check bridge paint against the approaches in both directions, including lane counts, yellow inside edge and wide regional dashes. Check tunnels and keep the previously reported bridge-height defects separate from this batch.

The changes are left uncommitted for review in GitHub Desktop.
