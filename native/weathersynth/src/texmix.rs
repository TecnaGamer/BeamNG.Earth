//! Recorded weather textures (rain, wind, drips, hail) mixed from the texture bank (texbuild.rs): random overlap-add of
//! clean recorded blocks (Frojd & Horner 2009), the recording-mix counterpart of the procedural rain loops.
//!
//!   weathersynth tex-set --bank BIN --out DIR [--seed N] [--ogg] [--prefix P] [--only a,b]   the loops, mod names
//!   weathersynth tex-check FILE.wav ...                                      loudness, peak, seam and texture stats
//!
//! - **Streams:** each loop is two independent streams (one when a class has a single recording), each drawing from
//!   its own share of the class's recordings, so the two never play the same take at once; sparse classes keep the
//!   second 6 dB down (stream_gains).
//! - **Segments:** U(2, 5) s of a block, from a random point of it (never within 1 s of where that block was last
//!   used), varispeed U(0.97, 1.03) (Catmull-Rom), gain U(-1, +1) dB, joined by 0.5 s sin/cos crossfades: equal power,
//!   right for uncorrelated noise, so the joins neither dip nor bump.
//! - **Seamless:** the streams are laid on a circular timeline: the last segment's crossfade overlaps the first one's
//!   fade-in across the wrap, and every filter runs settled on the loop (Biquad::run_circular). The wrap is a
//!   crossfade like any other join, never a cut.
//! - **Levels:** integrated loudness over the loop played twice, set to the class's target (the current loops' levels,
//!   so swapping files changes nothing in loudness); a rare hit over -1 dBTP goes through a circular look-ahead
//!   limiter at -1.5 dBTP, then the level is set again.
//! - **Deterministic:** rng.rs streams named by class and stream, libm maths, f64 sums in a fixed order.

use crate::dsp::Biquad;
use crate::loudness;
use crate::m;
use crate::rng::Rng;
use crate::texbuild::{self, Bank, CLASSES};

pub const SR: f64 = 44100.0;
/// The segments: core length U(core_min, core_max) s, crossfades xf_s, varispeed 1 +/- vs. Since the user's 2026-11-26
/// listen ("I can tell all sounds are trying to filter out other noises"): long segments and joins (4-12 s, 1 s), so
/// the timbre changes between takes come seldom and slowly, and +/-1 % varispeed (it was 2-5 s, 0.5 s, +/-3 %: a
/// take change, and a small pitch step, every few seconds). `--legacy` renders the old way for comparison.
#[derive(Clone, Copy)]
pub struct Params {
    pub core_min: f64,
    pub core_max: f64,
    pub xf_s: f64,
    pub vs: f64,
}
pub const PARAMS: Params = Params { core_min: 4.0, core_max: 12.0, xf_s: 1.0, vs: 0.01 };
pub const LEGACY: Params = Params { core_min: 2.0, core_max: 5.0, xf_s: 0.5, vs: 0.03 };
static ACTIVE: std::sync::OnceLock<Params> = std::sync::OnceLock::new();
fn params() -> Params {
    *ACTIVE.get().unwrap_or(&PARAMS)
}

/// A loop: its file stem, bank class, length s, target LUFS, low-pass corners applied after (muffled A), the in-car
/// filter (the roof heard inside: CABIN_LP twice and a body resonance), drops per second for an event loop (the
/// drips: single drops from the bank's drip grains; 0 = a texture), and the loop whose gain it takes ("" = its own
/// level: the drips' three densities share drips_1's gain, so a drop is as loud in each and only the rate falls).
pub struct LoopSpec {
    pub name: &'static str,
    pub class: &'static str,
    pub secs: f64,
    pub lufs: f64,
    pub lowpass: &'static [f64],
    pub cabin: bool,
    pub events: f64,
    pub gain_of: &'static str,
    /// an event loop of synthetic drops (synth_drops) instead of the bank's grains
    pub synth: bool,
    /// an event loop's bounces: the share of hits followed by a smaller one 40-150 ms later (hail)
    pub bounce: f64,
    /// a texture under an event loop (its bank class) and how far under the events (dB)
    pub bed: &'static str,
    pub bed_db: f64,
    /// level by the sample peak (dBFS) instead of the loudness: hits whose transients a limiter must not touch (hail)
    pub peak_db: f64,
}

