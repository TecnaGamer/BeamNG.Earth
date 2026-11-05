"""Address and place search for the in-game map's search box (the user, 2026-10-14: "look up real addresses and
places as you type, autocomplete style").

One public search service cannot do it alone: photon.komoot.io refuses the app (GitHub #602), and even when it answers
it knows only the exact name in one language ("Ganter Bridge": Kentucky). So several sources are asked at once and their
answers ranked together (search()):
  * Wikipedia and Wikidata, as you type: landmarks in any language, under any of their names, with short answers
  * Photon, as you type: OpenStreetMap's streets and places, while it answers (a refusal: left alone for REFUSED_SKIP_S)
  * Nominatim, on Enter only (full=True): OpenStreetMap's streets and house numbers; its usage policy forbids
    autocomplete, and more than one request a second (netfetch)
  * superlatives_data, the app's own table: "highest bridge in Switzerland", which no search service answers
Google is not used (project rule). The mod sends the typed text and the player's position (a location bias: nearer
results first); requests are debounced by the map (300 ms, 3+ characters) and limited by netfetch. The map credits
OpenStreetMap, Photon, Wikipedia and Wikidata.
"""
import concurrent.futures
import difflib
import json
import locale
import math
import re
import threading
import time
import unicodedata
import urllib.parse

import netfetch
import superlatives_data

PHOTON_URL = "https://photon.komoot.io/api/"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
WIKIDATA_URL = "https://www.wikidata.org/w/api.php"
LIMIT = 7
RESULTS = 8                     # at most this many in the map's list
MIN_CHARS = 3
ATTRIBUTION = "Search: OpenStreetMap (Nominatim, Photon by komoot), Wikipedia, Wikidata"
# Names in the language of the country the player is in, where Photon has it (en, de, fr, it; the status bar's
# place names too, placenames.py): Photon's lang="en"
# showed "Cologne", "Switzerland" to players in Cologne (SwissCheese26's PR #275: German names for DACH). By the
# country's main language, from the player's country (earthd: navgraph.country_near, ISO code); English elsewhere.
LANG_OF_COUNTRY = {**dict.fromkeys(("DE", "AT", "CH", "LI"), "de"), **dict.fromkeys(("FR", "MC", "LU"), "fr"),
                   **dict.fromkeys(("IT", "SM", "VA"), "it")}


def lang_for(country):
    """Photon's language for a player in country (ISO 3166 alpha-2; None: unknown): "en" where Photon has none of
    the country's. Pure."""
    return LANG_OF_COUNTRY.get(str(country or "").upper(), "en")


def url(q, lat=None, lon=None, limit=LIMIT, lang="en"):
    """The Photon request for text q, biased toward (lat, lon) when given. Pure."""
    params = [("q", q.strip()), ("limit", str(int(limit))), ("lang", lang)]
    if lat is not None and lon is not None:
        params += [("lat", f"{float(lat):.4f}"), ("lon", f"{float(lon):.4f}")]
    return PHOTON_URL + "?" + urllib.parse.urlencode(params)


def label(props):
    """(name, detail) of one Photon feature's properties: the place or the street address, then the town,
    state and country. Pure."""
    p = props or {}
    street = " ".join(v for v in (p.get("housenumber"), p.get("street")) if v)
    name = p.get("name") or street or p.get("city") or p.get("state") or p.get("country") or "?"
    parts = []
    if street and street != name:
        parts.append(street)
    for k in ("city", "district", "county", "state", "country"):
        v = p.get(k)
        if v and v != name and v not in parts:
            parts.append(v)
        if len(parts) >= 3:
            break
    kind = p.get("osm_value") or p.get("type") or ""
    return name, ", ".join(parts), kind


def parse(data):
    """[{name, detail, kind, lat, lon}] of a Photon GeoJSON response. Pure."""
    out = []
    for f in (data or {}).get("features") or []:
        try:
            lon, lat = f["geometry"]["coordinates"][:2]
            lat, lon = float(lat), float(lon)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        name, detail, kind = label(f.get("properties"))
        out.append({"name": name, "detail": detail, "kind": kind, "lat": round(lat, 6), "lon": round(lon, 6)})
    return out


