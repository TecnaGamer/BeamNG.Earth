# Small low-lying coastal areas and sea tiles

Base: `dev` at `2cbc26ce`. Related: #812; the broader #838 report has no coordinates.

## Evidence and cause

The aerial at 1.637555, 7.420206 shows Santo António's streets beside the river mouth, on land.
The report supplies a street-level reference and an in-game wireframe. Mapillary credentials and a
BeamNG runtime are unavailable on this machine; these are server-side measurements, not an in-game drive.

Using a fresh, isolated cache, Overture release `2026-10-21.0`, and the production DEM/carver:

- At the reported street point, the DEM is -1.375248 m. The water raster returns depth 0 and no water level:
  it is mapped land, not a water polygon.
- The near 2,048 m square has 39/4,096 low-land samples (0.952%). It passes the old 5% tile-wide allowance
  and becomes one sea WaterBlock over the town. Its low-land samples reach -2.831 m.
- A near tile now splits for any sampled dry land below sea level minus the existing 0.3 m tolerance.
  The numerical decision is native Rust; Python batches the samples and consumes keep/split/drop decisions.
  Distant DEM-band tiles retain the 5% allowance and 2 m tolerance. The 64 m floor, distance-dependent
  floor, majority-water rule at that floor, and 200-tile budget remain.

## Measured scope

The A/B probe uses the four production near leaves (each 2,048 m square) around an anchor at the reported
point. It retains the production sampling, sea mask, DEM zoom selection, and recursive splitting.
This isolates the affected near-water policy rather than measuring the whole 8 km scene.

| Santo António, same cached inputs | Before | After |
| --- | ---: | ---: |
| Sea tiles from those four leaves | 1 | 16 |
| Sea tile covers reported street point | yes | no |
| Dry low-ground samples covered on a 31 × 31, 40 m grid | 26 / 39 | 2 / 39 |

The two remaining sampled overlaps are the existing 64 m shoreline approximation. This does not claim
pixel-exact coastlines, correct the DEM's vertical error, or prove resolution of unrelated flooded bridges.
The new plan is computed for each session; road facts, grading plans, and carved-water rasters are unchanged,
so their persistent format numbers do not need a bump. Rebuild native API 159 before starting the server.

The regression constructs a small low town occupying less than 1% of a coastal square. It fails on the
unmodified baseline and passes with the patch; it also checks that adjacent sea is retained.

At Lower Manhattan (40.7081, -74.0082), the same four-near-leaf probe grows from 4 to 86 sea tiles;
the eight sampled dry low-ground points covered before are no longer covered. This remains below the
200-tile ceiling but increases the number of engine objects. Rendering/frame-time impact has not been
measured; no in-game performance claim is made.

## Checks

- Windows x64 MSVC native Release build, API 159.
- `test_seatiles`: 7 tests; `test_seacarve`: 32; `test_riverlines`: 10, all pass.
- Lua `test_sea` and `test_subsea_water`: 127 assertions pass, offline/stubbed.
- `test_protocol_agreement`: 32 tests pass (one platform-dependent skip).
- `git diff --check` passes. No dependency, workflow, or tracked binary changes.
