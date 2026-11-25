//! Rain from individual drops, as seamless 20 s loops.
//!
//! Drop sizes follow Marshall-Palmer (N(D) = 8000 exp(-Lambda D) per m^3 per mm, Lambda = 4.1 R^-0.21), falling at the
//! terminal speed of Atlas et al. (1973), v = 9.65 - 10.3 exp(-0.6 D) m/s; the impact rate per m^2 is the integral of
//! N(D) v(D) over 0.5-6 mm. Every drop lands at a random time and place round the listener (1.6 m up) and makes the
//! sound of the surface it hits:
//! - wet road: a short band-passed splash (higher for small drops) plus crown fall-out ticks; now and then a bubble;
//! - puddles: splash plus a Minnaert bubble (f = 3.26 / a), a damped sine whose pitch rises (van den Doel 2005:
//!   d = 0.043 f + 0.0014 f^1.5, rise xi = 0.1). Drops of 0.8-1.2 mm entrain ~0.22 mm bubbles (the 13-16 kHz "rain on
//!   water" peak, Pumphrey & Crum 1989); bigger drops entrain bigger bubbles at random (1-6 kHz);
//! - grass: a softer, duller tick; leaves (trees from 3 m out): two or three short leaf modes (Q 3-8) 200 Hz - 3.5 kHz, and
//!   drips (big drops off the leaves, ~1 % of leaf hits by water balance) onto the ground or into puddles.
//! Amplitude ~ D^1.5 v / r. Far rings are thinned (each rendered drop stands for k real ones at sqrt(k) amplitude, the
//! same energy) and low-passed by distance (ground and obstacle scattering, ISO 9613-1 air absorption).
//! Car roof: the drops excite the roof panel's plate modes (62 Hz up, loss factor 0.04, odd modes radiating most) and
//! the windscreen's, heard through the headliner, plus the outside rain through the body. Muffled: the medium rain
//! through two 900 Hz low-passes, +4 dB (as the mod's derived file).
//! Everything is written round a circular 20 s buffer, and every filter runs round the loop twice, so the loop has no
//! seam.

use crate::dsp::{self, Biquad};
use crate::loudness;
use crate::m;
use crate::rng::Rng;
use crate::thunder::cutoff_for_path;

pub const FS: f64 = 44100.0;
pub const LOOP_S: f64 = 20.0;
const OBS_H: f64 = 1.6;

fn terminal_v(d_mm: f64) -> f64 {
    (9.65 - 10.3 * m::exp(-0.6 * d_mm)).max(0.1)
}

struct Dsd {
    d: Vec<f64>,
    cdf: Vec<f64>,
    flux: f64, // impacts per m^2 per s
}

fn dsd(rate_mm_h: f64) -> Dsd {
    let lam = 4.1 * m::pow(rate_mm_h, -0.21);
    let mut d = Vec::new();
    let mut cdf = Vec::new();
    let mut acc = 0.0;
    let step = 0.01;
    let mut x = 0.5;
    while x <= 6.0 {
        // N0 = 8000 m^-3 mm^-1
        acc += 8000.0 * m::exp(-lam * x) * terminal_v(x) * step;
        d.push(x);
        cdf.push(acc);
        x += step;
    }
    Dsd { d, cdf, flux: acc }
}

impl Dsd {
    fn sample(&self, rng: &mut Rng) -> f64 {
        let u = rng.f() * self.flux;
        let i = self.cdf.partition_point(|&c| c < u).min(self.d.len() - 1);
        self.d[i] + rng.range(-0.005, 0.005)
    }
}

/// The air-absorption cutoff (ISO 9613-1, 6 dB point) by distance, tabulated on a log grid 0.25-400 m.
struct AirCutoff {
    tab: Vec<f64>,
}

impl AirCutoff {
    const N: usize = 512;
    fn new() -> AirCutoff {
        let tab = (0..Self::N)
            .map(|i| cutoff_for_path(0.25 * m::pow(1600.0, i as f64 / (Self::N - 1) as f64)))
            .collect();
        AirCutoff { tab }
    }
    fn at(&self, r: f64) -> f64 {
        let x = (m::ln(r.clamp(0.25, 400.0) / 0.25) / m::ln(1600.0)) * (Self::N - 1) as f64;
        let i = (x.floor() as usize).min(Self::N - 2);
        let fr = x - i as f64;
        self.tab[i] * (1.0 - fr) + self.tab[i + 1] * fr
    }
}

/// Add an event into the circular loop buffer at sample `at`.
fn put(buf: &mut [f64], at: usize, ev: &[f64], amp: f64) {
    let n = buf.len();
    let mut j = at % n;
    for &v in ev {
        buf[j] += amp * v;
        j += 1;
        if j == n {
            j = 0;
        }
    }
}

