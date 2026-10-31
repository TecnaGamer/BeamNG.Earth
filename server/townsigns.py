"""Town entry and exit signs (the user, 2026-10-28: the sign of every town and village "quand on rentre dans leur
territoire"): where a paved road enters a commune's agglomeration, the commune's entry sign facing the traffic coming
in, on its kerb side, and on its back the commune's exit sign, which the traffic going out sees across the road (the
user, 2026-10-28: "collés dos contre dos comme dans la vraie vie"; the usual French way). France first (its EB10 entry
and EB20 exit signs, Instruction interministérielle sur la signalisation routière, 5e partie: they mark the
agglomération, where the buildings begin along the road, not the commune's administrative boundary); other countries'
rules come later (SIGN_COUNTRIES). Where its land has a regional language (Catalan, Occitan, Basque, Breton, Corsican,
Alsatian, West Flemish), a commune's signs add its name in it under the French one (REGIONAL, local_name).

A commune's agglomeration (agglomeration) is its built-up land as one area: the land cover's built-up class on a grid
of AGGLO_CELL_M, its gaps up to about twice CLOSE_M closed (gardens, squares, a small field between two streets) and
its pieces smaller than MIN_AGGLO_M2 left out (a farm, a few scattered houses: Boisset-et-Gaujac's every house had its
own pair of signs, 2026-10-28), the land it encloses filled in, and it reaches EDGE_M past its last built-up land
(a road along the last houses is in the town). A commune with no piece that large keeps its largest (the bourg of a
small village
under trees), one with no built-up land at all is its agglomeration whole (its signs at its boundary). The grid is laid
on latitude and longitude, not on the frame, so the area is the same for every anchor and tile. A point is in a
commune's agglomeration when it lies inside the commune (Overture divisions `locality`: mapdata layer "divisions") and
on its area. The signs stand where that state changes along a road of SIGN_CLASSES: where two towns have grown
together, each kerb has the sign of the town its traffic enters, the other town's exit on the back of the one across.

The state is a function of the point alone, never of the road segment or the tile, so every segment and every tile
puts a road's signs at the same places (ROADMAP "deterministic generation"). Pure given its inputs.

In the game (BNE_TOWNSIGNS=on, off by default): Towns.rows gives a road tile its signs (roads.build_tile's towns),
each sign two Forest items of its commune's own faces (write_face: the name drawn on the sign, a textured quad,
written once per session next to the tile): the entry face with the body every town sign shares (the mod's
sign_town_body: two posts and the plate's back, tools/generate_town_signs.py) and the exit face at the same spot turned
round, on the body's back. The tile names its faces ("townsigns": group -> shape, texture, material); the mod's
townsigns.lua makes each face's Material and puts face and body at one spot (trees.lua).
"""
import hashlib
import logging
import math
import os
import re
import threading
import time
from collections import OrderedDict, namedtuple
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import furniture
import polygon
from overture import LATIN_COLS, REGIONAL_LANGS

log = logging.getLogger("earthd.townsigns")

ON = os.environ.get("BNE_TOWNSIGNS", "off") == "on"
# the countries whose rules are written here (ISO 3166 alpha-2): France (EB10 / EB20), Belgium (F1a / F3a, the user
# 2026-10-29: "toute l'Europe", country by country, as the real signs are), Luxembourg (E,9a / E,9b), Germany
# (Zeichen 310 / 311), Switzerland (4.27 - 4.30), Italy (figura II 273 / 274), Austria (Hinweiszeichen 17a / 17b), the
# Netherlands (H1 / H2), Spain (S-500 / S-510), Portugal (N1a / N2a) and the PLAINS countries' (Czechia, Slovakia,
# Hungary, Slovenia, Denmark, Poland, Sweden, Finland: their kommuner and kunnat too large for the villages' way, the
# signs are their built-up land's, PICTOGRAMS), the Baltic and Balkan countries', Norway's and Iceland's
SIGN_COUNTRIES = {"FR", "BE", "LU", "DE", "CH", "IT", "AT", "NL", "ES", "PT", "CZ", "SK", "HU", "SI", "DK", "PL", "SE",
                  "FI", "EE", "LV", "LT", "RO", "BG", "GR", "RS", "MK", "HR", "BA", "NO", "IS", "ME", "AL", "XK", "MD",
                  "AD", "LI", "SM", "UA", "BY", "GB", "IE", "MT", "CY", "MC", "US", "CA", "MX",
                  "GT", "BZ", "SV", "HN", "NI", "CR", "PA", "CU", "DO", "HT", "JM", "PR", "BS", "TT", "BB", "LC", "VC",
                  "GD", "DM", "AG", "KN", "GP", "MQ", "AW", "AR", "BO", "PY", "UY", "BR", "CL", "CO", "PE", "EC", "VE",
                  "GY", "SR", "GF",
                  "SN", "ML", "BF", "NE", "CI", "GN", "BJ", "TG", "CM", "GA", "CG", "CD", "CF", "TD", "MG", "DJ", "KM",
                  "BI", "RW", "RE", "YT", "KE", "UG", "TZ", "NG", "GH", "SL", "LR", "GM", "ZA", "NA", "BW", "ZW", "ZM",
                  "MW", "LS", "SZ", "SO", "SS", "MU", "SC", "AO", "MZ", "GW", "CV", "ST", "MA", "DZ", "TN", "LY", "MR",
                  "GQ", "EG", "SD", "ET", "ER", "TR", "SY", "LB", "JO", "IQ", "SA", "KW", "QA", "OM", "GE", "AM",
                  "BH", "AE", "YE", "IL", "IR", "AZ", "XW", "UZ", "TM", "AF", "IN", "BD", "LK", "KR", "MM", "LA", "KH",
                  "TL", "ID", "MN", "BN", "AU", "PG", "FJ", "NC", "PF", "VU", "TO", "CN", "JP", "MY", "PK", "NP", "HK",
                  "PH", "BT", "MV", "KP", "SB", "WS", "GU", "VN", "RU", "KZ", "KG", "TJ", "TW", "TH", "NZ"}
# the territories' signs are their country's (no standard of their own found): the British ones' and the Crown
# dependencies' the United Kingdom's, Western Sahara's Morocco's, the Pacific's free associated states' and
# Australia's islands' Australia's green; Singapore and Macau have no town of their own to sign, Antarctica none
# at all (territory -> its country)
TERRITORIES = {**{cc: "GB" for cc in ("IM", "JE", "GG", "GI", "BM", "KY", "VG", "AI", "MS", "TC", "FK", "SH", "IO",
                                      "PN")},
               **{cc: "US" for cc in ("VI", "AS", "MP")}, **{cc: "FR" for cc in ("BL", "MF", "PM", "WF")},
               **{cc: "NL" for cc in ("CW", "SX", "BQ")}, "AX": "FI", **{cc: "DK" for cc in ("FO", "GL")}, "SJ": "NO",
               "VA": "IT", "EH": "MA",
               **{cc: "AU" for cc in ("NF", "CX", "CC", "CK", "NU", "TK", "FM", "MH", "PW", "NR", "TV", "KI")}}
SIGN_COUNTRIES |= set(TERRITORIES)
# paved roads that go into a town: no motorway or trunk (France posts no EB10 on its motorways and dual carriageways;
# their exits lead to the roads that have one), no link, service road or track
SIGN_CLASSES = {"primary", "secondary", "tertiary", "unclassified", "residential"}
# the classes that are main roads where a country's sign differs on them (MAIN_SIGNS: Switzerland's blue Ortstafel
# is the Hauptstrassen', the white one the Nebenstrassen')
MAIN_CLASSES = {"primary"}
MAIN_SIGNS = {"CH", "LI", "IE"}            # (Ireland's: green on a national road, white on the others)
G_ENTRY, G_EXIT = "townentry", "townexit"
M_PER_DEG = 111320.0
AGGLO_CELL_M = 20.0             # m, the agglomeration grid's step (the land cover's pixels are 10 m)
AGGLO_MARGIN_M = 400.0          # m around a commune its grid reaches (a town across the boundary is one area)
CLOSE_M = 60.0                  # m the built-up land is grown and shrunk back by: gaps up to ~2x closed
OPEN_M = 20.0                   # m the closed area is shrunk and grown back by: strips up to ~3 cells wide out
MIN_AGGLO_M2 = 50000.0          # m2: a smaller piece of built-up land is no agglomeration (5 ha)
# m the area reaches past its last built-up land: a road along a town's last houses is in it (Val-Cenis' street along
# the Arc, 30-60 m from Lanslebourg's houses, went in and out of the area in 100-200 m stretches: 10 signs, 2026-10-28),
# and the entry sign stands a little before the first houses, as on the real roads
EDGE_M = 40.0
STEP_M = 10.0                   # m between the points a road's state is read at
REFINE = 5                      # halvings of a step where the state changes: the sign within STEP_M / 32
# m: a stretch of a road segment in or out of an agglomeration shorter than this is not one (a road that runs along
# the area's edge goes in and out of it: Tornac's D982, 50 signs in 600 m, 2026-10-28); stretches at a segment's
# ends are kept, their length unknown
MIN_RUN_M = 100.0
# m: ... and a stretch out of every agglomeration between two of the same commune's is a gap in it below this (a field
# or a river bank between a village's parts: Val-Cenis' road along the Arc left and entered Lanslebourg every 100-150 m)
MAX_GAP_M = 300.0
SIGN_OFF_M = 1.5                # m beyond the carriageway's edge (furniture.py's speed-limit signs' until 1.7.1)
# The sign (metres, its local frame: x across the road toward the kerb side, -y toward the traffic it faces, z up):
# a plate PLATE_W x PLATE_H (a French EB10 of the normal range is 1.3 m to 1.6 m long, IISR 5e partie) on two posts,
# its foot PLATE_Z0 above the road. SIGN_H, the model's height (the posts' tops), is the height of the rows and of the
# mod's catalog entry (scale 1). Keep in step with tools/generate_town_signs.py and the mod's townsigns.lua.
PLATE_W, PLATE_H, PLATE_Z0 = 1.3, 0.6, 1.4
SIGN_H = 2.1
# country -> its plate's width (m), PLATE_H high, where its sign's proportions are not France's: its own body
# (tools/generate_town_signs.py PLATES, the mod's sign_town_body_<country>) and face groups 'townin_<country>_...'
# (Belgium's F1a: 1.67 wide for 1 high; Luxembourg's E,9a 2.9; Germany's Zeichen 310 1.5; Switzerland's 4.27 1.4;
# Italy's figura II 274 3.35; Austria's 17a 1.5; the Netherlands' H1 3.41; Spain's S-500 3; Portugal's N1a 2.65;
# PLAINS' about theirs: Wikimedia Commons' drawings)
PLATES = {"BE": 1.0, "LU": 1.74, "DE": 0.9, "CH": 0.84, "IT": 2.0, "AT": 0.9, "NL": 2.0, "ES": 1.8, "PT": 1.6,
          "CZ": 1.2, "SK": 1.0, "HU": 1.6, "SI": 1.0, "DK": 1.2, "SE": 1.2, "FI": 1.0, "PL": 1.0, "EE": 1.0,
          "LV": 1.6, "LT": 1.6, "RO": 1.4, "BG": 1.2, "MK": 1.0, "RS": 1.4, "GR": 1.4, "HR": 1.2, "BA": 1.2,
          "NO": 1.4, "IS": 1.0, "ME": 1.2, "AL": 1.4, "XK": 1.2, "MD": 1.2, "AD": 1.8, "LI": 0.84, "SM": 2.0,
          "UA": 1.6, "BY": 1.2, "GB": 1.6, "IE": 1.6, "MT": 1.0, "CY": 1.4, "US": 1.2, "CA": 1.4, "MX": 1.8,
          **{cc: 1.8 for cc in ("GT", "BZ", "SV", "HN", "NI", "CR", "PA", "DO", "PR", "JM", "BS", "TT", "BB", "LC",
                                "VC", "GD", "DM", "AG", "KN")}, "CU": 1.4, "HT": 1.4, "AW": 2.0,
          **{cc: 1.8 for cc in ("AR", "BO", "PY", "UY", "BR", "CL", "CO", "PE", "EC", "VE", "GY", "SR")},
          **{cc: 1.6 for cc in ("KE", "UG", "TZ", "NG", "GH", "SL", "LR", "GM", "ZA", "NA", "BW", "ZW", "ZM", "MW",
                                "LS", "SZ", "SO", "SS", "MU", "SC", "AO", "MZ", "GW", "CV", "ST")}, "GQ": 1.8,
          **{cc: 1.4 for cc in ("MA", "DZ", "TN", "LY", "MR", "EG", "SD", "ET", "ER")},
          **{cc: 1.4 for cc in ("TR", "SY", "LB", "JO", "IQ", "SA", "KW", "QA", "OM", "GE", "AM", "BH",
                                "AE", "YE", "IL", "IR", "AZ", "XW")},
          **{cc: 1.4 for cc in ("UZ", "TM", "AF", "IN", "BD", "LK", "KR", "MM", "LA", "KH", "TL", "ID", "MN", "BN",
                                "AU", "PG", "FJ", "VU", "TO", "CN", "JP", "MY", "PK", "NP", "HK", "PH",
                                "BT", "MV", "KP", "SB", "WS", "GU", "VN", "RU", "KZ", "KG", "TJ", "TW", "TH", "NZ")}}
PLATES.update({cc: PLATES[p] for cc, p in TERRITORIES.items() if p in PLATES})
# m: the face's plane, in front of the body's plate (its front at -0.006); the exit face, the same model turned round,
# is as far behind it (its back at +0.006)
FACE_Y = -0.012
# the face texture's resolution: a face is kept for the session, one per commune and kind driven past (townsigns.lua),
# 520 x 240 for France's plate, a quarter of 800's (its letters as sharp at the distance a sign is read)
FACE_PX_PER_M = 400
FACE_RED = (204, 27, 27)        # the speed-limit signs' red (tools/generate_kmh_speed_signs.py)
FACE_WHITE = (247, 247, 247)
BORDER_FRAC = 0.07              # the red border's width, a share of the plate's height
MARGIN_FRAC = 0.025             # the white edge outside it
BAR_FRAC = 0.07                # the exit sign's red bar, a share of the plate's height
TEXT_FRAC = 0.40                # the name's capitals' height, a share of the plate's height (one line)
LOCAL_FRAC = 0.75               # the regional name's capitals, a share of the French name's (0.62 was hard to read
                                # from 8 m in game, Thuir 2026-10-28)
LOCAL_GAP = 0.45                # the space above the regional name, a share of the French name's capitals
LOCAL_LINE = 22                 # letters a regional name has on one line (longer: two, at a hyphen)

# The regional name under the French one (the user, 2026-10-28: in French Catalonia the sign gives the village's name
# in French, then in Catalan; the same for Occitan and France's other regional languages). Which language a commune's
# signs add is the language of its land, not any name the data has (Overture's names.common gives Pau a Basque name
# and Toulouse a Catalan one, both exonyms no sign there writes): its département's (the Overture county a point
# inside the commune lies in), in order, the first the commune has a name in that is not its French name. Departments
# not listed add none (their signs are French only, or their language's names are not in the data: Gallo, Arpitan,
# the Moselle's Platt). Measured on release 2026-09-23.1: names in the language for 44 of Perpignan's 55
# neighbouring localities, 1111 of 1417 around Quimper, 63 of 96 around Nîmes, 9 of 31 around Strasbourg.
_OC = ("oc",)
REGIONAL = {
    "Pyrénées-Orientales": ("ca",),
    "Corse-du-Sud": ("co",), "Haute-Corse": ("co",),
    "Finistère": ("br",), "Côtes-d'Armor": ("br",), "Morbihan": ("br",), "Ille-et-Vilaine": ("br",),
    "Loire-Atlantique": ("br",),
    "Bas-Rhin": ("gsw",), "Haut-Rhin": ("gsw",),
    # the Béarn's Occitan first; the Soule's villages have Basque names only (the Basque Country's other
    # arrondissement: REGIONAL_LOCALADMIN)
    "Pyrénées-Atlantiques": ("oc", "eu"),
    "Ariège": _OC, "Aude": _OC, "Aveyron": _OC, "Gard": _OC, "Haute-Garonne": _OC, "Gers": _OC,
    "Hautes-Pyrénées": _OC, "Hérault": _OC, "Lot": _OC, "Lozère": _OC, "Tarn": _OC, "Tarn-et-Garonne": _OC,
    "Alpes-de-Haute-Provence": _OC, "Hautes-Alpes": _OC, "Alpes-Maritimes": _OC, "Bouches-du-Rhône": _OC, "Var": _OC,
    "Vaucluse": _OC, "Drôme": _OC, "Ardèche": _OC, "Cantal": _OC, "Haute-Loire": _OC, "Puy-de-Dôme": _OC,
    "Dordogne": _OC, "Gironde": _OC, "Landes": _OC, "Lot-et-Garonne": _OC, "Corrèze": _OC, "Creuse": _OC,
    "Haute-Vienne": _OC,
}
# (département, Overture localadmin: the arrondissement) -> its languages, before REGIONAL's: the Labourd and Basse-
# Navarre (arrondissement of Bayonne) are Basque; French Flanders' West Flemish is the Westhoek's, the arrondissement
# of Dunkerque (Lille has a Flemish name, Rysel, its signs none: every French departement checked 2026-10-29)
REGIONAL_LOCALADMIN = {("Pyrénées-Atlantiques", "Bayonne"): ("eu", "oc"), ("Nord", "Dunkerque"): ("vls",)}

# A commune: its name on the signs, its parts in frame metres and in lon/lat (each a list of rings, the outer one
# first, then its holes), its agglomeration (Agglo; None: the whole commune), its Overture id (None in tests), its
# name in its regional language ("": none, REGIONAL), its country (ISO 3166 alpha-2: which sign it has) and, for a
# village of a VILLAGE_COUNTRIES commune, its commune's villages (sites: (k, 2) lon/lat, by id) and which it is (site):
# its land is the part of the commune nearer its point than the others' (nearest_site); the Overture county it lies in
# and the small line its sign writes under its names ("": none, split_villages); the town it is part of (group: the
# id of its German Gemeinde; None: itself), its villages one town for the signs (_changes); the name of the commune
# a village was split from (its comune's: with_agglomerations' CENTRES); the lon/lat parts its land is limited to (a
# PICTOGRAMS square's: its country's counties; None: all of it)
Commune = namedtuple("Commune", "name parts lonlat agglo fid local country sites site county sub group town within",
                     defaults=(None, "", "FR", None, -1, "", "", None, "", None))
# country -> the Overture area subtype that is its communes, whose signs name the villages in them (Overture's
# `locality` points, mapdata layer "division_points"): Luxembourg's 100 communes are counties, its 682 villages points
# (its E,9a names the village: HOWALD in the commune of Hesperange); Germany's Ortstafel names the Ortsteil, and its
# locality areas are Gemeinden, some of their Ortsteile (Assmannshausen in Rüdesheim am Rhein) and Samtgemeinden around
# Gemeinden, while 66,159 of its villages are points only (2026-10-29): every area is split by the points in it, the
# smaller areas first (state: a village with its own area is its area's); Switzerland's 2,119 Gemeinden are localadmins,
# its Ortschaften (the Ortstafel's: Wengen in Lauterbrunnen) 10,872 points, 564 of them with an area too; Italy's 8,575
# locality areas are its comuni (and a few frazioni), 58,099 frazioni points only (Marina di Pisa in Pisa): its centro
# abitato signs name the frazione Austria's are Germany's: 2,226 Gemeinden areas, 20,229 Ortschaften points, its
# Statutarstädte counties (Linz); Spain's 8,655 locality areas its municipios and, around them, comarcas (Baix Camp
# around Reus, nothing in the layer tells them apart: NESTED), 60,263 núcleos points only (Puerto Lope in Moclín)
# Portugal's 308 municípios are counties (its locality areas their freguesias), its localidades points (Cartaxo);
# Czechia's and Slovakia's obce localadmins, their parts points; Denmark's kommuner counties, their towns and villages
# points; Poland's gminy localities with its villages' areas in them (as Spain's comarcas: NESTED) and more villages as
# points; Slovenia's občine regions, its 6,821 naselja points; Estonia's vallad, Lithuania's savivaldybės, Romania's
# comune, Bulgaria's obshtini and Greece's dimoi counties, Latvia's novadi regions, their villages points; Serbia's
# settlements areas in its municipalities' (NESTED); Norway's kommuner and Iceland's sveitarfélög counties (Bergen,
# Kópavogsbær), their places points (Gyldenpris, Kópavogur); Bosnia's općine / opštine localities (Mostar 1,152 km2,
# Bijeljina), their villages points. (Hungary's 3,161 települések, North Macedonia's 1,746 settlements and Croatia's
# naselja are areas: as France's, DISTRICT_AREAS.) Albania's bashki and Kosovo's komuna counties, their villages points;
# Montenegro's opštine, Andorra's parishes, Liechtenstein's Gemeinden and San Marino's castelli regions, their places
# points (Montenegro's locality areas Bar's quarters, Bjeliši 1 km2). (Moldova's localities are areas: as France's.)
# Ukraine's hromady localities, a city's own area in its hromada's (NESTED: Вінниця in Вінницька міська громада), their
# villages points; Belarus' selsoviets localities, their villages points, its cities counties of their own
# (CITY_COUNTIES: Баранавічы beside Баранавіцкі раён). Great Britain's counties and unitary authorities (Cambridgeshire,
# Highland), Ireland's counties (County Cork: Overture regions), Malta's regions (Reġjun tat-Tramuntana: localities),
# Cyprus' districts and Monaco (regions), their places points (Cambridge, Macroom, Il-Mosta, Πάφος, Monaco); each place
# its own town, as France's communes (no BY_COMMUNE: a British county is no town). The United States' cities and
# townships localities (Columbus, Orange township), their places points in them (Lewis Center: NESTED); Canada's towns
# localities (Amos), its single-tier cities counties (Kingston: CITY_COUNTIES); Mexico's municipios counties (León,
# Municipio de Querétaro), their localities points. Central America's, Cuba's and the Dominican Republic's municipios
# counties too (Distrito de Panamá, Comayagua), Haiti's communes localadmins, the islands' parishes and Puerto Rico's
# municipios regions (Saint Andrew, Ponce), their places points; Belize's, Guadeloupe's, Martinique's and Aruba's towns
# areas (as France's). Argentina's, Paraguay's and Uruguay's towns areas too (Córdoba, Luque); Brazil's and Bolivia's
# municipios and Chile's comunas localities with their places points (Campinas, Limache); Colombia's, Ecuador's and
# Venezuela's municipios and cantones, Peru's provinces, the Guianas' regions and communes counties with their places
# points (Tunja, Arequipa). Africa's: most countries' districts, departments or cercles counties with their places
# points (Kitwe District, Cercle de Ségou), Libya's, Djibouti's, the Seychelles' and Mayotte's regions with their places
# points; Morocco's, Algeria's, Tunisia's, Egypt's, Kenya's, South Africa's, Mauritius' and Réunion's towns areas
# (Meknès, Stellenbosch; Kenya's wards in a town's area its districts: DISTRICT_AREAS).
# Africa's countries whose districts, departments or cercles are counties (the note above)
AFRICA_DISTRICTS = ("SD", "SS", "ER", "ET", "SO", "UG", "TZ", "BI", "CD", "CG", "GA", "CM", "CF", "TD", "NE", "NG",
                    "BJ", "TG", "GH", "CI", "BF", "ML", "SN", "GM", "GW", "GN", "SL", "LR", "MR", "GQ", "AO", "ZM",
                    "ZW", "MW", "MZ", "BW", "LS", "SZ", "MG", "CV", "ST", "KM", "RW",
                    "NA")                     # (Namibia: Swakopmund a point only)
