# Soft grade rows in the one solve (2026-11-16)

This follows the shadow note (2026-11-16-vsolve-shadow.md) and does the first item on its "still
missing" list.

With hard grade ceilings, one ceiling that could not hold made the whole cell's solve infeasible. The
cell was then solved again with looser ceilings, and then with none: three stages, each a rule about
what to give up. Now a ceiling is a soft row (earthfast 373: one slack per row with an exact L1
penalty). It holds wherever the hard rows leave room, and it is broken by the least where they
don't. Each cell reports how many of its rows broke, by how much, and where the worst one is
(lat/lon). All the numbers are in `2026-11-16-vsolve-soft-rows.json`.

    python tools/build_native.py --fast     # earthfast 373 (the committed modules are 372)
    BNE_REVISIT=off BNGE_PLANCACHE=off python tools/grade_metrics.py --vsolve --tiles 1 --out build/vsolve.json

Code: vsolve/soft-rows merged with PR #1344's speed fixes. A local development build of the kernel
(MSVC, no LTO), 8 places, 13.1 min wall. Two runs gave identical numbers.

## What changed

- **Kernel** (`carriageway_solver.rs`): a coupling with a penalty above 0 gets a slack. A "ge" row
  becomes z_a − z_b + s ≥ v with s ≥ 0. An equality becomes |z_a − z_b| ≤ s. The cost is the penalty
  times s. `carriageway_profiles` takes an optional `coupling_penalty`; without it, every coupling is
  hard, as before. The diagnostics report `soft_excess_m`, `soft_broken` and `soft_sum_m`. There are
  3 new Rust tests: soft rows that can hold give the hard solve's heights; soft rows that can't are
  broken by the least; a soft equality breaks where the holds leave no room. VERSION goes to 373, and
  `fast.MIN_VERSION` stays where it is, so the server still starts on today's modules. Only the shadow asks
  for 373.
- **vsolve**: grade rows are soft, and the penalty scales with the road's stiffness (hold × samples
  over its spread). There is one solve with an active set, and no more stages, floors or "loosened".
- **The shadow's dips**: they now come from the underpass rule run on the heights it sees in the
  passes (just before `_lower_underpasses`), not on the profile before any pass. Found with the new
  report: in Lagos (6.4318, 3.4086), a road the passes had raised onto its bridge's approach (11.85 m
  over 4.5 m of ground) got a dip at 4.20 m from the rule, 10 m from its own deck's end at 12.03 m.
  That is 7.8 m to climb in 10 m, 160 % over an 18 % ceiling.

## Results (passes → shadow with hard rows → soft rows)

| place | junction ≥ 30 cm | seams ≥ 30 cm | made steep | ground p95 (m) | low decks (through) | rows broken | fallback |
|---|---:|---:|---:|---:|---:|---:|---:|
| fells_point | 28 / 0 / 0 | 0 / 0 / 0 | 12 / 4 / 4 | 0.24 / 0.22 / 0.22 | 0 / 0 / 0 | 1 | 0 |
| lauterbrunnen | 3 / 0 / 0 | 0 / 0 / 0 | 37 / 34 / 34 | 0.47 / 0.44 / 0.44 | 0 / 0 / 0 | 0 | 0 |
| amsterdam | 5 / 5 / 5 | 0 / 0 / 0 | 3 / 0 / 0 | 0.16 / 0.17 / 0.17 | 0 / 0 / 0 | 0 | 0 |
| lagos_island | 6 / 0 / 0 | 0 / 0 / 0 | 25 / 4 / 3 | 0.37 / 0.35 / 0.36 | 39 (2) / 39 (1) / 39 (1) | 7 | 0 |
| tokyo_shibuya | 120 / 28 / 28 | 1 / 3 / 3 | 164 / 77 / 55 | 0.29 / 0.27 / 0.27 | 72 (6) / 60 (8) / 60 (8) | 50 | 0 |
| punjab_farmland | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0.22 / 0.21 / 0.21 | 0 / 0 / 0 | 0 | 0 |
| rio_botafogo | 62 / 1 / 1 | 9 / 3 / 3 | 260 / 223 / 215 | 1.15 / 1.06 / 1.07 | 3 (0) / 3 (1) / 3 (1) | 15 | 0 |
| athens_omonia | 43 / 1 / 1 | 0 / 0 / 0 | 74 / 22 / 22 | 1.08 / 1.06 / 1.07 | 3 (1) / 3 (1) / 3 (1) | 0 | 0 |

