# The one solve in shadow (2026-11-16)

Step 2 of the vertical solver RFC (docs/VERTICAL_SOLVER_RFC.md). Under `BNE_VSOLVE=shadow`,
`server/vsolve.py` solves every cell's ground-road heights in a single QP next to today's passes. Its
result is measured and logged, and the plans stay as the passes made them. This compares the two on
the same cells as the step-1 baseline (2026-11-16-grade-metrics-baseline.md), with the same metrics.
All the numbers, including each cell's stage and the worst spots with lat/lon, are in
`2026-11-16-vsolve-shadow.json`.

    BNE_REVISIT=off BNGE_PLANCACHE=off python tools/grade_metrics.py --vsolve --tiles 1 --out build/vsolve.json

Code: vsolve/shadow 1d48c1e2 merged with PR #1344's speed fixes. 8 places, 50 cells, 11.7 min wall.

## Results (passes → one solve)

| place | junction steps ≥ 30 cm | max (m) | seams ≥ 30 cm | seam max (m) | made steep | bump p99 | ground p95 (m) | low decks | CPU (s): passes / solve |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fells_point | 28 → 0 | 2.06 → 0.28 | 0 → 0 | 0.13 → 0.11 | 12 → 4 | 0.012 → 0.004 | 0.24 → 0.22 | 0 → 0 | 26.9 / 9.1 |
| lauterbrunnen | 3 → 0 | 0.43 → 0.11 | 0 → 0 | 0.24 → 0.16 | 37 → 34 | 0.036 → 0.021 | 0.47 → 0.44 | 0 → 0 | 4.7 / 1.1 |
| amsterdam | 5 → 5 | 5.69 → 5.65 | 0 → 0 | 0.05 → 0.03 | 3 → 0 | 0.003 → 0.002 | 0.16 → 0.17 | 0 → 0 | 35.2 / 5.5 |
| lagos_island | 6 → 0 | 1.71 → 0.06 | 0 → 0 | 0.14 → 0.07 | 25 → 4 | 0.009 → 0.006 | 0.37 → 0.35 | 39 → 39 | 21.6 / 10.5 |
| tokyo_shibuya | 120 → 28 | 5.32 → 1.22 | 1 → 3 | 0.63 → 0.32 | 164 → 77 | 0.011 → 0.004 | 0.29 → 0.27 | 72 → 60 | 264.4 / 81.4 |
| punjab_farmland | 0 → 0 | 0.16 → 0.01 | 0 → 0 | 0.05 → 0.06 | 0 → 0 | 0.006 → 0.005 | 0.22 → 0.21 | 0 → 0 | 5.0 / 2.8 |
| rio_botafogo | 62 → 1 | 2.53 → 0.32 | 9 → 3 | 1.42 → 0.79 | 260 → 223 | 0.033 → 0.018 | 1.15 → 1.06 | 3 → 3 | 11.5 / 6.7 |
| athens_omonia | 43 → 1 | 8.01 → 0.34 | 0 → 0 | 0.17 → 0.22 | 74 → 22 | 0.017 → 0.011 | 1.08 → 1.06 | 3 → 3 | 36.0 / 14.9 |

Totals: 267 → 35 junction steps of 30 cm or more, 575 → 364 roads made steep, 10 → 6 seams.
No cell fell back to the passes' heights.

- **Junctions** are fixed nearly everywhere, which was the main goal. The 8 m steps in Athens are
  gone (worst 0.34 m). Most of what is left is in Tokyo (28, worst 1.22 m) plus Amsterdam's 5.
- **Amsterdam's 5** are all at one spot (52.36678, 4.87054), with the same five steps before and
  after. The solve did not tie these ends into a node. Why is not known yet: it could be an
  abutment end held at its deck's height, or two levels 5.7 m apart that the metric reads as a
  meeting.
- **Seams**: fewer of 30 cm or more in total, and the worst ones are much lower (Tokyo 0.63 → 0.32,
  Rio 1.42 → 0.79). Tokyo went from 1 to 3, but each is 0.30-0.32 m, right at the threshold. A
  cell's solve doesn't see the next cell's solve yet. It only matches the pins at its cut ends,
  which come from the ground and the underpass dips.
- **Ground deviation** is equal or better everywhere except Amsterdam (+1 cm at p95), and the
  bumps are smaller everywhere.
- **Grades made too steep** went down or stayed at zero everywhere. Rio is still high (260 → 223),
  and so is Lauterbrunnen (37 → 34). The cells that fall back to stage 1 only hold the passes'
  own steepest grade, not the class ceiling.
- **Low decks**: Tokyo 72 → 60. The others are unchanged, because those decks are fixed by the
  passes' own dips (the underpass rule), which the solve takes as given. But more roads now pass
  through a deck's body (under 2.5 m from its surface): Tokyo 6 → 8 and Rio 0 → 1, while Lagos
  goes 2 → 1. Rio's worst gap is also lower, 5.5 → 6.1 m under the 8.35 m clearance. Without
  clearance rows, the solve can pull a road up towards a deck. This is a regression to fix before
  step 3.
- **CPU**: the right-hand column is the solve alone. The passes still run before it, to make its
  inputs (z0, the deck ends and the dips), so this is not a saving yet. It shows the order of cost
  a solve-only path would have: 1.1-81 s per place, with the worst cell at 10.7 s (Tokyo).

## Stages and fallbacks

Each cell first tries the class grade ceilings (stage 0). If that is infeasible, it retries with
floors at the passes' own steepest grade (stage 1), and then with no grade rows at all (stage 2).
If none of these is feasible, it falls back to the passes' heights, and `_culprit` names which kind
of row made it infeasible.

- 45 of 50 cells solved at stage 0, and 5 at stage 1 (Tokyo 4, Rio 1). None needed stage 2 or a
  fallback.
- 7 cells still have roads over their grade ceilings after the active-set rounds ("grade_left":
  Tokyo 4, Rio 2, Lagos 1).

## What is still missing before step 3 (the solve writes the plans)

1. **Soft grade rows.** The kernel only knows hard rows, so a ceiling that can't hold makes the
   whole stage infeasible. A slack per row (an L1 penalty) needs a change in earthfast's
   `carriageway_profiles`. That would replace the stages.
2. **Seams.** Either solve the cut ends with the next cell's solve (a shared seam value) or keep
   the current pins and accept about 30 cm. The RFC's plan is the shared seam value.
3. **Amsterdam's stacked junction** (above).
4. **Decks**: the solve takes the passes' dips as given. Moving the underpass rule into the solve
   (clearance rows) is what would bring the low decks down. Today it only keeps them.
5. **Cost**: the solve reuses the passes' outputs. Removing the passes, which is the speed gain,
   only comes once the solve's own inputs (deck ends, dips) are made without them.

The shadow changes nothing in the plans. The grading tests pass with it on or off (116 passed,
12 skipped), and `server/tests/test_vsolve.py` covers the solve on plans made by hand.
