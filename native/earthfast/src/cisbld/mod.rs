//! Soviet and post-Soviet housing (plan 048): which kind every building of a tile is (a khrushchyovka, a nine-storey
//! panel slab, a tower, a Stalinist block, a new monolithic block, a private house ...), and where its entrances and
//! stair sections are. The server (server/cisbuildings.py) passes the tile's footprints, what the map says of each, the
//! drivable roads; the facade cells and modules built from the verdicts come in the next stages.

pub mod classify;
pub mod context;
pub mod facade;
pub mod features;
pub mod modules;

use classify::{classify, Info};
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// Columns of a verdict row: kit, soviet (0/1), confidence, floors, long side L, short side W, long axis ux, uy,
/// side of the long axis the nearest road is on (+1 / -1 / 0), entrances, technical storey (0/1), same-block
/// neighbours, stair-section length (m), centre x, y of the oriented box, the width of the body (arm).
pub const COLUMNS: usize = 16;

/// (attrs columns: floors, height, year, material, obat, class, touching, country.)
/// The 3D modules of the walls: the balconies' share of the stacks and the entrance canopies. Off (the user, 2026-11-16, after
/// the first look in Vinnytsia: balconies and entrance canopies out): the painted cells (bay_balcony_far, entrance) stay.
pub const BALCONY_3D: f64 = 0.0;
pub const ENTRANCE_3D: bool = false;

/// One row per footprint (xy[start[i]..end[i]]); a ring with no area gets kit 0 and zeros.
#[pyfunction]
pub fn cis_classify<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, start: PyReadonlyArray1<'py, i64>,
                         end: PyReadonlyArray1<'py, i64>, attrs: PyReadonlyArray2<'py, f64>,
                         roads: PyReadonlyArray2<'py, f64>) -> Bound<'py, PyArray2<f64>> {
    let (xy, attrs, roads) = (xy.as_array(), attrs.as_array(), roads.as_array());
    let (start, end) = (start.as_array().to_vec(), end.as_array().to_vec());
    let n = start.len().min(end.len()).min(attrs.nrows());
    let segs: Vec<[f64; 4]> = if roads.ncols() >= 4 {
        roads.rows().into_iter().map(|r| [r[0], r[1], r[2], r[3]]).collect()
    } else {
        vec![]
    };
    let rows = py.detach(|| {
        let feats: Vec<Option<features::Feat>> = (0..n)
            .map(|i| {
                let (a, b) = (start[i].max(0) as usize, (end[i].max(0) as usize).min(xy.nrows()));
                if b <= a + 2 {
                    return None;
                }
                let xs: Vec<f64> = (a..b).map(|k| xy[[k, 0]]).collect();
                let ys: Vec<f64> = (a..b).map(|k| xy[[k, 1]]).collect();
                features::features(&xs, &ys)
            })
            .collect();
        let infos: Vec<Info> = (0..n)
            .map(|i| {
                let r = attrs.row(i);
                Info {
                    floors: r[0], height: r[1], year: r[2], material: r[3] as i32, obat: r[4] as i32,
                    class: r[5] as i32, touching: r[6] as i32, country: r[7] as i32,
                }
            })
            .collect();
        let floors: Vec<i32> = infos.iter().map(classify::floors_of).collect();
        let around = context::around(&feats, &floors, &segs);
        let mut out = vec![0.0f64; n * COLUMNS];
        for i in 0..n {
            let Some(f) = feats[i].as_ref() else { continue };
            let v = classify(&infos[i], f, &around[i]);
            let o = &mut out[i * COLUMNS..(i + 1) * COLUMNS];
            o.copy_from_slice(&[
                v.kit as f64, if v.soviet { 1.0 } else { 0.0 }, v.conf, v.floors as f64, f.l, f.w, f.ux, f.uy,
                around[i].road_side, v.entrances as f64, if v.tech { 1.0 } else { 0.0 }, around[i].serial as f64,
                v.section_m, f.cx, f.cy, f.arm,
            ]);
        }
        out
    });
    numpy::ndarray::Array2::from_shape_vec((n, COLUMNS), rows).unwrap().into_pyarray(py)
}

