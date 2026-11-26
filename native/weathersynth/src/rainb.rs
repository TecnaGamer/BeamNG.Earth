//! Method B rain: a physical model of rain sound, drop by drop (the counterpart of thunder method B; the parameters and
//! their sources are in ~/bnge-tests/weather/rain_research/PARAMS.md).
//!
//!   weathersynth rainb --kind light|medium|heavy|roof|cabin_light|cabin|cabin_heavy|muffled --seed N --out F.wav
//!   weathersynth rainb-set --out DIR [--seed N] [--ogg] [--only a,b]     the reference loops (bne_rainb_<kind>)
//!   weathersynth rainb-stats                                             the drop statistics per rain rate
//!
//! The model, in order:
//! - **Drops.** A gamma drop-size distribution N(D) = N0 D^mu exp(-Lambda D) (Ulbrich 1983), mu = 1, its median
//!   volume diameter D0 = c R^0.21 with c = 0.90 (Marshall-Palmer) up to 5 mm/h, blending to Joss thunderstorm (1.22)
//!   at >= 25 mm/h (Joss, Thams & Waldvogel 1968), N0 set so the distribution carries exactly the rain rate
//!   (R = 6 pi 1e-4 Int D^3 v N dD), the largest drop D_max = min(6, 2.5 D0 + 0.5) mm. Drops land at their terminal speed
//!   (Atlas et al. 1973). Each drop size band (0.4-0.8 ... 4.5-6 mm) and each distance ring is its own Poisson stream.
//! - **The impact** (every surface): the drop's force on the surface F(t) = Fmax sqrt(t/tp) e^((1 - t/tp) / 2): the
//!   early rise F ~ t^0.5 and the peak at tp = 0.18 D/V (times the surface's compliance) of Gordillo, Sun & Cheng
//!   (2018), its impulse the drop's momentum, so a rigid surface sees Fmax = 0.70 rho V^2 D^2 (they measure 0.83). The force radiates as a vertical dipole on the surface: p = cos(theta)
//!   dF/dt / (4 pi c r). Its spectrum peaks at 1 / (2 pi tp): bigger drops are lower and louder, and the radiated energy
//!   goes as D^3 V^5 (kinetic energy x Mach^3, as Guo & Ffowcs Williams 1991). The dipole makes far drops on the ground
//!   quieter than 1/r (grazing), so the near drops stand out and the far field is a wash of what radiates every way.
//! - **Splash** (wet surfaces, leaves): above the crown-splash threshold K = We Oh^-0.4 > 2100 + 5880 h*^1.44
//!   (Cossali et al. 1997; h* film depth / D) the crown's sheet and rim break up: a short noise burst (2-8 kHz) lasting
//!   about a quarter of the capillary time sqrt(rho D^3 / sigma). Drops under ~1.2 mm never splash.
//! - **Bubbles** (puddles, standing water): Minnaert rings f = 3.26 / a (a in m), damped and rising in pitch as in van den
//!   Doel (2005). Drops of 0.8-1.1 mm at terminal speed entrain a bubble every time (regular entrainment, Pumphrey &
//!   Elmore 1990: a ~0.18-0.25 mm, 13-18 kHz); 1.1-2.2 mm drops are quiet (impact only); bigger drops entrain large
//!   bubbles irregularly (1-6 kHz, more often the bigger the drop: Nystuen 2001's categories).
//! - **Leaves:** the drop's impulse on a leaf excites its damped bending modes (150 Hz - 3 kHz, Q 3-10) and the leaf
//!   splashes it; under the canopy big drips (4-5.5 mm) fall from 2-10 m at less than terminal speed.
//! - **Soil and grass:** compliant (tp x4): a dull, quiet thud.
//! - **Metal** (parked cars, signs; the car's own panels): the force drives the panel's plate modes (f_mn of a curved
//!   steel panel, f11 40-90 Hz, loss factor 1-4 %), each hit at its own place on the panel (mode shapes), so one panel
//!   keeps its voice.
//! - **Distance:** 1/r, the dipole's cos(theta), air absorption (ISO 9613-1) and scattering by kerbs, cars and plants
//!   along the ground for ground-level drops, as 1/3-octave low-pass bins (each bin filtered once round the loop). Rings out to 400 m; streams above CAP events a second are thinned at equal
//!   energy (each event sqrt(k) louder), per size band, so the rare big drops are never thinned.
//! - **Scenes:** the open road (light / medium / heavy), next to the car (its panels within 0.8-2 m), inside the car
//!   (the roof panel's modes driven by every hit on it, a headliner, the windscreen's ticks, the outside through the
//!   body) and under an open shelter (a 12 m deck: no drops overhead, the open sides, its edges dripping, the deck's
//!   reflection).
//! - **Loops:** everything is written round a circular buffer and the filters run settled round it: seamless. Gusts swing
//!   the rate +/-30-45 % over seconds (whole periods per loop) and rain's clustering flickers it at 0.5-8 Hz. The loop
//!   is set to the class's loudness (the current loops': light -30.2, medium -24.2, heavy -17.2 LUFS ...) with a
//!   true-peak ceiling.
//! - **Deterministic:** NpRng (numpy's PCG64) streams named by part, libm maths, one thread, sums in a fixed order.

use crate::dsp::Biquad;
use crate::loudness;
use crate::m;
use crate::nprng::NpRng;
use crate::thunder::cutoff_for_path;

pub const FS: f64 = 44100.0;
pub(crate) const OBS_H: f64 = 1.6;
const RHO_W: f64 = 998.0;
const SIGMA_W: f64 = 0.0728;
const MU_W: f64 = 1.0e-3;
const C_AIR: f64 = 343.0;
const G: f64 = 9.81;
const D_MIN: f64 = 0.4; // mm: smaller drops are 50 dB under a 2 mm drop (D^3 V^5), left out
const CEIL_DB: f64 = -1.0;
const LIM_DB: f64 = -1.5;

/// The size bands (mm): each its own stream, so the thinning never touches the rare big drops.
pub(crate) const BANDS: [(f64, f64); 7] = [(0.4, 0.8), (0.8, 1.1), (1.1, 1.6), (1.6, 2.2), (2.2, 3.0), (3.0, 4.5), (4.5, 6.0)];
/// Distance rings (m, horizontal).
pub(crate) const RINGS: [(f64, f64); 7] = [(0.0, 1.5), (1.5, 4.0), (4.0, 10.0), (10.0, 25.0), (25.0, 60.0), (60.0, 150.0), (150.0, 400.0)];

fn fnv(s: &str) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in s.bytes() {
        h ^= b as u64;
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    h
}

fn rng(seed: u64, stream: &str) -> NpRng {
    NpRng::new(&[seed, fnv(stream)])
}

fn lerp(a: f64, b: f64, t: f64) -> f64 {
    a + (b - a) * t.clamp(0.0, 1.0)
}

fn poisson(r: &mut NpRng, lam: f64) -> usize {
    if lam <= 0.0 {
        return 0;
    }
    if lam < 9.0 {
        return r.poisson_small(lam) as usize;
    }
    // normal approximation above (the counts here are only ever large means: events per loop)
    let v = lam + m::sqrt(lam) * r.standard_normal();
    m::round(v).max(0.0) as usize
}

// ------------------------------------------------------------------ tuning (the calibration constants)

/// The constants not fixed by the physics: relative levels of the parts, set against the real takes (rb_score.py).
/// WEATHERSYNTH_RB="name=value,..." overrides them for a tuning sweep.
#[derive(Clone, Copy, Debug)]
pub struct Tune {
    /// crown noise RMS relative to the impact click's peak (dB)
    pub crown_db: f64,
    /// a large bubble's amplitude (radius 1 mm) relative to a 2 mm drop's click peak at 1 m (dB)
    pub bub_db: f64,
    /// a regular-entrainment bubble's amplitude, same reference (dB)
    pub reg_db: f64,
    /// leaf modes, same reference, for a 2 mm drop at terminal speed (dB)
    pub leaf_db: f64,
    /// a click on soil / grass relative to a rigid surface (dB)
    pub soil_db: f64,
    /// metal panel modes, same reference (dB): parked cars and signs; the car's own panels (next to the car)
    pub metal_db: f64,
    pub car_db: f64,
    /// the impact dipole's share radiated every way (rough surfaces, the crown), 0..1
    pub iso: f64,
    /// rendered events per second per (ring, size band) at most
    pub cap: f64,
    /// cabin: windscreen ticks and the outside through the body, relative to the roof (dB)
    pub glass_db: f64,
    pub body_db: f64,
    /// cabin: the headliner's low-pass (Hz)
    pub liner_hz: f64,
    /// canopy drips' level (dB) and the share of intercepted rain that drips
    pub drip_db: f64,
    pub drip_frac: f64,
    /// clustering: the rate's 0.5-8 Hz flicker (std of ln rate) near (< 4 m) and farther out
    pub fast_near: f64,
    pub fast_far: f64,
    /// cabin: the closed cabin's low-frequency gain below 150 Hz (dB)
    pub cabin_lf_db: f64,
    /// cabin: the roof hits' own clicks (above 1.5 kHz) relative to the roof's modes (dB)
    pub roofhf_db: f64,
    /// cabin: the roof hits' clustering flicker (the panel averages over its 1.9 m^2)
    pub fast_cabin: f64,
}

