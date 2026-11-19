# BeamNG.Earth v1.4 "Speed": streaming that keeps up at 300 mph and in the air

Status: spec, 2026-10-10, revised after review (the "Review notes" at the end list every finding and
how it was resolved; E = engine review, S = speed review). It builds on v1.3.1 (protocol 4, FORMAT 3;
docs/V1_3_SPEC.md, docs/ROADMAP.md "Done today") and implements ROADMAP v1.4 items 1 to 6. Anything not
mentioned here stays as in v1.3.1.
Inputs: docs/ARCHITECTURE.md, docs/research/2026-10-10-perf-and-architecture.md ("perf"),
docs/research/2026-10-10-origin-distance.md ("origin"), and the drives in docs/research/sim_v13*,
sim_v131_*.

Markers used below:
- **[V]**: measured in-game, or read in code.
- **[M]**: measured offline for this spec: the real `farfield.select` driven along paths with 4 Hz
  poses, HOLD_S and hysteresis (`demand_check.py`, `flightdemand.py`), or the review's frame-level model
  (`fsim.py`: `farfield.select`, `swap_groups`, `level_size` and a copy of `follow.decide`, with a
  constant cost per unit). Scripts are in the session scratchpad (`v14review/`); M0 moves the demand
  part into `server/tests/test_demand.py` so every [M] demand number is reproducible from the repo.
- **[I]**: inference.
- **[P]**: needs an in-game probe (section 7).

Targets:
- 134 m/s (300 mph) sustained on the ground; up to 200 m/s on the ground (take-off roll) with a warm
  DEM (section 3.5).
- 250 m/s in the air at any altitude up to 10 km, including dives and terrain ahead of the aircraft.
- The visible ground under the car matches the physics surface: the exact level's vertices are the
  physics lattice, and between vertices the mesh differs from the engine's bilinear collision by at most
  |twist|/4 per 2 m square (p99 ≤ 5 cm, max ≤ 31 cm on the v1.3 `p_diag` block). A coarse tile under
  the car never happens (metric `exactMiss` = 0).
- No hole, no fall-through.
- No frame over 50 ms in steady driving; a rebase frame ≤ 45 ms if probe P7 gives a path, otherwise
  ≤ 60 ms and counted separately (section 3.5).
- At least 60 fps mean at 4K.
- Rebases are invisible (no pop, no z-fight).

## 0. Milestones (ordered by value per cost; each one ships alone and leaves the game working)

| M | Roadmap item | What | Key number | Wire | In-game probes first |
|---|---|---|---|---|---|
| M0 | - | Test harness and baselines: GE-side per-frame driver, effective-speed check, v1.3.1 baselines re-measured; `test_demand.py`, `bench_build.py` | runs at ≥ 95 % of nominal speed; no harness frame > 50 ms | none | P0, P1, P8 (+P10), P9, P13 |
| M1 | 3 (tiles) | 64x64-cell tiles at every level. The exact (physics) level becomes L-1 = 128 m at 2 m spacing | demand at 134 m/s (full detail): **49.9 -> 16.0 tiles/s**; objects **811 -> ~330** [M]. Gate: c_eff(300) ≤ 6.2 ms | FORMAT 4, `hello.cells` | P8 |
| M2 | 2 (priority), 3 (throughput) | Flow control (seq-numbered acks, at most W unacked tiles), ETA priority with group classes, one reserved build worker | mod queue ≤ W + 2 = 8 (today it grew to 2041) | protocol 5 | - |
| M3a | 2 (look-ahead), 3 | Server: look-ahead capsule, demand-budget controller (no speed floor), exact corridor with one-level refinement steps, capsule prefetch, parent-texture fallback, post-rebase reload reserve | demand ≤ 0.8 x measured capacity at every speed; exact tiles requested ≥ 2.4 s ahead at 134 m/s | pose `vz`/`cap`, `far_retex` | - |
| M3b | 3 (budget), 4 | Mod: urgent import, no crossover under the car, adaptive budget, `exactMiss`, teleport hold, old-tile sink, speed-scaled rebase prefetch | exactMiss = 0 at 134 m/s with c_eff up to 10 ms (offline) | none new | P13, P14 |
| M4 | 5 | 512² physics block re-centred on a 128 m grid, fast-mode follow, per-size rebase table | commit work **46-48 -> ~18 ms** [I, P2] | `hello.blockSize`, terrain.size 512 | P2 (extended), P7, P11 |
| M1h | 6 (part) | Horizon far field on the ground and in the air: radius and per-tile cull from the line-of-sight horizon (eye height and tile heights above sea level, refraction), disc selection, MAX_LEVEL 10, coarse textures, visibleDistance follows, distance haze | Whitney summit 80 km -> ~310 km visible terrain; +40-90 coarse tiles | `far_tile` levels up to 10 | P12 |
| M5 | 6 | Altitude: ground height from cached data, clearance and time to impact, impact-targeted physics block, 3D capsule LOD, disc far field to the horizon (400 km) | 250 m/s at 300 m AGL: 16.3 tiles/s unscaled, held ≤ 0.8 cap by the controller [M] | `pose_reply` | P12 |
| M6 | 1 | Global (cube-sphere) far tiles in tile-local frames plus a disk cache. **Gated** (section 3.7) | rebase reload ~300 -> ~133 tiles | protocol 5 (frame field) | - |

M0 makes every later in-game number trustworthy. M1 to M3b together fix the 200 mph failure. M4
removes the commit stutters. M5 adds flight. M6 is an optimisation: the post-rebase reload fits
between rebases without it (section 2.6), and section 3.7 gives the gate.

## 1. What fails today

### 1.1 The "200 mph" drive (docs/research/sim_v131_200mph) [V]
The drive was nominally 90 m/s, but `tools/sim_drive.py` moves the car with MCP `set_position`, i.e.
`spawn.safeTeleport(..., true)`, which resets the car and zeroes its velocity
(`GAME/lua/ge/extensions/mcp/tools/vehicle.lua:117-125`; `core.lua:367-376`), and each MCP round trip
slows the step. Effective figures:
- 20 km in 295.8 s = **67.6 m/s** (150 mph), not 90.
- The pose velocity the mod sent was about 0.
- 32 commits = one every **9.2 s**, not 6 s.
- Screenshots and resets make > 50 ms frames by themselves: `sim_v13/summary.json` has 11 such frames
  with only 3 commits. Some of the 55 frames > 50 ms below are harness frames.
- The "40 m/s" `sim_v131_rebase` ran at **32.2 m/s**.

| t (s) | queued | waiting groups | live | old | estCdaeTex (ms) | fps |
|---|---|---|---|---|---|---|
| 1 | 23 | 14 | 883 | 0 | 6.3 | 77 |
| 98 | 924 | 540 | 1087 | 0 | 8.0 | 65 |
| 193 | 2041 | 1417 | 1276 | 0 | 11.9 | 61 |
| 205 (after the rebase) | 589 | 540 | 69 | 1276 | 8.5 | 67 |
| 296 | 1968 | 1461 | 321 | 1276 | 11.9 | 56 |

Other figures from the same drive:
- Imports ran at 11.4/s: 5195 -> 8553 in 295 s.
- 32 terrain commits of 44-49 ms of work each (a 60-65 ms frame).
- One rebase of 71 ms of work (`lastRebaseMs`; the frame was about 85 ms).
- 55 frames over 50 ms; worst frame 160 ms; fps mean 64.1 (52-77).

Near the car only coarse tiles showed, the car floated, and 99 s after the rebase only 321 tiles of
the new set had loaded. M0 re-measures this drive and the 40 m/s baseline with the new driver; until
then the v1.3.1 numbers in this spec are stated at their effective speeds.

### 1.2 Root causes
- **R1: demand is 2.2 times the capacity at the effective 67.6 m/s, 4.5 times at 134 m/s.**
  - The v1.3.1 selection needs about 25 tiles/s at 67.6 m/s [I, between the 40 and 90 m/s rows of
    table 2.1], 33.4 at 90 m/s and 49.9 at 134 m/s [M].
  - The mod's import capacity is about 11/s; the capacity model of section 2.2 predicts 11.2/s against
    11.4/s measured.
- **R2: no backpressure, so the mod replays history.**
  - `Connection._stream` sends every tile as soon as it is built and emits a swap group once its adds
    are *sent*.
  - The server's `shown` therefore runs minutes ahead of the mod.
  - The mod must execute every group in arrival order. A waiting group blocks every later group that
    shares a key (`farfield.lua` M.update step 2), so 1461 groups waited and `live` grew to 1276,
    whereas the desired set is about 811.
  - `peekImport` scans the entire 2000-entry queue for every import.
- **R3: the cost of each import grows with the number of objects.**
  - Over all six v1.3 drives (921 samples) [M from the samples], `estCdaeTex` correlates with
    live + old + deleting + preloaded objects (r = 0.77) and with materials (r = 0.73).
  - Median cost by object count:

    | objects | 700-900 | 900-1100 | 1100-1400 | >1400 |
    |---|---|---|---|---|
    | estCdaeTex (ms) | 4.5 | 5.9 | 8.6 | 11.1 |

  - Fit used for planning: c(n) = c(n0) · (1 + α·(n − n0)) with **α ≈ 0.0016 per object** (the
    800 -> 1000 step; 0.0021 over the whole range). P8 measures α for 64-cell tiles at 300 and 600
    objects. Section 2.2 and the integration drive use this term.
  - `estMaterial` grows the same way, from 0.48 to 1.04 ms.
  - The cause inside the engine is unknown (probe P9). Fewer objects is the mitigation in any case.
- **R4: terrain-follow commits.**
  - Each one is `importMaps` 1024² (~43 ms) + `reloadCollision`, which makes a 60-65 ms frame.
  - Each heavy slot costs streaming budget: L(b) = b²/(2·RAMP_DOWN_HEAVY) + b²/(2·RAMP_UP) + 5·b
    (ramp down, ramp up, 2 + 2 quiet frames and the heavy frame). Today (0.1 and 0.05 ms/frame) that
    is 70 ms at b = 2.
  - The rebase took 71 ms of work: ~45 ms import plus ~26 ms nobody has broken down yet (probe P7).
- **R5: a rebase re-keys every tile.**
  - The quadtree lives in the anchor's ENU plane (`farfield.py` module doc), so about 880 tiles are
    rebuilt even though the old ones are exactly correct after `farfield.transform`.
- **R6: nothing guarantees exact ground under the car.**
  - L-2 tiles have no priority over other tiles apart from nearest-camera import order.
  - The C2 crossover moves an exact tile vertically by up to dIn for 0.5 s, even when it is under the
    car.
- **R7: the server has no notion of build capacity.** Imagery is fetched inside the tile build
  (`earthd.py:430`, synchronous in a worker); only the next 12 queued tiles are prefetched
  (`IMAGERY_PREFETCH`, `earthd.py:103`); 3 workers (`earthd.py:217`), no preemption. Perf 3.1 measured
  L2 ≈ 300 ms and L4 ≈ 85 ms at 256 px (imagery mosaic). Three far builds in progress can hold back a
  class-0 tile for about 1 s.

## 2. Throughput math

### 2.1 Demand: tiles added per second [M]
Model: a level-L ring with outer radius r_out and inner radius r_in that moves at horizontal speed v
adds about 2v(r_out + r_in)/S_L² tiles per second (splits ahead plus merges behind). In the air the
ring radii are horizontal: r = sqrt((K·S)² − agl²) where K·S > agl, else 0. A climb or descent adds
about 2π·agl·|vz|/S_L² per ring boundary. Both simulations reproduce this model.

| layout | v (m/s) | tiles at a pose | adds/s | L-2 | L-1 | L0 | L1 | L2 | L3 | L>=4 |
|---|---|---|---|---|---|---|---|---|---|---|
| v1.3.1 (32 cells, L-2 exact 150 m, L-1 400 m) | 40 | 811 | 14.4 | 4.5 | 4.0 | 3.1 | 1.6 | 0.7 | 0.4 | 0.3 |
| | 90 | 811 | 33.4 | 10.4 | 9.1 | 6.8 | 4.0 | 1.6 | 0.9 | 0.7 |
| | 134 | 811 | **49.9** | 15.6 | 13.3 | 10.3 | 5.8 | 2.7 | 1.2 | 1.1 |
| v1.4 M1 (64 cells, same vertex density, no look-ahead) | 40 | 300 | 4.6 | - | 1.6 | 1.3 | 0.9 | 0.5 | 0.2 | 0.1 |
| | 90 | 300 | 10.6 | - | 3.6 | 3.0 | 2.0 | 1.1 | 0.4 | 0.5 |
| | 134 | 300 | **16.0** | - | 5.4 | 4.5 | 3.1 | 1.8 | 0.8 | 0.5 |
| M1 + M3 capsule, speed scale s = 1 | 134 | 327 | 16.0 | - | 5.5 | 4.5 | 3.2 | 1.5 | 0.7 | 0.6 |
| M1 + M3 capsule, s·v = 90 m/s | 40 / 67 / 90 / 134 | 318 / 333 / 312 / 282 | 4.5 / 7.6 / 10.7 / **13.4** | - | exact 1.6 / 2.7 / 3.7 / 4.7 | | | | | |
| M1 + M3 capsule, s·v = 60 m/s | 134 | 243 | 11.4 | - | exact 3.9 | 3.2 | 2.0 | 1.1 | 0.6 | 0.6 |
| M1 + M3 + M5, plane at 250 m/s, s = 1 | 300 m AGL | 309 | 16.3 (finest L0) | | | | | | | |
| | 1 km AGL | 315 | 9.2 (finest L1) | | | | | | | |
| | 3 km AGL | 250 | 3.2 (finest L2) | | | | | | | |
| | 10 km AGL | 220 | 0.7 (finest L4; square radius, 52 of them L9) | | | | | | | |

