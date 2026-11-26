"""server/evals/earth_view.py: the 3D debug viewer's pure parts (the ground mesh, the camera, the z-buffer rasteriser, the shading, the tile
JSON's meshes, the picture's frame) on synthetic meshes; no Earth tree needed.

    python -m pytest tests
"""
import os
import sys
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import earth_view as ev  # noqa: E402


def quad(x0, y0, x1, y1, z):
    """Two triangles of the flat rectangle at height z."""
    a, b, c, d = (x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)
    return np.array([[a, b, c], [a, c, d]], np.float64)


def px(img, x, y):
    return img[int(y), int(x)]


# ---- rasteriser ------------------------------------------------------------------------------------------------------
def test_nearer_triangle_wins_in_either_order():
    far = [[2, 2], [30, 2], [2, 30]]
    near = [[4, 4], [28, 4], [4, 28]]                   # inside the far one
    for first_near in (False, True):
        scr = np.array([near, far] if first_near else [far, near], np.float64)
        depth = np.array([[5.0] * 3, [10.0] * 3] if first_near else [[10.0] * 3, [5.0] * 3])
        tri, z = ev.rasterize(scr, depth, 32, 32)
        near_i, far_i = (0, 1) if first_near else (1, 0)
        assert tri[10, 10] == near_i and z[10, 10] == pytest.approx(5.0, abs=1e-6)
        assert tri[3, 3] == far_i and z[3, 3] == pytest.approx(10.0, abs=1e-6)
        assert tri[1, 1] == -1 and np.isinf(z[1, 1])


def test_depth_is_interpolated_across_a_tilted_triangle():
    scr = np.array([[[0, 0], [20, 0], [0, 20]]], np.float64)
    depth = np.array([[0.0, 20.0, 40.0]])
    tri, z = ev.rasterize(scr, depth, 24, 24)
    assert tri[5, 5] == 0
    assert z[5, 5] == pytest.approx(0.0 + 20.0 * 5.5 / 20.0 + 40.0 * 5.5 / 20.0, abs=1e-5)      # (the pixel centre's weights)


def test_a_tiled_surface_has_no_gaps_even_with_degenerate_triangles_in_the_list():
    # a lattice of cells (every pixel centre is inside a cell), with a zero-area triangle and an off-screen one first: the old
    # per-bucket filter misaligned the arrays after dropping them
    tris = []
    for j in range(12):
        for i in range(12):
            x, y = 3.0 + i * 5.3, 2.0 + j * 4.7
            tris += [[[x, y], [x + 5.3, y], [x + 5.3, y + 4.7]], [[x, y], [x + 5.3, y + 4.7], [x, y + 4.7]]]
    bad = [[[5, 5], [5, 5], [5, 5]], [[500, 500], [510, 500], [500, 510]], [[-40, -40], [-30, -40], [-40, -30]]]
    scr = np.array(bad + tris, np.float64)
    depth = np.zeros((len(scr), 3))
    tri, _ = ev.rasterize(scr, depth, 64, 60)
    xs, ys = np.meshgrid(np.arange(64), np.arange(60))
    inside = (xs >= 4) & (xs < 3 + 12 * 5.3 - 1) & (ys >= 3) & (ys < 2 + 12 * 4.7 - 1)
    assert (tri[inside] >= len(bad)).all()


def test_a_triangle_larger_than_a_batch_is_cut_and_still_filled():
    scr = np.array([[[-100, -100], [400, -100], [-100, 400]]], np.float64)
    tri, _ = ev.rasterize(scr, np.zeros((1, 3)), 120, 90, kmax=16)
    assert (tri >= 0).all() and (tri == 0).all()
    scr = np.array([[[10, 10], [110, 10], [10, 80]]], np.float64)                 # half of the image only
    tri, _ = ev.rasterize(scr, np.zeros((1, 3)), 120, 90, kmax=16)
    assert abs(int((tri >= 0).sum()) - 0.5 * 100 * 70) < 150
    assert tri[15, 15] == 0 and tri[75, 100] == -1


def test_rasterize_of_nothing():
    tri, z = ev.rasterize(np.zeros((0, 3, 2)), np.zeros((0, 3)), 8, 6)
    assert tri.shape == (6, 8) and (tri == -1).all() and np.isinf(z).all()


