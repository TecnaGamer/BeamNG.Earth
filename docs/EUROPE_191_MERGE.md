# PR #505 on official 1.9.1-beta

Base: TecnaGamer/main `e1c72abf66a42f6e877c3e1de8839352360913fc`.
Original contribution: `eb73b0f7f02b1ddc42664d5da7ffcd095469daa2`.

The first scenery integration was squashed and adapted upstream in `a30fd93`.
This merge retains that adapted implementation and the subsequent official
changes, then ports the seven final contribution commits after `d9e08951`.
In particular it retains native country lookup, lazy regional data, highway
shoulders/state markings, bridge-aware barriers, wind turbines, railway cuts,
traffic-light clearance, golf terrain and the 1.9.1 fixes.

## Preserved additions

- European sidewalk surface palettes, mapped footpaths and village widths.
- Paved frontages and terrain grass integration, mapped residential parking.
- French fence models, inferred boundaries and hedge clearance/rotations.
- Surveyed French cameras, warning-board textures and client daylight flash.
- Italian roadside markers and the final direction-routing corrections.

Road facts format is now 8; map road-layer version is 10. The existing plans
format also includes the facts format. Retained tiles use the new code identity.

## French direction data

Upstream deliberately removed the large archived datasets from git and the app.
The final checked direction routes therefore require an updated regional data
build, not simply the Python changes. Do not re-add the old ZIPs to the PR.

`python tools/prepare_pr505_regional_data.py` reads the final archived sources
from git history and converts all regional datasets using upstream's
`build_regional_data` converter, adapted to stream one cell at a time instead
of holding millions of panel objects in memory. Output is untracked under
`build/regional-pr505-eb73b0f7`. Nothing is uploaded or published by this tool.
It requires numpy, Pillow and zstandard from the project's Python environment. The first
conversion needs several minutes, disk space and memory; subsequent runs reuse
the prepared directory.

The resulting regional folder is suitable for the data-host publication
workflow. Until that publication, `tester_france.bat` uses the local build when
present through upstream's existing `BNE_REGIONAL_DIR` override. Without it,
the server continues to use the currently published upstream datasets.

## Test launch on Windows

Close the game and all world servers. If dependencies need updating, run
`py -3.12 install.py` from the project root first.

1. Run `preparer_donnees_regionales.bat` once for the final direction data.
2. Run `tester_france.bat` and open the BeamNG.Earth level.

The test launcher explicitly enables French roads/houses and European road
details, uses `--regional-poles europe`, and disables the other town-entry sign
system with `BNE_TOWNSIGNS=off` to display this contribution's own sign models.
Official global defaults/settings and the alternative town-sign implementation
are otherwise unchanged. There is no license-plate branch integration.

## Validation

Python syntax compilation, the regional/road/geometry/packaging regression
suites and offline Lua tests were run. New integration coverage checks camera
rows through retained geotiles and skins, and French fences on official bridge
decks. Tests of the English sidewalk fixture use explicit offline geography
because upstream's small regional-data fixture covers Alsace, not England.

The existing Lua tree scheduling test failed once during the full run and
passed in isolation; its frame-time assertions depend on CPU timing. Some
upstream tests intentionally skip real-data or external-Lua cases, and four
previously marked stale direction tests remain skipped.

These checks do not replace an in-game Windows test. Compare the same locations
for curbs/frontages, residential parking/fences, country signs and camera flash
before pushing the merge.
