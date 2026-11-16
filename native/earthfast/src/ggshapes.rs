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
                let len = len2.sqrt();
                for r in r0..=r1 as usize {
                    // (stage 3) the row's texels within hw of the segment's line, not its whole box (a long diagonal
                    // visited its bounding rectangle): every point within hw of the segment is within hw of its line,
                    // so this interval (a texel wider each side) holds all of them; the exact test below decides
                    let (mut ca, mut cb) = (c0, c1 as usize);
                    if dy.abs() * 8.0 > len && len > 0.0 {
                        let yc = r as f64 + 0.5;
                        let xc = ax + (yc - ay) * dx / dy;
                        let half = hw * len / dy.abs();
                        let lo = (xc - half - 0.5).floor() - 1.0;
                        let hi = (xc + half - 0.5).ceil() + 1.0;
                        if hi < c0 as f64 || lo > c1 {
                            continue;
                        }
                        ca = ca.max(lo.max(0.0) as usize);
                        cb = cb.min(hi.max(0.0) as usize);
                    }
                    for c in ca..=cb {
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
/// the edges of all its rings (polygon p's rings are ring_off's [poly_off[p], poly_off[p + 1])) in xy (m, 2), an edge
/// left out where skip (points) is set at its first point (a clipped piece's edges along a map cell's border: the
/// clip, not the field). Linear in the edges (the Codex review of 308: a 50,000-vertex forest made the all-pairs vote
/// quadratic): each edge adds its length to the nearest of BINS double-angle directions; the best window of the bins
/// within 10 degrees wins, refined as the length-weighted mean of the edges within 10 degrees of its middle (double
/// angles). Pointing east, or north when it runs north-south; (1, 0) for a polygon without an edge.
#[pyfunction]
pub fn gg_poly_axes<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, ring_off: PyReadonlyArray1<'py, i64>,
                         poly_off: PyReadonlyArray1<'py, i64>, skip: PyReadonlyArray1<'py, u8>)
                         -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (xy, ring_off, poly_off, skip) = (xy.as_array(), ring_off.as_array(), poly_off.as_array(), skip.as_array());
    let np = poly_off.len().saturating_sub(1);
    let nr = ring_off.len().saturating_sub(1);
    let npts = xy.shape()[0];
    if skip.len() != npts || (npts > 0 && xy.shape()[1] < 2) {
        return Err(pyo3::exceptions::PyValueError::new_err("gg_poly_axes: bad shapes"));
    }
    let (xy, ring_off, poly_off, skip) = (xy.to_owned(), ring_off.to_owned(), poly_off.to_owned(), skip.to_owned());
    let out = py.detach(move || {
        let mut out = numpy::ndarray::Array2::<f64>::zeros((np, 2));
        let centres: Vec<(f64, f64)> = (0..BINS).map(|k| {
            let a = 2.0 * std::f64::consts::PI * k as f64 / BINS as f64;
            (libm::cos(a), libm::sin(a))
        }).collect();
        let mut edges: Vec<(f64, f64, f64)> = Vec::new();
        for p in 0..np {
            out[[p, 0]] = 1.0;
            let (r0, r1) = (poly_off[p], poly_off[p + 1]);
            if r0 < 0 || r1 < r0 || r1 as usize > nr {
                continue;
            }
            edges.clear();
            for r in r0 as usize..r1 as usize {
                let (a, b) = (ring_off[r], ring_off[r + 1]);
                if a < 0 || b > npts as i64 || b - a < 2 {
                    continue;
                }
                let (a, b) = (a as usize, b as usize);
                for q in a..b {
                    if skip[q] != 0 {
                        continue;
                    }
                    let q2 = if q + 1 < b { q + 1 } else { a };
                    let (dx, dy) = (xy[[q2, 0]] - xy[[q, 0]], xy[[q2, 1]] - xy[[q, 1]]);
                    let l2 = dx * dx + dy * dy;
                    if l2 > 1e-12 {
                        edges.push((l2.sqrt(), (dx * dx - dy * dy) / l2, 2.0 * dx * dy / l2));
                    }
                }
            }
            if edges.is_empty() {
                continue;
            }
            let mut hist = [0.0f64; BINS];
            for e in &edges {
                let mut best = (f64::NEG_INFINITY, 0usize);
                for (k, c) in centres.iter().enumerate() {
                    let d = e.1 * c.0 + e.2 * c.1;
                    if d > best.0 {
                        best = (d, k);
                    }
                }
                hist[best.1] += e.0;
            }
            let mut best = (f64::NEG_INFINITY, 0usize);
            for k in 0..BINS {
                let mut s = 0.0;
                for o in 0..=2 * WIN {
                    s += hist[(k + BINS + o - WIN) % BINS];
                }
                if s > best.0 {
                    best = (s, k);
                }
            }
            // (the window's own middle: its bins' length-weighted direction; windows tied on one strong bin start
            // left of it, their first bin's centre 10 degrees off)
            let (mut mx, mut my) = (0.0, 0.0);
            for o in 0..=2 * WIN {
                let kk = (best.1 + BINS + o - WIN) % BINS;
                mx += hist[kk] * centres[kk].0;
                my += hist[kk] * centres[kk].1;
            }
            let mn = (mx * mx + my * my).sqrt();
            let c = if mn > 0.0 { (mx / mn, my / mn) } else { centres[best.1] };
            let (mut cx, mut cy) = (0.0, 0.0);
            for f in edges.iter().filter(|f| f.1 * c.0 + f.2 * c.1 >= NEAR) {
                cx += f.0 * f.1;
                cy += f.0 * f.2;
            }
            let n = (cx * cx + cy * cy).sqrt();
            if n <= 0.0 {
                continue;
            }
            let (c2, s2) = (cx / n, cy / n);
            // half angle: (cos t, sin t) with cos 2t = c2, sin 2t = s2, t in (-90, 90] degrees
            let ux = ((1.0 + c2) / 2.0).max(0.0).sqrt();
            let mut uy = ((1.0 - c2) / 2.0).max(0.0).sqrt();
            if s2 < 0.0 {
                uy = -uy;
            }
            if ux < 1e-9 && uy < 0.0 {
                uy = -uy;
            }
            out[[p, 0]] = ux;
            out[[p, 1]] = uy;
        }
        out
    });
    Ok(PyArray2::from_owned_array(py, out))
}

// gg_poly_axes: cos(2 x 10 deg), the double-angle dot product of directions within 10 degrees; 72 bins of 5 degrees of
// double angle (2.5 of direction), 4 either side within 10 degrees of direction
const NEAR: f64 = 0.939_692_620_785_908_4;
const BINS: usize = 72;
const WIN: usize = 4;

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(gg_fill_lines, m)?)?;
    m.add_function(wrap_pyfunction!(gg_poly_axes, m)?)?;
    Ok(())
}
