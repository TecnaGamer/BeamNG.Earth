//! vegetation._mask_polys in Rust (2026-10-23: half of a tree cell's build, 13.9 of 26.6 s for six Baltimore cells,
//! was its Python loop over every building and water polygon). The same test as matplotlib's Path.contains_points
//! (point_in_path: the crossing test multiplied out, no division), per pixel centre, XOR over a part's rings; each
//! row tests only the edges that cross it.

use numpy::{PyReadonlyArray1, PyReadonlyArray2, PyReadwriteArray2};
use pyo3::prelude::*;

const R: f64 = 6378137.0;
const MAX_LAT: f64 = 85.0511287798066;

/// matplotlib's point_in_path over one closed ring (vx, vy), with its implicit closing edge: does (tx, ty) lie
/// inside (an odd number of crossings)?
#[inline]
fn crossings(edges: &[(f64, f64, f64, f64)], tx: f64, ty: f64) -> bool {
    let mut inside = false;
    for &(x0, y0, x1, y1) in edges {
        let f0 = y0 >= ty;
        let f1 = y1 >= ty;
        if f0 != f1 && (((y1 - ty) * (x0 - x1) >= (x1 - tx) * (y0 - y1)) == f1) {
            inside = !inside;
        }
    }
    inside
}

/// Mark the parts of the selected features (their rings given as lon / lat, mapgeom's offsets) on mask (rows,
/// cols; 0/1), the grid x0, y0 (EPSG:3857 of the top left corner), dx, dy (m per pixel); g: the bounding box's margin
/// in pixels (vegetation's grow_m / dx).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn mask_polys(py: Python<'_>, mut mask: PyReadwriteArray2<'_, u8>, lonlat: PyReadonlyArray2<'_, f64>,
                  coord_off: PyReadonlyArray1<'_, i64>, ring_off: PyReadonlyArray1<'_, i64>,
                  part_off: PyReadonlyArray1<'_, i64>, feats: PyReadonlyArray1<'_, i64>, x0: f64, y0: f64, dx: f64,
                  dy: f64, g: f64) -> usize {
    let ll = lonlat.as_array();
    let co = coord_off.as_slice().unwrap();
    let ro = ring_off.as_slice().unwrap();
    let po = part_off.as_slice().unwrap();
    let fs = feats.as_slice().unwrap().to_vec();
    let mut m = mask.as_array_mut();
    let (n_r, n_c) = (m.shape()[0], m.shape()[1]);
    let pts: Vec<(f64, f64)> = (0..ll.shape()[0]).map(|k| {
        let lon = ll[[k, 0]];
        let lat = ll[[k, 1]].clamp(-MAX_LAT, MAX_LAT);
        let x = lon.to_radians() * R;
        let y = (std::f64::consts::PI / 4.0 + lat.to_radians() / 2.0).tan().ln() * R;
        ((x - x0) / dx, (y0 - y) / dy)
    }).collect();
    let mut marked = 0usize;
    py.allow_threads(|| {
        let mut row_edges: Vec<(f64, f64, f64, f64)> = Vec::new();
        for &i in &fs {
            let i = i as usize;
            for p in po[i] as usize..po[i + 1] as usize {
                // the part's edges: each ring's segments plus its closing one
                let mut edges: Vec<(f64, f64, f64, f64)> = Vec::new();
                let (mut cmin, mut rmin, mut cmax, mut rmax) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY,
                                                                f64::NEG_INFINITY);
                for r in ro[p] as usize..ro[p + 1] as usize {
                    let (a, b) = (co[r] as usize, co[r + 1] as usize);
                    if b <= a {
                        continue;
                    }
                    for k in a..b {
                        let (c, rr) = pts[k];
                        cmin = cmin.min(c);
                        cmax = cmax.max(c);
                        rmin = rmin.min(rr);
                        rmax = rmax.max(rr);
                        let (c2, r2) = if k + 1 < b { pts[k + 1] } else { pts[a] };
                        edges.push((c, rr, c2, r2));
                    }
                }
                if edges.is_empty() {
                    continue;
                }
                let c0 = ((cmin - g).floor() as i64).max(0);
                let r0 = ((rmin - g).floor() as i64).max(0);
                let c1 = ((cmax + g).ceil() as i64 + 1).min(n_c as i64);
                let r1 = ((rmax + g).ceil() as i64 + 1).min(n_r as i64);
                if c1 <= c0 || r1 <= r0 {
                    continue;
                }
                for row in r0..r1 {
                    let ty = row as f64 + 0.5;
                    row_edges.clear();
                    row_edges.extend(edges.iter().copied().filter(|&(_, ya, _, yb)| (ya >= ty) != (yb >= ty)));
                    if row_edges.is_empty() {
                        continue;
                    }
                    for col in c0..c1 {
                        if crossings(&row_edges, col as f64 + 0.5, ty) {
                            let cell = &mut m[[row as usize, col as usize]];
                            if *cell == 0 {
                                *cell = 1;
                                marked += 1;
                            }
                        }
                    }
                }
            }
        }
    });
    marked
}

