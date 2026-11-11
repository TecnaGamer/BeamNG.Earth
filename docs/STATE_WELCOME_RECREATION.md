# Real state welcomes and brown recreation signs

Implemented in the 1.9.1 workspace. Signs are generated through normal road/sign
streaming; there are no manual sign-location inventories and no per-frame image
creation or image downloads. This session did not restart or hotpatch the server.

## Current welcome faces

The registry has 50 states plus D.C. Every entry is explicit about its status.
The user-selected policy is newest_available: verified current faces remain
enabled, and other states use the newest usable reference located during the asset
review. A reference's date and status are preserved; older available artwork is
not described as a verified current installation. Missing artwork never silently
becomes another state's sign or a shared generic blue board.

- Colorado: the current handmade wooden Welcome to Colorful Colorado design,
  confirmed by CDOT; wood framing and the cream-lettered face are represented.
- Wyoming: the scenic-postcard design introduced in 2024, confirmed against
  WYDOT's annual report and contemporary images. The retired Forever West design
  is not presented as current.
- Alabama, California, Georgia, Louisiana, Mississippi and Pennsylvania:
  contemporary photographed variants with source dates and licenses in the registry.
- Additional states use dated newest_available records. Run the welcome audit for
  the enabled count and per-state sources; the count is not a current-design claim.

Coverage after the 2026-11-10 asset review: 49 states have enabled faces, consisting
of eight verified-current and 41 newest-available references. Hawaii is explicitly
not applicable to highway border signs because it has no land road border. Airport
arrival artwork and landscape photographs are not substituted. D.C. remains a
separate pending territory entry and is excluded from the 50-state count.

Source quality varies. Where a newer photograph was obstructed, underexposed or
specific to the wrong road system, the clearest usable documented variant was
selected and the exception recorded. North Carolina's latest located face is
motion-blurred; several other photographed faces have limited detail. New Jersey
uses a generic state face instead of marking every road as the Turnpike. Dates,
credits, terms and selection notes for these additions are in AVAILABLE-NOTICE.md.

Image sources, asset SHA-256 values, design evidence and reuse notes are in
server/data/welcome/registry.json and NOTICE.md. Colorado's photo is CC BY-SA 2.0.
Wyoming's supplied WYDOT artwork has attribution but its reuse terms have not been
independently verified; it is not labelled public domain or MIT. Review those terms
before public redistribution. The noncommercial nature of the project does not
itself establish a license.

The original image pixels are retained. A projective transform rectifies the face
directly from the full-resolution reference into a cached 2048-pixel DDS.
Concave state-shaped faces use cached triangulated cutout outlines, with opaque
fronts, backs and edges. Monument variants use a low plinth instead of metal posts.
Generated DDS textures are cached by artwork and configuration content. Face and
mounting dimensions are configured approximations, not surveyed installation plans.

Large state-specific welcome signs appear only on Interstates, US highways and
numbered state routes. Cached road_routes memberships and ranges determine
eligibility; road class, width and name alone do not. County, local, farm/ranch
and track roads receive no welcome boards. This applies to both inferred signs
and OSM nodes tagged board_type=welcome_sign.

For mapped nodes, the inscription selects the state and the closest eligible
directed road determines the facing direction. If the node is closest to a
local road or track, it is omitted instead of being assigned to a nearby
highway. Mapped positions are checked against pavement, buildings, decks and
terrain; unsafe mapped positions are not moved into a lane or gore.
When no usable mapped board exists, streaming infers a board on the receiving
side of a qualifying highway crossing between US states. Connected source
sections are followed across the border. The default station is 90 m into
the state, with a bounded search up to 1500 m for safe ground. These inferred
boards are game scenery, not claims that real signs stand at those stations.

Mapped signs take priority within the same directed road corridor, including
across streaming-cell seams. A safe mapped board suppresses its fallback; an
unsafe mapped placement leaves the fallback available. Stable IDs and crossing
deduplication avoid repeated markers. Normal terrain, building and pavement
checks still apply. If the first inferred station is near a side road, generation
tries the next station along the same receiving road.
Inferred boards use the same physical panel-overlap check as mapped boards,
with 2 m clearance. A green advance guide within 20 m no longer hides a
separate, clear highway welcome panel.

