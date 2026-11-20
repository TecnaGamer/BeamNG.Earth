//! France's measured buildings (server/bdtopo.py): IGN BD TOPO `batiment` records joined to the map's footprints, and
//! the records that match no map footprint appended as buildings of their own.
//!
//! - `bdtopo_points`: one point inside each BD TOPO outline (its area centroid, or where that falls outside an L / U
//!   shaped outline the middle of the widest span across it at the centroid's latitude) and the outline's area in m2.
//!   Run once per downloaded map cell (~5 400 outlines in a central Paris cell), off the tiles' path.
//! - `bdtopo_match`: for each map footprint (its outer ring), the record whose point stands inside it, the largest
//!   record when several do (one OSM outline over two cadastre buildings); -1 none. Run once per decoded buildings
//!   layer of a French cell, on the tiles' path: a grid of the records (~40 m bins), then an even-odd test against the
//!   few records under each footprint's box.
//! - `bdtopo_unmatched`: the BD TOPO records in the cell's bounds that match no map footprint, validated and sorted by
//!   their stable `~ign:<id>` key, with their closed outer rings packed for `mapgeom.Features`.
//! All pure functions of their inputs.
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use numpy::ndarray::Array2;
use pyo3::prelude::*;
use std::collections::HashMap;

const M_PER_DEG: f64 = 111_320.0;
const BIN_DEG: f64 = 0.0005;

/// Even-odd crossings of a ray toward +x (an edge counts when lo <= y < hi), over the ring's vertices (closed or not).
fn inside(xs: &[f64], ys: &[f64], x: f64, y: f64) -> bool {
    let n = xs.len();
    if n < 3 {
        return false;
    }
    let mut c = false;
    let mut j = n - 1;
    for i in 0..n {
        let (xi, yi, xj, yj) = (xs[i], ys[i], xs[j], ys[j]);
        if (yi > y) != (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi {
            c = !c;
        }
        j = i;
    }
    c
}

/// (point inside, area m2) of one ring in lon / lat degrees.
fn interior(xs: &[f64], ys: &[f64]) -> (f64, f64, f64) {
    let n = xs.len();
    if n == 0 {
        return (f64::NAN, f64::NAN, 0.0);
    }
    let (x0, y0) = (xs[0], ys[0]);
    let kx = M_PER_DEG * y0.to_radians().dcos().max(0.01);
    // the area centroid in metres round the first vertex (shoelace)
    let (mut a2, mut cx, mut cy) = (0.0f64, 0.0f64, 0.0f64);
    for i in 0..n {
        let j = (i + 1) % n;
        let (ax, ay) = ((xs[i] - x0) * kx, (ys[i] - y0) * M_PER_DEG);
        let (bx, by) = ((xs[j] - x0) * kx, (ys[j] - y0) * M_PER_DEG);
        let cr = ax * by - bx * ay;
        a2 += cr;
        cx += (ax + bx) * cr;
        cy += (ay + by) * cr;
    }
    let area = (a2 * 0.5).abs();
    let (mut px, mut py) = if a2.abs() > 1e-9 {
        (x0 + cx / (3.0 * a2) / kx, y0 + cy / (3.0 * a2) / M_PER_DEG)
    } else {
        let m = n as f64;
        (xs.iter().sum::<f64>() / m, ys.iter().sum::<f64>() / m)
    };
    if !inside(xs, ys, px, py) {
        // the widest span across the ring at the centroid's latitude (nudged off a vertex's exact latitude)
        let y = py + 1e-9;
        let mut cuts: Vec<f64> = Vec::new();
        let mut j = n - 1;
        for i in 0..n {
            let (xi, yi, xj, yj) = (xs[i], ys[i], xs[j], ys[j]);
            if (yi > y) != (yj > y) {
                cuts.push((xj - xi) * (y - yi) / (yj - yi) + xi);
            }
            j = i;
        }
        cuts.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let mut best = -1.0;
        let mut k = 0;
        while k + 1 < cuts.len() {
            let w = cuts[k + 1] - cuts[k];
            if w > best {
                best = w;
                px = 0.5 * (cuts[k] + cuts[k + 1]);
                py = y;
            }
            k += 2;
        }
    }
    (px, py, area)
}

/// (points (r, 2) lon / lat, areas (r,) m2) of the rings coord_off (r + 1,) delimits in lonlat (n, 2).
#[pyfunction]
pub fn bdtopo_points<'py>(py: Python<'py>, coord_off: PyReadonlyArray1<'py, i64>, lonlat: PyReadonlyArray2<'py, f64>)
                          -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<f64>>) {
    let off = coord_off.as_slice().unwrap_or(&[]).to_vec();
    let ll = lonlat.as_array();
    let r = off.len().saturating_sub(1);
    let mut pts = Array2::<f64>::from_elem((r, 2), f64::NAN);
    let mut areas = vec![0.0f64; r];
    for k in 0..r {
        let (a, b) = (off[k].max(0) as usize, (off[k + 1].max(0) as usize).min(ll.nrows()));
        if b <= a + 2 {
            continue;
        }
        let xs: Vec<f64> = (a..b).map(|i| ll[[i, 0]]).collect();
        let ys: Vec<f64> = (a..b).map(|i| ll[[i, 1]]).collect();
        let (px, py_, area) = interior(&xs, &ys);
        pts[[k, 0]] = px;
        pts[[k, 1]] = py_;
        areas[k] = area;
    }
    (pts.into_pyarray(py), areas.into_pyarray(py))
}

