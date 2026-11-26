//! The thunder grain bank's builder, bank v3 ("recording mix" mode): plan the cuts in the CC0 originals, then cut,
//! clean and level each grain. The behaviour is ~/bnge-tests/weather/procedural/grainbank/v3/SPEC.md sections 1-5,
//! ported from its research reference (grainbank/tools/build_bank.py with BANK=v3, gbdsp.py, and the screening of
//! compare/tools/wxlib.py). The port matches the reference statistically (SPEC section 9), not bit for bit.
//!
//! Deterministic in its own right: libm for every transcendental function, sums in a fixed order, sources and grains in
//! the input files' order (no hash-map iteration anywhere), and worker threads that only ever split independent
//! sources, their results put back in source order.
//!
//!   weathersynth grainbank-build plan --gb DIR [--plan OUT.json] [--threads N] [--verbose]
//!   weathersynth grainbank-build cut  --gb DIR [--plan PLAN.json] --out BANKDIR [--threads N]
//!   weathersynth grainbank-compare --ref MANIFEST --new MANIFEST
//!
//! DIR holds sources.json, originals/fs<ID>.* and originals.sha256 (every original is checked against it before ffmpeg
//! decodes it). The plan is written to DIR/plan_v3.json unless --plan says otherwise; the cut writes BANKDIR/grains/
//! <id>.ogg (Vorbis q5, ffmpeg) and BANKDIR/manifest.json in the reference's schema.

use crate::dsp::{self, C};
use crate::m;
use crate::npx;
use crate::proto::Json;
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::rc::Rc;

pub const SR: f64 = 44100.0;
const SRI: i64 = 44100;
const REF_RMS_DB: f64 = -18.0;
const PEAKY_DB: f64 = 14.0;
const DRY_DB: f64 = -62.0;
const KINDS: [&str; 7] = ["body", "crack", "clap", "roll", "mid_body", "mid_roll", "far_roll"];
/// (budget s, cap per source) in KINDS order (SPEC section 4)
const BUDGET: [(f64, usize); 7] = [
    (66.0, 2),
    (10.0, 2),
    (45.0, 3),
    (30.0, 2),
    (21.0, 2),
    (24.0, 2),
    (36.0, 3),
];
/// kind: (pre s, post s, fade-in ms, fade-out ms) in KINDS order (SPEC section 3)
const CUT: [(f64, f64, i64, i64); 7] = [
    (0.010, 2.5, 2, 700),
    (0.010, 0.35, 2, 120),
    (0.06, 0.9, 15, 450),
    (0.0, 3.0, 300, 600),
    (0.02, 3.5, 5, 800),
    (0.0, 3.0, 300, 600),
    (0.2, 3.3, 400, 700),
];

fn kind_index(k: &str) -> usize {
    KINDS.iter().position(|v| *v == k).expect("known kind")
}

fn contam_max(cls: &str) -> f64 {
    match cls {
        "thunder_strike" | "thunder_close" => -55.0,
        _ => -50.0,
    }
}

fn thunder_snr(cls: &str) -> f64 {
    match cls {
        "thunder_strike" | "thunder_close" => 20.0,
        "thunder_mid" => 15.0,
        "thunder_far" => 12.0,
        "thunder_any" => 15.0,
        _ => 12.0,
    }
}

// ---------------------------------------------------------------- numpy-like helpers

/// Python's int() of a float: truncation toward zero. Every sample index is computed with the reference's own
/// expression (e.g. 0.9 * SR) so the product rounds the same way.
#[inline]
fn ti(x: f64) -> i64 {
    x as i64
}

/// x[a:b] with both ends clamped to the array (the reference guards every negative start with max(0, ...)).
fn sl(x: &[f64], a: i64, b: i64) -> &[f64] {
    let n = x.len() as i64;
    let a = a.clamp(0, n) as usize;
    let b = b.clamp(0, n) as usize;
    if b <= a {
        &x[0..0]
    } else {
        &x[a..b]
    }
}

fn sumsq(x: &[f64]) -> f64 {
    let mut s = 0.0;
    for v in x {
        s += v * v;
    }
    s
}

fn mean(x: &[f64]) -> f64 {
    let mut s = 0.0;
    for v in x {
        s += v;
    }
    s / x.len() as f64
}

pub(crate) fn mean_sq(x: &[f64]) -> f64 {
    sumsq(x) / x.len() as f64
}

/// 20 log10(rms + 1e-12), the reference's level of a span.
fn rms_db(x: &[f64]) -> f64 {
    20.0 * m::log10(m::sqrt(mean_sq(x)) + 1e-12)
}

/// wxlib.db: 10 log10(max(p, 1e-20)).
pub(crate) fn db10(p: f64) -> f64 {
    10.0 * m::log10(p.max(1e-20))
}

fn vmax(x: &[f64]) -> f64 {
    x.iter().fold(f64::NEG_INFINITY, |a, &v| if v > a { v } else { a })
}

fn vmin(x: &[f64]) -> f64 {
    x.iter().fold(f64::INFINITY, |a, &v| if v < a { v } else { a })
}

fn absmax(x: &[f64]) -> f64 {
    x.iter().fold(0.0f64, |a, &v| a.max(v.abs()))
}

/// numpy.argmax: the first maximum.
fn argmax(x: &[f64]) -> usize {
    let mut k = 0;
    for (i, &v) in x.iter().enumerate() {
        if v > x[k] {
            k = i;
        }
    }
    k
}

fn argmin(x: &[f64]) -> usize {
    let mut k = 0;
    for (i, &v) in x.iter().enumerate() {
        if v < x[k] {
            k = i;
        }
    }
    k
}

/// numpy.percentile, the default 'linear' method (numpy's virtual index and its two-sided lerp).
pub fn percentile(v: &[f64], q: f64) -> f64 {
    let mut s = v.to_vec();
    s.sort_by(|a, b| a.total_cmp(b));
    let n = s.len();
    let qq = q / 100.0;
    let vi = n as f64 * qq + (1.0 + qq * (1.0 - 1.0 - 1.0)) - 1.0;
    let lo = vi.floor();
    let g = vi - lo;
    let lo = (lo.max(0.0) as usize).min(n - 1);
    let hi = (lo + 1).min(n - 1);
    let (a, b) = (s[lo], s[hi]);
    let d = b - a;
    if g >= 0.5 {
        b - d * (1.0 - g)
    } else {
        a + d * g
    }
}

/// numpy.fft.rfftfreq(n, 1 / SR), computed as numpy does (k * (1 / (n * d))): the band masks fall on the same bins.
fn rfftfreq(n: usize) -> Vec<f64> {
    let val = 1.0 / (n as f64 * (1.0 / SR));
    (0..=n / 2).map(|k| k as f64 * val).collect()
}

/// numpy.arange for float arguments: ceil((stop - start) / step) values start + i * ((start + step) - start).
fn arange(start: f64, stop: f64, step: f64) -> Vec<f64> {
    let n = ((stop - start) / step).ceil();
    if !(n > 0.0) {
        return Vec::new();
    }
    let delta = (start + step) - start;
    (0..n as usize).map(|i| start + i as f64 * delta).collect()
}

/// numpy.linspace(start, stop, num) (endpoint included).
fn linspace(start: f64, stop: f64, num: usize) -> Vec<f64> {
    if num == 0 {
        return Vec::new();
    }
    if num == 1 {
        return vec![start];
    }
    let step = (stop - start) / (num - 1) as f64;
    let mut y: Vec<f64> = (0..num).map(|i| i as f64 * step + start).collect();
    y[num - 1] = stop;
    y
}

fn cabs2(c: C) -> f64 {
    c.re * c.re + c.im * c.im
}

/// wxlib.running_median: medians on a grid every max(1, k / 8) samples, interpolated.
fn running_median(v: &[f64], k: usize) -> Vec<f64> {
    let step = (k / 8).max(1);
    let mut cs = Vec::new();
    let mut meds = Vec::new();
    let mut c = 0;
    while c < v.len() {
        let a = c.saturating_sub(k / 2);
        let b = (c + k / 2 + 1).min(v.len());
        cs.push(c as f64);
        meds.push(npx::median(&v[a..b]));
        c += step;
    }
    (0..v.len()).map(|i| npx::interp(i as f64, &cs, &meds)).collect()
}

// ---------------------------------------------------------------- gbdsp.py

/// Decode any audio file to mono f64 at 44.1 kHz: ffmpeg -ac 1 -ar 44100, 32-bit float (the reference's gbdsp.load).
pub fn decode(path: &Path) -> Result<Vec<f64>, String> {
    let o = Command::new("ffmpeg")
        .args(["-nostdin", "-v", "error", "-i"])
        .arg(path)
        .args(["-ac", "1", "-ar", "44100", "-f", "f32le", "-"])
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("ffmpeg: {e}"))?;
    if !o.status.success() {
        return Err(format!(
            "ffmpeg could not decode {}: {}",
            path.display(),
            String::from_utf8_lossy(&o.stderr)
        ));
    }
    Ok(o.stdout
        .chunks_exact(4)
        .map(|p| f32::from_le_bytes([p[0], p[1], p[2], p[3]]) as f64)
        .collect())
}

/// Vorbis q5 through ffmpeg (bit-exact container: no encoder version tag, no metadata).
pub(crate) fn write_ogg(path: &Path, x: &[f64]) -> Result<(), String> {
    let raw: Vec<u8> = x.iter().flat_map(|v| (*v as f32).to_le_bytes()).collect();
    let mut ch = Command::new("ffmpeg")
        .args([
            "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", "44100", "-ac", "1", "-i", "-",
        ])
        .args([
            "-c:a",
            "libvorbis",
            "-q:a",
            "5",
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
        ])
        .arg(path)
        .stdin(Stdio::piped())
        .spawn()
        .map_err(|e| format!("ffmpeg: {e}"))?;
    ch.stdin
        .take()
        .ok_or("ffmpeg stdin")?
        .write_all(&raw)
        .map_err(|e| format!("ffmpeg: {e}"))?;
    let st = ch.wait().map_err(|e| format!("ffmpeg: {e}"))?;
    if !st.success() {
        return Err(format!("ffmpeg could not write {}", path.display()));
    }
    Ok(())
}

const NF: usize = 2048;
const HOP: usize = 256;
const NB: usize = NF / 2 + 1;

thread_local! {
    static HANN: Rc<Vec<f64>> = Rc::new(npx::hanning(NF));
}

/// STFT: 2048-point Hann frames at a 256 hop over x zero-padded by 2048 on both sides. Frames flat, NB bins each.
fn stft(x: &[f64]) -> (Vec<C>, usize) {
    let w = HANN.with(|h| h.clone());
    let plen = x.len() + 2 * NF;
    let nfr = (plen - NF) / HOP + 1;
    let mut out = Vec::with_capacity(nfr * NB);
    let mut fr = vec![0.0; NF];
    for i in 0..nfr {
        let s = i * HOP;
        for j in 0..NF {
            let p = s + j;
            let v = if p >= NF && p < NF + x.len() { x[p - NF] } else { 0.0 };
            fr[j] = v * w[j];
        }
        out.extend(npx::rfft(&fr, NF));
    }
    (out, nfr)
}

/// Overlap-add synthesis with the Hann window, normalised by the summed squared window.
fn istft(xs: &[C], nfr: usize, n_out: usize) -> Vec<f64> {
    let w = HANN.with(|h| h.clone());
    let mut out = vec![0.0; n_out + 3 * NF];
    let mut norm = vec![0.0; n_out + 3 * NF];
    for i in 0..nfr {
        let fr = npx::irfft(&xs[i * NB..(i + 1) * NB], NF);
        let s = i * HOP;
        for j in 0..NF {
            out[s + j] += fr[j] * w[j];
            norm[s + j] += w[j] * w[j];
        }
    }
    (NF..NF + n_out).map(|k| out[k] / norm[k].max(1e-6)).collect()
}