The Layercake POI file discovers nearby tourism=information node IDs, but does not
retain board_type or inscription. Only near supported state borders, small batched
OSM node API reads retrieve those tags and exact coordinates. These use the shared
fetcher, a one-request-per-second policy, demand-only access and a weekly disk cache.
Cached records remain available offline; failed reads are retried rather than cached
as empty. No runtime Overpass or OSM map-bbox queries are used. Source roads retain
their ground sections even when another part of the feature is a bridge or tunnel.

I-25 pilot map nodes: Colorado 12208361207 at 40.9982641, -104.9070974;
Wyoming 12208361208 at 40.9994853, -104.9062861. Both passed normal road, terrain
and building placement checks in an offline replay after the source records were
fetched. This is not an in-game visual confirmation.

Pine Bluffs fix: OSM node 11555306069 (Welcome to Wyoming) was being suppressed
by a generated advance guide about 8 m away, despite their panel footprints being
clear. Mapped welcome boards now use oriented physical envelopes with 1 m clearance
instead of the blanket 20 m crowding radius. Actual overlaps remain disallowed.
Full signstream exports include both mapped NE/WY boards at Pine Bluffs and
unmapped receiving-state boards on I-35 at TX/OK and US-163 at AZ/UT. US routes are
explicitly restored to the large-board category by the latest user clarification. Those are
terrain/building-backed offline exports, not live in-game confirmations. Reports
are in probes/border-stream-replay/report.json. No server restart was performed.

The optional small green welcome panels were removed. A native source replay
returns no welcome boards on local Lyons Highway crossings at the Michigan-Ohio
border. At the Michigan-Indiana I-94 crossing near 41.76018, -86.74979,
OSM node 6265416195 is closest to a track, so it does not create a board.
A full signstream replay places the large Michigan board on I-94. Earlier
small-sign previews and reports under probes/ are retained as historical
diagnostics, not current output. The running server was not restarted.

## Recreation

Brown boards use white names and borders, with categories such as RECREATION AREA,
NATIONAL PARK, NATURE RESERVE, CAMPING and PICNIC AREA. Names come from mapped,
named recreation polygons. Whole, unclipped geometry prevents streaming-cell edges
from being mistaken for entrances. Interior holes are respected.

Boards are generated on a suitable local access road before it actually enters
that named area. An adjacent road or an area's centroid is not used as proof of
access. Unnamed, private, inaccessible and disused areas are skipped. Passing
Interstates do not receive guessed park entrance signs. This initial version does
not create long-distance park navigation, invented turn arrows, or mileage claims.

Both kinds follow terrain, avoid intersections, bridge decks, generated sidewalks
and building footprints, and use one deterministic tile owner. Existing nearby
signs suppress crowded placements. Normal safety/geometry filters can omit a
candidate; other independently streamed props are not a unified occupancy map.
An in-game drive remains necessary before claiming placement verification.

## Supplemental recreation exit guides

A second brown-board layout supports a separate EXIT-number plaque, destination
lines and a posted instruction, on two metal posts. The first verified entry is
northbound I-25 before Exit 243 near 40.193228, -104.979911: Rocky Mtn / Nat'l Park /
NEXT RIGHT, matching the supplied photograph. The placement station is projected
onto the matching directed mainline, and the whole panel stands off its pavement.
Normal terrain, building, intersection, deck and tile-owner checks apply.

`server/data/recreation_guides.json` holds verified destinations, instructions,
exit numbers, dimensions, approach bearings and source notes. The normal streaming
source reads it; there is no manual offline build step. A new entry needs evidence
for that particular posted destination/exit, rather than a nearby park centroid.
The I-25 exit is OSM junction node 55722015 and ramp way 7673494; their destination
tags name Longmont/Lyons and do not establish the park board. The supplied photo
is the source of that supplemental message. No copy of the photo is redistributed.

The photo-verified supplement remains authoritative. Nationwide automatic boards
also read posted park destinations from exit records. If the weekly exit inventory
is unavailable, streamed ramp OSM way IDs are hydrated in small cached API batches.
An explicit park destination, actual exit number and connected directed mainline
are required. Existing green signs remain; the park gets its own brown panel.
Unknown destination messages or exit numbers are never invented. Upstream boards
have deterministic tile ownership and support left exits. Incomplete OSM tags or
short/disconnected approach geometry can leave an exit without a brown panel.

## Forest entrance boards