# ---- the ground, the camera, the light -------------------------------------------------------------------------------------
def test_terrain_cells_holes_and_moved_cells():
    z = np.arange(36, dtype=float).reshape(6, 6) * 0.1
    holes = np.zeros((6, 6), bool)
    holes[2, 3] = True                                  # the cell at column 3, row 2 (x 103..104, y 202..203): open
    moved = np.zeros((6, 6), bool)
    moved[0, 0] = True                                  # a corner of the first cell
    tris, cls = ev.terrain_mesh(z, 100.0, 200.0, (100.0, 200.0, 105.0, 205.0), holes, moved)
    assert tris.shape == (2 * (25 - 1), 3, 3)
    assert int((cls == ev.MOVED).sum()) == 2
    assert tris[..., 0].min() == 100.0 and tris[..., 0].max() == 105.0
    assert tris[0, 0, 2] == pytest.approx(z[0, 0])
    got = {tuple(t[:, :2].min(axis=0)) for t in tris}
    assert (103.0, 202.0) not in got and (102.0, 202.0) in got
    sub, _ = ev.terrain_mesh(z, 100.0, 200.0, (101.0, 201.0, 103.0, 203.0))      # cropped to a box: 2 x 2 cells
    assert len(sub) == 8


def test_terrain_normals_point_up_and_the_normal_of_a_degenerate_triangle_is_defined():
    tris, _ = ev.terrain_mesh(np.zeros((3, 3)), 0.0, 0.0, (0, 0, 2, 2))
    assert np.allclose(ev.face_normals(tris), (0, 0, 1))
    assert np.allclose(ev.face_normals(np.zeros((1, 3, 3))), (0, 0, 1))


def test_top_view_has_north_up_and_the_target_in_the_middle():
    cam = ev.view_camera((50.0, 60.0, 5.0), 0.0, 90.0, 200, 100, scale=2.0)
    sx, sy, d = cam.project(np.array([[50.0, 60.0, 5.0], [60.0, 60.0, 5.0], [50.0, 70.0, 5.0], [50.0, 60.0, 15.0]]))
    assert (sx[0], sy[0]) == (100.0, 50.0)
    assert sx[1] == 120.0 and sy[1] == 50.0             # east is to the right
    assert sx[2] == 100.0 and sy[2] == 30.0             # north is up
    assert d[3] < d[0]                                  # higher is nearer the camera


def test_an_oblique_camera_on_the_south_side_sees_north_farther_and_higher():
    cam = ev.view_camera((0.0, 0.0, 0.0), 180.0, 35.0, 400, 300, scale=1.0)
    sx, sy, d = cam.project(np.array([[0.0, 0.0, 0.0], [0.0, 10.0, 0.0], [0.0, 0.0, 10.0], [10.0, 0.0, 0.0]]))
    assert sy[1] < sy[0] and d[1] > d[0]                # north: higher in the picture, farther
    assert sy[2] < sy[0] and d[2] < d[0]                # up: higher in the picture, nearer
    assert sx[3] > sx[0]                                # east is to the right for a camera looking north
    cam = ev.view_camera((0.0, 0.0, 0.0), 0.0, 35.0, 400, 300)      # on the north side: looking south, east to the left
    assert cam.project(np.array([[10.0, 0.0, 0.0]]))[0][0] < 200.0
    assert abs(float(cam.right @ cam.up)) < 1e-12 and abs(np.linalg.norm(cam.up) - 1) < 1e-12


def test_bearing_names():
    assert ev.parse_bearing("sw") == 225.0 and ev.parse_bearing("N") == 0.0 and ev.parse_bearing("-90") == 270.0
    assert ev.view_name("top") == "top" and ev.view_name("sw") == "from_SW"


def test_fit_camera_frames_the_points_with_a_margin():
    cam = ev.view_camera((0.0, 0.0, 0.0), 180.0, 35.0, 800, 500)
    pts = np.array([[a, b, c] for a in (-50, 50) for b in (-50, 50) for c in (0, 20)], float)
    ev.fit_camera(cam, pts)
    sx, sy, _ = cam.project(pts)
    assert sx.min() > 0 and sx.max() < 800 and sy.min() > 0 and sy.max() < 500
    assert min(800 - (sx.max() - sx.min()), 500 - (sy.max() - sy.min())) < 0.1 * 500       # (the tighter axis is filled)


