# European infrastructure contribution — v4, integrated with 1.7.2

Current base: public v1.7.2-beta, `8375d5171454dcc8ca9005f9a34bf37229ff9523`.
Originally submitted against v1.7.1-beta, `8aaf4ebdd0485facadc755a612f4b0a2b55e4c57`.
Enable with `python start.py -- --regional-poles europe`; default is `off`.
The model and geographic-data set remains Europe v4. The runtime now uses
1.7.2's retained geotile content, area/route prebuilding and native sidewalk
geometry. See [EUROPE_172_MERGE.md](EUROPE_172_MERGE.md) for the integration and
new regression checks. This filename is kept so existing PR links still work.

## Review map

| Area | Main code | Detailed notes |
|---|---|---|
| Position-based country lookup | server/countrybounds.py, server/poleprofiles.py | COUNTRY_BOUNDS_EXPERIMENT.md |
| 56 country/territory profiles and 44 added models | server/europeinfra.py, tools/build_european_infrastructure.py | EUROPE_INFRASTRUCTURE.md |
| French poles, lanterns, signs and dark sidewalks | server/regionalmarkers.py, server/townsigns.py, server/directionsigns.py, server/sidewalks.py | FRENCH_FURNITURE_EXPERIMENT.md |
| Country deployment and short rural road posts | server/delineatorprofiles.py, server/delineators.py | DELINEATOR_RESEARCH.md |
| Quebec ARRÊT and Turkey DUR appearances | server/regionalstops.py | EUROPE_INFRASTRUCTURE.md and data notices |
| Streaming, LOD, collisions and night-light reuse | mod/beamng_earth/lua/ge/extensions/beamngEarth/{trees,roads,nightlights}.lua | accompanying mod/tests |

The base junction functions, classification constants and navigation call paths
are preserved. The entire Python/Lua navigation files are unchanged. The STOP
localizer replaces only the model key after public generation. Public placement
approximations therefore remain. No new runtime package or native build is added.

## Test evidence

| Python suite | Tests |
|---|---:|
| regional_poles | 17 |
| countrybounds | 12 |
| regional_furniture | 20 |
| regional_revision | 14 |
| europe_infrastructure | 25 |
| delineator_density | 25 |
| furniture | 21 |
| navgraph | 12 |
| sidewalk_collision | 8 |
| blockworker | 5 |
| revisit | 6 |
| regional_geotiles | 4 |
| roadgeo | 9 (2 skipped: real map cache absent) |
| geotiles | 14 |
| areas | 17 |
| signready | 1 |
| Total | 210: 208 passed, 2 skipped |

Lua: trees/furniture 116, roads 95, nightlights 14, roadgeo 37: 262 assertions passed.
These are targeted integration checks, not a claim that the entire upstream
test suite was run. Tests used the official 1.7.2 earthfast module, version 114;
the native binaries in Git are unchanged relative to upstream.
The 113 regional tests also passed in the contributor's Windows installer.
Tests include real country/Quebec boundary cases, text and mesh generation,
tile splits, all four German rural road classes, short features, curve density,
lighting/speed exclusions, road clearance and France's preserved sparse J6.
The independent Shapely comparison covers 7,493 positions on the same dataset.
These checks do not establish source survey accuracy or in-game frame times.

The contributor reported successful in-game testing of the 1.7.1 version before
submission. The 1.7.2 integration still needs an in-game check, especially a
rebase/teleport, prebuilt area revisit, night lighting, LODs and collisions.
The model preview sheets are generator renders, not game captures. An earlier
bare-verge-paint failure was reproduced on the pristine 1.7.1 public base;
no claim about that unrelated case on 1.7.2 is made here.

## Data and remaining decisions

The complete contribution includes all five runtime geographic archives, about
76.2 MB in total. They contain editable derivatives, notices, source pins and
rebuild tools. The country geometry archive alone is about 43.8 MB. An earlier
review-only patch excluded these archives; it is not sufficient to run this set.
Data distribution and update cadence remain maintainer decisions.

Overture/OSM derivatives retain ODbL notices, Quebec data retains the Statistics
Canada Open Licence, and the font/atlas retains the DejaVu notice. Original
procedural code/models/textures follow the project licence. Reference photos
are not redistributed. fontTools, pyproj and Shapely are build tools only.

Models and templates are representative. Full bilingual layouts, every national
font and pole variant, exact real sign locations and vegetation clearing are
not implemented. Fourteen countries have sourced delineator policies; five
territories use explicit analogies, and 36 other non-French profiles keep
provisional v3 density. Historical sources and scenery choices are documented.

Natural review/split boundaries are country lookup; profile/model registry;
town/direction signs; localized STOP appearances; sidewalk palette; delineator
deployment. Keep the opt-in default until the maintainer chooses integration.
When rebasing later, obtain the matching official native module and rerun the
relevant tests. Do not overwrite a newer public source tree with this overlay.
