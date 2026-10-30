//! Real road heights (server/roadz.py; docs/research/2026-10-28-3d-road-data.md): the kernels behind the height-source
//! override of roads, bridge decks and tunnel floors.
//!
//! - `las_decode`: the points of a LAS / LAZ file (USGS 3DEP's EPT nodes are LAZ 1.4, point format 6), xyz + class;
//! - `roadz_match`: heights along a map road from a source's 3D road lines (IGN BD TOPO, NVDB, lidar decks) matched
//!   by geometry (distance, direction, the caller's per-segment level / name test);
//! - `profile_clean`: a matched or measured profile with its spikes and impossible grades taken out, short gaps
//!   bridged, smoothed;
//! - `deck_lidar`: one drivable deck profile from the point cloud along a bridge's line: the heights at each station
//!   clustered into surfaces, the deck's surface chosen by a Viterbi pass (grades, a "nothing seen" state);
//! - `deck_blend`: a profile eased onto given end heights (the graded road it meets) within a grade-change budget.
//! All pure functions of their inputs (deterministic: no hashing, no threads that change results).

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use std::io::{Cursor, Seek, SeekFrom};

fn rd_u16(b: &[u8], o: usize) -> u16 { u16::from_le_bytes([b[o], b[o + 1]]) }
fn rd_u32(b: &[u8], o: usize) -> u32 { u32::from_le_bytes([b[o], b[o + 1], b[o + 2], b[o + 3]]) }
fn rd_i32(b: &[u8], o: usize) -> i32 { i32::from_le_bytes([b[o], b[o + 1], b[o + 2], b[o + 3]]) }
fn rd_u64(b: &[u8], o: usize) -> u64 {
    let mut a = [0u8; 8];
    a.copy_from_slice(&b[o..o + 8]);
    u64::from_le_bytes(a)
}
fn rd_f64(b: &[u8], o: usize) -> f64 {
    let mut a = [0u8; 8];
    a.copy_from_slice(&b[o..o + 8]);
    f64::from_le_bytes(a)
}

