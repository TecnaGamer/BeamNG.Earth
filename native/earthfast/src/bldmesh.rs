//! The buildings' mesh assembly in Rust (earthfast 519, the full Rust port's r2a19): buildings.Mesh (the positions,
//! uvs, colours, triangles and material indices a tile's buildings append to) as the native BldMesh, and the pieces
//! buildings.build drew in Python quad by quad: the box-frame roofs (gabled, hipped, half-hipped, pyramidal, mansard,
//! gambrel, round, skillion) with their gable walls and chimneys, flat lids, cornices, boxes (plain and tiled), stoops,
//! shop signs and the roof clutter's boxes. A profile of three cold road-tile builds (NYC, Fells Point, Vienna,
//! ~/bnge-tests/r2a19/profile.md) had build() at 1.8 % of the busy CPU, all Python (the largest self-contained
//! Python subsystem left). The same numbers as the Python, bit for bit: every value is made by the same IEEE
//! operations in the same order (numpy's elementwise c + u * s + v * t, CPython's math.hypot (crate::py_hypot) and
//! round, the libm crate's tan as dmath's, dnp.dot's order for the ring areas, the ear clipping triangulate used,
//! hash01's BLAKE2b of the same tuple repr: the seed's repr is the Python's own, handed in). Python keeps the glue:
//! which branch a building takes, its material tokens (_Looks), the rare roofs (domes, skeletons, heightfields,
//! courtyard lids), steeples, porches, steel frames, canopies, parking decks, landmarks, ships, France's houses: those
//! call BldMesh's quad / tri / poly / quads / tris as they called Mesh's.

use numpy::{IntoPyArray, PyReadonlyArray2, PyReadonlyArrayDyn};
use pyo3::exceptions::{PyIndexError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};

use crate::dmmatmul::dot1;
use crate::{blake2b_8, ear_clip, py_hypot};

/// A material token (index, RGBA) as buildings._Looks gives.
#[derive(Clone, Copy)]
pub struct Mat {
    idx: i64,
    rgba: [u8; 4],
}

fn mat_of(o: &Bound<'_, PyAny>) -> PyResult<Mat> {
    let idx: i64 = o.get_item(0)?.extract()?;
    let c = o.get_item(1)?;
    if c.len()? != 4 {
        return Err(PyValueError::new_err("a material token's colour is RGBA: 4 values"));
    }
    let mut rgba = [0u8; 4];
    for (k, x) in rgba.iter_mut().enumerate() {
        let v: i64 = c.get_item(k)?.extract()?;
        if !(0..=255).contains(&v) {
            return Err(PyValueError::new_err(format!("a material colour of {v}, past 0-255")));
        }
        *x = v as u8;
    }
    Ok(Mat { idx, rgba })
}

/// A point of N numbers (a tuple, a list or an array; N exactly).
fn pt<const N: usize>(o: &Bound<'_, PyAny>) -> PyResult<[f64; N]> {
    let mut r = [0.0; N];
    if let Ok(t) = o.cast::<PyTuple>() {
        if t.len() == N {
            for (k, x) in r.iter_mut().enumerate() {
                *x = t.get_item(k)?.extract()?;
            }
            return Ok(r);
        }
    }
    let v: Vec<f64> = o.extract()?;
    if v.len() != N {
        return Err(PyValueError::new_err(format!("a mesh point of {} values, not {N}", v.len())));
    }
    r.copy_from_slice(&v);
    Ok(r)
}

/// Rows of an (n, >= N) table (an array or a sequence of sequences), their first N columns (np.asarray(x)[:, :N]).
fn rows<const N: usize>(o: &Bound<'_, PyAny>) -> PyResult<Vec<[f64; N]>> {
    if let Ok(a) = o.extract::<PyReadonlyArray2<'_, f64>>() {
        let a = a.as_array();
        if a.ncols() < N {
            return Err(PyValueError::new_err(format!("rows of {} columns, not {N} or more", a.ncols())));
        }
        return Ok(a.rows().into_iter().map(|r| std::array::from_fn(|k| r[k])).collect());
    }
    let v: Vec<Vec<f64>> = o.extract()?;
    if v.iter().any(|r| r.len() != v[0].len()) {
        return Err(PyValueError::new_err("rows of different lengths (numpy refused a ragged table)"));
    }
    v.iter().map(|r| {
        if r.len() < N {
            Err(PyValueError::new_err(format!("a row of {} values, not {N} or more", r.len())))
        } else {
            Ok(std::array::from_fn(|k| r[k]))
        }
    }).collect()
}

/// All numbers of an array or a (nested) sequence in C order (np.asarray(x, np.float64).reshape(-1)).
fn flat_f64(o: &Bound<'_, PyAny>) -> PyResult<Vec<f64>> {
    if let Ok(a) = o.extract::<PyReadonlyArrayDyn<'_, f64>>() {
        return Ok(a.as_array().iter().copied().collect());
    }
    if let Ok(v) = o.extract::<Vec<f64>>() {
        return Ok(v);
    }
    let v: Vec<Vec<f64>> = o.extract()?;
    if v.iter().any(|r| r.len() != v[0].len()) {
        return Err(PyValueError::new_err("rows of different lengths (numpy refused a ragged table)"));
    }
    Ok(v.into_iter().flatten().collect())
}

fn ring2(ring: &PyReadonlyArray2<'_, f64>) -> PyResult<Vec<[f64; 2]>> {
    let a = ring.as_array();
    if a.ncols() != 2 {
        return Err(PyValueError::new_err(format!("a ring of {} columns, not 2", a.ncols())));
    }
    Ok(a.rows().into_iter().map(|r| [r[0], r[1]]).collect())
}

/// buildings._area2: twice the signed area (counter-clockwise positive), dnp.dot's sums of x . roll(y) and roll(x) . y.
fn area2(xy: &[[f64; 2]]) -> f64 {
    let n = xy.len();
    let x: Vec<f64> = xy.iter().map(|p| p[0]).collect();
    let y: Vec<f64> = xy.iter().map(|p| p[1]).collect();
    let yr: Vec<f64> = (0..n).map(|i| y[(i + 1) % n]).collect();
    let xr: Vec<f64> = (0..n).map(|i| x[(i + 1) % n]).collect();
    dot1(&x, &yr) - dot1(&xr, &y)
}