Notes:
- The "s·v" rows are the speed-scale controller of M3a (section 3.3) at the scale that equals the old
  fixed VCAP of 90 or 60 m/s; they reproduce the first version of this table within ±0.1/s.
- The flight rows replace the first version (7.5 / 3.3 / 0.8 tiles/s), which used an 80 km radius and
  scaled only the radii. With the cap applied to K as specified then, the plane at 1 km AGL saw L3
  (32 m vertices, 4 m/px) under it; the controller of M3a/M5 scales only as much as capacity requires.
  The 10 km row is recomputed with the disc and horizon cull of M5 when `test_demand.py` gets the M5
  case.
- **Objects.** Perf 1.4 gave ~372 objects for "one level coarser"; that emulation halved K at every
  level of the 32-cell layout and kept the v1.3 near radii, so it kept a 64 m ring. This spec drops
  L-2, uses NEAR_RADII {-1: 150, 0: 400} and the K' table of section 3.1, which gives 300 with no
  look-ahead and 282-333 with the capsule. Budgets use **330 steady, 350 with hysteresis**.
  `test_demand.py` (M0) pins these counts.

Look-ahead does not change the rate: the rate follows from the width of each ring perpendicular to the
motion. Look-ahead only adds slack: at 134 m/s every exact tile on the path is desired 2.4 s or more
(p5 2.6 s) before the car is over it, and 1.8 s or more at 250 m/s [M].

### 2.2 Capacity model, validated on today's data

```
capacity (tiles/s) = fps * b * (1 - heavyLoss) / c_eff(n)
c_eff(n) = Σ_kind share_kind · est_kind(n) + est.material + est.delete + est.matDelete
est_kind(n) = est_kind(n0) · (1 + α·(n − n0))       (n = live + old + deleting objects; R3)
heavyLoss = commits_per_s · L(b) / (fps · b)        (L(b) from R4, with the ramps in force)
```

200 mph drive, at its effective speed [V]:
- c_eff = 9.6 + 1.04 + 0.3 + ~0.05 ≈ 11 ms (at ~1100 objects);
- b = 2 ms; fps ≈ 65;
- heavyLoss = 64 ms per commit every 9.2 s = 64 / (9.2 × 65 × 2) ≈ 5 %.

The model gives 65 × 2 × 0.95 / 11 = **11.2/s against 11.4/s measured**.

### 2.3 v1.4 capacity
Assumptions:
- c_eff(330) ≈ **6 ms** [I]: 5.0 for a 64-cell textured spawn + 0.5 + 0.3 + 0.05. This is a planning
  value, not a measurement: a 64-cell .cdae is 348 KB (3.7x today's 94 KB; 57 B/vertex with u32
  indices, `cdae.py` `MESH_VECTOR_ELEM`), and scaling today's 1.9 ms .cdae + 1.7-2.8 ms DDS by bytes
  gives 9-10 ms. P8 decides (gate in section 3.1). The review model shows what happens between 6 and
  10 ms (section 2.4).
- c_eff(600) = c_eff(330) · 1.43 with α = 0.0016 (the state right after a rebase).
- M4 commits at 134 m/s: mean gap 1.9 s at heading 0°, 3.9 s at 37° [M, the real `follow.lua` with the
  512 table]; the heading-0° value is used. Loss per commit with the M3b ramps (down 0.25, up
  0.2 ms/frame): **L(b) = 4.5·b² + 5·b** = 17.6 ms at b = 1.5, 28 at 2, 55.5 at 3, 72.6 at 3.5.
- `base_e` and b come from the adaptive budget (M3b), which depends on probe P13.

| frame without streaming (`base_e`) | b = clamp(16.4 − base_e, 1.5, 3.5) | heavyLoss at 134 m/s | capacity at 60 fps, c_eff 6 ms | at c_eff 8 ms | at 10 ms |
|---|---|---|---|---|---|
| 15.0 ms (user's 4K today, `lastSplitCastersEnabled` on) | 1.5 | 10.3 % | **13.5/s** | 10.1 | 8.1 |
| 14.4 | 2.0 | 12.3 % | 17.5/s | 13.2 | 10.5 |
| 13.4 (with `lastSplitCastersEnabled` off, -1.6 ms) | 3.0 | 16.2 % | 25.1/s | 18.9 | 15.1 |

At 40 m/s commits come every ~8 s and heavyLoss is 2-4 %. In the air with follow off it is 0.

### 2.4 Margins at 134 m/s
The M3a controller sets the speed scale s so that predicted demand ≤ 0.8 · cap (cap = the mod's
measured capacity, smoothed over 10 s). The margin is therefore ≥ 1.25 by construction while s ≥ S_MIN
(0.3); what varies is the detail. Equivalent fixed cap (s·v) per row:

| case | cap (c_eff 6) | allowed demand | s·v equivalent | exact demand | class-0 margin |
|---|---|---|---|---|---|
| default (b = 2.0) | 17.5 | 14.0 | ~95 m/s | 4.7 | 3.7 |
| worst 4K (b = 1.5) | 13.5 | 10.8 | ~51 m/s | 3.8 | 3.6 |
| lastSplitCasters off (b = 3.0) | 25.1 | 20.1 | 134 (s = 1) | 5.5 | 4.6 |
| worst 4K, c_eff 10 | 8.1 | 6.5 | below S_MIN: s = 0.3 (40 m/s), demand ~9.5 [I] | 3.6 | 2.3 |
| v1.3.1 for comparison (67.6 m/s effective) | ~11 | - | - | - | demand/cap 0.44 |

What protects the ground under the car when demand still exceeds cap (the last row; review model,
straight run at 134 m/s, c_eff = 10 ms, b = 1.5, old VCAP-60 floor [M]):
- Without further rules, the exact tile was under the car in only **40 %** of frames, and the car sat
  on L3/L4 tiles for up to 22 s. Unsent groups merged into one big swap group (up to 64 adds, e.g. an
  L3 tile replaced by mixed L2..L-1 tiles); a group shows only when every add has loaded, and its side
  tiles (class 1 or 2) never got a turn behind the steady class 0/1 stream. The urgent rule loaded the
  exact tile but it stayed hidden.
- Giving every add the group's best class raised that only to 56 %.
- v1.4 therefore uses three rules together: (a) group class inheritance and import by group (M2);
  (b) one level of refinement per group near the corridor, ≤ 4 adds per reveal (M3a); (c) the
  controller with no speed floor (M3a). Integration run D134-c10 (section 3.4) must keep `exactMiss`
  at 0 with `BNGE_DRIVE_TEX_IMPORT_MS=10`.
- With c_eff ≤ 8 ms the review model already passed every ground scenario with exact ground 100 % of
  the time: 67/90/134 m/s straight, a 1 g circle at 134 m/s, 1.5 g at 67 m/s, S-curves, a U-turn
  after braking from 134, and a spin to 67 m/s reversed (queue ≤ 8, backlog ≤ 78, worst frame 33 ms,
  60.6-63.8 fps).

Merges behind the car are class 2 (M2 rule), so in a shortfall they wait and coalesce: children go
straight to the grandparent, which is one import instead of two. The finer tiles behind stay visible
meanwhile, so there is never a hole.

### 2.5 Latency budget of an exact tile at 134 m/s (2.4 s of slack)

| step | worst case | how it is bounded |
|---|---|---|
| pose interval | 0.25 s | 4 Hz above 30 m/s (M3a) |
| server select + ETA sort | 0.02 s | numpy over the to-do list |
| worker wait | ≤ 0.2 s | worker 0 serves classes 0-1 only (M2); a burst of 4 L-1 children at ~40 ms each [I; `bench_build.py` measures] |
| DEM and imagery | 0 when warm; never waits for imagery | capsule prefetch τ ≤ 10 s (M3a); on an imagery miss the tile is built with a texture resampled from the parent or a coarser zoom and retextured later (`far_retex`) |
| window wait | ≤ 2 / cap ≈ 0.15 s | prio 0 has 2 extra slots; a burst of 4 waits for 2 acks |
| token build-up | ≤ 4 tiles × 4 frames = 0.27 s | a 6 ms unit at b = 1.5 needs 4 frames of tokens; units inside the 1 s segment use the urgent path |
| group completion | included above | near the corridor a group has ≤ 4 adds, all class 0 (M2, M3a) |
| crossover | 0 near the car, else 0.5 s | near-car instant rule (M3b) |
| **total** | **≤ 1.4 s** | under the 2.4 s slack; a cold DEM fetch (0.3-2 s) is the one thing that can eat the rest, hence the prefetch |

### 2.6 Post-rebase reload
At 134 m/s rebases come every 112 s (15 km). After a rebase the whole new set (~280-300 tiles) must
load while the old set stays (it is exact after `farfield.transform`), so objects reach ~600.
- Review model, old VCAP-60 floor [M]: at c_eff 6 the backlog clears 74 s after the rebase; at c_eff
  8, 110 s after the rebase 171 of ~250 new tiles were still unshown and ~253 old tiles remained, so
  the next rebase started with 359 old tiles (~600 objects). The first version's "300 / 2.2 = 136 s"
  and its "reload ≤ 60 s" pass rule were inconsistent.
- v1.4 rule (M3a): for 90 s after a rebase, or until the reload is done, the controller holds ground
  demand ≤ 0.6 · cap, which leaves ≥ 0.4 · cap for the reload. Worst 4K with c_eff(330) = 6.2
  (c_eff(~450, the mean during the reload) ≈ 7.4 ms): cap ≈ 10.9/s, reload ≥ 4.4/s, 300 tiles in
  ≈ 69 s. At c_eff(330) = 8 it takes ≈ 89 s.
- Rebase deferral (M3b): the next rebase waits until the reload is done, up to 16 km from the origin
  (origin research: sub-pixel jitter up to 16 km), i.e. ≤ 7.5 s at 134 m/s.
- Old tiles of level ≥ 0 are sunk a little at the rebase (M3b) so old and new tiles never z-fight
  while both are shown.

## 3. Milestones

### 3.0 M0: test harness and baselines
**Problem.** The in-game harness cannot test anything speed-dependent (section 1.1): the car is reset
every step (velocity 0), steps are slower than nominal, and screenshots and resets make > 50 ms frames.
The demand table rests on a scratchpad script.

**Design (harness).**
- **GE-side per-frame driver** `beamngEarthDrive`, installed with the `newExtensionProxy` sink of
  origin research §6 (no mod file; uninstalled with `proxy:destroy()`). Each frame (`onPreRender`):
  - advance the arc length along the profile by `profile.speed(t) · dtSim`;
  - place the player with `veh:setClusterPosRelRot` (no reset) at the profile point and heading;
  - set the velocity with `veh:applyClusterVelocityScaleAdd(-1, 0, vx, vy, vz)`;
  - height: ground profiles use `castRayStatic` from 1000 m above plus the vehicle's clearance (a miss
    is logged as `fall` and the last height is kept); flight profiles use the profile altitude above
    the server's ground (`pose_reply.groundZ`, M5) or above the start height before M5;
  - all inputs stay zero; the camera stays on the player (as today).
  P0 verifies both calls (pose `vx`/`vy` equal the profile within 2 %, no reset events) before any
  other in-game run.
- **MCP only polls**: one snapshot per second at most (perfmon stats, `far.*`, `gov.*`, server
  status). Timing runs take no screenshots; a separate visual run (same profile) takes them. Perfmon
  tags the frames in which the harness itself did MCP work and the summary leaves them out.
- **Profiles** (`--profile file.json`): a list of {t or s, speed, heading, alt (AGL, optional), vz
  (optional)}; `--speed V --heading H --km D` builds a straight one.
- **New samples**: `far.exactMiss`, `far.underLevel`, `far.queued`, `far.waitingGroups`, `gov.cap`,
  `gov.target`, `gov.base_e`, `objects` (live + old + deleting), the server backlog (`status.backlog`,
  desired − shown), `follow.mode`, rebase and commit frame ms, `fall`.
- **Summary** gains: effective speed (km per wall second, and mean pose speed); **a run below 95 % of
  nominal fails**; exactMiss frames (overall, and excluding 2 s after each rebase); max queued; backlog
  slope (tiles/min, least squares); live max; estCdaeTex first-quarter vs last-quarter median; rebase
  reload time (rebase -> far_done); commit and rebase frame ms (separately from the frame histogram).

**Design (offline).**
- `server/tests/test_demand.py`: the [M] rows of table 2.1 (adds/s and tiles within ±10 %) for the
  v1.3.1 and M1 layouts; extended by M3a (capsule, controller) and M5 (flight).
- `server/tests/bench_build.py` (not part of unittest discovery): build time per level (L-1..L7) for
  64 cells with 256 and 512 px, warm (imagery and DEM cached) and cold (empty cache dirs, network),
  single worker. Output feeds the worker-wait row of table 2.5.

**Baselines (in-game, with the new driver, v1.3.1 code).** D40, D67 and D90 profiles of section 5.
They replace "fps ≥ 78" and the other v1.3.1 numbers in this spec. If D90 on v1.3.1 cannot hold
95 % of nominal speed (the frame rate collapses), the run is recorded as a failure with its effective
speed and that is the baseline.

**Probes.** P0, P1, P8 (+P10), P9, P13 (section 7).

**Rollback.** Only `status.backlog` (server) ships; the old `set_position` stepping stays as
`--step-mode mcp`.

### 3.1 M1: bigger tiles (64x64 cells at every level)
**Problem.**
- R1 and R3: 811 objects and 49.9 adds/s at 134 m/s.
- Each import got slower as objects accumulated (4.5 -> 11 ms).
- 2304 tris per tile means many small draw calls (perf 1.1).