/// SPEC 5.1: Wiener-style subtraction of the bed's mean power spectrum (over-subtraction `over`, gain floor `floor`),
/// the gain smoothed over 5 frames x 3 bins.
pub fn denoise(x: &[f64], bed: &[f64], over: f64, floor: f64) -> Vec<f64> {
    if bed.len() < 4096 {
        return x.to_vec();
    }
    let (mut xs, nfr) = stft(x);
    let (bs, nbf) = stft(bed);
    let mut nb = vec![0.0; NB];
    for i in 0..nbf {
        for k in 0..NB {
            nb[k] += cabs2(bs[i * NB + k]);
        }
    }
    for v in nb.iter_mut() {
        *v /= nbf as f64;
    }
    let fl2 = floor * floor;
    let g: Vec<f64> = (0..nfr * NB)
        .map(|idx| {
            let k = idx % NB;
            let px = cabs2(xs[idx]);
            m::sqrt((1.0 - over * nb[k] / px.max(1e-30)).max(fl2))
        })
        .collect();
    let h7 = npx::hanning(7);
    let h5 = npx::hanning(5);
    let mut ker = [[0.0f64; 3]; 5];
    let mut ksum = 0.0;
    for i in 0..5 {
        for j in 0..3 {
            ker[i][j] = h7[i + 1] * h5[j + 1];
            ksum += ker[i][j];
        }
    }
    for row in ker.iter_mut() {
        for v in row.iter_mut() {
            *v /= ksum;
        }
    }
    let mut gs = vec![0.0; nfr * NB];
    for i in 0..5 {
        for j in 0..3 {
            let kij = ker[i][j];
            for t in 0..nfr {
                let tt = (t as i64 + i as i64 - 2).clamp(0, nfr as i64 - 1) as usize;
                for f in 0..NB {
                    let ff = (f as i64 + j as i64 - 1).clamp(0, NB as i64 - 1) as usize;
                    gs[t * NB + f] += kij * g[tt * NB + ff];
                }
            }
        }
    }
    for (c, gv) in xs.iter_mut().zip(&gs) {
        *c = c.scale(*gv);
    }
    istft(&xs, nfr, x.len())
}

/// SPEC 5.2: duck the band above 1 kHz while the < 800 Hz band is far under its loudest frame.
pub fn hf_gate(x: &[f64]) -> Vec<f64> {
    let (fc, knee_db, depth_db, rel_ms) = (1000.0, 15.0, 18.0, 150.0);
    let (mut xs, nfr) = stft(x);
    let f = rfftfreq(NF);
    let lo: Vec<usize> = (0..NB).filter(|&k| f[k] < 800.0).collect();
    let l: Vec<f64> = (0..nfr)
        .map(|t| {
            let mut s = 0.0;
            for &k in &lo {
                s += cabs2(xs[t * NB + k]);
            }
            10.0 * m::log10(s + 1e-30)
        })
        .collect();
    let lmax = vmax(&l);
    let a = m::exp(-1.0 / (rel_ms / 1000.0 * SR / 256.0));
    let mut s = 0.0;
    let mut gs = Vec::with_capacity(nfr);
    for lt in &l {
        let g = -((lmax - lt) - knee_db).clamp(0.0, depth_db);
        s = if g < s { g } else { a * s + (1.0 - a) * g };
        gs.push(s);
    }
    let w: Vec<f64> = f
        .iter()
        .map(|&fk| ((fk - 0.7 * fc) / (0.3 * fc)).clamp(0.0, 1.0))
        .collect();
    for t in 0..nfr {
        for k in 0..NB {
            let gk = m::pow(10.0, gs[t] * w[k] / 20.0);
            xs[t * NB + k] = xs[t * NB + k].scale(gk);
        }
    }
    istft(&xs, nfr, x.len())
}

/// gbdsp.env_db: frame RMS in dB (10 log10(mean x^2 + 1e-20)), win and hop in seconds.
pub fn env_db(x: &[f64], win_s: f64, hop_s: f64) -> Vec<f64> {
    let win = ti(win_s * SR);
    let hop = ti(hop_s * SR);
    let n = (x.len() as i64 - win).div_euclid(hop) + 1;
    if n <= 0 {
        return vec![-200.0];
    }
    (0..n)
        .map(|i| {
            let s = &x[(i * hop) as usize..(i * hop + win) as usize];
            10.0 * m::log10(sumsq(s) / win as f64 + 1e-20)
        })
        .collect()
}

/// wxlib.frame_rms_db: frame mean power in dB (floored at -200), win and hop in samples.
pub(crate) fn frame_rms_db(x: &[f64], win: usize, hop: usize) -> Vec<f64> {
    let n = (x.len() as i64 - win as i64).div_euclid(hop as i64) + 1;
    if n <= 0 {
        return vec![db10(mean_sq(x))];
    }
    (0..n as usize)
        .map(|i| db10(sumsq(&x[i * hop..i * hop + win]) / win as f64))
        .collect()
}

/// gbdsp.max_rms: the loudest 400 ms RMS (cumulative sums, as the reference).
pub fn max_rms(x: &[f64]) -> f64 {
    let n = x.len().min(ti(0.4 * SR) as usize);
    if n == 0 {
        return f64::NAN;
    }
    let mut cs = Vec::with_capacity(x.len() + 1);
    cs.push(0.0);
    let mut acc = 0.0;
    for v in x {
        acc += v * v;
        cs.push(acc);
    }
    let mut best = f64::NEG_INFINITY;
    for i in 0..=x.len() - n {
        let p = (cs[i + n] - cs[i]) / n as f64;
        if p > best {
            best = p;
        }
    }
    m::sqrt(best)
}

