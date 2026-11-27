//! Click and glitch metrics for a rendered stream (mono WAV), per segment:
//!
//!   weathersynth click-check [--segments S] [--skip S] FILE.wav ...
//!
//! - **jumps:** sample-to-sample steps |x[n] - x[n-1]| over 10x the segment's RMS step, per minute, and the largest
//!   as a multiple of that RMS (a hard cut or a grain starting mid-waveform shows here);
//! - **HF spikes:** the 6 kHz high-passed envelope in 1 ms frames against its running median over 101 frames (~100 ms):
//!   frames 15 dB or more over it, per minute (crackle, clicks);
//! - **block steps:** the level in 512-sample blocks (11.6 ms) against the previous block: the 99.9th percentile of the
//!   step (dB), and steps over 10 dB per minute (pumping, gating, scene switches);
//! - **clip:** samples at or over 0.999 full scale.
//!
//! Real rain has drops that stand out too, so the numbers mean most next to the offline loops of the same states.

use crate::dsp::Biquad;
use crate::m;

pub struct Metrics {
    pub jumps_per_min: f64,
    pub max_jump: f64,
    pub spikes_per_min: f64,
    pub step_p999: f64,
    pub steps10_per_min: f64,
    pub clip: usize,
    pub rms_db: f64,
}

pub fn metrics(x: &[f64], fs: f64) -> Metrics {
    let n = x.len().max(2);
    let mins = n as f64 / fs / 60.0;
    let d: Vec<f64> = x.windows(2).map(|w| w[1] - w[0]).collect();
    let sd = m::sqrt(d.iter().map(|v| v * v).sum::<f64>() / d.len().max(1) as f64).max(1e-30);
    let jumps = d.iter().filter(|v| v.abs() > 10.0 * sd).count();
    let max_jump = d.iter().fold(0.0f64, |a, v| a.max(v.abs())) / sd;
    // HF spikes
    let mut h1 = Biquad::highpass(fs, 6000.0, 0.7071);
    let mut h2 = Biquad::highpass(fs, 6000.0, 0.7071);
    let hp: Vec<f64> = x.iter().map(|&v| h2.tick(h1.tick(v))).collect();
    let fl = (fs * 0.001) as usize;
    let e: Vec<f64> = hp.chunks(fl).map(|c| 10.0 * m::log10(c.iter().map(|v| v * v).sum::<f64>() / c.len() as f64 + 1e-30)).collect();
    let mut spikes = 0;
    let half = 50;
    let mut win: Vec<f64> = Vec::with_capacity(2 * half + 1);
    for i in 0..e.len() {
        let (a, b) = (i.saturating_sub(half), (i + half + 1).min(e.len()));
        win.clear();
        win.extend_from_slice(&e[a..b]);
        win.sort_by(|p, q| p.partial_cmp(q).unwrap());
        if e[i] - win[win.len() / 2] >= 15.0 {
            spikes += 1;
        }
    }
    // block steps
    let lv: Vec<f64> = x.chunks(512).map(|c| 10.0 * m::log10(c.iter().map(|v| v * v).sum::<f64>() / c.len() as f64 + 1e-20)).collect();
    let mut steps: Vec<f64> = lv.windows(2).map(|w| (w[1] - w[0]).abs()).collect();
    let steps10 = steps.iter().filter(|&&s| s > 10.0).count();
    steps.sort_by(|p, q| p.partial_cmp(q).unwrap());
    let p999 = if steps.is_empty() { 0.0 } else { steps[((steps.len() - 1) as f64 * 0.999) as usize] };
    let rms = m::sqrt(x.iter().map(|v| v * v).sum::<f64>() / n as f64);
    Metrics {
        jumps_per_min: jumps as f64 / mins,
        max_jump,
        spikes_per_min: spikes as f64 / mins,
        step_p999: p999,
        steps10_per_min: steps10 as f64 / mins,
        clip: x.iter().filter(|v| v.abs() >= 0.999).count(),
        rms_db: 20.0 * m::log10(rms + 1e-30),
    }
}

fn arg(args: &[String], key: &str) -> Option<String> {
    args.iter().position(|a| a == key).and_then(|i| args.get(i + 1).cloned())
}

pub fn run(args: &[String]) -> Result<(), String> {
    let seg: f64 = arg(args, "--segments").and_then(|v| v.parse().ok()).unwrap_or(0.0);
    let skip: f64 = arg(args, "--skip").and_then(|v| v.parse().ok()).unwrap_or(0.0);
    println!("file\tsegment\trms dBFS\tjumps/min (>10x)\tmax jump (x RMS step)\tHF spikes/min (>=15 dB)\tblock step p99.9 dB\tsteps >10 dB/min\tclipped");
    let mut i = 2;
    while i < args.len() {
        let a = &args[i];
        if a == "--segments" || a == "--skip" {
            i += 2;
            continue;
        }
        let (x, fs) = crate::wav::read(a)?;
        let fs = fs as f64;
        let len = if seg > 0.0 { (seg * fs) as usize } else { x.len() };
        let sk = (skip * fs) as usize;
        for (k, c) in x.chunks(len.max(1)).enumerate() {
            let c = if c.len() > sk + (fs as usize) { &c[sk..] } else { c };
            let mt = metrics(c, fs);
            println!(
                "{}\t{}\t{:.1}\t{:.1}\t{:.1}\t{:.0}\t{:.1}\t{:.1}\t{}",
                a.rsplit('/').next().unwrap_or(a),
                k,
                mt.rms_db,
                mt.jumps_per_min,
                mt.max_jump,
                mt.spikes_per_min,
                mt.step_p999,
                mt.steps10_per_min,
                mt.clip
            );
        }
        i += 1;
    }
    Ok(())
}
