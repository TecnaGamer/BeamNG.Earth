"""Step 5 (SPEC.md): who moves at each crossing, the dip windows and the decks held, as the Datum artifact. Today's
verdicts came with the system (gradeshapes' _Verdicts through find_systems); the artifact holds them as the one option
that was weighed and taken."""
from . import artifacts, cache

FORMAT = 2
LAWS = (("gradeshapes", ["DIP_MARGIN_M", "STEP_M", "W_DIP", "W_HOLD"]),)
LAWS_DIGEST = cache.laws_digest(*LAWS)
STEP = 5


def run(planner, net, sysm, loc, system, scope):
    """The Datum artifact of one system: today's verdicts (moves, dips, the held decks) as one option, the members
    numbered as the system's Paths' (loc)."""
    moves = tuple(str(c["moves"]) for c in sysm["crossings"])
    dips = tuple((loc[int(d)], float(a), float(b)) for d, a, b in sysm["dips"])
    held = tuple(sorted(loc[int(i)] for i in sysm["seeds"] if getattr(net.m[i], "held", False)))
    option = artifacts.Option(moves, None, None, "today's verdicts")
    return artifacts.Datum(artifacts.Meta(STEP, FORMAT, "", (), LAWS_DIGEST, scope), system, moves, dips, held,
                           [option])
