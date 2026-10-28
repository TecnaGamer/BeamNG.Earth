//! Geometry kernels for the world server (server/fast.py wraps them, with the Python versions as the fallback).
//! All coordinates are metres in the anchor's local frame, f64.
mod gpu;
mod dsm;
mod vegmask;
mod roadgeom;
mod shore;
mod photofill;
mod ambience;
mod power;
mod underpass;
mod gradesep;
mod lamps;

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
    let n = px.len();
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
    a.rows().into_iter().map(|r| [r[0], r[1], r[2], r[3]]).collect()
}

/// islands._island_ring's ray march: from (cx, cy) along `rays` evenly spaced directions, in steps of `step` up to
/// rmax, the last point inside poly and clear of every segment's half width. Returns (rays, 2).
#[pyfunction]
fn island_ring_rays<'py>(py: Python<'py>, poly: PyReadonlyArray2<'py, f64>, segs: PyReadonlyArray2<'py, f64>,
                         hw: PyReadonlyArray1<'py, f64>, cx: f64, cy: f64, rays: usize, step: f64, rmax: f64)
                         -> Bound<'py, PyArray2<f64>> {
    let p = poly.as_array();
    let px: Vec<f64> = p.column(0).to_vec();
    let pys: Vec<f64> = p.column(1).to_vec();
    let s = to_segs(&segs);
    let h = hw.as_slice().unwrap().to_vec();
    let out = release(py, px.len() * s.len().max(1), || {
        let mut out = Vec::with_capacity(rays * 2);
        for k in 0..rays {
            let a = 2.0 * std::f64::consts::PI * (k as f64) / (rays as f64);
            let (ux, uy) = (a.cos(), a.sin());
            let mut r = 0.0;
            while r < rmax {
                let (qx, qy) = (cx + ux * (r + step), cy + uy * (r + step));
                if !inside(&px, &pys, qx, qy) || near_any(&s, &h, qx, qy) {
                    break;
                }
                r += step;
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
    let px: Vec<f64> = p.column(0).to_vec();
    let pys: Vec<f64> = p.column(1).to_vec();
    let q = pts.as_array();
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
    let h = half.as_slice().unwrap();
    let o = owner.as_slice().unwrap();
    for i in 0..s.nrows() {
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
    if ka < 2 || kb < 2 {
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
    if total < 1.0 {
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

/// The GPU the kernels run on ("name (backend)"), or None (no usable GPU, or BNGE_GPU=0): callers keep their CPU
/// path. Made on first use.
#[pyfunction]
fn gpu_name(py: Python<'_>) -> Option<String> {
    py.allow_threads(|| gpu::get().map(|g| g.name.clone()))
}

/// colormatch.detect_seams' angle loop on the GPU (gpu.rs): src (7, h, w) float32 = [w, w*L_0 .. w*L_5]. Returns
/// (best [h, w], ang [h, w], D [h, w, 3]) float32. RuntimeError when there is no GPU or it failed.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn seam_detect_gpu<'py>(py: Python<'py>, src: PyReadonlyArray3<'py, f32>, n_ang: usize, half: i32, depth: i32,
                        offs: Vec<i32>, full: f32, gate: f32, agree: f32, min_valid: u32)
                        -> PyResult<(Bound<'py, PyArray2<f32>>, Bound<'py, PyArray2<f32>>, Bound<'py, numpy::PyArray3<f32>>)> {
    let v = src.as_array();
    let (c, h, w) = (v.shape()[0], v.shape()[1], v.shape()[2]);
    if c != 7 {
        return Err(pyo3::exceptions::PyValueError::new_err("seam_detect_gpu: src must be (7, h, w)"));
    }
    let data: Vec<f32> = v.iter().copied().collect();
    let out = py.allow_threads(|| {
        let g = gpu::get().ok_or_else(|| "no GPU".to_string())?;
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            gpu::seam_detect(g, &data, h, w, n_ang, half, depth, &offs, full, gate, agree, min_valid)
        })).unwrap_or_else(|_| Err("the GPU kernel panicked".to_string()))
    }).map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
    let n = h * w;
    let best = numpy::ndarray::Array2::from_shape_vec((h, w), out[..n].to_vec()).unwrap();
    let ang = numpy::ndarray::Array2::from_shape_vec((h, w), out[n..2 * n].to_vec()).unwrap();
    let mut d = numpy::ndarray::Array3::<f32>::zeros((h, w, 3));
    for ch in 0..3 {
        for i in 0..n {
            d[[i / w, i % w, ch]] = out[(2 + ch) * n + i];
        }
    }
    Ok((best.into_pyarray(py), ang.into_pyarray(py), d.into_pyarray(py)))
}

/// BC1 blocks of several images (a texture's mip levels: [(h, w, 3) uint8]) on the GPU (gpu.rs): one bytes object
/// per image, as bc1_encode's (float32 maths: a few blocks can differ from it). RuntimeError: no GPU or it failed.
#[pyfunction]
fn bc1_encode_gpu<'py>(py: Python<'py>, imgs: Vec<PyReadonlyArray3<'py, u8>>) -> PyResult<Vec<Bound<'py, PyBytes>>> {
    let data: Vec<(usize, usize, Vec<u8>)> = imgs.iter().map(|a| {
        let v = a.as_array();
        (v.shape()[1], v.shape()[0], v.iter().copied().collect())
    }).collect();
    let out = py.allow_threads(|| {
        let g = gpu::get().ok_or_else(|| "no GPU".to_string())?;
        let refs: Vec<(usize, usize, &[u8])> = data.iter().map(|(w, h, b)| (*w, *h, b.as_slice())).collect();
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| gpu::bc1_encode(g, &refs)))
            .unwrap_or_else(|_| Err("the GPU kernel panicked".to_string()))
    }).map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
    Ok(out.iter().map(|b| PyBytes::new(py, b)).collect())
}

/// roads._crossings: (part index, arc m along it, x, y) where a ground road crosses a deck part. Parts and ground
/// lines are polylines one after another (pts (n, 2), starts (L + 1,), the last: n); per part, per ground line whose
/// box meets the part's, per ground segment in order, the part's segments in order: numpy's float64 arithmetic of the
/// Python it replaces (the underpass and bridge-approach planning's hot loop, 2026-10-22).
#[pyfunction]
fn segment_crossings(
    py: Python<'_>,
    parts: PyReadonlyArray2<f64>,
    part_starts: PyReadonlyArray1<i64>,
    ground: PyReadonlyArray2<f64>,
    ground_starts: PyReadonlyArray1<i64>,
) -> Vec<(usize, f64, f64, f64)> {
    segment_crossings_g(py, parts, part_starts, ground, ground_starts)
        .into_iter().map(|(pi, _g, arc, x, y)| (pi, arc, x, y)).collect()
}

/// segment_crossings with each crossing's ground line too: (part index, ground line index, arc m, x, y), in the same
/// order (per part, per ground line, ...): one call for many ground lines, each line's crossings as its own call's
/// (Grader._lower_underpasses called it once per road plan, ~8600 calls per 16 city road tiles, 2026-10-26).
#[pyfunction]
fn segment_crossings_g(
    py: Python<'_>,
    parts: PyReadonlyArray2<f64>,
    part_starts: PyReadonlyArray1<i64>,
    ground: PyReadonlyArray2<f64>,
    ground_starts: PyReadonlyArray1<i64>,
) -> Vec<(usize, usize, f64, f64, f64)> {
    let pa = parts.as_array();
    let ps = part_starts.as_array();
    let ga = ground.as_array();
    let gs = ground_starts.as_array();
    let np_ = ps.len().saturating_sub(1);
    let ng = gs.len().saturating_sub(1);
    py.allow_threads(|| {
        let bbox = |a: &numpy::ndarray::ArrayView2<f64>, i0: usize, i1: usize| {
            let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
            for k in i0..i1 {
                x0 = x0.min(a[[k, 0]]); x1 = x1.max(a[[k, 0]]);
                y0 = y0.min(a[[k, 1]]); y1 = y1.max(a[[k, 1]]);
            }
            (x0, y0, x1, y1)
        };
        let gbox: Vec<_> = (0..ng).map(|g| bbox(&ga, gs[g] as usize, gs[g + 1] as usize)).collect();
        let mut out = Vec::new();
        for pi in 0..np_ {
            let (i0, i1) = (ps[pi] as usize, ps[pi + 1] as usize);
            if i1 < i0 + 2 { continue; }
            let (lx0, ly0, lx1, ly1) = bbox(&pa, i0, i1);
            let mut s = vec![0.0f64; i1 - i0];
            for k in 1..(i1 - i0) {
                let dx = pa[[i0 + k, 0]] - pa[[i0 + k - 1, 0]];
                let dy = pa[[i0 + k, 1]] - pa[[i0 + k - 1, 1]];
                s[k] = s[k - 1] + dx.hypot(dy);
            }
            for g in 0..ng {
                let (gx0, gy0, gx1, gy1) = gbox[g];
                if gx1 < lx0 || gx0 > lx1 || gy1 < ly0 || gy0 > ly1 { continue; }
                let (j0, j1) = (gs[g] as usize, gs[g + 1] as usize);
                for k in j0..j1.saturating_sub(1) {
                    let (ax, ay) = (ga[[k, 0]], ga[[k, 1]]);
                    let (dx, dy) = (ga[[k + 1, 0]] - ax, ga[[k + 1, 1]] - ay);
                    for j in 0..(i1 - i0 - 1) {
                        let (px, py_) = (pa[[i0 + j, 0]], pa[[i0 + j, 1]]);
                        let (rx, ry) = (pa[[i0 + j + 1, 0]] - px, pa[[i0 + j + 1, 1]] - py_);
                        let den = rx * dy - ry * dx;
                        if !(den.abs() > 1e-12) { continue; }
                        let (apx, apy) = (ax - px, ay - py_);
                        let t = (apx * dy - apy * dx) / den;
                        let u = (apx * ry - apy * rx) / den;
                        if t >= 0.0 && t <= 1.0 && u >= 0.0 && u <= 1.0 {
                            out.push((pi, g, s[j] + t * (s[j + 1] - s[j]), px + t * rx, py_ + t * ry));
                        }
                    }
                }
            }
        }
        out
    })
}

/// roads._seg_hits for many ground lines: (line index, arc m along m, sin of the angle) where each of the polylines
/// (pts (n, 2), starts (L + 1,), the last: n) crosses polyline m (k, 2); per line, per segment of m, per segment of the
/// line in order: numpy's float64 arithmetic of the Python it replaces (_bridge_reach's loop, 2026-10-25).
#[pyfunction]
fn seg_hits(py: Python<'_>, m: PyReadonlyArray2<f64>, pts: PyReadonlyArray2<f64>, starts: PyReadonlyArray1<i64>)
            -> Vec<(usize, f64, f64)> {
    let ma = m.as_array();
    let ga = pts.as_array();
    let gs = starts.as_array();
    let nm = ma.nrows();
    let ng = gs.len().saturating_sub(1);
    py.allow_threads(|| {
        let mut out = Vec::new();
        if nm < 2 { return out; }
        let mut sm = vec![0.0f64; nm];
        for k in 1..nm {
            sm[k] = sm[k - 1] + (ma[[k, 0]] - ma[[k - 1, 0]]).hypot(ma[[k, 1]] - ma[[k - 1, 1]]);
        }
        let (mut mx0, mut my0, mut mx1, mut my1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for k in 0..nm {
            mx0 = mx0.min(ma[[k, 0]]); mx1 = mx1.max(ma[[k, 0]]);
            my0 = my0.min(ma[[k, 1]]); my1 = my1.max(ma[[k, 1]]);
        }
        for g in 0..ng {
            let (j0, j1) = (gs[g] as usize, gs[g + 1] as usize);
            if j1 < j0 + 2 { continue; }
            let (mut gx0, mut gy0, mut gx1, mut gy1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
            for k in j0..j1 {
                gx0 = gx0.min(ga[[k, 0]]); gx1 = gx1.max(ga[[k, 0]]);
                gy0 = gy0.min(ga[[k, 1]]); gy1 = gy1.max(ga[[k, 1]]);
            }
            if gx1 < mx0 || gx0 > mx1 || gy1 < my0 || gy0 > my1 { continue; }
            for k in 0..nm - 1 {
                let (px, py_) = (ma[[k, 0]], ma[[k, 1]]);
                let (rx, ry) = (ma[[k + 1, 0]] - px, ma[[k + 1, 1]] - py_);
                for n in j0..j1 - 1 {
                    let (cx, cy) = (ga[[n, 0]], ga[[n, 1]]);
                    let (sx, sy) = (ga[[n + 1, 0]] - cx, ga[[n + 1, 1]] - cy);
                    let den = rx * sy - ry * sx;
                    if !(den.abs() > 1e-12) { continue; }
                    let (wx, wy) = (cx - px, cy - py_);
                    let t = (wx * sy - wy * sx) / den;
                    let u = (wx * ry - wy * rx) / den;
                    if t >= 0.0 && t <= 1.0 && u >= 0.0 && u <= 1.0 {
                        let sin = den.abs() / (rx.hypot(ry) * sx.hypot(sy)).max(1e-12);
                        out.push((g, sm[k] + t * (sm[k + 1] - sm[k]), sin));
                    }
                }
            }
        }
        out
    })
}

/// roads._under_decks: which points (xy (k, 2), road heights z (k,)) lie under a bridge deck: per deck chunk (its
/// points pts (n, 3) one chunk after another, starts (C + 1,)), the point's nearest segment (the first of equals, as
/// numpy's argmin of _seg_dist) within reach (C,) and the deck's lower end there over (or more) above z. numpy's
/// float64 arithmetic of the Python it replaces, bit for bit (retaining_walls' largest cost, 2026-10-25).
#[pyfunction]
fn under_decks(py: Python<'_>, xy: PyReadonlyArray2<f64>, z: PyReadonlyArray1<f64>, pts: PyReadonlyArray2<f64>,
               starts: PyReadonlyArray1<i64>, reach: PyReadonlyArray1<f64>, over: f64) -> Vec<bool> {
    let (p, z, d, st, r) = (xy.as_array(), z.as_array(), pts.as_array(), starts.as_array(), reach.as_array());
    let k = p.nrows();
    let nc = st.len().saturating_sub(1);
    py.allow_threads(|| {
        let mut out = vec![false; k];
        for c in 0..nc {
            let (j0, j1) = (st[c] as usize, st[c + 1] as usize);
            if j1 < j0 + 2 { continue; }
            let segs: Vec<[f64; 5]> = (j0..j1 - 1).map(|j| {
                let (ax, ay) = (d[[j, 0]], d[[j, 1]]);
                let (bx, by) = (d[[j + 1, 0]] - ax, d[[j + 1, 1]] - ay);
                [ax, ay, bx, by, (bx * bx + by * by).max(1e-12)]
            }).collect();
            for i in 0..k {
                if out[i] { continue; }
                let (px, py_) = (p[[i, 0]], p[[i, 1]]);
                let (mut best, mut bj) = (f64::INFINITY, usize::MAX);
                for (n, g) in segs.iter().enumerate() {
                    let (apx, apy) = (px - g[0], py_ - g[1]);
                    let t = ((apx * g[2] + apy * g[3]) / g[4]).clamp(0.0, 1.0);
                    let dd = (px - (g[0] + t * g[2])).hypot(py_ - (g[1] + t * g[3]));
                    if dd < best || bj == usize::MAX { best = dd; bj = n; }
                }
                if bj == usize::MAX || !(best <= r[c]) { continue; }
                let j = j0 + bj;
                let dz = d[[j, 2]].min(d[[j + 1, 2]]) - z[i];
                if dz >= over { out[i] = true; }
            }
        }
        out
    })
}

/// cdae.face_normal_sum: vertex normals as the normalised sum of the adjacent faces' unit normals (positions (n, 3),
/// tris (m, 3)); numpy's float64 order: np.cross, np.linalg.norm, np.add.at over the first corners, then the second,
/// then the third (a mesh write's largest cost, 2026-10-25).
#[pyfunction]
fn face_normal_sum<'py>(py: Python<'py>, positions: PyReadonlyArray2<'py, f64>, tris: PyReadonlyArray2<'py, i64>)
                        -> Bound<'py, PyArray2<f64>> {
    let (p, t) = (positions.as_array(), tris.as_array());
    let (n, m) = (p.nrows(), t.nrows());
    let acc = py.allow_threads(|| {
        let mut fnrm = vec![[0.0f64; 3]; m];
        for i in 0..m {
            let (a, b, c) = (t[[i, 0]] as usize, t[[i, 1]] as usize, t[[i, 2]] as usize);
            let e1 = [p[[b, 0]] - p[[a, 0]], p[[b, 1]] - p[[a, 1]], p[[b, 2]] - p[[a, 2]]];
            let e2 = [p[[c, 0]] - p[[a, 0]], p[[c, 1]] - p[[a, 1]], p[[c, 2]] - p[[a, 2]]];
            let f = [e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]];
            let l = (f[0] * f[0] + f[1] * f[1] + f[2] * f[2]).sqrt();
            let d = if l > 0.0 { l } else { 1.0 };
            fnrm[i] = [f[0] / d, f[1] / d, f[2] / d];
        }
        let mut acc = vec![0.0f64; n * 3];
        for k in 0..3 {
            for i in 0..m {
                let v = t[[i, k]] as usize;
                for c in 0..3 { acc[v * 3 + c] += fnrm[i][c]; }
            }
        }
        for v in 0..n {
            let (x, y, z) = (acc[v * 3], acc[v * 3 + 1], acc[v * 3 + 2]);
            let l = (x * x + y * y + z * z).sqrt();
            let d = if l > 0.0 { l } else { 1.0 };
            acc[v * 3] = x / d; acc[v * 3 + 1] = y / d; acc[v * 3 + 2] = z / d;
        }
        acc
    });
    numpy::ndarray::Array2::from_shape_vec((n, 3), acc).unwrap().into_pyarray(py)
}

/// cdae._mesh_data's vertex order: the vertices in order of first use by the triangle list (tris (m, 3) into nverts),
/// and the triangles renumbered to it with the opposite winding. -> (order (k,), idx (m, 3)).
#[pyfunction]
fn first_use<'py>(py: Python<'py>, tris: PyReadonlyArray2<'py, i64>, nverts: usize)
                  -> (Bound<'py, PyArray1<i64>>, Bound<'py, PyArray2<i64>>) {
    let t = tris.as_array();
    let m = t.nrows();
    let (order, idx) = py.allow_threads(|| {
        let mut remap = vec![-1i64; nverts];
        let mut order = Vec::new();
        for i in 0..m {
            for k in 0..3 {
                let v = t[[i, k]] as usize;
                if remap[v] < 0 { remap[v] = order.len() as i64; order.push(v as i64); }
            }
        }
        let mut idx = vec![0i64; m * 3];
        for i in 0..m {
            for k in 0..3 { idx[i * 3 + k] = remap[t[[i, 2 - k]] as usize]; }
        }
        (order, idx)
    });
    (order.into_pyarray(py), numpy::ndarray::Array2::from_shape_vec((m, 3), idx).unwrap().into_pyarray(py))
}

/// railways._blocked: which points (xy (k, 2), bed heights z (k,)) lie on a crossing road at grade: per road (its
/// centre line pts (n, 3) [x, y, z] one road after another, starts (C + 1,)) the point's nearest segment (the first of
/// equals) nearer than reach (C,) with the road's height at that segment's start within dz of z. numpy's float64
/// arithmetic of the Python it replaces.
#[pyfunction]
fn near_crossers(py: Python<'_>, xy: PyReadonlyArray2<f64>, z: PyReadonlyArray1<f64>, pts: PyReadonlyArray2<f64>,
                 starts: PyReadonlyArray1<i64>, reach: PyReadonlyArray1<f64>, dz: f64) -> Vec<bool> {
    let (p, z, d, st, r) = (xy.as_array(), z.as_array(), pts.as_array(), starts.as_array(), reach.as_array());
    let k = p.nrows();
    let nc = st.len().saturating_sub(1);
    py.allow_threads(|| {
        let mut out = vec![false; k];
        for c in 0..nc {
            let (j0, j1) = (st[c] as usize, st[c + 1] as usize);
            if j1 < j0 + 2 { continue; }
            let segs: Vec<[f64; 5]> = (j0..j1 - 1).map(|j| {
                let (ax, ay) = (d[[j, 0]], d[[j, 1]]);
                let (bx, by) = (d[[j + 1, 0]] - ax, d[[j + 1, 1]] - ay);
                [ax, ay, bx, by, (bx * bx + by * by).max(1e-12)]
            }).collect();
            for i in 0..k {
                if out[i] { continue; }
                let (px, py_) = (p[[i, 0]], p[[i, 1]]);
                let (mut best, mut bj) = (f64::INFINITY, usize::MAX);
                for (n, g) in segs.iter().enumerate() {
                    let t = (((px - g[0]) * g[2] + (py_ - g[1]) * g[3]) / g[4]).clamp(0.0, 1.0);
                    let dd = (px - (g[0] + t * g[2])).hypot(py_ - (g[1] + t * g[3]));
                    if dd < best || bj == usize::MAX { best = dd; bj = n; }
                }
                if bj != usize::MAX && best < r[c] && (z[i] - d[[j0 + bj, 2]]).abs() < dz { out[i] = true; }
            }
        }
        out
    })
}

/// roads.clip_polyline: the pieces of a polyline (pts (k, D >= 2), xy first) inside the box, Liang-Barsky per segment,
/// consecutive pieces joined (within 1e-6 in every column), as the Python: (points (n, D), starts (P + 1,)).
#[pyfunction]
fn clip_polyline<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, x0: f64, y0: f64, x1: f64, y1: f64)
                      -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>) {
    let p = pts.as_array();
    let (k, dim) = (p.nrows(), p.ncols());
    let (data, starts) = py.allow_threads(|| {
        let mut data: Vec<f64> = Vec::new();
        let mut starts: Vec<i64> = vec![0];
        let mut cur: Vec<Vec<f64>> = Vec::new();
        let flush = |cur: &mut Vec<Vec<f64>>, data: &mut Vec<f64>, starts: &mut Vec<i64>| {
            if cur.len() >= 2 {
                for r in cur.iter() { data.extend_from_slice(r); }
                starts.push((data.len() / dim) as i64);
            }
            cur.clear();
        };
        for i in 0..k.saturating_sub(1) {
            let (ax, ay) = (p[[i, 0]], p[[i, 1]]);
            let (dx, dy) = (p[[i + 1, 0]] - ax, p[[i + 1, 1]] - ay);
            let (mut t0, mut t1, mut ok) = (0.0f64, 1.0f64, true);
            for (pp, q) in [(-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)] {
                if pp == 0.0 {
                    if q < 0.0 { ok = false; break; }
                } else {
                    let t = q / pp;
                    if pp < 0.0 { if t > t0 { t0 = t; } } else if t < t1 { t1 = t; }
                }
            }
            if !ok || t0 > t1 {
                flush(&mut cur, &mut data, &mut starts);
                continue;
            }
            let pa: Vec<f64> = (0..dim).map(|c| p[[i, c]] + (p[[i + 1, c]] - p[[i, c]]) * t0).collect();
            let pb: Vec<f64> = (0..dim).map(|c| p[[i, c]] + (p[[i + 1, c]] - p[[i, c]]) * t1).collect();
            let joined = match cur.last() {
                None => false,
                Some(last) => {
                    let mut m = 0.0f64;
                    for c in 0..dim {
                        let v = (last[c] - pa[c]).abs();
                        if v.is_nan() { m = f64::NAN; break; }
                        if v > m { m = v; }
                    }
                    !(m > 1e-6)
                }
            };
            if !joined {
                flush(&mut cur, &mut data, &mut starts);
                cur.push(pa);
            }
            cur.push(pb);
            if t1 < 1.0 { flush(&mut cur, &mut data, &mut starts); }
        }
        flush(&mut cur, &mut data, &mut starts);
        (data, starts)
    });
    let n = data.len() / dim.max(1);
    (numpy::ndarray::Array2::from_shape_vec((n, dim), data).unwrap().into_pyarray(py), starts.into_pyarray(py))
}

