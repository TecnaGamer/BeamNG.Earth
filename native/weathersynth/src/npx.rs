//! The numpy functions method B's reference uses, with numpy's conventions: rfft / irfft at any length (radix 2 through
//! dsp::fft; other lengths by a mixed-radix FFT over the factors 2, 3, 5, 7, with a direct DFT for any other factor),
//! rfftfreq, hanning, convolve ('full' / 'same', numpy's alignment), median, interp. Deterministic: libm, a fixed order.

use crate::dsp::{self, C};
use crate::m;

pub const SR: f64 = 44100.0;

fn factors(mut n: usize) -> Vec<usize> {
    let mut f = Vec::new();
    for p in [4usize, 2, 3, 5, 7] {
        while n % p == 0 && n > 1 {
            f.push(p);
            n /= p;
        }
    }
    let mut p = 11;
    while n > 1 {
        while n % p == 0 {
            f.push(p);
            n /= p;
        }
        p += 2;
    }
    f
}

/// exp(sign * 2 pi i k / n), the angle reduced exactly (k mod n) before the trigonometry.
fn tw(k: usize, n: usize, sign: f64) -> C {
    let a = sign * m::TAU * ((k % n) as f64) / n as f64;
    C::new(m::cos(a), m::sin(a))
}

/// A mixed-radix FFT (recursive decimation in time over `fs`), unscaled, without allocation: the sub-transforms read the
/// input by stride and write into their part of `out`, then each butterfly combines p values in place. `table` holds
/// exp(sign 2 pi i k / N) for the top length N; a sub-transform of length n uses every (N / n)-th entry.
fn fft_rec(x: &[C], start: usize, stride: usize, out: &mut [C], fs: &[usize], table: &[C]) {
    let n = out.len();
    if n == 1 {
        out[0] = x[start];
        return;
    }
    let p = fs[0];
    let m_ = n / p;
    for j in 0..p {
        fft_rec(x, start + j * stride, stride * p, &mut out[j * m_..(j + 1) * m_], &fs[1..], table);
    }
    let tstride = table.len() / n;
    let mut s = [C::default(); 16];
    let mut r = [C::default(); 16];
    for k in 0..m_ {
        for (j, sj) in s.iter_mut().enumerate().take(p) {
            *sj = out[j * m_ + k];
        }
        for (q, rq) in r.iter_mut().enumerate().take(p) {
            let idx = k + q * m_;
            let mut acc = s[0];
            let mut t = 0usize; // (j * idx) mod n, kept incrementally
            for sj in s.iter().take(p).skip(1) {
                t += idx;
                if t >= n {
                    t -= n;
                }
                acc = acc.add(sj.mul(table[t * tstride]));
            }
            *rq = acc;
        }
        for (q, rq) in r.iter().enumerate().take(p) {
            out[k + q * m_] = *rq;
        }
    }
}

/// The full twiddle table of a length and direction (each entry from libm directly), the last few kept per thread:
/// the same values as computing them each time.
fn table_for(n: usize, inverse: bool) -> std::rc::Rc<Vec<C>> {
    thread_local! {
        static CACHE: std::cell::RefCell<Vec<(usize, bool, std::rc::Rc<Vec<C>>)>> = const { std::cell::RefCell::new(Vec::new()) };
    }
    CACHE.with(|c| {
        let mut c = c.borrow_mut();
        if let Some(i) = c.iter().position(|e| e.0 == n && e.1 == inverse) {
            let e = c.remove(i);
            let t = e.2.clone();
            c.push(e);
            return t;
        }
        let sign = if inverse { 1.0 } else { -1.0 };
        let t = std::rc::Rc::new((0..n).map(|k| tw(k, n, sign)).collect::<Vec<C>>());
        if c.len() >= 6 {
            c.remove(0);
        }
        c.push((n, inverse, t.clone()));
        t
    })
}