impl Tune {
    pub fn get() -> Tune {
        let mut t = Tune {
            crown_db: -18.0,
            bub_db: -4.0,
            reg_db: -20.0,
            leaf_db: -12.0,
            soil_db: -10.0,
            metal_db: -10.0,
            car_db: -4.0,
            iso: 0.25,
            cap: 3000.0,
            glass_db: -17.0,
            body_db: -6.0,
            liner_hz: 1000.0,
            drip_db: 0.0,
            drip_frac: 0.25,
            fast_near: 0.35,
            fast_far: 0.15,
            cabin_lf_db: 3.0,
            roofhf_db: -22.0,
            fast_cabin: 0.2,
        };
        if let Ok(s) = std::env::var("WEATHERSYNTH_RB") {
            for kv in s.split(',') {
                let mut it = kv.splitn(2, '=');
                let (Some(k), Some(v)) = (it.next(), it.next()) else { continue };
                let Ok(v) = v.trim().parse::<f64>() else { continue };
                match k.trim() {
                    "crown_db" => t.crown_db = v,
                    "bub_db" => t.bub_db = v,
                    "reg_db" => t.reg_db = v,
                    "leaf_db" => t.leaf_db = v,
                    "soil_db" => t.soil_db = v,
                    "metal_db" => t.metal_db = v,
                    "car_db" => t.car_db = v,
                    "iso" => t.iso = v,
                    "cap" => t.cap = v,
                    "glass_db" => t.glass_db = v,
                    "body_db" => t.body_db = v,
                    "liner_hz" => t.liner_hz = v,
                    "drip_db" => t.drip_db = v,
                    "drip_frac" => t.drip_frac = v,
                    "fast_near" => t.fast_near = v,
                    "fast_far" => t.fast_far = v,
                    "cabin_lf_db" => t.cabin_lf_db = v,
                    "roofhf_db" => t.roofhf_db = v,
                    "fast_cabin" => t.fast_cabin = v,
                    _ => {}
                }
            }
        }
        t
    }
}

// ------------------------------------------------------------------ drops

/// Terminal speed (m/s) of a drop of d mm: Atlas, Srivastava & Sekhon (1973), a fit to Gunn & Kinzer (1949).
pub fn terminal_v(d_mm: f64) -> f64 {
    (9.65 - 10.3 * m::exp(-0.6 * d_mm)).max(0.1)
}

/// Speed (m/s) of a drop of d mm after falling h m from rest (quadratic drag: v = vt sqrt(1 - exp(-2 g h / vt^2))).
pub fn fall_speed(d_mm: f64, h: f64) -> f64 {
    let vt = terminal_v(d_mm);
    vt * m::sqrt(1.0 - m::exp(-2.0 * G * h / (vt * vt)))
}

/// The drop-size distribution for a rain rate (mm/h).
#[derive(Clone, Debug)]
pub struct Dsd {
    #[allow(dead_code)]
    pub rate: f64,
    pub mu: f64,
    pub lam: f64,
    pub n0: f64,
    pub d0: f64,
    pub dmax: f64,
    d: Vec<f64>,
    /// cumulative impacts per m^2 per s (D >= D_MIN)
    cdf: Vec<f64>,
}

const DSD_STEP: f64 = 0.005;

impl Dsd {
    pub fn new(r: f64) -> Dsd {
        let r = r.max(0.01);
        let x = m::log10(r);
        let l5 = m::log10(5.0);
        let l25 = m::log10(25.0);
        // D0 = c R^0.21: Marshall-Palmer (Lambda = 4.1 R^-0.21 -> c = 3.67 / 4.1) up to 5 mm/h (Joss drizzle is drizzle,
        // not light rain), blending in log rate to Joss thunderstorm (3.0) at 25 mm/h
        let c = if r <= 5.0 {
            3.67 / 4.1
        } else {
            lerp(3.67 / 4.1, 3.67 / 3.0, (x - l5) / (l25 - l5))
        };
        let d0 = c * m::pow(r, 0.21);
        let mu = 1.0;
        let lam = (3.67 + mu) / d0;
        let dmax = (2.5 * d0 + 0.5).min(6.0);
        let shape = |dd: f64| -> f64 {
            let taper = if dd > dmax { m::exp(-(dd - dmax) / 0.1) } else { 1.0 };
            m::pow(dd, mu) * m::exp(-lam * dd) * taper
        };
        // N0 from the rate: R = 6 pi 1e-4 Int D^3 v(D) N(D) dD (D mm, v m/s, N per m^3 per mm)
        let mut acc = 0.0;
        let mut dd = 0.05;
        while dd <= 6.5 {
            acc += dd * dd * dd * terminal_v(dd) * shape(dd) * DSD_STEP;
            dd += DSD_STEP;
        }
        let n0 = r / (6.0 * m::PI * 1e-4 * acc);
        let mut d = Vec::new();
        let mut cdf = Vec::new();
        let mut c_acc = 0.0;
        let mut dd = D_MIN;
        while dd <= 6.5 {
            c_acc += n0 * shape(dd) * terminal_v(dd) * DSD_STEP;
            d.push(dd);
            cdf.push(c_acc);
            dd += DSD_STEP;
        }
        Dsd { rate: r, mu, lam, n0, d0, dmax, d, cdf }
    }

    fn at(&self, x: f64) -> f64 {
        let i = self.d.partition_point(|&v| v < x);
        if i == 0 {
            0.0
        } else {
            self.cdf[(i - 1).min(self.cdf.len() - 1)]
        }
    }

    /// Impacts per m^2 per s with lo <= D < hi.
    pub fn flux(&self, lo: f64, hi: f64) -> f64 {
        self.at(hi) - self.at(lo)
    }

    /// A drop size from the distribution, restricted to [lo, hi).
    pub fn sample(&self, r: &mut NpRng, lo: f64, hi: f64) -> f64 {
        let (c0, c1) = (self.at(lo), self.at(hi));
        let u = c0 + r.random() * (c1 - c0);
        let i = self.cdf.partition_point(|&c| c < u).min(self.d.len() - 1);
        (self.d[i] - DSD_STEP * r.random()).clamp(lo, hi)
    }

    /// Number concentration (per m^3) and the volume-weighted flux per size band, for the stats table.
    pub fn concentration(&self, lo: f64, hi: f64) -> f64 {
        let mut acc = 0.0;
        let mut dd = lo;
        while dd < hi {
            let taper = if dd > self.dmax { m::exp(-(dd - self.dmax) / 0.1) } else { 1.0 };
            acc += self.n0 * m::pow(dd, self.mu) * m::exp(-self.lam * dd) * taper * DSD_STEP;
            dd += DSD_STEP;
        }
        acc
    }
}

/// Crown-splash parameter K = We Oh^-0.4 of a drop of d mm at v m/s.
fn splash_k(d_mm: f64, v: f64) -> f64 {
    let d = d_mm * 1e-3;
    let we = RHO_W * v * v * d / SIGMA_W;
    let oh = MU_W / m::sqrt(RHO_W * SIGMA_W * d);
    we * m::pow(oh, -0.4)
}

/// Cossali et al. (1997): the splash threshold on a film of relative depth h* = film / D (deep water: h* -> 1).
fn splash_kc(hstar: f64) -> f64 {
    2100.0 + 5880.0 * m::pow(hstar.clamp(0.0, 1.0), 1.44)
}

// ------------------------------------------------------------------ one drop's sound

/// One event at 1 m: the impact dipole's part (scaled by the listener's cos(theta)) and the part radiated every way.
#[derive(Default)]
pub(crate) struct Ev {
    pub(crate) dip: Vec<f64>,
    pub(crate) iso: Vec<f64>,
}


/// The impact force F(t) = Fmax sqrt(t/tp) e^((1 - t/tp) / 2): the early rise F ~ t^0.5 and the peak at tp = 0.18 D/V
/// of Gordillo, Sun & Cheng (2018), its impulse the drop's momentum (Int = Fmax tp e^0.5 Gamma(1.5) 2^1.5 =
/// 4.132 Fmax tp), so a rigid surface sees Fmax = 0.70 rho V^2 D^2 (they measure 0.83). `compliance` stretches tp.
/// Returns (tp s, Fmax N).
fn force(d_mm: f64, v: f64, compliance: f64) -> (f64, f64) {
    let d = d_mm * 1e-3;
    let tp = 0.18 * d / v.max(0.1) * compliance;
    let mass = RHO_W * m::PI / 6.0 * d * d * d;
    (tp, mass * v / (FORCE_INT * tp))
}

