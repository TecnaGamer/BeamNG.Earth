# The one solve's CPU (2026-11-19)

The RFC's condition for replacing the passes: the solve "no worse on every metric at every place" and "its CPU
stays within today's per cell" (`docs/VERTICAL_SOLVER_RFC.md`, Validation). The default-on note
(`2026-11-18-vsolve-default-on.md`) did not measure the CPU: its runs shared the machine with other runs. Measured
here alone, with nothing else running, on the 8 baseline places:

`BNE_REVISIT=off BNGE_PLANCACHE=off BNE_VSOLVE=<mode> python tools/grade_metrics.py --tiles 1`: planning CPU (the
threads' CPU in `_make_cell`, its road stages and everything they call), seconds.

| place | passes (off) | solve (on) |
|---|---:|---:|
| Fells Point | 13.6 | 65.4 |
| Lauterbrunnen | 5.5 | 7.5 |
| Amsterdam | 53.0 | 59.2 |
| Lagos Island | 14.1 | 47.5 |
| Tokyo Shibuya | 201.9 | 535.4 |
| Punjab farmland | 3.3 | 8.7 |
| Rio Botafogo | 6.6 | 20.3 |
| Athens Omonia | 15.6 | 56.2 |
| **total** | **313.4** | **800.3** |

One run each, before this PR's changes. The passes' run was slowed at Amsterdam (53 s where every later run gave
19 s, with nothing changed on the passes' path): without it they took about 280 s.

So at the start of this PR, the solve on took 2.55 times the passes' CPU. That is far from the RFC's condition.

## Where it went

`cProfile` over each thread's `_make_cell` (Fells Point, then Tokyo):

- **Building the problem**: `vsolve.problem` was built again in each of the solve's rounds (2 to 3 a cell), and
  each time it found the junctions again (`nodes`, `joins`: 45,000 nearest-line searches in Python), the plans'
  steps and their ceilings. 16 of the solve's 44 s at Fells Point.
- **The kernel**: the chains carried grade and curvature bounds never reached (`GRADE_D`: 1000 %, `K_LOOSE`: tens
  of metres a step). Clarabel carried them all the same: 4 inequality rows a sample. 17.5 s at Fells Point (12
  calls, 1.5 s each).
- **The caps under decks** (`_vsolve_caps`): every sample of a plan against every segment of every deck whose box
  met the plan's box. Tokyo's plans run 400 m past the box, and the Shuto's viaducts have hundreds of segments: 137
  s of Tokyo's 386 s in that function's own lines, more in the distances it asked for.
- **The context** (`_vsolve_context`, the next cells' roads near the box): every next cell placed all of a cell's
  roads again to find those near its box (36,510 rings at Fells Point, 3 s), then planned the near ones with their
  pins (`_pins`: 10 s at Fells Point).

## What this PR changes

Each change gives the same heights. The 8 places' metrics are identical after each, but for Lagos, where two seams
are named from the other cell first (which cell's road is `a`), from the order the threads planned the cells in.

- `vsolve.solve` finds the junctions and each plan's steps and ceiling once, for all its rounds, `_joint_caps` and
  `_culprit` (`problem(..., node_list, pre)`).
- `grademetrics._Segments.near` computes a query's candidates at once, with the same operations as one by one
  (`near_scalar` stays as the tests' reference). 3 times faster, the same answers to the bit, in the same order.
- `_vsolve_context` keeps each cell's placed road rings (`_vsolve_cell_rings`, 64 cells).
- **earthfast 563**: `carriageway_profiles` takes a chain grade of +inf and a K of 0, which mean no bound and no
  rows. vsolve passes them on a 563 kernel (`vsolve._LOOSE`) and keeps the old bounds on an older one. These bounds
  were never reached, so the optimum is the same: the 8 places' metrics identical, test_drivability 1281.4 (it was
  1281.5). The kernel's calls at Fells Point: 1.5 s -> 0.2-0.6 s each.
- `_vsolve_caps` measures only the samples in a deck's box widened by the road's half width. A sample outside it is
  farther from the deck's line than both half widths. Each sample's row is computed alone, so the bits are the same.

## After

Two runs each, alternated (off, on, off, on), nothing else on the machine:

| place | off 1 | off 2 | on 1 | on 2 |
|---|---:|---:|---:|---:|
| Fells Point | 13.0 | 14.2 | 33.6 | 28.5 |
| Lauterbrunnen | 2.9 | 2.8 | 4.7 | 4.4 |
| Amsterdam | 18.9 | 19.2 | 37.6 | 37.6 |
| Lagos Island | 11.0 | 11.1 | 23.6 | 22.9 |
| Tokyo Shibuya | 179.4 | 177.5 | 338.0 | 305.1 |
| Punjab farmland | 3.2 | 3.0 | 7.2 | 5.9 |
| Rio Botafogo | 6.0 | 4.5 | 12.0 | 10.3 |
| Athens Omonia | 14.4 | 13.3 | 33.1 | 28.0 |
| **total** | **248.8** | **245.6** | **489.9** | **442.6** |

The solve on: 466 s against the passes' 247 s, 1.89 times (it was 2.55 times, about 2.9 against the passes'
undisturbed 280 s). Along the way (one run each): 800 s, 584 s after the problem built once and the vectorized
`near`, 530 s with the 563 kernel, 491 s with the caps' prefilter. The metrics (junctions, seams, grades, ground,
corridors) are the same in every one of them, place by place (`2026-11-19-vsolve-cpu.fin_on.json`: the second on run).

## What is left

The RFC's condition is not met: the solve still costs about 0.9 of the passes again. What is left, from the
profiles (Tokyo, where 2/3 of the 8 places' time is):

- **Not the passes.** The 247 s of off mode are mostly the planning every mode does (the profiles, the decks, the
  chains); the passes the solve replaces are a small part of them. Solo mode (`BNE_VSOLVE=solo`: those passes
  skipped) took as long as on mode, measured after dev's 560 merge: Tokyo 358 s against 366, the 8 places 506 s
  against 517 in the same session. Removing the passes (the RFC's step 4) will not bring the CPU down by itself.
- **The underpass rule runs twice**: once in the passes, once in the shadow over the solve's whole margin (its
  `near` is 400 m past the box, not UNDER_NEAR_M). The cells, spans and ground it gathers are nearly the same; kept
  once they would be found once.
- **The context's pins** (`_pins` on the next cells' roads): each next cell's roads planned again with their
  pins, for every cell next to it.

## Tried and dropped

- **Grade rows in the first round** wherever a row moves the solve (junctions, held ends, dips, caps, floors):
  in a city every road has a junction, so 30 times the soft rows. The kernel took twice as long a call, and the
  rounds were needed all the same.
- **Splitting the solve by connected pieces**: one piece holds 95 % or more of the unknowns at Fells Point (a
  city's streets all meet), so there is nothing to split.
