# Modernization contribution — scope and validation

This separates the work in this contribution from the broader modernization goals. It is not an upstream
release announcement or a claim that every target is complete. "Existing" means existing BeamNG.Earth code,
not a new feature or fresh engine validation in this contribution.

| Target | Current evidence/status |
|---|---|
| Faster world loading | Range-cache duplicate request fixed; controlled race benchmark improved. Whole-world cold/warm loading speed not yet measured. |
| Predictive world streaming | Existing speed/heading prediction, terrain preparation and GPS-route data prefetch. No new end-to-end driving claim. |
| Smoother roads/geographic accuracy | Existing grading, junction/bridge/tunnel handling; new malformed-survey isolation and finite-coordinate fixes tested. Vehicle-response validation pending. |
| Sketchfab buildings/landmarks | Secure metadata and licensing preflight implemented. Authenticated download, conversion and runtime import not implemented. |
| Intelligent placement and asset LOD | Existing procedural buildings/LOD; new downloaded-model matching/placement and LOD pipeline outstanding. |
| Steam friend grouping | Not implemented. Supported identity/invitation bridge still needed; no Steam authentication spoofing. |
| Multiplayer synchronization/recovery | Coordinate, dateline, stale-origin and malformed-vector fixes pass offline tests. Real two-client/network-fault acceptance pending. |
| Shared world data | Experimental immutable road-plan sharing integrated into grading, real loopback transfer and two-anchor tests. Complete shared terrain/world manifest outstanding. |
| Proximity/server/group voice | Not implemented. Mumble Link research does not constitute embedded audio transport or private channels. |
| Embedded VOIP/PTT/device/mute controls | Not implemented; actual audio client, authorized routing and runtime latency tests needed. |
| Spotify/internet radio/AI DJ/location radio | Not implemented in this initiative. Policy/authentication, licensed sources and playback work remain. |
| SIMD/AVX2/AVX-512 | No new SIMD change. Existing native Rust kernels remain; stage profiling must justify runtime-dispatched acceleration. |
| Multithreaded generation | Existing workers and native batching; retained. |
| Network utilization/caching | Range integrity and lock-race fixes; immutable prepared road plans deduplicate content and support offline warm reuse. |
| Reduced stuttering | Not measured in BeamNG during this initiative. |
| Memory/resource management | Revisit descriptor lifecycle fixed; bundle reads, handlers, artifacts and storage budgets bounded. Long-session memory profiling pending. |
| Windows/Linux dedicated hosting | Packaging tools produce an Earth plugin ZIP and version-pinned official BeamMP Windows/Linux executable bundles with notices/source and empty-key configuration. AuthKey and actual hosting acceptance still required. |
| Headless mode | Existing BeamMP dedicated server and Earth companion; new loopback road-plan cache commands in player executable. |
| TUI/GUI administration | Full shared server-management interfaces not implemented. |
| Integrated voice server | Not bundled or implemented. |
| Redis | Deferred; single-host prepared artifact distribution does not need Redis. |
| Server-side prefetching | Prepared bundles can be served before driving. Live aggregate-player predictive scheduling not implemented. |
| Docker/automated deployment | Full deployment stack not implemented. Existing native release automation retained and expanded. |
| Inherited bug fixes | Reproduced fixes integrated with regression coverage; see foundation audit. |
| Automated testing | Windows/Linux quality workflow; local Python/Lua/Node/Rust suites and package validation. Archived fork results document prior checks; the current PR's checks establish its build status. |
| Multiplayer stress testing | Not performed against actual BeamMP clients. |
| Performance benchmarks | Forced range race and synthetic two-client road-plan reuse measured. No full-city/FPS claims. |
| Error handling | Corrupt ranges, malformed surveys/packets, stale peer origins and incompatible/incomplete bundle failures covered. |
| Reliable releases | Required quality gate, published-release update checks, versioned player/host assets, digest validation and explicit validation boundaries. |

See [shared road-plan operations](SHARED_WORLD.md), [foundation audit](MODERNIZATION.md), and
[foundation validation](MODERNIZATION_VALIDATION.md). Next acceptance priority is real client driving
with pinned road plans and a larger measured region, then complete world-dependency manifests.