/// buildings._hash01(seed, tag) / (seed, tag, k): BLAKE2b-64 of the tuple's repr / 2^64; seed_repr is repr(seed).
fn h01(seed_repr: &str, tag: &str, k: Option<i64>) -> f64 {
    let s = match k {
        Some(k) => format!("({seed_repr}, '{tag}', {k})"),
        None => format!("({seed_repr}, '{tag}')"),
    };
    blake2b_8(s.as_bytes()) as f64 / 18446744073709551616.0
}

/// Python's min(a, b) / max(a, b) of two floats: the first unless the second is smaller / larger.
#[inline]
fn pmin(a: f64, b: f64) -> f64 { if b < a { b } else { a } }
#[inline]
fn pmax(a: f64, b: f64) -> f64 { if b > a { b } else { a } }

/// buildings.Mesh: positions, uvs, colours, triangles and their material index, appended to in order.
#[pyclass(module = "earthfast", subclass)]
#[derive(Default)]
pub struct BldMesh {
    p: Vec<f64>,
    uv: Vec<f64>,
    c: Vec<u8>,
    t: Vec<i64>,
    m: Vec<i64>,
    n: i64,
}

const UV_PLAIN: [[f64; 2]; 4] = [[0.002, 0.995], [0.012, 0.995], [0.012, 0.999], [0.002, 0.999]];
const UV_WALL: [[f64; 2]; 3] = [[0.0, 0.995], [0.02, 0.995], [0.01, 0.999]];

impl BldMesh {
    fn put_quad(&mut self, q: [[f64; 3]; 4], uv: [[f64; 2]; 4], mat: Mat) {
        let i = self.n;
        for v in q {
            self.p.extend_from_slice(&v);
        }
        for v in uv {
            self.uv.extend_from_slice(&v);
        }
        for _ in 0..4 {
            self.c.extend_from_slice(&mat.rgba);
        }
        self.t.extend_from_slice(&[i, i + 1, i + 2, i, i + 2, i + 3]);
        self.m.extend_from_slice(&[mat.idx, mat.idx]);
        self.n += 4;
    }

    fn put_tri(&mut self, q: [[f64; 3]; 3], uv: [[f64; 2]; 3], mat: Mat) {
        let i = self.n;
        for v in q {
            self.p.extend_from_slice(&v);
        }
        for v in uv {
            self.uv.extend_from_slice(&v);
        }
        for _ in 0..3 {
            self.c.extend_from_slice(&mat.rgba);
        }
        self.t.extend_from_slice(&[i, i + 1, i + 2]);
        self.m.push(mat.idx);
        self.n += 3;
    }

    /// Mesh.poly: a flat polygon, ear clipped (a clockwise one reversed, its triangles turned back: its underside);
    /// cw: the winding when the caller measured it (points of another dtype: their area in it), else from the area.
    fn put_poly(&mut self, pts: &[[f64; 3]], uv: &[[f64; 2]], mat: Mat, cw: Option<bool>) {
        let xy: Vec<[f64; 2]> = pts.iter().map(|p| [p[0], p[1]]).collect();
        let k = xy.len() as i64;
        let tris: Vec<[i64; 3]> = if cw.unwrap_or_else(|| area2(&xy) < 0.0) {
            let rev: Vec<(f64, f64)> = xy.iter().rev().map(|p| (p[0], p[1])).collect();
            ear_clip(&rev).into_iter().map(|[a, b, c]| [k - 1 - c, k - 1 - b, k - 1 - a]).collect()
        } else {
            ear_clip(&xy.iter().map(|p| (p[0], p[1])).collect::<Vec<_>>())
        };
        let i = self.n;
        for v in pts {
            self.p.extend_from_slice(v);
        }
        for v in uv {
            self.uv.extend_from_slice(v);
        }
        for _ in 0..pts.len() {
            self.c.extend_from_slice(&mat.rgba);
        }
        for tr in &tris {
            self.t.extend_from_slice(&[tr[0] + i, tr[1] + i, tr[2] + i]);
        }
        self.m.extend(std::iter::repeat_n(mat.idx, tris.len()));
        self.n += pts.len() as i64;
    }

    /// buildings._box: an axis-u box (half sizes hx along u, hy across) from z0 to z1: four sides and a top.
    #[allow(clippy::too_many_arguments)]
    fn put_box(&mut self, c: [f64; 2], u: [f64; 2], hx: f64, hy: f64, z0: f64, z1: f64, mat: Mat) {
        let k = box_corners(c, u, hx, hy);
        for i in 0..4 {
            let (a, b) = (k[i], k[(i + 1) % 4]);
            self.put_quad([[a[0], a[1], z0], [b[0], b[1], z0], [b[0], b[1], z1], [a[0], a[1], z1]], UV_PLAIN, mat);
        }
        self.put_quad(std::array::from_fn(|i| [k[i][0], k[i][1], z1]), UV_PLAIN, mat);
    }

    /// buildings._box_tiled: _box with the material tiled at `tile` m per repeat.
    #[allow(clippy::too_many_arguments)]
    fn put_box_tiled(&mut self, c: [f64; 2], u: [f64; 2], hx: f64, hy: f64, z0: f64, z1: f64, mat: Mat, tile: f64) {
        let k = box_corners(c, u, hx, hy);
        for i in 0..4 {
            let (p, q) = (k[i], k[(i + 1) % 4]);
            let w = py_hypot(q[0] - p[0], q[1] - p[1]) / tile;
            let (hz0, hz1) = (z0 / tile, z1 / tile);
            self.put_quad([[p[0], p[1], z0], [q[0], q[1], z0], [q[0], q[1], z1], [p[0], p[1], z1]],
                          [[0.0, hz0], [w, hz0], [w, hz1], [0.0, hz1]], mat);
        }
        self.put_quad(std::array::from_fn(|i| [k[i][0], k[i][1], z1]),
                      std::array::from_fn(|i| [k[i][0] / tile, k[i][1] / tile]), mat);
    }
}

/// _box's corners: c + u * sx * hx + v * sy * hy for (sx, sy) in (-1, -1), (1, -1), (1, 1), (-1, 1) (numpy's order).
fn box_corners(c: [f64; 2], u: [f64; 2], hx: f64, hy: f64) -> [[f64; 2]; 4] {
    let v = [-u[1], u[0]];
    let s = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)];
    std::array::from_fn(|i| {
        let (sx, sy): (f64, f64) = s[i];
        std::array::from_fn(|d| (c[d] + u[d] * sx * hx) + v[d] * sy * hy)
    })
}

