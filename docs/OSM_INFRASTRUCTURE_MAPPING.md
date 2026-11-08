# OSM infrastructure mapping update — 8 November 2026

This updates the local implementation of PR #894. It does not publish or edit the
upstream GitHub PR. The source folder is an extracted distribution without `.git`,
so Git status is unavailable. Existing files were inspected and snapshots saved
outside the workspace before editing. Perimeter fencing was not edited.

## Installed resources and naming corrections

Audited installation: `C:\Program Files\Steam\steamapps\common\BeamNG.drive`,
Steam build `26461569`. Resources below are virtual paths in its `content` archives.
They are existing COLLADA meshes with compiled `.cdae` siblings, instantiated as
`TSStatic` objects. No game mesh or texture is copied into this repository.

| Mapping | Actual installed virtual path | Archive | Default visible height / lowest visible Z |
|---|---|---|---|
| `communication:mobile_phone=yes` | `/levels/johnson_valley/art/shapes/misc/celltower.dae` | `content/levels/johnson_valley.zip` | 28.93660 m / −0.02262997 m |
| `man_made=mast` | `/levels/italy/art/shapes/buildings/italy_bld_antenna.dae` | `content/levels/italy.zip` | 29.95665 m / −0.42177 m |
| Applicable `man_made=tower` | `/levels/west_coast_usa/art/shapes/buildings/antenna.dae` | `content/levels/west_coast_usa.zip` | 62.94521 m / −1.8 m |
| `power=pole` | `/art/shapes/common/power_lines_procedural/electric_pole_wood_old_01.dae` | `content/art_shapes.zip` | 10.04585 m / −0.08358 m |
| Shorter wire configuration | `/art/shapes/common/power_lines_procedural/wire_30m_01.dae` | `content/art_shapes.zip` | Anchors at local X = 0 and 30 m |
| Longer wire configuration | `/art/shapes/common/power_lines_procedural/wire_40m_01.dae` | `content/art_shapes.zip` | Anchors at local X = 0 and 40 m |

The requested literal stems `italy_bid_antenna` and `electrical_pole_wood_old_01`
do **not** exist in this installation. Their installed spellings are
`italy_bld_antenna` and `electric_pole_wood_old_01`; those are the referenced assets.
There are several stock meshes named `antenna`; this implementation explicitly
selects West Coast USA's model. Italy's unscaled mesh is shorter than that model;
WWVB is made 122 m tall by its mapped height, not by assuming the Italy mesh is taller.

`power_lines_procedural` is a directory of reusable pole/wire meshes, materials and
attachment anchors, not a prefab or a callable runtime wire generator. The installed
Assembly Spline editor searches for `nail.*` anchors, whereas this kit has
`anchorPoint_*` anchors. Calling that editor tool would not automatically connect
these assets. We reuse the authored wire meshes directly and fit their endpoint
anchors to the actual mapped support attachments.

The numerical audit, transforms, per-LOD bounds, anchors, material paths, texture
resolution and mesh SHA-256 hashes are in
`server/tests/fixtures/infrastructure_assets.json`. Reproduce without extracting
proprietary resources:

```powershell
python tools/audit_infrastructure_assets.py 'C:\Program Files\Steam\steamapps\common\BeamNG.drive' "$env:TEMP\infrastructure-assets.json"
```

All seven audited shapes are Z-up with metre units. The additional transmission
pylon is the existing `/levels/automation_test_track/art/shapes/objects/pylon.dae`;
its existing native placement origin and scale are retained. The pole's three used attachment nodes
are `anchorPoint_11`, `anchorPoint_6` and `anchorPoint_10`, at local coordinates:

```text
(-0.5226234,  0.003783733, 9.209692)
( 0.6694779, -0.125222000, 7.245391)
(-0.6304421, -0.125222000, 7.245351)
```