The forest_entrance layout recreates the tapered, rounded cream-and-brown Forest
Service entrance-sign family, with timber posts/crossrails, tinted lettering,
USDA footer and a public-domain USFS shield (USFS-NOTICE.md beside the asset).
It uses opaque static materials. The forest/area/agency labels and dimensions are
configuration data, not guessed for every recreation area. Lettering, dimensions
and the current shield approximate the supplied historical-looking photograph;
the exact script and fee-area emblem from that photograph are not reproduced.

The Vedauwoo pilot is on mapped access way 15674422, entering northeast along the
Picnic Ground Complex Road. Its station near 41.1550764, -105.3816071 is approximate:
OSM does not identify the pictured board's surveyed position. USDA's directions
confirm the signed turn from Vedauwoo Road. The local terrain/building/road replay
placed the panel off pavement; final in-game visual verification is still needed.
Nationwide entrance detection additionally reads whole protected-area polygons
from OSM US Layercake parks and Overture. Connected access-road sections are joined
before testing an outside-to-inside crossing, avoiding false entrances at tile
edges or polygon holes. Medicine Bow–Routt National Forest was found in the Snowy
Range area through this source; the actual mapped forest name is retained.

National forests and grasslands use the Forest Service family. National parks use
the same entrance-board layout with National Park Service text, Department of the
Interior footer and NPS arrowhead. State parks use their mapped agency and no
invented federal badge. These are consistent game layouts, not verified replicas
of every park's individual entrance monument. USDA/NPS asset notices are packaged
beside the textures. Exact mapped board locations can be added as verified
supplements; automatically inferred entrances have approximate roadside stations.

Protected-area snapshots and exact OSM exit tags are cached by week, with stale
offline reuse and bounded retry after failure. All work occurs during tile/source
generation, never per frame. The first visit can require network range reads or
small ID requests; missing sources do not block ordinary road generation.

Vedauwoo utility correction: `roadutilities.py` suppresses inferred roadside poles
and wires on OSM ways 15674422 and 1524224932, following the user's confirmation
that they are absent. This is source-way-specific, not a blanket forest/rural-road
rule. The correction participates in the normal code/cache fingerprint; no terrain
or road-plan invalidation is needed because only props and wire meshes change.
A replay of three entrance-road tiles changed from 14 poles/42 wire spans to none.

## Review and development

Run the focused tests with the project's supported DuckDB below version 2:
python -m unittest discover -s server/tests -p test_travelboards.py

Generate a preview of the actual meshes and texture UVs:
python tools/preview_travelboards.py

The preview is an offline mesh render, not an in-game screenshot. It shows all
enabled welcome designs, an explicitly fictional recreation entrance, the verified
Exit 243 supplemental guide and the Vedauwoo layout.
Application packages already collect the new server modules, registry and artwork.

Focused validation includes route-facing placement, exact mapped welcomes, all
state-name recognition, park destination classification, separate exit panels,
left exits, agency selection, connected entrance roads, source caching and offline
reuse. Run test_travelboards.py, test_recreation_expansion.py and test_osmwelcomes.py.
Use tools/audit_welcome_signs.py for the explicit enabled/pending state inventory.
The server is not restarted by these changes.

## Missing recreation signs: streaming regression fix

The nationwide source originally sampled every full road against every full
forest polygon before returning even the bundled Vedauwoo sign. It recalculated
road distances for every sample, scanned boundaries for roads wholly inside
forests, and inherited the much larger exit-junction search halo. The single
sign worker could stay busy through a teleport; its old-anchor result was then
discarded. Candidate-only coordinate probes did not exercise this failure.

Boundary sampling now reuses one cumulative-distance array. Prepared whole-ring
indexes preserve holes and disjoint parts, reject interior roads using actual
boundary edges, and use the existing native point-in-ring test. Park/welcome
records use the destination tile plus a 100 m placement margin; ordinary exit
junctions and route reassurance markers retain their existing search halo.

Full signstream replays exported actual CDAE meshes and nonempty manifests for
Vedauwoo, Medicine Bow–Routt at Snowy Range, Glendo State Park and Mahoney Exit 426.
These use real cached road/building data and DEM elevations, rather than the flat
terrain used by earlier coordinate probes. The DEM replay does not reproduce the
live grader's exact terrain, and it is not an in-game visual confirmation.
Reports are in probes/sign-stream-replay. Installed forest materials and emblem
assets matched the workspace copies. No running server restart or hotpatch was
performed; a server already running older Python modules needs the fixed code
loaded on its next normal start.
