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

### Additional arid cliff sectors (November 2026)

All six additions reuse the existing **Layered canyon** family, its PBR maps,
local satellite colour matching, and the shared near/far/talus selector. No
regional texture variants or new runtime data downloads are introduced.

| Debug region | Why the shared material is a useful approximation | Preview latitude, longitude |
| --- | --- | --- |
| Santa Elena Canyon | Desert limestone canyon; limited to the river-cut escarpment, excluding the volcanic Chisos Mountains. | 29.18, -103.63 |
| Boquillas Canyon | Stacked Del Carmen limestone, Sue Peaks shale slope and Santa Elena limestone; a separate Rio Grande corridor. | 29.22, -102.89 |
| Kennedy Range southern escarpment | Desert sandstone/shale strata forming cliffs and gorges. Starts with the southern Temple Gorge sector rather than the whole range. | -24.66, 115.17 |
| Watarrka - Kings Canyon | Two sandstone formations separated by mudstone, exposed in desert canyon walls. Restricted to the western George Gill Range near Kings Canyon. | -24.25, 131.58 |
| Talampaya canyon sector | Arid sedimentary basin with red sandstone walls. Restricted to the principal canyon sector, not the whole Talampaya/Ischigualasto World Heritage property. | -29.79, -67.99 |
| Tuwaiq - Edge of the World | Desert sedimentary limestone escarpment. Covers the central Edge of the World sector, not the entire 800 km range. Local imagery tint is especially important for its pale rock. | 24.945, 45.994 |

Primary sources supporting these appearance choices:

- **Big Bend:** [NPS geology overview](https://www.nps.gov/bibe/learn/nature/rocks.htm)
  distinguishes limestone canyons from the park's volcanic rocks;
  [USGS Boquillas Canyon](https://www.usgs.gov/media/images/a-photo-boquillas-canyon)
  identifies the stacked cliff/slope formations.
- **Kennedy Range:** [WA Parks visitor guide](https://exploreparks.dbca.wa.gov.au/sites/default/files/2021-10/kennedy-range-national-park-mundatharrda-visitor-guide.pdf)
  describes the desert climate, sandstone/shale layers and southern/eastern cliffs.
- **Watarrka:** [NT Parks geology sheet](https://nt.gov.au/_media/docs/parks/find-a-park-to-visit/central-australia-region/watarrka-geology.pdf)
  describes the Mereenie/Carmichael sandstone and intervening mudstone;
  [Australian heritage listing](https://www.dcceew.gov.au/parks-heritage/heritage/places/national/watarrka-national-park)
  documents the arid setting. The texture is an approximation of the exposed
  beds, not a reproduction of the park's cross-bedded domes.
- **Talampaya:** [UNESCO property description](https://whc.unesco.org/en/list/966)
  describes the desert sedimentary basin;
  [Argentina National Parks](https://www.argentina.gob.ar/parquesnacionales/centro/parque-nacional-talampaya/ficha-del-area-protegida)
  documents the desert canyon and red walls.
- **Tuwaiq:** [Saudipedia escarpment description](https://saudipedia.com/en/tuwaiq-mountain)
  documents its Jurassic limestone and western cliff face;
  [USGS geology of the Arabian Peninsula](https://pubs.usgs.gov/pp/0560d/report.pdf)
  describes the sedimentary succession;
  [Edge of the World](https://saudipedia.com/en/edge-of-the-world)
  locates this desert cliff sector west of Riyadh.

The polygon vertices are hand-authored appearance envelopes, inferred from these
landmarks and their surroundings; they are not digitized geological contacts or
park boundaries. Preview points locate sectors, not guaranteed spawn positions.
Existing slope, cover, road clearance and enabled-cliff settings still decide
which faces generate. Both sides of the Rio Grande use the same appearance rule.

To inspect them, open **Layers > Debug layers > Regional placement > Natural
cliff regions** on the map and pan to the preview coordinates. **Generated cliffs
& road cuts** shows recorded coverage after near tiles have actually loaded.
Changing this registry participates in the revisit cache's source fingerprint;
restart the companion server to load the new rules and invalidate old results.

These are deliberately approximate visual envelopes inferred from documented
exposures, not authoritative geologic boundaries. Do not assign this texture to
all canyons or all arid places. Volcanic Bandelier tuff and Karijini banded iron
formation are excluded from this expansion. Wadi Rum's massive domes and the
soft clay/gypsum badlands of Bardenas Reales were also deferred: aridity alone
is insufficient. Similar bedded sandstone and limestone can share this material
without requiring a texture for every locality. The shared texture is a visual
approximation and does not reproduce every formation.

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
