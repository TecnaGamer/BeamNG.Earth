# Seams in the one solve: the next cells' roads in its problem (2026-11-17)

This follows the clearance note (2026-11-16-vsolve-clearance.md) and does the first item on its "Next"
list. A seam is where a road leaves one cell's plans and goes on in the next cell's. All the numbers
are in `2026-11-17-vsolve-seams.json`.

    python tools/build_native.py --fast     # earthfast 373 (the committed modules are 372)
    BNE_REVISIT=off BNGE_PLANCACHE=off python tools/grade_metrics.py --vsolve --tiles 1 --out build/vsolve.json

The code is vsolve/soft-rows merged with PR #1344's speed fixes. It ran on a local development build
of the kernel: 8 places, 16.6 min wall (13.4 before).

## Why the two cells disagreed

Each cell solves its own roads, up to 400 m past its box (`CLIP_MARGIN_M`), so a road's two cells
should see the same thing near their shared edge. But a road that belongs to the next cell alone was
in neither of its problems. Rio's worst seam (-22.95316, -43.17627, 0.79 m) is one road's end 6 m
past the edge of cell 14/6227/9265:

- In cell 14/6226/9265, where the end is inside the box, it meets a junction of four roads, and the
  solve lifts it 1.24 m with them, to 7.39 m.
- In 14/6227/9265, two of those four roads belong to the other cell alone, so they are not in its
  problem. The end is held at its profile, 6.15 m.

The passes have the same gap (Rio: 9 seams of 10 cm or more, the worst 1.42 m).

## What changed

`_vsolve_context` adds to the shadow solve the next cells' graded ground roads that come within
`VSOLVE_CONTEXT_M` = 150 m of the box. That is more than the widest spread a change takes
(`vsolve.SPREAD_M`, 120 m), so a change at the edge of the context has died out at the box. They are
in the problem, and none of them are in the output (every plan is trimmed to the box).

Each road is planned as its own cell plans it: its pieces, abutments, chains, observations and decks
are that cell's. Planned without its chains first, a road reached the junction at 6.34 m where its
own cell had 7.38 m, and the seam stayed. No pass runs on them, so their heights are their profiles.

## Results (passes / before / with the next cells' roads)

| place | seams ≥ 10 cm | seams ≥ 30 cm | worst seam (m) | made steep | solve CPU (s) |
|---|---:|---:|---:|---:|---:|
| fells_point | 1 / 1 / 0 | 0 / 0 / 0 | 0.13 / 0.11 / 0.02 | 12 / 5 / 5 | 15 / 19 |
| lauterbrunnen | 1 / 1 / 0 | 0 / 0 / 0 | 0.24 / 0.16 / 0.03 | 37 / 32 / 32 | 2 / 2 |
| amsterdam | 0 / 0 / 0 | 0 / 0 / 0 | 0.05 / 0.03 / 0.01 | 3 / 1 / 0 | 10 / 10 |
| lagos_island | 1 / 0 / 0 | 0 / 0 / 0 | 0.14 / 0.10 / 0.10 | 25 / 6 / 6 | 15 / 21 |
| tokyo_shibuya | 4 / 9 / 4 | 1 / 1 / 0 | 0.63 / 0.32 / 0.23 | 164 / 69 / 68 | 100 / 146 |
| punjab_farmland | 0 / 0 / 0 | 0 / 0 / 0 | 0.05 / 0.06 / 0.01 | 0 / 0 / 0 | 2 / 3 |
| rio_botafogo | 9 / 9 / 3 | 9 / 3 / 1 | 1.42 / 0.79 / 0.55 | 260 / 218 / 217 | 8 / 11 |
| athens_omonia | 1 / 2 / 0 | 0 / 0 / 0 | 0.17 / 0.22 / 0.05 | 74 / 20 / 20 | 12 / 19 |

Totals: seams of 10 cm or more 17 / 22 / 7, of 30 cm or more 10 / 4 / 1. The other measures are
unchanged within one or two roads: junctions 35, through-deck 8, clearance rows broken 6. Grade rows
broken went 177 → 194, mostly in Tokyo (101 → 114): the context roads' own rows.

The cost is the solve's CPU, up 40 % on Tokyo (100 → 146 s), from planning and solving the extra roads.

## What is left

- **Rio, 0.55 m** (-22.95306, -43.17627) and 0.12 m (where the 0.79 m was) are at the same junction.
  One of its roads is clipped at the edge of its map cell, 1.7 m short of the junction, in one cell's
  data and not in the other's. So the two cells still see different junctions, which is a
  difference in the inputs.
- Tokyo's worst, 0.23 m (35.64659, 139.70215), has not been looked at.

## Next

1. The overshoot past a held point: try the tension term on `test_vsolve`'s case.
2. Amsterdam's stacked junction (52.36678, 4.87054).
3. The RFC's step 3 (opt-in): the caps and floors come from the passes' heights, and the context
   roads have no passes. Without the passes, both come from the decks' own heights.
