//! Live weather audio (prototype): rain as a continuous texture driven by the weather and the camera, block by block,
//! for an audio callback in the world server. Nothing here allocates per block once built.
//!
//! Rain, per block of samples:
//! - grains: a bank of single drops rendered at start-up by the offline synth (rain.rs drop_event: road, puddle, grass,
//!   leaves, metal x 6 sizes x 12 variants, absolute levels) and bubbles; voices pick grains, a distance gain and a
//!   one-pole low-pass. Drops arrive as Poisson streams whose rates follow the drop-size distribution at the current
//!   rain rate: big drops within the rendered radius, near objects, canopy drips, fixed drip points, runoff bubbles.
//!   Streams above `VOICE_RATE_CAP` per second are thinned at equal energy (each voice sqrt(k) louder).
//! - the wash: white noise through a high-pass, a broad band-pass and a low-pass whose corners and level follow the
//!   rain rate (the offline renders' statistics).
//! - in the car: drop impulses on the roof drive 16 resonators (the roof's main radiating modes), dulled by the
//!   headliner, plus the outside through the body (two 450 Hz low-passes); under shelter: the outside through two
//!   900 Hz low-passes.
//! - every control (rain rate, shelter, cabin, volume, mute) is smoothed per block: no zipper noise, no clicks.
//!
//! Thunder: thunder::render_live on a worker thread per strike (the strike's real distance and bearing), mixed in at
//! flash time + its first arrival.

use crate::dsp::Biquad;
use crate::m;
use crate::rain::{self, Surf};
use crate::rng::Rng;

pub const FS: f64 = 44100.0;
const VOICES: usize = 384;
const VOICE_RATE_CAP: f64 = 1200.0; // voices started per second per stream at most (WEATHERSYNTH_VOICE_CAP: a low preset)
const GRAIN_MAX: usize = 4096;
const SIZES: [f64; 6] = [0.9, 1.3, 1.8, 2.5, 3.5, 4.5];
const SURFS: [Surf; 5] = [Surf::Road, Surf::Puddle, Surf::Grass, Surf::Leaves, Surf::Metal];
const VARIANTS: usize = 12;
const OBS_H: f64 = 1.6;

#[derive(Clone, Copy, Debug)]
pub struct Controls {
    pub rain_mm_h: f64,
    /// 1 open sky .. 0 under a roof
    pub shelter: f64,
    /// 1 in the driver's camera (roof drumming, outside damped)
    pub cabin: f64,
    /// the game's master x "other" channel volume
    pub volume: f64,
    /// paused, unfocused (when the game mutes then), the 3D map, a loading screen
    pub muted: bool,
}

struct Grain {
    s: Vec<f32>,
}

#[derive(Clone, Copy, Default)]
struct Voice {
    grain: u32,
    pos: u32,
    gain: f32,
    lp_a: f32,
    lp_s: f32,
    active: bool,
}

/// A rain rate's derived rates and levels (recomputed when the rate moves by more than 2 %).
#[derive(Clone, Copy, Default)]
struct Derived {
    big_rate: f64, // big drops per second within r_ev
    r_ev: f64,
    near_rate: f64,
    canopy_rate: f64,
    drip_period: f64,
    runoff_rate: f64,
    roof_rate: f64,
    wash_gain: f64,
    wash_fc: f64,
    wash_lp: f64,
    film: f64,
    d_max: f64,
    lam: f64,
}

