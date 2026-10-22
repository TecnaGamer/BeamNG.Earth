"""OSM exit-sign placement against the current 1.5.5 road geometry."""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import exitsigns
import geodesy
import roads


FRAME = geodesy.EnuFrame(41.7, -85.0)


def ll(x, y):
    lat, lon, _ = FRAME.enu_to_geodetic(x, y, 0.0)
    return [float(lon), float(lat)]


def fixture():
    main_spec = roads.Spec(11.0, "paved", False, "motorway", True, 2.5, 1, 110.0)
    ramp_spec = roads.Spec(6.0, "paved", False, "motorway", True, 2.5, 1, 55.0)
    main_line = np.array([[-1900., 0., 0.], [256., 0., 0.]])
    ramp_line = np.array([[0., 0., 0.], [100., -120., 0.], [200., -200., 0.]])
    rows = [("main", main_spec, main_line, -85.0), ("ramp", ramp_spec, ramp_line, -85.0)]
    obj = {
        "id": -201,
        "tags": {"highway": "motorway_junction", "ref": "36"},
        "exit": {
            "label": {"ref": "36", "names": ["Auburn"], "routes": []},
            "approach": [ll(-1900., 0.), ll(0., 0.)],
            "ramp": [ll(0., 0.), ll(100., -120.), ll(200., -200.)],
            "main_ids": [100], "ramp_id": 201, "side": 1,
        },
    }
    return rows, obj


def test_advance_and_gore_signs_follow_existing_road_geometry():
    rows, obj = fixture()
    flat = lambda lat, lon: np.zeros_like(np.asarray(lat, dtype=float))
    signs = exitsigns.place(FRAME, [obj], rows, (-1800., -300., 256., 100.), flat,
                            link_ids={"ramp"})
    advance = [s["distance"] for s in signs if s["kind"] == "advance"]
    assert advance == ["1 MILE", "1/2 MILE", "1/4 MILE"]
    gore = [s for s in signs if s["kind"] == "ramp"]
    assert len(gore) == 1
    assert gore[0]["label"]["ref"] == "36"
    assert np.isfinite(gore[0]["centre"]).all()

    mesh = exitsigns.build(signs, (0., 0., 0.))
    assert mesh is not None
    positions, triangles, uvs, materials, material_indices = mesh
    assert positions.ndim == 2 and positions.shape[1] == 3
    assert triangles.ndim == 2 and triangles.shape[1] == 3
    assert uvs.shape[0] == positions.shape[0]
    assert "bne_exit_letters" in materials
    assert material_indices.max() < len(materials)


def test_index_records_uses_real_destination_tags_and_skips_entries():
    coords = {1: ll(-500., 0.), 2: ll(-200., 0.), 3: ll(0., 0.),
              4: ll(30., -25.), 5: ll(60., -60.), 6: ll(-350., 30.)}
    main = [(10, [1, 2, 3], {"highway": "motorway", "oneway": "yes"})]
    ramps = [
        (20, [3, 4, 5], {"highway": "motorway_link", "oneway": "yes", "destination": "Auburn;Angola"}),
        (21, [6, 2], {"highway": "motorway_link", "oneway": "yes", "destination": "entry only"}),
    ]
    tags = {3: {"highway": "motorway_junction", "ref": "36"}}
    rows = exitsigns.index_records(main, ramps, coords, tags)
    assert len(rows) == 1
    assert rows[0]["ways"][0]["label"] == {"ref": "36", "names": ["Auburn", "Angola"], "routes": []}