/// Which points (n, 2) lie within (tx, ty) of a target (k, 2) along both axes (a box test, through a grid of the
/// targets): roads._skew_decks' pre-check of the features' ends against the bridges' ends (lon / lat).
#[pyfunction]
fn ends_near(py: Python<'_>, pts: PyReadonlyArray2<f64>, targets: PyReadonlyArray2<f64>, tx: f64, ty: f64) -> Vec<bool> {
    let (p, t) = (pts.as_array(), targets.as_array());
    py.allow_threads(|| {
        let n = p.nrows();
        if t.nrows() == 0 || !(tx > 0.0 && ty > 0.0) { return vec![false; n]; }
        let mut map: std::collections::HashMap<(i64, i64), Vec<usize>> = Default::default();
        for j in 0..t.nrows() {
            let (x, y) = (t[[j, 0]], t[[j, 1]]);
            if !(x.is_finite() && y.is_finite()) { continue; }
            map.entry(((x / tx).floor() as i64, (y / ty).floor() as i64)).or_default().push(j);
        }
        (0..n).map(|i| {
            let (x, y) = (p[[i, 0]], p[[i, 1]]);
            if !(x.is_finite() && y.is_finite()) { return false; }
            let (kx, ky) = ((x / tx).floor() as i64, (y / ty).floor() as i64);
            for gx in kx - 1..=kx + 1 {
                for gy in ky - 1..=ky + 1 {
                    if let Some(v) = map.get(&(gx, gy)) {
                        if v.iter().any(|&j| (t[[j, 0]] - x).abs() <= tx && (t[[j, 1]] - y).abs() <= ty) { return true; }
                    }
                }
            }
            false
        }).collect()
    })
}

