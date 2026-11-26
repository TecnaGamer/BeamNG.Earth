//! The live output bus after the mix (~/bnge-tests/weather/thunder_research/CLOSE_STRIKE_AND_LOUDNESS.md, "Live"):
//! a look-ahead limiter (2 ms, ceiling -1.5 dBTP, 4x oversampled detection, 100 ms release), then an antiderivative
//! anti-aliased (ADAA) soft clip from 0.9 of full scale, then a clamp, then a windowed-sinc resampler to the device rate
//! (never linear). Pure and deterministic: f64 inside, a fixed order; no allocation per sample.

use crate::m;

pub const LIM_LOOK_S: f64 = 0.002;
pub const LIM_CEIL_DB: f64 = -1.5;
pub const LIM_RELEASE_S: f64 = 0.1;
pub const CLIP_KNEE: f64 = 0.9;

/// The look-ahead limiter: delays the signal by the look-ahead and applies a gain that is at most ceiling / the
/// oversampled peak at every sample it reaches (a sliding minimum over the look-ahead, averaged over it: a smooth
/// attack that is down in time), released exponentially.
pub struct Limiter {
    look: usize,
    ceil: f64,
    rel: f64,
    hist: [f64; 3],         // the last 3 input samples (for the 4x detection)
    delay: Vec<f64>,        // the signal, delayed by `look`
    di: usize,
    gt: Vec<f64>,           // the target gains of the last look+1 samples (ring)
    gi: usize,
    sum: f64,               // the running sum of the sliding minimum over `look` samples
    mins: Vec<f64>,         // the sliding minimum's last `look` values (ring)
    mi: usize,
    g: f64,
    pub engaged: u64,       // times the gain went under 0.999 from above it
    was_on: bool,
}

/// Catmull-Rom at t in [0, 1] between p1 and p2.
fn cr(p0: f64, p1: f64, p2: f64, p3: f64, t: f64) -> f64 {
    let t2 = t * t;
    0.5 * (2.0 * p1 + (p2 - p0) * t + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2 + (3.0 * p1 - p0 - 3.0 * p2 + p3) * t2 * t)
}

impl Limiter {
    pub fn new(fs: f64) -> Limiter {
        let look = ((LIM_LOOK_S * fs) as usize).max(2);
        Limiter {
            look,
            ceil: m::undb(LIM_CEIL_DB),
            rel: m::exp(-1.0 / (LIM_RELEASE_S * fs)),
            hist: [0.0; 3],
            delay: vec![0.0; look],
            di: 0,
            gt: vec![1.0; look + 1],
            gi: 0,
            sum: look as f64,
            mins: vec![1.0; look],
            mi: 0,
            g: 1.0,
            engaged: 0,
            was_on: false,
        }
    }

    /// One sample in, the sample `look` earlier out, limited.
    pub fn tick(&mut self, x: f64) -> f64 {
        // 4x oversampled peak between the previous sample and this one (Catmull-Rom on the last four)
        let (p0, p1, p2) = (self.hist[0], self.hist[1], self.hist[2]);
        let p3 = x;
        let mut pk = p2.abs().max(p3.abs());
        for t in [0.25, 0.5, 0.75] {
            pk = pk.max(cr(p0, p1, p2, p3, t).abs());
        }
        self.hist = [p1, p2, p3];
        let target = if pk > self.ceil { self.ceil / pk } else { 1.0 };
        // sliding minimum of the targets over the look-ahead (+1), then its running mean over the look-ahead
        self.gt[self.gi] = target;
        self.gi = (self.gi + 1) % self.gt.len();
        let mn = self.gt.iter().fold(1.0f64, |a, v| a.min(*v));
        self.sum += mn - self.mins[self.mi];
        self.mins[self.mi] = mn;
        self.mi = (self.mi + 1) % self.look;
        // (every minimum averaged covers the sample leaving the delay now: the mean is under its target)
        let avg = (self.sum / self.look as f64).min(1.0);
        // never above the mean (down in time), released exponentially when it rises
        self.g = if avg < self.g { avg } else { avg + (self.g - avg) * self.rel };
        let on = self.g < 0.999;
        if on && !self.was_on {
            self.engaged += 1;
        }
        self.was_on = on;
        let out = self.delay[self.di];
        self.delay[self.di] = x;
        self.di = (self.di + 1) % self.look;
        out * self.g
    }
}

