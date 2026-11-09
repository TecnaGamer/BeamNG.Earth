# A11 self-underpass fix (#909 follow-up)

Location: 53.29391, 14.11574. Based on upstream `b2ccaf85`; Python with earthfast 172.

The 4.7 km and 10.8 km motorway features had different skew-expanded bridge ranges in different map cells. The underpass planner combined a long deck with overlapping ground from a shorter copy and lowered its own approach. Bridge-local crossing context and upper-road width now give every copy the same extents; source intervals also prevent self-overlap requirements without excluding genuine self-crossings.

| Saved-data measurement | Before | After |
|---|---:|---:|
| Affected deck's low end | 18.9787 m | 26.5048 m |
| Affected deck's maximum grade | 40.98% | 5.49% |
| Bridge lengths across eight cell copies | 15.42–43.84 m | 43.5636 / 43.8354 m |
| Modeled usable clearance over the lower road | — | 7.27–7.34 m |

These are source/profile checks, not surveyed dimensions or a live driving/collision sweep. The original #920 railway-overpass case at 53.250687, 14.019295 was also checked: both short decks remain level around 75.2 m. No artwork renders or mesh exports were needed for these probes.

## Regression checks

- 12 new offline cases cover long-road copies, cell boundaries and loading order, several bridges, differing inferred widths, missing context, concurrent readers, self-overlap and chains, real self-crossings, distinct lower roads, reprojection noise, and provenance construction.
- The focused run passes 156 tests: `test_bridge_ranges test_grading test_deckjoins test_underpass_profiles test_bridgeprofile test_bridge_level_spans test_bake_formats`.
- Independent review found provisional range publication and exact-float duplicate recognition defects. Each was reproduced with a failing test and corrected. Natural smoothed copies differing by 4.7 micrometers now give the same 42.763153155 m lower-road height with one or five copies.
- 104 Lua modules, both node map-math suites, 12 Rust serverlist tests, and mod/BeamMP packaging pass.
- Cache formats: road facts 10 and plans 26; previous facts/plans are rebuilt. These follow the 9/25 versions already used by upstream's #1008.

## Broad-suite environment notes

The following failures were independently reproduced on unmodified upstream:

- `test_advance_sign_layout.AdvanceLayoutTest.test_atlas_is_the_bundled_font_regenerated_and_licensed`: this virtual environment cannot import `fontTools`. The test passes when the already-installed user-site `fontTools` package alone is exposed through an isolated PYTHONPATH.
- `test_imagery.NetworkImageryTest.test_open_ocean`: the imagery chain selects `eox2023`; the test expects `eox2016`.
- `test_fargeo.RealDataTest.test_content_identical_from_two_anchors` and `test_one_owner_per_building`: required cached Overture building data is absent and the test uses an offline fetcher.
- `test_roadgeo.RealDataTest.test_content_identical_from_two_anchors` and `test_skin_on_the_drawn_ground_and_seams`: the same cached-world data limitation.

The final broad run after review fixes completed in 386 s: 304/308 modules passed, with only the four baseline failures above. All four `test_otto_bridge` cases passed; the module took 301 s. The first broad run had used a shorter 300 s per-module timeout and stopped that slow offline setup prematurely.

The final server command was `BNGE_SKIP_DRIVE=1 BNE_CACHE_DIR=/tmp/issue909-cache python tools/run_tests.py --par 4 --timeout 1500 --logs /tmp/issue909-suite-final`. The shared cache contained saved map/terrain data; generated cache entries were isolated in `/tmp`. No live driving check was run.

## Integration with current dev

Merged upstream `0dc7b4bb` into the PR branch. The only textual conflicts were the two cache-format declarations: upstream's #1008 and this PR had independently used facts 9 / plans 25. The combined tree retains both changes, uses facts 10 / plans 26, and regenerates the fixture through `BNGE_UPDATE_GOLDEN=1`. Its facts digest and sampled plan heights match upstream's fixture exactly; the version numbers advance to invalidate earlier cached A11 results.

All 482 tests in the 16 affected modules pass: `test_bridge_ranges test_grading test_deckjoins test_underpass_profiles test_bridgeprofile test_bridge_level_spans test_bake_formats test_gradesep test_roads test_tunnels test_railtags test_regionaldata test_fences test_plancache test_mapdata test_roadz`. The actual A11 source/profile probe also retains the measurements in the table above.

The earlier GitHub Actions run on PR head `f2f428ad` failed to import `zstandard` in `test_germany` on both Python 3.12 runners. Its Windows `test_bake_formats` also reported two of 5,929 heights differing by up to 0.2121 m; Linux passed that test. These CI results are separate from the cache-declaration conflicts. The Windows difference was not reproduced locally, so local checks do not establish a passing Windows result.
