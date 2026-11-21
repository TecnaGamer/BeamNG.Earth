# The one solve without the passes it replaces: BNE_VSOLVE=solo (2026-11-17)

Step 4 of docs/VERTICAL_SOLVER_RFC.md is "default on, then the passes it replaces removed one PR at a time". In
on mode both still run, because the solve takes some of its rows from the passes' heights. `BNE_VSOLVE=solo` is
an experiment that measures what removing them would cost. It is off by default and changes nothing otherwise.

What solo mode keeps and what it skips:

- **Kept, the rules the solve takes its rows from:** the approaches' lifts (`_lift_approaches`, the floors) and
  the carriageways (`_apply_carriageways`). The underpass rule runs once, inside the solve's own margin, as it
  already did in shadow and on.
- **Skipped, the passes the solve replaces:** `couple_neighbours`, the junction snaps and pads, `_ease_coarse`,
  the passes' own underpass dips, `_pin_cw_ends`, `ramp_envelope`, `grade_guard`, `share_corridors` and
  `weld_continuations`.
- **Caps under decks:** in on mode they are the underpass rule's heights or the passes' final heights, whichever
  is lower. In solo mode the plans carry no passes, so they are the rule's alone.
- **A solve that falls back** keeps the plans as the kept rules left them, not the passes'. That is fine for a
  measure, and would not be for a release.

The plans are cached under their own format (".vs2").

    BNE_VSOLVE=solo BNE_REVISIT=off BNGE_PLANCACHE=off python tools/grade_metrics.py --tiles 1 --out build/solo.json

It ran on a local development build of the kernel (earthfast 373): 8 places. The numbers are in
`2026-11-17-vsolve-solo.json` ("shadow": the passes and the shadow solve; "on"; "solo").

## A deck chain that switched profiles

The first solo measure gave on mode's numbers everywhere, except at Tokyo: 104 low decks against 49, and 19 roads
through them against 14. At 35.63, 139.65 (the Shuto), 114 of the new ones were under two parts of one 76-part,
4 km chain (`5506483f…`, `bf0c8b0f…`). That chain stood 24.7 m off between the two modes, the same 24.7 m as in the
on-mode note.

- Replayed alone, the roads it crosses explain 0.15 m. The ground along its lines explains all of it, and a
  bisection found one point (439, 2082 in the region's frame): 1.46 m higher in solo.
- That point is not where the deck moves. `deck_profile_once` takes the chain's curve profile (`deck_curve`),
  and when it is steeper than `DECK_STEEP_GRADE` (15 %) anywhere, it also computes the ground envelope's
  profile. It then keeps whichever has the gentler step at the chain's free ends (`_end_step`). The two
  profiles of this chain are 23 m apart.
- Both profiles have a step of 93 % or more somewhere. `_end_step` measured an end on its first segment alone.
  Part 58's first segment is 0.59 m long, and 1 m of ground there gave the curve a 167 % step and the envelope
  213 %. The choice flipped from the envelope (on) to the curve (solo), and the whole 4 km chain went with it.

`_end_step` now measures an end's grade over its first `END_STEP_RUN_M` (5 m, or the part's length when
shorter). Both modes then keep the envelope for this chain, and they differ by 2.3 m, which is the ground's own
difference. `test_bridgeprofile`'s new `EndStep` case covers a short first segment. It is a change to the deck
solve and touches every bridge, so it is its own PR against dev (#1450). This branch carries the same commit so these
numbers can be reproduced.

With it, Tokyo in solo has 50 low decks (14 through), like on mode. The passes' numbers moved at Tokyo only, by
one deck (50 → 51), and `server/tests/drivability.py` improves at paris_maillot_1083 (−4.0), whose baseline is locked in.

## Results (passes / on / solo)

All three from this branch with the deck fix, in one run of each mode.

| place | junction ≥ 30 cm | worst seam (m) | made steep | ground p95 (m) | low decks (through) |
|---|---:|---:|---:|---:|---:|
| fells_point | 28 / 0 / 0 | 0.13 / 0.02 / 0.02 | 12 / 6 / 5 | 0.24 / 0.22 / 0.22 | 0 (0) / 0 (0) / 0 (0) |
| lauterbrunnen | 3 / 0 / 0 | 0.24 / 0.03 / 0.03 | 37 / 32 / 32 | 0.47 / 0.46 / 0.46 | 0 (0) / 0 (0) / 0 (0) |
| amsterdam | 5 / 5 / 5 | 0.05 / 0.01 / 0.01 | 3 / 2 / 3 | 0.16 / 0.17 / 0.18 | 0 (0) / 0 (0) / 0 (0) |
| lagos_island | 6 / 0 / 0 | 0.14 / 0.10 / 0.10 | 25 / 14 / 14 | 0.37 / 0.38 / 0.37 | 17 (2) / 16 (1) / 17 (1) |
| tokyo_shibuya | 120 / 28 / 28 | 0.63 / 0.23 / 0.57 | 164 / 78 / 77 | 0.29 / 0.29 / 0.29 | 51 (14) / 50 (14) / 50 (14) |
| punjab_farmland | 0 / 0 / 0 | 0.05 / 0.01 / 0.01 | 0 / 0 / 0 | 0.22 / 0.21 / 0.21 | 0 (0) / 0 (0) / 0 (0) |
| rio_botafogo | 62 / 1 / 1 | 1.42 / 0.55 / 0.55 | 260 / 218 / 220 | 1.15 / 1.11 / 1.12 | 3 (0) / 3 (0) / 3 (1) |
| athens_omonia | 43 / 1 / 1 | 0.17 / 0.05 / 0.05 | 74 / 20 / 22 | 1.08 / 1.42 / 1.58 | 3 (1) / 3 (1) / 3 (1) |

Without the passes, the solve is nearly on mode's at every place. What the passes still bring:

- **Tokyo, one seam** of 0.57 m against 0.23 (35.67180, 139.65820), and a second of 0.32 m beside it.
- **Athens, ground p95** 1.42 → 1.58 m: the floors' ramps onto decks again (the clearance note), now without
  the passes' heights to cap them.
- **Rio, one road** through a deck (0 → 1).
- **Made steep:** +1 to +2 at Amsterdam, Rio and Athens, and −1 at Fells Point and Tokyo.

The CPU is not compared here. The three runs went one after the other on a machine doing other work, and the
same mode's CPU moved by up to 20 % between runs. In the shadow run, the solve alone took a fifth (Amsterdam)
to two thirds (Rio) of the passes' CPU, a third at Tokyo (170 s against 524), and solo mode drops most of the
passes.

## Next

1. Tokyo's seam and Rio's road through a deck in solo mode.
2. The caps under decks from the decks' own heights (the deck chains' solved surfaces), rather than the
   underpass rule's estimate. That is the RFC's clearance row, and with it Athens' ramps would be capped by the
   deck they lead onto.
3. A fallback for solo mode: run the passes when the solve fails, as on mode keeps them.
