//! Method B rain, live: the physical drop model (rainb.rs) as a block-by-block engine for the world server's audio
//! callback. Not wired into live-serve yet (2026-11-26: renders first, the user decides by ear).
//!
//! - **Grains:** at start-up rainb.rs's own drop events are rendered once into a bank (road, puddle, soil, leaf and
//!   three metal panels x the 7 size bands x 8 variants), each kept as its dipole part and its every-way part, so a
//!   voice can weigh the dipole by the listener's cos(theta).
//! - **Streams:** each (ring, size band) is a Poisson stream at the drop-size distribution's flux for the current rate,
//!   thinned at equal energy to at most CAP_NEAR / CAP_FAR voices a second; the big drops are never thinned.
//! - **Distance:** voices add into 1/3-octave low-pass bins (the offline model's bins): one one-pole filter per bin per
//!   sample, not per voice.
//! - **Rate flicker:** gusts (a slow random walk) and clustering (a 0.5-8 Hz log-normal flicker, faster and deeper
//!   near), as offline.
//! - **Cabin:** the roof panel's 24 strongest radiating modes driven by every hit on it (impulses), through the
//!   headliner, plus the outside through the body. **Shelter:** the drops within the shelter's half-width are left out.
//! - **Level:** the open-air mix goes through live.rs's RainCap (never louder than the game's loops at the rate).
//! - **Dry:** below 0.02 mm/h with no voice left, process() returns at once: no noise, no filters, nothing.
//! - **No allocation per block** once built (the bank, voices and scratch are sized up front).

use crate::dsp::Biquad;
use crate::live::RainCap;
use crate::m;
use crate::nprng::NpRng;
use crate::rainb::{self, Surf};

pub const FS: f64 = 44100.0;
const VARIANTS: usize = 8;
const VOICES: usize = 256;
const GRAIN_MAX: usize = 8192;
const CAP_NEAR: f64 = 400.0;
const CAP_FAR: f64 = 120.0;
const NB: usize = rainb::BUCKETS + 1; // the low-pass bins + open

struct Grain {
    dip: Vec<f32>,
    iso: Vec<f32>,
}

#[derive(Clone, Copy, Default)]
struct Voice {
    grain: u32,
    pos: u32,
    gd: f32,
    gi: f32,
    bin: u8,
    active: bool,
}

#[derive(Clone, Copy, Debug)]
pub struct Controls {
    pub rain_mm_h: f64,
    /// 1 open sky .. 0 under a shelter (the drops within its half-width left out)
    pub shelter: f64,
    /// 1 in the driver's camera
    pub cabin: f64,
    pub volume: f64,
    pub muted: bool,
}

/// One (ring, band) stream for the current rate: (events a second after thinning, gain sqrt(k)).
#[derive(Clone, Copy, Default)]
struct Stream {
    rate: f64,
    gain: f64,
}

pub struct LiveRainB {
    pub cap: RainCap,
    grains: Vec<Grain>,
    voices: Vec<Voice>,
    rng: NpRng,
    cur: Controls,
    target: Controls,
    dsd_rate: f64,
    dsd: rainb::Dsd,
    streams: Vec<Stream>,
    roof_rate: [f64; 7],
    gust: f64,
    gust_v: f64,
    flick_near: f64,
    flick_far: f64,
    bins: Vec<Vec<f32>>,
    bin_a: [f32; NB],
    bin_s: [f32; NB],
    used: [bool; NB],
    air_bin: Vec<u8>,
    roof: Vec<(f64, f64, f64, f64, f64)>,
    force: Vec<f64>,
    liner: Biquad,
    lf: Biquad,
    body1: Biquad,
    body2: Biquad,
    out_open: Vec<f32>,
    gain_s: f64,
    idle: bool,
}

/// Surfaces in the bank, in order: road, puddle, soil, leaf, metal 0..2.
const SURFS: usize = 7;

