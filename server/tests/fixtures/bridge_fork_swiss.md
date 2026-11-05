# Swiss bridge fork collision fixture

`bridge_fork_swiss.json` is a compact, millimetre-rounded geometry replay of the
reported bridge near **46.66729 N, 7.72157 E**. It contains three solved bridge
profiles, their two generated junction pads and rim walls, and the two connected
ground-road polylines. Coordinates are local metres in geotile 68347/46278's
frame. The original screenshot and cached replay were inspected on 2026-11-03.

Source geometry: **Overture Maps Foundation**, release **2026-09-23.1**,
transportation roads, map cell 14/8543/5784; underlying **OpenStreetMap
contributors**, ODbL 1.0. The feature identifiers are retained in the JSON.
Elevation inputs came from cached **Mapterhorn** terrain via the project's DEM,
grading and bridge-profile solvers. These are derived solved heights, not raw
survey data. See [project data credits](../../../CREDITS.md), including the
Mapterhorn underlying-source attribution, and retain this notice with the fixture.

No raw map cells, imagery, credentials or gameplay capture are included. This
fixture tests mesh construction from known profiles; it does not independently
validate the geographic survey or the upstream elevation solver. The larger
local replay separately reran those stages against the existing cached inputs.

Before the correction, independently sloped slabs and a centre/rim fan overlap
by about **0.36 m** on the driving lines. A second pad overlays the curved ground
approach and ends roughly **0.63 m** above it. Tests intersect actual triangles
along road centres and both tyre tracks, test asphalt/marking alignment, and
check that the connecting ground carriageway retains its terrain collision
without an additional pad floor or wall.