const FORCE_INT: f64 = 4.132;

/// The force at t (s) for (tp, Fmax).
#[inline]
fn force_at(t: f64, tp: f64, fmax: f64) -> f64 {
    if t <= 0.0 {
        return 0.0;
    }
    let u = t / tp;
    fmax * m::sqrt(u) * m::exp(0.5 * (1.0 - u))
}

/// The click: p(t) = dF/dt / (4 pi c) at 1 m on the dipole's axis, as the force's change over each sample (the mean
/// derivative over the sample: band-limits the t^-0.5 start), starting `frac` of a sample late. Returns the click's
/// nominal peak Fmax / tp / (4 pi c), the reference the other parts' levels are set against.
fn click(out: &mut Vec<f64>, d_mm: f64, v: f64, compliance: f64, frac: f64, g: f64) -> f64 {
    let (tp, fmax) = force(d_mm, v, compliance);
    let k = g * FS / (4.0 * m::PI * C_AIR);
    let n = (22.0 * tp * FS).ceil() as usize + 3;
    if out.len() < n {
        out.resize(n, 0.0);
    }
    let mut prev = 0.0;
    for (i, o) in out.iter_mut().enumerate().take(n) {
        let f = force_at((i as f64 + 1.0 - frac) / FS, tp, fmax);
        *o += k * (f - prev);
        prev = f;
    }
    fmax / tp / (4.0 * m::PI * C_AIR)
}

/// The reference: a 2 mm drop's nominal click peak on a rigid surface at terminal speed, 1 m on axis (Pa).
pub(crate) fn p_ref() -> f64 {
    let v = terminal_v(2.0);
    let (tp, fmax) = force(2.0, v, 1.0);
    fmax / tp / (4.0 * m::PI * C_AIR)
}

/// Band-limited noise burst: attack `att` s, exponential decay `tau` s, through a band-pass.
fn burst(r: &mut NpRng, out: &mut Vec<f64>, off: usize, att: f64, tau: f64, fc: f64, q: f64, rms: f64) {
    let n = ((att + 5.0 * tau) * FS).ceil() as usize + 2;
    let mut bp = Biquad::bandpass(FS, fc.min(0.45 * FS), q);
    // unit-RMS white noise into a band-pass of bandwidth fc / q: the RMS out ~ sqrt(bw / (FS / 2))
    let norm = 1.0 / m::sqrt(((fc / q) / (FS / 2.0)).clamp(1e-4, 1.0));
    if out.len() < off + n {
        out.resize(off + n, 0.0);
    }
    let na = (att * FS).max(1.0);
    let dk = m::exp(-1.0 / (tau * FS));
    let mut env = 1.0;
    let g = norm * rms;
    for i in 0..n {
        let e = if (i as f64) < na {
            i as f64 / na
        } else {
            env *= dk;
            env
        };
        let w = (r.random() * 2.0 - 1.0) * 1.7320508075688772;
        out[off + i] += bp.tick(w) * e * g;
    }
}

/// A Minnaert bubble of radius a (m): f = 3.26 / a, damping d = 0.043 f + 0.0014 f^1.5 per s and a pitch rise
/// sigma = 0.1 d (van den Doel 2005), starting at zero phase (no click).
pub(crate) fn bubble(out: &mut Vec<f64>, off: usize, a: f64, amp: f64) {
    let f0 = 3.26 / a;
    if f0 > 0.45 * FS {
        return;
    }
    let d = 0.043 * f0 + 0.0014 * m::pow(f0, 1.5);
    let sigma = 0.1 * d;
    let n = ((6.9 / d) * FS).ceil() as usize + 1;
    if out.len() < off + n {
        out.resize(off + n, 0.0);
    }
    // z = e^(i phase) rotated by the instantaneous frequency (refreshed every 16 samples), times the decay
    let (mut zr, mut zi) = (amp, 0.0);
    let dk = m::exp(-d / FS);
    let (mut cr, mut ci) = (1.0, 0.0);
    for i in 0..n {
        if i % 16 == 0 {
            let t = (i as f64 + 8.0) / FS;
            let w = m::TAU * f0 * (1.0 + sigma * t) / FS;
            cr = m::cos(w) * dk;
            ci = m::sin(w) * dk;
        }
        let nr = zr * cr - zi * ci;
        zi = zr * ci + zi * cr;
        zr = nr;
        out[off + i] += zi;
    }
}

/// Damped modes: (f Hz, Q, amplitude) summed into out at off.
fn modes(out: &mut Vec<f64>, off: usize, md: &[(f64, f64, f64)]) {
    for &(f, q, a) in md {
        if f >= 0.45 * FS || a == 0.0 {
            continue;
        }
        let dec = m::PI * f / q;
        let n = ((6.9 / dec) * FS).ceil() as usize + 1;
        if out.len() < off + n {
            out.resize(off + n, 0.0);
        }
        // a recursive oscillator (exact damped sine), no sin() per sample
        let w = m::TAU * f / FS;
        let rr = m::exp(-dec / FS);
        let c1 = 2.0 * rr * m::cos(w);
        let c2 = rr * rr;
        let (mut y1, mut y2) = (0.0, 0.0);
        for i in 0..n {
            let x = if i == 1 { a * m::sin(w) * rr } else { 0.0 };
            let y = c1 * y1 - c2 * y2 + x;
            y2 = y1;
            y1 = y;
            out[off + i] += y;
        }
    }
}

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Surf {
    /// wet asphalt / concrete: a thin film over a rigid surface
    Road,
    /// standing water
    Puddle,
    Soil,
    Leaf,
    /// a metal panel: index into the scene's panels
    Metal(usize),
}

/// A metal panel: its modes (f, Q, radiation weight, m, n), its size, and the panel's own level.
#[derive(Clone, Debug)]
#[allow(dead_code)]
pub struct Panel {
    pub modes: Vec<(f64, f64, f64, f64, f64)>,
    pub a: f64,
    pub b: f64,
}

/// A curved steel panel a x b m with its (1,1) mode at f11 (the curvature and the beading raise it far above the flat
/// plate's), modes f_mn = f11 ((m/a)^2 + (n/b)^2) / ((1/a)^2 + (1/b)^2) up to fmax, loss factor eta(f) falling from
/// the damped lowest modes to `eta`, odd-odd modes radiating best below coincidence.
pub fn panel(a: f64, b: f64, f11: f64, eta: f64, fmax: f64) -> Panel {
    let base = 1.0 / (a * a) + 1.0 / (b * b);
    let mut md = Vec::new();
    for mi in 1..40 {
        for ni in 1..40 {
            let (fm, fnn) = (mi as f64 / a, ni as f64 / b);
            let f = f11 * (fm * fm + fnn * fnn) / base;
            if f > fmax {
                continue;
            }
            let et = eta * (1.0 + f / 3000.0) + 0.10 * m::exp(-f / 200.0);
            let q = 1.0 / et;
            let rad = match (mi % 2, ni % 2) {
                (1, 1) => 1.0,
                (0, 0) => 0.12,
                _ => 0.35,
            } * m::pow(f / 1000.0, 0.4).min(2.0);
            md.push((f, q, rad, mi as f64, ni as f64));
        }
    }
    md.sort_by(|x, y| x.0.partial_cmp(&y.0).unwrap());
    Panel { modes: md, a, b }
}