/// (xyz, class) of a LAS 1.2-1.4 file in `data`, plain or LAZ-compressed (laz-rs), the points with x0 <= x <= x1 and
/// y0 <= y <= y1 (the file's own coordinates) only. Errors on anything that is not a LAS file.
pub fn decode_las(data: &[u8], bbox: [f64; 4]) -> Result<(Vec<[f64; 3]>, Vec<u8>), String> {
    if data.len() < 227 || &data[0..4] != b"LASF" {
        return Err("not a LAS file".into());
    }
    let minor = data[25];
    let header_size = rd_u16(data, 94) as usize;
    let off_points = rd_u32(data, 96) as usize;
    let n_vlr = rd_u32(data, 100) as usize;
    let fmt_raw = data[104];
    let rec_len = rd_u16(data, 105) as usize;
    let mut n = rd_u32(data, 107) as u64;
    if minor >= 4 && data.len() >= 255 && n == 0 {
        n = rd_u64(data, 247);
    }
    let (sx, sy, sz) = (rd_f64(data, 131), rd_f64(data, 139), rd_f64(data, 147));
    let (ox, oy, oz) = (rd_f64(data, 155), rd_f64(data, 163), rd_f64(data, 171));
    let fmt = fmt_raw & 0x3f;
    let compressed = fmt_raw & 0x80 != 0 || fmt_raw & 0x40 != 0;
    if rec_len < 20 || off_points > data.len() || n > (1u64 << 31) {
        return Err("bad LAS header".into());
    }
    let class_at = if fmt >= 6 { 16 } else { 15 };
    let class_mask = if fmt >= 6 { 0xff } else { 0x1f };
    // the laszip VLR ("laszip encoded", 22204)
    let mut laz_vlr = None;
    let mut o = header_size;
    for _ in 0..n_vlr {
        if o + 54 > data.len() {
            break;
        }
        let user = &data[o + 2..o + 18];
        let rid = rd_u16(data, o + 18);
        let len = rd_u16(data, o + 20) as usize;
        if user.starts_with(b"laszip encoded") && rid == 22204 && o + 54 + len <= data.len() {
            laz_vlr = Some((o + 54, len));
        }
        o += 54 + len;
    }
    let n = n as usize;
    let mut raw = vec![0u8; n * rec_len];
    if let Some((vo, vl)) = laz_vlr {
        let vlr = laz::LazVlr::read_from(&data[vo..vo + vl]).map_err(|e| format!("laz vlr: {e}"))?;
        let mut cur = Cursor::new(data);
        cur.seek(SeekFrom::Start(off_points as u64)).map_err(|e| e.to_string())?;
        let sel = laz::DecompressionSelection(laz::DecompressionSelection::Z | laz::DecompressionSelection::CLASSIFICATION);
        let mut dec = laz::LasZipDecompressor::selective(cur, vlr, sel).map_err(|e| format!("laz: {e}"))?;
        dec.decompress_many(&mut raw).map_err(|e| format!("laz points: {e}"))?;
    } else if compressed {
        return Err("compressed LAS without a laszip VLR".into());
    } else {
        if off_points + n * rec_len > data.len() {
            return Err("LAS points past the end".into());
        }
        raw.copy_from_slice(&data[off_points..off_points + n * rec_len]);
    }
    let mut xyz = Vec::with_capacity(n);
    let mut cls = Vec::with_capacity(n);
    for k in 0..n {
        let r = &raw[k * rec_len..(k + 1) * rec_len];
        let x = rd_i32(r, 0) as f64 * sx + ox;
        let y = rd_i32(r, 4) as f64 * sy + oy;
        if x < bbox[0] || x > bbox[2] || y < bbox[1] || y > bbox[3] {
            continue;
        }
        xyz.push([x, y, rd_i32(r, 8) as f64 * sz + oz]);
        cls.push(r[class_at] & class_mask);
    }
    Ok((xyz, cls))
}

/// (xyz (n, 3), class (n,)) of the LAS / LAZ file bytes `data` inside bbox (x0, y0, x1, y1), the file's coordinates.
#[pyfunction]
pub fn las_decode<'py>(py: Python<'py>, data: &[u8], bbox: [f64; 4])
                       -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<u8>>)> {
    let got = py.allow_threads(|| decode_las(data, bbox));
    let (xyz, cls) = got.map_err(pyo3::exceptions::PyValueError::new_err)?;
    let n = xyz.len();
    let flat: Vec<f64> = xyz.into_iter().flatten().collect();
    Ok((numpy::ndarray::Array2::from_shape_vec((n, 3), flat).expect("xyz").into_pyarray(py), cls.into_pyarray(py)))
}

/// A uniform grid over segments (ax, ay, bx, by): the candidates near a point.
struct SegGrid {
    x0: f64,
    y0: f64,
    cell: f64,
    nx: usize,
    ny: usize,
    cells: Vec<Vec<u32>>,
}

impl SegGrid {
    fn new(segs: &[[f64; 4]], cell: f64, pad: f64) -> SegGrid {
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for s in segs {
            x0 = x0.min(s[0].min(s[2]));
            y0 = y0.min(s[1].min(s[3]));
            x1 = x1.max(s[0].max(s[2]));
            y1 = y1.max(s[1].max(s[3]));
        }
        if !x0.is_finite() {
            return SegGrid { x0: 0.0, y0: 0.0, cell, nx: 0, ny: 0, cells: Vec::new() };
        }
        x0 -= pad;
        y0 -= pad;
        let nx = (((x1 + pad - x0) / cell).ceil() as usize).clamp(1, 4096);
        let ny = (((y1 + pad - y0) / cell).ceil() as usize).clamp(1, 4096);
        let cell = cell.max((x1 + pad - x0) / nx as f64).max((y1 + pad - y0) / ny as f64);
        let mut cells = vec![Vec::new(); nx * ny];
        for (k, s) in segs.iter().enumerate() {
            let cx0 = (((s[0].min(s[2]) - pad - x0) / cell).floor().max(0.0) as usize).min(nx - 1);
            let cx1 = (((s[0].max(s[2]) + pad - x0) / cell).floor().max(0.0) as usize).min(nx - 1);
            let cy0 = (((s[1].min(s[3]) - pad - y0) / cell).floor().max(0.0) as usize).min(ny - 1);
            let cy1 = (((s[1].max(s[3]) + pad - y0) / cell).floor().max(0.0) as usize).min(ny - 1);
            for cy in cy0..=cy1 {
                for cx in cx0..=cx1 {
                    cells[cy * nx + cx].push(k as u32);
                }
            }
        }
        SegGrid { x0, y0, cell, nx, ny, cells }
    }