/// Drop sizes blend from Joss drizzle (<= 1 mm/h) through Marshall-Palmer (5 mm/h) to Joss thunderstorm (>= 25 mm/h),
/// in log rate; above 1 mm/h no cap on the size, below it none above ~1.4 mm.
fn derive(r: f64) -> Derived {
    let r = r.max(0.01);
    let x = m::log10(r);
    let lerp = |a: f64, b: f64, t: f64| a + (b - a) * t.clamp(0.0, 1.0);
    // (n0, lambda coefficient) anchors: drizzle (30000, 5.7) at 1 mm/h, MP (8000, 4.1) at 5, thunderstorm (1400, 3.0) at 25
    let (ln0, c) = if r <= 5.0 {
        let t = (x - 0.0) / (m::log10(5.0) - 0.0);
        (lerp(m::ln(30000.0), m::ln(8000.0), t), lerp(5.7, 4.1, t))
    } else {
        let t = (x - m::log10(5.0)) / (m::log10(25.0) - m::log10(5.0));
        (lerp(m::ln(8000.0), m::ln(1400.0), t), lerp(4.1, 3.0, t))
    };
    let n0 = m::exp(ln0);
    let lam = c * m::pow(r, -0.21);
    let d_max = lerp(1.4, 6.0, (x - 0.0) / (m::log10(3.0)));
    let flux = |lo: f64, hi: f64| -> f64 {
        let mut acc = 0.0;
        let mut d = lo;
        while d < hi {
            let cut = if d > d_max { m::exp(-(d - d_max) / 0.1) } else { 1.0 };
            acc += n0 * m::exp(-lam * d) * rain::terminal_v(d) * 0.02 * cut;
            d += 0.02;
        }
        acc
    };
    let t_heavy = ((x - m::log10(5.0)) / (m::log10(35.0) - m::log10(5.0))).clamp(0.0, 1.0);
    let r_ev = lerp(8.0, 3.0, t_heavy);
    let big = flux(1.2, 6.0);
    Derived {
        big_rate: big * m::PI * r_ev * r_ev,
        r_ev,
        near_rate: flux(0.5, 6.0) * 0.4,
        canopy_rate: if r < 20.0 { lerp(5.0, 10.0, x / m::log10(5.0)) * (1.0 - t_heavy) } else { 0.0 },
        drip_period: lerp(1.6, 0.7, x / m::log10(5.0)),
        runoff_rate: lerp(0.0, 400.0, (x - m::log10(3.0)) / (m::log10(35.0) - m::log10(3.0))),
        roof_rate: flux(0.5, 6.0) * 1.9,
        // wash level: rises ~ rate^1.2 (more drops and bigger), its band moves down and widens in a downpour
        wash_gain: 0.004 * m::pow(r, 1.2) * (1.0 + 2.0 * t_heavy),
        wash_fc: lerp(2400.0, 1400.0, t_heavy),
        wash_lp: lerp(9000.0, 6000.0, t_heavy),
        film: lerp(1.0, 4.0, t_heavy),
        d_max,
        lam,
    }
}

// ------------------------------------------------------------------ the level cap (2026-11-26)
//
// The user, 2026-11-26: "the rain drops with Live sounds sound terrible and are super lound". The drops play at
// absolute levels (gain 1 / distance), which in a downpour came out far above the game's own rain. The open-air mix is
// now never louder than the pre-recorded loops play in the game at the same rate: the loops' loudness (SOURCES.md:
// light -30.2 @ 0.7 mm/h, medium -24.2 @ 5, heavy -17.2 @ 35 LUFS integrated) crossfaded in log rate as ambience.lua
// rainWeights does, x the volume the game plays them at (ambience.lua RAIN_GAIN x weather.lua's rain share
// RAIN_CURVE). A loudness meter (K-weighted, CAP_WINDOW_S) on the uncapped mix sets one gain, falling in CAP_DOWN_S,
// rising in CAP_UP_S; never above 1 (quieter rain stays as it is). server/tests/test_liveaudio.py checks these
// numbers against the Lua.

/// ambience.lua RAIN_GAIN
pub const RAIN_BED_GAIN: f64 = 0.5;
/// the loops: (ambience.lua RAIN_LAYERS rate mm/h, SOURCES.md integrated loudness LUFS)
pub const RAIN_LOOPS: [(f64, f64); 3] = [(0.7, -30.2), (5.0, -24.2), (35.0, -17.2)];
/// weather.lua RAIN_CURVE (rate mm/h, the rain's share), PRECIP_OFF
pub const RAIN_SHARE: [(f64, f64); 5] = [(0.05, 0.15), (0.1, 0.25), (1.0, 0.45), (4.0, 0.75), (10.0, 1.0)];
pub const PRECIP_OFF: f64 = 0.02;
/// (the drops run 20-30 dB over the loops uncapped, the recorded-mix agent's sweep 2026-11-26: so the meter is short,
/// momentary loudness, the gain falls fast, starts at 0 and rises only on a signal the meter has seen)
pub const CAP_WINDOW_S: f64 = 0.4;
pub const CAP_DOWN_S: f64 = 0.03;
pub const CAP_UP_S: f64 = 2.0;
/// below this the meter has nothing to go by: the gain is held
pub const CAP_FLOOR_LUFS: f64 = -70.0;
const CAP_BLOCK: usize = 4410; // 100 ms at FS
const CAP_BLOCKS: usize = (CAP_WINDOW_S * 10.0 + 0.5) as usize; // CAP_WINDOW_S of 100 ms blocks