/// One building's facade (facade.rs): `(P, UV, mid, slots)`: P (4q, 3) and UV (4q, 2) quad corners, `mid` (q,) the
/// material slot (0 the kit's atlas, 1 the common atlas), `slots` (m, 9) the places for modules.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn cis_facade<'py>(py: Python<'py>, kit: &str, kit_json: &str, common_json: &str, ring: PyReadonlyArray2<'py, f64>,
                       z_skirt: f64, z_base: f64, z_eave: f64, floors: i32, tech: bool, entrances: i32, axis: [f64; 2],
                       center: [f64; 2], length: f64, arm: f64, road_side: f64, seed: f64, lite: bool)
                       -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i32>>, Bound<'py, PyArray2<f64>>)> {
    let ka = facade::atlas(kit_json).map_err(PyValueError::new_err)?;
    let ca = facade::atlas(common_json).map_err(PyValueError::new_err)?;
    let ring: Vec<[f64; 2]> = ring.as_array().rows().into_iter().map(|r| [r[0], r[1]]).collect();
    let seed = (seed.clamp(0.0, 1.0) * 9007199254740992.0) as u64;
    let out = py.detach(|| {
        facade::build(&facade::Params {
            kit: &ka, common: &ca, kit_name: kit, ring: &ring, z_skirt, z_base, z_eave, floors, tech, entrances, axis, center,
            length, arm, road_side, seed, lite, balcony_3d: BALCONY_3D, entrance_3d: ENTRANCE_3D,
        })
    });
    let q = out.mid.len();
    let m = out.slots.len() / facade::SLOT_COLUMNS;
    let sh = |e: std::io::Error| PyValueError::new_err(e.to_string());
    let _ = sh;
    Ok((
        numpy::ndarray::Array2::from_shape_vec((q * 4, 3), out.p).map_err(|e| PyValueError::new_err(e.to_string()))?.into_pyarray(py),
        numpy::ndarray::Array2::from_shape_vec((q * 4, 2), out.uv).map_err(|e| PyValueError::new_err(e.to_string()))?.into_pyarray(py),
        numpy::ndarray::Array1::from_vec(out.mid).into_pyarray(py),
        numpy::ndarray::Array2::from_shape_vec((m, facade::SLOT_COLUMNS), out.slots).map_err(|e| PyValueError::new_err(e.to_string()))?.into_pyarray(py),
    ))
}

/// The modules of a building's slots (modules.rs): `(P (3t, 3), UV (3t, 2), mat (t,), names)`; `mat` indexes `names`.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn cis_modules<'py>(py: Python<'py>, table_json: &str, kit: &str, slots: PyReadonlyArray2<'py, f64>, seed: f64, floors: i32)
                        -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i32>>, Vec<String>)> {
    let t = modules::table(table_json).map_err(PyValueError::new_err)?;
    let flat: Vec<f64> = slots.as_array().iter().copied().collect();
    let seed = (seed.clamp(0.0, 1.0) * 9007199254740992.0) as u64;
    let out = py.detach(|| modules::place(&t, kit, &flat, seed, floors));
    let k = out.mat.len();
    Ok((
        numpy::ndarray::Array2::from_shape_vec((k * 3, 3), out.p).map_err(|e| PyValueError::new_err(e.to_string()))?.into_pyarray(py),
        numpy::ndarray::Array2::from_shape_vec((k * 3, 2), out.uv).map_err(|e| PyValueError::new_err(e.to_string()))?.into_pyarray(py),
        numpy::ndarray::Array1::from_vec(out.mat).into_pyarray(py),
        t.mats.clone(),
    ))
}
