# Targeted rural road fixes and mapped antennas

This change is based on upstream `a4053d3a524c33a85ccaa954a7cfd93bb3d1590c` (1.8.2-beta). It contains the I-25 development/lighting correction, Roundtop retaining-wall and utility-pole grounding fixes, and mapped communications antennas. The experimental perimeter-fence implementation, its data, materials, collision changes and tests are excluded. Existing upstream fences are unchanged.

## Rural sidewalks and lighting

ESA WorldCover's built-up class can include transportation pavement. On the reproduced I-25 frontage/mainline roads, nearby carriageways were counted as development at roadside probes. This made rural roads eligible for inferred sidewalks and continuous urban mainline lighting.

The classifier now excludes known pavement at the actual raster sampling positions using geographically complete neighboring road geometry. A wider probe is used only when the normal probe hits known pavement. Missing source geometry records a cache fault rather than permanently caching an incomplete classification. Explicit sidewalks and `lit=yes/no` are retained as independent mapped decisions, while rural interchange lighting keeps its existing inferred rule. Far lighting uses the same explicit lighting policy.

The road facts format increases from **4 to 5**. Baked plans record the facts version so incompatible urban/lighting facts cannot survive through an older baked result. The grading plans format stays **11**, and no terrain grading, banking or bridge algorithm is replaced.

## Roadside walls and utility poles

The Roundtop reproduction used a sparse centerline height chord as wall support. The chord crossed above a real sag and falsely created stone parapets on both sides of nearly flat road shoulders. Wall eligibility now samples final support at dense centerline stations alongside its existing side probes. Legitimate steep-cut and drop-off wall rules remain active; this is not a location/name exception or blanket suppression.

Roadside poles and lamps previously inherited interpolated road-center Z after moving laterally to their actual XY. They now sample final support at the emitted XY before generating attached arms, lenses and wires. Sidewalk objects retain their draped slab support. Queries remain batched and cover tile-owned bases plus the next endpoint of owned wire spans. Upstream road-clearance, local development, bridge envelopes and rounded wire anchors are preserved.

The production tile builder shares one `roadmesh.drawn_ground` callback between road furniture and antennas. It includes the drawn terrain triangle interpolation and height quantum rather than introducing an independent raw-DEM planting calculation.

## Mapped communications antennas

Runtime placement reads the existing worldwide Overture infrastructure/building layers and their OSM source tags. There is no new live OSM request, regional runtime provider or hardcoded Cheyenne placement. The two captured Cheyenne nodes are test fixtures only.

The selected installed shape is:

`/levels/west_coast_usa/art/shapes/buildings/antenna.dae`

It is a complete ground lattice tower, not a rooftop aerial. Visible bounds are approximately `(-4.510066,-4.510061,-1.8)..(4.510062,4.510064,61.145210)` m. Local Z is up, XY zero is the foundation centre and Z=0 is the planting datum; its foundation stays buried. Mapped above-ground height H gives uniform scale `H / 61.145210`. Missing height uses the documented model default, never a claimed surveyed height. The Forest catalog uses total height 62.945210 m.

The source has five visible details (1,844 / 546 / 427 / 52 / 48 triangulated faces), plus a 590-triangle authored collider. Materials `industrial`, `concrete_plain` and `construction` load from `/art/shapes/objects/main.materials.json`; `lod_vertcol` loads from `/art/shapes/common/common.materials.json`. Referenced textures resolve to installed cooked DDS resources. No proprietary model or texture is redistributed.

Compatibility requires positively identified lattice communications construction, such as `man_made=mast|tower`, `tower:construction=lattice` and `tower:type=communication` or suitable radio/television/mobile flags. Generic towers, water/observation/power/wind structures, radar, rooftops, guyed masts, inactive/historic structures and incompatible mapped heights are excluded. Supported lattice scales are 0.5 to 2. Explicitly compatible mobile ground towers retain the existing separate `celltower.dae` model.

Canonical OSM identities, compact point/footprint checks, building overlap rejection and geographic tile ownership prevent duplicate installations. Source preference is resolved before compatibility, so newer rejected metadata cannot reveal an older eligible alias. Placement samples final terrain at the rounded XY and keeps the tower vertical. Forest streaming, stock LODs, nearby collision, rebases and unloading are reused. The antenna group has a 4 km maximum range; actual visibility still depends on authored LODs and graphics settings. Wind deformation is disabled. A missing antenna DAE creates no item and logs one warning.

## Geographic cases and provenance