fn noise_burst(rng: &mut Rng, tau_ms: f64, len_tau: f64, filt: &mut Biquad) -> Vec<f64> {
    let tau = tau_ms * 1e-3 * FS;
    let n = (tau * len_tau).ceil() as usize + 4;
    let att = (0.00005 * FS).max(1.0);
    filt.reset();
    (0..n)
        .map(|i| {
            let t = i as f64;
            let env = if t < att { t / att } else { m::exp(-(t - att) / tau) };
            filt.tick((rng.f() * 2.0 - 1.0) * env)
        })
        .collect()
}

fn bubble(rng: &mut Rng, radius_m: f64) -> Vec<f64> {
    let f0 = 3.26 / radius_m;
    if f0 > 19000.0 {
        return Vec::new();
    }
    let d = 0.043 * f0 + 0.0014 * m::pow(f0, 1.5);
    let sigma = 0.1 * d;
    let n = ((6.9 / d) * FS).ceil() as usize + 1;
    let mut ph = rng.range(0.0, 0.3);
    (0..n)
        .map(|i| {
            let t = i as f64 / FS;
            let f = f0 * (1.0 + sigma * t);
            ph += m::TAU * f / FS;
            m::sin(ph) * m::exp(-d * t)
        })
        .collect()
}

fn mix_into(a: &mut Vec<f64>, b: &[f64], off: usize, g: f64) {
    if a.len() < off + b.len() {
        a.resize(off + b.len(), 0.0);
    }
    for (i, &v) in b.iter().enumerate() {
        a[off + i] += g * v;
    }
}

fn splash(rng: &mut Rng, d: f64) -> Vec<f64> {
    // the impact: broadband, centred lower for bigger drops
    let fc = (2200.0 * m::pow(2.0 / d, 0.5) * rng.range(0.7, 1.4)).min(14000.0);
    let mut bp = Biquad::bandpass(FS, fc, 0.7);
    let mut ev = noise_burst(rng, 0.25 + 0.15 * d, 6.0, &mut bp);
    // the body of the splash (the sheet and crown of water): a short low burst, stronger for big drops
    let mut lp = Biquad::bandpass(FS, rng.range(400.0, 1000.0), 0.7);
    let body = noise_burst(rng, 1.0 + 0.6 * d, 4.0, &mut lp);
    mix_into(&mut ev, &body, 0, 2.2 * m::sqrt(d / 2.0));
    // the sharp first contact: a few samples of high crackle
    let mut hp = Biquad::bandpass(FS, rng.range(7000.0, 11000.0), 0.8);
    let crack = noise_burst(rng, 0.06, 6.0, &mut hp);
    mix_into(&mut ev, &crack, 0, 0.8);
    let k = rng.poisson(1.5 * d);
    for _ in 0..k {
        let mut bp2 = Biquad::bandpass(FS, rng.range(5000.0, 9000.0), 1.2);
        let t = noise_burst(rng, 0.08, 6.0, &mut bp2);
        let off = (rng.range(0.003, 0.025) * FS) as usize;
        mix_into(&mut ev, &t, off, rng.range(0.08, 0.2));
    }
    ev
}

fn grass(rng: &mut Rng, d: f64) -> Vec<f64> {
    let fc = 800.0 * m::pow(2.0 / d, 0.4) * rng.range(0.6, 1.5);
    let mut bp = Biquad::bandpass(FS, fc, 0.5);
    noise_burst(rng, 0.3 + 0.2 * d, 6.0, &mut bp)
}

fn leaf(rng: &mut Rng) -> Vec<f64> {
    let modes = 2 + rng.poisson(0.6).min(2);
    let mut ev = Vec::new();
    for _ in 0..modes {
        let f = rng.lognormal(700.0, 0.5).clamp(200.0, 3500.0);
        let q = rng.range(3.0, 8.0);
        let dec = m::PI * f / q;
        let n = ((6.9 / dec) * FS) as usize + 1;
        let a = rng.range(0.4, 1.0);
        let ph0 = rng.range(0.0, m::TAU);
        let md: Vec<f64> = (0..n)
            .map(|i| {
                let t = i as f64 / FS;
                a * m::sin(m::TAU * f * t + ph0) * m::exp(-dec * t) * (1.0 - m::exp(-t * 20000.0))
            })
            .collect();
        mix_into(&mut ev, &md, 0, 1.0);
    }
    let mut bp = Biquad::bandpass(FS, 3000.0, 0.8);
    let tick = noise_burst(rng, 0.15, 6.0, &mut bp);
    mix_into(&mut ev, &tick, 0, 0.3);
    ev
}