def test_lambert_light_ambient_and_two_sided():
    cam = ev.view_camera((0, 0, 0), 180.0, 90.0, 10, 10)            # top down
    up = np.array([[0.0, 0.0, 1.0]])
    assert float(ev.shade(up, cam)[0]) == pytest.approx(float(ev.shade(-up, cam)[0]))     # seen from behind: lit alike
    assert float(ev.shade(up, cam)[0]) > 0.7
    L = cam.light()
    assert float(ev.shade(L[None], cam)[0]) == pytest.approx(1.0)                          # facing the light
    edge = np.cross(L, cam.right)                                                          # no light on it
    assert float(ev.shade(edge[None] / np.linalg.norm(edge), cam)[0]) == pytest.approx(ev.AMBIENT)


# ---- the whole picture -----------------------------------------------------------------------------------------------------
def scene():
    ground = quad(-20, -20, 20, 20, 0.0)
    road = quad(-3, -20, 3, 20, 0.0)
    return np.concatenate([ground, road]), np.array([ev.GROUND] * 2 + [ev.ROAD] * 2, np.int8)


def test_a_road_on_the_ground_is_pink_over_brown_in_the_top_view_despite_equal_heights():
    tris, cls = scene()
    cam = ev.view_camera((0.0, 0.0, 0.0), 0.0, 90.0, 120, 120, scale=2.5)
    img, used = ev.render(tris, cls, cam, ss=2)
    c_road, c_ground, c_bg = px(img, 60, 60), px(img, 20, 60), px(img, 3, 3)
    assert c_road[0] > c_road[1] and c_road[2] > c_road[1]          # pink: red and blue over green
    assert c_ground[0] > c_ground[1] > c_ground[2]                  # brown
    assert np.allclose(c_bg, ev.BACKGROUND)
    assert list(used) == [ev.GROUND, ev.ROAD]


def test_the_ground_hides_what_is_under_it_and_a_hole_shows_it():
    ground = quad(-20, -20, 20, 20, 5.0)
    tube = quad(-6, -6, 6, 6, 0.0)
    tris = np.concatenate([ground, tube])
    cls = np.array([ev.GROUND] * 2 + [ev.TUNNEL] * 2, np.int8)
    cam = ev.view_camera((0.0, 0.0, 0.0), 0.0, 90.0, 100, 100, scale=2.0)
    img, used = ev.render(tris, cls, cam, ss=1)
    assert list(used) == [ev.GROUND]
    z = np.full((41, 41), 5.0)
    holes = np.zeros((41, 41), bool)
    holes[14:26, 14:26] = True                                       # x -6..5, y -6..5
    g, c = ev.terrain_mesh(z, -20.0, -20.0, (-20, -20, 20, 20), holes)
    img, used = ev.render(np.concatenate([g, tube]), np.concatenate([c, np.full(2, ev.TUNNEL, np.int8)]), cam, ss=1)
    assert ev.TUNNEL in list(used)
    assert px(img, 50, 50)[2] > px(img, 50, 50)[0]                   # blue-grey at the middle (seen through the hole)


def test_the_stage_line_is_dark_where_seen_and_faint_where_hidden():
    ground = quad(-20, -20, 20, 20, 0.0)
    cls = np.array([ev.GROUND] * 2, np.int8)
    seen = np.array([[-15.0, -10.0, 0.2], [15.0, -10.0, 0.2]])
    hidden = np.array([[-15.0, 10.0, -6.0], [15.0, 10.0, -6.0]])     # 6 m under the ground
    cam = ev.view_camera((0.0, 0.0, 0.0), 180.0, 45.0, 160, 160, scale=3.0)
    img, _ = ev.render(ground, cls, cam, lines=[seen, hidden], ss=2)
    sx, sy, _ = cam.project(np.array([[0.0, -10.0, 0.2], [0.0, 10.0, -6.0]]))
    dark, faint = px(img, sx[0], sy[0]), px(img, sx[1], sy[1])
    ground_px = px(img, sx[1], sy[1] + 10)
    assert dark.max() < 0.2
    assert dark.sum() < faint.sum() < ground_px.sum() * 0.9           # a faint dark over the ground: between the two
    assert ev.render(ground, cls, cam, ss=1)[0].shape == (160, 160, 3)