/// The roof heard from inside the car (the user, 2026-11-26: "INSIDE the car ... a muffled version of the roof"):
/// two low-passes at CABIN_LP_HZ and a body / headliner resonance (+CABIN_PEAK_DB at CABIN_PEAK_HZ, Q CABIN_PEAK_Q).
pub const CABIN_LP_HZ: f64 = 1400.0;
pub const CABIN_PEAK_HZ: f64 = 140.0;
pub const CABIN_PEAK_Q: f64 = 1.2;
pub const CABIN_PEAK_DB: f64 = 6.0;

pub fn cabin_filter(x: &mut [f64]) {
    Biquad::lowpass(SR, CABIN_LP_HZ, std::f64::consts::FRAC_1_SQRT_2).run_circular(x);
    Biquad::lowpass(SR, CABIN_LP_HZ, std::f64::consts::FRAC_1_SQRT_2).run_circular(x);
    Biquad::peaking(SR, CABIN_PEAK_HZ, CABIN_PEAK_Q, CABIN_PEAK_DB).run_circular(x);
}

const fn tex(name: &'static str, class: &'static str, secs: f64, lufs: f64) -> LoopSpec {
    LoopSpec { name, class, secs, lufs, lowpass: &[], cabin: false, events: 0.0, gain_of: "", synth: false, bounce: 0.0, bed: "", bed_db: 0.0, peak_db: 0.0 }
}

pub const LOOPS: [LoopSpec; 23] = [
    tex("light", "rain_light", 30.0, -30.2),
    tex("medium", "rain_moderate", 30.0, -24.2),
    tex("heavy", "rain_heavy", 30.0, -17.2),
    // the roof from outside, near the car (hood / bumper cameras, orbit close in)
    tex("roof_light", "roof_light", 30.0, -30.0),
    tex("roof", "roof", 30.0, -24.1),
    tex("roof_heavy", "roof_heavy", 30.0, -17.2),
    // the roof from inside (driver / cockpit cameras): the same takes through the cabin filter, at the same loudness
    LoopSpec { cabin: true, ..tex("roof_cabin_light", "roof_light", 30.0, -30.0) },
    LoopSpec { cabin: true, ..tex("roof_cabin", "roof", 30.0, -24.1) },
    LoopSpec { cabin: true, ..tex("roof_cabin_heavy", "roof_heavy", 30.0, -17.2) },
    // muffled A: the outdoor medium rain through the mod's occlusion (two 900 Hz low-passes): tunnels, closed bridges,
    // buildings (the user, 2026-11-26)
    LoopSpec { lowpass: &[900.0, 900.0], ..tex("muffled", "rain_moderate", 30.0, -25.9) },
    // muffled B: real takes from under bridges and awnings: open bridges and overpasses
    tex("muffled_open", "rain_under", 30.0, -25.9),
    // drips after rain: sparse single drops, three densities the mod crossfades as the drips die away. Synthetic (the
    // user, 2026-11-26, of the recorded ones: "every time a drip sound is played I can hear its background noise"):
    // rain.rs's drop model, nothing between the drops
    LoopSpec { events: 0.8, synth: true, ..tex("drips_1", "drip", 60.0, -36.0) },
    LoopSpec { events: 0.3, synth: true, gain_of: "drips_1", ..tex("drips_2", "drip", 60.0, -36.0) },
    LoopSpec { events: 0.1, synth: true, gain_of: "drips_1", ..tex("drips_3", "drip", 60.0, -36.0) },
    // the A/B: recorded drops, each cleaned to true silence (texbuild::impact_grains)
    LoopSpec { events: 0.8, ..tex("drips_rec_1", "drip", 60.0, -36.0) },
    LoopSpec { events: 0.3, gain_of: "drips_rec_1", ..tex("drips_rec_2", "drip", 60.0, -36.0) },
    LoopSpec { events: 0.1, gain_of: "drips_rec_1", ..tex("drips_rec_3", "drip", 60.0, -36.0) },
    tex("gutter", "gutter", 30.0, -28.0),
    tex("wind_light", "wind_light", 60.0, -36.0),
    tex("wind_moderate", "wind_moderate", 60.0, -30.0),
    tex("wind_strong", "wind_strong", 60.0, -24.0),
    // hail (the user, 2026-11-26: "sounds similar to rain"): hard ice hits with gaps, some bouncing, over a little rain
    // (levelled by its peak, -3 dBFS: no limiter on the hits; its loudness is reported)
    LoopSpec { events: 7.0, bounce: 0.35, bed: "rain_moderate", bed_db: 14.0, peak_db: -3.0, ..tex("hail", "hail_hit", 30.0, -20.0) },
    // (the round-4 hail, a texture of the hail takes, for comparison)
    tex("hail_texture", "hail", 30.0, -20.0),
];

