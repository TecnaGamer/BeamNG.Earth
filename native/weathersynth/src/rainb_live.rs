//! Method B rain, live: the physical drop model (rainb.rs) as a streaming engine for the world server's audio callback.
//! Not wired into live-serve (2026-11-26: the user approved the offline loops' sound; the first live engine "sounds
//! very broken and glitchy": its cabin path was ~40 dB hot and clipped, every drop of a block started on the block's
//! first sample, and a few hundred sqrt(k)-loud grains stood in for thousands of quiet drops: crackle. This is v2.)
//!
//! - **Chunks:** everything is computed in fixed CH-sample chunks, whatever block the caller asks for. Controls are
//!   read at chunk starts, so the output is the same at any block size for the same control timeline.
//! - **Distinct drops:** each (ring, size band) stream plays at most `cap(ring)` drops a second at their true level,
//!   each at its own sample inside the chunk. Grains are rainb.rs's own drop events: road, puddle, soil, leaf and
//!   three metal panels x 7 size bands x 24 variants, plus canopy drips and runoff bubbles. Each grain keeps its
//!   dipole part and its every-way part, so a voice weighs the dipole by the listener's cos(theta). Distance:
//!   1/3-octave low-pass bins, one one-pole per bin per sample.
//! - **The dense rest (the wash):** a stream's drops above its cap overlap so much that their sum is Gaussian noise
//!   with the drops' mean spectrum at their total power. White noise through 9 octave band-passes, each band's gain
//!   from the grains' measured octave energies x the remaining rate x the ring's distance gain and low-pass. The bands
//!   follow the rate's flicker sample-smoothly (gains interpolated across the chunk).
//! - **Rate:** glides in log rate (1 s); gusts and the 0.5-8 Hz clustering flicker as offline (per chunk, fixed dt).
//! - **Level:** static, from a calibration table (LEVELS: the engine's own loudness by rate, measured by
//!   `rainb-live-calibrate`), so the open air, the cabin and the shelter each play at the game's loop loudness for
//!   the rate (live.rs rain_target_lufs). No meter in the loop, so nothing pumps.
//! - **Scenes:** the cabin crossfades equal-power (cos / sin) over CROSS_S. Inside: the roof's 24 strongest
//!   radiating modes (16) driven by every hit on it, headliner and cabin low end, the windscreen's ticks, the outside
//!   through the body. The shelter fades in the same way (drops over the 12 m strip left out with its weight, the wash
//!   reduced by the strip's share of each ring, the edges' drips, the deck's reflection); its level is
//!   interpolated in dB.
//! - **Dry:** below 0.02 mm/h with nothing left ringing, a chunk is skipped outright: no noise, no filters.
//! - **No allocation** once built.

use crate::dsp::{self, Biquad};
use crate::m;
use crate::nprng::NpRng;
use crate::rainb::{self, Surf};

pub const FS: f64 = 44100.0;
pub const CH: usize = 256;
const VARIANTS: usize = 24;
const VOICES: usize = 768;
const GRAIN_MAX: usize = 4096;
/// the event bins: the 1/3-octave low-pass bins, the open bin, and the windscreen's ticks (the cabin's own bin)
const NB: usize = rainb::BUCKETS + 2;
const OPEN_BIN: usize = rainb::BUCKETS;
const GLASS_BIN: usize = rainb::BUCKETS + 1;
const OCT: [f64; 9] = [63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0];
const NO: usize = 9;
/// distinct drops a second per (ring, band) stream at most, by ring (the rest is the wash)
const CAPS: [f64; 7] = [400.0, 300.0, 150.0, 60.0, 0.0, 0.0, 0.0];
/// distinct canopy drips a second per ring at most (the rest is the wash)
const DRIP_CAPS: [f64; 7] = [40.0, 60.0, 60.0, 40.0, 0.0, 0.0, 0.0];
/// the caps' scale, the most drops one distinct drop stands for, and the clustering flicker's corner (Hz)
const CAP_X: f64 = 2.0;
const K_MAX: f64 = 8.0;
/// the most drips one distinct drip stands for (the offline thinning makes the far drips the heavy rain's events)
const DRIP_K: f64 = 32.0;
/// metal hits a second per (ring, band) at most
const METAL_CAP: f64 = 6.0;
/// the roof hits' own clicks into the cabin's tick bin, relative to a windscreen tick
const ROOF_CLICK: f64 = 0.5;
const FLICKER_HZ: f64 = 1.0;
const CROSS_S: f64 = 1.5;
const RESTREAM_CHUNKS: u32 = 8;
const RATE_TAU_S: f64 = 1.0;
const SH_HALF: f64 = 6.0;
/// surfaces in the bank: road, puddle, soil, leaf, metal 0..2, then drips (road, puddle, soil), then runoff bubbles
const S_LEAF: usize = 3;
const N_SURF: usize = 7;

/// The calibration (rainb-live-calibrate, 2026-11-26): the engine's own integrated loudness (LUFS) at unity gain by
/// rate, for the open air, the cabin path and the shelter (shelter weight 1); and the cabin parts' gains.
const LEVEL_RATES: [f64; 11] = [0.05, 0.1, 0.3, 0.7, 1.0, 2.0, 5.0, 10.0, 20.0, 35.0, 60.0];
const LEVEL_OPEN: [f64; 11] = [-37.50, -34.45, -26.34, -23.24, -18.20, -13.50, -5.78, 1.48, 6.80, 10.48, 14.21];
const LEVEL_CABIN: [f64; 11] = [-37.92, -31.78, -26.04, -22.01, -18.63, -13.87, -8.39, -4.28, 0.77, 3.59, 8.74];
const LEVEL_SHELTER: [f64; 11] = [-40.23, -38.35, -30.54, -22.56, -22.55, -16.78, -8.40, -1.60, 5.65, 9.32, 11.08];
const GLASS_G: f64 = 1.027607e0;
const BODY_G: f64 = 2.486389e1;
/// the muffled loop plays 3.1 dB under the open rain (ambience.lua RAIN_UNDER_ROOF 0.7)
const SHELTER_DB: f64 = -3.1;

/// A bank of MB two-pole resonators driven by one input, as arrays (the per-sample loop over the modes
/// vectorises); the output summed in 4 lanes, then the lanes in order (a fixed summation order: deterministic).
#[derive(Clone)]
struct ModeBank<const MB: usize> {
    g: [f64; MB],
    c1: [f64; MB],
    c2: [f64; MB],
    y1: [f64; MB],
    y2: [f64; MB],
}

impl<const MB: usize> ModeBank<MB> {
    /// From (gain, c1, c2) triples (unused slots silent).
    fn new(modes: &[(f64, f64, f64)]) -> ModeBank<MB> {
        let mut b = ModeBank { g: [0.0; MB], c1: [0.0; MB], c2: [0.0; MB], y1: [0.0; MB], y2: [0.0; MB] };
        for (k, &(g, c1, c2)) in modes.iter().take(MB).enumerate() {
            b.g[k] = g;
            b.c1[k] = c1;
            b.c2[k] = c2;
        }
        b
    }
    fn run(&mut self, input: &[f64; CH], out: &mut [f64; CH], gain: f64) {
        for j in 0..CH {
            let x = input[j];
            let mut lane = [0.0f64; 4];
            for k in 0..MB {
                let y = self.c1[k] * self.y1[k] - self.c2[k] * self.y2[k] + x;
                self.y2[k] = self.y1[k];
                self.y1[k] = y;
                lane[k & 3] += self.g[k] * y;
            }
            out[j] += gain * ((lane[0] + lane[1]) + (lane[2] + lane[3]));
        }
    }
}

/// RBJ peaking EQ (gain_db at f0, quality q).
fn peaking(f0: f64, q: f64, gain_db: f64) -> Biquad {
    let a = m::pow(10.0, gain_db / 40.0);
    let w = m::TAU * f0 / FS;
    let (sn, cs) = (m::sin(w), m::cos(w));
    let al = sn / (2.0 * q);
    let a0 = 1.0 + al / a;
    Biquad::new((1.0 + al * a) / a0, -2.0 * cs / a0, (1.0 - al * a) / a0, -2.0 * cs / a0, (1.0 - al / a) / a0)
}

