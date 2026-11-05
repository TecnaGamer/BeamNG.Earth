# Québec boundary — Statistics Canada Open Licence

Source: **Statistics Canada, 2021 Census cartographic boundary files, provinces
and territories**, PRUID 24 (Québec), accessed 29 October 2026.

https://geo.statcan.gc.ca/geo_wa/rest/services/2021/Cartographic_boundary_files/MapServer/0

Query: `/query?objectIds=5&outFields=PRUID,PRNAME&returnGeometry=true&f=json&maxAllowableOffset=10`.
EPSG:3347 coordinates are transformed to WGS84 by the builder. This requests
**10 metre service generalisation**, not exact legal/survey accuracy.

Distributed under the **Statistics Canada Open Licence**:
https://www.statcan.gc.ca/en/reference/licence . The repository's MIT licence
does not replace it. Statistics Canada has not endorsed this mod or adaptation.

The ZIP contains editable JSON rings and an index. Received islands and assigned
holes are retained without further simplification. Of 39 interior/reversed input
rings, seven isolated shoreline slivers of at most 25 m² could not be assigned
after service generalisation; these are explicitly omitted and counted in the
metadata. Larger unassigned rings fail the build. Output: 21,556 polygon parts;
input: 464,810 ring points. Source-byte hash and processing details are retained
in `index.json`. Shoreline and census-date uncertainty remain.

Builder: `tools/build_quebec_boundary.py` (build-only pyproj and Shapely).
ZIP SHA-256: `becfdfb3fc08c1adb450e2f941aef41a83512f8cf84800f76002f60a30fe38ae`.

Runtime requires country CA AND membership of the province polygon for ARRÊT.
No rectangular latitude/longitude approximation is used. Unknown/ambiguous
boundary positions keep the stock STOP model.
