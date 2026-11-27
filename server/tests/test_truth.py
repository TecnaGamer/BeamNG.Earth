"""Earth's level decisions and solved shapes against the measured shape of real junctions (tests/truth.py: swisstopo's
3D lines round five places in Switzerland, and Earth's own map and elevation there, both offline).

One direction at a time, each in seconds and with no build:
  1. the survey alone: its levels order every real crossing (what a level rule can rest on);
  2. the heuristics alone: gradeshapes' order and joins given the survey's own kinds and levels;
  3. the solver alone: the shapes it solves from those true facts on Earth's ground, against the real heights;
  4. Earth's reading of the map: the order its own members give the real crossings (and which it cannot give: a way with
     no member along it, or one the map has and Earth builds nothing of: paths, street trams, roads under construction);
  5. the laws against the real roads: the clearance and the grade they ask, and what was built;
  6. what Earth builds: its graded ground under the survey's roads on the ground (a road dug or filled where the real
     one lies on its ground), and its railway bed against the survey's tracks (minutes: the place graded whole).

Each measure has two tests: no worse than TODAY (what holds now: lower a number when a change earns it), and its
goal, an expected failure until it is met (strict: the day it passes, the mark comes off and TODAY's number goes)."""
import functools

import numpy as np
import pytest

import grading
import truth

