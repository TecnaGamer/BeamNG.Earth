//! Geometry kernels for the world server (server/fast.py wraps them, with the Python versions as the fallback).
//! All coordinates are metres in the anchor's local frame, f64.
use numpy::{PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3, IntoPyArray};
use pyo3::types::PyBytes;
use pyo3::prelude::*;

/// Work (about the number of inner-loop steps) below which a kernel keeps the GIL: a thread that lets it go waits for
/// it again behind the interpreter's busy threads, up to the switch interval (5 ms), so a call of a few microseconds
/// cost milliseconds (the server's event loop on a Steam Deck spent ~22 s of a Baltimore teleport there, 2026-10-20).
const RELEASE_MIN: usize = 200_000;

fn release<T, F>(py: Python<'_>, work: usize, f: F) -> T
where
    F: pyo3::marker::Ungil + FnOnce() -> T,
    T: pyo3::marker::Ungil,
{
    if work >= RELEASE_MIN { py.allow_threads(f) } else { f() }
}


/// Even-odd point-in-polygon (the polygon's last vertex joins its first), as islands._inside.
#[inline]
fn inside(px: &[f64], py: &[f64], x: f64, y: f64) -> bool {
    let n = px.len().min(py.len());
    if n == 0 {
        return false;
    }
    let mut c = false;
    let mut j = n - 1;
    for i in 0..n {
        let (xi, yi, xj, yj) = (px[i], py[i], px[j], py[j]);
        if (yi > y) != (yj > y) {
            let dy = if yj - yi == 0.0 { 1e-12 } else { yj - yi };
            if x < (xj - xi) * (y - yi) / dy + xi {
                c = !c;
            }
        }
        j = i;
    }
    c
}

/// Distance from (x, y) to segment a-b.
#[inline]
fn seg_dist(ax: f64, ay: f64, bx: f64, by: f64, x: f64, y: f64) -> f64 {
    let (dx, dy) = (bx - ax, by - ay);
    let l2 = (dx * dx + dy * dy).max(1e-12);
    let t = (((x - ax) * dx + (y - ay) * dy) / l2).clamp(0.0, 1.0);
    let (qx, qy) = (ax + dx * t - x, ay + dy * t - y);
    (qx * qx + qy * qy).sqrt()
}

/// Is (x, y) within its segment's half width of any segment (segs: (m, 4) ax ay bx by, hw: (m,))?
#[inline]
fn near_any(segs: &[[f64; 4]], hw: &[f64], x: f64, y: f64) -> bool {
    segs.iter().zip(hw).any(|(s, &h)| seg_dist(s[0], s[1], s[2], s[3], x, y) <= h)
}

fn to_segs(segs: &PyReadonlyArray2<f64>) -> Vec<[f64; 4]> {
    let a = segs.as_array();
    if a.ncols() < 4 {
        return Vec::new();
    }
    a.rows().into_iter().map(|r| [r[0], r[1], r[2], r[3]]).collect()
}

/// islands._island_ring's ray march: from (cx, cy) along `rays` evenly spaced directions, in steps of `step` up to
/// rmax, the last point inside poly and clear of every segment's half width. Returns (rays, 2).
#[pyfunction]
fn island_ring_rays<'py>(py: Python<'py>, poly: PyReadonlyArray2<'py, f64>, segs: PyReadonlyArray2<'py, f64>,
                         hw: PyReadonlyArray1<'py, f64>, cx: f64, cy: f64, rays: usize, step: f64, rmax: f64)
                         -> Bound<'py, PyArray2<f64>> {
    let p = poly.as_array();
    let (px, pys) = if p.ncols() >= 2 {
        (p.column(0).to_vec(), p.column(1).to_vec())
    } else {
        (Vec::new(), Vec::new())
    };
    let s = to_segs(&segs);
    let h: Vec<f64> = hw.as_array().iter().copied().collect();
    let out = release(py, px.len() * s.len().max(1), || {
        let mut out = Vec::with_capacity(rays * 2);
        if rays == 0 {
            return out;
        }
        for k in 0..rays {
            let a = 2.0 * std::f64::consts::PI * (k as f64) / (rays as f64);
            let (ux, uy) = (a.cos(), a.sin());
            let mut r = 0.0;
            if step > 0.0 {
                while r < rmax {
                    let (qx, qy) = (cx + ux * (r + step), cy + uy * (r + step));
                    if !inside(&px, &pys, qx, qy) || near_any(&s, &h, qx, qy) {
                        break;
                    }
                    r += step;
                }
            }
            out.push(cx + ux * r);
            out.push(cy + uy * r);
        }
        out
    });
    numpy::ndarray::Array2::from_shape_vec((rays, 2), out).unwrap().into_pyarray(py)
}

/// Point-in-polygon for many points: pts (n, 2) -> bool (n,).
#[pyfunction]
fn points_inside<'py>(py: Python<'py>, poly: PyReadonlyArray2<'py, f64>, pts: PyReadonlyArray2<'py, f64>)
                      -> Bound<'py, PyArray1<bool>> {
    let p = poly.as_array();
    let q = pts.as_array();
    if p.nrows() < 3 || p.ncols() < 2 || q.ncols() < 2 {
        return vec![false; q.nrows()].into_pyarray(py);
    }
    let px: Vec<f64> = p.column(0).to_vec();
    let pys: Vec<f64> = p.column(1).to_vec();
    let v: Vec<bool> = q.rows().into_iter().map(|r| inside(&px, &pys, r[0], r[1])).collect();
    v.into_pyarray(py)
}

/// Distance from each point to the nearest of the polyline's segments: line (k, 2), pts (n, 2) -> (n,).
#[pyfunction]
fn polyline_dist<'py>(py: Python<'py>, line: PyReadonlyArray2<'py, f64>, pts: PyReadonlyArray2<'py, f64>)
                      -> Bound<'py, PyArray1<f64>> {
    let l = line.as_array();
    let q = pts.as_array();
    let k = l.nrows();
    if k < 2 || l.ncols() < 2 || q.ncols() < 2 {
        return vec![f64::INFINITY; q.nrows()].into_pyarray(py);
    }
    let v: Vec<f64> = q.rows().into_iter().map(|r| {
        let mut best = f64::INFINITY;
        for i in 0..k.saturating_sub(1) {
            let d = seg_dist(l[[i, 0]], l[[i, 1]], l[[i + 1, 0]], l[[i + 1, 1]], r[0], r[1]);
            if d < best { best = d; }
        }
        best
    }).collect();
    v.into_pyarray(py)
}

/// furniture._clear: is (px, py) farther than its segment's half width + margin from every segment not owned by
/// `own` (segs (m, 4), half (m,), owner (m,) as i64)?
#[pyfunction]
fn clear_of(segs: PyReadonlyArray2<f64>, half: PyReadonlyArray1<f64>, owner: PyReadonlyArray1<i64>, px: f64, py: f64,
            own: i64, margin: f64) -> bool {
    let s = segs.as_array();
    let h = half.as_array();
    let o = owner.as_array();
    let n = s.nrows().min(h.len()).min(o.len());
    if n == 0 || s.ncols() < 4 {
        return true;
    }
    for i in 0..n {
        if o[i] == own {
            continue;
        }
        if seg_dist(s[[i, 0]], s[[i, 1]], s[[i, 2]], s[[i, 3]], px, py) < h[i] + margin {
            return false;
        }
    }
    true
}

/// roads._dual_side: (share of a's ~10 m samples with b beside it gap_min..gap_max apart and parallel within
/// cos_par, mean side of b over those: +1 left of a's direction, -1 right). a (k, 2), b (m, 2).
#[pyfunction]
fn dual_side(a: PyReadonlyArray2<f64>, b: PyReadonlyArray2<f64>, gap_min: f64, gap_max: f64, cos_par: f64)
             -> (f64, f64) {
    let a = a.as_array();
    let b = b.as_array();
    let ka = a.nrows();
    let kb = b.nrows();
    if ka < 2 || kb < 2 || a.ncols() < 2 || b.ncols() < 2 {
        return (0.0, 0.0);
    }
    let nseg = ka - 1;
    let mut s = vec![0.0f64; ka];
    let mut seg = vec![[0.0f64; 2]; nseg];
    let mut len = vec![0.0f64; nseg];
    for i in 0..nseg {
        let dx = a[[i + 1, 0]] - a[[i, 0]];
        let dy = a[[i + 1, 1]] - a[[i, 1]];
        seg[i] = [dx, dy];
        len[i] = (dx * dx + dy * dy).sqrt();
        s[i + 1] = s[i] + len[i];
    }
    let total = s[ka - 1];
    if !(total >= 1.0) {
        return (0.0, 0.0);
    }
    let n = std::cmp::max(2, (total / 10.0).floor() as usize + 1);
    let mb = kb - 1;
    let mut bs = vec![[0.0f64; 2]; mb];
    let mut bl2 = vec![0.0f64; mb];
    for i in 0..mb {
        let dx = b[[i + 1, 0]] - b[[i, 0]];
        let dy = b[[i + 1, 1]] - b[[i, 1]];
        bs[i] = [dx, dy];
        bl2[i] = (dx * dx + dy * dy).max(1e-12);
    }
    let (mut ok_n, mut side_sum) = (0usize, 0.0f64);
    let step = total / ((n - 1) as f64);
    for si in 0..n {
        // np.linspace: i * step, the last exactly the end
        let t = if si == n - 1 { total } else { (si as f64) * step };
        // np.searchsorted(s, t, side="right") - 1, clipped to a segment
        let mut k = s.partition_point(|&v| v <= t) as isize - 1;
        k = k.clamp(0, nseg as isize - 1);
        let k = k as usize;
        // np.interp(t, s, a): linear within the segment holding t (the last point at the end)
        let (qx, qy) = if t >= total {
            (a[[ka - 1, 0]], a[[ka - 1, 1]])
        } else {
            let f = if len[k] > 0.0 { (t - s[k]) / len[k] } else { 0.0 };
            (a[[k, 0]] + seg[k][0] * f, a[[k, 1]] + seg[k][1] * f)
        };
        let lk = len[k].max(1e-9);
        let (tax, tay) = (seg[k][0] / lk, seg[k][1] / lk);
        let (mut best, mut bj, mut nx, mut ny) = (f64::INFINITY, 0usize, 0.0, 0.0);
        for j in 0..mb {
            let f = (((qx - b[[j, 0]]) * bs[j][0] + (qy - b[[j, 1]]) * bs[j][1]) / bl2[j]).clamp(0.0, 1.0);
            let (px, py) = (b[[j, 0]] + bs[j][0] * f, b[[j, 1]] + bs[j][1] * f);
            let d = (px - qx).hypot(py - qy);    // (as np.hypot: ties at a shared vertex pick the same segment)
            if d < best {
                best = d;
                bj = j;
                nx = px;
                ny = py;
            }
        }
        let bn = bl2[bj].sqrt();
        let (tbx, tby) = (bs[bj][0] / bn, bs[bj][1] / bn);
        let par = (tax * tbx + tay * tby).abs() >= cos_par;
        if best >= gap_min && best <= gap_max && par {
            ok_n += 1;
            let (vx, vy) = (nx - qx, ny - qy);
            let c = tax * vy - tay * vx;
            side_sum += if c > 0.0 { 1.0 } else if c < 0.0 { -1.0 } else { 0.0 };
        }
    }
    if ok_n == 0 {
        return (0.0, 0.0);
    }
    (ok_n as f64 / n as f64, side_sum / ok_n as f64)
}

/// roads._deck_crossings: (i, j, arc on i, arc on j) where polylines i < j cross away from their end points.
/// pts (n, 2) all lines one after another, starts (L + 1,) where each begins (the last: n).
#[pyfunction]
fn deck_crossings(pts: PyReadonlyArray2<f64>, starts: PyReadonlyArray1<i64>) -> Vec<(usize, usize, f64, f64)> {
    let p = pts.as_array();
    let st = starts.as_array();
    let nl = st.len().saturating_sub(1);
    if nl < 2 || p.nrows() < 2 || p.ncols() < 2 {
        return Vec::new();
    }
    let rng: Vec<(usize, usize)> = (0..nl).map(|i| (st[i] as usize, st[i + 1] as usize)).collect();
    let mut boxes = Vec::with_capacity(nl);
    let mut arcs: Vec<Vec<f64>> = Vec::with_capacity(nl);
    for &(a, b) in &rng {
        let (mut lo, mut hi) = ([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]);
        let mut arc = Vec::with_capacity(b.saturating_sub(a));
        let mut acc = 0.0;
        for k in a..b {
            for c in 0..2 {
                lo[c] = lo[c].min(p[[k, c]]);
                hi[c] = hi[c].max(p[[k, c]]);
            }
            if k > a {
                acc += (p[[k, 0]] - p[[k - 1, 0]]).hypot(p[[k, 1]] - p[[k - 1, 1]]);
            }
            arc.push(acc);
        }
        boxes.push((lo, hi));
        arcs.push(arc);
    }
    let mut out = Vec::new();
    for i in 0..nl {
        let (ai, bi) = rng[i];
        if bi.saturating_sub(ai) < 2 {
            continue;
        }
        for j in (i + 1)..nl {
            let (aj, bj) = rng[j];
            if bj.saturating_sub(aj) < 2 {
                continue;
            }
            let ((alo, ahi), (blo, bhi)) = (boxes[i], boxes[j]);
            if ahi[0] < blo[0] || bhi[0] < alo[0] || ahi[1] < blo[1] || bhi[1] < alo[1] {
                continue;
            }
            let (si_end, sj_end) = (arcs[i][bi - ai - 1], arcs[j][bj - aj - 1]);
            for q in 0..(bj - aj - 1) {
                let (ax, ay) = (p[[aj + q, 0]], p[[aj + q, 1]]);
                let (d0, d1) = (p[[aj + q + 1, 0]] - ax, p[[aj + q + 1, 1]] - ay);
                for k in 0..(bi - ai - 1) {
                    let (px, py) = (p[[ai + k, 0]], p[[ai + k, 1]]);
                    let (r0, r1) = (p[[ai + k + 1, 0]] - px, p[[ai + k + 1, 1]] - py);
                    let den = r0 * d1 - r1 * d0;
                    if den.abs() <= 1e-12 {
                        continue;
                    }
                    let (ap0, ap1) = (ax - px, ay - py);
                    let t = (ap0 * d1 - ap1 * d0) / den;
                    let u = (ap0 * r1 - ap1 * r0) / den;
                    if t > 1e-6 && t < 1.0 - 1e-6 && u > 1e-6 && u < 1.0 - 1e-6 {
                        let si = arcs[i][k] + t * (arcs[i][k + 1] - arcs[i][k]);
                        let sj = arcs[j][q] + u * (arcs[j][q + 1] - arcs[j][q]);
                        if si.min(si_end - si).min(sj).min(sj_end - sj) > 1.0 {
                            out.push((i, j, si, sj));
                        }
                    }
                }
            }
        }
    }
    out
}

/// One point in a polygon (n, >=2), even-odd, as islands._inside / buildings._inside.
#[pyfunction]
fn point_inside(poly: PyReadonlyArray2<f64>, x: f64, y: f64) -> bool {
    let p = poly.as_array();
    let n = p.nrows();
    if n == 0 || p.ncols() < 2 {
        return false;
    }
    let mut c = false;
    let mut j = n - 1;
    for i in 0..n {
        let (xi, yi, xj, yj) = (p[[i, 0]], p[[i, 1]], p[[j, 0]], p[[j, 1]]);
        if (yi > y) != (yj > y) {
            let dy = if yj - yi == 0.0 { 1e-12 } else { yj - yi };
            if x < (xj - xi) * (y - yi) / dy + xi {
                c = !c;
            }
        }
        j = i;
    }
    c
}

/// geodesy.EnuFrame.geodetic_to_enu for 1-D lat, lon, h (degrees, metres): WGS84 to ECEF, then into the frame
/// (origin (3,) ECEF, rot (3, 3) rows E, N, U). -> (e, n, u).
#[pyfunction]
fn geodetic_to_enu<'py>(py: Python<'py>, lat: PyReadonlyArray1<'py, f64>, lon: PyReadonlyArray1<'py, f64>,
                        h: PyReadonlyArray1<'py, f64>, origin: PyReadonlyArray1<'py, f64>,
                        rot: PyReadonlyArray2<'py, f64>)
                        -> (Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>) {
    const A: f64 = 6378137.0;
    const F: f64 = 1.0 / 298.257223563;
    const E2: f64 = F * (2.0 - F);
    let (la, lo, hh, o, r) = (lat.as_array(), lon.as_array(), h.as_array(), origin.as_array(), rot.as_array());
    let n = la.len();
    let (e, nn, u) = release(py, n * 40, || {
    let (mut e, mut nn, mut u) = (Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n));
    for i in 0..n {
        let (phi, lam, hv) = (la[i].to_radians(), lo[i].to_radians(), hh[i]);
        let (sl, cl) = (phi.sin(), phi.cos());
        let nr = A / (1.0 - E2 * sl * sl).sqrt();
        let rr = (nr + hv) * cl;
        let x = rr * lam.cos() - o[0];
        let y = rr * lam.sin() - o[1];
        let z = (nr * (1.0 - E2) + hv) * sl - o[2];
        e.push(r[[0, 0]] * x + r[[0, 1]] * y + r[[0, 2]] * z);
        nn.push(r[[1, 0]] * x + r[[1, 1]] * y + r[[1, 2]] * z);
        u.push(r[[2, 0]] * x + r[[2, 1]] * y + r[[2, 2]] * z);
    }
    (e, nn, u)
    });
    (e.into_pyarray(py), nn.into_pyarray(py), u.into_pyarray(py))
}

