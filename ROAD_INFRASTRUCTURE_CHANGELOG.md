# Road Infrastructure Update 1.6.3

**October 26, 2026** · Branch: `port/road-infrastructure-1.6.3`

Migrates the custom 1.6.2 road improvements into 1.6.3 and adds fixes for road geometry, lane continuity and loading. Includes the earlier highway signs, tree density options and per player autosaves.

## Roads and markings

- Restored lane widths, asymmetric highway shoulders, zipper merges and entrance/exit lane extensions from 1.6.2.
- Widened ramp travel lanes to **4.8 m** and restored yellow/orange left edge highway markings.
- Applied regional marking dimensions from [ROAD_IMPROVEMENT_NOTES.md](ROAD_IMPROVEMENT_NOTES.md), using the final Kentucky entry: **3.5 m dashes, 8.9 m gaps, 0.6 m markings and no ramp dashes**.
- Improved OSM lane count handling. Unmapped one way motorways and trunk roads default to **two lanes**; mapped counts feed road geometry, markings and AI.
- Widened eligible two lane rural roads worldwide to **4.25 m per lane**, with **11 m minimum pavement width** including shoulders.
- Fixed isolated rural lane count gaps when matching two lane roads connect at both ends.
- Fixed highway gaps where three lanes suddenly became two and then returned to three caused by missing or inconsistent cached data. All six cached copies of the reported **I-65 North segment near Plum Springs** now use three lanes and matching widths. Explicit lane drops and ramps remain intact.

## Bridges, intersections and roundabouts

- Matched bridge widths to connecting roads and added smoother width transitions.
- Reduced unnecessary overpass humps and improved terrain connections at bridge ends, including **I-165 South at Exit 3**. Required crossing clearance is preserved.
- Added rounded intersection pavement and edge markings, including intersections without sidewalks.
- Widened roundabouts to **9 m per circulating lane** plus edge allowances, with wider short approaches.
- Improved roundabout detection and removed conflicting sidewalks, curbs, parking and roadside objects, including at **Public Square in Angola**.
- Reduced automatic sidewalks in rural areas and around roundabouts while preserving explicitly mapped sidewalks.

## A Collins Road, Logan County, Kentucky

- Added gray gravel with two pale wheel tracks to match the supplied real life reference.
- Applied gravel tire physics and consistent textures across tile boundaries.
- Removed pavement markings, sidewalks and generated utility poles, lamps and wires along this road.

## Loading and reliability

- Reduced repeated roundabout clearance and intersection calculations through caching and faster nearby node searches.
- Isolated road photo benchmarks improved from **6.64 to 0.78 seconds** near Exit 3. A further optimization reduced the dense northbound benchmark from **4.36 to 2.19 seconds**, with identical images.
- Added stage timings for road tiles taking over 10 seconds to help diagnose remaining stalls.
- Fixed custom build HTTPS failures caused by a missing SSL runtime file.
- Fixed generated world cache identities so updated EXEs rebuild outdated geometry while preserving downloaded map data and saved positions.
- Retained the current worker settings and native 1.6.3 road, bridge, terrain and railway systems.

## Validation

- **Python:** 798 passed, 4 skipped across 51 modules.
- **Lua:** 203 assertions passed across five test files.
- EXE packaging, bundled source checks and launcher/server startup checks passed.
- User driving feedback: loading is substantially better, with one brief remaining delay. Benchmarks measure individual calculations, not total loading time; broader visual and driving checks remain pending.

## Latest build

- [BeamNG.Earth-1.6.3-beta-highway-loading.exe](../builds/BeamNG.Earth-1.6.3-beta-highway-loading.exe), approximately **74 MB**.
- SHA-256: `c288b71838f9f9cd47c8023e090c177d37f6d77d4d7b45b4fb3fc4015cb0245e`
- Source fingerprint: `2f9ddfaf248e25ce`.
- Previous delivered builds were retained. No standalone server ZIP was built.

[Branch PR template](.github/PULL_REQUEST_TEMPLATE/road-infrastructure.md) · Validation reports: `build/highway-loading/` and `build/highway-loading-final-tests/`.