/// The cabin's sound (2026-11-26, the user: "try to improve the inside car sounds. Use filters or effects"):
/// - roof: a dense floor (the small drops' impulse power as noise, raised by `floor_db`) and the big drops' impulses
///   through the roof's 16 modes, plus drum-like thumps (the big drops through 4 damped low panel modes);
/// - the cabin's transfer: the headliner's low-pass, the closed cabin's low end, body / panel resonances at ~110 and
///   ~190 Hz;
/// - the windscreen's ticks brighter (3.5 kHz) and quieter;
/// - a compressor on the cabin mix (fast envelope against a slow level: the ticks and thumps do not jump out of the
///   floor), then a small-room reverb (early reflections at 3-9 ms and two damped combs, RT ~0.15 s).
struct CabinFx {
    thump: ModeBank<4>,
    pk1: Biquad,
    pk2: Biquad,
    glass_hp: Biquad,
    env: f64,
    slow: f64,
    att: f64,
    rel: f64,
    slow_a: f64,
    thr: f64,
    ratio: f64,
    line: Vec<f32>,
    at: usize,
    comb_s: [f32; 2],
    er_lp: f32,
    rev_mix: f32,
    floor_g: f64,
    thump_g: f64,
    thump_d: f64,
    glass_g: f64,
    /// the patter floor on the glass and the roof's skin (the wash noise high-passed), relative to the roof floor
    hf_bp: Biquad,
    hf_g: f64,
    tick_pre: f64,
}

const CAB_LINE: usize = 2048;

impl CabinFx {
    fn new() -> CabinFx {
        let mk = |f: f64, q: f64, g: f64| {
            let w = m::TAU * f / FS;
            let rr = m::exp(-m::PI * f / q / FS);
            (g * m::sin(w), 2.0 * rr * m::cos(w), rr * rr)
        };
        CabinFx {
            thump: ModeBank::new(&[mk(85.0, 5.0, 1.0), mk(132.0, 6.0, 0.8), mk(205.0, 6.0, 0.6), mk(320.0, 7.0, 0.35)]),
            pk1: peaking(110.0, 2.0, env_f("pk1_db", 2.0)),
            pk2: peaking(190.0, 2.5, env_f("pk2_db", 2.0)),
            glass_hp: Biquad::bandpass(FS, env_f("glass_hz", 2800.0), 0.7),
            env: 0.0,
            slow: 0.0,
            att: dsp::onepole_coef(FS, 1.0 / (m::TAU * 0.001)),
            rel: 1.0 - m::exp(-1.0 / (0.08 * FS)),
            slow_a: 1.0 - m::exp(-1.0 / (0.4 * FS)),
            thr: m::undb(env_f("comp_thr_db", 6.0)),
            ratio: env_f("comp_ratio", 2.0),
            line: vec![0.0; CAB_LINE],
            at: 0,
            comb_s: [0.0; 2],
            er_lp: 0.0,
            rev_mix: env_f("rev_mix", 0.3) as f32,
            floor_g: m::undb(env_f("floor_db", 10.0)),
            thump_g: m::undb(env_f("thump_db", 0.0)),
            thump_d: env_f("thump_d", 2.2),
            glass_g: m::undb(env_f("glass_db", -2.0)),
            hf_bp: Biquad::highpass(FS, env_f("hf_hz", 2500.0), 0.7071),
            hf_g: m::undb(env_f("hf_db", -6.0)),
            tick_pre: env_f("tick_pre", 0.0),
        }
    }
}

/// The wash's octave band-passes, sharing one input (RBJ band-pass coefficients, b1 = 0, b2 = -b0).
struct WashBank {
    b0: [f64; NO],
    a1: [f64; NO],
    a2: [f64; NO],
    y1: [f64; NO],
    y2: [f64; NO],
    x1: f64,
    x2: f64,
}

impl WashBank {
    fn new(bp: &[Biquad]) -> WashBank {
        let mut w = WashBank { b0: [0.0; NO], a1: [0.0; NO], a2: [0.0; NO], y1: [0.0; NO], y2: [0.0; NO], x1: 0.0, x2: 0.0 };
        for (o, b) in bp.iter().enumerate().take(NO) {
            w.b0[o] = b.b0;
            w.a1[o] = b.a1;
            w.a2[o] = b.a2;
        }
        w
    }
}

struct Grain {
    dip: Vec<f32>,
    iso: Vec<f32>,
}

#[derive(Clone, Copy, Default)]
struct Voice {
    grain: u32,
    pos: u32,
    delay: u32,
    gd: f32,
    gi: f32,
    bin: u8,
    active: bool,
}

#[derive(Clone, Copy, Debug)]
pub struct Controls {
    pub rain_mm_h: f64,
    /// 1 open sky .. 0 under a shelter
    pub shelter: f64,
    /// 1 in the driver's camera
    pub cabin: f64,
    pub volume: f64,
    pub muted: bool,
}

/// What process() puts out (the calibration listens to the parts).
#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Tap {
    Mix,
    OpenRaw,
    RoofRaw,
    GlassRaw,
    BodyRaw,
    CabinRaw,
}

/// Ring geometry for one surface class: mean squared gains over the ring's area (dipole, every way) and its low-pass.
#[derive(Clone, Copy, Default)]
struct RingGain {
    gd2: f64,
    gi2: f64,
    /// the ring's low-pass corner (Hz) for this surface's height
    fc: f64,
    /// a representative distance (for the voices) and its gains
    gd: f64,
    gi: f64,
    bin: u8,
}

pub struct Tables {
    pub open: [f64; 11],
    pub cabin: [f64; 11],
    pub shelter: [f64; 11],
    pub glass_g: f64,
    pub body_g: f64,
}

impl Tables {
    pub fn shipped() -> Tables {
        Tables { open: LEVEL_OPEN, cabin: LEVEL_CABIN, shelter: LEVEL_SHELTER, glass_g: GLASS_G, body_g: BODY_G }
    }
}

pub struct LiveRainB {
    tn: rainb::Tune,
    tab: Tables,
    pub tap: Tap,
    grains: Vec<Grain>,
    /// mean octave energies of each (surface, band) grain group: (dipole, every way)
    oct_e: Vec<[[f64; NO]; 2]>,
    voices: Vec<Voice>,
    free: Vec<u32>,
    dropped: usize,
    rng: NpRng,
    target: Controls,
    // smoothed state
    lr: f64,
    sh: f64,
    cab: f64,
    vol: f64,
    gust: f64,
    gust_v: f64,
    fl_near: f64,
    fl_far: f64,
    // streams for the current rate
    st_rate: f64,
    since_restream: u32,
    ev_rate: Vec<f64>,     // distinct drops a second per (ring, band)
    ev_gain: Vec<f64>,     // their gain (sqrt of the drops each stands for)
    ev_rate_m: Vec<f64>,   // metal hits a second per (ring, band), and their gain
    ev_gain_m: Vec<f64>,
    metal_cap: f64,
    roofclick: f64,
    capx: f64,
    kmax: f64,
    fl_hz: f64,
    wash_near: [f64; NO],  // wash power per second per octave, near rings (< 4 m), open
    wash_far: [f64; NO],
    wash_near_sh: [f64; NO], // the same with the shelter's strip taken out
    wash_far_sh: [f64; NO],
    drip_rate: Vec<f64>,   // canopy drips a second per ring
    drip_gain: Vec<f64>,
    dripk: f64,
    runoff_rate: f64,
    roof_rate: [f64; 7],
    puddle: f64,
    ring_g: Vec<[RingGain; N_SURF]>,
    ring_excl: [f64; 7],
    // the wash's band-passes
    wash: WashBank,
    wv: [f64; NO],
    wgain: [f64; NO],
    // event bins
    bins: Vec<[f32; CH]>,
    bin_a: [f32; NB],
    bin_s: [f32; NB],
    used: [bool; NB],
    // cabin
    roof: ModeBank<16>,
    force: [f64; CH],
    liner: Biquad,
    cfx: CabinFx,
    wash_u: [f32; CH],
    floor_std: f64,
    cfx_fg: f64,
    thump_force: [f64; CH],
    lf: Biquad,
    body1: Biquad,
    // shelter reflection
    refl: Vec<f32>,
    refl_at: usize,
    refl_s: f32,
    // output
    chunk: [f32; CH],
    chunk_pos: usize,
    chunk_live: bool,
    level_prev: (f64, f64),
    idle: bool,
}

fn poisson(r: &mut NpRng, lam: f64) -> usize {
    if lam <= 0.0 {
        0
    } else if lam < 9.0 {
        r.poisson_small(lam) as usize
    } else {
        (lam + m::sqrt(lam) * r.standard_normal()).round().max(0.0) as usize
    }
}

fn lerp(a: f64, b: f64, t: f64) -> f64 {
    a + (b - a) * t.clamp(0.0, 1.0)
}

/// A value from a table by log rate (held at the ends).
fn by_rate(tab: &[f64; 11], r: f64) -> f64 {
    let x = m::log10(r.max(1e-3));
    let xs: Vec<f64> = LEVEL_RATES.iter().map(|v| m::log10(*v)).collect();
    if x <= xs[0] {
        return tab[0];
    }
    for i in 0..10 {
        if x <= xs[i + 1] {
            return lerp(tab[i], tab[i + 1], (x - xs[i]) / (xs[i + 1] - xs[i]));
        }
    }
    tab[10]
}