/// A complex FFT of any length (forward; `inverse` uses +i and divides by n, like dsp::fft).
pub fn fft_any(x: &mut Vec<C>, inverse: bool) {
    let n = x.len();
    if n.is_power_of_two() {
        dsp::fft(x, inverse);
        return;
    }
    let fs = factors(n);
    if fs.iter().any(|&p| p > 16) {
        bluestein(x, inverse);
        return;
    }
    let table = table_for(n, inverse);
    let mut y = vec![C::default(); n];
    fft_rec(x, 0, 1, &mut y, &fs, &table);
    let s = if inverse { 1.0 / n as f64 } else { 1.0 };
    for (a, b) in x.iter_mut().zip(y) {
        *a = b.scale(s);
    }
}

/// Bluestein's chirp-z transform for lengths with a prime factor over 16 (method B never has one; the grain-bank builder
/// takes FFTs of arbitrary cut lengths): x_k w_k convolved with conj(w) through power-of-two FFTs, w_k = exp(sign i pi
/// k^2 / n), the angle reduced exactly (k^2 mod 2n) before the trigonometry. Accurate to ~1e-12 relative.
fn bluestein(x: &mut [C], inverse: bool) {
    let n = x.len();
    let sign = if inverse { 1.0 } else { -1.0 };
    let w: Vec<C> = (0..n)
        .map(|k| {
            let k2 = ((k as u128 * k as u128) % (2 * n as u128)) as f64;
            let a = sign * m::PI * k2 / n as f64;
            C::new(m::cos(a), m::sin(a))
        })
        .collect();
    let len = dsp::next_pow2(2 * n - 1);
    let mut a = vec![C::default(); len];
    for k in 0..n {
        a[k] = x[k].mul(w[k]);
    }
    let mut b = vec![C::default(); len];
    b[0] = C::new(w[0].re, -w[0].im);
    for k in 1..n {
        let c = C::new(w[k].re, -w[k].im);
        b[k] = c;
        b[len - k] = c;
    }
    dsp::fft(&mut a, false);
    dsp::fft(&mut b, false);
    for (ai, bi) in a.iter_mut().zip(&b) {
        *ai = ai.mul(*bi);
    }
    dsp::fft(&mut a, true);
    let s = if inverse { 1.0 / n as f64 } else { 1.0 };
    for k in 0..n {
        x[k] = a[k].mul(w[k]).scale(s);
    }
}

/// numpy.fft.rfft(x, n): x zero-padded or cut to n; the n/2 + 1 bins. Even n goes through one complex FFT of n/2 points
/// (the even samples as real parts, the odd as imaginary), untangled with exp(-2 pi i k / n).
pub fn rfft(x: &[f64], n: usize) -> Vec<C> {
    let get = |i: usize| if i < x.len() { x[i] } else { 0.0 };
    if n % 2 != 0 || n < 4 {
        let mut a: Vec<C> = (0..n).map(|i| C::new(get(i), 0.0)).collect();
        fft_any(&mut a, false);
        a.truncate(n / 2 + 1);
        return a;
    }
    let h = n / 2;
    let mut z: Vec<C> = (0..h).map(|k| C::new(get(2 * k), get(2 * k + 1))).collect();
    fft_any(&mut z, false);
    let w = half_twiddles(n, false);
    let mut out = vec![C::default(); h + 1];
    for k in 0..=h {
        let zk = z[k % h];
        let zc = z[(h - k) % h];
        let zc = C::new(zc.re, -zc.im);
        // e = (zk + zc) / 2, o = (zk - zc) / (2i)
        let e = C::new(0.5 * (zk.re + zc.re), 0.5 * (zk.im + zc.im));
        let o = C::new(0.5 * (zk.im - zc.im), -0.5 * (zk.re - zc.re));
        out[k] = e.add(o.mul(w[k]));
    }
    out
}

