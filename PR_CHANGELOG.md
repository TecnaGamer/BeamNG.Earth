# Fix US highway lane counts and OSM lane resolution

Untagged urban highways could gain a third lane, and nearby same-named roads could supply the wrong highway lane count. This ports the relevant 1.7.0 custom-build behavior onto 1.7.1-beta while using the newer road-marking and navigation pipeline.

## Changes

- Default untagged motorway/trunk ways to **two lanes**, in towns and in the countryside. On divided highways this means two lanes per carriageway; on two-way roads it means two lanes total. Untagged one-way ramps default to one lane.
- Give valid mapped OSM counts priority over defaults. Reject fractional, conditional, out-of-range and ambiguous list values instead of truncating them into a lane count.
- Recover changed OSM way IDs using closely aligned current highway geometry, matching road class, ramp status and travel direction. Reject conflicting nearby matches.
- Fill missing highway counts only when aligned, connected approaches at both ends agree. Keep explicit lane additions/drops; do not borrow counts from opposing carriageways, ramps, forks or crossing roads.
- Use the same resolved count for ground roads, bridges, tunnels, lane markings and AI. Preserve mapped odd totals, directional splits and shared turn lanes; conflicting directional tags cannot increase the total.
- Consume total counts from the lane-detail shards as well as Layercake. Refresh derived road/structure specs when tags change, and advance the roads cache format to version 8.
- Restore the narrowly scoped I-65 southbound Exit 76 correction from the previous build: one generated ramp lane for OSM way `16257189` within its Upton bounds. The downloaded OSM tag remains intact.

## Validation

- **438 tests passed across 22 suites** on Windows, using Python 3.12 and the supplied version-100 native module.
- Coverage includes lane defaults and OSM resolution, real local Parquet decoding, directional paint/AI agreement, bridges, tunnels, road meshes, terrain grading, vegetation, cache refresh and the captured I-80/Otto Road replay.
- The existing corner-grading fixture explicitly retains its original 13 m road width, keeping that geometry test independent of the changed lane defaults.
- `git diff --check` passed.
- An in-game drive has not been performed.

## Scope and data

Based on `8aaf4eb` (1.7.1-beta). As in the previous custom build, the fallback uses the shared motorway/trunk classes worldwide; it has no US-only geographic filter. Other custom lighting, signs, road-width profiles and curve changes are outside this PR.

OSM `lanes` counts all motor-vehicle lanes on a way, including both directions for a two-way road; directional tags refine that total. See the [OSM lane-tag documentation](https://wiki.openstreetmap.org/wiki/Key:lanes). Geometry and connected-road matching are conservative fallbacks, not proof that every mapped road is up to date.
