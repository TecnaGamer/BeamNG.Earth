//! oneway_joins.apply in Rust, byte for byte the Python it replaced (the full Rust port: the largest Python self-time
//! of a cold first visit's roads layers, 2026-11-18): one-way roads meeting two-way roads at a point move their end to
//! the two-way's side where traffic goes their way, a quarter of its width (lanes x LANE_M, else TWO_WAY_W_M; at most
//! MAX_SHIFT_M), faded out along the one-way over BLEND_M. In place on the layer's lon / lat points, in the Python's
//! order (a later end's direction read from points an earlier move changed; the point index from the points as they
//! were). math.hypot is CPython's (crate::py_hypot), dmath.cos the pinned libm's.
//!
//! Input contract (R2a, roadnet::pyobj::Contract): lonlat an exact C-contiguous, aligned, writeable float64 numpy array
//! of two columns or more; the offsets int64 arrays; osm_oneway None or a column of the contract (its items' str()
//! read); osm_lanes None or a float64 array. Anything else TypeError.

use std::collections::HashMap;

use numpy::{PyReadonlyArray2, PyReadwriteArray2, PyUntypedArray, PyUntypedArrayMethods};
use pyo3::prelude::*;

use crate::dm::Dm;
use crate::roadnet::countries::np_index;
use crate::roadnet::pyobj::{py_str, CArr1, Contract};

const BLEND_M: f64 = 20.0;
const TWO_WAY_W_M: f64 = 8.0;
const LANE_M: f64 = 3.3;
const MAX_SHIFT_M: f64 = 5.0;
const KEY_DEG: f64 = 1e-6;

/// round(v / KEY_DEG) as the key's bits: numpy's round (half to even) of the double, -0 as 0 (Python's int has no
/// sign of zero); NaN ValueError and inf OverflowError, as int() of them raised.
fn key(v: f64) -> PyResult<u64> {
    let x = v / KEY_DEG;
    if x.is_nan() {
        return Err(pyo3::exceptions::PyValueError::new_err("cannot convert float NaN to integer"));
    }
    if x.is_infinite() {
        return Err(pyo3::exceptions::PyOverflowError::new_err("cannot convert float infinity to integer"));
    }
    Ok((x.round_ties_even() + 0.0).to_bits())
}