/// exp(sign 2 pi i k / n) for k = 0 ..= n/2 (each from libm), the last few kept per thread.
fn half_twiddles(n: usize, inverse: bool) -> std::rc::Rc<Vec<C>> {
    thread_local! {
        static CACHE: std::cell::RefCell<Vec<(usize, bool, std::rc::Rc<Vec<C>>)>> = const { std::cell::RefCell::new(Vec::new()) };
    }
    CACHE.with(|c| {
        let mut c = c.borrow_mut();
        if let Some(i) = c.iter().position(|e| e.0 == n && e.1 == inverse) {
            return c[i].2.clone();
        }
        let sign = if inverse { 1.0 } else { -1.0 };
        let t = std::rc::Rc::new((0..=n / 2).map(|k| tw(k, n, sign)).collect::<Vec<C>>());
        if c.len() >= 8 {
            c.remove(0);
        }
        c.push((n, inverse, t.clone()));
        t
    })
}

/// numpy.fft.irfft(X, n): the Hermitian spectrum of n points from its first n/2 + 1 bins (missing bins zero; the
/// imaginary parts of the DC and, for even n, the Nyquist bins ignored, as pocketfft does). Even n through one complex
/// inverse FFT of n/2 points.
pub fn irfft(xf: &[C], n: usize) -> Vec<f64> {
    let hh = n / 2 + 1;
    let get = |k: usize| {
        let mut v = if k < xf.len() { xf[k] } else { C::default() };
        if k == 0 || (n % 2 == 0 && k == n / 2) {
            v.im = 0.0;
        }
        v
    };
    if n % 2 != 0 || n < 4 {
        let mut a = vec![C::default(); n];
        for k in 0..hh.min(n) {
            let v = get(k);
            a[k] = v;
            if k > 0 && n - k != k {
                a[n - k] = C::new(v.re, -v.im);
            }
        }
        fft_any(&mut a, true);
        return a.into_iter().map(|z| z.re).collect();
    }
    let h = n / 2;
    let w = half_twiddles(n, true);
    // Z[k] = E[k] + i O[k], E = (X[k] + conj X[h-k]) / 2, O = (X[k] - conj X[h-k]) exp(+2 pi i k / n) / 2
    let mut z = vec![C::default(); h];
    for (k, zk) in z.iter_mut().enumerate() {
        let a = get(k);
        let b = get(h - k);
        let b = C::new(b.re, -b.im);
        let e = C::new(0.5 * (a.re + b.re), 0.5 * (a.im + b.im));
        let d = C::new(0.5 * (a.re - b.re), 0.5 * (a.im - b.im)).mul(w[k]);
        *zk = C::new(e.re - d.im, e.im + d.re);
    }
    fft_any(&mut z, true);
    let mut out = vec![0.0f64; n];
    for (k, v) in z.iter().enumerate() {
        out[2 * k] = v.re;
        out[2 * k + 1] = v.im;
    }
    out
}

/// numpy.fft.rfftfreq(n, 1 / SR).
pub fn rfftfreq(n: usize) -> Vec<f64> {
    (0..=n / 2).map(|k| k as f64 * SR / n as f64).collect()
}

/// numpy.hanning(m).
pub fn hanning(mlen: usize) -> Vec<f64> {
    if mlen == 1 {
        return vec![1.0];
    }
    (0..mlen).map(|k| 0.5 - 0.5 * m::cos(m::TAU * k as f64 / (mlen - 1) as f64)).collect()
}

/// numpy.convolve(a, v) ('full'): direct when the shorter is small, by FFT otherwise (the same sums within ~1e-15).
pub fn convolve_full(a: &[f64], v: &[f64]) -> Vec<f64> {
    if a.is_empty() || v.is_empty() {
        return Vec::new();
    }
    let (a, v) = if v.len() > a.len() { (v, a) } else { (a, v) };
    let out_len = a.len() + v.len() - 1;
    if v.len() <= 64 {
        let mut y = vec![0.0; out_len];
        for (i, &ai) in a.iter().enumerate() {
            for (j, &vj) in v.iter().enumerate() {
                y[i + j] += ai * vj;
            }
        }
        return y;
    }
    let n = dsp::next_pow2(out_len);
    let fa = rfft(a, n);
    let fv = rfft(v, n);
    let prod: Vec<C> = fa.iter().zip(&fv).map(|(x, y)| x.mul(*y)).collect();
    let mut y = irfft(&prod, n);
    y.truncate(out_len);
    y
}

