//! shelters.py's carriageway tests of a bus shelter's pose (_intrusion, _clear_of_roads): every point of the body or
//! every post against every road of the tile was a numpy round each (~0.4 CPU-s of a 16-tile I-83 Baltimore probe,
//! 2026-11-12). The roads come packed once (shelters._packed); the numbers are the numpy version's (operation order,
//! first minimum, NaN where numpy has one, Python's max).

use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

type Pt = [f64; 2];

fn nmax(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else { a.max(b) }
}

fn nmin(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else { a.min(b) }
}

/// Python's max(a, b): a unless b > a.
fn pymax(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

struct Roads<'a> {
    xy: Vec<Pt>,
    off: numpy::ndarray::ArrayView1<'a, i64>,
    half: numpy::ndarray::ArrayView1<'a, f64>,
}

/// The foot of p on each piece of line, in order: (foot, distance) (numpy's t = clip(((p - a) * ab).sum(1) / ab2, 0, 1),
/// ab2 = maximum((ab * ab).sum(1), 1e-9), dist = hypot(*(p - (a + t * ab)).T)).
fn feet(p: Pt, line: &[Pt]) -> impl Iterator<Item = (Pt, f64)> + '_ {
    line.windows(2).map(move |w| {
        let (a, b) = (w[0], w[1]);
        let ab = [b[0] - a[0], b[1] - a[1]];
        let ab2 = nmax(ab[0] * ab[0] + ab[1] * ab[1], 1e-9);
        let t = nmin(nmax(((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / ab2, 0.0), 1.0);
        let f = [a[0] + t * ab[0], a[1] + t * ab[1]];
        (f, (p[0] - f[0]).hypot(p[1] - f[1]))
    })
}

fn unpack<'a>(xy: &'a PyReadonlyArray2<'a, f64>, off: &'a PyReadonlyArray1<'a, i64>, half: &'a PyReadonlyArray1<'a, f64>)
    -> PyResult<Roads<'a>> {
    let (x, off, half) = (xy.as_array(), off.as_array(), half.as_array());
    let bad = |m: &str| pyo3::exceptions::PyValueError::new_err(format!("shelter roads: {m}"));
    if x.ncols() < 2 || off.len() != half.len() + 1 {
        return Err(bad("xy (N, 2), off (R + 1), half (R)"));
    }
    for k in 0..half.len() {
        if off[k] < 0 || off[k + 1] < off[k] || off[k + 1] as usize > x.nrows() {
            return Err(bad("bad off"));
        }
    }
    Ok(Roads { xy: (0..x.nrows()).map(|k| [x[[k, 0]], x[[k, 1]]]).collect(), off, half })
}

/// shelters._intrusion: m the deepest of the points pts (k, 2) is inside any road's carriageway (its half width less
/// its distance to the centre line), 0 when none is. Roads: centre lines xy one after another (off (R + 1)), half (R).
#[pyfunction]
pub fn shelter_intrusion<'py>(
    pts: PyReadonlyArray2<'py, f64>,
    xy: PyReadonlyArray2<'py, f64>,
    off: PyReadonlyArray1<'py, i64>,
    half: PyReadonlyArray1<'py, f64>,
) -> PyResult<f64> {
    let r = unpack(&xy, &off, &half)?;
    let p = pts.as_array();
    let pts: Vec<Pt> = (0..p.nrows()).map(|k| [p[[k, 0]], p[[k, 1]]]).collect();
    let mut worst = 0.0f64;
    for k in 0..r.half.len() {
        let line = &r.xy[r.off[k] as usize..r.off[k + 1] as usize];
        if line.len() < 2 {
            continue;
        }
        for &q in &pts {
            let dmin = feet(q, line).fold(f64::INFINITY, |m, (_, d)| nmin(m, d));     // (np.min: a NaN wins)
            worst = pymax(worst, r.half[k] - dmin);
        }
    }
    Ok(worst)
}

/// One pass of shelters._clear_of_roads: of every road and every post (posts (k, 2)), the post deepest inside the
/// road's half width + pad (by more than 1 mm; the first such), as (short, unit vector from its foot on the centre
/// line to the shelter's centre), or None.
#[pyfunction]
pub fn shelter_worst_post<'py>(
    posts: PyReadonlyArray2<'py, f64>,
    cx: f64,
    cy: f64,
    xy: PyReadonlyArray2<'py, f64>,
    off: PyReadonlyArray1<'py, i64>,
    half: PyReadonlyArray1<'py, f64>,
    pad: f64,
) -> PyResult<Option<(f64, f64, f64)>> {
    let r = unpack(&xy, &off, &half)?;
    let p = posts.as_array();
    let posts: Vec<Pt> = (0..p.nrows()).map(|k| [p[[k, 0]], p[[k, 1]]]).collect();
    let mut worst: Option<(f64, f64, f64)> = None;
    for k in 0..r.half.len() {
        let line = &r.xy[r.off[k] as usize..r.off[k + 1] as usize];
        if line.len() < 2 {
            continue;
        }
        for &q in &posts {
            // np.argmin: the first of the smallest, a NaN at once
            let mut best: Option<(Pt, f64)> = None;
            for (f, d) in feet(q, line) {
                match best {
                    None => best = Some((f, d)),
                    Some((_, bd)) if bd.is_nan() => {}
                    Some((_, bd)) => {
                        if d < bd || d.is_nan() {
                            best = Some((f, d));
                        }
                    }
                }
            }
            let (foot, d) = best.unwrap();
            let short = r.half[k] + pad - d;
            if short > 1e-3 && worst.map_or(true, |w| short > w.0) {
                let n = [cx - foot[0], cy - foot[1]];
                let h = n[0].hypot(n[1]);
                let den = if 1e-6 > h { 1e-6 } else { h };
                worst = Some((short, n[0] / den, n[1] / den));
            }
        }
    }
    Ok(worst)
}

/// roadcorners.tile's distance from a corner's node (x, y) to each road's points (np.min(np.hypot(*(p2[:, :2] - X).T)),
/// NaN if any is; inf for a road without points), the roads packed as above (xy, off).
#[pyfunction]
pub fn lines_point_min_dist<'py>(
    py: Python<'py>,
    xy: PyReadonlyArray2<'py, f64>,
    off: PyReadonlyArray1<'py, i64>,
    x: f64,
    y: f64,
) -> PyResult<Bound<'py, numpy::PyArray1<f64>>> {
    use numpy::IntoPyArray;
    let (a, off) = (xy.as_array(), off.as_array());
    let n = off.len().saturating_sub(1);
    if a.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("lines_point_min_dist: xy (N, 2), off (R + 1)"));
    }
    let mut out = Vec::with_capacity(n);
    for k in 0..n {
        if off[k] < 0 || off[k + 1] < off[k] || off[k + 1] as usize > a.nrows() {
            return Err(pyo3::exceptions::PyValueError::new_err("lines_point_min_dist: bad off"));
        }
        out.push((off[k] as usize..off[k + 1] as usize)
            .map(|i| (a[[i, 0]] - x).hypot(a[[i, 1]] - y))
            .fold(f64::INFINITY, nmin));
    }
    Ok(out.into_pyarray(py))
}
