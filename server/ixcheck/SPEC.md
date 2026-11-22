# ixcheck: independent checks over the pipeline's cached artifacts (implementation spec)

The checks read the artifacts a run left in a cache directory (`BNE_IX_CACHE`, layout `s<step>/<xx>/<key>.npz`, see
`server/ix/SPEC.md`). They **never** call generation code.

**The import rule.** `ixcheck` imports only:
- `ix.artifacts` (the records, `load`);
- numpy, and `dnp` / `dmath` for deterministic maths;
- its own geometry helpers (`ixcheck/geom.py`).

It must not import `gradeshapes`, `grading`, `roads`, `railways`, `staged_*`, `interchange_audit`, `ix.s*`, or
`ix.pipeline`. A test enforces this.

Read first:
- `server/ix/artifacts.py` (the contract; do not change it);
- `C:\Users\loren\earth-evals\pipeline_skeleton.md`, the section "Checks carried forward".

## Files

### `ixcheck/geom.py`
Own, small implementations, using `dnp` for the maths:
- `arclen(line)`: cumulative arc length;
- `at_s(line, s)`: a point at arc `s`;
- `seg_dist(p, a, b)`: point-to-segment distance;
- `crossings(a, b)`: crossings of two polylines, with the arc on each.

### `ixcheck/load.py`
`scan(root) -> dict`:
- `{step: [artifact, ...]}` for every `.npz` under root;
- `by_key`: `{key: artifact}`;
- the parent links resolved: a step k artifact finds its parents by `meta.parents`.

### `ixcheck/c1_paths.py` … `c6_heights.py`
Each has `check(art, parents) -> [artifacts.Finding]`, where parents is a dict of the parent artifacts by step.

**c1 (Paths):**
- `duplicate_member`: no two members share a key.
- `broken_member`: no member's line has fewer than 2 points or a zero-length segment.
- `hanging_end` (note): a structure member's end (deck, tunnel, rail_deck) with no other member's line within
  `JOIN_M = 2.5`: the map ends it there (a ramp's end at its gore, a gap in a way), nothing in the pipeline can join it.
- `unrelated_end`: a structure member's end with another member's line within `JOIN_M` must be at a join or a crossing
  of the Paths (within a metre of arc: gradeshapes finds a joint once from both its parts' ends). A `fail` with the member
  key and the nearest member's distance: what the lines show and the artifact does not.
- `bad_join`: every Join's `(a, sa)` and `(b, sb)` points coincide within `JOIN_M`, recomputed with `geom.at_s`.
- `bad_crossing`: every PlanCrossing's two points coincide within 0.5 m and with `q`, recomputed; within `JOIN_M` where
  either is at the end of its member (an end's relation: the end's own point is within a join of the other's line).

**c2 (Extent, parent Paths):**
- `bad_seed`: every seed is a structure member.
- `bad_boundary`: every boundary's `(j, sj)` is a ground or rail member, and its point is within `JOIN_M` of
  `(i, si)`'s.
- `no_boundary` (note): a system with no boundary.
- `oversized_system`: a system with more than 60 seeds. A `fail` that names its members.
- `bad_box`: the box contains every seed line.

**c3 (Order, parents Paths and Extent):**
- `bad_crossing`: every crossing's `u` and `d` points coincide within 0.5 m (`geom.at_s`) and with `q` (within `JOIN_M` where either is at its member's end).
- `order_cycle`: the relation "d below u" over the members has no cycle. A `fail` that names the loop's member keys.
- `bad_level`: `level[u] > level[d]` at every crossing.
- `bad_sign`: the sign matches the member kind (deck / rail_deck +1, tunnel -1, ground / rail 0).
- `bad_need`: `need > 0` and finite.

**c4 (Terrain, parent Heights of the same system, for the piece count):**
- `bad_shape`: the ground arrays have one value per sample of each piece.
- `ground_nan`: non-finite ground where trust > 0.
- `bad_trust`: trust lies in [0, 1].

**c5 (Datum, parent Order):**
- `bad_moves`: there is one move per crossing, each in up / down / none.
- `bad_dip`: every dip window's member is the lower member of a "down" crossing of the system. Recompute it from
  Order, not from the dips themselves.
- `bad_chosen`: `chosen` indexes `options`.

**c6 (Heights, parents Order, Datum, Terrain, and Paths through Extent):**
- `no_heights` (note): the system is not solved. Name the members.
- `heights_nan`: non-finite z in a solved piece.
- `clearance` (carried from 3): at every crossing, the upper surface minus the lower is at least `need - 0.05`. The
  upper surface is the z of the piece whose samples cover `(u, su)`; the lower is the piece covering `(d, sd)` if
  there is one, else the height Terrain.fixed holds of that member there (what the solve read of a member the shape does not
  move: the ground along the road, a track's top); neither held, nothing was asked of the crossing. A `fail` that names the two keys and the metres short.
  Interpolate z along a piece by its `d`, finding the sample by `mem` / `ms`.
- `joint_step` (carried from 1): at every Join inside the system where both members are in pieces, the two heights
  differ by at most 0.05 m.
- `boundary_off` (carried from 2): at every boundary, a deck piece's end z is within the ground road's reach.
  Skeleton rule: report the difference, as a note only.
- `grade` (note): a piece's grade between neighbour samples above the member's max grade. Use 0.08 for roads and 0.05
  for rail as fixed constants in ixcheck. These are notes, not fails, in this PR.

### `ixcheck/run.py`
`python -m ixcheck.run <root> [--json out.json]`.
- It prints a table: per step, the artifacts checked and the fails and notes per code.
- Then the first 20 fails, one line each.
- Exit code 0 always: the counts are the output.

### `ixcheck/__init__.py`
A docstring only.

## Tests (`server/tests/test_ixcheck.py`)
- **Import rule:** parse every file of `server/ixcheck` with `ast` and assert no forbidden import.
- **Per check:** synthetic artifacts built from the records in `artifacts.py`: one clean, plus one per fault the check
  must name, with the expected code. They cover a hanging end, a cycle, a crossing short of clearance, and a joint
  step.
- **End to end:** `scan()` over a temp dir with saved artifacts. This needs `artifacts.save`; if it still raises
  NotImplementedError when you start, write the test so it runs once save exists, and leave a note.

## Rules
- Deterministic maths: no `np.interp` / `np.hypot` / `math.cos` / `@` / `**` / unstable `argsort` (dnp / dmath).
- Short comments that say why. No location names. No issue numbers.
- Never wrap pytest in `timeout`. `pytest -n 3` at most. Kill only your own processes.
- Do not commit; leave the changes in the working tree. Touch only `server/ixcheck/` and
  `server/tests/test_ixcheck.py`.