    fn near(&self, x: f64, y: f64) -> &[u32] {
        if self.nx == 0 {
            return &[];
        }
        let cx = (x - self.x0) / self.cell;
        let cy = (y - self.y0) / self.cell;
        if cx < 0.0 || cy < 0.0 || cx >= self.nx as f64 || cy >= self.ny as f64 {
            return &[];
        }
        &self.cells[cy as usize * self.nx + cx as usize]
    }
}

/// (t in [0, 1], distance) of point p on segment s.
fn on_seg(s: &[f64; 4], x: f64, y: f64) -> (f64, f64) {
    let (abx, aby) = (s[2] - s[0], s[3] - s[1]);
    let l2 = abx * abx + aby * aby;
    let t = if l2 > 1e-12 { (((x - s[0]) * abx + (y - s[1]) * aby) / l2).clamp(0.0, 1.0) } else { 0.0 };
    (t, (x - (s[0] + t * abx)).hypot(y - (s[1] + t * aby)))
}

/// Heights along a map road from a source's 3D lines. q (n, 2): the road's stations; qdir (n, 2): its unit direction
/// there; seg (m, 4): the source's segments (ax, ay, bx, by) and segz (m, 2) their end heights (NaN: none); ok (m,):
/// 1 where a segment may match this road (its level and name agree: the caller's test). Each station takes the
/// nearest allowed segment within max_d whose direction is within acos(min_cos) of the road's (either way), its height
/// interpolated there. (z (n,) NaN where none, distance (n,) inf where none.) Ties go to the lower segment index.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn roadz_match<'py>(py: Python<'py>, q: PyReadonlyArray2<'py, f64>, qdir: PyReadonlyArray2<'py, f64>,
                        seg: PyReadonlyArray2<'py, f64>, segz: PyReadonlyArray2<'py, f64>,
                        ok: PyReadonlyArray1<'py, u8>, max_d: f64, min_cos: f64)
                        -> (Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>) {
    let (q, qdir, seg, segz, ok) = (q.as_array(), qdir.as_array(), seg.as_array(), segz.as_array(), ok.as_array());
    let segs: Vec<[f64; 4]> = (0..seg.nrows()).map(|k| [seg[[k, 0]], seg[[k, 1]], seg[[k, 2]], seg[[k, 3]]]).collect();
    let n = q.nrows();
    let work = n * 8 + segs.len() * 4;
    let (z, d) = crate::release(py, work, || {
        let grid = SegGrid::new(&segs, 32.0, max_d);
        let mut z = vec![f64::NAN; n];
        let mut dd = vec![f64::INFINITY; n];
        for i in 0..n {
            let (x, y) = (q[[i, 0]], q[[i, 1]]);
            let (ux, uy) = (qdir[[i, 0]], qdir[[i, 1]]);
            let mut best = (f64::INFINITY, usize::MAX, 0.0);
            for &k in grid.near(x, y) {
                let k = k as usize;
                if ok[k] == 0 {
                    continue;
                }
                let s = &segs[k];
                let (sx, sy) = (s[2] - s[0], s[3] - s[1]);
                let sl = sx.hypot(sy);
                if sl < 1e-6 || ((sx * ux + sy * uy) / sl).abs() < min_cos {
                    continue;
                }
                let (t, d) = on_seg(s, x, y);
                if d <= max_d && (d < best.0 || (d == best.0 && k < best.1)) {
                    best = (d, k, t);
                }
            }
            if best.1 != usize::MAX {
                let (z0, z1) = (segz[[best.1, 0]], segz[[best.1, 1]]);
                let t = best.2;
                let zz = if z0.is_finite() && z1.is_finite() {
                    z0 + (z1 - z0) * t
                } else if z0.is_finite() && t < 0.5 {
                    z0
                } else if z1.is_finite() && t >= 0.5 {
                    z1
                } else {
                    f64::NAN
                };
                z[i] = zz;
                dd[i] = if zz.is_finite() { best.0 } else { f64::INFINITY };
            }
        }
        (z, dd)
    });
    (z.into_pyarray(py), d.into_pyarray(py))
}

