//! Seeded random numbers: xoshiro256** seeded through SplitMix64 from (seed, stream name). Each part of a render draws
//! from its own named stream, so changing one part (say the branches) does not reshuffle the others.

use crate::m;

pub struct Rng {
    s: [u64; 4],
}

fn splitmix(x: &mut u64) -> u64 {
    *x = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
    let mut z = *x;
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

impl Rng {
    pub fn new(seed: u64, stream: &str) -> Rng {
        // FNV-1a of the stream name, mixed with the seed
        let mut h: u64 = 0xcbf2_9ce4_8422_2325;
        for b in stream.bytes() {
            h ^= b as u64;
            h = h.wrapping_mul(0x0100_0000_01b3);
        }
        let mut x = seed ^ h.rotate_left(17);
        let s = [splitmix(&mut x), splitmix(&mut x), splitmix(&mut x), splitmix(&mut x)];
        Rng { s }
    }

    pub fn next_u64(&mut self) -> u64 {
        let r = self.s[1].wrapping_mul(5).rotate_left(7).wrapping_mul(9);
        let t = self.s[1] << 17;
        self.s[2] ^= self.s[0];
        self.s[3] ^= self.s[1];
        self.s[1] ^= self.s[2];
        self.s[0] ^= self.s[3];
        self.s[2] ^= t;
        self.s[3] = self.s[3].rotate_left(45);
        r
    }

    /// Uniform in [0, 1).
    pub fn f(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / (1u64 << 53) as f64)
    }

    pub fn range(&mut self, a: f64, b: f64) -> f64 {
        a + (b - a) * self.f()
    }

    /// Standard normal (Box-Muller, one value per call).
    pub fn normal(&mut self) -> f64 {
        let u1 = self.f().max(1e-300);
        let u2 = self.f();
        m::sqrt(-2.0 * m::ln(u1)) * m::cos(m::TAU * u2)
    }

    /// Log-normal with the given median and log-standard deviation.
    pub fn lognormal(&mut self, median: f64, sigma: f64) -> f64 {
        median * m::exp(sigma * self.normal())
    }

    /// Exponential with the given mean.
    pub fn exp(&mut self, mean: f64) -> f64 {
        -mean * m::ln(1.0 - self.f())
    }

    /// Poisson count (Knuth for small means, normal approximation above 50).
    pub fn poisson(&mut self, mean: f64) -> usize {
        if mean <= 0.0 {
            return 0;
        }
        if mean > 50.0 {
            let v = mean + m::sqrt(mean) * self.normal();
            return v.round().max(0.0) as usize;
        }
        let l = m::exp(-mean);
        let mut k = 0usize;
        let mut p = 1.0;
        loop {
            p *= self.f();
            if p <= l {
                return k;
            }
            k += 1;
        }
    }

    pub fn chance(&mut self, p: f64) -> bool {
        self.f() < p
    }
}