impl LiveRainB {
    pub fn new(seed: u64) -> LiveRainB {
        let tn = rainb::Tune::get();
        let panels = rainb::object_panels(seed, 3);
        let mut r = NpRng::new(&[seed, 0x4c49_5645]);
        let mut grains = Vec::with_capacity(SURFS * rainb::BANDS.len() * VARIANTS);
        let pref = rainb::p_ref();
        for s in 0..SURFS {
            let surf = match s {
                0 => Surf::Road,
                1 => Surf::Puddle,
                2 => Surf::Soil,
                3 => Surf::Leaf,
                k => Surf::Metal(k - 4),
            };
            for &(d0, d1) in rainb::BANDS.iter() {
                for _ in 0..VARIANTS {
                    let d = r.uniform(d0, d1);
                    let ev = rainb::drop_event(&mut r, &tn, pref, surf, d, rainb::terminal_v(d), 0.5, &panels);
                    grains.push(Grain { dip: trim(&ev.dip), iso: trim(&ev.iso) });
                }
            }
        }
        // the 1/3-octave bins' coefficients, and each ring's bin (ground drops, its mid distance)
        let mut bin_a = [1.0f32; NB];
        for (k, a) in bin_a.iter_mut().enumerate().take(rainb::BUCKETS) {
            *a = crate::dsp::onepole_coef(FS, rainb::BUCKET_F0 * m::pow(2.0, k as f64 / 3.0)) as f32;
        }
        let air = rainb::AirTable::new();
        let air_bin = rainb::RINGS
            .iter()
            .map(|&(r0, r1)| {
                let rho = 0.5 * (r0 + r1);
                let rr = m::sqrt(rho * rho + rainb::OBS_H * rainb::OBS_H);
                let mut fc = air.fc(rr);
                if rho > 4.0 {
                    fc = fc.min((14000.0 * m::pow(4.0 / rho, 0.6)).clamp(1200.0, 16000.0));
                }
                rainb::bucket_of(fc) as u8
            })
            .collect();
        // the roof's 24 strongest radiating modes (odd-odd, low)
        let p = rainb::panel(1.3, 1.45, 62.0, 0.03, 3000.0);
        let mut md: Vec<(f64, f64, f64)> = p.modes.iter().map(|&(f, q, rad, _, _)| (f, q, rad)).collect();
        md.sort_by(|a, b| b.2.partial_cmp(&a.2).unwrap().then(a.0.partial_cmp(&b.0).unwrap()));
        let roof = md
            .iter()
            .take(24)
            .map(|&(f, q, rad)| {
                let w = m::TAU * f / FS;
                let rr = m::exp(-m::PI * f / q / FS);
                (rad * m::sin(w), 2.0 * rr * m::cos(w), rr * rr, 0.0, 0.0)
            })
            .collect();
        let c = Controls { rain_mm_h: 0.0, shelter: 1.0, cabin: 0.0, volume: 1.0, muted: false };
        LiveRainB {
            cap: RainCap::new(),
            grains,
            voices: vec![Voice::default(); VOICES],
            rng: NpRng::new(&[seed, 0x5255_4e00]),
            cur: c,
            target: c,
            dsd_rate: -1.0,
            dsd: rainb::Dsd::new(0.01),
            streams: vec![Stream::default(); rainb::RINGS.len() * rainb::BANDS.len()],
            roof_rate: [0.0; 7],
            gust: 1.0,
            gust_v: 0.0,
            flick_near: 0.0,
            flick_far: 0.0,
            bins: (0..NB).map(|_| Vec::new()).collect(),
            bin_a,
            bin_s: [0.0; NB],
            used: [false; NB],
            air_bin,
            roof,
            force: Vec::new(),
            liner: Biquad::lowpass(FS, tn.liner_hz, 0.6),
            lf: Biquad::lowshelf(FS, 150.0, tn.cabin_lf_db),
            body1: Biquad::lowpass(FS, 350.0, 0.7071),
            body2: Biquad::lowpass(FS, 700.0, 0.7071),
            out_open: Vec::new(),
            gain_s: 0.0,
            idle: true,
        }
    }

    pub fn set(&mut self, c: Controls) {
        self.target = c;
    }

