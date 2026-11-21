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
