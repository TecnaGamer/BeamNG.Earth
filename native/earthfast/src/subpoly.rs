//! roads.sub_polyline: the part of a lon/lat polyline between two fractions of its length (lon scaled by the cosine
//! of the ring's mean latitude), and sub_polylines: many rings x many fraction ranges in one call (a feature's level
//! parts: roads._parts_by_level_new / _part_keys_new, railways._pieces_of, roads.height_limit_lines; ~13k calls a
//! three-place cold road tile set, each its own dozen NumPy calls). The numbers are the old NumPy code's: numpy's
//! pairwise mean of the latitudes, CPython's radians (x * (pi / 180)), the dm cosine and hypot, cumsum's sequential
//! sums and dm::interp (numpy's np.interp) at both ends. Finite coordinates give the same bits; of a NaN made from
//! two NaN operands (an infinity's difference meeting a NaN coordinate) the payload may differ, as it may between
//! numpy's own compiled loops.

use numpy::{IntoPyArray, PyArray2, PyArrayMethods, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::exceptions::{PyIndexError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyList, PySlice};

use crate::dm::{interp, Dm};
use crate::levels::np_sum;

/// A ring read in place: k rows of d columns at byte strides (s0, s1) from data (NumPy's own layout, any strides and
/// alignment: every element read unaligned at its exact byte offset).
struct Ring {
    data: *const u8,
    k: usize,
    d: usize,
    s0: isize,
    s1: isize,
}

/// The elements NumPy's buffered reductions take at a time (np.getbufsize(), 8192: never changed by the server).
const NP_BUFSIZE: usize = 8192;

impl Ring {
    fn of(a: &PyReadonlyArray2<'_, f64>) -> PyResult<Ring> {
        let (sh, st) = (a.shape(), a.strides());
        if sh[1] < 2 {
            // (the old code's ring[:, 1])
            return Err(PyIndexError::new_err("sub_polyline: a ring (k, >= 2) of lon, lat"));
        }
        Ok(Ring { data: a.data() as *const u8, k: sh[0], d: sh[1], s0: st[0], s1: st[1] })
    }

    #[inline]
    fn at(&self, i: usize, j: usize) -> f64 {
        debug_assert!(i < self.k && j < self.d);
        // SAFETY: i < k and j < d, the array's shape; its buffer stays borrowed (read only) while the Ring is used
        unsafe { std::ptr::read_unaligned(self.data.offset(i as isize * self.s0 + j as isize * self.s1) as *const f64) }
    }

    /// Whether NumPy reduces the latitude column ring[:, 1] through its buffer: the column not aligned, its start or
    /// (over more than one row) its row stride not a multiple of 8 bytes (numpy's raw_array_is_aligned).
    fn lat_buffered(&self) -> bool {
        let start = (self.data as usize as isize).wrapping_add(self.s1);
        self.k > 1 && (start % 8 != 0 || self.s0 % 8 != 0)
    }

    fn col(&self, j: usize, out: &mut Vec<f64>) {
        out.clear();
        out.extend((0..self.k).map(|i| self.at(i, j)));
    }
}

/// The cumulative length s (k,) of a ring with k >= 2 in sub_polyline's metric: np.r_[0.0, np.cumsum(dnp.hypot(
/// np.diff(lon) * c, np.diff(lat)))], c = dmath.cos(math.radians(np.mean(lat))). lon, lat: its first two columns;
/// buffered: NumPy's mean of an unaligned column, a pairwise sum per NP_BUFSIZE values added up in order.
fn arc(lon: &[f64], lat: &[f64], buffered: bool) -> Vec<f64> {
    let k = lon.len();
    let sum = if buffered {
        let mut acc = 0.0f64;
        for ch in lat.chunks(NP_BUFSIZE) {
            acc += np_sum(ch);
        }
        acc
    } else {
        np_sum(lat)
    };
    let mean = sum / k as f64;
    let c = (mean * (std::f64::consts::PI / 180.0)).dcos();
    let mut s = Vec::with_capacity(k);
    s.push(0.0);
    for i in 1..k {
        let seg = ((lon[i] - lon[i - 1]) * c).dhypot(lat[i] - lat[i - 1]);
        // (cumsum: its first value the first segment itself, then sequential sums)
        s.push(if i == 1 { seg } else { s[i - 1] + seg });
    }
    s
}

/// Whether sub_polyline gives the ring's first point alone (ring[:1]): fewer than two points, or no length.
fn degenerate(k: usize, s: &[f64]) -> bool {
    k < 2 || s[k - 1] <= 0.0
}

/// The rows of sub_polyline(ring, a, b) of a ring that is not degenerate (s: its arc): the point at a, the vertices
/// strictly between and the point at b, row-major.
fn cut(r: &Ring, s: &[f64], a: f64, b: f64, col: &mut Vec<f64>) -> (Vec<f64>, usize) {
    let (k, d) = (r.k, r.d);
    let total = s[k - 1];
    let (sa, sb) = (a * total, b * total);
    let inner: Vec<usize> = (0..k).filter(|&i| s[i] > sa && s[i] < sb).collect();
    let rows = inner.len() + 2;
    let mut out = vec![0.0f64; rows * d];
    for j in 0..d {
        r.col(j, col);
        out[j] = interp(&[sa], s, col, None, None)[0];
        out[(rows - 1) * d + j] = interp(&[sb], s, col, None, None)[0];
        for (n, &i) in inner.iter().enumerate() {
            out[(n + 1) * d + j] = col[i];
        }
    }
    (out, rows)
}

/// How many rows sub_polyline(ring, a, b) has (s: the ring's arc, empty for fewer than two points).
fn rows_of(k: usize, s: &[f64], a: f64, b: f64) -> usize {
    if degenerate(k, s) {
        return k.min(1);
    }
    let total = s[k - 1];
    let (sa, sb) = (a * total, b * total);
    2 + s.iter().filter(|&&v| v > sa && v < sb).count()
}

fn to_py<'py>(py: Python<'py>, v: Vec<f64>, rows: usize, d: usize) -> PyResult<Bound<'py, PyAny>> {
    let arr = numpy::ndarray::Array2::from_shape_vec((rows, d), v).map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(arr.into_pyarray(py).into_any())
}