Materials are loaded from the kit's `main.materials.json`, the global
`common/common.materials.json`, `objects/main.materials.json`,
`garage_and_dealership/main.materials.json`, `garage_and_dealership/Clutter/main.materials.json`,
Italy's `art/shapes/buildings/main.materials.json`, and Johnson Valley's
`art/shapes/misc/main.materials.json`, as needed per shape.
All **97 referenced texture paths** resolve to installed textures, including their
compiled DDS forms. Italy's DAE also has two legacy material names with no installed
definition: `italy_bld_old_concrete` and `italy_bld_electric_wire`. If those materials
are absent at runtime, the client supplies neutral concrete and dark-wire material
bindings. It preserves existing definitions when present. The antenna's own texture,
metal texture and geometry still come from the game. This fallback is not a visual
match verified in-game.

## Classification, placement and streaming

The global infrastructure layer supplies OSM source IDs, geometry and source tags.
An explicit `communication:mobile_phone=yes` selects the existing Johnson Valley
`celltower` prop globally, ahead of generic mast/tower selection. It keeps source
identity, height metadata, terrain alignment and the same streaming lifecycle;
the old Forest path does not emit a duplicate. Electrical supports retain their
existing power-tag precedence. Otherwise raw `man_made=mast` takes precedence over
an Overture class such as `mobile_phone_tower`. Generic
communications towers use `antenna`. Explicit incompatible `tower:type` values
such as observation, bell, water and lighting retain existing handling rather than
becoming communications antennas. Electrical `power=tower` is excluded from
communications and small-pole selection even if tags conflict.

Mapped utility/communication poles retain their existing inclusion/exclusion rules;
lighting-only, inactive and underground supports are excluded. The global wooden
pole mapping also applies in France while `BNE_MAPPED_POLES` is on. Turning it off
retains the earlier opt-in French kit path. Guessed roadside poles keep their
existing models and rules; mapped runs suppress duplicate guesses.

Node coordinates are preserved. A polygon-mapped structure uses its mapped footprint
centre. When a node and a footprint describe the same structure, one object is
placed at the node, retaining both IDs in `sourceIds`. Repeated source features and
tile-boundary deliveries are deduplicated. Source IDs retain OSM type and number;
only revision suffixes are removed for object identity. Non-OSM features retain
their Overture identity instead of receiving fabricated OSM IDs.

The production placement callback is the final graded/arch-adjusted terrain
(`blockworker.drawn`). The model origin is raised by `-lowestVisibleZ * scale`.
Valid OSM `height` metadata, including metres and feet, determines uniform scale;
the measured mesh height is used when metadata is missing or invalid. Vegetation's
scale cap is not applied to towers. Structures stay upright; terrain slope does
not tilt them. Near-road infrastructure follows the existing buildings/props switch
and road-tile streaming radius.

Records travel with ordinary and prebuilt road content. The client retains one
scene object per stable source ID across overlapping owners, removes it when its
last owner unloads, and transforms every retained owner during rebasing. Missing
models or material files warn once and skip the object without substituting another
model. Later tile delivery can retry a repaired installation. Missing pole resources
also suppress the corresponding stock wire resources.

## Wooden poles and wires

Only active mapped overhead `minor_line`, explicitly overhead `cable`, and overhead
`communication_line` ways establish connectivity. Pole positions must match actual
way vertices within 0.25 m; proximity to a segment or to another pole is insufficient.
Excluded intermediate supports and un-supported bends are never bridged with a
straight chord. Separate lines are not joined by nearest-neighbor distance.

Each accepted span uses three measured, height-scaled insulator attachments. A span
up to 35 m uses `wire_30m_01`; a longer span uses `wire_40m_01`, up to the existing
250 m mapped-span limit. The mesh is rotated onto its actual 3D endpoint direction
and stretched **only along local X**. Thus 30 m, 40 m and intermediate/longer distances
retain the real pole coordinates; unequal terrain elevations are handled by the
endpoint rotation. Mesh width and the stock 1.278578 m sag remain unchanged.

Pairs are keyed by stable support IDs plus conductor number, with a single tile
owner. No intermediate poles are invented. A conservative guard omits conductors
crossing building footprints or falling below 4.5 m above sampled final terrain.
Mapped crossings of roads are allowed when their height is adequate.

