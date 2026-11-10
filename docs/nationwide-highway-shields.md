# Nationwide highway shields

Updated against upstream `main` at `e1c72abf` for **BeamNG.Earth 1.9.1-beta**. The integration retains current French sign rendering, legacy cached shield inventories, and the full building-aware placement API. Composite guide labels retain their compact text fallback, and new specialty shields retain compass qualifiers.

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
The user supplied four in-game screenshots for PR #969. They illustrate individual examples; they do not constitute a nationwide placement or performance benchmark.

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

## 1.9.1 efficiency and validation

Prepared shield backgrounds use a four-entry cache. Different route numbers copy the prepared blank before adding text, avoiding repeated image decoding and resizing without sharing mutable numbered artwork. The final numbered-face cache and existing content-addressed DDS reuse remain in place.

A comparison with the prior rasterizer produced byte-identical RGB output for **204 cases across all 50 states and selected specialty designs**. A synthetic batch of 24 distinct Wyoming faces improved from **76.701 ms to 32.040 ms**, median of five batches. This measures image preparation, excluding DDS encoding, game rendering, and FPS.

The updated branch ran **1,184 Python tests across 70 modules: 1,157 passed, 24 skipped, two environment errors, and one existing assertion failure**. Windows denies symlink creation in the two unchanged cache tests (`WinError 1314`). The font-atlas regeneration assertion also fails with unchanged upstream files under this machine's Pillow/FreeType installation. Optional real-cache geometry tests use `BNGE_ROADGEO_REAL=0`, as in the foundation gate.

All **103 Lua test modules**, both JavaScript map-math checks, OSM tag inventory, Python compilation, and source/BeamMP package builds passed. New regressions cover prepared-blank reuse without number contamination, French renderer compatibility, composite text labels, and specialty-shield direction qualifiers. User-provided screenshots are included in [PR #969](https://github.com/TecnaGamer/BeamNG.Earth/pull/969).