/// One drop's event at 1 m for a surface. `film`: the film / water depth (mm) on the road; `pitch_leaf`: the leaf modes.
pub(crate) fn drop_event(r: &mut NpRng, tn: &Tune, pref: f64, surf: Surf, d: f64, v: f64, film_mm: f64, panels: &[Panel]) -> Ev {
    let mut ev = Ev::default();
    let frac = r.random();
    match surf {
        Surf::Road | Surf::Puddle => {
            let deep = surf == Surf::Puddle;
            // the impact on a thin film is the rigid surface's; on standing water the surface gives (weaker, longer)
            let pk = if deep { click(&mut ev.dip, d, v, 2.5, frac, 0.35) } else { click(&mut ev.dip, d, v, 1.0, frac, 1.0) };
            let hstar = if deep { 1.0 } else { (film_mm / d).min(1.0) };
            let k = splash_k(d, v);
            let kc = splash_kc(hstar);
            if k > kc {
                let strength = ((k / kc - 1.0) / 3.0).min(1.0);
                let tcap = m::sqrt(RHO_W * m::pow(d * 1e-3, 3.0) / SIGMA_W);
                let fc = r.lognormal(m::ln(4500.0 * m::pow(2.0 / d, 0.3)), 0.3).clamp(1500.0, 12000.0);
                let rms = m::undb(tn.crown_db) * pk * m::sqrt(strength);
                burst(r, &mut ev.iso, 0, 0.0003, 0.25 * tcap, fc, 0.6, rms);
            }
            // bubbles: regular entrainment (0.8-1.1 mm at terminal speed), the quiet middle sizes, irregular big bubbles
            let terminal = (v / terminal_v(d)) > 0.9;
            let p_film = if deep { 1.0 } else { 0.08 };
            if (0.8..1.1).contains(&d) && terminal {
                if r.random() < 0.9 * p_film {
                    let a = r.uniform(0.18e-3, 0.25e-3);
                    let off = (r.uniform(0.001, 0.004) * FS) as usize;
                    bubble(&mut ev.iso, off, a, m::undb(tn.reg_db) * pref * r.uniform(0.5, 1.0));
                }
            } else if d >= 2.2 {
                let p = ((d - 2.0) / 2.0).clamp(0.0, 0.9) * p_film;
                if r.random() < p {
                    let nb = 1 + poisson(r, 0.3 * (d - 2.0));
                    for _ in 0..nb {
                        let a = r.lognormal(m::ln(0.25e-3 * d), 0.5).clamp(0.3e-3, 3.5e-3);
                        let off = (r.uniform(0.005, 0.025) * (d / 3.0) * FS) as usize;
                        bubble(&mut ev.iso, off, a, m::undb(tn.bub_db) * pref * (a / 1e-3) * r.uniform(0.4, 1.0));
                    }
                }
            } else if d >= 1.1 && r.random() < 0.04 * p_film {
                let a = r.lognormal(m::ln(0.5e-3), 0.4).clamp(0.25e-3, 1.5e-3);
                bubble(&mut ev.iso, (r.uniform(0.002, 0.01) * FS) as usize, a, m::undb(tn.bub_db) * pref * (a / 1e-3) * 0.5);
            }
        }
        Surf::Soil => {
            click(&mut ev.dip, d, v, 4.0, frac, m::undb(tn.soil_db));
        }
        Surf::Leaf => {
            // randomly oriented leaves: their sound goes every way
            let pk = click(&mut ev.iso, d, v, 2.0, frac, 0.4);
            let (_, fmax) = force(d, v, 1.0);
            let (_, f2) = force(2.0, terminal_v(2.0), 1.0);
            // modal amplitude ~ the impulse (D^3 V)
            let imp = (d * d * d * v) / (8.0 * terminal_v(2.0));
            let _ = (fmax, f2);
            let nm = 2 + poisson(r, 1.0).min(3);
            let fl = r.lognormal(m::ln(550.0), 0.55);
            let mut md = Vec::with_capacity(nm);
            for j in 0..nm {
                let f = (fl * m::pow(1.0 + j as f64 * r.uniform(0.6, 1.4), 1.0)).clamp(150.0, 3500.0);
                md.push((f, r.uniform(3.0, 10.0), m::undb(tn.leaf_db) * pref * imp * r.uniform(0.4, 1.0)));
            }
            modes(&mut ev.iso, 0, &md);
            let k = splash_k(d, v);
            if k > 2100.0 {
                let tcap = m::sqrt(RHO_W * m::pow(d * 1e-3, 3.0) / SIGMA_W);
                let fc = r.lognormal(m::ln(5000.0), 0.3).clamp(2000.0, 12000.0);
                burst(r, &mut ev.iso, 0, 0.0002, 0.15 * tcap, fc, 0.7, m::undb(tn.crown_db) * pk * 0.7);
            }
        }
        Surf::Metal(pi) => {
            let p = &panels[pi.min(panels.len() - 1)];
            let pk = click(&mut ev.iso, d, v, 1.0, frac, 0.3);
            let (tp, _) = force(d, v, 1.0);
            let imp = (d * d * d * v) / (8.0 * terminal_v(2.0));
            let (x, y) = (r.random(), r.random());
            let md: Vec<(f64, f64, f64)> = p
                .modes
                .iter()
                .map(|&(f, q, rad, mi, ni)| {
                    let shape = m::sin(m::PI * mi * x) * m::sin(m::PI * ni * y);
                    // the force pulse's own spectrum rolls off the modes above 1 / (2 pi tp)
                    let wt = m::TAU * f * tp;
                    let fs = 1.0 / (1.0 + wt * wt);
                    (f, q, m::undb(tn.metal_db) * pref * imp * shape * rad * fs)
                })
                .collect();
            // the 24 modes carrying the most energy (a^2 / decay rate ~ a^2 Q / f); the rest are 20+ dB under
            let mut idx: Vec<usize> = (0..md.len()).collect();
            let en = |i: usize| md[i].2 * md[i].2 * md[i].1 / md[i].0;
            idx.sort_by(|&i, &j| en(j).partial_cmp(&en(i)).unwrap().then(i.cmp(&j)));
            let md: Vec<(f64, f64, f64)> = idx.iter().take(24).map(|&i| md[i]).collect();
            modes(&mut ev.iso, 0, &md);
            let k = splash_k(d, v);
            if k > splash_kc(0.2) {
                let tcap = m::sqrt(RHO_W * m::pow(d * 1e-3, 3.0) / SIGMA_W);
                burst(r, &mut ev.iso, 0, 0.0003, 0.2 * tcap, 5000.0, 0.6, m::undb(tn.crown_db) * pk);
            }
        }
    }
    ev
}

// ------------------------------------------------------------------ scenes

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Mode {
    /// the open road
    Open,
    /// next to the car (hood / orbit camera): its panels within 0.8-2 m
    NearCar,
    /// inside the car (driver camera)
    Cabin,
    /// under an open shelter (a bridge deck, a porch): a strip overhead without rain
    Shelter,
}

#[derive(Clone, Copy, Debug)]
pub struct Scene {
    pub name: &'static str,
    pub rate: f64,
    /// ground shares (the road takes the rest)
    pub puddle: f64,
    pub soil: f64,
    /// canopy over this share of the ground (drops there hit leaves; drips fall under it)
    pub canopy: f64,
    /// the road's water film (mm)
    pub film: f64,
    /// parked cars and signs within 25 m
    pub objects: usize,
    /// gutters / downpipes running (relative)
    pub runoff: f64,
    pub gust: f64,
    pub lufs: f64,
    pub mode: Mode,
}

pub fn scene(kind: &str) -> Option<Scene> {
    let open = |name, rate, lufs, puddle, film, runoff, gust| Scene {
        name,
        rate,
        puddle,
        soil: 0.15,
        canopy: 0.15,
        film,
        objects: 6,
        runoff,
        gust,
        lufs,
        mode: Mode::Open,
    };
    Some(match kind {
        "light" => open("light", 0.7, -30.2, 0.04, 0.2, 0.0, 0.3),
        "medium" => open("medium", 5.0, -24.2, 0.10, 0.5, 0.2, 0.4),
        "heavy" => open("heavy", 35.0, -17.2, 0.30, 1.5, 1.0, 0.45),
        "roof" => Scene { mode: Mode::NearCar, ..open("roof", 5.0, -24.2, 0.10, 0.5, 0.2, 0.4) },
        "cabin_light" => Scene { mode: Mode::Cabin, ..open("cabin_light", 0.7, -30.0, 0.04, 0.2, 0.0, 0.3) },
        "cabin" => Scene { mode: Mode::Cabin, ..open("cabin", 5.0, -24.1, 0.10, 0.5, 0.2, 0.4) },
        "cabin_heavy" => Scene { mode: Mode::Cabin, ..open("cabin_heavy", 35.0, -17.2, 0.30, 1.5, 1.0, 0.45) },
        "muffled" => Scene { mode: Mode::Shelter, ..open("muffled", 5.0, -25.9, 0.10, 0.5, 0.2, 0.4) },
        // the mod's file set: the roof next to the car at the other rates, and an enclosed shelter (a tunnel, a closed
        // bridge, a building: the medium rain through the walls, two 900 Hz low-passes as the mod's muffled A)
        "roof_light" => Scene { mode: Mode::NearCar, ..open("roof_light", 0.7, -30.0, 0.04, 0.2, 0.0, 0.3) },
        "roof_heavy" => Scene { mode: Mode::NearCar, ..open("roof_heavy", 35.0, -17.2, 0.30, 1.5, 1.0, 0.45) },
        "muffled_closed" => Scene { name: "medium", lufs: -25.9, ..open("medium", 5.0, -25.9, 0.10, 0.5, 0.2, 0.4) },
        _ => return None,
    })
}

/// The drop rate's swings, as sines with whole periods over the loop (seamless), tabulated every 1 ms:
/// - gusts: 1 +/- `depth` at most over seconds (the first 8 harmonics);
/// - clustering: rain is patchy at metre scales (not a Poisson field), so the rate also flickers at 0.5-8 Hz: a
///   log-normal factor, standard deviation `fast` in ln rate, 1/sqrt(f) amplitudes, its own stream per area.
struct Gust {
    tab: Vec<f64>,
    peak: f64,
    mean: f64,
    secs: f64,
}

