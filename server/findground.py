"""Find the ground: which way is on it where two cross, and which one leaves it.

The user, 2026-11-17, over Popoli's SS17 in its trench under the Roma - Pescara line and the A25's viaduct over the
same road 800 m north: "more than a who moves I see this as not pushing above ground something layered above a tunnel,
or the 'find ground' rules. anything else being a tunnel or a overpass depends on it."

0. A way with no bridge or tunnel tag is the ground: its height is the elevation's.
1. A structure has a natural height: the line between the ground at the two ends of its whole chain (its pieces joined
   end to end, or at a switch, are one structure).
2. Where two ways cross and their natural heights already leave the clearance, nothing moves.
3. Else exactly one of them leaves the ground, by what is lacking (leaves): a tunnel under anything goes down, and what
   is over it keeps its natural height, a bridge too; else a bridge over a plain way goes up, and the plain way stays;
   two plain ways meet at grade.
4. The move is the structure's own, carried by it and the approaches joined to it, never by the other way.

Measured on the survey's 311 crossings round five places in Switzerland (tests/truth.py), each way's real height
against the elevation at the crossing: the plain way under a bridge within 1 m of it at 226 of 226 (the bridge 6 to
12 m up), the plain way over a tunnel at 59 of 60; a tunnel under a bridge at its ground and the bridge 5 to 16 m up
at 9 of 9 (rule 2). Popoli: the SS17's own 19 m tunnel under a 30 m rail bridge whose ends stand at the road's ground:
the road goes down, both tracks stay; the same road, plain, under the A25's viaduct 8 to 11 m over it: nothing moves.

Pure. The railways' beds (railways.profile), the shapes (gradeshapes._system) and the grading's underpasses
(grading._lower_underpasses) read it."""
GROUND, BRIDGE, TUNNEL = "ground", "bridge", "tunnel"


def leaves(lower, upper):
    """Which of two ways leaves the ground where upper crosses over lower (each GROUND, BRIDGE or TUNNEL) and their
    natural heights lack the clearance: "lower" (it goes down), "upper" (it goes up) or None (they meet at grade)."""
    if lower == TUNNEL:
        return "lower"
    if upper == BRIDGE:
        return "upper"
    return None
