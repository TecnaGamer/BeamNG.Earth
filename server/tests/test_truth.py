"""Earth's level decisions and solved shapes against the measured shape of real junctions (tests/truth.py: swisstopo's
3D lines round three places in Zurich, and Earth's own map and elevation there, both offline).

One direction at a time, each in seconds and with no build:
  1. the survey alone: its levels order every real crossing (what a level rule can rest on);
  2. the heuristics alone: gradeshapes' order and joins given the survey's own kinds and levels;
  3. the solver alone: the shapes it solves from those true facts on Earth's ground, against the real heights;
  4. Earth's reading of the map: the order its own members give the real crossings;
  5. the laws against the real roads: the clearance and the grade they ask, and what was built.

Each measure has two tests: no worse than TODAY (what holds now: lower a number when a change earns it), and its
goal, an expected failure until it is met (strict: the day it passes, the mark comes off and TODAY's number goes)."""
import functools

import numpy as np
import pytest

import truth

SURVEY_M = 1.5          # m: the survey's own accuracy (0.2-1.5 m): a shape nearer the real road than this is right
# what holds today, per place
TODAY = {
    "zurich_brunau": dict(
        over_wrong=40,          # roads over the Zimmerberg railway tunnel: a track in a tunnel has no kind, at grade
        reversed=3, at_grade=0,  # a link's deck at level 3 under the viaduct; the viaduct's parts read as ground
        shape_p95=6.9,          # m: the Sihl viaduct's decks, solved up to 10.8 m off the real ones
        under_laws=7, steeper=2),
    "zurich_europabruecke": dict(
        over_wrong=0,
        reversed=0, at_grade=4,  # the bridge's north end is ground on the map: the roads under it cross it at grade
        shape_p95=6.5,          # m: the roads under the tracks dug 3-4 m deeper than they are
        under_laws=16, steeper=2),
    "zurich_duttweilerbruecke": dict(
        over_wrong=0,
        reversed=3, at_grade=0,  # the railway viaduct over the bridge: every track is level 1, the bridge 2
        shape_p95=3.8,
        under_laws=5, steeper=1),
}
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
def test_the_order_given_the_true_kinds_and_levels_is_no_worse(site):
    assert len(truth.over_wrong(site)) <= TODAY[site]["over_wrong"]


@pytest.mark.parametrize("site", _goal({"zurich_brunau"}, "a track in a tunnel under a road is taken at grade"))
def test_the_order_given_the_true_kinds_and_levels_is_the_real_one(site):
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


@pytest.mark.parametrize("site", _goal(set(SITES), "the solver's decks and dips stand metres off the real ones"))
def test_the_shapes_solved_from_the_true_facts_are_the_real_roads_within_the_surveys_accuracy(site):
    assert _shape_p95(site) <= SURVEY_M, measured(site)["solved_minus_real"]


@pytest.mark.parametrize("site", SITES)
def test_earths_reading_of_the_map_orders_the_real_crossings_no_worse(site):
    r = measured(site)["earth_orders"]
    assert r["reversed"] <= TODAY[site]["reversed"], measured(site)["earth_orders_wrong"]
    assert r["at grade"] <= TODAY[site]["at_grade"], measured(site)["earth_orders_wrong"]
    assert r["right"] >= 20, "(the two sides are matched: most of the place's crossings are read)"


@pytest.mark.parametrize("site", _goal(set(SITES), "Earth's members order some real crossings wrongly or not at all"))
def test_earths_reading_of_the_map_orders_every_real_crossing(site):
    r = measured(site)["earth_orders"]
    assert r["reversed"] == 0 and r["at grade"] == 0, measured(site)["earth_orders_wrong"]


@pytest.mark.parametrize("site", SITES)
def test_the_laws_ask_no_more_clearance_of_the_real_bridges_than_today(site):
    r = measured(site)
    assert r["real_under_the_laws"] <= TODAY[site]["under_laws"], r["real_clearance_min_median"]


@pytest.mark.parametrize("site", _goal(set(SITES), "the laws' 6 m headroom against bridges built for less: a decision"))
def test_every_real_bridge_clears_what_the_laws_ask(site):
    assert measured(site)["real_under_the_laws"] == 0, measured(site)["real_clearance_min_median"]


@pytest.mark.parametrize("site", SITES)
def test_the_real_roads_keep_the_laws_grades(site):
    r = measured(site)
    assert r["roads_graded"] >= 100
    assert r["roads_steeper_than_the_laws"] <= TODAY[site]["steeper"]


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