VILLAGE_COUNTRIES = {"LU": "county", "DE": "locality", "CH": "localadmin", "IT": "locality", "AT": "locality",
                     "ES": "locality", "PT": "county", "CZ": "localadmin", "SK": "localadmin", "DK": "county",
                     "PL": "locality", "SI": "region", "EE": "county", "LV": "region", "LT": "county",
                     "RO": "county", "BG": "county", "GR": "county", "RS": "locality", "NO": "county", "IS": "county",
                     "BA": "locality", "AL": "county", "XK": "county", "ME": "region", "AD": "region", "LI": "region",
                     "SM": "region", "UA": "locality", "BY": "locality", "GB": "county", "IE": "region",
                     "MT": "locality", "CY": "region", "MC": "region", "US": "locality", "CA": "locality",
                     "MX": "county", "GT": "county", "SV": "county", "HN": "county", "NI": "county",
                     "CR": "county", "PA": "county", "CU": "county", "DO": "county", "HT": "localadmin",
                     **{cc: "region" for cc in ("JM", "PR", "BS", "TT", "BB", "LC", "VC", "GD", "DM", "AG", "KN")},
                     "BR": "locality", "BO": "locality", "CL": "locality",
                     **{cc: "county" for cc in ("CO", "PE", "EC", "VE", "GY", "SR", "GF")},
                     **{cc: "region" for cc in ("LY", "DJ", "SC", "YT")},
                     **{cc: "county" for cc in AFRICA_DISTRICTS},
                     **{cc: "county" for cc in ("TR", "SY", "LB", "JO", "IQ", "SA", "KW", "QA", "OM", "GE", "AM")},
                     "BH": "region", "AE": "region", "YE": "locality", "IL": "locality",
                     # (Asia's and Oceania's: the note above)
                     **{cc: "county" for cc in ("UZ", "TM", "AF", "IN", "BD", "LK", "KR", "MM", "LA", "KH", "TL", "ID",
                                                "MN", "BN", "AU", "PG", "FJ", "NC", "PF", "VU", "TO")},
                     **{cc: "locality" for cc in ("JP", "MY", "PK", "NP", "HK", "PH")}, "CN": "localadmin",
                     **{cc: "region" for cc in ("BT", "MV", "KP", "SB", "WS", "GU", "VN")}}
# the countries whose villages of one commune grown together are one town, no sign between them (_towns): Germany's,
# Austria's and Italy's by their Gemeinde or comune (town_of), the others by the commune they were split from
# (BY_COMMUNE)
BY_COMMUNE = {"CH", "ES", "PT", "CZ", "SK", "DK", "PL", "SI", "EE", "LV", "LT", "RO", "BG", "GR", "RS", "NO", "IS",
              "BA", "AL", "XK", "ME", "AD", "LI", "SM", "UA", "BY", "US", "CA", "MX",
              "GT", "SV", "HN", "NI", "CR", "PA", "CU", "DO", "HT", "PR", "BR", "BO", "CL", "CO", "PE", "EC", "VE",
              "GY", "SR", "GF", "LY", "DJ", "SC", "YT", "TR", "SY", "LB", "JO", "IQ",
              "SA", "KW", "QA", "OM", "GE", "AM", "BH", "AE", "YE",
              "UZ", "TM", "AF", "IN", "BD", "LK", "KR", "MM", "LA", "KH", "TL", "ID", "MN", "BN", "AU", "PG", "FJ",
              "NC", "PF", "VU", "TO", "CN", "JP", "MY", "PK", "NP", "HK", "PH", "BT", "MV", "KP", "SB", "WS", "GU",
              "VN"} | set(AFRICA_DISTRICTS)
GROUPED = {"DE", "IT", "AT"} | BY_COMMUNE
# the countries whose locality areas nest: a village point in a smaller area is that area's, and an area all of whose
# points are smaller areas' is no town (Spain's comarcas, Poland's gminy around their villages' areas, Serbia's
# municipalities around its settlements' areas: Угао's in Сјеница's)
NESTED = {"ES", "PL", "RS", "UA", "US", "CA", "IL"}
# the countries whose locality areas within another are its districts, no town of their own (Zagreb's Gornji grad -
# Medveščak and Maksimir in Zagreb's area, Split's gradski kotari in Split's, Meknès' Al Machouar Stinia): left out
DISTRICT_AREAS = {"HR", "KE", "MA", "TN", "EG", "AZ", "RU", "KZ", "KG", "TJ", "TW", "TH"}
# the countries whose cities are counties of their own (kreisfreie): Germany's kreisfreie Städte, Austria's
# Statutarstädte, Belarus' cities of their voblasts, Canada's single-tier cities
CITY_COUNTIES = {"DE", "AT", "BY", "CA"}
# country -> {region (ISO 3166-2) -> the style its signs have there (village_sub carries it to the face, Commune.sub)}:
# Ontario's community signs blue, the other provinces' green
REGION_STYLES = {"CA": {"CA-ON": "blue"}}
# Swiss locality points that are no place: Ticino's circoli and St. Gallen's Kreise, districts of villages (Circolo
# della Maggia over 130 of them)
DISTRICT_POINTS = re.compile(r"(Circolo|Kreis) ")
# locality points that are a municipality's, not a place: a Ukrainian hromada's ("Вінницька міська громада", beside
# Вінниця's own point), a Belarusian selsoviet's ("Гаранскі сельскі Савет"); nor is an industrial estate ("Paphos
# Industrial area", Barbados' "Dayrells Commercial Park")
MUNICIPALITY_POINTS = re.compile(r"(громада|сельскі Савет|сельсовет|Industrial (area|estate|zone)|"
                                 r"(Industrial|Commercial|Business) Park| ward| sublocation|Municipal District|"
                                 r" LC I{1,3})$"                      # (Kenya's sublocations, Uganda's LC I cells)
                                 r"|^Phường\s|^District \d+$", re.IGNORECASE)   # (Huế's wards, Baguio's districts)
# ... and the areas that are no town: an Iranian rural district (دهستان فجر, around Yazd), an Israeli regional council
# (מועצה אזורית שומרון)
# Russia's municipal settlements and districts ("Селецкое сельское поселение", "Центральный район"), a Kazakh
# district (Әулиеата ауданы), a Philippine city's ("District 10")
AREA_NOT_PLACE = re.compile(r"^(دهستان|מועצה אזורית|сельское поселение)\s|поселение$|\sрайон$|ауданы$"
                            r"|^District \d")
# ... nor, in Spanish-speaking America, a neighbourhood, an estate or a farm: their place the town's around them (the
# nearest real place's part of the commune: split_villages). "Colonia Bendeck" in Tegucigalpa, "Zona 2" in Guatemala
# City, "Condominio Residencial Portal de la Escalón" in San Salvador, "Finca La Joya" (2026-10-31)
NEIGHBOURHOOD_POINTS = re.compile(r"^(Colonia|Col\.|Barrio|Bo\.|Barriada|Residencial|Condominio|Urbanizaci[oó]n|"
                                  r"Urb\.|Lotificaci[oó]n|Reparto|Fraccionamiento|Finca|Hacienda|Zona|Sector|"
                                  r"Comunidad|Asentamiento|Proyecto|Conjunto|Municipal \d|"
                                  r"Bairro|Loteamento|Condom[ií]nio)\b"     # (Brazil's too)
                                  r"|^[IVX]+$"                          # (Managua's districts: I, IV)
                                  r"|^(حي|ناحیه|تحصیل)\s", re.IGNORECASE)   # (حي السعادة, Irbid; ناحیه اول, Herat)
NEIGHBOURHOOD_COUNTRIES = {"MX", "GT", "BZ", "SV", "HN", "NI", "CR", "PA", "CU", "DO", "PR", "BR", "BO", "CL", "CO",
                           "PE", "EC", "VE", "SY", "LB", "JO", "IQ", "SA", "KW", "BH", "QA", "AE", "OM", "YE", "AF",
                           "PK"}
# a Swiss name's canton, after a homonym's name ("Colombier (VD)", "Reinach BL"): the Ortstafel writes it under the
# name, small
CANTONS = "AG AI AR BE BL BS FR GE GL GR JU LU NE NW OW SG SH SO SZ TG TI UR VD VS ZG ZH"
CANTON = re.compile(r"^(.*\S)\s+\(?(" + "|".join(CANTONS.split()) + r")\)?$")
# an Italian name with both its languages' ("Bolzano - Bozen", "San Giovanni al Natisone / San Zuan dal Nadison",
# Sardinia's "Casteddu/Cagliari"): one line each on the sign
BILINGUAL = re.compile(r"\s*/\s*| - ")
# Germany's locality areas that are associations of Gemeinden, not a town (their name says so)
ASSOCIATIONS = re.compile(r"(Samtgemeinde|Verbandsgemeinde|Verwaltungsgemeinschaft|Gemeindeverwaltungsverband|"
                          r"Verwaltungsverband|Amt) ")
# country -> its villages' second name: {county (its German name, before " - ") or "": languages, in order}; Germany's
# Sorbian (Lower in Brandenburg's Lusatia, Upper in Saxony's) and North Frisian bilingual Ortstafeln
VILLAGE_LANGS = {"LU": {"": ("lb",)},
                 "DE": {"Oberspreewald-Lausitz": ("dsb",), "Spree-Neiße": ("dsb",), "Dahme-Spreewald": ("dsb",),
                        "Cottbus": ("dsb",), "Bautzen": ("hsb",), "Görlitz": ("hsb",),
                        "Kreis Nordfriesland": ("frr",)},
                 # Friuli's bilingual signs (Udine / Udin; the names Overture has both of already, "San Giovanni al
                 # Natisone / San Zuan dal Nadison", as they are) and Sardinia's (Cagliari / Casteddu)
                 "IT": {"Udine": ("fur",), "Pordenone": ("fur",), "Gorizia": ("fur",),
                        "Cagliari": ("sc",), "Sassari": ("sc",), "Nuoro": ("sc",), "Oristano": ("sc",),
                        "Sud Sardegna": ("sc",)},
                 # Wales' bilingual signs (Llanymddyfri over Llandovery: GB_FIRST) and the Highlands' and Islands'
                 # Gaelic ones, by their county; Ireland's Irish name over the English
                 "GB": {**{c: ("cy",) for c in (
                     "Isle of Anglesey", "Gwynedd", "Conwy", "Denbighshire", "Flintshire", "Wrexham", "Powys",
                     "Ceredigion", "Pembrokeshire", "Carmarthenshire", "Swansea", "Neath Port Talbot", "Bridgend",
                     "Vale of Glamorgan", "Cardiff", "Rhondda Cynon Taf", "Merthyr Tydfil", "Caerphilly",
                     "Blaenau Gwent", "Torfaen", "Monmouthshire", "Newport")},
                        **{c: ("gd",) for c in ("Highland", "Na h-Eileanan Siar", "Argyll and Bute")}},
                 "IE": {"": ("ga",)}}
# An agglomeration grid: cell (i, j) covers lat0 + i * dlat .. + dlat, lon0 + j * dlon .. + dlon; mask (rows i, cols j)
Agglo = namedtuple("Agglo", "lat0 lon0 dlat dlon mask")


def _grow(m, r):
    """Bool mask grown by r cells (a square window, separable: vegetation._dilate's)."""
    out = m.copy()
    for axis in (0, 1):
        acc = out.copy()
        for k in range(1, r + 1):
            acc[tuple(slice(k, None) if a == axis else slice(None) for a in range(2))] |= \
                out[tuple(slice(None, -k) if a == axis else slice(None) for a in range(2))]
            acc[tuple(slice(None, -k) if a == axis else slice(None) for a in range(2))] |= \
                out[tuple(slice(k, None) if a == axis else slice(None) for a in range(2))]
        out = acc
    return out


def _pieces(m):
    """(labels int32, sizes): the 8-connected pieces of bool mask m, numbered from 1 in scan order; sizes[k] is the
    cell count of piece k (sizes[0] unused). Each row's runs of cells at once and the pieces joined run by run (a
    union-find over the runs): the cell by cell walk took minutes on a German city's grid, 2026-10-29."""
    h, w = m.shape
    lab = np.zeros(m.shape, np.int32)
    if not m.any():
        return lab, np.array([0])
    d = np.diff(np.pad(m.astype(np.int8), ((0, 0), (1, 1))), axis=1)
    rs, s0 = np.nonzero(d == 1)                      # the runs, row-major: row, first column
    _, e0 = np.nonzero(d == -1)                      # ... their end (exclusive), in the same order
    parent = np.arange(len(rs))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    first = np.searchsorted(rs, np.arange(h + 1))    # row r's runs: first[r] .. first[r + 1]
    for r in range(h - 1):
        a0, a1, b0, b1 = first[r], first[r + 1], first[r + 1], first[r + 2]
        if a0 == a1 or b0 == b1:
            continue
        # (8-connected: a run below touches one above when their columns overlap or meet at a corner)
        lo = np.searchsorted(e0[b0:b1], s0[a0:a1], side="left")       # the first below ending at or after a's start
        hi = np.searchsorted(s0[b0:b1], e0[a0:a1], side="right")      # ... the last starting at or before a's end
        for k in np.flatnonzero(hi > lo):
            ra = find(a0 + k)
            for j in range(b0 + lo[k], b0 + hi[k]):
                rb = find(j)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
                    ra = min(ra, rb)
    roots = np.array([find(i) for i in range(len(rs))])
    # (numbered by their first run, which is their first cell in scan order)
    order, num = np.unique(roots, return_inverse=True)
    ids = (num + 1).astype(np.int32)
    sizes = np.zeros(len(order) + 1, np.int64)
    np.add.at(sizes, ids, e0 - s0)
    for k in range(len(rs)):
        lab[rs[k], s0[k]:e0[k]] = ids[k]
    return lab, sizes


def agglomeration(lonlat_parts, urban_fn, box=None):
    """The Agglo of a commune (lonlat_parts: its parts in lon/lat, as inside's), or None when the land cover has no
    built-up land in or near it (the whole commune is then its agglomeration). urban_fn(lat, lon) -> bool array: the
    land cover's built-up class (roads.road_specs'). box: (lon0, lat0, lon1, lat1) of the commune's part the grid
    covers (a village's, commune_agglomeration), the commune's own box when None."""
    pts = np.concatenate([r for rings in lonlat_parts for r in rings[:1] if len(r)]) if lonlat_parts else None
    if pts is None or not len(pts):
        return None
    lat_c = float(pts[:, 1].mean())
    if box is not None:
        pts = np.array([box[:2], box[2:]], np.float64)
    dlat = AGGLO_CELL_M / M_PER_DEG
    # (the commune's own longitude step, its latitude rounded: the same grid whichever cell or frame asks)
    dlon = AGGLO_CELL_M / (M_PER_DEG * max(math.cos(math.radians(round(lat_c, 1))), 0.01))
    mlat, mlon = AGGLO_MARGIN_M / AGGLO_CELL_M * dlat, AGGLO_MARGIN_M / AGGLO_CELL_M * dlon
    i0 = math.floor((float(pts[:, 1].min()) - mlat) / dlat)
    i1 = math.ceil((float(pts[:, 1].max()) + mlat) / dlat)
    j0 = math.floor((float(pts[:, 0].min()) - mlon) / dlon)
    j1 = math.ceil((float(pts[:, 0].max()) + mlon) / dlon)
    lat = (np.arange(i0, i1) + 0.5) * dlat
    lon = (np.arange(j0, j1) + 0.5) * dlon
    la, lo = np.meshgrid(lat, lon, indexing="ij")
    built = np.asarray(urban_fn(la.ravel(), lo.ravel()), bool).reshape(la.shape)
    if not built.any():
        return None
    r = max(1, int(round(CLOSE_M / AGGLO_CELL_M)))
    area = ~_grow(~_grow(built, r), r)              # closed: grown, then shrunk back (the complement grown)
    # opened: shrunk, then grown back: a strip narrower than about twice OPEN_M left out (the land cover counts a main
    # road's carriageway as built-up: Le Vernet's area ran 1.5 km up the N20 with it, its signs out in the fields,
    # 2026-10-29), after the closing so the houses along it have joined their town first
    o = int(round(OPEN_M / AGGLO_CELL_M))
    if o:
        opened = _grow(~_grow(~area, o), o)
        if opened.any():                # (all of it that thin: a village of a few houses along a lane, kept whole)
            area = opened
    lab, sizes = _pieces(area)
    keep = sizes >= MIN_AGGLO_M2 / AGGLO_CELL_M ** 2
    keep[0] = False
    if not keep.any():
        keep[int(np.argmax(sizes[1:])) + 1] = True  # (no piece that large: the largest, the village's bourg)
    area = keep[lab]
    # the land it encloses is in it too (a field or a park inside the town: no exit sign at its edge on every street
    # around it, Boisset-et-Gaujac 2026-10-28): the outside's pieces that do not reach the grid's edge
    out_lab, _ = _pieces(~area)
    edge = np.unique(np.concatenate([out_lab[0], out_lab[-1], out_lab[:, 0], out_lab[:, -1]]))
    area |= (out_lab > 0) & ~np.isin(out_lab, edge)
    area = _grow(area, int(round(EDGE_M / AGGLO_CELL_M)))     # (EDGE_M past its edge)
    return Agglo(i0 * dlat, j0 * dlon, dlat, dlon, area)


def on_agglo(agglo, lat, lon):
    """Which points (lat, lon arrays) lie on the agglomeration grid's area."""
    i = np.floor((np.asarray(lat, np.float64) - agglo.lat0) / agglo.dlat).astype(np.int64)
    j = np.floor((np.asarray(lon, np.float64) - agglo.lon0) / agglo.dlon).astype(np.int64)
    h, w = agglo.mask.shape
    ok = (i >= 0) & (i < h) & (j >= 0) & (j < w)
    out = np.zeros(i.shape, bool)
    out[ok] = agglo.mask[i[ok], j[ok]]
    return out


def inside(parts, xy):
    """Which points xy (n, 2) lie inside a polygon of parts (each a list of rings: outer first, then its holes)."""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    hit = np.zeros(len(xy), bool)
    for rings in parts:
        if not len(rings) or len(rings[0]) < 3:
            continue
        here = polygon.contains_points(rings[0], xy)
        for hole in rings[1:]:
            if len(hole) >= 3:
                here &= ~polygon.contains_points(hole, xy)
        hit |= here
    return hit


def inner_point(lonlat_parts):
    """A point (lon, lat) inside the polygon of lonlat_parts (its largest outer ring's): the mean of its vertices when
    that is inside, else the middle of the widest stretch of the ring that the mean's parallel crosses (a crescent's
    mean lies outside it). A function of the polygon alone. Pure."""
    ring = max((rings[0] for rings in lonlat_parts if len(rings) and len(rings[0]) >= 3), key=len)
    c = ring[:-1].mean(axis=0) if np.array_equal(ring[0], ring[-1]) else ring.mean(axis=0)
    if polygon.contains_points(ring, c[None, :])[0]:
        return c
    a, b = ring, np.roll(ring, -1, axis=0)
    cross = (a[:, 1] - c[1]) * (b[:, 1] - c[1]) < 0
    if not cross.any():
        return c
    t = (c[1] - a[cross, 1]) / (b[cross, 1] - a[cross, 1])
    xs = np.sort(a[cross, 0] + t * (b[cross, 0] - a[cross, 0]))
    k = int(np.argmax(xs[1::2] - xs[0::2][:len(xs[1::2])]))
    return np.array([(xs[2 * k] + xs[2 * k + 1]) / 2, c[1]])


def local_name(name, county, localadmin, names):
    """The commune's name in its regional language ("": none): REGIONAL_LOCALADMIN's or REGIONAL's languages of its
    county and localadmin, the first it has a name in (names: language code -> name, "" for none), unless the sign
    would write it as its French name (then none: Pau is Pau in Occitan, not Paue as the next language has it). A
    name that gives two forms ("Haezebroek / Oazebroeke") gives its first, one with a note in brackets ("Briva (La
    Galharda)", "Encausse (de Comenge)") its name without it."""
    for lang in REGIONAL_LOCALADMIN.get((county, localadmin), REGIONAL.get(county, ())):
        v = str(names.get(lang, "")).split(" / ")[0]
        v = " ".join(re.sub(r"\s*\([^)]*\)", "", v).split())
        if v:
            # (its accents count: Llupià under LLUPIA, written with them, face_image)
            return v if v.upper() != sign_text(name) else ""
    return ""


# the countries whose signs write a Cyrillic or Greek name's Latin form under it (Београд / Beograd, София / Sofia,
# Θεσσαλονίκη / Thessaloniki): Serbia's own (sr-Latn), the others' first alternate of no language (overture.LATIN_COLS);
# Ukraine's English name first, its official transliteration (Одеса / Odesa; its alternate may be the Russian's, Odessa)
# (and Tunisia's, Egypt's, Libya's, Mauritania's, Sudan's Arabic names, Ethiopia's and Eritrea's Ge'ez ones: their
# English name under; Morocco's and Algeria's names have their Latin form already, SCRIPTS)
# (the Middle East's Arabic, Persian, Hebrew, Georgian and Armenian names too)
LATIN_SECOND = {"RS", "BG", "MK", "GR", "UA", "CY", "TN", "EG", "LY", "MR", "SD", "ET", "ER", "SY", "LB", "JO", "IQ",
                "SA", "KW", "BH", "QA", "AE", "OM", "YE", "IR", "IL", "XW", "GE", "AM",
                "AF", "PK", "NP", "BD", "BT", "MV", "CN", "TW", "HK", "JP", "KR", "KP", "TH", "LA", "KH", "MM"}
_LATIN = re.compile(r"^[A-Za-zÀ-ɏ' .\-]+$")


def latin_name(names, name, country):
    """The Latin form of name the country's signs write under it (names: its columns, LATIN_COLS among them), ""
    when it has none or the name is Latin already. Pure."""
    if country not in LATIN_SECOND or _LATIN.match(name):
        return ""
    cols = {"RS": ("name_srlatn",), "UA": ("name_en", "name_alt")}.get(country, ("name_alt", "name_en"))
    for col in cols:
        v = str(names.get(col, "") or "")
        if v and _LATIN.match(v):
            return v
    return ""


def _names(feats, i):
    """{language or LATIN_COLS column: name} of feature i of a divisions layer ("" for none)."""
    out = {k: str(feats.attrs[f"name_{k}"][i]) for k in REGIONAL_LANGS if f"name_{k}" in feats.attrs}
    out.update({k: str(feats.attrs[k][i]) for k in LATIN_COLS if k in feats.attrs})
    return out


