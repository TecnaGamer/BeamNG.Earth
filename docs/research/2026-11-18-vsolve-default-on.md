# The one solve on by default (2026-11-18)

This is the RFC's step 4, first half. With `BNE_VSOLVE` unset, the planner now uses the solve's heights wherever the
native kernel has the solve (`vsolve.KERNEL_MIN`, earthfast 490). On an older build it stays off, as before: the
plans are the passes' either way, but with the mode on they would be cached under the solve's key
(`plancache.plans_format`, `.vs1`) and kept after the kernel was rebuilt. `BNE_VSOLVE=off` gives the passes alone.
The passes themselves stay: removing them, one PR each, is the second half.

Before switching, the whole suite ran in on mode. Three kinds of test failed: two of test_grading's underpass
cases, test_under_deck's road under a deck, and 22 of test_drivability's sites. The first two are fixed below.
test_drivability's ratchet holds every site to a locked baseline (fixtures/drivability_baseline.json): any change,
better or worse, fails until the baseline is rewritten. This note fixes what made sites worse where the solve was
missing a rule of the passes, then rewrites the baseline in on mode.

## Two rules of the passes the solve was missing

Found on test_drivability's sites, each fixed in the solve alone:

- **Past a dip's ramp, no lower than the underpass rule.** Smoothed over its class's spread, a motorway under a held
  overpass came back up 120 m past the ramp's foot and was still 0.11 m low 250 m from the deck
  (test_grading's underpass cases). Past the ramp's foot, a road now has a floor at the rule's height, no higher than
  the passes' (at the rule's height alone, a Südtiroler Platz service road stood 5 m over the junction its dip took
  down). With this, test_grading and test_under_deck pass in on and solo modes.
- **A junction by a dip is no higher than the dip.** A junction of four primary roads on Südtiroler Platz stayed at
  its profile, held there by all four, 0.27 m over the dip on one of them 4 m on. The underpass rule takes every road
  at a junction to the deepest dip within 12 m (`UNDER_JOINT_FLAT_M`). The solve now caps a junction there
  (`vsolve.JOINT_FLAT_M`, soft, as the caps under decks), except on the coarse model, where the rule keeps a dip
  between a street's junctions (`UNDER_RUN_ON`; test_grading's Samara case).

## One rule tried and left off: the snap's reach

Silistra's track ends 1.6 m off the next track's centre line. The solve joins ends within 1 m (`JOIN_M`), so it holds
the track at its profile, 3.4 m over the other track where the dip under the bridge takes that one down (`step_m`
0.17 → 3.44, score 1.5 → 5.8). The passes' snap joins an end within the road's half width plus 3 m
(`JUNCTION_REACH_M`).

`vsolve.joins` can now do the same (`halves`, `JOIN_REACH_M`), for an end that meets no line within 1 m and no end
within 2.5 m, at most 0.5 m off that road's profile (`JOIN_REACH_DZ_M`), and never an end cut at the cell's box. It
takes Silistra's step away (5.8 → 1.4). But Tokyo's cells then join some ends differently: 4 seams over 30 cm, up to
1.87 m, where there are none without it. Neither the height limit nor leaving out the cut ends removed them. So the
solve does not use it by default (`BNE_VSOLVE_REACH=on` turns it on). Without it the drivability total is also lower
(1281.6 against 1296.7 with it). Silistra's step is left for a later PR.

## test_drivability in on mode

`python server/tests/drivability.py` with the code of this PR (score: lower is better; the passes are the old
baseline):

| site | on | passes | change |
|---|---|---|---|
| compiegne_roundabout | 17.2 | 89.4 | −72.2 |
| m1_ireland_960 | 37.0 | 69.7 | −32.7 |
| castlegar_939 | 15.7 | 33.4 | −17.7 |
| perth_915 | 27.5 | 44.9 | −17.4 |
| boston_bowker | 73.9 | 79.6 | −5.7 |
| paris_maillot_ax_1083 | 118.6 | 123.6 | −5.0 |
| vienna_s1_rautenweg_1083 | 48.4 | 53.1 | −4.7 |
| dc_capitol_crossing_1083 | 42.5 | 45.9 | −3.4 |
| riva_gardesana | 17.6 | 20.6 | −3.0 |
| vienna_kennedy_1083 | 36.8 | 37.6 | −0.8 |
| vienna_a23_1083 | 44.7 | 45.5 | −0.8 |
| casagranda_bridge | 39.2 | 39.8 | −0.6 |
| bologna_929 | 1.5 | 1.7 | −0.2 |
| tamar_bridge_1112 | 21.1 | 20.7 | +0.4 |
| italy_967 | 3.0 | 2.5 | +0.5 |
| vienna_s1_breitenleer_1083 | 105.1 | 104.5 | +0.6 |
| boston_storrow_massave | 26.7 | 26.1 | +0.6 |
| vienna_mooslacken_1083 | 10.4 | 9.3 | +1.1 |
| prague_924 | 53.6 | 52.1 | +1.5 |
| silistra_936 | 5.8 | 1.5 | +4.3 |
| vienna_suedtiroler_1083 | 128.9 | 123.3 | +5.6 |
| paris_maillot_1083 | 316.0 | 303.7 | +12.3 |
| the other 7 sites | 90.4 | 90.4 | 0 |
| **total** | **1281.6** | **1418.9** | **−137.3 (−9.7 %)** |