/// roads._deck_crossings: (i, j, arc on i, arc on j) where polylines i < j cross away from their end points.
/// pts (n, 2) all lines one after another, starts (L + 1,) where each begins (the last: n).
#[pyfunction]
fn deck_crossings(pts: PyReadonlyArray2<f64>, starts: PyReadonlyArray1<i64>) -> Vec<(usize, usize, f64, f64)> {
    let p = pts.as_array();
    let st = starts.as_array();
    let nl = st.len().saturating_sub(1);
    let rng: Vec<(usize, usize)> = (0..nl).map(|i| (st[i] as usize, st[i + 1] as usize)).collect();
    let mut boxes = Vec::with_capacity(nl);
    let mut arcs: Vec<Vec<f64>> = Vec::with_capacity(nl);
    for &(a, b) in &rng {
        let (mut lo, mut hi) = ([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]);
        let mut arc = Vec::with_capacity(b - a);
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
        if bi - ai < 2 {
            continue;
        }
        for j in (i + 1)..nl {
            let (aj, bj) = rng[j];
            if bj - aj < 2 {
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
    inside_view(&poly.as_array(), x, y)
}

/// buildings.road_through: metres of the polylines (pts (n, 2) one after another, starts (L + 1,)) inside the ring,
/// each segment sampled at the middles of max(1, floor(len / step)) equal pieces: the Python loop's float64 sums, in
/// its order (the buildings' largest cost in a city road tile, 2026-10-25).
#[pyfunction]
fn road_through(py: Python<'_>, ring: PyReadonlyArray2<f64>, pts: PyReadonlyArray2<f64>,
                starts: PyReadonlyArray1<i64>, step: f64) -> f64 {
    let (r, p, st) = (ring.as_array(), pts.as_array(), starts.as_array());
    py.allow_threads(|| {
        let mut total = 0.0f64;
        for l in 0..st.len().saturating_sub(1) {
            let (i0, i1) = (st[l] as usize, st[l + 1] as usize);
            for i in i0..i1.saturating_sub(1) {
                let (px, py_) = (p[[i, 0]], p[[i, 1]]);
                let (dx, dy) = (p[[i + 1, 0]] - px, p[[i + 1, 1]] - py_);
                let len = dx.hypot(dy);
                let k = ((len / step) as i64).max(1);
                let kf = k as f64;
                for j in 0..k {
                    let t = (j as f64 + 0.5) / kf;
                    if inside_view(&r, px + dx * t, py_ + dy * t) {
                        total += len / kf;
                    }
                }
            }
        }
        total
    })
}

fn inside_view(p: &numpy::ndarray::ArrayView2<f64>, x: f64, y: f64) -> bool {
    let n = p.nrows();
    if n == 0 {
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
    let nb = h.len();
    let mut boxes = Vec::with_capacity(nb);
    for b in 0..nb {
        let (s0, s1) = (st[b] as usize, st[b + 1] as usize);
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for k in s0..s1 {
            x0 = x0.min(sg[[k, 0]]).min(sg[[k, 2]]);
            x1 = x1.max(sg[[k, 0]]).max(sg[[k, 2]]);
            y0 = y0.min(sg[[k, 1]]).min(sg[[k, 3]]);
            y1 = y1.max(sg[[k, 1]]).max(sg[[k, 3]]);
        }
        boxes.push((x0 - h[b], y0 - h[b], x1 + h[b], y1 + h[b]));
    }
    let mut out = vec![false; n];
    for i in 0..n {
        let (x, y) = (p[[i, 0]], p[[i, 1]]);
        'blk: for b in 0..nb {
            let (bx0, by0, bx1, by1) = boxes[b];
            if x < bx0 || x > bx1 || y < by0 || y > by1 {
                continue;
            }
            for k in (st[b] as usize)..(st[b + 1] as usize) {
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
        let n = ((b[0] - a[0]).hypot(b[1] - a[1]) / spacing).ceil() as i64;
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
    let n = xp.len();
    if x <= xp[0] {
        return fp[0];
    }
    if x >= xp[n - 1] {
        return fp[n - 1];
    }
    // the last j with xp[j] <= x
    let j = match xp.binary_search_by(|v| v.partial_cmp(&x).unwrap()) {
        Ok(mut k) => {
            while k + 1 < n && xp[k + 1] <= x {
                k += 1;
            }
            k
        }
        Err(k) => k - 1,
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
    let k = l.nrows();
    if k < 2 {
        return None;
    }
    let xs: Vec<f64> = (0..k).map(|i| l[[i, 0]]).collect();
    let ys: Vec<f64> = (0..k).map(|i| l[[i, 1]]).collect();
    let mut s = vec![0.0f64; k];
    for i in 1..k {
        s[i] = s[i - 1] + (xs[i] - xs[i - 1]).hypot(ys[i] - ys[i - 1]);
    }
    let total = s[k - 1];
    if total < 2.0 * end_skip + step {
        return None;
    }
    // numpy.arange(end_skip, total - end_skip + 1e-9, step)
    let stop = total - end_skip + 1e-9;
    let count = ((stop - end_skip) / step).ceil().max(0.0) as usize;
    let (keys, first, counts, seg_of) = (keys.as_slice().unwrap(), first.as_slice().unwrap(),
                                         counts.as_slice().unwrap(), seg_of.as_slice().unwrap());
    let sg = segs.as_array();
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
    w.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let m = w.len();
    let med = if m % 2 == 1 { w[m / 2] } else { (w[m / 2 - 1] + w[m / 2]) / 2.0 };
    let (q1, q3) = (np_percentile(&w, 25.0), np_percentile(&w, 75.0));
    if (q3 - q1) / med > agree {
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
/// core half width h, carriageway half width rh, road rid): the weights, the inverse-distance blend and the max weight
/// as the numpy code, the (segment, point) pairs in its order (segments, bucket rows, points sorted by bucket). On a
/// road's carriageway another road (past its own rh: its verge and blend) more than wall_cut above or below it does not
/// count: a street's sidewalk over a cutting dragged the road in it up its wall (Munich's McGraw-Graben). Behind a
/// cutting's wall (an up cut side, wall_flat deep: under the retaining wall's top) no other road at another level counts
/// at all: the terrain's 2 m lattice drew the step as teeth in front of the wall where a street above came near it.
/// grading.bank: a road's cross slope at each vertex of its centreline xy (n, 2) (m per m toward its left; a curve
/// leaning into its turn), by the curve's radius R (the circle through the points about `stencil` m either side):
/// e = e_max x min(1, r_min / R), left out under 0.01 (a gentle bend keeps its crown), then a running mean over
/// `runoff` m (the bank comes in gradually) and faded to 0 over `fade_a` / `fade_b` m at the first / last vertex
/// (0: none; a bridge's deck or a tunnel's floor is level there). After BeamNG's Hero Road editor (its bank runoff and
/// fading) with the design-speed rule for e.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn bank_profile<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, e_max: f64, r_min: f64, stencil: f64,
                     runoff: f64, fade_a: f64, fade_b: f64) -> Bound<'py, PyArray1<f64>> {
    let xy = xy.as_array();
    let n = xy.shape()[0];
    let mut e = vec![0.0f64; n];
    if n < 3 {
        return e.into_pyarray(py);
    }
    let mut arc = vec![0.0f64; n];
    for i in 1..n {
        arc[i] = arc[i - 1] + (xy[[i, 0]] - xy[[i - 1, 0]]).hypot(xy[[i, 1]] - xy[[i - 1, 1]]);
    }
    let (mut j, mut k) = (0usize, 0usize);
    for i in 0..n {
        while j + 1 < i && arc[i] - arc[j + 1] >= stencil { j += 1; }
        if k < i { k = i; }
        while k + 1 < n && arc[k] - arc[i] < stencil { k += 1; }
        if j >= i || k <= i { continue; }
        let (ax, ay) = (xy[[j, 0]] - xy[[i, 0]], xy[[j, 1]] - xy[[i, 1]]);
        let (bx, by) = (xy[[k, 0]] - xy[[i, 0]], xy[[k, 1]] - xy[[i, 1]]);
        let la = ax.hypot(ay);
        let lb = bx.hypot(by);
        let lc = (bx - ax).hypot(by - ay);
        if la < 1e-6 || lb < 1e-6 || lc < 1e-6 { continue; }
        // the signed curvature of the circle through j, i, k: > 0 turning left (from j through i to k)
        let cross = (xy[[i, 0]] - xy[[j, 0]]) * (xy[[k, 1]] - xy[[i, 1]]) - (xy[[i, 1]] - xy[[j, 1]]) * (xy[[k, 0]] - xy[[i, 0]]);
        let kappa = 2.0 * cross / (la * lb * lc);
        let rad = 1.0 / kappa.abs().max(1e-12);
        let ee = e_max * (r_min / rad).min(1.0);
        if ee >= 0.01 {
            e[i] = -kappa.signum() * ee;        // (a left turn: its left, the inside, lower)
        }
    }
    // the runoff: a running mean over `runoff` m (by arc length: the plan's vertices are evenly spaced or nearly)
    let mut out = vec![0.0f64; n];
    let half = runoff / 2.0;
    let (mut lo, mut hi, mut sum) = (0usize, 0usize, 0.0f64);
    for i in 0..n {
        while hi < n && arc[hi] - arc[i] <= half { sum += e[hi]; hi += 1; }
        while lo < hi && arc[i] - arc[lo] > half { sum -= e[lo]; lo += 1; }
        out[i] = if hi > lo { sum / (hi - lo) as f64 } else { 0.0 };
    }
    let total = arc[n - 1];
    for i in 0..n {
        let mut f = 1.0f64;
        if fade_a > 0.0 { let u = (arc[i] / fade_a).clamp(0.0, 1.0); f = f.min(u * u * (3.0 - 2.0 * u)); }
        if fade_b > 0.0 { let u = ((total - arc[i]) / fade_b).clamp(0.0, 1.0); f = f.min(u * u * (3.0 - 2.0 * u)); }
        out[i] *= f;
    }
    out.into_pyarray(py)
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn grade_points<'py>(py: Python<'py>, px: PyReadonlyArray1<'py, f64>, pyy: PyReadonlyArray1<'py, f64>,
                     base: PyReadonlyArray1<'py, f64>, a: PyReadonlyArray2<'py, f64>, b: PyReadonlyArray2<'py, f64>,
                     za: PyReadonlyArray1<'py, f64>, zb: PyReadonlyArray1<'py, f64>, h: PyReadonlyArray1<'py, f64>,
                     rh: PyReadonlyArray1<'py, f64>, rid: PyReadonlyArray1<'py, i64>, abut: PyReadonlyArray1<'py, u8>, cut: PyReadonlyArray2<'py, f64>,
                     ba: PyReadonlyArray1<'py, f64>, bb: PyReadonlyArray1<'py, f64>, lift: f64, slope_run: f64,
                     t_min: f64, t_max: f64, blend_r: f64,
                     bucket: f64, wall_cut: f64, wall_t: f64, wall_flat: f64, wall_ahead: f64,
                     sep: bool, sep_from: f64, sep_full: f64, core_fade: f64, side_fade: f64, sharp_step: f64)
                     -> Bound<'py, PyArray1<f64>> {
    let (px, pyy, base) = (px.as_array(), pyy.as_array(), base.as_array());
    let (a, b, za, zb, h) = (a.as_array(), b.as_array(), za.as_array(), zb.as_array(), h.as_array());
    let (rh, rid) = (rh.as_array(), rid.as_array());
    let abut = abut.as_array();
    let cut = cut.as_array();               // (segments, 6): a cutting's wall, top and top offset, left then right
    // (segments): the cross slope at each segment's ends (m per m toward its left: a banked curve, grading.bank)
    let (ba, bb) = (ba.as_array(), bb.as_array());
    let banked: Vec<bool> = (0..h.len()).map(|s| ba[s] != 0.0 || bb[s] != 0.0).collect();
    let sharp = cut.iter().any(|v| v.is_finite());
    let has_cut: Vec<bool> = (0..h.len()).map(|s| (0..6).any(|k| cut[[s, k]].is_finite())).collect();
    let cut_reach: Vec<f64> = (0..h.len()).map(|s| {
        let r = cut[[s, 2]].max(cut[[s, 5]]).max(cut[[s, 0]]).max(cut[[s, 3]]);
        if r.is_finite() { r + 0.5 + 2.0 * sharp_step } else { 0.0 }
    }).collect();
    let n = px.len();
    let mut out: Vec<f64> = base.iter().copied().collect();
    if n == 0 {
        return out.into_pyarray(py);
    }
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for i in 0..n {
        x0 = x0.min(px[i]); x1 = x1.max(px[i]);
        y0 = y0.min(pyy[i]); y1 = y1.max(pyy[i]);
    }
    let bxs: Vec<i64> = (0..n).map(|i| ((px[i] - x0) / bucket).floor() as i64).collect();
    let bys: Vec<i64> = (0..n).map(|i| ((pyy[i] - y0) / bucket).floor() as i64).collect();
    let nbx = bxs.iter().copied().max().unwrap() + 1;
    let keys: Vec<i64> = (0..n).map(|i| bys[i] * nbx + bxs[i]).collect();
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by_key(|&i| keys[i]);                    // stable, as argsort(kind="stable")
    let sk: Vec<i64> = order.iter().map(|&i| keys[i]).collect();
    // the points and their state in that order (the pairs loop reads them in runs: px[order[j]] and the state arrays
    // indexed at random were most of its time, 2026-10-26); each point's sums in the same segment order as before
    let xs: Vec<f64> = order.iter().map(|&i| px[i]).collect();
    let ys: Vec<f64> = order.iter().map(|&i| pyy[i]).collect();
    let bs: Vec<f64> = order.iter().map(|&i| base[i]).collect();
    let mut num = vec![0.0f64; n];
    let mut den = vec![0.0f64; n];
    let mut wmax = vec![0.0f64; n];
    // two passes: the highest core of a road's last stretch before a bridge (abut) over each point, then the blend
    // without the roads more than wall_cut under it beside them, outside their own cores (grading.grade_points, #38)
    let mut top = vec![f64::NEG_INFINITY; n];
    // (sep) how much each point is in a core, and that core's height: other roads beside it count less (#92)
    let mut cmax = vec![0.0f64; n];
    let mut csum = vec![0.0f64; n];
    let mut cz = vec![0.0f64; n];
    let mut raised = vec![f64::NEG_INFINITY; n];    // (sharp) the cuttings' tops over the points beside them
    let mut beff: Vec<f64> = bs.clone();
    // the carriageway each point is deepest in (d - rh, most negative) and its height there
    let mut cdeep = vec![0.0f64; n];
    let mut carz = vec![f64::NAN; n];
    let mut carid = vec![-1i64; n];
    // the cutting band each point is deepest in (d - its far edge) and its road and height
    let mut bdeep = vec![0.0f64; n];
    let mut bz = vec![f64::NAN; n];
    let mut bid = vec![-1i64; n];
    let smooth = |v: f64| { let v = v.clamp(0.0, 1.0); v * v * (3.0 - 2.0 * v) };
    // pass 1: a pair with d - h past this has u >= 1 (its core hs <= h, its ramp tt <= t_max, a wall's flat and ramp
    // wall_flat + wall_t), so w = 0: skipped (a margin for the rounding)
    let far1 = t_max.max(wall_flat + wall_t) + 1e-6;
    for pass in 0..2 {
        if pass == 1 && sharp {
            for i in 0..n {
                beff[i] = bs[i].max(raised[i]);
            }
        }
        for s in 0..h.len() {
            // (pass 0 within the blend's reach too: the Python loop pairs the points with a segment once, at h + t_max)
            let reach = if pass == 0 { (h[s] + if sep { core_fade } else { 0.0 }).max(cut_reach[s]).min(h[s] + t_max) }
                        else { h[s] + t_max };
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
            // pass 0's farthest effect: a carriageway (rh), an abutment (h), a cutting's band (h + wall_flat) and top
            // (cut_reach: its side and ahead limits), a core (h + core_fade); a margin for the rounding
            let far0 = rh[s].max(h[s] + if has_cut[s] { wall_flat.max(0.0) } else { 0.0 }).max(cut_reach[s])
                .max(h[s] + if sep { core_fade } else { 0.0 }) + 1e-6;
            for row in cy0..=cy1 {
                let i0 = sk.partition_point(|&k| k < row * nbx + cx0);
                let i1 = sk.partition_point(|&k| k <= row * nbx + cx1);
                for idx in i0..i1.max(i0) {
                    let (qx, qy) = (xs[idx] - ax, ys[idx] - ay);
                    let t = if l2 > 1e-12 { ((qx * abx + qy * aby) / l2).clamp(0.0, 1.0) } else { 0.0 };
                    let (ddx, ddy) = (qx - t * abx, qy - t * aby);
                    let d = (ddx * ddx + ddy * ddy).sqrt();       // (not hypot: libm's is ~5x dearer, the pairs loop's main cost)
                    let mut target = za[s] + (zb[s] - za[s]) * t;
                    if banked[s] && l2 > 1e-12 {    // (a banked curve: tilted across the carriageway, its edge height past)
                        let off = ((qy * abx - qx * aby) / l2.sqrt()).clamp(-rh[s], rh[s]);
                        target += (ba[s] + (bb[s] - ba[s]) * t) * off;
                    }
                    let left = (qx * aby - qy * abx) < 0.0;
                    let co = if left { 0 } else { 3 };
                    let (cwall, ctop, creach) = (cut[[s, co]], cut[[s, co + 1]], cut[[s, co + 2]]);
                    if pass == 0 {
                        if d > far0 {
                            continue;       // (nothing below reaches this far: the box's corners)
                        }
                        if d - rh[s] < cdeep[idx] || (d <= rh[s] && carz[idx].is_nan()) {
                            cdeep[idx] = d - rh[s];
                            carz[idx] = target;
                            carid[idx] = rid[s];
                        }
                        if abut[s] != 0 && d <= h[s] && target > top[idx] {
                            top[idx] = target;
                        }
                        if has_cut[s] && ctop.is_finite() {     // the ground raised to a cutting's top past its wall
                            let lab0 = l2.max(1e-24).sqrt();
                            let al0 = (qx * abx + qy * aby) / lab0;
                            let ahead0 = (-al0).max(al0 - lab0).max(0.0);
                            let side0 = (qx * aby - qy * abx).abs() / lab0;
                            let bend = h[s].min(cwall) + wall_flat;     // the band behind its wall
                            if side0 > rh[s] && ahead0 < side0 && d - bend < bdeep[idx] {
                                bdeep[idx] = d - bend;
                                bz[idx] = target;
                                bid[idx] = rid[s];
                            }
                            if side0 > cwall && side0 <= creach + 0.5 && ahead0 < sharp_step * 2.0 && ctop > raised[idx] {
                                raised[idx] = ctop;
                            }
                        }
                        if sep {
                            let cn = 1.0 - smooth((d - h[s]) / core_fade);
                            if cn > 0.0 {
                                if cn > cmax[idx] {
                                    cmax[idx] = cn;
                                }
                                csum[idx] += cn;
                                cz[idx] += cn * target;
                            }
                        }
                        continue;
                    }
                    // (past every blend's reach: no weight, nothing changes; the box's corners, ~half the pairs)
                    if d - h[s] > far1 {
                        continue;
                    }
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
                    // (on a cutting's side the core ends at its wall; where the ground falls away there the road ends)
                    let hs = if cwall.is_finite() { h[s].min(cwall) } else { h[s] };
                    let drop = cwall.is_finite() && !ctop.is_finite() && d > cwall;
                    let wall = beff[idx] - target > wall_cut && ahead < side && ahead < wall_ahead;
                    let tt = if wall { wall_t } else { (slope_run * (target - beff[idx]).abs()).clamp(t_min, t_max) };
                    // a wall: flat on one terrain step behind it (no slope in front of its face), then up
                    let u = ((d - hs - if wall { wall_flat } else { 0.0 }) / tt).clamp(0.0, 1.0);
                    let w = 1.0 - u * u * (3.0 - 2.0 * u);
                    // (another road's carriageway here, at another level: this road gives way past its own carriageway)
                    // (a road's own carriageway never gives way: a hairpin's band over the leg above sank its lanes
                    // under the terrain, the Stelvio Pass, the user's report 2026-10-21)
                    // (a carriageway first: a road's cutting band reaching over another road's carriageway at another level
                    // (its "climb" was that road's embankment) does not pull that road's ground; a bridge's abutment sank
                    // 2.5 m under the deck into the street below, a wall cars hit, GitHub #108 Meppen)
                    let on_other_car = carid[idx] >= 0 && carid[idx] != rid[s] && (target - carz[idx]).abs() > wall_cut;
                    let yields = d > rh[s] && (on_other_car
                        || (bid[idx] >= 0 && bid[idx] != rid[s] && (target - bz[idx]).abs() > wall_cut));
                    if w > 0.0 && !drop && !yields && !(d > hs && ahead < side && target < top[idx] - wall_cut) {
                        let r = d / blend_r;
                        let mut wb = w / (1.0 + r * r);
                        if sep {
                            let rf = cz[idx] / csum[idx].max(1e-12);
                            let f = cmax[idx] * smooth(((target - rf).abs() - sep_from) / (sep_full - sep_from))
                                * smooth((d - hs) / core_fade) * smooth((side - ahead) / side_fade);
                            wb *= 1.0 - f;
                        }
                        num[idx] += wb * target;
                        den[idx] += wb;
                        if w > wmax[idx] {
                            wmax[idx] = w;
                        }
                    }
                }
            }
        }
    }
    for j in 0..n {
        let i = order[j];
        out[i] = beff[j];
        if den[j] > 0.0 {
            out[i] = beff[j] + wmax[j] * (num[j] / den[j] - beff[j] + lift);
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
    let n = s.len();
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
        while k + 1 < hull.len() - 1 && s[hull[k + 1]] <= s[i] { k += 1; }
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
    let xs: Vec<f64> = (0..k).map(|i| p[[i, 0]]).collect();
    let ys: Vec<f64> = (0..k).map(|i| p[[i, 1]]).collect();
    let mut s = vec![0.0f64; k];
    for i in 1..k {
        s[i] = s[i - 1] + (xs[i] - xs[i - 1]).hypot(ys[i] - ys[i - 1]);
    }
    let total = s[k - 1];
    if !(total > 0.0) {
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

/// The point `dist` m along the polyline (x, y) from its first vertex (its last one when shorter).
fn along(x: &[f64], y: &[f64], dist: f64) -> (f64, f64) {
    let mut left = dist;
    for i in 1..x.len() {
        let l = (x[i] - x[i - 1]).hypot(y[i] - y[i - 1]);
        if l >= left && l > 0.0 {
            let t = left / l;
            return (x[i - 1] + t * (x[i] - x[i - 1]), y[i - 1] + t * (y[i] - y[i - 1]));
        }
        left -= l;
    }
    (x[x.len() - 1], y[y.len() - 1])
}

/// The candidate lines (points one line after another, offsets (C + 1,)) by the grid cells of their two end points: the
/// lines with an end within join of a point are among near()'s, in ascending order (the brute-force loop's order).
struct EndsGrid {
    cell: f64,
    map: std::collections::HashMap<(i64, i64), Vec<usize>>,
    all: Option<usize>,
}

impl EndsGrid {
    fn new(cxy: &numpy::ndarray::ArrayView2<f64>, coff: &numpy::ndarray::ArrayView1<i64>, join: f64) -> EndsGrid {
        let nc = coff.len().saturating_sub(1);
        // (a non-finite or non-positive join: every line, as before)
        if !(join.is_finite() && join > 0.0) {
            return EndsGrid { cell: 1.0, map: Default::default(), all: Some(nc) };
        }
        let cell = join;
        let mut map: std::collections::HashMap<(i64, i64), Vec<usize>> = Default::default();
        for c in 0..nc {
            let (a, b) = (coff[c] as usize, coff[c + 1] as usize);
            if b < a + 2 { continue; }
            for i in [a, b - 1] {
                let (x, y) = (cxy[[i, 0]], cxy[[i, 1]]);
                if !(x.is_finite() && y.is_finite()) { continue; }      // (never within join of anything)
                let k = ((x / cell).floor() as i64, (y / cell).floor() as i64);
                let v = map.entry(k).or_default();
                if v.last() != Some(&c) { v.push(c); }
            }
        }
        EndsGrid { cell, map, all: None }
    }

    fn near(&self, x: f64, y: f64) -> Vec<usize> {
        if let Some(n) = self.all { return (0..n).collect(); }
        if !(x.is_finite() && y.is_finite()) { return Vec::new(); }
        let (kx, ky) = ((x / self.cell).floor() as i64, (y / self.cell).floor() as i64);
        let mut out = Vec::new();
        for gx in kx - 2..=kx + 2 {            // (+-2: an end exactly join away across a cell edge)
            for gy in ky - 2..=ky + 2 {
                if let Some(v) = self.map.get(&(gx, gy)) { out.extend_from_slice(v); }
            }
        }
        out.sort_unstable();
        out.dedup();
        out
    }
}

/// grading.chain_extensions: for each query (a road part's end: its node, the way the road goes on through it, its
/// smoothing class, its road), the straightest candidate line with an end within join m of the node, of the same class
/// and another road, turning at most turn_deg (directions over look m of each line); its line from the node outward,
/// cut reach m along (each query its own), as points (n, 2) and per-query offsets (q + 1): an empty run for none.
/// Ties: the lower candidate index (the caller's order).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn chain_extensions<'py>(py: Python<'py>, cand_xy: PyReadonlyArray2<'py, f64>, cand_off: PyReadonlyArray1<'py, i64>,
                         cand_class: PyReadonlyArray1<'py, i64>, cand_road: PyReadonlyArray1<'py, i64>,
                         q_node: PyReadonlyArray2<'py, f64>, q_dir: PyReadonlyArray2<'py, f64>,
                         q_class: PyReadonlyArray1<'py, i64>, q_road: PyReadonlyArray1<'py, i64>,
                         q_reach: PyReadonlyArray1<'py, f64>, join: f64, turn_deg: f64, look: f64)
                         -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>) {
    let (cxy, coff, ccl, crd) = (cand_xy.as_array(), cand_off.as_array(), cand_class.as_array(), cand_road.as_array());
    let (qn, qd, qcl, qrd, qr) = (q_node.as_array(), q_dir.as_array(), q_class.as_array(), q_road.as_array(),
                                  q_reach.as_array());
    let cos_max = turn_deg.to_radians().cos();
    let grid = EndsGrid::new(&cxy, &coff, join);
    let mut out: Vec<f64> = Vec::new();
    let mut off: Vec<i64> = vec![0];
    for q in 0..qn.nrows() {
        let (nx, ny) = (qn[[q, 0]], qn[[q, 1]]);
        let (dx, dy) = (qd[[q, 0]], qd[[q, 1]]);
        let dl = dx.hypot(dy);
        let mut best: Option<(f64, Vec<f64>, Vec<f64>)> = None;
        if dl > 0.0 {
            for c in grid.near(nx, ny) {
                if ccl[c] != qcl[q] || crd[c] == qrd[q] {
                    continue;
                }
                let (a, b) = (coff[c] as usize, coff[c + 1] as usize);
                if b < a + 2 {
                    continue;
                }
                // (the ends first: most candidates are nowhere near the node)
                let at_start = (cxy[[a, 0]] - nx).hypot(cxy[[a, 1]] - ny) <= join;
                if !at_start && (cxy[[b - 1, 0]] - nx).hypot(cxy[[b - 1, 1]] - ny) > join {
                    continue;
                }
                let mut xs: Vec<f64> = (a..b).map(|i| cxy[[i, 0]]).collect();
                let mut ys: Vec<f64> = (a..b).map(|i| cxy[[i, 1]]).collect();
                if !at_start {
                    xs.reverse();
                    ys.reverse();
                }
                let (px, py) = along(&xs, &ys, look);
                let (ex, ey) = (px - xs[0], py - ys[0]);
                let el = ex.hypot(ey);
                if el <= 0.0 {
                    continue;
                }
                let cos = (dx * ex + dy * ey) / (dl * el);
                if cos >= cos_max && best.as_ref().map_or(true, |b| cos > b.0) {
                    best = Some((cos, xs, ys));
                }
            }
        }
        if let Some((_, xs, ys)) = best {
            // from the node itself, cut reach m along
            out.push(nx);
            out.push(ny);
            let mut left = qr[q];
            for i in 1..xs.len() {
                let l = (xs[i] - xs[i - 1]).hypot(ys[i] - ys[i - 1]);
                if l >= left {
                    let t = if l > 0.0 { left / l } else { 0.0 };
                    out.push(xs[i - 1] + t * (xs[i] - xs[i - 1]));
                    out.push(ys[i - 1] + t * (ys[i] - ys[i - 1]));
                    break;
                }
                out.push(xs[i]);
                out.push(ys[i]);
                left -= l;
            }
        }
        off.push((out.len() / 2) as i64);
    }
    let a = numpy::ndarray::Array2::from_shape_vec((out.len() / 2, 2), out).unwrap();
    (a.into_pyarray(py), off.into_pyarray(py))
}

/// grading node parts: for each query node (n, 2), the candidate lines (cand_xy / cand_off as chain_extensions') with an
/// end within join m of it, as flat indices with per-query offsets (q + 1); each index is the candidate's times 2, plus 1
/// when it is its last point that is at the node.
#[pyfunction]
fn node_parts<'py>(py: Python<'py>, cand_xy: PyReadonlyArray2<'py, f64>, cand_off: PyReadonlyArray1<'py, i64>,
                   q_node: PyReadonlyArray2<'py, f64>, join: f64)
                   -> (Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>) {
    let (cxy, coff, qn) = (cand_xy.as_array(), cand_off.as_array(), q_node.as_array());
    let grid = EndsGrid::new(&cxy, &coff, join);
    let mut out: Vec<i64> = Vec::new();
    let mut off: Vec<i64> = vec![0];
    for q in 0..qn.nrows() {
        let (nx, ny) = (qn[[q, 0]], qn[[q, 1]]);
        for c in grid.near(nx, ny) {
            let (a, b) = (coff[c] as usize, coff[c + 1] as usize);
            if b < a + 2 {
                continue;
            }
            if (cxy[[a, 0]] - nx).hypot(cxy[[a, 1]] - ny) <= join {
                out.push(2 * c as i64);
            } else if (cxy[[b - 1, 0]] - nx).hypot(cxy[[b - 1, 1]] - ny) <= join {
                out.push(2 * c as i64 + 1);
            }
        }
        off.push(out.len() as i64);
    }
    (out.into_pyarray(py), off.into_pyarray(py))
}

/// routeahead.arc_length_points: the route [lat], [lon] (routing.py's geometry, from the player) sampled every step m
/// along its length (plate carree about its first point), nearest first, up to max_m or the route's end.
#[pyfunction]
fn route_samples<'py>(py: Python<'py>, lat: PyReadonlyArray1<'py, f64>, lon: PyReadonlyArray1<'py, f64>, max_m: f64,
                      step: f64) -> (Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>) {
    let (lat, lon) = (lat.as_array(), lon.as_array());
    let n = lat.len().min(lon.len());
    let (mut ola, mut olo) = (Vec::new(), Vec::new());
    if n > 0 {
        ola.push(lat[0]);
        olo.push(lon[0]);
    }
    let m = 111320.0;
    let c = lat.get(0).copied().unwrap_or(0.0).to_radians().cos();
    let xy = |i: usize| (lon[i] * m * c, lat[i] * m);
    let (mut next, mut cum) = (step, 0.0);
    for i in 1..n {
        let ((ax, ay), (bx, by)) = (xy(i - 1), xy(i));
        let seg = (bx - ax).hypot(by - ay);
        while step > 0.0 && next <= cum + seg && next <= max_m {
            let t = if seg > 0.0 { (next - cum) / seg } else { 0.0 };
            ola.push(lat[i - 1] + (lat[i] - lat[i - 1]) * t);
            olo.push(lon[i - 1] + (lon[i] - lon[i - 1]) * t);
            next += step;
        }
        cum += seg;
        if cum >= max_m {
            break;
        }
    }
    (ola.into_pyarray(py), olo.into_pyarray(py))
}

/// grading.median_profile: each sample the median of the samples within half of it (the window shrunk evenly near the
/// ends: an end keeps its own value, a constant grade stays). A bridge deck in the elevation data (a spike a few
/// samples wide) goes; slopes, crests and steps longer than the window stay.
#[pyfunction]
fn median_profile<'py>(py: Python<'py>, z: PyReadonlyArray1<'py, f64>, half: usize) -> Bound<'py, PyArray1<f64>> {
    let z = z.as_array();
    let n = z.len();
    let mut buf: Vec<f64> = Vec::with_capacity(2 * half + 1);
    let out: Vec<f64> = (0..n).map(|i| {
        let h = half.min(i).min(n - 1 - i);
        buf.clear();
        buf.extend((i - h..=i + h).map(|j| z[j]));
        buf.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        buf[h]
    }).collect();
    out.into_pyarray(py)
}

/// grading.smooth_profile: Gaussian smoothing of a profile sampled every step m (turned about its end points past the
/// ends, so the ends keep their heights and a constant grade stays).
#[pyfunction]
fn smooth_profile<'py>(py: Python<'py>, z: PyReadonlyArray1<'py, f64>, step: f64, sigma: f64)
                       -> Bound<'py, PyArray1<f64>> {
    let z = z.as_array();
    let n = z.len();
    if n < 3 || sigma <= 0.0 {
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
    // Past the ends: the profile turned about its end point (2 z[0] - z[-i]), so a constant grade carries on to the end
    // and the end keeps its own height. Mirrored (z[-i]), the end was the average of the one side it has: on a 20 %
    // grade 1-2 m off the ground, the next part of the road (the same way split at a junction) off the other way, and
    // the two met in a step at every junction on a slope (GitHub #266). Beyond what one turn reaches (a part shorter than
    // the kernel), the straight line through the part's ends.
    let m = n as isize;
    let grade = (z[n - 1] - z[0]) / (n - 1) as f64;
    let at = |i: isize| -> f64 {
        if i < 0 {
            if -i < m { 2.0 * z[0] - z[(-i) as usize] } else { z[0] + grade * i as f64 }
        } else if i >= m {
            let j = 2 * (m - 1) - i;
            if j >= 0 { 2.0 * z[n - 1] - z[j as usize] } else { z[n - 1] + grade * (i - (m - 1)) as f64 }
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

/// imagery.mip_arrays below level 0: the levels 1.. (uint8 [h, w, c], c 3 or 4) of a power-of-two image: linear-light
/// (lut: the 256 float32 values of _TO_LIN) 2 x 2 averages, each from the previous unsharpened one, then sharpened
/// by sharpen (3 x 3 box, edge-padded) where both sides are >= 4, back to sRGB (x ** (1 / 2.2)), as the numpy code
/// in float32. A 4th channel (alpha: the terrain cache's ambient occlusion) is averaged as it is, not sharpened (the
/// 3-channel loop read an RGBA image's bytes as RGB: grey mips under the photo's, GitHub #115, 2026-10-23).
#[pyfunction]
fn mip_levels<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, lut: PyReadonlyArray1<'py, f32>, sharpen: f32)
                   -> PyResult<Vec<Bound<'py, numpy::PyArray3<u8>>>> {
    let img = rgb.as_array();
    let lut = lut.as_array();
    let nc = img.shape()[2];
    if nc != 3 && nc != 4 {
        return Err(pyo3::exceptions::PyValueError::new_err(format!("mip_levels: 3 or 4 channels, not {}", nc)));
    }
    let levels = py.allow_threads(|| {
    let (mut h, mut w) = (img.shape()[0], img.shape()[1]);
    let to_lin = |i: usize, v: u8| -> f32 { if i % nc < 3 { lut[v as usize] } else { v as f32 / 255.0f32 } };
    let mut lin: Vec<f32> = match img.as_slice() {
        Some(sl) => sl.iter().enumerate().map(|(i, &v)| to_lin(i, v)).collect(),
        None => img.iter().enumerate().map(|(i, &v)| to_lin(i, v)).collect(),
    };
    let mut out = Vec::new();
    while h.max(w) > 1 {
        let (mut nh, mut nw) = (h, w);
        if h > 1 {
            nh = h / 2;
            let mut nl = vec![0f32; nh * w * nc];
            for y in 0..nh {
                for x in 0..w * nc {
                    nl[y * w * nc + x] = 0.5f32 * (lin[(2 * y) * w * nc + x] + lin[(2 * y + 1) * w * nc + x]);
                }
            }
            lin = nl;
        }
        if w > 1 {
            nw = w / 2;
            let mut nl = vec![0f32; nh * nw * nc];
            for y in 0..nh {
                for x in 0..nw {
                    for c in 0..nc {
                        nl[(y * nw + x) * nc + c] =
                            0.5f32 * (lin[(y * w + 2 * x) * nc + c] + lin[(y * w + 2 * x + 1) * nc + c]);
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
                lin[(yy * w + xx) * nc + c]
            };
            let mut l2 = lin.clone();           // (alpha kept as averaged)
            let row = w * nc;
            // the interior: no clamping (the same sum, term by term)
            for y in 1..h - 1 {
                for x in 1..w - 1 {
                    for c in 0..3 {
                        let q = y * row + x * nc + c;
                        let blur = (lin[q - row - nc] + lin[q - row] + lin[q - row + nc] + lin[q - nc] + lin[q]
                            + lin[q + nc] + lin[q + row - nc] + lin[q + row] + lin[q + row + nc]) / 9.0f32;
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
                        l2[((y as usize) * w + x as usize) * nc + c] = v + sharpen * (v - blur);
                    }
                }
            }
            l2
        } else {
            lin.clone()
        };
        let th = srgb_thresholds();
        let bytes: Vec<u8> = lev.iter().enumerate().map(|(i, &v)| {
            let c = v.clamp(0.0, 1.0);
            if i % nc < 3 {
                th.partition_point(|&t| t <= c) as u8      // the byte the formula gives (monotonic: thresholds)
            } else {
                (c * 255.0f32 + 0.5f32) as u8
            }
        }).collect();
        out.push(numpy::ndarray::Array3::from_shape_vec((h, w, nc), bytes).unwrap());
    }
    out
    });
    Ok(levels.into_iter().map(|a| a.into_pyarray(py)).collect())
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

/// A TIFF tile of float32 samples stored with the floating-point predictor (3; the Copernicus DEM): each row holds
/// its w values' bytes as 4 planes, most significant first, differenced byte by byte along the row (cog.py). Returns
/// the (h, w) values in native order.
#[pyfunction]
fn float_predictor_f32<'py>(py: Python<'py>, data: &[u8], w: usize, h: usize) -> PyResult<Bound<'py, PyArray2<f32>>> {
    let row = w * 4;
    if data.len() < row * h {
        return Err(pyo3::exceptions::PyValueError::new_err("float_predictor_f32: tile data too short"));
    }
    let out = py.allow_threads(|| {
        let mut v = vec![0f32; w * h];
        let mut b = vec![0u8; row];
        for r in 0..h {
            let src = &data[r * row..(r + 1) * row];
            let mut acc = 0u8;
            for (k, &x) in src.iter().enumerate() {
                acc = acc.wrapping_add(x);
                b[k] = acc;
            }
            for c in 0..w {
                v[r * w + c] = f32::from_be_bytes([b[c], b[w + c], b[2 * w + c], b[3 * w + c]]);
            }
        }
        v
    });
    Ok(numpy::ndarray::Array2::from_shape_vec((h, w), out).unwrap().into_pyarray(py))
}

/// Bilinear samples of a grid (h, w float32, h and w >= 2) at fractional pixel coordinates (fx, fy: column, row of
/// pixel centres): the cell's corners clamped into the grid (ix in [0, w - 2]) and the weights into [0, 1], as
/// dem.sample did in numpy (float64 arithmetic in its order). The DEM's Web-Mercator mosaics and the polar DEM's
/// latitude / longitude grids (polardem.py).
#[pyfunction]
fn bilinear_grid<'py>(
    py: Python<'py>,
    grid: PyReadonlyArray2<'py, f32>,
    fx: PyReadonlyArray1<'py, f64>,
    fy: PyReadonlyArray1<'py, f64>,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let g = grid.as_array();
    let (h, w) = (g.shape()[0], g.shape()[1]);
    if h < 2 || w < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("bilinear_grid: the grid needs 2 x 2 pixels"));
    }
    let (xs, ys) = (fx.as_array(), fy.as_array());
    if xs.len() != ys.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("bilinear_grid: fx and fy differ in length"));
    }
    let out = py.allow_threads(|| {
        numpy::ndarray::Array1::from_shape_fn(xs.len(), |i| {
            let (x, y) = (xs[i], ys[i]);
            let ix = (x.floor() as i64).clamp(0, w as i64 - 2) as usize;
            let iy = (y.floor() as i64).clamp(0, h as i64 - 2) as usize;
            let wx = (x - ix as f64).clamp(0.0, 1.0);
            let wy = (y - iy as f64).clamp(0.0, 1.0);
            let (v00, v10) = (g[[iy, ix]] as f64, g[[iy, ix + 1]] as f64);
            let (v01, v11) = (g[[iy + 1, ix]] as f64, g[[iy + 1, ix + 1]] as f64);
            (v00 * (1.0 - wx) + v10 * wx) * (1.0 - wy) + (v01 * (1.0 - wx) + v11 * wx) * wy
        })
    });
    Ok(out.into_pyarray(py))
}

/// Python's math.hypot(x, y), exactly (CPython's vector_norm: the squares summed losslessly after scaling by a power of
/// two, one differential correction): libm's hypot differs from it in the last bit for ~13 % of inputs, enough to change
/// a building's oriented box (2026-10-26). For ports of Python code that calls math.hypot.
pub fn py_hypot(x: f64, y: f64) -> f64 {
    let (ax, ay) = (x.abs(), y.abs());
    if ax.is_infinite() || ay.is_infinite() {
        return f64::INFINITY;
    }
    if ax.is_nan() || ay.is_nan() {
        return f64::NAN;
    }
    let max = ax.max(ay);
    if max == 0.0 {
        return max;
    }
    let max_e = frexp_exp(max);
    if max_e < -1023 {
        return f64::MIN_POSITIVE * py_hypot(ax / f64::MIN_POSITIVE, ay / f64::MIN_POSITIVE);
    }
    let scale = ldexp1(-max_e);
    let dl_mul = |a: f64, b: f64| { let z = a * b; (z, a.mul_add(b, -z)) };
    let dl_fast_sum = |a: f64, b: f64| { let s = a + b; let z = s - a; (s, b - z) };
    let (mut csum, mut frac1, mut frac2) = (1.0f64, 0.0f64, 0.0f64);
    for v in [ax, ay] {
        let x = v * scale;
        let (hi, lo) = dl_mul(x, x);
        let (s, e) = dl_fast_sum(csum, hi);
        csum = s;
        frac1 += lo;
        frac2 += e;
    }
    let mut h = (csum - 1.0 + (frac1 + frac2)).sqrt();
    let (hi, lo) = dl_mul(-h, h);
    let (s, e) = dl_fast_sum(csum, hi);
    csum = s;
    frac1 += lo;
    frac2 += e;
    let x = csum - 1.0 + (frac1 + frac2);
    h += x / (2.0 * h);
    h / scale
}

/// frexp's exponent: x = m * 2^e with 0.5 <= m < 1 (x finite, > 0).
fn frexp_exp(x: f64) -> i32 {
    let bits = x.to_bits();
    let e = ((bits >> 52) & 0x7ff) as i32;
    if e == 0 {
        // subnormal: normalise first
        let y = x * f64::from_bits(0x4350000000000000);  // 2^54
        return frexp_exp(y) - 54;
    }
    e - 1022
}

/// ldexp(1.0, e) for the exponents vector_norm uses (-1022 ..= 1024).
fn ldexp1(e: i32) -> f64 {
    if e >= -1022 && e <= 1023 {
        f64::from_bits(((e + 1023) as u64) << 52)
    } else if e > 1023 {
        f64::INFINITY
    } else if e >= -1074 {
        f64::from_bits(1u64 << (e + 1074))          // (subnormal: 2^-1024 for a max near f64::MAX)
    } else {
        0.0
    }
}

#[pyfunction]
fn py_hypot_test<'py>(py: Python<'py>, x: PyReadonlyArray1<'py, f64>, y: PyReadonlyArray1<'py, f64>) -> Bound<'py, PyArray1<f64>> {
    let (x, y) = (x.as_array(), y.as_array());
    numpy::ndarray::Array1::from_shape_fn(x.len(), |i| py_hypot(x[i], y[i])).into_pyarray(py)
}

/// Python's repr() of a finite float (the shortest digits that read back the same; fixed notation for a decimal
/// exponent from -4 to 15, else d.ddde-XX), for hashes made of repr()s (buildings._hash01).
pub fn py_float_repr(x: f64) -> String {
    if x == 0.0 {
        return if x.is_sign_negative() { "-0.0".into() } else { "0.0".into() };
    }
    let s = format!("{:e}", x);                 // shortest round-trip digits: "-1.2345e-7"
    let (mant, exp) = s.split_once('e').unwrap();
    let exp: i32 = exp.parse().unwrap();
    let (neg, mant) = if let Some(m) = mant.strip_prefix('-') { (true, m) } else { (false, mant) };
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let decpt = exp + 1;                        // digits d1 d2 ... = 0.d1d2... x 10^decpt
    let mut out = String::new();
    if neg { out.push('-'); }
    if decpt > -4 && decpt <= 16 {
        if decpt <= 0 {
            out.push_str("0.");
            for _ in 0..(-decpt) { out.push('0'); }
            out.push_str(&digits);
        } else if (decpt as usize) >= digits.len() {
            out.push_str(&digits);
            for _ in 0..(decpt as usize - digits.len()) { out.push('0'); }
            out.push_str(".0");
        } else {
            out.push_str(&digits[..decpt as usize]);
            out.push('.');
            out.push_str(&digits[decpt as usize..]);
        }
    } else {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        let e = decpt - 1;
        out.push('e');
        out.push(if e < 0 { '-' } else { '+' });
        out.push_str(&format!("{:02}", e.abs()));
    }
    out
}

/// BLAKE2b (RFC 7693) of data with an 8-byte digest, unkeyed: hashlib.blake2b(data, digest_size=8).digest() as a
/// little-endian u64.
pub fn blake2b_8(data: &[u8]) -> u64 {
    const IV: [u64; 8] = [0x6a09e667f3bcc908, 0xbb67ae8584caa73b, 0x3c6ef372fe94f82b, 0xa54ff53a5f1d36f1,
                          0x510e527fade682d1, 0x9b05688c2b3e6c1f, 0x1f83d9abfb41bd6b, 0x5be0cd19137e2179];
    const SIGMA: [[usize; 16]; 12] = [
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], [14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3],
        [11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4], [7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8],
        [9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13], [2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9],
        [12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11], [13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10],
        [6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5], [10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0],
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], [14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3]];
    let mut h = IV;
    h[0] ^= 0x0101_0000 ^ 8;                    // digest length 8, no key, fanout 1, depth 1
    let compress = |h: &mut [u64; 8], block: &[u8; 128], t: u128, last: bool| {
        let mut m = [0u64; 16];
        for i in 0..16 {
            m[i] = u64::from_le_bytes(block[i * 8..i * 8 + 8].try_into().unwrap());
        }
        let mut v = [0u64; 16];
        v[..8].copy_from_slice(h);
        v[8..].copy_from_slice(&IV);
        v[12] ^= t as u64;
        v[13] ^= (t >> 64) as u64;
        if last { v[14] = !v[14]; }
        let g = |v: &mut [u64; 16], a: usize, b: usize, c: usize, d: usize, x: u64, y: u64| {
            v[a] = v[a].wrapping_add(v[b]).wrapping_add(x); v[d] = (v[d] ^ v[a]).rotate_right(32);
            v[c] = v[c].wrapping_add(v[d]); v[b] = (v[b] ^ v[c]).rotate_right(24);
            v[a] = v[a].wrapping_add(v[b]).wrapping_add(y); v[d] = (v[d] ^ v[a]).rotate_right(16);
            v[c] = v[c].wrapping_add(v[d]); v[b] = (v[b] ^ v[c]).rotate_right(63);
        };
        for s in SIGMA.iter() {
            g(&mut v, 0, 4, 8, 12, m[s[0]], m[s[1]]); g(&mut v, 1, 5, 9, 13, m[s[2]], m[s[3]]);
            g(&mut v, 2, 6, 10, 14, m[s[4]], m[s[5]]); g(&mut v, 3, 7, 11, 15, m[s[6]], m[s[7]]);
            g(&mut v, 0, 5, 10, 15, m[s[8]], m[s[9]]); g(&mut v, 1, 6, 11, 12, m[s[10]], m[s[11]]);
            g(&mut v, 2, 7, 8, 13, m[s[12]], m[s[13]]); g(&mut v, 3, 4, 9, 14, m[s[14]], m[s[15]]);
        }
        for i in 0..8 { h[i] ^= v[i] ^ v[i + 8]; }
    };
    let n = data.len();
    let mut off = 0usize;
    while n - off > 128 {
        let block: [u8; 128] = data[off..off + 128].try_into().unwrap();
        off += 128;
        compress(&mut h, &block, off as u128, false);
    }
    let mut block = [0u8; 128];
    block[..n - off].copy_from_slice(&data[off..]);
    compress(&mut h, &block, n as u128, true);
    h[0]
}

/// buildings._hash01 of a tuple of a float and ints (repr((seed, a, b, ...))): a number in [0, 1).
pub fn py_hash01_fints(seed: f64, ints: &[i64]) -> f64 {
    let mut s = String::from("(");
    s.push_str(&py_float_repr(seed));
    for v in ints {
        s.push_str(", ");
        s.push_str(&v.to_string());
    }
    if ints.is_empty() {
        s.push(',');                            // (a one-element tuple: "(0.5,)")
    }
    s.push(')');
    blake2b_8(s.as_bytes()) as f64 / 18446744073709551616.0
}

#[pyfunction]
fn hash01_test(seeds: Vec<f64>, ints: Vec<Vec<i64>>) -> (Vec<f64>, Vec<String>) {
    (seeds.iter().zip(&ints).map(|(&s, iv)| py_hash01_fints(s, iv)).collect(),
     seeds.iter().map(|&s| py_float_repr(s)).collect())
}

/// Python's float floor division x // y for finite y > 0 (CPython's float_floor_div: through fmod, not floor(x / y)).
fn py_floordiv(x: f64, y: f64) -> f64 {
    let m = x % y;
    let mut div = (x - m) / y;
    if m != 0.0 && ((y < 0.0) != (m < 0.0)) {
        div -= 1.0;
    }
    if div != 0.0 {
        let fd = div.floor();
        if div - fd > 0.5 { fd + 1.0 } else { fd }
    } else {
        0.0f64.copysign(x / y)
    }
}

/// buildings._walls (with _plain_wall_with_windows, _plain_strip, _band_v): each wall of the ring floor by floor as
/// quads, in the Python's order and float arithmetic (math.hypot: py_hypot, round(): half to even, _hash01: blake2b of
/// the repr). Returns (positions: 12 per quad, uvs: 8 per quad, material per quad: 0 the facade, 1 the shopfront,
/// 2 the window white). c: the constants (see buildings._walls). door_edge / front: -1 for none.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn building_walls(py: Python<'_>, ring: PyReadonlyArray2<'_, f64>, z0: f64, z_eave: f64, wall: bool, plain: bool,
                  band_k: i64, bpt: i64, good: Vec<i64>, floors: i64, floor_h: f64, shop: bool, door_edge: i64,
                  door_pos: f64, roof_flat: bool, seed: f64, windows_false: bool, front: i64, lite: bool, c: Vec<f64>)
                  -> PyResult<(Vec<f64>, Vec<f64>, Vec<u8>)> {
    if c.len() != 18 {
        return Err(pyo3::exceptions::PyValueError::new_err("building_walls: 18 constants"));
    }
    let r = ring.as_array();
    let pts: Vec<(f64, f64)> = (0..r.nrows()).map(|i| (r[[i, 0]], r[[i, 1]])).collect();
    let (siding_uv, tile_px, foundation, bands, shop_k, shop_bpt, door_gap, plain_strip) =
        (c[0], c[1], c[2], c[3], c[4] as i64, c[5] as i64, c[6], c[7]);
    let (gf_shop, win_out, house_bay) = (c[8], c[9], c[10]);
    let (wu0, wu1, wv0, wv1, win_w, win_h, win_sill) = (c[11], c[12], c[13], c[14], c[15], c[16], c[17]);
    Ok(py.allow_threads(|| {
        let mut pp: Vec<f64> = Vec::new();
        let mut uv: Vec<f64> = Vec::new();
        let mut mid: Vec<u8> = Vec::new();
        let mut quad = |p: [(f64, f64, f64); 4], t: [(f64, f64); 4], m: u8| {
            for q in p.iter() { pp.push(q.0); pp.push(q.1); pp.push(q.2); }
            for q in t.iter() { uv.push(q.0); uv.push(q.1); }
            mid.push(m);
        };
        let band_v = |k: i64| {
            let eps = 1.5 / 1024.0;
            (1.0 - (k + 1) as f64 / bands + eps, 1.0 - k as f64 / bands - eps)
        };
        let rnd = |x: f64| x.round_ties_even() as i64;
        // _plain_strip
        let strip = |quad: &mut dyn FnMut([(f64, f64, f64); 4], [(f64, f64); 4], u8), pa: (f64, f64), pb: (f64, f64),
                     za: f64, zb: f64, bpt: i64, bay_w: f64, v0: f64, v1: f64, m: u8, whole: bool| {
            let (ax, ay, bx, by) = (pa.0, pa.1, pb.0, pb.1);
            let (dx, dy) = (bx - ax, by - ay);
            let l = py_hypot(dx, dy);
            if l < 1e-3 {
                return;
            }
            let (cc, h) = (1.0 / bpt as f64, 0.5 * plain_strip / bpt as f64);
            let sw = (plain_strip * bay_w).max(0.1);
            let sw = if 0.1 > plain_strip * bay_w { 0.1 } else { sw };
            let n = if whole { 1 } else { rnd(l / sw).max(1) };
            for j in 0..n {
                let (t0, t1) = (j as f64 / n as f64, (j + 1) as f64 / n as f64);
                let (qax, qay, qbx, qby) = (ax + dx * t0, ay + dy * t0, ax + dx * t1, ay + dy * t1);
                quad([(qax, qay, za), (qbx, qby, za), (qbx, qby, zb), (qax, qay, zb)],
                     [(cc - h, v0), (cc + h, v0), (cc + h, v1), (cc - h, v1)], m);
            }
        };
        let n = pts.len();
        for i in 0..n {
            let (a, b) = (pts[i], pts[(i + 1) % n]);
            let l = py_hypot(b.0 - a.0, b.1 - a.1);
            if l < 0.05 {
                continue;
            }
            if wall {
                // _plain_wall_with_windows
                let su = siding_uv;
                quad([(a.0, a.1, z0), (b.0, b.1, z0), (b.0, b.1, z_eave), (a.0, a.1, z_eave)],
                     [(0.0, z0 / su), (l / su, z0 / su), (l / su, z_eave / su), (0.0, z_eave / su)], 0);
                if windows_false {
                    if front >= 0 && front == i as i64 && l > 4.0 {
                        let d = ((b.0 - a.0) / l, (b.1 - a.1) / l);
                        let out = (d.1 * win_out, -d.0 * win_out);
                        let base = z_eave - floors as f64 * floor_h;
                        let zt = (base + gf_shop).min(z_eave - 0.6);
                        let zt = if z_eave - 0.6 < base + gf_shop { z_eave - 0.6 } else { base + gf_shop };
                        let nbs = rnd(l * 0.7 / (tile_px / shop_bpt as f64)).max(1);
                        let (v0, v1) = band_v(shop_k);
                        let (s15, s85) = (l * 0.15, l * 0.85);
                        let p = (a.0 + d.0 * s15 + out.0, a.1 + d.1 * s15 + out.1);
                        let q = (a.0 + d.0 * s85 + out.0, a.1 + d.1 * s85 + out.1);
                        let un = nbs as f64 / shop_bpt as f64;
                        let _ = zt;
                        let ztop = if z_eave - 0.6 < base + gf_shop { z_eave - 0.6 } else { base + gf_shop };
                        quad([(p.0, p.1, base), (q.0, q.1, base), (q.0, q.1, ztop), (p.0, p.1, ztop)],
                             [(0.0, v0), (un, v0), (un, v1), (0.0, v1)], 1);
                    }
                    continue;
                }
                let nb = py_floordiv(l, house_bay) as i64;
                if nb < 1 || l < 2.0 {
                    continue;
                }
                let d = ((b.0 - a.0) / l, (b.1 - a.1) / l);
                let out = (d.1 * win_out, -d.0 * win_out);
                let base = z_eave - floors as f64 * floor_h;
                for f in 0..floors {
                    let zf = base + f as f64 * floor_h + win_sill;
                    if zf + win_h > z_eave - 0.2 {
                        continue;
                    }
                    for k in 0..nb {
                        if f == 0 && nb % 2 == 1 && k == nb / 2 {
                            continue;
                        }
                        let s = l * (k as f64 + 0.5) / nb as f64;
                        let cc = (a.0 + d.0 * s + out.0, a.1 + d.1 * s + out.1);
                        let hw = (d.0 * win_w / 2.0, d.1 * win_w / 2.0);
                        let p = (cc.0 - hw.0, cc.1 - hw.1);
                        let q = (cc.0 + hw.0, cc.1 + hw.1);
                        quad([(p.0, p.1, zf), (q.0, q.1, zf), (q.0, q.1, zf + win_h), (p.0, p.1, zf + win_h)],
                             [(wu0, wv0), (wu1, wv0), (wu1, wv1), (wu0, wv1)], 2);
                    }
                }
                continue;
            }
            let (ax, ay, bx, by) = (a.0, a.1, b.0, b.1);
            if plain {
                let u1 = l / siding_uv;
                quad([(ax, ay, z0), (bx, by, z0), (bx, by, z_eave), (ax, ay, z_eave)],
                     [(0.0, z0 / siding_uv), (u1, z0 / siding_uv), (u1, z_eave / siding_uv), (0.0, z_eave / siding_uv)], 0);
                continue;
            }
            let bay = tile_px / bpt as f64;
            let nb = rnd(l / bay);
            let u1 = if nb >= 1 { nb as f64 / bpt as f64 } else { 0.02 * l };
            let base = z_eave - floors as f64 * floor_h;
            let mut za = z0;
            if base - z0 > foundation + 0.4 {
                let (pv0, pv1) = band_v(band_k);
                let hp = (base - 0.3) - z0;
                let fh = if floor_h > 1e-3 { floor_h } else { 1e-3 };
                let fh = if 1e-3 > floor_h { 1e-3 } else { fh };
                let ratio = hp / fh;
                let ratio = if ratio < 1.0 { ratio } else { 1.0 };
                strip(&mut quad, a, b, z0, base - 0.3, bpt, bay, pv0, pv0 + (pv1 - pv0) * ratio, 0, lite);
                za = base - 0.3;
            }
            for f in 0..floors {
                let zb = if f == floors - 1 { z_eave } else { base + (f + 1) as f64 * floor_h };
                let (m, bk, uu);
                if f == 0 && shop {
                    m = 1u8;
                    bk = shop_k;
                    let nbs = rnd(l / (tile_px / shop_bpt as f64)).max(1);
                    uu = if l > 2.0 { nbs as f64 / shop_bpt as f64 } else { 0.02 * l };
                } else {
                    m = 0u8;
                    bk = band_k;
                    uu = u1;
                    if !good.is_empty() && nb >= 1 && !lite {
                        let (v0, v1) = band_v(bk);
                        let mut dk: i64 = -1;
                        if f == 0 && door_edge >= 0 && door_edge == i as i64 && roof_flat {
                            dk = (nb - 1).min((door_pos / l * nb as f64).trunc() as i64);
                        }
                        let (dx, dy) = (bx - ax, by - ay);
                        for k in 0..nb {
                            let hsh = py_hash01_fints(seed, &[i as i64, f, k]);
                            let g = good[(hsh * good.len() as f64).trunc() as usize];
                            let (t0, t1) = (k as f64 / nb as f64, (k + 1) as f64 / nb as f64);
                            let (pa, pb) = ((ax + dx * t0, ay + dy * t0), (ax + dx * t1, ay + dy * t1));
                            if k == dk {
                                strip(&mut quad, pa, pb, za, zb, bpt, bay, v0, v1, m, false);
                                continue;
                            }
                            let (g0, g1) = (g as f64 / bpt as f64, (g + 1) as f64 / bpt as f64);
                            quad([(pa.0, pa.1, za), (pb.0, pb.1, za), (pb.0, pb.1, zb), (pa.0, pa.1, zb)],
                                 [(g0, v0), (g1, v0), (g1, v1), (g0, v1)], m);
                        }
                        za = zb;
                        continue;
                    }
                }
                let (v0, v1) = band_v(bk);
                let has_door = f == 0 && roof_flat && door_edge >= 0 && door_edge == i as i64;
                if has_door && door_gap < door_pos && door_pos < l - door_gap {
                    let (mut s0, mut s1) = (door_pos - door_gap, door_pos + door_gap);
                    if nb >= 1 {
                        let bw = l / nb as f64;
                        let f0 = (s0 / bw).floor() * bw;
                        let f1 = (s1 / bw).ceil() * bw;
                        s0 = if f0 > 0.0 { f0 } else { 0.0 };
                        s1 = if f1 < l { f1 } else { l };
                    }
                    for (ta, tb) in [(0.0, s0), (s1, l)] {
                        if tb - ta < 1e-3 {
                            continue;
                        }
                        let (fa, fb) = (ta / l, tb / l);
                        let pa = (ax + (bx - ax) * fa, ay + (by - ay) * fa);
                        let pb = (ax + (bx - ax) * fb, ay + (by - ay) * fb);
                        quad([(pa.0, pa.1, za), (pb.0, pb.1, za), (pb.0, pb.1, zb), (pa.0, pa.1, zb)],
                             [(uu * ta / l, v0), (uu * tb / l, v0), (uu * tb / l, v1), (uu * ta / l, v1)], m);
                    }
                    let (fa, fb) = (s0 / l, s1 / l);
                    let pa = (ax + (bx - ax) * fa, ay + (by - ay) * fa);
                    let pb = (ax + (bx - ax) * fb, ay + (by - ay) * fb);
                    strip(&mut quad, pa, pb, za, zb, bpt, bay, v0, v1, m, lite);
                    za = zb;
                    continue;
                }
                quad([(ax, ay, za), (bx, by, za), (bx, by, zb), (ax, ay, zb)],
                     [(0.0, v0), (uu, v0), (uu, v1), (0.0, v1)], m);
                za = zb;
            }
        }
        drop(quad);
        (pp, uv, mid)
    }))
}

/// buildings.triangulate: ear clipping of the counter-clockwise simple ring p (n, 2), (n - 2, 3) indices into p (fewer if
/// the ring is not simple: the rest is left out); the Python's order, tests and float arithmetic exactly (its loops were
/// a large part of a far buildings tile's roofs and floors, 2026-10-26).
#[pyfunction]
fn triangulate<'py>(py: Python<'py>, p: PyReadonlyArray2<'py, f64>) -> Bound<'py, PyArray2<i64>> {
    let a = p.as_array();
    let n = a.nrows();
    let pts: Vec<(f64, f64)> = (0..n).map(|i| (a[[i, 0]], a[[i, 1]])).collect();
    let out = py.allow_threads(|| {
        let cross = |a: (f64, f64), b: (f64, f64), c: (f64, f64)| (b.0 - a.0) * (c.1 - a.1) - (b.1 - a.1) * (c.0 - a.0);
        let mut idx: Vec<usize> = (0..n).collect();
        let mut out: Vec<[i64; 3]> = Vec::new();
        let mut guard = 0usize;
        while idx.len() > 3 && guard < 4 * n * n {
            guard += 1;
            let m = idx.len();
            let mut found = false;
            for k in 0..m {
                let (i0, i1, i2) = (idx[(k + m - 1) % m], idx[k], idx[(k + 1) % m]);
                let (pa, pb, pc) = (pts[i0], pts[i1], pts[i2]);
                if cross(pa, pb, pc) <= 1e-12 {
                    continue;
                }
                let mut ear = true;
                for &j in &idx {
                    if j == i0 || j == i1 || j == i2 {
                        continue;
                    }
                    let q = pts[j];
                    if cross(pa, pb, q) >= 0.0 && cross(pb, pc, q) >= 0.0 && cross(pc, pa, q) >= 0.0 {
                        ear = false;
                        break;
                    }
                }
                if ear {
                    out.push([i0 as i64, i1 as i64, i2 as i64]);
                    idx.remove(k);
                    found = true;
                    break;
                }
            }
            if !found {
                break;
            }
        }
        if idx.len() == 3 {
            out.push([idx[0] as i64, idx[1] as i64, idx[2] as i64]);
        }
        out
    });
    let flat: Vec<i64> = out.iter().flat_map(|t| t.iter().copied()).collect();
    numpy::ndarray::Array2::from_shape_vec((out.len(), 3), flat).unwrap().into_pyarray(py)
}

/// buildings.clean_ring's simplification: the ring (n, 2) without its closing point (np.allclose), its nearly collinear
/// or duplicate vertices dropped (every other candidate per round, up to 8 rounds; tol x 1.6 while more than
/// max_verts are left), in numpy's float arithmetic; None when fewer than 3 are left. (Its orientation stays in Python.)
#[pyfunction]
fn clean_ring_core<'py>(py: Python<'py>, p: PyReadonlyArray2<'py, f64>, tol: f64, max_verts: usize)
                        -> Option<Bound<'py, PyArray2<f64>>> {
    let a = p.as_array();
    let mut pts: Vec<(f64, f64)> = (0..a.nrows()).map(|i| (a[[i, 0]], a[[i, 1]])).collect();
    let got = py.allow_threads(|| {
        if pts.len() > 1 {
            let ((ax, ay), (bx, by)) = (pts[0], pts[pts.len() - 1]);
            if (ax - bx).abs() <= 1e-08 + 1e-05 * bx.abs() && (ay - by).abs() <= 1e-08 + 1e-05 * by.abs() {
                pts.pop();
            }
        }
        let mut tol = tol;
        for _ in 0..8 {
            let n = pts.len();
            if n < 3 {
                return None;
            }
            let mut drop = vec![false; n];
            let mut any = false;
            for i in 0..n {
                let prev = pts[(i + n - 1) % n];
                let nxt = pts[(i + 1) % n];
                let (dx, dy) = (nxt.0 - prev.0, nxt.1 - prev.1);
                let l = dx.hypot(dy);
                let off = (dx * (pts[i].1 - prev.1) - dy * (pts[i].0 - prev.0)).abs() / l.max(1e-9);
                let near = (pts[i].0 - prev.0).hypot(pts[i].1 - prev.1) < 1e-3;
                if off < tol || near {
                    drop[i] = true;
                    any = true;
                }
            }
            if !any {
                if n <= max_verts {
                    break;
                }
                tol *= 1.6;
                continue;
            }
            let mut k = 0usize;             // every other candidate (idx[::2]) goes
            let mut keep = Vec::with_capacity(n);
            for i in 0..n {
                if drop[i] {
                    if k % 2 == 1 { keep.push(pts[i]); }
                    k += 1;
                } else {
                    keep.push(pts[i]);
                }
            }
            pts = keep;
        }
        if pts.len() < 3 { None } else { Some(pts) }
    });
    got.map(|v| {
        let flat: Vec<f64> = v.iter().flat_map(|&(x, y)| [x, y]).collect();
        numpy::ndarray::Array2::from_shape_vec((v.len(), 2), flat).unwrap().into_pyarray(py)
    })
}