/// Fade-in sin^2, fade-out cos^2 (lengths in ms).
pub fn fade(g: &mut [f64], a_ms: f64, r_ms: f64) {
    let a = ti(a_ms * SR / 1000.0).max(0) as usize;
    let r = ti(r_ms * SR / 1000.0).max(0) as usize;
    if a > 0 {
        let a = a.min(g.len());
        let lin = linspace(0.0, m::PI / 2.0, a);
        for i in 0..a {
            let s = m::sin(lin[i]);
            g[i] *= s * s;
        }
    }
    if r > 0 {
        let r = r.min(g.len());
        let n = g.len();
        let lin = linspace(0.0, m::PI / 2.0, r);
        for i in 0..r {
            let c = m::cos(lin[i]);
            g[n - r + i] *= c * c;
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Mag {
    /// 4th-order Butterworth-magnitude high-pass at 1 kHz x low-pass at 8 kHz (the rain band)
    Band1k8k,
    /// 2nd-order Butterworth-magnitude high-pass at 700 Hz (cracks)
    Hp700,
}

fn hp_mag(fc: f64, order: f64, f: f64) -> f64 {
    1.0 / m::sqrt(1.0 + m::pow(fc / f.max(1e-3), 2.0 * order))
}

fn lp_mag(fc: f64, order: f64, f: f64) -> f64 {
    1.0 / m::sqrt(1.0 + m::pow(f.max(0.0) / fc, 2.0 * order))
}

fn mag_of(mg: Mag, f: f64) -> f64 {
    match mg {
        Mag::Band1k8k => hp_mag(1000.0, 4.0, f) * lp_mag(8000.0, 4.0, f),
        Mag::Hp700 => hp_mag(700.0, 2.0, f),
    }
}

/// The minimum-phase spectrum of a magnitude at FFT length n (homomorphic: the real cepstrum folded), kept per thread:
/// a pure function of (magnitude, n), so the cache never changes a result.
fn min_phase_h(mg: Mag, n: usize) -> Rc<Vec<C>> {
    thread_local! {
        static CACHE: RefCell<BTreeMap<(Mag, usize), Rc<Vec<C>>>> = const { RefCell::new(BTreeMap::new()) };
    }
    if let Some(h) = CACHE.with(|c| c.borrow().get(&(mg, n)).cloned()) {
        return h;
    }
    let f = rfftfreq(n);
    let lm: Vec<C> = f
        .iter()
        .map(|&fk| C::new(m::ln(mag_of(mg, fk).max(1e-6)), 0.0))
        .collect();
    let mut c = npx::irfft(&lm, n);
    for v in c.iter_mut().take(n / 2).skip(1) {
        *v *= 2.0;
    }
    for v in c.iter_mut().skip(n / 2 + 1) {
        *v = 0.0;
    }
    let h: Vec<C> = npx::rfft(&c, n).into_iter().map(|z| z.expc()).collect();
    let h = Rc::new(h);
    CACHE.with(|cc| {
        let mut cc = cc.borrow_mut();
        if cc.len() > 16 {
            cc.clear();
        }
        cc.insert((mg, n), h.clone());
    });
    h
}

/// gbdsp.min_phase_filter: x through the minimum-phase filter at FFT length 2^ceil(log2(n + 8192)).
fn min_phase_filter(x: &[f64], mg: Mag) -> Vec<f64> {
    let n = x.len();
    let nn = dsp::next_pow2(n + 8192);
    let h = min_phase_h(mg, nn);
    let mut xf = npx::rfft(x, nn);
    for (a, b) in xf.iter_mut().zip(h.iter()) {
        *a = a.mul(*b);
    }
    let mut y = npx::irfft(&xf, nn);
    y.truncate(n);
    y
}

/// SPEC 5.3: feed-forward RMS compressor at a 44-sample control rate with a soft knee. Returns the signal and the
/// largest gain reduction (dB).
pub fn compress(
    x: &[f64],
    thr_db: f64,
    ratio: f64,
    att_ms: f64,
    rel_ms: f64,
    win_ms: f64,
    knee_db: f64,
) -> (Vec<f64>, f64) {
    let hop = 44usize;
    let n = x.len() / hop + 1;
    let mut p = vec![0.0; n];
    for (i, pi) in p.iter_mut().enumerate() {
        let mut s = 0.0;
        for j in 0..hop {
            let v = x.get(i * hop + j).copied().unwrap_or(0.0);
            s += v * v;
        }
        *pi = s / hop as f64;
    }
    let k = (ti(win_ms).max(1)) as usize;
    let p = npx::convolve_same(&p, &vec![1.0 / k as f64; k]);
    let cr = 1.0 - 1.0 / ratio;
    let gr: Vec<f64> = p
        .iter()
        .map(|&pv| {
            let over = 10.0 * m::log10(pv + 1e-20) - thr_db;
            if over <= -knee_db / 2.0 {
                0.0
            } else if over >= knee_db / 2.0 {
                over * cr
            } else {
                let o = over + knee_db / 2.0;
                cr * (o * o) / (2.0 * knee_db)
            }
        })
        .collect();
    let aa = m::exp(-1.0 / att_ms);
    let ar = m::exp(-1.0 / rel_ms);
    let mut s = 0.0;
    let mut out = Vec::with_capacity(n);
    for &g in &gr {
        s = if g > s {
            aa * s + (1.0 - aa) * g
        } else {
            ar * s + (1.0 - ar) * g
        };
        out.push(s);
    }
    let xp: Vec<f64> = (0..n).map(|i| i as f64).collect();
    let y = x
        .iter()
        .enumerate()
        .map(|(j, v)| v * m::pow(10.0, -npx::interp(j as f64 / hop as f64, &xp, &out) / 20.0))
        .collect();
    (y, vmax(&out))
}

/// SPEC 5.4: linear up to 0.5 c, a tanh knee above.
pub fn soft_clip(x: &mut [f64], c: f64) {
    crate::grainbank::soft_clip(x, c);
}

// ---------------------------------------------------------------- wxlib.py: screening and events

/// Welch PSD (Hann, 50 % overlap): frequencies and the mean power per bin.
pub(crate) fn psd(x: &[f64], n: usize) -> (Vec<f64>, Vec<f64>) {
    let mut xv = x.to_vec();
    if xv.len() < n {
        xv.resize(n, 0.0);
    }
    let hop = n / 2;
    let nfr = (xv.len() - n) / hop + 1;
    let w = npx::hanning(n);
    let mut p = vec![0.0; n / 2 + 1];
    let mut fr = vec![0.0; n];
    for i in 0..nfr {
        for j in 0..n {
            fr[j] = xv[i * hop + j] * w[j];
        }
        for (pk, c) in p.iter_mut().zip(npx::rfft(&fr, n)) {
            *pk += cabs2(c);
        }
    }
    for v in p.iter_mut() {
        *v /= nfr as f64;
    }
    (rfftfreq(n), p)
}

pub(crate) fn band_power(f: &[f64], p: &[f64], lo: f64, hi: f64) -> f64 {
    let mut s = 0.0;
    for (fk, pk) in f.iter().zip(p) {
        if *fk >= lo && *fk < hi {
            s += pk;
        }
    }
    s
}

/// wxlib.bandpass_fft: zero the bins outside [lo, hi).
pub(crate) fn bandpass_fft(x: &[f64], lo: f64, hi: f64) -> Vec<f64> {
    let n = x.len();
    let mut xf = npx::rfft(x, n);
    let f = rfftfreq(n);
    for (c, fk) in xf.iter_mut().zip(&f) {
        if *fk < lo || *fk >= hi {
            *c = C::default();
        }
    }
    npx::irfft(&xf, n)
}

/// wxlib.screen_window for the thunder classes: (passes, reasons). The non-thunder checks (traffic, rumble, knocks,
/// low tonal peaks) never reject a thunder window and are left out.
pub fn screen_window(x: &[f64], kind: &str) -> (bool, Vec<String>) {
    let mut reasons: Vec<String> = Vec::new();
    let thunder = kind.starts_with("thunder");
    let rmsd = db10(mean_sq(x));
    if rmsd < -75.0 {
        return (false, vec!["silent".into()]);
    }
    // clipping: more than 2 runs of >= 3 samples at the peak when the peak is near full scale
    let mx = absmax(x);
    if mx > 0.98 {
        let thr = mx * 0.999;
        let mut runs = 0;
        let mut run = 0;
        for v in x.iter().chain(std::iter::once(&0.0)) {
            if v.abs() >= thr {
                run += 1;
            } else {
                if run >= 3 {
                    runs += 1;
                }
                run = 0;
            }
        }
        if runs > 2 {
            reasons.push("clipping".into());
        }
    }
    // mains hum
    let (f, p) = psd(x, 32768);
    let mut hum_hit = false;
    for base in [50.0, 60.0] {
        let mut proms = Vec::new();
        for h in 1..=4 {
            let fc = base * h as f64;
            let mut pk = f64::NEG_INFINITY;
            let mut nbv = Vec::new();
            for (fk, pv) in f.iter().zip(&p) {
                if *fk > fc - 1.5 && *fk < fc + 1.5 && *pv > pk {
                    pk = *pv;
                }
                if (*fk > fc - 15.0 && *fk < fc - 4.0) || (*fk > fc + 4.0 && *fk < fc + 15.0) {
                    nbv.push(*pv);
                }
            }
            let nb = npx::median(&nbv);
            proms.push(db10(pk / nb.max(1e-30)));
        }
        if proms.iter().filter(|v| **v > 10.0).count() >= 2 || vmax(&proms) > 18.0 {
            hum_hit = true;
        }
    }
    if hum_hit {
        reasons.push("mains hum".into());
    }
    // spectrogram tonal tracks 1-10 kHz
    let n = 2048usize;
    let hop = 512usize;
    let nfr = if x.len() >= n { (x.len() - n) / hop + 1 } else { 0 };
    let w = npx::hanning(n);
    let ff = rfftfreq(n);
    let nbins = n / 2 + 1;
    let mut s = vec![0.0; nfr * nbins];
    let mut fr = vec![0.0; n];
    for i in 0..nfr {
        for j in 0..n {
            fr[j] = x[i * hop + j] * w[j];
        }
        for (k, c) in npx::rfft(&fr, n).into_iter().enumerate() {
            s[i * nbins + k] = db10(cabs2(c));
        }
    }
    let band: Vec<usize> = (0..nbins).filter(|&k| ff[k] >= 1000.0 && ff[k] <= 10000.0).collect();
    let b100: Vec<usize> = (0..nbins).filter(|&k| ff[k] >= 100.0 && ff[k] <= 10000.0).collect();
    let step = if nfr < 600 { 1 } else { (nfr / 600).max(1) };
    let rows: Vec<usize> = (0..nfr).step_by(step).collect();
    let mut tonal = Vec::with_capacity(rows.len());
    let mut pk_bin = Vec::with_capacity(rows.len());
    for &r in &rows {
        let sb: Vec<f64> = band.iter().map(|&k| s[r * nbins + k]).collect();
        let med = running_median(&sb, 31);
        let prom: Vec<f64> = sb.iter().zip(&med).map(|(a, b)| a - b).collect();
        let kb = argmax(&prom);
        let pv = prom[kb];
        let full = b100.iter().map(|&k| s[r * nbins + k]).fold(f64::NEG_INFINITY, f64::max);
        let lvl = sb[kb];
        tonal.push(pv > 15.0 && lvl > full - 30.0);
        pk_bin.push(kb as i64);
    }
    let mut track_frames = 0usize;
    let mut run = 0usize;
    for i in 0..tonal.len() {
        if tonal[i] && (run == 0 || (pk_bin[i] - pk_bin[(i + tonal.len() - 1) % tonal.len()]).abs() <= 8) {
            run += 1;
        } else {
            if run >= 3 {
                track_frames += run;
            }
            run = if tonal[i] { 1 } else { 0 };
        }
    }
    if run >= 3 {
        track_frames += run;
    }
    let tfrac = track_frames as f64 / tonal.len().max(1) as f64;
    if tfrac > 0.05 {
        reasons.push("tonal tracks 1-10 kHz (birds/insects/squeaks)".into());
    }
    // a steady narrow peak in the long-term spectrum 2-10 kHz
    let mut lt = vec![0.0; nbins];
    for i in 0..nfr {
        for k in 0..nbins {
            lt[k] += m::pow(10.0, s[i * nbins + k] / 10.0);
        }
    }
    for v in lt.iter_mut() {
        *v = db10(*v / nfr as f64);
    }
    let ltb: Vec<f64> = (0..nbins)
        .filter(|&k| ff[k] >= 2000.0 && ff[k] <= 10000.0)
        .map(|k| lt[k])
        .collect();
    let ltmax = b100.iter().map(|&k| lt[k]).fold(f64::NEG_INFINITY, f64::max);
    let rm = running_median(&ltb, 61);
    let ltp: Vec<f64> = ltb
        .iter()
        .zip(&rm)
        .map(|(a, b)| if *a < ltmax - 40.0 { 0.0 } else { a - b })
        .collect();
    if vmax(&ltp) > 8.0 {
        reasons.push("steady whine/insect peak 2-10 kHz".into());
    }
    // voices: harmonic frames 150-1000 Hz in runs
    let v = bandpass_fft(x, 150.0, 1000.0);
    let wl = ti(0.04 * SR) as usize;
    let hop2 = ti(0.02 * SR) as usize;
    let nv = if v.len() >= wl { (v.len() - wl) / hop2 + 1 } else { 0 };
    let en: Vec<f64> = (0..nv).map(|i| sumsq(&v[i * hop2..i * hop2 + wl])).collect();
    let lag_lo = ti(SR / 400.0) as usize;
    let lag_hi = ti(SR / 80.0) as usize;
    let mut voiced = vec![false; nv];
    if nv > 0 {
        let p50 = percentile(&en, 50.0);
        for i in 0..nv {
            if en[i] <= p50 {
                continue;
            }
            let frm = &v[i * hop2..i * hop2 + wl];
            let mu = mean(frm);
            let sv: Vec<f64> = frm.iter().map(|a| a - mu).collect();
            let fsp = npx::rfft(&sv, 2 * wl);
            let pw: Vec<C> = fsp.iter().map(|c| C::new(cabs2(*c), 0.0)).collect();
            let r = npx::irfft(&pw, 2 * (fsp.len() - 1));
            if r[0] <= 0.0 {
                continue;
            }
            let rr = vmax(&r[lag_lo..=lag_hi]) / r[0];
            voiced[i] = rr > 0.75;
        }
    }
    let mut vr = 0usize;
    let mut vrun = 0usize;
    for &vv in &voiced {
        vrun = if vv { vrun + 1 } else { 0 };
        if vrun == 4 {
            vr += 4;
        } else if vrun > 4 {
            vr += 1;
        }
    }
    let vfrac = vr as f64 / voiced.len().max(1) as f64;
    if (vfrac > 0.03 && !thunder) || vfrac > 0.08 {
        reasons.push("voiced/harmonic sound (voices, animals)".into());
    }
    if !thunder {
        non_thunder_checks(x, kind, &f, &p, &mut reasons);
    }
    // broadband hiss floor
    let hb = frame_rms_db(&bandpass_fft(x, 6000.0, 12000.0), 220, 220);
    let steady_hf = percentile(&hb, 99.0) - percentile(&hb, 50.0);
    let tilt = db10(band_power(&f, &p, 12000.0, 16000.0) / band_power(&f, &p, 1000.0, 4000.0).max(1e-30));
    if steady_hf < 3.0 && tilt > -15.0 {
        reasons.push("broadband hiss floor".into());
    }
    // a very quiet take whose quietest frames are flat 5-12 kHz noise
    if rmsd < -45.0 && nfr > 0 {
        let qb: Vec<usize> = (0..nbins).filter(|&k| ff[k] > 1000.0 && ff[k] < 8000.0).collect();
        let fb: Vec<usize> = (0..nbins).filter(|&k| ff[k] >= 5000.0 && ff[k] <= 12000.0).collect();
        let lvl: Vec<f64> = (0..nfr)
            .map(|i| mean(&qb.iter().map(|&k| s[i * nbins + k]).collect::<Vec<_>>()))
            .collect();
        let mut ord: Vec<usize> = (0..nfr).collect();
        ord.sort_by(|a, b| lvl[*a].total_cmp(&lvl[*b]));
        let nq = (nfr / 10).max(1);
        let mut flat = 0.0;
        for &i in ord.iter().take(nq) {
            let pw: Vec<f64> = fb.iter().map(|&k| m::pow(10.0, s[i * nbins + k] / 10.0)).collect();
            let gm = m::exp(mean(&pw.iter().map(|v| m::ln(v + 1e-30)).collect::<Vec<_>>()));
            flat += gm / mean(&pw);
        }
        flat /= nq as f64;
        if flat > 0.6 {
            reasons.push("quiet take: recorder hiss likely".into());
        }
    }
    (reasons.is_empty(), reasons)
}

fn std_pop(v: &[f64]) -> f64 {
    let mu = mean(v);
    m::sqrt(v.iter().map(|x| (x - mu) * (x - mu)).sum::<f64>() / v.len().max(1) as f64)
}

/// wxlib.screen_window's checks for the rain, wind, drip and hail classes (texbuild.rs): low tonal peaks (engines,
/// fans), a slow low-frequency swell under steady rain (traffic, planes), unsteady high band, a strong <120 Hz share and
/// low knocks without rain content (outdoor rain only). Wind classes keep only the tonal check: their low end swells
/// and gusts by nature (texbuild has its own buffeting check).
fn non_thunder_checks(x: &[f64], kind: &str, f: &[f64], p: &[f64], reasons: &mut Vec<String>) {
    let lp: Vec<f64> = p.iter().map(|v| db10(*v)).collect();
    let base = running_median(&lp, 41);
    let mut tonal_lf = f64::NEG_INFINITY;
    for k in 0..lp.len() {
        if f[k] >= 20.0 && f[k] <= 500.0 && lp[k] - base[k] > tonal_lf {
            tonal_lf = lp[k] - base[k];
        }
    }
    if tonal_lf > 15.0 {
        reasons.push("low tonal (engine/fan)".into());
    }
    if kind.starts_with("wind") {
        return;
    }
    let half = (0.5 * SR) as usize;
    let lf = frame_rms_db(&bandpass_fft(x, 40.0, 250.0), half, half);
    let hf = frame_rms_db(&bandpass_fft(x, 2000.0, 8000.0), half, half);
    let (slf, shf) = (std_pop(&lf), std_pop(&hf));
    if slf > 3.5 && shf < 2.0 {
        reasons.push("low-frequency swell (traffic/plane/wind)".into());
    }
    if shf > 4.0 && kind != "rain_light" {
        reasons.push("unsteady (something passing or gusting)".into());
    }
    let outdoor = matches!(kind, "rain_light" | "rain_moderate" | "rain_heavy");
    let lfs = db10(band_power(f, p, 20.0, 120.0) / band_power(f, p, 20.0, 16000.0).max(1e-30));
    if lfs > -6.0 && outdoor {
        reasons.push("strong <120 Hz rumble (wind/traffic/handling)".into());
    }
    let w50 = (0.05 * SR) as usize;
    let l50 = frame_rms_db(&bandpass_fft(x, 60.0, 500.0), w50, w50);
    let h50 = frame_rms_db(&bandpass_fft(x, 2000.0, 8000.0), w50, w50);
    let (ml, mh) = (npx::median(&l50), npx::median(&h50));
    let knocks = l50.iter().zip(&h50).filter(|(a, b)| **a - ml > 12.0 && **b - mh < 4.0).count();
    if knocks > 0 && outdoor {
        reasons.push("low knocks without rain content (handling/footsteps)".into());
    }
}

#[derive(Clone, Debug)]
pub struct Event {
    pub peak_t: f64,
    pub onset_t: f64,
    pub snr_db: f64,
}

/// wxlib.find_thunder_events: peaks of the 400 ms RMS (descending, min_gap_s apart, at least min_db over the 10th
/// percentile), onsets walked back on the 50 ms RMS, SNR re the median level 3-0.3 s before the onset.
pub fn find_thunder_events(x: &[f64], max_events: usize, min_gap_s: f64, min_db: f64) -> Vec<Event> {
    let w400 = ti(0.4 * SR) as usize;
    let hop = ti(0.05 * SR) as usize;
    let e400 = frame_rms_db(x, w400, hop);
    let e50 = frame_rms_db(x, ti(0.05 * SR) as usize, hop);
    let floor = percentile(&e400, 10.0);
    let mut order: Vec<usize> = (0..e400.len()).collect();
    order.sort_by(|a, b| e400[*b].total_cmp(&e400[*a]));
    let mut events: Vec<Event> = Vec::new();
    for i in order {
        if e400[i] < floor + min_db {
            break;
        }
        let t = (i * hop) as f64 / SR;
        if events.iter().any(|ev| (t - ev.peak_t).abs() < min_gap_s) {
            continue;
        }
        let w8 = &e50[i.min(e50.len())..(i + 8).min(e50.len())];
        let j0 = i + if w8.is_empty() { 0 } else { argmax(w8) };
        let mut j = j0 as i64;
        let lim = (j0 as i64 - ti(6.0 / 0.05)).max(0);
        while j > lim && e50[j as usize] > floor + 6.0 {
            j -= 1;
        }
        let onset = (j * hop as i64) as f64 / SR;
        let pre = sl(&e400, (j - ti(3.0 / 0.05)).max(0), (j - ti(0.3 / 0.05)).max(0));
        let pre_floor = if pre.len() > 5 { npx::median(pre) } else { floor };
        events.push(Event {
            peak_t: t,
            onset_t: onset,
            snr_db: e400[i] - pre_floor,
        });
        if events.len() >= max_events {
            break;
        }
    }
    events
}

// ---------------------------------------------------------------- sources

#[derive(Clone, Debug)]
pub struct Source {
    pub id: String,
    pub cls: String,
    pub title: String,
    pub author: String,
    pub license: String,
    pub page: String,
    pub checked: String,
    description: String,
    tags: Vec<String>,
    status: String,
}

fn jstr(j: &Json, k: &str) -> String {
    j.str(k).unwrap_or("").to_string()
}

pub fn read_sources(path: &Path) -> Result<Vec<Source>, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let doc = crate::proto::parse_limit(&text, 64 << 20)?;
    let Json::Obj(recs) = doc else {
        return Err("sources.json: not an object".into());
    };
    let mut out = Vec::new();
    for (id, r) in recs {
        let tags = match r.get("tags") {
            Some(Json::Arr(v)) => v
                .iter()
                .filter_map(|t| if let Json::Str(s) = t { Some(s.clone()) } else { None })
                .collect(),
            _ => Vec::new(),
        };
        out.push(Source {
            id,
            cls: jstr(&r, "cls"),
            title: jstr(&r, "title"),
            author: jstr(&r, "author"),
            license: jstr(&r, "license"),
            page: jstr(&r, "page"),
            checked: jstr(&r, "checked"),
            description: jstr(&r, "description"),
            tags,
            status: jstr(&r, "status"),
        });
    }
    Ok(out)
}

fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// Does `w` occur in `h` with a word boundary before it (if `lb`) and after it (if `rb`)? (Python's \b.)
fn find_bounded(h: &[char], w: &str, lb: bool, rb: bool) -> bool {
    let w: Vec<char> = w.chars().collect();
    if w.len() > h.len() {
        return false;
    }
    for i in 0..=h.len() - w.len() {
        if h[i..i + w.len()] != w[..] {
            continue;
        }
        if lb && i > 0 && is_word(h[i - 1]) {
            continue;
        }
        let j = i + w.len();
        if rb && j < h.len() && is_word(h[j]) {
            continue;
        }
        return true;
    }
    false
}

fn chars_lower(s: &str) -> Vec<char> {
    s.to_lowercase().chars().collect()
}

/// SPEC section 1's text rules: Some(reason) when the record is excluded.
pub fn text_excluded(s: &Source) -> Option<&'static str> {
    let desc: String = s.description.chars().take(200).collect();
    let a = chars_lower(&format!("{} {}", s.title, desc));
    // interior|indoors?|inside|window|room|house|porch|garage|\bcar\b|muffled
    let place = [
        "interior", "indoor", "inside", "window", "room", "house", "porch", "garage", "muffled",
    ];
    if place.iter().any(|w| find_bounded(&a, w, false, false)) || find_bounded(&a, "car", true, true) {
        return Some("indoors / in a car");
    }
    // explos|explod|sci-?fi|impact|\bsfx\b|\beff\b|effects?\b|bomb|gun|firework
    let b = chars_lower(&format!("{} {}", s.title, s.tags.join(" ")));
    let fx = [
        "explos", "explod", "scifi", "sci-fi", "impact", "bomb", "gun", "firework",
    ];
    if fx.iter().any(|w| find_bounded(&b, w, false, false))
        || find_bounded(&b, "sfx", true, true)
        || find_bounded(&b, "eff", true, true)
        || find_bounded(&b, "effect", false, true)
        || find_bounded(&b, "effects", false, true)
    {
        return Some("sound effects");
    }
    let t = chars_lower(&s.title);
    if !["thunder", "lightning", "storm"]
        .iter()
        .any(|w| find_bounded(&t, w, false, false))
    {
        return Some("title is not thunder");
    }
    None
}

/// originals/fs<ID>.*: the first such name in sorted order.
pub(crate) fn original_of(gb: &Path, sid: &str) -> Option<PathBuf> {
    let od = gb.join("originals");
    let mut names: Vec<String> = std::fs::read_dir(&od)
        .ok()?
        .filter_map(|e| e.ok().map(|e| e.file_name().to_string_lossy().to_string()))
        .collect();
    names.sort();
    let pre = format!("fs{sid}.");
    names.into_iter().find(|n| n.starts_with(&pre)).map(|n| od.join(n))
}

pub(crate) fn read_sha_list(gb: &Path) -> Result<BTreeMap<String, String>, String> {
    let p = gb.join("originals.sha256");
    let text =
        std::fs::read_to_string(&p).map_err(|e| format!("{}: {e} (every original must be listed)", p.display()))?;
    let mut out = BTreeMap::new();
    for line in text.lines() {
        let mut it = line.split_whitespace();
        if let (Some(h), Some(n)) = (it.next(), it.next()) {
            out.insert(n.trim_start_matches('*').to_string(), h.to_lowercase());
        }
    }
    Ok(out)
}

/// Read an original, check it against originals.sha256, decode it. Returns (samples, file name, sha256).
pub(crate) fn load_original(path: &Path, shas: &BTreeMap<String, String>) -> Result<(Vec<f64>, String, String), String> {
    let name = path
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default();
    let bytes = std::fs::read(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let sha = hex(&sha256(&bytes));
    match shas.get(&name) {
        Some(h) if *h == sha => {}
        Some(_) => {
            return Err(format!(
                "{name}: sha256 does not match originals.sha256; refusing to decode it"
            ))
        }
        None => return Err(format!("{name}: not listed in originals.sha256; refusing to decode it")),
    }
    drop(bytes);
    Ok((decode(path)?, name, sha))
}

// ---------------------------------------------------------------- plan

#[derive(Clone, Debug)]
pub struct Cand {
    pub src: String,
    pub cls: String,
    pub event: usize,
    pub bed: [f64; 2],
    pub snr_db: f64,
    pub resid_db: f64,
    pub onset_s: f64,
    pub contam_db: f64,
    pub bed_db: f64,
    pub tail_db: f64,
    pub kind: usize,
    pub t0: f64,
    pub t1: f64,
    pub fade_ms: [i64; 2],
    pub score: f64,
    pub crest_db: Option<f64>,
    pub centroid: Option<i64>,
    pub id: String,
}

/// round(x, d) on the exact binary value (Python's round for floats).
fn rnd(x: f64, d: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{x:.d$}").parse().unwrap_or(x)
}

fn bed_of(x: &[f64], onset_s: f64) -> (f64, f64) {
    let o = ti(onset_s * SR);
    let pre = sl(x, (o - ti(0.9 * SR)).max(0), (o - ti(0.1 * SR)).max(0));
    if pre.len() as f64 > 0.5 * SR {
        return ((onset_s - 0.9).max(0.0), onset_s - 0.1);
    }
    let z = sl(x, 0, 60 * SRI);
    let e = env_db(z, 1.0, 0.25);
    let k = argmin(&e) as f64;
    (k * 0.25, k * 0.25 + 1.0)
}

fn refine_onset(y: &[f64], o: i64) -> i64 {
    let ed = env_db(y, 0.005, 0.001);
    let lo = (o - ti(0.3 * SR)).div_euclid(44).max(0);
    let hi = ((o + ti(1.5 * SR)).div_euclid(44)).min(ed.len() as i64);
    if hi <= lo {
        return o;
    }
    let k = lo + argmax(&ed[lo as usize..hi as usize]) as i64;
    let mut j = k;
    while j > lo && ed[j as usize] > ed[k as usize] - 24.0 {
        j -= 1;
    }
    j * 44
}

/// The screened events of one original (SPEC section 2): its first 600 s, up to 6 events at least 10 s apart.
fn events_v3(x: &[f64], cls: &str, verbose: bool, log: &mut String) -> Vec<Event> {
    let z = sl(x, 0, ti(600.0 * SR));
    let mut out = Vec::new();
    for ev in find_thunder_events(z, 6, 10.0, 10.0) {
        let st = (ev.onset_t - 1.0).max(0.0);
        let seg = sl(z, ti(st * SR), ti((st + 15.0) * SR));
        if ev.snr_db < thunder_snr(cls) || (seg.len() as i64) < 10 * SRI {
            if verbose {
                log.push_str(&format!(
                    "    event onset {:.3} snr {:.2}: snr/length\n",
                    ev.onset_t, ev.snr_db
                ));
            }
            continue;
        }
        let (ok, reasons) = screen_window(seg, cls);
        if verbose {
            log.push_str(&format!(
                "    event onset {:.3} snr {:.2}: {}\n",
                ev.onset_t,
                ev.snr_db,
                if ok { "ok".into() } else { reasons.join("; ") }
            ));
        }
        if ok {
            out.push(ev);
        }
    }
    out
}

/// One source's candidates (SPEC section 3) and its log lines.
fn plan_source(src: &Source, x: &[f64], verbose: bool) -> (Vec<Cand>, String) {
    let mut log = String::new();
    let mut cands = Vec::new();
    let cls = src.cls.as_str();
    let k = &src.id;
    let windows = events_v3(x, cls, verbose, &mut log);
    for (wi, wd) in windows.iter().enumerate() {
        let on = wd.onset_t;
        let (b0, b1) = bed_of(x, on);
        let a = ti((on - 1.0) * SR).max(0);
        let seg = sl(x, a, a + ti(15.0 * SR));
        let bed = sl(x, ti(b0 * SR), ti(b1 * SR));
        let y = denoise(seg, bed, 1.5, 0.1);
        let o = refine_onset(&y, ti(on * SR) - a);
        let t_on = (a + o) as f64 / SR;
        let rb = denoise(bed, bed, 1.5, 0.1);
        let resid =
            20.0 * m::log10(m::sqrt(mean_sq(&rb)) + 1e-12) - 20.0 * m::log10(max_rms(sl(&y, o, o + 3 * SRI)) + 1e-12);
        if resid > -40.0 {
            log.push_str(&format!("  residue too high {k} {:.1}\n", resid));
            continue;
        }
        let snr = if -resid < wd.snr_db { -resid } else { wd.snr_db };
        let ref_raw = max_rms(sl(seg, o, o + 3 * SRI));
        let bed_db = if bed.len() > 4096 {
            rms_db(&min_phase_filter(bed, Mag::Band1k8k))
        } else {
            20.0 * m::log10(1e-12)
        } - 20.0 * m::log10(ref_raw + 1e-12);
        let tl = sl(seg, o + 4 * SRI, o + 12 * SRI);
        let mut tail_db = -200.0;
        if tl.len() as i64 > SRI {
            let th = min_phase_filter(tl, Mag::Band1k8k);
            let hop = ti(0.05 * SR) as usize;
            let nf = th.len() / hop;
            let lv: Vec<f64> = (0..nf)
                .map(|i| 10.0 * m::log10(sumsq(&th[i * hop..(i + 1) * hop]) / hop as f64 + 1e-20))
                .collect();
            tail_db = percentile(&lv, 10.0) - 20.0 * m::log10(ref_raw + 1e-12);
        }
        let contam = if tail_db > bed_db { tail_db } else { bed_db };
        let strike_close = cls == "thunder_strike" || cls == "thunder_close";
        let cmax = contam_max(cls) + if strike_close { 5.0 } else { 0.0 };
        if contam > cmax {
            log.push_str(&format!("  rain bed too high {k} {:.1}\n", contam));
            continue;
        }
        let contam_r = rnd(contam, 1);
        let base = Cand {
            src: k.clone(),
            cls: cls.to_string(),
            event: wi,
            bed: [rnd(b0, 4), rnd(b1, 4)],
            snr_db: rnd(snr, 1),
            resid_db: rnd(resid, 1),
            onset_s: rnd(t_on, 4),
            contam_db: contam_r,
            bed_db: rnd(bed_db, 1),
            tail_db: rnd(tail_db, 1),
            kind: 0,
            t0: 0.0,
            t1: 0.0,
            fade_ms: [0, 0],
            score: 0.0,
            crest_db: None,
            centroid: None,
            id: String::new(),
        };
        let mut add = |kind: &str, t0: f64, score: f64, crest: Option<f64>, cen: Option<i64>| {
            let ki = kind_index(kind);
            let (pre, post, fi, fo) = CUT[ki];
            if kind != "body" && kind != "crack" && contam_r > contam_max(cls) {
                return;
            }
            let score = score - 1.0 * (contam_r + 70.0);
            let mut c = base.clone();
            c.kind = ki;
            c.t0 = rnd(t0 - pre, 4);
            c.t1 = rnd(t0 + post, 4);
            c.fade_ms = [fi, fo];
            c.score = score;
            c.crest_db = crest;
            c.centroid = cen;
            cands.push(c);
        };
        // clipped or brick-walled takes: too many samples at the take's peak level in the event's first 3 s
        let ev3 = sl(seg, (o - 441).max(0), o + 3 * SRI);
        let pk3 = absmax(ev3);
        let nclip = ev3.iter().filter(|v| v.abs() >= 0.98 * pk3).count();
        if nclip as f64 / ev3.len() as f64 > 8e-4 {
            log.push_str(&format!("  clipped {k}\n"));
            continue;
        }
        let ev = env_db(&y, 0.05, 0.01);
        let o441 = (o / 441) as usize;
        let pk = if ev.len() > o441 {
            vmax(&ev[o441..(o441 + 300).min(ev.len())])
        } else {
            vmax(&ev)
        };
        let ny = y.len() as i64;
        if strike_close {
            let body = sl(&y, (o - 441).max(0), o + ti(2.5 * SR));
            let crest = 20.0 * m::log10(absmax(body) / (max_rms(body) + 1e-12));
            let fb = rfftfreq(body.len());
            let sp = npx::rfft(body, body.len());
            let (mut num, mut den) = (0.0, 0.0);
            for (fk, c) in fb.iter().zip(&sp) {
                let p = cabs2(*c);
                num += fk * p;
                den += p;
            }
            let cen = libm::rint(num / den) as i64;
            let crest_r = rnd(crest, 1);
            add(
                "body",
                t_on,
                snr - (crest - PEAKY_DB).max(0.0),
                Some(crest_r),
                Some(cen),
            );
            add(
                "crack",
                t_on,
                snr + if cls == "thunder_strike" { 6.0 } else { 0.0 },
                None,
                None,
            );
            let i_hi = (ev.len() as i64 - 10).min(o / 441 + 700);
            let mut i = o / 441 + 60;
            while i < i_hi {
                let iu = i as usize;
                let wmax = vmax(sl(&ev, (i - 25).max(0), i + 26));
                if ev[iu] == wmax {
                    let lo_ = vmin(sl(&ev, (i - 40).max(0), i)).min(vmin(sl(&ev, i + 1, i + 41)));
                    let prom = ev[iu] - lo_;
                    if prom >= 5.0 && ev[iu] > pk - 28.0 && (i * 441 + ti(0.9 * SR)) < ny {
                        add("clap", (a + i * 441) as f64 / SR, prom + 0.3 * snr, None, None);
                    }
                }
                i += 1;
            }
            let stop = ((ny - 3 * SRI) as f64).min(o as f64 + 9.0 * SR);
            for st in arange(o as f64 + 2.0 * SR, stop, 1.5 * SR) {
                let st = ti(st);
                let lv = rms_db(sl(&y, st, st + 3 * SRI));
                if lv > pk - 40.0 {
                    add("roll", (a + st) as f64 / SR, snr + 0.2 * (lv - pk), None, None);
                }
            }
        } else if cls == "thunder_mid" {
            add("mid_body", t_on, snr, None, None);
            let stop = ((ny - 3 * SRI) as f64).min(o as f64 + 10.0 * SR);
            for st in arange(o as f64 + 2.5 * SR, stop, 1.5 * SR) {
                let st = ti(st);
                let lv = rms_db(sl(&y, st, st + 3 * SRI));
                if lv > pk - 30.0 {
                    add("mid_roll", (a + st) as f64 / SR, snr + 0.3 * (lv - pk), None, None);
                }
            }
        } else if cls == "thunder_far" {
            let stop = (ny as f64 - 3.5 * SR).min(o as f64 + 10.0 * SR);
            for st in arange(o as f64, stop, 1.7 * SR) {
                let st = ti(st);
                let lv = rms_db(sl(&y, st, st + ti(3.3 * SR)));
                if lv > pk - 20.0 {
                    add(
                        "far_roll",
                        (a + st) as f64 / SR + 0.2,
                        snr + 0.3 * (lv - pk),
                        None,
                        None,
                    );
                }
            }
        }
    }
    log.push_str(&format!("planned {k} {cls}\n"));
    (cands, log)
}

/// Run `f` over 0..n on up to `threads` workers; the results come back in index order (deterministic whatever the
/// scheduling). `f` gets its own thread's caches; nothing is shared but the inputs.
pub(crate) fn par_map<T: Send, F: Fn(usize) -> T + Sync>(n: usize, threads: usize, f: F) -> Vec<T> {
    let threads = threads.max(1).min(n.max(1));
    if threads == 1 {
        return (0..n).map(&f).collect();
    }
    let next = std::sync::atomic::AtomicUsize::new(0);
    let slots: Vec<std::sync::Mutex<Option<T>>> = (0..n).map(|_| std::sync::Mutex::new(None)).collect();
    std::thread::scope(|s| {
        for _ in 0..threads {
            s.spawn(|| loop {
                let i = next.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                if i >= n {
                    break;
                }
                let r = f(i);
                *slots[i].lock().unwrap() = Some(r);
            });
        }
    });
    slots
        .into_iter()
        .map(|m| m.into_inner().unwrap().expect("every index done"))
        .collect()
}

/// `plan`: every usable source's candidates, then the selection (SPEC section 4). Writes the plan JSON.
pub fn plan(gb: &Path, out: &Path, threads: usize, verbose: bool) -> Result<String, String> {
    let sources = read_sources(&gb.join("sources.json"))?;
    let shas = read_sha_list(gb)?;
    let mut todo: Vec<(Source, PathBuf)> = Vec::new();
    for s in sources {
        if s.status != "clean" || s.cls.ends_with("_any") {
            continue;
        }
        if !matches!(
            s.cls.as_str(),
            "thunder_strike" | "thunder_close" | "thunder_mid" | "thunder_far"
        ) {
            continue;
        }
        if text_excluded(&s).is_some() {
            continue;
        }
        let Some(op) = original_of(gb, &s.id) else { continue };
        todo.push((s, op));
    }
    let results = par_map(todo.len(), threads, |i| -> Result<(Vec<Cand>, String), String> {
        let (s, op) = &todo[i];
        let (x, _, _) = load_original(op, &shas)?;
        Ok(plan_source(s, &x, verbose))
    });
    let mut by_kind: Vec<Vec<Cand>> = vec![Vec::new(); KINDS.len()];
    for r in results {
        let (cands, log) = r?;
        print!("{log}");
        for c in cands {
            by_kind[c.kind].push(c);
        }
    }
    // selection: best score first (stable), per-source caps, the kind's time budget; a source's crack only with its body
    let mut chosen: Vec<Cand> = Vec::new();
    for (ki, list) in by_kind.iter_mut().enumerate() {
        let (budget, cap) = BUDGET[ki];
        let mut per: BTreeMap<String, usize> = BTreeMap::new();
        let mut tot = 0.0;
        let body_srcs: Vec<String> = chosen.iter().filter(|c| c.kind == 0).map(|c| c.src.clone()).collect();
        list.sort_by(|a, b| b.score.total_cmp(&a.score));
        for c in list.iter() {
            let dur = c.t1 - c.t0;
            let n = per.get(&c.src).copied().unwrap_or(0);
            if n >= cap || tot + dur > budget {
                continue;
            }
            if KINDS[ki] == "crack" && !body_srcs.contains(&c.src) {
                continue;
            }
            if KINDS[ki] == "body" && c.cls == "thunder_close" && c.crest_db.unwrap_or(0.0) > PEAKY_DB + 6.0 {
                continue;
            }
            per.insert(c.src.clone(), n + 1);
            tot += dur;
            chosen.push(c.clone());
        }
    }
    for (i, c) in chosen.iter_mut().enumerate() {
        c.id = format!("{}_{:03}_{}", KINDS[c.kind], i, c.src);
    }
    let budget = Out::Obj(
        KINDS
            .iter()
            .zip(BUDGET)
            .map(|(k, b)| (k.to_string(), Out::F(b.0)))
            .collect(),
    );
    let doc = Out::Obj(vec![
        ("ref_rms_db".into(), Out::F(REF_RMS_DB)),
        ("peaky_db".into(), Out::F(PEAKY_DB)),
        ("budget_s".into(), budget),
        ("grains".into(), Out::Arr(chosen.iter().map(cand_json).collect())),
    ]);
    write_json(out, &doc)?;
    let mut summary = Vec::new();
    let mut total = 0.0;
    for (ki, k) in KINDS.iter().enumerate() {
        let n = chosen.iter().filter(|c| c.kind == ki).count();
        let s: f64 = chosen.iter().filter(|c| c.kind == ki).map(|c| c.t1 - c.t0).sum();
        total += s;
        summary.push(format!("'{k}': ({n}, {:.1})", s));
    }
    Ok(format!("{{{}}} total {:.1}", summary.join(", "), total))
}

fn cand_json(c: &Cand) -> Out {
    let mut v = vec![
        ("src".into(), Out::S(c.src.clone())),
        ("cls".into(), Out::S(c.cls.clone())),
        ("event".into(), Out::I(c.event as i64)),
        ("bed".into(), Out::Arr(vec![Out::F(c.bed[0]), Out::F(c.bed[1])])),
        ("snr_db".into(), Out::F(c.snr_db)),
        ("resid_db".into(), Out::F(c.resid_db)),
        ("onset_s".into(), Out::F(c.onset_s)),
        ("contam_db".into(), Out::F(c.contam_db)),
        ("bed_1_8k_db".into(), Out::F(c.bed_db)),
        ("tail_1_8k_db".into(), Out::F(c.tail_db)),
        ("kind".into(), Out::S(KINDS[c.kind].into())),
        ("t0".into(), Out::F(c.t0)),
        ("t1".into(), Out::F(c.t1)),
        (
            "fade_ms".into(),
            Out::Arr(vec![Out::I(c.fade_ms[0]), Out::I(c.fade_ms[1])]),
        ),
        ("score".into(), Out::F(c.score)),
    ];
    if let Some(cr) = c.crest_db {
        v.push(("crest_db".into(), Out::F(cr)));
    }
    if let Some(ce) = c.centroid {
        v.push(("centroid".into(), Out::I(ce)));
    }
    v.push(("id".into(), Out::S(c.id.clone())));
    Out::Obj(v)
}

fn read_plan(path: &Path) -> Result<Vec<Cand>, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let doc = crate::proto::parse_limit(&text, 64 << 20)?;
    let Some(Json::Arr(list)) = doc.get("grains") else {
        return Err("plan: no grains".into());
    };
    let pair = |g: &Json, k: &str| -> Result<[f64; 2], String> {
        match g.get(k) {
            Some(Json::Arr(v)) if v.len() == 2 => match (&v[0], &v[1]) {
                (Json::Num(a), Json::Num(b)) => Ok([*a, *b]),
                _ => Err(format!("plan: bad {k}")),
            },
            _ => Err(format!("plan: no {k}")),
        }
    };
    let mut out = Vec::new();
    for g in list {
        let num = |k: &str| g.num(k).ok_or(format!("plan: no {k}"));
        let kind = g.str("kind").ok_or("plan: no kind")?;
        let fm = pair(g, "fade_ms")?;
        out.push(Cand {
            src: g.str("src").ok_or("plan: no src")?.to_string(),
            cls: g.str("cls").ok_or("plan: no cls")?.to_string(),
            event: num("event")? as usize,
            bed: pair(g, "bed")?,
            snr_db: num("snr_db")?,
            resid_db: num("resid_db")?,
            onset_s: num("onset_s")?,
            contam_db: g.num("contam_db").unwrap_or(0.0),
            bed_db: g.num("bed_1_8k_db").unwrap_or(f64::NAN),
            tail_db: g.num("tail_1_8k_db").unwrap_or(f64::NAN),
            kind: KINDS
                .iter()
                .position(|k| *k == kind)
                .ok_or(format!("plan: unknown kind {kind}"))?,
            t0: num("t0")?,
            t1: num("t1")?,
            fade_ms: [fm[0] as i64, fm[1] as i64],
            score: num("score")?,
            crest_db: g.num("crest_db"),
            centroid: g.num("centroid").map(|v| v as i64),
            id: g.str("id").ok_or("plan: no id")?.to_string(),
        });
    }
    Ok(out)
}

// ---------------------------------------------------------------- cut

struct CutGrain {
    samples: Vec<f64>,
    processing: Vec<String>,
    gain_db: f64,
    crest_db: f64,
}

/// SPEC section 5 for one grain. `x` is the original with 1 s of silence in front (off = 1 s).
fn cut_grain(c: &Cand, x: &[f64]) -> CutGrain {
    let off = 1.0;
    let bed = sl(x, ti((c.bed[0] + off) * SR), ti((c.bed[1] + off) * SR));
    let w0 = (c.onset_s - 1.0 + off).max(0.0);
    let seg = sl(x, ti(w0 * SR), ti((w0 + 15.0) * SR));
    let dry = c.contam_db <= DRY_DB;
    let dn = |z: &[f64]| -> Vec<f64> {
        if dry {
            z.to_vec()
        } else {
            hf_gate(&denoise(z, bed, 1.5, 0.03))
        }
    };
    let mut y = dn(seg);
    let mut i0 = ti((c.t0 + off - w0) * SR);
    let mut i1 = ti((c.t1 + off - w0) * SR);
    if i1 > y.len() as i64 {
        // windows beyond the 15 s window: denoise their own span
        let seg2 = sl(x, ti((c.t0 + off - 0.5) * SR).max(0), ti((c.t1 + off + 0.5) * SR));
        y = dn(seg2);
        i0 = if c.t0 + off >= 0.5 {
            ti(0.5 * SR)
        } else {
            ti((c.t0 + off) * SR)
        };
        i1 = i0 + ti((c.t1 - c.t0) * SR);
    }
    let mut g = sl(&y, i0, i1).to_vec();
    let mut proc: Vec<String> = vec![if dry {
        "no denoise (dry take)".into()
    } else {
        "Wiener denoise (floor -30 dB) + high-band gate (knee 15 dB, depth 18 dB)".into()
    }];
    if KINDS[c.kind] == "crack" {
        g = min_phase_filter(&g, Mag::Hp700);
        proc.push("highpass 700 Hz 2nd order (minimum phase)".into());
    }
    let mu = mean(&g);
    for v in g.iter_mut() {
        *v -= mu;
    }
    let crest = 20.0 * m::log10(absmax(&g) / (max_rms(&g) + 1e-12));
    if KINDS[c.kind] == "body" && crest > PEAKY_DB {
        let thr = 20.0 * m::log10(max_rms(&g) + 1e-12) + 4.0;
        let (gc, gr) = compress(&g, thr, 4.0, 1.0, 150.0, 2.0, 6.0);
        g = gc;
        proc.push(format!(
            "pre-compressed: RMS 2 ms, thr = loudest-400ms-RMS + 4 dB, ratio 4, att 1 ms, rel 150 ms (max GR {gr:.1} dB)"
        ));
    }
    fade(&mut g, c.fade_ms[0] as f64, c.fade_ms[1] as f64);
    let gain_db = REF_RMS_DB - 20.0 * m::log10(max_rms(&g) + 1e-12);
    let gl = m::pow(10.0, gain_db / 20.0);
    for v in g.iter_mut() {
        *v *= gl;
    }
    let pk = absmax(&g);
    if pk > 0.98 {
        soft_clip(&mut g, 0.98);
        proc.push(format!(
            "soft-clipped to 0.98 (peak was {:.1} dBFS)",
            20.0 * m::log10(pk)
        ));
    }
    CutGrain {
        samples: g,
        processing: proc,
        gain_db,
        crest_db: crest,
    }
}

/// `cut`: every planned grain from its original, written as Vorbis q5 with a manifest (the reference's schema).
pub fn cut(gb: &Path, plan_path: &Path, out_dir: &Path, threads: usize) -> Result<String, String> {
    let plan = read_plan(plan_path)?;
    let sources = read_sources(&gb.join("sources.json"))?;
    let shas = read_sha_list(gb)?;
    let gdir = out_dir.join("grains");
    std::fs::create_dir_all(&gdir).map_err(|e| format!("{}: {e}", gdir.display()))?;
    // distinct sources in plan order; each worker decodes one source and cuts all of its grains
    let mut srcs: Vec<String> = Vec::new();
    for c in &plan {
        if !srcs.contains(&c.src) {
            srcs.push(c.src.clone());
        }
    }
    let results = par_map(srcs.len(), threads, |si| -> Result<Vec<(usize, f64, Out)>, String> {
        let sid = &srcs[si];
        let s = sources
            .iter()
            .find(|s| &s.id == sid)
            .ok_or(format!("{sid}: not in sources.json"))?;
        let op = original_of(gb, sid).ok_or(format!("{sid}: no original"))?;
        let (xo, name, sha) = load_original(&op, &shas)?;
        let mut x = vec![0.0; SRI as usize];
        x.extend_from_slice(&xo);
        drop(xo);
        let mut out = Vec::new();
        for (gi, c) in plan.iter().enumerate() {
            if &c.src != sid {
                continue;
            }
            let cg = cut_grain(c, &x);
            let file = gdir.join(format!("{}.ogg", c.id));
            write_ogg(&file, &cg.samples)?;
            let gsha = hex(&sha256(
                &std::fs::read(&file).map_err(|e| format!("{}: {e}", file.display()))?,
            ));
            let used = Out::Obj(vec![
                ("file".into(), Out::S("original".into())),
                ("path".into(), Out::S(name.clone())),
                ("sha256".into(), Out::S(sha.clone())),
                ("offset_s".into(), Out::F(0.0)),
                (
                    "timeline".into(),
                    Out::S("original (planned on the original itself)".into()),
                ),
            ]);
            let src_id: i64 = c
                .src
                .parse()
                .map_err(|_| format!("{}: source id is not a number", c.src))?;
            let dur = rnd(cg.samples.len() as f64 / SR, 4);
            let entry = Out::Obj(vec![
                ("id".into(), Out::S(c.id.clone())),
                ("kind".into(), Out::S(KINDS[c.kind].into())),
                ("class_of_source".into(), Out::S(c.cls.clone())),
                ("file".into(), Out::S(format!("grains/{}.ogg", c.id))),
                ("dur_s".into(), Out::F(dur)),
                ("source_id".into(), Out::I(src_id)),
                ("source_page".into(), Out::S(s.page.clone())),
                ("title".into(), Out::S(s.title.clone())),
                ("author".into(), Out::S(s.author.clone())),
                ("license".into(), Out::S(s.license.clone())),
                ("licence_checked".into(), Out::S(s.checked.clone())),
                ("source_file".into(), used),
                ("cut_s".into(), Out::Arr(vec![Out::F(c.t0), Out::F(c.t1)])),
                ("bed_s".into(), Out::Arr(vec![Out::F(c.bed[0]), Out::F(c.bed[1])])),
                ("onset_s".into(), Out::F(c.onset_s)),
                (
                    "fade_ms".into(),
                    Out::Arr(vec![Out::I(c.fade_ms[0]), Out::I(c.fade_ms[1])]),
                ),
                (
                    "processing".into(),
                    Out::Arr(cg.processing.iter().map(|p| Out::S(p.clone())).collect()),
                ),
                ("level_gain_db".into(), Out::F(rnd(cg.gain_db, 2))),
                ("crest_db_before".into(), Out::F(rnd(cg.crest_db, 1))),
                ("sha256_grain".into(), Out::S(gsha)),
                ("centroid_hz".into(), c.centroid.map(Out::I).unwrap_or(Out::Null)),
                ("snr_db".into(), Out::F(c.snr_db)),
                ("contam_db".into(), Out::F(c.contam_db)),
                ("bed_1_8k_db".into(), Out::F(c.bed_db)),
                ("tail_1_8k_db".into(), Out::F(c.tail_db)),
            ]);
            out.push((gi, dur, entry));
        }
        Ok(out)
    });
    let mut entries: Vec<Option<(f64, Out)>> = (0..plan.len()).map(|_| None).collect();
    for r in results {
        for (gi, dur, e) in r? {
            entries[gi] = Some((dur, e));
        }
    }
    let mut man = Vec::new();
    let mut total = 0.0;
    for (gi, e) in entries.into_iter().enumerate() {
        let (d, e) = e.ok_or(format!("grain {gi} not cut"))?;
        println!("cut {}", plan[gi].id);
        total += d;
        man.push(e);
    }
    let n = man.len();
    let doc = Out::Obj(vec![
        ("version".into(), Out::I(1)),
        ("sr".into(), Out::I(44100)),
        ("ref_rms_db".into(), Out::F(REF_RMS_DB)),
        ("total_s".into(), Out::F(rnd(total, 1))),
        ("grains".into(), Out::Arr(man)),
    ]);
    write_json(&out_dir.join("manifest.json"), &doc)?;
    Ok(format!("grains {n} total s {:.1}", total))
}

// ---------------------------------------------------------------- compare (SPEC section 9, builder acceptance)

const THIRD_LO_HZ: f64 = 31.0;
const THIRD_HI_HZ: f64 = 10000.0;

/// 1/3-octave band shares (dB re the 31.5 Hz-10 kHz total, floored at -60 dB) of a Welch PSD (4096-point Hann).
pub fn third_octave_shares(x: &[f64]) -> Vec<f64> {
    let (f, p) = psd(x, 4096);
    let mut bands = Vec::new();
    for i in -15..13 {
        let fc = 1000.0 * m::pow(2.0, i as f64 / 3.0);
        if !(THIRD_LO_HZ..=THIRD_HI_HZ).contains(&fc) {
            continue;
        }
        let lo = fc / m::pow(2.0, 1.0 / 6.0);
        let hi = fc * m::pow(2.0, 1.0 / 6.0);
        bands.push(band_power(&f, &p, lo, hi));
    }
    let tot: f64 = bands.iter().sum();
    bands.iter().map(|b| db10(b / tot).max(-60.0)).collect()
}

struct MEntry {
    id: String,
    kind: String,
    src: i64,
    cut: [f64; 2],
    contam: f64,
    dry: bool,
    file: PathBuf,
}

fn read_manifest(path: &Path) -> Result<Vec<MEntry>, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let doc = crate::proto::parse_limit(&text, 64 << 20)?;
    let dir = path.parent().map(|p| p.to_path_buf()).unwrap_or_default();
    let Some(Json::Arr(list)) = doc.get("grains") else {
        return Err("manifest: no grains".into());
    };
    let mut out = Vec::new();
    for g in list {
        let cut = match g.get("cut_s") {
            Some(Json::Arr(v)) if v.len() == 2 => match (&v[0], &v[1]) {
                (Json::Num(a), Json::Num(b)) => [*a, *b],
                _ => return Err("manifest: bad cut_s".into()),
            },
            _ => return Err("manifest: no cut_s".into()),
        };
        let dry = match g.get("processing") {
            Some(Json::Arr(v)) => v
                .iter()
                .any(|p| matches!(p, Json::Str(s) if s.starts_with("no denoise"))),
            _ => false,
        };
        out.push(MEntry {
            id: g.str("id").unwrap_or("").to_string(),
            kind: g.str("kind").unwrap_or("").to_string(),
            src: g.num("source_id").unwrap_or(-1.0) as i64,
            cut,
            contam: g.num("contam_db").unwrap_or(f64::NAN),
            dry,
            file: dir.join(g.str("file").unwrap_or("")),
        });
    }
    Ok(out)
}