SURVEY_M = 1.5          # m: the survey's own accuracy (0.2-1.5 m): a shape nearer the real road than this is right
# what holds today, per place
TODAY = {
    "zurich_brunau": dict(
        unmatched=8,            # real crossings with no member of Earth's along one of the two (not judged; 12 before): Earth's
                                # line ends or turns off within 20 m of the crossing (5), one line along both ways (2), a
                                # motorway tunnel the shapes leave out: a feature that is all tunnel (1)
        not_built=3,            # (the way is a path in the map: Earth builds none)
        shape_p95=4.9,          # m: the Sihl viaduct's decks, solved up to 6.3 m off the real ones (was 7.9)
        under_laws=2, steeper=2,
        dug=(9, 8),             # per mille of the ground roads' points, 3.3 m at the deepest. Without the shapes 9 (was 7): a road
                                # crossing under a long deck near its end dips by the deck's climb from that end (grading.UNDER_LONG_ON,
                                # BNE_UNDERPASS_LONG: the deck there is only as high as it climbs, trucks stood in it); with them 8
        filled=(14, 14),        # 2.3 m at most (was 6.5): the motorway at its tunnels' mouths. The mouths are right (floor
                                # 449.4 / 436.3 against the survey's 450.6 / 436); the roads beside the tunnel's low arch were
                                # lifted to its cover (5.8 m) until the cover was asked over the tube's own footprint
        rail_low=None, rail_high=None),     # (no railway bridge here)
    "zurich_europabruecke": dict(
        unmatched=5,            # (22 before: 9 were the tracks over a pedestrian underpass, a path now, as the map has it)
                                # Earth's line ends within 20 m of the crossing (2), one line along both ways (2), two lines
                                # along the bridge, one of them its deck, which reads right (1)
        not_built=5,            # 4 street trams on the ground, 1 a path under a bridge
        shape_p95=4.3,          # m: the roads under the tracks 1-2 m deeper (the dips 4.2 m off, the decks 3.1: was 5.7)
        under_laws=0, steeper=2,
        dug=(0, 0), filled=(0, 6),      # (7 before findground's cost law: a bridge's road under it dips where that costs less earth than the deck's rise; with the shapes 5 before the decks kept their line: a deck's end 1.9 m over its road)
        rail_low=-2.6,          # m: the Letzigraben flyover's ramp. It is 13 m over the tracks it crosses 270 m on, which
        rail_high=-0.4),        # Earth's clears by a track's headroom, and climbs to them at 4 %: the one law railways.GRADE
        #                         (5 %, the user, 2026-11-18) stands it -2.6 .. -0.4 under the real ramp, median -1.3 (it was
        #                         +2.5 .. +3.1 over, median +3.0, at 2 %: 3 m over the real one's climb; at 2.5 %: +0.7 .. +0.9,
        #                         at 2.8 %: -0.4 .. -0.1). The low end is the ramp's foot, flat in Earth's bed (0.1 %) where the
        #                         real one climbs 2.6 %
    "zurich_duttweilerbruecke": dict(
        unmatched=3,            # (22 before): Earth's rail ends at the crossing (1), one rail deck along both ways (2)
        not_built=14,           # 8 street trams on the ground, 6 paths under or on the bridges: Earth builds none
        shape_p95=2.3,          # m: a service ramp's deck (was 3.4)
        under_laws=2, steeper=1,
        dug=(6, 6),             # 3.3 m at the deepest. (12 while every rail bridge dug the road under it; 3 before findground's
                                # cost law: one rail bridge 12 m from its structure's end, 5.2 m short of clearing a service
                                # road there, is judged by the law (a rise of 91 m of fill against a dip of 61 m), the road
                                # dipping 1 m or less along the service roads that meet it)
        filled=(0, 0),
        rail_low=-11.6,         # m: the Wipkingen viaduct, 13 m over its streets, stands 10 to 13 m up (was at their
        rail_high=-0.5),        # level at its joints, -13.2); the Letzigraben flyover is 9 m short of the survey's 18 m
        #                         over the tracks it crosses: no bridge crosses another here, in the map or the survey
        #                         (the flyover, level 3 in the survey, crosses tracks on the ground, which the law asks
        #                         6.5 m over, and one road bridge, 6.6 m under it), so the 11 m it climbs over the
        #                         chord between its ends are no crossing's (measured 2026-11-18). The railway's one grade
        #                         law, railways.GRADE: 2 % made this -8.6 (2.5 % -9.2, 2.8 % -9.6), 5 % -11.6: a deck
        #                         stands as high as its crossings ask and its ramps fall away at the law's grade, which the
        #                         survey's viaducts do at 1.5-2.7 % (the flyover's 4.1 % is one ramp of 100 m), so the
        #                         steeper the law the further under the real decks that stand higher than they must
        #                         (-8.8 before a crossover of the viaduct, 6 m under it at both ends, joined it: -8.6)
    "goeschenen_gotthard_portal": dict(
        unmatched=1,            # (5 before): a tertiary road ends at the crossing
        not_built=3,            # the second tube of the road tunnel: the map flags it as under construction
        shape_p95=2.7,          # m: the links' decks (was 5.0)
        under_laws=0, steeper=1, graded=60,
        dug=(51, 49),           # a 6 m road 5 m, the exit ramp 2.3 m (its own plan under the DEM). Was 66 / 64 with a link road 7.3 m
                                # under its survey height beside the ramp in its cutting: the ramp's lanes reached on past
                                # its wall's face over the link's (a wall holds the link in the real place: grading.level_walls)
        filled=(5, 0),          # 1.8 m at most (was 8.3, 76 per mille; 0.9 m with the shapes): the road into the Gotthard tunnel, its mouth read on the face
        rail_low=-14.9,         # m (-9.7 at 2 %): the short bridges over the gorge stand 15 m over the elevation at their ends (the
        #                         real line on its terrace, the DEM below it) and Earth's bed there, 7 m over its ground at 2 %
        #                         (the lift run out from the decks round it), is 1 m over it at 5 %
        rail_high=0.1),
    "kempttal_viaduct": dict(
        unmatched=0, not_built=0, right=6,
        shape_p95=0.9,          # m: the viaduct's deck (was 4.6 at its ends)
        under_laws=0, steeper=0, graded=60,
        dug=(0, 0), filled=(0, 0), rail_low=-1.5, rail_high=0.1),     # (-1.0 at 2 %; dug 1 before the 1.1 m dig
        #                                                                 at the viaduct went with the upstream merge)
}
RAIL_M = 2.0            # m: a railway bridge's bed nearer the real track than this is right (what a road under it needs)
MODE = int(grading.SHAPES_ON)       # (dug, filled: without the shapes, with them (BNE_SHAPES=1): what Earth builds differs)
SITES = sorted(TODAY)


