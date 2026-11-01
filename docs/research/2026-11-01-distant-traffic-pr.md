# Distant traffic: integration and rendering checks

The contract and code map are in [DISTANT_TRAFFIC.md](../DISTANT_TRAFFIC.md). This note records the checks
performed for this contribution and their limits.

## Source and review

Integrated upstream dev `5ef60f111f298d9e1dc2f8993778ec4fc40391bc`, including the native fine-terrain API. The
upstream delta and current source rules were read before integration. New route algorithms remain in Rust;
Python marshals shared road rules, projections, samples and bridge profiles. Both native artifacts were rebuilt
from the combined source as version 126; no old binary was retained to resolve the merge.

Updated integration: merged dev.116 (`ae96a16a41fd7ceefcddcd642531d77421c46bb3`) and rebuilt both artifacts as
native132, preserving `TrafficRoutes` alongside the new fine-terrain APIs. The subsequent dev commit `a78df749`
changes only fine-height sampling; a local merge-tree check found no conflicts with that current target. The
feature's route, rendering and settings behavior did not change during this update.

Independent Standards review found a duplicated resync readiness condition; execution and `hasWork()` now share
one predicate. Independent Spec review found that inferred divided-carriageway direction was dropped. A failing
regression reproduced two same-direction streams on opposite carriageways; the producer now forwards `Spec.dir`
when no explicit one-way direction exists. The regression covers both driving sides and explicit rules. Both
reviewers confirmed their findings resolved. Neither static review proves whole-engine crash or leak freedom.

## Bridges and distribution

Bridge routes share the far slabs' anchor-free `BridgeProfiles` store. The profile owner's `(fid, part index)`
survives `ChainView` mapping; ids remain stable when neighbouring tiles request different spans. Deck Z is already
in the destination world frame, so it is not replaced with water elevation or given a second curvature offset.
Missing/non-finite decks are omitted. Ground routes and tunnels keep their existing separation.

The actual production stream in Vladivostok delivered 61 unique bridge routes at the observed checkpoint.
The renderer had 44 bridge assignments; 57 non-hidden planes belonged to the Russky Bridge's two carriageways.
The inspected screenshot showed white/red points along the span, and the user confirmed movement there. These
are delivered/rendered object counts, not a claim that every object was an unobscured visible pixel.

The user then noticed repeated three-car groups. The former linear rank transformation correlated adjacent slot
numbers before budget thinning (lag-one correlation about 0.28 in the fixed 64-road sample). Mixing those bits
before the existing bounded modular multiplications reduced that sample's correlation to about 0.002. Position
cell jitter was initially widened, but the tester still saw a regular pattern. The final sequence instead
distributes 80% of the route over bounded exponential gap weights and reserves 20% for minimum headways. It is
computed once, cached at no more than 128 offsets per route and released with that route. Expected density,
constant route speed and work/object limits remain unchanged; fractional rounding and selected identities can
differ between versions. Six deterministic rank-correlation regressions cover adjacent slots and a three-slot
lag; headway tests check ordering, variation, exact total length, wrap and cache reuse. This is cosmetic variation,
not a car-following simulation.

## Verification

Repeated on the dev.116 integration:

- Windows: 27 feature, 85 road, 8 fine-terrain, 46 grading, 13 plan-cache, 22 heightmap, one anchor-quantum and
  74 building tests passed. Protocol agreement: 32 run, 31 passed, one skipped. Synthetic far-geotile checks:
  six run, four passed, two real-data tests skipped.
- LuaJIT: 239 pure path, 1740 renderer, 282 core, 293 settings, 43 terrain-swap, 28 terrain-patch, four XXH64,
  17 geotile, 31 road-geotile and 33 far-geotile assertions passed.
- Both native132 artifacts were rebuilt from source. Windows API import and Linux bridge-route/API checks passed.
  The updated mod was installed and the Windows game connected with capability 2. This fresh daytime scene did
  not exercise nighttime movement; the moving-light and bridge observations below remain from the earlier build.

Earlier feature acceptance and lifecycle checks:

- Windows feature suite: 27 tests passed, including deck relief, missing/invalid profiles, stable bridge-part ids,
  the default profile-reader contract and divided carriageway directions.
- Protocol agreement: 32 run, 31 passed, one skipped.
- Adjacent roads: 83 passed; navigation graph: 13 passed; geotiles: 14 run, one skipped.
- LuaJIT fixtures: 239 pure path assertions, 1740 renderer assertions, 282 core and 293 settings assertions passed.
  The saturated camera-return fixture uses longer roads whose slots clear endpoint fades, so that it measures
  view restoration rather than intentional route-end dimming. Geotile 17 and navigation graph 46 also passed.
- Renderer lifecycle regression repeatedly creates the full 512-car pool and checks zero retained engine objects,
  routes and pending points after reset. A separate 60-cycle stub-engine run, collected every 20 cycles, measured
  about 2026/2079/2083 KiB of Lua heap with the final phase cache. This finite result does not measure native renderer allocations or prove
  absence of a long-session process-memory leak.
- JavaScript syntax and Git whitespace checks passed. The Linux artifact has a native route/API smoke check;
  the live game and comprehensive suites above ran on Windows, not Linux BeamNG.