**Design.** CELLS = 64 in mesh mode. Each level keeps its size, so its vertex spacing halves, and
selection stays one level coarser at the same distance:

| level | size | spacing | used where (32-cell v1.3 equivalent) | texture (default; P8 may change it) |
|---|---|---|---|---|
| L-1 (**exact**) | 128 m | 2 m = the physics lattice | within NEAR_RADII[-1] = 150 m (was L-2, 64 m) | 256 px (0.5 m/px; USGS is 1.9 m) |
| L0 | 256 m | 4 m | within 400 m (was L-1) | 256 px |
| L1 | 512 m | 8 m | to K(2)·1024 = 1536 m (was L0) | 256 px (2 m/px) |
| L>=2 | 1024 m+ | 16 m+ | K'(L) = K(L-1)/2 | 512 px (keeps today's texel density at distance) |

- K for 64 cells: K' = {1: 0.75 (400 m wins), 2: 1.5, 3: 1.5, >=4: 1.25}.
- NEAR_RADII = {-1: 150, 0: 400}.
- MESH_MIN_LEVEL = -1.
- `torque_split45` parity holds: L-1 origins are multiples of 128 m.
- `build_tile`'s level <= 0 path already handles a lattice step of 1 for L-1 (65² samples) and 2 for
  L0. Its 1 m error reference stays as is.
- Shape per tile: 8192 + 512 skirt tris, 4485 verts, **348 KB `.cdae`** [M, `build64.py`].
- `write_built` asserts `<= 8192` tris, which is the ProceduralMesh rule. Raise it to `<= 9000`:
  .cdae has no such cliff, and the 65k-vertex engine limit is far away.
- **Exact surface.** The L-1 vertices are the physics lattice with Torque diagonals. The engine's
  collision is bilinear per 2 m square (ARCHITECTURE.md, probe `p_diag`), so between vertices the mesh
  differs by |twist|/4 (median 0.4 cm, p99 3.9 cm, max 31 cm on a 145 m-relief block). This is the
  stated tolerance (Targets). A centre vertex per cell would cut it to |twist|/16, but it doubles the
  exact tile (16 384 + 512 tris, ~8.6k verts, ~700 KB) and the exact level is the most imported one;
  it is kept as an option for after v1.4 if P8 leaves room (the `write_built` limit would become
  ~17 000).
- Optional, behind probe P10 (run inside P8): drop tangents from the .cdae (16 of ~57 B/vertex; the
  material has no normal map), about 20 % smaller.
- **Server code takes a layout object, not module globals**: `Layout(cells, block)` (frozen dataclass
  with `k_table`, `near_radii`, `min_level`, `dem_zoom(lat, level)`, `tex_size(level)`) is created by
  `Server` and passed to `select`, `build_tile`, `zoom_for_level` and `heightmap`. Today
  `zoom_for_level` reads `CELLS` at call time (`farfield.py:272-274`), so a module setting would give
  anchors made before and after a change different DEM zooms, and in-process tests would depend on
  their order.
- Terrain mode (`--near-visual terrain`) is frozen at the v1.3.1 behaviour: Layout(32, 1024), L0
  cover logic. It is the legacy and rollback path.

**Gate (P8 before M1 ships).** P8 measures `cdaeTex256`, `cdaeTex512` (and `cdaeTex128`) for 64-cell
tiles at 300 and 600 live objects, cold and warm, and α.
1. If `cdaeTex512` > 12 ms, L≥2 keeps 256 px.
2. If `cdaeTex256` > 7.5 ms, the exact level uses 128 px (1 m/px, still finer than USGS 1.9 m).
3. With those choices, c_eff(330) must be ≤ **6.2 ms**: the worst-4K capacity 80.7/c_eff must be
   ≥ 1.15 × 11.4 (the demand at s·v = 60 m/s), and c_eff(600) ≤ 9.8 ms keeps the post-rebase reload
   ≤ 90 s (section 2.6).
4. If the gate fails, apply the P10 tangent drop and 128 px for L0 too, and re-measure. If it still
   fails, M1 ships anyway only if the D134-c10 integration run passes (`exactMiss` 0 at c_eff 10 ms);
   the controller then runs at lower detail on slow machines, which is logged.

**Protocol.**
- FORMAT 3 -> 4: 64-cell tiles, min level -1.
- `hello.cells` (int) is added under protocol 4; the `SERVER_TO_MOD` table in
  `test_protocol_agreement.py` lists it (the table is exact).
- `far_tile.tex` (int, texture px) so the mod can pick the governor kind (`cdaeTex128/256/512`).

**Data and files.**
- `server/farfield.py`: `Layout`; `K_TABLE[cells]`, `NEAR_RADII[cells]`, `MESH_MIN_LEVEL[cells]`;
  `zoom_for_level(layout, lat, level)`; the `write_built` assert.
- `server/heightmap.py`: functions take `layout` (block size) instead of reading SIZE/HALF/VALID/GRID.
- `server/earthd.py`: `--cells 32|64` (default 64 in mesh mode); `FORMAT = 4`;
  `Server.layout`; `--tex 256,256,256,512` (per level from -1, last value repeats); `TILE_CACHE`
  6000 -> 2500; hello `cells`; far_tile `tex`.
- `mod/.../core.lua`: `FORMATS = {3, 4}`.
- `mod/.../crossover.lua`: `defaultErr` reads the cell count from `hello.cells` (default 32).
- `mod/.../governor.lua`: kinds `cdaeTex128/256/512` with priors from P8 (`cdaeTex` stays for
  FORMAT 3).
- `mod/.../farfield.lua`: the unit kind comes from `far_tile.tex`.

**Tests.**
- Server, `test_farfield`:
  - the 64-cell selection tiles the radius square exactly once;
  - vertex spacing at distance d equals v1.3.1's at 25 sample distances;
  - L-1 vertices equal the heightmap lattice with Torque diagonals;
  - an 8704-tri .cdae round-trips through `cdae.read`.
- Parametrised over Layout(32, 1024) and Layout(64, 512) (M4 adds the block part): `test_farfield`,
  `test_server`, `test_protocol_agreement`. No test mutates module state.
- `test_protocol_agreement`: FORMAT 4 on both sides; `hello.cells` and `far_tile.tex` in
  SERVER_TO_MOD; `crossover.defaultErr` agrees with the server for both cell counts.
- `test_demand.py` (from M0): the M1 rows.
- `test_integration_drive` (mesh):
  - cost model per kind and object count (section 3.4 Tests);
  - assertion (f) becomes: the tile under the player is L-1, and the mesh height at 200 random points
    under the player differs from the **bilinear** physics reference (heightmap samples) by p99 ≤ 5 cm
    and max ≤ the |twist|/4 bound;
  - 8704 tris per tile.
- luajit: `test_crossover` (defaultErr with 64); `test_governor` (kind from `tex`).
- modsim trace additions: none (no rule changes in the mod).

**In-game.**
- P8 first (gate above).
- Then D40 and D67 (section 5): fps ≥ the M0 baseline − 2; estCdaeTex last/first ≤ 1.2; p99 frame
  ≤ 25 ms.
- A pixel diff against v1.3.1 at the 3 fixed v13 camera spots: no visible loss of texel density
  (mean diff ≤ 3).

**Risks.**
- Bigger import spikes: one 5-8 ms unit makes a 20-23 ms frame; a 16 ms L≥2 unit would make a 31 ms
  frame (rule 1 of the gate prevents it).
- A 0.5 m/px exact texture is coarser than today's 0.25 m/px. USGS is 1.9 m, so nothing visible is
  lost; NAIP (0.6 m) later would want 256 px or more.
- Larger swap groups (4 adds) take up to 4 imports before a reveal. M2 fixes the ordering.

**Rollback.** `--cells 32` (server) gives exactly the v1.3.1 layout and FORMAT 3. The mod accepts
FORMAT 3 and 4.

### 3.2 M2: flow control, ETA priority, build capacity
**Problem.** R2: the queue reached 2041 and 1461 groups were waiting. R6: there is no notion of
urgency. R7: a near tile can wait behind far builds.

**Design.**
- **Window.**
  - Every `far_tile` (and `far_retex`, M3a) carries `seq`, a per-connection send counter.
  - `Connection.unacked: {seq: (key, send_time)}`.
  - A built tile is sent only while `len(unacked) < W` (W = `FAR_WINDOW` = 6), or while
    `len(unacked) < W + 2` if its class is 0.
  - The mod acks by seq on import (loaded hidden, or failed), not on reveal, so swap groups larger
    than W still complete.
  - `far_remove` of a key with an unacked send frees that seq's slot. A re-sent key gets a new seq, so
    a late ack for the old copy (`farfield.lua:507-508`, "the newer one wins") frees only the old
    slot: the window can never grow past W + 2.
  - `commit_anchor` clears `unacked`.
  - An ack missing for 10 s frees the slot with a warning, so a lost ack cannot stall the stream.
- **Ready pool.**
  - `Connection.ready: {key: (Tile, TileInfo)}` holds built but unsent tiles.
  - Workers build ahead in priority order up to `W + 2·tile_workers` tiles beyond `unacked`.
  - Each time a slot frees, the sender takes the ready tile with the lowest priority key.
  - A tile that left the desired set stays in the server cache and is never sent: this is the
    coalescing.
- **Reserved worker.** Worker 0 takes only class 0 and 1 builds; workers 1-2 take any class, best
  first. A far build in progress therefore never delays a class-0 tile by more than the class-0/1
  builds ahead of it.
- **ETA.**
  - `eta(t) = min over τ in {0, 0.5, ..., 20} s of [τ + dist(bbox(t), p + v·τ) / V_LAT]`, with
    V_LAT = 30 m/s, vectorised with numpy over the to-do list in `update_far`.
  - class 0: exact-level tiles with eta < 3 s, or the tile containing p.
  - class 1: eta < 10 s, except behind tiles (below).
  - class 2: the rest. **Behind rule**: when |v| > 10 m/s, a tile whose centre is behind the car
    (dot(c − p, v) < 0) and that does not contain p is class 2. Without it, a merge parent 84-225 m
    behind got eta = d/30 = 2.8-7.5 s and class 1: 19-22 % of class-1 entries were behind the car
    [M].
  - **Group class**: every add of a swap group gets the group's best (lowest) class, so the side
    tiles of a group that reveals the exact tile are not starved.
  - Priority key: `(class, started, eta, level, key)`; `started` = the tile's group is partly sent,
    which completes groups inside a class.
  - In terrain mode the gate L0 tiles stay as class -1 (sent as 0).
  - Imagery prefetch (`IMAGERY_PREFETCH`) follows the same order.
- **Mod.**
  - `far_tile.prio` and `seq` are stored on the instance.
  - `peekImport` takes the minimum of `(prio, groupSeq, seq)`, where `groupSeq` is the lowest seq of
    the tile's group, so the mod imports a group's adds together (import by group); the queue holds at
    most W + 2 entries, so the scan is trivial.
  - `farfield` collects acked seqs and `core.lua` sends one `far_ack` per frame when the list is
    non-empty.

**Protocol 5.**
- `hello.protocol = 5`, plus `hello.farWindow` (int, informational).
- `far_tile.prio` (int 0..2), `far_tile.seq` (int).
- New mod -> server message `far_ack {anchorId, loaded: [seq], failed: [seq]}`.

**Data and files.**
- `server/earthd.py`:
  - `PROTOCOL = 5`, `FAR_WINDOW`;
  - `Connection.__init__` (`unacked`, `ready`, `send_seq`);
  - `dispatch` handles `far_ack`;
  - `_regroup`: `prio` becomes the class key, with group class;
  - `_next_tile`: build order, worker 0 restricted to classes 0-1;
  - `_stream`: the send gate and the ready pool;
  - `_retire`/`far_remove` free slots;
  - `commit_anchor` resets the window.
- New pure function `eta_keys(tiles, focus, vel)` in `server/farfield.py` (with the behind rule).
- `mod/.../farfield.lua`: `newInst` (prio, seq, groupSeq), `peekImport`, `M.update` step 4 (append
  to `acks`), `M.takeAcks()`.
- `mod/.../core.lua`: send `far_ack` after `farfield.update`; `PROTOCOL = 5`.

**Tests.**
- Server `test_server`:
  - a client that never acks receives at most W + 2 tiles;
  - acks release exactly that many;
  - a class-0 tile overtakes 20 built class-2 tiles;
  - `far_remove` frees a slot; a re-sent key followed by a late ack for the old seq leaves
    `len(unacked)` ≤ W + 2;
  - anchor switch; ack timeout; unknown or late acks are ignored;
  - worker 0 never runs a class-2 build.
- `test_farfield`: `eta_keys` orders on-path before lateral before behind; a stationary car is
  ordered by distance / 30; a tile 150 m behind at 134 m/s is class 2; a group's adds share the best
  class.
- `test_protocol_agreement`: `far_ack` in MOD_TO_SERVER; `prio` and `seq` in far_tile; the window
  constants.
- `test_integration_drive`: mod pending ≤ W + 2 at every frame; server backlog slope ≈ 0 over the
  drive.
- luajit `test_farfield`: import order by (prio, groupSeq, seq); acks contain loaded and failed seqs
  exactly once; groups larger than W complete.
- modsim: port the ack, seq, prio and window rules. **Trace additions** (`lua/farfield_trace.lua`):
  acks with seq, prio order, window full / late ack / re-sent key.

**In-game.** D90 (section 5):
- `queued` ≤ 8 at all times (it was 2041);
- waiting groups ≤ 10;
- live ≤ 1.2 × desired;
- estCdaeTex last/first ≤ 1.2 over 20 km.

**Risks.**
- The ack round trip limits throughput if W is too small. 6 tiles is 0.4-0.7 s of imports against a
  localhost round trip under 20 ms.