impl Gust {
    fn new(seed: u64, depth: f64, secs: f64) -> Gust {
        Gust::with_fast(seed, "", depth, 0.0, secs)
    }
    fn with_fast(seed: u64, stream: &str, depth: f64, fast: f64, secs: f64) -> Gust {
        let mut r = rng(seed, "rainb-gust");
        let mut slow = Vec::new();
        let mut tot = 0.0;
        for k in 1..=8 {
            let a = r.uniform(0.3, 1.0) / m::sqrt(k as f64);
            slow.push((k as f64, a, r.uniform(0.0, m::TAU)));
            tot += a;
        }
        for p in slow.iter_mut() {
            p.1 *= depth / tot;
        }
        let mut quick = Vec::new();
        if fast > 0.0 {
            let mut rf = rng(seed, &format!("rainb-cluster-{stream}"));
            let (k0, k1) = ((0.5 * secs).round() as usize, (8.0 * secs).round() as usize);
            let mut var = 0.0;
            for k in k0.max(1)..=k1 {
                let a = 1.0 / m::sqrt(k as f64) * rf.uniform(0.5, 1.5);
                quick.push((k as f64, a, rf.uniform(0.0, m::TAU)));
                var += 0.5 * a * a;
            }
            let g = fast / m::sqrt(var);
            for p in quick.iter_mut() {
                p.1 *= g;
            }
        }
        let n = (secs * 1000.0).round() as usize;
        let tab: Vec<f64> = (0..n)
            .map(|i| {
                let t = secs * i as f64 / n as f64;
                let s: f64 = slow.iter().map(|&(k, a, ph)| a * m::sin(m::TAU * k * t / secs + ph)).sum();
                let q: f64 = quick.iter().map(|&(k, a, ph)| a * m::sin(m::TAU * k * t / secs + ph)).sum();
                (1.0 + s) * m::exp(q - 0.5 * fast * fast)
            })
            .collect();
        let peak = tab.iter().cloned().fold(0.0, f64::max);
        let mean = tab.iter().sum::<f64>() / n as f64;
        Gust { tab, peak, mean, secs }
    }
    fn rate(&self, t: f64) -> f64 {
        let n = self.tab.len();
        let x = (t / self.secs).rem_euclid(1.0) * n as f64;
        let i = (x.floor() as usize).min(n - 1);
        let fr = x - i as f64;
        self.tab[i] * (1.0 - fr) + self.tab[(i + 1) % n] * fr
    }
}

/// The circular loop buffer and the per-event placement (distance gain, directivity, low-pass).
struct Loop {
    /// events heard open (no low-pass)
    buf: Vec<f64>,
    /// events whose distance low-pass falls in a 1/3-octave bin (BUCKET_F0 x 2^(k/3)): each bin low-passed once at
    /// the end (one-pole, settled round the loop), not each event
    buckets: Vec<Option<Vec<f64>>>,
    /// an extra image of every event (the shelter's ceiling): (height of the reflecting plane m, gain)
    ceiling: Option<(f64, f64)>,
    air: AirTable,
}

/// Air-absorption cutoff (ISO 9613-1, the 6 dB point) by path length, tabulated on a log grid 0.25-1600 m.
pub(crate) struct AirTable {
    tab: Vec<f64>,
}

impl AirTable {
    pub(crate) fn new() -> AirTable {
        AirTable { tab: (0..512).map(|i| cutoff_for_path(0.25 * m::pow(6400.0, i as f64 / 511.0))).collect() }
    }
    pub(crate) fn fc(&self, r: f64) -> f64 {
        let x = (m::ln(r.clamp(0.25, 1600.0) / 0.25) / m::ln(6400.0)) * 511.0;
        let i = (x.floor() as usize).min(510);
        let fr = x - i as f64;
        self.tab[i] * (1.0 - fr) + self.tab[i + 1] * fr
    }
}

/// The 1/3-octave low-pass bin of a cutoff (BUCKETS: open).
pub(crate) fn bucket_of(fc: f64) -> usize {
    (m::round(3.0 * m::log2(fc / BUCKET_F0)).max(0.0) as usize).min(BUCKETS)
}

pub(crate) const BUCKET_F0: f64 = 400.0;
pub(crate) const BUCKETS: usize = 16; // 400 Hz .. 12.7 kHz; above: open

impl Loop {
    fn new(n: usize) -> Loop {
        let air = AirTable::new();
        Loop { buf: vec![0.0; n], buckets: (0..BUCKETS).map(|_| None).collect(), ceiling: None, air }
    }
    fn air_fc(&self, r: f64) -> f64 {
        self.air.fc(r)
    }
    /// Place an event: horizontal distance rho, source height zs above the ground, ground-level (scattered by kerbs,
    /// cars and plants along the way) or raised, at time t (s) with an extra gain.
    fn place(&mut self, ev: &Ev, iso_share: f64, rho: f64, zs: f64, ground: bool, t: f64, gain: f64) {
        let dz = OBS_H - zs;
        let r = m::sqrt(rho * rho + dz * dz).max(0.3);
        let cos_t = (dz.abs() / r).min(1.0);
        let gd = (iso_share + (1.0 - iso_share) * cos_t) / r;
        let gi = 1.0 / r;
        let mut fc = self.air_fc(r);
        if ground && rho > 4.0 {
            fc = fc.min((14000.0 * m::pow(4.0 / rho, 0.6)).clamp(1200.0, 16000.0));
        }
        let delay = r / C_AIR;
        self.put(ev, gd * gain, gi * gain, fc, t + delay);
        if let Some((h, g)) = self.ceiling {
            // the image of the source in the ceiling plane at height h
            let dzi = 2.0 * h - zs - OBS_H;
            let ri = m::sqrt(rho * rho + dzi * dzi);
            let ci = (dzi.abs() / ri).min(1.0);
            let gdi = (iso_share + (1.0 - iso_share) * ci) / ri;
            let fci = self.air_fc(ri).min(9000.0);
            self.put(ev, gdi * gain * g, gain * g / ri, fci, t + ri / C_AIR);
        }
    }
    fn put(&mut self, ev: &Ev, gd: f64, gi: f64, fc: f64, t: f64) {
        let n = self.buf.len();
        let k = bucket_of(fc);
        let dst: &mut Vec<f64> = if k >= BUCKETS {
            &mut self.buf
        } else {
            self.buckets[k].get_or_insert_with(|| vec![0.0; n])
        };
        let mut j = ((t * FS) as usize) % n;
        for i in 0..ev.dip.len().max(ev.iso.len()) {
            dst[j] += gd * ev.dip.get(i).copied().unwrap_or(0.0) + gi * ev.iso.get(i).copied().unwrap_or(0.0);
            j += 1;
            if j == n {
                j = 0;
            }
        }
    }
    /// The open events plus every bin through its low-pass (settled round the loop).
    fn mix(mut self) -> Vec<f64> {
        let n = self.buf.len();
        for (k, b) in self.buckets.into_iter().enumerate() {
            let Some(b) = b else { continue };
            let a = crate::dsp::onepole_coef(FS, BUCKET_F0 * m::pow(2.0, k as f64 / 3.0));
            let mut s = 0.0;
            for pass in 0..2 {
                for i in 0..n {
                    s += a * (b[i] - s);
                    if pass == 1 {
                        self.buf[i] += s;
                    }
                }
            }
        }
        self.buf
    }
}

pub struct Stats {
    /// impacts per second per m^2 (D >= 0.4 mm)
    pub flux: f64,
    /// rendered events per second
    pub rendered: f64,
    /// drops of >= 2.2 mm per second within 4 m
    pub big_near: f64,
}