def test_a_depth_bias_lets_a_mesh_lying_on_the_ground_show():
    tris = np.concatenate([quad(-10, -10, 10, 10, 1.0), quad(-3, -10, 3, 10, 1.0)])
    cls = np.array([ev.GROUND] * 2 + [ev.ROAD] * 2, np.int8)
    cam = ev.view_camera((0.0, 0.0, 1.0), 0.0, 90.0, 60, 60, scale=2.0)
    assert ev.ROAD in list(ev.render(tris, cls, cam, ss=1, bias=0.03)[1])
    sunk = tris.copy()
    sunk[2:, :, 2] -= 0.5                                            # half a metre under: hidden whatever the bias
    assert ev.ROAD not in list(ev.render(sunk, cls, cam, ss=1, bias=0.03)[1])


def test_sample_polyline_keeps_its_vertices():
    P = np.array([[0.0, 0.0, 0.0], [3.0, 4.0, 0.0], [3.0, 4.0, 5.0]])
    S = ev.sample_polyline(P, 0.5)
    assert np.allclose(S[0], P[0]) and np.allclose(S[-1], P[-1]) and any(np.allclose(s, P[1]) for s in S)
    assert np.linalg.norm(np.diff(S, axis=0), axis=1).max() <= 0.5 + 1e-9


# ---- the tile's meshes and the frame -----------------------------------------------------------------------------------------
def test_tile_entries_name_the_meshes_of_a_tile_json():
    data = {"v": 1, "road": {"shape": "/vfs/road_2_0_0.cdae", "pos": [0, 0, 0]}, "bld2": {"shape": "/vfs/bld_2_0_0_2.cdae", "pos": [1.0, 2, 3]},
            "railcol3": {"shape": "x/railcol.cdae", "pos": [0, 0, 0]}, "chunks": [{"n": []}], "colliders": {"deck": [[0, 0, 0, 1, 1, 1]]},
            "b3dmr": [{"shape": "/vfs/m.cdae", "pos": [0, 0, 0]}], "props": {"groups": [], "items": []},
            "walk": {"shape": "/vfs/w.dae", "pos": [0, 0, 0]}}
    got = ev.tile_entries(data)
    assert ("road", "road_2_0_0.cdae", [0.0, 0.0, 0.0]) in got
    assert ("bld", "bld_2_0_0_2.cdae", [1.0, 2.0, 3.0]) in got
    assert ("railcol", "railcol.cdae", [0.0, 0.0, 0.0]) in got
    assert ("b3dmr", "m.cdae", [0.0, 0.0, 0.0]) in got
    assert len(got) == 4                                             # (the chunks, colliders and a .dae are not meshes)
    assert ev.KIND_CLASS["deck"] == ev.DECK and ev.KIND_CLASS["bld"] == ev.BUILDING and ev.KIND_CLASS["tunnel"] == ev.TUNNEL


def test_shape_triangles_places_the_meshes_at_pos(monkeypatch):
    fake = types.SimpleNamespace(mesh_arrays=lambda m: {"verts": m["v"], "indices": m["i"]})
    monkeypatch.setitem(sys.modules, "cdae", fake)
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 1]], np.float32)
    shape = {"meshes": [{"type": 0, "v": v, "i": np.array([0, 1, 2, 0, 2, 3, 9], np.uint32)}, {"type": 4}]}
    t = ev.shape_triangles(shape, [10.0, 20.0, 30.0])
    assert t.shape == (2, 3, 3) and np.allclose(t[1, 2], [10, 21, 31])
    assert ev.shape_triangles({"meshes": []}, [0, 0, 0]).shape == (0, 3, 3)