/// Catmull-Rom sample of x at fractional position t (clamped ends).
#[inline]
pub fn cubic(x: &[f32], t: f64) -> f64 {
    let i = t.floor() as i64;
    let f = t - i as f64;
    let g = |k: i64| -> f64 { x[k.clamp(0, x.len() as i64 - 1) as usize] as f64 };
    let (p0, p1, p2, p3) = (g(i - 1), g(i), g(i + 1), g(i + 2));
    p1 + 0.5 * f * (p2 - p0 + f * (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3 + f * (3.0 * (p1 - p2) + p3 - p0)))
}

/// The streams' gains (power sum 1): a sparse class (light rain, its roof, drips) keeps one stream in front and the
/// other 6 dB under it, so two takes laid over each other do not double the drop density and blur light into medium.
pub fn stream_gains(class: &str, streams: usize) -> Vec<f64> {
    let raw: Vec<f64> = match (class, streams) {
        (_, 1) => vec![1.0],
        ("rain_light" | "roof_light" | "drips", _) => vec![1.0, 0.5],
        _ => vec![1.0, 1.0],
    };
    let p: f64 = raw.iter().map(|g| g * g).sum();
    raw.iter().map(|g| g / m::sqrt(p)).collect()
}

/// The blocks of a class, grouped by recording: (source ids in bank order, block indices per source).
pub fn pool(bank: &Bank, class: &str) -> Vec<(u32, Vec<usize>)> {
    let ci = texbuild::class_index(class).map(|c| c as u8);
    let mut out: Vec<(u32, Vec<usize>)> = Vec::new();
    for (k, b) in bank.blocks.iter().enumerate() {
        if Some(b.cls) != ci {
            continue;
        }
        match out.iter_mut().find(|(s, _)| *s == b.src_id) {
            Some(e) => e.1.push(k),
            None => out.push((b.src_id, vec![k])),
        }
    }
    out
}

const CEIL_DB: f64 = -1.0;
const LIM_DB: f64 = -1.5;

/// A 5 ms look-ahead, 80 ms release true-peak limiter run on the loop played three times (the middle copy kept), so it
/// is settled across the wrap.
pub fn limit_loop(x: &mut [f64], ceil_db: f64) {
    let n = x.len();
    let tri: Vec<f64> = x.iter().chain(x.iter()).chain(x.iter()).cloned().collect();
    let env = loudness::tp_envelope(&tri, true);
    let g = loudness::limiter_gain(&env, m::undb(ceil_db), (0.005 * SR) as usize, 0.08, SR);
    for i in 0..n {
        x[i] *= g[n + i];
    }
}

pub struct Info {
    pub segments: usize,
    pub sources: Vec<u32>,
    pub streams: usize,
    /// the joins: (the crossfade's middle, sample; whether the take changed there)
    pub joins: Vec<(usize, bool)>,
}