/// The soft clip (linear to the knee, then tanh to full scale) with first-order antiderivative anti-aliasing.
pub struct SoftClip {
    prev: f64,
}

fn clip_f(x: f64) -> f64 {
    let a = CLIP_KNEE;
    let ax = x.abs();
    if ax <= a {
        x
    } else {
        x.signum() * (a + (1.0 - a) * m::tanh((ax - a) / (1.0 - a)))
    }
}

/// The antiderivative of clip_f (even).
fn clip_ad(x: f64) -> f64 {
    let a = CLIP_KNEE;
    let ax = x.abs();
    if ax <= a {
        0.5 * x * x
    } else {
        let k = 1.0 - a;
        let u = (ax - a) / k;
        // ln cosh u, stable: |u| + ln((1 + e^-2|u|) / 2)
        let lncosh = u + m::ln((1.0 + m::exp(-2.0 * u)) * 0.5);
        0.5 * a * a + a * (ax - a) + k * k * lncosh
    }
}

impl SoftClip {
    pub fn new() -> SoftClip {
        SoftClip { prev: 0.0 }
    }
    pub fn tick(&mut self, x: f64) -> f64 {
        let d = x - self.prev;
        let y = if d.abs() < 1e-7 { clip_f(0.5 * (x + self.prev)) } else { (clip_ad(x) - clip_ad(self.prev)) / d };
        self.prev = x;
        y.clamp(-1.0, 1.0)
    }
}

/// A polyphase windowed-sinc resampler (16 taps, 256 phases with linear interpolation between them, Blackman window,
/// the cutoff under the lower Nyquist). Fed blocks at the input rate; pulled one output sample at a time.
pub struct Resampler {
    step: f64, // input samples per output sample
    table: Vec<[f64; 16]>,
    buf: Vec<f64>,
    pos: f64, // the next output's position in buf
    pass: bool,
}

const TAPS: usize = 16;
const PHASES: usize = 256;

impl Resampler {
    pub fn new(fs_in: f64, fs_out: f64) -> Resampler {
        let pass = (fs_in - fs_out).abs() < 1e-9;
        let cut = (fs_out / fs_in).min(1.0) * 0.94;
        let mut table = Vec::with_capacity(PHASES + 1);
        for p in 0..=PHASES {
            let frac = p as f64 / PHASES as f64;
            let mut t = [0.0f64; TAPS];
            let mut s = 0.0;
            for (k, tv) in t.iter_mut().enumerate() {
                let x = k as f64 - (TAPS / 2 - 1) as f64 - frac; // tap k sits at offset k - 7 from the base
                let sinc = if x.abs() < 1e-12 { 1.0 } else { m::sin(m::PI * cut * x) / (m::PI * cut * x) };
                let w_pos = (x + TAPS as f64 / 2.0) / TAPS as f64;
                let w = if (0.0..=1.0).contains(&w_pos) {
                    0.42 - 0.5 * m::cos(m::TAU * w_pos) + 0.08 * m::cos(2.0 * m::TAU * w_pos)
                } else {
                    0.0
                };
                *tv = sinc * w;
                s += *tv;
            }
            for tv in t.iter_mut() {
                *tv /= s;
            }
            table.push(t);
        }
        Resampler { step: fs_in / fs_out, table, buf: vec![0.0; TAPS / 2], pos: (TAPS / 2 - 1) as f64, pass }
    }

    pub fn push(&mut self, x: &[f64]) {
        self.buf.extend_from_slice(x);
    }