def communes_of(feats_list, frame, country_ok=SIGN_COUNTRIES, box=None):
    """[Commune] of the divisions layer's features (mapdata cells' Features, None for a cell without the layer), each
    id once, sorted by id; agglo is left None (agglomeration's, for the caller to set). Only the communes of
    country_ok's countries: their sign rules are written here. Each with its regional name (local_name's), from the
    county and localadmin of the layer that its inner_point lies in. A PICTOGRAMS country's are squares (squares'),
    those over box (lon0, lat0, lon1, lat1: the cells'; None: none)."""
    seen, out = set(), []
    areas = {"county": {}, "localadmin": {}}     # subtype -> id -> (name, lon/lat parts)
    places = []                                  # (fid, name, lon/lat parts, {lang: name}, country, region)
    lands = {}                                   # PICTOGRAMS country -> [(lon/lat box, its counties' parts)]
    for feats in feats_list:
        if feats is None or not len(feats):
            continue
        subtypes = feats.attrs.get("subtype")
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if fid in seen or str(feats.attrs["country"][i]) not in country_ok:
                continue
            seen.add(fid)
            name = " ".join(str(feats.attrs["name"][i]).split())
            ll = [[np.asarray(r, np.float64) for r in rings if len(r) >= 3] for rings in feats.parts(i)]
            ll = [rs for rs in ll if rs]
            if not name or not ll:
                continue
            sub = "locality" if subtypes is None else str(subtypes[i])
            country = str(feats.attrs["country"][i])
            if country in PICTOGRAMS:           # (no name: its counties only, the land its squares are limited to)
                if sub == "county":
                    lands.setdefault(country, []).append((_box(ll), ll))
                continue
            region = str(feats.attrs["region"][i]) if "region" in feats.attrs else ""
            if country in VILLAGE_COUNTRIES and sub == VILLAGE_COUNTRIES[country]:
                if (country in CITY_COUNTIES and ASSOCIATIONS.match(name)) \
                        or _km2(ll) > TOWN_MAX_KM2.get(country, 1e9) \
                        or AREA_NOT_PLACE.search(name):     # (and Baguio's "District 18": no town)
                    # (an association: its Gemeinden are areas of their own; or no town at all: Mecklenburg's
                    # "Küstengewässer einschließlich Anteil am Festlandsockel", the Baltic's coastal waters, hung the
                    # agglomeration's grid, 2026-10-29)
                    continue
                places.append((fid, name, ll, {}, country, region))     # (split_villages names its villages)
            elif country in CITY_COUNTIES and sub == "county" and kreisfreie(name, ll):
                # (a kreisfreie Stadt is a county: Nürnberg has no locality area, its Stadtteile points only; an
                # Austrian Statutarstadt too, Linz)
                places.append((fid, name, ll, {}, country, region))
                areas[sub][fid] = (name, ll)
            elif country in VILLAGE_COUNTRIES and sub == "locality":
                continue                        # (Luxembourg's lone locality area, a farm: its villages are points)
            elif sub in areas:
                areas[sub][fid] = (name, ll)
            elif sub == "locality":
                if MUNICIPALITY_POINTS.search(name) or AREA_NOT_PLACE.search(name):
                    continue                    # (a ward, a rural district: no town)
                if country in NAME_EXTRA:
                    name = NAME_EXTRA[country].sub("", name)
                places.append((fid, name, ll, _names(feats, i), country, region))

    def around(sub, p):
        # (the first by id: the same answer whichever cells listed the areas)
        return next((n for _, (n, parts) in sorted(areas[sub].items()) if inside(parts, p[None, :])[0]), "")
    big = [(_area(ll), _box(ll), ll) for _, _, ll, _, country, _ in places if country in DISTRICT_AREAS]

    def district(ll):
        p, size = inner_point(ll), _area(ll)
        return any(s > size and b[0] <= p[0] <= b[2] and b[1] <= p[1] <= b[3] and inside(parts, p[None, :])[0]
                   for s, b, parts in big)
    places = [t for t in places if t[4] not in DISTRICT_AREAS or not district(t[2])]
    for fid, name, ll, names, country, region in places:
        local, county, sub = "", "", ""
        if country == "FR" and any(names.values()):
            p = inner_point(ll)
            local = local_name(name, around("county", p), around("localadmin", p), names)
        elif country in VILLAGE_COUNTRIES and VILLAGE_COUNTRIES[country] != "county":
            county = around("county", inner_point(ll))
            if country in REGION_STYLES:
                sub = REGION_STYLES[country].get(region, "")
        elif country == "NL":
            # (a woonplaats of a gemeente of another name: "gem. Meerssen" under Bunde; Friesland's Frisian name, its
            # signs bilingual: Sneek / Snits)
            county = around("county", inner_point(ll))
            if region == "NL-FR" and names.get("fy") and names["fy"].upper() != name.upper():
                local = names["fy"]
            sub = f"gem. {county}" if county and county != name else ""
        elif country in LATIN_SECOND:           # (North Macedonia's: Штип / Štip)
            local = latin_name(names, name, country)
        xy = [[np.column_stack(frame.geodetic_to_enu(r[:, 1], r[:, 0], 0.0)[:2]) for r in rs] for rs in ll]
        out.append((fid, Commune(name, xy, ll, None, fid, local, country, county=county, sub=sub)))
    if box is not None:
        out += [(c.fid, c) for c in squares(lands, box, frame)]
    return [c for _, c in sorted(out, key=lambda t: t[0])]


# the countries whose signs name no place (Sweden's E5, Finland's E22: a town's silhouette): their communes, too large
# to split into villages (Kiruna's kommun 20,000 km2), are not read; the built-up land of SQUARE_DEG squares on a fixed
# grid, all one town (no sign between two), limited to the country's counties, has the signs
PICTOGRAMS = {"SE", "FI"}
SQUARE_DEG = 0.04                # degrees of latitude and longitude (~4.4 km x 2.3 km at 60 N)


def squares(lands, box, frame):
    """The PICTOGRAMS countries' squares over box (lon0, lat0, lon1, lat1, widened by AGGLO_MARGIN_M): lands, country ->
    [(lon/lat box, county parts)]; each square a Commune with no name, its land within the counties over it, all of a
    country one town (group). The same squares whichever tile asks (the grid's)."""
    out = []
    pad = AGGLO_MARGIN_M / M_PER_DEG * 2
    for cc, counties in sorted(lands.items()):
        for i in range(math.floor((box[1] - pad) / SQUARE_DEG), math.floor((box[3] + pad) / SQUARE_DEG) + 1):
            for j in range(math.floor((box[0] - pad) / SQUARE_DEG), math.floor((box[2] + pad) / SQUARE_DEG) + 1):
                lo0, la0 = j * SQUARE_DEG, i * SQUARE_DEG
                lo1, la1 = lo0 + SQUARE_DEG, la0 + SQUARE_DEG
                within = [p for b, parts in counties if b[0] <= lo1 and b[2] >= lo0 and b[1] <= la1 and b[3] >= la0
                          for p in parts]
                if not within:
                    continue
                ring = np.array([(lo0, la0), (lo1, la0), (lo1, la1), (lo0, la1), (lo0, la0)], np.float64)
                xy = np.column_stack(frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)[:2])
                fid = f"{cc}:sq:{i}:{j}"
                out.append(Commune("", [[xy]], [[ring]], None, fid, "", cc, group=f"{cc}:sq", within=within))
    return out


def state(xy, communes, frame):
    """Per point xy (n, 2, frame metres): the index in communes of the commune whose agglomeration it is in, or -1
    (outside every agglomeration)."""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    out = np.full(len(xy), -1, np.int64)
    ll = None
    for k, c in enumerate(communes):
        free = np.flatnonzero(out < 0)
        if not len(free):
            break
        idx = free[inside(c.parts, xy[free])]
        if not len(idx):
            continue
        if c.agglo is not None or c.sites is not None:
            if ll is None:
                lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
                ll = (np.asarray(lat, np.float64), np.asarray(lon, np.float64))
            if c.sites is not None:             # (a village: its part of the commune)
                idx = idx[nearest_site(c.sites, ll[0][idx], ll[1][idx]) == c.site]
            if c.agglo is not None:
                idx = idx[on_agglo(c.agglo, ll[0][idx], ll[1][idx])]
        out[idx] = k
    return out


def nearest_site(sites, lat, lon):
    """Per point (lat, lon arrays): the index of the nearest of sites ((k, 2) lon/lat; distances on the plane of the
    sites' mean latitude). Pure."""
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    c = math.cos(math.radians(float(np.mean(sites[:, 1]))))
    d = ((lon[..., None] - sites[:, 0]) * c) ** 2 + (lat[..., None] - sites[:, 1]) ** 2
    return np.argmin(d, axis=-1)


def split_villages(communes, points_of):
    """communes with each VILLAGE_COUNTRIES commune replaced by its villages (sorted as communes_of sorts, by id): one
    Commune per Overture `locality` point inside it (points_of(commune) -> [Features of the "division_points" layer]
    covering it whole, so its villages are the same whichever tile asks), named after the point, its regional name its
    second line (VILLAGE_LANGS' of its country and county: Luxembourgish, Sorbian, North Frisian) when the sign would
    write it otherwise, and its small line (village_sub). A commune with no village point stays as it is (a NESTED
    commune whose points are all a smaller commune's is left out). The communes
    of other countries come first, by id, then the villages by their commune's area, the smaller first: a village with
    its own area (an Ortsteil) is its area's, not its Gemeinde's or Samtgemeinde's (state takes the first)."""
    out, villages = [], []
    # (the kreisfreie Städte among them: the German communes communes_of took from a county)
    cities = {c.name for c in communes if c.country in CITY_COUNTIES and c.county == c.name}
    # (NESTED: the areas around each, smaller ones first: (size, lon/lat box, commune))
    boxes = sorted((_area(c.lonlat), _box(c.lonlat), k) for k, c in enumerate(communes) if c.country in NESTED)
    for ci, c in enumerate(communes):
        if c.country not in VILLAGE_COUNTRIES:
            out.append(c)
            continue
        found, taken = {}, 0
        size = _area(c.lonlat)
        smaller = [(b, communes[k]) for s, b, k in boxes if s < size and k != ci] if c.country in NESTED else []
        # (a larger area's own point in it, no village: the comarca Serranía de Ronda's, in Ronda's municipio)
        larger = {communes[k].name for s, b, k in boxes if s > size} if c.country in NESTED else set()
        for feats in points_of(c):
            if feats is None or not len(feats):
                continue
            for i in range(len(feats)):
                vid = str(feats.ids[i])
                if vid in found or str(feats.attrs["country"][i]) != c.country:
                    continue
                p = np.asarray(feats.parts(i)[0][0], np.float64).reshape(-1, 2)[:1]
                if not inside(c.lonlat, p)[0]:
                    continue
                if c.country == "CH" and DISTRICT_POINTS.match(str(feats.attrs["name"][i])):
                    continue
                if MUNICIPALITY_POINTS.search(str(feats.attrs["name"][i])) or str(feats.attrs["name"][i]) in larger:
                    continue
                if c.country in NEIGHBOURHOOD_COUNTRIES and NEIGHBOURHOOD_POINTS.match(str(feats.attrs["name"][i])):
                    continue
                if any(b[0] <= p[0, 0] <= b[2] and b[1] <= p[0, 1] <= b[3] and inside(d.lonlat, p)[0]
                       for b, d in smaller):
                    taken += 1                  # (a smaller area's: a municipio's, not its comarca's)
                    continue
                names = _names(feats, i)
                town =(town_of(str(feats.attrs["localities"][i]), cities) if "localities" in feats.attrs
                        else (None, ""))
                found[vid] = (" ".join(str(feats.attrs["name"][i]).split()), names, p[0], town)
        if not found:
            # (all of its villages smaller areas': a comarca, no town; none near the tile in a large one, a Latvian
            # novads' 3,000 km2 whole on one grid: no town here either, its road tiles were killed, 2026-10-30)
            if not taken and _km2(c.lonlat) <= ALONE_MAX_KM2:
                # (named without its municipality's words: Ghana's "Asokwa Municipal District" is Asokwa)
                villages.append((size, str(c.fid), c._replace(name=SEAT_BEFORE.sub("", SEAT.sub("", c.name)) or c.name,
                                                              sub=village_sub(c.name, c))))
            continue
        ids = sorted(found)
        sites = np.array([found[v][2] for v in ids], np.float64)
        county = c.county.split(" - ")[0] or c.name     # (a county's own villages: its name, Great Britain's)
        langs = VILLAGE_LANGS.get(c.country, {})
        langs = langs.get(county, langs.get("", ()))
        def second(name, names):
            if c.country == "IT" and BILINGUAL.search(name):     # (both its names already)
                return ""
            if c.country in LATIN_SECOND:
                return latin_name(names, name, c.country)
            return next((names[g] for g in langs if names.get(g) and names[g].upper() != name.upper()), "")
        # (a city's own second name, its own point's: Cottbus' Chóśebuz on its Stadtteile' signs too)
        own = {f[0]: second(f[0], f[1]) for f in found.values()}
        for k, v in enumerate(ids):
            name, names, _, (gid, gname) = found[v]
            local = second(name, names)
            sub = village_sub(name, c, gname)
            if gname.startswith("="):       # a city's Stadtteil: the city's name, the Stadtteil's under it
                gname = gname[1:]
                name, sub, local = gname, (name if name != gname else village_sub(gname, c)), own.get(gname, "")
            if c.country == "DE":
                # (Lusatia's names are both already, "Cottbus - Chóśebuz": the German on the first line, the Sorbian
                # on the second, as its Ortstafeln; the small line the German only)
                name, _, both = name.partition(" - ")
                local = local or both
                sub = sub.partition(" - ")[0]
            if c.country == "CH":
                # (a homonym's canton under its name: "Colombier (VD)"; the villages of a Gemeinde one town)
                m = CANTON.match(name)
                if m:
                    name, sub = m.group(1), m.group(2)
            if c.country in BY_COMMUNE:         # (a municipio's núcleos one town: its comarca is above it)
                gid = str(c.fid)
            if c.country in NAME_EXTRA:
                name = NAME_EXTRA[c.country].sub("", name)
            villages.append((size, f"{c.fid}#{v}", c._replace(
                name=name, fid=f"{c.fid}#{v}", local=local, sites=sites, site=k, sub=sub,
                group=gid if c.country in GROUPED else None, town=c.name)))
    return sorted(out, key=lambda c: str(c.fid)) + [c for _, _, c in sorted(villages, key=lambda t: t[:2])]


def town_of(localities, cities=None):
    """(id, name) of the town a village point is part of, from its "division_points" localities
    ("subtype:id=name|...", outermost first): Berlin or Hamburg (CITY_STATES: their region) for their Bezirke and
    Ortsteile; a kreisfreie Stadt (its county, among cities: the kreisfreie counties' names, kreisfreie's by their
    size; None: any county kreisfreie's by its name) for its Stadtteile (Ulm for Grimmelfingen), their names
    given "=Berlin", "=Ulm": the city's name is the Ortstafel's, the Stadtteil's under it; else the outermost
    locality that is not an association of Gemeinden (Rüdesheim am Rhein for Assmannshausen, Husum for Husum in the
    Samtgemeinde Mittelweser); (None, "") if none."""
    for item in localities.split("|"):
        sub, _, rest = item.partition(":")
        fid, _, name = rest.partition("=")
        if not fid or not name:
            continue
        city = name in cities if cities is not None else kreisfreie(name)
        if (sub == "region" and name in CITY_STATES) or (sub == "county" and city):
            return fid, "=" + name          # (a city: its Ortstafeln write the city's name, split_villages)
        if sub == "locality" and not ASSOCIATIONS.match(name):
            return fid, name
    return None, ""


# Germany's city states whose Bezirke Overture has as localities right under the region (Bremen's two cities are
# Gemeinden of their own)
CITY_STATES = {"Berlin", "Hamburg"}
# km2: a German county named without Kreis, Region or Verband and smaller than this is a kreisfreie Stadt (the largest,
# Köln, 405 km2); the Landkreise Overture names without the word (Brandenburg's, Saxony's, Saxony-Anhalt's: Börde,
# 2366 km2, was split as one town into 268 villages, 2026-10-29) are all over 800
CITY_MAX_KM2 = 600.0
# km2: a VILLAGE_COUNTRIES commune area larger is no town, a little over each country's largest (Germany's, Gardelegen:
# 632; Spain's, Cáceres: 1,750, its "localities" that are provinces fetched hundreds of map cells of villages, the
# check of a village did not end in an hour, 2026-10-29; Rome 1,287; Odemira 1,720; Ringkøbing-Skjern 1,489)
TOWN_MAX_KM2 = {"DE": 1000.0, "AT": 1000.0, "ES": 2000.0, "IT": 1500.0, "PT": 2000.0, "PL": 1000.0, "CZ": 1000.0,
                "SK": 1000.0, "DK": 1600.0, "SI": 1000.0, "CH": 1000.0, "LU": 1000.0, "EE": 3000.0, "LV": 4000.0,
                "LT": 2500.0, "RO": 1500.0, "BG": 1500.0, "GR": 2500.0, "RS": 1500.0, "BA": 2500.0,
                # (Norway's and Iceland's largest, Kautokeino's 9,700 and Múlaþing's 10,700: their villages near the
                # tile only, POINTS_REACH_M)
                "NO": 12000.0, "IS": 12000.0, "AL": 2500.0, "XK": 1500.0, "ME": 3000.0, "UA": 2500.0, "BY": 1500.0,
                # (Highland's 26,000 km2, County Cork's 7,500: their villages near the tile only, POINTS_REACH_M)
                "GB": 30000.0, "IE": 8000.0, "CY": 3000.0,
                # (a US city's area, Jacksonville's 2,265 km2; Canada's largest towns', La Tuque's 28,000; a Mexican
                # municipio, Ensenada's 52,000: their villages near the tile only)
                "US": 3000.0, "CA": 30000.0, "MX": 60000.0,
                # (Central America's municipios, Distrito de Panamá's 2,153 km2)
                **{cc: 6000.0 for cc in ("GT", "SV", "HN", "NI", "CR", "PA", "CU", "DO", "HT")},
                # (the islands' parishes and municipios: Jamaica's Saint Ann 1,213 km2, the Bahamas' Andros)
                **{cc: 7000.0 for cc in ("JM", "PR", "BS", "TT", "BB", "LC", "VC", "GD", "DM", "AG", "KN")},
                # (a Brazilian município, Altamira's 159,000 km2; a Peruvian province, Maynas' 75,000: their villages
                # near the tile only)
                "BR": 200000.0, "BO": 200000.0, "CL": 50000.0,
                **{cc: 100000.0 for cc in ("CO", "PE", "EC", "VE", "GY", "SR", "GF")},
                # (Africa's districts and regions, Libya's Kufra 433,000 km2: their villages near the tile only)
                **{cc: 150000.0 for cc in AFRICA_DISTRICTS}, **{cc: 500000.0 for cc in ("LY", "DJ", "SC", "YT")},
                # (the Middle East's: Saudi Arabia's governorates, Yemen's districts, Israel's regional councils)
                **{cc: 200000.0 for cc in ("TR", "SY", "LB", "JO", "IQ", "SA", "KW", "QA", "OM", "GE", "AM")},
                "BH": 100000.0, "AE": 100000.0, "YE": 50000.0, "IL": 5000.0,
                # (Asia's and Oceania's: India's districts, Japan's municipalities, Indonesia's kabupaten)
                **{cc: 200000.0 for cc in ("UZ", "TM", "AF", "IN", "BD", "LK", "KR", "MM", "LA", "KH", "TL", "ID",
                                           "MN", "BN", "AU", "PG", "FJ", "NC", "PF", "VU", "TO")},
                **{cc: 50000.0 for cc in ("CN", "JP", "MY", "PK", "NP", "HK", "PH")},
                **{cc: 500000.0 for cc in ("BT", "MV", "KP", "SB", "WS", "GU", "VN")},
                # (the territories': Greenland's municipalities, Qeqqata's 115,500 km2)
                **{cc: 5000.0 for cc in ("IM", "JE", "BM", "KY", "VG", "TC", "PW", "GG", "AX", "AI", "BQ", "NF", "GI",
                                         "PN", "CX", "TK", "NR", "MS", "WF", "MH", "CC")},
                "SJ": 100000.0,
                "GL": 2000000.0}
# the territories whose villages are points in their parishes, islands or municipalities (the Isle of Man's, Jersey's,
# Greenland's, Bermuda's, the Caymans', the Virgin Islands', the Turks and Caicos', Palau's regions; Guernsey's and
# Åland's counties; Diego Garcia's points its base's camps, no town); Anguilla's, Bonaire's, Gibraltar's and Pitcairn's
# points in the territory itself (Overture's dependency: no smaller area holds them); Christmas Island's, Montserrat's,
# Wallis and Futuna's and the Marshall Islands' in their shire, parishes, kingdoms and atolls (regions), Svalbard's and
# Tokelau's in their lands' and atolls' locality areas (Nordenskiöld Land, Fakaofo), Nauru's and the Cocos' in their
# districts and shire (counties)
VILLAGE_COUNTRIES.update({**{cc: "region" for cc in ("IM", "JE", "GL", "BM", "KY", "VG", "TC", "PW", "CX", "MS",
                                                      "WF", "MH")},
                          "SJ": "locality", "TK": "locality", "NR": "county", "CC": "county",
                          "GG": "county", "AX": "county",
                          **{cc: "dependency" for cc in ("AI", "BQ", "NF", "GI", "PN")}})


def _km2(lonlat_parts):
    """The area of a polygon's outer rings, km2 (on the plane of their mean latitude). Pure."""
    lat = float(np.mean(np.concatenate([r[:, 1] for rings in lonlat_parts for r in rings[:1]])))
    return _area(lonlat_parts) * (M_PER_DEG / 1000.0) ** 2 * math.cos(math.radians(lat))


def kreisfreie(county, lonlat_parts=None):
    """Is the German county (Overture's name; its parts in lon/lat when known) a kreisfreie Stadt? Its name alone:
    the cities the split_villages of the tile knows (CITY_STATES aside, town_of's cities). Pure."""
    county = county.split(" - ")[0]
    # (Austria's: "Bezirk Bregenz"; Belarus': "Баранавіцкі раён"; Canada's: "Frontenac County")
    if re.search(r"kreis|Region|verband|Bezirk|раён|County|District", county, re.IGNORECASE):
        return False
    return lonlat_parts is None or _km2(lonlat_parts) < CITY_MAX_KM2


def _box(lonlat_parts):
    """(lon0, lat0, lon1, lat1) of a polygon's outer rings. Pure."""
    pts = np.concatenate([r for rings in lonlat_parts for r in rings[:1]])
    return (*pts.min(axis=0), *pts.max(axis=0))


def _area(lonlat_parts):
    """The area of a polygon's outer rings, in square degrees (a ranking only). Pure."""
    s = 0.0
    for rings in lonlat_parts:
        r = rings[0]
        s += abs(float(np.dot(r[:, 0], np.roll(r[:, 1], -1)) - np.dot(r[:, 1], np.roll(r[:, 0], -1)))) / 2
    return s


def village_sub(name, commune, town=""):
    """The small line under a German village's names (Zeichen 310): its Gemeinde (town: split_villages' town of it)
    when it is another place of it (Assmannshausen: Rüdesheim am Rhein), else its Kreis (Wilster: Kreis Steinburg; a
    Landkreis Overture names without the word: "Landkreis Zwickau"), none for a kreisfreie Stadt (Cottbus: its county
    is itself) or another country; an Austrian village's Gemeinde (its Ortstafeln write no Bezirk)."""
    if commune.country == "AT":
        return town.lstrip("=") if town and name != town.lstrip("=") else ""
    if commune.country in REGION_STYLES:     # (not a line: the province its sign's colours are, communes_of's)
        return commune.sub
    if commune.country != "DE":
        return ""
    if town and name != town:
        return town
    county = commune.county.split(" - ")[0]
    if not county or county == name.split(" - ")[0]:
        return ""
    return county if re.search(r"kreis|Region|Stadtverband", county, re.IGNORECASE) else "Landkreis " + county