/// `grainbank-compare`: a new manifest against the reference's: the same (source, kind) selection, cut times,
/// contamination, dry decisions and 1/3-octave shares per grain (grains matched by kind, source and nearest cut).
pub fn compare(ref_path: &Path, new_path: &Path) -> Result<String, String> {
    let r = read_manifest(ref_path)?;
    let n = read_manifest(new_path)?;
    let mut o = String::new();
    let key = |e: &MEntry| (e.kind.clone(), e.src);
    let mut rk: Vec<(String, i64)> = r.iter().map(key).collect();
    let mut nk: Vec<(String, i64)> = n.iter().map(key).collect();
    rk.sort();
    nk.sort();
    let same_sel = rk == nk;
    let mut rs: Vec<i64> = r.iter().map(|e| e.src).collect();
    let mut ns: Vec<i64> = n.iter().map(|e| e.src).collect();
    rs.sort();
    rs.dedup();
    ns.sort();
    ns.dedup();
    o += &format!(
        "reference: {} grains from {} sources; new: {} grains from {} sources\n",
        r.len(),
        rs.len(),
        n.len(),
        ns.len()
    );
    o += &format!(
        "same sources: {}; same (kind, source) multiset: {}\n",
        rs == ns,
        same_sel
    );
    let mut used = vec![false; n.len()];
    let mut rows = Vec::new();
    let mut unmatched = Vec::new();
    for e in &r {
        let mut best: Option<usize> = None;
        for (j, f) in n.iter().enumerate() {
            if used[j] || f.kind != e.kind || f.src != e.src {
                continue;
            }
            let d = (f.cut[0] - e.cut[0]).abs();
            if best.is_none_or(|b| d < (n[b].cut[0] - e.cut[0]).abs()) {
                best = Some(j);
            }
        }
        match best {
            Some(j) => {
                used[j] = true;
                rows.push((e, &n[j]));
            }
            None => unmatched.push(e.id.clone()),
        }
    }
    let extra: Vec<String> = n
        .iter()
        .zip(&used)
        .filter(|(_, u)| !**u)
        .map(|(e, _)| e.id.clone())
        .collect();
    o += &format!(
        "unmatched reference grains: {:?}\nextra new grains: {:?}\n",
        unmatched, extra
    );
    o += "id\tnew id\tcut0 diff ms\tcut1 diff ms\tcontam ref\tcontam new\tdiff dB\tdry ref/new\tthird-oct RMS dB\tdecoded samples\n";
    let (mut wc, mut wcon, mut wsp) = (
        (0.0f64, String::new()),
        (0.0f64, String::new()),
        (0.0f64, String::new()),
    );
    let mut dry_mis = Vec::new();
    let (mut n_cut, mut n_con, mut n_sp) = (0, 0, 0);
    let mut sps = Vec::new();
    let mut n_ident = 0;
    let mut worst_samp = (f64::NEG_INFINITY, String::new());
    for (e, f) in &rows {
        let d0 = (f.cut[0] - e.cut[0]) * 1000.0;
        let d1 = (f.cut[1] - e.cut[1]) * 1000.0;
        let dc = f.contam - e.contam;
        let a = decode(&e.file)?;
        let b = decode(&f.file)?;
        let sa = third_octave_shares(&a);
        let sb = third_octave_shares(&b);
        let sp = m::sqrt(sa.iter().zip(&sb).map(|(x, y)| (x - y) * (x - y)).sum::<f64>() / sa.len() as f64);
        sps.push(sp);
        // the decoded samples themselves: bit-identical, or the largest difference re the reference's peak (dB)
        let samp = if a.len() != b.len() {
            format!("len {} / {}", a.len(), b.len())
        } else if a == b {
            n_ident += 1;
            "identical".to_string()
        } else {
            let md = a.iter().zip(&b).fold(0.0f64, |acc, (x, y)| acc.max((x - y).abs()));
            let d = m::db(md / absmax(&a).max(1e-30));
            if d > worst_samp.0 {
                worst_samp = (d, e.id.clone());
            }
            format!("{d:.1} dB")
        };
        let dc_abs = dc.abs();
        let dmax = d0.abs().max(d1.abs());
        if dmax > wc.0 || wc.1.is_empty() {
            wc = (dmax, e.id.clone());
        }
        if dc_abs > wcon.0 || wcon.1.is_empty() {
            wcon = (dc_abs, e.id.clone());
        }
        if sp > wsp.0 || wsp.1.is_empty() {
            wsp = (sp, e.id.clone());
        }
        if dmax <= 5.0 {
            n_cut += 1;
        }
        if dc_abs <= 1.0 {
            n_con += 1;
        }
        if sp <= 1.0 {
            n_sp += 1;
        }
        if e.dry != f.dry {
            dry_mis.push(e.id.clone());
        }
        o += &format!(
            "{}\t{}\t{:.2}\t{:.2}\t{:.1}\t{:.1}\t{:+.2}\t{}/{}\t{:.3}\t{}\n",
            e.id, f.id, d0, d1, e.contam, f.contam, dc, e.dry as u8, f.dry as u8, sp, samp
        );
    }
    let mut sorted = sps.clone();
    sorted.sort_by(|a, b| a.total_cmp(b));
    let med = if sorted.is_empty() {
        f64::NAN
    } else {
        npx::median(&sorted)
    };
    let k = rows.len();
    o += &format!("matched {k}\n");
    o += &format!("cut times within 5 ms: {n_cut}/{k} (worst {:.2} ms, {})\n", wc.0, wc.1);
    o += &format!(
        "contamination within 1 dB: {n_con}/{k} (worst {:.2} dB, {})\n",
        wcon.0, wcon.1
    );
    o += &format!(
        "dry decisions equal: {}/{k} (differ: {:?})\n",
        k - dry_mis.len(),
        dry_mis
    );
    o += &format!(
        "1/3-octave shares within 1 dB RMS: {n_sp}/{k} (median {:.3} dB, worst {:.3} dB, {})\n",
        med, wsp.0, wsp.1
    );
    o += &format!(
        "decoded samples bit-identical: {n_ident}/{k} (largest difference elsewhere {:.1} dB re peak, {})\n",
        worst_samp.0, worst_samp.1
    );
    Ok(o)
}