fn median(v: &mut [f64]) -> f64 {
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let m = v.len();
    if m == 0 {
        return f64::NAN;
    }
    if m % 2 == 1 { v[m / 2] } else { 0.5 * (v[m / 2 - 1] + v[m / 2]) }
}

/// The profile z at arc lengths s (ascending; NaN where unknown) cleaned: values more than spike_m off the median of
/// the samples within win_m are dropped (twice); then, where two neighbouring kept samples are steeper than max_grade,
/// the one farther off its median goes, until none is; gaps of max_gap_m or less are bridged linearly (none past the
/// first / last kept sample); then smoothed (a Gaussian of sigma_m over the kept samples, 0: none). NaN where unknown.
pub fn clean(s: &[f64], z: &[f64], max_grade: f64, spike_m: f64, win_m: f64, max_gap_m: f64, sigma_m: f64) -> Vec<f64> {
    let n = s.len();
    let mut keep: Vec<bool> = z.iter().map(|v| v.is_finite()).collect();
    let local_med = |keep: &Vec<bool>, i: usize| -> f64 {
        let mut v = Vec::new();
        let (mut a, mut b) = (i, i);
        while a > 0 && s[i] - s[a - 1] <= win_m {
            a -= 1;
        }
        while b + 1 < n && s[b + 1] - s[i] <= win_m {
            b += 1;
        }
        for j in a..=b {
            if keep[j] {
                v.push(z[j]);
            }
        }
        median(&mut v)
    };
    for _ in 0..2 {
        let meds: Vec<f64> = (0..n).map(|i| if keep[i] { local_med(&keep, i) } else { f64::NAN }).collect();
        for i in 0..n {
            if keep[i] && (z[i] - meds[i]).abs() > spike_m {
                keep[i] = false;
            }
        }
    }
    for _ in 0..n {
        let idx: Vec<usize> = (0..n).filter(|&i| keep[i]).collect();
        let mut worst: Option<(f64, usize)> = None;
        for w in idx.windows(2) {
            let (a, b) = (w[0], w[1]);
            let ds = (s[b] - s[a]).max(1e-6);
            let g = (z[b] - z[a]).abs() / ds;
            // (a jump over a long gap may be real: only the grade beyond what the gap allows, with 0.5 m of noise)
            if g > max_grade && (z[b] - z[a]).abs() > max_grade * ds + 0.5 {
                let (ea, eb) = ((z[a] - local_med(&keep, a)).abs(), (z[b] - local_med(&keep, b)).abs());
                let (bad, e) = if eb >= ea { (b, eb) } else { (a, ea) };
                if worst.map_or(true, |(we, wi)| e > we || (e == we && bad < wi)) {
                    worst = Some((e, bad));
                }
            }
        }
        match worst {
            Some((_, i)) => keep[i] = false,
            None => break,
        }
    }
    let mut out: Vec<f64> = (0..n).map(|i| if keep[i] { z[i] } else { f64::NAN }).collect();
    let idx: Vec<usize> = (0..n).filter(|&i| keep[i]).collect();
    for w in idx.windows(2) {
        let (a, b) = (w[0], w[1]);
        if b > a + 1 && s[b] - s[a] <= max_gap_m {
            for j in a + 1..b {
                let t = (s[j] - s[a]) / (s[b] - s[a]).max(1e-9);
                out[j] = z[a] + (z[b] - z[a]) * t;
            }
        }
    }
    if sigma_m > 0.0 {
        let src = out.clone();
        let reach = 3.0 * sigma_m;
        for i in 0..n {
            if !src[i].is_finite() {
                continue;
            }
            let (mut a, mut b) = (i, i);
            while a > 0 && s[i] - s[a - 1] <= reach && src[a - 1].is_finite() {
                a -= 1;
            }
            while b + 1 < n && s[b + 1] - s[i] <= reach && src[b + 1].is_finite() {
                b += 1;
            }
            // (point-reflected at a run's ends, as grading's profiles: the end keeps its height and grade)
            let (mut acc, mut wsum) = (0.0, 0.0);
            for j in a..=b {
                let d = s[j] - s[i];
                let w = (-0.5 * (d / sigma_m) * (d / sigma_m)).exp();
                acc += w * src[j];
                wsum += w;
                let mirror_lo = 2.0 * s[a] - s[j];
                if j > a && s[i] - mirror_lo <= reach {
                    let dm = mirror_lo - s[i];
                    let wm = (-0.5 * (dm / sigma_m) * (dm / sigma_m)).exp();
                    acc += wm * (2.0 * src[a] - src[j]);
                    wsum += wm;
                }
                let mirror_hi = 2.0 * s[b] - s[j];
                if j < b && mirror_hi - s[i] <= reach {
                    let dm = mirror_hi - s[i];
                    let wm = (-0.5 * (dm / sigma_m) * (dm / sigma_m)).exp();
                    acc += wm * (2.0 * src[b] - src[j]);
                    wsum += wm;
                }
            }
            out[i] = acc / wsum;
        }
    }
    out
}

