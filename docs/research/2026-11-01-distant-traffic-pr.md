# Distant traffic: current dev integration and bridge check

The contract and code map are in [DISTANT_TRAFFIC.md](../DISTANT_TRAFFIC.md). This note records the checks
performed for this contribution and their limits.

## Source and review

Integrated upstream dev `5ef60f111f298d9e1dc2f8993778ec4fc40391bc`, including the native fine-terrain API. The
upstream delta and current source rules were read before integration. New route algorithms remain in Rust;
Python marshals shared road rules, projections, samples and bridge profiles. Both native artifacts were rebuilt
from the combined source as version 126; no old binary was retained to resolve the merge.

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
