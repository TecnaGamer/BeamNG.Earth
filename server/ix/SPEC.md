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
| s4_terrain | `ground = [self._ground_line(x["xy"]) for x in smp]` (the ground the solve reads: of a system already solved, the one its answer was solved on); trust = 1.0 where finite, else 0. And the heights of the members the shape does not move (a road on the ground under a deck, a track), read once at the arcs the problem asks (`gradeshapes.fixed_reads`, `Terrain.fixed`): the heights step is made from these and reads nothing. A track's top is the railways' bed (`railways.track_beds`: a lattice of beds, the costly read) plus `RAIL_TOP_M`; all the interchange's tracks are read at once, the railways taken round each cluster of the windows the questions lie in (`planner._rail_tops_many`, through `fixed_z.prefetch`), not once for each 64 m window of each question | `(Terrain, ground, table)` |
| s5_datum | `c["moves"]`, `sysm["dips"]`, `sysm["tracks"]` (the track windows), the held decks (`net.m[i].held`) | `Datum` with `options=[Option(moves, None, None, "today's verdicts")]` |
| s6_heights | `smp = _gsh.sample`, `zs = _gsh.solve_shape(...)` with today's cache and wait logic **unchanged**; the region's `embankments` + `prof` assembly | per system `Heights`, and `Region` for the region |

One interchange is one scope, `system:<system_key>`, for all six steps, and one numbering of its members: its Paths'
(by key). Every record of it that names a member (Extent, Order, Datum, Heights) uses it, so the same interchange is
the same artifacts from every region that finds it.

### Tracks are members of the shape
A track (a rail or rail deck member) that an interchange crosses is a piece of its shape (`kind "track"`, `sysm["tracks"]`:
a window round each crossing, wide enough for the track to leave its bed and come back at its grade and curve). Its chain is
drawn to the bed the railways lay (its top, read by terrain) at a weight, `gradeshapes.W_TRACK`, derived from the laws:
findground costs a move by the run of its ramp, lack / grade; a road moves at best at the steepest grade the laws allow
(`gradelaw.road_grade`, a link), a track at `railways.GRADE`, so a track costs that ratio times a road's dip to move. It is
held at the bed at both ends of its window, where the railways' own bed goes on (when the tracks are free to move: see below).

The solve asks the interchange first with the tracks held (`pin_tracks`): the crossings read a track at the bed, as they read a
road on the ground under a deck, never as unknowns, and the track's own chain, drawn to the bed, is held at nothing (a short
window's bed may be steeper at its two ends than the track's grade, and the roads' problem would have no heights for it); this
is the problem as it was before tracks were members, and a track
nothing needs to move stays at its bed (the Heights hold it there: the answer for it is the bed, not its chain, drawn to the
bed and held to its laws, which the bed's own noise does not meet exactly) and the diagnostics say `tracks_moved: false`. Only
if that has no heights, the crossings solve for the tracks too, each at its weight, `tracks_moved: true`, and only then relaxation
of the road laws is looked at. Where the railways lay no bed (a stub they leave out) there is no track: terrain reads NaN, the
crossing asks nothing.

The profiles of the moved tracks are in the region's, `"rail:<feature id>"`, mode `"track"`, at their bed (the rails' top less
`RAIL_TOP_M`); the railways read them back (`railways.TRACK_Z_HOOK`, in `railways._beds`: the tile, the grader's plans and the
crossing records draw what was solved), except inside the interchange's own steps (`railways.own_beds()`: the solve is made from
the railways' own bed, not from its answer). A track that stayed is the railways' own.

