# Clearance under and onto decks in the one solve (2026-11-16)

This follows the soft rows note (2026-11-16-vsolve-soft-rows.md) and does the first item on its
"Next" list. With soft rows, the solve put more roads through decks than the passes did (Tokyo 6 → 8,
Rio 0 → 1). It took the underpass rule's dips as given and nothing else about the decks. Two kinds of
rows now carry what the passes know about them. All the numbers are in
`2026-11-16-vsolve-clearance.json`.

    python tools/build_native.py --fast     # earthfast 373 (the committed modules are 372)
    BNE_REVISIT=off BNGE_PLANCACHE=off python tools/grade_metrics.py --vsolve --tiles 1 --out build/vsolve.json

The code is vsolve/soft-rows merged with PR #1344's speed fixes. It ran on a local development build
of the kernel (MSVC, no LTO): 8 places, 13.4 min wall.

## What changed

- **Kernel** (earthfast 373, the same version): a coupling may be an upper bound ("le"), and it may
  have no second sample (one sample against a constant). The Rust test for this case holds a cap on a
  single sample, hard and soft.
- **Caps under decks.** A road that runs under a road or rail deck gets a cap on each sample under it
  (within the deck's half width of its line, and more than 6 m from its ends). The cap is the lower of
  the underpass rule's height there and the passes' final. A later pass lowered a road under Rio's
  205 m deck by 0.66 m after the rule. Capped at the rule's height alone, the solve put it back there:
  2.3 m under the deck where the passes left 2.8 m.
- **Floors onto decks.** Where `_lift_approaches` raised a road by 0.3 m or more, that is its floor.
  The floor is bounded by what the underpass rule after it left and by the passes' final. Without
  floors, the solve left a road leading onto a Tokyo deck on its ground: 1.3 m under the ramp the
  passes gave it, and 1.07 m under the deck beside it.
- **Cut ends on their floor.** A road's end past the cell's box is held at its profile, as before,
  but no lower than its floor there.
- Caps and floors are soft rows at 10 times the grade rows' penalty. Each cell reports how many
  broke, by how much, and where the worst one is (`clear_broken`, `clear_excess_m`, `clear_worst`).

## Two inputs that contradicted each other

The first floors measured broke 46 rows, by up to 8 m. All of them were in one of two cases.

- **A floor over a dip** (Rio -22.95005, -43.17804; Athens 38.05491, 23.74636). The approach rule
  raised the road by up to 3.6 m. The underpass rule, which runs after it and sees the lift, dipped
  the road 6.4 m under a deck. `ramp_envelope` then raised it again in the passes, back onto the
  deck's height. The passes resolve this by order, and in Rio the last rule wins with a road at its
  deck's height. In the solve, the dip is a hard row and the floor a soft one, so the floor broke by
  the dip's depth. The floor is now no higher than the underpass rule's height. The solve keeps the
  dip, which is the clearance the rule asked for.
- **A floor against a cut end** (Tokyo 35.64964, 139.68050). The road's end, past the box, was held
  at its profile, 22.35 m. The approach rule had raised that end to 27.43 m, toward a deck in the next
  cell. That is 5 m to come down in 4 m. Between the two, the smoothing overshot and stood the whole
  road 4 to 8 m over its ground. The cut end is now held on its floor. This is the same rule as for a
  dip there, which was already taken into account.

## Results (passes / caps only / caps and floors)

| place | junction ≥ 30 cm | seams ≥ 30 cm | made steep | ground p95 (m) | low decks (through) | grade rows broken | clearance rows broken |
|---|---:|---:|---:|---:|---:|---:|---:|
| fells_point | 28 / 0 / 0 | 0 / 0 / 0 | 12 / 5 / 5 | 0.24 / 0.22 / 0.22 | 0 / 0 / 0 | 4 / 4 | 0 / 0 |
| lauterbrunnen | 3 / 0 / 0 | 0 / 0 / 0 | 37 / 34 / 32 | 0.47 / 0.44 / 0.46 | 0 / 0 / 0 | 0 / 1 | 0 / 0 |
| amsterdam | 5 / 5 / 5 | 0 / 0 / 0 | 3 / 1 / 1 | 0.16 / 0.17 / 0.17 | 0 / 0 / 0 | 0 / 0 | 0 / 0 |
| lagos_island | 6 / 0 / 0 | 0 / 0 / 0 | 25 / 5 / 6 | 0.37 / 0.36 / 0.37 | 38 (2) / 30 (1) / 30 (1) | 28 / 38 | 0 / 0 |
| tokyo_shibuya | 120 / 28 / 28 | 1 / 1 / 1 | 164 / 54 / 69 | 0.29 / 0.27 / 0.29 | 56 (6) / 49 (8) / 53 (6) | 51 / 101 | 2 / 6 |
| punjab_farmland | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 1 / 0 | 0.22 / 0.21 / 0.21 | 0 / 0 / 0 | 0 / 0 | 0 / 0 |
| rio_botafogo | 62 / 1 / 1 | 9 / 3 / 3 | 260 / 215 / 218 | 1.15 / 1.07 / 1.11 | 3 (0) / 3 (0) / 3 (0) | 15 / 32 | 0 / 0 |
| athens_omonia | 43 / 1 / 1 | 0 / 0 / 0 | 74 / 22 / 20 | 1.08 / 1.08 / 1.42 | 3 (1) / 3 (1) / 3 (1) | 0 / 1 | 0 / 0 |

"Low decks" counts the decks with less than the clearance they need over a road. "Through" counts
the roads that pass through a deck.

The roads through decks are back to the passes' count: Tokyo 6 and Rio 0. Junctions, seams and made steep stay well under the
passes everywhere: 351 roads made steep against 575.

## What the floors cost

- **Ground p95 in Athens, 1.08 → 1.42 m.** The floors raise the same roads, to the same heights, as
  the passes did. That is the largest lift per road, up to 9.8 m: real ramps onto decks, which the
  solve without floors left on their ground. The solve then keeps each ramp under its class's grade
  ceiling. A 9.8 m lift at 9 % needs over 100 m of ramp, where the passes climbed steeper: over
  ceiling 175 / 128 / 125.
- **Tokyo, made steep 54 → 69 and grade rows broken 51 → 101.** Where a ramp's foot is held (a node,
  a deck end, a cut end), it cannot lengthen, and its grade breaks instead. It is still well under the
  passes' 164.