/// buildings.oriented_box's search: along which edge of ring p (n, 2) the rectangle around it is smallest, the first
/// such edge: (ux, uy, vx, vy, amin, amax, bmin, bmax) in the Python's float arithmetic (math.hypot: py_hypot), or None
/// when every edge is shorter than 1e-6.
#[pyfunction]
fn oriented_box_core(py: Python<'_>, p: PyReadonlyArray2<'_, f64>) -> Option<(f64, f64, f64, f64, f64, f64, f64, f64)> {
    let a = p.as_array();
    let pts: Vec<(f64, f64)> = (0..a.nrows()).map(|i| (a[[i, 0]], a[[i, 1]])).collect();
    py.allow_threads(|| {
        let n = pts.len();
        let mut best: Option<(f64, (f64, f64, f64, f64, f64, f64, f64, f64))> = None;
        for i in 0..n {
            let (x0, y0) = pts[i];
            let (x1, y1) = pts[(i + 1) % n];
            let (ex, ey) = (x1 - x0, y1 - y0);
            let l = py_hypot(ex, ey);
            if l < 1e-6 {
                continue;
            }
            let (ux, uy) = (ex / l, ey / l);
            let (vx, vy) = (-uy, ux);
            let (mut amin, mut amax, mut bmin, mut bmax) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
            for &(x, y) in &pts {
                let av = x * ux + y * uy;
                let bv = x * vx + y * vy;
                if av < amin { amin = av; }
                if av > amax { amax = av; }
                if bv < bmin { bmin = bv; }
                if bv > bmax { bmax = bv; }
            }
            let area = (amax - amin) * (bmax - bmin);
            if best.map_or(true, |b| area < b.0) {
                best = Some((area, (ux, uy, vx, vy, amin, amax, bmin, bmax)));
            }
        }
        best.map(|b| b.1)
    })
}

