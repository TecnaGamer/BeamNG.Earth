"""Reality check: the generator's tree species at forest sites around the world against published facts (national
forest inventories, vegetation atlases). Each site: a forest-interior z14 cell, the share of its trees (the clumps
left out: far stand-ins; '_open' folded into its group) per group, and expectations with generous tolerances (the
stock shapes stand in for whole families, so only what a player would see as wrong is tested).

usage (from server/, online: fetches the cells' data): PYTHONPATH=. python ../tools/veg_reality_check.py CACHE [ONLY-prefix]
Each fact names its unit ([liczba drzew] tree count, [powierzchnia] area, [objętość] volume, [EU-Forest, 1/k] the
NFI plots' species weighted 1/k) and its source; [niezweryfikowane liczbowo] / [słabe]: general knowledge only."""
import json
import math
import os
import sys

import numpy as np

import dem as demmod
import mapdata
import netfetch
import vegetation as veg

CON = {"conifer", "pine_west", "juniper", "pine_eu", "spruce", "redwood", "pine_maritime", "snag_conifer"}
EU_TEMPERATE = {"birch", "oak", "beech", "pine_eu", "spruce"}
TEMPERATE_ANY = EU_TEMPERATE | {"deciduous", "conifer"}
TROPICAL = {"tropical", "palm", "savanna", "tro_thicket", "tro_understory", "laurel"}


def S(d, *names):
    return sum(d.get(n, 0.0) for n in names)


def con(d):
    return S(d, *CON)


def share_of_con(d, *names):
    c = con(d)
    return S(d, *names) / c if c > 0 else 0.0