/// vegetation._mask_roads' segments (2026-10-23: its Python loop over every road ring was most of what was left of a
/// tree cell's build): the selected features' rings (feats, hw_m: each one's half width in ground metres) on the
/// CHM grid, each ring's half width in grid pixels (hw_m / (dx * max(cos(mean lat), 0.05))), rings wholly off the
/// grid by more than their half width left out. -> (segs (m, 4) c0 r0 c1 r1, hws (m,)), in order.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn road_segments<'py>(py: Python<'py>, lonlat: PyReadonlyArray2<'py, f64>, coord_off: PyReadonlyArray1<'py, i64>,
                          ring_off: PyReadonlyArray1<'py, i64>, part_off: PyReadonlyArray1<'py, i64>,
                          feats: PyReadonlyArray1<'py, i64>, hw_m: PyReadonlyArray1<'py, f64>, x0: f64, y0: f64,
                          dx: f64, dy: f64, n_r: usize, n_c: usize)
                          -> (Bound<'py, numpy::PyArray2<f64>>, Bound<'py, numpy::PyArray1<f64>>) {
    use numpy::IntoPyArray;
    let ll = lonlat.as_array();
    let co = coord_off.as_slice().unwrap();
    let ro = ring_off.as_slice().unwrap();
    let po = part_off.as_slice().unwrap();
    let fs = feats.as_slice().unwrap();
    let hm = hw_m.as_slice().unwrap();
    let mut segs: Vec<f64> = Vec::new();
    let mut hws: Vec<f64> = Vec::new();
    for (fi, &i) in fs.iter().enumerate() {
        let i = i as usize;
        for p in po[i] as usize..po[i + 1] as usize {
            for r in ro[p] as usize..ro[p + 1] as usize {
                let (a, b) = (co[r] as usize, co[r + 1] as usize);
                if b < a + 2 {
                    if b == a + 1 {
                        // (one point: no segment; numpy's empty concatenation)
                    }
                    continue;
                }
                let n = (b - a) as f64;
                let mean_lat = (a..b).map(|k| ll[[k, 1]]).sum::<f64>() / n;
                let hw = hm[fi] / (dx * mean_lat.to_radians().cos().max(0.05));
                let q: Vec<(f64, f64)> = (a..b).map(|k| {
                    let lon = ll[[k, 0]];
                    let lat = ll[[k, 1]].clamp(-MAX_LAT, MAX_LAT);
                    let x = lon.to_radians() * R;
                    let y = (std::f64::consts::PI / 4.0 + lat.to_radians() / 2.0).tan().ln() * R;
                    ((x - x0) / dx, (y0 - y) / dy)
                }).collect();
                let (mut cmin, mut rmin, mut cmax, mut rmax) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY,
                                                                f64::NEG_INFINITY);
                for &(c, rr) in &q {
                    cmin = cmin.min(c);
                    cmax = cmax.max(c);
                    rmin = rmin.min(rr);
                    rmax = rmax.max(rr);
                }
                if cmax < -hw || rmax < -hw || cmin > n_c as f64 + hw || rmin > n_r as f64 + hw {
                    continue;
                }
                for k in 0..q.len() - 1 {
                    segs.extend_from_slice(&[q[k].0, q[k].1, q[k + 1].0, q[k + 1].1]);
                    hws.push(hw);
                }
            }
        }
    }
    let m = hws.len();
    let _ = py;
    (numpy::ndarray::Array2::from_shape_vec((m, 4), segs).unwrap().into_pyarray(py), hws.into_pyarray(py))
}