/// For each footprint ring lonlat[start[i]:end[i]], the index of the record (rec_lon, rec_lat) inside it with the
/// largest rec_area; -1 none.
#[pyfunction]
pub fn bdtopo_match<'py>(py: Python<'py>, start: PyReadonlyArray1<'py, i64>, end: PyReadonlyArray1<'py, i64>,
                         lonlat: PyReadonlyArray2<'py, f64>, rec_lon: PyReadonlyArray1<'py, f64>,
                         rec_lat: PyReadonlyArray1<'py, f64>, rec_area: PyReadonlyArray1<'py, f64>)
                         -> Bound<'py, PyArray1<i32>> {
    let (s, e) = (start.as_slice().unwrap_or(&[]), end.as_slice().unwrap_or(&[]));
    let ll = lonlat.as_array();
    let (rx, ry, ra) = (rec_lon.as_array(), rec_lat.as_array(), rec_area.as_array());
    let nr = rx.len().min(ry.len()).min(ra.len());
    let mut grid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    for k in 0..nr {
        if rx[k].is_finite() && ry[k].is_finite() {
            grid.entry(((rx[k] / BIN_DEG).floor() as i64, (ry[k] / BIN_DEG).floor() as i64)).or_default().push(k);
        }
    }
    let n = s.len().min(e.len());
    let mut out = vec![-1i32; n];
    if nr == 0 {
        return out.into_pyarray(py);
    }
    let mut xs: Vec<f64> = Vec::new();
    let mut ys: Vec<f64> = Vec::new();
    for i in 0..n {
        let (a, b) = (s[i].max(0) as usize, (e[i].max(0) as usize).min(ll.nrows()));
        if b <= a + 2 {
            continue;
        }
        xs.clear();
        ys.clear();
        let (mut x0, mut x1, mut y0, mut y1) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
        for p in a..b {
            let (x, y) = (ll[[p, 0]], ll[[p, 1]]);
            xs.push(x);
            ys.push(y);
            x0 = x0.min(x); x1 = x1.max(x); y0 = y0.min(y); y1 = y1.max(y);
        }
        let finite = x0.is_finite() && y0.is_finite() && x1.is_finite() && y1.is_finite();
        if !finite || (x1 - x0) > 0.05 || (y1 - y0) > 0.05 {
            continue;       // (a broken ring, or one kilometres across: not a building to match)
        }
        let mut best = -1i64;
        let mut best_a = f64::NEG_INFINITY;
        for gx in (x0 / BIN_DEG).floor() as i64..=(x1 / BIN_DEG).floor() as i64 {
            for gy in (y0 / BIN_DEG).floor() as i64..=(y1 / BIN_DEG).floor() as i64 {
                if let Some(v) = grid.get(&(gx, gy)) {
                    for &k in v {
                        let (px, py_) = (rx[k], ry[k]);
                        if px < x0 || px > x1 || py_ < y0 || py_ > y1 {
                            continue;
                        }
                        // the larger record wins; the lower index on a tie (deterministic)
                        if (ra[k] > best_a || (ra[k] == best_a && (k as i64) < best)) && inside(&xs, &ys, px, py_) {
                            best = k as i64;
                            best_a = ra[k];
                        }
                    }
                }
            }
        }
        out[i] = best as i32;
    }
    out.into_pyarray(py)
}