# What the map's search box says when the search has no answer (the user's "Address search unavailable (offline?)" for
# every failure: photon.komoot.io answered 403 to the app's User-Agent and the player was told to check their
# connection, GitHub #602). A refusal opens the host's circuit breaker after a few tries, and the requests after it
# fail without a status (HostUnavailable): the last refusal is kept so they still say why.
REFUSED_KEEP_S = 600.0
_refused = {}                   # "status" -> (HTTP status, time.monotonic() it came)


def error_text(e, now=None):
    """The search box's words for e, the exception search() raised (netfetch's errors; anything else: a
    bad answer). Pure but for the last refusal it remembers."""
    import time
    now = time.monotonic() if now is None else now
    status = getattr(e, "status", None)
    if isinstance(e, netfetch.FetchFailed) and status in (401, 403, 451):
        _refused["status"] = (status, now)
    if isinstance(e, netfetch.Offline):
        return "Address search unavailable (offline mode)"
    if isinstance(e, netfetch.RateLimited):
        return "Address search busy (too many requests): try again in a minute"
    if isinstance(e, netfetch.BudgetExceeded):
        return "Address search limit for today reached"
    held = _refused.get("status")
    if isinstance(e, netfetch.HostUnavailable):
        if held is not None and now - held[1] < REFUSED_KEEP_S:
            return f"Address search service refused the request (HTTP {held[0]})"
        return "Address search service not answering: try again in a minute"
    if isinstance(e, netfetch.FetchFailed) and status in (401, 403, 451):
        return f"Address search service refused the request (HTTP {status})"
    if isinstance(e, netfetch.FetchFailed) and status is not None:
        return f"Address search service error (HTTP {status})"
    if isinstance(e, netfetch.FetchError):
        return "Address search unavailable (offline?)"
    return "Address search gave an answer the map cannot read"


# ------------------------------------------------------------------------------------------- the sources
TYPING_DEADLINE_S = 6.0         # a search gives what it has by now (the map waits longer: MAP_WAIT_TRIES, EarthMap.vue)
ENTER_DEADLINE_S = 9.0
REFUSED_SKIP_S = 3600.0         # a service that refused the app is left alone this long
CACHE_S = 3600.0
CACHE_MAX = 512
WEAK = 0.8                      # the best match below this: look further (variants)
KEEP = 0.45                     # with a good match in the list, weaker ones than this are left out
NEAR_DEG = 0.75                 # a street's name alone is also looked for this far around the player
WIKI_KINDS = {"wikipedia", "wikidata"}
STREET_WORD = re.compile(r"(?:strasse|gasse|weg|pfad|allee|platz|steig|chaussee|street|road|avenue|lane|drive|rue|via)\b")
FLOOR = 0.3                     # less than this has nothing to do with the text (a word deep in an article)

_pool = concurrent.futures.ThreadPoolExecutor(8, thread_name_prefix="place-search")
_lock = threading.Lock()
_cache = {}                     # url -> (time.monotonic(), parsed JSON)
_skip = {}                      # source -> time.monotonic() it may be asked again

# Wikipedia's language for a country (netfetch.WIKIPEDIA_LANGS: its hosts have a policy); en is always asked.
WIKI_LANG_OF_COUNTRY = {
    **LANG_OF_COUNTRY, **dict.fromkeys(("ES", "MX", "AR", "CL", "CO", "PE", "VE", "UY", "BO", "EC"), "es"),
    **dict.fromkeys(("PT", "BR"), "pt"), **dict.fromkeys(("NL", "BE"), "nl"), "PL": "pl", "CZ": "cs", "SK": "sk",
    "SE": "sv", "NO": "no", "DK": "da", "FI": "fi", "HU": "hu", "RO": "ro", "GR": "el", "TR": "tr", "RU": "ru",
    "UA": "uk", "JP": "ja", "KR": "ko", "CN": "zh", "TW": "zh", "HR": "hr", "SI": "sl", "BG": "bg", "RS": "sr"}


