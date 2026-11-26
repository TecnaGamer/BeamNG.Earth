//! Live recorded rain and wind ("Rain sound: Recorded" while the weather sounds are Live): the texture bank's blocks
//! (texbuild.rs) mixed as texmix.rs mixes its loops, continuously, steered by the rain rate, the wind, shelter and the
//! car's cabin.
//!
//! - **Layers:** outdoor light / medium / heavy, the car roof's three, and wind light / moderate / strong; each is two
//!   streams of crossfaded segments (texmix: U(2, 5) s, varispeed U(0.97, 1.03), +/-1 dB, 0.5 s equal-power joins).
//! - **Weights:** rain classes by log10(rate) between the centres 0.7 / 5 / 35 mm/h, wind classes by speed between
//!   3 / 8 / 15 m/s, as triangles whose square roots are the gains (equal power: two uncorrelated layers crossfade
//!   without a dip). Wind fades in from 1 to 3 m/s.
//! - **Mix like the files:** outside x shelter (x RAIN_CABIN in the car), the roof x cabin x shelter x RAIN_ROOF, the
//!   muffled path (the outdoor mix through two 900 Hz low-passes, levelled as bne_rain_muffled) x (1 - shelter) x
//!   RAIN_UNDER_ROOF. Each layer's gain is set so it alone plays at its loop's loudness (texmix::LOOPS), so live and
//!   pre-recorded match at the same volume.
//! - **Cost:** a layer whose weight is zero is skipped sample by sample (nothing at all when dry and calm); a few
//!   dozen multiply-adds per sample in rain.
//! - **Deterministic:** the streams' draws are named rng.rs streams and every segment starts on a sample, so a fixed
//!   control timeline gives the same samples for any block size (test below).

use crate::dsp::Biquad;
use crate::loudness;
use crate::m;
use crate::rng::Rng;
use crate::texbuild::Bank;
use crate::texmix::{self, cubic};
use std::sync::Arc;

pub const FS: f64 = 44100.0;
const XF: usize = 22050;
pub const RAIN_CENTRES: [f64; 3] = [0.7, 5.0, 35.0];
pub const WIND_CENTRES: [f64; 3] = [3.0, 8.0, 15.0];
const RAIN_CABIN: f64 = 0.35;
const RAIN_ROOF: f64 = 0.9;
const RAIN_UNDER_ROOF: f64 = 0.7;
const SMOOTH_S: f64 = 0.3;

/// Pure: equal-power weights of three classes for x between centres c (log10 for rain): sqrt of the triangles.
pub fn weights(v: f64, c: &[f64; 3], log: bool) -> [f64; 3] {
    let f = |x: f64| if log { m::log10(x.max(1e-3)) } else { x };
    let x = f(v);
    let (a, b, d) = (f(c[0]), f(c[1]), f(c[2]));
    let mut w = [0.0; 3];
    if x <= a {
        w[0] = 1.0;
    } else if x >= d {
        w[2] = 1.0;
    } else if x <= b {
        let t = (x - a) / (b - a);
        w[0] = 1.0 - t;
        w[1] = t;
    } else {
        let t = (x - b) / (d - b);
        w[1] = 1.0 - t;
        w[2] = t;
    }
    [m::sqrt(w[0]), m::sqrt(w[1]), m::sqrt(w[2])]
}

/// The equal-power fade-in, sin(pi/2 (i + 0.5) / XF), once (its mirror is the fade-out's cos): no sin / cos per
/// sample. libm, so the same table on every platform.
fn fade() -> &'static [f64] {
    static T: std::sync::OnceLock<Vec<f64>> = std::sync::OnceLock::new();
    T.get_or_init(|| (0..XF).map(|i| m::sin(0.5 * m::PI * (i as f64 + 0.5) / XF as f64)).collect())
}

#[derive(Clone, Copy, Debug)]
pub struct Controls {
    pub rain_mm_h: f64,
    pub wind_ms: f64,
    pub shelter: f64,
    pub cabin: f64,
    /// the rain's volume (the mod's rain volume x the channel x the duck)
    pub rain_volume: f64,
    /// the wind's volume (the channel)
    pub wind_volume: f64,
    pub muted: bool,
}

#[derive(Clone, Copy)]
struct Seg {
    block: usize,
    st: f64,
    vs: f64,
    g: f64,
    i: usize,
    core: usize,
}

struct Stream {
    rng: Rng,
    blocks: Vec<usize>,
    segs: [Option<Seg>; 2],
    last: Option<(usize, f64)>,
}

