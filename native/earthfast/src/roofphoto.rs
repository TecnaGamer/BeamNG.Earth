//! Roof colours from the aerial photo (buildings.RoofPhoto): the median colour of each footprint's roof, read from the
//! cached high-resolution orthophoto the ground photo already fetched (streetfront.cached_photo's sources).

use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3};
use pyo3::prelude::*;

/// Whether a pixel is vegetation (a tree over the roof): the same test as streetfront.PhotoPaving's lawns.
fn green(r: i32, g: i32, b: i32) -> bool {
    g >= 38 && g - r >= 6 && 2 * g - r - b >= 20
}

/// Point-in-polygon (even-odd) of (x, y) in ring pts[a..b] (closed implicitly).
fn inside(px: f64, py: f64, xy: &numpy::ndarray::ArrayView2<f64>, a: usize, b: usize) -> bool {
    let mut c = false;
    let n = b - a;
    if n < 3 {
        return false;
    }
    let mut j = b - 1;
    for i in a..b {
        let (xi, yi, xj, yj) = (xy[[i, 0]], xy[[i, 1]], xy[[j, 0]], xy[[j, 1]]);
        if (yi > py) != (yj > py) && px < (xj - xi) * (py - yi) / (yj - yi) + xi {
            c = !c;
        }
        j = i;
    }
    c
}

/// Distance from (x, y) to the ring pts[a..b]'s edges.
fn edge_dist(px: f64, py: f64, xy: &numpy::ndarray::ArrayView2<f64>, a: usize, b: usize) -> f64 {
    let mut best = f64::INFINITY;
    let mut j = b - 1;
    for i in a..b {
        let (ax, ay, bx, by) = (xy[[j, 0]], xy[[j, 1]], xy[[i, 0]], xy[[i, 1]]);
        let (dx, dy) = (bx - ax, by - ay);
        let l2 = dx * dx + dy * dy;
        let t = if l2 > 1e-12 { (((px - ax) * dx + (py - ay) * dy) / l2).clamp(0.0, 1.0) } else { 0.0 };
        let (qx, qy) = (ax + t * dx - px, ay + t * dy - py);
        best = best.min((qx * qx + qy * qy).sqrt());
        j = i;
    }
    best
}

fn median(v: &mut [u8]) -> f64 {
    v.sort_unstable();
    let n = v.len();
    if n % 2 == 1 { v[n / 2] as f64 } else { (v[n / 2 - 1] as f64 + v[n / 2] as f64) / 2.0 }
}

/// (n, 3) sRGB colours in [0, 1] of the roofs of footprints xy[off[k]..off[k + 1]] (metres, the photo's frame) over
/// the photo rgb (rows north to south) covering the square [x0, x0 + size] x [y0, y0 + size]: per channel, the median
/// of the pixels inside the footprint and at least `inset` m from its outline (its walls and eaves' edges, the
/// neighbours' roofs where the outline is a little off), leaving out vegetation (a tree over the roof) and deep
/// shadow. NaN where fewer than min_px pixels remain, or where vegetation covers more than half of the roof (its
/// colour would be the tree's).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn roof_photo_colours<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, x0: f64, y0: f64, size: f64,
                               xy: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>, inset: f64,
                               min_px: i64, shadow_sum: i64) -> Bound<'py, PyArray2<f64>> {
    let rgb = rgb.as_array();
    let xy = xy.as_array();
    let off = off.as_array();
    let (rows, cols) = (rgb.shape()[0], rgb.shape()[1]);
    let n = off.len().saturating_sub(1);
    let mut out = numpy::ndarray::Array2::<f64>::from_elem((n, 3), f64::NAN);
    if rows == 0 || cols == 0 || size <= 0.0 {
        return out.into_pyarray(py);
    }
    let (px_w, px_h) = (size / cols as f64, size / rows as f64);
    let (mut rs, mut gs, mut bs) = (Vec::new(), Vec::new(), Vec::new());
    for k in 0..n {
        let (a, b) = (off[k] as usize, off[k + 1] as usize);
        if b <= a + 2 || b > xy.shape()[0] {
            continue;
        }
        let (mut lx, mut ly, mut hx, mut hy) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for i in a..b {
            lx = lx.min(xy[[i, 0]]); hx = hx.max(xy[[i, 0]]);
            ly = ly.min(xy[[i, 1]]); hy = hy.max(xy[[i, 1]]);
        }
        let c0 = (((lx - x0) / px_w).floor().max(0.0)) as usize;
        let c1 = (((hx - x0) / px_w).ceil().min(cols as f64)) as usize;
        let r0 = (((y0 + size - hy) / px_h).floor().max(0.0)) as usize;
        let r1 = (((y0 + size - ly) / px_h).ceil().min(rows as f64)) as usize;
        if c1 <= c0 || r1 <= r0 {
            continue;
        }
        rs.clear(); gs.clear(); bs.clear();
        let mut veg = 0usize;
        let mut seen = 0usize;
        for r in r0..r1 {
            let wy = y0 + size - (r as f64 + 0.5) * px_h;
            for c in c0..c1 {
                let wx = x0 + (c as f64 + 0.5) * px_w;
                if !inside(wx, wy, &xy, a, b) || edge_dist(wx, wy, &xy, a, b) < inset {
                    continue;
                }
                let (pr, pg, pb) = (rgb[[r, c, 0]], rgb[[r, c, 1]], rgb[[r, c, 2]]);
                seen += 1;
                if green(pr as i32, pg as i32, pb as i32) {
                    veg += 1;
                    continue;
                }
                if (pr as i64 + pg as i64 + pb as i64) < shadow_sum {
                    continue;
                }
                rs.push(pr); gs.push(pg); bs.push(pb);
            }
        }
        if (rs.len() as i64) < min_px || 2 * veg > seen {
            continue;
        }
        out[[k, 0]] = median(&mut rs) / 255.0;
        out[[k, 1]] = median(&mut gs) / 255.0;
        out[[k, 2]] = median(&mut bs) / 255.0;
    }
    out.into_pyarray(py)
}