/// The outdoor drop field of a scene round a circular buffer of `secs` s (not levelled). `exclude`: the shelter's
/// half-width (m): no drops on the strip |y| < exclude.
fn field(seed: u64, sc: &Scene, tn: &Tune, secs: f64, lp: &mut Loop, panels: &[Panel], exclude: f64) -> Stats {
    let dsd = Dsd::new(sc.rate);
    let near_g = Gust::with_fast(seed, "near", sc.gust, tn.fast_near, secs);
    let far_g = Gust::with_fast(seed, "far", sc.gust, tn.fast_far, secs);
    let pref = p_ref();
    let mut rendered = 0usize;
    let mut big_near = 0.0;
    for (ri, &(r0, r1)) in RINGS.iter().enumerate() {
        let area = m::PI * (r1 * r1 - r0 * r0);
        for (bi, &(d0, d1)) in BANDS.iter().enumerate() {
            let rate = dsd.flux(d0, d1) * area;
            if rate <= 0.0 {
                continue;
            }
            if d0 >= 2.2 && r1 <= 4.0 {
                big_near += rate;
            }
            let mut r = rng(seed, &format!("rainb-{}-{ri}-{bi}", sc.name));
            let gust = if r0 < 4.0 { &near_g } else { &far_g };
            let cap = if r0 >= 25.0 { tn.cap * 0.25 } else if r0 >= 10.0 { tn.cap * 0.5 } else { tn.cap };
            let k = (rate / cap).max(1.0);
            let g = m::sqrt(k);
            // draws at the gusts' peak rate, kept with probability rate(t) / peak
            let count = poisson(&mut r, rate / k * secs * gust.peak / gust.mean);
            for _ in 0..count {
                let t = r.random() * secs;
                let keep = r.random() * gust.peak < gust.rate(t);
                let rho = m::sqrt(r0 * r0 + r.random() * (r1 * r1 - r0 * r0));
                let phi = r.random() * m::TAU;
                if !keep {
                    continue;
                }
                if exclude > 0.0 && (rho * m::sin(phi)).abs() < exclude {
                    continue;
                }
                let d = dsd.sample(&mut r, d0, d1);
                let v = terminal_v(d);
                let u = r.random();
                // canopy first (the drop hits leaves 4-12 m up), then the ground surfaces
                let (surf, zs, ground) = if u < sc.canopy && rho > 2.0 {
                    (Surf::Leaf, r.uniform(4.0, 12.0), false)
                } else {
                    let w = r.random();
                    if !panels.is_empty() && sc.objects > 0 && w < 0.04 && rho > 3.0 && rho < 25.0 {
                        (Surf::Metal(r.integers(0, panels.len() as i64) as usize), 1.0, false)
                    } else if w < 0.04 + sc.puddle {
                        (Surf::Puddle, 0.0, true)
                    } else if w < 0.04 + sc.puddle + sc.soil {
                        (Surf::Soil, 0.0, true)
                    } else {
                        (Surf::Road, 0.0, true)
                    }
                };
                let ev = drop_event(&mut r, tn, pref, surf, d, v, sc.film, panels);
                lp.place(&ev, tn.iso, rho, zs, ground, t, g);
                rendered += 1;
            }
        }
    }
    // canopy drips: the intercepted rain falls again as big drips (4-5.5 mm) from 2-10 m, under the canopy
    if sc.canopy > 0.0 {
        let mut r = rng(seed, &format!("rainb-drips-{}", sc.name));
        let vol_flux = sc.rate / 3.6e6; // m/s
        for &(r0, r1) in RINGS.iter().take(5) {
            let area = m::PI * (r1 * r1 - r0 * r0) * sc.canopy;
            let dvol = m::PI / 6.0 * m::pow(4.8e-3, 3.0);
            let rate = vol_flux * area * tn.drip_frac / dvol;
            let k = (rate / tn.cap).max(1.0);
            let count = poisson(&mut r, rate / k * secs);
            for _ in 0..count {
                let t = r.random() * secs;
                let rho = m::sqrt(r0 * r0 + r.random() * (r1 * r1 - r0 * r0)).max(1.0);
                let phi = r.random() * m::TAU;
                if exclude > 0.0 && (rho * m::sin(phi)).abs() < exclude {
                    continue;
                }
                let d = r.uniform(4.0, 5.5);
                let v = fall_speed(d, r.uniform(2.0, 10.0));
                let w = r.random();
                let surf = if w < 0.3 { Surf::Soil } else if w < 0.3 + sc.puddle + 0.1 { Surf::Puddle } else { Surf::Road };
                let ev = drop_event(&mut r, tn, pref, surf, d, v, sc.film, panels);
                lp.place(&ev, tn.iso, rho, 0.0, true, t, m::sqrt(k) * m::undb(tn.drip_db));
            }
        }
    }
    // gutters and downpipes: streams of bubbles (0.5-6 mm, ~200 a second each), babbling, 4-10 m away
    if sc.runoff > 0.0 {
        let mut r = rng(seed, &format!("rainb-runoff-{}", sc.name));
        for s in 0..2 {
            let bab = Gust::new(seed.wrapping_add(17 + s), 0.7, secs);
            let rho = r.uniform(4.0, 10.0);
            let count = poisson(&mut r, 200.0 * sc.runoff * secs);
            for _ in 0..count {
                let t = r.random() * secs;
                if r.random() * bab.peak >= bab.rate(t) {
                    continue;
                }
                let a = r.lognormal(m::ln(1.8e-3), 0.55).clamp(0.5e-3, 6e-3);
                let mut ev = Ev::default();
                bubble(&mut ev.iso, 0, a, 0.05 * pref * m::sqrt(a / 1e-3) * r.lognormal(0.0, 0.6));
                lp.place(&ev, 1.0, rho, 0.3, true, t, 1.0);
            }
        }
    }
    Stats { flux: dsd.flux(D_MIN, 6.5), rendered: rendered as f64 / secs, big_near }
}

/// Parked cars and signs: a few panels with their own modes.
pub(crate) fn object_panels(seed: u64, count: usize) -> Vec<Panel> {
    let mut r = rng(seed, "rainb-panels");
    (0..count)
        .map(|_| {
            let a = r.uniform(0.8, 1.6);
            let b = r.uniform(0.6, 1.5);
            panel(a, b, r.uniform(40.0, 90.0), r.uniform(0.01, 0.04), 6000.0)
        })
        .collect()
}

/// The car's roof seen from inside: every hit on the 1.3 x 1.45 m panel drives its modes (6 zones, each with its own
/// mode-shape weights: the panel is linear, so each zone's force signal goes through the modal bank once); the
/// headliner's low-pass; the windscreen's ticks; the outside through the body (`outside`, the same rain outdoors).
fn cabin(seed: u64, sc: &Scene, tn: &Tune, n: usize, outside: &[f64]) -> Vec<f64> {
    let dsd = Dsd::new(sc.rate);
    let gust = Gust::with_fast(seed, "near", sc.gust, tn.fast_cabin, n as f64 / FS);
    let secs = n as f64 / FS;
    let roof = panel(1.3, 1.45, 62.0, 0.03, 3000.0);
    let zones = [(0.25, 0.25), (0.5, 0.3), (0.75, 0.25), (0.25, 0.7), (0.5, 0.65), (0.75, 0.75)];
    let mut fz = vec![vec![0.0f64; n]; zones.len()];
    let mut hf = vec![0.0f64; n];
    let mut r = rng(seed, &format!("rainb-roof-{}", sc.name));
    let area = 1.3 * 1.45;
    for &(d0, d1) in BANDS.iter() {
        let rate = dsd.flux(d0, d1) * area;
        let count = poisson(&mut r, rate * secs * gust.peak / gust.mean);
        for _ in 0..count {
            let t = r.random() * secs;
            if r.random() * gust.peak >= gust.rate(t) {
                continue;
            }
            let d = dsd.sample(&mut r, d0, d1);
            let v = terminal_v(d);
            let (tp, fmax) = force(d, v, 1.0);
            let z = r.integers(0, zones.len() as i64) as usize;
            // the force pulse itself, sampled (it drives the panel)
            let len = (22.0 * tp * FS).ceil() as usize + 2;
            let at = (t * FS) as usize;
            let frac = r.random();
            let mut prev = 0.0;
            for i in 0..len {
                let tt = (i as f64 - frac) / FS;
                if tt <= 0.0 {
                    continue;
                }
                let f = force_at(tt, tp, fmax);
                fz[z][(at + i) % n] += f;
                // the hit's own click, radiated by the panel round it (the near field the modes leave out)
                hf[(at + i) % n] += f - prev;
                prev = f;
            }
        }
    }
    Biquad::highpass(FS, 1500.0, 0.7071).run_circular(&mut hf);
    Biquad::highpass(FS, 1500.0, 0.7071).run_circular(&mut hf);
    // modal bank per zone (resonators run round the loop twice: settled, seamless)
    let mut roof_out = vec![0.0f64; n];
    for (zi, &(zx, zy)) in zones.iter().enumerate() {
        for &(f, q, rad, mi, ni) in roof.modes.iter() {
            let shape = m::sin(m::PI * mi * zx) * m::sin(m::PI * ni * zy);
            if shape.abs() < 1e-3 {
                continue;
            }
            let w = m::TAU * f / FS;
            let rr = m::exp(-m::PI * f / q / FS);
            let c1 = 2.0 * rr * m::cos(w);
            let c2 = rr * rr;
            // the panel's acceleration ~ w^2 x displacement near resonance; radiation weight rad
            let g = shape * rad * m::sin(w);
            let (mut y1, mut y2) = (0.0, 0.0);
            for pass in 0..2 {
                for i in 0..n {
                    let y = c1 * y1 - c2 * y2 + fz[zi][i];
                    y2 = y1;
                    y1 = y;
                    if pass == 1 {
                        roof_out[i] += g * y;
                    }
                }
            }
        }
    }
    let mut liner = Biquad::lowpass(FS, tn.liner_hz, 0.6);
    liner.run_circular(&mut roof_out);
    Biquad::highpass(FS, 35.0, 0.7071).run_circular(&mut roof_out);
    // the closed cabin below its first air mode (~70-90 Hz): a pressure chamber, the low end comes up
    Biquad::lowshelf(FS, 150.0, tn.cabin_lf_db).run_circular(&mut roof_out);
    // the windscreen (1.2 m^2, ~0.9 m away): laminated glass, well damped: bright ticks
    let mut glass = vec![0.0f64; n];
    let mut rg = rng(seed, &format!("rainb-glass-{}", sc.name));
    for &(d0, d1) in BANDS.iter() {
        let rate = dsd.flux(d0, d1) * 1.2;
        let count = poisson(&mut rg, rate * secs * gust.peak / gust.mean);
        for _ in 0..count {
            let t = rg.random() * secs;
            if rg.random() * gust.peak >= gust.rate(t) {
                continue;
            }
            let d = dsd.sample(&mut rg, d0, d1);
            let v = terminal_v(d) * 0.8; // the screen's slope: the normal component
            let mut ev = Vec::new();
            click(&mut ev, d, v, 1.0, rg.random(), 1.0);
            let at = (t * FS) as usize;
            for (i, &x) in ev.iter().enumerate() {
                glass[(at + i) % n] += x / 0.9;
            }
        }
    }
    Biquad::bandpass(FS, 2500.0, 0.6).run_circular(&mut glass);
    let lv = |x: &[f64]| -> f64 { m::sqrt(x.iter().map(|v| v * v).sum::<f64>() / x.len().max(1) as f64).max(1e-30) };
    // the outside through the body: a mass-law wall (two low-passes), level relative to the roof
    let mut body: Vec<f64> = outside.to_vec();
    Biquad::lowpass(FS, 350.0, 0.7071).run_circular(&mut body);
    Biquad::lowpass(FS, 700.0, 0.7071).run_circular(&mut body);
    let lr = lv(&roof_out);
    let gg = m::undb(tn.glass_db) * lr / lv(&glass);
    let gh = m::undb(tn.roofhf_db) * lr / lv(&hf);
    let gb = m::undb(tn.body_db) * lr / lv(&body);
    (0..n).map(|i| roof_out[i] + gg * glass[i] + gh * hf[i] + gb * body[i]).collect()
}