/// geodesy.EnuFrame.enu_to_geodetic for point lists: ENU (e, n, u) -> ECEF through the frame (origin, rot rows =
/// E, N, U in ECEF) -> (lat, lon, h) by Heikkinen's closed form, the same float64 maths as geodesy.ecef_to_geodetic
/// (the polar axis included), without the GIL. The ground-ahead / far-tile threads made many small numpy calls.
#[pyfunction]
fn enu_to_geodetic<'py>(py: Python<'py>, e: PyReadonlyArray1<'py, f64>, n: PyReadonlyArray1<'py, f64>,
                        u: PyReadonlyArray1<'py, f64>, origin: PyReadonlyArray1<'py, f64>,
                        rot: PyReadonlyArray2<'py, f64>)
                        -> (Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>) {
    const A: f64 = 6378137.0;
    const F: f64 = 1.0 / 298.257223563;
    const B: f64 = A * (1.0 - F);
    const E2: f64 = F * (2.0 - F);
    const EP2: f64 = E2 / (1.0 - E2);
    let (ea, na, ua, o, r) = (e.as_array(), n.as_array(), u.as_array(), origin.as_array(), rot.as_array());
    let m = ea.len();
    let (lat, lon, hgt) = release(py, m * 60, || {
    let (mut lat, mut lon, mut hgt) = (Vec::with_capacity(m), Vec::with_capacity(m), Vec::with_capacity(m));
    for i in 0..m {
        let (ev, nv, uv) = (ea[i], na[i], ua[i]);
        let x = o[0] + r[[0, 0]] * ev + r[[1, 0]] * nv + r[[2, 0]] * uv;
        let y = o[1] + r[[0, 1]] * ev + r[[1, 1]] * nv + r[[2, 1]] * uv;
        let z = o[2] + r[[0, 2]] * ev + r[[1, 2]] * nv + r[[2, 2]] * uv;
        let p2 = x * x + y * y;
        let p = p2.sqrt();
        let z2 = z * z;
        let (la, h);
        if p < 1e-6 {
            la = if z >= 0.0 { 90.0 } else { -90.0 };
            h = z.abs() - B;
        } else {
            let f_ = 54.0 * B * B * z2;
            let g = p2 + (1.0 - E2) * z2 - E2 * (A * A - B * B);
            let c = E2 * E2 * f_ * p2 / (g * g * g);
            let s = (1.0 + c + (c * c + 2.0 * c).sqrt()).cbrt();
            let k = s + 1.0 + 1.0 / s;
            let pp = f_ / (3.0 * k * k * g * g);
            let q = (1.0 + 2.0 * E2 * E2 * pp).sqrt();
            let r0 = -pp * E2 * p / (1.0 + q)
                + (0.5 * A * A * (1.0 + 1.0 / q) - pp * (1.0 - E2) * z2 / (q * (1.0 + q)) - 0.5 * pp * p2).max(0.0).sqrt();
            let t = (p - E2 * r0) * (p - E2 * r0);
            let uu = (t + z2).sqrt();
            let vv = (t + (1.0 - E2) * z2).sqrt();
            let z0 = B * B * z / (A * vv);
            h = uu * (1.0 - B * B / (A * vv));
            la = (z + EP2 * z0).atan2(p).to_degrees();
        }
        lat.push(la);
        lon.push(y.atan2(x).to_degrees());
        hgt.push(h);
    }
    (lat, lon, hgt)
    });
    (lat.into_pyarray(py), lon.into_pyarray(py), hgt.into_pyarray(py))
}

/// bc7.encode: BC7 mode-6 blocks (16 bytes each, row-major) of an (h, w, 3|4) uint8 image, edges padded by
/// repetition. The same encoder as the numpy one: endpoints along the block's main axis (power iteration on the
/// RGBA covariance), 7-bit endpoints with the best p-bit, indices by projection onto the quantised endpoints.
const BC7_W: [f64; 16] = [0., 4., 9., 13., 17., 21., 26., 30., 34., 38., 43., 47., 51., 55., 60., 64.];

fn bc7_quantise(e: [f64; 4], opaque: bool) -> ([i32; 4], i32) {
    let mut best_q = [0i32; 4];
    let mut best_p = 0;
    let mut best_err = f64::INFINITY;
    for p in 0..2 {
        let mut q = [0i32; 4];
        let mut err = 0.0;
        for c in 0..4 {
            q[c] = ((e[c] - p as f64) / 2.0).round_ties_even().clamp(0.0, 127.0) as i32;
            err += (((q[c] << 1) | p) as f64 - e[c]).abs();
        }
        if err < best_err {
            best_q = q;
            best_p = p;
            best_err = err;
        }
    }
    if opaque {
        for c in 0..4 {
            best_q[c] = ((e[c] - 1.0) / 2.0).round_ties_even().clamp(0.0, 127.0) as i32;
        }
        best_q[3] = 127;
        best_p = 1;
    }
    (best_q, best_p)
}

fn bc7_block(t: &[[i32; 4]; 16]) -> [u8; 16] {
    let mut mean = [0f64; 4];
    for k in 0..16 {
        for c in 0..4 {
            mean[c] += t[k][c] as f64;
        }
    }
    for c in 0..4 {
        mean[c] /= 16.0;
    }
    let mut d = [[0f64; 4]; 16];
    for k in 0..16 {
        for c in 0..4 {
            d[k][c] = t[k][c] as f64 - mean[c];
        }
    }
    let mut cov = [[0f64; 4]; 4];
    for i in 0..4 {
        for j in 0..4 {
            let mut s = 0.0;
            for k in 0..16 {
                s += d[k][i] * d[k][j];
            }
            cov[i][j] = s;
        }
    }
    let mut v = [0f64; 4];
    let mut m = 0;
    for i in 1..4 {
        if cov[i][i] > cov[m][m] {
            m = i;
        }
    }
    v[m] = 1.0;
    for _ in 0..4 {
        let mut w = [0f64; 4];
        for i in 0..4 {
            for j in 0..4 {
                w[i] += cov[i][j] * v[j];
            }
        }
        let n = (w[0] * w[0] + w[1] * w[1] + w[2] * w[2] + w[3] * w[3]).sqrt().max(1e-9);
        for i in 0..4 {
            v[i] = w[i] / n;
        }
    }
    let (mut pmin, mut pmax) = (f64::INFINITY, f64::NEG_INFINITY);
    for k in 0..16 {
        let p = d[k][0] * v[0] + d[k][1] * v[1] + d[k][2] * v[2] + d[k][3] * v[3];
        pmin = pmin.min(p);
        pmax = pmax.max(p);
    }
    let opaque = t.iter().all(|x| x[3] == 255);
    let mut lo = [0f64; 4];
    let mut hi = [0f64; 4];
    for c in 0..4 {
        lo[c] = (mean[c] + v[c] * pmin).clamp(0.0, 255.0);
        hi[c] = (mean[c] + v[c] * pmax).clamp(0.0, 255.0);
    }
    let (mut q0, mut p0) = bc7_quantise(lo, opaque);
    let (mut q1, mut p1) = bc7_quantise(hi, opaque);
    let mut e0 = [0i32; 4];
    let mut e1 = [0i32; 4];
    for c in 0..4 {
        e0[c] = (q0[c] << 1) | p0;
        e1[c] = (q1[c] << 1) | p1;
    }
    let mut axis = [0f64; 4];
    let mut den = 0.0;
    for c in 0..4 {
        axis[c] = (e1[c] - e0[c]) as f64;
        den += axis[c] * axis[c];
    }
    let den = den.max(1e-9);
    let mut idx = [0u8; 16];
    for k in 0..16 {
        let mut dot = 0.0;
        for c in 0..4 {
            dot += (t[k][c] - e0[c]) as f64 * axis[c];
        }
        let f = dot / den * 64.0;
        let mut bi = 0;
        let mut be = f64::INFINITY;
        for (w, wv) in BC7_W.iter().enumerate() {
            let e = (f - wv).abs();
            if e < be {
                be = e;
                bi = w;
            }
        }
        idx[k] = bi as u8;
    }
    if idx[0] >= 8 {
        std::mem::swap(&mut q0, &mut q1);
        std::mem::swap(&mut p0, &mut p1);
        for k in 0..16 {
            idx[k] = 15 - idx[k];
        }
    }
    let mut bits: u128 = 1 << 6;
    let mut pos = 7;
    let mut put = |v: u128, n: u32, pos: &mut u32| {
        bits |= v << *pos;
        *pos += n;
    };
    for c in 0..4 {
        put(q0[c] as u128, 7, &mut pos);
        put(q1[c] as u128, 7, &mut pos);
    }
    put(p0 as u128, 1, &mut pos);
    put(p1 as u128, 1, &mut pos);
    put(idx[0] as u128, 3, &mut pos);
    for k in 1..16 {
        put(idx[k] as u128, 4, &mut pos);
    }
    bits.to_le_bytes()
}

#[pyfunction]
fn bc7_encode<'py>(py: Python<'py>, img: PyReadonlyArray3<'py, u8>) -> Bound<'py, PyBytes> {
    let a = img.as_array();
    let out = py.allow_threads(|| {
    let (h, w, ch) = (a.shape()[0], a.shape()[1], a.shape()[2]);
    if h == 0 || w == 0 {
        return Vec::new();
    }
    let (nby, nbx) = ((h + 3) / 4, (w + 3) / 4);
    let mut out = vec![0u8; nby * nbx * 16];
    let mut t = [[0i32; 4]; 16];
    for by in 0..nby {
        for bx in 0..nbx {
            for yy in 0..4 {
                let y = (by * 4 + yy).min(h - 1);
                for xx in 0..4 {
                    let x = (bx * 4 + xx).min(w - 1);
                    let k = yy * 4 + xx;
                    for c in 0..4 {
                        t[k][c] = if c < ch { a[[y, x, c]] as i32 } else { 255 };
                    }
                }
            }
            let blk = bc7_block(&t);
            let o = (by * nbx + bx) * 16;
            out[o..o + 16].copy_from_slice(&blk);
        }
    }
    out
    });
    PyBytes::new(py, &out)
}

/// roads.cut_outside's test: which points (n, 2) lie within half width hw[j] of a segment of blocker j. segs (m, 4)
/// ax ay bx by of all blockers one after another, starts (B + 1,) where each blocker's begin, hw (B,).
#[pyfunction]
fn inside_blockers<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, segs: PyReadonlyArray2<'py, f64>,
                        starts: PyReadonlyArray1<'py, i64>, hw: PyReadonlyArray1<'py, f64>)
                        -> Bound<'py, PyArray1<bool>> {
    let p = pts.as_array();
    let sg = segs.as_array();
    let st = starts.as_array();
    let h = hw.as_array();
    let n = p.nrows();
    let nb = h.len().min(st.len().saturating_sub(1));
    let mut out = vec![false; n];
    if n == 0 || nb == 0 || sg.nrows() == 0 || p.ncols() < 2 || sg.ncols() < 4 {
        return out.into_pyarray(py);
    }
    let mut boxes = Vec::with_capacity(nb);
    for b in 0..nb {
        let (s0, s1) = (st[b] as usize, st[b + 1] as usize);
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for k in s0..s1.min(sg.nrows()) {
            x0 = x0.min(sg[[k, 0]]).min(sg[[k, 2]]);
            x1 = x1.max(sg[[k, 0]]).max(sg[[k, 2]]);
            y0 = y0.min(sg[[k, 1]]).min(sg[[k, 3]]);
            y1 = y1.max(sg[[k, 1]]).max(sg[[k, 3]]);
        }
        boxes.push((x0 - h[b], y0 - h[b], x1 + h[b], y1 + h[b]));
    }
    for i in 0..n {
        let (x, y) = (p[[i, 0]], p[[i, 1]]);
        'blk: for b in 0..nb {
            let (bx0, by0, bx1, by1) = boxes[b];
            if x < bx0 || x > bx1 || y < by0 || y > by1 {
                continue;
            }
            for k in (st[b] as usize)..(st[b + 1] as usize).min(sg.nrows()) {
                let (ax, ay) = (sg[[k, 0]], sg[[k, 1]]);
                let (dx, dy) = (sg[[k, 2]] - ax, sg[[k, 3]] - ay);
                let l2 = (dx * dx + dy * dy).max(1e-12);
                let t = (((x - ax) * dx + (y - ay) * dy) / l2).clamp(0.0, 1.0);
                if (x - (ax + t * dx)).hypot(y - (ay + t * dy)) < h[b] {
                    out[i] = true;
                    break 'blk;
                }
            }
        }
    }
    out.into_pyarray(py)
}

/// roads.densify: nodes added so no two are more than spacing apart in xy (columns beyond x, y interpolated too).
#[pyfunction]
fn densify<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, spacing: f64) -> Bound<'py, PyArray2<f64>> {
    let p = pts.as_array();
    let (k, c) = (p.nrows(), p.ncols());
    let mut out: Vec<f64> = Vec::with_capacity(k * c * 2);
    if k > 0 {
        out.extend(p.row(0).iter());
    }
    for i in 0..k.saturating_sub(1) {
        let (a, b) = (p.row(i), p.row(i + 1));
        let n = if spacing > 0.0 && c >= 2 {
            ((b[0] - a[0]).hypot(b[1] - a[1]) / spacing).ceil() as i64
        } else {
            1
        };
        for s in 1..n {
            let f = s as f64 / n as f64;
            for j in 0..c {
                out.push(a[j] + (b[j] - a[j]) * f);
            }
        }
        out.extend(b.iter());
    }
    let rows = out.len() / c.max(1);
    numpy::ndarray::Array2::from_shape_vec((rows, c), out).unwrap().into_pyarray(py)
}

/// roadwidth.Walls.rays for one ray: the distance to the first wall from (px, py) along unit (dx, dy) within
/// reach, or NaN. keys / first / counts / seg_of: the Walls bucket index, segs (m, 4).
fn wall_ray<T: Copy + Into<f64>>(keys: &[i64], first: &[i64], counts: &[i64], seg_of: &[i64],
                                  segs: &numpy::ndarray::ArrayView2<T>, bucket: f64, px: f64, py: f64, dx: f64, dy: f64,
                                  reach: f64) -> f64 {
    let (qx, qy) = (px + dx * reach, py + dy * reach);
    let bx0 = (px.min(qx) / bucket).floor() as i64;
    let bx1 = (px.max(qx) / bucket).floor() as i64;
    let by0 = (py.min(qy) / bucket).floor() as i64;
    let by1 = (py.max(qy) / bucket).floor() as i64;
    let mut best = f64::INFINITY;
    for by in by0..=by1 {
        for bx in bx0..=bx1 {
            let key = (bx + (1i64 << 30)) * (1i64 << 31) + (by + (1i64 << 30));
            let Ok(i) = keys.binary_search(&key) else { continue };
            for j in first[i]..first[i] + counts[i] {
                let sgi = seg_of[j as usize] as usize;
                // (float32 walls widened as read: exactly the float64 numbers the arithmetic used before)
                let (ax, ay): (f64, f64) = (segs[[sgi, 0]].into(), segs[[sgi, 1]].into());
                let (ex, ey) = (Into::<f64>::into(segs[[sgi, 2]]) - ax, Into::<f64>::into(segs[[sgi, 3]]) - ay);
                let den = dx * ey - dy * ex;
                if den.abs() <= 1e-12 {
                    continue;
                }
                let (wx, wy) = (ax - px, ay - py);
                let t = (wx * ey - wy * ex) / den;
                let u = (wx * dy - wy * dx) / den;
                if t > 0.0 && t <= reach && u >= 0.0 && u <= 1.0 && t < best {
                    best = t;
                }
            }
        }
    }
    if best.is_finite() { best } else { f64::NAN }
}

/// numpy.interp at x of the increasing xp, fp.
fn np_interp(x: f64, xp: &[f64], fp: &[f64]) -> f64 {
    let n = xp.len().min(fp.len());
    if n == 0 {
        return f64::NAN;
    }
    if x <= xp[0] {
        return fp[0];
    }
    if x >= xp[n - 1] {
        return fp[n - 1];
    }
    // the last j with xp[j] <= x
    let j = match xp[..n].binary_search_by(|v| v.partial_cmp(&x).unwrap_or(std::cmp::Ordering::Less)) {
        Ok(mut k) => {
            while k + 1 < n && xp[k + 1] <= x {
                k += 1;
            }
            k
        }
        Err(k) => k.saturating_sub(1),
    };
    if j + 1 >= n {
        return fp[n - 1];
    }
    let slope = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j]);
    let mut r = slope * (x - xp[j]) + fp[j];
    if r.is_nan() {
        r = slope * (x - xp[j + 1]) + fp[j + 1];
        if r.is_nan() && fp[j] == fp[j + 1] {
            r = fp[j];
        }
    }
    r
}

/// numpy.percentile (linear) of sorted v at q (0..100).
fn np_percentile(v: &[f64], q: f64) -> f64 {
    let n = v.len();
    if n == 0 {
        return f64::NAN;
    }
    let idx = q / 100.0 * (n as f64 - 1.0);
    let lo = idx.floor() as usize;
    let hi = (lo + 1).min(n - 1);
    let g = idx - lo as f64;
    // numpy's _lerp: a + (b - a) * t, and b - (b - a) * (1 - t) where t >= 0.5
    let (a, b) = (v[lo], v[hi]);
    let d = b - a;
    if g >= 0.5 { b - d * (1.0 - g) } else { a + d * g }
}

