"""Step 8 of the 3D solve: the heights as the build reads them.

    profiles(network, samples, z) -> {fid: [(xy (k, 2), z (k,), mode)]}

The grader's _Planner._shape_region form, read by every consumer of the interchange solve today (_shape_at): the decks
(roads.DECK_Z_HOOK), the tunnels' floors (roads.TUNNEL_Z_HOOK), the tracks' beds (railways.TRACK_Z_HOOK), the road plans
and through them the terrain's fills and cuts (grading._apply_shapes). One piece per member, from its own start to its
own end (the path's samples on it, its two ends' heights interpolated along the path: _shape_at drops a point more than
0.5 m past a piece's end):

- a deck: "deck" under its road's feature id;
- a tunnel: "cut" (the tube's floor);
- a road on the ground: "set" (the plan takes the solve's height: the solve is the road's profile, its fill and cut);
- a track and a track's deck: "track" under "rail:<way>", the bed the railways draw on.

The heights are the solve's, in the frame the ground was read in (the planner's)."""
import numpy as np

from . import network as _network

MODE = {"deck": "deck", "tunnel": "cut", "ground": "set", "rail": "track", "rail_deck": "track"}


def fid_of(m):
    """The feature id the build asks member m's heights under: a road's (its lane section's) id, "rail:<way>" for a
    track."""
    fam, way = _network.way_of(m)
    if fam == "rail":
        return "rail:" + way
    return str(m.key[0] if isinstance(m.key, tuple) else m.key)


def pieces(network, samples, z):
    """[(member, xy (k, 2), z (k,))]: each member's stretch of its path, its own ends included, in its own direction."""
    out = []
    net = network.net
    for p, path in enumerate(network.paths):
        sl = samples.of(p)
        s, zp, xyp = samples.s[sl], np.asarray(z[sl], np.float64), samples.xy[sl]
        for k, (i, forward) in enumerate(path.parts):
            line = net.lines[i]
            if len(line) < 2:
                continue
            a, b = float(path.start[k]), float(path.start[k] + net.L[i])
            inner = (s > a + 1e-6) & (s < b - 1e-6)
            ends = (line[0], line[-1]) if forward else (line[-1], line[0])
            xy = np.concatenate([ends[0][None], xyp[inner], ends[1][None]])
            zz = np.concatenate([[np.interp(a, s, zp)], zp[inner], [np.interp(b, s, zp)]])
            if not forward:
                xy, zz = xy[::-1], zz[::-1]
            out.append((i, np.ascontiguousarray(xy), np.ascontiguousarray(zz)))
    return out


def profiles(network, samples, z):
    """{fid: [(xy, z, mode)]} of the solve's heights z (step 7's, per sample)."""
    prof = {}
    for i, xy, zz in pieces(network, samples, z):
        m = network.members[i]
        prof.setdefault(fid_of(m), []).append((xy, zz, MODE[m.kind]))
    return prof


def summary(prof):
    """Counts of the profiles by mode, and their points."""
    out = {}
    for lst in prof.values():
        for xy, _z, mode in lst:
            out[mode] = out.get(mode, 0) + 1
            out["points"] = out.get("points", 0) + len(xy)
    out["fids"] = len(prof)
    return out
