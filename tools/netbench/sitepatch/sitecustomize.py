"""netprobe's servers (and their worker processes: PYTHONPATH carries this folder) lift netfetch's per-host request
limits once it is imported, but only when HTTPS_PROXY is the local cache proxy (proxy.sh env: 127.0.0.1 on
NB_PROXY_PORT); otherwise nothing changes. The proxy sends its misses upstream at each host's own netfetch limits
(cacheproxy.Upstream), so the lift speeds up stored answers only; without it, the regional photo hosts' 6 requests/s
set a place's length even from a warm store (Hartford's ~1,850 Connecticut tiles: ~310 s). The server's own limits are
not part of what is measured: both builds get the same.

Faster hosts change what the server does meanwhile (fewer coarser photos drawn while a regional host lags), the same
for both builds: the comparison holds, the totals are not a live player's.
"""
import importlib.abc
import importlib.util
import os
import sys
from urllib.parse import urlsplit

RATE, BURST, CONC = 200.0, 400.0, 16
LOCAL = ("127.0.0.1", "localhost", "::1")


def behind_local_proxy(env=os.environ):
    """HTTPS_PROXY names the cache proxy proxy.sh runs here (its host and port)."""
    try:
        u = urlsplit(env.get("HTTPS_PROXY") or env.get("https_proxy") or "")
        return u.hostname in LOCAL and u.port == int(env.get("NB_PROXY_PORT") or 8899)
    except ValueError:
        return False


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


if behind_local_proxy():
    sys.meta_path.insert(0, _AfterImport())