impl Stream {
    fn new_seg(&mut self, bank: &Bank, fade_in: bool) -> Option<Seg> {
        let core = (self.rng.range(2.0, 5.0) * FS) as usize;
        let len = core + XF;
        let vs = self.rng.range(0.97, 1.03);
        let span = len as f64 * vs + 6.0;
        let fits: Vec<usize> = self.blocks.iter().cloned().filter(|&k| bank.blocks[k].samples.len() as f64 > span).collect();
        if fits.is_empty() {
            return None;
        }
        let tot: f64 = fits.iter().map(|&k| bank.blocks[k].samples.len() as f64).sum();
        let (mut bk, mut st) = (fits[0], 1.0);
        for _ in 0..20 {
            let mut r = self.rng.f() * tot;
            bk = *fits.last().unwrap();
            for &k in &fits {
                let l = bank.blocks[k].samples.len() as f64;
                if r < l {
                    bk = k;
                    break;
                }
                r -= l;
            }
            st = 1.0 + self.rng.f() * (bank.blocks[bk].samples.len() as f64 - span);
            match self.last {
                Some((lb, ls)) if lb == bk && (ls - st).abs() < FS => continue,
                _ => break,
            }
        }
        self.last = Some((bk, st));
        let g = m::undb(self.rng.range(-1.0, 1.0));
        Some(Seg { block: bk, st, vs, g, i: if fade_in { 0 } else { XF }, core })
    }

    #[inline]
    fn tick(&mut self, bank: &Bank) -> f64 {
        let mut y = 0.0;
        let mut spawn = false;
        for s in self.segs.iter_mut() {
            if let Some(sg) = s {
                let x = &bank.blocks[sg.block].samples;
                let w = if sg.i < XF {
                    fade()[sg.i]
                } else if sg.i >= sg.core {
                    fade()[XF - 1 - (sg.i - sg.core)]
                } else {
                    1.0
                };
                y += cubic(x, sg.st + sg.i as f64 * sg.vs) * w * sg.g;
                sg.i += 1;
                if sg.i == sg.core {
                    spawn = true;
                }
                if sg.i >= sg.core + XF {
                    *s = None;
                }
            }
        }
        if spawn {
            let n = self.new_seg(bank, true);
            if let Some(slot) = self.segs.iter_mut().find(|s| s.is_none()) {
                *slot = n;
            }
        }
        y
    }
}

struct Layer {
    streams: Vec<Stream>,
    sg: Vec<f64>,
    gain: f64,
    w: f64,
}

impl Layer {
    fn new(bank: &Bank, class: &str, seed: u64, lufs: f64) -> Option<Layer> {
        let p = texmix::pool(bank, class);
        if p.is_empty() {
            return None;
        }
        let ns = if p.len() >= 2 { 2 } else { 1 };
        let mut streams = Vec::new();
        for s in 0..ns {
            let blocks: Vec<usize> = p.iter().enumerate().filter(|(i, _)| i % ns == s).flat_map(|(_, (_, v))| v.clone()).collect();
            let mut st = Stream { rng: Rng::new(seed, &format!("live-tex-{class}-{s}")), blocks, segs: [None, None], last: None };
            st.segs[0] = st.new_seg(bank, false);
            // the second stream starts part-way into its segment, so the two never join at the same moment
            if let Some(sg) = st.segs[0].as_mut() {
                if s == 1 {
                    sg.i += sg.core.saturating_sub(XF) / 2;
                }
            }
            streams.push(st);
        }
        // blocks sit at -30 LUFS; uncorrelated streams whose gains' powers sum to 1 keep that
        let gain = m::undb(lufs + 30.0);
        Some(Layer { streams, sg: texmix::stream_gains(class, ns), gain, w: 0.0 })
    }
    #[inline]
    fn tick(&mut self, bank: &Bank) -> f64 {
        let mut y = 0.0;
        for (s, g) in self.streams.iter_mut().zip(&self.sg) {
            y += s.tick(bank) * g;
        }
        y * self.gain
    }
}

pub struct LiveTex {
    bank: Arc<Bank>,
    /// outdoor 0-2, roof 3-5, wind 6-8 (None: no blocks of that class)
    layers: Vec<Option<Layer>>,
    target: Controls,
    // smoothed per sample
    sh: f64,
    cab: f64,
    rv: f64,
    wv: f64,
    a: f64,
    muff: [Biquad; 2],
    muff_gain: f64,
}

const LAYERS: [(&str, &str); 9] = [
    ("rain_light", "light"),
    ("rain_moderate", "medium"),
    ("rain_heavy", "heavy"),
    ("roof_light", "roof_light"),
    ("roof", "roof"),
    ("roof_heavy", "roof_heavy"),
    ("wind_light", "wind_light"),
    ("wind_moderate", "wind_moderate"),
    ("wind_strong", "wind_strong"),
];

fn loop_lufs(name: &str) -> f64 {
    texmix::LOOPS.iter().find(|l| l.name == name).map(|l| l.lufs).unwrap_or(-30.0)
}