# The next place on an exit sign (Luxembourg's E,9b names it above the struck-through name, with an arrow): the
# nearest village point (mapdata "division_points") ahead of the traffic leaving, within NEXT_CONE_DEG of its heading
# and NEXT_MAX_M, not the town itself. The points are read around the sign's own place, so the answer does not depend
# on the tile or anchor that asks.
NEXT_PLACE = {"LU", "DE", "CH", "LI"}       # the countries whose exit signs name it
NEXT_KM = {"DE", "CH", "LI"}                # ... and its distance (Zeichen 311: "Schotten 6 km", whole kilometres)
NEXT_CONE_DEG = 35.0
NEXT_MIN_M, NEXT_MAX_M = 300.0, 4500.0


def next_place(lat, lon, heading, points, name):
    """(name, metres) of the next place ahead of a sign at (lat, lon) for the traffic leaving it heading (degrees
    counter-clockwise from east), among points [(name, lon, lat)], its own name (name) left out; None if none is
    within NEXT_CONE_DEG and NEXT_MIN_M .. NEXT_MAX_M. The nearest, an angle off the heading counting as distance (a
    village straight ahead before one as near to the side). Pure."""
    c = math.cos(math.radians(lat))
    h = math.radians(heading)
    best = None
    for n, plon, plat in points:
        if not n or n == name:
            continue
        dx, dy = (plon - lon) * M_PER_DEG * c, (plat - lat) * M_PER_DEG
        d = math.hypot(dx, dy)
        if not NEXT_MIN_M <= d <= NEXT_MAX_M:
            continue
        off = abs((math.degrees(math.atan2(dy, dx) - h) + 180.0) % 360.0 - 180.0)
        if off > NEXT_CONE_DEG:
            continue
        score = (d * (1.0 + off / 45.0), n)
        if best is None or score < best[0]:
            best = (score, n, d)
    return None if best is None else (best[1], best[2])


VILLAGE_PROBE_M = 100.0         # m between the points a village's part of its commune is found at (its box)
VILLAGE_REACH_M = 8000.0        # m: the farthest from its point a village's grid reaches (no village is wider)
ALONE_MAX_KM2 = 100.0           # km2: a commune with no village near the tile, or no built-up land, larger is no town
WHOLE_MAX_KM2 = 1000.0          # km2: a commune larger has no agglomeration of its whole (too large for one grid;
                                # France's largest, Arles: 759)
VILLAGE_FINE_M = 3000.0         # m: a village's part wider (its box's side) has its fine grid over its probes' built-up
                                # land only (commune_agglomeration)


def commune_agglomeration(c, urban_fn):
    """agglomeration of the commune c, a village's of the built-up land of its part of the commune only, on a grid
    over that part's box (a city's thirty villages each on the whole city's grid took a quarter of an hour)."""
    if c.within is not None:            # (a PICTOGRAMS square: its country's built-up land only)
        within, base = c.within, urban_fn

        def urban_fn(lat, lon):
            lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
            return np.asarray(base(lat, lon), bool) & inside(within, np.column_stack([lon, lat]))
    if c.sites is None:
        if _km2(c.lonlat) > WHOLE_MAX_KM2:          # (too large for one grid: no town of it)
            return None
        return agglomeration(c.lonlat, urban_fn)
    pts = np.concatenate([r for rings in c.lonlat for r in rings[:1]])
    lon0, lat0 = pts.min(axis=0)
    lon1, lat1 = pts.max(axis=0)
    dlat = VILLAGE_PROBE_M / M_PER_DEG
    dlon = dlat / max(math.cos(math.radians(float(pts[:, 1].mean()))), 0.01)
    p = c.sites[c.site]
    # (the probes within VILLAGE_REACH_M of its point: a Swedish kommun's village alone in it, Kiruna's 20,000 km2;
    # on the commune's own lattice, the same probes as the whole box's there)
    rlat, rlon = VILLAGE_REACH_M / M_PER_DEG, VILLAGE_REACH_M / M_PER_DEG * dlon / dlat
    if p[1] - rlat > lat0:
        lat0 += math.floor((p[1] - rlat - lat0) / dlat) * dlat
    if p[0] - rlon > lon0:
        lon0 += math.floor((p[0] - rlon - lon0) / dlon) * dlon
    lat1, lon1 = min(lat1, p[1] + rlat), min(lon1, p[0] + rlon)
    la, lo = np.meshgrid(np.arange(lat0, lat1 + dlat, dlat), np.arange(lon0, lon1 + dlon, dlon), indexing="ij")
    la, lo = la.ravel(), lo.ravel()
    mine = nearest_site(c.sites, la, lo) == c.site
    mine[mine] = inside(c.lonlat, np.column_stack([lo[mine], la[mine]]))
    if not mine.any():                  # (its point's part smaller than the probe: around its point)
        box = (p[0] - dlon, p[1] - dlat, p[0] + dlon, p[1] + dlat)
    else:
        box = (lo[mine].min() - dlon, la[mine].min() - dlat, lo[mine].max() + dlon, la[mine].max() + dlat)
    box = (max(box[0], p[0] - rlon), max(box[1], p[1] - rlat), min(box[2], p[0] + rlon), min(box[3], p[1] + rlat))
    # the fine grid over the probes' built-up land only, when the part is large (a Greek dimos' villages each on a
    # grid of their whole part kept the road tiles minutes, Trikala 2026-10-30); none of it there: no village
    if (box[2] - box[0]) * (box[3] - box[1]) > (VILLAGE_FINE_M / M_PER_DEG) ** 2 / math.cos(math.radians(p[1])):
        built = np.zeros(len(la), bool)
        built[mine] = np.asarray(urban_fn(la[mine], lo[mine]), bool)
        if not built.any():
            return None
        box = (max(box[0], lo[built].min() - 2 * dlon), max(box[1], la[built].min() - 2 * dlat),
               min(box[2], lo[built].max() + 2 * dlon), min(box[3], la[built].max() + 2 * dlat))
    return agglomeration(c.lonlat, lambda lat, lon: np.asarray(urban_fn(lat, lon), bool)
                         & (nearest_site(c.sites, lat, lon) == c.site), box=box)


# the countries whose villages grown into their commune's centre are named after the commune: Italy's frazioni in a
# city (Trieste's 104 entry signs named Barcola, Opicina and the others but 3, 2026-10-29; its signs say TRIESTE),
# Portugal's localidades in their município's seat, Spain's barrios in their city's (San Sebastián's entry signs were
# named Herrera, Larratxo, Marrutxipi, in game 2026-10-29), the Czech, Slovak, Danish and Polish towns' parts
CENTRES = {"IT", "PT", "ES", "CZ", "SK", "DK", "PL", "SI", "EE", "LV", "LT", "RO", "BG", "RS", "NO", "BA", "AL", "XK",
           "ME", "AD", "SM", "LI", "UA", "BY", "US", "CA", "MX", "GT", "SV", "HN", "NI", "CR", "PA",
           "CU", "DO", "HT", "PR", "BR", "BO", "CL", "CO", "PE", "EC", "VE", "GY", "SR", "GF", "LY", "DJ", "SC",
           "YT", "TR", "SY", "LB", "JO", "IQ", "SA", "KW", "QA", "OM",
           "GE", "AM", "BH", "AE", "YE",
           "UZ", "TM", "AF", "IN", "BD", "LK", "KR", "MM", "LA", "KH", "TL", "ID", "MN", "BN", "AU", "PG", "FJ", "NC",
           "PF", "VU", "TO", "CN", "JP", "MY", "PK", "NP", "HK", "PH", "VN"} | set(AFRICA_DISTRICTS)
# the word a commune's name adds to its seat's ("Roskilde Kommune": Roskilde; Bakkehuse, a part of Roskilde, named
# its western signs, in game 2026-10-30)
# (vald: an Estonian rural municipality, Rapla vald's seat Rapla; Africa's "Jinja City", "Arusha Municipal",
# "Kumasi Metropolitan District", "Kitwe District", "Maseru (Maseru District)")
# (and Turkey's "Karabük Merkez", Armenia's "Վանաձոր համայնք")
# (and Uzbekistan's "Samarqand shahri", Turkmenistan's "Mary şäheri", Australia's "Bathurst Regional Council")
SEAT = re.compile(r"\s+(Kommune|vald|City|Municipal|Municipality|Metropolitan District|District|Merkez|համայնք|"
                  r"shahri|şäheri|Regional Council|City Council|Shire Council|Shire|Council)$|\s*\([^)]*\)$")
# ... and the word before it (Bashkia Shkodër, Opština Bar; Kosovo's in each of its languages, "Komuna e Prizrenit /
# Opština Prizren / Prizren Belediyesi": Prizren)
# (Mexico's "Municipio de Querétaro", Panama's "Distrito de Panamá", Guatemala's "Ciudad de Guatemala")
SEAT_BEFORE = re.compile(r"^(Bashkia|Opština|Općina|Municipio de|Municipio|Distrito de|Ciudad de|Cantón de|"
                         r"Commune de|Commune|Cercle de|Département de|Préfecture de|Prefecture de|Sector de|"
                         r"Cidade da|Cidade de|Inkhundla|WILAYAT|Wilayat|قضاء|لواء قصبة|لواء|محافظة|منطقة|"
                         r"Kota|Kabupaten|Majlis Bandaraya|Majlis Perbandaran|Majlis Daerah|مديرية)\s+")
# (Haiti's "Commune Cité Soleil", Mali's "Cercle de Ségou", Oman's "WILAYAT NIZWA", Lebanon's "قضاء زحلة")
# the word an Estonian place's name in the data ends with, not on its sign ("Rapla linn": a town, "Mahlamäe küla": a
# village; their signs say Rapla, Mahlamäe)
EE_KIND = re.compile(r"\s+(linn|küla|alev|alevik)$")
# country -> what a place's name in the data adds that its sign does not: Estonia's EE_KIND, an Albanian village's
# own village in brackets ("Kullajxhi (Bardhaj)"), a Montenegrin village's part ("Pečurice-dio": dio, a part)
# ..., a Maltese locality's article ("Il-Mosta": its council's sign says MOSTA; Ħal Lija keeps its Ħal)
NAME_EXTRA = {"EE": EE_KIND, "AL": re.compile(r"\s+\([^)]*\)$"), "ME": re.compile(r"-dio$"),
              "MT": re.compile(r"^(I[lċdnrstxżz]|L)-"),
              # (a Cypriot community's point, "Κοινότητα Χλώρακα": its sign says ΧΛΩΡΑΚΑ)
              "CY": re.compile(r"^Κοινότητα\s+"),
              # (Thiès's communes d'arrondissement: "Commune de Thiès Nord")
              "SN": re.compile(r"^Commune de\s+"),
              # (an Iranian town's "city" or village's "village of": شهر کاشان, its sign کاشان; روستای مسلم آباد)
              "IR": re.compile(r"^(شهر|روستای)\s+"),
              # (Kyrgyzstan's "Ош шаары": a city, its sign Ош; Thailand's municipalities and tambons,
              # "เทศบาลนครเชียงใหม่"; Mongolia's names' vertical Mongolian script, not on its signs)
              "KG": re.compile(r"\s+шаары$"), "TH": re.compile(r"^(เทศบาลนคร|เทศบาลเมือง|เทศบาลตำบล|ตำบล)"),
              "MN": re.compile(r"\s*[\u1800-\u18af].*$"),
              # (a Russian town's urban settlement: "городское поселение Суздаль", its sign СУЗДАЛЬ)
              "RU": re.compile(r"^городское поселение\s+"),
              # (a Northern Mariana village's island: "San Jose (Saipan)")
              "MP": re.compile(r"\s+\([^)]*\)$")}


def is_seat(name, town):
    """Is the village name its commune's (town's) seat: the commune's name (SEAT's and SEAT_BEFORE's words left out;
    one of its names " / "), that name followed by "de ..." (the Dominican Republic's Santiago de los Caballeros, its
    municipio Santiago's), or a Lithuanian savivaldybė's, the seat's name in the genitive before it (Vilnius, Vilniaus
    miesto savivaldybė; Šalčininkai, Šalčininkų rajono savivaldybė). Names compared without their accents or case
    (Colombia's municipio Zipaquira, its seat Zipaquirá). Pure."""
    plain = sign_text(name)
    for t in town.split(" / "):
        t = sign_text(SEAT_BEFORE.sub("", SEAT.sub("", t)))
        if plain == t or plain.startswith(t + " DE "):
            return True
    town = SEAT.sub("", town)
    first = town.split(" ")[0]
    return town.endswith("savivaldybė") and len(name) >= 5 and first[:len(name) - 2] == name[:-2]


def with_agglomerations(communes, agglo_of):
    """communes with their agglomerations (agglo_of(commune) -> Agglo or None); a village with no built-up land left
    out (no signs: its part of the commune is no line anybody's sign would stand on, a hamlet's point in the fields).
    A CENTRES village whose agglomeration touches the commune's own village's (its seat's: is_seat), or another's that
    does, has the commune's names: one town grown together. (By its houses, not its point: Guntschna's lies in the
    vineyards above Bolzano, its houses are Gries', 2026-10-29. The villages' own agglomerations: one of the whole
    commune, a Lithuanian district's 1,700 km2, kept the road tiles minutes and had them killed, 2026-10-30.)"""
    out = []
    for c in communes:
        a = agglo_of(c)
        if a is None and (c.sites is not None or c.within is not None):     # (a PICTOGRAMS square's too)
            continue
        if a is None and _km2(c.lonlat) > ALONE_MAX_KM2:     # (no built-up land in a large one: not all of it a town)
            continue
        out.append(c._replace(agglo=a))
    for k, main in enumerate(out):
        if main.country not in CENTRES or main.sites is None or main.agglo is None or not is_seat(main.name,
                                                                                                    main.town):
            continue
        # (the villages of its commune reached from it through agglomerations that touch)
        rest = [j for j, c in enumerate(out) if j != k and c.group == main.group and c.town == main.town
                and c.agglo is not None]
        reached, todo = set(), [k]
        while todo:
            a = out[todo.pop()].agglo
            for j in rest:
                if j not in reached and _touch(a, out[j].agglo):
                    reached.add(j)
                    todo.append(j)
        for j in reached:
            out[j] = out[j]._replace(name=main.name, local=main.local, sub=main.sub)
    return out


def _touch(a, b):
    """Do the agglomerations a and b (of one commune's villages: one grid, agglomeration's steps and origin of the
    same rings) share a cell? Pure."""
    oi = int(round((b.lat0 - a.lat0) / a.dlat))
    oj = int(round((b.lon0 - a.lon0) / a.dlon))
    (ha, wa), (hb, wb) = a.mask.shape, b.mask.shape
    r0, r1, c0, c1 = max(0, oi), min(ha, oi + hb), max(0, oj), min(wa, oj + wb)
    if r0 >= r1 or c0 >= c1:
        return False
    return bool((a.mask[r0:r1, c0:c1] & b.mask[r0 - oi:r1 - oi, c0 - oj:c1 - oj]).any())


def _runs(s, st):
    """[[state, first, last]] (indices into s) of the stretches of equal state st along stations s, the ones inside
    shorter than MIN_RUN_M (MAX_GAP_M: a stretch out of every agglomeration between two of the same commune's) taken
    into their neighbours (the shortest first: into both where they agree, else into the longer one). A stretch at
    either end stays: the segment's neighbour may go on with it."""
    runs = []
    for k, v in enumerate(st):
        if runs and runs[-1][0] == v:
            runs[-1][2] = k
        else:
            runs.append([int(v), k, k])

    def length(r):
        return s[min(r[2] + 1, len(s) - 1)] - s[r[1]]

    def short(j):
        gap = runs[j][0] < 0 and runs[j - 1][0] >= 0 and runs[j - 1][0] == runs[j + 1][0]
        return length(runs[j]) < (MAX_GAP_M if gap else MIN_RUN_M)
    while len(runs) > 2:
        cands = [j for j in range(1, len(runs) - 1) if short(j)]
        if not cands:
            break
        j = min(cands, key=lambda j: (length(runs[j]), j))
        a, r, b = runs[j - 1], runs[j], runs[j + 1]
        if a[0] == b[0]:
            runs[j - 1:j + 2] = [[a[0], a[1], b[2]]]
        elif length(a) >= length(b):
            runs[j - 1:j + 1] = [[a[0], a[1], r[2]]]
        else:
            runs[j:j + 2] = [[b[0], r[1], b[2]]]
    return runs


def _changes(line, communes, frame):
    """[(s, a, b)]: the arc lengths along line (k, 3) where the state (state's, its short stretches taken out:
    _runs) changes from a to b, each found to within STEP_M / 2**REFINE. The line's whole length is read, its last
    stretch too, so a road made of several segments has each change in exactly one of them."""
    L = furniture._arclen(line)[-1]
    if L <= 0:
        return []
    s = np.append(np.arange(0.0, L, STEP_M), L)
    p, _ = furniture._at(line, s)
    st = state(p[:, :2], communes, frame)
    # (the stretches are of towns: the villages of one German Gemeinde grown together are one, no sign between them)
    town = _towns(communes)
    tk = town[st]
    runs = _runs(s, tk)
    out = []
    for left, right in zip(runs[:-1], runs[1:]):
        # (a stretch's ends are stations of the raw state: the change lies between the last of one and the first of
        # the next)
        k = left[2]
        lo, hi, a = float(s[k]), float(s[k + 1]), int(tk[k])
        for _ in range(REFINE):
            mid = (lo + hi) / 2
            q, _ = furniture._at(line, np.array([mid]))
            if int(town[state(q[:, :2], communes, frame)[0]]) == a:
                lo = mid
            else:
                hi = mid
        # the communes each side: the town's nearest the change (its village whose name the sign writes)
        before = next((int(st[i]) for i in range(left[2], left[1] - 1, -1) if tk[i] == left[0]), -1)
        after = next((int(st[i]) for i in range(right[1], right[2] + 1) if tk[i] == right[0]), -1)
        out.append(((lo + hi) / 2, before, after))
    return out


def _towns(communes):
    """Per commune index (and -1 last: outside every commune), the town it belongs to as an int: its group (Commune's,
    the Gemeinde of a German or Swiss village: GROUPED) or itself; -1 for -1."""
    ids = {}
    return np.array([ids.setdefault(c.group or c.fid or k, len(ids)) for k, c in enumerate(communes)] + [-1],
                    np.int64)


def _sign(commune, line, s, travel, width, left_hand, main=False):
    """The commune's sign at arc length s along line, on the kerb side of the traffic going travel (+1 along the line,
    -1 against it): its entry face facing that traffic, its exit face on the back (the same spot, turned round); two
    rows [group, x, y, z, yaw, scale, height, rank, name, local, country, out, sub, main]: out, the heading (degrees
    counter-clockwise from the frame's east) of the traffic leaving the town there (next_place's); sub, the commune's
    small line (village_sub); main, whether the road is a main road (MAIN_CLASSES)."""
    p, d = furniture._at(line, np.array([s]))
    f = d[0] * travel
    side = np.array([f[1], -f[0]]) * (-1 if left_hand else 1)
    q = p[0][:2] + side * (width / 2 + SIGN_OFF_M)
    yaw = furniture._yaw(side[0], side[1])
    at = [round(float(q[0]), 2), round(float(q[1]), 2), round(float(p[0][2]), 2)]
    tail = [commune.name, commune.local, commune.country, round(math.degrees(math.atan2(-f[1], -f[0])), 1),
            commune.sub, bool(main)]
    return [[G_ENTRY] + at + [round(yaw, 1), 1.0, SIGN_H, 0.0] + tail,
            [G_EXIT] + at + [round((yaw + 180.0) % 360.0, 1), 1.0, SIGN_H, 0.0] + tail]


def town_signs(roads, communes, frame, box, left_hand=False, rings=()):
    """Sign rows [group, x, y, z, yaw, scale, height, rank, name, local, country, ...] (furniture's prop rows, the
    commune's name, its regional name and its country, then _sign's) of a tile box (x0, y0, x1, y1): on the roads of
    SIGN_CLASSES
    (build_tile's (fid, spec, centreline (k, 3), lon)) where they enter or leave a commune's agglomeration (communes:
    [Commune], communes_of's with agglo set; frame: the roads' geodesy.EnuFrame). Each way in gets the commune's sign
    on its kerb side (_sign: its exit face on the back, which the way out sees across the road). A one-way road has its
    way out's sign too, on the far kerb, turned to the way that does not exist: its back is the exit sign its traffic
    sees. A roundabout's ring (rings: their ids) has none: its approaches carry them (10 of Troyes' 121 entry signs
    stood on rings, each a few metres from the one on the road coming in, 2026-10-29)."""
    x0, y0, x1, y1 = box
    rows = []
    if not communes:
        return rows
    for fid, spec, line, _ in roads:
        if spec.cls not in SIGN_CLASSES or spec.link or spec.kind != "paved" or len(line) < 2 or fid in rings:
            continue
        line = np.asarray(line, np.float64)
        ways = (1.0, -1.0)
        if spec.oneway:
            ways = (-1.0,) if (spec.dir or 1) < 0 else (1.0,)
        main = spec.cls in MAIN_CLASSES
        for s, a, b in _changes(line, communes, frame):
            for travel in ways:
                # the traffic going along the line leaves a and enters b; the other way, the reverse
                leave, enter = (a, b) if travel > 0 else (b, a)
                if enter >= 0:
                    rows += _sign(communes[enter], line, s, travel, spec.width, left_hand, main)
                if spec.oneway and leave >= 0:      # (no way in on this kerb's far side to carry the exit sign)
                    rows += _sign(communes[leave], line, s, -travel, spec.width, left_hand, main)
    return [r for r in rows if x0 <= r[1] < x1 and y0 <= r[2] < y1]


def face_group(name, group, local="", country="FR", nxt="", km=0, sub="", main=False):
    """The prop group (and file name) of a commune's sign face: 'townin_<hash>' (G_ENTRY) or 'townout_<hash>', the
    hash of its name, regional name and country (the same names, the same face, whatever the anchor or tile; a name in
    two countries, two signs). A country with its own plate (PLATES) has its code after the prefix, 'townin_be_<hash>':
    the mod gives it that country's body."""
    key = name if not local else name + "\n" + local
    if country != "FR":
        key = country + "\n" + key
    if nxt:                             # (an exit face naming the next place: next_place, and its distance)
        key += "\n>" + nxt + (f" {km}" if km else "")
    if sub:
        key += "\n_" + sub
    if main:                            # (a main road's sign: MAIN_SIGNS)
        key += "\n^"
    h = hashlib.blake2b(key.encode("utf-8"), digest_size=6).hexdigest()
    cc = country.lower() + "_" if country in PLATES else ""
    return ("townin_" if group == G_ENTRY else "townout_") + cc + h


def plate_w(country):
    """The plate's width (m) of the country's sign."""
    return PLATES.get(country, PLATE_W)


def _font(size, text=""):
    """Pillow's own scalable typeface (Pillow 10.1+, the server's requirements ask for 11): no font file to find on
    the player's computer. A Cyrillic or Greek text (NONLATIN), whose letters it has not (it drew boxes, Београд
    2026-10-30), the system's typeface (SYSTEM_FONTS: Windows' Arial), Pillow's own if none is there."""
    from PIL import ImageFont
    for script, names in SCRIPT_FONTS:
        if script.search(text):
            for name in names:
                try:
                    return ImageFont.truetype(name, size)
                except OSError:
                    continue
    if NONLATIN.search(text):
        for name in SYSTEM_FONTS:
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


