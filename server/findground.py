"""Find the ground: which way is on it where two cross, and which one leaves it.

The user, 2026-11-17, over Popoli's SS17 in its trench under the Roma - Pescara line and the A25's viaduct over the
same road 800 m north: "more than a who moves I see this as not pushing above ground something layered above a tunnel,
or the 'find ground' rules. anything else being a tunnel or a overpass depends on it."

0. A way with no bridge or tunnel tag is the ground: its height is the elevation's.
1. A structure has a natural height: the line between the ground at the two ends of its whole chain (its pieces joined
   end to end, or at a switch, are one structure).
2. Where two ways cross and their natural heights already leave the clearance, nothing moves.
3. Else exactly one of them leaves the ground by the lack: a tunnel under anything goes down, and what is over it
   keeps its natural height, a bridge too; two plain ways meet at grade; where a bridge crosses a plain way, the one
   that moves is the one that costs less earth, each by its own grade law (leaves): the bridge climbs the lack at its
   grade over its own run first (its length to the nearer end of its structure, every metre of it free: piers, no
   earth) and only what that does not reach is an approach fill; the plain way dips the lack and climbs out of it at
   its grade, a cut each side. A long bridge's own run carries the whole rise (the bridge goes up and the way stays,
   Duttweiler); a short one over a minor road leaves a longer fill than the road's cut, and the road goes down (a 70 m
   motorway bridge over a local road on flat ground, the I-20; a 12 m street bridge over a street); a track as a way
   under is never dug where the bridge over it climbs as steeply as a track does (its cut is the longest).
4. The move is the structure's own, carried by it and the approaches joined to it, never by the other way.

Measured on the survey's 311 crossings round five places in Switzerland (tests/truth.py), each way's real height
against the elevation at the crossing: the plain way under a bridge within 1 m of it at 226 of 226 (the bridge 6 to
12 m up), the plain way over a tunnel at 59 of 60; a tunnel under a bridge at its ground and the bridge 5 to 16 m up
at 9 of 9 (rule 2). Popoli: the SS17's own 19 m tunnel under a 30 m rail bridge whose ends stand at the road's ground:
the road goes down, both tracks stay; the same road, plain, under the A25's viaduct 8 to 11 m over it: nothing moves.

The cost law holds on all 156 of the survey's crossings where the line between a structure's ends does not clear the
way under it: every one is the bridge up and the way on its ground, the smallest margin 5 m of ramp (tests/truth.py).

Pure. The railways' beds (railways.profile), the shapes (gradeshapes._system) and the grading's underpasses
(grading._lower_underpasses) read it."""
GROUND, BRIDGE, TUNNEL = "ground", "bridge", "tunnel"


def fill_run(lack, ends, grade):
    """m of approach fill the bridge's rise of lack m needs at grade, over the run of its own it has to each of its ends
    (ends: m along the structure from the crossing to its two ends, None or inf: no end that way): what the run does not
    carry (lack / grade of ramp, less the run itself, each side)."""
    run = lack / grade
    return sum(max(0.0, run - e) for e in ends)


def dip_run(lack, grade):
    """m of cut the plain way's dip of lack m needs at its grade: down and back up, a ramp each side."""
    return 2.0 * lack / grade


def leaves(lower, upper, lack=None, ends=None, up_grade=None, down_grade=None, fills=True):
    """Which of two ways leaves the ground where upper crosses over lower (each GROUND, BRIDGE or TUNNEL) and their
    natural heights lack the clearance: "lower" (it goes down), "upper" (it goes up) or None (they meet at grade).

    A bridge over a plain way (GROUND) is asked the cost: lack m the natural heights fall short by, ends the m along
    the bridge's structure from the crossing to each of its two ends (inf where it goes on), up_grade the grade the
    bridge climbs (its deck's law, a track's), down_grade the grade the plain way dips and climbs at. Without them
    (the lack not known) the bridge goes up (rule 3 as it stood). Nothing lacks (lack <= 0): the bridge, nothing moves. fills False: the
    approaches cannot be filled (a deck the caller never raises on approaches: the legacy grading's motorways and trunk
    roads): the bridge goes up only where its own run carries the whole rise, else the way goes down."""
    if lower == TUNNEL:
        return "lower"
    if upper != BRIDGE:
        return None
    if lower != GROUND or lack is None or lack <= 0.0 or ends is None or not up_grade or not down_grade:
        return "upper"
    fill = fill_run(lack, ends, up_grade)
    if not fills:
        return "lower" if fill > 0.0 else "upper"
    return "lower" if fill > dip_run(lack, down_grade) else "upper"
