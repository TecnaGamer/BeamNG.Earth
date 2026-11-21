# The passes the solve no longer needs (2026-11-21)

The RFC's step 4 removes the passes the one solve replaces. Solo mode (`BNE_VSOLVE=solo`) skips them all at once, and
it was no better than on mode (`2026-11-17-vsolve-solo.md`). Here each pass is skipped alone in on mode, to find the
ones that change nothing the solve keeps.

## Each pass alone

`BNE_VSOLVE=on BNE_VSOLVE_SKIP=<pass>`, grade_metrics on the 8 baseline places, against on mode without a skip (the
same code). Only the metrics that moved:

| pass skipped | what moved (on → skipped) |
|---|---|
| `neighbours`, `coarse`, `guard`, `struct_ends` | nothing: all four are off by default (`BNE_NEIGHBOURS`, `BNE_COARSE_EASE`, `BNE_GRADE_GUARD`, `BNE_DECK_STRUCT`) |
| `cw_ends` | nothing |
| `snap_dips` | better only: Tokyo made steep 104 → 103, corridor steps 819 → 813, Rio corridor 184 → 181 |
| `ramp` | Tokyo corridor 819 → 821, Rio 184 → 183 |
| `snap_weld` | Tokyo steep 104 → 102, corridor 819 → 826; Athens ground p95 1.487 → 1.496 |
| `weld` | ground p95: Punjab 0.213 → 0.214, Rio 1.131 → 1.133 |
| `under` | Athens ground p95 1.487 → 1.495 |
| `snap` | Tokyo corridor 819 → 889, Rio 184 → 190; Athens ground p95 1.487 → 1.65 |
| `corridors` | corridor steps everywhere: Tokyo 819 → 905, Athens 949 → 1046, Rio 184 → 196, Lagos 248 → 253 |

The CPU of these runs is not compared: they were spread over two hours, and the times moved by more than any one pass
costs (on mode without a skip: 490 s one run, 511 s the next).

## Skipped: `snap_dips` and `cw_ends`

Both together (`vsolve.SKIP`, default `snap_dips,cw_ends`): grade_metrics as `snap_dips` alone above (the same or
better), test_drivability 1281.4, every site unchanged.

Where the solve finds no heights (rows infeasible, an error), on mode now does as solo mode: the plans back to the
profiles before the passes and every pass run again (`grading`'s fallback). The plans' key gets `.k1`
(`vsolve.SKIP_TAG`; another list, its own tag). `BNE_VSOLVE_SKIP=none` runs every pass again.

The others stay: each makes something worse when skipped. `ramp`, `snap_weld`, `weld` and `under` by little; they are
the next to look at, with the solve given what they do.

## Where the rest of on mode's CPU is

A CPU-time profile of Tokyo in on mode with this skip (`time.thread_time`, every planner thread; 452 s under the
profiler):

| part | CPU |
|---|---:|
| the passes, all of them | 81 s |
| of which the underpass rule (`_lower_underpasses`, run in the passes and again in the shadow: 36 s for both) | ~18 s |
| of which the approaches' lifts (kept: the solve's floors) | most of the rest |
| of which `ramp_envelope`, `weld_continuations`, `share_corridors`, the snaps | under 6 s together |
| the solve's context (`_vsolve_context`: the next cells' roads planned as their cells plan them) | 78 s |
| of which `_pins` → `_stronger_zs` (the stronger roads those roads end on, planned) | 42 s |
| the solve (`vsolve.solve`: problem 19 s, joins 12 s, the kernel's carriageway solve 32 s) | 71 s |

So skipping the four passes left would save under 6 s, and it changes plans (above): they reach the solve through
its caps and floors, which are no higher than the passes' heights (`_vsolve_shadow`). Taking them out means caps from
the decks' own heights (the RFC's clearance rows), not a skip. The CPU is in the context's planning and the solve.
