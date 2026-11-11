# Mapped paths

Unknown ground-footway surfaces use an unsealed trail fallback. A footway classification alone, especially
in the UK, does not establish paving or justify earthworks. Explicit paving or compacted aggregate selects
the bounded built-path profile; explicit rock/grass/dirt trails retain their terrain. Mapped bridge deck fallbacks are unchanged.
The resulting roughness on an unspecified trail can reflect the underlying elevation data; it is not a surveyed
trail profile. Non-grassy paths clear grass roots within the existing narrow road-cell guard, without widening
the painted path or applying vehicle-verge clearance. Explicit grass paths keep their grass.
Grass clearance is registered at terrain vertices (the renderer's nearest samples); physical surface
painting retains its south-west-square convention. Sharing that half-cell offset displaced narrow grass gaps.

Cycleways, shared foot/cycle paths, footways, bridleways and linear pedestrian streets use Earth's
existing surface materials, terrain physics and streaming. They create no vehicle traffic, parking,
sidewalks, markings, building cuts, roadside decoration or procedural vehicle wear.

## Data, widths and surfaces

Overture geometry remains primary. Exact OSM way IDs supply classification, widths, surfaces and
structural tags through the existing Layercake reader. Only missing linear ways need supplementary
geometry; scoped source-way IDs prevent duplicate bridges. Nearby parallel ways stay separate.

| Class | Fallback width | Fallback surface |
| --- | ---: | --- |
| footway | 1.8 m | unsealed |
| path | 1.2 m | dirt |
| cycleway | 2.5 m | paved |
| bridleway | 2.0 m | dirt |
| pedestrian | 4.0 m | paved |

These are estimates. Valid mapped widths greater than zero and at most 40 m take precedence,
including widths below one metre. Explicit OSM surfaces take precedence over Overture surfaces.
Asphalt, concrete, paving, gravel/compacted, dirt, grass and rock reuse stock ground models.
Rock and grass keep the ground photo; a material tag does not create rock geometry.
Explicit `flagstone`, `flagstones` and `tiles` use the existing paving material, following #653.

## Ground profiles and vegetation

Only explicitly mapped paving or `surface=compacted` triggers a narrow hillside bench and the existing
endpoint-preserving Gaussian filter (6 m nominal sigma). Compacted paths retain their gravel material
and physics. Loose `gravel` and `fine_gravel`, dirt, grass, rock and untagged profiles retain their natural
terrain. Fallback paving affects materials only. Ground paths use the
existing physics heightfield, whose one-metre grid limits narrow-path detail.

The bench extends 1 m beyond the mapped edge to support the terrain triangles, then fades over
0.5 m. Correction is capped at 2.5 m; gentle crossfall up to 2.5% remains. Crossfall uses the same
filter before limiting its magnitude, avoiding alternating banks from noisy side probes while
preserving the broader drainage slope. Climbing grades remain, with no added vehicle-track wear.
These are generation limits, not surveyed dimensions. Vehicle carriageways, sidewalks and kept raceway surfaces retain height priority. Path joins
require a shared mapped vertex and act within 12 m; proximity alone never joins parallel paths.
Profiles are clipped before elevation sampling, aligned across cells and cached in a bounded
neighbourhood. No broad vehicle shoulders or retaining-wall extensions are added.

Path widths also control obstruction clearance. Bushes and tree origins on trails are removed;
canopies may overhang. Footways and pedestrian streets retain intentional street trees. Rocks
remain on rocky trails. Grass exclusion follows the painted terrain material.

## Bridges and limits

Mapped hard-surfaced path bridges reuse existing profiles, slabs, parapets, piers, collision and
streaming. Upstream walkway-span handling supplies underpass context. Mapped barriers can use
path-deck heights. Path decks create no vehicle bridge plans or vehicle markings.

Decks use existing road/rail crossing clearances. `layer` means relative order, not metres.
Approaches require a matching bridge endpoint, follow its tangent and taper crossfall to the deck.
Correction above 6 m is rejected. Nominal ramp reach uses an 8% adjustment gradient; short mapped
approaches return to the terrain before protected road footprints and may be steeper. Bridge
heights and engineering remain inferred. With grading disabled, decks render but approaches keep
the ungraded terrain. No surveyed grade or accessibility guarantee is implied.

Tunnels, unsupported off-ground surfaces, pedestrian-area polygons, stair treads and wooden/metal
boardwalks are excluded. Structural tags prevent painting those paths on the ground beneath them.
Access permissions and hiking ratings do not create paving, barriers, stairs or lane divisions.
Shared paths remain one corridor; vehicle GPS and AI routing are unchanged.

The tag reader does not provide `segregated`, `horse`, `designation`, `sac_scale`, `incline` or
`step_count`. Future consumers need a versioned data extension and tests. No runtime editing-API
or Overpass requests are added. Existing OSM contributor/ODbL attribution is retained.

Affected cache identities advance automatically; users do not need to clear their download cache.
The weekly tag-shard builder retains path geometry and structural tags. Archives with an older
schema fall back to the existing Layercake reader; geometry requests contain only missing way IDs.

This covers the mapped-path portion of Maverick568's #653 through a single path pipeline. Its
driveway work remains separate. Unlike its proposed defaults, untagged footways do not imply cobbles,
and paths do not inherit vehicle junctions or inferred steps. Mapped widths and surfaces still win.

Current upstream footbridge fallback (#958) remains available for unhandled spans. Accepted path deck pieces
are passed through both tile producers and excluded from fallback generation only when the same source ID
and complete geometry match. Other pieces, parallel paths and unsupported profiles keep upstream behavior.
