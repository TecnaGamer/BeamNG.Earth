# Roads side by side in the one solve: corridor rows (2026-11-18)

In solo mode (2026-11-17-vsolve-solo.md), Rio still had one road through a deck that the passes did not leave. This
note finds the cause and adds the missing rule to the solve. Tied both ways, the rule chained from road to road, so
it is one-sided, as the pass is. The note also fixes a seam that dev's `BNE_SEAM_FILLED` opened, and remeasures the 8
baseline places on this branch after merging dev.

All numbers below come from today's runs. The earlier notes' numbers are not comparable: rerun today, the same old
commit gives other numbers, because the downloaded data has changed since (Lauterbrunnen's seam: 0.033 m on 17/11,
0.156 m today, on commit 0ab2b00f).

## What was missing

The road is a street under Rio's 205 m deck (−22.9450, −43.1790). The deck is longer than `UNDER_MAX_BRIDGE_M`
(120 m), so the underpass rule does not dip the street. In the passes, `share_corridors` (roads side by side agree
in height) lowers it 0.83 m toward the road beside it, which takes it under the deck. The solve had no row for that
rule, so in solo mode the street stayed on its own profile, through the deck.

This rule is the RFC's `w_pair (z_a − z_b − gap_fall)²`, which no step had built yet. The measure had no number for
it either.

## What this adds

- **The pairs (earthfast 472, `plan_corridor_pairs`).** These are `share_corridors`' pairs, geometry only. For each
  vertex of a road, they list each other road whose carriageway runs beside it:
  - within 45° of its direction and 6 m of its edge;
  - not past its end;
  - not on the other side of a cutting's wall;
  - not within 25 m of the road's own bridge end.

  The search is the pass's own: `corridor_core` now calls the same helpers. Its output is bit-identical on 300 random
  cases, compared with dev's earthfast 470 build. A new Rust test covers the pairs.
