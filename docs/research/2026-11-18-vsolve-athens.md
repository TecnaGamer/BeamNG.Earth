# Athens' ground p95 under the one solve: one interchange under the A1 (2026-11-18)

In every mode of the one solve, Athens' ground p95 was the one metric worse than the passes'
(2026-11-17-vsolve-on.md and -solo.md: 1.08 → 1.42 m in on mode, 1.58 m in solo mode). This note finds where that
comes from. In short, it comes from one interchange, and there it is the passes that are wrong: they leave five roads
running through a motorway deck.

Measured on this branch after merging dev on 2026-11-18, with a local build of earthfast 442, using
`BNE_REVISIT=off BNGE_PLANCACHE=off BNE_VSOLVE=shadow python tools/grade_metrics.py --vsolve --tiles 1`. dev moved
the passes' own numbers: Athens' passes now have a ground p95 of 1.32 m, 31 junction steps and 86 roads made steep.

## Where the p95 comes from

| | passes | one solve |
|---|---:|---:|
| ground p95, every sample (m) | 1.32 | 1.42 |
| ground p95, without the interchange (m) | 0.98 | **0.89** |

Leave out the roads within about 400 m of one point, 38.0550 N 23.7453 E, and the solve's p95 is lower than the
passes'. These are 2,877 samples of 58,377.

## The interchange

The point is in Nea Filadelfeia, where the A1 (Athens–Thessaloniki) crosses over the Marmaron and Anagenniseos
avenues. In OSM the A1 there is two 4-lane bridges (`bridge=yes`, `layer=1`, ways 30033783 and 30033785), each
22 m wide and about 32 m long, with motorway and secondary links all round.

- **The underpass rule asks the most it allows.** The decks are never-raised overpasses, so `_lower_underpasses`
  dips the avenues under them by `UNDER_MAX_DROP_M` (8.0 m: 8.0 and 7.5 to 7.7 m at the four crossings). It then
  carries the dip through the junctions (`UNDER_JOINT_FLAT_M`, `UNDER_JOINT_FADE_M`): about 60 short roads go
  7 to 8 m under their ground.
- **The passes take most of it back.** `ramp_envelope` raises those roads again on ramps from the junctions that
  the approaches lifted toward the A1's links. One of them goes 124.3 m → 116.3 m (the rule) → 121.7 m
  (`ramp_envelope`), and the passes leave the avenues about 3 m under their ground.
- **The solve keeps the dips.** It takes the rule's depth as each dip's upper bound, and where an approach's floor
  meets a dip the dip wins (2026-11-16-vsolve-clearance.md, "a floor over a dip", which names this place). So the
  avenues stay 7 to 8 m down.

## Which one is right

The measure counts a deck only where the road tiles solve it, and the A1's decks are outside Athens' three tiles.
Centred on the interchange (`--at 38.05512,23.74573`), they are solved:

| | passes | on |
|---|---:|---:|
| junction steps ≥ 30 cm | 20 | 1 |
| seams ≥ 30 cm | 2 | 0 |
| made steep | 83 | 29 |
| low decks (roads through them) | **5 (5)** | **0 (0)** |
| ground p95 (m) | 2.28 | 2.67 |

With the passes, five roads run through the A1's deck. They are 1.0 to 1.2 m under its surface, so up to 6.7 m into
its body. The solve passes them under it. The 0.4 m of ground p95 is what that underpass costs, as the rule makes it.

## What this does not settle

The rule assumes the A1 stays at its approaches' heights (the deck ends are 0.1 m over their ground), so the whole
clearance comes from the dip. A real interchange like this one raises the motorway some way and dips the avenue
less. The solve could make that trade only with the decks' heights among its unknowns. That is the RFC's later
step: the caps under decks from the decks' own surfaces. Until then the solve does what the rule says, and the
passes do neither: they keep neither the dip nor the clearance.

So Athens' ground p95 is not a defect of the solve to fix. It is the one place where the "no worse on every metric"
criterion weighs a distance from the ground against a road through a deck. Measured where the decks are, the solve
is better on every metric except that one.
