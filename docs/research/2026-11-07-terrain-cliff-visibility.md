# Terrain shadows and cliff streaming, 2026-11-07

## Confirmed causes and fixes

At 36.01549 N, 114.74792 W, enabling the TerrainBlock castShadows flag removed
an incorrect sunlit band beside the cliff. This overturns the earlier flat-city
and Yosemite checks recorded in terrain.lua; it does not overturn their measured
shadow redraw cost on terrain moves. Terrain Shadows is now a saved graphics
option, applied immediately and retained across terrain imports and swaps.
Hidden physics-only terrain does not cast duplicate shadows.

Presets now include terrainShadows, cliffFaces, cliffRocks and cliffDecals, along
with the existing cliffDetail and distantCliffFaces. Low disables all those
features; Medium keeps nearby and distant faces but disables terrain shadows,
scattered cliff rocks and transition strips. High, Ultra and Extreme enable
all components. Individual overrides select Custom through the existing system.
Older preset settings acquire missing knobs through fillNewKnobs; explicit
saved overrides are retained.

The live game had missing terrain even with no pending work: 340 live tiles,
and numerous `far_swap ... unknown tile ...` warnings. An automated split /
merge / split sequence before the first import reproduced a missing final tile.
The latest pending instance for a key was being consumed by an earlier queued
swap. Each swap now holds the instance sent for that swap, and every unshown
generation remains accounted for until consumed or discarded. Queue imports,
slot retention, material lifetime, rebase and clear paths account for those
instances. Unclaimed prefetch replacements still use the newest tile.

New cliff faces and transition strips wait for visible ground covering their
whole XY bounds. Checking only the piece centre would miss a hole through its
edge. The committed visible TerrainBlock and settled regular far tiles count;
hidden preloads, failed tiles and moving crossovers do not. Block-edge
under-floors count as intentionally visible ground. New pieces conservatively
wait for current-frame regular terrain at a rebase/global boundary. Existing
pieces remain in place. Retry work is capped at eight pieces per frame, and a
held piece consumes no native shape-import budget. Legacy packets without bounds
retain existing behavior. New transition-strip packets now include bounds.

## Validation

- All 92 Lua test modules passed. Targeted terrain/far-field, cliff, settings,
  lifetime and rebase suites also passed after the final queue optimization.
- Eight Python cliff-stream tests passed, including strip support bounds.
- Protocol agreement: 32 tests, one skipped, no failures.
- In-game Graphics page: Terrain Shadows off changed the actual field to 0 and
  Quality to Custom; on restored field 1 and High. The toggle is visible in the UI.
- Refreshed live scene: 519 terrain tiles loaded, zero pending/failed tiles or
  waiting swaps. All 219 loaded bounded cliff faces had visible ground support;
  the road mesh queue drained. The final screenshot has continuous cliff-base
  shadowing. Car location is restored within a few centimetres; play is unpaused.
- The final sparse out-of-order-message waiter optimization is installed on disk
  and covered by the targeted tests; this running scene uses the equivalent
  generation-preservation implementation tested before that optimization.

Evidence: build/terrain-cliff-visibility-study/ (before.png, after.png,
final-play.png, settings-ui.txt, live-verified.json and lua-tests.txt).
Server descriptor was updated to PID 60520; normal revisit caching and the prior
BNE_CLIFF_COLLISION_SIMPLIFY=1 experiment remain. The accepted one-Forest-model
per-driving-frame behavior now runs directly from trees.lua after the reload.
No camera-travel benchmark or new driving performance comparison was run for
these visual changes. Terrain shadow performance remains hardware/scene dependent;
Low/Medium disable it. Release work remains on hold and no git commit was made.