/// bilinear_grid on the mosaic of tiles (ny x nx tiles of tile x tile pixels, row-major: tiles[r * nx + c] is the
/// mosaic's block at row r, column c) without building it: each corner pixel read from its own tile (dem.sample copied
/// every call's tiles into a new mosaic, ~2 s per 16 city road tiles, 2026-10-26). The same clamps and arithmetic.
#[pyfunction]
fn bilinear_tiles<'py>(
    py: Python<'py>,
    tiles: Vec<PyReadonlyArray2<'py, f32>>,
    nx: usize,
    tile: usize,
    fx: PyReadonlyArray1<'py, f64>,
    fy: PyReadonlyArray1<'py, f64>,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    if nx == 0 || tile == 0 || tiles.is_empty() || tiles.len() % nx != 0 {
        return Err(pyo3::exceptions::PyValueError::new_err("bilinear_tiles: tiles must fill ny x nx"));
    }
    let ny = tiles.len() / nx;
    let ts: Vec<_> = tiles.iter().map(|t| t.as_array()).collect();
    if ts.iter().any(|t| t.shape()[0] != tile || t.shape()[1] != tile) {
        return Err(pyo3::exceptions::PyValueError::new_err("bilinear_tiles: every tile must be tile x tile"));
    }
    let (h, w) = (ny * tile, nx * tile);
    if h < 2 || w < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("bilinear_tiles: the mosaic needs 2 x 2 pixels"));
    }
    let (xs, ys) = (fx.as_array(), fy.as_array());
    if xs.len() != ys.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("bilinear_tiles: fx and fy differ in length"));
    }
    let px = |y: usize, x: usize| ts[(y / tile) * nx + x / tile][[y % tile, x % tile]] as f64;
    let out = py.allow_threads(|| {
        numpy::ndarray::Array1::from_shape_fn(xs.len(), |i| {
            let (x, y) = (xs[i], ys[i]);
            let ix = (x.floor() as i64).clamp(0, w as i64 - 2) as usize;
            let iy = (y.floor() as i64).clamp(0, h as i64 - 2) as usize;
            let wx = (x - ix as f64).clamp(0.0, 1.0);
            let wy = (y - iy as f64).clamp(0.0, 1.0);
            let (v00, v10) = (px(iy, ix), px(iy, ix + 1));
            let (v01, v11) = (px(iy + 1, ix), px(iy + 1, ix + 1));
            (v00 * (1.0 - wx) + v10 * wx) * (1.0 - wy) + (v01 * (1.0 - wx) + v11 * wx) * wy
        })
    });
    Ok(out.into_pyarray(py))
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
        let np_ = pv.shape()[0];
        let near = |i: usize, g: &[f64; 6]| {
            let (p0, p1) = (pv[[i, 0]], pv[[i, 1]]);
            let t = (((p0 - g[0]) * g[2] + (p1 - g[1]) * g[3]) / g[4]).clamp(0.0, 1.0);
            let d = (p0 - (g[0] + t * g[2])).hypot(p1 - (g[1] + t * g[3]));
            d < g[5]
        };
        if np_ * ns < 4096 || np_ == 0 {
            return (0..np_).map(|i| seg.iter().any(|g| near(i, g))).collect();
        }
        // a uniform grid over the points: each segment listed in the cells its box (+ its half width) covers, each
        // point tests its own cell's segments (long roads' thousands of segments against a wall's points)
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for i in 0..np_ {
            x0 = x0.min(pv[[i, 0]]); x1 = x1.max(pv[[i, 0]]);
            y0 = y0.min(pv[[i, 1]]); y1 = y1.max(pv[[i, 1]]);
        }
        let cell = (((x1 - x0) * (y1 - y0)).max(1.0) / np_ as f64).sqrt().max(4.0) * 2.0;
        let nx = (((x1 - x0) / cell).floor() as usize + 1).min(4096);
        let ny = (((y1 - y0) / cell).floor() as usize + 1).min(4096);
        let mut grid: Vec<Vec<u32>> = vec![Vec::new(); nx * ny];
        for (k, g) in seg.iter().enumerate() {
            let (ax, ay, bx, by) = (g[0], g[1], g[0] + g[2], g[1] + g[3]);
            let r = g[5];
            let (lx, hx) = (ax.min(bx) - r, ax.max(bx) + r);
            let (ly, hy) = (ay.min(by) - r, ay.max(by) + r);
            if hx < x0 || lx > x1 || hy < y0 || ly > y1 {
                continue;
            }
            let cx0 = (((lx - x0) / cell).floor().max(0.0)) as usize;
            let cx1 = ((((hx - x0) / cell).floor()).max(0.0) as usize).min(nx - 1);
            let cy0 = (((ly - y0) / cell).floor().max(0.0)) as usize;
            let cy1 = ((((hy - y0) / cell).floor()).max(0.0) as usize).min(ny - 1);
            for cy in cy0..=cy1 {
                for cx in cx0..=cx1 {
                    grid[cy * nx + cx].push(k as u32);
                }
            }
        }
        (0..np_).map(|i| {
            let cx = (((pv[[i, 0]] - x0) / cell).floor().max(0.0) as usize).min(nx - 1);
            let cy = (((pv[[i, 1]] - y0) / cell).floor().max(0.0) as usize).min(ny - 1);
            grid[cy * nx + cx].iter().any(|&k| near(i, &seg[k as usize]))
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
        let mut p: Vec<[f64; 2]> = (0..pv.shape()[0]).map(|i| [pv[[i, 0]], pv[[i, 1]]]).collect();
        let mut stack: Vec<[usize; 3]> = (0..tv.shape()[0]).rev()
            .map(|i| [tv[[i, 0]] as usize, tv[[i, 1]] as usize, tv[[i, 2]] as usize]).collect();
        let mut mids: std::collections::HashMap<(usize, usize), usize> = std::collections::HashMap::new();
        let mut out: Vec<[usize; 3]> = Vec::new();
        let max2 = max_edge * max_edge;
        let mut guard = 0usize;
        while let Some(t) = stack.pop() {
            guard += 1;
            if guard > 4_000_000 { out.push(t); continue; }
            let d = |a: usize, b: usize| { let (x, y) = (p[a][0] - p[b][0], p[a][1] - p[b][1]); x * x + y * y };
            let e = [d(t[0], t[1]), d(t[1], t[2]), d(t[2], t[0])];
            let (k, &m) = e.iter().enumerate().fold((0, &e[0]), |acc, (i, v)| if *v > *acc.1 { (i, v) } else { acc });
            if m <= max2 { out.push(t); continue; }
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
        // each mesh row writes its own band of output rows: the bands on up to 4 threads (the same numbers; a block's
        // ground photo waits for two of these warps, ~50 ms each on a Steam Deck)
        let band_len = (cw * px * bands).max(1);
        let nthreads = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1).clamp(1, 4);
        let flat = out.as_slice_mut().expect("a new array is contiguous");
        let mut bands_v: Vec<(usize, &mut [u8])> = flat.chunks_mut(band_len).enumerate().filter(|(r, _)| *r < cells).collect();
        let mut groups: Vec<Vec<(usize, &mut [u8])>> = (0..nthreads).map(|_| Vec::new()).collect();
        for (k, item) in bands_v.drain(..).enumerate() {
            groups[k % nthreads].push(item);
        }
        let clipx = |x: i64| -> usize { (if x < 0 { 0 } else if x < iw { x } else { iw - 1 }) as usize };
        let clipy = |y: i64| -> usize { (if y < 0 { 0 } else if y < ih { y } else { ih - 1 }) as usize };
        let warp_band = |r: usize, band: &mut [u8]| {
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
                            band[((y - by0) * px + x) * bands + b] = px_[b];
                        }
                    }
                }
            }
        };
        std::thread::scope(|sc| {
            for g in groups {
                let wb = &warp_band;
                sc.spawn(move || {
                    for (r, band) in g {
                        wb(r, band);
                    }
                });
            }
        });
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
/// everywhere. In a terrain cell whose corners are more than step_m apart (a cutting's wall: the lattice draws it as teeth)
/// no point goes over the cell's lowest corner + max_up (an edge line climbed those teeth in front of the wall as tents
/// of white up to 1.3 m tall, the McGraw-Graben 2026-10-22: it stays at the road's level, the teeth over it).
/// Returns (points (m, 3), uvs (m, 2), triangles (r, 3), source triangle (r,)); points not shared.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (pts, uv, tris, cell, grid, i0, j0, tol, step_m=f64::INFINITY, max_up=f64::INFINITY))]
fn follow_tris<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, uv: PyReadonlyArray2<'py, f64>,
                    tris: PyReadonlyArray2<'py, i64>, cell: f64, grid: PyReadonlyArray2<'py, f64>, i0: i64, j0: i64,
                    tol: f64, step_m: f64, max_up: f64)
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
            // the triangle's own plane (own heights at its corners): the cap over it
            // in a terrain cell holding a step (corners more than step_m apart: a cutting's wall drawn by the lattice as
            // teeth) no point over its lowest corner + max_up
            let cap = |x: f64, y: f64| -> f64 {
                if !step_m.is_finite() { return f64::INFINITY; }
                let (gx, gy) = (x / cell - i0 as f64, y / cell - j0 as f64);
                let (ny, nx) = (gv.shape()[0] as i64, gv.shape()[1] as i64);
                let li = (gx.floor() as i64).clamp(0, nx - 2) as usize;
                let lj = (gy.floor() as i64).clamp(0, ny - 2) as usize;
                let c = [gv[[lj, li]], gv[[lj, li + 1]], gv[[lj + 1, li]], gv[[lj + 1, li + 1]]];
                let (lo, hi) = c.iter().fold((f64::INFINITY, f64::NEG_INFINITY), |a, &v| (a.0.min(v), a.1.max(v)));
                if hi - lo > step_m { lo + max_up } else { f64::INFINITY }
            };
            if !split {
                let base = p_out.len() as i64;
                for (k, q) in tri.iter().enumerate() {
                    p_out.push([q[0], q[1], z[k].min(cap(q[0], q[1]))]);
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
                    p_out.push([q[0], q[1], ter(q[0], q[1]).min(cap(q[0], q[1]))]);
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

/// grading._add_cuts, its probes: per vertex of a road's centreline xy (n, 2), points on its left, then its right, at
/// the offsets o from the carriageway's edge (road_half) out past the core (half): road_half + every step m short of
/// the core's edge, then half + 0, step, .. reach. Returns (points (2 * n * m, 2) in side, vertex, offset order, o (m,)).
#[pyfunction]
fn sharp_probes<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, road_half: f64, half: f64, reach: f64, step: f64)
                     -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<f64>>) {
    let p = xy.as_array();
    let n = p.nrows();
    let count = ((reach + 1e-9) / step).ceil().max(0.0) as usize;       // numpy.arange(0, reach + 1e-9, step)
    let offs: Vec<f64> = (0..count).map(|i| i as f64 * step).collect();
    let mut o: Vec<f64> = offs.iter().filter(|&&v| v < half - road_half).map(|&v| road_half + v).collect();
    o.extend(offs.iter().map(|&v| (road_half + v) + (half - road_half)));
    let m = o.len();
    let mut out = Vec::with_capacity(2 * n * m * 2);
    // the unit tangents (numpy.gradient: central differences inside, one-sided at the ends) and left normals
    let mut nl = Vec::with_capacity(n);
    for i in 0..n {
        let (a, b, d) = if n < 2 { (0, 0, 1.0) } else if i == 0 { (1, 0, 1.0) } else if i == n - 1 { (n - 1, n - 2, 1.0) }
                        else { (i + 1, i - 1, 2.0) };
        let (tx, ty) = ((p[[a, 0]] - p[[b, 0]]) / d, (p[[a, 1]] - p[[b, 1]]) / d);
        let l = tx.hypot(ty).max(1e-9);
        nl.push((-(ty / l), tx / l));
    }
    for side in [1.0f64, -1.0] {
        for i in 0..n {
            let (nx, ny) = (nl[i].0 * side, nl[i].1 * side);
            for &ov in &o {
                out.push(p[[i, 0]] + nx * ov);
                out.push(p[[i, 1]] + ny * ov);
            }
        }
    }
    let a = numpy::ndarray::Array2::from_shape_vec((2 * n * m, 2), out).unwrap();
    (a.into_pyarray(py), o.into_pyarray(py))
}