/// clean() for Python: (z (n,) cleaned, NaN where unknown).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn profile_clean<'py>(py: Python<'py>, s: PyReadonlyArray1<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                          max_grade: f64, spike_m: f64, win_m: f64, max_gap_m: f64, sigma_m: f64)
                          -> Bound<'py, PyArray1<f64>> {
    let (s, z) = (s.as_array().to_vec(), z.as_array().to_vec());
    clean(&s, &z, max_grade, spike_m, win_m, max_gap_m, sigma_m).into_pyarray(py)
}

/// Lidar classes never a road surface: low / high noise (7, 18), water (9), wire (13-14), tower (15).
fn unusable(c: u8) -> bool {
    matches!(c, 7 | 9 | 13 | 14 | 15 | 18)
}

const CLUSTER_GAP_M: f64 = 0.4;     // m between heights that start another surface at a station
const MAX_CANDS: usize = 6;         // surfaces kept per station (the most points)
const MISS_COST: f64 = 2.5;         // a station whose points are all passed over (cars, signs, another deck)
const GRADE_SIGMA: f64 = 0.04;      // a grade this steep between kept stations costs 1
const SMALL_COST: f64 = 1.0;        // a surface of fewer than MIN_PTS points

/// One deck profile from the point cloud along a bridge's line (k, 2), the anchor frame: stations every `step` m
/// (evenly, ending on both ends); every usable point (pts (n, 3) xyz, cls (n,)) within half_w of the line and over
/// the stretch nearest a station counts for it; its heights clustered into surfaces (CLUSTER_GAP_M); a Viterbi pass
/// picks one surface or none per station: the cost of a surface is how few of the station's points it has (and
/// SMALL_COST under min_pts), a step between kept stations ((grade / GRADE_SIGMA)^2, impossible beyond max_grade
/// plus 0.5 m of noise), a station passed over MISS_COST (nothing when it had no points). Then clean() (spikes, gaps
/// of max_gap_m, sigma_m smoothing). (s (m,), z (m,) NaN where unseen, points (m,) of the chosen surface.)
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn deck_lidar<'py>(py: Python<'py>, line: PyReadonlyArray2<'py, f64>, pts: PyReadonlyArray2<'py, f64>,
                       cls: PyReadonlyArray1<'py, u8>, half_w: f64, step: f64, max_grade: f64, min_pts: i64,
                       max_gap_m: f64, sigma_m: f64)
                       -> (Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>) {
    let (line, pts, cls) = (line.as_array(), pts.as_array(), cls.as_array());
    let lv: Vec<[f64; 2]> = (0..line.nrows()).map(|k| [line[[k, 0]], line[[k, 1]]]).collect();
    let np_ = pts.nrows();
    let (s_out, z_out, c_out) = crate::release(py, np_ * 4 + 1, || {
        let k = lv.len();
        if k < 2 {
            return (Vec::new(), Vec::new(), Vec::new());
        }
        let mut arc = vec![0.0; k];
        for i in 1..k {
            arc[i] = arc[i - 1] + (lv[i][0] - lv[i - 1][0]).hypot(lv[i][1] - lv[i - 1][1]);
        }
        let len = arc[k - 1];
        let m = ((len / step.max(0.5)).ceil() as usize).max(1) + 1;
        let ds = len / (m - 1) as f64;
        let st: Vec<f64> = (0..m).map(|i| i as f64 * ds).collect();
        let segs: Vec<[f64; 4]> = (0..k - 1).map(|i| [lv[i][0], lv[i][1], lv[i + 1][0], lv[i + 1][1]]).collect();
        let grid = SegGrid::new(&segs, 32.0, half_w);
        let mut bins: Vec<Vec<f64>> = vec![Vec::new(); m];
        for p in 0..np_ {
            if unusable(cls[p]) {
                continue;
            }
            let (x, y, z) = (pts[[p, 0]], pts[[p, 1]], pts[[p, 2]]);
            if !z.is_finite() {
                continue;
            }
            let mut best = (f64::INFINITY, usize::MAX, 0.0);
            for &sk in grid.near(x, y) {
                let sk = sk as usize;
                let (t, d) = on_seg(&segs[sk], x, y);
                if d < best.0 || (d == best.0 && sk < best.1) {
                    best = (d, sk, t);
                }
            }
            if best.1 == usize::MAX || best.0 > half_w {
                continue;
            }
            let a = arc[best.1] + best.2 * (arc[best.1 + 1] - arc[best.1]);
            // (not past the line's ends: the ground beyond an abutment is another road's)
            let s_seg = &segs[best.1];
            let (abx, aby) = (s_seg[2] - s_seg[0], s_seg[3] - s_seg[1]);
            let tr = ((x - s_seg[0]) * abx + (y - s_seg[1]) * aby) / (abx * abx + aby * aby).max(1e-12);
            if (best.1 == 0 && tr < 0.0) || (best.1 == k - 2 && tr > 1.0) {
                continue;
            }
            let i = ((a / ds).round() as usize).min(m - 1);
            bins[i].push(z);
        }
        // the surfaces at each station: (height: the median, points)
        let mut cands: Vec<Vec<(f64, usize)>> = Vec::with_capacity(m);
        for b in bins.iter_mut() {
            b.sort_by(|a, c| a.partial_cmp(c).unwrap());
            let mut cl: Vec<(f64, usize)> = Vec::new();
            let mut a = 0;
            for j in 1..=b.len() {
                if j == b.len() || b[j] - b[j - 1] > CLUSTER_GAP_M {
                    let mut v = b[a..j].to_vec();
                    cl.push((median(&mut v), j - a));
                    a = j;
                }
            }
            cl.sort_by(|p, q| q.1.cmp(&p.1).then(p.0.partial_cmp(&q.0).unwrap()));
            cl.truncate(MAX_CANDS);
            cands.push(cl);
        }
        // Viterbi: state c < n_i = surface c, state n_i = passed over (it carries its predecessor's last height)
        struct St { cost: f64, zl: f64, sl: f64, prev: usize }
        let mut lay: Vec<Vec<St>> = Vec::with_capacity(m);
        for i in 0..m {
            let ci = &cands[i];
            let tot = ci.iter().map(|c| c.1).max().unwrap_or(0).max(1) as f64;
            let emit = |c: &(f64, usize)| (1.0 - c.1 as f64 / tot) * 2.0 + if (c.1 as i64) < min_pts { SMALL_COST } else { 0.0 };
            let miss = if ci.is_empty() { 0.0 } else { MISS_COST };
            let mut row: Vec<St> = Vec::with_capacity(ci.len() + 1);
            if i == 0 {
                for c in ci {
                    row.push(St { cost: emit(c), zl: c.0, sl: st[i], prev: usize::MAX });
                }
                row.push(St { cost: miss, zl: f64::NAN, sl: f64::NAN, prev: usize::MAX });
            } else {
                let pr = &lay[i - 1];
                for c in ci {
                    let mut best = (f64::INFINITY, usize::MAX);
                    for (pi, p) in pr.iter().enumerate() {
                        let tc = if p.zl.is_finite() {
                            let gap = (st[i] - p.sl).max(1e-6);
                            let dz = (c.0 - p.zl).abs();
                            if dz > max_grade * gap + 0.5 {
                                f64::INFINITY
                            } else {
                                let g = (dz - 0.1).max(0.0) / gap;
                                (g / GRADE_SIGMA) * (g / GRADE_SIGMA)
                            }
                        } else {
                            0.0
                        };
                        let tot_c = p.cost + tc;
                        if tot_c < best.0 {
                            best = (tot_c, pi);
                        }
                    }
                    row.push(St { cost: best.0 + emit(c), zl: c.0, sl: st[i], prev: best.1 });
                }
                let mut best = (f64::INFINITY, usize::MAX);
                for (pi, p) in pr.iter().enumerate() {
                    if p.cost < best.0 {
                        best = (p.cost, pi);
                    }
                }
                let p = &pr[best.1];
                row.push(St { cost: best.0 + miss, zl: p.zl, sl: p.sl, prev: best.1 });
            }
            lay.push(row);
        }
        let mut z = vec![f64::NAN; m];
        let mut cnt = vec![0.0; m];
        let last = &lay[m - 1];
        let mut at = (0..last.len()).fold(0, |b, j| if last[j].cost < last[b].cost { j } else { b });
        for i in (0..m).rev() {
            if at < cands[i].len() {
                z[i] = cands[i][at].0;
                cnt[i] = cands[i][at].1 as f64;
            }
            at = lay[i][at].prev;
            if at == usize::MAX {
                break;
            }
        }
        let zc = clean(&st, &z, max_grade, 1.0, 12.0, max_gap_m, sigma_m);
        for i in 0..m {
            if !zc[i].is_finite() {
                cnt[i] = 0.0;
            }
        }
        (st, zc, cnt)
    });
    (s_out.into_pyarray(py), z_out.into_pyarray(py), c_out.into_pyarray(py))
}

