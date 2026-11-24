"""Trees, bushes and rocks per map-data cell (docs/ROADMAP.md v1.9 "Cities & nature"; the user, 2026-10-13:
"trees, rocks and other decoration").

    veg = Vegetation(md, cache_dir)
    items = veg.cell(key)          # Items: lon, lat, height, radius, kind, group, rank, yaw, scale

Deterministic (ROADMAP "Principle: deterministic generation"): every item is a pure function of the
cell's map data and VERSION; its random choices (species, yaw, size, thinning rank) hash its position.

- Trees: the tops of Meta/WRI's 1.2 m canopy height model (CHMv2), local maxima of the lightly
  smoothed heights, in a window that grows with the height (a crown about 0.12 h + 1 m across), taller
  than TREE_MIN_M. Not on buildings (the CHM counts roofs, solar panels: Kayenta), roads (a trunk in
  the lane, under canopy that overhangs it) or water.
- Bushes: canopy maxima from BUSH_MIN_M to TREE_MIN_M, and a hashed scatter over shrubland.
- Rocks: a hashed scatter over bare / sparse land (WorldCover 60), thinner over shrubs and grass.
- Groups: deciduous / conifer by a regional mix and height, shrub, rock (the mod maps groups to its
  stock shapes).
- Orchards (land_use class orchard): trees in lines along the long side, and the canopy's trees
  inside the orchard left out so the rows are not filled in at random.
- Farmland: shrubs in a line down the gap between the dirt crop rows.
"""
import functools
import logging
import math
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

import beaches
import fast
import dmath
import dnp

import mapcells
import pacer
import staging

log = logging.getLogger("earthd.vegetation")

VERSION = 56          # (the cached cells hold their groups: 14 for the regional rules of GitHub #110; 16: the roads'
                      # drawn widths and sidewalks kept clear; 17: BC's lidar canopy heights, mapdata's canopy layer 2;
                      # 18: the rounded junction corners kept clear, roadcorners.py; 19: one tree per flat crown,
                      # thin_tops; 20: none on or beside a bridge deck, _mask_decks; 21: orchard rows, canopy trees
                      # inside an orchard left out; 22: shrub lines between the crop rows; 23: both of those, the
                      # orchard rows and the deck mask; 24: no rocks or bushes on a beach's sand, beaches.py; 25: the
                      # Mediterranean basin's own trees, G_MEDITERRANEAN; 26: the far clumps, CLUMP_OF, and the
                      # vineyards' rows, vineyard_items; the round bales after the harvest, bale_items; 27: no bushes in a bus station's yard, PR #1023;
                      # 28: lengths in ground metres at every latitude, _merc_k; ties among flat crowns by a hash;
                      # 29 (dev): the far clumps kept off the carriageways, CLUMP_ROAD_MARGIN_M;
                      # 29: Eurasia's own species from the stock shapes, species_groups: birches, oaks and beeches
                      # out of the broadleaves, Scots pines and (Douglas firs standing in for) spruces out of the
                      # conifers by latitude and height above the sea, both in stands (stand_u); dead trees; date and
                      # fan palms in warm towns; the rainforests' trees tropical, fewer palms there);
                      # 30: the bushes by the climate (shrub_groups: maquis, the north's, the tropics',
                      # the deserts'), ferns on the forest floor, dead wood added in remote forest (_remote_floor), the
                      # mangroves' trees tropical
                      # 31: dead wood only added, in remote forest on level ground (30's replaced bushes and reached
                      # over roads); the tropics' vine-draped thickets the same way (G_TRO_THICKET); no mossy forest
                      # oaks or dead trees by the roads and in town (OPEN_FROM)
                      # 32: more admixture in the stands (STAND_MIX 0.3), pine_eu's shapes back to five
                      # 33: the open's oaks the open broadleaves (oak_open had two young oaks and one veteran)
                      # 34: the conifers in the open too (no bare-trunked crowd-grown firs and pines by roads)
                      # and palms by the climate: coconut palms in the tropics, fan / date palms elsewhere
                      # 35: Portugal's and Galicia's eucalypt plantations (eucalypt_share); the regional rules by
                      # RESOLVE's ecoregions (ecoregion.py) where boxes and climates stood: the Mediterranean's
                      # trees, the creosote, the steppe junipers; the Cantabrian oak woods; the conifers' share
                      # from the forests' leaf type (leaftype.py) where a coarse rule stood; the world's own groups
                      # by biome (_world_groups: savanna, laurel, pine_maritime; the tropics' and the south's pines,
                      # the south's beeches); the maquis by RESOLVE's Mediterranean biome
                      # 36: fewer birches west of Belarus (continental_shares), the broadleaves mixed tree by tree
                      # (BROAD_STAND_MIX); the broadleaves' shares everywhere outside the tropics (broad_shares:
                      # North America's, East Asia's, Japan's, the oceanic climates' by latitude), the spruces' west
                      # of Poland by SPRUCE_CENTRAL
                      # 37: one top per broadleaf crown (crown_spacing: the thinning by the leaf type and biome)
                      # 38: the conifer share kept to the leaf type map (conifer_odds), the beech belt,
                      # the pine woods' birches
                      # 39: Australia's forest broadleaves eucalypts, East Asia's conifers conical (sugi, Chinese fir)
                      # 40: by EU-Forest's NFI plots: the Mediterranean's conifers kept, its evergreen share by
                      # ecoregion; no spruce belt in dry climates; the Atlantic's spruces; the north's birches
                      # 41: the leaf type the mean of LC100's and ESA CCI's (leaftype.PATHS)
                      # 42: Europe's conifer share half ForestPaths' genus map's, its genera from it (treegenus.py)
                      # 43: the species' stands one ragged grid (stand_cells); the maps read on a grid (_on_grid)
                      # 44: North America's generic conifers spruces and pines (NA_SPRUCE_LAT)
                      # 45: the tropics' shores' coconut palms (coast_palms); the thickets no-collide
                      # 46: the Mediterranean coasts under a mountain ecoregion (eco_med, MED_FRINGE_*)
                      # 47: a tile's biome carries its Koppen class (the ground cover's grasses by climate)
                      # 48: the tropics' remote floor half dead wood (G_DEADWOOD_TROPICAL, THICKET_SHARE)
                      # 49: more birches: Europe's north fewer conifers (NORTH_CONIFER_FACTOR), central Europe CONT_BIRCH_SOUTH
                      # 50: dev's thinning back (its count of trees), the tall trunks at least TRUNK_MIN_M apart (_thin_trunks)
                      # 51: 50's last two builds kept 50's spread cells (the same VERSION); the trunk distance by height
                      # 52: the riverside poplars off the forest's land cover (regional)
                      # 53: the far clumps at one of their trees, not the square's centroid (_unlattice: no lattice)
                      # 54: the town palms by the sea and low only (palm_fit)
                      # 55: the veteran oaks in loose woods (veteran_oaks), the tropics' giant filler (G_TRO_GIANT)
                      # 56: the vineyards' edges filled with single rows (G_VINE_ROW)
TREE_MIN_M = 3.0
# The canopy raster, the scatter grids and the crown radii are in Web Mercator units (EPSG:3857: a metre there is
# cos(lat) metres on the ground) and were tuned by eye at mid latitudes. Past them the same pixel counts covered less
# ground: at 69 N tops were searched and thinned over radii 2.1x too small and the bushes' grids were 4.6x as dense
# (a player's Lapland forest, 2026-11-14: "way too many trees", a grid pattern far away). _merc_k scales those lengths
# to what they were at MERC_REF_LAT, so the tuned look holds everywhere (and the tropics are no longer a little sparse).
MERC_REF_LAT = 40.0
CROWN_SPACING = 0.5          # tops kept this many crown radii apart (0.8 looked sparse: the user, 2026-10-13)
# (36-49 thinned broadleaf cells at 1.0 crown radii, crown_spacing: the trunks in pairs gone with 35-45 % of their
# trees; 50: dev's thinning again, the tall trunks spaced by _thin_trunks without losing them)
BUSH_MIN_M = 1.0
KIND_TREE, KIND_BUSH, KIND_ROCK = 0, 1, 2
GROUPS = ("deciduous", "conifer", "shrub", "rock", "pine_west", "palm", "juniper", "desert_shrub", "desert_rock",
          "oak_west", "tropical", "mediterranean", "conifer_clump", "deciduous_clump", "oak_west_clump",
          "mediterranean_clump", "vine", "straw_n", "straw_s", "silage_n", "silage_s", "eucalyptus",
          "eucalyptus_clump", "poplar", "creosote",
          # (29, species_groups: new names only at the end, the older groups keep their numbers)
          "birch", "oak", "beech", "pine_eu", "spruce", "snag_broad", "snag_conifer", "palm_dry",
          "birch_clump", "oak_clump", "beech_clump",
          # (30: the bushes' and forest floors' own, shrub_groups; the stock survey 2026-11-16)
          "maquis", "shrub_north", "shrub_tropical", "fern", "deadwood", "tro_thicket",
          "deciduous_open", "oak_open", "birch_open", "beech_open", "pine_west_open", "mediterranean_open",
          "tropical_open", "conifer_open", "spruce_open", "pine_eu_open", "redwood", "tro_understory",
          "desert_shrub_hot",
          # (35: the world's own, species_groups' biome rules)
          "savanna", "laurel", "pine_maritime",
          # (48: the tropics' dead wood: Jungle Rock Island's fallen fronds and branches, its fallen giant)
          "deadwood_tropical",
          # (55: a rare veteran oak inside loose broadleaf woods; the tropics' giant filler in remote forest)
          "oak_veteran", "tro_giant",
          # (56: a vineyard's single row, Italy's grape_vine, where the three-row group does not fit: the edges)
          "vine_row")
(G_DECIDUOUS, G_CONIFER, G_SHRUB, G_ROCK, G_PINE_WEST, G_PALM, G_JUNIPER, G_DESERT_SHRUB, G_DESERT_ROCK,
 G_OAK_WEST, G_TROPICAL, G_MEDITERRANEAN, G_CONIFER_CLUMP, G_DECIDUOUS_CLUMP, G_OAK_WEST_CLUMP,
 G_MEDITERRANEAN_CLUMP, G_VINE, G_STRAW_N, G_STRAW_S, G_SILAGE_N, G_SILAGE_S, G_EUCALYPTUS, G_EUCALYPTUS_CLUMP,
 G_POPLAR, G_CREOSOTE, G_BIRCH, G_OAK, G_BEECH, G_PINE_EU, G_SPRUCE, G_SNAG_BROAD, G_SNAG_CONIFER, G_PALM_DRY,
 G_BIRCH_CLUMP, G_OAK_CLUMP, G_BEECH_CLUMP, G_MAQUIS, G_SHRUB_NORTH, G_SHRUB_TROPICAL, G_FERN, G_DEADWOOD,
 G_TRO_THICKET, G_DECIDUOUS_OPEN, G_OAK_OPEN, G_BIRCH_OPEN, G_BEECH_OPEN, G_PINE_WEST_OPEN, G_MEDITERRANEAN_OPEN,
 G_TROPICAL_OPEN, G_CONIFER_OPEN, G_SPRUCE_OPEN, G_PINE_EU_OPEN, G_REDWOOD, G_TRO_UNDERSTORY,
 G_DESERT_SHRUB_HOT, G_SAVANNA, G_LAUREL, G_PINE_MARITIME, G_DEADWOOD_TROPICAL, G_OAK_VETERAN, G_TRO_GIANT,
 G_VINE_ROW) = range(62)
# Far clumps (GitHub #1074, "trees cost too much"; the asset survey 2026-11-10): a forest's canopy trees gathered on a
# world-fixed grid of about CLUMP_CELL_M squares (a whole number of them a geotile: none straddles two); a square with
# CLUMP_MIN of them gets one item of a stock clump shape (trees.lua's *_clump groups: East Coast USA's
# tree_douglasfir_group_low / tree_oak_forest_group / tree_beech_forest_group, West Coast USA's oak_a_distant, Italy's
# holm_oak_filler_center, 4-8 trees each, the materials of the single trees they stand for). The mod shows the clump
# beyond its near ring (trees.lua CLUMP_M) and the square's trees, flagged by a negative scale, only within it: a far
# forest tile of ~900 single trees becomes ~150 clumps. Other groups (palms, junipers, tropical trees) stay single.
CLUMP_OF = {G_CONIFER: G_CONIFER_CLUMP, G_PINE_WEST: G_CONIFER_CLUMP, G_REDWOOD: G_CONIFER_CLUMP,
            G_DECIDUOUS: G_DECIDUOUS_CLUMP,
            G_OAK_WEST: G_OAK_WEST_CLUMP, G_MEDITERRANEAN: G_MEDITERRANEAN_CLUMP, G_EUCALYPTUS: G_EUCALYPTUS_CLUMP,
            # (29) the Scots pines and spruces far off as the conifer clump (no stock pine group: a pine clump is for a
            # shape of our own), the broadleaves as their own species' groups
            G_BIRCH: G_BIRCH_CLUMP, G_OAK: G_OAK_CLUMP, G_BEECH: G_BEECH_CLUMP, G_PINE_EU: G_CONIFER_CLUMP,
            G_SPRUCE: G_CONIFER_CLUMP,
            # (35) the maritime pines as the other pines; the laurel forests as the cork oaks' filler (their shapes)
            G_PINE_MARITIME: G_CONIFER_CLUMP, G_LAUREL: G_MEDITERRANEAN_CLUMP,
            # (45) as dev's far forests: the trees by a country road (the open groups, OPEN_ROAD_M) and the tropics'
            # rainforest trees (dev's generic broadleaves there, clumped) far off as their clumps again (before: a
            # tile's clumps a half to none, Rio's 1106 -> 0; the town's trees stay single, clumps())
            G_DECIDUOUS_OPEN: G_DECIDUOUS_CLUMP, G_OAK_OPEN: G_OAK_CLUMP, G_BIRCH_OPEN: G_BIRCH_CLUMP,
            G_BEECH_OPEN: G_BEECH_CLUMP, G_PINE_WEST_OPEN: G_CONIFER_CLUMP,
            G_MEDITERRANEAN_OPEN: G_MEDITERRANEAN_CLUMP, G_CONIFER_OPEN: G_CONIFER_CLUMP,
            G_SPRUCE_OPEN: G_CONIFER_CLUMP, G_PINE_EU_OPEN: G_CONIFER_CLUMP,
            G_TROPICAL: G_DECIDUOUS_CLUMP, G_TROPICAL_OPEN: G_DECIDUOUS_CLUMP}
CLUMP_GROUPS = tuple(sorted(set(CLUMP_OF.values())))
CLUMP_CELL_M = 18.0
CLUMP_MIN = 3
CLUMP_ROWS_MAX = 2000       # clump rows a tile sends at most (beside TILE_ROWS_MAX's single items)
CLUMP_R_K, CLUMP_R_C = 0.3, 2.0     # a clump's radius (ground m): CLUMP_R_K * height + CLUMP_R_C
# A far clump keeps its footprint off the carriageways (the user, 2026-11-16: "take into consideration if the road is
# blocked by other buildings or trees"): a forest road through a square had the square's one clump, 6-10 m across, on
# its lanes, seen from afar until the single trees took over near it. Stock East Coast USA keeps its group shapes 5-10 m
# off a drivable road's edge (centre to edge, p5: tree_douglasfir_group 5.7 m, tree_beech_forest_group 5.1 m,
# tree_douglasfir_group_low 10.4 m). Its reach: the road's half width (drawn or its class's, the wider) + a margin by
# class (CLUMP_ROAD_MARGIN_M, else CLUMP_ROAD_MARGIN_DEFAULT_M). A square whose clump reaches in is split along that
# road's line; each side's clump is moved straight off the roads by up to CLUMP_SHIFT_M, else its trees stay single
# (shown far as they were before the clumps). earthfast.veg_clumps
CLUMP_ROAD_MARGIN_M = {"motorway": 3.0, "motorway_link": 2.0, "trunk": 2.5, "trunk_link": 1.5, "primary": 2.0,
                       "primary_link": 1.0, "secondary": 1.5, "tertiary": 1.0}
CLUMP_ROAD_MARGIN_DEFAULT_M = 0.5
CLUMP_SHIFT_M = 4.0
# The Mediterranean basin's lowland ecoregions (35: RESOLVE's Ecoregions 2017, ecoregion.py; before, Beck et al.'s Cs
# climates inside a box, which took Galicia's and northern Portugal's oak woods and the Black Sea's and missed North
# Africa's and the Adriatic's maquis): holm and cork oaks, olives, maritime pines, cypresses (Italy's stock trees,
# trees.lua's "mediterranean"; the asset survey 2026-11-10 found them loaded with Italy's folder but unused). The
# Palearctic's Mediterranean biome without its montane forests (Corsica's, the Pindus's, the Apennines', northwest
# Iberia's, inner Anatolia's: oaks, beeches, firs), and the southern hemisphere's outside Australia (whose are
# eucalypts): Chile's matorral (quillay, peumo, litre: evergreen sclerophylls), the Cape's fynbos and renosterveld
# (their trees the stone and cluster pines, oaks and olives planted since the 1650s). California's keeps the West
# Coast's own groups.
ECO_MED = (785, 787, 789, 790, 791, 792, 793, 794, 795, 796, 797, 798, 799, 803, 804, 805, 806, 596, 89, 90)
# (46) The coast below a mountain ecoregion: RESOLVE's polygons are coarse, and where a range rises from the sea its
# mountain ecoregion reaches the shore (the Dinaric Mountains' at Makarska, under Biokovo: its Aleppo pine park forest
# came out Scots pines, beeches and ferns, and no cypress; the Pindus's, Corsica's, the South Apennines', western
# Anatolia's coasts the same). A Csa point (Koppen: hot dry summers, the lowlands' climate there) outside ECO_MED with
# one of them within MED_FRINGE_DEG is that one's (the nearer ring first, then the ring's order): Galicia's and northern
# Portugal's summer-dry oceanic woods are Csb, so stay out.
# The Koppen map is 0.1 degree: a coast's pixel averages the sea's and the mountain's (Makarska's northern half Cfb,
# its quays the sea's class 0), so one of MED_FRINGE_NEAR_K next to a Csa pixel (MED_FRINGE_K_DEG) and by the sea
# (a sea pixel within MED_FRINGE_SEA_DEG, or its own) counts as Csa: Makarska's north half, not Biokovo's summit.
MED_FRINGE_DEG = (0.075, 0.15)
MED_FRINGE_K = (8,)
MED_FRINGE_NEAR_K = (0, 14, 15)         # the sea, Cfa, Cfb
MED_FRINGE_K_DEG = 0.1
MED_FRINGE_SEA_DEG = 0.05
# The mountain and hill ecoregions whose foot is a Mediterranean coast or lowland (a world survey at 0.05 degrees,
# 2026-11-19: the Dinaric Mountains', western Anatolia's, the Maghreb's, northwest Iberia's (the Douro's), the Pindus's,
# the Apennines', Corsica's, the Balkans' and the Rhodopes' Thracian feet, the Marmara's, the Ligurian Alps', the
# Cevennes', the Pyrenees' Roussillon, central Chile's Andean foot). Not the steppes' and the plains' that touch a
# Csa pixel (central and eastern Anatolia's, the Zagros', Syria's, the Po's, the Karoo's, the High Atlas's): their
# trees are deciduous oaks, pistachios and poplars, not the maquis.
MED_FRINGE_ECO = (660, 786, 701, 800, 801, 802, 644, 788, 646, 665, 678, 689, 686, 676, 648, 563)
_RING = ((0.0, 1.0), (0.7071, 0.7071), (1.0, 0.0), (0.7071, -0.7071), (0.0, -1.0), (-0.7071, -0.7071), (-1.0, 0.0),
         (-0.7071, 0.7071))     # (south-north, west-east) unit steps
# More regional trees (the asset survey 2026-11-10, the stock shapes the flora supports; earthfast.regional_groups):
# - Australia: eucalypts most of its trees (West Coast USA's eucalyptus_double / _tall, eucalyptus_group far).
# - Temperate Europe's river banks: a share of the broadleaves within RIPARIAN_M of water are poplars (Automation Test
#   Track's s_poplar_large: the black and hybrid poplars along the Loire, the Po, the Danube; willows and alders have no
#   stock shape).
# - North America's hot deserts (35: their ecoregions, the Mojave, Sonoran, Chihuahuan, Baja California's and the Gulf
#   of California's; before, Koppen BWh in a box): most desert shrubs are creosote bushes (Automation Test Track's
#   shrub_a-c, the creosote of West Coast USA's desert; Larrea tridentata's range).
ZONE_MED, ZONE_RIPARIAN, ZONE_HOT_DESERT = 1, 2, 3
RIPARIAN_BOX = (42.0, -10.0, 60.0, 30.0)
RIPARIAN_M = 30.0
ECO_CREOSOTE = (426, 428, 431, 433, 435)
ROAD_CLEAR_M = 1.5          # trees kept this far off a road's edge (its width by class below)
TREE_PIT_M = 0.4            # m past a road's edge (+ the push's 0.6) where a top over the road lands: a sidewalk's
                            # tree pit by the curb (pushed out of the full corridor, Boston Street's trees landed on the
                            # row houses at the back of the sidewalk and all went; Mapillary: one every ~8 m, 2026-10-17)
PATH_CLASSES = {"footway", "path", "steps", "cycleway", "bridleway", "pedestrian"}   # no corridor: street trees
                            # stand in the sidewalk (mapped as a footway: Boston Street's row sat on its line)
ROAD_HALF_WIDTH_M = {"motorway": 7.0, "trunk": 6.0, "primary": 5.5, "secondary": 5.0, "tertiary": 4.5,
                     "residential": 4.0, "service": 3.0, "unclassified": 3.5, "living_street": 3.0}
ROAD_HALF_DEFAULT_M = 2.5
SCATTER_CELL_M = 6.0        # the hashed scatter's grid (one candidate per cell)
# class: (bush, rock). Tree cover (10) gets an understory (Columbia's photos: forest edges are thickets
# of shrubs and vines under the trees; ours were bare ground between trunks), also under the canopy.
SCATTER_P = {10: (0.30, 0.0), 20: (0.10, 0.04), 30: (0.01, 0.003), 60: (0.0, 0.08), 100: (0.02, 0.02)}
UNDERSTORY = 10
BUILT_NEAR_M = 60.0         # bare ground with built-up land this far around gets no scatter (squares, lots)
# Rocks on bare ground lie on slopes, outcrops and fans, not on flat playas, salt pans and dry lake beds (the
# Bonneville Salt Flats had a rock every ~75 m2, the user 2026-10-20; photos: a bare white crust for miles). A bare
# cell's rock is kept with the share of the ground's slope between these (the DEM ROCK_SLOPE_D m either side, at
# ROCK_SLOPE_ZOOM: coarse tiles, few downloads); without a DEM, as before.
ROCK_SLOPE = (0.02, 0.10)
BARREN_NEAR_M = 20.0        # m: a canopy top on bare ground or snow needs vegetated land this near (_build)
ROCK_SLOPE_D = 40.0
ROCK_SLOPE_ZOOM = 12
# Forest edges along roads in humid country (Columbia's photos: a wall of shrubs, saplings and vines from the
# ground up, where ours showed bare trunks over grass): a denser band of bigger bushes within EDGE_M of a road.
EDGE_M = 7.0
EDGE_CELL_M = 3.0
EDGE_P = 0.4
MEM_CELLS = 16
BUILD_SOON_MAX = 64          # cells build_soon keeps queued (the generated ground's blocks outside the warm-up)
WARM_M = 1200.0              # cells within this of the player are built ahead (warm_around)
WC = {"tree": 10, "shrub": 20, "grass": 30, "crop": 40, "built": 50, "bare": 60, "snow": 70,
      "water": 80, "wetland": 90, "mangrove": 95, "moss": 100}


@dataclass
class Items:
    lon: np.ndarray
    lat: np.ndarray
    height: np.ndarray          # m (trees: the CHM top; bushes and rocks: their size)
    radius: np.ndarray          # m (crown / footprint)
    kind: np.ndarray            # uint8 KIND_*
    group: np.ndarray           # uint8 GROUPS index
    rank: np.ndarray            # float32 in [0, 1): thinning (a level shows those below its threshold)
    yaw: np.ndarray             # degrees
    scale: np.ndarray           # size variation (around 1)

    def __len__(self):
        return len(self.lon)

    def take(self, sel):
        return Items(*(getattr(self, f)[sel] for f in ("lon", "lat", "height", "radius", "kind", "group",
                                                       "rank", "yaw", "scale")))


def _hash01(a, b, salt):
    """Deterministic uniform [0, 1) from two integer arrays and a salt."""
    a = np.asarray(a, np.uint64)
    b = np.asarray(b, np.uint64)
    x = a * np.uint64(0x9E3779B97F4A7C15) ^ (b + np.uint64(salt)) * np.uint64(0xC2B2AE3D27D4EB4F)
    x ^= x >> np.uint64(31)
    x *= np.uint64(0xBF58476D1CE4E5B9)
    x ^= x >> np.uint64(29)
    return (x >> np.uint64(11)).astype(np.float64) / float(1 << 53)


def _merc_k(lat):
    """Web Mercator length per tuned length at latitude lat (1 at MERC_REF_LAT, 2.14 at 69 N, 0.77 at the equator)."""
    c = dmath.cos(dmath.radians(min(abs(float(lat)), 85.0)))
    return dmath.cos(dmath.radians(MERC_REF_LAT)) / max(c, 0.05)


def _shift_max(a, r):
    """The maximum over the disc of radius r around each pixel ((dj - r)^2 + (di - r)^2 <= r * r + r; outside the
    array -inf; float32). earthfast disc_max (2026-10-23: the numpy version's full-array pass per disc offset)."""
    fast.require(64, "disc_max")
    return fast._ef.disc_max(np.ascontiguousarray(a, np.float32), int(r))


def _dilate(m, r):
    """Bool mask grown by r pixels (a square window, separable). (42) By doubling: a mask grown by d grown again by
    s <= d + 1 is grown by d + s, so log2(r) passes an axis, not r (OPEN_ROAD_M's ~12 px: 4 passes for 12; the same
    pixels)."""
    out = m.copy()
    for axis in (0, 1):
        d = 0
        while d < r:
            s = min(d + 1, r - d)
            acc = out.copy()
            acc[tuple(slice(s, None) if a == axis else slice(None) for a in range(2))] |= \
                out[tuple(slice(None, -s) if a == axis else slice(None) for a in range(2))]
            acc[tuple(slice(None, -s) if a == axis else slice(None) for a in range(2))] |= \
                out[tuple(slice(s, None) if a == axis else slice(None) for a in range(2))]
            out = acc
            d += s
    return out


def _dry_west(lat, lon):
    lat, lon = np.asarray(lat), np.asarray(lon)
    return (lon < -102.0) & (lat > 25.0) & (lat < 46.0) & ~((lon < -121.0) & (lat > 37.0))


def regional(group, height, lat, lon, lcls, u, riparian=None):
    """The groups of a region (the stock maps' species: West Coast USA's pines, oaks and palms, Utah's junipers
    and desert bushes, Johnson Valley's rocks, Jungle Rock Island's palms and tropical trees) from the generic
    ones, by a coarse climate from the position and the land cover class at the item (until a species map): the Rust
    kernel earthfast.regional_groups (its rules there). u: a hashed [0, 1) per item; riparian: per item, within
    RIPARIAN_M of water (None: none), counted only off the forest's land cover (51: the poplars are Automation Test
    Track's Lombardy poplars, a planted columnar cultivar of the lowland rivers' farmland and towns: 8 % of the trees of
    Bieszczady's beech forest, along its mountain streams, the model audit 2026-11-20). Pure."""
    fast.require(201, "regional_groups")
    f64 = np.float64
    n = np.size(group)
    bc = lambda a, t: np.ascontiguousarray(np.broadcast_to(np.asarray(a, t), (n,)))     # noqa: E731
    la, lo = bc(lat, f64), bc(lon, f64)
    pre, lc, uu = bc(group, np.uint8), bc(lcls, np.uint8), bc(u, f64)
    g = np.asarray(fast._ef.regional_groups(
        pre, bc(height, f64), la, lo, lc, uu,
        [G_DECIDUOUS, G_CONIFER, G_SHRUB, G_ROCK, G_PINE_WEST, G_PALM, G_JUNIPER, G_DESERT_SHRUB, G_DESERT_ROCK,
         G_OAK_WEST, G_TROPICAL], [WC["shrub"], WC["bare"], WC["grass"], WC["built"]],
        zones(la, lo, None if riparian is None else bc(riparian, bool) & (lc != WC["tree"])),
        [G_MEDITERRANEAN, G_EUCALYPTUS, G_POPLAR, G_CREOSOTE]), np.uint8)
    return _mediterranean_leaf(pre, g, la, lo, lc, uu)


