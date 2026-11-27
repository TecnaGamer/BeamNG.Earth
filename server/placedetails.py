"""The modding API's place details (api.placeDetails in the mod's api.lua, docs/ADDONS.md "Place details"): the
address, phones, websites, socials, emails, brand and category path of a few places an add-on already has from
api.places, for a card about the place the player is passing (a drive-by info mode, a career's job board).

The map cells' places layer keeps only what the world's own readers use (names, categories, brand, status,
confidence: mapdata slims it for memory), so the details are read back from Overture's own place tile on disk
instead, the one the cell was built from (pmtiles.py: a point layer's cell is its z14 tile alone). Nothing is
downloaded for a request: a place whose tile is not on disk (a parquet-built cell, BNGE_MAP_SOURCE=parquet, or a
cache cleared since) is in `missing`. A tile is decoded once (only its "place" layer's tags, pure Python: ~1,300
places on Ottawa's Bank Street in ~30 ms) and kept in a small LRU, since a drive asks for the same tile's places
again and again.

Request {type="place_details", id, places=[{id, lat, lon}, ...]} (at most DETAILS_MAX; lat / lon as api.places gave
them) -> place_details_result {id, details, missing, error}:
  details  [{id, name, category, taxonomy, hierarchy, brand {name, wikidata}, address {freeform, locality,
           postcode, region, country}, phones, websites, socials, emails, open, confidence, sources}, ...] in the
           order asked; lists capped (LIST_MAX), strings cleaned and capped (TEXT_MAX), only http(s) links
  missing  [id, ...] the places not found in a tile on disk
Every field comes from Overture (CDLA Permissive 2.0 / ODbL by source: `sources` names each one's licence, for the
add-on's credits)."""
import gzip
import json
import math
import re
import struct
import threading
from collections import OrderedDict

import mapcells

DETAILS_MAX = 20
TEXT_MAX = 200
LIST_MAX = 4
ID_MAX = 64
TILES_KEPT = 24                 # decoded tiles (a few MB at most: one tile's tags are ~0.5 MB of Python strings)
LAYER = "place"                 # the places archive's MVT layer
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_URL = re.compile(r"^https?://[^\s\"'<>]+$", re.I)
_EMAIL = re.compile(r"^[^@\s\"'<>]+@[^@\s\"'<>]+\.[^@\s\"'<>]+$")


# --- the request ----------------------------------------------------------------------------------------------
def request(msg):
    """([(id, lat, lon), ...], None) or (None, why)."""
    rows = msg.get("places")
    if not isinstance(rows, list) or not rows:
        return None, "no places asked"
    if len(rows) > DETAILS_MAX:
        return None, f"at most {DETAILS_MAX} places at once"
    out, seen = [], set()
    for r in rows:
        if not isinstance(r, dict):
            return None, "a place must be {id, lat, lon}"
        pid = r.get("id")
        try:
            lat, lon = float(r.get("lat")), float(r.get("lon"))
        except (TypeError, ValueError):
            return None, "bad coordinates"
        if not isinstance(pid, str) or not pid or len(pid) > ID_MAX:
            return None, "bad place id"
        if not (math.isfinite(lat) and math.isfinite(lon)) or abs(lat) > mapcells.MAX_LAT or abs(lon) > 180:
            return None, "bad coordinates"
        if pid not in seen:
            seen.add(pid)
            out.append((pid, lat, lon))
    return out, None


# --- MVT: just enough protobuf for a layer's features' tags ----------------------------------------------------
def _varint(b, i):
    r = s = 0
    while True:
        if i >= len(b):
            raise ValueError("truncated varint")
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i
        if s > 63:
            raise ValueError("varint too long")


def _fields(b):
    i, n = 0, len(b)
    while i < n:
        k, i = _varint(b, i)
        f, t = k >> 3, k & 7
        if t == 0:
            v, i = _varint(b, i)
        elif t == 2:
            ln, i = _varint(b, i)
            if i + ln > n:
                raise ValueError("truncated field")
            v, i = b[i:i + ln], i + ln
        elif t == 1:
            v, i = b[i:i + 8], i + 8
        elif t == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"wire type {t}")
        yield f, t, v


def _value(b):
    for f, t, v in _fields(b):
        if f == 1 and t == 2:
            return v.decode("utf-8", "replace")
        if f == 2 and t == 5:
            return struct.unpack("<f", v)[0]
        if f == 3 and t == 1:
            return struct.unpack("<d", v)[0]
        if f in (4, 5) and t == 0:
            return v
        if f == 6 and t == 0:
            return (v >> 1) ^ -(v & 1)
        if f == 7 and t == 0:
            return bool(v)
    return None


