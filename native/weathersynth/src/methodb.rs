//! Method B (the user's pick, 2026-11-25): the physical channel model of ~/bnge-tests/weather/procedural/methods/tools
//! (synth_b.py render + synth_b3.py render_dry3 / render3b / render3c), ported to Rust exactly. The approved renders
//! were made with numpy; nprng.rs draws numpy's numbers bit for bit, and every stage below follows the reference's
//! arithmetic (its FFT sizes and filter designs included), so the same seed gives the same signal within ~1e-12 in the
//! straight path (`Exact`). The fast path (`Fast`) changes only how the linear filters are computed (overlap-add,
//! kernels designed once per bin) and is checked against the straight one.
//!
//! Classes as finalB: strike / close = render3c (2-sample shock rise, micro-shocks after the class EQ, strike with
//! the quiet tear), mid = render3b, far = render3b with the rumble tilt (low-passed at 1.5 kHz).

use crate::dsp::C;
use crate::m;
use crate::nprng::NpRng;
use crate::npx::{self, convolve_full, convolve_same, hanning, irfft, median, rfft, rfftfreq, SR};

pub const SOUND: f64 = 344.6;
pub const OBS: [f64; 3] = [0.0, 0.0, 1.7];
pub const LENGTH_S: f64 = 14.0;
pub const PRE_S: f64 = 0.5;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum BClass {
    Strike,
    Close,
    Mid,
    Far,
}

/// One render's settings (as render_b_r3c.py / render_b_r3b.py call it for finalB).
#[derive(Clone, Debug)]
pub struct Settings {
    pub class: BClass,
    pub target_lufs: f64,
    pub eq: &'static [(f64, f64)],
    pub rise_s: f64,
    pub micro_w: f64,
    pub quiet_tear: bool,
    pub tilt_lp: Option<f64>,
    pub rumble_target: Option<f64>,
}

pub fn settings(class: BClass) -> Settings {
    use crate::methodb_data::*;
    match class {
        BClass::Strike => Settings { class, target_lufs: -13.0, eq: CORPUS_STRIKE, rise_s: 0.00005, micro_w: 0.30,
            quiet_tear: true, tilt_lp: None, rumble_target: None },
        BClass::Close => Settings { class, target_lufs: -14.0, eq: CORPUS_CLOSE, rise_s: 0.00005, micro_w: 0.12,
            quiet_tear: true, tilt_lp: None, rumble_target: None },
        BClass::Mid => Settings { class, target_lufs: -18.0, eq: CORPUS_MID, rise_s: 0.00025, micro_w: 0.0,
            quiet_tear: false, tilt_lp: None, rumble_target: None },
        BClass::Far => Settings { class, target_lufs: -22.0, eq: CORPUS_FAR, rise_s: 0.00025, micro_w: 0.0,
            quiet_tear: false, tilt_lp: Some(1500.0), rumble_target: Some(151.0) },
    }
}

/// The distance range per class and finalB's distance for a seed (np.random.default_rng(seed + 1000).uniform(range)).
pub fn class_range(class: BClass) -> (f64, f64) {
    match class {
        BClass::Strike => (60.0, 280.0),
        BClass::Close => (600.0, 1400.0),
        BClass::Mid => (2500.0, 5000.0),
        BClass::Far => (7000.0, 12000.0),
    }
}
pub fn final_dist(class: BClass, seed: u64) -> f64 {
    let (a, b) = class_range(class);
    NpRng::new(&[seed + 1000]).uniform(a, b)
}

// ------------------------------------------------------------------ small vector helpers (numpy semantics)

type V3 = [f64; 3];
fn norm3(a: V3) -> f64 {
    m::sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
}
fn sub3(a: V3, b: V3) -> V3 {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}
fn add3(a: V3, b: V3) -> V3 {
    [a[0] + b[0], a[1] + b[1], a[2] + b[2]]
}
fn sc3(a: V3, s: f64) -> V3 {
    [a[0] * s, a[1] * s, a[2] * s]
}
fn div3(a: V3, s: f64) -> V3 {
    [a[0] / s, a[1] / s, a[2] / s]
}
fn normal3(r: &mut NpRng, loc: f64, scale: f64) -> V3 {
    let a = r.normal(loc, scale);
    let b = r.normal(loc, scale);
    let c = r.normal(loc, scale);
    [a, b, c]
}
/// numpy's mean (pairwise summation as numpy's add.reduce does, blocks of 8 below 128) and std (ddof 0).
fn np_sum(x: &[f64]) -> f64 {
    pairwise(x)
}
fn pairwise(x: &[f64]) -> f64 {
    let n = x.len();
    if n < 8 {
        let mut s = 0.0;
        for v in x {
            s += v;
        }
        return s;
    }
    if n <= 128 {
        let mut r = [0.0f64; 8];
        r.copy_from_slice(&x[..8]);
        let mut i = 8;
        while i + 8 <= n {
            for k in 0..8 {
                r[k] += x[i + k];
            }
            i += 8;
        }
        let mut res = (r[0] + r[1]) + (r[2] + r[3]) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += x[i];
            i += 1;
        }
        return res;
    }
    let mut n2 = n / 2;
    n2 -= n2 % 8;
    pairwise(&x[..n2]) + pairwise(&x[n2..])
}
fn np_mean(x: &[f64]) -> f64 {
    np_sum(x) / x.len() as f64
}
fn np_std(x: &[f64]) -> f64 {
    let mu = np_mean(x);
    let d: Vec<f64> = x.iter().map(|v| (v - mu) * (v - mu)).collect();
    m::sqrt(np_sum(&d) / x.len() as f64)
}
fn linspace(a: f64, b: f64, n: usize) -> Vec<f64> {
    if n == 0 {
        return Vec::new();
    }
    if n == 1 {
        return vec![a];
    }
    let step = (b - a) / (n - 1) as f64;
    // numpy: a + i * step, the last forced to b
    let mut v: Vec<f64> = (0..n).map(|i| a + i as f64 * step).collect();
    v[n - 1] = b;
    v
}
/// Python's round() on a float: half to even.
fn py_round(x: f64) -> f64 {
    let r = m::round(x);
    if (x - x.trunc()).abs() == 0.5 {
        2.0 * m::round(x / 2.0)
    } else {
        r
    }
}

// ------------------------------------------------------------------ the filters (tlib / synth_b3)

/// ISO 9613-1 at 22 degC, 85 % RH (tlib.iso9613_alpha's defaults).
fn alpha(f: f64) -> f64 {
    crate::thunder::alpha(f)
}