/// vegetation._shift_max: the maximum over the disc of radius r around each pixel ((dj - r)^2 + (di - r)^2 <=
/// r * r + r), outside the array -inf (float32, exact: maxima only). The disc as rows of spans: a running max along
/// each row per span half width (van Herk), then the max of the rows' spans.
#[pyfunction]
pub fn disc_max<'py>(py: Python<'py>, a: numpy::PyReadonlyArray2<'py, f32>, r: usize)
                     -> Bound<'py, numpy::PyArray2<f32>> {
    use numpy::IntoPyArray;
    let arr = a.as_array();
    let (n, m) = (arr.shape()[0], arr.shape()[1]);
    let src: Vec<f32> = arr.iter().copied().collect();
    let out = py.allow_threads(|| {
        let ri = r as i64;
        // half width of the disc's span on row offset dj: the largest di with dj^2 + di^2 <= r^2 + r
        let half: Vec<usize> = (-ri..=ri).map(|dj| {
            let mut w = 0i64;
            while (w + 1) * (w + 1) + dj * dj <= ri * ri + ri {
                w += 1;
            }
            w as usize
        }).collect();
        let mut widths: Vec<usize> = half.clone();
        widths.sort_unstable();
        widths.dedup();
        let fmax = |x: f32, y: f32| if y > x { y } else { x };
        // runs[w]: the max over [i - w, i + w] of each row (outside: -inf)
        let mut runs: std::collections::HashMap<usize, Vec<f32>> = std::collections::HashMap::new();
        for &w in &widths {
            let mut h = vec![f32::NEG_INFINITY; n * m];
            if w == 0 {
                h.copy_from_slice(&src);
            } else {
                let k = 2 * w + 1;
                let len = m + 2 * w;
                let pl = len + (k - len % k) % k;
                let mut p = vec![f32::NEG_INFINITY; pl];
                let mut g = vec![0f32; pl];
                let mut sfx = vec![0f32; pl];
                for j in 0..n {
                    p[w..w + m].copy_from_slice(&src[j * m..(j + 1) * m]);
                    for b in (0..pl).step_by(k) {
                        g[b] = p[b];
                        for t in b + 1..b + k {
                            g[t] = fmax(g[t - 1], p[t]);
                        }
                        sfx[b + k - 1] = p[b + k - 1];
                        for t in (b..b + k - 1).rev() {
                            sfx[t] = fmax(sfx[t + 1], p[t]);
                        }
                    }
                    for i in 0..m {
                        h[j * m + i] = fmax(sfx[i], g[i + k - 1]);
                    }
                }
            }
            runs.insert(w, h);
        }
        let mut out = vec![f32::NEG_INFINITY; n * m];
        for (t, dj) in (-ri..=ri).enumerate() {
            let h = &runs[&half[t]];
            for j in 0..n as i64 {
                let y = j + dj;
                if y < 0 || y >= n as i64 {
                    continue;
                }
                let (o, s) = (&mut out[j as usize * m..(j as usize + 1) * m], &h[y as usize * m..(y as usize + 1) * m]);
                for i in 0..m {
                    if s[i] > o[i] {
                        o[i] = s[i];
                    }
                }
            }
        }
        out
    });
    numpy::ndarray::Array2::from_shape_vec((n, m), out).unwrap().into_pyarray(py)
}

/// vegetation._thin: non-maximum suppression of tree tops (jj, ii pixel rows / columns, h their heights, float32):
/// the tallest first (ties by row, then column), each removing the others within max(its, their) crown radius
/// rad = spacing * (0.12 h + 1) / px (float32, as numpy); the distance test as numpy's: the integer square distance
/// against the float32 square in float64. -> keep (bool per top).
#[pyfunction]
pub fn thin_tops<'py>(py: Python<'py>, jj: PyReadonlyArray1<'py, i64>, ii: PyReadonlyArray1<'py, i64>,
                      h: PyReadonlyArray1<'py, f32>, spacing: f32, px: f32) -> Bound<'py, numpy::PyArray1<bool>> {
    use numpy::IntoPyArray;
    let jj = jj.as_slice().unwrap().to_vec();
    let ii = ii.as_slice().unwrap().to_vec();
    let h = h.as_slice().unwrap().to_vec();
    let n = jj.len();
    let keep = py.allow_threads(|| {
        // (at least MIN_RAD_PX: a 9 m tree's 0.87 px let every pixel of a flat crown in the canopy map, its heights
        // whole metres, stand as its own tree: clusters of up to 9 trees in 3 m by East Pratt Street, 2026-10-31)
        const MIN_RAD_PX: f32 = 1.5;
        let rad: Vec<f32> = h.iter().map(|&v| (spacing * (0.12f32 * v + 1.0f32) / px).max(MIN_RAD_PX)).collect();
        let mut order: Vec<usize> = (0..n).collect();
        // np.lexsort((ii, jj, -h)): by -h, then jj, then ii (stable)
        order.sort_by(|&a, &b| {
            (-h[a]).partial_cmp(&-h[b]).unwrap_or(std::cmp::Ordering::Equal)
                .then(jj[a].cmp(&jj[b])).then(ii[a].cmp(&ii[b]))
        });
        let rmax = rad.iter().cloned().fold(f32::NEG_INFINITY, f32::max);
        let cell = (rmax.ceil() as i64).max(1);
        let mut grid: std::collections::HashMap<(i64, i64), Vec<usize>> = std::collections::HashMap::new();
        let mut keep = vec![false; n];
        for &k in &order {
            let (j, i, r) = (jj[k], ii[k], rad[k]);
            let (gj, gi) = (j.div_euclid(cell), i.div_euclid(cell));
            let mut ok = true;
            'outer: for a in gj - 1..=gj + 1 {
                for b in gi - 1..=gi + 1 {
                    if let Some(list) = grid.get(&(a, b)) {
                        for &m in list {
                            let d2 = (jj[m] - j) * (jj[m] - j) + (ii[m] - i) * (ii[m] - i);
                            let rr = if r >= rad[m] { r } else { rad[m] };
                            if (d2 as f64) < (rr * rr) as f64 {
                                ok = false;
                                break 'outer;
                            }
                        }
                    }
                }
            }
            if ok {
                keep[k] = true;
                grid.entry((gj, gi)).or_default().push(k);
            }
        }
        keep
    });
    keep.into_pyarray(py)
}
