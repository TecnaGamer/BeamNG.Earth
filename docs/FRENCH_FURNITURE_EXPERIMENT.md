# French furniture and variable-text signs — current v4 contribution

Pinned base: BeamNG.Earth 1.7.1-beta, commit
`8aaf4ebdd0485facadc755a612f4b0a2b55e4c57`. Opt-in experiment. The French features originated in v2 and are retained in v4.

## Enable

```sh
python start.py -- --regional-poles europe
```

`start.py` synchronizes the source mod into the configured BeamNG user directory.
Keep the terminal open; start BeamNG.drive normally and choose BeamNG.Earth in
Freeroam. The official downloaded BeamNG.Earth executable lacks this code.
`fr-all` remains the concrete-only pole comparison; `off` preserves stock behaviour.

All kit objects are country-gated at their actual position. The worldwide
country service is described in COUNTRY_BOUNDS_EXPERIMENT.md. The French kit remains
FR-only. The `europe` mode adds national representative pole/marker/chevron
profiles, town signs and a dark asphalt-footway/dark-grey-kerb palette in the
56-code registry. See EUROPE_INFRASTRUCTURE.md for current coverage. Russia and
Turkey are included over their full source geometry; Kazakhstan remains outside
the registry. This is not a complete national catalogue.
`fr-varied` still restricts all regional changes to FR; `off` remains stock.

## Models and selection

| Object | Visual LOD triangles | Collision triangles | Rule in this experiment |
|---|---:|---:|---|
| Slender wood pole / blue plate | 152 / 76 / 52 | 20 | Stable road-ID choice, ~75% wood, 25% previous concrete |
| Green lantern, 5.4 m | 290 / 242 / 158 | 20 | ~70% eligible village standalone lamp runs |
| J1 white reflective band | 240 / 144 / 80 | Disabled | Outer side of bends |
| J3 red band | 240 / 144 / 80 | Disabled | Rural intersections with at least three approaches |
| J6 delineator | 20 / 20 / 20 | Disabled | 8% of primary/secondary road features at least 250 m long, 90 m spacing |
| J4 left/right chevrons | 86 / 62 / 46 | 20 | Pronounced bends, oriented to the approaching traffic |
| Shared sign support | 12 / 12 / 12 | 12 | Town/direction panels |

Percentages are visual trial settings, not national equipment statistics. Wooden
poles can appear in towns as well; the mix does not yet identify a real operator,
network voltage or mapped support material. The blue ID plate is decorative with
no real network identifier. Original cable/underground placement is retained.
Wood and concrete share the bundle attachment, so their spans connect. Stock
three-wire to bundled-wire transitions still terminate rather than invent hardware.

Lantern selection uses the existing village/road classification and a stable hash,
not Wingen coordinates or an Alsace rectangle. It does not prove these lanterns
exist on each chosen street. Other modern lamps remain. Lamps attached to utility
poles keep their original arm/head. Lantern glazing is simplified and opaque in
this prototype; the separate bulb/lens and existing shared lighting pool provide
warm yellow light at night, with per-profile colour restored on pooled-light reuse.

Marker/chevron placement is inferred from road geometry, not an inventory of
actual signs. J3 is not repeated all along the road. Existing bridge-clearance
filtering also applies to the new markers. Retroreflective material factors are
set on reflector bands and sign faces; headlight appearance needs game validation.
J1/J3/J6 Forest collisions are disabled at all distances, including revisits.
Models are original procedural geometry. No photo pixels, game meshes or brands
were copied into the new assets. LODs and static collisions use the Forest lifecycle.

## Dynamic names and route numbers

Town entry panels use a white/red template; exits use a thin black frame and red
slash. Names come from named OSM city-limit nodes or a matching mapped settlement.
The shared multilingual atlas and material set serve all signs; the
original French-only atlas has been expanded for the European profiles. Panel width adapts to text
up to 4.8 m; longer labels shrink. Accents and Œ are supported; unsupported glyphs
use `?`. The bundled DejaVu Sans Bold is an approximate prototype typeface, not
France's official L1 road-sign alphabet.

Faces are batched into one render-only CDAE per road tile. Shared support models
carry static collisions. Both sides are readable where the source denotes both
entry and exit. Yellow D-road cartouches appear only for supported mapped `D`
references; unknown references are omitted. No priority diamonds, local badges,
flowers, speed rules or municipal partnerships are invented from the town name.