// ---------------------------------------------------------------- JSON out (Python json.dump(indent=1) layout)

pub enum Out {
    Null,
    I(i64),
    F(f64),
    S(String),
    Arr(Vec<Out>),
    Obj(Vec<(String, Out)>),
}

/// Python's repr() of a float (shortest round-trip digits; exponent below 1e-4 and from 1e16).
pub fn py_float(x: f64) -> String {
    if x.is_nan() {
        return "NaN".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "Infinity".into() } else { "-Infinity".into() };
    }
    if x == 0.0 {
        return if x.is_sign_negative() {
            "-0.0".into()
        } else {
            "0.0".into()
        };
    }
    let s = format!("{:e}", x);
    let (mant, exp) = s.split_once('e').unwrap();
    let exp: i32 = exp.parse().unwrap();
    let (neg, mant) = if let Some(mm) = mant.strip_prefix('-') {
        (true, mm)
    } else {
        (false, mant)
    };
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let decpt = exp + 1;
    let mut out = String::new();
    if neg {
        out.push('-');
    }
    if decpt > -4 && decpt <= 16 {
        if decpt <= 0 {
            out.push_str("0.");
            for _ in 0..(-decpt) {
                out.push('0');
            }
            out.push_str(&digits);
        } else if (decpt as usize) >= digits.len() {
            out.push_str(&digits);
            for _ in 0..(decpt as usize - digits.len()) {
                out.push('0');
            }
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

/// A JSON string as Python writes it (ensure_ascii: everything outside ASCII as \uXXXX, surrogate pairs above).
fn py_str(s: &str, o: &mut String) {
    o.push('"');
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            '\u{8}' => o.push_str("\\b"),
            '\u{c}' => o.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for u in c.encode_utf16(&mut buf) {
                    o.push_str(&format!("\\u{:04x}", u));
                }
            }
            c => o.push(c),
        }
    }
    o.push('"');
}

