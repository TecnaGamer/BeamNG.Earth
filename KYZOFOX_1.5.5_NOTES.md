# 1.5.5 (KyzoFox build): what changed over 1.5.4 beta

These fixes come from an I-20 drive through Louisiana and Mississippi on 1.5.4 beta. Every change is marked
`KyzoFox 1.5.5` in its code comment. Bridge heights were checked offline against the real map and elevation data of
each reported bridge (the cached Overture cells and Mapterhorn tiles), using the server's own `Grader` and
`BridgeProfiles`.

## Overpasses (`server/grading.py`, `server/roads.py`, `server/roadmesh.py`)

| Change | Where |
|---|---|
| Roads onto a twin bridge within 40 m of an abutment (a divided road's other carriageway) no longer cap its ramp | `_end_lift_calc` (`APPROACH_TWIN_M`) |
| The ramp's lift is measured from the road plan's own height at the abutment, not the raw DEM (`bridge_needs`; `bridge_lifts` kept as a wrapper) | `_lift_approaches`, `bridge_needs` |
| A road beside the ramp caps it over that road's own height (`approach_add(..., z)`); the cap slope is 1:1 (`APPROACH_CAP_RUN`) | `approach_add` |
| Roads passing under the bridge cap a ramp beyond 30 m of the abutment (they were never in `others`): no fill ribbons over frontage roads | `APPROACH_UNDER_FREE_M` |
| A raised ramp's core is never below its road (`Grader._floor_ramps`, from `Grader.apply`) | `APPROACH_FLOOR_M` |
| A deck over a motorway or trunk road (`STEEP_UNDER`) may climb up to `DECK_STEEP_GRADE` (12 %) where the clearance needs it; street bridges keep `DECK_MAX_GRADE` | `deck_profile_once(..., steep)`, `BridgeProfiles._solve` |
| A non-major road crossing under a motorway/trunk bridge the deck line (between its raw ground ends) cannot clear is lowered: up to `UNDERPASS_MAX_M` (6 m), flat `w/2 + UNDERPASS_FLAT_M` either side, cosine ramps of `UNDERPASS_GRADE`, carried along up to `UNDERPASS_HOPS` pieces of the same class going straight on (`UNDERPASS_ON_COS`), capped by other ground roads like a ramp. The major road keeps its height within `UNDERPASS_KEEP_M` of the bridge (its cores go to `_floor_ramps`) | `Grader._lower_underpasses`, `_underpass_others` |
| Hard floor: over every road crossing (not a junction at grade, not the road the deck continues at its ends) the deck stands `DECK_HARD_CLEAR` (6.6 m: 5 m under 1.6 m of slab + girders) over the road, flat `DECK_HARD_FLAT_M` either side; it climbs off an abutment no steeper than `DECK_HARD_MAX_GRADE` (25 %), `DECK_HARD_FAST_GRADE` (8 %) on motorway/trunk decks (`fast`), at most `DECK_HARD_GRADE` off a junction at grade (flush for `AT_GRADE_FLUSH_M`); kept after `_merge_overlaps` (`_hard_floor`) | `deck_profile(..., fast)`, `deck_profile_once(..., floors, fast)`, `BridgeProfiles._solve` |
| A junction at grade must lie nearer one abutment than `AT_GRADE_END_SHARE` of the chain's half-length (a 24 m overpass's road at its middle was one) | `deck_profile_once` |
| A neighbour cell that cannot be fetched: `_cell_enu` returns no roads for it instead of raising (which dropped every ramp of the cell), counts a miss (`_tl.misses`); results made with a miss are not cached (`end_cache`) and the cell's plans are made again after `RETRY_S` (`partial`) | `Grader._cell_enu`, `_end_lift`, `_cell_plans`, `_plan_cell` |
| `_lower_underpasses` speed: a piece is tested against a bridge only when their boxes meet; the pieces it goes on as are looked up by their end points (`meeting`); `_underpass_others` takes the dipped pieces' end points as one array and each cell's ground roads with their boxes once (`memo`). Monroe's I-20 / US 165 cell: 10.9 → 2.4 s (1.5.4 beta: 1.3 s); the 3 × 3 cells around West Monroe 22.4 → 7.7 s (1.5.4 beta: 10.1 s). The user fell through the Ouachita River bridge with the terrain there 19-47 s late in game | `Grader._lower_underpasses`, `_underpass_others` |
| `_floor_ramps` is square at a line's own ends, not round: a round end reached half a road width under the deck | `_floor_ramps` |
| `DECK_ROAD_CLEAR` 7.5 → 7.8 m; `roadmesh.DECK_DEPTH` 1.2 → 0.6 m: 6.2 m under the girders | |
| No parapet where the deck's edge lies on another road's surface less than 1.5 m below it | `roadmesh._over_roads`, `deck(..., ground)`, `blockworker` |