/// oneway_joins.apply(feats, left_hand) past its early return: the number of ends moved.
#[pyfunction]
#[pyo3(signature = (lonlat, part_off, ring_off, coord_off, oneway, lanes, left_hand, n))]
#[allow(clippy::too_many_arguments)]
pub fn oneway_join_ends<'py>(py: Python<'py>, lonlat: Bound<'py, PyAny>, part_off: CArr1<'py, i64>,
                             ring_off: CArr1<'py, i64>, coord_off: CArr1<'py, i64>, oneway: Bound<'py, PyAny>,
                             lanes: Option<CArr1<'py, f64>>, left_hand: bool, n: usize) -> PyResult<i64> {
    let refuse = |what: &str| pyo3::exceptions::PyTypeError::new_err(format!(
        "oneway_join_ends: {what} (the R2a input contract)"));
    let k = Contract::new(py)?;
    if !lonlat.get_type().is(&py.import("numpy")?.getattr("ndarray")?) {
        return Err(refuse("lonlat is no exact numpy array"));
    }
    // (a read-only array is read as it is and refused at the first move, as numpy's in-place write refused it:
    // ValueError)
    let u = lonlat.cast::<PyUntypedArray>()?;
    // SAFETY: as_array_ptr is the live array lonlat holds; its flags are read, nothing written.
    let writeable = unsafe { (*u.as_array_ptr()).fields.flags & numpy::npyffi::NPY_ARRAY_WRITEABLE != 0 };
    if !u.is_c_contiguous() || u.ndim() != 2 || u.shape()[1] < 2 {
        return Err(refuse("lonlat is no C-contiguous (k, 2+) array"));
    }
    let (npts, ncols) = (u.shape()[0], u.shape()[1]);
    let mut rw: Option<PyReadwriteArray2<'py, f64>> = None;
    let mut copy: Vec<f64> = Vec::new();
    if writeable {
        rw = Some(lonlat.extract().map_err(|_| refuse("lonlat is no float64 array"))?);
    } else {
        let r: PyReadonlyArray2<'py, f64> = lonlat.extract().map_err(|_| refuse("lonlat is no float64 array"))?;
        copy = r.as_slice()?.to_vec();
    }
    if !oneway.is_none() {
        k.column(&oneway, "osm_oneway")?;
    }
    let (po, ro, co) = (part_off.as_slice()?, ring_off.as_slice()?, coord_off.as_slice()?);
    let lanes = match &lanes { Some(l) => Some(l.as_slice()?), None => None };
    let pts: &mut [f64] = match rw.as_mut() { Some(a) => a.as_slice_mut()?, None => &mut copy };
    let at_of = |c: i64| -> PyResult<usize> { Ok(np_index(npts, c)? * ncols) };
    // _oneway(feats, i): str(osm_oneway[i]) in ("yes", "1", "true") -> 1, "-1" -> -1, else 0; asked where the Python
    // asked it (its IndexError at the same point), the same value after
    let mut ow_memo: Vec<Option<i8>> = vec![None; n];
    let mut ow = |i: usize| -> PyResult<i8> {
        if oneway.is_none() {
            return Ok(0);
        }
        if let Some(Some(v)) = ow_memo.get(i) {
            return Ok(*v);
        }
        let s = py_str(&oneway.get_item(i)?)?;
        let v = if s == "yes" || s == "1" || s == "true" { 1 } else if s == "-1" { -1 } else { 0 };
        if i < ow_memo.len() {
            ow_memo[i] = Some(v);
        }
        Ok(v)
    };
    // rings: (feature, first coord, last coord) of each line's first ring
    let mut rings = Vec::with_capacity(n);
    for i in 0..n {
        let r = ro[np_index(ro.len(), po[np_index(po.len(), i as i64)?])?];
        let a = co[np_index(co.len(), r)?];
        let b = co[np_index(co.len(), r.wrapping_add(1))?].wrapping_sub(1);
        rings.push((i, a, b));
    }
    // at: rounded point -> [(feature, coord index)], every coordinate of every line, from the points as they are now
    let mut at: HashMap<(u64, u64), Vec<(usize, i64)>> = HashMap::new();
    for &(i, a, b) in &rings {
        let mut c = a;
        while c <= b {
            let p = at_of(c)?;
            at.entry((key(pts[p])?, key(pts[p + 1])?)).or_default().push((i, c));
            c += 1;
        }
    }
    let mut moved = 0i64;
    for &(i, a, b) in &rings {
        let o = ow(i)?;
        if o == 0 || b.wrapping_sub(a) < 1 {
            continue;
        }
        for (end, nxt, sign) in [(a, a.wrapping_add(1), 1i64), (b, b.wrapping_sub(1), -1i64)] {
            let pe = at_of(end)?;
            let kk = (key(pts[pe])?, key(pts[pe + 1])?);
            let others: Vec<(usize, i64)> = at.get(&kk).map(|v| v.iter().copied().filter(|&(j, _)| j != i).collect())
                .unwrap_or_default();
            let mut two = Vec::new();
            for &(j, c) in &others {
                if ow(j)? == 0 {
                    two.push((j, c));
                }
            }
            if two.is_empty() {
                continue;
            }
            // any(_oneway(feats, j) for j, _ in others): in order, up to the first one-way
            let mut any_one = false;
            for &(j, _) in &others {
                if ow(j)? != 0 {
                    any_one = true;
                    break;
                }
            }
            if any_one {
                continue;
            }
            let lat = pts[pe + 1].to_radians();
            let mx = 111320.0 * lat.dcos();
            let my = 110540.0;
            let pn = at_of(nxt)?;
            let s = sign as f64;
            let o = o as f64;
            let mut dx = (pts[pn] - pts[pe]) * mx * s;
            let mut dy = (pts[pn + 1] - pts[pe + 1]) * my * s;
            dx *= o;
            dy *= o;
            let l = crate::py_hypot(dx, dy);
            if l < 1e-6 {
                continue;
            }
            dx /= l;
            dy /= l;
            let j = two[0].0;
            let mut w2 = TWO_WAY_W_M;
            if let Some(lv) = lanes {
                let v = *lv.get(j).ok_or_else(|| pyo3::exceptions::PyIndexError::new_err(format!(
                    "index {j} is out of bounds for axis 0 with size {}", lv.len())))?;
                if !v.is_nan() {
                    w2 = v * LANE_M;
                }
            }
            // min(w2 / 4.0, MAX_SHIFT_M): the first unless the second is less
            let q = w2 / 4.0;
            let off = if MAX_SHIFT_M < q { MAX_SHIFT_M } else { q };
            let (rx, ry) = if !left_hand { (dy, -dx) } else { (-dy, dx) };
            let (ox, oy) = (rx * off / mx, ry * off / my);
            let mut run = 0.0f64;
            let (mut c, mut prev) = (end, end);
            while a <= c && c <= b {
                let pc = at_of(c)?;
                if c != end {
                    let pp = at_of(prev)?;
                    run += crate::py_hypot((pts[pc] - pts[pp]) * mx, (pts[pc + 1] - pts[pp + 1]) * my);
                }
                // max(0.0, 1.0 - run / BLEND_M): the first unless the second is greater
                let v = 1.0 - run / BLEND_M;
                let t = if v > 0.0 { v } else { 0.0 };
                if t <= 0.0 {
                    break;
                }
                if !writeable {
                    return Err(pyo3::exceptions::PyValueError::new_err("assignment destination is read-only"));
                }
                pts[pc] += ox * t;
                pts[pc + 1] += oy * t;
                prev = c;
                c = c.wrapping_add(sign);
            }
            moved += 1;
        }
    }
    Ok(moved)
}