impl LiveTex {
    pub fn new(bank: Arc<Bank>, seed: u64) -> LiveTex {
        let layers = LAYERS.iter().map(|(c, n)| Layer::new(&bank, c, seed, loop_lufs(n))).collect();
        // the muffled path's level: a short medium texture through the two low-passes, levelled as the muffled loop
        let lp = || Biquad::lowpass(FS, 900.0, std::f64::consts::FRAC_1_SQRT_2);
        let muff_gain = match texmix::texture(&bank, "rain_moderate", seed, (10.0 * FS) as usize) {
            Some((mut x, _)) => {
                let l0 = loudness::integrated(&x, FS, true);
                lp().run_circular(&mut x);
                lp().run_circular(&mut x);
                m::undb(loop_lufs("muffled") - loop_lufs("medium") - (loudness::integrated(&x, FS, true) - l0))
            }
            None => 0.0,
        };
        let c = Controls { rain_mm_h: 0.0, wind_ms: 0.0, shelter: 1.0, cabin: 0.0, rain_volume: 0.0, wind_volume: 0.0, muted: true };
        LiveTex { bank, layers, target: c, sh: 1.0, cab: 0.0, rv: 0.0, wv: 0.0, a: 1.0 - m::exp(-1.0 / (SMOOTH_S * FS)), muff: [lp(), lp()], muff_gain }
    }

    pub fn set(&mut self, c: Controls) {
        self.target = c;
    }

    /// Adds the next out.len() samples to out.
    pub fn process(&mut self, out: &mut [f32]) {
        let t = self.target;
        let rw = weights(t.rain_mm_h, &RAIN_CENTRES, true);
        let ww = weights(t.wind_ms, &WIND_CENTRES, false);
        let wind_on = ((t.wind_ms - 1.0) / 2.0).clamp(0.0, 1.0);
        let wind_on = wind_on * wind_on * (3.0 - 2.0 * wind_on);
        let raining = t.rain_mm_h > 0.01;
        let (rv_t, wv_t) = if t.muted { (0.0, 0.0) } else { (if raining { t.rain_volume } else { 0.0 }, t.wind_volume * wind_on) };
        let cab_t = t.cabin.clamp(0.0, 1.0);
        let sh_t = t.shelter.clamp(0.0, 1.0);
        let bank = self.bank.clone();
        let a = self.a;
        for o in out.iter_mut() {
            self.sh += a * (sh_t - self.sh);
            self.cab += a * (cab_t - self.cab);
            self.rv += a * (rv_t - self.rv);
            self.wv += a * (wv_t - self.wv);
            if rv_t == 0.0 && self.rv < 1e-6 {
                self.rv = 0.0;
            }
            if wv_t == 0.0 && self.wv < 1e-6 {
                self.wv = 0.0;
            }
            let mut outdoor = 0.0;
            let mut roof = 0.0;
            let mut wind = 0.0;
            for (k, l) in self.layers.iter_mut().enumerate() {
                let Some(l) = l else { continue };
                let (wt, vol) = match k {
                    0..=2 => (rw[k], self.rv),
                    3..=5 => (rw[k - 3], if self.cab > 1e-4 { self.rv } else { 0.0 }),
                    _ => (ww[k - 6], self.wv),
                };
                let tgt = if vol > 0.0 { wt } else { 0.0 };
                l.w += a * (tgt - l.w);
                if tgt == 0.0 && l.w < 1e-5 {
                    l.w = 0.0;
                    continue;
                }
                let y = l.tick(&bank) * l.w;
                match k {
                    0..=2 => outdoor += y,
                    3..=5 => roof += y,
                    _ => wind += y,
                }
            }
            let mut y = 0.0;
            if self.rv > 0.0 {
                let open = outdoor * self.sh * (1.0 - self.cab + self.cab * RAIN_CABIN);
                let muff = if self.sh < 0.999 {
                    let m0 = self.muff[0].tick(outdoor);
                    self.muff[1].tick(m0) * self.muff_gain * (1.0 - self.sh) * RAIN_UNDER_ROOF
                } else {
                    0.0
                };
                y += (open + muff + roof * self.cab * self.sh * RAIN_ROOF) * self.rv;
            }
            if self.wv > 0.0 {
                // wind: damped in the car like the outside rain, quieter under a roof
                y += wind * self.wv * (1.0 - self.cab + self.cab * RAIN_CABIN) * (0.4 + 0.6 * self.sh);
            }
            *o += y as f32;
        }
    }
}