/// roadwidth.measure_row: the right-of-way between the building walls of a street (centreline (k, >=2)), or None.
#[allow(clippy::too_many_arguments)]
fn measure_row_any<T: Copy + Into<f64> + numpy::Element>(
        line: PyReadonlyArray2<f64>, keys: PyReadonlyArray1<i64>, first: PyReadonlyArray1<i64>,
        counts: PyReadonlyArray1<i64>, seg_of: PyReadonlyArray1<i64>, segs: PyReadonlyArray2<T>, bucket: f64,
        step: f64, end_skip: f64, reach: f64, min_samples: usize, walled: f64, agree: f64) -> Option<f64> {
    let l = line.as_array();
    let (keys, first, counts, seg_of) = (keys.as_slice().unwrap(), first.as_slice().unwrap(),
                                         counts.as_slice().unwrap(), seg_of.as_slice().unwrap());
    let sg = segs.as_array();
    let k = l.nrows();
    if k < 2 || l.ncols() < 2 || keys.is_empty() || !(step > 0.0) || !(bucket > 0.0) {
        return None;
    }
    let xs: Vec<f64> = (0..k).map(|i| l[[i, 0]]).collect();
    let ys: Vec<f64> = (0..k).map(|i| l[[i, 1]]).collect();
    let mut s = vec![0.0f64; k];
    for i in 1..k {
        s[i] = s[i - 1] + (xs[i] - xs[i - 1]).hypot(ys[i] - ys[i - 1]);
    }
    let total = s[k - 1];
    if !(total >= 2.0 * end_skip + step) {
        return None;
    }
    // numpy.arange(end_skip, total - end_skip + 1e-9, step)
    let stop = total - end_skip + 1e-9;
    let count = ((stop - end_skip) / step).ceil().max(0.0) as usize;
    let mut w = Vec::with_capacity(count);
    let mut n = 0usize;
    for i in 0..count {
        let dd = end_skip + i as f64 * step;
        // searchsorted(s, dd) (left), clipped to 1..k-1
        let mut j = s.partition_point(|&v| v < dd);
        j = j.clamp(1, k - 1);
        let (tx, ty) = (xs[j] - xs[j - 1], ys[j] - ys[j - 1]);
        let len = tx.hypot(ty);
        if !(len >= 1e-6) {
            continue;
        }
        n += 1;
        let (nx, ny) = (-ty / len, tx / len);
        let (cx, cy) = (np_interp(dd, &s, &xs), np_interp(dd, &s, &ys));
        let a = wall_ray(keys, first, counts, seg_of, &sg, bucket, cx, cy, nx, ny, reach);
        let b = wall_ray(keys, first, counts, seg_of, &sg, bucket, cx, cy, -nx, -ny, reach);
        let v = a + b;
        if !v.is_nan() {
            w.push(v);
        }
    }
    if n == 0 || w.len() < min_samples || (w.len() as f64) / (n as f64) < walled {
        return None;
    }
    w.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let m = w.len();
    let med = if m % 2 == 1 { w[m / 2] } else { (w[m / 2 - 1] + w[m / 2]) / 2.0 };
    let (q1, q3) = (np_percentile(&w, 25.0), np_percentile(&w, 75.0));
    if !(med > 0.0) || (q3 - q1) / med > agree {
        return None;
    }
    Some(med)
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn measure_row(line: PyReadonlyArray2<f64>, keys: PyReadonlyArray1<i64>, first: PyReadonlyArray1<i64>,
               counts: PyReadonlyArray1<i64>, seg_of: PyReadonlyArray1<i64>, segs: PyReadonlyArray2<f64>,
               bucket: f64, step: f64, end_skip: f64, reach: f64, min_samples: usize, walled: f64, agree: f64)
               -> Option<f64> {
    measure_row_any(line, keys, first, counts, seg_of, segs, bucket, step, end_skip, reach, min_samples, walled, agree)
}

/// measure_row with the walls as float32 (roadwidth keeps them as the shared memory-mapped file: one copy for every
/// server process instead of a float64 copy in each).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn measure_row32(line: PyReadonlyArray2<f64>, keys: PyReadonlyArray1<i64>, first: PyReadonlyArray1<i64>,
                 counts: PyReadonlyArray1<i64>, seg_of: PyReadonlyArray1<i64>, segs: PyReadonlyArray2<f32>,
                 bucket: f64, step: f64, end_skip: f64, reach: f64, min_samples: usize, walled: f64, agree: f64)
                 -> Option<f64> {
    measure_row_any(line, keys, first, counts, seg_of, segs, bucket, step, end_skip, reach, min_samples, walled, agree)
}

/// grading.grade_points: base heights at the points (px, py) graded under the plans' segments a-b (heights za-zb,
/// core half width h): the weights, the inverse-distance blend and the max weight as the numpy code, the (segment,
/// point) pairs in its order (segments, bucket rows, points sorted by bucket).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn grade_points<'py>(py: Python<'py>, px: PyReadonlyArray1<'py, f64>, pyy: PyReadonlyArray1<'py, f64>,
                     base: PyReadonlyArray1<'py, f64>, a: PyReadonlyArray2<'py, f64>, b: PyReadonlyArray2<'py, f64>,
                     za: PyReadonlyArray1<'py, f64>, zb: PyReadonlyArray1<'py, f64>, h: PyReadonlyArray1<'py, f64>,
                     lift: f64, slope_run: f64, t_min: f64, t_max: f64, blend_r: f64, bucket: f64,
                     wall_cut: f64, wall_t: f64, wall_flat: f64, wall_ahead: f64)
                     -> Bound<'py, PyArray1<f64>> {
    let (px, pyy, base) = (px.as_array(), pyy.as_array(), base.as_array());
    let (a, b, za, zb, h) = (a.as_array(), b.as_array(), za.as_array(), zb.as_array(), h.as_array());
    let n = px.len().min(pyy.len()).min(base.len());
    let mut out: Vec<f64> = base.iter().copied().collect();
    if n == 0 || h.is_empty() || !(bucket > 0.0) {
        return out.into_pyarray(py);
    }
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for i in 0..n {
        x0 = x0.min(px[i]); x1 = x1.max(px[i]);
        y0 = y0.min(pyy[i]); y1 = y1.max(pyy[i]);
    }
    if !x0.is_finite() || !y0.is_finite() {
        return out.into_pyarray(py);
    }
    let bxs: Vec<i64> = (0..n).map(|i| ((px[i] - x0) / bucket).floor() as i64).collect();
    let bys: Vec<i64> = (0..n).map(|i| ((pyy[i] - y0) / bucket).floor() as i64).collect();
    let nbx = bxs.iter().copied().max().unwrap_or(0) + 1;
    let keys: Vec<i64> = (0..n).map(|i| bys[i] * nbx + bxs[i]).collect();
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by_key(|&i| keys[i]);                    // stable, as argsort(kind="stable")
    let sk: Vec<i64> = order.iter().map(|&i| keys[i]).collect();
    let mut num = vec![0.0f64; n];
    let mut den = vec![0.0f64; n];
    let mut wmax = vec![0.0f64; n];
    for s in 0..h.len() {
        let reach = h[s] + t_max;
        let (ax, ay, bx, by) = (a[[s, 0]], a[[s, 1]], b[[s, 0]], b[[s, 1]]);
        let (lo_x, hi_x) = (ax.min(bx) - reach, ax.max(bx) + reach);
        let (lo_y, hi_y) = (ay.min(by) - reach, ay.max(by) + reach);
        if hi_x < x0 || lo_x > x1 || hi_y < y0 || lo_y > y1 {
            continue;
        }
        let cx0 = 0i64.max(((lo_x - x0) / bucket).floor() as i64);
        let cx1 = (nbx - 1).min(((hi_x - x0) / bucket).floor() as i64);
        let cy0 = 0i64.max(((lo_y - y0) / bucket).floor() as i64);
        let cy1 = ((hi_y - y0) / bucket).floor() as i64;
        let (abx, aby) = (bx - ax, by - ay);
        let l2 = abx * abx + aby * aby;
        for row in cy0..=cy1 {
            let i0 = sk.partition_point(|&k| k < row * nbx + cx0);
            let i1 = sk.partition_point(|&k| k <= row * nbx + cx1);
            for j in i0..i1.max(i0) {
                let idx = order[j];
                let (qx, qy) = (px[idx] - ax, pyy[idx] - ay);
                let t = if l2 > 1e-12 { ((qx * abx + qy * aby) / l2).clamp(0.0, 1.0) } else { 0.0 };
                let d = (qx - t * abx).hypot(qy - t * aby);
                let target = za[s] + (zb[s] - za[s]) * t;
                // a cut deeper than wall_cut: a retaining wall at the core's edge, the ground right behind it
                // (for a point more beside the segment than ahead of it: ahead, the road's own next segments blend
                // in along it, and cutting those off made a kink on every crest)
                let side = if l2 > 1e-12 { (qx * aby - qy * abx).abs() / l2.sqrt() } else { d };
                let ahead = if l2 > 1e-12 {
                    let along = (qx * abx + qy * aby) / l2.sqrt();
                    (-along).max(along - l2.sqrt()).max(0.0)
                } else { 0.0 };
                // (and not far past a road's end: beside a tunnel the approach's last segment flattened a strip
                // 10 m along it, a pit beside the arch, 2026-10-17)
                let wall = base[idx] - target > wall_cut && ahead < side && ahead < wall_ahead;
                let tt = if wall { wall_t } else { (slope_run * (target - base[idx]).abs()).clamp(t_min, t_max) };
                // a wall: flat on one terrain step behind it (no slope in front of its face), then up
                let u = ((d - h[s] - if wall { wall_flat } else { 0.0 }) / tt).clamp(0.0, 1.0);
                let w = 1.0 - u * u * (3.0 - 2.0 * u);
                if w > 0.0 {
                    let r = d / blend_r;
                    let wb = w / (1.0 + r * r);
                    num[idx] += wb * target;
                    den[idx] += wb;
                    if w > wmax[idx] {
                        wmax[idx] = w;
                    }
                }
            }
        }
    }
    for i in 0..n {
        if den[i] > 0.0 {
            out[i] = base[i] + wmax[i] * (num[i] / den[i] - base[i] + lift);
        }
    }
    out.into_pyarray(py)
}

/// roads.tunnel_sections: a tunnel's road level and its sections from the ground over it, sampled at arc lengths s.
/// The floor: the lower convex hull of (s, g) (the road's level under the covered parts, the ground where it lies
/// open). Each run of samples with cover (g - floor) of at least open_cover is one section: kind 2 (a full tunnel)
/// when its cover reaches full_cover, else 1 (a low arch); the rest 0 (open road). Covered runs of fewer than min_n
/// samples are open, open gaps of fewer than min_n between covered runs covered (the deeper kind). (floor, kinds)
#[pyfunction]
fn tunnel_sections<'py>(py: Python<'py>, s: PyReadonlyArray1<'py, f64>, g: PyReadonlyArray1<'py, f64>,
                        open_cover: f64, full_cover: f64, min_n: usize)
                        -> (Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<i64>>) {
    let (s, g) = (s.as_array(), g.as_array());
    let n = s.len().min(g.len());
    let mut floor = vec![0.0f64; n];
    let mut kinds = vec![0i64; n];
    if n == 0 {
        return (floor.into_pyarray(py), kinds.into_pyarray(py));
    }
    // the lower hull (monotone chain over s ascending)
    let mut hull: Vec<usize> = Vec::new();
    for i in 0..n {
        while hull.len() >= 2 {
            let (a, b) = (hull[hull.len() - 2], hull[hull.len() - 1]);
            let cross = (s[b] - s[a]) * (g[i] - g[a]) - (g[b] - g[a]) * (s[i] - s[a]);
            if cross <= 0.0 { hull.pop(); } else { break; }
        }
        hull.push(i);
    }
    let mut k = 0usize;
    for i in 0..n {
        while k + 1 < hull.len().saturating_sub(1) && s[hull[k + 1]] <= s[i] { k += 1; }
        let (a, b) = (hull[k], hull[(k + 1).min(hull.len() - 1)]);
        floor[i] = if b == a || s[b] <= s[a] { g[a] } else { g[a] + (g[b] - g[a]) * (s[i] - s[a]) / (s[b] - s[a]) };
    }
    // covered runs and their kinds
    let covered: Vec<bool> = (0..n).map(|i| g[i] - floor[i] >= open_cover).collect();
    let mut runs: Vec<(usize, usize, i64)> = Vec::new();
    let mut i = 0usize;
    while i < n {
        if !covered[i] { i += 1; continue; }
        let mut j = i;
        while j + 1 < n && covered[j + 1] { j += 1; }
        let deep = (i..=j).any(|q| g[q] - floor[q] >= full_cover);
        if j + 1 - i >= min_n { runs.push((i, j, if deep { 2 } else { 1 })); }
        i = j + 1;
    }
    let mut merged: Vec<(usize, usize, i64)> = Vec::new();
    for r in runs {
        if let Some(last) = merged.last_mut() {
            if r.0 - last.1 - 1 < min_n {
                last.1 = r.1;
                last.2 = last.2.max(r.2);
                continue;
            }
        }
        merged.push(r);
    }
    for (a, b, kind) in merged {
        for q in a..=b { kinds[q] = kind; }
    }
    (floor.into_pyarray(py), kinds.into_pyarray(py))
}

/// grading.resample: points every step m along xy (k, 2), the last point kept, and their arc lengths.
#[pyfunction]
fn resample<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, step: f64)
                 -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<f64>>) {
    let p = xy.as_array();
    let k = p.nrows();
    if k == 0 || p.ncols() < 2 {
        let a = numpy::ndarray::Array2::from_shape_vec((0, 2), Vec::new()).unwrap();
        return (a.into_pyarray(py), Vec::<f64>::new().into_pyarray(py));
    }
    let xs: Vec<f64> = (0..k).map(|i| p[[i, 0]]).collect();
    let ys: Vec<f64> = (0..k).map(|i| p[[i, 1]]).collect();
    let mut s = vec![0.0f64; k];
    for i in 1..k {
        s[i] = s[i - 1] + (xs[i] - xs[i - 1]).hypot(ys[i] - ys[i - 1]);
    }
    let total = s[k - 1];
    if !(total > 0.0) || !(step > 0.0) {
        let a = numpy::ndarray::Array2::from_shape_vec((1, 2), vec![xs[0], ys[0]]).unwrap();
        return (a.into_pyarray(py), vec![0.0f64].into_pyarray(py));
    }
    let count = (total / step).ceil() as usize;          // numpy.arange(0, total, step)
    let mut t: Vec<f64> = (0..count).map(|i| i as f64 * step).collect();
    t.push(total);
    let mut out = Vec::with_capacity(t.len() * 2);
    for &v in &t {
        out.push(np_interp(v, &s, &xs));
        out.push(np_interp(v, &s, &ys));
    }
    let a = numpy::ndarray::Array2::from_shape_vec((t.len(), 2), out).unwrap();
    (a.into_pyarray(py), t.into_pyarray(py))
}

/// grading.smooth_profile: Gaussian smoothing of a profile sampled every step m (reflected at the ends, edge-padded
/// when shorter than the kernel's radius).
#[pyfunction]
fn smooth_profile<'py>(py: Python<'py>, z: PyReadonlyArray1<'py, f64>, step: f64, sigma: f64)
                       -> Bound<'py, PyArray1<f64>> {
    let z = z.as_array();
    let n = z.len();
    if n < 3 || !(sigma > 0.0) || !(step > 0.0) {
        return z.to_vec().into_pyarray(py);
    }
    let r = (3.0 * sigma / step).ceil() as usize;
    let mut k: Vec<f64> = (0..=2 * r).map(|i| {
        let x = (i as f64 - r as f64) * step / sigma;
        (-0.5 * (x * x)).exp()
    }).collect();
    let sum: f64 = k.iter().sum();
    for v in k.iter_mut() {
        *v /= sum;
    }
    let reflect = n > r;
    let at = |i: isize| -> f64 {
        let m = n as isize;
        if i < 0 {
            if reflect { z[(-i) as usize] } else { z[0] }
        } else if i >= m {
            if reflect { z[(2 * (m - 1) - i) as usize] } else { z[n - 1] }
        } else {
            z[i as usize]
        }
    };
    let w = 2 * r + 1;
    let out: Vec<f64> = (0..n).map(|j| {
        // numpy.convolve(pad, k, "valid"): out[j] = sum_i pad[j + i] * k[w - 1 - i]
        let mut acc = 0.0;
        for i in 0..w {
            acc += at(j as isize + i as isize - r as isize) * k[w - 1 - i];
        }
        acc
    }).collect();
    out.into_pyarray(py)
}

/// roads._seg_dist_min: the distance from (px, py) to the polyline line (k, >= 2).
#[pyfunction]
fn seg_dist_min(line: PyReadonlyArray2<f64>, px: f64, py: f64) -> f64 {
    let l = line.as_array();
    let mut best = f64::INFINITY;
    for i in 0..l.nrows().saturating_sub(1) {
        let (ax, ay) = (l[[i, 0]], l[[i, 1]]);
        let (dx, dy) = (l[[i + 1, 0]] - ax, l[[i + 1, 1]] - ay);
        let l2 = (dx * dx + dy * dy).max(1e-12);
        let t = (((px - ax) * dx + (py - ay) * dy) / l2).clamp(0.0, 1.0);
        let d = (ax + dx * t - px).hypot(ay + dy * t - py);
        if d < best {
            best = d;
        }
    }
    best
}

/// polygon.contains_points: which points (m, 2) lie inside the ring (n, 2) (even-odd crossings of a ray toward +x:
/// an edge counts when lo <= y < hi and it crosses y right of the point), with its horizontal bands.
#[pyfunction]
fn contains_points<'py>(py: Python<'py>, ring: PyReadonlyArray2<'py, f64>, pts: PyReadonlyArray2<'py, f64>)
                        -> Bound<'py, PyArray1<bool>> {
    let r = ring.as_array();
    let p = pts.as_array();
    let (n, m) = (r.nrows(), p.nrows());
    let mut out = vec![false; m];
    if n < 3 || m == 0 {
        return out.into_pyarray(py);
    }
    let (mut ex0, mut ey0, mut elo, mut ehi, mut esl) = (vec![], vec![], vec![], vec![], vec![]);
    for i in 0..n {
        let (x0, y0) = (r[[i, 0]], r[[i, 1]]);
        let (x1, y1) = (r[[(i + 1) % n, 0]], r[[(i + 1) % n, 1]]);
        if y0 == y1 {
            continue;
        }
        ex0.push(x0);
        ey0.push(y0);
        elo.push(y0.min(y1));
        ehi.push(y0.max(y1));
        esl.push((x1 - x0) / (y1 - y0));
    }
    let ne = ex0.len();
    if ne == 0 {
        return out.into_pyarray(py);
    }
    let (mut bx0, mut bx1, mut by0, mut by1) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
    for i in 0..n {
        bx0 = bx0.min(r[[i, 0]]); bx1 = bx1.max(r[[i, 0]]);
        by0 = by0.min(r[[i, 1]]); by1 = by1.max(r[[i, 1]]);
    }
    let nb = 512usize.min(1usize.max(ne / 4));
    let mut h = (by1 - by0) / nb as f64;
    if h == 0.0 {
        h = 1.0;
    }
    let clip = |v: f64| -> usize { (v as i64).clamp(0, nb as i64 - 1) as usize };
    let mut bands: Vec<Vec<usize>> = vec![Vec::new(); nb];
    for e in 0..ne {
        let (b0, b1) = (clip((elo[e] - by0) / h), clip((ehi[e] - by0) / h));
        for b in b0..=b1 {
            bands[b].push(e);
        }
    }
    for j in 0..m {
        let (x, y) = (p[[j, 0]], p[[j, 1]]);
        if !(x >= bx0 && x <= bx1 && y >= by0 && y <= by1) {
            continue;
        }
        let b = (((y - by0) / h) as i64).min(nb as i64 - 1) as usize;
        let mut c = 0u32;
        for &e in &bands[b] {
            if y >= elo[e] && y < ehi[e] && ex0[e] + (y - ey0[e]) * esl[e] > x {
                c += 1;
            }
        }
        out[j] = c & 1 == 1;
    }
    out.into_pyarray(py)
}

