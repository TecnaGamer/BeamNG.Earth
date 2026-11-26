"""The 1 km overview in 3D (server/evals/earth_view.py) and the eval's 3 x 2 picture (server/evals/earth_eval.py); no Earth tree needed.

    python -m pytest tests
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import earth_checks as ck  # noqa: E402
import earth_eval as ee  # noqa: E402
import earth_view as ev  # noqa: E402


def block(n=1100, x0=-550.0, y0=-550.0, slope=0.01):
    """A height lattice n x n from (x0, y0) 1 m apart, rising to the east."""
    return np.tile(100.0 + slope * (x0 + np.arange(n)), (n, 1)), x0, y0


def test_the_overview_ground_covers_the_square_every_step_metres():
    z, x0, y0 = block()
    box = ev.overview_box(0.0, 0.0)
    tris, cls = ev.overview_ground(z, x0, y0, box, step=2)
    assert (cls == ev.GROUND).all()
    xs, ys = tris[..., 0], tris[..., 1]
    assert xs.min() <= -499 and xs.max() >= 499 and ys.min() <= -499 and ys.max() >= 499
    assert xs.min() >= -502 and xs.max() <= 502
    assert len(tris) == 2 * 500 * 500 or abs(len(tris) - 2 * 501 * 501) <= 2 * 1002    # 2 m cells
    near = np.abs(tris[:, 0, 0] - 100.0) < 0.5
    assert np.allclose(tris[near][:, 0, 2], 101.0, atol=0.05)                          # the lattice's own heights


def test_the_overview_marks_the_ground_that_rose_and_fell():
    z, x0, y0 = block(slope=0.0)
    dz = np.zeros_like(z)
    dz[550:560, 550:560] = 1.0          # x 0..10, y 0..10 rose
    dz[700:710, 700:710] = -2.0         # x 150..160, y 150..160 fell
    dz[400:410, 400:410] = 0.1          # x -150.. less than CHANGED_M: not marked
    tris, cls = ev.overview_ground(z, x0, y0, ev.overview_box(0.0, 0.0), dz=dz)
    c = tris.mean(axis=1)
    rose, fell = c[cls == ev.ROSE], c[cls == ev.FELL]
    assert len(rose) and len(fell)
    assert (np.abs(rose[:, :2] - 5) <= 7).all() and (np.abs(fell[:, :2] - 155) <= 7).all()
    assert not ((cls != ev.GROUND) & (np.abs(c[:, 0] + 145) < 4) & (np.abs(c[:, 1] + 145) < 4)).any()


def test_the_overview_keeps_only_the_meshes_lying_in_the_square():
    box = ev.overview_box(0.0, 0.0)
    inside = np.array([[[0, 0, 0], [10, 0, 0], [0, 10, 0]]], float)
    crossing = np.array([[[490, 0, 0], [900, 0, 0], [490, 10, 0]]], float)
    got = ev.inside_triangles(np.concatenate([inside, crossing]), box)
    assert len(got) == 1 and np.allclose(got[0], inside[0])


def test_the_close_up_box_is_a_closed_line_on_the_ground():
    z, x0, y0 = block()
    line = ev.box_outline(z, x0, y0, (-40, -40, 40, 40), lift=1.0)
    assert np.allclose(line[0, :2], line[-1, :2])
    assert np.allclose(line[:, 2], 101.0 + 0.01 * line[:, 0], atol=0.02)


def test_the_overview_camera_stands_south_west_looking_down():
    z, x0, y0 = block()
    tris, cls = ev.overview_ground(z, x0, y0, ev.overview_box(0.0, 0.0), step=10)
    cam = ev.overview_camera(tris, np.array([0.0, 0.0, 100.0]), 520, 360)
    assert cam.fwd[0] > 0 and cam.fwd[1] > 0 and cam.fwd[2] < 0                       # looking north-east and down
    elev = np.degrees(np.arcsin(-cam.fwd[2]))
    assert abs(elev - ev.OVERVIEW_ELEV) < 1e-6
    sx, sy, _ = cam.project(tris.reshape(-1, 3))
    assert sx.min() >= 0 and sx.max() <= 520 and sy.min() >= 0 and sy.max() <= 360    # the whole square in the picture


def test_the_overview_renders_the_marked_ground_in_its_colours():
    z, x0, y0 = block(slope=0.0)
    dz = np.zeros_like(z)
    dz[450:650, 450:650] = -3.0
    tris, cls = ev.overview_scene(z, x0, y0, ev.overview_box(0.0, 0.0), None, {}, dz)
    cam = ev.overview_camera(tris, np.array([0.0, 0.0, 100.0]), 200, 140)
    img, used = ev.render(tris, cls, cam, ss=1)
    assert ev.FELL in set(int(u) for u in used) and ev.ROSE not in set(int(u) for u in used)


def test_the_eval_picture_is_3_x_2(tmp_path):
    from PIL import Image
    PW, PH = 120, 80
    shade = {"overview": 60, "top": 120, "from_SW": 180}
    for side in ee.SIDES:
        d = tmp_path / "j" / side
        d.mkdir(parents=True)
        for v, g in shade.items():
            Image.new("RGB", (PW, PH), (g, g, g if side == "before" else 0)).save(d / f"view_panel_{v}.png")

    class A:
        before, after, out, image, views = "b", "a", str(tmp_path), f"{PW}x{PH}", "overview,top,SW"
    res = {"name": "j", **ee.compare(grids(), grids(deck_z=2.0))}
    res["_diff"] = np.ones((PH, PW, 3))
    path = ee.eval_picture({"name": "j", "what": "a test"}, A, res, lambda m: None)
    pic = np.asarray(Image.open(path).convert("RGB"))
    assert pic.shape[1] == 3 * PW + 2 * ev.PANEL_GAP                           # three columns
    top = 62
    for col, v in enumerate(shade):
        x = col * (PW + ev.PANEL_GAP) + PW // 2
        assert pic[top + PH // 2, x].tolist() == [shade[v]] * 3                    # before on top
        assert pic[top + PH + ev.PANEL_GAP + PH // 2, x].tolist() == [shade[v], shade[v], 0]     # after under it
    assert (tmp_path / "j_diff.png").is_file()


def grids(deck_z=4.0):
    tris = np.concatenate([quad(-10, -10, 10, 10, 0.0), quad(-2, 5, 2, 8, deck_z)])
    return ev.surface_grid(tris, np.array([ev.GROUND] * 2 + [ev.DECK] * 2, np.int8), (-10, -10, 10, 10), step=0.5)


def quad(x0, y0, x1, y1, z):
    a, b, c, d = (x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)
    return np.array([[a, b, c], [a, c, d]], np.float64)


def bed_and_deck(sunk=0.0):
    """(z, x0, y0, tris, cls, lines): a road along x = 0 on the ground (0 m) under a rail bridge along y = 0 over |x| <= 6
    (its deck 6..7.5 m), the track's bed at 7.2 m from |x| = 4.5 on (its end under the deck's last metre and a half, as
    Earth's bed plan's round end); sunk: the deck that far into the road instead. The lattice from x0 = -41: the overview's
    2 m samples at odd x."""
    n = 83
    xs = -41.0 + np.arange(n)
    z = np.tile(np.where(np.abs(xs) >= 4.5, 7.2, 0.0), (n, 1))
    x0 = y0 = -41.0
    lo = 6.0 - sunk
    deck = ev.box_triangles((0.0, 0.0, lo), (1.0, 0.0, 0.0), 12.0, 5.0, 1.5)
    lines = {"road": [(np.array([[0.0, -40.0], [0.0, 40.0]]), [])],
             "rail": [(np.array([[-40.0, 0.0], [40.0, 0.0]]), [(34.0 / 80.0, 46.0 / 80.0, "bridge")])]}
    box = (-30.0, -30.0, 30.0, 30.0)
    g, gc = ev.overview_ground(z, x0, y0, box, step=2)
    return z, x0, y0, np.concatenate([g, deck]), np.concatenate([gc, np.full(len(deck), ev.RAIL, np.int8)]), lines


def test_the_overview_checks_its_crossings_on_the_lattices_own_ground():
    z, x0, y0, tris, cls, lines = bed_and_deck()
    _cut, coarse = ck.penetrations(tris, cls, lines, ev.CHECK_CLASSES)
    assert [c[2] for c in coarse] == [True], "(the case: a 2 m triangle over the bed's edge reaches the road's corridor)"
    cut, got = ev.overview_cuts(z, x0, y0, None, tris, cls, lines)
    assert [c[2] for c in got] == [False] and not cut.any()


def test_the_overview_still_finds_a_deck_sunk_into_the_road():
    z, x0, y0, tris, cls, lines = bed_and_deck(sunk=6.5)
    cut, got = ev.overview_cuts(z, x0, y0, None, tris, cls, lines)
    assert [c[2] for c in got] == [True]
    assert cut[cls == ev.RAIL].any() and cut[cls == ev.GROUND].any()