/// ring[:1], a view of the ring as the old code gave it.
fn first<'py>(py: Python<'py>, ring: &Bound<'py, PyArray2<f64>>) -> PyResult<Bound<'py, PyAny>> {
    ring.as_any().get_item(PySlice::new(py, 0, 1, 1))
}

/// A ring's arc (empty for fewer than two points) from its first two columns.
fn arc_of(r: &Ring, col: &mut Vec<f64>) -> Vec<f64> {
    if r.k < 2 {
        return Vec::new();
    }
    r.col(0, col);
    let lon = col.clone();
    r.col(1, col);
    arc(&lon, col, r.lat_buffered())
}

/// roads.sub_polyline: the part of a lon/lat polyline (k, >= 2 columns: lon, lat, ...) between fractions a and b of
/// its length, (rows, d) float64: the point at a, the vertices strictly between, the point at b; a ring of no length
/// (or fewer than two points) ring[:1], a view of it.
#[pyfunction]
pub fn sub_polyline<'py>(py: Python<'py>, ring: PyReadonlyArray2<'py, f64>, a: f64, b: f64)
    -> PyResult<Bound<'py, PyAny>> {
    let r = Ring::of(&ring)?;
    let mut col = Vec::new();
    let s = arc_of(&r, &mut col);
    if degenerate(r.k, &s) {
        return first(py, &ring);
    }
    let (v, rows) = cut(&r, &s, a, b, &mut col);
    to_py(py, v, rows, r.d)
}