fn wrap_index(k: i64, n: usize) -> PyResult<usize> {
    let j = if k < 0 { k + n as i64 } else { k };
    if j < 0 || j >= n as i64 {
        return Err(PyIndexError::new_err(format!("index {k} out of range for {n} items")));
    }
    Ok(j as usize)
}

#[pymethods]
impl BldMesh {
    #[new]
    #[pyo3(signature = (*_args, **_kwargs))]
    fn new(_args: &Bound<'_, PyTuple>, _kwargs: Option<&Bound<'_, pyo3::types::PyDict>>) -> Self {
        BldMesh::default()
    }

    /// The vertices so far.
    #[getter]
    fn n(&self) -> i64 {
        self.n
    }

    /// Corners counter-clockwise seen from outside.
    #[allow(clippy::too_many_arguments)]
    fn quad(&mut self, a: &Bound<'_, PyAny>, b: &Bound<'_, PyAny>, c: &Bound<'_, PyAny>, d: &Bound<'_, PyAny>,
            uva: &Bound<'_, PyAny>, uvb: &Bound<'_, PyAny>, uvc: &Bound<'_, PyAny>, uvd: &Bound<'_, PyAny>,
            mat: &Bound<'_, PyAny>) -> PyResult<()> {
        let q = [pt::<3>(a)?, pt::<3>(b)?, pt::<3>(c)?, pt::<3>(d)?];
        let uv = [pt::<2>(uva)?, pt::<2>(uvb)?, pt::<2>(uvc)?, pt::<2>(uvd)?];
        self.put_quad(q, uv, mat_of(mat)?);
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn tri(&mut self, a: &Bound<'_, PyAny>, b: &Bound<'_, PyAny>, c: &Bound<'_, PyAny>, uva: &Bound<'_, PyAny>,
           uvb: &Bound<'_, PyAny>, uvc: &Bound<'_, PyAny>, mat: &Bound<'_, PyAny>) -> PyResult<()> {
        let q = [pt::<3>(a)?, pt::<3>(b)?, pt::<3>(c)?];
        let uv = [pt::<2>(uva)?, pt::<2>(uvb)?, pt::<2>(uvc)?];
        self.put_tri(q, uv, mat_of(mat)?);
        Ok(())
    }

    /// A flat polygon facing up when counter-clockwise seen from above, down when clockwise (its underside); cw: the
    /// winding as measured by the caller (buildings.Mesh.poly: points numpy makes another dtype than float64).
    #[pyo3(signature = (pts3, uv, mat, cw=None))]
    fn poly(&mut self, pts3: &Bound<'_, PyAny>, uv: &Bound<'_, PyAny>, mat: &Bound<'_, PyAny>, cw: Option<bool>)
            -> PyResult<()> {
        let pts = rows::<3>(pts3)?;
        let uvs = rows::<2>(uv)?;
        let mat = mat_of(mat)?;
        self.put_poly(&pts, &uvs, mat, cw);
        Ok(())
    }

    /// Quads from a kernel (earthfast.building_walls_np): P 12 numbers per quad, UV 8, mid the index into mats
    /// (material tokens, None for a slot no quad uses) per quad.
    fn quads(&mut self, p: &Bound<'_, PyAny>, uv: &Bound<'_, PyAny>, mid: &Bound<'_, PyAny>,
             mats: &Bound<'_, PyAny>) -> PyResult<()> {
        let mid: Vec<i64> = if let Ok(a) = mid.extract::<PyReadonlyArrayDyn<'_, i64>>() {
            a.as_array().iter().copied().collect()
        } else {
            mid.extract()?
        };
        let q = mid.len();
        if q == 0 {
            return Ok(());
        }
        let mats: Vec<Bound<'_, PyAny>> = mats.try_iter()?.collect::<PyResult<_>>()?;
        let toks: Vec<Option<Mat>> = mats.iter().map(|m| if m.is_none() { Ok(None) } else { mat_of(m).map(Some) })
            .collect::<PyResult<_>>()?;
        let mut used = Vec::with_capacity(q);
        for &k in &mid {
            used.push(wrap_index(k, toks.len())?);
        }
        if used.iter().any(|&j| toks[j].is_none()) {
            return Err(PyTypeError::new_err("a wall quad of no material"));
        }
        let pv = flat_f64(p)?;
        let uvv = flat_f64(uv)?;
        if pv.len() % 3 != 0 || uvv.len() % 2 != 0 {
            return Err(PyValueError::new_err("quads: positions not in threes or uvs not in pairs"));
        }
        let i = self.n;
        self.p.extend_from_slice(&pv);
        self.uv.extend_from_slice(&uvv);
        for &j in &used {
            let t = toks[j].unwrap();
            for _ in 0..4 {
                self.c.extend_from_slice(&t.rgba);
            }
        }
        for qi in 0..q as i64 {
            let b = qi * 4 + i;
            self.t.extend_from_slice(&[b, b + 1, b + 2, b, b + 2, b + 3]);
        }
        for &j in &used {
            let t = toks[j].unwrap();
            self.m.extend_from_slice(&[t.idx, t.idx]);
        }
        self.n += 4 * q as i64;
        Ok(())
    }

    /// Triangles P (3k, 3) with texture coordinates UV (3k, 2), all of material mat.
    fn tris(&mut self, p: &Bound<'_, PyAny>, uv: &Bound<'_, PyAny>, mat: &Bound<'_, PyAny>) -> PyResult<()> {
        let pv = flat_f64(p)?;
        if pv.len() % 3 != 0 {
            return Err(PyValueError::new_err("tris: positions not in threes"));
        }
        let k = pv.len() / 9;
        if k == 0 {
            return Ok(());
        }
        let uvv = flat_f64(uv)?;
        if uvv.len() < 6 * k || uvv.len() % 2 != 0 {
            return Err(PyValueError::new_err("tris: fewer uvs than positions"));
        }
        let mat = mat_of(mat)?;
        let i = self.n;
        self.p.extend_from_slice(&pv[..9 * k]);
        self.uv.extend_from_slice(&uvv[..6 * k]);
        for _ in 0..3 * k {
            self.c.extend_from_slice(&mat.rgba);
        }
        self.t.extend(i..i + 3 * k as i64);
        self.m.extend(std::iter::repeat_n(mat.idx, k));
        self.n += 3 * k as i64;
        Ok(())
    }

    /// (P (k, 3), UV (k, 2), T (t, 3), M (t,), C (k, 4) uint8): copies; each (material index, factor) of scale
    /// multiplies the uvs of every vertex a triangle of that material uses (buildings._OWN_UV_SCALE), in order.
    #[allow(clippy::type_complexity)]
    fn arrays_scaled<'py>(&self, py: Python<'py>, scale: Vec<(i64, f64)>)
        -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>, Bound<'py, PyAny>, Bound<'py, PyAny>, Bound<'py, PyAny>)> {
        let nv = self.p.len() / 3;
        let mut uv = self.uv.clone();
        let nuv = uv.len() / 2;
        for &(mi, f) in &scale {
            let mut mark = vec![false; nuv];
            let mut any = false;
            for (tri, &m) in self.t.chunks_exact(3).zip(&self.m) {
                if m == mi {
                    any = true;
                    for &v in tri {
                        let v = wrap_index(v, nuv)?;
                        mark[v] = true;
                    }
                }
            }
            if any {
                for (v, &on) in mark.iter().enumerate() {
                    if on {
                        uv[2 * v] *= f;
                        uv[2 * v + 1] *= f;
                    }
                }
            }
        }
        let sh = |v: Vec<f64>, n: usize, w: usize| -> PyResult<Bound<'py, PyAny>> {
            Ok(numpy::ndarray::Array2::from_shape_vec((n, w), v)
                .map_err(|e| PyValueError::new_err(e.to_string()))?.into_pyarray(py).into_any())
        };
        let p = sh(self.p.clone(), nv, 3)?;
        let uva = sh(uv, nuv, 2)?;
        let t = numpy::ndarray::Array2::from_shape_vec((self.t.len() / 3, 3), self.t.clone())
            .map_err(|e| PyValueError::new_err(e.to_string()))?.into_pyarray(py).into_any();
        let m = self.m.clone().into_pyarray(py).into_any();
        let c = numpy::ndarray::Array2::from_shape_vec((self.c.len() / 4, 4), self.c.clone())
            .map_err(|e| PyValueError::new_err(e.to_string()))?.into_pyarray(py).into_any();
        Ok((p, uva, t, m, c))
    }

    /// The content as flat lists (Mesh.P / UV / C / T / M): k 0 positions, 1 uvs, 2 colours, 3 triangles, 4 materials.
    fn flat<'py>(&self, py: Python<'py>, k: usize) -> PyResult<Bound<'py, PyList>> {
        match k {
            0 => PyList::new(py, &self.p),
            1 => PyList::new(py, &self.uv),
            2 => PyList::new(py, self.c.iter().map(|&v| v as i64)),
            3 => PyList::new(py, &self.t),
            4 => PyList::new(py, &self.m),
            _ => Err(PyValueError::new_err("flat: k is 0-4")),
        }
    }
}