/// min_phase_filter_floor (synth_b3: the floor 1e-7, N = next pow2(n + 32768)): the r3b replacement of every
/// tlib.min_phase_filter in the render.
pub fn min_phase_floor(x: &[f64], mag: &dyn Fn(f64) -> f64) -> Vec<f64> {
    let n = x.len();
    let big = crate::dsp::next_pow2(n + 32768);
    let f = rfftfreq(big);
    let lm: Vec<C> = f.iter().map(|&fr| C::new(m::ln(mag(fr).max(1e-7)), 0.0)).collect();
    let mut c = irfft(&lm, big);
    for v in c.iter_mut().take(big / 2).skip(1) {
        *v *= 2.0;
    }
    for v in c.iter_mut().skip(big / 2 + 1) {
        *v = 0.0;
    }
    let ch = rfft(&c, big);
    let h: Vec<C> = ch.iter().map(|z| z.expc()).collect();
    let xf = rfft(x, big);
    let prod: Vec<C> = xf.iter().zip(&h).map(|(a, b)| a.mul(*b)).collect();
    let mut y = irfft(&prod, big);
    y.truncate(n);
    y
}

/// tlib.fft_filter (a zero-phase magnitude, N = next pow2(n + 4096)).
pub fn fft_filter(x: &[f64], mag: &dyn Fn(f64) -> f64) -> Vec<f64> {
    let n = x.len();
    let big = crate::dsp::next_pow2(n + 4096);
    let f = rfftfreq(big);
    let xf = rfft(x, big);
    let prod: Vec<C> = xf.iter().zip(&f).map(|(a, &fr)| a.scale(mag(fr))).collect();
    let mut y = irfft(&prod, big);
    y.truncate(n);
    y
}

fn absorb_mag(r: f64) -> impl Fn(f64) -> f64 {
    move |f: f64| m::pow(10.0, -(alpha(f) * r) / 20.0)
}
fn highpass_mag(fc: f64, order: i32) -> impl Fn(f64) -> f64 {
    move |f: f64| 1.0 / m::sqrt(1.0 + m::pow(fc / f.max(1e-3), (2 * order) as f64))
}
fn lowpass_mag(fc: f64, order: i32) -> impl Fn(f64) -> f64 {
    move |f: f64| 1.0 / m::sqrt(1.0 + m::pow(f.max(0.0) / fc, (2 * order) as f64))
}

/// tlib.k_weight_mag (the analytic s-domain K-weighting) and max_short_term_lufs.
fn k_weight_mag(f: f64) -> f64 {
    let f = f.max(1e-3);
    let (fc, q) = (38.13547087602444, 0.5003270373238773);
    let s = C::new(0.0, f / fc);
    let s2 = s.mul(s);
    let den = s2.add(s.scale(1.0 / q)).add(C::new(1.0, 0.0));
    let hp = cdiv(s2, den);
    let g = m::pow(10.0, 3.999843853973347 / 20.0);
    let (f0, q2) = (1681.974450955533, 0.7071752369554196);
    let t = C::new(0.0, f / f0);
    let t2 = t.mul(t);
    let num = t2.scale(g).add(t.scale(m::sqrt(g) / q2)).add(C::new(1.0, 0.0));
    let den2 = t2.add(t.scale(1.0 / q2)).add(C::new(1.0, 0.0));
    let sh = cdiv(num, den2);
    let z = hp.mul(sh);
    m::sqrt(z.re * z.re + z.im * z.im)
}
fn cdiv(a: C, b: C) -> C {
    let d = b.re * b.re + b.im * b.im;
    C::new((a.re * b.re + a.im * b.im) / d, (a.im * b.re - a.re * b.im) / d)
}

pub fn max_short_term_lufs(x: &[f64]) -> f64 {
    let n = x.len();
    let big = crate::dsp::next_pow2(n + 1024);
    let f = rfftfreq(big);
    let xf = rfft(x, big);
    let prod: Vec<C> = xf.iter().zip(&f).map(|(a, &fr)| a.scale(k_weight_mag(fr))).collect();
    let mut y = irfft(&prod, big);
    y.truncate(n);
    let win = 3 * SR as usize;
    let hop = SR as usize / 10;
    if y.len() < win {
        y.resize(win, 0.0);
    }
    let mut cs = vec![0.0f64; y.len() + 1];
    for (i, v) in y.iter().enumerate() {
        cs[i + 1] = cs[i] + v * v;
    }
    let mut best = f64::NEG_INFINITY;
    let mut s = 0;
    while s + win <= y.len() {
        best = best.max((cs[s + win] - cs[s]) / win as f64);
        s += hop;
    }
    -0.691 + 10.0 * m::log10(best + 1e-30)
}

/// synth_b3.oversampled_peak (4x, by FFT zero-padding: the periodic band-limited interpolation).
pub fn oversampled_peak(x: &[f64]) -> Vec<f64> {
    let n = x.len();
    let k = 4;
    let xf = rfft(x, n);
    let mut yf = vec![C::default(); n * k / 2 + 1];
    yf[..xf.len()].copy_from_slice(&xf);
    let y = irfft(&yf, n * k);
    (0..n)
        .map(|i| {
            let mut mx = 0.0f64;
            for j in 0..k {
                mx = mx.max((y[i * k + j] * k as f64).abs());
            }
            mx.max(x[i].abs())
        })
        .collect()
}

/// synth_b3.centred_min: min over [i - L, i + L], padded with 1.0.
pub fn centred_min(a: &[f64], l: usize) -> Vec<f64> {
    (0..a.len())
        .map(|i| {
            let lo = i.saturating_sub(l);
            let hi = (i + l).min(a.len() - 1);
            let mut mn = a[lo..=hi].iter().fold(f64::INFINITY, |p, v| p.min(*v));
            if i < l || i + l >= a.len() {
                mn = mn.min(1.0);
            }
            mn
        })
        .collect()
}

/// synth_b3.smooth_tp_limit: (y, max gain reduction dB).
pub fn smooth_tp_limit(x: &[f64], ceil_db: f64, look_ms: f64, rel_ms: f64) -> (Vec<f64>, f64) {
    let ceil = m::pow(10.0, ceil_db / 20.0);
    let l = 2usize.max((look_ms * SR / 1000.0) as usize);
    let op = oversampled_peak(x);
    let req: Vec<f64> = op.iter().map(|p| (ceil / p.max(1e-12)).min(1.0)).collect();
    let mn = centred_min(&req, l);
    let hw = hanning(l + 2);
    let h: Vec<f64> = hw[1..hw.len() - 1].to_vec();
    let hs: f64 = np_sum(&h);
    let hn: Vec<f64> = h.iter().map(|v| v / hs).collect();
    let mut mp = vec![mn[0]; l];
    mp.extend_from_slice(&mn);
    mp.extend(std::iter::repeat(*mn.last().unwrap()).take(l));
    let g = convolve_same(&mp, &hn);
    let g: Vec<f64> = g[l..l + mn.len()].iter().zip(&req).map(|(a, b)| a.min(*b)).collect();
    let a = m::exp(-1.0 / (rel_ms / 1000.0 * SR));
    let mut s = 1.0f64;
    let mut out = vec![0.0; g.len()];
    for (i, &v) in g.iter().enumerate() {
        s = if v < s { v } else { s + (v - s) * (1.0 - a) };
        out[i] = s;
    }
    let gmin = out.iter().fold(1.0f64, |p, v| p.min(*v));
    (x.iter().zip(&out).map(|(a, b)| a * b).collect(), -20.0 * m::log10(gmin))
}