/// grading._add_cuts, the walls: from the raw ground g at sharp_probes' points (2 * n * m,) and the road's heights z
/// (n,) at its vertices, per segment and side (left, then right) a cutting's (wall offset, top height, top offset), NaN
/// where none (a drop: its wall only), as Plan.cut (n - 1, 6); None when there is no cutting at all. A climb counts
/// where the ground rises min_m or more at grade or steeper on average (from a quarter to 90 % of it), over runs of
/// run_n vertices at least; its wall is where the climb is half done (never nearer than road_half + 0.2), its top
/// where it is 90 % done. The same reading of a fall: the road's core ends there, nothing raised, and never before its
/// full half width (its sidewalk on the level: it hung down the drop, Munich's McGraw-Graben).
#[pyfunction]
fn sharp_cuts<'py>(py: Python<'py>, g: PyReadonlyArray1<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                   o: PyReadonlyArray1<'py, f64>, road_half: f64, half: f64, min_m: f64, grade: f64, step: f64,
                   run_n: usize)
                   -> Option<Bound<'py, PyArray2<f64>>> {
    let (g, z, o) = (g.as_array(), z.as_array(), o.as_array());
    let n = z.len();
    let m = o.len();
    if n < 2 || m == 0 || g.len() != 2 * n * m {
        return None;
    }
    let first = |r: &[f64], v: f64| r.iter().position(|&x| x >= v).unwrap_or(0);    // numpy.argmax of r >= v
    // (ok, wall offset, top rise, top offset) of a climb r (m,); its wall no nearer than near
    let wall_of = |r: &[f64], near: f64| -> (bool, f64, f64, f64) {
        let top = r.iter().fold(f64::NEG_INFINITY, |a, &b| if a.is_nan() || b.is_nan() { f64::NAN } else { a.max(b) });
        let ti = first(r, 0.9 * top);
        let half_i = first(r, top / 2.0);
        let foot_i = first(r, 0.25 * top);
        let steep = (top * 0.75) / (o[ti] - o[foot_i]).max(step);
        (top >= min_m && steep >= grade, o[half_i].max(near), top, o[ti])
    };
    let nan = f64::NAN;
    let mut cols: Vec<Vec<f64>> = Vec::with_capacity(6);
    for side in 0..2 {
        let (mut ok, mut wall, mut ztop, mut reach) = (vec![false; n], vec![0.0; n], vec![nan; n], vec![0.0; n]);
        let mut rise = vec![0.0f64; m];
        let mut fall = vec![0.0f64; m];
        for i in 0..n {
            let base = (side * n + i) * m;
            for k in 0..m {
                rise[k] = g[base + k] - z[i];
                fall[k] = -rise[k];
            }
            let (ok_up, wall_up, top, reach_o) = wall_of(&rise, road_half + 0.2);
            let (ok_dn, wall_dn, _, _) = wall_of(&fall, half);
            ok[i] = ok_up || ok_dn;
            wall[i] = if ok_up { wall_up } else { wall_dn };
            if ok_up {
                ztop[i] = z[i] + top;
            }
            reach[i] = reach_o;
        }
        // runs of run_n vertices at least (a single steep sample is noise, not a wall)
        let mut run = vec![false; n];
        let mut i = 0;
        while i < n {
            if !ok[i] {
                i += 1;
                continue;
            }
            let mut e = i;
            while e + 1 < n && ok[e + 1] {
                e += 1;
            }
            if e + 1 - i >= run_n {
                for q in i..=e {
                    run[q] = true;
                }
            }
            i = e + 1;
        }
        let (mut cw, mut ct, mut cr) = (vec![nan; n - 1], vec![nan; n - 1], vec![nan; n - 1]);
        for s in 0..n - 1 {
            if !(run[s] && run[s + 1]) {
                continue;
            }
            cw[s] = wall[s].max(wall[s + 1]);
            if ztop[s].is_finite() && ztop[s + 1].is_finite() {
                ct[s] = ztop[s].min(ztop[s + 1]);
                cr[s] = reach[s].max(reach[s + 1]);
            }
        }
        cols.push(cw);
        cols.push(ct);
        cols.push(cr);
    }
    if !cols[0].iter().chain(cols[3].iter()).any(|v| v.is_finite()) {
        return None;
    }
    let mut flat = Vec::with_capacity((n - 1) * 6);
    for s in 0..n - 1 {
        for c in &cols {
            flat.push(c[s]);
        }
    }
    Some(numpy::ndarray::Array2::from_shape_vec((n - 1, 6), flat).unwrap().into_pyarray(py))
}