| Case | Exact coordinate (latitude, longitude) | Source / expected result |
| --- | --- | --- |
| I-25 Frontage Road | **40.83387, -104.96816** | [OSM way 6169603 v8](https://www.openstreetmap.org/way/6169603), GERS `a78eea09-d357-4e35-a893-37ad22f6bf47`: rural, no inferred urban sidewalk/decorations. |
| I-25 northbound | **40.9585251, -104.9239535** | GERS `5f43e7c5-0f0f-4c23-a95f-567fbbfb42de`: no continuous inferred urban mainline lighting. |
| I-25 northbound, Wyoming | **41.0206898, -104.9030589** | GERS `e60c1ea3-f062-4c23-9bd7-d76ec65d0c0f`: retains rural context. |
| Cheyenne urban control | **41.1361564, -104.810587** | [Way 15732998](https://www.openstreetmap.org/way/15732998): urban sidewalks/lighting remain eligible. |
| Roundtop Road | **41.15981, -104.87994** | [Way 15739626 v11](https://www.openstreetmap.org/way/15739626), GERS `78de7c5f-612f-4f7f-8f28-9ee42f10eaeb`: false sag parapets removed; roadside bases use actual support. |
| Cheyenne antenna | **41.1329323, -104.8398548** | [Node 3787038673 v2](https://www.openstreetmap.org/node/3787038673): compatible lattice communications mast, no mapped height. |
| Unsupported guyed mast | **41.0997414, -104.9374865** | [Node 12752446639 v1](https://www.openstreetmap.org/node/12752446639): rejected for the freestanding asset. |

Regression fixtures retain source geometry, versions, original public URLs, hashes and attribution:

- [I-25 manifest](../../server/tests/fixtures/i25_decorations/manifest.json): 14 captured road cells, WorldCover rasters and public OSM/context evidence, about 1.22 MB.
- [Roundtop manifest](../../server/tests/fixtures/roundtop_walls/manifest.json) and [captured plan manifest](../../server/tests/fixtures/roundtop_walls/cached_plans_manifest.json): nine road cells, 100 decoded DEM tiles and four built-world plan snapshots, about 13.35 MB. This is captured production-plan replay, not a fresh independent regional grading run under plans format 11.
- [Antenna manifest](../../server/tests/fixtures/antennas/manifest.json): two official public node responses, exact coordinates/versions and ODbL attribution. These fixtures do not expand runtime source coverage.

OpenStreetMap-derived records are attributed to OpenStreetMap contributors under ODbL; the manifests retain Overture/WorldCover/DEM source attribution. Informational workstation paths were removed. No fixture requires access to the reporter's game cache.

## Validation

All project imports were verified to come from this isolated upstream checkout, using native module **149**. The final Python selection ran **42 modules / 852 cases: 848 passed, three skipped, one unchanged upstream failure, zero errors**. It covers the new road and antenna tests plus roads, classification, furniture, bridges, banking, grading, sidewalks, mesh/seams, vegetation, buildings, map-data caching, local development and upstream wind-turbine generation/streaming/packaging.

The unchanged failure is `test_bake_formats.FormatTest.test_plans_format`: actual `58caeaab0f182f42f4d8028923d80a873c45750d`, expected `0f6960aba985207e192d8e17a92210bb81bd28f4`. The same mismatch was measured on unmodified upstream before porting this patch. Its plans golden and format were not changed. The facts digest remains upstream `ea0c950f2042ecf24f5f1aef21c0d8bbbc9e66ae`; only its facts format marker changes to 5.

Two optional cached-world road tests are disabled with `BNGE_ROADGEO_REAL=0` to avoid writable stores in the active game cache. One standalone Lua executable agreement case is skipped because `luajit` is not on PATH. The separate Lupa runner passes **all 89 Lua modules**, including 152 tree assertions, antenna asset failure, scaling, streaming, rebasing and cleanup, plus existing turbine regressions. No timing or production clock changes were needed.

The ten new road/antenna test modules are included in the existing CI quality gate. Useful local commands, after installing normal project dependencies and building the native module:

```text
python tools/run_tests.py test_i25_decorations test_decoration_policy test_pavement_context test_roaddecor_debug test_roundtop_walls test_roadside_walls test_roundtop_poles test_pole_grounding test_roadside_debug test_antennas
python tools/run_lua_tests.py
python tools/quality_gate.py
```

The complete multi-platform quality gate is left to CI; the Python/Lua counts above describe the commands actually executed locally. No in-game appearance, collision response or GPU/frame-time claim is made.

## Diagnostics and reload

Optional development filters, set before the next server start:

```powershell
$env:BNGE_ROAD_DECOR_DEBUG = 'w6169603'
$env:BNGE_ROADSIDE_DEBUG = 'w15739626'
$env:BNGE_ANTENNA_DEBUG = '3787038673'
```

Diagnostics are disabled by default. They report source identity, eligibility/rule, final terrain and object positions; antenna records also include asset, scale and height provenance. Hooks run when content is generated, not when retained content is read from cache. The offline tools `tools/road_decorations.py` and `tools/roadside_profile.py` inspect captured data without a running server; the optional profile plot needs Matplotlib.

Restart the world server with this code and reload the updated client mod in a fresh level session when convenient. Antennas follow the existing buildings/object-generation setting. Full generated-content fingerprints and facts-version validation refresh affected output; raw source, imagery and DEM downloads remain reusable. **No cache wipe is required.** A controlled diagnostic session can use `BNE_REVISIT_SKIP=roads,roadsg` to bypass rendered-content caches without deleting source/terrain data. Clear optional variables for later normal starts.

The running server/game and installed mod were left alone while preparing the PR. Engine-side tower appearance, collision and long-distance LOD transitions remain manual verification items. Public map data can be incomplete or stale, and unsupported communications types are intentionally omitted.