# (name, lat, lon, source / fact, [(label, check(d) -> value, lo, hi)])
SITES = [
    # --- Europe
    ("Bory Tucholskie, Woziwoda", 53.665, 17.895, "[słabe] brak aktualnej liczby: RDLP Toruń, międzywojnie 92 % sosny (Pomorze); PAN: 60-70 % w badanym fragmencie; mapa Copernicus 0.99 iglastych",
     [("iglaste", con, .80, 1), ("sosna wśród iglastych", lambda d: share_of_con(d, "pine_eu"), .65, 1),
      ("buk", lambda d: S(d, "beech"), 0, .05)]),
    ("Puszcza Notecka", 52.75, 16.10, "[niezweryfikowane liczbowo] bór sosnowy; mapa Copernicus",
     [("iglaste", con, .80, 1), ("sosna wśród iglastych", lambda d: share_of_con(d, "pine_eu"), .65, 1)]),
    ("Bieszczady, Wetlina", 49.15, 22.48, "[liczba drzew] Roczniki Bieszczadzkie 16 (2008/9), cały las BdPN: buk 75 %, świerk 9 %, jawor 5 %, jodła 3 % https://archiwum.bdpn.pl/dokumenty/nauka/2009/roczniki16/art_06.pdf",
     [("buk", lambda d: S(d, "beech"), .60, .92), ("dąb", lambda d: S(d, "oak"), 0, .05)]),
    ("Tatry reglowe, Zakopane", 49.27, 19.93, "[niezweryfikowane liczbowo] regiel górny: świerk",
     [("iglaste", con, .55, 1), ("świerk wśród iglastych", lambda d: share_of_con(d, "spruce"), .55, 1)]),
    ("Schwarzwald", 48.00, 8.20, "[powierzchnia] Landtag BW 17/8683, Lkr. Freudenstadt: świerk 52,2 %, jodła 25,8 %, buk 7,6 % https://www.landtag-bw.de/resource/blob/568940/160f0380f4ef0512532c925dd6fa34e3/17_8683_D.pdf",
     [("iglaste", con, .70, 1), ("świerk/jodła wśród iglastych", lambda d: share_of_con(d, "spruce"), .60, 1)]),
    ("Finlandia, Jyväskylä", 62.00, 26.00, "[objętość] Luke VMI12/13: sosna 1283, świerk 763, brzoza 420 mln m3 (50/30/16 %) https://statdb.luke.fi/PxWeb/pxweb/en/LUKE/",
     [("iglaste", con, .72, .93), ("brzoza", lambda d: S(d, "birch"), .06, .28),
      ("dąb + buk", lambda d: S(d, "oak", "beech"), 0, .03)]),
    ("Szwecja, Norrbotten", 65.80, 21.00, "[niezweryfikowane liczbowo] tajga",
     [("iglaste", con, .65, 1), ("dąb + buk", lambda d: S(d, "oak", "beech"), 0, .02)]),
    ("Landy, Mimizan", 44.15, -1.10, "[powierzchnia] DRAAF Nouvelle-Aquitaine: ~1 mln ha zalesione, 92 % sosna nadmorska https://draaf.nouvelle-aquitaine.agriculture.gouv.fr/IMG/pdf/07-La_filiere_bois-Correctif_cle8c344d.pdf",
     [("sosna nadmorska", lambda d: S(d, "pine_maritime"), .75, 1)]),
    ("Galicja, Santiago", 42.85, -8.60, "[powierzchnia] IFN4 Galicia: drzewostany eukaliptusowe 18 % pow. zadrzewionej (czyste), plus mieszane z sosną https://www.miteco.gob.es/content/dam/miteco/es/biodiversidad/temas/inventarios-nacionales/galicia_tcm30-531786.pdf",
     [("eukaliptus", lambda d: S(d, "eucalyptus"), .12, .50), ("oliwki itp.", lambda d: S(d, "mediterranean"), 0, .10)]),
    ("Alentejo, montado", 38.60, -8.00, "[EU-Forest, 1/k] ekoregion 805/793: 84-93 % liściastych to dęby wiecznie zielone",
     [("wiecznie ziel. wśród liściastych", lambda d: S(d, "mediterranean", "maquis") / max(1e-9, 1 - con(d)), .7, 1),
      ("eukaliptus", lambda d: S(d, "eucalyptus"), 0, .15)]),
    ("Umbria", 43.00, 12.00, "[EU-Forest, 1/k] ekoregion 795: 10 % liściastych wiecznie zielone, iglaste 11 %",
     [("iglaste", con, 0, .30), ("wiecznie ziel. wśród liściastych", lambda d: S(d, "mediterranean") / max(1e-9, 1 - con(d)), 0, .3)]),
    ("Split, wybrzeże", 43.52, 16.30, "[niezweryfikowane liczbowo; brak danych NFI dla Chorwacji] wybrzeże: makia, dąb ostrolistny, sosna alepska; zaplecze: dąb omszony, grab wschodni",
     [("śródziemn. + sosna nadm.", lambda d: S(d, "mediterranean", "maquis", "pine_maritime"), .6, 1)]),
    # --- Americas
    ("Pensylwania, Allegheny", 41.00, -77.50, "[liczba drzew] Penn State: klon czerwony najczęstszy; typy: dąb/hikora i klon/buk/brzoza https://extension.psu.edu/from-the-woods-ten-important-hardwoods",
     [("liściaste (dąb+buk)", lambda d: S(d, "oak", "beech"), .55, 1), ("brzoza", lambda d: S(d, "birch"), 0, .15),
      ("iglaste", con, 0, .30)]),
    ("Maine, północ", 45.50, -69.50, "[powierzchnia] USFS FIA Maine 2016: klon/buk/brzoza ~42 %, świerk/jodła ~33 %, osika/brzoza 11 %, sosny 7 % https://www.fs.usda.gov/nrs/pubs/ru/ru_fs86.pdf",
     [("iglaste", con, .35, .85), ("brzoza", lambda d: S(d, "birch"), .08, .50)]),
    ("Georgia, pas sosnowy", 32.50, -83.50, "[powierzchnia, cały stan] FIA GA 1997: typy sosnowe 45 % (lokalnie więcej) https://gatrees.org/resources/fia-statistics-report-for-georgia-1997",
     [("iglaste", con, .45, 1)]),
    ("Olympic, WA", 47.80, -123.90, "daglezja, choina, świerk sitka",
     [("iglaste", con, .80, 1), ("sekwoja", lambda d: S(d, "redwood"), 0, 0)]),
    ("Humboldt Redwoods, CA", 40.33, -123.95, "sekwoje wieczniezielone",
     [("sekwoja", lambda d: S(d, "redwood"), .05, 1)]),
    ("Amazonia, Manaus", -2.90, -60.00, "las równikowy",
     [("tropikalne", lambda d: S(d, *TROPICAL), .85, 1), ("umiarkowane", lambda d: S(d, *TEMPERATE_ANY), 0, 0)]),
    ("Chile, matorral", -34.00, -71.20, "matorral, bosque esclerófilo",
     [("śródziemnomorskie", lambda d: S(d, "mediterranean", "maquis"), .50, 1),
      ("brzoza", lambda d: S(d, "birch"), 0, .02)]),
    ("Chile, Valdivia", -39.80, -73.00, "las waldiwijski + plantacje sosny radiata",
     [("dąb + brzoza (europejskie)", lambda d: S(d, "oak", "birch"), 0, .05)]),
    # --- Africa
    ("Tsavo, Kenia", -2.90, 38.40, "sawanna akacjowo-commiphorowa",
     [("sawanna", lambda d: S(d, "savanna"), .60, 1), ("umiarkowane", lambda d: S(d, *TEMPERATE_ANY), 0, 0)]),
    ("Zambia, miombo", -12.00, 28.00, "las miombo (Brachystegia)",
     [("tropikalne/sawanna", lambda d: S(d, *TROPICAL), .80, 1), ("umiarkowane", lambda d: S(d, *TEMPERATE_ANY), 0, 0)]),
    ("Kapsztad", -33.95, 18.45, "fynbos, plantacje sosny",
     [("dąb + buk + brzoza", lambda d: S(d, "oak", "beech", "birch"), 0, .30)]),
    # --- Asia, Oceania
    ("Kerala, Ghaty", 10.00, 77.00, "wilgotny las tropikalny",
     [("tropikalne", lambda d: S(d, *TROPICAL), .85, 1), ("umiarkowane", lambda d: S(d, *TEMPERATE_ANY), 0, 0)]),
    ("Japonia, Nagano", 36.60, 138.20, "buk japoński, dąb mongolski, cedr, cyprys",
     [("tropikalne", lambda d: S(d, "tropical", "palm", "savanna"), 0, 0),
      ("stożkowe wśród iglastych", lambda d: share_of_con(d, "spruce"), .5, 1)]),
    ("Japonia, Miyazaki", 31.90, 131.30, "[powierzchnia] pref. Miyazaki: 60 % lasu sadzone, sugi 72 % prywatnych plantacji https://www.pref.miyazaki.lg.jp/documents/15577/15577_20260526102528-1.pdf",
     [("wawrzynowe wśród liściastych", lambda d: S(d, "laurel") / max(1e-9, 1 - con(d)), .5, 1),
      ("stożkowe (cedr) wśród iglastych", lambda d: share_of_con(d, "spruce"), .5, 1),
      ("brzoza", lambda d: S(d, "birch"), 0, .05)]),
    ("Chiny płd., Guangdong", 24.50, 113.50, "wiecznie zielone lasy liściaste, sosna masson",
     [("brzoza", lambda d: S(d, "birch"), 0, .03),
      ("wawrzynowe wśród liściastych", lambda d: S(d, "laurel") / max(1e-9, 1 - con(d)), .5, 1)]),
    ("Syberia, Krasnojarsk", 60.00, 100.00, "tajga: modrzew, sosna, brzoza",
     [("iglaste", con, .45, 1), ("dąb + buk", lambda d: S(d, "oak", "beech"), 0, .02)]),
    ("Australia, Wiktoria", -37.50, 145.50, "[powierzchnia] SOFR 2023 ind. 1.1a: eukaliptusy 77 % lasów naturalnych Australii, 68-99 % w każdym stanie https://www.agriculture.gov.au/sites/default/files/documents/Indicator_1.1a-i_Forest_area_by_type.pdf",
     [("eukaliptus", lambda d: S(d, "eucalyptus"), .65, 1), ("dąb + brzoza", lambda d: S(d, "oak", "birch"), 0, .05)]),
    ("Nowa Zelandia, Southland", -45.00, 168.70, "Nothofagus + plantacje radiaty",
     [("dąb + brzoza", lambda d: S(d, "oak", "birch"), 0, .05), ("buk + sosna", lambda d: S(d, "beech", "pine_west"), .6, 1)]),
]