# (39) the Mediterranean's own trees by the forest inventories (EU-Forest, Mauri et al. 2017: the NFI plots' species,
# each plot's species weighted 1/k): the kernel made MED_SHARE (0.75) of its broadleaves AND conifers Italy's
# evergreen trees, which turned Spain's pine forests (its conifers 52 % of the plots' trees, 98 % of them pines) into
# holm oaks and olives, and central Italy's deciduous oak woods (Q. cerris, pubescens: 90 % of its broadleaves) too.
# Off town: a conifer stays a conifer (species_groups: the maritime and Mediterranean pines, the mountains' black and
# Scots pines), a broadleaf is an evergreen one at the ecoregion's share (ECO_EVERGREEN, else MED_EVERGREEN), a
# deciduous oak otherwise. The towns' trees keep the kernel's (their olives, holm oaks, cypresses).
ECO_EVERGREEN = {793: 0.84, 799: 0.53, 792: 0.52, 805: 0.93, 795: 0.10, 806: 0.57, 803: 0.95, 787: 0.27,
                 # (no plots: the ecoregion's own name) the Illyrian and southern Anatolian deciduous and montane
                 # woods; the Chilean matorral's and the Cape's sclerophyll scrub and woods
                 794: 0.4, 804: 0.3, 596: 0.9, 89: 0.9, 90: 0.9}
TREEGENUS_MIN = 0.01        # (39) the genus map's conifers (broadleaves) at least this share of the trees for its split
ECO_NO_BEECH = (785, 787, 789, 790, 791, 792, 793, 796, 797, 798, 799, 803, 804, 805)     # (39: no beech belt)
MED_EVERGREEN = 0.6             # (Greece's, Anatolia's, the Maghreb's, Chile's, the Cape's: no plots)


def _mediterranean_leaf(pre, g, lat, lon, lcls, u):
    """regional's groups (g) with the Mediterranean ecoregions' trees off town by their leaf type (pre: the groups
    before; ECO_EVERGREEN). Pure."""
    cand = _in(pre, (G_CONIFER, G_DECIDUOUS)) & (lcls != WC["built"]) & _in(g, (G_CONIFER, G_DECIDUOUS,
                                                                               G_MEDITERRANEAN))
    if not cand.any():
        return g
    eco = eco_med(lat[cand], lon[cand])
    med = _in(eco, ECO_MED)
    if not med.any():
        return g
    g = g.copy()
    idx = np.nonzero(cand)[0][med]
    ev = np.full(len(idx), MED_EVERGREEN)
    for e_, s in ECO_EVERGREEN.items():
        ev[eco[med] == e_] = s
    u2 = (u[idx] * 7919.0 + 0.31) % 1.0
    p = pre[idx]
    g[idx] = np.where(p == G_CONIFER, G_CONIFER, np.where(u2 < ev, G_MEDITERRANEAN, G_DECIDUOUS))
    return g


_LUTS = {}
LUT_N = 1024        # (the ids the rules test: groups < 256, Koppen classes < 31, land cover < 101, ECO_IDs < 848)


def _in(a, ids):
    """np.isin(a, ids) for small non-negative integers (groups, Koppen classes, land cover classes, ECO_IDs, all under
    LUT_N) by a cached lookup table: the species rules test ~30 sets a tile, np.isin sorted each (35: 3x faster)."""
    key = tuple(int(i) for i in ids)
    lut = _LUTS.get(key)
    if lut is None:
        lut = np.zeros(LUT_N, bool)
        lut[list(key)] = True
        _LUTS[key] = lut
    return lut[np.asarray(a)]


def _in_box(lat, lon, box):
    return (lat >= box[0]) & (lat <= box[2]) & (lon >= box[1]) & (lon <= box[3])


def _med_fringe(lat, lon):
    """Per point: the ECO_MED ecoregion a Csa point's coast belongs to (MED_FRINGE_*), else 0. Pure."""
    import climate
    import ecoregion
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    out = np.zeros(len(lat), np.int64)
    k = np.asarray(climate.koppen_many(lat, lon), np.int64)
    csa = _in(k, MED_FRINGE_K)
    near = np.flatnonzero(_in(k, MED_FRINGE_NEAR_K))
    sea = k[near] == 0
    for dy, dx in _RING:
        if sea.all():
            break
        rest = np.flatnonzero(~sea)
        sea[rest] = np.asarray(climate.koppen_many(lat[near[rest]] + dy * MED_FRINGE_SEA_DEG,
                                                   lon[near[rest]] + dx * MED_FRINGE_SEA_DEG)) == 0
    near = near[sea]
    for dy, dx in _RING:
        if not len(near):
            break
        kn = np.asarray(climate.koppen_many(lat[near] + dy * MED_FRINGE_K_DEG, lon[near] + dx * MED_FRINGE_K_DEG),
                        np.int64)
        hit = _in(kn, MED_FRINGE_K)
        csa[near[hit]] = True
        near = near[~hit]
    todo = np.flatnonzero(csa)
    for r in MED_FRINGE_DEG:
        for dy, dx in _RING:
            if not len(todo):
                return out
            e = ecoregion.eco_many(lat[todo] + dy * r, lon[todo] + dx * r)
            hit = _in(e, ECO_MED)
            out[todo[hit]] = e[hit]
            todo = todo[~hit]
    return out


def eco_med(lat, lon):
    """ecoregion.eco_many, a Mediterranean coast's points under a mountain ecoregion given the Mediterranean one's
    (MED_FRINGE_*): the ecoregion the vegetation's Mediterranean rules read. Pure."""
    import ecoregion
    lat, lon = np.atleast_1d(np.asarray(lat, np.float64)), np.atleast_1d(np.asarray(lon, np.float64))
    eco = ecoregion.eco_many(lat, lon)
    cand = np.flatnonzero(_in(eco, MED_FRINGE_ECO))
    if len(cand):
        f = np.asarray(_on_grid(_med_fringe, lat[cand], lon[cand]), np.int64).reshape(-1)
        hit = f > 0
        if hit.any():
            eco = eco.copy()
            eco[cand[hit]] = f[hit]
    return eco


def zones(lat, lon, riparian=None):
    """Per point: its regional zone for earthfast.regional_groups (ZONE_*; 0 none)."""
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    eco = eco_med(lat, lon)
    z = np.where(_in(eco, ECO_MED), ZONE_MED, 0).astype(np.uint8)
    if riparian is not None:
        z[(z == 0) & np.asarray(riparian, bool) & _in_box(lat, lon, RIPARIAN_BOX)] = ZONE_RIPARIAN
    z[(z == 0) & _in(eco, ECO_CREOSOTE)] = ZONE_HOT_DESERT
    return np.ascontiguousarray(z)


def mediterranean(lat, lon):
    """Per point: in the Mediterranean basin's lowland ecoregions (ECO_MED, eco_med)."""
    return np.ascontiguousarray(_in(eco_med(lat, lon), ECO_MED))


def _north_america(lat, lon):
    lat, lon = np.asarray(lat), np.asarray(lon)
    return (lat > 24.0) & (lon > -170.0) & (lon < -50.0)


MAP_Q = 0.0025      # (42) the 0.05 degree maps read at this grid's points (~250 m): a tile's trees share a few hundred


def _on_grid(fn, lat, lon):
    """fn(lat, lon) (a smooth map's per point values, (n,) or (n, k)) read once per MAP_Q grid point the points round to,
    not once per tree (a tile's 50-120 thousand trees, the maps read several times each: 13 ms a read). Pure."""
    lat, lon = np.atleast_1d(np.asarray(lat, np.float64)), np.atleast_1d(np.asarray(lon, np.float64))
    if len(lat) < 64:
        return fn(lat, lon)
    qi = np.round(lat / MAP_Q).astype(np.int64)
    qj = np.round(lon / MAP_Q).astype(np.int64)
    u, inv = np.unique(qi * 1000003 + qj, return_inverse=True)
    first = np.zeros(len(u), np.int64)
    first[inv[::-1]] = np.arange(len(inv))[::-1]
    v = fn(qi[first] * MAP_Q, qj[first] * MAP_Q)
    return np.asarray(v)[inv.reshape(-1)]


def _conifer_share(lat, lon, north=True):
    """The share of conifers among the trees: (35) the forests' leaf type around the point (leaftype.py: Copernicus
    Land Cover 100 m's Forest Type, 2019), else the coarse rule below. South of the tropics no less than the coarse
    rule: the map takes the radiata pine plantations of Chile, New Zealand and South Africa for evergreen
    broadleaves (Kaingaroa's 0.27, Biobio's 0.0). north: Europe's north's birches (NORTH_CONIFER_FACTOR); without it
    the maps' own share."""
    import leaftype
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    m = np.reshape(_on_grid(leaftype.conifer_share, lat, lon), lat.shape)
    coarse = _conifer_share_coarse(lat, lon)
    m = np.where(lat < -23.5, np.fmax(m, coarse), m)
    m = np.where(np.isnan(m), coarse, m)
    return m * north_conifer_factor(lat, lon) if north else m


# (49) Fennoscandia's and the Baltic's forests have more broadleaves (their birches) than the leaf type maps give: EU-Forest's
# plots 60-68 N 69-72 % conifers where the maps made 81-84 %, 68-70 N 39 % against 55 % (the user, 2026-11-19: "the
# birch 10 % in the game, 15 % in the data"). The conifer share there times this factor by latitude, Europe only
# (NORTH_CONIFER_LON: west of the Urals, where the inventories are; Siberia's taiga as it was).
NORTH_CONIFER_FACTOR = ((57.0, 1.0), (60.0, 0.86), (66.0, 0.86), (69.0, 0.72))
NORTH_CONIFER_LON = (-12.0, 40.0)


def north_conifer_factor(lat, lon):
    """Per point: the factor on the conifer share in Europe's north (NORTH_CONIFER_*), 1 elsewhere."""
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    f = dnp.interp(lat, [a for a, _ in NORTH_CONIFER_FACTOR], [b for _, b in NORTH_CONIFER_FACTOR])
    return np.where((lon >= NORTH_CONIFER_LON[0]) & (lon <= NORTH_CONIFER_LON[1]), f, 1.0)


CONIFER_TILT_H = 12.0           # (37) the taller trees more likely the conifers: a tree's weight its height over this,
CONIFER_TILT = (0.5, 1.2)       # within these


def conifer_odds(share, height, is_tree=None):
    """Per tree: the chance that it is a conifer, the local share tilted by its height (the tall ones more likely, the
    short ones the broadleaf scrub) with the cell's mean kept to the share: 1 - (1 - share) ** (w / mean w). (37:
    was share * w, which took a third of the trees off every conifer forest: Bory Tucholskie's 0.97 pines made 66 %
    conifers, Finland's 49 %.)"""
    share = np.clip(np.asarray(share, np.float64), 0.0, 1.0)
    w = np.clip(np.asarray(height, np.float64) / CONIFER_TILT_H, *CONIFER_TILT)
    wt = w if is_tree is None else w[np.asarray(is_tree, bool)]
    a = w / (float(np.mean(wt)) if wt.size else 1.0)
    return 1.0 - dnp.power(1.0 - share, a)


def _conifer_share_coarse(lat, lon):
    """The regional share of conifers (a coarse climate stand-in where the leaf type map has no forest). Beyond North
    America (2026-10-15: Douglas firs stood in Punjab's fields): few in the tropics and subtropics, the pines of the
    Mediterranean, the boreal north."""
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    west = lon < -100.0
    share = np.where(west, 0.7, 0.2)
    share = np.where(np.abs(lat) > 45.0, np.maximum(share, 0.6), share)
    alat = np.abs(lat)
    other = np.where(alat < 23.5, 0.02, np.where(alat < 35.0, 0.08, np.where(alat <= 45.0, 0.2, share)))
    mediterranean = (lat > 30.0) & (lat < 46.0) & (lon > -10.0) & (lon < 40.0)
    other = np.where(mediterranean, 0.3, other)
    return np.where(_north_america(lat, lon), share, other)


# Eurasia's species (29; the stock survey 2026-11-15: East Coast USA's "aspen" shapes wear the game's birch bark and
# leaves, /assets/materials/tree/birch): outside the Americas, the tropics and the southern hemisphere, the generic
# broadleaves become birches, oaks or beeches and the conifers Scots pines or spruces (Douglas firs stand in: no stock
# spruce), by the Koppen class (climate.py), the latitude and the height above the sea, in stands (stand_u) with a
# STAND_MIX share of single trees mixed in. Starting shares from the national inventories (Poland: pine first, oak
# 8.0 %, birch 6.8 %; the mountains spruce, fir, beech): to be tuned on screenshots.
EURASIA_BOX = (23.5, -25.0, 75.0, 180.0)        # south, west, north, east
STAND_CELL_M = 80.0         # a stand's size about (ground m); its edges ragged by STAND_JITTER of it per tree
STAND_JITTER = 0.35
STAND_MIX = 0.3             # this share of the trees picks its species on its own (a stand's admixture; 0.15 made
                            # one-species blocks, the user's test at Gorzyce 2026-11-17)
BROAD_STAND_MIX = 0.55      # (35) the broadleaves' species more mixed than the conifers' stands: an oak-hornbeam or
                            # beech wood is a mix tree by tree, its birches scattered pioneers, not blocks (the user's
                            # test at Syrynka 2026-11-18: an 80 m wall of birches); pines and spruces are planted in
                            # stands
# Koppen classes (Beck et al.) -> (birch, oak, beech) shares of the broadleaves
K_BOREAL = (19, 20, 23, 24, 27, 28, 29, 30)     # Dsc Dsd Dwc Dwd Dfc Dfd ET EF
K_CONTINENTAL = (17, 18, 21, 22, 25, 26)        # Dsa Dsb Dwa Dwb Dfa Dfb
K_OCEANIC = (12, 13, 15, 16)                    # Cwb Cwc Cfb Cfc
K_MED = (8, 9, 10)                              # Csa Csb Csc
K_SUBTROPICAL = (11, 14)                        # Cwa Cfa
K_COLD_ARID = (5, 7)                            # BWk BSk
K_HOT_ARID = (4, 6)                             # BWh BSh: the broadleaves Italy's (olives, holm oaks), not oaks
BOREAL_LAT = 55.0           # (a Dfc / ET class south of this is a mountain's: continental shares)
# (35) the continental climates' (Dfb, Dfa, Dwb...) by continental_shares, not one triple: the birches' share of the
# broadleaves rises eastwards (the national inventories: Germany ~10 %: birch 4.5 % of the forest of ~45 % broadleaves;
# Poland ~22 %: 6.8 % of ~31 %; Belarus and European Russia over half), lower in the forest steppe's south (Ukraine:
# oaks); the beeches (standing in for the hornbeams, limes and maples too: smooth grey bark, oval crowns) end at their
# range's eastern edge (Fagus sylvatica: eastern Poland, the Carpathians), the oaks take the rest. Was a flat 0.40
# birches: walls of them in Silesia's oak-hornbeam woods (the user's test at Syrynka, 2026-11-18).
CONT_BIRCH_LON = ((10.0, 0.10), (20.0, 0.20), (28.0, 0.45), (40.0, 0.60), (60.0, 0.60), (90.0, 0.45))
# (49) more of them in central Europe: EU-Forest's 48-52 N, birch 16 % of the broadleaves, the rule's 9 %
CONT_BIRCH_SOUTH = ((40.0, 0.1), (46.0, 0.45), (50.0, 0.85), (54.0, 1.0))  # (latitude, factor): fewer to the south
                                            # (Anatolia's, the Balkans', Ukraine's forest steppe: oaks; Ukraine's
                                            # birch ~8 % of a forest of ~57 % broadleaves)
NORTH_BIRCH_LAT = ((55.0, 0.30), (58.0, 0.65), (60.0, 0.85), (62.0, 0.95))
CONT_BEECH_LON = (20.0, 26.0)               # the beeches' part of the rest: CONT_BEECH up to the first, none east
CONT_BEECH = 0.45                           # of the second (the uphill gain, BEECH_UPHILL, still adds them)
BROAD_SHARES = {"boreal": (1.0, 0.0, 0.0), "continental": (0.20, 0.44, 0.36), "oceanic": (0.12, 0.48, 0.40),
                "med": (0.05, 0.95, 0.0), "subtropical": (0.05, 0.70, 0.25), "cold_arid": (0.30, 0.70, 0.0)}
# (36) the rest of the world's broadleaves by region (broad_shares; the stock shapes stand in: the beeches for the
# maples, limes, hornbeams, tulip trees; the oaks for the hickories, ashes, elms):
# - the oceanic climates: the birches' share by latitude (France ~5 %: birch 2-3 % of a forest of ~70 % broadleaves;
#   Great Britain ~18 %: the NFI's birch beside oak, ash, sycamore, beech; Norway's coast mostly birch)
OCEANIC_BIRCH_LAT = ((45.0, 0.05), (50.0, 0.10), (55.0, 0.20), (60.0, 0.40), (63.0, 0.75), (66.0, 0.95))
# - East Asia's temperate forests (east of EAST_ASIA_LON: Manchuria, Korea, the Russian Far East: Mongolian oak,
#   Asian white birch, maples, limes; Japan's cool temperate belt: Japanese beech first, Quercus crispula, birches)
EAST_ASIA_LON = 100.0
EAST_ASIA_BIRCH_LAT = ((35.0, 0.05), (40.0, 0.12), (45.0, 0.30), (50.0, 0.45))   # (Korea's and north China's oaks
EAST_ASIA_BEECH = 0.35                      # and pines in the south; Manchuria's birches in the north), the beeches
                                            # (maples, limes) this part of the rest
# - the cold steppes' (BSk, BWk) broadleaves: the birch groves (kolki) of the forest steppe in the north, the planted
#   poplars, elms and locusts (oaks stand in) of the south's towns and shelterbelts
COLD_ARID_BIRCH_LAT = ((40.0, 0.05), (46.0, 0.15), (52.0, 0.50))
JAPAN_BOX = (30.0, 128.5, 46.0, 146.5)      # south, west, north, east
JAPAN_SHARES = (0.15, 0.40, 0.45)
# - North America outside the dry West (the USFS FIA's eastern hardwoods: oak-hickory first, maples and beech, the
#   birches only in the north's northern hardwoods and the boreal aspen-birch): the birches' share by latitude, the
#   beeches (maples, beech, tulip tree, basswood) NA_BEECH of the rest
NA_BIRCH_LAT = ((32.0, 0.02), (38.0, 0.04), (42.0, 0.10), (46.0, 0.30), (50.0, 0.60), (54.0, 0.90))
NA_BEECH = 0.42
# (43) the spruce-shaped share of North America's generic conifers by latitude: the mid-Atlantic's and the
# Appalachians' conifers mostly pines (Virginia, pitch, shortleaf, eastern white pine) and hemlocks, the north's
# spruces and firs (Canada's NFI: 85 %)
NA_SPRUCE_LAT = ((38.0, 0.25), (42.0, 0.5), (45.0, 0.8), (48.0, 0.85))
NA_SPRUCE_LAKES = 0.65              # (Minnesota, Wisconsin, Michigan, Ontario's south: the red, white and jack pines)
GREAT_LAKES_BOX = (41.5, -97.5, 49.5, -78.0)    # south, west, north, east
# - elsewhere outside the tropics and Eurasia (the pampas', the Highveld's, the Middle East's planted and riverside
#   trees: oaks, planes, poplars, elms; birches hardly)
OTHER_SHARES = (0.03, 0.62, 0.35)
# (35) by the ecoregion where the Koppen class misleads: the Cantabrian and northwest Iberian oak woods (Galicia,
# northern Portugal, Asturias: Quercus robur and pyrenaica, birches; beeches only in the mountains, BEECH_UPHILL)
ECO_BROAD_SHARES = {648: (0.25, 0.75, 0.0), 800: (0.25, 0.75, 0.0)}
BEECH_UPHILL = 0.85         # the beeches' share gained over the beech belt (from the birches and oaks)
# (37) the beech belt (submontane and montane: the oak-hornbeam woods give way to beech, fir and sycamore; Bieszczady
# NP: beech ~60 % of the forest, oaks hardly any above 500 m) from BEECH_BELT_M[0] below the spruce line, whole at
# BEECH_BELT_M[1] below it (50 N: 334 to 734 m; 46 N, the Alps: 494 to 894; 36 N, Honshu: 894 to 1294). Was the
# montane belt (700 m below the line to the line) and 0.5: the Bieszczady at 700 m half oaks.
BEECH_BELT_M = (800.0, 400.0)
PINE_WOOD_SHARE = (0.5, 0.9)    # (37) the forest's conifer share over which its beeches become birches and oaks
PINE_WOOD_BIRCH = 0.6           # (of those: the rest oaks)
# spruces (of the conifers): SPRUCE_LAT's share by latitude in the lowlands, rising over the montane belt (MONTANE_M
# below the spruce line) to SPRUCE_MOUNTAIN halfway up it (the firs and spruces of the montane belt); the line
# SPRUCE_LINE_M at 50 N, SPRUCE_LINE_PER_DEG lower a degree north. The mountains' effect fades out north of
# MOUNTAIN_FADE (the boreal lowlands' own shares). Above the line SPRUCE_TAKES_BROAD of the broadleaves are spruces too
# (the upper montane belt: the Tatras, the Alps).
SPRUCE_LAT = ((45.0, 0.08), (50.0, 0.10), (55.0, 0.28), (60.0, 0.45), (65.0, 0.35), (70.0, 0.20))
# (36) west of SPRUCE_LON[0] (Germany, Czechia, Austria, Switzerland, Scandinavia: Norway spruce planted and native
# from the uplands down; the inventories: Germany's conifers ~46 % spruce, Czechia's ~70 %, Sweden's ~50 %) the
# spruces' share by SPRUCE_CENTRAL, east of SPRUCE_LON[1] by SPRUCE_LAT (Poland's, Belarus' and Russia's pine lands:
# Poland's conifers ~13 % spruce and fir)
SPRUCE_CENTRAL = ((45.0, 0.20), (48.0, 0.45), (52.0, 0.35), (55.0, 0.35), (60.0, 0.50), (65.0, 0.40), (70.0, 0.20))
SPRUCE_LON = (16.0, 21.0)
SPRUCE_PLAIN_M = (120.0, 350.0)     # (37) SPRUCE_CENTRAL's share from nothing at the first height to whole at the second
SPRUCE_PLAIN_LAT = 56.0
CONICAL_JAPAN = 0.7                 # (38) the conical (spruce-shaped) share of Japan's conifers at least
CONICAL_CHINA = 0.5                 # and of East Asia's south of CONICAL_CHINA_LAT
CONICAL_CHINA_LAT = 32.0
ATLANTIC_SPRUCE = 0.6               # (39) the spruces' share of the oceanic northwest's conifers at least
ATLANTIC_SPRUCE_LAT = 50.0
ATLANTIC_SPRUCE_LON = 13.0
SPRUCE_MOUNTAIN = 0.85
SPRUCE_LINE_M = 1100.0
SPRUCE_LINE_PER_DEG = 40.0
MONTANE_M = 700.0
SPRUCE_RAMP_M = 300.0
SPRUCE_TAKES_BROAD = 0.7
MOUNTAIN_FADE = (55.0, 60.0)
ELEV_ZOOM = 10              # the DEM's zoom for a tree's height above the sea (coarse: one tile serves a cell)
# dead trees: SNAG_SHARE of the forest trees (land cover tree, not in town) SNAG_H tall, outside the tropics
SNAG_SHARE = 0.015
SNAG_H = (6.0, 18.0)
SNAG_FROM = None            # (set below: the groups a dead tree may stand in for, broadleaf / conifer)
# warm towns' palms outside the tropics and North America (its own, earthfast.regional_groups): Koppen class ->
# (the share in town, outside town, the latitude they stop at); date and fan palms (Italy's, West Coast USA's)
DRY_PALMS = {8: (0.15, 0.0, 44.0), 4: (0.45, 0.15, 36.0), 6: (0.30, 0.03, 36.0), 14: (0.08, 0.0, 35.0)}
# (54) ... but by the sea and low: palms need mild winters, and a fan palm in a street is a coast's sight (Nice, Split),
# not a plateau's (Valensole, 550 m, 100 km inland: two of them by its car park, the user's 2026-11-24). Their share
# times palm_fit: the sea (Koppen class 0 on its 0.1 degree grid) within PALM_COAST_DEG (full) or PALM_INLAND_DEG
# (3/4), else PALM_INLAND_KEEP, outside the deserts' and steppes' classes (their oases: any distance); and full below
# PALM_ELEV_M[0], none from PALM_ELEV_M[1] up, linear between (the winters' mildness: the height decides most)
PALM_COAST_DEG = 0.2
PALM_INLAND_DEG = 0.4
PALM_ELEV_M = (200.0, 450.0)
PALM_INLAND_KEEP = 0.5      # (farther from the sea: Seville, Cordoba, Murcia, low and mild, have palms in plenty)
K_DRY_PALMS = (4, 6)             # BWh, BSh: the hot deserts' and steppes' palms are their oases', inland too
PALM_FROM = None
# the rainforests (Koppen Af, Am): every generic broadleaf a tropical tree (the shapes span 5-33 m since the stock
# survey), and fewer palms outside town (regional_groups' 20 % of the tropics' trees: RAINFOREST_PALM_KEEP of them)
K_RAINFOREST = (1, 2)
K_TROPICS = (1, 2, 3)             # Af Am Aw: the coconut palms' (34)
REDWOOD_MIN_M = 25.0
STEPPE_JUNIPER = 0.6
# (35) the juniper woodlands' ecoregions: Anatolia's steppes and Taurus, the Elburz, the Kopet Dag, eastern Iran,
# Baluchistan and Afghanistan, the Alai and western Tian Shan, Oman's Al-Hajar, the Asir, the High Atlas, Spain's
# Meseta (J. thurifera)
# (35) the world beyond Eurasia's and the dry West's rules, by RESOLVE's biomes and realms (ecoregion.py) and the
# forests' leaf type (leaftype.py), species_groups:
# - the tropics' broadleaves (the tropical biomes, and any land within 23.5 degrees outside the montane, temperate,
#   Mediterranean and desert ones): the rainforest's trees (G_TROPICAL; before, only Koppen Af / Am: the savanna
#   climates' woods were East Coast USA's oaks and beeches); the savannas' (biome 7) and the dry forests' and hot
#   deserts' open country (biomes 2, 13 within SAVANNA_LAT, off the Mediterranean) the umbrella-crowned acacias'
#   (G_SAVANNA: Jungle Rock Island's umbrella filler and ceiba, Italy's feathery scraggly trees, Johnson Valley's
#   mesquite / acacia bush); town trees there the rainforest's (mangoes, figs).
# - the conifers of the tropics and the southern hemisphere: pines (Caribbean, Mexican, chir, Benguet pines; Chile's,
#   New Zealand's, South Africa's and Australia's radiata and patula plantations): West Coast USA's Monterey pine
#   (Pinus radiata itself), not Douglas firs.
# - the subtropics' and the south's evergreen broadleaf (laurel) forests (southern China, Taiwan, southern Japan and
#   Korea, the Himalaya's foothills, Madeira; Valdivia, New Zealand): G_LAUREL (Italy's evergreen cork oaks, the
#   island's young and lianad trees) for the forest's evergreen broadleaf share (leaftype), at least ECO_LAUREL_SHARE
#   in the ECO_LAUREL ecoregions (the map reads Japan's and Korea's laurel woods as deciduous); not in Australia (its
#   evergreen broadleaves are eucalypts) or the Mediterranean (its own).
# - the south's temperate broadleaves (Chile, Argentina, New Zealand, Tasmania south of SOUTH_BEECH_LAT): southern
#   beeches (Nothofagus), East Coast USA's beeches.
# - the maritime and Mediterranean pines (Pinus pinaster, pinea, halepensis, brutia: Italy's maritime pines and, for
#   the tall forest grown ones, its Scots pine) for the conifers of the Mediterranean's lowland ecoregions and the
#   Atlantic's Cantabrian and northwest Iberian ones (Galicia, Portugal, the Landes) below MARITIME_PINE_M.
SAVANNA_LAT = 35.0
ECO_LAUREL = (233, 236, 251, 268, 283, 643, 657, 659, 668, 670, 680, 681, 682)
ECO_LAUREL_SHARE = 0.6
SOUTH_BEECH_LAT = -35.0
ECO_MARITIME_PINE = (648, 800)
MARITIME_PINE_M = 1000.0
ECO_JUNIPER = (59, 652, 694, 695, 721, 723, 727, 730, 756, 757, 758, 792, 804, 814, 816, 834)
# (35) the eucalypt plantations of Portugal and Galicia (Eucalyptus globulus; E. nitens higher up in Galicia): a
# quarter of both forests (Portugal's IFN6 2015: 845 000 ha of 3.2 M, its first species; Galicia's IFN4: about 30 %),
# most of them in the Atlantic lowlands north of the Tagus, few in the Alentejo's cork and holm oak montado or on the
# interior's uplands (E. globulus is frost-tender: rare above ~700 m). West Coast USA's eucalyptus shapes, whole stands
# (stand_u, no admixture: a plantation is one species) of the forest's trees (land cover tree, not in town).
EUC_BOX = (36.9, -9.6, 43.8, -6.9)          # south, west, north, east: Portugal and Galicia
EUC_SHARE = 0.45            # of the forest stands in the western lowlands north of EUC_SOUTH
EUC_INLAND = (-7.8, -6.9)   # the share falls to nothing from the first longitude to the second
EUC_SOUTH = ((38.5, 0.3), (39.5, 1.0))      # (latitude, the share's factor): the Alentejo's few
EUC_ELEV = (300.0, 700.0)   # the share falls to nothing between these heights above the sea (m)
EUC_FROM = None             # (set below: the groups a plantation replaces)


