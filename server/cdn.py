"""The project's own tile host (the user, 2026-11-07: "sure lets setup the r2 stuff"): Cloudflare R2 behind its CDN at
CDN_BASE, tile packs (tilepack.py) built by the data repo's GitHub Action. A switch file in the data repo
(SWITCH_URL, cdn.json) turns each dataset on or off for every player (the kill switch: R2 bills per request and has no
spending cap), read at the server's start and every SWITCH_TTL_S; unreachable or off: the original sources as before.

    cdn.json: {"imagery": {"bngs2": {"enabled": true}}}
"""
import json
import logging
import os
import time

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


def chain(names, fetcher, cache_dir):
    """The imagery chain (source names) with the CDN's sources the switch turns on, each where it belongs."""
    names = [n for n in names if n != S2.name]
    on = (switch(fetcher, cache_dir).get("imagery") or {}).get(S2.name) or {}
    if not on.get("enabled") or BEFORE not in names:
        return tuple(names)
    i = names.index(BEFORE)
    log.info("cdn: %s on (%s)", S2.name, CDN_BASE)
    return tuple(names[:i] + [S2.name] + names[i:])
