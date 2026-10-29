# European infrastructure contribution — v4

Base: public v1.7.1-beta, `8aaf4ebdd0485facadc755a612f4b0a2b55e4c57`.
Enable with `python start.py -- --regional-poles europe`; default is `off`.
Runtime code, models and geographic data match the tested Europe v4 package.
The contribution preparation corrects outdated documentation only.

## Review map

| Area | Main code | Detailed notes |
|---|---|---|
| Position-based country lookup | server/countrybounds.py, server/poleprofiles.py | COUNTRY_BOUNDS_EXPERIMENT.md |
| 56 country/territory profiles and 44 added models | server/europeinfra.py, tools/build_european_infrastructure.py | EUROPE_INFRASTRUCTURE.md |
| French poles, lanterns, signs and dark sidewalks | server/regionalmarkers.py, server/townsigns.py, server/directionsigns.py, server/sidewalks.py | FRENCH_FURNITURE_EXPERIMENT.md |
| Country deployment and short rural road posts | server/delineatorprofiles.py, server/delineators.py | DELINEATOR_RESEARCH.md |
| Quebec ARRET and Turkey DUR appearances | server/regionalstops.py | EUROPE_INFRASTRUCTURE.md and data notices |
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
| navgraph | 11 |
| sidewalk_collision | 8 |
| blockworker | 5 |
| revisit | 4 |
| Total | 162 |

Lua: trees/furniture 104, roads 95, nightlights 14: 213 assertions.
The 113 regional tests also passed in the contributor's Windows installer.
Tests include real country/Quebec boundary cases, text and mesh generation,
tile splits, all four German rural road classes, short features, curve density,
lighting/speed exclusions, road clearance and France's preserved sparse J6.
The independent Shapely comparison covers 7,493 positions on the same dataset.
These checks do not establish source survey accuracy or in-game frame times.

Earlier French pole rendering and FR/DE transitions were observed in game.
Complete Europe v4 importing/rendering, headlights, night lighting, LODs,
perceived collision, repeated visits and Windows gameplay performance still
need testing. The model preview sheets are generator renders, not game captures.
The full upstream suite is not claimed green: a prior bare-verge-paint failure
was reproduced on the pristine public base; the affected suites above passed.

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