struct MapFootprint {
    x0: f64,
    y0: f64,
    x1: f64,
    y1: f64,
    cx: f64,
    cy: f64,
    xs: Vec<f64>,
    ys: Vec<f64>,
}

#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub(crate) fn bdtopo_unmatched_core(
    s: &[i64],
    e: &[i64],
    ll: numpy::ndarray::ArrayView2<f64>,
    rx: &[f64],
    ry: &[f64],
    ra: &[f64],
    roff: &[i64],
    rll: numpy::ndarray::ArrayView2<f64>,
    rec_ids: &[String],
    bounds: (f64, f64, f64, f64),
    min_area_m2: f64,
) -> (Vec<i64>, Vec<String>, Vec<i64>, Vec<f64>) {
    let nr = rx
        .len()
        .min(ry.len())
        .min(ra.len())
        .min(roff.len().saturating_sub(1))
        .min(rec_ids.len());
    if nr == 0 {
        return (Vec::new(), Vec::new(), vec![0], Vec::new());
    }
    let mut rec_grid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    for k in 0..nr {
        if rx[k].is_finite() && ry[k].is_finite() {
            rec_grid
                .entry(((rx[k] / BIN_DEG).floor() as i64, (ry[k] / BIN_DEG).floor() as i64))
                .or_default()
                .push(k);
        }
    }
    let mut matched = vec![false; nr];
    let n = s.len().min(e.len());
    let mut fps: Vec<MapFootprint> = Vec::new();
    let mut map_grid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    for i in 0..n {
        let (a, b) = (s[i].max(0) as usize, (e[i].max(0) as usize).min(ll.nrows()));
        if b <= a + 2 {
            continue;
        }
        let mut xs = Vec::with_capacity(b - a);
        let mut ys = Vec::with_capacity(b - a);
        let (mut x0, mut x1, mut y0, mut y1) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
        let (mut sx, mut sy) = (0.0f64, 0.0f64);
        for p in a..b {
            let (x, y) = (ll[[p, 0]], ll[[p, 1]]);
            xs.push(x);
            ys.push(y);
            sx += x;
            sy += y;
            x0 = x0.min(x);
            x1 = x1.max(x);
            y0 = y0.min(y);
            y1 = y1.max(y);
        }
        let finite = x0.is_finite() && y0.is_finite() && x1.is_finite() && y1.is_finite();
        if !finite || (x1 - x0) > 0.05 || (y1 - y0) > 0.05 {
            continue;
        }
        let gx0 = (x0 / BIN_DEG).floor() as i64;
        let gx1 = (x1 / BIN_DEG).floor() as i64;
        let gy0 = (y0 / BIN_DEG).floor() as i64;
        let gy1 = (y1 / BIN_DEG).floor() as i64;
        for gx in gx0..=gx1 {
            for gy in gy0..=gy1 {
                if let Some(v) = rec_grid.get(&(gx, gy)) {
                    for &k in v {
                        let (px, py_) = (rx[k], ry[k]);
                        if px >= x0 && px <= x1 && py_ >= y0 && py_ <= y1 && inside(&xs, &ys, px, py_) {
                            matched[k] = true;
                        }
                    }
                }
            }
        }
        let m = xs.len() as f64;
        let fp_idx = fps.len();
        fps.push(MapFootprint { x0, y0, x1, y1, cx: sx / m, cy: sy / m, xs, ys });
        for gx in gx0..=gx1 {
            for gy in gy0..=gy1 {
                map_grid.entry((gx, gy)).or_default().push(fp_idx);
            }
        }
    }

    let (lon0, lat0, lon1, lat1) = bounds;
    let mut cands: Vec<(String, usize, Vec<[f64; 2]>)> = Vec::new();
    let mut rxs: Vec<f64> = Vec::new();
    let mut rys: Vec<f64> = Vec::new();
    for k in 0..nr {
        if matched[k] {
            continue;
        }
        let (px, py_, area) = (rx[k], ry[k], ra[k]);
        if !px.is_finite() || !py_.is_finite() || !area.is_finite() || area < min_area_m2 {
            continue;
        }
        if px < lon0 || px >= lon1 || py_ < lat0 || py_ >= lat1 {
            continue;
        }
        let (a, b) = (roff[k].max(0) as usize, (roff[k + 1].max(0) as usize).min(rll.nrows()));
        if b <= a + 2 {
            continue;
        }
        rxs.clear();
        rys.clear();
        let (mut x0, mut x1, mut y0, mut y1) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
        let mut ok_ring = true;
        for p in a..b {
            let (x, y) = (rll[[p, 0]], rll[[p, 1]]);
            if !x.is_finite() || !y.is_finite() {
                ok_ring = false;
                break;
            }
            if let (Some(&lx), Some(&ly)) = (rxs.last(), rys.last()) {
                if (x - lx).abs() == 0.0 && (y - ly).abs() == 0.0 {
                    continue;
                }
            }
            rxs.push(x);
            rys.push(y);
            x0 = x0.min(x);
            x1 = x1.max(x);
            y0 = y0.min(y);
            y1 = y1.max(y);
        }
        if !ok_ring || rxs.len() < 3 || (x1 - x0) <= 0.0 || (y1 - y0) <= 0.0 || (x1 - x0) > 0.05 || (y1 - y0) > 0.05 {
            continue;
        }
        if rxs[0] != *rxs.last().unwrap() || rys[0] != *rys.last().unwrap() {
            let (fx, fy) = (rxs[0], rys[0]);
            rxs.push(fx);
            rys.push(fy);
        }
        if rxs.len() < 4 {
            continue;
        }
        let gx0 = (x0 / BIN_DEG).floor() as i64;
        let gx1 = (x1 / BIN_DEG).floor() as i64;
        let gy0 = (y0 / BIN_DEG).floor() as i64;
        let gy1 = (y1 / BIN_DEG).floor() as i64;
        let mut overlaps_map = false;
        'bins: for gx in gx0..=gx1 {
            for gy in gy0..=gy1 {
                if let Some(v) = map_grid.get(&(gx, gy)) {
                    for &fi in v {
                        let fp = &fps[fi];
                        if fp.x1 < x0 || fp.x0 > x1 || fp.y1 < y0 || fp.y0 > y1 {
                            continue;
                        }
                        if fp.cx >= x0 && fp.cx <= x1 && fp.cy >= y0 && fp.cy <= y1 && inside(&rxs, &rys, fp.cx, fp.cy)
                        {
                            overlaps_map = true;
                            break 'bins;
                        }
                        for (&vx, &vy) in fp.xs.iter().zip(fp.ys.iter()) {
                            if vx >= x0 && vx <= x1 && vy >= y0 && vy <= y1 && inside(&rxs, &rys, vx, vy) {
                                overlaps_map = true;
                                break 'bins;
                            }
                        }
                        for (&rvx, &rvy) in rxs.iter().zip(rys.iter()) {
                            if rvx >= fp.x0 && rvx <= fp.x1 && rvy >= fp.y0 && rvy <= fp.y1
                                && inside(&fp.xs, &fp.ys, rvx, rvy)
                            {
                                overlaps_map = true;
                                break 'bins;
                            }
                        }
                    }
                }
            }
        }
        if overlaps_map {
            continue;
        }
        let raw = rec_ids[k].trim();
        let ign_id = if raw.is_empty() {
            format!("~ign:b{k}")
        } else if raw.starts_with("~ign:") {
            raw.to_string()
        } else {
            format!("~ign:{raw}")
        };
        let ring: Vec<[f64; 2]> = rxs.iter().copied().zip(rys.iter().copied()).map(|(x, y)| [x, y]).collect();
        cands.push((ign_id, k, ring));
    }

    cands.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.cmp(&b.1)));
    let mut keep_idx: Vec<i64> = Vec::with_capacity(cands.len());
    let mut out_ids: Vec<String> = Vec::with_capacity(cands.len());
    let mut out_off: Vec<i64> = Vec::with_capacity(cands.len() + 1);
    out_off.push(0);
    let mut out_coords: Vec<f64> = Vec::new();
    for (ign_id, k, ring) in cands {
        if out_ids.last().is_some_and(|prev| prev == &ign_id) {
            continue;
        }
        keep_idx.push(k as i64);
        out_ids.push(ign_id);
        for pt in &ring {
            out_coords.push(pt[0]);
            out_coords.push(pt[1]);
        }
        out_off.push((out_coords.len() / 2) as i64);
    }
    (keep_idx, out_ids, out_off, out_coords)
}