def redwood_belt(lat, lon):
    """The coast redwoods' fog belt (Big Sur to the Oregon line, ~40-60 km inland): a strip east of the coast, its
    edge from -122.0 at 37 N to -124.25 at 42 N. (35: kept over RESOLVE's Northern California coastal forests, which
    leave out Muir Woods' and Big Sur's canyons at 0.05 degrees.)"""
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    return (lat > 35.8) & (lat < 42.3) & (lon < -122.0 - 0.45 * (lat - 37.0)) & (lon > -125.0)
RAINFOREST_PALM_KEEP = 0.4
# (45) the tropics' shores: the coconut palms' belt behind the beach (Bali's, Goa's, Kerala's, Zanzibar's, the
# Caribbean's, Sri Lanka's). Before, a shore's trees were palms at the climate's share as anywhere (the regional
# kernel's 20 % off town, the rainforests' RAINFOREST_PALM_KEEP of them: Porto Seguro's beach 3 %, Phuket's 12 %, Bali's
# 24 %; dev 21-31 %). Within COAST_PALM_M of the sea's or a lake's shore (the water layer's polygons, not the rivers)
# in the tropics' climates (Af Am Aw), COAST_PALM_SHARE of the broadleaves palms (the rainforests' thinning not there);
# the casuarinas (pine_west), the mangroves' trees and the towns' (the kernel's 60 % already) kept.
COAST_PALM_M = 120.0
COAST_PALM_SHARE = 0.6
COAST_PALM_LAT = 28.0
COAST_PALM_FROM = None          # (set below)


def coast_palms(group, lat, lon, lcls, coast, u):
    """Per tree: the shore's broadleaves (coast: within COAST_PALM_M) in the tropics' climates palms at
    COAST_PALM_SHARE (u: a hashed [0, 1)). Pure."""
    import climate
    g = np.array(group, np.uint8, copy=True)
    lc = np.asarray(lcls)
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    cand = np.asarray(coast, bool) & _in(g, COAST_PALM_FROM) & (lc != WC["mangrove"]) & (lc != WC["built"]) \
        & (np.abs(lat) < COAST_PALM_LAT)
    if cand.any():
        k = climate.koppen_many(lat[cand], lon[cand])
        idx = np.flatnonzero(cand)[_in(k, K_TROPICS)]
        g[idx[np.asarray(u)[idx] < COAST_PALM_SHARE]] = G_PALM
    return g


def eucalypt_share(lat, lon, elev=None):
    """Per point: the share of the forest's stands that are eucalypt plantations (Portugal and Galicia, EUC_*)."""
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    z = np.zeros(len(lat)) if elev is None else np.asarray(elev, np.float64)
    inland = np.clip((EUC_INLAND[1] - lon) / (EUC_INLAND[1] - EUC_INLAND[0]), 0.0, 1.0)
    south = dnp.interp(lat, [a for a, _ in EUC_SOUTH], [b for _, b in EUC_SOUTH])
    up = np.clip((EUC_ELEV[1] - z) / (EUC_ELEV[1] - EUC_ELEV[0]), 0.0, 1.0)
    return np.where(_in_box(lat, lon, EUC_BOX), EUC_SHARE * inland * south * up, 0.0)


def stand_u(mx, my, gx, gy, lat, salt):
    """Per tree: a hashed [0, 1) shared by the trees of a stand (STAND_CELL_M squares of the Web Mercator grid at the
    ground length of lat, their edges ragged by a hashed jitter per tree). mx, my: Web Mercator m; gx, gy: the trees'
    global pixels (their own hashes). Pure: the same tree gets the same value from every cell build."""
    cx, cy = stand_cells(mx, my, gx, gy, lat, salt)
    return _hash01(cx, cy, salt)


STAND_SHARED_SALT = 50      # (42) the species' stands (salts 51, 61, 91) share one ragged grid (stand_cells)


def stand_cells(mx, my, gx, gy, lat, salt):
    """Per tree: its stand's (cx, cy) on STAND_CELL_M squares, the edges ragged by the tree's hashed jitter (salt + 1,
    salt + 2). (42) The species' stands share STAND_SHARED_SALT's (one grid, three hashes of it: 9 hashes a tile were
    5; their own values still independent)."""
    s = STAND_CELL_M * _merc_k(lat)
    j = STAND_JITTER * s
    cx = np.floor((np.asarray(mx, np.float64) + (_hash01(gx, gy, salt + 1) - 0.5) * j) / s).astype(np.int64)
    cy = np.floor((np.asarray(my, np.float64) + (_hash01(gx, gy, salt + 2) - 0.5) * j) / s).astype(np.int64)
    return cx, cy


def _mixed(stand, single, u_mix, mix=None):
    return np.where(u_mix < (STAND_MIX if mix is None else mix), single, stand)


def continental_shares(lat, lon):
    """Per tree: the continental climates' (birch, oak, beech) shares of the broadleaves (CONT_*), an (n, 3) array."""
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    birch = dnp.interp(lon, [a for a, _ in CONT_BIRCH_LON], [b for _, b in CONT_BIRCH_LON]) * \
        dnp.interp(lat, [a for a, _ in CONT_BIRCH_SOUTH], [b for _, b in CONT_BIRCH_SOUTH])
    # (39) the north's broadleaves birches whatever the longitude (EU-Forest's plots: southern Finland's 99 %, central
    # Sweden's 88 %, the Baltic states' 40 %)
    birch = np.maximum(birch, dnp.interp(lat, [a for a, _ in NORTH_BIRCH_LAT], [b for _, b in NORTH_BIRCH_LAT]))
    beech = CONT_BEECH * np.clip((CONT_BEECH_LON[1] - lon) / (CONT_BEECH_LON[1] - CONT_BEECH_LON[0]), 0.0, 1.0)
    rest = 1.0 - birch
    return np.stack([birch, rest * (1.0 - beech), rest * beech], axis=1)


def broad_shares(lat, lon, koppen, eco, montane=None, conifer=None):
    """Per tree: the (birch, oak, beech) shares of the broadleaves anywhere outside the tropics (the constants above:
    Eurasia's by its climates, East Asia's, Japan's, North America's, the rest's; ECO_BROAD_SHARES; more beeches
    uphill: montane, the beech belt's [0, 1] at each tree; fewer in the conifer forests: conifer, the forest's
    conifer share there), an (n, 3) array."""
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    k = np.asarray(koppen)
    n = len(lat)
    ea = eurasia(lat, lon)
    na = _north_america(lat, lon)
    sh = np.tile(np.array(OTHER_SHARES), (n, 1))
    sh[ea] = continental_shares(lat, lon)[ea]
    boreal = _in(k, K_BOREAL) & (lat >= BOREAL_LAT)
    oceanic = _in(k, K_OCEANIC)
    for name, m in (("oceanic", oceanic), ("med", _in(k, K_MED)), ("subtropical", _in(k, K_SUBTROPICAL)),
                    ("cold_arid", _in(k, K_COLD_ARID))):
        sh[ea & m] = BROAD_SHARES[name]
    ob = dnp.interp(lat, [a for a, _ in OCEANIC_BIRCH_LAT], [b for _, b in OCEANIC_BIRCH_LAT])
    m = ea & oceanic
    sh[m] = np.stack([ob, (1.0 - ob) * 0.5, (1.0 - ob) * 0.5], axis=1)[m]
    cb = dnp.interp(lat, [a for a, _ in COLD_ARID_BIRCH_LAT], [b for _, b in COLD_ARID_BIRCH_LAT])
    m = ea & _in(k, K_COLD_ARID)
    sh[m] = np.stack([cb, 1.0 - cb, np.zeros(n)], axis=1)[m]
    eb = dnp.interp(lat, [a for a, _ in EAST_ASIA_BIRCH_LAT], [b for _, b in EAST_ASIA_BIRCH_LAT])
    m = ea & (lon >= EAST_ASIA_LON) & ~_in(k, K_MED + K_COLD_ARID)
    sh[m] = np.stack([eb, (1.0 - eb) * (1.0 - EAST_ASIA_BEECH), (1.0 - eb) * EAST_ASIA_BEECH], axis=1)[m]
    sh[ea & _in_box(lat, lon, JAPAN_BOX)] = JAPAN_SHARES
    nb = dnp.interp(lat, [a for a, _ in NA_BIRCH_LAT], [b for _, b in NA_BIRCH_LAT])
    sh[na] = np.stack([nb, (1.0 - nb) * (1.0 - NA_BEECH), (1.0 - nb) * NA_BEECH], axis=1)[na]
    sh[boreal] = BROAD_SHARES["boreal"]
    for e_, share in ECO_BROAD_SHARES.items():
        sh[np.asarray(eco) == e_] = share
    if conifer is not None:
        # (37) the broadleaves of a pine or spruce forest (the lowlands' sands and bogs, the boreal and hemiboreal
        # forests: Bory Tucholskie, the Landes, Brandenburg, the Great Lakes' pinelands) its pioneers, birches and
        # aspens, and oaks: the beeches (maples, limes, hornbeams) want the richer soils of the mixed and broadleaf
        # woods. Not in the warm climates (the Mediterranean's and the subtropics' pine woods: oaks already).
        cold = _in(k, K_BOREAL + K_CONTINENTAL + K_OCEANIC + K_COLD_ARID)
        f = np.clip((np.asarray(conifer, np.float64) - PINE_WOOD_SHARE[0]) / (PINE_WOOD_SHARE[1] - PINE_WOOD_SHARE[0]),
                    0.0, 1.0) * cold
        if montane is not None:
            f = f * (1.0 - montane)          # (the mountains' spruce-fir-beech woods keep their beeches)
        moved = sh[:, 2] * f
        sh[:, 2] -= moved
        sh[:, 0] += moved * PINE_WOOD_BIRCH
        sh[:, 1] += moved * (1.0 - PINE_WOOD_BIRCH)
    if montane is not None:
        uphill = np.where(boreal, 0.0, montane)
        gain = np.minimum(BEECH_UPHILL * uphill, sh[:, 0] + sh[:, 1])
        rest = np.maximum(sh[:, 0] + sh[:, 1], 1e-9)
        sh[:, 0] -= gain * sh[:, 0] / rest
        sh[:, 1] -= gain * sh[:, 1] / rest
        sh[:, 2] += gain
    return sh


def eurasia(lat, lon):
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    return _in_box(lat, lon, EURASIA_BOX)


def spruce_line(lat):
    """The height above the sea (m) from which the conifers are mostly spruces, at latitude lat."""
    return np.clip(SPRUCE_LINE_M + (50.0 - np.asarray(lat, np.float64)) * SPRUCE_LINE_PER_DEG, 500.0, 1700.0)


def _sea_within(lat, lon, deg):
    """Per point: the sea (Koppen class 0) at the point or on the 8-direction rings of deg / 2 and deg. Pure."""
    import climate
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    sea = np.asarray(climate.koppen_many(lat, lon)) == 0
    for r in (0.5 * deg, deg):
        for dy, dx in _RING:
            rest = np.flatnonzero(~sea)
            if not len(rest):
                return sea
            sea[rest] = np.asarray(climate.koppen_many(lat[rest] + dy * r, lon[rest] + dx * r)) == 0
    return sea


def palm_fit(lat, lon, elev, k):
    """(54) Per point: the town palms' share factor 0..1 by the sea's nearness and the height (PALM_*); the hot
    deserts' and steppes' classes (K_DRY_PALMS) by the height only. elev None: as at the sea's level. Pure."""
    lat, lon = np.atleast_1d(np.asarray(lat, np.float64)), np.atleast_1d(np.asarray(lon, np.float64))
    n = len(lat)
    f = np.ones(n)
    dry = _in(np.atleast_1d(np.asarray(k)), K_DRY_PALMS)
    rest = np.flatnonzero(~dry)
    if len(rest):
        coast = np.asarray(_on_grid(lambda a, b: _sea_within(a, b, PALM_COAST_DEG), lat[rest], lon[rest]), bool)
        part = np.asarray(_on_grid(lambda a, b: _sea_within(a, b, PALM_INLAND_DEG), lat[rest], lon[rest]), bool)
        f[rest] = np.where(coast, 1.0, np.where(part, 0.75, PALM_INLAND_KEEP))
    if elev is not None:
        e = np.atleast_1d(np.asarray(elev, np.float64))
        lo, hi = PALM_ELEV_M
        f *= np.clip((hi - e) / (hi - lo), 0.0, 1.0)
    return f


def species_groups(group, height, lat, lon, lcls, koppen, elev, u_broad, u_conifer, u_snag, u_palm, u_alt=None,
                   u_stand=None):
    """The groups after regional(): Eurasia's species (birch / oak / beech, pine_eu / spruce), dead trees, the warm
    towns' palms and the rainforests' trees (the constants above). koppen: the class at each tree (climate.koppen_many);
    elev: its height above the sea (m) or None (no DEM: as in the lowlands); u_broad, u_conifer: the species choices'
    [0, 1) (stand_u, mixed); u_snag, u_palm, u_alt: hashed [0, 1) per tree (u_alt None: u_snag's); u_stand: a stand's
    [0, 1) for the eucalypt plantations and the laurel forests (stand_u, not mixed; None: neither). Pure."""
    g = np.array(group, np.uint8, copy=True)
    h = np.asarray(height, np.float64)
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    k = np.asarray(koppen)
    built = np.asarray(lcls) == WC["built"]
    ea = eurasia(lat, lon)
    eco = eco_med(lat, lon)
    z = np.zeros(len(g)) if elev is None else np.asarray(elev, np.float64)
    line = spruce_line(lat)
    fade = np.clip((MOUNTAIN_FADE[1] - lat) / (MOUNTAIN_FADE[1] - MOUNTAIN_FADE[0]), 0.0, 1.0)
    # (39) the dry climates' mountains (Iberia's, the Atlas', Anatolia's, Central Asia's: Csa Csb Csc, BSk BWk BSh BWh)
    # keep their pines (black, Scots, Aleppo, Brutia pines; EU-Forest: Spain's conifers 2 % firs) and their oaks: no
    # spruce or beech belt (before: a third of Spain's mountain conifers spruces, León's broadleaves 30 % beeches)
    # spruce belt; the beech belt kept in the Mediterranean's own beech mountains (the Apennines', Calabria's, the
    # Balkans': EU-Forest's Calabria 30 % beeches), not in ECO_NO_BEECH (Iberia's, the Maghreb's, Anatolia's)
    dry = _in(k, K_COLD_ARID + K_HOT_ARID)
    beech_fade = fade * ~(dry | (_in(k, K_MED) & _in(eco, ECO_NO_BEECH)))
    fade = fade * ~(dry | _in(k, K_MED))
    montane = np.clip((z - (line - MONTANE_M)) / MONTANE_M, 0.0, 1.0) * fade
    upper = np.clip((z - line) / SPRUCE_RAMP_M, 0.0, 1.0) * fade
    ua = np.asarray(u_snag if u_alt is None else u_alt)
    # the upper montane belt's broadleaves: most of them spruces
    g[ea & (g == G_DECIDUOUS) & (ua < SPRUCE_TAKES_BROAD * upper)] = G_CONIFER
    # (39) Europe's genera where ForestPaths' map has a say (treegenus.py): the conifers' pines against spruces, firs and
    # larches, the broadleaves' beeches and oaks from it, its other broadleaves by the rules below
    import treegenus
    tg = _on_grid(treegenus.shares, lat, lon) if ea.any() else np.full((len(g), 7), np.nan)
    con = ea & (g == G_CONIFER)
    if con.any():
        east = dnp.interp(lat, [a for a, _ in SPRUCE_LAT], [b for _, b in SPRUCE_LAT])
        west = dnp.interp(lat, [a for a, _ in SPRUCE_CENTRAL], [b for _, b in SPRUCE_CENTRAL])
        t = np.clip((lon - SPRUCE_LON[0]) / (SPRUCE_LON[1] - SPRUCE_LON[0]), 0.0, 1.0)
        # (37) central Europe's spruces are its uplands' (Bavaria, Bohemia, the Harz): its plain's conifers
        # (Brandenburg's, Pomerania's, the Netherlands') pines, as Poland's; north of SPRUCE_PLAIN_LAT the lowland
        # spruces are the boreal forest's own
        upland = np.clip((z - SPRUCE_PLAIN_M[0]) / (SPRUCE_PLAIN_M[1] - SPRUCE_PLAIN_M[0]), 0.0, 1.0)
        upland = np.where(lat >= SPRUCE_PLAIN_LAT, 1.0, upland)
        base = east + (west - east) * (1.0 - t) * upland
        # (38) East Asia's warm temperate conifers are mostly conical ones (Japan's sugi and hinoki plantations, ~40 %
        # of its forest, against its red and black pines; southern China's and Taiwan's Chinese fir beside the Masson
        # pine): the spruces' shapes (Douglas firs) for them, not the Scots pines'
        base = np.where(_in_box(lat, lon, JAPAN_BOX), np.maximum(base, CONICAL_JAPAN), base)
        # (39) the Atlantic's oceanic north (Ireland, Great Britain, Denmark, the Low Countries): its conifers the
        # planted Sitka and Norway spruces first (EU-Forest's plots: Ireland's conifers 77 % spruces, Scotland's
        # 64 %, Denmark's 67 %, England's 51 %, the Netherlands' 37 %)
        atl = _in(k, K_OCEANIC) & (lat > ATLANTIC_SPRUCE_LAT) & (lon < ATLANTIC_SPRUCE_LON)
        base = np.where(atl, np.maximum(base, ATLANTIC_SPRUCE), base)
        base = np.where((lon >= EAST_ASIA_LON) & (lat < CONICAL_CHINA_LAT) & ~_in_box(lat, lon, JAPAN_BOX),
                        np.maximum(base, CONICAL_CHINA), base)
        p = base + (SPRUCE_MOUNTAIN - base) * np.clip(2.0 * montane, 0.0, 1.0)      # (no pines from mid-belt up)
        needle = tg[:, [treegenus.LARIX, treegenus.PICEA, treegenus.PINUS, treegenus.OTHER_NEEDLE]].sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            p_tg = (tg[:, treegenus.LARIX] + tg[:, treegenus.PICEA] + tg[:, treegenus.OTHER_NEEDLE]) / needle
        p = np.where(~np.isnan(p_tg) & (needle > TREEGENUS_MIN), p_tg, p)
        g[con] = np.where(np.asarray(u_conifer)[con] < p[con], G_SPRUCE, G_PINE_EU)
    # (43) North America's remaining generic conifers (the regional kernel's dry West, Pacific coast and Southeast pines
    # aside: the boreal forest, the Rockies' north, the Great Lakes, New England): spruces' shapes for its spruces,
    # firs, hemlocks, Douglas firs, cedars and larches, the pines' for its jack, lodgepole, red and white pines (Canada's
    # NFI 2020, growing stock: spruce 44.3 %, pine 11.7 %, fir 8.3 %, hemlock 6.2 %, Douglas fir 3.5 %, cedar and other
    # 3.0 %, larch 1.5 %: pines 15 % of the conifers; Maine: spruce/fir 33 % of the forest, pines 7 %); the Great Lakes'
    # sands more pines (NA_SPRUCE_LAKES), the mid-Atlantic's mostly pines (NA_SPRUCE_LAT). Before, all of them Douglas firs (the generic conifer).
    nac = _north_america(lat, lon) & (g == G_CONIFER)
    if nac.any():
        p = dnp.interp(lat, [a for a, _ in NA_SPRUCE_LAT], [b for _, b in NA_SPRUCE_LAT])
        p = np.where(_in_box(lat, lon, GREAT_LAKES_BOX), np.minimum(p, NA_SPRUCE_LAKES), p)
        g[nac] = np.where(np.asarray(u_conifer)[nac] < p[nac], G_SPRUCE, G_PINE_EU)
    # broadleaves: Italy's in the hot deserts and steppes; birch / oak / beech by the climate, more beeches uphill
    g[ea & (g == G_DECIDUOUS) & _in(k, K_HOT_ARID)] = G_MEDITERRANEAN
    # (36) everywhere outside the tropics (before, Eurasia only: elsewhere the generic group, a quarter of whose
    # shapes are birches, chose by height alone)
    dec = (g == G_DECIDUOUS) & (np.abs(lat) >= 23.5)
    if dec.any():
        beech_up = np.clip((z - (line - BEECH_BELT_M[0])) / (BEECH_BELT_M[0] - BEECH_BELT_M[1]), 0.0, 1.0) * beech_fade
        sh = broad_shares(lat, lon, k, eco, beech_up, _conifer_share(lat, lon))
        broad = tg[:, [treegenus.FAGUS, treegenus.QUERCUS, treegenus.OTHER_BROAD]].sum(axis=1)
        ok = ~np.isnan(broad) & (broad > TREEGENUS_MIN)
        if ok.any():
            with np.errstate(invalid="ignore", divide="ignore"):
                f, q, o = (tg[:, i] / broad for i in (treegenus.FAGUS, treegenus.QUERCUS, treegenus.OTHER_BROAD))
                r = sh / np.maximum(sh.sum(axis=1), 1e-9)[:, None]
            sh_tg = np.stack([o * r[:, 0], q + o * r[:, 1], f + o * r[:, 2]], axis=1)
            sh = np.where(ok[:, None], sh_tg, sh)
        u = np.asarray(u_broad)
        pick = np.where(u < sh[:, 0], G_BIRCH, np.where(u < sh[:, 0] + sh[:, 1], G_OAK, G_BEECH)).astype(np.uint8)
        g[dec] = pick[dec]
    # the rainforests: no temperate broadleaves, fewer palms in the forest
    rain = _in(k, K_RAINFOREST) & (np.abs(lat) < 23.5)
    g[rain & (g == G_DECIDUOUS)] = G_TROPICAL
    # (30) a mangrove's trees (WorldCover 95, any climate: the Red Sea's, the Indus delta's) tropical broadleaves
    mangrove = np.asarray(lcls) == WC["mangrove"]
    g[mangrove & _in(g, (G_DECIDUOUS, G_MEDITERRANEAN, G_PALM, G_OAK, G_BIRCH, G_BEECH))] = G_TROPICAL
    sparse = rain & (g == G_PALM) & ~built & (np.asarray(u_palm) >= RAINFOREST_PALM_KEEP)
    g[sparse] = G_TROPICAL
    # warm towns' date and fan palms
    na = (lat > 24.0) & (lon > -170.0) & (lon < -50.0)
    cand = ~na & (np.abs(lat) >= 23.5) & _in(g, PALM_FROM)
    if cand.any():
        up = np.asarray(u_palm)
        fit = np.zeros(len(g))
        ci = np.flatnonzero(cand & _in(k, tuple(DRY_PALMS)))
        if len(ci):             # (54: the sea near and low, palm_fit)
            fit[ci] = palm_fit(lat[ci], lon[ci], None if elev is None else np.asarray(elev)[ci], k[ci])
        for kk, (p_town, p_out, lat_max) in DRY_PALMS.items():
            m = cand & (k == kk) & (np.abs(lat) < lat_max)
            g[m & (up < np.where(built, p_town, p_out) * fit)] = G_PALM_DRY
    # (34) coconut-type palms only in the tropics' climates (Af, Am, Aw): any other palm (California's, Florida's north,
    # the Mediterranean's, the Gulf's) the fan and date palms (the gallery 2026-11-17: the palm group mixed both)
    g[(g == G_PALM) & ~_in(k, K_TROPICS)] = G_PALM_DRY
    # (34) the Old World's juniper woodlands' open country (35: their ecoregions, ECO_JUNIPER; before, any BSk / BWk
    # outside North America, which put junipers on the Kazakh and Mongolian steppes): its low trees junipers
    # (Juniperus excelsa / polycarpos / seravschanica / thurifera / procera; Utah's shapes, as regional_groups does in
    # the dry West), STEPPE_JUNIPER of them
    c_ = np.asarray(lcls)
    steppe = _in(eco, ECO_JUNIPER) & _in(c_, (WC["shrub"], WC["grass"], WC["bare"])) & (h < 9.0)
    g[steppe & _in(g, (G_DECIDUOUS, G_OAK, G_BIRCH, G_CONIFER, G_PINE_EU, G_SPRUCE))
      & (np.asarray(u_palm) < STEPPE_JUNIPER)] = G_JUNIPER
    # (34) the coast redwoods only in their fog belt (the gallery 2026-11-17: pine_west's tallest were redwoods, drawn
    # for any 25 m+ conifer from Washington to the Southeast's loblolly pines)
    g[(g == G_PINE_WEST) & redwood_belt(lat, lon) & (h >= REDWOOD_MIN_M)] = G_REDWOOD
    g = _world_groups(g, lat, lon, c_, eco, z, u_stand)
    # (35) Portugal's and Galicia's eucalypt plantations: whole stands of the forest's trees
    if u_stand is not None:
        wood = (np.asarray(lcls) == WC["tree"]) & _in(g, EUC_FROM) & _in(k, K_MED + K_OCEANIC)
        if wood.any():
            g[wood & (np.asarray(u_stand) < eucalypt_share(lat, lon, elev))] = G_EUCALYPTUS
    # dead trees in the forests outside the tropics
    forest = (np.asarray(lcls) == WC["tree"]) & (np.abs(lat) >= 23.5) & (h >= SNAG_H[0]) & (h <= SNAG_H[1])
    snag = forest & (np.asarray(u_snag) < SNAG_SHARE)
    broad, conif = SNAG_FROM
    g[snag & _in(g, broad)] = G_SNAG_BROAD
    g[snag & _in(g, conif)] = G_SNAG_CONIFER
    return g


BROAD_FROM = (G_DECIDUOUS, G_OAK, G_BIRCH, G_BEECH)
CONIFER_FROM = (G_CONIFER, G_PINE_EU, G_SPRUCE)


