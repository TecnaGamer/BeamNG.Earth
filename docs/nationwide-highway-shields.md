# Nationwide highway shields

State, U.S. and Interstate shields are generated automatically by the normal sign
stream. Players do not install regional inventories or run an offline build.

The primary source is the road geometry already fetched for map streaming.
OSM way IDs join to the bundled, indexed road-route membership snapshot. This
snapshot contains network identities, numbers and membership, not sign positions.
Numbered shields are placed after actual junctions, using stable source coordinates
and existing pavement, building, terrain, one-way and tile ownership checks.
Unmapped roads are never assigned a number from their road class. Incomplete road
cells are retried instead of caching an incomplete junction graph.

The existing weekly sign shards still supply mapped markers and guide-board
locations. Layercake remains a bounded fallback if streamed-road context is not
ready. Saved fallback candidates and existing HTTP block caches improve outage
recovery; neither is a manual installation prerequisite. A first visit works from
streamed roads plus route membership even when Layercake is unavailable.

## Designs and guide boards

The data-driven registry contains verified standard designs for all 50 states,
ordinary U.S. and Interstate shields, and configured specialty systems including
Texas FM/RM/Loop/Spur and Tennessee primary/secondary. The Wyoming design includes
the bucking horse and rider. Correct route membership preserves federal shields
and explicit state identities near borders. Unavailable specialty designs use an
explicit network-labelled fallback, never another state's artwork.

Numberless templates and subset lettering generate cached 512-pixel DDS textures.
Faces are shared between standalone markers and state/special-design insets on
existing green roadside and overhead guide boards. Board size and placement are
unchanged. Existing ordinary federal shield meshes remain in use. The existing
material manifest registers generated faces before loading the sign mesh.

Licensing, original sources, authors and checksums are recorded in
`server/data/shields/NOTICE.md`, `artwork_sources.json`, `coverage.json`, and
`route_relations.manifest.json`. Standard artwork is public domain except the
attributed Minnesota wide derivative (CC BY-SA 4.0); lettering is Roadgeek MIT.
OSM route membership is ODbL-1.0. The Wyoming station sample is a test fixture only.

## Recovery and limits

A replaced Parquet dataset's HTTP 412 response no longer opens the host circuit
breaker. Failed version probes retry after 60 seconds instead of one hour; unique
atomic pin files avoid concurrent worker collisions. ETag/size validation, quotas,
Retry-After and cancellation handling remain enforced.

Generation and textures run in the existing sign worker, not per frame. Fixed
source-cell neighbourhoods, stable IDs, bounded caches and duplicate suppression
keep locations consistent across tile queries. No road geometry is modified.

Visibility still requires the ordinary sign-stream feature and generated roads.
Missing or obsolete OSM way membership can omit a marker. Streamed reassurance
markers currently use road-segment junctions with enough approach length; short
urban segments may be skipped. A source road shared by differently directed route
relations is resolved by membership; Overture geometry may reverse the original
OSM way, so original forward/backward roles are not treated as geometry direction.
Generated one-way rules govern physical facing and side placement.

Terrain, road matching, intersections, bridge decks, buildings and clearance can
suppress a candidate. Independently streamed props are not a unified occupancy map.
The implementation cannot promise every real sign or collision-free placement.
No in-game screenshot or nationwide drive has been used to claim verification.

## Building and tests

The application package includes all runtime modules, templates and membership
metadata through the existing server tree collection. No personal cache or
prebuilt per-state sign index is required or included. Development tools rebuild
numberless artwork and relation membership from pinned inputs; pyosmium is a
build/test dependency for the existing weekly sign-shard builder only.

Tests cover all 50 standard templates, relation-first recognition, federal/state
separation, special networks, number fitting, cache reuse, UVs and orientation,
streaming material registration, tile ownership, obstruction checks, first-visit
streamed generation with the Layercake path blocked, and source outage recovery.
