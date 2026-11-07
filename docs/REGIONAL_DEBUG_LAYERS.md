# Regional debug layers

Open the map, choose **Layers > Debug layers > Regional placement**, then select
an overlay. These overlays inspect rules/data without generating world assets.

| Layer | Meaning |
| --- | --- |
| Natural cliff regions | Exact configured canyon appearance envelopes; default rock elsewhere. Applies to near and far selection. |
| Generated cliffs & road cuts | Coarse detector footprints from already loaded near tiles, coloured by material. Toe fitting and mesh edges can differ; no horizon geometry is implied. |
| Road-cut material regions | Sampled arid/limestone choice for confirmed cuts, not predicted road excavation. |
| Building style regions | Existing production architectural defaults. Mapped attributes can override them. |
| Regional tree/shrub/scatter-rock variants | Production regional selector with representative cover and a fixed seed; not density or exact spawn positions. |

The map requests data only while enabled, coalesces pans, refreshes every five
seconds and paints a canvas. Sampled providers use a 40 x 24 grid compressed
into runs. Generated coverage reads at most 96 loaded tiles / 16 MiB of manifests / 6,000 features;
zoom in if limited. Older tiles without metadata are explicitly identified.
Cliff debug metadata is computed from the existing coarse detector, never from
the expensive refined render mesh. Disabled faces are labelled as disabled.

## Cliff regions and research

`server/clifftypes.py` is the source of truth for priority, context, family and
appearance envelopes. Higher priority wins; stable key resolves ties. Canyon
(priority 100) overrides default rock (0). Confirmed road cuts select
arid rock (100) in biome 1, limestone (0) otherwise. Climate describes appearance,
not measured geology. Slope, land-cover and clearance remain eligibility gates
in their near/far generators. Near tiles select each cell rather than their
centre; far triangles and talus use the same natural selector.

The existing Colorado Plateau envelope already includes Grand Canyon, Zion,
Canyonlands and other plateau exposures. Utah Geological Survey describes its
nearly flat sedimentary layers and mesa/canyon landforms:
https://geology.utah.gov/map-pub/survey-notes/glad-you-asked/how-was-utahs-topography-formed/

Added conservative appearance envelopes:

- **Palo Duro Canyon, Texas:** TPWD describes four visible geologic layers and
  differential erosion. https://tpwd.texas.gov/state-parks/palo-duro-canyon/nature
- **Caprock Canyons, Texas:** TPWD describes red beds of shale, sandstone,
  siltstone and mudstone with different red, orange and white strata.
  https://tpwd.texas.gov/state-parks/caprock-canyons/nature
- **Kalbarri inland gorges, Western Australia:** the Geological Survey of WA
  describes sedimentary Tumblagooda Sandstone and successive sand layers.
  https://www.wa.gov.au/organisation/department-of-mines-petroleum-and-exploration/geological-survey-of-western-australia/trace-fossils-of-the-tumblagooda-sandstone

These are deliberately approximate visual envelopes inferred from documented
exposures, not authoritative geologic boundaries. Do not assign this texture to
all canyons or all arid places. Volcanic Bandelier tuff and Karijini banded iron
formation need different material families. Massive/cross-bedded sandstone and
tilted formations within the broad Plateau remain candidates for future,
higher-priority variants. No new texture is claimed to reproduce every formation.

## Extending the system

For another cliff appearance, register a `CliffType` with a stable key, family,
context, priority, legend colour and optional regions/biomes. A new rendering
family also needs material/height/near relief and far-slot support; a new region
using an existing family only changes the registry. Never renumber existing
material IDs. Keep near/far/talus selection shared and update regression tests.

Other regional features add a provider to `debuglayers.LAYERS` and `query` using
their production selector. Responses contain `layers`, `legend` (id/label/color),
`features` (type/ring in longitude-latitude, optional label) and `note`. The map
loads provider choices from the server, so subsequent providers need no duplicate
UI catalogue. Validate bounds, enforce budgets, label approximate data, and use
no network or generation side effects. New sampled providers should reuse the
shared run compression and fixed grid budget.

Agent communication and validation requirements are in the root `AGENTS.md`.