/// Pure: weather.lua precipPct (the rain's share 0..1 for a rate).
pub fn rain_share(r: f64) -> f64 {
    if r <= PRECIP_OFF {
        return 0.0;
    }
    let c = RAIN_SHARE;
    if r <= c[0].0 {
        return c[0].1;
    }
    for w in c.windows(2) {
        if r <= w[1].0 {
            return w[0].1 + (w[1].1 - w[0].1) * m::ln(r / w[0].0) / m::ln(w[1].0 / w[0].0);
        }
    }
    c[c.len() - 1].1
}

/// Pure: the loudness (LUFS) the game's rain plays at for a rate: the loops crossfaded (ambience.lua rainWeights:
/// triangles in log10 rate between the loops' rates, the ends held; summed in power) x RAIN_BED_GAIN x the share.
/// -100 when it does not rain.
pub fn rain_target_lufs(r: f64) -> f64 {
    let share = rain_share(r);
    if share <= 0.0 {
        return -100.0;
    }
    let l = RAIN_LOOPS;
    let x = m::log10(r.max(1e-3));
    let mut w = [0.0f64; 3];
    if x <= m::log10(l[0].0) {
        w[0] = 1.0;
    } else if x >= m::log10(l[2].0) {
        w[2] = 1.0;
    } else {
        for i in 0..2 {
            let (a, b) = (m::log10(l[i].0), m::log10(l[i + 1].0));
            if x <= b {
                let t = (x - a) / (b - a);
                w[i] = 1.0 - t;
                w[i + 1] = t;
                break;
            }
        }
    }
    let p: f64 = (0..3).map(|i| (w[i] * m::undb(l[i].1)).powi(2)).sum();
    10.0 * m::log10(p.max(1e-30)) + 20.0 * m::log10(RAIN_BED_GAIN * share)
}

/// The open-air mix's level cap: a K-weighted meter on the uncapped signal, one smoothed gain (no allocation per block).
pub struct RainCap {
    k1: Biquad,
    k2: Biquad,
    acc: f64,
    n: usize,
    blocks: [f64; CAP_BLOCKS],
    filled: usize,
    at: usize,
    pub gain: f64,
    pub on: bool,
}

impl RainCap {
    pub fn new() -> RainCap {
        let (k1, k2) = crate::loudness::kweight_filters(FS);
        RainCap { k1, k2, acc: 0.0, n: 0, blocks: [0.0; CAP_BLOCKS], filled: 0, at: 0, gain: 0.0, on: true }
    }

    /// The meter's loudness now (LUFS over the last CAP_WINDOW_S, or what it has), None before 0.3 s.
    pub fn measured(&self) -> Option<f64> {
        if self.filled < 3 {
            return None;
        }
        let s: f64 = self.blocks[..self.filled].iter().sum();
        Some(crate::loudness::lufs_of(s / self.filled as f64))
    }

    /// Meter x (the uncapped open-air mix of one block, rate r mm/h), then scale it in place by the cap (ramped over
    /// the block from the last gain).
    pub fn process(&mut self, x: &mut [f32], r: f64) {
        for &v in x.iter() {
            let k = self.k2.tick(self.k1.tick(v as f64));
            self.acc += k * k;
            self.n += 1;
            if self.n == CAP_BLOCK {
                self.blocks[self.at] = self.acc / CAP_BLOCK as f64;
                self.at = (self.at + 1) % CAP_BLOCKS;
                self.filled = (self.filled + 1).min(CAP_BLOCKS);
                self.acc = 0.0;
                self.n = 0;
            }
        }
        let want = match (self.on, self.measured()) {
            (false, _) => 1.0,
            (true, Some(l)) if l > CAP_FLOOR_LUFS => m::undb(rain_target_lufs(r) - l).min(1.0),
            // (silence, or not measured yet: held; a fresh cap starts at 0, so the first drops never burst out)
            (true, _) => self.gain,
        };
        let dt = x.len() as f64 / FS;
        let tau = if want < self.gain { CAP_DOWN_S } else { CAP_UP_S };
        let g0 = self.gain;
        let g1 = g0 + (want - g0) * (1.0 - m::exp(-dt / tau));
        let n = x.len().max(1) as f64;
        for (i, v) in x.iter_mut().enumerate() {
            *v *= (g0 + (g1 - g0) * (i as f64 / n)) as f32;
        }
        self.gain = g1;
    }
}