fn emit(v: &Out, level: usize, o: &mut String) {
    match v {
        Out::Null => o.push_str("null"),
        Out::I(i) => o.push_str(&i.to_string()),
        Out::F(f) => o.push_str(&py_float(*f)),
        Out::S(s) => py_str(s, o),
        Out::Arr(a) => {
            if a.is_empty() {
                o.push_str("[]");
                return;
            }
            o.push('[');
            for (i, e) in a.iter().enumerate() {
                if i > 0 {
                    o.push(',');
                }
                o.push('\n');
                o.push_str(&" ".repeat(level + 1));
                emit(e, level + 1, o);
            }
            o.push('\n');
            o.push_str(&" ".repeat(level));
            o.push(']');
        }
        Out::Obj(a) => {
            if a.is_empty() {
                o.push_str("{}");
                return;
            }
            o.push('{');
            for (i, (k, e)) in a.iter().enumerate() {
                if i > 0 {
                    o.push(',');
                }
                o.push('\n');
                o.push_str(&" ".repeat(level + 1));
                py_str(k, o);
                o.push_str(": ");
                emit(e, level + 1, o);
            }
            o.push('\n');
            o.push_str(&" ".repeat(level));
            o.push('}');
        }
    }
}

pub(crate) fn write_json(path: &Path, v: &Out) -> Result<(), String> {
    let mut s = String::new();
    emit(v, 0, &mut s);
    let tmp = path.with_extension("json.new");
    std::fs::write(&tmp, s).map_err(|e| format!("{}: {e}", tmp.display()))?;
    std::fs::rename(&tmp, path).map_err(|e| format!("{}: {e}", path.display()))
}