def user_lang():
    """The two letters of the computer's language, or ''."""
    try:
        name = locale.getlocale()[0] or ""
    except (ValueError, TypeError):
        return ""
    if name[:2].isalpha() and name[2:3] in ("", "_", "-"):
        return name[:2].lower()
    return {"german": "de", "french": "fr", "italian": "it", "spanish": "es", "dutch": "nl", "polish": "pl",
            "portuguese": "pt", "swedish": "sv", "czech": "cs", "japanese": "ja"}.get(name.split("_")[0].lower(), "")


def wiki_langs(country, user=None):
    """The Wikipedias to ask for a player in country: its language, the computer's, English. Pure given user."""
    user = user_lang() if user is None else user
    out = []
    for lang in (WIKI_LANG_OF_COUNTRY.get(str(country or "").upper()), user, "en"):
        if lang in netfetch.WIKIPEDIA_LANGS and lang not in out:
            out.append(lang)
    return out


def wikipedia_url(q, lang, limit=LIMIT):
    return f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "generator": "search", "gsrsearch": q, "gsrlimit": limit, "gsrnamespace": 0,
        "prop": "coordinates|description", "coprimary": "primary", "colimit": "max", "format": "json",
        "formatversion": 2})


def parse_wikipedia(data):
    """The articles of a Wikipedia answer that have a place on Earth, the best first. Pure."""
    out = []
    pages = ((data or {}).get("query") or {}).get("pages") or []
    for p in sorted(pages, key=lambda p: p.get("index", 99)):
        c = (p.get("coordinates") or [None])[0]
        if not c or c.get("globe", "earth") != "earth":
            continue
        try:
            lat, lon = float(c["lat"]), float(c["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"name": p.get("title") or "?", "detail": p.get("description") or "", "kind": "wikipedia",
                    "lat": round(lat, 6), "lon": round(lon, 6), "rank": p.get("index", 9)})
    return out


def wikidata_search_url(q, lang):
    return WIKIDATA_URL + "?" + urllib.parse.urlencode({
        "action": "wbsearchentities", "search": q, "language": lang, "uselang": lang, "type": "item", "limit": 6,
        "format": "json"})


def wikidata_items_url(ids, lang):
    return WIKIDATA_URL + "?" + urllib.parse.urlencode({
        "action": "wbgetentities", "ids": "|".join(ids), "props": "claims|labels|descriptions",
        "languages": lang if lang == "en" else lang + "|en", "format": "json"})


def parse_wikidata(data, ids, lang):
    """The items of a wbgetentities answer that have a place on Earth, in the order of ids. Pure."""
    out = []
    entities = (data or {}).get("entities") or {}
    for rank, qid in enumerate(ids, 1):
        e = entities.get(qid) or {}
        try:
            v = e["claims"]["P625"][0]["mainsnak"]["datavalue"]["value"]
            if not str(v.get("globe", "")).endswith("/Q2"):
                continue
            lat, lon = float(v["latitude"]), float(v["longitude"])
        except (KeyError, IndexError, TypeError, ValueError):
            continue

        def text(field):
            values = e.get(field) or {}
            return (values.get(lang) or values.get("en") or {}).get("value") or ""
        if text("labels"):
            out.append({"name": text("labels"), "detail": text("descriptions"), "kind": "wikidata",
                        "lat": round(lat, 6), "lon": round(lon, 6), "rank": rank})
    return out


