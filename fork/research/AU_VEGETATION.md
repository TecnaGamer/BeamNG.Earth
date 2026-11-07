# AU vegetation: base mod only (2026-11-07)

Goal: make the Australian bush look right using **only BeamNG's own (vanilla) assets plus a small data file**, so the mod doesn't depend on any other mod. The survey of vanilla assets is in `fork/reference/vanilla_veg/`: five picture sheets (`sheet_01..05.jpg`) and `inventory.csv`, covering 683 shapes and 259 groundcover entries across 17 levels.

## What the bush is made of (Rockhampton region example)
- **Lowland roads** (e.g. Yeppoon–Rockhampton): open eucalypt woodland, i.e. ironbark, spotted gum, bloodwood and poplar box, plus paperbark (melaleuca) on wet flats. The ground layer is long tussock grass, brown in the dry season (roughly May–Nov) and green in the wet season (roughly Dec–Mar). The canopy is sparse with wide gaps.
- **Hills and wetter gullies** (Berserker Ra., Mt Archer, Byfield): dry rainforest and vine thicket, which is dense, low, dark green and closed, with scrubby edges (lantana). In wetter areas there is tall open eucalypt forest.
- **Inland**: brigalow/acacia, mallee, and spinifex or tussock grassland on red soil.

## Data: NVIS Major Vegetation Groups (CC BY 4.0, data.gov.au)
- A national 100 m grid with about 30 vegetation classes, covering present-day vegetation. Use v5.1, which is clearly CC BY 4.0. The licence field on v7.0 is inconsistent, so confirm it before use.
- **Plan:**
  - Build a cut-down copy with `tools/build_au_vegetation.py`: downsample to a 500 m–1 km grid, store one byte per cell, gzip it, and save as `server/data/au_mvg.*`.
  - Measure the file size. Aim for 1–3 MB; if it's larger, drop to 1 km.
  - The server looks up the class at each tree's position.
- **Division of work:** density and height still come from the canopy data (Meta/WRI) and WorldCover, as they do today. NVIS only chooses the **species mix and ground cover**, so other regions are untouched.
- Credit line: "Vegetation: NVIS Major Vegetation Groups, DCCEEW (CC BY 4.0)".

## Mapping NVIS classes to vanilla assets
Paths are shortened as follows: WCA = /levels/west_coast_usa/art/shapes, JRI = jungle_rock_island, IT = italy. Check the class numbers against the legend that comes with the data.

| NVIS group | Trees (vanilla) | Ground cover |
|---|---|---|
| 5 Eucalypt woodlands, 11 open woodlands, 12 tropical euc. woodland/grassland | WCA trees_eucalyptus: eucalyptus_double, _tall, **_group** (unused). Sparse, with scale variation of 0.7–1.3 | **Long dry grass**: m_jv_dry_grass (JV weed_02..06), m_utah_dry_grass (long_dry_grass), m_grass_dry_short. Swap to a green mix in the wet season |
| 3 Eucalypt open forest, 2 tall open forest | the same eucalypts, denser and taller | grass_dry_1 plus green long grass, with ferns in group 2 (seeded_grass) |
| 9 Melaleuca (paperbark) | eucalypts with the pale m_eucalyptus_bark (the closest match to paperbark), clumped | m_wet_plant, green grass |
| 1 Rainforest and vine thicket (dry rainforest) | JRI trees_tropical_2: tro_tree_2_tall, _medium, filler_02..04 (unused); IT holm_oak_bush_huge, tall_plant_bush, cork_oak_bush_large; already in use: tro_bush_huge, tro_tree_1_huge | dense, with no grass, only shrub filler |
| 6 Acacia forest (brigalow), 14 mallee | IT trees_italy: scraggly, scraggly_2, scraggly_tree_filler_1, cork_oak_medium | dry grass, sparse |
| 16/17 shrublands, 18 heath | WCA trees_shrub: shrub_a..g (unused) | m_jv_dry_bush |
| 19 tussock grassland, 21 other grassland | none, or a few lone eucalypts | utah/jv dry grass, dense |
| 20 hummock grassland (spinifex) | none | m_jv_dry_bush / utah dry grass, on red soil (already in place) |
| 23 mangroves | tro_bush_huge, kept low and dense | none |
| 25 cleared / non-native | the current behaviour (paddocks, lawns, street trees) | current |

**Tropical extras** (groups 1, 12 and urban areas north of about 23°S): Italy fan palms (italy_palm_1, fanpalm_*), plus JRI palm_bend and tropical_wide_leaves (banana/monstera look) in urban yards.

## Wet and dry season
- Grass colour and the dry/green mix follow the month, using the existing `season.py` (wet season Dec–Mar).
- In Australia, dry should be the default: brown long grass for most of the year.

## Fitting it into upstream (generalised; not AU-only)
Upstream already expects this. `vegetation.regional()` describes its rules as "a coarse climate from the position ... (until a species map)". The Rust kernel `earthfast.regional_groups` uses hard-coded lat/lon boxes (`_dry_west`, `_north_america`, `_conifer_share`). The proposal **is** that species map, made general for the whole world:

1. **Global base layer: biome/ecoregion grid.** Source: RESOLVE Ecoregions 2017 (846 ecoregions, 14 biomes; check that it is CC BY 4.0). Rasterise it to roughly 0.05–0.1°, store a uint16 ecoregion id per cell, and save as `server/data/veg/ecoregions.*`. Aim for 2–6 MB. This replaces the hard-coded boxes worldwide.
2. **Optional finer national layers** on top. Australia uses NVIS (Major Vegetation Groups, about 500 m), the first one. Other countries could plug in the same way, e.g. US LANDFIRE EVT or EU CORINE. The finest layer that covers a point wins.
3. **One lookup:** `vegclass.at(lat, lon)` returns a palette id, and is read wherever the vegetation is placed.
4. **Palettes as data:** `server/data/veg/palettes.json` maps a class to:
   - its tree groups with shares (eucalypt 0.8, palm 0.05, …);
   - shrub and understorey groups;
   - a ground cover set (long dry grass, green lawn, spinifex, …);
   - density and scale multipliers;
   - the wet/dry season months.

   Each ecoregion or NVIS class points to one palette, so a palette can be reused, e.g. "tropical savanna" covers both NVIS 12 and the RESOLVE savanna ecoregions.
5. **Rust:** `regional_groups` takes the palette table as arrays and applies it, instead of hard-coded rules. The logic stays in Rust, the numbers live in data, as upstream's policy wants.
6. **Lua:** `groundcover.lua` and `trees.lua` already work from a catalogue. Each palette's ground cover set and new tree groups (eucalypt, dry rainforest, mallee, scrub) are just catalogue entries made from vanilla shapes.
7. **Cache keys:** hash the palette file and the layer files into `vegetation.VERSION`, so edits rebuild the cells.

**Result:** someone in another country only has to edit `palettes.json` (and optionally add a national layer), with no code changes. All of Australia is covered by NVIS, not just QLD.

## Order of work
1. **No data needed:** add the unused eucalyptus_group; vary the eucalypt scale; switch the AU ground cover to the long dry grass set. This is a quick, visible win, and fits PR 3 (vegetation).
2. Build the NVIS grid and select tree groups by class. This goes in a separate PR, with the data, tool and credit.
3. Season swap for grass.