// ---------------------------------------------------------------- SHA-256 (FIPS 180-4)

pub fn sha256(data: &[u8]) -> [u8; 32] {
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98,
        0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
        0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8,
        0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
        0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819,
        0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
        0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    ];
    let block = |h: &mut [u32; 8], b: &[u8]| {
        let mut w = [0u32; 64];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([b[4 * i], b[4 * i + 1], b[4 * i + 2], b[4 * i + 3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16].wrapping_add(s0).wrapping_add(w[i - 7]).wrapping_add(s1);
        }
        let mut v = *h;
        for i in 0..64 {
            let s1 = v[4].rotate_right(6) ^ v[4].rotate_right(11) ^ v[4].rotate_right(25);
            let ch = (v[4] & v[5]) ^ (!v[4] & v[6]);
            let t1 = v[7]
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = v[0].rotate_right(2) ^ v[0].rotate_right(13) ^ v[0].rotate_right(22);
            let maj = (v[0] & v[1]) ^ (v[0] & v[2]) ^ (v[1] & v[2]);
            let t2 = s0.wrapping_add(maj);
            v[7] = v[6];
            v[6] = v[5];
            v[5] = v[4];
            v[4] = v[3].wrapping_add(t1);
            v[3] = v[2];
            v[2] = v[1];
            v[1] = v[0];
            v[0] = t1.wrapping_add(t2);
        }
        for i in 0..8 {
            h[i] = h[i].wrapping_add(v[i]);
        }
    };
    let full = data.len() / 64;
    for i in 0..full {
        block(&mut h, &data[i * 64..i * 64 + 64]);
    }
    let mut tail = data[full * 64..].to_vec();
    tail.push(0x80);
    while tail.len() % 64 != 56 {
        tail.push(0);
    }
    tail.extend_from_slice(&((data.len() as u64) * 8).to_be_bytes());
    for c in tail.chunks(64) {
        block(&mut h, c);
    }
    let mut out = [0u8; 32];
    for i in 0..8 {
        out[4 * i..4 * i + 4].copy_from_slice(&h[i].to_be_bytes());
    }
    out
}

pub fn hex(b: &[u8]) -> String {
    b.iter().map(|v| format!("{v:02x}")).collect()
}

// ---------------------------------------------------------------- command line

fn arg(args: &[String], key: &str) -> Option<String> {
    args.iter()
        .position(|a| a == key)
        .and_then(|i| args.get(i + 1).cloned())
}