/// Render a scene's loop (`secs` s), levelled to its loudness with a true-peak ceiling. (samples, info)
pub fn render(kind: &str, seed: u64, secs: f64) -> Result<(Vec<f64>, String), String> {
    let sc = scene(kind).ok_or(format!("unknown rainb kind {kind}"))?;
    let tn = Tune::get();
    let n = (secs * FS) as usize;
    let mut lp = Loop::new(n);
    let mut panels = object_panels(seed, sc.objects);
    let mut exclude = 0.0;
    if sc.mode == Mode::Shelter {
        // a 12 m deck 5 m up: no drops on the strip under it, its underside reflecting
        exclude = 6.0;
        lp.ceiling = Some((5.0, 0.55));
    }
    let st = field(seed, &sc, &tn, secs, &mut lp, &panels, exclude);
    if sc.mode == Mode::Shelter {
        // the deck's edges drip (4-5.5 mm drops onto the ground 6 m to either side)
        let mut r = rng(seed, "rainb-edge");
        let pref = p_ref();
        let rate = 1.5 * sc.rate * 30.0; // drips per s along 30 m of both edges
        let count = poisson(&mut r, rate * secs);
        for _ in 0..count {
            let t = r.random() * secs;
            let x = r.uniform(-15.0, 15.0);
            let y = if r.random() < 0.5 { 6.0 } else { -6.0 };
            let d = r.uniform(4.0, 5.5);
            let v = fall_speed(d, 4.5);
            let surf = if r.random() < 0.4 { Surf::Puddle } else { Surf::Road };
            let ev = drop_event(&mut r, &tn, pref, surf, d, v, sc.film, &panels);
            lp.place(&ev, tn.iso, m::sqrt(x * x + y * y), 0.0, true, t, 0.7);
        }
    }
    if sc.mode == Mode::NearCar {
        // the car's own panels next to the camera: roof, bonnet, boot lid, glass (as a stiff, damped panel)
        let car = vec![
            panel(1.3, 1.45, 62.0, 0.03, 6000.0),
            panel(1.4, 1.1, 75.0, 0.03, 6000.0),
            panel(1.2, 0.8, 90.0, 0.03, 6000.0),
            panel(1.4, 0.9, 140.0, 0.12, 6000.0),
        ];
        let base = panels.len();
        panels.extend(car);
        let dsd = Dsd::new(sc.rate);
        let gust = Gust::new(seed, sc.gust, secs);
        let pref = p_ref();
        // the car's own panels at their own level
        let tc = Tune { metal_db: tn.car_db, ..tn };
        let parts = [(base, 1.9, 0.9), (base + 1, 1.6, 1.3), (base + 2, 1.0, 2.4), (base + 3, 2.2, 1.1)];
        for (pi, area, dist) in parts {
            let mut r = rng(seed, &format!("rainb-car-{pi}"));
            for &(d0, d1) in BANDS.iter() {
                let rate = dsd.flux(d0, d1) * area;
                let k = (rate / tn.cap).max(1.0);
                let count = poisson(&mut r, rate / k * secs * gust.peak / gust.mean);
                for _ in 0..count {
                    let t = r.random() * secs;
                    if r.random() * gust.peak >= gust.rate(t) {
                        continue;
                    }
                    let d = dsd.sample(&mut r, d0, d1);
                    let ev = drop_event(&mut r, &tc, pref, Surf::Metal(pi), d, terminal_v(d), sc.film, &panels);
                    lp.place(&ev, 1.0, dist, OBS_H - 0.6, false, t, m::sqrt(k));
                }
            }
        }
    }
    let mut x = lp.mix();
    if sc.mode == Mode::Cabin {
        x = cabin(seed, &sc, &tn, n, &x);
    }
    if kind == "muffled_closed" {
        for _ in 0..2 {
            Biquad::lowpass(FS, 900.0, std::f64::consts::FRAC_1_SQRT_2).run_circular(&mut x);
        }
    }
    let lufs = finish(&mut x, sc.lufs);
    let tp = loudness::true_peak_db(&x, true);
    let dsd = Dsd::new(sc.rate);
    Ok((
        x,
        format!(
            "kind={kind} seed={seed} rate_mm_h={} d0_mm={:.2} dmax_mm={:.1} impacts_per_m2_s={:.0} rendered_per_s={:.0} big_drops_4m_per_s={:.1} lufs={lufs:.2} tp_dbtp={tp:.2}",
            sc.rate, dsd.d0, dsd.dmax, st.flux, st.rendered, st.big_near
        ),
    ))
}

/// A 30 Hz high-pass round the loop, the class's loudness (measured over the loop played twice) and, while the true
/// peak is over CEIL_DB, a circular look-ahead limiter at LIM_DB and the level again. Returns the loudness.
fn finish(x: &mut [f64], target: f64) -> f64 {
    Biquad::highpass(FS, 30.0, std::f64::consts::FRAC_1_SQRT_2).run_circular(x);
    for _ in 0..4 {
        let l = loudness::integrated(x, FS, true);
        let g = m::undb(target - l);
        for v in x.iter_mut() {
            *v *= g;
        }
        if loudness::true_peak_db(x, true) <= CEIL_DB {
            break;
        }
        crate::texmix::limit_loop(x, LIM_DB);
    }
    loudness::integrated(x, FS, true)
}

/// The physical (unlevelled) loudness of the open-road scene at a rate, LUFS re an arbitrary reference: how much
/// louder heavy rain is than light by the model alone.
pub fn physical_lufs(rate: f64, seed: u64, secs: f64) -> f64 {
    let mut sc = scene("medium").unwrap();
    sc.rate = rate;
    sc.name = "phys";
    let tn = Tune::get();
    let n = (secs * FS) as usize;
    let mut lp = Loop::new(n);
    let panels = object_panels(seed, sc.objects);
    field(seed, &sc, &tn, secs, &mut lp, &panels, 0.0);
    let mut x = lp.mix();
    Biquad::highpass(FS, 30.0, std::f64::consts::FRAC_1_SQRT_2).run_circular(&mut x);
    // 94 dB SPL = 1 Pa: report as a level re 20 uPa (LUFS of the pressure in Pa + 94 + ~0.7)
    loudness::integrated(&x, FS, true)
}