pub struct LiveRain {
    pub cap: RainCap,
    grains: Vec<Grain>,
    bubbles: Vec<Grain>,
    voices: Vec<Voice>,
    rng: Rng,
    cur: Controls,
    target: Controls,
    der: Derived,
    der_rate: f64,
    // wash
    wash_hp: Biquad,
    wash_bp: Biquad,
    wash_lp: Biquad,
    gust: f64,
    gust_v: f64,
    // fixed drip points: (time to next s, period s, grain base, distance m)
    drips: Vec<(f64, f64, u32, f64)>,
    // roof modes: (b0 gain, c1, c2, y1, y2)
    roof: Vec<(f64, f64, f64, f64, f64)>,
    roof_lp: Biquad,
    // shelter and cabin paths
    sh1: Biquad,
    sh2: Biquad,
    cb1: Biquad,
    cb2: Biquad,
    gain_s: f64,
    voice_cap: f64,
    // scratch
    out_open: Vec<f32>,
    force: Vec<f64>,
}

impl LiveRain {
    pub fn new(seed: u64) -> LiveRain {
        let mut grng = Rng::new(seed, "live-grains");
        let mut grains = Vec::with_capacity(SURFS.len() * SIZES.len() * VARIANTS);
        for &surf in SURFS.iter() {
            for &d in SIZES.iter() {
                for _ in 0..VARIANTS {
                    let v = rain::terminal_v(d);
                    let ev = rain::drop_event(&mut grng, surf, d, v, 1.5);
                    grains.push(Grain { s: to_grain(&ev) });
                }
            }
        }
        let mut bubbles = Vec::new();
        for _ in 0..48 {
            let rad = grng.lognormal(1.8e-3, 0.55).clamp(0.5e-3, 6e-3);
            bubbles.push(Grain { s: to_grain(&rain::bubble(&mut grng, rad)) });
        }
        let mut drng = Rng::new(seed, "live-drips");
        let drips = (0..12)
            .map(|_| {
                let rho: f64 = drng.range(1.2, 9.0);
                let surf = (drng.f() * 3.0) as u32; // road, puddle, grass
                let base = surf * (SIZES.len() * VARIANTS) as u32 + (4 * VARIANTS) as u32 + (drng.f() * VARIANTS as f64) as u32;
                (drng.range(0.0, 2.0), drng.lognormal(1.0, 0.4), base, m::sqrt(rho * rho + OBS_H * OBS_H))
            })
            .collect();
        // roof: the 16 strongest odd-odd modes of a 1.30 x 1.45 m panel with (1,1) at 62 Hz (rain.rs panel)
        let mut roof = Vec::new();
        let scale = 62.0 / (1.0 / (1.3 * 1.3) + 1.0 / (1.45 * 1.45));
        for mi in [1usize, 3, 5, 7] {
            for ni in [1usize, 3, 5, 7] {
                let (fm, fn_) = (mi as f64 / 1.3, ni as f64 / 1.45);
                let f = scale * (fm * fm + fn_ * fn_);
                let eta = 0.04 * (1.0 + f / 2000.0) + 0.12 * m::exp(-f / 250.0);
                let r = m::exp(-m::PI * f * eta / FS);
                let w = m::TAU * f / FS;
                let g = m::sin(w) * m::pow(f / 1000.0, 0.4) / ((mi * ni) as f64);
                roof.push((g, 2.0 * r * m::cos(w), r * r, 0.0, 0.0));
            }
        }
        let c = Controls { rain_mm_h: 0.0, shelter: 1.0, cabin: 0.0, volume: 1.0, muted: false };
        LiveRain {
            cap: RainCap::new(),
            grains,
            bubbles,
            voices: vec![Voice::default(); VOICES],
            rng: Rng::new(seed, "live-run"),
            cur: c,
            target: c,
            der: derive(0.01),
            der_rate: 0.01,
            wash_hp: Biquad::highpass(FS, 250.0, 0.7071),
            wash_bp: Biquad::bandpass(FS, 2000.0, 0.5),
            wash_lp: Biquad::lowpass(FS, 8000.0, 0.7071),
            gust: 1.0,
            gust_v: 0.0,
            drips,
            roof,
            roof_lp: Biquad::lowpass(FS, 1100.0, 0.6),
            sh1: Biquad::lowpass(FS, 900.0, 0.7071),
            sh2: Biquad::lowpass(FS, 900.0, 0.7071),
            cb1: Biquad::lowpass(FS, 450.0, 0.7071),
            cb2: Biquad::lowpass(FS, 450.0, 0.7071),
            gain_s: 0.0,
            voice_cap: std::env::var("WEATHERSYNTH_VOICE_CAP").ok().and_then(|v| v.parse().ok()).unwrap_or(VOICE_RATE_CAP),
            out_open: Vec::new(),
            force: Vec::new(),
        }
    }

    pub fn set(&mut self, c: Controls) {
        self.target = c;
    }