fn puddle(rng: &mut Rng, d: f64) -> Vec<f64> {
    let mut ev: Vec<f64> = splash(rng, d).into_iter().map(|v| v * 0.7).collect();
    let (p, a) = if d < 1.3 {
        let p = if (0.8..=1.2).contains(&d) { 0.85 } else { 0.3 };
        (p, rng.range(0.19e-3, 0.26e-3))
    } else {
        (0.4, rng.lognormal(0.9e-3, 0.5).clamp(0.3e-3, 3e-3))
    };
    if rng.chance(p) {
        let b = bubble(rng, a);
        let g = 0.9 * m::sqrt(a / 0.25e-3) * rng.range(0.3, 1.0);
        mix_into(&mut ev, &b, (rng.range(0.0005, 0.003) * FS) as usize, g);
    }
    ev
}

#[derive(Clone, Copy)]
enum Surf {
    Road,
    Puddle,
    Grass,
    Leaves,
}

/// The outdoor rain: drops on road, puddles, grass and trees round the listener.
pub fn outdoor(seed: u64, rate_mm_h: f64, stream: &str) -> Vec<f64> {
    let n = (LOOP_S * FS) as usize;
    let mut buf = vec![0.0; n];
    let ds = dsd(rate_mm_h);
    let mut rng = Rng::new(seed, stream);
    let air = AirCutoff::new();
    // WEATHERSYNTH_ONLY=road|puddle|grass|leaves renders one surface (testing; the random draws stay the same)
    let only = std::env::var("WEATHERSYNTH_ONLY").ok();
    // (inner, outer radius m, rendered drops per second at most)
    // Far rings are many quiet drops overlapping (thousands per second, each ~5-10 ms long): rendering a few thousand
    // at the same energy already sums to the same smooth wash.
    let rings = [(0.0, 2.5, 30000.0), (2.5, 6.0, 12000.0), (6.0, 15.0, 6000.0), (15.0, 40.0, 4000.0), (40.0, 100.0, 3000.0)];
    let weights = [(Surf::Road, 0.52), (Surf::Puddle, 0.08), (Surf::Grass, 0.22), (Surf::Leaves, 0.18)];
    for &(r1, r2, cap) in rings.iter() {
        let area = m::PI * (r2 * r2 - r1 * r1);
        let expected: f64 = ds.flux * area;
        let k = (expected / cap).max(1.0);
        let count = rng.poisson(expected / k * LOOP_S);
        for _ in 0..count {
            let t = rng.f() * LOOP_S;
            let rho = m::sqrt(rng.range(r1 * r1, r2 * r2));
            let mut u = rng.f();
            let mut surf = Surf::Road;
            for &(s, w) in weights.iter() {
                if u < w {
                    surf = s;
                    break;
                }
                u -= w;
            }
            if matches!(surf, Surf::Leaves) && rho < 3.0 {
                surf = Surf::Grass;
            }
            let mut d = ds.sample(&mut rng);
            let mut v = terminal_v(d);
            let z = if matches!(surf, Surf::Leaves) { rng.range(2.5, 8.0) } else { 0.0 };
            let (ev, sg) = match surf {
                Surf::Road => {
                    let mut e = splash(&mut rng, d);
                    if rng.chance(0.08) {
                        let a = rng.range(0.19e-3, 0.3e-3);
                        let b = bubble(&mut rng, a);
                        mix_into(&mut e, &b, 20, 0.4);
                    }
                    (e, 1.0)
                }
                Surf::Puddle => (puddle(&mut rng, d), 1.0),
                Surf::Grass => (grass(&mut rng, d), 0.45),
                Surf::Leaves => {
                    // Drips: the canopy collects the small drops and sheds them as big ones. By volume one 3.5-5.5 mm
                    // drip holds ~90 drops of 1 mm, so about 1 % of leaf hits come back down as a drip, onto the
                    // ground (70 %) or into a puddle.
                    if rng.chance(0.011) {
                        d = rng.range(3.5, 5.5);
                        v = 4.5;
                        if rng.chance(0.3) {
                            (puddle(&mut rng, d), 1.0)
                        } else {
                            (grass(&mut rng, d), 0.45)
                        }
                    } else {
                        (leaf(&mut rng), 0.3)
                    }
                }
            };
            if let Some(only) = &only {
                let name = match surf {
                    Surf::Road => "road",
                    Surf::Puddle => "puddle",
                    Surf::Grass => "grass",
                    Surf::Leaves => "leaves",
                };
                if name != only {
                    continue;
                }
            }
            let dz = OBS_H - z;
            let r = m::sqrt(rho * rho + dz * dz).max(0.5);
            let amp = m::pow(d / 2.0, 1.5) * (v / 6.5) * sg * m::sqrt(k) / r;
            // distance low-pass: scattering by the ground, kerbs, cars, plants, and the air
            let fc = if rho > 4.0 { (14000.0 * m::pow(4.0 / rho, 0.6)).clamp(1500.0, 16000.0) } else { 16000.0 };
            let fc = fc.min(air.at(r));
            let a = dsp::onepole_coef(FS, fc);
            let mut s = 0.0;
            let evf: Vec<f64> = ev
                .iter()
                .map(|&x| {
                    s += a * (x - s);
                    s
                })
                .collect();
            put(&mut buf, (t * FS) as usize, &evf, amp);
        }
    }
    buf
}

