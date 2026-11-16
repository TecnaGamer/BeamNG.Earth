//! The generated ground's map shapes (genground.rs, server/genground.py): lines buffered to their width into a texel
//! raster (hedges, tree rows, streams and ditches, paths, tracks, driveways, railway ballast), and each polygon's row
//! direction (a field's, an orchard's, a vineyard's rows run along its dominant edge).
//!
//! Deterministic: f64 arithmetic and sqrt only, sums in the polygons' own point order.

use numpy::{PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyReadwriteArray2};
use pyo3::prelude::*;

/// Lines buffered to half-widths filled into out (h, w) u32 at the texel centres (col + 0.5, row + 0.5): xy (m, 2)
/// texel coordinates (col, row) of every line's points, line_off (lines + 1) their ranges, half (lines) each line's
/// half-width in texels, values (lines) what each writes; later lines over earlier ones. Returns the texels written.
#[pyfunction]
pub fn gg_fill_lines(py: Python<'_>, mut out: PyReadwriteArray2<'_, u32>, xy: PyReadonlyArray2<'_, f64>,
                     line_off: PyReadonlyArray1<'_, i64>, half: PyReadonlyArray1<'_, f64>,
                     values: PyReadonlyArray1<'_, u32>) -> PyResult<usize> {
    let (xy, line_off, half, values) = (xy.as_array(), line_off.as_array(), half.as_array(), values.as_array());
    let nl = values.len();
    if line_off.len() != nl + 1 || half.len() != nl || (xy.shape()[0] > 0 && xy.shape()[1] < 2) {
        return Err(pyo3::exceptions::PyValueError::new_err("gg_fill_lines: bad shapes"));
    }
    let npts = xy.shape()[0] as i64;
    for k in 0..nl {
        if line_off[k] < 0 || line_off[k + 1] < line_off[k] || line_off[k + 1] > npts || !(half[k] >= 0.0) {
            return Err(pyo3::exceptions::PyValueError::new_err("gg_fill_lines: bad offsets"));
        }
    }
    let mut arr = out.as_array_mut();
    let (h, w) = (arr.shape()[0], arr.shape()[1]);
    let (xy, line_off, half, values) = (xy.to_owned(), line_off.to_owned(), half.to_owned(), values.to_owned());
    let mut grid = arr.to_owned();
    let written = py.detach(|| {
        let mut written = 0usize;
        for l in 0..nl {
            let (a, b) = (line_off[l] as usize, line_off[l + 1] as usize);
            let hw = half[l];
            for q in a..b.saturating_sub(1) {
                let (ax, ay, bx, by) = (xy[[q, 0]], xy[[q, 1]], xy[[q + 1, 0]], xy[[q + 1, 1]]);
                let c0 = ((ax.min(bx) - hw - 0.5).floor().max(0.0)) as usize;
                let c1 = (ax.max(bx) + hw - 0.5).ceil().min(w as f64 - 1.0);
                let r0 = ((ay.min(by) - hw - 0.5).floor().max(0.0)) as usize;
                let r1 = (ay.max(by) + hw - 0.5).ceil().min(h as f64 - 1.0);
                if c1 < 0.0 || r1 < 0.0 {
                    continue;
                }
                let (dx, dy) = (bx - ax, by - ay);
                let len2 = dx * dx + dy * dy;
                for r in r0..=r1 as usize {
                    for c in c0..=c1 as usize {
                        let (px, py_) = (c as f64 + 0.5, r as f64 + 0.5);
                        let t = if len2 > 0.0 { (((px - ax) * dx + (py_ - ay) * dy) / len2).clamp(0.0, 1.0) } else { 0.0 };
                        let (ex, ey) = (ax + t * dx - px, ay + t * dy - py_);
                        if ex * ex + ey * ey <= hw * hw {
                            if grid[[r, c]] != values[l] {
                                written += 1;
                            }
                            grid[[r, c]] = values[l];
                        }
                    }
                }
            }
        }
        written
    });
    arr.assign(&grid);
    Ok(written)
}

/// Each polygon's row direction (polygons, 2): the unit vector (east, north) along its dominant edge direction, from
/// its outer ring (the first ring of poly_off's range) in xy (m, 2) metres: every edge votes for the directions within
/// 10 degrees of its own with its length; the best-voted edge's direction, refined as the length-weighted mean of the
/// edges near it (double angles: no trigonometry). Pointing east, or north when it runs north-south.
#[pyfunction]
pub fn gg_poly_axes<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, ring_off: PyReadonlyArray1<'py, i64>,
                         poly_off: PyReadonlyArray1<'py, i64>) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (xy, ring_off, poly_off) = (xy.as_array(), ring_off.as_array(), poly_off.as_array());
    let np = poly_off.len().saturating_sub(1);
    let nr = ring_off.len().saturating_sub(1);
    let mut out = numpy::ndarray::Array2::<f64>::zeros((np, 2));
    // cos(2 * 10 deg): the double-angle dot product of directions within 10 degrees
    const NEAR: f64 = 0.939_692_620_785_908_4;
    for p in 0..np {
        out[[p, 0]] = 1.0;
        let r = poly_off[p];
        if r < 0 || r as usize >= nr {
            continue;
        }
        let (a, b) = (ring_off[r as usize], ring_off[r as usize + 1]);
        if a < 0 || b > xy.shape()[0] as i64 || b - a < 2 {
            continue;
        }
        let (a, b) = (a as usize, b as usize);
        // edges as (length, double-angle unit vector)
        let mut edges: Vec<(f64, f64, f64)> = Vec::with_capacity(b - a);
        for q in a..b {
            let q2 = if q + 1 < b { q + 1 } else { a };
            let (dx, dy) = (xy[[q2, 0]] - xy[[q, 0]], xy[[q2, 1]] - xy[[q, 1]]);
            let l2 = dx * dx + dy * dy;
            if l2 > 1e-12 {
                edges.push((l2.sqrt(), (dx * dx - dy * dy) / l2, 2.0 * dx * dy / l2));
            }
        }
        if edges.is_empty() {
            continue;
        }
        let mut best = (f64::NEG_INFINITY, 0usize);
        for (i, e) in edges.iter().enumerate() {
            let score: f64 = edges.iter().filter(|f| f.1 * e.1 + f.2 * e.2 >= NEAR).map(|f| f.0).sum();
            if score > best.0 {
                best = (score, i);
            }
        }
        let e = edges[best.1];
        let (mut cx, mut cy) = (0.0, 0.0);
        for f in edges.iter().filter(|f| f.1 * e.1 + f.2 * e.2 >= NEAR) {
            cx += f.0 * f.1;
            cy += f.0 * f.2;
        }
        let n = (cx * cx + cy * cy).sqrt();
        if n <= 0.0 {
            continue;
        }
        let (c2, s2) = (cx / n, cy / n);
        // half angle: (cos t, sin t) with cos 2t = c2, sin 2t = s2, t in [0, pi)
        let mut ux = ((1.0 + c2) / 2.0).max(0.0).sqrt();
        let mut uy = ((1.0 - c2) / 2.0).max(0.0).sqrt();
        if s2 < 0.0 {
            uy = -uy;
        }
        if ux < 1e-9 && uy < 0.0 {
            uy = -uy;
        }
        if ux < 0.0 {
            ux = -ux;
            uy = -uy;
        }
        out[[p, 0]] = ux;
        out[[p, 1]] = uy;
    }
    let _ = py;
    Ok(PyArray2::from_owned_array(py, out))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(gg_fill_lines, m)?)?;
    m.add_function(wrap_pyfunction!(gg_poly_axes, m)?)?;
    Ok(())
}