/// The live recorded engine through fixed conditions, rendered offline (no device): (label, samples) per condition,
/// 20 s each after a 3 s settle, for the loudness check against the files.
pub fn bench(bank: Arc<Bank>, block: usize) -> Vec<(String, Vec<f64>)> {
    let conds = [
        ("light", 0.7, 0.0, 1.0, 0.0),
        ("medium", 5.0, 0.0, 1.0, 0.0),
        ("heavy", 35.0, 0.0, 1.0, 0.0),
        ("roof_heavy", 35.0, 0.0, 1.0, 1.0),
        ("under_medium", 5.0, 0.0, 0.0, 0.0),
        ("wind_8ms", 0.0, 8.0, 1.0, 0.0),
    ];
    let mut out = Vec::new();
    // a sweep to hear the classes in turn: 6 s each of light, the crossfade to medium, medium, to heavy, heavy, then
    // heavy in the car and under a roof (60 s), the controls stepped every 0.25 s as the mod sends them
    {
        let mut lt = LiveTex::new(bank.clone(), 1);
        let n = (60.0 * FS) as usize;
        let mut buf = vec![0.0f32; n];
        let step = (0.25 * FS) as usize;
        for (k, c) in buf.chunks_mut(step).enumerate() {
            let t = k as f64 * 0.25;
            let lr = |a: f64, b: f64, u: f64| m::pow(10.0, m::log10(a) + (m::log10(b) - m::log10(a)) * u.clamp(0.0, 1.0));
            let rate = if t < 6.0 { 0.7 } else if t < 12.0 { lr(0.7, 5.0, (t - 6.0) / 6.0) } else if t < 18.0 { 5.0 } else if t < 24.0 { lr(5.0, 35.0, (t - 18.0) / 6.0) } else { 35.0 };
            let (sh, cab) = if t < 36.0 { (1.0, 0.0) } else if t < 48.0 { (1.0, 1.0) } else { (0.0, 0.0) };
            lt.set(Controls { rain_mm_h: rate, wind_ms: 0.0, shelter: sh, cabin: cab, rain_volume: 1.0, wind_volume: 0.0, muted: false });
            for v in c.chunks_mut(block) {
                lt.process(v);
            }
        }
        out.push(("sweep".to_string(), buf.iter().map(|v| *v as f64).collect()));
    }
    for (name, r, w, sh, cab) in conds {
        let mut lt = LiveTex::new(bank.clone(), 1);
        lt.set(Controls { rain_mm_h: r, wind_ms: w, shelter: sh, cabin: cab, rain_volume: 1.0, wind_volume: 1.0, muted: false });
        let n = (23.0 * FS) as usize;
        let mut buf = vec![0.0f32; n];
        for c in buf.chunks_mut(block) {
            lt.process(c);
        }
        out.push((name.to_string(), buf[(3.0 * FS) as usize..].iter().map(|v| *v as f64).collect()));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::texbuild::TexBlock;

    fn bank() -> Arc<Bank> {
        let mut r = Rng::new(3, "b");
        let mut mk = |cls: u8, src: u32| TexBlock { cls, src_id: src, samples: (0..(12.0 * FS) as usize).map(|_| (r.normal() * 0.03) as f32).collect() };
        Arc::new(Bank { blocks: vec![mk(0, 1), mk(0, 2), mk(1, 3), mk(1, 4), mk(2, 5), mk(10, 6), mk(10, 7)] })
    }

    #[test]
    fn equal_power_weights() {
        for r in [0.1, 0.7, 2.0, 5.0, 12.0, 35.0, 80.0] {
            let w = weights(r, &RAIN_CENTRES, true);
            let p: f64 = w.iter().map(|v| v * v).sum();
            assert!((p - 1.0).abs() < 1e-12, "{r}: {w:?}");
        }
    }

    #[test]
    fn block_size_does_not_change_the_output() {
        let b = bank();
        let c = Controls { rain_mm_h: 3.0, wind_ms: 6.0, shelter: 0.6, cabin: 0.0, rain_volume: 0.8, wind_volume: 1.0, muted: false };
        let run = |blk: usize| {
            let mut lt = LiveTex::new(b.clone(), 9);
            lt.set(c);
            let mut v = vec![0.0f32; (8.0 * FS) as usize];
            for ch in v.chunks_mut(blk) {
                lt.process(ch);
            }
            v
        };
        assert_eq!(run(256), run(1024));
    }

    #[test]
    fn silent_when_dry_and_calm() {
        let b = bank();
        let mut lt = LiveTex::new(b, 1);
        lt.set(Controls { rain_mm_h: 0.0, wind_ms: 0.0, shelter: 1.0, cabin: 0.0, rain_volume: 1.0, wind_volume: 1.0, muted: false });
        let mut v = vec![0.0f32; 4096];
        lt.process(&mut v);
        assert!(v.iter().all(|x| *x == 0.0));
    }
}