/// One modal bank (a panel): modes (m, n) of an a x b plate scaled so (1,1) is at f11, kept below fmax.
fn panel(
    rng: &mut Rng,
    buf: &mut [f64],
    drops: &[(usize, f64, f64, f64, f64)], // (sample, x 0..1, y 0..1, impulse, contact time s)
    a: f64,
    b: f64,
    f11: f64,
    fmax: f64,
    eta: f64,
    gain: f64,
) {
    let n = buf.len();
    let scale = f11 / (1.0 / (a * a) + 1.0 / (b * b));
    let mut force = vec![0.0; n];
    let mut modes = Vec::new();
    for mi in 1..40usize {
        for ni in 1..40usize {
            let f = scale * ((mi as f64 / a) * (mi as f64 / a) + (ni as f64 / b) * (ni as f64 / b));
            if f < fmax {
                modes.push((mi, ni, f));
            }
        }
    }
    for &(mi, ni, f) in modes.iter() {
        // jitter: the real panel is curved and stiffened
        let f = f * rng.range(0.94, 1.06);
        let eta_f = eta * (1.0 + f / 2000.0);
        let w = m::TAU * f / FS;
        let r = m::exp(-m::PI * f * eta_f / FS);
        let rad = (if mi % 2 == 1 { 1.0 } else { 0.35 }) * (if ni % 2 == 1 { 1.0 } else { 0.35 });
        let wf = m::pow(f / 1000.0, 0.4);
        for v in force.iter_mut() {
            *v = 0.0;
        }
        for &(at, x, y, imp, tc) in drops {
            let shape = m::sin(m::PI * mi as f64 * x) * m::sin(m::PI * ni as f64 * y);
            // half-sine contact force spectrum at f
            let u = 2.0 * f * tc;
            let hc = if (u - 1.0).abs() < 1e-6 { m::PI / 4.0 } else { (m::cos(m::PI * f * tc) / (1.0 - u * u)).abs() };
            force[at % n] += imp * shape * hc;
        }
        // resonator y[n] = 2 r cos w y[n-1] - r^2 y[n-2] + sin(w) F[n]: impulse response r^n sin((n+1) w)
        let c1 = 2.0 * r * m::cos(w);
        let c2 = r * r;
        let sw = m::sin(w);
        let (mut y1, mut y2) = (0.0, 0.0);
        for pass in 0..2 {
            for i in 0..n {
                let y = c1 * y1 - c2 * y2 + sw * force[i];
                y2 = y1;
                y1 = y;
                if pass == 1 {
                    // In the closed cabin the pressure follows the panel's displacement at low frequencies and its
                    // acceleration higher up: weight by (f / 1 kHz)^0.4 between the two.
                    buf[i] += gain * rad * wf * y;
                }
            }
        }
    }
}