- **Better at 13 sites, worse at 9.** The four biggest changes are gains: Compiègne's roundabout −72, the M1 −33,
  Castlegar −18, Perth −17.
- **The three worst, not fixed here:**
  - Porte Maillot (+12.3) is roads on top of one another. One trunk's carriageway spans 7 m of height across its
    width (39 m on one side, 31.6 m on the other, where another trunk runs under it). Its biggest new defect, a 1.85 m
    hole, is which of the two roads takes the ground at that metre, not a profile of the solve. The site's report
    already says every version gives "a different mess" there.
  - Südtiroler Platz (+5.6) is the lid over a trench: primary roads round the lid's junctions bent more than the
    passes bend them (`lift_g`).
  - Silistra (+4.3): the snap's reach above.
- **The baseline is rewritten in on mode** (`drivability.py --update`), so the ratchet holds the solve from here.

## The 8 baseline places

`BNE_REVISIT=off BNGE_PLANCACHE=off BNE_VSOLVE=<mode> python tools/grade_metrics.py --tiles 1`, today. Each cell:
passes / on before this PR (2026-11-18-vsolve-corridors.md's code, with the floors past the ramp) / on / solo.

| place | junction ≥ 30 cm | worst seam (m) | made steep | ground p95 (m) | corridor ≥ 30 cm |
|---|---|---|---|---|---|
| Fells Point | 28 / 0 / 0 / 0 | 0.13 / 0.02 / 0.02 / 0.02 | 12 / 6 / 6 / 6 | 0.24 / 0.22 / 0.22 / 0.22 | 13 / 13 / 13 / 26 |
| Lauterbrunnen | 3 / 0 / 0 / 0 | 0.24 / 0.03 / 0.03 / 0.03 | 37 / 32 / 32 / 32 | 0.49 / 0.47 / 0.47 / 0.47 | 0 / 0 / 0 / 0 |
| Amsterdam | 5 / 5 / 5 / 5 | 0.05 / 0.02 / 0.02 / 0.02 | 3 / 1 / 1 / 1 | 0.16 / 0.17 / 0.17 / 0.17 | 2 / 2 / 2 / 2 |
| Lagos Island | 6 / 0 / 0 / 0 | 0.14 / 0.06 / 0.06 / 0.06 | 25 / 15 / 15 / 15 | 0.37 / 0.37 / 0.37 / 0.37 | 178 / 254 / 254 / 260 |
| Tokyo Shibuya | 66 / 8 / 9 / 9 | 0.63 / 0.21 / 0.21 / 0.21 | 156 / 96 / 98 / 100 | 0.29 / 0.28 / 0.28 / 0.28 | 507 / 813 / 816 / 956 |
| Punjab farmland | 0 / 0 / 0 / 0 | 0.05 / 0.01 / 0.01 / 0.01 | 0 / 0 / 0 / 0 | 0.22 / 0.21 / 0.21 / 0.21 | 0 / 0 / 0 / 0 |
| Rio Botafogo | 58 / 1 / 1 / 1 | 1.42 / 0.55 / 0.55 / 0.55 | 260 / 224 / 224 / 224 | 1.15 / 1.13 / 1.13 / 1.13 | 171 / 196 / 184 / 205 |
| Athens Omonia | 31 / 1 / 1 / 1 | 0.17 / 0.05 / 0.05 / 0.05 | 86 / 31 / 30 / 32 | 1.32 / 1.49 / 1.49 / 1.68 | 998 / 999 / 944 / 1088 |
| **total** | **197 / 15 / 16 / 16** | seams ≥ 30 cm: **10 / 1 / 1 / 1** | **579 / 405 / 406 / 410** | | **1869 / 2277 / 2213 / 2537** |

- **The two rules change almost nothing here:** one more junction step (Tokyo, 8 → 9), 64 fewer corridor steps
  (Athens 999 → 944, Rio 196 → 184). Roads through decks: 16, against the passes' 17.
- **Against the passes:** junction steps 197 → 16, seams ≥ 30 cm 10 → 1 (Rio's 0.55 m, where the passes leave
  1.42 m), roads made steep 579 → 406. Steps between roads side by side: 1869 → 2213 (+18 %). Athens' ground p95:
  1.32 → 1.49 m.
- With the snap's reach on, Tokyo had 4 seams over 30 cm (up to 1.87 m) and 18 junction steps, and Athens 5 roads
  through decks.
- CPU: not compared here (the runs shared the machine with other runs).

## What this PR does not do

- **The drive in the game** (the RFC's step 3) has not been done. The user's order is that it comes last.
- **The passes stay.** They still run in on mode: the solve takes its rows from their rules and falls back to their
  heights where its hard rows are infeasible. Removing them is the RFC's step 4, second half, one PR each.
- **The kernel:** the solve needs earthfast 490. A build without it plans with the passes alone, as before.