/// synth_b3.normalise_tp.
pub fn normalise_tp(x: &[f64], target: f64, ceil_db: f64) -> (Vec<f64>, f64) {
    let mut y: Vec<f64> = {
        let g = m::pow(10.0, (target - max_short_term_lufs(x)) / 20.0);
        x.iter().map(|v| v * g).collect()
    };
    let mut z = Vec::new();
    let mut l = 0.0;
    for _ in 0..8 {
        z = smooth_tp_limit(&y, ceil_db, 2.0, 25.0).0;
        l = max_short_term_lufs(&z);
        if (l - target).abs() < 0.2 {
            break;
        }
        let g = m::pow(10.0, (target - l) / 20.0);
        y = y.iter().map(|v| v * g).collect();
    }
    (z, l)
}

/// tlib.compress (r3b: the gain Hann-smoothed over 6 ms): (y, the largest control-rate reduction dB).
#[allow(clippy::too_many_arguments)]
pub fn compress(x: &[f64], thr_db: f64, ratio: f64, att_ms: f64, rel_ms: f64, win_ms: f64, knee_db: f64, peak: bool)
    -> (Vec<f64>, f64) {
    let hop = 44usize;
    let n = x.len() / hop + 1;
    let mut p = vec![0.0f64; n];
    for (i, pv) in p.iter_mut().enumerate() {
        let fr: Vec<f64> = (0..hop).map(|j| {
            let k = i * hop + j;
            if k < x.len() { x[k] * x[k] } else { 0.0 }
        }).collect();
        *pv = if peak { fr.iter().fold(f64::NEG_INFINITY, |a, v| a.max(*v)) } else { np_mean(&fr) };
    }
    if peak {
        let q = p.clone();
        for i in 0..n {
            let nx = if i + 1 < n { q[i + 1] } else { q[n - 1] };
            p[i] = q[i].max(nx);
        }
    }
    let k = 1usize.max(win_ms as usize);
    let box_: Vec<f64> = vec![1.0 / k as f64; k];
    let p = convolve_same(&p, &box_);
    let gr: Vec<f64> = p
        .iter()
        .map(|v| {
            let over = 10.0 * m::log10(v + 1e-20) - thr_db;
            if over <= -knee_db / 2.0 {
                0.0
            } else if over >= knee_db / 2.0 {
                over * (1.0 - 1.0 / ratio)
            } else {
                let b = over + knee_db / 2.0;
                (1.0 - 1.0 / ratio) * (b * b) / (2.0 * knee_db)
            }
        })
        .collect();
    let (aa, ar) = (m::exp(-1.0 / att_ms), m::exp(-1.0 / rel_ms));
    let mut s = 0.0f64;
    let mut out = vec![0.0; n];
    for i in 0..n {
        let g = gr[i];
        s = if g > s { aa * s + (1.0 - aa) * g } else { ar * s + (1.0 - ar) * g };
        out[i] = s;
    }
    let xp: Vec<f64> = (0..n).map(|i| i as f64).collect();
    let mut grs: Vec<f64> = (0..x.len()).map(|i| npx::interp(i as f64 / hop as f64, &xp, &out)).collect();
    // COMP_SMOOTH_MS = 6 (r3b): a centred max over +-L, then a Hann window
    let l = 3usize.max((6.0 * SR / 1000.0) as usize);
    let hw = hanning(l + 2);
    let h: Vec<f64> = hw[1..hw.len() - 1].to_vec();
    let hs: f64 = np_sum(&h);
    let hn: Vec<f64> = h.iter().map(|v| v / hs).collect();
    let nn = grs.len();
    let cm: Vec<f64> = (0..nn)
        .map(|i| {
            let lo = i as isize - l as isize;
            let hi = i + l;
            let mut mx = f64::NEG_INFINITY;
            for j in lo..=hi as isize {
                let v = if j < 0 { grs[0] } else if j as usize >= nn { grs[nn - 1] } else { grs[j as usize] };
                mx = mx.max(v);
            }
            mx
        })
        .collect();
    let mut gp = vec![cm[0]; l];
    gp.extend_from_slice(&cm);
    gp.extend(std::iter::repeat(cm[nn - 1]).take(l));
    let sm = convolve_same(&gp, &hn);
    grs = sm[l..l + nn].to_vec();
    let y: Vec<f64> = x.iter().zip(&grs).map(|(v, g)| v * m::pow(10.0, -g / 20.0)).collect();
    (y, out.iter().fold(f64::NEG_INFINITY, |a, v| a.max(*v)))
}

/// tlib.decorrelate_allpass (3 stages, <= 8 ms in sparse_echoes).
fn decorrelate_allpass(x: &[f64], rng: &mut NpRng, stages: usize, max_ms: f64) -> Vec<f64> {
    let mut y = x.to_vec();
    for _ in 0..stages {
        let d = (rng.uniform(1.5, max_ms) * SR / 1000.0) as usize;
        let g = rng.uniform(0.3, 0.6) * if rng.choice_index(2) == 0 { -1.0 } else { 1.0 };
        let mut out = vec![0.0; y.len()];
        for i in 0..y.len() {
            let xd = if i >= d { y[i - d] } else { 0.0 };
            let od = if i >= d { out[i - d] } else { 0.0 };
            out[i] = -g * y[i] + xd + g * od;
        }
        y = out;
    }
    y
}

/// wxlib.psd (Welch: Hann 4096, 50 % overlap) and third_octave; v2_metrics.rumble's centroid.
fn psd(x: &[f64]) -> (Vec<f64>, Vec<f64>) {
    let n = 4096;
    let mut xx = x.to_vec();
    if xx.len() < n {
        xx.resize(n, 0.0);
    }
    let hop = n / 2;
    let w = hanning(n);
    let mut acc = vec![0.0f64; n / 2 + 1];
    let mut frames = 0usize;
    let mut s = 0;
    let mut rows: Vec<Vec<f64>> = Vec::new();
    while s + n <= xx.len() {
        let fr: Vec<f64> = (0..n).map(|i| xx[s + i] * w[i]).collect();
        let f = rfft(&fr, n);
        rows.push(f.iter().map(|z| z.re * z.re + z.im * z.im).collect());
        frames += 1;
        s += hop;
    }
    // numpy's mean over axis 0 (a 2-D reduction over the outer axis: a sequential sum over the frames)
    for (k, a) in acc.iter_mut().enumerate() {
        let mut sum = 0.0;
        for r in &rows {
            sum += r[k];
        }
        *a = sum / frames as f64;
    }
    (rfftfreq(n), acc)
}
fn third_oct() -> Vec<f64> {
    (-15..13).map(|k| 1000.0 * m::pow(2.0, k as f64 / 3.0)).collect()
}
fn third_octave(f: &[f64], p: &[f64]) -> Vec<f64> {
    third_oct()
        .iter()
        .map(|&fc| {
            let lo = fc / m::pow(2.0, 1.0 / 6.0);
            let hi = fc * m::pow(2.0, 1.0 / 6.0);
            let sel: Vec<f64> = f.iter().zip(p).filter(|(fv, _)| **fv >= lo && **fv < hi).map(|(_, pv)| *pv).collect();
            np_sum(&sel)
        })
        .collect()
}
fn rumble_centroid(y: &[f64], on_s: f64) -> f64 {
    let o = ((on_s + 1.0) * SR) as usize;
    let e = (o + (7.0 * SR) as usize).min(y.len());
    let (f, p) = psd(&y[o.min(e)..e]);
    let (mut num, mut den) = (Vec::new(), Vec::new());
    for (fv, pv) in f.iter().zip(&p) {
        if *fv >= 20.0 && *fv < 10000.0 {
            num.push(fv * pv);
            den.push(*pv);
        }
    }
    np_sum(&num) / np_sum(&den).max(1e-30)
}

