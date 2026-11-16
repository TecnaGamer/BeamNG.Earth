//! The tram overhead line in the kernel (plan 046, stages E and G): poles (poles.rs) and wires (wires.rs), with the
//! planar helpers they share (geom.rs). server/tramcat.py is the glue that reads the map's features and calls
//! `tram_overhead` (one call a tile). The ground track (track.rs, lines.rs: plan 046 stages 1a and A) is the same.
pub mod corridor;
pub mod geom;
pub mod lines;
pub mod mesh;
pub mod poles;
pub mod physmesh;
pub mod profile;
pub mod signals;
pub mod switches;
pub mod track;
pub mod wires;

use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyList, PyTuple};

use geom::P2;

fn pts2(a: &PyReadonlyArray2<f64>) -> Vec<P2> {
    let v = a.as_array();
    (0..v.nrows()).map(|i| [v[[i, 0]], v[[i, 1]]]).collect()
}

fn split_by<T: Clone>(all: &[T], off: &[i64]) -> Vec<Vec<T>> {
    (0..off.len().saturating_sub(1)).map(|i| all[off[i] as usize..off[i + 1] as usize].to_vec()).collect()
}

/// The overhead line of a tile's trams: ([prop row], row owners (n, 2), [(wire (k, 3), half width)], wire owners (w, 2),
/// [(x, y, wall, z or None)] poles). The scene: parts (ids, bridge flags, points with offsets), roads (points, offsets,
/// half widths), building rings (points, offsets), road bridge decks (points, heights, offsets, widths), railway tracks' axes and every ground tram track's (points, offsets each); ground(xy
/// (n, 2)) -> z (n,) is called once; country (ISO alpha-2, "" unknown: the pole family). Owners tell the caller which tile draws each item.
#[pyfunction]
#[pyo3(signature = (fids, bridge, part_pts, part_off, road_pts, road_off, road_hw, ring_pts, ring_off, deck_pts, deck_z, deck_off, deck_w, rail_pts, rail_off, track_pts, track_off, ground, country))]
#[allow(clippy::too_many_arguments)]
pub fn tram_overhead<'py>(py: Python<'py>, fids: Vec<String>, bridge: PyReadonlyArray1<'py, bool>, part_pts: PyReadonlyArray2<'py, f64>,
                          part_off: PyReadonlyArray1<'py, i64>, road_pts: PyReadonlyArray2<'py, f64>, road_off: PyReadonlyArray1<'py, i64>,
                          road_hw: PyReadonlyArray1<'py, f64>, ring_pts: PyReadonlyArray2<'py, f64>, ring_off: PyReadonlyArray1<'py, i64>,
                          deck_pts: PyReadonlyArray2<'py, f64>, deck_z: PyReadonlyArray1<'py, f64>, deck_off: PyReadonlyArray1<'py, i64>,
                          deck_w: PyReadonlyArray1<'py, f64>, rail_pts: PyReadonlyArray2<'py, f64>, rail_off: PyReadonlyArray1<'py, i64>,
                          track_pts: PyReadonlyArray2<'py, f64>, track_off: PyReadonlyArray1<'py, i64>,
                          ground: Bound<'py, PyAny>, country: &str) -> PyResult<Bound<'py, PyTuple>> {
    let off = |a: &PyReadonlyArray1<i64>| a.as_array().to_vec();
    let bridge = bridge.as_array().to_vec();
    let parts: Vec<poles::Part> = split_by(&pts2(&part_pts), &off(&part_off)).into_iter().enumerate()
        .map(|(i, xy)| poles::Part { fid: fids[i].clone(), bridge: bridge[i], xy }).collect();
    let hw = road_hw.as_array().to_vec();
    let roads: Vec<poles::Road> = split_by(&pts2(&road_pts), &off(&road_off)).into_iter().enumerate()
        .map(|(i, pts)| poles::Road { pts, hw: hw[i] }).collect();
    let rings = split_by(&pts2(&ring_pts), &off(&ring_off));
    let dz = deck_z.as_array().to_vec();
    let dw = deck_w.as_array().to_vec();
    let dpts = pts2(&deck_pts);
    let doff = off(&deck_off);
    let decks: Vec<poles::Deck> = (0..doff.len().saturating_sub(1)).map(|i| {
        let (a, b) = (doff[i] as usize, doff[i + 1] as usize);
        poles::Deck { xy: dpts[a..b].to_vec(), z: dz[a..b].to_vec(), width: dw[i] }
    }).collect();
    let sc = poles::Scene { parts, roads, rings, decks, country: country.to_string(), rails: split_by(&pts2(&rail_pts), &off(&rail_off)),
                           tracks: split_by(&pts2(&track_pts), &off(&track_off)) };
    let plan = poles::plan(&sc);
    let (rows, wires) = if plan.parts.is_empty() && plan.poles.is_empty() {
        (Vec::new(), Vec::new())
    } else {
        let pts = wires::ground_points(&plan);
        let flat: Vec<f64> = pts.iter().flat_map(|p| [p[0], p[1]]).collect();
        let arr = Array2::from_shape_vec((pts.len(), 2), flat).unwrap().into_pyarray(py);
        let got = ground.call1((arr,))?;
        let z: Vec<f64> = got.call_method0("ravel")?.extract()?;
        if z.len() != pts.len() {
            return Err(pyo3::exceptions::PyValueError::new_err("tram_overhead: ground gives a height per point"));
        }
        let h = wires::split_heights(&plan, &z);
        wires::build(&sc, &plan, &h)
    };
    let row_list = PyList::empty(py);
    let mut row_xy = Vec::with_capacity(rows.len() * 2);
    for r in &rows {
        let item = PyList::empty(py);
        item.append(r.group)?;
        for v in [r.x, r.y, r.z, r.yaw, r.scale, r.height, 0.0] {
            item.append(v)?;
        }
        row_list.append(item)?;
        row_xy.extend([r.owner[0], r.owner[1]]);
    }
    let wire_list = PyList::empty(py);
    let mut wire_xy = Vec::with_capacity(wires.len() * 2);
    for w in &wires {
        let flat: Vec<f64> = w.pts.iter().flat_map(|p| p.iter().copied()).collect();
        let arr = Array2::from_shape_vec((w.pts.len(), 3), flat).unwrap().into_pyarray(py);
        wire_list.append(PyTuple::new(py, [arr.into_any(), w.r.into_pyobject(py)?.into_any()])?)?;
        wire_xy.extend([w.owner[0], w.owner[1]]);
    }
    let pole_list = PyList::empty(py);
    for p in &plan.poles {
        pole_list.append(PyTuple::new(py, [p.q[0].into_pyobject(py)?.into_any(), p.q[1].into_pyobject(py)?.into_any(),
                                           PyBool::new(py, p.wall).to_owned().into_any(), p.z.into_pyobject(py)?.into_any()])?)?;
    }
    let rxy = Array2::from_shape_vec((rows.len(), 2), row_xy).unwrap().into_pyarray(py);
    let wxy = Array2::from_shape_vec((wires.len(), 2), wire_xy).unwrap().into_pyarray(py);
    // (the stretches of contact wire between two nodes farther apart than MAX_UNSUPPORTED_M: x, y, m; the yard audit)
    let gap_list = PyList::empty(py);
    for pp in &plan.parts {
        for w in pp.nodes.windows(2) {
            let l = geom::hyp(w[1].p[0] - w[0].p[0], w[1].p[1] - w[0].p[1]);
            if l > poles::MAX_UNSUPPORTED_M && l <= wires::MAX_SPAN_DRAWN_M {
                gap_list.append(PyTuple::new(py, [w[0].p[0], w[0].p[1], l])?)?;
            }
        }
    }
    PyTuple::new(py, [row_list.into_any(), rxy.into_any(), wire_list.into_any(), wxy.into_any(), pole_list.into_any(), gap_list.into_any()])
}