- Starving class 2 grows the live count behind the car. It is bounded by coalescing; the check is
  live ≤ 1.2 × desired at the end of D134.

**Rollback.** `--far-window 0` = unlimited (v1.3.1 behaviour); the mod's ack is then ignored.
`--prio distance` = v1.3.1 order.

### 3.3 M3a: server look-ahead, the demand controller, the exact corridor
**Problem.**
- R1 at 134 m/s even after M1 (16/s at full detail).
- R6: nothing guarantees exact ground under or ahead of the car.
- A fixed speed cap with a floor (first version: VCAP ≥ 60) cannot follow the machine: at c_eff 10 ms
  it still asked for 11.4 tiles/s against 9.1 [M], and it shrank K at altitude where no ground detail
  was needed.

**Design (server).**
- **Capsule distance.**
  - d = dist(bbox, polyline p + v·τ, τ = k·T/8, k = 0..8), where T = T_LA[level of the children]:
    2.0 s for children ≤ L0, 1.0 s otherwise.
  - It replaces the point distance in the split test, and the hysteresis uses the same d.
  - Above 30 m/s the mod sends poses at 4 Hz (`POSE_INTERVAL_FAST` = 0.25 s).
- **Demand controller (replaces VCAP).**
  - A speed scale s ∈ [S_MIN = 0.3, 1] multiplies K(L) for L ≤ 3 and NEAR_RADII[0]; radii[-1] never
    drops below 64 m, so the car's tile and its neighbours stay exact.
  - Predicted demand D(s) = Σ_L ring rate (section 2.1) with the scaled radii, horizontal speed and
    (M5) agl and vz. `test_demand.py` checks D(s) against the simulated rate within ±15 %.
  - s = the largest value with D(s) ≤ 0.8 · cap_s, where cap_s is the pose's `cap` smoothed with a
    10 s time constant. During the post-rebase reload (≤ 90 s, until the old set is gone) the
    target is 0.6 · cap_s (section 2.6).
  - Rate limit: s changes by at most 10 % per 5 s. A decrease applies to new splits only: the merge
    test uses s_hold = max(s over the last 30 s), so lowering s never triggers a burst of merges
    (the review saw 40-60 imports at once when VCAP went 93 -> 60 at 134 m/s, and a backlog of 69 when
    braking from 134 to 40 at c_eff 8 [M]).
  - With no `cap` in the pose (an M2 mod), s = 1 below 40 m/s and 40/|v| above, the conservative fixed
    rule.
- **Exact corridor.** The exact level is selected within radii[-1] of the capsule (T = 2 s), so exact
  tiles on the path are requested 2.4 s or more ahead at 134 m/s. ETA class 0 (M2) delivers them
  first.
- **One level of refinement per group near the corridor.** When a swap group's footprint is within
  NEAR_RADII[0] of the capsule, the server emits at most one level of change per group: a split into
  its 4 children (≤ 4 adds), or a merge of 4 children into their parent. A shown tile two or more
  levels coarser than desired is first replaced by its children, even if they are not in the desired
  set; the next update refines them. This keeps reveals near the car small (no 64-add groups) and
  bounds the instant pop under the car (M3b) to one level. Farther out, groups are formed as today.
  Cost: an intermediate tile is imported only when the selection jumps more than one level, which
  happens after a shortfall, a U-turn or a teleport.
- **Capsule prefetch (DEM and imagery).** For exact and L0 tiles along p + v·τ, τ ≤ 10 s, the server
  prefetches the heightmap-zoom DEM and the imagery tiles in a separate prefetch pool (2 threads),
  independent of the build queue. The existing `IMAGERY_PREFETCH` stays for the rest.
- **Never wait for imagery in an exact tile.** If a class-0/1 tile's imagery is not in the cache when
  its build starts, the tile is built with a texture resampled from the parent tile's texture or from
  the coarsest cached zoom, and the full imagery is fetched in the background. When it arrives the
  server sends `far_retex {anchorId, key, seq, tex, texture}`; the mod swaps the material's colour map
  (probe P8 times it as unit kind `retex`; fallback if P8 shows it does not work: re-send the tile as a
  class-2 replacement of itself).
- **Post-rebase reload reserve.** See section 2.6; the server knows the reload is running while any
  `old` tile is reported in the pose (`old` count, already in stats).

**Protocol 5.**
- `pose` gains `vz` (num), `cap` (num, tiles/s), `old` (int, old-anchor tiles still live).
- `hello` gains `exactLevel` (int: −1 for 64 cells, −2 for 32).
- New server -> mod message `far_retex {anchorId, key, seq, tex, texture}` (acked by seq like a tile).

**Data and files.**
- `server/farfield.py`: `select(layout, ..., vel=(0, 0), agl=0.0, lookahead=T_LA, scale=1.0,
  scale_hold=1.0)`, `switch_distance` with the scale, `demand_model(layout, v, agl, vz, scale)`,
  `limit_steps(groups, capsule)` (one-level rule).
- `server/earthd.py`: `on_pose` (vel, cap, old -> controller); `SpeedScale` controller class;
  `update_far` (passes vel, scale); prefetch pool; texture fallback in the build; `far_retex` sender.