/// numpy.convolve(a, v, 'same'): max(len) samples from the 'full' result at numpy's offset ((K - 1) / 2, K the
/// shorter's length).
pub fn convolve_same(a: &[f64], v: &[f64]) -> Vec<f64> {
    let full = convolve_full(a, v);
    let (la, k) = if v.len() > a.len() { (v.len(), a.len()) } else { (a.len(), v.len()) };
    let off = (k.max(1) - 1) / 2;
    full[off..off + la].to_vec()
}

/// numpy.median (the mean of the two middle values for an even count).
pub fn median(v: &[f64]) -> f64 {
    let mut s = v.to_vec();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = s.len();
    if n == 0 {
        return f64::NAN;
    }
    if n % 2 == 1 {
        s[n / 2]
    } else {
        0.5 * (s[n / 2 - 1] + s[n / 2])
    }
}

/// numpy.interp(x, xp, fp) for increasing xp (the edges held).
pub fn interp(x: f64, xp: &[f64], fp: &[f64]) -> f64 {
    let n = xp.len();
    if x <= xp[0] {
        return fp[0];
    }
    if x >= xp[n - 1] {
        return fp[n - 1];
    }
    // the last j with xp[j] <= x
    let mut lo = 0;
    let mut hi = n - 1;
    while hi - lo > 1 {
        let mid = (lo + hi) / 2;
        if xp[mid] <= x {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    let slope = (fp[hi] - fp[lo]) / (xp[hi] - xp[lo]);
    slope * (x - xp[lo]) + fp[lo]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mixed_radix_matches_a_dft() {
        for n in [4usize, 6, 8, 12, 30, 63, 70, 120, 77, 1024, 1470, 2048, 251, 502, 1757, 3514] {
            let x: Vec<f64> = (0..n).map(|i| m::sin(i as f64 * 0.7) + 0.3 * m::cos(i as f64 * 2.1)).collect();
            let f = rfft(&x, n);
            for k in 0..=n / 2 {
                let mut acc = C::default();
                for (j, xj) in x.iter().enumerate() {
                    acc = acc.add(C::new(*xj, 0.0).mul(tw(j * k, n, -1.0)));
                }
                assert!((acc.re - f[k].re).abs() < 1e-9 && (acc.im - f[k].im).abs() < 1e-9, "n {n} k {k}");
            }
            let back = irfft(&f, n);
            for (a, b) in x.iter().zip(&back) {
                assert!((a - b).abs() < 1e-12, "n {n}");
            }
        }
    }

    #[test]
    fn convolve_same_alignment_and_helpers() {
        let a: Vec<f64> = (0..50).map(|i| (i as f64 * 0.37).sin()).collect();
        for k in [1usize, 2, 3, 4, 10, 88] {
            let v: Vec<f64> = (0..k).map(|i| 1.0 + i as f64 * 0.1).collect();
            let full = convolve_full(&a, &v);
            let same = convolve_same(&a, &v);
            let la = a.len().max(v.len());
            assert_eq!(same.len(), la);
            if k <= a.len() {
                assert_eq!(same, full[(k - 1) / 2..(k - 1) / 2 + la].to_vec());
            }
        }
        assert_eq!(median(&[3.0, 1.0, 2.0]), 2.0);
        assert_eq!(median(&[4.0, 1.0, 2.0, 3.0]), 2.5);
        assert_eq!(interp(1.5, &[1.0, 2.0], &[10.0, 20.0]), 15.0);
        assert_eq!(interp(-1.0, &[1.0, 2.0], &[10.0, 20.0]), 10.0);
        assert_eq!(hanning(3), vec![0.0, 1.0, 0.0]);
    }
}