A join between two members is a sample of the piece it falls in (`pieces()`' `cuts`): a join is held at one height, and held at
the nearer of two samples half a step off it, two decks at a T-join stood 0.05-0.11 m apart.

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
- Each step has a wall-clock limit (`ix/limits.py`: paths 30 s, extent 60 s with the growth, order 10 s, terrain 30 s, datum
  10 s, heights 120 s, per interchange: to make slowness fail loudly, not to fit today's worst case, so the Shanghai
  interchanges, 1206 s of extent and 5043 s of heights, fail it until the build is restructured;
  `BNE_IX_STEP_LIMITS=heights=300,extent=120` overrides, 0 turns one off). It is enforced through the cancel mechanism: a deadline of the thread the step runs in, read
  by `xproc.check_cancel()` (which the growth's rounds, the finding's loop, the terrain's pieces, the heights' relaxation
  and its trial solves ask) and capping the heights kernel's own budget; never a thread killed. A step over its limit stops:
  its line is `ix step: <step> key - pieces N crossings N <seconds> s failed: time limit <N> s`, the pipeline writes a
  `Limit` artifact (the cache on; `ixcheck` reports its `time_limit` finding), the interchange has no answer and is not
  stored (the owner gives its claim back; this process does not make it again for every region that reaches it), and
  nothing takes its place: the eval's build shows `grading: SHAPES infeasible system ... time limit`.

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

The growth is claimed too (`owner.grow`, claim `ext` on the road a growth starts from): the first to reach a piece of an
interchange grows it once and stores an index from every member road to its key, and its whole extent (`index_add`); a
region reaching any piece finds the key there, or waits for the growth running. Two growths that reach each other's
pieces are one interchange: the one from the lower road id grows it, the other raises `GiveWay`, which releases its claim
before it waits (so the claims wait only on lower ids, never in a circle, and no growth holds two). A holder that is gone
hands the claim over and a cancelled one keeps what it stored, as for the solve. A region does not scan the pieces of an
interchange already solved (`owner.skipper`: the index names its key and the solution is kept): it reads the solution.
The ramps' reaches (`gradeshapes._ramp_reaches`) are computed only inside that one growth and stored with the extent
(`owner.extent_add`: the box and the member keys of the interchange's seeds, which are one interchange): a region's find
(`find_interchanges(reach=False)`) finds the systems alone, and a solver that did not grow starts from the stored extent, in
one round, with the stored reaches. A growth's rounds scan only the members they add (`gradeshapes.Scan`: each against its
neighbours through the net's grid; the relations of the members of earlier rounds are kept).

An interchange ends where its roads come back to the ground, which is the end of a ramp, not the end of a structure. A
road leaving a deck belongs to the interchange as far as the deck's ramp reaches (`gradeshapes._ramp_reaches`, from the
pieces alone, before anything is solved: the ramp the embankment step lays, `_Ramper`, from the height the structure needs
and the roads' grade and curve laws, through the junctions it meets, no further than `REACH_MAX_M` from the end it left
— the junctions' chains included — and only for a deck whose reach can meet another rising deck's: its extent artifact's
law names it). Structures that join, cross or dig one road are one
system; systems whose ramps run along one stretch of a road are one interchange (`Interchange.parts`): one extent, one key,
one owner, which solves each system as it is and lays the embankments of all at once, so where two ramps meet on a road
one lays what the other does. A road on the ground beyond the reaches is a boundary held at its own ground.

## The order and the heights of an interchange (`ix/s3_order.py`, `ix/relax.py`, `ix/diffgraph.py`)

**Order.** A crossing's over / under is decided from the evidence, strongest first: the structure (the members' kinds: a bridge
is over what it spans, a tunnel under what is over it), then the relative layer of the two roads where they cross
(`gradeshapes._over`); never a section's layer on its own. Each decision alone is consistent, but a road is continuous: its
sections meet at one height at their joins, and a layer that changes along a carriageway puts it under a road at one crossing
and over it a section later. The order step finds those as negative cycles of the difference constraints of the interchange's
crossings (upper at least `need` over lower), joins (one height) and the steepest grade its class may be built along each member
between those points (`gradeshapes.max_grade`: the heights step eases a road's grade law to it before it
gives up anything that keeps a vehicle off a deck), all heights free (`diffgraph`: the same graph the heights step uses). Each is resolved by giving up the weakest evidence in it: a
decision from layers alone (smallest layer difference first) before one from the structure; a structure decision is never
given up there (a loop of them only is `order_deferred`: the heights step eases the clearance). Every decision given up is an
`order_dropped` note of the Order artifact with its reason, and is no longer in `sysm["crossings"]` (settled in place) nor
held by `gradeshapes._stacked`. Deterministic and independent of the input's order: crossings and joins are taken in the order
of their roads' keys.

**Heights.** `solve_shape` no longer gives up an interchange whose laws cannot all hold: the laws that are differences of two
heights are a graph whose negative cycles are the contradictions, and each is eased by the least that closes it (plus a
millimetre of room), in the order the laws are given up, which is the order of what a vehicle can still do: first the grade law
(one factor over the roads of the cycle, no road past the steepest grade its class may be built, `gradeshapes.max_grade`: the class's design
grade by speed, where a deck's own law, `deck_grade`, is lower; a steeper road is drivable, a road through a deck never is); the vertical curve law, which is no
difference of two heights and so is eased from the kernel's answer (`relax.ease_curves`), to the tightest curve the laws give any
road (`gradeshapes.R_MIN_ROAD_M`) and no tighter; then, only where those have given all they can, a decision from the layers alone
(given up whole, smallest layer difference first), a deck's mapped bound (its floor over the ground it spans, a road going
under's ceiling), and a clearance; a join never. The first attempt's miss is no log line of its own (the answer's diagnostics keep
it as `first`): the last solve's is. Each relaxation is in the Heights artifact (`diag["relaxed"]`, a `relaxed_<kind>` note each)
and in the step's log line. A deck's end that goes into a tunnel lands on the ground at the mouth (no floor over the span there):
the bridge that becomes a tunnel along one road is not forbidden. `ixcheck` counts relaxations (`relaxed_*` notes) apart from
failures, and checks the order's consistency under the joins (`weld_cycle`).
