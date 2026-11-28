"""grade3d.region: the solve in the build. Blocks fixed on the planning frame's grid, each solved once; a cell's heights
blended from the blocks round it, the same at a point whichever cell asks; every member's line reads its height through
the grader's _shape_at."""
import threading

import numpy as np

import geodesy
import gradeshapes as gsh
import grading
import linenet
import mapcells
from gradeshapes import Member
from grade3d import outputs, region


def _line(x0, y0, x1, y1, step=20.0):
    n = max(2, int(np.hypot(x1 - x0, y1 - y0) / step) + 1)
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


class _Planner:
    """What the region reads of a planner: its frame, lock, elevation and each cell's members (here every cell has them
    all: load keeps each key once)."""

    def __init__(self, members):
        self.frame = geodesy.EnuFrame(45.0, 7.0)
        self.lock = threading.RLock()
        self.members = members
        self.shared = None          # (no plan cache shared with other processes)
        self.region = (0, 0)

    def _shape_members(self, _k):
        return self.members

    def elev(self, la, lo):
        e, n, _ = self.frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), 0.0)
        return 20.0 + 5.0 * np.sin(np.asarray(e) / 300.0) + 3.0 * np.cos(np.asarray(n) / 250.0)


class _Region:
    def __init__(self, frame, prof):
        self.frame, self.prof = frame, prof

    def _shape_region(self, _home):
        return self.prof


def _city(lo, hi):
    ms = [Member(("mw", "ground", "0"), _line(0, lo, 0, hi), "ground", "motorway", 100, 0.0, 6.0, None, None)]
    for k, y in enumerate(np.arange(lo + 100, hi, 300.0)):
        ms += [Member(("s%d" % k, "ground", "0"), _line(lo, y, -60, y), "ground", "secondary", None, 0.0, 4.0, None,
                      None),
               Member(("s%d" % k, "deck", 0, int(y)), _line(-60, y, 60, y, 10.0), "deck", "secondary", None, 1.0, 4.0,
                      None, None),
               Member(("s%d" % k, "ground", "1"), _line(60, y, hi, y), "ground", "secondary", None, 0.0, 4.0, None,
                      None)]
    return ms


def setup_function(_f):
    linenet.reset()


def test_two_cells_agree_and_every_line_reads_its_height(monkeypatch):
    monkeypatch.setattr(region, "BLOCK_M", 500.0)
    monkeypatch.setattr(region, "RAMP_M", 60.0)
    monkeypatch.setattr(region, "MARGIN_M", 200.0)
    monkeypatch.setattr(region, "READ_PAD_M", 100.0)
    pl = _Planner([])
    home = mapcells.cell_of(45.0, 7.0)
    lo, hi = region.cell_box(pl, home)
    east = mapcells.CellKey(home.z, home.x + 1, home.y)
    pl.members = _city(float(lo[1]) - 300, float(hi[1]) + 300)
    prof = {c: region.cell_profiles(pl, c) for c in (home, east)}
    assert len(pl._grade3d_blocks) >= 4
    assert all(b.diagnostics.get("status") == "Solved" for b in pl._grade3d_blocks.values() if b.prof)
    # (every member over the cell reads its whole line, in its mode)
    for m in pl.members:
        if not region.touches(m.line, lo, hi):
            continue
        zz, modes = grading._Planner._shape_at(_Region(pl.frame, prof[home]), m.line, m.line, outputs.fid_of(m),
                                               (outputs.MODE[m.kind],))
        assert np.isfinite(zz).all(), m.key
    # (where both cells have a road, one height)
    diffs = []
    for fid, lst in prof[home].items():
        for xy, zz, mode in lst:
            if fid in prof[east]:
                zb, _ = grading._Planner._shape_at(_Region(pl.frame, prof[east]), xy, xy, fid, (mode,))
                ok = np.isfinite(zb)
                diffs.append(np.abs(zb[ok] - zz[ok]))
    assert diffs and np.concatenate(diffs).max() < 0.02
    # (each bridge clears the motorway, read where the build reads it)
    for k, y in enumerate(np.arange(float(lo[1]) - 200, float(hi[1]) + 300, 300.0)):
        if not lo[1] + 100 < y < hi[1] - 100:
            continue
        q = np.array([[0.0, y]])
        zd, _ = grading._Planner._shape_at(_Region(pl.frame, prof[home]), q, q, "s%d" % k, ("deck",))
        zm, _ = grading._Planner._shape_at(_Region(pl.frame, prof[home]), q, q, "mw", ("set",))
        assert zd[0] - zm[0] >= gsh.HEADROOM_M + gsh.deck_depth(120.0) - 0.1


def test_a_block_is_solved_once():
    pl = _Planner(_city(-800, 800))
    a = region.block(pl, 0, 0)
    assert region.block(pl, 0, 0) is a and a.prof


class _Cell:
    """What a planner's _shape_members reads of a cell: its plan (one track, on the ground then on a deck) and its nodes."""

    def __init__(self):
        self.lock = threading.RLock()

    def _cell_enu(self, _k):
        return {"ground": [], "bridges": [], "rail_ground": [_line(0, 0, 100, 0)], "rail_ground_ids": ["t1"],
                "rail_bridges": [_line(100, 0, 200, 0)], "rail_bridge_ids": ["t1"], "rail_bridge_levels": [1.0]}

    def _cell_nodes(self, _k):
        line = _line(0, 0, 200, 0)
        return {"t1": (frozenset({"n0", "n1", "n2"}), (("n0", 0.0, 0.0), ("n1", 100.0, 0.0), ("n2", 200.0, 0.0)),
                       (0.0, 100.0, 200.0), line, line[:, 0].copy())}

    def _shape_tunnels(self, _k):
        return []


def test_a_cells_tracks_carry_their_map_nodes():
    # (a track meets a road where the map joins them, as a road meets a road: without its nodes it was read as joined
    # wherever a point of each lay within 5 cm, Yokohama's rail deck to the street under it)
    # (each piece the nodes on its own stretch: the ground 0..100 m, the deck 100..200 m, both the one they meet at)
    ms = {m.kind: m for m in grading._Planner._shape_members(_Cell(), "k")}
    assert sorted(ms) == ["rail", "rail_deck"]
    assert ms["rail"].nodes == frozenset({"n0", "n1"}) and len(ms["rail"].node_xy) == 2
    assert ms["rail_deck"].nodes == frozenset({"n1", "n2"}) and len(ms["rail_deck"].node_xy) == 2


class _LinkCell(_Cell):
    """A cell with a motorway and a motorway link on the ground (the link's ground part: ground_link)."""

    def _cell_enu(self, _k):
        return {"ground": [(_line(0, 0, 100, 0), 5.0, "motorway"), (_line(0, 10, 100, 10), 3.0, "motorway")],
                "ground_ids": [("m", 0, 1.0, 1.0), ("l", 0, 1.0, 1.0)], "ground_link": [False, True], "bridges": []}

    def _cell_nodes(self, _k):
        return {}


def test_a_links_ground_part_is_a_link():
    # (Pregerson's HOV connector: its ground part read as a motorway, held to a motorway's 934 m curves and 6.5 %
    # where its deck had a link's; its block had no answer)
    ms = {str(m.key[0]): m for m in grading._Planner._shape_members(_LinkCell(), "k")}
    assert ms["m"].cls == "motorway" and ms["l"].cls == "motorway_link"