/// One class's texture on a circular timeline of n samples (not levelled).
pub fn texture(bank: &Bank, class: &str, seed: u64, n: usize) -> Option<(Vec<f64>, Info)> {
    let p = pool(bank, class);
    if p.is_empty() {
        return None;
    }
    let streams = if p.len() >= 2 { 2 } else { 1 };
    let pr = params();
    let (core_min, core_max) = (pr.core_min, pr.core_max);
    let xf = (pr.xf_s * SR) as usize;
    let mut out = vec![0.0f64; n];
    let mut segs = 0;
    let mut joins = Vec::new();
    let sg = stream_gains(class, streams);
    for s in 0..streams {
        let mut rng = Rng::new(seed, &format!("tex-{class}-{s}"));
        let norm = sg[s];
        // this stream's recordings: every streams-th of the class's
        let mine: Vec<usize> = p.iter().enumerate().filter(|(i, _)| i % streams == s).flat_map(|(_, (_, v))| v.clone()).collect();
        let start = if s == 0 { 0 } else { (rng.range(0.5, 2.5) * SR) as usize };
        let end = start + n;
        let mut pos = start;
        let mut last: Option<(usize, f64)> = None;
        let mut first = true;
        while pos < end {
            let rem = (end - pos) as f64 / SR;
            let mut core = rng.range(core_min, core_max);
            if rem - core < core_min {
                core = if rem <= core_max { rem } else { rem / 2.0 };
            }
            let core_n = ((core * SR) as usize).min(end - pos).max(1);
            let vs = rng.range(1.0 - pr.vs, 1.0 + pr.vs);
            // (never longer than the stream's longest block allows; the timeline goes on with the next segment)
            let longest = mine.iter().map(|&k| bank.blocks[k].samples.len()).max().unwrap_or(0) as f64;
            let fit = (((longest - 8.0) / vs) as usize).saturating_sub(xf);
            let core_n = core_n.min(fit.max(SR as usize));
            let len = core_n + xf;
            let span = len as f64 * vs + 4.0;
            // a block long enough, weighted by its length; a new place in it
            let fits: Vec<usize> = mine.iter().cloned().filter(|&k| bank.blocks[k].samples.len() as f64 > span + 2.0).collect();
            let fits = if fits.is_empty() {
                p.iter().flat_map(|(_, v)| v.clone()).filter(|&k| bank.blocks[k].samples.len() as f64 > span + 2.0).collect()
            } else {
                fits
            };
            if fits.is_empty() {
                return None;
            }
            let tot: f64 = fits.iter().map(|&k| bank.blocks[k].samples.len() as f64).sum();
            let (mut bk, mut st) = (fits[0], 1.0);
            for _ in 0..20 {
                let mut r = rng.f() * tot;
                bk = *fits.last().unwrap();
                for &k in &fits {
                    let l = bank.blocks[k].samples.len() as f64;
                    if r < l {
                        bk = k;
                        break;
                    }
                    r -= l;
                }
                let room = bank.blocks[bk].samples.len() as f64 - span - 2.0;
                st = 1.0 + rng.f() * room;
                match last {
                    Some((lb, ls)) if lb == bk && (ls - st).abs() < SR => continue,
                    _ => break,
                }
            }
            let changed = last.map(|(lb, _)| bank.blocks[lb].src_id != bank.blocks[bk].src_id).unwrap_or(false);
            if !first {
                joins.push(((pos + xf / 2) % n, changed));
            }
            first = false;
            last = Some((bk, st));
            let g = m::undb(rng.range(-1.0, 1.0)) * norm;
            let x = &bank.blocks[bk].samples;
            for i in 0..len {
                let w = if i < xf {
                    m::sin(0.5 * m::PI * (i as f64 + 0.5) / xf as f64)
                } else if i >= core_n {
                    m::cos(0.5 * m::PI * ((i - core_n) as f64 + 0.5) / xf as f64)
                } else {
                    1.0
                };
                out[(pos + i) % n] += cubic(x, st + i as f64 * vs) * w * g;
            }
            segs += 1;
            pos += core_n;
        }
    }
    let sources = p.iter().map(|(s, _)| *s).collect();
    Some((out, Info { segments: segs, sources, streams, joins }))
}