/// The profile z at arc lengths s (ascending, all finite) eased onto end heights t0 / t1 (NaN: that end left): the
/// difference at an end fades out (smoothstep) over at least min_m, long enough that the grade changes by at most
/// max_dg (and never over more than share of the line). Pure.
#[pyfunction]
pub fn deck_blend<'py>(py: Python<'py>, s: PyReadonlyArray1<'py, f64>, z: PyReadonlyArray1<'py, f64>, t0: f64, t1: f64,
                       max_dg: f64, min_m: f64, share: f64) -> Bound<'py, PyArray1<f64>> {
    let (s, z) = (s.as_array(), z.as_array());
    let n = s.len();
    let mut out: Vec<f64> = z.to_vec();
    if n >= 2 {
        let len = s[n - 1] - s[0];
        for (end, t) in [(0usize, t0), (1usize, t1)] {
            if !t.is_finite() {
                continue;
            }
            let d = if end == 0 { t - z[0] } else { t - z[n - 1] };
            // (a smoothstep over L changes the grade by 1.5 d / L at most)
            let l = (1.5 * d.abs() / max_dg.max(1e-6)).max(min_m).min(len * share).max(1e-6);
            for i in 0..n {
                let dist = if end == 0 { s[i] - s[0] } else { s[n - 1] - s[i] };
                let u = (1.0 - dist / l).clamp(0.0, 1.0);
                out[i] += d * u * u * (3.0 - 2.0 * u);
            }
        }
    }
    out.into_pyarray(py)
}