/// Puddles and runoff by rate (the offline scenes': 0.04 / 0.10 / 0.30 standing water at 0.7 / 5 / 35 mm/h).
fn puddle_share(r: f64) -> f64 {
    let x = m::log10(r.max(0.01));
    if x <= m::log10(5.0) {
        lerp(0.04, 0.10, (x - m::log10(0.7)) / (m::log10(5.0) - m::log10(0.7)))
    } else {
        lerp(0.10, 0.30, (x - m::log10(5.0)) / (m::log10(35.0) - m::log10(5.0)))
    }
}

fn runoff_share(r: f64) -> f64 {
    let x = m::log10(r.max(0.01));
    if x <= m::log10(5.0) {
        lerp(0.0, 0.2, (x - m::log10(0.7)) / (m::log10(5.0) - m::log10(0.7)))
    } else {
        lerp(0.2, 1.0, (x - m::log10(5.0)) / (m::log10(35.0) - m::log10(5.0)))
    }
}

/// A grain part cut where what is left holds under 1e-5 of its energy, at most GRAIN_MAX, with a short fade.
fn trim(x: &[f64]) -> Vec<f32> {
    let total: f64 = x.iter().map(|v| v * v).sum();
    if total <= 0.0 {
        return Vec::new();
    }
    let mut tail = 0.0;
    let mut cut = x.len();
    for (i, v) in x.iter().enumerate().rev() {
        tail += v * v;
        if tail > 1e-4 * total {
            cut = i + 1;
            break;
        }
    }
    let cut = cut.min(GRAIN_MAX);
    let fade = 64.min(cut);
    (0..cut)
        .map(|i| {
            let f = if i + fade >= cut { (cut - i) as f64 / fade as f64 } else { 1.0 };
            (x[i] * f) as f32
        })
        .collect()
}

/// Energy (sum of squares) of x in each octave band round OCT.
fn octave_energy(x: &[f32]) -> [f64; NO] {
    let mut out = [0.0; NO];
    if x.is_empty() {
        return out;
    }
    let n = dsp::next_pow2(x.len().max(64));
    let mut c: Vec<dsp::C> = (0..n).map(|i| dsp::C::new(x.get(i).map(|v| *v as f64).unwrap_or(0.0), 0.0)).collect();
    dsp::fft(&mut c, false);
    for k in 1..n / 2 {
        let f = k as f64 * FS / n as f64;
        let p = 2.0 * (c[k].re * c[k].re + c[k].im * c[k].im) / n as f64;
        let o = (m::log2(f / OCT[0]) + 0.5).floor();
        if o >= 0.0 && (o as usize) < NO {
            out[o as usize] += p;
        }
    }
    out
}

impl LiveRainB {
    pub fn new(seed: u64) -> LiveRainB {
        LiveRainB::with_tables(seed, Tables::shipped())
    }

    pub fn with_tables(seed: u64, tab: Tables) -> LiveRainB {
        let tn = rainb::Tune::get();
        let panels = rainb::object_panels(seed, 3);
        let pref = rainb::p_ref();
        let mut r = NpRng::new(&[seed, 0x4c49_5645]);
        let nb = rainb::BANDS.len();
        let mut grains = Vec::new();
        let mut oct_e = Vec::new();
        let push_group = |grains: &mut Vec<Grain>, oct_e: &mut Vec<[[f64; NO]; 2]>, evs: Vec<rainb::Ev>| {
            let mut acc = [[0.0; NO]; 2];
            for ev in evs {
                let g = Grain { dip: trim(&ev.dip), iso: trim(&ev.iso) };
                let (ed, ei) = (octave_energy(&g.dip), octave_energy(&g.iso));
                for o in 0..NO {
                    acc[0][o] += ed[o] / VARIANTS as f64;
                    acc[1][o] += ei[o] / VARIANTS as f64;
                }
                grains.push(g);
            }
            oct_e.push(acc);
        };
        // rain drops: 7 surfaces x 7 bands x VARIANTS
        for s in 0..N_SURF {
            let surf = match s {
                0 => Surf::Road,
                1 => Surf::Puddle,
                2 => Surf::Soil,
                3 => Surf::Leaf,
                k => Surf::Metal(k - 4),
            };
            for &(d0, d1) in rainb::BANDS.iter() {
                let evs: Vec<rainb::Ev> = (0..VARIANTS)
                    .map(|_| {
                        let d = r.uniform(d0, d1);
                        rainb::drop_event(&mut r, &tn, pref, surf, d, rainb::terminal_v(d), 0.5, &panels)
                    })
                    .collect();
                push_group(&mut grains, &mut oct_e, evs);
            }
        }
        // canopy drips: road, puddle, soil (4-5.5 mm from 2-10 m)
        for surf in [Surf::Road, Surf::Puddle, Surf::Soil] {
            let evs: Vec<rainb::Ev> = (0..VARIANTS)
                .map(|_| {
                    let d = r.uniform(4.0, 5.5);
                    let v = rainb::fall_speed(d, r.uniform(2.0, 10.0));
                    rainb::drop_event(&mut r, &tn, pref, surf, d, v, 0.5, &panels)
                })
                .collect();
            push_group(&mut grains, &mut oct_e, evs);
        }
        // runoff bubbles
        let evs: Vec<rainb::Ev> = (0..VARIANTS)
            .map(|_| {
                let a = r.lognormal(m::ln(1.8e-3), 0.55).clamp(0.5e-3, 6e-3);
                let mut ev = rainb::Ev::default();
                rainb::bubble(&mut ev.iso, 0, a, 0.05 * pref * m::sqrt(a / 1e-3));
                ev
            })
            .collect();
        push_group(&mut grains, &mut oct_e, evs);
        let _ = nb;
        // ring gains per surface class: ground (z 0), leaf (canopy 4-12 m: 8), metal (1 m)
        let air = rainb::AirTable::new();
        let mut ring_g = Vec::new();
        let mut ring_excl = [0.0; 7];
        for (ri, &(r0, r1)) in rainb::RINGS.iter().enumerate() {
            let mut row = [RingGain::default(); N_SURF];
            for s in 0..N_SURF {
                let (zs, ground) = match s {
                    S_LEAF => (8.0, false),
                    4..=6 => (1.0, false),
                    _ => (0.0, true),
                };
                let mut gd2 = 0.0;
                let mut gi2 = 0.0;
                let k = 64;
                for j in 0..k {
                    // area-uniform radii
                    let u = (j as f64 + 0.5) / k as f64;
                    let rho = m::sqrt(r0 * r0 + u * (r1 * r1 - r0 * r0));
                    let dz = rainb::OBS_H - zs;
                    let rr = m::sqrt(rho * rho + dz * dz).max(0.3);
                    let ct = (dz.abs() / rr).min(1.0);
                    gd2 += ((tn.iso + (1.0 - tn.iso) * ct) / rr).powi(2) / k as f64;
                    gi2 += 1.0 / (rr * rr) / k as f64;
                }
                let rho = m::sqrt(0.5 * (r0 * r0 + r1 * r1));
                let dz = rainb::OBS_H - zs;
                let rr = m::sqrt(rho * rho + dz * dz).max(0.3);
                let mut fc = air.fc(rr);
                if ground && rho > 4.0 {
                    fc = fc.min((14000.0 * m::pow(4.0 / rho, 0.6)).clamp(1200.0, 16000.0));
                }
                row[s] = RingGain { gd2, gi2, fc, gd: m::sqrt(gd2), gi: m::sqrt(gi2), bin: rainb::bucket_of(fc) as u8 };
            }
            ring_g.push(row);
            // the share of the ring under the shelter's strip |y| < SH_HALF
            let mut inside = 0usize;
            let k: usize = 4096;
            for j in 0..k {
                let u = ((j * 2654435761) % k) as f64 / k as f64;
                let ph = (j as f64 + 0.5) / k as f64 * m::TAU;
                let rho = m::sqrt(r0 * r0 + u * (r1 * r1 - r0 * r0));
                if (rho * m::sin(ph)).abs() < SH_HALF {
                    inside += 1;
                }
            }
            ring_excl[ri] = inside as f64 / k as f64;
        }
        let mut bin_a = [1.0f32; NB];
        for (k, a) in bin_a.iter_mut().enumerate().take(rainb::BUCKETS) {
            *a = dsp::onepole_coef(FS, rainb::BUCKET_F0 * m::pow(2.0, k as f64 / 3.0)) as f32;
        }
        // the wash's octave band-passes and their noise gains (sum of h^2)
        let mut wbp = Vec::new();
        let mut wv = [0.0; NO];
        for (o, &f) in OCT.iter().enumerate() {
            let fc = f.min(0.42 * FS);
            let b = Biquad::bandpass(FS, fc, 1.414);
            let mut t = b;
            let mut s = 0.0;
            for i in 0..16384 {
                let y = t.tick(if i == 0 { 1.0 } else { 0.0 });
                s += y * y;
            }
            wv[o] = s;
            wbp.push(b);
        }
        // the roof's 24 strongest radiating modes
        let p = rainb::panel(1.3, 1.45, 62.0, 0.03, 3000.0);
        let mut md: Vec<(f64, f64, f64)> = p.modes.iter().map(|&(f, q, rad, _, _)| (f, q, rad)).collect();
        md.sort_by(|a, b| b.2.partial_cmp(&a.2).unwrap().then(a.0.partial_cmp(&b.0).unwrap()));
        let roof = md
            .iter()
            .take(env_f("modes", 16.0) as usize)
            .map(|&(f, q, rad)| {
                let w = m::TAU * f / FS;
                let rr = m::exp(-m::PI * f / q / FS);
                (rad * m::sin(w), 2.0 * rr * m::cos(w), rr * rr)
            })
            .collect::<Vec<_>>();
        let roof = ModeBank::new(&roof);
        let c = Controls { rain_mm_h: 0.0, shelter: 1.0, cabin: 0.0, volume: 1.0, muted: false };
        LiveRainB {
            tn,
            tab,
            tap: Tap::Mix,
            grains,
            oct_e,
            voices: vec![Voice::default(); VOICES],
            free: (0..VOICES as u32).rev().collect(),
            dropped: 0,
            rng: NpRng::new(&[seed, 0x5255_4e00]),
            target: c,
            lr: m::log10(0.005),
            sh: 0.0,
            cab: 0.0,
            vol: 1.0,
            gust: 1.0,
            gust_v: 0.0,
            fl_near: 0.0,
            fl_far: 0.0,
            st_rate: -1.0,
            since_restream: 0,
            ev_rate: vec![0.0; rainb::RINGS.len() * rainb::BANDS.len()],
            ev_gain: vec![1.0; rainb::RINGS.len() * rainb::BANDS.len()],
            ev_rate_m: vec![0.0; rainb::RINGS.len() * rainb::BANDS.len()],
            ev_gain_m: vec![1.0; rainb::RINGS.len() * rainb::BANDS.len()],
            metal_cap: env_f("metalcap", METAL_CAP),
            roofclick: env_f("roofclick", ROOF_CLICK),
            capx: env_f("capx", CAP_X),
            kmax: env_f("kmax", K_MAX),
            fl_hz: env_f("fl_hz", FLICKER_HZ),
            wash_near: [0.0; NO],
            wash_far: [0.0; NO],
            wash_near_sh: [0.0; NO],
            wash_far_sh: [0.0; NO],
            drip_rate: vec![0.0; rainb::RINGS.len()],
            drip_gain: vec![1.0; rainb::RINGS.len()],
            dripk: env_f("dripk", DRIP_K),
            runoff_rate: 0.0,
            roof_rate: [0.0; 7],
            puddle: 0.1,
            ring_g,
            ring_excl,
            wash: WashBank::new(&wbp),
            wv,
            wgain: [0.0; NO],
            bins: vec![[0.0f32; CH]; NB],
            bin_a,
            bin_s: [0.0; NB],
            used: [false; NB],
            roof,
            force: [0.0; CH],
            liner: Biquad::lowpass(FS, rainb::Tune::get().liner_hz, 0.6),
            cfx: CabinFx::new(),
            wash_u: [0.0; CH],
            floor_std: 0.0,
            cfx_fg: 0.0,
            thump_force: [0.0; CH],
            lf: Biquad::lowshelf(FS, 150.0, rainb::Tune::get().cabin_lf_db),
            // the outside through the body: one 2nd-order low-pass (~mass law above 400 Hz)
            body1: Biquad::lowpass(FS, 400.0, 0.6),
            refl: vec![0.0; 1024],
            refl_at: 0,
            refl_s: 0.0,
            chunk: [0.0; CH],
            chunk_pos: CH,
            chunk_live: false,
            level_prev: (0.0, 0.0),
            idle: true,
        }
    }