# (and Icelandic's ð and þ and Maltese's ħ, that no accent makes either: Ólafsfjörður drew a box, 2026-10-30;
# Arabic, shaped first: arabic_shape)
# (Ge'ez, Tifinagh: SCRIPT_FONTS; Arabic's presentation forms, arabic_shape's: a shaped name is drawn by its font too)
NONLATIN = re.compile(r"[Ͱ-ϿЀ-ӿÐðÞþĦħ؀-ۿሀ-᎟ⴰ-⵿ﭐ-﷿ﹰ-﻿֐-׿Ⴀ-ჿ԰-֏\u0900-\u0fff\u1000-\u109f\u1780-\u17ff"
                      r"\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\u1100-\u11ff]")
ARABIC = re.compile(r"[؀-ۿ]")
HEBREW = re.compile(r"[֐-׿]")     # (written right to left, unjoined: mark_text reverses it)


# Arabic letters' presentation forms (isolated, final, initial, medial; a letter that joins only the one before
# it, two): Pillow without its optional shaping library (raqm, not in the player's wheel on Windows) draws an Arabic
# name's letters apart and left to right (بوثلجة, Boutheldja's sign, came out ب و ث ل ج ة reversed, 2026-10-31)
_AR_FORMS = {
    "ء": (0xFE80,), "آ": (0xFE81, 0xFE82), "أ": (0xFE83, 0xFE84), "ؤ": (0xFE85, 0xFE86), "إ": (0xFE87, 0xFE88),
    "ئ": (0xFE89, 0xFE8A, 0xFE8B, 0xFE8C), "ا": (0xFE8D, 0xFE8E), "ب": (0xFE8F, 0xFE90, 0xFE91, 0xFE92),
    "ة": (0xFE93, 0xFE94), "ت": (0xFE95, 0xFE96, 0xFE97, 0xFE98), "ث": (0xFE99, 0xFE9A, 0xFE9B, 0xFE9C),
    "ج": (0xFE9D, 0xFE9E, 0xFE9F, 0xFEA0), "ح": (0xFEA1, 0xFEA2, 0xFEA3, 0xFEA4), "خ": (0xFEA5, 0xFEA6, 0xFEA7, 0xFEA8),
    "د": (0xFEA9, 0xFEAA), "ذ": (0xFEAB, 0xFEAC), "ر": (0xFEAD, 0xFEAE), "ز": (0xFEAF, 0xFEB0),
    "س": (0xFEB1, 0xFEB2, 0xFEB3, 0xFEB4), "ش": (0xFEB5, 0xFEB6, 0xFEB7, 0xFEB8), "ص": (0xFEB9, 0xFEBA, 0xFEBB, 0xFEBC),
    "ض": (0xFEBD, 0xFEBE, 0xFEBF, 0xFEC0), "ط": (0xFEC1, 0xFEC2, 0xFEC3, 0xFEC4), "ظ": (0xFEC5, 0xFEC6, 0xFEC7, 0xFEC8),
    "ع": (0xFEC9, 0xFECA, 0xFECB, 0xFECC), "غ": (0xFECD, 0xFECE, 0xFECF, 0xFED0), "ف": (0xFED1, 0xFED2, 0xFED3, 0xFED4),
    "ق": (0xFED5, 0xFED6, 0xFED7, 0xFED8), "ك": (0xFED9, 0xFEDA, 0xFEDB, 0xFEDC), "ل": (0xFEDD, 0xFEDE, 0xFEDF, 0xFEE0),
    "م": (0xFEE1, 0xFEE2, 0xFEE3, 0xFEE4), "ن": (0xFEE5, 0xFEE6, 0xFEE7, 0xFEE8), "ه": (0xFEE9, 0xFEEA, 0xFEEB, 0xFEEC),
    "و": (0xFEED, 0xFEEE), "ى": (0xFEEF, 0xFEF0), "ي": (0xFEF1, 0xFEF2, 0xFEF3, 0xFEF4),
    # (Persian's and the Maghreb's own letters: پ چ ژ گ ک ی ڤ)
    "پ": (0xFB56, 0xFB57, 0xFB58, 0xFB59), "چ": (0xFB7A, 0xFB7B, 0xFB7C, 0xFB7D), "ژ": (0xFB8A, 0xFB8B),
    "گ": (0xFB92, 0xFB93, 0xFB94, 0xFB95), "ک": (0xFB8E, 0xFB8F, 0xFB90, 0xFB91), "ی": (0xFBFC, 0xFBFD, 0xFBFE, 0xFBFF),
    "ڤ": (0xFB6A, 0xFB6B, 0xFB6C, 0xFB6D)}
# lam with an alef after it: one ligature (isolated, final)
_AR_LAM_ALEF = {"آ": (0xFEF5, 0xFEF6), "أ": (0xFEF7, 0xFEF8), "إ": (0xFEF9, 0xFEFA), "ا": (0xFEFB, 0xFEFC)}


def arabic_shape(text):
    """An Arabic text as Pillow draws it right without text shaping: each letter its joined form (_AR_FORMS: by
    whether the letters before and after it join it), lam and alef one ligature, the whole reversed (drawn left to
    right, it reads right to left); its vowel marks left out. Pure."""
    import unicodedata
    s = [c for c in text if not unicodedata.combining(c) and c != "ـ"]     # (marks; the tatweel)
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        prev = s[i - 1] if i else ""
        joins_prev = len(_AR_FORMS.get(prev, ())) == 4         # (the letter before joins the next one)
        if c == "ل" and i + 1 < len(s) and s[i + 1] in _AR_LAM_ALEF:
            out.append(chr(_AR_LAM_ALEF[s[i + 1]][1 if joins_prev else 0]))
            i += 2
            continue
        forms = _AR_FORMS.get(c)
        if not forms:
            out.append(c)
        elif len(forms) == 1:
            out.append(chr(forms[0]))
        else:
            nxt = s[i + 1] if i + 1 < len(s) else ""
            joins_next = len(forms) == 4 and len(_AR_FORMS.get(nxt, ())) > 1
            if joins_prev and joins_next:
                out.append(chr(forms[3]))           # medial
            elif joins_prev:
                out.append(chr(forms[1]))           # final
            elif joins_next:
                out.append(chr(forms[2]))           # initial
            else:
                out.append(chr(forms[0]))           # isolated
        i += 1
    return "".join(reversed(out))
SYSTEM_FONTS = ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf")
# the scripts Arial has not: Ge'ez (Ethiopia's, Eritrea's) and Tifinagh (Morocco's, Algeria's), Windows' Ebrima
# has them (Noto's elsewhere)
SCRIPT_FONTS = ((re.compile(r"[\u1200-\u139f\u2d30-\u2d7f]"),
                 ("ebrimabd.ttf", "ebrima.ttf", "NotoSansEthiopic-Bold.ttf", "NotoSansTifinagh-Regular.ttf")),
                # (and Georgian and Armenian: Windows' Sylfaen)
                (re.compile(r"[Ⴀ-ჿ԰-֏]"),
                 ("sylfaen.ttf", "NotoSansGeorgian-Bold.ttf", "NotoSansArmenian-Bold.ttf")),
                # (Asia's: Korean, Japanese kana, Chinese characters, Thai and Lao, the Indic scripts, Burmese, Khmer)
                (re.compile(r"[\uac00-\ud7af\u1100-\u11ff]"), ("malgunbd.ttf", "malgun.ttf", "NotoSansCJK-Bold.ttc")),
                (re.compile(r"[\u3040-\u30ff]"), ("YuGothB.ttc", "meiryob.ttc", "msyhbd.ttc", "NotoSansCJK-Bold.ttc")),
                (re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]"), ("msyhbd.ttc", "msyh.ttc", "YuGothB.ttc",
                                                               "NotoSansCJK-Bold.ttc")),
                (re.compile(r"[\u0e00-\u0e7f]"), ("LEELAWDB.TTF", "LEELAWAD.TTF", "NotoSansThai-Bold.ttf")),
                (re.compile(r"[\u0e80-\u0eff]"), ("LeelawUI.ttf", "NotoSansLao-Bold.ttf")),
                (re.compile(r"[\u0900-\u0dff]"), ("Nirmala.ttc", "NotoSansDevanagari-Bold.ttf")),
                (re.compile(r"[\u1000-\u109f]"), ("mmrtextb.ttf", "mmrtext.ttf", "NotoSansMyanmar-Bold.ttf")),
                (re.compile(r"[\u1780-\u17ff]"), ("LeelawUI.ttf", "KhmerUI.ttf", "NotoSansKhmer-Bold.ttf")))
# the scripts whose letters are reordered and joined in ways Pillow draws only with its optional raqm library (the
# Indic scripts, Tibetan, Burmese, Khmer): without it, a sign shows the name's Latin form only (shaped_ok)
NEEDS_SHAPING = re.compile(r"[\u0900-\u0dff\u0f00-\u0fff\u1000-\u109f\u1780-\u17ff]")


def shaped_ok(text):
    """Can Pillow draw text right: no NEEDS_SHAPING script in it, or its raqm text shaping there."""
    if not NEEDS_SHAPING.search(text):
        return True
    from PIL import features
    return bool(features.check("raqm"))


def sign_text(name):
    """The name as the sign writes it: capitals without their accents (SAINT-CHRISTOL-LEZ-ALES, as many French signs
    do; Pillow's own typeface has no È and drew a box for it, 2026-10-28)."""
    import unicodedata
    text = unicodedata.normalize("NFD", name.upper())
    return "".join(c for c in text if not unicodedata.combining(c))


def _lines(name, limit=14):
    """The name's lines on the sign: one, or two split at the hyphen nearest the middle when it is longer than limit
    (SAINT-CHRISTOL- / LEZ-ALES), as on the long signs."""
    if len(name) <= limit or "-" not in name[1:-1]:
        return [name]
    cuts = [i for i, c in enumerate(name) if c == "-" and 0 < i < len(name) - 1]
    i = min(cuts, key=lambda k: abs(k - len(name) / 2))
    return [name[:i + 1], name[i + 1:]]


# The accents a regional name is written with (Pillow's own typeface has no accented letter: each is drawn over its
# letter, mark_text): the combining mark -> its name
MARKS = {"̀": "grave", "́": "acute", "̂": "circumflex", "̃": "tilde", "̈": "diaeresis",
         "̧": "cedilla", "̌": "caron",          # (the caron: Sorbian's Budyšin)
         # (Romanian's ă and ș, Latvian's ā, Lithuanian's ą and ė, Hungarian's ő, Czech's ů: Vălenii de Munte lost its
         # breve, 2026-10-30)
         "̆": "breve", "̄": "macron", "̇": "dot", "̊": "ring", "̋": "double_acute",
         "̨": "ogonek", "̦": "comma"}
BELOW = {"cedilla", "ogonek", "comma"}  # (the marks under their letter)
MARK_ABOVE = 0.32               # the room the marks above a letter take, a share of the font size
MARK_BELOW = 0.26               # ... a cedilla below


# Letters Pillow's own typeface has not and no accent makes (it drew a box): their letters, and the mark drawn on the
# first (_draw_marks' "bar": Ł, Ø's stroke); ß written ss, as German writes it without the letter
LETTERS = {"ß": ("ss", None), "Ł": ("L", "bar"), "ł": ("l", "bar"), "Ø": ("O", "slash"), "ø": ("o", "slash"),
           "Đ": ("D", "bar"), "đ": ("d", "bar"), "Æ": ("AE", None), "æ": ("ae", None), "Œ": ("OE", None),
           "œ": ("oe", None)}


def mark_text(text):
    """(letters, marks): the text without its accents, and {letter index: [mark name]} of the ones MARKS draws (Tuïr:
    'Tuir', {2: ['diaeresis']}); other accents are left out; LETTERS' letters as they write them. A Cyrillic or Greek
    text as it is (the system's typeface draws it, _font), an Arabic one shaped (arabic_shape), a Hebrew one
    reversed. Pure."""
    import unicodedata
    if ARABIC.search(text):
        return arabic_shape(text), {}
    if HEBREW.search(text):
        return text[::-1], {}
    if NONLATIN.search(text):
        return text, {}
    letters, marks = "", {}
    for c in unicodedata.normalize("NFD", text):
        if c in LETTERS:
            base, mark = LETTERS[c]
            if mark:
                marks.setdefault(len(letters), []).append(mark)
            letters += base
        elif not unicodedata.combining(c):
            letters += c
        elif letters and c in MARKS:
            marks.setdefault(len(letters) - 1, []).append(MARKS[c])
    return letters, marks


def _marks_box(d, letters, marks, font, size):
    """textbbox of letters, its top raised and its bottom lowered by the room their marks take."""
    bx = list(d.textbbox((0, 0), letters, font=font))
    kinds = {m for ms in marks.values() for m in ms}
    if kinds - BELOW:
        top = min(font.getbbox(letters[k])[1] for k, ms in marks.items() if set(ms) - BELOW)
        bx[1] = min(bx[1], round(top - MARK_ABOVE * size))
    if kinds & BELOW:
        bx[3] = max(bx[3], round(max(font.getbbox(letters[k])[3] for k, ms in marks.items() if set(ms) & BELOW)
                                 + MARK_BELOW * size))
    return tuple(bx)


