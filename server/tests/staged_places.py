"""The places the staged solver is run on (server/staged.py), each a Place: members, meta (what the map has of each road by Earth
id), ll (x, y -> lat, lon), edge, ground (staged_fixtures.Ground), today (today's find_systems answer on the same members).

    synthetic(members, ground=None)   hand-made members on flat ground (the unit tests')
    replay(NAME)                      a tests/interchanges.py replay planned as the server plans it (Popoli, the A20, ...)
    truth(NAME)                       a Swiss survey place (tests/truth.py), its ways as the members the survey's facts give
    region(NAME)                      staged_fixtures.region: a Shanghai region read back from its dump (None where it is not there)
Offline: no downloads (the data is tests/fixtures and the eval's cache)."""
import os
import sys
from types import SimpleNamespace

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import gradeshapes as gs  # noqa: E402
from staged_fixtures import Ground, region  # noqa: E402,F401


def _flat(xy):
    return np.zeros(len(np.atleast_2d(xy)))


def synthetic(members, ground=None, meta=None, name="synthetic"):
    """A Place for hand-made members (flat ground unless ground(xy (k, 2)) -> z is given): today's find_systems run on them as the
    pipeline runs it."""
    members = list(members)
    net = gs.Net(members)
    g = Ground(net, func=ground or _flat)
    today = gs.find_systems(members, lambda n_: (g.xy, g.fixed))[1]
    return SimpleNamespace(name=name, net=net, members=members, meta=meta or {}, ll=None, edge=None, ground=g, today=today,
                           trace=None, audits=None, home=None)


def replay(name):
    """A replay of tests/interchanges.py (fixtures/interchange_NAME.npz) planned as the server plans it with every solve replaced
    by the audit's capture: the members the planner finds, what the map has of each road, the ground along them, today's systems."""
    from unittest import mock
    import interchange_audit_run as run
    import interchanges
    import mapcells
    import revisit
    rep = interchanges.load(name)
    with mock.patch.object(revisit, "ON", False):
        planner = run.planner_of(rep)
        home = mapcells.cell_of(*rep.origin)
        net, systems, audits, namer, trace = run.capture(planner, home)
    for s in systems:
        s["digs"] = set(s["digs"])
    ground = Ground(net, func=planner._ground_line)
    meta = {}
    for m in net.m:
        e = str(m.key[0] if isinstance(m.key, tuple) else m.key)
        if e not in meta:
            meta[e] = namer.meta.get(e)
    return SimpleNamespace(name=name, net=net, members=list(net.m), meta=meta, ll=run.frame_ll(rep.frame), edge=namer.edge, ground=ground,
                           today=systems, trace=trace, audits=audits, home=None, endinfo=namer.endinfo)


def truth(name):
    """A Swiss survey place (tests/truth.py): its ways as the members the survey's facts give, on Earth's ground there, a surveyed
    track at its surveyed height."""
    import interchange_audit_run as run
    import truth as tr
    site = tr.load(name)
    mem, ws = tr.members(site)
    rep = tr.replay(name)
    gfn = tr.ground_fn(rep, ws)
    net = gs.Net(mem)

    def fixed(i, s):
        sv = np.atleast_1d(np.asarray(s, np.float64))
        if ws[i].layer == "rail":
            z = np.interp(sv, tr.arclen(ws[i].xyz), ws[i].xyz[:, 2])
        else:
            z = gfn(np.stack([np.interp(sv, net.s[i], net.lines[i][:, 0]), np.interp(sv, net.s[i], net.lines[i][:, 1])], axis=1))
        return np.asarray(z, np.float64) if np.ndim(s) else float(z[0])
    g = Ground(net, func=gfn, fixed_func=fixed)
    today = gs.find_systems(mem, lambda n_: (g.xy, g.fixed))[1]
    meta = {ln.id: dict(name=ln.objektart, layer=str(ln.level)) for ln in ws}
    half = float(site.half)
    mine = {id(ln) for ln in ws}
    others = [ln for ln in site.lines if id(ln) not in mine]        # (the survey's paths and what is no way: not members)

    def edge(xy):
        """The survey's own box: a way that ends at it goes on outside it."""
        return max(abs(float(xy[0])), abs(float(xy[1]))) >= half - run.SURVEY_EDGE_M

    def endinfo(i, e):
        """What the survey has at a way's end: a path or a line that is no way ending (or running) there is a road the shapes do not
        take; nothing else (tests/interchange_audit_run.truth_site)."""
        p = ws[i].xyz[0 if e == 0 else -1]
        near = [(str(o.id), o.objektart, str(o.level), None, False, 0, {}) for o in others
                if float(np.sqrt(np.sum((o.xyz[:, :2] - p[:2]) ** 2, axis=1)).min()) <= run.SURVEY_NODE_M]
        return dict(near=[], inside=[], others=near, connector=str(tr.node_of(p)), feature_end=e, own_gap=None, own_next=None)
    return SimpleNamespace(name=name, net=net, members=list(mem), meta=meta, ll=run.frame_ll(rep.frame), edge=edge, ground=g,
                           today=today, trace=None, audits=None, home=None, endinfo=endinfo)
