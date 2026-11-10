# Environment-aware grass (1.9.1)

Grassland and shrubland now choose dry, light-green or lush GroundCover assets from the local terrain imagery. The grass density and distance settings continue to control quantity and reach independently of the selected palette.

**Cropland intentionally does not generate grass, even when its imagery is green.** Color chooses the grass type only where the existing land-cover and surface rules already allow grass. A green cultivated field can therefore remain clear of GroundCover; this is expected behavior.

## Classification and exclusions

Grass uses the engine's GroundCover system, separate from the Forest streaming used for trees, bushes and rocks. In the recommended `--near-visual detail` mode, the existing base-photo render supplies its colors before the terrain block is sent to the client.

A normally 256-square-pixel analysis image averages nearby colors over approximately a 12 m radius. Relative greenness, saturation and brightness produce dry, light-green and lush probabilities. Low-confidence gray, shadowed or nearly white imagery blends toward the existing regional/land-cover prior. A geographic hash selects a category from those probabilities, so placement remains stable across reloads and batching. An imagery halo supplies neighboring colors before smoothing to avoid a hard classification edge at block boundaries. No image processing or network request runs per grass object.

WorldCover, slope, road/verge/sidewalk/corner masks, lots, fuel/runway pads, water depth and tunnel holes run before palette selection. Cached building/water polygons and explicitly unvegetated land-use/infrastructure surfaces add vetoes without fetching missing data. Color never turns cropland, asphalt, concrete, dirt, bare/sparse land, rock, snow or water into grass. Existing urban lawns keep their short-only and photo-greenness rules.

The project has no separate OSM vegetation/land-use feed. Existing OSM road information remains part of the road masks, and existing Overture land use supplies polygon evidence. Coarse or missing map data can still miss small unmapped surfaces.

## Assets and streaming

The client references installed game resources at runtime:

| Palette | Preferred assets |
| --- | --- |
| Dry | Small Island golden short grass, West Coast dry blades, Johnson Valley dry billboard tufts |
| Light green | Italy short grass and lighter yellow-green long grass |
| Lush | Existing Hirochi green grass with the small dry accent |

See [the asset audit](2026-11-01-grass-assets.md) for exact definitions, materials, dependencies and fallbacks. Source-specific material names prevent Italy and Hirochi's identically named stock materials from replacing each other. Materials are registered before GroundCover is created. No proprietary meshes or textures are copied into the repository, and no asset conversion is required. Missing sources/dependencies fall back independently.

Four additional terrain layers retain the original grass/dusty ground models and detail appearance. Terrain files list only the materials actually used. Each rural palette retains the original 121,940-element role budget, each scrub palette 58,080 and lawns 30,600 before density/distance multipliers. Dry ground is not automatically thinned. Preferred and fallback providers use fixed per-role populations, radii, fades and grid sizes while retaining their own blade/clump geometry.

Updated against `main` at `e1c72abf66a42f6e877c3e1de8839352360913fc` (`1.9.1-beta`). The golf rough, fairway, green and bunker materials retain their existing indices and remain outside palette selection. Photo-worker rendering transports its analysis colors alongside the photo, without rendering it a second time.

Late and refreshed photos publish their terrain file, masks, material table and base photo together. Fresh terrain filenames retain the normal swap path; stale callbacks cannot overwrite a newer terrain generation or photo. Superseded files remain available to in-flight reads until block eviction/replacement. Palette refreshes explicitly rebuild GroundCover once, including with the optional Linux patch that retains cells across ordinary block moves. At high speed, the current deferred-photo behavior retains the complete terrain/photo bundle and applies it together after slowing down.

Environment color selection currently applies to the recommended **detail mode**. With imagery unavailable/disabled, or custom legacy `mesh`/`terrain` modes that do not build detail photos, the existing surface-based fallback remains. The grass-density UI, road classification, sidewalk generation, bridge geometry and banking implementation are unchanged.

## Verification