def _draw_marks(d, x, y, letters, marks, font, size, fill, paper):
    """The accents of letters drawn at text origin (x, y) (as d.text's), over or under their letters; an i's or j's
    dot painted over with paper first (ï: its two dots only)."""
    s, lw = size, max(1, round(size * 0.08))
    for k, ms in marks.items():
        x0 = x + d.textlength(letters[:k], font=font)
        x1 = x0 + d.textlength(letters[k], font=font)
        cx = (x0 + x1) / 2
        bb = font.getbbox(letters[k])
        top, bottom = y + bb[1], y + bb[3]
        if letters[k] in "ij" and set(ms) - BELOW:
            xh = y + font.getbbox("x")[1]                   # (the dot is above the x-height)
            d.rectangle([x0, top - 1, x1, xh - max(1, round(.04 * s))], fill=paper)
            top = xh
        for m in ms:
            if m == "acute":
                d.line([(cx - .10 * s, top - .07 * s), (cx + .10 * s, top - .27 * s)], fill=fill, width=lw)
            elif m == "bar":            # (Ł's, Đ's stroke across its stem)
                mid = (top + bottom) / 2 + (.12 * s if letters[k] in "Ll" else 0.0)
                d.line([(x0 + .02 * s, mid + .08 * s), (x0 + .32 * s, mid - .1 * s)], fill=fill, width=lw)
            elif m == "slash":          # (Ø's stroke across the O)
                d.line([(x1 - .05 * s, top - .02 * s), (x0 + .05 * s, bottom + .02 * s)], fill=fill, width=lw)
            elif m == "caron":
                d.line([(cx - .14 * s, top - .25 * s), (cx, top - .07 * s), (cx + .14 * s, top - .25 * s)], fill=fill,
                       width=lw, joint="curve")
            elif m == "grave":
                d.line([(cx - .10 * s, top - .27 * s), (cx + .10 * s, top - .07 * s)], fill=fill, width=lw)
            elif m == "circumflex":
                d.line([(cx - .14 * s, top - .07 * s), (cx, top - .25 * s), (cx + .14 * s, top - .07 * s)], fill=fill,
                       width=lw, joint="curve")
            elif m == "tilde":
                pts = [(cx + (t / 8 - 0.5) * .32 * s, top - .16 * s - .05 * s * math.sin(t / 8 * 2 * math.pi))
                       for t in range(9)]
                d.line(pts, fill=fill, width=lw, joint="curve")
            elif m == "diaeresis":
                r = .07 * s
                for dx in (-.15 * s, .15 * s):
                    d.ellipse([cx + dx - r, top - .16 * s - r, cx + dx + r, top - .16 * s + r], fill=fill)
            elif m == "cedilla":
                d.line([(cx, bottom), (cx, bottom + .09 * s), (cx + .08 * s, bottom + .15 * s),
                        (cx - .07 * s, bottom + .24 * s)], fill=fill, width=lw, joint="curve")
            elif m == "breve":          # (the lower half of a small ring)
                d.arc([cx - .13 * s, top - .34 * s, cx + .13 * s, top - .08 * s], 0, 180, fill=fill, width=lw)
            elif m == "macron":
                d.line([(cx - .14 * s, top - .17 * s), (cx + .14 * s, top - .17 * s)], fill=fill, width=lw)
            elif m == "dot":
                r = .07 * s
                d.ellipse([cx - r, top - .17 * s - r, cx + r, top - .17 * s + r], fill=fill)
            elif m == "ring":
                r = .08 * s
                d.ellipse([cx - r, top - .2 * s - r, cx + r, top - .2 * s + r], outline=fill, width=max(1, lw // 2))
            elif m == "double_acute":
                for dx in (-.08 * s, .08 * s):
                    d.line([(cx + dx - .06 * s, top - .07 * s), (cx + dx + .06 * s, top - .27 * s)], fill=fill,
                           width=lw)
            elif m == "ogonek":         # (a hook under the letter's right foot)
                d.line([(x1 - .1 * s, bottom - .02 * s), (x1 - .17 * s, bottom + .12 * s),
                        (x1 - .04 * s, bottom + .2 * s)], fill=fill, width=lw, joint="curve")
            elif m == "comma":          # (Romanian's ș, ț: a comma under it)
                d.line([(cx + .02 * s, bottom + .05 * s), (cx - .04 * s, bottom + .22 * s)], fill=fill, width=lw)


def face_image(name, group, local="", country="FR", nxt="", km=0, sub="", main=False):
    """A commune's sign face (PIL RGB image, plate_w x PLATE_H at FACE_PX_PER_M), as its country's signs are (FACES:
    the French one unless another is written there; nxt, km: the next place an exit face names and how far, NEXT_PLACE;
    sub: the small line, village_sub; main: the sign of a main road, MAIN_SIGNS). Pure."""
    return FACES.get(country, _face_fr)(name, group, local, nxt=nxt, km=km, sub=sub, main=main)


def _face_fr(name, group, local="", **_):
    """The French EB10 / EB20: white, its red border, the name in black capitals and under it, smaller and lighter,
    its regional name (local, "": none) as the data writes it, its accents drawn (mark_text: Tuïr, Llupià), as the
    bilingual signs of Roussillon or Brittany set it apart; the exit sign (G_EXIT) crossed by a red bar from its lower
    left to its upper right."""
    from PIL import Image, ImageDraw
    w, h = round(PLATE_W * FACE_PX_PER_M), round(PLATE_H * FACE_PX_PER_M)
    img = Image.new("RGB", (w, h), FACE_WHITE)
    d = ImageDraw.Draw(img)
    m, b = round(MARGIN_FRAC * h), round(BORDER_FRAC * h)
    d.rounded_rectangle([m, m, w - 1 - m, h - 1 - m], radius=3 * b, fill=FACE_RED)
    d.rounded_rectangle([m + b, m + b, w - 1 - m - b, h - 1 - m - b], radius=2 * b, fill=FACE_WHITE)
    lines = _lines(sign_text(name))
    # (its smaller letters: one line up to LOCAL_LINE letters, Donibane-Garazi)
    second = [mark_text(ln) for ln in _lines(local, LOCAL_LINE)] if local else []
    room_w, room_h = w - 2 * (m + 2 * b), h - 2 * (m + 2 * b)
    size = round(TEXT_FRAC * h / (1.0 if len(lines) == 1 else 1.6) / (1.0 if not second else 1.0 + LOCAL_FRAC))
    while size > 8:
        # (text, its marks, font, its size, stroke, box, space above): the French name's lines, then the regional
        # name's
        small = max(8, round(size * LOCAL_FRAC))
        font, sfont = _font(size), _font(small)
        sw, ssw = max(1, size // 18), max(1, small // 30)
        items = [(ln, {}, font, size, sw, d.textbbox((0, 0), ln, font=font, stroke_width=sw),
                  0.0 if k == 0 else size * 0.25) for k, ln in enumerate(lines)]
        items += [(ln, mk, sfont, small, ssw, _marks_box(d, ln, mk, sfont, small),
                   size * (LOCAL_GAP if k == 0 else 0.15)) for k, (ln, mk) in enumerate(second)]
        tw = max(it[5][2] - it[5][0] for it in items)
        th = sum(it[5][3] - it[5][1] + it[6] for it in items)
        if tw <= room_w and th <= room_h:
            break
        size -= 2
    y = (h - th) / 2
    for ln, mk, f, fs, sw, bx, above in items:
        y += above
        at = ((w - (bx[2] - bx[0])) / 2 - bx[0], y - bx[1])
        d.text(at, ln, font=f, fill=(0, 0, 0), stroke_width=sw, stroke_fill=(0, 0, 0))
        _draw_marks(d, at[0], at[1], ln, mk, f, fs, (0, 0, 0), FACE_WHITE)
        y += bx[3] - bx[1]
    if group == G_EXIT:
        bar = round(BAR_FRAC * h)
        d.line([(m + b, h - 1 - m - b), (w - 1 - m - b, m + b)], fill=FACE_RED, width=bar)
    return img


# Belgium's F1a / F3a (Code de la route, art. 71, since 2004; the drawings: Wikimedia Commons "Belgian traffic sign
# F1a horizontaal", "F3a"): white, a black border and a thin black line inside it, the agglomeration's name at the top
# and under it a village's black silhouette; the exit sign crossed by a red bar (#ED1C24) from its lower left to its
# upper right. Its names are its region's language (Overture's primary name: Dutch in Flanders, French in Wallonia,
# German in the German-speaking Community); Brussels' communes have both, "Ixelles - Elsene", on two lines of equal
# weight, as the Brussels signs write them.
BE_RED = (237, 28, 36)
BE_BORDER_FRAC = 0.045          # the black border, a share of the plate's height
BE_LINE_FRAC = 0.012            # the thin black line inside it
BE_NAME_FRAC = 0.30             # the name's room, a share of the inside's height (the silhouette has the rest)


def _silhouette(d, x0, y0, x1, y1, fill):
    """A village's silhouette (the Belgian F1's pictogram) in the box: houses with gabled roofs either side of a church
    with its tower and spire. Pure but for drawing."""
    w, h = x1 - x0, y1 - y0

    def P(u, v):
        return (x0 + u * w, y0 + v * h)
    # (left, right, eaves, ridge) as shares of the box, the ground at its bottom
    for a, b, eaves, ridge in ((0.0, 0.17, 0.62, 0.42), (0.13, 0.33, 0.50, 0.26), (0.62, 0.82, 0.52, 0.30),
                               (0.78, 1.0, 0.64, 0.44)):
        d.polygon([P(a, 1), P(a, eaves), P((a + b) / 2, ridge), P(b, eaves), P(b, 1)], fill=fill)
    d.polygon([P(0.32, 1), P(0.32, 0.55), P(0.47, 0.40), P(0.63, 0.55), P(0.63, 1)], fill=fill)    # the nave
    d.polygon([P(0.40, 1), P(0.40, 0.28), P(0.455, 0.0), P(0.51, 0.28), P(0.51, 1)], fill=fill)    # tower, spire


def _face_be(name, group, local="", **_):
    """The Belgian F1a (G_ENTRY) / F3a (G_EXIT), above. local is not used (Belgium's signs are in its region's
    language)."""
    from PIL import Image, ImageDraw
    w, h = round(plate_w("BE") * FACE_PX_PER_M), round(PLATE_H * FACE_PX_PER_M)
    img = Image.new("RGB", (w, h), FACE_WHITE)
    d = ImageDraw.Draw(img)
    m, b, ln = round(MARGIN_FRAC * h), round(BE_BORDER_FRAC * h), max(2, round(BE_LINE_FRAC * h))
    d.rounded_rectangle([m, m, w - 1 - m, h - 1 - m], radius=2 * b, fill=(0, 0, 0))
    d.rounded_rectangle([m + b, m + b, w - 1 - m - b, h - 1 - m - b], radius=b, fill=FACE_WHITE)
    g = m + b + round(0.6 * b)                      # the thin line inside the border
    d.rounded_rectangle([g, g, w - 1 - g, h - 1 - g], radius=b, outline=(0, 0, 0), width=ln)
    pad = g + ln + round(0.5 * b)
    room_w, room_h = w - 2 * pad, h - 2 * pad
    names = [mark_text(n.strip()) for n in name.split(" - ") if n.strip()] or [mark_text(name)]
    name_h = room_h * BE_NAME_FRAC * (1.0 if len(names) == 1 else 1.55)
    size = round(name_h / len(names) / 0.85)
    while size > 8:
        font = _font(size)
        boxes = [_marks_box(d, t, mk, font, size) for t, mk in names]
        tw = max(bx[2] - bx[0] for bx in boxes)
        th = sum(bx[3] - bx[1] for bx in boxes) + (len(names) - 1) * size * 0.15
        if tw <= room_w and th <= name_h:
            break
        size -= 2
    y = pad + (name_h - th) / 2
    sw = max(1, size // 22)
    for (t, mk), bx in zip(names, boxes):
        at = ((w - (bx[2] - bx[0])) / 2 - bx[0], y - bx[1])
        d.text(at, t, font=font, fill=(0, 0, 0), stroke_width=sw, stroke_fill=(0, 0, 0))
        _draw_marks(d, at[0], at[1], t, mk, font, size, (0, 0, 0), FACE_WHITE)
        y += bx[3] - bx[1] + size * 0.15
    top = pad + name_h + round(0.04 * room_h)
    sil_h = pad + room_h - top
    sil_w = min(room_w * 0.8, sil_h * 2.3)
    _silhouette(d, (w - sil_w) / 2, top, (w + sil_w) / 2, pad + room_h, (0, 0, 0))
    if group == G_EXIT:
        d.line([(g, h - 1 - g), (w - 1 - g, g)], fill=BE_RED, width=round(BAR_FRAC * h))
    return img


# Luxembourg's E,9a / E,9b (Arrêté grand-ducal du 23 novembre 1955, the drawings: Wikimedia Commons "Luxembourg road
# sign diagram E 09 a / b"): yellow, a black border, the village's official name in black capitals and under it its
# Luxembourgish name in italics (HESPERANGE / Hesper); the exit sign has the name struck through by a red line. (The
# real E,9b also names the next village above, with an arrow: not written here, the road's next village is not known
# to a tile.)
LU_YELLOW = (255, 228, 0)
LU_BORDER_FRAC = 0.05           # the black border, a share of the plate's height
LU_SHEAR = 0.21                 # the italics' slant (x per y)


def _italic(text, font, fill, size):
    """(RGBA image of text in font, slanted LU_SHEAR to the right: Pillow's own typeface has no italics, its letters
    are sheared, as an oblique), and the box of its ink."""
    from PIL import Image, ImageDraw
    letters, marks = mark_text(text)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    bx = _marks_box(probe, letters, marks, font, size)
    pad = round(size * 0.5)
    w, h = bx[2] - bx[0] + 2 * pad + round(LU_SHEAR * (bx[3] - bx[1])), bx[3] - bx[1] + 2 * pad
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    at = (pad - bx[0], pad - bx[1])
    d.text(at, letters, font=font, fill=255)
    _draw_marks(d, at[0], at[1], letters, marks, font, size, 255, 0)
    # x' = x + s (y - h): the foot stays, the top leans right (Image.transform maps output to input: the inverse)
    mask = mask.transform((w, h), Image.AFFINE, (1, LU_SHEAR, -LU_SHEAR * h, 0, 1, 0), resample=Image.BICUBIC)
    img = Image.new("RGBA", (w, h), fill + (0,))
    img.putalpha(mask)
    return img, mask.getbbox() or (0, 0, w, h)


def _face_lu(name, group, local="", nxt="", **_):
    """Luxembourg's E,9a (G_ENTRY) / E,9b (G_EXIT), above; an E,9b with the next place (nxt) has it at the top after
    an arrow up, the town's name struck through under it (Commons' "E 09 b": HOWALD over HESPERANGE)."""
    from PIL import Image, ImageDraw
    w, h = round(plate_w("LU") * FACE_PX_PER_M), round(PLATE_H * FACE_PX_PER_M)
    img = Image.new("RGB", (w, h), FACE_WHITE)
    d = ImageDraw.Draw(img)
    m, b = round(MARGIN_FRAC * h), round(LU_BORDER_FRAC * h)
    d.rectangle([m, m, w - 1 - m, h - 1 - m], fill=(0, 0, 0))
    d.rectangle([m + b, m + b, w - 1 - m - b, h - 1 - m - b], fill=LU_YELLOW)
    pad = m + 2 * b
    room_w, room_h = w - 2 * pad, h - 2 * pad
    if group == G_EXIT and nxt:
        _lu_exit_next(d, w, h, pad, room_w, room_h, sign_text(name), sign_text(nxt))
        return img
    upper = sign_text(name)
    size = round(room_h * (0.42 if local else 0.6))
    while size > 8:
        font = _font(size)
        sw = max(1, size // 20)
        bx = d.textbbox((0, 0), upper, font=font, stroke_width=sw)
        th, tw = bx[3] - bx[1], bx[2] - bx[0]
        it = None
        if local:
            it, ib = _italic(local, _font(round(size * 0.95)), (0, 0, 0), round(size * 0.95))
            th += ib[3] - ib[1] + size * 0.3
            tw = max(tw, ib[2] - ib[0])
        if tw <= room_w and th <= room_h:
            break
        size -= 2
    y = (h - th) / 2
    at = ((w - (bx[2] - bx[0])) / 2 - bx[0], y - bx[1])
    d.text(at, upper, font=font, fill=(0, 0, 0), stroke_width=sw, stroke_fill=(0, 0, 0))
    name_box = (at[0] + bx[0], y, at[0] + bx[2], y + bx[3] - bx[1])
    if it is not None:
        y += bx[3] - bx[1] + size * 0.3
        img.paste(it, (round((w - (ib[2] - ib[0])) / 2 - ib[0]), round(y - ib[1])), it)
    if group == G_EXIT:              # (the name struck through, from its lower left to its upper right)
        x0, y0, x1, y1 = name_box
        d.line([(x0 - 0.02 * w, y1 - 0.15 * (y1 - y0)), (x1 + 0.02 * w, y0 + 0.35 * (y1 - y0))], fill=FACE_RED,
               width=max(3, round(0.035 * h)))
    return img


def _lu_exit_next(d, w, h, pad, room_w, room_h, name, nxt):
    """The E,9b with the next place: a black arrow up at the left, the next place's name and under it the town's,
    struck through in red, both in black capitals, centred in the room right of the arrow."""
    aw = round(room_h * 0.55)                       # the arrow's width
    ax, top, bot = pad + round(0.04 * room_w), pad + round(0.08 * room_h), pad + round(0.92 * room_h)
    shaft = round(aw * 0.32)
    head = round((bot - top) * 0.42)
    cx = ax + aw / 2
    d.polygon([(cx, top), (ax + aw, top + head), (cx + shaft / 2, top + head), (cx + shaft / 2, bot),
               (cx - shaft / 2, bot), (cx - shaft / 2, top + head), (ax, top + head)], fill=(0, 0, 0))
    x_left = ax + aw + round(0.05 * room_w)
    rw = pad + room_w - x_left
    size = round(room_h * 0.4)
    while size > 8:
        font = _font(size)
        sw = max(1, size // 20)
        boxes = [d.textbbox((0, 0), t, font=font, stroke_width=sw) for t in (nxt, name)]
        tw = max(bx[2] - bx[0] for bx in boxes)
        th = sum(bx[3] - bx[1] for bx in boxes) + size * 0.35
        if tw <= rw and th <= room_h:
            break
        size -= 2
    y = (h - th) / 2
    for k, (t, bx) in enumerate(zip((nxt, name), boxes)):
        at = (x_left + (rw - (bx[2] - bx[0])) / 2 - bx[0], y - bx[1])
        d.text(at, t, font=font, fill=(0, 0, 0), stroke_width=sw, stroke_fill=(0, 0, 0))
        if k == 1:                                  # (the town's name struck through)
            x0, x1, y0, y1 = at[0] + bx[0], at[0] + bx[2], y, y + bx[3] - bx[1]
            d.line([(x0 - 0.01 * w, y1 - 0.15 * (y1 - y0)), (x1 + 0.01 * w, y0 + 0.35 * (y1 - y0))],
                   fill=FACE_RED, width=max(3, round(0.035 * h)))
        y += bx[3] - bx[1] + size * 0.35


# Germany's Zeichen 310 / 311 (StVO; the drawings: Wikimedia Commons "Zeichen 310-50 Ortstafel mit Kreis", "Zeichen
# 311-50", a 900 x 600 canvas): yellow #f0c900, a black border inset 10 and 15 wide, rounded; the entry face the
# village's name large and under it, small, its Kreis or Gemeinde (village_sub), a Sorbian or Frisian name between
# them; the exit face, the plate's back, the next place, its distance and an arrow up over a black line across the
# middle, the village's name under it crossed by a red (#c1121c) band from the lower left.
DE_YELLOW = (240, 201, 0)
DE_RED = (193, 18, 28)
# the exit's red band (the 900 x 600 drawing's polygon, the lower half's)
DE_BAND = ((18.71, 548.94), (772.30, 303.07), (884.19, 303.07), (882.68, 342.85), (158.01, 579.38), (35.46, 580.14),
           (18.71, 567.20))


def _face_de(name, group, local="", nxt="", km=0, sub="", **_):
    """Germany's Zeichen 310 (G_ENTRY) / 311 (G_EXIT), above."""
    from PIL import Image, ImageDraw
    w, h = round(plate_w("DE") * FACE_PX_PER_M), round(PLATE_H * FACE_PX_PER_M)
    sx, sy = w / 900.0, h / 600.0
    img = Image.new("RGB", (w, h), FACE_WHITE)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=round(37.5 * sy), fill=DE_YELLOW)
    d.rounded_rectangle([10 * sx, 10 * sy, w - 1 - 10 * sx, h - 1 - 10 * sy], radius=round(27.5 * sy),
                        outline=(0, 0, 0), width=max(2, round(15 * sy)))
    pad_x, pad_y = 45 * sx, 40 * sy

    def fit(text, size, room_w):
        """(font, its size, box) of text at size or smaller, as wide as room_w at most."""
        letters, marks = mark_text(text)
        while True:
            f = _font(size)
            bx = _marks_box(d, letters, marks, f, size)
            if bx[2] - bx[0] <= room_w or size <= 8:
                return f, size, bx, letters, marks
            size -= 2

    def put(item, cx, top):
        f, size, bx, letters, marks = item
        at = (cx - (bx[2] - bx[0]) / 2 - bx[0], top - bx[1])
        d.text(at, letters, font=f, fill=(0, 0, 0), stroke_width=max(1, size // 30), stroke_fill=(0, 0, 0))
        _draw_marks(d, at[0], at[1], letters, marks, f, size, (0, 0, 0), DE_YELLOW)
        return top + bx[3] - bx[1]
    room = w - 2 * pad_x
    if group == G_EXIT:
        if nxt:
            top = fit(nxt, round(105 * sy), room - 110 * sx)
            put(top, pad_x + (room - 110 * sx) / 2, 50 * sy)
            if km:
                put(fit(f"{km} km", round(62 * sy), room), pad_x + (room - 110 * sx) / 2, 190 * sy)
            ax, aw = w - pad_x - 75 * sx, 75 * sx            # (the arrow up, at the right)
            sh, hd = 12 * sx, 55 * sy
            d.polygon([(ax + aw / 2, 45 * sy), (ax + aw, 45 * sy + hd), (ax + aw / 2 + sh, 45 * sy + hd - 14 * sy),
                       (ax + aw / 2 + sh, 255 * sy), (ax + aw / 2 - sh, 255 * sy),
                       (ax + aw / 2 - sh, 45 * sy + hd - 14 * sy), (ax, 45 * sy + hd)], fill=(0, 0, 0))
        d.rectangle([20 * sx, 292.5 * sy, w - 1 - 20 * sx, 307.5 * sy], fill=(0, 0, 0))
        d.polygon([(x * sx, y * sy) for x, y in DE_BAND], fill=DE_RED)
        item = fit(name, round(150 * sy), room)
        put(item, w / 2, 450 * sy - (item[2][3] - item[2][1]) / 2)
        return img
    lines = [fit(name, round((150 if not (local or sub) else 135) * sy), room)]
    if local:
        lines.append(fit(local, round(105 * sy), room))
    if sub:
        lines.append(fit(sub, round(62 * sy), room))
    gaps = [0.0] + [45 * sy] * (len(lines) - 1)
    total = sum(it[2][3] - it[2][1] for it in lines) + sum(gaps)
    y = (h - total) / 2
    for it, gap in zip(lines, gaps):
        y = put(it, w / 2, y + gap)
    return img


# Switzerland's Ortschaftstafeln (Signalisationsverordnung 4.27 - 4.30; the drawings: Wikimedia Commons
# "CH-Hinweissignal-Ortsbeginn / Ortsende auf Haupt- / Nebenstrassen", 795 x 568): on a main road blue (#1a67bb), its
# letters white, a white edge; on another white, its letters and edge black. The entry face has the Ortschaft's name in
# mixed case, a bilingual name's two on two lines (Biel / Bienne), a homonym's canton under it, small (VD). The exit
# face (4.28 / 4.30) has no name of the town: a band at the top for the next place and under it the place the road
# leads to with its distance; the next place is that place here (the band left empty, as the rule has it when they are
# one), and the face is left blank without one.
CH_BLUE = (26, 103, 187)
CH_EDGE = 12.76                 # the paper's inset from the plate's edge, of the 795 x 568 drawing
CH_BAND = (11.34, 141.38)       # the exit's top band (y, from the top), the ink between it and the field below
CH_FIELD = (209.41, 555.24)


def _face_ch(name, group, local="", nxt="", km=0, sub="", main=False, **_):
    """Switzerland's 4.27 / 4.29 (G_ENTRY) and 4.28 / 4.30 (G_EXIT), above. local is not used (a Swiss name is in
    its place's language, a bilingual one has both)."""
    from PIL import Image, ImageDraw
    w, h = round(plate_w("CH") * FACE_PX_PER_M), round(PLATE_H * FACE_PX_PER_M)
    sx, sy = w / 795.0, h / 568.0
    paper, ink = (CH_BLUE, FACE_WHITE) if main else (FACE_WHITE, (0, 0, 0))
    img = Image.new("RGB", (w, h), ink)
    d = ImageDraw.Draw(img)
    if main:                                        # (the white edge's thin dark line)
        d.rectangle([0, 0, w - 1, h - 1], outline=(35, 31, 32), width=max(1, round(2.8 * sx)))
    e = CH_EDGE * sx
    d.rectangle([e, e, w - 1 - e, h - 1 - e], fill=paper)

    def fit(text, size, room_w):
        """(font, its size, box, letters, marks) of text at size or smaller, as wide as room_w at most."""
        letters, marks = mark_text(text)
        while True:
            f = _font(size)
            bx = _marks_box(d, letters, marks, f, size)
            if bx[2] - bx[0] <= room_w or size <= 8:
                return f, size, bx, letters, marks
            size -= 2

    def put(item, top):
        f, size, bx, letters, marks = item
        at = ((w - (bx[2] - bx[0])) / 2 - bx[0], top - bx[1])
        d.text(at, letters, font=f, fill=ink, stroke_width=max(1, size // 40), stroke_fill=ink)
        _draw_marks(d, at[0], at[1], letters, marks, f, size, ink, paper)
        return top + bx[3] - bx[1]

    def column(items, gaps, y0, y1):
        """items centred between y0 and y1, gaps above each but the first."""
        total = sum(it[2][3] - it[2][1] for it in items) + sum(gaps[1:])
        y = (y0 + y1 - total) / 2
        for it, gap in zip(items, gaps):
            y = put(it, y + gap)
    room = w - 2 * (e + 30 * sx)
    if group == G_EXIT:
        # (the band and the field, the ink between them)
        d.rectangle([e, CH_BAND[1] * sy, w - 1 - e, CH_FIELD[0] * sy], fill=ink)
        if nxt:
            items = [fit(nxt, round(175 * sy), room)]
            if km:
                items.append(fit(f"{km} km", round(110 * sy), room))
            column(items, [0.0, 40 * sy], CH_FIELD[0] * sy, CH_FIELD[1] * sy)
        return img
    names = [n.strip() for n in name.split("/") if n.strip()] or [name]
    big = 175 if len(names) == 1 and not sub else 160 if len(names) == 1 else 150
    # (a bilingual name's two at one size, the longer's)
    size = min(fit(n, round(big * sy), room)[1] for n in names)
    items = [fit(n, size, room) for n in names]
    gaps = [0.0] + [40 * sy] * (len(names) - 1)
    if sub:
        items.append(fit(sub, round(110 * sy), room))
        gaps.append(55 * sy)
    column(items, gaps, e, h - e)
    return img


# Italy's inizio / fine centro abitato (Regolamento del Codice della Strada, figura II 273 / 274; the drawing: Wikimedia
# Commons "Italian traffic signs - fine centro abitato (figura II 274)", 3350 x 1000): white, a black border inset as
# wide as itself (1/30 of the height), its corners rounded; the name in black capitals, a bilingual name's two on two
# lines (BOLZANO / BOZEN), a Friulian or Sardinian name under it, smaller (UDINE / Udin); the exit sign crossed by a red
# (#d40000) band from its lower left to its upper right. (The blue panel of the next places under a fine centro abitato
# is a sign of its own, not drawn.)
IT_RED = (212, 0, 0)


def _face_it(name, group, local="", **_):
    """Italy's figura II 273 (G_ENTRY) / 274 (G_EXIT), above."""
    from PIL import Image, ImageDraw
    w, h = round(plate_w("IT") * FACE_PX_PER_M), round(PLATE_H * FACE_PX_PER_M)
    s = h / 1000.0
    img = Image.new("RGB", (w, h), FACE_WHITE)
    d = ImageDraw.Draw(img)
    b = 32.6 * s
    d.rounded_rectangle([b, b, w - 1 - b, h - 1 - b], radius=round(115 * s), outline=(0, 0, 0),
                        width=max(2, round(b)))
    if group == G_EXIT:             # (under the letters: the band is behind the name, as the drawing has it)
        d.polygon([(2 * b, h - 2 * b - 0.10 * h), (w - 2 * b - 0.06 * h, 2 * b), (w - 2 * b, 2 * b + 0.04 * h),
                   (w - 2 * b, 2 * b + 0.14 * h), (2 * b + 0.06 * h, h - 2 * b), (2 * b, h - 2 * b)], fill=IT_RED)
    names = [mark_text(n.strip().upper()) for n in BILINGUAL.split(name) if n.strip()] or [mark_text(name.upper())]
    second = [mark_text(local)] if local else []
    room_w, room_h = w - 2 * (b + 90 * s), h - 2 * (b + 70 * s)
    size = round((560 if len(names) == 1 and not second else 380) * s)
    while size > 8:
        small = round(size * LOCAL_FRAC)
        font, sfont = _font(size), _font(small)
        items = [(t, mk, font, size, _marks_box(d, t, mk, font, size)) for t, mk in names]
        items += [(t, mk, sfont, small, _marks_box(d, t, mk, sfont, small)) for t, mk in second]
        gap = size * 0.18
        tw = max(it[4][2] - it[4][0] for it in items)
        th = sum(it[4][3] - it[4][1] for it in items) + gap * (len(items) - 1)
        if tw <= room_w and th <= room_h:
            break
        size -= 4
    y = (h - th) / 2
    for t, mk, f, fs, bx in items:
        at = ((w - (bx[2] - bx[0])) / 2 - bx[0], y - bx[1])
        d.text(at, t, font=f, fill=(0, 0, 0), stroke_width=max(1, fs // 36), stroke_fill=(0, 0, 0))
        _draw_marks(d, at[0], at[1], t, mk, f, fs, (0, 0, 0), FACE_WHITE)
        y += bx[3] - bx[1] + gap
    return img


# Austria's Ortstafel / Ortsende (StVO § 53 Z 17a / 17b; the drawings: Wikimedia Commons "Austria, Hinweiszeichen 17a
# (2010)", "Hinweiszeichen 17b (2010)", 1500 x 1000): white inside a blue (#009fe3) edge, the Ortschaft's name in black
# mixed case, a bilingual Carinthian village's two on two lines (Dellach / Dole), its Gemeinde under it, small, when it
# is another place of it (village_sub); the exit sign crossed by a red (#e30613) band from its lower left to its upper
# right.
AT_BLUE = (0, 159, 227)
AT_RED = (227, 6, 19)
AT_FIELD = (52.0, 60.5, 1446.1, 937.4)      # the white field, of the 1500 x 1000 drawing
AT_BAND = ((976.9, 440.5), (1444.8, 148.3), (1444.9, 62.3), (1317.5, 62.3), (494.7, 575.5), (54.1, 850.6),
           (54.1, 936.5), (181.7, 936.5))


def _face_at(name, group, local="", sub="", **_):
    """Austria's 17a (G_ENTRY) / 17b (G_EXIT), above. local is not used (a bilingual name has both)."""
    from PIL import Image, ImageDraw
    w, h = round(plate_w("AT") * FACE_PX_PER_M), round(PLATE_H * FACE_PX_PER_M)
    sx, sy = w / 1500.0, h / 1000.0
    img = Image.new("RGB", (w, h), AT_BLUE)
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = AT_FIELD
    d.rectangle([x0 * sx, y0 * sy, x1 * sx, y1 * sy], fill=FACE_WHITE)
    if group == G_EXIT:
        d.polygon([(x * sx, y * sy) for x, y in AT_BAND], fill=AT_RED)
    names = [n.strip() for n in name.split("/") if n.strip()] or [name]
    room = (x1 - x0 - 120) * sx
    big = round((330 if len(names) == 1 and not sub else 250) * sy)

    def fit(text, size):
        letters, marks = mark_text(text)
        while True:
            f = _font(size)
            bx = _marks_box(d, letters, marks, f, size)
            if bx[2] - bx[0] <= room or size <= 8:
                return f, size, bx, letters, marks
            size -= 2
    size = min(fit(n, big)[1] for n in names)       # (a bilingual name's two at one size)
    items = [fit(n, size) for n in names] + ([fit(sub, round(size * 0.5))] if sub else [])
    gaps = [0.0] + [0.15 * size] * (len(names) - 1) + ([0.3 * size] if sub else [])
    total = sum(it[2][3] - it[2][1] for it in items) + sum(gaps)
    y = (h - total) / 2
    for (f, fs, bx, letters, marks), gap in zip(items, gaps):
        y += gap
        at = ((w - (bx[2] - bx[0])) / 2 - bx[0], y - bx[1])
        d.text(at, letters, font=f, fill=(0, 0, 0), stroke_width=max(1, fs // 40), stroke_fill=(0, 0, 0))
        _draw_marks(d, at[0], at[1], letters, marks, f, fs, (0, 0, 0), FACE_WHITE)
        y += bx[3] - bx[1]
    return img


# The Netherlands' H1 / H2 (RVV 1990, bijlage 1; the drawings: Wikimedia Commons "Nederlands verkeersbord H1", "H2",
# 341 x 100): blue (#0e518d), a white line inside its edge, rounded, the woonplaats' name in white mixed case, a Frisian
# name on a second line (Sneek / Snits), its gemeente under them, small, when it has another name ("gem. Meerssen");
# the exit sign crossed by a red (#c1121c) bar from its lower left to its upper right.
NL_BLUE = (14, 81, 141)
NL_RED = (193, 18, 28)


def _face_nl(name, group, local="", sub="", **_):
    """The Dutch H1 (G_ENTRY) / H2 (G_EXIT), above."""
    from PIL import Image, ImageDraw
    w, h = round(plate_w("NL") * FACE_PX_PER_M), round(PLATE_H * FACE_PX_PER_M)
    s = h / 100.0
    img = Image.new("RGB", (w, h), NL_BLUE)
    d = ImageDraw.Draw(img)
    e = 6.5 * s
    d.rounded_rectangle([e, e, w - 1 - e, h - 1 - e], radius=round(9 * s), outline=FACE_WHITE,
                        width=max(2, round(5.9 * s)))
    if group == G_EXIT:
        d.polygon([(e, h - e - 12 * s), (w - e - 20 * s, e), (w - e, e), (w - e, e + 12 * s), (e + 20 * s, h - e),
                   (e, h - e)], fill=NL_RED)
    room = w - 2 * (e + 14 * s)

    def fit(text, size):
        letters, marks = mark_text(text)
        while True:
            f = _font(size)
            bx = _marks_box(d, letters, marks, f, size)
            if bx[2] - bx[0] <= room or size <= 8:
                return f, size, bx, letters, marks
            size -= 2
    names = [name] + ([local] if local else [])
    size = min(fit(n, round((56 if len(names) == 1 and not sub else 40) * s))[1] for n in names)  # (both at one size)
    while True:                                     # (and all of the lines inside the white line)
        items = [fit(n, size) for n in names] + ([fit(sub, round(size * 0.45))] if sub else [])
        gaps = [0.0] + [0.2 * size] * (len(names) - 1) + ([0.25 * size] if sub else [])
        total = sum(it[2][3] - it[2][1] for it in items) + sum(gaps)
        if total <= h - 2 * (e + 12 * s) or size <= 8:
            break
        size -= 2
    y = (h - total) / 2
    for (f, fs, bx, letters, marks), gap in zip(items, gaps):
        y += gap
        at = ((w - (bx[2] - bx[0])) / 2 - bx[0], y - bx[1])
        d.text(at, letters, font=f, fill=FACE_WHITE, stroke_width=max(1, fs // 40), stroke_fill=FACE_WHITE)
        _draw_marks(d, at[0], at[1], letters, marks, f, fs, FACE_WHITE, NL_BLUE)
        y += bx[3] - bx[1]
    return img


# Spain's S-500 / S-510 (Norma 8.1-IC, 2003; the drawings: Wikimedia Commons "Spain traffic signal S-500 (2003) Reus",
# "S-510 (2003)"): white, a red rounded border, the núcleo's name in black capitals, a bilingual one's two on two lines
# (DONOSTIA / SAN SEBASTIÁN); the exit sign crossed by a red bar from its lower left to its upper right. (The 2025
# pictogram S-500 is on few roads yet.)
ES_RED = (226, 0, 26)


def _face_es(name, group, local="", **_):
    """Spain's S-500 (G_ENTRY) / S-510 (G_EXIT), above."""
    from PIL import Image, ImageDraw
    w, h = round(plate_w("ES") * FACE_PX_PER_M), round(PLATE_H * FACE_PX_PER_M)
    img = Image.new("RGB", (w, h), FACE_WHITE)
    d = ImageDraw.Draw(img)
    m, b = round(0.02 * h), round(0.07 * h)
    d.rounded_rectangle([m, m, w - 1 - m, h - 1 - m], radius=round(0.2 * h), outline=ES_RED, width=b)
    if group == G_EXIT:
        d.line([(m + b, h - 1 - m - b), (w - 1 - m - b, m + b)], fill=ES_RED, width=round(0.11 * h))
    names = [mark_text(n.strip().upper()) for n in name.split("/") if n.strip()] or [mark_text(name.upper())]
    room_w, room_h = w - 2 * (m + b + 0.12 * h), h - 2 * (m + b + 0.08 * h)
    size = round(h * (0.42 if len(names) == 1 else 0.3))
    while size > 8:
        font = _font(size)
        boxes = [_marks_box(d, t, mk, font, size) for t, mk in names]
        gap = 0.2 * size
        tw = max(bx[2] - bx[0] for bx in boxes)
        th = sum(bx[3] - bx[1] for bx in boxes) + gap * (len(boxes) - 1)
        if tw <= room_w and th <= room_h:
            break
        size -= 2
    y = (h - th) / 2
    for (t, mk), bx in zip(names, boxes):
        at = ((w - (bx[2] - bx[0])) / 2 - bx[0], y - bx[1])
        d.text(at, t, font=font, fill=(0, 0, 0), stroke_width=max(1, size // 30), stroke_fill=(0, 0, 0))
        _draw_marks(d, at[0], at[1], t, mk, font, size, (0, 0, 0), FACE_WHITE)
        y += bx[3] - bx[1] + gap
    return img


# Portugal's N1a / N2a (Regulamento de Sinalização do Trânsito; the drawings: Wikimedia Commons "Portugal road sign
# N1a", "N2a", 777 x 294): white, a black rounded border, the localidade's name in black mixed case; the exit sign
# crossed by a red bar from its lower left to its upper right.
def _face_pt(name, group, local="", **_):
    """Portugal's N1a (G_ENTRY) / N2a (G_EXIT), above."""
    from PIL import Image, ImageDraw
    w, h = round(plate_w("PT") * FACE_PX_PER_M), round(PLATE_H * FACE_PX_PER_M)
    img = Image.new("RGB", (w, h), FACE_WHITE)
    d = ImageDraw.Draw(img)
    m, b = round(0.025 * h), round(0.045 * h)
    d.rounded_rectangle([m, m, w - 1 - m, h - 1 - m], radius=round(0.1 * h), outline=(0, 0, 0), width=b)
    if group == G_EXIT:
        d.line([(m + b, h - 1 - m - b), (w - 1 - m - b, m + b)], fill=FACE_RED, width=round(0.1 * h))
    lines = [name]
    if len(name) > 16 and " " in name:          # (a long name on two lines, at the space nearest its middle)
        i = min((k for k, ch in enumerate(name) if ch == " "), key=lambda k: abs(k - len(name) / 2))
        lines = [name[:i], name[i + 1:]]
    lines = [mark_text(t) for t in lines]
    room_w, room_h = w - 2 * (m + b + 0.12 * h), h - 2 * (m + b + 0.12 * h)
    size = round((0.5 if len(lines) == 1 else 0.32) * h)
    while size > 8:
        font = _font(size)
        boxes = [_marks_box(d, t, mk, font, size) for t, mk in lines]
        gap = 0.15 * size
        th = sum(bx[3] - bx[1] for bx in boxes) + gap * (len(boxes) - 1)
        if max(bx[2] - bx[0] for bx in boxes) <= room_w and th <= room_h:
            break
        size -= 2
    y = (h - th) / 2
    for (t, mk), bx in zip(lines, boxes):
        at = ((w - (bx[2] - bx[0])) / 2 - bx[0], y - bx[1])
        d.text(at, t, font=font, fill=(0, 0, 0), stroke_width=max(1, size // 45), stroke_fill=(0, 0, 0))
        _draw_marks(d, at[0], at[1], t, mk, font, size, (0, 0, 0), FACE_WHITE)
        y += bx[3] - bx[1] + gap
    return img


# Ireland's town and village sign (Traffic Signs Manual, chapter 4; the drawing: Wikimedia Commons "IE Town or Village
# Sign (green)", Cill Airne over KILLARNEY): green on a national road (main: MAIN_SIGNS), white with black letters on
# the others, a white edge; the Irish name (local) above in mixed case, leaning, the English one in capitals
# under it. One-sided: its back a blank plate.
IE_GREEN = (0, 132, 74)


def _face_ie(name, group, local="", main=False, **_):
    """Ireland's town or village sign (G_ENTRY) and its blank back (G_EXIT), above."""
    from PIL import Image, ImageDraw
    w, h = round(plate_w("IE") * FACE_PX_PER_M), round(PLATE_H * FACE_PX_PER_M)
    paper, ink = (IE_GREEN, FACE_WHITE) if main else (FACE_WHITE, (0, 0, 0))
    img = Image.new("RGB", (w, h), FACE_WHITE)
    d = ImageDraw.Draw(img)
    m, b = round(0.02 * h), round(0.035 * h)
    d.rounded_rectangle([m, m, w - 1 - m, h - 1 - m], radius=round(0.08 * h), fill=(0, 0, 0) if not main else
                        FACE_WHITE)
    d.rounded_rectangle([m + b, m + b, w - 1 - m - b, h - 1 - m - b], radius=round(0.06 * h), fill=paper)
    if group == G_EXIT:
        return img
    room = w - 2 * (m + b + 0.08 * h)

    def fit(text, size, frac_h):
        t, mk = mark_text(text)
        while size > 8:
            font = _font(size, t)
            bx = _marks_box(d, t, mk, font, size)
            if bx[2] - bx[0] <= room and bx[3] - bx[1] <= frac_h * h:
                return t, mk, font, size, bx
            size -= 2
        return t, mk, font, size, bx
    english = fit(name.upper(), round(0.4 * h if not local else 0.3 * h),
                  0.75 if not local else 0.36)
    if local:                           # (the Irish line leant as Luxembourg's second name: _italic)
        size = fit(local, round(0.3 * h), 0.34)[3]
        it, ib = _italic(local, _font(size, local), ink, size)
        gap = round(0.07 * h)
        y = round((h - (ib[3] - ib[1] + gap + english[4][3] - english[4][1])) / 2)     # (the two lines centred)
        img.paste(it, (round((w - (ib[2] - ib[0])) / 2 - ib[0]), y - ib[1]), it)
        top = y + ib[3] - ib[1] + gap
    else:
        top = (h - (english[4][3] - english[4][1])) / 2
    t, mk, font, size, bx = english
    at = ((w - (bx[2] - bx[0])) / 2 - bx[0], top - bx[1])
    d.text(at, t, font=font, fill=ink, stroke_width=max(1, size // 30), stroke_fill=ink)
    _draw_marks(d, at[0], at[1], t, mk, font, size, ink, paper)
    return img


# The signs of the countries whose sign is a plate, a border and the name or a village's silhouette, the exit one
# crossed by a red bar from its lower left to its upper right (the drawings: Wikimedia Commons' "Comparison of
# European road signs: Built-up area limits"): country -> Plain. paper, ink, border: colours; border_frac: the
# border's width, a share of the plate's height; radius_frac: its corners'; upper: the name in capitals; layout: "name"
# (the name alone, its lines at " / " or "/"), "name_silhouette" (the name above a silhouette, Denmark), "silhouette"
# (no name: Sweden, Finland), "name_panel" (a silhouette under a name panel of its own colours, name_paper and
# name_ink: Poland's D-42 under its E-17a on one post), "bands" (the name on the plate's upper half, its second name,
# local, on a lower band of name_paper and name_ink: Greece's P-17, Θεσσαλονίκη over Thessaloniki), "silhouette_name"
# (the silhouette above the name: Iceland's D12.11), "silhouette_band" (the silhouette on the plate's upper part, the
# name on a lower band of name_paper: Moldova's 5.63, ORHEI under it). A second name (local: a Cyrillic name's Latin
# form, LATIN_SECOND)
# is the name's last line. exit_bar: False for a country whose sign marks no limit, its back the same name sign
# (Norway's 727.1 stedsnavn: no built-up area sign, the speed limit's own). local_frac: the share of the plate's
# height a "name" layout's second name takes, smaller than the name (Ukraine's 5.49: Одеса over Odesa); None: one size.
# local_first: the second name the first line (Wales': Llanymddyfri over Llandovery). exit_blank: a country whose sign
# has no exit side, its back a blank plate (Great Britain's, Ireland's, Malta's, Cyprus')
Plain = namedtuple("Plain", "paper ink border border_frac radius_frac upper layout name_paper name_ink exit_bar "
                   "local_frac local_first exit_blank", defaults=(False, "name", None, None, True, None, False, False))
BLACK, YELLOW = (0, 0, 0), (255, 221, 0)
NA_GREEN = (0, 107, 63)             # (MUTCD's guide sign green)
PLAINS = {
    "CZ": Plain(FACE_WHITE, BLACK, BLACK, 0.05, 0.08, True),                 # IS12a / IS12b
    "SK": Plain(FACE_WHITE, BLACK, BLACK, 0.03, 0.04),                       # 301 / 302
    "HU": Plain(FACE_WHITE, BLACK, FACE_RED, 0.06, 0.05),                    # E-020 / E-021
    "SI": Plain(YELLOW, BLACK, BLACK, 0.05, 0.08),                           # III-14 / III-15
    "DK": Plain(FACE_WHITE, BLACK, BLACK, 0.035, 0.06, layout="name_silhouette"),    # E55 / E56
    "SE": Plain(FACE_WHITE, BLACK, BLACK, 0.035, 0.05, layout="silhouette"),         # E5 / E6
    "FI": Plain(YELLOW, BLACK, BLACK, 0.035, 0.05, layout="silhouette"),             # E22 / E23
    "PL": Plain(FACE_WHITE, BLACK, BLACK, 0.03, 0.05, layout="name_panel",          # D-42 / D-43 under E-17a
                name_paper=(0, 110, 60), name_ink=FACE_WHITE),
    "EE": Plain(FACE_WHITE, BLACK, BLACK, 0.035, 0.06, layout="name_silhouette"),    # 571a / 572a
    "LV": Plain(FACE_WHITE, BLACK, BLACK, 0.03, 0.05, True),                 # 555 / 556
    "LT": Plain(FACE_WHITE, BLACK, BLACK, 0.03, 0.05, True),                 # 550 / 551
    "RO": Plain(FACE_WHITE, BLACK, BLACK, 0.03, 0.05),                       # F46 / F48
    "BG": Plain(FACE_WHITE, BLACK, BLACK, 0.035, 0.05),                      # Д11 / Д12
    "MK": Plain(YELLOW, BLACK, BLACK, 0.04, 0.06),                           # 360 / 361
    "RS": Plain(CH_BLUE, FACE_WHITE, FACE_WHITE, 0.035, 0.05),               # III-23 / III-24
    "GR": Plain(FACE_WHITE, BLACK, CH_BLUE, 0.03, 0.05, layout="bands",      # P-17 / P-18
                name_paper=CH_BLUE, name_ink=FACE_WHITE),
    "HR": Plain(YELLOW, BLACK, BLACK, 0.04, 0.06),                           # naselje / its end (Čara, Commons)
    "BA": Plain(YELLOW, BLACK, BLACK, 0.04, 0.06),                           # naselje / its end, as Croatia's
    "NO": Plain(CH_BLUE, FACE_WHITE, FACE_WHITE, 0.035, 0.05, exit_bar=False),       # 727.1
    "IS": Plain(YELLOW, BLACK, BLACK, 0.035, 0.05, layout="silhouette_name"),        # D12.11 / D12.21
    "ME": Plain(YELLOW, BLACK, BLACK, 0.04, 0.06),                           # naselje (Blizikuće, Commons)
    "AL": Plain(FACE_WHITE, BLACK, BLACK, 0.02, 0.06, True),                 # town sign (Kukës, Commons)
    "XK": Plain(FACE_WHITE, BLACK, BLACK, 0.03, 0.06),                       # (Softaj, Commons)
    "MD": Plain(YELLOW, BLACK, BLACK, 0.025, 0.05, True, layout="silhouette_band",   # 5.63 / 5.64
                name_paper=FACE_WHITE, name_ink=BLACK),
    "UA": Plain(FACE_WHITE, BLACK, BLACK, 0.03, 0.08, local_frac=0.34),       # 5.49 / 5.50 (2021)
    "BY": Plain(FACE_WHITE, BLACK, BLACK, 0.03, 0.05, True, layout="name_silhouette"),   # (Baranavičy, Commons)
    "GB": Plain(FACE_WHITE, BLACK, BLACK, 0.03, 0.04, local_first=True, exit_blank=True),   # place name sign
    "MT": Plain(FACE_WHITE, BLACK, BLACK, 0.025, 0.06, True, local_frac=0.3, exit_blank=True),  # council's: MERĦBA
    "CY": Plain(FACE_WHITE, BLACK, BLACK, 0.03, 0.04, True, local_frac=0.4, exit_blank=True),   # (Kalo Chorio)
    # MUTCD's I2-1 (the name over CITY LIMIT: FACES), Canada's community signs (Quebec's green, Amos; Ontario's blue,
    # Forthton: CA_BLUE) and Mexico's: white on green, a white edge; one-sided
    "US": Plain(NA_GREEN, FACE_WHITE, FACE_WHITE, 0.025, 0.07, local_frac=0.3, exit_blank=True),
    "CA": Plain(NA_GREEN, FACE_WHITE, FACE_WHITE, 0.025, 0.07, True, exit_blank=True),
    "MX": Plain(NA_GREEN, FACE_WHITE, FACE_WHITE, 0.025, 0.05, True, exit_blank=True),
    # Cuba's: navy, the name in white capitals, the exit's crossed (Jicotea, Commons); Haiti's (no standard found): a
    # white plate
    "CU": Plain((24, 40, 84), FACE_WHITE, FACE_WHITE, 0.03, 0.04, True),
    "HT": Plain(FACE_WHITE, BLACK, BLACK, 0.03, 0.05, True, exit_blank=True),
}
# the countries whose road manuals follow the MUTCD's guide signs (Central America's SIECA manual, the Dominican
# Republic, Puerto Rico, the English-speaking Caribbean): Mexico's green sign (no standard of their own found for a
# town's: the guide sign's)
GUIDE_GREEN = {"GT", "BZ", "SV", "HN", "NI", "CR", "PA", "DO", "PR", "JM", "BS", "TT", "BB", "LC", "VC", "GD", "DM",
               "AG", "KN"}
PLAINS.update({cc: PLAINS["MX"] for cc in GUIDE_GREEN})
# South America's: the name in white on green, in mixed case (Chile's, San Pedro, Commons; Brazil's identificação
# de município, the manual's green, not its municipal limits' blue); the others' (no standard found) the same
PLAINS.update({cc: PLAINS["MX"]._replace(upper=False)
               for cc in ("AR", "BO", "PY", "UY", "BR", "CL", "CO", "PE", "EC", "VE", "GY", "SR")})
# Africa's: the Maghreb's white plate, a line per script, Arabic first, Tifinagh, then Latin (Boutheldja's, Algeria; Ain
# Glal's, Tunisia, Commons), Ethiopia's and Eritrea's the same with Ge'ez; Egypt's and Sudan's on blue (their guide
# signs'); the English-speaking countries' Great Britain's place name sign, the French-speaking ones' France's EB10,
# the Portuguese-speaking ones' Portugal's, Equatorial Guinea's Spain's (FACES; no standard of their own found)
PLAINS.update({cc: Plain(FACE_WHITE, BLACK, BLACK, 0.03, 0.05, True, layout="scripts")
               for cc in ("MA", "DZ", "TN", "LY", "MR", "ET", "ER")})
PLAINS.update({cc: Plain(CH_BLUE, FACE_WHITE, FACE_WHITE, 0.03, 0.05, layout="scripts") for cc in ("EG", "SD")})
AFRICA_ENGLISH = ("KE", "UG", "TZ", "NG", "GH", "SL", "LR", "GM", "ZA", "NA", "BW", "ZW", "ZM", "MW", "LS", "SZ", "SO",
                  "SS", "MU", "SC")
AFRICA_FRENCH = ("SN", "ML", "BF", "NE", "CI", "GN", "BJ", "TG", "CM", "GA", "CG", "CD", "CF", "TD", "MG", "DJ", "KM",
                 "BI", "RW", "RE", "YT")
AFRICA_PORTUGUESE = ("AO", "MZ", "GW", "CV", "ST")
PLAINS.update({cc: PLAINS["GB"] for cc in AFRICA_ENGLISH})
# the Middle East's: Turkey's B-9 / B-10 (blue, the name in white capitals, crossed on the way out); the Arab
# countries', Israel's and Palestine's names over their English one on green (their guide signs'), Iran's on blue;
# the Caucasus' a white plate crossed on the way out (no standard of their own found)
PLAINS.update({"TR": Plain((0, 70, 140), FACE_WHITE, FACE_WHITE, 0.035, 0.08, True),
               "IR": Plain(CH_BLUE, FACE_WHITE, FACE_WHITE, 0.03, 0.05, layout="scripts"),
               **{cc: Plain(NA_GREEN, FACE_WHITE, FACE_WHITE, 0.03, 0.05, layout="scripts")
                  for cc in ("SY", "LB", "JO", "IQ", "SA", "KW", "BH", "QA", "AE",
                             "OM", "YE", "XW", "IL")},
               **{cc: Plain(FACE_WHITE, BLACK, BLACK, 0.03, 0.05, True, layout="scripts")
                  for cc in ("GE", "AM", "AZ")}})
# Asia's and Oceania's: the former Soviet countries' white plate crossed on the way out (Russia's 5.23.1 / 5.24.1);
# the others' guide signs' colours, green (South Asia's, Southeast Asia's, Australia's, the Pacific's) or blue
# (China's, Japan's, Korea's, Thailand's, Laos', Vietnam's, Malaysia's), the name over its English form; New
# Caledonia's and French Polynesia's France's EB10 (FACES; no standard of their own found but Russia's)
PLAINS.update({**{cc: Plain(FACE_WHITE, BLACK, BLACK, 0.03, 0.05, True, layout="scripts")
                  for cc in ("RU", "KZ", "UZ", "TM", "KG", "TJ", "MN")},
               **{cc: Plain(NA_GREEN, FACE_WHITE, FACE_WHITE, 0.03, 0.05, layout="scripts")
                  for cc in ("AF", "PK", "IN", "NP", "BD", "LK", "BT", "MV", "MM", "KH", "ID", "PH", "TL", "BN", "HK",
                             "TW", "AU", "NZ", "PG", "FJ", "SB", "VU", "WS", "TO", "GU")},
               **{cc: Plain(CH_BLUE, FACE_WHITE, FACE_WHITE, 0.03, 0.05, layout="scripts")
                  for cc in ("CN", "JP", "KR", "KP", "TH", "LA", "VN", "MY")}})
CA_BLUE = PLAINS["CA"]._replace(paper=CH_BLUE)


def sign_upper(text):
    """text in capitals as a sign writes them: a Greek name without its accents (ΚΑΛΟ ΧΩΡΙΟ; Κάλο Χωριό's tonos is
    for lower case only), a Latin or Cyrillic one with them (PLZEŇ, KĖDAINIAI), a Georgian one as it is (its capitals,
    Mtavruli, are on no sign: გორი, not ᲒᲝᲠᲘ). Pure."""
    import unicodedata
    if re.search(r"[Ⴀ-ჿ]", text):
        return text
    text = text.upper()
    if not re.search(r"[Ͱ-Ͽ]", text):
        return text
    return unicodedata.normalize("NFC", "".join(c for c in unicodedata.normalize("NFD", text)
                                                if not unicodedata.combining(c)))


# a script's letters and the rank of its line on a sign that writes a line per script (split_scripts)
SCRIPT_ORDER = ((re.compile(r"[֐-׿]"), -1), (re.compile(r"[؀-ۿ]"), 0), (re.compile(r"[ሀ-᎟]"), 1),
                (re.compile(r"[ⴰ-⵿]"), 2))


def split_scripts(name):
    """A name of words in several scripts (Morocco's and Algeria's: "Meknès ⴰⵎⴽⵏⴰⵙ مكناس") as one line per script:
    Arabic, Ge'ez, Tifinagh, then the Latin words, each line's words in their order. Pure."""
    runs = {}
    for word in name.split():
        rank = next((r for script, r in SCRIPT_ORDER if script.search(word)), 3)
        runs.setdefault(rank, []).append(word)
    return [" ".join(runs[r]) for r in sorted(runs)] or [name]


def _face_plain(name, group, local="", country="", style=None, **_):
    """A PLAINS country's sign (G_ENTRY) and its exit sign (G_EXIT), above (style: another Plain than the country's)."""
    from PIL import Image, ImageDraw
    st = style or PLAINS[country]
    w, h = round(plate_w(country) * FACE_PX_PER_M), round(PLATE_H * FACE_PX_PER_M)
    img = Image.new("RGB", (w, h), FACE_WHITE)
    d = ImageDraw.Draw(img)
    m, b = round(MARGIN_FRAC * h), max(2, round(st.border_frac * h))
    r = round(st.radius_frac * h)

    def plate(y0, y1, paper, border):
        d.rounded_rectangle([m, y0, w - 1 - m, y1], radius=r, fill=border)
        d.rounded_rectangle([m + b, y0 + b, w - 1 - m - b, y1 - b], radius=max(1, r - b), fill=paper)

    def text(lines, x0, y0, x1, y1, ink, paper, size):
        """lines centred in the box, one size, as large as size and the box allow."""
        lines = [mark_text(t) for t in lines]
        while size > 8:
            fonts = [_font(size, t) for t, _ in lines]          # (each line's: a Cyrillic one the system's)
            boxes = [_marks_box(d, t, mk, f, size) for (t, mk), f in zip(lines, fonts)]
            gap = 0.15 * size
            th = sum(bx[3] - bx[1] for bx in boxes) + gap * (len(boxes) - 1)
            if max(bx[2] - bx[0] for bx in boxes) <= x1 - x0 and th <= y1 - y0:
                break
            size -= 2
        y = (y0 + y1 - th) / 2
        for (t, mk), f, bx in zip(lines, fonts, boxes):
            at = ((x0 + x1 - (bx[2] - bx[0])) / 2 - bx[0], y - bx[1])
            d.text(at, t, font=f, fill=ink, stroke_width=max(1, size // 40), stroke_fill=ink)
            _draw_marks(d, at[0], at[1], t, mk, f, size, ink, paper)
            y += bx[3] - bx[1] + gap
    lines = [n.strip() for n in BILINGUAL.split(name) if n.strip()] or [name]
    if st.layout == "scripts":              # (a line per script: Meknès ⴰⵎⴽⵏⴰⵙ مكناس)
        lines = split_scripts(name)
    if len(lines) == 1 and len(name) > 14 and " " in name:     # (a long name on two lines: Šmarje / pri Jelšah)
        i = min((k for k, ch in enumerate(name) if ch == " "), key=lambda k: abs(k - len(name) / 2))
        lines = [name[:i], name[i + 1:]]
    if local and st.layout != "bands" and not st.local_frac:
        lines = [local] + lines if st.local_first else lines + [local]
    lines = [t for t in lines if shaped_ok(t)] or [local or name]     # (a script it cannot draw: the Latin form only)
    if st.upper:
        lines = [sign_upper(t) for t in lines]
    pad = m + b + 0.07 * h
    top, bottom = m, h - 1 - m
    if st.layout == "name_panel":           # (the name panel above, the sign under it)
        split = round(0.36 * h)
        plate(m, split - 2, st.name_paper, FACE_WHITE)
        d.rounded_rectangle([m, m, w - 1 - m, split - 2], radius=r, outline=st.name_paper, width=1)
        text(lines, pad, m + b + 0.03 * h, w - pad, split - 2 - b - 0.03 * h, st.name_ink, st.name_paper,
             round(0.2 * h))
        top = split + 2
    plate(top, bottom, st.paper, st.border)
    if group == G_EXIT and st.exit_blank:   # (a one-sided sign's back)
        return img
    inner =(m + b + 0.05 * h, top + b + 0.05 * h, w - 1 - m - b - 0.05 * h, bottom - b - 0.05 * h)
    if st.layout in ("name", "scripts") and local and st.local_frac:     # (the second name smaller, under the name)
        mid = bottom - b - 0.06 * h - st.local_frac * (bottom - top)
        text(lines, pad, top + b + 0.06 * h, w - pad, mid, st.ink, st.paper,
             round((0.4 if len(lines) == 1 else 0.26) * h))
        text([local], pad, mid + 0.02 * h, w - pad, bottom - b - 0.06 * h, st.ink, st.paper, round(0.24 * h))
    elif st.layout in ("name", "scripts"):
        text(lines, pad, top + b + 0.08 * h, w - pad, bottom - b - 0.08 * h, st.ink, st.paper,
             round((0.42 if len(lines) == 1 else 0.3) * h))
    elif st.layout == "bands":              # (the name above, its Latin form on the band below)
        mid = round((top + bottom) / 2)
        d.rectangle([m + b, mid, w - 1 - m - b, bottom - b], fill=st.name_paper)
        text(lines, pad, top + b + 0.05 * h, w - pad, mid - 0.04 * h, st.ink, st.paper, round(0.3 * h))
        if local:
            text([local], pad, mid + 0.04 * h, w - pad, bottom - b - 0.05 * h, st.name_ink, st.name_paper,
                 round(0.3 * h))
    elif st.layout == "name_silhouette":
        mid = top + (bottom - top) * 0.45
        text(lines, pad, inner[1], w - pad, mid, st.ink, st.paper, round(0.3 * h))
        sw = min(inner[2] - inner[0], (inner[3] - mid) * 2.2)
        _silhouette(d, (w - sw) / 2, mid + 0.03 * h, (w + sw) / 2, inner[3], st.ink)
    elif st.layout == "silhouette_name":
        mid = top + (bottom - top) * 0.55
        sw = min(inner[2] - inner[0], (mid - inner[1]) * 2.2)
        _silhouette(d, (w - sw) / 2, inner[1] + 0.03 * h, (w + sw) / 2, mid - 0.03 * h, st.ink)
        text(lines, pad, mid, w - pad, inner[3], st.ink, st.paper, round(0.26 * h))
    elif st.layout == "silhouette_band":     # (the silhouette above, the name on the band below)
        mid = round(top + (bottom - top) * 0.5)
        d.rectangle([m + b, mid, w - 1 - m - b, bottom - b], fill=st.name_paper)
        d.line([(m + b, mid), (w - 1 - m - b, mid)], fill=st.border, width=max(1, b // 2))
        sw = min(inner[2] - inner[0], (mid - inner[1]) * 2.6)
        _silhouette(d, (w - sw) / 2, inner[1] + 0.04 * h, (w + sw) / 2, mid - 0.04 * h, st.ink)
        text(lines, pad, mid + 0.06 * h, w - pad, bottom - b - 0.06 * h, st.name_ink, st.name_paper, round(0.3 * h))
    else:                                   # (the silhouette alone)
        sw = min(inner[2] - inner[0], (inner[3] - inner[1]) * 2.4)
        _silhouette(d, (w - sw) / 2, inner[1] + 0.05 * h, (w + sw) / 2, inner[3], st.ink)
    if group == G_EXIT and st.exit_bar:
        d.line([(m + b, bottom - b), (w - 1 - m - b, top + b)], fill=FACE_RED, width=round(0.09 * h))
        if st.layout == "name_panel":       # (the name panel's own: E-18a)
            d.line([(m + b, split - 2 - b), (w - 1 - m - b, m + b)], fill=FACE_RED, width=round(0.05 * h))
    return img


# country -> its sign's drawing (face_image): (name, group, local, nxt=, km=, sub=, main=) -> PIL image
FACES = {"FR": _face_fr, "BE": _face_be, "LU": _face_lu, "DE": _face_de, "CH": _face_ch, "IT": _face_it,
         "AT": _face_at, "NL": _face_nl, "ES": _face_es, "PT": _face_pt}
FACES.update({cc: (lambda *a, _cc=cc, **k: _face_plain(*a, country=_cc, **k)) for cc in PLAINS})
# (Andorra's, Liechtenstein's and San Marino's signs are their neighbours': Spain's S-500, the Swiss Ortstafel, Italy's)
FACES.update({"AD": _face_es, "LI": _face_ch, "SM": _face_it})
# (Monaco's are France's EB10 / EB20; Malta's council signs say welcome under the name)
FACES.update({"IE": _face_ie, "MC": _face_fr,
              "GP": _face_fr, "MQ": _face_fr, "GF": _face_fr, "AW": _face_nl, "GQ": _face_es, "NC": _face_fr,
              "PF": _face_fr,
              **{cc: _face_fr for cc in AFRICA_FRENCH}, **{cc: _face_pt for cc in AFRICA_PORTUGUESE},
              "MT": lambda name, group, local="", **k: _face_plain(name, group, "MERĦBA", country="MT", **k),
              "US": lambda name, group, local="", **k: _face_plain(name, group, "CITY LIMIT", country="US", **k),
              "CA": lambda name, group, local="", sub="", **k: _face_plain(
                  name, group, local, country="CA", style=CA_BLUE if sub == "blue" else None, **k)})
FACES.update({cc: FACES.get(p, _face_fr) for cc, p in TERRITORIES.items()})


def write_face(out_dir, vfs_dir, name, group, local="", country="FR", nxt="", km=0, sub="", main=False):
    """A commune's sign face in out_dir (once: a face of the session is not written again): its texture (face_image,
    <face_group>.png) and its model (<face_group>.dae: the plate's front, a quad at FACE_Y textured with it, its
    material named 'bne_' + the face group). Returns the tile's entry for it {"shape", "texture", "material"} (vfs
    paths, the files' own: the revisit cache keeps the files a tile names, revisit.SHAPE_RE)."""
    import dae
    g = face_group(name, group, local, country, nxt, km, sub, main)
    png, model = os.path.join(out_dir, g + ".png"), os.path.join(out_dir, g + ".dae")
    material = "bne_" + g
    if not (os.path.exists(png) and os.path.exists(model)):
        import staging
        tmp = staging.tmp_for(png)
        face_image(name, group, local, country, nxt, km, sub, main).save(tmp, format="PNG")
        staging.replace(tmp, png)
        x, z0, z1 = plate_w(country) / 2, PLATE_Z0, PLATE_Z0 + PLATE_H
        dae.write_grid_dae(model, [-x, FACE_Y, z0, x, FACE_Y, z0, x, FACE_Y, z1, -x, FACE_Y, z1],
                           [0, 0, 1, 0, 1, 1, 0, 1], [0, 1, 2, 0, 2, 3], material=material, texture=g + ".png",
                           normals=[0, -1, 0] * 4)
    return {"shape": f"{vfs_dir}/{g}.dae", "texture": f"{vfs_dir}/{g}.png", "material": material}


# Street furniture taken away around a sign so drivers see it (in-game 2026-10-28, Boisset-et-Gaujac: a row of street
# trees hid two signs out of three where the road has a sidewalk): furniture.place's street trees, benches, bins and
# hydrants within CLEAR_M of a sign. The lamps and poles stay (thin, and their wires span to the next).
CLEAR_GROUPS = {furniture.G_STREET_TREE, furniture.G_BENCH, furniture.G_BIN, furniture.G_HYDRANT}
CLEAR_M = 8.0


def clear_around(props, signs):
    """props (prop rows) without the CLEAR_GROUPS rows within CLEAR_M of a sign of signs (rows). Pure."""
    if not signs:
        return props
    at = np.array([(s[1], s[2]) for s in signs], np.float64)
    out = []
    for r in props:
        if r[0] in CLEAR_GROUPS and np.hypot(at[:, 0] - r[1], at[:, 1] - r[2]).min() < CLEAR_M:
            continue
        out.append(r)
    return out


AGGLO_CACHE = 256              # communes' agglomerations kept per process (Towns)
POINTS_THREADS = 4             # village points' map cells fetched at once (Towns._queue)
POINTS_WAIT_S = 45.0           # s a road tile waits for its village points before it is built without signs
LANDCOVER_REACH_M = 10000.0    # m: the farthest from a commune's middle its land cover is waited for (_wait_landcover)
# m: the farthest from a tile's cells a commune's village points are read (_points_of; 12 km queued dozens of cells
# of a Lithuanian savivaldybe's, the road tiles waited for them minutes, Kedainiai 2026-10-30)
POINTS_REACH_M = 4000.0


class Towns:
    """A process's town signs for its road tiles: the communes of a tile's map cells (mapdata layer "divisions") with
    their agglomerations, made once per commune from the land cover (surface: surface.Surface) and kept, and the
    faces written next to the tiles. Not thread-safe beyond the dict's own atomic steps (a commune computed twice
    by two threads gives the same result)."""

    def __init__(self, md, surface):
        self.md, self.surface = md, surface
        self._agglo = OrderedDict()             # commune id -> Agglo or None
        self._pending = set()                   # (map cell, layer) a background fetch reads (_queue)
        self._lock = threading.Lock()
        self._pool = None
        self._waited_in_vain = -1e9             # monotonic time of the last wait for points that ran out (_points)

    def _points(self, keys):
        """The "division_points" layer of the map cells keys, or None when one is not cached yet: its fetch is queued
        (surface.Surface's way with the land cover) and nothing built now is kept (revisit.fault), the tile built
        again once it is. A German tile read some forty cells of it from the network, minutes: the road tile's
        worker was killed at 300 s and the tile never came, 2026-10-29."""
        import revisit
        missing = [k for k in keys if not self.md.has(k, "division_points")]
        if missing:
            for k in missing:
                self._queue(k)
            # (a while for them: a road tile is not asked for again once sent, its signs came only when the player
            # came back, Bolzano 2026-10-29; the fetches run POINTS_THREADS at once)
            # (not again for a while after a wait in vain: a tile's every commune and exit sign asks, the road tile's
            # worker is killed at 300 s)
            now = time.monotonic()
            deadline = now + (POINTS_WAIT_S if now - self._waited_in_vain > POINTS_WAIT_S else 0.0)
            while time.monotonic() < deadline and not all(self.md.has(k, "division_points") for k in missing):
                time.sleep(0.2)
            if not all(self.md.has(k, "division_points") for k in missing):
                if deadline > now:
                    self._waited_in_vain = time.monotonic()
                revisit.fault()
                return None
        return [self.md.cell(k, layers=("division_points",), kind="background").layers.get("division_points")
                for k in keys]

    def _queue(self, key, layer="division_points"):
        """Fetch the layer of the map cell key in the background (once at a time)."""
        with self._lock:
            if (key, layer) in self._pending:
                return
            self._pending.add((key, layer))
            if self._pool is None:
                self._pool = ThreadPoolExecutor(POINTS_THREADS, thread_name_prefix="townsigns-points")

        def run():
            try:
                self.md.prefetch(key, (layer,))
            except Exception as e:      # (network trouble: asked again by the next tile)
                log.debug("town signs: %s of %s: %s: %s", layer, key, type(e).__name__, e)
            finally:
                with self._lock:
                    self._pending.discard((key, layer))
        self._pool.submit(run)

    def _urban_fn(self, done):
        import roads as roadsmod

        def urban(lat, lon):
            cls, complete = self.surface.classes(np.asarray(lat, np.float64), np.asarray(lon, np.float64))
            done[0] = done[0] and complete
            return cls == roadsmod.URBAN_CLASS
        return urban

    def communes(self, frame, keys):
        """[Commune] of the map cells keys with their agglomerations, or None when some land cover is not cached yet
        (the tile is then built without town signs and not kept: surface.classes marked it for a rebuild)."""
        feats = [self.md.cell(k, layers=("divisions",), kind="demand").layers.get("divisions") for k in keys]
        ready = [True]

        # (the cells' box: PICTOGRAMS' squares over it, the villages' points near it)
        bounds = np.array([k.bounds() for k in keys if hasattr(k, "bounds")], np.float64).reshape(-1, 4)
        box = (*bounds[:, :2].min(axis=0), *bounds[:, 2:].max(axis=0)) if len(bounds) else None

        def points_of(c):
            got = self._points_of(c, box)
            if got is None:
                ready[0] = False
                return []
            return got
        communes = split_villages(communes_of(feats, frame, box=box), points_of)
        if not ready[0]:                # (a village's points not cached yet: no signs now, the tile built again)
            return None
        done = [True]
        deadline = time.monotonic() + POINTS_WAIT_S             # (the tile's one wait for land cover)

        def agglo_of(c):
            if c.fid in self._agglo:
                self._agglo.move_to_end(c.fid)
                return self._agglo[c.fid]
            a = commune_agglomeration(c, self._urban_fn(done))
            if not done[0] and self._wait_landcover(c, deadline):    # (its land cover now here: again, complete)
                done[0] = True
                a = commune_agglomeration(c, self._urban_fn(done))
            if done[0]:
                self._agglo[c.fid] = a
                while len(self._agglo) > AGGLO_CACHE:
                    self._agglo.popitem(last=False)
            return a
        out = with_agglomerations(communes, agglo_of)
        return out if done[0] else None

    def _wait_landcover(self, c, deadline):
        """Fetch the land cover of the map cells over the commune c's box (and AGGLO_MARGIN_M; LANDCOVER_REACH_M at
        most from its point or middle) and wait for it until deadline (monotonic; the tile's one budget: each commune
        waiting POINTS_WAIT_S kept the road tiles 75 s and more, Trikala 2026-10-30), not again for a while after a
        wait in vain (as _points: a first visit's road tiles came before the land cover and kept no signs, the tile not
        asked for again, Michalovce 2026-10-30). True if it all arrived."""
        import mapcells
        now = time.monotonic()
        if now >= deadline or now - self._waited_in_vain <= POINTS_WAIT_S:
            return False
        pts = np.concatenate([r for rings in c.lonlat for r in rings[:1]])
        mid = c.sites[c.site] if c.sites is not None else (pts.min(axis=0) + pts.max(axis=0)) / 2
        rlat = min(LANDCOVER_REACH_M, AGGLO_MARGIN_M + VILLAGE_REACH_M) / M_PER_DEG
        rlon = rlat / max(math.cos(math.radians(float(mid[1]))), 0.01)
        m = AGGLO_MARGIN_M / M_PER_DEG
        lon0, lat0 = np.maximum(pts.min(axis=0) - m, (mid[0] - rlon, mid[1] - rlat))
        lon1, lat1 = np.minimum(pts.max(axis=0) + m, (mid[0] + rlon, mid[1] + rlat))
        try:
            keys = mapcells.cells_for_bbox(float(lat0), float(lon0), float(lat1), float(lon1))
        except ValueError:
            return False
        missing = [k for k in keys if not self.md.has(k, "landcover")]
        for k in missing:
            self._queue(k, "landcover")
        while time.monotonic() < deadline and not all(self.md.has(k, "landcover") for k in missing):
            time.sleep(0.2)
        if all(self.md.has(k, "landcover") for k in missing):
            return True
        self._waited_in_vain = time.monotonic()
        return False

    def _points_of(self, c, near=None):
        """The "division_points" layer of every map cell the commune c's box touches (split_villages' points_of), within
        POINTS_REACH_M of the box near (lon0, lat0, lon1, lat1: the tile's cells'; None: all of it), None when not all
        are cached yet (_points). (A Greek dimos' box is hundreds of map cells: the road tile waited for their points
        and its worker was killed at 300 s, Trikala 2026-10-30; the parts of the villages near the tile are the same
        with the nearby villages only.)"""
        import mapcells
        pts = np.concatenate([r for rings in c.lonlat for r in rings[:1]])
        (lon0, lat0), (lon1, lat1) = pts.min(axis=0), pts.max(axis=0)
        if near is not None:
            rlat = POINTS_REACH_M / M_PER_DEG
            rlon = rlat / max(math.cos(math.radians((near[1] + near[3]) / 2)), 0.01)
            lon0, lat0 = max(lon0, near[0] - rlon), max(lat0, near[1] - rlat)
            lon1, lat1 = min(lon1, near[2] + rlon), min(lat1, near[3] + rlat)
            if lon0 > lon1 or lat0 > lat1:
                return []
        try:
            keys = mapcells.cells_for_bbox(float(lat0), float(lon0), float(lat1), float(lon1))
        except ValueError:                  # (an area too large to read its points: it stays one, unsplit)
            return []
        return self._points(keys)

    def maker(self, frame, keys, out_dir, vfs_dir, faces):
        """roads.build_tile's towns: (roads, box, left_hand, rings) -> the tile's sign rows (prop rows of their faces'
        groups), each new face written and named in faces (the tile's "townsigns": write_tile's towns)."""
        def rows(roads, box, left_hand, rings=()):
            try:
                communes = self.communes(frame, keys)
            except Exception as e:      # (no signs rather than no tile)
                # (a bug, not the network's: in the log, Szentendre's tiles had none and said nothing, 2026-10-30)
                log.warning("town signs: communes: %s: %s", type(e).__name__, e, exc_info=True)
                return []
            if not communes:
                return []
            out = []
            for r in town_signs(roads, communes, frame, box, left_hand, rings):
                nxt, km = "", 0
                if r[0] == G_EXIT and r[10] in NEXT_PLACE:
                    try:
                        lat, lon, _ = frame.enu_to_geodetic(r[1], r[2], 0.0)
                        found = next_place(float(lat), float(lon), r[11], self._points_near(float(lat), float(lon),
                                                                                           r[11]), r[8])
                        if found:
                            nxt = found[0]
                            km = max(1, int(round(found[1] / 1000.0))) if r[10] in NEXT_KM else 0
                    except Exception as e:      # (the sign without it rather than no sign)
                        log.debug("town signs: next place: %s: %s", type(e).__name__, e)
                text = dict(local=r[9], country=r[10], nxt=nxt, km=km, sub=r[12],
                            main=r[13] and r[10] in MAIN_SIGNS)
                g = face_group(r[8], r[0], **text)
                if g not in faces:
                    faces[g] = write_face(out_dir, vfs_dir, r[8], r[0], **text)
                out.append([g] + r[1:8])
            return out
        return rows

    def _points_near(self, lat, lon, heading):
        """[(name, lon, lat)] of the village points (mapdata "division_points") of the map cells near the ray of
        NEXT_MAX_M from (lat, lon) along heading (next_place's points; the same cells for the same sign); [] when not
        all are cached yet (_points: the tile is built again)."""
        import mapcells
        c = math.cos(math.radians(lat))
        h = math.radians(heading)
        s = np.arange(500.0, NEXT_MAX_M + 1.0, 1000.0)
        la = lat + s * math.sin(h) / M_PER_DEG
        lo = lon + s * math.cos(h) / (M_PER_DEG * c)
        pad = 1500.0 / M_PER_DEG
        seen, out = set(), []
        for feats in self._points(mapcells.cells_near_points(la, lo, pad, pad / c)) or ():
            for i in range(len(feats) if feats is not None else 0):
                fid = str(feats.ids[i])
                if fid not in seen:
                    seen.add(fid)
                    p = np.asarray(feats.parts(i)[0][0], np.float64).reshape(-1, 2)[0]
                    out.append((" ".join(str(feats.attrs["name"][i]).split()), float(p[0]), float(p[1])))
        return out