    fn start(&mut self, grain: u32, gain: f64, fc: f64) {
        if let Some(v) = self.voices.iter_mut().find(|v| !v.active) {
            *v = Voice {
                grain,
                pos: 0,
                gain: gain as f32,
                lp_a: crate::dsp::onepole_coef(FS, fc) as f32,
                lp_s: 0.0,
                active: true,
            };
        }
    }

    fn size_index(&mut self, lo: f64) -> usize {
        // a drop size from the current distribution (>= lo), mapped to the nearest bank size
        let lam = self.der.lam;
        let u = self.rng.f();
        let d = (lo - m::ln(1.0 - u) / lam).min(self.der.d_max.max(lo));
        let mut best = 0;
        for (i, s) in SIZES.iter().enumerate() {
            if (s - d).abs() < (SIZES[best] - d).abs() {
                best = i;
            }
        }
        best
    }

    /// Poisson arrivals in a block of `dt` seconds for a stream of `rate`, thinned above the cap: (count, gain factor).
    fn arrivals(&mut self, rate: f64, dt: f64) -> (usize, f64) {
        let k = (rate / self.voice_cap).max(1.0);
        (self.rng.poisson(rate / k * dt * self.gust), m::sqrt(k))
    }

    /// Render `out.len()` samples (mono) into out, mixing over what is there.
    pub fn process(&mut self, out: &mut [f32]) {
        let n = out.len();
        let dt = n as f64 / FS;
        // smooth the controls (~0.5 s for the rate, 0.3 s for shelter / cabin)
        let a = 1.0 - m::exp(-dt / 0.5);
        let b = 1.0 - m::exp(-dt / 0.3);
        self.cur.rain_mm_h += a * (self.target.rain_mm_h - self.cur.rain_mm_h);
        self.cur.shelter += b * (self.target.shelter - self.cur.shelter);
        self.cur.cabin += b * (self.target.cabin - self.cur.cabin);
        self.cur.volume = self.target.volume;
        let want = if self.target.muted { 0.0 } else { self.cur.volume };
        let r = self.cur.rain_mm_h;
        if (r - self.der_rate).abs() > 0.02 * self.der_rate.max(0.01) {
            self.der = derive(r);
            self.der_rate = r;
            self.wash_bp = with_state(Biquad::bandpass(FS, self.der.wash_fc, 0.5), &self.wash_bp);
            self.wash_lp = with_state(Biquad::lowpass(FS, self.der.wash_lp, 0.7071), &self.wash_lp);
        }
        // gusts: a slow random walk of the drop rate (+/-40 %)
        self.gust_v += (self.rng.normal() * 0.6 - 0.3 * (self.gust - 1.0)) * dt;
        self.gust = (self.gust + self.gust_v * dt).clamp(0.6, 1.4);
        let d = self.der;
        if self.out_open.len() != n {
            self.out_open = vec![0.0f32; n];
            self.force = vec![0.0; n];
        }
        for v in self.out_open.iter_mut() {
            *v = 0.0;
        }
        for v in self.force.iter_mut() {
            *v = 0.0;
        }
        let silent = r < 0.02;
        if !silent {
            let per = SIZES.len() * VARIANTS;
            // big drops within r_ev
            let (cnt, g) = self.arrivals(d.big_rate, dt);
            for _ in 0..cnt {
                let rho = d.r_ev * m::sqrt(self.rng.f());
                let rr = m::sqrt(rho * rho + OBS_H * OBS_H);
                // surfaces: 0 road, 1 puddle, 2 grass, 3 leaves (more puddles as the water stands)
                let p_puddle = 0.1 + 0.3 * (d.film - 1.0) / 3.0;
                let u = self.rng.f();
                let surf = if u < p_puddle {
                    1
                } else if u < p_puddle + 0.25 {
                    2
                } else if u < p_puddle + 0.4 {
                    3
                } else {
                    0
                };
                let si = self.size_index(1.2);
                let gi = surf * per + si * VARIANTS + (self.rng.f() * VARIANTS as f64) as usize;
                let fc = if rho > 4.0 { (14000.0 * m::pow(4.0 / rho, 0.6)).max(1500.0) } else { 16000.0 };
                self.start(gi as u32, g / rr, fc);
            }
            // near objects (leaves, metal, puddle at 0.5-1.2 m)
            let (cnt, g) = self.arrivals(d.near_rate, dt);
            for _ in 0..cnt {
                let surf = [3usize, 3, 4, 1][(self.rng.f() * 4.0) as usize % 4];
                let si = self.size_index(0.9);
                let gi = surf * per + si * VARIANTS + (self.rng.f() * VARIANTS as f64) as usize;
                let rr = self.rng.range(0.5, 1.2);
                self.start(gi as u32, g / rr, 16000.0);
            }
            // canopy drips
            let (cnt, _) = self.arrivals(d.canopy_rate, dt);
            for _ in 0..cnt {
                let rho = self.rng.range(1.5, 12.0);
                let rr = m::sqrt(rho * rho + OBS_H * OBS_H);
                let surf = [0usize, 1, 2][(self.rng.f() * 3.0) as usize % 3];
                let gi = surf * per + 5 * VARIANTS + (self.rng.f() * VARIANTS as f64) as usize;
                self.start(gi as u32, 1.0 / rr, (14000.0 * m::pow(4.0 / rho.max(4.0), 0.6)).max(1500.0));
            }
            // fixed drip points, while the canopy drips
            if d.canopy_rate > 0.0 {
                for i in 0..self.drips.len() {
                    let (mut t, p, base, rr) = self.drips[i];
                    t -= dt;
                    if t <= 0.0 {
                        t += p * d.drip_period * self.rng.range(0.85, 1.15);
                        let gi = base;
                        self.start(gi, 1.0 / rr, 9000.0);
                    }
                    self.drips[i].0 = t;
                }
            }
            // runoff
            let (cnt, g) = self.arrivals(d.runoff_rate, dt);
            for _ in 0..cnt {
                let bi = (self.rng.f() * self.bubbles.len() as f64) as usize;
                let lv = self.rng.lognormal(1.0, 0.6);
                self.start((self.grains.len() + bi) as u32, 0.15 * g * lv / 5.0, 4000.0);
            }
            // roof impulses (momentum ~ D^3 v), placed in the block
            if self.cur.cabin > 0.001 {
                let (cnt, g) = self.arrivals(d.roof_rate, dt);
                for _ in 0..cnt {
                    let si = self.size_index(0.5);
                    let dd = SIZES[si] * self.rng.range(0.8, 1.2);
                    let at = (self.rng.f() * n as f64) as usize % n;
                    self.force[at] += g * dd * dd * dd * rain::terminal_v(dd) * 0.01;
                }
            }
        }
        // voices into the open-air mix
        let ng = self.grains.len();
        for v in self.voices.iter_mut() {
            if !v.active {
                continue;
            }
            let src: &[f32] = if (v.grain as usize) < ng { &self.grains[v.grain as usize].s } else { &self.bubbles[v.grain as usize - ng].s };
            let pos = v.pos as usize;
            let left = src.len().saturating_sub(pos);
            let k = left.min(self.out_open.len());
            let seg = &src[pos..pos + k];
            if v.lp_a > 0.97 {
                // open (a near drop): no low-pass
                let g = v.gain;
                for (o, &x) in self.out_open[..k].iter_mut().zip(seg) {
                    *o += x * g;
                }
            } else {
                let mut s = v.lp_s;
                for (o, &x) in self.out_open[..k].iter_mut().zip(seg) {
                    s += v.lp_a * (x - s);
                    *o += s * v.gain;
                }
                v.lp_s = s;
            }
            v.pos = (pos + k) as u32;
            if k < self.out_open.len() || pos + k >= src.len() {
                v.active = false;
            }
        }
        // the wash
        let wg = if silent { 0.0 } else { d.wash_gain * m::sqrt(self.gust) };
        for o in self.out_open.iter_mut() {
            let w = (self.rng.f() * 2.0 - 1.0) * wg;
            let w = self.wash_lp.tick(self.wash_bp.tick(self.wash_hp.tick(w)));
            *o += w as f32;
        }
        // the open air never louder than the game's own rain loops at this rate (RainCap)
        self.cap.process(&mut self.out_open, self.cur.rain_mm_h);
        // mix: open air, under shelter (muffled), in the cabin (roof + outside through the body)
        let sh = self.cur.shelter;
        let cab = self.cur.cabin;
        let g0 = self.gain_s;
        let g1 = want;
        let muffle_on = sh < 0.999;
        for i in 0..n {
            let x = self.out_open[i] as f64;
            let outside = if muffle_on {
                let muff = self.sh2.tick(self.sh1.tick(x)) * 1.6;
                x * sh + muff * (1.0 - sh) * 0.7
            } else {
                x
            };
            let mut roof = 0.0;
            if cab > 0.001 {
                let f = self.force[i];
                for md in self.roof.iter_mut() {
                    let y = md.1 * md.3 - md.2 * md.4 + f;
                    md.4 = md.3;
                    md.3 = y;
                    roof += md.0 * y;
                }
                roof = self.roof_lp.tick(roof) * sh;
            }
            let y = if cab > 0.001 {
                let body = self.cb2.tick(self.cb1.tick(outside));
                outside * (1.0 - cab) + (body * 0.6 + roof * 0.9) * cab
            } else {
                outside
            };
            let g = g0 + (g1 - g0) * (i as f64 / n as f64);
            out[i] += (y * g) as f32;
        }
        self.gain_s = g1;
    }