def nominatim_url(q, lat=None, lon=None, limit=LIMIT, lang="en", near=False):
    """near: only around (lat, lon); else that area is a bias, not a limit."""
    params = [("q", q), ("format", "jsonv2"), ("limit", int(limit)), ("addressdetails", 1), ("accept-language", lang)]
    if lat is not None and lon is not None:
        la, lo, half = float(lat), float(lon), NEAR_DEG if near else 2.0
        params.append(("viewbox", f"{lo - half:.3f},{la + half:.3f},{lo + half:.3f},{la - half:.3f}"))
        if near:
            params.append(("bounded", 1))
    return NOMINATIM_URL + "?" + urllib.parse.urlencode(params)


def parse_nominatim(data):
    """[{name, detail, kind, lat, lon, rank}] of a Nominatim jsonv2 answer. Pure."""
    out = []
    for rank, r in enumerate(data if isinstance(data, list) else [], 1):
        try:
            lat, lon = float(r["lat"]), float(r["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        a = r.get("address") or {}
        street = " ".join(v for v in (a.get("road"), a.get("house_number")) if v)
        name = r.get("name") or street or str(r.get("display_name") or "?").split(",")[0]
        town = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or a.get("municipality")
        parts = [v for v in (street, town, a.get("state"), a.get("country")) if v and v != name]
        out.append({"name": name, "detail": ", ".join(parts[:3]), "kind": r.get("type") or r.get("category") or "",
                    "lat": round(lat, 6), "lon": round(lon, 6), "rank": rank, "importance": r.get("importance"),
                    "where": r.get("display_name") or ""})     # the whole address, for matching: town and district
    return out


def _json(net, url):
    """The JSON at url, from CACHE_S of memory when asked before (a typed word is asked again and again)."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(url)
    if hit and now - hit[0] < CACHE_S:
        return hit[1]
    data = json.loads(net.get(url, kind="demand").body.decode("utf-8"))
    with _lock:
        if len(_cache) >= CACHE_MAX:
            _cache.clear()
        _cache[url] = (now, data)
    return data


def _photon(net, q, lat, lon, lang):
    return [{**r, "rank": i} for i, r in enumerate(parse(_json(net, url(q, lat, lon, lang=lang))), 1)]


def _wikipedia(net, q, lang):
    return parse_wikipedia(_json(net, wikipedia_url(q, lang)))


def _wikidata(net, q, lang):
    found = (_json(net, wikidata_search_url(q, lang)) or {}).get("search") or []
    ids = [str(x["id"]) for x in found if x.get("id")]
    return parse_wikidata(_json(net, wikidata_items_url(ids, lang)), ids, lang) if ids else []


def _nominatim(net, q, lat, lon, lang, near=False):
    return parse_nominatim(_json(net, nominatim_url(q, lat, lon, lang=lang, near=near)))


# ------------------------------------------------------------------------------------------- matching
def fold(text):
    """text for comparing: no case, accents or punctuation; ue = u, ss = sz, str. = strasse. Pure."""
    t = unicodedata.normalize("NFKD", str(text or "").replace("ß", "ss").replace("ẞ", "ss").casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"(?<=[a-z])ae", "a", re.sub(r"(?<=[a-z])oe", "o", re.sub(r"(?<=[a-z])ue", "u", t)))
    t = re.sub(r"(?<=[a-z])str\.", "strasse", t)
    return " ".join(re.sub(r"[^\w]+", " ", t).split())


def match(q, name, detail=""):
    """How well a result fits the typed text, 0..1. Pure."""
    fq, fn = fold(q), fold(name)
    if not fq or not fn:
        return 0.0
    cq, cn = fq.replace(" ", ""), fn.replace(" ", "")
    if cq == cn:
        return 1.0
    if cn.startswith(cq):
        return 0.9
    words = fq.split()
    have = fn.split()
    hay = have + fold(detail).split()
    if all(w in have for w in words):
        return 0.88
    if all(any(h.startswith(w) for h in hay) for w in words):
        return 0.85
    if cq in cn:
        return 0.8
    # part of the text: by the letters found, so "Ganterbrücke Simplon" prefers the bridge to the pass
    got = sum(len(w) for w in words if any(h.startswith(w) or (len(h) > 3 and w.startswith(h)) for h in hay))
    part = 0.75 * got / max(1, sum(len(w) for w in words))
    return max(part, 0.85 * difflib.SequenceMatcher(None, cq, cn).ratio() ** 2)


def fits(q, r):
    """match of q and result r, with all r says about where it is."""
    return match(q, r["name"], " ".join(v for v in (r.get("detail"), r.get("where")) if v))


def street_like(q):
    """q looks like a street's name, which only OpenStreetMap knows: an encyclopedia's namesake is no answer. Pure."""
    return bool(STREET_WORD.search(fold(q)))


def km(lat1, lon1, lat2, lon2):
    x = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return 111.2 * math.hypot(x, lat2 - lat1)


def score(q, r, lat=None, lon=None):
    """The rank of result r for text q: the match first, then the source's own order, then nearness. Pure."""
    s = fits(q, r)
    have = fold(r["name"]).split()
    words = fold(q).split()
    address = any(c.isdigit() for c in q) and r.get("kind") not in ("wikipedia", "wikidata")
    s += (0.3 if address else 0.1) * sum(w in have for w in words) / max(1, len(words))   # in the name, not just its town
    s += (0.2 if address else 0.06) / max(1, r.get("rank", 9))        # an address: the geocoder knows best
    s += 1.5 * max(0.0, float(r.get("importance") or 0.0) - 0.4)    # a landmark among like addresses (Washington, D.C.)
    if lat is not None and lon is not None:
        wide = r.get("kind") in WIKI_KINDS      # (of many streets of one name, the nearest)
        s += (0.06 if wide else 0.15) * math.exp(-km(lat, lon, r["lat"], r["lon"]) / (300.0 if wide else 100.0))
    if any(c.isdigit() for c in q) and any(c.isdigit() for c in r["name"] + r.get("detail", "")):
        s += 0.1                # a house number was asked for and this has one
    return s


def _same_place(a, b, d, encyclopedia=False):
    """Two folded names d km apart are one place: the same name ("Umbrail Pass", "Umbrailpass") close by (encyclopedia:
    two articles' middle of a large place, further apart), or a like name on the spot, as the languages call it ("Ganter
    Bridge", "Ganterbrücke", "Viaduc de Millau"). Pure."""
    ca, cb = a.replace(" ", ""), b.replace(" ", "")
    if ca == cb and d < (30.0 if encyclopedia else 3.0):
        return True
    if d >= 0.5:
        return False
    shared = any(len(w) >= 4 for w in set(a.split()) & set(b.split()))
    return shared or ca in cb or cb in ca or difflib.SequenceMatcher(None, ca, cb).ratio() >= 0.6


def merge(q, results, lat=None, lon=None, limit=RESULTS):
    """results ranked, one line per place (_same_place). Pure."""
    ranked = sorted(((score(q, r, lat, lon), i, r) for i, r in enumerate(results)), key=lambda t: (-t[0], t[1]))
    out = []
    for s, _, r in ranked:
        fn = fold(r["name"])
        same = None
        for o in out:
            d = km(o["lat"], o["lon"], r["lat"], r["lon"])
            on = fold(o["name"])
            if _same_place(on, fn, d, {o.get("kind"), r.get("kind")} <= WIKI_KINDS):
                same = o
                break
        if same is not None:
            if not same.get("detail") and r.get("detail"):
                same["detail"] = r["detail"]
            continue
        out.append({**r, "score": round(s, 3)})
    if out and fits(q, out[0]) >= WEAK:
        out = [r for r in out if fits(q, r) >= KEEP]
    out = [r for r in out if fits(q, r) >= FLOOR]
    final = [{"name": r["name"], "detail": r.get("detail") or "", "kind": r.get("kind") or "", "lat": r["lat"],
              "lon": r["lon"], "score": r["score"]} for r in out[:limit]]
    if final and fold(final[0]["name"]).replace(" ", "") == fold(q).replace(" ", "") and not street_like(q) \
            and not any(c.isdigit() for c in q):
        final[0]["sure"] = True     # Enter may take it without the full search (EarthMap.vue onSearch)
    return final


def variants(q):
    """Other ways to ask for q: "Felsenau Viadukt" as one word, "Ganterbrücke Simplon" word by word. Pure."""
    words = q.split()
    if len(words) < 2:
        return []
    out = []
    if len(words) == 2:
        out.append(words[0] + words[1].lower())
    out += sorted((w for w in words if len(w) >= 4), key=len, reverse=True)[:2]
    return out


# ------------------------------------------------------------------------------------------- superlatives
SUPER_WORDS = {
    "high": ("highest", "tallest", "hochste", "hochster", "hochstes", "hochsten", "plus haut", "plus haute", "piu alto",
             "piu alta", "mas alto", "mas alta", "hoogste", "najwyzszy", "najwyzsza"),
    "long": ("longest", "langste", "langster", "langstes", "langsten", "plus long", "plus longue", "piu lungo",
             "piu lunga", "mas largo", "mas larga", "najdluzszy", "najdluzsza"),
}
NOUN_WORDS = {
    "bridge": ("bridge", "bridges", "brucke", "brucken", "viadukt", "viaduct", "overpass", "uberfuhrung", "talbrucke",
               "pont", "viaduc", "ponte", "viadotto", "puente", "viaducto", "brug", "most"),
    "tunnel": ("tunnel", "tunnels", "strassentunnel", "road tunnel", "galleria", "tunel"),
    "pass": ("pass", "passe", "passstrasse", "mountain pass", "gebirgspass", "bergpass", "alpenpass", "col", "passo",
             "puerto"),
    "dam": ("dam", "dams", "staudamm", "staumauer", "talsperre", "barrage", "diga", "presa", "stuwdam"),
}
SUPER_KIND = {("high", "bridge"): "bridge_height", ("long", "bridge"): "bridge_length", ("long", "tunnel"): "tunnel_length",
              ("high", "pass"): "pass_elevation", ("high", "dam"): "dam_height"}
SUPER_TEXT = {"bridge_height": "m high", "bridge_length": "m long", "tunnel_length": "m long",
              "pass_elevation": "m above sea level (may have no road)", "dam_height": "m high"}
WORLD_WORDS = frozenset("welt world weltweit earth erde".split())
FILLER = WORLD_WORDS | frozenset("der die das den dem des in im am auf von vom of the a an de la le du en al del dans di "
                                 "nel".split())


_COUNTRIES = {fold(name): cc for name, cc in superlatives_data.COUNTRIES.items()}


def _take(text, words):
    """(the word of words found in text, text without it), longest words first. Pure."""
    for w in sorted(words, key=len, reverse=True):
        m = re.search(rf"\b{re.escape(w)}\b", text)
        if m:
            return w, " ".join((text[:m.start()] + " " + text[m.end():]).split())
    return None, text


def superlative(q):
    """(table kind, ISO country) of a question like "highest bridge in Switzerland", or None. The country: None when
    none is named, '' for the world. Pure."""
    text = fold(q)
    for size, words in SUPER_WORDS.items():
        hit, rest = _take(text, words)
        if hit is None:
            continue
        for noun, names in NOUN_WORDS.items():
            found, rest2 = _take(rest, names)
            if found is None or (size, noun) not in SUPER_KIND:
                continue
            world = any(w in WORLD_WORDS for w in rest2.split())
            left = " ".join(w for w in rest2.split() if w not in FILLER)
            country = (_COUNTRIES.get(left) or _COUNTRIES.get(left.rstrip("s"))) if left else None
            if left and country is None:
                return None     # something more was asked ("highest bridge cafe"): not this question
            return SUPER_KIND[(size, noun)], "" if world else country
    return None


def superlative_results(kind, cc, player_cc, lang):
    """The table's places for kind: cc's; no country named: the player's country, then the world's. Pure."""
    table = superlatives_data.TABLE.get(kind) or {}
    areas = [cc] if cc is not None else [str(player_cc or "").upper(), ""]
    out = []
    for a in areas:
        for i, (en, de, v, la, lo) in enumerate(table.get(a) or [], 1):
            if any(abs(o["lat"] - la) < 1e-4 and abs(o["lon"] - lo) < 1e-4 for o in out):
                continue
            where = a or "the world"
            out.append({"name": de if lang == "de" else en, "detail": f"#{i} in {where}: {v} {SUPER_TEXT[kind]}",
                        "kind": kind, "lat": la, "lon": lo, "score": round(2.0 - 0.01 * len(out), 3), "sure": True})
    return out[:RESULTS]


# ------------------------------------------------------------------------------------------- the search
def _ask(jobs, deadline):
    """Run jobs ({name: (function, args)}) at once until deadline (time.monotonic()): (results, errors, unanswered)."""
    futures = {_pool.submit(fn, *args): name for name, (fn, args) in jobs.items()}
    done, late = concurrent.futures.wait(futures, timeout=max(0.0, deadline - time.monotonic()))
    results, errors = [], []
    for f in sorted(done, key=lambda f: list(jobs).index(futures[f])):
        try:
            results += f.result()
        except Exception as e:
            if futures[f] == "photon" and getattr(e, "status", None) in (401, 403, 451):
                with _lock:
                    _skip["photon"] = time.monotonic() + REFUSED_SKIP_S
            errors.append(e)
    return results, errors, len(late)


def search(q, lat=None, lon=None, net=None, country=None, full=False):
    """Results for q (blocking; [] for a short query), best first: [{name, detail, kind, lat, lon, score}]. full: the
    player pressed Enter, so the sources that may not be asked while typing are asked too. Raises what the sources
    raised (netfetch.FetchError) only when every one of them failed and there is nothing to show."""
    q = (q or "").strip()
    if len(q) < MIN_CHARS:
        return []
    lang = lang_for(country)
    langs = wiki_langs(country)
    shown = user_lang() if user_lang() in netfetch.WIKIPEDIA_LANGS else langs[0]   # the player's language, not Peru's
    hit = superlative(q)
    if hit:
        found = superlative_results(hit[0], hit[1], country, shown)
        if found:
            return found
    net = net if net is not None else netfetch.default_fetcher()
    deadline = time.monotonic() + (ENTER_DEADLINE_S if full else TYPING_DEADLINE_S)
    jobs = {}
    if time.monotonic() >= _skip.get("photon", 0.0):
        jobs["photon"] = (_photon, (net, q, lat, lon, lang))
    if full:
        jobs["nominatim"] = (_nominatim, (net, q, lat, lon, shown))
        if lat is not None and lon is not None and street_like(q):
            jobs["nominatim near"] = (_nominatim, (net, q, lat, lon, shown, True))
    jobs["wikidata"] = (_wikidata, (net, q, shown))
    for w in langs:
        jobs["wikipedia " + w] = (_wikipedia, (net, q, w))
    results, errors, late = _ask(jobs, deadline)
    asked = len(jobs)
    if not any(fits(q, r) >= WEAK for r in results):
        more = {}
        for v in variants(q):
            more["wikidata " + v] = (_wikidata, (net, v, shown))
            for w in langs[:2]:
                more[f"wikipedia {w} {v}"] = (_wikipedia, (net, v, w))
        if " " not in q:        # one word, nothing like it: a typing mistake?
            for w in langs[:2]:
                more[f"wikipedia {w} ~"] = (_wikipedia, (net, q + "~", w))
        if full:
            for v in variants(q)[:1]:
                more["nominatim " + v] = (_nominatim, (net, v, lat, lon, shown))
        r2, e2, late2 = _ask(more, deadline)
        results += r2
        errors += e2
        late += late2
        asked += len(more)
    if not results and errors and len(errors) + late >= asked:
        raise errors[0]
    return merge(q, results, lat, lon)


# ------------------------------------------------------------------------------------------- where am I
# The status bar (the user, 2026-10-15: "location info like street, city, country"): the street from our own map data
# (nearest_street); the town, state and country from our own copy of Overture's divisions (placenames.py). Photon's
# reverse geocoding is no longer asked (GitHub #602: komoot refused the app's requests; the user, 2026-11-03: the
# status bar's names come from local data, the search box keeps Photon).
STREET_M = 40.0                 # the nearest named road within this is "the street"


# The route number a road is known by (overture "road_routes": its memberships "network|ref|from|to;..."): an Interstate
# or a US freeway has a number and often no name (I-94 across Montana: the status bar said only "Montana, United
# States", Tann2019 2026-11-05). On the fast classes the number comes first, as drivers call them.
ROUTE_FIRST_CLASSES = {"motorway", "trunk"}


def route_label(network, ref):
    """A route membership as drivers write it: us:i 94 -> "I-94", us:us 212 -> "US 212", us:mt 3 -> "MT 3"; any other
    network its ref as mapped ("A 9", "E 45"). Pure."""
    net, ref = str(network or "").lower(), str(ref or "").strip()
    if not ref:
        return ""
    if net == "us:i":
        return f"I-{ref}"
    if net == "us:us":
        return f"US {ref}"
    if len(net) == 5 and net.startswith("us:") and net[3:].isalpha():
        return f"{net[3:].upper()} {ref}"
    return ref


def _route_rank(network):
    net = str(network or "").lower()
    return 0 if net == "us:i" else 1 if net == "us:us" else 2 if net.startswith("us:") else 3


def road_route(routes_txt):
    """The label of a road_routes row's first-ranked route (an Interstate before a US route before a state route), or
    ''. Pure."""
    best = None
    for part in str(routes_txt or "").split(";"):
        bits = part.split("|")
        if len(bits) >= 2 and bits[1]:
            if best is None or _route_rank(bits[0]) < _route_rank(best[0]):
                best = (bits[0], bits[1])
    return route_label(*best) if best else ""


def nearest_street(roads_layers, lat, lon, max_m=STREET_M, routes=None):
    """The label of the nearest named or numbered road (mapgeom "roads" Features) within max_m of (lat, lon), or ''.
    routes: {segment id: routes_txt} (Surface.road_routes): a motorway or trunk road with a number is called by it
    (ROUTE_FIRST_CLASSES), any other road by its name, else its number. Pure."""
    import math

    import numpy as np
    mx = 111320.0 * math.cos(math.radians(lat))
    best, bd = "", max_m
    routes = routes or {}
    for f in roads_layers:
        if f is None or len(f.kind) == 0:
            continue
        names = f.attrs.get("name")
        classes = f.attrs.get("class")
        ids = getattr(f, "ids", None) if routes else None
        if names is None and ids is None:
            continue
        for i in range(len(f.kind)):
            nm = names[i] if names is not None else None
            nm = "" if nm is None or str(nm) in ("", "None", "nan") else str(nm)
            num = road_route(routes.get(str(ids[i]))) if ids is not None else ""
            first = num and classes is not None and str(classes[i]) in ROUTE_FIRST_CLASSES
            label = num if first else (nm or num)
            if not label:
                continue
            a = f.coord_off[f.ring_off[f.part_off[i]]]
            b = f.coord_off[f.ring_off[f.part_off[i + 1]]]
            ll = f.lonlat[a:b]
            if len(ll) < 2:
                continue
            xy = np.column_stack([(ll[:, 0] - lon) * mx, (ll[:, 1] - lat) * 110540.0])
            p, q = xy[:-1], xy[1:]
            d = q - p
            L2 = np.maximum((d * d).sum(1), 1e-9)
            t = np.clip(-(p * d).sum(1) / L2, 0.0, 1.0)
            dist = float(np.min(np.hypot(*(p + d * t[:, None]).T)))
            if dist < bd:
                best, bd = label, dist
    return best
