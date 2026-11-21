"""interchange_audit: the validators of the shapes pipeline's steps, each on a small synthetic system that has the fault (and
a twin that has not): a ramp left dangling, one-way directions that disagree, a road's lane sections split, broken parts,
a level cycle needing 12.7 m in 130 m at 6 %, two interchanges merged through one link."""
import math
from unittest import mock

import numpy as np

import gradeshapes as gs
import interchange_audit as ia
from gradeshapes import Member


def _line(x0, y0, x1, y1, n=40):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def _deck(key, line, cls="secondary", level=1.0, half=5.0, nodes=None):
    return Member((key, "deck", 0), line, "deck", cls, 60, level, half, None, nodes)


def _ground(key, line, cls="secondary", half=5.0):
    return Member((key, "ground", 0), line, "ground", cls, 60, 0.0, half, None)


def _audit(members, **kw):
    net, systems, audits, namer = ia.audit_members(members, **kw)
    return net, systems, audits


def _finds(audits, step, code=None):
    return [f for a in audits for f in a["steps"][step]["findings"] if code is None or f.code == code]


def _overpass(ramp_far_end=True):
    """A deck over a motorway with a ramp going on from its north end (a link: the road under the deck is not its own)."""
    m = [_ground("mway", _line(-800, 0, 800, 0, 200), "motorway"),
         _deck("deck", _line(0, -30, 0, 30, 13)),
         _ground("south", _line(0, -600, 0, -30)),
         _deck("ramp", _line(0, 30, 0, 90, 16), "secondary_link")]
    if ramp_far_end:
        m.append(_ground("north", _line(0, 90, 0, 600)))
    return m


# ---- step 1: in / out ----

def test_a_ramp_left_dangling_is_named():
    net, systems, audits = _audit(_overpass(ramp_far_end=False))
    f = _finds(audits, 1, "dangling_end")
    assert len(f) == 1 and f[0].level == "fail"
    assert f[0].refs[0]["earth"] == "ramp" and f[0].refs[0]["cls"] == "secondary_link"
    assert "a ramp" in f[0].text and "its end" in f[0].text
    assert not audits[0]["steps"][1]["ok"]
    assert audits[0]["steps"][1]["stats"]["roles"] == {"ramp": 1, "street": 1}


def test_the_ramp_joined_at_both_ends_passes():
    net, systems, audits = _audit(_overpass())
    assert audits[0]["steps"][1]["ok"], [f.text for f in _finds(audits, 1)]
    assert audits[0]["steps"][1]["stats"]["portals"] == 2         # (the roads on the ground it meets: south and north)


def test_a_deck_end_over_another_deck_with_no_node_is_a_separation_not_a_joint():
    # (a level-2 deck ending right over a level-1 deck's middle, no node of both: gradeshapes._end_meets says separation)
    m = [_ground("mway", _line(-800, 0, 800, 0, 200), "motorway"),
         _deck("low", _line(-60, 30, 60, 30, 40), nodes=frozenset({"a", "b"})),
         _deck("high", _line(0, -30, 0, 30, 13), level=2.0, nodes=frozenset({"c", "d"})),
         _ground("s", _line(0, -600, 0, -30))]
    net, systems, audits = _audit(m)
    sep = _finds(audits, 1, "end_separated")
    assert len(sep) == 1 and sep[0].refs[0]["earth"] == "high" and sep[0].refs[1]["earth"] == "low"


def test_one_way_directions_that_disagree_at_a_joint():
    m = _overpass()
    meta = {"deck": dict(oneway=True, dir=1), "ramp": dict(oneway=True, dir=-1)}      # (against its line: it flows to the joint)
    net, systems, audits = _audit(m, meta=meta)
    f = _finds(audits, 1, "oneway_conflict")
    assert len(f) == 2, [x.text for x in f]                 # (each of the two ways arriving at the joint finds no way on)
    assert all("takes it on" in x.text for x in f)
    meta["ramp"] = dict(oneway=True, dir=1)
    net, systems, audits = _audit(m, meta=meta)
    assert not _finds(audits, 1, "oneway_conflict")


