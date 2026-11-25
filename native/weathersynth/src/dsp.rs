//! Small DSP kit: radix-2 FFT, biquads (run once or around a loop), minimum-phase spectra, FFT convolution.

use crate::m;

#[derive(Clone, Copy, Debug, Default)]
pub struct C {
    pub re: f64,
    pub im: f64,
}

impl C {
    pub fn new(re: f64, im: f64) -> C {
        C { re, im }
    }
    #[inline]
    pub fn mul(self, o: C) -> C {
        C::new(self.re * o.re - self.im * o.im, self.re * o.im + self.im * o.re)
    }
    #[inline]
    pub fn add(self, o: C) -> C {
        C::new(self.re + o.re, self.im + o.im)
    }
    #[inline]
    pub fn scale(self, s: f64) -> C {
        C::new(self.re * s, self.im * s)
    }
    pub fn expc(self) -> C {
        let r = m::exp(self.re);
        C::new(r * m::cos(self.im), r * m::sin(self.im))
    }
}

pub fn next_pow2(n: usize) -> usize {
    let mut p = 1;
    while p < n {
        p <<= 1;
    }
    p
}

/// In-place iterative radix-2 FFT. `inverse` uses +i and divides by n.
pub fn fft(a: &mut [C], inverse: bool) {
    let n = a.len();
    assert!(n.is_power_of_two());
    let mut j = 0usize;
    for i in 1..n {
        let mut bit = n >> 1;
        while j & bit != 0 {
            j ^= bit;
            bit >>= 1;
        }
        j |= bit;
        if i < j {
            a.swap(i, j);
        }
    }
    let sign = if inverse { 1.0 } else { -1.0 };
    let mut len = 2;
    while len <= n {
        let half = len / 2;
        // twiddles for this stage, each from libm directly (no recurrence drift)
        let tw: Vec<C> = (0..half)
            .map(|k| {
                let ang = sign * m::TAU * k as f64 / len as f64;
                C::new(m::cos(ang), m::sin(ang))
            })
            .collect();
        let mut i = 0;
        while i < n {
            for k in 0..half {
                let u = a[i + k];
                let v = a[i + k + half].mul(tw[k]);
                a[i + k] = u.add(v);
                a[i + k + half] = C::new(u.re - v.re, u.im - v.im);
            }
            i += len;
        }
        len <<= 1;
    }
    if inverse {
        let s = 1.0 / n as f64;
        for x in a.iter_mut() {
            *x = x.scale(s);
        }
    }
}

/// Minimum-phase spectrum (n points, full circle) from a log-magnitude given at the n/2+1 bins 0..=n/2
/// (natural log of |H|). Homomorphic method: real cepstrum folded onto positive quefrencies.
pub fn min_phase(logmag_half: &[f64], n: usize) -> Vec<C> {
    assert_eq!(logmag_half.len(), n / 2 + 1);
    let mut c: Vec<C> = (0..n)
        .map(|k| {
            let kk = if k <= n / 2 { k } else { n - k };
            C::new(logmag_half[kk], 0.0)
        })
        .collect();
    fft(&mut c, true);
    // fold
    for k in 1..n / 2 {
        c[k] = c[k].scale(2.0);
    }
    for k in n / 2 + 1..n {
        c[k] = C::default();
    }
    for x in c.iter_mut() {
        x.im = 0.0;
    }
    fft(&mut c, false);
    c.iter().map(|z| z.expc()).collect()
}

/// Linear convolution of a signal with an impulse response through one FFT pair.
pub fn convolve(x: &[f64], h: &[f64]) -> Vec<f64> {
    let out_len = x.len() + h.len() - 1;
    let n = next_pow2(out_len);
    let mut a: Vec<C> = (0..n).map(|i| C::new(if i < x.len() { x[i] } else { 0.0 }, 0.0)).collect();
    let mut b: Vec<C> = (0..n).map(|i| C::new(if i < h.len() { h[i] } else { 0.0 }, 0.0)).collect();
    fft(&mut a, false);
    fft(&mut b, false);
    for i in 0..n {
        a[i] = a[i].mul(b[i]);
    }
    fft(&mut a, true);
    a[..out_len].iter().map(|z| z.re).collect()
}

/// Direct-form-I biquad.
#[derive(Clone, Copy, Debug)]
pub struct Biquad {
    pub b0: f64,
    pub b1: f64,
    pub b2: f64,
    pub a1: f64,
    pub a2: f64,
    x1: f64,
    x2: f64,
    y1: f64,
    y2: f64,
}

impl Biquad {
    pub fn new(b0: f64, b1: f64, b2: f64, a1: f64, a2: f64) -> Biquad {
        Biquad { b0, b1, b2, a1, a2, x1: 0.0, x2: 0.0, y1: 0.0, y2: 0.0 }
    }
    // RBJ cookbook designs
    pub fn lowpass(fs: f64, f0: f64, q: f64) -> Biquad {
        let w = m::TAU * f0 / fs;
        let (s, c) = (m::sin(w), m::cos(w));
        let al = s / (2.0 * q);
        let a0 = 1.0 + al;
        Biquad::new((1.0 - c) / 2.0 / a0, (1.0 - c) / a0, (1.0 - c) / 2.0 / a0, -2.0 * c / a0, (1.0 - al) / a0)
    }
    pub fn highpass(fs: f64, f0: f64, q: f64) -> Biquad {
        let w = m::TAU * f0 / fs;
        let (s, c) = (m::sin(w), m::cos(w));
        let al = s / (2.0 * q);
        let a0 = 1.0 + al;
        Biquad::new((1.0 + c) / 2.0 / a0, -(1.0 + c) / a0, (1.0 + c) / 2.0 / a0, -2.0 * c / a0, (1.0 - al) / a0)
    }
    /// Band-pass, 0 dB peak gain.
    pub fn bandpass(fs: f64, f0: f64, q: f64) -> Biquad {
        let w = m::TAU * f0 / fs;
        let (s, c) = (m::sin(w), m::cos(w));
        let al = s / (2.0 * q);
        let a0 = 1.0 + al;
        Biquad::new(al / a0, 0.0, -al / a0, -2.0 * c / a0, (1.0 - al) / a0)
    }
    /// Take another filter's state (new coefficients without a click).
    pub fn copy_state(&mut self, o: &Biquad) {
        self.x1 = o.x1;
        self.x2 = o.x2;
        self.y1 = o.y1;
        self.y2 = o.y2;
    }
    pub fn reset(&mut self) {
        self.x1 = 0.0;
        self.x2 = 0.0;
        self.y1 = 0.0;
        self.y2 = 0.0;
    }
    #[inline]
    pub fn tick(&mut self, x: f64) -> f64 {
        let y = self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2 - self.a1 * self.y1 - self.a2 * self.y2;
        self.x2 = self.x1;
        self.x1 = x;
        self.y2 = self.y1;
        self.y1 = y;
        y
    }
    pub fn run(&mut self, x: &mut [f64]) {
        for v in x.iter_mut() {
            *v = self.tick(*v);
        }
    }
    /// Filter a loop as if it had always been playing: warm up on the whole loop, then filter it again. The filters
    /// here decay to below 1e-12 within one 20 s loop, so the result wraps seamlessly.
    pub fn run_circular(&mut self, x: &mut [f64]) {
        self.reset();
        for &v in x.iter() {
            self.tick(v);
        }
        self.run(x);
    }
}

/// One-pole low-pass coefficient for a cutoff.
pub fn onepole_coef(fs: f64, fc: f64) -> f64 {
    1.0 - m::exp(-m::TAU * fc / fs)
}