/// Unmatched BD TOPO building records in `bounds` `(lon0, lat0, lon1, lat1)` with `rec_area >= min_area_m2`, sorted
/// deterministically by their `~ign:<id>` key: `(keep_idx (m,), ids [str], coord_off (m + 1,), lonlat (c, 2))`.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn bdtopo_unmatched<'py>(
    py: Python<'py>,
    start: PyReadonlyArray1<'py, i64>,
    end: PyReadonlyArray1<'py, i64>,
    lonlat: PyReadonlyArray2<'py, f64>,
    rec_lon: PyReadonlyArray1<'py, f64>,
    rec_lat: PyReadonlyArray1<'py, f64>,
    rec_area: PyReadonlyArray1<'py, f64>,
    rec_off: PyReadonlyArray1<'py, i64>,
    rec_lonlat: PyReadonlyArray2<'py, f64>,
    rec_ids: Vec<String>,
    bounds: (f64, f64, f64, f64),
    min_area_m2: f64,
) -> (
    Bound<'py, PyArray1<i64>>,
    Vec<String>,
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray2<f64>>,
) {
    let (s, e) = (start.as_slice().unwrap_or(&[]), end.as_slice().unwrap_or(&[]));
    let ll = lonlat.as_array();
    let rx = rec_lon.as_slice().unwrap_or(&[]);
    let ry = rec_lat.as_slice().unwrap_or(&[]);
    let ra = rec_area.as_slice().unwrap_or(&[]);
    let roff = rec_off.as_slice().unwrap_or(&[]);
    let rll = rec_lonlat.as_array();
    let (keep_idx, out_ids, out_off, out_coords) =
        bdtopo_unmatched_core(s, e, ll, rx, ry, ra, roff, rll, &rec_ids, bounds, min_area_m2);
    let n_pts = out_coords.len() / 2;
    let arr_ll = Array2::from_shape_vec((n_pts, 2), out_coords).unwrap_or_else(|_| Array2::zeros((0, 2)));
    (keep_idx.into_pyarray(py), out_ids, out_off.into_pyarray(py), arr_ll.into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sq(lon: f64, lat: f64, d: f64) -> Vec<[f64; 2]> {
        vec![[lon, lat], [lon + d, lat], [lon + d, lat + d], [lon, lat + d], [lon, lat]]
    }

    #[test]
    fn unmatched_excludes_matched_and_sorts_by_ign_key() {
        // Map has one building at (5.0, 43.0)
        let map_ring = sq(5.0, 43.0, 0.0002);
        let map_flat: Vec<f64> = map_ring.iter().flat_map(|p| [p[0], p[1]]).collect();
        let map_ll = Array2::from_shape_vec((5, 2), map_flat).unwrap();
        let start = [0i64];
        let end = [5i64];

        // Three BD TOPO records:
        // 0: inside the map building -> matched (excluded)
        // 1: at (5.002, 43.0) with id "BAT02" -> unmatched
        // 2: at (5.001, 43.0) with id "BAT01" -> unmatched (sorts before BAT02)
        // 3: at (5.1, 43.0) outside cell bounds -> excluded
        let r0 = sq(5.00002, 43.00002, 0.0001);
        let r1 = sq(5.002, 43.0, 0.0001);
        let r2 = sq(5.001, 43.0, 0.0001);
        let r3 = sq(5.1, 43.0, 0.0001);
        let all_rings = [r0, r1, r2, r3];
        let mut roff = vec![0i64];
        let mut rflat = Vec::new();
        let mut rx = Vec::new();
        let mut ry = Vec::new();
        let mut ra = Vec::new();
        for r in &all_rings {
            let xs: Vec<f64> = r.iter().map(|p| p[0]).collect();
            let ys: Vec<f64> = r.iter().map(|p| p[1]).collect();
            let (px, py, area) = interior(&xs, &ys);
            rx.push(px);
            ry.push(py);
            ra.push(area);
            for p in r {
                rflat.push(p[0]);
                rflat.push(p[1]);
            }
            roff.push((rflat.len() / 2) as i64);
        }
        let rll = Array2::from_shape_vec((rflat.len() / 2, 2), rflat).unwrap();
        let ids = vec!["BAT00".to_string(), "BAT02".to_string(), "BAT01".to_string(), "BAT03".to_string()];
        let (keep, out_ids, out_off, out_coords) = bdtopo_unmatched_core(
            &start,
            &end,
            map_ll.view(),
            &rx,
            &ry,
            &ra,
            &roff,
            rll.view(),
            &ids,
            (4.99, 42.99, 5.05, 43.05),
            12.0,
        );
        assert_eq!(out_ids, vec!["~ign:BAT01".to_string(), "~ign:BAT02".to_string()]);
        assert_eq!(keep, vec![2, 1]);
        assert_eq!(out_off, vec![0, 5, 10]);
        assert_eq!(out_coords.len(), 20);
    }
}