    pub fn set(&mut self, c: Controls) {
        self.target = c;
    }

    pub fn active_voices(&self) -> usize {
        self.voices.iter().filter(|v| v.active).count()
    }

    pub fn dropped(&self) -> usize {
        self.dropped
    }

    /// The surface probabilities of a drop in ring ri (as rainb.rs field()): leaf, metal, puddle, soil, road.
    fn surf_p(&self, ri: usize) -> [f64; N_SURF] {
        let (r0, r1) = rainb::RINGS[ri];
        let mid = 0.5 * (r0 + r1);
        let canopy = if mid > 2.0 { 0.15 } else { 0.0 };
        let metal = if mid > 3.0 && mid < 25.0 { 0.04 } else { 0.0 };
        let rest = 1.0 - canopy;
        let pud = self.puddle;
        let soil = 0.15;
        let mut p = [0.0; N_SURF];
        p[S_LEAF] = canopy;
        for k in 4..7 {
            p[k] = rest * metal / 3.0;
        }
        p[1] = rest * pud;
        p[2] = rest * soil;
        p[0] = rest * (1.0 - metal - pud - soil).max(0.0);
        p
    }

    /// Streams and wash powers for a rate.
    fn restream(&mut self, r: f64) {
        let dsd = rainb::Dsd::new(r);
        self.puddle = puddle_share(r);
        let nb = rainb::BANDS.len();
        let mut wn = [0.0; NO];
        let mut wf = [0.0; NO];
        let mut wns = [0.0; NO];
        let mut wfs = [0.0; NO];
        for (ri, &(r0, r1)) in rainb::RINGS.iter().enumerate() {
            let area = m::PI * (r1 * r1 - r0 * r0);
            let p0 = self.surf_p(ri);
            let pm = p0[4] + p0[5] + p0[6];
            // the other surfaces' shares among themselves
            let mut p = p0;
            for k in 0..N_SURF {
                p[k] = if (4..7).contains(&k) { 0.0 } else { p0[k] / (1.0 - pm) };
            }
            for (bi, &(d0, d1)) in rainb::BANDS.iter().enumerate() {
                let all = dsd.flux(d0, d1) * area;
                // metal (parked cars, signs): its own stream, at most METAL_CAP hits a second, each standing for as
                // many as it must (sqrt(k) louder: the offline thinning, whose rare loud tinks are what the real
                // takes' distinct events sound like); never in the wash
                let rm = all * pm;
                let capm = self.metal_cap;
                let (evm, km) = if rm <= capm { (rm, 1.0) } else { (capm, rm / capm) };
                self.ev_rate_m[ri * nb + bi] = evm;
                self.ev_gain_m[ri * nb + bi] = m::sqrt(km);
                let rate = all - rm;
                // distinct drops up to the cap; past it each stands for up to KMAX drops (sqrt(k) louder, as the
                // offline thinning), and only what is left over goes to the wash
                let cap = CAPS[ri] * self.capx;
                let (ev, k) = if rate <= cap { (rate, 1.0) } else { (cap, (rate / cap).min(self.kmax)) };
                self.ev_rate[ri * nb + bi] = ev;
                self.ev_gain[ri * nb + bi] = m::sqrt(k);
                let rem = rate - ev * k;
                if rem <= 0.0 {
                    continue;
                }
                for s in 0..N_SURF {
                    if p[s] <= 0.0 {
                        continue;
                    }
                    let g = self.ring_g[ri][s];
                    let e = &self.oct_e[s * nb + bi];
                    for o in 0..NO {
                        let h2 = 1.0 / (1.0 + (OCT[o] / g.fc).powi(2));
                        let w = rem * p[s] * (e[0][o] * g.gd2 + e[1][o] * g.gi2) * h2;
                        if r0 < 4.0 {
                            wn[o] += w;
                            wns[o] += w * (1.0 - self.ring_excl[ri]);
                        } else {
                            wf[o] += w;
                            wfs[o] += w * (1.0 - self.ring_excl[ri]);
                        }
                    }
                }
            }
        }
        // canopy drips (rings to 60 m) and runoff, as distinct events
        let vol_flux = r / 3.6e6;
        let dvol = m::PI / 6.0 * m::pow(4.8e-3, 3.0);
        let gdrip = m::undb(self.tn.drip_db).powi(2);
        for (ri, &(r0, r1)) in rainb::RINGS.iter().enumerate() {
            let rate = if ri < 5 { vol_flux * m::PI * (r1 * r1 - r0 * r0) * 0.15 * self.tn.drip_frac / dvol } else { 0.0 };
            // distinct drips up to the ring's cap, the rest into the wash (the drip grains' energies: soil 0.3,
            // puddle 0.1 + the puddle share, road the rest)
            let cap = DRIP_CAPS[ri];
            let (ev, k) = if rate <= cap { (rate, 1.0) } else { (cap, (rate / cap).min(self.dripk)) };
            self.drip_rate[ri] = ev;
            self.drip_gain[ri] = m::sqrt(k);
            let rem = rate - ev * k;
            if rem > 0.0 {
                let ps = [(0usize, (0.6 - self.puddle).max(0.0)), (1, 0.1 + self.puddle), (2, 0.3)];
                for &(s, pk) in ps.iter() {
                    let g = self.ring_g[ri][s];
                    let e = &self.oct_e[N_SURF * nb + s];
                    for o in 0..NO {
                        let h2 = 1.0 / (1.0 + (OCT[o] / g.fc).powi(2));
                        let w = rem * pk * gdrip * (e[0][o] * g.gd2 + e[1][o] * g.gi2) * h2;
                        if r0 < 4.0 {
                            wn[o] += w;
                            wns[o] += w * (1.0 - self.ring_excl[ri]);
                        } else {
                            wf[o] += w;
                            wfs[o] += w * (1.0 - self.ring_excl[ri]);
                        }
                    }
                }
            }
        }
        self.runoff_rate = 2.0 * 200.0 * runoff_share(r);
        for (bi, &(d0, d1)) in rainb::BANDS.iter().enumerate() {
            self.roof_rate[bi] = dsd.flux(d0, d1) * 1.3 * 1.45;
        }
        // the roof's small drops (under thump_d) as a noise floor of the same power: E[(D^3 V 1e-3)^2] per hit
        let mut pw = 0.0;
        for (bi, &(d0, d1)) in rainb::BANDS.iter().enumerate() {
            let k = 16;
            for j in 0..k {
                let d = d0 + (d1 - d0) * (j as f64 + 0.5) / k as f64;
                if d >= self.cfx.thump_d {
                    continue;
                }
                let imp = d * d * d * rainb::terminal_v(d) * 1e-3;
                pw += self.roof_rate[bi] / k as f64 * imp * imp;
            }
        }
        self.floor_std = m::sqrt(pw / FS);
        self.wash_near = wn;
        self.wash_far = wf;
        self.wash_near_sh = wns;
        self.wash_far_sh = wfs;
        self.st_rate = r;
    }

