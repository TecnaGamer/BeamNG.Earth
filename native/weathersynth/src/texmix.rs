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
const XF_S: f64 = 0.5;
const CORE_MIN_S: f64 = 2.0;
const CORE_MAX_S: f64 = 5.0;

/// A loop: (file stem, bank class, the length s, its target LUFS, low-pass corners applied after (muffled))
pub struct LoopSpec {
    pub name: &'static str,
    pub class: &'static str,
    pub secs: f64,
    pub lufs: f64,
    pub lowpass: &'static [f64],
}

pub const LOOPS: [LoopSpec; 14] = [
    LoopSpec { name: "light", class: "rain_light", secs: 30.0, lufs: -30.2, lowpass: &[] },
    LoopSpec { name: "medium", class: "rain_moderate", secs: 30.0, lufs: -24.2, lowpass: &[] },
    LoopSpec { name: "heavy", class: "rain_heavy", secs: 30.0, lufs: -17.2, lowpass: &[] },
    LoopSpec { name: "roof_light", class: "roof_light", secs: 30.0, lufs: -30.0, lowpass: &[] },
    LoopSpec { name: "roof", class: "roof", secs: 30.0, lufs: -24.1, lowpass: &[] },
    LoopSpec { name: "roof_heavy", class: "roof_heavy", secs: 30.0, lufs: -17.2, lowpass: &[] },
    // muffled (a): the outdoor medium rain through the mod's occlusion (two 900 Hz low-passes), as today's file
    LoopSpec { name: "muffled", class: "rain_moderate", secs: 30.0, lufs: -25.9, lowpass: &[900.0, 900.0] },
    // muffled (b): real takes from under a bridge / deck
    LoopSpec { name: "muffled_under", class: "rain_under", secs: 30.0, lufs: -25.9, lowpass: &[] },
    LoopSpec { name: "drips", class: "drips", secs: 30.0, lufs: -34.0, lowpass: &[] },
    LoopSpec { name: "gutter", class: "gutter", secs: 30.0, lufs: -28.0, lowpass: &[] },
    LoopSpec { name: "wind_light", class: "wind_light", secs: 60.0, lufs: -36.0, lowpass: &[] },
    LoopSpec { name: "wind_moderate", class: "wind_moderate", secs: 60.0, lufs: -30.0, lowpass: &[] },
    LoopSpec { name: "wind_strong", class: "wind_strong", secs: 60.0, lufs: -24.0, lowpass: &[] },
    LoopSpec { name: "hail", class: "hail", secs: 30.0, lufs: -20.0, lowpass: &[] },
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
}

/// One class's texture on a circular timeline of n samples (not levelled).
pub fn texture(bank: &Bank, class: &str, seed: u64, n: usize) -> Option<(Vec<f64>, Info)> {
    let p = pool(bank, class);
    if p.is_empty() {
        return None;
    }
    let streams = if p.len() >= 2 { 2 } else { 1 };
    let xf = (XF_S * SR) as usize;
    let mut out = vec![0.0f64; n];
    let mut segs = 0;
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
        while pos < end {
            let rem = (end - pos) as f64 / SR;
            let mut core = rng.range(CORE_MIN_S, CORE_MAX_S);
            if rem - core < CORE_MIN_S {
                core = if rem <= CORE_MAX_S { rem } else { rem / 2.0 };
            }
            let core_n = ((core * SR) as usize).min(end - pos).max(1);
            let len = core_n + xf;
            let vs = rng.range(0.97, 1.03);
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
    Some((out, Info { segments: segs, sources, streams }))
}

/// A finished loop: texture, its low-passes and a 30 Hz high-pass (circular), levelled. (samples, LUFS, true peak)
pub fn render_loop(bank: &Bank, spec: &LoopSpec, seed: u64) -> Option<(Vec<f64>, f64, f64, Info)> {
    let n = (spec.secs * SR) as usize;
    let (mut x, info) = texture(bank, spec.class, seed, n)?;
    for &fc in spec.lowpass {
        Biquad::lowpass(SR, fc, std::f64::consts::FRAC_1_SQRT_2).run_circular(&mut x);
    }
    Biquad::highpass(SR, 30.0, std::f64::consts::FRAC_1_SQRT_2).run_circular(&mut x);
    // level, then while the true peak is over CEIL_DB a circular look-ahead limiter at LIM_DB and level again
    let mut tp = 0.0;
    for _ in 0..4 {
        let l = loudness::integrated(&x, SR, true);
        let g = m::undb(spec.lufs - l);
        for v in x.iter_mut() {
            *v *= g;
        }
        tp = loudness::true_peak_db(&x, true);
        if tp <= CEIL_DB {
            break;
        }
        limit_loop(&mut x, LIM_DB);
    }
    let li = loudness::integrated(&x, SR, true);
    Some((x, li, tp, info))
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
    std::fs::create_dir_all(format!("{dir}/wav")).map_err(|e| e.to_string())?;
    println!("file\tsegments\tstreams\tsources\tLUFS\ttrue peak dBTP");
    for spec in LOOPS.iter() {
        if let Some(o) = &only {
            if !o.iter().any(|n| n == spec.name) {
                continue;
            }
        }
        let Some((x, li, tp, info)) = render_loop(&bank, spec, seed) else {
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
        println!("{stem}\t{}\t{}\t{}\t{li:.2}\t{tp:.2}", info.segments, info.streams, src.join(","));
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
        Bank { blocks: vec![mk(&mut r, 1, 10), mk(&mut r, 1, 11), mk(&mut r, 1, 10)] }
    }

    #[test]
    fn deterministic_and_levelled() {
        let b = noise_bank();
        let a = render_loop(&b, &LOOPS[1], 3).unwrap();
        let c = render_loop(&b, &LOOPS[1], 3).unwrap();
        assert_eq!(a.0, c.0, "same seed, same samples");
        assert!((a.1 - LOOPS[1].lufs).abs() < 0.05, "levelled: {}", a.1);
        assert_eq!(a.3.streams, 2);
    }

    #[test]
    fn the_wrap_is_a_crossfade() {
        let b = noise_bank();
        let (x, ..) = render_loop(&b, &LOOPS[1], 5).unwrap();
        let s = check(&x);
        // the seam's 50 ms step is no outlier
        let p: f64 = s.split("(p").nth(1).unwrap().split(')').next().unwrap().parse().unwrap();
        assert!(p < 99.0, "seam step percentile {p}: {s}");
    }
}
