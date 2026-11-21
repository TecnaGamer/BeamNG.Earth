# ix: the interchange pipeline skeleton (implementation spec)

Goal of this PR: **no behaviour change**. The skeleton wraps what runs today under `BNE_SHAPES=1`
(`grading._Planner._shape_region_calc` and the gradeshapes calls in it). Each step's output becomes a cached,
checkable artifact (`ix/artifacts.py`, the contract: do not change its records without asking). The game, the tests
and the evals then run one code path.

Read first:
- `server/ix/artifacts.py`
- `server/grading.py` `_Planner._shape_region_calc` (the code being split; around line 4840)
- `server/gradeshapes.py`: `one_road`, `find_systems`, `_system`, `pieces`, `sample`, `solve_shape`, `embankments`,
  `system_key`, `clearance_need`, `under_pairs`
- `C:\Users\loren\earth-evals\pipeline_skeleton.md` (the design)

## Files to write

### `server/ix/__init__.py`
Empty, apart from a docstring that points to `artifacts.py` and this spec.

### `server/ix/cache.py`
- `ROOT`: `os.environ.get("BNE_IX_CACHE")`, or None. With None nothing is read or written: the game's default, so
  there is no change in disk use.
- `laws_digest(*modules_and_names)`: a sha1 hex of the repr of the named constants. Steps call it with their own
  law names, e.g. `("gradeshapes", ["JOIN_M", "STEP_M", "DECK_MAX_GRADE", ...])`.
- `key_of(step, fmt, laws, parents, inputs_bytes)`: sha1 hex over `artifacts.SCHEMA`, step, fmt, laws, the sorted
  parents and the inputs' bytes. Deterministic.
- `path_of(step, key)`: `ROOT/s<step>/<key[:2]>/<key>.npz`.
- `get(step, key)`: `artifacts.load` the file, or None.
- `put(artifact)`: write to a temp file, then `os.replace` (atomic, safe across processes).

### `server/ix/s1_paths.py` … `s6_heights.py`
Each module has:
- `FORMAT`: an integer, up with each change of what the step writes;
- `LAWS`: the constants it reads, as `(module name, [names])` for `laws_digest`;
- a `run(...)` function that builds its artifact from today's code and returns it **and** the live objects the next
  step needs. The next steps keep using gradeshapes' own `net` / `sysm` / `pcs` / `smp` objects, so behaviour is
  byte-identical. The artifacts are a faithful copy.

| module | wraps | run returns |
|---|---|---|
| s1_paths | one interchange's members (what it names, what its structures are near, what its pieces are followed through; numbered by key) and the relations of its structures with them (`_relations`; never a ground road's pair with another). Also the cells' members' reading into the region (`load`, `members_of`) | `Paths` |
| s2_extent | the region growth loop with `_gsh.find_interchanges(...)`; per interchange its `System` (seeds, joins, boundary, box) in the Paths' numbering | `find`: `(net, mine, loaded, members)` (mine: the interchanges this region solves); `run`: `Extent` |
| s3_order | per sysm: `sysm["crossings"]` → `Crossing` (need from `gradeshapes.clearance_need(net, u, d, q)` as `_system` / `shape_problem` use it); sign from member kind; level: a longest-path layering of "d below u" over the system's members (0 for ground members); a cycle gives a `Finding("order_cycle")`, never an exception | `Order` |
| s4_terrain | `ground = [self._ground_line(x["xy"]) for x in smp]` (the ground the solve reads: of a system already solved, the one its answer was solved on); trust = 1.0 where finite, else 0 | `(Terrain, ground)` |
| s5_datum | `c["moves"]`, `sysm["dips"]`, the held decks (`net.m[i].held`) | `Datum` with `options=[Option(moves, None, None, "today's verdicts")]` |
| s6_heights | `smp = _gsh.sample`, `zs = _gsh.solve_shape(...)` with today's cache and wait logic **unchanged**; the region's `embankments` + `prof` assembly | per system `Heights`, and `Region` for the region |

One interchange is one scope, `system:<system_key>`, for all six steps, and one numbering of its members: its Paths'
(by key). Every record of it that names a member (Extent, Order, Datum, Heights) uses it, so the same interchange is
the same artifacts from every region that finds it.

### `server/ix/pipeline.py`
`region(planner, home, hk) -> prof`: **exactly** what `_shape_region_calc(home, hk)` returns, built by calling
s1…s6 in order for each interchange the region finds.
- When `cache.ROOT` is set, it puts every artifact.
- Keys: scope `system:<system_key>`; inputs: the Paths' members' bytes (steps 1, 2), the system's key (3), the
  samples' xy bytes (as `_shape_sys_key` does: steps 4-6). The parents' keys chain them: paths, extent, order,
  terrain, datum, heights. The region's profiles (`Region`) are the one region-scoped artifact.
- **Reading back:** the pipeline never reads artifacts back to skip generation. It writes only. Reading comes when a
  step is replaced.