/// Synthetic drops at `rate` per second, Poisson on a circular timeline of n samples: drips from trees and eaves, big
/// slow drops (3.5-5.5 mm at 4-8 m/s) onto a puddle (a splash and a Minnaert bubble whose pitch rises), the wet road
/// or leaves (rain.rs drop_event_pitched, pitched down 0.6-0.85: bigger drops ring lower), each U(-12, 0) dB. Silence
/// between the drops.
pub fn synth_drops(seed: u64, n: usize, rate: f64) -> (Vec<f64>, Info) {
    use crate::rain::{drop_event_pitched, Surf};
    let mut rng = Rng::new(seed, &format!("synth-drops-{rate}"));
    let mut out = vec![0.0f64; n];
    let mut t = 0.0f64;
    let mut count = 0;
    loop {
        t += -m::ln(1.0 - rng.f()) / rate;
        let at = (t * SR) as usize;
        if at >= n {
            break;
        }
        let u = rng.f();
        let surf = if u < 0.5 { Surf::Puddle } else if u < 0.75 { Surf::Road } else { Surf::Leaves };
        let (dmm, vel, pitch) = (rng.range(3.5, 5.5), rng.range(4.0, 8.0), rng.range(0.6, 0.85));
        let ev = drop_event_pitched(&mut rng, surf, dmm, vel, 1.0, pitch);
        let g = m::undb(rng.range(-12.0, 0.0));
        for (i, v) in ev.iter().enumerate() {
            out[(at + i) % n] += v * g;
        }
        count += 1;
    }
    (out, Info { segments: count, sources: vec![], streams: 1, joins: vec![] })
}

/// An event loop: single hits (the class's grains) at `rate` per second, Poisson on a circular timeline of n samples,
/// each a random grain at U(-10, 0) dB and varispeed U(0.92, 1.08) (drops and hailstones differ in size and pitch);
/// a `bounce` share followed by a smaller hit of the same grain 40-150 ms later (a hailstone bouncing).
pub fn events(bank: &Bank, class: &str, seed: u64, n: usize, rate: f64, bounce: f64) -> Option<(Vec<f64>, Info)> {
    let p = pool(bank, class);
    let all: Vec<usize> = p.iter().flat_map(|(_, v)| v.clone()).collect();
    if all.is_empty() {
        return None;
    }
    let mut rng = Rng::new(seed, &format!("tex-events-{class}-{rate}"));
    let mut out = vec![0.0f64; n];
    let mut t = 0.0f64;
    let mut count = 0;
    loop {
        t += -m::ln(1.0 - rng.f()) / rate;
        let at = (t * SR) as usize;
        if at >= n {
            break;
        }
        let k = all[((rng.f() * all.len() as f64) as usize).min(all.len() - 1)];
        let g = m::undb(rng.range(-10.0, 0.0));
        let vs = rng.range(0.92, 1.08);
        let x = &bank.blocks[k].samples;
        let len = ((x.len() as f64 - 3.0) / vs) as usize;
        for i in 0..len {
            out[(at + i) % n] += cubic(x, i as f64 * vs) * g;
        }
        if bounce > 0.0 && rng.chance(bounce) {
            let at2 = at + (rng.range(0.04, 0.15) * SR) as usize;
            let g2 = g * m::undb(rng.range(-14.0, -6.0));
            let vs2 = vs * rng.range(1.02, 1.12);
            let len2 = ((x.len() as f64 - 3.0) / vs2) as usize;
            for i in 0..len2 {
                out[(at2 + i) % n] += cubic(x, i as f64 * vs2) * g2;
            }
        }
        count += 1;
    }
    Some((out, Info { segments: count, sources: p.iter().map(|(s, _)| *s).collect(), streams: 1, joins: vec![] }))
}