    /// Size the scratch for blocks of up to n samples (call before the audio starts: process() never allocates
    /// when the block is no larger).
    pub fn reserve(&mut self, n: usize) {
        for b in self.bins.iter_mut() {
            b.resize(n, 0.0);
        }
        self.force.resize(n, 0.0);
        self.out_open.resize(n, 0.0);
    }

    fn restream(&mut self, r: f64) {
        self.dsd = rainb::Dsd::new(r);
        for (ri, &(r0, r1)) in rainb::RINGS.iter().enumerate() {
            let area = m::PI * (r1 * r1 - r0 * r0);
            let cap = if r0 < 10.0 { CAP_NEAR } else { CAP_FAR };
            for (bi, &(d0, d1)) in rainb::BANDS.iter().enumerate() {
                let rate = self.dsd.flux(d0, d1) * area;
                let k = (rate / cap).max(1.0);
                self.streams[ri * rainb::BANDS.len() + bi] = Stream { rate: rate / k, gain: m::sqrt(k) };
            }
        }
        for (bi, &(d0, d1)) in rainb::BANDS.iter().enumerate() {
            self.roof_rate[bi] = self.dsd.flux(d0, d1) * 1.9;
        }
        self.dsd_rate = r;
    }

    fn start(&mut self, grain: usize, gd: f64, gi: f64, bin: u8) {
        if let Some(v) = self.voices.iter_mut().find(|v| !v.active) {
            *v = Voice { grain: grain as u32, pos: 0, gd: gd as f32, gi: gi as f32, bin, active: true };
        }
    }

    pub fn active_voices(&self) -> usize {
        self.voices.iter().filter(|v| v.active).count()
    }

