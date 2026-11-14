"""OpenStreetMap's mapped colours of buildings and building parts (building:colour, roof:colour) where Overture drops
them: buildings.tile_buildings fills a building's or a part's facade_color / roof_color from these only when Overture's
is empty (a colour Overture kept wins); the parts take their building's as before (PART_OWN, p_rcol).

Why (the Plaza investigation, 2026-11-14): Overture 2026-10-21.0 empties roof_color / facade_color for the colour names
its table does not have (DarkGreen, beige, lightgray, darkgray, light_gray, lightgrey, gold, lightblue ...): of 14,604
Manhattan buildings and parts matched to OSM by way id, 340 facade and 170 roof colours (the Plaza's green mansard,
City Hall's and the Tweed Courthouse's light grey roofs, the gold tops of New York Life and Met Life, the Empire State
Building's beige). tools/build_osm_building_colours.py reads them from the planet (Layercake's buildings file, which
osmyears reads, has no parts), as RGB, into zoom-12 tiles in PMTiles parts: the same layout, reader and checks as the
construction years' (osmyears.YearShards: the build frozen per process and pinned by its parts' digests and sizes).

A tile's buildings ask for the colour tiles under it, MARGIN_M round it (a building's parts lie in its outline, not in
the tile's box: one across a z12 tile's edge takes its parts' colours from the next tile), on a thread as the tile
starts (Fetch), and wait WAIT_S at most; a row's key is its way's or relation's (2 x id, + 1 for a relation).

The build is the building tiles' key (identity(): MapData.versions(buildings=True), blockworker._md_versions(True)),
"off" / "none" without one, as osmyears'.
- Off (BNE_OSMCOLOURS=off, BNE_RAILTAGS=off), nothing pinned at the process's first ask (not published yet, a first
  run), or a manifest this cannot read: no colours (Overture's alone, as before), nothing faulted.
- A tile that could not be had: no colours, the building tile faulted (revisit.fault: built again later).

    BNE_OSMCOLOURS_BASE   the dataset's folder elsewhere than <BNE_DATA_PAGES>/osmcolours (a staged build)

Data (c) OpenStreetMap contributors, ODbL 1.0.
"""
import logging
import os
import threading

import numpy as np

import fast
import osmyears

log = logging.getLogger("earthd.osmcolours")

ON = os.environ.get("BNE_OSMCOLOURS", "on") != "off"
BASE = os.environ.get("BNE_OSMCOLOURS_BASE")
NATIVE = 301                    # earthfast.VERSION of obc_decode
MARGIN_M = 500.0                # m round a tile its colour tiles are asked for (its buildings' parts reach out of it)

_EMPTY = (np.zeros(0, np.uint64), np.zeros(0, np.int32), np.zeros(0, np.int32))
COLOUR_KEYS = ("facade_color", "roof_color")


class ColourShards(osmyears.YearShards):
    NAME, FORMAT, NATIVE, LABEL = "osmcolours", 1, NATIVE, "osm colours shards"

    def _on(self):
        return ON

    def _base(self):
        return BASE

    def _decode(self, b, m):
        if len(b) > self.MAX_TILE_BYTES:
            raise ValueError(f"tile of {len(b)} bytes")
        return fast._ef.obc_decode(b)


_SHARDS = {}
_LOCK = threading.Lock()


def shards(md):
    """The colour shards of map data md (one per cache folder and fetcher), None when off or md has no fetcher."""
    fetcher = getattr(md, "fetcher", None)
    cache_dir = getattr(md, "cache_dir", None)
    if not ON or fetcher is None or not isinstance(cache_dir, str) or not cache_dir:
        return None
    k = (cache_dir, id(fetcher), BASE)
    with _LOCK:
        got = _SHARDS.get(k)
        if got is None:
            got = _SHARDS[k] = ColourShards(cache_dir, fetcher)
        return got


def identity(md):
    """The building tiles' key for the colours of map data md: "off", "none" (nothing frozen), or the frozen build."""
    return osmyears.identity(md, shards)


key_of = osmyears.key_of


class Fetch(osmyears.Fetch):
    """A tile's colour tiles asked for on a thread as the tile starts; colours() waits WAIT_S at most."""
    EMPTY, LABEL, MARGIN_M = _EMPTY, "osm colours", MARGIN_M

    @staticmethod
    def _shards(md):
        return shards(md)

    def colours(self):
        """(keys, facade, roof: 0xRRGGBB, -1 none) of the tile's buildings and parts, or None (none in use; one not had
        faults the tile)."""
        return self.get()


def hex_of(v):
    return f"#{int(v):06x}"


class Index:
    """The colours of a Fetch (colours()'s; None: none) by source record: {key: (facade, roof)} made at the first ask
    (a tile's ~10k rows in Manhattan: a few ms; then a part's look-up is a dict's)."""

    def __init__(self, colours):
        self.c = colours
        self._by = None

    def _of(self, record):
        if self._by is None:
            k, f, r = (c.tolist() for c in self.c)
            self._by = {kk: (hex_of(ff) if ff >= 0 else None, hex_of(rr) if rr >= 0 else None)
                        for kk, ff, rr in zip(k, f, r)}
        key = key_of(str(record)) if record is not None else None
        return self._by.get(key, (None, None)) if key is not None else (None, None)

    def lookup(self, records):
        """[(facade, roof)] of src_record strings records: "#rrggbb" or None each."""
        if self.c is None:
            return [(None, None)] * len(records)
        return [self._of(r) for r in records]

    def fill(self, attrs, record):
        """attrs' facade_color / roof_color from the record's colours where attrs has none (Overture's empty): attrs
        itself, or a copy with them."""
        if self.c is None or record is None:
            return attrs
        got = self._of(record)
        return attrs if got == (None, None) else fill_from(attrs, got)


def fill_from(attrs, got):
    """attrs with got's (facade, roof) where it has none (a copy when one is filled)."""
    out = attrs
    for k, v in zip(COLOUR_KEYS, got):
        if v is not None and not out.get(k):
            if out is attrs:
                out = dict(attrs)
            out[k] = v
    return out