/// How far a ground road goes on elevated from a deck's end (roads.lidar_gap_candidates, roadz.RoadZ: an untagged
/// viaduct stretch). Its stations at arc lengths s (ascending, from the deck's end), the surface the lidar picked
/// there z (NaN: none) and the DTM under it dem: the stretch starts at the first station with a surface (one of the
/// first two) when that is at least rise_m over the DTM and, where the deck's own measured end height z_end is known
/// (else NaN), within end_tol_m of it; it goes on over stations at least rise_m over the DTM, max_miss stations
/// without (no surface, or lower) passed over at most in a row. m along the line to its last such station, 0: none.
pub fn gap_stretch(s: &[f64], z: &[f64], dem: &[f64], z_end: f64, rise_m: f64, end_tol_m: f64, max_miss: usize) -> f64 {
    let n = s.len().min(z.len()).min(dem.len());
    let up = |k: usize| z[k].is_finite() && dem[k].is_finite() && z[k] - dem[k] >= rise_m;
    let Some(first) = (0..n.min(2)).find(|&k| z[k].is_finite()) else { return 0.0 };
    if !up(first) || (z_end.is_finite() && (z[first] - z_end).abs() > end_tol_m) {
        return 0.0;
    }
    let (mut last, mut miss) = (first, 0usize);
    for k in first + 1..n {
        if up(k) {
            last = k;
            miss = 0;
        } else {
            miss += 1;
            if miss > max_miss {
                break;
            }
        }
    }
    s[last]
}