def key(lat, lon, z=14):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return (z, x, y)


def composition(v, lat, lon):
    it = v.cell(key(lat, lon))
    tr = (it.kind == veg.KIND_TREE) & ~np.isin(it.group, list(veg.CLUMP_GROUPS))
    names = [veg.GROUPS[int(g)].replace("_open", "") for g in it.group[tr]]
    n = len(names)
    d = {}
    for nm in names:
        d[nm] = d.get(nm, 0) + 1
    return n, {k: c / n for k, c in d.items()} if n else {}


def main(cache, only=""):
    md = mapdata.MapData(cache)
    v = veg.Vegetation(md, None)
    v.dem = demmod.Dem(os.path.join(cache, "dem"), fetcher=netfetch.Fetcher(offline=False))
    res = []
    for name, lat, lon, fact, checks in SITES:
        if only and not name.startswith(only):
            continue
        try:
            n, d = composition(v, lat, lon)
        except Exception as e:      # (offline tiles etc.)
            res.append({"site": name, "error": f"{type(e).__name__}: {e}"})
            continue
        r = {"site": name, "lat": lat, "lon": lon, "fact": fact, "n": n,
             "top": sorted(((k, round(100 * x)) for k, x in d.items() if x >= .02), key=lambda t: -t[1]), "checks": []}
        for label, f, lo, hi in checks:
            val = f(d) if n >= 200 else None
            r["checks"].append({"label": label, "value": None if val is None else round(val, 3), "lo": lo, "hi": hi,
                                "ok": None if val is None else bool(lo - 1e-9 <= val <= hi + 1e-9)})
        res.append(r)
        print(json.dumps(r, ensure_ascii=False), flush=True)
    print("JSON" + json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