/// The points of a polyline xy (k, 2) at a tight bend: its direction half_span m back along it and half_span m on differ by
/// min_turn_deg or more (a hairpin's apex and its approaches). Retaining walls and sharp cuttings stop there (the user,
/// the Stelvio Pass 2026-10-21: the wall between a hairpin's legs ran on into the turn, full height to its end).
#[pyfunction]
fn tight_bends<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, half_span: f64, min_turn_deg: f64)
                    -> Bound<'py, PyArray1<bool>> {
    let p = xy.as_array();
    let k = p.nrows();
    let mut out = vec![false; k];
    if k < 3 {
        return out.into_pyarray(py);
    }
    let mut s = vec![0.0f64; k];
    for i in 1..k {
        s[i] = s[i - 1] + (p[[i, 0]] - p[[i - 1, 0]]).hypot(p[[i, 1]] - p[[i - 1, 1]]);
    }
    let dir = |d: f64| -> (f64, f64) {         // the unit direction of the segment at arc d (clamped)
        let d = d.clamp(0.0, s[k - 1]);
        let mut j = s.partition_point(|&v| v < d).clamp(1, k - 1);
        while j > 1 && s[j] - s[j - 1] < 1e-9 {
            j -= 1;
        }
        let (dx, dy) = (p[[j, 0]] - p[[j - 1, 0]], p[[j, 1]] - p[[j - 1, 1]]);
        let l = dx.hypot(dy).max(1e-12);
        (dx / l, dy / l)
    };
    let cos_min = min_turn_deg.to_radians().cos();
    for i in 0..k {
        let (ux, uy) = dir(s[i] - half_span);
        let (vx, vy) = dir(s[i] + half_span);
        out[i] = ux * vx + uy * vy <= cos_min;
    }
    out.into_pyarray(py)
}


/// vegetation.regional: the groups of a region from the generic ones (group, height m, lat, lon, ESA WorldCover class,
/// a hashed [0, 1) per item) by a coarse climate. ids: the group ids (deciduous, conifer, shrub, rock, pine_west, palm,
/// juniper, desert_shrub, desert_rock, oak_west, tropical); wc: WorldCover's (shrub, bare, grass, built) classes.
/// Palms are most town trees only where they are in life: South Florida, the Rio Grande Valley, coastal Southern
/// California, the tropics; a few along the rest of the Gulf Coast (GitHub #110: Houston and Austin were mostly palms,
/// in life live oaks and loblolly pines). The Southeast's conifers are pines.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn regional_groups<'py>(py: Python<'py>, group: PyReadonlyArray1<'py, u8>, height: PyReadonlyArray1<'py, f64>,
                        lat: PyReadonlyArray1<'py, f64>, lon: PyReadonlyArray1<'py, f64>,
                        lcls: PyReadonlyArray1<'py, u8>, u: PyReadonlyArray1<'py, f64>, ids: [u8; 11], wc: [u8; 4])
                        -> Bound<'py, PyArray1<u8>> {
    let (group, height, lat, lon, lcls, u) = (group.as_array(), height.as_array(), lat.as_array(), lon.as_array(),
                                               lcls.as_array(), u.as_array());
    let [g_dec, g_con, g_shrub, g_rock, g_pine_w, g_palm, g_juniper, g_dshrub, g_drock, g_oak_w, g_trop] = ids;
    let [wc_shrub, wc_bare, wc_grass, wc_built] = wc;
    let n = group.len();
    let mut out = vec![0u8; n];
    for i in 0..n {
        let (mut g, h, la, lo, c, ui) = (group[i], height[i], lat[i], lon[i], lcls[i], u[i]);
        let west_coast = lo < -114.0 && la > 32.0 && la < 49.5;
        let dry_west = lo < -102.0 && la > 25.0 && la < 46.0 && !(lo < -121.0 && la > 37.0);
        let open_dry = dry_west && (c == wc_shrub || c == wc_bare || c == wc_grass);
        let tropics = la.abs() < 23.5;
        let built = c == wc_built;
        let north_america = la > 24.0 && lo > -170.0 && lo < -50.0;
        // where palms are most town trees: South Florida, the Rio Grande Valley, coastal Southern California
        let palm_land = (la < 29.0 && lo > -82.8 && lo < -79.5) || (la < 26.9 && lo > -99.8 && lo < -96.8)
            || (la < 34.6 && lo < -114.0 && la > 20.0);
        let gulf = !palm_land && la < 30.8 && lo > -98.0 && lo < -80.0;
        let southeast = la < 35.5 && lo > -98.5 && lo < -75.0;
        // towns of the dry West are irrigated broadleaves far more than conifers (Moab's photos)
        if dry_west && built && g == g_con && ui < 0.8 {
            g = g_dec;
        }
        let tree = g == g_dec || g == g_con;
        if (west_coast || southeast) && g == g_con {       // the west's pines; the Southeast's loblolly / slash pines
            g = g_pine_w;
        }
        if west_coast && g == g_dec && h < 11.0 {
            g = g_oak_w;
        }
        if open_dry && tree && h < 9.0 {                    // dry open country: junipers
            g = g_juniper;
        }
        if open_dry && g == g_con {
            g = g_pine_w;
        }
        let arid = !north_america && (c == wc_shrub || c == wc_bare) && la.abs() < 42.0;
        if (dry_west || arid) && g == g_shrub {
            g = g_dshrub;
        }
        if (dry_west || arid) && g == g_rock {
            g = g_drock;
        }
        let palm_p = if palm_land || tropics { if built { 0.6 } else { 0.2 } }
                     else if gulf { if built { 0.08 } else { 0.02 } } else { 0.0 };
        if tree && ui < palm_p {
            g = g_palm;
        }
        if tropics && g == g_dec && h >= 12.0 {
            g = g_trop;
        }
        out[i] = g;
    }
    out.into_pyarray(py)
}


/// Each segment of a polyline xy (k, 2) clipped to the box (x0, y0, x1, y1) (Liang-Barsky): (k - 1, 2) the parameters
/// (t0, t1) along it of the part inside, NaN for a segment wholly outside. grading: a long road's plan trimmed to its
/// cell's own box, so every stretch of road is planned once (GitHub #108 / the Stelvio Pass: overlapping copies from
/// neighbouring cells counted a road several times, the ground graded differently by every batch).
#[pyfunction]
fn clip_segments<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, bx: (f64, f64, f64, f64))
                      -> Bound<'py, PyArray2<f64>> {
    let p = xy.as_array();
    let k = p.nrows();
    let n = k.saturating_sub(1);
    let (x0, y0, x1, y1) = bx;
    let mut out = vec![f64::NAN; n * 2];
    for i in 0..n {
        let (ax, ay, bx_, by) = (p[[i, 0]], p[[i, 1]], p[[i + 1, 0]], p[[i + 1, 1]]);
        let (dx, dy) = (bx_ - ax, by - ay);
        let (mut t0, mut t1) = (0.0f64, 1.0f64);
        let mut inside = true;
        for (pp, qq) in [(-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)] {
            if pp.abs() < 1e-15 {
                if qq < 0.0 {
                    inside = false;
                    break;
                }
            } else {
                let r = qq / pp;
                if pp < 0.0 {
                    if r > t1 { inside = false; break; }
                    if r > t0 { t0 = r; }
                } else {
                    if r < t0 { inside = false; break; }
                    if r < t1 { t1 = r; }
                }
            }
        }
        if inside && t1 > t0 {
            out[2 * i] = t0;
            out[2 * i + 1] = t1;
        }
    }
    numpy::ndarray::Array2::from_shape_vec((n, 2), out).unwrap().into_pyarray(py)
}


/// roads.TunnelProfiles._road_caps' search: the first of the probe points (m, 2) (in order) that lies within
/// radius[l] + gap of a road line l (vertices pts[starts[l]..starts[l + 1]]), lines tested in order, each by its nearest
/// segment; lines of fewer than 2 points or with an end within join_m of p are left out. (probe index, the nearest
/// point on that line) or None. The same float64 sums as the numpy loop it replaced (2 s of a Munich block).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn first_probe_near_lines(pts: PyReadonlyArray2<f64>, starts: PyReadonlyArray1<i64>, radius: PyReadonlyArray1<f64>,
                          gap: f64, p: (f64, f64), join_m: f64, probes: PyReadonlyArray2<f64>)
                          -> Option<(usize, f64, f64)> {
    let (pv, st, rv, qv) = (pts.as_array(), starts.as_array(), radius.as_array(), probes.as_array());
    let nl = rv.len();
    let m = qv.nrows();
    if m == 0 {
        return None;
    }
    let (mut qx0, mut qy0, mut qx1, mut qy1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for i in 0..m {
        qx0 = qx0.min(qv[[i, 0]]); qx1 = qx1.max(qv[[i, 0]]);
        qy0 = qy0.min(qv[[i, 1]]); qy1 = qy1.max(qv[[i, 1]]);
    }
    // the lines that count: not joined at p, their box near the probes
    let mut lines: Vec<usize> = Vec::new();
    for l in 0..nl {
        let (a, b) = (st[l] as usize, st[l + 1] as usize);
        if b < a + 2 {
            continue;
        }
        let d0 = (pv[[a, 0]] - p.0).hypot(pv[[a, 1]] - p.1);
        let d1 = (pv[[b - 1, 0]] - p.0).hypot(pv[[b - 1, 1]] - p.1);
        if d0.min(d1) < join_m {
            continue;
        }
        let r = rv[l] + gap;
        let (mut lx, mut ly, mut hx, mut hy) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for k in a..b {
            lx = lx.min(pv[[k, 0]]); hx = hx.max(pv[[k, 0]]);
            ly = ly.min(pv[[k, 1]]); hy = hy.max(pv[[k, 1]]);
        }
        if hx + r < qx0 || lx - r > qx1 || hy + r < qy0 || ly - r > qy1 {
            continue;
        }
        lines.push(l);
    }
    for i in 0..m {
        let (qx, qy) = (qv[[i, 0]], qv[[i, 1]]);
        for &l in &lines {
            let (a, b) = (st[l] as usize, st[l + 1] as usize);
            let (mut best, mut bx, mut by) = (f64::INFINITY, 0.0, 0.0);
            for k in a..b - 1 {
                let (ax, ay) = (pv[[k, 0]], pv[[k, 1]]);
                let (dx, dy) = (pv[[k + 1, 0]] - ax, pv[[k + 1, 1]] - ay);
                let t = (((qx - ax) * dx + (qy - ay) * dy) / (dx * dx + dy * dy).max(1e-9)).clamp(0.0, 1.0);
                let (cx, cy) = (ax + t * dx, ay + t * dy);
                let d = (cx - qx).hypot(cy - qy);
                if d < best {
                    best = d;
                    bx = cx;
                    by = cy;
                }
            }
            if best < rv[l] + gap {
                return Some((i, bx, by));
            }
        }
    }
    None
}

/// terrainpatch.mesh: a height grid z (ny, nx) over xs (nx,) and ys (ny,) as a mesh relative to (ox, oy, 0):
/// positions (nx*ny, 3) float32 row-major (y rows, x columns), triangles (2*(nx-1)*(ny-1), 3) int32, two a quad
/// (a b d, a d c: counter-clockwise from above), the first of every quad, then the second.
#[pyfunction]
fn grid_mesh<'py>(py: Python<'py>, xs: PyReadonlyArray1<'py, f64>, ys: PyReadonlyArray1<'py, f64>,
                  z: PyReadonlyArray2<'py, f64>, ox: f64, oy: f64)
                  -> PyResult<(Bound<'py, PyArray2<f32>>, Bound<'py, PyArray2<i32>>)> {
    let (xv, yv, zv) = (xs.as_array(), ys.as_array(), z.as_array());
    let (nx, ny) = (xv.len(), yv.len());
    if zv.shape() != [ny, nx] {
        return Err(pyo3::exceptions::PyValueError::new_err("grid_mesh: z must be (len(ys), len(xs))"));
    }
    let mut pos = numpy::ndarray::Array2::<f32>::zeros((nx * ny, 3));
    for j in 0..ny {
        for i in 0..nx {
            let k = j * nx + i;
            pos[[k, 0]] = (xv[i] - ox) as f32;
            pos[[k, 1]] = (yv[j] - oy) as f32;
            pos[[k, 2]] = zv[[j, i]] as f32;
        }
    }
    let q = nx.saturating_sub(1) * ny.saturating_sub(1);
    let mut tris = numpy::ndarray::Array2::<i32>::zeros((2 * q, 3));
    let mut t = 0;
    for half in 0..2 {
        for j in 0..ny.saturating_sub(1) {
            for i in 0..nx.saturating_sub(1) {
                let a = (j * nx + i) as i32;
                let (b, c, d) = (a + 1, a + nx as i32, a + nx as i32 + 1);
                let row = if half == 0 { [a, b, d] } else { [a, d, c] };
                for m in 0..3 { tris[[t, m]] = row[m]; }
                t += 1;
            }
        }
    }
    Ok((pos.into_pyarray(py), tris.into_pyarray(py)))
}

/// Which points lie on any of the triangles (2D): tris (n, 6) as x0 y0 x1 y1 x2 y2, pts (m, 2) -> bool (m,).
/// The triangles are bucketed by their boxes in square cells of `cell` m; a point on an edge counts as on it.
/// (plazas.py: a paved area's ground the road mesh already covers is left out.)
#[pyfunction]
fn points_on_tris<'py>(py: Python<'py>, tris: PyReadonlyArray2<'py, f64>, pts: PyReadonlyArray2<'py, f64>, cell: f64)
                       -> PyResult<Bound<'py, PyArray1<bool>>> {
    let t = tris.as_array();
    let p = pts.as_array();
    if t.ncols() != 6 || p.ncols() != 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("points_on_tris: tris (n, 6), pts (m, 2)"));
    }
    let (n, m) = (t.nrows(), p.nrows());
    let mut out = vec![false; m];
    if n == 0 || m == 0 || !(cell > 0.0) {
        return Ok(out.into_pyarray(py));
    }
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for i in 0..n {
        for k in 0..3 {
            x0 = x0.min(t[[i, 2 * k]]); x1 = x1.max(t[[i, 2 * k]]);
            y0 = y0.min(t[[i, 2 * k + 1]]); y1 = y1.max(t[[i, 2 * k + 1]]);
        }
    }
    let nx = (((x1 - x0) / cell).floor() as usize + 1).min(4096);
    let ny = (((y1 - y0) / cell).floor() as usize + 1).min(4096);
    let ix = |x: f64| (((x - x0) / cell).floor().max(0.0) as usize).min(nx - 1);
    let iy = |y: f64| (((y - y0) / cell).floor().max(0.0) as usize).min(ny - 1);
    let mut cells: Vec<Vec<u32>> = vec![Vec::new(); nx * ny];
    for i in 0..n {
        let xs = [t[[i, 0]], t[[i, 2]], t[[i, 4]]];
        let ys = [t[[i, 1]], t[[i, 3]], t[[i, 5]]];
        let (a, b) = (ix(xs[0].min(xs[1]).min(xs[2])), ix(xs[0].max(xs[1]).max(xs[2])));
        let (c, d) = (iy(ys[0].min(ys[1]).min(ys[2])), iy(ys[0].max(ys[1]).max(ys[2])));
        for j in c..=d {
            for k in a..=b {
                cells[j * nx + k].push(i as u32);
            }
        }
    }
    for q in 0..m {
        let (x, y) = (p[[q, 0]], p[[q, 1]]);
        if x < x0 || x > x1 || y < y0 || y > y1 {
            continue;
        }
        for &i in &cells[iy(y) * nx + ix(x)] {
            let i = i as usize;
            let (ax, ay, bx, by, cx, cy) = (t[[i, 0]], t[[i, 1]], t[[i, 2]], t[[i, 3]], t[[i, 4]], t[[i, 5]]);
            let d1 = (bx - ax) * (y - ay) - (by - ay) * (x - ax);
            let d2 = (cx - bx) * (y - by) - (cy - by) * (x - bx);
            let d3 = (ax - cx) * (y - cy) - (ay - cy) * (x - cx);
            let neg = d1 < 0.0 || d2 < 0.0 || d3 < 0.0;
            let pos = d1 > 0.0 || d2 > 0.0 || d3 > 0.0;
            if !(neg && pos) {
                out[q] = true;
                break;
            }
        }
    }
    Ok(out.into_pyarray(py))
}