// ------------------------------------------------------------------ the channel (synth_b3.macro_channel3 ...)

fn sub_rng(seed: u64, i: u64) -> NpRng {
    NpRng::new(&[seed, 7777, i])
}

struct Walker<'a> {
    rng: &'a mut NpRng,
    sig: f64,
}

impl Walker<'_> {
    fn walk(&mut self, p0: V3, d0: V3, bias: &dyn Fn(V3) -> V3, stop: &dyn Fn(V3, f64) -> bool, cap: usize, foot: bool)
        -> Vec<V3> {
        let mut pts = vec![p0];
        let (mut p, mut d) = (p0, d0);
        let mut run = 0.0;
        while !stop(p, run) && pts.len() < cap {
            let low = foot && p[2] < 500.0;
            let step = self.rng.lognormal(m::ln(if low { 12.0 } else { 25.0 }), 0.5);
            let mut b = bias(p);
            if low {
                b = sc3(b, 0.08 / 0.25);
            }
            d = add3(add3(d, normal3(self.rng, 0.0, self.sig)), b);
            d = div3(d, norm3(d));
            p = add3(p, sc3(d, step));
            run += step;
            pts.push(p);
        }
        pts
    }
}

struct Channel {
    main: Vec<V3>,
    cloud: Vec<Vec<V3>>,
    branches: Vec<Vec<V3>>,
}

fn macro_channel3(rng: &mut NpRng, seed: u64, dist: f64) -> Channel {
    let az = rng.uniform(0.0, m::TAU);
    let g0 = [dist * m::cos(az), dist * m::sin(az), 0.0];
    let _top0 = rng.uniform(1500.0, 3000.0);
    let horiz = rng.uniform(2000.0, 6000.0);
    let top = sub_rng(seed, 1).uniform(4000.0, 7000.0);
    let mut hdir = normal3(rng, 0.0, 1.0);
    hdir[2] = 0.0;
    let _hdir = div3(hdir, norm3(hdir));
    let mut w = Walker { rng, sig: 0.22 };
    let main = w.walk(g0, [0.0, 0.0, 1.0], &|_| [0.0, 0.0, 0.25], &|p, _| p[2] > top, 900, true);
    let mut cloud = Vec::new();
    let nc = w.rng.integers(2, 5);
    for _ in 0..nc {
        let mut hd = normal3(w.rng, 0.0, 1.0);
        hd[2] = 0.0;
        let hd = div3(hd, norm3(hd));
        let l = w.rng.uniform(1500.0, horiz);
        let c = w.walk(*main.last().unwrap(), hd, &|p| add3(sc3(hd, 0.25), [0.0, 0.0, 0.05 * (top + 500.0 - p[2]) / 300.0]),
            &|_, run| run > l, 900, false);
        let k = w.rng.integers((c.len() / 4) as i64, c.len() as i64) as usize;
        let mut hd2 = add3(hd, normal3(w.rng, 0.0, 0.8));
        hd2[2] = 0.0;
        let hd2 = div3(hd2, norm3(hd2));
        let l2 = w.rng.uniform(800.0, 3000.0);
        let start = c[k];
        cloud.push(c);
        cloud.push(w.walk(start, hd2, &|_| sc3(hd2, 0.25), &|_, run| run > l2, 900, false));
    }
    let mut sb = sub_rng(seed, 2);
    let mut branches: Vec<Vec<V3>> = Vec::new();
    let nb = w.rng.integers(6, 15);
    for _ in 0..nb {
        let pick_main = w.rng.random() < 0.7 || branches.is_empty();
        let (par_main, bi) = if pick_main { (true, 0) } else { (false, w.rng.integers(0, branches.len() as i64) as usize) };
        let plen = if par_main { main.len() } else { branches[bi].len() };
        let k = if par_main {
            (plen as f64 * m::pow(sb.random(), 2.0)) as usize
        } else {
            w.rng.integers((plen / 5) as i64, plen as i64) as usize
        };
        let mut dd = normal3(w.rng, 0.0, 1.0);
        dd[2] = -dd[2].abs() - 0.3;
        let dd = div3(dd, norm3(dd));
        let _l0 = w.rng.uniform(100.0, 1500.0);
        let l = sb.lognormal(m::ln(200.0), 0.7).min(1000.0);
        let start = if par_main { main[k] } else { branches[bi][k] };
        let br = w.walk(start, dd, &|_| [0.0, 0.0, -0.08], &|p, run| run > l || p[2] < 30.0, 900, false);
        branches.push(br);
    }
    Channel { main, cloud, branches }
}

fn new_ground_channel(main: &[V3], ss: &mut NpRng) -> Option<Vec<V3>> {
    let idx: Vec<usize> = (0..main.len()).filter(|&i| main[i][2] > 300.0 && main[i][2] < 2000.0).collect();
    if idx.is_empty() {
        return None;
    }
    let k = idx[ss.choice_index(idx.len())];
    let start = main[k];
    let off = ss.uniform(300.0, 3000.0);
    let a = ss.uniform(0.0, m::TAU);
    let target = [start[0] + off * m::cos(a), start[1] + off * m::sin(a), 0.0];
    let mut p = start;
    let mut pts = vec![p];
    let mut d = div3(sub3(target, p), norm3(sub3(target, p)));
    while p[2] > 0.0 && pts.len() < 900 {
        let step = ss.lognormal(m::ln(if p[2] < 500.0 { 12.0 } else { 25.0 }), 0.5);
        let tp = sub3(target, p);
        let aim = div3(tp, norm3(tp) + 1e-9);
        d = add3(add3(d, normal3(ss, 0.0, 0.22)), sc3(aim, 0.3));
        d = div3(d, norm3(d));
        p = add3(p, sc3(d, step));
        pts.push(p);
    }
    pts.reverse();
    let mut out = pts;
    out.extend_from_slice(&main[k + 1..]);
    Some(out)
}