pub fn run_build(args: &[String]) -> Result<(), String> {
    let stage = args.get(2).map(|s| s.as_str()).unwrap_or("");
    let gb = PathBuf::from(arg(args, "--gb").ok_or("--gb DIR (sources.json, originals/, originals.sha256)")?);
    let threads: usize = arg(args, "--threads")
        .map(|v| v.parse().map_err(|_| "bad --threads"))
        .transpose()?
        .unwrap_or(1);
    let plan_path = arg(args, "--plan")
        .map(PathBuf::from)
        .unwrap_or_else(|| gb.join("plan_v3.json"));
    let t0 = std::time::Instant::now();
    match stage {
        "plan" => {
            let verbose = args.iter().any(|a| a == "--verbose");
            println!("{}", plan(&gb, &plan_path, threads, verbose)?);
        }
        "cut" => {
            let out = PathBuf::from(arg(args, "--out").ok_or("--out BANKDIR")?);
            println!("{}", cut(&gb, &plan_path, &out, threads)?);
        }
        _ => return Err("grainbank-build plan|cut --gb DIR [--plan F] [--out BANKDIR] [--threads N]".into()),
    }
    eprintln!("{stage}: {:.1} s", t0.elapsed().as_secs_f64());
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lcg(seed: u64) -> impl FnMut() -> f64 {
        let mut s = seed;
        move || {
            s = s.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            ((s >> 11) as f64 / (1u64 << 53) as f64) * 2.0 - 1.0
        }
    }

    fn noise(n: usize, amp: f64, seed: u64) -> Vec<f64> {
        let mut r = lcg(seed);
        (0..n).map(|_| amp * r()).collect()
    }

    #[test]
    fn sha256_known_answers() {
        assert_eq!(
            hex(&sha256(b"")),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            hex(&sha256(b"abc")),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        let long = vec![b'a'; 1000];
        assert_eq!(
            hex(&sha256(&long)),
            "41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3"
        );
    }

    #[test]
    fn denoise_removes_a_steady_bed_and_keeps_a_tone() {
        // a 440 Hz tone over white noise; the bed is the same noise process alone
        let n = 3 * 44100;
        let bed = noise(44100, 0.05, 1);
        let nz = noise(n, 0.05, 2);
        let tone: Vec<f64> = (0..n).map(|i| 0.3 * m::sin(m::TAU * 440.0 * i as f64 / SR)).collect();
        let x: Vec<f64> = tone.iter().zip(&nz).map(|(a, b)| a + b).collect();
        let y = denoise(&x, &bed, 1.5, 0.1);
        assert_eq!(y.len(), x.len());
        // the residual (y - tone) is far below the noise; the tone survives (middle second, away from the edges)
        let mid = 44100..2 * 44100;
        let res: Vec<f64> = mid.clone().map(|i| y[i] - tone[i]).collect();
        let nzm: Vec<f64> = mid.clone().map(|i| nz[i]).collect();
        let ton: Vec<f64> = mid.clone().map(|i| tone[i]).collect();
        // (the reference on the same kind of signal, numpy's uniform noise: noise -8.8 dB, tone -0.003 dB, the bed
        // through itself -9.2 dB: the smoothed gain lets the noise peaks through partly)
        let red = rms_db(&res) - rms_db(&nzm);
        assert!(red < -7.5 && red > -10.0, "noise reduced by {red:.1} dB");
        let ym: Vec<f64> = mid.map(|i| y[i]).collect();
        assert!((rms_db(&ym) - rms_db(&ton)).abs() < 0.05);
        let z = denoise(&bed, &bed, 1.5, 0.1);
        let zb = rms_db(&z) - rms_db(&bed);
        assert!(zb < -8.0 && zb > -10.5, "bed through itself {zb:.1} dB");
        // a bed under 4096 samples: untouched
        assert_eq!(denoise(&x[..5000], &bed[..4000], 1.5, 0.1), x[..5000].to_vec());
        // deterministic
        assert_eq!(denoise(&x, &bed, 1.5, 0.1), y);
    }

    #[test]
    fn stft_round_trip_is_transparent() {
        let x = noise(20000, 0.5, 3);
        let (xs, nfr) = stft(&x);
        let y = istft(&xs, nfr, x.len());
        for (a, b) in x.iter().zip(&y) {
            assert!((a - b).abs() < 1e-12);
        }
    }

    #[test]
    fn hf_gate_ducks_highs_only_while_the_lows_are_quiet() {
        // first second: loud 200 Hz + 3 kHz; second second: the 200 Hz 40 dB down, the 3 kHz unchanged
        let n = 2 * 44100;
        let x: Vec<f64> = (0..n)
            .map(|i| {
                let t = i as f64 / SR;
                let lo = if i < 44100 { 0.5 } else { 0.005 };
                lo * m::sin(m::TAU * 200.0 * t) + 0.05 * m::sin(m::TAU * 3000.0 * t)
            })
            .collect();
        let y = hf_gate(&x);
        let hi = |s: &[f64]| rms_db(&bandpass_fft(s, 2000.0, 5000.0));
        let lo = |s: &[f64]| rms_db(&bandpass_fft(s, 100.0, 400.0));
        let a = 11025..33075;
        let b = 55125..77175;
        // the reference (gbdsp.hf_gate + wxlib.bandpass_fft on this same signal) gives -0.804 dB in the loud part (the
        // gate still releasing from the zero padding before the start, 150 ms), -18.000 dB in the quiet part (deficit
        // 40 dB - knee 15 > depth 18) and lows unchanged
        let loud = hi(&y[a.clone()]) - hi(&x[a.clone()]);
        assert!((loud + 0.804).abs() < 0.01, "loud part {loud:.3} dB");
        let duck = hi(&y[b.clone()]) - hi(&x[b.clone()]);
        assert!((duck + 18.0).abs() < 0.01, "ducked {duck:.3} dB");
        assert!((lo(&y[b.clone()]) - lo(&x[b.clone()])).abs() < 1e-6);
        // later in the loud part the release is done
        let c = 39690..44100;
        assert!((hi(&y[c.clone()]) - hi(&x[c])).abs() < 0.15);
    }

    #[test]
    fn soft_clip_is_linear_then_saturates() {
        let mut x = vec![0.0, 0.2, -0.49, 0.6, -0.9, 2.0, -50.0];
        let orig = x.clone();
        soft_clip(&mut x, 0.98);
        assert_eq!(&x[..3], &orig[..3]);
        for (a, b) in x.iter().zip(&orig) {
            assert!(*b == 0.0 || (a.abs() <= 0.98 && a.signum() == b.signum()));
            assert!(a.abs() <= b.abs());
        }
        let k = 0.49;
        assert!((x[3] - (k + k * m::tanh((0.6 - k) / k))).abs() < 1e-15);
        assert!(x[6] < -0.979);
    }

    #[test]
    fn compress_reduces_only_above_threshold() {
        // 0.5 s quiet then 0.5 s 20 dB louder: threshold between, ratio 4
        let n = 44100;
        let x: Vec<f64> = (0..n)
            .map(|i| (if i < n / 2 { 0.01 } else { 0.1 }) * m::sin(m::TAU * 500.0 * i as f64 / SR))
            .collect();
        let lq = 20.0 * m::log10(0.01 / m::sqrt(2.0));
        let thr = lq + 10.0;
        let (y, gr) = compress(&x, thr, 4.0, 1.0, 150.0, 2.0, 6.0);
        // quiet half untouched (well before the step)
        for i in 1000..n / 2 - 1000 {
            assert!((y[i] - x[i]).abs() < 1e-12);
        }
        // loud half: 10 dB over -> 7.5 dB of reduction once the attack has settled
        assert!((gr - 7.5).abs() < 0.2, "max GR {gr}");
        let tail = &y[n - 4410..];
        let red = rms_db(tail) - rms_db(&x[n - 4410..]);
        assert!((red + 7.5).abs() < 0.3, "reduction {red}");
    }

    #[test]
    fn fade_and_max_rms() {
        let mut g = vec![1.0; 44100];
        fade(&mut g, 2.0, 700.0);
        assert_eq!(g[0], 0.0);
        assert!(g[87] > 0.999 && g[88] == 1.0);
        assert!(g[44099].abs() < 1e-30);
        let x: Vec<f64> = (0..44100)
            .map(|i| if (10000..10000 + 17640).contains(&i) { 0.5 } else { 0.0 })
            .collect();
        assert!((max_rms(&x) - 0.5).abs() < 1e-12);
    }

    #[test]
    fn percentile_matches_numpy() {
        let v = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0];
        assert!((percentile(&v, 10.0) - 1.0).abs() < 1e-12);
        assert!((percentile(&v, 50.0) - 3.5).abs() < 1e-12);
        assert!((percentile(&v, 99.0) - 8.79).abs() < 1e-12);
    }

    #[test]
    fn finds_events_in_a_synthetic_take() {
        // 60 s of quiet noise with three decaying bursts at 10 s, 25 s and 45 s (the last 6 dB quieter)
        let n = 60 * 44100;
        let mut x = noise(n, 0.001, 4);
        let mut r = lcg(5);
        for (t, amp) in [(10.0, 0.5), (25.0, 0.5), (45.0, 0.25)] {
            let s = (t * SR) as usize;
            for i in 0..(3 * 44100) {
                x[s + i] += amp * m::exp(-(i as f64) / (0.6 * SR)) * r();
            }
        }
        let ev = find_thunder_events(&x, 6, 10.0, 10.0);
        assert_eq!(ev.len(), 3, "{ev:?}");
        let mut on: Vec<f64> = ev.iter().map(|e| e.onset_t).collect();
        on.sort_by(|a, b| a.total_cmp(b));
        for (o, t) in on.iter().zip([10.0, 25.0, 45.0]) {
            assert!((o - t).abs() <= 0.1, "onset {o} vs {t}");
        }
        // loudest first; SNR well over the strike threshold
        assert!(ev[0].snr_db > 40.0 && ev[2].onset_t > 44.0);
        // min gap: a second burst 5 s after the first is not a separate event
        let ev2 = find_thunder_events(&x, 6, 30.0, 10.0);
        assert_eq!(ev2.len(), 2);
    }

    #[test]
    fn screening_passes_noise_bursts_and_rejects_a_whistle() {
        let n = 15 * 44100;
        let mut r = lcg(6);
        let x: Vec<f64> = (0..n)
            .map(|i| 0.2 * m::exp(-(i as f64) / (2.0 * SR)) * r() + 0.003 * r())
            .collect();
        let (ok, why) = screen_window(&x, "thunder_close");
        assert!(ok, "{why:?}");
        // a steady 4 kHz whistle on top: tonal tracks / steady whine
        let y: Vec<f64> = x
            .iter()
            .enumerate()
            .map(|(i, v)| v + 0.05 * m::sin(m::TAU * 4000.0 * i as f64 / SR))
            .collect();
        let (ok, why) = screen_window(&y, "thunder_close");
        assert!(
            !ok && why.iter().any(|w| w.contains("tonal") || w.contains("whine")),
            "{why:?}"
        );
        // 50 Hz mains hum with harmonics
        let z: Vec<f64> = x
            .iter()
            .enumerate()
            .map(|(i, v)| {
                let t = i as f64 / SR;
                v + 0.02 * (m::sin(m::TAU * 50.0 * t) + m::sin(m::TAU * 100.0 * t) + m::sin(m::TAU * 150.0 * t))
            })
            .collect();
        let (ok, why) = screen_window(&z, "thunder_close");
        assert!(!ok && why.iter().any(|w| w == "mains hum"), "{why:?}");
    }

    #[test]
    fn text_rules() {
        let mk = |t: &str, d: &str, tags: &[&str]| Source {
            id: "1".into(),
            cls: "thunder_close".into(),
            title: t.into(),
            author: String::new(),
            license: String::new(),
            page: String::new(),
            checked: String::new(),
            description: d.into(),
            tags: tags.iter().map(|s| s.to_string()).collect(),
            status: "clean".into(),
        };
        assert_eq!(text_excluded(&mk("Thunder clap", "open field", &["weather"])), None);
        assert!(text_excluded(&mk("Thunder from the porch", "", &[])).is_some());
        assert!(text_excluded(&mk("Thunder", "heard in my car", &[])).is_some());
        assert_eq!(
            text_excluded(&mk("Thunder", "scary cartoon", &[])),
            None,
            "car needs word boundaries"
        );
        assert!(text_excluded(&mk("Thunder", "", &["sound-effects"])).is_some());
        assert!(text_excluded(&mk("Thunder", "", &["effect"])).is_some());
        assert_eq!(text_excluded(&mk("Thunder", "", &["effective"])), None);
        assert!(text_excluded(&mk("Thunder", "", &["eff"])).is_some());
        assert!(
            text_excluded(&mk("Rain", "thunder", &[])).is_some(),
            "title must say thunder"
        );
        assert_eq!(text_excluded(&mk("LIGHTNING strike", "", &[])), None);
    }

    #[test]
    fn json_layout_and_floats() {
        assert_eq!(py_float(13.0), "13.0");
        assert_eq!(py_float(0.2394), "0.2394");
        assert_eq!(py_float(-233.2), "-233.2");
        assert_eq!(py_float(1e-5), "1e-05");
        assert_eq!(py_float(1e16), "1e+16");
        assert_eq!(py_float(195.8504681183423), "195.8504681183423");
        let mut s = String::new();
        emit(
            &Out::Obj(vec![
                ("a".into(), Out::Arr(vec![Out::I(2), Out::F(0.5)])),
                ("b".into(), Out::S("é\"".into())),
            ]),
            0,
            &mut s,
        );
        assert_eq!(s, "{\n \"a\": [\n  2,\n  0.5\n ],\n \"b\": \"\\u00e9\\\"\"\n}");
        assert_eq!(rnd(0.23944999, 4), 0.2394);
        assert_eq!(arange(10.0, 20.0, 3.0), vec![10.0, 13.0, 16.0, 19.0]);
        assert!(arange(10.0, 5.0, 1.0).is_empty());
    }
}