/// buildings._box (m, centre, axis u, half sizes, z0, z1, token).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn bld_box(m: &Bound<'_, BldMesh>, cx: f64, cy: f64, ux: f64, uy: f64, hx: f64, hy: f64, z0: f64, z1: f64,
               mat: &Bound<'_, PyAny>) -> PyResult<()> {
    let mat = mat_of(mat)?;
    m.borrow_mut().put_box([cx, cy], [ux, uy], hx, hy, z0, z1, mat);
    Ok(())
}

/// buildings._box_tiled (m, centre, axis u, half sizes, z0, z1, token, tile).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn bld_box_tiled(m: &Bound<'_, BldMesh>, cx: f64, cy: f64, ux: f64, uy: f64, hx: f64, hy: f64, z0: f64, z1: f64,
                     mat: &Bound<'_, PyAny>, tile: f64) -> PyResult<()> {
    let mat = mat_of(mat)?;
    m.borrow_mut().put_box_tiled([cx, cy], [ux, uy], hx, hy, z0, z1, mat, tile);
    Ok(())
}

/// A flat lid over ring (n, 2) at z (the flat roof: uvs p / uv_m; uv_m None: every uv (0.002, 0.995), a part's
/// underside), its ring reversed first when rev (the underside's clockwise ring: build's ring[::-1]).
#[pyfunction]
#[pyo3(signature = (m, ring, z, uv_m, rev, mat))]
pub fn bld_lid(m: &Bound<'_, BldMesh>, ring: PyReadonlyArray2<'_, f64>, z: f64, uv_m: Option<f64>, rev: bool,
               mat: &Bound<'_, PyAny>) -> PyResult<()> {
    let mat = mat_of(mat)?;
    let mut r = ring2(&ring)?;
    if rev {
        r.reverse();
    }
    let pts: Vec<[f64; 3]> = r.iter().map(|p| [p[0], p[1], z]).collect();
    let uv: Vec<[f64; 2]> = match uv_m {
        Some(s) => r.iter().map(|p| [p[0] / s, p[1] / s]).collect(),
        None => vec![[0.002, 0.995]; r.len()],
    };
    m.borrow_mut().put_poly(&pts, &uv, mat, None);
    Ok(())
}

/// buildings._cornice: a box moulding round a flat roof's edge (out o, down / up from the eave), the token mat.
#[pyfunction]
pub fn bld_cornice(m: &Bound<'_, BldMesh>, ring: PyReadonlyArray2<'_, f64>, z_eave: f64, mat: &Bound<'_, PyAny>,
                   o: f64, down: f64, up: f64) -> PyResult<()> {
    let mat = mat_of(mat)?;
    let r = ring2(&ring)?;
    let mut mm = m.borrow_mut();
    let (z0, z1) = (z_eave - down, z_eave + up);
    let n = r.len();
    for i in 0..n {
        let (a, b) = (r[i], r[(i + 1) % n]);
        let e = [b[0] - a[0], b[1] - a[1]];
        let l = py_hypot(e[0], e[1]);
        if l < 0.3 {
            continue;
        }
        let d = [e[0] / l, e[1] / l];
        let nn = [d[1], -d[0]];
        let a2: [f64; 2] = std::array::from_fn(|k| (a[k] + nn[k] * o) - d[k] * o);
        let b2: [f64; 2] = std::array::from_fn(|k| (b[k] + nn[k] * o) + d[k] * o);
        mm.put_quad([[a2[0], a2[1], z0], [b2[0], b2[1], z0], [b2[0], b2[1], z1], [a2[0], a2[1], z1]], UV_PLAIN, mat);
        mm.put_quad([[a[0], a[1], z1], [a2[0], a2[1], z1], [b2[0], b2[1], z1], [b[0], b[1], z1]], UV_PLAIN, mat);
        mm.put_quad([[a[0], a[1], z0], [b[0], b[1], z0], [b2[0], b2[1], z0], [a2[0], a2[1], z0]], UV_PLAIN, mat);
        mm.put_quad([[b[0], b[1], z_eave], [a[0], a[1], z_eave], [a[0], a[1], z1], [b[0], b[1], z1]], UV_PLAIN, mat);
    }
    Ok(())
}

