"""A cell's eight neighbour cells' roads layers, for what reads past its own cell: lane flow, a road's home cell, the
regional classes (MapData.cell hooks a cell's roads layer with neighbours_hook as "_bnge_neighbours")."""
import threading
import weakref

import mapcells

_POOL = None
_POOL_LOCK = threading.Lock()


def neighbours_hook(md, key):
    """The callable a cell's roads layer carries (roads.lane_flow): the roads layers of the eight cells round it
    (_neighbours), held weakly: handed back while the map data (or a caller) still has them, asked again once gone; a
    cell without one is not asked again. A plain list made every layer keep its neighbours alive, and those theirs, so
    no roads layer a server process decoded was ever freed by MapData's memory cap (GitHub #893: 270 cells driven three
    times at MEM_MB 192, 647 layers alive for the 175 it kept and 500 MB of private memory, still growing; now 180 for
    180 and 241 MB, flat, in the same time)."""
    lock = threading.Lock()
    keys = _neighbour_keys(key)
    names = [str(k) for k in keys]
    refs = [_UNREAD] * len(keys)    # per key: a weak reference to its roads layer, None (none there) or _UNREAD

    def run(want=None):
        """The eight layers (None where a cell has none); want: only the cells with these names are read (the others
        None unless at hand already): a caller that needs a few of them (a road's home cell, the cells a road
        crosses) does not have the rest decoded, and theirs (2026-11-11: most of a cold first block's halo)."""
        with lock:
            got = [r() if r is not None and r is not _UNREAD else None for r in refs]
            todo = [i for i, r in enumerate(refs) if (r is _UNREAD or (r is not None and got[i] is None))
                    and (want is None or names[i] in want)]
            if todo:
                for i, f in zip(todo, _neighbours(md, key, [keys[i] for i in todo])):
                    got[i] = f
                    refs[i] = weakref.ref(f) if f is not None else None
            return got
    run.names = names
    return run


_UNREAD = object()


def _neighbour_keys(key):
    """The keys of the eight cells round key (fewer at the map's top and bottom rows)."""
    return [mapcells.CellKey(key.z, (key.x + dx) % key.n, key.y + dy)
            for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dx or dy) and 0 <= key.y + dy < key.n]


def _neighbours(md, key, keys=None):
    """The roads layers of the eight cells round key (or of keys), asked at once (one after another, cold, they held a
    fresh place's first road tile ~7 s: 2026-10-27 A/B)."""
    global _POOL
    keys = _neighbour_keys(key) if keys is None else keys
    with _POOL_LOCK:
        if _POOL is None:
            from runtimeprofiler import ThreadPoolExecutor
            _POOL = ThreadPoolExecutor(8, thread_name_prefix="neighbour-cells")
    futs = [_POOL.submit(lambda k: md.cell(k, layers=("roads",), kind="demand").layers.get("roads"), k) for k in keys]
    return [f.result() for f in futs]