/// gap_stretch for Python: (s, z, dem) (n,), z_end (NaN: unknown), rise_m, end_tol_m, max_miss. m.
#[pyfunction]
pub fn lidar_gap_stretch<'py>(s: PyReadonlyArray1<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                              dem: PyReadonlyArray1<'py, f64>, z_end: f64, rise_m: f64, end_tol_m: f64,
                              max_miss: usize) -> f64 {
    let (s, z, dem) = (s.as_array().to_vec(), z.as_array().to_vec(), dem.as_array().to_vec());
    gap_stretch(&s, &z, &dem, z_end, rise_m, end_tol_m, max_miss)
}

#[cfg(test)]
mod gap_tests {
    use super::gap_stretch;

    #[test]
    fn elevated_stretch_then_ground() {
        let s: Vec<f64> = (0..10).map(|k| 4.0 * k as f64).collect();
        let dem = vec![10.0; 10];
        let z = vec![18.0, 17.8, 17.5, 17.2, f64::NAN, 16.8, 11.0, 10.2, 10.1, 10.0];
        assert_eq!(gap_stretch(&s, &z, &dem, 18.1, 3.0, 1.5, 1), 20.0);
        // (an at-grade end, or one off the deck's measured end, or no surface: none)
        let low = vec![10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 11.4, 11.6, 11.8, 12.0];
        assert_eq!(gap_stretch(&s, &low, &dem, 10.0, 3.0, 1.5, 1), 0.0);
        assert_eq!(gap_stretch(&s, &z, &dem, 25.0, 3.0, 1.5, 1), 0.0);
        assert_eq!(gap_stretch(&s, &[f64::NAN; 10], &dem, f64::NAN, 3.0, 1.5, 1), 0.0);
    }
}