def test_two_ways_leaving_a_joint_with_nothing_arriving_disagree_too():
    m = _overpass()
    meta = {"deck": dict(oneway=True, dir=-1), "ramp": dict(oneway=True, dir=1)}      # (both flow away from the joint)
    net, systems, audits = _audit(m, meta=meta)
    f = _finds(audits, 1, "oneway_conflict")
    assert len(f) == 2 and all("feeds it" in x.text for x in f)


# ---- step 2: pieces ----

def test_one_roads_lane_sections_at_two_levels_are_one_road_split():
    # (Shanghai's Yan'an elevated road: one carriageway's sections tagged level 1 and 2, BeamNG.Earth#47)
    a, b = "road~lanes000000000-500000000", "road~lanes500000000-999999999"
    m = [_ground("st", _line(-300, -30, 300, -30, 100), "primary"),
         _deck(a, _line(0, -60, 0, 0, 16), "primary", 1.0),
         _deck(b, _line(0, 0, 0, 60, 16), "primary", 2.0),
         _ground("s", _line(0, -400, 0, -60), "primary"), _ground("n", _line(0, 60, 0, 400), "primary")]
    net, systems, audits = _audit(m)
    f = _finds(audits, 2, "level_split")
    assert len(f) >= 1 and {r["earth"] for r in f[0].refs} == {a, b}
    assert "levels (1 and 2)" in f[0].text
    assert audits[0]["steps"][3]["ok"], "(PR 47: joined as one road, the system has heights)"


def test_a_gap_between_one_roads_parts():
    a, b = "road~lanes000000000-500000000", "road~lanes500000000-999999999"
    m = [_ground("st", _line(-300, -30, 300, -30, 100), "primary"),
         _deck(a, _line(0, -60, 0, -4, 16), "primary", 1.0),
         _deck(b, _line(0, 4, 0, 60, 16), "primary", 1.0)]
    net, systems, audits = _audit(m)
    f = _finds(audits, 2, "gap")
    assert len(f) == 1 and "8.0 m apart" in f[0].text


def test_broken_parts_zero_length_spikes_and_short_parts():
    line = np.array([[0.0, 0.0], [0.0, 0.0], [10.0, 0.0], [5.0, 0.01]])
    net = gs.Net([_deck("a", line), _deck("b", _line(0, 0, 1.5, 0, 3))])
    kinds = {c for c, _t in ia._member_defects(net, 0)}
    assert kinds == {"zero_length", "spike"}
    assert {c for c, _t in ia._member_defects(net, 1)} == {"short_part"}


def test_a_duplicate_part_is_named():
    m = [_ground("mway", _line(-800, 0, 800, 0, 200), "motorway"),
         _deck("d1", _line(0, -30, 0, 30, 13)), _deck("d2", _line(0, -30, 0, 30, 13)),
         _ground("s", _line(0, -600, 0, -30)), _ground("n", _line(0, 30, 0, 600))]
    net, systems, audits = _audit(m)
    f = _finds(audits, 2, "duplicate")
    assert len(f) == 1 and {r["earth"] for r in f[0].refs} == {"d1", "d2"}


def test_a_road_cut_in_a_very_short_part_is_held_to_its_shortest_step():
    # (a street dug under a short deck is followed through its joints; a 1 m part among 4 m steps makes the kernel's grade
    # law (grade x its closest two samples) a quarter of the law all along the piece)
    m = [_ground("w", _line(-400, 0, -3, 0, 100), "residential"), _ground("m", _line(-3, 0, -2, 0, 2), "residential"),
         _ground("e", _line(-2, 0, 400, 0, 100), "residential"),
         _deck("deck", _line(0, -6, 0, 6, 4), "tertiary"),
         _ground("s", _line(0, -300, 0, -6), "tertiary"), _ground("n", _line(0, 6, 0, 300), "tertiary")]
    net, systems, audits = _audit(m)
    short = _finds(audits, 2, "short_step")
    assert len(short) == 1 and "1.00 m apart" in short[0].text and short[0].refs[1]["earth"] == "m"
    assert audits[0]["steps"][2]["stats"]["min_step_m"] == 1.0