    fn start(&mut self, grain: usize, gd: f64, gi: f64, bin: u8, delay: usize) {
        // a free voice from the stack (O(1): a scan for a free slot cost more than the mixing at 5000 starts a second)
        if let Some(i) = self.free.pop() {
            self.voices[i as usize] =
                Voice { grain: grain as u32, pos: 0, delay: delay as u32, gd: gd as f32, gi: gi as f32, bin, active: true };
        } else {
            self.dropped += 1;
        }
    }

    /// Mix out.len() samples into out (any block size: the engine runs in fixed chunks underneath).
    pub fn process(&mut self, out: &mut [f32]) {
        let mut i = 0;
        while i < out.len() {
            if self.chunk_pos == CH {
                self.render_chunk();
                self.chunk_pos = 0;
            }
            let k = (CH - self.chunk_pos).min(out.len() - i);
            if self.chunk_live {
                for j in 0..k {
                    out[i + j] += self.chunk[self.chunk_pos + j];
                }
            }
            self.chunk_pos += k;
            i += k;
        }
    }

    fn render_chunk(&mut self) {
        let dt = CH as f64 / FS;
        let tg = self.target;
        let dry_target = tg.rain_mm_h < rainb_live_dry();
        let lt = m::log10(if dry_target { 0.005 } else { tg.rain_mm_h });
        self.lr += (lt - self.lr) * (1.0 - m::exp(-dt / RATE_TAU_S));
        let r = m::pow(10.0, self.lr);
        let step = dt / CROSS_S;
        let towards = |x: f64, t: f64| if x < t { (x + step).min(t) } else { (x - step).max(t) };
        self.sh = towards(self.sh, (1.0 - tg.shelter).clamp(0.0, 1.0));
        self.cab = towards(self.cab, tg.cabin.clamp(0.0, 1.0));
        let want_vol = if tg.muted { 0.0 } else { tg.volume };
        let dry = r < rainb_live_dry();
        if dry && self.idle && want_vol >= 0.0 {
            self.chunk_live = false;
            self.vol = want_vol;
            return;
        }
        self.chunk_live = true;
        // the streams follow the rate in steps of at most 2 % and at most every RESTREAM_CHUNKS chunks (the drop-size
        // tables are the costly part; the wash gains are ramped across each chunk, the steps are 0.17 dB)
        self.since_restream += 1;
        if !dry && (self.st_rate < 0.0 || ((r / self.st_rate - 1.0).abs() > 0.02 && self.since_restream >= RESTREAM_CHUNKS)) {
            self.since_restream = 0;
            self.restream(r);
        }
        // gusts and clustering (fixed dt per chunk)
        self.gust_v += (self.rng.standard_normal() * 0.6 * m::sqrt(dt) - 0.3 * (self.gust - 1.0) * dt) * 1.0;
        self.gust = (self.gust + self.gust_v * dt).clamp(0.6, 1.4);
        let th = 1.0 - m::exp(-dt * m::TAU * self.fl_hz);
        let (fnr, ffr) = (self.tn.fast_near, self.tn.fast_far);
        self.fl_near += -th * self.fl_near + fnr * m::sqrt(2.0 * th) * self.rng.standard_normal();
        self.fl_far += -th * self.fl_far + ffr * m::sqrt(2.0 * th) * self.rng.standard_normal();
        let gn = self.gust * m::exp(self.fl_near - 0.5 * fnr * fnr);
        let gf = self.gust * m::exp(self.fl_far - 0.5 * ffr * ffr);
        // the car roof averages the clustering over its 1.9 m^2 (offline fast_cabin): the near flicker, scaled
        let fcb = self.tn.fast_cabin;
        let gcab = self.gust * m::exp(self.fl_near * fcb / fnr.max(1e-9) - 0.5 * fcb * fcb);
        let fade = ((r - rainb_live_dry()) / 0.03).clamp(0.0, 1.0);
        for k in 0..NB {
            if self.used[k] {
                self.bins[k] = [0.0; CH];
            }
        }
        self.force = [0.0; CH];
        self.thump_force = [0.0; CH];
        let nb = rainb::BANDS.len();
        let sh = self.sh;
        if !dry {
            // fully inside the car the outside is only heard through the body (a 400 Hz low-pass): its drops are left
            // out there (the wash carries it; the cabin's level is calibrated so): the cabin's CPU budget
            let inside = self.cab >= 1.0 && self.tap != Tap::OpenRaw;
            if !inside {
            // distinct drops
            for ri in 0..rainb::RINGS.len() {
                let g_fl = if rainb::RINGS[ri].0 < 4.0 { gn } else { gf };
                let p0 = self.surf_p(ri);
                let pm = p0[4] + p0[5] + p0[6];
                let mut p = p0;
                for k in 0..N_SURF {
                    p[k] = if (4..7).contains(&k) { 0.0 } else { p0[k] / (1.0 - pm) };
                }
                for bi in 0..nb {
                    // metal hits
                    let lm = self.ev_rate_m[ri * nb + bi] * dt * g_fl;
                    let cm = poisson(&mut self.rng, lm);
                    for _ in 0..cm {
                        let off = self.rng.integers(0, CH as i64) as usize;
                        let s = 4 + self.rng.integers(0, 3) as usize;
                        let vi = self.rng.integers(0, VARIANTS as i64) as usize;
                        let (r0, r1) = rainb::RINGS[ri];
                        let rho = m::sqrt(r0 * r0 + self.rng.random() * (r1 * r1 - r0 * r0));
                        let phi = self.rng.random();
                        if sh > 0.0 && (rho * m::sin(phi * m::TAU)).abs() < SH_HALF && self.rng.random() < sh {
                            continue;
                        }
                        let g = self.ring_g[ri][s];
                        let eg = self.ev_gain_m[ri * nb + bi];
                        self.start((s * nb + bi) * VARIANTS + vi, g.gd * eg, g.gi * eg, g.bin, off);
                    }
                    let lam = self.ev_rate[ri * nb + bi] * dt * g_fl;
                    let cnt = poisson(&mut self.rng, lam);
                    for _ in 0..cnt {
                        let off = self.rng.integers(0, CH as i64) as usize;
                        let phi = self.rng.random();
                        let u = self.rng.random();
                        let vi = self.rng.integers(0, VARIANTS as i64) as usize;
                        // the shelter's strip, with its weight
                        let (r0, r1) = rainb::RINGS[ri];
                        let rho = m::sqrt(r0 * r0 + self.rng.random() * (r1 * r1 - r0 * r0));
                        if sh > 0.0 && (rho * m::sin(phi * m::TAU)).abs() < SH_HALF && self.rng.random() < sh {
                            continue;
                        }
                        let mut acc = 0.0;
                        let mut s = 0;
                        for (k, pk) in p.iter().enumerate() {
                            acc += pk;
                            if u < acc {
                                s = k;
                                break;
                            }
                        }
                        let g = self.ring_g[ri][s];
                        let grain = (s * nb + bi) * VARIANTS + vi;
                        let eg = self.ev_gain[ri * nb + bi];
                        self.start(grain, g.gd * eg, g.gi * eg, g.bin, off);
                    }
                }
            }
            // canopy drips
            for ri in 0..rainb::RINGS.len() {
                let cnt = poisson(&mut self.rng, self.drip_rate[ri] * dt * gf * (1.0 - sh * self.ring_excl[ri]));
                for _ in 0..cnt {
                    let off = self.rng.integers(0, CH as i64) as usize;
                    let w = self.rng.random();
                    let s = if w < 0.3 { 2 } else if w < 0.4 + self.puddle { 1 } else { 0 };
                    let g = self.ring_g[ri][s];
                    let grain = ((N_SURF * nb) + s) * VARIANTS + self.rng.integers(0, VARIANTS as i64) as usize;
                    let gain = m::undb(self.tn.drip_db) * self.drip_gain[ri];
                    self.start(grain, g.gd * gain, g.gi * gain, g.bin, off);
                }
            }
            // the shelter's edges dripping (drips 6 m to either side, within 15 m along)
            if sh > 0.0 {
                let cnt = poisson(&mut self.rng, sh * 1.5 * r * 30.0 * dt);
                for _ in 0..cnt {
                    let off = self.rng.integers(0, CH as i64) as usize;
                    let x = self.rng.uniform(-15.0, 15.0);
                    let rho: f64 = m::sqrt(x * x + SH_HALF * SH_HALF);
                    let ri = rainb::RINGS.iter().position(|&(a, b)| rho >= a && rho < b).unwrap_or(3);
                    let s = if self.rng.random() < 0.4 { 1 } else { 0 };
                    let g = self.ring_g[ri][s];
                    let grain = ((N_SURF * nb) + s) * VARIANTS + self.rng.integers(0, VARIANTS as i64) as usize;
                    self.start(grain, g.gd * 0.7, g.gi * 0.7, g.bin, off);
                }
            }
            // runoff (4-10 m)
            let cnt = poisson(&mut self.rng, self.runoff_rate * dt * gf);
            for _ in 0..cnt {
                let off = self.rng.integers(0, CH as i64) as usize;
                let g = self.ring_g[2][0];
                let grain = ((N_SURF * nb) + 3) * VARIANTS + self.rng.integers(0, VARIANTS as i64) as usize;
                let lv = self.rng.lognormal(0.0, 0.6);
                self.start(grain, 0.0, g.gi * lv, g.bin, off);
            }
            }
            // the car's roof and windscreen
            if self.cab > 0.0 {
                for bi in 0..nb {
                    let (d0, d1) = rainb::BANDS[bi];
                    let cnt = poisson(&mut self.rng, self.roof_rate[bi] * dt * gcab);
                    for _ in 0..cnt {
                        let d = self.rng.uniform(d0, d1);
                        let v = rainb::terminal_v(d);
                        let at = self.rng.integers(0, CH as i64) as usize;
                        if d >= self.cfx.thump_d {
                            self.force[at] += d * d * d * v * 1e-3;
                            self.thump_force[at] += d * d * d * v * 1e-3;
                        }
                        // the hit's own click from the panel round it (the near field the 16 modes leave out; offline roofhf):
                        // into the windscreen's bin, a dense floor under the ticks
                        let gi = bi * VARIANTS + self.rng.integers(0, VARIANTS as i64) as usize;
                        self.start(gi, self.roofclick, 0.0, GLASS_BIN as u8, at);
                    }
                    // windscreen: 1.2 m^2 at 0.9 m, road grains' clicks (dipole on axis)
                    let cnt = poisson(&mut self.rng, self.roof_rate[bi] * (1.2 / 1.885) * dt * gcab);
                    for _ in 0..cnt {
                        let at = self.rng.integers(0, CH as i64) as usize;
                        let gi = bi * VARIANTS + self.rng.integers(0, VARIANTS as i64) as usize;
                        // a voice into the windscreen's own bin (0.9 m away, the screen's slope ~0.8: the dipole only)
                        self.start(gi, 0.8 / 0.9, 0.0, GLASS_BIN as u8, at);
                    }
                }
            }
        }
        // voices into their bins
        let mut any = false;
        for vi in 0..self.voices.len() {
            let v = self.voices[vi];
            if !v.active {
                continue;
            }
            any = true;
            let g = &self.grains[v.grain as usize];
            let start = v.delay as usize;
            let pos = v.pos as usize;
            // (a voice lasts only as long as the parts it plays: a windscreen tick is the dipole click alone)
            let len = (if v.gd != 0.0 { g.dip.len() } else { 0 }).max(if v.gi != 0.0 { g.iso.len() } else { 0 });
            let k = len.saturating_sub(pos).min(CH - start);
            let bin = &mut self.bins[v.bin as usize];
            self.used[v.bin as usize] = true;
            if pos < g.dip.len() && v.gd != 0.0 {
                let kd = (g.dip.len() - pos).min(k);
                for (o, &x) in bin[start..start + kd].iter_mut().zip(&g.dip[pos..pos + kd]) {
                    *o += x * v.gd;
                }
            }
            if pos < g.iso.len() {
                let ki = (g.iso.len() - pos).min(k);
                for (o, &x) in bin[start..start + ki].iter_mut().zip(&g.iso[pos..pos + ki]) {
                    *o += x * v.gi;
                }
            }
            let nv = &mut self.voices[vi];
            nv.pos = (pos + k) as u32;
            nv.delay = 0;
            if pos + k >= len {
                nv.active = false;
                self.free.push(vi as u32);
            }
        }
        // the open air: bins through their low-passes, plus the wash
        let mut open = [0.0f32; CH];
        let mut ringing = false;
        for kb in 0..NB {
            if !self.used[kb] {
                continue;
            }
            if kb == GLASS_BIN {
                if !any {
                    self.used[kb] = false;
                }
                continue;
            }
            let a = self.bin_a[kb];
            let mut s = self.bin_s[kb];
            if kb == OPEN_BIN {
                for j in 0..CH {
                    open[j] += self.bins[kb][j];
                }
            } else {
                for j in 0..CH {
                    s += a * (self.bins[kb][j] - s);
                    open[j] += s;
                }
            }
            self.bin_s[kb] = s;
            if s.abs() > 1e-9 {
                ringing = true;
            } else if !any {
                self.used[kb] = false;
                self.bin_s[kb] = 0.0;
            }
        }
        // wash gains: power per octave (near x near flicker + far x far flicker), the shelter's strip by its weight
        let mut g_new = [0.0; NO];
        let mut wash_on = false;
        for o in 0..NO {
            let near = lerp(self.wash_near[o], self.wash_near_sh[o], sh);
            let far = lerp(self.wash_far[o], self.wash_far_sh[o], sh);
            let p = if dry { 0.0 } else { near * gn + far * gf };
            g_new[o] = m::sqrt(p / FS / self.wv[o].max(1e-30));
            if g_new[o] > 0.0 || self.wgain[o] > 0.0 {
                wash_on = true;
            }
        }
        if wash_on {
            // the band-passes share their input: y = b0 (u - u[n-2]) - a1 y1 - a2 y2 (RBJ band-pass: b1 = 0,
            // b2 = -b0), all bands in one pass; gains ramped across the chunk
            let mut dg = [0.0; NO];
            let mut g = self.wgain;
            for o in 0..NO {
                dg[o] = (g_new[o] - self.wgain[o]) / CH as f64;
            }
            let w = &mut self.wash;
            for j in 0..CH {
                let u = (self.rng.random() * 2.0 - 1.0) * 1.7320508075688772;
                self.wash_u[j] = u as f32;
                let d = u - w.x2;
                w.x2 = w.x1;
                w.x1 = u;
                let mut acc = 0.0;
                for o in 0..NO {
                    let y = w.b0[o] * d - w.a1[o] * w.y1[o] - w.a2[o] * w.y2[o];
                    w.y2[o] = w.y1[o];
                    w.y1[o] = y;
                    g[o] += dg[o];
                    acc += y * g[o];
                }
                open[j] += acc as f32;
            }
        }
        self.wgain = g_new;
        // the shelter's deck: one reflection (~10 ms later, 0.4 x, dulled)
        if sh > 0.0 || self.refl_s.abs() > 1e-9 {
            let a = dsp::onepole_coef(FS, 6000.0) as f32;
            let d = (0.010 * FS) as usize;
            let n = self.refl.len();
            for j in 0..CH {
                let x = open[j];
                let back = self.refl[(self.refl_at + n - d) % n];
                self.refl[self.refl_at] = x;
                self.refl_at = (self.refl_at + 1) % n;
                self.refl_s += a * (back - self.refl_s);
                open[j] += self.refl_s * (0.4 * sh) as f32;
            }
        }
        // levels: the open air (shelter interpolated in dB) and the cabin, to the game's loop loudness for the rate
        let tgt = if dry { -100.0 } else { crate::live::rain_target_lufs(r.max(0.03)) };
        let l_open = lerp(by_rate(&self.tab.open, r), by_rate(&self.tab.shelter, r) - SHELTER_DB, sh);
        let g_open = if self.tap == Tap::OpenRaw { 1.0 } else { m::undb(tgt - l_open) * fade };
        let g_cab = if self.tap == Tap::CabinRaw { 1.0 } else { m::undb(tgt - by_rate(&self.tab.cabin, r)) * fade };
        // the roof's modes over the chunk, one mode at a time (each a short recursion; no per-sample loop over modes)
        let mut roof_raw = [0.0f64; CH];
        if self.cab > 0.0 || self.tap != Tap::Mix {
            let fg = self.floor_std * self.cfx.floor_g * gcab.sqrt();
            self.cfx_fg = fg;
            let mut drive = [0.0f64; CH];
            for j in 0..CH {
                drive[j] = self.force[j] + fg * self.wash_u[j] as f64;
            }
            let tg = self.cfx.thump_g;
            self.cfx.thump.run(&self.thump_force, &mut roof_raw, tg);
            self.roof.run(&drive, &mut roof_raw, 1.0);
        }
        let (go0, gc0) = self.level_prev;
        let c = self.cab;
        let (wo, wc) = (m::cos(0.5 * m::PI * c), m::sin(0.5 * m::PI * c));
        let v0 = self.vol;
        let v1 = want_vol;
        for j in 0..CH {
            let t = (j + 1) as f64 / CH as f64;
            let go = go0 + (g_open - go0) * t;
            let gc = gc0 + (g_cab - gc0) * t;
            let o = open[j] as f64 * go;
            let mut cabin = 0.0;
            let (mut roof_v, mut glass_v, mut body_v) = (0.0, 0.0, 0.0);
            if c > 0.0 || self.tap != Tap::Mix {
                roof_v = self.lf.tick(self.liner.tick(roof_raw[j]));
                glass_v = self.cfx.glass_hp.tick(self.bins[GLASS_BIN][j] as f64);
                body_v = self.body1.tick(o);
                // the cabin's transfer: body / panel resonances on the roof and the outside
                let hf = self.cfx.hf_bp.tick(self.wash_u[j] as f64) * self.cfx_fg * self.cfx.hf_g;
                // the roof and the outside through the cabin's resonances; the ticks and the patter floor join after the
                // compressor (it evens the thumps; the ticks stay ticks, at their own lower level)
                let ticks = self.cfx.glass_g * self.tab.glass_g * glass_v + hf;
                // (tick_pre: the share of the ticks that goes through the compressor too)
                let x = self.cfx.pk2.tick(self.cfx.pk1.tick(roof_v + self.tab.body_g * body_v)) + self.cfx.tick_pre * ticks;
                // compressor: a fast envelope against the slow level (the floor): peaks over thr x slow are cut
                let cf = &mut self.cfx;
                let ax = x.abs();
                cf.env += (if ax > cf.env { cf.att } else { cf.rel }) * (ax - cf.env);
                cf.slow += cf.slow_a * (ax - cf.slow);
                let lim = cf.thr * cf.slow;
                // (2:1, the default, is a square root; other ratios pay for a pow)
                let gcmp = if cf.env > lim && lim > 0.0 {
                    if cf.ratio == 2.0 { m::sqrt(lim / cf.env) } else { m::pow(lim / cf.env, 1.0 - 1.0 / cf.ratio) }
                } else {
                    1.0
                };
                let xd = x * gcmp + (1.0 - cf.tick_pre) * ticks;
                // small-room reverb: early reflections (3.1 / 4.7 / 6.3 / 8.9 ms) and two damped combs (RT ~0.15 s)
                let tap = |k: usize| cf.line[(cf.at + CAB_LINE - k) & (CAB_LINE - 1)];
                let er = 0.5 * tap(136) + 0.4 * tap(207) + 0.32 * tap(278) + 0.25 * tap(392);
                cf.er_lp += 0.35 * (er - cf.er_lp);
                let c0 = tap(498);
                let c1 = tap(604);
                cf.comb_s[0] += 0.4 * (c0 - cf.comb_s[0]);
                cf.comb_s[1] += 0.4 * (c1 - cf.comb_s[1]);
                // (one shared line: the two feedbacks sum to 0.68 < 1, RT ~0.2 s at the low end, less above)
                cf.line[cf.at] = xd as f32 + 0.37 * cf.comb_s[0] + 0.31 * cf.comb_s[1];
                cf.at = (cf.at + 1) & (CAB_LINE - 1);
                let wet = cf.er_lp + 0.5 * (cf.comb_s[0] + cf.comb_s[1]);
                cabin = (xd + cf.rev_mix as f64 * wet as f64) * gc;
            }
            let y = match self.tap {
                Tap::Mix => (o * wo + cabin * wc) * (v0 + (v1 - v0) * t),
                Tap::OpenRaw => o,
                Tap::RoofRaw => roof_v,
                Tap::GlassRaw => glass_v,
                Tap::BodyRaw => body_v,
                Tap::CabinRaw => cabin,
            };
            // a soft ceiling for the rare big-drop thunk on the roof (above 0.7 full scale only; the bus limiter follows)
            let y = if self.tap == Tap::Mix && y.abs() > 0.7 { y.signum() * (0.7 + 0.3 * m::tanh((y.abs() - 0.7) / 0.3)) } else { y };
            self.chunk[j] = y as f32;
        }
        self.level_prev = (g_open, g_cab);
        self.vol = v1;
        // (in the car too, once the roof and the cabin's reverb have died away)
        let quiet = self.chunk.iter().all(|v| v.abs() < 1e-7);
        self.idle = dry && !any && !ringing && !wash_on && (c == 0.0 || quiet);
    }
}