- `server/imagery.py`: `resample_from(parent_texture | coarser zoom)`.
- `mod/.../core.lua`: pose interval and fields; `far_retex` dispatch.
- `mod/.../farfield.lua` + `satmat.lua`: `retex` unit (swap the colour map of the tile's material).

**Tests.**
- `test_demand.py`: the capsule rows of table 2.1 within ±10 %; D(s) within ±15 % of the simulated
  rate at 40/90/134 m/s and s ∈ {0.3, 0.5, 1}.
- `test_farfield`:
  - capsule: a tile 250 m ahead on the path is exact at 134 m/s, the same tile lateral at 250 m is
    not;
  - the scale never drops radii[-1] below 64;
  - hysteresis stays symmetric under capsule distance; s_hold prevents merges on a scale decrease;
  - `limit_steps`: near the capsule no group has more than 4 adds or changes more than one level.
- `test_server`: the controller holds D(s) ≤ 0.8 cap for cap ∈ {8, 13.5, 17.5, 25}; rate limit;
  reload reserve; a class-0 build with no imagery cached completes without a network call and is
  followed by a `far_retex`.
- `test_integration_drive` (see M3b for the drive cases).

**Rollback.** `--lookahead 0 --scale fixed` (server): point distance and the conservative fixed rule.
`--retex off`: exact tiles wait for imagery as in v1.3.1.

### 3.4 M3b: urgent import, the near-car rule, the adaptive budget (mod)
**Problem.**
- R6 on the mod side: the exact tile can arrive and still not be shown in time, and the C2 crossover
  moves it vertically under the car.
- The fixed 2 ms budget is wrong at both a 10 ms base (wasted) and a 15 ms base (fps below 60).
- After a teleport the world fills while the player can already drive (hole under the car for 0.2 s
  warm, 0.33 s with cold imagery, the whole world blank at first [M]).

**Design (mod).**
- **Urgent import.**
  - A queued prio-0 tile whose footprint (+20 m) meets the segment [p, p + v·1.0 s] may run
    `gov:tryUrgent(kind)`: one unit per frame, even without tokens.
  - Not allowed in heavy or quiet frames, **not once a pending heavy request has ramped b to 0**
    (otherwise ~5 urgent units/s would keep restarting the `QUIET_BEFORE` wait of the terrain commit,
    `governor.lua:210`, exactly when the block edge matters), and not on top of a unit larger than b in
    the same frame.
  - Tokens may then go negative, down to −2·maxUnit, so later frames pay it back.
- **No crossover under the car.**
  - `startGroup` runs a group instantly (C1) when any of its adds or removes meets the segment
    [p, p + v·0.75 s] + 20 m (`crossover.NEAR_CAR_INSTANT_S = 0.75`).
  - The pop is one level (the M3a one-level rule): coarse -> exact is the L0 tile's error at the car,
    bounded by its `TileInfo.err` (≤ 1 m by the build's error reference, typically cm to dm); L1 -> L0
    near the car happens only after a shortfall and is bounded by the L1 tile's err. `test_farfield`
    records the per-level bound on the test terrain.
- **Adaptive budget** (after probe P13).
  - The governor keeps `base_e` = median over 64 frames of (wall − k·frameSpent), where k =
    d(wall)/d(spent) from P13 (k = 1 until measured; at the user's 15 ms frame with CPU ~10 ms and GPU
    ~8.3 ms the main thread has slack, so k < 1 is likely, and vsync at 239 Hz quantises wall time).
  - With work queued: target = clamp(FRAME_GOAL_MS − base_e, 1.5, 3.5), FRAME_GOAL_MS = 16.4.
  - Without work: ramp down as today.
  - `beamngEarth_core.setFarBudget(ms)` pins the target (autoTarget off); `setFarBudget(0)` returns
    to auto.
- **Faster heavy ramps** (the commits get small in M4): RAMP_DOWN_HEAVY 0.1 -> 0.25 ms/frame; after a
  heavy slot, ramp up at 0.2 ms/frame. Loss per commit L(b) = 4.5·b² + 5·b (section 2.3).
- **cap.** Sent in the pose:
  `cap = fpsAvg · target · (1 − heavyLoss) / c_eff`, c_eff = the import-kind mix of the last 600
  imports weighted by their `est`, plus material, delete and matDelete.
  heavyLoss = Σ(target − b) / Σ target over the last 600 frames **that had work or were inside a
  heavy window** (from the ramp-down start to the end of the ramp-up). Idle frames do not count, so a
  car accelerating from rest does not report cap ≈ 0.
- **Metric `exactMiss`.**
  - Each frame, `farfield` finds the shown current-anchor tile containing (p.x, p.y). Only the previous
    tile and its neighbours are checked: O(1).
  - The frame counts as a miss if that tile is not the exact level, or is in a crossover with
    |off| > 1 cm. An old-anchor exact tile counts as exact.
  - Exposed in `stats()` along with `underLevel`.
- **Teleport hold.** After a teleport (`anchor_ready` purpose teleport, or a jump > 2 km within one
  frame), the player vehicle is frozen and the screen faded until the tile under the player and its 8
  neighbours are shown at the exact level and no class-0/1 tile is queued, or 10 s pass. The freeze and
  fade calls are verified by probe P14.
- **Old-tile sink at a rebase.** `farfield.transform` lowers each old tile of level L ≥ 0 by
  `OLD_SINK_FRAC · S_L` (0.00025: 6 cm at L0, 0.5 m at L3, 8 m at L7) so new tiles win the depth test
  where both are shown during the reload. Old exact (L-1) tiles are not sunk; they are replaced first
  (class 0). A level is used from about 1.5·S_L, so the sink is about 0.35 px at 4K (0.9 px for L0 at
  150 m) [I]. The visual run of D134 checks flat ground and water for z-fighting during the reload; if
  any is seen, the fraction doubles for L ≥ 1.
- **Rebase timing.** `rebase.PREFETCH_MARGIN` becomes max(300 m, 8 s · |v|) (at 134 m/s 1072 m,
  i.e. 8 s to survey, build the terrain and fetch the DEM; at 250 m/s 2 km). The next rebase is
  deferred while a reload is running, up to 16 km from the origin (section 2.6).

**Data and files.**
- `mod/.../governor.lua`: `base_e` history with k, `autoTarget`, `tryUrgent` (with the heavy rule),
  the ramp constants, `capacity()` with the work-frame heavyLoss and the kind mix.
- `mod/.../farfield.lua`: `startGroup` near-car rule, urgent path in step 4, `exactMiss`, old-tile
  sink in `transform`.
- `mod/.../crossover.lua`: `NEAR_CAR_INSTANT_S`.
- `mod/.../core.lua`: player pos/vel passed to `farfield.update`; teleport hold; `setFarBudget(0)`.
- `mod/.../rebase.lua`: `PREFETCH_MARGIN` by speed; deferral while reloading.

**Tests.**
- luajit:
  - `test_governor`: auto target from `base_e` (with k); `tryUrgent` once per frame and never in
    heavy or quiet frames; **no urgent once `heavyPending` has ramped b to 0** (the commit is granted
    within QUIET_BEFORE + 2 frames under a steady urgent stream); the negative-token floor; the new
    ramps; heavyLoss ignores idle frames (cap after 600 idle frames then work equals cap before);
  - `test_farfield`: near-car instant crossover; urgent import; `exactMiss` counting; old-tile sink;
  - `test_rebase`: prefetch margin by speed; deferral up to 16 km.
- `test_core_stubbed`: teleport hold releases on exact cover and on timeout.
- `test_integration_drive`, mesh, **real time (speed-up 1)** for every case at ≥ 90 m/s (the
  default 4x speed-up makes a 134 m/s drive a 536 m/s wall-time demand on the server). Cost model:
  c(kind, n) = prior(kind) · (1 + α·(n − 300)), `BNGE_DRIVE_ALPHA` (default 0.0016),
  `BNGE_DRIVE_TEX_IMPORT_MS` (the 256 px prior, default 6), `BNGE_DRIVE_COMMIT_MS` and
  `BNGE_DRIVE_REBASE_MS` per block size (1024: 46 and 71; 512: 18 and 44 until P2/P7 replace them),
  base 15 ms. Cases:
  - D134: 30 km, 2 rebases; exactMiss = 0 outside 2 s after a rebase; pending ≤ W + 2; live ≤ 400;
    no frame over b + maxUnit + one urgent unit; reload done before the next rebase;
  - **D134-c10**: the same with `BNGE_DRIVE_TEX_IMPORT_MS=10`; exactMiss = 0;
  - transients: accelerate 40 -> 134, brake to 40, U-turn, spin; exactMiss = 0, backlog back to < 20
    within 30 s;
  - TELEPORT: no frame with the player unfrozen over a hole.
- modsim **trace additions** (`lua/modsim_trace.lua`, `lua/farfield_trace.lua`): `tryUrgent` and
  negative tokens; urgent blocked by a pending heavy request; near-car instant swaps; `exactMiss`;
  `autoTarget`/`base_e` with k; heavyLoss over work frames.

**In-game (M3a + M3b together).**
- D134 (section 5): fps mean ≥ 60, frames > 50 ms = 0 outside commit and rebase frames, exactMiss = 0
  except ≤ 2 s after each rebase, pops = 0, reload done before the next rebase.
- D40 and D67: no regression against the M0 baseline (fps ≥ baseline − 2, p99 ≤ 25 ms).
- TELEPORT: no hole while unfrozen; unfrozen within 5 s warm, 10 s cold.

**Risks.**
- The controller coarsens visible detail at speed on slow machines (worst 4K: about the old VCAP 51
  at c_eff 6). Accepted at 300 mph and checked on screenshots of the visual run.
- Urgent units make 20 ms frames; at most about 5/s, and only when the corridor is late.
- Look-ahead on sharp turns requests tiles that are not needed: about 10 % extra at worst, and they
  coalesce away.

**Rollback.** Governor `autoTarget = false` through `beamngEarth_core.setFarBudget(2)`. Urgent and
near-car rules apply only to prio-0 tiles, so a server that sends no prio disables them.

### 3.5 M4: cheaper terrain-follow (512² block on a 128 m grid)
**Problem.** R4: 46-48 ms of commit work (60-65 ms frame) every ~540 m, and a 71 ms rebase.

**Prerequisite probes.**
- **P2 (extended)**: in a session with the live 1024² block, re-import 512² into it and back; time the
  first and the steady-state imports, with and without the layer map; check with `castRayStatic` that
  the collidable extent is [c−512, c+510] after the switch and that `setRenderEnabled(false)` survives.
  `updateGrid` takes 37 ms per 1024² (ARCHITECTURE.md), so most of a commit may be grid rebuild even
  with rendering off; P2 tells.
- **P7 (a)**: the timing wrapper of origin research (P7 code) around `terrain.load`,
  `farfield.transform`, `farfield.switchAnchor`, `camshift.shift`, `be:reloadCollision` and phase 1's
  `moveVehicles`, over 3 rebases. 1276 tiles × 0.006 ms explains only ~8 ms of the ~26 ms remainder.
  Phase 1 and phase 2 can land in the same frame (`rebaseLag` 0), so the wrapper records the frame
  number of each.
- **P7 (b)**: double-buffered terrain: a second hidden TerrainBlock imported ahead of time, then
  swapped (does the hidden block collide before `reloadCollision`?). If it works, a commit or a rebase
  becomes a ~2 ms swap plus `reloadCollision`, and the import moves to a heavy slot of its own ahead of
  time.
- **P11**: a commit without `reloadCollision` (does the car stay on a re-imported block?).

**Design.**
- N = 512 at 2 m: collidable [c−512, c+510], 1022 m wide.
- Centres are multiples of 128 m. The SW corner is then a multiple of 128 m, which is an even lattice
  index, so `torque_split45` parity holds.
- Follow table for size 512 (flat constants, section "Data and files"):

| constant | 1024² (v1.3) | 512² (v1.4) | 512² fast mode (|v| > 150 m/s) |
|---|---|---|---|
| GRID | 256 | 128 | 128 |
| EDGE_MARGIN, REACH | 600, 420 | 250, 260 | 90, 420 |
| LOOKAHEAD_S | 4 | 2 | 3 |
| prepare test point | p | p | p + v·0.75 s |
| PREPARE / COMMIT / HARD_COMMIT (Chebyshev from the centre) | 300 / 450 / 700 | 128 / 192 / 320 | 128 / 192 / 320 |

- Commit-time re-check: a ready pending block is discarded (and re-prepared) if
  cheb(p + v·2 s, pending) > PREPARE. Without it the 512 table gave commits 0.40 s apart at 134 m/s,
  heading 37° (two 33 ms frames) [M].
- Measured cadence [M, real `follow.lua`, before the re-check]: at 134 m/s mean commit gap 1.9 s at
  0°, 2.4 s at 20°, 3.9 s at 37°, minimum 0.40 s. `test_follow` asserts a minimum gap ≥ 0.9 s at
  134 m/s for headings 0-45° with the re-check. Section 2.3 uses 1.9 s.
- Speed limits on the ground [M]: the time from prepare to leaving the block is 2.4 s at 134 m/s. In
  fast mode, 200 m/s passes at 1.2 s heightmap latency (min edge margin 196 m at 0°); 250 m/s
  passes with warm latency (0.3 s: margin 214 m at 0°) but at 1.2 s fails at headings 0-10° (up to 29
  frames off the block; 20-45° pass). A single pending block per cycle bounds this. Hence the M5 heightmap-zoom DEM
  prefetch along the path (τ ≤ 30 s) on the ground at ≥ 100 m/s, and the stated target (≤ 200 m/s on
  the ground; 250 m/s needs a warm DEM).
- Commit cost [I]: ~11 ms fixed + 27 ns/sample gives ~18 ms (P2), and `reloadCollision` 0.4 ms. The
  frame becomes about 33 ms at a 15 ms base.
- **Rebase frame.** Work = 512² import (~18 ms) + the remainder P7(a) explains (~26 ms today) +
  vehicles: ~44 ms of work, a ~59 ms frame. The ≤ 45 ms target needs either the double buffer of
  P7(b) (the import leaves the rebase frame) or P7(a) finding the remainder and cutting it to ≤ 10 ms.
  If neither works, the target is ≤ 60 ms, one frame per rebase (every 112 s at 134 m/s), reported
  separately.
- **Rebase table per block size** (`rebase.lua`): STALE_DIST 450 (1024) / **192** (512). Today a
  rebase could commit a block 449 m from the player, leaving 61 m (0.45 s at 134 m/s) before the car
  leaves the collidable area.
- **Stale anchors do not loop.** A prepared anchor whose terrain centre is farther than STALE_DIST is
  still committed if the player is inside its valid area with ≥ 150 m to every edge, and a follow
  prepare is issued at once; otherwise it is discarded and re-requested with its terrain at
  p + v·(measured anchor latency + 1 s). Today a cold `prepare_anchor` over 3 s at 250 m/s is
  discarded forever.
- With follow off (M5, in the air), a rebase does not apply the staleness check.
- The 1024² path stays available for terrain mode and as the rollback.
- **Gate**: if P2 measures a 512² import above 30 ms, M4 does not ship as is; P7(b) and P11 decide the
  alternative.

**Protocol.**
- `terrain.size` is already sent, and the mod picks the follow table from it.
- hello gains `blockSize` (int) so the mod can choose before the first terrain.

**Data and files.**
- `server/heightmap.py`: everything takes `layout.block` (no `configure`).
- `server/earthd.py`: `--block 512|1024` (default 512 in mesh mode); `FOLLOW_PREPARE`, `FOLLOW_LEAD`,
  `FOLLOW_REACH` from the layout; `predict_request`, `PREFETCH_START` (= PREPARE − 28 m);
  `ANCHOR_CENTRE_MAX` unchanged.
- `mod/.../lattice.lua`: `configure(size)` sets N, HALF, VALID_W, COVER_W, GRID.
- `mod/.../follow.lua`: flat constants `PREPARE_DIST_512 = 128`, `COMMIT_DIST_512 = 192`,
  `HARD_COMMIT_DIST_512 = 320`, `EDGE_MARGIN_512 = 250`, `LOOKAHEAD_S_512 = 2`, `GRID_512 = 128`,
  `FAST_V = 150`, `EDGE_MARGIN_FAST = 90`, `LOOKAHEAD_FAST_S = 3`, `PREP_AHEAD_FAST_S = 0.75`, and the
  `_1024` set; `follow.configure(size)` sets the active `M.*` values **and every derived local**
  (`REACH`, `SIZE`, `HALF`, `GRID`; today `follow.lua:29` captures `local REACH`, and `terrain.lua`
  and `follow.lua` copy `lattice.N` at load). This keeps `modsim.lua_consts` (`modsim.py:79-97`,
  `M.X = expr` and flat tables only) and `test_protocol_agreement.py:202-207` working.
- `mod/.../terrain.lua`: `LAYER_MAP` and `FLAT_HEIGHTMAP` per size; **`loadFlat` uses the session
  block size** (level start imports a 1024² flat map today, `terrain.lua:29-30, 124-127`,
  `core.lua:704`).
- `mod/.../core.lua`: `anchorTerrain` accepts the session block size (`core.lua:155` rejects any size
  other than `terrain.SIZE`); `lattice.configure` from `hello.blockSize` before anything else.
- `mod/.../rebase.lua`: `STALE_DIST_512`, `STALE_DIST_1024`; the stale-anchor rule.
- `tools/make_level_assets.py`: `beamngEarth_layer_512.png`, `beamngEarth_flat_512.png` (the layer map
  must match the heightmap size, `lattice.lua:214`).

**Tests.**
- Server `test_heightmap` (parametrised over the two layouts): 512² blocks share bit-identical
  samples across 128 m moves; parity of the SW corner; q/zBase rules unchanged.
- `test_protocol_agreement`: both follow tables equal on both sides (flat constants); `blockSize` in
  hello; STALE_DIST per size.
- `test_core_stubbed`: an `anchor_ready` with size 512 is accepted after `hello.blockSize = 512`;
  `loadFlat` imports the 512 flat map.
- `test_integration_drive`: assertion (a) for 512²; no late terrain at 134 m/s; the car never within
  150 m of a block edge; 200 m/s with 1.2 s heightmap latency in fast mode stays on the block; anchor
  latency 3 s at 250 m/s ends in a rebase (no loop).
- luajit `test_follow`: the 512 table (prepare/commit/hard-commit/discard at the 134 m/s and 40 m/s
  geometry); fast mode; the commit-time re-check; `configure` switches tables and derived locals;
  `test_rebase`: STALE_DIST per size and the stale-anchor rule.
- modsim **trace additions**: the 512 follow table, fast mode and `configure`; the stale-anchor rule.

**In-game.**
- P2, P7, P11 first.
- Then D134: every commit frame ≤ 40 ms; rebase frame per the rule above; frames > 50 ms = 0
  otherwise; the car never falls (the driver's `castRayStatic` hits at every frame).
- The fixed-camera pixel diff across a commit stays at v1.3 levels (mean ≤ 3.4); the block is
  invisible.

**Risks.**
- P2 shows that the fixed part of `importMaps` dominates (512² ≈ 1024²); see the gate.
- A car flung far, such as a crash at 134 m/s, could leave the block between commits. Mitigation: the
  hard commit at 320 m, plus the urgent commit in M5.

**Rollback.** `--block 1024` (server). The mod follows `hello.blockSize` and `terrain.size`.

**Implementation notes (built offline 2026-10-10, docs/ARCHITECTURE.md "v1.4 M4").**
- `--block` defaulted to 1024 until P2, P7 and P11 passed (`tools/m4_probes.py`, in-game 2026-10-10);
  since then the command line defaults to **512** in mesh mode (`resolve_block`; `Server()` keeps 1024).
  `hello.blockSize` is sent only with 512, so a 1024 session's messages are unchanged.
- The commit-time re-check as written above discarded nearly every block in the offline model (77 of
  78 commits at 134 m/s, 0.3 s latency) and took a 250 m/s car up to 98 m off the block at 1.2 s
  latency. It is implemented with a guard: it discards only while the player stays out of the 150 m
  edge band (Chebyshev 360 m from the centre) for longer than the measured heightmap latency
  (`terrain_ready` - `prepare_terrain`, 1 s until measured) + 0.5 s, never past the hard commit
  distance and not in fast mode. Result (real follow.lua under luajit): at 134 m/s, headings 0-45°,
  commits >= 2.6 s apart at 0.3 s latency, >= 166 m inside the edges at 0.3-1.2 s; 200 m/s at 1.2 s
  >= 216 m inside.
- Review (2026-10-10, offline, the real follow.lua with the latency of each request drawn from 0.3-1.2 s
  and the commit 0-0.15 s later in a heavy slot): two holes, both 512² only, fixed in follow.lua and
  the modsim port. (1) A ready block that is no better than the current one (typically prepared from
  ~130 m behind the centre, one grid step to the side of the way the car goes) was never committed
  and blocked every new request until it left the reach (260 m, fast mode 420 m: 250 / 90 m from
  the edge): 76-93 m from the edge at 134 m/s, and a car accelerating from 100 to 200 m/s left the
  block by up to 121 m. Such a block is now dropped when it would still be no better where the player
  reaches the hard commit distance (straight on), so the next frame prepares the target. (2) The
  re-check guard used the last measured latency, which says little about the next request: it now
  assumes at least LATENCY_S (1 s), and it never discards a block only to re-request the same
  centre. Result (`test_follow`): jittered latency, all headings: >= 171 m inside at 134 m/s, >= 178 m
  accelerating 100 -> 200 m/s; fixed latency: 134 m/s >= 199 m (0.3-1.2 s), commits >= 0.95 s apart at
  0.3 s (the guard re-prepares less at warm latency: ~1 commit per 2.4 s at 134 m/s in the model, vs
  §2.3's 1.9 s), 200 m/s at 1.2 s >= 216 m, 250 m/s at 0.3 s >= 254 m.
- The stale-anchor rule and the re-check apply to 512² only (1024² keeps v1.3.1's rules).

### 3.6 M5: altitude-aware LOD, physics follow in the air, far field to the horizon
**Problem.** Planes fly up to about 10 km at 250+ m/s:
- the ground-based LOD would stream exact tiles under a plane;
- terrain commits every 1.5 s are useless at altitude;
- a plane can fly into a ridge or dive into the ground where no physics block is;
- the 80 km radius and 100 km visibleDistance end long before the horizon, which is 357 km away at
  10 km altitude.

**Design (server).**
- **Ground height without blocking.** `on_pose` runs on the asyncio loop (`earthd.py:806`), and
  `dem.sample` blocks on network fetches with retries (`dem.py:90-100`, 20 s timeout). So ground
  heights come **only from cached data**: the finest shown tile containing the point
  (`TileInfo.grid` + `surface_at`), else the coarsest cached DEM zoom (computed in the executor at
  z ≤ 12, never fetched in `on_pose`). Unknown = null.
- **Clearance and time to impact.** Over τ = 0, 0.25, ..., 8 s:
  `h(τ) = z + vz·τ − ground(p + v·τ)`. `clearance` = min h(τ); `tti` = the first τ with h(τ) ≤ 0
  (null if none); `impact` = p + v·tti. Sent in `pose_reply`.
- **Path prefetch.** The heightmap-zoom DEM (and imagery for exact/L0 tiles) is prefetched along
  p + v·τ, τ ≤ 30 s, whenever agl < 3 km, and on the ground at ≥ 100 m/s.
- **3D capsule LOD.** For each τ sample, agl(τ) = max(0, h(τ)); d3 = min over τ of
  hypot(dist_xy(bbox, p + v·τ), agl(τ)). This is the split distance; with vz = 0 and agl = 0 it is the
  M3a capsule.
- **Exact level in the air.** Exact tiles are selected while min agl(τ) < max(60 m, 3 s · max(0, −vz)),
  i.e. at least 3 s before touchdown (the first version's fixed 60 m gave 0.36 s in the DESC profile),
  with hysteresis (off at 1.5× that), and never dropped below 150 m AGL while the horizontal speed is
  < 100 m/s (a car jumping off a 100 m cliff keeps them). For the exact level T_LA is 3 s when
  vz < −5 m/s.
- **Demand controller in 3D.** The ring model of section 2.1 with horizontal radii
  r_L = sqrt((K·S)² − agl²) and the vertical term; s scales K for L ≤ 3 only as far as demand
  ≤ 0.8 · cap requires. No |v|-based cap: at 1 km AGL and 250 m/s the unscaled demand (9.2/s, finest
  L1) fits the default machine, so the plane sees L1, not L3.
- **Radius: the real line-of-sight horizon** (user, 2026-10-10: "view distance as close to real life
  as possible"; moved forward as milestone **M1h**, see the table in section 0 - it does not depend on
  the flight follow modes and applies on the ground too).
  - Heights are measured from **sea level (the ellipsoid)**, not from the ground under the player:
    curvature drops a point by d²/2R' regardless of the local ground. With AGL, a player on Mount
    Whitney (eye 4417 m MSL, AGL 1.7 m) got 80 km; the real view is ~308 km (San Gorgonio, 3342 m) and
    ~300 km (Diablo Range), median over directions 194 km [M: scratchpad `viewshed.py`, DEM z7, refraction
    k = 0.13, R' = R/(1−k) = 7323 km].
  - Per-tile horizon test: a tile is kept only if its nearest point is within
    d_h = sqrt(2R'·h_eye) + sqrt(2R'·max(0, zmax_tile)), with h_eye and zmax_tile in metres above sea
    level (zmax from the tile's coarse grid, or a coarse regional max for tiles not built yet). This is
    exact for the earth's bulge; terrain in between can still hide a tile (no viewshed: the depth buffer
    handles occlusion, and occluded coarse tiles cost little).
  - RADIUS = clamp(sqrt(2R'·h_eye) + sqrt(2R'·zmax_region), 80 km, 1500 km), zmax_region = max height
    within RADIUS from a coarse (z5-z6) DEM pass, cached per anchor. Examples: Whitney summit ~480 km
    bound (actual ~308 km, the per-tile test trims the rest); Central Valley floor (h_eye ~110 m) with the
    Sierra (4.4 km) ~ 280 km; open ocean at sea level ~ 5 km + nothing; Everest-region extremes ~690 km.
  - Selection within a **disc** (not a square: a square put 49-64 L9 roots at 400 km with corners at
    566 km, past `visibleDistance` and the horizon, ~21 % of the area [M]).
  - Levels: MAX_LEVEL 10 (262 km tiles, 64 cells = 4 km vertex spacing, enough for a silhouette at
    300+ km). Textures for L >= 8: 128-256 px from EOX z5-z7 (a 256 px L9 tile is 512 m/px; at 300 km a
    pixel on screen is ~100 m, so the texture is never the limit). Count beyond 80 km: ~40-90 tiles,
    each one draw call; built once per anchor, rarely swapped (they change only with large moves).
  - Float32: tile origins at 700 km have a ULP of 6 cm; vertices are tile-relative: fine.
  - **Engine finding (in-game 2026-10-10, docs/research/view_distance/):** assigning
    `scenetree.theLevelInfo.visibleDistance` at runtime does NOTHING by itself: the engine keeps the
    level file's value (100 km) until `scenetree.theLevelInfo:postApply()` is called. With postApply,
    markers 200 / 400 / 800 / 1600 / 3200 km away render (crisp, haze-tinted, no depth artefacts) from
    10 km altitude; without it everything past ~100-150 km is cut. Fog density (4e-5) was not the cause.
    So every visibleDistance change must be followed by postApply (also today's 100 km assignment in
    core.lua activateWorld is a no-op beyond the level value).
  - Radius cap raised from 700 km to **1500 km** (user: planes see much farther): 10 km altitude ->
    ~360 km to the sea-level horizon, ~500 km to mountain tops; 20 km -> ~800 km; 1500 km covers eyes
    up to ~40 km. Above that the far field stays at 1500 km.
  - `visibleDistance` = RADIUS + 10 %, set at runtime (then postApply) when the radius changes (probe P12 checks cost,
    shadow cascade fit and depth artefacts with nearClip 0.1 at 300-700 km).
  - **Sky horizon = ground horizon** (user, 2026-10-10). The 0.39 sky is a physical atmosphere around a
    sphere: it places the camera at altitude camPos.z above a planet of `ScatterSky.atmoPlanetRadius`
    (shaders/common/sky/skyLutSkyP.hlsl:28, RenderSkyRayMarching.h.hlsl:456-495, which also clamps
    below-horizon view directions to the tangent), so its horizon already dips with height. In our
    frame z is the height above the anchor's tangent plane, i.e. ~ altitude (off by d²/2R: 17.7 m at the
    15 km rebase limit, a 0.002° dip error at 4 km altitude), and our tiles are curved with R = 6371 km.
    A distance-driven rebase that is still preparing after 60 s expires once the player is past the
    rebase distance. The client retries near the current player position after 5 s and ignores replies
    for the expired request. The terrain build's heartbeat does not extend this deadline (see #1467).
    The running level has atmoPlanetRadius = 6360 km [V, in-game]: set it to 6371 (celestial.lua:392
    applyAtmosphere `planetRadiusKm`, or the field directly at level start) so both horizons use the same
    sphere. The visible mismatch today is the gap between the far field's 80 km edge and the sky's
    horizon (the sky shows through below the true horizon); the line-of-sight radius above removes it.
    Geometry has no refraction (the sky has none either), so the two agree; the horizon-cull test uses
    refraction only to be generous. Probe P12 adds: screenshot at 4.4 km (Whitney) and 10 km looking at
    the horizon, the terrain silhouette must meet the sky's horizon band with no sky-coloured gap.
  - Atmosphere: real visibility is haze-limited (typically 50-150 km, 300 km only on the clearest
    days). Add distance haze tuned so mountains at 150-300 km fade to faint silhouettes rather than
    stopping at a hard edge (`core_environment` fog/atmosphere settings; P12 also checks this).

**Design (mod).**
- **Follow modes** (`follow.decide(..., air)`, with `air = {agl, clearance, tti, impact}` from the
  latest `pose_reply`):
  - `ground`: agl ≤ 60 m and |vz| < 5 m/s: the M4 table.
  - `track`: tti < 8 s, or clearance < 400 m, or ground unknown: target = snap(impact) when tti < 8 s,
    else the fast-mode target; prepare when the target differs from the centre; commit when ready in a
    normal heavy slot, or with `requestHeavy(true)` (urgent) when tti ≤ 3 s or the player is outside
    the valid area with agl < 100 m.
  - `off`: otherwise (clearance ≥ 500 m and no impact within 8 s; hysteresis 400/500 m). No prepare,
    no commit; the block stays.
  - A plane at 600 m AGL flying into a ridge gets `track` from 8 s before impact even though agl never
    drops below 500 m (the CFIT case). A vertical dive at 250 m/s from 2 km gets `track` at 2 km
    (tti = 8 s), not at 400 m (1.6 s), with the DEM already prefetched below 3 km.
  - Cost: at 300 m AGL and 250 m/s, `track` makes about 54 commits in 120 s, ~13 % of the streaming
    budget [M]; it is in the heavyLoss term, and demand at that height leaves room.
- **Airborne rebases import a block** (`finishRebase` always does, `core.lua:258-265`), placed at the
  new anchor's terrain centre without the staleness check, so a later urgent descent has ground. They
  are excluded from "no commit above 500 m AGL" and counted separately.
- **Visible distance.** `LevelInfo.visibleDistance = radius + 20 km` from `pose_reply.radius` (with
  10 % hysteresis); `core.lua` `VISIBLE_DISTANCE` becomes the minimum. Fog at altitude through
  `core_environment` (`fogAtmosphereHeight`, `environment.lua:1262-1300`) if P12 says it is needed.

**Protocol 5.**
- New server -> mod message `pose_reply {anchorId, groundZ (num|null), clearance (num|null),
  tti (num|null), impact ([x, y]|null), radius (num)}`.
- The pose already has `z`; `vz` comes from M3a.

**Data and files.**
- `server/earthd.py`: `on_pose` (ground from cache, clearance, tti, radius; never awaits the
  network), `pose_reply`; path prefetch.
- `server/farfield.py`: `select(..., agl_path, radius)` (3D capsule, disc, horizon cull),
  `radius_for(agl)`, `ground_cached(point)`.
- `mod/.../core.lua`: handle `pose_reply`, visibleDistance, pass `air` to follow.
- `mod/.../follow.lua`: modes `ground`/`track`/`off`, urgent commit.
- `mod/.../sky.lua`: untouched unless P12 says otherwise.

**Tests.**
- Server: `radius_for` values; the disc and horizon cull (L9 root count at 10 km AGL); no exact tiles
  while agl(τ) ≥ the exact threshold, and exact tiles 3 s before a 167 m/s touchdown; the cliff jump
  keeps exact tiles; d3 selection counts at 300/1000/3000/10000 m match `test_demand.py` within
  ±10 %; `on_pose` with an empty DEM cache returns within 5 ms and sends `groundZ: null`; clearance and
  tti on a synthetic ridge.
- `test_protocol_agreement`: the new message; the AGL and mode constants on both sides.
- luajit `test_follow`: mode transitions and hysteresis; `track` targets the impact point; urgent
  commit when tti ≤ 3 s; no prepare in `off`; airborne rebase without the staleness check.
- `test_core_stubbed`: visibleDistance follows `pose_reply.radius` with hysteresis.
- `test_integration_drive`: CFIT (600 m AGL into rising ground), vertical dive from 2 km at 250 m/s
  with the DEM cold (+1, +2, +3 s fetch delay), and DESC; no fall-through (the review model fell
  through for 9 and 69 frames at +2 and +3 s with the first version's rule [M]).
- modsim **trace additions**: the agl/clearance/tti gate and the follow modes.

**In-game (flight profiles, section 5).** F300, F3000, F10000, CFIT, DIVE and DESC:
- fps ≥ 60;
- no terrain commit above 500 m AGL in `off` mode (airborne rebases and `track` commits are counted
  separately);
- the horizon is drawn to 350 km at 10 km (visual run);
- DESC and DIVE run on a **cold** DEM and imagery cache; after touchdown the car is on exact ground
  (exactMiss = 0 from touchdown) and never falls through.

**Risks.**
- visibleDistance may be clamped or expensive, or may change depth or cascade splits: probe P12. The
  fallback is to keep 100 km and fade with fog.
- A manoeuvre that creates an impact less than ~2 s ahead (a pull-down at very low height) can still
  outrun the block; `track` below 400 m clearance keeps the block near the plane to shorten this.

**Rollback.** `--altitude off`: agl is treated as 0, the radius is 80 km, and the follow gate is
disabled.

**Implementation notes, M1h "Radius" (built offline 2026-10-10, docs/ARCHITECTURE.md "v1.4 M1h").**
- Behind `earthd --horizon` (mesh mode only); **on by default in mesh mode since 2026-10-11** (`--no-horizon`), was off until P12 (extended, below);
  without it hello, status and the selection are unchanged (test_v131_default unchanged).
- Wire (additive under protocol 4, like M1/M4): `hello.horizon: true` and `status.radius` (m, every
  status) with `--horizon` only; `far_tile.level` up to 10. `pose_reply` stays M5: until then the radius
  rides on `status`, re-sent when it moves by > 2 %.
- Eye = the pose's camera z (else player z + 1.5 m) as a geodetic height in the pose's anchor frame
  (its h0 is sea level, so this adds the plane's d²/2R above the ellipsoid), quantised to
  max(10 m, 5 %). It survives rebases (it is frame-independent).
- zmax per anchor: one worker builds a pyramid of L6 cells (16 km) up to L10 over
  ±(min(1500 km, d(h_eye) + d(8900 m)) + 21 km), each cell the highest **DEM z6 pixel** touching it
  (block maxima of 8 x 8 px, never a sample that could miss a summit; z5 or coarser where z6 would
  need > 64 tiles); rebuilt bigger when a climbing eye needs more. A tile's zmax always comes from
  this pyramid (not from its built grid) so its selection never depends on whether it was built.
  Until the first pyramid exists the far field is the 80 km disc without a cull.
- RADIUS = clamp(d(h_eye) + d(zmax_region), 80 km, **1500 km**) with zmax_region = the highest L6
  cell above the eye's horizon (its nearest point within d(h_eye) + d(z)); the cap follows the
  "raised to 1500 km" item above (the task text said 700 km; one constant, `farfield.HORIZON_MAX`).
- The cull drops a node (and its subtree) when its nearest point is past d(h_eye) + d(zmax_node), but
  never within 80 km: the near field is today's tile for tile (tested along a 134 m/s drive, both
  layouts; demand 15.98 adds/s either way). Textures: 256 px at L8/L9, 128 px at L10.
- Offline numbers on the real DEM (z6, 64 cells): Whitney summit (eye 4417 m) radius 503 km,
  zmax 4217 m (z6 smooths the 4421 m summit), San Gorgonio's tile (306 km) kept, 83 tiles beyond
  80 km (379 in all vs 300 today), farthest kept tile 414 km; 10 km above Whitney 631 km, 97 beyond
  80 km; Central Valley floor (eye 110 m) 289 km, 49 beyond; open ocean (eye 2 m) 80 km, none beyond
  (296 tiles: the disc drops the square's corners). Selection 1-2.5 ms; pyramid 0.02-0.04 s from
  cached DEM tiles (0.9 s with z6 downloads).
- Mod: `horizon.lua` (pure target rule + LevelInfo / ScatterSky); `core.setHorizon(hello.horizon)`,
  `core.setFarRadius(status.radius)`: visibleDistance = max(100 km, 1.1 × radius) with 10 %
  hysteresis and postApply after every change; ScatterSky.atmoPlanetRadius = 6371 while horizon mode
  is on in an active world, the level's value restored at level end, on unload and with a server
  without horizon. Distance haze: a documented TODO in horizon.lua (core_environment.setFogDensity,
  setFogAtmosphereHeight, ScatterSky atmoMie*) for the lead to tune in P12.
- **P12 extended (before enabling):** with `--horizon`, at Whitney summit and at 10 km altitude:
  frame cost (fps, p50/p95, perfmon) with the real distant tiles shown (the original P12 had none
  beyond 80 km), live object count, far_done time and the L8-L10 import costs per kind; screenshots
  toward San Gorgonio / the Coast Ranges: the terrain silhouette meets the sky's horizon band with no
  sky-coloured gap; read back atmoPlanetRadius = 6371 and visibleDistance = 1.1 × radius after
  postApply; no depth or shadow artefacts at 500-650 km; then tune the haze.

### 3.7 M6: global far tiles in tile-local frames plus a disk cache (gated)
**Problem.**
- R5: each rebase rebuilds the whole set, about 300 tiles after M1.
- With the reload reserve (section 2.6) this reload ends before the next rebase on every machine that
  passes the M1 gate, but it costs 40 % of capacity for up to 90 s every 112 s at 134 m/s, i.e. lower
  detail at speed.
- There is no cache across sessions.

**Gate.**
- Ship M6 if, after M5, D134 shows any of:
  - a post-rebase reload (rebase -> far_done) > 90 s, or not done before the next rebase;
  - the controller at s = S_MIN for > 10 s during a reload;
  - fps < 60 for > 5 s after a rebase.
- Or if v1.6 (roads) needs the disk cache.
- Otherwise it moves to v1.5.

**Design.**
- **Index.**
  - Levels of 2048 m and up (L3+ in the 64-cell layout, 167 of 300 tiles at a pose [M]) use a
    cube-sphere quadtree: S2 face f, level z, i, j, with the quadratic projection.
  - Global level z is chosen so the edge is within 0.7-1.4× of 256·2^L.
  - Selection uses each tile's real size, so the K rules carry over.
  - Key: `g<f>_<z>_<i>_<j>`.
- **Frames.** Geometry and texture are written in the tile's own ENU frame at its centre (float32 ≤ 66
  km). The message carries `frame {lat, lon, h}`, and the mod computes tile -> anchor with geodesy
  (as `Anchor:transformTo`) and places the tile with setPosRot.
- **Detail UVs.** UV1 (the detail map) is in tile-frame metres / DETAIL_TILE for global tiles and in
  anchor-frame metres for anchor tiles, as today. The detail pattern therefore jumps at tile-frame
  boundaries; global tiles start at 2048 m (L3), where the detail map has faded out (≤ 2 km), so the
  jump is invisible [I; checked in the seam screenshots].
- **Seam.**
  - A refined global tile P of the finest global level is replaced by anchor tiles: every anchor
    quadtree node of L2 that intersects quad(P) (straight edges in the anchor frame, because the map
    is rigid), clipped to quad(P).
  - Clipping is per triangle against 4 half-planes (Sutherland-Hodgman). A clip vertex takes its
    height from the exact triangle it cuts, so clipped exact pieces stay physics-exact.
  - Pieces are keyed `<L>_<i>_<j>_c<f>_<z>_<i>_<j>`; their children inherit the clip, and only nodes
    that cross an edge are clipped. **Keys never contain `|`** (it breaks `SHAPE_RE` and the
    material-name rule, and is risky in VFS file names).
  - Shape regex for protocol 5 with M6:
    `^/temp/beamngEarth/[^/]+/far_\d+_(-?\d+_-?\d+_-?\d+(_c\d_\d+_\d+_\d+)?|g\d_\d+_\d+_\d+)\.cdae$`
    (session dir, anchor id, then an anchor key with an optional clip suffix, or a global key).
    Material names follow the same key: `bngEarthSat_<key>` with only `[A-Za-z0-9_-]`.
  - The swap group "P <-> its pieces" has one footprint, so the hole rules hold. Cracks along clip
    edges are covered by skirts on both sides, as at every LOD edge today.
- **Rebase.**
  - Global keys survive `commit_anchor`: the server keeps them in `shown`; the mod keeps them in `live`
    and re-places them with `xf` as today.
  - Only anchor keys (L ≤ 2 and the pieces, about 133 tiles) become `old`.
- **Disk cache.**
  - `~/.cache/beamng-earth/tiles/<build hash>/<gkey>.{cdae,dds}`, where the build hash covers every
    build parameter: FORMAT, the imagery source chain, texture size per level, tone mapping, cells,
    the DEM zoom rule and DEM source, skirt rule, and the cdae writer version. Hard-linked into the
    session dir (copied if on another filesystem).
  - Material name `bngEarthSat_<gkey>`.
  - LRU limit 4 GB.

**Protocol 5.**
- `far_tile.frame {lat, lon, h}` (optional; absent = anchor frame).
- `far_tile.poly` (optional: the footprint corners in the anchor frame, for the mod's AABB).
- hello gains `globalMinSize`.

**Data and files.**
- New `server/globaltiles.py` (S2 index, quad corners, tile frames).
- `server/farfield.py`: `select_global`, `clip_tile`, `build_tile(frame=tile frame)`.
- `server/earthd.py`: `commit_anchor` keeps global keys; the cache, `tile_name`, `sat_material`.
- `mod/.../farfield.lua`: `newInst` (frame -> transform, poly AABB); `switchAnchor` keeps keys with a
  `g` prefix in `live`.
- `mod/.../geodesy.lua`: tile-frame transform.

**Tests.**
- Server: global tiles tile the sphere at faces and corners with no gaps or overlaps (area sum);
  clipped pieces plus the global neighbours cover the radius exactly once (polygon area); clipped
  exact pieces equal physics heights at clip vertices; a cached tile is byte-identical across
  sessions; keys are stable across anchors; changing any build parameter changes the build hash.
- `test_protocol_agreement`: the M6 shape regex and material-name rule on generated keys.
- luajit: global tiles survive switchAnchor (no `old`, one transform applied).
- Integration: the rebase reload sends ≤ 150 tiles.

**In-game.**
- D134 rebase reload ≤ 20 s.
- No seam visible at the L2/L3 boundary in screenshots at 3 fixed spots.
- A second session at the same place loads with 0 server builds for global tiles.

**Risks.** Clipping complexity (the biggest code item in v1.4), seam artefacts, and S2 size variation
(up to about 2x across a face) making LOD uneven.

**Rollback.** `--global off` (the default until the gate passes).

## 4. Protocol summary (protocol 5, FORMAT 4)

| direction | message | change | milestone |
|---|---|---|---|
| S->M | hello | `cells` int (M1, additive under protocol 4); `farWindow` int (M2); `exactLevel` int (M3a); `blockSize` int (M4); `horizon` true, opt (M1h); `globalMinSize` int, opt (M6) | M1-M6 |
| S->M | far_tile | `tex` int (M1); `prio` int 0..2, `seq` int (M2); `level` up to 10 (M1h); `frame` {lat, lon, h} opt, `poly` [[x, y]...] opt (M6) | M1, M2, M1h, M6 |
| S->M | far_retex | new: `anchorId` int, `key` str, `seq` int, `tex` int, `texture` str | M3a |
| M->S | far_ack | new: `anchorId` int, `loaded` [int seq], `failed` [int seq] (an empty Lua table may encode as {}) | M2 |
| M->S | pose | `vz` num, `cap` num, `old` int | M3a |
| S->M | pose_reply | new: `anchorId` int, `groundZ` num/null, `clearance` num/null, `tti` num/null, `impact` [x, y]/null, `radius` num | M5 |
| S->M | status | `backlog` int (desired − shown, current anchor), alongside `text`; `radius` num, opt (M1h: the far-field radius with `--horizon`, until `pose_reply` carries it) | M0, M1h |
| S->M | terrain_ready / anchor_ready.terrain | `size` 512 (already a field) | M4 |

`test_protocol_agreement.py` tables (MOD_TO_SERVER, SERVER_TO_MOD) are exact and are extended in the
milestone that adds each field. A v4 server answers a v5 mod with the existing mismatch status; there
is no mixed mode. `SHAPE_RE` already allows levels down to -2 and needs no change until M6.

## 5. In-game verification (lead, only while the user is not playing)

Harness: section 3.0 (GE-side driver, MCP polling ≤ 1 Hz, no screenshots in timing runs, perfmon
harness tags, effective-speed check). Every timing profile has a separate visual run that takes the
timeline screenshots (every 20 s) and is not used for frame statistics.

| profile | route | pass |
|---|---|---|
| D40 | 40 m/s, 20 km, heading 37°, 1 rebase | M0: baseline. Later: fps ≥ baseline − 2, p99 ≤ 25 ms, exactMiss 0, pops 0 |
| D67 | 67 m/s (150 mph), 20 km | M0: baseline (the "200 mph" drive at its real speed). Later: fps ≥ 60, frames > 50 ms = 0 outside commit/rebase frames |
| D90 | 90 m/s, 20 km | fps ≥ 60, frames > 50 ms = 0 outside commit/rebase frames, max queued ≤ 8, backlog slope ≤ 1/min, estCdaeTex last/first ≤ 1.2 |
| D134 | 134 m/s, 30 km, 2 rebases | as D90, plus exactMiss = 0 (excluding ≤ 2 s after each rebase), commit frame ≤ 40 ms (M4), rebase frame per section 3.5, reload done before the next rebase and ≤ 90 s (≤ 20 s with M6), live ≤ 400 |
| TELEPORT | 5 teleports 50-500 km apart, warm and cold cache | no hole while the player is unfrozen; unfrozen ≤ 5 s warm, ≤ 10 s cold |
| F300 / F3000 / F10000 | 250 m/s at 300 m / 3 km / 10 km AGL, 60 km | fps ≥ 60, no commit in `off` mode, frames > 50 ms = 0 outside counted rebase/track commits, horizon visual run at 10 km |
| CFIT | 250 m/s at 600 m AGL towards a ridge that rises above the flight level, cold cache | `track` ≥ 6 s before impact, block under the impact point at impact (castRayStatic hits) |
| DIVE | vertical dive at 250 m/s from 2 km, cold cache | castRayStatic hits under the car from 1 s before impact |
| DESC | 10 km -> ground over 60 s at 250 m/s, then 5 km at 80 m/s, cold cache | no fall-through, exactMiss 0 from touchdown |

Every run passes only at ≥ 95 % of nominal speed, and also reports failed = 0, materials == live
tiles at the end, and no `Could not open` in beamng.log.

## 6. Room for v1.5 and later (budgets, unit kinds)
- **Capacity share.**
  - At 134 m/s the controller gives the ground up to 80 % of cap (60 % during a reload); at ≤ 40 m/s
    the ground uses ~25 % of the default machine's 17.5/s.
  - Content layers (roads, buildings, forest) take what is left, by ETA: the ack/window/prio scheme of
    M2 is the template. Each layer gets its own window and seq space in `far_ack`-style messages, and
    the server picks the next item across layers by (class, eta). The controller's 0.8 becomes a
    shared budget: ground first, then content, each with its own speed scale.
  - Ground class 0 always wins.
- **Unit kinds.**
  - The governor keys estimates by kind (`cdaeTex128/256/512`, `retex`, `material`, ...).
  - New kinds keep the "life cost" accounting (spawn + material + delete) and the object-count term
    so `cap` stays honest: road chunk ~2 ms, building chunk ~4 ms, forest batch ≤ 2 ms (perf 4.3).
  - The urgent path stays ground-only.
- **Object count.** Keep total streamed objects ≤ ~1 000 (R3; c grows ~0.16 % per object).
  - Ground is about 330 after M1 (up to ~600 during a reload, ~330 with M6), leaving about 400-700
    for content.
  - Use shared materials for content (perf 4.3); materials scale the same way (R3).
- **Physics extent.** The 512² block is ±511 m, and the car can be up to 320 m off-centre (hard
  commit), so collidable ground extends at least ~190 m behind the player and ~700 m ahead at
  134 m/s; sideways at least 190 m. AI traffic and collidable content must stay within these limits
  per direction (noted for v1.8).
- **Frame.** FRAME_GOAL_MS 16.4 covers streaming only. GPU-heavy layers must use the adaptive-radius
  controller in perf 4.3 (`$fps::gpuTime`).

## 7. Probes (GE Lua via MCP, only while the user is not playing)

| id | what | before | decides |
|---|---|---|---|
| P0 | The GE-side driver: `setClusterPosRelRot` + `applyClusterVelocityScaleAdd(-1, 0, vx, vy, vz)` each frame keeps the pose velocity within 2 % of the profile, no reset events, ≥ 95 % of nominal speed at 134 m/s on v1.3.1 | M0 baselines | the harness |
| P1 | Perf 1.5 frame A/B (lastSplitCasters, shared material, castShadows per level) | M3b | the b column of 2.3 |
| P13 | Busy-wait 0/1/2/4/6 ms in `onUpdate` (probe sink) at the 4K desert spot, 10 s each, nothing streaming; record wall p50/p99 and fps; fit k = d(wall)/d(spent) | M3b | `base_e = wall − k·spent`; the b column of 2.3 |
| P8 | Spawn time of a 64-cell .cdae with 128/256/512 px DDS, cold and warm, at 300 and 600 live objects (α); P10 inside it (a .cdae without tangents: import time, rendering and lighting/shadow look, since the engine may rebuild missing tangents at load); `retex` (swap a tile material's colour map) time and correctness | M1 | the M1 gate, texture sizes, governor priors, the `far_retex` path |
| P9 | Spawn cost vs live objects / materials / files in the session dir: create 1 500 dummy TSStatics, then 1 500 materials, then 10k files in the dir, timing a reference spawn after each step | M1 (informative) | R3 cause; whether to split the session dir |
| P14 | Teleport hold: freeze (`controller.setFreeze(1)` via `queueLuaCommand`) and a screen fade on the player, released cleanly | M3b | the teleport hold calls |
| P2 | Extended (section 3.5): 512² vs 1024² re-import into the live block and back, first and steady state, with and without the layer map; collidable extent via `castRayStatic`; `setRenderEnabled(false)` survives | M4 | M4 gate (≤ 30 ms) |
| P7 | (a) timing wrapper of origin research P7 over `finishRebase` and phase 1, with frame numbers; (b) double-buffered terrain (second hidden TerrainBlock, swap) | M4 | the rebase-frame target and design |
| P11 | Terrain commit without `reloadCollision` (does the car stay on a re-imported block?) | M4 | M4 margin; later content |
| P12 | `LevelInfo.visibleDistance` 400 km at runtime: frame cost, shadow (SDSM) cost and cascade fit, depth artefacts with `nearClip` 0.1 (`cameraUpdate.lua:180`) at 10 km and on the ground, fog at 10 km via `core_environment` `fogAtmosphereHeight`. Reversed depth is a compile flag (`BNG_USE_REVERSED_DEPTH_BUFFER`) that Lua cannot read, so P12 judges depth precision only by the artefacts | M5 | M5 radius and visibleDistance |

## Review notes

Engine review (E):
- **E1** (blocker, harness): accepted. Section 3.0 specifies the GE-side per-frame driver
  (`newExtensionProxy` sink, `setClusterPosRelRot`, `applyClusterVelocityScaleAdd`, height from
  `castRayStatic` or profile altitude), MCP polling ≤ 1 Hz, no screenshots in timing runs, perfmon
  harness tags, and the effective-speed summary with the 95 % rule; probe P0 verifies the calls. §1.1
  and §2.2 are restated at 67.6 m/s and a 9.2 s commit interval (model 11.2/s vs 11.4/s measured);
  the `--alt` "zero velocity" step is gone; M0 re-measures the v1.3.1 baselines and D40 now passes
  against that baseline instead of "fps ≥ 78".
- **E2** (imagery not warm, server capacity): accepted. R7 added; capsule prefetch of DEM and imagery
  τ ≤ 10 s in a separate pool (M3a); exact tiles never wait for imagery (parent/coarser texture, then
  `far_retex`); worker 0 reserved for classes 0-1 (M2); `bench_build.py` (M0); table 2.5 redone with
  worker wait, window wait for a burst of 4 and token build-up (≤ 1.4 s).
- **E3** (c_eff gate): accepted. M1 gate is c_eff(330) ≤ 6.2 ms (worst-4K margin ≥ 1.15 at the
  s·v = 60 demand) and c_eff(600) ≤ 9.8 ms, from P8 at 300 and 600 objects; mitigations (128 px exact,
  L≥2 at 256 px, P10) come before M1 ships. The object-count term is in §1.2 (α), §2.2, §2.3 and the
  drive cost model.
- **E4** (adaptive budget assumption): accepted. Probe P13 before M3b; `base_e = wall − k·spent`.
- **E5** (rebase remainder): accepted. P7(a) is required before M4 and records frame numbers because
  phase 1 and 2 can share a frame (`rebaseLag` 0); the rebase-frame target is now conditional
  (≤ 45 ms with a P7 path, else ≤ 60 ms, reported separately) with the ~44 ms of work stated.
- **E6** (block size change unverified): accepted. P2 is extended as asked; `loadFlat` uses the
  session block size, `core.lua:155` is in M4's files, and `test_core_stubbed` covers size 512.
- **E7** (visible = physics contradicts p_diag): accepted, option 1. The target is stated as a
  tolerance (|twist|/4: p99 ≤ 5 cm, max ≤ 31 cm on the p_diag block) and integration assertion (f)
  compares against the bilinear reference. The centre-vertex option is deferred: it doubles the most
  imported tile (~700 KB), which works against the E3/S9 cost gate.
- **E8** (groundZ blocks the server; cold DEM in the air): accepted. Ground heights only from cached
  data, null when unknown (M5); heightmap-zoom DEM prefetched along the path (τ ≤ 30 s below 3 km AGL,
  and on the ground at ≥ 100 m/s); `PREFETCH_MARGIN` = max(300 m, 8 s·|v|) (M3b); DESC, DIVE and CFIT
  run on a cold cache.
- **E9** (integration drive cannot validate M3): accepted. Real time for ≥ 90 m/s cases;
  c(kind, n) with α; `BNGE_DRIVE_COMMIT_MS`/`REBASE_MS` per block size; D134-c10 case.
- **E10** (lua_consts cannot read nested tables): accepted. Flat constants (`PREPARE_DIST_512` ...);
  `configure()` updates every derived local.
- **E11** (configure mutates globals): accepted. `Layout(cells, block)` is passed through `Server`,
  `select`, `build_tile`, `zoom_for_level` and `heightmap`; tests are parametrised over (32, 1024) and
  (64, 512). The mod keeps `lattice.configure` (one session per process; luajit tests call it in
  setup).
- **E12** (acks keyed by tile key): accepted. `far_tile.seq`, acks by seq.
- **E13** (urgent delays the heavy slot): accepted. No `tryUrgent` once `heavyPending` has ramped b to
  0; `test_governor` case added.
- **E14** (heavyLoss counts idle frames; loss per b): accepted. Work and heavy-window frames only;
  L(b) = 4.5·b² + 5·b stated per b (17.6 / 28 / 55.5 / 72.6 ms) and used in table 2.3.
- **E15** (protocol drift): accepted. (a) `hello.cells` listed in §4 and in M1; (b) the no-op
  `SHAPE_RE` item removed; (c) `status.backlog` defined in §4 (M0); (d) M6 keys use `_c` suffixes,
  `|` is banned, and the M6 shape regex is written out.
- **E16** (console name): accepted. `setFarBudget(ms)`; `setFarBudget(0)` returns to auto.
- **E17** (demand not reproducible; 300 vs 372): accepted. `test_demand.py` moved to M0; the M1 rows
  were re-run (`demand_check.py`: 318/333/312/282 tiles, 4.5/7.6/10.7/13.4 adds/s at s·v = 90) and the
  300 vs 372 difference is explained in §2.1 (perf's emulation kept a 64 m ring).
- **E18** (test_modsim checks nothing new): accepted. Each milestone lists its trace additions (M2,
  M3b, M4, M5; none needed in M1 or M3a, which change only server rules).
- **E19** (airborne rebases import): accepted. They import (so an urgent descent has ground), are
  excluded from the "no commit above 500 m" rule and are counted separately.
- **E20** (P10, P12 too narrow): accepted. P10 runs inside P8 and times the import and checks lighting;
  P12 records SDSM cost and depth artefacts, notes that reversed depth cannot be read from Lua, and
  uses `core_environment` for `fogAtmosphereHeight`.
- **E21** (disk-cache key, UV1): accepted. The build hash covers every build parameter; UV1 is in the
  tile frame for global tiles and the seam is beyond the detail-map fade.

Speed review (S):
- **S1** (blocker, ground lost in a shortfall): accepted, all four parts. (a) group class inheritance
  and import by group (M2); (b) one level of refinement per group near the corridor, ≤ 4 adds (M3a);
  (c) the VCAP floor is removed; the controller keeps demand ≤ 0.8 cap (M3a); (d) the D134-c10
  integration run must keep exactMiss 0. §2.4 states the 40 % and 56 % results.
- **S2** (blocker, planes through terrain): accepted. Server clearance and time to impact over 8 s from
  cached grids (`pose_reply`); `track` mode when tti < 8 s or clearance < 400 m with the block at the
  impact point; DEM prefetch below 3 km AGL (wider than the 2 km asked, to cover a 167 m/s descent);
  CFIT and cold DIVE profiles and integration cases. The DESC vs "100 m/s safe" contradiction is gone
  (the limit is now the 8 s horizon, not a vertical speed).
- **S3** (EXACT_AGL too late): accepted. max(60 m, 3 s·−vz) with hysteresis; never dropped below
  150 m AGL under 100 m/s horizontal.
- **S4** (speed cap in the air, wrong flight rows): accepted. The |v| cap is replaced by the
  demand-budget controller with the 3D ring model; the flight rows of table 2.1 are replaced by the
  re-run (`flightdemand.py`: 309/315/250/220 tiles, 16.3/9.2/3.2/0.7 adds/s).
- **S5** (reload does not finish before the next rebase): partly accepted. Accepted: count old + new
  objects in the capacity model (α, c_eff(600)), defer the next rebase until the reload is done (up to
  16 km), and the D134 pass becomes "done before the next rebase, ≤ 90 s". Not accepted: putting M6 or
  "keep L≥3 old tiles" on the critical path. Instead the controller reserves 40 % of cap for the
  reload (§2.6): at c_eff(330) = 6.2 the reload ends in ≈ 69 s, at 8 ms in ≈ 89 s, both before the
  112 s rebase interval. The "keep old L≥3 tiles" variant needs the same seam handling as M6 (the old
  and new quadtrees are rotated against each other, so kept tiles would overlap or gap the new near
  set), so it is not cheaper than M6. M6's gate now trips on exactly the failure the review found.
- **S6** (merges behind are class 1): accepted. Behind rule in M2; §2.4 no longer quotes "ahead"
  margins.
- **S7** (pose_reply ground stalls the server): accepted, same fix as E8.
- **S8** (rebase/follow margins for 512²): accepted with changes from a re-run of the review's
  `follow.lua` test. STALE_DIST 192 for 512; PREFETCH_MARGIN max(300 m, 8 s·|v|) (8 s rather than the
  6 s suggested, to match E8); no staleness check with follow off; stale anchors no longer loop. A
  fast mode (|v| > 150 m/s: REACH 420, look-ahead 3 s, prepare test at p + v·0.75 s) passes 200 m/s at
  1.2 s latency (196 m margin at 0°); 250 m/s at 1.2 s still fails at headings 0-10° (one pending block per cycle bounds
  it), so the target is stated as ≤ 200 m/s on the ground, 250 m/s with a warm DEM, and the path
  prefetch makes the DEM warm. Tests for 1.2 s at 200 m/s and 3 s anchor latency at 250 m/s are in M4.
- **S9** (6 ms is a guess; one kind for all textures): accepted. Kinds `cdaeTex128/256/512` weighted
  by mix in `cap`; P8 measures each; L≥2 keeps 256 px above 12 ms, exact 128 px above 7.5 ms; urgent
  never on top of a unit larger than b.
- **S10** (irregular commit bursts): accepted. Commit-time re-check; the measured gaps (1.9-3.9 s,
  1.5 s at 250 m/s, 13 % at 300 m AGL) replace "every 2.5 s" in §2.3 and M4/M5.
- **S11** (rebases not invisible): accepted. P7 (both parts) before M4 with the conditional target;
  old tiles of L ≥ 0 are sunk at the rebase so old and new never z-fight.
- **S12** (pop bound): accepted. The near-car pop is one level (M3a one-level rule) and bounded by
  that tile's `TileInfo.err`; per-level bounds recorded in `test_farfield`.
- **S13** (teleport): accepted. Teleport hold (freeze and fade until the exact tile and neighbours
  are shown, 10 s cap), probe P14, TELEPORT profile and integration case.
- **S14** (scale-change bursts): accepted. Scale rate limit ≤ 10 % per 5 s, decreases for new splits
  only (s_hold), cap smoothed over 10 s.
- **S15** (square far radius): accepted. Disc selection and a geometric-horizon cull (M5).
- **S16** (physics extent note): accepted. §6 gives the extent per direction (≥ 190 m behind and
  sideways, ~700 m ahead).