Totals: roads made steep 575 → 364 → 333, with the rest unchanged. Before, a cell that fell to stage
1 or 2 gave up its class ceilings everywhere. Now it gives them up only on the broken rows (Tokyo
77 → 55).

Tokyo's worst seam is 0.37 m with soft rows, against 0.32 m with hard ones, and the count of seams
of 30 cm or more is still 3.

## Where the ceilings still break

| cell | rows | worst | lat, lon | what it is |
|---|---:|---|---|---|
| Tokyo 14/14549/6453 | 7 | 78 % over 12 % | 35.64699, 139.69667 | a 2.4 m road between two bridges' ends at 10.74 m and 8.86 m. Both are deck heights (inputs), and the passes have the same step. |
| Tokyo 14/14546/6454 | 5 | 74 % over 12 % | 35.62798, 139.61644 | not looked at yet |
| Tokyo 14/14550/6454 | 2 | 44 % over 12 % | 35.63460, 139.71532 | not looked at yet |
| Tokyo 14/14547/6454 | 1 | 34 % over 12 % | 35.62422, 139.64861 | not looked at yet |
| Lagos 14/8347/7898 | 7 | 28 % over 18 % | 6.43184, 3.40867 | 4 m from where the 160 % was, probably the same road; not looked at again |
| Rio 14/6226/9264 | 11 | 22 % over 14 % | -22.92034, -43.19362 | not looked at yet |
| Tokyo 14/14549/6452 | 7 | 18 % over 9 % | 35.66865, 139.68783 | not looked at yet |
| Tokyo 14/14550/6452 | 28 | 13 % over 8 % | 35.65823, 139.70737 | not looked at yet |
| Fells Point 14/4705/6245 | 1 | 66 % over 64 % | 39.26662, -76.61474 | a steep street at its own ground's grade |
| Rio 14/6226/9265 | 4 | 9 % over 9 % | -22.95074, -43.17977 | under 1 % over |

The ones looked at are contradictions in the inputs, which the passes have as well. With the soft
rows, they come out as a place and a size, and the rest of the cell keeps its ceilings.

## Known limits

- **Overshoot past a held point.** The smoothing is on second differences, so a change carries past
  a pin like a spline: after a 6 m change it sags 1.2 m, about 30 m past the pin (the case in
  `test_vsolve`). The kernel
  already has a first-difference term (`length`, a tension spline) that vsolve leaves at 0. It is
  the lead to try.
- **Timing**: the solve's CPU here is up to 1.7 times that of the hard-row run (Tokyo 81 → 117 s,
  Athens 15 → 26 s). The passes' CPU went up by the same ratio (Tokyo 264 → 383 s, Athens 36 → 62 s),
  because both run on the development build of the module.
  Soft rows don't cost more by themselves.
- The committed modules (`server/earthfast.pyd`, `server/earthfast.abi3.so`) are not rebuilt. That is
  the maintainer's release build. Without 373, `test_vsolve`'s solve tests are skipped.

## Next

1. Clearance under decks as rows in the solve (it takes the underpass rule's dips as given now). That
   is what would bring down the low decks, and the roads through decks (Tokyo 6 → 8, Rio 0 → 1).
2. Seams: one value shared by both cells at a cut end.
3. The overshoot: try the tension term.
4. Amsterdam's stacked junction (52.36678, 4.87054).