    pub fn active_voices(&self) -> usize {
        self.voices.iter().filter(|v| v.active).count()
    }
}

/// A grain: the event cut where what is left holds less than 1e-5 of its energy (-50 dB; most drops are 5-20 ms,
/// so the voices stay short), at most GRAIN_MAX samples, with a short fade.
fn to_grain(ev: &[f64]) -> Vec<f32> {
    let total: f64 = ev.iter().map(|v| v * v).sum();
    let mut tail = 0.0;
    let mut cut = ev.len();
    for (i, v) in ev.iter().enumerate().rev() {
        tail += v * v;
        if tail > 1e-5 * total {
            cut = i + 1;
            break;
        }
    }
    let n = cut.min(GRAIN_MAX).max(1);
    let fade = 256.min(n);
    (0..n)
        .map(|i| {
            let w = if i + fade >= n { (n - i) as f64 / fade as f64 } else { 1.0 };
            (ev[i] * w) as f32
        })
        .collect()
}

/// New coefficients, the old filter's state (a control change mid-stream without a click).
fn with_state(mut new: Biquad, old: &Biquad) -> Biquad {
    new.copy_state(old);
    new
}

/// Offline harness: run the engine through a scripted sweep (dry -> light -> medium -> heavy -> into the car ->
/// under a roof -> muted) in blocks, time it, and optionally write what it played.
/// `fixed`: constant (rate, shelter, cabin) instead of the sweep.
pub fn bench_with(seconds: f64, block: usize, wav: Option<&str>, fixed: Option<(f64, f64, f64)>) -> String {
    let t_init = std::time::Instant::now();
    let mut lr = LiveRain::new(1);
    let init_s = t_init.elapsed().as_secs_f64();
    let total = (seconds * FS) as usize;
    let mut out_all: Vec<f64> = if wav.is_some() { Vec::with_capacity(total) } else { Vec::new() };
    let mut buf = vec![0.0f32; block];
    let mut busy = 0.0f64;
    let mut worst = 0.0f64;
    let mut max_voices = 0;
    let mut done = 0;
    while done < total {
        let t = done as f64 / FS;
        let frac = t / seconds;
        // the sweep
        let (rate, shelter, cabin, muted) = if frac < 0.05 {
            (0.0, 1.0, 0.0, false)
        } else if frac < 0.25 {
            (0.7, 1.0, 0.0, false)
        } else if frac < 0.45 {
            (5.0, 1.0, 0.0, false)
        } else if frac < 0.65 {
            (35.0, 1.0, 0.0, false)
        } else if frac < 0.8 {
            (35.0, 1.0, 1.0, false)
        } else if frac < 0.93 {
            (35.0, 0.0, 0.0, false)
        } else {
            (35.0, 1.0, 0.0, true)
        };
        let (rate, shelter, cabin, muted) = match fixed {
            Some((r, s, c)) => (r, s, c, false),
            None => (rate, shelter, cabin, muted),
        };
        lr.set(Controls { rain_mm_h: rate, shelter, cabin, volume: 1.0, muted });
        for v in buf.iter_mut() {
            *v = 0.0;
        }
        let t0 = std::time::Instant::now();
        lr.process(&mut buf);
        let el = t0.elapsed().as_secs_f64();
        busy += el;
        worst = worst.max(el);
        max_voices = max_voices.max(lr.active_voices());
        if wav.is_some() {
            out_all.extend(buf.iter().map(|&v| v as f64));
        }
        done += block;
    }
    if let Some(p) = wav {
        let _ = crate::wav::write_f32(p, &out_all, FS as u32);
    }
    let block_ms = block as f64 / FS * 1000.0;
    format!(
        "audio_s={seconds:.0} block={block} ({block_ms:.1} ms) init_s={init_s:.3} cpu_s={busy:.3} core_pct={:.3} worst_block_ms={:.3} ({:.1} % of its deadline) max_voices={max_voices}",
        100.0 * busy / seconds,
        worst * 1000.0,
        100.0 * worst * 1000.0 / block_ms
    )
}