fn rf_hash3(i: i64, j: i64, k: i64) -> f64 {
    let mut x = (i as u64).wrapping_mul(0x9E3779B97F4A7C15) ^ (j as u64).wrapping_mul(0xC2B2AE3D27D4EB4F);
    x ^= (k as u64).wrapping_mul(0x165667B19E3779F9);
    x ^= x >> 31;
    x = x.wrapping_mul(0xBF58476D1CE4E5B9);
    x ^= x >> 29;
    (x >> 11) as f64 / (1u64 << 53) as f64
}

fn rf_noise3(x: f64, y: f64, z: f64) -> f64 {
    let (xi, yi, zi) = (x.floor(), y.floor(), z.floor());
    let s = |f: f64| f * f * (3.0 - 2.0 * f);
    let (fx, fy, fz) = (s(x - xi), s(y - yi), s(z - zi));
    let (xi, yi, zi) = (xi as i64, yi as i64, zi as i64);
    let mut out = 0.0;
    for dx in 0..2 {
        let wx = if dx == 1 { fx } else { 1.0 - fx };
        for dy in 0..2 {
            let wy = if dy == 1 { fy } else { 1.0 - fy };
            for dz in 0..2 {
                let wz = if dz == 1 { fz } else { 1.0 - fz };
                out = out + wx * wy * wz * rf_hash3(xi + dx, yi + dy, zi + dz);
            }
        }
    }
    out
}

/// rockface.rock: the tile photo rgb (px, px, 3) with its steep texels drawn toward rock, or None when nothing is
/// steep. U: the tile's height grid [j, i], origin (ux0, uy0), spacing usp. steep_a / steep_b: the slopes
/// (rise / run) of STEEP_DEG; octaves (k, 2) (scale m, weight); m_lat / m_lon: metres per degree.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn rock<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, u: PyReadonlyArray2<'py, f64>, x0: f64, y0: f64,
             size: f64, ux0: f64, uy0: f64, usp: f64, lat: f64, lon: f64, m_lat: f64, m_lon: f64, steep_a: f64,
             steep_b: f64, octaves: PyReadonlyArray2<'py, f64>, strata_m: f64, strata_w: f64,
             tint: (f32, f32, f32), desat: f64, darken: f64) -> Option<Bound<'py, numpy::PyArray3<u8>>> {
    let img = rgb.as_array();
    let u = u.as_array();
    let oct = octaves.as_array();
    let res = py.allow_threads(|| {
    let px = img.shape()[0];
    let (ny, nx) = (u.nrows(), u.ncols());
    if px == 0 || ny < 2 || nx < 2 || !(size > 0.0) || !(usp > 0.0) {
        return None;
    }
    let t = size / px as f64;
    let c: Vec<f64> = (0..px).map(|k| (k as f64 + 0.5) * t).collect();
    let xs: Vec<f64> = c.iter().map(|v| x0 + v).collect();
    let ys: Vec<f64> = c.iter().map(|v| y0 + size - v).collect();
    let fi: Vec<(usize, f64)> = xs.iter().map(|x| {
        let f = ((x - ux0) / usp).clamp(0.0, nx as f64 - 1.000001);
        let i0 = f.floor();
        (i0 as usize, f - i0)
    }).collect();
    let fj: Vec<(usize, f64)> = ys.iter().map(|y| {
        let f = ((y - uy0) / usp).clamp(0.0, ny as f64 - 1.000001);
        let j0 = f.floor();
        (j0 as usize, f - j0)
    }).collect();
    let mut h = vec![0.0f64; px * px];
    for j in 0..px {
        let (j0, wj) = fj[j];
        for i in 0..px {
            let (i0, wi) = fi[i];
            h[j * px + i] = (u[[j0, i0]] * (1.0 - wi) + u[[j0, i0 + 1]] * wi) * (1.0 - wj)
                + (u[[j0 + 1, i0]] * (1.0 - wi) + u[[j0 + 1, i0 + 1]] * wi) * wj;
        }
    }
    // numpy.gradient(H, t): central inside, one-sided at the edges
    let grad = |a: usize, b: usize, n: usize, f: &dyn Fn(usize) -> f64| -> f64 {
        let _ = b;
        if n < 2 {
            0.0
        } else if a == 0 {
            (f(1) - f(0)) / t
        } else if a == n - 1 {
            (f(n - 1) - f(n - 2)) / t
        } else {
            (f(a + 1) - f(a - 1)) / (2.0 * t)
        }
    };
    let mut slope = vec![0.0f64; px * px];
    let mut kk = vec![0.0f64; px * px];
    let mut any = false;
    for j in 0..px {
        for i in 0..px {
            let gx = grad(i, 0, px, &|q| h[j * px + q]);
            let gy = grad(j, 0, px, &|q| h[q * px + i]);
            let sl = gx.hypot(gy);
            let tt = ((sl - steep_a) / (steep_b - steep_a)).clamp(0.0, 1.0);
            let k = tt * tt * (3.0 - 2.0 * tt);
            slope[j * px + i] = sl;
            kk[j * px + i] = k;
            if k > 0.01 {
                any = true;
            }
        }
    }
    if !any {
        return None;
    }
    let wsum: f64 = (0..oct.nrows()).map(|o| oct[[o, 1]]).sum();
    let mut out = img.to_owned();
    let (one_m_desat, desat32, darken32) = ((1.0 - desat) as f32, desat as f32, darken as f32);
    let lw = (0.299f32, 0.587f32, 0.114f32);
    for j in 0..px {
        for i in 0..px {
            let q = j * px + i;
            let k = kk[q];
            if !(k > 0.01) {
                continue;
            }
            let (hs, sl) = (h[q], slope[q]);
            let ex = lon * m_lon + xs[i];
            let nyv = lat * m_lat + ys[j];
            let foot = t * sl.max(1.0);
            let mut n = 0.0f64;
            for o in 0..oct.nrows() {
                let (scale, w) = (oct[[o, 0]], oct[[o, 1]]);
                let a = (1.0 - 1.5 * foot / scale).clamp(0.0, 1.0) * w;
                if a > 0.0 {
                    n += a * (rf_noise3(ex / scale, nyv / scale, hs / scale) - 0.5);
                }
            }
            n /= wsum;
            let sa = (1.0 - 1.5 * foot / strata_m).clamp(0.0, 1.0);
            let strata = sa * (2.0 * std::f64::consts::PI * hs / strata_m + 3.0 * n).sin() * strata_w;
            let f = [img[[j, i, 0]] as f32 / 255.0, img[[j, i, 1]] as f32 / 255.0, img[[j, i, 2]] as f32 / 255.0];
            let lum = f[0] * lw.0 + f[1] * lw.1 + f[2] * lw.2;
            let tints = [tint.0, tint.1, tint.2];
            let mult = 1.0 + 0.55 * n + strata;
            for ch in 0..3 {
                let rockc = (f[ch] * one_m_desat + lum * desat32) * tints[ch] * darken32;
                let rc = rockc as f64 * mult;
                let mixed = f[ch] as f64 * (1.0 - k) + rc * k;
                out[[j, i, ch]] = (mixed * 255.0).round_ties_even().clamp(0.0, 255.0) as u8;
            }
        }
    }
    Some(out)
    });
    res.map(|o| o.into_pyarray(py))
}

/// The byte of imagery._to_srgb for a float32 in [0, 1]: rint(x ** (1 / 2.2) * 255).
fn srgb_byte(x: f32) -> u8 {
    let inv = (1.0f64 / 2.2) as f32;
    (x.powf(inv) * 255.0f32).round_ties_even().clamp(0.0, 255.0) as u8
}

/// For k = 1..255 the smallest float32 in [0, 1] whose srgb_byte is >= k (the byte of x is then the number of
/// thresholds <= x; srgb_byte never decreases in x, checked over every float32 in [0, 1] by srgb_monotonic).
fn srgb_thresholds() -> &'static [f32; 255] {
    static T: std::sync::OnceLock<[f32; 255]> = std::sync::OnceLock::new();
    T.get_or_init(|| {
        let mut t = [0f32; 255];
        for k in 1..=255u32 {
            let (mut lo, mut hi) = (0u32, 1.0f32.to_bits());     // positive floats order as their bits
            while lo < hi {
                let mid = lo + (hi - lo) / 2;
                if (srgb_byte(f32::from_bits(mid)) as u32) >= k { hi = mid } else { lo = mid + 1 }
            }
            t[(k - 1) as usize] = f32::from_bits(lo);
        }
        t
    })
}

/// Whether srgb_byte never decreases over every float32 in [0, 1] (the thresholds' premise; a test calls it).
#[pyfunction]
fn srgb_monotonic() -> bool {
    let mut prev = 0u8;
    for b in 0..=1.0f32.to_bits() {
        let v = srgb_byte(f32::from_bits(b));
        if v < prev {
            return false;
        }
        prev = v;
    }
    true
}

/// imagery.mip_arrays below level 0: the levels 1.. (uint8 [h, w, 3]) of a power-of-two image rgb: linear-light
/// (lut: the 256 float32 values of _TO_LIN) 2 x 2 averages, each from the previous unsharpened one, then sharpened
/// by sharpen (3 x 3 box, edge-padded) where both sides are >= 4, back to sRGB (x ** (1 / 2.2)), as the numpy code
/// in float32.
#[pyfunction]
fn mip_levels<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, lut: PyReadonlyArray1<'py, f32>, sharpen: f32)
                   -> Vec<Bound<'py, numpy::PyArray3<u8>>> {
    let img = rgb.as_array();
    let lut = lut.as_array();
    let levels = py.allow_threads(|| {
    let (mut h, mut w) = (img.shape()[0], img.shape()[1]);
    let mut lin: Vec<f32> = match img.as_slice() {
        Some(sl) => sl.iter().map(|&v| lut[v as usize]).collect(),
        None => img.iter().map(|&v| lut[v as usize]).collect(),
    };
    let mut out = Vec::new();
    while h.max(w) > 1 {
        let (mut nh, mut nw) = (h, w);
        if h > 1 {
            nh = h / 2;
            let mut nl = vec![0f32; nh * w * 3];
            for y in 0..nh {
                for x in 0..w * 3 {
                    nl[y * w * 3 + x] = 0.5f32 * (lin[(2 * y) * w * 3 + x] + lin[(2 * y + 1) * w * 3 + x]);
                }
            }
            lin = nl;
        }
        if w > 1 {
            nw = w / 2;
            let mut nl = vec![0f32; nh * nw * 3];
            for y in 0..nh {
                for x in 0..nw {
                    for c in 0..3 {
                        nl[(y * nw + x) * 3 + c] =
                            0.5f32 * (lin[(y * w + 2 * x) * 3 + c] + lin[(y * w + 2 * x + 1) * 3 + c]);
                    }
                }
            }
            lin = nl;
        }
        h = nh;
        w = nw;
        let lev: Vec<f32> = if h.min(w) >= 4 && sharpen > 0.0 {
            let at = |y: isize, x: isize, c: usize| -> f32 {
                let yy = y.clamp(0, h as isize - 1) as usize;
                let xx = x.clamp(0, w as isize - 1) as usize;
                lin[(yy * w + xx) * 3 + c]
            };
            let mut l2 = vec![0f32; h * w * 3];
            let row = w * 3;
            // the interior: no clamping (the same sum, term by term)
            for y in 1..h - 1 {
                for x in 1..w - 1 {
                    for c in 0..3 {
                        let q = y * row + x * 3 + c;
                        let blur = (lin[q - row - 3] + lin[q - row] + lin[q - row + 3] + lin[q - 3] + lin[q]
                            + lin[q + 3] + lin[q + row - 3] + lin[q + row] + lin[q + row + 3]) / 9.0f32;
                        l2[q] = lin[q] + sharpen * (lin[q] - blur);
                    }
                }
            }
            for y in 0..h as isize {
                for x in 0..w as isize {
                    if y > 0 && y < h as isize - 1 && x > 0 && x < w as isize - 1 {
                        continue;
                    }
                    for c in 0..3 {
                        // the numpy sum's order: rows top to bottom, each left to right
                        let blur = (at(y - 1, x - 1, c) + at(y - 1, x, c) + at(y - 1, x + 1, c) + at(y, x - 1, c)
                            + at(y, x, c) + at(y, x + 1, c) + at(y + 1, x - 1, c) + at(y + 1, x, c)
                            + at(y + 1, x + 1, c)) / 9.0f32;
                        let v = at(y, x, c);
                        l2[((y as usize) * w + x as usize) * 3 + c] = v + sharpen * (v - blur);
                    }
                }
            }
            l2
        } else {
            lin.clone()
        };
        let th = srgb_thresholds();
        let bytes: Vec<u8> = lev.iter().map(|&v| {
            let c = v.clamp(0.0, 1.0);
            th.partition_point(|&t| t <= c) as u8      // the byte the formula gives (monotonic: thresholds)
        }).collect();
        out.push(numpy::ndarray::Array3::from_shape_vec((h, w, 3), bytes).unwrap());
    }
    out
    });
    levels.into_iter().map(|a| a.into_pyarray(py)).collect()
}

fn polyline_min_dist(l: &numpy::ndarray::ArrayView2<f64>, px: f64, py: f64) -> f64 {
    let mut best = f64::INFINITY;
    for i in 0..l.nrows().saturating_sub(1) {
        let (ax, ay) = (l[[i, 0]], l[[i, 1]]);
        let (dx, dy) = (l[[i + 1, 0]] - ax, l[[i + 1, 1]] - ay);
        let l2 = (dx * dx + dy * dy).max(1e-12);
        let t = (((px - ax) * dx + (py - ay) * dy) / l2).clamp(0.0, 1.0);
        let d = (ax + dx * t - px).hypot(ay + dy * t - py);
        if d < best {
            best = d;
        }
    }
    best
}

/// islands._crosses: whether road pts (k, >= 2) runs through the inside of the loop poly (n, 2): its samples every
/// ~2 m inside the loop (even-odd) and farther than margin from the loop's closed line.
#[pyfunction]
fn loop_crossed(poly: PyReadonlyArray2<f64>, pts: PyReadonlyArray2<f64>, margin: f64) -> bool {
    let pl = poly.as_array();
    let p = pts.as_array();
    let n = pl.nrows();
    if n == 0 || p.nrows() == 0 {
        return false;
    }
    let (mut lx, mut ly, mut hx, mut hy) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for i in 0..n {
        lx = lx.min(pl[[i, 0]]); hx = hx.max(pl[[i, 0]]);
        ly = ly.min(pl[[i, 1]]); hy = hy.max(pl[[i, 1]]);
    }
    let (lx, ly, hx, hy) = (lx - 1.0, ly - 1.0, hx + 1.0, hy + 1.0);
    let (mut mx, mut my, mut nx, mut ny) = (f64::NEG_INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::INFINITY);
    for i in 0..p.nrows() {
        mx = mx.max(p[[i, 0]]); nx = nx.min(p[[i, 0]]);
        my = my.max(p[[i, 1]]); ny = ny.min(p[[i, 1]]);
    }
    if mx < lx || my < ly || nx > hx || ny > hy {
        return false;
    }
    let px: Vec<f64> = (0..n).map(|i| pl[[i, 0]]).collect();
    let pys: Vec<f64> = (0..n).map(|i| pl[[i, 1]]).collect();
    let mut ring = numpy::ndarray::Array2::<f64>::zeros((n + 1, 2));
    for i in 0..=n {
        ring[[i, 0]] = pl[[i % n, 0]];
        ring[[i, 1]] = pl[[i % n, 1]];
    }
    let rv = ring.view();
    for i in 0..p.nrows() - 1 {
        let (ax, ay, bx, by) = (p[[i, 0]], p[[i, 1]], p[[i + 1, 0]], p[[i + 1, 1]]);
        let k = 1usize.max(((bx - ax).hypot(by - ay) / 2.0) as usize);
        for s in 0..k {
            let t = (s as f64 + 0.5) / k as f64;
            let (qx, qy) = (ax + (bx - ax) * t, ay + (by - ay) * t);
            if inside(&px, &pys, qx, qy) && polyline_min_dist(&rv, qx, qy) > margin {
                return true;
            }
        }
    }
    false
}

/// junctions._on_carriageway's test: whether any point (m, 2) lies closer than h to the polyline (k, >= 2).
#[pyfunction]
fn any_within(pts: PyReadonlyArray2<f64>, line: PyReadonlyArray2<f64>, h: f64) -> bool {
    let p = pts.as_array();
    let l = line.as_array();
    (0..p.nrows()).any(|i| polyline_min_dist(&l, p[[i, 0]], p[[i, 1]]) < h)
}

/// buildings._shared_walls: per ring, the number of other rings with at least two vertices within tol of one of its
/// edges. Rings concatenated in coords (N, 2) with offsets offs (n + 1); cents / rads / order as the Python computes
/// them (numpy's means, so the same prefilter); the same arithmetic in the same order as the numpy expressions.
#[pyfunction]
fn shared_walls(coords: PyReadonlyArray2<f64>, offs: PyReadonlyArray1<i64>, cents: PyReadonlyArray2<f64>,
                rads: PyReadonlyArray1<f64>, order: PyReadonlyArray1<i64>, tol: f64) -> Vec<i64> {
    let c = coords.as_array();
    let o = offs.as_array();
    let ce = cents.as_array();
    let r = rads.as_array();
    let ord = order.as_array();
    let n = r.len();
    let mut cnt = vec![0i64; n];
    if n < 2 {
        return cnt;
    }
    let xs: Vec<f64> = ord.iter().map(|&i| ce[[i as usize, 0]]).collect();
    // np.searchsorted(side="left"): the first index whose value is >= v
    let left = |v: f64| -> usize { xs.partition_point(|&x| x < v) };
    for &iu in ord.iter() {
        let i = iu as usize;
        let lo = left(ce[[i, 0]] - r[i] - 30.0);
        let hi = left(ce[[i, 0]] + r[i] + 30.0);
        let (ia, ib) = (o[i] as usize, o[i + 1] as usize);
        let ni = ib - ia;
        for k in lo..hi.max(lo) {
            let j = ord[k] as usize;
            if j == i || (ce[[j, 0]] - ce[[i, 0]]).hypot(ce[[j, 1]] - ce[[i, 1]]) > r[i] + r[j] + tol {
                continue;
            }
            let (ja, jb) = (o[j] as usize, o[j + 1] as usize);
            let mut close = 0usize;
            'pts: for pj in ja..jb {
                let (px, py) = (c[[pj, 0]], c[[pj, 1]]);
                for e in 0..ni {
                    let (ax, ay) = (c[[ia + e, 0]], c[[ia + e, 1]]);
                    let bi = ia + (e + 1) % ni;
                    let (dx, dy) = (c[[bi, 0]] - ax, c[[bi, 1]] - ay);
                    let l2 = (dx * dx + dy * dy).max(1e-9);
                    let t = (((px - ax) * dx + (py - ay) * dy) / l2).clamp(0.0, 1.0);
                    let (qx, qy) = (ax + t * dx, ay + t * dy);
                    if (px - qx).hypot(py - qy) < tol {
                        close += 1;
                        if close >= 2 {
                            break 'pts;
                        }
                    }
                }
            }
            if close >= 2 {
                cnt[i] += 1;
            }
        }
    }
    cnt
}

