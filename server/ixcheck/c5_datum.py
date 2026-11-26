"""Check 5, Datum: who moves at each crossing and where the dips are, against the Order it was decided on."""
from ix import artifacts

MOVES = ("up", "down", "none")


def check(art, parents):
    if not isinstance(art, artifacts.Datum):
        return []
    out = []
    o = parents.get(3)
    paths = parents.get(1)
    ms = paths.members if paths is not None else []
    key = lambda i: str(ms[i].key) if 0 <= i < len(ms) else str(i)      # noqa: E731
    if o is not None and getattr(o, "system", None) == art.system:
        # the crossings the moves answer: without the Order the counts and the dips have nothing to be read against
        if len(art.moves) != len(o.crossings):
            out.append(_f(art, "fail", "bad_moves", "%d moves for %d crossings"
                          % (len(art.moves), len(o.crossings))))
        for k, mv in enumerate(art.moves):
            if mv not in MOVES:
                out.append(_f(art, "fail", "bad_moves", "move %r of crossing %d is not one of %s"
                              % (mv, k, "/".join(MOVES))))
        downs = {c.d for c, mv in zip(o.crossings, art.moves) if mv == "down"}
        for d, a, b in art.dips:
            if d not in downs:
                out.append(_f(art, "fail", "bad_dip", "a dip window on %s, the lower member of no down crossing"
                              % key(d), ((d, float(a)), (d, float(b)))))
        crossed = {c.d for c in o.crossings} | {c.u for c in o.crossings}
        for t, a, b in art.tracks:
            if ms and not (0 <= t < len(ms) and ms[t].kind in ("rail", "rail_deck")):
                out.append(_f(art, "fail", "bad_track", "a track window on %s, which is not a track" % key(t),
                              ((t, float(a)), (t, float(b)))))
            elif t not in crossed:
                out.append(_f(art, "fail", "bad_track", "a track window on %s, a member of no crossing" % key(t),
                              ((t, float(a)), (t, float(b)))))
    if not (0 <= art.chosen < len(art.options)):
        out.append(_f(art, "fail", "bad_chosen", "chosen option %d of %d" % (art.chosen, len(art.options))))
    return out


def _f(art, level, code, text, where=()):
    return artifacts.Finding(art.meta.step, level, code, text, where)