/// Rain on the car roof heard from inside. `outside` is the outdoor rain at the same rate (heard through the body).
pub fn roof(seed: u64, rate_mm_h: f64, outside: &[f64]) -> Vec<f64> {
    let n = (LOOP_S * FS) as usize;
    let mut buf = vec![0.0; n];
    let ds = dsd(rate_mm_h);
    let mut rng = Rng::new(seed, "rain-roof-drops");
    let mut make = |rng: &mut Rng, area: f64| -> Vec<(usize, f64, f64, f64, f64)> {
        let cnt = rng.poisson(ds.flux * area * LOOP_S);
        let mut v = Vec::with_capacity(cnt);
        for _ in 0..cnt {
            let d = ds.sample(rng);
            let vt = terminal_v(d);
            let mass = 1000.0 * m::PI / 6.0 * (d * 1e-3) * (d * 1e-3) * (d * 1e-3);
            v.push(((rng.f() * LOOP_S * FS) as usize, rng.f(), rng.f(), mass * vt * 1e4, d * 1e-3 / vt));
        }
        v.sort_by_key(|e| e.0);
        v
    };
    let roof_drops = make(&mut rng, 1.30 * 1.45);
    let glass_drops = make(&mut rng, 1.40 * 0.85 * 0.6); // the windscreen is raked: less rain per m^2
    let mut mrng = Rng::new(seed, "rain-roof-modes");
    panel(&mut mrng, &mut buf, &roof_drops, 1.30, 1.45, 62.0, 3500.0, 0.04, 1.0);
    // the headliner and cabin: the roof's high modes and its ticks heard dulled
    let mut lp = Biquad::lowpass(FS, 1100.0, 0.6);
    lp.run_circular(&mut buf);
    let mut glass = vec![0.0; n];
    panel(&mut mrng, &mut glass, &glass_drops, 1.40, 0.85, 110.0, 5000.0, 0.05, 0.2);
    let mut lpg = Biquad::lowpass(FS, 2500.0, 0.7);
    lpg.run_circular(&mut glass);
    // contact ticks on the glass (no headliner)
    let mut trng = Rng::new(seed, "rain-roof-ticks");
    for &(at, _, _, imp, _) in glass_drops.iter() {
        let mut bp = Biquad::bandpass(FS, trng.range(2500.0, 4500.0), 1.0);
        let t = noise_burst(&mut trng, 0.12, 6.0, &mut bp);
        put(&mut glass, at, &t, imp * 0.08);
    }
    for i in 0..n {
        buf[i] += glass[i];
    }
    // the outside rain through the body: low-passed hard and quiet
    let mut out = outside.to_vec();
    let mut l1 = Biquad::lowpass(FS, 450.0, 0.7071);
    let mut l2 = Biquad::lowpass(FS, 450.0, 0.7071);
    l1.run_circular(&mut out);
    l2.run_circular(&mut out);
    let rb = rms(&buf);
    let ro = rms(&out);
    if ro > 0.0 {
        let g = 0.3 * rb / ro;
        for i in 0..n {
            buf[i] += g * out[i];
        }
    }
    buf
}

fn rms(x: &[f64]) -> f64 {
    m::sqrt(x.iter().map(|v| v * v).sum::<f64>() / x.len().max(1) as f64)
}

pub struct LoopTarget {
    pub lufs: f64,
    pub ceil_db: f64,
}

/// High-pass 30 Hz round the loop, gain to the loudness (measured over the loop played twice), then a true-peak limiter
/// round three copies (the middle one kept). Two corrections for what the limiter takes.
pub fn finish_loop(mut x: Vec<f64>, t: &LoopTarget) -> (Vec<f64>, f64) {
    let mut hp = Biquad::highpass(FS, 30.0, 0.7071);
    hp.run_circular(&mut x);
    let mut g = m::undb(t.lufs - loudness::integrated(&x, FS, true));
    let env = loudness::tp_envelope(&x, true);
    let mut y = Vec::new();
    for _ in 0..3 {
        y = limit_loop(&x, &env, g, t.ceil_db);
        let l = loudness::integrated(&y, FS, true);
        g *= m::undb(t.lufs - l);
    }
    let gdb = m::db(g);
    (y, gdb)
}

/// Gain and a true-peak limiter (2 ms attack, 60 ms release) round a loop: three copies limited, the middle one kept.
/// `env` is the loop's true-peak envelope at unity gain.
pub fn limit_loop(x: &[f64], env: &[f64], gain: f64, ceil_db: f64) -> Vec<f64> {
    let n = x.len();
    let tri: Vec<f64> = (0..3 * n).map(|i| env[i % n] * gain).collect();
    let g = loudness::limiter_gain(&tri, m::undb(ceil_db), (0.002 * FS).round() as usize, 0.06, FS);
    (0..n).map(|i| x[i] * gain * g[n + i]).collect()
}

pub fn muffle(x: &[f64]) -> Vec<f64> {
    let mut y = x.to_vec();
    let mut l1 = Biquad::lowpass(FS, 900.0, 0.7071);
    let mut l2 = Biquad::lowpass(FS, 900.0, 0.7071);
    l1.run_circular(&mut y);
    l2.run_circular(&mut y);
    for v in y.iter_mut() {
        *v *= 1.6;
    }
    y
}

pub fn rate_for(kind: &str) -> Option<f64> {
    match kind {
        "light" => Some(1.5),
        "medium" | "muffled" | "roof" => Some(6.0),
        "heavy" => Some(22.0),
        _ => None,
    }
}