- Logging: keep today's `log.warning("grading: SHAPES pid ...")` line and `log.error(... infeasible ...)` lines
  verbatim. Added, at `LOG_LEVEL` (WARNING: what the eval builds' view.log keeps), one line per step per interchange
  and one per region:
  - `ix step: <step> key <key[:12]> pieces N crossings N <seconds> s ok @ <region> system <system key>`, with
    `failed: <why>` in place of `ok` (and at WARNING whatever `LOG_LEVEL`) when the step found nothing it could pass:
    no heights (the kernel's own words: infeasible, timed out, not converged, its error) for `heights`, a level cycle
    for `order`. Pieces and crossings are the interchange's (`paths`: its members and their crossings). The seconds of
    `paths` and `extent` are the interchange's share of finding it (`find_interchanges`: the relations of its members,
    its merges, dips and record) and of making the artifact; `terrain` is the pieces' samples and the ground there;
    `heights` is the solve.
  - `ix region: <region> systems N <seconds> s, load a s, scan b s, steps paths c s, extent d s, order e s, terrain f s,
    datum g s, heights h s`: the totals of the region's step lines, and the region's own: `load` (the cells' members
    and `one_road`), `scan` (the net and the members that belong to no interchange).
- One interchange's steps are `pipeline.system()`, the one place that runs them: a lock round an interchange wraps
  that call.
- `xproc.check_cancel()` runs at each round of the region growth and before each interchange, as the old body did.

### `server/grading.py`
- `_Planner._shape_region_calc` becomes `return ix_pipeline.region(self, home, hk)`.
- Move its body into the s1–s6 modules. Helper closures that need `self` (`make_fixed`, `ground_along`,
  `_ground_line`, `_rail_tops`, `_shape_members`, `_shape_sys_key`, `_shape_debug`) stay on the planner; the steps
  get the planner as an argument.
- Import at the top: `import ix.pipeline as ix_pipeline`, with the server dir on `sys.path` as today. `ix` is a package
  under `server/`.

### `server/ixcheck/`
Written by a separate worker, out of scope here.

## Tests to add (`server/tests/test_ix_pipeline.py`)
1. **Same output:** for the fixture places other tests already plan with `SHAPES_ON` patched True (see
   `test_grading.ShapeSolvedOnceTest`, which uses `test_approaches.overpass` / `flat_elev`), `ix.pipeline.region` and
   the pre-change `_shape_region_calc` give identical `prof`: same keys, same `xy` / `z` bytes, same modes. Keep a
   private copy of the old body in the test module as the reference: copy it verbatim from git (`HEAD` before your
   change).
2. **Round trip:** every artifact of a run round-trips `save` → `load` with equal arrays (`tobytes`) and equal fields.
3. **Determinism:** two runs produce the same artifact keys and bytes.
4. **Off by default:** with `BNE_IX_CACHE` unset, nothing is written.

## Rules
- No behaviour change. `pytest server/tests/test_gradeshapes.py server/tests/test_grading.py
  server/tests/test_staged.py server/tests/test_interchange_audit.py -n 3` passes as before.
- Determinism (`server/tests/test_determinism.py`): no `np.interp` / `np.hypot` / `math.cos` / BLAS `@` / `**` /
  unstable `argsort` in new server code. Use `dnp` / `dmath`.
- No location-specific code. No issue numbers in comments. Comments are short and say why.
- Never wrap pytest in `timeout`. `pytest -n 3` at most. Kill only your own processes.

## The owner of an interchange (`ix/owner.py`)

A region no longer solves the systems its box holds: each is a piece of an interchange, which `owner.grow` follows to the
whole (the members within `SHAPES_PAD_M` of its seeds, found again as systems, until it takes in nothing more), so every
region reaches the same members and the same key (`gradeshapes.interchange_key`: the sorted canonical ids of its roads,
its centre, the plans format). The cell holding its centre is its owner. The interchange is solved once, by whoever takes
its lock (`plancache.PlanCache.shape_claim`, `xproc.once` without a wait budget), by `pipeline.interchange`: the steps of its
one system, then its embankments. Every other thread and process waits for the lock and reads the stored solution
(`PlanCache.shape_store`: lat / lon and heights, no frame). A holder that is gone hands the lock over; an abandoned one
gives it back. `pipeline.region` is the region's own find, the owner calls and the merge of their profiles.

An interchange ends where its roads come back to the ground, which is the end of a ramp, not the end of a structure. A
road leaving a deck belongs to the interchange as far as the deck's ramp reaches (`gradeshapes._ramp_reaches`, from the
pieces alone, before anything is solved: the ramp the embankment step lays, `_Ramper`, from the height the structure needs
and the roads' grade and curve laws, through the junctions it meets). Structures that join, cross or dig one road are one
system; systems whose ramps run along one stretch of a road are one interchange (`Interchange.parts`): one extent, one key,
one owner, which solves each system as it is and lays the embankments of all at once, so where two ramps meet on a road
one lays what the other does. A road on the ground beyond the reaches is a boundary held at its own ground.
