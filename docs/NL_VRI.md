# Opt-in Dutch Groenpol vehicle signals

Enable **Dutch Groenpol signals** in the Earth settings and restart. The setting
defaults to false. BGT is outside this integration. No installed game files or
user launchers are changed by this PR.
The stock path does not load Dutch planning, a country outline, a component
catalogue or a surface reader. The server plans optional assemblies in Rust;
the mod only renders received parts using shared Forest items.

Models and textures derive from Sven's confirmed `Groenpol.fbx`, SHA256
`0c87eef5eea270f91807cabdaf9f37df3ebec64d115e6d99e6ecc28100effe77`.
The component/source-object/triangle provenance is retained in `docs/assets/groenpol`,
outside the runtime mod. The runtime catalogue is server/data/groenpol_components.json.
Model/texture author and licensor: Sven / svenegaming-glitch, **CC BY-SA 4.0**;
see [the asset licence notice](GROENPOL_ASSET_LICENSE.md). Derived assets retain
this licence independently of the project code and map-data licences.
These are the contributor's supplied signal assets, not the rejected storefront
sign faces. Signal heads reuse the engine's existing light controllers.

The existing lane-aware construction includes separate posts, independently
aimed heads, original mounting feet, shared posts with separate controllers,
200/300 mm lenses and turn arrows. Through lanes keep full round lenses.
Mixed urban/rural junctions and motorway exits use 300 mm. Dutch signals go from
red directly to green. Cantilever arms carry at most three overhead lane heads;
wide approaches may use portals, mirrored cantilevers or a 3+1 arrangement.
Placement checks use the actual drawn road/walk meshes and terrain; unsafe
positions trigger shared supports or upstream searches before rejection. Missing
surfaces retry. A completely accepted owned assembly suppresses its old prop
locally, rather than deleting Dutch props country-wide.

WKD built-up sections and OSM lane/signal context come from public per-tile
server data; the old 46 MiB client pack and its Lua planner are removed.
The companion BeamNG.Earth-data workflow rebuilds deterministic attributed
shards from WKD/Overpass sources. Publication requires the data maintainer.
Without published data, existing stock signals remain available.
See [NL_SIGNAL_CONTEXT_DATA.md](NL_SIGNAL_CONTEXT_DATA.md) and
[the live native pipeline](NL_SIGNAL_SERVER_PLACEMENT.md). BGT adapters,
polygons and local pole overrides remain outside this integration.

Stop positions are inferred, not surveyed stop lines. Lane arrows follow the
existing approach controller; this does not synthesize independent protected
turn phases. New assemblies wait for the matching visible road and terrain
generation before replacing stock heads.

The client integration preserves current dev's independent scenery hiders and
native sign/road queues. The low-level tests exercise ownership, reloads,
rebasing, lane/head counts, mounting, headings, shield clearance and road feet.
These are offline/stubbed tests, not proof of visual correctness at every
junction. In-game placement review and performance measurement remain needed.
Runtime textures use DDS, avoiding the engine's PNG cooking path on current dev.