/// The edge `edge` of ring r: (a, b, its length) (ring[edge], ring[(edge + 1) % n]).
fn edge_of(r: &[[f64; 2]], edge: i64) -> PyResult<([f64; 2], [f64; 2], f64)> {
    let n = r.len();
    if n == 0 {
        return Err(PyIndexError::new_err("an edge of an empty ring"));
    }
    let i = wrap_index(edge, n)?;
    let a = r[i];
    let b = r[(edge + 1).rem_euclid(n as i64) as usize];
    Ok((a, b, py_hypot(b[0] - a[0], b[1] - a[1])))
}

/// buildings._stoop: marble steps up to the front door on edge `edge`, sd m along it; door() the ground at the door
/// (door_ground: called where the Python called it, past the degenerate edge's return), gmin the footprint's lowest.
/// k: STOOP_H, STOOP_RISE, STOOP_STEPS, STOOP_MAX_STEPS, STOOP_RISE_MAX, STOOP_LANDING, STOOP_TREAD, STOOP_W.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn bld_stoop(m: &Bound<'_, BldMesh>, ring: PyReadonlyArray2<'_, f64>, edge: i64, sd: f64, gmin: f64,
                 door: &Bound<'_, PyAny>, mat: &Bound<'_, PyAny>, k: Vec<f64>) -> PyResult<()> {
    if k.len() != 8 {
        return Err(PyValueError::new_err("bld_stoop: 8 constants"));
    }
    let mat = mat_of(mat)?;
    let r = ring2(&ring)?;
    let (a, b, l) = edge_of(&r, edge)?;
    if l < 1e-6 {
        return Ok(());
    }
    let (st_h, st_rise, steps, max_steps, rise_max, landing, tread, w) = (k[0], k[1], k[2], k[3], k[4], k[5], k[6], k[7]);
    let d = [(b[0] - a[0]) / l, (b[1] - a[1]) / l];
    let out = [d[1], -d[0]];
    let c = [a[0] + d[0] * sd, a[1] + d[1] * sd];
    let base: f64 = door.call0()?.extract()?;
    let top = base + st_h;
    let low = pmin(gmin, base);
    let z0 = low - 0.3;
    let x = (top - low) / st_rise;
    if !x.is_finite() {
        return Err(PyValueError::new_err("bld_stoop: a step count of a non-finite height"));
    }
    let n = x.round_ties_even().max(steps - 1.0).min(max_steps);      // int(np.clip(round(x), lo, hi))
    let rise = pmin((top - low) / n, rise_max);
    let mut mm = m.borrow_mut();
    for kk in 0..n as i64 {
        let kf = kk as f64;
        let depth = landing + kf * tread;
        let zt = top - kf * rise;
        let cc = [c[0] + out[0] * depth / 2.0, c[1] + out[1] * depth / 2.0];
        mm.put_box_tiled(cc, d, w / 2.0, depth / 2.0, z0, zt, mat, 1.0);
    }
    Ok(())
}

/// buildings._signs: the sign band over a shop's street wall (edge `edge`), shop by shop (hashed widths, a few without
/// one, hashed tints: tints[i] the token of "sign|i"). k: SIGN_SHOP_M[0], SIGN_SHOP_M[1], SIGN_OUT, SIGN_H.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn bld_signs(m: &Bound<'_, BldMesh>, ring: PyReadonlyArray2<'_, f64>, edge: i64, z_top: f64, seed_repr: &str,
                 tints: Vec<Bound<'_, PyAny>>, k: Vec<f64>) -> PyResult<()> {
    if k.len() != 4 {
        return Err(PyValueError::new_err("bld_signs: 4 constants"));
    }
    let r = ring2(&ring)?;
    let (a, b, l) = edge_of(&r, edge)?;
    if l < 3.0 {
        return Ok(());
    }
    let tints: Vec<Mat> = tints.iter().map(mat_of).collect::<PyResult<_>>()?;
    let (s0, s1, sout, sh) = (k[0], k[1], k[2], k[3]);
    let d = [(b[0] - a[0]) / l, (b[1] - a[1]) / l];
    let out = [d[1], -d[0]];
    let (mut t, mut kk) = (0.3f64, 0i64);
    let mut mm = m.borrow_mut();
    while t < l - 1.5 {
        let w = s0 + (s1 - s0) * h01(seed_repr, "signw", Some(kk));
        let mut t1 = pmin(l - 0.3, t + w);
        if l - 0.3 - t1 < 2.5 {
            t1 = l - 0.3;
        }
        if h01(seed_repr, "signon", Some(kk)) < 0.85 {
            let mid = (t + t1) / 2.0;
            let c: [f64; 2] = std::array::from_fn(|q| (a[q] + d[q] * mid) + out[q] * (sout / 2.0));
            let li = (h01(seed_repr, "signc", Some(kk)) * tints.len() as f64).trunc() as i64;
            let tok = tints[wrap_index(li, tints.len())?];
            mm.put_box(c, d, (t1 - t) / 2.0 - 0.15, sout / 2.0, z_top - 0.15 - sh, z_top - 0.15, tok);
        }
        t = t1;
        kk += 1;
    }
    Ok(())
}