The game stayed open during companion replacement; the user's camera and time were preserved. Physics was
running during the bridge check. No distant-traffic traceback was observed. A pending asyncio task warning
reappeared during companion reconnect, as it had before this integration. The earlier reported native crash
while deleting gameplay traffic was not reproduced in the finite moving-traffic checks; its cause remains
unestablished. Genuine driving rebases, long sessions and controlled full CPU/GPU comparisons remain additional
acceptance work, rather than implied by these fixtures or mixed live timing counters.

## Camera stability and quality (2026-11-02)

The former view-dependent budget reproduced two visible problems: zooming added 235 planes to an already
occupied central road, and a 45-degree turn initially revealed fewer cars than the settled view. Regression
fixtures failed on that renderer. Admission now builds a stable population around the player, independently of
camera position, orientation, FOV and aspect. Fixed slot phases prevent unrelated road populations changing when
moving cars cross selection bands. Genuine additions fade in; revealing an established actor refreshes its
cached pose immediately, even when the preparation governor is blocked.

The first world-wide 512-car variant passed the transition checks but the user found it too sparse. They
accepted the denser 2048-car variant's appearance and transitions, then reported a frame-rate cost. Low/Medium/High
now cap the whole population at 512/1024/2048 cars, including retiring assignments. Hidden planes receive no
transform/color uploads, off-screen motion is cached at 2 Hz, and an unchanged population avoids repeat scans.
Quality reductions release excess native pairs incrementally. Producer ranges, road density, protocol and the
dev.116/native132 server remain unchanged.

A fixed-camera Windows comparison with the user's background frame-rate cap disabled sampled each phase for
15 seconds. Camera position/orientation/FOV remained unchanged within each sample; time continued normally.

| Phase | Mean FPS | Mean module time per frame | World cars at end | In-frustum count |
|---|---:|---:|---:|---:|
| Off, before | 53.4 | 0.03 ms | 0 | 0 |
| High | 38.7 | 4.96 ms | 1932 | 660 |
| Medium | 43.8 | 2.39 ms | 953 | 321 |
| Low | 50.3 | 1.33 ms | 470 | 154 |
| Off, after | 55.0 | 0.04 ms | 0 | 0 |

These are short, scene-specific end-to-end samples, not a hardware-independent FPS promise or a separated GPU
profile. Population/density continued changing slowly with the clock. The final small change skips native object
lookups for already-hidden planes; the table predates that change and does not claim a measured gain from it.

LuaJIT fixtures passed: 241 pure path assertions, 6780 renderer assertions, 310 settings assertions, 282 core,
17 geotile and 46 navigation graph assertions, plus JS syntax and whitespace checks. They cover zoom/aspect/free
camera movement, a first-frame 45-degree turn, moving off-screen poses, admission fades, quality migration,
quality reductions and repeated full-pool reset. A separate 60-cycle full-2048-pool run also passed, with zero
retained stub objects/routes/pending points after each reset. Collected Lua heap at cycles 20/40/60 was about
1768/1787/1830 KiB; this is finite harness evidence with a small increase, not proof of zero native-memory leaks.

Upstream conventions were refreshed through `eefbb41549e1d0125169bb8537e815a508a3de9e`; the instruction/workflow
files were unchanged. That newer terrain work was inspected for conventions, not merged or executed in this test.

A subsequent human-driven High test recorded 590 one-second samples over about ten minutes, with roughly 23 km
of sampled movement within common anchor frames. Physical-follow commits increased from 130 to 188. Anchor ids
changed from 2 through 3 to 4, with the driving rebase counter increasing from 2 to 3. Every sample's native group
count equalled the renderer's pool count. Peaks were 2048 actors, 4096 planes, 336 tiles, 5379 prepared routes and
87626 prepared plus pending points; no deferred tiles were needed. The route cache changed with travel rather
than retaining all visited roads. No input, camera, weather or time commands were used to drive this test.

Whole-game physical memory ranged from about 3674 to 5470 MiB and GE Lua heap from 261 to 360 MiB, with falls as
well as rises. These include terrain, vegetation, other mods and the bounded telemetry itself; they cannot be
attributed to this feature alone. Native-object equality, fixed ceilings and repeated cleanup provide finite
resource evidence, not an assertion of leak freedom. The game logged a 359 ms world-loading hitch near a rebase
(146.6 ms world work, 15.9 ms traffic module work); limits on work units are not hard frame-time guarantees.
High still has a measurable rendering cost; weaker machines should use a lower quality or disable the effect.
The user confirmed smooth operation and good camera/zoom transitions after this drive.

An actual idle cleanup check then disabled the renderer and verified all 4096 native traffic planes disappeared
in about 34.4 seconds, including the 30-second idle hold and incremental deletion. A global TSStatic shape scan
found zero remaining traffic planes, agreeing with zero pool/group objects. Route data stayed cached as designed.
The test restored the user's High/on settings; temporary metrics extensions were removed after verification.

All 190 installed mod files matched source SHA256. The actual CEF runtime module cache exposed the new quality
control with all three options. The game remained open throughout these checks; public PR/test archives still
contain the preceding version until a separately approved publication.
