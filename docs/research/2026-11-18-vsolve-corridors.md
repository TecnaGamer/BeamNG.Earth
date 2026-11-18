# Roads side by side in the one solve: corridor rows (2026-11-18)

In solo mode (2026-11-17-vsolve-solo.md), Rio still had one road through a deck that the passes did not leave. This
note finds the cause, adds the missing rule to the solve (opt-in), and fixes a seam that dev's `BNE_SEAM_FILLED`
opened. It then remeasures the 8 baseline places on this branch after merging dev.

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
- **The rows (`vsolve.CORRIDOR_ROWS_ON`, `BNE_VSOLVE_CORRIDOR=on`, off by default).** Two heights side by side may
  differ by what their gap allows at `CORRIDOR_BANK` (0.4 m per m of gap; no difference where the pavements overlap).
  - They are soft rows, with the grade rows' penalty times the stiffer road's stiffness. A cap under a deck (10× that
    penalty) wins over them.
  - Like the grade rows, they are added round by round, wherever a solve's heights break them by more than 1 cm.
  - As in the pass, two through roads keep their own heights (a dual carriageway's halves on a hillside).
  - Two heights more than 3 m apart in the rule's heights (after the dips and the floors) are two levels, so no row
    ties them (a road dipped under a deck beside the ramp up to it).
- **The measure (`grademetrics.corridor_steps`).** For each vertex beside another road, it measures how much more
  the two heights differ than their gap allows, where they are within 3 m. `tools/grade_metrics.py` prints it as
  "corridor ≥ 30 cm".

## Why the rows are off by default

The rows fix Rio, but they cost elsewhere. Solo mode, without the rows → with them:

| | Rio | Tokyo | Fells Point | Athens |
|---|---:|---:|---:|---:|
| corridor steps ≥ 30 cm | 523 → **36** | 2123 → **195** | 80 → **0** | 1859 → **171** |
| roads through decks | **1 → 0** | 14 → 14 | 0 → 0 | 1 → 1 |
| low decks | 3 → 3 | 50 → 55 | 0 → 0 | 3 → 3 |
| ground p95 (m) | 1.12 → 1.22 | 0.29 → 0.32 | 0.22 → 0.23 | 1.58 → **2.20** |
| worst seam (m) | 0.55 → 0.55 | 0.57 → 0.31 | 0.02 → **0.31** | 0.05 → 0.05 |
| made steep | 220 → 220 | 77 → 89 | 5 → 6 | 22 → 26 |

The cause is chaining. In one solve, a road follows its neighbour down, the next road follows that one, and so on
across a block. The pass moves each road once, toward its neighbour as the neighbour was before the pass. At Athens
that takes the ground p95 from 1.58 to 2.20 m (2.88 m before pairs more than 3 m apart were left out).

Without the rows, the one solve has no corridor rule at all. Over the 8 places, it leaves about three times the
passes' corridor steps (below). So before the solve is on by default (RFC step G), the rule needs a form that does
not chain. Two options:
- one-sided rows: only the minor road follows, toward the major road's rule height;
- rows that tie a road to its neighbour's rule height, not to its solved height.

## Rio

`--at=-22.9450,-43.1790,rio --tiles 1`:

| | passes | solo | solo + rows |
|---|---:|---:|---:|
| corridor steps ≥ 30 cm | 171 | 523 | **36** |
| corridor step max (m) | 2.92 | 2.94 | 1.38 |
| roads through decks | 0 | **1** | **0** |
| made steep | 260 | 220 | 220 |
| ground p95 (m) | 1.15 | 1.12 | 1.22 |
| worst seam (m) | 1.42 | 0.55 | 0.55 |

With the rows, solo mode passes the street under the deck again, and the steps between roads side by side drop from
523 to 36. The passes leave 171 because `share_corridors` runs before the welds and the junction snaps, which move
the roads again afterwards.

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

Today's code (`23e72395`), rows off, `--tiles 1`. Each cell is: passes / on / solo.

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
`on`, `solo`, and `solo_corridor_rows` (Rio, Tokyo, Fells Point and Athens with `BNE_VSOLVE_CORRIDOR=on`).