# ---- step 3: levels ----

def _chain_graph(steps, run_m, grade, need):
    """A road of `steps` steps of run_m at grade, and a clearance asking its far end `need` m over its start."""
    g = ia.Graph(steps + 1)
    for k in range(steps):
        grp = g.add(k, k + 1, grade * run_m, "grade", (0, k))
        g.add(k + 1, k, grade * run_m, "grade", (0, k), grp)
    g.add(steps, 0, -need, "clear", (0,))
    return g.finish()


def test_a_level_cycle_needing_12_7_m_in_130_m_at_6_percent_is_4_9_m_short():
    g = _chain_graph(26, 5.0, 0.06, 12.7)           # (130 m, 7.8 m of rise at 6 %)
    got = ia.negative_cycle(g)
    assert got is not None
    cyc, w = got
    assert abs(w - (7.8 - 12.7)) < 1e-9
    small, w2, _tests = ia.minimal_contradiction(g, cyc)
    assert abs(w2 + 4.9) < 1e-9 and sum(1 for k in small if g.kind[k] == "clear") == 1


def test_the_same_cycle_with_the_rise_the_law_gives_has_heights():
    g = _chain_graph(26, 5.0, 0.06, 7.7)
    assert ia.negative_cycle(g) is None
    d, cyc = ia._spfa(g)
    assert cyc is None and d[26] - d[0] >= 7.7 - 1e-6          # (the heights it finds are the clearance's)
    assert ia.certificate(g, d) <= ia.EPS_M * 2, "(and they satisfy every constraint, checked edge by edge)"


def test_a_cycle_is_reduced_until_each_constraint_of_it_is_needed():
    g = _chain_graph(26, 5.0, 0.06, 12.7)
    a = ia.Graph(27 + 11)                           # (a second road, its own clearance met, and a joint between)
    for k in range(10):
        grp = g.add(27 + k, 28 + k, 0.5, "grade", (1, k))
        g.add(28 + k, 27 + k, 0.5, "grade", (1, k), grp)
    g.add(27, 13, 0.0, "join", (0,))
    g.add(13, 27, 0.0, "join", (0,))
    g.add(37, 27, -2.0, "clear", (1,))              # (10 steps x 0.5 m: 2 m over its start is met)
    g.n, g.adj = 39, None
    g.finish()
    cyc, w = ia.negative_cycle(g)
    small, w2, _t = ia.minimal_contradiction(g, cyc)
    assert abs(w2 + 4.9) < 1e-9
    assert {g.kind[k] for k in small} <= {"grade", "clear"}, "the second road is no part of it"


def _y_of_ramps_over_one_road(run, cross):
    """Ramps A and B join at the origin, diverging; one road's two parts (levels 1 and 3, joined end to end) cross under A
    and over B, `cross` m from the joint along each ramp: its 2 x 6.35 m of clearance asks 12.7 m of rise over 2 x run + the
    road's own 2 x cross... (Shanghai's Yan'an West Road: one road under one ramp and, 35 m on, over its twin)."""
    sin = cross / run
    cos = math.sqrt(1.0 - sin * sin)
    x = run * cos
    far = 2.0 * run
    return [_deck("A", _line(0, 0, far * cos, far * sin, 40), "secondary", 2.0),
            _deck("B", _line(0, 0, far * cos, -far * sin, 40), "secondary", 2.0),
            _deck("R", _line(x, 4 * cross, x, 0, 20), "secondary", 1.0),
            Member(("R", "deck", 1), _line(x, 0, x, -4 * cross, 20), "deck", "secondary", 60, 3.0, 5.0, None)]