Measured clearance under the girders over the lowest lane (1.5.4 beta → 1.5.5): Thomas Rd 3.05 → 6.11 m, Stella St
3.51 → 5.65 m, Nutland Rd 2.12 → 5.00 m, US 65 Tallulah 5.09 → 6.05 m, 31st Ave S Meridian 4.99 → 5.97 m; I-20 over the tertiary road at
(32.5403, -92.6433), Ruston, LA 1.3 → 5.6 m (both carriageways' bridges).

Sweep of every bridge-over-road crossing in the 94 cached map cells (Ruston, Monroe, West Monroe, Tallulah, Meridian,
Newton, Tuscaloosa, Greene County AL; 302 crossings): under 5 m under the girders, 138 on 1.5.4 beta, 8 with the
neighbour-fetch fix alone, 4 now: all in Monroe's I-20 / US 165 interchange (3.4-4.9 m), motorway ramps crossing under
I-20's deck within ~20 m of its abutment, where 8 % cannot climb enough. Lowering a motorway under another is left out.
Decks steeper than 15 % the floor adds: 2 (20 %, 25 %).

## Street furniture (`server/roads.py`)
`ground_props`: a prop row more than `PROP_SNAP_M` (0.6 m) off the graded ground at its own (x, y) is set on it,
except within `PROP_DECK_M` of a deck.

## The Earth's curvature (`server/navgraph.py`, `server/parking.py`)
`navgraph.build` took node heights from `elev()` (heights above the ellipsoid) where the terrain, road tiles and
buildings use `frame.geodetic_to_enu(...)[2]`. The difference is the curvature drop, d²/2R: 9.7 m at 11 km. Signal heads
(`app[...][9]`) and AI nodes floated by that much. Lot stalls (`tile_lot_spots`' `z_at`) had the same issue. Checked on
the Ruston, LA map data in the user's real anchor frame: 169 of the signal heads were 10-11 m off, now 0.

## Retaining walls (`server/roads.py`)
`retaining_walls` now ignores probes within another road's core + `grading.T_MAX` (`_probes_near_other_roads`). There the
ground is blended between the roads, so there's no natural cut. The walls left at the Ruston interchange all stand under
1 m over the ground behind them.

## Streaming (`mod/.../governor.lua`, `core.lua`)
`Gov:setFast`: above 20 m/s with work queued, `targetNow()` is at least `FAST_TARGET_MS` (3.5 ms). Loading mode still
wins, and `chargeEngine`'s debt still applies.

## Tests
- New: `server/tests/test_approaches.py::KyzoFox155Test` (5 tests), `test_navgraph.py::CurvatureTest`,
  `test_roadmesh.py::BridgeLookTest::test_no_parapet_across_a_road_it_starts_on`, `test_roads.py::GroundPropsTest`,
  and the fast budget in `mod/tests/test_governor.lua`.
- Updated for the new numbers: `test_approaches.py` (cap slope, deck depth).
- Failing on unmodified 1.5.4 beta in the same sandbox too: `test_roads.py::PaintRoadsTest::test_bare_verge_beside_roads`,
  `TrackerTest::test_an_away_camera_gets_its_tiles_after_the_cars_and_keeps_them` (intermittent), `mod/tests/test_sky.lua`.
  The end-to-end suites need network access to the map servers.
