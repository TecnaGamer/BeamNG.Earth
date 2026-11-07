# Regional debug layers

Open the map, choose **Layers > Debug Overlays > Regional placement**, then select
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
into continuous contours with shared grid edges removed and holes preserved. Generated coverage reads at most 96 loaded tiles / 16 MiB of manifests / 6,000 features;
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

To inspect them, open **Layers > Debug Overlays > Regional placement > Natural
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
`features` (type and ring or rings in longitude-latitude, optional label; outline only for explicit envelopes) and `note`. The map
loads provider choices from the server, so subsequent providers need no duplicate
UI catalogue. Validate bounds, enforce budgets, label approximate data, and use
no network or generation side effects. New sampled providers should reuse the
shared run compression and fixed grid budget.

Agent communication and validation requirements are in the root `AGENTS.md`.

## Non-layered arid fractured rock (2026-11-07)

**Arid fractured rock** uses the local Köppen dry classes (4-7) as an appearance
prior, below **Layered canyon** in priority and above **Default rock**. It is not
a geological boundary or a claim that every dry area has volcanic rock. Existing
slope, land-cover and user settings gate generation. Near and far faces and toe
rubble share the production selector; road cuts keep their separate context.

On the map choose **Layers > Debug Overlays > Regional placement > Natural cliff
regions** and inspect Hoover Dam (36.02, -114.74). The arid coverage is a bounded
sample of the climate raster; layered canyon envelopes are exact registry rings.
Use **Generated cliffs & road cuts** to inspect recorded actual generation.
The server supplies the legend and coverage; no independent UI region rules exist.

Validation: selector/precedence, climate vector sampling, far classification,
provider bounds/empty/error/disabled states and talus placement tests pass. Live
Hoover Dam near/far materials load without missing textures, and the map displays
the new legend and sampled overlay. Geological accuracy outside the tested area
and full driving performance have not been established by these visual checks.


## Seam-free coverage contours (2026-11-07)

Sampled regions and recorded detector rectangles are merged by material before
being sent to the map. Only their outer contours and holes remain; shared row
and tile edges are not drawn. The canvas fills all rings of each material once
with the even-odd rule, preserving gaps without darker overlaps. Explicit
appearance envelopes alone have outlines. This does not smooth, expand or
reclassify the production coverage, and does not load render meshes. The existing
query, tile, file and detector-rectangle limits remain in effect.

The menu section is **Debug Overlays**. Regional placement's legend shows its
colour key and loading/error status without the long provider description. The
provider response retains its coverage note for tools and diagnostics.

Validated with 18 provider/contour tests, the JavaScript canvas checks and the live
map. A uniform 40 x 24 sample becomes one four-corner contour instead of 24
outlined rows. Random masks retain their exact cell membership; tests include
holes, corner-only contacts, overlapping rectangles, material separation and the
date line. A 6,000-rectangle local check took about 6 ms for solid coverage and
51 ms for isolated patches; this is request-time server work, not game-frame work.


### Confirmed road cuts

The **Confirmed road cuts** overlay reads `debugCliffCuts` from already delivered
road tiles. It records only the production road-cut detector, independently of
natural cliffs that share the same arid material. Outlines merge detector cells;
they are approximate coverage before toe fitting and pavement clipping. Older
tiles without this field show missing coverage until regenerated. No map query
starts terrain or mesh generation. Test near 35.98080, -114.70057 for diagonal
road cuts; face length is measured along its principal direction as well as
the map axes. Initial gentle shoulders are excluded from the face-slope average,
while full approach, rise, crest, land-cover and clearance checks still apply.

Rural service/frontage roads use the same cut-profile checks as other rural
roads. At the Nevada example above, the upper service road must supply the
left-hand bank profile instead of the highway farther below it. Urban service
roads remain excluded. The Confirmed road cuts overlay records both sides.

At 35.96077, -114.66431, lower sustained highway banks also qualify: the road-cut
minimum is a 4 m rise, 192 square metres of face, and 96 square metres of
profile-confirmed footprint. Six steep core cells and a 24 m bank span are still required,
along with the profile, natural crest, city/land-cover and road clearance tests.
These limits apply to road cuts, not natural-cliff selection.

The same highway near 35.96077, -114.66431 also exercises partial road labels
at a tapered bank. A 24 m road-label margin accounts for two coarse land-cover
pixels and a detector cell. Independently validated terminal profiles may
contribute the bank length and footprint; they cannot replace its six directly
confirmed steep core cells. The map overlay records the resulting footprint
from the production detector, with no extra generation work in map queries.
