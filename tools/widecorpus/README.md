# Wide road regression corpus

[places.json](places.json) lists public roads with coordinates, region, type tags and rationale. Add player reports only to the private JSON array at `~/bnge-tests/widecorpus/places_private.json`. Results and images stay outside Git.

Verify new pins against a 260 m aerial and street photos. A nearby access road can produce valid lane samples while the intended structure lies outside the tile.

## Run

From the repository root on Linux, use the server Python environment, shipped native module, C++17 compiler and GNU coreutils.

```sh
nice -n 10 python3 tools/widecorpus/run.py --run nightly --jobs 3 --bisect
nice -n 10 python3 tools/widecorpus/run.py --run smoke --places london-oxford-circus,brno-moravske-namesti
nice -n 10 python3 tools/widecorpus/run.py --run sidewalks --switch BNE_SIDEWALK_V2 --places london-oxford-circus
nice -n 10 python3 tools/widecorpus/compare.py --run nightly --expect-catalog all --gate
```

Default runs compare standard roads to all 17 Revamped switches. `--extra FILE` adds a JSON array of places kept outside Git (the bridge study's 30 sites: `~/bnge-tests/roadsys/study_places.json`); the manifest records the file so `compare.py` finds them. `--switch` tests one at its Revamped value. `--public-only` omits private places. Repeat the same command to resume. Use different run names for different selections. The runner locks scratch, limits affinity to half the available CPUs and runs at nice 10 or lower scheduling priority.

The default 30 m radius selects intersecting zoom-17 geotiles. Each whole tile is measured. Production `blockworker.roads_geo` emits the meshes and their placement variants. `blockworker.build` emits physics terrain. Builds use local DEM precision, traffic rules, mesh roads and sidewalks. Buildings and gas station props are disabled.

Cold builds warm both configurations through production per-host limits. Each measured build or switch trial runs offline in a fresh process with a private copy of the raw inputs. The cache at `~/bnge-tests/widecorpus/cache` evicts inactive snapshots near 27 GB, favours the ten regression leaders and guards in-flight work near 39 GB.

The first 193-place sweep took about seven hours, including anchor corrections and an interruption. With early trials and retained snapshots, the top-ten switch results finished another 28 minutes after the last successful pair. Across 150 uninterrupted cold pairs on the shared PC, median wall time was 2.7 minutes and the 90th percentile was 7.4 minutes, including downloads, warmups and scans. The runner flags 40 minutes without a finished terrain block. Switch trials and broker queuing add time. Per-tile build seconds measure the production road call.

Under `~/bnge-tests/widecorpus/runs/RUN/`, the manifest identifies code, selection and settings. Per-place JSON records input hashes and measurements. `progress.json` tracks the sweep. `comparison.md` and `comparison.json` give ranks and deltas. Failed builds retain logs. With `--bisect`, a production exception is replayed offline alongside a passing default. Repeat a crash investigation with `nice -n 10 python3 tools/widecorpus/failures.py --run nightly --places ID`. Its trials and failed tile are under `failures/ID/`.

## Measurements

| Check | What is counted |
|---|---|
| Collision | The codex-snag scanner finds shared or partially shared mesh edges with vertical discontinuities of (1,5], (5,20], (20,50] and >50 mm. In-lane steps exceed 20 mm and sit at least 0.75 m inside a mapped ground road. |
| Overlap and holes | Triangle overlap within 5 cm, including collider–terrain contacts, overlap with an in-lane centroid and visual base-layer overlap. Mapped lane samples lacking physical support count as holes. |
| Pavement and terrain | Upward-facing road triangle centres >10 cm above or >2 cm below the alternating-diagonal physics terrain, excluding deck surfaces. Paved ground lanes should hit pavement in the terrain material map. Source lines use the game's OSM-derived Overture transportation data. |
| Kerbs, outlines and paint | Steep kerb faces, exposed height >22 cm, overhangs and in-lane lips. Road and sidewalk outlines are checked for self-crossings, angles <15° and edges <2 cm. Marking components under 0.015 m² are counted after a 1 mm vertex weld. |
| Ride and structures | Lane profiles at 2 m stations measure grades >15%, crossfall changes >5 percentage points and vertical acceleration >0.5 g at 30/50/70 mph. Under-deck clearance <4 m and sampled deck ends with >5 cm steps are counted. These are geometric ride estimates. |
| M0 audit (`native/roadsys` audit.rs, the road rewrite's checks: bridgerewrite/DESIGN.md section 10) | On 0.5 m cells inside the mapped lanes (half width less 0.5 m): `double_owner_m2` (two colliders, or a collider and the terrain, within 5 cm), `envelope_intrusion_m2` (a collider other than the drivable surface within 4.5 m above a lane; by kind), `envelope_overhead_m2` (structure parts at least 1 m up), `opening_blocked_m2` (under a structure, less than 2.5 m open), `pier_in_face_m2` (a tall structure face within 1.5 m of a lower lane), `parapet_in_cw_m` (a barrier or parapet wall across a lane at its level), `clearance_short_cells` (< 4.25 m to the lowest part overhead), `clearance_excess_cells` (road to road > 7.6 m) and `road_to_road_max_p50_m`. On the free edges of colliding tops where a lane crosses them: `handover_lip_*` (the step to the terrain or the next collider within 15 cm, 0.25 m samples; kerbs excluded). `tcracks_*` (a vertex of another piece inside a free edge), `folds_in_lane`, `zero_area_in_lane`, `edge_max_m`. |
| Cost and coverage | Triangles and material primitives in generated road, walk, structure and rail meshes (estimated draw calls before engine batching). Counts exclude instanced props. Also recorded are build seconds, lane and pavement samples, deck ends, source faults and lidar waits. Zero observations give no evidence for that check. |

[Score weights](compare.py) rank worsening and improving changes separately, tile by tile. Buried or elevated triangle-centre counts and outline counts depend on tessellation. Compare them with geometry cost and images. `--bisect` investigates the worst ten places, confirmed crashes first. For geometry it tests switches that reproduce at least 80% of the largest weighted tile/check increase, then removes the sufficient set from the full bundle. See each place’s `attribution.json`.

## Matched visual review

```sh
nice -n 10 python3 tools/widecorpus/visuals.py plan --run nightly --worst 3
nice -n 10 python3 tools/widecorpus/visuals.py submit --run nightly
nice -n 10 python3 tools/widecorpus/visuals.py sheets --run nightly
```

Build both native binaries before submission. Each PC request uses matched cameras, an overview, a regional aerial and up to three photo positions. The corrected broker forces unrequested experiments off. Replan older requests. Add `--without BNE_ROADSURF_V3` for a matched removal test. For controls, plan with `--set controls --places ID` and use the same set for submission and sheets. Run `sheets` after the receipts arrive. It validates the cameras, switch settings and returned images before including them in the report. Retry a failed or partial capture with `submit --retry-failed`. Use `plan --allow-incomplete` for a labelled provisional ranking. If buildings obscure an overview, use `plan --set overhead --overhead-only --places ID` and submit that set for an additional centred, downward view.

## Proposed gate

Run a mixed smoke subset per change and the full corpus nightly. Use `--expect-catalog all` to require every current public and private entry, or `--expect-catalog public` for an all-public nightly. Preserve an accepted run. Compare each configuration across revisions with `compare.py --run candidate --against accepted --gate`. Measurement definitions, places, radius, settings and source hashes must match. For changed source data, repeat the affected subset on both checkouts while the same snapshot is retained.

A requested geometry build failure returns 2 from the runner. The comparison gate returns 2 for missing or incompatible evidence, 1 for a confirmed production crash or a tile meeting a threshold below, and 0 for a clean comparison.

| Change on one tile | Hold threshold |
|---|---|
| In-lane lip, kerb inside a lane, deck-end step or self-crossing | 1 new |
| M0 drivability: double owner, envelope intrusion, blocked opening or pier in a face (m²), parapet in the carriageway (m), hand-over lip > 5 mm or collider T-crack | 1 new |
| Collision edges >5 cm | 2 new |
| Unsupported lane or clearance <4 m | 3 additional stations |
| Missing pavement, grade >15%, crossfall jump >5 points, or ride >0.5 g at 50 mph | 5 additional stations per check |
| Overlap | 1 m² additional in a lane, or 3 m² additional visual base-layer overlap |
| Kerbs and markings | 3 additional overhanging faces, 5 high faces or 10 small marking fragments |
| Outline or terrain diagnostics | 25 additional acute angles or tiny edges, or 100 additional elevated or buried triangle centres |
| Coverage | >2% loss of lane or pavement stations, or increased source faults or lidar waits |
| Geometry cost | Both >50% and >10,000 additional triangles |

A collision step solely between neighbouring geotiles needs a seam test. For build time, repeat the tile three times under comparable load and hold a median increase exceeding both 25% and 2 seconds. Review the control and ranked visual sheets before acceptance. A visually confirmed loss blocks release even if the score improves.

Harness tests run with `nice -n 10 python3 -m unittest discover -s tools/widecorpus -q`.
