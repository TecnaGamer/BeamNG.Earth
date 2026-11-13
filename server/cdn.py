"""The project's own tile host (the user, 2026-11-07: "sure lets setup the r2 stuff"): Cloudflare R2 behind its CDN at
CDN_BASE, tile packs (tilepack.py) built by the data repo's GitHub Action. A switch file in the data repo
(SWITCH_URL, cdn.json) turns each dataset on or off for every player (the kill switch: R2 bills per request and has no
spending cap), read at the server's start and every SWITCH_TTL_S; unreachable or off: the original sources as before.

    cdn.json: {"imagery": {"bngs2": {"enabled": true}},
               "dem": {"bngmt": {"enabled": true, "release": "0.0.13"}}}
    cdn.json: {"imagery": {"bngs2": {"enabled": true, "max_zoom": 13}}, ...}   # the zoom-13 WebP packs too

"max_zoom" (default 12, the JPEG packs) raises bngs2's finest zoom (imagery.set_s2_max_zoom: max_zoom and
max_view_zoom, in this process and the block workers it spawns): the far rings of that zoom take its packs instead
of EOX's tiles. Read once at the server's start, so a change reaches a player at their next start.

"max_lat" (a number: for max_zoom; or {"14": 50, ...} per zoom): the packs of that zoom end at that latitude (the z14
packs are built to 50 degrees: tiles.yml max_lat); farther out bngs2's finest zoom is the next coarser one
(imagery.zoom_caps), as before the switch raised it there.

"bngaer" ({"imagery": {"bngaer": {"enabled": true}}}): the 1 m aerial packs (aerialpacks.py) first in the chain, where
their coverage files list packs; elsewhere the live sources as before, and the mini PC asked to build the place's cell
("request": false: never asked). Left out when this build's Pillow cannot decode AVIF. BNGE_AERIAL=off / on overrides
the switch (tests).

"dem" / "bngmt": Mapterhorn's z0-12 elevation tiles repacked (dem.MT_CDN_*, the data repo's dem workflow), the release
the packs were built from; the server's BNGE_DEM_CDN (a release, or "off") overrides it.
"""
import json
import logging
import os
import time

import aerialpacks as aerialpacksmod
import imagery as imagerymod

log = logging.getLogger("earthd.cdn")

CDN_BASE = imagerymod.CDN_BASE
# (in the data repo itself, read raw: a commit flips it for everyone within ~an hour, no site rebuild)
SWITCH_URL = os.environ.get("BNGE_CDN_SWITCH",
                            "https://raw.githubusercontent.com/TecnaGamer/BeamNG.Earth-data/main/cdn.json")
SWITCH_TTL_S = 3600.0
CDN_HOST = CDN_BASE.split("://", 1)[-1].split("/", 1)[0]
S2 = imagerymod.SOURCES["bngs2"]
BEFORE = "usgs"             # the chain position: ahead of this source (after the regional and NAIP ones)


def switch(fetcher, cache_dir):
    """The switch file's dict (from the net at most every SWITCH_TTL_S, else the copy on disk), {} when unknown."""
    path = os.path.join(cache_dir, "cdn", "cdn.json")
    try:
        if time.time() - os.path.getmtime(path) < SWITCH_TTL_S:
            with open(path) as fh:
                return json.load(fh)
    except (OSError, ValueError):
        pass
    try:
        body = fetcher.get(SWITCH_URL, kind="demand").body
        doc = json.loads(body)
        if not isinstance(doc, dict):
            raise ValueError("not an object")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as fh:
            json.dump(doc, fh)
        os.replace(path + ".tmp", path)
        return doc
    except Exception as e:          # (offline, not there yet: the original sources)
        log.info("cdn: no switch file (%s: %s): off", type(e).__name__, e)
        try:
            with open(path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}


AER = "bngaer"


def _max_lat(on, max_zoom):
    """The switch's "max_lat" as {zoom: degrees}; None when absent or unreadable."""
    v = on.get("max_lat")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return {int(max_zoom): float(v)}
    if isinstance(v, dict):
        try:
            return {int(k): float(x) for k, x in v.items()}
        except (TypeError, ValueError):
            return None
    return None


def chain(names, fetcher, cache_dir):
    """The imagery chain (source names) with the CDN's sources the switch turns on, each where it belongs."""
    names = [n for n in names if n not in (S2.name, AER)]
    imagery = switch(fetcher, cache_dir).get("imagery") or {}
    aer = imagery.get(AER) or {}
    env = os.environ.get("BNGE_AERIAL", "").strip().lower()
    if env == "on" or (env != "off" and isinstance(aer, dict) and aer.get("enabled")):
        if not aerialpacksmod.avif_ok():
            log.info("cdn: %s left out (this build's Pillow does not decode AVIF)", AER)
        else:
            if isinstance(aer, dict) and aer.get("request") is False:
                os.environ["BNGE_AERIAL_REQUEST_URL"] = "off"      # (this process and the block workers it spawns)
                aerialpacksmod.REQUEST_URL = "off"
            log.info("cdn: %s on (%s)", AER, imagerymod.AERIAL_BASE)
            names = [AER] + names
    on = imagery.get(S2.name) or {}
    if not isinstance(on, dict) or not on.get("enabled") or BEFORE not in names:
        return tuple(names)
    i = names.index(BEFORE)
    # (no "max_zoom": as the environment says, BNGE_S2_MAX_ZOOM, else 12: a test of the next zoom before the switch)
    src = (imagerymod.set_s2_max_zoom(on["max_zoom"], _max_lat(on, on["max_zoom"])) if "max_zoom" in on
           else imagerymod.SOURCES[S2.name])
    log.info("cdn: %s on (%s, to zoom %d%s)", S2.name, CDN_BASE, src.max_zoom,
             "".join(f", z{z} to {lim:g} deg" for z, lim in src.zoom_max_lat))
    return tuple(names[:i] + [S2.name] + names[i:])


def dem_release(fetcher, cache_dir):
    """The Mapterhorn pack release the switch turns on ("dem" -> "bngmt"), or None."""
    on = (switch(fetcher, cache_dir).get("dem") or {}).get("bngmt") or {}
    rel = str(on.get("release") or "").strip()
    if not on.get("enabled") or not rel or "/" in rel or ".." in rel:
        return None
    log.info("cdn: Mapterhorn packs %s on (%s)", rel, CDN_BASE)
    return rel
