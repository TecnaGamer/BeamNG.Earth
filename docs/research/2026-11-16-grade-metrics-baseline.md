# Grade metrics baseline (2026-11-16)

The numbers today's grading makes on real places: the reference that a change to the road heights is
judged against. This is step 1 of the vertical solver RFC (docs/VERTICAL_SOLVER_RFC.md). Made by
`tools/grade_metrics.py` (metrics: `server/grademetrics.py`); every place's full numbers, with
the worst spots and their lat/lon, are in `2026-11-16-grade-metrics-baseline.json`.

    BNE_REVISIT=off BNGE_PLANCACHE=off python tools/grade_metrics.py --tiles 1 --out build/grade_metrics.json

Code: dev 4eee541e plus PR #1344's speed fixes (plans bit-identical). Each place has 3 x 3 road
tiles (768 m), and the measures cover the cells those tiles planned. Wall time was 11 min on one
machine.

| place | roads | junction steps ≥ 30 cm | max (m) | seams ≥ 30 cm | made steep | bump p99 (ground) | ground p95 (m) | low decks (through) | plan CPU (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fells_point | 4163 | 28 / 8200 | 2.06 | 0 / 41 | 12 | 0.012 (0.051) | 0.24 | 0 | 13.2 |
| lauterbrunnen | 488 | 3 / 493 | 0.43 | 0 / 61 | 37 | 0.036 (0.143) | 0.47 | 0 | 3.1 |
| amsterdam | 2761 | 5 / 5295 | 5.69 | 0 / 39 | 3 | 0.003 (0.046) | 0.16 | 0 | 20.6 |
| lagos_island | 3700 | 6 / 6311 | 1.71 | 0 / 117 | 25 | 0.009 (0.037) | 0.37 | 39 (2) | 11.5 |
| tokyo_shibuya | 25980 | 120 / 49672 | 5.32 | 1 / 516 | 164 | 0.011 (0.052) | 0.29 | 72 (6) | 184.3 |
| punjab_farmland | 1120 | 0 / 1837 | 0.16 | 0 / 39 | 0 | 0.006 (0.038) | 0.22 | 0 | 2.8 |
| rio_botafogo | 2459 | 62 / 4079 | 2.53 | 9 / 62 | 260 | 0.033 (0.088) | 1.15 | 3 (0) | 6.4 |
| athens_omonia | 3331 | 43 / 6763 | 8.01 | 0 / 38 | 74 | 0.017 (0.059) | 1.08 | 3 (1) | 19.2 |

What each column measures:

- **junction steps**: places where a road's end meets another road (its end or its middle, within
  1 m of its centre line), and the height between the two there. 30 cm is
  `grading.BREAK_STEP_M`. Ends on a cell's edge are counted as seams instead.
- **seams**: the same road (`Plan.rid`) where it leaves one cell's plans and goes on in the next.
- **made steep**: roads whose steepest 10 m is over their class's ceiling (`grading.guard_limits`)
  and steeper than their own ground by more than 2 %. A steep street on a steep hill is not counted.
- **bump p99**: the median over roads of the 99th percentile of the second difference per 2 m (the
  car's pitch change). The ground's own value is in brackets. The plans are smoother than the ground
  everywhere.
- **ground p95**: how far the road stands from the elevation it was made from, |plan − ground|.
- **low decks**: road samples under a bridge deck that are closer to its surface than
  `roads.DECK_ROAD_CLEAR` (8.35 m). This uses only the decks the tiles solved. "through" counts
  roads within 2.5 m of the surface, which cross through the deck's body.

What the numbers say:

- Seams are not where grading breaks. Only Rio has seam steps of 30 cm or more (9 of 62, the worst
  1.4 m), on its steep hills.
- Junctions are where it breaks. The tall ones are not data noise. Athens' 4-8 m steps (around
  38.0547, 23.7445) are residential streets without any bridge or tunnel tag whose ends were planned
  4-8 m under their ground, right where they meet a secondary road that stays on the ground. Those
  are the "local passes disagree" faults the RFC is about.
- Grades the plans made: Rio (260 roads) and Tokyo (164 roads) lead. Punjab's flat farmland has none.
- Planning CPU tracks the road count. Tokyo is 184 s for 15 cells.

Known limits:

- `deck_end_over_ground_median` checks the deck frames, and should be about 0 where decks end on
  the ground. It is 6.2 m in Tokyo and 1.4 m in Lagos, because their expressway decks are pieces of
  long elevated chains whose ends stand in the air. It is about 0.05 m elsewhere.
- Clearance only covers the tiles' own reach. The other measures cover the whole cells.
