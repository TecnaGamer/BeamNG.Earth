# Road calculation alternatives and GPU assessment

Investigated 2 November 2026 against original dev `9f4739d2`, continuing the
isolated performance PR from checkpoint `35e2e38e`. The endpoint-preparation
experiment below has now been implemented in a follow-up from `df2cb877`;
the other calculation and GPU options remain proposals. Measurements and
reproduction steps are in [the performance audit](ROAD_GENERATION_PERFORMANCE.md).

## What the measurement says

The packed mirror reduces the first road job's median from 43.54 to 29.04 s in
three matched offline pairs. A separate instrumented first-tile run separates
the preparation layers from their native calculations:

| Code path | Calls | Inclusive time in this trace |
|---|---:|---:|
| `roads._lane_flow_local`, including neighbour reads and Python input preparation | 145 | 10.535 s |
| Native `earthfast.lane_flow` | 38 | 0.088 s |
| `RoadWidths._near_walls`, including cell reads, wall assembly and coordinate frames | 1,420 | 12.993 s |
| Native `measure_row` and `measure_row32`, combined | 1,420 | 0.032 s |
| `grading.plan_roads` | 2 | 2.005 s |
| Native `earthfast.grade_points` | 141 | 0.092 s |
| `buildings.road_tile_items` | 1 | 0.248 s |

The three measured native kernels total about 0.21 s. Even eliminating their
entire measured cost would recover little of this cold start. This does not
cover every native kernel, other cities, or dense terrain updates while driving.
The trace adds overhead (46.77 s overall here), so it is used for attribution,
not the A/B speed claim. Nested timings overlap, aggregate multiple threads and
include waiting; they must not be added or interpreted as CPU time.

## Alternatives worth testing, in order

1. **Prepared lane inputs: implemented and measured.**
   `_lane_flow_inputs` caches endpoints, short-feature counts and four attributes
   per decoded source cell. Neighbourhood assembly retains the first occurrence
   of each ID and the canonical home-cell decision. A local solve with no
   unknown eligible mainline count skips neighbour reads; those neighbours
   cannot change its answers. Replacing the OSM lane column invalidates prepared
   inputs, including when a new tag shard arrives before the cell's first solve.
   Eight regression cases cover connectivity, duplicate precedence, home cells,
   excluded/short features, reuse and replacement tags/cells.

   In the isolated nine-cell benchmark, lane preparation and solving drop from
   about 0.90 to 0.30 s, with matching answers. The earlier 10.535 s trace includes
   neighbour reads and waits; it is not ten seconds of replaceable calculations.
   The matched full-worker median improves 27.07 → 26.34 s (2.7%); keep this
   separate from the isolated improvement. Retained
   prepared arrays cost 72 bytes per feature (about 3.71 MiB across 40 decoded
   cells in this sample), plus small Python/array headers. Coalescing duplicate
   concurrent cell decoding remains a separate follow-up.

2. **Use a worklist for lane propagation if solver time becomes material.**
   The current Rust solver scans all features for up to 16 passes in each of
   two phases. A queue of nodes affected by changed lane counts can avoid
   unchanged work. Worklist fixed-point propagation is a well-established
   technique; Clang documents this optimization. Applying it here requires
   separate correctness work: the current in-place order, pass cap, default
   phase and cyclic graphs may produce different results under another update
   order. At 0.088 s in this trace, it is lower priority than input preparation.
   [Clang data-flow analysis](https://clang.llvm.org/docs/DataFlowAnalysisIntro.html).

3. **Test ray traversal or a tree against the existing wall grid.**
   `wall_ray` currently visits the rectangular bucket range of a ray. A 2D
   adaptation of Amanatides–Woo traversal visits intersected grid cells in
   order and can stop after passing the nearest hit. Corner ties, rays on grid
   edges and duplicate segments need exact handling. Its likely gain here is
   bounded: rays are only 25 m long and buckets 16 m, and native width
   measurement is already about 0.032 s in this trace.
   [Amanatides and Woo, original paper](https://www.eecs.yorku.ca/~amana/research/grid.pdf).

   For unevenly populated cells, a packed R-tree or AABB hierarchy is another
   candidate-selection method. STRtree indexes 2D bounding boxes; CGAL documents
   static AABB structures for intersection and distance queries. These are
   alternative designs to benchmark, not a reason to add either dependency
   automatically. Include index construction, memory and coordinate conversion
   in the comparison. A nearest-geometry query cannot replace the current
   directional first-wall hit without changing measured widths. Prefer a
   native implementation over creating thousands of new Python geometry objects.
   [Shapely STRtree](https://shapely.readthedocs.io/en/stable/strtree.html),
   [CGAL AABB tree](https://doc.cgal.org/latest/AABB_tree/).

## Where GPU work fits

The original dev build already has a headless `wgpu` device and compute
shaders in `native/earthfast/src/gpu.rs`: imagery seam detection and BC1/BC7
encoding, with CPU fallbacks and `BNGE_GPU=0`. Its dependency is wgpu 25, with
Vulkan, D3D12 and Metal enabled. The matching API supports those platforms;
there is no need to introduce a CUDA-only runtime for an initial experiment.
[wgpu 25 documentation](https://docs.rs/wgpu/25.0.2/wgpu/).

The strongest candidate for a future road GPU experiment is **batched terrain
point grading**, if a driving capture shows a large enough batch cost. Keep
immutable segment/index data on the device and process many points per dispatch.
Batching wall queries could follow if dense-city measurements justify it.
File opens, decompression, Python feature decoding and blocking dependency
requests remain CPU work in this design. NVIDIA's guidance explicitly calls
for including transfer overhead, batching small transfers and retaining
intermediate data on the GPU; a shader-only stopwatch is insufficient.
[CUDA best practices, data transfer](https://docs.nvidia.com/cuda/cuda-c-best-practices-guide/index.html#data-transfer-between-host-and-device).

RAPIDS/cuGraph is useful as a research reference, but its documented Windows
installation uses WSL2 and its stack requires NVIDIA hardware. That adds a
deployment boundary to this native Windows application, while its generic
graph algorithms do not directly implement this road-specific two-phase lane
inference. It is not the recommended next integration.
[cuGraph requirements](https://docs.nvidia.com/cugraph/latest/tutorials/basic_cugraph/),
[RAPIDS installation](https://docs.nvidia.com/datascience/install/).

A GPU experiment should retain a CPU fallback, use bounded batches so it can
share the GPU with BeamNG, and measure first-use initialization/compilation,
upload, dispatch, readback, VRAM and game frame time together. Current road
math uses float64; the existing image shaders use float32. Any precision change
needs explicit tolerances and seam, bridge-clearance, grading and collision
checks, including multiple GPU vendors. No road GPU kernel or new dependency
is added by this PR, and no GPU speedup is claimed.

The next experiment should coalesce cell decoding and measure remaining
neighbour-read and wall-preparation costs, then collect a live driving trace.
GPU point grading remains a concrete later option if that trace
shows enough arithmetic work to amortize dispatch and transfer costs.
