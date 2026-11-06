# Cliff generation performance — 2026-11-05

The retained changes accelerate shared cliff LOD/export work and reduce competing client work during camera movement. They apply to all cliff families, not just canyon materials. Geometry detail, textures, triangle chunk limits and collision distances are unchanged.

## Matching uncached camera test

BeamNG D3D12, RTX 5090, foreground limiter 60 FPS. Baseline: commit `20e2d8cf`; optimized: this working-tree patch. Each version used a fresh server and client extension reload, with generated-content revisits disabled (`BNE_REVISIT=off`). Source elevation/imagery caches were retained to avoid internet variability. This is an uncached **generation** comparison, not a cold OS/GPU/shader-cache test.

Both versions settled the initial scene, collected setup garbage outside the timed interval, and used essential frame profiling with the same 120-frame settling period. No tests or other intentional heavy analysis ran during these final captures. The baseline was repeated after the optimized capture with the same setup-garbage collection procedure. These are single matching passes, not statistical confidence intervals.

Route, with anchor latitude 36.06 / longitude -112.15:

- Original position: `(11163.246996249, -6692.35741625, 1937.4044350083)`; rotation quaternion `(-.0047313887328638, .0073617255892862, .84120598994165, .54064396921014)`; FOV 65 degrees.
- 30-second rotation: position fixed; rotation `quatFromEuler(0,0,2*pi*t)*originalRot`.
- 45-second out/back travel: position `originalPos + (1200,600,100)*sin(pi*t)`; rotation `quatFromEuler(0,0,.7*sin(2*pi*t))*originalRot`.
- 30-second stationary drain at the original view. Here `t` is normalized phase time.
- The same vehicle position was restored for each pass. Physics remained active. The scripted path was identical but was not separately checked for terrain clearance.

### Travel phase results

| Metric | Baseline | Optimized |
|---|---:|---:|
| Average FPS | 57.91 | 58.16 |
| P95 frame time | 20.69 ms | 20.65 ms |
| P99 frame time | 28.81 ms | 28.12 ms |
| Worst frame | 199.18 ms | 93.35 ms |
| Frames over 25 ms | 55 | 40 |
| Frames over 33.4 ms | 12 | 12 |
| Frames over 50 ms | 5 | 4 |
| Cliff pieces imported | 146 | 257 |
| Total measured cliff import work | 526.73 ms | 958.55 ms |

Both rotation passes averaged 59.99 FPS, with no frames over 25 ms. Both stationary drain passes averaged approximately 60 FPS. Neither travel pass ended with queued mesh walks. The optimized server delivered 76% more cliff pieces during the same moving route; this is an end-to-end streaming comparison, not an identical count of imported pieces. Average import cost per piece did not improve (3.61 versus 3.73 ms). The modest frame-time improvement comes while doing more total work, not from removing rock detail.

Raw final captures: `build/cliff-cold-study/cap_control_{spin,travel,drain}.json` and `cap_final_{spin,travel,drain}.json`. Compact measurements are committed alongside this report as `2026-11-05-cliff-generation-timings.json` when the patch is committed.

## Server work

A three-repeat median microbenchmark on 16,928-triangle meshes compared all generated LOD attributes and topology (floating-point values within absolute tolerance 1e-12):

| Surface | Before | After | Speedup |
|---|---:|---:|---:|
| Mixed material families | 110.05 ms | 33.95 ms | 3.24x |
| Terrain-contact constrained | 166.88 ms | 68.76 ms | 2.43x |

Matching large streamed geotiles in the earlier uncached runs dropped from 14.2 to 8.1 seconds and 16.6 to 9.6 seconds total; their mesh-writing stages fell from 10.6 to 4.8 and 12.7 to 5.6 seconds. These end-to-end worker measurements include process contention and are illustrative, not isolated microbenchmarks.

Implementation:

- Reuse boundary/material topology, original normals and original terrain-clearance samples across LOD trials.
- Group integer edge keys and use weighted bincount accumulation instead of slower structured edge sorting and repeated indexed accumulation.
- Skip redundant Python vertex remapping when exported meshes already fit the runtime vertex limit.
- Select cliff collision before native shape registration, avoiding immediate duplicate `postApply` setup. Cache collision range checks per tile and hysteresis state within each pass.
- Treat free-camera translation, rotation and FOV changes as interactive activity even with a parked car. Retain the larger stationary/teleport loading allowance.
- Account for network/world work before shape admission; give tree insertion only the remaining interactive frame allowance.
- Do not add the mod's adaptive sleep when the game already has an active FPS limiter or VSync. Query limiter state once a second; uncapped behavior remains available.

## Rejected experiments and remaining limits

Halving cliff chunks to 8,192 triangles increased shape count/import churn and worsened pacing; the original 16,384-triangle cap was retained. Camera-aware budgets alone also performed worse as faster generation exposed more client arrivals. Shared budgets plus avoiding double frame pacing were needed for the final result. Earlier captures used heavier instrumentation or different pre-capture GC conditions and are not used in the final table.

Stutters are reduced, not eliminated. Final travel hitch records include approximately 38–51 ms Lua GC events; the 93 ms frame included 46.7 ms GC plus 39.8 ms Lua work. Native shape import and collision setup are synchronous and cannot be preempted by the Lua scheduler. Initial scene/teleport loading can still produce larger spikes outside this moving-route measurement. GPU timing telemetry was stale in these captures, so no GPU-time improvement is claimed. Further gains should target allocation churn and native collision/import scheduling, with the same route and independently repeated trials.

## Verification and restored state

- 122 focused Python tests passed (cliff families, LODs, export, slots and terrain contact).
- 3,154 Lua checks across eight modules passed (core, roads, road geometry, LODs, governor, scoping, trees and frame pacing).
- Topology/attribute equivalence and export byte-equivalence regression checks passed.
- Final screenshot `screenshots/mcp/shot_20261105_174957_95.png` was visually inspected: canyon layers, PBR detail and vegetation remain present; existing terrain seams are not changed by this patch.
- Optimized client/server restored, normal generated-content caching re-enabled, original camera/vehicle location restored, temporary measurement hooks removed, 60 FPS cap preserved.

The prior material work and unused-texture cleanup were committed separately as `a6a33ea0` and `20e2d8cf`. This optimization patch has not been committed or pushed yet.
