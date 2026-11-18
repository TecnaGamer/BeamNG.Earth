//! Mapped OpenStreetMap street lamps (highway=street_lamp, PR #981 by wyo-backroads): which way each lamp's arm faces,
//! and how many mapped points (lamps, utility poles) stand along each road, so a road with its own mapped ones guesses
//! none (furniture.place). Both were per-point numpy loops in Python over every road line near the tile.
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// The nearest point of segment (ax, ay)-(bx, by) to (px, py): (squared distance, dx, dy) with (dx, dy) the vector
/// from p to it; a zero-length segment is infinitely far (no direction).
#[inline]
fn seg_near(px: f64, py: f64, ax: f64, ay: f64, bx: f64, by: f64) -> (f64, f64, f64) {
    let (ux, uy) = (bx - ax, by - ay);
    let l2 = ux * ux + uy * uy;
    if l2 < 1e-12 {
        return (f64::INFINITY, 0.0, 0.0);
    }
    let t = (((px - ax) * ux + (py - ay) * uy) / l2).clamp(0.0, 1.0);
    let (dx, dy) = (ax + t * ux - px, ay + t * uy - py);
    (dx * dx + dy * dy, dx, dy)
}

/// The yaw (degrees) of each mapped lamp at pts (n, 2) that turns its arm toward the nearest road within max_m, NaN
/// where none is. Roads: polylines line_pts (m, 2) with offsets off (k + 1), in a stable order (ties go to the earlier
/// line, then the earlier segment), level (k,) their mapped layer (NaN: none). layer (n,): the lamp's layer (NaN: none);
/// a lamp and a road whose layers are both known and differ do not match. kind (n,): 0 an arm along the model's +x
/// (the stock post, West Coast's double arm), 1 along its -y (the Driver Experience straight mast), 2 a double arm
/// (across the road: the segment's normal, modulo 180). A lamp on (within 0.2 m of) a centreline also takes the normal.
#[pyfunction]
pub fn lamp_road_yaw<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, layer: PyReadonlyArray1<'py, f64>,
                          kind: PyReadonlyArray1<'py, i64>, line_pts: PyReadonlyArray2<'py, f64>,
                          off: PyReadonlyArray1<'py, i64>, level: PyReadonlyArray1<'py, f64>, max_m: f64)
                          -> Bound<'py, PyArray1<f64>> {
    let (pts, layer, kind, lp, off, level) = (pts.as_array(), layer.as_array(), kind.as_array(), line_pts.as_array(),
                                              off.as_array(), level.as_array());
    let n = pts.nrows();
    let nl = off.len().saturating_sub(1);
    // each line's box, to skip the far ones
    let mut boxes = Vec::with_capacity(nl);
    for i in 0..nl {
        let (a, b) = (off[i] as usize, off[i + 1] as usize);
        let mut bx = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        for j in a..b {
            bx[0] = bx[0].min(lp[[j, 0]]);
            bx[1] = bx[1].min(lp[[j, 1]]);
            bx[2] = bx[2].max(lp[[j, 0]]);
            bx[3] = bx[3].max(lp[[j, 1]]);
        }
        boxes.push(bx);
    }
    let max2 = max_m * max_m;
    let mut out = vec![f64::NAN; n];
    for p in 0..n {
        let (px, py) = (pts[[p, 0]], pts[[p, 1]]);
        if !px.is_finite() || !py.is_finite() {
            continue;
        }
        let ll = layer[p];
        // best: (squared distance, dx, dy, tangent x, tangent y)
        let mut best: Option<(f64, f64, f64, f64, f64)> = None;
        for i in 0..nl {
            let bx = boxes[i];
            if px < bx[0] - max_m || px > bx[2] + max_m || py < bx[1] - max_m || py > bx[3] + max_m {
                continue;
            }
            if ll.is_finite() && level[i].is_finite() && ll != level[i] {
                continue;
            }
            let (a, b) = (off[i] as usize, off[i + 1] as usize);
            if b < a + 2 {
                continue;
            }
            // this line's nearest segment (the first of equals)
            let mut here: Option<(f64, f64, f64, f64, f64)> = None;
            for j in a..b - 1 {
                let (ax, ay, bx2, by2) = (lp[[j, 0]], lp[[j, 1]], lp[[j + 1, 0]], lp[[j + 1, 1]]);
                let (d2, dx, dy) = seg_near(px, py, ax, ay, bx2, by2);
                if here.map_or(true, |h| d2 < h.0) {
                    here = Some((d2, dx, dy, bx2 - ax, by2 - ay));
                }
            }
            if let Some(h) = here {
                if h.0 <= max2 && best.map_or(true, |b| h.0 < b.0) {
                    best = Some(h);
                }
            }
        }
        let Some((d2, mut dx, mut dy, tx, ty)) = best else { continue };
        let k = kind[p];
        if k == 2 || d2 < 0.04 {
            // on a centreline or a median: no inward side, the arms go across the road (a stable one of the two)
            dx = ty;
            dy = -tx;
            if dx < 0.0 || (dx == 0.0 && dy < 0.0) {
                dx = -dx;
                dy = -dy;
            }
        }
        let target = dy.datan2(dx).to_degrees();
        let yaw = (target + if k == 1 { 90.0 } else { 0.0 }).rem_euclid(360.0);
        out[p] = if k == 2 { yaw.rem_euclid(180.0) } else { yaw };
    }
    out.into_pyarray(py)
}

