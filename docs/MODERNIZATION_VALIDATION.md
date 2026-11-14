# Foundation validation and release readiness

This report covers the modernization foundation on Windows, not the full platform initiative.
Baseline `8b0e9b04` remains available as `baseline/modernization-20261102`.
No release or deployment was performed. The existing product version is unchanged.

## Local evidence

| Check | Result |
|---|---|
| Native earthfast build | Successful release build from source, native API version 116. Four existing Rust warnings; no native source changed. |
| Companion startup interface | `python server/earthd.py --help` succeeded. This is not game/runtime validation. |
| Python foundation gate | 27 modules, 426 cases: 421 passed, 5 skipped, zero failures/errors. |
| Lua unit/stub gate | All 72 modules passed. Following the final multiplayer input-hardening change, all 6 affected multiplayer/visibility modules passed again (371 assertions). |
| JavaScript map math | Both Node test modules passed. |
| Development source/BeamMP ZIPs | Built successfully; integrity checked locally. Not native installers or published releases. |
| Rust discovery service | 4 tests passed with `cargo test --locked --manifest-path native/serverlist/Cargo.toml`. |
| Workflow syntax | Both YAML files parsed locally; remote Actions execution pending. |
| Code hygiene | `git diff --check` passed during integration. |
| Sketchfab | 14 offline mocked-HTTP tests; live authentication and model import not performed. |
| Performance | Forced range-cache race measured before/after; see [benchmark](research/2026-11-02-range-cache.md). No FPS or whole-world loading claim. |

The Python gate's five skips are explicit:

- One disabled automatic peer-sharing port test (upstream feature commented out).
- Two optional cached real-world road tests disabled for a repeatable synthetic gate.
- One POSIX FIFO test, unavailable on Windows.
- One external `luajit` executable check in Python; the separate Lua gate uses Lupa's LuaJIT runtime.

The real-cache roadgeo tests did pass earlier locally (two anchor builds and road-skin seams), but their
results depend on existing local geographic data. They are not counted as reproducible CI coverage.
Lua keybinding checks report that BEAMNG_DIR is unset and inspect only this mod's map; engine keybinding
compatibility is not established. Stubbed engine behavior is not validation of the real renderer or physics.

Raw local logs: `build/quality-gate.log`, `build/quality-tests/*.log`, `build/baseline-tests/*.log` and
`build/foundation-baseline/*.log`. These generated logs are ignored by Git. Baseline failures were retained:
TLS test-double errors, Windows mount parsing and revisit-cache handle cleanup. Agent regression evidence
also reproduced invalid road inputs, multiplayer coordinate/expiry errors and range corruption/races.
The combined quality-gate process completed with exit code 0. `build/multiplayer-final.log` records the
subsequent affected-suite run; no full-suite rerun is implied after that narrow change.

## Integration review

Independent reviews covered range locking/read integrity, revisit descriptor lifecycle, CI dependencies,
road-test clocks and multiplayer expiry. Review found and corrected an expired-origin fallback that would
have passed untransformed Earth peer packets to BeamMP. Known expired peers now remain quarantined until
a valid origin is received; never-known legacy peers retain existing behavior.
Review also exposed inherited vector arithmetic before validation. Complete finite position, velocity,
angular-velocity and rotation components are now checked before conversion, then checked again after it.

The quality workflow runs on pull requests or manual invocation and is reused by release builds. Both
Windows and Linux package build jobs depend on it. It does not publish anything independently. Remote
branch protection settings have not been changed. Existing release triggers remain subject to operator
authorization; this session has not pushed branches or triggered those workflows.

## Unmet release acceptance

1. Execute CI on Windows and Linux from a clean checkout, including native dependency installation.
2. Test installed Windows EXE and Linux AppImage launch, setup, shutdown, restart and update recovery.
   Source ZIP construction and `--help` cannot establish installer quality.
3. Run two real BeamNG/BeamMP clients through join, leave, rejoin, ID reuse, delayed/out-of-order metadata,
   map reload, rebasing, dateline proximity and long-session driving. See
   [multiplayer acceptance](../beammp/MODERNIZATION.md).
4. Record controlled cold and warm drives with the same route/settings/hardware, including time to
   driveable ground, p50/p95/p99 frame time, generation stages, CPU/RAM/disk/network, cache hits and stalls.
5. Measure road discontinuities and vehicle response on flat highways, mountain grades, intersections,
   bridges, tunnels and tile boundaries. Missing-survey tests alone do not prove drive quality.
6. Measure packet loss/jitter/interruption recovery. Shared manifests and server-assisted cache reuse
   need their own two-client acceptance before automatic sharing is enabled.
7. Complete actual embedded voice, supported party flows, radio, asset import/LOD/collision and server
   administration before marking those requested workstreams implemented. None is certified by this gate.
8. Audit dependencies and notices in final native artifacts, credential handling and public-service
   authentication. In particular, explicit compute helpers currently use plain TCP/code fingerprints,
   not a production authentication boundary.

Release decision: **not ready for a production-quality full-platform release**. The foundation fixes are
locally reviewable and tested; engine-dependent behavior and all unimplemented work remain tracked in
[the milestone status table](MODERNIZATION.md#workstream-status-and-next-integration-contracts).
