"""server/evals/earth_eval.py and earth_view's surface grid: the before / after numbers, the diff picture, the junction set's arguments;
no Earth tree needed.

    python -m pytest tests
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import earth_eval as ee  # noqa: E402
import earth_view as ev  # noqa: E402


def quad(x0, y0, x1, y1, z):
    a, b, c, d = (x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)
    return np.array([[a, b, c], [a, c, d]], np.float64)


def scene(deck_z=4.0, road_z=0.0):
    tris = np.concatenate([quad(-10, -10, 10, 10, 0.0), quad(-2, -10, 2, 10, road_z), quad(-2, 5, 2, 8, deck_z)])
    return tris, np.array([ev.GROUND] * 2 + [ev.ROAD] * 2 + [ev.DECK] * 2, np.int8)


def test_the_surface_grid_sees_the_deck_over_the_road_over_the_ground_south_first():
    tris, cls = scene()
    c, z = ev.surface_grid(tris, cls, (-10, -10, 10, 10), step=0.5)
    assert c.shape == (40, 40)
    assert c[2, 2] == ev.GROUND and abs(z[2, 2]) < 1e-6           # (south west corner)
    assert c[2, 20] == ev.ROAD and abs(z[2, 20]) < 1e-6           # the road wins over the ground it lies on
    assert c[30, 20] == ev.DECK and abs(z[30, 20] - 4.0) < 1e-6   # y = 5.25: the deck, at its own height
    assert c[30, 2] == ev.GROUND


def test_nothing_drawn_is_class_minus_one_and_nan():
    c, z = ev.surface_grid(quad(0, 0, 5, 5, 1.0), np.array([ev.ROAD] * 2, np.int8), (-5, -5, 5, 5), step=1.0)
    assert c[0, 0] == -1 and np.isnan(z[0, 0]) and c[7, 7] == ev.ROAD


def grids(**kw):
    tris, cls = scene(**kw)
    return ev.surface_grid(tris, cls, (-10, -10, 10, 10), step=0.5)


def test_the_same_tree_is_the_same():
    r = ee.compare(grids(), grids())
    assert not r["changed"] and r["kind_changed"] == 0.0 and r["ground"]["max"] == 0.0 and r["deck"]["n"] > 0


def test_a_deck_lowered_is_changed_and_measured():
    r = ee.compare(grids(), grids(deck_z=2.5))
    assert r["changed"] and r["kind_changed"] == 0.0
    assert abs(r["deck"]["max"] - 1.5) < 1e-6 and r["road"]["max"] < 1e-6


def test_a_small_move_is_minor_not_changed():
    r = ee.compare(grids(), grids(deck_z=4.1))
    assert r["verdict"] == "minor" and not r["changed"]
    assert ee.compare(grids(), grids())["verdict"] == "same"


def test_a_deck_gone_changes_what_the_surface_is():
    tris, cls = scene()
    after = ev.surface_grid(tris[:4], cls[:4], (-10, -10, 10, 10), step=0.5)
    r = ee.compare(grids(), after)
    assert r["changed"] and r["kind_changed"] > 0.0


def test_the_diff_image_is_white_where_same_red_where_risen_grey_where_changed():
    b = grids()
    a = grids(deck_z=6.0)
    img = ee.diff_image(b, a, 80, 80, ee.SAME_M)
    assert img.shape == (80, 80, 3)
    assert (img[75, 5] == 1.0).all()                       # south west: the ground, the same (north up: last rows south)
    red = img[80 - 31 * 2, 40]                             # the deck (row 30 of the grid, south first, 2 px a sample)
    assert red[0] > 0.8 and red[1] < 0.6 and red[2] < 0.6
    tris, cls = scene()
    gone = ee.diff_image(b, ev.surface_grid(tris[:4], cls[:4], (-10, -10, 10, 10), step=0.5), 80, 80)
    assert np.allclose(gone[80 - 31 * 2, 40], ee.KIND_RGB)


def test_junction_args_anchor_point_and_lines():
    j = {"name": "p", "at": [-31.889125, 115.91902], "radius": 80}
    args = ee.junction_args(j)
    assert "--anchor=-31.89,115.92" in args and "--at=-31.889125,115.91902" in args
    assert args[args.index("--aid") + 1] == "9" and args[args.index("--radius") + 1] == "80.0"
    j = {"name": "q", "anchor": [42.17, 13.83], "aid": 2, "q": 0.015625, "xy": [-446, 880],
         "geometry": ["geometry/ss1.json"]}
    args = ee.junction_args(j)
    assert "--xy=-446,880" in args and args[args.index("--q") + 1] == "0.015625"
    assert os.path.isfile(args[args.index("--geometry") + 1])


def test_the_set_is_well_formed():
    js = ee.load_set(ee.DEFAULT_SET)
    names = [j["name"] for j in js]
    assert len(names) == len(set(names)) >= 5
    for j in js:
        assert j.get("what") and ("at" in j or ("xy" in j and "anchor" in j))
        ee.junction_args(j)
    assert any(abs(j.get("at", [0, 0])[0] - 35.463744) < 1e-6 for j in js)       # (Yokohama)


def test_the_report_names_every_junction(tmp_path):
    class A:
        before, after, out = "b", "a", str(tmp_path)
    r1 = {"name": "x", **ee.compare(grids(), grids(deck_z=2.0))}
    r2 = {"name": "y", "error": "earth_view exited 1"}
    table = ee.write_report([r1, r2], A)
    assert "| x | **changed** |" in table and "| y | error" in table
    data = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert [j["name"] for j in data["junctions"]] == ["x", "y"]


def test_the_before_side_comes_from_a_former_run_when_given(tmp_path):
    import types
    a = types.SimpleNamespace(out=str(tmp_path / "run"), before_from=str(tmp_path / "upstream"))
    assert ee.side_dir(a, "x", "before") == os.path.join(a.before_from, "x", "before")
    assert ee.side_dir(a, "x", "after") == os.path.join(a.out, "x", "after")
    a.before_from = None
    assert ee.side_dir(a, "x", "before") == os.path.join(a.out, "x", "before")


def test_a_missing_captured_before_side_is_an_error_not_a_build(tmp_path):
    import types
    a = types.SimpleNamespace(out=str(tmp_path / "run"), before_from=str(tmp_path / "upstream"))
    logs = []
    err = ee.build_side({"name": "x"}, "before", "unused-tree", a, logs.append)
    assert err and "no before side" in err
    os.makedirs(tmp_path / "upstream" / "x" / "before")
    (tmp_path / "upstream" / "x" / "before" / "view_surface.npz").write_bytes(b"")
    assert ee.build_side({"name": "x"}, "before", "unused-tree", a, logs.append) is None


def test_the_page_lists_the_worst_junction_first_with_both_sides(tmp_path):
    import eval_page
    from PIL import Image
    run = tmp_path / "run"
    run.mkdir()
    rows = [{"name": "clean", "what": "a", "changed": False, "verdict": "same",
             "checks": {"vehicles_hit": [2, 0], "crossings_cut": [0, 0]}},
            {"name": "bad", "what": "b", "changed": True, "worse": ["vehicles_hit"],
             "checks": {"vehicles_hit": [1, 5], "crossings_cut": [0, 1]}}]
    (run / "report.json").write_text(json.dumps({"before": str(tmp_path), "after": str(tmp_path), "junctions": rows}))
    Image.new("RGB", (8, 8)).save(run / "bad.png")
    page = eval_page.build(str(run), str(tmp_path / "page"))
    doc = open(page, encoding="utf-8").read()
    assert doc.index('id="bad"') < doc.index('id="clean"')
    assert "img/bad.jpg" in doc and (tmp_path / "page" / "img" / "bad.jpg").is_file()
    assert "1 &rarr; <b>5</b>" in doc and "3 &rarr; 5" in doc     # (a row, and the column's totals)


def test_a_before_only_capture_builds_no_after_side(tmp_path, monkeypatch):
    import types
    built = []
    monkeypatch.setattr(ee, "build_side", lambda j, side, tree, a, log: built.append(side))
    a = types.SimpleNamespace(out=str(tmp_path), before="b", after="a", sides=["before"])
    res = ee.run_junction({"name": "x"}, a, lambda m: None)
    assert built == ["before"] and "error" not in res


def test_a_sides_timing_is_its_build_and_the_solver_passes_it_logged(tmp_path):
    side = tmp_path / "after"
    (side / "data").mkdir(parents=True)
    (side / "data" / "timing.json").write_text(json.dumps({"block_s": 100.0, "tiles_s": 50.0, "tiles": 3}))
    (side / "view.log").write_text(
        "WARNING earthd.grading: grading: road laws 14/1/2: 3 of 9 plans changed, 4 chains, 2 couplings, 99 samples, "
        "1 networks (0 infeasible, left as they are), 1.50 s\n"
        "WARNING earthd.grading: grading: SHAPES pid 7 region 14/1/2: 9 cells, 5 members, 1 systems (0 infeasible, left "
        "as they are), 1 roads graded, find 0.25 s, solve 2.25 s\n")
    t = ee.load_timing(str(side))
    assert t["build_s"] == 150.0 and t["laws_s"] == 1.5 and t["shapes_s"] == 2.5 and t["solver_s"] == 4.0
    assert ee.load_timing(str(tmp_path / "none")) == {}


def test_the_build_time_table_has_the_change_and_the_total():
    rows = ee.timing_rows([{"name": "j", "timing": {"before": {"build_s": 100.0}, "after": {"build_s": 130.0, "solver_s": 20.0}}},
                           {"name": "k", "timing": {"before": {}, "after": {"build_s": 50.0}}}])
    assert "| j | 100.0 | 130.0 | +30 % | 20.0 |" in rows
    assert "| k | - | 50.0 | - | - |" in rows
    assert "| **total** | 100 | 130 | +30 % | 20 |" in rows


def test_a_watched_build_is_stopped_at_its_first_failing_line(tmp_path):
    # (--fail-fast: Shanghai solved on two hours past its first infeasible system)
    script = ("import sys, time\nprint('building', flush=True)\n"
              "print('ERROR earthd.grading: grading: SHAPES infeasible system in region 14/1/2', flush=True)\n"
              "time.sleep(120)\n")
    t = __import__("time").time()
    rc, line = ee.run_watched([sys.executable, "-c", script], str(tmp_path / "view.log"), dict(os.environ), str(tmp_path),
                              ee.FAIL_LINES)
    assert "infeasible system" in line
    assert __import__("time").time() - t < 60, "stopped, not waited for"
    assert "building" in (tmp_path / "view.log").read_text(encoding="utf-8")


def test_an_unwatched_or_clean_build_runs_to_its_end(tmp_path):
    script = "print('WARNING region 1: 5 systems (0 infeasible, left as they are)')\n"
    for watch in (None, ee.FAIL_LINES):
        rc, line = ee.run_watched([sys.executable, "-c", script], str(tmp_path / "view.log"), dict(os.environ),
                                  str(tmp_path), watch)
        assert rc == 0 and line is None


def test_red_is_an_error_or_a_hit_or_a_cut_after_the_change():
    assert ee.red({"error": "failed early: x"}) == "failed early: x"
    assert ee.red({"checks": {"vehicles_hit": [3, 0], "crossings_cut": [0, 0], "overview_crossings_cut": [None, None]}}) is None
    assert "2 " in ee.red({"checks": {"vehicles_hit": [0, 0], "crossings_cut": [0, 2]}})


def test_the_junctions_red_last_time_run_first_the_quickest_first():
    js = [{"name": n} for n in ("green", "new", "heavy_red", "quick_red")]
    hist = {"green": {"red": False, "s": 10}, "heavy_red": {"red": True, "s": 10000}, "quick_red": {"red": True, "s": 300}}
    assert [j["name"] for j in ee.fail_first(js, hist)] == ["quick_red", "heavy_red", "new", "green"]


def test_fail_fast_stops_at_the_first_red_junction_and_writes_the_history(tmp_path, monkeypatch):
    ran = []

    def fake(j, a, log):
        ran.append(j["name"])
        return {"name": j["name"], "checks": {"vehicles_hit": [0, 1 if j["name"] == "b" else 0]}}
    monkeypatch.setattr(ee, "run_junction", fake)
    monkeypatch.setattr(ee, "load_set", lambda p: [{"name": n} for n in ("a", "b", "c")])
    monkeypatch.setattr(ee, "write_report", lambda results, a: "report")
    hist = tmp_path / "h.json"
    rc = ee.main(["--before", str(tmp_path), "--after", str(tmp_path), "--out", str(tmp_path / "o"), "--fail-fast",
                  "--history", str(hist)])
    assert rc == 1 and ran == ["a", "b"]
    h = json.loads(hist.read_text())
    assert h["b"]["red"] and not h["a"]["red"] and "c" not in h
    ran.clear()
    ee.main(["--before", str(tmp_path), "--after", str(tmp_path), "--out", str(tmp_path / "o"), "--fail-fast",
             "--history", str(hist)])
    assert ran == ["b"], "the red one first next time, and stopped there"
