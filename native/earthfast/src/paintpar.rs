//! The ground photo's tone paint on several threads (earthfast 652; ~/bnge-tests/reports/
//! gpu_photo_compositor_design.md section 12: measured beside a GPU version of the road step, this was as fast and
//! needs no GPU). roadtone.rs's line_cover + tone_paint made a float32 coverage plane of the whole photo per road
//! kind (five: 80 MB at 2048², 335 MB at 4096²) and walked them on one thread, also for a square with one kind of
//! road, or a car park in one corner.
//!
//! Here: a plane only for the kinds the square has, the coverage filled row by row, the tone samples gathered per
//! row of tone cells and the blend done per pixel row, on par.rs's threads, and only over the rows something covers.
//! Every pixel and every tone cell is computed from the same values in the same order as the one-thread kernel's
//! (a pixel's coverage is the largest of the same float32 values; a cell's samples are summed row by row, left to
//! right; a pixel's kinds are blended in their order), so the photo is byte-identical to it on any number of threads
//! (tests/test_paintlist.py against the Python reference, tests/test_paintpar.py against roadtone's kernel).

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use numpy::ndarray::{Array3, ArrayView2};
use pyo3::prelude::*;

// --- the steps' times (a measurement aid) ----------------------------------------------------------------------

pub const STEPS: [&str; 7] = ["fields", "golf", "lots", "pads", "islands", "roads", "corners"];
static STEP_NS: [AtomicU64; 7] = [const { AtomicU64::new(0) }; 7];
static STEP_N: [AtomicU64; 7] = [const { AtomicU64::new(0) }; 7];

/// f timed as paint step `step` (an index into STEPS).
pub fn timed<R>(step: usize, f: impl FnOnce() -> R) -> R {
    let t = Instant::now();
    let out = f();
    STEP_NS[step].fetch_add(t.elapsed().as_nanos() as u64, Ordering::Relaxed);
    STEP_N[step].fetch_add(1, Ordering::Relaxed);
    out
}

/// The ground photos' paint steps since the process started (or the last reset=True): [(step, calls, wall ms)]
/// (paintlist.rs PaintLayers: fields, golf, lots, pads, islands, roads, corners).
#[pyfunction]
#[pyo3(signature = (reset=false))]
pub fn paint_step_ms(reset: bool) -> Vec<(&'static str, u64, f64)> {
    (0..STEPS.len()).map(|i| {
        let (ns, n) = if reset {
            (STEP_NS[i].swap(0, Ordering::Relaxed), STEP_N[i].swap(0, Ordering::Relaxed))
        } else {
            (STEP_NS[i].load(Ordering::Relaxed), STEP_N[i].load(Ordering::Relaxed))
        };
        (STEPS[i], n, ns as f64 / 1e6)
    }).collect()
}

// --- coverage ----------------------------------------------------------------------------------------------------

/// The tone paint's style: the kinds' colours (k, 4) and tone rows (k, 9), the tone radius in pixels and the sample
/// limits (roadtone::tone_paint's arguments).
pub struct Tone<'a> {
    pub colors: ArrayView2<'a, f32>,
    pub tone: ArrayView2<'a, f32>,
    pub radius: usize,
    pub luma: (f32, f32),
    pub sat: f32,
    pub green: f32,
}

/// Coverage planes per kind (None: all zero) of an h x w photo, and the rows [r0, r1) outside which every plane is
/// zero.
pub struct Cover {
    pub planes: Vec<Option<Vec<f32>>>,
    pub rows: (usize, usize),
}

/// A segment line_cover would draw: its ends, half width, kind, and the pixel box it may touch.
struct Seg {
    a: [f64; 2],
    b: [f64; 2],
    r: f64,
    kind: usize,
    c0: usize,
    c1: usize,
    r0: usize,
    r1: usize,
}

/// line_cover's own tests and box per segment (the ones it skips are left out).
fn live_segs(h: usize, w: usize, segs: &[[f64; 4]], hw: &[f64], kind: &[u8]) -> Vec<Seg> {
    let mut out = Vec::with_capacity(kind.len());
    for s in 0..kind.len() {
        let [ax, ay, bx, by] = segs[s];
        let r = hw[s];
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
        out.push(Seg { a: [ax, ay], b: [bx, by], r, kind: kind[s] as usize, c0: c0 as usize, c1: c1 as usize,
                       r0: r0 as usize, r1: r1 as usize });
    }
    out
}

