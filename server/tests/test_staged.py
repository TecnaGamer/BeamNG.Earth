"""The staged grade solver (server/staged*.py): each stage on small synthetic places, then the real places the offline data has
(Popoli's replay, the Zurich survey sites, the Shanghai regions where their dumps are there: tests/staged_fixtures.py)."""
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import gradeshapes as gs  # noqa: E402
import interchange_audit as ia  # noqa: E402
import staged  # noqa: E402
import staged_feasible as s4  # noqa: E402
import staged_ground as s3  # noqa: E402
import staged_levels as s2  # noqa: E402
import staged_paths as s1  # noqa: E402
import staged_places as places  # noqa: E402

Member = gs.Member


def line(x0, y0, x1, y1, n=20):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def poly(*pts):
    return np.array(pts, np.float64)


def road(key, ln, kind="ground", cls="secondary", level=0.0, half=4.0, kmh=60):
    return Member(key, ln, kind, cls, kmh, level, half, None)


def stages(members, meta=None, **kw):
    pl = places.synthetic(members, meta=meta)
    return staged.run(pl.members, pl.meta, None, None, pl.ground, **kw), pl


def codes(S, step, level=None):
    return sorted(f.code for f in S.finds[step] if level is None or f.level == level)


# ---- stage 1: paths ----

def test_a_road_split_into_lane_sections_is_one_path_and_a_side_street_is_another():
    m = [road(("r~lanes0-5", "ground", "a"), line(0, 0, 100, 0)), road(("r~lanes5-9", "ground", "b"), line(100, 0, 220, 0)),
         road(("r~lanes9-9", "ground", "c"), line(220, 0, 300, 0)), road(("side", "ground", "d"), line(100, 0, 100, 90))]
    S, _ = stages(m, feasibility=False)
    P = S.P
    assert len(P.paths) == 2
    main = next(p for p in P.paths if len(p.segs) == 3)
    assert [S.net.m[i].key[2] for i, _f in main.segs] in (["a", "b", "c"], ["c", "b", "a"])
    assert main.start.state == "free" and main.stop.state == "free"
    # the side street meets the road at the second joint: a joint of three ends where the road goes on into itself
    jt = next(j for j in P.joints if len(j.ends) == 3)
    assert len(jt.paired) == 1
    assert S.passed(1) or not S.fails(1)


