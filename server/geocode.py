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
# Names in the language of the country the player is in, where Photon has it (en, de, fr, it): Photon's lang="en"
# showed "Cologne", "Switzerland" to players in Cologne (SwissCheese26's PR #275: German names for DACH). By the
# country's main language, from the player's country (earthd: navgraph.country_near, ISO code); English elsewhere.
LANG_OF_COUNTRY = {**dict.fromkeys(("DE", "AT", "CH", "LI"), "de"), **dict.fromkeys(("FR", "MC", "LU"), "fr"),
                   **dict.fromkeys(("IT", "SM", "VA"), "it")}


def lang_for(country):
    """Photon's language for a player in country (ISO 3166 alpha-2; None: unknown): "en" where Photon has none of
    the country's. Pure."""
    return LANG_OF_COUNTRY.get(str(country or "").upper(), "en")


def url(q, lat=None, lon=None, limit=LIMIT, lang="en", zoom=None, bias=None):
    """The Photon request for text q, biased toward (lat, lon) when given (zoom: how wide a bias area, bias: how
    hard it pulls, 0..1). Pure."""
    params = [("q", q.strip()), ("limit", str(int(limit))), ("lang", lang)]
    if lat is not None and lon is not None:
        params += [("lat", f"{float(lat):.4f}"), ("lon", f"{float(lon):.4f}")]
        if zoom is not None:
            params.append(("zoom", str(int(zoom))))
        if bias is not None:
            params.append(("location_bias_scale", f"{float(bias):.2f}"))
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


def search(q, lat=None, lon=None, net=None, country=None, zoom=None, bias=None):
    """Results for q (blocking; [] for a short query), names in country's language (lang_for), the search biased to
    (lat, lon) when given (zoom: how wide a bias area, bias: how hard it pulls). Raises netfetch.FetchError on
    network trouble."""
    q = (q or "").strip()
    if len(q) < MIN_CHARS:
        return []
    net = net if net is not None else netfetch.default_fetcher()
    u = url(q, lat, lon, lang=lang_for(country), zoom=zoom, bias=bias)
    return parse(json.loads(net.get(u, kind="demand").body.decode("utf-8")))


# ------------------------------------------------------------------------------------------- where am I
# The status bar (the user, 2026-10-15: "location info like street, city, country"): the town, state and country
# from Photon's reverse geocoding, asked with the position ROUNDED to REVERSE_ROUND decimals (~1 km: the bar needs the
# town, not the house) and only when the mod's setting allows it; the street comes from our own map data (nearest_street).
REVERSE_URL = "https://photon.komoot.io/reverse"
REVERSE_ROUND = 2
STREET_M = 40.0                 # the nearest named road within this is "the street"


def reverse_url(lat, lon, lang="en"):
    """The Photon reverse request for (lat, lon) rounded to REVERSE_ROUND decimals. Pure."""
    return REVERSE_URL + "?" + urllib.parse.urlencode(
        [("lat", f"{round(float(lat), REVERSE_ROUND):.{REVERSE_ROUND}f}"),
         ("lon", f"{round(float(lon), REVERSE_ROUND):.{REVERSE_ROUND}f}"), ("lang", lang), ("limit", "1")])


def parse_place(data):
    """{place, region, country} of a Photon reverse response (the first feature; missing parts ''). Pure."""
    feats = (data or {}).get("features") or []
    p = (feats[0].get("properties") or {}) if feats else {}
    place = p.get("city") or p.get("town") or p.get("village") or p.get("district") or p.get("county") or ""
    if not place and p.get("type") in ("city", "town", "village", "locality"):
        place = p.get("name") or ""
    return {"place": place, "region": p.get("state") or "", "country": p.get("country") or ""}


def reverse(lat, lon, net=None, country=None):
    """The town, state and country around (lat, lon) (blocking), in country's language (lang_for). Raises
    netfetch.FetchError on network trouble."""
    net = net if net is not None else netfetch.default_fetcher()
    # (a lane of its own: the status bar asks as the player moves, and its timeouts opened the host's breaker and
    # shut the map's search out, "Address search unavailable", GitHub #350)
    return parse_place(json.loads(net.get(reverse_url(lat, lon, lang=lang_for(country)), kind="demand", lane="reverse")
                                  .body.decode("utf-8")))


def point_url(lat, lon, lang="en"):
    """The Photon reverse request for the exact (lat, lon), to name a dropped pin. Pure."""
    return REVERSE_URL + "?" + urllib.parse.urlencode(
        [("lat", f"{float(lat):.6f}"), ("lon", f"{float(lon):.6f}"), ("lang", lang), ("limit", "1")])


def point_name(lat, lon, net=None):
    """(name, detail) of the place at (lat, lon): a street address, else the nearest named place (blocking)."""
    net = net if net is not None else netfetch.default_fetcher()
    data = json.loads(net.get(point_url(lat, lon), kind="demand").body.decode("utf-8"))
    feats = (data or {}).get("features") or []
    if not feats:
        return "", ""
    p = feats[0].get("properties") or {}
    name, detail, _ = label(p)
    num = p.get("housenumber")
    if num and name and str(num) not in name:
        name = f"{name} {num}"
    return name, detail


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