def decode_tile(raw, layer=LAYER):
    """{place id: {tag: value}} of the tile's layer (raw as stored: gzip or not; b"" an empty tile)."""
    if not raw:
        return {}
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    out = {}
    for f, t, lb in _fields(raw):
        if f != 3 or t != 2:
            continue
        name, keys, vals, feats = None, [], [], []
        for lf, lt, lv in _fields(lb):
            if lf == 1 and lt == 2:
                name = lv.decode("utf-8", "replace")
            elif lf == 3 and lt == 2:
                keys.append(lv.decode("utf-8", "replace"))
            elif lf == 4 and lt == 2:
                vals.append(_value(lv))
            elif lf == 2 and lt == 2:
                feats.append(lv)
        if name != layer:
            continue
        for fb in feats:
            tags = {}
            for ff, ft, fv in _fields(fb):
                if ff == 2 and ft == 2:
                    j, idx = 0, []
                    while j < len(fv):
                        x, j = _varint(fv, j)
                        idx.append(x)
                    for k in range(0, len(idx) - 1, 2):
                        if idx[k] < len(keys) and idx[k + 1] < len(vals):
                            tags[keys[idx[k]]] = vals[idx[k + 1]]
            pid = tags.get("id")
            if isinstance(pid, str) and pid:
                out[pid] = tags
    return out


# --- a place's tags -> its details -----------------------------------------------------------------------------
def _json(v):
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return json.loads(v)
        except ValueError:
            return None
    return None


def _text(v):
    if not isinstance(v, str):
        return ""
    v = _CTRL.sub(" ", v).strip()
    return v[:TEXT_MAX]


def _strings(v, ok=None):
    got = _json(v)
    if not isinstance(got, list):
        return []
    out = []
    for s in got:
        s = _text(s)
        if s and (ok is None or ok.match(s)) and s not in out:
            out.append(s)
        if len(out) >= LIST_MAX:
            break
    return out


def details(tags):
    """The add-on's view of one place's Overture tags (decode_tile's)."""
    names = _json(tags.get("names")) or {}
    tax = _json(tags.get("taxonomy")) or {}
    brand = _json(tags.get("brand")) or {}
    bnames = brand.get("names") if isinstance(brand.get("names"), dict) else {}
    addr = _json(tags.get("addresses"))
    a = addr[0] if isinstance(addr, list) and addr and isinstance(addr[0], dict) else {}
    srcs = _json(tags.get("sources"))
    sources = []
    for s in srcs if isinstance(srcs, list) else []:
        if not isinstance(s, dict):
            continue
        e = {"dataset": _text(s.get("dataset") or s.get("resource")), "license": _text(s.get("license"))}
        if e["dataset"] and e not in sources:
            sources.append(e)
        if len(sources) >= LIST_MAX:
            break
    try:
        conf = float(tags.get("confidence"))
        conf = round(conf, 3) if math.isfinite(conf) else None
    except (TypeError, ValueError):
        conf = None
    hier = tax.get("hierarchy") if isinstance(tax, dict) else None
    return {
        "id": _text(tags.get("id")),
        "name": _text(names.get("primary") if isinstance(names, dict) else "") or _text(tags.get("@name")),
        "category": _text(tags.get("basic_category")),
        "taxonomy": _text(tax.get("primary") if isinstance(tax, dict) else ""),
        "hierarchy": [_text(h) for h in hier[:6] if _text(h)] if isinstance(hier, list) else [],
        "brand": {"name": _text(bnames.get("primary")), "wikidata": _text(brand.get("wikidata"))},
        "address": {k: _text(a.get(k)) for k in ("freeform", "locality", "postcode", "region", "country")},
        "phones": _strings(tags.get("phones")),
        "websites": _strings(tags.get("websites"), _URL),
        "socials": _strings(tags.get("socials"), _URL),
        "emails": _strings(tags.get("emails"), _EMAIL),
        "open": _text(tags.get("operating_status")),
        "confidence": conf,
        "sources": sources,
    }


# --- the lookup ------------------------------------------------------------------------------------------------
class Lookup:
    """Details from the tiles on disk; tile_bytes(z, x, y) -> bytes as stored, b"" an empty tile, None not on disk
    (pmtiles.Archive.cached of the places archive). release: the archive's Overture release (a str, or a function
    returning it): the kept tiles are keyed by it, so a data update never answers from the last release's tiles."""

    def __init__(self, tile_bytes, kept=TILES_KEPT, release=""):
        self.tile_bytes, self.kept = tile_bytes, kept
        self._release = release if callable(release) else (lambda: release)
        self._tiles = OrderedDict()
        self._lock = threading.Lock()

    def _tile(self, key):
        rel = str(self._release() or "")
        key = (rel,) + tuple(key)
        with self._lock:
            if rel != getattr(self, "_rel", rel):       # a new release: the last one's tiles go at once
                self._tiles.clear()
            self._rel = rel
            got = self._tiles.get(key)
            if got is not None:
                self._tiles.move_to_end(key)
                return got
        raw = self.tile_bytes(*key[1:])
        if raw is None:
            return None
        try:
            got = decode_tile(raw)
        except (ValueError, OSError, EOFError) as e:      # (a damaged tile file: as if missing)
            raise LookupError(f"place tile {key}: {e}") from e
        with self._lock:
            self._tiles[key] = got
            while len(self._tiles) > self.kept:
                self._tiles.popitem(last=False)
        return got

    def find(self, wanted):
        """(details in the order asked, missing ids) for [(id, lat, lon), ...] (request's)."""
        found, missing = [], []
        for pid, lat, lon in wanted:
            c = mapcells.cell_of(lat, lon)
            try:
                tile = self._tile((c.z, c.x, c.y))
            except LookupError:
                tile = None
            tags = tile.get(pid) if tile else None
            if tags is None:
                missing.append(pid)
            else:
                found.append(details(tags))
        return found, missing
