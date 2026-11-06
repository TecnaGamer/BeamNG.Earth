"""Opt-in tower evidence: BNGE_ANTENNA_DEBUG=*, OSM ID or source record.

Unset by default. Cached content does not regenerate merely to produce a log.
This reports source/placement evidence, never an assertion about in-game output.
"""
import json
import logging
import os
import re

import numpy as np

log = logging.getLogger("earthd.antennas.debug")
SELECTORS = frozenset(x.strip() for x in os.environ.get("BNGE_ANTENNA_DEBUG", "").split(",") if x.strip())
ENABLED = bool(SELECTORS)


def _safe(value):
    if isinstance(value, np.ndarray):
        return _safe(value.tolist())
    if isinstance(value, np.generic):
        return _safe(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe(v) for v in value]
    return value


def selected(record):
    if not ENABLED:
        return False
    if "*" in SELECTORS:
        return True
    ids = [str(record.get(k, "")) for k in ("feature_id", "source_record")]
    ids.extend(str(value) for value in record.get("osm_ids", ()))
    candidates = set(ids)
    for value in ids:
        numbers = re.findall(r"\d{4,}", value)
        candidates.update(numbers)
        candidates.update(str(int(number)) for number in numbers)
        candidates.add(re.sub(r"@\d+$", "", value))
    return bool(candidates & SELECTORS)


def antenna(record):
    if selected(record):
        log.info("%s", json.dumps(_safe(dict(record, system="antenna", schema=1)),
                                 sort_keys=True, separators=(",", ":"), allow_nan=False))