- **The rows (`vsolve.CORRIDOR_ROWS_ON`, on in the solve's modes; `BNE_VSOLVE_CORRIDOR=off` turns them off).**
  - As in the pass, of two roads side by side, one gives way: by `share_corridors`' keys, the slip road or the lower
    class; between equals, the lower one, which goes up.
  - Its height is held within what the gap allows of the other road's **rule height**: that road's profile, after its
    dips under decks and its floors onto them. The gap allows `CORRIDOR_BANK` (0.4 m per m of gap; nothing where the
    pavements overlap). The target is fixed, so it does not move with the other road's solved height.
  - They are soft rows, with the grade rows' penalty times the road's stiffness. A cap under a deck (10× that penalty)
    wins over them.
  - Like the grade rows, they are added round by round, wherever a solve's heights break them by more than 1 cm.
  - As in the pass, two through roads keep their own heights (a dual carriageway's halves on a hillside).
  - Two roads more than 3 m apart in their rule heights are two levels, so no row ties them (a road dipped under a
    deck beside the ramp up to it).
- **The measure (`grademetrics.corridor_steps`).** For each vertex beside another road, it measures how much more
  the two heights differ than their gap allows, where they are within 3 m. `tools/grade_metrics.py` prints it as
  "corridor ≥ 30 cm".

## Two-sided rows chained; one-sided rows do not

The first form of the rows tied the two solved heights to each other, both ways. In one solve, a road then followed
its neighbour down, the next road followed that one, and so on across a block. The pass moves each road once, toward
its neighbour as the neighbour was before the pass. The one-sided rows do the same: the target is the neighbour's rule
height, which the solve does not move. `test_vsolve.CorridorTest.test_the_next_road_does_not_follow_in_turn` puts a
third road beside the slip road: with the two-sided rows it went down 0.79 m, with the one-sided rows it stays.

Solo mode (on mode in brackets where it differs), without the rows / two-sided / one-sided:

| | Rio | Tokyo | Fells Point | Athens |
|---|---|---|---|---|
| corridor steps ≥ 30 cm | 523 / 36 / **164** (158) | 2123 / 195 / **1056** (908) | 80 / 0 / **15** (17) | 1859 / 171 / **1093** (1007) |
| passes' corridor steps | 171 | 507 | 13 | 998 |
| roads through decks | 1 / 0 / **0** | 14 / 14 / 14 | 0 / 0 / 0 | 1 / 1 / 1 |
| low decks | 3 / 3 / 3 | 50 / 55 / **50** | 0 / 0 / 0 | 3 / 3 / 3 |
| ground p95 (m) | 1.12 / 1.22 / **1.16** (1.15) | 0.29 / 0.32 / **0.29** | 0.22 / 0.23 / 0.22 | 1.58 / 2.20 / **1.69** (1.51) |
| worst seam (m) | 0.55 / 0.55 / 0.55 | 0.57 / 0.31 / **0.21** | 0.02 / 0.31 / **0.36** | 0.05 / 0.05 / 0.05 |
| made steep | 220 / 220 / 225 (224) | 77 / 89 / 86 (85) | 5 / 6 / 6 | 22 / 26 / 29 (28) |

- **What the one-sided rows keep:**
  - Rio's street passes under its deck;
  - the steps between roads side by side are roughly the passes' again (2090 in on mode at the four places,
    against the passes' 1689 and 4732 without rows);
  - Tokyo's worst seam in solo mode drops from 0.57 to 0.21 m.
- **What they cost:** Athens' ground p95 rises by 0.11 m in solo mode and 0.09 m in on mode (against 0.62 m with the
  two-sided rows), and a few more roads are made steep (+1 to +9 per place).
- **Still open:** the rows hold a road near its neighbour's rule height, so they leave a step wherever the solve
  moves the neighbour itself (a junction, a grade row). That is the likely source of the steps they leave; not yet
  checked one by one.

The table above is before the three fixes of the next section. Fells Point's 0.36 m seam came with the rows.

So the rows are on wherever the solve runs. `BNE_VSOLVE` itself is still off by default.

## Fells Point's seam: three fixes

With the rows, Fells Point had a 0.36 m seam (39.26644, −76.59668), on slip road `b5749112…` between cells
14/4706/6245 and 14/4705/6245. The two cells give it the same bands. Three things made them solve it apart, each fixed
in the solve alone:

- **The neighbour's ramp onto its bridge.** The slip road gives way to `44168e28…`, which climbs to its bridge's
  abutment. The rows lifted the slip road up to 1 m along that ramp. 12 m on, the slip road passes under the deck, and
  a cap there held it down. Only one of the two cells had that cap: its vertex sat 0.15 m on the other side of the
  cap's own edge, because the cells draw the road's last 12 vertices up to 2 m apart (below). Following a road up its
  ramp onto its bridge makes no sense: the ramp is that deck's approach, not ground both roads share. The pass already
  holds a road near its own bridge ends (`CORRIDOR_ABUT_HOLD_M`, 25 m). For the solve, `corridor_pairs(...,
  ramps=False)` now also leaves out the pairs within 25 m of the neighbour's bridge end. The measure still counts
  every pair. **This fix takes the seam from 0.37 to 0.02 m.**
- **A road the rows move keeps its ends.** The slip road's correction ran on, smoothed, down to its free end. In the
  cell that owns that end, it took the junction there 1.3 m under its profile, while the other cell held the same end
  at its profile. `share_corridors`' spline keeps a road's ends fixed. The solve now keeps the ends of a road the rows
  move, as soft rows with the rows' penalty. The junction is at 3.97 m in both cells.
- **An end a little off another end.** The cells draw the slip road's last vertices from the roads each has: in one,
  it ends on its junction; in the other (which lacks the third road there), 2 m short. So the solve tied it to the
  junction in one cell, and in the other held it at its profile as a cut end. An end that no road comes within
  `JOIN_M` (1 m) of now meets another road's end within `JOIN_END_M` (2.5 m) (`vsolve.joins`). An end 2 m from the
  middle of a road still meets none. In solo mode, this also takes away the step of
  `test_grading.UnderpassTest.test_a_road_ending_short_of_the_junction_dips_with_it`'s bug case by itself. That test
  turns it off to show the bug.

The drawing difference itself is upstream of the solve (each cell places its roads from its own features), and is not
changed here.

## The 8 places with the rows

Code with the one-sided rows and the three fixes, `--tiles 1`. Each cell is: passes / on / solo. In brackets: on /
solo without the rows (the last table of this note).

| place | junction ≥ 30 cm | worst seam (m) | made steep | ground p95 (m) | corridor ≥ 30 cm |
|---|---|---|---|---|---|
| Fells Point | 28 / 0 / 0 | 0.13 / 0.02 / 0.02 | 12 / 6 / 6 | 0.24 / 0.22 / 0.22 | 13 / **13** / 26 (129 / 80) |
| Lauterbrunnen | 3 / 0 / 0 | 0.24 / 0.03 / 0.03 | 37 / 32 / 32 | 0.49 / 0.47 / 0.48 | 0 / 0 / 0 |
| Amsterdam | 5 / 5 / 5 | 0.05 / 0.02 / 0.02 | 3 / 1 / 1 | 0.16 / 0.17 / 0.18 | 2 / 2 / 2 |
| Lagos Island | 6 / 0 / 0 | 0.14 / 0.06 / 0.06 | 25 / 15 / 15 | 0.37 / 0.37 / 0.37 | 178 / 254 / 260 (728 / 810) |
| Tokyo Shibuya | 66 / 8 / 8 | 0.63 / 0.21 / **0.21** | 156 / 89 / 91 | 0.29 / 0.29 / 0.29 | 507 / 907 / 1054 (2171 / 2123) |
| Punjab farmland | 0 / 0 / 0 | 0.05 / 0.01 / 0.01 | 0 / 0 / 0 | 0.22 / 0.21 / 0.21 | 0 / 0 / 0 |
| Rio Botafogo | 58 / 1 / 1 | 1.42 / 0.55 / 0.55 | 260 / 223 / 224 | 1.15 / 1.13 / 1.13 | 171 / 182 / 191 (524 / 523) |
| Athens Omonia | 31 / 1 / 1 | 0.17 / 0.05 / 0.05 | 86 / 28 / 29 | 1.32 / 1.52 / 1.71 | 998 / 998 / 1087 (1908 / 1859) |
| **total** | **197 / 15 / 15** | seams ≥ 30 cm: **10 / 1 / 1** | **579 / 394 / 398** | | **1869 / 2356 / 2620** (5462 / 5397) |

- **Better than the passes:**
  - junction steps: 197 → 15;
  - seams ≥ 30 cm: 10 → 1. The one left is Rio's 0.55 m, where the passes leave 1.42 m;
  - roads made steep: 579 → 394.
- **Roads through decks:** 16 in on mode and in solo, against the passes' 17. Rio's street is under its deck in solo
  mode again.
- **Close to the passes:** steps between roads side by side, 1869 → 2356 (on) or 2620 (solo); without the rows, 5462.
- **What the rows cost against no rows:** 26 more roads made steep over the 8 places (368 → 394 in on mode).
  Athens' ground p95 rises 1.42 → 1.52 m in on mode and 1.58 → 1.71 m in solo; the passes give 1.32 m.

## Rio

`--at=-22.9450,-43.1790,rio --tiles 1`:

| | passes | solo | solo + two-sided rows | solo + one-sided rows |
|---|---:|---:|---:|---:|
| corridor steps ≥ 30 cm | 171 | 523 | 36 | **164** |
| corridor step max (m) | 2.92 | 2.94 | 1.38 | 2.57 |
| roads through decks | 0 | **1** | 0 | **0** |
| made steep | 260 | 220 | 220 | 225 |
| ground p95 (m) | 1.15 | 1.12 | 1.22 | 1.16 |
| worst seam (m) | 1.42 | 0.55 | 0.55 | 0.55 |

With the rows, solo mode passes the street under the deck again. The one-sided rows leave about as many steps between
roads side by side as the passes (164 against 171). The passes leave theirs because `share_corridors` runs before the
welds and the junction snaps, which move the roads again afterwards.

## Lauterbrunnen's seam: the context roads' approach floors

On this branch after merging dev, Lauterbrunnen's worst seam in the solve rose from 0.03 to 0.43 m. Bisecting
pointed to dev's merge, and within it to `BNE_SEAM_FILLED` (#1398): with it set to off, the seam is 0.03 m again.

`BNE_SEAM_FILLED` makes `_grade_match` read bridge ends as filled ground, so the approach rule lifts more.
- In cell 14/8552/5789, the cell's own road `1bef30ac…` climbs to a bridge. The rule gives it a floor 0.72 m over its
  profile, which raises the junctions at its foot by about 0.9 m and pulls road `f75389d9…` up with them.
- In cell 14/8552/5788, the same roads are context roads. The solve held them at their profiles, with no floor,
  so `f75389d9…` met the seam 0.43 m lower than in the other cell.

The fix (`_vsolve_context`) runs each context road's own cell's approach rule on a copy of its plans
(`_lift_approaches(..., keep=False)`, which keeps no ramp context behind), and gives the solve the lifted heights as
floors. The dips under decks already worked this way. Lauterbrunnen's solve seam is back to 0.03 m (the 8 places
on this code are below).

