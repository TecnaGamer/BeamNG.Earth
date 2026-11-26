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

/// A mixed-radix FFT (recursive decimation in time over `factors`), unscaled. `table` holds exp(sign 2 pi i k / N) for
/// the top length N; a sub-transform of length n = N / stride uses every stride-th entry.
fn fft_mixed(x: &[C], table: &[C], stride: usize, fs: &[usize]) -> Vec<C> {
    let n = x.len();
    if n == 1 {
        return vec![x[0]];
    }
    let nt = table.len();
    let p = fs[0];
    let m_ = n / p;
    let subs: Vec<Vec<C>> = (0..p)
        .map(|j| {
            let s: Vec<C> = (0..m_).map(|k| x[j + k * p]).collect();
            fft_mixed(&s, table, stride * p, &fs[1..])
        })
        .collect();
    let mut out = vec![C::default(); n];
    for k in 0..m_ {
        for q in 0..p {
            let idx = k + q * m_;
            let mut acc = subs[0][k];
            for (j, sj) in subs.iter().enumerate().skip(1) {
                let t = table[((j * idx) % n) * stride % nt];
                acc = acc.add(sj[k].mul(t));
            }
            out[idx] = acc;
        }
    }
    out
}

/// A complex FFT of any length (forward; `inverse` uses +i and divides by n, like dsp::fft).
pub fn fft_any(x: &mut Vec<C>, inverse: bool) {
    let n = x.len();
    if n.is_power_of_two() {
        dsp::fft(x, inverse);
        return;
    }
    let sign = if inverse { 1.0 } else { -1.0 };
    let fs = factors(n);
    let table: Vec<C> = (0..n).map(|k| tw(k, n, sign)).collect();
    let y = fft_mixed(x, &table, 1, &fs);
    let s = if inverse { 1.0 / n as f64 } else { 1.0 };
    for (a, b) in x.iter_mut().zip(y) {
        *a = b.scale(s);
    }
}

/// numpy.fft.rfft(x, n): x zero-padded or cut to n; the n/2 + 1 bins.
pub fn rfft(x: &[f64], n: usize) -> Vec<C> {
    let mut a: Vec<C> = (0..n).map(|i| C::new(if i < x.len() { x[i] } else { 0.0 }, 0.0)).collect();
    fft_any(&mut a, false);
    a.truncate(n / 2 + 1);
    a
}

/// numpy.fft.irfft(X, n): the Hermitian spectrum of n points from its first n/2 + 1 bins (missing bins zero; the
/// imaginary parts of the DC and, for even n, the Nyquist bins ignored, as pocketfft does).
pub fn irfft(xf: &[C], n: usize) -> Vec<f64> {
    let h = n / 2 + 1;
    let get = |k: usize| if k < xf.len() { xf[k] } else { C::default() };
    let mut a = vec![C::default(); n];
    for k in 0..h.min(n) {
        let mut v = get(k);
        if k == 0 || (n % 2 == 0 && k == n / 2) {
            v.im = 0.0;
        }
        a[k] = v;
        if k > 0 && n - k != k {
            a[n - k] = C::new(v.re, -v.im);
        }
    }
    fft_any(&mut a, true);
    a.into_iter().map(|z| z.re).collect()
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
        for n in [6usize, 12, 30, 63, 70, 120, 77] {
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