@functools.lru_cache(maxsize=None)
def measured(name):
    return truth.report(name)


def _goal(names, reason):
    """The sites as parameters, those in names expected to fail (strictly) for reason."""
    return [pytest.param(s, marks=pytest.mark.xfail(strict=True, reason=reason)) if s in names else s for s in SITES]


@pytest.mark.parametrize("site", SITES)
def test_the_surveys_levels_alone_order_every_real_crossing(site):
    assert truth.levels_wrong(site) == [], "(a bridge's or a tunnel's level says which line is over which, everywhere)"


@pytest.mark.parametrize("site", SITES)
def test_the_order_given_the_true_kinds_and_levels_is_the_real_one(site):
    # (Brunau's 40 roads over the Zimmerberg railway tunnel were at grade: a track's level was never read)
    assert truth.over_wrong(site) == []


def _shape_p95(site):
    return max(v[1] for v in measured(site)["solved_minus_real"].values())


@pytest.mark.parametrize("site", SITES)
def test_earth_digs_and_fills_no_more_of_the_real_roads_on_the_ground_than_today(site):
    dug, filled, deepest, tallest = truth.moved(site)
    assert dug <= TODAY[site]["dug"][MODE], f"per mille dug {truth.MOVED_M} m or more, the deepest {deepest} m"
    assert filled <= TODAY[site]["filled"][MODE], f"per mille filled {truth.MOVED_M} m or more, the tallest {tallest} m"


@pytest.mark.parametrize("site", _goal({s for s in SITES if TODAY[s]["dug"][MODE]},
                                          "roads on their ground dug under bridges that stand high over them"))
def test_earth_digs_no_real_road_that_lies_on_its_ground(site):
    assert truth.moved(site)[0] == 0, truth.moved(site)


@pytest.mark.parametrize("site", _goal({s for s in SITES if TODAY[s]["filled"][MODE]},
                                          "roads on their ground filled at tunnels' mouths and beside higher roads"))
def test_earth_fills_no_real_road_that_lies_on_its_ground(site):
    assert truth.moved(site)[1] == 0, truth.moved(site)


def _rail_low(site, q=5):
    v = truth.rail_beds(site).get("bridge")
    return None if v is None else round(float(np.percentile(v, q)), 1)


def _rail_high(site):
    return _rail_low(site, 95)


def test_the_survey_reads_as_lines_with_heights():
    site = truth.load("zurich_duttweilerbruecke")
    ws = truth.ways(site)
    assert len(ws) > 400 and {ln.layer for ln in ws} == {"road", "rail"}
    over = [c for c in truth.separations(ws, box=site.half)
            if {ws[c.i].layer, ws[c.j].layer} == {"road", "rail"} and "bridge" == ws[c.i].kind == ws[c.j].kind]
    assert over, "(the railway's viaduct and the road's bridge cross, both on bridges)"
    up = [ws[c.i] if c.dz > 0 else ws[c.j] for c in over]
    assert {ln.layer for ln in up} == {"road", "rail"}, "(one track over the bridge, the bridge over another)"
    assert np.all([4.0 < abs(c.dz) < 12.0 for c in over])


def test_a_way_with_stairs_is_a_path_not_a_road():
    # (the Europabruecke's "10 m road" under the tracks: a pedestrian underpass whose ends drop 4.7 m in 14 m; its 9 crossings
    # with the tracks were read as a road's, and left unjudged: the map has footways and steps there)
    site = truth.load("zurich_europabruecke")
    stairs = [ln for ln in site.lines if ln.attrs["kunstbaute"] in truth.STAIRS]
    assert any(ln.attrs["objektart"] == "10m Strasse" for ln in stairs)
    assert all(ln.cls == "path" for ln in stairs)
    assert not [ln for ln in truth.ways(site) if ln.attrs["kunstbaute"] in truth.STAIRS]