/// numpy's float remainder (np.remainder / %) for a positive divisor: fmod, moved into [0, b), -0 as +0.
fn np_mod_f32(a: f32, b: f32) -> f32 {
    let m = a % b;
    if m != 0.0 {
        if (b < 0.0) != (m < 0.0) { m + b } else { m }
    } else {
        0.0f32.copysign(b)
    }
}

/// season._smooth for float32 x with Python-float edges: t = clip((x - a) / (b - a), 0, 1), t * t * (3 - 2 t).
fn sm_f32(x: f32, a: f64, b: f64) -> f32 {
    let t = ((x - a as f32) / ((b - a) as f32)).max(0.0).min(1.0);
    t * t * (3.0f32 - 2.0f32 * t)
}

/// season.grade_full's per-texel part, the same float32 operations in numpy's order: the vegetation texels (green the
/// largest channel; hue 60-175 deg ramps, saturation 0.12-0.22) of rgb (h, w, 3) moved toward fall / winter /
/// spring with the noise n, pick and canopy (h, w) as season.py computes them. None: no vegetation texel (unchanged).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn season_grade<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, n: PyReadonlyArray2<'py, f32>,
                     pick: PyReadonlyArray2<'py, f32>, canopy: PyReadonlyArray2<'py, f32>, fall: f64, winter: f64,
                     spring: f64, pal: PyReadonlyArray2<'py, f32>, fall_yellow: f64, winter_desat: f64)
                     -> Option<Bound<'py, numpy::PyArray3<u8>>> {
    let img = rgb.as_array();
    let (nv_, pk, cn, pl) = (n.as_array(), pick.as_array(), canopy.as_array(), pal.as_array());
    let res = py.allow_threads(|| {
        let (hh, ww) = (img.shape()[0], img.shape()[1]);
        let mut out = img.to_owned();
        let mut any = false;
        let w_fall = (1.0f64).min(fall * 1.6) as f32;
        let (ta, tb) = (0.62 - 0.5 * fall, 0.72 - 0.5 * fall);
        let (fy, fl, wi, sp) = (fall_yellow as f32, fall as f32, winter as f32, spring as f32);
        let wd = winter_desat as f32;
        let k255 = 255.0f32;
        for y in 0..hh {
            for x in 0..ww {
                let (r8, g8, b8) = (img[[y, x, 0]], img[[y, x, 1]], img[[y, x, 2]]);
                if !(g8 >= r8 && g8 >= b8) {
                    continue;
                }
                // season._hsv
                let (r, g, b) = (r8 as f32 / k255, g8 as f32 / k255, b8 as f32 / k255);
                let mx = r.max(g).max(b);
                let mn = r.min(g).min(b);
                let d = mx - mn + 1e-6f64 as f32;
                let hraw = if mx == r {
                    np_mod_f32((g - b) / d, 6.0)
                } else if mx == g {
                    (b - r) / d + 2.0
                } else {
                    (r - g) / d + 4.0
                } * 60.0f32;
                let hv = np_mod_f32(hraw, 360.0);
                let sv = if mx > 0.0 { (mx - mn) / (mx + 1e-6f64 as f32) } else { 0.0 };
                let vg = sm_f32(hv, 60.0, 75.0) * (1.0f32 - sm_f32(hv, 160.0, 175.0)) * sm_f32(sv, 0.12, 0.22);
                if !(vg > 0.01f64 as f32) {
                    continue;
                }
                any = true;
                let vv = mx;
                let (nn, pkk, cg) = (nv_[[y, x]], pk[[y, x]], cn[[y, x]]);
                let turned = sm_f32(nn, ta, tb);
                let k = (pkk * 3.0f32).max(0.0).min(2.999f64 as f32) as i8 as usize;
                let (th, tv, ts) = (pl[[k, 0]], pl[[k, 1]], pl[[k, 2]]);
                let w = vg * cg * turned * w_fall;
                let mut nh = hv + (55.0f32 - hv) * vg * fy * fl;
                nh = nh + (th - nh) * w;
                let mut ns = sv * (1.0f32 + (ts - 1.0f32) * w);
                let mut nvv = vv * (1.0f32 + (tv - 1.0f32) * w);
                if winter > 0.0 {
                    let bare = vg * cg * wi * sm_f32(nn, 0.25, 0.4);
                    let grass = vg * (1.0f32 - cg) * wi;
                    nh = nh + (30.0f32 - nh) * bare + (45.0f32 - nh) * grass;
                    ns = ns * (1.0f32 - wd * bare) * (1.0f32 - 0.45f64 as f32 * grass);
                    nvv = nvv * (1.0f32 + 0.15f64 as f32 * bare);
                }
                if spring > 0.0 {
                    let f = vg * sp;
                    nh = nh + (82.0f32 - nh) * 0.5f32 * f;
                    nvv = nvv * (1.0f32 + 0.08f64 as f32 * f);
                    ns = ns * (1.0f32 + 0.1f64 as f32 * f);
                }
                // season._rgb
                let s_ = ns.max(0.0).min(1.0);
                let v_ = nvv.max(0.0).min(1.0);
                let h6 = np_mod_f32(nh, 360.0) / 60.0f32;
                let fl6 = h6.floor();
                let i = ((fl6 as i8) as i32).rem_euclid(6) as usize;
                let f = h6 - fl6;
                let p = v_ * (1.0f32 - s_);
                let q = v_ * (1.0f32 - f * s_);
                let t = v_ * (1.0f32 - (1.0f32 - f) * s_);
                let four = [v_, q, p, t];
                const RI: [usize; 6] = [0, 1, 2, 2, 3, 0];
                const GI: [usize; 6] = [3, 0, 0, 1, 2, 2];
                const BI: [usize; 6] = [2, 2, 3, 0, 0, 1];
                let px = [four[RI[i]], four[GI[i]], four[BI[i]]];
                for c in 0..3 {
                    out[[y, x, c]] = (px[c] * 255.0f32).round_ties_even().max(0.0).min(255.0) as u8;
                }
            }
        }
        if any { Some(out) } else { None }
    });
    res.map(|o| o.into_pyarray(py))
}

/// cdae.tangents: per-vertex tangents (xyz, w) by Lengyel's method, the numpy code's operations in its order (the
/// corner sums in bincount's order: corner 0 of every triangle, then 1, then 2; 3-term dots left to right).
#[pyfunction]
fn tangents<'py>(py: Python<'py>, v: PyReadonlyArray2<'py, f64>, uv: PyReadonlyArray2<'py, f64>,
                 nrm: PyReadonlyArray2<'py, f64>, idx: PyReadonlyArray2<'py, i64>) -> Bound<'py, PyArray2<f64>> {
    let (v, uv, nr, ix) = (v.as_array(), uv.as_array(), nrm.as_array(), idx.as_array());
    let out = release(py, ix.shape()[0] * 20, || {
        let n = v.nrows();
        let m = ix.nrows();
        let mut sd = vec![[0f64; 3]; m];
        let mut td = vec![[0f64; 3]; m];
        for k in 0..m {
            let (i0, i1, i2) = (ix[[k, 0]] as usize, ix[[k, 1]] as usize, ix[[k, 2]] as usize);
            let e1 = [v[[i1, 0]] - v[[i0, 0]], v[[i1, 1]] - v[[i0, 1]], v[[i1, 2]] - v[[i0, 2]]];
            let e2 = [v[[i2, 0]] - v[[i0, 0]], v[[i2, 1]] - v[[i0, 1]], v[[i2, 2]] - v[[i0, 2]]];
            let d1 = [uv[[i1, 0]] - uv[[i0, 0]], uv[[i1, 1]] - uv[[i0, 1]]];
            let d2 = [uv[[i2, 0]] - uv[[i0, 0]], uv[[i2, 1]] - uv[[i0, 1]]];
            let den = d1[0] * d2[1] - d2[0] * d1[1];
            let r = if den.abs() > 0.0 { 1.0 / den } else { 0.0 };
            for c in 0..3 {
                sd[k][c] = (e1[c] * d2[1] - e2[c] * d1[1]) * r;
                td[k][c] = (e2[c] * d1[0] - e1[c] * d2[0]) * r;
            }
        }
        let mut t1 = vec![[0f64; 3]; n];
        let mut t2 = vec![[0f64; 3]; n];
        for corner in 0..3 {
            for k in 0..m {
                let i = ix[[k, corner]] as usize;
                for c in 0..3 {
                    t1[i][c] += sd[k][c];
                    t2[i][c] += td[k][c];
                }
            }
        }
        let mut out = numpy::ndarray::Array2::<f64>::zeros((n, 4));
        for i in 0..n {
            let nn = [nr[[i, 0]], nr[[i, 1]], nr[[i, 2]]];
            let d = nn[0] * t1[i][0] + nn[1] * t1[i][1] + nn[2] * t1[i][2];
            let mut t = [t1[i][0] - nn[0] * d, t1[i][1] - nn[1] * d, t1[i][2] - nn[2] * d];
            let mut tl = (t[0] * t[0] + t[1] * t[1] + t[2] * t[2]).sqrt();
            for axis in 0..2 {
                if !(tl < 1e-12) {
                    break;
                }
                let mut e = [0f64; 3];
                e[axis] = 1.0;
                t = [e[0] - nn[0] * nn[axis], e[1] - nn[1] * nn[axis], e[2] - nn[2] * nn[axis]];
                tl = (t[0] * t[0] + t[1] * t[1] + t[2] * t[2]).sqrt();
            }
            let dv = if tl > 0.0 { tl } else { 1.0 };
            for c in 0..3 {
                t[c] /= dv;
            }
            let cr = [nn[1] * t[2] - nn[2] * t[1], nn[2] * t[0] - nn[0] * t[2], nn[0] * t[1] - nn[1] * t[0]];
            let s = cr[0] * t2[i][0] + cr[1] * t2[i][1] + cr[2] * t2[i][2];
            out[[i, 0]] = t[0];
            out[[i, 1]] = t[1];
            out[[i, 2]] = t[2];
            out[[i, 3]] = if s < 0.0 { -1.0 } else { 1.0 };
        }
        out
    });
    out.into_pyarray(py)
}

/// season._noise: smooth value noise in [0, 1] (float32) on the unit lattice at x, y (float32 (h, w)); numpy's int64
/// hash with its wrap-around products and arithmetic shifts, the float32 smoothstep and lerps in its order.
#[pyfunction]
fn season_noise<'py>(py: Python<'py>, x: PyReadonlyArray2<'py, f32>, y: PyReadonlyArray2<'py, f32>, seed: i64)
                     -> Bound<'py, PyArray2<f32>> {
    let (xa, ya) = (x.as_array(), y.as_array());
    let out = py.allow_threads(|| {
        let sd = seed.wrapping_mul(83492791);
        let h = |ix: f32, iy: f32| -> f32 {
            let mut v = (ix as i64).wrapping_mul(73856093) ^ (iy as i64).wrapping_mul(19349663) ^ sd;
            v = (v ^ (v >> 13)).wrapping_mul(1274126177);
            ((v ^ (v >> 16)) & 0xFFFF) as f32 / 65535.0f32
        };
        numpy::ndarray::Array2::from_shape_fn(xa.raw_dim(), |(i, j)| {
            let (xv, yv) = (xa[[i, j]], ya[[i, j]]);
            let (x0, y0) = (xv.floor(), yv.floor());
            let (mut fx, mut fy) = (xv - x0, yv - y0);
            fx = fx * fx * (3.0f32 - 2.0f32 * fx);
            fy = fy * fy * (3.0f32 - 2.0f32 * fy);
            let (a, b, c, d) = (h(x0, y0), h(x0 + 1.0, y0), h(x0, y0 + 1.0), h(x0 + 1.0, y0 + 1.0));
            (a + (b - a) * fx) + ((c + (d - c) * fx) - (a + (b - a) * fx)) * fy
        })
    });
    out.into_pyarray(py)
}

/// dem.decode_terrarium's arithmetic on the decoded image (h, w, 3|4 uint8): R * 256 + G + B / 256 - 32768 in float32,
/// in numpy's order, without the RGB conversion and float temporaries.
#[pyfunction]
fn terrarium_heights<'py>(py: Python<'py>, img: PyReadonlyArray3<'py, u8>) -> Bound<'py, PyArray2<f32>> {
    let a = img.as_array();
    let out = py.allow_threads(|| {
        numpy::ndarray::Array2::from_shape_fn((a.shape()[0], a.shape()[1]), |(y, x)| {
            a[[y, x, 0]] as f32 * 256.0f32 + a[[y, x, 1]] as f32 + a[[y, x, 2]] as f32 / 256.0f32 - 32768.0f32
        })
    });
    out.into_pyarray(py)
}

/// colormatch._rf_pass in place: X (rows, cols, ch) float32 filtered along axis 1, forward then backward, with the
/// link weight f = 0 where cut_prev[r, c] (no link between c - 1 and c), else a; numpy's float32 operations in order.
#[pyfunction]
fn rf_pass(py: Python<'_>, mut x: numpy::PyReadwriteArray3<'_, f32>, cut_prev: PyReadonlyArray2<'_, bool>, a: f64) {
    let cut = cut_prev.as_array();
    let mut xa = x.as_array_mut();
    let (rows, cols, ch) = (xa.shape()[0], xa.shape()[1], xa.shape()[2]);
    let af = a as f32;
    py.allow_threads(|| {
        for r in 0..rows {
            for c in 1..cols {
                let f = if cut[[r, c]] { 0.0f32 } else { af };
                for k in 0..ch {
                    let prev = xa[[r, c - 1, k]];
                    let cur = xa[[r, c, k]];
                    xa[[r, c, k]] = cur + f * (prev - cur);
                }
            }
            for c in (0..cols.saturating_sub(1)).rev() {
                let f = if cut[[r, c + 1]] { 0.0f32 } else { af };
                for k in 0..ch {
                    let next = xa[[r, c + 1, k]];
                    let cur = xa[[r, c, k]];
                    xa[[r, c, k]] = cur + f * (next - cur);
                }
            }
        }
    });
}

/// colormatch._vmedian: the lower median over axis 0 of X (K, h, w) float32 of the entries with ok (K, h, w), the
/// (n - 1) // 2-th smallest of them (n: (h, w) counts), 0 where there are none.
#[pyfunction]
fn vmedian<'py>(py: Python<'py>, x: PyReadonlyArray3<'py, f32>, ok: PyReadonlyArray3<'py, bool>,
                n: PyReadonlyArray2<'py, i64>) -> Bound<'py, PyArray2<f32>> {
    let (xa, oa, na) = (x.as_array(), ok.as_array(), n.as_array());
    let out = py.allow_threads(|| {
        let (k, h, w) = (xa.shape()[0], xa.shape()[1], xa.shape()[2]);
        let mut buf = vec![0f32; k];
        numpy::ndarray::Array2::from_shape_fn((h, w), |(y, x_)| {
            for i in 0..k {
                buf[i] = if oa[[i, y, x_]] { xa[[i, y, x_]] } else { f32::INFINITY };
            }
            buf.sort_by(|p, q| p.partial_cmp(q).unwrap_or(std::cmp::Ordering::Equal));
            let idx = ((na[[y, x_]] - 1).div_euclid(2)).max(0) as usize;
            let mut m = buf[0];
            for kk in 1..=(k - 1) / 2 {
                if idx == kk {
                    m = buf[kk];
                }
            }
            if m.is_finite() { m } else { 0.0 }
        })
    });
    out.into_pyarray(py)
}

/// The lower median of `v[..k]` entries with `ok`, as colormatch._vmedian (idx = (n - 1) // 2 among the sorted
/// ok values, 0 when there are none).
fn lower_median(v: &[f32], ok: &[bool], n: i64, buf: &mut [f32]) -> f32 {
    let k = v.len();
    // a stable insertion sort (k is 5): the order sort_by gives
    for i in 0..k {
        let x = if ok[i] { v[i] } else { f32::INFINITY };
        let mut j = i;
        while j > 0 && buf[j - 1] > x {
            buf[j] = buf[j - 1];
            j -= 1;
        }
        buf[j] = x;
    }
    let idx = ((n - 1).div_euclid(2)).max(0) as usize;
    let mut m = buf[0];
    for kk in 1..=(k - 1) / 2 {
        if idx == kk {
            m = buf[kk];
        }
    }
    if m.is_finite() { m } else { 0.0 }
}