On `power=line` ways, a mapped `power=pole` node also uses
`electric_pole_wood_old_01`, regardless of the way's voltage. The way's inferred
pylon placeholder is removed; the pole retains its original coordinates, measured
origin correction and final-terrain elevation. `powersupports.py` joins the actual
support nodes to way vertices, including vertices in neighboring map cells.
Mapped poles also participate in suppression of guessed roadside runs.

The native transmission skeleton is retained so neighboring `power=tower` objects
keep their existing model, placement, yaw and scale. Only conductors that touch a
wooden support are fitted to its actual mesh anchors; tower-only spans remain
unchanged. On these transmission ways the existing streamed wire mesh is retained
to support the existing span range. Three-conductor ways use the three measured
upper anchors. Six-conductor ways can also use the stock lower anchors
`anchorPoint_5`, `anchorPoint_7`, and `anchorPoint_8`. Spans touching wooden supports
use the stock 1.278578 m sag, avoiding a pylon-sized sag through a ten-metre pole's
ground plane. No intermediate support is added. Node type is never inferred from
voltage when an explicit mapped pole is available. Turning `BNE_MAPPED_POLES` off
retains the previous fallback behavior.

This rule is global and enabled by default. There is no Wyoming-specific branch or
support-ID allowlist in the power-pole renderer. Global regression cases cover the
US, Canada, France, Norway, Japan, Australia, high latitudes and an antimeridian
crossing; they also verify that tower models and tower-only wire spans remain unchanged.

## Required supports stay with their wire spans

The missing-pylon investigation reproduced three independent gaps in the former
rendering path. A failed `RoadClearance` check omitted the complete pylon while
retaining its overhead wires. The native coarse deduplication grid could also let
an off-tile support claim the cell of a separate in-tile support; a reproduced
case emitted six conductors but only one of their two required supports. Finally,
Forest's independent pylon cutoff (1,500 m), and point-owning tile delivery, could
hide a support while its incident wire span was still visible.

Required supports now travel as named infrastructure records with every tile that
shows their incident spans, plus the tile owning the base. Their poses come from
the same retained source-way vertices as the wire layout. Only exact source
locations are deduplicated, so nearby separate supports remain visible. Their IDs
use the original geographic vertex and support family, remaining stable across
tile boundaries, rebases and temporarily missing node metadata. Available original
OSM tower IDs are retained in `sourceIds`. No pylons are added at intermediate sag
samples; substation endpoints retain their gantry handling.

The client shares one stock `TSStatic` per support across its tile owners, without
an independent Forest distance cutoff. Safe base owners enable the stock collider;
wire-only owners and unsafe footprints keep the support visible with collision
disabled. Unloading a base tile leaves the pylon visible while a wire owner remains.
Legacy Forest rows for the same supports are removed from the serialized tile to
avoid double models. Mapped `power=pole` supports retain the wooden asset and their
source identity throughout this ownership path.

This fixes missing supports without putting rejected pylon colliders back into roads.
A render-only pylon can still visually overlap a constrained road or bridge; moving
the complete installation and rerouting its wires is outside this correction.
Updated server **and client Lua** are required; use the normal restart/update steps
below. No live reload, restart or active-cache deletion was performed.

## Verified WWVB selection

