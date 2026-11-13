"""netprobe's servers (and their worker processes: PYTHONPATH carries this folder) lift netfetch's per-host request
limits once it is imported: every host answers from the cache proxy, which keeps its own limit for what goes upstream
(cacheproxy.UPSTREAM_CONC). The regional photo hosts' 6 requests/s set a cold place's length (Hartford's ~1,850
Connecticut tiles: ~310 s). The server's own limits are not part of what is measured: both builds get the same.

Faster hosts change what the server does meanwhile (fewer coarser photos drawn while a regional host lags), the same
for both builds: the comparison holds, the totals are not a live player's.
"""
import importlib.abc
import importlib.util
import sys

RATE, BURST, CONC = 200.0, 400.0, 16


def _lift(netfetch):
    from dataclasses import replace
    for host, p in list(netfetch.POLICIES.items()):
        if p.denied:
            continue
        netfetch.POLICIES[host] = replace(p, rate=max(p.rate, RATE), burst=max(p.burst, BURST),
                                          max_conc=max(p.max_conc, CONC), daily_budget=None, daily_hard=None)


class _AfterImport(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "netfetch":
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            return spec
        run = spec.loader.exec_module

        def exec_module(module):
            run(module)
            _lift(module)
        spec.loader.exec_module = exec_module
        return spec


sys.meta_path.insert(0, _AfterImport())