/// colormatch.detect_seams, one rotation's step test on the rotated weights and weighted log channels
/// (arrs [7, H, W] float32: w, then USGS r g b, then S2 r g b, each times w): the band sums each side of every
/// candidate line (float64 cumulative sums as numpy's), the per-segment steps, their lower medians over the
/// `offs` segments, the agreement and the S2 gate. Returns (score [H, W], Dm [3, H, W]) as the numpy code, bit
/// for bit, without its ~40 full-size temporaries.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn seam_pass<'py>(py: Python<'py>, arrs: PyReadonlyArray3<'py, f32>, half: i64, depth: i64, offs: Vec<i64>,
                  full: f64, gate: f32, agree_frac: f64, min_valid: i64)
                  -> (Bound<'py, PyArray2<f32>>, Bound<'py, numpy::PyArray3<f32>>) {
    let view = arrs.as_array();
    let (nch, h, w) = (view.shape()[0], view.shape()[1], view.shape()[2]);
    assert!(nch == 7);
    let a = arrs.as_slice().expect("contiguous");
    let (score, dm) = py.allow_threads(|| {
        let clip = |v: i64, hi: usize| v.clamp(0, hi as i64) as usize;
        let hi_x: Vec<usize> = (0..w as i64).map(|i| clip(i + half + 1, w)).collect();
        let lo_x: Vec<usize> = (0..w as i64).map(|i| clip(i - half, w)).collect();
        let b1: Vec<usize> = (0..h as i64).map(|j| clip(j + depth + 1, h)).collect();
        let b0: Vec<usize> = (0..h as i64).map(|j| clip(j + 1, h)).collect();
        let a1: Vec<usize> = (0..h as i64).map(|j| clip(j, h)).collect();
        let a0: Vec<usize> = (0..h as i64).map(|j| clip(j - depth, h)).collect();
        // per channel: the band sums below (b) and above (a) each line, as numpy's cumsum differences
        let mut c = vec![0f64; w + 1];
        let mut cy = vec![0f64; (h + 1) * w];
        let mut band = |ch: usize, b: &mut [f64], at: &mut [f64]| {
            for y in 0..h {
                let row = &a[(ch * h + y) * w..(ch * h + y + 1) * w];
                c[0] = 0.0;
                for x in 0..w {
                    c[x + 1] = c[x] + row[x] as f64;
                }
                for i in 0..w {
                    let s = c[hi_x[i]] - c[lo_x[i]];
                    cy[(y + 1) * w + i] = cy[y * w + i] + s;
                }
            }
            for j in 0..h {
                for i in 0..w {
                    b[j * w + i] = cy[b1[j] * w + i] - cy[b0[j] * w + i];
                    at[j * w + i] = cy[a1[j] * w + i] - cy[a0[j] * w + i];
                }
            }
        };
        let mut wb = vec![0f64; h * w];
        let mut wa = vec![0f64; h * w];
        band(0, &mut wb, &mut wa);
        // the per-segment steps (float32) and the segments with enough weight both sides
        let mut ok = vec![false; h * w];
        for p in 0..h * w {
            ok[p] = wb[p] > full && wa[p] > full;
            wb[p] = 1.0 / wb[p].max(1e-3);
            wa[p] = 1.0 / wa[p].max(1e-3);
        }
        let mut dseg = vec![0f32; 6 * h * w];
        let mut bs = vec![0f64; h * w];
        let mut as_ = vec![0f64; h * w];
        for ch in 0..6 {
            band(ch + 1, &mut bs, &mut as_);
            let d = &mut dseg[ch * h * w..(ch + 1) * h * w];
            for p in 0..h * w {
                d[p] = ((bs[p] * wb[p]) - (as_[p] * wa[p])) as f32;
            }
        }
        let k = offs.len();
        let mut score = numpy::ndarray::Array2::<f32>::zeros((h, w));
        let mut dm = numpy::ndarray::Array3::<f32>::zeros((3, h, w));
        let mut oks = vec![false; k];
        let mut dl = vec![[0f32; 3]; k];
        let mut ds = vec![[0f32; 3]; k];
        let mut col = vec![0f32; k];
        let mut buf = vec![0f32; k];
        for y in 0..h {
            for x in 0..w {
                let mut nvalid = 0i64;
                for (kk, &o) in offs.iter().enumerate() {
                    let xs = x as i64 + o;
                    if xs >= 0 && (xs as usize) < w {
                        let p = y * w + xs as usize;
                        oks[kk] = ok[p];
                        for ch in 0..3 {
                            dl[kk][ch] = dseg[ch * h * w + p] - dseg[(ch + 3) * h * w + p];
                            ds[kk][ch] = dseg[(ch + 3) * h * w + p];
                        }
                    } else {
                        oks[kk] = false;
                        dl[kk] = [0.0; 3];
                        ds[kk] = [0.0; 3];
                    }
                    if oks[kk] {
                        nvalid += 1;
                    }
                }
                let mut m = [0f32; 3];
                let mut msm = [0f32; 3];
                for ch in 0..3 {
                    for kk in 0..k {
                        col[kk] = dl[kk][ch];
                    }
                    m[ch] = lower_median(&col, &oks, nvalid, &mut buf);
                    for kk in 0..k {
                        col[kk] = ds[kk][ch];
                    }
                    msm[ch] = lower_median(&col, &oks, nvalid, &mut buf);
                }
                let nm = (m[0] * m[0] + m[1] * m[1] + m[2] * m[2]).sqrt();
                let mut agree = 0i64;
                for kk in 0..k {
                    let d = dl[kk];
                    let nk = (d[0] * d[0] + d[1] * d[1] + d[2] * d[2]).sqrt();
                    let cos = (d[0] * m[0] + d[1] * m[1] + d[2] * m[2]) / (nk * nm).max(1e-6f32);
                    if cos > 0.7f32 && nk > 0.5f32 * nm && oks[kk] {
                        agree += 1;
                    }
                }
                let cons = nvalid >= min_valid && (agree as f64) >= agree_frac * nvalid as f64;
                let nsm = (msm[0] * msm[0] + msm[1] * msm[1] + msm[2] * msm[2]).sqrt();
                let g = (1.0f32 - (nsm - 0.03f32).max(0.0) / (gate * nm + 1e-3f32)).clamp(0.0, 1.0);
                score[[y, x]] = nm * (if cons { 1.0f32 } else { 0.0 }) * g;
                for ch in 0..3 {
                    dm[[ch, y, x]] = m[ch];
                }
            }
        }
        (score, dm)
    });
    (score.into_pyarray(py), dm.into_pyarray(py))
}

/// farfield.view_distance for many tiles: bboxes (n, 4) [x0, y0, x1, y1]; the horizontal distance from (px, py) to
/// each footprint, times `penalty` when the tile's centre is outside the view cone (cone = [cx, cy, fx, fy, cosmax]
/// with a unit forward; empty: no cone) and farther from the camera than its own width. The build queue's order.
#[pyfunction]
fn view_distances<'py>(py: Python<'py>, bboxes: PyReadonlyArray2<'py, f64>, px: f64, py_: f64, cone: Vec<f64>,
                       penalty: f64) -> Bound<'py, PyArray1<f64>> {
    let b = bboxes.as_array();
    let n = b.shape()[0];
    let out: Vec<f64> = release(py, n * cone.len().max(1), || {
        (0..n).map(|k| {
            let (x0, y0, x1, y1) = (b[[k, 0]], b[[k, 1]], b[[k, 2]], b[[k, 3]]);
            let dx = (x0 - px).max(0.0).max(px - x1);
            let dy = (y0 - py_).max(0.0).max(py_ - y1);
            let d = dx.hypot(dy);
            if cone.len() != 5 || d <= 0.0 {
                return d;
            }
            let (cx, cy) = ((x0 + x1) / 2.0 - cone[0], (y0 + y1) / 2.0 - cone[1]);
            let r = cx.hypot(cy);
            if r <= x1 - x0 || (cx * cone[2] + cy * cone[3]) / r >= cone[4] { d } else { d * penalty }
        }).collect()
    });
    numpy::ndarray::Array1::from(out).into_pyarray(py)
}

/// vegetation._mask_roads' inner loop: the road corridors of segments (n, 4) [ax, ay, bx, by] in CHM grid px (column,
/// row), half widths hw (n) px, in order: mask (rows, cols) |= distance <= hw, and push (3, rows, cols) float32 (dx, dy,
/// distance) where a segment is nearer than any before. float64 as numpy (window, projection, np.hypot), bit for bit.
#[pyfunction]
fn road_corridors<'py>(py: Python<'py>, mut mask: numpy::PyReadwriteArray2<'py, bool>,
                       mut push: Option<numpy::PyReadwriteArray3<'py, f32>>, segs: PyReadonlyArray2<'py, f64>,
                       hw: PyReadonlyArray1<'py, f64>) {
    let s = segs.as_array();
    let h = hw.as_array();
    let mut m = mask.as_array_mut();
    let mut p = push.as_mut().map(|a| a.as_array_mut());
    let (n_r, n_c) = (m.shape()[0] as i64, m.shape()[1] as i64);
    py.allow_threads(|| {
        for k in 0..s.shape()[0] {
            let (a0, a1, b0, b1, hwk) = (s[[k, 0]], s[[k, 1]], s[[k, 2]], s[[k, 3]], h[k]);
            let c0 = ((a0.min(b0) - hwk) as i64).max(0);
            let r0 = ((a1.min(b1) - hwk) as i64).max(0);
            let c1 = ((a0.max(b0) + hwk) as i64 + 2).min(n_c);
            let r1 = ((a1.max(b1) + hwk) as i64 + 2).min(n_r);
            if c1 <= c0 || r1 <= r0 {
                continue;
            }
            let (ab0, ab1) = (b0 - a0, b1 - a1);
            let dot = 0.0 + ab0 * ab0 + ab1 * ab1;
            let l2 = if dot != 0.0 { dot } else { 1e-9 };
            let (nx, ny) = (-ab1, ab0);
            let nl0 = nx.hypot(ny);
            let nl = if nl0 != 0.0 { nl0 } else { 1.0 };
            for r in r0..r1 {
                let gr = r as f64 + 0.5;
                for c in c0..c1 {
                    let gc = c as f64 + 0.5;
                    let t = (((gc - a0) * ab0 + (gr - a1) * ab1) / l2).clamp(0.0, 1.0);
                    let ox = gc - (a0 + t * ab0);
                    let oy = gr - (a1 + t * ab1);
                    let d = ox.hypot(oy);
                    let inside = d <= hwk;
                    if inside {
                        m[[r as usize, c as usize]] = true;
                    }
                    if let Some(pa) = p.as_mut() {
                        if inside && d < pa[[2, r as usize, c as usize]] as f64 {
                            let (ux, uy) = if d > 1e-3 { (ox / d.max(1e-3), oy / d.max(1e-3)) } else { (nx / nl, ny / nl) };
                            let go = hwk + 0.6 - d;
                            pa[[0, r as usize, c as usize]] = (ux * go) as f32;
                            pa[[1, r as usize, c as usize]] = (uy * go) as f32;
                            pa[[2, r as usize, c as usize]] = d as f32;
                        }
                    }
                }
            }
        }
    });
}

/// deshadow.horizon_tan's march for a batch of steps: best (n, n) float32 = max(best, (h(step) - h0) / d), h(step) the
/// bilinear sample of hpad (float32) at (pad + iy + fy, pad + ix + fx) as numpy's slices compute it (weights cast to
/// float32 as numpy does with a Python float). steps (k, 5): [iy, ix, fy, fx, d].
#[pyfunction]
fn horizon_steps<'py>(py: Python<'py>, hpad: PyReadonlyArray2<'py, f32>, mut best: numpy::PyReadwriteArray2<'py, f32>,
                      pad: i64, steps: PyReadonlyArray2<'py, f64>) {
    let view = hpad.as_array();
    let w = view.shape()[1];
    let hp = hpad.as_slice().expect("contiguous");
    let st = steps.as_array();
    let n = best.as_array().shape()[0];
    let b = best.as_slice_mut().expect("contiguous");
    let p = pad as usize;
    py.allow_threads(|| {
        for k in 0..st.shape()[0] {
            let (iy, ix, fy, fx, d) = (st[[k, 0]] as i64, st[[k, 1]] as i64, st[[k, 2]], st[[k, 3]], st[[k, 4]]);
            let (wx0, wx1, wy0, wy1) = ((1.0 - fx) as f32, fx as f32, (1.0 - fy) as f32, fy as f32);
            let df = d as f32;
            let r0 = (pad + iy) as usize;
            let c0 = (pad + ix) as usize;
            for r in 0..n {
                let top_row = &hp[(r0 + r) * w + c0..(r0 + r) * w + c0 + n + 1];
                let bot_row = &hp[(r0 + r + 1) * w + c0..(r0 + r + 1) * w + c0 + n + 1];
                let h0_row = &hp[(p + r) * w + p..(p + r) * w + p + n];
                let b_row = &mut b[r * n..(r + 1) * n];
                for c in 0..n {
                    let top = top_row[c] * wx0 + top_row[c + 1] * wx1;
                    let bot = bot_row[c] * wx0 + bot_row[c + 1] * wx1;
                    let hs = top * wy0 + bot * wy1;
                    let v = (hs - h0_row[c]) / df;
                    if v > b_row[c] {
                        b_row[c] = v;
                    }
                }
            }
        }
    });
}

/// grading.Grader._over_tunnels for one covered tunnel, into mask (m,) in place: the points (e, n) over the line xy
/// (k, 2) within hw of it, along it from keep_in inside its ends. Only segments whose box (+ hw) meets the points' box
/// are visited (the Gotthard road tunnel: 4,200 segments). The numpy version's float64 sums, point for point.
#[pyfunction]
fn over_tunnel(py: Python<'_>, mut mask: numpy::PyReadwriteArray1<'_, bool>, e: PyReadonlyArray1<'_, f64>,
               n: PyReadonlyArray1<'_, f64>, xy: PyReadonlyArray2<'_, f64>, hw: f64, keep_in: f64) {
    let (ev, nv, xv) = (e.as_array(), n.as_array(), xy.as_array());
    let mut mv = mask.as_array_mut();
    release(py, ev.len() * xv.shape()[0].max(1), || {
        let np_ = ev.len();
        let k = xv.shape()[0];
        if np_ == 0 || k < 2 {
            return;
        }
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for i in 0..np_ {
            x0 = x0.min(ev[i]);
            x1 = x1.max(ev[i]);
            y0 = y0.min(nv[i]);
            y1 = y1.max(nv[i]);
        }
        let mut sj = vec![0.0f64; k];
        let mut l2 = vec![0.0f64; k - 1];
        for j in 0..k - 1 {
            let (ab0, ab1) = (xv[[j + 1, 0]] - xv[[j, 0]], xv[[j + 1, 1]] - xv[[j, 1]]);
            l2[j] = (ab0 * ab0 + ab1 * ab1).max(1e-12);
            sj[j + 1] = sj[j] + l2[j].sqrt();
        }
        let total = sj[k - 1];
        for j in 0..k - 1 {
            let (a0, a1, b0, b1) = (xv[[j, 0]], xv[[j, 1]], xv[[j + 1, 0]], xv[[j + 1, 1]]);
            if a0.min(b0) - hw > x1 || a0.max(b0) + hw < x0 || a1.min(b1) - hw > y1 || a1.max(b1) + hw < y0 {
                continue;
            }
            let (ab0, ab1) = (b0 - a0, b1 - a1);
            let len = l2[j].sqrt();
            for i in 0..np_ {
                if mv[i] {
                    continue;
                }
                let t = ((ev[i] - a0) * ab0 + (nv[i] - a1) * ab1) / l2[j];
                if !(t > 0.0 && t < 1.0) {
                    continue;
                }
                let s_ = sj[j] + t * len;
                if !(s_ > keep_in && s_ < total - keep_in) {
                    continue;
                }
                let d = ((ev[i] - a0) * ab1 - (nv[i] - a1) * ab0).abs() / len;
                if d <= hw {
                    mv[i] = true;
                }
            }
        }
    });
}

/// riverlines._confluences' test: for each point p (m, 2), is any segment a -> b (s, 2) nearer than its hw (s)? float64
/// as numpy's broadcast (squared length sum, clipped projection, np.hypot), bit for bit.
#[pyfunction]
fn points_near_segments<'py>(py: Python<'py>, p: PyReadonlyArray2<'py, f64>, a: PyReadonlyArray2<'py, f64>,
                             b: PyReadonlyArray2<'py, f64>, hw: PyReadonlyArray1<'py, f64>) -> Bound<'py, PyArray1<bool>> {
    let (pv, av, bv, hv) = (p.as_array(), a.as_array(), b.as_array(), hw.as_array());
    let out: Vec<bool> = release(py, pv.shape()[0] * av.shape()[0].max(1), || {
        let ns = av.shape()[0];
        let seg: Vec<[f64; 6]> = (0..ns).map(|k| {
            let (a0, a1) = (av[[k, 0]], av[[k, 1]]);
            let (ab0, ab1) = (bv[[k, 0]] - a0, bv[[k, 1]] - a1);
            [a0, a1, ab0, ab1, (ab0 * ab0 + ab1 * ab1).max(1e-9), hv[k]]
        }).collect();
        (0..pv.shape()[0]).map(|i| {
            let (p0, p1) = (pv[[i, 0]], pv[[i, 1]]);
            seg.iter().any(|g| {
                let t = (((p0 - g[0]) * g[2] + (p1 - g[1]) * g[3]) / g[4]).clamp(0.0, 1.0);
                let d = (p0 - (g[0] + t * g[2])).hypot(p1 - (g[1] + t * g[3]));
                d < g[5]
            })
        }).collect()
    });
    numpy::ndarray::Array1::from(out).into_pyarray(py)
}

/// roads.tunnel_hole_mask for one tunnel part, into mask (n, n) in place: the samples (ix, iy) of the block (heights u,
/// sample at (x0 + ix * square, y0 + iy * square)) over the tube's footprint (half width hw) beside a segment of xy
/// (k, 2) whose ground lies between its floor z (k,) and the vault th (+- margin); for a low arch (low) instead its
/// mouths (arc within end_m of a portal end flagged m0 / m1) out to hw + side_extra, whatever the height. The same
/// float64 sums as the numpy version, sample for sample.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn tunnel_holes(py: Python<'_>, mut mask: numpy::PyReadwriteArray2<'_, bool>, u: PyReadonlyArray2<'_, f64>, x0: f64, y0: f64,
                square: f64, xy: PyReadonlyArray2<'_, f64>, z: PyReadonlyArray1<'_, f64>, hw: f64, th: f64, low: bool,
                m0: bool, m1: bool, margin: f64, end_m: f64, side_extra: f64) {
    let (uv, xv, zv) = (u.as_array(), xy.as_array(), z.as_array());
    let mut mv = mask.as_array_mut();
    py.allow_threads(|| {
        let n = uv.shape()[0] as i64;
        let k = xv.shape()[0];
        if k < 2 { return; }
        let mut sj = vec![0.0f64; k];
        for j in 1..k { sj[j] = sj[j - 1] + (xv[[j, 0]] - xv[[j - 1, 0]]).hypot(xv[[j, 1]] - xv[[j - 1, 1]]); }
        let total = sj[k - 1];
        let reach = if low { hw + side_extra } else { hw };
        for j in 0..k - 1 {
            let (ax, ay, bx, by) = (xv[[j, 0]], xv[[j, 1]], xv[[j + 1, 0]], xv[[j + 1, 1]]);
            let (lox, loy, hix, hiy) = (ax.min(bx) - hw, ay.min(by) - hw, ax.max(bx) + hw, ay.max(by) + hw);
            let ix0 = (((lox - x0) / square).floor() as i64).max(0);
            let iy0 = (((loy - y0) / square).floor() as i64).max(0);
            let ix1 = (((hix - x0) / square).ceil() as i64).min(n - 1);
            let iy1 = (((hiy - y0) / square).ceil() as i64).min(n - 1);
            if ix0 > ix1 || iy0 > iy1 { continue; }
            let (abx, aby) = (bx - ax, by - ay);
            let l2 = (abx * abx + aby * aby).max(1e-12);
            for iy in iy0..=iy1 {
                let yy = y0 + iy as f64 * square;
                for ix in ix0..=ix1 {
                    let xx = x0 + ix as f64 * square;
                    let t0 = ((xx - ax) * abx + (yy - ay) * aby) / l2;
                    if !(0.0..=1.0).contains(&t0) { continue; }
                    let t = t0;
                    let d = (xx - (ax + t * abx)).hypot(yy - (ay + t * aby));
                    let hit = if low {
                        let s = sj[j] + t * (sj[j + 1] - sj[j]);
                        d <= reach && ((s <= end_m && m0) || (s >= total - end_m && m1))
                    } else {
                        let floor = zv[j] + t * (zv[j + 1] - zv[j]);
                        let g = uv[[iy as usize, ix as usize]];
                        d <= hw && g > floor - margin && g < floor + th + margin
                    };
                    if hit { mv[[iy as usize, ix as usize]] = true; }
                }
            }
        }
    });
}

