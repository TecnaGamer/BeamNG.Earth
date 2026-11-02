#!/usr/bin/env python3
"""Reproduce two road-audit cache findings without network or cache-file I/O.

Prints JSON; this is a diagnostic, not an assertion that the current behaviour
is desirable. Separate fixes should make stale stages empty and decoded cells
shared. Run in a dedicated process because the worker singleton is replaced.
"""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import blockworker
import geotiles
import mapcells
import mapdata
import mapgeom
import revisit


def main():
    gx, gy = geotiles.tile_of(-36.85, 174.76)
    with patch.object(blockworker, "_s", SimpleNamespace(cache_dir="unused")), \
         patch.object(blockworker, "_geo_content_key", return_value="unused"), \
         patch.object(revisit, "load", return_value=({}, {})):
        blockworker._tile_stages.last = {"old_tile_work": 12.5}
        blockworker._geo_content(gx, gy, 15, "unused", "/temp/audit", True, True,
                                 False, False, 0, None, "NZ")
        stale = blockworker.last_tile_stages()

    # Substitute the I/O boundary only. Exercise cell()'s actual decode/cache
    # path with both readers known to miss memory before either finishes.
    md = object.__new__(mapdata.MapData)
    md._mem, md._mem_lock, md._mem_bytes = OrderedDict(), threading.Lock(), 0
    md.versions = lambda: {}
    md._rails = md._lanes = lambda *args: None
    barrier = threading.Barrier(2)
    arrays = mapgeom.empty("roads").to_arrays("")
    arrays["provenance"] = "{}"

    def layer_arrays(*args):
        barrier.wait(timeout=5)
        return arrays, True

    md.layer_arrays = layer_arrays
    with ThreadPoolExecutor(2) as md.pool, ThreadPoolExecutor(2) as callers:
        fs = [callers.submit(md.cell, mapcells.CellKey(14, 16145, 9998), ("roads",)) for _ in range(2)]
        cells = [f.result() for f in fs]
    print(json.dumps({"cache_hit_stages": stale,
                      "concurrent_cell_reads_share_features": cells[0].layers["roads"] is cells[1].layers["roads"]},
                     indent=2))


if __name__ == "__main__":
    main()
