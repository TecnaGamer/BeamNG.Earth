# Draft: opt-in support for paved terrain edges

This proposal is **disabled by default** and has not been validated in game.
An earlier unconditional widening of the graded verge was associated with a
reported map/performance regression and was removed from the local installation.
This draft isolates that experiment; it does not claim the regression's cause
has been established or that this option resolves it.

## What changes when enabled

Some ground-road grading cores extend only 1 m beyond the paved carriageway.
The terrain mesh and collision grid have 2 m cells: a cell touching pavement can
include a hillside corner farther away than that verge. A road draped across
such a cell can intersect dirt when the existing steep-cell guard holds its
surface below the hillside triangle.

`BNE_TERRAIN_EDGE_SUPPORT=on` sets the minimum graded support verge to one full
terrain-cell diagonal, `hypot(heightmap.SQUARE, heightmap.SQUARE)` (about 2.828 m
with the current grid). Existing wider verges stay wider. This is symmetric on
both sides, including median-facing edges, and includes narrow measured walks.

The option changes only the grading support returned by `grading.shoulder()`.
It does not widen asphalt, change lane counts/markings, modify banking, introduce
a separate collision surface, or replace any bridge solver. Existing cut/fill
blending and ground-road exclusions remain. Bridge and covered-tunnel spans do
not acquire ground corridors; approach roads remain ground roads and can still
interact with nearby plans when this experimental option is enabled.

With the option omitted or set to `off`, the previous verge widths and generated
road-plan coordinates, heights and banking arrays are unchanged. There is no
new setting slider, renderer, or native-module change.

## Enable only in a test checkout

For a PowerShell process launching the companion server:

```powershell
$env:BNE_TERRAIN_EDGE_SUPPORT = 'on'
# Launch the server from this test checkout with its usual options.
```

Unset the variable or set it to `off` and restart to disable it. The setting is
read at module import and must be inherited by all workers. Existing generated
cache fingerprints include the server source and `BNE_*` environment switches;
the on/off runs use distinct generated keys. No raw geographic cache deletion
or saved-settings replacement is required. Reload the level to retire active
terrain/road/collision objects when changing test runs.

## Automated coverage and remaining checks

`test_terrain_edge_support` checks exact default-off compatibility across road
classes and measured verge widths; terrain-resolution-derived support; unchanged
asphalt and longitudinal plans; unbuilt-road exclusion; bridge/tunnel ground-road
exclusion; both edges of flat and +/-8% banked divided-road curves across a tile
seam; visible terrain/road clearance; collision agreement; and a blended hillside
transition. Inputs are synthetic and do not claim an exact Wyoming reproduction.

The existing grading, road mesh, bridge clearance, approaches, road, terrain
patch, sidewalk collision, tunnel and underpass suites are also run against
the pinned upstream source. No banking or short-overpass changes from other
open PRs are included in this branch.

Before considering a default change, compare this option off/on in game at
US-30 **41.71311,-105.87816** and **41.60284,-105.72247**, through curves,
junctions, both outer/median-facing edges and bridge approaches. Repeat the
working I-80 bridge/banking routes. Record road/terrain collision behavior,
generated wall/cut placement, terrain-build latency, memory, and frame-time
costs with the same cached data and settings. Wider grading support can alter
cut probes and neighboring-road blending; retaining/underpass interactions
and the earlier reported regression require investigation. There is no game,
GPU, FPS or performance-improvement claim in this draft.