/// The rule's numbers (for the tests and the glue): name -> metres.
#[pyfunction]
pub fn tram_consts(py: Python<'_>) -> PyResult<Bound<'_, pyo3::types::PyDict>> {
    let d = pyo3::types::PyDict::new(py);
    for (k, v) in [("SPAN_M", poles::SPAN_M), ("SIDEWALK_OFF_M", poles::SIDEWALK_OFF_M), ("BRIDGE_OUT_M", poles::BRIDGE_OUT_M),
                   ("MAST_OFF_M", poles::MAST_OFF_M),
                   ("SPAN_Z", wires::SPAN_Z), ("WALL_Z", wires::WALL_Z), ("CONTACT_Z", wires::CONTACT_Z),
                   ("HANGER_M", wires::HANGER_M), ("SPAN_R", wires::SPAN_R), ("CONTACT_R", wires::CONTACT_R),
                   ("DROPPER_R", wires::DROPPER_R), ("Z_ARM", wires::Z_ARM), ("MODEL_H", wires::MODEL_H),
                   ("LAMP_CLEAR_M", poles::LAMP_CLEAR_M), ("GAP_MIN_M", poles::GAP_MIN_M), ("GAP_MAX_M", poles::GAP_MAX_M), ("INS_R", wires::INS_R),
                   ("CLAMP_R", wires::CLAMP_R), ("HANGER_R", wires::HANGER_R), ("SPRING_R", wires::SPRING_R),
                   ("JOINT_M", wires::JOINT_M), ("POLE_TRACK_CLEAR_M", poles::POLE_TRACK_CLEAR_M), ("MAX_UNSUPPORTED_M", poles::MAX_UNSUPPORTED_M),
                   ("BAY_MAX_M", poles::BAY_MAX_M), ("PORTAL_POST_OUT_M", poles::PORTAL_POST_OUT_M), ("PULL_TOL_M", wires::PULL_TOL_M)] {
        d.set_item(k, v)?;
    }
    Ok(d)
}

/// Which of the points (n, 2) lie within r of a tram pole (m, 2): the street lamps a pole's bracket replaces.
#[pyfunction]
pub fn tram_near<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, poles_xy: PyReadonlyArray2<'py, f64>, r: f64) -> Bound<'py, numpy::PyArray1<bool>> {
    let got = poles::near_any(&pts2(&pts), &pts2(&poles_xy), r);
    numpy::PyArray1::from_vec(py, got)
}

/// The family (tube, square, lattice, concrete) of the tram line `fid` in a country.
#[pyfunction]
pub fn tram_family(country: &str, fid: &str) -> &'static str {
    poles::family_of(country, fid).name()
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(tram_overhead, m)?)?;
    m.add_function(wrap_pyfunction!(tram_consts, m)?)?;
    m.add_function(wrap_pyfunction!(tram_near, m)?)?;
    m.add_function(wrap_pyfunction!(tram_family, m)?)?;
    // the ground track (track.rs; kernel 277 on its branch): tram_track_consts, tram_gauge, tram_paving, tram_paving_width,
    // tram_in_street, tram_ground_chunks
    m.add_function(wrap_pyfunction!(track::tram_track_consts, m)?)?;
    m.add_function(wrap_pyfunction!(track::tram_gauge, m)?)?;
    m.add_function(wrap_pyfunction!(track::tram_paving, m)?)?;
    m.add_function(wrap_pyfunction!(track::tram_paving_width, m)?)?;
    m.add_function(wrap_pyfunction!(track::tram_in_street, m)?)?;
    m.add_function(wrap_pyfunction!(track::tram_ground_chunks, m)?)?;
    m.add_function(wrap_pyfunction!(track::tram_open_runs, m)?)?;
    m.add_function(wrap_pyfunction!(track::tram_open_heights, m)?)?;
    // the 3D track's mesh, the deck paving, the road deck's height (mesh.rs; kernel 300)
    m.add_function(wrap_pyfunction!(mesh::tram_open_mesh, m)?)?;
    m.add_function(wrap_pyfunction!(mesh::tram_open_bed, m)?)?;
    m.add_function(wrap_pyfunction!(mesh::tram_deck_paving, m)?)?;
    m.add_function(wrap_pyfunction!(mesh::tram_road_deck_z, m)?)?;
    m.add_function(wrap_pyfunction!(mesh::tram_rides_deck, m)?)?;
    m.add_function(wrap_pyfunction!(mesh::tram_mesh_consts, m)?)?;
    m.add_function(wrap_pyfunction!(track::tram_sleeper_reach, m)?)?;
    // the corridor nothing stands in along a ground tram (corridor.rs)
    m.add_function(wrap_pyfunction!(corridor::tram_corridor_lines, m)?)?;
    m.add_function(wrap_pyfunction!(corridor::tram_corridor_hits, m)?)?;
    m.add_function(wrap_pyfunction!(corridor::tram_corridor_consts, m)?)?;
    // the ground track's bed (profile.rs): tram_beds, tram_bed_consts
    m.add_function(wrap_pyfunction!(profile::tram_beds, m)?)?;
    m.add_function(wrap_pyfunction!(profile::tram_bed_consts, m)?)?;
    m.add_function(wrap_pyfunction!(profile::tram_follow, m)?)?;
    // the street rails for the physics (physmesh.rs)
    m.add_function(wrap_pyfunction!(physmesh::tram_street_runs, m)?)?;
    m.add_function(wrap_pyfunction!(physmesh::tram_physics_points, m)?)?;
    m.add_function(wrap_pyfunction!(physmesh::tram_physics_mesh, m)?)?;
    m.add_function(wrap_pyfunction!(physmesh::tram_physics_consts, m)?)?;
    // tram signals (signals.rs; kernel 279 on its branch): tram_signals
    m.add_function(wrap_pyfunction!(signals::tram_signals, m)?)?;
    Ok(())
}