def test_a_road_under_one_ramp_and_over_its_twin_is_a_named_negative_cycle():
    net, systems, audits = _audit(_y_of_ramps_over_one_road(49.0, 16.0))
    assert len(systems) == 1
    s3 = audits[0]["steps"][3]
    assert not s3["ok"]
    stats = s3["stats"]
    # (12.7 m of clearance over ~110-165 m of road at 6 %: the tightest cycle takes the footprint's near edge)
    assert 4.0 < stats["short_m"] < 9.0, stats["short_m"]
    assert abs(stats["demand_m"] - 12.7) < 0.01 and stats["grade_factor"] > 1.5
    kinds = [leg["kind"] for leg in stats["legs"]]
    assert kinds.count("clear") == 2 and "join" in kinds
    names = {r["earth"] for leg in stats["legs"] for r in leg["refs"]}
    assert names == {"A", "B", "R"}
    f = _finds(audits, 3, "negative_cycle")[0]
    assert {r["earth"] for r in f.refs} == {"A", "B", "R"} and "short" in f.text
    assert stats["feasible"] is False and stats["true_spacing_feasible"] is False


def test_the_same_road_with_the_run_it_needs_has_heights():
    net, systems, audits = _audit(_y_of_ramps_over_one_road(200.0, 60.0))
    assert audits[0]["steps"][3]["ok"], audits[0]["steps"][3]["stats"].get("short_m")


def test_the_negative_cycle_search_is_deterministic():
    runs = []
    for _ in range(2):
        net, systems, audits = _audit(_y_of_ramps_over_one_road(49.0, 16.0))
        legs = audits[0]["steps"][3]["stats"]["legs"]
        runs.append([(leg["kind"], leg["w"], leg["text"]) for leg in legs])
    assert runs[0] == runs[1]


def test_the_constraints_are_the_kernels_own():
    """The graph is made of gradeshapes.shape_problem's chains and couplings: the coupling count and the grade law agree."""
    m = _y_of_ramps_over_one_road(49.0, 16.0)
    trace = {}
    net, systems = gs.find_systems(m, None, trace)
    sysm = systems[0]
    pcs = gs.pieces(net, sysm)
    smp = gs.sample(net, pcs)
    ground = [np.zeros(len(x["xy"])) for x in smp]
    fixed = lambda i, s: np.zeros(len(s)) if np.ndim(s) else 0.0           # noqa: E731
    cons = ia.Constraints(net, sysm, pcs, smp, ground, fixed)
    assert len(cons.cps) == len(cons.trace["cp"])
    grade = [k for k in range(len(cons.g.eu)) if cons.g.kind[k] == "grade"]
    assert len(grade) == 2 * sum(len(ch["t"]) - 1 for ch in cons.chains)
    assert all(abs(cons.g.ew[k] - cons.chains[cons.g.ref[k][0]]["grade"] * cons.chains[cons.g.ref[k][0]]["step"]) < 1e-12
               for k in grade)


def test_the_trace_changes_nothing_of_the_answer():
    m = _y_of_ramps_over_one_road(49.0, 16.0)
    net1, s1 = gs.find_systems(m)
    net2, s2 = gs.find_systems(m, None, {})
    assert repr([{k: v for k, v in s.items() if k != "crossings"} for s in s1]) == \
        repr([{k: v for k, v in s.items() if k != "crossings"} for s in s2])
    assert [c["moves"] for s in s1 for c in s["crossings"]] == [c["moves"] for s in s2 for c in s["crossings"]]


# ---- step 4: where the level resets ----

def _two_interchanges(link_m):
    """Two overpasses over two motorways, a road on the ground of link_m between the first's north end and the second's
    south end."""
    y1 = 30.0 + link_m
    return [_ground("m1", _line(-800, 0, 800, 0, 200), "motorway"),
            _deck("d1", _line(0, -30, 0, 30, 13)),
            _ground("s1", _line(0, -600, 0, -30)),
            _ground("link", _line(0, 30, 0, y1, 30)),
            _ground("m2", _line(-800, y1 + 30, 800, y1 + 30, 200), "motorway"),
            _deck("d2", _line(0, y1, 0, y1 + 60, 13)),
            _ground("n2", _line(0, y1 + 60, 0, y1 + 700))]