def _world_groups(g, lat, lon, lcls, eco, elev, u_stand):
    """species_groups' biome rules (35; the constants by SAVANNA_LAT): the tropics' rainforest and savanna trees,
    the tropics' and the south's pines, the laurel forests, the south's beeches, the maritime pines. g changed in
    place and returned."""
    import ecoregion as er
    import leaftype
    bio, realm = er.biome_of(eco), er.realm_of(eco)
    alat = np.abs(lat)
    med = _in(eco, ECO_MED)
    laurel_eco = _in(eco, ECO_LAUREL)
    built = lcls == WC["built"]
    hot = ~laurel_eco & (_in(bio, (er.B_TROP_MOIST, er.B_TROP_DRY, er.B_TROP_GRASS, er.B_MANGROVE))
                         | ((alat < 23.5) & ~_in(bio, (er.B_TROP_CONIFER, er.B_TEMP_BROAD, er.B_TEMP_CONIFER,
                                                          er.B_MONTANE_GRASS, er.B_MEDITERRANEAN, er.B_DESERT,
                                                          er.B_TEMP_GRASS))))
    broad = _in(g, BROAD_FROM) | ((g == G_MEDITERRANEAN) & ~med)
    # savannas and the dry country's open land: acacias (also of the trees regional_groups made the tropics' own);
    # the rest of the tropics' broadleaves: the rainforest's
    # (the hot deserts' woods too: the Kalahari's camelthorns, the Sahel's; not the oases' and the Nile's, which
    # Eurasia's rule made Italy's: olives, carobs)
    desert = (bio == er.B_DESERT) & (alat < SAVANNA_LAT) & ~med
    open_dry = _in(bio, (er.B_TROP_DRY, er.B_DESERT)) & (lcls != WC["tree"]) & (alat < SAVANNA_LAT) & ~med
    acacia_from = _in(g, BROAD_FROM + (G_TROPICAL,))
    savanna = (~built & (lcls != WC["mangrove"])
               & (((acacia_from | broad) & (((bio == er.B_TROP_GRASS) & hot) | open_dry)) | (acacia_from & desert)))
    g[savanna] = G_SAVANNA
    g[broad & ~savanna & hot] = G_TROPICAL
    # the conifers of the tropics, the hot deserts and the south: pines
    g[_in(g, CONIFER_FROM) & (hot | desert | (bio == er.B_TROP_CONIFER) | (lat < -23.5))] = G_PINE_WEST
    # the laurel forests
    australia = (realm == er.R_AUSTRALASIA) & (lon > 110.0) & (lon < 155.0)
    if u_stand is not None:
        cand = _in(g, BROAD_FROM) & ~hot & ~med & ~australia & (alat < 45.0)
        if cand.any():
            evb = np.nan_to_num(_on_grid(leaftype.evergreen_broad_share, lat, lon), nan=0.0)
            share = np.maximum(evb, np.where(laurel_eco, ECO_LAUREL_SHARE, 0.0))
            g[cand & (np.asarray(u_stand) < share)] = G_LAUREL
    # the south's beeches (Nothofagus)
    south = (lat < SOUTH_BEECH_LAT) & ((realm == er.R_NEOTROPIC) | ((realm == er.R_AUSTRALASIA)
                                                                   & ((lon > 160.0) | (lat < -39.5))))
    g[south & _in(g, BROAD_FROM)] = G_BEECH
    # (38) Australia's other broadleaves (the regional kernel's eucalypts take most): its forests' and woodlands' are
    # eucalypts too (mountain ash, messmate, stringybarks, boxes; the wattles below them), not East Coast USA's oaks
    # and beeches (the reality check at the Victorian Alps' foothills: 11 %); the towns' planted oaks and planes kept
    g[australia & ~built & ~south & _in(g, BROAD_FROM)] = G_EUCALYPTUS
    # the maritime and Mediterranean pines
    g[_in(g, CONIFER_FROM) & (med | _in(eco, ECO_MARITIME_PINE)) & (elev < MARITIME_PINE_M)] = G_PINE_MARITIME
    return g


COAST_PALM_FROM = (G_DECIDUOUS, G_TROPICAL, G_SAVANNA, G_LAUREL, G_MEDITERRANEAN, G_OAK, G_BEECH, G_BIRCH)
EUC_FROM = (G_DECIDUOUS, G_CONIFER, G_MEDITERRANEAN, G_BIRCH, G_OAK, G_BEECH, G_PINE_EU, G_SPRUCE, G_PINE_MARITIME)
SNAG_FROM = ((G_DECIDUOUS, G_BIRCH, G_OAK, G_BEECH, G_OAK_WEST), (G_CONIFER, G_PINE_EU, G_SPRUCE, G_PINE_WEST))
OPEN_FROM = {G_DECIDUOUS: G_DECIDUOUS_OPEN, G_OAK: G_DECIDUOUS_OPEN, G_BIRCH: G_BIRCH_OPEN, G_BEECH: G_BEECH_OPEN,
             G_PINE_WEST: G_PINE_WEST_OPEN, G_MEDITERRANEAN: G_MEDITERRANEAN_OPEN, G_TROPICAL: G_TROPICAL_OPEN,
             G_CONIFER: G_CONIFER_OPEN, G_SPRUCE: G_SPRUCE_OPEN, G_PINE_EU: G_PINE_EU_OPEN,
             G_SNAG_BROAD: G_DECIDUOUS_OPEN, G_SNAG_CONIFER: G_CONIFER_OPEN}


# (55) The veteran oaks (the user, 2026-11-24: "the veteran should be there but rare inside loose forests"): East
# Coast USA's tree_oak_large_a, a broad old oak with a flared, buttressed trunk, out since 2026-11-20 (scaled to a 20 m
# forest tree its trunk was a 3-4 m boulder of bark, and it stood in roadside rows of clones). Now its own group, for
# VETERAN_SHARE of the broadleaf woods' oaks and broadleaves VETERAN_H tall (near its own 13.2 m: the trunk ~2-3 m
# across, an old oak's) where the canopy is loose (under VETERAN_COVER of the pixels within VETERAN_WIN_M are canopy:
# an old oak grew up in the open, a wood pasture's, a forest edge's or a glade's), in the temperate latitudes, never
# in the open (by a road, in town: their _open twins are not taken).
VETERAN_SHARE = 0.03
VETERAN_H = (11.0, 17.0)
VETERAN_COVER = 0.6
VETERAN_WIN_M = 15.0
VETERAN_LAT = (30.0, 62.0)


def _cover_at(m, r, jj, ii):
    """The share of m's pixels in the (2r+1)^2 window around the pixels (jj, ii), the window clipped at the grid's
    edges. Pure, exact (integer sums)."""
    m = np.asarray(m)
    n_r, n_c = m.shape
    c = np.zeros((n_r + 1, n_c + 1), np.int64)
    c[1:, 1:] = m.astype(np.int64).cumsum(0).cumsum(1)
    jj, ii = np.asarray(jj, np.int64), np.asarray(ii, np.int64)
    r0, r1 = np.clip(jj - r, 0, n_r), np.clip(jj + r + 1, 0, n_r)
    c0, c1 = np.clip(ii - r, 0, n_c), np.clip(ii + r + 1, 0, n_c)
    s = c[r1, c1] - c[r0, c1] - c[r1, c0] + c[r0, c0]
    return s / np.maximum(1, (r1 - r0) * (c1 - c0))


def veteran_oaks(group, height, lat, lcls, cover, u):
    """group with the veteran oaks (VETERAN_*) in G_OAK_VETERAN: the forest's (land cover tree) oaks and broadleaves
    VETERAN_H tall where the canopy cover is under VETERAN_COVER, u (hashed [0, 1) per tree) under VETERAN_SHARE.
    Pure."""
    g = np.array(group, np.uint8, copy=True)
    h, a = np.asarray(height), np.abs(np.asarray(lat))
    v = _in(g, (G_OAK, G_DECIDUOUS)) & (np.asarray(lcls) == WC["tree"]) & (h >= VETERAN_H[0]) & (h <= VETERAN_H[1])         & (a >= VETERAN_LAT[0]) & (a <= VETERAN_LAT[1]) & (np.asarray(cover) < VETERAN_COVER)         & (np.asarray(u) < VETERAN_SHARE)
    g[v] = G_OAK_VETERAN
    return g


def open_groups(group, open_):
    """group with the trees in the open (per item, open_) in their OPEN_FROM twins. Pure."""
    g = np.array(group, np.uint8, copy=True)
    o = np.asarray(open_, bool)
    for a, b in OPEN_FROM.items():
        g[o & (g == a)] = b
    return g
PALM_FROM = (G_DECIDUOUS, G_BIRCH, G_OAK, G_BEECH, G_MEDITERRANEAN)

# The bushes by the climate (30; the stock survey 2026-11-16): the same bushes in the same places (the canopy's 1-3 m
# tops, the scatter, the forest edges, the field hedges), their shapes from the climate at each one (Koppen, climate.py)
# instead of East Coast USA's broadleaf bushes everywhere:
# - Cs (summer-dry): maquis and garrigue (Italy's cork and holm oak bushes, its scraggly and fluffy bushes, Utah's
#   junipers and low grey bushes for the rosemary / thyme scrub); in North America (California's chaparral of scrub
#   oaks, chamise and manzanita, the same look) CHAPARRAL_SHARE of them, the rest the coastal sage scrub's grey desert
#   bushes as regional_groups made them (West Coast USA's own trees_shrub are creosote bushes: the creosote group's);
# - the north (Koppen ET / EF anywhere, the boreal D classes from BOREAL_SHRUB_LAT) and the tundra's moss and lichen:
#   birch and willow scrub (the aspen / birch bushes), spruce saplings (the fir bushes), junipers;
# - the rainforests, the mangroves and TROPICAL_SAVANNA_SHARE of the savannas' (Aw): Jungle Rock Island's bushes and
#   wide-leaved plants;
# - the deserts and steppes (B classes): the desert bushes wherever the land cover is (regional_groups made them only
#   on shrubland or bare ground below 42 degrees and in the dry West);
# - the forest floor (the understory scatter under the canopy): FERN_SHARE of its bushes ferns in the humid temperate
#   and boreal climates.
# Dead wood (logs, fallen trees, branches, stumps) is not one of the bushes but added (_remote_floor): one on
# DEADWOOD_P of a DEADWOOD_CELL_M grid's squares inside the forest, only DEADWOOD_ROAD_M or more from any road, with no
# built-up land within DEADWOOD_BUILT_M and on level ground (the DEM's slope under DEADWOOD_MAX_SLOPE: no log on an
# embankment) (remote woods: a fallen beech lay across a road, the user's test 2026-11-16); not in the deserts. In
# the tropics (Af, Am, Aw) the same places get a vine-draped thicket instead (Jungle Rock Island's tro_bush_2_small,
# a 9 m dome of aerial roots: it stood by a road like a hut, the user's test 2026-11-16).
K_HUMID = (12, 13, 14, 15, 16, 17, 18, 21, 22, 25, 26, 27, 28)     # Cwb Cwc Cfa Cfb Cfc Dsa Dsb Dwa Dwb Dfa Dfb Dfc Dfd
K_POLAR = (29, 30)                                                  # ET EF
K_ARID = (4, 5, 6, 7)                                               # BWh BWk BSh BSk
K_SAVANNA = (3,)                                                    # Aw / As
BOREAL_SHRUB_LAT = 50.0
CHAPARRAL_SHARE = 0.7
TROPICAL_SAVANNA_SHARE = 0.5
FERN_SHARE = {"humid": 0.35, "boreal": 0.15}
DEADWOOD_CELL_M = 20.0
DEADWOOD_P = 0.1            # (2.5 a hectare of remote forest)
DEADWOOD_ROAD_M = 40.0      # ground m (a fallen beech is 10 m long)
DEADWOOD_BUILT_M = 150.0
DEADWOOD_H = (1.0, 3.0)     # m: a dead-wood item's height drawn in this range (the shapes' 1.0-3.0 m, M.pick's)
DEADWOOD_MAX_SLOPE = 0.12   # rise over run (7 degrees)
THICKET_H = (5.5, 7.5)
# (48) the tropics' remote floor: this share of its items the vine thicket, the rest dead wood (fallen palm fronds and
# branches, now and then a fallen giant: Jungle Rock Island's, unused before; the user, 2026-11-19: "use as many of the
# available assets as possible"), DEADWOOD_H tall as the temperate dead wood
THICKET_SHARE = 0.5
# (55) and TRO_GIANT_SHARE of the thickets Jungle Rock Island's giant filler (tro_tree_2_filler_04: a 24 m dome of aerial
# roots and vines, 27 m across; the user, 2026-11-24: "only far away from roads"), TRO_GIANT_ROAD_M or more from any road
TRO_GIANT_SHARE = 0.25
TRO_GIANT_H = (22.0, 26.0)
TRO_GIANT_ROAD_M = 80.0
# Trees in the open (OPEN_ROAD_M from a road's edge, or on built-up land): no forest-interior shapes there (the
# forest trees and fillers by their names, *_forest*, *filler*: tall, thin, sparse; East Coast USA's mossy oaks,
# tree_oak_large_*: moss on a trunk by an asphalt road, the user's test 2026-11-16, and a veteran's huge gnarled
# trunk in a roadside row of clones, 2026-11-17; trees.lua M.FOREST_ONLY). An oak in the open takes the open
# broadleaves (deciduous_open: oak_open was left two young oaks).
# and no dead trees (taken down by the road): the group's _open twin (OPEN_FROM above; the mod's catalog without
# those shapes).
OPEN_ROAD_M = 15.0
FERN_MAX_M = 1.6            # (ferns only for the low bushes)
SHRUB_FROM = (G_SHRUB, G_DESERT_SHRUB)


def shrub_groups(group, height, lat, lon, lcls, koppen, u, u_floor, understory=None):
    """The bushes' groups by the climate (the constants above): group's G_SHRUB / G_DESERT_SHRUB items (the rest as
    they are) become maquis, northern, tropical or desert bushes; understory (per item: a forest floor
    bush, the scatter under the canopy; None: none) ferns. koppen: the class at each item (climate.koppen_many);
    u, u_floor: hashed [0, 1) per item. (groups, heights; the heights as given). Pure."""
    g = np.array(group, np.uint8, copy=True)
    h = np.array(height, np.float32, copy=True)
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    k = np.asarray(koppen)
    c = np.asarray(lcls)
    u, uf = np.asarray(u, np.float64), np.asarray(u_floor, np.float64)
    bush = _in(g, SHRUB_FROM)
    if not bush.any():
        return g, h
    na = (lat > 24.0) & (lon > -170.0) & (lon < -50.0)
    alat = np.abs(lat)
    # (35) the maquis where RESOLVE's Mediterranean biome is (the Mediterranean's, California's chaparral, Chile's
    # matorral, the Cape's fynbos, Australia's mallee; before, Koppen Cs: Galicia's and the Pacific Northwest's
    # summer-dry oceanic woods and the Central Asian mountains' Cs pockets too)
    import ecoregion
    med = ecoregion.biome_of(eco_med(lat, lon)) == ecoregion.B_MEDITERRANEAN
    north = (_in(k, K_POLAR) | (_in(k, K_BOREAL) & (alat >= BOREAL_SHRUB_LAT)) | (c == WC["moss"])) & ~med
    tropical = (_in(k, K_RAINFOREST) | (c == WC["mangrove"])
                | (_in(k, K_SAVANNA) & (u < TROPICAL_SAVANNA_SHARE))) & (alat < 30.0)
    arid = _in(k, K_ARID) & ~tropical
    g[bush & med & (~na | (u < CHAPARRAL_SHARE))] = G_MAQUIS
    g[bush & north] = G_SHRUB_NORTH
    g[bush & tropical] = G_SHRUB_TROPICAL
    g[bush & arid & (g == G_SHRUB)] = G_DESERT_SHRUB
    # (34) the hot deserts and steppes (BWh, BSh): no junipers (Utah's: the cold deserts', the Great Basin's, Central
    # Asia's and Iran's mountains'), desert_shrub_hot (the mod's desert_shrub without them; the gallery 2026-11-17)
    g[(g == G_DESERT_SHRUB) & _in(k, (4, 6))] = G_DESERT_SHRUB_HOT
    if understory is not None:
        # (34) the tropics' forest floor: the wide-leaved shade plants (monstera, the island's tropical_plant_*), not
        # in the open (a monstera on a savanna or a field's hedge; the gallery 2026-11-17)
        g[bush & tropical & np.asarray(understory, bool)] = G_TRO_UNDERSTORY
        floor = bush & np.asarray(understory, bool) & ~tropical & ~arid
        humid = np.isin(k, K_HUMID) & ~north & ~med     # (46: a Mediterranean coast's Cfb pixel: no ferns)
        p = np.where(humid, FERN_SHARE["humid"], np.where(north & ~np.isin(k, K_POLAR), FERN_SHARE["boreal"], 0.0))
        fern = floor & (uf < p) & (h <= FERN_MAX_M)
        g[fern] = G_FERN
    return g, h


def climate_shrubs(it, lcls, understory=None):
    """Items it with its bushes' groups by the climate (shrub_groups; the Koppen class from climate.koppen_many at
    each, the hashes from its position, so the same bush gets the same shape from every build). lcls: the land cover
    class per item; understory: per item, a forest floor bush (None: none)."""
    if not len(it) or not np.isin(it.group, SHRUB_FROM).any():
        return it
    import climate
    qx = np.rint(np.asarray(it.lon, np.float64) * 1e6).astype(np.int64)
    qy = np.rint(np.asarray(it.lat, np.float64) * 1e6).astype(np.int64)
    g, h = shrub_groups(it.group, it.height, it.lat, it.lon, lcls, climate.koppen_many(it.lat, it.lon),
                        _hash01(qx, qy, 71), _hash01(qx, qy, 72), understory)
    return Items(it.lon, it.lat, h, it.radius, it.kind, g, it.rank, it.yaw, it.scale)


