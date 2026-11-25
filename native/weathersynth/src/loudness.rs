//! ITU-R BS.1770-4 / EBU R128 loudness (K-weighting, gated integrated loudness, short-term 3 s), true peak by 4x
//! oversampling, and a true-peak look-ahead limiter. Used to hit the class targets inside the generator; the results are
//! checked against ffmpeg's ebur128 filter.

use crate::dsp::Biquad;
use crate::m;

/// The K-weighting pre-filter and RLB high-pass for any sample rate (libebur128's analog-prototype designs).
pub fn kweight(x: &[f64], fs: f64) -> Vec<f64> {
    let f0 = 1681.974450955533;
    let g = 3.999843853973347;
    let q = 0.7071752369554196;
    let k = m::tan(m::PI * f0 / fs);
    let vh = m::pow(10.0, g / 20.0);
    let vb = m::pow(vh, 0.4996667741545416);
    let a0 = 1.0 + k / q + k * k;
    let mut s1 = Biquad::new(
        (vh + vb * k / q + k * k) / a0,
        2.0 * (k * k - vh) / a0,
        (vh - vb * k / q + k * k) / a0,
        2.0 * (k * k - 1.0) / a0,
        (1.0 - k / q + k * k) / a0,
    );
    let f0 = 38.13547087602444;
    let q = 0.5003270373238773;
    let k = m::tan(m::PI * f0 / fs);
    let d = 1.0 + k / q + k * k;
    let mut s2 = Biquad::new(1.0, -2.0, 1.0, 2.0 * (k * k - 1.0) / d, (1.0 - k / q + k * k) / d);
    let mut y = x.to_vec();
    s1.run(&mut y);
    s2.run(&mut y);
    y
}

fn lufs_of(ms: f64) -> f64 {
    -0.691 + 10.0 * m::log10(ms.max(1e-30))
}

/// Mean squares of 400 ms blocks every 100 ms of K-weighted samples.
fn block_ms(kw: &[f64], fs: f64, win_s: f64) -> Vec<f64> {
    let win = (win_s * fs).round() as usize;
    let hop = (0.1 * fs).round() as usize;
    let mut out = Vec::new();
    if kw.len() < win {
        let s: f64 = kw.iter().map(|v| v * v).sum();
        out.push(s / win as f64);
        return out;
    }
    // prefix sums of squares (exact order: left to right)
    let mut pre = Vec::with_capacity(kw.len() + 1);
    pre.push(0.0f64);
    let mut acc = 0.0;
    for v in kw {
        acc += v * v;
        pre.push(acc);
    }
    let mut i = 0;
    while i + win <= kw.len() {
        out.push((pre[i + win] - pre[i]) / win as f64);
        i += hop;
    }
    out
}

/// Gated integrated loudness. `circular`: the signal is a loop; it is measured played twice (as SOURCES.md does).
pub fn integrated(x: &[f64], fs: f64, circular: bool) -> f64 {
    let sig: Vec<f64> = if circular { x.iter().chain(x.iter()).cloned().collect() } else { x.to_vec() };
    let kw = if circular {
        // filter state settled: run the doubled loop after a warm-up copy
        let tri: Vec<f64> = x.iter().chain(sig.iter()).cloned().collect();
        kweight(&tri, fs)[x.len()..].to_vec()
    } else {
        kweight(&sig, fs)
    };
    let blocks = block_ms(&kw, fs, 0.4);
    let abs: Vec<f64> = blocks.into_iter().filter(|&b| lufs_of(b) > -70.0).collect();
    if abs.is_empty() {
        return -100.0;
    }
    let mean_abs = abs.iter().sum::<f64>() / abs.len() as f64;
    let rel = lufs_of(mean_abs) - 10.0;
    let gated: Vec<f64> = abs.into_iter().filter(|&b| lufs_of(b) > rel).collect();
    if gated.is_empty() {
        return -100.0;
    }
    lufs_of(gated.iter().sum::<f64>() / gated.len() as f64)
}

/// The loudest short-term (3 s) loudness, windows every 100 ms.
pub fn max_short_term(x: &[f64], fs: f64) -> f64 {
    let kw = kweight(x, fs);
    block_ms(&kw, fs, 3.0).into_iter().map(lufs_of).fold(-100.0, f64::max)
}