def test_two_interchanges_joined_only_by_a_ground_road_are_two_systems_and_both_pass():
    # (short decks, held at their ground, have no ramp: where their roads are back on the ground is the end of each, and
    # the road between is a boundary held at the ground, of any length, not a way on to the next structure)
    for link_m in (60.0, 250.0, 450.0, 2000.0):
        net, systems, audits = _audit(_two_interchanges(link_m))
        assert len(systems) == 2 and all(len(s["seeds"]) == 1 for s in systems), link_m
        assert all(a["steps"][4]["ok"] for a in audits), link_m
        assert not _finds(audits, 4, "merged_over_a_ground_node")


def test_a_road_dug_under_a_deck_between_the_ends_is_no_ground_node():
    # (the link goes under a third deck: its ground is not free)
    m = _two_interchanges(250.0) + [_deck("d3", _line(-25, 150, 25, 150, 8), "tertiary")]
    net, systems, audits = _audit(m)
    assert len(systems) == 3, "d3 is a system of its own (it digs the link under it)"
    assert not _finds(audits, 4, "merged_over_a_ground_node")


def _long_overpasses(link_m):
    """Two long viaducts, each over its motorway, the first's north end and the second's south end on one road on the
    ground of link_m. Long enough to rise for the motorway: a ramp then, where a short deck is held at its ground."""
    y1 = 250.0 + link_m
    return [_ground("m1", _line(-800, 0, 800, 0, 200), "motorway"),
            _deck("d1", _line(0, -250, 0, 250, 60)),
            _ground("s1", _line(0, -900, 0, -250)),
            _ground("link", _line(0, 250, 0, y1, 30)),
            _ground("m2", _line(-800, y1 + 250, 800, y1 + 250, 200), "motorway"),
            _deck("d2", _line(0, y1, 0, y1 + 500, 60)),
            _ground("n2", _line(0, y1 + 700, 0, y1 + 1200))]


def _interchanges(members):
    net, found = gs.find_interchanges(members)
    return [(sorted(str(net.m[i].key[0]) for i in f.sysm["seeds"]),
             sorted(sorted(str(net.m[i].key[0]) for i in p.sysm["seeds"]) for p in f.parts)) for f in found]


def test_viaducts_whose_ramps_meet_on_a_road_are_one_interchange_of_two_systems():
    # (an interchange ends where its road is back on the ground: the ramps of two viaducts 60 m apart on one road meet,
    # the road is not on the ground between them. Each stays a system of its own, solved as it is)
    assert _interchanges(_long_overpasses(60.0)) == [(["d1", "d2"], [["d1"], ["d2"]])]
    net, systems = gs.find_systems(_long_overpasses(60.0))
    assert sorted(str(net.m[i].key[0]) for s in systems for i in s["seeds"]) == ["d1", "d2"] and len(systems) == 2


def test_viaducts_whose_ramps_are_back_on_the_ground_before_they_meet_are_two_interchanges():
    assert _interchanges(_long_overpasses(2000.0)) == [(["d1"], []), (["d2"], [])]


def test_what_a_ramp_reaches_does_not_depend_on_the_order_of_the_members():
    for link_m in (60.0, 2000.0):
        m = _long_overpasses(link_m)
        assert _interchanges(m) == _interchanges(m[::-1]), link_m


# ---- naming ----

def test_a_finding_names_the_feature_its_osm_way_class_level_and_place():
    meta = {"ramp": dict(osm="w123@4", name="Ramp A", layer="2")}
    net, systems, audits = _audit(_overpass(ramp_far_end=False), meta=meta, ll=lambda x, y: (31.0 + y * 1e-5, 121.0 + x * 1e-5))
    f = _finds(audits, 1, "dangling_end")[0]
    assert "ramp (OSM w123@4) secondary_link bridge level 1" in f.text and '"Ramp A"' in f.text
    assert f.where == "31.00090,121.00000"
    assert f.refs[0]["osm"] == "w123@4" and f.refs[0]["layer"] == "2"


