# RFC: one vertical solver for the graded roads

> **Status (2026-11-22):** step 1 (these grade metrics, no plan change) is merged. The solver itself is superseded by the approved road system rewrite (`BNE_ROADSYS`, one vertical profile per carriageway component over ground and structure; see docs/ARCHITECTURE.md). Its lessons (star ties per node, one-sided corridor bands, deck-part joints are not abutments, cap width = deck half width + road half width, floors past dip ramps, soft L1 grade ceilings) are carried into that design, and these metrics feed its quality checks.

Status: proposal, 2026-11-16 (zolaski333). Nothing here is implemented; step 1 below is the first PR it asks for.

## The problem

A ground road's height is decided by a chain of passes in `grading._Planner._make_cell_roads`, each changing
`Plan.z` in place, in a fixed order, after the profile from the DEM (`plan_roads`):

| # | pass | default | what it wants |
|---|---|---|---|
| 1 | abutment fills, chains / pins | on | continuity onto decks and across cells |
| 2 | `couple_neighbours` | off | parallel roads at matching heights |
| 3 | `snap_junctions` + `junction_pads` | on | branches meet at one height, a node on one plane |
| 4 | `_ease_coarse` | off | smoother where the DEM is coarse |
| 5 | `_lift_approaches` | on | approaches raised so decks clear the roads under them |
| 6 | `_apply_carriageways` | on | motorway carriageways as one profile (already a QP: PR #814) |
| 7 | `_lower_underpasses` | on | roads dipped under low overpasses |
| 8 | `snap_junctions` + `junction_pads` again | on | side roads after 5 and 7 moved their roads |
| 9 | `_pin_cw_ends` (+ `snap_junctions`) | on | carriageway ends back at the deck after the passes |
| 10 | `ramp_envelope` | on | no step, no ramp over `RAMP_GRADE` round a raised road |
| 11 | `grade_guard` | off | grade ceilings ("it changes every road") |
| 12 | `share_corridors` | on | touching carriageways agree in height |
| 13 | `weld_continuations` | on | a way cut at side roads meets itself |
| 14 | `snap_junctions` a 4th time | on | the side roads after 12 and 13 |

Then hairpin widening, rounded corners and rail beds, which read the heights but do not decide them.

Every rule is right on its own. The trouble is how they are combined:

- **Later passes undo earlier ones.** Junction snapping runs up to four times because 5, 7, 9, 12 and 13 move roads it
  had joined. The comments record the cases: alleys 3.5 m over the street a dip lowered (Haven Street, 2026-10-28),
  carriageway ends pulled off their decks by the passes after them (`_pin_cw_ends` exists for that).
- **The order is the model.** Whether a ramp limit or a clearance wins depends on which pass ran last, not on which
  matters more. Moving a pass, or adding one, changes roads everywhere, which is why `grade_guard` is still off.
- **Failures are silent.** Each pass is wrapped in `except Exception` ("rather than no grading": 39 `_caught` in
  `grading.py`), so a pass that fails leaves the plans half done with no sign in the result.
- **Fixes stay local.** `grading.py` is 8,072 lines with 360 module constants and 77 `BNE_*` switches; 160 commits
  touched it from 2026-11-01 to 11-16. Each fix is a threshold or a new pass for the place it was found at, and the
  next report is often the same family of bug somewhere else (junction steps, ramp kinks, decks too low, roads sinking
  over tunnels).

This is not a complaint about the rules: they hold a great deal of what the map and the DEM leave out. It is about
solving them one after another instead of together.

## The proposal

Solve every graded ground road of a planning cell, and its margin, in **one constrained least-squares problem**. This
generalises the carriageway solver (`native/earthfast/src/carriageway_solver.rs`, Clarabel + QDLDL, PR #814) and the
underwater tunnel floors (PR #822), which already do this for their own roads.

**Unknowns.** The heights of every plan's samples, every `PROFILE_STEP` (2 m), as today.

**Objective** (quadratic):
- data: `w_dem(class, DEM quality) * (z - dem)^2` per sample (lidar weighted higher than the 30 m model, `_ease_coarse`
  becoming a weight);
- smoothness: `w_curv(class) * (second difference)^2` per sample, the design speed's curvature (it replaces the Gaussian
  `profile_sigma`, so σ 12 m / 5 m become weights, tuned to give the same profiles on open roads);
- coupling of roads alongside each other (`couple_neighbours`, `share_corridors`): `w_pair * (z_a - z_b - gap_fall)^2`.

**Hard constraints** (linear):
- junctions: the branches' samples at a node equal (or on one plane through the node with a bounded tilt: `junction_pads`);
  a way cut at its side roads equal at its own ends (`weld_continuations`);
- decks and portals: a road's end at an abutment or portal equal to the deck's / floor's height, which stay inputs
  (`BridgeProfiles`, `TunnelProfiles`) in phase 1 (`_pin_cw_ends`, abutment fills);
- clearance: a road under a deck at most `deck - DECK_ROAD_CLEAR`, the deck's approach at least what clears the road
  under it (`_lift_approaches`, `_lower_underpasses`, `bridge_lifts`);
- grade and curvature ceilings per class (`ramp_envelope`'s `RAMP_GRADE`, `grade_guard`'s ceilings);
- bounds: cuttings and fills no deeper than the walls allow (`wall_max_h`), tunnel cover kept.

Passes 3, 5-14 become rows of this one problem; there is no order between them any more. The outputs stay `Plan`s
(`xy`, `z`, `half`, `abut`, `cut`, ...), so `grade_points`, the road meshes, sidewalks, decks, signs and the navgraph do
not change.

**What stays heuristic.** The discrete decisions cannot go into a QP and stay rules, run before the solve as today:
who is over whom (layers, `gradesep`), whether a road dips or the deck rises (the `UNDER_*` / `UNDER_LIDAR` rules),
tunnel lids and open sections, ungraded keep strips, the deck heights themselves. The solver removes the conflicts
*between* the continuous rules, not the need to guess 3D from a 2D map: bugs from wrong layer tags or a missing bridge
stay what they are.

**Infeasible means said.** An infeasible or unconverged solve is logged with the cell, the constraint rows that
conflict and their places, the cell falls back to today's passes and is marked incomplete (not persisted), as the
carriageway solver does now. Contradictory data becomes a report with coordinates instead of a hump.

## Size and speed

Measured 2026-11-16 (PR #1344's branch, central Athens 38.0446, 23.7371, one road tile, plans made every run):
the tile's planning took 2 cells' plans and 3 road stages (one neighbour's, one level deep), 20.7 s of CPU, of which
the densest cell's road stage 10.4 s. So the region a cell depends on is already small; the solve has to fit in a
budget of that order.

A dense cell holds a few thousand plans of a few dozen samples: 10^4-10^5 unknowns. The objective is banded per road
(three entries per sample), junctions and couplings add a few entries each, so the KKT system is sparse; QDLDL's fill
depends on the junction graph and has to be measured, not assumed (step 1). If a whole cell is too big, the cell splits
by its junction graph's components with the roads at the cut pinned, as `CLIP_MARGIN_M` pieces are pinned now.

## Determinism and seams

As today: a cell plans its own roads plus `CLIP_MARGIN_M` of each beyond its box and keeps only its own box
(`trim_plan`), so two cells solve overlapping pieces of the same road. The solve must be a pure function of the cell's
inputs (sorted feature ids, fixed solver settings, one thread per solve) so the plans stay the same in any order, on
any thread (`test_plan_order`). Seams: the proposal pins the piece beyond the box to the DEM's profile at its far
end, so two neighbours agree within the margin's smoothing; step 1 measures the step at cell edges against today's,
and step 2 the solver's.

## Validation

One script, run on `docs/benchmarks.json`'s places plus a held-out set, both ways, reporting per place:

- junction mismatch: max |z| difference between branches at a node;
- grade and curvature over each class's ceiling (count, worst);
- clearance under decks below `DECK_ROAD_CLEAR` (count, worst);
- deviation from the DEM / lidar (median, p95), and cut / fill volumes;
- bumpiness: second difference per 2 m, p99 (the 2026-10 numbers: 0.17 -> 0.027 m);
- seam step at cell edges;
- planning CPU per cell.

The solver replaces the passes when it is no worse on every metric at every place and better on junction mismatch,
grade and clearance, and its CPU stays within today's per cell.

## Steps

1. **Metrics, no change** (one PR): the validation script above on today's passes, so there is a baseline.
2. **Shadow solve** (`BNE_VSOLVE=shadow`, off by default): the solver runs beside the passes, its result only measured
   and logged; plans unchanged. Junctions, decks, clearance and grade first; couplings after.
3. **Opt-in** (`BNE_VSOLVE=on`): the solver's plans used, the passes as the fallback. Driven in game on the benchmark
   places; `PLANS_FORMAT` bumped by the maintainer.
4. **Default on**, then the passes it replaces removed one PR at a time, with their tests rewritten as constraint tests.

Each step is small enough to review and revert. Steps 1-2 change no plan.

## Questions for the maintainer

- Is replacing the pass chain something you want at all, or would you rather keep the passes and add guards?
- The carriageway solver owns motorway families now: grow it into this, or a new kernel beside it?
- Which places should be the held-out set?
- Should rail beds and tunnel floors join the same problem later, or stay their own solves pinned at the shared points?