/// synth_b.refine: the macro polyline -> meso steps -> 0.5 m pieces with micro jitter.
fn refine(rng: &mut NpRng, p: &[V3]) -> Vec<V3> {
    let (meso, meso_sigma, piece, micro) = (2.5, 0.35, 0.5, 0.08);
    let mut out = vec![p[0]];
    for w in p.windows(2) {
        let (a, b) = (w[0], w[1]);
        let seg = sub3(b, a);
        let l = norm3(seg);
        let n = 1usize.max(py_round(l / meso) as usize);
        let mut q = *out.last().unwrap();
        for i in 0..n {
            let rem = sub3(add3(a, div3(sc3(seg, (i + 1) as f64), n as f64)), q);
            let mut dirn = add3(div3(rem, norm3(rem) + 1e-9), normal3(rng, 0.0, meso_sigma));
            dirn = div3(dirn, norm3(dirn));
            q = add3(q, sc3(dirn, l / n as f64));
            out.push(q);
        }
    }
    let segl: Vec<f64> = out.windows(2).map(|w| norm3(sub3(w[1], w[0]))).collect();
    let mut s = vec![0.0f64];
    for v in &segl {
        let last = *s.last().unwrap();
        s.push(last + v);
    }
    let total = *s.last().unwrap();
    let nss = (total / piece).ceil().max(0.0) as usize;
    let cols: Vec<Vec<f64>> = (0..3).map(|k| out.iter().map(|v| v[k]).collect()).collect();
    let mut r: Vec<V3> = (0..nss)
        .map(|i| {
            let x = i as f64 * piece;
            [npx::interp(x, &s, &cols[0]), npx::interp(x, &s, &cols[1]), npx::interp(x, &s, &cols[2])]
        })
        .collect();
    for v in r.iter_mut() {
        for c in v.iter_mut() {
            *c += rng.normal(0.0, micro);
        }
    }
    r
}

/// synth_b.smooth_noise.
fn smooth_noise(rng: &mut NpRng, n: usize, corr: usize) -> Vec<f64> {
    let x: Vec<f64> = (0..n + 4 * corr).map(|_| rng.standard_normal()).collect();
    let k = hanning(2 * corr + 1);
    let ks: f64 = np_sum(&k);
    let kn: Vec<f64> = k.iter().map(|v| v / ks).collect();
    let y = convolve_same(&x, &kn);
    let y = y[2 * corr..2 * corr + n].to_vec();
    let sd = np_std(&y);
    y.iter().map(|v| v / (sd + 1e-12)).collect()
}

fn nwave_soft(t: f64, rise_s: f64) -> Vec<f64> {
    let n = 2usize.max((t * SR) as usize);
    let k: Vec<f64> = (0..n).map(|i| 1.0 - 2.0 * i as f64 / (n - 1) as f64).collect();
    let r = 1usize.max(py_round(rise_s * SR) as usize);
    if k.len() > 4 * r {
        let ls = linspace(0.0, m::PI, r + 1);
        let up: Vec<f64> = ls[1..].iter().map(|v| 0.5 - 0.5 * m::cos(*v)).collect();
        let mut out: Vec<f64> = up.iter().map(|u| u * k[0]).collect();
        out.extend_from_slice(&k);
        out.extend(up.iter().rev().map(|u| u * k[n - 1]));
        return out;
    }
    k
}

/// One piece list's contributions.
struct Contrib {
    t: Vec<f64>,
    amp: Vec<f64>,
    tn: Vec<f64>,
    z: Vec<f64>,
    width: Vec<f64>,
    r: Vec<f64>,
    shadow: Vec<f64>,
}

