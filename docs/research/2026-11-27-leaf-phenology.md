# The trees' leaf cycle: the model and its sources (2026-11-27)

The user, 2026-11-27: "trees should gradually lose their leaves". With winter's bare trees (W4, `BNE_WINTER`) and the
Experimental page's **Leaf cycle** (`BNE_LEAF_CYCLE`, off by default), each deciduous tree follows its own dates around
the place's leaf calendar. A wood then colours and thins over weeks, one tree at a time, and leafs out in spring the
same way.

Code:
- `native/earthfast/src/phenology.rs`: the calendar.
- `server/leafcycle.py`: the anchor's `leaf` field.
- `mod/.../baretrees.lua`: `treeLeaf`, `leafState`, each tree's dates and look.

## What is computed

The **place's calendar** comes from the Rust kernel (`phenology.rs`). It gives these days of the year per species
group: oak, beech, birch (East Coast USA's aspens draw the birch cards) and poplar.

| Event | Day | Approximate MODIS MCD12Q2 metric |
|---|---|---|
| Bud break | `bud` | Greenup: 15 % of the EVI2 amplitude |
| Full leaf | `full` | Maturity: 90 % |
| First colour | `colour` | Senescence: last 90 % crossing |
| Leaf fall starts | `drop` | |
| Midpoint | `half` | MidGreendown: 50 % |
| Bare | `bare` | Dormancy: 15 % |

The calendar has four parts:
- **Reference** (45° N, continental humid Koppen Dfa/Dfb, sea level):

  | Species | Bud | Full | Colour | Drop | Half | Bare |
  |---|---|---|---|---|---|---|
  | Oak | 118 | 146 | 270 | 284 | 295 | 317 |
  | Beech | 112 | 134 | 262 | 274 | 285 | 305 |
  | Birch | 106 | 124 | 252 | 260 | 272 | 288 |
  | Poplar | 109 | 129 | 257 | 267 | 278 | 295 |

  - Spring order: birch, then poplar, beech, oak (PEP725: birch DOY 107, beech 117, oak 122 in Germany).
  - Autumn order: birch and poplar first, oaks last (Delpierre et al. 2009; Gill et al. 2015).
- **Latitude:**
  - Spring is 3.5 days later per degree poleward up to 55°, then 2 days per degree. Hopkins' bioclimatic law gives 4 days per degree; MODIS green-up studies give 3-4.
  - Autumn is 2.5 days earlier per degree up to 55°, then 1 day per degree.
- **Elevation:** spring is 2.5 days later per 100 m and autumn 1.5 days earlier. Vitasse et al. 2009 found 1.1-3.4 days per 100 m for leaf unfolding in the Pyrenees, and weaker, mixed trends for senescence. Hopkins' law gives 4 days per 400 ft, about 3.3 days per 100 m.
- **Climate offsets** (spring days, autumn days) by Koppen-Geiger class, Beck et al. 2023:

  | Class | Spring | Autumn | Basis |
  |---|---|---|---|
  | Cfb / Cfc maritime | -22 | +23 | Fitted to PEP725 Germany: beech leaf unfolding DOY 116.6 at about 50.5° N |
  | Cs Mediterranean | -10 | +12 | |
  | Cfa humid subtropical | +8 | +5 | Baltimore leaf-out about mid-April |
  | Cw | 0 | +5 | |
  | Ds | 0 | -3 | |
  | Dw | +3 | -3 | |
  | Dfc / Dfd / E | -12 | +8 | Boreal birch green-up about 125-150, bare about 265-285 |
  | B arid | 0 | +5 | Planted trees |

  - **Tropics:** no cycle within 23° of the equator, or in classes A.
  - **Hot deserts and steppes** (BWh, BSh): a quarter of the trees take part. These are planted, irrigated trees, as in `seasonlook.CLIMATE_LEAF`.
- The **southern hemisphere** is half a year later: the mod shifts its day by 182, as `season.py` does.
- **Elevation input:** the anchor's ground height. Individual trees do not yet use their own height. The `perM` values in the message are kept for that.

**Each tree:**
- **Its own dates:** two independent draws of its fixed rank (MurmurHash3, normal-like). They shift its spring by sd 6 days and its autumn by sd 9 days. PEP725 and NPN show trees of one site leafing out within about a week, and colouring and dropping over about two.
- **Leaf share:**
  - In spring it rises from `bud` to `full`.
  - It stays full until `drop`.
  - It falls to nothing at `bare`.
- **Colour:**
  - The fresh flush (`s`) fades over the 14 days after full leaf.
  - The autumn colour (`f`, the species' tint from `seasonlook.SPECIES`) grows from `colour` to `half` and stays at full until bare.
  - Both are quantised to `TINT_STEPS` = 4.
- **Look:**
  - A tree with half or more of its leaves draws them: the stock tree, or a tinted copy at its kind and step.
  - Below half, it draws its bare twig copy.
  - Marcescence: the `KEEP` share of oaks and young beeches keeps a crown of dead leaves (the `w` tint at full) from leaf fall to its own bud break.
- **Determinism:** the result depends only on the date, the place and the tree's rank. It is the same for every player.

**When it is evaluated:**
- Once per tree, when the tree is first placed; trees.lua caches the shape per row.
- One day per session. A new anchor's calendar is taken only when it moves 3 days or more (a teleport, not a rebase).
- Nothing runs per frame, and no material is changed live.
- The tinted copies are made once and kept: one folder for every kind and step, so the engine bakes their far imposters once.

## Sources

Tags:
- **[S]**: confirmed in the source.
- **[M]**: from the literature, not re-read for this note.
- **[E]**: our estimate, used for tuning.

**MODIS MCD12Q2 v6.1:**
- Friedl, M., Gray, J., Sulla-Menashe, D. (2022), MODIS/Terra+Aqua Land Cover Dynamics Yearly L3 Global 500 m, NASA LP DAAC, https://www.earthdata.nasa.gov/data/catalog/lpcloud-mcd12q2-061 [S]. It defines the metrics above.
- Gray, J. et al. (2019), the v6 algorithm user guide [M].
- We use no MCD12Q2 data product. The model is a fitted climatology.

**Latitude:**
- Zhang, X., Friedl, M., Schaaf, C., Strahler, A. (2004), "Climate controls on vegetation phenological patterns in northern mid- and high latitudes inferred from MODIS data", Global Change Biology 10:1133-1145 [S, qualitative].
  - Green-up moves north from March, and dormancy moves south from late September.
  - Forest green-up follows a thermal-time model.
- Hopkins, A. D. (1918), "Periodical events and natural law as guides to agricultural research and practice", Monthly Weather Review Supplement 9 [S, secondary]. It gives 4 days per degree of latitude, per 5° of longitude and per 400 ft of altitude.
- Richardson, A. D. et al. (2019), "Testing Hopkins' Bioclimatic Law with PhenoCam data", Applications in Plant Sciences 7(3) [S]. Deciduous green-up follows the law.

**Elevation:**
- Vitasse, Y. et al. (2009), "Leaf phenology sensitivity to temperature in European trees: do within-species populations exhibit similar responses?", Agricultural and Forest Meteorology 149:735-744 [M]. It gives 1.1-3.4 days per 100 m for leaf unfolding.
- Vitasse, Y., Delzon, S. et al. (2009), Tree Physiology 29:1-11 [S, abstract].

**PEP725:**
- Templ, B. et al. (2018), "Pan European Phenological database (PEP725): a single point of access for European data", International Journal of Biometeorology 62:1109-1113 [M].
- Leaf unfolding (BBCH 11), mean DOY and SD over 107 German sites, 1980-2009 [S, secondary]:

  | Species | Mean DOY | SD (days) |
  |---|---|---|
  | Fagus sylvatica | 116.6 | 7.5 |
  | Quercus robur | 122.1 | 8.1 |
  | Betula pendula | 107.1 | 9.4 |

**Autumn:**
- Delpierre, N. et al. (2009), "Modelling interannual and spatial variability of leaf senescence for three deciduous tree species in France", Agricultural and Forest Meteorology 149:938-948 [S, abstract]. The model is photoperiod-gated cold-degree days. Quercus colouring processes start earlier than in Fagus.
- Gill, A. L. et al. (2015), "Changes in autumn senescence in northern hemisphere deciduous trees: a meta-analysis of autumn phenology studies", Annals of Botany 116:875-888 [S].

**Ranges [E] and the [M] items:**
- The per-latitude MCD12Q2 DOY ranges are [E].
- USA-NPN species timing and the transition lengths are [E]:
  - Spring: bud break to full leaf takes 3-5 weeks.
  - Autumn: first colour to bare takes 4-7 weeks.
- The marcescence shares are [E]; the existing `KEEP` values are used.
- Check these against the NPN data portal and the PEP725 autumn phases before the switch goes on by default.

**Tests:**
- `phenology.rs` tests check the kernel against:
  - PEP725 Germany: beech, oak and birch leaf-out within 6 days.
  - MCD12Q2 eastern USA at 40° N: green-up 90-110, bare 305-330.
  - Boreal birch at 60° N.
- `mod/tests/test_baretrees.lua` checks Baltimore:
  - The wood thins over three weeks or more.
  - About half of it is bare at the midpoint.
  - No leafless tree comes back.
  - The marcescent share in January.
  - Spring leaf-out tree by tree.

## Not done yet

- **This year's weather:** a process model of temperature and photoperiod with real daily temperatures, for example a growing-degree-day spring and Delpierre's autumn on Open-Meteo's archive. Today's model is a climatology.
- **Per-tree elevation:** on mountains, trees above the anchor should turn earlier.
- **Partly thinned crowns:** a tree is either leafy or bare. The stock leaf opacity maps are binary (65 % at 0, 31 % at 255), so raising alphaRef cannot thin them. A thinning state needs our own twig-with-leaves atlases, whose leaves carry their drop order in their opacity.
- **Poplars:** they have no autumn tint (`TINT_SPECIES`); they go from green to bare.