/// WEATHERSYNTH_RBL="name=value,..." (tuning sweeps of the live engine's own choices).
fn env_f(name: &str, default: f64) -> f64 {
    std::env::var("WEATHERSYNTH_RBL")
        .ok()
        .and_then(|s| {
            s.split(',').find_map(|kv| {
                let mut it = kv.splitn(2, '=');
                match (it.next(), it.next()) {
                    (Some(k), Some(v)) if k.trim() == name => v.trim().parse().ok(),
                    _ => None,
                }
            })
        })
        .unwrap_or(default)
}

fn rainb_live_dry() -> f64 {
    0.02
}

/// Run the engine at fixed controls and return its integrated loudness (LUFS) over `secs` after `settle` s.
fn measure(e: &mut LiveRainB, c: Controls, settle: f64, secs: f64) -> f64 {
    e.set(c);
    let mut buf = vec![0.0f32; 4096];
    let mut n = 0usize;
    while (n as f64) < settle * FS {
        buf.iter_mut().for_each(|v| *v = 0.0);
        e.process(&mut buf);
        n += buf.len();
    }
    let mut all = Vec::with_capacity((secs * FS) as usize + 4096);
    while (all.len() as f64) < secs * FS {
        buf.iter_mut().for_each(|v| *v = 0.0);
        e.process(&mut buf);
        all.extend(buf.iter().map(|&v| v as f64));
    }
    crate::loudness::integrated(&all, FS, false)
}

