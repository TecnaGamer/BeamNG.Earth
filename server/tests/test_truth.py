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
        dug=(7, 8),             # per mille of the ground roads' points, 3.3 m at the deepest
        filled=(14, 13),        # 2.3 m at most (was 6.5): the motorway at its tunnels' mouths. The mouths are right (floor
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
        dug=(0, 0), filled=(0, 7),      # (with the shapes 5 before the decks kept their line: a deck's end 1.9 m over its road)
        rail_low=2.6,           # m: the Letzigraben flyover's ramp. It is 13 m over the tracks it crosses 270 m on, which
        rail_high=3.1),         # Earth's clears by a track's headroom, and climbs to them at 4 %: Earth's at its 2 %
        #                         (GRADE at 2.5 %: +0.7 .. +0.9, at 2.8 %: -0.4 .. -0.1; the other places' lows then 0.1-0.9
        #                         lower, PopoliTest in test_findground.py has the numbers)
    "zurich_duttweilerbruecke": dict(
        unmatched=3,            # (22 before): Earth's rail ends at the crossing (1), one rail deck along both ways (2)
        not_built=14,           # 8 street trams on the ground, 6 paths under or on the bridges: Earth builds none
        shape_p95=2.3,          # m: a service ramp's deck (was 3.4)
        under_laws=2, steeper=1,
        dug=(3, 3),             # 3.3 m at the deepest. (12 while every rail bridge dug the road under it)
        filled=(0, 0),
        rail_low=-8.6,          # m: the Wipkingen viaduct, 13 m over its streets, stands 10 to 13 m up (was at their
        rail_high=0.4),         # level at its joints, -13.2); the Letzigraben flyover is 9 m short of the survey's 18 m
        #                         over the tracks it crosses: no bridge crosses another here, in the map or the survey
        #                         (the flyover, level 3 in the survey, crosses tracks on the ground, which the law asks
        #                         6.5 m over, and one road bridge, 6.6 m under it), so the 11 m it climbs over the
        #                         chord between its ends are no crossing's (measured 2026-11-18). The railway's grade
        #                         law (2 %; the survey's flyover ramps are 3.3-4.1 %): 2.5 % makes this -9.2, 2.8 % -9.6
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
        rail_low=-9.7, rail_high=0.2),
    "kempttal_viaduct": dict(
        unmatched=0, not_built=0, right=6,
        shape_p95=0.9,          # m: the viaduct's deck (was 4.6 at its ends)
        under_laws=0, steeper=0, graded=60,
        dug=(1, 0), filled=(0, 0), rail_low=-1.0, rail_high=0.1),
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


@pytest.mark.parametrize("site", SITES)
def test_the_true_facts_join_no_lines_really_apart_and_every_system_has_a_shape(site):
    r = measured(site)
    assert r["false_joins"] == 0
    assert r["infeasible"] == 0, "(the real junction's facts have an answer under the laws)"
    assert r["systems"] >= 2


def _shape_p95(site):
    return max(v[1] for v in measured(site)["solved_minus_real"].values())


@pytest.mark.parametrize("site", SITES)
def test_the_shapes_solved_from_the_true_facts_are_no_further_from_the_real_roads(site):
    assert _shape_p95(site) <= TODAY[site]["shape_p95"], measured(site)["solved_minus_real"]


@pytest.mark.parametrize("site", _goal(set(SITES) - {"kempttal_viaduct"},
                                          "the solver's decks and dips stand metres off the real ones"))
def test_the_shapes_solved_from_the_true_facts_are_the_real_roads_within_the_surveys_accuracy(site):
    assert _shape_p95(site) <= SURVEY_M, measured(site)["solved_minus_real"]


@pytest.mark.parametrize("site", SITES)
def test_earths_reading_of_the_map_orders_every_real_crossing_it_has_both_lines_of(site):
    # (the Duttweilerbruecke, level 2, was over the railway viaduct on level 3 that crosses over it: 3 crossings)
    r = measured(site)["earth_orders"]
    assert r["reversed"] == 0 and r["at grade"] == 0, measured(site)["earth_orders_wrong"]
    assert r["right"] >= TODAY[site].get("right", 20), "(most of the place's crossings are matched and read)"
    assert r["unmatched"] <= TODAY[site]["unmatched"], measured(site)["unmatched_why"]
    assert r["not built"] <= TODAY[site]["not_built"], "(no more of the ways Earth builds nothing of than today)"


@pytest.mark.parametrize("site", _goal({s for s in SITES if TODAY[s]["unmatched"]},
                                          "real crossings with no member of Earth's along one of the two"))
def test_every_real_crossing_is_judged(site):
    assert measured(site)["earth_orders"]["unmatched"] == 0, measured(site)["unmatched_why"]


@pytest.mark.parametrize("site", SITES)
def test_the_laws_ask_no_more_clearance_of_the_real_bridges_than_today(site):
    r = measured(site)
    assert r["real_under_the_laws"] <= TODAY[site]["under_laws"], r["real_clearance_min_median"]


@pytest.mark.parametrize("site", _goal({s for s in SITES if TODAY[s]["under_laws"]},
                                          "a road's 4 m and a track's 6 m against bridges built for less"))
def test_every_real_bridge_clears_what_the_laws_ask(site):
    assert measured(site)["real_under_the_laws"] == 0, measured(site)["real_clearance_min_median"]


@pytest.mark.parametrize("site", SITES)
def test_the_real_roads_keep_the_laws_grades(site):
    r = measured(site)
    assert r["roads_graded"] >= TODAY[site].get("graded", 100)
    assert r["roads_steeper_than_the_laws"] <= TODAY[site]["steeper"]


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


@pytest.mark.parametrize("site", [s for s in SITES if TODAY[s]["rail_low"] is not None])
def test_earths_railway_bridges_lie_no_further_under_the_real_tracks_than_today(site):
    assert _rail_low(site) >= TODAY[site]["rail_low"], truth.built(site)["rail_bed_minus_real"]
    ground = truth.rail_beds(site)["ground"]
    assert abs(float(np.median(ground))) <= 0.2, "(the track on the ground is the real one's: the elevation has it)"


@pytest.mark.parametrize("site", [s for s in SITES if TODAY[s]["rail_high"] is not None])
def test_earths_railway_bridges_lie_no_further_over_the_real_tracks_than_today(site):
    assert _rail_high(site) <= TODAY[site]["rail_high"], truth.built(site)["rail_bed_minus_real"]


@pytest.mark.parametrize("site", [pytest.param(s, marks=pytest.mark.xfail(strict=True, reason="a railway bridge's bed "
                                               "off the real track")) if TODAY[s]["rail_low"] < -RAIL_M
                                  or TODAY[s]["rail_high"] > RAIL_M else s
                                  for s in SITES if TODAY[s]["rail_low"] is not None])
def test_earths_railway_bridges_are_the_real_tracks(site):
    assert _rail_low(site) >= -RAIL_M and _rail_high(site) <= RAIL_M, truth.built(site)["rail_bed_minus_real"]


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