def _coarse_near(m, r_px, b=8):
    """(mask, b): m reduced to b x b pixel blocks (any set) and grown by r_px pixels rounded up to blocks: a pixel
    (r, c) is within r_px of a set pixel of m (or a little farther) where mask[r // b, c // b]."""
    H, W = m.shape
    h, w = -(-H // b), -(-W // b)
    p = np.zeros((h * b, w * b), bool)
    p[:H, :W] = m
    return _dilate(p.reshape(h, b, w, b).any(axis=(1, 3)), int(math.ceil(r_px / b))), b


class Vegetation:
    def __init__(self, md, cache_dir=None):
        self.md = md
        self.dem = None                 # (set by the server: rocks on bare ground by slope, ROCK_SLOPE)
        self.surface = self.widths = None   # (set by the server: the roads' drawn widths, roads.road_specs)
        self.dir = os.path.join(cache_dir, "vegetation", f"v{VERSION}") if cache_dir else None
        self._mem = OrderedDict()
        self._lock = threading.Lock()
        self._cell_locks = {}
        self._tl = threading.local()    # per build thread: .dem_failed (the DEM could not answer: _load keeps no cell)
        self._warm_at, self._warm_thread = None, None   # warm_around's latest (lat, lon, radius_m), for the warm thread
        self._warm_cv = threading.Condition()
        self._on_disk = set()           # cells whose file ready() found (the warm thread's: each file checked once)
        self._soon = OrderedDict()      # build_soon's cells (x, y), oldest first, at most BUILD_SOON_MAX

    def cell(self, key):
        """The Items of map-data cell key ((z, x, y) or CellKey)."""
        k = (int(key[1]), int(key[2]))
        with self._lock:
            if k in self._mem:
                self._mem.move_to_end(k)
                return self._mem[k]
            lock = self._cell_locks.setdefault(k, threading.Lock())
        with lock:
            with self._lock:
                if k in self._mem:
                    return self._mem[k]
            items = self._load(*k)
            with self._lock:
                self._mem[k] = items
                while len(self._mem) > MEM_CELLS:
                    self._mem.popitem(last=False)
            return items

    def warm_around(self, lat, lon, radius_m=WARM_M):
        """Build the cells within radius_m of (lat, lon) in the background (nearest first): the tiles
        near a teleport's destination then find them ready instead of waiting ~3.5 s each. The event loop's, at every
        pose: only the place is kept here, the warm thread picks the cells (their file checks, up to 64 a pose, held
        the loop on a busy hard drive: GitHub #1054)."""
        with self._warm_cv:
            self._warm_at = (lat, lon, radius_m)
            if self._warm_thread is None:
                self._warm_thread = threading.Thread(target=self._warm_loop, daemon=True, name="veg-warm")
                self._warm_thread.start()
            self._warm_cv.notify()

    def build_soon(self, key):
        """Build cell key in the background: the warm thread, after the cells round the player (warm_around), behind
        the pacer like them. The generated ground's (genground._ask_trees): a block's photo waits for its cells, which
        the pose's warm-up may never reach. Bounded: BUILD_SOON_MAX, the oldest dropped (asked again after its
        backoff). True when queued (False: built already)."""
        if self.ready(key):
            return False
        k = (int(key[1]), int(key[2]))
        with self._warm_cv:
            if k not in self._soon:
                self._soon[k] = None
                while len(self._soon) > BUILD_SOON_MAX:
                    self._soon.popitem(last=False)
            if self._warm_thread is None:
                self._warm_thread = threading.Thread(target=self._warm_loop, daemon=True, name="veg-warm")
                self._warm_thread.start()
            self._warm_cv.notify()
        return True

    def _warm_cells(self, lat, lon, radius_m):
        """The cells within radius_m of (lat, lon) not built yet, nearest first (the warm thread's: file checks)."""
        d = radius_m / 111320.0
        dl = d / max(dmath.cos(math.radians(lat)), 0.01)
        try:
            keys = mapcells.cells_for_bbox(lat - d, lon - dl, lat + d, lon + dl, max_cells=64)
        except ValueError:
            return []
        keys = sorted(keys, key=lambda k: dnp.sq(k.center()[0] - lat) + dnp.sq((k.center()[1] - lon) * dmath.cos(math.radians(lat))))
        return [(int(k.x), int(k.y)) for k in keys if not self.ready(k)]

    def _warm_loop(self):
        cells = []
        while True:
            pacer.hold()                # (a teleport's first block first)
            with self._warm_cv:
                while not cells and self._warm_at is None and not self._soon:
                    self._warm_cv.wait()
                at, self._warm_at = self._warm_at, None
            if at is not None:          # a newer place: its nearest cells first
                cells = self._warm_cells(*at)
            if cells:
                x, y = cells.pop(0)
            else:                       # (then build_soon's, oldest first)
                with self._warm_cv:
                    if not self._soon:
                        continue
                    (x, y), _ = self._soon.popitem(last=False)
            try:
                if self.ready((mapcells.ZOOM, x, y)):
                    continue            # (built before: on disk)
                self.cell((mapcells.ZOOM, x, y))
            except Exception as e:
                import netfetch
                if isinstance(e, netfetch.HostUnavailable):
                    # (a host's circuit breaker open, or offline: expected, and the cell is built when it is asked
                    # for; a traceback a cell was the first week's third most frequent error, ~5.5k)
                    log.debug("vegetation: warming cell %s skipped: %s", (x, y), e)
                else:
                    log.exception("vegetation: building cell %s", (x, y))

    def ready(self, key):
        k = (int(key[1]), int(key[2]))
        with self._lock:
            if k in self._mem:
                return True
        if k in self._on_disk:
            return True
        p = self._path(*k)
        if p and os.path.exists(p):
            self._on_disk.add(k)        # (the warm thread asks at every new place: each cell's file checked once)
            return True
        return False

    def _path(self, x, y):
        rel = getattr(self.md, "release", "x")
        return os.path.join(self.dir, str(rel), str(x), f"{y}.npz") if self.dir else None

    def _load(self, x, y):
        p = self._path(x, y)
        fields = ("lon", "lat", "height", "radius", "kind", "group", "rank", "yaw", "scale")
        if p and os.path.exists(p):
            # (a damaged file, empty or cut short after a crash: removed and built again, staging.read_npz)
            got = staging.read_npz(p, lambda f: Items(*(f[n] for n in fields)), "trees cell")
            if got is not None:
                return got
        self._tl.dem_failed = False
        items = self._build(x, y)
        # (a cell built while the DEM could not answer, offline or no tile, lacks its slope rocks: it serves this
        # session but is not kept, so the next one builds it whole. From cristivasile's PR #295. No DEM at all,
        # self.dem None, is a setting, not a failure)
        if p and not self._tl.dem_failed:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as fh:
                np.savez_compressed(fh, **{n: getattr(items, n) for n in fields})
            staging.replace_cached(tmp, p)
        return items

    # --- the build ------------------------------------------------------------------------------
    def _specs(self, f):
        """roads.road_specs of features f as the roads are drawn (the server's land cover and measured widths)."""
        import roads as roadsmod
        try:
            return roadsmod.road_specs(f, getattr(self.surface, "urban", None), self.widths)
        except Exception as e:          # (the class widths alone)
            log.debug("vegetation: road specs: %s: %s", type(e).__name__, e)
            return [None] * len(f.kind)

    def _build(self, x, y):
        key = (mapcells.ZOOM, x, y)
        cd = self.md.cell(key, layers=("canopy", "landcover", "buildings", "roads", "water"))
        chm = cd.layers.get("canopy")
        lc = cd.layers.get("landcover")
        if chm is None:
            return _empty()
        h = np.where(chm.data == (chm.nodata if chm.nodata is not None else 255), 0, chm.data).astype(np.float32)
        # pixel centres (EPSG:3857) of the CHM window, cut to the cell (the window may be larger)
        n_r, n_c = h.shape
        mx = chm.x0 + (np.arange(n_c) + 0.5) * chm.dx
        my = chm.y0 - (np.arange(n_r) + 0.5) * chm.dy
        lon0, lat0, lon1, lat1 = cd.bounds
        ex0, ey0 = _to3857(lon0, lat0)
        ex1, ey1 = _to3857(lon1, lat1)
        ci = (mx >= ex0) & (mx < ex1)
        rj = (my > ey0) & (my <= ey1)
        if not ci.any() or not rj.any():
            return _empty()
        # the blocked pixels: buildings, roads, water (on the CHM grid, with the neighbours' features)
        blocked = np.zeros(h.shape, bool)
        road = np.zeros(h.shape, bool)
        curb = np.zeros(h.shape, bool)      # the roads themselves (+ TREE_PIT_M): where a street tree may not land
        push = np.zeros((3,) + h.shape, np.float32)
        push[2] = np.inf
        deck = np.zeros(h.shape, bool)      # bridge decks (+ DECK_CLEAR_M): no tree on, beside or moved off one
        lon0_, lat0_, lon1_, lat1_ = cd.bounds
        near_water = np.zeros(h.shape, bool) if _in_box((lat0_ + lat1_) / 2, (lon0_ + lon1_) / 2, RIPARIAN_BOX) \
            else None                       # (within RIPARIAN_M of a river, lake or shore: riverside poplars)
        near_coast = np.zeros(h.shape, bool) if abs((lat0_ + lat1_) / 2) < COAST_PALM_LAT else None    # (45)
        road_cells = []
        clump_roads = []                    # (the carriageways the far clumps keep off: clumps)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                kk = (mapcells.ZOOM, x + dx, y + dy)
                try:
                    c2 = cd if (dx, dy) == (0, 0) else self.md.cell(kk, layers=("buildings", "roads", "water"))
                except Exception as e:          # a neighbour not at hand: its features unmasked
                    log.debug("vegetation: neighbour %s: %s", kk, e)
                    continue
                _mask_polys(c2.layers.get("buildings"), blocked, chm, 1.0)
                _mask_polys(c2.layers.get("water"), blocked, chm, 0.0, kind=3)
                _mask_roads(c2.layers.get("roads"), road, chm, specs=self._specs, walks=True)
                _mask_roads(c2.layers.get("roads"), curb, chm, push, clear=TREE_PIT_M, specs=self._specs)
                _mask_decks(c2.layers.get("roads"), deck, chm, self._specs)
                got = _road_segs(c2.layers.get("roads"), h.shape, chm, CLUMP_ROAD_MARGIN_DEFAULT_M, self._specs,
                                 margins=CLUMP_ROAD_MARGIN_M)
                if got is not None:
                    clump_roads.append(got)
                if near_water is not None:
                    _mask_near(c2.layers.get("water"), near_water, chm, RIPARIAN_M)
                if near_coast is not None:
                    _mask_near(c2.layers.get("water"), near_coast, chm, COAST_PALM_M, kinds=(3,))
                road_cells.append(c2.layers.get("roads"))
        # the rounded corners of curbless junctions (roadcorners.py): no tree, bush or rock on their asphalt
        _mask_corners(road_cells, (road, curb), chm, cd.bounds, getattr(self.surface, "urban", None), self.widths)
        # smoothed heights (3 x 3 mean), then the local maxima by height band
        p = np.pad(h, 1, mode="edge")
        hs = sum(p[a:a + n_r, b:b + n_c] for a in range(3) for b in range(3)) / 9.0
        tops = np.zeros(h.shape, bool)
        k = _merc_k((lat0 + lat1) / 2)     # (the radii below in pixels at MERC_REF_LAT: scaled to the ground's)
        for lo_m, hi_m, r in ((BUSH_MIN_M, TREE_MIN_M, 1), (TREE_MIN_M, 8.0, 2), (8.0, 15.0, 3), (15.0, 1e9, 4)):
            band = (hs >= lo_m) & (hs < hi_m)
            if band.any():
                tops |= band & (hs >= _shift_max(hs, max(1, int(round(r * k)))))
        tops &= ~blocked
        tops[~rj, :] = False
        tops[:, ~ci] = False
        # street trees: a top over a road corridor (its crown overhangs the street) moves just out of it,
        # onto the verge / sidewalk by the curb, unless that lands on a building, water or another road
        tops &= ~deck                       # (over a deck: the crown of a tree below it, or none; not moved off it
        blocked |= deck                     # onto the river or the bank under the deck, East Pratt Street 2026-10-31)
        on_road = tops & road
        tops &= ~road
        rj_, ri_ = np.nonzero(on_road)
        if len(rj_):
            nc = np.rint(ri_ + push[0, rj_, ri_]).astype(np.int64)
            nr = np.rint(rj_ + push[1, rj_, ri_]).astype(np.int64)
            ok = (nc >= 0) & (nc < n_c) & (nr >= 0) & (nr < n_r)
            ok[ok] &= ~blocked[nr[ok], nc[ok]] & ~curb[nr[ok], nc[ok]]
            moved = np.zeros(h.shape, bool)
            moved[nr[ok], nc[ok]] = True
            moved[~rj, :] = False
            moved[:, ~ci] = False
            hs = hs.copy()
            hs[nr[ok], nc[ok]] = np.maximum(hs[nr[ok], nc[ok]], hs[rj_[ok], ri_[ok]])     # keep its height
            tops |= moved
        blocked |= road
        jj, ii = np.nonzero(tops)
        # one top per crown (flat crowns had several); a crown's radius in pixels as at MERC_REF_LAT (chm.dx / k), and
        # equal heights (a canopy raster upsampled into flat plateaus) ranked by a hash of the global pixel, not by
        # position: the position order left the tops of a plateau on a regular lattice, rows seen from afar
        gx0 = np.floor((mx[ii] + 20037508.342789244) / chm.dx).astype(np.int64)
        gy0 = np.floor((20037508.342789244 - my[jj]) / chm.dy).astype(np.int64)
        tie = (_hash01(gx0, gy0, 29) * 1e-3).astype(np.float32)
        # (50) thinned as dev (CROWN_SPACING: dev's count of trees), then the tall trees' trunks at least TRUNK_MIN_M
        # apart (_thin_trunks: only where they stood closer, the shorter one goes; none moved)
        jj, ii = _thin(jj, ii, hs[jj, ii] + tie, chm.dx, k)
        valid = (hs >= TRUNK_TALL_M) & ~blocked        # (where a tall top may stand: as the tops above)
        valid[~rj, :] = False
        valid[:, ~ci] = False
        jj, ii = _thin_trunks(jj, ii, hs, valid, chm.dx, (lat0 + lat1) / 2.0, mx, my)
        th = hs[jj, ii]
        tlon, tlat = _to4326(mx[ii], my[jj])
        is_tree = th >= TREE_MIN_M
        # the landcover class at each top (rocks / bushes scatter below)
        lcls = lc.sample(tlon, tlat) if lc is not None else np.zeros(len(tlon), np.uint8)
        is_tree &= lcls != WC["water"]
        # canopy heights on bare ground or permanent snow with no vegetated land around: the canopy map misreading
        # bright ground (the Bonneville Salt Flats had ~19k "trees" up to 21 m tall on bare salt, 2026-10-20). By
        # its definition bare land has under 10 % plant cover; an edge tree, or one by an oasis, has some within
        # BARREN_NEAR_M and stays
        barren = np.flatnonzero((lcls == WC["bare"]) | (lcls == WC["snow"])) if lc is not None else []
        if len(barren):
            k = max(0.05, float(dnp.cos(np.radians(tlat[barren[0]]))))
            dm = BARREN_NEAR_M / k
            near = np.zeros(len(barren), bool)
            for ox, oy in ((dm, 0.0), (-dm, 0.0), (0.0, dm), (0.0, -dm)):
                lo2, la2 = _to4326(mx[ii[barren]] + ox, my[jj[barren]] + oy)
                near |= ~np.isin(lc.sample(lo2, la2), (WC["bare"], WC["snow"], WC["water"]))
            keep = np.ones(len(jj), bool)
            keep[barren[~near]] = False
            jj, ii, th, tlon, tlat, lcls, is_tree = jj[keep], ii[keep], th[keep], tlon[keep], tlat[keep], lcls[keep], \
                is_tree[keep]
        # hashed choices keyed on the global pixel (z17 Web-Mercator index: the CHM grid)
        gx = np.floor((mx[ii] + 20037508.342789244) / chm.dx).astype(np.int64)
        gy = np.floor((20037508.342789244 - my[jj]) / chm.dy).astype(np.int64)
        u1, u2, u3, u4 = (_hash01(gx, gy, s) for s in (1, 2, 3, 4))
        # (29) in Eurasia the conifers and broadleaves in stands (stand_u), each tree's own hash elsewhere
        tmx, tmy, clat = mx[ii], my[jj], (lat0 + lat1) / 2
        u_mix = _hash01(gx, gy, 31)
        ea = eurasia(tlat, tlon)
        if ea.any():
            u1 = np.where(ea, _mixed(stand_u(tmx, tmy, gx, gy, clat, 41), u1, u_mix), u1)
        conifer = u1 < conifer_odds(_conifer_share(tlat, tlon), th, is_tree)
        group = np.where(is_tree, np.where(conifer, G_CONIFER, G_DECIDUOUS), G_SHRUB).astype(np.uint8)
        group = regional(group, th, tlat, tlon, lcls, _hash01(gx, gy, 7),
                         riparian=None if near_water is None else near_water[jj, ii])
        group = self._species(group, th, tlat, tlon, lcls, tmx, tmy, gx, gy, clat, u_mix)
        if near_coast is not None and near_coast.any():
            group = coast_palms(group, tlat, tlon, lcls, near_coast[jj, ii], _hash01(gx, gy, 36))
        # (31) the trees in the open: by a road (OPEN_ROAD_M from its edge) or on built-up land
        open_ = lcls == WC["built"]
        if curb.any():
            sec = 1.0 / max(0.05, dmath.cos(dmath.radians(clat)))
            near = _dilate(curb, max(1, int(round(OPEN_ROAD_M * sec / chm.dx))))
            open_ |= near[jj, ii]
        group = open_groups(group, open_)
        # (55) a rare veteran oak inside loose broadleaf woods (veteran_oaks: never in the open, the twins above)
        vsec = 1.0 / max(0.05, dmath.cos(dmath.radians(clat)))
        cover = _cover_at(hs >= TREE_MIN_M, max(1, int(round(VETERAN_WIN_M * vsec / chm.dx))), jj, ii)
        group = veteran_oaks(group, th, tlat, lcls, cover, _hash01(gx, gy, 39))
        kind = np.where(is_tree, KIND_TREE, KIND_BUSH).astype(np.uint8)
        jitter = chm.dx * 0.5
        tlon2, tlat2 = _to4326(mx[ii] + (u3 - 0.5) * jitter, my[jj] + (u4 - 0.5) * jitter)
        tree = Items(tlon2, tlat2, th.astype(np.float32), (0.12 * th + 1.0).astype(np.float32), kind, group,
                     _hash01(gx, gy, 5).astype(np.float32), (u2 * 360.0).astype(np.float32),
                     (0.85 + 0.3 * _hash01(gx, gy, 6)).astype(np.float32))
        tree = climate_shrubs(tree, lcls)     # (30: the canopy's 1-3 m tops by the climate too)
        # squares, pedestrian areas and parking lots: no scattered bushes or rocks (a real tree there stays).
        # An orchard keeps its planted rows and loses the canopy's trees inside it.
        paved = np.zeros(h.shape, bool)
        orchard = np.zeros(h.shape, bool)
        vines = np.zeros(h.shape, bool)
        lu = None
        try:
            cl = self.md.cell((mapcells.ZOOM, x, y), layers=("land_use", "infrastructure"))
            lu, inf = cl.layers.get("land_use"), cl.layers.get("infrastructure")
            if lu is not None and len(lu) and lu.attrs.get("subtype") is not None:
                _mask_polys(lu, paved, chm, 0.0, select=lu.attrs["subtype"].astype(str) == "pedestrian")
            if inf is not None and len(inf) and inf.attrs.get("class") is not None:
                _mask_polys(inf, paved, chm, 0.0,
                            select=np.isin(inf.attrs["class"].astype(str), ("parking", "bus_station")))   # (a bus station's yard: no bushes)
            if lu is not None and len(lu) and lu.attrs.get("class") is not None:
                lclass = lu.attrs["class"].astype(str)
                _mask_polys(lu, orchard, chm, 0.0, select=lclass == "orchard")
                _mask_polys(lu, vines, chm, 0.0, select=lclass == "vineyard")
        except Exception as e:                  # not at hand: scattered as before
            log.debug("vegetation: land use of %s: %s", (x, y), e)
        if orchard.any():
            keep = _keep_off_mask(tree, orchard, chm)
            tree, lcls = tree.take(keep), lcls[keep]
        if vines.any():             # (the vines' own canopy read as bushes all over a vineyard: its rows stand there)
            keep = _keep_off_mask(tree, vines, chm) | (tree.height >= VINE_TREE_M)
            tree, lcls = tree.take(keep), lcls[keep]
        # (a clump moved off a road stays in this cell and off its buildings, water and decks: ClumpRoads)
        tree = clumps(tree, lcls, (lat0 + lat1) / 2,
                      roads=ClumpRoads(clump_roads, chm, blocked, (ex0, ey0, ex1, ey1)))
        scatter = self._scatter(cd, lc, blocked | paved | orchard | vines, chm, hs)
        dead = self._remote_floor(cd, lc, blocked | paved | orchard | vines, road, chm, hs)
        edge = self._edge(cd, lc, blocked | orchard | vines, road, chm, hs)
        bounds = (lon0, lat0, lon1, lat1)
        planted = orchard_items(lu, chm, blocked, bounds)
        hedges = farm_foliage(lu, chm, blocked, bounds)
        rows = vineyard_items(lu, chm, blocked, bounds)
        bales = bale_items(lu, chm, blocked, bounds)
        return _concat_all([tree, scatter, dead, edge, planted, hedges, rows, bales])

    def _edge(self, cd, lc, blocked, road, chm, hs):
        """The thicket along forest edges beside roads (EDGE_*), outside the dry West."""
        if lc is None or not road.any():
            return _empty()
        lon0, lat0, lon1, lat1 = cd.bounds
        if _dry_west(np.array([(lat0 + lat1) / 2]), np.array([(lon0 + lon1) / 2]))[0]:
            return _empty()
        band = _dilate(road, max(1, int(round(EDGE_M / chm.dx)))) & ~blocked
        if not band.any():
            return _empty()
        ex0, ey0 = _to3857(lon0, lat0)
        ex1, ey1 = _to3857(lon1, lat1)
        s = EDGE_CELL_M * _merc_k((lat0 + lat1) / 2)     # (in ground metres as at MERC_REF_LAT)
        GI, GJ = np.meshgrid(np.arange(math.floor(ex0 / s), math.ceil(ex1 / s)),
                             np.arange(math.floor(ey0 / s), math.ceil(ey1 / s)))
        GI, GJ = GI.ravel(), GJ.ravel()
        px = (GI + _hash01(GI, GJ, 21)) * s
        py = (GJ + _hash01(GI, GJ, 22)) * s
        c_ = np.floor((px - chm.x0) / chm.dx).astype(np.int64)
        r_ = np.floor((chm.y0 - py) / chm.dy).astype(np.int64)
        ok = (px >= ex0) & (px < ex1) & (py >= ey0) & (py < ey1) & (c_ >= 0) & (c_ < hs.shape[1]) \
            & (r_ >= 0) & (r_ < hs.shape[0])
        ok[ok] &= band[r_[ok], c_[ok]] & (hs[r_[ok], c_[ok]] >= TREE_MIN_M)     # under the forest's canopy
        ok &= _hash01(GI, GJ, 23) < EDGE_P
        if not ok.any():
            return _empty()
        GI, GJ, px, py = GI[ok], GJ[ok], px[ok], py[ok]
        lon, lat = _to4326(px, py)
        cls = lc.sample(lon, lat)
        forest = cls == WC["tree"]          # not under street trees in town
        if not forest.any():
            return _empty()
        GI, GJ, lon, lat, cls = GI[forest], GJ[forest], lon[forest], lat[forest], cls[forest]
        size = 1.2 + 1.8 * _hash01(GI, GJ, 24)
        n = len(lon)
        return climate_shrubs(Items(lon, lat, size.astype(np.float32), (size * 0.6).astype(np.float32),
                                    np.full(n, KIND_BUSH, np.uint8),
                                    regional(np.full(n, G_SHRUB, np.uint8), size, lat, lon, cls, _hash01(GI, GJ, 25)),
                                    _hash01(GI, GJ, 26).astype(np.float32),
                                    (_hash01(GI, GJ, 27) * 360.0).astype(np.float32),
                                    (0.8 + 0.4 * _hash01(GI, GJ, 28)).astype(np.float32)), cls)

    def _remote_floor(self, cd, lc, blocked, road, chm, hs):
        """Dead wood (the tropics: thickets) in remote forest on level ground (DEADWOOD_*): added items, not bushes
        changed."""
        if lc is None:
            return _empty()
        lon0, lat0, lon1, lat1 = cd.bounds
        clat = (lat0 + lat1) / 2
        ex0, ey0 = _to3857(lon0, lat0)
        ex1, ey1 = _to3857(lon1, lat1)
        sec = 1.0 / max(0.05, dmath.cos(dmath.radians(clat)))      # (Web Mercator m per ground m)
        s = DEADWOOD_CELL_M * sec
        GI, GJ = np.meshgrid(np.arange(math.floor(ex0 / s), math.ceil(ex1 / s)),
                             np.arange(math.floor(ey0 / s), math.ceil(ey1 / s)))
        GI, GJ = GI.ravel(), GJ.ravel()
        px = (GI + _hash01(GI, GJ, 81)) * s
        py = (GJ + _hash01(GI, GJ, 82)) * s
        c_ = np.floor((px - chm.x0) / chm.dx).astype(np.int64)
        r_ = np.floor((chm.y0 - py) / chm.dy).astype(np.int64)
        ok = (px >= ex0) & (px < ex1) & (py >= ey0) & (py < ey1) & (c_ >= 0) & (c_ < hs.shape[1]) \
            & (r_ >= 0) & (r_ < hs.shape[0]) & (_hash01(GI, GJ, 83) < DEADWOOD_P)
        ok[ok] &= ~blocked[r_[ok], c_[ok]] & (hs[r_[ok], c_[ok]] >= TREE_MIN_M)      # under the forest's canopy
        if ok.any() and road.any():
            near, b = _coarse_near(road, DEADWOOD_ROAD_M * sec / chm.dx)
            ok[ok] &= ~near[r_[ok] // b, c_[ok] // b]
        if not ok.any():
            return _empty()
        GI, GJ, px, py = GI[ok], GJ[ok], px[ok], py[ok]
        lon, lat = _to4326(px, py)
        keep = lc.sample(lon, lat) == WC["tree"]
        d = DEADWOOD_BUILT_M * sec          # no built-up land around: 8 points at d, 4 at d / 2
        for r, step in ((d, 45), (d / 2, 90)):
            for a in range(0, 360, step):
                if not keep.any():
                    return _empty()
                lo2, la2 = _to4326(px + r * dmath.cos(dmath.radians(a)), py + r * dmath.sin(dmath.radians(a)))
                keep &= lc.sample(lo2, la2) != WC["built"]
        import climate
        k = climate.koppen_many(lat, lon)
        keep &= ~np.isin(k, K_ARID)
        if keep.any() and self.dem is not None:       # level ground only (no DEM: as it is)
            sl = self._slope(px[keep], py[keep])
            if sl is not None:
                keep[np.flatnonzero(keep)[sl >= DEADWOOD_MAX_SLOPE]] = False
        if not keep.any():
            return _empty()
        GI, GJ, lon, lat, k = GI[keep], GJ[keep], lon[keep], lat[keep], k[keep]
        px, py = px[keep], py[keep]
        n = len(lon)
        tro = np.isin(k, K_RAINFOREST + K_SAVANNA) & (np.abs(lat) < 23.5)
        thicket = tro & (_hash01(GI, GJ, 88) < THICKET_SHARE)
        giant = thicket & (_hash01(GI, GJ, 89) < TRO_GIANT_SHARE)
        if giant.any() and road.any():
            near, b = _coarse_near(road, TRO_GIANT_ROAD_M * sec / chm.dx)
            gc = np.clip(np.floor((px - chm.x0) / chm.dx).astype(np.int64), 0, road.shape[1] - 1)
            gr = np.clip(np.floor((chm.y0 - py) / chm.dy).astype(np.int64), 0, road.shape[0] - 1)
            giant &= ~near[gr // b, gc // b]
        thicket &= ~giant
        lo_h = np.where(giant, TRO_GIANT_H[0], np.where(thicket, THICKET_H[0], DEADWOOD_H[0]))
        hi_h = np.where(giant, TRO_GIANT_H[1], np.where(thicket, THICKET_H[1], DEADWOOD_H[1]))
        h = (lo_h + (hi_h - lo_h) * _hash01(GI, GJ, 84)).astype(np.float32)
        group = np.where(giant, G_TRO_GIANT,
                         np.where(thicket, G_TRO_THICKET, np.where(tro, G_DEADWOOD_TROPICAL, G_DEADWOOD)))
        r_m = np.where(giant, 0.5 * h, np.where(thicket, 0.6 * h, 1.5 * h))
        return Items(lon, lat, h, r_m.astype(np.float32), np.where(giant, KIND_TREE, KIND_BUSH).astype(np.uint8),
                     group.astype(np.uint8), _hash01(GI, GJ, 85).astype(np.float32),
                     (_hash01(GI, GJ, 86) * 360.0).astype(np.float32),
                     (0.85 + 0.3 * _hash01(GI, GJ, 87)).astype(np.float32))

    def _elev(self, lat, lon):
        """Heights above the sea (m) at the points from the DEM at ELEV_ZOOM; None without a DEM or when it cannot say
        (then the cell is not kept, as for the rocks' slopes)."""
        if self.dem is None or not len(lat):
            return None
        try:
            return np.asarray(self.dem.sample(lat, lon, ELEV_ZOOM), np.float64).reshape(len(lat))
        except Exception as e:
            log.debug("vegetation: no heights above the sea: %s", e)
            self._tl.dem_failed = True
            return None

    def _species(self, group, th, tlat, tlon, lcls, tmx, tmy, gx, gy, clat, u_mix):
        """species_groups for a cell's canopy tops (their DEM heights only where Eurasia's conifers or broadleaves
        need them)."""
        if not len(group):
            return group
        import climate
        k = climate.koppen_many(tlat, tlon)
        need = eurasia(tlat, tlon) & np.isin(group, (G_CONIFER, G_DECIDUOUS))
        need |= _north_america(tlat, tlon) & (group == G_DECIDUOUS)      # (36) the Appalachians' beeches uphill
        need |= _in_box(tlat, tlon, EUC_BOX) & np.isin(group, EUC_FROM)      # (35) the plantations' uplands
        elev = None
        if need.any():
            e = self._elev(tlat[need], tlon[need])
            if e is not None:
                elev = np.zeros(len(group))
                elev[need] = e
        u_single = _hash01(gx, gy, 32)
        cx, cy = stand_cells(tmx, tmy, gx, gy, clat, STAND_SHARED_SALT)
        u_broad = _mixed(_hash01(cx, cy, 51), u_single, u_mix, BROAD_STAND_MIX)
        u_con = _mixed(_hash01(cx, cy, 61), u_single, u_mix)
        return species_groups(group, th, tlat, tlon, lcls, k, elev, u_broad, u_con, _hash01(gx, gy, 33),
                              _hash01(gx, gy, 34), _hash01(gx, gy, 35),
                              u_stand=_hash01(cx, cy, 91))

    def _slope(self, px, py):
        """The ground's slope (rise over run) at Web Mercator points (px, py), from the DEM ROCK_SLOPE_D m either side
        in x and y; None when the DEM cannot say."""
        k = dnp.cos(np.radians(_to4326(px[:1], py[:1])[1][0]))       # Web Mercator m per ground m (one cell)
        d = ROCK_SLOPE_D / max(k, 0.05)
        x = np.concatenate([px + d, px - d, px, px])
        y = np.concatenate([py, py, py + d, py - d])
        lon, lat = _to4326(x, y)
        try:
            z = np.asarray(self.dem.sample(lat, lon, ROCK_SLOPE_ZOOM), np.float64).reshape(4, len(px))
        except Exception as e:          # (offline, no tile): rocks as without a DEM
            log.debug("vegetation: no slope: %s", e)
            self._tl.dem_failed = True
            return None
        return dnp.hypot(z[0] - z[1], z[2] - z[3]) / (2 * ROCK_SLOPE_D)

    def _scatter(self, cd, lc, blocked, chm, hs):
        """Bushes and rocks on a hashed SCATTER_CELL_M grid over shrubland, grass and bare ground."""
        if lc is None:
            return _empty()
        lon0, lat0, lon1, lat1 = cd.bounds
        ex0, ey0 = _to3857(lon0, lat0)
        ex1, ey1 = _to3857(lon1, lat1)
        s = SCATTER_CELL_M * _merc_k((lat0 + lat1) / 2)  # (in ground metres as at MERC_REF_LAT)
        gi = np.arange(math.floor(ex0 / s), math.ceil(ex1 / s))
        gj = np.arange(math.floor(ey0 / s), math.ceil(ey1 / s))
        GI, GJ = np.meshgrid(gi, gj)
        GI, GJ = GI.ravel(), GJ.ravel()
        px = (GI + _hash01(GI, GJ, 11)) * s
        py = (GJ + _hash01(GI, GJ, 12)) * s
        keep = (px >= ex0) & (px < ex1) & (py >= ey0) & (py < ey1)
        px, py, GI, GJ = px[keep], py[keep], GI[keep], GJ[keep]
        lon, lat = _to4326(px, py)
        cls = lc.sample(lon, lat)
        u = _hash01(GI, GJ, 13)
        pb = np.zeros(len(px))
        pr = np.zeros(len(px))
        for c, (b, r) in SCATTER_P.items():
            pb = np.where(cls == c, b, pb)
            pr = np.where(cls == c, r, pr)
        # on the CHM grid: not blocked, no canopy there already (the trees are the CHM's)
        c_ = np.floor((px - chm.x0) / chm.dx).astype(np.int64)
        r_ = np.floor((chm.y0 - py) / chm.dy).astype(np.int64)
        ok = (c_ >= 0) & (c_ < hs.shape[1]) & (r_ >= 0) & (r_ < hs.shape[0])
        free = np.zeros(len(px), bool)
        free[ok] = ~blocked[r_[ok], c_[ok]] & ((hs[r_[ok], c_[ok]] < BUSH_MIN_M) | (cls[ok] == UNDERSTORY))
        # bare land in a town is a square, a lot or a building site, not a rock field (desert rocks stood on
        # Mexico City's Zocalo, 2026-10-15): none where built-up land is around (2 of 4 points BUILT_NEAR_M away)
        bare = np.flatnonzero(free & (cls == WC["bare"]))
        if len(bare):
            near = np.zeros(len(bare), np.int64)
            for dx, dy in ((BUILT_NEAR_M, 0.0), (-BUILT_NEAR_M, 0.0), (0.0, BUILT_NEAR_M), (0.0, -BUILT_NEAR_M)):
                lo2, la2 = _to4326(px[bare] + dx, py[bare] + dy)
                near += lc.sample(lo2, la2) == WC["built"]
            free[bare[near >= 2]] = False
        # a beach's sand (beaches.py, GitHub #578): neither rocks nor bushes
        # (only the points that would get one: a few % of them, the test samples ~70 points round each)
        sandy = np.flatnonzero(free & (u < pr + pb) & ((cls == WC["bare"]) | (cls == WC["grass"])))
        if len(sandy):
            sec = 1.0 / dnp.cos(np.radians(lat[sandy]))      # (Web Mercator metres per ground metre)
            pxs, pys = px[sandy], py[sandy]

            def at(idx, dx, dy):
                return lc.sample(*_to4326(pxs[idx] + dx * sec[idx], pys[idx] + dy * sec[idx]))
            free[sandy[beaches.sand_points(cls[sandy], at)]] = False
        rock = free & (u < pr)
        bare_rock = np.flatnonzero(rock & (cls == WC["bare"]))
        if len(bare_rock) and self.dem is not None:
            slope = self._slope(px[bare_rock], py[bare_rock])
            if slope is not None:
                lo_s, hi_s = ROCK_SLOPE
                keep = _hash01(GI[bare_rock], GJ[bare_rock], 19) < np.clip((slope - lo_s) / (hi_s - lo_s), 0.0, 1.0)
                rock[bare_rock[~keep]] = False
                free[bare_rock[~keep]] = False     # (nor a bush there: bare ground stays bare)
        bush = free & ~rock & (u < pr + pb)
        sel = rock | bush
        n = int(sel.sum())
        size = 0.4 + 1.6 * dnp.sq(_hash01(GI[sel], GJ[sel], 14))
        if not n:
            return _empty()
        it = Items(lon[sel], lat[sel], size.astype(np.float32), (size * 0.6).astype(np.float32),
                     np.where(rock[sel], KIND_ROCK, KIND_BUSH).astype(np.uint8),
                     regional(np.where(rock[sel], G_ROCK, G_SHRUB), size, lat[sel], lon[sel], cls[sel],
                              _hash01(GI[sel], GJ[sel], 18)),
                     _hash01(GI[sel], GJ[sel], 15).astype(np.float32),
                     (_hash01(GI[sel], GJ[sel], 16) * 360.0).astype(np.float32),
                     (0.8 + 0.4 * _hash01(GI[sel], GJ[sel], 17)).astype(np.float32))
        return climate_shrubs(it, cls[sel], understory=cls[sel] == UNDERSTORY)


def _thin(jj, ii, h, px, k=1.0, spacing=None):
    """Non-maximum suppression: the tallest tops first, each removing the others within its crown
    radius (0.12 h + 1 m, times CROWN_SPACING). Deterministic (ties broken by position). earthfast thin_tops
    (2026-10-23: the same tops as the Python loop it replaced). k (_merc_k): the pixels judged at the tuned scale, their
    indices divided by k (thin_tops' radius and its 1.5 px floor are in pixels: at 69 N a pixel is 0.47 of one at
    MERC_REF_LAT on the ground, and the floor let ~3x the trees stand)."""
    if len(jj) == 0:
        return jj, ii
    fast.require(64, "thin_tops")
    sj, si = (jj, ii) if k == 1.0 else (np.rint(jj / k), np.rint(ii / k))
    keep = fast._ef.thin_tops(np.ascontiguousarray(sj, np.int64), np.ascontiguousarray(si, np.int64),
                              np.ascontiguousarray(h, np.float32),
                              float(CROWN_SPACING if spacing is None else spacing), float(px))
    return jj[keep], ii[keep]


TRUNK_MIN_M = 3.0       # (50) two trees of TRUNK_TALL_M or more at least this far apart (ground m): a flat canopy
TRUNK_PER_H = 0.14      # ... and taller ones TRUNK_PER_H of their height (a 25 m oak: 3.5 m; its stock shape, a 16 m one
#                         scaled up 1.6-1.8 times, has a trunk 2-2.6 m wide at the ground)
TRUNK_TALL_M = 8.0      # plateau's tops are a lattice of the thinning radius, 2 m apart (Rogowska, the user's 2026-11-20)
TRUNK_BLOCK_M = 40.0    # dev's count of tall trees kept in every square this big (ground m)
TRUNK_CAND_M = 2.5      # a removed tree's replacement: one canopy pixel at a hashed spot of every square this big


def _trunk_v(h, u):
    """(50) _thin_trunks' heights for thin_tops: 0.12 v + 1 = the trunk distance (ground m) of a tree of height h
    (TRUNK_MIN_M, or TRUNK_PER_H x h when more), plus h / 1000 and u (a hash 0..1) / 10000 so the taller rank first
    and equal heights by the hash (exact in float32 up to ~60 m: 4e-6)."""
    h = np.asarray(h, np.float64)
    return (np.maximum(TRUNK_MIN_M, TRUNK_PER_H * h) - 1.0) / 0.12 + h * 1e-3 + np.asarray(u, np.float64) * 1e-4


def _thin_trunks(jj, ii, hs, valid, dx, lat, mx, my):
    """(50) The thinned tops (pixel rows jj, columns ii of the smoothed canopy hs) with the trees of TRUNK_TALL_M or
    more re-chosen so no two stand within their trunk distance on the ground (TRUNK_MIN_M, TRUNK_PER_H of the taller's
    height when more), and as many of them in every TRUNK_BLOCK_M
    square as dev's thinning had where the canopy has room. Dev's tops are kept tallest first (ties by a hash of the
    pixel, as _thin's); in a square that lost some, canopy pixels (valid: tall canopy, not blocked; one at a hashed spot of
    every TRUNK_CAND_M square) fill the free spots, the taller first. No tree moves and none stands off the canopy. Dev's
    thinning left a flat canopy's tops a lattice 2 m apart (Rogowska, the user's 2026-11-20); pushing them apart
    filled clearings and packed dense patches into grids, and thinning alone dropped ~57 % of the trees
    (Pennsylvania at 3 m). Shorter tops untouched. earthfast thin_tops, its radius the trunk distance (_trunk_v)."""
    h = hs[jj, ii]
    tall = h >= TRUNK_TALL_M
    if tall.sum() < 2:
        return jj, ii
    fast.require(64, "thin_tops")
    px_m = dx * max(0.05, dmath.cos(dmath.radians(min(abs(float(lat)), 85.0))))    # (a pixel on the ground)
    blk = max(1, int(round(TRUNK_BLOCK_M / px_m)))
    dj, di = jj[tall], ii[tall]
    # thin_tops' radius spacing x (0.12 v + 1) / px set to the trunk distance by the height: v from it, the height and
    # a hash added so the taller go first and equal heights by the hash (not by position: a lattice)
    tx = np.floor((mx[di] + 20037508.342789244) / dx).astype(np.int64)
    ty = np.floor((20037508.342789244 - my[dj]) / dx).astype(np.int64)
    v_d = _trunk_v(h[tall], _hash01(tx, ty, 29))
    first = fast._ef.thin_tops(np.ascontiguousarray(dj, np.int64), np.ascontiguousarray(di, np.int64),
                               np.ascontiguousarray(v_d, np.float32), float(1.0 / px_m), 1.0)
    # the blocks short of dev's count after it: there, one canopy pixel (valid) at a hashed spot of every
    # TRUNK_CAND_M square is a candidate
    kj, ki = dj[first], di[first]
    n_r, n_c = valid.shape
    bw = n_c // blk + 2
    nb = (n_r // blk + 2) * bw
    quota = np.bincount((dj // blk) * bw + di // blk, minlength=nb)
    short = quota > np.bincount((kj // blk) * bw + ki // blk, minlength=nb)
    if not short.any():
        return np.concatenate([jj[~tall], kj]), np.concatenate([ii[~tall], ki])
    c = max(1, int(round(TRUNK_CAND_M / px_m)))
    sy, sx = np.meshgrid(np.arange(0, n_r, c), np.arange(0, n_c, c), indexing="ij")
    sy, sx = sy.ravel(), sx.ravel()
    gx = np.floor((mx[sx] + 20037508.342789244) / dx).astype(np.int64) // c
    gy = np.floor((20037508.342789244 - my[sy]) / dx).astype(np.int64) // c
    cj = np.minimum(sy + (_hash01(gx, gy, 37) * c).astype(np.int64), n_r - 1)
    ci_ = np.minimum(sx + (_hash01(gx, gy, 38) * c).astype(np.int64), n_c - 1)
    ok = valid[cj, ci_] & short[(cj // blk) * bw + ci_ // blk]
    cj, ci_ = cj[ok], ci_[ok]
    u = _hash01(gx[ok], gy[ok], 39)
    dj, di, v_d = kj, ki, v_d[first]          # (the kept ones: no conflict among them)
    nd, nc = len(dj), len(cj)
    pri = np.concatenate([v_d, _trunk_v(hs[cj, ci_].astype(np.float64), u)])
    aj, ai = np.concatenate([dj, cj]), np.concatenate([di, ci_])
    keep = fast._ef.thin_tops(np.ascontiguousarray(aj, np.int64), np.ascontiguousarray(ai, np.int64),
                              np.ascontiguousarray(pri, np.float32), float(1.0 / px_m), 1.0)
    # each block keeps as many as dev had there, the highest priorities first
    bkey = (aj // blk) * bw + ai // blk
    k_idx = np.flatnonzero(keep)
    o = k_idx[np.lexsort((-pri[k_idx], bkey[k_idx]))]
    kb = bkey[o]
    grp = np.r_[0, np.flatnonzero(kb[1:] != kb[:-1]) + 1]
    start = np.repeat(grp, np.diff(np.r_[grp, len(o)]))
    sel = o[(np.arange(len(o)) - start) < quota[kb]]
    sel.sort()
    return np.concatenate([jj[~tall], aj[sel]]), np.concatenate([ii[~tall], ai[sel]])


def _empty():
    z = np.zeros(0)
    return Items(z, z, z.astype(np.float32), z.astype(np.float32), z.astype(np.uint8), z.astype(np.uint8),
                 z.astype(np.float32), z.astype(np.float32), z.astype(np.float32))


def _concat(a, b):
    return Items(*(np.concatenate([getattr(a, f), getattr(b, f)]) for f in
                   ("lon", "lat", "height", "radius", "kind", "group", "rank", "yaw", "scale")))


R = 6378137.0


def _to3857(lon, lat):
    lon = np.asarray(lon, np.float64)
    lat = np.clip(np.asarray(lat, np.float64), -85.0511287798066, 85.0511287798066)
    return np.radians(lon) * R, dnp.log(dnp.tan(np.pi / 4 + np.radians(lat) / 2)) * R


def _to4326(x, y):
    x = np.asarray(x, np.float64)
    y = np.asarray(y, np.float64)
    return np.degrees(x / R), np.degrees(2 * dnp.arctan(dnp.exp(y / R)) - np.pi / 2)


# Planted rows (Overture land_use class orchard). Spacing grows, by whole steps, on a huge orchard so one
# cell does not emit a forest of its own; the lattice is fixed in the world so a row continues across a
# cell edge when both pieces share an axis.
ORCHARD_ALONG_M = 2.5       # along a row
ORCHARD_ACROSS_M = 3.0      # between rows
ORCHARD_MIN_M2 = 600.0
ORCHARD_POLY_MAX = 3500
ORCHARD_CELL_MAX = 4500
# Crop rows (surface.FARM_ROW_M): a line of shrubs down the middle of each gap between the dirt furrows.
# Spacing along the row grows when a field would emit too many.
FARM_BUSH_ALONG = 6.0
FARM_BUSH_POLY = 500
FARM_BUSH_CELL = 2000
FARM_MIN_M2 = 800.0


def _keep_off_mask(items, mask, chm):
    """Per item: its position is not on a set pixel of mask (canopy trees inside an orchard go)."""
    if len(items) == 0 or mask is None or not np.any(mask):
        return np.ones(len(items), bool)
    x, y = _to3857(items.lon, items.lat)
    c = np.floor((x - chm.x0) / chm.dx).astype(np.int64)
    r = np.floor((chm.y0 - y) / chm.dy).astype(np.int64)
    n_r, n_c = mask.shape
    inside = (r >= 0) & (r < n_r) & (c >= 0) & (c < n_c)
    drop = np.zeros(len(items), bool)
    drop[inside] = mask[r[inside], c[inside]]
    return ~drop


def clump_cell(lat):
    """The clump grid's square (Web Mercator m) at latitude lat: a whole number of them a geotile (geotiles.Z), each
    about CLUMP_CELL_M on the ground."""
    import geotiles
    tile = 2.0 * math.pi * R / (1 << geotiles.Z)
    k = max(1, int(round(tile * max(dmath.cos(math.radians(lat)), 0.05) / CLUMP_CELL_M)))
    return tile / k


class ClumpRoads:
    """The carriageways the far clumps keep off (clumps): _road_segs' (segs, hws) on the CHM grid chm, any number; a
    clump moved off one stays in the cell's box own (x0, y0, x1, y1, Web Mercator m: x0 <= x < x1, y0 < y <= y1; its
    tiles show it) and off blocked (bool, on the CHM grid: buildings, water, decks), else its trees stay single (Codex
    review of cff64d91: a move across the cell's edge or into water hid its trees far away)."""

    def __init__(self, parts, chm, blocked=None, own=None):
        self.blocked = np.zeros((0, 0), bool) if blocked is None else np.asarray(blocked, bool)
        self.own = tuple(float(v) for v in own) if own is not None else (-np.inf, -np.inf, np.inf, np.inf)
        parts = [p for p in parts if p is not None and len(p[1])]
        self.segs = np.ascontiguousarray(np.concatenate([p[0] for p in parts]), np.float64) if parts \
            else np.zeros((0, 4), np.float64)
        self.hws = np.ascontiguousarray(np.concatenate([p[1] for p in parts]), np.float64) if parts \
            else np.zeros(0, np.float64)
        self.grid = (float(chm.x0), float(chm.y0), float(chm.dx), float(chm.dy)) if chm is not None \
            else (0.0, 0.0, 1.0, 1.0)


CLUMP_STATS = {"clear": 0, "split": 0, "moved": 0, "single": 0}     # (counts for the probes: clumps' road outcomes)
_clump_stats_lock = threading.Lock()


MERC_HALF_WORLD = 20037508.342789244     # (earthfast vegclumps' HALF_WORLD: its squares' keys)


def _unlattice(cx, cy, ch, cu, x, y, member, cell, merc_per_m, roads):
    """(53) The far clumps a square's trees' centroid stood at (earthfast veg_clumps: the squares clear of the roads)
    moved to one of their own trees, picked by the clump's hash cu: the centroid of a full square's 4-8 tops is nearly
    its middle, a lattice of ~18 m up a forested slope, rows as only an orchard has (the user's hills over Split,
    2026-11-23; Syrynka's clumps: 43 % within a tenth of a square of its middle, 3 % for random spots). A tree's spot is
    on the canopy, off buildings and water, in the square's own map cell; kept at the centroid when the clump there
    would reach a road (roads: ClumpRoads' segments and half widths). The road-split and moved clumps stay as placed."""
    if len(cx) == 0 or not member.any():
        return cx, cy
    ids = np.flatnonzero(member)
    ki = np.floor((x[ids] + MERC_HALF_WORLD) / cell).astype(np.int64)
    kj = np.floor((y[ids] + MERC_HALF_WORLD) / cell).astype(np.int64)
    key = ki * (1 << 31) + kj
    order = np.argsort(key, kind="stable")
    ids, key = ids[order], key[order]
    start = np.r_[0, np.flatnonzero(key[1:] != key[:-1]) + 1]
    count = np.diff(np.r_[start, len(key)])
    ukey = key[start]
    sx = np.add.reduceat(x[ids], start) / count
    sy = np.add.reduceat(y[ids], start) / count
    ck = np.floor((cx + MERC_HALF_WORLD) / cell).astype(np.int64) * (1 << 31)         + np.floor((cy + MERC_HALF_WORLD) / cell).astype(np.int64)
    pos = np.searchsorted(ukey, ck)
    pos = np.minimum(pos, len(ukey) - 1)
    found = ukey[pos] == ck
    clear = found & (np.abs(sx[pos] - cx) < 1e-3) & (np.abs(sy[pos] - cy) < 1e-3)
    if not clear.any():
        return cx, cy
    k = np.flatnonzero(clear)
    pick = ids[start[pos[k]] + np.minimum((cu[k] * count[pos[k]]).astype(np.int64), count[pos[k]] - 1)]
    nx, ny = x[pick], y[pick]
    if roads is not None and len(roads.segs):
        r = (CLUMP_R_K * ch[k].astype(np.float64) + CLUMP_R_C) * merc_per_m
        ok = np.ones(len(k), bool)
        # (the segments in the CHM grid's pixels: to Web Mercator as earthfast veg_clumps reads them, roads.grid)
        gx0, gy0, gdx, gdy = roads.grid
        segs, hws = roads.segs, roads.hws * gdx
        for a in range(0, len(segs), 512):          # (in chunks: clumps x segments)
            sg, hw = segs[a:a + 512], hws[a:a + 512]
            ax, ay = gx0 + sg[:, 0] * gdx, gy0 - sg[:, 1] * gdy
            bx, by = gx0 + sg[:, 2] * gdx, gy0 - sg[:, 3] * gdy
            dx, dy = bx - ax, by - ay
            l2 = np.maximum(dx * dx + dy * dy, 1e-12)
            t = np.clip(((nx[:, None] - ax) * dx + (ny[:, None] - ay) * dy) / l2, 0.0, 1.0)
            qx, qy = ax + t * dx - nx[:, None], ay + t * dy - ny[:, None]
            d = np.sqrt(qx * qx + qy * qy)
            ok &= ~(d < hw[None, :] + r[:, None]).any(axis=1)
        k, nx, ny = k[ok], nx[ok], ny[ok]
    cx, cy = cx.copy(), cy.copy()
    cx[k], cy[k] = nx, ny
    return cx, cy


def clumps(tree, lcls, lat, roads=None):
    """tree (a cell's canopy Items) with the far clumps added (CLUMP_OF, earthfast.veg_clumps) and the trees they
    stand for flagged (a negative scale: trees.lua shows them only within its near ring). lcls: the land cover class
    of each; lat: the cell's (clump_cell). Street trees (built-up land) stay single. roads (ClumpRoads): the
    carriageways a clump's footprint (CLUMP_R_K * height + CLUMP_R_C) keeps off: split at the road, moved off it by up
    to CLUMP_SHIFT_M, else its trees stay single (veg_clumps)."""
    if len(tree) < CLUMP_MIN:
        return tree
    fast.require(321, "veg_clumps")
    x, y = _to3857(tree.lon, tree.lat)
    table = [255] * len(GROUPS)
    for g, c in CLUMP_OF.items():
        table[g] = c
    if roads is None:
        roads = ClumpRoads([], None)
    merc_per_m = 1.0 / max(dmath.cos(math.radians(min(abs(float(lat)), 85.0))), 0.05)
    member, cx, cy, ch, cr, cg, cu, st = fast._ef.veg_clumps(
        np.ascontiguousarray(x, np.float64), np.ascontiguousarray(y, np.float64),
        np.ascontiguousarray(tree.group, np.uint8), np.ascontiguousarray(tree.kind, np.uint8),
        np.ascontiguousarray(tree.height, np.float32), np.ascontiguousarray(tree.rank, np.float32),
        np.ascontiguousarray(lcls, np.uint8), table, KIND_TREE, WC["built"], clump_cell(lat), CLUMP_MIN,
        roads.segs, roads.hws, roads.grid, merc_per_m, (CLUMP_R_K, CLUMP_R_C), CLUMP_SHIFT_M, roads.own,
        roads.blocked)
    with _clump_stats_lock:
        for k_, v_ in zip(("clear", "split", "moved", "single"), st):
            CLUMP_STATS[k_] += int(v_)
    if not len(cx):
        return tree
    member = np.asarray(member, bool)
    cx, cy = _unlattice(np.asarray(cx, np.float64), np.asarray(cy, np.float64), np.asarray(ch, np.float32),
                        np.asarray(cu, np.float64), x, y, member, clump_cell(lat), merc_per_m, roads)
    scale = tree.scale.copy()
    scale[member] = -np.abs(scale[member])
    tree = Items(tree.lon, tree.lat, tree.height, tree.radius, tree.kind, tree.group, tree.rank, tree.yaw, scale)
    lon, la = _to4326(np.asarray(cx), np.asarray(cy))
    h = np.asarray(ch, np.float32)
    n = len(lon)
    return _concat(tree, Items(lon, la, h, (CLUMP_R_K * h + CLUMP_R_C).astype(np.float32), np.full(n, KIND_TREE, np.uint8),
                               np.asarray(cg, np.uint8), np.asarray(cr, np.float32),
                               (np.asarray(cu) * 360.0).astype(np.float32), np.ones(n, np.float32)))


def _fid_key(fid):
    x = 1469598103934665603
    for b in str(fid).encode():
        x ^= b
        x = (x * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return x


def _lattice(xy, holes, u, along, across, phase_a, phase_b, max_steps=300):
    """The points (i along + phase_a) u + (j across + phase_b) v (Web Mercator m, v = u turned left) inside ring xy
    and outside holes (lon / lat rings), i-major (earthfast.field_lattice); None when a side would take more than
    max_steps."""
    rings = [np.asarray(xy, np.float64)]
    for hole in holes:
        hole = np.asarray(hole, np.float64)
        if len(hole) >= 3:
            rings.append(np.column_stack(_to3857(hole[:, 0], hole[:, 1])))
    off = np.cumsum([0] + [len(r) for r in rings]).astype(np.int64)
    fast.require(201, "field_lattice")
    got = fast._ef.field_lattice(np.ascontiguousarray(np.concatenate(rings)), off, (float(u[0]), float(u[1])),
                                 float(along), float(across), float(phase_a), float(phase_b), int(max_steps))
    return None if got is None else np.asarray(got[0])


def orchard_items(feats, chm, blocked, bounds=None):
    """Trees in lines on land_use class orchard: along the long side, one line every ORCHARD_ACROSS_M.
    blocked is the canopy grid's buildings, roads and water. bounds (lon0, lat0, lon1, lat1) keeps a tree
    in this cell. None of the random canopy."""
    if feats is None or len(getattr(feats, "kind", ())) == 0 or feats.attrs.get("class") is None:
        return _empty()
    cls = np.asarray(feats.attrs["class"]).astype(str)
    ids = getattr(feats, "ids", None)
    xs, ys, hs, yaws, ranks, us = [], [], [], [], [], []
    seen = set()
    for i in np.flatnonzero((np.asarray(feats.kind) == 3) & (cls == "orchard")):
        fid = ids[i] if ids is not None else i
        if str(fid) in seen:
            continue
        seen.add(str(fid))
        parts = feats.parts(int(i))
        if not parts or not parts[0] or len(parts[0][0]) < 3:
            continue
        ring = np.asarray(parts[0][0], np.float64)
        if len(ring) >= 2 and np.allclose(ring[0], ring[-1]):
            ring = ring[:-1]
        x, y = _to3857(ring[:, 0], ring[:, 1])
        xy = np.column_stack([x, y])
        area = abs(float(dnp.dot(x, np.roll(y, -1)) - dnp.dot(y, np.roll(x, -1)))) * 0.5
        if area < ORCHARD_MIN_M2:
            continue
        u = _orchard_axis(xy)
        v = np.array([-u[1], u[0]])
        span_a = float((dnp.matmul(xy, u)).max() - (dnp.matmul(xy, u)).min())
        span_b = float((dnp.matmul(xy, v)).max() - (dnp.matmul(xy, v)).min())
        along, across = ORCHARD_ALONG_M, ORCHARD_ACROSS_M
        factor = 1
        while factor < 16 and (area / (along * across) > ORCHARD_POLY_MAX or span_a / along > 300
                               or span_b / across > 300):
            factor += 1
            along, across = ORCHARD_ALONG_M * factor, ORCHARD_ACROSS_M * factor
        phase_a = float(_hash01(np.array([_fid_key(fid)], np.uint64), np.array([1], np.uint64), 11)[0]) * along
        phase_b = float(_hash01(np.array([_fid_key(fid)], np.uint64), np.array([2], np.uint64), 11)[0]) * across
        pts = _lattice(xy, parts[0][1:], u, along, across, phase_a, phase_b)
        if pts is None:
            continue
        if len(pts) == 0:
            continue
        key = np.uint64(_fid_key(fid))
        jit_a = (_hash01(np.full(len(pts), key), np.arange(len(pts), dtype=np.uint64), 3) - 0.5) * 0.6
        jit_b = (_hash01(np.full(len(pts), key), np.arange(len(pts), dtype=np.uint64), 4) - 0.5) * 0.3
        pts = pts + jit_a[:, None] * u + jit_b[:, None] * v
        if blocked is not None and np.any(blocked):
            c = np.floor((pts[:, 0] - chm.x0) / chm.dx).astype(np.int64)
            r = np.floor((chm.y0 - pts[:, 1]) / chm.dy).astype(np.int64)
            n_r, n_c = blocked.shape
            ok = (r >= 0) & (r < n_r) & (c >= 0) & (c < n_c)
            bad = np.zeros(len(pts), bool)
            bad[ok] = blocked[r[ok], c[ok]]
            pts = pts[~bad]
        if len(pts) == 0:
            continue
        if bounds is not None:
            lon0, lat0, lon1, lat1 = bounds
            ex0, ey0 = _to3857(lon0, lat0)
            ex1, ey1 = _to3857(lon1, lat1)
            pts = pts[(pts[:, 0] >= ex0) & (pts[:, 0] < ex1) & (pts[:, 1] > ey0) & (pts[:, 1] <= ey1)]
        if len(pts) == 0:
            continue
        lon, lat = _to4326(pts[:, 0], pts[:, 1])
        uh = _hash01(np.full(len(pts), key), np.arange(len(pts), dtype=np.uint64), 5)
        yaw0 = math.degrees(dmath.atan2(float(u[1]), float(u[0])))
        xs.append(lon)
        ys.append(lat)
        hs.append(4.2 + 2.3 * uh)
        yaws.append(yaw0 + (uh - 0.5) * 24.0)
        ranks.append(uh * 0.18)
        us.append(uh)
    if not xs:
        return _empty()
    lon, lat = np.concatenate(xs), np.concatenate(ys)
    height = np.concatenate(hs).astype(np.float32)
    yaw = np.concatenate(yaws).astype(np.float32)
    rank = np.concatenate(ranks).astype(np.float32)
    u = np.concatenate(us)
    if len(lon) > ORCHARD_CELL_MAX:
        keep = np.sort(np.argsort(rank, kind="stable")[:ORCHARD_CELL_MAX])
        lon, lat, height, yaw, rank, u = lon[keep], lat[keep], height[keep], yaw[keep], rank[keep], u[keep]
    group = np.full(len(lon), G_DECIDUOUS, np.uint8)
    group = regional(group, height, lat, lon, np.full(len(lon), WC["crop"], np.uint8), u)
    return Items(lon, lat, height, (0.12 * height + 1.0).astype(np.float32),
                 np.full(len(lon), KIND_TREE, np.uint8), group, rank, yaw, np.ones(len(lon), np.float32))


# Vineyards (Overture land_use class vineyard; the asset survey's #11): Italy's grape_vine_group, three rows 2.45 m
# apart and 15.9 m long, end to end along the field's dominant edge (_edge_axis; aerials of Saint-Emilion and Napa,
# 2026-11-10: the rows run along the parcel's sides, 1.5-3 m apart), an item's ends inside the field and off its
# roads and buildings. The
# canopy's "bushes" inside a vineyard (the vines themselves, read from the canopy heights) are left out.
VINE_ALONG_M = 15.9
VINE_ACROSS_M = 7.35
VINE_H_M = 2.6              # the shape's height (trees.lua CATALOG.vine)
VINE_MIN_M2 = 300.0
VINE_CELL_MAX = 40000
VINE_TREE_M = 4.0           # a canopy top this tall in a vineyard is a tree, kept
# (56) where a group does not fit (its ends out of the field, on a road or a building: the field's edges, a lattice
# square's worth of bare ground by every road, the user's Laufen 2026-11-24, where the real rows run to the track)
# single rows (Italy's grape_vine, 5.5 m of one row, 2.4 m high) fill each row line (the groups' rows, VINE_ROW_ACROSS_M
# apart) from where the groups end to the field's edge: the line sampled every VINE_ROW_SAMPLE_M, its free runs (in the
# field, out of its holes, off roads and buildings, off the groups) VINE_ROW_END_M in from their ends, each run covered
# by pieces at most VINE_ROW_ALONG_M apart, the first and last flush with its ends (rows ending along the edge, not in
# the lattice's steps: the user's "teeth", 2026-11-24); a run under VINE_ROW_MIN_M stays bare
VINE_ROW_ALONG_M = 5.3
VINE_ROW_ACROSS_M = 2.45
VINE_ROW_HALF_M = 2.75      # m: half the piece's length
VINE_ROW_END_M = 0.5
VINE_ROW_SAMPLE_M = 0.5
VINE_ROW_MIN_M = 4.5         # (a shorter run: the 5.5 m piece would reach past the field's edge)
VINE_ROW_H_M = 2.39         # the shape's height (trees.lua CATALOG.vine_row)
EDGE_AXIS_DEG = 8.0         # _edge_axis: edges this near a direction count for it


def _blocked_at(pts, chm, blocked):
    """Per Web Mercator point (m, 2): on a set pixel of the canopy-grid mask blocked."""
    c = np.floor((pts[:, 0] - chm.x0) / chm.dx).astype(np.int64)
    r = np.floor((chm.y0 - pts[:, 1]) / chm.dy).astype(np.int64)
    n_r, n_c = blocked.shape
    ok = (r >= 0) & (r < n_r) & (c >= 0) & (c < n_c)
    bad = np.zeros(len(pts), bool)
    bad[ok] = blocked[r[ok], c[ok]]
    return bad


def _field_rings(feats, cls_name, min_m2):
    """(fid, ring (Web Mercator m, open), its holes (lon / lat rings), area m2 (Web Mercator)) of each land use
    polygon of class cls_name, once per id."""
    if feats is None or len(getattr(feats, "kind", ())) == 0 or feats.attrs.get("class") is None:
        return
    cls = np.asarray(feats.attrs["class"]).astype(str)
    ids = getattr(feats, "ids", None)
    seen = set()
    for i in np.flatnonzero((np.asarray(feats.kind) == 3) & (cls == cls_name)):
        fid = ids[i] if ids is not None else i
        if str(fid) in seen:
            continue
        seen.add(str(fid))
        parts = feats.parts(int(i))
        if not parts or not parts[0] or len(parts[0][0]) < 3:
            continue
        ring = np.asarray(parts[0][0], np.float64)
        if len(ring) >= 2 and np.allclose(ring[0], ring[-1]):
            ring = ring[:-1]
        x, y = _to3857(ring[:, 0], ring[:, 1])
        area = abs(float(dnp.dot(x, np.roll(y, -1)) - dnp.dot(y, np.roll(x, -1)))) * 0.5
        if area >= min_m2:
            yield fid, np.column_stack([x, y]), parts[0][1:], area


def _vine_runs(xy, path, hps, chm, blocked, u, v, groups, phase_b, sec):
    """(56) The single rows (VINE_ROW_*) of a vineyard ring xy (Web Mercator; path its Path, hps its holes'): on each
    row line (v-coordinate phase_b + m VINE_ROW_ACROSS_M sec, the groups' rows) the runs in the field, off blocked and off
    the groups (centres groups: a group covers its three lines VINE_ALONG_M / 2 either side of its centre), each covered by
    pieces flush with its ends. Returns their centres (n, 2) and an id per piece (line and order: its hash)."""
    rs, ds = VINE_ROW_ACROSS_M * sec, VINE_ROW_SAMPLE_M * sec
    a_r, b_r = dnp.matmul(xy, u), dnp.matmul(xy, v)
    m0 = int(math.ceil((float(b_r.min()) - phase_b) / rs))
    m1 = int(math.floor((float(b_r.max()) - phase_b) / rs))
    ga = dnp.matmul(groups, u) if len(groups) else np.zeros(0)
    gm = np.rint((dnp.matmul(groups, v) - phase_b) / rs).astype(np.int64) if len(groups) else np.zeros(0, np.int64)
    gh = (0.5 * VINE_ALONG_M) * sec
    t = np.arange(float(a_r.min()), float(a_r.max()) + ds, ds)
    out, ids = [], []
    if len(t) < 2 or m1 < m0:
        return np.zeros((0, 2)), np.zeros(0, np.int64)
    for m in range(m0, m1 + 1):
        b = phase_b + m * rs
        q = t[:, None] * u[None, :] + b * v[None, :]
        free = np.asarray(path.contains_points(q), bool)
        if not free.any():
            continue
        for hp in hps:
            free &= ~np.asarray(hp.contains_points(q), bool)
        if blocked is not None and np.any(blocked):
            free &= ~_blocked_at(q, chm, blocked)
        near = np.abs(gm - m) <= 1
        for a in ga[near]:
            free &= np.abs(t - a) > gh
        if not free.any():
            continue
        edge = np.diff(np.concatenate([[0], free.astype(np.int8), [0]]))
        starts, ends = np.flatnonzero(edge == 1), np.flatnonzero(edge == -1) - 1
        h, step, end = VINE_ROW_HALF_M * sec, VINE_ROW_ALONG_M * sec, VINE_ROW_END_M * sec
        for i0, i1 in zip(starts, ends):
            t0, t1 = float(t[i0]) + end, float(t[i1]) - end
            length = t1 - t0
            if length < VINE_ROW_MIN_M * sec:
                continue
            if length <= 2 * h:
                cs = np.array([0.5 * (t0 + t1)])
            else:
                n = int(math.ceil((length - 2 * h) / step)) + 1
                cs = t0 + h + (length - 2 * h) * np.arange(n) / (n - 1)
            out.append(cs[:, None] * u[None, :] + b * v[None, :])
            ids.append((m + (1 << 16)) * (1 << 16) + int(i0) // 4 * 64 + np.arange(len(cs)))
    if not out:
        return np.zeros((0, 2)), np.zeros(0, np.int64)
    return np.concatenate(out), np.concatenate(ids).astype(np.int64)


def vineyard_items(feats, chm, blocked, bounds=None):
    """The rows of vines of land_use class vineyard (VINE_*): G_VINE items, yaw along the rows. blocked: the canopy
    grid's buildings, roads and water; bounds (lon0, lat0, lon1, lat1) keeps an item in this cell."""
    from polygon import Path
    xs, ys, yaws, us, gs = [], [], [], [], []
    for fid, xy, holes, area in _field_rings(feats, "vineyard", VINE_MIN_M2):
        u = _edge_axis(xy)
        key = np.uint64(_fid_key(fid))
        sec = 1.0 / max(dmath.cos(math.radians(float(_to4326(xy[:1, 0], xy[:1, 1])[1][0]))), 0.05)   # (merc m / m)
        along, across = VINE_ALONG_M * sec, VINE_ACROSS_M * sec
        phase_a = float(_hash01(np.array([key]), np.array([1], np.uint64), 31)[0]) * along
        phase_b = float(_hash01(np.array([key]), np.array([2], np.uint64), 31)[0]) * across
        pts = _lattice(xy, holes, u, along, across, phase_a, phase_b)
        if pts is None or len(pts) == 0:
            continue
        path = Path(xy)
        hps = [Path(np.column_stack(_to3857(np.asarray(hole, np.float64)[:, 0], np.asarray(hole, np.float64)[:, 1])))
               for hole in holes if len(hole) >= 3]

        def fits(c, k):
            """an item centred at c (n, 2) reaching k along u either side: its ends in the field, out of its holes,
            and its centre and ends free"""
            ends = [c + k * u, c - k * u]
            ok = np.ones(len(c), bool)
            for e in ends:
                ok &= np.asarray(path.contains_points(e), bool)
                for hp in hps:
                    ok &= ~np.asarray(hp.contains_points(e), bool)
            if blocked is not None and np.any(blocked):
                for q in [c] + ends:
                    ok &= ~_blocked_at(q, chm, blocked)
            return ok

        # the item's ends (a metre in from them) in the field too, and every one of its three points free
        ok = fits(pts, (0.5 * VINE_ALONG_M - 1.0) * sec)
        pts = pts[ok]
        # (56) the row lines' runs the groups leave: single rows flush with their ends
        v = np.array([-u[1], u[0]])
        rows, rid = _vine_runs(xy, path, hps, chm, blocked, u, v, pts, phase_b, sec)
        if bounds is not None:
            ex0, ey0 = _to3857(bounds[0], bounds[1])
            ex1, ey1 = _to3857(bounds[2], bounds[3])

            def inside(q):
                return (q[:, 0] >= ex0) & (q[:, 0] < ex1) & (q[:, 1] > ey0) & (q[:, 1] <= ey1)
            if len(pts):
                pts = pts[inside(pts)]
            if len(rows):
                m = inside(rows)
                rows, rid = rows[m], rid[m]
        if len(pts) == 0 and len(rows) == 0:
            continue
        yaw0 = math.degrees(dmath.atan2(float(u[1]), float(u[0])))
        uh = _hash01(np.full(len(pts), key), np.arange(len(pts), dtype=np.uint64), 32)
        ur = _hash01(np.full(len(rows), key), rid.astype(np.uint64) + np.uint64(1 << 20), 32)
        xs.append(np.concatenate([pts[:, 0], rows[:, 0]]))
        ys.append(np.concatenate([pts[:, 1], rows[:, 1]]))
        yaws.append(np.concatenate([yaw0 + 180.0 * (uh < 0.5), yaw0 + 180.0 * (ur < 0.5)]))
        us.append(np.concatenate([uh, ur]))
        gs.append(np.concatenate([np.full(len(pts), G_VINE, np.uint8), np.full(len(rows), G_VINE_ROW, np.uint8)]))
    if not xs:
        return _empty()
    x, y, yaw, u = np.concatenate(xs), np.concatenate(ys), np.concatenate(yaws), np.concatenate(us)
    g = np.concatenate(gs)
    rank = (u * 0.15).astype(np.float32)
    if len(x) > VINE_CELL_MAX:
        keep = np.sort(np.argsort(rank, kind="stable")[:VINE_CELL_MAX])
        x, y, yaw, u, rank, g = x[keep], y[keep], yaw[keep], u[keep], rank[keep], g[keep]
    lon, lat = _to4326(x, y)
    n = len(lon)
    h = (np.where(g == G_VINE_ROW, VINE_ROW_H_M, VINE_H_M)
         * (0.95 + 0.1 * _hash01(np.arange(n, dtype=np.uint64), np.full(n, 7, np.uint64), 33))).astype(np.float32)
    return Items(lon, lat, h, np.full(n, 1.2, np.float32), np.full(n, KIND_BUSH, np.uint8),
                 g.astype(np.uint8), rank, yaw.astype(np.float32), np.ones(n, np.float32))


# Round bales on fields after the harvest (the asset survey's #16): the stock race props s_haybaleround_large_01
# (straw, on land_use class farmland: the cereals' straw) and s_haybaleround_alt_01 (wrapped silage, on meadows),
# along windrows BALE_ACROSS_M apart down the field's dominant edge, one every ~BALE_ALONG_M, some left out. An OSM
# farmland polygon often spans many fields (the Beauce's, 2026-11-10 aerial: one polygon over dozens of fields, the
# bales' blocks crossed them): only a polygon up to BALE_FIELD_MAX_M2, a field, is baled, BALE_SHARE of them (a hash of
# its id), a headland BALE_EDGE_M wide left clear. The season is the mod's (trees.lua SEASON: its groups by hemisphere, the
# date's month): straw in July-September, silage May-September (the south: six months on); none in the tropics.
BALE_ALONG_M = 38.0
BALE_ACROSS_M = 14.0
BALE_FIELD_MAX_M2 = 40e4
BALE_EDGE_M = 8.0
BALE_SHARE = 0.2
BALE_KEEP = 0.75
BALE_JITTER_M = 9.0
BALE_H_M = 1.25             # the shapes' height (trees.lua CATALOG.straw_* / silage_*)
BALE_MIN_M2 = 3000.0
BALE_CELL_MAX = 6000
BALE_GROUPS = (G_STRAW_N, G_STRAW_S, G_SILAGE_N, G_SILAGE_S)
BALE_CLASSES = {"farmland": (G_STRAW_N, G_STRAW_S), "meadow": (G_SILAGE_N, G_SILAGE_S)}


def bale_items(feats, chm, blocked, bounds=None):
    """Round bales on a share of the fields (BALE_*): items of the BALE_GROUPS by the field's class and hemisphere.
    blocked: the canopy grid's buildings, roads and water; bounds (lon0, lat0, lon1, lat1) keeps a bale in this cell."""
    xs, ys, yaws, gs, us = [], [], [], [], []
    for cls_name, (g_north, g_south) in BALE_CLASSES.items():
        for fid, xy, holes, area in _field_rings(feats, cls_name, BALE_MIN_M2):
            lat = float(_to4326(xy[:1, 0], xy[:1, 1])[1][0])
            if abs(lat) < 23.5:
                continue
            sec = 1.0 / max(dmath.cos(math.radians(lat)), 0.05)
            if area > BALE_FIELD_MAX_M2 * sec * sec:
                continue                    # (many fields in one polygon: no field's edges to follow)
            key = np.uint64(_fid_key(fid))
            if float(_hash01(np.array([key]), np.array([0], np.uint64), 42)[0]) >= BALE_SHARE:
                continue
            u = _edge_axis(xy)
            along, across = BALE_ALONG_M * sec, BALE_ACROSS_M * sec
            phase_a = float(_hash01(np.array([key]), np.array([1], np.uint64), 41)[0]) * along
            phase_b = float(_hash01(np.array([key]), np.array([2], np.uint64), 41)[0]) * across
            pts = _lattice(xy, holes, u, along, across, phase_a, phase_b, max_steps=800)
            if pts is None or len(pts) == 0:
                continue
            idx = np.arange(len(pts), dtype=np.uint64)
            baled = _hash01(np.full(len(pts), key), idx, 43) < BALE_KEEP
            pts, idx = pts[baled], idx[baled]
            pts = pts + ((_hash01(np.full(len(pts), key), idx, 44) - 0.5) * 2 * BALE_JITTER_M * sec)[:, None] * u
            if len(pts):                    # (the headland: BALE_EDGE_M in from the field's edge, every way)
                from polygon import Path
                path, v = Path(xy), np.array([-u[1], u[0]])
                for d in (u, -u, v, -v):
                    pts = pts[np.asarray(path.contains_points(pts + d * BALE_EDGE_M * sec), bool)]
            if blocked is not None and np.any(blocked):
                pts = pts[~_blocked_at(pts, chm, blocked)]
            if bounds is not None and len(pts):
                ex0, ey0 = _to3857(bounds[0], bounds[1])
                ex1, ey1 = _to3857(bounds[2], bounds[3])
                pts = pts[(pts[:, 0] >= ex0) & (pts[:, 0] < ex1) & (pts[:, 1] > ey0) & (pts[:, 1] <= ey1)]
            if len(pts) == 0:
                continue
            uh = _hash01(np.full(len(pts), key), np.arange(len(pts), dtype=np.uint64), 45)
            yaw0 = math.degrees(dmath.atan2(float(u[1]), float(u[0])))
            xs.append(pts[:, 0])
            ys.append(pts[:, 1])
            yaws.append(yaw0 + 90.0 + (uh - 0.5) * 50.0)
            gs.append(np.full(len(pts), g_north if lat >= 0 else g_south, np.uint8))
            us.append(uh)
    if not xs:
        return _empty()
    x, y, yaw, g, u = (np.concatenate(a) for a in (xs, ys, yaws, gs, us))
    rank = (u * 0.3).astype(np.float32)
    if len(x) > BALE_CELL_MAX:
        keep = np.sort(np.argsort(rank, kind="stable")[:BALE_CELL_MAX])
        x, y, yaw, g, u, rank = x[keep], y[keep], yaw[keep], g[keep], u[keep], rank[keep]
    lon, lat = _to4326(x, y)
    n = len(lon)
    h = (BALE_H_M * (0.95 + 0.25 * u)).astype(np.float32)      # (1.2-1.5 m bales)
    return Items(lon, lat, h, np.full(n, 0.8, np.float32), np.full(n, KIND_BUSH, np.uint8), g, rank,
                 yaw.astype(np.float32), np.ones(n, np.float32))


def farm_foliage(feats, chm, blocked, bounds=None):
    """Shrubs in lines between the crop rows of land_use class farmland (surface.FARM_ROW_M): one line down
    the middle of each gap between the dirt furrows. Roads (blocked) are left bare, so a dirt track through a
    field is not planted. bounds (lon0, lat0, lon1, lat1) keeps a shrub in this cell."""
    if feats is None or len(getattr(feats, "kind", ())) == 0 or feats.attrs.get("class") is None:
        return _empty()
    import geodesy
    import surface as sf
    from polygon import Path
    cls = np.asarray(feats.attrs["class"]).astype(str)
    ids = getattr(feats, "ids", None)
    xs, ys, hs, yaws, ranks, us = [], [], [], [], [], []
    seen = set()
    row = sf.FARM_ROW_M
    for i in np.flatnonzero((np.asarray(feats.kind) == 3) & (cls == "farmland")):
        fid = ids[i] if ids is not None else i
        if str(fid) in seen:
            continue
        seen.add(str(fid))
        parts = feats.parts(int(i))
        if not parts or not parts[0] or len(parts[0][0]) < 3:
            continue
        ring = np.asarray(parts[0][0], np.float64)
        if len(ring) >= 2 and np.allclose(ring[0], ring[-1]):
            ring = ring[:-1]
        frame = geodesy.EnuFrame(float(np.mean(ring[:, 1])), float(np.mean(ring[:, 0])))
        e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], np.zeros(len(ring)))
        xy = np.column_stack([np.asarray(e, np.float64), np.asarray(n, np.float64)])
        area = abs(float(dnp.dot(xy[:, 0], np.roll(xy[:, 1], -1)) - dnp.dot(xy[:, 1], np.roll(xy[:, 0], -1)))) * 0.5
        if area < FARM_MIN_M2:
            continue
        c = xy.mean(axis=0)
        u = _orchard_axis(xy)
        v = np.array([-float(u[1]), float(u[0])])
        rel = xy - c
        span_a = float((dnp.matmul(rel, u)).max() - (dnp.matmul(rel, u)).min())
        span_b = float((dnp.matmul(rel, v)).max() - (dnp.matmul(rel, v)).min())
        along = FARM_BUSH_ALONG
        est = max(span_a, 1.0) / along * max(span_b, 1.0) / row
        while est > FARM_BUSH_POLY and along < 24.0:
            along += 4.0
            est = max(span_a, 1.0) / along * max(span_b, 1.0) / row
        phase = sf._field_phase(fid, xy)
        target = (phase + 0.5 * (sf.FARM_FURROW_M + row)) % row
        amin, amax = float((dnp.matmul(rel, u)).min()), float((dnp.matmul(rel, u)).max())
        bmin, bmax = float((dnp.matmul(rel, v)).min()), float((dnp.matmul(rel, v)).max())
        i0, i1 = math.floor(amin / along), math.ceil(amax / along)
        j0, j1 = math.floor((bmin - target) / row), math.ceil((bmax - target) / row)
        if i1 < i0 or j1 < j0 or i1 - i0 > 400 or j1 - j0 > 200:
            continue
        ii, jj = np.meshgrid(np.arange(i0, i1 + 1), np.arange(j0, j1 + 1), indexing="ij")
        pts = c + (ii.ravel() * along)[:, None] * u + (target + jj.ravel() * row)[:, None] * v
        inside = np.asarray(Path(xy).contains_points(pts), bool)
        pts = pts[inside]
        if len(pts) == 0:
            continue
        key = np.uint64(_fid_key(fid))
        jit = (_hash01(np.full(len(pts), key), np.arange(len(pts), dtype=np.uint64), 21) - 0.5) * 0.8
        pts = pts + jit[:, None] * u
        la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        if blocked is not None and np.any(blocked):
            x, y = _to3857(lo, la)
            cc = np.floor((x - chm.x0) / chm.dx).astype(np.int64)
            rr = np.floor((chm.y0 - y) / chm.dy).astype(np.int64)
            n_r, n_c = blocked.shape
            ok = (rr >= 0) & (rr < n_r) & (cc >= 0) & (cc < n_c)
            bad = np.zeros(len(pts), bool)
            bad[ok] = blocked[rr[ok], cc[ok]]
            la, lo = la[~bad], lo[~bad]
        if len(la) == 0:
            continue
        if bounds is not None:
            lon0, lat0, lon1, lat1 = bounds
            keep = (lo >= lon0) & (lo < lon1) & (la > lat0) & (la <= lat1)
            la, lo = la[keep], lo[keep]
        if len(la) == 0:
            continue
        uh = _hash01(np.full(len(la), key), np.arange(len(la), dtype=np.uint64), 22)
        yaw0 = math.degrees(dmath.atan2(float(u[1]), float(u[0])))
        xs.append(lo)
        ys.append(la)
        hs.append(1.3 + 0.7 * uh)
        yaws.append(yaw0 + (uh - 0.5) * 16.0)
        ranks.append(uh * 0.2)
        us.append(uh)
    if not xs:
        return _empty()
    lon, lat = np.concatenate(xs), np.concatenate(ys)
    height = np.concatenate(hs).astype(np.float32)
    yaw = np.concatenate(yaws).astype(np.float32)
    rank = np.concatenate(ranks).astype(np.float32)
    u = np.concatenate(us)
    if len(lon) > FARM_BUSH_CELL:
        keep = np.sort(np.argsort(rank, kind="stable")[:FARM_BUSH_CELL])
        lon, lat, height, yaw, rank, u = lon[keep], lat[keep], height[keep], yaw[keep], rank[keep], u[keep]
    group = np.full(len(lon), G_SHRUB, np.uint8)
    crop = np.full(len(lon), WC["crop"], np.uint8)
    group = regional(group, height, lat, lon, crop, u)
    return climate_shrubs(Items(lon, lat, height, (0.4 * height).astype(np.float32),
                                np.full(len(lon), KIND_BUSH, np.uint8), group, rank, yaw,
                                np.ones(len(lon), np.float32)), crop)


def _edge_axis(xy):
    """The way a field's rows run (vineyards, hay windrows): its dominant edge direction, a unit vector pointing east
    (north when north-south); earthfast.edge_axis (the principal axis of an odd shape ran across its rows:
    Saint-Emilion's parcels, 2026-11-10 aerial)."""
    fast.require(201, "edge_axis")
    return np.array(fast._ef.edge_axis(np.ascontiguousarray(xy, np.float64), EDGE_AXIS_DEG))


def _orchard_axis(xy):
    """Unit long-side axis of a ring in metres, pointing east, or north when it runs north-south."""
    p = np.asarray(xy, np.float64)
    if len(p) >= 2 and np.allclose(p[0], p[-1]):
        p = p[:-1]
    d = p - p.mean(axis=0)
    if len(d) < 2:
        return np.array([1.0, 0.0])
    cov = dnp.matmul(d.T, d)
    tr = float(cov[0, 0] + cov[1, 1])
    det = float(cov[0, 0] * cov[1, 1] - cov[0, 1] * cov[0, 1])
    lam = tr / 2.0 + math.sqrt(max(tr * tr / 4.0 - det, 0.0))
    if abs(float(cov[0, 1])) > 1e-8:
        vx, vy = lam - float(cov[1, 1]), float(cov[0, 1])
    elif float(cov[0, 0]) >= float(cov[1, 1]):
        vx, vy = 1.0, 0.0
    else:
        vx, vy = 0.0, 1.0
    nrm = math.hypot(vx, vy) or 1.0
    u = np.array([vx / nrm, vy / nrm])
    if u[0] < -1e-9 or (abs(u[0]) <= 1e-9 and u[1] < 0):
        u = -u
    return u


def _mask_polys(f, mask, chm, grow_m, kind=3, select=None):
    """Mark the polygons of Features f (buildings, water) on the CHM-grid mask (bool, C order): every pixel whose
    centre lies inside a part (its rings XOR'd, matplotlib's point-in-path test), within the part's box grown by
    grow_m (select: a bool per feature, only those). The native module's (earthfast mask_polys, 2026-10-23: the same
    pixels as the per-polygon Python loop it replaced, 13x faster: half of a tree cell's build)."""
    if f is None:
        return
    fast.require(62, "vegetation mask_polys")
    feats = np.flatnonzero((f.kind == kind) & (True if select is None else select)).astype(np.int64)
    if not len(feats):
        return
    fast._ef.mask_polys(mask.view(np.uint8), np.ascontiguousarray(f.lonlat, np.float64),
                        np.ascontiguousarray(f.coord_off, np.int64), np.ascontiguousarray(f.ring_off, np.int64),
                        np.ascontiguousarray(f.part_off, np.int64), feats, float(chm.x0), float(chm.y0),
                        float(chm.dx), float(chm.dy), grow_m / chm.dx)


def _mask_corners(road_cells, masks, chm, bounds, urban=None, widths=None, grow=ROAD_CLEAR_M):
    """Mark the rounded corners of the junctions among road_cells (road Features; roadcorners.pads, their arcs grown by
    grow m) that may reach the cell's bounds (lon0, lat0, lon1, lat1) on each CHM-grid mask of masks: a tree whose top
    lands on one is not moved off it (no road corridor pushes it: _build drops it), nor is a bush or rock scattered."""
    import mapgeom
    import roadcorners
    cells = [f for f in road_cells if isinstance(f, mapgeom.Features) and len(f)]
    if not cells:
        return
    try:
        pads = roadcorners.pads(cells, urban, widths, lonlat_bounds=tuple(bounds), grow=grow)
    except Exception as e:      # (the roads' masks alone)
        log.debug("vegetation: road corners: %s: %s", type(e).__name__, e)
        return
    if not pads:
        return
    feats = mapgeom.build("roadcorners", np.array([str(i) for i in range(len(pads))]),
                          [(mapgeom.POLYGON, [[np.c_[lo, la]]]) for la, lo in pads])
    for m in masks:
        _mask_polys(feats, m, chm, 0.0)


DECK_CLEAR_M = 2.0          # m past a bridge deck's edge (its sidewalks and parapets) no tree stands


def _mask_decks(f, mask, chm, specs):
    """Mark the bridge decks of road Features f on the CHM-grid mask: each bridge piece (roads.bridge_parts) its deck
    wide (roads.bridge_specs, and the sidewalks it carries: roads.deck_walks with specs(f), the ground widths) +
    DECK_CLEAR_M either side. A street tree whose crown is over a road moves to its edge (_mask_roads' push): over a
    deck that edge is the river or the bank below it, where the trees stood in the air or up through the deck."""
    if f is None or not len(f.kind):
        return
    import roads as roadsmod
    try:
        bspecs = roadsmod.bridge_specs(f)
    except Exception as e:              # (no bridge to keep clear: as _specs)
        log.debug("vegetation: bridge specs: %s: %s", type(e).__name__, e)
        return
    if not any(b is not None for b in bspecs):
        return
    ground = specs(f) if specs is not None else roadsmod.road_specs(f)
    n_r, n_c = mask.shape
    segs, hws = [], []
    for i, b in enumerate(bspecs):
        if b is None:
            continue
        walk = roadsmod.deck_walks(f, i, ground)
        half_m = b.width / 2 + (walk[0] if walk else 0.0) + DECK_CLEAR_M      # (one side's: as wide either side)
        for ring in roadsmod.bridge_parts(f, i):
            ring = np.asarray(ring, np.float64)
            if len(ring) < 2:
                continue
            x, y = _to3857(ring[:, 0], ring[:, 1])
            c, r = (np.asarray(x) - chm.x0) / chm.dx, (chm.y0 - np.asarray(y)) / chm.dy
            hw = half_m / (chm.dx * max(0.05, dmath.cos(math.radians(float(ring[:, 1].mean())))))
            if c.max() < -hw or r.max() < -hw or c.min() > n_c + hw or r.min() > n_r + hw:
                continue
            segs.append(np.stack([c[:-1], r[:-1], c[1:], r[1:]], axis=1))
            hws.append(np.full(len(c) - 1, hw))
    if segs:
        fast._ef.road_corridors(mask, None, np.ascontiguousarray(np.concatenate(segs), np.float64),
                                np.ascontiguousarray(np.concatenate(hws), np.float64))


def _mask_near(f, mask, chm, dist_m, kinds=(2, 3)):
    """Mark the CHM-grid pixels within dist_m (ground m) of Features f's lines and polygon outlines (water: its rivers'
    lines and its lakes' and the sea's shores; inside a polygon is water, masked as such). kinds: the feature kinds
    (2 lines, 3 polygons; (3,): the sea's and the lakes' shores, not the rivers)."""
    if f is None or not len(f.kind):
        return
    fast.require(63, "road_segments")
    sel = np.flatnonzero(np.isin(f.kind, kinds)).astype(np.int64)
    if not len(sel):
        return
    n_r, n_c = mask.shape
    segs, hws = fast._ef.road_segments(np.ascontiguousarray(f.lonlat, np.float64),
                                       np.ascontiguousarray(f.coord_off, np.int64),
                                       np.ascontiguousarray(f.ring_off, np.int64),
                                       np.ascontiguousarray(f.part_off, np.int64), sel,
                                       np.full(len(sel), float(dist_m)), float(chm.x0), float(chm.y0), float(chm.dx),
                                       float(chm.dy), n_r, n_c)
    if len(hws):
        fast._ef.road_corridors(mask, None, np.ascontiguousarray(segs), np.ascontiguousarray(hws))


def _mask_roads(f, mask, chm, push=None, clear=ROAD_CLEAR_M, specs=None, walks=False):
    """Mark road corridors (half width + clear) on the CHM-grid mask (footpaths left out): the half width the road is
    drawn with (specs(f): roads.road_specs; walks: with its sidewalks) or its class's, the wider. push (optional, (3, rows,
    cols) float32: dx, dy in px and the distance to the nearest road, inf where none) gets, per corridor
    pixel, the move that takes it just out of its nearest road's corridor (street trees: vegetation._build). With the
    class widths alone a city street's trees stood in its parking lanes: 136 of 536 trunks in Baltimore, 215 of 1358 in
    Seattle on a drawn carriageway (the user, 2026-10-28: "make sure no trees spawn on the road")."""
    got = _road_segs(f, mask.shape, chm, clear, specs, walks)
    if got is not None:
        fast._ef.road_corridors(mask, push, np.ascontiguousarray(got[0]), np.ascontiguousarray(got[1]))


def _road_segs(f, shape, chm, clear=ROAD_CLEAR_M, specs=None, walks=False, margins=None):
    """(segs (m, 4) c0 r0 c1 r1, hws (m,) px) of road Features f's carriageways (footpaths left out) on the CHM grid
    (shape: its rows, cols), each its half width (the drawn one, specs(f): roads.road_specs; walks: with its sidewalks;
    or its class's, the wider) + clear, or + margins[class] (margins: a dict, clear the default); None when it has
    none."""
    if f is None:
        return None
    n_r, n_c = shape
    cls = f.attrs.get("class", np.full(len(f.kind), "", object))
    fast.require(63, "road_segments")
    sel = [i for i in np.flatnonzero(f.kind == 2) if str(cls[i]) not in PATH_CLASSES]
    if not sel:
        return None
    sp = specs(f) if specs is not None else [None] * len(f.kind)
    import roads as roadsmod

    def drawn(s):       # the drawn half width, its sidewalks too (walks: the canopy's trees and the bushes keep off
        if s is None:   # them; the street trees, pushed to the curb, stand in pits at its edge: GitHub #424 / #425)
            return 0.0
        return s.width / 2 + (s.walk if walks and getattr(s, "walk", 0) and roadsmod.has_sidewalks(s) else 0.0)
    import racedig

    def margin(c):      # (BNE_RACEWAY_DIG: a racetrack's band kept clear, racedig.PROP_CLEAR_M)
        m = clear if margins is None else margins.get(c, clear)
        return max(m, racedig.PROP_CLEAR_M) if racedig.ON and c == "raceway" else m
    hw_m = np.array([max(ROAD_HALF_WIDTH_M.get(str(cls[i]), ROAD_HALF_DEFAULT_M), drawn(sp[i]))
                     + margin(str(cls[i])) for i in sel], np.float64)
    # every ring's segments on the CHM grid, its half width in grid pixels (a 3857 metre is 1 / cos(lat) of a ground
    # metre), rings off the grid left out (earthfast road_segments, 2026-10-23: the per-ring Python loop it
    # replaced gave the same segments); then the distances and the nearest road's push, without the GIL
    segs, hws = fast._ef.road_segments(np.ascontiguousarray(f.lonlat, np.float64),
                                       np.ascontiguousarray(f.coord_off, np.int64),
                                       np.ascontiguousarray(f.ring_off, np.int64),
                                       np.ascontiguousarray(f.part_off, np.int64), np.asarray(sel, np.int64), hw_m,
                                       float(chm.x0), float(chm.y0), float(chm.dx), float(chm.dy), n_r, n_c)
    return (segs, hws) if len(hws) else None


# --- tiles for the mod --------------------------------------------------------------------------------
TILE = 256.0                # m, as the road tiles (roads.tile_bbox)
TILE_ROWS_MAX = 6000        # items a tile sends at most, the lowest ranks: the most the mod shows of one (trees.lua
                            # TILE_CAP). Dense forest has 10-20k a tile: Bodø's 145 tiles held 1.66 M rows in the game's
                            # Lua (394 MB of heap, 1.25 s garbage collection pauses on a Steam Deck; GitHub #66)
SEND_M = 1280.0             # tiles this close to the player (Euclidean, to the square's nearest point) are sent
DROP_M = 1536.0             # ... and dropped beyond this
BUILD_PER_POSE = 8
TRACK_MOVE_M = 8.0          # a geotile tracker passes again once the car, camera or a focus moved this much ...
TRACK_MAX_S = 2.0           # ... or this long after its last pass, or something it sent, built or redoes changed


class JsonRows(str):
    """tile_items' rows already as their JSON text (as_json; write_tile writes it as is); n: how many; audio, biome:
    the tile's forest ambience (_ambience)."""
    n = 0
    audio = ()
    biome = (0, 0)


# The forest ambience (the mod's ambience.lua feeds the stock core_audioForest): the trees and bushes of a tile in
# squares of AUDIO_CELL_M (12 a tile; the stock nodes are ~20 m apart), sized by height as the stock classes (a third
# and two thirds of the 95th percentile's volume: 0.69 and 0.87 of its height, ~22 m)
AUDIO_CELL_M = 256.0 / 12
AUDIO_H_MID = 15.0
AUDIO_H_LARGE = 19.0
AUDIO_SOUND = [g for g in range(len(GROUPS)) if g not in (G_ROCK, G_DESERT_ROCK, G_VINE, G_VINE_ROW, G_DEADWOOD,
                                                         G_DEADWOOD_TROPICAL)
               + CLUMP_GROUPS + BALE_GROUPS]


def _ambience(group, e, n, z, height, lat, lon):
    """(audio, biome) of a tile's items: audio its sound nodes as one flat list of [x, y, z, depth, size, count] rows
    (earthfast.audio_nodes), biome its ambience set and variant (earthfast.audio_biome) and the Koppen class at the
    tile (climate.koppen_at: the ground cover's climate)."""
    fast.require(80, "audio_nodes")
    f64 = lambda a: np.ascontiguousarray(a, np.float64)     # noqa: E731
    g = np.ascontiguousarray(group, np.uint8)
    nodes = fast._ef.audio_nodes(g, f64(e), f64(n), f64(z), f64(height), AUDIO_SOUND, AUDIO_CELL_M, AUDIO_H_MID,
                                 AUDIO_H_LARGE)
    nodes[:, :4] = np.round(nodes[:, :4], 1)
    nodes[:, 4] = np.round(nodes[:, 4], 2)
    import climate
    k = int(climate.koppen_at(float(lat), float(lon)))
    biome = fast._ef.audio_biome(g, float(lat), float(lon), [G_DESERT_SHRUB, G_DESERT_ROCK, G_JUNIPER], k)
    # (47) the tile's Koppen class third: the mod's ground cover picks its grasses and flowers by it (groundcover.lua)
    return nodes.ravel().tolist(), list(biome) + [k]


def _no_items(as_json, lat, lon):
    if not as_json:
        return []
    out = JsonRows("[]")
    out.audio, out.biome = _ambience([], [], [], [], [], lat, lon)
    return out


# no tree or bush where the roads' grading moved the ground this much (a cutting's or an embankment's face: cleared in
# life; drawn on the terrain's 2 m squares the face is steeper than the exact ground the items stand on, a tree on it
# 2-10 m over or under the drawn slope: Pittsburgh's Mount Washington, 2026-10-31, .claude/devtools/floattrees.py).
# Rocks stay (sunk into the ground: _sink_rocks)
GRADED_CLEAR_M = 1.5


def _off_graded(it, e, n, zg, raw):
    """(it, e, n, zg) without the trees and bushes standing where the graded ground zg is GRADED_CLEAR_M or more off
    the DEM's (raw(lat, lon); None: all kept)."""
    if raw is None or not len(it):
        return it, e, n, zg
    zr = np.asarray(raw(it.lat, it.lon), np.float64)
    keep = (np.abs(zg - zr) < GRADED_CLEAR_M) | np.isin(it.group, (G_ROCK, G_DESERT_ROCK))
    if keep.all():
        return it, e, n, zg
    return it.take(keep), e[keep], n[keep], zg[keep]


def _capped(it):
    """The rows a tile sends (sorted indices): the TILE_ROWS_MAX lowest ranks of its single items, and the
    CLUMP_ROWS_MAX lowest of its far clumps beside them (they never show where the single ones do)."""
    clump = np.isin(it.group, CLUMP_GROUPS)
    out = []
    for sel, cap in ((np.flatnonzero(~clump), TILE_ROWS_MAX), (np.flatnonzero(clump), CLUMP_ROWS_MAX)):
        if len(sel) > cap:
            sel = sel[np.sort(np.argsort(it.rank[sel], kind="stable")[:cap])]
        out.append(sel)
    return np.sort(np.concatenate(out))


def tile_items(frame, tx, ty, veg, cell_ready, elev, sea=None, as_json=False, raw=None):
    """The items of anchor tile (tx, ty), each [group, x, y, z, yaw, scale, height, rank] in the anchor
    frame, the ground z from elev(lat, lon) (the DEM shaped as the block's ground: runways, carved
    water). None when a cell under it is not at hand yet (veg cells are built in the background). raw(lat, lon): the
    ground before the roads' grading (_off_graded)."""
    import roads as roadsmod
    x0, y0, x1, y1 = roadsmod.tile_bbox(tx, ty)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), np.zeros(4))
    keys = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))
    clat, clon = float(la.mean()), float(lo.mean())
    parts = []
    for k in keys:
        if not cell_ready(k):
            return None
        parts.append(veg.cell(k))
    items = parts[0] if len(parts) == 1 else _concat_all(parts)
    if len(items) == 0:
        return _no_items(as_json, clat, clon)
    e, n, _ = frame.geodetic_to_enu(items.lat, items.lon, np.zeros(len(items)))
    e, n = np.asarray(e), np.asarray(n)
    sel = (e >= x0) & (e < x1) & (n >= y0) & (n < y1)
    if not sel.any():
        return _no_items(as_json, clat, clon)
    it = items.take(sel)
    e, n = e[sel], n[sel]
    if sea is not None:                 # nothing in the water (lakes, rivers, sea: their carved beds)
        dry = sea.water(it.lat, it.lon)[0] <= 0
        it, e, n = it.take(dry), e[dry], n[dry]
    if len(it) == 0:
        return _no_items(as_json, clat, clon)
    if len(it) > TILE_ROWS_MAX:         # dense forest: the lowest ranks only (the mod shows no more of a tile)
        keep = _capped(it)
        it, e, n = it.take(keep), e[keep], n[keep]
    zg = np.asarray(elev(it.lat, it.lon), np.float64)
    it, e, n, zg = _off_graded(it, e, n, zg, raw)
    if len(it) == 0:
        return _no_items(as_json, clat, clon)
    _, _, z = frame.geodetic_to_enu(it.lat, it.lon, zg)
    z = _sink_rocks(frame, it, e, n, np.asarray(z, np.float64), elev)
    if as_json:
        # Rust: the same text json.dumps would write for the rows below, without a Python list per tree
        fast.require(0, "veg_rows_json")
        f64 = lambda a: np.ascontiguousarray(a, np.float64)     # noqa: E731
        out = JsonRows(fast._ef.veg_rows_json(np.ascontiguousarray(it.group, np.int64), f64(e), f64(n), f64(z),
                                               f64(it.yaw), f64(it.scale), f64(it.height), f64(it.rank)))
        out.n = len(it)
        out.audio, out.biome = _ambience(it.group, e, n, z, it.height, clat, clon)
        return out
    return [[int(g), round(float(a), 2), round(float(b), 2), round(float(c), 2), round(float(yw), 1),
             round(float(sc), 3), round(float(h), 1), round(float(r), 4)]
            for g, a, b, c, yw, sc, h, r in zip(it.group, e, n, np.asarray(z), it.yaw, it.scale, it.height, it.rank)]


def geo_tile_items(gx, gy, veg, cell_ready, elev, sea=None, as_json=False, raw=None):
    """The items of geotile (gx, gy) (geotiles.Z: the global grid, no anchor) in the tile's own frame (geotiles.frame:
    x, y of each at h = 0, z at its ground), as tile_items' rows, and the tile's pivot (the median of the items'
    z, m in that frame: the mod places the tile there, geotile.lua). Each item is in the one tile its lat / lon falls
    in (the grid partitions the Earth: no item twice, none lost, whatever the anchor). (rows, pivot), or None when
    its map cell is not at hand yet. raw(lat, lon): the ground before the roads' grading (_off_graded)."""
    import geotiles
    frame = geotiles.frame(gx, gy)
    key = mapcells.CellKey(*geotiles.tile_cell(gx, gy))
    clat, clon = geotiles.centre(gx, gy)
    if not cell_ready(key):
        return None
    items = veg.cell(key)
    if len(items) == 0:
        return _no_items(as_json, clat, clon), 0.0
    tx, ty = geotiles.tile_of(items.lat, items.lon)
    sel = (tx == gx) & (ty == gy)
    if not sel.any():
        return _no_items(as_json, clat, clon), 0.0
    it = items.take(sel)
    e, n, _ = frame.geodetic_to_enu(it.lat, it.lon, np.zeros(len(it)))
    e, n = np.asarray(e), np.asarray(n)
    if sea is not None:
        dry = sea.water(it.lat, it.lon)[0] <= 0
        it, e, n = it.take(dry), e[dry], n[dry]
    if len(it) == 0:
        return _no_items(as_json, clat, clon), 0.0
    if len(it) > TILE_ROWS_MAX:
        keep = _capped(it)
        it, e, n = it.take(keep), e[keep], n[keep]
    zg = np.asarray(elev(it.lat, it.lon), np.float64)
    it, e, n, zg = _off_graded(it, e, n, zg, raw)
    if len(it) == 0:
        return _no_items(as_json, clat, clon), 0.0
    _, _, z = frame.geodetic_to_enu(it.lat, it.lon, zg)
    z = _sink_rocks(frame, it, e, n, np.asarray(z, np.float64), elev)
    pivot = round(float(np.median(z)), 2)
    if as_json:
        fast.require(0, "veg_rows_json")
        f64 = lambda a: np.ascontiguousarray(a, np.float64)     # noqa: E731
        out = JsonRows(fast._ef.veg_rows_json(np.ascontiguousarray(it.group, np.int64), f64(e), f64(n), f64(z),
                                               f64(it.yaw), f64(it.scale), f64(it.height), f64(it.rank)))
        out.n = len(it)
        out.audio, out.biome = _ambience(it.group, e, n, z, it.height, clat, clon)
        return out, pivot
    return [[int(g), round(float(a), 2), round(float(b), 2), round(float(c), 2), round(float(yw), 1),
             round(float(sc), 3), round(float(h), 1), round(float(r), 4)]
            for g, a, b, c, yw, sc, h, r in zip(it.group, e, n, np.asarray(z), it.yaw, it.scale, it.height,
                                                it.rank)], pivot


ROCK_SINK = 0.35            # a rock sinks this share of its height per unit of ground slope (1 = 45 degrees) ...
ROCK_SINK_MAX = 0.6         # ... up to this share: on a steep slope its downhill side stood clear of the ground and
                            # its flat underside showed (Stelvio pass, the user's report 2026-10-17)
SLOPE_STEP_M = 1.5


def _sink_rocks(frame, it, e, n, z, elev):
    """z with the rocks lowered into sloping ground (ROCK_SINK)."""
    rock = (it.group == G_ROCK) | (it.group == G_DESERT_ROCK)
    if not rock.any():
        return z
    k = np.flatnonzero(rock)
    d = SLOPE_STEP_M
    ex = np.concatenate([e[k] + d, e[k] - d, e[k], e[k]])
    ny = np.concatenate([n[k], n[k], n[k] + d, n[k] - d])
    la, lo, _ = frame.enu_to_geodetic(ex, ny, np.zeros(len(ex)))
    _, _, zz = frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))
    zz = np.asarray(zz, np.float64).reshape(4, len(k))
    slope = dnp.hypot(zz[0] - zz[1], zz[2] - zz[3]) / (2 * d)
    z = z.copy()
    z[k] -= np.minimum(ROCK_SINK * slope, ROCK_SINK_MAX) * it.height[k]
    return z


def _concat_all(parts):
    out = parts[0]
    for p in parts[1:]:
        out = _concat(out, p)
    return out


FLAT_STRIDE = 8             # numbers an item in a flat tile: tile_items' row, in its order


def flat_json(rows):
    """tile_items' rows (a list, or its JsonRows text) as the JSON text of one flat array, FLAT_STRIDE numbers an item,
    the same numbers as json.dumps writes in the rows: no table per item in the game's Lua, which every garbage
    collection walks (trees.lua). The JsonRows text is flattened as text, without Python objects."""
    import json
    if isinstance(rows, JsonRows):
        return "[" + rows[2:-2].replace("],[", ",") + "]" if len(rows) > 2 else "[]"
    return json.dumps([v for r in rows for v in r], separators=(",", ":"))


def write_tile(out_dir, vfs_dir, aid, tx, ty, rows, flat=False, geo=None):
    """The tile's file: {"groups", "items": rows}, or with flat (a mod that reads it: hello.vegFlat) {"groups",
    "stride": FLAT_STRIDE, "flat": [...]} (flat_json) under its own name. (vfs path, items). geo: a geotile's
    {"key", "frame", "pivot"} (geo_tile_items; aid unused): in the file too, its name the key's (no anchor)."""
    import json
    stem = geo["key"] if geo is not None else f"{aid}_{tx}_{ty}"
    name = f"veg_{stem}.flat.json" if flat else f"veg_{stem}.json"
    path = os.path.join(out_dir, name)
    tmp = staging.tmp_for(path)
    with open(tmp, "w") as f:
        # (the tile's sound: the forest cloud's nodes and biome, ambience.lua; tile_items as_json's rows carry them)
        sound = {"audio": rows.audio, "biome": rows.biome} if isinstance(rows, JsonRows) else {}
        if geo is not None:             # (the tile's frame and pivot: the mod places it, geotile.lua)
            sound = dict(sound, frame=list(geo["frame"]), pivot=geo["pivot"])
        if flat:
            head = dict({"groups": list(GROUPS)}, **sound, stride=FLAT_STRIDE, flat=[])
            f.write(json.dumps(head, separators=(",", ":"))[:-3] + flat_json(rows) + "}")
        elif isinstance(rows, JsonRows):  # (tile_items as_json: the items' text, as json.dumps writes them)
            head = dict({"groups": list(GROUPS)}, **sound, items=[])
            f.write(json.dumps(head, separators=(",", ":"))[:-3] + rows + "}")
        else:
            f.write(json.dumps({"groups": list(GROUPS), "items": rows}, separators=(",", ":")))  # the C encoder
    staging.replace(tmp, path)
    return f"{vfs_dir}/{name}", rows.n if isinstance(rows, JsonRows) else len(rows)


class Tracker:
    """Per connection: which tiles of a kind the mod has (as roads.Tracker): vegetation tiles by default
    (messages veg_tile / veg_drop); the far buildings use one too (buildings.FAR_*)."""

    def __init__(self, send_m=SEND_M, drop_m=DROP_M, per_pose=BUILD_PER_POSE, kind="veg", skip_m=None, group=None,
                 unit=1, inner=None, geo=False, z=None, group_z=None, rebuild_history=True):
        # group G (the far buildings under the skyline tiles): every tile of a G x G group whose square comes within
        # send_m is wanted (a skyline tile shows only while none of its tiles do: no overlap, no hole). unit U (the
        # skyline tiles): keys are U x U squares of road tiles; inner (m, G): squares whose G-group comes within m are
        # not wanted (the far buildings' own groups)
        self.group, self.unit, self.inner = group, unit, inner
        self.aid = None
        self.sent = set()
        self.building = set()
        self.send_m, self.drop_m, self.per_pose, self.kind = send_m, drop_m, per_pose, kind
        # skip_m: tiles within this (as roads.tiles_near: a disc) are not built: the far buildings under the road
        # tiles, which carry the full buildings (the mod would hide them at once; ~36 of a teleport's ~196 tiles)
        self.skip_m = skip_m
        # its own threads (per_pose): in the loop's shared executor the tiles queued behind other trackers' builds
        # blocked on the road processes, which re-plan every road cell for a new anchor: after a rebase at 90 mph
        # not one tree tile came for minutes (2026-10-17, salt flats)
        self.pool = None
        self.futs = {}
        # geo: keys are geotiles (geotiles.Z, the global grid): what was sent stays sent across anchors (the mod
        # re-places it at a rebase, geotile.lua), the tiles near a point found by their quads in the anchor's world
        # (on_pose's frame); build(gx, gy) and the messages carry the tile's frame and pivot
        self.geo = bool(geo)
        # (geotiles of zoom z: geotiles.Z by default; group_z: whole groups, every tile under each zoom-group_z tile
        # that comes within send_m (the far buildings under the skyline's squares); inner (m, _): the tiles within m
        # not wanted (the skyline: where the far buildings' squares are, those being its own tiles))
        self.z, self.group_z = z, group_z
        self.frame = None
        self.rebuilt = 0            # tiles built again for a key sent before (diagnostics: 0 with geo)
        self.ever = set() if rebuild_history else None
        # (geotiles: redo_all, the tiles sent asked for again, sent again only where their file changed; digests: the
        # sent files' digests are kept for that, earthd's stale mode turns it on)
        self.redo, self.sent_h, self.digests = set(), {}, False
        self.epoch = 0              # (drop_all: a build started before it lands unwanted)
        self._gate = None           # last geotile pass's inputs, for the TRACK_MOVE_M gate

    def redo_all(self):
        """Every geotile sent is asked for again (earthd Connection.stale_run's swap: they may be an older version's);
        one whose file comes back the same is not sent."""
        if self.geo:
            self.redo = set(self.sent)
            self._gate = None

    def close(self):
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def forget(self):
        """The mod has none of the tiles sent (its level loaded again, earthd Connection.forget_world): every one is
        wanted again (those building land as new ones)."""
        self.sent = set()
        self.aid = None
        self._gate = None

    def progress(self):
        """(done, wanted) of the tiles round the car at the last pose (done: sent to the mod); (0, 0) before one."""
        fast.require(551, "tracker_progress")
        return fast._ef.tracker_progress(getattr(self, "car", ()), self.sent, None, None, None, None, None)

    def on_pose(self, conn, aid, x, y, loop, build, view=None, foci=(), frame=None, tree=None):
        """As roads.Tracker.on_pose: the tiles within send_m of the car (x, y) and of the camera when it is away
        (view), the car's first; dropped beyond drop_m of both. foci: other vehicles' collision foci (x, y, r, ...)
        whose trees collide: the tiles within r + 128 m of each, after the car's and camera's, kept within r + 256 m.
        tree: the car's time-to-reach tree (reach.Tree; geotiles only): the car's tiles in its order
        (reach.order_geotiles: right round the car, then along the way it goes) instead of nearest first."""
        from runtimeprofiler import TileJobs
        import geotiles
        fast.require(551, "scenery tile tracker")
        if self.geo and frame is None:
            return
        if aid != self.aid:
            if self.geo:
                self.frame = frame
            else:
                for f in self.futs.values():
                    f.cancel()
                self.sent, self.building, self.futs = set(), set(), {}
            self.aid = aid
        if self.pool is None:
            self.pool = TileJobs(self.per_pose, f"{self.kind}-{'geo' if self.geo else 'tile'}")
        view = tuple(view) if view else None
        start = functools.partial(self._start, conn, loop, build)
        if self.geo:
            z = self.z or geotiles.Z
            fast._ef.scenery_geo(self, aid, frame, (x, y), view, foci, tree, z, time.monotonic(),
                                 TRACK_MAX_S, TRACK_MOVE_M, start,
                                 functools.partial(self._drop_geo, conn, z))
        else:
            fast._ef.anchor_tiles(self, conn, aid, (x, y), view, foci, None, False, 0., start, None)

    def _start(self, conn, loop, build, key):
        out_dir = getattr(getattr(conn, "s", None), "out_dir", None)
        digest = self.geo and (self.digests or key in self.redo)
        job = functools.partial(staging.build_inline, build, out_dir, digest)
        if hasattr(getattr(conn, "s", None), "matslots") and self.kind in ("bldfar", "bldsky", "brfar", "bldpack", "bldhz"):
            job = functools.partial(staging.prepare_shapes, conn.s, self.kind, self.geo, self.aid, key, job)
        args = key if self.geo else (self.aid, *key)
        self.building.add(key)
        fut = self.futs[key] = loop.run_in_executor(self.pool, job, *args)
        if self.geo:
            fut.add_done_callback(lambda f, e=self.epoch: self._built_geo(conn, key, f, e))
        else:
            fut.add_done_callback(lambda f, a=self.aid: self._built(conn, a, key, f))

    def _drop_geo(self, conn, z, key):
        import geotiles
        self.sent.discard(key)
        self.sent_h.pop(key, None)
        name = geotiles.key(*key, z)
        conn.send({"type": f"{self.kind}_drop", "anchorId": self.aid, "key": name})
        gone = getattr(getattr(conn, "s", None), "geo_dropped", None)
        if callable(gone):
            gone(self.kind, key, name)

    def drop_all(self, conn):
        """Every geotile sent dropped (a drop message each) and the builds in flight left unwanted (their results are
        not sent): the kind turned off while its tiles show (the horizon ring at the Near building distance, Codex
        review: with no more poses its tiles were never dropped)."""
        import geotiles
        z = self.z or geotiles.Z
        self.epoch += 1
        for f in list(self.futs.values()):
            f.cancel()
        self.futs, self.building = {}, set()
        gone = getattr(getattr(conn, "s", None), "geo_dropped", None)
        for k in sorted(self.sent):
            conn.send({"type": f"{self.kind}_drop", "anchorId": self.aid, "key": geotiles.key(*k, z)})
            if callable(gone):
                gone(self.kind, k, geotiles.key(*k, z))
        self.sent, self.sent_h, self.redo, self.car = set(), {}, set(), set()

    def _built_geo(self, conn, key, fut, epoch=None):
        import geotiles
        if epoch is not None and epoch != self.epoch:
            return                      # (started before drop_all: unwanted)
        self.building.discard(key)
        self.futs.pop(key, None)
        if fut.cancelled():
            return
        try:
            res = fut.result()
        except Exception as e:
            log.debug("%s geotile %s: %s: %s", self.kind, key, type(e).__name__, e)
            return
        pre = res if isinstance(res, staging.Inlined) else None
        if pre is not None:
            res = pre.res
        if res is None:
            return
        path, n, frame, pivot = res[:4]
        shapes = res[4] if len(res) > 4 else None       # (the far geotiles': {what: shape file}, pooled paths below)
        h = pre.digest if pre is not None else None
        if h is None and (self.digests or key in self.redo):
            try:
                import hashlib
                with open(os.path.join(conn.s.out_dir, os.path.basename(path)), "rb") as f:
                    h = hashlib.sha1(f.read()).digest()
            except (OSError, AttributeError):
                pass
        fast.require(551, "scenery_result")
        if not fast._ef.scenery_result(self, key, h):
            return
        m = {"type": f"{self.kind}_tile", "anchorId": self.aid, "key": geotiles.key(*key, self.z or geotiles.Z),
             "path": path, "n": n, "frame": [float(frame[0]), float(frame[1])], "pivot": float(pivot)}
        s = getattr(conn, "s", None)
        out_dir = getattr(s, "out_dir", None)
        data = pre.data if pre is not None else staging.inline_json(out_dir, path) if out_dir else None
        if data is not None:
            m["data"] = data
        pool = pre.pool if pre and pre.prepared is not None else getattr(s, "pool_shapes", None)
        if shapes and pool is not None:
            slots = pool(conn, shapes)
            if slots:
                m["slots"] = slots
        conn.send(m)

    def _near(self, x, y, m, unit=None, group=None):
        """Keys (tiles, or unit x unit squares of tiles) whose square, or whose group's square, comes within m."""
        import roads as roadsmod
        fast.require(551, "tracker_near")
        return fast._ef.tracker_near(x, y, m, roadsmod.TILE, self.unit if unit is None else unit,
                                     self.group if group is None else group)

    def _dist(self, k, x, y):
        import roads as roadsmod
        fast.require(551, "tracker_distance")
        return fast._ef.tracker_distance(k, x, y, roadsmod.TILE, self.unit, self.group)

    def _built(self, conn, aid, key, fut):
        if aid != self.aid:                 # (an old anchor's: the same key may be in flight for the new one)
            return
        self.building.discard(key)
        self.futs.pop(key, None)
        if fut.cancelled():
            return
        try:
            res = fut.result()
        except Exception as e:
            log.debug("%s tile %s: %s: %s", self.kind, key, type(e).__name__, e)
            return
        pre = res if isinstance(res, staging.Inlined) else None
        if pre is not None:
            res = pre.res
        if res is None:                 # its cells not at hand yet: asked again on a later pose
            return
        path, n = res
        self.sent.add(key)
        m = {"type": f"{self.kind}_tile", "anchorId": aid, "key": f"{key[0]}_{key[1]}", "path": path, "n": n}
        out_dir = getattr(getattr(conn, "s", None), "out_dir", None)
        data = pre.data if pre is not None else staging.inline_json(out_dir, path) if out_dir else None
        if data is not None:                # (the game need not open the file: staging.inline_json)
            m["data"] = data
        pool = pre.pool if pre and pre.prepared is not None else getattr(getattr(conn, "s", None), "pool_shapes", None)
        if self.kind in ("bldfar", "bldsky", "brfar", "bldpack") and pool is not None:     # its mesh at a pooled shape path (matslots.py)
            slots = pool(conn, {"shape": f"{self.kind}_{aid}_{key[0]}_{key[1]}.cdae"})
            if slots:
                m["slots"] = slots
        conn.send(m)
