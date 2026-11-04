"""Address and place search for the in-game map's search box (the user, 2026-10-14: "look up real addresses and
places as you type, autocomplete style").

Photon (https://photon.komoot.io, komoot; OpenStreetMap data, ODbL) is built for search-as-you-type; Nominatim's
usage policy forbids autocomplete, and Google is not used (project rule). The mod sends the typed text and the
player's position (a location bias: nearer results first); requests are debounced by the map (300 ms, 3+
characters), limited and cached by netfetch (photon.komoot.io policy). The map credits OpenStreetMap / Photon.
"""
import json
import urllib.parse

import netfetch

PHOTON_URL = "https://photon.komoot.io/api/"
LIMIT = 7
MIN_CHARS = 3
ATTRIBUTION = "Search: Photon by komoot, data (c) OpenStreetMap contributors (ODbL)"
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


def search(q, lat=None, lon=None, net=None, country=None):
    """Results for q (blocking; [] for a short query), names in country's language (lang_for). Raises
    netfetch.FetchError on network trouble."""
    q = (q or "").strip()
    if len(q) < MIN_CHARS:
        return []
    net = net if net is not None else netfetch.default_fetcher()
    return parse(json.loads(net.get(url(q, lat, lon, lang=lang_for(country)), kind="demand").body.decode("utf-8")))


# ------------------------------------------------------------------------------------------- where am I
# The status bar (the user, 2026-10-15: "location info like street, city, country"): the street from our own map data
# (nearest_street); the town, state and country from our own copy of Overture's divisions (placenames.py). Photon's
# reverse geocoding is no longer asked (GitHub #602: komoot refused the app's requests; the user, 2026-11-03: the
# status bar's names come from local data, the search box keeps Photon).
STREET_M = 40.0                 # the nearest named road within this is "the street"


def nearest_street(roads_layers, lat, lon, max_m=STREET_M):
    """The name of the nearest named road (mapgeom "roads" Features) within max_m of (lat, lon), or ''. Pure."""
    import math

    import numpy as np
    mx = 111320.0 * math.cos(math.radians(lat))
    best, bd = "", max_m
    for f in roads_layers:
        if f is None or len(f.kind) == 0:
            continue
        names = f.attrs.get("name")
        if names is None:
            continue
        for i in range(len(f.kind)):
            nm = names[i]
            if nm is None or str(nm) in ("", "None", "nan"):
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
                best, bd = str(nm), dist
    return best
