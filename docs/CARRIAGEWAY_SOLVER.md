# Coupled carriageway height solver

The opt-in carriageway planner now solves its existing quadratic objective in
Rust, with hard linear constraints for grade, vertical curvature, height bounds,
junction equality and crossing clearance. Roads and bridge decks continue to
consume the same region profiles. Network discovery, class limits, OSM layer
ordering and bottom-up level staging remain in `server/grading.py`.

The previous penalty iteration could stop without satisfying its constraints.
For example, a feasible synthetic five-level corridor left an 11.14 m height
bound error, despite completing within its budget. A port of that iteration
would preserve the error. The new kernel keeps the objective and replaces the
penalty treatment with a constrained quadratic program.

## Numerical implementation

`server/carriageways.py` packs the inputs and validates per-chain array shapes.
The PyO3 boundary validates indices, dimensions and numerical parameters, owns
the inputs, and releases the GIL for the solve.

An O(n) bandwidth-two Cholesky solve handles profiles whose unconstrained optimum
already satisfies all constraints. Otherwise, Clarabel 0.11.1 solves a sparse QP
with its native Rust QDLDL backend. No Python optimizer or BLAS installation is
required at runtime. The objective has at most three upper-triangle entries per
sample; each constraint has at most three entries. There is no dense inverse or
dense matrix sized by the number of junctions. Factorization fill still depends
on the topology; sparse assembly does not imply a linear memory bound for every
possible network. One solver thread is used per request.

Diagnostics report the solver status, iteration count, elapsed time, sparse
matrix/factor sizes and independently measured constraint residuals. Grade and
curvature residuals use slope and inverse metres; height, junction and clearance
residuals use metres. `feasible` retains the planner's existing acceptance
tolerances (2% + 0.001 m per grade step, 5% + 0.0001 m per second difference,
0.02 m for heights and couplings); normal solved cases are substantially tighter.
`converged` additionally requires a solved/almost-solved optimizer status.

The region keeps its shared 30 s budget (`BNE_CW_BUDGET_S`), with a 150-iteration
cap per solve. Setup/factorization and individual optimizer iterations cannot be
preempted mid-operation, so this is a cooperative time limit. Exhausted or
infeasible results are explicit. Infeasibility certificates are never interpreted
as road elevations. The grading consumer falls back to its existing rules on an
unsolved region, marks the build incomplete, and does not persist its region or
dependent plans. Later reads of a failed memoized region also mark the build
incomplete. Cross-level junction pins are bounds rather than high-weight targets.

## Reproduction

Build the native module (API 153) before running the server tests:

```sh
python -m pip install scipy  # independent test reference only
python tools/build_native.py
python tools/run_tests.py test_carriageway_solver test_crossings test_bridge_clearance test_bridge_junction_surface test_bridgeprofile test_bridge_scenarios test_approaches test_deckjoins test_road_seams test_bake_formats test_plancache
python tools/benchmark_carriageways.py --repeat 5 --output build/carriageways.json
```

`test_carriageway_solver` enables the opt-in planner in its own test process. It
includes an independent dense unconstrained solution, analytic scalar/junction
solutions, three seeded comparisons against SciPy SLSQP, six larger synthetic
workloads, invalid inputs, redundant equations, infeasibility, budget exhaustion,
concurrent repeatability and grading/cache integration. SLSQP is a test reference,
not a runtime fallback. The module is included in `tools/quality_gate.py`.

To compare the old solver, use an unchanged checkout of `8a0165ce` (with its own
native module) and point the same workload generator at it:

```sh
python tools/benchmark_carriageways.py --server-root /path/to/baseline --repeat 5 --output build/carriageways-baseline.json
```

All benchmark inputs are deterministic synthetic arrays. They do not establish
survey accuracy, in-game collision quality or driving frame rate. The existing
bridge-junction tests also exercise the documented Swiss solved-geometry fixture;
that fixture is not an independent elevation survey.

## Offline measurements

Windows, Intel Core i5-10600, Python 3.10.11, NumPy 2.2.3, SciPy 1.15.3; native
`--release` build. Medians of five sequential server-only calls per workload,
without BeamNG or its audio/UI. The baseline is `8a0165ce`. Raw timings and residuals are in
[`research/2026-11-06-carriageway-solver.json`](research/2026-11-06-carriageway-solver.json).

| Synthetic workload | Samples / couplings | Baseline ms | Native ms |
| --- | ---: | ---: | ---: |
| Coarse profile | 501 / 0 | 0.630 | 0.094 |
| Long profile | 12,001 / 0 | 13.596 | 2.972 |
| 150 chains, shared junction | 12,150 / 149 | 35.350 | 140.579 |
| 150 chains, redundant junctions | 12,150 / 297 | 37.891 | 141.045 |
| Five-level corridor | 1,505 / 28 | 239.133 | 26.683 |
| Shallow overlap | 802 / 161 | 1,169.733 | 23.386 |

The constrained solver is slower on the two equality-only networks. The old
method already met the planner's acceptance tolerance there. This change does
not claim a speedup for every topology.

On the five-level corridor, the baseline had 11.1353 m maximum bound error,
0.134908 excess slope and 0.0108790 excess inverse-metre curvature. On the
shallow overlap it had 0.377641 m bound error and 0.000669611 excess curvature.
All five residual metrics of all six native results are below 1e-5, with bound
errors below 2e-14 m in these runs. The old and new times on failing baseline
cases are elapsed-workload comparisons, not equal-accuracy optimizer benchmarks.

The Release validation passed 182 tests across 11 solver/geometry/cache modules,
plus the Rust banded-factor unit test. An additional run of eight geometry
modules with `BNE_INTERCHANGE=1` passed (149 tests). The wider 39-module Python
foundation run attempted 669 tests: 37 modules passed; `test_cache` had two
Windows symlink privilege errors and all three `test_process_lifecycle` fixtures
timed out waiting for `guard.json`. Those same failures were observed before this
change. This is not a fully green foundation gate or a Linux/gameplay result.

## Cache and rollout

`PLANS_FORMAT` increases from 12 to 13 because opt-in profiles change. The default
bake fixture still produces `58caeaab0f182f42f4d8028923d80a873c45750d`, the same
digest observed on the unchanged baseline on this machine. The checked-in format
12 golden already differed from that baseline output; the format 13 golden was
refreshed explicitly. Facts format 4 and its digest are unchanged.

The feature remains opt-in through `BNE_INTERCHANGE=1` and `BNE_CARRIAGEWAYS=on`.
This change does not implement the complete proposal in #627, infer missing
topology/layer tags, impose tangent continuity across different chains or resolve
contradictory map constraints. No BeamNG runtime validation was available for
this contribution. Source builds and CI must rebuild earthfast for API 153;
generated native binaries are not included in this contribution.