# ---- the report ----

def test_the_report_says_per_step_pass_or_fail_and_names_the_contradiction():
    net, systems, audits = _audit(_y_of_ramps_over_one_road(49.0, 16.0))
    text = ia.render("y", audits)
    assert "| 3 levels | 0 | 1 | 0 |" in text and "First step to fail: step 1" in text   # (the synthetic decks end in the air: step 1 fails first)
    assert "### Contradiction, system 0" in text and "12.70 m of rise" in text
    net, systems, audits = _audit(_overpass())
    assert "First step to fail: none." in ia.render("ok", audits)


# ---- a road passing over itself (Nanpu Bridge's spiral: OSM layers 1 to 3 of one road) ----

def _dense(p):
    return np.concatenate([np.linspace(p[k], p[k + 1], 12, endpoint=False) for k in range(len(p) - 1)] + [p[-1:]])


def _loop(side):
    """One road's two decks (levels 1 and 2) joined end to end: the first runs 60 m, the second turns round `side` m on
    each of three sides and crosses the first at 40 m."""
    first = np.array([[0.0, 0.0], [60.0, 0.0]])
    second = np.array([[60.0, 0.0], [60.0, side], [40.0, side], [40.0, -side]])
    return [Member(("road", "deck", 1), _dense(first), "deck", "secondary", 60, 1.0, 5.0, None),
            Member(("road", "deck", 2), _dense(second), "deck", "secondary", 60, 2.0, 5.0, None)]


def test_a_road_passing_over_itself_with_the_run_it_needs_has_heights_and_is_no_split_road():
    net, systems, audits = _audit(_loop(100.0))
    assert len(systems) == 1 and len(systems[0]["crossings"]) >= 1
    assert audits[0]["steps"][3]["ok"]
    assert not _finds(audits, 2, "road_over_itself_too_close")


def test_a_road_over_itself_within_the_run_its_clearance_takes_is_named_in_steps_2_and_3():
    net, systems, audits = _audit(_loop(20.0))
    near = _finds(audits, 2, "road_over_itself_too_close")
    assert len(near) == 1 and {r["earth"] for r in near[0].refs} == {"road"}, [f.text for f in _finds(audits, 2)]
    s3 = audits[0]["steps"][3]
    assert not s3["ok"] and s3["stats"]["short_m"] > 0.5
    assert {r["earth"] for leg in s3["stats"]["legs"] for r in leg["refs"]} == {"road"}


# ---- a loose end is what the map has at its node ----

def _facts(**kw):
    out = dict(near=[], inside=[], others=[], connector=None, feature_end=None)
    out.update(kw)
    return out


def _with_map(members, facts_of, meta=None):
    """The audit with endinfo(member, end) -> facts_of(the member's Earth id)."""
    box = {}

    class Capture(ia.Namer):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            box["namer"] = self

    def endinfo(i, e):
        return facts_of(box["namer"].net.m[i].key[0])
    with mock.patch.object(ia, "Namer", Capture):
        return ia.audit_members(members, meta=meta, endinfo=endinfo)[2]


def _dangling_ramp_with(facts):
    return _with_map(_overpass(ramp_far_end=False), lambda key: facts if key == "ramp" else _facts())


def test_a_deck_end_whose_road_goes_on_outside_the_region_is_no_loose_end():
    row = ("north-road", "ground", "secondary", 0.0, 0.4, ("north-road", "ground", 7))
    audits = _dangling_ramp_with(_facts(near=[row]))
    f = _finds(audits, 1, "end_beyond_region")
    assert len(f) == 1 and f[0].level == "note" and "outside" in f[0].text and "north-road" in f[0].text
    assert audits[0]["steps"][1]["ok"] and not _finds(audits, 1, "dangling_end")