pub fn stats() -> String {
    let mut s = String::from(
        "rate_mm_h\tD0_mm\tDmax_mm\tLambda_per_mm\tN_per_m3(>=0.4)\timpacts_per_m2_s(>=0.4)\t0.8-1.1\t1.1-2.2\t>=2.2\t>=3.0\tsplash_Dmin_film_mm\n",
    );
    for &r in &[0.3, 0.7, 1.0, 2.0, 5.0, 10.0, 20.0, 35.0, 60.0] {
        let d = Dsd::new(r);
        // the smallest drop that splashes on a 0.5 mm film at terminal speed
        let mut dm = 0.4;
        while dm < 6.0 && splash_k(dm, terminal_v(dm)) <= splash_kc((0.5 / dm).min(1.0)) {
            dm += 0.01;
        }
        s += &format!(
            "{r}\t{:.2}\t{:.1}\t{:.2}\t{:.0}\t{:.0}\t{:.0}\t{:.0}\t{:.1}\t{:.2}\t{:.2}\n",
            d.d0,
            d.dmax,
            d.lam,
            d.concentration(D_MIN, 6.5),
            d.flux(D_MIN, 6.5),
            d.flux(0.8, 1.1),
            d.flux(1.1, 2.2),
            d.flux(2.2, 6.5),
            d.flux(3.0, 6.5),
            dm
        );
    }
    s
}

fn arg(args: &[String], key: &str) -> Option<String> {
    args.iter().position(|a| a == key).and_then(|i| args.get(i + 1).cloned())
}

pub const KINDS: [&str; 11] =
    ["light", "medium", "heavy", "roof", "cabin_light", "cabin", "cabin_heavy", "muffled", "roof_light", "roof_heavy", "muffled_closed"];

/// The mod's Generated rain files (ambience.lua RAIN_LAYERS, RAIN_ROOF_LAYERS, RAIN_CABIN_LAYERS, RAIN_MUFFLED,
/// RAIN_MUFFLED_OPEN): (file stem, kind).
pub const MOD_FILES: [(&str, &str); 11] = [
    ("bne_rain_light", "light"),
    ("bne_rain_medium", "medium"),
    ("bne_rain_heavy", "heavy"),
    ("bne_rain_roof_light", "roof_light"),
    ("bne_rain_roof", "roof"),
    ("bne_rain_roof_heavy", "roof_heavy"),
    ("bne_rain_roof_cabin_light", "cabin_light"),
    ("bne_rain_roof_cabin", "cabin"),
    ("bne_rain_roof_cabin_heavy", "cabin_heavy"),
    ("bne_rain_muffled", "muffled_closed"),
    ("bne_rain_muffled_open", "muffled"),
];

/// weathersynth rainb-modset --out DIR [--seed N] [--secs S]: the mod's Generated rain loops (Vorbis q4, as the
/// recorded set) and a table of their loudness, true peak and size for SOURCES.md.
pub fn run_modset(args: &[String]) -> Result<(), String> {
    let dir = arg(args, "--out").ok_or("--out DIR")?;
    let seed: u64 = arg(args, "--seed").and_then(|v| v.parse().ok()).unwrap_or(1);
    let secs: f64 = arg(args, "--secs").and_then(|v| v.parse().ok()).unwrap_or(30.0);
    std::fs::create_dir_all(format!("{dir}/wav")).map_err(|e| e.to_string())?;
    println!("file\tkind\tLUFS\ttrue peak dBTP\tbytes");
    for (stem, kind) in MOD_FILES {
        let (x, _) = render(kind, seed, secs)?;
        crate::wav::write_f32(&format!("{dir}/wav/{stem}.wav"), &x, 44100).map_err(|e| e.to_string())?;
        let path = format!("{dir}/{stem}.ogg");
        write_ogg_q(&path, &x, "4")?;
        let bytes = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
        println!(
            "{stem}.ogg\t{kind}\t{:.2}\t{:.2}\t{bytes}",
            loudness::integrated(&x, FS, true),
            loudness::true_peak_db(&x, true)
        );
    }
    Ok(())
}

pub fn run_one(args: &[String]) -> Result<(), String> {
    let kind = arg(args, "--kind").ok_or("--kind K")?;
    let seed: u64 = arg(args, "--seed").and_then(|v| v.parse().ok()).unwrap_or(1);
    let secs: f64 = arg(args, "--secs").and_then(|v| v.parse().ok()).unwrap_or(30.0);
    let out = arg(args, "--out").ok_or("--out F.wav")?;
    let t0 = std::time::Instant::now();
    let (x, info) = render(&kind, seed, secs)?;
    crate::wav::write_f32(&out, &x, 44100).map_err(|e| e.to_string())?;
    println!("{info} render_s={:.2}", t0.elapsed().as_secs_f64());
    Ok(())
}

pub fn run_set(args: &[String]) -> Result<(), String> {
    let dir = arg(args, "--out").ok_or("--out DIR")?;
    let seed: u64 = arg(args, "--seed").and_then(|v| v.parse().ok()).unwrap_or(1);
    let secs: f64 = arg(args, "--secs").and_then(|v| v.parse().ok()).unwrap_or(30.0);
    let only: Option<Vec<String>> = arg(args, "--only").map(|v| v.split(',').map(|s| s.trim().to_string()).collect());
    let ogg = args.iter().any(|a| a == "--ogg");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    for kind in KINDS {
        if let Some(o) = &only {
            if !o.iter().any(|n| n == kind) {
                continue;
            }
        }
        let t0 = std::time::Instant::now();
        let (x, info) = render(kind, seed, secs)?;
        let stem = format!("{dir}/bne_rainb_{kind}");
        crate::wav::write_f32(&format!("{stem}.wav"), &x, 44100).map_err(|e| e.to_string())?;
        if ogg {
            write_ogg(&format!("{stem}.ogg"), &x)?;
        }
        println!("{info} render_s={:.2}", t0.elapsed().as_secs_f64());
    }
    Ok(())
}

fn write_ogg(path: &str, x: &[f64]) -> Result<(), String> {
    write_ogg_q(path, x, "6")
}

fn write_ogg_q(path: &str, x: &[f64], q: &str) -> Result<(), String> {
    use std::io::Write;
    use std::process::{Command, Stdio};
    let raw: Vec<u8> = x.iter().flat_map(|v| (*v as f32).to_le_bytes()).collect();
    let mut ch = Command::new("ffmpeg")
        .args(["-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", "44100", "-ac", "1", "-i", "-"])
        .args(["-c:a", "libvorbis", "-q:a", q, "-map_metadata", "-1", "-fflags", "+bitexact"])
        .arg(path)
        .stdin(Stdio::piped())
        .spawn()
        .map_err(|e| format!("ffmpeg: {e}"))?;
    ch.stdin.take().ok_or("ffmpeg stdin")?.write_all(&raw).map_err(|e| format!("ffmpeg: {e}"))?;
    if !ch.wait().map_err(|e| format!("ffmpeg: {e}"))?.success() {
        return Err(format!("ffmpeg could not write {path}"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The distribution carries the rain rate it was made for.
    #[test]
    fn dsd_carries_its_rate() {
        for &r in &[0.7, 5.0, 35.0] {
            let d = Dsd::new(r);
            let mut acc = 0.0;
            let mut dd = 0.05;
            while dd <= 6.5 {
                let taper = if dd > d.dmax { m::exp(-(dd - d.dmax) / 0.1) } else { 1.0 };
                acc += dd * dd * dd * terminal_v(dd) * d.n0 * m::pow(dd, d.mu) * m::exp(-d.lam * dd) * taper * DSD_STEP;
                dd += DSD_STEP;
            }
            let back = 6.0 * m::PI * 1e-4 * acc;
            assert!((back / r - 1.0).abs() < 1e-9, "{r}: {back}");
        }
    }

    /// A rigid-surface impact peaks at 0.70 rho V^2 D^2 (the momentum-consistent pulse; Gordillo et al. measure 0.83)
    /// at tp = 0.18 D/V, and drops under ~1.2 mm do
    /// not splash on a wet road while 2 mm drops do.
    #[test]
    fn impact_and_splash_physics() {
        let v = terminal_v(2.0);
        let (_, f) = force(2.0, v, 1.0);
        let want = 0.70 * RHO_W * v * v * 4e-6;
        assert!((f / want - 1.0).abs() < 0.1, "{f} vs {want}");
        assert!(splash_k(1.0, terminal_v(1.0)) < splash_kc(0.5));
        assert!(splash_k(2.0, terminal_v(2.0)) > splash_kc(0.25));
    }

    /// Same seed, same samples.
    #[test]
    fn deterministic() {
        let a = render("light", 7, 2.0).unwrap().0;
        let b = render("light", 7, 2.0).unwrap().0;
        assert_eq!(a.len(), b.len());
        assert!(a.iter().zip(&b).all(|(x, y)| x.to_bits() == y.to_bits()));
    }
}