The snapshot was obtained from the [OSM map API](https://api.openstreetmap.org/api/0.6/map?bbox=-105.058,40.669,-105.032,40.688)
on 8 November 2026. The names, communication/radiator tags and two arrays were
cross-checked against [NIST's WWVB description](https://www.nist.gov/pml/time-and-frequency-division/time-distribution/radio-station-wwvb).
The override requires the exact source node ID **and** proximity within 10 m of
that node's verified coordinates. It never applies to a Fort Collins bounding box.
All eight nodes currently have OSM `height=122`, which is retained.

| OSM node | Name | Latitude | Longitude |
|---|---|---|---|
| [1122692044](https://www.openstreetmap.org/node/1122692044) | WWVB North | 40.6802987 | −105.0516082 |
| [1122695902](https://www.openstreetmap.org/node/1122695902) | WWVB North | 40.6815653 | −105.0494791 |
| [1122705125](https://www.openstreetmap.org/node/1122705125) | WWVB North | 40.6829624 | −105.0526314 |
| [1122705133](https://www.openstreetmap.org/node/1122705133) | WWVB North | 40.6789089 | −105.0484608 |
| [1122705126](https://www.openstreetmap.org/node/1122705126) | WWVB South | 40.6750611 | −105.0436910 |
| [1122705130](https://www.openstreetmap.org/node/1122705130) | WWVB South | 40.6767699 | −105.0465668 |
| [1122705136](https://www.openstreetmap.org/node/1122705136) | WWVB South | 40.6740281 | −105.0460370 |
| [1122705137](https://www.openstreetmap.org/node/1122705137) | WWVB South | 40.6723037 | −105.0431510 |

Nearby WWV nodes are included in the test fixture and explicitly do not receive the
override. `server/data/wwvb_antennas.json` preserves the verified OSM records as a
fallback when the Overture snapshot lacks them. It changes asset selection only,
not the source tags or coordinates.

Eight WWVB antenna top-hat cable ways are tagged `power=line` in OSM:
`97086165`, `97086166`, `759913046`, `759913047`, `759913048`, `759913049`,
`759913050`, `759913051`. Only those verified radio-cable IDs are excluded from
electrical pylon rendering, preventing duplicate pylons at the WWVB antennas.
The ordinary transmission pylon model, layout, clearance and substation connections
are otherwise unchanged. Tower-only electrical conductors are unchanged; mixed
spans use the pole attachment correction described above. WWVB's radio top-hat
cables are not replaced by utility-pole wires.

## Changed files

- Server: `server/mappedinfra.py` (new), `powersupports.py` (new), `mappedpoles.py`, `worldprops.py`,
  `blockworker.py`, `roads.py`, `powerlines.py`.
- Client: `mod/beamng_earth/lua/ge/extensions/beamngEarth/mappedinfra.lua` (new),
  `roads.lua`, `core.lua`.
- Data/tooling: `server/data/wwvb_antennas.json`, `tools/audit_infrastructure_assets.py`.
- Tests: `server/tests/test_mapped_infrastructure.py`, `test_mapped_poles.py`,
  `test_power_support_types.py`, `test_powerline_support_visibility.py`, `test_worldprops.py`,
  `mod/tests/test_mappedinfra.lua`.
- Fixtures: `server/tests/fixtures/infrastructure_assets.json`,
  `osm_infrastructure_wwvb.json`, `osm_infrastructure_mast.json`, `fe_warren_power_supports.json`.
- Documentation: this report and `CREDITS.md`.

No fence, wall, road-classification, bridge, banking, vegetation, crop, wind-turbine
or building-generation algorithms were changed. `worldprops.py` only delegates the
communications structures to their new renderer and avoids rendering inactive or
electrical structures through its old cell-tower path.

## Validation and limits

**510 Python tests passed, zero failures/errors/skips**, across the new mapping
tests and selected existing mapped-pole, world-prop, transmission, furniture,
regional-geotile, road, bridge-clearance/profile/type, I-80 banking, sidewalk,
rural-lighting, wall, wind-turbine, worker, grass, surface and crop suites.
The new mapping suite contains 19 tests; its fixtures include real OSM mast,
WWVB/WWV tower, power-pole and minor-line records.

The eight support-type regressions include global cases and complete real ways
`158028370`, `158100138`, `158100246`, and `157467934` through/near the west side
of F. E. Warren, Wyoming. The mixed 115 kV ways reproduce the original bug:
classification by way voltage gave `power=pole` nodes pylons, and the old mapper
deferred to those pylons. All **156 mapped pole nodes** in the fixture now select
the wooden asset; **54 mapped tower rows are identical** to their previous output.
A tower-only control retains identical conductor geometry too. Tests cover missing,
66 kV, 115 kV and 230 kV voltage metadata, mapped pole positions when the legacy
pylon clearance moves supports, real insulator attachment positions, final terrain,
and tile-boundary ownership.

A separate read-only run used the player's actual cached Overture infrastructure:
**143 cached pole nodes across 85 road tiles** all generated the wooden asset at
their mapped positions, with no pylon at those coordinates. This check used flat
elevation to isolate support selection. It neither changed the cache nor contacted
the running server, and it was not an in-game rendering test. Thirteen of the live
fixture's pole nodes were outside the available cached cells.

Eleven support-visibility regressions cover unsafe footprints, the native off-tile
claim failure, nearby separate circuits, shared junctions, visible spans whose bases
are outside the tile, substation exclusions, wooden endpoints, rebasing, late node
metadata, serialization without duplicate Forest models, and the F. E. Warren data.

**930 Lua checks passed** across `test_mappedinfra`, `test_roads`, `test_geotile`,
`test_windturbines`, `test_trees`, `test_core_stubbed`, `test_surface_layers`,
and `test_groundcover_environment`. The new renderer suite has 111 checks, including
3D endpoint fitting for both stock wire configurations, upright scaling, duplicate
ownership, tile unloading/reloading, rebasing, missing resources and recovery.
The pylon checks also cover shared incident-span owners, safe collision promotion,
render-only retention after the base tile unloads, distant support creation and
floating-origin placement.
Python compilation and the installed-resource audit passed. Native kernels were
not changed or rebuilt for this update.

No successful in-game rendering is claimed: scene tests use stubs. Unmeasured
risks include visual material fallback quality, distant LOD appearance, and the
frame-time cost of individual stock wire objects. Long spans retain the stock sag
rather than simulating cable tension. Terrain clearance is sampled every ~2 m;
thin obstacles absent from the building data are not known. Simplified source
ways lacking pole vertices can omit legitimate spans. Building checks deliberately
omit even a legitimate span above a footprint. The WWVB fallback and other global
source data are snapshots, not live OSM synchronization. Unmapped objects are not
invented. WWVB radio top-hat cable geometry is not rendered by this update.

## Applying when ready

The running server/game were not restarted or reloaded, and no active caches were
cleared. This source update has not copied files into the active installed mod.

1. When finished playing, close BeamNG.drive and stop the world server normally
   (Ctrl+C in its console, or the app's normal stop control).
2. From this source folder, run `python start.py` or use the normal project launcher.
   Its default behavior updates the installed mod from `mod/beamng_earth`; do not
   use `--keep-mod` for this update.
3. Relaunch BeamNG.drive and enter BeamNG.Earth normally. That loads the new Lua
   module and requests road content using the new infrastructure fingerprint.

No manual cache deletion is required. The fingerprint invalidates cached `roads`
and `roadsg` content automatically; elevation, imagery, vegetation and other caches
are retained. Existing saved/prebuilt road content is rebuilt on demand under its
new key. A full game restart is the recommended reload route here, so no live-world
Lua reload is needed. If diagnosing missing assets later, inspect warnings tagged
`beamngEarth.mappedinfra`; do not clear all game or map caches as a first step.

The F. E. Warren correction advances the infrastructure fingerprint to
`mapped-infrastructure-assets-v2-node-power-tags`, so earlier pylon-based road
content will not be reused after the normal restart.

The mobile-phone addition advanced the fingerprint to
`mapped-infrastructure-assets-v3-mobile-phone-towers`. Previously generated generic
mast/tower tiles are rebuilt automatically after the normal restart.

The required-support correction advances the current fingerprint to
`mapped-infrastructure-assets-v4-supported-wire-spans`. Old road tiles that omitted
required pylons are rebuilt automatically. The updated client Lua must be copied
with the server update so it recognizes the pylon support records.

### Wyoming street lighting

Wyoming uses `na:osm-points`: no road-generated lamps on mainlines, ramps,
city streets, sidewalks, or utility poles, including ways tagged `lit=yes`.
Only active OSM street-lamp nodes present in the infrastructure source are
placed, at their mapped positions. Missing source nodes produce no inferred
replacement lamps. Other states retain their existing lighting rules.
The road cache lighting version changes automatically; no live cache reset
or server/game restart is performed by this change.
