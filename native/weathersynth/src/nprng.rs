//! numpy's random Generator, bit for bit: SeedSequence (entropy as an int or a list of ints), PCG64 (XSL-RR 128/64)
//! and the distributions method B's renders use (random, uniform, standard_normal / normal / lognormal through
//! numpy's ziggurat, integers through Lemire's 32-bit method with numpy's buffered 32-bit draws, poisson by
//! multiplication). With it the Rust port of method B draws exactly what numpy drew for the approved renders: any seed
//! reproduces the Python reference (numpy/random: bit_generator.pyx, pcg64.h, distributions.c,
//! ziggurat_constants.h; BSD-3-Clause). Checked against numpy 2.5.4's own draws (tests below).

use crate::npzig::{FI, KI, WI, ZIG_INV_R, ZIG_R};

const M32: u64 = 0xFFFF_FFFF;
const INIT_A: u32 = 0x43b0_d7e5;
const MULT_A: u32 = 0x931e_8875;
const INIT_B: u32 = 0x8b51_f9dd;
const MULT_B: u32 = 0x58f3_8ded;
const MIX_MULT_L: u32 = 0xca01_f9dd;
const MIX_MULT_R: u32 = 0x4973_f715;
const XSHIFT: u32 = 16;
const PCG_MULT: u128 = 0x2360_ED05_1FC6_5DA4_4385_DF64_9FCC_F645;

/// A seed as numpy takes it: an int or a list of ints (each split into 32-bit words, low first).
pub fn seed_words(entropy: &[u64]) -> Vec<u32> {
    let mut out = Vec::new();
    for &v in entropy {
        let mut x = v;
        out.push((x & M32) as u32);
        x >>= 32;
        while x != 0 {
            out.push((x & M32) as u32);
            x >>= 32;
        }
    }
    out
}

/// SeedSequence's pool (pool size 4) for the entropy words.
pub fn seed_pool(ent: &[u32]) -> [u32; 4] {
    let mut h = INIT_A;
    let mut hm = |v: u32, h: &mut u32| -> u32 {
        let mut v = v ^ *h;
        *h = h.wrapping_mul(MULT_A);
        v = v.wrapping_mul(*h);
        v ^ (v >> XSHIFT)
    };
    let mix = |x: u32, y: u32| -> u32 {
        let r = MIX_MULT_L.wrapping_mul(x).wrapping_sub(MIX_MULT_R.wrapping_mul(y));
        r ^ (r >> XSHIFT)
    };
    let mut pool = [0u32; 4];
    for (i, p) in pool.iter_mut().enumerate() {
        *p = hm(ent.get(i).copied().unwrap_or(0), &mut h);
    }
    for s in 0..4 {
        for d in 0..4 {
            if s != d {
                let v = hm(pool[s], &mut h);
                pool[d] = mix(pool[d], v);
            }
        }
    }
    for &e in ent.iter().skip(4) {
        for d in 0..4 {
            let v = hm(e, &mut h);
            pool[d] = mix(pool[d], v);
        }
    }
    pool
}

/// SeedSequence.generate_state(n, uint64).
pub fn generate_state64(pool: &[u32; 4], n: usize) -> Vec<u64> {
    let mut h = INIT_B;
    let mut w = Vec::with_capacity(2 * n);
    for i in 0..2 * n {
        let mut v = pool[i % 4] ^ h;
        h = h.wrapping_mul(MULT_B);
        v = v.wrapping_mul(h);
        v ^= v >> XSHIFT;
        w.push(v as u64);
    }
    (0..n).map(|i| w[2 * i] | (w[2 * i + 1] << 32)).collect()
}

/// numpy.random.default_rng(entropy): a Generator on PCG64.
pub struct NpRng {
    state: u128,
    inc: u128,
    has32: bool,
    u32buf: u32,
}