/// roads.arch_raise for one low arch, into z (m,) in place at the points (px, py): over the vault (beside a segment of
/// xy within hw) at least floor + th + soil, at its mouths (arc within mouth_m of an end flagged m0 / m1) at most
/// floor - 0.02. The numpy version's sums.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn arch_raise_part(py: Python<'_>, mut z: numpy::PyReadwriteArray1<'_, f64>, px: PyReadonlyArray1<'_, f64>,
                   pyy: PyReadonlyArray1<'_, f64>, xy: PyReadonlyArray2<'_, f64>, zf: PyReadonlyArray1<'_, f64>, hw: f64,
                   th: f64, soil: f64, mouth_m: f64, m0: bool, m1: bool) {
    let (xv, fv) = (xy.as_array(), zf.as_array());
    let pxs = px.as_slice().expect("contiguous px");
    let pys = pyy.as_slice().expect("contiguous py");
    let zs = z.as_slice_mut().expect("contiguous z");
    release(py, zs.len() * xv.shape()[0].max(1), || {
        let k = xv.shape()[0];
        if k < 2 { return; }
        // per segment: start, direction, squared length, its length, arc at its start, floor at both ends
        let mut seg: Vec<[f64; 8]> = Vec::with_capacity(k - 1);
        let mut s0 = 0.0;
        for j in 0..k - 1 {
            let (ax, ay, abx, aby) = (xv[[j, 0]], xv[[j, 1]], xv[[j + 1, 0]] - xv[[j, 0]], xv[[j + 1, 1]] - xv[[j, 1]]);
            let l2 = (abx * abx + aby * aby).max(1e-12);
            let len = abx.hypot(aby);
            seg.push([ax, ay, abx, aby, l2, s0, fv[j], fv[j + 1]]);
            s0 += len;
        }
        let total = s0;
        let (mut lox, mut loy, mut hix, mut hiy) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for j in 0..k { lox = lox.min(xv[[j, 0]]); loy = loy.min(xv[[j, 1]]); hix = hix.max(xv[[j, 0]]); hiy = hiy.max(xv[[j, 1]]); }
        let (lox, loy, hix, hiy) = (lox - hw, loy - hw, hix + hw, hiy + hw);
        // the box test without branches (random points: a branch here mispredicted half the time, 1.5 ms a block)
        let near: Vec<usize> = pxs.iter().zip(pys.iter()).enumerate()
            .filter(|(_, (&x, &y))| (x >= lox) & (x <= hix) & (y >= loy) & (y <= hiy)).map(|(i, _)| i).collect();
        for i in near {
            let (x, y) = (pxs[i], pys[i]);
            let zi = &mut zs[i];
            for (j, g) in seg.iter().enumerate() {
                let (ax, ay, abx, aby, l2) = (g[0], g[1], g[2], g[3], g[4]);
                let t = ((x - ax) * abx + (y - ay) * aby) / l2;
                if !(0.0..=1.0).contains(&t) { continue; }
                let d = ((x - ax) * aby - (y - ay) * abx).abs() / l2.sqrt();
                if d > hw { continue; }
                let send = if j + 1 < seg.len() { seg[j + 1][5] } else { total };
                let s = g[5] + t * (send - g[5]);
                let floor = g[6] + t * (g[7] - g[6]);
                let mouth = (s <= mouth_m && m0) || (s >= total - mouth_m && m1);
                if mouth { *zi = zi.min(floor - 0.02); } else { *zi = zi.max(floor + th + soil); }
            }
        }
    });
}

/// plazas.mesh: triangles (t, 3) over points (n, 2) split at their edges' midpoints (each edge's midpoint shared by
/// both its triangles: no cracks) until every edge is at most max_edge long; returns (points, triangles). The split
/// is by the longest edge first, deterministic.
#[pyfunction]
fn subdivide_tris<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, tris: PyReadonlyArray2<'py, i64>, max_edge: f64)
                       -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>) {
    let (pv, tv) = (pts.as_array(), tris.as_array());
    let (p_out, t_out) = release(py, tv.shape()[0] * 50, || {
        let mut p: Vec<[f64; 2]> = if pv.ncols() >= 2 {
            (0..pv.shape()[0]).map(|i| [pv[[i, 0]], pv[[i, 1]]]).collect()
        } else {
            Vec::new()
        };
        let mut stack: Vec<[usize; 3]> = if tv.ncols() >= 3 && !p.is_empty() {
            (0..tv.shape()[0]).rev()
                .map(|i| [tv[[i, 0]] as usize, tv[[i, 1]] as usize, tv[[i, 2]] as usize]).collect()
        } else {
            Vec::new()
        };
        let mut mids: std::collections::HashMap<(usize, usize), usize> = std::collections::HashMap::new();
        let mut out: Vec<[usize; 3]> = Vec::new();
        let max2 = max_edge * max_edge;
        let mut guard = 0usize;
        while let Some(t) = stack.pop() {
            guard += 1;
            if guard > 4_000_000 || !(max_edge > 0.0) { out.push(t); continue; }
            let d = |a: usize, b: usize| { let (x, y) = (p[a][0] - p[b][0], p[a][1] - p[b][1]); x * x + y * y };
            let e = [d(t[0], t[1]), d(t[1], t[2]), d(t[2], t[0])];
            let (k, &m) = e.iter().enumerate().fold((0, &e[0]), |acc, (i, v)| if *v > *acc.1 { (i, v) } else { acc });
            if !m.is_finite() || m <= max2 { out.push(t); continue; }
            let (a, b, c) = (t[k], t[(k + 1) % 3], t[(k + 2) % 3]);
            let key = if a < b { (a, b) } else { (b, a) };
            let mi = *mids.entry(key).or_insert_with(|| {
                p.push([(p[a][0] + p[b][0]) / 2.0, (p[a][1] + p[b][1]) / 2.0]);
                p.len() - 1
            });
            stack.push([mi, b, c]);
            stack.push([a, mi, c]);
        }
        (p, out)
    });
    let pa = numpy::ndarray::Array2::from_shape_vec((p_out.len(), 2), p_out.into_iter().flatten().collect()).unwrap();
    let ta = numpy::ndarray::Array2::from_shape_vec((t_out.len(), 3),
        t_out.into_iter().flat_map(|t| t.into_iter().map(|v| v as i64)).collect()).unwrap();
    (pa.into_pyarray(py), ta.into_pyarray(py))
}

/// roadmesh.deck's parapet test: for each deck edge point p (m, 3) of deck owner op (m,), does another deck's surface
/// lie there: a centre-line segment a -> b (s, 3) of another owner (os, (s,)) nearer in plan than its half width hw (s,)
/// with its height there within dz of the point's (a ramp leaving the mainline, the carriageways of one road side by
/// side: no wall between them, 2026-10-17).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn deck_edges_covered<'py>(py: Python<'py>, p: PyReadonlyArray2<'py, f64>, op: PyReadonlyArray1<'py, i64>,
                           a: PyReadonlyArray2<'py, f64>, b: PyReadonlyArray2<'py, f64>, hw: PyReadonlyArray1<'py, f64>,
                           os: PyReadonlyArray1<'py, i64>, dz: f64) -> Bound<'py, PyArray1<bool>> {
    let (pv, opv, av, bv, hv, osv) = (p.as_array(), op.as_array(), a.as_array(), b.as_array(), hw.as_array(), os.as_array());
    let out: Vec<bool> = release(py, pv.shape()[0] * av.shape()[0].max(1), || {
        let ns = av.shape()[0];
        // per segment: start, direction, squared length, half width, z at both ends, owner
        let seg: Vec<[f64; 9]> = (0..ns).map(|k| {
            let (a0, a1, a2) = (av[[k, 0]], av[[k, 1]], av[[k, 2]]);
            let (ab0, ab1) = (bv[[k, 0]] - a0, bv[[k, 1]] - a1);
            [a0, a1, ab0, ab1, (ab0 * ab0 + ab1 * ab1).max(1e-9), hv[k], a2, bv[[k, 2]], osv[k] as f64]
        }).collect();
        (0..pv.shape()[0]).map(|i| {
            let (p0, p1, p2, o) = (pv[[i, 0]], pv[[i, 1]], pv[[i, 2]], opv[i] as f64);
            seg.iter().any(|g| {
                if g[8] == o { return false; }
                let t = (((p0 - g[0]) * g[2] + (p1 - g[1]) * g[3]) / g[4]).clamp(0.0, 1.0);
                let d = (p0 - (g[0] + t * g[2])).hypot(p1 - (g[1] + t * g[3]));
                d < g[5] && (p2 - (g[6] + t * (g[7] - g[6]))).abs() < dz
            })
        }).collect()
    });
    numpy::ndarray::Array1::from(out).into_pyarray(py)
}

/// Python's repr(round(x, nd)) for a finite float in fixed range (|x| < 1e16, nonzero |x| >= 1e-4): round correctly
/// (ties to even on the exact value, as float.__round__), then the shortest round-trip digits, ".0" for a whole number.
fn py_round_repr(x: f64, nd: usize, out: &mut String) {
    let v: f64 = format!("{:.*}", nd, x).parse().unwrap_or(x);
    let s = format!("{}", v);
    out.push_str(&s);
    if !s.contains('.') && !s.contains('e') && !s.contains("inf") && !s.contains("NaN") {
        out.push_str(".0");
    }
}

/// vegetation.tile_items' rows as json.dumps(rows, separators=(",", ":")) writes them: [[group, round(e, 2), round(n, 2),
/// round(z, 2), round(yaw, 1), round(scale, 3), round(height, 1), round(rank, 4)], ...], byte for byte.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn veg_rows_json<'py>(py: Python<'py>, group: PyReadonlyArray1<'py, i64>, e: PyReadonlyArray1<'py, f64>,
                      n: PyReadonlyArray1<'py, f64>, z: PyReadonlyArray1<'py, f64>, yaw: PyReadonlyArray1<'py, f64>,
                      scale: PyReadonlyArray1<'py, f64>, height: PyReadonlyArray1<'py, f64>,
                      rank: PyReadonlyArray1<'py, f64>) -> String {
    let (g, e, n, z, yaw, sc, h, r) = (group.as_array(), e.as_array(), n.as_array(), z.as_array(), yaw.as_array(),
                                       scale.as_array(), height.as_array(), rank.as_array());
    py.allow_threads(|| {
        let mut out = String::with_capacity(g.len() * 56 + 2);
        out.push('[');
        for i in 0..g.len() {
            if i > 0 {
                out.push(',');
            }
            out.push('[');
            out.push_str(&g[i].to_string());
            for (x, nd) in [(e[i], 2), (n[i], 2), (z[i], 2), (yaw[i], 1), (sc[i], 3), (h[i], 1), (r[i], 4)] {
                out.push(',');
                py_round_repr(x, nd, &mut out);
            }
            out.push(']');
        }
        out.push(']');
        out
    })
}

fn bc1_565(c: [f64; 3]) -> u16 {
    let r = (c[0].clamp(0.0, 255.0) * 31.0 / 255.0).round() as u16;
    let g = (c[1].clamp(0.0, 255.0) * 63.0 / 255.0).round() as u16;
    let b = (c[2].clamp(0.0, 255.0) * 31.0 / 255.0).round() as u16;
    (r << 11) | (g << 5) | b
}

fn bc1_rgb(x: u16) -> [i32; 3] {
    let r = ((x >> 11) & 31) as i32;
    let g = ((x >> 5) & 63) as i32;
    let b = (x & 31) as i32;
    [(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)]
}

/// One BC1 block (4-colour mode) of 16 RGB texels: endpoints along the block's principal axis (power iteration on
/// the covariance), refined once by least squares on the chosen indices, each texel the nearest of the 4 colours.
fn bc1_block(t: &[[i32; 3]; 16]) -> [u8; 8] {
    let mut mean = [0f64; 3];
    for k in 0..16 {
        for c in 0..3 {
            mean[c] += t[k][c] as f64 / 16.0;
        }
    }
    let mut cov = [[0f64; 3]; 3];
    for k in 0..16 {
        let d = [t[k][0] as f64 - mean[0], t[k][1] as f64 - mean[1], t[k][2] as f64 - mean[2]];
        for i in 0..3 {
            for j in 0..3 {
                cov[i][j] += d[i] * d[j];
            }
        }
    }
    let mut v = [0.577f64, 0.577, 0.577];
    for _ in 0..6 {
        let w = [cov[0][0] * v[0] + cov[0][1] * v[1] + cov[0][2] * v[2],
                 cov[1][0] * v[0] + cov[1][1] * v[1] + cov[1][2] * v[2],
                 cov[2][0] * v[0] + cov[2][1] * v[1] + cov[2][2] * v[2]];
        let n = (w[0] * w[0] + w[1] * w[1] + w[2] * w[2]).sqrt();
        if n < 1e-9 {
            break;
        }
        v = [w[0] / n, w[1] / n, w[2] / n];
    }
    let (mut pmin, mut pmax) = (f64::INFINITY, f64::NEG_INFINITY);
    for k in 0..16 {
        let p = (t[k][0] as f64 - mean[0]) * v[0] + (t[k][1] as f64 - mean[1]) * v[1] + (t[k][2] as f64 - mean[2]) * v[2];
        pmin = pmin.min(p);
        pmax = pmax.max(p);
    }
    let mut e0 = [mean[0] + v[0] * pmax, mean[1] + v[1] * pmax, mean[2] + v[2] * pmax];
    let mut e1 = [mean[0] + v[0] * pmin, mean[1] + v[1] * pmin, mean[2] + v[2] * pmin];
    let mut best: Option<(u64, u16, u16, u32)> = None;
    for pass in 0..2 {
        let (mut c0, mut c1) = (bc1_565(e0), bc1_565(e1));
        if c0 < c1 {
            std::mem::swap(&mut c0, &mut c1);
        }
        let (a, b) = (bc1_rgb(c0), bc1_rgb(c1));
        let pal: [[i32; 3]; 4] = if c0 == c1 {
            [a, a, a, a]
        } else {
            [a, b, [(2 * a[0] + b[0]) / 3, (2 * a[1] + b[1]) / 3, (2 * a[2] + b[2]) / 3],
             [(a[0] + 2 * b[0]) / 3, (a[1] + 2 * b[1]) / 3, (a[2] + 2 * b[2]) / 3]]
        };
        let mut idx = 0u32;
        let mut err = 0u64;
        let mut sel = [0usize; 16];
        for k in 0..16 {
            let mut bi = 0usize;
            let mut be = i32::MAX;
            for (pi, p) in pal.iter().enumerate() {
                let d = (t[k][0] - p[0]).pow(2) + (t[k][1] - p[1]).pow(2) + (t[k][2] - p[2]).pow(2);
                if d < be {
                    be = d;
                    bi = pi;
                }
            }
            sel[k] = bi;
            err += be as u64;
            idx |= (bi as u32) << (2 * k);
        }
        if best.map_or(true, |b| err < b.0) {
            best = Some((err, c0, c1, idx));
        }
        if pass == 1 || c0 == c1 {
            break;
        }
        // least squares on the chosen weights (w of e0: 1, 0, 2/3, 1/3)
        const W: [f64; 4] = [1.0, 0.0, 2.0 / 3.0, 1.0 / 3.0];
        let (mut aa, mut bb, mut ab) = (0f64, 0f64, 0f64);
        let mut ax = [0f64; 3];
        let mut bx = [0f64; 3];
        for k in 0..16 {
            let wa = W[sel[k]];
            let wb = 1.0 - wa;
            aa += wa * wa;
            bb += wb * wb;
            ab += wa * wb;
            for c in 0..3 {
                ax[c] += wa * t[k][c] as f64;
                bx[c] += wb * t[k][c] as f64;
            }
        }
        let det = aa * bb - ab * ab;
        if det.abs() < 1e-9 {
            break;
        }
        for c in 0..3 {
            e0[c] = (bb * ax[c] - ab * bx[c]) / det;
            e1[c] = (aa * bx[c] - ab * ax[c]) / det;
        }
    }
    let (_, c0, c1, idx) = best.unwrap();
    let mut out = [0u8; 8];
    out[0..2].copy_from_slice(&c0.to_le_bytes());
    out[2..4].copy_from_slice(&c1.to_le_bytes());
    out[4..8].copy_from_slice(&idx.to_le_bytes());
    out
}

/// BC1 (DXT1, 4-colour) blocks of an (h, w, 3) uint8 image, row-major, edges padded by repetition.
#[pyfunction]
fn bc1_encode<'py>(py: Python<'py>, img: PyReadonlyArray3<'py, u8>) -> Bound<'py, PyBytes> {
    let a = img.as_array();
    let out = py.allow_threads(|| {
        let (h, w) = (a.shape()[0], a.shape()[1]);
        if h == 0 || w == 0 {
            return Vec::new();
        }
        let (nby, nbx) = ((h + 3) / 4, (w + 3) / 4);
        let mut out = vec![0u8; nby * nbx * 8];
        let mut t = [[0i32; 3]; 16];
        for by in 0..nby {
            for bx in 0..nbx {
                for yy in 0..4 {
                    let y = (by * 4 + yy).min(h - 1);
                    for xx in 0..4 {
                        let x = (bx * 4 + xx).min(w - 1);
                        for c in 0..3 {
                            t[yy * 4 + xx][c] = a[[y, x, c]] as i32;
                        }
                    }
                }
                let o = (by * nbx + bx) * 8;
                out[o..o + 8].copy_from_slice(&bc1_block(&t));
            }
        }
        out
    });
    PyBytes::new(py, &out)
}