/// sub_polyline of every ring with every (a, b) range: a list of len(rings) * len(pieces), ring by ring, its ranges
/// in their order. whole: a range (0.0, 1.0) gives the ring object itself, uncut (the callers' shortcut). rows_only:
/// each entry the number of rows instead (the callers keep parts of two or more). Each ring is read in place and
/// done before the next (native scratch: three of its columns and one part).
#[pyfunction]
#[pyo3(signature = (rings, pieces, whole=true, rows_only=false))]
pub fn sub_polylines<'py>(py: Python<'py>, rings: Vec<PyReadonlyArray2<'py, f64>>, pieces: Vec<(f64, f64)>,
                          whole: bool, rows_only: bool) -> PyResult<Bound<'py, PyList>> {
    let out = PyList::empty(py);
    let is_whole = |a: f64, b: f64| whole && a == 0.0 && b == 1.0;
    let mut col = Vec::new();
    for ring in &rings {
        if pieces.iter().all(|&(a, b)| is_whole(a, b)) {
            // (no cut: the ring itself each time, nothing read or checked, as the callers' shortcut)
            for _ in &pieces {
                if rows_only {
                    out.append(ring.shape()[0])?;
                } else {
                    out.append(ring.as_any())?;
                }
            }
            continue;
        }
        let r = Ring::of(ring)?;
        let s = arc_of(&r, &mut col);
        let degen = degenerate(r.k, &s);
        for &(a, b) in &pieces {
            if is_whole(a, b) {
                if rows_only {
                    out.append(r.k)?;
                } else {
                    out.append(ring.as_any())?;
                }
            } else if rows_only {
                out.append(rows_of(r.k, &s, a, b))?;
            } else if degen {
                out.append(first(py, ring)?)?;
            } else {
                let (v, rows) = cut(&r, &s, a, b, &mut col);
                out.append(to_py(py, v, rows, r.d)?)?;
            }
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ring(v: &[f64]) -> Ring {
        Ring { data: v.as_ptr() as *const u8, k: v.len() / 2, d: 2, s0: 16, s1: 8 }
    }

    fn sub(v: &[f64], a: f64, b: f64) -> (Vec<f64>, usize) {
        let r = ring(v);
        let mut col = Vec::new();
        let s = arc_of(&r, &mut col);
        assert!(!degenerate(r.k, &s));
        assert_eq!(rows_of(r.k, &s, a, b), cut(&r, &s, a, b, &mut col).1);
        cut(&r, &s, a, b, &mut col)
    }

    #[test]
    fn cuts_between_and_at_vertices() {
        let v = [0.0, 0.0, 0.0, 1.0, 0.0, 2.0, 0.0, 4.0];
        assert_eq!(sub(&v, 0.25, 0.75), (vec![0.0, 1.0, 0.0, 2.0, 0.0, 3.0], 3));
        // (a vertex at a range's end is the interpolated end, not an inner point)
        assert_eq!(sub(&v, 0.5, 1.0), (vec![0.0, 2.0, 0.0, 4.0], 2));
    }

    #[test]
    fn no_length_and_short() {
        let mut col = Vec::new();
        for v in [&[1.0, 2.0, 1.0, 2.0][..], &[1.0, 2.0][..], &[][..]] {
            let r = ring(v);
            let s = arc_of(&r, &mut col);
            assert!(degenerate(r.k, &s));
            assert_eq!(rows_of(r.k, &s, 0.2, 0.4), r.k.min(1));
        }
    }

    #[test]
    fn nan_gives_nan_ends() {
        let (v, rows) = sub(&[0.0, 0.0, f64::NAN, 1.0, 0.0, 2.0], 0.1, 0.9);
        assert_eq!(rows, 2);
        assert!(v.iter().all(|x| x.is_nan()));
    }

    #[test]
    fn odd_byte_strides() {
        // (rows 17 bytes apart, the first one byte into the buffer: unaligned reads at the exact offsets)
        let pts = [[0.0f64, 0.0], [0.0, 2.0], [0.0, 4.0]];
        let mut buf = vec![0u8; 1 + 17 * 3];
        for (i, p) in pts.iter().enumerate() {
            buf[1 + 17 * i..9 + 17 * i].copy_from_slice(&p[0].to_ne_bytes());
            buf[9 + 17 * i..17 + 17 * i].copy_from_slice(&p[1].to_ne_bytes());
        }
        let r = Ring { data: buf[1..].as_ptr(), k: 3, d: 2, s0: 17, s1: 8 };
        let mut col = Vec::new();
        let s = arc_of(&r, &mut col);
        assert_eq!(cut(&r, &s, 0.25, 0.75, &mut col), (vec![0.0, 1.0, 0.0, 2.0, 0.0, 3.0], 3));
    }
}