def test_a_deck_end_at_a_road_the_shapes_do_not_take_is_a_note_naming_it():
    other = ("path-1", "footway", "-1", "w123@4", False, 0, {})
    audits = _dangling_ramp_with(_facts(feature_end=1, connector="c1", others=[other]))
    f = _finds(audits, 1, "end_at_non_member")
    assert len(f) == 1 and f[0].level == "note" and "w123@4" in f[0].text and "footway" in f[0].text
    assert audits[0]["steps"][1]["ok"]


def test_a_deck_end_the_map_leaves_alone_is_a_map_defect_not_a_pipeline_failure():
    audits = _dangling_ramp_with(_facts(feature_end=1, connector="c9"))
    f = _finds(audits, 1, "end_unconnected")
    assert len(f) == 1 and f[0].level == "map" and "c9" in f[0].text
    assert audits[0]["steps"][1]["ok"]
    assert "Map defect, end_unconnected" in ia.render("x", audits)


def test_a_road_of_the_net_at_the_end_with_no_joint_is_the_pipelines_failure():
    row = ("far", "ground", "secondary", 0.0, 0.5, ("far", "ground", 0))
    audits = _dangling_ramp_with(_facts(inside=[row]))
    f = _finds(audits, 1, "end_not_joined")
    assert len(f) == 1 and f[0].level == "fail" and not audits[0]["steps"][1]["ok"]


def test_a_one_way_disagreement_the_maps_own_roads_explain_is_a_note_else_a_map_defect():
    m = _overpass()
    meta = {"deck": dict(oneway=True, dir=1), "ramp": dict(oneway=True, dir=-1)}
    # (the deck and the ramp both flow into the joint: a road of the map that vehicles enter at that node takes them on)
    taker = ("feeder", "secondary", None, "w1@1", True, 0, dict(oneway=True, dir=1))
    for others, code, level in (([taker], "oneway_fed_outside", "note"), ([], "oneway_map", "map")):
        audits = _with_map(m, lambda key, others=others: _facts(feature_end=1, connector="cx", others=others), meta)
        f = _finds(audits, 1, code)
        assert f and all(x.level == level for x in f), (code, [x.text for x in _finds(audits, 1)])
        assert not _finds(audits, 1, "oneway_conflict")


def test_a_gap_between_a_deck_end_and_its_own_roads_next_part_is_the_pipelines():
    row = ("ramp", "ground", "secondary", 0.0, 4.0, ("ramp", "ground", 1))
    audits = _dangling_ramp_with(_facts(own_gap=row))
    f = _finds(audits, 1, "gap_to_own_part")
    assert len(f) == 1 and f[0].level == "fail" and "4.0 m short" in f[0].text


def test_a_separated_end_the_map_leaves_unconnected_is_a_map_defect():
    m = [_ground("mway", _line(-800, 0, 800, 0, 200), "motorway"),
         _deck("low", _line(-60, 30, 60, 30, 40), nodes=frozenset({"a", "b"})),
         _deck("high", _line(0, -30, 0, 30, 13), level=2.0, nodes=frozenset({"c", "d"})),
         _ground("s", _line(0, -600, 0, -30))]
    audits = _with_map(m, lambda key: _facts(feature_end=1, connector="cz") if key == "high" else _facts())
    f = _finds(audits, 1, "end_unconnected")
    assert len(f) == 1 and f[0].level == "map" and "low" in f[0].text and not _finds(audits, 1, "end_separated")
    assert not [x for x in _finds(audits, 1) if x.level == "fail" and x.refs[0]["earth"] == "high"]


def test_a_deck_end_where_its_own_road_goes_on_as_a_tunnel_nobody_made_a_member_of_is_a_note():
    audits = _dangling_ramp_with(_facts(own_next=("tunnel", 0.42, 1.0)))
    f = _finds(audits, 1, "end_at_tunnel_network")
    assert len(f) == 1 and f[0].level == "note" and "its own map road goes on as a tunnel part" in f[0].text
    assert audits[0]["steps"][1]["ok"]