/// buildings._clutter's boxes: roof_kit's (k, 6) rows (s, t, half along u, half across, height, material 0 facade /
/// 1 louvres / 2 units) on the roof at z_eave, in the oriented box's frame (centre c, axis u).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn bld_kit_boxes(m: &Bound<'_, BldMesh>, boxes: PyReadonlyArray2<'_, f64>, cx: f64, cy: f64, ux: f64, uy: f64,
                     z_eave: f64, own: &Bound<'_, PyAny>, louvre: &Bound<'_, PyAny>, unit: &Bound<'_, PyAny>,
                     louvre_tile: f64) -> PyResult<()> {
    let bx = boxes.as_array();
    if bx.nrows() > 0 && bx.ncols() != 6 {
        return Err(PyValueError::new_err("bld_kit_boxes: rows of 6"));
    }
    let (own, louvre, unit) = (mat_of(own)?, mat_of(louvre)?, mat_of(unit)?);
    let (c, u, v) = ([cx, cy], [ux, uy], [-uy, ux]);
    let mut mm = m.borrow_mut();
    for row in bx.rows() {
        let (s, t, hs, ht, h, mat) = (row[0], row[1], row[2], row[3], row[4], row[5]);
        let p: [f64; 2] = std::array::from_fn(|q| (c[q] + u[q] * s) + v[q] * t);
        if mat == 1.0 {
            mm.put_box_tiled(p, u, hs, ht, z_eave - 0.3, z_eave + h, louvre, louvre_tile);
        } else {
            mm.put_box(p, u, hs, ht, z_eave - 0.1, z_eave + h, if mat == 0.0 { own } else { unit });
        }
    }
    Ok(())
}

/// The box-frame roof's geometry (buildings._roof after its oriented box): centre c, ridge axis u, v across, half
/// sizes, the overhanging halves, the eave / ridge heights and the slope's length.
struct Frame {
    c: [f64; 2],
    u: [f64; 2],
    v: [f64; 2],
    hl: f64,
    hw2: f64,
    slope: f64,
    uvm: f64,
}

impl Frame {
    #[inline]
    fn at(&self, s: f64, t: f64, z: f64) -> [f64; 3] {
        [(self.c[0] + self.u[0] * s) + self.v[0] * t, (self.c[1] + self.u[1] * s) + self.v[1] * t, z]
    }
    #[inline]
    fn uv(&self, s: f64, t: f64) -> [f64; 2] {
        [s / self.uvm, (self.hw2 - t.abs()) / self.hw2 * self.slope / self.uvm]
    }
    /// end_wall: the wall up to the roof at an end of the house, outline (t, z) fanned, the facade's token.
    fn end_wall(&self, m: &mut BldMesh, sgn: f64, outline: &[(f64, f64)], fac: Mat) {
        let s = sgn * self.hl;
        let pts: Vec<[f64; 3]> = outline.iter().map(|&(t, z)| self.at(s, sgn * t, z)).collect();
        for j in 1..pts.len().saturating_sub(1) {
            m.put_tri([pts[0], pts[j], pts[j + 1]], UV_WALL, fac);
        }
    }
}

/// buildings._roof's box-frame forms (spec.roof: skillion, round, gambrel, mansard, pyramidal, half-hipped, gabled,
/// anything else hipped; gabled and hipped roofs a chimney by the seed, not lite), from the drawn ring's oriented box
/// (c, u, hl, hw; across: the ridge across it). roof_h: spec.roof_h (None / 0: none); hi: a skillion's high side;
/// chimney_cls: not a shed / barn; floors: spec.floors or 1. k: EAVE_M, ROOF_UV_M, SKILLION_PITCH, ROUND_RISE,
/// ROUND_SEGS, GAMBREL_PITCHES (2), GAMBREL_BREAK, MANSARD_PITCHES (2), MANSARD_INSET_M, HALF_HIP, CHIMNEY_SHARE,
/// CHIMNEY_HALF_M (2), CHIMNEY_OVER_M, CHIMNEY_MAX_M2.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (m, roof, cx, cy, ux, uy, hl, hw, across, z_eave, pitch, roof_h, hi, lite, rm, fac, seed_repr,
                    chimney_cls, floors, k))]
