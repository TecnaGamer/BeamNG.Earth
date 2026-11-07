# What shapes the road surface, and in what order

Written 2026-11-07 while chasing steps where freeway decks meet the ground and rough freeway merges (Perth, Mitchell
Freeway). Line references are to `feat/interchange-refine` at d597170.

There are three separate surfaces, built by different code, and they only agree if their inputs agree:

| Surface | Built from | Drawn as |
|---|---|---|
| Ground under and beside roads (physics and visible) | the road **plans**, blended by `grade_points` | the terrain heightmap, a 2 m lattice, bilinear between points |
| Road on the ground | samples of the **physics ground** every 2 m, both edges | render-only ribbon mesh (`roadmesh`), 1 cm markings on top |
| Road on a bridge | **deck profiles** (`roads.BridgeProfiles`) | `roadmesh.deck`: a 1.2 m concrete slab whose top is the road, road mesh 1 cm above |

A ground road's surface is the terrain itself (plus 4 cm lift), so anything that moves the terrain moves the road.
A deck is independent of the terrain: terrain that ends up above a deck's top shows through it.

## 1. Plans, per map cell (`grading._Planner._make_cell`, about line 1757)

Each stage edits the plans of the cell in place: `Plan.z` (centreline heights), `Plan.bank` (cross slope), `Plan.cut`
(walls and raised ground beside the road), `Plan.half` / `road_half` (flat core, carriageway).

| # | Stage | Switch | What it changes |
|---|---|---|---|
| 1 | `plan_roads`: DEM every `PROFILE_STEP` along each ground part; `deck_capped` (ground read beside decks over it); `fill_abutment` (last metres before a bridge filled up to the approach's straight grade); measured heights (roadz); median + Gaussian smoothing per class (sigma 12 m streets, 40 m majors); end pins and extensions; **banking** (`bank_profile`, design-speed rule, 8 % cap, faded to level over 100 m at abutments and loose ends); core width; cuttings and walls (`_add_cuts`) | always | z, bank, cut, half |
| 2 | `couple_neighbours` | `BNE_NEIGHBOURS`, coarse DEM only | z |
| 3 | `snap_junctions`, then `junction_pads` | snap always; pads `BNE_JUNCTION_PAD` | z near junctions; pads also a plane (height + tilt) per node |
| 4 | `_ease_coarse` | coarse DEM only | z |
| 5 | `_lift_approaches` (bridge approaches raised so the deck clears what is under it) | always; crossing solve with `BNE_INTERCHANGE` | z near abutments |
| 6 | `_apply_carriageways`: freeway family plans replaced by the carriageway profile | `BNE_INTERCHANGE` | z (whole plan) |
| 7 | `_lower_underpasses`: roads under decks dip to clear them | always; CW decks with `BNE_INTERCHANGE` | z under decks |
| 8 | `_under_deck_walls`: walls beside streets under freeway decks | `BNE_INTERCHANGE` | cut |
| 9 | `snap_junctions` + `junction_pads` again (after the lifts and dips) | as 3 | z near junctions |
| 10 | `hairpin_cores`, `roadcorners` (corner plans), `railways.grade_plans` (track beds), clip to cell, tunnel mouth plans | various | extra plans, widen |

Bridge parts get **no plan**: nothing grades the ground under a deck except what spills from the plans around it.

## 2. Terrain from plans (`grade_points`, Rust: `native/earthfast/src/lib.rs` about line 1424)

Called through `Grader.apply` for every height query: the physics block (`lift = ROAD_LIFT`, 4 cm), the ground the
road meshes are sampled from (same lift), the visible ground tiles (no lift), the AI graph. Per query point:

- **Pass 0** finds which carriageway the point is in, the raised tops behind cuttings, and an **abutment top**: the
  highest core of any road's last stretch before a bridge within its half width. Distance to a segment is to the
  segment, so at a plan's end it is radial: **this reaches past the plan's end, under the deck**.
- **Pass 1** blends every nearby segment: weight 1 inside the core, falling over the transition (1:2 slope, 5-12 m),
  times `1 / (1 + (d / 4 m)^2)` between roads (`BLEND_R`). Roads at another level give way (`YIELD_RAMP_M`). The core
  is tilted by `Plan.bank`. Cut walls raise the ground behind them.
- The lift is added under roads, tapering with the weight.

`grade_points` knows nothing about deck heights.

## 3. Deck heights (`roads.BridgeProfiles._solve`, about line 6060)

1. `deck_profile`: from the abutments' ground (the physics ground, lift included), clearances over what is under
   (headroom, `maxheight`), decks over decks, merges, easing (curve mode).
2. `_observed`: measured deck heights where a source has them (roadz).
3. `DECK_Z_HOOK` (`BNE_INTERCHANGE`): a freeway family deck is replaced by its carriageway profile.
4. `deck_pads`: where deck parts meet, the hull of their carriageways is paved at their heights.

## Measured (offline, Perth box, 2026-11-07)

- Carriageway profiles agree at every joint between freeway parts (62 joints, largest difference 1 mm). The regions
  are not the cause.
- At the 69 deck ends with a ground plan, the plan's end is at the deck's height to the millimetre (an earlier
  reading of up to 0.31 m compared heights with the anchor frame's up, which falls away with distance). The lip at a
  deck's end is the physics ground's 4 cm road lift, less the 1 cm the deck's road mesh lies over its slab.

## Where the steps and rough merges come from (likely; to confirm in game)

1. **Deck meets ground (screenshot over Murray Street).** At an abutment the ground road ends at the deck's height
   plus 0-5 cm plus the 4 cm lift. `grade_points` then carries that height radially past the plan's end and under the
   deck (pass 0's abutment top, pass 1's blend). Wherever the deck is lower than that, the terrain shows through the
   deck along the 2 m lattice: the saw-tooth edge.
2. **Dirt across the lanes (south of Murray).** Plans of the twin carriageways and the ramps end at different points
   next to each other's decks. Each end spreads a round blob of graded ground 5-12 m wide; over a neighbouring deck
   that is lower, or a banked neighbour's low side, it shows as rounded patches of dirt.
3. **Rough merge.** A slip road and the carriageway it merges into are banked separately and blended by distance
   (`BLEND_R` 4 m). Two different cross slopes averaged across the taper give a rough surface; the junction pads also
   put a flat plane there (skipped for freeway-only nodes since d597170).

## Fixes, in order

1. **Terrain stays under decks.** After `grade_points`, cap the ground inside each deck's footprint at the deck's top
   minus a margin (all three users of `Grader.apply` then agree). Freeway decks first, from the carriageway profile
   (cheap, the same in every process); then every deck. *Done for freeway decks (`grading.under_decks`, the cells'
   `decktops`, `BNE_DECK_GROUND`, 0.10 m): offline in Perth 332 of the 1,241 terrain points under freeway decks stood
   above the deck's road, by up to 0.17 m; none after.*
2. **Ground meets the deck exactly at an abutment.** A plan's end leading onto a freeway deck takes the deck's height,
   and the 4 cm lift tapers to 0 at the abutment. *Done for freeway decks (`_meet_decks`, `BNE_DECK_MEET`): the plan's
   end is set 3 cm under the deck, eased in over 20 m, so the lifted ground meets the deck's road mesh.*
3. **One cross slope through a merge.** A slip road takes the carriageway's bank over its merge taper (or the
   carriageway's weight wins inside its own lanes).
4. Check each in game at the three places (the game's MCP: terrain height against the deck's top along the joint).