    /// Mix `out.len()` samples (mono) into out.
    pub fn process(&mut self, out: &mut [f32]) {
        let n = out.len();
        let dt = n as f64 / FS;
        let a = 1.0 - m::exp(-dt / 0.5);
        let b = 1.0 - m::exp(-dt / 0.3);
        self.cur.rain_mm_h += a * (self.target.rain_mm_h - self.cur.rain_mm_h);
        self.cur.shelter += b * (self.target.shelter - self.cur.shelter);
        self.cur.cabin += b * (self.target.cabin - self.cur.cabin);
        let r = self.cur.rain_mm_h;
        let dry = r < 0.02 || self.target.muted;
        // dry and quiet: nothing at all
        if dry && self.idle {
            self.gain_s = 0.0;
            return;
        }
        if self.bins[0].len() < n {
            self.reserve(n); // (only if the caller sent a bigger block than reserved)
        }
        if !dry && (r - self.dsd_rate).abs() > 0.02 * self.dsd_rate.max(0.01) {
            self.restream(r);
        }
        // gusts (a slow random walk, +/-40 %) and clustering (Ornstein-Uhlenbeck flicker of ln rate, ~4 Hz)
        self.gust_v += (self.rng.standard_normal() * 0.6 - 0.3 * (self.gust - 1.0)) * dt;
        self.gust = (self.gust + self.gust_v * dt).clamp(0.6, 1.4);
        let th = 1.0 - m::exp(-dt * m::TAU * 4.0);
        let tn = rainb::Tune::get_cached();
        self.flick_near += th * (-self.flick_near) + tn.fast_near * m::sqrt(2.0 * th) * self.rng.standard_normal();
        self.flick_far += th * (-self.flick_far) + tn.fast_far * m::sqrt(2.0 * th) * self.rng.standard_normal();
        let gn = self.gust * m::exp(self.flick_near - 0.5 * tn.fast_near * tn.fast_near);
        let gf = self.gust * m::exp(self.flick_far - 0.5 * tn.fast_far * tn.fast_far);
        for k in 0..NB {
            if self.used[k] {
                for v in self.bins[k][..n].iter_mut() {
                    *v = 0.0;
                }
            }
        }
        for v in self.force[..n].iter_mut() {
            *v = 0.0;
        }
        let nb = rainb::BANDS.len();
        let exclude = 6.0 * (1.0 - self.cur.shelter).clamp(0.0, 1.0);
        if !dry {
            for ri in 0..rainb::RINGS.len() {
                let (r0, r1) = rainb::RINGS[ri];
                let g_fl = if r0 < 4.0 { gn } else { gf };
                for bi in 0..nb {
                    let st = self.streams[ri * nb + bi];
                    if st.rate <= 0.0 {
                        continue;
                    }
                    let cnt = poisson(&mut self.rng, st.rate * dt * g_fl);
                    for _ in 0..cnt {
                        let rho = m::sqrt(r0 * r0 + self.rng.random() * (r1 * r1 - r0 * r0));
                        if exclude > 0.0 && (rho * m::sin(self.rng.random() * m::TAU)).abs() < exclude {
                            continue;
                        }
                        let u = self.rng.random();
                        let (s, zs) = if u < 0.15 && rho > 2.0 {
                            (3usize, 8.0)
                        } else if u < 0.19 && rho > 3.0 && rho < 25.0 {
                            (4 + self.rng.integers(0, 3) as usize, 1.0)
                        } else if u < 0.19 + 0.1 {
                            (1, 0.0)
                        } else if u < 0.19 + 0.25 {
                            (2, 0.0)
                        } else {
                            (0, 0.0)
                        };
                        let dz = rainb::OBS_H - zs;
                        let rr = m::sqrt(rho * rho + dz * dz).max(0.3);
                        let cos_t = dz.abs() / rr;
                        let gd = (tn.iso + (1.0 - tn.iso) * cos_t) / rr * st.gain;
                        let gi = st.gain / rr;
                        let grain = (s * nb + bi) * VARIANTS + self.rng.integers(0, VARIANTS as i64) as usize;
                        let bin = if zs > 0.0 { self.air_bin[ri].max(rainb::bucket_of(16000.0) as u8) } else { self.air_bin[ri] };
                        self.start(grain, gd, gi, bin.min((NB - 1) as u8));
                    }
                }
            }
            if self.cur.cabin > 0.001 {
                for bi in 0..nb {
                    let cnt = poisson(&mut self.rng, self.roof_rate[bi] * dt * gn);
                    for _ in 0..cnt {
                        let (d0, d1) = rainb::BANDS[bi];
                        let d = self.rng.uniform(d0, d1);
                        let v = rainb::terminal_v(d);
                        // the hit's impulse (momentum), placed in the block
                        let at = self.rng.integers(0, n as i64) as usize;
                        self.force[at] += d * d * d * v * 1e-3;
                    }
                }
            }
        }
        // voices into their bins
        let mut any = false;
        for v in self.voices.iter_mut() {
            if !v.active {
                continue;
            }
            any = true;
            let g = &self.grains[v.grain as usize];
            let pos = v.pos as usize;
            let len = g.dip.len().max(g.iso.len());
            let k = (len - pos).min(n);
            let bin = &mut self.bins[v.bin as usize];
            self.used[v.bin as usize] = true;
            if pos < g.dip.len() {
                let kd = (g.dip.len() - pos).min(k);
                for (o, &x) in bin[..kd].iter_mut().zip(&g.dip[pos..pos + kd]) {
                    *o += x * v.gd;
                }
            }
            if pos < g.iso.len() {
                let ki = (g.iso.len() - pos).min(k);
                for (o, &x) in bin[..ki].iter_mut().zip(&g.iso[pos..pos + ki]) {
                    *o += x * v.gi;
                }
            }
            v.pos = (pos + k) as u32;
            if pos + k >= len {
                v.active = false;
            }
        }
        // the bins through their low-passes, into the open-air mix
        for v in self.out_open[..n].iter_mut() {
            *v = 0.0;
        }
        let mut still = false;
        for kb in 0..NB {
            if !self.used[kb] {
                continue;
            }
            let a = self.bin_a[kb];
            let mut s = self.bin_s[kb];
            let src = &self.bins[kb];
            if kb == NB - 1 {
                for (o, &x) in self.out_open[..n].iter_mut().zip(&src[..n]) {
                    *o += x;
                }
            } else {
                for (o, &x) in self.out_open[..n].iter_mut().zip(&src[..n]) {
                    s += a * (x - s);
                    *o += s;
                }
            }
            self.bin_s[kb] = s;
            if s.abs() > 1e-9 {
                still = true;
            }
            if !any && s.abs() <= 1e-9 {
                self.used[kb] = false;
            }
        }
        self.cap.process(&mut self.out_open[..n], r.max(0.02));
        let want = if self.target.muted { 0.0 } else { self.target.volume };
        let cab = self.cur.cabin;
        let g0 = self.gain_s;
        let roof_g = 2.0e3 * self.cap.gain;
        for i in 0..n {
            let x = self.out_open[i] as f64;
            let y = if cab > 0.001 {
                let f = self.force[i];
                let mut rf = 0.0;
                for md in self.roof.iter_mut() {
                    let yy = md.1 * md.3 - md.2 * md.4 + f;
                    md.4 = md.3;
                    md.3 = yy;
                    rf += md.0 * yy;
                }
                let rf = self.lf.tick(self.liner.tick(rf)) * roof_g;
                let body = self.body2.tick(self.body1.tick(x)) * 0.5;
                x * (1.0 - cab) + (rf + body) * cab
            } else {
                x
            };
            let g = g0 + (want - g0) * (i as f64 / n as f64);
            out[i] += (y * g) as f32;
        }
        self.gain_s = want;
        self.idle = dry && !any && !still;
    }
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
        if tail > 1e-5 * total {
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

/// Offline harness: dry -> light -> medium -> heavy -> cabin -> shelter -> dry, in blocks, timed; optionally written.
pub fn bench(seconds: f64, block: usize, wav: Option<&str>) -> String {
    let t_build = std::time::Instant::now();
    let mut e = LiveRainB::new(1);
    e.reserve(block);
    let build_s = t_build.elapsed().as_secs_f64();
    let total = (seconds * FS) as usize;
    let mut out = vec![0.0f32; block];
    let mut all = Vec::with_capacity(if wav.is_some() { total } else { 0 });
    // per phase: (label, rate, shelter, cabin, cpu seconds, audio seconds, max voices)
    let mut phases: Vec<(&str, f64, f64, f64, f64, f64, usize)> = vec![
        ("dry", 0.0, 1.0, 0.0, 0.0, 0.0, 0),
        ("light 0.7", 0.7, 1.0, 0.0, 0.0, 0.0, 0),
        ("medium 5", 5.0, 1.0, 0.0, 0.0, 0.0, 0),
        ("heavy 35", 35.0, 1.0, 0.0, 0.0, 0.0, 0),
        ("cabin 5", 5.0, 1.0, 1.0, 0.0, 0.0, 0),
        ("shelter 5", 5.0, 0.0, 0.0, 0.0, 0.0, 0),
        ("drying (rate easing out)", 0.0, 1.0, 0.0, 0.0, 0.0, 0),
        ("dry again, settled", 0.0, 1.0, 0.0, 0.0, 0.0, 0),
    ];
    let per = total / phases.len();
    let mut done = 0;
    while done < total {
        let ph = (done / per).min(phases.len() - 1);
        let p = phases[ph];
        e.set(Controls { rain_mm_h: p.1, shelter: p.2, cabin: p.3, volume: 1.0, muted: false });
        for v in out.iter_mut() {
            *v = 0.0;
        }
        let t0 = std::time::Instant::now();
        e.process(&mut out);
        let el = t0.elapsed().as_secs_f64();
        phases[ph].4 += el;
        phases[ph].5 += block as f64 / FS;
        phases[ph].6 = phases[ph].6.max(e.active_voices());
        if wav.is_some() {
            all.extend(out.iter().map(|&v| v as f64));
        }
        done += block;
    }
    if let Some(w) = wav {
        let _ = crate::wav::write_f32(w, &all, 44100);
    }
    let mut s = format!("bank built in {build_s:.2} s ({} grains); block {block}\nphase\tcpu % of one core\tmax voices\n", e.grains.len());
    for p in phases.iter() {
        s += &format!("{}\t{:.3}\t{}\n", p.0, 100.0 * p.4 / p.5.max(1e-9), p.6);
    }
    s
}