    /// The next output sample, or None until more input is pushed.
    pub fn pull(&mut self) -> Option<f64> {
        if self.pass {
            let i = self.pos as usize;
            if i >= self.buf.len() {
                self.compact();
                return None;
            }
            self.pos += 1.0;
            return Some(self.buf[i]);
        }
        let base = self.pos.floor() as usize;
        if base + TAPS / 2 >= self.buf.len() {
            self.compact();
            return None;
        }
        let frac = self.pos - base as f64;
        let ph = frac * PHASES as f64;
        let pi = (ph as usize).min(PHASES - 1);
        let pf = ph - pi as f64;
        let (a, b) = (&self.table[pi], &self.table[pi + 1]);
        let start = base + 1 - TAPS / 2;
        let mut acc = 0.0;
        for k in 0..TAPS {
            acc += (a[k] + (b[k] - a[k]) * pf) * self.buf[start + k];
        }
        self.pos += self.step;
        Some(acc)
    }

    /// Input samples still to be used (their time is part of the output latency).
    pub fn pending(&self) -> f64 {
        (self.buf.len() as f64 - self.pos).max(0.0)
    }

    fn compact(&mut self) {
        let keep_from = (self.pos.floor() as usize).saturating_sub(TAPS);
        if keep_from > 4096 {
            self.buf.drain(..keep_from);
            self.pos -= keep_from as f64;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn limiter_holds_the_ceiling_and_leaves_quiet_alone() {
        let fs = 44100.0;
        let mut l = Limiter::new(fs);
        let ceil = m::undb(LIM_CEIL_DB);
        let x: Vec<f64> = (0..44100).map(|i| m::sin(i as f64 * 0.2) * if (20000..21000).contains(&i) { 1.6 } else { 0.3 }).collect();
        let y: Vec<f64> = x.iter().map(|v| l.tick(*v)).collect();
        let peak = y.iter().fold(0.0f64, |a, v| a.max(v.abs()));
        assert!(peak <= ceil * 1.001, "peak {peak} over the ceiling {ceil}");
        assert_eq!(l.engaged, 1, "one burst, one engagement");
        // quiet before the burst: delayed by the look-ahead, untouched
        let d = (LIM_LOOK_S * fs) as usize;
        for i in 1000..2000 {
            assert!((y[i + d] - x[i]).abs() < 1e-12);
        }
    }

    #[test]
    fn softclip_is_linear_below_the_knee_and_bounded() {
        let mut c = SoftClip::new();
        let mut prev_in = 0.0;
        for i in 0..1000 {
            let x = 0.5 * m::sin(i as f64 * 0.01);
            let y = c.tick(x);
            if i > 0 {
                assert!((y - 0.5 * (x + prev_in)).abs() < 1e-3, "ADAA below the knee: the half-sample average");
            }
            prev_in = x;
        }
        let mut c = SoftClip::new();
        for i in 0..1000 {
            let y = c.tick(3.0 * m::sin(i as f64 * 0.05));
            assert!(y.abs() <= 1.0);
        }
    }

    #[test]
    fn resampler_passes_a_tone_and_is_deterministic() {
        let fs_in = 44100.0;
        let fs_out = 48000.0;
        let f = 1000.0;
        let mut r = Resampler::new(fs_in, fs_out);
        let x: Vec<f64> = (0..44100).map(|i| m::sin(m::TAU * f * i as f64 / fs_in)).collect();
        let mut out = Vec::new();
        for c in x.chunks(512) {
            r.push(c);
            while let Some(v) = r.pull() {
                out.push(v);
            }
        }
        assert!((out.len() as f64 - 48000.0).abs() < 40.0, "{}", out.len());
        // output i sits at input index i * step - 1 (8 zeros lead the input, the first output at position 7)
        let mut err = 0.0f64;
        for (i, v) in out.iter().enumerate().skip(100).take(40000) {
            let t = i as f64 / fs_out - 1.0 / fs_in;
            err = err.max((v - m::sin(m::TAU * f * t)).abs());
        }
        assert!(err < 0.01, "resampled tone error {err}");
        let mut p = Resampler::new(44100.0, 44100.0);
        p.push(&[1.0, 2.0, 3.0]);
        let got: Vec<f64> = std::iter::from_fn(|| p.pull()).collect();
        assert_eq!(&got[got.len() - 3..], &[1.0, 2.0, 3.0], "the same rate: passed through");
    }
}