fn rms_of(e: &mut LiveRainB, c: Controls, settle: f64, secs: f64) -> f64 {
    e.set(c);
    let mut buf = vec![0.0f32; 4096];
    let mut n = 0usize;
    let mut acc = 0.0;
    let mut cnt = 0usize;
    while (n as f64) < (settle + secs) * FS {
        buf.iter_mut().for_each(|v| *v = 0.0);
        e.process(&mut buf);
        if (n as f64) >= settle * FS {
            acc += buf.iter().map(|&v| (v as f64) * (v as f64)).sum::<f64>();
            cnt += buf.len();
        }
        n += buf.len();
    }
    m::sqrt(acc / cnt.max(1) as f64)
}

/// One part of the engine at fixed controls, written to a WAV (diagnostics).
pub fn tap_render(tap: &str, rate: f64, shelter: f64, cabin: f64, secs: f64, out: &str) -> Result<(), String> {
    let mut e = LiveRainB::new(1);
    e.tap = match tap {
        "mix" => Tap::Mix,
        "open" => Tap::OpenRaw,
        "roof" => Tap::RoofRaw,
        "glass" => Tap::GlassRaw,
        "body" => Tap::BodyRaw,
        "cabin" => Tap::CabinRaw,
        _ => return Err(format!("unknown tap {tap}")),
    };
    e.cab = cabin;
    e.sh = 1.0 - shelter;
    e.lr = m::log10(rate.max(0.005));
    e.set(Controls { rain_mm_h: rate, shelter, cabin, volume: 1.0, muted: false });
    let n = (secs * FS) as usize;
    let mut buf = vec![0.0f32; n];
    e.process(&mut buf);
    {
        let mut by = [0usize; NB];
        for v in e.voices.iter().filter(|v| v.active) {
            by[v.bin as usize] += 1;
        }
        eprintln!("active voices by bin: {by:?}");
    }
    let x: Vec<f64> = buf.iter().map(|&v| v as f64).collect();
    crate::wav::write_f32(out, &x, 44100).map_err(|e| e.to_string())
}

