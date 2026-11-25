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
//! Car roof: the drops excite the roof panel's plate modes (62 Hz up, loss factor 0.04, more on the lowest modes, odd
//! modes radiating most) and
//! the windscreen's, heard through the headliner, plus the outside rain through the body. Muffled: the medium rain
//! through two 900 Hz low-passes, +4 dB (as the mod's derived file).
//! Gusts swing the drop rate +/-45 % over seconds (whole periods per loop). Everything is written round a circular 20 s
//! buffer, and every filter runs round the loop twice, so the loop has no
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

/// Gusts: the drop rate swings slowly (sum of sines with whole periods over the loop, so the loop stays seamless),
/// relative 1 +/- `depth` at most. `accept(t)` keeps a drop drawn at the peak rate with probability rate(t) / peak.
struct Gust {
    parts: Vec<(f64, f64, f64)>, // (cycles per loop, amplitude, phase)
    peak: f64,
    mean: f64,
}

impl Gust {
    fn new(seed: u64, stream: &str, depth: f64) -> Gust {
        let mut rng = Rng::new(seed, stream);
        let mut parts = Vec::new();
        let mut tot = 0.0;
        for k in 1..=7 {
            let a = rng.range(0.3, 1.0) / m::sqrt(k as f64);
            parts.push((k as f64, a, rng.range(0.0, m::TAU)));
            tot += a;
        }
        for p in parts.iter_mut() {
            p.1 *= depth / tot;
        }
        let mut g = Gust { parts, peak: 1.0, mean: 1.0 };
        let n = 2000;
        let v: Vec<f64> = (0..n).map(|i| g.rate(LOOP_S * i as f64 / n as f64)).collect();
        g.peak = v.iter().cloned().fold(0.0, f64::max);
        g.mean = v.iter().sum::<f64>() / n as f64;
        g
    }
    fn rate(&self, t: f64) -> f64 {
        1.0 + self.parts.iter().map(|&(k, a, ph)| a * m::sin(m::TAU * k * t / LOOP_S + ph)).sum::<f64>()
    }
    /// Draws needed at the peak rate for `expected` kept drops.
    fn draws(&self, expected: f64) -> f64 {
        expected * self.peak / self.mean
    }
    fn accept(&self, rng: &mut Rng, t: f64) -> bool {
        rng.f() * self.peak < self.rate(t)
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

fn splash(rng: &mut Rng, d: f64, film: f64) -> Vec<f64> {
    // the impact: broadband, centred lower for bigger drops
    let fc = (2200.0 * m::pow(2.0 / d, 0.5) * rng.range(0.7, 1.4)).min(14000.0);
    let mut bp = Biquad::bandpass(FS, fc, 0.7);
    let mut ev = noise_burst(rng, 0.25 + 0.15 * d, 6.0, &mut bp);
    // the body of the splash (the sheet and crown of water): a short low burst, stronger for big drops
    let mut lp = Biquad::bandpass(FS, rng.range(400.0, 1000.0), 0.7);
    let body = noise_burst(rng, 1.0 + 0.6 * d, 4.0, &mut lp);
    // deeper standing water: a bigger crown and cavity, more low-mid body
    mix_into(&mut ev, &body, 0, 2.2 * m::sqrt(d / 2.0) * (1.0 + 0.25 * (film - 1.0)));
    // the sharp first contact: a few samples of high crackle
    let mut hp = Biquad::bandpass(FS, rng.range(7000.0, 11000.0), 0.8);
    let crack = noise_burst(rng, 0.06, 6.0, &mut hp);
    mix_into(&mut ev, &crack, 0, 0.8);
    let k = rng.poisson(1.5 * d * film);
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

#[derive(Clone, Copy, PartialEq)]
enum Surf {
    Road,
    Puddle,
    Grass,
    Leaves,
    Metal,
}

/// Impact amplitude at 1 m relative to a 2 mm drop at terminal speed: Guo & Ffowcs Williams (1991) find the radiated
/// energy proportional to the drop's kinetic energy times the impact Mach number cubed, so amplitude ~ D^1.5 v^2.5.
/// Big, fast drops dominate: a 1 mm drop is ~19 dB under a 2 mm one, a 0.3 mm drizzle droplet ~55 dB.
fn impact_amp(d: f64, v: f64) -> f64 {
    m::pow(d / 2.0, 1.5) * m::pow(v / 6.5, 2.5)
}

/// A bubble's amplitude does not follow the impact: entrainment of the small drops (0.8-1.2 mm) rings louder than
/// their impact (the rain-on-water peak), relative to the 2 mm impact at 1 m.
const BUBBLE_AMP: f64 = 0.25;

/// Bubble entrained by a drop of d mm (None: none). Regular entrainment for 0.8-1.2 mm (~0.22 mm bubbles, 13-16 kHz;
/// Pumphrey & Crum 1989), irregular and larger for big drops.
fn entrained(rng: &mut Rng, d: f64) -> Option<f64> {
    if d < 0.5 {
        return None; // drizzle droplets land too softly to entrain air
    }
    let (p, a) = if d < 1.3 {
        (if (0.8..=1.2).contains(&d) { 0.85 } else { 0.25 }, rng.range(0.19e-3, 0.26e-3))
    } else {
        (0.45, rng.lognormal(0.9e-3, 0.5).clamp(0.3e-3, 3e-3))
    };
    if rng.chance(p) {
        Some(a)
    } else {
        None
    }
}

fn metal(rng: &mut Rng) -> Vec<f64> {
    // thin sheet metal (a gutter, a car body, a sign): a hard tick and a few lightly damped modes
    let mut ev = Vec::new();
    for _ in 0..3 {
        let f = rng.lognormal(2600.0, 0.45).clamp(700.0, 9000.0);
        let q = rng.range(30.0, 110.0);
        let dec = m::PI * f / q;
        let n = ((6.9 / dec) * FS) as usize + 1;
        let a = rng.range(0.3, 1.0);
        let md: Vec<f64> = (0..n)
            .map(|i| {
                let t = i as f64 / FS;
                a * m::sin(m::TAU * f * t) * m::exp(-dec * t)
            })
            .collect();
        mix_into(&mut ev, &md, 0, 0.5);
    }
    let mut bp = Biquad::bandpass(FS, rng.range(4000.0, 8000.0), 0.8);
    let tick = noise_burst(rng, 0.08, 6.0, &mut bp);
    mix_into(&mut ev, &tick, 0, 1.0);
    ev
}

/// One drop's sound at 1 m, absolute (impact_amp scaling included): its surface's impact plus an entrained bubble.
/// `film`: how much standing water there is (1 a wet road; more in a downpour): more crown spray (the secondary
/// droplets' ticks) and more small bubbles from it.
fn drop_event(rng: &mut Rng, surf: Surf, d: f64, v: f64, film: f64) -> Vec<f64> {
    let a = impact_amp(d, v);
    let mut ev: Vec<f64> = match surf {
        Surf::Road => splash(rng, d, film),
        Surf::Puddle => splash(rng, d, film).into_iter().map(|x| x * 0.7).collect(),
        Surf::Grass => grass(rng, d).into_iter().map(|x| x * 0.45).collect(),
        Surf::Leaves => leaf(rng).into_iter().map(|x| x * 0.3).collect(),
        Surf::Metal => metal(rng).into_iter().map(|x| x * 1.2).collect(),
    };
    for x in ev.iter_mut() {
        *x *= a;
    }
    // bubbles: in puddles; on the wet road's film now and then (small ones)
    let bub = match surf {
        Surf::Puddle => entrained(rng, d),
        Surf::Road if d >= 0.8 && rng.chance(0.06 * film) => Some(rng.range(0.19e-3, 0.3e-3)),
        _ => None,
    };
    if let Some(r) = bub {
        let b = bubble(rng, r);
        let g = BUBBLE_AMP * m::sqrt(r / 0.25e-3) * rng.range(0.3, 1.0);
        mix_into(&mut ev, &b, (rng.range(0.0005, 0.003) * FS) as usize, g);
    }
    // in standing water the spray falls back in and entrains its own small bubbles (the downpour's hiss)
    if matches!(surf, Surf::Puddle | Surf::Road) && film > 1.0 && d >= 1.0 {
        let k = rng.poisson(0.5 * (film - 1.0) * (d / 2.0));
        for _ in 0..k {
            let r = rng.range(0.15e-3, 0.3e-3);
            let b = bubble(rng, r);
            let g = 0.4 * BUBBLE_AMP * rng.range(0.2, 1.0);
            mix_into(&mut ev, &b, (rng.range(0.005, 0.06) * FS) as usize, g);
        }
    }
    ev
}

#[derive(Clone, Copy)]
enum DsdKind {
    /// Joss et al. (1969) drizzle: N0 = 30000, Lambda = 5.7 R^-0.21 (many small drops)
    Drizzle,
    /// Marshall-Palmer: N0 = 8000, Lambda = 4.1 R^-0.21 (widespread rain)
    MarshallPalmer,
    /// Joss et al. (1969) thunderstorm: N0 = 1400, Lambda = 3.0 R^-0.21 (fewer small, more big drops)
    Thunderstorm,
}

/// One rain intensity: its drop sizes, what lies round the listener, and what else is heard.
#[derive(Clone, Copy)]
pub struct Scene {
    pub name: &'static str,
    pub rate_mm_h: f64,
    kind: DsdKind,
    /// surface shares of the ground (road takes the rest)
    puddle: f64,
    grass: f64,
    leaves: f64,
    /// big drops rendered one by one out to this radius (m); beyond it, and every small drop, as the wash
    r_events: f64,
    /// raised far-field sources (roofs, canopy, awnings) on the rings past 8 m, as a share of the ground's rain there;
    /// unlike the ground they are not shadowed by the ground effect
    elevated: f64,
    /// standing water (see drop_event)
    film: f64,
    /// the largest drop (mm): drizzle and light rain hold no big drops
    d_max: f64,
    /// random drips off the canopy (per second) on top of the fixed drip points
    canopy_drips: f64,
    /// fixed drip points (eaves, branches, gutters) and their median period (s)
    drips: usize,
    drip_period: f64,
    /// running water (gutters, downpipes, runoff) relative level
    runoff: f64,
    gust: f64,
    pub lufs: f64,
}

pub fn scene(name: &str) -> Option<Scene> {
    let s = match name {
        // drizzle to light rain: countable taps and drips over almost no wash
        "light" => Scene {
            name: "light",
            rate_mm_h: 0.7,
            kind: DsdKind::Drizzle,
            puddle: 0.05,
            grass: 0.25,
            leaves: 0.2,
            r_events: 8.0,
            elevated: 0.05,
            film: 1.0,
            d_max: 1.4,
            canopy_drips: 5.0,
            drips: 9,
            drip_period: 1.6,
            runoff: 0.0,
            gust: 0.3,
            lufs: -30.0,
        },
        // steady patter, a soft wash starting, quicker drips, a little runoff
        "medium" => Scene {
            name: "medium",
            rate_mm_h: 5.0,
            kind: DsdKind::MarshallPalmer,
            puddle: 0.12,
            grass: 0.22,
            leaves: 0.18,
            r_events: 8.0,
            elevated: 0.1,
            film: 1.5,
            d_max: 6.0,
            canopy_drips: 10.0,
            drips: 12,
            drip_period: 0.7,
            runoff: 0.25,
            gust: 0.4,
            lufs: -24.0,
        },
        // downpour: big drops, standing water everywhere, the wash on top, gutters running
        "heavy" => Scene {
            name: "heavy",
            rate_mm_h: 35.0,
            kind: DsdKind::Thunderstorm,
            puddle: 0.4,
            grass: 0.15,
            leaves: 0.15,
            r_events: 3.0,
            elevated: 1.5,
            film: 4.0,
            d_max: 6.0,
            canopy_drips: 0.0,
            drips: 0,
            drip_period: 1.0,
            runoff: 1.0,
            gust: 0.45,
            lufs: -17.0,
        },
        _ => return None,
    };
    Some(s)
}

/// Drop sizes 0.1-6 mm for a scene, as impacts per m^2 per s (N(D) v(D)).
fn dsd_for(sc: &Scene) -> Dsd {
    let r = sc.rate_mm_h;
    let (n0, lam) = match sc.kind {
        DsdKind::Drizzle => (30000.0, 5.7 * m::pow(r, -0.21)),
        DsdKind::MarshallPalmer => (8000.0, 4.1 * m::pow(r, -0.21)),
        DsdKind::Thunderstorm => (1400.0, 3.0 * m::pow(r, -0.21)),
    };
    let mut d = Vec::new();
    let mut cdf = Vec::new();
    let mut acc = 0.0;
    let step = 0.01;
    let mut x = 0.1;
    while x <= 6.0 {
        // above d_max a steep roll-off (0.1 mm) rather than a hard cut
        let cut = if x > sc.d_max { m::exp(-(x - sc.d_max) / 0.1) } else { 1.0 };
        acc += n0 * m::exp(-lam * x) * terminal_v(x) * step * cut;
        d.push(x);
        cdf.push(acc);
        x += step;
    }
    Dsd { d, cdf, flux: acc }
}

impl Dsd {
    /// impacts per m^2 per s with lo <= D < hi
    fn flux_between(&self, lo: f64, hi: f64) -> f64 {
        let at = |x: f64| -> f64 {
            let i = self.d.partition_point(|&v| v < x);
            if i == 0 {
                0.0
            } else {
                self.cdf[(i - 1).min(self.cdf.len() - 1)]
            }
        };
        at(hi) - at(lo)
    }
    fn sample_between(&self, rng: &mut Rng, lo: f64, hi: f64) -> f64 {
        // inverse CDF restricted to [lo, hi)
        let at = |x: f64| -> f64 {
            let i = self.d.partition_point(|&v| v < x);
            if i == 0 {
                0.0
            } else {
                self.cdf[(i - 1).min(self.cdf.len() - 1)]
            }
        };
        let (c0, c1) = (at(lo), at(hi));
        let u = c0 + rng.f() * (c1 - c0);
        let i = self.cdf.partition_point(|&c| c < u).min(self.d.len() - 1);
        (self.d[i] + rng.range(-0.005, 0.005)).clamp(lo, hi)
    }
}

const D_SPLIT: f64 = 1.2; // mm: smaller drops only ever as the wash
const RINGS: [(f64, f64); 5] = [(0.0, 3.0), (3.0, 8.0), (8.0, 20.0), (20.0, 50.0), (50.0, 150.0)];
const EVENT_CAP: f64 = 30000.0; // rendered drops per second per ring at most (thinned at equal energy above)
const NEAR_AREA: f64 = 0.4; // m^2 of nearby objects 0.5-1.2 m from the listener

fn pick_surface(rng: &mut Rng, sc: &Scene, rho: f64) -> Surf {
    let u = rng.f();
    let s = if u < sc.puddle {
        Surf::Puddle
    } else if u < sc.puddle + sc.grass {
        Surf::Grass
    } else if u < sc.puddle + sc.grass + sc.leaves {
        Surf::Leaves
    } else {
        Surf::Road
    };
    if s == Surf::Leaves && rho < 3.0 {
        Surf::Grass
    } else {
        s
    }
}

/// Distance low-pass cutoff for a drop rho metres out, r metres away: scattering by the ground, kerbs, cars and plants
/// past 4 m, and the air.
fn distance_fc(air: &AirCutoff, rho: f64, r: f64) -> f64 {
    let fc = if rho > 4.0 { (14000.0 * m::pow(4.0 / rho, 0.6)).clamp(1500.0, 16000.0) } else { 16000.0 };
    fc.min(air.at(r))
}

fn onepole(ev: &[f64], fc: f64) -> Vec<f64> {
    let a = dsp::onepole_coef(FS, fc);
    let mut s = 0.0;
    ev.iter()
        .map(|&x| {
            s += a * (x - s);
            s
        })
        .collect()
}

const SPEC_N: usize = 2048;

/// Mean energy spectrum (|X|^2 on SPEC_N bins) of a scene's drops of lo-hi mm at 1 m, from `count` sample events.
fn mean_spectrum(seed: u64, sc: &Scene, ds: &Dsd, lo: f64, hi: f64, count: usize, stream: &str) -> Vec<f64> {
    let mut rng = Rng::new(seed, stream);
    let mut acc = vec![0.0; SPEC_N / 2 + 1];
    for _ in 0..count {
        let rho = rng.range(3.0, 30.0);
        let surf = pick_surface(&mut rng, sc, rho);
        let d = ds.sample_between(&mut rng, lo, hi);
        let ev = drop_event(&mut rng, surf, d, terminal_v(d), sc.film);
        let mut x: Vec<dsp::C> = (0..SPEC_N).map(|i| dsp::C::new(ev.get(i).copied().unwrap_or(0.0), 0.0)).collect();
        dsp::fft(&mut x, false);
        for k in 0..=SPEC_N / 2 {
            acc[k] += x[k].re * x[k].re + x[k].im * x[k].im;
        }
    }
    acc.iter().map(|v| v / count as f64).collect()
}

/// White noise round the loop through a zero-phase FIR made from an energy spectrum s (|X|^2 summed per second, on
/// SPEC_N bins), circular, so the wash is seamless. Its mean square is (sum over the full circle of s) / (SPEC_N FS),
/// per Parseval: a Poisson stream of events with that summed spectrum.
fn shaped_noise(rng: &mut Rng, s: &[f64], n: usize) -> Vec<f64> {
    let taps = 1024;
    let mut h: Vec<dsp::C> = (0..taps)
        .map(|k| {
            let kk = if k <= taps / 2 { k } else { taps - k };
            dsp::C::new(m::sqrt(s[(2 * kk).min(SPEC_N / 2)].max(0.0)), 0.0)
        })
        .collect();
    dsp::fft(&mut h, true);
    let mut fir: Vec<f64> = (0..taps)
        .map(|i| {
            let j = (i + taps / 2) % taps;
            let w = 0.5 - 0.5 * m::cos(m::TAU * i as f64 / taps as f64);
            h[j].re * w
        })
        .collect();
    let want: f64 = s
        .iter()
        .enumerate()
        .map(|(k, v)| if k == 0 || k == SPEC_N / 2 { *v } else { 2.0 * v })
        .sum::<f64>()
        / (SPEC_N as f64 * FS);
    let got: f64 = fir.iter().map(|v| v * v).sum();
    let g = if got > 0.0 { m::sqrt(want / got) } else { 0.0 };
    for v in fir.iter_mut() {
        *v *= g;
    }
    // white noise of unit variance, then circular convolution
    let x: Vec<f64> = (0..n).map(|_| (rng.f() * 2.0 - 1.0) * 1.7320508075688772).collect();
    let mut y = vec![0.0; n];
    for (i, yv) in y.iter_mut().enumerate() {
        let mut acc = 0.0;
        for (k, &c) in fir.iter().enumerate() {
            let j = if i >= k { i - k } else { i + n - k };
            acc += c * x[j];
        }
        *yv = acc;
    }
    y
}

/// Running water: gutters and downpipes, runoff along the kerb. Streams of bubbles (van den Doel 2005: bubbles of
/// 0.5-6 mm, ~200 per second per stream) whose rate and level babble at 0.05-0.35 Hz, low-passed by their 3-8 m.
fn runoff(seed: u64, n: usize) -> Vec<f64> {
    let mut rng = Rng::new(seed, "rain-runoff");
    let mut buf = vec![0.0; n];
    for _src in 0..2 {
        let babble = Gust::new(rng.next_u64(), "rain-runoff-babble", 0.7);
        let r = rng.range(3.0, 8.0);
        let fc = (14000.0 * m::pow(4.0 / r, 0.6)).clamp(2500.0, 8000.0);
        let count = rng.poisson(babble.draws(200.0 * LOOP_S));
        for _ in 0..count {
            let t = rng.f() * LOOP_S;
            if !babble.accept(&mut rng, t) {
                continue;
            }
            let rad = rng.lognormal(1.8e-3, 0.55).clamp(0.5e-3, 6e-3);
            let b = bubble(&mut rng, rad);
            let g = rng.lognormal(1.0, 0.6) * m::sqrt(rad / 1e-3) / r;
            put(&mut buf, (t * FS) as usize, &onepole(&b, fc), g);
        }
    }
    buf
}

/// Fixed drip points round the listener (eaves, branches, a gutter's seam, a sign): each drops a big drop (3.5-5.5 mm)
/// onto its own spot every ~`period` s (+/-15 %), so the same plink or tick comes back from the same place.
fn drips(seed: u64, sc: &Scene, air: &AirCutoff, buf: &mut [f64]) -> usize {
    let mut rng = Rng::new(seed, "rain-drips");
    let mut total = 0;
    for _ in 0..sc.drips {
        let rho = rng.range(1.2, 9.0);
        let u = rng.f();
        let surf = if u < 0.4 {
            Surf::Puddle
        } else if u < 0.65 {
            Surf::Road
        } else if u < 0.85 {
            Surf::Metal
        } else {
            Surf::Leaves
        };
        let d0 = rng.range(3.5, 5.5);
        let fall = rng.range(1.0, 4.0);
        let v = m::sqrt(2.0 * 9.81 * fall).min(terminal_v(d0));
        let r = m::sqrt(rho * rho + OBS_H * OBS_H);
        let fc = distance_fc(air, rho, r);
        // a whole number of drips per loop keeps the loop seamless
        let period = rng.lognormal(sc.drip_period, 0.4).clamp(0.25, 5.0);
        let per_loop = (LOOP_S / period).round().max(1.0) as usize;
        let phase = rng.f();
        // its puddle's own bubble size (the plink's pitch)
        let rb = rng.lognormal(1.3e-3, 0.35).clamp(0.5e-3, 3e-3);
        for k in 0..per_loop {
            let t = ((k as f64 + phase + rng.range(-0.15, 0.15)) / per_loop as f64).rem_euclid(1.0) * LOOP_S;
            let d = d0 * rng.range(0.95, 1.05);
            let mut ev = drop_event(&mut rng, if surf == Surf::Puddle { Surf::Road } else { surf }, d, v, 1.0);
            if surf == Surf::Puddle {
                for x in ev.iter_mut() {
                    *x *= 0.7;
                }
                if rng.chance(0.8) {
                    let rr = rb * rng.range(0.92, 1.08);
                    let b = bubble(&mut rng, rr);
                    mix_into(&mut ev, &b, (rng.range(0.001, 0.004) * FS) as usize, 0.6 * impact_amp(d, v));
                }
            }
            put(buf, (t * FS) as usize, &onepole(&ev, fc), 1.0 / r);
            total += 1;
        }
    }
    total
}

pub struct OutdoorStats {
    /// drops landing per second within 150 m (all sizes)
    pub drops_per_s: f64,
    /// drops of 1.2 mm and more per second within 3 m
    pub big_per_s_3m: f64,
    /// individually rendered drops per second (after thinning)
    pub rendered_per_s: f64,
    pub drips_per_s: f64,
    /// the wash's share of the drop sound's energy (dB)
    pub wash_share_db: f64,
}

/// The outdoor rain of a scene: big drops one by one near the listener, everything else (small drops, far rings) as
/// the wash: noise with the drops' mean spectrum at their total power. Plus drips and running water.
pub fn outdoor(seed: u64, sc: &Scene) -> (Vec<f64>, OutdoorStats) {
    let n = (LOOP_S * FS) as usize;
    let mut buf = vec![0.0; n];
    let ds = dsd_for(sc);
    let mut rng = Rng::new(seed, &format!("rain-outdoor-{}", sc.name));
    let air = AirCutoff::new();
    let gust = Gust::new(seed, "rain-gust", sc.gust);
    let h = OBS_H;
    let flux_small = ds.flux_between(0.0, D_SPLIT);
    let flux_big = ds.flux_between(D_SPLIT, 10.0);
    let spec_small = mean_spectrum(seed, sc, &ds, 0.1, D_SPLIT, 1500, "rain-spec-small");
    let spec_big = mean_spectrum(seed, sc, &ds, D_SPLIT, 6.0, 1500, "rain-spec-big");
    let mut wash = vec![0.0; SPEC_N / 2 + 1];
    let mut rendered = 0usize;
    let mut big_near = 0.0;
    for &(r1, r2) in RINGS.iter() {
        let area = m::PI * (r2 * r2 - r1 * r1);
        // the ring's drops at 1/r^2: the area integral of 1/(rho^2 + h^2)
        let geo = m::PI * m::ln((r2 * r2 + h * h) / (r1 * r1 + h * h));
        let rho_mid = m::sqrt(0.5 * (r1 * r1 + r2 * r2));
        let r_mid = m::sqrt(rho_mid * rho_mid + h * h);
        let fc = distance_fc(&air, rho_mid, r_mid);
        // Ground effect: a source on the ground heard 1.6 m up at grazing incidence over soft, wet ground loses
        // several dB more than spreading (the ground dip spans ~300 Hz-2 kHz); raised sources are not shadowed.
        let ground_ex_db = if r1 >= 50.0 { 12.0 } else if r1 >= 20.0 { 8.0 } else if r1 >= 8.0 { 4.0 } else { 0.0 };
        let env = m::pow(10.0, -ground_ex_db / 10.0) + if r1 >= 8.0 { sc.elevated } else { 0.0 };
        let lp2: Vec<f64> = (0..=SPEC_N / 2)
            .map(|k| {
                let f = k as f64 * FS / SPEC_N as f64;
                (1.0 / (1.0 + (f / fc) * (f / fc))) * m::pow(10.0, -crate::thunder::alpha(f) * r_mid / 10.0)
            })
            .collect();
        for k in 0..=SPEC_N / 2 {
            wash[k] += flux_small * geo * env * spec_small[k] * lp2[k];
        }
        if r2 <= 3.0 + 1e-9 {
            big_near = flux_big * area;
        }
        if r2 <= sc.r_events + 1e-9 {
            let expected = flux_big * area;
            let k_thin = (expected / EVENT_CAP).max(1.0);
            let count = rng.poisson(gust.draws(expected / k_thin * LOOP_S));
            for _ in 0..count {
                let t = rng.f() * LOOP_S;
                if !gust.accept(&mut rng, t) {
                    continue;
                }
                let rho = m::sqrt(rng.range(r1 * r1, r2 * r2));
                let surf = pick_surface(&mut rng, sc, rho);
                let d = ds.sample_between(&mut rng, D_SPLIT, 6.0);
                let v = terminal_v(d);
                let z = if surf == Surf::Leaves { rng.range(2.5, 8.0) } else { 0.0 };
                let ev = drop_event(&mut rng, surf, d, v, sc.film);
                let dz = h - z;
                let r = m::sqrt(rho * rho + dz * dz).max(0.5);
                put(&mut buf, (t * FS) as usize, &onepole(&ev, distance_fc(&air, rho, r)), m::sqrt(k_thin) / r);
                rendered += 1;
            }
        } else {
            for k in 0..=SPEC_N / 2 {
                wash[k] += flux_big * geo * env * spec_big[k] * lp2[k];
            }
        }
    }
    // The listener's immediate surroundings (0.5-1.2 m: a bush, a mirror or sign, a puddle at the kerb; 0.4 m^2):
    // few drops a second land this close, but each is several dB over the bed, the patter one hears as single drops.
    {
        let mut orng = Rng::new(seed, &format!("rain-near-{}", sc.name));
        let expected = ds.flux_between(0.5, 10.0) * NEAR_AREA;
        let count = orng.poisson(gust.draws(expected * LOOP_S));
        for _ in 0..count {
            let t = orng.f() * LOOP_S;
            if !gust.accept(&mut orng, t) {
                continue;
            }
            let u = orng.f();
            let surf = if u < 0.5 { Surf::Leaves } else if u < 0.8 { Surf::Metal } else { Surf::Puddle };
            let d = ds.sample_between(&mut orng, 0.5, 6.0);
            let ev = drop_event(&mut orng, surf, d, terminal_v(d), sc.film);
            let r = orng.range(0.5, 1.2);
            put(&mut buf, (t * FS) as usize, &ev, 1.0 / r);
            rendered += 1;
        }
    }
    // random drips off the canopy: big drops (3.5-5.5 mm from 2-8 m) onto the ground or a puddle round the listener
    let mut canopy = 0usize;
    {
        let mut crng = Rng::new(seed, &format!("rain-canopy-{}", sc.name));
        let count = crng.poisson(gust.draws(sc.canopy_drips * LOOP_S));
        for _ in 0..count {
            let t = crng.f() * LOOP_S;
            if !gust.accept(&mut crng, t) {
                continue;
            }
            let rho = crng.range(1.5, 12.0);
            let surf = if crng.chance(0.35) { Surf::Puddle } else if crng.chance(0.5) { Surf::Grass } else { Surf::Road };
            let d = crng.range(3.5, 5.5);
            let v = m::sqrt(2.0 * 9.81 * crng.range(2.0, 8.0)).min(terminal_v(d));
            let ev = drop_event(&mut crng, surf, d, v, sc.film);
            let r = m::sqrt(rho * rho + h * h);
            put(&mut buf, (t * FS) as usize, &onepole(&ev, distance_fc(&air, rho, r)), 1.0 / r);
            canopy += 1;
        }
    }
    let mut nrng = Rng::new(seed, &format!("rain-wash-{}", sc.name));
    let mut w = shaped_noise(&mut nrng, &wash, n);
    // the wash swells with the gusts too (power ~ rate)
    for (i, v) in w.iter_mut().enumerate() {
        *v *= m::sqrt(gust.rate(i as f64 / FS) / gust.mean);
    }
    let e_ev: f64 = buf.iter().map(|v| v * v).sum();
    let e_w: f64 = w.iter().map(|v| v * v).sum();
    for i in 0..n {
        buf[i] += w[i];
    }
    let nd = drips(seed, sc, &air, &mut buf);
    if sc.runoff > 0.0 {
        let ro = runoff(seed, n);
        let g = sc.runoff * 0.3 * rms(&buf) / rms(&ro).max(1e-30);
        for i in 0..n {
            buf[i] += g * ro[i];
        }
    }
    let stats = OutdoorStats {
        drops_per_s: ds.flux * m::PI * 150.0 * 150.0,
        big_per_s_3m: big_near,
        rendered_per_s: rendered as f64 / LOOP_S,
        drips_per_s: (nd + canopy) as f64 / LOOP_S,
        wash_share_db: 10.0 * m::log10(e_w.max(1e-30) / (e_ev + e_w).max(1e-30)),
    };
    (buf, stats)
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
        // The headliner, its glue and the roof bows damp the lowest modes hardest (loss factor ~0.15 at 60 Hz):
        // lightly damped, the (1,1) mode would ring as a steady drone under thousands of drops a second.
        let eta_f = eta * (1.0 + f / 2000.0) + 0.12 * m::exp(-f / 250.0);
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
pub fn roof(seed: u64, sc: &Scene, outside: &[f64]) -> Vec<f64> {
    let n = (LOOP_S * FS) as usize;
    let mut buf = vec![0.0; n];
    let ds = dsd_for(sc);
    let mut rng = Rng::new(seed, "rain-roof-drops");
    // the same gusts as the outdoor rain of this seed, so the cabin and the outside swell together
    let gust = Gust::new(seed, "rain-gust", sc.gust);
    let make = |rng: &mut Rng, area: f64| -> Vec<(usize, f64, f64, f64, f64)> {
        let cnt = rng.poisson(gust.draws(ds.flux * area * LOOP_S));
        let mut v = Vec::with_capacity(cnt);
        for _ in 0..cnt {
            let d = ds.sample(rng);
            let vt = terminal_v(d);
            let mass = 1000.0 * m::PI / 6.0 * (d * 1e-3) * (d * 1e-3) * (d * 1e-3);
            let t = rng.f() * LOOP_S;
            let (x, y) = (rng.f(), rng.f());
            if !gust.accept(rng, t) {
                continue;
            }
            v.push(((t * FS) as usize, x, y, mass * vt * 1e4, d * 1e-3 / vt));
        }
        v.sort_by_key(|e| e.0);
        v
    };
    let mut roof_drops = make(&mut rng, 1.30 * 1.45);
    // Parked under a tree or a roof edge in light and medium rain: a few drip points onto the roof, each a big drop
    // (4-5.5 mm from 2-6 m) at its own spot every ~1.5 drip periods (a whole number per loop).
    if sc.drips > 0 {
        let mut drng = Rng::new(seed, "rain-roof-drips");
        for _ in 0..3 {
            let (x, y) = (drng.range(0.1, 0.9), drng.range(0.1, 0.9));
            let d0 = drng.range(4.0, 5.5);
            let v = m::sqrt(2.0 * 9.81 * drng.range(2.0, 6.0)).min(terminal_v(d0));
            let period = drng.lognormal(1.5 * sc.drip_period, 0.4).clamp(0.3, 6.0);
            let per_loop = (LOOP_S / period).round().max(1.0) as usize;
            let phase = drng.f();
            for k in 0..per_loop {
                let t = ((k as f64 + phase + drng.range(-0.15, 0.15)) / per_loop as f64).rem_euclid(1.0) * LOOP_S;
                let d = d0 * drng.range(0.95, 1.05);
                let mass = 1000.0 * m::PI / 6.0 * (d * 1e-3) * (d * 1e-3) * (d * 1e-3);
                roof_drops.push(((t * FS) as usize, x, y, mass * v * 1e4, d * 1e-3 / v));
            }
        }
        roof_drops.sort_by_key(|e| e.0);
    }
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