/// Windowed-sinc 4x interpolator taps (48 taps per phase, Blackman-Harris window), built once.
fn interp_taps() -> Vec<[f64; 48]> {
    let mut phases = Vec::new();
    for p in 0..4 {
        let mut t = [0.0f64; 48];
        let frac = p as f64 / 4.0;
        let mut sum = 0.0;
        for (i, tv) in t.iter_mut().enumerate() {
            let k = i as f64 - 23.0 - frac; // tap position relative to the output point
            let sinc = if k.abs() < 1e-12 { 1.0 } else { m::sin(m::PI * k) / (m::PI * k) };
            let w_pos = (i as f64 + 1.5 - frac) / 49.0; // window centred on the output point
            let w = 0.35875 - 0.48829 * m::cos(m::TAU * w_pos) + 0.14128 * m::cos(2.0 * m::TAU * w_pos)
                - 0.01168 * m::cos(3.0 * m::TAU * w_pos);
            *tv = sinc * w;
            sum += *tv;
        }
        for tv in t.iter_mut() {
            *tv /= sum;
        }
        phases.push(t);
    }
    phases
}

/// Per original sample, the largest |value| among its 4 oversampled points (the true-peak envelope).
/// `circular` wraps the interpolation around a loop.
pub fn tp_envelope(x: &[f64], circular: bool) -> Vec<f64> {
    let taps = interp_taps();
    let n = x.len() as isize;
    let get = |i: isize| -> f64 {
        if circular {
            x[i.rem_euclid(n) as usize]
        } else if i < 0 || i >= n {
            0.0
        } else {
            x[i as usize]
        }
    };
    let mut env = vec![0.0; x.len()];
    for (j, e) in env.iter_mut().enumerate() {
        let mut mx = x[j].abs();
        for t in taps.iter().skip(1) {
            let mut s = 0.0;
            for (i, tv) in t.iter().enumerate() {
                s += tv * get(j as isize + i as isize - 23);
            }
            mx = mx.max(s.abs());
        }
        *e = mx;
    }
    env
}

pub fn true_peak_db(x: &[f64], circular: bool) -> f64 {
    m::db(tp_envelope(x, circular).into_iter().fold(0.0, f64::max))
}

/// Gain curve of a look-ahead true-peak limiter: never above `ceil / env`, reaching each reduction over `attack`
/// samples before the peak (sliding minimum, then a moving average of the same length), recovering with a one-pole
/// release. `env` is the true-peak envelope of the input already multiplied by its make-up gain.
pub fn limiter_gain(env: &[f64], ceil: f64, attack: usize, release_s: f64, fs: f64) -> Vec<f64> {
    let n = env.len();
    let req: Vec<f64> = env.iter().map(|&e| if e > ceil { ceil / e } else { 1.0 }).collect();
    let l = attack.max(1);
    // sliding minimum over [i, i + l)
    let mut smin = vec![1.0; n];
    let mut dq: std::collections::VecDeque<usize> = std::collections::VecDeque::new();
    for i in (0..n).rev() {
        while let Some(&b) = dq.back() {
            if req[b] >= req[i] {
                dq.pop_back();
            } else {
                break;
            }
        }
        dq.push_back(i);
        while let Some(&f) = dq.front() {
            if f >= i + l {
                dq.pop_front();
            } else {
                break;
            }
        }
        smin[i] = req[*dq.front().unwrap()];
    }
    // moving average over (i - l, i]: each term's window contains i, so the mean is <= req[i]
    let mut avg = vec![1.0; n];
    let mut acc = 0.0;
    for i in 0..n {
        acc += smin[i];
        if i >= l {
            acc -= smin[i - l];
        }
        let cnt = (i + 1).min(l);
        // windows before the start count as 1.0 (no reduction)
        avg[i] = (acc + (l - cnt) as f64) / l as f64;
        avg[i] = avg[i].min(req[i]);
    }
    let rc = m::exp(-1.0 / (release_s * fs));
    let mut g = vec![1.0; n];
    let mut prev = 1.0;
    for i in 0..n {
        let rel = 1.0 - (1.0 - prev) * rc;
        let v = avg[i].min(rel).min(req[i]);
        g[i] = v;
        prev = v;
    }
    g
}

/// Apply make-up gain and limit to `ceil_db` true peak. For a loop, three copies are limited and the middle one kept.
pub fn gain_limit(x: &[f64], gain: f64, ceil_db: f64, attack_s: f64, release_s: f64, fs: f64, circular: bool) -> Vec<f64> {
    let ceil = m::undb(ceil_db);
    let attack = (attack_s * fs).round() as usize;
    if circular {
        let tri: Vec<f64> = x.iter().chain(x.iter()).chain(x.iter()).map(|v| v * gain).collect();
        let env = tp_envelope(&tri, true);
        let g = limiter_gain(&env, ceil, attack, release_s, fs);
        let n = x.len();
        (0..n).map(|i| tri[n + i] * g[n + i]).collect()
    } else {
        let y: Vec<f64> = x.iter().map(|v| v * gain).collect();
        let env = tp_envelope(&y, false);
        let g = limiter_gain(&env, ceil, attack, release_s, fs);
        y.iter().zip(g.iter()).map(|(a, b)| a * b).collect()
    }
}