/// For each line (polyline line_pts (m, 2), offsets off (k + 1)), how many of the points pts (n, 2) stand within
/// reach[i] m of it (furniture.place: a road with its own mapped poles or lamps along it guesses none).
#[pyfunction]
pub fn points_along<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, line_pts: PyReadonlyArray2<'py, f64>,
                         off: PyReadonlyArray1<'py, i64>, reach: PyReadonlyArray1<'py, f64>)
                         -> Bound<'py, PyArray1<i64>> {
    let (pts, lp, off, reach) = (pts.as_array(), line_pts.as_array(), off.as_array(), reach.as_array());
    let nl = off.len().saturating_sub(1);
    let mut out = vec![0i64; nl];
    for i in 0..nl {
        let (a, b) = (off[i] as usize, off[i + 1] as usize);
        if b < a + 2 {
            continue;
        }
        let r = reach[i];
        let r2 = r * r;
        let mut bx = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        for j in a..b {
            bx[0] = bx[0].min(lp[[j, 0]]);
            bx[1] = bx[1].min(lp[[j, 1]]);
            bx[2] = bx[2].max(lp[[j, 0]]);
            bx[3] = bx[3].max(lp[[j, 1]]);
        }
        let mut count = 0i64;
        for p in 0..pts.nrows() {
            let (px, py) = (pts[[p, 0]], pts[[p, 1]]);
            if !(px >= bx[0] - r && px <= bx[2] + r && py >= bx[1] - r && py <= bx[3] + r) {
                continue;
            }
            let mut near = false;
            for j in a..b - 1 {
                let (ax, ay, bx2, by2) = (lp[[j, 0]], lp[[j, 1]], lp[[j + 1, 0]], lp[[j + 1, 1]]);
                // (a zero-length segment: the distance to its point, as furniture._seg_dist_min)
                let (ux, uy) = (bx2 - ax, by2 - ay);
                let l2 = (ux * ux + uy * uy).max(1e-12);
                let t = (((px - ax) * ux + (py - ay) * uy) / l2).clamp(0.0, 1.0);
                let (dx, dy) = (ax + t * ux - px, ay + t * uy - py);
                if dx * dx + dy * dy <= r2 {
                    near = true;
                    break;
                }
            }
            if near {
                count += 1;
            }
        }
        out[i] = count;
    }
    out.into_pyarray(py)
}
