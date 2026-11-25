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

With bngs2 on, its second place (imagery "bngs2fine": the same packs enlarged, enlarge_only) goes right after USGS:
the views finer than the packs take them where USGS has nothing, EOX's 2016 mosaic only at sea, past the packs'
latitude and wherever no pack exists (EOX's 2018+ mosaics are CC BY-NC-SA: none in the chain since 2026-11-23).
"fine": false ({"imagery": {"bngs2": {"enabled": true, "fine": false}}}) leaves it out: EOX 2016 draws those views
(blurrier, but none of the 2021 composite's snow and cloud in the Alps and the Arctic, ~/bnge-tests/eoxswap/sheets);
BNGE_S2_FINE=0 / 1 overrides the switch.

"max_lat" (a number: for max_zoom; or {"14": 50, ...} per zoom): the packs of that zoom end at that latitude (the z14
packs are built to 50 degrees: tiles.yml max_lat); farther out bngs2's finest zoom is the next coarser one
(imagery.zoom_caps), as before the switch raised it there.

"bngaer" ({"imagery": {"bngaer": {"enabled": true}}}): the 1 m aerial packs (aerialpacks.py) first in the chain, where
their coverage files list packs; elsewhere the live sources as before, and the mini PC asked to build the place's cell
("request": false: never asked). Left out when this build's Pillow cannot decode AVIF. BNGE_AERIAL=off / on overrides
the switch (tests).

"decisions" ({"decisions": {"enabled": true}}): the shared decision archives (decisions.py: road plans and cell facts
built and signed by the project's builder for each release); BNGE_DECISIONS=on / off overrides the switch.

"lidarz" ({"lidarz": {"enabled": true}}): the lidar road heights per map cell (lidarz.py: what the server's roadz
measures in the USGS 3DEP point clouds, built and signed by the project's builder); BNGE_LIDARZ=on / off overrides
the switch.

"overture" ({"overture": {"release": "2026-12-16.0"}}): the Overture release the map data is read from instead of the
build's own (overture.RELEASE), once the project's pipeline has built its products. Not read here: the server's
supervisor decides it once per app session (overturepick.Pick: listed in Overture's STAC catalogue, a schema this
build reads, one short deadline) and hands it to every server it starts (overturepick.RELEASE_ENV). The switch file
is not signed: the most an edit of it can do is pick a release Overture lists, of a schema this build reads.

"dem" / "bngmt": Mapterhorn's z0-12 elevation tiles repacked (dem.MT_CDN_*, the data repo's dem workflow), the release
the packs were built from; the server's BNGE_DEM_CDN (a release, or "off") overrides it.
"""
import logging
import os
import threading
import time

import aerialpacks as aerialpacksmod
import imagery as imagerymod
import netfetch
import overturepick

log = logging.getLogger("earthd.cdn")

CDN_BASE = imagerymod.CDN_BASE
# (in the data repo itself, read raw: a commit flips it for everyone within ~an hour, no site rebuild)
SWITCH_URL = overturepick.SWITCH_URL     # (BNGE_CDN_SWITCH, else the data repo's cdn.json)
SWITCH_TTL_S = overturepick.SWITCH_TTL_S
CDN_HOST = CDN_BASE.split("://", 1)[-1].split("/", 1)[0]
S2 = imagerymod.SOURCES["bngs2"]
S2_FINE = "bngs2fine"       # its tiles enlarged for the finer views: right after BEFORE
BEFORE = "usgs"             # the chain position: ahead of this source (after the regional and NAIP ones)


_refresh = {"lock": threading.Lock(), "backoff": netfetch.Backoff(SWITCH_TTL_S / 6, SWITCH_TTL_S)}


def switch(fetcher, cache_dir):
    """The switch file's dict (from the net at most every SWITCH_TTL_S, else the copy on disk), {} when unknown.
    An older copy on disk is used when the net fails (stale-if-error: no build faulted, netfetch.stale_ok), and the net
    is asked again only after a backoff (the server's start reads the switch four times: one try, not four)."""
    path = os.path.join(cache_dir, "cdn", "cdn.json")
    try:
        fresh = time.time() - os.path.getmtime(path) < SWITCH_TTL_S
    except OSError:
        fresh = False
    doc = overturepick.read_switch_file(path) if fresh else None    # (past SWITCH_MAX_BYTES: not read)
    if doc is not None:
        return doc
    old = overturepick.read_switch_file(path)
    with _refresh["lock"]:
        backoff = _refresh["backoff"]
        if old is not None and not backoff.due(time.monotonic()):
            return old
        try:
            # (no more than SWITCH_MAX_BYTES read, on the wire and decoded: netfetch.TooLarge)
            with netfetch.stale_ok(old is not None):
                doc = overturepick.get_switch(
                    lambda url: fetcher.get(url, kind="demand", max_bytes=overturepick.SWITCH_MAX_BYTES).body)
            if doc is None:
                raise ValueError(f"not an object of at most {overturepick.SWITCH_MAX_BYTES} bytes")
            overturepick.keep_switch(path, doc)     # (whole, a writer-unique name: the app's pick writes it too)
            backoff.ok()
            return doc
        except Exception as e:          # (offline, not there yet: the copy on disk, else the original sources)
            if old is not None:
                netfetch.refresh_failed(log, backoff, time.monotonic(), "cdn: the switch file", e)
                return old
            log.info("cdn: no switch file (%s: %s): off", type(e).__name__, e)
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
    names = [n for n in names if n not in (S2.name, S2_FINE, AER)]
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
    env = os.environ.get("BNGE_S2_FINE", "").strip()
    fine = env != "0" if env in ("0", "1") else on.get("fine", True) is not False
    return tuple(names[:i] + [S2.name, names[i]] + ([S2_FINE] if fine else []) + names[i + 1:])


def dem_release(fetcher, cache_dir):
    """The Mapterhorn pack release the switch turns on ("dem" -> "bngmt"), or None."""
    on = (switch(fetcher, cache_dir).get("dem") or {}).get("bngmt") or {}
    rel = str(on.get("release") or "").strip()
    if not on.get("enabled") or not rel or "/" in rel or ".." in rel:
        return None
    log.info("cdn: Mapterhorn packs %s on (%s)", rel, CDN_BASE)
    return rel


def decisions_on(fetcher, cache_dir):
    """Does the switch turn the shared decision archives on ({"decisions": {"enabled": true}}: decisions.py)?"""
    on = switch(fetcher, cache_dir).get("decisions") or {}
    got = isinstance(on, dict) and bool(on.get("enabled"))
    if got:
        log.info("cdn: shared decision archives on")
        if on.get("request") is True and "BNGE_DECISIONS_REQUEST" not in os.environ:
            os.environ["BNGE_DECISIONS_REQUEST"] = "on"     # (the builder asked for unbuilt blocks: decisions.ask_build)
    return got


def lidarz_on(fetcher, cache_dir):
    """Does the switch turn the lidar road heights product on ({"lidarz": {"enabled": true}}: lidarz.py)?"""
    on = switch(fetcher, cache_dir).get("lidarz") or {}
    got = isinstance(on, dict) and bool(on.get("enabled"))
    if got:
        log.info("cdn: lidar road heights on")
    return got