/// Time live thunder renders (worker-thread cost per strike) at distances, for a detail level.
pub fn thunder_bench(micro_min: f64) -> String {
    let mut s = String::new();
    for (i, dist) in [150.0, 900.0, 3500.0, 10000.0].iter().enumerate() {
        let t0 = std::time::Instant::now();
        let (y, first) = crate::thunder::render_live(1000 + i as u64, *dist, 0.7, micro_min, 14.0);
        let el = t0.elapsed().as_secs_f64();
        s.push_str(&format!(
            "dist_m={dist:.0} micro_min={micro_min} render_s={el:.2} sound_arrives_after_s={:.2} samples={} \n",
            first,
            y.len()
        ));
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_target_is_the_games_rain() {
        // at the heavy loop's rate (share 1 from 10 mm/h): the loop x RAIN_BED_GAIN (-6.02 dB)
        assert!((rain_target_lufs(35.0) - (-17.2 - 6.0206)).abs() < 0.01);
        // 5 mm/h: the medium loop alone, its share 0.75 + 0.25 ln(5 / 4) / ln(10 / 4)
        let share = 0.75 + 0.25 * (1.25f64).ln() / (2.5f64).ln();
        assert!((rain_target_lufs(5.0) - (-24.2 + 20.0 * (0.5 * share).log10())).abs() < 0.01);
        assert!(rain_target_lufs(0.0) <= -100.0 && rain_share(0.01) == 0.0);
        assert!(rain_target_lufs(15.0) > rain_target_lufs(5.0) && rain_target_lufs(5.0) > rain_target_lufs(0.7));
    }

    /// The live rain's loudness (integrated over the last half of `secs`) at a fixed rate, the cap on or off.
    fn loudness_at(rate: f64, cap: bool, secs: f64) -> f64 {
        let mut lr = LiveRain::new(7);
        lr.cap.on = cap;
        lr.set(Controls { rain_mm_h: rate, shelter: 1.0, cabin: 0.0, volume: 1.0, muted: false });
        let mut out = Vec::new();
        let mut buf = vec![0.0f32; 512];
        while (out.len() as f64) < secs * FS {
            buf.iter_mut().for_each(|v| *v = 0.0);
            lr.process(&mut buf);
            out.extend(buf.iter().map(|&v| v as f64));
        }
        let half = out.len() / 2;
        crate::loudness::integrated(&out[half..], FS, false)
    }

    #[test]
    fn the_live_rain_is_never_louder_than_the_loops() {
        for rate in [0.7, 5.0, 15.0, 35.0] {
            let (raw, capped, target) = (loudness_at(rate, false, 10.0), loudness_at(rate, true, 10.0), rain_target_lufs(rate));
            eprintln!("live rain {rate} mm/h: uncapped {raw:.1} LUFS, capped {capped:.1}, the game's loops {target:.1}");
            assert!(capped <= target + 1.0, "{rate} mm/h: {capped:.1} LUFS over the loops' {target:.1}");
            assert!(capped <= raw + 0.1, "the cap never makes it louder");
        }
    }

    #[test]
    fn no_burst_at_the_start_or_a_downpour_arriving() {
        // the start at 35 mm/h, then dry, then a jump from drizzle to a downpour: the loudest 400 ms anywhere stays
        // within a few dB of the loops' level for the rate it reaches
        let mut lr = LiveRain::new(3);
        let mut out = Vec::new();
        let mut buf = vec![0.0f32; 512];
        let plan = [(35.0, 3.0), (0.0, 3.0), (0.7, 4.0), (35.0, 4.0)];
        for (rate, secs) in plan {
            lr.set(Controls { rain_mm_h: rate, shelter: 1.0, cabin: 0.0, volume: 1.0, muted: false });
            let end = out.len() + (secs * FS) as usize;
            while out.len() < end {
                buf.iter_mut().for_each(|v| *v = 0.0);
                lr.process(&mut buf);
                out.extend(buf.iter().map(|&v| v as f64));
            }
        }
        let kw = crate::loudness::kweight(&out, FS);
        let win = (0.4 * FS) as usize;
        let mut worst = -100.0f64;
        let mut i = 0;
        while i + win <= kw.len() {
            let ms = kw[i..i + win].iter().map(|v| v * v).sum::<f64>() / win as f64;
            worst = worst.max(crate::loudness::lufs_of(ms));
            i += win / 4;
        }
        let target = rain_target_lufs(35.0);
        eprintln!("the loudest 400 ms: {worst:.1} LUFS (the loops at 35 mm/h {target:.1})");
        assert!(worst <= target + 4.0, "{worst:.1} LUFS");
    }
}
