//! The random numbers of a worn dirt road's profile (server grading.dirt_bumps / dirt_bank: BNE_DIRT_BUMP, BNE_DIRT_BANK),
//! the same bits on every platform. numpy's Generator drew them before: its normal (the ziggurat's wedge and tail) and
//! poisson (exp, and log in PTRS from lam 10) call the platform's C maths library, so a last-bit difference between
//! glibc, the UCRT and Apple's libm could flip an accept / reject and shift the rest of the road's stream (GitHub #1169).
//!
//! Here everything is integer maths or exact IEEE operations: splitmix64 for the bits, a uniform from the top 53 of
//! them, a normal as Irwin-Hall's sum of 12 uniforms less 6 (mean 0, variance 1, bounded at +-6: no maths library),
//! and a Poisson count by Knuth's multiplication method against exp(-lam) from dm (the pinned libm crate), lam split
//! into equal chunks of at most POISSON_CHUNK so the running product never nears the subnormals.

use crate::dm::Dm;
use numpy::{IntoPyArray, PyArray1};
use pyo3::prelude::*;

const POISSON_CHUNK: f64 = 32.0;
const POISSON_MAX_LAM: f64 = 1.0e7;     // (a road 50,000 km long at a pothole per 5 m: never; refused, not looped on)
const MAX_DRAWS: usize = 1 << 26;       // (an array's length: the same guard against a runaway count)

/// splitmix64 (Steele, Lea and Flood 2014; Vigna's reference): wrapping integer operations only.
#[derive(Clone)]
pub struct Stream {
    state: u64,
}

impl Stream {
    pub fn new(seed: u64) -> Self {
        Stream { state: seed }
    }

    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// [0, 1): the top 53 bits times 2^-53 (exact).
    pub fn unit(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / 9_007_199_254_740_992.0)
    }

    /// [lo, hi) as numpy's: lo + (hi - lo) * u (two roundings, never fused).
    pub fn uniform(&mut self, lo: f64, hi: f64) -> f64 {
        lo + (hi - lo) * self.unit()
    }

    /// Irwin-Hall: the sum of 12 uniforms (in draw order) less 6.
    pub fn normal(&mut self) -> f64 {
        let mut s = 0.0;
        for _ in 0..12 {
            s += self.unit();
        }
        s - 6.0
    }

    /// Poisson(lam) by the multiplication method, lam in chunks of at most POISSON_CHUNK (a sum of Poisson counts is a
    /// Poisson count of the summed rates).
    pub fn poisson(&mut self, lam: f64) -> u64 {
        if !(lam > 0.0) {
            return 0;
        }
        let chunks = (lam / POISSON_CHUNK).ceil().max(1.0);
        let part = lam / chunks;
        let limit = (-part).dexp();
        let mut k = 0u64;
        for _ in 0..chunks as u64 {
            let mut p = self.unit();
            while p > limit {
                k += 1;
                p *= self.unit();
            }
        }
        k
    }
}

fn check(ok: bool, what: &str) -> PyResult<()> {
    if ok {
        Ok(())
    } else {
        Err(pyo3::exceptions::PyValueError::new_err(format!("DirtNoise: {what}")))
    }
}

/// grading.dirt_bumps' and dirt_bank's random numbers, seeded by the road's own position (an int, 0 .. 2^64).
#[pyclass(module = "earthfast")]
pub struct DirtNoise {
    s: Stream,
}

#[pymethods]
impl DirtNoise {
    #[new]
    fn new(seed: u64) -> Self {
        DirtNoise { s: Stream::new(seed) }
    }

    /// n normal draws (Irwin-Hall: mean 0, variance 1, within +-6).
    fn normals<'py>(&mut self, py: Python<'py>, n: usize) -> PyResult<Bound<'py, PyArray1<f64>>> {
        check(n <= MAX_DRAWS, "too many draws")?;
        Ok((0..n).map(|_| self.s.normal()).collect::<Vec<f64>>().into_pyarray(py))
    }

    /// n uniform draws in [lo, hi).
    fn uniforms<'py>(&mut self, py: Python<'py>, lo: f64, hi: f64, n: usize) -> PyResult<Bound<'py, PyArray1<f64>>> {
        check(n <= MAX_DRAWS, "too many draws")?;
        check(lo.is_finite() && hi.is_finite() && lo <= hi, "bounds must be finite, lo <= hi")?;
        Ok((0..n).map(|_| self.s.uniform(lo, hi)).collect::<Vec<f64>>().into_pyarray(py))
    }

    /// One uniform draw in [lo, hi).
    fn uniform(&mut self, lo: f64, hi: f64) -> PyResult<f64> {
        check(lo.is_finite() && hi.is_finite() && lo <= hi, "bounds must be finite, lo <= hi")?;
        Ok(self.s.uniform(lo, hi))
    }

    /// A Poisson count of mean lam (0 <= lam <= 1e7).
    fn poisson(&mut self, lam: f64) -> PyResult<u64> {
        check(lam >= 0.0 && lam <= POISSON_MAX_LAM, "lam must be within 0 .. 1e7")?;
        Ok(self.s.poisson(lam))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splitmix64_reference() {
        // Vigna's splitmix64.c, seed 1234567; the same draws pinned through Python in server/tests/test_grading.py
        let mut s = Stream::new(1234567);
        assert_eq!(s.next_u64(), 6457827717110365317);
        assert_eq!(s.next_u64(), 3203168211198807973);
    }

    #[test]
    fn normal_and_poisson_moments() {
        let mut s = Stream::new(7);
        let n = 200_000;
        let (mut m, mut v) = (0.0, 0.0);
        let xs: Vec<f64> = (0..n).map(|_| s.normal()).collect();
        for x in &xs {
            m += x;
        }
        m /= n as f64;
        for x in &xs {
            v += (x - m) * (x - m);
        }
        v /= n as f64;
        assert!(m.abs() < 0.01, "mean {m}");
        assert!((v - 1.0).abs() < 0.015, "variance {v}");
        for lam in [0.3, 1.0, 4.0, 31.0, 95.0, 400.0] {
            let n = 40_000;
            let ks: Vec<f64> = (0..n).map(|_| s.poisson(lam) as f64).collect();
            let m = ks.iter().sum::<f64>() / n as f64;
            let v = ks.iter().map(|k| (k - m) * (k - m)).sum::<f64>() / n as f64;
            assert!((m - lam).abs() < 4.0 * (lam / n as f64).sqrt() + 1e-9, "lam {lam} mean {m}");
            assert!((v / lam - 1.0).abs() < 0.05, "lam {lam} variance {v}");
        }
    }
}