/// The longest run (m) of wet points along the line xy (n, 2) that touches neither end, measured between the dry
/// points either side of it (roads.wet_run_inside: a tunnel under a river, not a portal on its bank).
#[pyfunction]
fn wet_run_inside(xy: PyReadonlyArray2<f64>, wet: PyReadonlyArray1<bool>) -> PyResult<f64> {
    let p = xy.as_array();
    let w = wet.as_array();
    let n = p.nrows();
    if w.len() != n || p.ncols() != 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("wet_run_inside: xy (n, 2), wet (n,)"));
    }
    if n < 3 {
        return Ok(0.0);
    }
    let mut s = vec![0.0f64; n];
    for i in 1..n {
        s[i] = s[i - 1] + ((p[[i, 0]] - p[[i - 1, 0]]).powi(2) + (p[[i, 1]] - p[[i - 1, 1]]).powi(2)).sqrt();
    }
    let (mut best, mut i) = (0.0f64, 0usize);
    while i < n {
        if !w[i] {
            i += 1;
            continue;
        }
        let mut j = i;
        while j + 1 < n && w[j + 1] {
            j += 1;
        }
        if i > 0 && j < n - 1 {
            best = best.max(s[j + 1] - s[i - 1]);
        }
        i = j + 1;
    }
    Ok(best)
}

/// surface.photo_roads: the photo rgb (h, w, 3) with thick lines painted in. segs (m, 4): segment ends in pixel
/// coordinates (column, row; a pixel's centre at +0.5); hw (m,): half widths in pixels; kind (m,): index into colors
/// (k, 4: r g b, opacity). Each kind's coverage is the largest over its segments of clamp(hw - distance + 0.5, 0, 1)
/// (round ends and joints, a one-pixel antialiased edge); the kinds are blended over the photo in index order at their
/// opacity times their coverage (the last kind wins where they overlap).
#[pyfunction]
fn paint_lines<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, segs: PyReadonlyArray2<'py, f64>,
                    hw: PyReadonlyArray1<'py, f64>, kind: PyReadonlyArray1<'py, u8>, colors: PyReadonlyArray2<'py, f32>)
                    -> PyResult<Bound<'py, numpy::PyArray3<u8>>> {
    let img = rgb.as_array();
    let sg = segs.as_array();
    let hw = hw.as_array();
    let kd = kind.as_array();
    let col = colors.as_array();
    let m = sg.nrows();
    if sg.ncols() != 4 || hw.len() != m || kd.len() != m || col.ncols() != 4 || img.shape()[2] != 3 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "paint_lines: rgb (h, w, 3), segs (m, 4), hw (m,), kind (m,), colors (k, 4)"));
    }
    let k = col.nrows();
    if kd.iter().any(|&v| v as usize >= k) {
        return Err(pyo3::exceptions::PyValueError::new_err("paint_lines: a kind past the colours"));
    }
    let out = py.allow_threads(|| {
        let (h, w) = (img.shape()[0], img.shape()[1]);
        let mut cov = vec![0f32; k * h * w];
        for s in 0..m {
            let (ax, ay, bx, by, r) = (sg[[s, 0]], sg[[s, 1]], sg[[s, 2]], sg[[s, 3]], hw[s]);
            if !(r > 0.0) || !ax.is_finite() || !ay.is_finite() || !bx.is_finite() || !by.is_finite() {
                continue;
            }
            let pad = r + 1.0;
            let c0 = (ax.min(bx) - pad).floor().max(0.0);
            let c1 = (ax.max(bx) + pad).ceil().min(w as f64);
            let r0 = (ay.min(by) - pad).floor().max(0.0);
            let r1 = (ay.max(by) + pad).ceil().min(h as f64);
            if c0 >= c1 || r0 >= r1 {
                continue;
            }
            let (dx, dy) = (bx - ax, by - ay);
            let l2 = dx * dx + dy * dy;
            let base = kd[s] as usize * h * w;
            for j in r0 as usize..r1 as usize {
                let py_ = j as f64 + 0.5;
                for i in c0 as usize..c1 as usize {
                    let px_ = i as f64 + 0.5;
                    let t = if l2 > 0.0 { (((px_ - ax) * dx + (py_ - ay) * dy) / l2).clamp(0.0, 1.0) } else { 0.0 };
                    let (ex, ey) = (px_ - ax - t * dx, py_ - ay - t * dy);
                    let c = (r - (ex * ex + ey * ey).sqrt() + 0.5).clamp(0.0, 1.0) as f32;
                    let v = &mut cov[base + j * w + i];
                    if c > *v { *v = c; }
                }
            }
        }
        let mut out = img.to_owned();
        for q in 0..k {
            let base = q * h * w;
            let c = [col[[q, 0]], col[[q, 1]], col[[q, 2]]];
            let alpha = col[[q, 3]];
            for j in 0..h {
                for i in 0..w {
                    let a = cov[base + j * w + i] * alpha;
                    if a <= 0.0 { continue; }
                    for ch in 0..3 {
                        let v = out[[j, i, ch]] as f32 * (1.0 - a) + c[ch] * a;
                        out[[j, i, ch]] = (v + 0.5).clamp(0.0, 255.0) as u8;
                    }
                }
            }
        }
        out
    });
    Ok(out.into_pyarray(py))
}

/// farfield.build_tile's adaptive mesh: the right-triangulated irregular network (RTIN, a 4-8 hierarchy, as Mapbox's
/// Martini, but with each triangle's true error: every grid point inside it) of a (2^k + 1)² height grid h [j, i]
/// (j north) whose surface is within max_err (m) of every grid height.
/// Returns (tris (m, 3) int64: grid indices j * n + i, counter-clockwise from +Z; surf (n, n): the mesh's surface at
/// every grid point; the largest |surf - h|). The top split is the SW-NE diagonal and the finest cells split SW-NE
/// where i + j is even, SE-NW where odd (the TerrainBlock's pattern, farfield.torque_split45), so surf on the regular
/// grid triangulation is the mesh's surface exactly. The mesh has no T-junctions (a midpoint's error is at least its
/// children's).
#[pyfunction]
fn rtin_mesh<'py>(py: Python<'py>, h: PyReadonlyArray2<'py, f64>, max_err: f64)
                  -> PyResult<(Bound<'py, PyArray2<i64>>, Bound<'py, PyArray2<f64>>, f64)> {
    let g = h.as_array();
    let n = g.nrows();
    let t = n.wrapping_sub(1);
    if g.ncols() != n || n < 3 || !t.is_power_of_two() {
        return Err(pyo3::exceptions::PyValueError::new_err("rtin_mesh: a (2^k + 1)² grid"));
    }
    let hv: Vec<f64> = g.iter().copied().collect();
    let (tris, surf, worst) = py.allow_threads(|| {
        let at = |x: usize, y: usize| hv[y * n + x];
        let num = t * t * 2 - 2;
        let parents = num - t * t;
        let mut coords = vec![0usize; num * 4];
        for i in 0..num {
            let mut id = i + 2;
            let (mut ax, mut ay, mut bx, mut by, mut cx, mut cy);
            if id & 1 != 0 {
                ax = 0; ay = 0; bx = t; by = t; cx = t; cy = 0;
            } else {
                ax = t; ay = t; bx = 0; by = 0; cx = 0; cy = t;
            }
            id >>= 1;
            while id > 1 {
                let (mx, my) = ((ax + bx) >> 1, (ay + by) >> 1);
                if id & 1 != 0 {
                    bx = ax; by = ay; ax = cx; ay = cy;
                } else {
                    ax = bx; ay = by; bx = cx; by = cy;
                }
                cx = mx; cy = my;
                id >>= 1;
            }
            coords[i * 4..i * 4 + 4].copy_from_slice(&[ax, ay, bx, by]);
        }
        let mut errors = vec![0f64; n * n];
        for i in (0..num).rev() {
            let (ax, ay, bx, by) = (coords[i * 4], coords[i * 4 + 1], coords[i * 4 + 2], coords[i * 4 + 3]);
            let (mx, my) = ((ax + bx) >> 1, (ay + by) >> 1);
            let cx = (mx + my) as isize - ay as isize;
            let cy = (my + ax) as isize - mx as isize;
            let mid = my * n + mx;
            // the triangle's true error: every grid point inside it against its plane (the hypotenuse midpoint alone
            // let big flat triangles reach up a wall's step: spikes in front of the McGraw-Graben's walls, 2026-10-22)
            let (cxu, cyu) = (cx as usize, cy as usize);
            let (za, zb, zc) = (at(ax, ay), at(bx, by), at(cxu, cyu));
            let (fax, fay, fbx, fby, fcx, fcy) = (ax as f64, ay as f64, bx as f64, by as f64, cx as f64, cy as f64);
            let det = (fbx - fax) * (fcy - fay) - (fby - fay) * (fcx - fax);
            let mut e = 0f64;
            if det != 0.0 {
                let (x0, x1) = (ax.min(bx).min(cxu), ax.max(bx).max(cxu));
                let (y0, y1) = (ay.min(by).min(cyu), ay.max(by).max(cyu));
                for y in y0..=y1 {
                    for x in x0..=x1 {
                        let (fx, fy) = (x as f64, y as f64);
                        let w1 = ((fx - fax) * (fcy - fay) - (fy - fay) * (fcx - fax)) / det;
                        let w2 = ((fbx - fax) * (fy - fay) - (fby - fay) * (fx - fax)) / det;
                        let w0 = 1.0 - w1 - w2;
                        if w0 < -1e-9 || w1 < -1e-9 || w2 < -1e-9 { continue; }
                        let d = (w0 * za + w1 * zb + w2 * zc - hv[y * n + x]).abs();
                        if d > e { e = d; }
                    }
                }
            }
            if e > errors[mid] { errors[mid] = e; }
            if i < parents {
                let (cx, cy) = (cx as usize, cy as usize);
                let l = ((ay + cy) >> 1) * n + ((ax + cx) >> 1);
                let r = ((by + cy) >> 1) * n + ((bx + cx) >> 1);
                errors[mid] = errors[mid].max(errors[l]).max(errors[r]);
            }
        }
        let mut tris: Vec<[i64; 3]> = Vec::new();
        let mut surf = vec![f64::NAN; n * n];
        let mut stack = vec![(0usize, 0usize, t, t, t, 0usize), (t, t, 0, 0, 0, t)];
        while let Some((ax, ay, bx, by, cx, cy)) = stack.pop() {
            let (mx, my) = ((ax + bx) >> 1, (ay + by) >> 1);
            let leg = (ax as isize - cx as isize).abs() + (ay as isize - cy as isize).abs();
            if leg > 1 && errors[my * n + mx] > max_err {
                stack.push((cx, cy, ax, ay, mx, my));
                stack.push((bx, by, cx, cy, mx, my));
                continue;
            }
            let p = [(ax, ay), (bx, by), (cx, cy)];
            let z = [at(ax, ay), at(bx, by), at(cx, cy)];
            let cross = (bx as f64 - ax as f64) * (cy as f64 - ay as f64) - (by as f64 - ay as f64) * (cx as f64 - ax as f64);
            let idx = |k: usize| (p[k].1 * n + p[k].0) as i64;
            tris.push(if cross > 0.0 { [idx(0), idx(1), idx(2)] } else { [idx(0), idx(2), idx(1)] });
            // the plane over the grid points of this triangle (barycentric; edges shared, the same value both sides)
            let (x0, x1) = (ax.min(bx).min(cx), ax.max(bx).max(cx));
            let (y0, y1) = (ay.min(by).min(cy), ay.max(by).max(cy));
            let det = cross;
            for y in y0..=y1 {
                for x in x0..=x1 {
                    let (fx, fy) = (x as f64, y as f64);
                    let w1 = ((fx - ax as f64) * (cy as f64 - ay as f64) - (fy - ay as f64) * (cx as f64 - ax as f64)) / det;
                    let w2 = ((bx as f64 - ax as f64) * (fy - ay as f64) - (by as f64 - ay as f64) * (fx - ax as f64)) / det;
                    let w0 = 1.0 - w1 - w2;
                    if w0 < -1e-9 || w1 < -1e-9 || w2 < -1e-9 { continue; }
                    surf[y * n + x] = w0 * z[0] + w1 * z[1] + w2 * z[2];
                }
            }
        }
        let mut worst = 0f64;
        for k in 0..n * n {
            worst = worst.max((surf[k] - hv[k]).abs());
        }
        (tris, surf, worst)
    });
    let m = tris.len();
    let tris = numpy::ndarray::Array2::from_shape_vec((m, 3), tris.into_iter().flatten().collect()).unwrap();
    let surf = numpy::ndarray::Array2::from_shape_vec((n, n), surf).unwrap();
    Ok((tris.into_pyarray(py), surf.into_pyarray(py), worst))
}

#[pymodule]
fn earthfast(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(island_ring_rays, m)?)?;
    m.add_function(wrap_pyfunction!(dsm::dsm_ground, m)?)?;
    m.add_function(wrap_pyfunction!(dsm::fill_holes, m)?)?;
    m.add_function(wrap_pyfunction!(roadgeom::dedupe_polyline, m)?)?;
    m.add_function(wrap_pyfunction!(roadgeom::curve_deck, m)?)?;
    m.add_function(wrap_pyfunction!(underpass::underpass_offsets, m)?)?;
    m.add_function(wrap_pyfunction!(gradesep::gradesep_candidates, m)?)?;
    m.add_function(wrap_pyfunction!(gradesep::gradesep_spans, m)?)?;
    m.add_function(wrap_pyfunction!(shore::shore_steps, m)?)?;
    m.add_function(wrap_pyfunction!(shore::carve_segments, m)?)?;
    m.add_function(wrap_pyfunction!(vegmask::mask_polys, m)?)?;
    m.add_function(wrap_pyfunction!(ambience::audio_nodes, m)?)?;
    m.add_function(wrap_pyfunction!(ambience::audio_biome, m)?)?;
    m.add_function(wrap_pyfunction!(ambience::sound_boxes, m)?)?;
    m.add_function(wrap_pyfunction!(ambience::shore_lines, m)?)?;
    m.add_function(wrap_pyfunction!(power::power_line_layout, m)?)?;
    m.add_function(wrap_pyfunction!(lamps::lamp_stations, m)?)?;
    m.add_function(wrap_pyfunction!(power::substation_yard, m)?)?;
    m.add_function(wrap_pyfunction!(vegmask::road_segments, m)?)?;
    m.add_function(wrap_pyfunction!(vegmask::disc_max, m)?)?;
    m.add_function(wrap_pyfunction!(vegmask::thin_tops, m)?)?;
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
    m.add_function(wrap_pyfunction!(bank_profile, m)?)?;
    m.add_function(wrap_pyfunction!(resample, m)?)?;
    m.add_function(wrap_pyfunction!(tunnel_sections, m)?)?;
    m.add_function(wrap_pyfunction!(smooth_profile, m)?)?;
    m.add_function(wrap_pyfunction!(chain_extensions, m)?)?;
    m.add_function(wrap_pyfunction!(route_samples, m)?)?;
    m.add_function(wrap_pyfunction!(node_parts, m)?)?;
    m.add_function(wrap_pyfunction!(median_profile, m)?)?;
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
    m.add_function(wrap_pyfunction!(bilinear_grid, m)?)?;
    m.add_function(wrap_pyfunction!(bilinear_tiles, m)?)?;
    m.add_function(wrap_pyfunction!(segment_crossings_g, m)?)?;
    m.add_function(wrap_pyfunction!(triangulate, m)?)?;
    m.add_function(wrap_pyfunction!(clean_ring_core, m)?)?;
    m.add_function(wrap_pyfunction!(py_hypot_test, m)?)?;
    m.add_function(wrap_pyfunction!(oriented_box_core, m)?)?;
    m.add_function(wrap_pyfunction!(hash01_test, m)?)?;
    m.add_function(wrap_pyfunction!(building_walls, m)?)?;
    m.add_function(wrap_pyfunction!(float_predictor_f32, m)?)?;
    m.add_function(wrap_pyfunction!(segment_crossings, m)?)?;
    m.add_function(wrap_pyfunction!(seg_hits, m)?)?;
    m.add_function(wrap_pyfunction!(under_decks, m)?)?;
    m.add_function(wrap_pyfunction!(road_through, m)?)?;
    m.add_function(wrap_pyfunction!(face_normal_sum, m)?)?;
    m.add_function(wrap_pyfunction!(first_use, m)?)?;
    m.add_function(wrap_pyfunction!(near_crossers, m)?)?;
    m.add_function(wrap_pyfunction!(clip_polyline, m)?)?;
    m.add_function(wrap_pyfunction!(ends_near, m)?)?;
    m.add_function(wrap_pyfunction!(gpu_name, m)?)?;
    m.add_function(wrap_pyfunction!(seam_detect_gpu, m)?)?;
    m.add_function(wrap_pyfunction!(bc1_encode_gpu, m)?)?;
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
    m.add_function(wrap_pyfunction!(sharp_probes, m)?)?;
    m.add_function(wrap_pyfunction!(sharp_cuts, m)?)?;
    m.add_function(wrap_pyfunction!(tight_bends, m)?)?;
    m.add_function(wrap_pyfunction!(regional_groups, m)?)?;
    m.add_function(wrap_pyfunction!(clip_segments, m)?)?;
    m.add_function(wrap_pyfunction!(first_probe_near_lines, m)?)?;
    m.add_function(wrap_pyfunction!(grid_mesh, m)?)?;
    m.add_function(wrap_pyfunction!(points_on_tris, m)?)?;
    m.add_function(wrap_pyfunction!(wet_run_inside, m)?)?;
    m.add_function(wrap_pyfunction!(paint_lines, m)?)?;
    m.add_function(wrap_pyfunction!(photofill::fill_photo, m)?)?;
    m.add_function(wrap_pyfunction!(rtin_mesh, m)?)?;
    m.add("VERSION", 87)?;
    Ok(())
}