/// line_cover's coverage of the pixel centre (px, py) by segment s (float64, its expression).
#[inline]
fn seg_cover(s: &Seg, px: f64, py: f64) -> f32 {
    let (dx, dy) = (s.b[0] - s.a[0], s.b[1] - s.a[1]);
    let l2 = dx * dx + dy * dy;
    let t = if l2 > 0.0 { (((px - s.a[0]) * dx + (py - s.a[1]) * dy) / l2).clamp(0.0, 1.0) } else { 0.0 };
    let (ex, ey) = (px - s.a[0] - t * dx, py - s.a[1] - t * dy);
    (s.r - (ex * ex + ey * ey).sqrt() + 0.5).clamp(0.0, 1.0) as f32
}

const BAND: usize = 32;     // rows per bucket of segments (line_cover_sparse)

/// roadtone::line_cover's planes (segs (m, 4) pixel ends, hw half widths, kind into k kinds) with the pavement masks
/// laid over them (paintlist.rs step_roads: max(plane, mask / 255)), only for the kinds that have a segment or a
/// mask, each row filled on its own from the segments that reach it.
pub fn line_cover_sparse(h: usize, w: usize, k: usize, segs: &[[f64; 4]], hw: &[f64], kind: &[u8],
                         pavement: &[(usize, ArrayView2<'_, u8>)]) -> Cover {
    let segs = live_segs(h, w, segs, hw, kind);
    let mut planes: Vec<Option<Vec<f32>>> = (0..k).map(|_| None).collect();
    let mut buckets: Vec<Vec<Vec<u32>>> = (0..k).map(|_| Vec::new()).collect();
    let (mut r0, mut r1) = (h, 0usize);
    for (i, s) in segs.iter().enumerate() {
        if s.kind >= k {
            continue;
        }
        if buckets[s.kind].is_empty() {
            buckets[s.kind] = vec![Vec::new(); h.div_ceil(BAND)];
        }
        for b in s.r0 / BAND..=(s.r1 - 1) / BAND {
            buckets[s.kind][b].push(i as u32);
        }
        r0 = r0.min(s.r0);
        r1 = r1.max(s.r1);
    }
    for q in 0..k {
        let mask = pavement.iter().filter(|(kind, _)| *kind == q).collect::<Vec<_>>();
        if buckets[q].is_empty() && mask.is_empty() {
            continue;
        }
        let mut plane = vec![0f32; h * w];
        if !buckets[q].is_empty() {
            let bk = &buckets[q];
            let threads = crate::par::threads_for(h * w, 1 << 18);
            crate::par::rows(&mut plane, w, threads, |j, row| {
                let py = j as f64 + 0.5;
                for &i in &bk[j / BAND] {
                    let s = &segs[i as usize];
                    if j < s.r0 || j >= s.r1 {
                        continue;
                    }
                    for (x, v) in row[s.c0..s.c1].iter_mut().enumerate() {
                        let c = seg_cover(s, (s.c0 + x) as f64 + 0.5, py);
                        if c > *v { *v = c; }
                    }
                }
            });
        }
        for (_, m) in mask {
            for (p, &value) in plane.iter_mut().zip(m.iter()) {
                *p = p.max(value as f32 / 255.0);
            }
            (r0, r1) = (0, h);
        }
        planes[q] = Some(plane);
    }
    Cover { planes, rows: (r0.min(r1), r1) }
}

// --- the tone paint ----------------------------------------------------------------------------------------------

/// roadtone::tone_paint of img in place from sparse planes: the gather per row of tone cells, the blend per pixel
/// row, over cover.rows alone (a row no plane covers has no sample and no paint).
pub fn tone_paint_sparse(img: &mut Array3<u8>, cover: &Cover, st: &Tone) {
    const ROBUST_LUMA: f32 = 22.0;
    const LIGHT_RAMP: f32 = 20.0;
    let (h, w) = (img.shape()[0], img.shape()[1]);
    let (col, tn) = (st.colors, st.tone);
    let k = col.nrows();
    let planes = &cover.planes;
    let (row0, row1) = (cover.rows.0.min(h), cover.rows.1.min(h));
    if row0 >= row1 || planes.iter().all(Option::is_none) {
        return;
    }
    if !img.is_standard_layout() {
        *img = img.as_standard_layout().into_owned();
    }
    let f = (st.radius / 3).max(1);
    let (gh, gw) = ((h + f - 1) / f, (w + f - 1) / f);
    let rc = ((st.radius + f / 2) / f).max(1);
    let sampled: Vec<bool> = (0..k).map(|q| tn[[q, 3]] > 0.0 && planes[q].is_some()).collect();
    let used: Vec<usize> = (0..k).filter(|&q| planes[q].is_some()).collect();
    let work = (row1 - row0) * w;
    let threads = crate::par::threads_for(work, 1 << 17);
    // the tone cells' rows that hold a covered pixel row
    let (g0, g1) = (row0 / f, (row1 - 1) / f + 1);
    type Tones = Vec<Option<[Vec<f32>; 4]>>;
    let tones = {
        let src = img.as_slice().expect("a contiguous photo");
        let gather = |first: Option<&Tones>| -> Tones {
            // acc: per row of cells (g0..g1), per kind, (n, r, g, b) per cell
            let row_len = k * 4 * gw;
            let mut acc = vec![0f32; (g1 - g0) * row_len];
            crate::par::rows(&mut acc, row_len, threads, |gi, row| {
                let gy = g0 + gi;
                for j in (gy * f).max(row0)..((gy + 1) * f).min(row1) {
                    for i in 0..w {
                        let p = j * w + i;
                        let mut top = usize::MAX;
                        for &q in used.iter().rev() {
                            if planes[q].as_ref().unwrap()[p] > 0.0 {
                                top = q;
                                break;
                            }
                        }
                        if top == usize::MAX || planes[top].as_ref().unwrap()[p] < 1.0 || !sampled[top] {
                            continue;
                        }
                        let (r, g, b) = (src[p * 3] as f32, src[p * 3 + 1] as f32, src[p * 3 + 2] as f32);
                        let luma = 0.299 * r + 0.587 * g + 0.114 * b;
                        let mx = r.max(g).max(b);
                        let sat = if mx > 0.0 { (mx - r.min(g).min(b)) / mx } else { 0.0 };
                        let cap = if tn.ncols() >= 9 && tn[[top, 5]] > 0.0 { tn[[top, 5]] } else { st.luma.1 };
                        if luma < st.luma.0 || luma > cap || sat > st.sat || (g > r + st.green && g > b + st.green) {
                            continue;
                        }
                        let cx = i / f;
                        if let Some(first) = first {
                            if let Some(t) = &first[top] {
                                let c = gy * gw + cx;
                                if t[0][c].is_finite() {
                                    let l0 = 0.299 * t[0][c] + 0.587 * t[1][c] + 0.114 * t[2][c];
                                    if (luma - l0).abs() > ROBUST_LUMA {
                                        continue;
                                    }
                                }
                            }
                        }
                        let o = (top * 4) * gw + cx;
                        row[o] += 1.0;
                        row[o + gw] += r;
                        row[o + 2 * gw] += g;
                        row[o + 3 * gw] += b;
                    }
                }
            });
            let mut tones: Tones = Vec::with_capacity(k);
            for q in 0..k {
                if !sampled[q] {
                    tones.push(None);
                    continue;
                }
                let plane = |c: usize| -> Vec<f32> {
                    let mut v = vec![0f32; gh * gw];
                    for gi in 0..g1 - g0 {
                        let o = gi * row_len + (q * 4 + c) * gw;
                        v[(g0 + gi) * gw..(g0 + gi + 1) * gw].copy_from_slice(&acc[o..o + gw]);
                    }
                    v
                };
                let n = plane(0);
                if !n.iter().any(|&v| v > 0.0) {
                    tones.push(None);
                    continue;
                }
                let n = crate::roadtone::box_sum(&n, gh, gw, rc);
                let least = tn[[q, 4]].max(1.0);
                let (sr, sg_, sb) = (crate::roadtone::box_sum(&plane(1), gh, gw, rc),
                                     crate::roadtone::box_sum(&plane(2), gh, gw, rc),
                                     crate::roadtone::box_sum(&plane(3), gh, gw, rc));
                let mut mr = vec![f32::NAN; gh * gw];
                let mut mg = vec![f32::NAN; gh * gw];
                let mut mb = vec![f32::NAN; gh * gw];
                for c in 0..gh * gw {
                    if n[c] >= least {
                        mr[c] = sr[c] / n[c];
                        mg[c] = sg_[c] / n[c];
                        mb[c] = sb[c] / n[c];
                    }
                }
                tones.push(Some([mr, mg, mb, n]));
            }
            tones
        };
        let first = gather(None);
        gather(Some(&first))
    };
    let tone_at = |t: &[Vec<f32>; 4], j: usize, i: usize| -> Option<([f32; 3], f32)> {
        let gy = ((j as f32 + 0.5) / f as f32 - 0.5).clamp(0.0, (gh - 1) as f32);
        let gx = ((i as f32 + 0.5) / f as f32 - 0.5).clamp(0.0, (gw - 1) as f32);
        let (y0, x0) = (gy.floor() as usize, gx.floor() as usize);
        let (y1, x1) = ((y0 + 1).min(gh - 1), (x0 + 1).min(gw - 1));
        let (fy, fx) = (gy - y0 as f32, gx - x0 as f32);
        let mut acc = [0f32; 3];
        let mut wsum = 0f32;
        for &(yy, xx, wt) in &[(y0, x0, (1.0 - fy) * (1.0 - fx)), (y0, x1, (1.0 - fy) * fx), (y1, x0, fy * (1.0 - fx)),
                               (y1, x1, fy * fx)] {
            let c = yy * gw + xx;
            if wt > 0.0 && t[0][c].is_finite() {
                acc[0] += t[0][c] * wt;
                acc[1] += t[1][c] * wt;
                acc[2] += t[2][c] * wt;
                wsum += wt;
            }
        }
        if wsum > 1e-6 { Some(([acc[0] / wsum, acc[1] / wsum, acc[2] / wsum], wsum)) } else { None }
    };
    let out = img.as_slice_mut().expect("a contiguous photo");
    crate::par::rows(&mut out[row0 * w * 3..row1 * w * 3], w * 3, threads, |jj, row| {
        let j = row0 + jj;
        for &q in &used {
            let pl = planes[q].as_ref().unwrap();
            let flat = [col[[q, 0]], col[[q, 1]], col[[q, 2]]];
            let alpha = col[[q, 3]];
            let (lmin, lmax, smax, share) = (tn[[q, 0]], tn[[q, 1]], tn[[q, 2]], tn[[q, 3]]);
            for i in 0..w {
                let a = pl[j * w + i] * alpha;
                if a <= 0.0 { continue; }
                let mut c = flat;
                if let Some(tq) = &tones[q] {
                    if let Some((mut t, cover)) = tone_at(tq, j, i) {
                        let luma = 0.299 * t[0] + 0.587 * t[1] + 0.114 * t[2];
                        let (mut lmax, mut smax) = (lmax, smax);
                        if tn.ncols() >= 9 && tn[[q, 7]] > lmax {
                            let mx0 = t[0].max(t[1]).max(t[2]);
                            let sat0 = if mx0 > 0.0 { (mx0 - t[0].min(t[1]).min(t[2])) / mx0 } else { 0.0 };
                            let (from, lcap, scap) = (tn[[q, 6]], tn[[q, 7]], tn[[q, 8]]);
                            let wl = ((luma - (from - LIGHT_RAMP)) / LIGHT_RAMP).clamp(0.0, 1.0);
                            let ws = if scap > 0.0 { ((1.5 * scap - sat0) / (0.5 * scap)).clamp(0.0, 1.0) } else { 1.0 };
                            let wgt = wl * ws;
                            lmax += (lcap - lmax) * wgt;
                            if scap > 0.0 && scap < smax { smax += (scap - smax) * wgt; }
                        }
                        let want = luma.clamp(lmin, lmax);
                        if luma > 0.0 {
                            for v in t.iter_mut() { *v *= want / luma; }
                        }
                        let mx = t[0].max(t[1]).max(t[2]);
                        let sat = if mx > 0.0 { (mx - t[0].min(t[1]).min(t[2])) / mx } else { 0.0 };
                        if sat > smax && sat > 0.0 {
                            let f = smax / sat;
                            for v in t.iter_mut() { *v = want + (*v - want) * f; }
                        }
                        for ch in 0..3 { c[ch] = flat[ch] + (t[ch] - flat[ch]) * share * cover; }
                    }
                }
                for ch in 0..3 {
                    let v = row[i * 3 + ch] as f32 * (1.0 - a) + c[ch] * a;
                    row[i * 3 + ch] = (v + 0.5).clamp(0.0, 255.0) as u8;
                }
            }
        }
    });
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(paint_step_ms, m)?)?;
    Ok(())
}