/// synth_b3.render_dry3: (dry, the micro-shock layer, rr.min()).
pub fn render_dry3(rng: &mut NpRng, seed: u64, dist: f64, length_s: f64, st: &Settings) -> (Vec<f64>, Vec<f64>, f64) {
    let ch = macro_channel3(rng, seed, dist);
    let _n_strokes0 = rng.integers(2, 6);
    let mut ss = sub_rng(seed, 3);
    let n_strokes = if ss.random() < 0.15 { 1 } else { 1 + ss.poisson_small(3.5).min(11) } as usize;
    let mut newch_k = None;
    let mut newch = None;
    if n_strokes > 1 && ss.random() < 0.4 {
        newch_k = Some(ss.integers(1, n_strokes as i64) as usize);
        newch = new_ground_channel(&ch.main, &mut ss);
    }
    let mut t_stroke = 0.0;
    let mut contribs: Vec<Contrib> = Vec::new();
    let wv = [rng.normal(0.0, 1.0), rng.normal(0.0, 1.0)];
    let wn = m::sqrt(wv[0] * wv[0] + wv[1] * wv[1]);
    let ws = rng.uniform(2.0, 10.0);
    let wind = [wv[0] / wn * ws, wv[1] / wn * ws];
    let r_curv = sub_rng(seed, 4).uniform(55000.0, 80000.0);
    for k in 0..n_strokes {
        let mut e_stroke = if k == 0 { 1.0 } else { rng.uniform(0.3, 0.9) };
        if k > 0 {
            e_stroke = ss.lognormal(m::ln(0.42), 0.5).min(1.3);
        }
        let trunk: &Vec<V3> = if newch_k == Some(k) && newch.is_some() { newch.as_ref().unwrap() } else { &ch.main };
        let mut parts: Vec<(&Vec<V3>, u8)> = vec![(trunk, 0)];
        for c in &ch.cloud {
            if k == 0 || rng.random() < 0.5 {
                parts.push((c, 1));
            }
        }
        if k == 0 {
            for b in &ch.branches {
                parts.push((b, 2));
            }
        }
        let r0_scale = if k > 0 { ss.uniform(0.85, 0.95) } else { 1.0 };
        for (pp, kind) in parts {
            let pw: Vec<V3> = pp
                .iter()
                .map(|p| {
                    let c = (p[2] / 1500.0).clamp(0.0, 1.0);
                    [p[0] + wind[0] * t_stroke * c, p[1] + wind[1] * t_stroke * c, p[2]]
                })
                .collect();
            let rr = refine(rng, &pw);
            // (a part of one point: no pieces, but its draws are still made, as numpy's empty arrays do)
            let nm = rr.len().saturating_sub(1);
            let mid: Vec<V3> = (0..nm).map(|i| sc3(add3(rr[i + 1], rr[i]), 0.5)).collect();
            let r: Vec<f64> = mid.iter().map(|p| norm3(sub3(*p, OBS))).collect();
            let width: Vec<f64> = (0..nm)
                .map(|i| (norm3(sub3(rr[i + 1], OBS)) - norm3(sub3(rr[i], OBS))).abs() / SOUND)
                .collect();
            let corr = rng.uniform(10.0, 60.0) as usize;
            let sn = smooth_noise(rng, nm, corr);
            let fld: Vec<f64> = sn.iter().map(|v| m::exp(0.7 * v - 0.245)).collect();
            let wv: Vec<f64> = match kind {
                1 => vec![0.15; nm],
                2 => linspace(0.25, 0.1, nm),
                _ => vec![1.0; nm],
            };
            let w: Vec<f64> = wv.iter().zip(&mid).map(|(a, p)| a * m::exp(-p[2].max(0.0) / 2500.0)).collect();
            let u = rng.uniform(0.9, 2.0);
            let sn2 = smooth_noise(rng, nm, 200);
            let r0: Vec<f64> = sn2.iter().map(|v| (u * (1.0 + 0.25 * v) * r0_scale).max(0.3)).collect();
            let jit: Vec<f64> = (0..nm).map(|_| m::exp(rng.normal(0.0, 0.18))).collect();
            let g = |rr_: f64, r0v: f64| (1.0 + 0.5 * m::ln(rr_.max(1.0) / (10.0 * r0v))).max(1.0);
            let tn: Vec<f64> = (0..nm)
                .map(|i| (1.05 * r0[i] / SOUND * m::sqrt(g(r[i], r0[i]) / g(1000.0, r0[i]))).clamp(0.0012, 0.02) * jit[i])
                .collect();
            // (numpy's x ** -1.0 is its reciprocal fast path: 1 / x)
            let amp: Vec<f64> = (0..nm).map(|i| e_stroke * w[i] * fld[i] * (1.0 / (r[i].max(30.0) / dist))).collect();
            let mut tarr: Vec<f64> = r.iter().map(|v| t_stroke + v / SOUND).collect();
            let shadow: Vec<f64> = mid
                .iter()
                .map(|p| {
                    let d = libm::hypot(p[0], p[1]);
                    let hs = d * d / (2.0 * r_curv);
                    ((hs - p[2]) / hs.max(1e-6)).clamp(0.0, 1.0)
                })
                .collect();
            let mut sj = NpRng::new(&[seed, 8888, k as u64, nm as u64]);
            let corr2 = 200usize;
            let xx: Vec<f64> = (0..nm + 4 * corr2).map(|_| sj.standard_normal()).collect();
            let kern = hanning(2 * corr2 + 1);
            let ks: f64 = np_sum(&kern);
            let kn: Vec<f64> = kern.iter().map(|v| v / ks).collect();
            let jt = convolve_same(&xx, &kn);
            let jt = jt[2 * corr2..2 * corr2 + nm].to_vec();
            let sd = np_std(&jt);
            for i in 0..nm {
                tarr[i] += jt[i] / (sd + 1e-12) * 0.0005 * m::sqrt(r[i] / 1000.0);
            }
            contribs.push(Contrib { t: tarr, amp, tn, z: mid.iter().map(|p| p[2]).collect(), width, r, shadow });
        }
        t_stroke += (m::pow(10.0, m::log10(0.065) + 0.36 * ss.standard_normal())).clamp(0.003, 0.5);
    }
    // the pieces, binned by distance x N-wave length x shadow depth
    let cat = |f: &dyn Fn(&Contrib) -> &Vec<f64>| -> Vec<f64> { contribs.iter().flat_map(|c| f(c).iter().copied()).collect() };
    let tt0 = cat(&|c| &c.t);
    let t0 = tt0.iter().fold(f64::INFINITY, |a, v| a.min(*v));
    let keep: Vec<usize> = (0..tt0.len()).filter(|&i| tt0[i] - t0 < length_s - 0.5).collect();
    let pick = |v: Vec<f64>| -> Vec<f64> { keep.iter().map(|&i| v[i]).collect() };
    let tt: Vec<f64> = keep.iter().map(|&i| tt0[i] - t0).collect();
    let amp = pick(cat(&|c| &c.amp));
    let tn = pick(cat(&|c| &c.tn));
    let z = pick(cat(&|c| &c.z));
    let wd = pick(cat(&|c| &c.width));
    let rr = pick(cat(&|c| &c.r));
    let sh = pick(cat(&|c| &c.shadow));
    let n = (length_s * SR) as usize;
    let mut out = vec![0.0f64; n];
    let mut micro_out = vec![0.0f64; n];
    let rmin = rr.iter().fold(f64::INFINITY, |a, v| a.min(*v));
    let key: Vec<i64> = (0..tt.len())
        .map(|i| {
            let rb = (m::ln(rr[i].max(30.0)) / m::ln(1.04)).floor() as i64;
            let tb = (m::ln(tn[i] / 0.0012) / m::ln(1.3)).floor() as i64;
            let sb = py_round(sh[i] * 4.0) as i64;
            (rb * 64 + tb) * 8 + sb
        })
        .collect();
    let gdelay: Vec<f64> = (0..tt.len()).map(|i| 2.0 * OBS[2] * z[i].max(0.0) / rr[i].max(1.0) / SOUND).collect();
    let mut keys: Vec<i64> = key.clone();
    keys.sort();
    keys.dedup();
    for kk in keys {
        let sel: Vec<usize> = (0..tt.len()).filter(|&i| key[i] == kk).collect();
        let tsel: Vec<f64> = sel.iter().map(|&i| tt[i]).collect();
        let tmin = tsel.iter().fold(f64::INFINITY, |a, v| a.min(*v));
        let tmax = tsel.iter().fold(f64::NEG_INFINITY, |a, v| a.max(*v));
        let wmax = sel.iter().map(|&i| wd[i]).fold(f64::NEG_INFINITY, f64::max);
        let i0 = (tmin * SR) as usize;
        let i1 = ((tmax + wmax) * SR) as usize + 2;
        let l = i1 - i0 + 1;
        let mut buf = vec![0.0f64; l + 2];
        let glen = l + (0.03 * SR) as usize + 2;
        let mut gbuf = vec![0.0f64; glen];
        let mut gbuf2 = vec![0.0f64; glen];
        let mut jr = NpRng::new(&[(sel.len() * 7919 + i0) as u64]);
        let jit: Vec<f64> = (0..sel.len()).map(|_| jr.uniform(0.0002, 0.002)).collect();
        for (bi, gain) in [(0usize, 1.0f64), (1, 0.85), (2, 0.85)] {
            let b: &mut Vec<f64> = match bi {
                0 => &mut buf,
                1 => &mut gbuf,
                _ => &mut gbuf2,
            };
            let bl = b.len() as i64;
            for (j, &i) in sel.iter().enumerate() {
                let extra = match bi {
                    0 => 0.0,
                    1 => gdelay[i],
                    _ => gdelay[i] + jit[j],
                };
                let s = (tsel[j] - i0 as f64 / SR + extra) * SR;
                let wsm = (wd[i] * SR).max(1.0);
                let e = s + wsm;
                let a = amp[i] * gain / wsm;
                let si = (s as i64).clamp(0, bl - 1) as usize;
                let ei = (e as i64).clamp(0, bl - 1) as usize;
                b[si] += a;
                b[ei] -= a;
            }
            let mut acc = 0.0;
            for v in b.iter_mut() {
                acc += *v;
                *v = acc;
            }
        }
        let tm = median(&sel.iter().map(|&i| tn[i]).collect::<Vec<_>>());
        let rm = median(&sel.iter().map(|&i| rr[i]).collect::<Vec<_>>());
        let smean = np_mean(&sel.iter().map(|&i| sh[i]).collect::<Vec<_>>());
        let kern = nwave_soft(tm, st.rise_s);
        let ab = absorb_mag(rm);
        let sig = convolve_full(&buf, &kern);
        let ga = fft_filter(&convolve_full(&gbuf, &kern), &|f| 0.85 / m::sqrt(1.0 + m::pow(f / 300.0, 4.0)));
        let g2 = fft_filter(&convolve_full(&gbuf2, &kern), &|f| 0.5 * (1.0 - 1.0 / m::sqrt(1.0 + m::pow(f / 300.0, 4.0))));
        let gl = ga.len().min(g2.len());
        let gsig: Vec<f64> = (0..gl).map(|i| ga[i] + g2[i]).collect();
        if st.micro_w > 0.0 {
            let mut mr = NpRng::new(&[seed, 5151, i0 as u64, sel.len() as u64]);
            let mut hb = vec![0.0f64; sig.len()];
            let pos: Vec<f64> =
                tsel.iter().map(|tv| ((tv - i0 as f64 / SR) + mr.uniform(-5e-5, 5e-5)) * SR).collect();
            let signs: Vec<f64> = (0..sel.len()).map(|_| if mr.choice_index(2) == 0 { -1.0 } else { 1.0 }).collect();
            let hl = hb.len() as i64;
            for (j, &i) in sel.iter().enumerate() {
                let wr = m::exp(-(rr[i] - rmin) / 400.0);
                let pi = (pos[j] as i64).clamp(0, hl - 1) as usize;
                hb[pi] += amp[i] * wr * signs[j];
            }
            let sg = 0.00004 * SR;
            let lim = (4.0 * sg) as i64;
            let kx: Vec<f64> = (-lim - 1..lim + 2).map(|v| v as f64).collect();
            let dk: Vec<f64> = kx.iter().map(|v| -v * m::exp(-0.5 * (v / sg) * (v / sg))).collect();
            let dn = m::sqrt(np_sum(&dk.iter().map(|v| v * v).collect::<Vec<_>>()));
            let dk: Vec<f64> = dk.iter().map(|v| v / dn).collect();
            let conv = convolve_same(&hb, &dk);
            let mut padded = conv;
            padded.extend(std::iter::repeat(0.0).take((0.05 * SR) as usize));
            let hfs = min_phase_floor(&padded, &ab);
            let e2 = n.min(i0 + hfs.len());
            for i in i0..e2 {
                micro_out[i] += hfs[i - i0];
            }
        }
        let l2 = sig.len().max(gsig.len()) + (0.3 * SR) as usize;
        let mut tot = vec![0.0f64; l2];
        for (i, v) in sig.iter().enumerate() {
            tot[i] += v;
        }
        for (i, v) in gsig.iter().enumerate() {
            tot[i] += v;
        }
        let tot = if smean > 0.01 {
            let ab2 = absorb_mag(rm);
            min_phase_floor(&tot, &move |f: f64| {
                ab2(f) * m::pow(10.0, -smean * (10.0 + 15.0 * (m::log2(f.max(1.0) / 100.0) / 5.0).clamp(0.0, 1.0)) / 20.0)
            })
        } else {
            min_phase_floor(&tot, &ab)
        };
        let e_ = n.min(i0 + l2);
        for i in i0..e_ {
            out[i] += tot[i - i0];
        }
    }
    (out, micro_out, rmin)
}

