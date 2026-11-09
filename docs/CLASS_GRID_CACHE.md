# Raw land-cover class reuse across physics blocks

The broad `landcover` stage includes classification, slope/material selection, underwater overrides and road
inputs. An isolated profile of 1,048,576 points with already decoded WorldCover inputs on the i5-10600 found
raw `Surface.classes` at 172 ms median: warp 107 ms and raster sampling 33 ms in the instrumented pass. This
does not attribute the entire observed 835 ms landcover stage to this sampler.

`ClassGridCache` (native API 172) retains only raw classes in 64 x 64 sample chunks of the exact physics
lattice, up to 8 MiB of class samples plus small identity/entry metadata. It recomputes missing points with
the existing sampler. Frame, lattice spacing and warp changes invalidate the namespace. Immutable decoded
rasters contribute weak identity tokens: replacement/reload invalidates entries without pinning raster data
outside MapData's own memory budget. Source sets are conservative for the full block; changing that set can
cause extra misses, which favors correctness over hit rate. Missing coverage is never retained. Polar and
antimeridian blocks keep uncached sampling. Plans are independent and stale generations cannot repopulate
a cleared cache. Returned arrays and input arrays do not share mutable storage with retained samples.

Only raw WorldCover classes are cached. Slope, lawns, water-bed and road overrides still run on the current
block. No plan/facts format change: focused regressions compare raw classes, final material indices and `.ter`
bytes across moves. The source rasters are immutable by MapData's contract; callers cannot mutate them in place.

### Measurement on Windows

Core i5-10600, Python 3.10, Release MSVC earthfast 160 (the measurement build; the code is unchanged since), benchmark process restricted to logical CPUs 6..11.
Nine alternating pairs per route, each route nine 1024 x 1024 blocks. Every measured cached pass starts empty;
one warmup per consumer is excluded. Coordinate generation and output checks are outside timing. The same
native binary serves both consumers. Inputs are already decoded, either seeded synthetic rasters or independent
copies of six WorldCover cache files (no live cache/server, downloads, sea/road work or game running).

| Input / route | Original route ms | Cached route ms | Change |
| --- | ---: | ---: | ---: |
| Synthetic, straight | 1581.4 | 585.9 | -62.9% |
| Synthetic, diagonal | 1550.6 | 838.8 | -45.9% |
| Synthetic, turn/revisit | 1553.6 | 318.5 | -79.5% |
| WorldCover, straight | 1583.3 | 572.4 | -63.8% |
| WorldCover, diagonal | 1536.3 | 1005.0 | -34.6% |
| WorldCover, turn/revisit | 1546.9 | 683.6 | -55.8% |

All measured paired route totals favor caching. All 486 measured cached grid hashes match their uncached
references across these datasets; the unit tests also compare arrays and terrain-file bytes directly. Sample
storage peaks at 1.56..2.88 MiB in these routes. These numbers measure this sampler, not the whole terrain
pipeline or gameplay FPS. A cold isolated block can incur cache-setup overhead; the cold fill is included above.

Reproduce from a source checkout without network or the player's cache:
`python tools/benchmark_class_grid.py --pairs 9 --out build/class-grid.json`.
`--inputs` accepts standalone `z_x_y.npz` snapshots; missing coverage fails before measurement.

## Scope

This is a server-side speedup of one stage of a physics block's build (its raw land-cover sampling). It does
not change the in-game terrain move itself: a manual A/B drive at Sarreguemines showed the same stutters, whose
frames are the engine's own terrain-file application and its render work, outside this stage.
