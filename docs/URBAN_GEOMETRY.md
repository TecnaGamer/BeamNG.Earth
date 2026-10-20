# Grounded buildings and connected urban frontages

This patch addresses generator defects behind floating building bodies, inflated
wall footprints and short gaps around sidewalks. It was extracted from
[the tested fork changes](https://github.com/hashk014/BeamNG.Earth/pull/4)
onto upstream `main` at `011d6b5` without the fork's cache, water, settings,
launcher, release or branding changes.

## Building behavior

- A road crossing a large footprint no longer lifts the entire building by
  5.5 m. An inferred local passage retains grounded wings and the original top.
  Explicit bridges, parking decks, elevated parts and tunnel portals retain
  their existing treatment. Roads inside courtyards do not lift the walls.
- When mapped building parts cover only a roof structure or one wing, the
  uncovered parent body is retained up to the first mapped support height.
  Overlapping parts are subtracted as a union. Disconnected components and
  mapped courtyard holes are preserved in the visible, LOD and collision meshes.
- Related parts share a foundation datum. Boundary samples include intermediate
  points along long walls, batched across the tile, and all production building
  paths sample the graded terrain used by the road pipeline.
- Walls, doors and collision keep the mapped footprint. Guessed pitched roofs
  require a near-rectangular outline; mapped ridge direction and roof height
  remain consistent with collision. Building appearance is stable across tiles.
- Roof equipment requires its whole footprint to be supported, including near
  concave notches. Parent support fragments do not acquire extra roof clutter.

## Sidewalk behavior

Intersection cutting retains sidewalk pieces below the former three-metre
minimum without changing the marking minimum. End joins accept gaps below
30 cm and coincident endpoints without duplicate vertices. A chain's endpoint
orientation is recalculated after earlier merges, and separate levels stay apart.

Nearby facing facades receive concrete paving from an existing sidewalk's back
edge. The visible, LOD and collision meshes share its top geometry. Nearby runs
are collected before tile clipping so a tile boundary does not discard support.

## Deliberate limits

An inferred passage reserves five metres across, requires at least 15 m of road
inside the solid footprint and retains at least 25% grounded support area.
Ambiguous overlaps retain their mapped walls. These rules cannot recover the
real structure of every underpass from incomplete source data.

Paving only covers facing gaps up to three metres and rejects ground differences
above 0.75 m. It does not cover large gardens, opposite sidewalks or rural
properties without generated sidewalks. Other sidewalk gaps can still need
source data or junction-specific investigation.

## Reproducible checks

Install the server requirements, then run from the repository root:

```sh
python -m pip install --only-binary=duckdb -r server/requirements.txt
python tools/check_urban_geometry.py --report dist/urban-fallback-validation.json
python tools/build_native.py
python tools/check_urban_geometry.py --native --report dist/urban-native-validation.json
```

The first run explicitly disables the bundled native module. The second requires
it and includes its equivalence tests. Local Windows results on Python 3.12.10:
235 fallback tests and 266 native tests pass, with no skips. This includes
20 urban regressions and eight building-fidelity regressions, plus neighboring
terrain, roads, materials, water, vegetation and traffic suites. CI runs both
modes on Windows 2025 and Ubuntu 24.04 and uploads the reports.

The fallback run also exposed an existing `TypeError` in pier placement: its
road-clearance test called `any()` on a single Boolean. The same test fails on
unchanged upstream `011d6b5` with the native module disabled. Reusing the existing
segment-distance helper fixes this fallback; the native pier path is unchanged.

Earlier fork-only offline reconstruction of two real Les Sables-d'Olonne sectors
produced 292 building items, 149 paving pieces and 13 encoded mesh files with
valid indices. That experiment included other fork changes; it is supporting
evidence, not a measured result for this extracted upstream branch.

The player reports that the corrected fork looks better in game. The exact
original floating annex remains unidentified, and collision at speed, all
original upstream report locations, rendering and performance still need
specific gameplay checks. No universal fix, FPS gain or new release is claimed.
The full timing-sensitive server suite is outside these checks.

Related reports: #2, #48 and #60 (floating buildings), and the continuity subset
of #24 and #29 (sidewalks). These references are not issue-closing claims.
The open PR #64 also touches building/road overlap; its removal policy and this
patch's local supported passages need to be reconciled if both are accepted.
