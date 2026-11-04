# Horse Creek Road classification fixture

Captured read-only from the available BeamNG.Earth map cache on 2026-11-01. No network requests or cache writes
were used. The fixture replays original source geometry and original land-cover pixels, not a synthesized road
or an invented development mask.

* Location: latitude 41.2183589, longitude -104.833019.
* Overture release: 2026-09-23.1; mapdata v2 road layer `roads.9`, source cell `14/3420/6129`.
* Feature: `b41164c5-263b-41d5-9732-d2c5325b10d2`, Horse Creek Road, class secondary.
* Joined source record: OSM relation `r1512899@20`.
* Original geometry contains two vertices over approximately 196 metres. The archive retains the feature's
  original attributes and applicable road rules. It has no explicit sidewalk evidence or mapped width.
* Both ESA WorldCover cells touched by original and uniform-distance beside-road queries are included as their
  unmodified decoded mapdata rasters, with CRS, georeference, nodata, and file provenance metadata.

Under the original vertex-voting rule, both centreline endpoints are built-up and at least one 30-metre side
sample at both endpoints is built-up. That classifies the whole road as urban and expands it to 12.1 metres,
including inferred parking widths, even though almost all of its length lacks roadside development. Uniform
samples along the length produce only 18.18% either-side built-up share, below the unchanged 20% threshold.
The corrected specification remains a rural 8.0-metre road, with no inferred sidewalk.

`horse_creek_tertiary_urban.npz` captures a second actual Horse Creek Road segment near Roundtop Road, at
41.2182404, -104.9002486. Its ID is `c4ef93e7-5010-49e7-9658-f583d2186cd6`, from source cell `14/3417/6129` in the
same release and layer. It has two mapped lanes, paved surface, and no mapped width. Uniform sampling alone
leaves its 27.78% built-up roadside share above the old 20% threshold, expanding it to an urban 10.8-metre road.
The conservative default now requires 35% of distance samples to show surrounding development; this segment
retains its rural 6.2-metre treatment. Both original decoded WorldCover cells are included, as above.

The threshold is configurable with `BNE_URBAN_BESIDE_SHARE` (default `0.35`). This adjusts context classification,
not sidewalk authority, road lane tags, or mapped width. Explicit width remains authoritative even when a road's
urban context is classified. The uniform sample spacing is at most 20 metres and source geometry is unchanged.

Read-only comparison against actual cached WorldCover and original road geometry in 512-metre-square neighborhoods
found no changed eligible city-street classifications when moving the uniform rule from 20% to 35%: Cheyenne
downtown 42/42 remained urban (3,431 metres), the reported Cheyenne player location 7/7 (490 metres), west Cheyenne
9/9 (1,032 metres), and central Laramie 46/46 (3,923 metres). Cheyenne inputs were `roads.9`, while the available
Laramie inputs were `roads.7`; these are geometry/context comparisons, not claims about current sidewalk-tag
coverage or an in-game drive. In the Roundtop vicinity, the one remaining false urban Horse Creek segment changed
from 10.8 to 6.2 metres. Other rural roads in that neighborhood remained rural.

Road data: OpenStreetMap contributors and Overture Maps Foundation, ODbL 1.0; retain the project's CREDITS.md
and [OpenStreetMap attribution](https://www.openstreetmap.org/copyright).
Land cover: ESA WorldCover 2021 v200, [CC BY 4.0](https://esa-worldcover.org/en/data-access), including the project's
existing geographic warp during lookup (`surface.warp`). That warp is deliberately retained in the replay because
it is part of the actual road-classification query.

The fixture demonstrates this cached segment's classification error. It does not claim that every segment of
Horse Creek Road, Round Top Road, or Happy Jack Road has identical conditions.