/// synth_b.corpus_eq with the class median (the mod: min_phase_floor).
fn corpus_eq(x: &[f64], onset: usize, eq: &[(f64, f64)]) -> Vec<f64> {
    let e = (onset + 8 * SR as usize).min(x.len());
    let (f, p) = psd(&x[onset..e]);
    let to = third_octave(&f, &p);
    let toc = third_oct();
    let tot: f64 = np_sum(&toc.iter().zip(&to).filter(|(fc, _)| **fc >= 31.0 && **fc <= 10000.0).map(|(_, v)| *v).collect::<Vec<_>>());
    let mut fcs = Vec::new();
    let mut gains = Vec::new();
    for (fc, v) in toc.iter().zip(&to) {
        if let Some(&(_, med)) = eq.iter().find(|(c, _)| (c - fc).abs() < 1e-9 * fc) {
            fcs.push(*fc);
            gains.push(med - 10.0 * m::log10(v / tot + 1e-30));
        }
    }
    let ng = gains.len();
    let mut padg = vec![gains[0]];
    padg.extend_from_slice(&gains);
    padg.push(gains[ng - 1]);
    let g: Vec<f64> = (0..ng).map(|i| padg[i] * 0.25 + padg[i + 1] * 0.5 + padg[i + 2] * 0.25).collect();
    let med = median(&g);
    let g: Vec<f64> = g.iter().map(|v| (v - med).clamp(-18.0, 18.0)).collect();
    let lf: Vec<f64> = fcs.iter().map(|v| m::log2(*v)).collect();
    min_phase_floor(x, &move |fr: f64| m::pow(10.0, npx::interp(m::log2(fr.max(1.0)), &lf, &g) / 20.0))
}

fn peak5(z: &[f64]) -> f64 {
    let h = (0.005 * SR) as usize;
    let mm = z.len() / h;
    if mm == 0 {
        return 1e-12;
    }
    (0..mm)
        .map(|i| m::sqrt(np_mean(&z[i * h..(i + 1) * h].iter().map(|v| v * v).collect::<Vec<_>>())))
        .fold(f64::NEG_INFINITY, f64::max)
}

/// synth_b.sparse_echoes.
fn sparse_echoes(rng: &mut NpRng, dry: &[f64]) -> Vec<f64> {
    let mut out = vec![0.0f64; dry.len()];
    let ne = rng.integers(2, 6) as usize;
    let mut delays: Vec<f64> = (0..ne).map(|_| rng.uniform(0.35, 3.5)).collect();
    delays.sort_by(|a, b| a.partial_cmp(b).unwrap());
    for d in delays {
        let extra = d * SOUND;
        let fc = rng.uniform(600.0, 2000.0);
        let g = m::pow(10.0, (rng.uniform(-16.0, -9.0) - 3.0 * d) / 20.0);
        let ab = absorb_mag(extra);
        let lp = lowpass_mag(fc, 1);
        let e = min_phase_floor(dry, &move |f| ab(f) * lp(f));
        let e = decorrelate_allpass(&e, rng, 3, 8.0);
        let i = (d * SR) as usize;
        for j in i..out.len() {
            out[j] += g * e[j - i];
        }
    }
    out
}