def test_every_segment_is_in_exactly_one_path_with_no_gap():
    rng = np.random.default_rng(3)
    m = []
    for k in range(12):                 # a grid of streets cut at their junctions, one-way and two-way
        x = (k % 4) * 100.0
        y = (k // 4) * 100.0
        m.append(road(("e%d" % k, "ground", "a"), line(x, y, x + 100, y)))
        m.append(road(("n%d" % k, "ground", "b"), line(x, y, x, y + 100)))
    S, _ = stages(m, feasibility=False)
    P = S.P
    seen = [i for p in P.paths for i, _f in p.segs]
    assert sorted(seen) == list(range(len(S.net.m)))
    assert not [f for f in S.finds[1] if f.code in ("missing_segment", "duplicated_segment", "path_gap")]
    for p in P.paths:
        for (i, fi), (j, fj) in zip(p.segs, p.segs[1:]):
            a = S.net.lines[i][-1 if fi else 0]
            b = S.net.lines[j][0 if fj else -1]
            assert np.hypot(*(a - b)) <= gs.JOIN_M


def test_a_ramp_whose_end_meets_nothing_is_named_with_its_place():
    m = [road(("main", "ground", "a"), line(-300, 0, 300, 0), cls="motorway"),
         road(("ramp", "deck", 0, 0), line(0, 0, 0, 150), "deck", "motorway_link", 1.0),
         road(("under", "ground", "b"), line(-50, 60, 50, 60))]
    S, _ = stages(m, feasibility=False)
    bad = [f for f in S.finds[1] if f.code == "dangling_end"]
    assert len(bad) == 1 and bad[0].level == "fail"
    assert "a ramp" in bad[0].text and "ramp" in bad[0].text and "hangs in the air" in bad[0].text
    p = S.P.paths[S.P.path_of[next(i for i, x in enumerate(S.net.m) if x.kind == "deck")]]
    assert {r.kind for r in p.runs} == {"deck", "ground"} or {r.kind for r in p.runs} == {"deck"}


def test_the_same_road_twice_is_one_segment_and_two_roads_on_one_line_are_named():
    a = road(("w1", "deck", 0, 0), line(0, 0, 200, 0), "deck", "primary", 1.0)
    b = road(("w1", "deck", 3, 1), line(0, 0.1, 200, 0.1), "deck", "primary", 1.0)       # the copy another cell made
    c = road(("w2", "ground", "x"), line(0, 50, 80, 50))
    d = road(("w3", "ground", "y"), line(0, 50.1, 80, 50.1))                              # another way, the same line
    S, _ = stages([a, b, c, d], feasibility=False)
    assert len(S.net.m) == 3
    assert [f.code for f in S.finds[1] if f.code == "collapsed_copy"] == ["collapsed_copy"]
    assert [f.code for f in S.finds[1] if f.code == "duplicate"] == ["duplicate"]
    assert "two roads of the map" in next(f.text for f in S.finds[1] if f.code == "duplicate")
    assert not [f for f in S.finds[1] if f.code == "missing_segment"]


def test_a_road_and_its_own_lane_sections_is_one_path_of_the_sections():
    parent = road(("rd", "ground", "p"), line(0, 0, 300, 0, 80))
    s1_ = road(("rd~lanes0-5", "ground", "s1"), line(0, 0.2, 100, 0.2, 5))
    s2_ = road(("rd~lanes5-9", "ground", "s2"), line(100, 0.2, 300, 0.2, 7))
    S, _ = stages([parent, s1_, s2_], feasibility=False)
    assert len(S.net.m) == 2 and all("~lanes" in str(m.key[0]) for m in S.net.m)
    assert len(S.P.paths) == 1 and len(S.P.paths[0].segs) == 2
    assert not S.fails(1)
    # a section over a stretch of the road: the road keeps what it does not cover
    part = road(("rd2", "ground", "p"), line(0, 0, 300, 0, 80))
    sec = road(("rd2~lanes3-6", "ground", "s"), line(100, 0.1, 200, 0.1, 6))
    S2, _ = stages([part, sec], feasibility=False)
    assert len(S2.net.m) == 3
    assert len(S2.P.paths) == 1 and not S2.fails(1), [f.text for f in S2.fails(1)]


def test_a_tunnel_part_short_of_its_own_ground_part_is_joined_by_the_roads_identity():
    g1 = road(("rd", "ground", "a"), line(-300, 0, -100, 0))
    t = road(("rd", "tunnel", 0, 0), line(-100, 0, 0, 0), "tunnel", "secondary", -1.0)
    g2 = road(("rd", "ground", "b"), line(12, 0, 300, 0))              # 12 m past the tunnel's end
    S, _ = stages([g1, t, g2], feasibility=False)
    assert len(S.P.paths) == 1 and len(S.P.paths[0].segs) == 3
    gap = [f for f in S.finds[1] if f.code == "identity_join"]
    assert len(gap) == 1 and "12.0 m" in gap[0].text
    assert not [f for f in S.finds[1] if f.code in ("path_gap", "dangling_end")]
    # another road's end 12 m off is no part of it
    other = road(("other", "ground", "c"), line(-300, 40, -100, 40))
    far = road(("another", "ground", "d"), line(-88, 40, 100, 40))
    S2, _ = stages([other, far], feasibility=False)
    assert len(S2.P.paths) == 2


def test_no_segment_is_shorter_than_the_kernel_holds_it_to():
    pts = np.array([[0, 0], [0.1, 0], [0.2, 0.1], [50, 0], [50.2, 0], [100, 0]], np.float64)
    tiny = road(("tiny", "ground", "t"), line(100, 0, 100.3, 0, 3))
    m = [road(("a", "ground", "a"), pts), tiny, road(("b", "ground", "b"), line(100.3, 0, 200, 0))]
    S, _ = stages(m, feasibility=False)
    assert S.P.cleaned.merged_points >= 3 and S.P.cleaned.absorbed == [1]
    for ln in S.net.lines:
        assert (np.hypot(*np.diff(ln, axis=0).T) >= s1.MIN_SEG_M).all()
    assert len(S.P.paths) == 1 and not S.fails(1)


def test_traffic_arriving_where_nothing_takes_it_on_is_a_trap_and_the_roads_do_not_pair():
    a = road(("a", "ground", "a"), line(0, 0, 100, 0))
    b = road(("b", "ground", "b"), line(200, 0, 100, 0))           # both flow into the joint at x = 100
    meta = {"a": dict(oneway=True, dir=1), "b": dict(oneway=True, dir=1)}
    S, _ = stages([a, b], meta=meta, feasibility=False)
    assert len(S.P.paths) == 2
    bad = [f for f in S.finds[1] if f.code == "oneway_trap"]
    assert len(bad) == 1 and "no road there takes it on" in bad[0].text and bad[0].level == "fail"
    # one flowing on from the other is one path, oriented with the traffic
    b2 = road(("b", "ground", "b"), line(100, 0, 200, 0))
    S2, _ = stages([a, b2], meta=meta, feasibility=False)
    assert len(S2.P.paths) == 1 and S2.P.paths[0].flow == 1 and not S2.fails(1)
    # two one-way roads leaving a joint where nothing arrives are a source: a note (their feeder is not in the map read)
    c = road(("c", "ground", "c"), line(100, 0, 200, 0))
    d = road(("d", "ground", "d"), line(100, 0, 100, 150))
    meta2 = {"c": dict(oneway=True, dir=1), "d": dict(oneway=True, dir=1)}
    S3, _ = stages([c, d], meta=meta2, feasibility=False)
    assert not S3.fails(1) and [f.code for f in S3.finds[1] if f.code == "oneway_source"] == ["oneway_source"]


def test_one_way_roads_at_a_crossing_that_do_not_go_on_into_each_other_are_no_failure():
    # a four-way junction: east-west one way east, north-south one way north; each goes straight on, nothing is trapped
    m = [road(("w", "ground", "w"), line(-100, 0, 0, 0)), road(("e", "ground", "e"), line(0, 0, 100, 0)),
         road(("s", "ground", "s"), line(0, -100, 0, 0)), road(("n", "ground", "n"), line(0, 0, 0, 100))]
    meta = {k: dict(oneway=True, dir=1) for k in "wesn"}
    S, _ = stages(m, meta=meta, feasibility=False)
    assert len(S.P.paths) == 2 and not S.fails(1) and not [f for f in S.finds[1] if f.code == "oneway_source"]


def test_a_two_way_road_between_carriageways_that_both_feed_it_is_cut_so_no_path_runs_against_its_own_flows():
    # a (one way east) and b (one way west) both end on the two-way road t: each pairs with it, the chain a-t-b has traffic both ways
    a = road(("a", "ground", "a"), line(-200, 0, -100, 0))
    t = road(("t", "ground", "t"), line(-100, 0, 100, 0))
    b = road(("b", "ground", "b"), line(300, 0, 100, 0))
    meta = {"a": dict(oneway=True, dir=1), "b": dict(oneway=True, dir=1)}
    S, _ = stages([a, t, b], meta=meta, feasibility=False)
    assert len(S.P.paths) == 2
    assert not [f for f in S.finds[1] if f.code == "path_direction"]
    assert [f.code for f in S.finds[1] if f.code == "path_cut"] == ["path_cut"]
    assert all(p.flow is not None for p in S.P.paths)


def test_two_structures_facing_across_a_short_gap_are_one_road_going_on_and_side_by_side_ones_are_not():
    a = road(("a", "deck", 0, 0), line(-300, 0, -5, 0, 30), "deck", "trunk", 1.0, half=6.0)
    b = road(("b", "deck", 0, 1), line(5, 0, 300, 0, 30), "deck", "trunk", 1.0, half=6.0)         # 10 m on, nothing joins either end
    S, _ = stages([a, b], feasibility=False)
    assert len(S.P.paths) == 1 and [f.code for f in S.finds[1] if f.code == "facing_join"] == ["facing_join"]
    assert "10.0 m apart" in next(f.text for f in S.finds[1] if f.code == "facing_join")
    # another level, or another class, or side by side: each end meets nothing
    b2 = road(("b", "deck", 0, 1), line(5, 0, 300, 0, 30), "deck", "trunk", 2.0, half=6.0)
    assert len(stages([a, b2], feasibility=False)[0].P.paths) == 2
    b3 = road(("b", "deck", 0, 1), line(-5, 10, 300, 10, 30), "deck", "trunk", 1.0, half=6.0)     # beside it, not on from it
    assert len(stages([a, b3], feasibility=False)[0].P.paths) == 2
    # the ends that meet nothing are named with what is nearest them
    d = [f for f in stages([a, b2], feasibility=False)[0].finds[1] if f.code == "dangling_end"]
    assert d and any("the nearest road is" in f.text and "m off" in f.text for f in d)


def test_a_loose_end_is_looked_up_in_the_map_before_it_is_a_failure():
    """A deck end that meets nothing is the map's word first: the road goes on outside the box, into a tunnel network or a road the
    shapes take no member of, or the map has no road there (the map's, named, no failure); only an end the map says nothing of is a
    dangling_end. A part shortened by the cleaning at that end has no such facts."""
    a = road(("a", "deck", 0, 0), line(0, 0, 300, 0, 30), "deck", "trunk", 1.0, half=6.0)
    b = road(("g", "ground", 0, 0), line(-200, 0, 0, 0, 30))
    base = dict(near=[], inside=[], others=[], connector=None, feature_end=None, own_gap=None, own_next=None)

    def run(facts_of):
        pl = places.synthetic([a, b])
        return staged.run(pl.members, pl.meta, None, None, pl.ground, feasibility=False,
                          endinfo=lambda i, e: facts_of(pl.members[i].key[0], e))
    none = run(lambda k, e: dict(base))
    assert codes(none, 1, "fail") == ["dangling_end"]                   # (its start meets the ground road, its end meets nothing)
    far = run(lambda k, e: dict(base, near=[("x", "deck", "trunk", 1.0, 1.2, ("x", "deck", 0, 0))]) if (k, e) == ("a", 1) else dict(base))
    assert "end_beyond_region" in codes(far, 1, "note")
    assert far.stats[1]["ends_by_map"].get("end_beyond_region") == 1
    unconnected = run(lambda k, e: dict(base, feature_end=e, connector="c9") if (k, e) == ("a", 1) else dict(base))
    assert [f.level for f in unconnected.finds[1] if f.code == "end_unconnected"] == ["map"]
    assert "end_unconnected" not in codes(unconnected, 1, "fail") and unconnected.passed(1) == (not unconnected.fails(1))


def test_a_road_bridges_end_on_a_track_is_no_failure_and_a_member_is_not_its_own_neighbour():
    """The map says a road is at that end, in the net, within the joint distance, and no joint was made: it is none by the law where it
    is another network (roads and rails do not join), and a copy the cleaning made one with the member is the member itself."""
    a = road(("a", "deck", 0, 0), line(0, 0, 300, 0, 30), "deck", "trunk", 1.0, half=6.0)
    g = road(("g", "ground", 0, 0), line(-200, 0, 0, 0, 30))
    r = Member(("rail", "rail", 0), line(300, 0, 500, 0, 20), "rail", "rail", 100, 0.0, 2.0, None)
    base = dict(near=[], inside=[], others=[], connector=None, feature_end=None, own_gap=None, own_next=None)
    pl = places.synthetic([a, g, r])
    ia_ = {i: m.key for i, m in enumerate(pl.members)}

    def endinfo(i, e):
        if ia_[i][0] == "a" and e == 1:
            return dict(base, inside=[("rail", "rail", "rail", 0.0, 0.0, ("rail", "rail", 0))])
        return dict(base)
    S = staged.run(pl.members, pl.meta, None, None, pl.ground, feasibility=False, endinfo=endinfo)
    assert "end_at_separation" in codes(S, 1, "note") and "end_not_joined" not in codes(S, 1)
    assert "another network" in next(f.text for f in S.finds[1] if f.code == "end_at_separation")


def test_one_road_twice_with_another_smoothing_is_one_segment():
    ln1 = line(0, 0, 400, 0, 9)
    ln2 = np.stack([np.linspace(0, 400, 25), 0.8 * np.sin(np.linspace(0, np.pi, 25))], axis=1)     # the same road, 0.8 m off along its middle
    a = road(("w1", "deck", 0, 0), ln1, "deck", "trunk", 1.0)
    b = road(("w1", "deck", 1, 1), ln2, "deck", "trunk", 1.0)
    S, _ = stages([a, b], feasibility=False)
    assert len(S.net.m) == 1 and S.P.cleaned.collapsed and not [f for f in S.finds[1] if f.code in ("overlap", "duplicate")]


def test_two_ways_on_one_line_on_the_ground_are_a_note_and_on_a_structure_a_failure():
    g1 = road(("w1", "ground", "a"), line(0, 0, 80, 0))
    g2 = road(("w2", "ground", "b"), line(0, 0.1, 80, 0.1))
    S, _ = stages([g1, g2], feasibility=False)
    assert [(f.code, f.level) for f in S.finds[1] if f.code == "duplicate"] == [("duplicate", "note")]
    d1 = road(("w3", "deck", 0, 0), line(0, 50, 80, 50), "deck", "primary", 1.0)
    d2 = road(("w4", "deck", 1, 1), line(0, 50.1, 80, 50.1), "deck", "primary", 1.0)
    S2, _ = stages([d1, d2], feasibility=False)
    assert [(f.code, f.level) for f in S2.finds[1] if f.code == "duplicate"] == [("duplicate", "fail")]


# ---- stage 2: level order ----

def tie_members():
    a = road(("a", "deck", 0, 0), line(-120, 0, 120, 0, 25), "deck", "primary", 1.0, half=5.0)
    b = road(("b", "deck", 1, 1), line(0, -120, 0, 120, 25), "deck", "primary", 1.0, half=5.0)
    return [a, b]


def test_a_tie_of_two_decks_is_ordered_by_their_natural_heights_where_they_differ():
    def hill(xy):
        xy = np.atleast_2d(xy)
        return np.where(np.abs(xy[:, 1]) > 60.0, 14.0, 0.0)          # b's ends stand on a hill, a's on the flat
    pl = places.synthetic(tie_members(), ground=hill)
    S = staged.run(pl.members, pl.meta, None, None, pl.ground, feasibility=False)
    assert not S.fails(2) and len(S.L.crossings) == 1
    c = S.L.crossings[0]
    assert c.by == "elevation" and S.net.m[c.upper].key[2] == 1 and c.moves == "up"
    assert len(S.G.systems) == 1 and [x["moves"] for x in S.G.systems[0]["crossings"]] == ["up"]
    # on flat ground nothing orders them: a tie, named with the heights the elevation gave
    flat, _ = stages(tie_members(), feasibility=False)
    tie = [f for f in flat.finds[2] if f.code == "level_tie"]
    assert len(tie) == 1 and tie[0].level == "fail" and "the elevation does not say either" in tie[0].text


def test_the_level_tie_is_a_tie_where_the_decks_cross_with_no_elevation_given():
    pl = places.synthetic(tie_members())
    S = staged.run(pl.members, pl.meta, None, None, None, feasibility=False)
    assert [f.code for f in S.finds[2]] == ["level_tie"]


# ---- stage 2 (the order graph) ----

def overpass_members(deck_level=1.0, mway_kind="ground"):
    return [road(("mway", mway_kind, "m"), line(-400, 0, 400, 0), cls="motorway", kmh=120, half=12.0),
            road(("deck", "deck", 0, 0), line(0, -30, 0, 30, 13), "deck", "secondary", deck_level, half=5.0),
            road(("south", "ground", "s"), line(0, -600, 0, -30), half=5.0),
            road(("north", "ground", "n"), line(0, 30, 0, 600), half=5.0)]


def test_a_deck_over_a_road_is_above_it_and_provisionally_goes_up():
    S, _ = stages(overpass_members(), feasibility=False)
    L = S.L
    assert len(L.crossings) == 1
    c = L.crossings[0]
    assert S.net.m[c.upper].kind == "deck" and S.net.m[c.lower].kind == "ground" and c.moves == "up" and c.provisional
    assert S.passed(2) and S.stats[2]["edges"] == 1


def test_a_tunnel_under_a_road_goes_down_and_two_structures_with_one_level_are_a_tie():
    m = [road(("rd", "ground", "r"), line(-300, 0, 300, 0)),
         road(("under", "tunnel", 0, 0), line(0, -20, 0, 20, 9), "tunnel", "secondary", -1.0)]
    S, _ = stages(m, feasibility=False)
    assert [(S.net.m[c.upper].kind, c.moves) for c in S.L.crossings] == [("ground", "down")] or \
        [(S.net.m[c.lower].kind, c.moves) for c in S.L.crossings] == [("tunnel", "down")]
    d1 = road(("d1", "deck", 0, 0), line(-50, 0, 50, 0), "deck", "primary", 1.0)
    d2 = road(("d2", "deck", 1, 1), line(0, -50, 0, 50), "deck", "primary", 1.0)
    S2, _ = stages([d1, d2], feasibility=False)
    tie = [f for f in S2.finds[2] if f.code == "level_tie"]
    assert len(tie) == 1 and "nothing says which is over" in tie[0].text and tie[0].level == "fail"


def test_a_spiral_crossing_itself_is_two_places_on_one_path_and_a_cycle_in_above_is_reported_with_its_crossings():
    # one road in three decks at levels 1, 2, 3 winding up round a point: the later turns cross over the first
    r = 60.0
    t = np.linspace(0.0, 3 * 2 * np.pi * 0.98, 300)
    xy = np.stack([r * np.cos(t) * (1 + 0.05 * t / 6.28), r * np.sin(t)], axis=1)
    xy[:, 0] += np.linspace(0, 40, len(xy))
    a, b = np.array_split(np.arange(len(xy)), [100, 200])[:2]
    parts = np.array_split(np.arange(len(xy) + 2), 3)
    cuts = [0, 100, 200, len(xy) - 1]
    segs = [xy[cuts[k]:cuts[k + 1] + 1] for k in range(3)]
    m = [road(("spiral", "deck", k, 0), segs[k], "deck", "trunk", float(k + 1), half=5.0) for k in range(3)]
    m += [road(("g0", "ground", "0"), line(xy[0][0] - 200, xy[0][1], xy[0][0], xy[0][1])),
          road(("g1", "ground", "1"), line(xy[-1][0], xy[-1][1], xy[-1][0] + 300, xy[-1][1] + 100))]
    S, _ = stages(m, feasibility=False)
    assert len(S.P.paths) == 1 or any(len(p.segs) == 3 for p in S.P.paths)
    own = [c for c in S.L.crossings if c.span_u[0] == c.span_d[0]]
    assert own, "a turn over an earlier one is a crossing of the path with itself"
    assert all(c.span_u != c.span_d for c in own)                      # (two spans of one path: the levels differ)
    assert "path_over_itself" in codes(S, 2, "note") and S.passed(2)
    # an order that came from elsewhere against the levels closes a cycle: both crossings named
    L = S.L
    c = own[0]
    L.graph.add(c.span_u, c.span_d, c.id)
    namer = ia.Namer(S.net, None, {}, None)
    finds, stats = s2.check(L, namer)
    cyc = [f for f in finds if f.code == "order_cycle"]
    assert len(cyc) == 1 and cyc[0].level == "fail" and " over " in cyc[0].text and stats["cycles"] == 1


def test_the_order_graph_finds_a_cycle_of_three_and_nothing_in_a_stack():
    g = s2.OrderGraph()
    for a, b, ref in ((1, 2, "x"), (2, 3, "y"), (3, 1, "z"), (3, 4, "w")):
        g.add(a, b, ref)
    comps = g.components()
    assert comps == [[1, 2, 3]]
    cyc = g.cycle(comps[0])
    assert sorted(ref for _a, _b, ref in cyc) == ["x", "y", "z"]
    h = s2.OrderGraph()
    for a, b in ((1, 2), (2, 3), (1, 3)):
        h.add(a, b, None)
    assert h.components() == []


# ---- stage 3: ground points, systems ----

def two_overpasses(gap=60.0):
    """Two overpasses over two motorways, one secondary road going over both, a ground road of gap m between the decks' ends."""
    return [road(("m1", "ground", "m1"), line(-800, 0, 800, 0, 200), cls="motorway", kmh=120, half=12.0),
            road(("m2", "ground", "m2"), line(-800, 30 + gap + 30, 800, 30 + gap + 30, 200), cls="motorway", kmh=120, half=12.0),
            road(("d1", "deck", 0, 0), line(0, -30, 0, 30, 13), "deck", "secondary", 1.0, half=5.0),
            road(("d2", "deck", 0, 1), line(0, 30 + gap, 0, 90 + gap, 13), "deck", "secondary", 1.0, half=5.0),
            road(("mid", "ground", "mid"), line(0, 30, 0, 30 + gap, 8), half=5.0),
            road(("s", "ground", "s"), line(0, -600, 0, -30), half=5.0),
            road(("n", "ground", "n"), line(0, 90 + gap, 0, 700), half=5.0)]


def test_two_interchanges_joined_only_by_a_ground_road_are_two_systems_where_today_has_one():
    S, pl = stages(two_overpasses(), feasibility=False)
    assert len(pl.today) == 1 and len(pl.today[0]["seeds"]) == 2          # (find_systems: a road of up to CONNECT_M between two ends)
    assert len(S.G.systems) == 2 and all(len(s["seeds"]) == 1 for s in S.G.systems)
    assert [sorted(ss) for _j, ss in S.G.ramps] and len(S.G.ramps) == 1   # (the road the old rule joined them through: named)
    # the decks' ends meet that road at grade: a ground point on each deck's path
    for pid in {S.P.path_of[i] for s in S.G.systems for i in s["seeds"]}:
        assert any(g.why in ("segment", "at_grade") for g in S.G.points[pid])
    # a road with a deck of the other system's over it keeps them joined: here the deck's own joint does
    cmp_ = staged.compare(S, pl.today, pl.net)
    assert cmp_["today_split_into"] == [2]


def test_a_path_with_no_ground_point_is_reported_and_one_joined_to_a_grounded_path_is_anchored():
    m = [road(("rd", "ground", "r"), line(-300, 0, 300, 0)),
         road(("d1", "deck", 0, 0), line(0, -40, 0, 40, 20), "deck", "primary", 1.0)]
    S, _ = stages(m, feasibility=False)
    fl = [f for f in S.finds[3] if f.code == "no_ground_point"]
    assert len(fl) == 1 and fl[0].level == "fail"
    m2 = m + [road(("g", "ground", "g"), line(0, 40, 0, 300))]
    S2, _ = stages(m2, feasibility=False)
    assert not [f for f in S2.finds[3] if f.code == "no_ground_point"]
    assert S2.G.reach[S2.P.path_of[1]] == 0.0           # (the path itself runs on to the road on the ground)


def test_a_big_system_names_what_links_it(monkeypatch):
    monkeypatch.setattr(s3, "OVERSIZE_PIECES", 1)
    S, _ = stages(two_overpasses(gap=0.5), feasibility=False)
    big = [f for f in S.finds[3] if f.code == "oversized_system"]
    assert big and "joints" in big[0].text and "linked by" in big[0].text


# ---- stage 4: feasibility ----

def test_an_overpass_has_heights_and_a_short_bridge_over_a_street_digs_it():
    S, _ = stages(overpass_members())
    assert S.stats[4]["infeasible"] == 0 and S.feasible[0]["feasible"]
    assert S.feasible[0]["certificate_violation_m"] <= 1e-6        # (the heights the check ends on satisfy every constraint)
    m = [road(("street", "ground", "s"), line(-400, 0, 400, 0, 100), cls="residential", kmh=40, half=4.0),
         road(("deck", "deck", 0, 0), line(0, -6, 0, 6, 4), "deck", "tertiary", 1.0, half=4.0, kmh=50),
         road(("s", "ground", "a"), line(0, -300, 0, -6), cls="tertiary", kmh=50, half=4.0),
         road(("n", "ground", "b"), line(0, 6, 0, 300), cls="tertiary", kmh=50, half=4.0)]
    S2, _ = stages(m)
    assert [c["moves"] for c in S2.G.systems[0]["crossings"]] == ["down"] and S2.feasible[0]["feasible"]


def twisted(p1_level, q_level):
    """Two ramps and a road in two decks: the road P (decks at p1_level then 3, joined at 0,0) is crossed 19 m before the joint by
    ramp Q and 15 m after it by Q again, the ramp's two decks joined between (Shanghai's w47097208: 12.7 m of rise asked over 130 m
    that 6 % allows 7.8, the road tagged level 1 and level 3 in two parts where level 2 ramps cross each part)."""
    return [Member(("P", "deck", 1), line(-100, 0, 0, 0, 30), "deck", "trunk_link", None, p1_level, 4.0, None),
            Member(("P", "deck", 2), line(0, 0, 100, 0, 30), "deck", "trunk_link", None, 3.0, 4.0, None),
            Member(("Q1", "deck", 1), poly((-19, -60), (-19, 0), (-19, 60)), "deck", "trunk_link", None, q_level, 4.0, None),
            Member(("Q2", "deck", 1), poly((-19, 60), (15, 60), (15, 0), (15, -60)), "deck", "trunk_link", None, q_level, 4.0, None)]


def test_a_contradiction_is_the_maps_with_its_loop_of_roads_named():
    S, _ = stages(twisted(1.0, 2.0))
    st = S.feasible[0]
    assert not st["feasible"] and st["independent_contradictions"] == 1
    c = st["contradictions"][0]
    assert c["short_m"] > 1.0 and c["demand_m"] > c["allowed_m"]
    kinds = [leg["kind"] for leg in c["legs"]]
    assert kinds.count("clear") == 2 and "join" in kinds and "grade" in kinds
    keys = {r["earth"] for leg in c["legs"] for r in leg["refs"]}
    assert keys == {"P", "Q1", "Q2"}
    assert c["owner"] == "map_levels"                     # (a joint of one road whose parts carry levels 1 and 3)
    f = S.fails(4)[0]
    assert "never solved round" in f.text and "levels" in f.text
    # the same road at one level under the ramps' loop: nothing contradicts
    S2, _ = stages(twisted(3.0, 2.0))
    assert S2.feasible[0]["feasible"]


def test_the_grade_law_is_each_steps_own_length_not_the_closest_two_samples():
    # a deck joined to another 0.4 m from a sample: the kernel's step (the closest two samples) holds the whole piece to the
    # grade over 0.4 m; here a deck that climbs exactly its law over its whole length has heights
    g = gs.deck_grade(Member("x", None, "deck", "secondary", 60, 1.0, 4.0, None))
    L = 120.0
    rise = g * L
    ground = lambda xy: np.where(xy[:, 1] > 0, 0.0, 0.0)          # noqa: E731
    deck = road(("d", "deck", 0, 0), poly((0, 0), (0, 40.4), (0, 40.8), (0, L)), "deck", "secondary", 1.0, half=4.0)
    other = road(("c", "ground", "c"), line(-60, 70, 60, 70), half=4.0)
    pl = places.synthetic([deck, other], ground)
    S = staged.run(pl.members, pl.meta, None, None, pl.ground)
    assert S.stats[4]["infeasible"] == 0, [f.text for f in S.finds[4]]
    assert rise > 0


# ---- the real places the offline data has ----

def test_popoli_runs_through_the_four_stages_and_the_copies_are_collapsed():
    pl = places.replay("popoli_ss17_underpass")
    S = staged.run(pl.members, pl.meta, pl.ll, pl.edge, pl.ground, name="popoli")
    assert S.P.cleaned.collapsed and not [f for f in S.finds[1] if f.code in ("duplicate", "overlap", "missing_segment")]
    assert S.passed(2) and S.passed(3) and S.passed(4)
    assert len(S.G.systems) == 5
    cmp_ = staged.compare(S, pl.today, pl.net)
    assert cmp_["moves_differ"] == 0


@pytest.mark.parametrize("name", ["zurich_brunau", "zurich_duttweilerbruecke"])
def test_the_survey_places_have_the_systems_today_finds_and_heights(name):
    pl = places.truth(name)
    S = staged.run(pl.members, pl.meta, pl.ll, pl.edge, pl.ground, name=name, endinfo=getattr(pl, "endinfo", None))
    assert len(S.G.systems) == len(pl.today)
    assert S.stats[4]["infeasible"] == 0 and S.passed(2) and S.passed(3)


def test_the_stages_are_deterministic():
    pl = places.truth("zurich_duttweilerbruecke")
    a = staged.run(pl.members, pl.meta, pl.ll, pl.edge, pl.ground, name="x").report()
    pl2 = places.truth("zurich_duttweilerbruecke")
    b = staged.run(pl2.members, pl2.meta, pl2.ll, pl2.edge, pl2.ground, name="x").report()
    assert a == b


@pytest.mark.skipif(places.region("shanghai_13719_6695") is None, reason="the Shanghai dump is not here")
def test_shanghai_yanan_west_names_the_contradiction_of_the_map():
    pl = places.region("shanghai_13719_6695")
    S = staged.run(pl.members, pl.meta, pl.ll, pl.edge, pl.ground, name="yanan")
    assert S.stats[4]["infeasible"] >= 1
    worst = max((c for st in S.feasible.values() for c in st.get("contradictions", ())), key=lambda c: c["short_m"])
    assert 4.5 < worst["short_m"] < 5.2 and worst["owner"] == "map_levels"
    assert {r["osm"] for leg in worst["legs"] for r in leg["refs"] if r.get("osm")} >= {"w47097208@6", "w9930045@17", "w493165656@5"}
    # the ground roads between decks' ends no longer join them, the copies of one road are one
    cmp_ = staged.compare(S, pl.today, pl.net)
    assert cmp_["staged_sizes"][0][0] < cmp_["today_sizes"][0][0] and S.P.cleaned.collapsed
    assert len(S.G.ramps) > 50