Mapped position takes precedence; an on-road node is moved to a clear shoulder.
Missing orientation can be inferred from a matching place centre. Otherwise the
sign is omitted. On an unmapped approach, a conservative fallback looks for a
sustained rural/built-up transition and a nearby named city/town/village. This is
an approximate visual entry, not a legal boundary or a town-centre label. The
fallback follows connected short road features for the +/-120 m context.
It excludes residential/living-street candidates to avoid garden gaps. Mapped
unnamed nodes can receive an unambiguous nearby settlement name; records keep
that provenance. A mapped sign blocked by an access road can move at most 16 m
along its road. Land-use gaps and ambiguous approaches still leave entries
missing. None is guaranteed for every settlement.

Direction arrows use explicit `destination_sign` relation labels and outgoing
geometry, restricted to white/black local signs and nearby drivable roads. Up to
four lines share a support and read on both faces. This is partial mapped coverage;
no destination graph, route distance or full reproduction of each real sign stack.

`regionalSigns` in each road JSON records name, position, route and mapped versus
estimated source. The streaming Lua pools/deletes/rebases the sign mesh with the
road tile; all faces share the atlas. Data hashes and the kit version enter the
revisit key so a previous mode cannot supply stale sign geometry.

## STOP policy and sidewalks

The previous revision's inferred-STOP suppression has been removed. Junction
road classifications, STOP placement and the corresponding AI controllers
now follow the pinned public 1.7.1 version exactly. Mapped signals and roundabout
priority also retain that public behaviour. This contribution changes scenery,
not traffic-priority policy; it does not promise that public inferred STOPs match
every real junction. `server/navgraph.py` is identical to upstream.

Sidewalk changes use three new materials and UV scale, not global texture
replacement. Footway mesh geometry, ramps and lean collision mesh are unchanged.
The default footway now uses rough, dark asphalt and a darker grey kerb,
retaining the pale tactile surface. Original procedural textures use `.color.png`
for sRGB base colour, with new paths and bumped profile keys to avoid stale data.
See https://documentation.beamng.com/modding/levels/level_creation/section6/ .
The references guide a default palette; no exact national material coverage is claimed.
Country selection is per clipped sidewalk run/corner midpoint; it can therefore
switch up to one run away from a border. Original brick materials are retained.

## Validation and useful game checks

For the current v4 package, 113 focused Python tests and 49 affected upstream
tests passed under Python 3.12 with official Linux native module VERSION 100.
Three modified Lua suites passed 213 assertions under LuaJIT. Native binaries
are not changed by this contribution. The contributor subsequently ran all 113
installer tests successfully on Windows. Complete Europe v4 in-game rendering
and performance validation remain outstanding; earlier French observations
are not validation of the whole new set.

Real Overture roads and WorldCover data generate mapped Lembach south/north,
estimated Wingen from Lembach and Lembach from Wingen, and mapped Climbach east
through roads.build_tile, on flat control ground. These checks are not surveying
or game rendering. The documented regression report separates a pre-existing
upstream accotement-paint test failure reproduced on a pristine 1.7.1 base.

User testing of v1 reported pale sidewalks and missing STOPs. V2 addresses those
reports; its final lighting/material appearance still requires a new game check. Inspect the five routes, tree occlusion, front/back text, grounding,
night lights, headlight reflection, LODs and reload/rebase behaviour in game.

The cross-platform COLLADA test fix is retained: XML/bindings are exact; finite
float arrays use rtol=1e-4 and atol=1e-12, matching five-significant-digit export.

## Developer rebuild and contribution

```sh
python tools/build_french_furniture.py
python tools/build_european_surfaces.py
python tools/build_sign_font.py
python tools/fetch_fr_sign_data.py towns --output RAW_TOWNS
python tools/build_fr_sign_data.py RAW_TOWNS server/data/fr_town_signs.json.gz
python tools/fetch_fr_sign_data.py directions --output RAW_DIRECTIONS
python tools/build_fr_direction_data.py RAW_DIRECTIONS/directions.json.gz server/data/fr_direction_signs.json.gz
```

Asset builders use existing NumPy/Pillow; the font source is included. Data fetch
is developer-only and serial/resumable. Read the NOTICES and retain source pins.
Rebuild data deliberately and update hashes after review. A different FreeType or
PNG compressor may produce different encoded images; compare decoded content.

General country lookup and French assets can be proposed as separate changes,
with common APIs for later country profiles. Acceptance is the maintainer's
decision; a France-first optional kit is not inherently a Wingen-only special case.
Port to current upstream, agree on data distribution and rendering conventions,
then repeat the relevant tests after any integration changes.

Code and original meshes/textures: repository MIT. Geographic/sign databases:
ODbL-1.0. DejaVu font and derived atlas: retained font licence, not MIT. See CREDITS.