/// The far tilt (render3b's tilt_lp): a smooth dB/octave tilt about 250 Hz to the rumble centroid target.
fn rumble_tilt_lp(x: &[f64], o: usize, target: f64, lp: f64) -> Vec<f64> {
    let tilted = |k: f64| -> Vec<f64> {
        min_phase_floor(x, &move |f: f64| {
            m::pow(10.0, k * m::log2(f.clamp(40.0, lp) / 250.0) / 20.0) * (1.0 / m::sqrt(1.0 + m::pow(f / (2.0 * lp), 4.0)))
        })
    };
    let cen = |y: &[f64]| rumble_centroid(y, o as f64 / SR);
    let (mut k0, mut c0) = (0.0, cen(x));
    let mut k1 = if c0 < target { 3.0 } else { -3.0 };
    let mut y1 = tilted(k1);
    let mut c1 = cen(&y1);
    for _ in 0..4 {
        if (c1 - target).abs() < 8.0 || c1 == c0 {
            break;
        }
        let k2 = (k1 + (target - c1) * (k1 - k0) / (c1 - c0)).clamp(-6.0, 12.0);
        k0 = k1;
        c0 = c1;
        k1 = k2;
        y1 = tilted(k2);
        c1 = cen(&y1);
    }
    y1
}

/// Stages kept for the check against the Python reference.
#[derive(Default)]
pub struct Stages {
    pub dry: Vec<f64>,
    pub micro: Vec<f64>,
    pub eq: Vec<f64>,
    pub comp: Vec<f64>,
    pub echoes: Vec<f64>,
    pub hp: Vec<f64>,
    pub level: Vec<f64>,
    pub fin: Vec<f64>,
}

/// One method B strike (offline: the file's 14 s with the 0.5 s lead), as render3c / render3b do it.
pub fn render(seed: u64, dist: f64, st: &Settings, keep: bool) -> (Vec<f64>, Stages) {
    let mut stages = Stages::default();
    let mut rng = NpRng::new(&[seed]);
    let (dry, micro, _rmin) = render_dry3(&mut rng, seed, dist, LENGTH_S - PRE_S, st);
    let n = (LENGTH_S * SR) as usize;
    let o = (PRE_S * SR) as usize;
    let mut x = vec![0.0f64; n];
    x[o..o + dry.len()].copy_from_slice(&dry);
    // (the leader tear of render3b: none for these classes; render3c draws none)
    let mut y = corpus_eq(&x, o, st.eq);
    if st.micro_w > 0.0 {
        let mut hf = vec![0.0f64; y.len()];
        let take = micro.len().min(y.len() - o);
        hf[o..o + take].copy_from_slice(&micro[..take]);
        let w = (0.05 * SR) as usize;
        let g = st.micro_w * peak5(&y[o..o + w]) / (peak5(&hf[o..o + w]) + 1e-12);
        for (a, b) in y.iter_mut().zip(&hf) {
            *a += b * g;
        }
    }
    if keep {
        stages.dry = dry;
        stages.micro = micro;
        stages.eq = y.clone();
    }
    let seg: Vec<f64> = y[o + SR as usize..o + 4 * SR as usize].iter().map(|v| v * v).collect();
    let roll = 10.0 * m::log10(np_mean(&seg) + 1e-20);
    let (x1, _) = compress(&y, roll + 14.0, 6.0, 0.5, 120.0, 2.0, 6.0, false);
    let (x2, _) = compress(&x1, roll + 21.0, 8.0, 0.3, 60.0, 1.0, 8.0, true);
    let (mut x3, _) = compress(&x2, roll + 10.0, 4.0, 2.0, 350.0, 10.0, 6.0, false);
    // (fixed with fade_db_s 0: no fade)
    if let (Some(lp), Some(target)) = (st.tilt_lp, st.rumble_target) {
        x3 = rumble_tilt_lp(&x3, o, target, lp);
    }
    if keep {
        stages.comp = x3.clone();
    }
    let ech = sparse_echoes(&mut rng, &x3);
    let x4: Vec<f64> = x3.iter().zip(&ech).map(|(a, b)| a + b).collect();
    if keep {
        stages.echoes = x4.clone();
    }
    let hp = highpass_mag(28.0, 2);
    let mut x5 = min_phase_floor(&x4, &hp);
    let fo = (3.0 * SR) as usize;
    let ls = linspace(0.0, m::PI / 2.0, fo);
    let s0 = x5.len() - fo;
    for i in 0..fo {
        let c = m::cos(ls[i]);
        x5[s0 + i] *= c * c;
    }
    if keep {
        stages.hp = x5.clone();
    }
    let (mut z, _l) = normalise_tp(&x5, st.target_lufs, -1.5);
    if keep {
        stages.level = z.clone();
    }
    if st.quiet_tear && dist <= 600.0 {
        z = quiet_tear(&z, seed);
    }
    if keep {
        stages.fin = z.clone();
    }
    (z, stages)
}

/// render3c's quiet tear, after the level stage.
fn quiet_tear(y: &[f64], seed: u64) -> Vec<f64> {
    let o = (0.5 * SR) as usize;
    let mut r = NpRng::new(&[seed, 4242]);
    let dur = r.uniform(0.01, 0.04);
    let n = ((dur + 0.015) * SR) as usize;
    let mut burst = vec![0.0f64; n];
    let count = r.uniform(15.0, 40.0) as usize;
    for _ in 0..count {
        let l = (r.uniform(0.002, 0.004) * SR) as usize;
        let i = (m::pow(r.uniform(0.0, dur), 1.5) / m::pow(dur, 0.5) * SR) as usize;
        if i + l < n {
            let hw = hanning(l);
            for j in 0..l {
                burst[i + j] += r.standard_normal() * hw[j];
            }
        }
    }
    let hpm = highpass_mag(2500.0, 2);
    let lpm = lowpass_mag(9000.0, 2);
    let mut burst = fft_filter(&burst, &move |f| hpm(f) * lpm(f));
    let nd = (dur * SR) as usize;
    let mut env = vec![1.0f64; n];
    let a = linspace(0.3, 1.0, nd);
    env[..nd].copy_from_slice(&a);
    let b = linspace(0.0, m::PI, n - nd);
    for (i, v) in b.iter().enumerate() {
        env[nd + i] = 0.5 + 0.5 * m::cos(*v);
    }
    for (v, e) in burst.iter_mut().zip(&env) {
        *v *= e;
    }
    let w = (0.05 * SR) as usize;
    let gt = peak5(&y[o..o + w]) * m::pow(10.0, -22.0 / 20.0) / (peak5(&burst) + 1e-12);
    let mut out = y.to_vec();
    for i in 0..n {
        out[o - nd + i] += burst[i] * gt;
    }
    smooth_tp_limit(&out, -1.5, 2.0, 25.0).0
}