/// Pillow's Image.transform(MESH, BILINEAR) of an RGBA image (H, W, 4) onto a px x px output, exactly (Pillow warps
/// RGBA premultiplied: RGBA -> RGBa (MULDIV255), the warp, RGBa -> RGBA (255 c / a, clipped)): the mesh is
/// cells x cells boxes of cw px, cell (r, c) the quad nw (qx[r, c], qy[r, c]), sw [r + 1, c], se [r + 1, c + 1],
/// ne [r, c + 1]; Pillow's QUAD coefficients (Image.__transformer), quad_transform at pixel centres and
/// bilinear_filter32RGB (taps clipped to the image, 0 outside it, the value truncated to uint8).
#[pyfunction]
fn mesh_warp<'py>(py: Python<'py>, img: PyReadonlyArray3<'py, u8>, qx: PyReadonlyArray2<'py, f64>,
                  qy: PyReadonlyArray2<'py, f64>, cells: usize, cw: usize, px: usize)
                  -> Bound<'py, numpy::PyArray3<u8>> {
    let (a, qx, qy) = (img.as_array(), qx.as_array(), qy.as_array());
    let out = py.allow_threads(|| {
        let (ih, iw, bands) = (a.shape()[0] as i64, a.shape()[1] as i64, a.shape()[2]);
        let mut out = numpy::ndarray::Array3::<u8>::zeros((px, px, bands));
        let clipx = |x: i64| -> usize { (if x < 0 { 0 } else if x < iw { x } else { iw - 1 }) as usize };
        let clipy = |y: i64| -> usize { (if y < 0 { 0 } else if y < ih { y } else { ih - 1 }) as usize };
        for r in 0..cells {
            for c in 0..cells {
                let (bx0, by0) = (c * cw, r * cw);
                let (bx1, by1) = (((c + 1) * cw).min(px), ((r + 1) * cw).min(px));
                let (w, h) = ((c + 1) * cw - c * cw, (r + 1) * cw - r * cw);
                let (nwx, nwy) = (qx[[r, c]], qy[[r, c]]);
                let (swx, swy) = (qx[[r + 1, c]], qy[[r + 1, c]]);
                let (sex, sey) = (qx[[r + 1, c + 1]], qy[[r + 1, c + 1]]);
                let (nex, ney) = (qx[[r, c + 1]], qy[[r, c + 1]]);
                let as_ = 1.0 / w as f64;
                let at = 1.0 / h as f64;
                let a0 = nwx;
                let a1 = (nex - nwx) * as_;
                let a2 = (swx - nwx) * at;
                let a3 = (sex - swx - nex + nwx) * as_ * at;
                let a4 = nwy;
                let a5 = (ney - nwy) * as_;
                let a6 = (swy - nwy) * at;
                let a7 = (sey - swy - ney + nwy) * as_ * at;
                for y in by0..by1 {
                    for x in bx0..bx1 {
                        let xin = (x - bx0) as f64 + 0.5;
                        let yin = (y - by0) as f64 + 0.5;
                        let mut xx = a0 + a1 * xin + a2 * yin + a3 * xin * yin;
                        let mut yy = a4 + a5 * xin + a6 * yin + a7 * xin * yin;
                        if xx < 0.0 || xx >= iw as f64 || yy < 0.0 || yy >= ih as f64 {
                            continue;                       // fill: 0
                        }
                        xx -= 0.5;
                        yy -= 0.5;
                        let xi = if xx < 0.0 { xx.floor() as i64 } else { xx as i64 };
                        let yi = if yy < 0.0 { yy.floor() as i64 } else { yy as i64 };
                        let dx = xx - xi as f64;
                        let dy = yy - yi as f64;
                        let (x0, x1) = (clipx(xi), clipx(xi + 1));
                        let row0 = clipy(yi);
                        let has1 = yi + 1 >= 0 && yi + 1 < ih;
                        // the premultiplied source value (Pillow's rgbA2rgba: MULDIV255 for colour, alpha as is)
                        let pm = |row: usize, col: usize, b: usize| -> f64 {
                            let v = a[[row, col, b]] as u32;
                            if b == 3 || bands < 4 {
                                return v as f64;
                            }
                            let t = v * a[[row, col, 3]] as u32 + 128;
                            (((t >> 8) + t) >> 8) as f64
                        };
                        let mut px_ = [0u8; 4];
                        for b in 0..bands {
                            let p0 = pm(row0, x0, b);
                            let p1 = pm(row0, x1, b);
                            let v1 = p0 + (p1 - p0) * dx;
                            let v2 = if has1 {
                                let q0 = pm((yi + 1) as usize, x0, b);
                                let q1 = pm((yi + 1) as usize, x1, b);
                                q0 + (q1 - q0) * dx
                            } else {
                                v1
                            };
                            px_[b] = (v1 + (v2 - v1) * dy) as u8;
                        }
                        // back from premultiplied (Pillow's rgba2rgbA)
                        if bands == 4 {
                            let al = px_[3] as i32;
                            if al != 255 && al != 0 {
                                for b in 0..3 {
                                    px_[b] = ((255 * px_[b] as i32) / al).clamp(0, 255) as u8;
                                }
                            }
                        }
                        for b in 0..bands {
                            out[[y, x, b]] = px_[b];
                        }
                    }
                }
            }
        }
        out
    });
    out.into_pyarray(py)
}

/// The terrain block's height at (x, y) from its samples grid[j, i] (lattice index i0 + i, j0 + j, every cell m): each
/// square cut from its south-west corner to its north-east one, as the engine draws it. NaN outside the grid.
#[inline]
fn terrain_at(grid: &numpy::ndarray::ArrayView2<f64>, i0: i64, j0: i64, cell: f64, x: f64, y: f64) -> f64 {
    let (gx, gy) = (x / cell, y / cell);
    let (mut i, mut j) = (gx.floor() as i64, gy.floor() as i64);
    let (ny, nx) = (grid.shape()[0] as i64, grid.shape()[1] as i64);
    // a point on the grid's last line belongs to the square before it
    if i - i0 == nx - 1 && gx == (i as f64) { i -= 1; }
    if j - j0 == ny - 1 && gy == (j as f64) { j -= 1; }
    let (li, lj) = (i - i0, j - j0);
    if li < 0 || lj < 0 || li + 1 >= nx || lj + 1 >= ny { return f64::NAN; }
    let (fx, fy) = (gx - i as f64, gy - j as f64);
    let (li, lj) = (li as usize, lj as usize);
    let (a, b, c, d) = (grid[[lj, li]], grid[[lj, li + 1]], grid[[lj + 1, li]], grid[[lj + 1, li + 1]]);
    if fy > fx { a + fy * (c - a) + fx * (d - c) } else { a + fx * (b - a) + fy * (d - b) }
}

/// roadmesh.build's road surface on the terrain block: triangles (t, 3) over points (n, 2), uvs (n, 2), each given
/// the terrain's heights at its corners (grid: as terrain_at); where the ground inside departs from that plane by more
/// than tol (checked at the grid points inside and where its edges cross the terrain's edges) it is cut along the
/// terrain's edges and every piece takes the terrain's heights: within tol of the drawn ground
/// everywhere. Returns (points (m, 3), uvs (m, 2), triangles (r, 3), source triangle (r,)); points not shared.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn follow_tris<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, uv: PyReadonlyArray2<'py, f64>,
                    tris: PyReadonlyArray2<'py, i64>, cell: f64, grid: PyReadonlyArray2<'py, f64>, i0: i64, j0: i64,
                    tol: f64)
                    -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>,
                        Bound<'py, PyArray1<i64>>) {
    let (pv, uvv, tv, gv) = (pts.as_array(), uv.as_array(), tris.as_array(), grid.as_array());
    let (p_out, uv_out, t_out, src) = release(py, tv.shape()[0] * 200, || {
        type V = [f64; 4];
        let cell = if cell > 0.0 { cell } else { 2.0 };
        let fams: [(f64, f64); 3] = [(1.0, 0.0), (0.0, 1.0), (-1.0, 1.0)];
        let ter = |x: f64, y: f64| terrain_at(&gv, i0, j0, cell, x, y);
        let mut p_out: Vec<[f64; 3]> = Vec::new();
        let mut uv_out: Vec<[f64; 2]> = Vec::new();
        let mut t_out: Vec<[i64; 3]> = Vec::new();
        let mut src: Vec<i64> = Vec::new();
        let lerp = |a: &V, b: &V, f: f64| -> V {
            [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, a[3] + (b[3] - a[3]) * f]
        };
        for ti in 0..tv.shape()[0] {
            let tri: Vec<V> = (0..3).map(|k| {
                let i = tv[[ti, k]] as usize;
                [pv[[i, 0]], pv[[i, 1]], uvv[[i, 0]], uvv[[i, 1]]]
            }).collect();
            let z: Vec<f64> = tri.iter().map(|q| ter(q[0], q[1])).collect();
            // the plane through the corners: z = z0 + s (p1 - p0) + t (p2 - p0) in barycentric terms
            let (x0, y0, x1, y1, x2, y2) = (tri[0][0], tri[0][1], tri[1][0], tri[1][1], tri[2][0], tri[2][1]);
            let det = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0);
            let mut split = !z.iter().all(|v| v.is_finite()) || det.abs() < 1e-12;
            if !split {
                let plane = |x: f64, y: f64| {
                    let s = ((x - x0) * (y2 - y0) - (x2 - x0) * (y - y0)) / det;
                    let t = ((x1 - x0) * (y - y0) - (x - x0) * (y1 - y0)) / det;
                    (z[0] + s * (z[1] - z[0]) + t * (z[2] - z[0]), s, t)
                };
                let dev = |x: f64, y: f64| { let tz = ter(x, y); if tz.is_finite() { (tz - plane(x, y).0).abs() } else { f64::INFINITY } };
                // the grid points inside
                let (lx, hx) = (x0.min(x1).min(x2), x0.max(x1).max(x2));
                let (ly, hy) = (y0.min(y1).min(y2), y0.max(y1).max(y2));
                'outer: for gi in ((lx / cell).ceil() as i64)..=((hx / cell).floor() as i64) {
                    for gj in ((ly / cell).ceil() as i64)..=((hy / cell).floor() as i64) {
                        let (x, y) = (gi as f64 * cell, gj as f64 * cell);
                        let (_, s, t) = plane(x, y);
                        if s >= -1e-9 && t >= -1e-9 && s + t <= 1.0 + 1e-9 && dev(x, y) > tol { split = true; break 'outer; }
                    }
                }
                // where its edges cross the terrain's edges
                if !split {
                    'edges: for e in 0..3 {
                        let (a, b) = (&tri[e], &tri[(e + 1) % 3]);
                        for &(nx, ny) in fams.iter() {
                            let (da, db) = (nx * a[0] + ny * a[1], nx * b[0] + ny * b[1]);
                            let (lo, hi) = (da.min(db), da.max(db));
                            for k in ((lo / cell).floor() as i64 + 1)..=((hi / cell).ceil() as i64 - 1) {
                                let line = k as f64 * cell;
                                if line <= lo || line >= hi { continue; }
                                let f = (line - da) / (db - da);
                                let q = lerp(a, b, f);
                                if dev(q[0], q[1]) > tol { split = true; break 'edges; }
                            }
                        }
                    }
                }
            }
            if !split {
                let base = p_out.len() as i64;
                for (k, q) in tri.iter().enumerate() {
                    p_out.push([q[0], q[1], z[k]]);
                    uv_out.push([q[2], q[3]]);
                }
                t_out.push([base, base + 1, base + 2]);
                src.push(ti as i64);
                continue;
            }
            let mut polys: Vec<Vec<V>> = vec![tri];
            for &(nx, ny) in fams.iter() {
                let mut next: Vec<Vec<V>> = Vec::new();
                for poly in polys.into_iter() {
                    let d: Vec<f64> = poly.iter().map(|q| nx * q[0] + ny * q[1]).collect();
                    let (lo, hi) = d.iter().fold((f64::INFINITY, f64::NEG_INFINITY), |a, &x| (a.0.min(x), a.1.max(x)));
                    let mut rest = poly;
                    for k in ((lo / cell).floor() as i64 + 1)..=((hi / cell).ceil() as i64 - 1) {
                        let line = k as f64 * cell;
                        if line <= lo + 1e-9 || line >= hi - 1e-9 { continue; }
                        let n = rest.len();
                        let mut below: Vec<V> = Vec::new();
                        let mut above: Vec<V> = Vec::new();
                        for j in 0..n {
                            let (a, b) = (&rest[j], &rest[(j + 1) % n]);
                            let (da, db) = (nx * a[0] + ny * a[1] - line, nx * b[0] + ny * b[1] - line);
                            if da <= 0.0 { below.push(*a); }
                            if da >= 0.0 { above.push(*a); }
                            if (da < 0.0 && db > 0.0) || (da > 0.0 && db < 0.0) {
                                let x = lerp(a, b, da / (da - db));
                                below.push(x);
                                above.push(x);
                            }
                        }
                        if below.len() >= 3 { next.push(below); }
                        rest = above;
                        if rest.len() < 3 { break; }
                    }
                    if rest.len() >= 3 { next.push(rest); }
                }
                polys = next;
            }
            for poly in polys.iter() {
                let area: f64 = (0..poly.len()).map(|j| {
                    let (a, b) = (&poly[j], &poly[(j + 1) % poly.len()]);
                    a[0] * b[1] - b[0] * a[1]
                }).sum::<f64>() / 2.0;
                if area.abs() < 1e-8 { continue; }
                let base = p_out.len() as i64;
                for q in poly.iter() {
                    p_out.push([q[0], q[1], ter(q[0], q[1])]);
                    uv_out.push([q[2], q[3]]);
                }
                for a in 1..(poly.len() as i64 - 1) {
                    t_out.push([base, base + a, base + a + 1]);
                    src.push(ti as i64);
                }
            }
        }
        (p_out, uv_out, t_out, src)
    });
    let pa = numpy::ndarray::Array2::from_shape_vec((p_out.len(), 3), p_out.into_iter().flatten().collect()).unwrap();
    let ua = numpy::ndarray::Array2::from_shape_vec((uv_out.len(), 2), uv_out.into_iter().flatten().collect()).unwrap();
    let ta = numpy::ndarray::Array2::from_shape_vec((t_out.len(), 3), t_out.into_iter().flatten().collect()).unwrap();
    (pa.into_pyarray(py), ua.into_pyarray(py), ta.into_pyarray(py), src.into_pyarray(py))
}

#[pymodule]
fn earthfast(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(island_ring_rays, m)?)?;
    m.add_function(wrap_pyfunction!(points_inside, m)?)?;
    m.add_function(wrap_pyfunction!(polyline_dist, m)?)?;
    m.add_function(wrap_pyfunction!(clear_of, m)?)?;
    m.add_function(wrap_pyfunction!(dual_side, m)?)?;
    m.add_function(wrap_pyfunction!(deck_crossings, m)?)?;
    m.add_function(wrap_pyfunction!(point_inside, m)?)?;
    m.add_function(wrap_pyfunction!(geodetic_to_enu, m)?)?;
    m.add_function(wrap_pyfunction!(enu_to_geodetic, m)?)?;
    m.add_function(wrap_pyfunction!(bc7_encode, m)?)?;
    m.add_function(wrap_pyfunction!(inside_blockers, m)?)?;
    m.add_function(wrap_pyfunction!(densify, m)?)?;
    m.add_function(wrap_pyfunction!(measure_row, m)?)?;
    m.add_function(wrap_pyfunction!(measure_row32, m)?)?;
    m.add_function(wrap_pyfunction!(grade_points, m)?)?;
    m.add_function(wrap_pyfunction!(resample, m)?)?;
    m.add_function(wrap_pyfunction!(tunnel_sections, m)?)?;
    m.add_function(wrap_pyfunction!(smooth_profile, m)?)?;
    m.add_function(wrap_pyfunction!(seg_dist_min, m)?)?;
    m.add_function(wrap_pyfunction!(contains_points, m)?)?;
    m.add_function(wrap_pyfunction!(rock, m)?)?;
    m.add_function(wrap_pyfunction!(mip_levels, m)?)?;
    m.add_function(wrap_pyfunction!(srgb_monotonic, m)?)?;
    m.add_function(wrap_pyfunction!(loop_crossed, m)?)?;
    m.add_function(wrap_pyfunction!(any_within, m)?)?;
    m.add_function(wrap_pyfunction!(shared_walls, m)?)?;
    m.add_function(wrap_pyfunction!(season_grade, m)?)?;
    m.add_function(wrap_pyfunction!(tangents, m)?)?;
    m.add_function(wrap_pyfunction!(season_noise, m)?)?;
    m.add_function(wrap_pyfunction!(terrarium_heights, m)?)?;
    m.add_function(wrap_pyfunction!(rf_pass, m)?)?;
    m.add_function(wrap_pyfunction!(vmedian, m)?)?;
    m.add_function(wrap_pyfunction!(bc1_encode, m)?)?;
    m.add_function(wrap_pyfunction!(mesh_warp, m)?)?;
    m.add_function(wrap_pyfunction!(seam_pass, m)?)?;
    m.add_function(wrap_pyfunction!(view_distances, m)?)?;
    m.add_function(wrap_pyfunction!(road_corridors, m)?)?;
    m.add_function(wrap_pyfunction!(horizon_steps, m)?)?;
    m.add_function(wrap_pyfunction!(tunnel_holes, m)?)?;
    m.add_function(wrap_pyfunction!(arch_raise_part, m)?)?;
    m.add_function(wrap_pyfunction!(subdivide_tris, m)?)?;
    m.add_function(wrap_pyfunction!(deck_edges_covered, m)?)?;
    m.add_function(wrap_pyfunction!(points_near_segments, m)?)?;
    m.add_function(wrap_pyfunction!(over_tunnel, m)?)?;
    m.add_function(wrap_pyfunction!(veg_rows_json, m)?)?;
    m.add_function(wrap_pyfunction!(follow_tris, m)?)?;
    m.add("VERSION", 34)?;
    Ok(())
}