impl NpRng {
    pub fn new(entropy: &[u64]) -> NpRng {
        let v = generate_state64(&seed_pool(&seed_words(entropy)), 4);
        let s = ((v[0] as u128) << 64) | v[1] as u128;
        let inc = ((((v[2] as u128) << 64) | v[3] as u128) << 1) | 1;
        let mut r = NpRng { state: 0, inc, has32: false, u32buf: 0 };
        r.state = r.state.wrapping_mul(PCG_MULT).wrapping_add(r.inc);
        r.state = r.state.wrapping_add(s);
        r.state = r.state.wrapping_mul(PCG_MULT).wrapping_add(r.inc);
        r
    }

    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_mul(PCG_MULT).wrapping_add(self.inc);
        let s = self.state;
        let x = ((s >> 64) as u64) ^ (s as u64);
        x.rotate_right((s >> 122) as u32)
    }

    /// numpy's buffered 32-bit draw (the low half, then the high half of one 64-bit draw).
    pub fn next_u32(&mut self) -> u32 {
        if self.has32 {
            self.has32 = false;
            return self.u32buf;
        }
        let n = self.next_u64();
        self.has32 = true;
        self.u32buf = (n >> 32) as u32;
        (n & M32) as u32
    }

    /// Generator.random(): [0, 1).
    pub fn random(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / 9007199254740992.0)
    }

    pub fn uniform(&mut self, low: f64, high: f64) -> f64 {
        low + (high - low) * self.random()
    }

    pub fn standard_normal(&mut self) -> f64 {
        loop {
            let mut r = self.next_u64();
            let idx = (r & 0xff) as usize;
            r >>= 8;
            let sign = r & 1;
            let rabs = (r >> 1) & 0x000f_ffff_ffff_ffff;
            let mut x = rabs as f64 * WI[idx];
            if sign == 1 {
                x = -x;
            }
            if rabs < KI[idx] {
                return x;
            }
            if idx == 0 {
                loop {
                    let xx = -ZIG_INV_R * libm::log1p(-self.random());
                    let yy = -libm::log1p(-self.random());
                    if yy + yy > xx * xx {
                        return if (rabs >> 8) & 1 == 1 { -(ZIG_R + xx) } else { ZIG_R + xx };
                    }
                }
            } else if (FI[idx - 1] - FI[idx]) * self.random() + FI[idx] < libm::exp(-0.5 * x * x) {
                return x;
            }
        }
    }

    pub fn normal(&mut self, loc: f64, scale: f64) -> f64 {
        loc + scale * self.standard_normal()
    }

    pub fn lognormal(&mut self, mean: f64, sigma: f64) -> f64 {
        libm::exp(self.normal(mean, sigma))
    }

    /// Generator.integers(low, high): [low, high), int64 (Lemire's method on numpy's buffered 32-bit draws when the
    /// range fits 32 bits).
    pub fn integers(&mut self, low: i64, high: i64) -> i64 {
        let rng = (high - 1 - low) as u64; // the inclusive range
        if rng == 0 {
            return low;
        }
        assert!(rng <= 0xFFFF_FFFE, "ranges over 32 bits are not needed by method B");
        let rng = rng as u32;
        let excl = rng as u64 + 1;
        let mut m = self.next_u32() as u64 * excl;
        let mut left = m & M32;
        if left < excl {
            let thr = ((u32::MAX - rng) as u64) % excl;
            while left < thr {
                m = self.next_u32() as u64 * excl;
                left = m & M32;
            }
        }
        low + (m >> 32) as i64
    }

    /// Generator.choice over a population of n (replace=True, no p): an index.
    pub fn choice_index(&mut self, n: usize) -> usize {
        self.integers(0, n as i64) as usize
    }

    /// Generator.poisson(lam) for lam < 10 (numpy's multiplication method; method B draws Poisson(3.5)).
    pub fn poisson_small(&mut self, lam: f64) -> i64 {
        assert!(lam < 10.0, "numpy uses PTRS from 10: not needed by method B");
        if lam <= 0.0 {
            return 0;
        }
        let enlam = libm::exp(-lam);
        let mut x = 0;
        let mut prod = 1.0;
        loop {
            prod *= self.random();
            if prod > enlam {
                x += 1;
            } else {
                return x;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    include!("nprng_vectors.rs");
}