/// The timbre change across joins: the 1/3-octave shape (dB) of the second before a join's crossfade against the
/// second after, RMS over the bands; the same at the segments' middles (no join) as the yardstick. (median, p90) for the
/// joins that change take, and for the middles.
pub fn join_jumps(x: &[f64], info: &Info, xf_s: f64) -> ((f64, f64), (f64, f64)) {
    let n = x.len();
    let w = SR as usize;
    let half = (xf_s * SR / 2.0) as usize + w / 10;
    let grab = |a: usize| -> Vec<f64> { (0..w).map(|i| x[(a + i) % n]).collect() };
    let shape = |a: usize| -> Vec<f64> {
        crate::texbuild::third_oct_shape(&grab(a)).iter().map(|v| 10.0 * m::log10(v.max(1e-20))).collect()
    };
    let d = |c: usize| -> f64 {
        let a = shape((c + n - half - w) % n);
        let b = shape((c + half) % n);
        m::sqrt(a.iter().zip(&b).map(|(p, q)| (p - q) * (p - q)).sum::<f64>() / a.len() as f64)
    };
    let mut sw: Vec<f64> = info.joins.iter().filter(|j| j.1).map(|j| d(j.0)).collect();
    // the yardstick: points midway between consecutive joins of the loop (inside a segment)
    let mut cs: Vec<usize> = info.joins.iter().map(|j| j.0).collect();
    cs.sort();
    let mut mid: Vec<f64> = cs.windows(2).filter(|p| p[1] - p[0] > 3 * w).map(|p| d((p[0] + p[1]) / 2)).collect();
    let stat = |v: &mut Vec<f64>| -> (f64, f64) {
        if v.is_empty() {
            return (0.0, 0.0);
        }
        v.sort_by(|a, b| a.total_cmp(b));
        (v[v.len() / 2], v[((v.len() as f64 * 0.9) as usize).min(v.len() - 1)])
    };
    (stat(&mut sw), stat(&mut mid))
}

/// A finished loop: texture (or events), its filters and a 30 Hz high-pass (circular), levelled (or at `fixed_gain`).
/// (samples, LUFS, true peak, info, the total gain applied)
pub fn render_loop(bank: &Bank, spec: &LoopSpec, seed: u64, fixed_gain: Option<f64>) -> Option<(Vec<f64>, f64, f64, Info, f64)> {
    let n = (spec.secs * SR) as usize;
    let (mut x, info) = if spec.synth {
        synth_drops(seed, n, spec.events)
    } else if spec.events > 0.0 {
        events(bank, spec.class, seed, n, spec.events, spec.bounce)?
    } else {
        texture(bank, spec.class, seed, n)?
    };
    if !spec.bed.is_empty() {
        // the bed bed_db under the events (both measured over the loop)
        if let Some((mut b, _)) = texture(bank, spec.bed, seed ^ 0xbed, n) {
            let (le, lb) = (loudness::integrated(&x, SR, true), loudness::integrated(&b, SR, true));
            let g = m::undb(le - spec.bed_db - lb);
            for (v, w) in x.iter_mut().zip(b.iter_mut()) {
                *v += *w * g;
            }
        }
    }
    for &fc in spec.lowpass {
        Biquad::lowpass(SR, fc, std::f64::consts::FRAC_1_SQRT_2).run_circular(&mut x);
    }
    if spec.cabin {
        cabin_filter(&mut x);
    }
    Biquad::highpass(SR, 30.0, std::f64::consts::FRAC_1_SQRT_2).run_circular(&mut x);
    // level, then while the true peak is over CEIL_DB a circular look-ahead limiter at LIM_DB and level again
    let mut tp = 0.0;
    let mut total = 1.0;
    for _ in 0..4 {
        let g = match fixed_gain {
            Some(fg) => fg / total,
            None if spec.peak_db != 0.0 => m::undb(spec.peak_db) / m::undb(loudness::true_peak_db(&x, true).max(-200.0)),
            None => m::undb(spec.lufs - loudness::integrated(&x, SR, true)),
        };
        for v in x.iter_mut() {
            *v *= g;
        }
        total *= g;
        tp = loudness::true_peak_db(&x, true);
        if tp <= CEIL_DB {
            break;
        }
        limit_loop(&mut x, LIM_DB);
        if fixed_gain.is_some() {
            tp = loudness::true_peak_db(&x, true);
            break;
        }
    }
    let li = loudness::integrated(&x, SR, true);
    Some((x, li, tp, info, total))
}