## Tokyo's seam in solo mode

Solo mode's worst Tokyo seam (0.57 m at 35.67180, 139.65820, road `54465fbe…` between cells 14/14547/6452 and
14/14548/6452) is not a corridor matter.
- Each cell holds a road's cut end past its own box at that cell's profile before the passes, `z_before`.
- The two cells each compute that profile from their own surroundings. Along this road, the two profiles differ by
  up to 0.6 m at the edge, so each cell's solve meets the seam at a different height.
- Holding the ends at the box's edge instead made on mode worse, because the profiles differ there too.

A fix needs one height per seam that both cells share: either a profile that does not depend on the cell, or the
first cell's seam heights given to the second. Both change how cells are planned, so they are left for a later step.
In on mode, the worst Tokyo seam is 0.21 m; solo's 0.57 m is still under the passes' 0.63 m.

## The 8 places

Code `23e72395`, without the rows, `--tiles 1`. Each cell is: passes / on / solo. The one-sided rows at four of
these places are in the table above.

| place | junction ≥ 30 cm | worst seam (m) | made steep | ground p95 (m) | low decks (through) | corridor ≥ 30 cm |
|---|---|---|---|---|---|---|
| Fells Point | 28 / 0 / 0 | 0.13 / 0.02 / 0.02 | 12 / 6 / 5 | 0.24 / 0.22 / 0.22 | 0 / 0 / 0 | 13 / 129 / 80 |
| Lauterbrunnen | 3 / 0 / 0 | 0.24 / 0.03 / 0.03 | 37 / 32 / 32 | 0.49 / 0.47 / 0.48 | 0 / 0 / 0 | 0 / 0 / 0 |
| Amsterdam | 5 / 5 / 5 | 0.05 / 0.01 / 0.01 | 3 / 1 / 1 | 0.16 / 0.17 / 0.18 | 0 / 0 / 0 | 2 / 2 / 2 |
| Lagos Island | 6 / 0 / 0 | 0.14 / 0.08 / 0.08 | 25 / 14 / 14 | 0.37 / 0.38 / 0.37 | 17 (2) / 16 (1) / 17 (1) | 178 / 728 / 810 |
| Tokyo Shibuya | 66 / 8 / 8 | 0.63 / 0.21 / 0.57 | 156 / 77 / 77 | 0.29 / 0.29 / 0.29 | 50 (14) / 50 (14) / 50 (14) | 507 / 2171 / 2123 |
| Punjab farmland | 0 / 0 / 0 | 0.05 / 0.01 / 0.01 | 0 / 0 / 0 | 0.22 / 0.21 / 0.21 | 0 / 0 / 0 | 0 / 0 / 0 |
| Rio Botafogo | 58 / 1 / 1 | 1.42 / 0.55 / 0.55 | 260 / 218 / 220 | 1.15 / 1.11 / 1.12 | 3 (0) / 3 (0) / 3 (1) | 171 / 524 / 523 |
| Athens Omonia | 31 / 1 / 1 | 0.17 / 0.05 / 0.05 | 86 / 20 / 22 | 1.32 / 1.42 / 1.58 | 3 (1) / 3 (1) / 3 (1) | 998 / 1908 / 1859 |
| **total** | **197 / 15 / 15** | seams ≥ 30 cm: **10 / 1 / 3** | **579 / 368 / 371** | | **73 (17) / 72 (16) / 73 (17)** | **1869 / 5462 / 5397** |

- **Better than the passes:**
  - junction steps: 197 → 15;
  - seams ≥ 30 cm: 10 → 1 (on) or 3 (solo);
  - roads made steeper than their ground: 579 → 368.
- **The same:** low decks, and roads through decks. The 14 at Tokyo are decks the data puts on the ground (see
  2026-11-17-vsolve-clearance.md).
- **Worse:**
  - steps between roads side by side, everywhere they exist: 1869 → 5462, because without the rows the solve has no
    corridor rule;
  - Athens' ground p95 in solo: 1.32 → 1.58 m (2026-11-17-vsolve-solo.md).

With the rows on (four places measured), the corridor steps there are 36 / 195 / 0 / 171, under the passes'
171 / 507 / 13 / 998, at the costs in the first table.

The data is in `2026-11-18-vsolve-corridors.json`: `shadow` (the passes, with the shadow solve under `vsolve`),
`on`, `solo` (these three without the rows), `solo_corridor_rows` (Rio, Tokyo, Fells Point and Athens with the
two-sided rows), `solo_one_sided` and `on_one_sided` (the same four places with the one-sided rows, before the
three fixes), and `on_rows` and `solo_rows` (the 8 places with the rows and the three fixes).