def test_crop_and_tiles_and_block():
    t = np.concatenate([quad(0, 0, 1, 1, 0.0), quad(50, 50, 51, 51, 0.0)])
    assert len(ev.crop_triangles(t, (-5, -5, 5, 5))) == 2
    assert ev.tiles_covering(-446.0, 880.0, 120.0) == [(-3, 2), (-3, 3), (-2, 2), (-2, 3)]
    assert ev.tiles_covering(128.0, 128.0, 100.0) == [(0, 0)]
    assert ev.block_centre(-446.0, 880.0) == (-384.0, 896.0) and ev.block_centre(-423.0, 105.0) == (-384.0, 128.0)
    assert ev.joined_pairs(["--xy", "-446,880", "--radius", "5", "--at", "1,2"]) == ["--xy=-446,880", "--radius", "5", "--at=1,2"]


def test_the_framed_picture_has_a_title_a_legend_and_a_scale_bar():
    tris, cls = scene()
    cam = ev.view_camera((0.0, 0.0, 0.0), 0.0, 90.0, 300, 200, scale=4.0)
    img, used = ev.render(tris, cls, cam, ss=1)
    pic = ev.frame_picture(img, cam, "title", "subtitle", used, True, True)
    assert pic.size[0] == 300 and pic.size[1] > 200 + 62
    a = np.asarray(pic)
    assert (a[:62] > 240).mean() > 0.8                              # the title band is white with text
    assert (a[:62] < 100).any()                                     # ... the text is there
    legend = a[62 + 200:]
    assert any((legend == tuple(int(c * 255) for c in ev.CLASS_RGB[k])).all(axis=2).any() for k in (ev.GROUND, ev.ROAD))
    assert (legend == tuple(int(c * 255) for c in ev.LINE_RGB)).all(axis=2).any()        # the stage line's swatch


def test_four_views_make_one_picture_two_per_row_each_labelled():
    tris, cls = scene()
    panels, used_all = [], set()
    for k, (bearing, elev) in enumerate(((0.0, 90.0), (225.0, 35.0), (135.0, 35.0), (0.0, 35.0))):
        cam = ev.fit_camera(ev.view_camera((0.0, 0.0, 0.0), bearing, elev, 200, 120), tris.reshape(-1, 3))
        img, used = ev.render(tris, cls, cam, ss=1)
        panels.append(ev.annotate_panel(img, cam, f"view {k}"))
        used_all.update(int(u) for u in used)
    pic = ev.compose(panels, "title", "subtitle", ev.legend_entries(sorted(used_all), False, True))
    assert pic.size[0] == 2 * 200 + ev.PANEL_GAP and pic.size[1] > 62 + 2 * 120 + ev.PANEL_GAP
    a = np.asarray(pic)
    for r in range(2):
        for c in range(2):
            y, x = 62 + r * (120 + ev.PANEL_GAP), c * (200 + ev.PANEL_GAP)
            label = a[y + 1:y + 29, x + 1:x + 60]
            assert (label > 240).mean() > 0.5 and (label < 100).any()      # each panel's white label with its text
    gap = a[62:62 + 2 * 120, 200:200 + ev.PANEL_GAP]
    assert (gap == 255).all()                                                 # the panels apart, on white
    legend = a[62 + 2 * 120 + ev.PANEL_GAP:]
    assert (legend == tuple(int(c * 255) for c in ev.NEEDLE_RGB)).all(axis=2).any()


def test_the_ground_the_grader_moves_is_magenta_in_the_picture_and_the_legend():
    z = np.zeros((41, 41))
    moved = np.zeros((41, 41), bool)
    moved[18:23, 18:23] = True
    tris, cls = ev.terrain_mesh(z, -20.0, -20.0, (-20, -20, 20, 20), None, moved)
    cam = ev.view_camera((0.0, 0.0, 0.0), 0.0, 90.0, 100, 100, scale=2.0)
    img, used = ev.render(tris, cls, cam, ss=1)
    c = px(img, 50, 50)
    assert c[0] > 0.5 and c[2] > 0.5 and c[1] < 0.2 * c[0]          # magenta: red and blue, no green
    assert list(used) == [ev.GROUND, ev.MOVED]
    pic = np.asarray(ev.frame_picture(img, cam, "t", "s", used, False, False))
    assert (pic[62 + 100:] == (255, 0, 255)).all(axis=2).any()