fn write_ogg(path: &str, x: &[f64], q: &str) -> Result<(), String> {
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

fn arg(args: &[String], key: &str) -> Option<String> {
    args.iter().position(|a| a == key).and_then(|i| args.get(i + 1).cloned())
}

pub fn run_set(args: &[String]) -> Result<(), String> {
    let bank = Bank::load(&arg(args, "--bank").ok_or("--bank BIN")?)?;
    let dir = arg(args, "--out").ok_or("--out DIR")?;
    let seed: u64 = arg(args, "--seed").and_then(|v| v.parse().ok()).unwrap_or(1);
    let prefix = arg(args, "--prefix").unwrap_or_else(|| "bne_rec_".into());
    let only: Option<Vec<String>> = arg(args, "--only").map(|v| v.split(',').map(|s| s.trim().to_string()).collect());
    let ogg = args.iter().any(|a| a == "--ogg");
    if args.iter().any(|a| a == "--legacy") {
        let _ = ACTIVE.set(LEGACY);
    }
    let pr = params();
    std::fs::create_dir_all(format!("{dir}/wav")).map_err(|e| e.to_string())?;
    let mut gains: std::collections::BTreeMap<&str, f64> = std::collections::BTreeMap::new();
    println!("file\tsegments\tstreams\tsources\tLUFS\ttrue peak dBTP\ttake changes / min\ttake-change joins: timbre jump median / p90 dB (inside a segment)");
    for spec in LOOPS.iter() {
        if let Some(o) = &only {
            if !o.iter().any(|n| n == spec.name) {
                continue;
            }
        }
        let fixed = if spec.gain_of.is_empty() { None } else { gains.get(spec.gain_of).cloned() };
        let Some((x, li, tp, info, gain)) = render_loop(&bank, spec, seed, fixed) else {
            println!("{}{}\t(no blocks of {})", prefix, spec.name, spec.class);
            continue;
        };
        let stem = format!("{prefix}{}", spec.name);
        crate::wav::write_f32(&format!("{dir}/wav/{stem}.wav"), &x, 44100).map_err(|e| e.to_string())?;
        if ogg {
            write_ogg(&format!("{dir}/{stem}.ogg"), &x, "4")?;
        }
        let src: Vec<String> = info.sources.iter().map(|s| s.to_string()).collect();
        // the takes used (the scoring's leave-own-takes-out reads them)
        let takes: Vec<String> = src.iter().map(|s| format!("{{\"id\": \"fs{s}\"}}")).collect();
        std::fs::write(
            format!("{dir}/wav/{stem}.json"),
            format!("{{\"class\": \"{}\", \"seed\": {seed}, \"lufs\": {li:.2}, \"true_peak\": {tp:.2}, \"takes\": [{}]}}\n", spec.class, takes.join(", ")),
        )
        .map_err(|e| e.to_string())?;
        gains.insert(spec.name, gain);
        let ((jm, jp), (mm, mp)) = join_jumps(&x, &info, pr.xf_s);
        let per_min = info.joins.iter().filter(|j| j.1).count() as f64 * 60.0 / spec.secs;
        println!("{stem}\t{}\t{}\t{}\t{li:.2}\t{tp:.2}\t{per_min:.1}\t{jm:.2} / {jp:.2} ({mm:.2} / {mp:.2})", info.segments, info.streams, src.join(","));
    }
    let _ = CLASSES;
    Ok(())
}

/// Seam and texture statistics of a loop (mono WAV): (LUFS looped, max short-term, true peak, the 50 ms level step and
/// the spectral flux across the wrap as percentiles of the same measures inside the loop, kurtosis, onsets/s, centroid).
pub fn check(x: &[f64]) -> String {
    let n = x.len();
    let li = loudness::integrated(x, SR, true);
    let tp = loudness::true_peak_db(x, true);
    let two: Vec<f64> = x.iter().chain(x.iter()).cloned().collect();
    let mst = loudness::max_short_term(&two, SR);
    // 50 ms level steps on the doubled loop; the steps that straddle the wrap (at n)
    let w = (0.05 * SR) as usize;
    let lv: Vec<f64> = (0..two.len() / w).map(|i| crate::grainbuild::db10(crate::grainbuild::mean_sq(&two[i * w..(i + 1) * w]))).collect();
    let steps: Vec<f64> = lv.windows(2).map(|p| (p[1] - p[0]).abs()).collect();
    let wrap_i = n / w;
    let seam_step = steps[wrap_i.saturating_sub(2)..(wrap_i + 2).min(steps.len())].iter().cloned().fold(0.0, f64::max);
    // (the seam takes the largest of 4 steps, so it is ranked among the largest of every 4 inside the loop)
    let inside: Vec<f64> = steps[..wrap_i.saturating_sub(2)].windows(4).map(|w| w.iter().cloned().fold(0.0, f64::max)).collect();
    let pct = |v: &[f64], s: f64| 100.0 * v.iter().filter(|q| **q < s).count() as f64 / v.len().max(1) as f64;
    // spectral flux (2048-pt Hann, hop 512) in dB per bin, 100 Hz-16 kHz
    let nf = 2048usize;
    let hop = 512usize;
    let win = crate::npx::hanning(nf);
    let fr: Vec<Vec<f64>> = (0..(two.len() - nf) / hop)
        .map(|i| {
            let s: Vec<f64> = (0..nf).map(|j| two[i * hop + j] * win[j]).collect();
            crate::npx::rfft(&s, nf).iter().skip(5).take(738).map(|c| 10.0 * m::log10(c.re * c.re + c.im * c.im + 1e-18)).collect()
        })
        .collect();
    let flux: Vec<f64> = fr.windows(2).map(|p| p[0].iter().zip(&p[1]).map(|(a, b)| (a - b).abs()).sum::<f64>() / p[0].len() as f64).collect();
    let wf = n / hop;
    let seam_flux = flux[wf.saturating_sub(4)..(wf + 2).min(flux.len())].iter().cloned().fold(0.0, f64::max);
    let fin: Vec<f64> = flux[..wf.saturating_sub(4)].windows(6).map(|w| w.iter().cloned().fold(0.0, f64::max)).collect();
    let (k, on, c) = texbuild::block_stats(x);
    format!(
        "{li:.2}\t{mst:.2}\t{tp:.2}\t{seam_step:.2} dB (p{:.0})\t{seam_flux:.2} dB (p{:.0})\t{k:.2}\t{on:.2}\t{c:.0}",
        pct(&inside, seam_step),
        pct(&fin, seam_flux)
    )
}

pub fn run_check(args: &[String]) -> Result<(), String> {
    println!("file\tLUFS (loop)\tmax short-term\ttrue peak\tseam 50 ms step\tseam flux\tkurtosis 2.5-6k\tonsets/s\tcentroid Hz");
    for f in &args[2..] {
        let (x, _) = crate::wav::read(f)?;
        let name = std::path::Path::new(f).file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
        println!("{name}\t{}", check(&x));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::texbuild::TexBlock;

    fn noise_bank() -> Bank {
        let mut r = Rng::new(7, "t");
        let mk = |r: &mut Rng, cls: u8, src: u32| TexBlock {
            cls,
            src_id: src,
            samples: (0..(12.0 * SR) as usize).map(|_| (r.normal() * 0.05) as f32).collect(),
        };
        // (blocks long enough for the 12 s segments)
        let mk2 = |r: &mut Rng, cls: u8, src: u32| {
            let mut b = mk(r, cls, src);
            let more = mk(r, cls, src);
            b.samples.extend(more.samples);
            b
        };
        Bank { blocks: vec![mk2(&mut r, 1, 10), mk2(&mut r, 1, 11), mk2(&mut r, 1, 10)] }
    }

    #[test]
    fn deterministic_and_levelled() {
        let b = noise_bank();
        let a = render_loop(&b, &LOOPS[1], 3, None).unwrap();
        let c = render_loop(&b, &LOOPS[1], 3, None).unwrap();
        assert_eq!(a.0, c.0, "same seed, same samples");
        assert!((a.1 - LOOPS[1].lufs).abs() < 0.05, "levelled: {}", a.1);
        assert_eq!(a.3.streams, 2);
    }

    #[test]
    fn the_wrap_is_a_crossfade() {
        let b = noise_bank();
        let (x, ..) = render_loop(&b, &LOOPS[1], 5, None).unwrap();
        let s = check(&x);
        // the seam's 50 ms step is no outlier
        let p: f64 = s.split("(p").nth(1).unwrap().split(')').next().unwrap().parse().unwrap();
        assert!(p < 99.0, "seam step percentile {p}: {s}");
    }
}