- **Lagos, the largest pitch change ("bump max", the second difference of the grade: not a step in
  m) 0.23 → 1.31** (the passes have 3.34), and ground max 7.5 → 8.8 m. Both are on one road (6.43185,
  3.40885), the one the 160 % was on. It is 26 m long, from a junction on its ground at 4.43 m to its
  own deck's end at 12.03 m: 29 % over an 18 % ceiling, a contradiction in the inputs. The approach
  rule raises every sample but the junction's by 6.4 m, and the passes keep that as a 6.4 m step in
  3.7 m. Without floors, the solve left the road on its ground under its own deck's end. With them,
  it raises the junction by 2.3 m and overshoots the deck's end by 1.3 m (13.32 m). The kernel's
  tension term (`length`, tried at 0.1 to 3 times the hold at the spread) changes that by under
  0.12 m, and makes it worse, so it is not the fix here. `grade_metrics` now reports where the worst
  pitch change and the largest distance from the ground are (`bump.worst`, `ground.worst`, with
  lat/lon).
- The 6 clearance rows still broken in Tokyo are under 0.4 m. The worst one is a single sample 0.46 m
  over its neighbours, which is both a floor and a cap in the passes, and the solve evens it out.

## Next

1. Seams: one value shared by both cells at a cut end.
2. The overshoot past a held point. The tension term did not help on Lagos' road. Try it on
   `test_vsolve`'s case (1.2 m past a pin) before changing anything.
3. Amsterdam's stacked junction (52.36678, 4.87054).
4. The RFC's step 3 (opt-in): the caps and floors come from the passes' heights now. Without the
   passes (step 4), they will have to come from the decks' own heights.