pub fn bld_roof(m: &Bound<'_, BldMesh>, roof: &str, cx: f64, cy: f64, ux: f64, uy: f64, hl: f64, hw: f64,
                across: bool, z_eave: f64, pitch: f64, roof_h: Option<f64>, hi: f64, lite: bool,
                rm: &Bound<'_, PyAny>, fac: &Bound<'_, PyAny>, seed_repr: &str, chimney_cls: bool, floors: f64,
                k: Vec<f64>) -> PyResult<()> {
    if k.len() != 18 {
        return Err(PyValueError::new_err("bld_roof: 18 constants"));
    }
    let (rm, fac) = (mat_of(rm)?, mat_of(fac)?);
    let (eave, uvm, sk_pitch, round_rise, round_segs) = (k[0], k[1], k[2], k[3], k[4]);
    let (gam_lo, gam_up, gam_break, man_lo, man_up, man_inset, half_hip) = (k[5], k[6], k[7], k[8], k[9], k[10], k[11]);
    let (ch_share, ch_h0, ch_h1, ch_over, ch_max) = (k[12], k[13], k[14], k[15], k[16]);
    let _ = k[17];
    let rad = |deg: f64| deg * (std::f64::consts::PI / 180.0);       // math.radians
    let tan = |deg: f64| libm::tan(rad(deg));                          // dmath.tan(math.radians(deg))
    let rh = roof_h.filter(|&v| v != 0.0);                            // (`spec.roof_h or ...`: 0 counts as none)
    let (mut u, mut hl, mut hw) = ([ux, uy], hl, hw);
    if across {
        u = [-u[1], u[0]];
        std::mem::swap(&mut hl, &mut hw);
    }
    let v = [-u[1], u[0]];
    let (hl2, hw2) = (hl + eave, hw + eave);
    let rise = hw2 * tan(pitch);
    let ze = z_eave - eave * tan(pitch);
    let zr = ze + rise;
    let f = Frame { c: [cx, cy], u, v, hl, hw2, slope: py_hypot(hw2, rise), uvm };
    let slope = f.slope;
    let mut mm = m.borrow_mut();
    let mm = &mut *mm;
    let corners = [((-1.0, -1.0), (1.0, -1.0)), ((1.0, -1.0), (1.0, 1.0)), ((1.0, 1.0), (-1.0, 1.0)),
                   ((-1.0, 1.0), (-1.0, -1.0))];
    match roof {
        "skillion" => {
            let tn = match rh { Some(r) => r / (2.0 * pmax(hw, 0.5)), None => tan(sk_pitch) };
            let z = |t: f64| z_eave + (hi * t + hw) * tn;
            mm.put_quad([f.at(-hl2, -hw2, z(-hw2)), f.at(hl2, -hw2, z(-hw2)), f.at(hl2, hw2, z(hw2)),
                         f.at(-hl2, hw2, z(hw2))],
                        [f.uv(-hl2, -hw2), f.uv(hl2, -hw2), [hl2 / uvm, 2.0 * hw2 / uvm], [-hl2 / uvm, 2.0 * hw2 / uvm]],
                        rm);
            let top = z(hi * hw);
            let (a, b) = if hi > 0.0 { (hl, -hl) } else { (-hl, hl) };
            mm.put_quad([f.at(a, hi * hw, z_eave), f.at(b, hi * hw, z_eave), f.at(b, hi * hw, top), f.at(a, hi * hw, top)],
                        [[0.0, 0.995], [0.02, 0.995], [0.02, 0.999], [0.0, 0.999]], fac);
            for sgn in [-1.0, 1.0] {
                f.end_wall(mm, sgn, &[(-hw, z_eave), (hw, z_eave), (sgn * hi * hw, top)], fac);
            }
        }
        "round" => {
            let rise = rh.unwrap_or(hw * round_rise);
            let segs = round_segs as i64;
            let ts: Vec<f64> = (0..=segs).map(|kk| -hw + 2.0 * hw * kk as f64 / round_segs).collect();
            let zs: Vec<f64> = ts.iter().map(|&t| { let q = t / hw; z_eave + rise * pmax(0.0, 1.0 - q * q).sqrt() })
                .collect();
            let mut arc = 0.0;
            for kk in 0..segs as usize {
                let (t0, t1, z0, z1) = (ts[kk], ts[kk + 1], zs[kk], zs[kk + 1]);
                let seg = py_hypot(t1 - t0, z1 - z0);
                mm.put_quad([f.at(hl, t0, z0), f.at(hl, t1, z1), f.at(-hl, t1, z1), f.at(-hl, t0, z0)],
                            [[hl / uvm, arc / uvm], [hl / uvm, (arc + seg) / uvm], [-hl / uvm, (arc + seg) / uvm],
                             [-hl / uvm, arc / uvm]], rm);
                arc += seg;
            }
            let mut outline = vec![(-hw, z_eave), (hw, z_eave)];
            let rev: Vec<(f64, f64)> = ts.iter().rev().copied().zip(zs.iter().rev().copied()).collect();
            if rev.len() > 2 {
                outline.extend_from_slice(&rev[1..rev.len() - 1]);
            }
            for sgn in [-1.0, 1.0] {
                f.end_wall(mm, sgn, &outline, fac);
            }
        }
        "gambrel" => {
            let (lo, up) = (tan(gam_lo), tan(gam_up));
            let tb = hw * gam_break;
            let ze = z_eave - eave * lo;
            let zb = z_eave + (hw - tb) * lo;
            let zr = zb + tb * up;
            for sgn in [-1.0, 1.0] {
                for (t0, z0, t1, z1) in [(hw2, ze, tb, zb), (tb, zb, 0.0, zr)] {
                    let (a, b) = if sgn < 0.0 { (-hl2, hl2) } else { (hl2, -hl2) };
                    mm.put_quad([f.at(a, sgn * t0, z0), f.at(b, sgn * t0, z0), f.at(b, sgn * t1, z1), f.at(a, sgn * t1, z1)],
                                [f.uv(a, t0), f.uv(b, t0), f.uv(b, t1), f.uv(a, t1)], rm);
                }
            }
            for sgn in [-1.0, 1.0] {
                f.end_wall(mm, sgn, &[(-hw, z_eave), (hw, z_eave), (tb, zb), (0.0, zr - eave * 0.2), (-tb, zb)], fac);
            }
        }
        "mansard" => {
            let (lo, up) = (tan(man_lo), tan(man_up));
            let inset = pmin(0.3 * hw, man_inset);
            let ze = z_eave - eave * lo;
            let zb = z_eave + inset * lo;
            let (il, iw) = (hl - inset, hw - inset);
            let zr = zb + iw * up;
            let r = pmax(il - iw, 0.0);
            for ((a, ta), (b, tb)) in corners {
                mm.put_quad([f.at(a * hl2, ta * hw2, ze), f.at(b * hl2, tb * hw2, ze), f.at(b * il, tb * iw, zb),
                             f.at(a * il, ta * iw, zb)],
                            [[0.0, 0.0], [2.0 * hl2 / uvm, 0.0], [2.0 * hl2 / uvm, (inset + eave) / uvm],
                             [0.0, (inset + eave) / uvm]], rm);
            }
            mm.put_quad([f.at(-il, -iw, zb), f.at(il, -iw, zb), f.at(r, 0.0, zr), f.at(-r, 0.0, zr)],
                        [f.uv(-il, -iw), f.uv(il, -iw), f.uv(r, 0.0), f.uv(-r, 0.0)], rm);
            mm.put_quad([f.at(il, iw, zb), f.at(-il, iw, zb), f.at(-r, 0.0, zr), f.at(r, 0.0, zr)],
                        [f.uv(il, iw), f.uv(-il, iw), f.uv(-r, 0.0), f.uv(r, 0.0)], rm);
            mm.put_tri([f.at(il, -iw, zb), f.at(il, iw, zb), f.at(r, 0.0, zr)],
                       [[-iw / uvm, 0.0], [iw / uvm, 0.0], [0.0, slope / uvm]], rm);
            mm.put_tri([f.at(-il, iw, zb), f.at(-il, -iw, zb), f.at(-r, 0.0, zr)],
                       [[-iw / uvm, 0.0], [iw / uvm, 0.0], [0.0, slope / uvm]], rm);
        }
        "pyramidal" => {
            let apex = f.at(0.0, 0.0, zr);
            for ((a, ta), (b, tb)) in corners {
                mm.put_tri([f.at(a * hl2, ta * hw2, ze), f.at(b * hl2, tb * hw2, ze), apex],
                           [[-hl2 / uvm, 0.0], [hl2 / uvm, 0.0], [0.0, slope / uvm]], rm);
            }
        }
        "half-hipped" => {
            let tw = hw2 * half_hip;
            let zc = zr - rise * half_hip;
            let r = pmax(hl2 - tw, 0.0);
            mm.put_quad([f.at(-hl2, -hw2, ze), f.at(hl2, -hw2, ze), f.at(hl2, -tw, zc), f.at(-hl2, -tw, zc)],
                        [f.uv(-hl2, -hw2), f.uv(hl2, -hw2), f.uv(hl2, -tw), f.uv(-hl2, -tw)], rm);
            mm.put_quad([f.at(-hl2, -tw, zc), f.at(hl2, -tw, zc), f.at(r, 0.0, zr), f.at(-r, 0.0, zr)],
                        [f.uv(-hl2, -tw), f.uv(hl2, -tw), f.uv(r, 0.0), f.uv(-r, 0.0)], rm);
            mm.put_quad([f.at(hl2, hw2, ze), f.at(-hl2, hw2, ze), f.at(-hl2, tw, zc), f.at(hl2, tw, zc)],
                        [f.uv(hl2, hw2), f.uv(-hl2, hw2), f.uv(-hl2, tw), f.uv(hl2, tw)], rm);
            mm.put_quad([f.at(hl2, tw, zc), f.at(-hl2, tw, zc), f.at(-r, 0.0, zr), f.at(r, 0.0, zr)],
                        [f.uv(hl2, tw), f.uv(-hl2, tw), f.uv(-r, 0.0), f.uv(r, 0.0)], rm);
            mm.put_tri([f.at(hl2, -tw, zc), f.at(hl2, tw, zc), f.at(r, 0.0, zr)],
                       [[-tw / uvm, 0.0], [tw / uvm, 0.0], [0.0, slope / uvm]], rm);
            mm.put_tri([f.at(-hl2, tw, zc), f.at(-hl2, -tw, zc), f.at(-r, 0.0, zr)],
                       [[-tw / uvm, 0.0], [tw / uvm, 0.0], [0.0, slope / uvm]], rm);
            let top = zc + eave * rise / hw2;
            for sgn in [-1.0, 1.0] {
                f.end_wall(mm, sgn, &[(-hw, z_eave), (hw, z_eave), (tw, top), (-tw, top)], fac);
            }
        }
        _ => {
            let gabled = roof == "gabled";
            if (gabled || roof == "hipped") && !lite {
                // _chimney: a stack on the ridge line, for a house that has one by its seed
                let ridge_half = if gabled { hl2 } else { pmax(hl2 - hw2, 0.0) };
                if !(!chimney_cls || floors > 3.0 || ridge_half < 1.2 || hw < 2.0
                     || 4.0 * (ridge_half + hw) * hw > ch_max || h01(seed_repr, "chimney", None) >= ch_share) {
                    let half = ch_h0 + (ch_h1 - ch_h0) * h01(seed_repr, "chimney size", None);
                    let along = (2.0 * h01(seed_repr, "chimney at", None) - 1.0) * 0.7 * (ridge_half - half);
                    let side = (if h01(seed_repr, "chimney side", None) < 0.5 { 0.6 } else { -0.6 })
                        * pmin(1.0, 0.25 * hw);
                    let centre: [f64; 2] = std::array::from_fn(|q| ([cx, cy][q] + u[q] * along) + v[q] * side);
                    mm.put_box(centre, u, half, half, zr - 1.2, zr + ch_over, fac);
                }
            }
            if gabled {
                let r = hl2;
                mm.put_quad([f.at(-hl2, -hw2, ze), f.at(hl2, -hw2, ze), f.at(r, 0.0, zr), f.at(-r, 0.0, zr)],
                            [f.uv(-hl2, -hw2), f.uv(hl2, -hw2), f.uv(r, 0.0), f.uv(-r, 0.0)], rm);
                mm.put_quad([f.at(hl2, hw2, ze), f.at(-hl2, hw2, ze), f.at(-r, 0.0, zr), f.at(r, 0.0, zr)],
                            [f.uv(hl2, hw2), f.uv(-hl2, hw2), f.uv(-r, 0.0), f.uv(r, 0.0)], rm);
                for sgn in [-1.0, 1.0] {
                    let s = sgn * hl;
                    let (a, b, top) = (f.at(s, -sgn * hw, z_eave), f.at(s, sgn * hw, z_eave),
                                       f.at(s, 0.0, zr - eave * 0.2));
                    mm.put_tri([a, b, top], UV_WALL, fac);
                }
            } else {
                let r = pmax(hl2 - hw2, 0.0);
                mm.put_quad([f.at(-hl2, -hw2, ze), f.at(hl2, -hw2, ze), f.at(r, 0.0, zr), f.at(-r, 0.0, zr)],
                            [f.uv(-hl2, -hw2), f.uv(hl2, -hw2), f.uv(r, 0.0), f.uv(-r, 0.0)], rm);
                mm.put_quad([f.at(hl2, hw2, ze), f.at(-hl2, hw2, ze), f.at(-r, 0.0, zr), f.at(r, 0.0, zr)],
                            [f.uv(hl2, hw2), f.uv(-hl2, hw2), f.uv(-r, 0.0), f.uv(r, 0.0)], rm);
                mm.put_tri([f.at(hl2, -hw2, ze), f.at(hl2, hw2, ze), f.at(r, 0.0, zr)],
                           [[-hw2 / uvm, 0.0], [hw2 / uvm, 0.0], [0.0, slope / uvm]], rm);
                mm.put_tri([f.at(-hl2, hw2, ze), f.at(-hl2, -hw2, ze), f.at(-r, 0.0, zr)],
                           [[-hw2 / uvm, 0.0], [hw2 / uvm, 0.0], [0.0, slope / uvm]], rm);
            }
        }
    }
    Ok(())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<BldMesh>()?;
    m.add_function(wrap_pyfunction!(bld_box, m)?)?;
    m.add_function(wrap_pyfunction!(bld_box_tiled, m)?)?;
    m.add_function(wrap_pyfunction!(bld_lid, m)?)?;
    m.add_function(wrap_pyfunction!(bld_cornice, m)?)?;
    m.add_function(wrap_pyfunction!(bld_stoop, m)?)?;
    m.add_function(wrap_pyfunction!(bld_signs, m)?)?;
    m.add_function(wrap_pyfunction!(bld_kit_boxes, m)?)?;
    m.add_function(wrap_pyfunction!(bld_roof, m)?)?;
    Ok(())
}