The reporter tested the grass in-game across multiple environments and confirmed that it works. An initial report of missing coverage on green fields was traced to cropland: two inspected fields were 99.4% and 100% mapped cropland, preserving the intended exclusion. This user testing confirms appearance across environments; it is not a measured frame-time or GPU-memory benchmark.

The 1.9.1 update collected **1,129 Python tests across 62 selected modules: 1,098 passed, 29 optional/platform/live cases skipped and two environment errors**. Both errors are unchanged cache symlink tests: Windows denied `os.symlink` with `WinError 1314`. All **41 grass classifier, exclusion, layer and lifecycle tests passed**, including sparse-sampling equivalence, golf exclusions, stale generation rejection and photo-worker transport. Surface, vegetation, detail-mode, protocol, quick-photo, restart, packaging, I-80 banking, bridge-clearance/profile, sidewalk, airport and gas-station checks also passed.

All **104 Lua suites passed (16,598 reported assertions)** through the offline LuaJIT/Lua 5.3 runner, including active and speed-deferred coherent palette/photo updates. Both JavaScript map suites passed. Python compilation, static material JSON parsing, and source/BeamMP release packaging passed. These are offline/synthetic checks; no new in-game session was run for this update.

Example commands:

```powershell
python tools/run_tests.py test_grass test_grass_exclusions test_grass_layers test_grass_lifecycle test_surface test_blockworker test_detail test_golf --par 4
python tools/run_lua_tests.py
python tools/audit_grass_environment.py --cache-dir /path/to/beamng-earth-cache --offline
```

The offline geographic audit reads existing imagery, WorldCover and vector caches without requests or cache writes. Dry palette shares were 100% in the Cheyenne prairie sample, 99.97% on Happy Jack Road and 99.93% at Curt Gowdy. Mixed Cheyenne, Denver and Baltimore samples retained green categories. The diagnostic audit used tone=1 and season=off without de-shadow/color-match analyses; its outputs are palette maps, not engine screenshots. Exact initial Colorado/Columbia footprints lacked cached imagery, and decoded DEM coverage was incomplete at five of the seven successful sites. Those gaps are reported explicitly by the audit.

The original implementation's local seven-run warmed benchmark measured median imagery-render time of 42.01 ms before halo analysis and 56.22 ms with it. Applying palettes and writing a 512-square block/masks took 155.09 ms median. The 1.9.1 cleanup samples only eligible positions for sparse grass grids and avoids full coordinate meshes. Dense grids retain broadcast interpolation. A local nine-run warmed synthetic check (256-square random analysis image, 512-square terrain, 10% eligibility, NumPy RNG seed 658) reduced median sampling time from 44.687 ms to 5.856 ms and traced peak allocations from 30.02 MiB to 4.60 MiB. Output was bit-identical to the original full-grid result at selected positions; sparse, dense and empty masks have regression coverage. This measures one sampling call, excluding image rendering and terrain/mask writes. Work runs per block/photo rather than per plant. GroundCover definitions increase from 7 to 19 and mixed blocks can use additional terrain materials; engine GPU-memory and swap-frame timing have not been measured.

## Updating and checking in-game

Restart the server and reload the updated mod/game level so the new static terrain materials and Lua load together. `start.py` updates the installed mod by default. No manual deletion of imagery, land-cover or vegetation caches is needed: grass layers are generated in session terrain files, and the revisit cache fingerprints server source.

Use detail mode with imagery, surface generation and grass enabled. Compare dry Wyoming grassland with a green meadow/pasture or forest floor, then inspect mixed brown/green edges while crossing block boundaries and revisiting the same place. Cropland is not a suitable green-grass check because it intentionally remains excluded. Roads, buildings, water and mapped bare ground should stay clear. Low/Normal/High density should change quantity while preserving the palette.

The GE Lua console command `dump(beamngEarth_groundcover.status())` reports density, distance, created objects, selected installed definitions and resource errors. A complete installation resolves 19 definitions.
