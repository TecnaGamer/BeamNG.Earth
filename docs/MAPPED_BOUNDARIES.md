# Mapped boundaries

Hedge inner coverage uses the stock leaf opacity map as well as its colour/normal maps. An opaque inner
box can become conspicuous as the leaf cards thin with distance. Stock models use their authored LODs for the loaded road tile
lifetime; there is no additional 150 m stock-to-mesh switch. Missing assets, reset and payload replacement still
restore the continuous backstop. Authored LODs can retain their own detail and shading differences.

With buildings enabled, near road tiles draw mapped, ground-level fences, ordinary walls and linear hedges from
the existing Overture infrastructure layer. Closed linear ways remain boundaries; polygon hedge areas do not
become perimeter fences. Guardrails, retaining walls and elevated/underground boundaries are separate systems.

`server/barriers.py` selects pieces before elevation work, deduplicates cells, resolves nearby gate/entrance
points and lines, and cuts openings before generating geometry. Source tags take precedence over normalized
classes. A gate must match a boundary within 1 m; incompatible/equally plausible boundaries are left unchanged.
Collinear pieces meeting at the same gate may both be cut. Gate diagnostics are available at debug log level.
OSM shared-node topology is not assumed to survive ingestion.

Gates are **passable openings**, with posts where applicable. There are no animated leaves or closed gates in
this version. `access` and `locked` do not create a physical blockage. Clear width uses `maxwidth:physical`, then
`width`, then the mapped line span, otherwise an estimated 3.5 m. Legal `maxwidth` is not physical width.
Unmatched standalone gates are omitted rather than given an invented bearing or boundary.

Valid physical dimensions accept metres, centimetres and explicit feet/inches. Missing/invalid dimensions use:

| Boundary | Height | Thickness/width |
|---|---:|---:|
| Fence | 1.3 m | 0.12 m nominal; construction-specific rails/posts |
| Wall | 1.5 m | 0.3 m |
| Hedge | 1.8 m | 0.8 m |

Timber fences use open rails; recorded railings use vertical metal bars. Pickets, panels, plain wire,
barbed wire and electric fences use simple construction-specific geometry. Existing chain-link and noise-wall
assets retain their stock renderer when no valid physical width requires a dimensioned mesh; electric and barbed combinations are visual details, without shock physics.
Walls use existing brick, stone or concrete materials. Hedges reuse installed cypress foliage and materials;
stock fitting and continuous fallback coverage are described below. Missing fence construction uses timber rails.
These are documented visual fallbacks, not measured local dimensions or exact replicas of every fence type.

Unspecified wall material uses a neutral, rough stock plaster finish. Explicit supported materials take precedence
over wall-type hints. Boundary-specific stone, brick and wood definitions reuse existing textures without the
building materials' vertex-colour requirement. Physical height/width estimates are unchanged.

Solid parts reuse `roadmesh.barrier_solid`: hard edges and planar end UVs, including fence posts/rails and hedge
cores. Continuous wall spans retain source-distance texture phase and omit internal sampling caps. True bends
share bounded source mitres while retaining hard face normals; gate/road cuts and tile/mesh limits split spans.
This changes wall joints, not the global CDAE normal builder or existing stock noise-wall/chain-link assets.

Meshes follow graded ground, retain corners and short runs, and are sampled from each source piece's start.
Distances use Earth-fixed ECEF coordinates so rebasing cannot change the post spacing or its phase.
Tile cuts do not reset the sampling phase. Collision uses the visible fence/wall geometry; hedges do not collide.
There is no breakability. Procedural yard panels overlapping mapped boundaries are suppressed, including their
gate gaps. The current limit is four 60,000-vertex meshes each for solid boundaries and hedges per tile; an
exception reports an over-limit tile rather than silently cutting holes. Raw source caches are unchanged;
the existing source-hashed revisit cache invalidates generated outputs automatically.

Conflicting spans inside the generated ground carriageways are conservatively omitted (debug diagnostics),
including their posts and corner fills. Verge boundaries remain. Only the existing ground-road context is used;
an elevated crossing does not erase a ground boundary. This avoids introducing a closed barrier on an existing
traffic route where boundary/road data disagree; it is not a simulation of mapped road closures.

`boundary[2..4]` and `hedge[2..4]` are optional road-tile mesh fields. They use the existing shape-slot pooling,
collision scheduling, rebase, drop, cleanup and settings lifecycle. Both server build paths call the same
generator. No new message family, dependency or data source is needed; no BeamNG assets are redistributed.

Offline regressions: `cd server && python -m unittest discover -s tests -t tests -p test_barriers.py`;
`luajit mod/tests/test_barriers.lua`. Also run the existing buildings/furniture/roads/block-worker/protocol suites
and the documented complete Python/Lua suites. Game validation must check physical gate clearance, fence/wall
contact, slopes, seams, high-speed arrival and teleport/rebase cleanup. Compare performance with unchanged dev
using identical fixtures/settings and disclose unsupported or ambiguous cases.

Integration with current dev: mapped meshes use separate `boundary[2..4]` fields, preserving highway
median barriers. Existing stock chain-link, wire-mesh and noise-wall rendering remains in charge
of those lines. Panels intersecting a reliably mapped gate are omitted whole, so their opening
can be wider than the mapped width; stock models are not distorted to fit. Valid mapped heights (source tags before normalized height) uniformly
scale their panel length, origin offsets and ground lift together. Valid mapped widths on ground-level stock
lines use the existing continuous wall/mesh-fence generator and suppress the corresponding stock panels.
Chain-link mesh reuses the stock chain-link maps without a vertex-colour requirement. Bridge/tunnel width
replacement remains outside the ground-boundary renderer; those panels keep their structural renderer. Explicit bridge/tunnel
levels keep their existing renderer. A path crossing alone never invents a gate. Plain wire, timber,
pickets, panels, railings, electric and barbed construction use the existing material palette.
Electric details are visual; gates do not animate, and boundaries are not breakable.
When the French regional kit is enabled, its mapped boundaries retain source ownership and
their regional palette. That result is computed once and excluded from the generic mesh pass.
Road-fitted noise panels retain mapped scale and gate exclusions after their placement is adjusted.

Vehicle clearance uses the existing `roads.STYLE` classification. Untyped legacy footprints keep
their clearance behavior; foot/cycle/bridle paths and steps do not invent gaps. This renderer has
no dependency on the separate mapped-path contribution.

Stock hedge refinement reuses Italy's authored cypress shape when its uniform scale fits both
mapped width and height, terrain variation is small and the complete span fits between gates and
bends. Other spans use clipped stock-leaf cards with an inset core, not opaque outer cuboids.
Dense tiles use bounded cutout sheets. Stock items stay non-colliding and retain authored wind.
Whole Forests remain hidden until every item and its replaceable backstop mesh are ready; the
backstop returns on missing assets, settings changes and replacement. Both ordinary
and geotile rows reuse the current placement and cleanup paths. No stock artwork is redistributed.