/// Measure the level tables (open, cabin, shelter by rate) and the cabin parts' gains; print them as Rust.
pub fn calibrate() -> String {
    let ctl = |r: f64, sh: f64, cab: f64| Controls { rain_mm_h: r, shelter: sh, cabin: cab, volume: 1.0, muted: false };
    let mut out = String::new();
    // 1. the open air's own loudness by rate
    let mut open = [0.0; 11];
    for (i, &r) in LEVEL_RATES.iter().enumerate() {
        let mut e = LiveRainB::with_tables(1, Tables::shipped());
        e.tap = Tap::OpenRaw;
        open[i] = measure(&mut e, ctl(r, 1.0, 0.0), 4.0, 40.0);
    }
    // 2. the shelter's (weight 1)
    let mut shelter = [0.0; 11];
    for (i, &r) in LEVEL_RATES.iter().enumerate() {
        let mut e = LiveRainB::with_tables(1, Tables::shipped());
        e.tap = Tap::OpenRaw;
        e.sh = 1.0;
        shelter[i] = measure(&mut e, ctl(r, 0.0, 0.0), 4.0, 40.0);
    }
    // 3. the cabin parts at 5 mm/h (the outside levelled), as offline: the windscreen 12 dB and the body 6 dB under
    //    the roof (rainb.rs Tune glass_db, body_db)
    let tabs = |o: [f64; 11], s: [f64; 11], g: f64, b: f64| Tables { open: o, cabin: [0.0; 11], shelter: s, glass_g: g, body_g: b };
    let part = |tap: Tap| {
        let mut e = LiveRainB::with_tables(1, tabs(open, shelter, 1.0, 1.0));
        e.tap = tap;
        e.cab = 1.0;
        rms_of(&mut e, ctl(5.0, 1.0, 1.0), 4.0, 20.0)
    };
    // (kept since the cabin effects were tuned on them, 2026-11-26: the windscreen and body balance is now CabinFx's;
    // WEATHERSYNTH_RBL="cabparts=1" measures them afresh as offline's -12 / -6 dB under the roof)
    let (glass_g, body_g) = if env_f("cabparts", 0.0) > 0.0 {
        let (rr, rg, rb) = (part(Tap::RoofRaw), part(Tap::GlassRaw), part(Tap::BodyRaw));
        let tn = rainb::Tune::get();
        (m::undb(tn.glass_db) * rr / rg.max(1e-30), m::undb(tn.body_db) * rr / rb.max(1e-30))
    } else {
        (GLASS_G, BODY_G)
    };
    // 4. the cabin's loudness by rate
    let mut cabin = [0.0; 11];
    for (i, &r) in LEVEL_RATES.iter().enumerate() {
        let mut e = LiveRainB::with_tables(1, tabs(open, shelter, glass_g, body_g));
        e.tap = Tap::CabinRaw;
        e.cab = 1.0;
        cabin[i] = measure(&mut e, ctl(r, 1.0, 1.0), 4.0, 40.0);
    }
    let fmt = |a: &[f64; 11]| a.iter().map(|v| format!("{v:.2}")).collect::<Vec<_>>().join(", ");
    out += &format!("const LEVEL_OPEN: [f64; 11] = [{}];\n", fmt(&open));
    out += &format!("const LEVEL_CABIN: [f64; 11] = [{}];\n", fmt(&cabin));
    out += &format!("const LEVEL_SHELTER: [f64; 11] = [{}];\n", fmt(&shelter));
    out += &format!("const GLASS_G: f64 = {glass_g:.6e};\nconst BODY_G: f64 = {body_g:.6e};\n");
    out
}

/// The scripted sweep, timed, written; the caller's blocks split at the phase changes (so any block size gives the
/// same control timeline). `secs_per`: each phase's length (s); `block`: the caller's block.
pub fn bench(secs_per: f64, block: usize, wav: Option<&str>, cabin_sweep: bool) -> String {
    let t_build = std::time::Instant::now();
    let mut e = LiveRainB::new(1);
    let build_s = t_build.elapsed().as_secs_f64();
    let all: [(&str, f64, f64, f64); 8] = [
        ("dry", 0.0, 1.0, 0.0),
        ("light 0.7", 0.7, 1.0, 0.0),
        ("medium 5", 5.0, 1.0, 0.0),
        ("heavy 35", 35.0, 1.0, 0.0),
        ("cabin 35", 35.0, 1.0, 1.0),
        ("cabin 5", 5.0, 1.0, 1.0),
        ("shelter 5", 5.0, 0.0, 0.0),
        ("dry again", 0.0, 1.0, 0.0),
    ];
    // the cabin's own sweep: in the car through light, medium, heavy; out into the heavy rain; back in; dry
    let cab: [(&str, f64, f64, f64); 8] = [
        ("dry", 0.0, 1.0, 1.0),
        ("cabin light 0.7", 0.7, 1.0, 1.0),
        ("cabin medium 5", 5.0, 1.0, 1.0),
        ("cabin heavy 35", 35.0, 1.0, 1.0),
        ("outside heavy 35", 35.0, 1.0, 0.0),
        ("cabin heavy again", 35.0, 1.0, 1.0),
        ("cabin medium 5", 5.0, 1.0, 1.0),
        ("dry again", 0.0, 1.0, 1.0),
    ];
    let phases = if cabin_sweep { cab } else { all };
    let per = ((secs_per * FS) as usize / CH) * CH;
    let total = per * phases.len();
    let mut out = vec![0.0f32; block];
    let mut all: Vec<f64> = Vec::with_capacity(if wav.is_some() { total } else { 0 });
    let mut cpu = vec![0.0f64; phases.len()];
    let mut maxv = vec![0usize; phases.len()];
    let mut done = 0usize;
    while done < total {
        let ph = done / per;
        let p = phases[ph];
        e.set(Controls { rain_mm_h: p.1, shelter: p.2, cabin: p.3, volume: 1.0, muted: false });
        let n = block.min(per * (ph + 1) - done);
        let o = &mut out[..n];
        o.iter_mut().for_each(|v| *v = 0.0);
        let t0 = thread_cpu_s();
        e.process(o);
        cpu[ph] += thread_cpu_s() - t0;
        maxv[ph] = maxv[ph].max(e.active_voices());
        if wav.is_some() {
            all.extend(o.iter().map(|&v| v as f64));
        }
        done += n;
    }
    if let Some(w) = wav {
        let _ = crate::wav::write_f32(w, &all, 44100);
    }
    let mut s = format!(
        "bank built in {build_s:.2} s ({} grains); block {block}; chunk {CH}; voices dropped {}\nphase\tcpu % of one core\tmax voices\n",
        e.grains.len(),
        e.dropped()
    );
    for (i, p) in phases.iter().enumerate() {
        s += &format!("{}\t{:.3}\t{}\n", p.0, 100.0 * cpu[i] / (per as f64 / FS), maxv[i]);
    }
    s
}

/// This thread's CPU time (s): /proc/thread-self/schedstat on Linux (its time on a CPU, ns), so a busy machine and a
/// niced bench do not count the time spent waiting for a core; elsewhere wall time.
fn thread_cpu_s() -> f64 {
    static T0: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();
    if let Ok(s) = std::fs::read_to_string("/proc/thread-self/schedstat") {
        if let Some(ns) = s.split_whitespace().next().and_then(|v| v.parse::<u64>().ok()) {
            return ns as f64 * 1e-9;
        }
    }
    T0.get_or_init(std::time::Instant::now).elapsed().as_secs_f64()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(block: usize, secs: f64, c: Controls) -> Vec<f32> {
        let mut e = LiveRainB::new(3);
        e.set(c);
        let n = (secs * FS) as usize;
        let mut out = vec![0.0f32; n];
        let mut i = 0;
        while i < n {
            let k = block.min(n - i);
            e.process(&mut out[i..i + k]);
            i += k;
        }
        out
    }

    /// The same samples whatever block the caller asks for (the engine runs in fixed chunks).
    #[test]
    fn any_block_size_same_output() {
        let c = Controls { rain_mm_h: 5.0, shelter: 1.0, cabin: 0.0, volume: 1.0, muted: false };
        let a = run(512, 1.5, c);
        for b in [1usize, 333, 4096] {
            let x = run(b, 1.5, c);
            assert!(a.iter().zip(&x).all(|(p, q)| p.to_bits() == q.to_bits()), "block {b}");
        }
        assert!(a.iter().any(|v| *v != 0.0));
    }

    /// Dry: nothing is added to the output (and nothing is computed).
    #[test]
    fn dry_is_silent() {
        let c = Controls { rain_mm_h: 0.0, shelter: 1.0, cabin: 0.0, volume: 1.0, muted: false };
        let x = run(512, 0.5, c);
        assert!(x.iter().all(|v| *v == 0.0));
    }
}
